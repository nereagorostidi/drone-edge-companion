# Detección de personas: deteccion.py, YOLO y pesos

Todo lo relativo al dominio `deteccion`: opciones de línea de comandos de `deteccion.py`, los distintos motores de ejecución (`--runtime`) y cómo generar los pesos que necesita cada uno, el streaming en directo, y el arranque/parada remota cuando corre como servicio con cámara.

Requiere `weights/best.pt` (pesos del modelo YOLO entrenado).

**Procedencia de `weights/`.** El entrenamiento de `best.pt` (YOLO11n sobre el dataset de personas) se hace en Google Colab, aprovechando su GPU gratuita. A partir de ahí, la conversión de ese `best.pt` al resto de formatos que necesita `deteccion.py` — ONNX, ONNX INT8, NCNN y el `.hef` para el Hailo-8 — se hace aparte, con `generate_all_formats.py` (ver más abajo), en un PC Linux prestado con GPU: es simplemente más ágil y cómodo que hacerlo desde el propio Colab, sobre todo por la compilación a `.hef`, que necesita el Dataflow Compiler de Hailo instalado localmente (no disponible en Colab) y tarda varios minutos.

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
Por defecto (`--runtime pt`) carga `weights/best.pt` con PyTorch. Los otros cuatro motores cargan un formato derivado de ese mismo `best.pt` — `weights/best.onnx` (`--runtime onnx`), `weights/best.int8.onnx` (`--runtime onnx-int8`), `weights/best_ncnn_model/` (`--runtime ncnn`, motor optimizado para CPUs ARM como la de la Raspberry Pi) y `weights/best_hailo_model/best.hef` (`--runtime hef`, el acelerador Hailo-8 del AI Kit) — y los cuatro se generan **en un solo paso** con `generate_all_formats.py`:
```bash
python3.11 -m venv compiler_env && source compiler_env/bin/activate    # solo la primera vez
pip install vendor/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl
pip install -r requirements-compile.txt

python generate_all_formats.py                # genera los 4 formatos a partir de weights/best.pt
python deteccion.py samples/vuelo1.mp4 --runtime onnx
python deteccion.py samples/vuelo1.mp4 --runtime onnx-int8
python deteccion.py samples/vuelo1.mp4 --runtime ncnn
python deteccion.py samples/vuelo1.mp4 --runtime hef
```
Cada vez que haya un `weights/best.pt` nuevo (reentrenamiento), hay que volver a ejecutar `generate_all_formats.py` para regenerar los cuatro; el motor de ejecución (`--runtime`) es independiente del modelo base. Antes de usar `--runtime onnx-int8` en vuelo real, conviene comparar sus detecciones con las de `--runtime onnx` sobre el mismo vídeo, porque la cuantización dinámica no calibra con datos reales y puede perder algo de precisión.

**Importante — esto NO se ejecuta en la Raspberry Pi.** El paso que compila `best_hailo_model/best.hef` necesita el Dataflow Compiler de Hailo, que solo existe para x86_64 (nunca corre en la Pi, que es ARM). `generate_all_formats.py` se ejecuta en un PC de escritorio con su propio entorno virtual `compiler_env/` (independiente del `venv/` de producción, ver [Requisitos](../README.md#requisitos)); el resultado (`weights/`) se lleva a la Pi por el medio habitual (`git pull`, o copiando la carpeta a mano). El wheel del DFC no se distribuye por pip — hay que descargarlo de la Developer Zone de Hailo y colocarlo en `vendor/` (ver comentarios en `requirements-compile.txt`).

**Alternativa sin el PC de compilación (todo salvo el `.hef`).** `weights/best.onnx`, `weights/best.int8.onnx` y `weights/best_ncnn_model/` no necesitan el Dataflow Compiler de Hailo, así que también se pueden generar desde el propio Google Colab donde se entrena `best.pt`, sin depender de ningún PC prestado. Para eso están los scripts sueltos de `conversion/` (independientes entre sí y del pipeline unificado de arriba):
```bash
python conversion/exportar_onnx.py       # weights/best.pt -> weights/best.onnx
python conversion/cuantizar_onnx.py      # weights/best.onnx -> weights/best.int8.onnx
python conversion/exportar_ncnn.py       # weights/best.pt -> weights/best_ncnn_model/
```
El `.hef` es la única excepción: siempre hace falta la máquina local con el DFC instalado (ver arriba), Colab no sirve para ese paso.

Opciones útiles de `generate_all_formats.py` (`-h` para el resto):
- `--weights otro/best.pt` — compila a partir de otros pesos en vez de `weights/best.pt` (se copian ahí, pasando a ser los pesos canónicos).
- `--skip-hef` — se salta la compilación a `.hef` (el paso lento, 10-20 min); útil para iterar rápido sobre onnx/int8/ncnn.
- `--fresh --epochs 150` — entrena un YOLO11n nuevo desde cero sobre `dataset/` en vez de partir de un `.pt` existente.

Antes de sobreescribir `weights/`, el script guarda una copia de la versión anterior en `weights_prev/` (se sobreescribe en cada ejecución, no es un historial).

`dataset/` (el dataset de personas usado para calibrar la cuantización INT8 del `.hef`, y para `--fresh`) no está en git — es el dataset versionado en Roboflow que indica `dataset/data.yaml`, y hay que copiarlo ahí a mano antes de compilar.

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
