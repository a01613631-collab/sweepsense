"""Funciones compartidas para usar la base de datos SQLite de SweepSense."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

RUTA_DB = os.environ.get("SWEEPSENSE_DB", os.path.join(os.path.dirname(__file__), "sweepsense.db"))
ESQUEMA = os.path.join(os.path.dirname(__file__), "esquema.sql")

# Área de prueba: 3 x 3 m en celdas de 30 cm
ANCHO, ALTO, CELDA_M = 10, 10, 0.3
BASE = (4, 9)


def ahora():
    return datetime.now().isoformat(timespec="seconds")


# Una orden de la app más vieja que esto ya no se manda al robot (ver comandos_pendientes)
EDAD_MAX_COMANDO_S = 10


@contextmanager
def conectar():
    """Uso:  with db.conectar() as con: ...
    Al salir guarda los cambios (o los deshace si hubo un error) y CIERRA la conexión.
    sqlite3 solo, con «with», guarda pero no cierra: el puente abre una conexión en cada
    vuelta (10 por segundo) y se quedarían abiertas hasta que Python las recoja."""
    # timeout=10: con varios procesos (API, puente, MQTT, visión) escribiendo, espera
    # hasta 10 s a que se libere la base en lugar de fallar con «database is locked»
    con = sqlite3.connect(RUTA_DB, timeout=10)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")  # permite que la API lea mientras el puente escribe
        with con:
            yield con
    finally:
        con.close()


def crear():
    """Crea las tablas y la cuadrícula del área si no existen."""
    with conectar() as con, open(ESQUEMA, encoding="utf-8") as f:
        con.executescript(f.read())
        con.executemany(
            "INSERT OR IGNORE INTO celdas_en_mapa (cx, cy) VALUES (?, ?)",
            [(x, y) for y in range(ALTO) for x in range(ANCHO)],
        )


def evento(con, tipo, detalle, id_sesion=None):
    if id_sesion is None:
        id_sesion = sesion_activa(con)
    con.execute(
        "INSERT INTO eventos (id_sesion, momento, tipo, detalle) VALUES (?, ?, ?, ?)",
        (id_sesion, ahora(), tipo, detalle),
    )


def sesion_activa(con):
    fila = con.execute("SELECT id_sesion FROM sesiones WHERE hora_fin IS NULL ORDER BY id_sesion DESC LIMIT 1").fetchone()
    return fila["id_sesion"] if fila else None


def ultima_lectura(con):
    return con.execute("SELECT * FROM lecturas ORDER BY id DESC LIMIT 1").fetchone()


def marcar_celda(con, cx, cy, id_sesion=None):
    """La navegación llama esto cada vez que el robot pasa por una celda."""
    id_sesion = id_sesion or sesion_activa(con)
    if id_sesion is None:
        return
    celda = con.execute("SELECT id_celda FROM celdas_en_mapa WHERE cx=? AND cy=?", (cx, cy)).fetchone()
    if celda:
        con.execute(
            "INSERT OR IGNORE INTO sesion_celda (id_sesion, id_celda, momento) VALUES (?, ?, ?)",
            (id_sesion, celda["id_celda"], ahora()),
        )


def marcar_obstaculo(con, cx, cy):
    con.execute("UPDATE celdas_en_mapa SET es_obstaculo = 1 WHERE cx=? AND cy=?", (cx, cy))


def cerrar_sesion(con, bateria=None):
    """Termina la limpieza en curso (la usan la API y el puente)."""
    activa = sesion_activa(con)
    if not activa:
        return None
    if bateria is None:
        l = ultima_lectura(con)
        bateria = l["bateria"] if l else None
    con.execute("UPDATE sesiones SET hora_fin = ?, bateria_final = ? WHERE id_sesion = ?", (ahora(), bateria, activa))
    con.execute("UPDATE tareas SET estado = 'completada' WHERE id_sesion = ?", (activa,))
    evento(con, "fin", "Limpieza terminada", activa)
    return activa


def obstaculos(con):
    return {(f["cx"], f["cy"]) for f in con.execute("SELECT cx, cy FROM celdas_en_mapa WHERE es_obstaculo = 1")}


def comandos_pendientes(con, edad_max_s=EDAD_MAX_COMANDO_S):
    """Órdenes por mandar al robot, de la más vieja a la más nueva.

    Las que llevan más de edad_max_s esperando se descartan: se marcan atendidas y se anota
    un evento. Por qué: si alguien aprieta «Iniciar» con el USB desconectado (o con el
    puente caído), la orden no debe ejecutarse sola minutos después, al reconectar."""
    filas = con.execute("SELECT * FROM comandos WHERE atendido = 0 ORDER BY id").fetchall()
    frescos = []
    for f in filas:
        try:
            edad = (datetime.now() - datetime.fromisoformat(f["momento"])).total_seconds()
        except (TypeError, ValueError):
            edad = edad_max_s + 1                    # momento ilegible: se trata como vieja
        if edad > edad_max_s:
            descartar_comando(con, f, edad)
        else:
            frescos.append(dict(f, parametros=json.loads(f["parametros"] or "{}")))
    return frescos


def descartar_comando(con, fila, edad):
    comando_atendido(con, fila["id"])
    evento(con, "sistema", f"Orden «{fila['accion']}» descartada: llevaba {int(edad)} s esperando "
                           "(el robot no estaba conectado)")
    if fila["accion"] != "iniciar":
        return
    # La API ya abrió la sesión de ese «iniciar». Si no llegó otro «iniciar» después,
    # esa sesión nunca empezó: se cierra (sin evento «fin») y su tarea vuelve a pendiente.
    otro = con.execute("SELECT 1 FROM comandos WHERE accion = 'iniciar' AND id > ?", (fila["id"],)).fetchone()
    activa = sesion_activa(con)
    if activa and not otro:
        con.execute("UPDATE sesiones SET hora_fin = ? WHERE id_sesion = ?", (ahora(), activa))
        con.execute("UPDATE tareas SET estado = 'pendiente' WHERE id_sesion = ? AND estado = 'en_proceso'", (activa,))


def comando_atendido(con, id_comando):
    con.execute("UPDATE comandos SET atendido = 1 WHERE id = ?", (id_comando,))
