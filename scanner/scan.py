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


def sha256_de(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(CHUNK), b""):
            h.update(bloco)
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

    print("Identificando formatos (Siegfried, uma passada na arvore)...", flush=True)
    mapa_puid = siegfried_lote(raiz)

    lote, novos, lidos = [], 0, 0
    campos = ["disco_label", "caminho", "nome", "extensao", "tamanho_bytes",
              "mtime", "sha256", "puid", "formato"]

    with open(manifesto_csv, "w", newline="", encoding="utf-8") as fcsv, \
         open(manifesto_json, "w", encoding="utf-8") as fjson:
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
                        continue

                if lidos % 500 == 0:
                    print(f"  {lidos} lidos / {novos} novos...", flush=True)
                sha = sha256_de(caminho_abs)
                ext = caminho_abs.suffix.lower().lstrip(".")
                puid, formato = mapa_puid.get(_norm(rel), (None, None))
                # MediaInfo so em AV; ExifTool so em imagem; resto: so hash + formato
                if ext in EXT_VIDEO_AUDIO:
                    meta = mediainfo(caminho_abs)
                elif ext in EXT_IMAGEM:
                    meta = exiftool(caminho_abs)
                else:
                    meta = None

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
                if len(lote) >= LOTE_DB:
                    db.enviar_lote(lote); lote = []

    db.enviar_lote(lote)
    db.fechar_run(run_id, novos, lidos)
    db.concluir_disco(disco_label)
    print(f"\nOK — disco '{disco_label}': {lidos} lidos, {novos} novos/alterados.")
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
    a = p.parse_args()
    if not a.raiz.exists():
        sys.exit(f"Raiz nao encontrada: {a.raiz}")
    if a.dry_run:
        print(f"[DRY-RUN] Inspecionando '{a.disco}' em {a.raiz} (nada sera calculado ou gravado)\n")
        inspecionar(a.raiz)
        return
    varrer(a.disco, a.raiz, a.grupo, a.force)


if __name__ == "__main__":
    main()
