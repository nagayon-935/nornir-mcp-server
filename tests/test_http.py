import logging
from types import SimpleNamespace

import pytest
from nornir.core.task import Result

import server
from server import _get_http_timeout, _get_tls_verify, custom_http_task, run_http_request
from tests.support import FakeClient, FakeNornir, FakeResponse, build_agg_result, fake_host


class TestHostHttpSettings:
    def test_tls_verify_defaults_to_enabled(self):
        assert _get_tls_verify(fake_host()) is True

    def test_tls_verify_treats_none_as_enabled(self):
        assert _get_tls_verify(fake_host(tls_verify=None)) is True

    def test_tls_verify_can_be_disabled(self):
        assert _get_tls_verify(fake_host(tls_verify=False)) is False

    def test_tls_verify_rejects_non_boolean_values(self):
        with pytest.raises(ValueError, match="tls_verify"):
            _get_tls_verify(fake_host(tls_verify="false"))

    def test_http_timeout_defaults_to_module_constant(self):
        assert _get_http_timeout(fake_host()) == server.HTTP_TIMEOUT

    def test_http_timeout_treats_none_as_default(self):
        assert _get_http_timeout(fake_host(http_timeout=None)) == server.HTTP_TIMEOUT

    def test_http_timeout_is_overridable_per_host(self):
        assert _get_http_timeout(fake_host(http_timeout=45)) == 45.0


class TestCustomHttpTask:
    def _run(self, data, response, method="GET", path="/api/v1/system", json_data=None):
        host = fake_host(**data)
        secure_client = FakeClient(response)
        insecure_client = FakeClient(response)
        result = custom_http_task(
            SimpleNamespace(host=host),
            {True: secure_client, False: insecure_client},
            method,
            path,
            json_data=json_data,
        )
        active_client = insecure_client if data.get("tls_verify") is False else secure_client
        return result, active_client.requests[0]

    def test_defaults_base_url_to_https_hostname(self):
        _, request = self._run({}, FakeResponse(payload={"ok": True}))

        assert request["url"] == "https://192.168.1.1/api/v1/system"

    def test_raises_when_neither_base_url_nor_hostname_set(self):
        host = fake_host(hostname=None)
        client = FakeClient(FakeResponse(payload={}))

        with pytest.raises(ValueError, match="base_url"):
            custom_http_task(SimpleNamespace(host=host), {True: client}, "GET", "/api")

    def test_joins_base_url_and_path_without_doubling_slashes(self):
        _, request = self._run({"base_url": "https://dev.example/"}, FakeResponse(payload={}))

        assert request["url"] == "https://dev.example/api/v1/system"

    def test_passes_per_host_timeout_headers_and_json_payload(self):
        data = {"http_timeout": 30, "http_headers": {"X-Auth": "token"}}

        _, request = self._run(data, FakeResponse(payload={}), method="POST", json_data={"enabled": True})

        assert request["method"] == "POST"
        assert request["timeout"] == 30.0
        assert request["headers"] == {"X-Auth": "token"}
        assert request["json"] == {"enabled": True}

    def test_selects_insecure_client_and_logs_warning_when_tls_disabled(self, caplog):
        with caplog.at_level(logging.WARNING):
            _, request = self._run({"tls_verify": False}, FakeResponse(payload={"ok": True}))

        assert request["url"] == "https://192.168.1.1/api/v1/system"
        assert "TLS verification disabled for host router1" in caplog.text

    def test_returns_parsed_json_body(self):
        result, _ = self._run({}, FakeResponse(payload={"version": "17.3"}))

        assert result.result == {"version": "17.3"}

    def test_falls_back_to_text_for_non_json_body(self):
        result, _ = self._run({}, FakeResponse(payload=None, text="plain body"))

        assert result.result == "plain body"


class TestHttpClientFanOut:
    def test_builds_one_client_per_distinct_tls_verify_value(self, patched_nornir, monkeypatch):
        created = []

        class RecordingClient:
            def __init__(self, verify, timeout):
                self.verify = verify
                self.closed = False
                created.append(self)

            def close(self):
                self.closed = True

        monkeypatch.setattr(server.httpx, "Client", lambda verify, timeout: RecordingClient(verify, timeout))
        hosts = {
            "secure": fake_host(),
            "also_secure": fake_host(tls_verify=True),
            "insecure": fake_host(tls_verify=False),
        }
        agg_result = build_agg_result(Result(host=None, result="ok"))
        patched_nornir(FakeNornir(hosts=hosts, agg_result=agg_result))

        run_http_request("GET", "/api")

        assert sorted(client.verify for client in created) == [False, True]
        assert all(client.closed for client in created)

    def test_closes_existing_clients_when_later_client_creation_fails(self, patched_nornir, monkeypatch):
        created = []

        class RecordingClient:
            closed = False

            def close(self):
                self.closed = True

        def create_client(**kwargs):
            if created:
                raise RuntimeError("client initialization failed")
            client = RecordingClient()
            created.append(client)
            return client

        monkeypatch.setattr(server.httpx, "Client", create_client)
        nr = patched_nornir(FakeNornir(hosts={"r1": fake_host(), "r2": fake_host(tls_verify=False)}))

        output = run_http_request("GET", "/api")

        assert "client initialization failed" in output
        assert created[0].closed is True
        assert nr.closed is True
        assert nr.run_calls == []

    def test_closes_all_resources_even_when_a_client_close_raises(self, patched_nornir, monkeypatch):
        created = []

        class RecordingClient:
            def __init__(self, **kwargs):
                self.closed = False
                created.append(self)

            def close(self):
                self.closed = True
                raise RuntimeError("client close failed")

        monkeypatch.setattr(server.httpx, "Client", RecordingClient)
        nr = patched_nornir(
            FakeNornir(
                hosts={"r1": fake_host(), "r2": fake_host(tls_verify=False)},
                agg_result=build_agg_result(Result(host=None, result="ok")),
            )
        )

        output = run_http_request("GET", "/api")

        assert "client close failed" in output
        assert all(client.closed for client in created)
        assert nr.closed is True
