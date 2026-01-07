import os
import json
import time
import re
import requests
from typing import List, Dict, Optional
import azure.functions as func

# =========================================================
# CONFIGURAÇÕES GERAIS VIA VARIÁVEIS DE AMBIENTE
# =========================================================
TENANT_ID = os.getenv("AZ_TENANT_ID")
CLIENT_ID = os.getenv("AZ_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZ_CLIENT_SECRET")

XONE_API_TOKEN = os.getenv("XONE_API_TOKEN")

COLLAB_API_URL = "https://register-api.xonecloud.com/collaborators/api/v1"
DEPT_API_URL = "https://register-api.xonecloud.com/departments/api/v1/"

ONLY_ENABLED = False
EXCLUDE_SERVICE_ACCOUNTS = True
EXCLUDE_WITHOUT_DEPARTMENT = True

SEND_DEPARTMENTS = True
DEPT_DRY_RUN = False

SEND_COLLABORATORS = True
COLLAB_DRY_RUN = False
TEST_SINGLE_USER = False

TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_USERS_URL = "https://graph.microsoft.com/v1.0/users"
SELECT_COLLAB_FIELDS = "userPrincipalName,displayName,accountEnabled,department,employeeId"
PAGE_SIZE = 999


# =========================================================
# FUNÇÕES DO SEU SCRIPT ORIGINAL
# =========================================================
def ensure_azure_credentials():
    missing = []
    if not TENANT_ID: missing.append("AZ_TENANT_ID")
    if not CLIENT_ID: missing.append("AZ_CLIENT_ID")
    if not CLIENT_SECRET: missing.append("AZ_CLIENT_SECRET")
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
    last_err = None
    for i in range(retries):
        try:
            resp = requests.post(TOKEN_URL, data=data, timeout=30)
            resp.raise_for_status()
            return resp.json()["access_token"]
        except Exception as e:
            last_err = e
            time.sleep(backoff ** (i + 1))
    raise RuntimeError(f"Falha ao obter token: {last_err}")


def fetch_users_with_manager(token: str) -> List[Dict]:
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"{GRAPH_USERS_URL}?"
        f"$select=userPrincipalName,displayName,department"
        f"&$expand=manager($select=displayName,userPrincipalName)"
        f"&$top={PAGE_SIZE}"
    )

    all_users = []
    while url:
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        all_users.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return all_users


def transform_to_departments(users: List[Dict]) -> List[Dict]:
    departments = {}
    for user in users:
        email = user.get("userPrincipalName", "")
        department = user.get("department", "")
        manager = user.get("manager", {}) or {}

        if not department:
            continue

        departments.setdefault(department, {
            "name": department,
            "manager": manager.get("displayName", "Manager Name"),
            "manager_email": manager.get("userPrincipalName", "manager@email"),
            "workingday_name": None,
            "user_name": email.split("@")[0],
        })
    return list(departments.values())


def send_departments_to_api(departments: List[Dict], dry_run=True):
    if not XONE_API_TOKEN:
        raise RuntimeError("XONE_API_TOKEN não configurado.")

    headers = {
        "accept": "application/json",
        "authorization": XONE_API_TOKEN,
        "Content-Type": "application/json",
    }

    payload = {"lang": "pt-BR", "departments": departments}

    if dry_run:
        print("[DEPARTAMENTOS] DRY RUN")
        return {"status": "dry_run"}

    resp = requests.post(DEPT_API_URL, headers=headers, json=payload, timeout=30)
    return resp.json()


def fetch_all_users(token: str, only_enabled=False):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_USERS_URL}?$select={SELECT_COLLAB_FIELDS}&$top={PAGE_SIZE}"
    users = []

    while url:
        r = requests.get(url, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        users.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return users


ALLOWED_USERNAME_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_USERNAME_LEN = 32


def normalize_username(raw: str) -> str:
    if not raw:
        return ""
    return ALLOWED_USERNAME_RE.sub("", raw)[:MAX_USERNAME_LEN]


def build_username(user: Dict) -> str:
    empid = (user.get("employeeId") or "").strip()
    if empid:
        candidate = normalize_username(empid)
        if candidate:
            return candidate

    email_local = (user.get("userPrincipalName") or "").split("@")[0]
    return normalize_username(email_local)


def transform_collaborators(users_msgraph: List[Dict]) -> List[Dict]:
    out = []
    seen = set()

    for u in users_msgraph:
        username = build_username(u)
        display = u.get("displayName") or ""
        email = u.get("userPrincipalName") or ""
        dept = u.get("department") or "Sem Departamento"

        if username in seen:
            username = f"{username}-dup"
        seen.add(username)

        out.append({
            "username": username,
            "displayname": display,
            "status": u.get("accountEnabled", False),
            "department": dept,
            "workingday": "Jornada padrão",
            "email": email
        })
    return out


def send_collaborators_to_api(users_data: List[Dict], dry_run=True):
    if not XONE_API_TOKEN:
        raise RuntimeError("XONE_API_TOKEN não configurado.")

    headers = {
        "accept": "application/json",
        "Authorization": XONE_API_TOKEN,
        "Content-Type": "application/json",
    }

    if dry_run:
        print("[COLABORADORES] DRY RUN")
        return {"status": "dry_run"}

    resp = requests.post(COLLAB_API_URL, headers=headers, json=users_data, timeout=60)
    return resp.json()


# =========================================================
# ENTRYPOINT DA AZURE FUNCTION
# =========================================================
def main(mytimer: func.TimerRequest) -> None:
    print("⚡ Execução iniciada via Azure Function Timer")

    token = get_access_token()

    # 1. Departamentos
    users_for_dept = fetch_users_with_manager(token)
    departments = transform_to_departments(users_for_dept)

    if SEND_DEPARTMENTS:
        result_dept = send_departments_to_api(departments, dry_run=DEPT_DRY_RUN)
        print(result_dept)

    # 2. Colaboradores
    users_raw = fetch_all_users(token)
    collabs = transform_collaborators(users_raw)

    if SEND_COLLABORATORS:
        result_collab = send_collaborators_to_api(collabs, dry_run=COLLAB_DRY_RUN)
        print(result_collab)

    print("Execução concluída.")
