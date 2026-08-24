import serial
import time

DEVICE = '/dev/ttyAMA0'
BAUD = 57600
MENSAJE = b'TEST_LOOPBACK_OK\n'

print(f"Abriendo {DEVICE} a {BAUD} baudios...")
s = serial.Serial(DEVICE, BAUD, timeout=2)
s.reset_input_buffer()

print(f"Enviando: {MENSAJE}")
s.write(MENSAJE)
time.sleep(0.2)

respuesta = s.readline()
print(f"Recibido: {respuesta}")

if respuesta == MENSAJE:
    print("\n--> [OK] El puente TX-RX funciona correctamente.\n")
else:
    print(f"\n--> [FALLO] No ha llegado el mismo mensaje. Recibido: {respuesta!r}\n")

s.close()
