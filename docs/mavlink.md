# Conexión real: Pixhawk por TELEM3

Además del modo SITL (Mission Planner reenviando por red, el caso por defecto), `receptor.py` y `vuelo.py` pueden hablar directamente con un Pixhawk físico conectado por cable al puerto TELEM3, sin cambiar nada de su lógica de vuelo — solo cambia de dónde viene la conexión MAVLink.

**Por qué hace falta `mavlink-router`:** en SITL, Mission Planner reenvía por red a un puerto UDP distinto para cada proceso (`receptor.py` y `vuelo.py` pueden escuchar cada uno en el suyo sin pisarse). Un puerto serie no funciona igual: es un recurso exclusivo del sistema operativo, así que solo un proceso puede tenerlo abierto a la vez. `mavlink-router` resuelve esto: es el único proceso que abre el puerto serie de TELEM3, y reparte ese tráfico hacia un puerto UDP local por proceso — es lo que permite que varios flujos (`receptor.py` y `vuelo.py`, y cualquier otro que se añada en el futuro) se conecten simultáneamente a la Pixhawk a través del mismo puerto serie, exactamente el mismo papel que hacía Mission Planner en SITL, pero para hardware real.

**Si en cambio lo que quieres es conectar contra un SITL remoto por red** (Mission Planner en otra máquina, alcanzable por IP), no hace falta `mavlink-router` en absoluto: esa es una conexión de red directa, no un puerto serie exclusivo, así que cada proceso puede abrir su propia conexión UDP contra ese SITL sin que nadie se los reparta — es el modo `sitl` de siempre (`MAVLINK_CONN`/`MAVLINK_CONN_VUELO`), solo que apuntando a una IP remota en vez de `127.0.0.1`.

Instalación completa (cableado de TELEM3, activación en ArduPilot, compilación de `mavlink-router` y su servicio systemd) en [docs/instalacion-hardware.md](instalacion-hardware.md). Una vez instalado, el cambio de modo se reduce a poner `MAVLINK_MODE=real` en el `.env` (en vez de `sitl`) — ver [Variables de entorno](../README.md#variables-de-entorno).

Antes de dar por bueno el enlace, conviene comprobarlo con los scripts sueltos de la siguiente sección — son más rápidos de usar que arrancar `receptor.py`/`vuelo.py` para cada comprobación.

## Scripts de prueba de la conexión serie
Cuatro scripts sueltos, sin MQTT ni dominios, pensados para aislar el enlace TELEM3 paso a paso (documentados en detalle, con el código completo, en el Apéndice C del documento del proyecto):

| Script | Para qué sirve | Conexión |
|---|---|---|
| `test-serial.py` | Confirma que el UART de la Raspberry Pi funciona, con un jumper TX-RX en los propios pines 8 y 10 — sin ningún cable a la Pixhawk. Primer paso si TELEM3 "no da señal" | Solo serie (loopback) |
| `test-mavlink.py` | Confirma que llega un heartbeat real de la Pixhawk, ya con el cable de TELEM3 conectado | Solo serie directo |
| `test-arm-serial.py` | Prueba el ciclo completo de armado/desarmado: pide confirmación antes de tocar los motores, captura el motivo real de un rechazo (STATUSTEXT del autopiloto, p. ej. `PreArm: GPS: no fix`) y permite forzar el armado saltándose los pre-arm checks | Serie directo o `--conexion router` |
| `test-estado.py` | Panel en vivo de batería, GPS, RC y los mensajes de pre-arm reales del autopiloto — útil para ajustar calibraciones sin tener que interpretar MAVLink a mano | Serie directo o `--conexion router` |

`test-arm-serial.py` y `test-estado.py` admiten `--conexion serial` (por defecto, abre `/dev/ttyAMA0` directamente) o `--conexion router` (UDP contra `mavlink-router`, sin tocar el puerto serie). Si `mavlink-router.service` ya está corriendo, tiene el puerto serie abierto para él solo: usa `--conexion router`, o para el servicio primero con `sudo systemctl stop mavlink-router.service`.

```bash
python test-serial.py                       # loopback, sin la Pixhawk
python test-mavlink.py                       # heartbeat real por serie directo
python test-arm-serial.py --conexion router   # armar/desarmar, vía mavlink-router
python test-estado.py --conexion serial       # panel batería/GPS/RC, serie directo
```
