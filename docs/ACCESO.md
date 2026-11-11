# Acceso a THOR Vision

## Quién entra
Cuentas personales, creadas por TI. Hoy: `Diego.Reyes`, `Osvaldo`, `brayan.lopez`, `cesar.vazquez`, y Abraham con su acceso actual.
No hay registro público ni recuperación por correo. Las cuentas se pueden desactivar sin borrar su historial.

## Cómo funciona el inicio de sesión
1. **Usuario y contraseña** en la primera pantalla. Nada más.
2. **Primera vez:** la app muestra un código QR para registrar el autenticador (Google o Microsoft Authenticator).
   Se confirma con el primer código de 6 dígitos y entra al panel.
3. **Siguientes veces:** usuario y contraseña, y en una **pantalla aparte** el código de 6 dígitos.
4. Sesión de 8 horas. El botón de salir cierra la sesión.

Protecciones:
- 5 intentos fallidos desde la misma IP bloquean esa IP 5 minutos.
- 10 fallos contra una misma cuenta en 15 minutos la bloquean temporalmente (sin revelar si la cuenta existe).
- Cada intento queda en la bitácora de acceso (`data/auth.db`, tabla `audit`), con usuario e IP. Nunca se registra la contraseña ni el código.
- Sin sesión válida no se entra a ninguna ruta, ni a la API ni a los datos. Las páginas devuelven a la pantalla de login.

## Dónde entrar
- **En la oficina:** `http://<IP de la Thor>:8080`.
- **Fuera de la oficina:** por **VPN**. No se publica el panel en internet.

## Crear o administrar cuentas (sin dejar scripts en la Thor)
Se hace desde la terminal, con la contraseña escrita por la persona que la crea. No se guarda ningún archivo fuera de la base.

```bash
ssh -t igigi-jetson@<IP de la Thor> 'docker exec -it thor-vision python -m app.auth.admin add Diego.Reyes'
ssh -t igigi-jetson@<IP de la Thor> 'docker exec -it thor-vision python -m app.auth.admin list'
ssh -t igigi-jetson@<IP de la Thor> 'docker exec -it thor-vision python -m app.auth.admin disable Osvaldo'
ssh -t igigi-jetson@<IP de la Thor> 'docker exec -it thor-vision python -m app.auth.admin enable Osvaldo'
```

`admin.py` es código de la aplicación, no un script de acceso: por sí solo no abre nada, porque para entrar hacen falta
la contraseña y el código de una cuenta activa. Las cuentas viven en `data/auth.db` dentro del volumen de la Thor.

**Regla:** no dejar copias de herramientas, contraseñas ni listados de cuentas en el equipo. Si un script o nota
con credenciales queda en la Thor, se elimina en la misma sesión.

## Secretos (en `.env` de la Thor, permisos 600, fuera de git)
- `THOR_SESSION_SECRET`: firma las sesiones. Si falta o tiene menos de 32 caracteres, **nadie puede entrar** (falla cerrado).
- `THOR_MACHINE_TOKEN`: token de los servicios internos (señalización). Solo abre `/api/detections` y `/api/cameras`.
  Se configura igual en el servicio de señalización como `VISION_TOKEN`.

Generar con `openssl rand -hex 32`. No se muestran en chat ni en logs.

## Qué NO cubre esta versión (pendiente y visible)
- **Otros servicios en la red:** go2rtc (puerto 1984, video de cámaras), el webhook (8090) y el vision-agent (3200)
  siguen accesibles sin login en la red de la Thor. Cerrarlos o protegerlos es la siguiente entrega.
- **Secretos en el código:** la clave de la VLM, las contraseñas RTSP de `cameras.yml` y el código Tuya siguen en
  texto plano. Ya estuvieron en GitHub. Requieren rotación, que queda fuera de esta entrega por decisión.
- **TLS:** el acceso es HTTP dentro de la red. La contraseña viaja sin cifrar dentro de la LAN o la VPN.
