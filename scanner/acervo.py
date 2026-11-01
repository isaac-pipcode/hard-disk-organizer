"""
Acervo: o estado de cada HD, montado SÓ com o que está neste computador —
manifestos, fichas físicas (disco_<etiqueta>.json) e registros de operação —,
sem banco de dados e sem internet. É a tela inicial do painel.

Situação de cada HD:
  varrendo     a operação em andamento é neste HD
  incompleta   a última varredura falhou ou foi interrompida (ou há arquivos
               sem impressão digital): continuar de onde parou
  completar    varrido, mas falta formato (Siegfried) ou dados de mídia
               (MediaInfo/ExifTool) em parte dos arquivos: "Completar metadados"
  a_varrer     conhecido (tem ficha), mas ainda sem inventário
  concluido    inventário completo
  novo         HD ligado agora que ainda não está no catálogo
"""
import json
from pathlib import Path

import hardware
import nomes
from scan import EXT_VIDEO_AUDIO, EXT_IMAGEM

_CACHE = {}   # caminho do manifesto -> ((mtime, tamanho), resumo)

GRUPOS = {"antigos": "Antigo", "uso_continuo": "Uso contínuo", "transporte": "Transporte"}

SECOES = {   # na ordem em que aparecem (prioridade)
    "varrendo": "Em andamento",
    "incompleta": "Precisam de você",
    "nao_reconhecido": "Não reconhecidos",
    "novo": "Ligados agora, fora do catálogo",
    "completar": "Completar dados",
    "enviar": "Prontos para enviar",
    "a_varrer": "Na fila para varrer",
    "concluido": "Concluídos",
}
ORDEM = list(SECOES)

# Sintomas de um HD que o computador não reconhece (tela "Registrar HD").
SINTOMAS = {
    "nao_aparece": "Não aparece no Windows",
    "pede_formatar": "Aparece, mas pede para formatar ou não abre",
    "barulho": "Faz barulho (estalos, bipes) ou não gira",
    "outro": "Outro problema",
}


def humano(n, base=1000):
    """Tamanho legível. Base decimal por padrão: é a da etiqueta do fabricante
    e da planilha da equipe (um HD de 4 TB aparece como 4 TB, não 3,6 TB)."""
    if not n:
        return ""
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < base or u == "PB":
            txt = f"{n:.1f}".replace(".", ",")
            return (txt[:-2] if txt.endswith(",0") else txt) + f" {u}"
        n /= base


def data_curta(iso):
    """'2026-10-30T14:05:00' -> '30/10/2026' (também usado como filtro nos templates)."""
    if not iso:
        return ""
    try:
        y, m, d = iso[:10].split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return iso


def _milhar(n):
    return f"{int(n):,}".replace(",", ".")


def resumo_manifesto(arq):
    """Conta o que interessa ao Acervo num manifesto JSONL. Cada caminho conta
    uma vez (o último registro vale — retomadas podem repetir arquivos)."""
    arq = Path(arq)
    st = arq.stat()
    chave = (st.st_mtime, st.st_size)
    c = _CACHE.get(str(arq))
    if c and c[0] == chave:
        return c[1]
    ultimo = {}
    with open(arq, encoding="utf-8", errors="replace") as f:
        for linha in f:
            if not linha.strip():
                continue
            try:
                d = json.loads(linha)
            except Exception:
                continue
            ext = (d.get("extensao") or "").lower()
            tem_mi = d.get("mediainfo") not in (None, {}, "")
            ultimo[d.get("caminho")] = (
                int(d.get("tamanho_bytes") or 0),
                bool(d.get("sha256")),
                bool(d.get("puid") or d.get("formato")),
                "av" if ext in EXT_VIDEO_AUDIO else ("img" if ext in EXT_IMAGEM else ""),
                tem_mi,
            )
    r = {"arquivos": 0, "bytes": 0, "sem_hash": 0, "sem_formato": 0,
         "av": 0, "av_sem": 0, "img": 0, "img_sem": 0}
    for tam, tem_hash, tem_fmt, midia, tem_mi in ultimo.values():
        r["arquivos"] += 1
        r["bytes"] += tam
        r["sem_hash"] += not tem_hash
        r["sem_formato"] += not tem_fmt
        if midia:
            r[midia] += 1
            r[midia + "_sem"] += not tem_mi
    _CACHE[str(arq)] = (chave, r)
    return r


def _status_por_log(pasta, etiqueta):
    """Para inventários de antes da ficha: lê o registro da última VARREDURA
    deste HD. 'concluida', 'falhou', 'interrompida' ou None (sem registro)."""
    for log in reversed(nomes.registros(pasta, etiqueta)):
        try:
            txt = log.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not txt.startswith("=== varredura"):
            continue                      # registro de "Completar metadados"
        fim = txt[-6000:]
        if "OK — disco" in fim:
            return "concluida"
        if "ERRO:" in fim:
            return "falhou"
        return "interrompida"
    return None


def _cartao_base(etiqueta, ficha):
    tamanho = ficha.get("tamanho") or ficha.get("total")
    livre, total = ficha.get("livre"), ficha.get("total") or ficha.get("tamanho")
    pct = round(100 * int(livre) / int(total), 1) if livre and total else None
    return {
        "etiqueta": etiqueta,
        "tipo": ficha.get("tipo") or "desconhecido",
        "rotulo_tipo": ficha.get("rotulo_tipo") or "",
        "formato": ficha.get("formato") or "",
        "conexao": ficha.get("conexao") or "",
        "marca": ficha.get("marca") or "",
        "linha": ficha.get("linha") or "",
        "modelo": ficha.get("modelo") or "",
        "tem_ficha_fisica": bool(ficha.get("modelo") or ficha.get("serial") or ficha.get("tipo")),
        "grupo": GRUPOS.get(ficha.get("grupo") or "", ""),
        "grupo_id": ficha.get("grupo") or "",
        "capacidade": humano(tamanho) or ficha.get("capacidade_nominal") or "",
        "livre_pct": pct,
        "livre": humano(livre) if livre else "",
        "total": humano(total) if total else "",
        "arquivos": "", "volume": "", "conectado_em": "",
        "progresso": None, "detalhe": "", "nao_enviado": False,
    }


def montar(pasta, job=None, conectados=None, envio=None):
    """Monta os cartões do Acervo. `job` = {'disco', 'pct', 'fase'} da operação em
    andamento (ou None); `conectados` = discos ligados agora (panel.discos_disponiveis);
    `envio` = pacote.situacao_envio() — o que ainda não foi empacotado para envio."""
    envio = envio or {}
    pasta = Path(pasta)
    fichas = hardware.fichas(pasta) if pasta.exists() else {}
    manifestos = nomes.manifestos(pasta)
    ligados = {}
    novos = []
    for d in conectados or []:
        if d.get("reconhecido"):
            ligados[d["reconhecido"]] = d
        elif not d.get("sistema") and d.get("rotulo") in manifestos:
            ligados[d["rotulo"]] = d            # inventário sem ficha: casa pelo rótulo
        elif not d.get("sistema"):
            novos.append(d)

    cartoes = []
    for etiqueta in sorted(set(manifestos) | set(fichas), key=str.lower):
        ficha = fichas.get(etiqueta, {})
        c = _cartao_base(etiqueta, ficha)
        r = resumo_manifesto(manifestos[etiqueta]) if etiqueta in manifestos else None
        if r:
            c["arquivos"] = _milhar(r["arquivos"])
            c["volume"] = humano(r["bytes"])
        d = ligados.get(etiqueta)
        if d:
            c["conectado_em"] = d.get("letra") or "ligado"
            if d.get("livre_pct") is not None:
                c["livre_pct"] = d["livre_pct"]
                c["livre"] = humano(d.get("livre_bytes")) or d.get("livre", "")
                c["total"] = humano(d.get("total_bytes")) or c["total"] or d.get("tamanho", "")
            if not c["tem_ficha_fisica"] and d.get("hw"):
                h = d["hw"]
                c.update(tipo=h.get("tipo") or c["tipo"], rotulo_tipo=h.get("rotulo_tipo", ""),
                         formato=h.get("formato", ""), conexao=h.get("conexao", ""),
                         marca=h.get("marca", ""), linha=h.get("linha", ""),
                         tem_ficha_fisica=True)

        estado = (ficha.get("varredura") or {}).get("status") or _status_por_log(pasta, etiqueta)
        em_job = bool(job and job.get("disco") == etiqueta)
        nr = hardware.nao_reconhecido(ficha)
        env = envio.get(etiqueta) if r else None
        if em_job:
            c["situacao"] = "varrendo"
            c["titulo"] = job.get("fase") or "Varrendo"
            c["progresso"] = job.get("pct")
        elif nr:
            c["situacao"] = "nao_reconhecido"
            c["titulo"] = "Não reconhecido"
            c["detalhe"] = SINTOMAS.get(nr.get("sintoma"), "")
        elif estado in ("falhou", "em_andamento", "interrompida") or (r and r["sem_hash"]):
            c["situacao"] = "incompleta"
            c["titulo"] = "Varredura incompleta"
            c["detalhe"] = ("Parou com erro — continua de onde parou" if estado == "falhou"
                            else "Parou no meio — continua de onde parou")
            if r and r["sem_hash"] and estado not in ("falhou", "em_andamento", "interrompida"):
                c["detalhe"] = f"{_milhar(r['sem_hash'])} arquivos sem impressão digital"
        elif r is None:
            c["situacao"] = "a_varrer"
            c["titulo"] = "Na fila para varrer"
            c["detalhe"] = "Ainda sem inventário"
        elif r["sem_formato"] or r["av_sem"] or r["img_sem"]:
            c["situacao"] = "completar"
            c["titulo"] = "Completar dados"
            partes = []
            if r["sem_formato"]:
                partes.append("sem formato" if r["sem_formato"] == r["arquivos"]
                              else f"{_milhar(r['sem_formato'])} sem formato")
            if r["av_sem"] or r["img_sem"]:
                partes.append(f"{_milhar(r['av_sem'] + r['img_sem'])} sem dados de mídia")
            c["detalhe"] = " · ".join(partes)
        elif env and env["pendente"]:
            c["situacao"] = "enviar"
            c["titulo"] = "Pronto para enviar"
            c["detalhe"] = "Mudou desde o último pacote" if env.get("em") else "Concluído · ainda não enviado"
        else:
            c["situacao"] = "concluido"
            c["titulo"] = "Concluído e completo"
            if env and env.get("em"):
                c["detalhe"] = "Incluído no pacote de " + data_curta(env["em"])
        c["nao_enviado"] = bool(env and env["pendente"]) and c["situacao"] != "enviar"
        cartoes.append(c)

    for d in novos:
        h = d.get("hw") or {}
        c = _cartao_base("", {k: h.get(k) for k in ("tipo", "rotulo_tipo", "formato", "conexao",
                                                     "marca", "linha", "modelo")})
        c.update(situacao="novo", titulo="Novo no catálogo",
                 detalhe=f"Rótulo no Windows: “{d.get('rotulo')}”" if d.get("rotulo") else "",
                 conectado_em=d.get("letra") or "ligado", rotulo=d.get("rotulo") or "",
                 livre_pct=d.get("livre_pct"), livre=humano(d.get("livre_bytes")) or d.get("livre", ""),
                 total=humano(d.get("total_bytes")) or d.get("tamanho", ""),
                 tem_ficha_fisica=bool(h))
        cartoes.append(c)

    secoes = []
    for s in ORDEM:
        itens = [c for c in cartoes if c["situacao"] == s]
        if itens:
            secoes.append({"id": s, "titulo": SECOES[s], "cartoes": itens})
    catalogo = [c for c in cartoes if c["situacao"] != "novo"]
    total_arquivos = sum(int((c["arquivos"] or "0").replace(".", "")) for c in catalogo)
    return {
        "secoes": secoes,
        "kpi": {
            "hds": len(catalogo),
            "varridos": sum(1 for c in catalogo if c["arquivos"]),
            "atencao": sum(1 for c in catalogo if c["situacao"] in ("incompleta", "completar", "nao_reconhecido")),
            "concluidos": sum(1 for c in catalogo if c["situacao"] in ("concluido", "enviar")),
            "pouco_espaco": sum(1 for c in cartoes if c["livre_pct"] is not None and c["livre_pct"] < 10),
            "para_enviar": sum(1 for e, v in envio.items() if v.get("pendente")),
            "arquivos": _milhar(total_arquivos),
        },
        "novos": len([c for c in cartoes if c["situacao"] == "novo"]),
    }
