import os
import sys
import json
from dotenv import load_dotenv

# Permite importar rota_ia_client.py (na raiz do projeto) a partir deste
# módulo, que vive em services/Gemini/.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from rota_ia_client import chamar_ia as _chamar_rota_ia

# 1. Carrega as variáveis de ambiente do arquivo .env
load_dotenv()


def analisar_vaga_com_gemini(texto_da_vaga: str) -> dict:
    """
    Envia o texto da vaga para o gateway Rota.IA
    (http://localhost:8787/v1/chat/completions) e retorna um dicionário (JSON)
    com as informações estruturadas.
    """
    # Engenharia de Prompt focada no seu caso de uso
    prompt = f"""
    Você é um assistente de RH especializado em tecnologia.
    Analise o texto da vaga abaixo e extraia as seguintes informações em formato JSON estrito:
    - "nome_empresa": (string)
    - "cargo": (string)
    - "requisitos_obrigatorios": (lista de strings)
    - "modelo_de_trabalho": (string: Remoto, Híbrido, Presencial ou Não informado)
    - "vaga_para_python_ou_dados": (booleano: true se tiver relação com Python, Dados ou Automação)

    Responda APENAS com o JSON (sem crases, sem texto fora do JSON).

    Texto da vaga:
    {texto_da_vaga}
    """

    try:
        # Faz a requisição para o gateway Rota.IA solicitando que o retorno seja JSON
        resposta = _chamar_rota_ia(prompt, temperature=0.2)

        # Converte a string JSON de resposta para um dicionário Python
        dados_vaga = json.loads(resposta)
        return dados_vaga

    except Exception as e:
        print(f"[ERRO] Falha ao processar via Rota.IA: {e}")
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

    print("Enviando solicitação para o gateway Rota.IA...\n")
    resultado = analisar_vaga_com_gemini(vaga_exemplo)

    print("=== Resultado Estruturado ===")
    print(json.dumps(resultado, indent=4, ensure_ascii=False))
