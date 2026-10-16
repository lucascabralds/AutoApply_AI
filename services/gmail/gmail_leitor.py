import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# O escopo 'readonly' garante que o script apenas leia os e-mails, sem risco de apagar nada.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def autenticar_gmail():
    """Realiza a autenticação via OAuth2 e retorna o serviço da API."""
    creds = None
    # O arquivo token.json armazena o token de acesso. 
    # Ele é gerado automaticamente na primeira vez que você rodar o script e fizer login.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # Se não houver credenciais válidas, pede para o usuário logar no navegador.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # Salva o token para a próxima execução
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)

def obter_gama_emails(service, max_resultados=10, query=""):
    """
    Busca uma gama de e-mails.
    - max_resultados: Limite de e-mails a retornar.
    - query: Filtros do Gmail (ex: "is:unread", "newer_than:2d", "from:chefe@empresa.com").
    """
    try:
        print(f"\nBuscando e-mails (Máx: {max_resultados}, Filtro: '{query}')...")
        # Faz a requisição de listagem de mensagens
        results = service.users().messages().list(userId='me', maxResults=max_resultados, q=query).execute()
        messages = results.get('messages', [])

        if not messages:
            print('Nenhuma mensagem encontrada para esta gama.')
            return

        for msg in messages:
            # A listagem retorna apenas IDs. É preciso buscar os detalhes de cada um.
            msg_detail = service.users().messages().get(userId='me', id=msg['id'], format='metadata').execute()
            
            # Extrai os cabeçalhos (Subject, From, Date)
            headers = msg_detail['payload']['headers']
            assunto = next((header['value'] for header in headers if header['name'] == 'Subject'), 'Sem Assunto')
            remetente = next((header['value'] for header in headers if header['name'] == 'From'), 'Desconhecido')
            data = next((header['value'] for header in headers if header['name'] == 'Date'), 'Data Desconhecida')
            
            print(f"- Data: {data[:16]} | De: {remetente} | Assunto: {assunto}")

    except Exception as error:
        print(f'Ocorreu um erro ao comunicar com a API: {error}')

if __name__ == '__main__':
    # 1. Autentica no Google
    servico = autenticar_gmail()
    
    # 2. Exemplo A: Obter os últimos 5 e-mails gerais da caixa de entrada
    obter_gama_emails(servico, max_resultados=5)
    
    # 3. Exemplo B: Obter uma gama específica (ex: até 10 e-mails não lidos da última semana)
    obter_gama_emails(servico, max_resultados=10, query="is:unread newer_than:7d")

    # Até aqui solicita a gama de e-mails, mas não os processa. O processamento pode ser feito em outro módulo, onde você pode aplicar filtros adicionais, extrair informações específicas ou até mesmo responder automaticamente.  
    # Mas até o momento vamos avançar para entender como obter uma gama de e-mails, ou seja, um conjunto de mensagens que atendam a certos critérios, como os últimos 10 e-mails, ou os e-mails não lidos da última semana.
    # A função obter_gama_emails é responsável por isso. Ela aceita parâmetros para limitar o número de e-mails retornados e para aplicar filtros usando a sintaxe de pesquisa do Gmail. O resultado é uma lista de e-mails que correspondem aos critérios especificados, e cada e-mail é exibido com sua data, remetente e assunto.
    # O Ideal e pegar alguns grupos de e-mails, como os últimos 5 e os não lidos da última semana, para ter uma visão geral do que está chegando na caixa de entrada. Depois, podemos usar essas informações para decidir quais e-mails precisam de atenção imediata ou quais podem ser processados posteriormente.
    # Podemos definir um script inicial que declara o que e imediato e o que pode ser processado depois, e a partir disso, criar um fluxo de trabalho para lidar com os e-mails de forma eficiente.
    # O próximo passo seria criar um módulo de processamento que analise o conteúdo dos e-mails, extraia informações relevantes e tome ações com base nessas informações, como responder automaticamente, marcar como lido, não quero alterar onde está os e-mails, mas sim usar a automação externa para lidar com eles, sem alterar o estado dos e-mails na caixa de entrada.
    