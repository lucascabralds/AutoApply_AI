import os
import base64
import json
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import google.generativeai as genai

# ===========================================================================
# Configurações Iniciais
# ===========================================================================
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

load_dotenv()

# Configuração do Gemini
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("[ERRO] Chave GEMINI_API_KEY não encontrada no arquivo .env")
genai.configure(api_key=API_KEY)

# ===========================================================================
# 1) Autenticação no Gmail
# ===========================================================================
def autenticar_gmail():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)

# ===========================================================================
# 2) Extração Limpa do Corpo do E-mail
# ===========================================================================
def extrair_texto_email(payload):
    """
    Função simples para extrair o texto de um e-mail decodificando o base64url.
    """
    texto_extraido = ""
    if 'parts' in payload:
        for parte in payload['parts']:
            if parte.get('mimeType') == 'text/plain':
                dados = parte['body'].get('data', '')
                if dados:
                    texto_extraido += base64.urlsafe_b64decode(dados.encode('UTF-8')).decode('utf-8', errors='ignore')
    else:
        dados = payload['body'].get('data', '')
        if dados:
            texto_extraido = base64.urlsafe_b64decode(dados.encode('UTF-8')).decode('utf-8', errors='ignore')
            
    return texto_extraido.strip()

# ===========================================================================
# 3) Inteligência Artificial: Validação e Extração (O Prompt)
# ===========================================================================
def analisar_acesso_com_ia(remetente, assunto, corpo_email):
    """
    Utiliza o Gemini para ler o e-mail e determinar com precisão se é uma
    tentativa de login, enviando o código extraído em formato JSON.
    """
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    prompt = f"""
    Você é um assistente de segurança digital focado em extrair códigos de autenticação.
    Eu vou te passar os dados de um e-mail. Sua tarefa é analisar o contexto e me dizer
    se este e-mail contém um código numérico de login (OTP), uma notificação de acesso, 
    ou um link de recuperação de senha.
    
    Remetente: {remetente}
    Assunto: {assunto}
    Corpo da Mensagem:
    {corpo_email}
    
    Responda ESTRITAMENTE no seguinte formato JSON, sem crases de markdown (```json):
    {{
        "eh_email_de_acesso": true/false,
        "plataforma": "Nome da empresa (ex: Spotify, Cinemark, Ticket360)",
        "tipo_solicitacao": "otp / recuperacao_senha / alerta_novo_login / irrelevante",
        "codigo_ou_link": "Se tiver um código numérico de 4 a 6 dígitos ou link, coloque aqui. Senão, null",
        "justificativa_curta": "Explique em 1 frase por que tomou essa decisão."
    }}
    """

    try:
        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1 # Temperatura baixíssima pois queremos precisão cirúrgica, não criatividade
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[ERRO IA] Falha ao processar com Gemini: {e}")
        return None

# ===========================================================================
# 4) Motor Principal de Monitoramento
# ===========================================================================
def monitorar_codigos_de_acesso():
    service = autenticar_gmail()
    
    # Heurística inicial (Filtro do Gmail):
    # Pega apenas e-mails do último dia que tenham palavras relacionadas a login.
    # Isso economiza tokens da IA, pois não mandamos e-mails de propaganda pra ela ler.
    query = "subject:(código OR senha OR login OR password OR pin OR recuperação OR ticket) newer_than:1d"
    
    print(f"🔎 Buscando alertas de segurança com a query: '{query}'\n")
    
    try:
        resultados = service.users().messages().list(userId='me', q=query, maxResults=10).execute()
        mensagens = resultados.get('messages', [])
        
        if not mensagens:
            print("Nenhum e-mail de acesso ou recuperação encontrado hoje.")
            return

        for msg in mensagens:
            msg_detalhes = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
            headers = msg_detalhes['payload']['headers']
            
            assunto = next((h['value'] for h in headers if h['name'] == 'Subject'), 'Sem Assunto')
            remetente = next((h['value'] for h in headers if h['name'] == 'From'), 'Desconhecido')
            corpo = extrair_texto_email(msg_detalhes['payload'])
            
            # Se o corpo for muito longo, corta para economizar processamento da IA (códigos sempre estão no começo)
            corpo_curto = corpo[:800] 
            
            # Chama a IA para dar o veredito final
            analise = analisar_acesso_com_ia(remetente, assunto, corpo_curto)
            
            if analise and analise.get("eh_email_de_acesso"):
                print("==================================================")
                print(f"🔒 ALERTA DE ACESSO DETECTADO: {analise.get('plataforma')}")
                print(f"   Tipo: {analise.get('tipo_solicitacao').upper()}")
                
                # Destaca o código ou link de forma visível no terminal
                cod = analise.get("codigo_ou_link")
                if cod:
                    print(f"   🔑 CÓDIGO / LINK: ->  {cod}  <-")
                
                print(f"   Detalhe: {assunto}")
                print(f"   De: {remetente}")
                print("==================================================\n")
            else:
                # Comentário: Às vezes a query puxa um "Código de defesa do consumidor" 
                # e a IA vai bater o olho e classificar eh_email_de_acesso = false.
                pass

    except Exception as e:
        print(f"Erro na execução do monitor de acessos: {e}")

if __name__ == '__main__':
    monitorar_codigos_de_acesso()