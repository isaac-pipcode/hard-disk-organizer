#!/usr/bin/env python3
"""Testa a tela "Conteúdo do HD": navegar por pastas e arquivos a partir do
inventário — inclusive enquanto a varredura ainda está gravando o manifesto
(lê só as linhas novas, ignora a linha pela metade) e depois de o manifesto ser
reescrito por "Completar dados"."""
import json
import os
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import conteudo  # noqa: E402


def _linha(caminho, tam=10, sha=True, mi=None):
    nome = caminho.replace("\\", "/").rsplit("/", 1)[-1]
    return json.dumps({"disco_label": "HD X", "caminho": caminho, "nome": nome,
                       "extensao": nome.rsplit(".", 1)[-1].lower() if "." in nome else "",
                       "tamanho_bytes": tam, "mtime": "2025-03-04T10:00:00",
                       "sha256": "a" * 64 if sha else None, "puid": "fmt/1", "formato": "Fmt",
                       "mediainfo": mi}, ensure_ascii=False) + "\n"


MI = {"media": {"track": [{"@type": "General", "Duration": "3725.2"},
                          {"@type": "Video", "Width": "1920", "Height": "1080", "Format": "ProRes"}]}}


def verificar():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        man = p / "manifesto_HD X.jsonl"
        ck("sem inventário: None", conteudo.listar(p, "HD X"), None)

        # A varredura começou: duas linhas completas e uma pela metade
        with open(man, "w", encoding="utf-8") as f:
            f.write(_linha("Projetos\\Filme A\\bruto.mov", 3000, mi=MI))
            f.write(_linha("leia-me.txt", 5))
            f.write(_linha("Projetos/Filme A/still.jpg", 20)[:30])      # sendo escrita
        r = conteudo.listar(p, "HD X")
        ck("raiz: só linhas completas entram", r["total_hd"], 2)
        ck("raiz: subpasta + arquivo direto", ([s["nome"] for s in r["subpastas"]], [a["nome"] for a in r["arquivos"]]),
           (["Projetos"], ["leia-me.txt"]))
        ck("totais da raiz", (r["totais"]["arquivos"], r["totais"]["bytes"]), (2, 3005))

        # A linha termina de ser escrita e chegam mais (só o que é novo é lido)
        with open(man, "a", encoding="utf-8") as f:
            f.write(_linha("Projetos/Filme A/still.jpg", 20)[30:])
            f.write(_linha("Projetos/Filme B/som.wav", 7, sha=False))
            f.write(_linha("Projetos\\Filme A\\bruto.mov", 3000, mi=MI))   # retomada repetiu
        r = conteudo.listar(p, "HD X", pasta="Projetos")
        ck("barra invertida do Windows vira pasta", [s["nome"] for s in r["subpastas"]], ["Filme A", "Filme B"])
        ck("repetido no manifesto conta uma vez", r["totais"]["arquivos"], 3)
        ck("tipos da pasta", (r["totais"]["tipos"]["video"], r["totais"]["tipos"]["imagem"], r["totais"]["tipos"]["audio"]),
           (1, 1, 1))
        ck("sem impressão digital contado", r["totais"]["sem_hash"], 1)
        ck("migalhas", [m["caminho"] for m in conteudo.listar(p, "HD X", pasta="Projetos/Filme A")["migalhas"]],
           ["Projetos", "Projetos/Filme A"])
        a = conteudo.listar(p, "HD X", pasta="Projetos/Filme A", ordem="tamanho")["arquivos"][0]
        ck("dados de mídia legíveis", (a["nome"], a["duracao"], a["resolucao"], a["codec"]),
           ("bruto.mov", "1:02:05", "1920x1080", "ProRes"))
        ck("ordem 'lidos por último'", [x["nome"] for x in conteudo.listar(p, "HD X", pasta="Projetos/Filme A", ordem="recentes")["arquivos"]],
           ["bruto.mov", "still.jpg"])

        b = conteudo.listar(p, "HD X", busca="WAV")
        ck("busca em toda a árvore (sem diferenciar maiúsculas)", ([x["caminho"] for x in b["arquivos"]], b["subpastas"]),
           (["Projetos/Filme B/som.wav"], []))
        ck("filtro por tipo", [x["nome"] for x in conteudo.listar(p, "HD X", tipo="imagem")["arquivos"]], ["still.jpg"])
        ck("pasta com '..' não sai do HD", conteudo.listar(p, "HD X", pasta="../../Projetos")["pasta"], "Projetos")

        # "Completar dados" reescreve o manifesto (arquivo novo, menor): índice refeito
        tmp = p / "novo.tmp"
        tmp.write_text(_linha("so_isto.mov", 1), encoding="utf-8")
        os.replace(tmp, man)
        r = conteudo.listar(p, "HD X")
        ck("manifesto reescrito: índice refeito", (r["total_hd"], [a["nome"] for a in r["arquivos"]]), (1, ["so_isto.mov"]))

        # Reescrito no MESMO arquivo e maior (sem troca de inode, como pode
        # acontecer em exFAT): a cauda já lida não confere -> índice refeito
        with open(man, "w", encoding="utf-8") as f:
            f.write(_linha("outro_primeiro.mov", 2))
            f.write(_linha("so_isto.mov", 1, mi=MI))
        r = conteudo.listar(p, "HD X")
        ck("reescrito no mesmo arquivo: índice refeito", sorted(a["nome"] for a in r["arquivos"]),
           ["outro_primeiro.mov", "so_isto.mov"])
        ck("e com os dados novos", next(a for a in r["arquivos"] if a["nome"] == "so_isto.mov")["resolucao"], "1920x1080")

        # paginação
        with open(man, "a", encoding="utf-8") as f:
            for i in range(conteudo.POR_PAGINA + 5):
                f.write(_linha(f"lote/f{i:04d}.dpx", 1))
        r2 = conteudo.listar(p, "HD X", pasta="lote", pagina=2)
        ck("paginação", (r2["paginas"], r2["pagina"], len(r2["arquivos"])), (2, 2, 5))
    return checks, falhas


def test_conteudo():
    _, falhas = verificar()
    assert not falhas, f"Conteúdo do HD incorreto: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    print(f"RESULTADO: {'OK — todas passaram' if not falhas else 'FALHOU: '+str(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
