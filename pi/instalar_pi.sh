#!/bin/bash
# Instala todo lo de SweepSense en la Raspberry Pi «roomba».
# Se puede correr varias veces: lo que ya está hecho no se rompe ni se repite.
#
# Hace: Mosquitto (como la guía de la bóveda), grupos dialout y plugdev, clona o
# actualiza el fork, entorno de Python (.venv), servidor/.env y los servicios systemd.
#
# Uso, en la Pi, con tu usuario normal (NO con sudo):
#   bash ~/sweepsense/pi/instalar_pi.sh
# Desde la Mac (te pide las contraseñas en tu terminal):
#   ssh -t USUARIO@roomba.local bash sweepsense/pi/instalar_pi.sh
#
# Las contraseñas se escriben en la terminal y nunca quedan en este archivo.
# La red propia de la Pi va aparte (red_propia.sh): este script no toca el Wi-Fi.

set -e

# Todo va dentro de una función: el paso 4 puede actualizar este mismo archivo con
# git pull, y así bash ya lo leyó completo antes de empezar.
instalar() {

USUARIO=$(whoami)
CARPETA="$HOME/sweepsense"
REPO="https://github.com/a01613631-collab/sweepsense.git"
RAMA="adaptacion-plan"

if [ "$(id -u)" -eq 0 ]; then
  echo "Corre este script con tu usuario normal, sin sudo (él pide sudo cuando lo necesita)."
  exit 1
fi

# Con la red propia (red_propia.sh) la Pi no tiene internet: el script se puede correr
# igual para reinstalar servicios o .env; se saltan apt, git pull y pip si ya están.
if timeout 5 getent hosts github.com > /dev/null 2>&1; then
  HAY_INTERNET=1
else
  HAY_INTERNET=0
  echo "Sin internet (¿red Roomba puesta?): me salto apt, git pull y pip si ya están instalados."
fi

echo "== 1. Paquetes del sistema"
PAQUETES="mosquitto mosquitto-clients git python3-venv python3-pip"
if [ "$HAY_INTERNET" = 1 ]; then
  sudo apt-get update
  sudo apt-get install -y $PAQUETES
elif ! dpkg -s $PAQUETES > /dev/null 2>&1; then
  echo "Faltan paquetes ($PAQUETES) y no hay internet. Conecta la Pi a internet y vuelve a correrlo."
  exit 1
fi

echo "== 2. Mosquitto (pasos 1 a 4 de la Guía de Mosquitto)"
# listener 1883: sin esta línea Mosquitto 2.x solo acepta conexiones de la propia Pi
sudo tee /etc/mosquitto/conf.d/roomba.conf > /dev/null <<'FIN'
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
FIN
if sudo test -f /etc/mosquitto/passwd && sudo grep -q '^roomba:' /etc/mosquitto/passwd; then
  echo "El usuario roomba ya existe en el broker: no se toca su contraseña."
else
  echo "Escribe la contraseña del broker para el usuario roomba (te la pide dos veces):"
  if sudo test -f /etc/mosquitto/passwd; then
    sudo mosquitto_passwd /etc/mosquitto/passwd roomba        # sin -c: no borra otros usuarios
  else
    sudo mosquitto_passwd -c /etc/mosquitto/passwd roomba
  fi
fi
# Mosquitto corre como el usuario «mosquitto»: si el archivo es de root, no arranca
sudo chown mosquitto:mosquitto /etc/mosquitto/passwd
sudo chmod 640 /etc/mosquitto/passwd
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto

echo "== 3. Grupos: dialout (puerto serie del ESP32) y plugdev (Coral)"
sudo usermod -aG dialout,plugdev "$USUARIO"

# ModemManager abre cada puerto USB serie nuevo para ver si es un módem: le manda
# basura al ESP32 y le cambia la velocidad al puerto que ya tenía abierto el puente
# (probado el 2026-11-25: el puente dejó de leer tramas hasta reiniciarlo).
# mask además de disable: NetworkManager lo puede volver a arrancar por D-Bus.
sudo systemctl disable --now ModemManager 2>/dev/null || true
sudo systemctl mask ModemManager 2>/dev/null || true

echo "== 4. Código: el fork en $CARPETA (rama $RAMA)"
if [ -d "$CARPETA/.git" ]; then
  if [ "$HAY_INTERNET" = 1 ]; then
    git -C "$CARPETA" fetch origin
    git -C "$CARPETA" checkout "$RAMA"
    git -C "$CARPETA" pull --ff-only origin "$RAMA"
  else
    echo "Sin internet: se queda el código que ya está."
  fi
elif [ "$HAY_INTERNET" = 1 ]; then
  git clone -b "$RAMA" "$REPO" "$CARPETA"
else
  echo "No está $CARPETA y no hay internet para clonarlo."
  exit 1
fi

echo "== 5. Entorno de Python (.venv)"
# En Bookworm/Trixie «pip install» fuera de un entorno falla (externally-managed-environment).
# --system-site-packages: dentro del .venv se ven picamera2 y otros paquetes del sistema.
cd "$CARPETA/servidor"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv --system-site-packages .venv
fi
if [ "$HAY_INTERNET" = 1 ]; then
  .venv/bin/pip install -r requirements.txt
elif .venv/bin/python -c "import flask, serial, paho.mqtt" 2>/dev/null; then
  echo "Sin internet: las bibliotecas ya están en el .venv."
else
  echo "Faltan bibliotecas en el .venv y no hay internet. Conecta la Pi a internet y vuelve a correrlo."
  exit 1
fi

echo "== 6. servidor/.env (contraseña del broker para el servidor)"
if [ ! -f .env ]; then
  cp .env.ejemplo .env
  read -r -s -p "Contraseña del broker (usuario roomba; no se ve al escribir): " CLAVE
  echo
  # Se escribe con Python para que cualquier caracter de la contraseña quede tal cual
  CLAVE="$CLAVE" python3 - <<'FIN'
import os
lineas = open(".env", encoding="utf-8").read().splitlines()
lineas = ["MQTT_CLAVE=" + os.environ["CLAVE"] if l.startswith("MQTT_CLAVE=") else l for l in lineas]
open(".env", "w", encoding="utf-8").write("\n".join(lineas) + "\n")
FIN
  unset CLAVE
  chmod 600 .env
else
  echo ".env ya existe: no se toca."
fi

echo "== 7. Servicios (arrancan solos al prender la Pi)"
for archivo in "$CARPETA"/pi/systemd/*.service; do
  nombre=$(basename "$archivo")
  sed -e "s|@USUARIO@|$USUARIO|g" -e "s|@CARPETA@|$CARPETA|g" "$archivo" \
    | sudo tee "/etc/systemd/system/$nombre" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable roomba-api roomba-puente roomba-estacion
sudo systemctl restart roomba-api roomba-puente roomba-estacion
# La visión con el Coral es bonus (D12): se instala pero no se prende sola
sudo systemctl disable roomba-vision 2>/dev/null || true

echo
echo "== Listo"
echo "IP de la Pi: $(hostname -I)"
for s in mosquitto roomba-api roomba-puente roomba-estacion roomba-vision; do
  printf "  %-16s %s\n" "$s" "$(systemctl is-active $s 2>/dev/null || true)"
done
echo
echo "Para ver los mensajes MQTT (cambia TU_CLAVE):"
echo "  mosquitto_sub -h localhost -u roomba -P TU_CLAVE -t 'roomba/#' -v"
echo "Para ver el log de un servicio:  journalctl -u roomba-puente -f"
echo "Si es la primera vez, sal y vuelve a entrar por SSH para que cuenten los grupos nuevos."
}

instalar
exit
