# PRESERVA-SCAN — módulo de varredura (esqueleto)

Varre discos e nuvem do acervo, **somente leitura**, e produz o inventário
(caminho, SHA-256, formato, metadados). É a fase E1 do projeto Porto Iracema
e a base do produto que poderá ser oferecido a outras instituições.

## Arquitetura (quem roda o quê)

```
  MÁQUINA DA ESCOLA (local)                       NUVEM
  ┌──────────────────────────────┐
  │  PRESERVA-SCAN (Docker)       │   só metadados   ┌──────────────┐      ┌──────────────┐
  │  rclone · Siegfried ·         │ ───────────────▶ │  Supabase    │ ◀─── │  Dashboard    │
  │  MediaInfo · ExifTool · hash  │   (inventário)   │ (PostgreSQL) │      │  (Vercel)     │
  │  + painel de operação :8080   │                  └──────────────┘      └──────────────┘
  └──────────────────────────────┘
     ▲ lê os discos (99 TB                              ▲ os 99 TB          ▲ o GESTOR acompanha
       NUNCA saem daqui)                                  nunca sobem         de qualquer lugar
     operado pelo PROFISSIONAL INTERNO
```

- **Profissional interno (na escola):** liga os discos e opera o scanner pelo painel `http://localhost:8080`.
- **Gestor/consultor (remoto):** acompanha resultados pelo **dashboard no Vercel**, que lê o Supabase. Não precisa de VPN nem de acesso à máquina.
- **Importante:** o scanner **não roda no Vercel** (serverless não acessa discos locais). Só o dashboard roda lá.

## Modo mais simples: o executável (PreservaScan.exe)

Para quem vai **operar em campo sem mexer em terminal**. O `PreservaScan.exe`
é um programa único, **não precisa de Python nem de Docker instalados**.

**Para o operador (uso diário):**
1. Dê **duplo-clique** em `PreservaScan.exe`. Abre uma janela preta (pode
   minimizar) e, sozinho, o navegador no painel. O endereço do painel também
   fica escrito na janela preta **e** salvo no arquivo `ENDERECO_DO_PAINEL.txt`
   ao lado do `.exe` — use-o se o navegador abrir a página errada (o programa
   escolhe uma porta livre automaticamente, para não colidir com outros apps).
2. No painel: **selecione o disco**, dê uma **etiqueta**, clique em iniciar.
3. Acompanhe pela **barra de progresso** (tempo restante, velocidade). Se algo
   travar ou falhar, o painel avisa em destaque.
4. Para **encerrar**, feche a janela preta.

Os relatórios (manifestos CSV/JSON e o log de cada varredura) aparecem numa
pasta **`manifestos/`** criada **ao lado do `.exe`**.

**De onde vem o `.exe`:** ele é gerado automaticamente numa máquina Windows pelo
GitHub Actions (não é versionado no repositório). Veja
[`packaging/README.md`](packaging/README.md) para baixá-lo ou gerar uma nova versão.

## Instalação na máquina da escola (na viagem)

Pré-requisitos: Docker + Docker Compose. (No Windows, Docker Desktop com WSL2.)

```bash
git clone <este-repo> preserva-scan && cd preserva-scan
cp .env.example .env          # preencha DATABASE_URL com a string do Supabase
# edite docker-compose.yml: aponte o volume :ro para o disco ligado
docker compose up --build     # sobe o painel em http://localhost:8080
```

Sem Supabase (modo offline, dados só locais):
```bash
docker compose --profile local up --build   # sobe um Postgres local também
```

## Como usar

1. Ligue um HD e monte-o (ex.: `/mnt/hd_antigos_c`); ajuste o volume `:ro` no compose.
2. Abra `http://localhost:8080`, informe **etiqueta** e **ponto de montagem**, clique em iniciar.
3. Acompanhe pelo log; ao fim, o disco aparece no resumo e no dashboard.
4. Repita para cada HD (prioridade: grupo **antigos** primeiro).

Ou por linha de comando, direto no container:
```bash
docker compose exec scanner python scan.py --disco "Antigos-C" --raiz /mnt/hd --grupo antigos
```

## Garantias de segurança

- **Somente leitura:** os discos são montados com `:ro`; o scanner só lê. Nunca move, copia ou apaga o acervo.
- **Idempotência / retomada:** re-rodar não duplica registros (chave `disco_label + caminho`); arquivo já lido, com mesmo tamanho e data, é pulado. Um mesmo arquivo com hash diferente do anterior = alerta de alteração/corrupção. Funciona **mesmo offline** (lê o próprio manifesto local): se uma varredura de horas cair, basta **rodar de novo o mesmo disco** — ele continua de onde parou, sem refazer o que já foi feito. Para forçar uma varredura do zero, use `--force`.
- **Um manifesto por disco (nome estável):** `manifesto_<etiqueta>.csv`/`.jsonl` — é atualizado a cada passada (não cria um arquivo novo por dia), o que é o que permite a retomada.
- **Escrita durável:** o manifesto é gravado **linha a linha em disco**; uma interrupção (queda de energia, USB solto, janela fechada) perde no máximo 1 registro, e CSV e JSONL nunca ficam dessincronizados.
- **Redundância do inventário:** tudo é gravado em manifesto local (CSV + JSON) **antes** de ir ao banco. O catálogo é, ele próprio, objeto de preservação.

## Metadados de mídia (MediaInfo / ExifTool)

Para um acervo audiovisual, os metadados técnicos (codec, resolução, duração,
taxa de amostragem, profundidade de bits…) são parte da preservação. O scanner
usa **MediaInfo** (áudio/vídeo) e **ExifTool** (imagens); o **Siegfried** já
identifica o formato/PRONOM.

Onde o programa procura essas ferramentas (nesta ordem): **embutidas no `.exe`**
(quando a build conseguiu incluí-las) → uma pasta **`ferramentas/` ao lado do
`.exe`** (basta colocar `MediaInfo.exe` e `exiftool.exe` lá — sem instalar nada,
sem PATH) → o **PATH** do sistema. O painel mostra em selos quais estão
disponíveis.

**Completar metadados que faltaram (sem re-hashear):** se um disco foi varrido
sem MediaInfo/ExifTool, não é preciso refazer a varredura (horas de hash). Use o
botão **"Completar metadados de mídia"** no painel, ou:

```bash
python scanner/scan.py --disco "TRANSPORTE A" --raiz F:\ --backfill
```

Ele percorre o manifesto, roda só as ferramentas de mídia nos arquivos que ainda
não têm metadados e atualiza o manifesto e o banco — **sem recalcular o SHA-256**.

## Relatório local (sem banco, sem custo)

A partir dos manifestos já varridos, o sistema gera um **relatório do acervo** —
sem depender de banco ou nuvem. No painel, botão **"Gerar / atualizar relatório"**;
ou pela linha de comando:

```bash
python scanner/relatorio.py --manifestos ./manifestos --saida ./relatorio
```

Saída (na pasta `relatorio/`), tudo em formato aberto que a instituição guarda:

- `dashboard.html` — painel visual **estático** (abre no navegador, offline): volume
  por disco, formatos mais frequentes, duplicatas e resumo.
- `inventario_consolidado.csv` — todos os discos num arquivo só.
- `duplicatas.csv` — arquivos idênticos (mesmo SHA-256) e **em quantos discos**
  aparecem (responde "está em 1 ou em 2+ discos", como a view `duplicidade` do banco).
- `resumo.json` — números do acervo, para outras ferramentas.

É a alternativa soberana e de custo zero ao dashboard hospedado: o mesmo valor
analítico, como um arquivo que não expira nem depende de assinatura.

## O que este esqueleto entrega e o que falta

Entregue: varredura (rclone/Siegfried/MediaInfo/ExifTool/SHA-256), idempotência,
manifestos, envio ao Supabase, painel local de operação, empacotamento Docker,
esquema SQL com a **visão de duplicidade** (responde "está em 1 ou em 2+ discos").

Próximo passo (v1): **dashboard no Vercel** (Next.js lendo o Supabase) com volumetria,
mapa de duplicatas, progresso por disco e download dos relatórios — a interface
amigável para o gestor. A tabela `arquivos` e as visões `duplicidade`/`resumo_discos`
já são o contrato de dados desse dashboard.

## Aviso de licenças (para comercializar depois)

As ferramentas chamadas como processo externo (rclone/MIT, Siegfried/Apache-2.0,
MediaInfo/BSD, ExifTool/Artistic, FFmpeg como binário) permitem produto proprietário
por cima. **Não** embutir Archivematica/AtoM (AGPL-3.0). Validar a estratégia de
licenciamento e o arquivo de atribuição (NOTICE) com um advogado de PI antes de vender.
