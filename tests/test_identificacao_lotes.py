#!/usr/bin/env python3
"""Testa a identificação de formatos em LOTES (Siegfried) e o tempo-limite do ExifTool.

Em campo, a fase "Identificando formatos" passava horas sem progresso (o sf lia cada
vídeo inteiro) e podia travar para sempre num setor defeituoso. Agora:
  - o sf roda em lotes (-f lista), com progresso a cada lote;
  - lote que estoura o tempo é refeito um a um, isolando só o arquivo travado;
  - muitos arquivos travados = disco degradando: a identificação é interrompida;
  - pastas de sistema não entram na lista;
  - o ExifTool tem tempo-limite e aproveita a saída parcial.
Usa um `sf` e um `exiftool` FALSOS (scripts) que imitam o real — inclusive o buffer:
um arquivo "travado" não produz saída nenhuma, como o sf de verdade."""
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import scan  # noqa: E402

SF_FALSO = '''#!{py}
import sys, time, csv, os
a = sys.argv[1:]
if "-version" in a:
    print("siegfried 1.11.9")
    print("  - pronom: DROID_SignatureFile_V125.xml" + ("" if os.environ.get("SF_SEM_LIMITE") else "; max BOF 1048576; max EOF 1048576"))
    sys.exit(0)
lista = a[a.index("-f") + 1]
caminhos = [l.rstrip("\\n") for l in open(lista, encoding="utf-8") if l.strip()]
linhas = []
for c in caminhos:
    if "TRAVA" in os.path.basename(c):
        time.sleep(60)          # setor defeituoso: o sf fica preso e nada sai (buffer)
    ext = os.path.splitext(c)[1].lstrip(".")
    linhas.append([c, "1", "", "", "pronom", "fmt/" + str(len(ext)), "Formato " + ext.upper(), "", "", "", "", ""])
w = csv.writer(sys.stdout)
w.writerow(["filename","filesize","modified","errors","namespace","id","format","version","mime","class","basis","warning"])
w.writerows(linhas)
'''

EXIF_FALSO = '''#!{py}
import sys, time
sys.stdout.write('[{{"SourceFile": "{raiz}/Fotos/a.jpg", "ImageWidth": 10}},\\n')
sys.stdout.write(' {{"SourceFile": "{raiz}/Fotos/b.jpg", "ImageWidth": 20}},\\n {{"SourceFile": "{raiz}/Fot')
sys.stdout.flush()
time.sleep(60)                  # trava na terceira imagem
'''


def _script(pasta, nome, corpo):
    p = Path(pasta) / nome
    p.write_text(corpo, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _arvore(raiz, nomes):
    for n in nomes:
        f = Path(raiz) / n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 10)


def verificar():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    orig = {k: getattr(scan, k) for k in ("ferramenta", "SF_LOTE", "SF_TEMPO_LOTE",
            "SF_TEMPO_ARQUIVO", "SF_MAX_TRAVADOS", "EXIF_TEMPO_BASE",
            "EXIF_TEMPO_POR_IMAGEM", "CALLBACK", "MANIFESTOS", "mediainfo")}
    with tempfile.TemporaryDirectory() as td:
        sf = _script(td, "sf", SF_FALSO.format(py=sys.executable))
        ferramentas = {"sf": sf}
        try:
            scan.ferramenta = lambda n: ferramentas.get(n)
            scan.SF_LOTE, scan.SF_TEMPO_LOTE, scan.SF_TEMPO_ARQUIVO = 5, 3, 2
            scan.SF_MAX_TRAVADOS = 2
            eventos = []
            scan.CALLBACK = eventos.append

            # 1) caso normal + pastas de sistema fora da lista + progresso
            r1 = Path(td) / "d1"
            _arvore(r1, [f"Videos/v{i}.mov" for i in range(12)] + ["Fotos/f.jpg",
                    "System Volume Information/x.dat", "$RECYCLE.BIN/apagado.mov",
                    "Edição 2025/cena ç.mxf"])
            prog = []
            m = scan.siegfried_lote(r1, progresso=lambda f, t: prog.append((f, t)))
            ck("todos os arquivos do acervo identificados", len(m), 14)
            ck("caminho relativo + formato", m.get("Videos/v0.mov"), ("fmt/3", "Formato MOV"))
            ck("acentos preservados", m.get("Edição 2025/cena ç.mxf"), ("fmt/3", "Formato MXF"))
            ck("System Volume Information fora", any("System Volume" in k for k in m), False)
            ck("Lixeira fora", any("RECYCLE" in k for k in m), False)
            ck("progresso começa em 0", prog[0], (0, 14))
            ck("progresso chega ao total", prog[-1], (14, 14))
            ck("progresso por lote (5 em 5)", [f for f, _ in prog], [0, 5, 10, 14])

            # 2) um arquivo travado: só ele fica sem formato, o resto do lote é salvo
            r2 = Path(td) / "d2"
            _arvore(r2, [f"a{i}.mov" for i in range(4)] + ["b_TRAVA.mov"] + [f"c{i}.wav" for i in range(5)])
            eventos.clear()
            t0 = time.time()
            m2 = scan.siegfried_lote(r2)
            dt = time.time() - t0
            ck("travado isolado: 9 de 10 identificados", len(m2), 9)
            ck("arquivo travado sem formato", "b_TRAVA.mov" in m2, False)
            ck("vizinhos do travado identificados", sorted(k for k in m2 if k.startswith("a")),
               [f"a{i}.mov" for i in range(4)])
            ck("aviso do arquivo travado emitido",
               any(e.get("event") == "aviso_arquivo" and "b_TRAVA" in e.get("arquivo", "") for e in eventos), True)
            ck("não esperou o travamento (tempo-limite funcionou)", dt < 20, True)

            # 3) muitos travados (disco degradando): interrompe e devolve o parcial
            r3 = Path(td) / "d3"
            _arvore(r3, ["a_ok.mov"] + [f"t{i}_TRAVA.mov" for i in range(4)] + [f"z{i}.mov" for i in range(5)])
            eventos.clear()
            m3 = scan.siegfried_lote(r3)
            ck("parcial preservado ao interromper", "a_ok.mov" in m3, True)
            ck("identificação interrompida (lotes seguintes não rodam)", any(k.startswith("z") for k in m3), False)
            ck("aviso de disco degradando emitido",
               any("interrompida" in (e.get("erro") or "") for e in eventos), True)

            # 4) detecção de assinatura limitada (a do .exe) x sem limite (sf -update)
            env = scan._sf_env(sf)
            ck("assinatura limitada detectada", scan._sf_limitado(sf, env), True)
            os.environ["SF_SEM_LIMITE"] = "1"
            ck("assinatura sem limite detectada", scan._sf_limitado(sf, scan._sf_env(sf)), False)
            del os.environ["SF_SEM_LIMITE"]

            # 5) JSON truncado do ExifTool: aproveita os objetos completos
            parcial = '[{"SourceFile": "a.jpg", "W": 1},\n {"SourceFile": "b.jpg", "X": {"y": 2}},\n {"SourceFi'
            ck("JSON parcial: objetos completos", [o["SourceFile"] for o in scan._json_parcial(parcial)],
               ["a.jpg", "b.jpg"])
            ck("JSON íntegro continua normal", scan._json_parcial('[{"a": 1}]'), [{"a": 1}])
            ck("saída vazia", scan._json_parcial(""), [])

            # 6) ExifTool travado: tempo-limite + saída parcial (antes: esperava para sempre)
            r6 = Path(td) / "d6"
            _arvore(r6, ["Fotos/a.jpg", "Fotos/b.jpg", "Fotos/c.jpg"])
            ferramentas["exiftool"] = _script(td, "exiftool", EXIF_FALSO.format(py=sys.executable, raiz=str(r6)))
            scan.EXIF_TEMPO_BASE, scan.EXIF_TEMPO_POR_IMAGEM = 2, 0
            t0 = time.time()
            m6 = scan.exiftool_lote(r6)
            ck("ExifTool travado não segura a varredura", time.time() - t0 < 20, True)
            ck("imagens lidas antes do travamento aproveitadas", sorted(m6), ["Fotos/a.jpg", "Fotos/b.jpg"])
            del ferramentas["exiftool"]

            # 7) varredura completa: eventos de identificação + formato no manifesto
            r7 = Path(td) / "d7"
            _arvore(r7, [f"v{i}.mov" for i in range(7)])
            scan.MANIFESTOS = Path(td) / "manifestos"
            scan.mediainfo = lambda c: None
            eventos.clear()
            scan.varrer("HDID", r7)
            ident = [e for e in eventos if e.get("event") == "identificando" and e.get("total") is not None]
            ck("varredura emite progresso da identificação", bool(ident), True)
            ck("identificação chega ao total", (ident[-1]["feitos"], ident[-1]["total"]) if ident else None, (7, 7))
            linhas = [json.loads(l) for l in open(scan.MANIFESTOS / "manifesto_HDID.jsonl", encoding="utf-8")]
            ck("formato gravado no manifesto", sorted({l["formato"] for l in linhas}), ["Formato MOV"])
        finally:
            for k, v in orig.items():
                setattr(scan, k, v)
    return checks, falhas


def test_identificacao_lotes():
    _, falhas = verificar()
    assert not falhas, f"Identificação em lotes incorreta: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    print(f"RESULTADO: {'OK — todas passaram' if not falhas else 'FALHOU: '+str(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
