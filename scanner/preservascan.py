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

HOST = "127.0.0.1"


def _porta_livre(inicial=8080, tentativas=20):
    """Acha uma porta livre a partir de `inicial` (evita 'porta em uso' se ja
    houver algo no 8080)."""
    for p in range(inicial, inicial + tentativas):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, p)) != 0:      # nada escutando = livre
                return p
    return inicial


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

    import uvicorn
    import panel

    porta = int(os.environ.get("PORT") or _porta_livre())
    url = f"http://{HOST}:{porta}"

    print("=" * 58)
    print("  PRESERVA-SCAN — painel de varredura (somente leitura)")
    print(f"  Abra no navegador:  {url}")
    print("  Esta janela pode ficar minimizada.")
    print("  Para ENCERRAR o painel, feche esta janela.")
    print("=" * 58, flush=True)

    threading.Thread(target=_abrir_navegador, args=(url,), daemon=True).start()
    uvicorn.run(panel.app, host=HOST, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
