"""
Conteúdo do HD: navega pelas pastas e arquivos de um HD a partir do INVENTÁRIO
(manifesto_<etiqueta>.jsonl) — nunca do disco. Por isso funciona:

  * DURANTE a varredura: o scan grava o manifesto linha a linha, e aqui só as
    linhas novas são lidas a cada atualização (o HD antigo não recebe nenhuma
    leitura a mais competindo com a varredura);
  * depois dela, com o HD desligado, e no computador do gestor (pacote importado).

O índice de cada manifesto fica em memória e é atualizado de forma incremental
(pela posição já lida no arquivo). Se o manifesto for reescrito — "Completar
dados" troca o arquivo inteiro —, o índice é refeito do zero.
"""
import json
import threading

import nomes
from relatorio import resumo_midia
from scan import EXT_IMAGEM, EXT_VIDEO_AUDIO

EXT_AUDIO = {"wav", "aif", "aiff", "mp3", "flac", "m4a", "aac", "bwf", "ac3", "ogg"}
EXT_DOCUMENTO = {"pdf", "doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "ods", "csv",
                 "ppt", "pptx", "odp", "md", "pages", "key", "numbers"}
TIPOS = {"video": "Vídeo", "audio": "Áudio", "imagem": "Imagem", "documento": "Documento",
         "outro": "Outros"}
POR_PAGINA = 200


def tipo_de(ext):
    ext = (ext or "").lower()
    if ext in EXT_AUDIO:
        return "audio"
    if ext in EXT_VIDEO_AUDIO:
        return "video"
    if ext in EXT_IMAGEM:
        return "imagem"
    if ext in EXT_DOCUMENTO:
        return "documento"
    return "outro"


def _arquivo(d, seq):
    """O que a tela precisa de uma linha do manifesto (sem o bloco de mídia bruto)."""
    caminho = str(d.get("caminho") or "").replace("\\", "/").strip("/")
    pasta, _, nome = caminho.rpartition("/")
    ext = (d.get("extensao") or "").lower()
    midia = resumo_midia(d)
    return {
        "caminho": caminho, "pasta": pasta, "nome": d.get("nome") or nome,
        "ext": ext, "tipo": tipo_de(ext),
        "bytes": int(d.get("tamanho_bytes") or 0), "mtime": d.get("mtime") or "",
        "sha256": d.get("sha256") or "", "formato": d.get("formato") or "",
        "puid": d.get("puid") or "",
        "duracao": midia["duracao"], "resolucao": midia["resolucao"],
        "codec": midia["codec_video"] or midia["codec_audio"],
        "seq": seq,
    }


class _Indice:
    def __init__(self):
        self.ident = None        # (dispositivo, inode) do arquivo lido
        self.pos = 0             # até onde o arquivo já foi lido (fim da última linha completa)
        self.cauda = b""         # últimos bytes lidos: conferem que o arquivo não foi trocado
        self.seq = 0             # nº de linhas aceitas (ordem de chegada)
        self.arquivos = {}       # caminho -> registro (o último vale: retomadas repetem)
        self.lock = threading.Lock()

    def atualizar(self, arq):
        st = arq.stat()
        ident = (st.st_dev, st.st_ino)
        with open(arq, "rb") as f:
            # Reescrito ("Completar dados" troca o arquivo inteiro)? Então refaz.
            # O inode nem sempre serve (exFAT/FAT no Windows): confere também se
            # os últimos bytes já lidos continuam lá.
            if self.pos:
                f.seek(self.pos - len(self.cauda))
                mudou = ident != self.ident or st.st_size < self.pos or f.read(len(self.cauda)) != self.cauda
                if mudou:
                    self.pos, self.seq, self.cauda, self.arquivos = 0, 0, b"", {}
            self.ident = ident
            if st.st_size == self.pos:
                return
            f.seek(self.pos)
            bloco = f.read()
        fim = bloco.rfind(b"\n") + 1          # só linhas completas: a última pode estar sendo escrita
        if not fim:
            return
        for linha in bloco[:fim].splitlines():
            if not linha.strip():
                continue
            try:
                d = json.loads(linha.decode("utf-8", errors="replace"))
            except Exception:
                continue
            self.seq += 1
            a = _arquivo(d, self.seq)
            if a["caminho"]:
                self.arquivos[a["caminho"]] = a
        self.pos += fim
        self.cauda = bloco[max(0, fim - 64):fim]

_INDICES = {}
_INDICES_LOCK = threading.Lock()


def _indice(manifest_dir, etiqueta):
    arq = nomes.manifesto(manifest_dir, etiqueta)
    if not arq.exists():
        return None
    with _INDICES_LOCK:
        ind = _INDICES.setdefault(str(arq), _Indice())
    with ind.lock:
        ind.atualizar(arq)
    return ind


def _limpar_pasta(pasta):
    partes = [p for p in str(pasta or "").replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(partes)


def _dentro(caminho, pasta):
    return not pasta or caminho.startswith(pasta + "/")


def _ordenar(itens, ordem):
    if ordem == "tamanho":
        return sorted(itens, key=lambda a: -a["bytes"])
    if ordem == "data":
        return sorted(itens, key=lambda a: a.get("mtime") or "", reverse=True)
    if ordem == "recentes":
        return sorted(itens, key=lambda a: -a.get("seq", 0))
    return sorted(itens, key=lambda a: a["nome"].lower())


def listar(manifest_dir, etiqueta, pasta="", busca="", tipo="", ordem="nome", pagina=1):
    """Uma "tela" do conteúdo: subpastas e arquivos de `pasta`, ou — com `busca`
    ou `tipo` — os arquivos que casam em toda a árvore abaixo de `pasta`.
    Devolve None se o HD não tem inventário aqui."""
    ind = _indice(manifest_dir, etiqueta)
    if ind is None:
        return None
    pasta = _limpar_pasta(pasta)
    busca = (busca or "").strip().lower()
    tipo = tipo if tipo in TIPOS else ""
    with ind.lock:
        todos = list(ind.arquivos.values())
        ultimo_seq = ind.seq

    prefixo = pasta + "/" if pasta else ""
    sub = {}
    diretos = []
    totais = {"arquivos": 0, "bytes": 0, "sem_hash": 0, "tipos": dict.fromkeys(TIPOS, 0)}
    filtrados = []
    for a in todos:
        if not _dentro(a["caminho"], pasta):
            continue
        totais["arquivos"] += 1
        totais["bytes"] += a["bytes"]
        totais["sem_hash"] += not a["sha256"]
        totais["tipos"][a["tipo"]] += 1
        if busca or tipo:
            if (not tipo or a["tipo"] == tipo) and (not busca or busca in a["caminho"].lower()):
                filtrados.append(a)
            continue
        if a["pasta"] == pasta:
            diretos.append(a)
        else:
            filho = a["caminho"][len(prefixo):].split("/", 1)[0]
            s = sub.setdefault(filho, {"nome": filho, "caminho": prefixo + filho, "arquivos": 0,
                                       "bytes": 0, "tipos": dict.fromkeys(TIPOS, 0), "seq": 0})
            s["arquivos"] += 1
            s["bytes"] += a["bytes"]
            s["tipos"][a["tipo"]] += 1
            s["seq"] = max(s["seq"], a["seq"])

    lista = filtrados if (busca or tipo) else diretos
    lista = _ordenar(lista, ordem)
    paginas = max(1, -(-len(lista) // POR_PAGINA))
    pagina = min(max(1, int(pagina or 1)), paginas)
    chave = (lambda s: -s["bytes"]) if ordem == "tamanho" else (lambda s: s["nome"].lower())
    subpastas = sorted(sub.values(), key=chave)
    migalhas = []
    acumulado = ""
    for p in pasta.split("/") if pasta else []:
        acumulado = f"{acumulado}/{p}" if acumulado else p
        migalhas.append({"nome": p, "caminho": acumulado})
    return {
        "etiqueta": etiqueta, "pasta": pasta, "migalhas": migalhas,
        "busca": busca, "tipo": tipo, "ordem": ordem,
        "subpastas": [] if (busca or tipo) else subpastas,
        "arquivos": lista[(pagina - 1) * POR_PAGINA: pagina * POR_PAGINA],
        "encontrados": len(lista), "pagina": pagina, "paginas": paginas,
        "totais": totais, "total_hd": len(todos), "ultimo_seq": ultimo_seq,
    }

