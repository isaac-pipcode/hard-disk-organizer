"""
Painel de operacao local do PRESERVA-SCAN — para o PROFISSIONAL INTERNO.
Interface simples no navegador (http://localhost:8080) para:
  - iniciar a varredura de um disco;
  - acompanhar o progresso (log ao vivo) e o resumo por disco.

Este painel roda NA MAQUINA DA ESCOLA. Ele opera o scanner.
O dashboard analitico (para o gestor, remoto) e outro app, no Vercel, lendo o Supabase.
"""
import os
import string
import platform
import threading
import subprocess
from pathlib import Path


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
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

import db

app = FastAPI(title="PRESERVA-SCAN — Operacao")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

LOG = Path(os.environ.get("MANIFEST_DIR", "./manifestos")) / "operacao.log"
_lock = threading.Lock()


def _rodar(disco, raiz, grupo):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n=== varredura {disco} ({raiz}) ===\n")
    with _lock, open(LOG, "a", encoding="utf-8") as f:
        proc = subprocess.Popen(
            ["python", "scan.py", "--disco", disco, "--raiz", raiz] +
            (["--grupo", grupo] if grupo else []),
            stdout=f, stderr=subprocess.STDOUT, cwd=str(Path(__file__).parent),
        )
        proc.wait()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    resumo = []
    if db.conectado():
        import psycopg2
        with psycopg2.connect(db.DATABASE_URL) as c, c.cursor() as cur:
            cur.execute("SELECT label, grupo, status, arquivos, bytes_totais FROM resumo_discos ORDER BY label")
            resumo = cur.fetchall()
    return templates.TemplateResponse("index.html", {
        "request": request, "resumo": resumo, "online": db.conectado(),
        "discos": discos_disponiveis(),
    })


@app.post("/varrer")
def varrer(disco: str = Form(...), raiz: str = Form(...), subpasta: str = Form(""), grupo: str = Form("")):
    caminho = os.path.join(raiz, subpasta) if subpasta.strip() else raiz
    threading.Thread(target=_rodar, args=(disco, caminho, grupo or None), daemon=True).start()
    return HTMLResponse(
        f"<p>Varredura de <b>{disco}</b> ({caminho}) iniciada. "
        f"<a href='/'>voltar</a> | <a href='/log'>ver log ao vivo</a></p>")


@app.get("/log", response_class=PlainTextResponse)
def log():
    return LOG.read_text(encoding="utf-8") if LOG.exists() else "(sem log ainda)"
