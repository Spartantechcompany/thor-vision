"""Exporta a CSV los registros de los ultimos N dias (una tabla = un archivo) para cargarlos en un almacen de datos o un dashboard.

Uso dentro del contenedor:  docker exec thor-vision python3 /app/scripts/export_csv.py --days 7 --out /app/data/exports
Genera <out>/thor-<fecha>/ con un CSV por tabla, dim_camaras.csv, manifest.json (filas por archivo) y LEEME.md (diccionario de datos).

Lo que NUNCA sale: imagenes (caras, cuerpos, escenas, miniaturas), embeddings y huellas biometricas (embedding, body_emb, vec, feat), ni claves.
Las columnas de tiempo van dos veces: epoch (UTC) y *_iso con hora local de Ciudad de Mexico (-06:00, sin horario de verano desde 2022).
Los eventos del VLM se desglosan en columnas (actividad, severidad, tipos de alerta...) y se conserva el JSON original en la columna data.
"""
import argparse
import csv
import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone

DB = os.environ.get("EVENTS_DB", "/app/data/events.db")
TZ = timezone(timedelta(hours=-6))
BLOB_COLS = {"embedding", "body_emb", "vec", "feat", "face", "body", "thumb", "scene", "plate_img", "img", "image", "token", "key"}
# tabla -> columna de tiempo para el filtro (None = tabla pequena que se exporta completa)
TABLES = {
    "events": "ts", "person_visits": "start_ts", "subjects": None, "journeys": "start_ts", "trips": "start_ts", "parked_vehicles": "first_ts",
    "iot_events": "ts", "onvif_events": "ts", "identity_checks": "ts", "tuya_entries": "ts", "street_counts": "hour", "pet_sightings": "ts",
    "pet_crops": "ts", "snapshots": "ts", "clips": "ts", "gait_samples": "ts", "wifi_sessions": "start_ts", "wifi_devices": None, "reports": "generated_at",
    "device_actions": "ts", "dashboard_visits": "ts", "chat_messages": "ts", "rule_analyses": "ts",
}
TIME_COLS = {"ts", "start_ts", "end_ts", "first_ts", "last_ts", "left_ts", "created_ts", "hour", "generated_at", "period_start", "period_end", "first_seen", "last_seen", "labeled_ts", "reviewed_at"}
DOC = {
    "events": "Una fila por analisis del VLM, alerta, viaje, timbre o aviso. type=nemotron para casi todos (nombre historico). Columnas desglosadas del JSON; la columna data trae el JSON original.",
    "person_visits": "Una fila por visita de una persona frente a una camara (el tracker). subject_id enlaza con subjects; fp=1 falso positivo; static=1 objeto quieto; vlm_desc descripcion del modelo.",
    "subjects": "Identidades (Persona #N o con nombre). category: revisar|empleado|invitado. Sin embeddings.",
    "journeys": "Recorridos de una persona entre camaras (visitas enlazadas).", "trips": "Viajes disparados por el timbre o el porton: direccion, visitas y resumen del modelo.",
    "parked_vehicles": "Vehiculos estacionados: llegada, salida, placa (plate) o lectura por confirmar (plate_guess).", "iot_events": "Mensajes de los dispositivos Tuya (source=event o sync; sync se repite cada 10 min).",
    "onvif_events": "Eventos nativos de camaras y videoportero.", "identity_checks": "Decisiones de Clef al unir identidades (auto, sugerencia, consolidado).",
    "tuya_entries": "Avisos de entrada de Tuya y si se enlazaron con una visita.", "street_counts": "Transeuntes por hora en las camaras de calle (modo ligero).",
    "pet_sightings": "Mascotas vistas (pet, camara, afuera o adentro).", "pet_crops": "Recortes de perros y gatos con su etiqueta (sin la imagen).", "snapshots": "Capturas guardadas (ruta y disparador; sin la imagen).",
    "clips": "Clips de video guardados (ruta, duracion).", "gait_samples": "Muestras de marcha por visita (sin el vector).", "wifi_sessions": "Sesiones de dispositivos WiFi conocidos.",
    "wifi_devices": "Dispositivos WiFi vistos (MAC, nombre, dueno).", "reports": "Reportes generados por el modelo (texto).", "device_actions": "Ordenes enviadas a dispositivos.",
    "dashboard_visits": "Accesos al panel (IP y navegador): dato personal.", "chat_messages": "Mensajes del chat.", "rule_analyses": "Analisis de reglas.", "dim_camaras": "Catalogo de camaras (id, nombre, zona).",
}


def iso(v):
    try:
        return datetime.fromtimestamp(float(v), TZ).strftime("%Y-%m-%d %H:%M:%S") if v not in (None, "", 0) else ""
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def flat_event(d: dict) -> dict:
    j = lambda x: " | ".join(map(str, x)) if isinstance(x, list) else ("" if x is None else x)
    trip, parked, door, visit = d.get("trip") or {}, d.get("parked") or {}, d.get("doorbell") or {}, d.get("visit") or {}
    return {"activity": d.get("activity"), "scene": d.get("scene"), "severity": d.get("severity"), "confidence": d.get("confidence"), "relevant": d.get("relevant"),
            "alerts": j(d.get("alerts")), "alert_types": j(d.get("alert_types")), "n_persons": len(d.get("persons") or []), "vehicles": d.get("vehicles"),
            "yolo_people": d.get("yolo_people"), "people_mismatch": d.get("people_mismatch"), "source": d.get("source"), "trigger": d.get("_trigger"), "media_kind": d.get("_media_kind"),
            "vlm_ms": d.get("_ms"), "vlm_model": d.get("_model"), "schema": d.get("schema"), "trip_direction": trip.get("direction"), "trip_id": trip.get("trip_id"),
            "parked_state": parked.get("state"), "parked_plate": parked.get("plate"), "doorbell_kind": door.get("kind"), "visit_id": visit.get("visit_id") or door.get("visit_id"),
            "summary": d.get("summary")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--out", default="/app/data/exports")
    a = ap.parse_args()
    since = time.time() - a.days * 86400
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime())
    out = os.path.join(a.out, f"thor-{stamp}")
    os.makedirs(out, exist_ok=True)
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    manifest = {"generated": datetime.now(TZ).isoformat(timespec="seconds"), "since": iso(since), "days": a.days, "files": {}}
    existing = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    for t, tcol in TABLES.items():
        if t not in existing:
            continue
        cols = [r[1] for r in c.execute(f"pragma table_info({t})")]
        keep = [x for x in cols if x not in BLOB_COLS]
        sql = f"select {', '.join(keep)} from {t}" + (f" where {tcol} >= ?" if tcol else "") + (f" order by {tcol}" if tcol else "")
        cur = c.execute(sql, (since,) if tcol else ())
        extra = [f"{x}_iso" for x in keep if x in TIME_COLS]
        ev_cols = list(flat_event({}).keys()) if t == "events" else []
        head = keep + extra + ev_cols + (["data_json_raw"] if False else [])
        n = 0
        with open(os.path.join(out, f"{t}.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(head)
            for row in cur:
                r = list(row)
                rec = dict(zip(keep, r))
                r += [iso(rec[x[:-4]]) for x in extra]
                if t == "events":
                    try:
                        d = json.loads(rec.get("data") or "{}")
                    except ValueError:
                        d = {}
                    fl = flat_event(d if isinstance(d, dict) else {})
                    r += [fl[k] for k in ev_cols]
                w.writerow(r)
                n += 1
        manifest["files"][f"{t}.csv"] = n
    # catalogo de camaras: solo id, nombre y zona (el archivo tiene credenciales RTSP: no se copia ni se imprime nada mas)
    try:
        import yaml
        cams = (yaml.safe_load(open(os.environ.get("CAMERAS_YML", "/app/config/cameras.yml"), encoding="utf-8")) or {}).get("cameras") or []
        with open(os.path.join(out, "dim_camaras.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["cam_id", "nombre", "zona"])
            for x in cams:
                w.writerow([x.get("id"), x.get("name"), x.get("zone") or "interior"])
        manifest["files"]["dim_camaras.csv"] = len(cams)
    except (OSError, ValueError, ImportError, AttributeError) as exc:
        manifest["dim_camaras_error"] = str(exc)[:80]
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "LEEME.md"), "w", encoding="utf-8") as f:
        f.write(f"# THOR Vision: exportacion de los ultimos {a.days:g} dias\n\nGenerada {manifest['generated']} (hora de Ciudad de Mexico). Codificacion UTF-8, separador coma, primera fila = encabezados.\n"
                "Tiempos: epoch (UTC) y columnas *_iso en hora local (-06:00).\nNo incluye imagenes, embeddings ni huellas biometricas.\n\n## Archivos\n\n| archivo | filas | contenido |\n|---|---:|---|\n")
        for k, v in manifest["files"].items():
            f.write(f"| {k} | {v} | {DOC.get(k[:-4], '')} |\n")
        f.write("\n## Relaciones\n\n- person_visits.subject_id -> subjects.id; person_visits.journey_id -> journeys.id; trips.event_id -> events.id; events.cam_id / *.cam_id -> dim_camaras.cam_id\n"
                "- identity_checks.visit_id -> person_visits.id; tuya_entries.visit_id -> person_visits.id; parked_vehicles.twin_id / merged_into -> parked_vehicles.id\n")
    print(json.dumps({"carpeta": out, **manifest["files"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
