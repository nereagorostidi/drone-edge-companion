# drone-edge-companion
Nodo edge del sistema UAV de Búsqueda y Rescate (SAR) del TFG.
Corre en una Raspberry Pi 5 y agrupa varios procesos independientes ("dominios"), cada uno con su propia captura, buffer local SQLite y publicación MQTT hacia el broker en AWS EC2, más un receptor de comandos que traduce órdenes MQTT a MAVLink para el autopiloto.

## Dominios
Cada dominio es un script independiente que sigue la misma plantilla store-and-forward (captura → buffer SQLite → reenvío MQTT oportunista) y publica en su propio topic `dronsar/{dron_id}/{dominio}`:

| Dominio | Script | Publica en | Descripción |
|---|---|---|---|
| `ambiental` | `sensor.py` | `dronsar/{dron_id}/ambiental` | Lectura del BME680 (temperatura, humedad, presión) |
| `sistema` | `sistema.py` | `dronsar/{dron_id}/sistema` | Estado del nodo edge: CPU, temperatura, RAM, disco, throttling, uptime |
| `vuelo` | `vuelo.py` | `dronsar/{dron_id}/vuelo` | Telemetría de vuelo (posición, actitud, batería, GPS, modo). Además escribe `posicion_actual.json` con la última posición conocida |
| `deteccion` | `deteccion.py` | `dronsar/{dron_id}/deteccion` | Detección de personas con YOLO sobre un vídeo o cámara en vivo (`--camera`); adjunta a cada alerta la posición del dron leída de `posicion_actual.json` y el nombre de la foto guardada de esa detección |

Aparte de los dominios anteriores, `receptor.py` no publica telemetría: se suscribe al topic de comandos y los traduce a MAVLink (ver [Flujo de comandos](#flujo-de-comandos)).

## Estructura del repositorio
- `sensor.py` — Dominio `ambiental`: lectura del BME680 y publicación por MQTT.
- `sistema.py` — Dominio `sistema`: métricas del propio nodo edge (CPU, RAM, disco, throttling, uptime).
- `vuelo.py` — Dominio `vuelo`: telemetría de vuelo (de momento con datos simulados, a la espera del Pixhawk) y escritura de `posicion_actual.json`.
- `deteccion.py` — Dominio `deteccion`: detección de personas (YOLO) sobre un vídeo o una cámara en vivo (`--camera`), con alerta MQTT georreferenciada.
- `streaming.py` — Clase `EmisorRTSP`, usada por `deteccion.py` (`--stream`) para emitir el vídeo anotado en directo hacia un servidor MediaMTX por RTSP, vía `ffmpeg`. Es un extra a prueba de fallos: si `ffmpeg` no está instalado, la red falla o MediaMTX no responde, se desactiva sola y la detección (vídeo local + alertas) sigue igual.
- `receptor.py` — Suscriptor MQTT de comandos: traduce cada orden recibida a MAVLink y la envía al autopiloto.
- `weights/` — Pesos del modelo YOLO entrenado: `best.pt` (PyTorch) y, opcionalmente, `best.onnx` (ONNX), `best.int8.onnx` (ONNX cuantizado) y `best_ncnn_model/` (NCNN), usados por `deteccion.py` según `--runtime`.
- `conversion/exportar_onnx.py` — Utilidad puntual para generar `weights/best.onnx` a partir de `weights/best.pt`; volver a ejecutarlo cada vez que haya un `best.pt` nuevo (reentrenamiento).
- `conversion/cuantizar_onnx.py` — Utilidad puntual para generar `weights/best.int8.onnx` (cuantización dinámica a INT8) a partir de `weights/best.onnx`; más ligero y rápido en CPU, a costa de algo de precisión. Volver a ejecutarlo cada vez que se regenere `best.onnx`.
- `conversion/exportar_ncnn.py` — Utilidad puntual para generar `weights/best_ncnn_model/` a partir de `weights/best.pt`; NCNN es un motor de inferencia optimizado para CPUs ARM (Raspberry Pi incluida), a veces más rápido que ONNX Runtime en ese hardware. Volver a ejecutarlo cada vez que haya un `best.pt` nuevo.
- `samples/` — Vídeos de prueba para `deteccion.py`.
- `results/videos/` — Vídeos anotados generados por `deteccion.py` (se crea automáticamente; excluida de git). Ruta por defecto, configurable con `VIDEOS_DIR` en el `.env`.
- `results/fotos/` — Fotogramas JPEG de cada alerta enviada por `deteccion.py` (se crea automáticamente; excluida de git). Ruta por defecto, configurable con `FOTOS_DIR` en el `.env`.
- `posicion_actual.json` — Última posición conocida del dron; la escribe `vuelo.py` y la lee `deteccion.py`. Se genera en tiempo de ejecución.
- `sensor-sar.service` — Servicio systemd del dominio `ambiental` (`sensor.py`).
- `sistema-sar.service` — Servicio systemd del dominio `sistema` (`sistema.py`).
- `vuelo-sar.service` — Servicio systemd del dominio `vuelo` (`vuelo.py`).
- `deteccion-sar.service` — Servicio systemd de la detección (`deteccion.py`); requiere editar la fuente (ruta de vídeo o `--camera`) antes de instalarlo.
- `receptor-sar.service` — Servicio systemd del receptor de comandos (`receptor.py`).
- `requirements.txt` — Dependencias de los 5 dominios (instala siempre todo junto, pensado para el nodo completo en la Pi).
- `.env.example` — Plantilla de variables de entorno (copiar a `.env`).
- `.gitignore` — Excluye el entorno virtual, el `.env`, las bases de datos locales (`*.db`) y las salidas generadas por `deteccion.py` (`results/`).

## Flujo de telemetría
Cada dominio guarda primero su lectura en su propio buffer SQLite (`ambiental.db`, `sistema.db`, `vuelo.db`, `deteccion.db`) marcada como no enviada, y solo después la publica por MQTT con QoS 1. La fila se marca como enviada únicamente al recibir la confirmación del broker, de modo que una pérdida temporal de conectividad no supone pérdida de datos:

```
BME680 → sensor.py ────────┐
psutil/vcgencmd → sistema.py ─┤
Pixhawk (simulado) → vuelo.py ─┼→ buffer SQLite (por dominio) → MQTT (Mosquitto) → EC2 → InfluxDB
YOLO → deteccion.py ────────┘
```

`vuelo.py` escribe además, en cada ciclo, `posicion_actual.json` con la última posición del dron (escritura atómica). `deteccion.py` lee ese fichero para adjuntar la posición aproximada de cada persona detectada a su alerta MQTT; si `vuelo.py` no está en marcha, la alerta se envía igualmente pero sin coordenadas.

`deteccion.py` genera además, por cada sesión de grabación (con un vídeo de fichero, una sesión = toda la ejecución; con `--camera` y MQTT, una sesión = de `start_recording` a `stop_recording`), un vídeo anotado en `results/videos/` (nombrado con el ID del dron, la fuente y la fecha de inicio de esa sesión), y por cada alerta enviada (respetando el `--anti-spam`) guarda una foto del frame en `results/fotos/`. El nombre de esa foto viaja también dentro del JSON de la alerta MQTT, en el campo `foto`, para poder relacionar cada alerta con su imagen.

Al terminar cada sesión de grabación (con `--mqtt true`), `deteccion.py` publica además un resumen en `dronsar/{dron_id}/video/resumen` — a diferencia de las alertas de detección, este mensaje se publica directo (sin pasar por el buffer SQLite de store-and-forward):
```json
{
  "evento": "sesion_completada",
  "dron_id": "dron01",
  "video": {"fichero": "dron01_camara0_20260820_231500.mp4", "duracion_segundos": 142.5, "frames_totales": 1280},
  "rendimiento": {"runtime": "onnx", "fps_medio": 18.4, "latencia_media_ms": 54.3, "latencia_p95_ms": 68.1, "vid_stride": 6},
  "detecciones": {"total_alertas_emitidas": 4, "confianza_media": 0.82},
  "timestamp_inicio": "2026-08-20T23:15:00.000000+02:00",
  "timestamp_fin": "2026-08-20T23:17:22.500000+02:00",
  "timestamp": "2026-08-20T23:17:22.500000+02:00"
}
```
`timestamp` va siempre igual a `timestamp_fin`, para que el puente hacia InfluxDB use la hora exacta de cierre de la sesión en vez de la hora de llegada del mensaje. Se manda siempre que se cierra una sesión (`stop_recording`, `q` en el preview, o fin natural de un vídeo de fichero), aunque la sesión haya durado 0 frames.

## Flujo de comandos
El panel de control envía órdenes al dron (armar, desarmar…) a través de esta cadena:

```
Botón (control.gorostiditfg.com) → HTTP → api.py (EC2)
   → MQTT (Mosquitto) → receptor.py (Raspberry Pi)
   → MAVLink → autopiloto
```

`receptor.py` se suscribe al topic `dronsar/<DRONE_ID>/comandos` y traduce cada comando recibido mediante un diccionario de acciones MAVLink. No se comunica directamente con `api.py`: ambos son clientes independientes del broker. Tampoco da órdenes a Mission Planner, sino al autopiloto; Mission Planner, conectado al mismo autopiloto, refleja lo que ocurre.

> Nota: `receptor.py` usa sus propias variables de entorno (`DRONE_ID`, `MQTT_BROKER`) en vez de las del resto de dominios (`DRON_ID`, `EC2_HOST`), aunque todos comparten ya el mismo prefijo de topic `dronsar/...`. Son procesos independientes técnicamente, pero **`DRONE_ID` y `DRON_ID` deben tener el mismo valor** en el `.env`: el panel de control ahora deja elegir el dron desde un desplegable (`dron-01` / `dron-02`, validado en `api.py` contra `DRONES_VALIDOS`), y ese valor va literalmente en el topic. Si `DRONE_ID` no coincide con `DRON_ID` (p. ej. uno en `dron-02` y el otro con el valor por defecto `dron-01`), los comandos de vuelo se publican en un topic que `receptor.py` no escucha y se pierden sin ningún error visible.

## Flujo de configuración
Además de comandos de vuelo, el panel de control puede reconfigurar en caliente tres dominios, con el mismo esquema `dronsar/...` y el mismo formato de payload (`command`/`params`/`drone_id`/`command_id`/`timestamp`) que usa `receptor.py` para comandos — lo publica `api.py` (ver `COMANDOS_CONFIG`). Cada script se suscribe a su propio topic de configuración y aplica el cambio sin reiniciar:

| Dominio | Script | Se suscribe a | `command` | `params` | Efecto |
|---|---|---|---|---|---|
| `sensor` | `sensor.py` | `dronsar/{dron_id}/sensor/config` | `set_sensor_interval` | `{"interval_seconds": N}` | Cambia el intervalo de captura del BME680 (segundos) |
| `deteccion` | `deteccion.py` | `dronsar/{dron_id}/deteccion/config` | `set_video_throttle` | `{"throttle_ms": N}` | Cambia el anti-spam de alertas de vídeo (el valor llega en milisegundos y se convierte a segundos) |
| `deteccion` | `deteccion.py` | `dronsar/{dron_id}/deteccion/config` | `start_recording` | `{}` | Arranca la sesión de grabación/detección (solo tiene efecto con `--camera` y `--mqtt true`; ver [Arranque y parada remota](#arranque-y-parada-remota-de-deteccionpy)) |
| `deteccion` | `deteccion.py` | `dronsar/{dron_id}/deteccion/config` | `stop_recording` | `{}` | Detiene la sesión en curso (guarda el vídeo) sin cerrar el script, que vuelve a esperar el siguiente `start_recording` |
| `sistema` | `sistema.py` | `dronsar/{dron_id}/sistema/config` | `shutdown` | `{}` | Apaga la Raspberry Pi (`sudo shutdown -h now`) |

## Requisitos
- Raspberry Pi 5 con Raspberry Pi OS (Bookworm) y fuente oficial de 27 W (5 V / 5 A)
- Sensor BME680 conectado por I2C (dirección `0x76`), para el dominio `ambiental`
- Python 3.10+
- Broker MQTT accesible (Mosquitto en el EC2)
- Entorno virtual en `/home/nerea/drone-edge-companion/venv`
- Para `vuelo.py` (y el servicio `vuelo-sar.service`, que arranca sin `--fake`): Pixhawk conectado, o Mission Planner en modo SITL reenviando por MAVLink al puerto de `MAVLINK_CONN_VUELO`. Sin eso disponible al arrancar, el proceso se queda esperando el heartbeat MAVLink indefinidamente (no falla, simplemente no arranca del todo); para simular sin autopiloto, usar `--fake` (ver [Ejecución manual](#ejecución-manual))
- Para `deteccion.py`: pesos del modelo en `weights/best.pt` (con `--runtime onnx`, además `weights/best.onnx`, generado con `conversion/exportar_onnx.py`; con `--runtime onnx-int8`, además `weights/best.int8.onnx`, generado con `conversion/cuantizar_onnx.py`; con `--runtime ncnn`, además `weights/best_ncnn_model/`, generado con `conversion/exportar_ncnn.py`)
- Para `deteccion.py --camera`: cámara expuesta como dispositivo V4L2 (`/dev/video0`); con la cámara oficial de la Pi puede requerir `sudo modprobe bcm2835-v4l2` o la capa de compatibilidad de libcamera
- Para `deteccion.py --stream` (activado por defecto): `ffmpeg` instalado como binario de sistema (`sudo apt install ffmpeg` en la Pi) — no está en `requirements.txt` porque no es un paquete de Python. Además, `STREAM_HOST`/`STREAM_USER`/`STREAM_PASS` en el `.env` (ver [Variables de entorno](#variables-de-entorno)) y un servidor MediaMTX accesible en esa dirección, puerto `8554`. Sin `ffmpeg` o sin esas variables, el streaming se desactiva solo y avisa por consola — no impide que `deteccion.py` funcione
## Conexionado del sensor
| Pin del sensor | Pin de la Raspberry Pi | Nº de pin |
|---|---|---|
| VCC | 3.3 V | 1 |
| GND | GND | 6 |
| SDA | GPIO2 (SDA) | 3 |
| SCL | GPIO3 (SCL) | 5 |
| SDO | GND | 9 |
| CS | 3.3 V | 17 |

`CS` a 3.3 V fuerza el modo I2C; si se deja al aire, el sensor puede quedar en modo SPI y no aparecer.
`SDO` a GND fija la dirección en `0x76`; si se deja suelto, el sensor responde en `0x77`.

## Instalación
Activar la interfaz I2C con `sudo raspi-config` (Interface Options → I2C → Yes) y verificar que el sensor se detecta:
```bash
sudo apt install -y i2c-tools
i2cdetect -y 1        # debe aparecer 76 en la cuadrícula
```

Clonar el repositorio y preparar el entorno (dentro de la propia carpeta del proyecto, `venv/`, en la ruta que esperan los servicios systemd):
```bash
git clone git@github.com:<tu-usuario>/drone-edge-companion.git
cd drone-edge-companion
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt install -y ffmpeg   # necesario para deteccion.py --stream (streaming en directo)
cp .env.example .env   # edita con tus credenciales
```

## Variables de entorno
| Variable | Usado por | Descripción |
|---|---|---|
| `DRON_ID` | `sensor.py`, `sistema.py`, `vuelo.py`, `deteccion.py` | Identificador del dron; forma el topic `dronsar/{dron_id}/{dominio}` |
| `EC2_HOST` | `sensor.py`, `sistema.py`, `vuelo.py`, `deteccion.py` | IP elástica o dominio del servidor con el broker |
| `MQTT_PORT` | todos | Puerto del broker (1883) |
| `LOTE` | todos los dominios | Filas del buffer reenviadas por ciclo (por defecto 50) |
| `BUFFER_SENSOR` | `sensor.py` | Ruta del buffer SQLite del dominio `ambiental` (por defecto `./ambiental.db`) |
| `BUFFER_SISTEMA` | `sistema.py` | Ruta del buffer SQLite del dominio `sistema` (por defecto `./sistema.db`) |
| `BUFFER_VUELO` | `vuelo.py` | Ruta del buffer SQLite del dominio `vuelo` (por defecto `./vuelo.db`) |
| `BUFFER_DB` | `deteccion.py` | Ruta del buffer SQLite del dominio `deteccion` (por defecto `./deteccion.db`) |
| `POS_FILE` | `vuelo.py`, `deteccion.py` | Ruta del fichero de posición compartido (por defecto `./posicion_actual.json`) |
| `VIDEOS_DIR` | `deteccion.py` | Carpeta donde se guardan los vídeos anotados (por defecto `results/videos`, dentro del repo). Cambiarla no requiere tocar código — útil para apuntar a almacenamiento aparte (p. ej. `/media/...`) |
| `FOTOS_DIR` | `deteccion.py` | Carpeta donde se guardan las fotos de cada alerta (por defecto `results/fotos`, dentro del repo). Mismo caso de uso que `VIDEOS_DIR` |
| `MQTT_TOPIC` | — | Legado del dominio único original; los dominios actuales construyen el topic a partir de `DRON_ID` |
| `MQTT_BROKER` | `receptor.py` | IP elástica o dominio del servidor con el broker; debe ser el mismo valor que `EC2_HOST` |
| `DRONE_ID` | `receptor.py` | Identificador del dron; forma el topic de comandos `dronsar/{drone_id}/comandos`. Debe ser el mismo valor que `DRON_ID`, y estar entre los `DRONES_VALIDOS` que acepta `api.py` (p. ej. `dron-01`, `dron-02`) |
| `MAVLINK_CONN` | `receptor.py` | Cadena de conexión MAVLink (`udpin:0.0.0.0:14550` contra el SITL) |
| `MAVLINK_CONN_VUELO` | `vuelo.py` | Cadena de conexión MAVLink para leer telemetría (por defecto `udpin:0.0.0.0:14552`; debe ser un puerto distinto al de `MAVLINK_CONN`). No se usa con `--fake` |
| `STREAM_HOST` | `deteccion.py` (`--stream`) | IP o dominio del servidor MediaMTX. Obligatoria para que el streaming se active (si falta, se desactiva solo con un aviso) |
| `STREAM_USER` / `STREAM_PASS` | `deteccion.py` (`--stream`) | Credenciales RTSP contra MediaMTX. Obligatorias igual que `STREAM_HOST` |
| `STREAM_PATH` | `deteccion.py` (`--stream`) | Nombre del stream en MediaMTX (por defecto `dron_live`); forma la URL `rtsp://.../{STREAM_PATH}` |
| `STREAM_ANCHO` / `STREAM_ALTO` | `deteccion.py` (`--stream`) | Resolución del vídeo emitido en directo (por defecto 640×360) — menor que la del vídeo local, para no saturar el enlace |
| `STREAM_FPS` | `deteccion.py` (`--stream`) | FPS del streaming en directo (por defecto 12) |

El topic debe coincidir de forma exacta con el del suscriptor en el EC2: un topic mal escrito no genera ningún error, los mensajes se publican y se descartan silenciosamente. Esto incluye `DRONE_ID`/`DRON_ID`: si no coinciden entre sí (y con el valor elegido en el desplegable del panel de control), el mensaje se publica pero ningún proceso de este Pi lo recibe.

## Ejecución manual
Los scripts de dominio y `receptor.py` se ejecutan en paralelo y son independientes entre sí.

Telemetría ambiental (el flag `-i` define el intervalo entre lecturas en segundos):
```bash
source /home/nerea/drone-edge-companion/venv/bin/activate
python sensor.py -i 30
```

Estado del sistema:
```bash
source /home/nerea/drone-edge-companion/venv/bin/activate
python sistema.py -i 10
```

Telemetría de vuelo (por defecto lee MAVLink real; con `--fake`, datos simulados):
```bash
source /home/nerea/drone-edge-companion/venv/bin/activate
python vuelo.py -i 1              # MAVLink real (Pixhawk o Mission Planner en SITL)
python vuelo.py -i 1 --fake       # datos simulados, sin autopiloto
```
Para leer del SITL, Mission Planner necesita reenviar por UDP a la Pi en un puerto propio para `vuelo.py` (`SerialOutput → UDP Outbound → puerto 14552`, con *Write access* activado), además del que ya usa `receptor.py` — son dos flujos UDP independientes, uno por proceso.

Detección de personas sobre un vídeo (requiere `weights/best.pt`):
```bash
source /home/nerea/drone-edge-companion/venv/bin/activate
python deteccion.py samples/vuelo1.mp4                    # MQTT activado, SIN ventana de preview (por defecto)
python deteccion.py samples/vuelo1.mp4 --mqtt false       # solo detección + vídeo anotado, sin MQTT
python deteccion.py samples/vuelo1.mp4 --preview true     # con ventana de vista previa; el vídeo en results/videos/ se genera igual
python deteccion.py -h                                    # todas las opciones (--conf, --vid-stride, --anti-spam...)
```
El vídeo anotado de cada sesión se guarda en `{VIDEOS_DIR}/{DRON_ID}_{fuente}_{fecha}.mp4` — por defecto `results/videos/`, configurable con `VIDEOS_DIR` en el `.env` (ver [Variables de entorno](#variables-de-entorno)) sin tocar código (con un vídeo de fichero, o con `--camera` y `--mqtt false`, se genera siempre, de principio a fin de la ejecución; con `--camera` y `--mqtt true` ver [Arranque y parada remota](#arranque-y-parada-remota-de-deteccionpy)). Cada alerta enviada (con `--mqtt true`, respetando el `--anti-spam`) guarda además una foto del frame en `{FOTOS_DIR}/{DRON_ID}_{fecha}.jpg` (por defecto `results/fotos/`, configurable con `FOTOS_DIR`), cuyo nombre viaja en el campo `foto` del JSON de la alerta.

Para que las alertas lleven posición, `vuelo.py` debe estar en marcha en la misma carpeta (comparten `posicion_actual.json`); si no lo está, la alerta se envía igualmente pero sin coordenadas.

Por defecto (`--overlay true`) esa foto lleva superpuestas las coordenadas del dron y la fecha/hora de la detección; con `--overlay false` se guarda el frame tal cual. Solo afecta a la foto — el vídeo anotado y la ventana de preview nunca llevan esta marca:
```bash
python deteccion.py samples/vuelo1.mp4 --overlay false   # fotos sin coordenadas/fecha superpuestas
```

Por defecto (`--stream true`) se emite además el vídeo anotado en directo hacia MediaMTX (`streaming.py`, vía `ffmpeg`, RTSP), en paralelo a la grabación local — más ligero (por defecto 640×360 a 12 FPS, configurable con `STREAM_ANCHO`/`STREAM_ALTO`/`STREAM_FPS`) que el vídeo guardado en `results/videos/`. Es un extra a prueba de fallos: si falta `ffmpeg`, faltan `STREAM_HOST`/`STREAM_USER`/`STREAM_PASS` en el `.env`, o se cae la conexión a mitad de sesión, se desactiva solo con un aviso por consola y la detección (vídeo local + alertas MQTT) sigue sin cortarse:
```bash
python deteccion.py samples/vuelo1.mp4 --stream false   # sin streaming en directo, solo vídeo local
```

Por defecto (`--runtime pt`) carga `weights/best.pt` con PyTorch. Con `--runtime onnx` carga en su lugar `weights/best.onnx` (más ligero y rápido de cargar), que hay que generar antes con `conversion/exportar_onnx.py`; con `--runtime onnx-int8` carga `weights/best.int8.onnx`, la versión cuantizada a INT8 (aún más ligera y rápida en CPU, a costa de algo de precisión), que hay que generar antes con `conversion/cuantizar_onnx.py`; con `--runtime ncnn` carga la carpeta `weights/best_ncnn_model/`, un motor optimizado para CPUs ARM (Raspberry Pi incluida, a veces más rápido que ONNX Runtime en ese hardware), que hay que generar antes con `conversion/exportar_ncnn.py`:
```bash
python conversion/exportar_onnx.py                          # genera weights/best.onnx a partir de weights/best.pt
python deteccion.py samples/vuelo1.mp4 --runtime onnx

python conversion/cuantizar_onnx.py                          # genera weights/best.int8.onnx a partir de weights/best.onnx
python deteccion.py samples/vuelo1.mp4 --runtime onnx-int8

python conversion/exportar_ncnn.py                           # genera weights/best_ncnn_model/ a partir de weights/best.pt
python deteccion.py samples/vuelo1.mp4 --runtime ncnn
```
Cada vez que haya un `weights/best.pt` nuevo (reentrenamiento), hay que volver a ejecutar el script de export correspondiente (`conversion/exportar_onnx.py`, `conversion/cuantizar_onnx.py` y/o `conversion/exportar_ncnn.py`) para regenerar los ficheros; el motor de ejecución (`--runtime`) es independiente del modelo base. Antes de usar `--runtime onnx-int8` en vuelo real, conviene comparar sus detecciones con las de `--runtime onnx` sobre el mismo vídeo, porque la cuantización dinámica no calibra con datos reales y puede perder algo de precisión.

En la Raspberry Pi, en vez de un vídeo grabado se puede analizar en directo desde la cámara con `--camera` (mutuamente excluyente con `video_path`):
```bash
python deteccion.py --camera 0                      # cámara por índice (la primera detectada); SIN ventana de preview (por defecto)
python deteccion.py --camera /dev/video0             # cámara por ruta de dispositivo V4L2
python deteccion.py --camera 0 --preview true        # en directo, con ventana (NO usar en systemd, no hay pantalla)
```
Con `--camera` la fuente no tiene fin natural (a diferencia de un fichero): el análisis sigue hasta pulsar `Ctrl+C` (o `q` en la ventana de preview si está activada, lo que además cierra el script del todo). Requiere que la cámara esté expuesta como dispositivo V4L2 (`ls /dev/video*`); con el módulo oficial de la Raspberry Pi puede hacer falta `sudo modprobe bcm2835-v4l2` (o la capa de compatibilidad de libcamera) para que aparezca como `/dev/video0`.

#### Arranque y parada remota de `deteccion.py`
Con `--camera` **y** `--mqtt true` (el caso real: el servicio systemd), `deteccion.py` no arranca solo al lanzarlo: se queda a la espera del comando `start_recording` en `dronsar/{dron_id}/deteccion/config` (el mismo topic que `set_video_throttle`, ver [Flujo de configuración](#flujo-de-configuración)). Mientras espera no hay preview, ni vídeo, ni detección — el proceso solo escucha MQTT:
```bash
python deteccion.py --camera 0
# -> "A la espera de 'start_recording' desde el panel (topic 'dronsar/dron-02/deteccion/config')..."
```
Al recibir `start_recording` arranca la sesión completa (vídeo anotado, preview si está activado, detección y alertas MQTT, y streaming en directo si `--stream` está activado); al recibir `stop_recording` la cierra —guardando el vídeo de esa sesión en `results/videos/` con su propio timestamp y cortando el streaming— **sin cerrar el script**, que vuelve a quedarse a la espera del siguiente `start_recording`. Se pueden encadenar tantas sesiones como se quiera sin reiniciar el proceso.

Con un fichero de vídeo, o con `--mqtt false`, no hay nada que esperar: `deteccion.py` arranca directo, como siempre (estos comandos no tienen efecto en ese caso).

Receptor de comandos:
```bash
source /home/nerea/drone-edge-companion/venv/bin/activate
python receptor.py
```

Al arrancar, `receptor.py` debe mostrar el autopiloto conectado y la suscripción al topic. Si se queda esperando en la conexión MAVLink, revisar que Mission Planner esté reenviando por UDP a la IP actual de la Pi (SerialOutput → UDP Outbound → puerto 14550, con *Write access* activado).

Para comprobar desde otra máquina que la telemetría de un dominio llega al broker:
```bash
mosquitto_sub -h <EC2_HOST> -t 'dronsar/<DRON_ID>/<dominio>' -v
```

## Ejecución como servicio
Cada proceso tiene ya su propio fichero `.service` en el repositorio, listo para instalar en `/etc/systemd/system/`:

| Servicio | Proceso | Restart |
|---|---|---|
| `sensor-sar.service` | `sensor.py` (dominio `ambiental`) | `always` — captura continua |
| `sistema-sar.service` | `sistema.py` (dominio `sistema`) | `always` — captura continua |
| `vuelo-sar.service` | `vuelo.py` (dominio `vuelo`) | `always` — captura continua |
| `deteccion-sar.service` | `deteccion.py` (dominio `deteccion`) | `always` — cámara por defecto: queda en marcha indefinidamente a la espera de `start_recording`/`stop_recording` |
| `receptor-sar.service` | `receptor.py` (comandos) | `on-failure` |

Instalación de cualquiera de los servicios de captura continua (`sensor-sar`, `sistema-sar`, `vuelo-sar`) o del receptor — mismo patrón para los cinco, cambiando el nombre del fichero:
```bash
sudo cp sensor-sar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sensor-sar.service
```
Con `vuelo-sar.service` en concreto, comprobar antes que el Pixhawk (o Mission Planner en SITL) ya está reenviando por MAVLink (ver [Requisitos](#requisitos)): el `ExecStart` no lleva `--fake`, así que sin eso disponible el servicio se queda esperando el heartbeat indefinidamente en vez de arrancar.

### Particularidad de `deteccion-sar.service`
A diferencia de los demás, `deteccion.py` no es un colector en bucle infinito sobre un fichero: recibe como fuente O bien una ruta de vídeo O bien una cámara en vivo (`--camera`, ver [Ejecución manual](#ejecución-manual)). `deteccion-sar.service` trae **la cámara (índice 0) como fuente por defecto** — es el caso normal de este servicio: siempre disponible, sin grabar nada hasta que el panel lo pida. Antes de instalar `deteccion-sar.service` conviene revisar:

1. El índice/ruta de la cámara en `ExecStart` (`--camera 0` por defecto): cambiarlo si la Pi tiene más de una cámara o el índice `0` no es el correcto (`ls /dev/video*` para comprobar). Si en su lugar quieres procesar un vídeo de fichero con este servicio (caso raro — pensado sobre todo para pruebas manuales por SSH, no para el despliegue), sustituye `--camera 0` por la ruta del vídeo y ten en cuenta que entonces `start_recording`/`stop_recording` no tienen efecto (arranca directo, ver [Arranque y parada remota](#arranque-y-parada-remota-de-deteccionpy)).
2. No añadir `--preview true`: un servicio systemd no tiene pantalla, así que la ventana de vista previa (`cv2.imshow`) fallaría si se activa. `--preview` es `false` por defecto, así que basta con no tocarlo. El vídeo anotado en `results/videos/` y las fotos en `results/fotos/` se generan igual, con o sin preview.
3. La política de reinicio: por defecto `Restart=always`, para que el servicio se recupere también de una caída limpia de la cámara (no solo de un fallo) — tiene sentido con la cámara como fuente permanente. Si en su lugar apuntas a un vídeo de fichero, probablemente quieras `Restart=on-failure` (que no lo relance si termina bien).
4. Comprobar `ffmpeg` y las variables `STREAM_*` si se quiere streaming en directo (`--stream` es `true` por defecto — ver [Requisitos](#requisitos)); si no interesa para este despliegue, añadir `--stream false` al `ExecStart` para no intentarlo en cada sesión.
5. `ExecStart` trae `--runtime ncnn` (a diferencia del `--runtime pt` por defecto de `deteccion.py` en manual, pensado para dev/depuración): NCNN es el motor más ligero/rápido para la CPU ARM de la Pi, ideal para producción. Requiere `weights/best_ncnn_model/` (ya viene commiteado en el repo — si algún día se reentrena, hay que regenerarlo con `conversion/exportar_ncnn.py` y volver a commitearlo, ver [Ejecución manual](#ejecución-manual)); sin él, el servicio falla al arrancar con un aviso claro (`No encuentro ".../weights/best_ncnn_model/"...`).

Con la cámara como fuente y `--mqtt true` (el caso por defecto de este servicio), el proceso arranca y se queda esperando el comando `start_recording` del panel de control — no analiza nada hasta que se lo mandan (ver [Arranque y parada remota](#arranque-y-parada-remota-de-deteccionpy)). Esto es intencional: el servicio puede estar `enable --now` de forma permanente sin grabar nada hasta que se necesite.

```bash
sudo cp deteccion-sar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deteccion-sar.service
```

Ver logs en tiempo real de cualquier servicio:
```bash
journalctl -u sensor-sar.service -f
journalctl -u sistema-sar.service -f
journalctl -u vuelo-sar.service -f
journalctl -u deteccion-sar.service -f
journalctl -u receptor-sar.service -f
```

## Autora
Nerea Gorostidi García — TFG UC3M
