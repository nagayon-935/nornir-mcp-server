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

`SimpleInventory` の `host_file`・`group_file`・`defaults_file` の相対パスは、既定のファイル名も含めて設定ファイルのディレクトリを基準に解決します。`NetBoxInventory2` のローカルなグループ・デフォルトファイルも同様です。絶対パスは維持します。その他のインベントリプラグインは、それぞれのパス解決方法を維持します。

`hosts.yaml` / `groups.yaml` / `defaults.yaml` は機器の認証情報を含むため `.gitignore` 対象です。テンプレートからコピーして作成してください。

```bash
cp hosts.yaml.example hosts.yaml
cp groups.yaml.example groups.yaml
```

`groups.yaml` を編集し、すべての `CHANGE_ME` を実際のログイン情報に置き換えてください。トップレベルの `username` / `password`（`run_serial_command` が使用）と、`connection_options.netmiko.extras` 配下（SSH/Telnet が使用）の両方が対象です。これらのファイルはコミットされません。

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

⚠️ インベントリファイルとは異なり、`config.yaml` は **git の管理対象** です。NetBox の API トークンを直接書き込まないでください。`NetBoxInventory2` はトークンを環境変数 `NB_TOKEN` から（URL を `NB_URL` から）読み込みます。

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
* 返却する `data` にはグループ・デフォルトから継承した値も含まれます。ホストに定義した値を優先します。

### `run_netmiko_command`
フィルタリングされた対象機器群に対して、並列で `show` 系コマンドを実行します。
* **引数**: `command` (実行するコマンド), `filter_criteria` (対象絞り込み条件), `use_textfsm` (Trueにするとntc-templatesを用いて構造化JSONで返します)

### `run_http_request`
フィルタリングされた対象機器群の REST API エンドポイントに HTTP リクエストを実行します。
* **引数**: `method` (GET/POST等), `path` (APIパス), `filter_criteria`, `json_data`
* 対象ホストのインベントリデータ (`host.data`) に `base_url` (例: `https://192.168.1.1`), `http_headers`, `tls_verify` (True/False), `http_timeout` (秒数、既定は 10) 等の情報を記載して利用します。
* これらの設定はグループ・デフォルトのデータにも定義できます。ホストに定義した値は継承した値より優先されます。

### `run_serial_command`
直接シリアル接続（コンソールケーブル等）を用いてコマンドを実行します。物理ポートは同時に1つのプロセスからしか使えないため、対象が複数ホストでも常に1台ずつ順番に処理されます（並列実行しません）。
* **引数**: `command` (実行するコマンド), `filter_criteria`
* 対象ホストのインベントリデータに `serial_settings` (port, baudrate 等) の記載が必要です。Netmiko のシリアルドライバは `device_type` の末尾が `_serial` である必要があるため、`serial_settings.device_type` を明示するか、未指定の場合は `host.platform`（例: `cisco_ios`）に自動で `_serial` を付与します。
* `serial_settings` はグループ・デフォルトのデータからも継承できます。
* この自動付与は Netmiko のドライバ一覧と照合されません。Netmiko がシリアルドライバを提供しているプラットフォームは一部のみのため、対応するドライバが無い場合（例: `arista_eos` → `arista_eos_serial`）は `Unsupported device_type` で失敗します。その場合は `serial_settings.device_type` を明示してください。
* ログイン認証には `hosts.yaml` / `groups.yaml` / `defaults.yaml` の**トップレベル**の `username` / `password` を使用します。`connection_options.netmiko.extras` 配下の値（SSH接続用）は参照しないため、シリアル接続も使う場合はトップレベルにも認証情報を設定してください（`groups.yaml.example` には両方を記載済みです）。これら3ファイルはいずれも `.gitignore` 対象です。

### `generate_config`
Jinja2 テンプレート（サンドボックス環境で実行）を用いて、コンフィグを動的生成します。（実機への適用は行いません）
* **引数**: `template_string` (Jinja2テンプレート), `filter_criteria`
* テンプレート内で参照できるのは `{{ host.name }}`, `{{ host.hostname }}`, `{{ host.platform }}`, `{{ host.groups }}`, `{{ host.data['key'] }}` のみです。認証情報などその他の属性は意図的に公開されません。
* `host.data` にはグループ・デフォルトから継承した値も含まれ、ホストに定義した値を優先します。データの内容はそのまま公開されるため、インベントリ照会やテンプレートで使うデータに秘密情報を含めないでください。

### `run_netmiko_config`
⚠️ **実機の設定を変更する破壊的な操作です。** 生成したコンフィグや指定のコマンドリストを、対象機器に並列で設定投入 (Config Deploy) します。
* **引数**: `commands` (投入する設定コマンドのリスト), `filter_criteria`
* 本サーバー唯一の書き込み経路です。ツールの説明文で「実行前にユーザーへ確認すること」をエージェントに指示しています。このツールについては必ず人間の承認を挟んでください。

## 対象の発見と明示的な選択

処理の実行前に `get_inventory_summary` で拠点・役割・機種の値と件数を確認できます。各候補にはそのまま使える `filter_criteria` が含まれます。NetBox の入れ子の値には `site__slug` などを使います。

`preview_targets(filter_criteria, offset=0, limit=50)` は、実機へ接続せずに対象の機器名・接続先・機種・グループ・件数を返します。機器名順にページ分割され、limit は 1〜200 です。認証情報は返しません。プレビューは現在の台帳を参照するもので、後の実行対象を固定するものではありません。

タスクを実行するツールには、空でない `filter_criteria`、または明示的な `all_hosts=True` が必要になりました。両方を省略すると台帳の読み込み前にエラーを返します。空でないフィルターを指定した場合は、`all_hosts=True` でも対象を絞り込みます。

例：候補を確認 → `{"site": "tokyo", "role": "core"}` で対象をプレビュー → `run_netmiko_command(command="show version", filter_criteria={"site": "tokyo", "role": "core"})` を実行。NetBox では候補に含まれる入れ子のフィルターを使ってください。

## 構造化された実行結果

タスクを実行するツールは、JSON文字列に代わって構造化されたMCPオブジェクトを返します。`status`（`success`・`partial_failure`・`failed`・`no_hosts`）、`execution_id`、`summary`（総件数・成功件数・失敗件数・所要秒数）、機器ごとの `results` が含まれます。エラーには機械判読用の `code`・メッセージ・確認先の案内が入り、認証失敗・タイムアウト・HTTPエラーを区別できます。自動再実行は行いません。

既定の `include_output=False` では機器ごとの状態とエラーを返し、成功したコマンドの詳細出力を省略します。すぐに詳細も受け取る場合は `include_output=True` を指定してください。後から `get_execution_details(execution_id, host_names=["router1"])` で特定の機器の出力を取得することもできます。`offset`・`limit`（1〜200）によるページ分割にも対応し、summary/status は常に元の実行全体を表します。詳細取得で処理を再実行することはありません。

結果の保持期間は同じサーバープロセスで最大15分、上限は100実行・シリアライズ後の合計16 MiBです。上限に達した場合は古い結果が先に削除され、再起動でも消えます。キャッシュ上限を超える出力は `details_available=False` としてその場で返します。後処理で失敗しても完了したタスクの結果を保持し、`cleanup_failed` を返します。設定変更を再実行する前に結果を確認してください。台帳の照会・候補発見ツールは台帳向けの形式を維持します。

## オフラインでの設定診断

実機操作の前にMCPツール `diagnose_setup` を実行すると、設定・台帳ファイルの解決後のパス、YAMLやグループ参照の不備、認証情報のプレースホルダー、SSH・シリアル認証情報の不足、未対応の機種、HTTP設定の不備を確認できます。診断コードと修正の案内を返し、認証情報の値やYAMLパーサーの抜粋は表示しません。省略可能なグループ・デフォルトファイルがない場合は警告になります。

診断中はネットワークアクセスを行いません。ローカルのSimpleInventoryファイルを直接読み込み、インベントリの変換処理は実行せず、NetBoxや独自の外部インベントリも読み込みません。外部台帳の確認はスキップとして表示し、`hosts_checked` はnullです。NB_TOKENの設定有無のみ確認し、値は表示しません。実際の疎通や認証の有効性は、対象を明示した照会操作で別途確認してください。

`mcpServers` 形式のJSONを受け付けるMCPクライアントでは、`examples/mcp-client.json` をコピーして2か所の絶対パスを置き換えて使えます。クライアントから `uv` を見つけられるようにするか、実行ファイルの絶対パスを指定してください。標準入出力で起動する例であり、APIの認証情報をチャットに入力する必要はありません。

## マルチベンダー環境での定型照会

`get_inspection_presets` で照会の種類とコマンドを確認できます。`run_inspection(inspection="os_version" | "interfaces", filter_criteria=..., include_output=True)` は、各ホストの実際のNetmiko接続設定の機種に合わせて個別にコマンドを選びます。空でないフィルター、または明示的な `all_hosts=True` が必要です。定義済みの照会コマンドのみ実行し、任意のコマンドや設定投入は受け付けません。

| 機種 | OS情報 | インターフェース概要 |
|---|---|---|
| Cisco IOS / IOS XE | `show version` | `show interfaces description` |
| Cisco NX-OS | `show version` | `show interface brief` |
| Cisco IOS XR | `show version` | `show interfaces description` |
| Juniper Junos | `show version` | `show interfaces terse` |
| Arista EOS | `show version` | `show interfaces status` |

Nornirの機種別名と対応するTelnetドライバも同じコマンドを使います。シリアルの定型照会は未対応です。未対応の機種にはコマンドを送らず `unsupported_platform` を返し、他の機器の照会は続けます。必要に応じて実行全体は部分失敗として報告します。認識できるCLIの拒否メッセージには `command_rejected` を返します。

成功時の詳細には照会の種類、選んだ機種・コマンド、`parsed`、共通の `facts`、元の `data` が含まれます。TextFSMによる解析は可能な場合に行い、対応するパーサーがなければ生のテキストを維持し、`parsed=False`、factsは空になります（Junosのterse出力など）。解析できたOS情報はversion/model/hostname、インターフェース情報はname/status/protocol/descriptionとして返します。欠けている値はnullとし、機種固有の状態値は推測で変換しません。他の実行ツールと同様に、詳細をその場で返すには `include_output=True` が必要です。後から `get_execution_details` でも取得できます。

例：`run_inspection(inspection="interfaces", filter_criteria={"site": "tokyo"}, include_output=True)` では、対応するCisco・Juniper・Arista機器を一度に照会できます。既存の `run_netmiko_config` は引き続き同じコマンド列を対象の全機器へ送る方式であり、生成したコンフィグの自動振り分け・投入は行いません。

コマンドの参照先：[Cisco NX-OS](https://www.cisco.com/c/en/us/td/docs/switches/datacenter/nexus9000/sw/7-x/command_references/show_commands/b_N9K_Show_Commands_703i4x/b_N9K_Show_Commands_703i4x_chapter_01001.html)、[Cisco IOS XR](https://www.cisco.com/c/en/us/td/docs/iosxr/cisco8000/Interfaces/b-interfaces-hardware-component-cr-8000/global-interface-commands.html)、[Junos](https://www.juniper.net/documentation/us/en/software/junos/cli-reference/topics/topic-map/operational-commands.html)、[Arista EOS](https://www.arista.com/en/um-eos/eos-ethernet-ports?tmpl=component)。

## テスト

`tests/` にユニットテストがあります。

```bash
uv sync
uv run pytest tests/ -v
```

## License
MIT License
