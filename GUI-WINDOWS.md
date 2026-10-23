# セッションと実機待ち列の管理画面

> **ランチャーの引数が変わりました。** `Start-CardputerGui.ps1` は `-Exe` と `-Profile` を指定します
> （`-Config` / `-EnableSerial` は廃止、既定で `--enable-serial`、`-NoSerial` で外します。既定の 127.0.0.1:8765 以外なら `-Url` を指定）。
> [START-WINDOWS.md](START-WINDOWS.md) を参照してください。`uv sync --frozen` は不要です。


## 稼働中のMCPツールを確認する（v0.7.1）

画面上部の **MCP info** を押します。GUIが保持している認証で同じローカルbrokerへ
接続し、初期化情報・tools/list・device_statusだけを読み取ります。USBを開かず、
ジョブ作成・キー作成・サーバー再起動をしません。診断全体の待ち時間は最大5秒です。
認証キー、ヘッダー、完全なツール定義、SDKエラー本文は表示・記録しません。

表示は次の6項目です。

- gui_version: 今開いているGUIのコード版（この版は0.12.0）
- server_version: 実際に応答したMCPサーバーの版
- tools_count: 実際のtools/listの件数（この版は17）
- read_capture: 画像読取ツールの有無
- capture_home: prepare_testの選択肢にcapture_homeがあるか
- broker_epoch: このbroker起動を識別する値。認証キーではありません

件数とcapture項目だけでは、新しい引数や全選択肢が反映されたかまでは確認できません。
クラウド側device_statusのbroker_epochも一致するか比較すると、
同じ実行中サーバーを見ているか切り分けられます。GUIとserverの版が違う場合は、
稼働中プロセスや起動元を確認します。診断は自動で再試行しません。

更新時は仕事を完了して所有を解放し、実行中ジョブと待ち列が空なのを確認します。
利用者がGUIを閉じ、所有するTunnelを切断してからbrokerコンソールでCtrl+Cを押します。
同じプロジェクトのソースだけを更新し、private TOML、.state、キー台帳、外部envファイルは
保持してください。uv sync --frozen後、通常のStart-CardputerGui.ps1で起動します。
既存APIキーを再発行する必要はありません。GUIが保持していたAPIキーの再入力は、
通常どおりTunnelへConnectする際に利用者が行います。

GUIだけの再起動は、認証を保持する元のPowerShellが残っている場合に限り下記CLIで可能です。
新しいPowerShellには認証が継承されません。Start-CardputerGui.ps1はbroker起動も行うため、
既存brokerへの無条件な接続用ランチャーとして使えません。秘密を取得する代替経路はありません。

## 仕事ごとのタイムライン

同じjobカードで要求内容・影響・実行状態・結果・保持中の画像を確認します。
認証済みクライアントがprepareした正確なplan_hashをexecuteすると待ち列へ進みます。
GUIでのApprove、画像送信判断、Pending承認バッジ、許可ルール画面は廃止しました。
`wait_for_approval` は互換性用の非推奨引数で、指定しても待機しません。

画像の取得元principalは、有効なread権限と元の保持期限内で画像を取得できます。
プレビュー表示や更新で再撮影したり、5分の保持期限を延長したりしません。
承認に付随していたローカルHOME raw行の収集オプションも廃止しました。
受信件数など内容を含まない診断と、失敗・所有権不明・review状態は引き続き表示します。

更新時に旧承認待ちと未開始のqueued jobは再実行せず取消します。
実行中断による所有権不明状態は保持し、更新だけでreviewを解除しません。
Queue、session、キャンセル、安全なidle lease解放、Tunnelの管理は引き続き利用できます。

## まず mock で起動

既存 broker が動いていれば、ジョブが実行中でないことを確認し、そのコンソールで停止します。
更新 ZIP のコードを同じプロジェクトに入れ、private 設定と `.state` はそのまま保持してください。
設定・履歴・認証キーを含むファイルは配布 ZIP に入っていません。

通常の PowerShell を MCP フォルダーで開きます。

```powershell
uv sync --frozen
& .\Start-CardputerGui.ps1
```

既定では認証付き mock broker と GUI を起動します。USB を開きません。
初回だけ汎用テンプレートから device.mock.local.toml を作ります。既存ファイルは上書きしません。
Tunnel の実際の ID や認証キーは自動設定しません。
既存 broker の黙った再利用や強制終了、認証キーの永続保存はしません。
GUI 右上の Tunnel... から、同じ broker TOML の非秘密設定と、GUI が起動した client の死活を管理できます。
GUI を閉じるときは、所有する Tunnel があれば切断確認を表示します。broker と実行中の仕事は継続します。元の PowerShell で再度 GUI だけを開く場合:

```powershell
uv run cardputer --auth-source environment gui
```

別ポート/設定では同じ private config を `gui --config` に指定してください。
Tkinter がない Python では GUI は起動できません。通常の Python 3.11+ の Tk 対応環境が必要です。
タイムラインはheadless GUI試験とmock HTTPで検証しています。
この版のWindowsネイティブ表示・PowerShell起動・実機操作は、この実装の試験では未実行です。

## 画面でできること

- MOCK/SERIAL、現在の物理所有者、公開 lease/session/request ID、実行中 job、review/所有権不明状態を表示
- 論理作業セッションを表示。開いているもの、実行継続中の所有者、最近20件の終了セッションが対象
- 実機取得の FIFO 待ち列と、待ち時間の残りを表示
- WAITING の行を選択してキャンセル、上下に移動
- 仕事が動いていない所有者だけを、公開 lease ID を確認して解放

並べ替えは表示時の queue revision と一致した場合のみ受理します。
ほかのクライアントや時間切れで順序が変わった場合は、再取得してから操作します。
待っている間の TTL と、実機取得後の lease TTL は別です。

実行中のジョブ・未確認の USB close がある間は次へ渡しません。
この版には強制停止、実行途中の flash 中断、reset、review の自動解除はありません。
現在の real adapter は passive observation、HELLO、HOME render-buffer captureの限定版です。
GUI は同じ resident broker の状態を読むだけで、別のシリアル読み取り処理を持ちません。

## 実機モードの起動だけ行う場合

```powershell
& .\Start-CardputerGui.ps1 -EnableSerial -Config '.\device.local.toml'
```

これだけでは USB は開きません。実行には別途、認証・所有 lease・immutable planと明示executeが必要です。以前の HELLO の needs_review は保持されます。

## Tunnel と同時に使う場合

[TUNNEL-WINDOWS.md](TUNNEL-WINDOWS.md) の公式セットアップを利用者が済ませた後、
同じ一度の起動から GUI を開けます。v0.5 の -Gui は GUI 管理モードです。
-Connect が併記されても別コンソールの client は起動せず、パネルの Connect 操作を待ちます。

```powershell
& .\Start-CardputerTunnel.ps1 -Connect -Gui
```

まず mock を使ってください。実機切替は別途 `-EnableSerial` と private config が必要です。
GUI 操作用のローカル token は公式 Tunnel client に渡しません。
GUI の operator API は local_cli + control の認証だけを受け付け、Tunnel 用 remote_mcp では操作できません。
クラウド側での接続可否は、実際の tools/list と device_status が成功するまで未確認です。

## Cancel a registered-project job

A queued/running source or build card shows **Cancel job...** at the bottom of
its selected details only when cancellation is available to this operator. An authenticated local operator with control can stop a registered
project's source/build job regardless of its requesting principal/origin. This is
a dedicated local stop authority; the plan owner remains unchanged. Cancellation
uses the existing profile worker/process cleanup path. A first operator stop records
operator_cancelled after confirmed cleanup; earlier stop reasons and uncertain
cleanup codes remain unchanged.

Remote clients still require their own job's principal/origin for MCP cancel_job;
they cannot use the local operator route. This stop authority grants no execution,
artifact-use, lease-acquisition or flash/restore/device-operation permission.
Legacy lease-bound work, unregistered projects and other job types are rejected.
No credential, capability or lease is copied into the GUI.

One click sends one exact job/plan/broker-epoch request. Repeated clicks, refreshes
and lost responses do not replay cancellation. An accepted request displays a
cleanup wait, not a completed stop or released USB device. The current job remains
running until the broker confirms its terminal state. If completion wins the race,
the GUI displays the completed result without rollback. Cleanup uncertainty stays
**Review required**, and device release is never inferred from a cancel response.
Rejected/unknown requests use fixed messages and fresh status; raw errors are not
shown. Closing the GUI does not cancel another job or stop the broker.


## Selected results

Jobs uses a resizable master-detail view. The selected row stays visible while
Enter focuses its detail; Alt+Left returns keyboard focus to the list. Both panes
remain visible. Permitted supported logs and retained images load automatically. Context
labels identify the source, live/retained state and actual read/capture times;
log read time is not an inferred emission time. Empty, loading, denied, unsupported,
expired and unavailable output appears in the viewer itself. Gap/truncation
warnings remain visible. No selection, refresh or image read launches device work.

Text stays selectable and Copy text copies the selected job's displayed text only
on explicit click. Images use the existing validation, recipient/approval and
expiry checks; one automatic read per selected image identity never recaptures or
retries a failed read. Cancel remains a confirmed exact job/plan/epoch operation;
unsupported job types do not gain cancellation authority.

App-session logs use the same authenticated principal's bounded RAM log cache only.
A local operator does not acquire another principal's private output, including
remote-client app logs. Existing LOCAL/control/read checks, immutable plan/runtime
ownership and current-epoch execution records must agree. Control events are not
exposed through this log viewer. No MCP tool/schema or config migration is needed.

Connection keeps current broker updates/owner/active job separate from historical
review. An empty waiting queue uses a compact empty state. MCP info and HTTP
diagnostics have independent collapsible result panels, explicit Refresh/Copy and
actual received timestamps; opening a panel does not issue a request. Table
columns adapt to available space and horizontal bars appear only for overflow.


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
