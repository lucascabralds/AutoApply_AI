# AutoApply AI 🚀

## 🎯 Intenção do Projeto
Este projeto é um pipeline inteligente de automação de e-mails desenvolvido para otimizar a gestão da caixa de entrada e acelerar o processo de candidaturas a vagas de emprego. Construído em **Python**, o sistema integra a **Gmail API**, **Google Drive API** e a inteligência artificial do **Google Gemini** para atuar como um assistente de carreira e segurança em segundo plano. 

O sistema não apenas resume comunicações diárias, mas realiza uma triagem ativa de oportunidades profissionais, decodifica links complexos de certificados, extrai códigos de segurança (OTP) de forma inteligente, valida requisitos e prepara o terreno para o envio automatizado de currículos focados em posições de tecnologia.

## 📥 O que o sistema captura?
O algoritmo monitora ativamente a conta do Gmail e realiza a extração e o processamento das seguintes informações:

* **Dados Estruturados de E-mail (Gmail API):**
  * IDs de mensagens e *threads*.
  * Extração do corpo do texto bruto (decodificando `base64url` e limpando tags HTML/CSS).
  * Metadados de anexos para extração direta de PDFs e Imagens.
* **Inteligência de Vagas e Ofertas (Gemini API & Heurística):**
  * Capacidade de extrair **múltiplas vagas (N ofertas)** de um único e-mail (ex: Newsletters da Catho, Indeed, Infojobs).
  * Extração de **requisitos técnicos e comportamentais** solicitados na descrição.
  * Cruzamento das exigências capturadas com o perfil do candidato.
* **Gestão de Certificados e Capacitação:**
  * Identificação de e-mails contendo certificados de plataformas educacionais (Udemy, DIO, etc.).
  * Decodificação de **Links de Rastreamento (AWS SES/SendGrid)** para extração da URL real do certificado.
* **Monitoramento de Acessos e Segurança:**
  * Extração em tempo real de **códigos numéricos (OTP)**, links de login temporários e links de recuperação de senha (ex: Spotify, Cinemark, Ticket360).
  * Identificação de contexto via IA para descartar falsos positivos (como e-mails promocionais com "códigos de desconto").

## 🛠️ Stack e Integrações
* **Linguagem Principal:** Python 3.x
* **Integrações Google (GCP):** Gmail API (Leitura e Modificação) e Google Drive API.
* **Inteligência Artificial:** Gemini API (`gemini-2.5-flash`).
* **Bibliotecas Essenciais:** `google-api-python-client`, `google-auth-oauthlib`, `python-dotenv`, `requests`, `urllib.parse`.

---

## 📂 Estrutura de Arquivos Principal

* **`extrator_vagas_v2.py`**: Motor de extração focado em varrer a caixa de entrada, identificar plataformas de emprego e estruturar JSONs com múltiplas ofertas contidas no mesmo e-mail.
* **`gmail_extrator_certificados.py`**: Script dedicado a buscar aprovações em cursos, baixar PDFs anexos ou extrair links de plataformas, salvando-os em um diretório local.
* **`alerta_acessos.py`**: Assistente de segurança digital que monitora a entrada de OTPs, PINs e links de login temporários, validando a intenção do e-mail através do Gemini e imprimindo a credencial diretamente no terminal.
* **`ai_engine.py`**: Módulo de integração com o Gemini para forçar saídas em JSON estrito e realizar a validação de *match* de perfil.
* **`classifier.py`**: Filtro heurístico rápido para poupar tokens de IA, descartando falsos positivos (ex: e-mails de condomínio) antes de chamar o Gemini.

---

## ⚙️ Pré-requisitos e Configuração de Ambiente

### 1. Dependências
Crie um ambiente virtual e instale as dependências listadas:
```bash
python -m venv venv
source venv/bin/activate  # No Windows: venv\Scripts\activate
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib google-generativeai python-dotenv requests