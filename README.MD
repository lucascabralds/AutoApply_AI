# AutoApply AI 🚀

## 🎯 Intenção do Projeto
Este projeto é um pipeline inteligente de automação de e-mails desenvolvido para otimizar a gestão da caixa de entrada e acelerar o processo de candidaturas a vagas de emprego. Construído em **Python**, o sistema integra a **Gmail API**, **Google Drive API** e modelos de IA (via gateway **Rota.IA**) para atuar como um assistente de carreira e segurança em segundo plano. 

O sistema não apenas resume comunicações diárias, mas realiza uma triagem ativa de oportunidades profissionais, decodifica links complexos de certificados, extrai códigos de segurança (OTP) de forma inteligente, valida requisitos e prepara o terreno para o envio automatizado de currículos focados em posições de tecnologia.

## 📥 O que o sistema captura?
O algoritmo monitora ativamente a conta do Gmail e realiza a extração e o processamento das seguintes informações:

* **Dados Estruturados de E-mail (Gmail API):**
  * IDs de mensagens e *threads*.
  * Extração do corpo do texto bruto (decodificando `base64url` e limpando tags HTML/CSS).
  * Metadados de anexos para extração direta de PDFs e Imagens.
* **Inteligência de Vagas e Ofertas (IA & Heurística):**
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
* **Linguagem Principal:** Python 3.10+
* **Integrações Google (GCP):** Gmail API (leitura, labels e envio), Google Drive API e Google Tasks API (opcional).
* **Inteligência Artificial:** gateway local [Rota.IA](https://github.com/lucascabralds/Rota-ia) (formato OpenAI-compatible), que escolhe o provedor (Gemini, OpenAI, Claude, Groq…) e faz fallback sozinho.
* **Persistência:** Supabase (PostgreSQL).
* **Notificações:** bot do Telegram (opcional).

---

## 📂 Estrutura de Arquivos

| Arquivo | Papel |
|---|---|
| `main.py` | Orquestrador: vagas → certificados → alertas → e-mails gerais, com labels no Gmail e gravação no Supabase. |
| `briefing_diario.py` | Briefing matinal: resume as últimas 24h (vagas por score, despesas, agendamentos, alertas) e envia para o seu Gmail. |
| `gemini_service.py` | Prompts e parse robusto do JSON da IA (cercas de markdown, vírgulas soltas, respostas truncadas). |
| `rota_ia_client.py` | Cliente HTTP do gateway Rota.IA com retry em 429/503. |
| `google_auth.py` | OAuth2 único para todos os scripts (um só `token.json`, com todos os escopos). |
| `gerar_dashboard.py` + `dashboard/template.html` | Dashboard HTML com os dados reais do Supabase (vagas por score, candidaturas, tecnologias, despesas, certificados). |
| `diagnostico.py` | Confere toda a configuração (arquivos, escopos do token, Rota.IA, tabelas do Supabase, Telegram) e diz o que falta. |
| `agendador.py` | Roda alertas, pipeline e briefing em horários definidos (usado no Docker). |
| `calendario.py` | Vencimentos e agendamentos → eventos no Google Calendar (com lembrete). |
| `notificador.py` | Envio de mensagens pelo Telegram. |
| `seguranca.py` | Redação de OTPs e links de recuperação antes de gravar/enviar. |
| `candidatura.py` | Rascunhos de candidatura no Gmail: carta gerada pela IA a partir do perfil + currículo anexado. |
| `services/gmail/add_task.py` | Follow-ups de vagas no Google Tasks. |
| `supabase/schema.sql` | Schema idempotente de todas as tabelas. |
| `tests/` | Testes automatizados (`pytest`), rodando no GitHub Actions. |
| Demais arquivos em `services/` | Protótipos anteriores, mantidos como referência (não são usados pelo `main.py`). |

---

## ⚙️ Configuração

### 1. Dependências
```bash
python -m venv venv
source venv/bin/activate        # No Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Google Cloud
1. Crie um projeto no [Google Cloud Console](https://console.cloud.google.com/) e habilite **Gmail API** e **Google Drive API** (e **Google Tasks API** / **Google Calendar API**, se for usar follow-ups e eventos).
2. Crie uma credencial OAuth do tipo *Desktop* e salve como `credentials.json` na raiz do projeto.
3. Na primeira execução o navegador abre para autorizar. O `token.json` gerado é compartilhado por todos os scripts.

### 3. Supabase
Abra o **SQL Editor** do seu projeto e rode o conteúdo de [`supabase/schema.sql`](supabase/schema.sql). O script é idempotente: cria o que falta e adiciona as colunas novas sem apagar dados — rode de novo sempre que atualizar o projeto.

> Sem a migração o pipeline continua funcionando no formato antigo (uma linha por e-mail) e avisa no terminal.

### 4. Variáveis de ambiente
```bash
cp .env.example .env
```
Preencha pelo menos `ROTA_IA_API_KEY`, `SUPABASE_URL` e `SUPABASE_KEY`. Todas as opções estão comentadas no [`.env.example`](.env.example).

### 5. Telegram (opcional)
Crie um bot com o [@BotFather](https://t.me/BotFather), preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` e teste com `python notificador.py "olá"`.

---

## 🚀 Uso

```bash
python diagnostico.py              # confere .env, token, Rota.IA, Supabase e Telegram antes de tudo
python main.py                     # todos os módulos, e-mails das últimas 24h
python main.py --dias 7            # amplia a janela
python main.py --modulo vagas      # vagas | certificados | alertas | geral
python main.py --dry-run           # não grava nada (Supabase/Drive/Telegram)
python main.py --modulo alertas --intervalo 60   # fica de plantão: OTPs no celular em ~1 min

python briefing_diario.py --preview   # imprime o briefing no terminal
python briefing_diario.py             # envia o briefing para o seu Gmail

python gerar_dashboard.py --abrir     # gera dashboard/index.html e abre no navegador
```

### 🐳 Rodando sozinho (Docker)
O `agendador.py` mantém tudo no ar sem cron: alertas a cada 1 min, pipeline completo a cada 30 min e o briefing todo dia às 07:30 (ajuste com `ALERTAS_INTERVALO_MIN`, `PIPELINE_INTERVALO_MIN` e `BRIEFING_HORARIO`).

```bash
python google_auth.py                       # 1x, na sua máquina: gera o token.json
mkdir dados && cp credentials.json token.json dados/
docker compose up -d --build
docker compose logs -f          # o dashboard fica em ./dados/dashboard.html
```
O container roda em modo headless (nunca tenta abrir navegador) e acessa o Rota.IA da máquina host via `host.docker.internal`. Sem Docker, `python agendador.py` faz o mesmo.

Para rodar os testes:
```bash
pip install -r requirements-dev.txt
pytest -q
```

---

## 🏷️ Sistema de Marcadores (Labels) no Gmail
O `main.py` cria a hierarquia abaixo e aplica sempre a label pai + a sub-label. E-mails com qualquer label `AutoApply/*` não são processados de novo.

```
AutoApply/Vagas/Alta_Prioridade      score ≥ 75
AutoApply/Vagas/Baixa_Prioridade     score < 75
AutoApply/Certificados/Extraido      arquivo salvo no Drive
AutoApply/Certificados/Pendente      requer ação manual
AutoApply/Alertas/OTP                código numérico / novo login
AutoApply/Alertas/Recuperacao        link de recuperação de senha
AutoApply/Geral/Agendamento          confirmações, reservas
AutoApply/Geral/Financeiro           faturas, boletos, recibos
AutoApply/Geral/Comunicado           avisos de empresas e bancos
AutoApply/Geral/Servicos             demais notificações
AutoApply/Geral/Veiculo              Detran, IPVA, licenciamento, multas, CNH
```

---

## 🗺️ Roadmap

**Concluído**
- [x] Persistência no Supabase com uma linha por vaga (score, empresa, modelo, salário, requisitos)
- [x] Notificações push no Telegram para OTPs e vagas de alta prioridade
- [x] Follow-ups automáticos de vagas no Google Tasks
- [x] Briefing diário por e-mail
- [x] Testes automatizados + CI
- [x] Monitoramento automotivo e vencimentos: Detran/IPVA/licenciamento/multas com label própria, gravados como despesa e, com o Calendar ligado, boletos e agendamentos viram eventos com lembrete
- [x] Rascunhos de candidatura (Fase 3, com revisão humana): carta personalizada + currículo anexado, endereçada ao recrutador quando o e-mail aparece na vaga
- [x] Dashboard com dados reais do Supabase (antes: protótipo com dados fictícios)
- [x] Docker + agendador (alertas a cada minuto, pipeline a cada 30 min, briefing diário)
- [x] Status de candidatura: "sua candidatura foi enviada" / "você avançou de fase" atualizam a vaga existente (nova → aplicada → em_processo → reprovada/aprovada), sem duplicar

**Próximos passos**
1. **Automação de Finanças:** cruzar `despesas_domesticas` com as planilhas financeiras existentes.
2. **Envio automático opcional** dos rascunhos de candidatura, depois de algumas semanas validando a qualidade das cartas.

---

## ⚠️ Notas Técnicas Importantes

1. **Links de Rastreamento (Tracking Links):** Plataformas como a DIO (Digital Innovation One) utilizam a AWS SES (`awstrack.me`) para mascarar as URLs. O módulo de certificados utiliza `urllib.parse` para contornar esse redirecionamento e extrair o endereço limpo.
2. **Plataformas Fechadas (Udemy, LinkedIn Learning):** E-mails contendo links "Visualizar Certificado" que exigem autenticação na plataforma não baixarão o PDF via script (para evitar bloqueios por tentativas sem sessão). O sistema salva inteligentemente um arquivo `.txt` no diretório de destino apontando a URL exata para extração manual.
3. **Limpeza de Payload:** A extração da API do Gmail implementa `_limpar_html()` para contornar sujeiras comuns no formato `text/plain`, como injeções de blocos CSS inline ocultos que quebram o parser.