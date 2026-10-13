"""Armado del sistema: normal o absoluto (toda persona es alerta, siempre y en todas las camaras)."""
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.vision import arming

router = APIRouter()


class Mode(BaseModel):
    mode: str


@router.get("/api/arming")
def get_state():
    return arming.get()


@router.post("/api/arming")
def set_state(body: Mode, request: Request):
    if body.mode not in arming.MODES:
        raise HTTPException(400, "modo invalido: normal o absoluto")
    before = arming.get()["mode"]
    st = arming.set_mode(body.mode, request.client.host if request.client else "")
    db = getattr(request.app.state, "db", None)
    if db is not None and before != body.mode:           # el cambio de estado queda en la linea de tiempo
        txt = "ARMADO ABSOLUTO activado: toda persona es alerta" if body.mode == "absoluto" else "Armado absoluto desactivado: reglas normales"
        db.insert_event("nemotron", None, {"schema": 2, "source": "arming", "people": 0, "persons": [], "vehicles": 0, "activity": txt, "scene": "", "relevant": True,
                                           "alerts": [txt], "alert_types": ["estado_armado"], "severity": "high" if body.mode == "absoluto" else "low",
                                           "confidence": "high", "arming": {"mode": body.mode, "from": st.get("by")}}, people=0, has_alert=True)
    return st
