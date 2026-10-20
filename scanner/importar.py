#!/usr/bin/env python3
"""
PRESERVA-SCAN — importação de manifestos varridos em OUTRA máquina.

As etapas de análise (relatório, deduplicação, espelhamento, mapa entre discos,
execução) leem apenas a pasta `manifestos/` ao lado do programa. Quando os discos
foram varridos em computadores diferentes (ou por cópias diferentes do .exe), os
manifestos precisam ser TRAZIDOS para cá — é o que este módulo faz.

Aceita:
  - manifesto_<disco>.jsonl  (preferido: tem os metadados de mídia completos);
  - manifesto_<disco>.csv    (só quando não há o .jsonl: é convertido, sem mediainfo);
  - .zip contendo esses arquivos (em qualquer subpasta do zip);
  - uma PASTA (ex.: pendrive) — procura os manifestos dentro dela, recursivamente.

Garantias:
  - a etiqueta do disco vem do CONTEÚDO (coluna disco_label), não do nome do arquivo;
  - linhas corrompidas (ex.: última linha cortada por queda de energia) são
    descartadas e CONTADAS no resultado — nunca em silêncio;
  - registros repetidos do mesmo caminho (varredura retomada que re-hasheou um
    arquivo alterado) ficam só com o mais recente, como no upsert do banco;
  - disco que JÁ existe aqui com conteúdo diferente NÃO é sobrescrito, a menos que
    se peça; ao substituir, o manifesto antigo vai para `manifestos/_substituidos/`;
  - grava JSONL e CSV a partir do mesmo conteúdo (sempre em sincronia), de forma
    atômica (arquivo temporário + rename).

Uso:
    python importar.py --origem E:\\manifestos --manifestos ./manifestos
    python importar.py --origem pacote.zip --manifestos ./manifestos --substituir
"""
import io
import os
import csv
import json
import time
import shutil
import hashlib
import zipfile
import argparse
from pathlib import Path

CAMPOS_CSV = ["disco_label", "caminho", "nome", "extensao", "tamanho_bytes",
              "mtime", "sha256", "puid", "formato"]
# Caracteres que não podem ir no nome de arquivo (Windows) — a etiqueta vira parte
# de manifesto_<etiqueta>.jsonl; se tiver um desses, a importação é recusada.
_PROIBIDOS = set('<>:"/\\|?*')


def _eh_manifesto(nome):
    base = os.path.basename(nome.replace("\\", "/"))
    return base.startswith("manifesto_") and base.lower().endswith((".jsonl", ".csv"))


def _fontes_de_pasta(pasta: Path):
    """[(nome, obter)] dos manifestos dentro de uma pasta (recursivo) e de zips nela.
    `obter()` lê os bytes só na hora de importar aquele disco: com dezenas de
    manifestos de centenas de MB, nunca se carrega tudo na memória de uma vez."""
    fontes = []
    for p in sorted(pasta.rglob("*")):
        if not p.is_file() or "_substituidos" in p.parts:
            continue
        if p.suffix.lower() == ".zip":
            fontes.extend(_fontes_de_zip(p))
        elif _eh_manifesto(p.name):
            fontes.append((p.name, p.read_bytes))
    return fontes


def _fontes_de_zip(caminho_zip: Path):
    fontes = []
    try:
        with zipfile.ZipFile(caminho_zip) as z:
            membros = [i.filename for i in z.infolist()
                       if not i.is_dir() and _eh_manifesto(i.filename)]
    except zipfile.BadZipFile:
        raise ValueError(f"{caminho_zip.name}: arquivo .zip inválido ou corrompido")
    for m in membros:
        def obter(m=m):
            with zipfile.ZipFile(caminho_zip) as z:
                return z.read(m)
        # Só o nome-base: nunca usa o caminho interno do zip para gravar.
        fontes.append((os.path.basename(m.replace("\\", "/")), obter))
    return fontes


def _ler_jsonl(dados):
    linhas, invalidas = [], 0
    for linha in dados.decode("utf-8", errors="replace").splitlines():
        if not linha.strip():
            continue
        try:
            r = json.loads(linha)
        except Exception:
            invalidas += 1
            continue
        if isinstance(r, dict) and r.get("caminho"):
            linhas.append(r)
        else:
            invalidas += 1
    return linhas, invalidas


def _ler_csv(dados):
    linhas, invalidas = [], 0
    texto = dados.decode("utf-8-sig", errors="replace")
    for r in csv.DictReader(io.StringIO(texto)):
        if not r.get("caminho"):
            invalidas += 1
            continue
        r = {k: (v if v != "" else None) for k, v in r.items() if k in CAMPOS_CSV}
        try:
            r["tamanho_bytes"] = int(r["tamanho_bytes"]) if r.get("tamanho_bytes") else None
        except ValueError:
            invalidas += 1
            continue
        linhas.append(r)
    return linhas, invalidas


def _validar_etiqueta(label):
    if not label or label.strip() != label or label in (".", ".."):
        return False
    return not (set(label) & _PROIBIDOS) and all(ord(c) >= 32 for c in label)


def _serializar(linhas):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in linhas)


def _digest_conteudo(texto):
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _digest_existente(caminho: Path):
    """Digest do manifesto que já está aqui, normalizado do mesmo jeito que o
    importado (para 'mesmo conteúdo' não depender de espaços/ordem de chaves)."""
    linhas, _ = _ler_jsonl(caminho.read_bytes())
    return _digest_conteudo(_serializar(_consolidar(linhas)[0]))


def _consolidar(linhas):
    """Um registro por caminho, o ÚLTIMO vence (é o mais recente da varredura)."""
    por_caminho = {}
    for r in linhas:
        por_caminho.pop(r["caminho"], None)      # reinsere no fim: preserva a ordem
        por_caminho[r["caminho"]] = r
    return list(por_caminho.values()), len(linhas) - len(por_caminho)


def _gravar(manifest_dir: Path, label, linhas):
    jsonl = manifest_dir / f"manifesto_{label}.jsonl"
    csv_ = manifest_dir / f"manifesto_{label}.csv"
    tmp_j = jsonl.with_name(jsonl.name + ".tmp")
    tmp_c = csv_.with_name(csv_.name + ".tmp")
    tmp_j.write_text(_serializar(linhas), encoding="utf-8")
    with open(tmp_c, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_CSV)
        w.writeheader()
        for r in linhas:
            w.writerow({k: r.get(k) for k in CAMPOS_CSV})
    os.replace(tmp_j, jsonl)
    os.replace(tmp_c, csv_)


def _guardar_antigo(manifest_dir: Path, label):
    destino = manifest_dir / "_substituidos" / time.strftime("%Y%m%d_%H%M%S")
    destino.mkdir(parents=True, exist_ok=True)
    for ext in (".jsonl", ".csv"):
        p = manifest_dir / f"manifesto_{label}{ext}"
        if p.exists():
            shutil.move(str(p), str(destino / p.name))
    return destino


def importar(fontes, manifest_dir, substituir=False):
    """Importa os manifestos `fontes` ([(nome, obter)], já sem zips) para
    `manifest_dir`. Devolve {"discos": [...], "erros": [...]}; cada disco traz
    situação (importado|substituido|identico|conflito) e os números de integridade."""
    manifest_dir = Path(manifest_dir)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    # Agrupa por nome-base: se vierem .jsonl e .csv do mesmo disco, o .jsonl manda
    # (tem os metadados de mídia; o .csv é só uma projeção dele).
    por_base = {}
    for nome, obter in fontes:
        base, ext = os.path.splitext(nome)
        por_base.setdefault(base, {})[ext.lower()] = (nome, obter)

    discos, erros, vistos = [], [], {}
    for base in sorted(por_base):
        par = por_base[base]
        nome, obter = par.get(".jsonl") or par[".csv"]
        origem_csv = ".jsonl" not in par
        try:
            linhas, invalidas = (_ler_csv if origem_csv else _ler_jsonl)(obter())
        except Exception as e:
            erros.append(f"{nome}: não foi possível ler ({type(e).__name__}: {e})")
            continue
        if not linhas:
            erros.append(f"{nome}: nenhum registro válido encontrado")
            continue

        labels = {r.get("disco_label") for r in linhas}
        if len(labels) != 1 or None in labels:
            erros.append(f"{nome}: o arquivo mistura etiquetas de disco "
                         f"({', '.join(sorted(str(l) for l in labels))}) — não importado")
            continue
        label = labels.pop()
        if not _validar_etiqueta(label):
            erros.append(f"{nome}: etiqueta de disco inválida para nome de arquivo: {label!r}")
            continue
        if label in vistos:
            erros.append(f"{nome}: o disco '{label}' já veio em {vistos[label]} nesta "
                         f"mesma importação — só o primeiro foi considerado")
            continue
        vistos[label] = nome

        linhas, repetidos = _consolidar(linhas)
        info = {
            "disco": label, "arquivo": nome,
            "origem": "csv (sem metadados de mídia)" if origem_csv else "jsonl",
            "registros": len(linhas),
            "bytes": sum(int(r.get("tamanho_bytes") or 0) for r in linhas),
            "sem_hash": sum(1 for r in linhas if not r.get("sha256")),
            "sem_formato": sum(1 for r in linhas if not (r.get("puid") or r.get("formato"))),
            "linhas_invalidas": invalidas, "repetidos": repetidos,
        }

        atual = manifest_dir / f"manifesto_{label}.jsonl"
        if atual.exists():
            if _digest_existente(atual) == _digest_conteudo(_serializar(linhas)):
                info["situacao"] = "identico"
                discos.append(info)
                continue
            if not substituir:
                info["situacao"] = "conflito"
                discos.append(info)
                continue
            info["backup"] = str(_guardar_antigo(manifest_dir, label))
            info["situacao"] = "substituido"
        else:
            info["situacao"] = "importado"
        _gravar(manifest_dir, label, linhas)
        discos.append(info)
    return {"discos": discos, "erros": erros}


def importar_de(origem, manifest_dir, substituir=False):
    """Importa de uma pasta, de um .zip ou de um manifesto avulso no disco."""
    origem = Path(origem)
    if origem.is_dir():
        fontes = _fontes_de_pasta(origem)
    elif origem.suffix.lower() == ".zip":
        fontes = _fontes_de_zip(origem)
    elif origem.is_file() and _eh_manifesto(origem.name):
        fontes = [(origem.name, origem.read_bytes)]
    elif not origem.exists():
        raise FileNotFoundError(f"Origem não encontrada: {origem}")
    else:
        fontes = []
    if not fontes:
        raise FileNotFoundError(f"Nenhum manifesto (manifesto_*.jsonl/.csv) em: {origem}")
    return importar(fontes, manifest_dir, substituir=substituir)


def main():
    ap = argparse.ArgumentParser(description="Importa manifestos varridos em outra máquina")
    ap.add_argument("--origem", required=True, help="pasta, .zip ou manifesto avulso")
    ap.add_argument("--manifestos", default="./manifestos")
    ap.add_argument("--substituir", action="store_true",
                    help="substitui disco já existente com conteúdo diferente (guarda backup)")
    a = ap.parse_args()
    res = importar_de(a.origem, a.manifestos, substituir=a.substituir)
    for d in res["discos"]:
        print(f"  [{d['situacao']}] {d['disco']}: {d['registros']} registros"
              + (f", {d['sem_hash']} sem hash" if d["sem_hash"] else "")
              + (f", {d['linhas_invalidas']} linha(s) inválida(s)" if d["linhas_invalidas"] else ""))
    for e in res["erros"]:
        print(f"  ERRO: {e}")
    if any(d["situacao"] == "conflito" for d in res["discos"]):
        print("Há discos em conflito (já existem aqui com conteúdo diferente). "
              "Use --substituir para trocá-los (o antigo vai para _substituidos/).")


if __name__ == "__main__":
    main()
