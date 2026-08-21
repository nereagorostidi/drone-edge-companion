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
muestre o no el preview, en:
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
    python3 deteccion.py vuelo1.mp4 --runtime onnx   # carga weights/best.onnx (mas ligero, requiere conversion/exportar_onnx.py antes)
    python3 deteccion.py vuelo1.mp4 --runtime onnx-int8  # carga weights/best.int8.onnx (cuantizado, requiere conversion/cuantizar_onnx.py antes)
    python3 deteccion.py vuelo1.mp4 --runtime ncnn   # carga weights/best_ncnn_model/ (requiere conversion/exportar_ncnn.py antes)
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
parser.add_argument('--preview', type=_str2bool, default=False,
                     help='Mostrar la ventana de vista previa (true/false). El video de salida en output/ se genera igual, se muestre o no el preview')
parser.add_argument('--anti-spam', type=float, default=5.0,
                     help='Segundos minimos entre envios de alertas por MQTT, para no saturar el topic con la misma persona en frames seguidos')
parser.add_argument('--overlay', type=_str2bool, default=True,
                     help='Añadir a la foto guardada (results/fotos/) la posicion del dron y la fecha/hora de la deteccion (true/false). No afecta al video anotado ni al preview')
parser.add_argument('--runtime', choices=('pt', 'onnx', 'onnx-int8', 'ncnn'), default='pt',
                     help="Motor de inferencia: 'pt' carga weights/best.pt via PyTorch (el de siempre); "
                          "'onnx' carga weights/best.onnx via ONNX Runtime (mas ligero/rapido, requiere "
                          "haberlo generado antes con conversion/exportar_onnx.py); 'onnx-int8' carga "
                          "weights/best.int8.onnx, la version cuantizada (aun mas ligera, requiere "
                          "haberla generado antes con conversion/cuantizar_onnx.py; revisa la precision antes de "
                          "usarla en vuelo real); 'ncnn' carga la carpeta weights/best_ncnn_model/ "
                          "(motor optimizado para CPUs ARM como la de la Raspberry Pi, requiere haberla "
                          "generado antes con conversion/exportar_ncnn.py)")
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
# Cargar VUESTRO cerebro entrenado. El motor de inferencia (--runtime)
# es independiente del modelo base: solo decide que pesos se cargan y
# con que backend (PyTorch, ONNX Runtime o NCNN). Todos son un unico
# fichero salvo 'ncnn', que exporta una carpeta.
NOMBRE_PESOS = {'pt': 'best.pt', 'onnx': 'best.onnx', 'onnx-int8': 'best.int8.onnx',
                'ncnn': 'best_ncnn_model'}[args.runtime]
GENERAR_CON = {'pt': None, 'onnx': 'conversion/exportar_onnx.py', 'onnx-int8': 'conversion/cuantizar_onnx.py',
               'ncnn': 'conversion/exportar_ncnn.py'}[args.runtime]
WEIGHTS_PATH = os.path.join(BASE_DIR, 'weights', NOMBRE_PESOS)
existe = os.path.isdir(WEIGHTS_PATH) if args.runtime == 'ncnn' else os.path.isfile(WEIGHTS_PATH)
if not existe:
    raise SystemExit(
        f'No encuentro "{WEIGHTS_PATH}". '
        + (f'Genera ese fichero antes con {GENERAR_CON} o usa --runtime pt.'
           if GENERAR_CON else 'Falta weights/best.pt.'))
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

    def on_connect(client, userdata, flags, reason_code, properties):
        """Al conectar (o reconectar), nos suscribimos al topic de configuración."""
        client.subscribe(CONFIG_TOPIC, qos=1)
        print(f"Suscrito a '{CONFIG_TOPIC}'")

    def on_message(client, userdata, msg):
        """Aplica un comando de configuración recibido del panel de control.

        Formato del payload (lo publica api.py, ver COMANDOS_CONFIG):
            {"command": "...", "params": {...}, "drone_id": "...",
             "command_id": "...", "timestamp": "..."}

        Comandos soportados en este topic:
            set_video_throttle  {"throttle_ms": N}  Cambia el anti-spam de alertas
                                                     (llega en ms; se guarda en s).
            start_recording      {}                 Arranca la grabación/detección
                                                     (ver ESPERA_COMANDO más abajo).
            stop_recording        {}                 La detiene, sin cerrar el script.
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
    for (cx, cy, w, h), conf in zip(cajas, confianzas):
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
            "timestamp": ts_iso,
        }
        guardar_deteccion(ts_iso, json.dumps(mensaje))
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
                            alertas_total, confianzas_alertas):
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
            "duracion_segundos": round((fin - inicio).total_seconds(), 1),
            "frames_totales": frames_totales,
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

ultimo_envio = 0.0     # marca de tiempo del último envío, para el anti-spam (persiste entre sesiones)
parar_por_usuario = False   # se puso a True al pulsar 'q' en el preview: para todo, no solo la sesion
writer = None   # definido aqui para que el finally pueda cerrarlo aunque no haya arrancado ninguna sesion


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
        # Fecha de realización del vídeo: se fija una vez por sesión de
        # grabación (no una sola vez para todo el proceso), para que cada
        # start_recording genere su propio fichero con su propio nombre.
        # Con zona horaria (igual que el resto de timestamps del script) para
        # que timestamp_inicio del resumen sea comparable a timestamp_fin.
        FECHA_INICIO = datetime.now().astimezone()
        videos_dir = os.path.join('results', 'videos')
        os.makedirs(videos_dir, exist_ok=True)
        fecha_str = FECHA_INICIO.strftime('%Y%m%d_%H%M%S')
        # Nombre fijado ya aquí (no al escribir el primer frame): así el
        # resumen de la sesión tiene un nombre de fichero aunque no se haya
        # detectado/escrito ni un solo frame (sesión cortada casi al instante).
        nombre_video = f"{DRON_ID or 'sindron'}_{fuente_nombre}_{fecha_str}.mp4"
        output_path = os.path.join(videos_dir, nombre_video)

        # ------------------- BENCHMARK: contadores de esta sesión -------------------
        tiempos_inferencia = []
        t_anterior = time.perf_counter()
        alertas_sesion = 0        # total de alertas MQTT encoladas en esta sesión
        confianzas_sesion = []    # confianza de cada una de esas alertas

        for r in results:
            # Medir tiempo del fotograma procesado
            t_ahora = time.perf_counter()
            latencia_frame = t_ahora - t_anterior
            tiempos_inferencia.append(latencia_frame)
            t_anterior = t_ahora

            # Mostrar FPS instantáneos en la consola durante la ejecución
            ms_actual = latencia_frame * 1000
            fps_actual = 1.0 / latencia_frame if latencia_frame > 0 else 0
            print(f"\r[Benchmark] Frame {len(tiempos_inferencia)}: {ms_actual:.1f} ms ({fps_actual:.1f} FPS)", end="", flush=True)

            annotated_frame = r.plot()

            if writer is None:
                h, w = annotated_frame.shape[:2]
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
                if ahora - ultimo_envio >= anti_spam_actual:
                    ts = datetime.now().astimezone()
                    pos = leer_posicion()
                    if pos:
                        _posicion_fresca(pos)
                    else:
                        print("\n  (aviso: sin posicion_actual.json; la alerta va SIN coordenadas. "
                              "¿Está vuelo.py en marcha en la misma carpeta?)")
                    foto_nombre = guardar_foto(annotated_frame, pos, ts)
                    confs = procesar_detecciones(r, foto_nombre, ts, pos)
                    alertas_sesion += len(confs)
                    confianzas_sesion.extend(confs)
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
    cv2.destroyAllWindows()
    if MQTT_ON:
        reenviar()                 # último intento de vaciar el buffer
        client.loop_stop()
        client.disconnect()
        db.close()