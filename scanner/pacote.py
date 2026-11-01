"""
Pacote de envio: junta num único .zip tudo o que o gestor do acervo precisa para
analisar os HDs varridos neste computador — sem banco, sem internet no momento da
varredura. O .zip é levado pelo canal combinado (e-mail, Drive, pendrive) e entra
do outro lado pela tela "Importar manifestos".

Conteúdo:
  manifestos/manifesto_<etiqueta>.jsonl   inventário (com metadados de mídia)
  manifestos/disco_<etiqueta>.json        ficha física do HD (marca, modelo, série,
                                          tipo, grupo, estado da última varredura)
  manifestos/operacao_<etiqueta>_*.log    registros das varreduras
  pacote.json                             o que foi enviado, de onde, com que
                                          versão — e o SHA-256 de cada arquivo
  LEIAME.txt                              como importar

O registro do que já foi empacotado fica em <envios>/registro.json: um inventário
que muda depois (ex.: "Completar dados") volta a ficar "pronto para enviar".
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import zipfile
from pathlib import Path

import hardware
import nomes
from scan import CHUNK, sha256_de
from versao import VERSAO

REGISTRO = "registro.json"

LEIAME = """PRESERVA-SCAN — pacote de envio
================================

Este arquivo foi gerado pelo PRESERVA-SCAN e contém os inventários de HDs do acervo
(nenhum arquivo do acervo vai junto: só a lista do que existe em cada HD, com a
impressão digital SHA-256 de cada arquivo).

Como usar (gestor do acervo):
  1. Abra o PRESERVA-SCAN.
  2. Menu "Importar manifestos" -> escolha este arquivo .zip -> Importar.
  3. Os HDs passam a aparecer no Acervo, no relatório e nas análises.

Integridade: pacote.json traz o SHA-256 de cada arquivo deste pacote, para conferir
que nada se corrompeu no caminho.
"""


class _Hasher:
    """Arquivo-só-leitura que calcula o SHA-256 do que é lido por ele."""
    def __init__(self, f):
        self.f, self.h = f, hashlib.sha256()

    def read(self, n=-1):
        bloco = self.f.read(n)
        self.h.update(bloco)
        return bloco


def _incluir(z, arquivo):
    """Põe `arquivo` no zip (em manifestos/) lendo-o UMA vez só: o SHA-256 sai
    da mesma leitura que comprime — manifestos têm centenas de MB."""
    info = zipfile.ZipInfo.from_file(arquivo, arcname=f"manifestos/{arquivo.name}")
    info.compress_type = zipfile.ZIP_DEFLATED
    with open(arquivo, "rb") as origem, z.open(info, "w", force_zip64=True) as destino:
        leitor = _Hasher(origem)
        shutil.copyfileobj(leitor, destino, CHUNK)
    return {"arquivo": arquivo.name, "bytes": info.file_size, "sha256": leitor.h.hexdigest()}


def _marca_manifesto(caminho):
    """Assinatura barata de um manifesto (tamanho + data de modificação): muda
    quando o inventário muda, sem reler arquivos de centenas de MB."""
    st = Path(caminho).stat()
    return [st.st_size, st.st_mtime_ns]


def _maquina():
    try:
        nome = socket.gethostname()
    except Exception:
        nome = ""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", nome).strip("-")[:40] or "computador"


def ler_registro(envios_dir):
    arq = Path(envios_dir) / REGISTRO
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except Exception:
        return {}


def etiquetas(manifest_dir):
    return list(nomes.manifestos(manifest_dir))


def fichas_sem_inventario(manifest_dir):
    """Etiquetas dos HDs que têm ficha mas ainda não têm inventário aqui
    (não reconhecidos, na fila para varrer)."""
    pasta = Path(manifest_dir)
    if not pasta.is_dir():
        return []
    return sorted(set(hardware.fichas(pasta)) - set(nomes.manifestos(pasta)), key=str.lower)


def situacao_envio(manifest_dir, envios_dir):
    """{etiqueta: {"pendente": bool, "em": data do último pacote ou None,
    "pacote": nome do último pacote ou None}} para cada inventário daqui."""
    reg = ler_registro(envios_dir)
    out = {}
    for e in etiquetas(manifest_dir):
        r = reg.get(e) or {}
        atual = _marca_manifesto(nomes.manifesto(manifest_dir, e))
        out[e] = {"pendente": r.get("marca") != atual, "em": r.get("em"), "pacote": r.get("pacote")}
    return out


def gerar(manifest_dir, envios_dir, selecionadas=None):
    """Gera o pacote .zip com as etiquetas `selecionadas` (padrão: as pendentes).
    Devolve {"arquivo", "nome", "discos", "bytes", "sha256"}."""
    manifest_dir, envios_dir = Path(manifest_dir), Path(envios_dir)
    todas = etiquetas(manifest_dir)
    if selecionadas is None:
        sit = situacao_envio(manifest_dir, envios_dir)
        selecionadas = [e for e in todas if sit[e]["pendente"]]
    selecionadas = [e for e in selecionadas if e in todas]
    if not selecionadas:
        raise ValueError("Nenhum inventário selecionado para o pacote.")

    envios_dir.mkdir(parents=True, exist_ok=True)
    agora = datetime.datetime.now()
    maquina = _maquina()
    nome = f"PRESERVA-SCAN_{maquina}_{agora:%Y-%m-%d_%H%M}.zip"
    destino = envios_dir / nome
    n = 1
    while destino.exists():
        n += 1
        destino = envios_dir / nome.replace(".zip", f"_{n}.zip")
    tmp = destino.with_name(destino.name + ".parcial")

    manifestacao = {"gerado_em": agora.isoformat(timespec="seconds"), "maquina": maquina,
                    "versao_programa": VERSAO, "discos": []}
    marcas = {}
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for e in selecionadas:
            manifesto = nomes.manifesto(manifest_dir, e)
            marcas[e] = _marca_manifesto(manifesto)
            ficha = nomes.ficha(manifest_dir, e)
            arquivos = [manifesto] + ([ficha] if ficha.exists() else []) + nomes.registros(manifest_dir, e)
            manifestacao["discos"].append({"etiqueta": e, "arquivos": [_incluir(z, a) for a in arquivos]})
        # Fichas de HDs sem inventário (ex.: "não reconhecido", ou na fila para
        # varrer) vão sempre: são pequenas e é assim que o gestor fica sabendo deles.
        manifestacao["fichas_sem_inventario"] = [_incluir(z, nomes.ficha(manifest_dir, e))
                                                 for e in fichas_sem_inventario(manifest_dir)]
        z.writestr("pacote.json", json.dumps(manifestacao, ensure_ascii=False, indent=2))
        z.writestr("LEIAME.txt", LEIAME)
    os.replace(tmp, destino)

    reg = ler_registro(envios_dir)
    for e, marca in marcas.items():
        reg[e] = {"marca": marca, "em": agora.isoformat(timespec="seconds"), "pacote": destino.name}
    (envios_dir / REGISTRO).write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"arquivo": str(destino), "nome": destino.name, "discos": selecionadas,
            "fichas_sem_inventario": len(manifestacao["fichas_sem_inventario"]),
            "bytes": destino.stat().st_size, "sha256": sha256_de(destino)}


def pacotes(envios_dir):
    """Pacotes já gerados, do mais novo ao mais antigo: [{nome, bytes, em}]."""
    pasta = Path(envios_dir)
    if not pasta.exists():
        return []
    itens = []
    for a in pasta.glob("PRESERVA-SCAN_*.zip"):
        st = a.stat()
        itens.append({"nome": a.name, "bytes": st.st_size,
                      "em": datetime.datetime.fromtimestamp(st.st_mtime)})
    return sorted(itens, key=lambda x: x["em"], reverse=True)
