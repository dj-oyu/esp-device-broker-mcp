# セッションと実機待ち列の管理画面

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

## 仕事ごとのタイムライン（v0.9）

v0.9.3では、手動の実機capture承認の横に、今回のjobだけHOME受信行をローカル確認する
チェック項目を追加しました。既定はOFFです。必要な場合だけチェックし、通常の内容確認・
HOME/firmware確認文・Approve planを行います。チェックだけでは実行しません。
実行後、同じカードに最大1行・1024 bytesの内容をエスケープ表記し、残り時間を表示します。
保持は実行開始から120秒のメモリだけです。長過ぎた場合は抜粋せず、その旨を表示します。
MCPやクラウドに自動送信せず、保存・コピーも自動では行いません。内容を見て、必要なら
秘密を除いた部分だけ利用者が選んで共有してください。認証の更新失敗や期限切れでは表示を消します。
確認済みの過去の失敗は **Failed · reviewed** と表示し、再確認要求を残しません。

v0.9.2では実行後に「別途実行待ち」の案内を残さず、撮影失敗と取得済み画像の保持期限切れを区別します。
失敗結果の診断には固定項目の受信件数を追加しました。更新だけで再実行やneeds_review解除は行いません。

v0.9.1ではカードの枠を明確にし、完了済みカードを既定で折りたたみます。
ヘッダーから同じカードを開閉でき、手動で選んだ開閉状態は通常の更新で変わりません。
状態・次の操作・時刻を短く表示し、SHA・内部stage・JSON・詳しい時刻は詳細にまとめます。
画像はサムネイルからカード内で拡大できます。開閉や拡大は再撮影しません。
承認時に必要な操作の影響・対象・送信先・実機確認文は、操作ボタンの近くに表示します。

メイン画面で、同じjobカードに要求内容・影響・承認・実行状態・画像・送信許可を表示します。
プランの内容は自動で読み込みます。全文JSONはカード内の詳細で開閉できます。
クライアントが先に `execute_test(wait_for_approval=true)` を送ったjobでは、
**Approve & start** を1回押すと同じbrokerがそのjobを実行待ち列へ渡します。
承認後にAIの次の要求を待つ必要はありません。USBを安全に占有できるまでは待機し、
実機の反応が厳密に0秒になる保証はありません。
まだprepareだけのjobは **Approve plan** と表示され、実行要求は別途必要です。
実行依頼の待機期限は送信時のlease期限までです。自動更新・再起動後の再実行はしません。
実機では確認文入力・期限・プラン一致の条件を引き続き満たす必要があります。

取得済み画像は同じカードに自動表示します。未許可の場合は画像を確認して送信を承認するか、
拒否します。ローカルのプレビュー表示だけではAIへ送信する権限は付与しません。
画像は完了から5分で消えます。残り時間と期限切れを表示し、更新は再撮影しません。

**Pending** バッジは有効な手動承認待ち件数と **NEW** を示し、押すとタイムラインへ移動します。
別の承認ウィンドウは開きません。約2秒ごとにバックグラウンド更新し、取得失敗や古い結果は
**?** で示します。Queueは同じ画面右側、許可ルールとTunnelは設定パネルにあります。
詳細な許可モデルは [CAPTURE-PERMISSIONS.md](CAPTURE-PERMISSIONS.md) を参照してください。

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
現在の real adapter は passive observation、HELLO、承認付きHOME render-buffer captureの限定版です。
GUI は同じ resident broker の状態を読むだけで、別のシリアル読み取り処理を持ちません。

## 実機モードの起動だけ行う場合

```powershell
& .\Start-CardputerGui.ps1 -EnableSerial -Config '.\device.local.toml'
```

これだけでは USB は開きません。実行には別途、認証・所有 lease・immutable plan・
ローカル承認が必要です。以前の HELLO の needs_review は保持されます。

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

A queued/running source or build card can show **Cancel job <exact job ID>** in
its header. An authenticated local operator with control can stop a registered
project's source/build job regardless of its requesting principal/origin. This is
a dedicated local stop authority; the plan owner remains unchanged. Cancellation
uses the existing profile worker/process cleanup path and records operator_cancelled
when cleanup is confirmed. Uncertain cleanup retains its original needs_review code.

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
