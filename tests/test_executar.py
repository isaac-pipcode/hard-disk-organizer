#!/usr/bin/env python3
"""Testes de SEGURANÇA da execução guiada (scanner/executar.py).

O foco é provar as invariantes: nunca move sem um keeper idêntico verificado, nunca
apaga (só quarentena, reversível), e a simulação não toca em nada.
"""
import csv
import hashlib
import json
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import executar as ex  # noqa: E402


def _sha(b): return hashlib.sha256(b).hexdigest()


def _montar(tmp: Path):
    """Cria um disco de teste com arquivos reais e um plano_dedup.csv apontando p/ eles."""
    disc = tmp / "disc"; disc.mkdir()
    def w(rel, data):
        p = disc / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data); return p
    A, B, C, E, F = b"AAA-conteudo", b"BBB", b"CCC", b"EEE", b"FFF"
    # Row1 ok: keeper e removivel com o MESMO conteudo A
    w("PROJ/a.txt", A); w("Downloads/a copy.txt", A)
    # Row2 keeper ausente: removivel existe, keeper NAO
    w("x/b.txt", B)
    # Row3 keeper hash diferente: keeper existe com conteudo ERRADO
    w("y/c.txt", C); w("M/c_wrong.txt", b"DIFERENTE")
    # Row4 removivel mudou: removivel tem conteudo diferente do sha do plano; keeper ok
    w("z/e.txt", b"MUDOU-no-disco"); w("M/e.txt", E)
    # Row5 ja ausente: removivel nao existe; keeper existe
    w("M/f.txt", F)
    rows = [
        {"sha256": _sha(A), "disco_label": "HD1", "caminho": "Downloads/a copy.txt", "tamanho_bytes": len(A),
         "motivo": "x", "manter_disco": "HD1", "manter_caminho": "PROJ/a.txt"},                    # ok
        {"sha256": _sha(B), "disco_label": "HD1", "caminho": "x/b.txt", "tamanho_bytes": len(B),
         "motivo": "x", "manter_disco": "HD1", "manter_caminho": "M/b_ausente.txt"},               # keeper_ausente
        {"sha256": _sha(C), "disco_label": "HD1", "caminho": "y/c.txt", "tamanho_bytes": len(C),
         "motivo": "x", "manter_disco": "HD1", "manter_caminho": "M/c_wrong.txt"},                 # keeper_hash_diferente
        {"sha256": _sha(E), "disco_label": "HD1", "caminho": "z/e.txt", "tamanho_bytes": len(E),
         "motivo": "x", "manter_disco": "HD1", "manter_caminho": "M/e.txt"},                       # removivel_mudou
        {"sha256": _sha(F), "disco_label": "HD1", "caminho": "gone/f.txt", "tamanho_bytes": len(F),
         "motivo": "x", "manter_disco": "HD1", "manter_caminho": "M/f.txt"},                       # ja_ausente
        {"sha256": _sha(A), "disco_label": "HD1", "caminho": "PROJ/a.txt", "tamanho_bytes": len(A),
         "motivo": "x", "manter_disco": "HD1", "manter_caminho": "PROJ/a.txt"},                    # e_o_proprio_keeper
        {"sha256": _sha(B), "disco_label": "HD2", "caminho": "algum/b.txt", "tamanho_bytes": len(B),
         "motivo": "x", "manter_disco": "HD1", "manter_caminho": "M/f.txt"},                       # disco_nao_montado
    ]
    plano = tmp / "plano_dedup.csv"
    with open(plano, "w", newline="", encoding="utf-8") as f:
        w2 = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w2.writeheader()
        for r in rows: w2.writerow(r)
    return disc, plano, {"HD1": str(disc)}


def verificar_tudo():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        disc, plano, montados = _montar(tmp)

        # --- SIMULAÇÃO ---
        v = ex.verificar(plano, montados, tmp / "exec")
        st = {r["caminho"]: r["status"] for r in v["_resultados"]}
        ck("row1 ok_remover", st["Downloads/a copy.txt"], "ok_remover")
        ck("row2 keeper_ausente", st["x/b.txt"], "keeper_ausente")
        ck("row3 keeper_hash_diferente", st["y/c.txt"], "keeper_hash_diferente")
        ck("row4 removivel_mudou", st["z/e.txt"], "removivel_mudou")
        ck("row5 ja_ausente", st["gone/f.txt"], "ja_ausente")
        ck("row6 e_o_proprio_keeper", st["PROJ/a.txt"], "e_o_proprio_keeper")
        ck("row7 disco_nao_montado", st["algum/b.txt"], "disco_nao_montado")
        ck("só 1 é seguro remover", v["ok_remover"], 1)
        # simulação não move nada
        ck("simulação preserva removível", (disc / "Downloads/a copy.txt").exists(), True)

        # --- EXECUÇÃO exige confirmação ---
        try:
            ex.executar(plano, montados, tmp / "exec", confirmar=False)
            ck("executar sem confirmar levanta erro", False, True)
        except ValueError:
            ck("executar sem confirmar levanta erro", True, True)

        # --- EXECUÇÃO real (quarentena) ---
        res = ex.executar(plano, montados, tmp / "exec", confirmar=True)
        ck("moveu exatamente 1", res["movidos_para_quarentena"], 1)
        ck("removível saiu do lugar", (disc / "Downloads/a copy.txt").exists(), False)
        q = disc / ex.QUARENTENA / "Downloads/a copy.txt"
        ck("removível está na quarentena", q.exists(), True)
        ck("keeper permanece intacto", (disc / "PROJ/a.txt").read_bytes(), b"AAA-conteudo")
        # os que não eram seguros continuam onde estavam
        ck("keeper_ausente: removível preservado", (disc / "x/b.txt").exists(), True)
        ck("keeper_hash_dif: removível preservado", (disc / "y/c.txt").exists(), True)
        ck("removivel_mudou: preservado", (disc / "z/e.txt").exists(), True)
        # auditoria gravada
        alog = disc / ex.QUARENTENA / "_auditoria.jsonl"
        ck("log de auditoria existe", alog.exists(), True)
        entradas = [json.loads(l) for l in alog.read_text(encoding="utf-8").splitlines() if l.strip()]
        ck("auditoria registra 1 quarentena", sum(1 for e in entradas if e.get("acao") == "quarentena"), 1)

        # --- DESFAZER ---
        r = ex.restaurar(montados)
        ck("restaurou 1", r["restaurados"], 1)
        ck("removível voltou ao lugar", (disc / "Downloads/a copy.txt").read_bytes(), b"AAA-conteudo")

    return checks, falhas


def test_execucao_segura():
    _, falhas = verificar_tudo()
    assert not falhas, f"Falhas de segurança: {falhas}"


def main():
    checks, falhas = verificar_tudo()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    if falhas:
        print(f"RESULTADO: FALHOU — {falhas}"); return 1
    print(f"RESULTADO: OK — todas as {len(checks)} verificações passaram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
