#!/usr/bin/env python3
"""Testa que falhas de leitura do disco (setor defeituoso, pasta ilegível) NÃO
abortam a varredura nem somem em silêncio: são contadas, registradas e puladas,
e os demais arquivos são inventariados normalmente."""
import json
import os
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import scan  # noqa: E402


def verificar():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    eventos = []
    with tempfile.TemporaryDirectory() as td:
        raiz = Path(td) / "disco"; (raiz / "pasta").mkdir(parents=True)
        for n in ("a.txt", "ruim.mov", "c.txt"):
            (raiz / "pasta" / n).write_bytes(n.encode() * 10)
        man = Path(td) / "manifestos"

        orig = (scan.MANIFESTOS, scan.Path.is_file, scan.os.walk, scan.CALLBACK,
                scan.siegfried_lote, scan.exiftool_lote, scan.mediainfo)
        def is_file_com_defeito(self):
            if self.name == "ruim.mov":   # simula WinError 23 (CRC) -> errno 22
                raise OSError(22, "Invalid argument", str(self))
            return orig[1](self)
        def walk_com_pasta_ilegivel(top, onerror=None, **kw):
            if onerror:                    # simula pasta que não pode ser listada
                onerror(OSError(5, "Access denied", str(Path(top) / "ilegivel")))
            yield from orig[2](top, onerror=onerror, **kw)
        try:
            scan.MANIFESTOS = man
            scan.Path.is_file = is_file_com_defeito
            scan.os.walk = walk_com_pasta_ilegivel
            scan.CALLBACK = eventos.append
            scan.siegfried_lote = lambda r: {}
            scan.exiftool_lote = lambda r: {}
            scan.mediainfo = lambda c: None
            abortou = False
            try:
                scan.varrer("HDTESTE", raiz)
            except Exception:
                abortou = True
        finally:
            (scan.MANIFESTOS, scan.Path.is_file, scan.os.walk, scan.CALLBACK,
             scan.siegfried_lote, scan.exiftool_lote, scan.mediainfo) = orig

        mj = man / "manifesto_HDTESTE.jsonl"
        nomes = sorted(json.loads(l)["nome"] for l in open(mj, encoding="utf-8") if l.strip())

    done = [e for e in eventos if e.get("event") == "done"]
    avisos = [e for e in eventos if e.get("event") == "aviso_arquivo"]
    ck("varredura NÃO abortou", abortou, False)
    ck("arquivos bons inventariados", nomes, ["a.txt", "c.txt"])
    ck("evento done emitido", len(done), 1)
    ck("2 falhas contadas (arquivo + pasta)", done[0]["erros"] if done else None, 2)
    ck("arquivo com defeito registrado", any("ruim.mov" in a["arquivo"] for a in avisos), True)
    ck("pasta ilegível registrada", any("ilegivel" in a["arquivo"] for a in avisos), True)
    return checks, falhas


def test_falha_leitura():
    _, falhas = verificar()
    assert not falhas, f"Tratamento de falha de leitura incorreto: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    print(f"RESULTADO: {'OK — todas passaram' if not falhas else 'FALHOU: '+str(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
