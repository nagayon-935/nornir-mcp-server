import json
from datetime import date
from types import SimpleNamespace

import pytest
from nornir.core.inventory import Defaults, Group, Host, ParentGroups
from nornir.core.task import Result

import server
from server import (
    _JINJA_ENV,
    _get_http_timeout,
    _get_tls_verify,
    custom_http_task,
    custom_serial_task,
    custom_template_task,
    get_inventory,
    run_http_request,
)
from tests.support import FakeClient, FakeConnectHandler, FakeNornir, FakeResponse, build_agg_result, fake_host


class TestInheritedHostData:
    @pytest.fixture
    def host(self):
        defaults = Defaults(data={"http_timeout": 25, "region": "jp"})
        group = Group(
            name="tokyo",
            defaults=defaults,
            data={
                "site": "tokyo",
                "base_url": "https://group.example",
                "http_headers": {"X-Auth": "group-token"},
                "tls_verify": False,
                "serial_settings": {"port": "/dev/ttyUSB0", "baudrate": 9600},
            },
        )
        return Host(
            name="router1",
            hostname="192.168.1.1",
            platform="cisco_ios",
            groups=ParentGroups([group]),
            defaults=defaults,
            data={"role": "core"},
        )

    def test_http_request_uses_group_settings_and_default_timeout(self, host):
        client = FakeClient(FakeResponse(payload={"ok": True}))

        custom_http_task(SimpleNamespace(host=host), {False: client}, "GET", "/api")

        assert client.requests[0]["url"] == "https://group.example/api"
        assert client.requests[0]["headers"] == {"X-Auth": "group-token"}
        assert client.requests[0]["timeout"] == 25.0

    def test_http_fan_out_selects_client_using_inherited_tls_setting(self, host, patched_nornir, monkeypatch):
        created = []

        class RecordingClient:
            def __init__(self, verify, timeout):
                created.append(verify)

            def close(self):
                pass

        monkeypatch.setattr(server.httpx, "Client", RecordingClient)
        patched_nornir(FakeNornir(hosts={host.name: host}, agg_result=build_agg_result(Result(host=None, result="ok"))))

        run_http_request("GET", "/api")

        assert created == [False]

    def test_host_settings_override_group_and_default_settings(self, host):
        host.data.update(tls_verify=True, http_timeout=3)

        assert _get_tls_verify(host) is True
        assert _get_http_timeout(host) == 3.0

    def test_serial_request_uses_group_settings(self, host, monkeypatch):
        FakeConnectHandler.instances = []
        monkeypatch.setattr(server, "ConnectHandler", FakeConnectHandler)

        custom_serial_task(SimpleNamespace(host=host), "show version")

        assert FakeConnectHandler.instances[0].params["serial_settings"] == {"port": "/dev/ttyUSB0", "baudrate": 9600}

    def test_inventory_includes_group_and_default_data(self, host, patched_nornir):
        patched_nornir(FakeNornir(hosts={host.name: host}))

        data = json.loads(get_inventory())[host.name]["data"]

        assert data["site"] == "tokyo"
        assert data["region"] == "jp"
        assert data["role"] == "core"

    def test_templates_include_group_and_default_data(self, host):
        template = _JINJA_ENV.from_string("{{ host.data.site }} {{ host.data.region }} {{ host.data.role }}")

        result = custom_template_task(SimpleNamespace(host=host), template)

        assert result.result == "tokyo jp core"


def test_inventory_serializes_yaml_date_values(patched_nornir):
    patched_nornir(FakeNornir(hosts={"router1": fake_host(commissioned=date(2026, 1, 1))}))

    assert json.loads(get_inventory())["router1"]["data"]["commissioned"] == "2026-01-01"
