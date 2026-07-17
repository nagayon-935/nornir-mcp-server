import json
from types import SimpleNamespace

import pytest
from nornir.core.inventory import Host
from nornir.core.task import AggregatedResult, MultiResult, Result

from server import (
    _format_agg_result,
    _resolve_serial_device_type,
    custom_template_task,
)


def _build_agg_result(result: Result) -> AggregatedResult:
    multi_result = MultiResult("task")
    multi_result.append(result)
    agg_result = AggregatedResult("task")
    agg_result["router1"] = multi_result
    return agg_result


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
        template = "hostname {{ host.name }} ! {{ host.hostname }} {{ host.data['role'] }}"

        result = custom_template_task(task, template)

        assert result.result == "hostname router1 ! 192.168.1.1 core"

    def test_does_not_expose_credentials(self):
        task = self._build_task()

        result = custom_template_task(task, "{{ host.password }}{{ host.username }}")

        assert "supersecret" not in result.result
        assert "admin" not in result.result
