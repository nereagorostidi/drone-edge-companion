# Detección de personas: deteccion.py, YOLO y pesos

Todo lo relativo al dominio `deteccion`: opciones de línea de comandos de `deteccion.py`, los distintos motores de ejecución (`--runtime`) y cómo generar los pesos que necesita cada uno, el streaming en directo, y el arranque/parada remota cuando corre como servicio con cámara.

Requiere `weights/best.pt` (pesos del modelo YOLO entrenado).

## Ejecución sobre un vídeo de fichero
```bash
source /home/nerea/drone-edge-companion/venv/bin/activate
python deteccion.py samples/vuelo1.mp4                    # MQTT activado, SIN ventana de preview (por defecto)
python deteccion.py samples/vuelo1.mp4 --mqtt false       # solo detección + vídeo anotado, sin MQTT
python deteccion.py samples/vuelo1.mp4 --preview true     # con ventana de vista previa; el vídeo en results/videos/ se genera igual
python deteccion.py -h                                    # todas las opciones (--conf, --vid-stride, --anti-spam...)
```
El vídeo anotado de cada sesión se guarda en `{VIDEOS_DIR}/{DRON_ID}_{fuente}_{fecha}.mp4` — por defecto `results/videos/`, configurable con `VIDEOS_DIR` en el `.env` (ver [Variables de entorno](../README.md#variables-de-entorno)) sin tocar código (con un vídeo de fichero, o con `--camera` y `--mqtt false`, se genera siempre, de principio a fin de la ejecución; con `--camera` y `--mqtt true` ver [Arranque y parada remota](#arranque-y-parada-remota-de-deteccionpy)). Cada alerta enviada (con `--mqtt true`, respetando el `--anti-spam`) guarda además una foto del frame en `{FOTOS_DIR}/{DRON_ID}_{fecha}.jpg` (por defecto `results/fotos/`, configurable con `FOTOS_DIR`), cuyo nombre viaja en el campo `foto` del JSON de la alerta.

Para que las alertas lleven posición, `vuelo.py` debe estar en marcha en la misma carpeta (comparten `posicion_actual.json`); si no lo está, la alerta se envía igualmente pero sin coordenadas.

Por defecto (`--overlay true`) esa foto lleva superpuestas las coordenadas del dron y la fecha/hora de la detección; con `--overlay false` se guarda el frame tal cual. Solo afecta a la foto — el vídeo anotado y la ventana de preview nunca llevan esta marca:
```bash
python deteccion.py samples/vuelo1.mp4 --overlay false   # fotos sin coordenadas/fecha superpuestas
```

## Streaming en directo
Por defecto (`--stream true`) se emite además el vídeo anotado en directo hacia MediaMTX (`streaming.py`, vía `ffmpeg`, RTSP), en paralelo a la grabación local — más ligero (por defecto 640×360 a 12 FPS, configurable con `STREAM_ANCHO`/`STREAM_ALTO`/`STREAM_FPS`) que el vídeo guardado en `results/videos/`. Es un extra a prueba de fallos: si falta `ffmpeg`, faltan `STREAM_HOST`/`STREAM_USER`/`STREAM_PASS` en el `.env`, o se cae la conexión a mitad de sesión, se desactiva solo con un aviso por consola y la detección (vídeo local + alertas MQTT) sigue sin cortarse:
```bash
python deteccion.py samples/vuelo1.mp4 --stream false   # sin streaming en directo, solo vídeo local
```

## Motores de ejecución (`--runtime`) y generación de pesos
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

## Cámara en vivo
En la Raspberry Pi, en vez de un vídeo grabado se puede analizar en directo desde la cámara con `--camera` (mutuamente excluyente con `video_path`):
```bash
python deteccion.py --camera 0                      # cámara por índice (la primera detectada); SIN ventana de preview (por defecto)
python deteccion.py --camera /dev/video0             # cámara por ruta de dispositivo V4L2
python deteccion.py --camera 0 --preview true        # en directo, con ventana (NO usar en systemd, no hay pantalla)
```
Con `--camera` la fuente no tiene fin natural (a diferencia de un fichero): el análisis sigue hasta pulsar `Ctrl+C` (o `q` en la ventana de preview si está activada, lo que además cierra el script del todo). Requiere que la cámara esté expuesta como dispositivo V4L2 (`ls /dev/video*`); con el módulo oficial de la Raspberry Pi puede hacer falta `sudo modprobe bcm2835-v4l2` (o la capa de compatibilidad de libcamera) para que aparezca como `/dev/video0`.

## Arranque y parada remota de `deteccion.py`
Con `--camera` **y** `--mqtt true` (el caso real: el servicio systemd), `deteccion.py` no arranca solo al lanzarlo: se queda a la espera del comando `start_recording` en `dronsar/{dron_id}/deteccion/config` (el mismo topic que `set_video_throttle`, ver [Flujo de configuración](flujos.md#flujo-de-configuración)). Mientras espera no hay preview, ni vídeo, ni detección — el proceso solo escucha MQTT:
```bash
python deteccion.py --camera 0
# -> "A la espera de 'start_recording' desde el panel (topic 'dronsar/dron-02/deteccion/config')..."
```
Al recibir `start_recording` arranca la sesión completa (vídeo anotado, preview si está activado, detección y alertas MQTT, y streaming en directo si `--stream` está activado); al recibir `stop_recording` la cierra —guardando el vídeo de esa sesión en `results/videos/` con su propio timestamp y cortando el streaming— **sin cerrar el script**, que vuelve a quedarse a la espera del siguiente `start_recording`. Se pueden encadenar tantas sesiones como se quiera sin reiniciar el proceso.

Con un fichero de vídeo, o con `--mqtt false`, no hay nada que esperar: `deteccion.py` arranca directo, como siempre (estos comandos no tienen efecto en ese caso).

Instalación como servicio systemd (`deteccion-sar.service`) y sus particularidades (índice de cámara, `--runtime ncnn` por defecto, política de reinicio) en [docs/servicios.md](servicios.md#particularidad-de-deteccion-sarservice).
