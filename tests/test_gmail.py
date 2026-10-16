import main


class _Exec:
    def __init__(self, valor):
        self._valor = valor

    def execute(self):
        return self._valor


class _FakeMessages:
    """Simula messages.list paginado: 250 ids em páginas de até 100."""

    def __init__(self, total=250):
        self.ids = [{"id": str(i)} for i in range(total)]
        self.chamadas = []

    def list(self, userId, q, maxResults, pageToken=None):
        self.chamadas.append((q, maxResults, pageToken))
        inicio = int(pageToken or 0)
        fim = inicio + maxResults
        resp = {"messages": self.ids[inicio:fim]}
        if fim < len(self.ids):
            resp["nextPageToken"] = str(fim)
        return _Exec(resp)


class _FakeService:
    def __init__(self, total=250):
        self.msgs = _FakeMessages(total)

    def users(self):
        return self

    def messages(self):
        return self.msgs


def test_listar_mensagens_segue_paginacao():
    service = _FakeService(250)
    msgs = main._listar_mensagens(service, "newer_than:7d", limite=1000)
    assert len(msgs) == 250
    assert len(service.msgs.chamadas) == 3


def test_listar_mensagens_respeita_limite():
    service = _FakeService(250)
    msgs = main._listar_mensagens(service, "q", limite=120)
    assert [m["id"] for m in msgs] == [str(i) for i in range(120)]
    assert service.msgs.chamadas[-1][1] == 20


def test_iterar_partes_desce_em_multipart_aninhado():
    payload = {
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain"}, {"mimeType": "text/html"},
            ]},
            {"mimeType": "application/pdf", "filename": "certificado.pdf"},
        ]
    }
    tipos = [p["mimeType"] for p in main._iterar_partes(payload)]
    assert tipos == ["multipart/alternative", "text/plain", "text/html", "application/pdf"]


def test_query_exclui_todos_os_modulos():
    for nome in ("AutoApply/Vagas", "AutoApply/Certificados", "AutoApply/Alertas", "AutoApply/Geral"):
        assert f"-label:{nome}" in main._QUERY_JA_PROCESSADO
