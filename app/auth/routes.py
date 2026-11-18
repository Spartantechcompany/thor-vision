"""Pantallas de acceso: usuario y contraseña; después el código (o el QR la primera vez)."""

import html
import io
import os
import time
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import limiter, passwords, sessions, store, totp
from app.auth.guard import COOKIE_FULL, COOKIE_PREFIX

router = APIRouter()
COOKIE_PASO = f"{COOKIE_PREFIX}_paso"
APP_NAME = os.environ.get("THOR_APP_NAME", "THOR Vision")
_DUMMY = []


def _ip(request: Request) -> str:
    return request.client.host if request.client else "desconocida"


def _same_origin(request: Request) -> bool:
    sitio = request.headers.get("sec-fetch-site")
    if sitio is not None:
        return sitio in ("same-origin", "none")
    origen = request.headers.get("origin")
    return origen is None or (origen != "null" and urlsplit(origen).netloc == request.headers.get("host", ""))


ESTILO = """:root{--bg:#0E1117;--card:#161B24;--panel:#1C2130;--borde:#252C3A;--texto:#E8EDF3;
--secundario:#8B949E;--apagado:#484F58;--acento:#76B900;--error:#F85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--texto);font:15px system-ui,-apple-system,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}
.caja{background:var(--card);border:1px solid var(--borde);border-top:3px solid var(--acento);border-radius:10px;
padding:24px;width:min(92vw,380px);display:flex;flex-direction:column;gap:14px}
.marca{font-size:1.15rem;font-weight:700;letter-spacing:-0.5px}
.marca .oxm{color:#595959}.marca .tech{color:#1E78C2}
h1{font-size:1rem;font-weight:600;margin:0;color:var(--texto)}
label{display:flex;flex-direction:column;gap:6px;font-size:.8rem;color:var(--secundario);text-transform:uppercase;
letter-spacing:.6px;font-weight:600}
input,select{min-height:44px;background:var(--bg);color:var(--texto);border:1px solid var(--borde);border-radius:6px;
padding:0 12px;font:inherit;letter-spacing:normal;text-transform:none}
input:focus,select:focus{outline:none;border-color:var(--acento)}
button{min-height:44px;background:var(--acento);color:var(--bg);border:0;border-radius:6px;font:inherit;font-weight:600;
cursor:pointer}
button.secundario{background:transparent;color:var(--texto);border:1px solid var(--borde);font-weight:500}
button.peligro{background:transparent;color:var(--error);border:1px solid var(--error);font-weight:500}
.err{color:var(--error);margin:0}.pequeno{color:var(--secundario);font-size:.82rem;margin:0}
img{background:#fff;border-radius:8px;padding:8px;align-self:center}
code{background:var(--bg);border:1px solid var(--borde);border-radius:4px;padding:2px 6px;color:var(--texto);
word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--borde);vertical-align:middle}
th{color:var(--secundario);font-size:.72rem;text-transform:uppercase;letter-spacing:.6px}
.etiqueta{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.75rem;border:1px solid var(--borde)}
.on{color:var(--acento);border-color:var(--acento)}.off{color:var(--error);border-color:var(--error)}
a{color:var(--acento)}
@media(max-width:600px){th:nth-child(n+4),td:nth-child(n+4){display:none}}
"""
MARCA = '<div class="marca"><span class="oxm">oxm</span><span class="tech">tech</span> &middot; ' + html.escape(APP_NAME) + '</div>'


def _page(titulo: str, cuerpo: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(titulo)}</title>
<style>{ESTILO}</style></head><body>{cuerpo}</body></html>"""
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store"})


def _login_form(error: str | None = None, status: int = 200) -> HTMLResponse:
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return _page(f"Acceso · {APP_NAME}", f"""<form method="post" action="/login" class="caja">
{MARCA}<h1>Acceso al panel</h1>{err}
<label>Usuario <input name="usuario" autocomplete="username" required maxlength="40"></label>
<label>Contraseña <input name="password" type="password" autocomplete="current-password" required maxlength="200"></label>
<button type="submit">Entrar</button><p class="pequeno">Solo personal autorizado. Cada intento queda registrado.</p>
</form>""", status)


def _not_configured() -> HTMLResponse:
    return _page("Acceso no configurado", '<div class="caja"><p class="err">El acceso no está configurado en este '
                                          'servidor. Contacte a TI.</p></div>', 503)


def _form_fields(body: bytes) -> dict:
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


def _stage_cookie(response, username: str, stage: str) -> None:
    response.set_cookie(COOKIE_PASO, sessions.sign(username, stage, sessions.STAGE_TTL_S),
                        max_age=sessions.STAGE_TTL_S, httponly=True, samesite="strict", path="/")


def _current_stage(request: Request, wanted: str) -> str | None:
    v = sessions.verify(request.cookies.get(COOKIE_PASO))
    if not v or v[1] != wanted:
        return None
    if store.get_user(v[0]) is None or not store.get_user(v[0])["active"]:
        return None
    return v[0]


def _fallo(ip: str, usuario: str, now: float, desconocido: bool = False) -> None:
    """Registra un fallo y, si se repite, bloquea la IP en la base (persiste tras reiniciar)."""
    limiter.fail(ip, usuario, now)
    if desconocido and limiter.unknown_fail(ip, now) >= limiter.DESCONOCIDO_MAX:
        store.block_ip(ip, "usuario desconocido repetido", 3600)
        store.log(ip, "ip_bloqueada", "usuario desconocido repetido (1 h)")
    elif limiter.ip_blocked(ip, now):
        store.block_ip(ip, "fallos repetidos", 1800)
        store.log(ip, "ip_bloqueada", "fallos repetidos (30 min)")


def _full_cookie(response, username: str) -> None:
    response.set_cookie(COOKIE_FULL, sessions.sign(username, sessions.FULL, sessions.FULL_TTL_S),
                        max_age=sessions.FULL_TTL_S, httponly=True, samesite="strict", path="/")
    response.delete_cookie(COOKIE_PASO, path="/")


@router.get("/login", response_class=HTMLResponse)
def login_page():
    return _login_form() if sessions.configured() else _not_configured()


@router.post("/login")
async def login(request: Request):
    if not sessions.configured():
        return _not_configured()
    if not _same_origin(request):
        return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
    ip, now = _ip(request), time.time()
    if limiter.ip_blocked(ip, now):
        return _login_form("Demasiados intentos. Espera 5 minutos.", 429)
    datos = _form_fields(await request.body())
    usuario = (datos.get("usuario") or "")[:40]
    clave = (datos.get("password") or "")[:200]
    cuenta = store.get_user(usuario) if usuario else None
    if not _DUMMY:
        _DUMMY.append(passwords.hash_password("inutil-" + "x" * 16))
    ok_clave = passwords.verify_password(clave, cuenta["password_hash"] if cuenta else _DUMMY[0])
    bloqueada = bool(usuario) and limiter.user_blocked(usuario, now)
    if not (cuenta and cuenta["active"] and ok_clave and not bloqueada):
        _fallo(ip, usuario or "-", now, desconocido=bool(usuario) and cuenta is None)
        store.log(ip, "login_fallido", f"usuario={usuario or '(vacío)'}")
        return _login_form("Usuario o contraseña incorrectos.", 401)
    limiter.ok(ip)
    store.log(ip, "contraseña_correcta", f"usuario={cuenta['username']}")
    if cuenta.get("sin_2fa"):
        store.log(ip, "sesion_iniciada", f"usuario={cuenta['username']} (sin 2FA)")
        response = RedirectResponse("/", status_code=303)
        _full_cookie(response, cuenta["username"])
        return response
    destino = "/codigo" if cuenta["totp_enabled"] else "/enrolar"
    response = RedirectResponse(destino, status_code=303)
    _stage_cookie(response, cuenta["username"], sessions.PWD_MFA if cuenta["totp_enabled"] else sessions.PWD_ENROL)
    return response


@router.get("/codigo", response_class=HTMLResponse)
def codigo_page(request: Request):
    if _current_stage(request, sessions.PWD_MFA) is None:
        return RedirectResponse("/login", status_code=303)
    return _page(f"Código · {APP_NAME}", f"""<form method="post" action="/codigo" class="caja">
{MARCA}<h1>Código de verificación</h1>
<label>Código de 6 dígitos de tu app <input name="codigo" inputmode="numeric" autocomplete="one-time-code"
pattern="[0-9]{{6}}" required maxlength="6"></label><button type="submit">Verificar</button>
<p class="pequeno">Si no tienes el código, pide a TI que revise tu autenticador.</p></form>""")


@router.post("/codigo")
async def codigo(request: Request):
    if not _same_origin(request):
        return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
    usuario = _current_stage(request, sessions.PWD_MFA)
    if usuario is None:
        return RedirectResponse("/login", status_code=303)
    ip, now = _ip(request), time.time()
    if limiter.ip_blocked(ip, now):
        return _page("Espera", '<div class="caja"><p class="err">Demasiados intentos. Espera 5 minutos.</p></div>', 429)
    cuenta = store.get_user(usuario)
    codigo_ = (_form_fields(await request.body()).get("codigo") or "")[:10]
    if not totp.verify(cuenta["totp_secret"] or "", codigo_, now):
        _fallo(ip, usuario, now)
        store.log(ip, "codigo_incorrecto", f"usuario={usuario}")
        return _page("Código", '<div class="caja"><p class="err">Código incorrecto.</p>'
                               '<p><a href="/login">Volver</a></p></div>', 401)
    limiter.ok(ip)
    store.log(ip, "sesion_iniciada", f"usuario={usuario}")
    response = RedirectResponse("/", status_code=303)
    _full_cookie(response, usuario)
    return response


@router.get("/enrolar", response_class=HTMLResponse)
def enrolar_page(request: Request):
    usuario = _current_stage(request, sessions.PWD_ENROL)
    if usuario is None:
        return RedirectResponse("/login", status_code=303)
    cuenta = store.get_user(usuario)
    if not cuenta["totp_secret"]:
        store.set_pending_secret(usuario, totp.new_secret())
        cuenta = store.get_user(usuario)
    clave = html.escape(cuenta["totp_secret"])
    return _page(f"Registrar autenticador · {APP_NAME}", f"""<div class="caja">
{MARCA}<h1>Registra tu autenticador</h1>
<p class="pequeno">Cuenta: {html.escape(usuario)}. Hazlo una sola vez. Escanea el QR con Google o Microsoft Authenticator
(cuenta «basada en hora»).</p>
<img src="/enrolar/qr.svg" alt="Código QR" width="220" height="220">
<p class="pequeno">Si no puedes escanear, clave manual: <code>{clave}</code></p>
<form method="post" action="/enrolar" style="display:flex;flex-direction:column;gap:12px">
<label>Código de 6 dígitos que muestra la app <input name="codigo" inputmode="numeric" autocomplete="one-time-code"
pattern="[0-9]{{6}}" required maxlength="6"></label><button type="submit">Confirmar</button></form></div>""")


@router.get("/enrolar/qr.svg")
def enrolar_qr(request: Request):
    from app._vendor import segno
    usuario = _current_stage(request, sessions.PWD_ENROL)
    cuenta = store.get_user(usuario) if usuario else None
    if not cuenta or not cuenta["totp_secret"]:
        return Response(status_code=404)
    buf = io.BytesIO()
    segno.make(totp.provisioning_uri(cuenta["username"], cuenta["totp_secret"], APP_NAME), error="m").save(
        buf, kind="svg", scale=6, border=2)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.post("/enrolar")
async def enrolar(request: Request):
    if not _same_origin(request):
        return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
    usuario = _current_stage(request, sessions.PWD_ENROL)
    if usuario is None:
        return RedirectResponse("/login", status_code=303)
    ip, now = _ip(request), time.time()
    cuenta = store.get_user(usuario)
    codigo_ = (_form_fields(await request.body()).get("codigo") or "")[:10]
    if not totp.verify(cuenta["totp_secret"] or "", codigo_, now):
        _fallo(ip, usuario, now)
        store.log(ip, "enrolar_fallido", f"usuario={usuario}")
        return _page("Registrar", '<div class="caja"><p class="err">El código no coincide. Revisa la hora de tu '
                                  'teléfono.</p><p><a href="/enrolar">Volver</a></p></div>', 401)
    store.enable_totp(usuario)
    store.log(ip, "autenticador_activado", f"usuario={usuario}")
    response = RedirectResponse("/", status_code=303)
    _full_cookie(response, usuario)
    return response


@router.post("/logout")
async def logout(request: Request):
    if not _same_origin(request):
        return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_FULL, path="/")
    response.delete_cookie(COOKIE_PASO, path="/")
    return response
