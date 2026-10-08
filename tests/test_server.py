import json
from datetime import date
from types import SimpleNamespace

import pytest
from nornir.core import Nornir
from nornir.core.inventory import Defaults, Group, Host, Inventory, ParentGroups
from nornir.core.task import AggregatedResult, MultiResult, Result
from nornir.plugins.runners import SerialRunner

import server
from server import (
    _JINJA_ENV,
    _format_agg_result,
    _get_http_timeout,
    _get_tls_verify,
    _resolve_serial_device_type,
    custom_http_task,
    custom_serial_task,
    custom_template_task,
    generate_config,
    get_inventory,
    run_http_request,
    run_netmiko_command,
    run_netmiko_config,
    run_serial_command,
)

NO_MATCH_MESSAGE = "No hosts matched the filter criteria."


def _fake_host(**data) -> Host:
    """Build a real inventory host without opening device connections."""
    return Host(name="router1", hostname="192.168.1.1", data=data)


def _build_agg_result(result: Result) -> AggregatedResult:
    multi_result = MultiResult("task")
    multi_result.append(result)
    agg_result = AggregatedResult("task")
    agg_result["router1"] = multi_result
    return agg_result


class FakeNornir:
    """Stands in for a filtered Nornir object so tools can be tested without devices."""

    def __init__(self, hosts=None, agg_result=None, run_error=None):
        self.inventory = SimpleNamespace(hosts=hosts if hosts is not None else {})
        self.agg_result = agg_result
        self.run_error = run_error
        self.closed = False
        self.closed_failed_hosts = False
        self.run_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close_connections(on_failed=True)
        return False

    def run(self, task, **kwargs):
        self.run_calls.append((task, kwargs))
        if self.run_error is not None:
            raise self.run_error
        return self.agg_result

    def close_connections(self, on_failed=False):
        self.closed = True
        self.closed_failed_hosts = on_failed


@pytest.fixture
def patched_nornir(monkeypatch):
    """Install a FakeNornir and record the arguments _get_nornir was called with."""

    calls = []

    def install(nr):
        def fake_get_nornir(filter_criteria=None, num_workers=None):
            calls.append({"filter_criteria": filter_criteria, "num_workers": num_workers})
            return nr

        monkeypatch.setattr(server, "_get_nornir", fake_get_nornir)
        return nr

    install.calls = calls
    return install


# Every tool that opens connections: (callable, kwargs needed to invoke it).
CONNECTION_TOOLS = [
    pytest.param(run_netmiko_command, {"command": "show version", "all_hosts": True}, id="run_netmiko_command"),
    pytest.param(run_http_request, {"method": "GET", "path": "/api", "all_hosts": True}, id="run_http_request"),
    pytest.param(run_serial_command, {"command": "show version", "all_hosts": True}, id="run_serial_command"),
    pytest.param(
        generate_config, {"template_string": "hostname {{ host.name }}", "all_hosts": True}, id="generate_config"
    ),
    pytest.param(run_netmiko_config, {"commands": ["hostname r1"], "all_hosts": True}, id="run_netmiko_config"),
]


class TestFormatAggResult:
    def test_returns_output_for_successful_result(self):
        agg_result = _build_agg_result(Result(host=None, result="Cisco IOS XE"))

        output = _format_agg_result(agg_result)

        assert output["results"] == {"router1": {"status": "success", "output": "Cisco IOS XE"}}

    def test_returns_exception_message_for_failed_result(self):
        agg_result = _build_agg_result(Result(host=None, failed=True, exception=ValueError("connection refused")))

        output = _format_agg_result(agg_result)

        assert output["results"]["router1"]["error"]["message"] == "connection refused"

    def test_falls_back_to_result_text_when_failed_without_exception(self):
        agg_result = _build_agg_result(Result(host=None, failed=True, result="Traceback: something went wrong"))

        output = _format_agg_result(agg_result)

        assert output["results"]["router1"]["error"]["message"] == "Traceback: something went wrong"

    def test_handles_empty_multi_result_without_index_error(self):
        agg_result = AggregatedResult("task")
        agg_result["router1"] = MultiResult("task")

        output = _format_agg_result(agg_result)

        assert output["results"]["router1"]["error"]["message"] == "No result returned for host."

    def test_serializes_non_json_values_via_default_str(self):
        agg_result = _build_agg_result(Result(host=None, result={"uptime": complex(1, 2)}))

        output = _format_agg_result(agg_result)

        assert output["results"]["router1"]["output"]["uptime"] == str(complex(1, 2))

    def test_reports_subtask_failure_even_when_parent_result_succeeded(self):
        agg_result = _build_agg_result(Result(host=None, result="parent output"))
        agg_result["router1"].append(Result(host=None, failed=True, exception=ValueError("subtask failed")))

        assert _format_agg_result(agg_result)["results"]["router1"]["error"]["message"] == "subtask failed"


class TestResolveSerialDeviceType:
    def test_prefers_explicit_device_type_in_serial_settings(self):
        device_type = _resolve_serial_device_type({"device_type": "cisco_ios_serial"}, platform="cisco_xr")

        assert device_type == "cisco_ios_serial"

    def test_appends_serial_suffix_to_platform(self):
        device_type = _resolve_serial_device_type({}, platform="cisco_ios")

        assert device_type == "cisco_ios_serial"

    def test_appends_serial_suffix_to_explicit_device_type(self):
        device_type = _resolve_serial_device_type({"device_type": "cisco_ios"}, platform=None)

        assert device_type == "cisco_ios_serial"

    def test_raises_when_device_type_cannot_be_determined(self):
        with pytest.raises(ValueError, match="serial device_type"):
            _resolve_serial_device_type({}, platform=None)


class TestHostHttpSettings:
    def test_tls_verify_defaults_to_enabled(self):
        assert _get_tls_verify(_fake_host()) is True

    def test_tls_verify_can_be_disabled(self):
        assert _get_tls_verify(_fake_host(tls_verify=False)) is False

    def test_http_timeout_defaults_to_module_constant(self):
        assert _get_http_timeout(_fake_host()) == server.HTTP_TIMEOUT

    def test_http_timeout_is_overridable_per_host(self):
        assert _get_http_timeout(_fake_host(http_timeout=45)) == 45.0


class TestCustomTemplateTask:
    def _build_task(self) -> SimpleNamespace:
        host = Host(
            name="router1",
            hostname="192.168.1.1",
            platform="cisco_ios",
            username="admin",
            password="supersecret",
            data={"role": "core"},
        )
        return SimpleNamespace(host=host)

    def test_renders_allowed_host_fields(self):
        task = self._build_task()
        template = _JINJA_ENV.from_string("hostname {{ host.name }} ! {{ host.hostname }} {{ host.data['role'] }}")

        result = custom_template_task(task, template)

        assert result.result == "hostname router1 ! 192.168.1.1 core"

    def test_does_not_expose_credentials(self):
        task = self._build_task()
        template = _JINJA_ENV.from_string("{{ host.password }}{{ host.username }}")

        result = custom_template_task(task, template)

        assert "supersecret" not in result.result
        assert "admin" not in result.result


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    def __init__(self, response):
        self._response = response
        self.requests = []

    def request(self, **kwargs):
        self.requests.append(kwargs)
        return self._response


class TestCustomHttpTask:
    def _run(self, data, response, method="GET", path="/api/v1/system"):
        host = _fake_host(**data)
        client = FakeClient(response)
        result = custom_http_task(SimpleNamespace(host=host), {True: client, False: client}, method, path)
        return result, client.requests[0]

    def test_defaults_base_url_to_https_hostname(self):
        _, request = self._run({}, FakeResponse(payload={"ok": True}))

        assert request["url"] == "https://192.168.1.1/api/v1/system"

    def test_joins_base_url_and_path_without_doubling_slashes(self):
        _, request = self._run({"base_url": "https://dev.example/"}, FakeResponse(payload={}))

        assert request["url"] == "https://dev.example/api/v1/system"

    def test_passes_per_host_timeout_and_headers(self):
        data = {"http_timeout": 30, "http_headers": {"X-Auth": "token"}}

        _, request = self._run(data, FakeResponse(payload={}))

        assert request["timeout"] == 30.0
        assert request["headers"] == {"X-Auth": "token"}

    def test_returns_parsed_json_body(self):
        result, _ = self._run({}, FakeResponse(payload={"version": "17.3"}))

        assert result.result == {"version": "17.3"}

    def test_falls_back_to_text_for_non_json_body(self):
        result, _ = self._run({}, FakeResponse(payload=None, text="plain body"))

        assert result.result == "plain body"


class FakeConnectHandler:
    """Records the params netmiko would receive, and doubles as its context manager."""

    instances = []

    def __init__(self, **params):
        self.params = params
        self.commands = []
        self.exited = False
        FakeConnectHandler.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.exited = True
        return False

    def send_command(self, command):
        self.commands.append(command)
        return f"output of {command}"


class TestCustomSerialTask:
    @pytest.fixture
    def connect_handler(self, monkeypatch):
        FakeConnectHandler.instances = []
        monkeypatch.setattr(server, "ConnectHandler", FakeConnectHandler)
        return FakeConnectHandler

    def _build_task(self, **overrides) -> SimpleNamespace:
        host = Host(
            name="router1",
            hostname="192.168.1.1",
            platform=overrides.pop("platform", "cisco_ios"),
            username="serial-user",
            password="serial-pass",
            data=overrides.pop("data", {"serial_settings": {"port": "/dev/ttyUSB0", "baudrate": 9600}}),
        )
        return SimpleNamespace(host=host)

    def test_uses_top_level_credentials_not_connection_options(self, connect_handler):
        # The asymmetry groups.yaml.example now documents: serial reads host.username
        # / host.password, never connection_options.netmiko.extras.
        custom_serial_task(self._build_task(), "show version")

        params = connect_handler.instances[0].params
        assert params["username"] == "serial-user"
        assert params["password"] == "serial-pass"

    def test_derives_serial_device_type_from_platform(self, connect_handler):
        custom_serial_task(self._build_task(), "show version")

        assert connect_handler.instances[0].params["device_type"] == "cisco_ios_serial"

    def test_strips_device_type_from_pyserial_settings(self, connect_handler):
        task = self._build_task(data={"serial_settings": {"port": "/dev/ttyUSB0", "device_type": "cisco_ios_serial"}})

        custom_serial_task(task, "show version")

        assert connect_handler.instances[0].params["serial_settings"] == {"port": "/dev/ttyUSB0"}

    def test_returns_command_output_and_closes_connection(self, connect_handler):
        result = custom_serial_task(self._build_task(), "show version")

        assert result.result == "output of show version"
        assert connect_handler.instances[0].exited is True

    def test_raises_when_serial_settings_missing(self, connect_handler):
        with pytest.raises(ValueError, match="serial_settings"):
            custom_serial_task(self._build_task(data={}), "show version")

        assert connect_handler.instances == []


class TestNoHostsShortCircuit:
    @pytest.mark.parametrize("tool,kwargs", CONNECTION_TOOLS)
    def test_returns_no_match_message_without_running(self, patched_nornir, tool, kwargs):
        nr = patched_nornir(FakeNornir(hosts={}))

        assert tool(**kwargs)["status"] == "no_hosts"
        assert nr.run_calls == []
        assert nr.closed is True

    def test_get_inventory_returns_empty_mapping(self, patched_nornir):
        patched_nornir(FakeNornir(hosts={}))

        assert json.loads(get_inventory()) == {}


class TestConnectionCleanup:
    """CLAUDE.md treats close_connections() as mandatory: a leak holds device VTYs open."""

    @pytest.mark.parametrize("tool,kwargs", CONNECTION_TOOLS)
    def test_closes_connections_on_success(self, patched_nornir, tool, kwargs):
        agg_result = _build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(FakeNornir(hosts={"router1": _fake_host()}, agg_result=agg_result))

        assert tool(**kwargs, include_output=True)["results"] == {"router1": {"status": "success", "output": "ok"}}
        assert nr.closed is True
        assert nr.closed_failed_hosts is True

    @pytest.mark.parametrize("tool,kwargs", CONNECTION_TOOLS)
    def test_closes_connections_and_reports_error_when_run_raises(self, patched_nornir, tool, kwargs):
        nr = patched_nornir(FakeNornir(hosts={"router1": _fake_host()}, run_error=RuntimeError("boom")))

        output = tool(**kwargs)

        assert nr.closed is True
        assert output["error"]["message"] == "boom"
        assert output["status"] == "failed"


class TestToolRunnerArguments:
    def test_serial_command_forces_single_worker(self, patched_nornir):
        agg_result = _build_agg_result(Result(host=None, result="ok"))
        patched_nornir(FakeNornir(hosts={"router1": _fake_host()}, agg_result=agg_result))

        run_serial_command("show version", filter_criteria={"site": "tokyo"})

        assert patched_nornir.calls[-1] == {
            "filter_criteria": {"site": "tokyo"},
            "num_workers": 1,
        }

    @pytest.mark.parametrize(
        "tool,kwargs",
        [param for param in CONNECTION_TOOLS if param.id != "run_serial_command"],
    )
    def test_other_tools_leave_worker_count_to_config(self, patched_nornir, tool, kwargs):
        agg_result = _build_agg_result(Result(host=None, result="ok"))
        patched_nornir(FakeNornir(hosts={"router1": _fake_host()}, agg_result=agg_result))

        tool(**kwargs)

        assert patched_nornir.calls[-1]["num_workers"] is None

    def test_generate_config_compiles_template_once_for_all_hosts(self, patched_nornir, monkeypatch):
        compiled = []
        original_from_string = _JINJA_ENV.from_string

        def counting_from_string(source, *args, **kwargs):
            compiled.append(source)
            return original_from_string(source, *args, **kwargs)

        monkeypatch.setattr(_JINJA_ENV, "from_string", counting_from_string)
        agg_result = _build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(FakeNornir(hosts={"r1": _fake_host(), "r2": _fake_host()}, agg_result=agg_result))

        generate_config("hostname {{ host.name }}", all_hosts=True)

        # Compiled exactly once for a two-host run, and handed to the task already
        # compiled, so the task cannot re-parse it per host.
        assert compiled == ["hostname {{ host.name }}"]
        _, run_kwargs = nr.run_calls[0]
        assert "template_string" not in run_kwargs
        assert hasattr(run_kwargs["template"], "render")

    def test_generate_config_reports_bad_template_once_without_running(self, patched_nornir):
        """A malformed template fails at compile time, before any host task is dispatched."""
        nr = patched_nornir(FakeNornir(hosts={"r1": _fake_host(), "r2": _fake_host()}))

        output = generate_config("hostname {{ unclosed", all_hosts=True)

        assert output["status"] == "failed"
        assert nr.run_calls == []
        assert nr.closed is True


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
            "secure": _fake_host(),
            "also_secure": _fake_host(tls_verify=True),
            "insecure": _fake_host(tls_verify=False),
        }
        agg_result = _build_agg_result(Result(host=None, result="ok"))
        patched_nornir(FakeNornir(hosts=hosts, agg_result=agg_result))

        run_http_request("GET", "/api", all_hosts=True)

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
        nr = patched_nornir(FakeNornir(hosts={"r1": _fake_host(), "r2": _fake_host(tls_verify=False)}))

        output = run_http_request("GET", "/api", all_hosts=True)

        assert output["error"]["message"] == "client initialization failed"
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
                hosts={"r1": _fake_host(), "r2": _fake_host(tls_verify=False)},
                agg_result=_build_agg_result(Result(host=None, result="ok")),
            )
        )

        output = run_http_request("GET", "/api", all_hosts=True)

        assert output["error"]["message"] == "client close failed"
        assert all(client.closed for client in created)
        assert nr.closed is True


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
        patched_nornir(
            FakeNornir(hosts={host.name: host}, agg_result=_build_agg_result(Result(host=None, result="ok")))
        )

        run_http_request("GET", "/api", all_hosts=True)

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
    patched_nornir(FakeNornir(hosts={"router1": _fake_host(commissioned=date(2026, 1, 1))}))

    assert json.loads(get_inventory())["router1"]["data"]["commissioned"] == "2026-01-01"


def test_failed_nornir_hosts_have_their_connections_closed(monkeypatch):
    """Use the real Nornir runner: the fake cannot detect its failed-host exclusion."""
    hosts = {name: Host(name=name) for name in ("healthy", "failed")}
    closed_hosts = []

    class Connection:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed_hosts.append(self.name)

    def command_task(task, **kwargs):
        task.host.connections["netmiko"] = Connection(task.host.name)
        if task.host.name == "failed":
            raise RuntimeError("command failed after connecting")
        return Result(host=task.host, result="ok")

    nr = Nornir(inventory=Inventory(hosts=hosts), runner=SerialRunner())
    monkeypatch.setattr(server, "_get_nornir", lambda *args, **kwargs: nr)
    monkeypatch.setattr(server, "netmiko_send_command", command_task)

    output = run_netmiko_command("show version", all_hosts=True, include_output=True)["results"]

    assert output["healthy"] == {"status": "success", "output": "ok"}
    assert output["failed"]["error"]["message"] == "command failed after connecting"
    assert sorted(closed_hosts) == ["failed", "healthy"]
    assert all(not host.connections for host in hosts.values())
