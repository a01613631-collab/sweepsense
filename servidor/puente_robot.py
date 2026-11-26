"""Puente entre el ESP32 (serial USB) y la base de datos.

- Lee las tramas del ESP32 y guarda lecturas, eventos y celdas recorridas.
- Lee los comandos pendientes (de la app o Alexa) y los manda al ESP32.

Protocolo (decisión D17 de la bóveda, 115200 baud, una trama cada 200 ms):
  ESP32 -> Pi: S,t_ms,encI,encD,yaw,tof0,tof1,tof2,piso,bump,ir,mV,mA,fuga,estado,alerta
               estado = ok | paro | fuga     alerta = texto sin comas ("ninguna" si no hay)
  Pi -> ESP32: M,vI,vD  (velocidades)  ·  C,turbina%,cepillo,bomba  ·  X (paro)
               R (quita el paro)  ·  Z (pone el yaw en 0)
  El paro X se queda trabado en el ESP32 hasta que llega R.
  El cepillo va con la turbina en el mismo MOSFET, así que el ESP32 ignora ese campo.

Ejecutar:
  python3 puente_robot.py                  # con el robot en /dev/ttyUSB0
  python3 puente_robot.py --puerto auto    # busca el ESP32 (/dev/serial/by-id/, ttyUSB0 o ttyACM0)
  python3 puente_robot.py --simular        # sin hardware: robot simulado para probar API y app
"""
import argparse
import glob
import math
import os
import random
import time
from datetime import datetime

import db
from navegacion import Navegacion

CUENTAS_POR_VUELTA = 495     # encoder GM25-370: verificar con 10 vueltas a mano (plan de pruebas)
DIAMETRO_RUEDA_M = 0.065
GUARDAR_CADA_S = 0.5         # una lectura en la base cada medio segundo
TURBINA = 80                 # % de la turbina al limpiar (la turbina necesita 50 % o más)
CARGA_POR_CONTACTOS = False  # False = la batería se carga aparte (decisión 1 del 2026-11-24): al
                             # llegar a la base el modo es en_espera, no cargando. True = D17 punto 7
V_VACIA = 10.5               # batería 3S: 0 % (deja carga para regresar a la base)
V_LLENA = 12.3               # 100 % (D17 punto 8). Provisional: se revisa al medir en E6.
                             # Si cambia, cambiarlo también en el firmware del ESP32 (V_LLENA)


def bateria_pct(mv):
    """Batería 3S: V_VACIA = 0 %, V_LLENA = 100 %.
    El ESP32 usa la misma fórmula para el tema MQTT roomba/robot/bateria (D17)."""
    return max(0, min(100, round((mv / 1000 - V_VACIA) / (V_LLENA - V_VACIA) * 100)))


def estacion_cargando(con):
    """Lo que dice la estación base: True o False.
    None si la estación no ha mandado nada en los últimos 10 s (por ejemplo, en el simulador)."""
    fila = con.execute("SELECT momento, cargando FROM estacion ORDER BY id DESC LIMIT 1").fetchone()
    if not fila:
        return None
    momento = datetime.fromisoformat(fila["momento"])
    if (datetime.now() - momento).total_seconds() > 10:
        return None
    return bool(fila["cargando"])


class Puente:
    def __init__(self, enviar, marcar_obstaculos=True):
        self.enviar = enviar           # función que manda una línea al ESP32
        self.marcar_obstaculos = marcar_obstaculos
        self.modo = "en_espera"
        self.nav = Navegacion()
        self.x = (db.BASE[0] + 0.5) * db.CELDA_M
        self.y = (db.BASE[1] + 0.5) * db.CELDA_M
        self.enc = None
        self.t_guardado = 0
        self.ultimo_choque = 0
        self.fuga_antes = False
        self.bateria_baja_avisada = False
        self.celda_antes = None
        self.bomba = 0                 # 1 si la tarea en curso es aspirar y trapear
        self.alerta_antes = "ninguna"
        self.t_rearme = 0              # cuándo se mandó la última R

    def rearmar(self):
        """Quita el paro trabado del ESP32 (comando R)."""
        self.enviar("R")
        self.t_rearme = time.time()

    # ---- comandos de la app / Alexa ----
    def atender_comandos(self, con):
        for c in db.comandos_pendientes(con):
            a, p = c["accion"], c["parametros"]
            if a == "iniciar":
                self.modo = "limpiando"
                self.bomba = 1 if p.get("tipo") == "aspirar_trapear" else 0
                self.rearmar()         # por si el robot quedó en paro
                self.enviar(f"C,{TURBINA},0,{self.bomba}")
                self.nav.iniciar(p.get("zona", "completa"))
            elif a == "pausar":
                self.modo = "pausado"
                self.enviar("X")
                self.enviar("C,0,0,0")
            elif a == "reanudar":
                self.modo = "limpiando"
                self.rearmar()
                self.enviar(f"C,{TURBINA},0,{self.bomba}")   # la bomba sigue como en la tarea
            elif a == "detener":
                self.modo = "en_espera"
                self.enviar("X")
                self.enviar("C,0,0,0")
                self.nav.detener()
            elif a == "regresar":
                self.modo = "regresando"
                self.rearmar()
                self.enviar("C,0,0,0")
                self.nav.regresar()
            db.comando_atendido(con, c["id"])

    # ---- trama del ESP32 ----
    def procesar(self, linea, con):
        partes = linea.strip().split(",")
        if len(partes) != 16 or partes[0] != "S":
            return
        try:
            _, t, encI, encD, yaw, tof0, tof1, tof2, piso, bump, ir, mv, ma, fuga, estado, alerta = partes
            encI, encD, bump, mv, ma, fuga = int(encI), int(encD), int(bump), int(mv), int(ma), int(fuga)
            yaw = float(yaw)
            tof = [int(tof0), int(tof1), int(tof2)]
        except ValueError:
            return

        self.odometria(encI, encD, yaw)
        cx = min(db.ANCHO - 1, max(0, int(self.x / db.CELDA_M)))
        cy = min(db.ALTO - 1, max(0, int(self.y / db.CELDA_M)))
        bat = bateria_pct(mv)

        # Eventos
        if (bump or min(tof) < 120) and time.time() - self.ultimo_choque > 2:
            self.ultimo_choque = time.time()
            db.evento(con, "obstaculo", f"Obstáculo en ({cx}, {cy})")
            ox, oy = cx + round(math.sin(math.radians(yaw))), cy - round(math.cos(math.radians(yaw)))
            # Solo se marca si esa celda no se limpió ya en esta sesión (si el robot pasó, no es obstáculo fijo)
            if self.marcar_obstaculos and 0 <= ox < db.ANCHO and 0 <= oy < db.ALTO:
                ya_pasado = con.execute(
                    """SELECT 1 FROM sesion_celda sc JOIN celdas_en_mapa c USING (id_celda)
                       WHERE sc.id_sesion = ? AND c.cx = ? AND c.cy = ?""",
                    (db.sesion_activa(con), ox, oy),
                ).fetchone()
                if not ya_pasado:
                    db.marcar_obstaculo(con, ox, oy)
        if fuga and not self.fuga_antes:
            self.enviar("C,0,0,0")
            self.modo = "pausado"
            db.evento(con, "fuga", "Agua detectada: bomba y turbina apagadas")
        self.fuga_antes = bool(fuga)
        # Alertas del ESP32: se anotan cuando cambian
        if alerta != self.alerta_antes and alerta != "ninguna":
            db.evento(con, "sistema", f"Alerta del robot: {alerta}")
        self.alerta_antes = alerta
        # Paro trabado en el ESP32 (llegó una X, por ejemplo desde la app MQTT): la Pi pasa a pausa
        # (se esperan 0.5 s después de una R: la trama que ya venía en camino todavía dice "paro")
        if estado == "paro" and self.modo in ("limpiando", "regresando") and time.time() - self.t_rearme > 0.5:
            self.modo = "pausado"
            self.enviar("C,0,0,0")
            db.evento(con, "sistema", "El robot recibió un paro (X): en pausa hasta reanudar")
        if bat < 20 and not self.bateria_baja_avisada and self.modo == "limpiando":
            self.bateria_baja_avisada = True
            db.evento(con, "bateria_baja", "Batería baja: regresando a la base")
            self.modo = "regresando"
            self.nav.regresar()
        if bat > 30:
            self.bateria_baja_avisada = False
        # Llegada a la base
        if CARGA_POR_CONTACTOS:
            # Carga por los contactos (D17 punto 7): el robot no ve su corriente de carga
            # (el INA219 está después del interruptor), así que manda la estación:
            # roomba/base/cargando = 1. Sin datos de la estación (simulador), la celda de la base.
            cargando = estacion_cargando(con)
            if cargando is None:
                en_base = (cx, cy) == db.BASE
            else:
                en_base = cargando
            if self.modo == "regresando" and en_base:
                self.modo = "cargando"
                self.enviar("C,0,0,0")
                self.enviar("Z")           # el yaw vuelve a 0 en la base
                db.evento(con, "base", "Robot en la base")
            if self.modo == "cargando" and (bat >= 100 or cargando is False):
                self.modo = "en_espera"
        else:
            # Sin contactos (decisión del 2026-11-24): la batería se carga aparte con un
            # eliminador y la estación ya no ve corriente de carga. La llegada se decide por
            # odometría: al llegar a la celda de la base se apaga todo y queda en espera.
            if self.modo == "regresando" and (cx, cy) == db.BASE:
                self.modo = "en_espera"
                self.enviar("C,0,0,0")
                self.enviar("Z")           # el yaw vuelve a 0 en la base
                db.evento(con, "base", "Robot en la base")

        # Mapa: celdas recorridas
        if self.modo == "limpiando" and (cx, cy) != self.celda_antes:
            db.marcar_celda(con, cx, cy)
            self.celda_antes = (cx, cy)
            if self.nav.termino(cx, cy):
                db.cerrar_sesion(con, bat)
                db.evento(con, "regreso", "Zona terminada: regresando a la base")
                self.modo = "regresando"
                self.enviar("C,0,0,0")
                self.nav.regresar()

        # Navegación (Adrián): decide velocidades con los sensores
        if self.modo in ("limpiando", "regresando"):
            vI, vD = self.nav.paso(cx=cx, cy=cy, yaw=yaw, tof=tof, piso=int(piso), bump=bump, ir=int(ir))
            self.enviar(f"M,{vI},{vD}")

        if time.time() - self.t_guardado >= GUARDAR_CADA_S:
            self.t_guardado = time.time()
            con.execute(
                "INSERT INTO lecturas (momento, modo, bateria, voltaje, corriente_ma, fuga, cx, cy) VALUES (?,?,?,?,?,?,?,?)",
                (db.ahora(), self.modo, bat, round(mv / 1000, 2), ma, fuga, cx, cy),
            )

    def odometria(self, encI, encD, yaw):
        if self.enc is None:
            self.enc = (encI, encD)
            return
        dI, dD = encI - self.enc[0], encD - self.enc[1]
        self.enc = (encI, encD)
        metros = (dI + dD) / 2 / CUENTAS_POR_VUELTA * math.pi * DIAMETRO_RUEDA_M
        # yaw de la IMU: 0° = hacia el frente del área (arriba del mapa)
        self.x += metros * math.sin(math.radians(yaw))
        self.y -= metros * math.cos(math.radians(yaw))


# ---------------- robot simulado ----------------

class RobotSimulado:
    """Genera tramas 'S,...' como si fuera el ESP32, para probar todo sin hardware."""

    def __init__(self):
        self.mv = 12300
        self.yaw = 0.0
        self.enc = 0
        self.lineas = []
        self.moviendo = False
        self.fuga = 0
        self.ruta = []
        self.i = 0
        self.pos = list(db.BASE)
        self.paro = False          # igual que el ESP32: X traba el paro hasta que llega R

    def escribir(self, linea):
        if linea == "X":
            self.paro = True
            self.moviendo = False
        elif linea == "R":
            self.paro = False
        elif linea == "Z":
            self.yaw = 0.0
        elif self.paro:
            return                 # con el paro trabado se ignoran M y C
        elif linea.startswith("C,") and linea != "C,0,0,0":
            self.moviendo = True
        elif linea == "C,0,0,0":
            self.moviendo = False

    def leer(self, puente):
        if puente.modo == "limpiando" and self.moviendo:
            if self.ruta is not puente.nav.ruta:      # empezó una limpieza nueva
                self.ruta = puente.nav.ruta or []
                self.i = 0
            if self.i < len(self.ruta):
                self.pos = list(self.ruta[self.i])
                self.i += 1
            self.mv -= 4
        elif puente.modo == "regresando":
            bx, by = db.BASE
            x, y = self.pos
            self.pos = [x + (bx > x) - (bx < x), y] if x != bx else [x, y + (by > y) - (by < y)]
            self.mv -= 2
        elif puente.modo == "cargando":
            self.mv = min(int(V_LLENA * 1000), self.mv + 25)
        # El simulador pone la posición directo (sin encoders)
        puente.x = (self.pos[0] + 0.5) * db.CELDA_M
        puente.y = (self.pos[1] + 0.5) * db.CELDA_M
        bump = 1 if random.random() < 0.04 and puente.modo == "limpiando" else 0
        ma = {"limpiando": 2600, "regresando": 900, "cargando": -1500}.get(puente.modo, 600) + random.randint(-40, 40)
        if self.paro:
            estado = "paro"
        elif self.fuga:
            estado = "fuga"
        else:
            estado = "ok"
        return f"S,{int(time.time()*1000)},0,0,{self.yaw},800,800,800,0,{bump},0,{self.mv},{ma},{self.fuga},{estado},ninguna"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--puerto", default="/dev/ttyUSB0", help="ruta del ESP32, o auto para buscarlo")
    ap.add_argument("--simular", action="store_true", help="no usa el ESP32; simula el robot")
    ap.add_argument("--ver", action="store_true", help="en --simular, también muestra las órdenes de motores (M,...)")
    args = ap.parse_args()
    db.crear()

    if args.simular:
        sim = RobotSimulado()

        def enviar(linea):
            # Muestra en pantalla cada orden que la Pi le manda al robot
            if not linea.startswith("M,") or args.ver:
                print(f"  Pi → robot   {linea}", flush=True)
            sim.escribir(linea)

        puente = Puente(enviar, marcar_obstaculos=False)  # los choques simulados no crean obstáculos
        print("Robot simulado corriendo. Ctrl+C para salir.")
        print("Cada segundo verás la línea que manda el robot y su estado.\n", flush=True)
        while True:
            with db.conectar() as con:
                puente.atender_comandos(con)
                trama = sim.leer(puente)
                puente.procesar(trama, con)
            cx = int(puente.x / db.CELDA_M)
            cy = int(puente.y / db.CELDA_M)
            bat = bateria_pct(int(trama.split(",")[11]))
            print(f"robot → Pi   {trama}", flush=True)
            print(f"             modo: {puente.modo:<11} celda: ({cx}, {cy})   batería: {bat} %", flush=True)
            time.sleep(1.0)
    else:
        robot_real(args.puerto)


def buscar_puerto():
    """Para --puerto auto. Primero /dev/serial/by-id/, porque ahí el nombre del ESP32
    no cambia aunque se desconecte y se vuelva a conectar (ttyUSB0 puede volverse ttyUSB1)."""
    por_id = sorted(glob.glob("/dev/serial/by-id/*"))
    if por_id:
        return por_id[0]
    for ruta in ("/dev/ttyUSB0", "/dev/ttyACM0"):
        if os.path.exists(ruta):
            return ruta
    return None


def robot_real(puerto):
    """Lee el ESP32 por USB. Si se desconecta, no se cae: espera 2 s y vuelve a intentar."""
    import serial  # pip install pyserial

    conexion = {"esp": None}       # el puerto abierto, o None si no hay ESP32

    def enviar(linea):
        if conexion["esp"] is not None:
            conexion["esp"].write((linea + "\n").encode())

    puente = Puente(enviar)
    ultimo_rx = time.time()
    paro_por_silencio = False      # para mandar la X una sola vez por cada pérdida de datos
    ya_conectado_antes = False
    ultima_trama_ok = time.time()

    while True:
        # 1. Sin ESP32: buscarlo cada 2 s
        if conexion["esp"] is None:
            ruta = buscar_puerto() if puerto == "auto" else puerto
            try:
                if ruta:
                    # Recién conectado, el CP2102 tarda en estar listo: si se abre de
                    # inmediato, a veces se queda a otra velocidad y solo llega basura
                    # (probado el 2026-11-25). Se espera 1 s y se pone la velocidad dos veces.
                    time.sleep(1)
                    esp = serial.Serial(ruta, 115200, timeout=0.1)
                    esp.baudrate = 9600
                    esp.baudrate = 115200
                    esp.reset_input_buffer()
                    conexion["esp"] = esp
                else:
                    conexion["esp"] = None
            except serial.SerialException:
                conexion["esp"] = None
            if conexion["esp"] is None:
                time.sleep(2)
                continue
            print(f"Conectado al ESP32 en {ruta}", flush=True)
            if ya_conectado_antes:
                with db.conectar() as con:
                    db.evento(con, "sistema", "El USB del ESP32 volvió a conectarse")
            ya_conectado_antes = True
            ultimo_rx = time.time()
            ultima_trama_ok = time.time()

        # 2. Con ESP32: comandos de la app, trama del robot y seguridad
        try:
            with db.conectar() as con:
                puente.atender_comandos(con)
                linea = conexion["esp"].readline().decode(errors="ignore")
                if linea:
                    # Solo una trama cuenta como «el robot está vivo»: la basura
                    # (puerto a otra velocidad) no debe evitar el paro por silencio
                    if linea.startswith("S,"):
                        ultimo_rx = time.time()
                        ultima_trama_ok = time.time()
                        paro_por_silencio = False
                    puente.procesar(linea, con)
            # Vigilante: llegan datos pero ninguna trama válida en 5 s (puerto a otra
            # velocidad o trabado) → se cierra y se vuelve a abrir el puerto
            if time.time() - ultima_trama_ok > 5:
                print("5 s sin tramas válidas del ESP32: vuelvo a abrir el puerto.", flush=True)
                raise serial.SerialException("sin tramas validas")
            # Seguridad: 0.5 s sin tramas mientras se mueve = paro (una vez) y pausa
            if time.time() - ultimo_rx > 0.5 and not paro_por_silencio and puente.modo in ("limpiando", "regresando"):
                paro_por_silencio = True
                enviar("X")
                puente.modo = "pausado"
                with db.conectar() as con:
                    db.evento(con, "sistema", "El robot dejó de mandar datos: paro (X) y pausa")
        except (serial.SerialException, OSError):
            print("Se perdió el USB del ESP32. Vuelvo a intentar en 2 s.", flush=True)
            try:
                conexion["esp"].close()
            except Exception:
                pass
            conexion["esp"] = None
            with db.conectar() as con:
                db.evento(con, "sistema", "Se desconectó el USB del ESP32")
                if puente.modo in ("limpiando", "regresando"):
                    puente.modo = "pausado"
            time.sleep(2)


if __name__ == "__main__":
    main()
