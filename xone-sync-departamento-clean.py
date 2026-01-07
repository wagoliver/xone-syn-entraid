import os
import json
import requests
from typing import List, Dict, Optional

# Credenciais Azure AD
TENANT_ID = os.getenv("AZ_TENANT_ID")
CLIENT_ID = os.getenv("AZ_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZ_CLIENT_SECRET")

TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_USERS_URL = "https://graph.microsoft.com/v1.0/users"

def validate_credentials():
    """Valida se as credenciais do Azure AD estão configuradas"""
    missing = []
    if not TENANT_ID:
        missing.append("AZ_TENANT_ID")
    if not CLIENT_ID:
        missing.append("AZ_CLIENT_ID") 
    if not CLIENT_SECRET:
        missing.append("AZ_CLIENT_SECRET")
    
    if missing:
        raise ValueError(f"Credenciais não configuradas: {', '.join(missing)}")
    
    print("✅ Credenciais Azure AD configuradas")
    return True

def get_access_token() -> str:
    """Obtém token de acesso do Azure AD"""
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": GRAPH_SCOPE,
        "grant_type": "client_credentials",
    }
    
    try:
        resp = requests.post(TOKEN_URL, data=data, timeout=30)
        resp.raise_for_status()
        print("✅ Token obtido com sucesso")
        return resp.json()["access_token"]
    except Exception as e:
        raise RuntimeError(f"Falha ao obter token: {e}")

def fetch_users_and_departments() -> List[Dict]:
    """Busca usuários e monta estrutura de departamentos"""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    # Busca usuários com campos necessários, incluindo expansão do manager
    url = f"{GRAPH_USERS_URL}?$select=userPrincipalName,displayName,department&$expand=manager($select=displayName,userPrincipalName)&$top=999"
    
    all_users = []
    
    while url:
        try:
            response = requests.get(url, headers=headers, timeout=60)
            response.raise_for_status()
            
            data = response.json()
            users = data.get("value", [])
            all_users.extend(users)
            
            url = data.get("@odata.nextLink")
            
        except Exception as e:
            print(f"❌ Erro ao buscar usuários: {e}")
            break
    
    print(f"✅ {len(all_users)} usuários encontrados")
    return all_users

def transform_to_departments(users: List[Dict]) -> List[Dict]:
    """Transforma dados dos usuários no formato solicitado"""
    departments = {}
    
    for user in users:
        email = user.get("userPrincipalName", "")
        display_name = user.get("displayName", "")
        department = user.get("department", "")
        manager_info = user.get("manager", {})
        
        if not department:
            continue
            
        # Extrai informações do gerente
        manager_name = "Manager Name"  # Default
        manager_email = "manager.email@arctica.com.br"  # Default
        
        if manager_info:
            manager_name = manager_info.get("displayName", "Manager Name")
            manager_email = manager_info.get("userPrincipalName", "manager.email@arctica.com.br")
            
        # Se departamento ainda não existe, cria entrada
        if department not in departments:
            departments[department] = {
                "name": department,
                "manager": manager_name,
                "manager_email": manager_email,
                "workingday_name": None,
                "user_name": email.split('@')[0] if '@' in email else email  # Username do primeiro usuário encontrado
            }
        else:
            # Se departamento já existe, mas não tem gerente definido, atualiza
            if departments[department]["manager"] == "Manager Name" and manager_name != "Manager Name":
                departments[department]["manager"] = manager_name
                departments[department]["manager_email"] = manager_email
    
    # Debug: Mostrar todos os departamentos encontrados no AD
    all_departments = {}
    for user in users:
        dept = user.get("department")
        if dept:
            if dept not in all_departments:
                all_departments[dept] = []
            all_departments[dept].append(user.get("displayName", ""))
    
    print(f"🔍 DEPARTAMENTOS ENCONTRADOS NO AZURE AD:")
    print("-" * 60)
    for dept_name, users_list in all_departments.items():
        print(f"📂 {dept_name}")
        print(f"   👥 {len(users_list)} usuários: {', '.join(users_list[:3])}{'...' if len(users_list) > 3 else ''}")
    print("-" * 60)
    
    return list(departments.values())

def send_departments_to_api(departments: List[Dict], dry_run: bool = True) -> Dict:
    """Envia departamentos para a API XoneCloud"""
    # Token da API XoneCloud - deve ser configurado como variável de ambiente
    XONE_API_TOKEN = os.getenv("XONE_API_TOKEN")
    
    if not XONE_API_TOKEN:
        raise ValueError("Token XONE_API_TOKEN não configurado! Use: setx XONE_API_TOKEN 'seu_token'")
    
    DEPARTMENTS_API_URL = "https://register-api.xonecloud.com/departments/api/v1/"
    
    headers = {
        "accept": "application/json",
        "authorization": XONE_API_TOKEN,  # Note: 'authorization' em minúsculo como no curl
        "Content-Type": "application/json",
        "User-Agent": "XoneSync-Departments/1.0 (Python/requests)"
    }
    
    print(f"🔑 Token configurado: {XONE_API_TOKEN[:30]}...")
    
    if dry_run:
        print(f"\n🧪 DRY RUN - Simulacao do POST para Departments API")
        print(f"URL: {DEPARTMENTS_API_URL}")
        print(f"Total de departamentos: {len(departments)}")
        print(f"Token: {XONE_API_TOKEN[:30]}...")
        return {"status": "dry_run", "departments_count": len(departments)}
    
    print(f"\n📤 Enviando {len(departments)} departamentos para XoneCloud API...")
    print(f"URL: {DEPARTMENTS_API_URL}")
    print("-" * 60)
    
    try:
        # Formato correto baseado no curl fornecido
        payload = {
            "lang": "pt-BR",
            "departments": departments
        }
        
        print(f"📦 Enviando payload com {len(departments)} departamentos...")
        print(f"📋 Estrutura: {{'lang': 'pt-BR', 'departments': [array com {len(departments)} itens]}}")
        
        response = requests.post(DEPARTMENTS_API_URL, headers=headers, json=payload, timeout=30)
        
        if response.status_code in [200, 201]:
            print(f"✅ SUCESSO: {response.status_code}")
            result = response.json()
            print(f"✅ Resposta: {json.dumps(result, indent=2, ensure_ascii=False)}")
            return {"status": "success", "successful": len(departments), "failed": 0}
        else:
            print(f"❌ ERRO: HTTP {response.status_code}: {response.text[:300]}")
            return {"status": "error", "error": f"HTTP {response.status_code}", "successful": 0, "failed": len(departments)}
        
    except requests.exceptions.RequestException as e:
        error_msg = f"Conexão: {str(e)[:100]}"
        print(f"❌ ERRO CONEXÃO: {error_msg}")
        return {"status": "error", "error": error_msg, "successful": 0, "failed": len(departments)}
    
    # Relatório final será tratado pelo return acima
    # Não precisa mais do loop de relatório individual

if __name__ == "__main__":
    print("🚀 Iniciando análise de departamentos Azure AD")
    print("=" * 50)
    
    # Configurações
    SEND_TO_API = True           # True = enviar para XoneCloud API
    DRY_RUN = False              # True = apenas simular, False = enviar de verdade
    
    try:
        # 1. Validar credenciais
        validate_credentials()
        
        # 2. Buscar usuários
        users = fetch_users_and_departments()
        
        # 3. Transformar em departamentos
        departments = transform_to_departments(users)
        
        print(f"📊 {len(departments)} departamentos encontrados")
        print("=" * 50)
        
        # 4. Mostrar resultado
        result = json.dumps(departments, ensure_ascii=False, indent=2)
        print(result)
        
        print("\n" + "=" * 50)
        print(f"✅ Processamento concluído - {len(departments)} departamentos")
        
        # 5. Enviar para API se configurado
        if SEND_TO_API:
            print(f"\n🔗 Integrando com XoneCloud Departments API...")
            print("=" * 60)
            
            api_result = send_departments_to_api(departments, dry_run=DRY_RUN)
            
            if api_result["status"] == "success":
                print("🎉 Sincronização de departamentos concluída com sucesso!")
            elif api_result["status"] == "partial_success":
                successful = api_result["successful"]
                failed = api_result["failed"]
                print(f"⚠️ Sincronização parcial: {successful} sucessos, {failed} falhas")
            elif api_result["status"] == "dry_run":
                print("🧪 Simulação concluída - altere DRY_RUN=False para enviar de verdade")
            else:
                print(f"❌ Erro na sincronização: {api_result.get('error', 'Erro desconhecido')}")
        else:
            print(f"\n📋 Para enviar para XoneCloud API, configure:")
            print("   SEND_TO_API = True")
            print("   DRY_RUN = False  (quando quiser enviar de verdade)")
        
    except Exception as e:
        print(f"❌ Erro: {e}")
