import os
import re
import base64
import requests
import urllib.parse
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ===========================================================================
# Configurações Iniciais
# ===========================================================================
# Escopo: Permite ler o e-mail e modificar (aplicar novos marcadores/labels)
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# Carrega as variáveis do .env (Garante que a pasta de destino exista)
load_dotenv()
PASTA_DESTINO = os.getenv("Certificado_Drive")

# ===========================================================================
# 1) Autenticação na API do Gmail
# ===========================================================================
def autenticar_gmail():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                os.remove('token.json')
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)

# ===========================================================================
# 2) Gerenciamento Dinâmico de Marcadores (Labels)
# ===========================================================================
def obter_ou_criar_marcador(service, nome_marcador):
    """
    Busca o ID de um marcador específico no Gmail do usuário.
    Se o marcador não existir, ele cria automaticamente e retorna o novo ID.
    """
    resultados = service.users().labels().list(userId='me').execute()
    labels = resultados.get('labels', [])
    
    for label in labels:
        if label['name'].lower() == nome_marcador.lower():
            return label['id']
            
    # Criação do marcador caso não seja encontrado
    novo_label = {
        'name': nome_marcador,
        'labelListVisibility': 'labelShow',
        'messageListVisibility': 'show'
    }
    label_criado = service.users().labels().create(userId='me', body=novo_label).execute()
    return label_criado['id']

# ===========================================================================
# 3) Lógica de Download e Validação de Links
# ===========================================================================
def tentar_baixar_link(url, assunto):
    """
    Testa a URL extraída. Se for um PDF/Imagem direto, faz o download.
    Se for uma página web (como Udemy exigindo login), salva a URL num arquivo .txt.
    Retorna True se conseguiu extrair algo útil, e False em caso de erro crítico.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        resposta = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
        content_type = resposta.headers.get('Content-Type', '').lower()
        
        # Sanitização do nome do arquivo baseado no assunto do e-mail
        nome_base = "".join([c for c in assunto if c.isalpha() or c.isdigit() or c==' ']).rstrip()
        
        if 'application/pdf' in content_type:
            caminho = os.path.join(PASTA_DESTINO, f"{nome_base[:30]}.pdf")
            with open(caminho, 'wb') as f:
                f.write(resposta.content)
            print(f"      [SUCESSO] PDF baixado: {caminho}")
            return True
            
        elif 'image/' in content_type:
            ext = content_type.split('/')[-1]
            caminho = os.path.join(PASTA_DESTINO, f"{nome_base[:30]}.{ext}")
            with open(caminho, 'wb') as f:
                f.write(resposta.content)
            print(f"      [SUCESSO] Imagem baixada: {caminho}")
            return True
            
        else:
            # Caso a página exija renderização ou login (ex: portal da DIO/Udemy)
            caminho_txt = os.path.join(PASTA_DESTINO, f"LINK_{nome_base[:30]}.txt")
            with open(caminho_txt, 'w', encoding='utf-8') as f:
                f.write(f"Vaga/Curso: {assunto}\nLink do certificado:\n{url}")
            print(f"      [AVISO] Link web salvo em TXT: {caminho_txt}")
            return True # Retorna True pois a extração do link foi um sucesso

    except Exception as e:
        print(f"      [ERRO] Falha ao tentar acessar a URL extraída: {e}")
        return False

# ===========================================================================
# 4) Fluxo Principal do Robô de Extração
# ===========================================================================
def processar_emails_certificados():
    if not PASTA_DESTINO:
        print("[ERRO] Variável 'Certificado_Drive' não definida no .env")
        return

    os.makedirs(PASTA_DESTINO, exist_ok=True)
    service = autenticar_gmail()
    
    # Mapeamento das Labels de Status (Conforme padrão de cores/tags do usuário)
    label_extraido_id = obter_ou_criar_marcador(service, "Certificado_Extraido")
    label_pendente_id = obter_ou_criar_marcador(service, "Certificados_Pendentes")
    
    # Query: Emails do último dia com 'certificado', ignorando os que JÁ foram processados
    query = "subject:certificado newer_than:1d -label:Certificado_Extraido -label:Certificados_Pendentes"
    print(f"Buscando e-mails com a query: '{query}'...\n")
    
    try:
        resultados = service.users().messages().list(userId='me', q=query, maxResults=20).execute()
        mensagens = resultados.get('messages', [])
        
        if not mensagens:
            print("Nenhum e-mail novo encontrado para processar.")
            return
            
        for msg in mensagens:
            msg_detalhes = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
            headers = msg_detalhes['payload']['headers']
            assunto = next((h['value'] for h in headers if h['name'] == 'Subject'), 'Sem Assunto')
            
            print(f"Processando E-mail: {assunto}")
            partes = msg_detalhes['payload'].get('parts', [])
            
            # Variável de controle para definir qual Label será aplicada no final
            sucesso_na_extracao = False
            
            # -----------------------------------------------------------------------
            # PARTE A: BUSCAR ANEXOS DIRETOS (Ex: Udemy enviando PDF anexo)
            # -----------------------------------------------------------------------
            for parte in partes:
                if parte.get('filename'):
                    if 'data' in parte['body']:
                        dados = parte['body']['data']
                    elif 'attachmentId' in parte['body']:
                        anexo = service.users().messages().attachments().get(
                            userId='me', messageId=msg['id'], id=parte['body']['attachmentId']
                        ).execute()
                        dados = anexo['data']
                    
                    dados_limpos = base64.urlsafe_b64decode(dados.encode('UTF-8'))
                    caminho_anexo = os.path.join(PASTA_DESTINO, parte['filename'])
                    with open(caminho_anexo, 'wb') as f:
                        f.write(dados_limpos)
                    print(f"      [SUCESSO] Anexo salvo: {caminho_anexo}")
                    sucesso_na_extracao = True

            # -----------------------------------------------------------------------
            # PARTE B: ENTENDER O E-MAIL E DESCRIPTOGRAFAR LINKS DE RASTREAMENTO
            # -----------------------------------------------------------------------
            """
            NOTA PARA DESENVOLVEDORES FUTUROS:
            Muitas plataformas usam serviços como AWS SES que substituem o link original
            do certificado por um "Tracking Link" gigante para monitorar cliques.
            
            Exemplo Real:
            https://2lspc0k8.r.us-east-1.awstrack.me/L0/https:%2F%2Fwww.dio.me%2F%2Fcertificate%2F2LFM7LLA/1/0100019ef2f3c3fd-1d51ef28-2a19-4e60-8cb5-732ad13f7120-000000/doJ_c7z7LFvNVtcT12a4l7qz5I4=473
            
            O algoritmo abaixo "entende" o e-mail lendo o HTML bruto, localiza esses links, 
            e usa urllib.parse para decodificar (%2F vira /) puxando apenas o endereço real 
            do certificado para garantir o download correto.
            """
            html_body = ""
            for parte in partes:
                if parte.get('mimeType') == 'text/html':
                    html_body = base64.urlsafe_b64decode(parte['body'].get('data', '')).decode('utf-8', errors='ignore')
            
            links_encontrados = re.findall(r'href="(https?://[^"]+)"', html_body)
            links_certificados = []
            
            for url in links_encontrados:
                # Decodifica o link da AWS/SendGrid para revelar o payload verdadeiro
                url_decodificada = urllib.parse.unquote(url)
                
                # Regra Específica: Extrair o link limpo da plataforma DIO
                m_dio = re.search(r'(https?://(?:www\.)?dio\.me/+certificate/[A-Z0-9]+)', url_decodificada, re.IGNORECASE)
                if m_dio:
                    link_limpo = m_dio.group(1).replace('//certificate', '/certificate')
                    if link_limpo not in links_certificados:
                        links_certificados.append(link_limpo)
                    continue
                
                # Regra Geral: Buscar links que indicam download ou visualização
                palavras_chave = ['cert', 'download', 'visualizar', 'credential', 'udemy']
                if any(p in url.lower() for p in palavras_chave):
                    if url not in links_certificados:
                        links_certificados.append(url)
            
            # Tenta processar os links limpos
            for url in links_certificados:
                print(f"      [PROCESSANDO LINK] URL: {url[:80]}...")
                resultado_link = tentar_baixar_link(url, assunto)
                if resultado_link:
                    sucesso_na_extracao = True
            
            # -----------------------------------------------------------------------
            # PARTE C: DEFINIR SE FOI EXTRAÍDO OU NÃO E APLICAR MARCADOR
            # -----------------------------------------------------------------------
            if sucesso_na_extracao:
                # Se baixou anexo ou extraiu link, marca como SUCESSO (Vermelho/Certificado_Extraido)
                service.users().messages().modify(
                    userId='me', 
                    id=msg['id'], 
                    body={'addLabelIds': [label_extraido_id]}
                ).execute()
                print("      [STATUS] E-mail atualizado com a label: 'Certificado_Extraido'")
            else:
                # Se não encontrou nada útil, marca como PENDENTE para revisão humana
                service.users().messages().modify(
                    userId='me', 
                    id=msg['id'], 
                    body={'addLabelIds': [label_pendente_id]}
                ).execute()
                print("      [ALERTA] Nada extraído. E-mail atualizado com a label: 'Certificados_Pendentes'")
                
            print("-" * 60)

    except Exception as e:
        print(f"Erro Crítico na execução do pipeline: {e}")

# ===========================================================================
# Execução Principal
# ===========================================================================
if __name__ == '__main__':
    processar_emails_certificados()