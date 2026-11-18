# OXM Hogar

Panel aparte para controlar y automatizar las luces y dispositivos de la casa (Home Assistant + Tuya). Vive en el mismo
repositorio que thor-vision (`hogar/`), pero corre en **su propio contenedor y puerto**: si uno se cae, el otro sigue.
thor-vision solo muestra un reflejo ligero (un indicador con las luces encendidas en el encabezado).

## Cómo entrar

- Dirección: `http://192.168.0.10:8095`
- Cuentas propias (no son las de thor-vision). La primera se crea desde una terminal en la Thor, para que ninguna contraseña
  pase por el navegador:

```bash
ssh -t thor docker exec -it oxm-hogar python3 -m app.auth.admin add Nombre.Apellido     # pide la contraseña (mínimo 12)
ssh -t thor docker exec -it oxm-hogar python3 -m app.auth.admin rol Nombre.Apellido admin
```

- Al primer inicio de sesión se muestra un QR para el autenticador (Google Authenticator, 1Password...). Después, cada entrada
  pide usuario + contraseña y luego el código de 6 dígitos.
- Más cuentas, roles y bloqueos: `http://192.168.0.10:8095/admin` (solo admin).

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

- El token de Home Assistant se pega en **Ajustes**; se prueba antes de guardarse, queda en `data/ha.json` con permisos 600
  y nunca se devuelve al navegador (solo los últimos 4 caracteres). Solo el rol admin lo puede cambiar o borrar.
- Solo se permiten acciones de control (luces, enchufes, medios, cortinas, escenas, scripts, anuncios). No se puede reiniciar
  Home Assistant ni presionar botones de equipos (el NAS expone «reboot» y «shutdown»).
- Las órdenes que cambian algo exigen mismo origen: otra página abierta en el navegador no puede ordenar por ti.
- Tuya: el servicio de oapc-devops solo acepta los equipos de `config/iot.yml` (casa Patricio 10) y como mucho una orden por
  segundo por equipo.
- Cookies propias (`hogar_sesion`): iniciar o cerrar sesión aquí no afecta a thor-vision, aunque estén en el mismo host.

## Despliegue en la Thor

Código en `~/oxm-hogar/code` (solo `hogar/` y `app/auth`, `app/_vendor`), datos en `~/oxm-hogar/data`, variables en
`~/oxm-hogar/hogar.env` (600). Usa la misma imagen que thor-vision; no reconstruye nada.

```bash
docker run -d --name oxm-hogar --restart unless-stopped --network host \
  --env-file ~/oxm-hogar/hogar.env -e PYTHONPATH=/srv/code -w /srv/code \
  -v ~/oxm-hogar/code:/srv/code:ro -v ~/oxm-hogar/data:/srv/data \
  --entrypoint python3 thor-vision:latest -m uvicorn hogar.main:app --host 0.0.0.0 --port 8095
```

Variables de `hogar.env`:

| Variable | Valor |
|---|---|
| `HOGAR_DATA` / `THOR_AUTH_DB` | `/srv/data` / `/srv/data/auth.db` |
| `THOR_SESSION_SECRET` | aleatorio, 64 caracteres (distinto del de thor-vision) |
| `THOR_COOKIE_PREFIX` / `THOR_APP_NAME` | `hogar` / `OXM Hogar` |
| `THOR_MACHINE_TOKEN` + `THOR_MACHINE_EXTRA=/api/resumen` | token para que thor-vision lea el resumen |
| `TUYA_SERVICE_URL` | `http://192.168.0.194:8765` (servicio de control en oapc-devops) |
| `VISION_URL` (+ `VISION_TOKEN`) | `http://127.0.0.1:8080`; con el login de thor-vision activo hace falta su token de máquina y agregar `/api/iot` a su `THOR_MACHINE_EXTRA` |

Reflejo en thor-vision: `HOGAR_TOKEN` (= `THOR_MACHINE_TOKEN` de Hogar) y opcionalmente `HOGAR_URL` / `HOGAR_PUBLIC_URL` en su
`docker-compose.yml`. Sin `HOGAR_TOKEN` el indicador simplemente no aparece.

## Alexa y Fire TV

Hogar muestra automáticamente cualquier reproductor que exista en Home Assistant. Hoy Home Assistant no tiene Alexa y el Fire TV
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
