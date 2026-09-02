# -*- mode: python ; coding: utf-8 -*-
"""
Spec do PyInstaller para gerar o PreservaScan.exe (onefile).

Rodar a partir da RAIZ do repositorio:
    pyinstaller packaging/preservascan.spec --noconfirm

Gera dist/PreservaScan.exe (Windows) ou dist/PreservaScan (Linux/macOS).
Os caminhos sao resolvidos a partir do proprio .spec (SPECPATH), entao funciona
de qualquer diretorio.
"""
import os
from PyInstaller.utils.hooks import collect_submodules

# SPECPATH = pasta deste .spec (packaging/). O codigo fica um nivel acima.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
SCANNER = os.path.join(ROOT, "scanner")

# uvicorn/starlette/fastapi carregam varios modulos dinamicamente; sem estes o
# .exe compila mas quebra ao subir o servidor. collect_submodules pega todos.
hiddenimports = []
for pacote in ("uvicorn", "starlette", "fastapi", "anyio"):
    hiddenimports += collect_submodules(pacote)
hiddenimports += [
    "relatorio",                 # gerador do relatorio local (importado pelo panel)
    "dedup",                     # planejador de deduplicacao (importado pelo panel)
    "dotenv",                    # carrega .env ao lado do .exe (DATABASE_URL do Supabase)
    "multipart",                 # python-multipart (formularios do painel)
    "psycopg2", "psycopg2.extras",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

# Ferramentas externas (MediaInfo.exe, exiftool.exe) — se a CI baixou para
# packaging/ferramentas, empacota junto para o operador NÃO precisar instalar nada.
# Se a pasta estiver ausente/vazia, o build segue e o scanner usa a pasta
# 'ferramentas' ao lado do .exe ou o PATH (fallbacks do resolvedor).
_datas = [(os.path.join(SCANNER, "templates"), "templates")]
_fer = os.path.join(SPECPATH, "ferramentas")
if os.path.isdir(_fer) and os.listdir(_fer):
    _datas.append((_fer, "ferramentas"))

a = Analysis(
    [os.path.join(SCANNER, "preservascan.py")],
    pathex=[SCANNER],                         # panel/scan/db sao irmaos do entrypoint
    binaries=[],
    datas=_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PreservaScan",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # sem UPX: menos falso-positivo de antivirus
    runtime_tmpdir=None,
    console=True,                 # janela preta com instrucoes/erros (nao ocultar)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
