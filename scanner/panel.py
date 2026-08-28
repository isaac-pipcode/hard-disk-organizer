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
import html
import string
import platform
import threading
import contextlib
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

import db
import scan       # a varredura roda no MESMO processo (essencial para o .exe empacotado)
import relatorio  # gera o relatório local (dashboard + planilhas) a partir dos manifestos


def _recurso(rel):
    """Resolve um recurso (ex.: pasta de templates) tanto no modo normal quanto
    dentro do executavel empacotado pelo PyInstaller (que extrai em _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# Onde gravar manifestos/logs. No .exe, ao lado do proprio executavel (a pessoa
# encontra a pasta 'manifestos' junto do programa); no modo normal, no diretorio atual.
if getattr(sys, "frozen", False):
    _BASE_DADOS = Path(sys.executable).parent
else:
    _BASE_DADOS = Path.cwd()
MANIFEST_DIR = Path(os.environ.get("MANIFEST_DIR") or (_BASE_DADOS / "manifestos"))

app = FastAPI(title="PRESERVA-SCAN — Operacao")
templates = Jinja2Templates(directory=_recurso("templates"))

STALL_SEGUNDOS = 90           # sem batimento por mais que isto (em fase com pulso) = suspeita de travamento
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
        "avisos": [], "log_path": None, "retomados": 0,
    }


JOB = _job_inicial()


def _rodando():
    return JOB["estado"] in ("contando", "identificando", "varrendo", "completando")


def ferramentas_status():
    """Quais ferramentas externas o scanner enxerga (embutidas no .exe, numa pasta
    'ferramentas' ao lado, ou no PATH)."""
    return {
        "siegfried": scan.ferramenta("sf") is not None,
        "mediainfo": scan.ferramenta("mediainfo") is not None,
        "exiftool": scan.ferramenta("exiftool") is not None,
    }


def _humano_tb(nbytes):
    if not nbytes:
        return ""
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {u}".replace(".0 ", " ")
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def _rotulo_volume_windows(letra):
    """Nome do volume (ex.: 'TRANSPORTE A') de uma unidade Windows, via API do SO."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        fsbuf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(f"{letra}:\\"), buf, ctypes.sizeof(buf),
            None, None, None, fsbuf, ctypes.sizeof(fsbuf))
        return buf.value if ok else ""
    except Exception:
        return ""


def discos_disponiveis():
    """Lista os discos montados com etiqueta e tamanho, para o operador escolher
    num menu sem digitar caminho. Cada item:
      {valor, letra, rotulo, tamanho, texto}  (texto = o que aparece no menu)."""
    achados = []
    if platform.system() == "Windows":
        import shutil
        sysdrive = os.environ.get("SystemDrive", "C:").rstrip("\\").upper()
        for letra in string.ascii_uppercase:
            p = f"{letra}:\\"
            if not os.path.exists(p):
                continue
            rotulo = _rotulo_volume_windows(letra)
            try:
                total = shutil.disk_usage(p).total
            except Exception:
                total = 0
            eh_sistema = (f"{letra}:" == sysdrive)
            texto = f"{letra}:  {rotulo}".rstrip()
            if total:
                texto += f"  ({_humano_tb(total)})"
            if eh_sistema:
                texto += "  — disco do sistema (evite varrer)"
            achados.append({"valor": p, "letra": f"{letra}:", "rotulo": rotulo,
                            "tamanho": _humano_tb(total), "texto": texto,
                            "sistema": eh_sistema})
    else:
        import shutil
        for base in ("/Volumes", f"/media/{os.environ.get('USER','')}", "/mnt"):
            if os.path.isdir(base):
                for nome in sorted(os.listdir(base)):
                    full = os.path.join(base, nome)
                    if os.path.isdir(full):
                        try:
                            total = shutil.disk_usage(full).total
                        except Exception:
                            total = 0
                        texto = nome + (f"  ({_humano_tb(total)})" if total else "")
                        achados.append({"valor": full, "letra": "", "rotulo": nome,
                                        "tamanho": _humano_tb(total), "texto": texto})
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
    elif tipo == "done":
        for k in ("lidos", "novos", "erros", "bytes_lidos", "total", "total_bytes"):
            if ev.get(k) is not None:
                JOB[k] = ev[k]
    elif tipo == "retomar":
        JOB["retomados"] = ev.get("ja", 0)
    elif tipo == "erro":
        JOB["estado"] = "erro"
        JOB["erro_msg"] = ev.get("msg")


def _sink(ev):
    """Recebe cada evento do scanner (no mesmo processo) e atualiza o estado.
    Tocar o relogio aqui e o que alimenta a deteccao de travamento."""
    JOB["ultimo_evento_em"] = time.time()
    _aplicar_evento(ev)


def _rodar(disco, raiz, grupo, modo="varredura"):
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    log_path = MANIFEST_DIR / f"operacao_{disco}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    JOB["log_path"] = str(log_path)

    # A varredura roda NESTE processo (nao ha 'python scan.py' externo — o que
    # tornaria o .exe fragil). Ligamos o scanner ao painel por callback e
    # redirecionamos as mensagens humanas do scanner para o arquivo de log.
    scan.MANIFESTOS = MANIFEST_DIR
    scan.EMIT = False
    scan.CALLBACK = _sink
    try:
        with open(log_path, "w", encoding="utf-8") as flog:
            flog.write(f"=== {modo} {disco} ({raiz}) — {time.ctime()} ===\n")
            flog.flush()
            with contextlib.redirect_stdout(flog), contextlib.redirect_stderr(flog):
                if modo == "backfill":
                    scan.backfill(disco, Path(raiz))
                else:
                    scan.varrer(disco, Path(raiz), grupo, force=False)
        JOB["returncode"] = 0
        if JOB["estado"] != "erro":
            JOB["estado"] = "concluido"
    except (Exception, SystemExit) as e:
        # varrer/backfill já emitiram o evento 'erro' (que marcou o estado); aqui
        # garantimos a mensagem mesmo que o erro tenha vindo de fora do bloco tratado
        # (backfill sinaliza falta de manifesto/ferramenta com SystemExit).
        JOB["estado"] = "erro"
        JOB["returncode"] = 1
        JOB["erro_msg"] = JOB["erro_msg"] or f"{e}"
    finally:
        JOB["fim_em"] = time.time()
        scan.CALLBACK = None
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
    tem_manifestos = bool(list(MANIFEST_DIR.glob("manifesto_*.jsonl")))
    return templates.TemplateResponse(request, "index.html", {
        "resumo": resumo, "online": db.conectado(),
        "discos": discos_disponiveis(), "tem_manifestos": tem_manifestos,
        "rodando": _rodando(), "job_estado": JOB["estado"], "job_disco": JOB["disco"],
        "ferramentas": ferramentas_status(),
    })


def _salvar_env(url):
    """Grava (ou remove) a linha DATABASE_URL no .env ao lado do programa, preservando
    as outras linhas. Assim a conexão configurada pelo painel PERSISTE ao reabrir."""
    env_path = _BASE_DADOS / ".env"
    linhas = []
    if env_path.exists():
        try:
            linhas = [l for l in env_path.read_text(encoding="utf-8").splitlines()
                      if not l.strip().startswith("DATABASE_URL=")]
        except Exception:
            linhas = []
    if url:
        linhas.append(f"DATABASE_URL={url}")
    corpo = "\n".join(l for l in linhas if l.strip())
    env_path.write_text((corpo + "\n") if corpo else "", encoding="utf-8")
    return env_path


def _pagina_config(valor="", erro=None, ok=None):
    """Tela de configuração do banco (Supabase) — o operador cola a string e conecta,
    sem criar arquivo .env à mão."""
    valor = html.escape(valor or "")
    alerta = ""
    if erro:
        alerta = f'<div class="box err">⚠️ {html.escape(erro)}</div>'
    elif ok:
        alerta = f'<div class="box ok">✅ {html.escape(ok)}</div>'
    return f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PRESERVA-SCAN — conectar ao banco</title>
<style>
  :root{{ --ink:#1a1a1a; --bg:#fff; --cinza:#666; --azul:#1857b8; --linha:#e3e3e3;
          --okbg:#e7f6ec; --okln:#1c7a3f; --errbg:#fdecec; --errln:#b3261e; }}
  @media (prefers-color-scheme:dark){{ :root{{ --ink:#ececec; --bg:#1b1b1d; --cinza:#a6a6a6;
          --azul:#7aa7ff; --linha:#3a3a3d; --okbg:#12331f; --okln:#6ee7a0; --errbg:#3a1c1a; --errln:#ff8a80; }} }}
  *{{ box-sizing:border-box; }}
  body{{ font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg);
         color:var(--ink); max-width:720px; margin:0 auto; padding:1.5rem 1.2rem; line-height:1.5; }}
  h1{{ font-size:1.3rem; margin:.2rem 0 .3rem; }}
  a{{ color:var(--azul); }}
  label{{ display:block; font-weight:600; margin:1.1rem 0 .35rem; }}
  textarea{{ width:100%; min-height:90px; font-family:ui-monospace,Consolas,monospace; font-size:.9rem;
             padding:.6rem; border:1px solid var(--linha); border-radius:8px; background:var(--bg); color:var(--ink); }}
  .btn{{ display:inline-block; margin-top:1rem; background:var(--azul); color:#fff; border:0; border-radius:8px;
         padding:.7rem 1.3rem; font-size:1rem; font-weight:600; cursor:pointer; }}
  .btn.sec{{ background:transparent; color:var(--azul); border:1px solid var(--azul); margin-left:.5rem; }}
  .ajuda{{ color:var(--cinza); font-size:.9rem; }}
  .passos{{ background:var(--okbg); border-radius:8px; padding:.8rem 1rem; font-size:.9rem; margin:1rem 0; }}
  .box{{ border-radius:8px; padding:.7rem .9rem; margin:1rem 0; font-size:.92rem; }}
  .box.err{{ background:var(--errbg); color:var(--errln); }}
  .box.ok{{ background:var(--okbg); color:var(--okln); }}
  code{{ background:rgba(128,128,128,.15); padding:.05rem .3rem; border-radius:4px; }}
</style></head><body>
  <p><a href="/">← voltar ao painel</a></p>
  <h1>Conectar ao banco (Supabase)</h1>
  <p class="ajuda">Cole aqui a string de conexão do seu projeto. O programa testa e
     guarda sozinho — você <b>não</b> precisa criar arquivo nenhum.</p>
  {alerta}
  <div class="passos">
    <b>Onde achar a string:</b> no Supabase, botão <b>Connect</b> (no topo) →
    aba <b>Session pooler</b> → copie a string inteira. Troque <code>[YOUR-PASSWORD]</code>
    pela senha do banco (de preferência só letras e números).
  </div>
  <form method="post" action="/config">
    <label for="cs">String de conexão (DATABASE_URL)</label>
    <textarea id="cs" name="connection_string" placeholder="postgresql://postgres.SEU-REF:SENHA@aws-0-...pooler.supabase.com:5432/postgres">{valor}</textarea>
    <div>
      <button class="btn" type="submit">Testar e conectar</button>
      <button class="btn sec" type="submit" name="connection_string" value="">Desconectar (offline)</button>
    </div>
  </form>
  <p class="ajuda" style="margin-top:1.4rem">🔒 A string fica guardada só nesta máquina
     (arquivo <code>.env</code> ao lado do programa). Contém a senha do banco — trate
     este computador como de confiança.</p>
</body></html>"""


@app.get("/config", response_class=HTMLResponse)
def config_form(request: Request):
    return HTMLResponse(_pagina_config(db.DATABASE_URL or ""))


@app.post("/config")
def config_salvar(connection_string: str = Form("")):
    url = connection_string.strip()
    if not url:
        # Desconectar: volta ao modo offline e remove do .env.
        db.configurar("")
        _salvar_env("")
        return RedirectResponse(url="/", status_code=303)
    ok, msg = db.testar(url)
    if not ok:
        return HTMLResponse(_pagina_config(url, erro=msg), status_code=400)
    db.configurar(url)
    _salvar_env(url)
    return RedirectResponse(url="/", status_code=303)


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


@app.post("/backfill")
def backfill(disco: str = Form(...), raiz: str = Form(...), subpasta: str = Form(""), grupo: str = Form("")):
    """Completa só os metadados de mídia que faltaram, sem re-hashear."""
    caminho = os.path.join(raiz, subpasta) if subpasta.strip() else raiz
    if not disco.strip():
        return HTMLResponse("<p>Informe a etiqueta do disco. <a href='/'>voltar</a></p>", status_code=400)
    if not os.path.isdir(caminho):
        return HTMLResponse(
            f"<p>Caminho não encontrado: <b>{html.escape(caminho)}</b>. Ligue o disco. "
            f"<a href='/'>voltar</a></p>", status_code=400)
    if not (MANIFEST_DIR / f"manifesto_{disco.strip()}.jsonl").exists():
        return HTMLResponse(
            f"<p>Ainda não há manifesto para <b>{html.escape(disco.strip())}</b> — "
            f"faça a varredura antes de completar metadados. <a href='/'>voltar</a></p>", status_code=400)
    if not _lock.acquire(blocking=False):
        return HTMLResponse("<p>Já existe uma operação em andamento. "
                            "<a href='/acompanhar'>acompanhar</a></p>", status_code=409)
    JOB.update(_job_inicial())
    JOB.update({"estado": "completando", "disco": disco.strip(), "raiz": caminho,
                "iniciado_em": time.time(), "ultimo_evento_em": time.time()})
    threading.Thread(target=_rodar,
                     args=(disco.strip(), caminho, grupo.strip() or None, "backfill"),
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
    travado = _rodando() and JOB["estado"] in ("contando", "varrendo", "completando") and parado_ha > STALL_SEGUNDOS
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
        "retomados": JOB["retomados"], "online": db.conectado(),
    })


RELATORIO_DIR = MANIFEST_DIR / "relatorio"


@app.post("/relatorio")
def gerar_relatorio():
    # Rota síncrona (def): o Starlette a executa num threadpool, sem travar o painel.
    try:
        relatorio.gerar(MANIFEST_DIR, RELATORIO_DIR)
    except FileNotFoundError as e:
        return HTMLResponse(f"<p>{html.escape(str(e))} <a href='/'>voltar</a></p>", status_code=400)
    except Exception as e:
        return HTMLResponse(
            f"<p>Falha ao gerar o relatório: {html.escape(type(e).__name__)}: "
            f"{html.escape(str(e))} <a href='/'>voltar</a></p>", status_code=500)
    return RedirectResponse(url="/relatorio/ver", status_code=303)


@app.get("/relatorio/ver", response_class=HTMLResponse)
def ver_relatorio():
    dash = RELATORIO_DIR / "dashboard.html"
    if not dash.exists():
        return HTMLResponse("<p>Relatório ainda não gerado. <a href='/'>voltar</a></p>",
                            status_code=404)
    return HTMLResponse(dash.read_text(encoding="utf-8", errors="replace"))


@app.get("/log", response_class=PlainTextResponse)
def log():
    p = Path(JOB["log_path"]) if JOB["log_path"] else None
    if not p or not p.exists():
        return "(sem log ainda)"
    linhas = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(linhas[-TAIL_LINHAS:])
