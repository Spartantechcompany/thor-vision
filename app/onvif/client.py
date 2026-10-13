"""Cliente ONVIF minimo (SOAP + WS-Security UsernameToken digest), sin dependencias.

Solo lectura por defecto: informacion del equipo, hora, perfiles de video, URIs de stream y eventos (PullPoint).
Las credenciales nunca se devuelven ni se registran; las URIs de stream se limpian antes de salir.
"""
import base64
import datetime as dt
import hashlib
import os
import re
import urllib.error
import ssl
import urllib.request
import xml.etree.ElementTree as ET

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "tdc": "http://www.onvif.org/ver10/doorcontrol/wsdl",
}
TIMEOUT = 8


class OnvifError(Exception):
    pass


def _strip(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _envelope(body: str, user: str = "", password: str = "", offset: float = 0.0) -> bytes:
    header = ""
    if user:
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        header = ('<s:Header><Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
                  'xmlns:u="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
                  f'<UsernameToken><Username>{_esc(user)}</Username>'
                  '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
                  f'{digest}</Password><Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
                  f'{base64.b64encode(nonce).decode()}</Nonce><u:Created>{created}</u:Created></UsernameToken></Security></s:Header>')
    ns = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
    return f'<?xml version="1.0" encoding="utf-8"?><s:Envelope {ns}>{header}<s:Body>{body}</s:Body></s:Envelope>'.encode()


_INSECURE = ssl._create_unverified_context()     # camaras con certificado propio (ej. cam-236); es la red interna


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def soap(url: str, body: str, user: str = "", password: str = "", offset: float = 0.0, timeout: float = TIMEOUT) -> ET.Element:
    req = urllib.request.Request(url, data=_envelope(body, user, password, offset),
                                 headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        raw = urllib.request.urlopen(req, timeout=timeout, context=_INSECURE if url.startswith("https") else None).read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            root = ET.fromstring(raw)
            fault = next((e.text for e in root.iter() if _strip(e.tag) in ("Text", "faultstring") and e.text), f"HTTP {exc.code}")
        except ET.ParseError:
            fault = f"HTTP {exc.code}"
        raise OnvifError(fault) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OnvifError(f"sin respuesta: {getattr(exc, 'reason', exc)}") from exc
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise OnvifError("respuesta no es XML") from exc
    for e in root.iter():
        if _strip(e.tag) == "Fault":
            msg = next((x.text for x in e.iter() if _strip(x.tag) in ("Text", "faultstring") and x.text), "fault")
            raise OnvifError(msg)
    return root


def _text(root: ET.Element, name: str) -> str:
    for e in root.iter():
        if _strip(e.tag) == name and e.text:
            return e.text.strip()
    return ""


def clean_uri(uri: str) -> str:
    """Quita usuario:clave de una URI (rtsp://user:pass@host/...)."""
    return re.sub(r"^(\w+://)[^/@]*@", r"\1", uri or "")


def device_url(host: str, port: int = 80, path: str = "/onvif/device_service") -> str:
    return f"http://{host}:{port}{path}"


def get_datetime(host: str, port: int = 80) -> dict:
    """Hora del equipo (sin autenticacion, como permite ONVIF) y desfase contra este servidor."""
    r = soap(device_url(host, port), "<tds:GetSystemDateAndTime/>")
    g = {k: int(_text(r, k) or 0) for k in ("Year", "Month", "Day", "Hour", "Minute", "Second")}
    # el primer bloque UTCDateTime es el que importa
    utc = None
    for e in r.iter():
        if _strip(e.tag) == "UTCDateTime":
            vals = {_strip(c.tag): c for c in e.iter()}
            t = {k: int(vals[k].text) for k in ("Year", "Month", "Day", "Hour", "Minute", "Second") if k in vals}
            utc = dt.datetime(t["Year"], t["Month"], t["Day"], t["Hour"], t["Minute"], t["Second"], tzinfo=dt.timezone.utc)
            break
    if utc is None:
        utc = dt.datetime(g["Year"], g["Month"], g["Day"], g["Hour"], g["Minute"], g["Second"], tzinfo=dt.timezone.utc)
    skew = (utc - dt.datetime.now(dt.timezone.utc)).total_seconds()
    return {"utc": utc.isoformat(), "skew_s": round(skew, 1), "tz": _text(r, "TZ"), "ntp": _text(r, "DateTimeType")}


def get_device_info(host: str, port: int, user: str, password: str, offset: float = 0.0) -> dict:
    r = soap(device_url(host, port), "<tds:GetDeviceInformation/>", user, password, offset)
    return {k: _text(r, v) for k, v in (("manufacturer", "Manufacturer"), ("model", "Model"), ("firmware", "FirmwareVersion"),
                                        ("serial", "SerialNumber"), ("hardware", "HardwareId"))}


def get_services(host: str, port: int, user: str, password: str, offset: float = 0.0) -> dict:
    """namespace -> XAddr de cada servicio (media, events, ptz...)."""
    r = soap(device_url(host, port), "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>", user, password, offset)
    out = {}
    for svc in r.iter():
        if _strip(svc.tag) == "Service":
            ns = next((c.text for c in svc if _strip(c.tag) == "Namespace"), "")
            xa = next((c.text for c in svc if _strip(c.tag) == "XAddr"), "")
            if ns and xa:
                out[ns.rsplit("/", 2)[-2] if ns.endswith("/wsdl") else ns] = xa
    return out


def _rewrite(xaddr: str, host: str, port: int) -> str:
    """Algunos equipos anuncian su IP interna o 127.0.0.1: se usa el host con el que ya conectamos."""
    return re.sub(r"^(http://)[^/]+", rf"\g<1>{host}:{port}", xaddr)


def get_profiles(media_url: str, user: str, password: str, offset: float = 0.0) -> list:
    r = soap(media_url, "<trt:GetProfiles/>", user, password, offset)
    out = []
    for p in r.iter():
        if _strip(p.tag) != "Profiles":
            continue
        item = {"token": p.attrib.get("token", ""), "name": "", "video": {}, "audio": False, "ptz": False}
        for c in p.iter():
            t = _strip(c.tag)
            if t == "Name" and not item["name"]:
                item["name"] = (c.text or "").strip()
            elif t == "VideoEncoderConfiguration":
                v = {_strip(x.tag): x for x in c.iter()}
                item["video"] = {"encoding": (v["Encoding"].text if "Encoding" in v else ""),
                                 "width": int(v["Width"].text) if "Width" in v else 0, "height": int(v["Height"].text) if "Height" in v else 0,
                                 "fps": float(v["FrameRateLimit"].text) if "FrameRateLimit" in v else 0,
                                 "bitrate": int(v["BitrateLimit"].text) if "BitrateLimit" in v else 0}
            elif t == "AudioEncoderConfiguration":
                item["audio"] = True
            elif t == "PTZConfiguration":
                item["ptz"] = True
        out.append(item)
    return out


def get_stream_uri(media_url: str, token: str, user: str, password: str, offset: float = 0.0) -> str:
    body = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            f"</trt:StreamSetup><trt:ProfileToken>{_esc(token)}</trt:ProfileToken></trt:GetStreamUri>")
    return clean_uri(_text(soap(media_url, body, user, password, offset), "Uri"))


def get_users(host: str, port: int, user: str, password: str, offset: float = 0.0) -> list:
    r = soap(device_url(host, port), "<tds:GetUsers/>", user, password, offset)
    return [{"name": _text(u, "Username"), "level": _text(u, "UserLevel")} for u in r.iter() if _strip(u.tag) == "User"]


def open_pullpoint(events_url: str, user: str, password: str, offset: float = 0.0, ttl_s: int = 120) -> str:
    """Crea la suscripcion PullPoint y devuelve la URL desde la que se leen los eventos."""
    sub = soap(events_url, f"<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT{int(ttl_s)}S</tev:InitialTerminationTime></tev:CreatePullPointSubscription>",
               user, password, offset)
    addr = next((e.text.strip() for e in sub.iter() if _strip(e.tag) == "Address" and e.text and "http" in e.text), "")
    if not addr:
        raise OnvifError("el equipo no devolvio la direccion del PullPoint")
    host = re.match(r"https?://([^/]+)", events_url).group(1)       # se usa el host con el que ya conectamos
    return re.sub(r"^(https?://)[^/]+", rf"\g<1>{host}", addr)


def pull(pull_url: str, user: str, password: str, seconds: int = 10, offset: float = 0.0) -> list:
    # el equipo retiene la consulta hasta `seconds` si no hay eventos: el tiempo de espera del cliente debe ser mayor
    r = soap(pull_url, f"<tev:PullMessages><tev:Timeout>PT{int(seconds)}S</tev:Timeout><tev:MessageLimit>50</tev:MessageLimit></tev:PullMessages>",
             user, password, offset, timeout=int(seconds) + TIMEOUT)
    out = []
    for msg in r.iter():
        if _strip(msg.tag) != "NotificationMessage":
            continue
        topic = next((c.text for c in msg if _strip(c.tag) == "Topic" and c.text), "")
        data = {}
        for it in msg.iter():
            if _strip(it.tag) == "SimpleItem" and it.attrib.get("Name"):
                data[it.attrib["Name"]] = it.attrib.get("Value", "")
        out.append({"topic": topic.strip(), "data": data})
    return out


def pull_events(events_url: str, user: str, password: str, seconds: int = 10, offset: float = 0.0) -> list:
    """Atajo: se suscribe y devuelve los eventos que lleguen en `seconds`."""
    return pull(open_pullpoint(events_url, user, password, offset), user, password, seconds, offset)
