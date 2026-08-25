# Ejecución como servicio (systemd)

Cada proceso tiene ya su propio fichero `.service` en el repositorio, listo para instalar en `/etc/systemd/system/`:

| Servicio | Proceso | Restart |
|---|---|---|
| `sensor-sar.service` | `sensor.py` (dominio `ambiental`) | `always` — captura continua |
| `sistema-sar.service` | `sistema.py` (dominio `sistema`) | `always` — captura continua |
| `vuelo-sar.service` | `vuelo.py` (dominio `vuelo`) | `always` — captura continua |
| `deteccion-sar.service` | `deteccion.py` (dominio `deteccion`) | `always` — cámara por defecto: queda en marcha indefinidamente a la espera de `start_recording`/`stop_recording` |
| `receptor-sar.service` | `receptor.py` (comandos) | `on-failure` |

> `mavlink-router.service` no es un servicio de este repositorio (se instala aparte, ver [docs/instalacion-hardware.md](instalacion-hardware.md)), pero es un **prerrequisito** de `receptor-sar.service` y `vuelo-sar.service` cuando trabajan en modo real (`MAVLINK_MODE=real`): debe estar `enable`d y corriendo para que ambos puedan conectarse por el puerto serie de TELEM3 a la vez (ver [docs/mavlink.md](mavlink.md)). Comprobar con `sudo systemctl status mavlink-router.service` antes de arrancar `receptor-sar`/`vuelo-sar` en producción; en modo `sitl` no hace falta.

Instalación de cualquiera de los servicios de captura continua (`sensor-sar`, `sistema-sar`, `vuelo-sar`) o del receptor — mismo patrón para los cinco, cambiando el nombre del fichero:
```bash
sudo cp sensor-sar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sensor-sar.service
```
Con `vuelo-sar.service` en concreto, comprobar antes que el Pixhawk (o Mission Planner en SITL) ya está reenviando por MAVLink (ver [Requisitos](../README.md#requisitos)): el `ExecStart` no lleva `--fake`, así que sin eso disponible el servicio se queda esperando el heartbeat indefinidamente en vez de arrancar.

## Particularidad de `deteccion-sar.service`
A diferencia de los demás, `deteccion.py` no es un colector en bucle infinito sobre un fichero: recibe como fuente O bien una ruta de vídeo O bien una cámara en vivo (`--camera`, ver [docs/video.md](video.md)). `deteccion-sar.service` trae **la cámara (índice 0) como fuente por defecto** — es el caso normal de este servicio: siempre disponible, sin grabar nada hasta que el panel lo pida. Antes de instalar `deteccion-sar.service` conviene revisar:

1. El índice/ruta de la cámara en `ExecStart` (`--camera 0` por defecto): cambiarlo si la Pi tiene más de una cámara o el índice `0` no es el correcto (`ls /dev/video*` para comprobar). Si en su lugar quieres procesar un vídeo de fichero con este servicio (caso raro — pensado sobre todo para pruebas manuales por SSH, no para el despliegue), sustituye `--camera 0` por la ruta del vídeo y ten en cuenta que entonces `start_recording`/`stop_recording` no tienen efecto (arranca directo, ver [docs/video.md](video.md#arranque-y-parada-remota-de-deteccionpy)).
2. No añadir `--preview true`: un servicio systemd no tiene pantalla, así que la ventana de vista previa (`cv2.imshow`) fallaría si se activa. `--preview` es `false` por defecto, así que basta con no tocarlo. El vídeo anotado en `results/videos/` y las fotos en `results/fotos/` se generan igual, con o sin preview.
3. La política de reinicio: por defecto `Restart=always`, para que el servicio se recupere también de una caída limpia de la cámara (no solo de un fallo) — tiene sentido con la cámara como fuente permanente. Si en su lugar apuntas a un vídeo de fichero, probablemente quieras `Restart=on-failure` (que no lo relance si termina bien).
4. Comprobar `ffmpeg` y las variables `STREAM_*` si se quiere streaming en directo (`--stream` es `true` por defecto — ver [Requisitos](../README.md#requisitos)); si no interesa para este despliegue, añadir `--stream false` al `ExecStart` para no intentarlo en cada sesión.
5. `ExecStart` trae `--runtime ncnn` (a diferencia del `--runtime pt` por defecto de `deteccion.py` en manual, pensado para dev/depuración): NCNN es el motor más ligero/rápido para la CPU ARM de la Pi, ideal para producción. Requiere `weights/best_ncnn_model/` (ya viene commiteado en el repo — si algún día se reentrena, hay que regenerarlo con `conversion/exportar_ncnn.py` y volver a commitearlo, ver [docs/video.md](video.md)); sin él, el servicio falla al arrancar con un aviso claro (`No encuentro ".../weights/best_ncnn_model/"...`).

Con la cámara como fuente y `--mqtt true` (el caso por defecto de este servicio), el proceso arranca y se queda esperando el comando `start_recording` del panel de control — no analiza nada hasta que se lo mandan (ver [docs/video.md](video.md#arranque-y-parada-remota-de-deteccionpy)). Esto es intencional: el servicio puede estar `enable --now` de forma permanente sin grabar nada hasta que se necesite.

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

