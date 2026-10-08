English | [日本語](README.ja.md)

# nornir-mcp-server

`nornir-mcp-server` is an MCP (Model Context Protocol) server that exposes **Nornir**, a network automation framework, to AI agents.

Rather than the traditional "iterate over every device in a list" approach, it lets an AI agent leverage Nornir's metadata-based filtering (`nornir.filter`) to autonomously select targets — e.g. "only the core routers in Tokyo" — and run tasks against them in parallel.

## Current Status
* **Phase 1, 2, 3 complete (full feature set)**
  * `SimpleInventory` (YAML) and NetBox (`nornir_netbox`) support
  * Parallel execution of `show` commands (SSH/Telnet)
  * REST API request execution (via `httpx`)
  * Direct serial connection support to hosts (`netmiko` serial mode)
  * Dynamic config generation with Jinja2
  * Deploying configuration to real devices (Config Deploy)

## Key Features
* **Flexible inventory**: Uses Nornir's plugin ecosystem as-is — the standard `SimpleInventory` (hosts.yaml / groups.yaml) rather than a custom format. Dynamic inventory from NetBox is also supported.
* **Autonomous filtering**: The AI narrows down target devices at runtime by supplying metadata such as `{"role": "core", "site": "tokyo"}`.
* **Configuration management and deployment**: Give (or have the AI write) a template, and it instantly generates per-host configuration and can push it straight to the real devices.
* **Multi-protocol support**: SSH/Telnet, REST APIs, and physical serial console connections are all reachable from a single MCP server.
* **Fast parallel execution**: Nornir's ThreadedRunner dispatches tasks to multiple devices safely and quickly (serial ports are the exception — see below).

## Installation and Setup

This project is managed with the `uv` package manager.

### 1. Install dependencies
```bash
cd nornir-mcp-server
uv sync
```

### 2. Configure the inventory
YAML files are used by default; edit `config.yaml` to switch to NetBox, etc.

By default the config file is loaded from `config.yaml` next to `server.py`, but you can point it anywhere with the `NORNIR_MCP_CONFIG` environment variable (independent of the process's working directory).

```bash
export NORNIR_MCP_CONFIG=/path/to/config.yaml
```

Only the config file path is resolved this way. The `host_file` / `group_file` paths **inside** that config are resolved by Nornir against the process's current working directory, so a config stored outside the repository must use absolute inventory paths.

`hosts.yaml` / `groups.yaml` / `defaults.yaml` contain device credentials and are gitignored. Copy them from the templates:

```bash
cp hosts.yaml.example hosts.yaml
cp groups.yaml.example groups.yaml
```

Edit `groups.yaml` and replace every `CHANGE_ME` with real login credentials — both the top-level `username` / `password` (used by `run_serial_command`) and the ones under `connection_options.netmiko.extras` (used for SSH/Telnet). These files are never committed.

**`config.yaml` (SimpleInventory example):**
```yaml
---
inventory:
  plugin: SimpleInventory
  options:
    host_file: "hosts.yaml"
    group_file: "groups.yaml"
logging:
  enabled: False
```

**`config.yaml` (NetBox integration example):**

⚠️ Unlike the inventory files, `config.yaml` **is tracked in git** — never write your NetBox API token into it. `NetBoxInventory2` reads the token from the `NB_TOKEN` environment variable (and the URL from `NB_URL`).

```bash
export NB_TOKEN=your-netbox-api-token
```

```yaml
---
inventory:
  plugin: NetBoxInventory2
  options:
    nb_url: "https://netbox.local"
logging:
  enabled: False
```

### 3. Start the MCP server
Run it locally with:

```bash
uv run mcp run server.py
```

### Running via Docker (GHCR)

GitHub Actions automatically builds and publishes a container image to the GitHub Container Registry (GHCR) on every commit to `main`.
If you have Docker, you can use the server right away without cloning the repository.

```bash
docker pull ghcr.io/nagayon-935/nornir-mcp-server:latest
```

**Example:**
Mount your config files (`config.yaml` and the inventory files) into `/app` in the container. (MCP communicates over stdio, so you'll need `-i` or similar — follow your MCP client's setup instructions.)

```bash
docker run -i --rm \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/hosts.yaml:/app/hosts.yaml \
  -v $(pwd)/groups.yaml:/app/groups.yaml \
  ghcr.io/nagayon-935/nornir-mcp-server:latest
```

## MCP Tools Provided

The following tools are currently exposed to AI agents.

### `get_inventory`
Returns the hosts and their metadata from the current inventory, optionally filtered.
* **Args**: `filter_criteria` (e.g. `{"site": "tokyo"}`)
* Returned `data` includes values inherited from groups and defaults; host values take precedence.

### `run_netmiko_command`
Runs a `show`-style command in parallel across the filtered target devices.
* **Args**: `command` (the CLI command to run), `filter_criteria` (target filter), `use_textfsm` (if `True`, parses output into structured JSON using ntc-templates)

### `run_http_request`
Sends an HTTP request to the REST API endpoint of each filtered target device.
* **Args**: `method` (GET/POST/etc.), `path` (API path), `filter_criteria`, `json_data`
* Configure the target host's inventory data (`host.data`) with `base_url` (e.g. `https://192.168.1.1`), `http_headers`, `tls_verify` (True/False), and `http_timeout` (seconds, defaults to 10) as needed.
* These settings can also be defined in group or default data. Host values override inherited values.

### `run_serial_command`
Runs a command over a direct serial connection (console cable, etc.). Physical ports can only be held by one process at a time, so even with multiple target hosts, they are always processed one at a time (never in parallel).
* **Args**: `command` (the CLI command to run), `filter_criteria`
* The target host's inventory data must include `serial_settings` (port, baudrate, etc.). Netmiko's serial drivers require a `device_type` ending in `_serial`, so either set `serial_settings.device_type` explicitly, or leave it unset and it will be derived by appending `_serial` to `host.platform` (e.g. `cisco_ios`).
* `serial_settings` can also be inherited from group or default data.
* The derivation is not validated against Netmiko's driver table, and Netmiko only ships serial drivers for a subset of platforms. A platform with no matching `*_serial` driver (e.g. `arista_eos` → `arista_eos_serial`) fails with `Unsupported device_type`; set `serial_settings.device_type` explicitly in that case.
* Login uses the **top-level** `username` / `password` fields from `hosts.yaml` / `groups.yaml` / `defaults.yaml`. It does **not** read `connection_options.netmiko.extras` (which is used for SSH connections), so if you also use serial connections, set credentials at the top level too — `groups.yaml.example` ships both. All three files are gitignored.

### `generate_config`
Renders a Jinja2 template (in a sandboxed environment) to generate configuration. Does **not** deploy it — useful for dry runs and auditing.
* **Args**: `template_string` (a Jinja2 template), `filter_criteria`
* Only `{{ host.name }}`, `{{ host.hostname }}`, `{{ host.platform }}`, `{{ host.groups }}`, and `{{ host.data['key'] }}` are available inside the template. Other host attributes (such as credentials) are intentionally not exposed.
* `host.data` includes values inherited from groups and defaults; host values take precedence. Data fields are exposed as provided, so avoid placing secrets in data used by inventory queries or templates.

### `run_netmiko_config`
⚠️ **This is a destructive operation that changes device configuration.** Deploys the given configuration commands (or a generated config) to the target devices in parallel.
* **Args**: `commands` (list of configuration commands to deploy), `filter_criteria`
* This is the only write path in the server. The tool description instructs the agent to confirm with you before calling it — keep a human in the loop for this tool.

## Target discovery and explicit selection

Before running an operation, use `get_inventory_summary` to discover site, role and platform values and counts. Each choice includes a ready-to-use `filter_criteria`; NetBox nested values use filters such as `site__slug`.

`preview_targets(filter_criteria, offset=0, limit=50)` returns the matching host names, addresses, platforms, groups and counts without connecting to devices. Results are sorted by name and paginated (limit 1–200). It exposes no authentication data. The preview reads the current inventory; it does not reserve targets for a later operation.

All five task-running tools now require a non-empty `filter_criteria`, or an explicit `all_hosts=True`. Omitting both is rejected before loading inventory. A non-empty filter continues to restrict targets even when `all_hosts=True`.

Example: discover values → preview with `{"site": "tokyo", "role": "core"}` → run `run_netmiko_command(command="show version", filter_criteria={"site": "tokyo", "role": "core"})`. For NetBox, use the exact nested filters returned by discovery.

## Testing

Unit tests live in `tests/`.

```bash
uv sync
uv run pytest tests/ -v
```

## License
MIT License
