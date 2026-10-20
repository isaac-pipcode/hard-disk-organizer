#!/usr/bin/env python3
"""Teste da importação de manifestos varridos em outra máquina (scanner/importar.py):
pasta, zip, CSV sem JSONL, linha corrompida, registro repetido, conflito e
substituição com backup — e que o resultado alimenta as etapas de análise."""
import csv
import json
import sys
import zipfile
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import importar  # noqa: E402
import consolidado  # noqa: E402

A, B, C = "a"*64, "b"*64, "c"*64


def _reg(disco, caminho, sha, tam):
    return {"disco_label": disco, "caminho": caminho, "nome": caminho, "extensao": "mp4",
            "tamanho_bytes": tam, "mtime": "t", "sha256": sha, "puid": "fmt/199",
            "formato": "MPEG-4", "mediainfo": {"media": {"track": []}}}


def _jsonl(p: Path, regs, extra=""):
    p.write_text("".join(json.dumps(r) + "\n" for r in regs) + extra, encoding="utf-8")


def verificar():
    checks, falhas = [], []

    def check(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp:
            falhas.append(nome)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        origem, destino = td / "pendrive", td / "manifestos"
        (origem / "maquina2").mkdir(parents=True)
        # HD-1: JSONL com última linha cortada (queda de energia) e um caminho
        # repetido (retomada re-hasheou um arquivo alterado: o último vale).
        _jsonl(origem / "manifesto_HD-1.jsonl",
               [_reg("HD-1", "x.mp4", A, 100), _reg("HD-1", "y.mp4", C, 1),
                _reg("HD-1", "y.mp4", B, 200)], extra='{"disco_label": "HD-1", "cam')
        # CSV do HD-1 também presente: deve ser ignorado (o JSONL manda).
        (origem / "manifesto_HD-1.csv").write_text("lixo\n", encoding="utf-8")
        # HD-2: só CSV, numa subpasta, nome de arquivo diferente da etiqueta.
        with open(origem / "maquina2" / "manifesto_copia.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=importar.CAMPOS_CSV)
            w.writeheader()
            w.writerow({"disco_label": "HD-2", "caminho": "z.mp4", "nome": "z.mp4",
                        "extensao": "mp4", "tamanho_bytes": "100", "mtime": "t",
                        "sha256": A, "puid": "", "formato": ""})
        # HD-3: dentro de um zip, em subpasta interna.
        tmpj = td / "manifesto_HD-3.jsonl"
        _jsonl(tmpj, [_reg("HD-3", "w.mp4", B, 200)])
        with zipfile.ZipFile(origem / "lote.zip", "w") as z:
            z.write(tmpj, "algum/lugar/manifesto_HD-3.jsonl")
        # Arquivo com etiquetas misturadas: recusado.
        _jsonl(origem / "manifesto_misto.jsonl", [_reg("HD-8", "a", A, 1), _reg("HD-9", "b", B, 1)])
        # Etiqueta com barra (viraria caminho): recusada.
        _jsonl(origem / "manifesto_ruim.jsonl", [_reg("HD/../x", "a", A, 1)])

        res = importar.importar_de(origem, destino)
        por = {d["disco"]: d for d in res["discos"]}
        check("discos importados", sorted(por), ["HD-1", "HD-2", "HD-3"])
        check("todos 'importado'", {d["situacao"] for d in por.values()}, {"importado"})
        check("HD-1 linha corrompida contada", por["HD-1"]["linhas_invalidas"], 1)
        check("HD-1 repetido consolidado", por["HD-1"]["repetidos"], 1)
        check("HD-1 registros", por["HD-1"]["registros"], 2)
        check("HD-1 veio do jsonl", por["HD-1"]["origem"], "jsonl")
        check("HD-2 veio do csv", por["HD-2"]["origem"].startswith("csv"), True)
        check("HD-2 sem formato contado", por["HD-2"]["sem_formato"], 1)
        check("HD-2 gravado pela etiqueta", (destino / "manifesto_HD-2.jsonl").exists(), True)
        check("2 erros (misto + etiqueta inválida)", len(res["erros"]), 2)
        check("nada fora da pasta", sorted(p.name for p in td.iterdir()),
              ["manifesto_HD-3.jsonl", "manifestos", "pendrive"])

        hd1 = [json.loads(l) for l in open(destino / "manifesto_HD-1.jsonl", encoding="utf-8")]
        check("HD-1 último registro de y vence", [r["sha256"] for r in hd1 if r["caminho"] == "y.mp4"], [B])
        check("HD-1 mediainfo preservado", hd1[0].get("mediainfo"), {"media": {"track": []}})
        linhas_csv = list(csv.DictReader(open(destino / "manifesto_HD-1.csv", encoding="utf-8")))
        check("CSV em sincronia com JSONL", len(linhas_csv), 2)
        hd2 = json.loads(open(destino / "manifesto_HD-2.jsonl", encoding="utf-8").readline())
        check("CSV convertido: tamanho inteiro", hd2["tamanho_bytes"], 100)

        # Alimenta a análise: A está em HD-1 e HD-2; B em HD-1 e HD-3.
        cons = consolidado.gerar(destino, td / "cons")
        check("consolidado enxerga 3 discos", cons["discos"], 3)
        check("HD-1 pode esvaziar", cons["por_disco"]["HD-1"]["pode_esvaziar"], True)

        # Reimportar o mesmo: idêntico, nada muda.
        res2 = importar.importar_de(origem, destino)
        check("reimportação = idêntico", {d["situacao"] for d in res2["discos"]}, {"identico"})

        # Mesmo disco, conteúdo diferente: conflito sem substituir; com substituir, backup.
        outra = td / "outra"
        outra.mkdir()
        _jsonl(outra / "manifesto_HD-3.jsonl", [_reg("HD-3", "novo.mp4", C, 5)])
        res3 = importar.importar_de(outra, destino)
        check("conflito detectado", res3["discos"][0]["situacao"], "conflito")
        check("conflito não altera", json.loads(open(destino / "manifesto_HD-3.jsonl").readline())["caminho"], "w.mp4")
        res4 = importar.importar_de(outra, destino, substituir=True)
        check("substituído", res4["discos"][0]["situacao"], "substituido")
        check("novo conteúdo gravado", json.loads(open(destino / "manifesto_HD-3.jsonl").readline())["caminho"], "novo.mp4")
        bkp = Path(res4["discos"][0]["backup"])
        check("backup guardado", sorted(p.name for p in bkp.iterdir()),
              ["manifesto_HD-3.csv", "manifesto_HD-3.jsonl"])
        check("backup fora do glob da análise",
              sorted(p.name for p in destino.glob("manifesto_*.jsonl")),
              ["manifesto_HD-1.jsonl", "manifesto_HD-2.jsonl", "manifesto_HD-3.jsonl"])

    for nome, got, exp, ok in checks:
        print(f"  [{'OK' if ok else 'FALHOU'}] {nome}: {got!r}" + ("" if ok else f" (esperado {exp!r})"))
    return falhas


if __name__ == "__main__":
    falhas = verificar()
    print("\nTUDO OK" if not falhas else f"\n{len(falhas)} FALHA(S): {falhas}")
    sys.exit(1 if falhas else 0)
