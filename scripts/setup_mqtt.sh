#!/usr/bin/env bash
# Se corre EN THOR (el servidor). Guarda las credenciales MQTT de solo lectura en data/mqtt.json (permisos 600, dentro del contenedor)
# e instala paho-mqtt en el volumen de datos (sin tocar la imagen). La clave se pide sin mostrarla y viaja por stdin, no por argumentos.
set -euo pipefail
read -rp "Usuario MQTT de solo lectura (ej. thor_lector): " U
read -rsp "Clave: " P; echo
read -rp "Host del broker [192.168.0.101]: " H; H=${H:-192.168.0.101}
printf '%s' "$P" | docker exec -i -e U="$U" -e H="$H" thor-vision python3 -c '
import json, os, sys
cfg = {"host": os.environ["H"], "port": 1883, "user": os.environ["U"], "password": sys.stdin.read(), "topic": "oxm/tuya/#"}
fd = os.open("/app/data/mqtt.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f)
print("data/mqtt.json guardado (permisos 600)")
'
docker exec thor-vision pip install -q --no-deps --target /app/data/pylibs paho-mqtt
docker exec thor-vision python3 -c "import sys; sys.path.append('/app/data/pylibs'); import paho.mqtt.client; print('paho-mqtt instalado')"
echo "Listo. Reinicia thor-vision: docker restart thor-vision"
