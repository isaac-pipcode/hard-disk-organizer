"""Versão do programa. O build (GitHub Actions) grava scanner/_versao.py com a
tag da release (ex.: v0.8.2) antes de empacotar o .exe; rodando do código-fonte,
vale "desenvolvimento". Aparece no rodapé do menu e vai dentro de cada pacote de
envio — para saber com que versão cada inventário foi feito."""
try:
    from _versao import VERSAO  # gerado no build
except Exception:
    VERSAO = "desenvolvimento"
