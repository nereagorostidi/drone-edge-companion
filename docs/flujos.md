# Flujos de datos y comandos

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

`receptor.py` se suscribe al topic `dronsar/<DRON_ID>/comandos` y traduce cada comando recibido mediante un diccionario de acciones MAVLink (`ACCIONES`). No se comunica directamente con `api.py`: ambos son clientes independientes del broker. Tampoco da órdenes a Mission Planner, sino al autopiloto; Mission Planner, conectado al mismo autopiloto, refleja lo que ocurre.

| `command` | `params` | Traducción a MAVLink |
|---|---|---|
| `arm` | `{}` | `arducopter_arm()` + espera confirmación de armado |
| `disarm` | `{}` | `arducopter_disarm()` + espera confirmación |
| `takeoff` | `{"altitude": N}` | modo `GUIDED` → arma si hace falta → `MAV_CMD_NAV_TAKEOFF` a `N` m (AGL); si no llega `altitude`, usa `ALTITUD_DESPEGUE_DEF` = 10 m |
| `hold` | `{}` | modo `LOITER` (mantener posición; requiere GPS con fix 3D) |
| `land` | `{}` | modo `LAND` (aterriza en la vertical actual) |
| `rtl` | `{}` | modo `RTL` (vuelve al punto de despegue y aterriza) |

Los cambios de modo se confirman leyendo `master.flightmode` (que mantiene al día el hilo lector de MAVLink), no releyendo del puerto. Si el autopiloto rechaza un armado o un cambio de modo, el motivo aparece en el log como `[STATUSTEXT autopiloto]` (p. ej. `PreArm: GPS: no fix`).

> Nota: `receptor.py` usa las mismas `DRON_ID`/`EC2_HOST` que el resto de dominios. El valor de `DRON_ID` debe coincidir con el que elige el panel de control en su desplegable (`dron-01` / `dron-02`, validado en `api.py` contra `DRONES_VALIDOS`), ya que va literalmente en el topic — si no coincide, los comandos de vuelo se publican en un topic que nadie escucha y se pierden sin ningún error visible.

## Flujo de configuración
Además de comandos de vuelo, el panel de control puede reconfigurar en caliente tres dominios, con el mismo esquema `dronsar/...` y el mismo formato de payload (`command`/`params`/`dron_id`/`command_id`/`timestamp`) que usa `receptor.py` para comandos — lo publica `api.py` (ver `COMANDOS_CONFIG`). Cada script se suscribe a su propio topic de configuración y aplica el cambio sin reiniciar:

| Dominio | Script | Se suscribe a | `command` | `params` | Efecto |
|---|---|---|---|---|---|
| `sensor` | `sensor.py` | `dronsar/{dron_id}/sensor/config` | `set_sensor_interval` | `{"interval_seconds": N}` | Cambia el intervalo de captura del BME680 (segundos) |
| `deteccion` | `deteccion.py` | `dronsar/{dron_id}/deteccion/config` | `set_video_throttle` | `{"throttle_ms": N}` | Cambia el anti-spam de alertas de vídeo (el valor llega en milisegundos y se convierte a segundos) |
| `deteccion` | `deteccion.py` | `dronsar/{dron_id}/deteccion/config` | `start_recording` | `{}` | Arranca la sesión de grabación/detección (solo tiene efecto con `--camera` y `--mqtt true`; ver [Arranque y parada remota](video.md#arranque-y-parada-remota-de-deteccionpy)) |
| `deteccion` | `deteccion.py` | `dronsar/{dron_id}/deteccion/config` | `stop_recording` | `{}` | Detiene la sesión en curso (guarda el vídeo) sin cerrar el script, que vuelve a esperar el siguiente `start_recording` |
| `sistema` | `sistema.py` | `dronsar/{dron_id}/sistema/config` | `shutdown` | `{}` | Apaga la Raspberry Pi (`sudo shutdown -h now`) |

