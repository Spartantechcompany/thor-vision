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
| Ajustes | Conexiones en vivo (Home Assistant, servicio Tuya, focos por red local, thor-vision/MQTT, broker, nube) con su tiempo de respuesta; token de Home Assistant con fecha de creación y vencimiento; registro de la nube de Tuya con botón **Sincronizar ahora** y la lista de cambios detectados |
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

## Registro de la nube de Tuya (base de datos)

El servicio Tuya de oapc-devops (`video_service.py`, el único con las credenciales de la nube) expone `GET /registry` y
`POST /registry/sync` (solo lectura, sin claves locales ni uuid). PAT·CC los consulta cada 10 min (`PATCC_SYNC_MIN`) o al apretar
**Sincronizar ahora**, y guarda en `data/registro.db` (SQLite):

- `dispositivos`: cada equipo con su casa, su estado de nube y si está en línea.
- `cambios`: lo que cambió entre sincronizaciones (nuevo, renombrado, cambio de nube, en línea/sin conexión, cambió de casa, detectado en la nube, de otra casa).
- `sync`: historial de sincronizaciones (éxito, tiempo, detalle).

Estado de nube de cada equipo:

| Estado | Significado | En el panel |
|---|---|---|
| `cupo` | Está en la lista de la nube: las órdenes por la nube funcionan | normal |
| `sin_cupo` | Está en `iot.yml` pero la nube no lo lista: rechaza sus órdenes («quota is insufficient» / «permission deny») | etiqueta «sin cupo en la nube», controles desactivados (los focos siguen por red local) |
| `retirado` | `retired: true` en `iot.yml` (ya no existe en la nube) | oculto |

**Filtro por casa:** solo cuenta la casa `PATCC_TUYA_HOME` (153519480 = Patricio 10). Un equipo de otra casa (Coworking540 =
195128033) se anota como «de otra casa (ignorado)» y nunca se muestra ni se controla; PAT·CC además rechaza órdenes a cualquier
equipo que no esté en la lista de la casa. Mientras thor-vision (v2.45.0) no exponga `retired`, `PATCC_RETIRED_IDS` (ids separados por coma) cubre el caso.

## Consola de devops

`http://192.168.0.10:8095/consola` (no está enlazada en la interfaz; en cualquier pestaña, la tecla `` ` `` la abre en otra ventana).
Muestra en vivo el registro técnico (llamadas a Home Assistant y a Tuya con su tiempo, errores, sincronizaciones, horarios), con filtros
por nivel y fuente, búsqueda, pausa y seguimiento. Botones de diagnóstico que **no cambian ningún dispositivo**: Sincronizar Tuya,
Probar HA, Probar Tuya, Probar MQTT. Se guarda también en `data/consola.log` (rotativo, 2 MB × 3). No registra tokens ni claves.
La **bitácora** (pestaña Bitácora) es otra cosa: quién ordenó qué.

## API de usuario de Tuya (OpenClaw skills)

La nube del proyecto de Tuya solo controla los equipos de su «cupo» (hoy 24); el resto responde «quota is insufficient» o «permission deny».
La **API de usuario final** (la de los *OpenClaw skills*, github.com/tuya/tuya-openclaw-skills) actúa como el usuario de la app y no
usa ese cupo. En **Ajustes → Tuya · API de usuario** se pega la API Key (`sk-…`):

- Se prueba antes de guardarse (lee casas y equipos; no cambia nada) y queda en `data/tuya_user.json` con permisos 600. Nunca vuelve al navegador ni se escribe en la consola.
- El país sale de las dos letras tras `sk-` (AZ = `openapi.tuyaus.com`); el endpoint se puede cambiar a mano.
- Solo se usan las casas `PATCC_TUYA_HOME` / `PATCC_TUYA_HOME_NAME` (Patricio 10). Coworking540 no se consulta.
- Enrutado de las órdenes: focos → red local; equipos **con cupo** → servicio Tuya de siempre; equipos **sin cupo** → API de usuario
  (`POST /v1.0/end-user/devices/{id}/shadow/properties/issue`, valor validado contra el modelo del equipo, sin reintentos automáticos).
  Si la nube del proyecto rechaza una orden por cupo o permiso, se intenta una vez por la API de usuario.
- Límites del lado de Tuya (fase de prueba, sin cifras publicadas): puede haber cuotas y límites de frecuencia. Solo propiedades booleanas, enteras, de lista o texto (no cámaras ni cerraduras).
- Si la clave se pegó en un chat o correo, genera una nueva en la plataforma de Tuya y reemplázala aquí.
