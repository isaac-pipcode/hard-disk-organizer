#!/usr/bin/env python3
"""
PRESERVA-SCAN — execução guiada da limpeza (fase 2, item 4).

Executa o plano de deduplicação (plano_dedup.csv) de forma SEGURA. É a única parte
do sistema que mexe nos arquivos — por isso, invariantes rígidas:

  1. NUNCA apaga de forma definitiva. Move a cópia redundante para uma pasta de
     QUARENTENA (_QUARENTENA_PRESERVASCAN) no próprio disco, preservando o caminho.
     A remoção definitiva é a equipe esvaziar a quarentena depois, conferindo.
  2. NUNCA remove sem confirmar que uma cópia idêntica SOBREVIVE: antes de mover,
     re-calcula o SHA-256 da cópia que fica (keeper) e confirma que existe e bate.
  3. Confere o próprio arquivo a remover: se mudou desde o plano (hash diferente),
     não mexe.
  4. Modo SIMULAÇÃO por padrão (verificar): não toca em nada; só diz o que faria.
  5. Registra tudo num log de auditoria (JSONL) e permite DESFAZER (restaurar).

`discos_montados` mapeia o rótulo do disco (disco_label do manifesto) para a raiz
onde ele está montado agora, ex.: {"HD EDICAO O": "F:\\"}.

Uso:
    python executar.py --plano ./dedup/plano_dedup.csv --disco "HD EDICAO O=F:\\"        # simula
    python executar.py --plano ./dedup/plano_dedup.csv --disco "HD EDICAO O=F:\\" --executar
    python executar.py --disco "HD EDICAO O=F:\\" --restaurar     # desfaz (tira da quarentena)
"""
import os
import csv
import json
import hashlib
import datetime
import argparse
from pathlib import Path

QUARENTENA = "_QUARENTENA_PRESERVASCAN"
CHUNK = 1024 * 1024


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(CHUNK), b""):
            h.update(bloco)
    return h.hexdigest()


def _rel_os(caminho: str) -> str:
    """Normaliza o separador do manifesto (Windows usa '\\') para o SO atual."""
    return caminho.replace("\\", os.sep).replace("/", os.sep).lstrip(os.sep)


def _resolver(disco_label, caminho, discos_montados):
    raiz = discos_montados.get(disco_label)
    if not raiz:
        return None
    return Path(raiz) / _rel_os(caminho)


def _ler_plano(plano_csv):
    with open(plano_csv, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def verificar(plano_csv, discos_montados, saida_dir=None):
    """Dry-run: classifica cada remoção proposta SEM tocar em nada. Devolve resumo e,
    se saida_dir, grava verificacao.csv. Status possíveis por linha:
      ok_remover · disco_nao_montado · ja_ausente · keeper_disco_nao_montado ·
      keeper_ausente · keeper_hash_diferente · removivel_mudou · e_o_proprio_keeper"""
    linhas = _ler_plano(plano_csv)
    keeper_cache = {}          # caminho_keeper(str) -> sha calculado (evita re-hash)
    resultados = []
    contagem = {}
    bytes_ok = 0
    for r in linhas:
        sha = r.get("sha256")
        rem = _resolver(r.get("disco_label"), r.get("caminho"), discos_montados)
        keep = _resolver(r.get("manter_disco"), r.get("manter_caminho"), discos_montados)
        tam = int(r.get("tamanho_bytes") or 0)

        def reg(status):
            contagem[status] = contagem.get(status, 0) + 1
            resultados.append({"status": status, "sha256": sha, "tamanho_bytes": tam,
                               "remover": str(rem) if rem else "",
                               "manter": str(keep) if keep else "",
                               "disco_label": r.get("disco_label"),
                               "caminho": r.get("caminho")})
            return status

        if rem is None:
            reg("disco_nao_montado"); continue
        if keep is not None and rem.resolve() == keep.resolve():
            reg("e_o_proprio_keeper"); continue
        if not rem.exists():
            reg("ja_ausente"); continue
        if keep is None:
            reg("keeper_disco_nao_montado"); continue
        if not keep.exists():
            reg("keeper_ausente"); continue          # NUNCA remove: keeper sumiu
        # hash do keeper (com cache) — a cópia que fica precisa existir e bater
        ks = keeper_cache.get(str(keep))
        if ks is None:
            try:
                ks = _sha256(keep)
            except OSError:
                reg("keeper_ilegivel"); continue
            keeper_cache[str(keep)] = ks
        if ks != sha:
            reg("keeper_hash_diferente"); continue   # NUNCA remove: keeper não confere
        # hash do removível — não pode ter mudado desde o plano
        try:
            rs = _sha256(rem)
        except OSError:
            reg("removivel_ilegivel"); continue
        if rs != sha:
            reg("removivel_mudou"); continue
        bytes_ok += tam
        reg("ok_remover")

    resumo = {"total_no_plano": len(linhas), "ok_remover": contagem.get("ok_remover", 0),
              "bytes_a_liberar": bytes_ok, "por_status": contagem}
    if saida_dir:
        saida = Path(saida_dir); saida.mkdir(parents=True, exist_ok=True)
        with open(saida / "verificacao.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["status", "sha256", "tamanho_bytes",
                                              "remover", "manter", "disco_label", "caminho"])
            w.writeheader()
            for x in resultados:
                w.writerow(x)
        (saida / "verificacao_resumo.json").write_text(
            json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")
    resumo["_resultados"] = resultados
    return resumo


def executar(plano_csv, discos_montados, saida_dir=None, confirmar=False):
    """Move para a QUARENTENA as cópias verificadas como 'ok_remover'. Requer
    confirmar=True. Não apaga nada de forma definitiva; grava log de auditoria."""
    if not confirmar:
        raise ValueError("executar() exige confirmar=True (proteção contra remoção acidental).")
    v = verificar(plano_csv, discos_montados, saida_dir)
    movidos = falhas = 0
    bytes_movidos = 0
    carimbo = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    logs_por_raiz = {}
    for x in v["_resultados"]:
        if x["status"] != "ok_remover":
            continue
        origem = Path(x["remover"])
        raiz = Path(discos_montados[x["disco_label"]])
        destino = raiz / QUARENTENA / _rel_os(x["caminho"])
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            os.replace(origem, destino)     # move no mesmo disco (rápido, reversível)
            movidos += 1
            bytes_movidos += x["tamanho_bytes"]
            logs_por_raiz.setdefault(str(raiz), []).append(
                {"quando": carimbo, "acao": "quarentena", "sha256": x["sha256"],
                 "de": str(origem), "para": str(destino), "keeper": x["manter"]})
        except OSError as e:
            falhas += 1
            logs_por_raiz.setdefault(str(raiz), []).append(
                {"quando": carimbo, "acao": "falha", "erro": str(e), "de": str(origem)})
    # grava auditoria em cada disco (append)
    for raiz, entradas in logs_por_raiz.items():
        alog = Path(raiz) / QUARENTENA / "_auditoria.jsonl"
        alog.parent.mkdir(parents=True, exist_ok=True)
        with open(alog, "a", encoding="utf-8") as f:
            for e in entradas:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return {"movidos_para_quarentena": movidos, "falhas": falhas,
            "bytes_movidos": bytes_movidos, "verificacao": v["por_status"]}


def restaurar(discos_montados):
    """DESFAZ: devolve tudo da quarentena de cada disco para o lugar original."""
    restaurados = falhas = 0
    for _label, raiz in discos_montados.items():
        qroot = Path(raiz) / QUARENTENA
        if not qroot.is_dir():
            continue
        for atual in qroot.rglob("*"):
            if atual.is_dir() or atual.name == "_auditoria.jsonl":
                continue
            rel = atual.relative_to(qroot)
            destino = Path(raiz) / rel
            try:
                destino.parent.mkdir(parents=True, exist_ok=True)
                os.replace(atual, destino)
                restaurados += 1
            except OSError:
                falhas += 1
    return {"restaurados": restaurados, "falhas": falhas}


def _parse_discos(pares):
    d = {}
    for p in pares or []:
        if "=" in p:
            k, v = p.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def main():
    ap = argparse.ArgumentParser(description="Execução guiada da limpeza do PRESERVA-SCAN")
    ap.add_argument("--plano", help="caminho do plano_dedup.csv")
    ap.add_argument("--disco", action="append", metavar='ROTULO=RAIZ',
                    help='mapeia disco montado, ex.: --disco "HD EDICAO O=F:\\"')
    ap.add_argument("--saida", default="./execucao")
    ap.add_argument("--executar", action="store_true", help="MOVE para a quarentena (senão, só simula)")
    ap.add_argument("--restaurar", action="store_true", help="desfaz: tira tudo da quarentena")
    a = ap.parse_args()
    discos = _parse_discos(a.disco)
    if a.restaurar:
        print("Restaurando da quarentena...", restaurar(discos)); return
    if not a.plano:
        ap.error("--plano é obrigatório (a menos de --restaurar)")
    if a.executar:
        print("EXECUTANDO (movendo para quarentena)...")
        print(executar(a.plano, discos, a.saida, confirmar=True))
    else:
        v = verificar(a.plano, discos, a.saida)
        print(f"SIMULAÇÃO — verificação (nada foi movido):")
        print(f"  a remover com segurança: {v['ok_remover']} arquivos "
              f"({v['bytes_a_liberar']} bytes)")
        print(f"  por status: {v['por_status']}")
        print("  Rode com --executar para mover essas cópias para a quarentena.")


if __name__ == "__main__":
    main()
