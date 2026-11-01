"""
Convenção de nomes dos arquivos de cada HD na pasta de manifestos — um só lugar
para ela (varredura, Acervo, pacote de envio e importação usam daqui):

  manifesto_<etiqueta>.jsonl            inventário
  disco_<etiqueta>.json                 ficha física / estado do HD
  operacao_<etiqueta>_<data>.log        registro de cada operação
"""
import glob
import os
import time
from pathlib import Path


def manifesto(pasta, etiqueta):
    return Path(pasta) / f"manifesto_{etiqueta}.jsonl"


def ficha(pasta, etiqueta):
    return Path(pasta) / f"disco_{etiqueta}.json"


def registros(pasta, etiqueta):
    """Registros de operação deste HD, do mais antigo ao mais novo."""
    padrao = os.path.join(glob.escape(str(pasta)), f"operacao_{glob.escape(etiqueta)}_*.log")
    return [Path(p) for p in sorted(glob.glob(padrao))]


def manifestos(pasta):
    """{etiqueta: caminho} dos inventários da pasta, em ordem alfabética."""
    pasta = Path(pasta)
    if not pasta.is_dir():
        return {}
    achados = {a.name[len("manifesto_"):-len(".jsonl")]: a for a in pasta.glob("manifesto_*.jsonl")}
    return dict(sorted(achados.items(), key=lambda kv: kv[0].lower()))


def novo_registro(pasta, etiqueta):
    return Path(pasta) / f"operacao_{etiqueta}_{time.strftime('%Y%m%d_%H%M%S')}.log"
