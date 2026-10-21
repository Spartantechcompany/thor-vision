"""Dispositivos IoT con tipo (foco, interruptor, contacto, gateway) y estado en linea de los que no tienen sensores."""
import json
import re
import yaml

from app.iot import listener as lst


def test_todos_los_dispositivos_de_iot_yml_tienen_tipo_y_los_interruptores_botones():
    d = yaml.safe_load(open("config/iot.yml", encoding="utf-8"))["devices"]
    assert all(v.get("type") for v in d.values())
    assert all(v.get("gangs") in (1, 2, 3) for v in d.values() if v["type"] == "interruptor")
    assert {v["kind"] for v in d.values() if v["type"] == "gateway"} == {"gateway"} and sum(1 for v in d.values() if v["type"] == "gateway") == 2


def test_la_carpeta_de_iot_yml_no_mezcla_tabuladores():
    assert "\t" not in open("config/iot.yml", encoding="utf-8").read()


def test_el_icono_de_cada_tipo_existe_en_la_pagina():
    html = open("app/dashboard/templates/iot.html", encoding="utf-8").read()
    for k in ("contacto", "switch1", "switch2", "switch3", "gateway", "panel", "plug", "alarm", "curtain"):
        assert re.search(rf"\b{k}: '<svg", html), k


def test_el_registro_onvif_ignora_camaras_leidas_por_go2rtc():
    import types
    from app.onvif import registry
    cams = [types.SimpleNamespace(id="cam-vto", name="Videoportero", rtsp_url="rtsp://127.0.0.1:8554/cam-vto", zone="interior"),
            types.SimpleNamespace(id="cam-189", name="Exterior 1", rtsp_url="rtsp://u:p@192.168.20.189:554/profile2/media.smp", zone="exterior")]
    assert [e["id"] for e in registry.camera_endpoints(types.SimpleNamespace(cameras=cams))] == ["cam-189"]
