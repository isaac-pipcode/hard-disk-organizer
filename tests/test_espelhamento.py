#!/usr/bin/env python3
"""Teste do planejador de espelhamento (scanner/espelhamento.py).

Verifica: dedup por conteúdo antes de dimensionar, bin-packing (FFD), duplicação
do espelho (total = 2× primários), detecção de arquivo grande demais e a escolha
da capacidade recomendada.
"""
import csv
import json
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import espelhamento as esp  # noqa: E402

TB = esp.TB


def _teste_ffd():
    # 3 itens de 4 TB em bins de 5 TB → 3 bins (nenhum par cabe junto).
    itens = [("a", 4 * TB), ("b", 4 * TB), ("c", 4 * TB)]
    _atr, nbins, _restos, over = esp._ffd(itens, 5 * TB)
    assert nbins == 3, nbins
    assert over == []
    # 4 itens de 2 TB + 1 de 5 TB em bins de 6 TB → FFD: [5], [2+2+2? não cabe 3º]
    # 5 no bin1(rem1), 2,2 no bin2(rem2), 2 no bin3 → 3 bins
    itens = [("x", 5 * TB), ("a", 2 * TB), ("b", 2 * TB), ("c", 2 * TB), ("d", 2 * TB)]
    _atr, nbins, _restos, over = esp._ffd(itens, 6 * TB)
    assert nbins == 3, nbins
    # oversize: item de 9 TB num disco de 8 TB
    _atr, nbins, _restos, over = esp._ffd([("big", 9 * TB), ("s", 1 * TB)], 8 * TB)
    assert over == ["big"], over


def _rodar(tmp: Path, dados, capacidade=None, capacidades=None):
    man, sai = tmp / "manifestos", tmp / "esp"
    man.mkdir(parents=True, exist_ok=True)
    with open(man / "manifesto_HD-1.jsonl", "w", encoding="utf-8") as f:
        for r in dados:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    resumo = esp.planejar(man, sai, capacidade_tb=capacidade,
                          capacidades=capacidades or [4, 8, 16])
    plano = list(csv.DictReader(open(sai / "plano_espelhamento.csv", encoding="utf-8")))
    return resumo, plano


def verificar():
    checks, falhas = [], []

    def check(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp:
            falhas.append(nome)

    _teste_ffd()
    checks.append(("FFD (bin-packing) básico", "ok", "ok", True))

    # Dados: 3 conteúdos únicos de 3 TB cada = 9 TB únicos. Um deles duplicado
    # (mesmo sha) para provar que o dedup conta só UMA vez.
    dados = [
        {"disco_label": "HD-1", "caminho": "a.mov", "nome": "a.mov", "sha256": "a"*64, "tamanho_bytes": 3*TB},
        {"disco_label": "HD-1", "caminho": "b.mov", "nome": "b.mov", "sha256": "b"*64, "tamanho_bytes": 3*TB},
        {"disco_label": "HD-1", "caminho": "c.mov", "nome": "c.mov", "sha256": "c"*64, "tamanho_bytes": 3*TB},
        {"disco_label": "HD-1", "caminho": "copia_de_a.mov", "nome": "copia_de_a.mov", "sha256": "a"*64, "tamanho_bytes": 3*TB},
    ]
    with tempfile.TemporaryDirectory() as td:
        # capacidade 8 TB, utilizável 1.0 para contas exatas
        resumo, plano = _rodar(Path(td), dados, capacidade=8, capacidades=[8])
        # forçar utilizável=1.0 via nova chamada determinística:
        man = Path(td) / "manifestos"
        resumo = esp.planejar(man, Path(td) / "esp2", capacidade_tb=8,
                              capacidades=[8], utilizavel=1.0)
        plano = list(csv.DictReader(open(Path(td) / "esp2" / "plano_espelhamento.csv", encoding="utf-8")))

    # 3 conteúdos únicos (não 4), volume 9 TB.
    check("conteúdo único conta dedup", resumo["conteudo_unico"]["arquivos"], 3)
    check("volume único (bytes)", resumo["conteudo_unico"]["bytes"], 9 * TB)
    # 9 TB únicos em discos de 8 TB (util 1.0): 2 primários (8+? 3+3 num, 3 noutro).
    check("discos primários", resumo["recomendacao"]["discos_primarios"], 2)
    check("discos totais = 2x", resumo["recomendacao"]["discos_total"], 4)
    # plano lista os 3 conteúdos únicos, não os 4 arquivos
    check("linhas do plano = conteúdos únicos", len(plano), 3)
    # cada linha tem um primário Pxx e o espelho correspondente Mxx
    par_ok = all(l["disco_espelho"] == "M" + l["disco_primario"][1:] for l in plano)
    check("cada primário tem espelho correspondente", par_ok, True)

    # Recomendação: menor capacidade total comprada entre 4/8/16 TB para 9 TB únicos.
    #  4TB: ceil packing de 3+3+3 em 4 -> 3 primários -> 6 discos -> 24 TB
    #  8TB: 2 primários -> 4 discos -> 32 TB
    #  16TB: 1 primário -> 2 discos -> 32 TB
    # menor capacidade total = 24 TB (discos de 4) — recomendado.
    with tempfile.TemporaryDirectory() as td:
        resumo2 = esp.planejar(_escrever(td, dados), Path(td) / "esp3",
                               capacidades=[4, 8, 16], utilizavel=1.0)
    check("recomenda capacidade de menor custo total (4 TB)",
          resumo2["recomendacao"]["capacidade_tb"], 4)

    return checks, falhas


def _escrever(td, dados):
    man = Path(td) / "manifestos"
    man.mkdir(parents=True, exist_ok=True)
    with open(man / "manifesto_HD-1.jsonl", "w", encoding="utf-8") as f:
        for r in dados:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return man


def test_planejador_espelhamento():
    _, falhas = verificar()
    assert not falhas, f"Espelhamento incorreto: {falhas}"


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
