"""
Módulo de Integração com Google Tasks - AutoApply_AI

Este script é responsável por estabelecer a comunicação com a API do Google Tasks 
utilizando autenticação OAuth 2.0. Ele permite a criação automatizada de tarefas 
na conta do usuário.

Contexto no Projeto:
Embora atualmente esteja configurado com uma tarefa de exemplo ("Fazer Janta"), 
este módulo serve como base no projeto AutoApply_AI para automatizar lembretes 
importantes (ex: agendar follow-ups de vagas aplicadas, lembretes de entrevistas 
ou prazos de testes técnicos).

Pré-requisitos:
- Um arquivo 'credentials.json' válido (obtido via Google Cloud Console).
- A API do Google Tasks precisa estar ativada no projeto do Google Cloud.
- O primeiro uso abrirá o navegador para consentimento e gerará o 'token.json'.
"""


import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Escopo necessário para ler e escrever no Google Tasks
SCOPES = ['https://www.googleapis.com/auth/tasks']

def adicionar_tarefa():
    creds = None
    
    # O arquivo token.json armazena os tokens de acesso e atualização do usuário.
    # Ele é criado automaticamente na primeira vez que você faz login.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # Se não houver credenciais válidas disponíveis, permita que o usuário faça login.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Requer um arquivo credentials.json obtido no Google Cloud Console
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Salva as credenciais para a próxima execução do script
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    # Constrói o serviço de comunicação com a API do Google Tasks
    service = build('tasks', 'v1', credentials=creds)

    # Detalhes da tarefa que você solicitou
    nova_tarefa = {
        'title': 'Fazer Janta',
        'notes': 'Adicionado automaticamente via script.'
    }

    try:
        # Insere a tarefa na sua lista padrão (@default) do Google Tasks
        resultado = service.tasks().insert(tasklist='@default', body=nova_tarefa).execute()
        print(f"Tarefa '{resultado.get('title')}' adicionada com sucesso!")
        print(f"ID da Tarefa: {resultado.get('id')}")
    except Exception as e:
        print(f"Ocorreu um erro ao adicionar a tarefa: {e}")

if __name__ == '__main__':
    adicionar_tarefa()