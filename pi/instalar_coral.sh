#!/bin/bash
# Instala el Coral USB y la cámara en la Raspberry Pi (bonus, D12).
# Se puede correr varias veces. Correr DESPUÉS de instalar_pi.sh (usa su .venv).
#
# Uso, en la Pi, con tu usuario normal:   bash ~/sweepsense/pi/instalar_coral.sh
#
# Qué combinación se usa (revisada el 2026-11-24):
#   - Runtime del Edge TPU: libedgetpu1-std (frecuencia estándar, NO la -max, que
#     calienta y consume más) del repositorio de la comunidad feranick/libedgetpu,
#     porque los paquetes de Google ya no se mantienen.
#   - Intérprete: tflite-runtime 2.14.0 con numpy < 2 (para Python 3.11 de Bookworm).
#     Si no se puede instalar, ai-edge-litert 2.0.2 (el sucesor oficial).
#   - Cámara: python3-picamera2 del sistema (el .venv lo ve por --system-site-packages).
#   - Modelo de prueba: SSD MobileNet v2 COCO compilado para el Edge TPU.

set -e

instalar() {

CARPETA="$HOME/sweepsense"
VENV="$CARPETA/servidor/.venv"
VERSION_TPU="16.0TF2.19.1-1"
VERSION_DEB="16.0tf2.19.1-1"
URL_TPU="https://github.com/feranick/libedgetpu/releases/download/$VERSION_TPU"
URL_MODELOS="https://raw.githubusercontent.com/google-coral/test_data/master"

if [ "$(id -u)" -eq 0 ]; then
  echo "Corre este script con tu usuario normal, sin sudo."
  exit 1
fi
if [ "$(uname -m)" != "aarch64" ]; then
  echo "La Pi no es de 64 bits ($(uname -m)). El Coral necesita Raspberry Pi OS de 64 bits."
  exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
  echo "Falta $VENV. Corre antes pi/instalar_pi.sh."
  exit 1
fi

. /etc/os-release
SISTEMA="$VERSION_CODENAME"            # bookworm o trixie
if [ "$SISTEMA" != "bookworm" ] && [ "$SISTEMA" != "trixie" ]; then
  echo "Sistema $SISTEMA: no hay paquete del Edge TPU para él. Se esperaba bookworm."
  exit 1
fi

echo "== 1. Runtime del Edge TPU (libedgetpu1-std $VERSION_DEB, $SISTEMA)"
INSTALADA=$(dpkg-query -W -f='${Version}' libedgetpu1-std 2>/dev/null || true)
if [ "$INSTALADA" = "$VERSION_DEB.$SISTEMA" ] || [ "$INSTALADA" = "$VERSION_DEB" ]; then
  echo "Ya está instalada ($INSTALADA)."
else
  # Quitar versiones viejas (y la -max, si alguien la puso)
  sudo apt-get remove -y libedgetpu1-std libedgetpu1-max 2>/dev/null || true
  DEB="libedgetpu1-std_${VERSION_DEB}.${SISTEMA}_arm64.deb"
  wget -q -O "/tmp/$DEB" "$URL_TPU/$DEB"
  # apt instala el .deb igual que dpkg -i, pero además trae lo que le falte
  sudo apt-get install -y "/tmp/$DEB"
  rm -f "/tmp/$DEB"
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  echo ">>> Desconecta el Coral y vuelve a conectarlo (puerto USB 3, azul) para que cuente la regla de udev."
fi

echo "== 2. Intérprete de TensorFlow Lite en el .venv"
if "$VENV/bin/python" -c "import tflite_runtime.interpreter" 2>/dev/null; then
  echo "tflite-runtime ya está."
elif "$VENV/bin/python" -c "import ai_edge_litert.interpreter" 2>/dev/null; then
  echo "ai-edge-litert ya está."
elif "$VENV/bin/pip" install "tflite-runtime==2.14.0" "numpy<2"; then
  echo "Quedó tflite-runtime 2.14.0."
else
  echo "tflite-runtime no se pudo instalar: pruebo ai-edge-litert 2.0.2."
  "$VENV/bin/pip" install "ai-edge-litert==2.0.2" "numpy<2"
fi
"$VENV/bin/pip" install pillow    # para dibujar los recuadros en la foto de prueba

echo "== 3. Cámara"
sudo apt-get install -y python3-picamera2 --no-install-recommends
if command -v rpicam-hello > /dev/null; then
  rpicam-hello --list-cameras || true
elif command -v libcamera-hello > /dev/null; then
  libcamera-hello --list-cameras || true
fi

echo "== 4. Modelo de prueba (no se sube al repositorio)"
mkdir -p "$CARPETA/modelos" "$CARPETA/fotos"
for archivo in ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite coco_labels.txt; do
  if [ ! -s "$CARPETA/modelos/$archivo" ]; then
    wget -q -O "$CARPETA/modelos/$archivo" "$URL_MODELOS/$archivo"
  fi
done
ls -l "$CARPETA/modelos"

echo
echo "== Listo"
echo "Coral en el USB (antes de usarlo: 1a6e:089a Global Unichip; después: 18d1:9302 Google):"
lsusb | grep -Ei "1a6e:089a|18d1:9302" || echo "  No veo el Coral. ¿Está conectado en un puerto azul?"
echo
echo "Prueba:  cd $CARPETA/servidor && .venv/bin/python vision_coral.py --prueba"
echo "Servicio (bonus, apagado por defecto):  sudo systemctl enable --now roomba-vision"

}

instalar
exit
