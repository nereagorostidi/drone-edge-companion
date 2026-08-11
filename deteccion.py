#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 Detección de personas (YOLO) — dominio DETECCION
 Sistema SAR basado en dron — Raspberry Pi 5 (nodo edge)
=====================================================================

Este script ejecuta la detección de personas con YOLO sobre un vídeo o
sobre una cámara en vivo (igual que antes: genera el vídeo anotado de
salida y la ventana de vista previa) y, ADEMÁS, cuando localiza una
persona publica una alerta en sar/{dron_id}/deteccion con la misma
lógica que el resto de colectores (buffer local SQLite + reenvío MQTT
"store-and-forward").

La fuente es un fichero de vídeo (argumento posicional) O una cámara
conectada (--camera), nunca las dos a la vez. En la Raspberry Pi, con
la cámara accesible como dispositivo V4L2 (/dev/video0), --camera
permite analizar en directo en vez de sobre un vídeo ya grabado. Con
una cámara la fuente no tiene fin natural: el script sigue analizando
hasta que se detiene con Ctrl+C (o 'q' en la ventana de preview).

A cada alerta se le adjunta la posición del dron, que se lee del fichero
posicion_actual.json que escribe vuelo.py. Así el mensaje lleva la zona
(posición del dron) y los píxeles de la caja dentro del fotograma.

El envío MQTT se controla con --mqtt (por defecto true) y la ventana de
vista previa con --preview (por defecto true). El vídeo anotado de salida
se genera SIEMPRE, se muestre o no el preview, en:
    results/videos/{dron_id}_{video}_{fecha}.mp4

Cada vez que se envía una alerta (respetando el --anti-spam) se guarda
además el frame anotado de esa detección como JPEG en:
    results/fotos/{dron_id}_{fecha}.jpg
El nombre de ese fichero viaja también dentro del JSON de la alerta MQTT
(campo 'foto'), para poder relacionar cada alerta con su imagen. Con
--overlay (por defecto true) esa foto lleva además superpuestas las
coordenadas del dron y la fecha/hora de la detección; con --overlay false
se guarda el frame tal cual, sin esa marca. El vídeo anotado y el preview
nunca llevan overlay, solo la foto.

A cada alerta se le adjunta SIEMPRE el bloque 'dron' con la posición del
dron (la ubicación aproximada de la persona), leída de posicion_actual.json.
Si vuelo.py no está en marcha, ese fichero no existe y la alerta sale con
las coordenadas nulas (y un aviso por consola).

Uso:
    python3 deteccion.py vuelo1.mp4                  # MQTT y preview (por defecto)
    python3 deteccion.py vuelo1.mp4 --mqtt false     # no envía por MQTT
    python3 deteccion.py vuelo1.mp4 --preview false  # sin ventana (output igual)
    python3 deteccion.py vuelo1.mp4 --anti-spam 3    # una alerta como mucho cada 3 s
    python3 deteccion.py --camera 0                  # cámara en vivo (índice 0) en vez de un vídeo
    python3 deteccion.py --camera /dev/video0        # cámara en vivo por ruta de dispositivo (Raspberry Pi)
    python3 deteccion.py --camera 0 --preview false  # cámara en vivo, sin ventana (para systemd)
    python3 deteccion.py vuelo1.mp4 --overlay false  # fotos sin coordenadas/fecha superpuestas
    python3 deteccion.py -h                           # ayuda con los valores por defecto

Variables de entorno (.env) — necesarias solo con --mqtt true:
    DRON_ID     identificador del dron
    EC2_HOST    IP o dominio del broker MQTT
    MQTT_PORT   puerto MQTT (por defecto 1883)
    BUFFER_DB   ruta del buffer SQLite (por defecto: deteccion.db)
    POS_FILE    ruta del posicion_actual.json (compartido con vuelo.py)
    LOTE        filas enviadas por ciclo (por defecto 50)
"""

import argparse
import os
import time
import json
import sqlite3
from datetime import datetime
import cv2
from ultralytics import YOLO
from dotenv import load_dotenv
import paho.mqtt.client as mqtt


# =====================================================================
#  ARGUMENTOS DE LÍNEA DE COMANDOS  (los tuyos + MQTT y anti-spam)
# =====================================================================
def _str2bool(v):
    """Convierte 'true'/'false' (y equivalentes) en booleano, para --mqtt."""
    return str(v).strip().lower() in ('true', '1', 'yes', 'si', 's', 'y')


def _fuente_camara(v):
    """Convierte el valor de --camera en índice (int) o ruta de dispositivo (str).

    Un índice tipo '0' identifica la primera cámara del sistema; una ruta
    tipo '/dev/video0' apunta a un dispositivo V4L2 concreto (útil en la
    Raspberry Pi cuando hay varias cámaras o el índice no es estable).
    """
    return int(v) if v.isdigit() else v


parser = argparse.ArgumentParser(
    description='Detector de personas YOLO sobre un video o una camara en vivo.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument('video_path', nargs='?', default=None,
                     help='Ruta del video a analizar (ej. vuelo1.mp4). Omitir si se usa --camera')
parser.add_argument('--camera', type=_fuente_camara, default=None, metavar='INDICE_O_RUTA',
                     help="Analizar en vivo desde una camara en vez de un fichero: indice (0, 1...) "
                          "o ruta de dispositivo (/dev/video0). No se puede combinar con video_path")
parser.add_argument('--conf', type=float, default=0.5,
                     help='Confianza minima para mostrar una deteccion (subir = menos falsos positivos, bajar = menos personas sin detectar)')
parser.add_argument('--vid-stride', type=int, default=6,
                     help='Analiza 1 de cada N frames (1 = analiza todos; subirlo va mas rapido pero puede saltarse personas que pasan rapido)')
parser.add_argument('--augment', action=argparse.BooleanOptionalAction, default=True,
                     help='Test-time augmentation: analiza cada frame varias veces (flips/escalas) y combina resultados, mas preciso pero mas lento. Usa --no-augment para desactivarlo')
parser.add_argument('--mqtt', type=_str2bool, default=True,
                     help='Enviar las detecciones por MQTT (true/false). Con false no envia nada por MQTT')
parser.add_argument('--preview', type=_str2bool, default=True,
                     help='Mostrar la ventana de vista previa (true/false). El video de salida en output/ se genera SIEMPRE, se muestre o no el preview')
parser.add_argument('--anti-spam', type=float, default=5.0,
                     help='Segundos minimos entre envios de alertas por MQTT, para no saturar el topic con la misma persona en frames seguidos')
parser.add_argument('--overlay', type=_str2bool, default=True,
                     help='Añadir a la foto guardada (results/fotos/) la posicion del dron y la fecha/hora de la deteccion (true/false). No afecta al video anotado ni al preview')
args = parser.parse_args()


# =====================================================================
#  CONFIGURACIÓN (.env) — igual que el resto de colectores
# =====================================================================
load_dotenv()

DOMINIO = "deteccion"
DRON_ID = os.getenv("DRON_ID")
EC2_HOST = os.getenv("EC2_HOST")
PORT = int(os.getenv("MQTT_PORT", 1883))
LOTE = int(os.getenv("LOTE", 50))

# Rutas por defecto relativas al script (funcionan en Windows y en la Pi).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("BUFFER_DB", os.path.join(BASE_DIR, f"{DOMINIO}.db"))
# Mismo fichero de posición que escribe vuelo.py.
POS_FILE = os.getenv("POS_FILE", os.path.join(BASE_DIR, "posicion_actual.json"))

TOPIC = f"sar/{DRON_ID}/{DOMINIO}"
CLIENT_ID = f"{DRON_ID}-{DOMINIO}"

# El envío MQTT se controla con el parámetro --mqtt (por defecto true).
# Con --mqtt false, el script solo hace detección y preview.
MQTT_ON = args.mqtt
if MQTT_ON:
    faltan = [k for k, v in {"DRON_ID": DRON_ID, "EC2_HOST": EC2_HOST}.items() if not v]
    if faltan:
        raise SystemExit(
            f"--mqtt true pero faltan variables en el .env: {', '.join(faltan)}. "
            f"Usa --mqtt false para solo detección y preview.")

# Antigüedad máxima (s) de la posición para darla por buena sin avisar.
POS_MAX_EDAD = 5.0


# =====================================================================
#  MODELO Y VÍDEO  (tu código)
# =====================================================================
# Cargar VUESTRO cerebro entrenado
model = YOLO('weights/best.pt')

video_path = args.video_path
VID_STRIDE = args.vid_stride

# La fuente es un fichero O una camara, nunca las dos ni ninguna.
if (video_path is None) == (args.camera is None):
    raise SystemExit(
        'Indica exactamente una fuente: "video_path" (fichero) o --camera '
        '<indice/ruta>, pero no ambos ni ninguno.')

if video_path is not None:
    if not os.path.isfile(video_path):
        raise SystemExit(f'No encuentro "{video_path}"')
    fuente = video_path
    fuente_nombre = os.path.splitext(os.path.basename(video_path))[0]
else:
    fuente = args.camera
    fuente_nombre = f"camara{args.camera}".replace('/', '_')

# fps de la fuente, para que el output dure lo mismo que el original. En
# una camara en vivo esto no siempre esta disponible (muchas webcams y la
# camara de la Pi devuelven 0), asi que se usa un valor por defecto.
cap_info = cv2.VideoCapture(fuente)
fps_original = cap_info.get(cv2.CAP_PROP_FPS)
cap_info.release()
if not fps_original or fps_original <= 1:
    fps_original = 20.0
    print(f"Aviso: la fuente no informa un FPS valido; se usa {fps_original} por defecto.")


# =====================================================================
#  BUFFER LOCAL + CLIENTE MQTT  (solo si MQTT_ON)
# =====================================================================
if MQTT_ON:
    # El mensaje de detección tiene objetos anidados (caja, resolucion,
    # dron), así que se guarda el JSON completo en una columna 'payload'.
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS lecturas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        payload TEXT,
        enviado INTEGER DEFAULT 0)""")
    db.commit()

    client = mqtt.Client(client_id=CLIENT_ID,
                         callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(EC2_HOST, PORT, 60)
    client.loop_start()
    print(f"Dominio '{DOMINIO}' -> topic '{TOPIC}' como '{CLIENT_ID}'")
    print(f"Posición leída de: {POS_FILE}")
else:
    db = None
    client = None
    print("Modo solo preview (--mqtt false): detección y vídeo, sin envío MQTT.")


# =====================================================================
#  FUNCIONES DE LA CAPA MQTT
# =====================================================================
def leer_posicion():
    """Devuelve la última posición del dron (dict) o None si no está.

    Lee el posicion_actual.json que escribe vuelo.py. Si el fichero no
    existe todavía (vuelo.py no arrancado) o no se puede leer, devuelve
    None y la alerta se envía sin el bloque 'dron'.
    """
    try:
        with open(POS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _posicion_fresca(pos):
    """Comprueba si la posición es reciente; avisa por consola si es vieja."""
    try:
        edad = (datetime.now().astimezone()
                - datetime.fromisoformat(pos["ts"])).total_seconds()
        if edad > POS_MAX_EDAD:
            print(f"  (aviso: la posición del dron tiene {edad:.1f}s de antigüedad)")
    except (KeyError, ValueError, TypeError):
        pass


def guardar_deteccion(ts, payload):
    """Guarda una alerta en el buffer local (se enviará por MQTT)."""
    db.execute("INSERT INTO lecturas (ts, payload) VALUES (?,?)", (ts, payload))
    db.commit()


def _dibujar_overlay(frame, pos, ts):
    """Devuelve una copia del frame con las coordenadas del dron y la
    fecha/hora de la detección superpuestas (activado con --overlay).

    Solo afecta a la foto que se guarda en results/fotos/; el vídeo
    anotado y la ventana de preview no llevan esta marca.
    """
    frame = frame.copy()
    if pos and pos.get("lat") is not None and pos.get("lon") is not None:
        alt = pos.get("alt_rel")
        alt_txt = f"{alt:.1f}m" if alt is not None else "NA"
        linea_coords = f"lat {pos['lat']:.6f}  lon {pos['lon']:.6f}  alt {alt_txt}"
    else:
        linea_coords = "lat/lon: sin posicion"
    lineas = [ts.strftime("%Y-%m-%d %H:%M:%S"), linea_coords]

    # Texto negro grueso debajo y blanco fino encima, para que se lea sobre
    # cualquier fondo del vídeo. Las líneas se apilan desde el borde inferior.
    y = frame.shape[0] - 15
    for texto in reversed(lineas):
        cv2.putText(frame, texto, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, texto, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        y -= 28
    return frame


def guardar_foto(frame, pos, ts):
    """Guarda el frame donde se ha detectado una persona.

    Se llama solo cuando se supera el antispam (mismo ritmo que las
    alertas), así que genera como mucho una foto por alerta enviada, no
    una por frame analizado. Con --overlay (activado por defecto), la
    foto lleva superpuestas las coordenadas del dron y la fecha/hora. El
    nombre incluye el DRON_ID y la fecha, y se adjunta a cada alerta MQTT
    de ese frame para poder relacionarlas.
    """
    if args.overlay:
        frame = _dibujar_overlay(frame, pos, ts)
    fotos_dir = os.path.join('results', 'fotos')
    os.makedirs(fotos_dir, exist_ok=True)
    nombre = f"{DRON_ID or 'sindron'}_{ts.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    cv2.imwrite(os.path.join(fotos_dir, nombre), frame)
    return nombre


def reenviar():
    """Vacía el buffer de alertas hacia MQTT, solo con conexión (QoS 1)."""
    if not client.is_connected():
        return
    filas = db.execute(
        "SELECT id, payload FROM lecturas WHERE enviado=0 ORDER BY id LIMIT ?",
        (LOTE,)).fetchall()
    for id_, payload in filas:
        try:
            info = client.publish(TOPIC, payload, qos=1)
            info.wait_for_publish(timeout=5)
            if info.is_published():
                db.execute("UPDATE lecturas SET enviado=1 WHERE id=?", (id_,))
                db.commit()
                print(f"Alerta enviada [{TOPIC}]: {payload}")
            else:
                break
        except (ValueError, RuntimeError):
            break


def procesar_detecciones(r, foto_nombre, ts, pos):
    """Convierte las cajas detectadas en un frame en alertas y las encola.

    Emite una alerta por persona, todas con el mismo 'foto' y la misma
    posición (el frame y la posición del dron ya se leyeron una única vez
    para esta llamada, en el bucle principal). El control de frecuencia
    (anti-spam) lo aplica el bucle principal, para no saturar el topic
    con la misma persona en frames consecutivos.
    """
    if r.boxes is None or len(r.boxes) == 0:
        return

    ts_iso = ts.isoformat()

    # Posición del dron en el instante de la detección: es la ubicación
    # (aproximada, Nivel 0) de la persona localizada, leída de
    # posicion_actual.json (que escribe vuelo.py) y adjuntada SIEMPRE al
    # mensaje (con valores si está disponible, o nula si no lo está).
    if pos:
        dron = {"lat": pos.get("lat"),
                "lon": pos.get("lon"),
                "alt_rel": pos.get("alt_rel")}
    else:
        dron = None

    alto, ancho = r.orig_shape                 # resolución del frame original
    cajas = r.boxes.xywh.cpu().numpy()         # [N, 4] -> cx, cy, w, h (píxeles)
    confianzas = r.boxes.conf.cpu().numpy()    # [N]

    for (cx, cy, w, h), conf in zip(cajas, confianzas):
        mensaje = {
            "confianza": round(float(conf), 2),
            # Caja de la persona dentro del fotograma (píxeles): centro y tamaño.
            "caja": {"cx": int(cx), "cy": int(cy), "w": int(w), "h": int(h)},
            "resolucion": {"ancho": int(ancho), "alto": int(alto)},
            # Ubicación (aproximada) de la persona = posición del dron.
            "dron": dron,
            # Nombre del JPEG guardado en results/fotos/ con este frame.
            "foto": foto_nombre,
            "timestamp": ts_iso,
        }
        guardar_deteccion(ts_iso, json.dumps(mensaje))


# =====================================================================
#  INFERENCIA  (tu código)
# =====================================================================
# stream=True: procesa el video frame a frame (con el salto de vid_stride)
# sin cargarlo entero en memoria
results = model.predict(
    source=fuente,
    imgsz=640,       # igual que el imgsz de entrenamiento; a menos resolucion se pierde detalle y confunde mas las clases
    conf=args.conf,        # confianza minima para mostrar una deteccion (subir = menos falsos positivos, bajar = menos personas sin detectar)
    vid_stride=VID_STRIDE,    # 1 = analiza todos los frames; subirlo va mas rapido pero puede saltarse personas que pasan rapido
    stream=True,
    verbose=False,
    augment=args.augment     # test-time augmentation: analiza cada frame varias veces (flips/escalas) y combina resultados, mas preciso pero mas lento
)

writer = None
ultimo_envio = 0.0     # marca de tiempo del último envío, para el anti-spam
# Fecha de realización del vídeo: se fija una sola vez al empezar a
# procesar, no en cada frame, para que el nombre del fichero sea estable.
FECHA_INICIO = datetime.now()

try:
    for r in results:
        annotated_frame = r.plot()

        if writer is None:
            h, w = annotated_frame.shape[:2]
            videos_dir = os.path.join('results', 'videos')
            os.makedirs(videos_dir, exist_ok=True)
            fecha_str = FECHA_INICIO.strftime('%Y%m%d_%H%M%S')
            nombre_video = f"{DRON_ID or 'sindron'}_{fuente_nombre}_{fecha_str}.mp4"
            output_path = os.path.join(videos_dir, nombre_video)
            writer = cv2.VideoWriter(
                output_path,
                cv2.VideoWriter_fourcc(*'mp4v'),
                fps_original / VID_STRIDE,
                (w, h),
            )
        writer.write(annotated_frame)

        # ----- NUEVO: detección -> alerta MQTT (con anti-spam) -----
        if MQTT_ON and r.boxes is not None and len(r.boxes) > 0:
            ahora = time.monotonic()
            if ahora - ultimo_envio >= args.anti_spam:
                ts = datetime.now().astimezone()
                pos = leer_posicion()
                if pos:
                    _posicion_fresca(pos)
                else:
                    print("  (aviso: sin posicion_actual.json; la alerta va SIN coordenadas. "
                          "¿Está vuelo.py en marcha en la misma carpeta?)")
                foto_nombre = guardar_foto(annotated_frame, pos, ts)
                procesar_detecciones(r, foto_nombre, ts, pos)
                ultimo_envio = ahora
        # Se intenta vaciar el buffer en cada frame (barato si está vacío).
        if MQTT_ON:
            reenviar()

        # Vista previa opcional (--preview). El vídeo de salida ya se ha
        # escrito arriba, así que se genera SIEMPRE, se muestre o no la ventana.
        if args.preview:
            # Redimensionar manteniendo la proporcion original
            # (960x540 fijo deformaba los videos verticales del iPhone)
            h, w = annotated_frame.shape[:2]
            escala = 540 / h
            vista = cv2.resize(annotated_frame, (round(w * escala), 540))

            # Mostrar ventana interactiva (q para salir)
            cv2.imshow('Detector de personas', vista)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

except KeyboardInterrupt:
    # Con --camera el analisis no tiene fin natural (a diferencia de un
    # fichero de video); Ctrl+C es la forma normal de pararlo.
    print("\nDetenido por el usuario.")

finally:
    # Cierre ordenado (también si se interrumpe con Ctrl+C).
    if writer is not None:
        writer.release()
    cv2.destroyAllWindows()
    if MQTT_ON:
        reenviar()               # último intento de vaciar el buffer
        client.loop_stop()
        client.disconnect()
        db.close()