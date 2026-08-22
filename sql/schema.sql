-- PRESERVA-SCAN — esquema da fase de VARREDURA (inventario)
-- Roda no Supabase (ou qualquer PostgreSQL). Este e o inventario bruto da E1;
-- ele alimenta, na fase de catalogacao (E3), o modelo completo do DOSSIE §9.3.

-- Registro dos discos varridos
CREATE TABLE IF NOT EXISTS discos (
    label            TEXT PRIMARY KEY,          -- etiqueta do HD (ex.: "Antigos-C")
    capacidade_tb    NUMERIC,
    grupo            TEXT,                        -- antigos | uso_continuo | transporte
    status           TEXT DEFAULT 'pendente',     -- pendente | varrendo | concluido | erro
    varrido_em       TIMESTAMPTZ
);

-- Inventario arquivo a arquivo (uma linha por arquivo por disco)
CREATE TABLE IF NOT EXISTS arquivos (
    id             BIGSERIAL PRIMARY KEY,
    disco_label    TEXT NOT NULL REFERENCES discos(label),
    caminho        TEXT NOT NULL,               -- caminho relativo dentro do disco
    nome           TEXT NOT NULL,
    extensao       TEXT,
    tamanho_bytes  BIGINT,
    mtime          TIMESTAMPTZ,                 -- data de modificacao do arquivo
    sha256         CHAR(64),                    -- impressao digital do CONTEUDO
    puid           TEXT,                        -- identificador de formato (Siegfried/PRONOM)
    formato        TEXT,                        -- nome do formato
    mediainfo      JSONB,                       -- metadados tecnicos (codec, resolucao...)
    scan_run_id    UUID,
    criado_em      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (disco_label, caminho)               -- idempotencia: 1 registro por arquivo/disco
);

CREATE INDEX IF NOT EXISTS idx_arquivos_sha256 ON arquivos(sha256);
CREATE INDEX IF NOT EXISTS idx_arquivos_disco  ON arquivos(disco_label);

-- Historico de execucoes de varredura
CREATE TABLE IF NOT EXISTS scan_runs (
    id              UUID PRIMARY KEY,
    disco_label     TEXT REFERENCES discos(label),
    iniciado_em     TIMESTAMPTZ DEFAULT now(),
    finalizado_em   TIMESTAMPTZ,
    arquivos_novos  INTEGER DEFAULT 0,
    arquivos_lidos  INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'rodando'       -- rodando | concluido | erro
);

-- VISAO: analise de duplicidade (a pergunta "esta em 1 ou em 2+ discos?")
-- Um mesmo SHA-256 em varios discos = conteudo redundante. SHA unico = copia unica (em risco).
CREATE OR REPLACE VIEW duplicidade AS
SELECT
    sha256,
    count(*)                          AS total_copias,
    count(DISTINCT disco_label)       AS discos_distintos,
    array_agg(DISTINCT disco_label)   AS onde,
    max(tamanho_bytes)                AS tamanho_bytes,
    CASE WHEN count(DISTINCT disco_label) >= 2 THEN 'redundante'
         ELSE 'copia_unica' END       AS situacao
FROM arquivos
WHERE sha256 IS NOT NULL
GROUP BY sha256;

-- VISAO: resumo por disco (para o dashboard)
CREATE OR REPLACE VIEW resumo_discos AS
SELECT d.label, d.grupo, d.status, d.varrido_em,
       count(a.id)              AS arquivos,
       COALESCE(sum(a.tamanho_bytes),0) AS bytes_totais
FROM discos d LEFT JOIN arquivos a ON a.disco_label = d.label
GROUP BY d.label, d.grupo, d.status, d.varrido_em;
