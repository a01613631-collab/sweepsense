"""Punto de conexión con la navegación (responsable: Adrián).

El puente llama a estos métodos. Esta versión es básica: planea una ruta en zigzag
por la zona (saltando obstáculos conocidos) y, en el robot real, avanza y gira cuando
ve un obstáculo. Adrián puede reemplazar el contenido manteniendo los mismos métodos.
"""
import db

# Velocidades en la escala de M,vI,vD: -1023 a 1023 (PWM del TB6612). Son las de Carlos,
# sin medir en el robot: se ajustan en las pruebas de piso. El puente no las manda de
# golpe: les aplica una rampa (RAMPA_POR_TRAMA en puente_robot.py).
VEL = 600          # avanzar en línea recta
GIRO = 450         # girar en su lugar y retroceder


class Navegacion:
    def __init__(self):
        self.ruta = None
        self.zona = "completa"

    def iniciar(self, zona):
        self.zona = zona
        with db.conectar() as con:
            obst = db.obstaculos(con)
        x0, x1 = {"izquierda": (0, 4), "derecha": (5, 9)}.get(zona, (0, 9))
        ruta = []
        for y in range(db.ALTO):
            xs = list(range(x0, x1 + 1))
            if y % 2:
                xs.reverse()
            ruta += [(x, y) for x in xs if (x, y) not in obst]
        self.ruta = ruta

    def detener(self):
        self.ruta = None

    def regresar(self):
        self.ruta = None

    def termino(self, cx, cy):
        """True cuando ya se recorrió toda la zona."""
        return bool(self.ruta) and (cx, cy) == self.ruta[-1]

    def paso(self, cx, cy, yaw, tof, piso, bump, ir):
        """Devuelve (vel_izq, vel_der) para este ciclo.

        Casos en que esta versión básica se quedaría en un bucle, y quién los corta
        (en puente_robot.py, sin cambiar este método):
          - piso ≠ 0 sostenido (lo levantaron: ruedas al aire) → retrocede sin fin.
            El puente pausa a los SIN_PISO_MAX_S.
          - un ToF en 0 (el ESP32 manda 0 cuando el sensor falla) → min(tof) < 200 y gira
            en su lugar sin fin. El puente pausa si los 3 están en 0, o si lleva
            SIN_AVANZAR_MAX_S sin pedir avanzar.
        """
        if piso or bump:
            return (-GIRO, -GIRO)            # retrocede (el ESP32 ya frenó por seguridad)
        if min(tof) < 200:
            return (GIRO, -GIRO)             # gira a la derecha en su lugar
        return (VEL, VEL)
