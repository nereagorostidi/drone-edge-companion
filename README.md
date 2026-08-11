# drone-edge-companion
Nodo edge del sistema UAV de Búsqueda y Rescate (SAR) del TFG.
Corre en una Raspberry Pi 5 y agrupa varios procesos independientes ("dominios"), cada uno con su propia captura, buffer local SQLite y publicación MQTT hacia el broker en AWS EC2, más un receptor de comandos que traduce órdenes MQTT a MAVLink para el autopiloto.

## Dominios
Cada dominio es un script independiente que sigue la misma plantilla store-and-forward (captura → buffer SQLite → reenvío MQTT oportunista) y publica en su propio topic `sar/{dron_id}/{dominio}`:

| Dominio | Script | Publica en | Descripción |
|---|---|---|---|
| `ambiental` | `sensor.py` | `sar/{dron_id}/ambiental` | Lectura del BME680 (temperatura, humedad, presión) |
| `sistema` | `sistema.py` | `sar/{dron_id}/sistema` | Estado del nodo edge: CPU, temperatura, RAM, disco, throttling, uptime |
| `vuelo` | `vuelo.py` | `sar/{dron_id}/vuelo` | Telemetría de vuelo (posición, actitud, batería, GPS, modo). Además escribe `posicion_actual.json` con la última posición conocida |
| `deteccion` | `deteccion.py` | `sar/{dron_id}/deteccion` | Detección de personas con YOLO sobre un vídeo o cámara en vivo (`--camera`); adjunta a cada alerta la posición del dron leída de `posicion_actual.json` y el nombre de la foto guardada de esa detección |

Aparte de los dominios anteriores, `receptor.py` no publica telemetría: se suscribe al topic de comandos y los traduce a MAVLink (ver [Flujo de comandos](#flujo-de-comandos)).

## Estructura del repositorio
- `sensor.py` — Dominio `ambiental`: lectura del BME680 y publicación por MQTT.
- `sistema.py` — Dominio `sistema`: métricas del propio nodo edge (CPU, RAM, disco, throttling, uptime).
- `vuelo.py` — Dominio `vuelo`: telemetría de vuelo (de momento con datos simulados, a la espera del Pixhawk) y escritura de `posicion_actual.json`.
- `deteccion.py` — Dominio `deteccion`: detección de personas (YOLO) sobre un vídeo o una cámara en vivo (`--camera`), con alerta MQTT georreferenciada.
- `receptor.py` — Suscriptor MQTT de comandos: traduce cada orden recibida a MAVLink y la envía al autopiloto.
- `weights/` — Pesos del modelo YOLO entrenado (`best.pt`), usado por `deteccion.py`.
- `samples/` — Vídeos de prueba para `deteccion.py`.
- `results/videos/` — Vídeos anotados generados por `deteccion.py` (se crea automáticamente; excluida de git).
- `results/fotos/` — Fotogramas JPEG de cada alerta enviada por `deteccion.py` (se crea automáticamente; excluida de git).
- `posicion_actual.json` — Última posición conocida del dron; la escribe `vuelo.py` y la lee `deteccion.py`. Se genera en tiempo de ejecución.
- `sensor-sar.service` — Servicio systemd del dominio `ambiental` (`sensor.py`).
- `sistema-sar.service` — Servicio systemd del dominio `sistema` (`sistema.py`).
- `vuelo-sar.service` — Servicio systemd del dominio `vuelo` (`vuelo.py`).
- `deteccion-sar.service` — Servicio systemd de la detección (`deteccion.py`); requiere editar la fuente (ruta de vídeo o `--camera`) antes de instalarlo.
- `receptor-sar.service` — Servicio systemd del receptor de comandos (`receptor.py`).
- `requirements.txt` — Dependencias de Python.
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

`deteccion.py` genera además, por cada ejecución, un vídeo anotado en `results/videos/` (nombrado con el ID del dron, el vídeo de origen y la fecha), y por cada alerta enviada (respetando el `--anti-spam`) guarda una foto del frame en `results/fotos/`. El nombre de esa foto viaja también dentro del JSON de la alerta MQTT, en el campo `foto`, para poder relacionar cada alerta con su imagen.

## Flujo de comandos
El panel de control envía órdenes al dron (armar, desarmar…) a través de esta cadena:

```
Botón (control.gorostiditfg.com) → HTTP → api.py (EC2)
   → MQTT (Mosquitto) → receptor.py (Raspberry Pi)
   → MAVLink → autopiloto
```

`receptor.py` se suscribe al topic `dronsar/<DRONE_ID>/comandos` y traduce cada comando recibido mediante un diccionario de acciones MAVLink. No se comunica directamente con `api.py`: ambos son clientes independientes del broker. Tampoco da órdenes a Mission Planner, sino al autopiloto; Mission Planner, conectado al mismo autopiloto, refleja lo que ocurre.

> Nota: `receptor.py` usa sus propias variables de entorno (`DRONE_ID`, `MQTT_BROKER`) y un esquema de topic distinto (`dronsar/...`) al del resto de dominios (`DRON_ID`, `EC2_HOST`, `sar/...`). Son procesos independientes, así que no hace falta que coincidan, pero conviene mantener `DRONE_ID`/`DRON_ID` con el mismo valor en el `.env`.

## Requisitos
- Raspberry Pi 5 con Raspberry Pi OS (Bookworm) y fuente oficial de 27 W (5 V / 5 A)
- Sensor BME680 conectado por I2C (dirección `0x76`), para el dominio `ambiental`
- Python 3.10+
- Broker MQTT accesible (Mosquitto en el EC2)
- Entorno virtual en `/home/nerea/bme680-env`
- Para `deteccion.py`: pesos del modelo en `weights/best.pt` y las dependencias `opencv-python` y `ultralytics` (añadir a `requirements.txt` si no están instaladas)
- Para `deteccion.py --camera`: cámara expuesta como dispositivo V4L2 (`/dev/video0`); con la cámara oficial de la Pi puede requerir `sudo modprobe bcm2835-v4l2` o la capa de compatibilidad de libcamera
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

Clonar el repositorio y preparar el entorno (que vive **fuera** de la carpeta del proyecto, en la ruta que espera el servicio systemd):
```bash
git clone git@github.com:<tu-usuario>/drone-edge-companion.git
cd drone-edge-companion
python3 -m venv /home/nerea/bme680-env
source /home/nerea/bme680-env/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edita con tus credenciales
```

## Variables de entorno
| Variable | Usado por | Descripción |
|---|---|---|
| `DRON_ID` | `sensor.py`, `sistema.py`, `vuelo.py`, `deteccion.py` | Identificador del dron; forma el topic `sar/{dron_id}/{dominio}` |
| `EC2_HOST` | `sensor.py`, `sistema.py`, `vuelo.py`, `deteccion.py` | IP elástica o dominio del servidor con el broker |
| `MQTT_PORT` | todos | Puerto del broker (1883) |
| `LOTE` | todos los dominios | Filas del buffer reenviadas por ciclo (por defecto 50) |
| `BUFFER_SENSOR` | `sensor.py` | Ruta del buffer SQLite del dominio `ambiental` (por defecto `./ambiental.db`) |
| `BUFFER_SISTEMA` | `sistema.py` | Ruta del buffer SQLite del dominio `sistema` (por defecto `./sistema.db`) |
| `BUFFER_VUELO` | `vuelo.py` | Ruta del buffer SQLite del dominio `vuelo` (por defecto `./vuelo.db`) |
| `BUFFER_DB` | `deteccion.py` | Ruta del buffer SQLite del dominio `deteccion` (por defecto `./deteccion.db`) |
| `POS_FILE` | `vuelo.py`, `deteccion.py` | Ruta del fichero de posición compartido (por defecto `./posicion_actual.json`) |
| `MQTT_TOPIC` | — | Legado del dominio único original; los dominios actuales construyen el topic a partir de `DRON_ID` |
| `MQTT_BROKER` | `receptor.py` | IP elástica o dominio del servidor con el broker |
| `DRONE_ID` | `receptor.py` | Identificador del dron; forma el topic de comandos `dronsar/{drone_id}/comandos` |
| `MAVLINK_CONN` | `receptor.py` | Cadena de conexión MAVLink (`udpin:0.0.0.0:14550` contra el SITL) |

El topic debe coincidir de forma exacta con el del suscriptor en el EC2: un topic mal escrito no genera ningún error, los mensajes se publican y se descartan silenciosamente.

## Ejecución manual
Los scripts de dominio y `receptor.py` se ejecutan en paralelo y son independientes entre sí.

Telemetría ambiental (el flag `-i` define el intervalo entre lecturas en segundos):
```bash
source /home/nerea/bme680-env/bin/activate
python sensor.py -i 30
```

Estado del sistema:
```bash
source /home/nerea/bme680-env/bin/activate
python sistema.py -i 10
```

Telemetría de vuelo (datos simulados hasta que el Pixhawk esté montado):
```bash
source /home/nerea/bme680-env/bin/activate
python vuelo.py -i 1
```

Detección de personas sobre un vídeo (requiere `weights/best.pt`):
```bash
source /home/nerea/bme680-env/bin/activate
python deteccion.py samples/vuelo1.mp4                    # MQTT y preview activados (por defecto)
python deteccion.py samples/vuelo1.mp4 --mqtt false       # solo detección + vídeo anotado, sin MQTT
python deteccion.py samples/vuelo1.mp4 --preview false    # sin ventana, el vídeo en results/videos/ se genera igual
python deteccion.py -h                                    # todas las opciones (--conf, --vid-stride, --anti-spam...)
```
El vídeo anotado se guarda siempre en `results/videos/{DRON_ID}_{fuente}_{fecha}.mp4`. Cada alerta enviada (con `--mqtt true`, respetando el `--anti-spam`) guarda además una foto del frame en `results/fotos/{DRON_ID}_{fecha}.jpg`, cuyo nombre viaja en el campo `foto` del JSON de la alerta.

Para que las alertas lleven posición, `vuelo.py` debe estar en marcha en la misma carpeta (comparten `posicion_actual.json`); si no lo está, la alerta se envía igualmente pero sin coordenadas.

Por defecto (`--overlay true`) esa foto lleva superpuestas las coordenadas del dron y la fecha/hora de la detección; con `--overlay false` se guarda el frame tal cual. Solo afecta a la foto — el vídeo anotado y la ventana de preview nunca llevan esta marca:
```bash
python deteccion.py samples/vuelo1.mp4 --overlay false   # fotos sin coordenadas/fecha superpuestas
```

En la Raspberry Pi, en vez de un vídeo grabado se puede analizar en directo desde la cámara con `--camera` (mutuamente excluyente con `video_path`):
```bash
python deteccion.py --camera 0                      # cámara por índice (la primera detectada)
python deteccion.py --camera /dev/video0             # cámara por ruta de dispositivo V4L2
python deteccion.py --camera 0 --preview false       # en directo, sin ventana (para systemd)
```
Con `--camera` la fuente no tiene fin natural (a diferencia de un fichero): el análisis sigue hasta pulsar `Ctrl+C` (o `q` en la ventana de preview si está activada). Requiere que la cámara esté expuesta como dispositivo V4L2 (`ls /dev/video*`); con el módulo oficial de la Raspberry Pi puede hacer falta `sudo modprobe bcm2835-v4l2` (o la capa de compatibilidad de libcamera) para que aparezca como `/dev/video0`.

Receptor de comandos:
```bash
source /home/nerea/bme680-env/bin/activate
python receptor.py
```

Al arrancar, `receptor.py` debe mostrar el autopiloto conectado y la suscripción al topic. Si se queda esperando en la conexión MAVLink, revisar que Mission Planner esté reenviando por UDP a la IP actual de la Pi (SerialOutput → UDP Outbound → puerto 14550, con *Write access* activado).

Para comprobar desde otra máquina que la telemetría de un dominio llega al broker:
```bash
mosquitto_sub -h <EC2_HOST> -t 'sar/<DRON_ID>/<dominio>' -v
```

## Ejecución como servicio
Cada proceso tiene ya su propio fichero `.service` en el repositorio, listo para instalar en `/etc/systemd/system/`:

| Servicio | Proceso | Restart |
|---|---|---|
| `sensor-sar.service` | `sensor.py` (dominio `ambiental`) | `always` — captura continua |
| `sistema-sar.service` | `sistema.py` (dominio `sistema`) | `always` — captura continua |
| `vuelo-sar.service` | `vuelo.py` (dominio `vuelo`) | `always` — captura continua |
| `deteccion-sar.service` | `deteccion.py` (dominio `deteccion`) | `on-failure` — procesa un vídeo concreto, no un flujo continuo |
| `receptor-sar.service` | `receptor.py` (comandos) | `on-failure` |

Instalación de cualquiera de los servicios de captura continua (`sensor-sar`, `sistema-sar`, `vuelo-sar`) o del receptor — mismo patrón para los cinco, cambiando el nombre del fichero:
```bash
sudo cp sensor-sar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sensor-sar.service
```

### Particularidad de `deteccion-sar.service`
A diferencia de los demás, `deteccion.py` no es un colector en bucle infinito sobre un fichero: recibe como fuente O bien una ruta de vídeo O bien una cámara en vivo (`--camera`, ver [Ejecución manual](#ejecución-manual)). Antes de instalar `deteccion-sar.service` hay que:

1. Editar la línea `ExecStart` del fichero: dejar la ruta de un vídeo (por defecto trae el placeholder `samples/vuelo1.mp4`) o sustituirla por `--camera 0` (u otro índice/ruta de dispositivo) para analizar en directo desde la cámara de la Pi.
2. Mantener `--preview false`: un servicio systemd no tiene pantalla, así que la ventana de vista previa (`cv2.imshow`) fallaría si se deja activada. El vídeo anotado en `results/videos/` y las fotos en `results/fotos/` se generan igualmente sin preview.
3. Decidir la política de reinicio según la fuente: con un vídeo, `Restart=on-failure` (por defecto) no lo vuelve a lanzar si termina bien; con `--camera`, el proceso no termina solo (sigue en directo), así que `on-failure` solo lo reinicia si falla — tiene sentido dejarlo así o cambiarlo a `always` si se quiere que se recupere también de una caída limpia de la cámara.

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
