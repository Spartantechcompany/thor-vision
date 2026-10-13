"""Gestion de dispositivos por marca: resumen, lecturas, acciones con politica (modo gestion, confirmacion) y bitacora."""
import json
import logging
import sqlite3
import threading
import time
import urllib.parse

from app.devices import onvif_ops
from app.devices import sunapi as sn
from app.onvif import client as oc
from app.onvif import registry

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, device TEXT NOT NULL, brand TEXT, kind TEXT NOT NULL,
  action TEXT NOT NULL, params TEXT, ok INTEGER NOT NULL, detail TEXT, actor TEXT);
CREATE INDEX IF NOT EXISTS idx_devact_ts ON device_actions(ts);
"""


class PolicyError(Exception):
    """La accion no esta permitida (HTTP 403/400 en la API)."""


class DeviceManager:
    def __init__(self, db, config):
        self.db, self.config = db, config
        self._info: dict = {}
        self._lock = threading.Lock()
        with db._lock:
            db._conn.executescript(_SCHEMA)

    # ---- dispositivos
    def _camera_cfg(self, dev_id: str):
        return next((c for c in self.config.cameras if c.id == dev_id), None)

    def _sunapi(self, dev_id: str) -> sn.Sunapi:
        c = self._camera_cfg(dev_id)
        if c is None or c.rtsp_url.startswith("onvif://"):
            raise sn.SunapiError("este equipo no se administra por SUNAPI")
        u = urllib.parse.urlparse(c.rtsp_url)
        return sn.Sunapi(u.hostname, u.username or "", u.password or "")

    def _onvif_ctx(self, dev_id: str) -> onvif_ops.Ctx:
        ep = registry.get(self.config, dev_id)
        user, pw = registry.auth(dev_id)
        if ep is None or not user:
            raise oc.OnvifError("sin usuario ONVIF configurado")
        return onvif_ops.Ctx(ep["host"], int(ep.get("port", 80)), user, pw)

    def brand(self, dev_id: str) -> str:
        ep = registry.get(self.config, dev_id)
        if ep is None:
            raise KeyError(dev_id)
        if ep["kind"] == "camera":
            return "hanwha"
        manu = (self._info.get(dev_id) or {}).get("manufacturer", "")
        return "dahua" if "dahua" in manu.lower() else "onvif"

    def summary(self, dev_id: str, refresh: bool = False) -> dict:
        """Resumen cacheado (10 min): marca, modelo, firmware, hora."""
        with self._lock:
            hit = self._info.get(dev_id)
        if hit and not refresh and time.time() - hit.get("_ts", 0) < 600:
            return hit
        ep = registry.get(self.config, dev_id)
        info: dict = {"_ts": time.time(), "reachable": False}
        try:
            if ep["kind"] == "camera":
                r = self._sunapi(dev_id).call("system", "deviceinfo", "view")
                kv = r["kv"]
                info.update(reachable=r["status"] == 200, manufacturer="Hanwha", model=kv.get("Model", ""),
                            firmware=kv.get("FirmwareVersion", ""), serial=kv.get("SerialNumber", ""),
                            onvif_version=kv.get("ONVIFVersion", ""), cgi_version=kv.get("CGIVersion", ""))
                d = self._sunapi(dev_id).call("system", "date", "view")["kv"]
                info.update(local_time=d.get("LocalTime", ""), sync=d.get("SyncType", ""))
            else:
                ctx = self._onvif_ctx(dev_id)
                di = oc.get_device_info(ep["host"], int(ep.get("port", 80)), ctx.user, ctx.pw, ctx.off)
                info.update(reachable=True, manufacturer=di.get("manufacturer", ""), model=di.get("model", ""),
                            firmware=di.get("firmware", ""), serial=di.get("serial", ""))
        except (sn.SunapiError, oc.OnvifError, OSError) as exc:
            info["error"] = str(exc)[:120]
        with self._lock:
            self._info[dev_id] = info
        return info

    def devices(self) -> list:
        out = []
        hidden = {c.id for c in self.config.cameras if str(c.rtsp_url).startswith("onvif://")}      # ya aparecen como equipo propio
        for ep in registry.endpoints(self.config):
            if ep["id"] in hidden:
                continue
            s = self.summary(ep["id"])
            out.append({"id": ep["id"], "name": ep["name"], "kind": ep["kind"], "host": ep["host"], "zone": ep.get("zone"),
                        "brand": self.brand(ep["id"]), "model": s.get("model", ""), "firmware": s.get("firmware", ""),
                        "reachable": s.get("reachable", False), "error": s.get("error", ""), "manage": registry.is_manage(ep["id"])})
        return out

    # ---- bitacora
    def audit(self, dev_id: str, kind: str, action: str, params: dict, ok: bool, detail: str, actor: str) -> None:
        with self.db._lock:
            self.db._conn.execute("INSERT INTO device_actions (ts, device, brand, kind, action, params, ok, detail, actor) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (time.time(), dev_id, self.brand(dev_id), kind, action, json.dumps(sn.redact(params), ensure_ascii=False),
                                   int(ok), detail[:300], actor))
            self.db._conn.commit()

    def audit_list(self, dev_id: str = "", limit: int = 100) -> list:
        sql, args = "SELECT id, ts, device, brand, kind, action, params, ok, detail, actor FROM device_actions", []
        if dev_id:
            sql += " WHERE device=?"
            args.append(dev_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(limit, 500))
        with self.db._lock:
            rows = self.db._conn.execute(sql, args).fetchall()
        return [{"id": i, "ts": t, "device": d, "brand": b, "kind": k, "action": a, "params": json.loads(p or "{}"), "ok": bool(o), "detail": de, "actor": ac}
                for i, t, d, b, k, a, p, o, de, ac in rows]

    # ---- politica comun
    def _authorize(self, dev_id: str, level: str, confirm: str) -> None:
        if level == "deny":
            raise PolicyError("esta accion no se permite desde aqui (se hace en la interfaz propia del equipo)")
        if not registry.is_manage(dev_id):
            raise PolicyError("el equipo esta en modo solo lectura: activa el modo gestion para modificarlo")
        if level == "strong" and confirm != dev_id:
            raise PolicyError(f"accion sensible: escribe el id del equipo ({dev_id}) para confirmar")
        if level == "write" and not confirm:
            raise PolicyError("confirma la accion")

    # ---- Hanwha (SUNAPI)
    def sunapi_tree(self, dev_id: str) -> dict:
        cat = self._sunapi(dev_id).catalog()
        return {cgi: {sm: [{"action": a, "access": spec["access"], "class": sn.classify(cgi, sm, a), "params": len([p for p in spec["params"] if p["request"]])}
                           for a, spec in acts.items()] for sm, acts in sms.items()} for cgi, sms in cat.items()}

    def sunapi_spec(self, dev_id: str, cgi: str, sm: str, action: str) -> dict:
        spec = self._sunapi(dev_id).spec(cgi, sm, action)
        return {"access": spec["access"], "class": sn.classify(cgi, sm, action), "params": spec["params"]}

    def sunapi_view(self, dev_id: str, cgi: str, sm: str, params: dict | None = None) -> dict:
        cli = self._sunapi(dev_id)
        cli.spec(cgi, sm, "view")
        return cli.call(cgi, sm, "view", params or {})

    def sunapi_act(self, dev_id: str, cgi: str, sm: str, action: str, params: dict, confirm: str, actor: str) -> dict:
        cli = self._sunapi(dev_id)
        level = sn.classify(cgi, sm, action)
        self._authorize(dev_id, level, confirm)
        try:
            clean = sn.validate_params(cli.spec(cgi, sm, action), params)
            res = cli.call(cgi, sm, action, clean)
        except (sn.SunapiError, PolicyError) as exc:
            self.audit(dev_id, level, f"{cgi}.{sm}.{action}", params, False, str(exc), actor)
            raise
        ok = res["status"] == 200
        self.audit(dev_id, level, f"{cgi}.{sm}.{action}", clean, ok, res["raw"][:200] if not ok else "ok", actor)
        self._info.pop(dev_id, None)
        return res

    # ---- ONVIF (Dahua y otros)
    def onvif_read(self, dev_id: str, op: str) -> dict:
        if op not in onvif_ops.READS:
            raise oc.OnvifError(f"lectura desconocida: {op}")
        return onvif_ops.read(self._onvif_ctx(dev_id), op)

    def onvif_act(self, dev_id: str, op: str, params: dict, confirm: str, actor: str) -> dict:
        if op not in onvif_ops.ACTIONS:
            raise PolicyError("accion desconocida")
        level = onvif_ops.ACTIONS[op][0]
        self._authorize(dev_id, level, confirm)
        try:
            res = onvif_ops.act(self._onvif_ctx(dev_id), op, params)
        except oc.OnvifError as exc:
            self.audit(dev_id, level, f"onvif.{op}", params, False, str(exc), actor)
            raise
        self.audit(dev_id, level, f"onvif.{op}", params, True, "ok", actor)
        self._info.pop(dev_id, None)
        return res
