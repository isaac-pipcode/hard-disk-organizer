#!/usr/bin/env python3
"""Teste da visão consolidada entre discos (scanner/consolidado.py)."""
import csv
import json
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import consolidado as cons  # noqa: E402

# Cenário:
#  A (100) em HD-1 e HD-2  -> compartilhado
#  B (200) em HD-1 e HD-2  -> compartilhado
#  C (50)  só em HD-1      -> exclusivo do HD-1
#  D (300) em HD-2 e HD-3  -> compartilhado
# HD-2 não tem nada exclusivo (A,B também no HD-1; D também no HD-3) -> pode esvaziar.
# HD-1 tem C exclusivo -> não pode. HD-3 tem D só com HD-2, mas D também está no HD-2,
#   então D não é exclusivo do HD-3 -> HD-3 pode esvaziar (só tem D).
A, B, C, D = "a"*64, "b"*64, "c"*64, "d"*64
DADOS = {
    "HD-1": [(A, 100), (B, 200), (C, 50)],
    "HD-2": [(A, 100), (B, 200), (D, 300)],
    "HD-3": [(D, 300)],
}


def _rodar(tmp: Path):
    man, sai = tmp / "manifestos", tmp / "cons"
    man.mkdir(parents=True, exist_ok=True)
    for disco, itens in DADOS.items():
        with open(man / f"manifesto_{disco}.jsonl", "w", encoding="utf-8") as f:
            for sha, tam in itens:
                f.write(json.dumps({"disco_label": disco, "caminho": sha[:6],
                                    "sha256": sha, "tamanho_bytes": tam}) + "\n")
    resumo = cons.gerar(man, sai)
    disc = {r["disco"]: r for r in csv.DictReader(open(sai / "discos_resumo.csv", encoding="utf-8"))}
    pares = list(csv.DictReader(open(sai / "discos_sobreposicao.csv", encoding="utf-8")))
    return resumo, disc, pares


def verificar():
    with tempfile.TemporaryDirectory() as td:
        resumo, disc, pares = _rodar(Path(td))
    checks, falhas = [], []

    def check(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp:
            falhas.append(nome)

    check("nº de discos", resumo["discos"], 3)
    check("conteúdos únicos", resumo["conteudos_unicos"], 4)   # A,B,C,D
    # Em 1 disco só: apenas C (50). Em 2+: A(100)+B(200)+D(300)=600.
    check("bytes em 1 disco", resumo["bytes_em_1_disco"], 50)
    check("bytes em 2+ discos", resumo["bytes_em_2mais_discos"], 600)

    # HD-1: exclusivo = C (50) -> não pode esvaziar.
    check("HD-1 exclusivos", int(disc["HD-1"]["bytes_exclusivos"]), 50)
    check("HD-1 pode esvaziar", disc["HD-1"]["pode_esvaziar"], "não")
    # HD-2: nada exclusivo -> pode esvaziar.
    check("HD-2 exclusivos", int(disc["HD-2"]["bytes_exclusivos"]), 0)
    check("HD-2 pode esvaziar", disc["HD-2"]["pode_esvaziar"], "sim")
    # HD-3: só D, que também está no HD-2 -> exclusivo 0 -> pode esvaziar.
    check("HD-3 pode esvaziar", disc["HD-3"]["pode_esvaziar"], "sim")
    check("discos esvaziáveis", set(resumo["discos_esvaziaveis"]), {"HD-2", "HD-3"})

    # Sobreposição: par (HD-1,HD-2) compartilha A+B = 300; (HD-2,HD-3) compartilha D = 300.
    pmap = {(p["disco_a"], p["disco_b"]): int(p["bytes_compartilhados"]) for p in pares}
    check("overlap HD-1/HD-2", pmap.get(("HD-1", "HD-2")), 300)
    check("overlap HD-2/HD-3", pmap.get(("HD-2", "HD-3")), 300)
    check("sem overlap HD-1/HD-3", ("HD-1", "HD-3") in pmap, False)

    return checks, falhas


def test_consolidado_entre_discos():
    _, falhas = verificar()
    assert not falhas, f"Consolidado incorreto: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    if falhas:
        print(f"RESULTADO: FALHOU — {falhas}")
        return 1
    print(f"RESULTADO: OK — todas as {len(checks)} verificações passaram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
