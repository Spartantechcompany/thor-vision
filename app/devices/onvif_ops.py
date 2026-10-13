"""Gestion por ONVIF (videoportero Dahua y cualquier equipo ONVIF): lecturas de red/hora/video/puerta y acciones controladas."""
import re
import xml.etree.ElementTree as ET

from app.onvif import client as oc

_NS_URI = {"tt": oc.NS["tt"], "trt": oc.NS["trt"], "tds": oc.NS["tds"], "tdc": oc.NS["tdc"]}
for _p, _u in _NS_URI.items():
    ET.register_namespace(_p, _u)

READS = ("network", "dns", "ntp", "hostname", "scopes", "encoders", "doors", "relays")
ACTIONS = {                                   # op -> (nivel, descripcion)
    "set_ntp": ("write", "Servidor NTP (DNS o IP)"),
    "set_encoder": ("strong", "Resolucion, fps y tasa de bits de un perfil de video"),
    "reboot": ("strong", "Reiniciar el equipo"),
    "door_unlock": ("strong", "Abrir la puerta (cerradura)"),
}
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9.-]{1,120}$")


class Ctx:
    """Conexion ya resuelta a un equipo ONVIF."""
    def __init__(self, host: str, port: int, user: str, password: str):
        self.host, self.port, self.user, self.pw = host, port, user, password
        self.off = oc.get_datetime(host, port)["skew_s"]
        self.services = oc.get_services(host, port, user, password, self.off)

    def url(self, svc: str) -> str:
        if svc == "device":
            return oc.device_url(self.host, self.port)
        if svc not in self.services:
            raise oc.OnvifError(f"el equipo no ofrece el servicio {svc}")
        return oc._rewrite(self.services[svc], self.host, self.port)

    def call(self, svc: str, body: str) -> ET.Element:
        return oc.soap(self.url(svc), body, self.user, self.pw, self.off)


def _t(e: ET.Element, name: str) -> str:
    for x in e.iter():
        if oc._strip(x.tag) == name and x.text:
            return x.text.strip()
    return ""


def _all(e: ET.Element, name: str) -> list:
    return [x for x in e.iter() if oc._strip(x.tag) == name]


def read(ctx: Ctx, op: str) -> dict:
    if op == "network":
        r = ctx.call("device", "<tds:GetNetworkInterfaces/>")
        return {"interfaces": [{"name": _t(i, "Name"), "mac": _t(i, "HwAddress"), "enabled": _t(i, "Enabled"),
                                "ip": _t(i, "Address"), "prefix": _t(i, "PrefixLength"), "dhcp": _t(i, "DHCP")} for i in _all(r, "NetworkInterfaces")]}
    if op == "dns":
        r = ctx.call("device", "<tds:GetDNS/>")
        return {"from_dhcp": _t(r, "FromDHCP"), "servers": [a.text for a in _all(r, "IPv4Address") if a.text]}
    if op == "ntp":
        r = ctx.call("device", "<tds:GetNTP/>")
        return {"from_dhcp": _t(r, "FromDHCP"), "servers": [x.text for x in _all(r, "IPv4Address") + _all(r, "DNSname") if x.text]}
    if op == "hostname":
        return {"name": _t(ctx.call("device", "<tds:GetHostname/>"), "Name")}
    if op == "scopes":
        return {"scopes": [x.text for x in _all(ctx.call("device", "<tds:GetScopes/>"), "ScopeItem") if x.text]}
    if op == "encoders":
        r = ctx.call("media", "<trt:GetVideoEncoderConfigurations/>")
        return {"encoders": [{"token": c.attrib.get("token", ""), "name": _t(c, "Name"), "encoding": _t(c, "Encoding"),
                              "width": _t(c, "Width"), "height": _t(c, "Height"), "fps": _t(c, "FrameRateLimit"),
                              "bitrate": _t(c, "BitrateLimit"), "gop": _t(c, "GovLength")} for c in _all(r, "Configurations")]}
    if op == "doors":
        r = ctx.call("doorcontrol", "<tdc:GetDoorList/>")
        doors = []
        for d in _all(r, "Door"):
            tok = d.attrib.get("token", "")
            state = ""
            if tok:
                try:
                    state = _t(ctx.call("doorcontrol", f"<tdc:GetDoorState><tdc:Token>{tok}</tdc:Token></tdc:GetDoorState>"), "DoorMode")
                except oc.OnvifError:
                    state = "?"
            doors.append({"token": tok, "name": _t(d, "Name"), "mode": state})
        return {"doors": doors}
    if op == "relays":
        r = ctx.call("device", "<tds:GetRelayOutputs/>")
        return {"relays": [{"token": x.attrib.get("token", ""), "mode": _t(x, "Mode"), "idle": _t(x, "IdleState")} for x in _all(r, "RelayOutputs")]}
    raise oc.OnvifError(f"lectura desconocida: {op}")


def act(ctx: Ctx, op: str, params: dict) -> dict:
    if op == "set_ntp":
        host = str(params.get("server", "")).strip()
        if not _HOSTNAME.match(host):
            raise oc.OnvifError("servidor NTP invalido")
        kind = "IPv4" if re.match(r"^\d+\.\d+\.\d+\.\d+$", host) else "DNS"
        tag = "IPv4Address" if kind == "IPv4" else "DNSname"
        ctx.call("device", f"<tds:SetNTP><tds:FromDHCP>false</tds:FromDHCP><tds:NTPManual><tt:Type>{kind}</tt:Type><tt:{tag}>{host}</tt:{tag}></tds:NTPManual></tds:SetNTP>")
        return {"ok": True}
    if op == "reboot":
        return {"ok": True, "message": _t(ctx.call("device", "<tds:SystemReboot/>"), "Message")}
    if op == "door_unlock":
        tok = str(params.get("token", ""))
        if not _TOKEN.match(tok):
            raise oc.OnvifError("token de puerta invalido")
        ctx.call("doorcontrol", f"<tdc:AccessDoor><tdc:Token>{tok}</tdc:Token></tdc:AccessDoor>")
        return {"ok": True}
    if op == "set_encoder":
        tok = str(params.get("token", ""))
        if not _TOKEN.match(tok):
            raise oc.OnvifError("token de perfil invalido")
        r = ctx.call("media", f"<trt:GetVideoEncoderConfiguration><trt:ConfigurationToken>{tok}</trt:ConfigurationToken></trt:GetVideoEncoderConfiguration>")
        cfg = next(iter(_all(r, "Configuration")), None)
        if cfg is None:
            raise oc.OnvifError("el equipo no devolvio el perfil")
        for field, tag in (("width", "Width"), ("height", "Height"), ("fps", "FrameRateLimit"), ("bitrate", "BitrateLimit")):
            if params.get(field) not in (None, ""):
                v = str(params[field])
                if not v.isdigit() or not 1 <= int(v) <= 100000:
                    raise oc.OnvifError(f"{field}: valor invalido")
                for x in _all(cfg, tag):
                    x.text = v
        cfg.tag = f"{{{oc.NS['trt']}}}Configuration"
        body = ("<trt:SetVideoEncoderConfiguration>" + ET.tostring(cfg, encoding="unicode") +
                "<trt:ForcePersistence>true</trt:ForcePersistence></trt:SetVideoEncoderConfiguration>")
        ctx.call("media", body)
        return {"ok": True}
    raise oc.OnvifError(f"accion desconocida: {op}")
