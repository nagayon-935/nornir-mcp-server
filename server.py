import json
import logging
import os
from collections import Counter, OrderedDict
from contextlib import ExitStack
from importlib.metadata import entry_points
from math import isfinite
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Literal, NotRequired, TypedDict
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from jinja2 import Template
from jinja2.sandbox import SandboxedEnvironment
from mcp.server.fastmcp import FastMCP
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from netmiko.ssh_dispatcher import CLASS_MAPPER
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.configuration import Config
from nornir.core.filter import F
from nornir.core.inventory import Host
from nornir.core.task import AggregatedResult, Result, Task
from nornir.plugins.inventory.simple import SimpleInventory
from nornir_netmiko.connections.netmiko import napalm_to_netmiko_map
from nornir_netmiko.tasks import netmiko_send_command, netmiko_send_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nornir-mcp-server")

CONFIG_FILE = Path(os.environ.get("NORNIR_MCP_CONFIG", str(Path(__file__).parent / "config.yaml")))
# Default per-request HTTP timeout; override per host with 'http_timeout' in host.data.
HTTP_TIMEOUT = 10.0
SERIAL_DEVICE_TYPE_SUFFIX = "_serial"

# Mirrors the direct imports above (and pyproject.toml): `mcp install` builds its
# isolated environment from this list, so a missing entry breaks that path at import.
mcp = FastMCP(
    "nornir server",
    dependencies=["httpx", "jinja2", "netmiko", "nornir", "nornir-netbox", "nornir-netmiko"],
)

# Jinja2 environments are thread-safe and shareable across renders.
_JINJA_ENV = SandboxedEnvironment()


class ErrorDetail(TypedDict):
    code: str
    message: str
    hint: str


class HostResult(TypedDict):
    status: Literal["success", "failed"]
    output: NotRequired[Any]
    error: NotRequired[ErrorDetail]


class ExecutionSummary(TypedDict):
    total: int
    succeeded: int
    failed: int
    duration_seconds: float


class ExecutionReport(TypedDict):
    status: Literal["success", "partial_failure", "failed", "no_hosts"]
    execution_id: str | None
    summary: ExecutionSummary
    results: dict[str, HostResult]
    error: NotRequired[ErrorDetail]
    next_offset: NotRequired[int | None]
    details_available: NotRequired[bool]
    message: NotRequired[str]


_RESULT_TTL = 900.0
_MAX_RESULTS = 100
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_RESULT_CACHE: OrderedDict[str, tuple[float, int, ExecutionReport]] = OrderedDict()
_RESULT_LOCK = Lock()


def _purge_results(now: float) -> None:
    """Caller holds _RESULT_LOCK; expiry follows creation, independent of LRU order."""
    for execution_id, (created, _, _) in list(_RESULT_CACHE.items()):
        if now - created >= _RESULT_TTL:
            del _RESULT_CACHE[execution_id]


def _error_detail(error: Exception | None, message: str) -> ErrorDetail:
    code, hint = "execution_error", "Check the operation and server configuration."
    if isinstance(error, NetmikoAuthenticationException):
        code, hint = "authentication_failed", "Check the device credentials and account permissions."
    elif isinstance(error, (NetmikoTimeoutException, httpx.TimeoutException)):
        code, hint = "timeout", "Check device reachability and the configured timeout."
    elif isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code in (401, 403):
            code, hint = "authentication_failed", "Check the HTTP credentials and endpoint permissions."
        else:
            code, hint = "http_error", "Check the HTTP method, path and payload."
    elif isinstance(error, httpx.ConnectError):
        code, hint = "connection_failed", "Check the address, network connectivity and TLS settings."
    elif message.startswith("Target selection required:"):
        code, hint = (
            "target_selection_required",
            "Preview targets and provide a filter, or explicitly set all_hosts=True.",
        )
    return {"code": code, "message": message, "hint": hint}


def _empty_report(status: Literal["failed", "no_hosts"], started: float | None = None) -> ExecutionReport:
    return {
        "status": status,
        "execution_id": None,
        "summary": {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "duration_seconds": round(monotonic() - started, 3) if started is not None else 0.0,
        },
        "results": {},
    }


def _tool_error(
    operation: str, error: Exception, started: float, completed: ExecutionReport | None = None
) -> ExecutionReport:
    logger.exception("Error %s", operation)
    if completed is not None:
        report = {**completed, "status": "partial_failure" if completed["summary"]["succeeded"] else "failed"}
        report["error"] = _error_detail(error, str(error))
        report["error"]["code"] = "cleanup_failed"
        report["message"] = "Tasks completed but cleanup failed. Inspect their results before repeating any operation."
        with _RESULT_LOCK:
            entry = _RESULT_CACHE.get(report["execution_id"])
            if entry is not None:
                full_report = {
                    **entry[2],
                    "status": report["status"],
                    "error": report["error"],
                    "message": report["message"],
                }
                _RESULT_CACHE[report["execution_id"]] = (entry[0], entry[1], full_report)
        return report
    report = _empty_report("failed", started)
    report["error"] = _error_detail(error, str(error))
    return report


def _inventory_paths(config: Config) -> dict[str, Path]:
    """Normalize only the file options of known built-in inventory backends."""
    fields = (
        ("host_file", "group_file", "defaults_file")
        if config.inventory.plugin == "SimpleInventory"
        else (("group_file", "defaults_file") if config.inventory.plugin == "NetBoxInventory2" else ())
    )
    config_directory = CONFIG_FILE.expanduser().resolve().parent
    default_files = {"host_file": "hosts.yaml", "group_file": "groups.yaml", "defaults_file": "defaults.yaml"}
    paths = {}
    for field in fields:
        path = Path(config.inventory.options.get(field, default_files[field])).expanduser()
        paths[field] = path if path.is_absolute() else config_directory / path
    return paths


def _load_config() -> Config:
    config = Config.from_file(str(CONFIG_FILE.expanduser().resolve()))
    config.inventory.options = {
        **config.inventory.options,
        **{field: str(path) for field, path in _inventory_paths(config).items()},
    }
    return config


def _get_nornir(filter_criteria: dict[str, Any] | None = None, num_workers: int | None = None) -> Nornir:
    """Initialize Nornir with inventory paths relative to the configuration file."""
    kwargs = _load_config().dict()
    if num_workers is not None:
        kwargs["runner"] = {"plugin": "threaded", "options": {"num_workers": num_workers}}
    nr = InitNornir(**kwargs)
    if filter_criteria:
        # F(**filter_criteria) allows matching by attributes like site, role, name, etc.
        nr = nr.filter(F(**filter_criteria))
    return nr


class DiagnosticCheck(TypedDict):
    status: Literal["ok", "warning", "error"]
    code: str
    message: str
    hint: str
    host: NotRequired[str]
    field: NotRequired[str]


class DiagnosticReport(TypedDict):
    status: Literal["success", "warning", "failed"]
    config_path: str
    inventory_plugin: str | None
    inventory_files: dict[str, str]
    network_accessed: bool
    hosts_checked: int | None
    checks: list[DiagnosticCheck]


def _diagnose_host(host: Host) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []

    def add(status: Literal["warning", "error"], code: str, field: str, message: str, hint: str) -> None:
        checks.append(
            {"status": status, "code": code, "host": host.name, "field": field, "message": message, "hint": hint}
        )

    params = host.get_connection_parameters("netmiko")
    extras = params.extras or {}
    device_type = extras.get("device_type") or napalm_to_netmiko_map.get(params.platform, params.platform)
    if device_type and device_type not in CLASS_MAPPER:
        add(
            "error",
            "unsupported_platform",
            "platform",
            "No Netmiko driver matches this host's SSH platform.",
            "Use a supported Netmiko device_type in platform or connection_options.netmiko.extras.",
        )
    elif device_type:
        username = extras.get("username", params.username)
        password = extras.get("password", params.password)
        if not username or (not password and not extras.get("use_keys") and not extras.get("allow_agent")):
            add(
                "warning",
                "ssh_credentials_missing",
                "credentials",
                "SSH credentials are incomplete.",
                "Configure login credentials or an SSH key/agent. Connectivity is not tested here.",
            )
        for field, value in {"username": username, "password": password, "secret": extras.get("secret")}.items():
            if value == "CHANGE_ME":
                add(
                    "error",
                    "placeholder_credentials",
                    field,
                    "A sample credential placeholder is still configured.",
                    "Replace the placeholder in the local credential configuration.",
                )
    elif not host.get("serial_settings") and not host.get("base_url"):
        add(
            "warning",
            "platform_missing",
            "platform",
            "No SSH platform or alternative connection is configured.",
            "Set platform for SSH, base_url for HTTP, or serial_settings for serial access.",
        )

    if not host.hostname and not host.get("base_url") and not host.get("serial_settings"):
        add("error", "address_missing", "hostname", "No connection address is configured.", "Set hostname or base_url.")
    if not isinstance(host.get("tls_verify", True), bool):
        add("error", "invalid_tls_setting", "tls_verify", "tls_verify must be a YAML boolean.", "Use true or false.")
    try:
        timeout = _get_http_timeout(host)
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (ValueError, TypeError):
        add(
            "error",
            "invalid_http_timeout",
            "http_timeout",
            "HTTP timeout must be a positive finite number.",
            "Set http_timeout in seconds, for example 10.",
        )
    base_url = host.get("base_url")
    if base_url is not None:
        try:
            parsed = urlsplit(base_url)
            valid_url = parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username
        except (ValueError, TypeError, AttributeError):
            valid_url = False
        if not valid_url:
            add(
                "error",
                "invalid_base_url",
                "base_url",
                "HTTP base URL is invalid or contains embedded credentials.",
                "Use an http(s) URL without embedded credentials.",
            )
    headers = host.get("http_headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
    ):
        add(
            "error",
            "invalid_http_headers",
            "http_headers",
            "HTTP headers must map string names to string values.",
            "Correct the header mapping; values are not included in this report.",
        )
    elif any("CHANGE_ME" in value for value in headers.values()):
        add(
            "error",
            "placeholder_credentials",
            "http_headers",
            "An HTTP credential placeholder is still configured.",
            "Replace the placeholder in the local credential configuration.",
        )

    settings = host.get("serial_settings")
    if settings is not None:
        if not isinstance(settings, dict) or not isinstance(settings.get("port"), str) or not settings["port"]:
            add(
                "error",
                "invalid_serial_settings",
                "serial_settings",
                "Serial settings require a non-empty port.",
                "Set serial_settings.port to the local serial device.",
            )
        else:
            try:
                serial_type = _resolve_serial_device_type(settings, host.platform)
                if serial_type not in CLASS_MAPPER:
                    raise ValueError
            except (ValueError, AttributeError):
                add(
                    "error",
                    "unsupported_serial_platform",
                    "serial_settings.device_type",
                    "No serial driver matches this host.",
                    "Set a supported Netmiko serial device_type explicitly.",
                )
            for field, value in {"username": host.username, "password": host.password}.items():
                if value == "CHANGE_ME":
                    add(
                        "error",
                        "placeholder_credentials",
                        field,
                        "A serial credential placeholder is still configured.",
                        "Set top-level serial credentials; SSH extras are not used by serial connections.",
                    )
            if not host.username or not host.password:
                add(
                    "warning",
                    "serial_credentials_missing",
                    "credentials",
                    "Top-level serial credentials are incomplete.",
                    "Set top-level username/password if this console requires authentication.",
                )
    return checks


@mcp.tool()
def diagnose_setup() -> DiagnosticReport:
    """Check local setup without contacting devices, NetBox or other remote inventories.

    Reports config/inventory paths, malformed YAML, missing credentials/placeholders,
    unsupported drivers and invalid HTTP/serial settings. Credential values and YAML
    parser excerpts are never included. Remote inventory validation is explicitly skipped.
    """
    report: DiagnosticReport = {
        "status": "success",
        "config_path": str(CONFIG_FILE.expanduser().resolve()),
        "inventory_plugin": None,
        "inventory_files": {},
        "network_accessed": False,
        "hosts_checked": None,
        "checks": [],
    }
    checks = report["checks"]

    def add(status: Literal["ok", "warning", "error"], code: str, message: str, hint: str = "") -> None:
        checks.append({"status": status, "code": code, "message": message, "hint": hint})

    try:
        config = _load_config()
    except FileNotFoundError:
        add(
            "error",
            "config_missing",
            "Configuration file does not exist.",
            "Set NORNIR_MCP_CONFIG to an existing config file.",
        )
    except Exception:
        add(
            "error",
            "config_invalid",
            "Configuration could not be parsed or contains invalid options.",
            "Check YAML syntax and Nornir option types locally. Parser excerpts are omitted to protect credentials.",
        )
    else:
        report["inventory_plugin"] = config.inventory.plugin
        paths = _inventory_paths(config)
        report["inventory_files"] = {field: str(path) for field, path in paths.items()}
        add("ok", "config_loaded", "Configuration loaded successfully.")
        if config.logging.enabled and config.logging.to_console:
            add(
                "warning",
                "stdio_logging",
                "Nornir console logging can interfere with MCP stdio transport.",
                "Set logging.enabled: false or logging.to_console: false.",
            )
        if config.runner.plugin == "threaded":
            workers = config.runner.options.get("num_workers", 20)
            if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
                add(
                    "error",
                    "invalid_worker_count",
                    "Threaded runner worker count must be a positive integer.",
                    "Set runner.options.num_workers to a positive integer.",
                )
        if config.runner.plugin not in {entry.name for entry in entry_points(group="nornir.plugins.runners")}:
            add(
                "error",
                "runner_plugin_missing",
                "The configured runner plugin is not installed.",
                "Select threaded or serial, or install the configured runner plugin.",
            )
        if config.inventory.transform_function:
            add(
                "warning",
                "inventory_transform_skipped",
                "Inventory transforms are skipped during offline diagnosis.",
                "Credentials or metadata added by a transform cannot be validated by this offline check.",
            )
        inventory_plugins = {entry.name for entry in entry_points(group="nornir.plugins.inventory")}
        if config.inventory.plugin not in inventory_plugins:
            add(
                "error",
                "inventory_plugin_missing",
                "The configured inventory plugin is not installed.",
                "Install the plugin or select SimpleInventory.",
            )
        elif config.inventory.plugin == "SimpleInventory":
            for field, path in paths.items():
                if not path.is_file():
                    add(
                        "error" if field == "host_file" else "warning",
                        "inventory_file_missing",
                        f"The {field} inventory file does not exist.",
                        "Copy and edit the example inventory, or correct the configured path.",
                    )
            if paths["host_file"].is_file():
                try:
                    inventory = SimpleInventory(**config.inventory.options).load()
                except Exception:
                    add(
                        "error",
                        "inventory_invalid",
                        "Inventory YAML or group references are invalid.",
                        "Check hosts/groups/defaults locally. Parser excerpts are omitted to protect credentials.",
                    )
                else:
                    report["hosts_checked"] = len(inventory.hosts)
                    if not inventory.hosts:
                        add("warning", "inventory_empty", "No hosts are registered.", "Add hosts to the inventory.")
                    for host in inventory.hosts.values():
                        try:
                            checks.extend(_diagnose_host(host))
                        except Exception:
                            checks.append(
                                {
                                    "status": "error",
                                    "code": "host_invalid",
                                    "host": host.name,
                                    "message": "Host settings have invalid types.",
                                    "hint": "Check this host's local configuration.",
                                }
                            )
        else:
            add(
                "warning",
                "remote_inventory_skipped",
                "Remote inventory was not loaded; device settings were not checked.",
                "Use get_inventory_summary to load the configured remote inventory separately.",
            )
            if config.inventory.plugin == "NetBoxInventory2":
                if config.inventory.options.get("nb_token"):
                    add(
                        "warning",
                        "netbox_token_in_config",
                        "NetBox token is stored in the configuration file.",
                        "Prefer supplying NB_TOKEN through the server environment or a secret injection mechanism.",
                    )
                if not (config.inventory.options.get("nb_token") or os.environ.get("NB_TOKEN")):
                    add(
                        "error",
                        "netbox_token_missing",
                        "No NetBox API token is configured.",
                        "Supply NB_TOKEN to the server process.",
                    )
                if not (config.inventory.options.get("nb_url") or os.environ.get("NB_URL")):
                    add(
                        "warning",
                        "netbox_url_default",
                        "NetBox URL is unset and would use the plugin default.",
                        "Configure nb_url or supply NB_URL to the server process.",
                    )
    report["status"] = (
        "failed"
        if any(c["status"] == "error" for c in checks)
        else ("warning" if any(c["status"] == "warning" for c in checks) else "success")
    )
    return report


def _format_agg_result(
    agg_result: AggregatedResult, include_output: bool = True, started: float | None = None
) -> ExecutionReport:
    """Summarize a run and cache its full output for later, paginated retrieval."""
    if not agg_result:
        return _empty_report("no_hosts", started)
    output_data: dict[str, HostResult] = {}
    for host_name, multi_result in agg_result.items():
        if len(multi_result) == 0:
            output_data[host_name] = {"status": "failed", "error": _error_detail(None, "No result returned for host.")}
            continue
        failed_result = next((result for result in multi_result if result.failed), None)
        if failed_result is not None:
            result = failed_result
            detail = str(result.exception) if result.exception else str(result.result)
            output_data[host_name] = {"status": "failed", "error": _error_detail(result.exception, detail)}
        else:
            output_data[host_name] = {"status": "success", "output": multi_result[0].result}
    failed = sum(result["status"] == "failed" for result in output_data.values())
    total = len(output_data)
    report: ExecutionReport = {
        "status": "success" if not failed else "failed" if failed == total else "partial_failure",
        "execution_id": uuid4().hex,
        "summary": {
            "total": total,
            "succeeded": total - failed,
            "failed": failed,
            "duration_seconds": round(monotonic() - started, 3) if started is not None else 0.0,
        },
        "results": output_data,
        "details_available": True,
    }
    # Normalize non-JSON values once so MCP output and cached details agree.
    serialized = json.dumps(report, default=str)
    report = json.loads(serialized)
    size = len(serialized.encode())
    with _RESULT_LOCK:
        _purge_results(monotonic())
        if size <= _MAX_RESULT_BYTES:
            while _RESULT_CACHE and (
                len(_RESULT_CACHE) >= _MAX_RESULTS
                or sum(entry[1] for entry in _RESULT_CACHE.values()) + size > _MAX_RESULT_BYTES
            ):
                _RESULT_CACHE.popitem(last=False)
            _RESULT_CACHE[report["execution_id"]] = (monotonic(), size, report)
        else:
            report["execution_id"] = None
            report["details_available"] = False
            report["message"] = "Output exceeds the result-cache limit; full output is included in this response."
            include_output = True
    if include_output:
        return json.loads(json.dumps(report))
    return {
        **report,
        "results": {
            name: {k: v for k, v in result.items() if k != "output"} for name, result in report["results"].items()
        },
    }


@mcp.tool()
def get_execution_details(
    execution_id: str, host_names: list[str] | None = None, offset: int = 0, limit: int = 20
) -> ExecutionReport:
    """Retrieve stored output without rerunning an operation or connecting to devices.

    Results expire after 15 minutes and may be evicted earlier from the bounded in-memory
    cache. A server restart clears them. Select exact host_names or paginate by sorted host
    name (limit 1–200). summary/status always describe the full original execution.
    """
    if offset < 0 or not 1 <= limit <= 200:
        report = _empty_report("failed")
        report["error"] = _error_detail(None, "offset must be non-negative and limit must be between 1 and 200.")
        report["error"]["code"] = "invalid_arguments"
        return report
    with _RESULT_LOCK:
        _purge_results(monotonic())
        entry = _RESULT_CACHE.get(execution_id)
        if entry is None:
            report = _empty_report("failed")
            report["error"] = {
                "code": "result_not_found",
                "message": "Execution ID is unknown, expired or evicted.",
                "hint": "Results are temporary. Do not repeat configuration changes just to retrieve their output.",
            }
            return report
        _RESULT_CACHE.move_to_end(execution_id)
        cached = entry[2]
        names = sorted(cached["results"] if host_names is None else set(host_names))
        if any(name not in cached["results"] for name in names):
            report = _empty_report("failed")
            report["error"] = _error_detail(None, "A requested host is not part of this execution.")
            report["error"]["code"] = "unknown_host"
            return report
        selected = names[offset : offset + limit]
        report = {
            **cached,
            "results": {name: cached["results"][name] for name in selected},
            "next_offset": offset + len(selected) if offset + len(selected) < len(names) else None,
        }
        # Avoid returning mutable references into the shared cache.
        return json.loads(json.dumps(report))


def _host_metadata(host: Host) -> dict[str, Any]:
    """Expose host metadata and inherited data without top-level credentials."""
    return {
        "hostname": host.hostname,
        "platform": host.platform,
        "groups": [group.name for group in host.groups],
        "data": dict(host.items()),
    }


def _target_nornir(filter_criteria: dict[str, Any] | None, all_hosts: bool, num_workers: int | None = None) -> Nornir:
    """Require a deliberate selection before initializing any inventory backend."""
    if not filter_criteria and not all_hosts:
        raise ValueError("Target selection required: provide non-empty filter_criteria or set all_hosts=True.")
    return _get_nornir(filter_criteria, num_workers=num_workers)


def _inventory_choices(nr: Nornir) -> dict[str, list[dict[str, Any]]]:
    """Only aggregate public selection fields, never arbitrary inventory data."""
    choices = {}
    for field in ("site", "role", "platform"):
        counts = Counter()
        for host in nr.inventory.hosts.values():
            value = host.get(field)
            filter_field = field
            if isinstance(value, dict):
                nested_field = "slug" if value.get("slug") else "name"
                filter_field = f"{field}__{nested_field}"
                value = value.get(nested_field)
            if isinstance(value, str) and value:
                counts[(filter_field, value)] += 1
        choices[field] = [
            {"value": value, "count": count, "filter_criteria": {filter_field: value}}
            for (filter_field, value), count in sorted(counts.items())
        ]
    return choices


@mcp.tool()
def get_inventory_summary() -> dict[str, Any]:
    """Discover registered site, role and platform values and host counts without connecting to devices.

    For NetBox, site/role values are slugs from nested metadata; use site__slug/role__slug
    in filters. For YAML string metadata, use site/role directly.
    """
    try:
        nr = _get_nornir()
        return {"status": "success", "total_hosts": len(nr.inventory.hosts), "choices": _inventory_choices(nr)}
    except Exception:
        logger.exception("Error summarizing inventory")
        return {"status": "failed", "error": "Could not load inventory. Check the server configuration."}


@mcp.tool()
def preview_targets(filter_criteria: dict[str, Any] | None = None, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """Preview matching hosts, counts and platforms without connecting to devices.

    Empty filters preview all registered hosts. This is a live inventory preview, not
    a reservation: execution reloads the inventory. limit must be between 1 and 200.
    Only names, addresses, platforms and groups are returned; no credential data.
    """
    if offset < 0 or not 1 <= limit <= 200:
        return {"status": "failed", "error": "offset must be non-negative and limit must be between 1 and 200."}
    try:
        nr = _get_nornir()
        available_choices = _inventory_choices(nr)
        if filter_criteria:
            nr = nr.filter(F(**filter_criteria))
        names = sorted(nr.inventory.hosts)
        hosts = []
        for name in names[offset : offset + limit]:
            host = nr.inventory.hosts[name]
            hosts.append(
                {
                    "name": name,
                    "hostname": host.hostname,
                    "platform": host.platform,
                    "groups": [group.name for group in host.groups],
                }
            )
        next_offset = offset + len(hosts)
        return {
            "status": "success" if names else "no_hosts",
            "matched_hosts": len(names),
            "hosts": hosts,
            "next_offset": next_offset if next_offset < len(names) else None,
            "choices": available_choices,
            "platforms": _inventory_choices(nr)["platform"],
        }
    except Exception:
        logger.exception("Error previewing targets (filter=%s)", filter_criteria)
        return {"status": "failed", "error": "Could not load or filter inventory. Check the filter and configuration."}


@mcp.tool()
def get_inventory(filter_criteria: dict[str, Any] | None = None) -> str:
    """
    Get inventory hosts and their metadata, optionally filtered.
    Returned data includes values inherited from groups and defaults.

    Args:
        filter_criteria: Dictionary of key-value pairs to filter hosts
                         (e.g. {"name": "router1"} or {"role": "core", "site": "tokyo"}).
                         Matches host attributes or data fields.
    """
    try:
        nr = _get_nornir(filter_criteria)
        hosts_info = {name: _host_metadata(host) for name, host in nr.inventory.hosts.items()}
        return json.dumps(hosts_info, indent=2, default=str)
    except Exception as e:
        logger.exception(f"Error loading inventory (filter={filter_criteria})")
        return f"Error loading inventory: {str(e)}"


@mcp.tool()
def run_netmiko_command(
    command: str,
    filter_criteria: dict[str, Any] | None = None,
    use_textfsm: bool = False,
    all_hosts: bool = False,
    include_output: bool = False,
) -> ExecutionReport:
    """
    Run a show command concurrently on multiple hosts via Netmiko.

    Args:
        command: The CLI command to execute (e.g., 'show version').
        filter_criteria: Non-empty dictionary of key-value pairs to filter target hosts.
        use_textfsm: If True, attempts to parse output into structured data using ntc-templates.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.
    """
    started = monotonic()
    report = None
    try:
        # Nornir's context manager also closes connections on failed hosts.
        with _target_nornir(filter_criteria, all_hosts) as nr:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)
            agg_result = nr.run(
                task=netmiko_send_command, raise_on_error=False, command_string=command, use_textfsm=use_textfsm
            )
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("executing command", e, started, report)


def _get_tls_verify(host: Host) -> bool:
    return bool(host.get("tls_verify", True))


def _get_http_timeout(host: Host) -> float:
    """Per-host request timeout; falls back to HTTP_TIMEOUT when unset."""
    return float(host.get("http_timeout", HTTP_TIMEOUT))


def custom_http_task(
    task: Task,
    clients: dict[bool, httpx.Client],
    method: str,
    path: str,
    json_data: dict | None = None,
) -> Result:
    """Custom Nornir task to execute HTTP requests using shared clients."""
    # Build base URL from host attributes
    host = task.host
    base_url = host.get("base_url", f"https://{host.hostname}")
    headers = host.get("http_headers", {})
    verify = _get_tls_verify(host)
    if not verify:
        logger.warning(f"TLS verification disabled for host {host.name}")

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    response = clients[verify].request(
        method=method, url=url, headers=headers, json=json_data, timeout=_get_http_timeout(host)
    )
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError:
        result = response.text

    return Result(host=task.host, result=result)


@mcp.tool()
def run_http_request(
    method: str,
    path: str,
    filter_criteria: dict[str, Any] | None = None,
    json_data: dict | None = None,
    all_hosts: bool = False,
    include_output: bool = False,
) -> ExecutionReport:
    """
    Run an HTTP API request (REST) concurrently on multiple hosts.

    Args:
        method: HTTP method (e.g., 'GET', 'POST', 'PUT').
        path: API path to request (e.g., '/api/v1/system').
        filter_criteria: Dictionary to filter target hosts.
        json_data: Optional JSON payload for POST/PUT requests.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.

    Per-host settings come from inventory data, including group/default inheritance:
    'base_url', 'http_headers', 'tls_verify', and 'http_timeout' (seconds, defaults to 10).
    """
    started = monotonic()
    report = None
    try:
        with _target_nornir(filter_criteria, all_hosts) as nr, ExitStack() as stack:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)

            # Register cleanup immediately, including for partial initialization.
            # Clients are shared across threads, one per TLS verification setting.
            verify_values = {_get_tls_verify(host) for host in nr.inventory.hosts.values()}
            clients = {}
            for verify in verify_values:
                client = httpx.Client(verify=verify, timeout=HTTP_TIMEOUT)
                stack.callback(client.close)
                clients[verify] = client
            agg_result = nr.run(
                task=custom_http_task,
                raise_on_error=False,
                clients=clients,
                method=method,
                path=path,
                json_data=json_data,
            )
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("executing HTTP request", e, started, report)


def _resolve_serial_device_type(serial_settings: dict[str, Any], platform: str | None) -> str:
    """Netmiko selects its serial drivers by a device_type ending in '_serial'."""
    device_type = serial_settings.get("device_type") or platform
    if not device_type:
        raise ValueError(
            "Cannot determine serial device_type: set 'device_type' in serial_settings or 'platform' on the host."
        )
    if not device_type.endswith(SERIAL_DEVICE_TYPE_SUFFIX):
        device_type = f"{device_type}{SERIAL_DEVICE_TYPE_SUFFIX}"
    return device_type


def custom_serial_task(task: Task, command: str) -> Result:
    """Custom Nornir task to execute commands via Serial port using Netmiko."""
    host = task.host
    serial_settings = host.get("serial_settings")
    if not serial_settings:
        raise ValueError("No 'serial_settings' found in host data.")

    device_type = _resolve_serial_device_type(serial_settings, host.platform)
    # 'device_type' is Netmiko metadata; the rest goes straight to pyserial.
    pyserial_settings = {key: value for key, value in serial_settings.items() if key != "device_type"}

    connection_params = {
        "device_type": device_type,
        "serial_settings": pyserial_settings,
        "username": host.username,
        "password": host.password,
    }

    # Instantiate ConnectHandler directly for serial
    with ConnectHandler(**connection_params) as net_connect:
        output = net_connect.send_command(command)

    return Result(host=task.host, result=output)


@mcp.tool()
def run_serial_command(
    command: str, filter_criteria: dict[str, Any] | None = None, all_hosts: bool = False, include_output: bool = False
) -> ExecutionReport:
    """
    Run a CLI command on devices via direct Serial connection (using Netmiko).

    Note: Physical serial ports generally cannot be used concurrently by multiple threads,
    so hosts are processed sequentially (one at a time).

    Args:
        command: The CLI command to execute (e.g., 'show version').
        filter_criteria: Dictionary to filter target hosts.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.
    """
    started = monotonic()
    report = None
    try:
        with _target_nornir(filter_criteria, all_hosts, num_workers=1) as nr:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)
            agg_result = nr.run(task=custom_serial_task, raise_on_error=False, command=command)
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("executing serial command", e, started, report)


def custom_template_task(task: Task, template: Template) -> Result:
    """Custom Nornir task to render a precompiled Jinja2 template using host inventory data.

    The template MUST come from `_JINJA_ENV` (the module-level SandboxedEnvironment).
    The annotation cannot express that: a plain `jinja2.Template` would render here just
    as well, with the sandbox silently gone.
    """
    host = task.host
    # Exclude top-level credentials; inventory data is exposed as provided.
    safe_host = {"name": host.name, **_host_metadata(host)}
    rendered = template.render(host=safe_host)
    return Result(host=task.host, result=rendered)


@mcp.tool()
def generate_config(
    template_string: str,
    filter_criteria: dict[str, Any] | None = None,
    all_hosts: bool = False,
    include_output: bool = False,
) -> ExecutionReport:
    """
    Generate configuration from a Jinja2 template string for each target host.
    Does NOT deploy the configuration; useful for dry-runs and auditing.

    Args:
        template_string: Jinja2 template string. Available variables: {{ host.name }}, {{ host.hostname }},
                         {{ host.platform }}, {{ host.groups }}, {{ host.data['key'] }}. Other host
                         attributes (e.g. credentials) are not exposed.
                         host.data includes inherited group/default values.
        filter_criteria: Dictionary to filter target hosts.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.
    """
    started = monotonic()
    report = None
    try:
        with _target_nornir(filter_criteria, all_hosts) as nr:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)

            # Compile once for all hosts, inside the connection cleanup scope.
            template = _JINJA_ENV.from_string(template_string)
            agg_result = nr.run(task=custom_template_task, raise_on_error=False, template=template)
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("generating config", e, started, report)


@mcp.tool()
def run_netmiko_config(
    commands: list[str],
    filter_criteria: dict[str, Any] | None = None,
    all_hosts: bool = False,
    include_output: bool = False,
) -> ExecutionReport:
    """
    WRITE OPERATION: modifies device configuration.
    Deploy configuration commands concurrently to multiple hosts via Netmiko.
    Confirm with the user before calling this tool.

    Args:
        commands: List of configuration commands to execute.
        filter_criteria: Dictionary to filter target hosts.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.
    """
    started = monotonic()
    report = None
    try:
        with _target_nornir(filter_criteria, all_hosts) as nr:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)
            agg_result = nr.run(task=netmiko_send_config, raise_on_error=False, config_commands=commands)
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("executing config commands", e, started, report)


if __name__ == "__main__":
    mcp.run()
