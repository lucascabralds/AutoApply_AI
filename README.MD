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
```

### 2. Variáveis de Ambiente (`.env`)
O projeto utiliza um arquivo `.env` para proteger credenciais e configurar diretórios locais. 

1. Na raiz do projeto, faça uma cópia do arquivo de modelo:
```bash
cp .env.example .env
```
2. Abra o arquivo `.env` recém-criado e preencha com as suas informações reais (API Key do Gemini e o caminho da sua pasta local).

---

## 🏷️ Sistema de Marcadores (Labels) no Gmail
Para evitar processamento duplicado, o sistema interage diretamente com as *labels* do usuário, criando e atribuindo categorias automaticamente:

* 🔴 **`Certificado_Extraido`**: Aplicado quando o script consegue baixar o anexo (PDF/IMG) ou extrai e salva com sucesso a URL limpa de um certificado.
* 🟢 **`Certificados_Pendentes`**: Aplicado quando um e-mail de certificado é detectado, mas exige intervenção humana (ex: falha no download, link quebrado ou necessidade de login complexo).
* *O script ignora automaticamente qualquer e-mail que já possua uma dessas duas etiquetas.*

---

## 🚀 Sugestões de Melhorias Futuras (Roadmap)

Para as próximas fases de evolução arquitetural, considera-se a implementação de:

1. **Automação de Finanças e Despesas Domésticas:** Integrar um filtro no Gmail para identificar notas fiscais, faturas e comprovantes de contas de consumo (luz, internet). O Gemini irá extrair os dados e inseri-los automaticamente em uma tabela dedicada no banco de dados (Supabase), permitindo o cruzamento com planilhas financeiras preexistentes.
2. **Monitoramento Automotivo e Burocrático:** Implementar um módulo para varrer notificações de órgãos de trânsito (ex: Detran), referentes a CNH, IPVA, licenciamento e seguros. O assistente irá extrair datas de vencimento e valores, injetando alertas automaticamente no Google Calendar e salvando os documentos no Google Drive.
3. **Notificações Push (Webhooks):** Integrar o `alerta_acessos.py` com a API do Telegram ou Discord. Assim, ao invés de exibir os códigos OTP apenas no terminal, o sistema enviaria o PIN diretamente para o smartphone, acelerando o fluxo de login em serviços de terceiros.
4. **Containerização (Docker):** Criar um `Dockerfile` e um `docker-compose.yml` para isolar a aplicação, facilitando o deploy e a configuração de *cron jobs* para rodar a automação periodicamente em servidores ou instâncias de nuvem.
5. **Persistência em Banco de Dados:** Substituir a exportação de arquivos TXT e agrupamentos em memória por um banco de dados relacional (como PostgreSQL ou SQLite via Supabase) para criar um histórico robusto das empresas que mais enviaram vagas e mapear tendências de tecnologias solicitadas.
6. **Disparo Automatizado (`messages.send`):** Finalizar a **Fase 3** do projeto construindo a funcionalidade que anexa o currículo estruturado e realiza a candidatura ativa utilizando o protocolo MIME da Gmail API.

---

## ⚠️ Notas Técnicas Importantes

1. **Links de Rastreamento (Tracking Links):** Plataformas como a DIO (Digital Innovation One) utilizam a AWS SES (`awstrack.me`) para mascarar as URLs. O módulo de certificados utiliza `urllib.parse` para contornar esse redirecionamento e extrair o endereço limpo.
2. **Plataformas Fechadas (Udemy, LinkedIn Learning):** E-mails contendo links "Visualizar Certificado" que exigem autenticação na plataforma não baixarão o PDF via script (para evitar bloqueios por tentativas sem sessão). O sistema salva inteligentemente um arquivo `.txt` no diretório de destino apontando a URL exata para extração manual.
3. **Limpeza de Payload:** A extração da API do Gmail implementa `_limpar_html()` para contornar sujeiras comuns no formato `text/plain`, como injeções de blocos CSS inline ocultos que quebram o parser.