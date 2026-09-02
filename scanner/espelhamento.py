#!/usr/bin/env python3
"""
PRESERVA-SCAN — planejador de espelhamento (fase 2, item 1).

Responde à pergunta prática: "quantos discos comprar, e de que tamanho, para
guardar todo o acervo com 2 cópias em 2 discos, gastando o mínimo?"

Como funciona:
  1. Lê os manifestos e reduz ao CONTEÚDO ÚNICO (dedup por SHA-256): é o volume que
     precisa ser preservado UMA vez. Espelhar o torna duas.
  2. Para cada tamanho de disco candidato, distribui os conteúdos em discos
     "primários" usando bin-packing First-Fit-Decreasing (encaixa os maiores
     primeiro, desperdício mínimo), aplicando um fator de espaço UTILIZÁVEL
     (formatação + folga de segurança).
  3. Cada disco primário ganha um disco ESPELHO idêntico → total = 2× os primários.
     Assim nenhum arquivo fica num disco só.

NÃO move nem copia nada: produz um plano (JSON + CSV) para orçar e executar.

Uso:
    python espelhamento.py --manifestos ./manifestos --saida ./espelhamento
    python espelhamento.py --manifestos ./manifestos --saida ./espelhamento --capacidade 8
"""
import os
import csv
import json
import argparse
from pathlib import Path

try:
    from relatorio import _manifestos, _linhas, humano
    from dedup import _chave_manter
except Exception:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from relatorio import _manifestos, _linhas, humano
    from dedup import _chave_manter

TB = 1_000_000_000_000                    # 1 TB comercial (decimal), como os fabricantes anunciam
CAPACIDADES_PADRAO = [4, 6, 8, 12, 14, 16, 18, 20, 22]   # tamanhos comuns de HD (TB)
UTILIZAVEL = 0.93                         # fração aproveitável após formatação + folga


def _conteudos_unicos(manifest_dir):
    """{sha: (tamanho_bytes, melhor_caminho, melhor_disco)} — uma entrada por conteúdo.
    Guarda o caminho mais organizado como referência humana no plano."""
    unicos = {}
    for mf in _manifestos(manifest_dir):
        for r in _linhas(mf):
            sha = r.get("sha256")
            if not sha:
                continue
            tam = int(r.get("tamanho_bytes") or 0)
            cur = unicos.get(sha)
            cand = {"disco_label": r.get("disco_label"), "caminho": r.get("caminho"),
                    "nome": r.get("nome")}
            if cur is None:
                unicos[sha] = [tam, cand]
            else:
                # mantém como referência o caminho mais "organizado"
                if _chave_manter(cand) < _chave_manter(cur[1]):
                    cur[1] = cand
    return unicos


def _ffd(itens, capacidade):
    """First-Fit-Decreasing. `itens`: lista de (chave, tamanho). Devolve
    (atribuicao {chave: bin}, n_bins, restos, oversize). Oversize = itens maiores
    que um disco inteiro (não cabem — precisam de disco maior)."""
    oversize = [k for k, s in itens if s > capacidade]
    packable = [(k, s) for k, s in itens if s <= capacidade]
    packable.sort(key=lambda t: t[1], reverse=True)
    restos = []            # capacidade restante de cada bin
    atrib = {}
    for k, s in packable:
        colocado = False
        for b in range(len(restos)):
            if restos[b] >= s:
                restos[b] -= s
                atrib[k] = b
                colocado = True
                break
        if not colocado:
            restos.append(capacidade - s)
            atrib[k] = len(restos) - 1
    return atrib, len(restos), restos, oversize


def _avaliar(volume, itens, capacidades, utilizavel):
    """Para cada capacidade candidata, quantos discos (primário + espelho) e a
    eficiência de uso. Não gera atribuição detalhada — só o panorama para orçar."""
    linhas = []
    for tb in capacidades:
        cap = int(tb * TB * utilizavel)
        _atr, nbins, restos, oversize = _ffd(itens, cap)
        primarios = nbins
        total = primarios * 2
        capacidade_comprada = total * tb * TB
        uso = volume / (primarios * cap) if primarios else 0
        linhas.append({
            "capacidade_tb": tb,
            "utilizavel_por_disco": humano(cap),
            "discos_primarios": primarios,
            "discos_espelho": primarios,
            "discos_total": total,
            "capacidade_total_comprada": humano(capacidade_comprada),
            "capacidade_total_tb": round(total * tb, 1),
            "uso_medio": round(uso, 3),
            "arquivos_grandes_demais": len(oversize),
        })
    return linhas


def planejar(manifest_dir, saida_dir, capacidade_tb=None,
             capacidades=None, utilizavel=UTILIZAVEL):
    manifest_dir, saida = Path(manifest_dir), Path(saida_dir)
    saida.mkdir(parents=True, exist_ok=True)
    capacidades = capacidades or CAPACIDADES_PADRAO

    unicos = _conteudos_unicos(manifest_dir)
    itens = [(sha, tam) for sha, (tam, _ref) in unicos.items()]
    volume = sum(s for _k, s in itens)

    tabela = _avaliar(volume, itens, capacidades, utilizavel)

    # Recomendação: menor capacidade TOTAL comprada; desempate por menos discos.
    validos = [t for t in tabela if t["arquivos_grandes_demais"] == 0]
    recomendado = min(validos or tabela,
                      key=lambda t: (t["capacidade_total_tb"], t["discos_total"]))
    rec_tb = capacidade_tb or recomendado["capacidade_tb"]

    # Atribuição detalhada para a capacidade escolhida.
    cap = int(rec_tb * TB * utilizavel)
    atrib, nbins, restos, oversize = _ffd(itens, cap)
    plano_csv = saida / "plano_espelhamento.csv"
    with open(plano_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["disco_primario", "disco_espelho", "sha256", "tamanho_bytes",
                    "referencia_caminho", "disco_origem"])
        # ordena por bin e por tamanho desc, para a planilha ficar legível
        for sha, b in sorted(atrib.items(), key=lambda kv: (kv[1], -unicos[kv[0]][0])):
            tam, ref = unicos[sha]
            w.writerow([f"P{b+1:02d}", f"M{b+1:02d}", sha, tam,
                        ref.get("caminho"), ref.get("disco_label")])

    resumo = {
        "politica": {"copias": 2, "discos_por_copia": 2, "utilizavel": utilizavel},
        "conteudo_unico": {
            "arquivos": len(itens),
            "bytes": volume,
            "volume_humano": humano(volume),
        },
        "volume_espelhado_humano": humano(volume * 2),
        "recomendacao": {
            "capacidade_tb": rec_tb,
            "discos_primarios": nbins,
            "discos_espelho": nbins,
            "discos_total": nbins * 2,
            "capacidade_total_tb": round(nbins * 2 * rec_tb, 1),
            "uso_medio": round(volume / (nbins * cap), 3) if nbins else 0,
            "arquivos_grandes_demais": len(oversize),
        },
        "opcoes_por_capacidade": tabela,
        "observacao": ("Comprar disco espelho novo para cada primário garante 2 cópias "
                       "em 2 discos. Para economizar, discos existentes saudáveis podem "
                       "servir de espelho depois de consolidados e conferidos por hash."),
    }
    (saida / "plano_espelhamento_resumo.json").write_text(
        json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Plano de espelhamento gerado em: {saida}")
    print(f"  Conteúdo único: {len(itens)} arquivos, {humano(volume)} "
          f"(espelhado: {humano(volume*2)})")
    print(f"  Recomendado: discos de {rec_tb} TB -> {nbins} primários + {nbins} espelho "
          f"= {nbins*2} discos ({nbins*2*rec_tb} TB no total, uso {resumo['recomendacao']['uso_medio']:.0%})")
    if oversize:
        print(f"  ATENÇÃO: {len(oversize)} arquivo(s) não cabem num disco de {rec_tb} TB.")
    return resumo


def main():
    ap = argparse.ArgumentParser(description="Planejador de espelhamento do PRESERVA-SCAN")
    ap.add_argument("--manifestos", default="./manifestos")
    ap.add_argument("--saida", default="./espelhamento")
    ap.add_argument("--capacidade", type=float, default=None,
                    help="TB por disco para o plano detalhado (padrão: recomendado)")
    ap.add_argument("--utilizavel", type=float, default=UTILIZAVEL,
                    help="fração aproveitável do disco (padrão 0.93)")
    a = ap.parse_args()
    planejar(a.manifestos, a.saida, capacidade_tb=a.capacidade, utilizavel=a.utilizavel)


if __name__ == "__main__":
    main()
