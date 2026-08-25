#!/usr/bin/env python3
"""
PRESERVA-SCAN — gerador de relatório local (sem banco, sem custo).

Lê os manifestos gerados pela varredura (manifesto_<disco>.jsonl) e produz, numa
pasta de saída:
  - inventario_consolidado.csv  — todos os discos num arquivo só
  - duplicatas.csv              — arquivos idênticos (mesmo SHA-256), com em quantos
                                  discos aparecem (responde "está em 1 ou 2+ discos")
  - resumo.json                 — números do acervo (para outras ferramentas)
  - dashboard.html              — painel visual estático (abre no navegador, offline)

Tudo é derivado do manifesto local: é o próprio acervo em formato aberto/convertível,
que a instituição guarda para sempre — não depende de nuvem nem de banco.

Uso:
    python relatorio.py --manifestos ./manifestos --saida ./relatorio
"""
import os
import csv
import json
import glob
import html
import argparse
import datetime
from pathlib import Path
from collections import Counter

CAMPOS = ["disco_label", "caminho", "nome", "extensao", "tamanho_bytes",
          "mtime", "sha256", "puid", "formato"]


def humano(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024:
            return (f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}")
        n /= 1024
    return f"{n:.1f} EB"


def _num(n):
    return f"{int(n):,}".replace(",", ".")


def _manifestos(manifest_dir: Path):
    return sorted(Path(manifest_dir).glob("manifesto_*.jsonl"))


def _linhas(caminho: Path):
    with open(caminho, encoding="utf-8", errors="replace") as f:
        for linha in f:
            linha = linha.strip()
            if not linha:
                continue
            try:
                yield json.loads(linha)
            except Exception:
                continue


def analisar(manifest_dir: Path):
    """Passada 1 (streaming): totais, volume por disco, formatos e, por SHA-256,
    quantas cópias e em quantos discos. Memória ~ nº de arquivos ÚNICOS."""
    arquivos = bytes_tot = 0
    por_disco = {}                    # disco -> [arquivos, bytes]
    formatos = Counter()              # rótulo de formato -> contagem
    # sha -> [copias, tamanho, {discos...}]  (guarda um set pequeno de discos)
    porhash = {}
    for mf in _manifestos(manifest_dir):
        for r in _linhas(mf):
            disco = r.get("disco_label") or mf.stem.replace("manifesto_", "")
            tam = int(r.get("tamanho_bytes") or 0)
            arquivos += 1
            bytes_tot += tam
            d = por_disco.setdefault(disco, [0, 0])
            d[0] += 1; d[1] += tam
            rotulo = r.get("formato") or (("." + r["extensao"]) if r.get("extensao") else "sem extensão")
            formatos[rotulo] += 1
            sha = r.get("sha256")
            if sha:
                h = porhash.get(sha)
                if h is None:
                    porhash[sha] = [1, tam, {disco}]
                else:
                    h[0] += 1
                    h[2].add(disco)
    return {
        "arquivos": arquivos, "bytes": bytes_tot,
        "por_disco": por_disco, "formatos": formatos, "porhash": porhash,
    }


def escrever_csvs(manifest_dir: Path, saida: Path, a):
    """Passada 2 (streaming): grava o inventário consolidado e o CSV de duplicatas."""
    porhash = a["porhash"]
    inv = saida / "inventario_consolidado.csv"
    dup = saida / "duplicatas.csv"
    with open(inv, "w", newline="", encoding="utf-8") as fi, \
         open(dup, "w", newline="", encoding="utf-8") as fd:
        wi = csv.DictWriter(fi, fieldnames=CAMPOS); wi.writeheader()
        wd = csv.writer(fd)
        wd.writerow(["sha256", "copias", "em_n_discos", "tamanho_bytes",
                     "disco_label", "caminho"])
        for mf in _manifestos(manifest_dir):
            for r in _linhas(mf):
                wi.writerow({k: r.get(k) for k in CAMPOS})
                sha = r.get("sha256")
                info = porhash.get(sha) if sha else None
                if info and info[0] > 1:      # só arquivos que têm cópia idêntica
                    wd.writerow([sha, info[0], len(info[2]),
                                 r.get("tamanho_bytes"),
                                 r.get("disco_label"), r.get("caminho")])
    return inv, dup


def resumir(a):
    porhash = a["porhash"]
    unicos = len(porhash)
    grupos_dup = [(s, c, tam, len(discos)) for s, (c, tam, discos) in porhash.items() if c > 1]
    arquivos_duplicados = sum(c for _, c, _, _ in grupos_dup)          # cópias que são redundância
    redundantes = sum((c - 1) for _, c, _, _ in grupos_dup)           # arquivos "a mais"
    bytes_redundantes = sum(tam * (c - 1) for _, c, tam, _ in grupos_dup)
    em_2mais_discos = sum(1 for _, _, _, nd in grupos_dup if nd >= 2)
    top_dups = sorted(grupos_dup, key=lambda g: g[2] * (g[1] - 1), reverse=True)[:20]
    return {
        "gerado_em": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "arquivos": a["arquivos"], "bytes": a["bytes"],
        "arquivos_unicos_por_conteudo": unicos,
        "discos": len(a["por_disco"]),
        "grupos_duplicados": len(grupos_dup),
        "arquivos_redundantes": redundantes,
        "bytes_redundantes": bytes_redundantes,
        "grupos_em_2mais_discos": em_2mais_discos,
        "por_disco": {k: {"arquivos": v[0], "bytes": v[1]} for k, v in a["por_disco"].items()},
        "top_formatos": a["formatos"].most_common(15),
        "top_duplicatas": [{"sha256": s, "copias": c, "tamanho_bytes": tam, "discos": nd}
                            for s, c, tam, nd in top_dups],
    }


# ----------------------------- dashboard HTML -----------------------------

def _barras(pares, cor_var="--series-1", unidade="bytes"):
    """pares: lista de (rótulo, valor). Devolve HTML de barras horizontais."""
    if not pares:
        return '<p class="vazio">Sem dados.</p>'
    maxv = max(v for _, v in pares) or 1
    linhas = []
    for rot, val in pares:
        pct = max(1.5, val / maxv * 100)
        valtxt = humano(val) if unidade == "bytes" else _num(val)
        titulo = f"{html.escape(str(rot))}: {valtxt}"
        linhas.append(
            f'<div class="linha" title="{titulo}">'
            f'<div class="rot" title="{html.escape(str(rot))}">{html.escape(str(rot))}</div>'
            f'<div class="trilho"><div class="fill" style="width:{pct:.1f}%;background:var({cor_var})"></div></div>'
            f'<div class="val">{valtxt}</div></div>'
        )
    return "\n".join(linhas)


def escrever_dashboard(saida: Path, s):
    por_disco = sorted(s["por_disco"].items(), key=lambda kv: kv[1]["bytes"], reverse=True)
    barras_disco = _barras([(k, v["bytes"]) for k, v in por_disco], unidade="bytes")
    barras_fmt = _barras([(f, c) for f, c in s["top_formatos"]], unidade="num")
    pct_red = (100 * s["arquivos_redundantes"] / s["arquivos"]) if s["arquivos"] else 0

    linhas_dup = []
    for d in s["top_duplicatas"]:
        linhas_dup.append(
            f'<tr><td class="mono">{d["sha256"][:12]}…</td>'
            f'<td class="tnum">{d["copias"]}</td>'
            f'<td class="tnum">{d["discos"]}</td>'
            f'<td class="tnum">{humano(d["tamanho_bytes"])}</td>'
            f'<td class="tnum">{humano(d["tamanho_bytes"]*(d["copias"]-1))}</td></tr>'
        )
    tabela_dup = "\n".join(linhas_dup) or '<tr><td colspan="5" class="muted">Nenhuma duplicata encontrada.</td></tr>'

    linhas_disco_tab = "\n".join(
        f'<tr><td>{html.escape(k)}</td><td class="tnum">{_num(v["arquivos"])}</td>'
        f'<td class="tnum">{humano(v["bytes"])}</td></tr>'
        for k, v in por_disco
    )

    gerado = s["gerado_em"].replace("T", " ")[:19]
    tiles = [
        ("Arquivos", _num(s["arquivos"])),
        ("Volume total", humano(s["bytes"])),
        ("Discos", _num(s["discos"])),
        ("Arquivos únicos", _num(s["arquivos_unicos_por_conteudo"])),
        ("Redundantes", f'{_num(s["arquivos_redundantes"])} · {pct_red:.1f}%'),
        ("Espaço redundante", humano(s["bytes_redundantes"])),
    ]
    tiles_html = "\n".join(
        f'<div class="tile"><div class="tn">{v}</div><div class="tl">{html.escape(l)}</div></div>'
        for l, v in tiles
    )

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PRESERVA-SCAN — Relatório do acervo</title>
<style>
  /* Tokens no :root para que TODO o documento (inclusive body) herde as cores.
     (bug anterior: variáveis ficavam num filho .viz-root e o body não as via —
      texto ficava sem cor, ilegível no modo escuro do sistema.) */
  :root{{ color-scheme:light;
    --plane:#f4f4f2; --surface:#ffffff; --ink:#141414; --ink2:#4a4a48; --muted:#6b6b68;
    --grid:#e3e2dc; --baseline:#c3c2b7; --border:rgba(0,0,0,.12);
    --series-1:#2a6fd6; --track:#e9edf2; }}
  @media (prefers-color-scheme:dark){{ :root{{
    color-scheme:dark; --plane:#101012; --surface:#1c1c1f; --ink:#f2f2f2; --ink2:#cbcac4;
    --muted:#9a9a94; --grid:#333336; --baseline:#3a3a3d; --border:rgba(255,255,255,.16);
    --series-1:#5aa0f2; --track:#2a2a2e; }} }}
  *{{ box-sizing:border-box; }}
  html,body{{ margin:0; background:var(--plane); color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif; line-height:1.5; }}
  h1,h2{{ color:var(--ink); }}
  .tn{{ color:var(--ink); }}
  .viz-root{{ max-width:900px; margin:0 auto; padding:2rem 1.1rem 3rem; }}
  h1{{ font-size:1.5rem; margin:0 0 .15rem; }}
  .sub{{ color:var(--ink2); font-size:.92rem; margin:0 0 1.4rem; }}
  .tiles{{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:.7rem; margin-bottom:1.8rem; }}
  .tile{{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:.9rem 1rem; }}
  .tn{{ font-size:1.5rem; font-weight:700; letter-spacing:-.01em; }}
  .tl{{ color:var(--muted); font-size:.76rem; text-transform:uppercase; letter-spacing:.04em; margin-top:.15rem; }}
  section{{ background:var(--surface); border:1px solid var(--border); border-radius:12px;
    padding:1.1rem 1.2rem; margin-bottom:1.2rem; }}
  h2{{ font-size:1.02rem; margin:0 0 .9rem; }}
  .linha{{ display:grid; grid-template-columns:minmax(90px,190px) 1fr auto; align-items:center;
    gap:.6rem; margin-bottom:6px; }}
  .rot{{ font-size:.82rem; color:var(--ink2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .trilho{{ background:var(--track); border-radius:5px; height:16px; overflow:hidden; }}
  .fill{{ height:100%; border-radius:0 4px 4px 0; }}
  .val{{ font-size:.8rem; color:var(--ink2); font-variant-numeric:tabular-nums; white-space:nowrap; }}
  table{{ width:100%; border-collapse:collapse; font-size:.86rem; }}
  th,td{{ text-align:left; padding:.4rem .55rem; border-bottom:1px solid var(--grid); }}
  th{{ color:var(--muted); font-weight:600; font-size:.72rem; text-transform:uppercase; letter-spacing:.03em; }}
  .tnum{{ text-align:right; font-variant-numeric:tabular-nums; }}
  .mono{{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.8rem; }}
  .muted{{ color:var(--muted); }}
  .frase{{ color:var(--ink2); font-size:.92rem; margin:0 0 1rem; }}
  .frase b{{ color:var(--ink); }}
  .rodape{{ color:var(--muted); font-size:.8rem; margin-top:1.4rem; }}
  .arqs{{ font-size:.82rem; color:var(--ink2); }} .arqs code{{ font-size:.82rem; }}
</style></head>
<body><div class="viz-root">
  <h1>Relatório do acervo</h1>
  <p class="sub">PRESERVA-SCAN · inventário consolidado dos manifestos · gerado em {gerado}</p>

  <div class="tiles">{tiles_html}</div>

  <section>
    <h2>Volume por disco</h2>
    {barras_disco}
  </section>

  <section>
    <h2>Formatos mais frequentes <span class="muted" style="font-weight:400">(por nº de arquivos)</span></h2>
    {barras_fmt}
  </section>

  <section>
    <h2>Duplicatas (mesmo conteúdo / SHA-256)</h2>
    <p class="frase"><b>{_num(s["arquivos_redundantes"])}</b> arquivo(s) são cópias redundantes
      ({pct_red:.1f}% do acervo), ocupando <b>{humano(s["bytes_redundantes"])}</b> que poderiam ser
      liberados. <b>{_num(s["grupos_em_2mais_discos"])}</b> conjunto(s) de arquivos idênticos aparecem
      em <b>2 ou mais discos</b>.</p>
    <table>
      <tr><th>SHA-256</th><th class="tnum">Cópias</th><th class="tnum">Em nº discos</th>
          <th class="tnum">Tamanho</th><th class="tnum">Espaço redundante</th></tr>
      {tabela_dup}
    </table>
    <p class="muted" style="font-size:.78rem;margin-top:.6rem">Top 20 por espaço redundante. Lista completa em <code>duplicatas.csv</code>.</p>
  </section>

  <section>
    <h2>Discos</h2>
    <table>
      <tr><th>Disco</th><th class="tnum">Arquivos</th><th class="tnum">Volume</th></tr>
      {linhas_disco_tab}
    </table>
  </section>

  <p class="arqs">Arquivos gerados nesta pasta: <code>inventario_consolidado.csv</code> ·
    <code>duplicatas.csv</code> · <code>resumo.json</code> · <code>dashboard.html</code>.</p>
  <p class="rodape">Derivado dos manifestos locais (somente leitura). Arquivos com falha de leitura
    não entram no inventário — ver o log de cada varredura. Este relatório é um arquivo:
    guarde-o e abra quando quiser, sem internet.</p>
</div></body></html>
"""


def gerar(manifest_dir, saida_dir):
    """Gera todos os artefatos. Devolve o caminho do dashboard.html."""
    manifest_dir = Path(manifest_dir)
    saida = Path(saida_dir)
    saida.mkdir(parents=True, exist_ok=True)
    if not _manifestos(manifest_dir):
        raise FileNotFoundError(
            f"Nenhum manifesto (manifesto_*.jsonl) encontrado em {manifest_dir}.")
    a = analisar(manifest_dir)
    escrever_csvs(manifest_dir, saida, a)
    s = resumir(a)
    (saida / "resumo.json").write_text(
        json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    dash = saida / "dashboard.html"
    dash.write_text(escrever_dashboard(saida, s), encoding="utf-8")
    return dash


def main():
    p = argparse.ArgumentParser(description="Gera o relatório local do acervo a partir dos manifestos.")
    p.add_argument("--manifestos", default=os.environ.get("MANIFEST_DIR", "./manifestos"),
                   help="pasta com os manifesto_*.jsonl")
    p.add_argument("--saida", default="./relatorio", help="pasta de saída do relatório")
    args = p.parse_args()
    dash = gerar(args.manifestos, args.saida)
    print(f"Relatório gerado: {dash}")
    print(f"Abra no navegador: {dash.resolve().as_uri()}")


if __name__ == "__main__":
    main()
