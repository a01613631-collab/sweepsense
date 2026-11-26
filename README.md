# SweepSense · App, API y base de datos

```
app/        App móvil y web (React Native + Expo)
servidor/   API HTTP, base de datos SQLite, puente con el ESP32 y MQTT de la estación (Raspberry Pi)
docs/       Diagrama de arquitectura para la presentación
```

## 1. Probar la app (sin robot)

Necesitas Node.js LTS en la compu y la app **Expo Go** en el celular.

```bash
cd app
npm install
npx expo start          # escanea el QR con Expo Go (celular y compu en la misma red)
npx expo start --web    # o ábrela como página web
```

La app arranca en **modo demo**: un robot simulado que limpia, choca con obstáculos y regresa a la base (sin cargar: la batería se carga aparte, decisión D18; escala 10.5–12.3 V). Sirve para presentar aunque el robot no esté listo. En **Ajustes** (engrane) hay un interruptor para simular una fuga de agua.

## 2. Servidor en la Raspberry Pi

Raspberry Pi OS **Bookworm de 64 bits** (Legacy). Todo se instala con un script que se puede correr varias veces:

```bash
git clone -b adaptacion-plan https://github.com/a01613631-collab/sweepsense.git ~/sweepsense
bash ~/sweepsense/pi/instalar_pi.sh
```

El script instala Mosquitto (usuario `roomba`; la contraseña te la pide en la terminal), te agrega a los grupos `dialout` y `plugdev`, crea el entorno de Python y deja tres servicios que arrancan solos al prender la Pi: `roomba-api` (puerto 5000), `roomba-puente` (ESP32 por USB, `--puerto auto`) y `roomba-estacion` (MQTT). Un cuarto, `roomba-vision` (Coral, bonus), queda instalado pero apagado. Ver su estado: `systemctl status roomba-puente`; su log: `journalctl -u roomba-puente -f`. El script también apaga y **enmascara ModemManager** (abría el puerto del ESP32 y el puente dejaba de leer tramas) y se puede volver a correr **sin internet** (con la red `Roomba` puesta): se salta apt, `git pull` y pip si ya está todo instalado.

**Entorno de Python:** en Bookworm, `pip install` fuera de un entorno falla con «externally-managed-environment». Por eso todo corre dentro de `servidor/.venv`, creado con `python3 -m venv --system-site-packages .venv` (así también se ven `picamera2` y otros paquetes del sistema):

```bash
cd servidor
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

`servidor/.env` lleva la contraseña del broker (el script lo crea a partir de `.env.ejemplo`). No se sube a GitHub.

Sin el robot armado se puede probar todo así (detén antes los servicios con `sudo systemctl stop roomba-puente`):

```bash
cd servidor
.venv/bin/python datos_prueba.py             # (una vez) limpiezas pasadas para el Historial
.venv/bin/python app.py
.venv/bin/python puente_robot.py --simular   # robot simulado en lugar del ESP32
```

La red propia de la Pi para el demo (decisión 15) está en `pi/red_propia.sh`; no se corre sola. La Pi emite la red «Roomba» con IP fija `10.42.0.1` (con `autoconnect-priority 100`, gana al Wi-Fi guardado al reiniciar): las ESP usan `MQTT_BROKER "10.42.0.1"` y la app `http://10.42.0.1:5000` (ya viene por defecto). Mientras la emite, la Pi no tiene internet por Wi-Fi.

En la app: **Ajustes → Robot real**, deja `http://10.42.0.1:5000` (o, en el hotspot de respaldo, `http://<IP de la Pi>:5000`, con la IP de `hostname -I`) y toca **Probar conexión**. Si la Pi no contesta en 5 s, la app lo dice («¿El celular está en la red Roomba?»).

## 3. Cómo viajan los datos

| Conexión | Protocolo | Detalle |
|---|---|---|
| ESP32 ↔ Pi | Serial USB | Trama `S,...` cada 200 ms (5 por segundo); la Pi responde `M,`, `C,`, `X`, `R` y `Z` (abajo) |
| Estación ESP8266 ↔ Pi | **MQTT** | `roomba/base/en_linea`, `roomba/base/cargando`, `roomba/base/corriente_ma` y `roomba/base/faro`; la Pi manda `roomba/base/faro/set` (`1`/`0`) y publica su modo en `roomba/pi/modo` |
| App ↔ Pi | **HTTP** | API REST en JSON (tabla de abajo) |
| Alexa → Pi | **HTTP** | `POST /api/comando` con `"origen": "alexa"` |

La app pide el estado cada 2 s y los comandos se guardan en la tabla `comandos`; el puente los lee y los manda al ESP32. Así la base de datos es el punto de comunicación entre dispositivos y de control en tiempo real.

Protecciones del puente (`servidor/puente_robot.py`):

- Una orden que lleva más de 10 s sin atenderse se descarta con un evento (`db.EDAD_MAX_COMANDO_S`); si era «iniciar», su sesión se cierra.
- Rampa de motores: cada rueda sube como máximo `RAMPA_POR_TRAMA` = 150 por trama (0 → 600 en 0.8 s); frenar es inmediato.
- Pausa con evento si los 3 ToF marcan 0, si no ve piso más de 2 s o si pasan 10 s sin poder avanzar.
- Reanudar vuelve al modo de antes de la pausa (`limpiando` o `regresando`).
- Si `t_ms` baja (el ESP32 se reinició), reinicia la odometría y pausa si se movía.
- Vigilante del puerto: 0.5 s sin tramas válidas mientras se mueve → `X`; 5 s → reabre el puerto (con evento).

### Protocolo ESP32 ↔ Pi (USB, 115200 baudios)

El ESP32 manda una línea cada 200 ms con 16 campos:

```
S,t_ms,encI,encD,yaw,tof0,tof1,tof2,piso,bump,ir,mV,mA,fuga,estado,alerta
```

- `piso`: número de 0 a 15, un bit por sensor TCRT5000 (1 = ve el borde).
- `ir`: 0 = no ve el faro, 1 = LED izquierdo, 2 = LED derecho.
- `estado`: `ok`, `paro` o `fuga`. `alerta`: texto sin comas, `ninguna` si no hay.

La Pi manda:

| Comando | Qué hace |
|---|---|
| `M,vI,vD` | Velocidad de cada rueda (−1023 a 1023). Si pasan 0.5 s sin `M`, el ESP32 frena. |
| `C,turbina,cepillo,bomba` | Turbina en % (0 o de 50 a 100) y bomba (0/1). El cepillo va con la turbina en el mismo MOSFET, así que el ESP32 ignora ese campo. |
| `X` | Paro. Se queda trabado hasta que llega `R`; el ESP32 avisa con `estado` = `paro` y la Pi pasa a `pausado`. |
| `R` | Quita el paro. La Pi lo manda al iniciar y al reanudar; al regresar («A la base») solo si el paro lo puso la propia Pi, no si llegó una `X` por MQTT. |
| `Z` | Pone el yaw en 0 (la Pi lo manda cuando el robot llega a la base). |

- `tof`: mm; 2000 = nada enfrente, 0 = el sensor falla.
- Las líneas que empiezan con `#` son diagnóstico del ESP32 y el puente las ignora.

La batería se calcula igual en la Pi y en el ESP32: 10.5 V = 0 % y 12.3 V = 100 % (`V_LLENA` en `puente_robot.py` y en el firmware; provisional hasta medir la batería recién cargada).

## 4. API

| Método | Ruta | Devuelve |
|---|---|---|
| GET | `/api/estado` | Modo, batería, voltaje, corriente, fuga, posición, sesión en curso y última sesión |
| GET | `/api/mapa` (`?sesion=id`) | Cuadrícula 10×10, celdas limpias, ruta, obstáculos, base y robot |
| GET | `/api/sesiones` | Limpiezas terminadas con sus celdas |
| GET | `/api/tareas` | Tareas pendientes, en proceso y completadas |
| GET | `/api/eventos?limite=20` | Obstáculos, fugas, batería baja, comandos… |
| GET | `/api/lecturas?minutos=30` | Lecturas para gráficas |
| POST | `/api/comando` | `{"accion": "iniciar" \| "pausar" \| "reanudar" \| "detener" \| "regresar", "zona", "tipo", "origen"}` |
| POST | `/api/tareas` | `{"zona", "tipo"}` |
| POST | `/api/estacion` | Alternativa HTTP para la estación: `{"v", "ma", "cargando", "rssi"}` |

`POST /api/comando` responde **409** con un mensaje si el robot no está en línea (sin lectura en 10 s) o si la orden no aplica al modo: `pausar` solo en `limpiando`/`regresando`, `reanudar` solo en `pausado`, `regresar` en `en_espera`, `limpiando` o `pausado`; `detener` siempre. En `/api/estado`, `cargando` solo cuenta con `CARGA_POR_CONTACTOS = True` (hoy `False`, decisión D18).

## 5. Base de datos

`servidor/esquema.sql` sigue el modelo entidad-relación del equipo (`sesiones`, `eventos`, `celdas_en_mapa` y la relación M:N `sesion_celda`) y agrega cuatro tablas: `lecturas`, `estacion`, `comandos` y `tareas`.

## 6. Indicadores de la app

Batería, voltaje y corriente, tiempo limpiando, cobertura (%), área limpiada (m²), obstáculos y sensor de fuga en tiempo real. En Historial: número de limpiezas, cobertura promedio, duración promedio y consumo de batería por minuto.

Si el robot no está en línea, los controles se desactivan y sale un aviso. **Detener** funciona también mientras regresa a la base («A la base» se desactiva en `regresando` y `cargando`). Los datos que la Pi no tiene (batería, voltaje, corriente) se muestran como «—».

## 7. Pendientes para integrar

- **Navegación (Adrián):** reemplazar `servidor/navegacion.py` manteniendo los métodos `iniciar`, `paso`, `termino`, `detener` y `regresar`.
- **Encoders:** medir `CUENTAS_POR_VUELTA` en `puente_robot.py` (10 vueltas a mano).
- **Estación:** el sketch `estacion_base.ino` del proyecto usa los temas `roomba/base/...` de arriba; en su `config.h` van la red `Roomba` y `MQTT_BROKER "10.42.0.1"`. Desde D18 ya no carga la batería (solo faro IR) y para la Expo quedó fuera del alcance.
- **Alexa (Javier):** que la skill mande `POST /api/comando`. Para eso la API debe ser accesible desde internet (túnel HTTPS con Cloudflare Tunnel o ngrok).
- **Firmware del ESP32 (José):** la navegación usa solo el USB. El ESP32 **sí** conserva el Wi-Fi para publicar en MQTT (`roomba/robot/...`) y recibir un paro desde la app MQTT, así que los pines ADC2 siguen sin usarse.
