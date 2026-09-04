#!/usr/bin/env python3
"""Testa a marcação de arquivos DESCARTÁVEIS (cache regenerável / lixo) no relatório."""
import csv
import json
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import relatorio as rel  # noqa: E402


DADOS = [
    # descartáveis
    {"disco_label": "HD1", "caminho": "cache/a.cfa", "nome": "a.cfa", "extensao": "cfa", "sha256": "1"*64, "tamanho_bytes": 1000},
    {"disco_label": "HD1", "caminho": "cache/a.pek", "nome": "a.pek", "extensao": "pek", "sha256": "2"*64, "tamanho_bytes": 500},
    {"disco_label": "HD1", "caminho": "x/Thumbs.db", "nome": "Thumbs.db", "extensao": "db", "sha256": "3"*64, "tamanho_bytes": 20},
    {"disco_label": "HD1", "caminho": "y/desktop.ini", "nome": "desktop.ini", "extensao": "ini", "sha256": "4"*64, "tamanho_bytes": 10},
    # NÃO descartáveis (acervo / projeto) — não podem ser marcados
    {"disco_label": "HD1", "caminho": "edit/projeto.prproj", "nome": "projeto.prproj", "extensao": "prproj", "sha256": "5"*64, "tamanho_bytes": 3000},
    {"disco_label": "HD1", "caminho": "video.mp4", "nome": "video.mp4", "extensao": "mp4", "sha256": "6"*64, "tamanho_bytes": 9000},
]


def verificar():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    # marcação unitária
    ck(".cfa é descartável", rel.descartavel({"extensao": "cfa", "nome": "a.cfa"}), True)
    ck(".pek é descartável", rel.descartavel({"extensao": "pek", "nome": "a.pek"}), True)
    ck("Thumbs.db é descartável", rel.descartavel({"extensao": "db", "nome": "Thumbs.db"}), True)
    ck(".prproj NÃO é descartável", rel.descartavel({"extensao": "prproj", "nome": "p.prproj"}), False)
    ck(".mp4 NÃO é descartável", rel.descartavel({"extensao": "mp4", "nome": "v.mp4"}), False)

    with tempfile.TemporaryDirectory() as td:
        man, sai = Path(td) / "manifestos", Path(td) / "rel"
        man.mkdir(parents=True); sai.mkdir(parents=True)
        with open(man / "manifesto_HD1.jsonl", "w", encoding="utf-8") as f:
            for r in DADOS:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        rel.gerar(man, sai)
        resumo = json.loads((sai / "resumo.json").read_text(encoding="utf-8"))
        desc = list(csv.DictReader(open(sai / "descartaveis.csv", encoding="utf-8")))

    # 4 descartáveis, 1530 bytes
    ck("contagem descartáveis", resumo["descartaveis"]["arquivos"], 4)
    ck("bytes descartáveis", resumo["descartaveis"]["bytes"], 1000 + 500 + 20 + 10)
    ck("descartaveis.csv tem 4 linhas", len(desc), 4)
    caminhos = {d["caminho"] for d in desc}
    ck("projeto NÃO entra em descartáveis", "edit/projeto.prproj" in caminhos, False)
    ck("vídeo NÃO entra em descartáveis", "video.mp4" in caminhos, False)
    return checks, falhas


def test_descartaveis():
    _, falhas = verificar()
    assert not falhas, f"Descartáveis incorreto: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    print(f"RESULTADO: {'OK — todas passaram' if not falhas else 'FALHOU: '+str(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
