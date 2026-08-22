# HANDOFF — PRESERVA-SCAN → Claude Code

> Ponto de entrada para dar continuidade ao desenvolvimento no Claude Code.
> Leia este arquivo primeiro; ele referencia os demais artefatos do pacote.

## 1. O que é o projeto

Software de preservação digital para acervos audiovisuais nato-digitais, em
desenvolvimento como **produto proprietário** (uso interno da consultoria,
reutilizando componentes livres) com **piloto na Escola Porto Iracema das
Artes** (Fortaleza-CE). Objetivo de médio prazo: oferecer o serviço a outras
instituições culturais brasileiras.

Duas peças, arquitetura monólito-modular:
1. **Módulo de varredura (Ingest)** — em desenvolvimento agora, é o foco desta
   entrega. Lê discos/nuvem, identifica formato, extrai metadados, calcula
   checksum, detecta duplicatas, empacota. **Somente leitura sobre o acervo.**
2. **Módulo de catálogo/acesso** — ainda não iniciado. Vem depois do piloto de
   varredura validar-se em campo.

## 2. Estado atual do código (o que já funciona, testado)

Pasta `scanner/` + arquivos de raiz. Testado localmente (dados sintéticos) e
em campo pelo cliente num HD real de 2,6 TB / 124.906 arquivos (Windows).

| Arquivo | Função | Status |
|---|---|---|
| `scanner/scan.py` | Orquestrador da varredura: hash SHA-256, Siegfried **em lote** (1 chamada por disco, não por arquivo — otimização crítica, ver §4), MediaInfo/ExifTool restritos por extensão, manifesto CSV+JSON, `--dry-run` para estimar tempo/volume antes de rodar | ✅ funcional, testado |
| `scanner/db.py` | Persistência no Postgres/Supabase; idempotência por `(disco_label, caminho)` | ✅ funcional; **idempotência não testada em campo ainda** (cliente testou offline) |
| `scanner/panel.py` | Painel web local (FastAPI, porta 8080): detecção automática de discos montados, seleção por menu, disparo da varredura, log ao vivo | ✅ funcional, sintaxe validada; **não testado em campo** |
| `scanner/templates/index.html` | UI do painel (form com dropdown de discos) | ✅ funcional |
| `sql/schema.sql` | Schema Postgres: tabelas `discos`, `arquivos`, `scan_runs`; views `duplicidade` e `resumo_discos` | ✅ pronto; **não aplicado ainda no Supabase real do cliente** |
| `Dockerfile` / `docker-compose.yml` | Empacotamento (rclone, mediainfo, exiftool, siegfried, python) | ⚠️ criado mas **não testado end-to-end**; ver §5 (risco) |
| `iniciar_painel.bat` | Launcher Windows por duplo-clique (sem terminal) | ✅ criado; **não testado em campo** |
| `.env.example` | Template de `DATABASE_URL` | ok |

## 3. Decisões de arquitetura já fechadas (não reabrir sem motivo novo)

- **Monólito modular**, não microsserviços — equipe pequena, um só sistema com módulos internos desacoplados por API.
- **Varredura é somente-leitura**, sempre. Discos montados `:ro` no Docker. Nenhuma escrita/movimentação no acervo original nesta fase.
- **Identidade de arquivo = SHA-256 do conteúdo**, não nome/caminho. Duplicidade = mesmo hash em 2+ discos.
- **Idempotência por `(disco_label, caminho)`**: mesmo tamanho+mtime → pula; hash diferente no mesmo caminho → alerta de alteração/corrupção.
- **Siegfried em lote** (uma chamada `sf -json <raiz>` por disco), não por arquivo — motivado por medição real: ~125 mil arquivos por-arquivo estimava dobrar o tempo de varredura (~6h32 → ~12-13h). Não reverter para chamada por-arquivo.
- **MediaInfo só em extensões AV; ExifTool só em imagem**; demais arquivos recebem apenas hash+formato — mesma motivação de performance.
- **Separação de camadas no deploy:** scanner roda **local** (máquina com os discos); dashboard analítico deve rodar no **Vercel**, lendo do **Supabase** — nunca o inverso (Vercel é serverless, sem acesso a disco local/USB).
- **Licenciamento (para produtização futura):** ferramentas chamadas via subprocess/CLI (rclone MIT, Siegfried Apache-2.0, MediaInfo BSD, ExifTool Artistic, FFmpeg como binário) podem compor produto proprietário. **Não embutir Archivematica/AtoM** (AGPL-3.0 — copyleft de rede). Validar com advogado de PI antes de comercializar.
- **Software NÃO será cedido à Porto Iracema** (decisão de negócio, orçamento reduzido a R$25k) — mas a **exportação aberta dos dados (CSV/JSON/Dublin Core) é cláusula contratual obrigatória**, para não recriar aprisionamento. Isso deve ser respeitado em qualquer decisão de produto: exportação nunca é feature opcional.
- **Modelo de dados de catalogação** (fase de catálogo, ainda não implementada) segue um esquema de referência com nível primário "Item", crosswalk Dublin Core/PBCore/PREMIS/FIAF — a integrar quando o módulo de catálogo começar. O `schema.sql` atual é só da fase de inventário/varredura, não o modelo de catálogo completo.

## 4. Achado de campo mais importante (não repetir o erro)

Teste real do cliente (Windows, HD USB 2,6TB/124.906 arquivos) expôs que
chamar Siegfried e MediaInfo **por arquivo** não escala: overhead de criação
de processo no Windows (~100-250ms cada) multiplicado por ~125k arquivos
adicionava horas. Corrigido rodando Siegfried **uma vez sobre a árvore
inteira** e restringindo MediaInfo/ExifTool por extensão. Groselha para
qualquer nova ferramenta que se adicione ao pipeline: **sempre preferir
chamada em lote sobre a árvore a chamada por-arquivo**, quando a ferramenta
suportar.

## 5. Riscos e pendências conhecidas (não testado / incerto)

- **`Dockerfile`: instalação do Siegfried via `.deb` não testada de fato** — o passo usa uma URL de release do GitHub que pode não corresponder ao nome real do artefato/versão; testar o build e ajustar `SF_VERSION` e o nome do pacote antes de depender dele em campo. Cliente instalou Siegfried nativamente no Windows (fora do Docker) via binário do GitHub — esse caminho funcionou e está documentado no histórico da conversa, mas não foi automatizado.
- **Idempotência real (2ª passada = "0 novos") ainda não validada em campo** — só testada offline, onde não há banco para comparar. Cliente ainda não rodou com `DATABASE_URL` configurado. **Próximo teste crítico.**
- **`panel.py` (painel web com seleção de disco por botão) não testado em máquina real** — feito sob pressão de prazo (viagem em ~1 semana a partir do handoff). Testar detecção de discos no Windows real (a lógica usa `A:` a `Z:` verificando `os.path.exists`, deve funcionar, mas não foi validado).
- **`iniciar_painel.bat` não testado** — depende de `python -m uvicorn` estar no PATH do Windows do cliente.
- **`schema.sql` não aplicado no Supabase real do cliente ainda.**
- **Ambiente de execução do cliente:** Windows, PowerShell, sem Docker confirmado em uso (rodou `python3 scan.py` nativo). Ferramentas confirmadas instaladas: Python 3, Siegfried 1.11.6 (nativo, via binário GitHub, PATH configurado em `C:\Tools\siegfried`), Scoop (gerenciador de pacotes). MediaInfo/ExifTool/rclone — **instalação não confirmada nesta conversa**; scan.py degrada com elegância se ausentes (`shutil.which`), mas confirmar antes da viagem.
- **Volume real do acervo da escola:** ~30 TB estimados sobre 99 TB de capacidade em 30 HDs; 2 HDs corrompidos/ilegíveis (envio a laboratório de diagnóstico recomendado, ainda não confirmado se foi feito).

## 6. Próximos passos técnicos (ordem sugerida)

1. **Testar `panel.py` + `iniciar_painel.bat` em máquina Windows real** do cliente ou similar — é o que ele vai operar em campo.
2. **Configurar Supabase real**: aplicar `sql/schema.sql`, preencher `.env`, rodar uma varredura com `DATABASE_URL` ativo, confirmar que a 2ª passada sobre o mesmo disco resulta em "0 novos" (idempotência) e que a view `duplicidade` responde corretamente.
3. **Testar/corrigir o build Docker** (Siegfried via apt/deb) — ou decidir formalmente abandonar Docker para a fase de campo e manter instalação nativa (Windows) como caminho oficial do piloto, documentando isso.
4. **Confirmar instalação de MediaInfo, ExifTool, rclone** na(s) máquina(s) que irão a campo.
5. **Iniciar o scaffold do dashboard (Vercel + Next.js)** lendo as views `duplicidade`/`resumo_discos` do Supabase — é o que fecha o ciclo "profissional interno opera local, gestor acompanha remoto".
6. Depois do piloto de varredura validado em campo: iniciar o **módulo de catálogo** (schema de referência com crosswalk Dublin Core/PBCore/PREMIS/FIAF, ainda não trazido para este pacote — está apenas descrito na documentação de arquitetura, não implementado em código).

## 7. Contexto de negócio relevante para decisões técnicas

- Orçamento do piloto: R$25.000 (reduzido de R$28.500) — **implica que o software NÃO é cedido ao cliente**, e por isso a exportação de dados é o único mecanismo anti-aprisionamento disponível; qualquer decisão técnica que dificulte exportar dados quebra um compromisso contratual.
- Cronograma do piloto: 11 semanas, início em 17/08/2026, encerramento após o 2º turno das eleições (25/10/2026).
- Viagem a campo (Fortaleza) na semana seguinte a este handoff — instalação e início da varredura acontecem lá; o restante do diagnóstico e a consolidação rodam nas semanas seguintes, monitorados remotamente.
- Uso previsto de máquinas: incerto ainda se serão computadores da escola ou locados — se locados, exigir administrador local e USB 3.0 confirmado (gargalo de performance).

## 8. Arquivos deste pacote

```
preserva-scan/
├── HANDOFF.md              ← este arquivo
├── README.md                ← instruções de instalação/uso (arquitetura, comandos)
├── Dockerfile                ← empacotamento (não testado, ver §5)
├── docker-compose.yml
├── .env.example
├── iniciar_painel.bat        ← launcher Windows (não testado, ver §5)
├── scanner/
│   ├── scan.py                ← núcleo da varredura
│   ├── db.py                  ← persistência/idempotência
│   ├── panel.py                ← painel web local
│   ├── requirements.txt
│   └── templates/index.html
├── sql/
│   └── schema.sql             ← schema da fase de inventário (não é o modelo de catálogo)
└── docs/
    └── arquitetura_sgdp_referencia.md   ← documento de arquitetura completo (OAIS, RF1-RF3,
                                            DDL de referência do módulo de catálogo ainda não
                                            implementado, diagramas textuais). Ler antes de
                                            iniciar o item 6 dos próximos passos (§6).
```

