"""Reconocimiento de objetos con el Coral USB y la cámara (bonus, D12).

No toca la navegación ni los motores: si algo del Coral falla, el robot sigue igual.

- Toma una foto cada 0.5 s, la reduce a 300 × 300 y la pasa por el Coral.
- Lo que detecte con puntaje > 0.5 lo publica en roomba/pi/objeto (no retenido),
  por ejemplo «persona 0.82», y lo anota como evento «objeto» (máximo 1 por objeto
  cada 10 s, para no llenar el historial).
- Si no hay cámara o Coral, lo escribe en el log y espera 30 s antes de reintentar.

Necesita (lo instala pi/instalar_coral.sh): libedgetpu1-std, tflite-runtime (o
ai-edge-litert), python3-picamera2, pillow y el modelo en ~/sweepsense/modelos/.

Ejecutar:
  .venv/bin/python vision_coral.py --prueba   # 1 foto, imprime lo que ve y la guarda con recuadros
  .venv/bin/python vision_coral.py            # sin parar (es lo que corre el servicio roomba-vision)
"""
import argparse
import os
import time

import db

CARPETA = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(CARPETA)                      # ~/sweepsense
MODELO = os.path.join(RAIZ, "modelos", "ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite")
ETIQUETAS = os.path.join(RAIZ, "modelos", "coco_labels.txt")
FOTO_PRUEBA = os.path.join(RAIZ, "fotos", "prueba.jpg")

PUNTAJE_MIN = 0.5        # detecciones con menos puntaje se ignoran
CADA_S = 0.5             # 2 fotos por segundo: poco CPU y RAM (la Pi tiene 2 GB)
EVENTO_CADA_S = 10       # máximo un evento por objeto cada 10 s
ESPERA_ERROR_S = 30      # sin cámara o sin Coral: reintentar cada 30 s
T_OBJETO = "roomba/pi/objeto"

# Nombres en español de algunos objetos de COCO; los demás se quedan en inglés
EN_ESPANOL = {
    "person": "persona", "cat": "gato", "dog": "perro", "cup": "taza", "bottle": "botella",
    "chair": "silla", "couch": "sillon", "bed": "cama", "dining table": "mesa", "potted plant": "planta",
    "tv": "tele", "laptop": "laptop", "cell phone": "celular", "book": "libro", "backpack": "mochila",
    "shoe": "zapato", "sports ball": "pelota", "teddy bear": "peluche", "remote": "control",
    "bowl": "plato hondo", "toilet": "inodoro", "sink": "lavabo", "refrigerator": "refri",
}


def cargar_etiquetas():
    """coco_labels.txt: una etiqueta por renglón; el número de renglón es la clase."""
    with open(ETIQUETAS, encoding="utf-8") as f:
        return [EN_ESPANOL.get(l.strip(), l.strip()) for l in f]


def cargar_interprete():
    """Carga el modelo en el Coral. Lanza un error si falta el Coral o su runtime."""
    try:
        from tflite_runtime.interpreter import Interpreter, load_delegate
    except ImportError:
        from ai_edge_litert.interpreter import Interpreter, load_delegate
    delegado = load_delegate("libedgetpu.so.1")      # falla si no hay Coral conectado
    interprete = Interpreter(model_path=MODELO, experimental_delegates=[delegado])
    interprete.allocate_tensors()
    return interprete


def abrir_camara():
    from picamera2 import Picamera2
    camara = Picamera2()
    # 640 × 480 y luego se reduce a 300 × 300 (el ISP no siempre acepta 300 × 300 exacto)
    camara.configure(camara.create_preview_configuration(main={"size": (640, 480), "format": "RGB888"}))
    camara.start()
    time.sleep(1)                                    # que ajuste la exposición
    return camara


def tomar_foto(camara):
    """Regresa la foto como imagen PIL en RGB."""
    from PIL import Image
    arreglo = camara.capture_array()
    # Ojo: en picamera2, el formato "RGB888" guarda los colores en orden B, G, R
    return Image.fromarray(arreglo[:, :, ::-1])


def detectar(interprete, foto, etiquetas):
    """Pasa la foto por el modelo. Regresa (lista de (nombre, puntaje, recuadro), ms de inferencia).
    El recuadro va de 0 a 1: (arriba, izquierda, abajo, derecha)."""
    import numpy as np
    entrada = interprete.get_input_details()[0]
    alto, ancho = entrada["shape"][1], entrada["shape"][2]   # 300 × 300
    datos = np.asarray(foto.resize((ancho, alto)), dtype=np.uint8)[np.newaxis, ...]
    interprete.set_tensor(entrada["index"], datos)
    inicio = time.perf_counter()
    interprete.invoke()
    ms = (time.perf_counter() - inicio) * 1000
    # Salidas del modelo SSD con postproceso: recuadros, clases, puntajes y cuántos
    salidas = interprete.get_output_details()
    recuadros = interprete.get_tensor(salidas[0]["index"])[0]
    clases = interprete.get_tensor(salidas[1]["index"])[0]
    puntajes = interprete.get_tensor(salidas[2]["index"])[0]
    cuantos = int(interprete.get_tensor(salidas[3]["index"])[0])
    return armar_detecciones(recuadros, clases, puntajes, cuantos, etiquetas), ms


def armar_detecciones(recuadros, clases, puntajes, cuantos, etiquetas):
    """Junta las salidas del modelo en una lista ordenada del más seguro al menos seguro."""
    detecciones = []
    for i in range(cuantos):
        if puntajes[i] > PUNTAJE_MIN:
            clase = int(clases[i])
            nombre = etiquetas[clase] if 0 <= clase < len(etiquetas) else f"clase{clase}"
            detecciones.append((nombre, float(puntajes[i]), tuple(float(v) for v in recuadros[i])))
    detecciones.sort(key=lambda d: d[1], reverse=True)
    return detecciones


def prueba():
    """Una foto: imprime lo que ve y la guarda con los recuadros en ~/sweepsense/fotos/prueba.jpg."""
    from PIL import ImageDraw
    etiquetas = cargar_etiquetas()
    try:
        interprete = cargar_interprete()
    except (ValueError, OSError) as e:
        print("No se pudo cargar el delegado del Coral (libedgetpu.so.1):", e)
        print("Revisa: ¿está conectado en un puerto azul? ¿corriste pi/instalar_coral.sh y lo reconectaste?")
        raise SystemExit(1)
    camara = abrir_camara()
    foto = tomar_foto(camara)
    camara.close()
    # La 1.ª inferencia también sube el modelo al Coral y tarda más: se mide la 2.ª
    detectar(interprete, foto, etiquetas)
    detecciones, ms = detectar(interprete, foto, etiquetas)
    print(f"Inferencia: {ms:.1f} ms")
    if not detecciones:
        print(f"No vi nada con puntaje mayor a {PUNTAJE_MIN}.")
    dibujo = ImageDraw.Draw(foto)
    w, h = foto.size
    for nombre, puntaje, (arriba, izq, abajo, der) in detecciones:
        print(f"  {nombre} {puntaje:.2f}")
        dibujo.rectangle([izq * w, arriba * h, der * w, abajo * h], outline=(255, 0, 0), width=3)
        dibujo.text((izq * w + 4, arriba * h + 2), f"{nombre} {puntaje:.2f}", fill=(255, 0, 0))
    os.makedirs(os.path.dirname(FOTO_PRUEBA), exist_ok=True)
    foto.save(FOTO_PRUEBA)
    print("Foto guardada en", FOTO_PRUEBA)


def conectar_mqtt():
    import paho.mqtt.client as mqtt
    from mqtt_estacion import leer_env              # lee servidor/.env
    leer_env()
    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="roomba-vision")
    cliente.username_pw_set(os.environ.get("MQTT_USUARIO", "roomba"), os.environ.get("MQTT_CLAVE", ""))
    cliente.connect_async(os.environ.get("MQTT_HOST", "localhost"), 1883)
    cliente.loop_start()                            # reconecta solo si el broker se reinicia
    return cliente


def sin_parar():
    """Modo servicio: foto cada 0.5 s, publica lo que ve y anota eventos."""
    db.crear()
    etiquetas = cargar_etiquetas()
    cliente = conectar_mqtt()
    ultimo_evento = {}                               # objeto -> cuándo se anotó por última vez
    ultimo_error = None
    while True:
        camara = None
        try:
            interprete = cargar_interprete()
            camara = abrir_camara()
            print("Visión lista: Coral y cámara funcionando", flush=True)
            ultimo_error = None
            while True:
                inicio = time.time()
                detecciones, _ = detectar(interprete, tomar_foto(camara), etiquetas)
                for nombre, puntaje, _ in detecciones:
                    cliente.publish(T_OBJETO, f"{nombre} {puntaje:.2f}")
                    if time.time() - ultimo_evento.get(nombre, 0) >= EVENTO_CADA_S:
                        ultimo_evento[nombre] = time.time()
                        with db.conectar() as con:
                            db.evento(con, "objeto", f"Objeto visto: {nombre} ({puntaje:.0%})")
                time.sleep(max(0, CADA_S - (time.time() - inicio)))
        except Exception as e:                       # sin Coral, sin cámara, cable flojo...
            texto = f"{type(e).__name__}: {e}"
            if texto != ultimo_error:                # el mismo error no se repite en el log
                print("Visión detenida:", texto, f"· reintento cada {ESPERA_ERROR_S} s", flush=True)
                ultimo_error = texto
            if camara is not None:
                try:
                    camara.close()
                except Exception:
                    pass
            time.sleep(ESPERA_ERROR_S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prueba", action="store_true", help="1 foto, imprime detecciones y la guarda")
    args = ap.parse_args()
    if args.prueba:
        prueba()
    else:
        sin_parar()


if __name__ == "__main__":
    main()
