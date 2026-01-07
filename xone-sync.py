import os
import re
import json
import time
import requests
import math
from typing import List, Dict, Optional

# =========================================================
# CONFIGURAÇÕES GERAIS
# =========================================================
# Azure AD (Entra ID)
TENANT_ID = os.getenv("AZ_TENANT_ID")
CLIENT_ID = os.getenv("AZ_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZ_CLIENT_SECRET")

# XoneCloud (um único token para departamentos e colaboradores)
XONE_API_TOKEN = os.getenv("XONE_API_TOKEN")
COLLAB_API_URL = "https://register-api.xonecloud.com/collaborators/api/v1"
DEPT_API_URL = "https://register-api.xonecloud.com/departments/api/v1/"

# Flags de comportamento
ONLY_ENABLED = False                 # Colaboradores: filtra apenas contas habilitadas
EXCLUDE_SERVICE_ACCOUNTS = True      # Colaboradores: remove contas de serviço
EXCLUDE_WITHOUT_DEPARTMENT = True    # Colaboradores: remove quem não tem departamento

SEND_DEPARTMENTS = True              # Envia departamentos para XoneCloud
DEPT_DRY_RUN = False                 # True = simula envio de departamentos

SEND_COLLABORATORS = True            # Envia colaboradores para XoneCloud
COLLAB_DRY_RUN = False               # True = simula envio de colaboradores
TEST_SINGLE_USER = False             # True = envia apenas 1 colaborador para teste
COLLAB_BATCH_SIZE = 5000             # tamanho maximo por chamada para colaboradores

# =========================================================
# CONSTANTES DO GRAPH
# =========================================================
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_USERS_URL = "https://graph.microsoft.com/v1.0/users"
SELECT_COLLAB_FIELDS = "userPrincipalName,displayName,accountEnabled,department,employeeId"
PAGE_SIZE = 999  # limite do v1.0


# =========================================================
# UTILITÁRIOS DE AUTENTICAÇÃO
# =========================================================
def ensure_azure_credentials() -> None:
    missing = []
    if not TENANT_ID:
        missing.append("AZ_TENANT_ID")
    if not CLIENT_ID:
        missing.append("AZ_CLIENT_ID")
    if not CLIENT_SECRET:
        missing.append("AZ_CLIENT_SECRET")
    if missing:
        raise RuntimeError(f"Credenciais Azure AD faltando: {', '.join(missing)}")


def get_access_token(retries: int = 3, backoff: float = 1.5) -> str:
    ensure_azure_credentials()
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
# FLUXO 1: DEPARTAMENTOS
# =========================================================
def fetch_users_with_manager(token: str) -> List[Dict]:
    """
    Busca usuários com manager expandido (para gerar departamentos).
    """
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"{GRAPH_USERS_URL}"
        f"?$select=userPrincipalName,displayName,department"
        f"&$expand=manager($select=displayName,userPrincipalName)"
        f"&$top={PAGE_SIZE}"
    )

    all_users: List[Dict] = []
    while url:
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        all_users.extend(data.get("value", []))
        url = data.get("@odata.nextLink")

    print(f"[Departamentos] {len(all_users)} usuários coletados do Graph")
    return all_users


def transform_to_departments(users: List[Dict]) -> List[Dict]:
    """
    Constrói a lista de departamentos com gerente e um usuário base.
    """
    departments: Dict[str, Dict] = {}

    for user in users:
        email = user.get("userPrincipalName", "")
        display_name = user.get("displayName", "")
        department = user.get("department", "")
        manager_info = user.get("manager", {})

        if not department:
            continue

        manager_name = manager_info.get("displayName", "Manager Name") if manager_info else "Manager Name"
        manager_email = manager_info.get("userPrincipalName", "manager.email@arctica.com.br") if manager_info else "manager.email@arctica.com.br"
        username_base = email.split("@")[0] if "@" in email else email

        if department not in departments:
            departments[department] = {
                "name": department,
                "manager": manager_name,
                "manager_email": manager_email,
                "workingday_name": None,
                "user_name": username_base,
            }
        else:
            # Atualiza gerente se ainda estiver vazio
            if departments[department]["manager"] == "Manager Name" and manager_name != "Manager Name":
                departments[department]["manager"] = manager_name
                departments[department]["manager_email"] = manager_email

    # Log de departamentos encontrados
    print("[Departamentos] Lista identificada:")
    for dept_name, info in departments.items():
        print(f"  - {dept_name} | manager: {info['manager']} | user base: {info['user_name']}")

    return list(departments.values())


def send_departments_to_api(departments: List[Dict], dry_run: bool = True) -> Dict:
    """
    Envia departamentos para a API do XoneCloud.
    """
    if not XONE_API_TOKEN:
        raise RuntimeError("XONE_API_TOKEN não configurado.")

    headers = {
        "accept": "application/json",
        "authorization": XONE_API_TOKEN,  # conforme usado nos scripts anteriores
        "Content-Type": "application/json",
        "User-Agent": "XoneSync-Departments/1.0 (Python/requests)",
    }

    payload = {"lang": "pt-BR", "departments": departments}

    if dry_run:
        print("[Departamentos] DRY RUN - nenhuma chamada real.")
        print(f"URL: {DEPT_API_URL}")
        print(f"Total: {len(departments)}")
        return {"status": "dry_run", "departments_count": len(departments)}

    print(f"[Departamentos] Enviando {len(departments)} departamentos...")
    try:
        resp = requests.post(DEPT_API_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code in [200, 201]:
            print(f"[Departamentos] Sucesso: {resp.status_code}")
            return {"status": "success", "successful": len(departments), "failed": 0}
        print(f"[Departamentos] Erro HTTP {resp.status_code}: {resp.text[:300]}")
        return {"status": "error", "error": f"HTTP {resp.status_code}", "successful": 0, "failed": len(departments)}
    except requests.exceptions.RequestException as e:
        msg = f"Conexão: {str(e)[:200]}"
        print(f"[Departamentos] Erro de conexão: {msg}")
        return {"status": "error", "error": msg, "successful": 0, "failed": len(departments)}


# =========================================================
# FLUXO 2: COLABORADORES
# =========================================================
ALLOWED_USERNAME_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_USERNAME_LEN = 32


def normalize_username(raw: str) -> str:
    if not raw:
        return ""
    u = ALLOWED_USERNAME_RE.sub("", raw)
    return u[:MAX_USERNAME_LEN]


def build_username(user: Dict) -> str:
    empid = (user.get("employeeId") or "").strip()
    if empid:
        candidate = normalize_username(empid)
        if candidate:
            return candidate
    email_full = user.get("userPrincipalName") or ""
    local = email_full.split("@")[0] if "@" in email_full else email_full
    return normalize_username(local)


def is_service_account(username: str, displayname: str) -> bool:
    service_patterns = [
        "-admin",
        "-service",
        "-cluster",
        "-sync",
        "-bot",
        "-svc",
        "system-",
        "service-",
        "noreply",
        "no-reply",
        "automated",
    ]
    username_lower = (username or "").lower()
    displayname_lower = (displayname or "").lower()
    return any(p in username_lower or p in displayname_lower for p in service_patterns)


def fetch_all_users(token: str, only_enabled: bool = False) -> List[Dict]:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_USERS_URL}?$select={SELECT_COLLAB_FIELDS}&$top={PAGE_SIZE}"
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

    print(f"[Colaboradores] {len(users)} usuários coletados do Graph")
    return users


def transform_collaborators(users_msgraph: List[Dict],
                             exclude_service_accounts: bool = True,
                             exclude_without_department: bool = False) -> List[Dict]:
    out: List[Dict] = []
    seen_usernames = set()

    for u in users_msgraph:
        email_full = u.get("userPrincipalName") or ""
        displayname = u.get("displayName") or ""
        department = u.get("department") or ""

        username = build_username(u)

        base = username or "user"
        suffix = 1
        while username in seen_usernames or not username:
            username = f"{base[:MAX_USERNAME_LEN-3]}-{suffix}"[:MAX_USERNAME_LEN]
            suffix += 1
        seen_usernames.add(username)

        service = is_service_account(email_full, displayname)

        if exclude_service_accounts and service:
            continue
        if exclude_without_department and not department:
            continue

        if not department and not service:
            department = "Sem Departamento"
        elif not department and service:
            department = "Conta de Serviço"

        out.append(
            {
                "username": username,
                "displayname": displayname,
                "status": bool(u.get("accountEnabled", False)),
                "department": department,
                "workingday": "Jornada padrão" if not service else "N/A",
                "email": email_full,
            }
        )

    return out


def send_collaborators_to_api(users_data: List[Dict], dry_run: bool = True) -> Dict:
    """
    Envia colaboradores para a API, em chamada unica ou em blocos.
    """
    if not XONE_API_TOKEN:
        raise RuntimeError("XONE_API_TOKEN nao configurado.")
    if not users_data:
        return {"status": "noop", "users_count": 0}

    headers = {
        "accept": "application/json",
        "Authorization": XONE_API_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": "XoneSync/1.0 (Python/requests)",
    }

    total = len(users_data)
    total_batches = int(math.ceil(total / COLLAB_BATCH_SIZE))

    def build_payload(batch: List[Dict]) -> List[Dict]:
        return [
            {
                "username": u["username"],
                "displayname": u["displayname"],
                "status": u["status"],
                "department": u["department"],
                "workingday": u["workingday"],
                "email": u.get("email", ""),
            }
            for u in batch
        ]

    if dry_run:
        print("[Colaboradores] DRY RUN - nenhuma chamada real.")
        print(f"URL: {COLLAB_API_URL}")
        if total <= COLLAB_BATCH_SIZE:
            print(f"Total: {total} (chamada unica)")
        else:
            print(f"Total: {total} (em {total_batches} blocos de ate {COLLAB_BATCH_SIZE})")
        sample = build_payload(users_data[:1])[0]
        print(f"Primeiro payload: {json.dumps(sample, ensure_ascii=False)}")
        return {"status": "dry_run", "users_count": total}

    if total <= COLLAB_BATCH_SIZE:
        payload = build_payload(users_data)
        print(f"[Colaboradores] Enviando {total} colaboradores em chamada unica...")
        try:
            response = requests.post(COLLAB_API_URL, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            msg = f"Erro na chamada unica: {str(e)[:200]}"
            print(f"[Colaboradores] {msg}")
            return {"status": "error", "error": msg}

        print(f"[Colaboradores] Resposta HTTP {response.status_code}: {response.text[:300]}")
        return {"status": "success", "successful": total}

    print(
        f"[Colaboradores] Enviando {total} colaboradores em {total_batches} blocos de ate {COLLAB_BATCH_SIZE}..."
    )
    successful = 0
    failed = 0
    errors: List[str] = []

    for idx in range(0, total, COLLAB_BATCH_SIZE):
        batch_number = (idx // COLLAB_BATCH_SIZE) + 1
        batch = users_data[idx : idx + COLLAB_BATCH_SIZE]
        payload = build_payload(batch)

        print(f"[Colaboradores] Enviando bloco {batch_number}/{total_batches} ({len(batch)} registros)...")
        try:
            response = requests.post(COLLAB_API_URL, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            successful += len(batch)
            print(f"[Colaboradores] Bloco {batch_number} OK (HTTP {response.status_code})")
        except requests.exceptions.RequestException as e:
            msg = f"Bloco {batch_number} erro: {str(e)[:200]}"
            print(f"[Colaboradores] {msg}")
            failed += len(batch)
            errors.append(msg)

    if failed:
        return {
            "status": "partial_error",
            "successful": successful,
            "failed": failed,
            "errors": errors[:3],
        }
    return {"status": "success", "successful": successful}

# =========================================================
# MAIN
# =========================================================
def main() -> None:
    print("Fluxo unificado: departamentos -> colaboradores")
    token = get_access_token()

    # 1. Departamentos
    users_for_dept = fetch_users_with_manager(token)
    departments = transform_to_departments(users_for_dept)
    print(f"[Departamentos] Total gerado: {len(departments)}")

    if SEND_DEPARTMENTS:
        dept_result = send_departments_to_api(departments, dry_run=DEPT_DRY_RUN)
        print(f"[Departamentos] Resultado: {dept_result}")
    else:
        print("[Departamentos] Envio desativado (SEND_DEPARTMENTS=False)")

    # 2. Colaboradores
    users_raw = fetch_all_users(token, only_enabled=ONLY_ENABLED)
    collaborators = transform_collaborators(
        users_raw,
        exclude_service_accounts=EXCLUDE_SERVICE_ACCOUNTS,
        exclude_without_department=EXCLUDE_WITHOUT_DEPARTMENT,
    )

    total_users = len(collaborators)
    users_without_dept = sum(1 for u in collaborators if u["department"] == "Sem Departamento")
    print(f"[Colaboradores] Total: {total_users} | Sem departamento: {users_without_dept}")

    if SEND_COLLABORATORS:
        data_to_send = collaborators[:1] if TEST_SINGLE_USER else collaborators
        if TEST_SINGLE_USER:
            print("[Colaboradores] TESTE: enviando apenas 1 usuário para validar a API.")
        collab_result = send_collaborators_to_api(data_to_send, dry_run=COLLAB_DRY_RUN)
        print(f"[Colaboradores] Resultado: {collab_result}")
    else:
        print("[Colaboradores] Envio desativado (SEND_COLLABORATORS=False)")

    print("\nFluxo completo.")


if __name__ == "__main__":
    main()

