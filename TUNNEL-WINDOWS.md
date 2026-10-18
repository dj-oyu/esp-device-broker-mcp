# Windows: Tunnel の準備

まずローカルの認証付き mock で確認します。クラウド Tunnel の作成、API キー発行、
アプリ登録、実機操作は自動実行しません。PowerShell 5.1/7 向けコードは静的レビュー済みですが、
この更新の Windows ネイティブ起動と実際のクラウド接続はまだ未検証です。

## v0.5: GUI で設定・接続・死活を管理（推奨）

```powershell
uv sync --frozen
& .\Start-CardputerGui.ps1
```

初回は汎用 mock 設定を device.mock.local.toml に作ります。既存設定は上書きしません。
右上の **Tunnel...** を開き、同じ broker TOML の `[tunnel]` を編集して明示的に保存します。
公式 client の EXE/ZIP と、利用者が既に用意した Tunnel ID を設定してください。
Save はコメント・書式を再生成しますが、broker/serial の設定値は保持します。
実際の ID や key を自動発行したり、設定だけで接続したりしません。

**Connect...** で OpenAI Tunnel runtime API key を非表示入力します。
Cardputer の発行済み remote key を使う場合は、同じダイアログの別欄に入力します。
空欄なら、その broker 起動時の一時 remote token を使います。
接続前の読み取り専用 client_identity で remote_mcp の権限を確認し、local_cli key を拒否します。
キー入力は1回限りで、保存・表示・引数への埋込み・自動再利用をしません。
同一 OS ユーザー/管理者からのプロセスメモリー隔離は保証しません。

表示は分かれています:
- Owned Tunnel client alive: GUI が起動したプロセスの生存
- Local readiness: 公式 client のローカル準備状態
- Cloud polling success: 固定仕様に合う、新しく成功した poll の観測。未観測・古い・非対応なら unknown
- Application discovery verified: この機能では確認できず unknown。実際のアプリで別途確認

unexpected exit は終了コードと固定メッセージで表示し、自動再接続しません。
終了後は Connect で再開します。動作中の **Manual restart** は所有 client を停止確認してから
新しいキー入力を求めます。観測した 401/403 はその client を停止し、再入力を待ちます。
**Disconnect**、異常終了、再接続、GUI 終了は broker のジョブ・キュー・lease を変更しません。
実行中の実機操作を再送・再実行・中断しません。

この GUI が起動した Windows Job Object の子プロセスだけを停止します。
他のランチャーで起動した client は監視・引継ぎ・強制停止しません。
停止確認が取れなければ所有 lock を保持し、再接続と GUI 終了を拒否します。
Tunnel パネルだけを閉じる操作は非表示にするだけで、監視は継続します。
親ウィンドウを閉じるときは、所有 Tunnel の切断を確認します。broker/jobs は継続します。

起動中は設定編集を無効化します。canonical broker TOML が外部で変更された場合、
次の接続は Reload が必要です。既存別ファイルの設定を変更した場合も明示的に読み直してください。
HTTP health は接続・ヘッダー・本文を含めた総時間とサイズを制限します。
ただし公式 client の実行と Windows Job Object は、この環境では実機 Windows 未検証です。

以下のコンソール方式は互換用です。GUI 監視を使う場合は上の起動方法を選んでください。
`Start-CardputerTunnel.ps1 -Gui` も同じ GUI 管理に入り、-Connect があっても自動接続しません。

## 1. API キー不要の準備確認

既存 broker が動いている場合は、ジョブが動いていないことを確認してそのコンソールで停止します。
このスクリプトは既存 broker を再利用・強制停止せず、新規起動を拒否します。
MCP フォルダーの通常の PowerShell から実行します。

```powershell
uv sync --frozen
& .\Start-CardputerTunnel.ps1
```

別コンソールに認証付き mock broker を起動し、元のウィンドウから remote 用認証で
MCP tools/list と device_status を確認します。ここまで API キーや公式 Tunnel client は不要です。
ローカル用コマンドは元のウィンドウで使います。二重クリックや別の powershell -File 経由では
親ウィンドウに認証環境変数が戻らないため、この実行方法を使ってください。

## 2. 利用者が行う公式セットアップ

以下は自動化していません。実際の作成・権限付与は、利用者が確認して実施します。

- [公式 Secure MCP Tunnels ガイド](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)で、対象組織・ワークスペースの利用可否を確認
- Tunnel を作成し、実行に必要な Read + Use 権限に限定した runtime API key を用意
- [公式 v0.0.15 Windows AMD64 ZIP](https://github.com/openai/tunnel-client/releases/download/v0.0.15/tunnel-client-v0.0.15-windows-amd64.zip)をダウンロードし展開
- ZIP と展開した tunnel-client.exe の両方を保管。ランチャーが固定 ZIP SHA-256 と、その中の exe の一致を検証
- examples/tunnel.example.toml を broker.local.toml にコピーし、同じファイルの `[tunnel]` に exe、ZIP、既存 Tunnel ID を記入。API キー、Cardputer client key、bearer token は記入しない

このランチャーは v0.0.15 Windows AMD64 に固定しています。ARM64 や別バージョンを黙って実行しません。
署名・provenance の追加確認は[公式検証手順](https://github.com/openai/tunnel-client#validate-a-downloaded-release)を参照してください。
公式クライアントや依存ファイルをこのプロジェクトに同梱していません。

### broker・GUI・Tunnel で1個の TOML を共有

broker の `backend`・`port` 等と、非秘密の Tunnel 設定を同じファイルに置きます。
GUI とランチャーにも、この broker ファイルを指定します。接続先 URL は同じファイルの
loopback host・port から導出し、`[tunnel]` に別の URL や `broker_config` は持たせません。

```toml
backend = "mock"
host = "127.0.0.1"
port = 8765
state_dir = ".state/tunnel-mock"
allow_mock_execution = false

[tunnel]
ingress_enabled = false # 既定は無効。専用入口を使う場合だけ明示的に true
tunnel_id = "tunnel_00000000000000000000000000000000" # 接続前に既存 ID に置換
client_exe = 'C:\Tools\tunnel-client-v0.0.15\tunnel-client.exe'
client_archive = 'C:\Downloads\tunnel-client-v0.0.15-windows-amd64.zip'
health_port = 8766
```

相対パスはすべてプロジェクトフォルダー基準です。`[tunnel]` は省略可能で、ID 未入力や
上記ゼロ ID のテンプレートでも mock broker を起動できます。接続時には完全な metadata と
実際の既存 ID が必要です。設定の読込・保存・GUI 起動だけで Tunnel を接続しません。
API key、MCP/client key、bearer、autoconnect 等の追加フィールドは拒否します。

旧 `tunnel.local.toml` も読めます。旧ファイルの `broker_config` が同じ broker ファイルを
指している場合だけ、canonical `[tunnel]` の省略フィールドを補完します。両方に明示した
ID・exe・ZIP・health port が異なる場合は固定エラー `tunnel_config_conflict` で停止し、
どちらかを黙って採用しません。利用者が対象ファイルを確認して一致させてください。
`ingress_enabled` は canonical broker TOML が決定します。省略時は `false` です。
旧ファイルで省略した場合は canonical の値を保持し、旧ファイルにも明示した場合は
canonical の実効値と一致する必要があります。旧ファイルだけで専用入口を有効にはできません。
自動移行・旧ファイルの削除はしません。旧形式の例は `examples/tunnel.legacy.example.toml` です。

GUI の明示的な metadata 保存は切断中だけ行います。選択した broker TOML の `[tunnel]`
だけを意味上変更し、broker/serial の値は保ちます。TOML のコメント・書式は再生成されます。
旧ファイルとの競合、未知の設定、symlink、保存中の変更がある場合は保存を拒否します。
秘密キーはこの保存対象に入りません。

### 転送ヘッダー付きの Tunnel 専用入口を明示的に有効にする

既定の `ingress_enabled = false` では従来どおり `/mcp` だけを使用します。
今回の専用入口を使う場合は、実際に選んだ `broker.local.toml` 等の同じ `[tunnel]` に
次の値を手動で設定します。既存の表を編集し、`[tunnel]` を二重に追加しないでください。

```toml
[tunnel]
ingress_enabled = true
# tunnel_id / client_exe / client_archive / health_port は既存値を保持
```

設定は TOML の真偽値だけを受け付けます。文字列 `"true"` や数値 `1` は拒否します。
読み込み・保存だけで接続せず、新しい認証キー・ポート・broker・proxy は作りません。
GUI はこの値を読み取り専用で表示し、metadata 保存でも既存値を保持します。

mock で切り替える手順:

1. 所有 Tunnel を Disconnect し、mock broker に実行中のジョブ・所有者がないことを確認します
2. 選んだ broker TOML を手動で編集し、`[tunnel] ingress_enabled = true` にします
3. その idle broker を元のコンソールで停止します。GUI を閉じるだけでは broker は停止しません
4. 同じ TOML で認証付き mock broker と GUI を起動し直します

```powershell
& .\Start-CardputerGui.ps1 -Config '.\broker.local.toml'
```

5. Tunnel パネルで同じ TOML を読み込み直し、Connect を明示的に実行します

GUI・ローカル CLI・接続前の broker 確認は引き続き `http://127.0.0.1:8765/mcp` を使います。
公式 client の MCP 接続先だけが `http://127.0.0.1:8765/tunnel/mcp` になります
（port は同じ broker TOML の値）。別の URL を設定するフィールドはありません。
通常入口 `/mcp` とローカル管理入口の転送ヘッダー拒否は維持します。

接続前に `client_identity` の `tunnel_ingress_enabled` と選択中の値を照合します。
古い broker、設定変更後に再起動していない broker、不明な値は
`tunnel_broker_ingress_mismatch` で拒否し、公式 client を起動しません。
GUI の Manual restart は Tunnel client だけを再起動するため、この設定の反映には不十分です。
無効へ戻すときも `false` に変更して idle broker を再起動・TOML 再読込してください。

専用入口では、有効な `remote_mcp` 認証を確認してから、既知の転送 metadata
`Forwarded`、`X-Forwarded-For`、`X-Forwarded-Host`、`X-Forwarded-Proto`、
`X-Forwarded-Port`、`X-Forwarded-Client-Cert` だけを除去し、同じ MCP アプリへ渡します。
`X-Forwarded-Client-Cert` の値は証明書の検証や本人確認には使いません。
通常のリモート認証キーを確認した後、他の転送 metadata と同じく破棄します。
これらの値から接続元の身元や権限を判断しません。未知の `X-Forwarded-*` 等は引き続き拒否し、
Host・Origin・loopback peer・重複ヘッダー・JSON の検証も維持します。
local_cli 用キーは専用入口と接続前確認で拒否します。
認証後の scopes、共有 broker の順番待ち・session・lease・実機の明示executeは変わりません。

これは承認されたソース変更とローカル試験の範囲です。実際の Windows 起動、対象アプリからの
Tunnel 接続、アカウント権限と転送ヘッダーの組合せは利用者の環境で別途確認が必要です。
未知ヘッダーの拒否が続く場合は実際の拒否理由を確認し、無条件にヘッダーを除去しないでください。

## 3. 明示的に接続するとき

mock のまま、既存の idle broker を停止してから実行します。

```powershell
& .\Start-CardputerTunnel.ps1 -Config '.\broker.local.toml' -Connect
```

ランチャーがローカルで runtime API key を非表示入力します。キーは引数・設定・ファイル・
ログに保存せず、専用子プロセスの環境に渡します。入力やプロセスメモリーは信頼できる
同一 OS ユーザー・管理者から完全には隔離できません。PowerShell の transcript/画面録画を使わないでください。
終端後、専用コンソールのキー環境変数を消去します。元のウィンドウへ API キーを保存しません。

公式 client の stdout/stderr は安全のため表示・保存しません。固定診断コードだけを表示します。
ただし公式 client 自身には、loopback admin UI のメモリー内ログがあります。組込みの秘匿化は
ベストエフォートであり、エクスポートを無条件に公開しないでください。health/admin ポートは
127.0.0.1 のみ。外部公開・Tunnel の別チャンネル・汎用 HTTP プロキシとして設定しません。

「local readiness」は OpenAI への認証・poll 成功を意味しません。実際の対象アプリで tools/list と
読み取り専用 device_status を成功させるまで、クラウド接続確認済みとは扱いません。
この dot での利用可否・ユーザーアカウントの権限・実際のヘッダー転送は未検証です。

## 4. Cardputer 用の発行済みキーを使う場合

[KEYS.md](KEYS.md) のローカル管理機能で発行済みのキーだけを入力します。
既存の一時 bearer と互換性があり、自動移行や自動発行はしません。

```powershell
& .\Start-CardputerTunnel.ps1 -Config '.\broker.local.toml' -Connect -KeyRegistry '.state\keys.sqlite3' -UseClientKey
```

API key と Cardputer client key は別のものです。前者は OpenAI Tunnel の実行、後者は broker の
権限確認に使います。既定の read キーは状態・ログ・履歴の読み取りだけです。

通常、Tunnel に設定した1個の静的 bearer を複数の遠隔端末が共有すると、broker には1つの
認証主体として見えます。物理端末を識別できません。公式 client は転送 Authorization を最後に適用するため、
別々の有効なキーを実際に転送できれば区別できますが、ホスト側アプリの対応は未確認です。
不正な置換ヘッダーは拒否します。ローカル操作用 token を公式 client に渡しません。

## 実機への切替

mock とクラウド側の読み取り確認後にのみ検討してください。既存の private device.local.toml に
非秘密の `[tunnel]` を置き、同じファイルを `-Config` に指定し、明示的に `-EnableSerial` を指定します。
それだけでは USB を開きません。実行には認証・control 権限・session・ローカルの正確な
正確なplan_hashの明示executeが必要です。既存 needs_review を消去したり、失敗した HELLO を再実行したりしません。
現在の実機通信の未解決診断は [DIAGNOSTICS-UPDATE.md](DIAGNOSTICS-UPDATE.md) を参照してください。

## テストの意味

現行 mcp 2.2 の実 HTTP、2026-07-28 と3種類の旧 initialize、認証ヘッダーの優先順位、
共有 broker の排他、認証、mock subprocess を検証しています。
これは公式 client の実行、クラウド Tunnel、実機 USB の成功証明ではありません。

[固定バージョンの設定仕様](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/configuration.md)
/ [ヘッダー適用実装](https://github.com/openai/tunnel-client/blob/v0.0.15/pkg/mcpclient/fxmodule.go)
/ [readiness 実装](https://github.com/openai/tunnel-client/blob/v0.0.15/pkg/runtimehealth/health.go)

### broker の device_status と Tunnel の死活表示

`device_status.tunnel` は `external_lifecycle_unknown` です。broker は外部の
Tunnel client / クラウドの接続状態を管理していないため、未設定や接続成功とは断定しません。
`tunnel_ingress_enabled` はその broker の専用入口の設定値だけを示します。
接続状態は GUI の owned-client 表示と、実際のクラウド側での読み取り確認で別々に確認します。

## Runtime API key を環境変数から渡す

明示入力 → `CARDPUTER_TUNNEL_API_KEY` → `CONTROL_PLANE_API_KEY` の順で選びます。
GUI の API key 欄は空欄のまま Connect を押すと環境変数を使用します。
PowerShell ランチャーは `-Connect` 時だけ環境変数を読み、両方空なら従来の
SecureString 入力に戻ります。`-PromptApiKey` は環境変数より明示入力を優先します。
空白だけの値はキーとして無効です。`OPENAI_API_KEY` / admin key は使用しません。
環境変数は起動元プロセスに利用者が設定してください。ここでは実キーを設定しません。

既存の child environment allowlist を維持し、選んだ値を Tunnel 用子プロセスの
`CARDPUTER_TUNNEL_API_KEY` にだけ渡します。native client は既存の
`--control-plane.api-key=env:CARDPUTER_TUNNEL_API_KEY` で読みます。
TOML、SQLite、引数、ログへ保存しません。GUI 欄には環境変数の値を表示しません。
接続や再接続は利用者の明示操作が必要です。
[公式 v0.0.15 設定仕様](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/configuration.md)
には native 変数 `CONTROL_PLANE_API_KEY` と env 参照が記載されています。

## private broker TOML の保存先

`%LOCALAPPDATA%\esp-device-broker-mcp\device.local.toml` を推奨します。
clone や OneDrive の外で保持でき、更新時にもコピーを増やさずに済みます。
自動移動・作成はしません。API key はこのファイルに書かないでください。
既存ランチャーは絶対 `-Config`、CLI は絶対 `--config` を受け付けます。
既定の探索先は clone 内のままなので、外部ファイルは毎回明示してください。

```powershell
cd C:\app\mcp\esp-device-broker-mcp
$config = Join-Path $env:LOCALAPPDATA 'esp-device-broker-mcp\device.local.toml'
# mock 設定を利用者が用意した後（backend = "mock"）:
.\Start-CardputerGui.ps1 -Config $config
# CLI GUI も同じファイルを参照:
.\.venv\Scripts\python.exe -m cardputer_mcp.cli --auth-source environment gui --config $config
```

`state_dir` は TOML の親ではなくプロセスの cwd 基準です。ランチャーの broker cwd は
clone になります。外部設定では `state_dir` を AppData 内の絶対パスにしてください
（TOML の single-quoted literal string で Windows パスを記述できます）。
Tunnel の相対 client_exe/client_archive は project 基準です。外部保管時は絶対パスが明確です。
実機 serial 設定を使う場合だけ GUI に `-EnableSerial` を追加します。
mock 検証に serial 設定を流用しないでください。

ローカル Codex は [STDIO-CLIENTS.md](STDIO-CLIENTS.md) の stdio adapter で
同じ broker に接続できます。API key / Tunnel はローカル stdio 接続に不要です。
`examples/codex-project-config.toml` の Python と cwd をこの clone に合わせ、
private env ファイルは AppData に置きます。発行済み local client key と registry が必要です。
初回は mock で client_identity/device_status/read_logs を確認し、作業セッションと
FIFO を試せます。実機 flash、USB、IDF ビルドの検証にはなりません。
