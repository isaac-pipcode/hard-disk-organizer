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


def _norm(p):
    """Normaliza separadores de caminho (Windows/Unix) para casar os mapas."""
    return str(p).replace("\\", "/")


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
    if not shutil.which("sf"):
        print("AVISO: 'sf' (Siegfried) nao encontrado; seguindo sem identificacao de formato.")
        return {}
    mapa = {}
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(tmp, "w", encoding="utf-8") as out:
            subprocess.run(["sf", "-json", str(raiz)], stdout=out,
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
    if not shutil.which("mediainfo"):
        return None
    try:
        r = subprocess.run(["mediainfo", "--Output=JSON", str(caminho)],
                           capture_output=True, text=True, timeout=180)
        return json.loads(r.stdout) if r.stdout.strip() else None
    except Exception:
        return None


def exiftool(caminho: Path):
    """Metadados embutidos (imagens/documentos) via ExifTool — complementa o MediaInfo."""
    if not shutil.which("exiftool"):
        return None
    try:
        r = subprocess.run(["exiftool", "-json", str(caminho)],
                           capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout)
        return data[0] if data else None
    except Exception:
        return None


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
    for dirpath, _, files in os.walk(raiz):
        for nome in files:
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


def varrer(disco_label, raiz: Path, grupo=None, force=False):
    run_id = str(uuid.uuid4())
    MANIFESTOS.mkdir(parents=True, exist_ok=True)
    manifesto_csv = MANIFESTOS / f"manifesto_{disco_label}_{datetime.date.today()}.csv"
    manifesto_json = MANIFESTOS / f"manifesto_{disco_label}_{datetime.date.today()}.jsonl"

    db.registrar_disco(disco_label, grupo=grupo)
    db.abrir_run(run_id, disco_label)
    ja = {} if force else db.ja_varridos(disco_label)

    # Fase 0 (so com painel): conta arquivos/bytes antes, para a barra ter um
    # denominador. Uma passada leve (stat, sem hash); emite batimentos para o
    # painel nao achar que travou. No modo CLI puro isso e pulado.
    total_arq, total_bytes = 0, 0
    if EMIT or CALLBACK is not None:
        _emit({"event": "phase", "fase": "contando"}, forcar=True)
        for dirpath, _, files in os.walk(raiz):
            for nome in files:
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
    print("Identificando formatos (Siegfried, uma passada na arvore)...", flush=True)
    mapa_puid = siegfried_lote(raiz)

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
        with open(manifesto_csv, "w", newline="", encoding="utf-8", buffering=1) as fcsv, \
             open(manifesto_json, "w", encoding="utf-8", buffering=1) as fjson:
            w = csv.DictWriter(fcsv, fieldnames=campos)
            w.writeheader()

            for dirpath, _, files in os.walk(raiz):
                for nome in files:
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
                        # MediaInfo so em AV; ExifTool so em imagem; resto: so hash + formato
                        if ext in EXT_VIDEO_AUDIO:
                            meta = mediainfo(caminho_abs)
                        elif ext in EXT_IMAGEM:
                            meta = exiftool(caminho_abs)
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


def main():
    p = argparse.ArgumentParser(description="Varredura somente-leitura de um disco do acervo.")
    p.add_argument("--disco", required=True, help="Etiqueta do HD (ex.: Antigos-C)")
    p.add_argument("--raiz", required=True, type=Path, help="Ponto de montagem do disco")
    p.add_argument("--grupo", default=None, help="antigos | uso_continuo | transporte")
    p.add_argument("--force", action="store_true", help="Re-varre tudo, ignorando o ja lido")
    p.add_argument("--dry-run", action="store_true",
                   help="So conta arquivos/bytes e estima o tempo; nao calcula hash nem grava nada")
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
    varrer(a.disco, a.raiz, a.grupo, a.force)


if __name__ == "__main__":
    main()
