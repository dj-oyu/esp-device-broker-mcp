# Claude Code / Codex 共通 stdio adapter

Claude Code と Codex は同じ adapter を別プロセスで起動し、起動済みの HTTP broker を
共有します。adapter は tool catalog を取得して呼び出しを転送するだけで、broker の
自動起動、USB reader、別キュー、デバイス取得・解放を持ちません。

この追加は特定デバイスのコマンドを adapter に実装しません。現在の broker が提供する
Cardputer の機能だけが利用可能です。Stackchan 対応や実機画像取得が追加されるわけではありません。

## 1回だけ準備するもの

1. このプロジェクトを、ファームウェアの worktree 外の安定したフォルダーに置きます。
   以下の `C:\Tools\cardputer-device-mcp` は架空の例です。そこで `uv sync --frozen` を実行します。
2. ローカル開発用の **発行済み client key** を1個用意し、Claude Code と Codex で共有します。
   `remote_mcp` の `read` が必須です。操作にも使う場合だけ `control` を明示的に付けます。
   キーの実際の発行は利用者が [KEYS.md](KEYS.md) の対話コマンドで確認して行います。
3. 通常起動では、キー発行時と同じ標準台帳
   `%LOCALAPPDATA%\DeviceBroker\keys.sqlite3` が自動選択されます。起動オプションは不要です。
   標準外の既存台帳を使う場合だけ `serve --key-registry` で明示します。
   起動済み broker の台帳を切り替える場合は、先に安全に idle であることを確認します。
4. リポジトリと adapter フォルダーの両方の外に、OS 上で自分用の private ファイルを用意します。
   以下の例は `C:\Users\YOUR_NAME\AppData\Local\DeviceBroker\local-client.env` です。

GUI の `CARDPUTER_LOCAL_TOKEN`、起動時の一時的な remote token、OpenAI の runtime API key は
このファイルには使えません。ローカル開発用キーとクラウド接続用キーは別にします。
ソフトウェアを更新するだけでは、キーもファイルも作成しません。

Windows では private フォルダーのプロパティ → セキュリティでアクセス権を利用者が確認します。
自分以外の一般ユーザーに読み取り・書き込みを許可しない場所を使ってください。
adapter と broker は Windows ACL を検証・変更しません。標準台帳と明示台帳は同じ
OS-user/private-store の信頼モデルを使います。POSIX では現在の所有者と `0400` / `0600` の
ファイル権限を要求します。リンク・reparse point・複数 hard link は拒否します。

## private env ファイル

受け付けるのは次の2項目だけです。broker URL は省略すると `http://127.0.0.1:8765/mcp` です。
下記のキーは無効なプレースホルダーです。発行時に一度表示された実際の値は利用者が手元で入れ、
チャット、登録 JSON/TOML、コマンド引数、公開リポジトリへ貼り付けません。

```dotenv
CARDPUTER_BROKER_URL=http://127.0.0.1:8765/mcp
CARDPUTER_CLIENT_KEY=REPLACE_WITH_ISSUED_LOCAL_DEVELOPMENT_KEY
```

- UTF-8。Windows の BOM / CRLF も可。最大 8 KiB
- 空行と行頭 `#` のコメント以外は、空白なしの `KEY=value` のみ
- 引用符、`export`、変数展開、コマンド、escape、行末コメント、重複項目、未知の項目は不可
- 値を shell として実行せず、プロセス環境とのマージもしません
- 絶対パスを明示。adapter 配下と `.git` 境界のある checkout 配下は拒否
- URL は `http://127.0.0.1:1024..65535/mcp` の形だけ。Tunnel URL や redirect は不可

手で貼り付ける代わりに、既存のこのファイルへ `keys issue` / `keys rotate` が直接書き込めます。
`--write-env` にファイルの絶対パスを明示した場合だけで、確認入力は同じく必要です。
成功時はキーを表示せず、`CARDPUTER_CLIENT_KEY` の値だけを置き換えます(行がなければ追加)。
他の行・コメント・URL・BOM・改行は維持し、Windows では既存ファイルの ACL を保ちます。
ファイルは新規作成しません。詳細と失敗時の扱いは [KEYS.md](KEYS.md) を参照してください。

登録先は同じ Windows 環境で実行するローカル Claude Code / Codex を想定します。
WSL、別 PC、クラウドの `127.0.0.1` は同じ broker を指すとは限りません。

## Claude Code のプロジェクト登録

[examples/claude-code.mcp.json](examples/claude-code.mcp.json) の `mcpServers.device-broker` を、
対象プロジェクトの `.mcp.json` に既存サーバーを残してマージします。
`command` を安定した adapter の Python に、`--env-file` の次の値を private ファイルの
絶対パスに置き換えます。登録ファイルにはキーを含めません。

project scope は `.mcp.json` を使い、対話セッションではプロジェクト側でサーバーの利用承認を求めます。
新しい worktree ではその登録が存在し、必要な承認が済んでいることを確認します。
同じ PC なら adapter と env ファイルの絶対パスは worktree ごとに変えません。
[Claude Code 公式 MCP ガイド](https://code.claude.com/docs/en/mcp)

実ユーザー名を含むパスもローカル設定です。公開共有には同梱の架空パスの template を使い、
既存の登録を丸ごと上書きしたり実パスを公開したりしないでください。

## Codex のプロジェクト登録

[examples/codex-project-config.toml](examples/codex-project-config.toml) の
`[mcp_servers.device_broker]` を、対象プロジェクトの `.codex/config.toml` にマージします。
`command`、`cwd`、private env ファイルの絶対パスを手元の値へ合わせます。
認証環境の受け渡しや bearer 値を登録 TOML に書く必要はありません。

現在の公式ドキュメントでは project config は信頼済みプロジェクトだけで読み込まれます。
信頼されていない worktree へファイルを置いただけでは有効になりません。
この配布物は信頼設定や sandbox 設定を変更しません。起動するプロジェクト/worktree の
登録と信頼状態を利用者が確認し、MCP の一覧で接続を確認してください。
[公式 MCP 設定](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) / 
[公式 config reference](https://learn.chatgpt.com/docs/config-file/config-reference)

同じ絶対パスを使うため、ファームウェア worktree ごとに adapter やキーを複製する必要はありません。
登録ファイルの存在・信頼状態は各クライアントで確認が必要で、自動的な継承は保証しません。

## 実行時の流れ

先に GUI/既存ランチャーで、1つの認証付き broker を起動しておきます。
Claude Code と Codex が登録された stdio コマンドを起動します。adapter を手入力で実行しても
対話 UI は出ず、MCP の標準入力を待つプログラムです。

初回は mock broker に対して `status(view=identity)`、`status(view=device)`、`status(view=logs, stream=broker)` を確認します。
接続だけでは実機操作しません。読み取り権限だけのキーは機器を取得できません。

MCP の transport セッションと、broker の作業セッションは別です。各作業は
`session(action=open)` で固有の resume capability を受け取り、`session(action=request)` で待ち行列へ入ります。
終了時は安全に `session(action=close)` します。2クライアントで同じキーを共有しても作業セッションは
別に作れますが、セキュリティ上の本人は同じ principal です。個人や端末の識別は保証しません。

CLI/GUI/Tunnel と同じ broker の lease・FIFO・job ID を使います。
必要なscopeを持つ認証済みclientが、submitした正確なplan_hashを明示的に(flash/restoreのplanはflash、それ以外はexecute)積み込みます。
ローカル承認・撮影許可ルール・画像送信判断は廃止しました。取得元principalは保持期限内に
有効なread権限で自分の画像を取得できます。
stdio の接続や切断では、自動で作業セッションを作ったり所有権を解放したりしません。

## エラー・再接続・出力

- `invalid_client_environment`: ファイルの場所・権限・文字コード・上記の厳密な書式を確認
- `stdio_adapter_broker_unavailable`: 同じ PC の既存 broker が動いているかを確認
- `stdio_adapter_client_key_rejected`: broker がキーを拒否した (期限切れ・失効・台帳に無い) うえ、
  env ファイルにも新しいキーが無い。後述の「キーのローテーション」で env ファイルへ有効なキーを入れ、
  同じ呼び出しをやり直す。クライアントの再起動は不要
- `stdio_adapter_client_environment_invalid`: キー拒否の後に env ファイルを読み直したが、起動時と同じ
  場所・権限・書式の検査に通らなかった。adapter は以前のキーのまま何も再送していない。ファイルを直して再試行
- `stdio_adapter_broker_url_changed`: キー拒否の後に読み直した env ファイルの `CARDPUTER_BROKER_URL` が
  変わっていた。実行中の adapter は接続先 broker を切り替えない。URL を戻して再試行するか、クライアントを再起動
- `stdio_adapter_authentication_failed`: 上記以外の 401/403 (Host・Origin 等の拒否を含む)。対象 registry、
  broker URL、キーの有効期限・失効状態を確認。env ファイルの読み直しや再送はしない
- `stdio_adapter_remote_read_key_required`: local operator key ではなく remote/read キーが必要
- `stdio_adapter_tool_outcome_unknown`: 要求の結果が不明。実行済みの可能性があるため、同じ job ID を
  `status(view=job)` で確認し、adapter が自動再実行したとは考えない

adapter は失敗した呼び出しを再送しません。唯一の例外は次節のキー拒否で、broker が実行しなかったことを
証明できる場合に限り1回だけ送り直します。後から明示された別の要求では、新しい HTTP 接続を
使えます。各要求の待ち時間は接続待ちを含め最大30秒、並列接続は4個までです。
キャンセルや EOF は transport を閉じるだけで、broker 内の実行中 job や所有権は継続します。

## キーのローテーション (再起動不要)

`cardputer.exe keys rotate --key-id <key-id> --write-env <env-file>` で、adapter の起動に使った
同じ private env ファイルの `CARDPUTER_CLIENT_KEY` を書き換えれば、Claude Code / Codex の再起動は不要です。
ローテーションで古いキーは即時に失効し、adapter の次の要求は broker に拒否されます。そのとき adapter は:

1. broker の認証ガードによる拒否 (HTTP 401 と固定本文 `{"error":"authentication_required"}`) だけを対象にします。
   このガードは MCP handler より前に応答するため、拒否された要求は broker で実行されていません。
2. 起動時と同じ `--env-file` を、同じ場所・権限・書式の検査で1回だけ読み直します。
3. 別の有効なキーがあり、broker URL が同じ場合だけ、新しいキーで接続し直して同じ要求を **1回だけ** 送ります。
   tool 呼び出しは、その呼び出し自体が拒否されたか、まだ送っていなかった場合に限ります。
   以後の要求は新しいキーを使います。2回目も拒否されたら、それ以上は読み直しも再送もしません。
4. キーが変わっていなければ再送せず、`stdio_adapter_client_key_rejected` を返します。

`stdio_adapter_client_key_rejected` の tool 結果には、固定コードの後に JSON の案内
(`detail`、`next_step`) が付きます。`tools/list` の失敗では MCP エラーの `data` に同じ案内が入ります。
案内にはキー、key id、ファイルパス、broker の応答本文を含めません。意味は次のとおりです。

- キーが期限切れ・失効・未登録で、env ファイルにも新しいキーが無い
- まだ有効なキーの置き換えは `keys rotate --key-id <key-id> --write-env <env-file>`
- 期限切れ・失効したキーはローテーションできないため、`keys issue` で新しいキーを発行し、同じ env ファイルへ入れる
- その後、同じ呼び出しをやり直す (adapter が env ファイルを読み直すので再起動不要)

起動時の確認でキーが拒否された場合は、adapter はこのコードを標準エラーへ出して終了します。
ファイルを直した後、クライアントの MCP 一覧から再接続します。送り直しも同じ要求ごとの時間上限を持つため、
キー拒否の後の1要求は最大で2回分の待ち時間になります。読み直しは拒否の後だけで、
ファイルの変更を監視したり、broker URL を切り替えたりはしません。

標準出力は MCP 専用です。adapter のエラーは固定コードを標準エラーへ出し、
SDK の payload を含み得るログも抑制します。認証キーを引数、表示、ログへ書きません。
作業セッションの resume capability など、broker が意図して返す tool result は MCP 応答として転送します。

現在は `tools/list` と `tools/call` の転送です。SDK が有効と判断した broker の catalog を使い、
annotations、schema、metadata、pagination、本文、画像 content、structuredContent を維持します。
現在の broker にない resource/prompt、task、input-required の対話継続を追加するものではありません。
将来 `x-mcp-header` を持つツールを追加する場合は、呼び出し前の schema 読込と別の互換性確認が必要です。
画像 content の転送が可能でも、現在の Cardputer broker にスクリーンショット機能はありません。

ソフトウェアテストは synthetic key と mock broker で行います。利用者の Windows、実際の
Claude Code/Codex 登録、実キー保存/ACL、実機の動作確認が完了したとは扱いません。


## Standard key registry

Authenticated broker startup automatically reads `%LOCALAPPDATA%\DeviceBroker\keys.sqlite3`
(Unix: `$XDG_STATE_HOME/DeviceBroker/keys.sqlite3`, otherwise
`~/.local/state/DeviceBroker/keys.sqlite3`). `cardputer.exe keys` uses the same default.
It does not depend on cwd or an executable version directory. No registry flag,
Downloads helper, credential regeneration or broker restart is needed for committed
key additions/revocations. Existing GUI local tokens, stdio's explicit private
`--env-file`, Tunnel credentials and scope/principal isolation remain unchanged.
No key is issued at startup; no existing registry is moved or merged. See [KEYS.md](KEYS.md)
for failure behavior and explicit compatibility overrides.
