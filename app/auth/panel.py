"""Panel de administración: quién tiene acceso, con qué rol y si está activo; bloqueos de IP y bitácora.

Solo el rol admin entra. Nunca se puede desactivar la propia cuenta ni dejar el sistema sin administrador activo.
Las cuentas nuevas se siguen creando desde la terminal (app/auth/admin.py), para que ninguna contraseña pase por el navegador.
"""

import html
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import limiter, sessions, store
from app.auth.guard import COOKIE_FULL
from app.auth.routes import APP_NAME, MARCA, _form_fields, _ip, _page, _same_origin

router = APIRouter(prefix="/admin")
ESTILO_PANEL = """<style>
.panel{width:min(96vw,980px);align-items:stretch;padding:20px;gap:18px}
.barra{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px}
.bloque{background:var(--card);border:1px solid var(--borde);border-radius:10px;padding:16px;overflow-x:auto}
.bloque h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.6px;color:var(--secundario);margin:0 0 10px}
.fila{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.fila select{min-height:36px}
.fila button{min-height:36px;padding:0 12px;font-size:.82rem}
</style>"""


def _admin(request: Request) -> dict | None:
    v = sessions.verify(request.cookies.get(COOKIE_FULL))
    if not v or v[1] != sessions.FULL:
        return None
    cuenta = store.get_user(v[0])
    return cuenta if cuenta and cuenta["active"] and cuenta["role"] == "admin" else None


def _aviso(texto: str, status: int = 409) -> HTMLResponse:
    return _page(f"Administración · {APP_NAME}", f'<div class="caja">{MARCA}<p class="err">{html.escape(texto)}</p>'
                                                 '<p><a href="/admin">Volver al panel</a></p></div>', status)


def _sin_permiso() -> HTMLResponse:
    return _page("Sin permiso", f'<div class="caja">{MARCA}<p class="err">Solo para administradores.</p>'
                                '<p><a href="/">Volver</a></p></div>', 403)


def _fecha(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def _celda_rol(u: dict) -> str:
    opciones = "".join(f'<option value="{r}"{" selected" if r == u["role"] else ""}>{r}</option>'
                       for r in store.ROLES)
    return (f'<form method="post" action="/admin/rol" class="fila"><input type="hidden" name="usuario" '
            f'value="{html.escape(u["username"])}"><select name="rol" aria-label="Rol de {html.escape(u["username"])}">'
            f'{opciones}</select><button type="submit" class="secundario">Cambiar</button></form>')


def _celda_activo(u: dict) -> str:
    if u["active"]:
        etiqueta, cambio, boton = '<span class="etiqueta on">activa</span>', "0", "Desactivar"
    else:
        etiqueta, cambio, boton = '<span class="etiqueta off">inactiva</span>', "1", "Activar"
    clase = "peligro" if u["active"] else "secundario"
    return (f'{etiqueta} <form method="post" action="/admin/activo" class="fila" style="display:inline-flex">'
            f'<input type="hidden" name="usuario" value="{html.escape(u["username"])}">'
            f'<input type="hidden" name="activo" value="{cambio}"><button type="submit" class="{clase}">{boton}</button></form>')


def _tabla_usuarios(admin: dict) -> str:
    filas = []
    for u in store.list_users():
        yo = " (tú)" if u["username"].lower() == admin["username"].lower() else ""
        autenticador = "registrado" if u["totp_enabled"] else "pendiente (QR en el primer inicio)"
        filas.append(f"<tr><td>{html.escape(u['username'])}{yo}</td><td>{_celda_rol(u)}</td>"
                     f"<td>{_celda_activo(u)}</td><td>{autenticador}</td><td>{_fecha(u['created_at'])}</td></tr>")
    return ('<div class="bloque"><h2>Personas con acceso</h2><table><thead><tr><th>Usuario</th><th>Rol</th>'
            '<th>Estado</th><th>Autenticador</th><th>Creada</th></tr></thead><tbody>' + "".join(filas) +
            '</tbody></table><p class="pequeno">Para agregar a alguien: comando en la terminal de la Thor, '
            'ver docs/ACCESO.md.</p></div>')


def _tabla_bloqueos() -> str:
    bloqueos = store.list_blocks()
    if not bloqueos:
        cuerpo = '<tr><td colspan="4" class="pequeno">Ninguna IP bloqueada.</td></tr>'
    else:
        cuerpo = "".join(
            f"<tr><td><code>{html.escape(b['ip'])}</code></td><td>{html.escape(b['motivo'])}</td>"
            f"<td>hasta {_fecha(b['hasta'])}</td><td><form method=\"post\" action=\"/admin/desbloquear\">"
            f"<input type=\"hidden\" name=\"ip\" value=\"{html.escape(b['ip'])}\">"
            f"<button type=\"submit\" class=\"secundario\">Desbloquear</button></form></td></tr>"
            for b in bloqueos)
    return ('<div class="bloque"><h2>IP bloqueadas</h2><table><thead><tr><th>IP</th><th>Motivo</th><th>Hasta</th>'
            '<th></th></tr></thead><tbody>' + cuerpo + '</tbody></table></div>')


def _tabla_bitacora() -> str:
    filas = "".join(
        f"<tr><td>{_fecha(a['at'])}</td><td><code>{html.escape(a['ip'])}</code></td>"
        f"<td>{html.escape(a['action'])}</td><td>{html.escape(a['detail'])}</td></tr>"
        for a in store.list_audit())
    return ('<div class="bloque"><h2>Bitácora reciente</h2><table><thead><tr><th>Cuándo</th><th>IP</th>'
            '<th>Acción</th><th>Detalle</th></tr></thead><tbody>' + filas + '</tbody></table></div>')


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel(request: Request):
    admin = _admin(request)
    if admin is None:
        return _sin_permiso()
    cuerpo = f"""{ESTILO_PANEL}<div class="caja panel">
<div class="barra">{MARCA}<span class="pequeno">Sesión: {html.escape(admin['username'])} · administrador</span>
<form method="post" action="/logout"><button type="submit" class="secundario">Salir</button></form></div>
<h1>Administración de acceso</h1>
{_tabla_usuarios(admin)}{_tabla_bloqueos()}{_tabla_bitacora()}</div>"""
    return _page(f"Administración · {APP_NAME}", cuerpo)


@router.post("/rol")
async def cambiar_rol(request: Request):
    if not _same_origin(request):
        return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
    admin = _admin(request)
    if admin is None:
        return _sin_permiso()
    datos = _form_fields(await request.body())
    cuenta = store.get_user(datos.get("usuario", ""))
    rol = datos.get("rol", "")
    if cuenta is None or rol not in store.ROLES:
        return _aviso("Cuenta o rol no válido.", 400)
    if cuenta["role"] == "admin" and rol != "admin" and store.admins_activos_sin(cuenta["username"]) == 0:
        return _aviso("No se puede quitar el rol de administrador: quedaría el sistema sin administrador activo.")
    store.set_role(cuenta["username"], rol)
    store.log(_ip(request), "rol_cambiado", f"{admin['username']} -> {cuenta['username']}: {rol}")
    return RedirectResponse("/admin", status_code=303)


@router.post("/activo")
async def cambiar_activo(request: Request):
    if not _same_origin(request):
        return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
    admin = _admin(request)
    if admin is None:
        return _sin_permiso()
    datos = _form_fields(await request.body())
    cuenta = store.get_user(datos.get("usuario", ""))
    activar = datos.get("activo") == "1"
    if cuenta is None or datos.get("activo") not in ("0", "1"):
        return _aviso("Cuenta no válida.", 400)
    if not activar:
        if cuenta["username"].lower() == admin["username"].lower():
            return _aviso("No puedes desactivar tu propia cuenta.")
        if cuenta["role"] == "admin" and store.admins_activos_sin(cuenta["username"]) == 0:
            return _aviso("No se puede desactivar al último administrador activo.")
    store.set_active(cuenta["username"], activar)
    accion = "cuenta_activada" if activar else "cuenta_desactivada"
    store.log(_ip(request), accion, f"{admin['username']} -> {cuenta['username']}")
    return RedirectResponse("/admin", status_code=303)


@router.post("/desbloquear")
async def desbloquear(request: Request):
    if not _same_origin(request):
        return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
    admin = _admin(request)
    if admin is None:
        return _sin_permiso()
    ip = (_form_fields(await request.body()).get("ip") or "")[:45]
    if ip:
        store.unblock_ip(ip)
        limiter.ok(ip)
        store.log(_ip(request), "ip_desbloqueada", f"{admin['username']} -> {ip}")
    return RedirectResponse("/admin", status_code=303)
