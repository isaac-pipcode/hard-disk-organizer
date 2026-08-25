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


def siegfried_lote(raiz: Path):
    """Roda o Siegfried UMA vez sobre a arvore inteira (rapido) e devolve
    {caminho_relativo: (puid, formato)}. Substitui a chamada por-arquivo, que
    nao escala: 125 mil arquivos = 125 mil processos. Em lote = 1 processo."""
    sf = ferramenta("sf")
    if not sf:
        print("AVISO: 'sf' (Siegfried) nao encontrado; seguindo sem identificacao de formato.")
        return {}
    mapa = {}
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(tmp, "w", encoding="utf-8") as out:
            subprocess.run([sf, "-json", str(raiz)], stdout=out,
                           stderr=subprocess.DEVNULL, check=False)
        with open(tmp, encoding="utf-8") as f:
            data = json.load(f)
        os.unlink(tmp)
        for entry in data.get("files", []):
            fn = entry.get("filename")
            if not fn:
                continue
            try:
                rel = _norm(Path(fn).relative_to(raiz))
            except Exception:
                rel = _norm(fn)
            matches = entry.get("matches") or []
            if matches:
                mapa[rel] = (matches[0].get("id"), matches[0].get("format"))
    except Exception as e:
        print(f"AVISO: Siegfried em lote falhou ({e}); seguindo sem PUID.")
    return mapa


def mediainfo(caminho: Path):
    """Metadados tecnicos AV (codec, resolucao, duracao...) via MediaInfo."""
    exe = ferramenta("mediainfo")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--Output=JSON", str(caminho)],
                           capture_output=True, text=True, timeout=180)
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
        r = subprocess.run([exe, "-json", str(caminho)],
                           capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout)
        return data[0] if data else None
    except Exception:
        return None


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
    mapa = {}
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           errors="replace")   # sem timeout: é uma passada única
        data = json.loads(r.stdout) if r.stdout.strip() else []
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
    print("Identificando formatos (Siegfried) e metadados de imagem (ExifTool), "
          "uma passada na arvore...", flush=True)
    mapa_puid = siegfried_lote(raiz)
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

            for dirpath, dirs, files in os.walk(raiz):
                dirs[:] = [d for d in dirs if not _pular_dir(d)]
                for nome in files:
                    if _pular_arquivo(nome):
                        continue
                    caminho_abs = Path(dirpath) / nome
                    if caminho_abs.is_symlink() or not caminho_abs.is_file():
                        continue
                    rel = str(caminho_abs.relative_to(raiz))
                    try:
                        st = caminho_abs.stat()
                    except OSError:
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
                        erros += 1
                        msg = f"{type(e).__name__}: {e}"
                        print(f"  AVISO: falha ao ler '{rel}' ({msg}); pulando.", flush=True)
                        _emit({"event": "aviso_arquivo", "arquivo": rel, "erro": msg},
                              forcar=True)
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


def backfill(disco_label, raiz: Path):
    """Completa SÓ os metadados de A/V e imagem (MediaInfo/ExifTool) dos arquivos
    que ficaram sem eles — SEM re-hashear. Usa o manifesto já existente: para cada
    registro com `mediainfo` vazio e extensão de mídia, localiza o arquivo no disco,
    roda a ferramenta e atualiza o registro (manifesto local + banco). O SHA-256 e a
    identificação de formato não são recalculados."""
    manifesto_json = MANIFESTOS / f"manifesto_{disco_label}.jsonl"
    if not manifesto_json.exists():
        msg = f"Manifesto não encontrado: {manifesto_json}. Faça a varredura antes."
        _emit({"event": "erro", "msg": msg}, forcar=True)
        sys.exit(msg)
    tem_mi = ferramenta("mediainfo") is not None
    tem_et = ferramenta("exiftool") is not None
    if not (tem_mi or tem_et):
        msg = "Nenhuma ferramenta de metadados disponível (instale/forneça MediaInfo e ExifTool)."
        _emit({"event": "erro", "msg": msg}, forcar=True)
        sys.exit(msg)

    db.registrar_disco(disco_label)
    _emit({"event": "phase", "fase": "identificando"}, forcar=True)
    # Imagens: uma passada do ExifTool em LOTE (rápido); AV: MediaInfo por arquivo.
    mapa_exif = exiftool_lote(raiz) if tem_et else {}
    _emit({"event": "phase", "fase": "completando"}, forcar=True)
    total = sum(1 for _ in open(manifesto_json, encoding="utf-8", errors="replace"))
    _emit({"event": "total", "total": total}, forcar=True)
    print(f"Completando metadados de '{disco_label}' ({total} registros)...", flush=True)

    tmp = manifesto_json.with_name(manifesto_json.name + ".tmp")
    lidos = feitos = 0
    lote = []
    with open(manifesto_json, encoding="utf-8", errors="replace") as fin, \
         open(tmp, "w", encoding="utf-8", buffering=1) as fout:
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
            vazio = r.get("mediainfo") in (None, {}, "")
            if vazio and (ext in EXT_VIDEO_AUDIO or ext in EXT_IMAGEM):
                alvo = Path(raiz) / r.get("caminho", "")
                meta = None
                try:
                    if ext in EXT_VIDEO_AUDIO and tem_mi:
                        meta = mediainfo(alvo)
                    elif ext in EXT_IMAGEM and tem_et:
                        meta = mapa_exif.get(_norm(r.get("caminho", "")))
                except Exception:
                    meta = None
                if meta is not None:
                    r["mediainfo"] = meta
                    feitos += 1
                    lote.append(r)
                    if len(lote) >= LOTE_DB:
                        db.enviar_lote(lote); lote = []
            fout.write(json.dumps(r, ensure_ascii=False) + "\n")
            if lidos % 200 == 0:
                _emit({"event": "progress", "fase": "completando", "lidos": lidos,
                       "novos": feitos, "total": total, "arquivo": r.get("caminho")},
                      mingap=1.0)
    db.enviar_lote(lote)
    os.replace(tmp, manifesto_json)
    _emit({"event": "done", "lidos": lidos, "novos": feitos, "total": total,
           "online": db.conectado()}, forcar=True)
    print(f"\nOK — backfill '{disco_label}': {lidos} verificados, "
          f"{feitos} completados com metadados de mídia (sem re-hash).", flush=True)


def main():
    p = argparse.ArgumentParser(description="Varredura somente-leitura de um disco do acervo.")
    p.add_argument("--disco", required=True, help="Etiqueta do HD (ex.: Antigos-C)")
    p.add_argument("--raiz", required=True, type=Path, help="Ponto de montagem do disco")
    p.add_argument("--grupo", default=None, help="antigos | uso_continuo | transporte")
    p.add_argument("--force", action="store_true", help="Re-varre tudo, ignorando o ja lido")
    p.add_argument("--dry-run", action="store_true",
                   help="So conta arquivos/bytes e estima o tempo; nao calcula hash nem grava nada")
    p.add_argument("--backfill", action="store_true",
                   help="So completa metadados de midia (MediaInfo/ExifTool) que faltaram, sem re-hash")
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
