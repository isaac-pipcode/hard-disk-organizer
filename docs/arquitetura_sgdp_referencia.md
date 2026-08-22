---
titulo: "Arquitetura do Sistema de Gerenciamento de Dados de Preservação (SGDP) — Escola Porto Iracema das Artes"
persona: "ARQUITETA (PRESERV-BR/OPS, Persona 2), com consulta interna à ORÇA"
versao: "1.0.0"
data: "2026-07-16"
base_conhecimento: "dossie_preservacao_br.md v1.0.0 (hash pbr-2026-07-16-v1); proposta_tecnica_porto_iracema_v2.md; estudo SGDP/LUPA-UFF"
rota_definida: "software proprio (bespoke), substitutivo ao Tainacan — WP2/WP3 da v2"
destino: "diagramacao no Claude Design"
lingua: "pt-BR"
---

> **Nota para diagramação (Claude Design):** os blocos marcados com `🗺️ DIAGRAMA` são descrições prontas para virar peças visuais. Tabelas e blocos de código podem ser preservados como estão. As citações acadêmicas devem ser mantidas (requisito do cliente).

# Arquitetura do SGDP — Escola Porto Iracema das Artes

## 1. Propósito e princípios

Este documento especifica a arquitetura de um **Sistema de Gerenciamento de Dados de Preservação (SGDP)** para o acervo nato-digital da Porto Iracema das Artes: um software próprio que **varre discos e nuvem, estrutura a base de dados e entrega uma interface de consulta e gestão dinâmica**, substituindo o Tainacan por uma solução mais flexível, conforme decisão do cliente (proposta técnica v2, §3.2).

O projeto assenta-se em cinco princípios teóricos, herdados do DOSSIÊ (§2) e vinculantes para todas as decisões abaixo:

1. **Preservação é processo, não evento** — o modelo de referência OAIS (ISO 14721) trata um repositório como sistema de responsabilidades contínuas, não como depósito de arquivos. O SGDP existe para manter essas responsabilidades ao longo do tempo, não para "guardar uma vez".
2. **Autonomia do campo** (Edmondson, *Audiovisual Archiving: Philosophy and Principles*, 3. ed., UNESCO, 2016; trad. bras. 2017) — preservar audiovisual é manter conjuntamente suportes, equipamentos, competências e conteúdos; o software é apenas a camada de dados dessa manutenção, e por isso precisa ser legado com código e documentação (competência institucionalizada).
3. **Dualidade do objeto e disciplina matriz/acesso** (Cherchi Usai, *The Death of Cinema*, BFI, 2001) — a matriz de preservação não circula; o acesso público se dá por derivados (proxies). O modelo de dados e a interface separam rigorosamente master de proxy.
4. **Dados como camada de preservação** (PREMIS Data Dictionary v3, Library of Congress, 2015; FIAF *Moving Image Cataloguing Manual*, 2016) — um objeto sem checksum, proveniência e descrição recuperável está em desaparecimento silencioso. Fixity e eventos PREMIS são requisitos, não recursos opcionais.
5. **Anti-aprisionamento** (LOC, *Sustainability of Digital Formats*; OAIS) — nenhuma dependência de serviço ou formato do qual o acervo não possa sair. Este princípio incide **também sobre o próprio SGDP**: exportação padrão é obrigatória (§5).

---

## 2. O SGDP no modelo OAIS

O OAIS (ISO 14721) define seis entidades funcionais. O estudo de arquitetura do caso LUPA-UFF observa corretamente que o OAIS agrupa *funções*, não *componentes de implementação*, sendo neutro quanto a fundir ou desacoplar módulos. Adotamos, para uma equipe pequena, o **monólito modular**: uma única aplicação, com módulos internamente desacoplados por API — a mesma posição de engenharia que o estudo do LUPA recomenda para instituições de porte equivalente.

🗺️ **DIAGRAMA 1 — Mapa OAIS → módulos do SGDP** (fluxo horizontal, seis blocos funcionais)

```
   PRODUTOR                         SGDP (monólito modular)                        CONSUMIDOR
  (HDs, Drive)                                                                   (gestor, público)

  ┌──────────┐   SIP    ┌───────────────┐   AIP   ┌───────────────┐   DIP    ┌──────────────┐
  │ Arquivos │ ───────▶ │  INGESTÃO     │ ──────▶ │ ARMAZENAMENTO │ ───────▶ │   ACESSO     │
  │ nato-dig.│          │ (Módulo 1:    │         │ + GESTÃO DE   │          │ (Módulo 3:   │
  └──────────┘          │  Varredura)   │         │ DADOS         │          │  Interface)  │
                        └───────┬───────┘         │ (Módulo 2:    │          └──────┬───────┘
                                │                 │  Base+PREMIS) │                 │
                                ▼                 └───────┬───────┘                 ▼
                        [identificação de              │                    [busca facetada,
                         formato, fixidez,             ▼                     dashboard, proxies,
                         empacotamento]        ┌───────────────┐             perfis gestão/público]
                                               │ PLANEJAMENTO  │
                                               │ DE PRESERVAÇÃO│  ── fixity agendada, obsolescência,
                                               │ + ADMIN.      │     migração, backups do banco
                                               └───────────────┘
```

Legenda dos pacotes OAIS: **SIP** (pacote de submissão — o que a varredura produz por volume); **AIP** (pacote de arquivamento — matriz + proxies + metadados + laudos, empacotados em BagIt); **DIP** (pacote de disseminação — o que a interface pública serve, sempre derivados).

---

## 3. Arquitetura em três camadas

🗺️ **DIAGRAMA 2 — Camadas do SGDP** (três faixas horizontais empilhadas, com os componentes de cada uma; setas verticais indicando fluxo de baixo para cima na ingestão e de cima para baixo no acesso)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  CAMADA 3 — ACESSO (Módulo 3)                                                 │
│  Interface web própria · busca facetada · dashboard de gestão · player de     │
│  proxies (aviso de direitos) · perfis Gestão/Público · exportação DC/OAI-PMH  │
├─────────────────────────────────────────────────────────────────────────────┤
│  CAMADA 2 — DADOS (Módulo 2)                                                  │
│  PostgreSQL (esquema §9.3 do DOSSIÊ) · tabelas PREMIS (eventos/agentes/       │
│  direitos/localizações) · fila de transcodificação (FFmpeg → proxies H.264) · │
│  exportação agendada CSV/JSON (o catálogo também é objeto de preservação)     │
├─────────────────────────────────────────────────────────────────────────────┤
│  CAMADA 1 — COLETA (Módulo 1)                                                 │
│  rclone (discos + Google Drive) · Siegfried/PRONOM (formato) · MediaInfo/     │
│  ExifTool (metadados) · hashlib SHA-256 (fixidez) · Brunnhilde (triagem/      │
│  duplicatas) · bagit-python (SIP/AIP em BagIt RFC 8493)                       │
└─────────────────────────────────────────────────────────────────────────────┘
        ▲ ingestão (SIP→AIP)                              │ acesso (DIP) ▼
   [HDs físicos + Google Drive institucional]      [gestor interno · público liberado]
```

**Componentes acrescentados a partir do estudo do LUPA** (não constavam da pilha mínima do DOSSIÊ §8 e são incorporados aqui): **rclone** (MIT) — conectores para o Google Drive institucional e discos, com verificação de checksum nas transferências; **Siegfried/PRONOM** (Apache 2.0) — identificação de formato por PUID, base da política de obsolescência (DOSSIÊ §6.4 p.5); **Brunnhilde** (MIT) — relatório de triagem que consolida Siegfried e estatísticas de duplicatas, diretamente útil para quantificar a duplicação estimada do acervo (proposta v2, P4).

---

## 4. Requisitos funcionais

### RF1 — Varredura de discos e nuvem (Ingestão / SIP)

- Crawler de sistemas de arquivos locais e externos (os 30 HDs) e conector do **Google Drive institucional** via **rclone**, com escopo configurável e prioridade para o grupo "Antigos A–O" (maior risco, DOSSIÊ §6.2);
- Para cada arquivo: identificação de formato (**Siegfried/PRONOM**), extração de metadados técnicos (**MediaInfo/ExifTool**), cálculo de **SHA-256** (hashlib), detecção de duplicatas por hash, registro de caminho/volume de origem (proveniência PREMIS);
- Classificação preliminar automática: matriz candidata × proxy × duplicata/descartável (por formato e resolução);
- Saída: **SIP em BagIt (RFC 8493)** ou, no mínimo, **manifesto CSV/JSON por volume**;
- **Re-varredura agendada** = auditoria de fixidez incremental (comparação de hashes detecta novos, movidos e corrompidos);
- **Idempotência obrigatória**: re-executar a varredura sobre o mesmo volume não duplica registros — a chave é o par (checksum_sha256, caminho_origem).

### RF2 — Estruturação da base de dados (Gestão de Dados / AIP)

- Esquema = **modelo de dados do DOSSIÊ §9.3**, sem alteração de chaves (nível primário Item; campos obrigatórios e condicionais; vocabulários controlados; crosswalk Dublin Core / PBCore / PREMIS / FIAF-MIC) — ver DDL na §6;
- Tabelas PREMIS adicionais: `eventos_preservacao`, `agentes`, `localizacoes` (física e lógica: disco X, bucket Y), `direitos`;
- Itens físicos e digitais coexistem, vinculados por `vinculo_superior` (no caso nato-digital da Porto Iracema, o acervo é digital, mas o modelo permanece apto a receber eventual doação física futura);
- **Exportação íntegra e agendada** em CSV UTF-8 / JSON — o catálogo é ele próprio objeto de preservação (DOSSIÊ §6.5, p.7);
- API REST documentada; **OAI-PMH / Dublin Core** desejável [R/I] para difusão e para eventual migração a Tainacan/AtoM.

### RF3 — Interface de visualização e acervo dinâmico (Acesso / DIP)

- Navegação **facetada** por vocabulários controlados (categoria de suporte, formato, estado/integridade, direitos, década);
- **Dashboard de gestão** (indicadores na §7);
- Gráficos interativos: crescimento do acervo, distribuição por formato, semáforo de risco, treemap por coleção/fundo;
- **Player de proxies** com aviso de direitos; **matrizes nunca são servidas pela interface pública** (disciplina matriz/acesso — Cherchi Usai, 2001);
- Dois perfis: **Gestão** (acervo integral) e **Público** (apenas itens com `direitos_status` ∈ {dominio_publico, autorizado} e sem dados pessoais sensíveis — ver LGPD na §5).

---

## 5. Requisitos não-funcionais

| # | Requisito | Descrição | Fonte/critério |
|---|---|---|---|
| RNF1 | **Fixidez** | SHA-256 na criação; auditoria agendada (semestral [R] / anual [M]) via re-varredura; cada verificação gera evento PREMIS | DOSSIÊ §6.4 |
| RNF2 | **Backup do próprio sistema** | O banco PostgreSQL e os manifestos entram na regra 3-2-1; *dump* diário versionado; o catálogo é objeto de preservação | DOSSIÊ §6.5 p.7 |
| RNF3 | **Autenticação e perfis** | Perfil Gestão (autenticado) × Público (anônimo, somente leitura de itens liberados) | Boa prática OAIS (Administração) |
| RNF4 | **LGPD** (Lei 13.709/2018) | Obras de alunos, dados de doadores (`fonte_aquisicao`) e eventuais depoimentos contêm dados pessoais: minimização na camada pública, base legal/consentimento para publicação de pessoas identificáveis, campo de restrição por item | Lei 13.709/2018; distinta da questão autoral (Lei 9.610/1998, DOSSIÊ §4.2) |
| RNF5 | **Anti-aprisionamento (aplica-se ao próprio SGDP)** | Código-fonte aberto entregue à escola; documentação técnica e de usuário; **exportação padrão obrigatória** (CSV/JSON/Dublin Core, OAI-PMH desejável); esquema aderente 1:1 ao §9.3 → o acervo catalogado migra sem perdas para Tainacan/AtoM/Archivematica se o sistema for descontinuado | LOC/OAIS; proposta v2 §3.2 |
| RNF6 | **Sustentabilidade / porte** | Curva de aprendizado baixa para a equipe; *stack* enxuto operável por equipe não especializada; hospedagem em VPS modesto | DOSSIÊ §8 (recomendação de pilha mínima) |
| RNF7 | **Formatos fixados** | FFV1/MKV e DPX como matrizes (quando aplicável); WAV/BWF para áudio; H.264/MP4 para proxies; nada proprietário como matriz | DOSSIÊ §6.3–6.4 |

---

## 6. Esquema de dados executável (DDL)

Derivado 1:1 do modelo do DOSSIÊ §9.3. Chaves em snake_case, sem acentos. PostgreSQL. Campos físicos (ex.: `base_suporte`, `sindrome_vinagre_nivel`) permanecem no esquema por fidelidade ao modelo e por abertura a doações físicas futuras, ainda que nulos no acervo nato-digital atual.

🗺️ **DIAGRAMA 3 — Modelo entidade-relacionamento** (entidade central `itens` ao centro; `itens` liga-se a si mesma por `vinculo_superior` (auto-relacionamento Obra↔Item); relações 1:N de `itens` para `eventos_preservacao`, `localizacoes`, `direitos`; `eventos_preservacao` liga-se a `agentes` por N:1)

```sql
-- Tabela central: nivel primario = Item (FIAF-MIC)
CREATE TABLE itens (
    identificador           UUID PRIMARY KEY,
    titulo                  TEXT NOT NULL,
    titulo_alternativo      TEXT,
    nivel_descricao         TEXT NOT NULL DEFAULT 'item',      -- item|manifestacao|variante|obra
    vinculo_superior        UUID REFERENCES itens(identificador),
    data_producao           TEXT,                              -- data ou intervalo estimado
    creditos                TEXT,
    descricao_conteudo      TEXT,
    assuntos                TEXT[],                            -- vocabulario controlado
    categoria_suporte       TEXT NOT NULL,                     -- ver vocab. abaixo
    bitola_formato          TEXT NOT NULL,
    base_suporte            TEXT,                              -- nato-digital: normalmente nulo
    cor                     TEXT,
    som                     TEXT,
    duracao_metragem        TEXT,
    geracao_elemento        TEXT,
    estado_conservacao      TEXT NOT NULL DEFAULT 'nao_avaliado',
    sindrome_vinagre_nivel  TEXT,                              -- nato-digital: nulo
    localizacao_fisica      TEXT,
    formato_digital         TEXT,                              -- ex.: FFV1/MKV, DPX, WAV/BWF, MP4
    resolucao_amostragem    TEXT,
    tamanho_bytes           BIGINT,
    checksum_sha256         CHAR(64),
    data_ultima_fixity      TIMESTAMPTZ,
    direitos_status         TEXT NOT NULL DEFAULT 'indeterminado',
    fonte_aquisicao         TEXT,                              -- doador/procedencia + instrumento juridico
    puid_formato            TEXT,                              -- PRONOM (Siegfried) — acrescimo tecnico
    caminho_origem          TEXT,                              -- proveniencia da varredura
    notas                   TEXT,
    criado_em               TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- PREMIS: eventos de preservacao (digitalizacao, migracao, fixity_check, reparo, ingest)
CREATE TABLE eventos_preservacao (
    id              BIGSERIAL PRIMARY KEY,
    item_id         UUID NOT NULL REFERENCES itens(identificador) ON DELETE CASCADE,
    tipo_evento     TEXT NOT NULL,                             -- ingest|fixity_check|migracao|proxy|reparo
    data_evento     TIMESTAMPTZ NOT NULL DEFAULT now(),
    agente_id       BIGINT REFERENCES agentes(id),
    resultado       TEXT,                                      -- sucesso|falha|alerta
    detalhe         TEXT
);

-- PREMIS: agentes (pessoa, software, organizacao)
CREATE TABLE agentes (
    id       BIGSERIAL PRIMARY KEY,
    nome     TEXT NOT NULL,
    tipo     TEXT NOT NULL,                                    -- pessoa|software|organizacao
    papel    TEXT
);

-- Localizacoes fisicas e logicas (disco, LTO, bucket)
CREATE TABLE localizacoes (
    id                 BIGSERIAL PRIMARY KEY,
    item_id            UUID NOT NULL REFERENCES itens(identificador) ON DELETE CASCADE,
    tipo               TEXT NOT NULL,                          -- fisica|logica
    meio               TEXT,                                   -- disco|lto|bucket|nas
    identificador_meio TEXT,                                   -- ex.: "HD Antigos-C", "bucket-frio-01"
    caminho            TEXT,
    e_copia_canonica   BOOLEAN DEFAULT false
);

-- Detalhamento de direitos (complementa itens.direitos_status)
CREATE TABLE direitos (
    id             BIGSERIAL PRIMARY KEY,
    item_id        UUID NOT NULL REFERENCES itens(identificador) ON DELETE CASCADE,
    tipo           TEXT,                                       -- autoral|imagem|lgpd
    titular        TEXT,
    instrumento    TEXT,                                       -- termo de cessao/autorizacao
    observacao     TEXT
);

-- Vocabularios controlados (conforme DOSSIE §9.3)
-- categoria_suporte: pelicula|fita_video|fita_audio|disco|arquivo_digital|outro
-- base_suporte:      nitrato|acetato|poliester|magnetico|optico|desconhecido
-- estado_conservacao:otimo|bom|regular|ruim|critico|nao_avaliado
-- direitos_status:   dominio_publico|autorizado|restrito|indeterminado
```

---

## 7. Especificação do módulo de varredura (RF1)

🗺️ **DIAGRAMA 4 — Fluxo da varredura** (fluxograma vertical, do volume de entrada ao manifesto/SIP)

```
  Volume (HD ou Google Drive)
          │
          ▼
  [1] rclone/os.scandir ── enumera arquivos AV, preserva timestamps, verifica transferencia
          │
          ▼
  [2] Siegfried/PRONOM ── identifica formato (PUID) → itens.puid_formato
          │
          ▼
  [3] MediaInfo/ExifTool ── extrai metadados tecnicos → itens.formato_digital, resolucao...
          │
          ▼
  [4] hashlib SHA-256 ── calcula checksum → itens.checksum_sha256
          │
          ▼
  [5] Dedup por hash ── se checksum ja existe: registra como localizacao adicional, NAO novo item
          │
          ▼
  [6] Classificacao ── matriz candidata | proxy | duplicata (por formato/resolucao)
          │
          ▼
  [7] Brunnhilde ── relatorio de triagem do volume (formatos, datas, duplicatas)
          │
          ▼
  [8] bagit-python ── empacota SIP (BagIt/RFC 8493) + evento PREMIS "ingest"
          │
          ▼
  Saida: manifesto CSV/JSON por volume  +  SIP em BagIt
```

**Contrato do módulo (CLI):**
- **Entrada:** caminho do volume (ou remote rclone), perfil de escopo, flag de prioridade.
- **Saída:** manifesto (`manifesto_<volume>_<data>.csv/json`), SIP BagIt opcional, log de execução.
- **Idempotência:** a chave (checksum_sha256, caminho_origem) impede duplicação de registros em re-execuções; arquivo já ingerido com mesmo hash e caminho é ignorado; hash novo em caminho conhecido → alerta de corrupção; mesmo hash em novo caminho → nova `localizacao`.
- **Tratamento de erro:** volume ilegível (ex.: os 2 HDs corrompidos) → registra falha, segue para o próximo, sinaliza para diagnóstico; nunca interrompe a varredura inteira por um item.

---

## 8. Especificação do dashboard (RF3)

Indicadores com fórmula de cálculo explícita. Para o acervo nato-digital, os indicadores de risco do DOSSIÊ (pirâmide de síndrome do vinagre) são substituídos por **risco de redundância e de obsolescência de formato**.

| Indicador | Fórmula | Leitura |
|---|---|---|
| % inventariado | `itens_registrados / itens_estimados` | avanço da varredura |
| % com integridade | `itens com checksum_sha256 ≠ nulo / total` | cobertura de fixidez |
| % catalogado (descritivo) | `itens com descricao_conteudo ≠ nulo / total` | avanço da descrição |
| Taxa de duplicação | `1 − (hashes_distintos / arquivos_totais)` | quanto se pode consolidar |
| Itens sem redundância 3-2-1 | `count(itens com < 3 localizacoes OU sem copia remota)` | risco de perda |
| Itens com fixidez vencida | `count(data_ultima_fixity < now − N meses)` | dívida de auditoria |
| Formatos em risco | `count(puid_formato ∈ lista_obsoletos)` | risco de obsolescência |
| Armazenado × capacidade | `soma(tamanho_bytes) / capacidade_total` | ocupação |
| Distribuição por direitos | `group by direitos_status` | o que pode ir a público |
| Crescimento do acervo | série temporal por `criado_em` | ritmo de ingestão |

🗺️ **DIAGRAMA 5 — Wireframe do dashboard de gestão** (uma tela; faixa superior de KPIs; grade de gráficos; tabela ao pé)

```
┌───────────────────────────────────────────────────────────────────────┐
│  SGDP · PORTO IRACEMA — Painel de Gestão            [Gestão ▼] [sair]   │
├───────────────┬───────────────┬───────────────┬───────────────────────┤
│ % INVENTARIADO│ % INTEGRIDADE │ TAXA DUPLIC.  │ SEM REDUNDÂNCIA 3-2-1  │
│     72%       │     91%       │     38%       │     ▲ 214 itens        │
├───────────────┴───────────────┴───────┬───────┴───────────────────────┤
│  CRESCIMENTO DO ACERVO (série temporal)│  DISTRIBUIÇÃO POR DIREITOS     │
│  ▁▂▃▄▅▆▇                                │  ◐ pizza (público/restrito/…)  │
├────────────────────────────────────────┼────────────────────────────────┤
│  FORMATOS EM RISCO (barras)            │  MAPA DE LOCALIZAÇÕES (discos)  │
│  ▇▇▅▃▁                                  │  ▢▢▢▣▢ por HD/bucket           │
├────────────────────────────────────────┴────────────────────────────────┤
│  TABELA: itens com fixidez vencida  ·  [exportar CSV] [exportar DC/OAI] │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 9. Inventário de componentes reutilizados

Todos livres, sem *lock-in*. O desenvolvimento próprio concentra-se na **cola** (orquestração dos componentes), no **modelo de dados §9.3**, no **dashboard** e na **interface substitutiva ao Tainacan** — não em reescrever o que já existe (princípio de projeto da Persona 2).

| Componente | Módulo | Função | Licença | Custo |
|---|---|---|---|---|
| rclone | 1 | Conectores discos + Google Drive, checksum | MIT | 0 |
| Siegfried / PRONOM | 1 | Identificação de formato (PUID) | Apache 2.0 | 0 |
| MediaInfo | 1 | Metadados técnicos AV | BSD | 0 |
| ExifTool | 1 | Metadados de imagens/arquivos | Artistic | 0 |
| hashlib (Python) | 1 | SHA-256 | PSF | 0 |
| Brunnhilde | 1 | Relatório de triagem/duplicatas | MIT | 0 |
| bagit-python | 1 | Empacotamento BagIt (RFC 8493) | CC0/LC | 0 |
| PostgreSQL | 2 | Banco relacional (esquema §9.3) | PostgreSQL Lic. | 0 |
| FFmpeg | 2 | Geração de proxies H.264 | LGPL/GPL | 0 |
| **Interface própria (WP3)** | 3 | Catálogo facetado + dashboard + player + perfis + export DC/OAI | proprietária da escola (código aberto entregue) | desenvolvimento (v2) |

---

## 10. Roteiro de implantação incremental

Alinhado às 14 semanas do cronograma (v2, §5) e aos pacotes WP2 (varredura) e WP3 (catálogo/acesso). Esforço em semanas-pessoa `[ESTIMATIVA — validar na execução]`.

| Versão | Escopo | Semanas | Esforço est. |
|---|---|---|---|
| **MVP** | Módulo de varredura (RF1) completo: rclone + Siegfried + MediaInfo + hashlib + Brunnhilde + bagit-python; manifestos CSV/JSON; export | 1–4 | ~3 sem-pessoa |
| **v1** | Banco PostgreSQL (§9.3) + ingestão dos manifestos + catálogo com busca facetada + geração de proxies (FFmpeg) + perfis de autenticação | 5–9 | ~5 sem-pessoa |
| **v2** | Dashboard de gestão + fixidez agendada + exportação Dublin Core/OAI-PMH + camada pública com controles LGPD + difusão | 10–14 | ~4 sem-pessoa |

Marco de aceite por versão: MVP entrega o **diagnóstico do acervo** (volumetria, duplicação, risco por disco); v1 entrega o **catálogo operacional**; v2 entrega a **gestão e a difusão**.

---

## 11. Decisão de arquitetura registrada

**Rota escolhida (pelo cliente): software próprio substitutivo ao Tainacan.** Justificativa: a rigidez do Tainacan é crítica corrente do setor audiovisual; a escola deseja uma ferramenta mais flexível e sob medida (v2, §3.2).

**Alternativa avaliada e não adotada:** reuso do **CollectiveAccess** como base do catálogo (rota sugerida pelo estudo do LUPA para equipes pequenas). Registro, para governança, que essa alternativa reduziria código próprio ao preço de curva de configuração alta e de um *stack* mais pesado — decisão legítima do cliente por não a adotar, mantida aqui documentada caso se queira revisitar.

**Condições que tornam a rota própria segura (RNF5, invioláveis):** código aberto + documentação + exportação padrão obrigatória + aderência 1:1 ao §9.3. Com elas, o SGDP deixa de ser ponto único de falha: se a manutenção cessar, o acervo migra sem perdas. É o mesmo critério anti-aprisionamento que o DOSSIÊ aplica a qualquer ferramenta — aplicado, por coerência, ao software que nós mesmos entregamos.

---

## 12. Anexo machine-readable

```json
{
  "schema_versao": "1.0",
  "sistema": "sgdp_porto_iracema",
  "rota": "software_proprio_substituto_tainacan",
  "modelo_dados": "dossie_s9_3",
  "arquitetura": "monolito_modular_oais_aware",
  "modulos": [
    {"id": 1, "nome": "varredura_ingest", "oais": "ingest", "componentes": ["rclone","siegfried","mediainfo","exiftool","hashlib","brunnhilde","bagit-python"]},
    {"id": 2, "nome": "base_dados_premis", "oais": ["archival_storage","data_management","preservation_planning"], "componentes": ["postgresql","ffmpeg"]},
    {"id": 3, "nome": "interface_acesso", "oais": "access", "componentes": ["interface_propria_wp3"]}
  ],
  "requisitos_nao_funcionais": ["fixidez","backup_do_banco","autenticacao","lgpd","anti_aprisionamento","sustentabilidade","formatos_fixados"],
  "anti_aprisionamento": {"codigo_aberto": true, "export_obrigatorio": ["csv","json","dublin_core"], "oai_pmh": "desejavel", "aderencia_modelo": "dossie_s9_3"},
  "roadmap": [
    {"versao": "mvp", "semanas": "1-4", "esforco_sem_pessoa": 3, "entrega": "diagnostico_acervo"},
    {"versao": "v1", "semanas": "5-9", "esforco_sem_pessoa": 5, "entrega": "catalogo_operacional"},
    {"versao": "v2", "semanas": "10-14", "esforco_sem_pessoa": 4, "entrega": "gestao_e_difusao"}
  ],
  "componentes_reuso": [
    {"nome": "rclone", "modulo": 1, "licenca": "mit", "custo": 0},
    {"nome": "siegfried_pronom", "modulo": 1, "licenca": "apache2", "custo": 0},
    {"nome": "mediainfo", "modulo": 1, "licenca": "bsd", "custo": 0},
    {"nome": "exiftool", "modulo": 1, "licenca": "artistic", "custo": 0},
    {"nome": "hashlib", "modulo": 1, "licenca": "psf", "custo": 0},
    {"nome": "brunnhilde", "modulo": 1, "licenca": "mit", "custo": 0},
    {"nome": "bagit_python", "modulo": 1, "licenca": "cc0", "custo": 0},
    {"nome": "postgresql", "modulo": 2, "licenca": "postgresql", "custo": 0},
    {"nome": "ffmpeg", "modulo": 2, "licenca": "lgpl_gpl", "custo": 0}
  ]
}
```

---

*Documento de arquitetura da Persona ARQUITETA (PRESERV-BR/OPS). Referências teóricas: OAIS/ISO 14721; PREMIS v3 (LOC, 2015); FIAF Moving Image Cataloguing Manual (2016); Edmondson (UNESCO, 2016/2017); Cherchi Usai (BFI, 2001); Freire (Imagofagia n. 22, 2020); DOSSIÊ §§2, 5, 6, 8, 9; estudo SGDP/LUPA-UFF. Estruturado para diagramação no Claude Design.*
