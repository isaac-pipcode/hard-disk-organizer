# Empacotamento — PreservaScan.exe (executável Windows)

O painel de varredura pode ser distribuído como um **executável único** para
Windows, para que operadores sem conhecimento de terminal apenas dêem
**duplo-clique**. O `.exe` embute o Python, o painel (FastAPI/uvicorn) e o
scanner — **nada precisa estar instalado na máquina de destino**.

O binário **não é versionado** no Git (é um artefato de build). Ele é gerado
numa máquina Windows real pelo GitHub Actions.

## Baixar o `.exe` já pronto

1. No GitHub, abra a aba **Actions** → workflow **"Gerar PreservaScan.exe (Windows)"**.
2. Clique na execução mais recente que terminou com ✔.
3. Em **Artifacts**, baixe **`PreservaScan-windows`** (um `.zip` com o `PreservaScan.exe`).

> Artefatos de build expiram após alguns dias. Para um link permanente, gere uma
> **Release** (veja abaixo) — o `.exe` fica anexado a ela.

## Gerar uma nova versão

**Opção A — build sob demanda (recomendado para testar):**
Actions → *Gerar PreservaScan.exe (Windows)* → **Run workflow**. Ao terminar,
baixe o artefato.

**Opção B — publicar uma Release com o `.exe` anexado:**
```bash
git tag v0.1.0
git push origin v0.1.0
```
A tag `v*` dispara o build e cria uma **Release** com o `PreservaScan.exe` anexado.

## Gerar localmente (numa máquina Windows com Python)

Da **raiz do repositório**:
```powershell
pip install -r scanner/requirements.txt
pip install pyinstaller==6.11.1
pyinstaller packaging/preservascan.spec --noconfirm
```
Saída: `dist/PreservaScan.exe`.

## Como está montado

- `scanner/preservascan.py` — ponto de entrada do `.exe`: acha uma porta livre,
  sobe o painel e abre o navegador; fechar a janela encerra tudo.
- `packaging/preservascan.spec` — receita do PyInstaller (onefile). Inclui a pasta
  `scanner/templates` como dado embutido e lista os módulos que o uvicorn/FastAPI
  carregam dinamicamente (sem eles o `.exe` compila mas não sobe).
- `.github/workflows/build-exe.yml` — compila em `windows-latest` e publica o `.exe`.

O scanner roda **no mesmo processo** do painel (não há `python scan.py` externo),
o que é o que torna o empacotamento confiável.

## Observações

- **Antivírus / SmartScreen:** executáveis PyInstaller sem assinatura digital podem
  disparar aviso do Windows SmartScreen ("Mais informações → Executar assim mesmo")
  ou falso-positivo de antivírus. Para distribuição ampla, considere **assinar o
  código** (certificado Authenticode). O build usa `upx=False` justamente para
  reduzir falso-positivo.
- **Somente leitura:** o `.exe` não muda nada disso — a varredura continua só lendo
  os discos. Os manifestos são gravados na pasta `manifestos/` ao lado do `.exe`.
- **Supabase (opcional):** para enviar ao dashboard, defina a variável de ambiente
  `DATABASE_URL` antes de abrir o `.exe` (modo offline funciona sem ela).
