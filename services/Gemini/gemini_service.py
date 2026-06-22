import os
import json
import google.generativeai as genai
from dotenv import load_dotenv

# 1. Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# 2. Configura a chave da API
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("Chave da API do Gemini não encontrada. Verifique seu arquivo .env")

genai.configure(api_key=API_KEY)

def analisar_vaga_com_gemini(texto_da_vaga: str) -> dict:
    """
    Envia o texto da vaga para o Gemini e retorna um dicionário (JSON) 
    com as informações estruturadas.
    """
    # Usando o modelo flash, que é o mais rápido e barato para processamento de texto
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    # Engenharia de Prompt focada no seu caso de uso
    prompt = f"""
    Você é um assistente de RH especializado em tecnologia.
    Analise o texto da vaga abaixo e extraia as seguintes informações em formato JSON estrito:
    - "nome_empresa": (string)
    - "cargo": (string)
    - "requisitos_obrigatorios": (lista de strings)
    - "modelo_de_trabalho": (string: Remoto, Híbrido, Presencial ou Não informado)
    - "vaga_para_python_ou_dados": (booleano: true se tiver relação com Python, Dados ou Automação)

    Texto da vaga:
    {texto_da_vaga}
    """

    try:
        # Faz a requisição para a API solicitando que o retorno seja obrigatoriamente um JSON
        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.2 # Temperatura baixa para respostas mais precisas e menos criativas
            )
        )
        
        # Converte a string JSON de resposta para um dicionário Python
        dados_vaga = json.loads(response.text)
        return dados_vaga

    except Exception as e:
        print(f"[ERRO] Falha ao processar com o Gemini: {e}")
        return {}

# ==========================================
# Exemplo de Uso
# ==========================================
if __name__ == "__main__":
    vaga_exemplo = """
    A TechSolutions está buscando um Desenvolvedor Python Júnior para integrar nosso time de Automação.
    Você trabalhará com criação de scripts, integrações de APIs e manipulação de dados.
    Requisitos: Conhecimento em Python, lógica de programação e SQL básico.
    Benefícios: Vale alimentação e plano de saúde.
    Modelo: 100% Home Office.
    """
    
    print("Enviando solicitação para o Gemini...\n")
    resultado = analisar_vaga_com_gemini(vaga_exemplo)
    
    print("=== Resultado Estruturado ===")
    print(json.dumps(resultado, indent=4, ensure_ascii=False))