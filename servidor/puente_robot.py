"""Puente entre el ESP32 (serial USB) y la base de datos.

- Lee las tramas del ESP32 y guarda lecturas, eventos y celdas recorridas.
- Lee los comandos pendientes (de la app o Alexa) y los manda al ESP32.

Protocolo (decisión D17 de la bóveda, 115200 baud, una trama cada 200 ms):
  ESP32 -> Pi: S,t_ms,encI,encD,yaw,tof0,tof1,tof2,piso,bump,ir,mV,mA,fuga,estado,alerta
               estado = ok | paro | fuga     alerta = texto sin comas ("ninguna" si no hay)
  Pi -> ESP32: M,vI,vD  (velocidades)  ·  C,turbina%,cepillo,bomba  ·  X (paro)
               R (quita el paro)  ·  Z (pone el yaw en 0)
  El paro X se queda trabado en el ESP32 hasta que llega R. La Pi manda R con Iniciar y
  Reanudar (D17 punto 4), y con «A la base» solo si el paro lo puso la propia Pi.
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


MOVIENDO = ("limpiando", "regresando")

# Rampa de los motores: la navegación pide VEL de golpe (0 → 600) y eso jala mucha
# corriente y hace patinar las ruedas (y el ESP32 no tiene rampa para los motores, solo
# para la turbina). El puente sube cada rueda como máximo RAMPA_POR_TRAMA por trama
# (una cada 200 ms): de 0 a 600 en 0.8 s. Frenar o bajar la velocidad es inmediato.
RAMPA_POR_TRAMA = 150
# Casos en que la navegación básica se queda en un bucle (ver navegacion.py): el puente
# los corta con una pausa y un evento, sin cambiar nav.paso.
SIN_PISO_MAX_S = 2.0         # piso ≠ 0 tanto tiempo = lo levantaron o está atorado en un borde
SIN_AVANZAR_MAX_S = 10.0     # 10 s girando o retrocediendo sin avanzar = atorado (p. ej. un ToF en 0)
SALTO_ENCODER_MAX = 3000     # cuentas en una trama: más que eso no es movimiento, es un reinicio o basura
REPETIR_ALERTA_S = 60        # la misma alerta del ESP32 se anota como máximo una vez por minuto


def acercar(actual, objetivo, paso):
    """Rampa de una rueda: sube de a poco, baja (o frena) de golpe."""
    if actual * objetivo < 0:
        actual = 0                                   # cambio de sentido: primero se para
    if abs(objetivo) <= abs(actual):
        return objetivo
    return actual + max(-paso, min(paso, objetivo - actual))


class Puente:
    def __init__(self, enviar, marcar_obstaculos=True):
        self.enviar = enviar           # función que manda una línea al ESP32
        self.marcar_obstaculos = marcar_obstaculos
        self.modo = "en_espera"
        self.modo_antes_pausa = None   # limpiando o regresando: a dónde vuelve «reanudar»
        self.nav = Navegacion()
        self.x = (db.BASE[0] + 0.5) * db.CELDA_M
        self.y = (db.BASE[1] + 0.5) * db.CELDA_M
        self.enc = None
        self.t_esp = None              # último t_ms del ESP32 (si baja, el ESP32 se reinició)
        self.t_guardado = 0
        self.ultimo_choque = 0
        self.fuga_antes = False
        self.bateria_baja_avisada = False
        self.celda_antes = None
        self.bomba = 0                 # 1 si la tarea en curso es aspirar y trapear
        self.alerta_antes = "ninguna"
        self.alertas_anotadas = {}     # alerta -> cuándo se anotó (para no llenar los eventos)
        self.t_rearme = 0              # cuándo se mandó la última R
        self.x_propia = False          # True si el último paro (X) lo mandó la Pi
        self.estado_esp = "ok"         # último estado de la trama: ok | paro | fuga
        self.v = (0, 0)                # velocidades que se mandaron (para la rampa)
        self.t_sin_piso = None         # desde cuándo piso ≠ 0
        self.t_sin_avanzar = None      # desde cuándo la navegación no pide avanzar

    def rearmar(self):
        """Quita el paro trabado del ESP32 (comando R)."""
        self.enviar("R")
        self.t_rearme = time.time()
        self.x_propia = False

    def paro(self):
        """Manda X (paro trabado) y recuerda que fue la Pi, no la app MQTT."""
        self.enviar("X")
        self.x_propia = True

    def pausar(self, con, detalle, tipo="sistema", mandar_x=True):
        """Pasa a pausado y anota por qué. Recuerda si iba limpiando o regresando."""
        if self.modo in MOVIENDO:
            self.modo_antes_pausa = self.modo
        self.modo = "pausado"
        if mandar_x:
            self.paro()
        self.enviar("C,0,0,0")
        self.v = (0, 0)
        if detalle:
            db.evento(con, tipo, detalle)

    # ---- comandos de la app / Alexa ----
    def atender_comandos(self, con):
        for c in db.comandos_pendientes(con):
            a, p = c["accion"], c["parametros"]
            if a == "iniciar":
                self.modo = "limpiando"
                self.modo_antes_pausa = None
                self.bomba = 1 if p.get("tipo") == "aspirar_trapear" else 0
                self.rearmar()         # por si el robot quedó en paro (D17 punto 4)
                self.enviar(f"C,{TURBINA},0,{self.bomba}")
                self.nav.iniciar(p.get("zona", "completa"))
            elif a == "pausar":
                if self.modo in MOVIENDO:
                    self.pausar(con, None)
            elif a == "reanudar":
                self.reanudar(con)
            elif a == "detener":
                self.modo = "en_espera"
                self.modo_antes_pausa = None
                self.paro()
                self.enviar("C,0,0,0")
                self.v = (0, 0)
                self.nav.detener()
            elif a == "regresar":
                self.regresar(con)
            db.comando_atendido(con, c["id"])

    def reanudar(self, con):
        if self.modo != "pausado":
            return
        destino = self.modo_antes_pausa or "limpiando"
        # La API ya cerró la sesión (detener, a la base, zona terminada): no hay qué limpiar
        if destino == "limpiando" and db.sesion_activa(con) is None:
            db.evento(con, "sistema", "No hay una limpieza abierta para reanudar: usa Iniciar")
            return
        self.modo = destino
        self.modo_antes_pausa = None
        self.rearmar()                 # D17 punto 4: Reanudar manda R y después C y M
        if destino == "limpiando":
            self.enviar(f"C,{TURBINA},0,{self.bomba}")   # la bomba sigue como en la tarea
        else:
            self.enviar("C,0,0,0")
            self.nav.regresar()

    def regresar(self, con):
        """«A la base». D17 punto 4 solo permite quitar el paro (R) con Iniciar o Reanudar:
        un paro que llegó de fuera (X por MQTT) es una orden de seguridad de alguien que
        está viendo el robot, y «A la base» no debe quitarlo. Si el paro lo puso la propia
        Pi (Pausar, Detener, silencio), sí se quita, porque esa X solo era para detenerlo."""
        if self.fuga_antes:
            db.evento(con, "fuga", "Hay agua detectada: no voy a la base hasta revisar el robot")
            return
        if self.estado_esp == "paro" and not self.x_propia:
            if self.modo in MOVIENDO:
                self.pausar(con, None, mandar_x=False)
            db.evento(con, "sistema", "El robot está en paro (X externo): primero Reanudar, luego A la base")
            return
        self.modo = "regresando"
        self.modo_antes_pausa = None
        self.rearmar()
        self.enviar("C,0,0,0")
        self.nav.regresar()

    # ---- trama del ESP32 ----
    def procesar(self, linea, con):
        """Una línea del ESP32. Cualquier línea que no sea una trama S válida se ignora
        (líneas «#» de diagnóstico, avisos, basura del puerto a otra velocidad)."""
        partes = linea.strip().split(",")
        if len(partes) != 16 or partes[0] != "S":
            return False
        try:
            _, t, encI, encD, yaw, tof0, tof1, tof2, piso, bump, ir, mv, ma, fuga, estado, alerta = partes
            t, encI, encD, bump, mv, ma, fuga = int(t), int(encI), int(encD), int(bump), int(mv), int(ma), int(fuga)
            piso, ir = int(piso), int(ir)
            yaw = float(yaw)
            tof = [int(tof0), int(tof1), int(tof2)]
        except ValueError:
            return False
        if not math.isfinite(yaw):     # "nan" o "inf" volverían nan la posición
            return False
        self.estado_esp = estado

        # El ESP32 se reinició sin perder el USB: t_ms vuelve a empezar, los encoders a 0
        # y el yaw a 0 donde esté. La odometría vieja ya no sirve.
        if self.t_esp is not None and t < self.t_esp:
            self.enc = None
            self.x_propia = False      # el paro trabado se perdió con el reinicio
            if self.modo in MOVIENDO:
                self.pausar(con, "El ESP32 se reinició: pausa (la posición ya no es confiable)")
            else:
                db.evento(con, "sistema", "El ESP32 se reinició")
        self.t_esp = t

        if not self.odometria(encI, encD, yaw):
            db.evento(con, "sistema", "Salto raro en los encoders: se reinicia la referencia")
        cx = min(db.ANCHO - 1, max(0, int(self.x / db.CELDA_M)))
        cy = min(db.ALTO - 1, max(0, int(self.y / db.CELDA_M)))
        bat = bateria_pct(mv)

        # Eventos. Un ToF en 0 es un sensor que falla (el ESP32 lo manda así para que la
        # navegación no avance), no un obstáculo: no se anota ni se marca en el mapa.
        tof_validos = [d for d in tof if d > 0]
        cerca = bool(tof_validos) and min(tof_validos) < 120
        if (bump or cerca) and time.time() - self.ultimo_choque > 2:
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
            # El ESP32 ya apagó bomba y turbina (D17 punto 5); la Pi deja de mover el robot
            if self.modo in MOVIENDO:
                self.pausar(con, None, mandar_x=False)
            else:
                self.enviar("C,0,0,0")
            db.evento(con, "fuga", "Agua detectada: bomba y turbina apagadas")
        self.fuga_antes = bool(fuga)
        # Alertas del ESP32: se anotan cuando cambian, y la misma como máximo una vez por
        # minuto (un ToF que va y viene no debe llenar el historial)
        if alerta != self.alerta_antes and alerta != "ninguna":
            if time.time() - self.alertas_anotadas.get(alerta, 0) > REPETIR_ALERTA_S:
                self.alertas_anotadas[alerta] = time.time()
                db.evento(con, "sistema", f"Alerta del robot: {alerta}")
        self.alerta_antes = alerta
        # Paro trabado en el ESP32 (llegó una X, por ejemplo desde la app MQTT): la Pi pasa a pausa
        # (se esperan 0.5 s después de una R: la trama que ya venía en camino todavía dice "paro")
        if estado == "paro" and self.modo in MOVIENDO and time.time() - self.t_rearme > 0.5:
            self.pausar(con, "El robot recibió un paro (X): en pausa hasta reanudar", mandar_x=False)
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
        if self.modo in MOVIENDO:
            self.revisar_bucles(con, tof, piso)
        if self.modo in MOVIENDO:
            vI, vD = self.nav.paso(cx=cx, cy=cy, yaw=yaw, tof=tof, piso=piso, bump=bump, ir=ir)
            self.sin_avanzar(con, vI, vD)
        if self.modo in MOVIENDO:
            self.v = (acercar(self.v[0], vI, RAMPA_POR_TRAMA), acercar(self.v[1], vD, RAMPA_POR_TRAMA))
            self.enviar(f"M,{self.v[0]},{self.v[1]}")
        else:
            self.v = (0, 0)            # al volver a moverse, la rampa empieza desde 0
            self.t_sin_piso = self.t_sin_avanzar = None

        if time.time() - self.t_guardado >= GUARDAR_CADA_S:
            self.t_guardado = time.time()
            con.execute(
                "INSERT INTO lecturas (momento, modo, bateria, voltaje, corriente_ma, fuga, cx, cy) VALUES (?,?,?,?,?,?,?,?)",
                (db.ahora(), self.modo, bat, round(mv / 1000, 2), ma, fuga, cx, cy),
            )
        return True

    def revisar_bucles(self, con, tof, piso):
        """Casos en que la navegación básica nunca saldría sola (ver navegacion.py)."""
        if all(d == 0 for d in tof):
            self.pausar(con, "Los 3 ToF marcan 0 (fallan): pausa para no girar sin fin")
            return
        if piso:
            if self.t_sin_piso is None:
                self.t_sin_piso = time.time()
            elif time.time() - self.t_sin_piso > SIN_PISO_MAX_S:
                self.pausar(con, f"El robot no ve el piso desde hace {SIN_PISO_MAX_S:.0f} s "
                                 "(¿lo levantaron o está en un borde?): pausa")
        else:
            self.t_sin_piso = None

    def sin_avanzar(self, con, vI, vD):
        """Si la navegación lleva SIN_AVANZAR_MAX_S sin pedir avanzar (gira en su lugar o
        retrocede), el robot está atorado: pausa con evento en vez de girar para siempre."""
        if vI > 0 and vD > 0:
            self.t_sin_avanzar = None
        elif self.t_sin_avanzar is None:
            self.t_sin_avanzar = time.time()
        elif time.time() - self.t_sin_avanzar > SIN_AVANZAR_MAX_S:
            self.pausar(con, f"La navegación lleva {SIN_AVANZAR_MAX_S:.0f} s sin poder avanzar "
                             "(¿atorado o un ToF fallando?): pausa")

    def odometria(self, encI, encD, yaw):
        """Suma lo que avanzó desde la trama anterior. Regresa False si hubo un salto
        imposible (reinicio del ESP32 o dato corrupto): ahí solo se toma la nueva referencia."""
        if self.enc is None:
            self.enc = (encI, encD)
            return True
        dI, dD = encI - self.enc[0], encD - self.enc[1]
        self.enc = (encI, encD)
        if abs(dI) > SALTO_ENCODER_MAX or abs(dD) > SALTO_ENCODER_MAX:
            return False
        metros = (dI + dD) / 2 / CUENTAS_POR_VUELTA * math.pi * DIAMETRO_RUEDA_M
        # yaw de la IMU: 0° = hacia el frente del área (arriba del mapa)
        self.x += metros * math.sin(math.radians(yaw))
        self.y -= metros * math.cos(math.radians(yaw))
        return True


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


SILENCIO_PARO_S = 0.5          # sin tramas válidas tanto tiempo mientras se mueve = X (D17 punto 3)
SILENCIO_REABRIR_S = 5         # sin tramas válidas tanto tiempo = se cierra y se reabre el puerto


def robot_real(puerto):
    """Lee el ESP32 por USB. Si se desconecta, no se cae: espera 2 s y vuelve a intentar.

    Dos fallas distintas, con su propio mensaje y evento:
    - USB desconectado (SerialException/OSError): se cierra el puerto y se busca otra vez.
    - El ESP32 dejó de mandar tramas válidas (se colgó, o el puerto quedó a otra velocidad y
      solo llega basura): a los 0.5 s, paro y pausa; a los 5 s se reabre el puerto, y se
      sigue reabriendo cada 5 s hasta que vuelvan las tramas. El evento se anota UNA vez
      por cada falla, no en cada reintento, para no llenar el historial."""
    import sqlite3
    import serial  # pip install pyserial

    conexion = {"esp": None}       # el puerto abierto, o None si no hay ESP32

    def enviar(linea):
        if conexion["esp"] is not None:
            conexion["esp"].write((linea + "\n").encode())

    def cerrar():
        try:
            conexion["esp"].close()
        except Exception:
            pass
        conexion["esp"] = None

    puente = Puente(enviar)
    paro_por_silencio = False      # para mandar la X una sola vez por cada pérdida de datos
    ya_conectado_antes = False
    reabriendo = False             # True mientras se reabre el puerto por falta de tramas
    ultima_trama_ok = time.time()
    ultimo_error = None            # para no repetir el mismo error en el log

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
                    # write_timeout: si algo trabara la escritura, que falle en vez de colgar el puente
                    esp = serial.Serial(ruta, 115200, timeout=0.1, write_timeout=1)
                    esp.baudrate = 9600
                    esp.baudrate = 115200
                    esp.reset_input_buffer()
                    conexion["esp"] = esp
            except (serial.SerialException, OSError):
                conexion["esp"] = None
            if conexion["esp"] is None:
                time.sleep(2)
                continue
            print(f"Conectado al ESP32 en {ruta}", flush=True)
            if ya_conectado_antes and not reabriendo:
                with db.conectar() as con:
                    db.evento(con, "sistema", "El USB del ESP32 volvió a conectarse")
            ya_conectado_antes = True
            ultima_trama_ok = time.time()

        # 2. Con ESP32: comandos de la app, trama del robot y seguridad
        try:
            with db.conectar() as con:
                puente.atender_comandos(con)
                linea = conexion["esp"].readline().decode(errors="ignore")
                valida = False
                if linea:
                    try:
                        valida = puente.procesar(linea, con)
                    except Exception as e:           # un error del código no debe tumbar el puente
                        texto = f"{type(e).__name__}: {e}"
                        if texto != ultimo_error:
                            print("Error al procesar la trama:", texto, "·", linea.strip()[:80], flush=True)
                            ultimo_error = texto
                # Solo una trama válida cuenta como «el robot está vivo»: la basura
                # (puerto a otra velocidad, ESP32 colgado) no debe evitar el paro por silencio
                if valida:
                    ultima_trama_ok = time.time()
                    paro_por_silencio = False
                    if reabriendo:
                        reabriendo = False
                        print("El ESP32 volvió a mandar tramas.", flush=True)
                        db.evento(con, "sistema", "El ESP32 volvió a mandar tramas")
                silencio = time.time() - ultima_trama_ok
                # Seguridad: 0.5 s sin tramas mientras se mueve = paro (una vez) y pausa
                if silencio > SILENCIO_PARO_S and not paro_por_silencio and puente.modo in MOVIENDO:
                    paro_por_silencio = True
                    puente.pausar(con, "El robot dejó de mandar datos: paro (X) y pausa")
            # Vigilante: 5 s sin una trama válida → se cierra y se vuelve a abrir el puerto
            if silencio > SILENCIO_REABRIR_S:
                print(f"{SILENCIO_REABRIR_S} s sin tramas válidas del ESP32: reabro el puerto.", flush=True)
                if not reabriendo:
                    reabriendo = True
                    with db.conectar() as con:
                        db.evento(con, "sistema", "El ESP32 dejó de mandar tramas: reabro el puerto")
                cerrar()
        except (serial.SerialException, OSError):
            print("Se perdió el USB del ESP32. Vuelvo a intentar en 2 s.", flush=True)
            cerrar()
            reabriendo = False
            with db.conectar() as con:
                db.evento(con, "sistema", "Se desconectó el USB del ESP32")
                if puente.modo in MOVIENDO:
                    puente.pausar(con, None, mandar_x=False)   # sin USB no hay a quién mandar la X
            time.sleep(2)
        except sqlite3.Error as e:
            # Base ocupada más de 10 s u otro error de SQLite: se avisa y se sigue.
            # El ESP32 frena solo si no le llega M en 0.5 s.
            print("Error de la base de datos:", e, flush=True)
            time.sleep(0.5)


if __name__ == "__main__":
    main()
