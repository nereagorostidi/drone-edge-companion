from pymavlink import mavutil

DEVICE = '/dev/ttyAMA0'
BAUD = 57600

print(f"Conectando a {DEVICE} a {BAUD} baudios...")
m = mavutil.mavlink_connection(DEVICE, baud=BAUD)

print("Esperando heartbeat (puede tardar unos segundos)...")
m.wait_heartbeat()

print(f"\n--> OK: heartbeat recibido")
print(f"    Sistema: {m.target_system}")
print(f"    Componente: {m.target_component}")
