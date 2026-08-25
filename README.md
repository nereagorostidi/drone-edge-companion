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

Aparte de los dominios anteriores, `receptor.py` no publica telemetría: se suscribe al topic de comandos y los traduce a MAVLink (ver [Flujo de comandos](docs/flujos.md#flujo-de-comandos)).

## Documentación adicional
Este README cubre lo esencial para arrancar y operar el nodo edge. Para temas más específicos:
- [docs/instalacion-hardware.md](docs/instalacion-hardware.md) — cableado del sensor BME680 y, si se va a volar con el Pixhawk real, la conexión serie por TELEM3 (requisito solo para producción, no para SITL con Mission Planner).
- [docs/flujos.md](docs/flujos.md) — cómo circulan los datos: flujo de telemetría, flujo de comandos y flujo de configuración en caliente.
- [docs/mavlink.md](docs/mavlink.md) — conexión real contra el Pixhawk por TELEM3, por qué hace falta `mavlink-router`, y los scripts de prueba de la conexión serie.
- [docs/servicios.md](docs/servicios.md) — instalación de cada proceso como servicio systemd.
- [docs/video.md](docs/video.md) — `deteccion.py`: opciones de línea de comandos, motores de ejecución (YOLO/ONNX/NCNN) y generación de pesos, streaming en directo, arranque/parada remota.

## Estructura del repositorio
- `sensor.py` — Dominio `ambiental`: lectura del BME680 y publicación por MQTT.
- `sistema.py` — Dominio `sistema`: métricas del propio nodo edge (CPU, RAM, disco, throttling, uptime).
- `vuelo.py` — Dominio `vuelo`: telemetría de vuelo (de momento con datos simulados, a la espera del Pixhawk) y escritura de `posicion_actual.json`.
- `deteccion.py` — Dominio `deteccion`: detección de personas (YOLO) sobre un vídeo o una cámara en vivo (`--camera`), con alerta MQTT georreferenciada.
- `streaming.py` — Clase `EmisorRTSP`, usada por `deteccion.py` (`--stream`) para emitir el vídeo anotado en directo hacia un servidor MediaMTX por RTSP, vía `ffmpeg`. Es un extra a prueba de fallos: si `ffmpeg` no está instalado, la red falla o MediaMTX no responde, se desactiva sola y la detección (vídeo local + alertas) sigue igual.
- `receptor.py` — Suscriptor MQTT de comandos: traduce cada orden recibida a MAVLink y la envía al autopiloto.
- `mision01.py` — Script suelto de prueba (sin MQTT, sin dominios): se conecta directo a Mission Planner en SITL, sube una misión de 4 waypoints sobre el campo de Galapagar, arma, despega en `GUIDED` y la ejecuta en `AUTO`. Sirve para comprobar la comunicación Pi ↔ Mission Planner por MAVLink de forma aislada, sin el resto del sistema — no forma parte del nodo edge en producción (ver [Comprobación aislada de MAVLink con `mision01.py`](#comprobación-aislada-de-mavlink-con-mision01py)).
- `test-serial.py` — Script suelto de prueba: loopback UART en la propia Raspberry Pi (TX puenteado con RX en los pines 8 y 10), sin ningún cable a la Pixhawk. Aísla si el problema está en la Pi o en el cableado/Pixhawk (ver [Scripts de prueba de la conexión serie](docs/mavlink.md#scripts-de-prueba-de-la-conexión-serie)).
- `test-mavlink.py` — Script suelto de prueba: heartbeat MAVLink real contra la Pixhawk por TELEM3, sin MQTT ni dominios.
- `test-arm-serial.py` — Script suelto de prueba: ciclo completo de armado/desarmado con confirmación de seguridad, captura de motivos de rechazo (STATUSTEXT) y opción de forzar el armado; funciona por serie directo o vía `mavlink-router`.
- `test-estado.py` — Script suelto de prueba: panel en vivo de batería, GPS y RC (incluye los mensajes de pre-arm reales del autopiloto); funciona por serie directo o vía `mavlink-router`.
- `weights/` — Pesos del modelo YOLO entrenado: `best.pt` (PyTorch) y, opcionalmente, `best.onnx` (ONNX), `best.int8.onnx` (ONNX cuantizado) y `best_ncnn_model/` (NCNN), usados por `deteccion.py` según `--runtime` (ver [docs/video.md](docs/video.md)).
- `conversion/exportar_onnx.py`, `conversion/cuantizar_onnx.py`, `conversion/exportar_ncnn.py` — Utilidades puntuales para generar los distintos formatos de `weights/` a partir de `best.pt`; volver a ejecutarlas cada vez que haya un `best.pt` nuevo (reentrenamiento) — ver [docs/video.md](docs/video.md).
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

## Requisitos
- Raspberry Pi 5 con Raspberry Pi OS (Bookworm) y fuente oficial de 27 W (5 V / 5 A)
- Sensor BME680 conectado por I2C (dirección `0x76`), para el dominio `ambiental`
- Python 3.10+
- Broker MQTT accesible (Mosquitto en el EC2)
- Entorno virtual en `/home/nerea/drone-edge-companion/venv`
- Para `vuelo.py` (y el servicio `vuelo-sar.service`, que arranca sin `--fake`): Pixhawk conectado, o Mission Planner en modo SITL reenviando por MAVLink al puerto de `MAVLINK_CONN_VUELO`. Sin eso disponible al arrancar, el proceso se queda esperando el heartbeat MAVLink indefinidamente (no falla, simplemente no arranca del todo); para simular sin autopiloto, usar `--fake` (ver [Ejecución manual](#ejecución-manual))
- Para conectar contra el Pixhawk real por TELEM3 (`MAVLINK_MODE=real`, ver [docs/mavlink.md](docs/mavlink.md)): `mavlink-router` instalado y corriendo como servicio en la Pi. Es obligatorio en este proyecto porque `receptor.py` y `vuelo.py` necesitan hablar con la Pixhawk a la vez, y el puerto serie de TELEM3 solo lo puede tener abierto un proceso — `mavlink-router` es el que lo abre y lo reparte a ambos. No hace falta en modo SITL (`MAVLINK_MODE=sitl`, por defecto), donde Mission Planner reenvía por red a cada uno por su propio puerto UDP
- Para `deteccion.py`: pesos del modelo en `weights/best.pt` (con `--runtime onnx`, además `weights/best.onnx`, generado con `conversion/exportar_onnx.py`; con `--runtime onnx-int8`, además `weights/best.int8.onnx`, generado con `conversion/cuantizar_onnx.py`; con `--runtime ncnn`, además `weights/best_ncnn_model/`, generado con `conversion/exportar_ncnn.py`)
- Para `deteccion.py --camera`: cámara expuesta como dispositivo V4L2 (`/dev/video0`); con la cámara oficial de la Pi puede requerir `sudo modprobe bcm2835-v4l2` o la capa de compatibilidad de libcamera
- Para `deteccion.py --stream` (activado por defecto): `ffmpeg` instalado como binario de sistema (`sudo apt install ffmpeg` en la Pi) — no está en `requirements.txt` porque no es un paquete de Python. Además, `STREAM_HOST`/`STREAM_USER`/`STREAM_PASS` en el `.env` (ver [Variables de entorno](#variables-de-entorno)) y un servidor MediaMTX accesible en esa dirección, puerto `8554`. Sin `ffmpeg` o sin esas variables, el streaming se desactiva solo y avisa por consola — no impide que `deteccion.py` funcione
## Conexionado del sensor y de la Pixhawk
Cableado del BME680 y, si se va a trabajar con hardware real, del Pixhawk por TELEM3 — ver [docs/instalacion-hardware.md](docs/instalacion-hardware.md).

## Instalación
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
| `DRON_ID` | `sensor.py`, `sistema.py`, `vuelo.py`, `deteccion.py`, `receptor.py` | Identificador del dron; forma tanto los topics de telemetría (`dronsar/{dron_id}/{dominio}`) como el de comandos (`dronsar/{dron_id}/comandos`). Debe coincidir con el valor elegido en el desplegable del panel de control y estar entre los `DRONES_VALIDOS` que acepta `api.py` (p. ej. `dron-01`, `dron-02`) |
| `EC2_HOST` | `sensor.py`, `sistema.py`, `vuelo.py`, `deteccion.py`, `receptor.py` | IP elástica o dominio del servidor con el broker |
| `MQTT_PORT` | todos | Puerto del broker (1883) |
| `LOTE` | todos los dominios | Filas del buffer reenviadas por ciclo (por defecto 50) |
| `BUFFER_SENSOR` | `sensor.py` | Ruta del buffer SQLite del dominio `ambiental` (por defecto `./ambiental.db`) |
| `BUFFER_SISTEMA` | `sistema.py` | Ruta del buffer SQLite del dominio `sistema` (por defecto `./sistema.db`) |
| `BUFFER_VUELO` | `vuelo.py` | Ruta del buffer SQLite del dominio `vuelo` (por defecto `./vuelo.db`) |
| `BUFFER_DB` | `deteccion.py` | Ruta del buffer SQLite del dominio `deteccion` (por defecto `./deteccion.db`) |
| `POS_FILE` | `vuelo.py`, `deteccion.py` | Ruta del fichero de posición compartido (por defecto `./posicion_actual.json`) |
| `VIDEOS_DIR` | `deteccion.py` | Carpeta donde se guardan los vídeos anotados (por defecto `results/videos`, dentro del repo). Cambiarla no requiere tocar código — útil para apuntar a almacenamiento aparte (p. ej. `/media/...`) |
| `FOTOS_DIR` | `deteccion.py` | Carpeta donde se guardan las fotos de cada alerta (por defecto `results/fotos`, dentro del repo). Mismo caso de uso que `VIDEOS_DIR` |
| `MAVLINK_MODE` | `receptor.py`, `vuelo.py` | `sitl` (por defecto) o `real`. Selecciona qué par de variables de conexión de abajo se usa — ver [docs/mavlink.md](docs/mavlink.md) |
| `MAVLINK_CONN` | `receptor.py` | Cadena de conexión MAVLink en modo `sitl` (por defecto `udpin:127.0.0.1:14550`; debe ser un puerto distinto al de `MAVLINK_CONN_VUELO`) |
| `MAVLINK_CONN_VUELO` | `vuelo.py` | Cadena de conexión MAVLink en modo `sitl` (por defecto `udpin:0.0.0.0:14552`; debe ser un puerto distinto al de `MAVLINK_CONN`). No se usa con `--fake` |
| `MAVLINK_CONN_REAL_RECEPTOR` | `receptor.py` | Cadena de conexión MAVLink en modo `real`, contra el endpoint de `mavlink-router` (por defecto `udpin:127.0.0.1:14550`) |
| `MAVLINK_CONN_REAL_VUELO` | `vuelo.py` | Cadena de conexión MAVLink en modo `real`, contra el endpoint de `mavlink-router` (por defecto `udpin:127.0.0.1:14551`). No se usa con `--fake` |
| `MAVLINK_SYSID` | `receptor.py`, `vuelo.py` | SYSID propio de ambos procesos (por defecto `1`, el mismo que el vehículo) |
| `MAVLINK_COMPID_RECEPTOR` | `receptor.py` | COMPID propio de `receptor.py` (por defecto `191`, `MAV_COMP_ID_ONBOARD_COMPUTER`) — distinto del de `vuelo.py` para que ambos sean identificables por separado en los logs del autopiloto |
| `MAVLINK_COMPID_VUELO` | `vuelo.py` | COMPID propio de `vuelo.py` (por defecto `192`, `MAV_COMP_ID_ONBOARD_COMPUTER2`) |
| `STREAM_HOST` | `deteccion.py` (`--stream`) | IP o dominio del servidor MediaMTX. Obligatoria para que el streaming se active (si falta, se desactiva solo con un aviso) |
| `STREAM_USER` / `STREAM_PASS` | `deteccion.py` (`--stream`) | Credenciales RTSP contra MediaMTX. Obligatorias igual que `STREAM_HOST` |
| `STREAM_PATH` | `deteccion.py` (`--stream`) | Nombre del stream en MediaMTX (por defecto `dron_live`); forma la URL `rtsp://.../{STREAM_PATH}` |
| `STREAM_ANCHO` / `STREAM_ALTO` | `deteccion.py` (`--stream`) | Resolución del vídeo emitido en directo (por defecto 640×360) — menor que la del vídeo local, para no saturar el enlace |
| `STREAM_FPS` | `deteccion.py` (`--stream`) | FPS del streaming en directo (por defecto 12) |

El topic debe coincidir de forma exacta con el del suscriptor en el EC2: un topic mal escrito no genera ningún error, los mensajes se publican y se descartan silenciosamente. Esto incluye `DRON_ID`: si no coincide con el valor elegido en el desplegable del panel de control, el mensaje se publica pero ningún proceso de este Pi lo recibe.

## Ejecución manual
Los scripts de dominio y `receptor.py` se ejecutan en paralelo y son independientes entre sí. En un despliegue normal, cada uno corre como servicio systemd (ver [docs/servicios.md](docs/servicios.md)) y no hace falta lanzarlo a mano — la ejecución manual de esta sección es sobre todo para pruebas puntuales, depuración, o la primera comprobación antes de instalar el servicio.

> ⚠️ Cuidado con dejar `sensor.py` (dominio `ambiental`) corriendo de forma manual y continuada en vez de como servicio: escribe en su buffer SQLite local a cada lectura, y un apagado brusco de la Raspberry Pi (corte de corriente, desconexión sin `shutdown`) mientras se está escribiendo en la tarjeta SD puede corromper la tarjeta o el propio fichero de la base de datos. El servicio systemd no elimina este riesgo de fondo, pero al menos evita el caso más frecuente de apagado brusco: dejar una sesión SSH con el script a medio arrancar y cerrar la terminal sin más. Para desconexiones limpias, usar siempre `sudo shutdown -h now` en vez de cortar la alimentación directamente.

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
python vuelo.py -i 1              # MAVLink real (Pixhawk o Mission Planner en SITL, según MAVLINK_MODE)
python vuelo.py -i 1 --fake       # datos simulados, sin autopiloto
```
En modo `sitl` (por defecto), Mission Planner necesita reenviar por UDP a la Pi en un puerto propio para `vuelo.py` (`SerialOutput → UDP Outbound → puerto 14552`, con *Write access* activado), además del que ya usa `receptor.py` — son dos flujos UDP independientes, uno por proceso. En modo `real` (`MAVLINK_MODE=real`), en su lugar hace falta `mavlink-router` corriendo — ver [docs/mavlink.md](docs/mavlink.md).

Detección de personas sobre vídeo o cámara en vivo (opciones de línea de comandos, motores YOLO/ONNX/NCNN, streaming, arranque/parada remota): ver [docs/video.md](docs/video.md).

Receptor de comandos:
```bash
source /home/nerea/drone-edge-companion/venv/bin/activate
python receptor.py
```

Al arrancar, `receptor.py` debe mostrar el autopiloto conectado y la suscripción al topic. Si se queda esperando en la conexión MAVLink, revisar que Mission Planner esté reenviando por UDP a la IP actual de la Pi (SerialOutput → UDP Outbound → puerto 14550, con *Write access* activado) en modo `sitl`, o que `mavlink-router.service` esté activo en modo `real`.

#### Comprobación aislada de MAVLink con `mision01.py`
`mision01.py` es un script de prueba suelto, sin MQTT ni ningún dominio — solo para comprobar que la comunicación Pi ↔ Mission Planner por MAVLink funciona, antes de meter el resto del sistema por medio. Sube una misión fija de 4 waypoints sobre el campo de Galapagar, arma, despega en `GUIDED` a 30 m y la ejecuta en `AUTO`; al terminar el dron vuelve al punto de despegue (RTL).

En Mission Planner: `Ctrl+F` (pantalla de opciones de simulación) → `MAVLink` → `SerialOutput` → `UDP` → `Outbound` → IP de la Pi, puerto `14550` (con *Write access* activado) — la misma configuración de reenvío que usa `receptor.py`.

```bash
source /home/nerea/drone-edge-companion/venv/bin/activate
python mision01.py
```
Con `vuelo-sar.service` en concreto, comprobar antes que el Pixhawk (o Mission Planner en SITL) ya está reenviando por MAVLink (ver [Requisitos](#requisitos)): el `ExecStart` no lleva `--fake`, así que sin eso disponible el servicio se queda esperando el heartbeat indefinidamente en vez de arrancar.

Ojo: usa el **mismo puerto (14550)** que `receptor.py` — no los ejecutes a la vez, competirían por el mismo socket UDP.

Para comprobar desde otra máquina que la telemetría de un dominio llega al broker:
```bash
mosquitto_sub -h <EC2_HOST> -t 'dronsar/<DRON_ID>/<dominio>' -v
```

## Autora
Nerea Gorostidi García — TFG UC3M
