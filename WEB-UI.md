# 読み取り専用の Web UI

`cardputer webui` は、broker の状態・ジョブ一覧・ジョブのログ・ライブ更新を、ブラウザ（iPhone・iPad を含む）から見るための画面です。

- **見るだけです。** ジョブの取り消し・停止・確認などの操作はできません。
- **既定は tailnet の中だけです。** `tailscale serve` を使います。`funnel = true` を明示したときだけ、`tailscale funnel`（インターネット公開、8443 番）を使います。
- **どちらの場合も Google ログインが必要です。** 指定した Google アカウント（`sub`）だけが入れます。

```
自分の端末（tailnet 内）──HTTPS──▶ tailscale serve (<machine>.<tailnet>.ts.net:8443)
                                      │
                                      ▼
                        cardputer webui (127.0.0.1:8771)
                          ├─ /login, /oauth/google/callback ◀── Google ログイン
                          └─ /api/*（セッション必須）──(LOCAL の読み取り専用キー)──▶ broker /operator/*
```

gateway（Claude 用、Funnel の 443 番）とは別のプロセス・別のポートです。Web UI を Funnel にしても、gateway の Funnel の向き先は変わりません。

## 守っていること

- **ログインなしでは何も見えません。** ログイン開始と Google のコールバック以外のすべてのパス（ページ、JS・CSS、API、ライブ更新、存在しないパス）でセッションを確かめます。
  - 未ログインのとき、`/` はログインへ転送します。それ以外は `401 {"error":"authentication_required"}` だけを返します。
  - broker の状態、ジョブ、エラーの詳細、バージョンは返しません。
- **資格情報をブラウザに出しません。**
  - broker のキーは、broker への要求の `Authorization` ヘッダーにしか載りません。
  - Google のクライアントシークレットは、Google のトークン交換にしか使いません。
  - ブラウザへ返す応答は、Web UI が組み立てます。broker の応答ヘッダーは転送しません。
  - broker の応答やライブ更新の 1 イベントに、キーやシークレットの文字列が万一含まれていた場合は、送らずに `response_withheld` を返すか、ストリームを止めます。
  - ページ・JS・CSS には、設定値・環境変数・キーを埋め込みません。`Start-CardputerStack.ps1` は、Web UI を起動する前に `CARDPUTER_*` と `OPENAI_*` の環境変数を消します。
  - 例外の詳細（トレースバック）は応答に出しません。
- **セッション**
  - サーバー側のメモリに持ちます。cookie に入るのはランダムな値だけで、サーバーはその SHA-256 しか保存しません。
  - cookie は `__Host-` 付きで、HttpOnly、Secure、SameSite=Strict です。
  - 有効期限は 8 時間で、ログアウトするとその場で無効になります。Web UI を再起動すると、すべてのセッションが消えます。
- **CSRF**
  - POST（ログアウト、ログの読み取り）には、ページの CSRF トークンと、同じオリジンの `Origin` が必要です。
- **ブラウザ側の制限**
  - 応答には、すべて `Content-Security-Policy: default-src 'none'; script-src 'self'; ...` を付けます。インラインのスクリプトは使いません。
  - ほかに `X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store` を付けます。
  - JS は、broker の値を `textContent` で表示します（HTML として解釈しません）。
- **Host の確認**
  - `Host` が `public_url` のホスト名でない要求は 400 にします。
- **broker の権限**
  - Web UI のキーは、LOCAL の**読み取り専用**キーです（`--control` なし）。
  - 起動時に、このキーが `/operator/state` を読めて、操作側の `/operator/images` は 403 になることを確かめます。操作できるキーなら、起動を拒否します（`webui_broker_key_not_read_only`）。
- **記録**
  - ログイン成功は `webui_login_accepted`、失敗は `webui_login_rejected reason=…` と記録します。
  - アクセスは `webui_access <method> <既知のパスか other> <status>` の形で記録します。
  - cookie、トークン、クエリ文字列、Google の応答は記録しません。

## 前提

- broker が、読み取り専用の LOCAL キーで `/operator/state`・`timeline`・`events`・`job-logs` を読める版であること（#142 以降）。
- Claude gateway の手順（[CLAUDE-GATEWAY.md](CLAUDE-GATEWAY.md)）のうち、Tailscale の準備と Google Cloud の OAuth クライアントが済んでいること。Web UI は同じ Google クライアントとシークレットファイルを使えます。

## 1. 読み取り専用の LOCAL キーを作る

まず、プレースホルダーのキーを入れたファイルを作ります。置き場所の制約は gateway の鍵ファイルと同じで、リポジトリやインストール先の外に置きます。

```dotenv
CARDPUTER_BROKER_URL=http://127.0.0.1:8765/mcp
CARDPUTER_CLIENT_KEY=REPLACE_WITH_ISSUED_KEY
```

次に、`--control` を**付けずに** LOCAL のキーを発行して、このファイルに書き込みます。

```powershell
.\cardputer.exe keys issue --origin local_cli --label webui `
  --write-env 'C:\Users\YOUR_NAME\AppData\Local\DeviceBroker\webui-broker.env'
```

キーの有効期限は最長 30 日です。期限の前に `keys rotate --key-id <ID> --write-env <同じファイル>` で更新し、Web UI を起動し直してください。

## 2. Google のリダイレクト URI を追加する

Google Cloud Console で gateway と同じ OAuth クライアントを開き、**Authorized redirect URIs** に次の URI を 1 つ追加します。

```
https://<machine>.<tailnet>.ts.net:8443/oauth/google/callback
```

## 3. Tailscale で 8443 番に届く端末を絞る

既定では、Web UI は tailnet の中にしか出しません（`funnel = true` のときは公開されるので、この ACL では絞れず、Google ログインが守りです）。そのうえで、管理画面の **Access controls** で、この PC の `tcp:8443` に届く送信元を自分の端末（自分のユーザー）だけにしてください。

自分 1 人だけの tailnet で、既定のポリシー（全員がすべてに届く）のままでも動きます。ただ、ほかの人を招待する前には必ず絞ってください。Google ログインは 2 段目の守りです。

## 4. broker TOML に `[webui]` を書く

```toml
[webui]
public_url = "https://<machine>.<tailnet>.ts.net:8443"   # ポートは必須
port = 8771
google_client_id = "<id>.apps.googleusercontent.com"
allowed_google_sub = "<あなたの Google sub>"
broker_env_file = 'C:\Users\YOUR_NAME\AppData\Local\DeviceBroker\webui-broker.env'
google_client_secret_file = 'C:\Users\YOUR_NAME\AppData\Local\DeviceBroker\gateway-google.env'
serve = false   # true で tailscale serve（tailnet 内だけ）を開く
funnel = false  # true で tailscale funnel（インターネット公開）を開く。serve とは同時に true にできない
```

- `port` は、broker、Tunnel の `health_port`、gateway のどの `port` とも違う値にします。
- `public_url` のポートは、gateway の Funnel と同じ 443 にはできません。
- `funnel = true` のポートは 8443 か 10000 だけです（Tailscale の Funnel の制約）。
- `funnel = true` はインターネットに公開します。iCloud プライベートリレーが有効な iPhone の Safari は、tailnet 限定の `*.ts.net`（`100.x` に解決）に届かず開けません。その場合に使います。Google ログイン（`allowed_google_sub` のアカウントだけ）と読み取り専用の鍵が守りです。
- v0.13.1 より前の broker は `[webui]` の入った TOML を読めません。broker を新しい版にしてから書き足してください。

## 5. まず serve なしで確かめる

```powershell
.\cardputer.exe webui --config '.\device.local.toml'
```

`webui_listening 127.0.0.1:8771` と `webui_serve_disabled; listening on loopback only` が出れば、次のことは確認できています。

- 設定を読めたこと
- 鍵ファイルとシークレットファイルを読めたこと
- キーが読み取り専用であること

別のターミナルで、次のコマンドが 401 を返すことを確かめます。

```powershell
curl.exe -si http://127.0.0.1:8771/api/state -H 'Host: <machine>.<tailnet>.ts.net:8443'
```

## 6. serve を有効にして開く

`serve = true` にして起動し直します。`webui_serve_open https://…:8443 (tailnet only)` が出たら、tailnet に入っている端末のブラウザで `https://<machine>.<tailnet>.ts.net:8443/` を開き、Google でログインします。

- iPhone・iPad では、Safari の共有メニューから「ホーム画面に追加」できます。
- Ctrl+C で止めると、serve を先に閉じてから待ち受けを止めます。
- `funnel = true` のときは、Funnel として確認できるまで待ちます。`funnel = false`（serve）のときに設定が Funnel（公開）になっていたら、起動を拒否して閉じます（`webui_serve_is_public`）。
- Funnel で開くと `webui_funnel_open https://…:8443 (public; Google login required)` が出ます。

`Start-CardputerStack.ps1` を使うと、broker → gateway → Web UI → GUI の順に起動できます。

## エラー一覧

| コード | 原因と対処 |
| --- | --- |
| `webui_config_invalid` | TOML を読めないか、値が不正です（`public_url` に `:ポート` がない、http になっている、など） |
| `webui_config_missing` | `[webui]` 節がありません |
| `webui_port_conflict` | `port` がほかのポートと重なっているか、gateway がある状態で `public_url` が 443 番です |
| `webui_broker_env_invalid` / `webui_broker_port_mismatch` | broker のキーファイルを読めないか、URL のポートが broker と違います |
| `webui_google_secret_invalid` | シークレットファイルを読めません |
| `webui_broker_unavailable` | broker に接続できません（broker を先に起動してください） |
| `webui_broker_key_rejected` | broker がキーを拒否しました。キーの期限切れ・失効か、broker が古い版です |
| `webui_broker_key_not_read_only` | キーに `--control` が付いています。読み取り専用のキーを発行し直してください |
| `webui_already_running` | Web UI はすでに動いています |
| `webui_listen_failed` | `127.0.0.1:<port>` で待ち受けできません |
| `tailscale_serve_port_already_configured` | そのポートには、すでに serve か Funnel の設定があります |
| `webui_public_url_not_this_machine` | `public_url` がこの PC の Tailscale 名と違います |
| `webui_serve_start_failed` / `webui_serve_exited` / `webui_serve_not_confirmed` | serve を開始できない、すぐに終了した、または 15 秒以内に確認できませんでした |
| `webui_serve_is_public` | `funnel = false` なのに、serve の設定が Funnel になっていました。閉じて止めます |
| `tailscale_funnel_not_permitted` | `funnel = true` ですが、このノードに Funnel の権限がありません |

## ミニ表示（PiP）

ヘッダーの **PiP** ボタンで、最新のジョブ（実行中・待ちがあればそれ、なければ直近）の種別・状態・経過時間を、小さな浮動ウィンドウに出します。ボタンは、そのブラウザが `<video>` の PiP に対応しているときだけ出ます。

- 画面に出ている情報（ライブ更新で届いたもの）を `<canvas>` に描き、`captureStream()` で `<video>` にして PiP にします。サーバーへの新しい通信は増えません。
- 画面は 240×32（15:2）の細長い帯で、描画は 360×48 ピクセルです（以前の 320×180 より小さくしています）。窓の大きさは iOS が決めるため、縦横比を細くして、窓の面積が減るかを実機で確かめます。細すぎる比を iOS が補正する可能性があり、タップで出る再生・閉じるボタンが帯を覆うかも未確認です。
- 上の段に、状態の印、種別（BUILD、FLASH など）、経過時間、受信の秒数を出します。ログの行は入れていません。
- 下の段に、ジョブ ID・段階・理由または結果を、小さな文字で電光掲示板のようにゆっくり（毎秒 12 単位）流します。画面に収まる長さのときは止まったままで、先頭で 2 秒止まってから流れ始めます。端末の「視差効果を減らす」が有効なときは流れません。流れている間だけ、100 ミリ秒ごとに描き直します。ジョブのカードには焼いたファームウェア自体の名前がないので、ジョブ ID（コミットの目印が入っていることが多い）を出しています。
- 状態は文字ではなく印で出します。実行中=回る弧（1 秒ごとに進む）、待ち=点滅する丸、成功=チェック、失敗・取り消し=バツ、要確認=感嘆符です。
- 背景は、表示しているジョブの状態の色（実行中=青、成功=緑、失敗=赤、要確認=橙）に、約 0.25 秒かけて変わります。色は、次に状態が変わるまで残ります。
- 表示しているジョブが終了（成功・失敗・取り消し・要確認）に変わったときは、背景を 2 秒間に 3 回（1.5 Hz）だけ明るくします。ページを開いた直後の最初の状態では動かしません。端末の「視差効果を減らす」が有効なときは、色の変化だけで、点滅はしません。
- 経過時間は大きく出します。これはページ自身の時計なので、数字が止まったら、ページごと止められたということです。
- 右端の「N s」は、サーバーから最後にメッセージが届いてからの秒数です。40 秒を超えると、「停止 N s」に変わり、下端に赤い線が出ます。ライブ更新が生きているかは、ここで判定します。
- 開始には、ボタンを押す操作が必要です（ブラウザの制約）。

### iPhone・iPad での確認項目（未確認）

1. ボタンを押すと、小窓が出る。
2. Safari を裏に回す（ホームに戻る）と、小窓の経過時間と状態の更新が続く。止まる場合は、Web Push の通知で代替する。
3. ジョブが動いている間に、状態が小窓に反映される。
4. 小窓を閉じると、更新の処理も止まる。

## ライブ更新が止まったとき

iPhone は、裏に回したページの通信を、ページに知らせずに切ることがあります。そのため、ページ側で次のように自分から張り直します。

- サーバーは、待機中の 25 秒ごとの合図を、ページから見えるデータイベント（`kind: heartbeat`）として送ります。
- 40 秒間何も届かなければ、接続を張り直します。張り直すと、最新の全体の状態が届きます。
- Safari に戻ったとき（`visibilitychange`、`pageshow`）にも張り直します。
- 接続を拒まれたときは、5 秒後に `/api/state` で確かめます。401（ログイン切れ）なら「ログイン切れ」と表示して止まり、それ以外なら張り直します。

## まだ確かめていないこと

- 実際の iPhone・iPad での表示と、ライブ更新の続き方（上の張り直しで解消するか）
- Tailscale Funnel の経路が、SSE の小さなイベントをまとめて遅らせていないか
- `tailscale serve` が転送する `Host` ヘッダーの形（`ホスト名:8443` を想定しています）
- PiP の動作。下の節の確認項目を、実際の iPhone・iPad で確かめてください
