#!/bin/bash
# Red propia de la Pi para el demo (opción B de la Guía de Mosquitto, decisión 15).
# La Pi emite la red «Roomba» y su IP queda fija (normalmente 10.42.0.1).
#
# OJO: mientras emite su red, la Pi NO tiene internet por Wi-Fi.
# Instala todo primero (instalar_pi.sh, instalar_coral.sh) con internet.
# Este script NO se corre solo: córrelo a mano cuando ya quieras la red del demo.
#
# Uso:   bash ~/sweepsense/pi/red_propia.sh
# Para volver a tu Wi-Fi normal:   sudo nmcli connection down RoombaAP
#
# Después, en los config.h del ESP32 y del ESP8266: WIFI_SSID "Roomba", su clave,
# y MQTT_BROKER = la IP que imprime al final.

set -e

if ! command -v nmcli > /dev/null; then
  echo "No está nmcli (NetworkManager). Usa la opción A: hotspot del celular."
  exit 1
fi

echo "Esto cambia el Wi-Fi de la Pi: si estás conectado por Wi-Fi, se va a cortar tu SSH."
read -r -p "¿Seguir? escribe si: " RESPUESTA
if [ "$RESPUESTA" != "si" ]; then
  echo "No se hizo nada."
  exit 0
fi

read -r -s -p "Clave para la red Roomba (8 caracteres o más; no se ve): " CLAVE
echo
if [ ${#CLAVE} -lt 8 ]; then
  echo "La clave debe tener 8 caracteres o más."
  exit 1
fi

# Si ya existía, se borra y se crea otra vez (así se puede correr varias veces)
sudo nmcli connection delete RoombaAP > /dev/null 2>&1 || true
sudo nmcli device wifi hotspot ifname wlan0 con-name RoombaAP ssid Roomba band bg password "$CLAVE"
sudo nmcli connection modify RoombaAP connection.autoconnect yes
unset CLAVE

echo "Red Roomba lista. IP de la Pi: $(hostname -I)"
