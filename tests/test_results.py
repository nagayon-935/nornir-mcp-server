import json

from nornir.core.task import AggregatedResult, MultiResult, Result

from server import _format_agg_result
from tests.support import build_agg_result


class TestFormatAggResult:
    def test_returns_output_for_successful_result(self):
        agg_result = build_agg_result(Result(host=None, result="Cisco IOS XE"))

        output = json.loads(_format_agg_result(agg_result))

        assert output == {"router1": {"output": "Cisco IOS XE"}}

    def test_returns_exception_message_for_failed_result(self):
        agg_result = build_agg_result(Result(host=None, failed=True, exception=ValueError("connection refused")))

        output = json.loads(_format_agg_result(agg_result))

        assert output == {"router1": {"error": "connection refused"}}

    def test_falls_back_to_result_text_when_failed_without_exception(self):
        agg_result = build_agg_result(Result(host=None, failed=True, result="Traceback: something went wrong"))

        output = json.loads(_format_agg_result(agg_result))

        assert output == {"router1": {"error": "Traceback: something went wrong"}}

    def test_handles_empty_multi_result_without_index_error(self):
        agg_result = AggregatedResult("task")
        agg_result["router1"] = MultiResult("task")

        output = json.loads(_format_agg_result(agg_result))

        assert output == {"router1": {"error": "No result returned for host."}}

    def test_serializes_non_json_values_via_default_str(self):
        agg_result = build_agg_result(Result(host=None, result={"uptime": complex(1, 2)}))

        output = json.loads(_format_agg_result(agg_result))

        assert output["router1"]["output"]["uptime"] == str(complex(1, 2))

    def test_reports_subtask_failure_even_when_parent_result_succeeded(self):
        agg_result = build_agg_result(Result(host=None, result="parent output"))
        agg_result["router1"].append(Result(host=None, failed=True, exception=ValueError("subtask failed")))

        assert json.loads(_format_agg_result(agg_result)) == {"router1": {"error": "subtask failed"}}
