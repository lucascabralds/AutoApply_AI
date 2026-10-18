"""Leitura determinística de digests (Catho e Indeed) — fixtures com a marcação real, dados fictícios."""
import base64
import json

import main
import parsers_vagas as p


def _rastreio_catho(url_real: str) -> str:
    payload = base64.b64encode(json.dumps({"c": "jobalerts", "r": url_real}).encode()).decode()
    return "https://www.catho.com.br/channel-tracking/v1/email/clicked?i=" + payload.replace("=", "%3D")


def _card_catho(cargo, empresa_cidade, salario, url_real):
    # Mesma estrutura do card real: <a> envolvendo tabela com span (cargo),
    # td "EMPRESA\n| Cidade" e span "Salário: ..."
    return f'''<tr><td bgcolor=#ffffff width=100% align=center><a href="{_rastreio_catho(url_real)}" target=_blank>
<table width=520 cellpadding=0 cellspacing=0><tbody><tr><td align=center width=100%><!--[if (gte mso 9)|(IE)]><![endif]--><div><table width=100%><tbody>
<tr><td align=left width=100%><table align=right width=100%><tbody><tr><td align=left>
<span style="color:#3624D6;text-decoration:underline">{cargo}</span>
<tr><td align=left style="color:#666666;padding-top:6px">{empresa_cidade}
<tr><td align=left><span style="color:#666666">Salário: {salario}</span>
</table></table></div></table></a>'''


HTML_CATHO = (
    "<html><body><table>"
    f'<a href="{_rastreio_catho("https://www.catho.com.br/vagas/?q=dados&utm_source=x")}">Ver todos os resultados</a>'
    f'<a href="{_rastreio_catho("https://www.catho.com.br/central-preferencias/unsubscribe?tk=1")}">Descadastre-se</a>'
    + _card_catho("Analista de Dados Jr - Python", "EMPRESA FICTICIA ALFA\n| São Paulo", "A Combinar",
                  "https://www.catho.com.br/vagas/analista-de-dados-jr-python/11111111/?imgid=1&utm_source=b2c")
    + _card_catho("Desenvolvedor Python Júnior Home Office", "Empresa Confidencial\n| Campinas", "De R$ 3.001,00 a R$ 4.000,00",
                  "https://www.catho.com.br/vagas/desenvolvedor-python-junior/22222222/?utm_medium=email")
    + _card_catho("Analista de Dados Jr - Python", "EMPRESA FICTICIA ALFA\n| São Paulo", "A Combinar",   # repetido
                  "https://www.catho.com.br/vagas/analista-de-dados-jr-python/11111111/?outro=param")
    + f'<a href="{_rastreio_catho("https://www.catho.com.br/vagas/?q=dados")}">ver todas as vagas</a>'
    "</table></body></html>"
)

TEXTO_INDEED = """Indeed Job Alert
20 vagas abertas de Python - Estado de São Paulo

Jobs 1-3 of 3 new jobs
See matching results on Indeed: https://br.indeed.com/jobs?q=Python&l=SP&alid=abc&tmtk=zzz

Desenvolvedor Python Junior
Empresa Fictícia Beta - Osasco, SP
R$ 4.000 – R$ 5.500 por mês
Candidate-se facilmente
Experiência com Python, SQL e APIs REST. Atuação 100% remota com o time de dados…
há 3 dias
https://br.indeed.com/rc/clk/dl?jk=0123abcd4567ef89&from=ja&qd=TOKENPESSOAL&tk=TOKEN&bb=OUTRO

Analista de Testes e Qualidade Jr
Empresa Gama Ltda. - Comércio - Indaiatuba, SP
Testes automatizados e documentação de casos de teste.
Recém publicada
https://br.indeed.com/rc/clk/dl?jk=fedcba9876543210&from=ja&qd=X

Desenvolvedor Python Junior
Empresa Fictícia Beta - Osasco, SP
Experiência com Python.
há 3 dias
https://br.indeed.com/rc/clk/dl?jk=0123abcd4567ef89&from=ja

Não compartilhe este e-mail
Este e-mail contém links seguros personalizados para você.
Gerenciar alertas de vagas: https://subscriptions.indeed.com?token=XYZ
"""


def test_catho_le_todas_as_vagas_com_empresa_local_e_salario():
    vagas = p.extrair("Catho <avisovagas@catho.com.br>", "", HTML_CATHO,
                      main._extrair_tecnologias, main._resolver_link_real)
    assert [v["cargo"] for v in vagas] == ["Analista de Dados Jr - Python", "Desenvolvedor Python Júnior Home Office"]
    a, b = vagas
    assert (a["empresa"], a["local"], a["salario"]) == ("EMPRESA FICTICIA ALFA", "São Paulo", None)
    assert a["link_candidatura"] == "https://www.catho.com.br/vagas/analista-de-dados-jr-python/11111111/"
    assert b["salario"] == "De R$ 3.001,00 a R$ 4.000,00" and b["modelo_trabalho"] == "Remoto"
    assert "python" in a["tecnologias"] and a["origem_extracao"] == "parser"


def test_indeed_le_blocos_e_limpa_tokens_pessoais_do_link():
    vagas = p.extrair("Indeed <donotreply@jobalert.indeed.com>", TEXTO_INDEED, "", main._extrair_tecnologias)
    assert [v["cargo"] for v in vagas] == ["Desenvolvedor Python Junior", "Analista de Testes e Qualidade Jr"]
    dev, qa = vagas
    assert (dev["empresa"], dev["local"]) == ("Empresa Fictícia Beta", "Osasco, SP")
    assert dev["salario"] == "R$ 4.000 – R$ 5.500 por mês"
    assert dev["link_candidatura"] == "https://br.indeed.com/viewjob?jk=0123abcd4567ef89"   # sem qd/tk/bb
    assert dev["modelo_trabalho"] == "Remoto" and {"python", "sql"} <= set(dev["tecnologias"])
    assert "há 3 dias" not in (dev["resumo"] or "") and "Candidate-se" not in (dev["resumo"] or "")
    # empresa com " - " no nome: a cidade é o que vem depois do ÚLTIMO " - "
    assert (qa["empresa"], qa["local"]) == ("Empresa Gama Ltda. - Comércio", "Indaiatuba, SP")


def test_molde_desconhecido_ou_sem_vagas_volta_para_a_ia():
    assert p.extrair("Gupy <noreply@gupy.io>", "texto", "<html></html>", main._extrair_tecnologias) is None
    # e-mail "empresas destacadas" do InfoJobs: só um botão, nenhuma vaga
    assert p.extrair("info@infojobs.com.br", "VER NOVAS VAGAS", "<a href='https://www.infojobs.com.br/x'>VER</a>",
                     main._extrair_tecnologias) is None
    # Catho sem cards (ex.: só "ver todas as vagas")
    html = f'<a href="{_rastreio_catho("https://www.catho.com.br/vagas/?q=x")}">ver todas</a>'
    assert p.extrair("avisovagas@catho.com.br", "", html, main._extrair_tecnologias, main._resolver_link_real) is None


def test_html_quebrado_nunca_derruba_o_pipeline():
    def explode(_):
        raise RuntimeError("molde mudou")
    assert p.extrair("avisovagas@catho.com.br", "", HTML_CATHO, explode, main._resolver_link_real) is None


def test_payload_de_rastreio_com_mais_e_barra_e_decodificado():
    # base64 padrão pode ter "+" e "/"; o parse_qs troca "+" por espaço
    url_real = "https://www.catho.com.br/vagas/dev/33333333/?x=>>>???"
    payload = base64.b64encode(json.dumps({"r": url_real}).encode()).decode()
    assert "+" in payload or "/" in payload
    href = "https://www.catho.com.br/channel-tracking/v1/email/clicked?i=" + payload
    assert main._resolver_link_real(href) == url_real


def test_vagas_de_empresa_confidencial_nao_sao_fundidas():
    from formatacao import empresa_conhecida
    assert not empresa_conhecida("Empresa Confidencial") and empresa_conhecida("ACME")
    a = main._vaga_uid("m1", "Assistente de Compras", "Empresa Confidencial", "https://www.catho.com.br/vagas/x/1/")
    b = main._vaga_uid("m1", "Assistente de Compras", "Empresa Confidencial", "https://www.catho.com.br/vagas/x/2/")
    assert a != b
