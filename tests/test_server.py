import json
from types import SimpleNamespace

import pytest
from nornir.core.inventory import Host
from nornir.core.task import AggregatedResult, MultiResult, Result

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


def _fake_host(**data) -> SimpleNamespace:
    """Minimal host stub: run_http_request reads host.data before dispatching."""
    return SimpleNamespace(name="router1", hostname="192.168.1.1", data=data)


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
        self.run_calls = []

    def run(self, task, **kwargs):
        self.run_calls.append((task, kwargs))
        if self.run_error is not None:
            raise self.run_error
        return self.agg_result

    def close_connections(self):
        self.closed = True


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
    pytest.param(run_netmiko_command, {"command": "show version"}, id="run_netmiko_command"),
    pytest.param(run_http_request, {"method": "GET", "path": "/api"}, id="run_http_request"),
    pytest.param(run_serial_command, {"command": "show version"}, id="run_serial_command"),
    pytest.param(
        generate_config, {"template_string": "hostname {{ host.name }}"}, id="generate_config"
    ),
    pytest.param(run_netmiko_config, {"commands": ["hostname r1"]}, id="run_netmiko_config"),
]


class TestFormatAggResult:
    def test_returns_output_for_successful_result(self):
        agg_result = _build_agg_result(Result(host=None, result="Cisco IOS XE"))

        output = json.loads(_format_agg_result(agg_result))

        assert output == {"router1": {"output": "Cisco IOS XE"}}

    def test_returns_exception_message_for_failed_result(self):
        agg_result = _build_agg_result(
            Result(host=None, failed=True, exception=ValueError("connection refused"))
        )

        output = json.loads(_format_agg_result(agg_result))

        assert output == {"router1": {"error": "connection refused"}}

    def test_falls_back_to_result_text_when_failed_without_exception(self):
        agg_result = _build_agg_result(
            Result(host=None, failed=True, result="Traceback: something went wrong")
        )

        output = json.loads(_format_agg_result(agg_result))

        assert output == {"router1": {"error": "Traceback: something went wrong"}}

    def test_handles_empty_multi_result_without_index_error(self):
        agg_result = AggregatedResult("task")
        agg_result["router1"] = MultiResult("task")

        output = json.loads(_format_agg_result(agg_result))

        assert output == {"router1": {"error": "No result returned for host."}}

    def test_serializes_non_json_values_via_default_str(self):
        agg_result = _build_agg_result(Result(host=None, result={"uptime": complex(1, 2)}))

        output = json.loads(_format_agg_result(agg_result))

        assert output["router1"]["output"]["uptime"] == str(complex(1, 2))


class TestResolveSerialDeviceType:
    def test_prefers_explicit_device_type_in_serial_settings(self):
        device_type = _resolve_serial_device_type(
            {"device_type": "cisco_ios_serial"}, platform="cisco_xr"
        )

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
        assert _get_tls_verify(SimpleNamespace(data={})) is True

    def test_tls_verify_can_be_disabled(self):
        assert _get_tls_verify(SimpleNamespace(data={"tls_verify": False})) is False

    def test_http_timeout_defaults_to_module_constant(self):
        assert _get_http_timeout(SimpleNamespace(data={})) == server.HTTP_TIMEOUT

    def test_http_timeout_is_overridable_per_host(self):
        assert _get_http_timeout(SimpleNamespace(data={"http_timeout": 45})) == 45.0


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
        template = _JINJA_ENV.from_string(
            "hostname {{ host.name }} ! {{ host.hostname }} {{ host.data['role'] }}"
        )

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
        host = SimpleNamespace(name="router1", hostname="192.168.1.1", data=data)
        client = FakeClient(response)
        result = custom_http_task(
            SimpleNamespace(host=host), {True: client, False: client}, method, path
        )
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
        task = self._build_task(
            data={"serial_settings": {"port": "/dev/ttyUSB0", "device_type": "cisco_ios_serial"}}
        )

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

        assert tool(**kwargs) == NO_MATCH_MESSAGE
        assert nr.run_calls == []

    def test_get_inventory_returns_empty_mapping(self, patched_nornir):
        patched_nornir(FakeNornir(hosts={}))

        assert json.loads(get_inventory()) == {}


class TestConnectionCleanup:
    """CLAUDE.md treats close_connections() as mandatory: a leak holds device VTYs open."""

    @pytest.mark.parametrize("tool,kwargs", CONNECTION_TOOLS)
    def test_closes_connections_on_success(self, patched_nornir, tool, kwargs):
        agg_result = _build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(FakeNornir(hosts={"router1": _fake_host()}, agg_result=agg_result))

        assert json.loads(tool(**kwargs)) == {"router1": {"output": "ok"}}
        assert nr.closed is True

    @pytest.mark.parametrize("tool,kwargs", CONNECTION_TOOLS)
    def test_closes_connections_and_reports_error_when_run_raises(
        self, patched_nornir, tool, kwargs
    ):
        nr = patched_nornir(
            FakeNornir(hosts={"router1": _fake_host()}, run_error=RuntimeError("boom"))
        )

        output = tool(**kwargs)

        assert nr.closed is True
        assert "boom" in output
        assert output.startswith("Error ")


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

    def test_generate_config_compiles_template_once_for_all_hosts(
        self, patched_nornir, monkeypatch
    ):
        compiled = []
        original_from_string = _JINJA_ENV.from_string

        def counting_from_string(source, *args, **kwargs):
            compiled.append(source)
            return original_from_string(source, *args, **kwargs)

        monkeypatch.setattr(_JINJA_ENV, "from_string", counting_from_string)
        agg_result = _build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(
            FakeNornir(hosts={"r1": _fake_host(), "r2": _fake_host()}, agg_result=agg_result)
        )

        generate_config("hostname {{ host.name }}")

        # Compiled exactly once for a two-host run, and handed to the task already
        # compiled, so the task cannot re-parse it per host.
        assert compiled == ["hostname {{ host.name }}"]
        _, run_kwargs = nr.run_calls[0]
        assert "template_string" not in run_kwargs
        assert hasattr(run_kwargs["template"], "render")

    def test_generate_config_reports_bad_template_once_without_running(self, patched_nornir):
        """A malformed template fails at compile time, before any host task is dispatched."""
        nr = patched_nornir(FakeNornir(hosts={"r1": _fake_host(), "r2": _fake_host()}))

        output = generate_config("hostname {{ unclosed")

        assert output.startswith("Error generating config:")
        assert nr.run_calls == []


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

        monkeypatch.setattr(
            server.httpx, "Client", lambda verify, timeout: RecordingClient(verify, timeout)
        )
        hosts = {
            "secure": SimpleNamespace(data={}),
            "also_secure": SimpleNamespace(data={"tls_verify": True}),
            "insecure": SimpleNamespace(data={"tls_verify": False}),
        }
        agg_result = _build_agg_result(Result(host=None, result="ok"))
        patched_nornir(FakeNornir(hosts=hosts, agg_result=agg_result))

        run_http_request("GET", "/api")

        assert sorted(client.verify for client in created) == [False, True]
        assert all(client.closed for client in created)
