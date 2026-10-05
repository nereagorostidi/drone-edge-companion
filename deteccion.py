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
persona publica una alerta en dronsar/{dron_id}/deteccion con la misma
lógica que el resto de colectores (buffer local SQLite + reenvío MQTT
"store-and-forward").

La fuente es un fichero de vídeo (argumento posicional) O una cámara
conectada (--camera), nunca las dos a la vez. En la Raspberry Pi, con
la cámara accesible como dispositivo V4L2 (/dev/video0), --camera
permite analizar en directo en vez de sobre un vídeo ya grabado. Con
una cámara la fuente no tiene fin natural: el script sigue analizando
hasta que se detiene con Ctrl+C (o 'q' en la ventana de preview).

Con --camera y MQTT activo (el caso real: el servicio systemd), el
script NO arranca a analizar solo: se queda a la espera del comando
'start_recording' del panel de control, en el mismo topic de
configuración que usa 'set_video_throttle'
(dronsar/{dron_id}/deteccion/config). Mientras espera no hay preview,
ni vídeo, ni detección: el proceso solo escucha MQTT. Al recibir
'start_recording' arranca la sesión (vídeo, preview, detección y
alertas, todo junto); al recibir 'stop_recording' la cierra (guarda el
vídeo de esa sesión) SIN cerrar el script, que vuelve a quedarse a la
espera del siguiente 'start_recording'. Cada sesión genera su propio
vídeo en results/videos/, con su propio timestamp. Con un fichero de
vídeo, o sin MQTT, no hay nada que esperar: arranca directo, como
siempre.

A cada alerta se le adjunta la posición del dron, que se lee del fichero
posicion_actual.json que escribe vuelo.py. Así el mensaje lleva la zona
(posición del dron) y los píxeles de la caja dentro del fotograma.

El envío MQTT se controla con --mqtt (por defecto true) y la ventana de
vista previa con --preview (por defecto FALSE: hay que activarla a mano
con --preview true). El vídeo anotado de salida se genera igual, se
muestre o no el preview, en (VIDEOS_DIR configurable por .env, por
defecto results/videos):
    {VIDEOS_DIR}/{dron_id}_{video}_{fecha}.mp4
Con --raw (por defecto true) se graba además, a la vez y con el mismo
códec/FPS/resolución, una copia SIN detecciones dibujadas:
    {VIDEOS_DIR}/{dron_id}_{video}_{fecha}_raw.mp4

Cada vez que se envía una alerta (respetando el --anti-spam) se guarda
además el frame anotado de esa detección como JPEG en (FOTOS_DIR
configurable por .env, por defecto results/fotos):
    {FOTOS_DIR}/{dron_id}_{fecha}.jpg
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

Al terminar cada sesión de grabación (con MQTT activo) se publica además
un resumen en dronsar/{dron_id}/video/resumen: evento, fichero de vídeo,
duración, frames totales, rendimiento (runtime, fps medio, latencia media
y p95, vid_stride), detecciones (alertas emitidas y confianza media) y
timestamp_inicio/timestamp_fin de la sesión (ver publicar_resumen_video).

Uso:
    python3 deteccion.py vuelo1.mp4                  # MQTT activado, SIN preview (por defecto)
    python3 deteccion.py vuelo1.mp4 --mqtt false     # no envía por MQTT
    python3 deteccion.py vuelo1.mp4 --preview true   # con ventana de vista previa (output igual)
    python3 deteccion.py vuelo1.mp4 --anti-spam 3    # una alerta como mucho cada 3 s
    python3 deteccion.py --camera 0                  # cámara en vivo (índice 0); con MQTT, espera 'start_recording' del panel
    python3 deteccion.py --camera /dev/video0        # cámara en vivo por ruta de dispositivo (Raspberry Pi)
    python3 deteccion.py --camera 0 --preview true   # cámara en vivo, con ventana (NO usar en systemd)
    python3 deteccion.py vuelo1.mp4 --overlay false  # fotos sin coordenadas/fecha superpuestas
    python3 deteccion.py vuelo1.mp4 --raw false      # sin la copia _raw.mp4 (solo el vídeo anotado)
    python3 deteccion.py vuelo1.mp4 --runtime onnx   # carga weights/best.onnx (mas ligero, requiere conversion/exportar_onnx.py antes)
    python3 deteccion.py vuelo1.mp4 --runtime onnx-int8  # carga weights/best.int8.onnx (cuantizado, requiere conversion/cuantizar_onnx.py antes)
    python3 deteccion.py vuelo1.mp4 --runtime ncnn   # carga weights/best_ncnn_model/ (requiere conversion/exportar_ncnn.py antes)
    python3 deteccion.py vuelo1.mp4 --runtime hef    # carga weights/best_hailo_model/ (acelerador Hailo; solo en la Raspberry Pi con el AI Kit y HailoRT)
    python3 deteccion.py -h                           # ayuda con los valores por defecto

Variables de entorno (.env) — necesarias solo con --mqtt true:
    DRON_ID     identificador del dron
    EC2_HOST    IP o dominio del broker MQTT
    MQTT_PORT   puerto MQTT (por defecto 1883)
    BUFFER_DB   ruta del buffer SQLite (por defecto: deteccion.db)
    POS_FILE    ruta del posicion_actual.json (compartido con vuelo.py)
    LOTE        filas enviadas por ciclo (por defecto 50)
    CONF_FILE   fichero donde se guarda el umbral de confianza fijado desde el
                panel (por defecto ~/.config/deteccion/confianza.json)
"""

import argparse
import os
import time
import json
import sqlite3
import threading
from datetime import datetime, timedelta
import cv2
from ultralytics import YOLO
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from streaming import EmisorRTSP


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
parser.add_argument('--conf', type=float, default=None,
                     help='Confianza minima para mostrar una deteccion (subir = menos falsos positivos, bajar = menos personas sin detectar). '
                          'Si se omite, se usa la guardada desde el panel (CONF_FILE) o, si no hay, 0.5. '
                          'Si se pasa, manda sobre la guardada en esta ejecucion (sin sobrescribirla)')
parser.add_argument('--vid-stride', type=int, default=2,
                     help='Analiza 1 de cada N frames (1 = analiza todos; subirlo va mas rapido pero puede saltarse personas que pasan rapido)')
parser.add_argument('--augment', action=argparse.BooleanOptionalAction, default=True,
                     help='Test-time augmentation: analiza cada frame varias veces (flips/escalas) y combina resultados, mas preciso pero mas lento. Usa --no-augment para desactivarlo')
parser.add_argument('--mqtt', type=_str2bool, default=True,
                     help='Enviar las detecciones por MQTT (true/false). Con false no envia nada por MQTT')
parser.add_argument('--preview', type=_str2bool, default=False,
                     help='Mostrar la ventana de vista previa (true/false). El video de salida en output/ se genera igual, se muestre o no el preview')
parser.add_argument('--anti-spam', type=float, default=5.0,
                     help='Segundos minimos entre envios de alertas por MQTT, para no saturar el topic con la misma persona en frames seguidos')
parser.add_argument('--overlay', type=_str2bool, default=True,
                     help='Añadir a la foto guardada (results/fotos/) la posicion del dron y la fecha/hora de la deteccion (true/false). No afecta al video anotado ni al preview')
parser.add_argument('--stream', type=_str2bool, default=True,
                     help='Emitir el vídeo anotado en directo hacia MediaMTX (true/false), en paralelo a la grabación local')
parser.add_argument('--raw', type=_str2bool, default=True,
                     help='Grabar además una copia del vídeo SIN detecciones dibujadas ({nombre}_raw.mp4, true/false). '
                          'Desactivar con --raw false si en vuelo baja demasiado el rendimiento')
parser.add_argument('--runtime', choices=('pt', 'onnx', 'onnx-int8', 'ncnn', 'hef'), default='pt',
                     help="Motor de inferencia: 'pt' carga weights/best.pt via PyTorch (el de siempre); "
                          "'onnx' carga weights/best.onnx via ONNX Runtime (mas ligero/rapido, requiere "
                          "haberlo generado antes con conversion/exportar_onnx.py); 'onnx-int8' carga "
                          "weights/best.int8.onnx, la version cuantizada (aun mas ligera, requiere "
                          "haberla generado antes con conversion/cuantizar_onnx.py; revisa la precision antes de "
                          "usarla en vuelo real); 'ncnn' carga la carpeta weights/best_ncnn_model/ "
                          "(motor optimizado para CPUs ARM como la de la Raspberry Pi, requiere haberla "
                          "generado antes con conversion/exportar_ncnn.py); 'hef' carga la carpeta "
                          "weights/best_hailo_model/ en el acelerador Hailo del AI Kit (el .hef se compila "
                          "aparte y se deja ahi; SOLO funciona en la Raspberry Pi con el Hailo conectado y "
                          "HailoRT instalado, no en un PC normal)")
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

# Carpetas de salida (vídeos anotados y fotos de alerta). Configurables por
# si el día de mañana se monta almacenamiento aparte (p. ej. /media/...) sin
# tener que tocar código: basta con fijar VIDEOS_DIR/FOTOS_DIR en el .env.
VIDEOS_DIR = os.getenv("VIDEOS_DIR", os.path.join(BASE_DIR, "results", "videos"))
FOTOS_DIR = os.getenv("FOTOS_DIR", os.path.join(BASE_DIR, "results", "fotos"))
# Los ficheros se escriben primero en una subcarpeta 'en_curso' y solo se
# mueven a VIDEOS_DIR/FOTOS_DIR cuando están COMPLETOS (os.replace es atómico
# dentro del mismo disco). Así, lo que hay en VIDEOS_DIR/FOTOS_DIR está
# siempre terminado, y una sincronización (rsync) que excluya 'en_curso/'
# nunca puede subir ni borrar un vídeo que se está grabando.
VIDEOS_EN_CURSO = os.path.join(VIDEOS_DIR, "en_curso")
FOTOS_EN_CURSO = os.path.join(FOTOS_DIR, "en_curso")

TOPIC = f"dronsar/{DRON_ID}/{DOMINIO}"
CLIENT_ID = f"{DRON_ID}-{DOMINIO}"

# Topic de configuración remota (panel de control -> este script), con el
# mismo esquema "dronsar/..." que usa receptor.py para comandos.
CONFIG_TOPIC = f"dronsar/{DRON_ID}/deteccion/config"

# Topic del resumen de cada sesión de grabación (ver publicar_resumen_video).
RESUMEN_TOPIC = f"dronsar/{DRON_ID}/video/resumen"

# Anti-spam EN USO (segundos mínimos entre alertas MQTT). Arranca con el
# valor de --anti-spam, pero se puede actualizar en caliente desde el panel
# de control (ver on_message), igual que el intervalo en sensor.py.
anti_spam_actual = args.anti_spam

# Umbral de confianza EN USO. Se puede cambiar en caliente desde el panel
# (set_confidence, ver on_message) y se guarda en CONF_FILE para que
# sobreviva a un reinicio del servicio. CONF_POR_DEFECTO solo se usa si no
# hay valor guardado (o no es válido) y no se ha pasado --conf.
CONF_POR_DEFECTO = 0.5
CONF_FILE = os.getenv("CONF_FILE", os.path.expanduser("~/.config/deteccion/confianza.json"))
# El callback MQTT corre en el hilo de paho y el bucle de inferencia en el
# principal: todo acceso a _confianza_actual pasa por este lock.
_conf_lock = threading.Lock()


def _confianza_valida(valor):
    """True si 'valor' es un número (no bool) entre 0 y 1, ambos incluidos."""
    return (isinstance(valor, (int, float)) and not isinstance(valor, bool)
            and 0.0 <= valor <= 1.0)


def _cargar_confianza():
    """Lee el umbral guardado en CONF_FILE; si no existe o no es válido,
    devuelve CONF_POR_DEFECTO."""
    try:
        with open(CONF_FILE) as f:
            valor = json.load(f).get("confianza")
    except FileNotFoundError:
        print(f"Sin umbral de confianza guardado ({CONF_FILE}); "
              f"se usa el valor por defecto {CONF_POR_DEFECTO}.")
        return CONF_POR_DEFECTO
    except (json.JSONDecodeError, OSError, AttributeError) as e:
        print(f"Aviso: no se pudo leer {CONF_FILE} ({e}); "
              f"se usa el valor por defecto {CONF_POR_DEFECTO}.")
        return CONF_POR_DEFECTO
    if not _confianza_valida(valor):
        print(f"Aviso: umbral guardado en {CONF_FILE} no válido ({valor!r}); "
              f"se usa el valor por defecto {CONF_POR_DEFECTO}.")
        return CONF_POR_DEFECTO
    print(f"Umbral de confianza cargado de {CONF_FILE}: {float(valor)}")
    return float(valor)


def _guardar_confianza(valor):
    """Guarda el umbral en CONF_FILE de forma atómica: se escribe un .tmp y
    se renombra (os.replace), así nunca queda un fichero a medias."""
    tmp = CONF_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(CONF_FILE) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump({"confianza": valor,
                       "actualizado": datetime.now().astimezone().isoformat()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONF_FILE)
    except OSError as e:
        print(f"  -> ERROR: no se pudo guardar el umbral en {CONF_FILE} ({e}); "
              f"se aplica igualmente, pero se perderá al reiniciar.")


def obtener_confianza():
    """Umbral de confianza en uso (thread-safe)."""
    with _conf_lock:
        return _confianza_actual


def fijar_confianza(nueva):
    """Cambia el umbral en uso (thread-safe) y devuelve el anterior."""
    global _confianza_actual
    with _conf_lock:
        anterior, _confianza_actual = _confianza_actual, nueva
    return anterior


if args.conf is not None:
    if not _confianza_valida(args.conf):
        raise SystemExit(f"--conf debe estar entre 0 y 1 (recibido: {args.conf}).")
    _confianza_actual = args.conf
    print(f"Umbral de confianza fijado por --conf: {args.conf} (no se guarda en {CONF_FILE}).")
else:
    _confianza_actual = _cargar_confianza()

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

# --- Config del streaming en directo (solo si --stream) ---
STREAM_ON = args.stream
STREAM_HOST = os.getenv("STREAM_HOST")
STREAM_USER = os.getenv("STREAM_USER")
STREAM_PASS = os.getenv("STREAM_PASS")
STREAM_PATH = os.getenv("STREAM_PATH", "dron_live")
STREAM_ANCHO = int(os.getenv("STREAM_ANCHO", 640))
STREAM_ALTO = int(os.getenv("STREAM_ALTO", 360))
STREAM_FPS = int(os.getenv("STREAM_FPS", 12))

# Si se pide streaming pero faltan datos del .env, se avisa y se desactiva
# (sin tumbar el script: el vídeo en directo es un extra, no algo crítico).
if STREAM_ON and not all([STREAM_HOST, STREAM_USER, STREAM_PASS]):
    print("Aviso: --stream true pero faltan STREAM_HOST/USER/PASS en el .env; "
          "se desactiva el streaming en directo.")
    STREAM_ON = False


# =====================================================================
#  MODELO Y VÍDEO  (tu código)
# =====================================================================
# Cargar VUESTRO cerebro entrenado. El motor de inferencia (--runtime)
# es independiente del modelo base: solo decide que pesos se cargan y
# con que backend (PyTorch, ONNX Runtime, NCNN o Hailo). Todos son un
# unico fichero salvo 'ncnn' y 'hef', que son carpetas.
NOMBRE_PESOS = {'pt': 'best.pt', 'onnx': 'best.onnx', 'onnx-int8': 'best.int8.onnx',
                'ncnn': 'best_ncnn_model', 'hef': 'best_hailo_model'}[args.runtime]
GENERAR_CON = {'pt': None, 'onnx': 'conversion/exportar_onnx.py', 'onnx-int8': 'conversion/cuantizar_onnx.py',
               'ncnn': 'conversion/exportar_ncnn.py', 'hef': None}[args.runtime]
WEIGHTS_PATH = os.path.join(BASE_DIR, 'weights', NOMBRE_PESOS)
ES_CARPETA = args.runtime in ('ncnn', 'hef')

# El .hef (formato del acelerador Hailo del AI Kit) no se genera aqui: se
# compila aparte y se deja en weights/. Ultralytics lo carga desde una
# CARPETA weights/best_hailo_model/ (igual que ncnn), que debe contener el
# best.hef y, idealmente, su metadata.yaml. Si el best.hef quedo suelto en
# weights/, lo movemos dentro de esa carpeta la primera vez (y su
# metadata.yaml si tambien esta suelto al lado).
if args.runtime == 'hef' and not os.path.isdir(WEIGHTS_PATH):
    hef_suelto = os.path.join(BASE_DIR, 'weights', 'best.hef')
    if os.path.isfile(hef_suelto):
        os.makedirs(WEIGHTS_PATH, exist_ok=True)
        os.replace(hef_suelto, os.path.join(WEIGHTS_PATH, 'best.hef'))
        meta_suelto = os.path.join(BASE_DIR, 'weights', 'metadata.yaml')
        if os.path.isfile(meta_suelto):
            os.replace(meta_suelto, os.path.join(WEIGHTS_PATH, 'metadata.yaml'))
        print(f'best.hef movido a "{WEIGHTS_PATH}" (carpeta que espera Ultralytics para Hailo).')

existe = os.path.isdir(WEIGHTS_PATH) if ES_CARPETA else os.path.isfile(WEIGHTS_PATH)
if not existe:
    if args.runtime == 'hef':
        raise SystemExit(
            f'No encuentro "{WEIGHTS_PATH}". Crea la carpeta weights/best_hailo_model/ con el '
            f'best.hef dentro (y su metadata.yaml si lo tienes), o usa --runtime pt. Recuerda que '
            f'--runtime hef solo funciona en la Raspberry Pi con el Hailo conectado y HailoRT instalado.')
    raise SystemExit(
        f'No encuentro "{WEIGHTS_PATH}". '
        + (f'Genera ese fichero antes con {GENERAR_CON} o usa --runtime pt.'
           if GENERAR_CON else 'Falta weights/best.pt.'))

# El .hef de este proyecto NO lo exporto Ultralytics (sale del pipeline del
# Hailo Model Zoo / DFC) y devuelve los tensores del head YOLO sin
# postprocesar, asi que el backend Hailo de Ultralytics no sabe leerlo. Lo
# ejecuta un runtime propio (runtime_hef.py): HailoRT + postproceso YOLO a
# mano, exponiendo la misma interfaz .predict()/.boxes/.plot() que YOLO.
if args.runtime == 'hef':
    from runtime_hef import HailoYolo
    model = HailoYolo(WEIGHTS_PATH, imgsz=640)
else:
    model = YOLO(WEIGHTS_PATH)

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

# FPS del vídeo que se GRABA: se escribe un fotograma por cada fotograma
# analizado (1 de cada VID_STRIDE), así que el fichero va a fps_original /
# VID_STRIDE. Si la inferencia no llega a ese ritmo, el vídeo dura MENOS que la
# sesión real: por eso las posiciones dentro del fichero (tiempo_en_video_s,
# duracion_fichero_s) se calculan por número de fotograma, no por el reloj.
FPS_VIDEO = fps_original / VID_STRIDE


# =====================================================================
#  BUFFER LOCAL + CLIENTE MQTT  (solo si MQTT_ON)
# =====================================================================
if MQTT_ON:
    # El mensaje de detección tiene objetos anidados (caja, resolucion,
    # dron), así que se guarda el JSON completo en una columna 'payload'.
    # timeout=60: si limpia.py está compactando la base de datos (VACUUM la
    # bloquea entera), se espera hasta 60 s en vez de fallar a los 5 s por
    # defecto con "database is locked" y tumbar el proceso.
    db = sqlite3.connect(DB, timeout=60)
    db.execute("""CREATE TABLE IF NOT EXISTS lecturas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        payload TEXT,
        enviado INTEGER DEFAULT 0)""")
    db.commit()

    def on_connect(client, userdata, flags, reason_code, properties):
        """Al conectar (o reconectar), nos suscribimos al topic de configuración."""
        client.subscribe(CONFIG_TOPIC, qos=1)
        print(f"Suscrito a '{CONFIG_TOPIC}'")

    def on_message(client, userdata, msg):
        """Aplica un comando de configuración recibido del panel de control.

        Formato del payload (lo publica api.py, ver COMANDOS_CONFIG):
            {"command": "...", "params": {...}, "dron_id": "...",
             "command_id": "...", "timestamp": "..."}

        Comandos soportados en este topic:
            set_video_throttle  {"throttle_ms": N}  Cambia el anti-spam de alertas
                                                     (llega en ms; se guarda en s).
            start_recording      {}                 Arranca la grabación/detección
                                                     (ver ESPERA_COMANDO más abajo).
            stop_recording        {}                 La detiene, sin cerrar el script.
            set_confidence       {"confidence": X}  Cambia el umbral de confianza (0-1)
                                                     en caliente y lo guarda en CONF_FILE.
        """
        global anti_spam_actual, grabando
        try:
            orden = json.loads(msg.payload)
        except json.JSONDecodeError:
            print(f"Mensaje recibido en '{CONFIG_TOPIC}' que no es JSON válido; se ignora.")
            return

        command = orden.get("command")
        cmd_id = orden.get("command_id", "?")
        print(f"Comando de configuración recibido [{cmd_id}]: {command}")

        if command == "set_video_throttle":
            try:
                throttle_ms = float(orden["params"]["throttle_ms"])
            except (KeyError, TypeError, ValueError):
                print("  -> 'params.throttle_ms' ausente o inválido; se ignora.")
                return
            anti_spam_actual = throttle_ms / 1000.0
            print(f"  -> Anti-spam de alertas actualizado a {anti_spam_actual}s ({throttle_ms} ms)")

        elif command == "start_recording":
            grabando = True
            print("  -> Grabación/detección iniciada")

        elif command == "stop_recording":
            grabando = False
            print("  -> Grabación/detección detenida (el script sigue en marcha, a la espera)")

        elif command == "set_confidence":
            params = orden.get("params")
            nueva = params.get("confidence") if isinstance(params, dict) else None
            if not _confianza_valida(nueva):
                print(f"  -> ERROR: 'params.confidence' debe ser un número entre 0 y 1 "
                      f"(recibido: {nueva!r}); se ignora.")
                return
            nueva = float(nueva)
            anterior = fijar_confianza(nueva)
            print(f"  -> Umbral de confianza actualizado: {anterior} -> {nueva}")
            _guardar_confianza(nueva)

        else:
            print(f"  -> Comando desconocido '{command}' en este topic; se ignora.")

    client = mqtt.Client(client_id=CLIENT_ID,
                         callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
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

    # Un único color (amarillo), sin contorno superpuesto: se lee bien sobre
    # el verde/tierra/gris típico de las fotos aéreas. Las líneas se apilan
    # desde el borde inferior.
    y = frame.shape[0] - 15
    for texto in reversed(lineas):
        cv2.putText(frame, texto, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
        y -= 28
    return frame


def _tamano_fichero(ruta):
    """Tamaño en bytes de un fichero, o None si no existe o no se puede leer."""
    try:
        return os.path.getsize(ruta)
    except OSError:
        return None


def _publicar_video(ruta_en_curso, ruta_final):
    """Mueve el vídeo ya cerrado de en_curso/ a VIDEOS_DIR (atómico).

    Se llama siempre DESPUÉS de writer.release(): hasta entonces el .mp4 no
    está completo (le falta el índice final y no se podría reproducir).
    """
    if ruta_en_curso and os.path.exists(ruta_en_curso):
        os.replace(ruta_en_curso, ruta_final)


def _avisar_huerfanos():
    """Avisa de ficheros que quedaron en en_curso/ por una parada brusca.

    Si el proceso se cae (o se corta la alimentación) a mitad de una
    grabación, el vídeo se queda en en_curso/ sin cerrar: normalmente no es
    reproducible y NO se sincroniza. No se borra automáticamente, para poder
    revisarlo a mano si hiciera falta.
    """
    for carpeta in (VIDEOS_EN_CURSO, FOTOS_EN_CURSO):
        if os.path.isdir(carpeta):
            huerfanos = sorted(os.listdir(carpeta))
            if huerfanos:
                print(f"Aviso: {len(huerfanos)} fichero(s) sin terminar en {carpeta} "
                      f"(de una parada brusca anterior): {', '.join(huerfanos[:5])}"
                      f"{' ...' if len(huerfanos) > 5 else ''}")


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
    os.makedirs(FOTOS_EN_CURSO, exist_ok=True)
    nombre = f"{DRON_ID or 'sindron'}_{ts.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    ruta_en_curso = os.path.join(FOTOS_EN_CURSO, nombre)
    # Se escribe en en_curso/ y se mueve a FOTOS_DIR ya completa (ver arriba).
    if cv2.imwrite(ruta_en_curso, frame):
        os.replace(ruta_en_curso, os.path.join(FOTOS_DIR, nombre))
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


def procesar_detecciones(r, foto_nombre, ts, pos, foto_bytes=None, tiempo_en_video_s=None,
                         video_fichero=None):
    """Convierte las cajas detectadas en un frame en alertas y las encola.

    Emite una alerta por persona, todas con el mismo 'foto' y la misma
    posición (el frame y la posición del dron ya se leyeron una única vez
    para esta llamada, en el bucle principal). El control de frecuencia
    (anti-spam) lo aplica el bucle principal, para no saturar el topic
    con la misma persona en frames consecutivos.

    Devuelve la lista de confianzas de las alertas encoladas en esta
    llamada, para que el bucle principal las acumule y calcule la
    'confianza_media' del resumen de la sesión (ver publicar_resumen_video).
    """
    if r.boxes is None or len(r.boxes) == 0:
        return []

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

    confianzas_encoladas = []
    for i, ((cx, cy, w, h), conf) in enumerate(zip(cajas, confianzas)):
        conf_redondeada = round(float(conf), 2)
        mensaje = {
            "confianza": conf_redondeada,
            # Caja de la persona dentro del fotograma (píxeles): centro y tamaño.
            "caja": {"cx": int(cx), "cy": int(cy), "w": int(w), "h": int(h)},
            "resolucion": {"ancho": int(ancho), "alto": int(alto)},
            # Ubicación (aproximada) de la persona = posición del dron.
            "dron": dron,
            # Nombre del JPEG guardado en results/fotos/ con este frame.
            "foto": foto_nombre,
            # Tamaño de esa foto en bytes (para estimar el consumo de disco).
            "tamano_bytes": foto_bytes,
            # Vídeo de la sesión en el que está este frame (mismo nombre que
            # video.fichero del resumen) y segundo en que aparece dentro de él:
            # juntos permiten abrir ese vídeo y saltar directamente al instante.
            "video_fichero": video_fichero,
            "tiempo_en_video_s": tiempo_en_video_s,
            # InfluxDB identifica un punto por measurement + tags (dron_id) +
            # timestamp: con varias personas en el mismo fotograma y el mismo
            # timestamp, cada alerta SOBRESCRIBIRÍA a la anterior y solo quedaría
            # la última. Se desplaza 1 µs por persona (+0, +1, +2 µs...) para que
            # cada una sea un punto distinto; la diferencia es imperceptible.
            "timestamp": (ts + timedelta(microseconds=i)).isoformat() if i else ts_iso,
        }
        guardar_deteccion(mensaje["timestamp"], json.dumps(mensaje))
        confianzas_encoladas.append(conf_redondeada)
    return confianzas_encoladas


def _percentil(valores, p):
    """Percentil p (0-100) de una lista, por interpolación lineal (sin numpy)."""
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    k = (len(ordenados) - 1) * (p / 100)
    piso, techo = int(k), min(int(k) + 1, len(ordenados) - 1)
    if piso == techo:
        return ordenados[piso]
    return ordenados[piso] + (ordenados[techo] - ordenados[piso]) * (k - piso)


def publicar_resumen_video(fichero_video, inicio, fin, frames_totales, latencias_ms,
                            alertas_total, confianzas_alertas,
                            tamano_bytes=None, duracion_fichero_s=None,
                            foto_representativa=None, confianza_maxima=None):
    """Publica el resumen de una sesión de grabación que acaba de terminar.

    Formato acordado con el tutor, topic dronsar/{dron_id}/video/resumen.
    Se manda SIEMPRE que termine una sesión con MQTT activo (aunque haya
    durado 0 frames). 'timestamp' se manda igual a 'timestamp_fin' para que
    el puente hacia InfluxDB use la hora exacta de cierre de la sesión en
    vez de la hora de llegada del mensaje.
    """
    if latencias_ms:
        latencia_media_ms = sum(latencias_ms) / len(latencias_ms)
        latencia_p95_ms = _percentil(latencias_ms, 95)
        fps_medio = 1000.0 / latencia_media_ms if latencia_media_ms > 0 else 0.0
    else:
        latencia_media_ms = latencia_p95_ms = fps_medio = 0.0
    confianza_media = (round(sum(confianzas_alertas) / len(confianzas_alertas), 2)
                        if confianzas_alertas else 0.0)

    resumen = {
        "evento": "sesion_completada",
        "dron_id": DRON_ID,
        "video": {
            "fichero": fichero_video,
            # Duración de la SESIÓN (reloj: de start_recording a stop_recording).
            "duracion_segundos": round((fin - inicio).total_seconds(), 1),
            # Duración del FICHERO grabado (frames / FPS del vídeo). Es menor que
            # la de la sesión si la inferencia no mantiene el ritmo del vídeo.
            "duracion_fichero_s": duracion_fichero_s,
            "frames_totales": frames_totales,
            # Tamaño del .mp4 en bytes (para estimar el consumo de disco).
            "tamano_bytes": tamano_bytes,
        },
        "rendimiento": {
            "runtime": args.runtime,
            "fps_medio": round(fps_medio, 1),
            "latencia_media_ms": round(latencia_media_ms, 1),
            "latencia_p95_ms": round(latencia_p95_ms, 1),
            "vid_stride": VID_STRIDE,
        },
        "detecciones": {
            "total_alertas_emitidas": alertas_total,
            "confianza_media": confianza_media,
            # Alerta de mayor confianza de la sesión (None si no hubo ninguna):
            # su foto sirve de imagen representativa del vídeo en el servidor.
            "confianza_maxima": confianza_maxima,
            "foto_representativa": foto_representativa,
        },
        "timestamp_inicio": inicio.isoformat(),
        "timestamp_fin": fin.isoformat(),
        "timestamp": fin.isoformat(),
    }
    payload = json.dumps(resumen)
    try:
        info = client.publish(RESUMEN_TOPIC, payload, qos=1)
        info.wait_for_publish(timeout=5)
        print(f"Resumen de sesión enviado [{RESUMEN_TOPIC}]: {payload}")
    except (ValueError, RuntimeError) as e:
        print(f"  (aviso: no se pudo publicar el resumen de la sesión: {e})")


# =====================================================================
#  ARRANQUE/PARADA REMOTA  (start_recording / stop_recording)
# =====================================================================
# Solo tiene sentido esperar un comando del panel en el caso de uso real:
# camara en vivo + MQTT (el del servicio systemd). Con un fichero de video,
# o sin MQTT (nadie puede mandar el comando), se arranca directo como
# siempre. ESPERA_COMANDO controla ademas si, al parar una grabacion, el
# script se queda vivo esperando la siguiente, o si termina del todo.
ESPERA_COMANDO = MQTT_ON and args.camera is not None
grabando = not ESPERA_COMANDO
if ESPERA_COMANDO:
    print(f"A la espera de 'start_recording' desde el panel (topic '{CONFIG_TOPIC}')...")

_avisar_huerfanos()

ultimo_envio = 0.0     # marca de tiempo del último envío, para el anti-spam (persiste entre sesiones)
parar_por_usuario = False   # se puso a True al pulsar 'q' en el preview: para todo, no solo la sesion
writer = None   # definido aqui para que el finally pueda cerrarlo aunque no haya arrancado ninguna sesion
writer_raw = None   # igual que writer, para la copia sin detecciones (--raw)
emisor = None   # emisor de streaming de la sesión en curso (como writer, pero para el directo)


# =====================================================================
#  INFERENCIA  (tu código, ahora repetible: una vuelta por cada
#  start_recording -> stop_recording)
# =====================================================================
try:
    while not parar_por_usuario:
        if ESPERA_COMANDO and not grabando:
            # Nada que procesar todavia: seguimos vivos, atendiendo MQTT
            # (el hilo de client.loop_start() ya escucha start_recording) y
            # vaciando el buffer por si quedaban alertas pendientes de antes.
            if MQTT_ON:
                reenviar()
            time.sleep(0.3)
            continue

        # stream=True: procesa el video frame a frame (con el salto de
        # vid_stride) sin cargarlo entero en memoria.
        # classes=[0]: nos quedamos solo con la clase 0 ('persona'). Con los
        # pesos propios (pt/onnx/ncnn) es un no-op porque solo hay esa clase;
        # con 'hef' es ademas una red de seguridad: si el .hef trajera mas
        # clases, evita que r.plot() reviente con un indice fuera de 'names'.
        results = model.predict(
            source=fuente,
            imgsz=640,       # igual que el imgsz de entrenamiento; a menos resolucion se pierde detalle y confunde mas las clases
            conf=obtener_confianza(),  # umbral al arrancar la sesion; se actualiza en cada frame dentro del bucle (set_confidence)
            classes=[0],     # solo 'persona' (indice 0); ver comentario de arriba
            vid_stride=VID_STRIDE,    # 1 = analiza todos los frames; subirlo va mas rapido pero puede saltarse personas que pasan rapido
            stream=True,
            verbose=False,
            augment=args.augment     # test-time augmentation: analiza cada frame varias veces (flips/escalas) y combina resultados, mas preciso pero mas lento
        )

        writer = None
        writer_raw = None
        # Fecha de realización del vídeo: se fija una vez por sesión de
        # grabación (no una sola vez para todo el proceso), para que cada
        # start_recording genere su propio fichero con su propio nombre.
        # Con zona horaria (igual que el resto de timestamps del script) para
        # que timestamp_inicio del resumen sea comparable a timestamp_fin.
        FECHA_INICIO = datetime.now().astimezone()
        os.makedirs(VIDEOS_EN_CURSO, exist_ok=True)
        fecha_str = FECHA_INICIO.strftime('%Y%m%d_%H%M%S')
        # Nombre fijado ya aquí (no al escribir el primer frame): así el
        # resumen de la sesión tiene un nombre de fichero aunque no se haya
        # detectado/escrito ni un solo frame (sesión cortada casi al instante).
        nombre_video = f"{DRON_ID or 'sindron'}_{fuente_nombre}_{fecha_str}.mp4"
        output_path = os.path.join(VIDEOS_DIR, nombre_video)            # destino final (completo)
        output_en_curso = os.path.join(VIDEOS_EN_CURSO, nombre_video)   # mientras se graba
        # Copia sin detecciones (--raw): mismo nombre con sufijo _raw y mismo
        # flujo en_curso/ -> VIDEOS_DIR que el vídeo anotado.
        nombre_video_raw = f"{os.path.splitext(nombre_video)[0]}_raw.mp4"
        output_raw_path = os.path.join(VIDEOS_DIR, nombre_video_raw)
        output_raw_en_curso = os.path.join(VIDEOS_EN_CURSO, nombre_video_raw)

        # ------------------- BENCHMARK: contadores de esta sesión -------------------
        tiempos_inferencia = []
        t_anterior = time.perf_counter()
        alertas_sesion = 0        # total de alertas MQTT encoladas en esta sesión
        confianzas_sesion = []    # confianza de cada una de esas alertas
        # Foto de la alerta con MAYOR confianza de la sesión: se envía en el
        # resumen para que el servidor use su miniatura como imagen del vídeo.
        foto_representativa = None
        confianza_maxima = None

        for r in results:
            # Umbral en caliente: model.predict() solo lee 'conf' al arrancar,
            # pero el postproceso relee predictor.args.conf en cada frame (en
            # Ultralytics y en runtime_hef.py). Se actualiza aquí, antes de
            # pedir el siguiente frame, para que un set_confidence recibido a
            # media grabación se aplique desde el frame siguiente.
            model.predictor.args.conf = obtener_confianza()

            # Medir tiempo del fotograma procesado
            t_ahora = time.perf_counter()
            latencia_frame = t_ahora - t_anterior
            tiempos_inferencia.append(latencia_frame)
            t_anterior = t_ahora

            # Mostrar FPS instantáneos en la consola durante la ejecución
            ms_actual = latencia_frame * 1000
            fps_actual = 1.0 / latencia_frame if latencia_frame > 0 else 0
            print(f"\r[Benchmark] Frame {len(tiempos_inferencia)}: {ms_actual:.1f} ms ({fps_actual:.1f} FPS)", end="", flush=True)

            # Frame tal cual sale de la cámara, sin cajas ni etiquetas. Se
            # escribe en el vídeo raw ANTES de r.plot() (r.plot() dibuja sobre
            # una copia, pero así no dependemos de ello).
            frame_raw = r.orig_img

            if writer is None:
                # r.plot() devuelve un frame del mismo tamaño que orig_img, así
                # que ambos writers comparten resolución, códec y FPS.
                h, w = frame_raw.shape[:2]
                writer = cv2.VideoWriter(
                    output_en_curso,
                    cv2.VideoWriter_fourcc(*'mp4v'),
                    FPS_VIDEO,
                    (w, h),
                )
                # El raw se abre a la vez que el anotado (y se cierra a la vez, abajo).
                if args.raw:
                    writer_raw = cv2.VideoWriter(
                        output_raw_en_curso,
                        cv2.VideoWriter_fourcc(*'mp4v'),
                        FPS_VIDEO,
                        (w, h),
                    )
                # Arrancamos el emisor en directo a la vez que el vídeo local,
                # una sola vez por sesión (cuando se crea el writer).
                if STREAM_ON:
                    emisor = EmisorRTSP(STREAM_HOST, STREAM_USER, STREAM_PASS,
                                        STREAM_PATH, STREAM_ANCHO, STREAM_ALTO, STREAM_FPS)
                    emisor.abrir()
            if writer_raw is not None:
                writer_raw.write(frame_raw)

            annotated_frame = r.plot()
            writer.write(annotated_frame)

            # Copia ligera en directo hacia MediaMTX (si falla, no afecta a lo demás).
            if STREAM_ON and emisor is not None:
                emisor.enviar(annotated_frame)

            # ----- NUEVO: detección -> alerta MQTT (con anti-spam) -----
            if MQTT_ON and r.boxes is not None and len(r.boxes) > 0:
                ahora = time.monotonic()
                if ahora - ultimo_envio >= anti_spam_actual:
                    ts = datetime.now().astimezone()
                    pos = leer_posicion()
                    if pos:
                        _posicion_fresca(pos)
                    else:
                        print("\n  (aviso: sin posicion_actual.json; la alerta va SIN coordenadas. "
                              "¿Está vuelo.py en marcha en la misma carpeta?)")
                    foto_nombre = guardar_foto(annotated_frame, pos, ts)
                    foto_bytes = _tamano_fichero(os.path.join(FOTOS_DIR, foto_nombre))
                    # Este frame es el nº len(tiempos_inferencia) escrito en el
                    # vídeo (ya se ha hecho writer.write), así que su posición
                    # en el fichero es (n - 1) / FPS_VIDEO segundos.
                    tiempo_en_video_s = round((len(tiempos_inferencia) - 1) / FPS_VIDEO, 2)
                    confs = procesar_detecciones(r, foto_nombre, ts, pos,
                                                 foto_bytes, tiempo_en_video_s, nombre_video)
                    alertas_sesion += len(confs)
                    confianzas_sesion.extend(confs)
                    if confs and (confianza_maxima is None or max(confs) > confianza_maxima):
                        confianza_maxima = max(confs)
                        foto_representativa = foto_nombre
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

                # Mostrar ventana interactiva (q para salir del todo)
                cv2.imshow('Detector de personas', vista)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    parar_por_usuario = True
                    break

            # stop_recording recibido a media sesión: cortamos aquí: el resto
            # se trata igual que si el vídeo/cámara hubiera terminado.
            if MQTT_ON and not grabando:
                break

        # ---------------- Cierre de ESTA sesión de grabación ----------------
        if writer is not None:
            writer.release()
            writer = None
            # Vídeo ya cerrado y completo: se mueve a VIDEOS_DIR ANTES de medir
            # su tamaño y de publicar el resumen.
            _publicar_video(output_en_curso, output_path)
        if writer_raw is not None:
            writer_raw.release()
            writer_raw = None
            _publicar_video(output_raw_en_curso, output_raw_path)
        # Cerramos el emisor en directo de esta sesión (si estaba activo).
        if STREAM_ON and emisor is not None:
            emisor.cerrar()
            emisor = None
        cv2.destroyAllWindows()

        # Liberar la cámara (o el fichero) de ESTA sesión. Al cortar el
        # generador de model.predict() con 'break' (stop_recording, 'q'),
        # Ultralytics no cierra el cv2.VideoCapture por su cuenta: se queda
        # abierto y "ocupado" por este mismo proceso, y el siguiente
        # start_recording falla al no poder reabrir la cámara. Solo el
        # loader de camara en vivo (LoadStreams) tiene close(); el de
        # fichero de video no lo necesita (termina solo al agotarse).
        dataset = getattr(model.predictor, 'dataset', None) if model.predictor is not None else None
        if dataset is not None and hasattr(dataset, 'close'):
            dataset.close()

        FIN_SESION = datetime.now().astimezone()

        # ------------------- BENCHMARK: resumen de esta sesión -------------------
        if len(tiempos_inferencia) > 1:
            # Descartamos el primer frame porque suele tardar más (warmup)
            tiempos_validos = tiempos_inferencia[1:]
            media_s = sum(tiempos_validos) / len(tiempos_validos)
            media_ms = media_s * 1000
            fps_medio = 1.0 / media_s if media_s > 0 else 0

            print(f"\n\n{'='*42}")
            print(f" RESUMEN DE RENDIMIENTO ({args.runtime.upper()})")
            print(f"{'='*42}")
            print(f" Total frames procesados : {len(tiempos_inferencia)}")
            print(f" Latencia media por frame: {media_ms:.2f} ms")
            print(f" Rendimiento medio       : {fps_medio:.2f} FPS")
            print(f"{'='*42}\n")

        # ----- NUEVO: resumen de la sesión -> MQTT (dronsar/.../video/resumen) -----
        # Se manda siempre que termine una sesión con MQTT activo (aunque
        # haya sido de 0 o 1 frame), para que el panel se entere de que la
        # grabación se ha cerrado y con qué estadísticas.
        if MQTT_ON:
            latencias_ms = [t * 1000 for t in tiempos_inferencia[1:]]  # sin el frame de warmup, igual que arriba
            publicar_resumen_video(
                fichero_video=nombre_video,
                inicio=FECHA_INICIO,
                fin=FIN_SESION,
                frames_totales=len(tiempos_inferencia),
                latencias_ms=latencias_ms,
                alertas_total=alertas_sesion,
                confianzas_alertas=confianzas_sesion,
                # El writer ya se ha cerrado arriba: el .mp4 está completo en disco.
                tamano_bytes=_tamano_fichero(output_path),
                foto_representativa=foto_representativa,
                confianza_maxima=confianza_maxima,
                duracion_fichero_s=(round(len(tiempos_inferencia) / FPS_VIDEO, 1)
                                    if tiempos_inferencia else None),
            )

        if not ESPERA_COMANDO:
            # Video de fichero, o sin MQTT: una sola pasada, como siempre.
            break
        # Camara + MQTT: seguimos vivos, volvemos arriba a esperar el
        # siguiente start_recording (grabando ya esta a False aqui).

except KeyboardInterrupt:
    # Con --camera el analisis no tiene fin natural (a diferencia de un
    # fichero de video); Ctrl+C es la forma normal de pararlo.
    print("\nDetenido por el usuario.")

finally:
    # Cierre ordenado (también si se interrumpe con Ctrl+C a media sesión).
    if writer is not None:
        writer.release()
        # Cierre ordenado a mitad de sesión: el vídeo queda completo, se publica.
        _publicar_video(output_en_curso, output_path)
    if writer_raw is not None:
        writer_raw.release()
        _publicar_video(output_raw_en_curso, output_raw_path)
    if STREAM_ON and emisor is not None:
        emisor.cerrar()
    cv2.destroyAllWindows()
    if MQTT_ON:
        reenviar()                 # último intento de vaciar el buffer
        client.loop_stop()
        client.disconnect()
        db.close()
