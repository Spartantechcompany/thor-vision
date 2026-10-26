"""Pantallas de acceso: usuario y contraseña; después el código (o el QR la primera vez)."""

import html
import io
import time
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import limiter, passwords, sessions, store, totp
from app.auth.guard import COOKIE_FULL

router = APIRouter()
COOKIE_PASO = "thor_paso"
_DUMMY = []


def _ip(request: Request) -> str:
    return request.client.host if request.client else "desconocida"


def _same_origin(request: Request) -> bool:
    sitio = request.headers.get("sec-fetch-site")
    if sitio is not None:
        return sitio in ("same-origin", "none")
    origen = request.headers.get("origin")
    return origen is None or (origen != "null" and urlsplit(origen).netloc == request.headers.get("host", ""))


def _page(titulo: str, cuerpo: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(titulo)}</title>
<style>body{{margin:0;background:#0E141B;color:#E6EDF3;font:16px system-ui,sans-serif;display:flex;
min-height:100vh;align-items:center;justify-content:center}} form,.caja{{background:#151D27;border:1px solid #243040;
border-radius:12px;padding:24px;width:min(92vw,380px);display:flex;flex-direction:column;gap:12px}}
input{{min-height:44px;background:#0E141B;color:#E6EDF3;border:1px solid #243040;border-radius:8px;padding:0 10px;font:inherit}}
button{{min-height:44px;background:#12332D;color:#E6EDF3;border:1px solid #6FE3C8;border-radius:8px;font:inherit}}
.err{{color:#FF9E96}} .pequeno{{color:#9AA8B7;font-size:13px}} img{{background:#fff;border-radius:8px;padding:8px}}
</style></head><body>{cuerpo}</body></html>"""
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store"})


def _login_form(error: str | None = None, status: int = 200) -> HTMLResponse:
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return _page("Acceso · THOR Vision", f"""<form method="post" action="/login">
<h1 style="font-size:20px;margin:0">THOR Vision</h1>{err}
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
        limiter.fail(ip, usuario or "-", now)
        store.log(ip, "login_fallido", f"usuario={usuario or '(vacío)'}")
        return _login_form("Usuario o contraseña incorrectos.", 401)
    limiter.ok(ip)
    store.log(ip, "contraseña_correcta", f"usuario={cuenta['username']}")
    destino = "/codigo" if cuenta["totp_enabled"] else "/enrolar"
    response = RedirectResponse(destino, status_code=303)
    _stage_cookie(response, cuenta["username"], sessions.PWD_MFA if cuenta["totp_enabled"] else sessions.PWD_ENROL)
    return response


@router.get("/codigo", response_class=HTMLResponse)
def codigo_page(request: Request):
    if _current_stage(request, sessions.PWD_MFA) is None:
        return RedirectResponse("/login", status_code=303)
    return _page("Código · THOR Vision", """<form method="post" action="/codigo">
<h1 style="font-size:20px;margin:0">Código de verificación</h1>
<label>Código de 6 dígitos de tu app <input name="codigo" inputmode="numeric" autocomplete="one-time-code"
pattern="[0-9]{6}" required maxlength="6"></label><button type="submit">Verificar</button>
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
        limiter.fail(ip, usuario, now)
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
    return _page("Registrar autenticador · THOR Vision", f"""<div class="caja">
<h1 style="font-size:20px;margin:0">Registra tu autenticador</h1>
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
    segno.make(totp.provisioning_uri(cuenta["username"], cuenta["totp_secret"]), error="m").save(
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
        limiter.fail(ip, usuario, now)
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
