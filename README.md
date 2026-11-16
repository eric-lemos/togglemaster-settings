# ToggleMaster Settings

Script Python para criar ou atualizar **GitHub Actions repository secrets e variables**
dos repositórios ToggleMaster. Não configura GitHub Environments nem altera workflows.

## Pré-requisitos

- Python 3.10 ou superior, com `pip`.
- Acesso à internet para executar `--apply`.
- Personal access token (PAT) do GitHub com acesso aos repositórios configurados.
  Para um token fine-grained, conceda as permissões de repositório
  **Secrets: Read and write** e **Variables: Read and write**.
  Para PAT classic em repositórios privados, use o escopo `repo`.
  Autorize SSO quando exigido pela organização.

O token `GITHUB_TOKEN` autentica este script. Ele não é enviado como secret.
O `GITOPS_TOKEN` é outro valor: será enviado ao repositório de apps para os
workflows atualizarem o GitOps; ele precisa de acesso de escrita a **Contents**
no repositório GitOps.

## Instalação no Windows

Execute no PowerShell, na pasta deste projeto:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edite o arquivo `.env` local e preencha os valores. Ele está no `.gitignore`;
nunca envie tokens, senhas ou credenciais reais ao Git.
Não repita o comando de cópia depois de preencher o arquivo: ele sobrescreve os valores.

As credenciais AWS são compartilhadas pelos três repositórios. Opcionalmente,
crie um arquivo `.aws` na pasta do projeto no formato de credenciais do AWS CLI,
com as três chaves `aws_access_key_id`, `aws_secret_access_key` e
`aws_session_token` na seção `[default]`. Em toda execução, inclusive `--dry-run`,
o script atualiza essas três chaves no `.env` (ou no arquivo de `--env-file`)
antes de carregar as configurações. O arquivo `.env` deve existir; os demais
valores são preservados. Credenciais incompletas ou arquivos inválidos interrompem
a execução sem atualizar o `.env` nem fazer chamadas ao GitHub.

Quando `.aws` existe, suas credenciais têm precedência sobre as variáveis AWS
do processo. Sem `.aws`, o comportamento anterior é mantido: atualize o `.env`
manualmente e as variáveis do processo têm precedência. O arquivo `.aws` está no
`.gitignore`; nunca o envie ao Git. O AWS CLI usa normalmente `~/.aws/credentials`:
esse caminho não é lido automaticamente pelo script.

## Configuração

O arquivo `settings.json` mantém o formato por repositório, com as chaves completas
`owner/repository`. O proprietário inicial é `eric-lemos`, conforme os remotes locais.
Altere as chaves e `GITOPS_REPOSITORY` se usar outro usuário ou organização.

As variables aceitam texto literal ou referência ao ambiente.
Os secrets aceitam **somente referências**, evitando senhas no JSON:

```json
{
    "owner/repository": {
        "vars": {
            "GITOPS_BRANCH": "main",
            "AWS_REGION": {"env": "AWS_REGION"}
        },
        "secrets": {
            "AWS_ACCESS_KEY_ID": {"env": "AWS_ACCESS_KEY_ID"}
        }
    }
}
```

O nome dentro de `env` identifica uma variável do `.env` ou do ambiente do processo.
Variáveis já definidas no processo têm precedência sobre o `.env`, exceto pelas
três credenciais AWS quando o arquivo `.aws` está presente.
Não há interpolação de `${...}`: caracteres `$` de senhas são preservados.
Use aspas simples no `.env` para valores contendo `#`, espaços ou JSON:

```dotenv
TF_VAR_RDS_INSTANCE_PASSWORDS='{"auth":"senha-auth","targeting":"senha-targeting","flags":"senha-flags"}'
```

Para o secret `TF_VAR_RDS_INSTANCE_PASSWORDS`, use exatamente as chaves
`auth`, `targeting` e `flags`. Esses nomes vêm das instâncias `rds.instances`;
`evaluation` não é uma chave válida. Se o secret atual estiver usando `evaluation`,
substitua o valor completo no GitHub (ou no `.env` local antes de sincronizar).

O template inclui 16 variables e 19 secrets. O workflow Terraform calcula
dinamicamente o bucket de backend a partir da conta AWS:

| Repositório | Variables | Secrets |
| --- | ---: | ---: |
| `togglemaster-apps` | 8 | 4 |
| `togglemaster-gitops` | 7 | 11 |
| `togglemaster-terraform` | 1 | 4 |

No `togglemaster-gitops`, `SELF_HEALING_ENABLED` controla a autorização de reinícios
e a rota do Alertmanager. Mantenha `false` na primeira instalação Helm e até
autorizar os reinícios; depois de o Kubernetes Apply criar o Secret
`observability/self-healing`, defina `true` e execute Helm Apply para ativar a
rota. Ao voltar para `false`, execute Helm Apply para desativar a rota.
Os secrets `DISCORD_WEBHOOK_URL` e
`PAGERDUTY_INTEGRATION_KEY` são sincronizados para o repositório GitOps.
`SELF_HEALING_APP_PRIVATE_KEY` é multilinha: use aspas duplas no `.env`, incluindo as linhas BEGIN e END.
Nomes iniciados por `GITHUB_` são reservados pelo GitHub e não podem ser secrets;
por isso `GITHUB_TOKEN` fica apenas no `.env`.

O nome do bucket de backend é calculado automaticamente pelo workflow Terraform
com o ID da conta AWS e a região.

Valores ausentes ou vazios são ignorados individualmente: o script não envia uma
atualização para aquela chave e segue com as demais. Para variáveis literais,
strings vazias ou só com espaços também são ignoradas. Configurações malformadas
continuam causando erro antes de qualquer chamada ao GitHub.

Se `GITHUB_TOKEN` estiver vazio e não houver valores preenchidos para atualizar,
`--apply` termina sem fazer chamadas ao GitHub. Se ao menos uma chave tiver valor,
o token é obrigatório. Chaves vazias existentes no GitHub **não são apagadas nem
sobrescritas**; são deixadas como estão. Assim, `AWS_SESSION_TOKEN` pode ficar
vazio se não usar credenciais temporárias.

## Executar

Simule primeiro. Este é o comportamento padrão e não exige token nem faz chamadas
de rede; imprime somente repositórios, tipos e nomes, nunca valores. Se `.aws`
existir, o dry-run também atualiza as credenciais no `.env` local:

```powershell
.\.venv\Scripts\python.exe updater.py --dry-run
```

Envie as configurações explicitamente:

```powershell
.\.venv\Scripts\python.exe updater.py --apply
```

Para atuar somente em um repositório:

```powershell
.\.venv\Scripts\python.exe updater.py --repo eric-lemos/togglemaster-apps --apply
```

Repita `--repo owner/repository` para selecionar vários. Apenas as referências dos
repositórios selecionados são exigidas. Os caminhos padrão são relativos à pasta
do script, independentemente do diretório atual. Para outros arquivos:

```powershell
.\.venv\Scripts\python.exe updater.py --settings settings.json --env-file .env --dry-run
```

Os secrets são criptografados com a chave pública de cada repositório via
LibSodium/PyNaCl antes de serem enviados à API oficial do GitHub por HTTPS.
As variables são criadas quando ausentes e atualizadas quando existentes.
Os secrets são criados ou sobrescritos; o GitHub não permite ler seu valor atual.

O script não apaga nenhuma configuração remota. Alterações não são transacionais:
em caso de falha, para na primeira operação com erro, retorna código 1 e mantém
as gravações anteriores. Corrija a causa e execute novamente; as operações podem
ser repetidas. O código 0 indica conclusão da simulação ou das gravações solicitadas.
Nenhum workflow é disparado explicitamente.
