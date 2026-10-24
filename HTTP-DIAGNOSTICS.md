# HTTP 接続拒否の診断（v0.5.1 以降）

v0.5 向けの診断追加パッチです。依存パッケージの変更はありません。
配布 ZIP 内の `cardputer-device-mcp` の内容を、既存の同名プロジェクトに重ねて更新します。
下記の安全な停止条件を確認してから更新し、元の private TOML と `.state` は残します。
更新後にプロジェクト内で `uv sync --frozen` を実行し、従来の
`Start-CardputerGui.ps1` で起動してください。v0.5.2 の専用入口については
[TUNNEL-WINDOWS.md](TUNNEL-WINDOWS.md) を参照してください。

GUI の **HTTP diagnostics** と `http-diagnostics` コマンドは、起動中の同じ broker が
HTTP リクエストをどの段階で拒否したかを、固定の理由コードと回数で表示します。
追加の診断では、固定カテゴリによる入口・拒否段階と、既知の転送・重複ヘッダー名も
表示します。ヘッダーの値は記録・表示しません。
broker・キーの作成、設定や `.state` の読み書き、MCP ツールの呼び出し、USB 操作はしません。
認証、loopback、Host、Origin、転送ヘッダーの既存チェックは維持します。

## 更新前に残すもの

このパッチを既存プロジェクトへ適用するときは、既存の private TOML、`[tunnel]` 設定、
`.state` 全体、履歴、承認、キーデータベースをそのまま残してください。
ディレクトリーの初期化、設定のサンプルへの置換、DB や lock ファイルの削除は不要です。
`needs_review` や状態不明のジョブを解除・再実行して診断しないでください。

更新済みコードを読み込むための broker 再起動は、所有状況を確認し、ジョブや実機操作が
動いておらず安全に停止できると分かった場合だけ行います。状態が不明なら停止せず確認を
続けてください。停止はその broker のコンソールから行い、強制終了や二重起動は避けます。
今回の確認は既存の **mock 設定** で行い、実機モードへ切り替えません。

## GUI で比較する（推奨）

1. 更新済み broker を既存の mock 設定と、従来どおりの認証付き起動方法で起動します。
   新しい設定、キー、Tunnel、アプリを診断のために作る必要はありません。
2. GUI 上部の **HTTP diagnostics** を押し、比較前の回数を控えます。ダイアログを閉じます。
3. 利用者が対象の既存コネクターで、mock のツール検出を **1回だけ** 明示的に試します。
4. broker を再起動せず、**HTTP diagnostics** をもう一度押して、増えたカウンターを比較します。

GUI や Tunnel を閉じる必要はありません。GUI が既に持っているローカル認証と通信ワーカーを
使います。追加のキー入力はなく、診断の取得後も通常どおりキューの状態を更新します。
読み取り中はボタンを無効にし、GUI を閉じた後に遅れて届いた結果は表示しません。
ダイアログは読み取り専用で、raw レスポンスではなく既知の項目だけを表示します。
画面にはゼロ以外のカウンター、最後の拒否の詳細、直近最大4件を新しい順に表示します。
CLI の JSON にはゼロのカウンターもすべて含まれます。古い broker から取得した場合も
カウンターは表示でき、追加の詳細は `Request details unavailable (older broker).` と表示します。

## CLI で JSON を取得する場合

起動元の PowerShell が使用できる場合は、同じ情報を JSON で取得できます。
比較前後で broker を再起動せず、次の同じコマンドを使ってください。

```powershell
uv run cardputer --auth-source environment http-diagnostics
```

既定の port は 8765 です。既存 broker が別 port の場合は、その既知の値を指定します。
次の 9000 は例です。`--url` と `--auth-source` はコマンド名より前に置きます。

```powershell
uv run cardputer --url http://127.0.0.1:9000/mcp --auth-source environment http-diagnostics
```

URL は `http://127.0.0.1:PORT`、末尾 `/`、または `/mcp` のみです。
このコマンドは `--config` を読みません。公開 Tunnel URL は指定しません。

### CLI の認証環境

GUI と同じ、その broker 起動時の `CARDPUTER_LOCAL_TOKEN` を継承したプロセス環境が必要です。
ランチャーを実行した元の PowerShell を使ってください。別に開いた PowerShell や
二重クリックで起動した子プロセスから、元の環境へ認証情報は戻りません。
キーを引数・設定・貼り付けたスクリプトへ入れたり、環境変数の値を表示したりしないでください。

`Start-CardputerGui.ps1` が GUI の終了を待っていて入力できない場合は、上記の GUI ボタンを
使ってください。診断のために GUI や Tunnel を閉じたり、キーを別のコンソールへ移したりする
必要はありません。

## 出力の読み方

- `since: "broker_start"` は **現在の broker プロセス起動後だけ** の集計です。
  再起動でカウンターはゼロになり、以前の拒否は復元されません。診断用に DB を消す必要はありません。
- `counts` は固定の理由コードごとの回数です。未発生の理由もゼロで含まれます。
- `total_denials` は記録した HTTP 拒否の合計です。カウンターは有限の整数上限で止まります。
- `schema_version: 1` は従来と同じです。追加の詳細が使える broker は
  `context_version: 1` も返します。
- `last_denial` は最後の1件の理由、層、ルート区分、HTTP status と、取得できた場合の
  `context` です。拒否がなければ `null` です。詳細がない場合は「利用できない」と表示します。
- `recent_denials` は詳細を記録できた拒否の直近最大16件で、JSON では古い順です。
  各項目の `sequence` はこの broker プロセスでの拒否の通し番号です。再起動で消えます。
  永続的な監査ログではなく、利用者やコネクターを識別する情報も含みません。
- `route` は `mcp` / `operator` / `other` の区分だけで、URL やパスは保存しません。
  `/mcp` と専用入口 `/tunnel/mcp` は、どちらも論理ルート `mcp` になります。
  実際にどちらの入口へ届いたかは、下記の `context.entry` で区別します。

### 入口と拒否段階（context）

`context.entry` は `direct_mcp`（通常入口）、`tunnel_ingress`（専用入口）、
`operator`（ローカル操作 API）、`other` のいずれかです。
これは受信した物理的な入口の区分で、外部サービスや認証主体の識別ではありません。

`context.stage` は、**最初に拒否した段階だけ**を示します。
1件のリクエストについて後続のチェックまで評価して、すべての不一致を集める機能ではありません。

- `direct_guard`: 通常入口側の外側の guard
- `ingress_guard`: 専用入口でのパス・接続元・ヘッダーなどの guard
- `ingress_auth`: 専用入口での認証・権限チェック
- `post_ingress_guard`: 専用入口で検証・転送情報除去後、内側の guard
- `post_ingress_auth`: 専用入口の内側の認証
- `mcp_handler`: MCP 処理側の HTTP エラー
- `operator_handler`: ローカル操作 API の HTTP エラー
- `other_handler`: その他の処理側の HTTP エラー

たとえば `forwarding_header_present` と `entry: tunnel_ingress` /
`stage: ingress_guard` なら専用入口側で、`stage: post_ingress_guard` ならその内側で
拒否したことを示します。`entry: direct_mcp` なら通常入口に届いています。
この区分だけで外部 Tunnel の設定や動作が正しいと断定はできません。

`method` は `GET` / `POST` / `DELETE` / `HEAD` / `OPTIONS` / `PUT` / `PATCH` /
`other` だけです。`content_type` は `application_json`、`application_json_variant`、
`missing`、`text_plain`、`form_urlencoded`、`multipart_form_data`、`octet_stream`、
`other`、`ambiguous` のいずれかで、パラメーターなどの実際の値は保存しません。
`application_json_variant` は大小文字や空白がある JSON 表記の区分、`ambiguous` は
複数の Content-Type ヘッダーを示します。これらの区分は受理条件を変更しません。

### 転送・重複ヘッダーの名前

`forwarding_headers` は、記録した拒否段階で存在した転送ヘッダーの固定名リストです。
`post_ingress_*` や MCP 処理側の段階では、許可した metadata は既に除去されています。
その段階の一覧が空でも、最初の受信時に転送ヘッダーがなかったとは限りません。
拒否の原因と断定した名前の一覧ではありません。表示できる名前は次に限定します。

- `forwarded`、`x-forwarded-for`、`x-forwarded-host`、`x-forwarded-proto`、`x-forwarded-port`
- `x-forwarded-prefix`、`x-forwarded-path`、`x-forwarded-uri`、`x-forwarded-server`
- `x-forwarded-scheme`、`x-forwarded-protocol`、`x-forwarded-ssl`
- `x-forwarded-client-cert`、`x-forwarded-client-ip`、`x-forwarded-address`、`x-forwarded-by`
- `x-forwarded-script-name`、`other_forwarding`

未登録の `x-forwarded-*` 名は、実際の名前を出さず `other_forwarding` にまとめます。
`duplicate_headers` は重複があった固定名のリストで、上記の既知名に加えて
`authorization`、`host`、`origin`、`content-type`、`other` だけを表示します。
未登録の重複名は `other` です。空のリストはその種類が観測されなかったことを示します。
どちらの一覧にも値は含まれません。診断を追加しても、許可ヘッダー、認証、アクセス権、
転送や重複の拒否条件は変わりません。

### 理由コード

理由コードの意味:

- `non_ascii_headers`: HTTP ヘッダーを既存の ASCII 制約で拒否
- `duplicate_headers`: 重複した HTTP ヘッダーを拒否
- `non_loopback_peer`: loopback 以外の接続元を拒否
- `host_not_allowed`: 許可していない Host を拒否
- `origin_not_allowed`: 許可していない Origin を拒否
- `forwarding_header_present`: 禁止されている転送ヘッダーを拒否
- `content_type_not_json`: POST の Content-Type が JSON 条件を満たさない
- `authentication_required`: broker の認証段階で拒否
- `tunnel_route_rejected`: 専用入口で非標準のパス表現、query、mount を拒否
- `tunnel_principal_rejected`: 専用入口で remote_mcp と read 権限の条件を満たさず拒否
- `mcp_http_rejection`: 外側の guard と認証を通過後、MCP 処理側が HTTP エラーを返した
- `operator_http_rejection`: operator API 側が HTTP エラーを返した
- `other_http_rejection`: その他のルートが HTTP エラーを返した

`mcp_http_rejection` の具体的な HTTP status は `last_denial.http_status` を確認します。
Origin や転送ヘッダーの重複を示しても、チェックを無効化する理由にはなりません。
まずどの層が拒否したかを確認するための情報です。

同時に届いた別のリクエストも集計されるため、増分だけで特定のコネクターや1件の試行との
対応を断定できません。1回の検出操作が複数リクエストになる場合もあります。
すべてゼロのままでも接続成功の証明ではありません。リクエストが broker に届かなかった、
別のプロセスを参照した、または HTTP 拒否以外の問題だった可能性もあります。
MCP 本文内のツールエラー、Tunnel の poll 成否、クラウド側の失敗はこの集計の対象外です。

出力には raw ヘッダー・本文・URL・IP・キー・認証主体 ID・ジョブ ID・例外文字列を含めません。
比較前後のダイアログの値、またはこの JSON だけを診断に使い、環境変数一覧や raw ログを
貼り付ける必要はありません。

## コマンド自体が失敗した場合

- `http_diagnostics_requires_local_environment_credentials`: `--auth-source environment` と
  ローカル identity が必要です。`--client remote_mcp`、file、key-environment は使えません。
- `runtime_environment_credentials_unavailable`: 元の起動 PowerShell を確認してください。
  broker の再起動前の環境や、別の PowerShell では現在の local token を継承していない場合があります。
- `Local operator authentication failed`: 現在の broker 起動時の環境か、指定した port が正しいかを確認します。
- `Cannot reach the local broker`: 既存 broker の稼働と port を確認します。
- `The broker rejected the operator request`: 更新前の broker が動いたままの場合などに発生します。
  更新済みコードの反映には、上記の安全な停止条件を満たしてから再起動が必要です。

コマンドは失敗時に終了コード 1 と固定メッセージを返し、受信した本文や秘密の値を表示しません。
