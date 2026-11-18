# PAT Control Center (PAT·CC)

Centro de control de todo lo importante de la casa Patricio 10: luces, medios, sensores, escenas y horarios (Home Assistant + Tuya).
Más adelante Vision se integra a esta identidad. Vive en el mismo
repositorio que thor-vision (`patcc/`), pero corre en **su propio contenedor y puerto**: si uno se cae, el otro sigue.
thor-vision solo muestra un reflejo ligero (un indicador con las luces encendidas en el encabezado).

## Cómo entrar

- Dirección: `http://192.168.0.10:8095`. **No pide cuenta**: entra quien esté en la red local (decisión del dueño, mientras el
  proyecto está empezando). En la bitácora cada acción queda con la dirección desde donde se ordenó.
- Para volver a pedir login (usuario, contraseña y código, con roles): poner `PATCC_AUTH=on` en `~/pat-cc/patcc.env`,
  recrear el contenedor y crear la primera cuenta:

```bash
ssh -t thor docker exec -it pat-cc python3 -m app.auth.admin add Nombre.Apellido
ssh -t thor docker exec -it pat-cc python3 -m app.auth.admin rol Nombre.Apellido admin
```

## Qué hace

| Pestaña | Contenido |
|---|---|
| Inicio | Luces encendidas, puertas abiertas, qué suena, escenas a un toque, todos los focos Tuya a la vez (encender, brillo, color, blanco) |
| Luces | Interruptores Tuya por botón, focos Tuya (brillo, color y blanco por red local), luces y enchufes de Home Assistant, cortinas |
| Medios | Bocinas AirPlay/Music Assistant, Apple TV y Fire TV de Home Assistant: reproducir, volumen, fuente y **anunciar un mensaje** |
| Sensores y puertas | Puertas, ventanas y portón de Tuya; sensores, personas y clima de Home Assistant |
| Escenas y horarios | Escenas que mezclan órdenes de Tuya y Home Assistant; horarios por hora fija o al amanecer/atardecer (con desfase) |
| Ajustes | Dirección y token de Home Assistant (solo admin) |
| Bitácora | Quién ordenó qué y cuándo |

## Seguridad

- **Sin login por defecto:** cualquier equipo de la red local puede controlar los dispositivos y cambiar el token de Home Assistant.
  Conviene que el puerto 8095 no se publique fuera de la red (sin reenvío de puertos en el UDM).
- El token de Home Assistant se pega en **Ajustes**; se prueba antes de guardarse, queda en `data/ha.json` con permisos 600
  y nunca se devuelve al navegador (solo los últimos 4 caracteres). Con login activo, solo el rol admin lo cambia o borra.
- Solo se permiten acciones de control (luces, enchufes, medios, cortinas, escenas, scripts, anuncios). No se puede reiniciar
  Home Assistant ni presionar botones de equipos (el NAS expone «reboot» y «shutdown»).
- Las órdenes que cambian algo exigen mismo origen: otra página abierta en el navegador no puede ordenar por ti.
- Tuya: el servicio de oapc-devops solo acepta los equipos de `config/iot.yml` (casa Patricio 10) y como mucho una orden por
  segundo por equipo.
- Con login activo, las cookies son propias (`patcc_sesion`): iniciar o cerrar sesión aquí no afecta a thor-vision.

## Despliegue en la Thor

Código en `~/pat-cc/code` (solo `patcc/` y `app/auth`, `app/_vendor`), datos en `~/pat-cc/data`, variables en
`~/pat-cc/patcc.env` (600). Usa la misma imagen que thor-vision; no reconstruye nada.

```bash
docker run -d --name pat-cc --restart unless-stopped --network host \
  --env-file ~/pat-cc/patcc.env -e PYTHONPATH=/srv/code -w /srv/code \
  -v ~/pat-cc/code:/srv/code:ro -v ~/pat-cc/data:/srv/data \
  --entrypoint python3 thor-vision:latest -m uvicorn patcc.main:app --host 0.0.0.0 --port 8095
```

Variables de `patcc.env`:

| Variable | Valor |
|---|---|
| `PATCC_DATA` / `THOR_AUTH_DB` | `/srv/data` / `/srv/data/auth.db` |
| `PATCC_AUTH` | `off` (por defecto, sin login) o `on` |
| `THOR_SESSION_SECRET` | aleatorio, 64 caracteres (solo se usa con `PATCC_AUTH=on`) |
| `THOR_COOKIE_PREFIX` / `THOR_APP_NAME` | `patcc` / `PAT Control Center` |
| `THOR_MACHINE_TOKEN` + `THOR_MACHINE_EXTRA=/api/resumen` | token para que thor-vision lea el resumen |
| `TUYA_SERVICE_URL` | `http://192.168.0.194:8765` (servicio de control en oapc-devops) |
| `VISION_URL` (+ `VISION_TOKEN`) | `http://127.0.0.1:8080`; con el login de thor-vision activo hace falta su token de máquina y agregar `/api/iot` a su `THOR_MACHINE_EXTRA` |

Reflejo en thor-vision: sin login no hace falta configurar nada (opcionalmente `PATCC_URL` / `PATCC_PUBLIC_URL`); con `PATCC_AUTH=on`
se necesita `PATCC_TOKEN` (= `THOR_MACHINE_TOKEN` de PAT·CC).

## Alexa y Fire TV

PAT·CC muestra automáticamente cualquier reproductor que exista en Home Assistant. Hoy Home Assistant no tiene Alexa y el Fire TV
está desconectado:

- **Alexa:** instalar en Home Assistant la integración *Alexa Media Player* (por HACS). Cada Echo aparece como reproductor, con
  anuncios. Para que Alexa controle los equipos de Home Assistant por voz, el camino es la habilidad de Home Assistant para
  Alexa (Home Assistant Cloud o la configuración manual de la habilidad).
- **Fire TV:** integración *Android Debug Bridge* en Home Assistant, con la depuración ADB encendida en el Fire TV
  (Configuración → Mi Fire TV → Opciones de desarrollador).

## Pendiente

- Las luces de Home Assistant (2 focos TP-Link KL125) están sin conexión.
- Notificaciones al celular desde las escenas (Home Assistant ya tiene `notify.gecko_15p` y `notify.gecko_xiii_pro`).
- Ver las cámaras de Home Assistant (hoy sin conexión; go2rtc de Home Assistant apunta a una dirección que no responde).
