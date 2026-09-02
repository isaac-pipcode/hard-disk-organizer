#!/usr/bin/env python3
"""
PRESERVA-SCAN — visão consolidada entre discos (fase 2, item 3).

Cruza TODOS os manifestos e responde, por conteúdo (SHA-256), "o que está repetido
em quais discos". Serve para decidir a consolidação:

  - por disco: quanto do conteúdo é EXCLUSIVO dele (só existe ali) e quanto já está
    também em OUTRO disco → um disco cujo conteúdo exclusivo é ZERO pode ser
    esvaziado com segurança (tudo nele existe em outro lugar);
  - sobreposição entre PARES de discos: quanto conteúdo dois discos compartilham
    (quem é redundante com quem).

NÃO apaga nem move nada: gera planilhas (CSV) + um resumo (JSON).

Uso:
    python consolidado.py --manifestos ./manifestos --saida ./consolidado
"""
import os
import csv
import json
import argparse
from itertools import combinations
from pathlib import Path

try:
    from relatorio import _manifestos, _linhas, humano
except Exception:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from relatorio import _manifestos, _linhas, humano


def analisar(manifest_dir):
    """{sha: [tamanho, {discos}]} e agregados por disco e por par de discos."""
    por_sha = {}
    for mf in _manifestos(manifest_dir):
        for r in _linhas(mf):
            sha = r.get("sha256")
            if not sha:
                continue
            disco = r.get("disco_label") or "?"
            tam = int(r.get("tamanho_bytes") or 0)
            e = por_sha.get(sha)
            if e is None:
                por_sha[sha] = [tam, {disco}]
            else:
                e[1].add(disco)

    por_disco = {}       # disco -> [conteudos, bytes, bytes_so_aqui, bytes_tambem_em_outro]
    pares = {}           # (a,b) -> [conteudos, bytes] compartilhados
    bytes_1disco = bytes_2mais = 0
    for _sha, (tam, discos) in por_sha.items():
        exclusivo = len(discos) == 1
        if exclusivo:
            bytes_1disco += tam
        else:
            bytes_2mais += tam
        for d in discos:
            pd = por_disco.setdefault(d, [0, 0, 0, 0])
            pd[0] += 1
            pd[1] += tam
            if exclusivo:
                pd[2] += tam
            else:
                pd[3] += tam
        if not exclusivo:
            for a, b in combinations(sorted(discos), 2):
                p = pares.setdefault((a, b), [0, 0])
                p[0] += 1
                p[1] += tam
    return {"por_sha": por_sha, "por_disco": por_disco, "pares": pares,
            "bytes_1disco": bytes_1disco, "bytes_2mais": bytes_2mais}


def gerar(manifest_dir, saida_dir):
    manifest_dir, saida = Path(manifest_dir), Path(saida_dir)
    saida.mkdir(parents=True, exist_ok=True)
    a = analisar(manifest_dir)
    por_disco, pares = a["por_disco"], a["pares"]

    # Por disco: exclusivo × redundante, e "pode esvaziar?".
    resumo_csv = saida / "discos_resumo.csv"
    with open(resumo_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["disco", "conteudos", "bytes", "bytes_exclusivos",
                    "bytes_tambem_em_outro", "pode_esvaziar"])
        for d, (n, bytes_tot, so_aqui, tambem) in sorted(por_disco.items()):
            w.writerow([d, n, bytes_tot, so_aqui, tambem,
                        "sim" if so_aqui == 0 else "não"])

    # Sobreposição entre pares de discos.
    sob_csv = saida / "discos_sobreposicao.csv"
    with open(sob_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["disco_a", "disco_b", "conteudos_compartilhados", "bytes_compartilhados"])
        for (x, y), (n, by) in sorted(pares.items(), key=lambda kv: -kv[1][1]):
            w.writerow([x, y, n, by])

    esvaziaveis = [d for d, v in por_disco.items() if v[2] == 0]
    resumo = {
        "discos": len(por_disco),
        "conteudos_unicos": len(a["por_sha"]),
        "bytes_em_1_disco": a["bytes_1disco"],
        "bytes_em_1_disco_humano": humano(a["bytes_1disco"]),
        "bytes_em_2mais_discos": a["bytes_2mais"],
        "bytes_em_2mais_discos_humano": humano(a["bytes_2mais"]),
        "discos_esvaziaveis": sorted(esvaziaveis),
        "por_disco": {d: {"conteudos": v[0], "bytes": v[1], "bytes_exclusivos": v[2],
                          "bytes_tambem_em_outro": v[3],
                          "pode_esvaziar": v[2] == 0} for d, v in sorted(por_disco.items())},
        "pares_top": [
            {"disco_a": x, "disco_b": y, "conteudos": n, "bytes": by,
             "bytes_humano": humano(by)}
            for (x, y), (n, by) in sorted(pares.items(), key=lambda kv: -kv[1][1])[:20]
        ],
        "observacao": ("Um disco 'pode_esvaziar' quando NENHUM conteúdo é exclusivo dele "
                       "(tudo já existe em outro disco). Com só 1 disco varrido, tudo é "
                       "exclusivo — esta visão ganha sentido com vários discos juntos."),
    }
    (saida / "consolidado_resumo.json").write_text(
        json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Mapa entre discos gerado em: {saida}")
    print(f"  Discos: {len(por_disco)}  |  conteúdos únicos: {len(a['por_sha'])}")
    print(f"  Em 1 disco só: {humano(a['bytes_1disco'])}  |  em 2+ discos: {humano(a['bytes_2mais'])}")
    if esvaziaveis:
        print(f"  Podem ser esvaziados (tudo existe noutro disco): {', '.join(sorted(esvaziaveis))}")
    return resumo


def main():
    ap = argparse.ArgumentParser(description="Visão consolidada entre discos do PRESERVA-SCAN")
    ap.add_argument("--manifestos", default="./manifestos")
    ap.add_argument("--saida", default="./consolidado")
    a = ap.parse_args()
    gerar(a.manifestos, a.saida)


if __name__ == "__main__":
    main()
