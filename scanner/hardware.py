"""
Metadados FÍSICOS dos discos ligados (marca, modelo, nº de série, conexão, HDD/SSD),
lidos do próprio disco — sem nada digitado pelo operador.

Para que serve:
  - mostrar no painel o tipo de cada HD (portátil 2,5", de mesa 3,5", SSD...) com o
    ícone certo, a marca e o modelo;
  - RECONHECER um HD já varrido pelo nº de série, mesmo que o rótulo do volume ou a
    letra mudem — o operador não precisa redigitar a etiqueta (retomada segura);
  - guardar a procedência do inventário (de qual peça física ele veio).

Onde os dados vêm:
  Windows -> uma chamada ao PowerShell (Win32_DiskDrive + partições + volumes, e
             Get-PhysicalDisk para HDD/SSD e barramento). O PNPDeviceID das caixas
             USB traz o fabricante/produto reportado pela ponte USB (VEN_/PROD_).
  Linux   -> lsblk (modelo, fabricante, série, transporte, rotacional).
Sem permissão ou sem a ferramenta, devolve {} — o painel segue como antes.

*** Só lê informações do sistema. Nunca escreve no disco do acervo. ***
"""
import base64
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path

_CACHE = {"em": 0.0, "dados": {}}
_CACHE_SEG = 20

# --------------------------------------------------------------------------- #
# Classificação: marca, linha de produto e tipo físico                        #
# --------------------------------------------------------------------------- #

# Fabricante pelo VID do USB (quando a ponte USB é do próprio fabricante).
# Pontes genéricas (ASMedia 174C, JMicron 152D, Realtek 0BDA...) não dizem a marca.
VID_MARCA = {
    "1058": "Western Digital", "0BC2": "Seagate", "0480": "Toshiba",
    "04E8": "Samsung", "059F": "LaCie", "0781": "SanDisk", "0411": "Buffalo",
    "125F": "ADATA", "8564": "Transcend", "0951": "Kingston", "18A5": "Verbatim",
    "17EF": "Lenovo", "0634": "Micron", "1E3D": "Chipsbank",
}

# (padrão, marca) — testados em ordem, sobre "fabricante + modelo + produto USB".
MARCAS = [
    (r"\bWDC?\b|WD_|western\s*digital|my\s*passport|my\s*book|\belements\b|easystore|wd\s*black", "Western Digital"),
    (r"seagate|\bST\d{3,}|\bexpansion\b|backup\s*plus|one\s*touch|barracuda|ironwolf|firecuda|game\s*drive", "Seagate"),
    (r"lacie|\brugged\b|\bd2\b|2big|porsche\s*design", "LaCie"),
    (r"toshiba|canvio|\bDT01|\bMQ0\d|\bHDW[A-Z]", "Toshiba"),
    (r"samsung|portable\s*ssd\s*t\d|\bT[579]\b|\bMZ-", "Samsung"),
    (r"sandisk|cruzer|ultra\s*fit|extreme\s*(pro\s*)?portable", "SanDisk"),
    (r"hgst|\bHT[SE]\d|\bHU[SH]\d", "HGST"),
    (r"hitachi|\bHDS\d|\bHDT\d", "Hitachi"),
    (r"maxtor", "Maxtor"),
    (r"buffalo|ministation|drivestation", "Buffalo"),
    (r"adata|\bHD[3-8]\d0\b|\bSD[67]\d0\b", "ADATA"),
    (r"transcend|storejet", "Transcend"),
    (r"verbatim", "Verbatim"),
    (r"kingston|datatraveler|\bSA400|\bSKC\d|\bSNV\d", "Kingston"),
    (r"crucial|\bCT\d+(MX|BX|P\d|X\d)", "Crucial"),
    (r"lenovo", "Lenovo"),
    (r"intel", "Intel"),
    (r"micron", "Micron"),
]

# Linhas de produto conhecidas -> (nome, tipo). O tipo da linha vence a heurística.
LINHAS = [
    (r"my\s*passport", "My Passport", "portatil"),
    (r"my\s*book", "My Book", "mesa"),
    (r"elements\s*desktop", "Elements Desktop", "mesa"),
    # Elements / Expansion / easystore existem em portátil E de mesa: o tipo fica
    # para a heurística de capacidade (None).
    (r"\belements\b", "Elements", None),
    (r"easystore", "easystore", None),
    (r"expansion\s*\+?\s*desk", "Expansion Desktop", "mesa"),
    (r"\bexpansion\b", "Expansion", None),
    (r"backup\s*plus\s*(hub|desk)|\bBUP\s*(desk|hub)", "Backup Plus Desktop", "mesa"),
    (r"backup\s*plus|\bBUP\b", "Backup Plus", "portatil"),
    (r"one\s*touch\s*hub", "One Touch Hub", "mesa"),
    (r"one\s*touch", "One Touch", "portatil"),
    (r"game\s*drive", "Game Drive", "portatil"),
    (r"canvio", "Canvio", "portatil"),
    (r"\brugged\b", "Rugged", "portatil"),
    (r"\bd2\b|2big", "d2", "mesa"),
    (r"ministation", "MiniStation", "portatil"),
    (r"drivestation", "DriveStation", "mesa"),
    (r"storejet", "StoreJet", "portatil"),
    (r"portable\s*ssd|\bPSSD\b|\b(T[579])\b", "Portable SSD", "ssd_externo"),
    (r"extreme\s*(pro\s*)?portable", "Extreme Portable SSD", "ssd_externo"),
    (r"datatraveler|cruzer|ultra\s*fit|flash\s*drive|usb\s*flash", "Pendrive", "pendrive"),
]

ROTULOS_TIPO = {
    "portatil": ("HD portátil", "2,5″"),
    "mesa": ("HD de mesa (com fonte)", "3,5″"),
    "ssd_externo": ("SSD externo", ""),
    "interno_hdd": ("HD interno", "3,5″"),
    "interno_ssd": ("SSD interno", ""),
    "pendrive": ("Pendrive", ""),
    "desconhecido": ("Disco", ""),
}

_BUS_NUM = {"7": "USB", "11": "SATA", "17": "NVMe", "10": "SAS", "8": "RAID",
            "3": "ATA", "6": "Fibre Channel", "1": "SCSI"}
_MIDIA_NUM = {"3": "HDD", "4": "SSD", "5": "SCM", "0": "Unspecified"}


def _pnp_partes(pnp):
    """Extrai (VEN, PROD, VID) do PNPDeviceID/IDs de hardware do Windows.
    Ex.: USBSTOR\\DISK&VEN_WD&PROD_ELEMENTS_25A2&REV_1021\\575831... -> ('WD','ELEMENTS 25A2','')"""
    pnp = pnp or ""
    ven = re.search(r"VEN_([^&\\]+)", pnp, re.I)
    prod = re.search(r"PROD_([^&\\]+)", pnp, re.I)
    vid = re.search(r"VID_([0-9A-F]{4})", pnp, re.I)
    limpa = lambda m: m.group(1).replace("_", " ").strip() if m else ""
    return limpa(ven), limpa(prod), (vid.group(1).upper() if vid else "")


def _midia(info):
    m = str(info.get("media") or "").strip()
    return _MIDIA_NUM.get(m, m)


def _barramento(info):
    b = str(info.get("barramento") or "").strip()
    b = _BUS_NUM.get(b, b)
    iface = str(info.get("interface") or "").upper()
    pnp = str(info.get("pnp") or "").upper()
    if b.upper() == "USB" or iface == "USB" or pnp.startswith("USBSTOR") or info.get("transporte") == "usb":
        return "USB"
    if b:
        return b
    if info.get("transporte"):
        return str(info["transporte"]).upper()
    return iface or ""


def classificar(info):
    """Recebe o dicionário cru do disco e devolve {marca, linha, tipo, rotulo_tipo,
    formato, conexao, midia}. Nunca falha: sem pistas, tipo 'desconhecido'."""
    ven, prod, vid = _pnp_partes(info.get("pnp"))
    texto = " ".join(str(x) for x in (info.get("fabricante"), info.get("modelo"),
                                      info.get("nome_amigavel"), ven, prod) if x)
    marca = ""
    for padrao, nome in MARCAS:
        if re.search(padrao, texto, re.I):
            marca = nome
            break
    if not marca and vid in VID_MARCA:
        marca = VID_MARCA[vid]

    linha, tipo_linha = "", None
    for padrao, nome, t in LINHAS:
        if re.search(padrao, texto, re.I):
            linha, tipo_linha = nome, t
            break

    conexao = _barramento(info)
    midia = _midia(info)
    if midia not in ("HDD", "SSD") and info.get("rotacional") is not None:
        midia = "HDD" if info.get("rotacional") else "SSD"
    if midia not in ("HDD", "SSD") and re.search(r"\bssd\b|nvme|solid\s*state", texto, re.I):
        midia = "SSD"
    tamanho = int(info.get("tamanho") or 0)
    externo = conexao == "USB"

    if tipo_linha:
        tipo = tipo_linha
    elif externo and midia == "SSD":
        tipo = "ssd_externo"
    elif externo and tamanho and tamanho < 300 * 10**9 and midia != "HDD":
        tipo = "pendrive"
    elif externo and re.search(r"desk|book|hub|station|duo|raid", texto, re.I):
        tipo = "mesa"
    elif externo:
        # Portáteis 2,5" vão até ~5-6 TB; acima disso é HD de mesa (3,5", com fonte).
        tipo = "mesa" if tamanho > 6.2 * 10**12 else "portatil"
    elif midia == "SSD" or conexao == "NVMe":
        tipo = "interno_ssd"
    elif midia == "HDD" or conexao in ("SATA", "ATA", "SAS"):
        tipo = "interno_hdd"
    else:
        tipo = "desconhecido"

    rotulo, formato = ROTULOS_TIPO[tipo]
    return {"marca": marca, "linha": linha, "tipo": tipo, "rotulo_tipo": rotulo,
            "formato": formato, "conexao": conexao, "midia": midia}


# --------------------------------------------------------------------------- #
# Leitura no sistema                                                           #
# --------------------------------------------------------------------------- #

_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$fis = @{}
foreach ($p in (Get-PhysicalDisk)) { $fis[[string]$p.DeviceId] = $p }
$saida = foreach ($d in (Get-CimInstance Win32_DiskDrive)) {
  $vols = @()
  foreach ($part in (Get-CimAssociatedInstance -InputObject $d -ResultClassName Win32_DiskPartition)) {
    foreach ($l in (Get-CimAssociatedInstance -InputObject $part -ResultClassName Win32_LogicalDisk)) {
      $vols += [pscustomobject]@{ letra = $l.DeviceID; rotulo = $l.VolumeName;
        serial_volume = $l.VolumeSerialNumber; sistema_arquivos = $l.FileSystem;
        livre = $l.FreeSpace; total = $l.Size }
    }
  }
  $pd = $fis[[string]$d.Index]
  [pscustomobject]@{
    modelo = $d.Model; serial = ([string]$d.SerialNumber).Trim(); interface = $d.InterfaceType;
    tipo_midia_win = $d.MediaType; tamanho = $d.Size; pnp = $d.PNPDeviceID; indice = $d.Index;
    fabricante = $d.Manufacturer; firmware = $d.FirmwareRevision;
    media = $(if ($pd) { [string]$pd.MediaType } else { $null });
    barramento = $(if ($pd) { [string]$pd.BusType } else { $null });
    nome_amigavel = $(if ($pd) { $pd.FriendlyName } else { $null });
    volumes = $vols
  }
}
@($saida) | ConvertTo-Json -Depth 5 -Compress
"""


def _windows():
    cmd = base64.b64encode(_PS.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-EncodedCommand", cmd],
                           capture_output=True, encoding="utf-8", errors="replace",
                           timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        dados = json.loads(r.stdout) if r.stdout.strip() else []
    except Exception:
        return {}
    if isinstance(dados, dict):
        dados = [dados]
    por_ponto = {}
    for d in dados:
        vols = d.pop("volumes", None) or []
        if isinstance(vols, dict):
            vols = [vols]
        # Fabricante do Win32 costuma ser "(Unidades de disco padrão)": não é marca.
        if re.search(r"padr|standard", str(d.get("fabricante") or ""), re.I):
            d["fabricante"] = ""
        for v in vols:
            letra = str(v.get("letra") or "").upper()
            if not letra:
                continue
            info = dict(d, **{k: v.get(k) for k in ("rotulo", "serial_volume",
                                                    "sistema_arquivos", "livre", "total")})
            info["letra"] = letra
            por_ponto[letra + "\\"] = info
    return por_ponto


def _linux():
    try:
        r = subprocess.run(["lsblk", "-J", "-b", "-o",
                            "NAME,TYPE,MODEL,VENDOR,SERIAL,TRAN,ROTA,SIZE,MOUNTPOINT,LABEL,FSTYPE"],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=15)
        arv = json.loads(r.stdout).get("blockdevices", [])
    except Exception:
        return {}
    por_ponto = {}

    def visitar(no, disco):
        mp = no.get("mountpoint")
        if mp and disco:
            info = {"modelo": (disco.get("model") or "").strip(),
                    "fabricante": (disco.get("vendor") or "").strip(),
                    "serial": (disco.get("serial") or "").strip(),
                    "transporte": (disco.get("tran") or "").strip(),
                    "rotacional": bool(disco.get("rota")) if disco.get("rota") is not None else None,
                    "tamanho": disco.get("size"), "rotulo": no.get("label"),
                    "sistema_arquivos": no.get("fstype"), "letra": ""}
            por_ponto[mp] = info
        for f in no.get("children") or []:
            visitar(f, disco)

    for d in arv:
        if d.get("type") == "disk":
            visitar(d, d)
    return por_ponto


def detectar(forcar=False):
    """{ponto_de_montagem: info} de todos os discos montados, com a classificação
    (marca/linha/tipo) já aplicada. Resultado guardado por alguns segundos."""
    agora = time.monotonic()
    if not forcar and agora - _CACHE["em"] < _CACHE_SEG:
        return _CACHE["dados"]
    bruto = _windows() if platform.system() == "Windows" else _linux()
    dados = {p: dict(i, **classificar(i)) for p, i in bruto.items()}
    _CACHE.update(em=agora, dados=dados)
    return dados


def info_para(caminho):
    """Metadados do disco onde está `caminho` (ex.: 'H:\\' ou 'H:\\Projetos')."""
    dados = detectar()
    caminho = str(caminho)
    if platform.system() == "Windows":
        letra = caminho[:2].upper() + "\\"
        return dados.get(letra)
    melhor = None
    for mp, info in dados.items():
        if caminho == mp or caminho.startswith(mp.rstrip("/") + "/"):
            if melhor is None or len(mp) > len(melhor[0]):
                melhor = (mp, info)
    return melhor[1] if melhor else None


# --------------------------------------------------------------------------- #
# Ficha do disco (procedência + reconhecimento pelo nº de série)               #
# --------------------------------------------------------------------------- #

CAMPOS_FICHA = ("modelo", "fabricante", "serial", "conexao", "midia", "tamanho",
                "marca", "linha", "tipo", "rotulo_tipo", "formato", "rotulo",
                "serial_volume", "sistema_arquivos", "firmware", "pnp", "livre", "total")


def _arquivo_ficha(pasta, etiqueta):
    return Path(pasta) / f"disco_{etiqueta}.json"


def ler_ficha(pasta, etiqueta):
    arq = _arquivo_ficha(pasta, etiqueta)
    if arq.exists():
        try:
            return json.loads(arq.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def atualizar_ficha(pasta, etiqueta, **campos):
    """Mescla `campos` na ficha do HD (cria se não existir). Independe de haver
    metadados físicos: guarda também o grupo e o estado da última varredura."""
    ficha = ler_ficha(pasta, etiqueta)
    ficha.update({k: v for k, v in campos.items() if v not in (None, "")})
    ficha["etiqueta"] = etiqueta
    try:
        Path(pasta).mkdir(parents=True, exist_ok=True)
        _arquivo_ficha(pasta, etiqueta).write_text(
            json.dumps(ficha, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return None
    return ficha


def registrar(pasta, etiqueta, caminho):
    """Grava/atualiza a ficha física do HD ao lado dos manifestos
    (disco_<etiqueta>.json). Devolve a ficha, ou None se não há metadados."""
    info = info_para(caminho)
    if not info:
        return None
    vistos = (ler_ficha(pasta, etiqueta).get("visto_em") or [])
    vistos.append(time.strftime("%Y-%m-%dT%H:%M:%S"))
    campos = {k: info.get(k) for k in CAMPOS_FICHA}
    campos["visto_em"] = vistos[-50:]
    return atualizar_ficha(pasta, etiqueta, **campos)


def fichas(pasta):
    """Todas as fichas já gravadas: {etiqueta: ficha}."""
    out = {}
    for arq in Path(pasta).glob("disco_*.json"):
        try:
            f = json.loads(arq.read_text(encoding="utf-8"))
            out[f.get("etiqueta") or arq.stem[len("disco_"):]] = f
        except Exception:
            continue
    return out


def serial_util(s):
    """Nº de série que identifica de fato um disco. Pontes USB baratas reportam
    séries genéricas (zeros, '0123456789ABCDEF', repetidos) — iguais em discos
    diferentes —, que NÃO servem para reconhecer um HD."""
    s = re.sub(r"[\s.\-_]", "", str(s or "")).upper()
    if len(s) < 6 or len(set(s)) <= 2:
        return ""
    if s in ("0123456789ABCDEF", "123456789ABC", "ABCDEF0123456789", "NONE", "DEFAULTSTRING"):
        return ""
    return s


def reconhecer(pasta, info):
    """Etiqueta de um HD já catalogado com o MESMO nº de série físico (ou, sem
    série, o mesmo nº de série do volume). None se for um HD novo."""
    if not info:
        return None
    serial = serial_util(info.get("serial"))
    sv = (info.get("serial_volume") or "").strip()
    todas = fichas(pasta)
    for etiqueta, f in todas.items():
        if serial and serial == serial_util(f.get("serial")):
            return etiqueta
    # Sem série física confiável: o nº de série do VOLUME (muda se formatar, mas
    # o acervo não é formatado) ainda distingue os HDs.
    for etiqueta, f in todas.items():
        if not serial and sv and sv == (f.get("serial_volume") or "").strip():
            return etiqueta
    return None
