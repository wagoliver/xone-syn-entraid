import os
import re
import json
import time
import requests
from typing import List, Dict, Optional

# =========================================================
# CONFIGURAÇÕES (ajuste conforme necessário)
# =========================================================
# Variáveis de ambiente exigidas para o Graph:
#   AZ_TENANT_ID, AZ_CLIENT_ID, AZ_CLIENT_SECRET
TENANT_ID = os.getenv("AZ_TENANT_ID")
CLIENT_ID = os.getenv("AZ_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZ_CLIENT_SECRET")

# Token da API xOne Cloud via env: XONE_API_TOKEN
XONE_API_TOKEN = os.getenv("XONE_API_TOKEN")
XONE_API_URL = "https://register-api.xonecloud.com/collaborators/api/v1"

# Flags principais
ONLY_ENABLED = False                 # True = apenas usuários habilitados
EXCLUDE_SERVICE_ACCOUNTS = True      # Excluir contas de serviço/admin
EXCLUDE_WITHOUT_DEPARTMENT = True    # Excluir usuários sem department

SEND_TO_API = True                   # True = enviar para XoneCloud API
DRY_RUN = False                      # True = simular envio; False = enviar de verdade
TEST_SINGLE_USER = False             # True = envia só 1 usuário para teste

# =========================================================
# CONSTANTES DO GRAPH
# =========================================================
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_USERS_URL = "https://graph.microsoft.com/v1.0/users"
SELECT_FIELDS = "userPrincipalName,displayName,accountEnabled,department,employeeId"
PAGE_SIZE = 999  # máx do v1.0

# =========================================================
# OBTENÇÃO DO TOKEN (Client Credentials)
# Requer permissões de app: User.Read.All (Application) consentidas.
# =========================================================
def get_access_token(retries: int = 3, backoff: float = 1.5) -> str:
    if not (TENANT_ID and CLIENT_ID and CLIENT_SECRET):
        raise RuntimeError("AZ_TENANT_ID / AZ_CLIENT_ID / AZ_CLIENT_SECRET não configurados.")

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": GRAPH_SCOPE,
        "grant_type": "client_credentials",
    }
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            resp = requests.post(TOKEN_URL, data=data, timeout=30)
            resp.raise_for_status()
            return resp.json()["access_token"]
        except Exception as e:
            last_err = e
            time.sleep(backoff ** (i + 1))
    raise RuntimeError(f"Falha ao obter token: {last_err}")

# =========================================================
# COLETA DE USUÁRIOS DO ENTRA ID
# =========================================================
def fetch_all_users(only_enabled: bool = False) -> List[Dict]:
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    base_url = f"{GRAPH_USERS_URL}?$select={SELECT_FIELDS}&$top={PAGE_SIZE}"
    url = base_url
    users: List[Dict] = []

    while url:
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code in [401, 403]:
            raise RuntimeError(f"Permissões insuficientes (HTTP {r.status_code}): {r.text}")
        r.raise_for_status()

        data = r.json()
        batch = data.get("value", [])

        if only_enabled:
            batch = [u for u in batch if u.get("accountEnabled") is True]

        users.extend(batch)
        url = data.get("@odata.nextLink")

    return users

# =========================================================
# NORMALIZAÇÃO DE USERNAME
# =========================================================
ALLOWED_USERNAME_RE = re.compile(r'[^A-Za-z0-9._-]')
MAX_USERNAME_LEN = 32

def normalize_username(raw: str) -> str:
    if not raw:
        return ""
    u = ALLOWED_USERNAME_RE.sub("", raw)
    return u[:MAX_USERNAME_LEN]

def build_username(u: Dict) -> str:
    """
    Prioriza employeeId como username; fallback = parte local do e-mail.
    """
    empid = (u.get("employeeId") or "").strip()
    if empid:
        candidate = normalize_username(empid)
        if candidate:
            return candidate

    email_full = u.get("userPrincipalName") or ""
    local = email_full.split("@")[0] if "@" in email_full else email_full
    return normalize_username(local)

# =========================================================
# DETECÇÃO DE CONTAS DE SERVIÇO
# =========================================================
def is_service_account(username: str, displayname: str) -> bool:
    service_patterns = [
        "-admin", "-service", "-cluster", "-sync", "-bot", "-svc",
        "system-", "service-", "noreply", "no-reply", "automated"
    ]
    username_lower = (username or "").lower()
    displayname_lower = (displayname or "").lower()
    return any(p in username_lower or p in displayname_lower for p in service_patterns)

# =========================================================
# TRANSFORMAÇÃO PARA FORMATO XONE CLOUD
# =========================================================
def transform(users_msgraph: List[Dict],
              exclude_service_accounts: bool = True,
              exclude_without_department: bool = False) -> List[Dict]:
    out: List[Dict] = []
    seen_usernames = set()

    for u in users_msgraph:
        email_full = u.get("userPrincipalName") or ""
        displayname = u.get("displayName") or ""
        department = u.get("department") or ""

        # Username priorizando employeeId
        username = build_username(u)

        # Evita duplicidade e vazios
        base = username or "user"
        suffix = 1
        while username in seen_usernames or not username:
            username = f"{base[:MAX_USERNAME_LEN-3]}-{suffix}"[:MAX_USERNAME_LEN]
            suffix += 1
        seen_usernames.add(username)

        # Conta de serviço?
        is_service = is_service_account(email_full, displayname)

        # Filtros opcionais
        if exclude_service_accounts and is_service:
            continue
        if exclude_without_department and not department:
            continue

        if not department and not is_service:
            department = "Sem Departamento"
        elif not department and is_service:
            department = "Conta de Serviço"

        out.append({
            "username": username,
            "displayname": displayname,
            "status": bool(u.get("accountEnabled", False)),
            "department": department,
            "workingday": "Jornada padrão" if not is_service else "N/A",
            "email": email_full
        })

    return out

# =========================================================
# ENVIO PARA API XONE CLOUD
# =========================================================
def send_to_xone_api(users_data: List[Dict], dry_run: bool = True) -> Dict:
    """
    Envia os dados dos usuários para a API do XoneCloud - uma chamada por usuário.
    """
    if not XONE_API_TOKEN:
        raise RuntimeError("XONE_API_TOKEN não configurado nas variáveis de ambiente.")

    headers = {
        "accept": "application/json",
        "Authorization": XONE_API_TOKEN,  # Token direto, sem 'Bearer'
        "Content-Type": "application/json",
        "User-Agent": "XoneSync/1.0 (Python/requests)"
    }

    if dry_run:
        print("\nDRY RUN - Simulação de envio para XoneCloud API")
        print(f"URL: {XONE_API_URL}")
        print(f"Total de usuários: {len(users_data)} (1 chamada por usuário)")
        print(f"Token: {XONE_API_TOKEN[:30]}...")
        return {"status": "dry_run", "users_count": len(users_data)}

    successful_users = []
    failed_users = []

    print(f"\nEnviando {len(users_data)} usuários para XoneCloud API...")
    print(f"URL: {XONE_API_URL}")
    print("Fazendo 1 chamada por usuário...\n")

    for i, user in enumerate(users_data, 1):
        try:
            api_payload = {
                "username": user['username'],
                "displayname": user['displayname'],
                "status": user['status'],
                "department": user['department'],
                "workingday": user['workingday']
            }

            response = requests.post(
                XONE_API_URL,
                headers=headers,
                json=[api_payload],  # lista com 1 usuário
                timeout=30
            )

            if response.status_code in [200, 201]:
                print(f"[{i}/{len(users_data)}] SUCESSO: {user['username']}")
                successful_users.append(user['username'])
            else:
                msg = f"HTTP {response.status_code}: {response.text[:200]}"
                print(f"[{i}/{len(users_data)}] ERRO: {user['username']} - {msg}")
                failed_users.append({"user": user['username'], "error": msg})

        except requests.exceptions.RequestException as e:
            msg = f"Conexão: {str(e)[:100]}"
            print(f"[{i}/{len(users_data)}] ERRO CONEXÃO: {user['username']} - {msg}")
            failed_users.append({"user": user['username'], "error": msg})

        if i < len(users_data):
            time.sleep(0.5)

    print("\nResumo final:")
    print(f"   Sucessos: {len(successful_users)}")
    print(f"   Falhas: {len(failed_users)}")

    if len(successful_users) == len(users_data):
        return {"status": "success", "successful": len(successful_users)}
    elif len(successful_users) > 0:
        return {"status": "partial_success", "successful": len(successful_users), "failed": len(failed_users)}
    else:
        return {"status": "error", "failed": len(failed_users)}

# =========================================================
# MAIN
# =========================================================
if __name__ == "__main__":
    print("Consultando usuários do Entra ID...")
    raw = fetch_all_users(only_enabled=ONLY_ENABLED)

    result = transform(
        raw,
        exclude_service_accounts=EXCLUDE_SERVICE_ACCOUNTS,
        exclude_without_department=EXCLUDE_WITHOUT_DEPARTMENT
    )

    # Estatísticas básicas
    total_users = len(result)
    users_without_dept = sum(1 for u in result if u["department"] == "Sem Departamento")

    print("\nEstatísticas:")
    print(f"   Total de usuários: {total_users}")
    print(f"   Sem departamento: {users_without_dept}")
    print("\n" + "="*60 + "\n")

    # Visualização JSON no console
    json_output = json.dumps(result, ensure_ascii=False, indent=2)
    print(json_output)
    print(f"\nJSON disponível na variável 'json_output' ({len(json_output)} caracteres)")
    print(f"Array 'result' contém {len(result)} usuários")

    # Envio para API do xOne Cloud
    if SEND_TO_API:
        print("\nIntegrando com XoneCloud API...")
        print("=" * 60)
        data_to_send = result[:1] if TEST_SINGLE_USER else result
        if TEST_SINGLE_USER:
            print("TESTE: enviando apenas 1 usuário para validar a API.")
        api_result = send_to_xone_api(data_to_send, dry_run=DRY_RUN)

        if api_result["status"] == "success":
            print("Sincronização com XoneCloud concluída com sucesso!")
        elif api_result["status"] == "partial_success":
            print(f"Sincronização parcial: {api_result['successful']} sucessos, {api_result.get('failed', 0)} falhas")
        elif api_result["status"] == "dry_run":
            print("Simulação concluída - altere DRY_RUN=False para enviar de verdade")
        else:
            print("Erro na sincronização:", api_result)
    else:
        print("\nEnvio para API desativado (SEND_TO_API=False)")
