"""
Persistencia do PRESERVA-SCAN.
Envia o inventario ao PostgreSQL hospedado (Supabase) e controla idempotencia.
Se DATABASE_URL nao estiver definido, opera em modo offline (so manifesto local).
"""
import os
import json
import datetime

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

DATABASE_URL = os.environ.get("DATABASE_URL")  # ex.: postgresql://...supabase.co:5432/postgres


def conectado():
    return bool(DATABASE_URL and psycopg2)


def _conn():
    return psycopg2.connect(DATABASE_URL)


def _amigavel(erro):
    """Traduz erros comuns do psycopg2 numa mensagem que o operador entende."""
    e = (erro or "").lower()
    if "could not translate host name" in e or "name or service not known" in e:
        return ("Endereço do servidor não encontrado. Confira se copiou a string "
                "inteira e se a senha não tem caracteres especiais (@ : / #) que "
                "quebram o endereço — use uma senha só com letras e números.")
    if "password authentication failed" in e:
        return "Senha incorreta. Confira a senha do banco no Supabase."
    if "tenant or user not found" in e:
        return ("Usuário/projeto não encontrado — a string parece incompleta. "
                "Copie a string do 'Session pooler' inteira (postgres.SEU-REF:SENHA@...).")
    if "timeout" in e or "could not connect" in e or "connection refused" in e:
        return ("Não foi possível conectar (sem internet, ou host/porta errados). "
                "Confira a conexão e a string do Supabase.")
    if "does not exist" in e and "database" in e:
        return "Banco de dados não existe nessa string — confira o final (…/postgres)."
    return f"Falha ao conectar: {erro}"


def testar(url):
    """Testa uma string de conexão sem alterar nada. Devolve (ok, mensagem)."""
    if not psycopg2:
        return False, "Esta versão do programa não tem suporte a banco (psycopg2 ausente)."
    url = (url or "").strip()
    if not url:
        return False, "Cole a string de conexão do Supabase."
    try:
        c = psycopg2.connect(url, connect_timeout=8)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            c.close()
        return True, ""
    except Exception as e:
        return False, _amigavel(str(e))


def configurar(url):
    """Passa a usar `url` como conexão AGORA (em memória), sem reiniciar o programa.
    String vazia = volta ao modo offline. Devolve True se ficou conectado."""
    global DATABASE_URL
    DATABASE_URL = (url or "").strip() or None
    os.environ["DATABASE_URL"] = DATABASE_URL or ""
    return conectado()


def registrar_disco(label, capacidade_tb=None, grupo=None):
    if not conectado():
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO discos (label, capacidade_tb, grupo, status, varrido_em)
               VALUES (%s,%s,%s,'varrendo', now())
               ON CONFLICT (label) DO UPDATE SET status='varrendo'""",
            (label, capacidade_tb, grupo),
        )


def concluir_disco(label, status="concluido"):
    if not conectado():
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE discos SET status=%s, varrido_em=now() WHERE label=%s",
            (status, label),
        )


def ja_varridos(disco_label):
    """Retorna dict {caminho: (tamanho, mtime_iso)} do que ja foi lido — base da idempotencia."""
    if not conectado():
        return {}
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT caminho, tamanho_bytes, mtime FROM arquivos WHERE disco_label=%s",
            (disco_label,),
        )
        return {r[0]: (r[1], r[2].isoformat() if r[2] else None) for r in cur.fetchall()}


def abrir_run(run_id, disco_label):
    if not conectado():
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO scan_runs (id, disco_label, status) VALUES (%s,%s,'rodando')",
            (run_id, disco_label),
        )


def fechar_run(run_id, novos, lidos, status="concluido"):
    if not conectado():
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """UPDATE scan_runs SET finalizado_em=now(), arquivos_novos=%s,
               arquivos_lidos=%s, status=%s WHERE id=%s""",
            (novos, lidos, status, run_id),
        )


def enviar_lote(linhas):
    """Faz upsert de um lote de arquivos. `linhas` = lista de dicts."""
    if not conectado() or not linhas:
        return
    cols = ["disco_label", "caminho", "nome", "extensao", "tamanho_bytes",
            "mtime", "sha256", "puid", "formato", "mediainfo", "scan_run_id"]
    valores = [[l.get(k) if k != "mediainfo" else json.dumps(l.get(k)) for k in cols] for l in linhas]
    with _conn() as c, c.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            f"""INSERT INTO arquivos ({','.join(cols)}) VALUES %s
                ON CONFLICT (disco_label, caminho) DO UPDATE SET
                  tamanho_bytes=EXCLUDED.tamanho_bytes, mtime=EXCLUDED.mtime,
                  sha256=EXCLUDED.sha256, puid=EXCLUDED.puid, formato=EXCLUDED.formato,
                  mediainfo=EXCLUDED.mediainfo, scan_run_id=EXCLUDED.scan_run_id""",
            valores,
        )
