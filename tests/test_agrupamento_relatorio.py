#!/usr/bin/env python3
"""Teste de agrupamento do relatório PRESERVA-SCAN.

Monta manifestos de DOIS discos com valores conhecidos e verifica se o relatório
(`scanner/relatorio.py`) agrupa corretamente:
  - totais e volume POR DISCO;
  - agrupamento POR CONTEÚDO (SHA-256): quantas cópias e em quantos discos —
    distinguindo duplicata no mesmo disco de duplicata espalhada em 2+ discos;
  - contagem POR FORMATO;
  - colunas AUDIOVISUAIS extraídas do MediaInfo/ExifTool (duração, resolução,
    codec, bitrate).

É uma verificação de regressão: qualquer mudança futura que quebre o agrupamento
é pega aqui. Roda sem dependências externas:

    python tests/test_agrupamento_relatorio.py        # imprime PASS/FALHA e sai 1 se falhar
    python -m pytest tests/                            # também funciona (usa o assert final)
"""
import csv
import json
import sys
import tempfile
from pathlib import Path

# Importa o relatório a partir do diretório scanner/, ao lado deste pacote de testes.
_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import relatorio  # noqa: E402


def _mi_video(dur, w, h, br):
    """Bloco no formato do MediaInfo (media.track com General/Video/Audio)."""
    return {"media": {"track": [
        {"@type": "General", "Duration": str(dur), "OverallBitRate": str(br)},
        {"@type": "Video", "Format": "AVC", "Format_Profile": "Main",
         "Width": str(w), "Height": str(h)},
        {"@type": "Audio", "Format": "AAC", "Channels": "2"}]}}


def _exif_img(w, h):
    """Bloco no formato do ExifTool (imagem)."""
    return {"ImageWidth": w, "ImageHeight": h, "FileType": "JPEG"}


# SHAs conhecidos: AAA (mp4, presente em 2 discos), BBB (jpg, 2 cópias no MESMO
# disco), CCC (mp4, único).
AAA, BBB, CCC = "a" * 64, "b" * 64, "c" * 64

HD_A = [  # 3 arquivos, 1400 bytes
    {"disco_label": "HD-A", "caminho": "video_x.mp4", "nome": "video_x.mp4", "extensao": "mp4",
     "tamanho_bytes": 1000, "mtime": "2026-01-01T00:00:00+00:00", "sha256": AAA,
     "puid": "fmt/199", "formato": "MPEG-4 Media File", "mediainfo": _mi_video(60, 1920, 1080, 8000000)},
    {"disco_label": "HD-A", "caminho": "foto_y.jpg", "nome": "foto_y.jpg", "extensao": "jpg",
     "tamanho_bytes": 200, "mtime": "2026-01-01T00:00:00+00:00", "sha256": BBB,
     "puid": "fmt/44", "formato": "JPEG File Interchange Format", "mediainfo": _exif_img(800, 600)},
    {"disco_label": "HD-A", "caminho": "copia_da_foto.jpg", "nome": "copia_da_foto.jpg", "extensao": "jpg",
     "tamanho_bytes": 200, "mtime": "2026-01-01T00:00:00+00:00", "sha256": BBB,
     "puid": "fmt/44", "formato": "JPEG File Interchange Format", "mediainfo": _exif_img(800, 600)},
]
HD_B = [  # 2 arquivos, 1500 bytes
    {"disco_label": "HD-B", "caminho": "backup/video_x.mp4", "nome": "video_x.mp4", "extensao": "mp4",
     "tamanho_bytes": 1000, "mtime": "2026-01-01T00:00:00+00:00", "sha256": AAA,   # mesmo conteúdo que HD-A
     "puid": "fmt/199", "formato": "MPEG-4 Media File", "mediainfo": _mi_video(60, 1920, 1080, 8000000)},
    {"disco_label": "HD-B", "caminho": "outro.mp4", "nome": "outro.mp4", "extensao": "mp4",
     "tamanho_bytes": 500, "mtime": "2026-01-01T00:00:00+00:00", "sha256": CCC,
     "puid": "fmt/199", "formato": "MPEG-4 Media File", "mediainfo": _mi_video(30, 1280, 720, 4000000)},
]


def _gerar(tmp: Path):
    man, sai = tmp / "manifestos", tmp / "relatorio"
    man.mkdir(parents=True, exist_ok=True)
    sai.mkdir(parents=True, exist_ok=True)
    for nome, linhas in (("HD-A", HD_A), ("HD-B", HD_B)):
        with open(man / f"manifesto_{nome}.jsonl", "w", encoding="utf-8") as f:
            for r in linhas:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    relatorio.gerar(man, sai)
    resumo = json.loads((sai / "resumo.json").read_text(encoding="utf-8"))
    inv = list(csv.DictReader(open(sai / "inventario_consolidado.csv", encoding="utf-8")))
    dup = list(csv.DictReader(open(sai / "duplicatas.csv", encoding="utf-8")))
    return resumo, inv, dup


def verificar():
    """Devolve (checagens, falhas). Não usa framework para rodar solto também."""
    with tempfile.TemporaryDirectory() as td:
        resumo, inv, dup = _gerar(Path(td))

    checks, falhas = [], []

    def check(nome, got, exp):
        ok = got == exp
        checks.append((nome, got, exp, ok))
        if not ok:
            falhas.append(nome)

    # Totais e agrupamento por disco
    check("total de arquivos", resumo["arquivos"], 5)
    check("total de bytes", resumo["bytes"], 2900)
    check("nº de discos", resumo["discos"], 2)
    check("HD-A arquivos", resumo["por_disco"]["HD-A"]["arquivos"], 3)
    check("HD-A bytes", resumo["por_disco"]["HD-A"]["bytes"], 1400)
    check("HD-B arquivos", resumo["por_disco"]["HD-B"]["arquivos"], 2)
    check("HD-B bytes", resumo["por_disco"]["HD-B"]["bytes"], 1500)

    # Agrupamento por conteúdo (SHA-256) e duplicatas
    check("arquivos únicos por conteúdo", resumo["arquivos_unicos_por_conteudo"], 3)
    check("grupos duplicados", resumo["grupos_duplicados"], 2)          # AAA e BBB
    check("grupos em 2+ discos", resumo["grupos_em_2mais_discos"], 1)   # só AAA
    check("arquivos redundantes (a mais)", resumo["arquivos_redundantes"], 2)
    check("bytes redundantes", resumo["bytes_redundantes"], 1200)       # 1000(AAA) + 200(BBB)

    fmt = dict(resumo["top_formatos"])
    check("formato MPEG-4 (contagem)", fmt.get("MPEG-4 Media File"), 3)
    check("formato JPEG (contagem)", fmt.get("JPEG File Interchange Format"), 2)

    por_sha = {}
    for r in dup:
        por_sha.setdefault(r["sha256"], []).append(r)
    check("linhas de duplicata p/ AAA", len(por_sha.get(AAA, [])), 2)
    check("AAA em_n_discos", {r["em_n_discos"] for r in por_sha.get(AAA, [])}, {"2"})
    check("linhas de duplicata p/ BBB", len(por_sha.get(BBB, [])), 2)
    check("BBB em_n_discos", {r["em_n_discos"] for r in por_sha.get(BBB, [])}, {"1"})
    check("único (CCC) NÃO entra em duplicatas", CCC in por_sha, False)

    # Colunas audiovisuais no inventário
    row_x = next(r for r in inv if r["caminho"] == "video_x.mp4" and r["disco_label"] == "HD-A")
    check("video_x duração", row_x["duracao"], "0:01:00")
    check("video_x resolução", row_x["resolucao"], "1920x1080")
    check("video_x codec_video", row_x["codec_video"], "AVC Main")
    check("video_x codec_audio", row_x["codec_audio"], "AAC 2ch")
    check("video_x bitrate", row_x["bitrate"], "8.0 Mbps")
    row_y = next(r for r in inv if r["caminho"] == "foto_y.jpg")
    check("foto_y resolução (ExifTool)", row_y["resolucao"], "800x600")
    check("inventário tem todas as linhas", len(inv), 5)

    return checks, falhas


def test_agrupamento_do_relatorio():
    """Ponto de entrada para o pytest."""
    _, falhas = verificar()
    assert not falhas, f"Agrupamento incorreto: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    if falhas:
        print(f"RESULTADO: FALHOU — {len(falhas)} verificação(ões): {falhas}")
        return 1
    print(f"RESULTADO: OK — todas as {len(checks)} verificações passaram (agrupamento correto).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
