#!/usr/bin/env python3
"""
PRESERVA-SCAN — planejador de deduplicação (fase 2).

Lê os manifestos das varreduras e, agrupando por CONTEÚDO (SHA-256), propõe um
plano para liberar espaço mantendo a segurança de preservação. NÃO apaga nada:
produz apenas um plano em CSV/JSON que a equipe revisa e executa manualmente.

Política (padrão, escolhida no projeto):
  - manter ao menos DUAS cópias de cada conteúdo, em DOIS discos diferentes;
  - só é "removível" o que exceder isso (3ª cópia em diante, ou cópia extra no
    mesmo disco);
  - a cópia que FICA ("principal") é a de caminho mais organizado — penaliza
    pastas soltas (Downloads, Área de Trabalho, temp, "nova pasta", cópia…),
    caminhos muito profundos e nomes de arquivo com marca de duplicata.

Conteúdo que hoje só existe em UM disco não tem redundância de 2 discos: o plano
mantém 1 cópia, marca as cópias extras (no mesmo disco) como removíveis e SINALIZA
o conteúdo como "risco: 1 disco" — candidato à etapa de espelhamento.

Uso:
    python dedup.py --manifestos ./manifestos --saida ./dedup
    python dedup.py --manifestos ./manifestos --saida ./dedup --copias 2 --discos 2
"""
import os
import csv
import json
import argparse
from pathlib import Path

try:
    from relatorio import _manifestos, _linhas, humano  # reaproveita utilitários
except Exception:  # execução fora da pasta scanner/
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from relatorio import _manifestos, _linhas, humano

# Marcas de "lugar solto / não-oficial" no caminho — cópia ali é pior candidata a ficar.
_LIXO_CAMINHO = (
    "download", "downloads", "área de trabalho", "area de trabalho", "desktop",
    "temp", "tmp", "temporario", "temporário", "cache", "lixeira", "recycle",
    "$recycle", "nova pasta", "new folder", "sem título", "sem titulo",
    "untitled", "conflicted", "conflito", "backup", "bkp", "old", "antigo",
    "antigos", "novo", "nova", "copia", "cópia", "copy", "duplicad",
)
# Marcas no NOME do arquivo que indicam duplicata ("- Cópia", "(1)", "copy 2"…).
_LIXO_NOME = ("- cópia", "- copia", "- copy", "copy", "cópia", "copia",
              "(1)", "(2)", "(3)", "(cópia", "(copia", "_copy", "_copia", "_cópia")


def _chave_manter(copia):
    """Ordena cópias do MESMO conteúdo: menor tupla = melhor candidata a FICAR.
    Critérios, em ordem: menos marcas de 'lugar solto' no caminho, menos marcas de
    duplicata no nome, caminho mais raso, caminho mais curto e, por fim, ordem
    alfabética (determinística/auditável)."""
    caminho = (copia.get("caminho") or "")
    cl = caminho.lower()
    nome = (copia.get("nome") or "").lower()
    lixo_cam = sum(1 for t in _LIXO_CAMINHO if t in cl)
    lixo_nome = sum(1 for t in _LIXO_NOME if t in nome)
    profundidade = cl.replace("\\", "/").count("/")
    return (lixo_cam, lixo_nome, profundidade, len(caminho),
            copia.get("disco_label") or "", caminho)


def _planejar_grupo(copias, min_copias, min_discos):
    """Dada a lista de cópias de UM conteúdo, decide quais FICAM e quais podem SAIR.
    Devolve (mantidos, removiveis, n_discos, risco_um_disco)."""
    discos = {}
    for c in copias:
        discos.setdefault(c.get("disco_label") or "?", []).append(c)
    for lst in discos.values():
        lst.sort(key=_chave_manter)

    # Ordena os discos pela qualidade da MELHOR cópia de cada um.
    discos_ord = sorted(discos.items(), key=lambda kv: _chave_manter(kv[1][0]))
    n_discos = len(discos_ord)

    mantidos = []
    if n_discos >= min_discos:
        # Mantém a melhor cópia de cada um dos `min_discos` melhores discos.
        for _disco, lst in discos_ord[:min_discos]:
            mantidos.append(lst[0])
        risco = False
    else:
        # Só existe em 1 disco (ou menos que o mínimo): mantém 1 cópia; sem
        # redundância entre discos — sinaliza para a etapa de espelhamento.
        mantidos.append(discos_ord[0][1][0])
        risco = True

    ids_mantidos = {id(c) for c in mantidos}
    removiveis = [c for c in copias if id(c) not in ids_mantidos]
    return mantidos, removiveis, n_discos, risco


def planejar(manifest_dir, saida_dir, min_copias=2, min_discos=2):
    manifest_dir, saida = Path(manifest_dir), Path(saida_dir)
    saida.mkdir(parents=True, exist_ok=True)

    # Agrupa cópias por SHA-256 (memória ~ nº de arquivos com hash).
    por_sha = {}
    total_arq = 0
    for mf in _manifestos(manifest_dir):
        for r in _linhas(mf):
            sha = r.get("sha256")
            if not sha:
                continue
            total_arq += 1
            por_sha.setdefault(sha, []).append({
                "sha256": sha,
                "disco_label": r.get("disco_label"),
                "caminho": r.get("caminho"),
                "nome": r.get("nome"),
                "tamanho_bytes": int(r.get("tamanho_bytes") or 0),
            })

    plano_csv = saida / "plano_dedup.csv"
    manter_csv = saida / "plano_dedup_manter.csv"
    grupos = removiveis_tot = bytes_recuperaveis = 0
    grupos_risco = arq_risco = bytes_risco = 0

    with open(plano_csv, "w", newline="", encoding="utf-8") as fp, \
         open(manter_csv, "w", newline="", encoding="utf-8") as fm:
        wp = csv.writer(fp)
        wp.writerow(["sha256", "disco_label", "caminho", "tamanho_bytes", "motivo",
                     "manter_disco", "manter_caminho"])
        wm = csv.writer(fm)
        wm.writerow(["sha256", "disco_label", "caminho", "tamanho_bytes",
                     "copias_no_acervo", "discos", "risco_1_disco"])

        for sha, copias in por_sha.items():
            if len(copias) < 2:
                continue  # conteúdo único: nada a deduplicar
            grupos += 1
            mantidos, removiveis, n_discos, risco = _planejar_grupo(
                copias, min_copias, min_discos)
            principal = mantidos[0]
            for c in mantidos:
                wm.writerow([sha, c["disco_label"], c["caminho"], c["tamanho_bytes"],
                             len(copias), n_discos, "sim" if risco else "não"])
            if risco:
                grupos_risco += 1
                arq_risco += len(mantidos)
                bytes_risco += sum(c["tamanho_bytes"] for c in mantidos)
            for c in removiveis:
                mesmo_disco = c["disco_label"] == principal["disco_label"]
                if risco:
                    motivo = "cópia extra no mesmo disco (só 1 disco — espelhar depois)"
                elif mesmo_disco:
                    motivo = "cópia extra no mesmo disco"
                else:
                    motivo = f"cópia além das {min_copias} em {min_discos} discos"
                wp.writerow([sha, c["disco_label"], c["caminho"], c["tamanho_bytes"],
                             motivo, principal["disco_label"], principal["caminho"]])
                removiveis_tot += 1
                bytes_recuperaveis += c["tamanho_bytes"]

    resumo = {
        "politica": {"min_copias": min_copias, "min_discos": min_discos,
                     "principal": "caminho mais organizado"},
        "arquivos_no_acervo": total_arq,
        "conteudos_unicos": len(por_sha),
        "grupos_com_duplicata": grupos,
        "arquivos_removiveis": removiveis_tot,
        "bytes_recuperaveis": bytes_recuperaveis,
        "espaco_recuperavel_humano": humano(bytes_recuperaveis),
        "risco_1_disco": {
            "grupos": grupos_risco,
            "arquivos_sem_redundancia_entre_discos": arq_risco,
            "bytes": bytes_risco,
            "volume_humano": humano(bytes_risco),
            "observacao": ("conteúdos que hoje só existem em 1 disco — mantidos, "
                           "mas precisam ser espelhados num 2º disco para ficarem seguros"),
        },
    }
    (saida / "plano_dedup_resumo.json").write_text(
        json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Plano de deduplicação gerado em: {saida}")
    print(f"  Grupos com duplicata: {grupos}")
    print(f"  Arquivos removíveis:  {removiveis_tot}")
    print(f"  Espaço recuperável:   {humano(bytes_recuperaveis)}")
    if grupos_risco:
        print(f"  ATENÇÃO — {grupos_risco} conteúdo(s) só em 1 disco "
              f"({humano(bytes_risco)}): manter e espelhar.")
    return resumo


def main():
    ap = argparse.ArgumentParser(description="Planejador de deduplicação do PRESERVA-SCAN")
    ap.add_argument("--manifestos", default="./manifestos")
    ap.add_argument("--saida", default="./dedup")
    ap.add_argument("--copias", type=int, default=2, help="cópias a manter (padrão 2)")
    ap.add_argument("--discos", type=int, default=2, help="discos distintos a manter (padrão 2)")
    a = ap.parse_args()
    planejar(a.manifestos, a.saida, min_copias=a.copias, min_discos=a.discos)


if __name__ == "__main__":
    main()
