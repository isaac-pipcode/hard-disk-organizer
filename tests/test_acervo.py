#!/usr/bin/env python3
"""Testa o Acervo (tela inicial): a situação de cada HD montada só com o que está
no computador — manifestos, fichas físicas e registros de operação —, sem banco."""
import json
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import acervo  # noqa: E402
import hardware  # noqa: E402

TB = 10**12


def _manifesto(pasta, etiqueta, registros):
    with open(pasta / f"manifesto_{etiqueta}.jsonl", "w", encoding="utf-8") as f:
        for r in registros:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _reg(caminho, ext, tam=100, sha=True, fmt=True, mi=True):
    return {"caminho": caminho, "extensao": ext, "tamanho_bytes": tam,
            "sha256": "a" * 64 if sha else None, "puid": "fmt/1" if fmt else None,
            "formato": "X" if fmt else None, "mediainfo": {"ok": 1} if mi else None}


def _card(dados, etiqueta):
    for s in dados["secoes"]:
        for c in s["cartoes"]:
            if c["etiqueta"] == etiqueta:
                return c
    return None


def verificar():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        # HD A: completo, com ficha física e grupo
        _manifesto(p, "HD A", [_reg("v1.mov", "mov"), _reg("v1.mov", "mov"), _reg("f.jpg", "jpg"), _reg("d.pdf", "pdf", mi=False)])
        hardware.atualizar_ficha(p, "HD A", marca="Western Digital", linha="My Book", tipo="mesa",
                                 rotulo_tipo="HD de mesa (com fonte)", formato="3,5″", conexao="USB",
                                 tamanho=4 * TB, serial="WX31A81B2C3D", grupo="antigos",
                                 varredura={"status": "concluida"})
        # HD B: falta formato em 1 e dados de mídia em 2 (vídeo + imagem)
        _manifesto(p, "HD B", [_reg("a.mov", "mov", mi=False), _reg("b.jpg", "jpg", mi=False),
                               _reg("c.doc", "doc", fmt=False, mi=False), _reg("d.mov", "mov")])
        # HD C: a última varredura ficou "em andamento" (programa fechado no meio)
        _manifesto(p, "HD C", [_reg("x.mov", "mov")])
        hardware.atualizar_ficha(p, "HD C", varredura={"status": "em_andamento"})
        # HD D: inventário antigo, sem ficha; registro da varredura cortado no meio
        _manifesto(p, "HD D", [_reg("y.mov", "mov")])
        (p / "operacao_HD D_20261001_100000.log").write_text("=== varredura HD D (E:\\) — x ===\n  500 lidos\n", encoding="utf-8")
        # HD E: inventário antigo, varredura terminou OK (e um backfill depois)
        _manifesto(p, "HD E", [_reg("z.mov", "mov")])
        (p / "operacao_HD E_20261001_100000.log").write_text("=== varredura HD E (F:\\) ===\nOK — disco 'HD E': 1 lidos\n", encoding="utf-8")
        (p / "operacao_HD E_20261002_100000.log").write_text("=== backfill HD E (F:\\) ===\nsem fim\n", encoding="utf-8")
        # HD F: tem ficha, mas nenhum inventário ainda
        hardware.atualizar_ficha(p, "HD F", grupo="transporte")
        # HD G: marcado "não reconhecido" e varrido DEPOIS -> a marca não vale mais
        _manifesto(p, "HD G", [_reg("g.mov", "mov")])
        hardware.atualizar_ficha(p, "HD G", nao_reconhecido={"sintoma": "barulho", "em": "2026-10-01T09:00:00"},
                                 varredura={"status": "concluida", "em": "2026-10-02T09:00:00"})
        # HD H: varrido antes, marcado "não reconhecido" depois -> vale
        _manifesto(p, "HD H", [_reg("h.mov", "mov")])
        hardware.atualizar_ficha(p, "HD H", nao_reconhecido={"sintoma": "barulho", "em": "2026-10-05T09:00:00"},
                                 varredura={"status": "concluida", "em": "2026-10-02T09:00:00"})

        conectados = [
            {"valor": "E:\\", "letra": "E:", "rotulo": "MIDIAS", "reconhecido": "HD A",
             "livre_pct": 5.0, "livre": "200 GB", "tamanho": "3,6 TB", "hw": None},
            {"valor": "G:\\", "letra": "G:", "rotulo": "TRANSPORTE 07", "reconhecido": None,
             "livre_pct": 25.0, "livre": "500 GB", "tamanho": "1,8 TB",
             "hw": {"tipo": "portatil", "marca": "Toshiba", "linha": "Canvio", "rotulo_tipo": "HD portátil",
                    "formato": "2,5″", "conexao": "USB"}},
            {"valor": "C:\\", "letra": "C:", "rotulo": "Windows", "sistema": True, "reconhecido": None, "hw": None},
        ]
        dados = acervo.montar(p, job=None, conectados=conectados)
        a, b, c = _card(dados, "HD A"), _card(dados, "HD B"), _card(dados, "HD C")
        d, e, f = _card(dados, "HD D"), _card(dados, "HD E"), _card(dados, "HD F")

        ck("HD completo = concluído", a["situacao"], "concluido")
        ck("cartão traz marca, linha e tipo da ficha", (a["marca"], a["linha"], a["tipo"]), ("Western Digital", "My Book", "mesa"))
        ck("grupo legível", a["grupo"], "Antigo")
        ck("arquivo repetido no manifesto conta uma vez", a["arquivos"], "3")
        ck("HD reconhecido ligado agora: letra e espaço livre atuais", (a["conectado_em"], a["livre_pct"]), ("E:", 5.0))
        ck("falta formato/mídia = completar dados", b["situacao"], "completar")
        ck("detalhe do que falta", b["detalhe"], "1 sem formato · 2 sem dados de mídia")
        ck("varredura largada 'em andamento' = incompleta", c["situacao"], "incompleta")
        ck("inventário antigo com registro cortado = incompleta", d["situacao"], "incompleta")
        ck("inventário antigo com registro OK = concluído (ignora o backfill)", e["situacao"], "concluido")
        ck("ficha sem inventário = na fila para varrer", f["situacao"], "a_varrer")
        ck("HD sem ficha física pede para ligar (marca vazia)", (d["marca"], d["tem_ficha_fisica"]), ("", False))
        ck("marca de não reconhecido: vale só se for depois da última leitura",
           (_card(dados, "HD G")["situacao"], _card(dados, "HD H")["situacao"]), ("concluido", "nao_reconhecido"))
        ck("detalhe do não reconhecido = sintoma", _card(dados, "HD H")["detalhe"], acervo.SINTOMAS["barulho"])

        novos = [x for s in dados["secoes"] if s["id"] == "novo" for x in s["cartoes"]]
        ck("HD novo ligado vira cartão próprio (disco do sistema fica de fora)", len(novos), 1)
        ck("cartão do HD novo traz marca e tipo lidos do disco", (novos[0]["marca"], novos[0]["tipo"]), ("Toshiba", "portatil"))

        ordem = [s["id"] for s in dados["secoes"]]
        ck("seções na ordem de prioridade", ordem, ["incompleta", "nao_reconhecido", "novo", "completar", "a_varrer", "concluido"])
        k = dados["kpi"]
        ck("indicadores", (k["hds"], k["varridos"], k["atencao"], k["concluidos"], k["pouco_espaco"]), (8, 7, 4, 3, 1))

        # com uma varredura em andamento no HD C
        dados2 = acervo.montar(p, job={"disco": "HD C", "pct": 41, "fase": "Lendo e conferindo arquivos"}, conectados=[])
        c2 = _card(dados2, "HD C")
        ck("operação em andamento = varrendo, com progresso", (c2["situacao"], c2["progresso"]), ("varrendo", 41))
        ck("'Em andamento' vem primeiro", dados2["secoes"][0]["id"], "varrendo")

        # pasta vazia não quebra
        vazio = acervo.montar(p / "nao_existe")
        ck("sem manifestos: acervo vazio", (vazio["secoes"], vazio["kpi"]["hds"]), ([], 0))
    return checks, falhas


def test_acervo():
    _, falhas = verificar()
    assert not falhas, f"Acervo incorreto: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    print(f"RESULTADO: {'OK — todas passaram' if not falhas else 'FALHOU: '+str(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
