#!/usr/bin/env python3
"""Testa o pacote de envio: o .zip leva inventários, fichas e registros (com o
SHA-256 de cada arquivo), lembra o que já foi enviado e volta a marcar como
pendente o inventário que mudou. Do outro lado, "Importar manifestos" lê o .zip
inteiro — inclusive a ficha do HD não reconhecido."""
import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import acervo  # noqa: E402
import hardware  # noqa: E402
import importar  # noqa: E402
import pacote  # noqa: E402


def _manifesto(pasta, etiqueta, n=2):
    with open(pasta / f"manifesto_{etiqueta}.jsonl", "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"disco_label": etiqueta, "caminho": f"v{i}.mov", "extensao": "mov", "tamanho_bytes": 10,
                                "sha256": "a" * 64, "puid": "fmt/1", "formato": "X",
                                "mediainfo": {"ok": 1}}) + "\n")


def verificar():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    with tempfile.TemporaryDirectory() as td:
        raiz = Path(td)
        man, env = raiz / "manifestos", raiz / "envios"
        man.mkdir()
        _manifesto(man, "HD A")
        _manifesto(man, "HD B", 3)
        hardware.atualizar_ficha(man, "HD A", marca="Seagate", tipo="portatil", varredura={"status": "concluida"})
        (man / "operacao_HD A_20261001_100000.log").write_text("=== varredura HD A ===\nOK — disco 'HD A'\n", encoding="utf-8")
        hardware.atualizar_ficha(man, "HD U", nao_reconhecido={"sintoma": "nao_aparece", "obs": "", "em": "2026-10-30"},
                                 capacidade_nominal="2 TB")

        sit = pacote.situacao_envio(man, env)
        ck("tudo pendente antes do primeiro pacote", {e: v["pendente"] for e, v in sit.items()},
           {"HD A": True, "HD B": True})

        res = pacote.gerar(man, env)
        zpath = Path(res["arquivo"])
        ck("pacote padrão leva os pendentes", res["discos"], ["HD A", "HD B"])
        ck("ficha do HD sem inventário vai junto", res["fichas_sem_inventario"], 1)
        ck("sem arquivo .parcial sobrando", list(env.glob("*.parcial")), [])
        with zipfile.ZipFile(zpath) as z:
            nomes = sorted(z.namelist())
            info = json.loads(z.read("pacote.json"))
            sha_ok = all(hashlib.sha256(z.read("manifestos/" + i["arquivo"])).hexdigest() == i["sha256"]
                         for d in info["discos"] for i in d["arquivos"])
        ck("conteúdo do zip", nomes, sorted(["LEIAME.txt", "pacote.json", "manifestos/manifesto_HD A.jsonl",
                                             "manifestos/disco_HD A.json", "manifestos/operacao_HD A_20261001_100000.log",
                                             "manifestos/manifesto_HD B.jsonl", "manifestos/disco_HD U.json"]))
        ck("pacote.json traz versão e o SHA-256 confere", (bool(info["versao_programa"]), sha_ok), (True, True))
        ck("sha256 do pacote", res["sha256"], hashlib.sha256(zpath.read_bytes()).hexdigest())

        sit = pacote.situacao_envio(man, env)
        ck("depois do pacote: nada pendente", [e for e, v in sit.items() if v["pendente"]], [])
        ck("registro aponta o pacote", sit["HD A"]["pacote"], zpath.name)
        try:
            pacote.gerar(man, env)
            ck("sem pendentes: recusa pacote vazio", "gerou", "erro")
        except ValueError:
            ck("sem pendentes: recusa pacote vazio", "erro", "erro")

        # "Completar dados" reescreve o inventário do HD B -> volta a ficar pendente
        time.sleep(0.01)
        _manifesto(man, "HD B", 4)
        sit = pacote.situacao_envio(man, env)
        ck("inventário que mudou volta a ficar pendente", [e for e, v in sit.items() if v["pendente"]], ["HD B"])
        d = acervo.montar(man, envio=sit)
        situ = {c["etiqueta"]: c["situacao"] for s in d["secoes"] for c in s["cartoes"]}
        ck("Acervo: pendente concluído = pronto para enviar; enviado = concluído; NR",
           (situ["HD B"], situ["HD A"], situ["HD U"]), ("enviar", "concluido", "nao_reconhecido"))
        ck("KPI para enviar", d["kpi"]["para_enviar"], 1)
        a = next(c for s in d["secoes"] for c in s["cartoes"] if c["etiqueta"] == "HD A")
        ck("HD já enviado diz em qual pacote", a["detalhe"].startswith("Incluído no pacote de"), True)

        res2 = pacote.gerar(man, env, selecionadas=["HD B", "não existe"])
        ck("seleção manual ignora etiqueta inexistente", res2["discos"], ["HD B"])
        ck("pacotes listados (2)", len(pacote.pacotes(env)), 2)

        # Do outro lado: importar o .zip numa pasta vazia
        destino = raiz / "gestor"
        r = importar.importar_de(zpath, destino)
        ck("importa os 2 inventários", sorted(x["disco"] for x in r["discos"] if x["situacao"] == "importado"),
           ["HD A", "HD B"])
        ck("importa fichas e registros", (r["fichas"], r["registros"]), (2, 1))
        ficha_u = hardware.ler_ficha(destino, "HD U")
        ck("ficha do não reconhecido chega intacta", (ficha_u.get("nao_reconhecido") or {}).get("sintoma"), "nao_aparece")
        # O HD U volta a funcionar e é varrido na escola; o pacote novo leva a ficha
        # sem a marca — no computador do gestor ele deixa de ser "não reconhecido".
        hardware.atualizar_ficha(man, "HD U", remover=("nao_reconhecido",),
                                 varredura={"status": "concluida", "em": "2026-11-01T10:00:00"})
        _manifesto(man, "HD U")
        res3 = pacote.gerar(man, env, selecionadas=["HD U"])
        importar.importar_de(res3["arquivo"], destino)
        d2 = acervo.montar(destino)
        u = next(c for s in d2["secoes"] for c in s["cartoes"] if c["etiqueta"] == "HD U")
        ck("gestor: HD varrido depois da marca deixa de ser não reconhecido", u["situacao"], "concluido")
        r2 = importar.importar_de(zpath, destino)
        ck("reimportar o mesmo pacote: idêntico, sem duplicar registro",
           ([x["situacao"] for x in r2["discos"]], r2["registros"]), (["identico", "identico"], 0))
        ck("pasta de envios sem lixo", sorted(os.listdir(env))[-1], pacote.REGISTRO)
    return checks, falhas


def test_pacote():
    _, falhas = verificar()
    assert not falhas, f"Pacote de envio incorreto: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    print(f"RESULTADO: {'OK — todas passaram' if not falhas else 'FALHOU: '+str(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
