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
