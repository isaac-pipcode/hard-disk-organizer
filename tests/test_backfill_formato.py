#!/usr/bin/env python3
"""Testa o backfill de FORMATO/PUID (Siegfried) — completa discos varridos por
versões antigas do .exe (sem default.sig), SEM re-hashear, e mantém CSV↔JSONL
em sincronia. Não depende de ferramentas reais: o Siegfried em lote é substituído
por um mapa controlado."""
import csv
import json
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import scan  # noqa: E402


# manifesto de entrada: alguns registros SEM formato, um JÁ identificado, um sem match
DADOS = [
    # AV sem formato -> deve ser preenchido pelo mapa
    {"disco_label": "HDX", "caminho": "v/a.mp4", "nome": "a.mp4", "extensao": "mp4",
     "tamanho_bytes": 100, "mtime": "t", "sha256": "a"*64, "puid": None, "formato": None},
    # imagem JÁ identificada -> NÃO pode ser re-processada (mantém puid/formato)
    {"disco_label": "HDX", "caminho": "i/b.jpg", "nome": "b.jpg", "extensao": "jpg",
     "tamanho_bytes": 200, "mtime": "t", "sha256": "b"*64, "puid": "fmt/44", "formato": "JPEG"},
    # documento sem formato -> preenchido
    {"disco_label": "HDX", "caminho": "d/manual.pdf", "nome": "manual.pdf", "extensao": "pdf",
     "tamanho_bytes": 300, "mtime": "t", "sha256": "c"*64, "puid": "", "formato": ""},
    # extensão qualquer sem formato E sem match no mapa -> continua vazio
    {"disco_label": "HDX", "caminho": "u/weird.xyz", "nome": "weird.xyz", "extensao": "xyz",
     "tamanho_bytes": 400, "mtime": "t", "sha256": "d"*64, "puid": None, "formato": None},
]

# o que o Siegfried "identificaria" (chave = caminho normalizado)
MAPA_SF = {
    "v/a.mp4":       ("fmt/199", "MPEG-4 Media File"),
    "d/manual.pdf":  ("fmt/18",  "Acrobat PDF 1.4"),
    # weird.xyz ausente de propósito
}


def verificar():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    with tempfile.TemporaryDirectory() as td:
        man = Path(td) / "manifestos"
        man.mkdir(parents=True)
        mj = man / "manifesto_HDX.jsonl"
        mc = man / "manifesto_HDX.csv"
        with open(mj, "w", encoding="utf-8") as f:
            for r in DADOS:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        # CSV inicial (como o varrer teria escrito), com formato ainda vazio
        campos = ["disco_label", "caminho", "nome", "extensao", "tamanho_bytes",
                  "mtime", "sha256", "puid", "formato"]
        with open(mc, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos); w.writeheader()
            for r in DADOS:
                w.writerow({k: r.get(k) for k in campos})

        # injeta ambiente controlado: só o Siegfried "existe"; nada de MediaInfo/ExifTool
        orig_mani, orig_ferr, orig_sf = scan.MANIFESTOS, scan.ferramenta, scan.siegfried_lote
        try:
            scan.MANIFESTOS = man
            scan.ferramenta = lambda nome: "sf-fake" if nome == "sf" else None
            scan.siegfried_lote = lambda raiz: MAPA_SF
            scan.backfill("HDX", Path(td))
        finally:
            scan.MANIFESTOS, scan.ferramenta, scan.siegfried_lote = orig_mani, orig_ferr, orig_sf

        depois = [json.loads(l) for l in open(mj, encoding="utf-8") if l.strip()]
        por_caminho = {r["caminho"]: r for r in depois}
        csv_rows = list(csv.DictReader(open(mc, encoding="utf-8")))
        csv_por_caminho = {r["caminho"]: r for r in csv_rows}

    # formato preenchido onde faltava
    ck("mp4 recebeu PUID", por_caminho["v/a.mp4"]["puid"], "fmt/199")
    ck("mp4 recebeu formato", por_caminho["v/a.mp4"]["formato"], "MPEG-4 Media File")
    ck("pdf recebeu PUID", por_caminho["d/manual.pdf"]["puid"], "fmt/18")
    # já identificado permanece intocado
    ck("jpg mantém PUID", por_caminho["i/b.jpg"]["puid"], "fmt/44")
    ck("jpg mantém formato", por_caminho["i/b.jpg"]["formato"], "JPEG")
    # sem match no mapa continua vazio (nunca inventado)
    ck("xyz sem match continua sem PUID", por_caminho["u/weird.xyz"]["puid"], None)
    # SHA-256 NUNCA é recalculado/alterado
    esperado_sha = {r["caminho"]: r["sha256"] for r in DADOS}
    ck("SHA-256 inalterado (todos)",
       {c: por_caminho[c]["sha256"] for c in esperado_sha}, esperado_sha)
    # CSV reescrito em sincronia com o JSONL
    ck("CSV tem todas as linhas", len(csv_rows), len(DADOS))
    ck("CSV mp4 em sincronia (formato)", csv_por_caminho["v/a.mp4"]["formato"], "MPEG-4 Media File")
    ck("CSV pdf em sincronia (puid)", csv_por_caminho["d/manual.pdf"]["puid"], "fmt/18")
    ck("CSV sha em sincronia", csv_por_caminho["v/a.mp4"]["sha256"], "a"*64)
    return checks, falhas


def test_backfill_formato():
    _, falhas = verificar()
    assert not falhas, f"Backfill de formato incorreto: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    print(f"RESULTADO: {'OK — todas passaram' if not falhas else 'FALHOU: '+str(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
