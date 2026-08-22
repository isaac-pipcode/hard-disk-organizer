"""
Painel de operacao local do PRESERVA-SCAN — para o PROFISSIONAL INTERNO.
Interface simples no navegador (http://localhost:8080) para:
  - iniciar a varredura de um disco (somente leitura);
  - acompanhar o progresso com BARRA, tempo estimado e contadores;
  - ser AVISADO, de forma clara, sobre erros e possiveis travamentos.

Este painel roda NA MAQUINA DA ESCOLA. Ele opera o scanner.
O dashboard analitico (para o gestor, remoto) e outro app, no Vercel, lendo o Supabase.
"""
import os
import sys
import time
import json
import string
import platform
import threading
import subprocess
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

import db

app = FastAPI(title="PRESERVA-SCAN — Operacao")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MANIFEST_DIR = Path(os.environ.get("MANIFEST_DIR", "./manifestos"))
SENTINELA = "@@PS@@ "          # prefixo das linhas de progresso emitidas pelo scan.py
STALL_SEGUNDOS = 60            # sem novidade por mais que isto (em fase com pulso) = suspeita de travamento
TAIL_LINHAS = 400             # linhas de log mostradas no painel

_lock = threading.Lock()       # garante uma varredura por vez


def _job_inicial():
    return {
        "estado": "ocioso",     # ocioso|contando|identificando|varrendo|concluido|erro
        "disco": None, "raiz": None,
        "total": 0, "total_bytes": 0,
        "lidos": 0, "novos": 0, "erros": 0, "bytes_lidos": 0,
        "arquivo": None,
        "iniciado_em": None, "varrendo_em": None,
        "ultimo_evento_em": None, "fim_em": None,
        "erro_msg": None, "returncode": None,
        "avisos": [], "log_path": None,
    }


JOB = _job_inicial()


def _rodando():
    return JOB["estado"] in ("contando", "identificando", "varrendo")


def discos_disponiveis():
    """Lista os discos/volumes montados, para o operador escolher num menu (sem digitar caminho)."""
    achados = []
    if platform.system() == "Windows":
        for letra in string.ascii_uppercase:
            p = f"{letra}:\\"
            if os.path.exists(p):
                achados.append(p)
    else:
        for base in ("/Volumes", f"/media/{os.environ.get('USER','')}", "/mnt"):
            if os.path.isdir(base):
                for nome in sorted(os.listdir(base)):
                    full = os.path.join(base, nome)
                    if os.path.isdir(full):
                        achados.append(full)
    return achados


def _aplicar_evento(ev):
    """Atualiza o JOB a partir de um evento estruturado vindo do scan.py."""
    tipo = ev.get("event")
    if tipo == "phase":
        JOB["estado"] = ev.get("fase", JOB["estado"])
        if ev.get("fase") == "varrendo" and not JOB["varrendo_em"]:
            JOB["varrendo_em"] = time.time()
    elif tipo in ("contagem", "total"):
        JOB["total"] = ev.get("total", JOB["total"])
        JOB["total_bytes"] = ev.get("total_bytes", JOB["total_bytes"])
    elif tipo == "progress":
        for k in ("lidos", "novos", "erros", "bytes_lidos", "total", "total_bytes"):
            if ev.get(k) is not None:
                JOB[k] = ev[k]
        JOB["arquivo"] = ev.get("arquivo")
    elif tipo == "aviso_arquivo":
        avisos = JOB["avisos"]
        avisos.append({"arquivo": ev.get("arquivo"), "erro": ev.get("erro")})
        del avisos[:-20]                      # guarda so os ultimos 20
    elif tipo == "erro":
        JOB["estado"] = "erro"
        JOB["erro_msg"] = ev.get("msg")


def _rodar(disco, raiz, grupo):
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    log_path = MANIFEST_DIR / f"operacao_{disco}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    JOB["log_path"] = str(log_path)

    cmd = [sys.executable, "-u", "scan.py", "--disco", disco, "--raiz", raiz,
           "--progress-json"] + (["--grupo", grupo] if grupo else [])
    try:
        with open(log_path, "w", encoding="utf-8") as flog:
            flog.write(f"=== varredura {disco} ({raiz}) — {time.ctime()} ===\n")
            flog.flush()
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=str(Path(__file__).parent),
            )
            for linha in proc.stdout:
                JOB["ultimo_evento_em"] = time.time()
                if linha.startswith(SENTINELA):
                    try:
                        _aplicar_evento(json.loads(linha[len(SENTINELA):]))
                    except Exception:
                        pass
                else:
                    flog.write(linha)
                    flog.flush()
            rc = proc.wait()
        JOB["returncode"] = rc
        JOB["fim_em"] = time.time()
        if JOB["estado"] != "erro":
            if rc == 0:
                JOB["estado"] = "concluido"
            else:
                JOB["estado"] = "erro"
                JOB["erro_msg"] = JOB["erro_msg"] or f"O scanner terminou com codigo {rc}. Veja o log."
    except Exception as e:
        JOB["estado"] = "erro"
        JOB["erro_msg"] = f"Falha ao iniciar o scanner: {type(e).__name__}: {e}"
        JOB["fim_em"] = time.time()
    finally:
        _lock.release()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    resumo = []
    if db.conectado():
        try:
            import psycopg2
            with psycopg2.connect(db.DATABASE_URL) as c, c.cursor() as cur:
                cur.execute("SELECT label, grupo, status, arquivos, bytes_totais FROM resumo_discos ORDER BY label")
                resumo = cur.fetchall()
        except Exception:
            resumo = []
    return templates.TemplateResponse(request, "index.html", {
        "resumo": resumo, "online": db.conectado(),
        "discos": discos_disponiveis(),
        "rodando": _rodando(), "job_estado": JOB["estado"], "job_disco": JOB["disco"],
    })


@app.post("/varrer")
def varrer(disco: str = Form(...), raiz: str = Form(...), subpasta: str = Form(""), grupo: str = Form("")):
    caminho = os.path.join(raiz, subpasta) if subpasta.strip() else raiz
    if not disco.strip():
        return HTMLResponse("<p>Informe a etiqueta do disco. <a href='/'>voltar</a></p>", status_code=400)
    if not os.path.isdir(caminho):
        return HTMLResponse(
            f"<p>Caminho nao encontrado ou inacessivel: <b>{caminho}</b>. "
            f"Confira se o disco esta ligado/montado. <a href='/'>voltar</a></p>", status_code=400)
    # Uma varredura por vez: se ja ha uma rodando, nao inicia outra.
    if not _lock.acquire(blocking=False):
        return HTMLResponse(
            "<p>Ja existe uma varredura em andamento. "
            "<a href='/acompanhar'>acompanhar</a></p>", status_code=409)

    JOB.update(_job_inicial())
    JOB.update({"estado": "contando", "disco": disco.strip(), "raiz": caminho,
                "iniciado_em": time.time(), "ultimo_evento_em": time.time()})
    threading.Thread(target=_rodar, args=(disco.strip(), caminho, grupo.strip() or None),
                     daemon=True).start()
    return RedirectResponse(url="/acompanhar", status_code=303)


@app.get("/acompanhar", response_class=HTMLResponse)
def acompanhar(request: Request):
    return templates.TemplateResponse(request, "acompanhar.html", {})


@app.get("/status")
def status():
    agora = time.time()
    ult = JOB["ultimo_evento_em"]
    parado_ha = (agora - ult) if ult else 0
    # So consideramos "travado" nas fases que emitem batimento (contando/varrendo).
    # A fase 'identificando' (Siegfried) e naturalmente silenciosa e pode levar minutos.
    travado = _rodando() and JOB["estado"] in ("contando", "varrendo") and parado_ha > STALL_SEGUNDOS
    decorrido = (agora - JOB["iniciado_em"]) if JOB["iniciado_em"] else 0
    varrendo_ha = (agora - JOB["varrendo_em"]) if JOB["varrendo_em"] else 0
    return JSONResponse({
        "estado": JOB["estado"], "disco": JOB["disco"], "raiz": JOB["raiz"],
        "total": JOB["total"], "total_bytes": JOB["total_bytes"],
        "lidos": JOB["lidos"], "novos": JOB["novos"], "erros": JOB["erros"],
        "bytes_lidos": JOB["bytes_lidos"], "arquivo": JOB["arquivo"],
        "decorrido": round(decorrido), "varrendo_ha": round(varrendo_ha),
        "parado_ha": round(parado_ha), "travado": travado,
        "erro_msg": JOB["erro_msg"], "avisos": JOB["avisos"],
        "online": db.conectado(),
    })


@app.get("/log", response_class=PlainTextResponse)
def log():
    p = Path(JOB["log_path"]) if JOB["log_path"] else None
    if not p or not p.exists():
        return "(sem log ainda)"
    linhas = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(linhas[-TAIL_LINHAS:])
