# Instalación de hardware: sensor BME680 y conexión con la Pixhawk

Cableado físico y configuración de interfaces de la Raspberry Pi necesarios antes de poder ejecutar los dominios. Se divide en dos partes independientes: el sensor ambiental (necesario siempre) y la conexión serie con la Pixhawk (necesaria solo para volar con hardware real por TELEM3, no para trabajar en SITL con Mission Planner).

## Conexionado del sensor BME680
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

Activar la interfaz I2C con `sudo raspi-config` (Interface Options → I2C → Yes) y verificar que el sensor se detecta:
```bash
sudo apt install -y i2c-tools
i2cdetect -y 1        # debe aparecer 76 en la cuadrícula
```

## Conexión serie con la Pixhawk (TELEM3) — solo para producción
Necesario únicamente si se va a volar con el Pixhawk real (`MAVLINK_MODE=real`). Para trabajar en SITL con Mission Planner (`MAVLINK_MODE=sitl`, el caso por defecto) no hace falta nada de esta sección — Mission Planner reenvía por red, sin ningún cable ni interfaz que activar en la Pi.

1. **Activar el puerto serie en `raspi-config`** (Interface Options → Serial Port): responder "No" a la consola serie (para no dejar un login shell escuchando en ese UART) y "Sí" al hardware serial habilitado.
2. **Cablear TELEM3** de la Pixhawk a la Raspberry Pi (TX↔RX cruzados + GND; en esta Raspberry Pi concreta, el dispositivo correcto es `/dev/ttyAMA0` — **no** `/dev/serial0`, que en esta placa apunta a un UART distinto sin cablear).
3. **Activar MAVLink2 en el `SERIALx` correspondiente a TELEM3 en ArduPilot** (en un Pixhawk 6X, TELEM3 = `SERIAL5`), con Mission Planner conectado por USB.

### Instalación de `mavlink-router`
`mavlink-router` es un proceso ligero que actúa como repartidor de mensajes MAVLink: es el único que abre el puerto serie de TELEM3, y reenvía ese tráfico hacia varios puertos UDP locales, uno por proceso. Hace falta porque un puerto serie solo lo puede tener abierto un proceso a la vez, y aquí `receptor.py` y `vuelo.py` necesitan hablar con la Pixhawk a la vez — sin `mavlink-router`, solo uno de los dos podría conectar (ver [docs/mavlink.md](mavlink.md) para la explicación completa).

Compilar e instalar (instrucciones detalladas, con capturas y solución de problemas de compilación, en el documento del proyecto *"DronSAR — De SITL a vuelo real"*):
```bash
sudo apt install -y git meson ninja-build pkg-config gcc g++ systemd libsystemd-dev
git clone https://github.com/mavlink-router/mavlink-router.git
cd mavlink-router
git submodule update --init --recursive
meson setup build . -Dsystemdsystemunitdir=/usr/lib/systemd/system
ninja -C build
sudo ninja -C build install
```

Configurar `/etc/mavlink-router/main.conf` para que reparta `/dev/ttyAMA0` hacia `udpin:127.0.0.1:14550` (para `receptor.py`) y `udpin:127.0.0.1:14551` (para `vuelo.py`), y dejarlo activo y habilitado en el arranque:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mavlink-router.service
```

Con esto instalado y activo, el cambio de `receptor.py`/`vuelo.py` a modo real se reduce a `MAVLINK_MODE=real` en el `.env` — ver [docs/mavlink.md](mavlink.md).
