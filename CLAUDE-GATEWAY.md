# Claude gateway の OAuth 連携手順

claude.ai のカスタムコネクタからこの broker を使うための初回設定手順です。
`cardputer gateway` は次の 2 つを 1 つのプロセスで行います。

- OAuth 2.1 の認可サーバー。ログインできるのは、指定した Google アカウント 1 つだけです。
- 認証済みの MCP 要求を broker の `/tunnel/mcp` へ中継すること。

インターネットからの入口は Tailscale Funnel です。
仕組みと制限の要約は [README.md](README.md#claude-gateway-tailscale-funnel-optional) にあります。

```
claude.ai ──HTTPS──▶ Tailscale Funnel (<machine>.<tailnet>.ts.net:443)
                         │
                         ▼
             cardputer gateway (127.0.0.1:8770)
               ├─ /.well-known/*, /authorize, /token, /register, /revoke
               ├─ /oauth/google/callback ◀── Google ログイン
               └─ /mcp ──(remote_mcp キー)──▶ broker 127.0.0.1:<port>/tunnel/mcp
```

`/operator/*` やローカル管理入口は外へ出ません。
broker・OpenAI Tunnel・デバイス操作を gateway が起動・停止することもありません。

## 0. 前提

- broker が canonical な broker TOML（例: `broker.local.toml`）で動いていること
- この PC に Tailscale が入っていて、tailnet にログイン済みであること
- Google アカウントと、Google Cloud のプロジェクトを 1 つ作れること
- claude.ai でカスタムコネクタを追加できるアカウントであること（Team / Enterprise では組織の管理者が許可している必要があります）

以下では、例として次の値を使います。実際の値に置き換えてください。

| 例 | 意味 |
| --- | --- |
| `https://<machine>.<tailnet>.ts.net` | この PC の Tailscale 上の名前（`public_url`） |
| `C:\Users\YOUR_NAME\AppData\Local\DeviceBroker\` | 秘密ファイルの置き場所 |
| `8765` | broker の port |
| `8770` | gateway の port（既定値） |

この PC の Tailscale 名は秘密ではありませんが、公開の場（Issue・PR など）には書かないでください。

## 1. Tailscale の準備

[Tailscale 管理画面](https://login.tailscale.com/admin)で次を確認します。

1. **DNS** で **MagicDNS** と **HTTPS Certificates** が有効になっていること
2. **Access controls** の `nodeAttrs` で、この PC に `funnel` 属性が付いていること
   （既定のポリシーでは `autogroup:member` に付いています）

この PC で名前を確認します。

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' status --json | Select-String DNSName
```

`Self` の `DNSName` から末尾の `.` を除き、`https://` を付けたものが `public_url` です（小文字のみ）。

HTTPS 443 にほかの `tailscale serve` / `funnel` の設定があると、gateway は起動を拒否します
（`tailscale_https_443_already_configured`）。他人が作った設定を上書きしないためです。
次のコマンドの結果が `{}` であることを確認してください。

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' funnel status --json
```

## 2. Google Cloud で OAuth クライアントを作る

gateway は Google に `openid` スコープだけを求めます。
受け取るのはアカウントの識別子（`sub`）だけで、メールアドレスやプロフィールは受け取りません。

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作るか選びます。
2. **Google Auth Platform**（旧「OAuth 同意画面」）を開き、初期設定をします。
   - **Audience（対象）**: `External`（外部）を選び、公開ステータスは `Testing`（テスト）のままにします。
   - **Test users（テストユーザー）**: 自分の Google アカウントを追加します。
   - **Data access（データアクセス）**: スコープの追加は不要です（`openid` は審査の対象外です）。
3. **Clients（クライアント）** → **Create client（クライアントを作成）** に進みます。
   - **Application type**: `Web application`
   - **Authorized redirect URIs**: `https://<machine>.<tailnet>.ts.net/oauth/google/callback`
     - 末尾にスラッシュは付けません。大文字・小文字も `public_url` と同じにします。
   - **Authorized JavaScript origins**: 空欄のままでかまいません。
4. 作成直後に表示される **Client ID** と **Client secret** を控えます。
   - Client secret は作成時にしか全文が表示されないことがあります。見失ったら新しいシークレットを追加し、古いものは無効にします。
   - Client ID は `….apps.googleusercontent.com` の形です。秘密ではありません。

### 自分の Google `sub` を調べる

`sub` は Google アカウントごとに変わらない数字の ID です（メールアドレスではありません）。
gateway はログインした人の `sub` がこの値と一致する場合だけ許可します。

1. [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/) を開きます。
2. 左の入力欄に `openid` と入力して **Authorize APIs** を押し、自分のアカウントでログインします。
3. **Exchange authorization code for tokens** を押します。
4. **Request URI** に `https://openidconnect.googleapis.com/v1/userinfo` を入れて **Send the request** を押します。
5. 応答の `"sub": "…"` の数字が `allowed_google_sub` に入れる値です。

終わったら、[Google アカウントのサードパーティ接続](https://myaccount.google.com/connections)から
OAuth 2.0 Playground のアクセスを削除してかまいません。

## 3. 秘密ファイルを 2 つ用意する

どちらのファイルも、次の場所に置くと読み込みを拒否されます。

- リポジトリ（`.git` のあるフォルダー）の中
- `cardputer.exe` のインストール先の中

stdio adapter の env ファイルと同じ規則です（[STDIO-CLIENTS.md](STDIO-CLIENTS.md)）。
加えて、次のファイルは使えません。

- シンボリックリンク、ハードリンクが 2 本以上あるファイル
- 8 KiB を超えるファイル
- 読み取り専用属性が付いたファイル（`keys --write-env` が書き換えられません）

置き場所は、自分だけが読めるフォルダー（例: `%LOCALAPPDATA%\DeviceBroker\`）にしてください。

### 3a. broker へ中継するためのキー（`gateway-broker.env`）

まず、プレースホルダーのキーを入れたファイルを作ります。
URL の port は broker TOML の `port` と同じにします（違うと `gateway_broker_port_mismatch`）。

```dotenv
CARDPUTER_BROKER_URL=http://127.0.0.1:8765/mcp
CARDPUTER_CLIENT_KEY=REPLACE_WITH_ISSUED_KEY
```

次に、`remote_mcp` のキーを発行してこのファイルへ直接書き込みます。キーは画面に表示されません。

```powershell
.\cardputer.exe keys issue --origin remote_mcp --label claude-gateway `
  --write-env 'C:\Users\YOUR_NAME\AppData\Local\DeviceBroker\gateway-broker.env'
```

- 既定では読み取り（`read`）だけを許可します。Claude にデバイスのセッション・計画・実行もさせる場合だけ、`--control` を付けます。
- キーには有効期限（最長 30 日）があります。期限の前に `keys rotate --key-id <ID> --write-env <同じファイル>` で更新してください。
  動いている gateway は、broker に古いキーを拒否されると、このファイルを 1 回だけ読み直して再送します。
- 詳しくは [KEYS.md](KEYS.md) を見てください。

### 3b. Google のクライアントシークレット（`gateway-google.env`）

中身は次の 1 行だけにします。値に引用符は付けません。

```dotenv
CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET=GOCSPX-REPLACE_WITH_SECRET
```

## 4. broker TOML を設定する

broker が使っている TOML に、次の 2 か所を設定します。

```toml
[tunnel]
ingress_enabled = true
# tunnel_id / client_exe / client_archive / health_port は既存の値をそのまま残す

[gateway]
public_url = "https://<machine>.<tailnet>.ts.net"
port = 8770
google_client_id = "<id>.apps.googleusercontent.com"
allowed_google_sub = "<手順 2 で調べた sub>"
broker_env_file = 'C:\Users\YOUR_NAME\AppData\Local\DeviceBroker\gateway-broker.env'
google_client_secret_file = 'C:\Users\YOUR_NAME\AppData\Local\DeviceBroker\gateway-google.env'
funnel = false
# tailscale_exe = 'C:\Program Files\Tailscale\tailscale.exe'   # 既定値と違う場所のときだけ
```

- `[tunnel]` が既にある場合は、表を二重に作らずに既存の表を編集します。
- `ingress_enabled = true` は broker を再起動して初めて反映されます。
  OpenAI Tunnel を使っている場合は、その接続先も `/tunnel/mcp` に変わります。
  切り替え手順は [TUNNEL-WINDOWS.md](TUNNEL-WINDOWS.md) を見てください。
- `port` は broker の `port` とも、Tunnel の `health_port` とも違う値にします（同じだと `gateway_port_conflict`）。
- 2 つのファイルのパスは絶対パスで書きます。パスに `\` があるので、TOML では単引用符 `'…'` を使います。
- `funnel` は `true` / `false` の真偽値だけを受け付けます。

## 5. まず Funnel なしで起動して確かめる

broker を起動した状態で、`funnel = false` のまま gateway を起動します。

```powershell
.\cardputer.exe gateway --config '.\broker.local.toml'
```

`gateway_listening 127.0.0.1:8770` に続いて `gateway_funnel_disabled; listening on loopback only` が出れば、
設定ファイルと秘密ファイルの読み込みは成功しています。別のターミナルから次を確認します。

```powershell
# 401 と、resource_metadata を含む WWW-Authenticate ヘッダーが返れば正常
curl.exe -si -X POST http://127.0.0.1:8770/mcp -H 'content-type: application/json' -d '{}'
# resource が public_url + "/mcp" と完全一致すること
curl.exe -s http://127.0.0.1:8770/.well-known/oauth-protected-resource/mcp
```

Ctrl+C で停止します。

## 6. Funnel を有効にして Claude から接続する

> `funnel = true` にすると、gateway が動いている間、この PC の 443 番がインターネットに公開されます。
> OAuth を通らない要求は `/mcp` で 401 になります。それでも、OAuth のエンドポイントには誰でも到達できます。

1. TOML の `funnel = true` にして、gateway を起動し直します。
2. `gateway_funnel_open https://<machine>.<tailnet>.ts.net` が出るまで待ちます（最長 15 秒）。
3. claude.ai で **Settings → Connectors → Add custom connector** を開きます。
   - **Name**: 任意（例: `Cardputer`）
   - **Remote MCP server URL**: `https://<machine>.<tailnet>.ts.net/mcp`
   - **Advanced settings** の OAuth Client ID / Secret は空欄のままにします（Claude が自動で登録します）。
4. **Connect** を押すと Google のログイン画面が出るので、`allowed_google_sub` のアカウントでログインします。
   - 「このアプリは Google で確認されていません」と出たら、**続行** を押します（テストモードでは正常な表示です）。
5. Claude に戻って接続済みになれば完了です。新しい会話でツールが見えることを確認してください。

gateway の標準エラーには、次のような固定のイベント名だけが出ます。

- `gateway_client_registered`
- `gateway_login_accepted`
- `gateway_token_issued`

トークン・コード・シークレットは出力しません。

## まとめて起動する

設定が済んだら、`Start-CardputerStack.ps1` で次の 4 つを順に起動できます。

1. broker
2. gateway（別のコンソール）
3. 読み取り専用の Web UI（別のコンソール、[WEB-UI.md](WEB-UI.md)）
4. GUI

引数は `Start-CardputerGui.ps1` と同じです。
いつもの PowerShell から `&` で実行します。ダブルクリックや `-File` で実行すると、broker のトークンがその窓に残りません。

```powershell
& 'C:\path\to\Start-CardputerStack.ps1' -Exe 'C:\path\to\cardputer.exe' -Profile 'C:\path\to\device.local.toml'
```

- gateway と Web UI のコンソールには、`CARDPUTER_*` と `OPENAI_*` の環境変数を渡しません。どちらも TOML に書いた鍵ファイルだけを読みます。
- TOML に `[gateway]` や `[webui]` がない場合、そのコンソールは `gateway_config_missing` / `webui_config_missing` を出して終わります。ほかはそのまま動きます。
- GUI を閉じても、broker・gateway・Web UI は止まりません。それぞれのコンソールで止めてください。

自分の PC のパスを書いた短いラッパーをリポジトリの外（ホームディレクトリなど）に置いておくと、毎回パスを打たずに済みます。

```powershell
# 例: $HOME\Start-Cardputer-Mine.ps1（リポジトリには入れない）
& 'C:\path\to\repo\Start-CardputerStack.ps1' `
  -Exe "$env:LOCALAPPDATA\esp-device-broker-mcp\versions\<version>\cardputer.exe" `
  -Profile "$env:LOCALAPPDATA\esp-device-broker-mcp\device.local.toml"
```

## 停止と再起動

- 停止するときは、gateway のターミナルで Ctrl+C を押します。Funnel を先に閉じてから待ち受けを止めます（`gateway_funnel_closed` → `gateway_stopped`）。
- トークンはメモリにしか持っていません。gateway を再起動すると、Claude 側で再接続（再ログイン）が必要です。
- Funnel のプロセスが終了すると、gateway も止まります（`gateway_funnel_exited; stopping the gateway`）。
- 異常終了したあとは、`tailscale funnel status --json` が `{}` に戻っていることを確認してください。
  強制終了で Funnel の設定が必ず消えるかどうかは、まだ確かめていません。

## エラー一覧

起動時のエラーは、固定のコードを 1 行出して終了コード 1 で終わります。

| コード | 原因と対処 |
| --- | --- |
| `gateway_config_invalid` | TOML を読めない、または値が不正です。`public_url` の形式（https・小文字・末尾 `/` なし）とパス（絶対パス）を確認してください |
| `gateway_config_missing` | `[gateway]` 節がありません |
| `gateway_requires_tunnel_ingress` | `[tunnel] ingress_enabled = true` になっていません |
| `gateway_port_conflict` | gateway の `port` が broker の `port` か `health_port` と同じです |
| `gateway_broker_env_invalid` | `broker_env_file` を読めません（置き場所、書式、プレースホルダーのままのキー） |
| `gateway_broker_port_mismatch` | env ファイルの URL の port が broker の `port` と違います |
| `gateway_google_secret_invalid` | `google_client_secret_file` を読めません（置き場所、キー名、値の形式） |
| `gateway_already_running` | gateway がすでに動いています |
| `gateway_listen_failed` | `127.0.0.1:<port>` で待ち受けできません（ほかのプロセスが使用中など） |
| `tailscale_executable_missing` | `tailscale_exe` が見つかりません |
| `tailscale_not_running` | Tailscale が接続されていません |
| `gateway_public_url_not_this_machine` | `public_url` がこの PC の Tailscale 名と違います |
| `tailscale_funnel_not_permitted` | この PC に `funnel` 属性が付いていません |
| `tailscale_https_443_already_configured` | HTTPS 443 にほかの serve/Funnel 設定があります |
| `tailscale_unavailable` / `tailscale_query_failed` / `tailscale_output_invalid` | `tailscale` コマンドの実行や出力の読み取りに失敗しました |
| `gateway_funnel_start_failed` / `gateway_funnel_exited` / `gateway_funnel_not_confirmed` | Funnel を開始できない、すぐに終了した、または 15 秒以内に確認できませんでした |

ログイン時に拒否された場合は、標準エラーに `gateway_login_rejected reason=<理由>` が出ます。

| 理由 | 意味 |
| --- | --- |
| `account_not_allowed` | ログインした Google アカウントの `sub` が `allowed_google_sub` と違います |
| `google_denied` | Google の画面でキャンセルされました |
| `unknown_state` | ログインの途中状態が見つかりません（期限切れ、再送、gateway の再起動） |

broker が中継を拒否した場合、Claude には 502 が返ります。

- `broker_key_rejected`: キーが失効・期限切れです。手順 3a のキーを更新してください。
- `broker_unavailable`: broker が動いていません。

## 確認済みと未確認

- 確認済み: 自動テスト（偽の Google と実際の broker 入口を使用）と、Windows パッケージのスモークテスト
- 未確認: 実際の Google ログイン、実際の Claude からの接続、強制終了時の Funnel 設定の後始末
