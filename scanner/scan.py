#!/usr/bin/env python3
"""
PRESERVA-SCAN — modulo de varredura do acervo (SOMENTE LEITURA).

Para cada arquivo de um disco: calcula SHA-256, identifica o formato
(Siegfried/PRONOM), extrai metadados tecnicos (MediaInfo/ExifTool),
grava um manifesto local (CSV+JSON) e, se configurado, envia o
inventario ao banco hospedado (Supabase/Postgres) para o dashboard.

*** NUNCA escreve, move ou apaga arquivos do acervo. Apenas le. ***

Uso:
    python scan.py --disco "Antigos-C" --raiz /mnt/hd --grupo antigos
    python scan.py --disco "Antigos-C" --raiz /mnt/hd --force   # re-varre tudo
"""
import os
import sys
import csv
import json
import time
import uuid
import shutil
import hashlib
import argparse
import subprocess
import datetime
from pathlib import Path

import db  # persistencia (Supabase/Postgres) + idempotencia

CHUNK = 1024 * 1024          # 1 MiB por bloco de leitura (seguro para videos grandes)
LOTE_DB = 200                # envia ao banco a cada N arquivos
MANIFESTOS = Path(os.environ.get("MANIFEST_DIR", "./manifestos"))

# --- Emissao de progresso estruturado ---------------------------------------
# Dois destinos possiveis, ambos desligados por padrao (uso por linha de comando
# fica identico ao ja testado em campo):
#   EMIT=True      -> escreve linhas "@@PS@@ {json}" em stdout (--progress-json)
#   CALLBACK=func  -> chama func(evento) no mesmo processo (usado pelo painel .exe)
EMIT = False
CALLBACK = None
SENTINELA = "@@PS@@ "
_ULTIMO_EMIT = 0.0


def _emit(obj, forcar=False, mingap=1.0):
    """Emite um evento de progresso, com throttle por tempo.

    Eventos importantes (fase, total, done, erro, aviso) usam forcar=True e nunca
    sao descartados; batimentos de progresso sao limitados a 1 por `mingap`."""
    global _ULTIMO_EMIT
    if CALLBACK is None and not EMIT:
        return
    agora = time.monotonic()
    if not forcar and (agora - _ULTIMO_EMIT) < mingap:
        return
    _ULTIMO_EMIT = agora
    if CALLBACK is not None:
        try:
            CALLBACK(obj)
        except Exception:
            pass
    if EMIT:
        try:
            sys.stdout.write(SENTINELA + json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass

# MediaInfo so roda em audiovisual; ExifTool so em imagens. O resto recebe apenas
# hash + identificacao de formato (evita centenas de milhares de chamadas de processo).
EXT_VIDEO_AUDIO = {"mov", "mp4", "mxf", "avi", "mkv", "mts", "m2ts", "m4v", "mpg",
                   "mpeg", "wmv", "flv", "webm", "dv", "r3d", "braw", "vob", "3gp",
                   "wav", "aif", "aiff", "mp3", "flac", "m4a", "aac", "bwf", "ac3", "ogg"}
EXT_IMAGEM = {"jpg", "jpeg", "tif", "tiff", "png", "dpx", "cr2", "nef", "arw", "dng",
              "gif", "bmp", "psd", "heic", "webp"}

# Pastas/arquivos de SISTEMA e lixo que nunca são acervo — pulados sempre, para
# não perder tempo (Lixeira, metadados de volume) nem tropeçar em arquivos
# bloqueados do Windows (pagefile/hiberfil). Nomes comparados em minúsculas.
IGNORAR_PASTAS = {
    "$recycle.bin", "system volume information", "$winreagent", "$sysreset",
    "config.msi", "recovery", "found.000", "found.001", "lost+found",
    ".trashes", ".spotlight-v100", ".fseventsd", "$getcurrent",
}
IGNORAR_ARQUIVOS = {
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "dumpstack.log", "dumpstack.log.tmp", "desktop.ini", "thumbs.db",
}


def _pular_dir(nome):
    return nome.lower() in IGNORAR_PASTAS


def _pular_arquivo(nome):
    return nome.lower() in IGNORAR_ARQUIVOS


def _norm(p):
    """Normaliza separadores de caminho (Windows/Unix) para casar os mapas."""
    return str(p).replace("\\", "/")


def _dirs_ferramentas():
    """Pastas onde procurar binários (mediainfo, exiftool, sf): embutidos no .exe
    (_MEIPASS/ferramentas) ou numa pasta 'ferramentas' ao lado do programa. Assim
    o operador NÃO precisa instalar nem configurar PATH — basta o .exe."""
    dirs = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        dirs.append(Path(mp) / "ferramentas")
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).parent / "ferramentas")
    dirs.append(Path(__file__).parent / "ferramentas")
    dirs.append(Path.cwd() / "ferramentas")
    return dirs


def ferramenta(nome):
    """Caminho do executável `nome` (ex.: 'mediainfo'): primeiro nas pastas
    'ferramentas', depois no PATH do sistema. None se não achar."""
    for d in _dirs_ferramentas():
        for c in (nome, nome + ".exe"):
            p = d / c
            if p.exists():
                return str(p)
    return shutil.which(nome)


def sha256_de(caminho: Path, pulso=None) -> str:
    """Calcula o SHA-256 lendo em blocos. `pulso(bytes_lidos)` e chamado a cada
    bloco: serve de batimento cardiaco para o painel enxergar que um arquivo
    grande (video de dezenas de GB) esta AVANCANDO, e nao travado."""
    h = hashlib.sha256()
    lidos = 0
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(CHUNK), b""):
            h.update(bloco)
            lidos += len(bloco)
            if pulso is not None:
                pulso(lidos)
    return h.hexdigest()


# Siegfried em LOTES (via `sf -f lista`), e não numa passada única sobre a árvore:
#  - cada lote concluído vira progresso real no painel (antes: horas de "0 lidos");
#  - um lote que estoura o tempo é refeito arquivo a arquivo, isolando só o arquivo
#    que travou o sf (setor defeituoso) sem perder a identificação dos demais;
#  - a lista já sai filtrada (sem Lixeira / System Volume Information).
# Com a assinatura do .exe (limitada a 1 MiB do início e do fim de cada arquivo,
# como o DROID), identificar um vídeo de 4 GiB leva ~0,2 s; sem o limite, o sf lê
# o arquivo INTEIRO — no HD todo, uma leitura completa do disco só para formato.
SF_LOTE = 200               # arquivos por processo do sf
SF_TEMPO_LOTE = 600         # s: lote que não termina nisso tem arquivo travado
SF_TEMPO_ARQUIVO = 120      # s: limite por arquivo quando o lote é refeito um a um
SF_MAX_TRAVADOS = 5         # mais que isso = disco degradando: interrompe a identificação
EXIF_TEMPO_BASE = 900       # s: ExifTool em lote — tempo-limite = base + por imagem
EXIF_TEMPO_POR_IMAGEM = 2


def _sf_env(sf):
    """Ambiente do sf. O Siegfried precisa do arquivo de assinaturas (default.sig):
    quando o sf.exe vem EMBUTIDO no .exe, o default.sig está ao lado dele — então
    apontamos SIEGFRIED_HOME para essa pasta (sem isso o sf roda mas não identifica
    nada). Sem .sig ao lado, o sf usa o próprio HOME (instalação nativa)."""
    env = os.environ.copy()
    if not env.get("SIEGFRIED_HOME"):
        sig_local = Path(sf).parent / "default.sig"
        if sig_local.exists():
            env["SIEGFRIED_HOME"] = str(Path(sf).parent)
    return env


def _sf_limitado(sf, env):
    """True se a assinatura em uso limita a leitura (max BOF/EOF) — a do .exe limita.
    Uma assinatura sem limite (ex.: `sf -update` numa instalação nativa) lê cada
    arquivo inteiro, e então os tempos-limite precisam ser bem mais folgados."""
    try:
        r = subprocess.run([sf, "-version"], capture_output=True, encoding="utf-8",
                           errors="replace", timeout=60, env=env)
        return "max BOF" in (r.stdout + r.stderr)
    except Exception:
        return False


def _listar_arquivos(raiz: Path):
    """Arquivos sob `raiz`, com os mesmos filtros da varredura (sem pastas de sistema
    e lixo). Não consulta o disco além da listagem: o sf reporta o que não abrir."""
    lista = []
    for dirpath, dirs, files in os.walk(raiz):
        # ordem alfabética: determinística em qualquer sistema (o NTFS já lista
        # assim; no Linux a ordem do os.walk é arbitrária)
        dirs[:] = sorted(d for d in dirs if not _pular_dir(d))
        lista += [str(Path(dirpath) / n) for n in sorted(files) if not _pular_arquivo(n)]
    return lista


def _sf_rodar(sf, env, caminhos, timeout):
    """Identifica `caminhos` num único processo do sf (`-csv -f lista`). Devolve
    {caminho: (puid, formato)}, ou None se o sf estourou `timeout` ou falhou."""
    import tempfile
    fd, lista = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    fd, saida = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    try:
        with open(lista, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(caminhos) + "\n")
        with open(saida, "wb") as out:
            p = subprocess.Popen([sf, "-csv", "-f", lista], stdout=out,
                                 stderr=subprocess.DEVNULL, env=env)
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
                return None
        pedidos = {_norm(c): c for c in caminhos}
        res = {}
        with open(saida, encoding="utf-8", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                c = pedidos.get(_norm(r.get("filename") or ""))
                if c and c not in res and r.get("id"):
                    res[c] = (r.get("id"), r.get("format"))
        if not res and p.returncode != 0:
            return None
        return res
    except Exception:
        return None
    finally:
        for t in (lista, saida):
            try:
                os.unlink(t)
            except OSError:
                pass


def _progresso_identificacao(feitos, total):
    """Batimento da fase de identificação: o painel mostra 'N de M' e uma barra real."""
    _emit({"event": "identificando", "etapa": "formatos", "feitos": feitos,
           "total": total}, forcar=(feitos in (0, total)), mingap=1.0)


def siegfried_lote(raiz: Path, progresso=None):
    """Identifica o formato (PRONOM) de todos os arquivos sob `raiz` e devolve
    {caminho_relativo: (puid, formato)}. Roda em lotes com tempo-limite (veja acima):
    nunca trava a varredura, e `progresso(feitos, total)` alimenta a barra do painel."""
    sf = ferramenta("sf")
    if not sf:
        print("AVISO: 'sf' (Siegfried) nao encontrado; seguindo sem identificacao de formato.")
        return {}
    env = _sf_env(sf)
    limitado = _sf_limitado(sf, env)
    # Assinatura sem limite lê arquivos inteiros: tempos-limite folgados para não
    # confundir um vídeo grande com um arquivo travado.
    t_lote = SF_TEMPO_LOTE if limitado else 6 * 3600
    t_arq = SF_TEMPO_ARQUIVO if limitado else 3600
    arquivos = _listar_arquivos(raiz)
    total = len(arquivos)
    achados, travados = {}, []
    if progresso:
        progresso(0, total)
    for i in range(0, total, SF_LOTE):
        lote = arquivos[i:i + SF_LOTE]
        res = _sf_rodar(sf, env, lote, t_lote)
        if res is None:
            # Algum arquivo do lote travou o sf (setor defeituoso): refaz um a um,
            # com limite por arquivo, para isolar só o culpado.
            for c in lote:
                r1 = _sf_rodar(sf, env, [c], t_arq)
                if r1 is None:
                    travados.append(c)
                    msg = "Siegfried nao respondeu (possivel setor defeituoso)"
                    print(f"  AVISO: formato nao identificado em '{c}' ({msg}).", flush=True)
                    _emit({"event": "aviso_arquivo", "arquivo": c, "erro": msg}, forcar=True)
                    if len(travados) > SF_MAX_TRAVADOS:
                        break
                else:
                    achados.update(r1)
        else:
            achados.update(res)
        if progresso:
            progresso(min(i + len(lote), total), total)
        if len(travados) > SF_MAX_TRAVADOS:
            print(f"AVISO: {len(travados)} arquivos travaram o Siegfried — o disco parece "
                  "estar se degradando. Identificacao de formato interrompida; o restante "
                  "segue sem formato. Recomenda-se CLONAR o disco antes de novas leituras.",
                  flush=True)
            _emit({"event": "aviso_arquivo", "arquivo": str(raiz),
                   "erro": "muitos arquivos travaram o Siegfried: identificacao interrompida"},
                  forcar=True)
            break
    mapa = {}
    for c, v in achados.items():
        try:
            rel = _norm(Path(c).relative_to(raiz))
        except Exception:
            rel = _norm(c)
        mapa[rel] = v
    return mapa


def mediainfo(caminho: Path):
    """Metadados tecnicos AV (codec, resolucao, duracao...) via MediaInfo."""
    exe = ferramenta("mediainfo")
    if not exe:
        return None
    try:
        # encoding/errors explícitos: sem isso, no Windows o Python decodifica a
        # saída do MediaInfo com a página de código local (cp1252) e QUEBRA
        # (UnicodeDecodeError) quando o JSON traz bytes UTF-8 — acentos, nomes de
        # câmera, strings embutidas. O arquivo então fica SEM metadados. Forçar
        # UTF-8 com errors="replace" resolve.
        r = subprocess.run([exe, "--Output=JSON", str(caminho)],
                           capture_output=True, encoding="utf-8", errors="replace",
                           timeout=180)
        return json.loads(r.stdout) if r.stdout.strip() else None
    except Exception:
        return None


def exiftool(caminho: Path):
    """Metadados embutidos (imagens) via ExifTool — chamada única (usada no backfill
    de poucos arquivos). Na varredura em massa, use exiftool_lote (uma passada)."""
    exe = ferramenta("exiftool")
    if not exe:
        return None
    try:
        # UTF-8 explícito (mesma razão do mediainfo): no Windows, text=True usaria
        # cp1252 e quebraria a leitura em nomes/metadados acentuados.
        r = subprocess.run([exe, "-json", str(caminho)],
                           capture_output=True, encoding="utf-8", errors="replace",
                           timeout=120)
        data = json.loads(r.stdout) if r.stdout.strip() else None
        return data[0] if data else None
    except Exception:
        return None


def _json_parcial(texto):
    """Lê a lista JSON do ExifTool mesmo truncada (processo morto no meio): devolve
    os objetos completos que conseguir, descartando só o último pedaço incompleto."""
    texto = (texto or "").strip()
    if not texto:
        return []
    try:
        return json.loads(texto)
    except Exception:
        pass
    dec, objs = json.JSONDecoder(), []
    i = texto.find("{")
    while i != -1:
        try:
            obj, j = dec.raw_decode(texto, i)
        except ValueError:
            break
        objs.append(obj)
        i = texto.find("{", j)
    return objs


def exiftool_lote(raiz: Path):
    """Roda o ExifTool UMA vez sobre a árvore, só nas extensões de imagem, e devolve
    {caminho_relativo: metadados}. Evita o custo de subir o processo do ExifTool
    (lançador Perl, ~0,2–0,5s cada) por imagem — o mesmo princípio do Siegfried em
    lote. Em disco com muitas imagens, isso é a diferença entre horas e minutos."""
    exe = ferramenta("exiftool")
    if not exe:
        return {}
    args = [exe, "-json", "-q", "-r", "-fast2"]
    for e in sorted(EXT_IMAGEM):
        args += ["-ext", e]
    args.append(str(raiz))
    # Tempo-limite proporcional ao nº de imagens: num setor defeituoso o ExifTool
    # pode travar para sempre (antes não havia limite). Estourou: mata o processo e
    # aproveita o que ele já tinha produzido; o resto fica para "Completar metadados".
    n_img = sum(1 for _, _, fs in os.walk(raiz) for n in fs
                if n.rsplit(".", 1)[-1].lower() in EXT_IMAGEM)
    if not n_img:
        return {}
    mapa = {}
    try:
        try:
            r = subprocess.run(args, capture_output=True, encoding="utf-8",
                               errors="replace",   # UTF-8 explícito
                               timeout=EXIF_TEMPO_BASE + EXIF_TEMPO_POR_IMAGEM * n_img)
            saida = r.stdout
        except subprocess.TimeoutExpired as e:
            saida = e.stdout or ""
            if isinstance(saida, bytes):
                saida = saida.decode("utf-8", errors="replace")
            print("AVISO: ExifTool excedeu o tempo-limite (possivel setor defeituoso); "
                  "usando os metadados de imagem lidos ate ali.", flush=True)
        data = _json_parcial(saida)
        for obj in data:
            src = obj.get("SourceFile")
            if not src:
                continue
            try:
                rel = _norm(Path(src).relative_to(raiz))
            except Exception:
                rel = _norm(src)
            mapa[rel] = obj
    except Exception as e:
        print(f"AVISO: ExifTool em lote falhou ({e}); imagens ficam sem metadados.")
    return mapa


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def humano(n):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def inspecionar(raiz: Path, mbps=120):
    """Dry-run: conta arquivos e bytes SEM calcular hash. Estima o tempo da varredura real."""
    n, total = 0, 0
    for dirpath, dirs, files in os.walk(raiz):
        dirs[:] = [d for d in dirs if not _pular_dir(d)]
        for nome in files:
            if _pular_arquivo(nome):
                continue
            p = Path(dirpath) / nome
            if p.is_symlink() or not p.is_file():
                continue
            try:
                total += p.stat().st_size
                n += 1
            except OSError:
                continue
    segundos = total / (mbps * 1_000_000)
    h, m = divmod(int(segundos // 60), 60)
    print(f"Arquivos: {n:,}".replace(",", "."))
    print(f"Volume total: {humano(total)}")
    print(f"Tempo estimado de varredura (@~{mbps} MB/s de leitura): ~{h}h{m:02d}min")
    print("(estimativa; a velocidade real do seu HD/USB pode variar)")
    return n, total


def _ja_do_manifesto(caminho_jsonl: Path):
    """Le um manifesto JSONL ja existente e devolve {caminho: (tamanho, mtime)}.
    Base da RETOMADA offline: numa nova passada, os arquivos que ja estao no
    inventario local sao pulados — sem precisar de banco."""
    ja = {}
    if not caminho_jsonl.exists():
        return ja
    try:
        with open(caminho_jsonl, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    r = json.loads(linha)
                    ja[r["caminho"]] = (r.get("tamanho_bytes"), r.get("mtime"))
                except Exception:
                    continue
    except Exception:
        pass
    return ja


def varrer(disco_label, raiz: Path, grupo=None, force=False):
    run_id = str(uuid.uuid4())
    MANIFESTOS.mkdir(parents=True, exist_ok=True)
    # Um manifesto por disco (nome estavel, sem data): e o que permite RETOMAR de
    # onde parou e reconhecer o que ja foi inventariado numa nova passada.
    manifesto_csv = MANIFESTOS / f"manifesto_{disco_label}.csv"
    manifesto_json = MANIFESTOS / f"manifesto_{disco_label}.jsonl"

    db.registrar_disco(disco_label, grupo=grupo)
    db.abrir_run(run_id, disco_label)

    # O que ja foi lido antes (para pular): banco (se online) + manifesto local
    # (offline). --force ignora tudo e revarre do zero.
    ja = {}
    if not force:
        ja.update(db.ja_varridos(disco_label))
        ja.update(_ja_do_manifesto(manifesto_json))
    retomando = bool(ja)
    if retomando:
        _emit({"event": "retomar", "ja": len(ja)}, forcar=True)
        print(f"Retomando: {len(ja)} arquivo(s) ja no inventario serao pulados.",
              flush=True)

    # Fase 0 (so com painel): conta arquivos/bytes antes, para a barra ter um
    # denominador. Uma passada leve (stat, sem hash); emite batimentos para o
    # painel nao achar que travou. No modo CLI puro isso e pulado.
    total_arq, total_bytes = 0, 0
    if EMIT or CALLBACK is not None:
        _emit({"event": "phase", "fase": "contando"}, forcar=True)
        for dirpath, dirs, files in os.walk(raiz):
            dirs[:] = [d for d in dirs if not _pular_dir(d)]
            for nome in files:
                if _pular_arquivo(nome):
                    continue
                p = Path(dirpath) / nome
                try:
                    if p.is_symlink() or not p.is_file():
                        continue
                    total_arq += 1
                    total_bytes += p.stat().st_size
                except OSError:
                    continue
                if total_arq % 2000 == 0:
                    _emit({"event": "contagem", "total": total_arq,
                           "total_bytes": total_bytes}, mingap=1.0)
        _emit({"event": "total", "total": total_arq, "total_bytes": total_bytes},
              forcar=True)

    _emit({"event": "phase", "fase": "identificando"}, forcar=True)
    print("Identificando formatos (Siegfried, em lotes) e metadados de imagem "
          "(ExifTool)...", flush=True)
    mapa_puid = siegfried_lote(raiz, progresso=_progresso_identificacao)
    _emit({"event": "identificando", "etapa": "imagens"}, forcar=True)
    mapa_exif = exiftool_lote(raiz)

    _emit({"event": "phase", "fase": "varrendo"}, forcar=True)
    lote, novos, lidos, bytes_lidos, erros = [], 0, 0, 0, 0
    campos = ["disco_label", "caminho", "nome", "extensao", "tamanho_bytes",
              "mtime", "sha256", "puid", "formato"]

    def _progresso(forcar=False, arquivo=None, parcial=0):
        _emit({"event": "progress", "fase": "varrendo", "lidos": lidos,
               "novos": novos, "erros": erros, "total": total_arq,
               "bytes_lidos": bytes_lidos + parcial, "total_bytes": total_bytes,
               "arquivo": arquivo}, forcar=forcar, mingap=1.5)

    try:
        # buffering=1 (linha a linha): cada registro vai para o disco na hora. Assim,
        # se a varredura cair no meio (queda de energia, USB solta, janela fechada),
        # o manifesto NAO perde o final que estaria preso no buffer — e o CSV e o
        # JSONL param no mesmo ponto, nunca dessincronizados. O manifesto e, ele
        # proprio, objeto de preservacao; nao pode depender do fechamento limpo.
        # Retomando (nao-force e ja havia manifesto): ANEXA em vez de truncar,
        # para nao apagar o que ja foi inventariado. O cabecalho do CSV so e
        # escrito quando o arquivo esta comecando do zero.
        anexar = retomando and not force
        modo = "a" if anexar else "w"
        csv_ja_tem_cabecalho = (manifesto_csv.exists()
                                and manifesto_csv.stat().st_size > 0)
        with open(manifesto_csv, modo, newline="", encoding="utf-8", buffering=1) as fcsv, \
             open(manifesto_json, modo, encoding="utf-8", buffering=1) as fjson:
            w = csv.DictWriter(fcsv, fieldnames=campos)
            if not (anexar and csv_ja_tem_cabecalho):
                w.writeheader()

            def _falha(alvo, e):
                # Falha de leitura NUNCA derruba a varredura nem some em silêncio:
                # conta, registra no log e segue. Ao fim, "N com falha de leitura"
                # avisa que o disco não foi lido por inteiro (re-rodar retoma).
                nonlocal erros
                erros += 1
                msg = f"{type(e).__name__}: {e}"
                print(f"  AVISO: falha ao ler '{alvo}' ({msg}); pulando.", flush=True)
                _emit({"event": "aviso_arquivo", "arquivo": str(alvo), "erro": msg},
                      forcar=True)

            # onerror: pasta que não pode ser listada (setor defeituoso, permissão)
            # antes era pulada em silêncio — todo o conteúdo dela sumia do inventário.
            for dirpath, dirs, files in os.walk(
                    raiz, onerror=lambda e: _falha(getattr(e, "filename", "?"), e)):
                dirs[:] = [d for d in dirs if not _pular_dir(d)]
                for nome in files:
                    if _pular_arquivo(nome):
                        continue
                    caminho_abs = Path(dirpath) / nome
                    rel = str(caminho_abs.relative_to(raiz))
                    # is_symlink/is_file/stat consultam o disco: num setor defeituoso
                    # levantam OSError (ex.: WinError 23 -> errno 22) que, sem este
                    # try, abortava a varredura INTEIRA — sempre no mesmo arquivo.
                    try:
                        if caminho_abs.is_symlink() or not caminho_abs.is_file():
                            continue
                        st = caminho_abs.stat()
                    except OSError as e:
                        _falha(rel, e)
                        continue
                    lidos += 1

                    # IDEMPOTENCIA: se ja lido com mesmo tamanho e mtime, pula (a menos de --force)
                    if rel in ja:
                        tam_ant, mt_ant = ja[rel]
                        if tam_ant == st.st_size and mt_ant == iso(st.st_mtime):
                            _progresso(arquivo=rel)  # mostra avanco mesmo pulando
                            continue

                    if lidos % 500 == 0:
                        print(f"  {lidos} lidos / {novos} novos...", flush=True)

                    # Um arquivo ilegivel (permissao, setor defeituoso) NAO pode
                    # abortar a varredura inteira: registra o aviso e segue.
                    try:
                        sha = sha256_de(
                            caminho_abs,
                            pulso=lambda parcial: _progresso(arquivo=rel, parcial=parcial),
                        )
                        ext = caminho_abs.suffix.lower().lstrip(".")
                        puid, formato = mapa_puid.get(_norm(rel), (None, None))
                        # MediaInfo so em AV (por arquivo, aceitavel: sao poucos e
                        # grandes); imagens vêm do ExifTool em LOTE (mapa_exif),
                        # sem subir um processo por imagem; resto: so hash + formato
                        if ext in EXT_VIDEO_AUDIO:
                            meta = mediainfo(caminho_abs)
                        elif ext in EXT_IMAGEM:
                            meta = mapa_exif.get(_norm(rel))
                        else:
                            meta = None
                    except Exception as e:
                        _falha(rel, e)
                        continue

                    linha = {
                        "disco_label": disco_label,
                        "caminho": rel,
                        "nome": nome,
                        "extensao": ext,
                        "tamanho_bytes": st.st_size,
                        "mtime": iso(st.st_mtime),
                        "sha256": sha,
                        "puid": puid,
                        "formato": formato,
                        "mediainfo": meta,
                        "scan_run_id": run_id,
                    }
                    w.writerow({k: linha[k] for k in campos})
                    fjson.write(json.dumps(linha, ensure_ascii=False) + "\n")
                    lote.append(linha)
                    novos += 1
                    bytes_lidos += st.st_size
                    _progresso(arquivo=rel)
                    if len(lote) >= LOTE_DB:
                        db.enviar_lote(lote); lote = []

        db.enviar_lote(lote)
        db.fechar_run(run_id, novos, lidos)
        db.concluir_disco(disco_label)
    except Exception as e:
        # Falha global (banco caiu, disco desconectado no meio, etc.): marca a run
        # como falha no banco e avisa o painel de forma estruturada.
        msg = f"{type(e).__name__}: {e}"
        try:
            db.fechar_run(run_id, novos, lidos, status="falhou")
            db.concluir_disco(disco_label, status="falhou")
        except Exception:
            pass
        _emit({"event": "erro", "msg": msg, "lidos": lidos, "novos": novos},
              forcar=True)
        print(f"\nERRO: a varredura de '{disco_label}' falhou — {msg}", flush=True)
        raise

    _progresso(forcar=True)
    _emit({"event": "done", "lidos": lidos, "novos": novos, "erros": erros,
           "total": total_arq, "bytes_lidos": bytes_lidos,
           "csv": str(manifesto_csv), "jsonl": str(manifesto_json),
           "online": db.conectado()}, forcar=True)
    print(f"\nOK — disco '{disco_label}': {lidos} lidos, {novos} novos/alterados"
          + (f", {erros} com falha de leitura." if erros else "."))
    print(f"Manifestos: {manifesto_csv}  |  {manifesto_json}")
    if not db.conectado():
        print("(modo offline: dados so no manifesto local; defina DATABASE_URL para enviar ao dashboard)")


def _sem_formato(r):
    """Registro SEM identificação de formato: `puid` e `formato` ambos vazios.
    É o estado dos discos varridos por versões antigas do .exe (sf embutido sem o
    default.sig ao lado): hash correto, mas formato/PRONOM em branco. Se qualquer
    um dos dois já tem valor, o arquivo foi identificado — não se re-processa."""
    return not (r.get("puid") or r.get("formato"))


def backfill(disco_label, raiz: Path):
    """Completa os metadados que faltaram — SEM re-hashear:
      • MediaInfo/ExifTool onde `mediainfo` está vazio (áudio/vídeo e imagens);
      • Siegfried (formato/PUID) onde `puid`/`formato` ficaram vazios (discos
        varridos por versões antigas do .exe, sem o `default.sig`).
    Usa o manifesto já existente: para cada registro localiza o arquivo no disco,
    roda só as ferramentas que faltam e atualiza o inventário (JSONL **e** CSV,
    mantidos em sincronia) e o banco por upsert. O SHA-256 nunca é recalculado."""
    manifesto_json = MANIFESTOS / f"manifesto_{disco_label}.jsonl"
    manifesto_csv = MANIFESTOS / f"manifesto_{disco_label}.csv"
    if not manifesto_json.exists():
        msg = f"Manifesto não encontrado: {manifesto_json}. Faça a varredura antes."
        _emit({"event": "erro", "msg": msg}, forcar=True)
        sys.exit(msg)
    tem_mi = ferramenta("mediainfo") is not None
    tem_et = ferramenta("exiftool") is not None
    tem_sf = ferramenta("sf") is not None
    if not (tem_mi or tem_et or tem_sf):
        msg = ("Nenhuma ferramenta de metadados/formato disponível "
               "(forneça MediaInfo, ExifTool ou Siegfried).")
        _emit({"event": "erro", "msg": msg}, forcar=True)
        sys.exit(msg)

    db.registrar_disco(disco_label)
    _emit({"event": "phase", "fase": "identificando"}, forcar=True)

    # Pré-passada única: conta os registros e detecta se ALGUM está sem formato.
    # O Siegfried varre a árvore inteira (lê os cabeçalhos dos arquivos), então só
    # o rodamos quando de fato há formato a preencher — senão seria uma leitura
    # desnecessária do disco todo num backfill que só queria completar mídia.
    total = 0
    precisa_sf = False
    with open(manifesto_json, encoding="utf-8", errors="replace") as f:
        for linha in f:
            if not linha.strip():
                continue
            total += 1
            if tem_sf and not precisa_sf:
                try:
                    if _sem_formato(json.loads(linha)):
                        precisa_sf = True
                except Exception:
                    pass
    _emit({"event": "total", "total": total}, forcar=True)

    # Imagens: ExifTool em LOTE (rápido). Formato: Siegfried em LOTE (só se faltar
    # a algum registro). A/V: MediaInfo por arquivo, dentro do laço.
    mapa_puid = (siegfried_lote(raiz, progresso=_progresso_identificacao)
                 if (tem_sf and precisa_sf) else {})
    if tem_et:
        _emit({"event": "identificando", "etapa": "imagens"}, forcar=True)
    mapa_exif = exiftool_lote(raiz) if tem_et else {}

    _emit({"event": "phase", "fase": "completando"}, forcar=True)
    print(f"Completando metadados de '{disco_label}' ({total} registros)"
          + ("; incluindo formato/Siegfried" if mapa_puid else "") + "...", flush=True)

    # O CSV é reescrito a partir do JSONL (fonte da verdade, com todos os campos),
    # projetando as colunas estáveis — assim CSV e JSONL nunca dessincronizam quando
    # o formato passa a ser preenchido (puid/formato existem em ambos).
    campos = ["disco_label", "caminho", "nome", "extensao", "tamanho_bytes",
              "mtime", "sha256", "puid", "formato"]
    tmp = manifesto_json.with_name(manifesto_json.name + ".tmp")
    tmp_csv = manifesto_csv.with_name(manifesto_csv.name + ".tmp")
    lidos = feitos_mi = feitos_sf = 0
    lote = []
    with open(manifesto_json, encoding="utf-8", errors="replace") as fin, \
         open(tmp, "w", encoding="utf-8", buffering=1) as fout, \
         open(tmp_csv, "w", newline="", encoding="utf-8", buffering=1) as fcsv:
        w = csv.DictWriter(fcsv, fieldnames=campos)
        w.writeheader()
        for linha in fin:
            linha = linha.rstrip("\n")
            if not linha.strip():
                continue
            try:
                r = json.loads(linha)
            except Exception:
                fout.write(linha + "\n"); continue
            lidos += 1
            ext = (r.get("extensao") or "").lower()
            mudou = False

            # 1) Metadados de mídia (MediaInfo / ExifTool) onde faltaram.
            if r.get("mediainfo") in (None, {}, "") and (ext in EXT_VIDEO_AUDIO or ext in EXT_IMAGEM):
                meta = None
                try:
                    if ext in EXT_VIDEO_AUDIO and tem_mi:
                        meta = mediainfo(Path(raiz) / r.get("caminho", ""))
                    elif ext in EXT_IMAGEM and tem_et:
                        meta = mapa_exif.get(_norm(r.get("caminho", "")))
                except Exception:
                    meta = None
                if meta is not None:
                    r["mediainfo"] = meta
                    feitos_mi += 1
                    mudou = True

            # 2) Formato/PUID (Siegfried) onde faltou — sem tocar no SHA-256.
            if mapa_puid and _sem_formato(r):
                puid, formato = mapa_puid.get(_norm(r.get("caminho", "")), (None, None))
                if puid or formato:
                    r["puid"], r["formato"] = puid, formato
                    feitos_sf += 1
                    mudou = True

            fout.write(json.dumps(r, ensure_ascii=False) + "\n")
            w.writerow({k: r.get(k) for k in campos})
            if mudou:
                lote.append(r)
                if len(lote) >= LOTE_DB:
                    db.enviar_lote(lote); lote = []
            if lidos % 200 == 0:
                _emit({"event": "progress", "fase": "completando", "lidos": lidos,
                       "novos": feitos_mi + feitos_sf, "total": total,
                       "arquivo": r.get("caminho")}, mingap=1.0)
    db.enviar_lote(lote)
    os.replace(tmp, manifesto_json)
    os.replace(tmp_csv, manifesto_csv)
    # Restaura o status do disco (registrar_disco o marcou 'varrendo' no inicio):
    # o backfill so enriquece metadados, nao deixa o disco "varrendo" para sempre.
    db.concluir_disco(disco_label)
    _emit({"event": "done", "lidos": lidos, "novos": feitos_mi + feitos_sf,
           "total": total, "online": db.conectado()}, forcar=True)
    print(f"\nOK — backfill '{disco_label}': {lidos} verificados, "
          f"{feitos_mi} com metadados de mídia + {feitos_sf} com formato (Siegfried), "
          f"sem re-hash.", flush=True)


def main():
    p = argparse.ArgumentParser(description="Varredura somente-leitura de um disco do acervo.")
    p.add_argument("--disco", required=True, help="Etiqueta do HD (ex.: Antigos-C)")
    p.add_argument("--raiz", required=True, type=Path, help="Ponto de montagem do disco")
    p.add_argument("--grupo", default=None, help="antigos | uso_continuo | transporte")
    p.add_argument("--force", action="store_true", help="Re-varre tudo, ignorando o ja lido")
    p.add_argument("--dry-run", action="store_true",
                   help="So conta arquivos/bytes e estima o tempo; nao calcula hash nem grava nada")
    p.add_argument("--backfill", action="store_true",
                   help="So completa metadados (MediaInfo/ExifTool) e formato (Siegfried) que faltaram, sem re-hash")
    p.add_argument("--progress-json", action="store_true",
                   help="Emite eventos de progresso (linhas '@@PS@@ {json}') para o painel")
    a = p.parse_args()
    global EMIT
    EMIT = a.progress_json
    if not a.raiz.exists():
        _emit({"event": "erro", "msg": f"Raiz nao encontrada: {a.raiz}"}, forcar=True)
        sys.exit(f"Raiz nao encontrada: {a.raiz}")
    if a.dry_run:
        print(f"[DRY-RUN] Inspecionando '{a.disco}' em {a.raiz} (nada sera calculado ou gravado)\n")
        inspecionar(a.raiz)
        return
    if a.backfill:
        backfill(a.disco, a.raiz)
        return
    varrer(a.disco, a.raiz, a.grupo, a.force)


if __name__ == "__main__":
    main()
