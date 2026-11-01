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
import glob
import hashlib
import json
import os
import re
import socket
import zipfile
from pathlib import Path

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


def _sha256(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


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
    pasta = Path(manifest_dir)
    if not pasta.exists():
        return []
    return sorted((a.name[len("manifesto_"):-len(".jsonl")] for a in pasta.glob("manifesto_*.jsonl")),
                  key=str.lower)


def fichas_sem_inventario(manifest_dir):
    """Fichas (disco_<etiqueta>.json) de HDs que ainda não têm inventário aqui."""
    pasta = Path(manifest_dir)
    if not pasta.exists():
        return []
    com_inventario = set(etiquetas(pasta))
    return sorted((a for a in pasta.glob("disco_*.json")
                   if a.name[len("disco_"):-len(".json")] not in com_inventario),
                  key=lambda a: a.name.lower())


def situacao_envio(manifest_dir, envios_dir):
    """{etiqueta: {"pendente": bool, "em": data do último pacote ou None,
    "pacote": nome do último pacote ou None}} para cada inventário daqui."""
    reg = ler_registro(envios_dir)
    out = {}
    for e in etiquetas(manifest_dir):
        r = reg.get(e) or {}
        atual = _marca_manifesto(Path(manifest_dir) / f"manifesto_{e}.jsonl")
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
    nome = f"PRESERVA-SCAN_{_maquina()}_{agora:%Y-%m-%d_%H%M}.zip"
    destino = envios_dir / nome
    n = 1
    while destino.exists():
        n += 1
        destino = envios_dir / nome.replace(".zip", f"_{n}.zip")
    tmp = destino.with_name(destino.name + ".parcial")

    manifestacao = {"gerado_em": agora.isoformat(timespec="seconds"), "maquina": _maquina(),
                    "versao_programa": VERSAO, "discos": []}
    marcas = {}
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for e in selecionadas:
            arquivos = [manifest_dir / f"manifesto_{e}.jsonl"]
            ficha = manifest_dir / f"disco_{e}.json"
            if ficha.exists():
                arquivos.append(ficha)
            padrao = os.path.join(glob.escape(str(manifest_dir)), f"operacao_{glob.escape(e)}_*.log")
            arquivos += [Path(p) for p in sorted(glob.glob(padrao))]
            itens = []
            for a in arquivos:
                z.write(a, arcname=f"manifestos/{a.name}")
                itens.append({"arquivo": a.name, "bytes": a.stat().st_size, "sha256": _sha256(a)})
            marcas[e] = _marca_manifesto(arquivos[0])
            manifestacao["discos"].append({"etiqueta": e, "arquivos": itens})
        # Fichas de HDs sem inventário (ex.: "não reconhecido", ou na fila para
        # varrer) vão sempre: são pequenas e é assim que o gestor fica sabendo deles.
        avulsas = []
        for a in fichas_sem_inventario(manifest_dir):
            z.write(a, arcname=f"manifestos/{a.name}")
            avulsas.append({"arquivo": a.name, "bytes": a.stat().st_size, "sha256": _sha256(a)})
        manifestacao["fichas_sem_inventario"] = avulsas
        z.writestr("pacote.json", json.dumps(manifestacao, ensure_ascii=False, indent=2))
        z.writestr("LEIAME.txt", LEIAME)
    os.replace(tmp, destino)

    reg = ler_registro(envios_dir)
    for e, marca in marcas.items():
        reg[e] = {"marca": marca, "em": agora.isoformat(timespec="seconds"), "pacote": destino.name}
    (envios_dir / REGISTRO).write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"arquivo": str(destino), "nome": destino.name, "discos": selecionadas,
            "fichas_sem_inventario": len(avulsas),
            "bytes": destino.stat().st_size, "sha256": _sha256(destino)}


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
