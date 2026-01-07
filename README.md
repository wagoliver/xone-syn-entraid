# xone-syn-entraid

Sincroniza departamentos e colaboradores do Microsoft Entra ID (Azure AD) para a API da XoneCloud.

## O que faz
- Busca usuarios no Microsoft Graph (users + manager).
- Monta departamentos e colaboradores no formato da XoneCloud.
- Envia departamentos e colaboradores para a API (com dry-run opcional).

## Requisitos
- Python 3.9+
- App registration no Entra ID com permissao de aplicacao `User.Read.All`
- Token da XoneCloud

## Variaveis de ambiente
```
AZ_TENANT_ID=...
AZ_CLIENT_ID=...
AZ_CLIENT_SECRET=...
XONE_API_TOKEN=...
```

## Instalacao
```
python -m venv .venv
.\.venv\Scripts\activate
pip install requests
```

## Execucao (fluxo completo)
```
python xone-sync.py
```

## Flags principais (ajuste no arquivo)
- `SEND_DEPARTMENTS`, `DEPT_DRY_RUN`
- `SEND_COLLABORATORS`, `COLLAB_DRY_RUN`, `TEST_SINGLE_USER`
- `ONLY_ENABLED`, `EXCLUDE_SERVICE_ACCOUNTS`, `EXCLUDE_WITHOUT_DEPARTMENT`
- `COLLAB_BATCH_SIZE`

## Outros scripts
- `xone-sync-collaborators.py`: envia colaboradores com 1 chamada por usuario.
- `xone-sync-collaborators-full.py`: envia colaboradores em chamada unica (lista completa).
- `xone-sync-departamento-clean.py`: somente departamentos.
- `xone-sync.-azure.py`: versao para Azure Functions Timer (exige `azure-functions`).

## Docker / n8n
O `Docker/Dockerfile` adiciona Python + requests ao container do n8n. O `Docker/docker-compose.yml`
exponibiliza as variaveis de ambiente para o script.

Exemplo:
```
cd Docker
docker compose up --build
```

## Seguranca
- Nao commitar `.env`, `cred.txt`, `credentials` ou tokens.
- Revogue tokens se foram expostos.

## Licenca
MIT
