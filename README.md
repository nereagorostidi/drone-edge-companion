# drone-edge-companion

Nodo edge del sistema UAV de Búsqueda y Rescate (SAR) del TFG.

Corre en una Raspberry Pi 5 con sensor BME680 (temperatura, humedad y presión). Lee las medidas, las publica por MQTT hacia el broker en AWS EC2, y almacena las que no se pueden enviar en un buffer SQLite local para reenviarlas cuando se recupere la conectividad (*store-and-forward*).

## Requisitos

- Raspberry Pi 5 con Raspberry Pi OS (Bookworm)
- Sensor BME680 conectado por I2C (dirección `0x76`)
- Python 3.10+
- Entorno virtual en `/home/nerea/bme680-env`

## Instalación

```bash
git clone git@github.com:<tu-usuario>/drone-edge-companion.git
cd drone-edge-companion
pip install -r requirements.txt
cp .env.example .env   # edita con tus credenciales
```

## Ejecución manual

```bash
source /home/nerea/bme680-env/bin/activate
python sensor.py -i 30
```

El flag `-i` define el intervalo entre lecturas en segundos.

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
