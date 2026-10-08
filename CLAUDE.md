# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP (Model Context Protocol) server that exposes the **Nornir** network automation framework to AI agents. The design premise: instead of "iterate over a device list", the agent supplies metadata filters (`{"role": "core", "site": "tokyo"}`) and Nornir resolves the target set and runs tasks against it in parallel.

Everything lives in one module — `server.py`, which is also the only entry point.

## Commands

Managed with `uv` (Python >= 3.13).

```bash
uv sync
```

Run the server locally (stdio transport):

```bash
uv run mcp run server.py
```

Tests (the suite stubs `_get_nornir`, so it never touches a device or the network):

```bash
uv run pytest tests/ -v
```

Target is 80%+ coverage. Cover new tools at both the helper level and the tool level — the no-hosts early return and connection cleanup contract are parametrized across every task-running tool in `tests/test_tool_contract.py` (the `CONNECTION_TOOLS` list in `tests/support.py`); add new tools to that list. Tests are split by area (`test_http.py`, `test_serial.py`, `test_template.py`, `test_inventory.py`, `test_results.py`); shared fakes live in `tests/support.py`. A regression test uses the real Nornir runner with fake connections to verify cleanup on failed hosts, without contacting devices.

Single test / single class:

```bash
uv run pytest tests/test_serial.py::TestResolveSerialDeviceType::test_appends_serial_suffix_to_platform -v
```

`pyproject.toml` sets `pythonpath = ["."]` so `from server import ...` works in tests without installing the package.

Lint and format (ruff is configured in `pyproject.toml` under `[tool.ruff]`, and pinned in the dev group — its `I` rules cover import sorting):

```bash
uv run ruff check .
uv run ruff format .
```

Before running anything that touches real devices, the inventory must exist locally:

```bash
cp hosts.yaml.example hosts.yaml
cp groups.yaml.example groups.yaml
```

Both are gitignored — they hold device credentials.

## Architecture

### Config resolution

`CONFIG_FILE` is resolved once at import time from `NORNIR_MCP_CONFIG`, falling back to `config.yaml` **next to `server.py`** (not the process CWD). This matters because MCP clients launch the server from arbitrary working directories. Note that `config.yaml` itself points at `hosts.yaml` / `groups.yaml` by relative path, which Nornir resolves against the CWD — so a config outside the repo needs absolute inventory paths.

`config.yaml` is **tracked in git**, unlike `hosts.yaml` / `groups.yaml` / `defaults.yaml` (all gitignored). No credential ever goes in it: the NetBox inventory plugin reads `NB_URL` / `NB_TOKEN` from the environment, and device logins live in the gitignored inventory files.

### The tool pattern

Every `@mcp.tool()` that *runs tasks against hosts* goes through `_run_tool(action, filter_criteria, run, *, num_workers=None, log_context="")`, and new tools should do the same. The tool supplies only a `run(nr, stack)` callable that returns the `AggregatedResult` (a lambda for simple tools; a nested function when setup is needed, as in `run_http_request` and `generate_config`). `_run_tool` owns the rest of the contract:

1. `_get_nornir(filter_criteria, num_workers)` — fresh `InitNornir` per call (no shared long-lived Nornir object), then `nr.filter(F(**filter_criteria))`.
2. Early-return `NO_MATCH_MESSAGE` (`"No hosts matched the filter criteria."`) when the filter selects nothing.
3. Everything runs inside `with _get_nornir(...) as nr, ExitStack() as stack`, including the early return and any setup. Nornir's context manager closes connections on both successful and failed hosts; plain `nr.close_connections()` excludes failed hosts by default. Connection cleanup is not optional; leaking netmiko sessions holds device VTYs open. Extra resources (e.g. httpx clients) are registered on `stack` so they are released on every path.
4. `_format_agg_result(agg_result)` — collapses Nornir's `AggregatedResult` into `{host: {"output": ...}}` or `{host: {"error": ...}}` JSON. Returns the first failed result's error if any task/subtask failed, otherwise `multi_result[0]`'s output; tolerates an empty `MultiResult`.
5. Broad `except Exception` returning `"Error {action}: ..."` as a **string** rather than raising — MCP tools return text to the agent, so failures must be reported, not propagated. Logged with `logger.exception(...)` including `log_context` and the filter criteria.

`get_inventory` is the deliberate exception: it only reads `nr.inventory`, so it bypasses `_run_tool` — no `nr.run`, no `close_connections()` (it opens no connections), no `_format_agg_result`, and it returns `{}` rather than the no-match string. Don't "fix" it to conform.

### Task implementations

- **`run_netmiko_command` / `run_netmiko_config`** delegate to `nornir_netmiko` tasks directly.
- **`custom_http_task`** — httpx `Client` instances are created **before** threads start, one per distinct `tls_verify` value across the selected hosts, and passed into the task so the connection pool is reused. Each client is registered with an `ExitStack` immediately so partial initialization or cleanup failures still close the other resources. Per-host settings use `host.get(...)` to include group/default inheritance: `base_url` (defaults to `https://{hostname}`), `http_headers`, `tls_verify`, `http_timeout` (seconds, falls back to the `HTTP_TIMEOUT` constant and is applied per request). Disabling TLS verification logs a warning.
- **`custom_serial_task`** — bypasses Nornir's connection plugins and instantiates `netmiko.ConnectHandler` directly. `run_serial_command` forces `num_workers=1`: a physical serial port cannot be shared across threads. Credentials come from the host's **top-level** `username`/`password`, *not* from `connection_options.netmiko.extras` (which only feeds SSH) — this asymmetry is a recurring source of confusion. `_resolve_serial_device_type` enforces netmiko's requirement that serial drivers use a `*_serial` device_type, deriving it from `platform` when unset.
- **`_host_metadata`** — shared by inventory queries and template rendering. Uses `host.items()` to include group/default data with host values taking precedence. HTTP and serial settings follow the same inheritance using `host.get(...)`. Top-level credentials are excluded; data fields are exposed as provided, including any secrets the inventory author puts there.
- **`custom_template_task`** — takes an already-compiled `Template`; `generate_config` compiles it once via the module-level `SandboxedEnvironment` (thread-safe, shared) before `nr.run`, because `from_string` has no cache and compiling inside the task would re-parse the same template per host. The template receives a hand-built `safe_host` dict containing only `name`, `hostname`, `platform`, `groups`, `data` — **never the `Host` object itself**, so `{{ host.password }}` cannot leak credentials. There is a test asserting this; preserve the property when touching the template path.

### Dependency subtleties

`httpx` and `netmiko` are imported directly by `server.py`, so both are declared explicitly in `pyproject.toml` even though they would also arrive transitively via `mcp[cli]` and `nornir-netmiko`. Keep it that way: relying on the transitive path means an upstream dropping either one breaks the server at import. Any new direct import gets the same treatment in **three** places, which are easy to drift apart: `pyproject.toml` `dependencies`, the `dependencies=[...]` list passed to `FastMCP(...)` (used by `mcp install` to build its isolated env), and `uv lock` — re-run it in the same change, because the Dockerfile builds with `uv sync --frozen`, which fails on a stale lock.

## Safety

`run_netmiko_config` is the only write path and mutates real device configuration. Its docstring instructs the agent to confirm with the user first; keep that wording intact on any edit. Treat the same rule as binding for you: never invoke it (or add another write-capable tool that runs without confirmation) against lab or production gear without explicit approval.

## Docs and CI

`README.md` (English) and `README.ja.md` (Japanese) are kept in sync — a change to the tool list, arguments, or setup steps must land in both. Pushes to `main` build and publish `ghcr.io/nagayon-935/nornir-mcp-server:latest` via `.github/workflows/docker-publish.yml`; the Dockerfile copies only `server.py`, so config and inventory are expected to be bind-mounted into `/app`.
