# Assinatura do Siegfried embutida no PreservaScan.exe

`default.sig` é o arquivo de assinaturas que o `sf.exe` embutido usa para
identificar o formato (PRONOM) de cada arquivo. O build (`build-exe.yml`) o copia
para `packaging/ferramentas/`, ao lado do `sf.exe`.

## Por que uma assinatura própria (e não a do `sf -update`)

A assinatura padrão baixada por `sf -update` não limita quanto de cada arquivo o
Siegfried examina: num vídeo, ele percorre o arquivo **inteiro**. Num HD de acervo
audiovisual, a fase "Identificando formatos" lia o disco todo **antes** do
checksum, o que levava horas sem progresso visível. Esta assinatura limita a
leitura ao **primeiro e ao último 1 MiB** de cada arquivo.

O DROID (The National Archives), referência de identificação em preservação
digital, examina por padrão só os primeiros 64 KiB. Usamos 1 MiB porque, com
64 KiB, a assinatura específica de RAW Nikon (fmt/202, NEF) não é alcançada e o
arquivo sai como TIFF genérico.

Validação feita na geração (Siegfried 1.11.9):

| Teste | Sem limite | Com 1 MiB |
|---|---|---|
| Skeleton-suite oficial do PRONOM (2.572 arquivos, 2.175 formatos) | referência | **2.572 idênticos** |
| Corpus variado (6.000 arquivos, 1.870 extensões) | 13,6 s | **6.000 idênticos**, 3,8 s |
| Vídeo QuickTime de 4 GiB | 21,3 s | **0,19 s**, mesmo formato |

## Procedência

- Ferramenta: `roy` do Siegfried **v1.11.9** (o mesmo autor e a mesma versão do `sf`)
- Dados: `DROID_SignatureFile_V125.xml` + `container-signature-20260119.xml`
  (pasta `cmd/roy/data` do Siegfried v1.11.9)
- Opções: `-bof 1048576 -eof 1048576`; o resto é padrão (PRONOM, com contêineres)
- SHA-256: `cdea648de351ad68551ec00b45a6151429ae33caccee88850d4436708b229782`

Para conferir, `sf -sig default.sig -version` deve mostrar
`max BOF 1048576; max EOF 1048576`.

## Como regenerar (por exemplo, para uma versão nova do PRONOM)

Requer Go:

```bash
V=v1.11.9   # versão do Siegfried (use a mesma do sf.exe embutido)
GOBIN=$PWD/bin go install github.com/richardlehane/siegfried/cmd/roy@$V
DADOS=$(go mod download -json github.com/richardlehane/siegfried@$V | python3 -c 'import json,sys;print(json.load(sys.stdin)["Dir"])')/cmd/roy/data
cp -r "$DADOS" ./data && chmod -R u+w ./data
./bin/roy build -home ./data -bof 1048576 -eof 1048576
cp ./data/default.sig packaging/siegfried/default.sig
```

Depois de regenerar, atualize a procedência e o SHA-256 acima.
