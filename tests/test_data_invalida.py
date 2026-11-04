#!/usr/bin/env python3
"""Testa que um arquivo com data fora do intervalo do Windows (antes de 1970, ex.:
01/01/1601 de cartão de câmera com relógio desacertado) NÃO aborta a varredura.

No Windows, `datetime.fromtimestamp` levanta `OSError: [Errno 22] Invalid argument`
para essas datas. Era o erro que derrubava os HDs EDICAO D e E sempre no mesmo ponto.
Como o teste roda em Linux (onde a conversão funciona), o comportamento do Windows
é simulado substituindo o `datetime` usado pelo scanner."""
import datetime as _dt
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import scan  # noqa: E402

NTFS_ZERO = -11644473600          # 1601-01-01T00:00:00Z, o "zero" do NTFS
ANTES_1970 = -14182940            # 1969-07-20T20:17:40Z (o FS do Linux de CI guarda; 1601 não)


class _DatetimeWindows(_dt.datetime):
    """`datetime` que se comporta como no Windows: Errno 22 antes de 1970."""
    @classmethod
    def fromtimestamp(cls, ts, tz=None):
        if ts < 0:
            raise OSError(22, "Invalid argument")
        return _dt.datetime.fromtimestamp(ts, tz)


def verificar():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    fake = types.SimpleNamespace(datetime=_DatetimeWindows, timezone=_dt.timezone,
                                 timedelta=_dt.timedelta)
    eventos = []
    with tempfile.TemporaryDirectory() as td:
        raiz = Path(td) / "disco"; (raiz / "DCIM").mkdir(parents=True)
        for n in ("a.txt", "M2100.CTG", "c.txt"):
            (raiz / "DCIM" / n).write_bytes(n.encode() * 10)
        os.utime(raiz / "DCIM" / "M2100.CTG", (ANTES_1970, ANTES_1970))
        man = Path(td) / "manifestos"

        orig = (scan.MANIFESTOS, scan.datetime, scan.CALLBACK,
                scan.siegfried_lote, scan.exiftool_lote, scan.mediainfo)
        try:
            scan.MANIFESTOS = man
            scan.datetime = fake
            scan.CALLBACK = eventos.append
            scan.siegfried_lote = lambda r, **kw: {}
            scan.exiftool_lote = lambda r: {}
            scan.mediainfo = lambda c: None
            ck("iso() de 1601 não levanta", scan.iso(NTFS_ZERO), "1601-01-01T00:00:00+00:00")
            ck("iso() de data irrepresentável devolve None", scan.iso(1e15), None)
            abortou = False
            try:
                scan.varrer("HDDATA", raiz)
            except Exception:
                abortou = True
            # 2ª passada (retomada): o arquivo de 1601 deve ser reconhecido e pulado
            eventos.clear()
            scan.varrer("HDDATA", raiz)
        finally:
            (scan.MANIFESTOS, scan.datetime, scan.CALLBACK,
             scan.siegfried_lote, scan.exiftool_lote, scan.mediainfo) = orig

        regs = [json.loads(l) for l in open(man / "manifesto_HDDATA.jsonl", encoding="utf-8")
                if l.strip()]

    por_nome = {r["nome"]: r for r in regs}
    done = [e for e in eventos if e.get("event") == "done"]
    ck("varredura NÃO abortou", abortou, False)
    ck("os 3 arquivos inventariados, sem duplicar na retomada", sorted(r["nome"] for r in regs),
       ["M2100.CTG", "a.txt", "c.txt"])
    ck("mtime anterior a 1970 registrado", (por_nome.get("M2100.CTG") or {}).get("mtime"),
       "1969-07-20T20:17:40+00:00")
    ck("retomada: 0 novos", done[0]["novos"] if done else None, 0)
    return checks, falhas


def test_data_invalida():
    _, falhas = verificar()
    assert not falhas, f"Data fora do intervalo derruba a varredura: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    print(f"RESULTADO: {'OK — todas passaram' if not falhas else 'FALHOU: '+str(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
