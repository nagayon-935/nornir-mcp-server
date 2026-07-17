[English](README.md) | 日本語

# nornir-mcp-server

`nornir-mcp-server` は、ネットワーク自動化フレームワークである **Nornir** を、AIエージェントから操作可能にするための MCP (Model Context Protocol) サーバーです。

従来の「全機器リストを順番に叩く」アプローチではなく、AI 自身が Nornir の強力なメタデータ・フィルタリング機能（`nornir.filter`）を活用し、「東京拠点のコアルーターだけを対象にする」といった自律的な対象選定と並列実行を可能にします。

## 現在のステータス
* **Phase 1, 2, 3 実装完了 (フル機能)**
  * `SimpleInventory` (YAML) および NetBox (`nornir_netbox`) サポート
  * `show` 系コマンドの並列実行 (SSH/Telnet)
  * REST API コマンドの実行サポート (`httpx` 経由)
  * ホストへの直接シリアル接続サポート (`netmiko` シリアルモード)
  * Jinja2 を用いたコンフィグの動的生成
  * 実機への設定投入 (Config Deploy)

## 主な特徴
* **柔軟なインベントリ**: Nornir のプラグインエコシステムをそのまま利用。独自の形式ではなく標準の `SimpleInventory` (hosts.yaml / groups.yaml) を利用します。NetBoxからの動的取得にも対応しています。
* **自律的なフィルタリング**: 実行時に `{"role": "core", "site": "tokyo"}` などのメタデータを AI が指定することで、対象機器を自動で絞り込みます。
* **構成管理と設定投入**: AI にテンプレートを渡すか作らせることで、対象機器ごとの個別コンフィグを瞬時に生成し、そのまま実機へ反映させることが可能です。
* **多様なプロトコル対応**: SSH/Telnetに加え、REST APIや物理シリアルコンソール接続まで単一のMCPサーバーから操作可能です。
* **高速な並列処理**: Nornir の ThreadedRunner により、複数デバイスへのタスク発行を安全かつ高速に処理します（※シリアルポートを除く）。

## インストールと起動

このプロジェクトはパッケージマネージャ `uv` で管理されています。

### 1. 依存関係のインストール
```bash
cd nornir-mcp-server
uv sync
```

### 2. インベントリの設定
デフォルトでは YAML ファイルを使用しますが、`config.yaml` を変更することで NetBox 等に切り替えられます。

読み込む設定ファイルのパスは既定では `server.py` と同じディレクトリの `config.yaml` ですが、環境変数 `NORNIR_MCP_CONFIG` を設定することで任意のパスに変更できます（プロセスの起動ディレクトリに依存しません）。

```bash
export NORNIR_MCP_CONFIG=/path/to/config.yaml
```

`hosts.yaml` / `groups.yaml` は機器の認証情報を含むため `.gitignore` 対象です。テンプレートからコピーして作成してください。

```bash
cp hosts.yaml.example hosts.yaml
cp groups.yaml.example groups.yaml
```

`groups.yaml` を編集し、`CHANGE_ME` を実際のログイン情報に置き換えてください。このファイルはコミットされません。

**`config.yaml` (SimpleInventoryの例):**
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

**`config.yaml` (NetBox連携の例):**
```yaml
---
inventory:
  plugin: NBInventory
  options:
    nb_url: "https://netbox.local"
    nb_token: "YOUR_NETBOX_API_TOKEN"
logging:
  enabled: False
```

### 3. MCP サーバーの起動
以下のコマンドでローカル実行します。

```bash
uv run mcp run server.py
```

### Docker での実行 (GHCR)

GitHub Actions により、`main` ブランチへのコミット時に自動でコンテナイメージがビルドされ、GitHub Container Registry (GHCR) に公開されます。
Docker環境があれば、手元にリポジトリをcloneしなくてもすぐに利用可能です。

```bash
docker pull ghcr.io/nagayon-935/nornir-mcp-server:latest
```

**起動例:**
設定ファイル（`config.yaml` などのインベントリ群）をコンテナの `/app` にマウントして起動します。（※ MCPは標準入出力を利用するため `-i` オプション等が必要です。お使いの MCP クライアントの設定方法に従ってください）

```bash
docker run -i --rm \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/hosts.yaml:/app/hosts.yaml \
  -v $(pwd)/groups.yaml:/app/groups.yaml \
  ghcr.io/nagayon-935/nornir-mcp-server:latest
```

## 提供する MCP ツール

現在、AI エージェント向けに以下のツールを提供しています。

### `get_inventory`
現在のインベントリから、条件に合致するホスト一覧とメタデータを取得します。
* **引数**: `filter_criteria` (例: `{"site": "tokyo"}`)

### `run_netmiko_command`
フィルタリングされた対象機器群に対して、並列で `show` 系コマンドを実行します。
* **引数**: `command` (実行するコマンド), `filter_criteria` (対象絞り込み条件), `use_textfsm` (Trueにするとntc-templatesを用いて構造化JSONで返します)

### `run_http_request`
フィルタリングされた対象機器群の REST API エンドポイントに HTTP リクエストを実行します。
* **引数**: `method` (GET/POST等), `path` (APIパス), `filter_criteria`, `json_data`
* 対象ホストのインベントリデータ (`host.data`) に `base_url` (例: `https://192.168.1.1`), `http_headers`, `tls_verify` (True/False) 等の情報を記載して利用します。

### `run_serial_command`
直接シリアル接続（コンソールケーブル等）を用いてコマンドを実行します。物理ポートは同時に1つのプロセスからしか使えないため、対象が複数ホストでも常に1台ずつ順番に処理されます（並列実行しません）。
* **引数**: `command` (実行するコマンド), `filter_criteria`
* 対象ホストのインベントリデータに `serial_settings` (port, baudrate 等) の記載が必要です。Netmiko のシリアルドライバは `device_type` の末尾が `_serial` である必要があるため、`serial_settings.device_type` を明示するか、未指定の場合は `host.platform`（例: `cisco_ios`）に自動で `_serial` を付与します。
* ログイン認証には `hosts.yaml` / `groups.yaml` / `defaults.yaml` の**トップレベル**の `username` / `password` を使用します。`connection_options.netmiko.extras` 配下の値（SSH接続用）は参照しないため、シリアル接続も使う場合はトップレベルにも認証情報を設定してください。

### `generate_config`
Jinja2 テンプレート（サンドボックス環境で実行）を用いて、コンフィグを動的生成します。（実機への適用は行いません）
* **引数**: `template_string` (Jinja2テンプレート), `filter_criteria`
* テンプレート内で参照できるのは `{{ host.name }}`, `{{ host.hostname }}`, `{{ host.platform }}`, `{{ host.groups }}`, `{{ host.data['key'] }}` のみです。認証情報などその他の属性は意図的に公開されません。

### `run_netmiko_config`
⚠️ **実機の設定を変更する破壊的な操作です。** 生成したコンフィグや指定のコマンドリストを、対象機器に並列で設定投入 (Config Deploy) します。
* **引数**: `commands` (投入する設定コマンドのリスト), `filter_criteria`

## テスト

`tests/` にユニットテストがあります。

```bash
uv sync
uv run pytest tests/ -v
```

## License
MIT License
