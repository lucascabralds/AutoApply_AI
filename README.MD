# AutoApply AI 

## 🎯 Intenção do Projeto
Este projeto é um pipeline inteligente de automação de e-mails desenvolvido para otimizar a gestão da caixa de entrada e acelerar o processo de candidaturas a vagas de emprego. Construído preferencialmente em **Python**, o sistema integra a **Gmail API** com a inteligência artificial do **Google Gemini** para atuar como um assistente de carreira em segundo plano. Ele não apenas resume comunicações diárias, mas realiza uma triagem ativa de oportunidades profissionais, validando requisitos e enviando o currículo de forma automatizada apenas para vagas que dão *match* com o perfil do candidato.

## 📥 O que o sistema captura?
O algoritmo monitora ativamente a conta do Gmail e realiza a extração e o processamento das seguintes informações:

* **Dados Estruturados de E-mail (Gmail API):**
  * IDs de mensagens e *threads*.
  * Assunto (título) e remetente.
  * Corpo do texto bruto.
  * Metadados de anexos para extração de arquivos.
* **Inteligência de Vagas (Gemini API):**
  * Identificação do **nome da empresa** e do **título da vaga**.
  * Extração de **requisitos técnicos e comportamentais** solicitados na descrição.
  * Coleta da **história e contexto da empresa** presentes no corpo do e-mail.
* **Dados de Compatibilidade:**
  * Cruzamento das exigências capturadas na vaga com o **currículo e as habilidades do usuário**.
* **Síntese de Comunicação:**
  * Para e-mails que não são vagas, o sistema captura o núcleo da mensagem, ignorando assinaturas e ruídos, para gerar um **resumo executivo** do que está pendente na caixa de entrada.

## 🛠️ Stack e Integrações
* **Linguagem Principal:** Python 
* **Comunicação:** Gmail API (GCP)
* **Processamento de Linguagem Natural:** Gemini API (Google AI)
* **Estruturação de Dados:** MIME (Multipurpose Internet Mail Extensions) para envio de e-mails com anexos (currículo em PDF).