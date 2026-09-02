#!/usr/bin/env python3
"""Teste do planejador de deduplicação (scanner/dedup.py).

Verifica a política "manter 2 cópias em 2 discos" e a escolha da cópia principal
pelo caminho mais organizado, incluindo o caso de conteúdo em 1 só disco (risco).
"""
import csv
import json
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import dedup  # noqa: E402

# Conteúdos:
#  X (2 discos, 3 cópias): mantém 2 (uma por disco), remove 1 → dedup normal.
#  Y (1 disco, 2 cópias): mantém 1, remove 1, marca RISCO (1 disco).
#  Z (3 discos, 3 cópias): mantém 2 (2 discos), remove a 3ª (3º disco).
#  U (único): não entra.
X, Y, Z, U = "x" * 64, "y" * 64, "z" * 64, "u" * 64

HD_A = [
    {"disco_label": "HD-A", "caminho": "PROJETOS/2024/video.mov", "nome": "video.mov", "sha256": X, "tamanho_bytes": 1000},
    {"disco_label": "HD-A", "caminho": "Downloads/video (1).mov", "nome": "video (1).mov", "sha256": X, "tamanho_bytes": 1000},
    {"disco_label": "HD-A", "caminho": "MIDIAS/foto.jpg", "nome": "foto.jpg", "sha256": Y, "tamanho_bytes": 200},
    {"disco_label": "HD-A", "caminho": "Área de Trabalho/nova pasta/foto - Cópia.jpg", "nome": "foto - Cópia.jpg", "sha256": Y, "tamanho_bytes": 200},
    {"disco_label": "HD-A", "caminho": "ARQUIVO/z.wav", "nome": "z.wav", "sha256": Z, "tamanho_bytes": 500},
    {"disco_label": "HD-A", "caminho": "unico.txt", "nome": "unico.txt", "sha256": U, "tamanho_bytes": 10},
]
HD_B = [
    {"disco_label": "HD-B", "caminho": "backup/temp/video.mov", "nome": "video.mov", "sha256": X, "tamanho_bytes": 1000},
    {"disco_label": "HD-B", "caminho": "ACERVO/z.wav", "nome": "z.wav", "sha256": Z, "tamanho_bytes": 500},
]
HD_C = [
    {"disco_label": "HD-C", "caminho": "temp/z.wav", "nome": "z.wav", "sha256": Z, "tamanho_bytes": 500},
]


def _rodar(tmp: Path):
    man, sai = tmp / "manifestos", tmp / "dedup"
    man.mkdir(parents=True, exist_ok=True)
    for nome, linhas in (("HD-A", HD_A), ("HD-B", HD_B), ("HD-C", HD_C)):
        with open(man / f"manifesto_{nome}.jsonl", "w", encoding="utf-8") as f:
            for r in linhas:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    resumo = dedup.planejar(man, sai, min_copias=2, min_discos=2)
    plano = list(csv.DictReader(open(sai / "plano_dedup.csv", encoding="utf-8")))
    manter = list(csv.DictReader(open(sai / "plano_dedup_manter.csv", encoding="utf-8")))
    return resumo, plano, manter


def verificar():
    with tempfile.TemporaryDirectory() as td:
        resumo, plano, manter = _rodar(Path(td))
    checks, falhas = [], []

    def check(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp:
            falhas.append(nome)

    porsha = {}
    for r in plano:
        porsha.setdefault(r["sha256"], []).append(r)
    manter_sha = {}
    for r in manter:
        manter_sha.setdefault(r["sha256"], []).append(r)

    # X: 2 discos → mantém 2, remove 1 (a cópia de Downloads).
    check("X removíveis", len(porsha.get(X, [])), 1)
    check("X remove a cópia de Downloads",
          porsha[X][0]["caminho"], "Downloads/video (1).mov")
    check("X principal é o caminho organizado",
          porsha[X][0]["manter_caminho"], "PROJETOS/2024/video.mov")
    check("X mantidos = 2", len(manter_sha.get(X, [])), 2)

    # Y: 1 disco, 2 cópias → mantém 1 (a de MIDIAS), remove 1, RISCO.
    check("Y removíveis", len(porsha.get(Y, [])), 1)
    check("Y mantém a cópia limpa (MIDIAS)",
          manter_sha[Y][0]["caminho"], "MIDIAS/foto.jpg")
    check("Y marcado como risco 1 disco", manter_sha[Y][0]["risco_1_disco"], "sim")
    check("Y motivo menciona espelhar", "espelhar" in porsha[Y][0]["motivo"], True)

    # Z: 3 discos → mantém 2, remove 1 (3º disco).
    check("Z removíveis", len(porsha.get(Z, [])), 1)
    check("Z mantidos = 2", len(manter_sha.get(Z, [])), 2)

    # U: único → não entra em lugar nenhum.
    check("U não aparece no plano", U in porsha, False)

    # Resumo agregado
    check("grupos com duplicata", resumo["grupos_com_duplicata"], 3)   # X, Y, Z
    check("arquivos removíveis", resumo["arquivos_removiveis"], 3)      # 1+1+1
    check("bytes recuperáveis", resumo["bytes_recuperaveis"], 1000 + 200 + 500)
    check("grupos em risco (1 disco)", resumo["risco_1_disco"]["grupos"], 1)  # só Y

    return checks, falhas


def test_planejador_dedup():
    _, falhas = verificar()
    assert not falhas, f"Dedup incorreto: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    if falhas:
        print(f"RESULTADO: FALHOU — {falhas}")
        return 1
    print(f"RESULTADO: OK — todas as {len(checks)} verificações passaram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
