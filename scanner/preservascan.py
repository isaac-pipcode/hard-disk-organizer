#!/usr/bin/env python3
"""
Ponto de entrada do PRESERVA-SCAN empacotado (PreservaScan.exe).

Ao abrir (duplo-clique), sobe o painel local e abre o navegador sozinho, para
o operador NAO precisar de terminal. Fechar a janela preta encerra o painel.

Tambem funciona sem empacotar:  python preservascan.py
"""
import os
import sys
import time
import socket
import threading
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
# Portas preferidas: incomuns de proposito, para NAO colidir com outros apps que
# a pessoa possa ter rodando (8080/3000/5000/8000 sao muito disputadas — inclusive
# por outros paineis de IA locais). Tentamos nesta ordem; se todas ocupadas,
# varremos a partir da primeira.
PORTAS_PREFERIDAS = [8971, 8972, 8973, 8974, 8975]


def _porta_livre():
    """Devolve a primeira porta preferida que esteja de fato livre nesta maquina."""
    candidatas = list(PORTAS_PREFERIDAS) + list(range(8976, 8996))
    for p in candidatas:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, p)) != 0:      # nada escutando = livre
                return p
    return PORTAS_PREFERIDAS[0]


def _base_dir():
    """Pasta onde deixar o arquivo com o endereco: ao lado do .exe (empacotado)
    ou ao lado deste script (modo normal)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _salvar_endereco(url):
    """Grava o endereco do painel num .txt ao lado do programa, para o operador
    reencontrar o painel sem depender da janela preta."""
    try:
        alvo = _base_dir() / "ENDERECO_DO_PAINEL.txt"
        alvo.write_text(
            "PRESERVA-SCAN — endereco do painel\n\n"
            f"Abra este endereco no navegador:\n{url}\n\n"
            "(o programa precisa estar aberto; feche a janela preta para encerrar)\n",
            encoding="utf-8",
        )
        return alvo
    except Exception:
        return None


def _abrir_navegador(url):
    time.sleep(1.5)                                # da tempo do servidor subir
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    # Garante que os modulos irmaos (panel, scan, db) sejam importaveis tambem
    # quando rodando como script solto.
    base = os.path.dirname(os.path.abspath(__file__))
    if base not in sys.path:
        sys.path.insert(0, base)

    # Carrega DATABASE_URL (e afins) de um arquivo .env ao lado do programa, se
    # existir — assim o operador conecta o Supabase colando a string num arquivo,
    # sem mexer em variável de ambiente do Windows. Precisa rodar ANTES de importar
    # o painel (o db.py lê DATABASE_URL na importação).
    # override=True: o .env ao lado do .exe é a FONTE DA VERDADE. Sem isso, uma
    # variável de ambiente antiga (ex.: deixada por um 'setx DATABASE_URL ...' de
    # um teste anterior) venceria o .env e o operador ficaria conectando na string
    # velha sem entender por quê. Com override, o .env sempre manda.
    try:
        from dotenv import load_dotenv
        env_path = _base_dir() / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=True)
            print(f"  Configuração carregada de: {env_path.name}")
    except Exception:
        pass

    import uvicorn
    import panel

    porta = int(os.environ.get("PORT") or _porta_livre())
    url = f"http://{HOST}:{porta}"
    arq = _salvar_endereco(url)

    print("=" * 60)
    print("  PRESERVA-SCAN — painel de varredura (somente leitura)")
    print("")
    print(f"  >>> ABRA ESTE ENDERECO NO NAVEGADOR:  {url}")
    print("")
    print("  (o navegador deve abrir sozinho; se abrir a pagina errada,")
    print("   use EXATAMENTE o endereco acima)")
    if arq:
        print(f"  O endereco tambem foi salvo em: {arq.name} (ao lado do programa)")
    print("  Esta janela pode ficar minimizada.")
    print("  Para ENCERRAR o painel, feche esta janela.")
    print("=" * 60, flush=True)

    threading.Thread(target=_abrir_navegador, args=(url,), daemon=True).start()
    uvicorn.run(panel.app, host=HOST, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
