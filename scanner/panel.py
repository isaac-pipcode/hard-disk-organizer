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
import shutil
import threading
import contextlib
from pathlib import Path
from urllib.parse import quote

from typing import List

from fastapi import FastAPI, Form, File, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

import db
import hardware  # marca/modelo/série dos discos (ícone + reconhecimento)
import acervo    # tela inicial: o estado de cada HD (cartões), sem banco
import pacote    # pacote de envio (.zip) para o gestor do acervo
import nomes     # nomes dos arquivos de cada HD (manifesto, ficha, registros)
import conteudo  # navegar no conteúdo de um HD pelo inventário (inclusive durante a varredura)
from versao import VERSAO
import scan       # a varredura roda no MESMO processo (essencial para o .exe empacotado)
import relatorio  # gera o relatório local (dashboard + planilhas) a partir dos manifestos
import dedup      # planeja a deduplicação (fase 2) a partir dos manifestos
import espelhamento  # planeja o espelhamento / compra de discos (fase 2)
import consolidado   # visão entre discos: o que está repetido onde (fase 2)
import executar       # execução guiada e segura da limpeza (fase 2)
import importar       # traz manifestos varridos em outras máquinas


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
# Pacotes .zip para o gestor do acervo — FORA da pasta de manifestos (que a
# importação lê inteira).
ENVIOS_DIR = Path(os.environ.get("ENVIOS_DIR") or (MANIFEST_DIR.parent / "envios"))

app = FastAPI(title="PRESERVA-SCAN — Operacao")
templates = Jinja2Templates(directory=_recurso("templates"))
templates.env.globals["humano"] = relatorio.humano   # formatar bytes nos templates
templates.env.filters["data_curta"] = acervo.data_curta   # ISO -> dd/mm/aaaa


def _tem_manifestos():
    return bool(list(MANIFEST_DIR.glob("manifesto_*.jsonl")))


def _ctx(**extra):
    """Contexto comum a todas as telas (sidebar): status do banco e se há manifestos."""
    base = {"online": db.conectado(), "tem_manifestos": _tem_manifestos(), "versao": VERSAO}
    base.update(extra)
    return base

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
        # fase de identificação (Siegfried em lotes / ExifTool): progresso próprio
        "ident_etapa": None, "ident_feitos": 0, "ident_total": 0,
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
    return [_enriquecer(d) for d in achados]


def _enriquecer(d):
    """Acrescenta ao disco listado o que o próprio disco informa: marca, modelo,
    tipo físico (para o ícone), nº de série e espaço livre. Se o nº de série bate
    com a ficha de um HD já varrido, sugere a MESMA etiqueta (retomada segura,
    sem depender de o operador redigitar o nome igualzinho)."""
    import shutil
    d = dict(d, hw=None, reconhecido=None, livre="", livre_pct=None)
    try:
        uso = shutil.disk_usage(d["valor"])
        if uso.total:
            d["livre"] = _humano_tb(uso.free)
            d["livre_pct"] = round(100 * uso.free / uso.total, 1)
            d["livre_bytes"], d["total_bytes"] = uso.free, uso.total
    except Exception:
        pass
    try:
        d["hw"] = hardware.info_para(d["valor"])
        d["reconhecido"] = hardware.reconhecer(MANIFEST_DIR, d["hw"])
    except Exception:
        pass
    return d


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
    elif tipo == "identificando":
        JOB["ident_etapa"] = ev.get("etapa", JOB["ident_etapa"])
        if ev.get("total") is not None:
            JOB["ident_feitos"] = ev.get("feitos", 0)
            JOB["ident_total"] = ev["total"]
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
    log_path = nomes.novo_registro(MANIFEST_DIR, disco)
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


def _job_para_acervo():
    """Operação em andamento, no formato do cartão do Acervo (ou None)."""
    if not _rodando():
        return None
    pct = None
    if JOB["estado"] == "identificando" and JOB.get("ident_total"):
        pct = round(100 * JOB["ident_feitos"] / JOB["ident_total"])
        fase = "Identificando formatos"
    elif JOB["estado"] == "completando":
        pct = round(100 * JOB["lidos"] / JOB["total"]) if JOB["total"] else None
        fase = "Completando dados"
    elif JOB["estado"] == "contando":
        fase = "Contando arquivos"
    else:
        pct = round(100 * JOB["lidos"] / JOB["total"]) if JOB["total"] else None
        fase = "Lendo e conferindo arquivos"
    return {"disco": JOB["disco"], "pct": pct, "fase": fase}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, ok: str = ""):
    """Tela inicial: o Acervo — cartões com o estado de cada HD, montados dos
    manifestos e fichas deste computador (funciona offline)."""
    try:
        conectados = discos_disponiveis()
    except Exception:
        conectados = []
    try:
        envio = pacote.situacao_envio(MANIFEST_DIR, ENVIOS_DIR)
        dados = acervo.montar(MANIFEST_DIR, job=_job_para_acervo(), conectados=conectados, envio=envio)
    except Exception as e:
        dados = {"secoes": [], "kpi": {}, "novos": 0, "erro": f"{type(e).__name__}: {e}"}
    return templates.TemplateResponse(request, "acervo.html", _ctx(
        ativo="acervo", rodando=_rodando(), job_disco=JOB["disco"], ok=ok[:200], **dados))


# ---------------------------------------------------------------- conteúdo do HD
@app.get("/conteudo/{etiqueta}", response_class=HTMLResponse)
def tela_conteudo(request: Request, etiqueta: str, pasta: str = "", busca: str = "",
                  tipo: str = "", ordem: str = "nome", pagina: int = 1, parcial: int = 0,
                  desde: int = 0):
    """Pastas e arquivos do HD, lidos do inventário — nunca do disco. Durante a
    varredura deste HD a tela se atualiza sozinha (parcial=1 devolve só o miolo)."""
    if not importar._validar_etiqueta(etiqueta):
        return PlainTextResponse("HD não encontrado.", status_code=404)
    try:
        dados = conteudo.listar(MANIFEST_DIR, etiqueta, pasta, busca, tipo, ordem, pagina)
    except Exception as e:
        dados, erro = None, f"Não foi possível ler o inventário ({type(e).__name__}: {e})"
    else:
        erro = None
    job = _job_para_acervo()
    varrendo = job if job and job["disco"] == etiqueta else None
    ctx = _ctx(ativo="acervo", etiqueta=etiqueta, d=dados, erro=erro, varrendo=varrendo, desde=desde,
               tipos=conteudo.TIPOS, ficha=hardware.ler_ficha(MANIFEST_DIR, etiqueta),
               humano=acervo.humano)   # tamanhos como no Acervo (decimal: 4 TB = 4 TB)
    nome = "_conteudo_corpo.html" if parcial else "conteudo.html"
    return templates.TemplateResponse(request, nome, ctx, status_code=200 if dados or varrendo else 404)


# ---------------------------------------------------------------- HD não reconhecido
@app.get("/registrar", response_class=HTMLResponse)
def tela_registrar(request: Request, etiqueta: str = ""):
    """Registra no catálogo um HD que o computador não reconhece — para ele não
    sumir do controle (antes ficava só na planilha) e o gestor saber o sintoma."""
    etiqueta = etiqueta.strip()
    ficha = hardware.ler_ficha(MANIFEST_DIR, etiqueta) if etiqueta else {}
    return _tela_registrar(request, etiqueta, ficha, hardware.nao_reconhecido(ficha) or {})


def _tela_registrar(request, etiqueta, ficha, nr, erro=None, status_code=200):
    return templates.TemplateResponse(request, "registrar.html", _ctx(
        ativo="acervo", etiqueta=etiqueta, ficha=ficha, nr=nr, erro=erro,
        sintomas=acervo.SINTOMAS, conhecidas=_etiquetas_conhecidas()), status_code=status_code)


def _etiquetas_conhecidas():
    """Nomes já usados no catálogo (inventários + fichas): sugeridos ao digitar,
    para não nascer um "HD_R" quando já existe "HD R"."""
    try:
        return sorted(set(pacote.etiquetas(MANIFEST_DIR)) | set(hardware.fichas(MANIFEST_DIR)), key=str.lower)
    except Exception:
        return []


@app.post("/registrar", response_class=HTMLResponse)
def registrar_hd(request: Request, etiqueta: str = Form(""), grupo: str = Form(""),
                 capacidade: str = Form(""), sintoma: str = Form(""), obs: str = Form("")):
    etiqueta = etiqueta.strip()
    if not importar._validar_etiqueta(etiqueta):
        return _tela_registrar(request, etiqueta, {}, {"sintoma": sintoma, "obs": obs}, status_code=400,
                               erro='Informe o nome do HD (sem os caracteres < > : " / \\ | ? *).')
    hardware.atualizar_ficha(
        MANIFEST_DIR, etiqueta, grupo=grupo.strip() or None,
        capacidade_nominal=capacidade.strip() or None,
        nao_reconhecido={"sintoma": sintoma if sintoma in acervo.SINTOMAS else "outro",
                         "obs": obs.strip()[:2000],
                         "em": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return RedirectResponse(url="/?ok=" + quote(f"{etiqueta} registrado como não reconhecido."),
                            status_code=303)


@app.post("/registrar/resolvido")
def registrar_resolvido(etiqueta: str = Form("")):
    """O HD voltou a ser reconhecido (ou foi registrado por engano): tira a marca.
    Quando ele for lido de novo, a marca também deixa de valer sozinha
    (hardware.nao_reconhecido)."""
    etiqueta = etiqueta.strip()
    if importar._validar_etiqueta(etiqueta):
        hardware.atualizar_ficha(MANIFEST_DIR, etiqueta, remover=("nao_reconhecido",))
    return RedirectResponse(url="/?ok=" + quote(f"{etiqueta}: marca de não reconhecido retirada."),
                            status_code=303)


# ---------------------------------------------------------------- pacote de envio
def _tela_enviar(request, resultado=None, erro=None, status_code=200):
    sit = pacote.situacao_envio(MANIFEST_DIR, ENVIOS_DIR)
    discos = []
    for e, v in sit.items():
        arq = nomes.manifesto(MANIFEST_DIR, e)
        try:
            r = acervo.resumo_manifesto(arq)
            arquivos, volume = acervo._milhar(r["arquivos"]), acervo.humano(r["bytes"])
        except Exception:
            arquivos, volume = "", ""
        discos.append({"etiqueta": e, "pendente": v["pendente"], "em": v["em"] or "",
                       "arquivos": arquivos, "volume": volume,
                       "tamanho_manifesto": acervo.humano(arq.stat().st_size)})
    return templates.TemplateResponse(request, "enviar.html", _ctx(
        ativo="enviar", discos=discos, avulsas=pacote.fichas_sem_inventario(MANIFEST_DIR),
        pacotes=pacote.pacotes(ENVIOS_DIR)[:10], pasta=str(ENVIOS_DIR),
        resultado=resultado, erro=erro, humano=acervo.humano), status_code=status_code)


@app.get("/enviar", response_class=HTMLResponse)
def tela_enviar(request: Request):
    return _tela_enviar(request)


@app.post("/enviar", response_class=HTMLResponse)
def gerar_pacote(request: Request, etiqueta: List[str] = Form([])):
    # Rota síncrona (def): roda no threadpool — compactar GBs não trava o painel.
    if not etiqueta:
        return _tela_enviar(request, erro="Marque pelo menos um HD para o pacote.", status_code=400)
    try:
        res = pacote.gerar(MANIFEST_DIR, ENVIOS_DIR, etiqueta)
    except Exception as e:
        return _tela_enviar(request, erro=f"Não foi possível gerar o pacote: {e}", status_code=500)
    return _tela_enviar(request, resultado=res)


@app.get("/enviar/baixar/{nome}")
def baixar_pacote(nome: str):
    nome = os.path.basename(nome)
    arq = ENVIOS_DIR / nome
    if not (nome.startswith("PRESERVA-SCAN_") and nome.endswith(".zip") and arq.is_file()):
        return PlainTextResponse("Pacote não encontrado.", status_code=404)
    return FileResponse(arq, filename=nome, media_type="application/zip")


@app.post("/enviar/abrir")
def abrir_pasta_envios():
    """Abre a pasta dos pacotes no Explorer (só no Windows, onde o .exe roda)."""
    ENVIOS_DIR.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows":
        try:
            os.startfile(str(ENVIOS_DIR))  # noqa: S606 — abre o Explorer na pasta
        except Exception:
            pass
    return RedirectResponse(url="/enviar", status_code=303)


@app.get("/varrer", response_class=HTMLResponse)
def tela_varrer(request: Request, etiqueta: str = "", completar: str = ""):
    """Formulário de varredura. `etiqueta` chega dos botões do Acervo (Continuar /
    Completar dados) para não precisar redigitar o nome do HD."""
    return templates.TemplateResponse(request, "index.html", _ctx(
        ativo="varredura", discos=discos_disponiveis(),
        rodando=_rodando(), job_estado=JOB["estado"], job_disco=JOB["disco"],
        ferramentas=ferramentas_status(), etiqueta=etiqueta.strip(),
        completar=bool(completar),
    ))


@app.get("/passo/relatorio", response_class=HTMLResponse)
def passo_relatorio(request: Request):
    return templates.TemplateResponse(request, "relatorio.html", _ctx(ativo="relatorio"))


@app.get("/passo/dedup", response_class=HTMLResponse)
def passo_dedup(request: Request):
    return templates.TemplateResponse(request, "dedup.html", _ctx(ativo="dedup", resumo=None, pasta=str(DEDUP_DIR)))


@app.get("/passo/espelhamento", response_class=HTMLResponse)
def passo_espelhamento(request: Request):
    return templates.TemplateResponse(request, "espelhamento.html", _ctx(ativo="espelhamento", resumo=None, pasta=str(ESPELHO_DIR)))


@app.get("/passo/consolidado", response_class=HTMLResponse)
def passo_consolidado(request: Request):
    return templates.TemplateResponse(request, "consolidado.html", _ctx(ativo="consolidado", resumo=None, pasta=str(CONSOLIDADO_DIR)))


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


@app.get("/config", response_class=HTMLResponse)
def config_form(request: Request):
    return templates.TemplateResponse(request, "config.html",
        _ctx(ativo="config", valor=db.DATABASE_URL or ""))


@app.post("/config", response_class=HTMLResponse)
def config_salvar(request: Request, connection_string: str = Form("")):
    url = connection_string.strip()
    if not url:
        # Desconectar: volta ao modo offline e remove do .env.
        db.configurar("")
        _salvar_env("")
        return RedirectResponse(url="/", status_code=303)
    ok, msg = db.testar(url)
    if not ok:
        return templates.TemplateResponse(request, "config.html",
            _ctx(ativo="config", valor=url, erro=msg), status_code=400)
    db.configurar(url)
    _salvar_env(url)
    return RedirectResponse(url="/", status_code=303)


@app.post("/varrer")
def varrer(disco: str = Form(...), raiz: str = Form(...), subpasta: str = Form(""), grupo: str = Form("")):
    caminho = os.path.join(raiz, subpasta) if subpasta.strip() else raiz
    if not disco.strip():
        return HTMLResponse("<p>Informe a etiqueta do disco. <a href='/varrer'>voltar</a></p>", status_code=400)
    if not os.path.isdir(caminho):
        return HTMLResponse(
            f"<p>Caminho nao encontrado ou inacessivel: <b>{caminho}</b>. "
            f"Confira se o disco esta ligado/montado. <a href='/varrer'>voltar</a></p>", status_code=400)
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
        return HTMLResponse("<p>Informe a etiqueta do disco. <a href='/varrer'>voltar</a></p>", status_code=400)
    if not os.path.isdir(caminho):
        return HTMLResponse(
            f"<p>Caminho não encontrado: <b>{html.escape(caminho)}</b>. Ligue o disco. "
            f"<a href='/varrer'>voltar</a></p>", status_code=400)
    if not (MANIFEST_DIR / f"manifesto_{disco.strip()}.jsonl").exists():
        return HTMLResponse(
            f"<p>Ainda não há manifesto para <b>{html.escape(disco.strip())}</b> — "
            f"faça a varredura antes de completar metadados. <a href='/varrer'>voltar</a></p>", status_code=400)
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


def _tela_importar(request, resultado=None, erro=None, status_code=200):
    return templates.TemplateResponse(request, "importar.html", _ctx(
        ativo="importar", resultado=resultado, erro=erro, pasta=str(MANIFEST_DIR),
        existentes=list(nomes.manifestos(MANIFEST_DIR)),
    ), status_code=status_code)


@app.get("/passo/importar", response_class=HTMLResponse)
def passo_importar(request: Request):
    return _tela_importar(request)


@app.post("/importar", response_class=HTMLResponse)
def importar_manifestos(request: Request, arquivos: List[UploadFile] = File(default=[]),
                        origem: str = Form(""), substituir: str = Form("")):
    """Traz manifestos de outra máquina: arquivos enviados (.jsonl/.csv/.zip) ou uma
    pasta desta máquina (ex.: pendrive). Rota síncrona: roda no threadpool."""
    if _rodando():
        return _tela_importar(request, erro="Há uma varredura em andamento — importe "
                              "quando ela terminar.", status_code=409)
    enviados = [a for a in arquivos if a.filename]
    try:
        if enviados:
            # Grava os uploads numa pasta temporária e importa dela: assim a leitura
            # é sob demanda (um disco por vez), como na importação de pasta.
            import tempfile
            with tempfile.TemporaryDirectory(dir=MANIFEST_DIR.parent) as tmp:
                for a in enviados:
                    destino = Path(tmp) / os.path.basename(a.filename.replace("\\", "/"))
                    with open(destino, "wb") as f:
                        shutil.copyfileobj(a.file, f)
                res = importar.importar_de(tmp, MANIFEST_DIR, substituir=bool(substituir))
        elif origem.strip():
            res = importar.importar_de(origem.strip().strip('"'), MANIFEST_DIR,
                                       substituir=bool(substituir))
        else:
            return _tela_importar(request, erro="Escolha os arquivos ou informe uma pasta.",
                                  status_code=400)
    except (FileNotFoundError, ValueError) as e:
        return _tela_importar(request, erro=str(e), status_code=400)
    return _tela_importar(request, resultado=res)


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
        "ident_etapa": JOB["ident_etapa"], "ident_feitos": JOB["ident_feitos"],
        "ident_total": JOB["ident_total"],
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


DEDUP_DIR = MANIFEST_DIR / "dedup"


@app.post("/dedup", response_class=HTMLResponse)
def gerar_dedup(request: Request):
    """Gera o plano de deduplicação (não apaga nada — só propõe)."""
    try:
        resumo = dedup.planejar(MANIFEST_DIR, DEDUP_DIR)
    except Exception as e:
        return templates.TemplateResponse(request, "dedup.html",
            _ctx(ativo="dedup", resumo=None, pasta=str(DEDUP_DIR), erro=f"{type(e).__name__}: {e}"))
    return templates.TemplateResponse(request, "dedup.html",
        _ctx(ativo="dedup", resumo=resumo, pasta=str(DEDUP_DIR)))


ESPELHO_DIR = MANIFEST_DIR / "espelhamento"


@app.post("/espelhamento", response_class=HTMLResponse)
def gerar_espelhamento(request: Request):
    """Gera o plano de espelhamento (quantos discos comprar). Não copia nada."""
    try:
        resumo = espelhamento.planejar(MANIFEST_DIR, ESPELHO_DIR)
    except Exception as e:
        return templates.TemplateResponse(request, "espelhamento.html",
            _ctx(ativo="espelhamento", resumo=None, pasta=str(ESPELHO_DIR), erro=f"{type(e).__name__}: {e}"))
    return templates.TemplateResponse(request, "espelhamento.html",
        _ctx(ativo="espelhamento", resumo=resumo, pasta=str(ESPELHO_DIR)))


CONSOLIDADO_DIR = MANIFEST_DIR / "consolidado"


@app.post("/consolidado", response_class=HTMLResponse)
def gerar_consolidado(request: Request):
    """Gera o mapa entre discos (o que está repetido onde). Não altera nada."""
    try:
        resumo = consolidado.gerar(MANIFEST_DIR, CONSOLIDADO_DIR)
    except Exception as e:
        return templates.TemplateResponse(request, "consolidado.html",
            _ctx(ativo="consolidado", resumo=None, pasta=str(CONSOLIDADO_DIR), erro=f"{type(e).__name__}: {e}"))
    return templates.TemplateResponse(request, "consolidado.html",
        _ctx(ativo="consolidado", resumo=resumo, pasta=str(CONSOLIDADO_DIR)))


EXEC_DIR = MANIFEST_DIR / "execucao"


def _plano_dedup():
    return DEDUP_DIR / "plano_dedup.csv"


def _labels_do_plano():
    """Rótulos de disco citados no plano (remover + manter), para o operador mapear."""
    p = _plano_dedup()
    if not p.exists():
        return []
    import csv as _csv
    labels = set()
    with open(p, encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            if r.get("disco_label"):
                labels.add(r["disco_label"])
            if r.get("manter_disco"):
                labels.add(r["manter_disco"])
    return sorted(labels)


def _ctx_exec(**extra):
    base = dict(ativo="execucao", plano_existe=_plano_dedup().exists(),
                labels=_labels_do_plano(), drives=discos_disponiveis(),
                pasta=str(EXEC_DIR), verif=None, montados_json="{}",
                resultado=None, restaurado=None)
    base.update(extra)
    return _ctx(**base)


def _montados_do_form(form):
    montados = {}
    for lbl in _labels_do_plano():
        v = (form.get("map::" + lbl) or "").strip()
        if v:
            montados[lbl] = v
    return montados


@app.get("/passo/execucao", response_class=HTMLResponse)
def passo_execucao(request: Request):
    return templates.TemplateResponse(request, "execucao.html", _ctx_exec())


@app.post("/execucao/verificar", response_class=HTMLResponse)
async def execucao_verificar(request: Request):
    form = await request.form()
    montados = _montados_do_form(form)
    resumo = executar.verificar(_plano_dedup(), montados, EXEC_DIR)
    resumo.pop("_resultados", None)
    import json as _json
    return templates.TemplateResponse(request, "execucao.html",
        _ctx_exec(verif=resumo, montados_json=_json.dumps(montados)))


@app.post("/execucao/executar", response_class=HTMLResponse)
async def execucao_executar(request: Request):
    import json as _json
    form = await request.form()
    montados = _json.loads(form.get("montados") or "{}")
    if form.get("confirmo") != "sim":
        # sem confirmação: reapresenta a simulação
        resumo = executar.verificar(_plano_dedup(), montados, EXEC_DIR)
        resumo.pop("_resultados", None)
        return templates.TemplateResponse(request, "execucao.html",
            _ctx_exec(verif=resumo, montados_json=_json.dumps(montados)))
    res = executar.executar(_plano_dedup(), montados, EXEC_DIR, confirmar=True)
    return templates.TemplateResponse(request, "execucao.html",
        _ctx_exec(resultado=res, montados_json=_json.dumps(montados)))


@app.post("/execucao/restaurar", response_class=HTMLResponse)
async def execucao_restaurar(request: Request):
    import json as _json
    form = await request.form()
    montados = _json.loads(form.get("montados") or "{}")
    res = executar.restaurar(montados)
    return templates.TemplateResponse(request, "execucao.html",
        _ctx_exec(restaurado=res, montados_json=_json.dumps(montados)))


@app.get("/log", response_class=PlainTextResponse)
def log():
    p = Path(JOB["log_path"]) if JOB["log_path"] else None
    if not p or not p.exists():
        return "(sem log ainda)"
    linhas = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(linhas[-TAIL_LINHAS:])
