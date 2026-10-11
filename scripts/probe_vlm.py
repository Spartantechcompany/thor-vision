"""Sondea que acepta el modelo de vision del gateway: texto, JSON, 1 y 2 imagenes, imagen grande, clip de video (video_url mp4) y streaming.
Corre dentro del contenedor: docker exec thor-vision python3 /app/scripts/probe_vlm.py   (PROBE_MODEL=otro para probar otro nombre)."""
import os,json,urllib.request,base64,time,urllib.error,sys,subprocess,tempfile
sys.path.insert(0,"/app")
import cv2,numpy as np
ep=os.environ["NEMOTRON_ENDPOINT"].rstrip("/"); key=os.environ["NEMOTRON_API_KEY"]
H={"Authorization":"Bearer "+key,"Content-Type":"application/json"}
def snap(c,w=480): return urllib.request.urlopen(f"http://localhost:8080/api/snapshot/{c}?w={w}",timeout=8).read()
def call(label,content,mt=120,extra=None,timeout=75):
    body={"model":os.environ.get("PROBE_MODEL") or os.environ.get("NEMOTRON_MODEL","thor-vision"),"max_tokens":mt,"messages":[{"role":"user","content":content}],"chat_template_kwargs":{"enable_thinking":False}}
    if extra: body.update(extra)
    t=time.time()
    try:
        r=json.load(urllib.request.urlopen(urllib.request.Request(ep+"/v1/chat/completions",data=json.dumps(body).encode(),headers=H),timeout=timeout))
        m=r["choices"][0]["message"]; u=r.get("usage") or {}
        print(f"{label:34s} OK {time.time()-t:4.1f}s modelo={r.get('model')} tokens={u.get('prompt_tokens')}/{u.get('completion_tokens')} | {(m.get('content') or '')[:90]!r}")
    except urllib.error.HTTPError as e: print(f"{label:34s} HTTP {e.code} {e.read()[:120].decode('utf-8','ignore')}")
    except Exception as e: print(f"{label:34s} {type(e).__name__} {str(e)[:80]}")
b64=lambda b: base64.b64encode(b).decode()
img=lambda c: {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+b64(snap(c))}}
T=lambda s: {"type":"text","text":s}
call("texto simple",[T("Responde solo: ok")])
call("texto largo (instrucciones+JSON)",[T("Devuelve SOLO este JSON válido: {\"personas\":0,\"actividad\":\"texto\"} con valores de ejemplo")],80)
call("1 imagen",[T("Describe la imagen en una frase."),img("cam-215")])
call("2 imagenes",[T("¿Qué diferencia hay entre estas dos imágenes? Una frase."),img("cam-215"),img("cam-120")],150)
call("imagen 1280 px",[T("Describe la imagen en una frase."),{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+b64(snap("cam-120",1280))}}])
# video como lo manda el analizador: clip mp4 en video_url
frames=[cv2.imdecode(np.frombuffer(snap("cam-215",640),np.uint8),1) for _ in range(6)]
tmp=tempfile.mktemp(suffix=".mp4"); vw=cv2.VideoWriter(tmp,cv2.VideoWriter_fourcc(*"mp4v"),3,(frames[0].shape[1],frames[0].shape[0]))
for f in frames: vw.write(f)
vw.release(); mp4=open(tmp,"rb").read(); print("clip mp4:",len(mp4)//1024,"KB")
call("video (video_url mp4)",[T("Describe lo que pasa en el video en una frase."),{"type":"video_url","video_url":{"url":"data:video/mp4;base64,"+b64(mp4)}}],150)
call("respuesta en streaming",[T("Cuenta del 1 al 5")],60,{"stream":True},40)
