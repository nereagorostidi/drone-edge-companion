# drone-edge-companion
Nodo edge del sistema UAV de Búsqueda y Rescate (SAR) del TFG.
Corre en una Raspberry Pi 5 con sensor BME680 (temperatura, humedad, presión y calidad del aire). Publica la telemetría ambiental por MQTT hacia el broker en AWS EC2, con buffer local SQLite para no perder datos ante caídas de red, y recibe por MQTT los comandos de vuelo que traduce a MAVLink para el autopiloto.
 
## Estructura del repositorio
- `sensor.py` — Lectura del BME680, buffer SQLite local y publicación de telemetría por MQTT (*store-and-forward*).
- `receptor.py` — Suscriptor MQTT de comandos: traduce cada orden recibida a MAVLink y la envía al autopiloto.
- `sensor-sar.service` — Servicio systemd del script de telemetría.
- `requirements.txt` — Dependencias de Python.
- `.env.example` — Plantilla de variables de entorno (copiar a `.env`).
- `.gitignore` — Excluye el entorno virtual, el `.env` y la base de datos local.
## Flujo de telemetría
Cada lectura se guarda primero en `buffer.db` marcada como no enviada y solo después se publica por MQTT con QoS 1. La fila se marca como enviada únicamente al recibir la confirmación del broker, de modo que una pérdida temporal de conectividad no supone pérdida de datos:
 
```
BME680 → sensor.py (Raspberry Pi) → buffer SQLite
   → MQTT (Mosquitto) → mqtt_to_influx.py (EC2) → InfluxDB
```
 
## Flujo de comandos
El panel de control envía órdenes al dron (armar, desarmar…) a través de esta cadena:
 
```
Botón (control.gorostiditfg.com) → HTTP → api.py (EC2)
   → MQTT (Mosquitto) → receptor.py (Raspberry Pi)
   → MAVLink → autopiloto
```
 
`receptor.py` se suscribe al topic `dronsar/<DRONE_ID>/comandos` y traduce cada comando recibido mediante un diccionario de acciones MAVLink. No se comunica directamente con `api.py`: ambos son clientes independientes del broker. Tampoco da órdenes a Mission Planner, sino al autopiloto; Mission Planner, conectado al mismo autopiloto, refleja lo que ocurre.
 
## Requisitos
- Raspberry Pi 5 con Raspberry Pi OS (Bookworm) y fuente oficial de 27 W (5 V / 5 A)
- Sensor BME680 conectado por I2C (dirección `0x76`)
- Python 3.10+
- Broker MQTT accesible (Mosquitto en el EC2)
- Entorno virtual en `/home/nerea/bme680-env`
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
| `EC2_IP` | `sensor.py` | IP elástica o dominio del servidor con el broker |
| `MQTT_BROKER` | `receptor.py` | IP elástica o dominio del servidor con el broker |
| `MQTT_PORT` | ambos | Puerto del broker (1883) |
| `MQTT_TOPIC` | `sensor.py` | Topic de publicación de telemetría |
| `DRONE_ID` | `receptor.py` | Identificador del dron; forma el topic de comandos |
| `MAVLINK_CONN` | `receptor.py` | Cadena de conexión MAVLink (`udpin:0.0.0.0:14550` contra el SITL) |
 
El topic debe coincidir de forma exacta con el del suscriptor en el EC2: un topic mal escrito no genera ningún error, los mensajes se publican y se descartan silenciosamente.
 
## Ejecución manual
Ambos scripts se ejecutan en paralelo y son independientes entre sí.
 
Telemetría (el flag `-i` define el intervalo entre lecturas en segundos):
```bash
source /home/nerea/bme680-env/bin/activate
python sensor.py -i 30
```
 
Receptor de comandos:
```bash
source /home/nerea/bme680-env/bin/activate
python receptor.py
```
 
Al arrancar, `receptor.py` debe mostrar el autopiloto conectado y la suscripción al topic. Si se queda esperando en la conexión MAVLink, revisar que Mission Planner esté reenviando por UDP a la IP actual de la Pi (SerialOutput → UDP Outbound → puerto 14550, con *Write access* activado).
 
Para comprobar desde otra máquina que la telemetría llega al broker:
```bash
mosquitto_sub -h <MQTT_BROKER> -t '<MQTT_TOPIC>' -v
```
 
## Ejecución como servicio
```bash
sudo cp sensor-sar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sensor-sar.service
```
Ver logs en tiempo real:
```bash
journalctl -u sensor-sar.service -f
```
 
## Autora
Nerea Gorostidi García — TFG UC3M
