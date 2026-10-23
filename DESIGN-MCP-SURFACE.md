# 設計: MCP ツール面の作り直し

状態: 設計提案(実装なし)。この文書は `src/` を変更しない。
確認の区別: 「確認済み」はこの文書の作成時に該当コードを読んで、または計測して確かめた
事実。「未確認」「推測」と書いたものは確かめていない。

関連文書: ジョブの積み込み・継続チケット・デバイス保持(mutex)・進捗は
`DESIGN-CONTINUATION-TICKETS.md`。この文書は、その機能を載せるための、ツール面そのものを
定める。

## 1. 決定事項(利用者の方針)

1. 現在の 25 本のツールは、すべて置き換えてよい。場当たり的に増えたものである。
2. **同じ種類の依頼は、同じツールで扱う。** 混同してはいけないパターンは、必須の引数で
   明示する。
3. 列挙しきれない依頼のバリエーションが怖いから、という理由でツールを増やさない。
4. MCP の注釈(`readOnlyHint` など)はツール単位で、ホストは、これに基づいて承認を決める。
   したがって、**引数で一つにまとめてよいのは、同じ安全クラスの操作だけ**。読み取りと変更、
   変更と実行は、混ぜない。
5. 引数の、不明な組み合わせは、固定の理由コードで拒否する(これは、`ValueError` が
   `invalid_request` に潰れる問題も解消する)。

## 2. 現状(確認済み)

方法: 設定ファイル(mock バックエンド)から `create_server` を呼び、ブローカーを起動せずに、
`list_tools()` が返す各ツール(説明・入力スキーマ・注釈を含む)を、コンパクトな JSON にして
文字数を数えた。**トークン数ではなく、文字数**。serial や認証付きの設定でも同じ集合かは
確認していない(`@server.tool` は、条件付きでは登録されていないように見える。未確認)。

- ツール数: 25(読み取り専用 12、変更あり 13。うち `execute_test` だけが `destructive`)。
- `tools/list` の合計: 32,207 文字。`prepare_test` 一つで 8,897 文字(約 28%)。

| ツール | 文字数 | 注釈上の種別 |
|---|---|---|
| `prepare_test` | 8,897 | 変更 |
| `execute_test` | 2,701 | 変更(destructive) |
| `read_build_logs` | 2,324 | 読み取り |
| `app_command` | 1,638 | 変更 |
| `read_app_events` | 1,245 | 読み取り |
| `read_source_resources` | 1,061 | 読み取り |
| `request_home_recovery` | 1,032 | 変更 |
| `read_device_logs` | 1,014 | 読み取り |
| `read_logs` | 963 | 読み取り |
| `cancel_job` | 903 | 変更 |
| `request_device` | 891 | 変更 |
| `read_capture` | 850 | 読み取り |
| `create_work_session` | 836 | 変更 |
| `cancel_device_request` | 806 | 変更 |
| `acquire_session` | 802 | 変更 |
| `cancel_home_recovery` | 786 | 変更 |
| `list_registered_targets` | 765 | 読み取り |
| `renew_session` | 733 | 変更 |
| `resume_work_session` | 723 | 読み取り |
| `close_work_session` | 711 | 変更 |
| `release_session` | 661 | 変更 |
| `job_status` | 645 | 読み取り |
| `list_apps` | 441 | 読み取り |
| `client_identity` | 394 | 読み取り |
| `device_status` | 385 | 読み取り |

観察(確認済み):

- 読み取りの入口が 11 本ある(`client_identity`、`list_registered_targets`、
  `read_source_resources`、`device_status`、`job_status`、`read_logs`、`read_build_logs`、
  `read_device_logs`、`read_app_events`、`read_capture`、`list_apps`)。
- セッションの概念が 2 系統ある(旧 `acquire_session` / `renew_session` / `release_session`
  と、`create_work_session` / `resume_work_session` / `request_device` /
  `cancel_device_request` / `close_work_session`)。`SESSIONS.md` は、旧 `acquire_session` を
  即時取得のみのものとして残している、と書いている。
- 注釈が、実際の効果と合わないものがある。`resume_work_session` は読み取り専用の注釈だが、
  制御の権限があるときは、非公開の物理リースの capability を返す(`SESSIONS.md`)。
  `app_command` は、デバイスに型付きの入力を送る(`変更`の注釈、`destructive` ではない)。
  `request_home_recovery` は、デバイスに `q` を一つ送る明示的な復旧を要求する(同じく
  `変更`)。
- 参照の規模: 旧ツール名は、テスト 182 ファイル中 43、`src/` の 11 モジュール
  (`app_client`、`broker`、`cli`、`gui_job_status`、`gui_live`、`gui_timeline`、
  `input_diagnostics`、`mcp_diagnostics`、`server`、`tunnel`、`tunnel_manager`)、ドキュメント
  17 本に現れる。`cli.py` は、CLI のコマンドとツール名の対応表を持ち、Tunnel の
  `check_broker` も `device_status` を名前で呼ぶ。内部のクライアントも、切り替えの対象である。

## 3. 安全クラス

ツールは、ちょうど一つのクラスに属する。クラスは、**効果**で決める(今の注釈ではなく)。

| クラス | 意味 | 注釈 |
|---|---|---|
| R(読み取り) | 状態を読むだけ。ブローカーの状態も、デバイスも変えない | `readOnlyHint=true` |
| M(状態) | ブローカーの記録(plan、セッション、チケット、キャンセル)を作る・変える。プロセスの起動もデバイスへの送信もしない | `readOnlyHint=false`、`destructiveHint=false` |
| X(効果) | プロセスの起動、デバイスへの書き込みや入力など、外への効果を起こす | `readOnlyHint=false`、`destructiveHint=true` |

引数で統合してよいのは、同じクラスの中だけ。分類の見直し(§1 の 4 に従った結果):

- `resume_work_session` は、リースの capability を返すので、R ではなく M。
- `app_command` の効果のある操作(`action.tap`、`suspend`、`resume`、`stop`)と、
  `request_home_recovery` の実行は、デバイスへの効果なので、X。現在の `変更` より
  厳しくなる(ホストの承認のしかたが変わる可能性。§13 の未決事項)。
- `app_command` の `operation=status` は、効果のない読み取りなので、R(`status` の
  `view=app_status`)に移す。

## 4. 新しいツール面(5 本)

| ツール | クラス | 区別する必須引数 | 役割 |
|---|---|---|---|
| `status` | R | `view` | 状態の読み取りすべて |
| `submit` | M | `kind` | 不変の plan を作る(効果なし)。チケットを使う |
| `execute` | X | `op` | 準備した plan をキューに積む。デバイスへの入力 |
| `cancel` | M | `target` | ジョブ・待ち・チケットの取り消し |
| `session` | M | `action` | リースと作業セッション(一つの概念に統合) |

スキーマの表し方: 区別の引数(`view` / `kind` / `op` / `target` / `action`)は、列挙の
必須項目とする。種類ごとの必須・禁止の項目は、今の `PrepareRequest` が使っている
`allOf` の `if`/`then` の形で表す(`oneOf` は、ホストやモデルによって扱いが違う可能性が
あり、未確認)。**本当の強制はサーバー側の検証**(判別付きの pydantic モデル)で行い、
スキーマは、クライアントへの案内にとどめる。

### 4.1 `status`(R)

必須: `view`。他の引数は、`view` ごとに必須または禁止。

| `view` | 引数 | 旧ツール |
|---|---|---|
| `identity` | なし | `client_identity` |
| `targets` | なし | `list_registered_targets` |
| `source` | `project_id` | `read_source_resources` |
| `device` | なし | `device_status` |
| `session` | `session_id`(省略時は自分の一覧) | (新規。公開情報のみ。capability は返さない) |
| `job` | `job_id` | `job_status` |
| `logs` | `stream`(`broker` / `build` / `device` / `app`)、ほか下記 | `read_logs`、`read_build_logs`、`read_device_logs`、`read_app_events` |
| `capture` | `job_id` | `read_capture` |
| `apps` | なし | `list_apps` |
| `app_status` | `session`、`job_id` | `app_command` の `operation=status` |

`logs` の `stream` ごとの引数:

| `stream` | 必須 | 任意 |
|---|---|---|
| `broker` | なし | `cursor`(`epoch`、`sequence`)、`limit`(1〜100、既定 40) |
| `build` | `job_id` | `cursor`、`levels`、`context`(0〜10)、`limit`、`include_source_cache` |
| `device` | `job_id` | なし |
| `app` | `job_id` | `cursor`(`epoch`、`control`、`log`)、`limit` |

戻り値: `{"ok": true, "view": "<view>", ...}`。`view` ごとの本体は、旧ツールの戻り値と
同じ項目を保つ(クライアントの移行を単純にするため)。`capture` は、旧 `read_capture` と
同じく、画像の内容を含む結果を返す。進捗は、`view=job`(`chain`)と `view=device`
(`device_hold`、`queue_ahead`)に入る(§9)。

### 4.2 `submit`(M)

必須: `kind`、`job_id`。デバイスの kind は、さらに `session`(リースの capability)が必須。
`submit` は、plan を作って返すだけで、プロセスもデバイスにも触れない(確認済み:
`prepare_test` の説明は「シリアルの準備は、USB を開かずに列挙する」)。

`kind` は、今の `TestName` の値と一対一にする(名前は短くする)。

| `kind` | 旧 `TestName` | 必須 | 任意 | 禁止 |
|---|---|---|---|---|
| `fetch` | `firmware_fetch_source` | `project_id`、`source_commit`(40 桁 hex) | | `session`、`device_id`、`build_job_id` など |
| `build` | `firmware_build` | `project_id` | `source_receipt`、`derby_detail_mode`、`derby_dma_mode` | `session`、`device_id` |
| `backup` | `firmware_backup` | `session`、`device_id` | | `build_job_id`、`backup_job_id`、`lkg_receipt`、`project_id` |
| `flash` | `firmware_flash` | `session`、`device_id`、`build_job_id` | `backup_job_id` か `lkg_receipt`(どちらか一方) | `project_id`、`source_*` |
| `restore` | `firmware_restore` | `session`、`device_id` | `backup_job_id` か `lkg_receipt`(どちらか一方) | `build_job_id`、`project_id` |
| `app_session` | `app_session` | `session`、`app`(`app_id`、`start`、許可する操作、上限など) | | `project_id` |
| `derby_fixed` | `derby_fixed_workload` | `session` | `flash_job_id` | `project_id` |
| `derby` | `derby_smoke` | `session` | | `project_id` |
| `capture` | `capture_home` | `session` | | `project_id` |
| `restart` | `restart_and_observe` | `session` | | `project_id` |
| `observe` | `passive_observe` | `session` | | `project_id` |
| `observe_firmware` | `firmware_observe` | `session` | | `project_id` |
| `hello` | `hello` | `session` | | `project_id` |
| `diagnostics` | `diagnostics` | `session` | | `project_id` |
| `workflow` | `firmware_workflow` | `session` | | `project_id` |
| `home_recovery` | (`request_home_recovery`) | `source_job_id` | | `session` |

注意:

- 各 kind の必須・禁止の完全な表は、実装の前に、今の検証(`PrepareRequest` のスキーマと、
  `Broker.prepare` / `ProfileJobs.prepare` の分岐)から機械的に起こす。この表の、
  `backup` / `flash` / `restore` / `fetch` / `build` の行は、`prepare_test` の説明と
  スキーマの読み取りに基づく(確認済み)。それ以外の kind の細部は、推測を含む。
- `observe`、`workflow`、`hello`、`diagnostics` など、今も必要な種類かは、確認していない
  (§12)。不要なら、`kind` から外す。
- `home_recovery` は、今は `request_home_recovery` が、plan の作成と積み込みを一度に行う。
  新しい面では、`submit`(plan の作成)と `execute`(積み込み)の 2 回に分かれる。
  15 秒の単回の権限は、`execute` の時点から数える(推測。§13)。
- `app_session` の `app` の中身(`AppSessionOptions`)は、今のまま。

任意の共通引数: `after`(チケット。`DESIGN-CONTINUATION-TICKETS.md`)。

戻り値: `{"ok": true, "plan_hash": ..., "plan": {...}, "approval_required": false,
"storage": {...}(あれば)}`(今の `prepare_test` と同じ)。

### 4.3 `execute`(X)

必須: `op`。

| `op` | 必須 | 任意 | 旧ツール |
|---|---|---|---|
| `run` | `plan_hash` | `session`(デバイスの plan のとき必須)、`continues`、`chain_total_hint` | `execute_test` |
| `app_input` | `session`、`job_id`、`boot_id`、`run_id`、`control_session`、`operation`(`action.tap` / `suspend` / `resume` / `stop`)、`command_id`、`seq`、`action`(`action.tap` のときだけ) | | `app_command`(`status` 以外) |

- `op=run` の戻り値: 今の `execute_test` と同じジョブの記録に、`continues` のときは
  `continuation_ticket` を加える。
- 旧 `ExecuteRequest.wait_for_approval` は、承認の仕組みが廃止されている(確認済み:
  `approval_interface_retired`)ため、新しい面には持ち込まない。
- 積み込みは「承認」ではなく、認証済みの明示的な要求である。この性質は変えない。

### 4.4 `cancel`(M)

必須: `target`。

| `target` | 必須 | 任意 | 旧ツール |
|---|---|---|---|
| `job` | `job_id` | `session`(デバイスのジョブのとき必須)、`plan_hash`(指定したら、一致を確認) | `cancel_job`、`cancel_home_recovery` |
| `request` | `resume_token`、`request_id` | | `cancel_device_request` |
| `ticket` | `ticket` | | (新規) |

旧 `cancel_home_recovery` の `plan_hash` は、「その plan を取り消す」という厳密さを
表す。一般の `job` の取り消しに、任意の `plan_hash` として広げる。

### 4.5 `session`(M)

必須: `action`。リース(物理的なデバイスの占有)と、作業セッション(論理的な継続)を、
一つの概念にまとめる。

| `action` | 必須 | 任意 | 旧ツール |
|---|---|---|---|
| `open` | `owner`(`Origin`) | `ttl_seconds`(1〜86400、既定 3600)、`lease_seconds`、`wait_seconds` | `create_work_session`、`acquire_session` |
| `resume` | `resume_token` | | `resume_work_session` |
| `request` | `resume_token` | `lease_seconds`(1〜300、既定 30)、`wait_seconds`(0〜3600、既定 300)、`after` | `request_device` |
| `renew` | `session`(capability) | `lease_seconds` | `renew_session` |
| `release` | `session` | | `release_session` |
| `close` | `resume_token` | | `close_work_session` |

- `open` に `lease_seconds` と `wait_seconds` を付けると、作業セッションの作成と、リースの
  要求を、一度に行う。`wait_seconds=0` は「待たない。すぐ取れなければ `device_busy`」で、
  旧 `acquire_session` と同じ規則(待ち行列や実行中があれば取れず、FIFO を飛び越さない)。
- 旧 `acquire_session` は、作業セッションを作らない。新しい面では、作業セッションが一つ
  できる(開いているセッションの上限 128 に数えられる)。この違いは、§13 の未決事項。
- `resume` は、認証と、制御の権限があるときだけ、リースの capability を返す。

戻り値: 今の各ツールの戻り値の項目を保つ。

### 4.6 戻り値とエラーの形

成功: `{"ok": true, ...}`。失敗: `{"ok": false, "error": "<固定コード>"}`(必要なら、
`"field": "<引数名>"` を加える)。例外の文章、パス、値は返さない(今の方針と同じ)。

## 5. エラーコードの語彙

固定の語彙とし、一か所(`PUBLIC_REASON_CODES`)に集める。

1. **引数の検証(新規。`invalid_request` の潰れを置き換える)**

   | コード | 意味 | `field` |
   |---|---|---|
   | `unknown_view` / `unknown_kind` / `unknown_op` / `unknown_target` / `unknown_action` / `unknown_stream` | 区別の引数の値が、一覧にない | なし |
   | `argument_required` | その種類で必須の引数が、ない | 引数名 |
   | `argument_not_allowed` | その種類で禁止の引数が、ある | 引数名 |
   | `argument_conflict` | 排他の引数が両方ある(`backup_job_id` と `lkg_receipt` など) | 引数名 |
   | `argument_invalid` | 形式や範囲が合わない | 引数名 |

   `field` の値は、スキーマの引数名(固定の語彙)に限る。値そのものは返さない。
2. **認証・所有権(既存のコードをそのまま保つ)**: `authentication_required`、
   `insufficient_scope`、`principal_not_authorized`、`invalid_session`、
   `authenticated_owner_mismatch`、`invalid_work_session`、`job_not_found`、
   `job_session_mismatch` など。
3. **資源・キュー(既存)**: `device_busy`、`queue_full`、`wait_queue_full`、
   `source_fetch_busy`、`recovery_review_required`、`history_full_archive_offline` など。
4. **ソース・ビルド・flash の失敗(既存の `SourceError` / `BuildLocationError` /
   `FlashFailure` のコード)**: 変えない(クライアントが依存している)。
5. **チェーン(新規。`DESIGN-CONTINUATION-TICKETS.md`)**: `ticket_invalid`、
   `ticket_consumed`、`ticket_void`、`hold_expired`、`chain_broken`、
   `device_state_changed`。
6. 残余の `invalid_request`: 分類できない `ValueError` だけ(原則として、出さない状態を
   目指す)。

既存のコードの全一覧は、段階 1 で、コードから機械的に起こして登録する(今の件数は、
確認していない)。語彙にない文字列は、`invalid_request` のままにする。パス・例外の文章・
内部の値は返さない。

## 6. 旧ツールから新ツールへの対応(25 本、すべて)

| # | 旧ツール | 新しい呼び方 | 備考 |
|---|---|---|---|
| 1 | `client_identity` | `status`(`view=identity`) | |
| 2 | `list_registered_targets` | `status`(`view=targets`) | |
| 3 | `read_source_resources` | `status`(`view=source`、`project_id`) | |
| 4 | `device_status` | `status`(`view=device`) | 保持・待ちの集計が加わる |
| 5 | `job_status` | `status`(`view=job`、`job_id`) | `chain` が加わる |
| 6 | `read_logs` | `status`(`view=logs`、`stream=broker`) | |
| 7 | `read_build_logs` | `status`(`view=logs`、`stream=build`、`job_id`) | |
| 8 | `read_device_logs` | `status`(`view=logs`、`stream=device`、`job_id`) | |
| 9 | `read_app_events` | `status`(`view=logs`、`stream=app`、`job_id`) | |
| 10 | `read_capture` | `status`(`view=capture`、`job_id`) | 画像の内容を返す |
| 11 | `list_apps` | `status`(`view=apps`) | |
| 12 | `resume_work_session` | `session`(`action=resume`) | R から M へ。公開情報の読み取りは `status`(`view=session`) |
| 13 | `acquire_session` | `session`(`action=open`、`owner`、`lease_seconds`、`wait_seconds=0`) | 作業セッションが一つできる |
| 14 | `renew_session` | `session`(`action=renew`) | |
| 15 | `release_session` | `session`(`action=release`) | |
| 16 | `create_work_session` | `session`(`action=open`) | |
| 17 | `request_device` | `session`(`action=request`) | `wait_seconds` の下限が 1 から 0 に |
| 18 | `cancel_device_request` | `cancel`(`target=request`) | |
| 19 | `close_work_session` | `session`(`action=close`) | |
| 20 | `prepare_test` | `submit`(`kind=...`) | `test` の代わりに `kind` |
| 21 | `execute_test` | `execute`(`op=run`) | `wait_for_approval` は廃止 |
| 22 | `cancel_job` | `cancel`(`target=job`) | |
| 23 | `app_command` | `execute`(`op=app_input`)、`operation=status` は `status`(`view=app_status`) | `status` 以外は X に |
| 24 | `request_home_recovery` | `submit`(`kind=home_recovery`)と `execute`(`op=run`) | 1 回が 2 回に |
| 25 | `cancel_home_recovery` | `cancel`(`target=job`、`job_id`、`plan_hash`) | |

失われる機能はない。ただし、同じ結果が得られることは、移行のテストで確かめる(§11 の段階 3)。
`status`(`view=app_status`)が、旧 `app_command` の `operation=status` と同じ内容を
返すかは、未確認。

## 7. ジョブの種類の抽象

`kind`(`submit` の区別)と、plan の `test` は、**登録された種類**に振り分ける。今の
手書きの `if`/`elif`(`Broker.prepare`、`Broker.execute`、`Broker._work`、`Broker.cancel`、
`_physical_busy`、`_execution_uncertain`、`ProfileJobs` など)を、この登録に置き換える。

```
class JobKind(Protocol):
    kind: str                       # submit の kind
    test: str                       # plan の "test"
    resource: Literal["source", "build", "device"]
    needs_lease: bool               # device のとき true
    def prepare(self, request, principal) -> dict   # 効果なし。plan の詳細を返す
    def resolve(self, plan, parent_result) -> dict  # 親の完了後に値を解決する(チケット)
    def verify(self, plan, resolved) -> None        # 実行直前の検証。固定コードで失敗
    async def execute(self, plan, stage, live) -> str
    def cancel(self, job_id) -> bool                # 停止の要求
    def active(self) -> bool                        # 実行中か
    def uncertain(self) -> bool                     # 状態が不確実か
    def public_view(self, job, principal) -> dict   # status の job ビューの詳細
```

- 登録簿 `JOB_KINDS` を、一か所に置く。`_physical_busy()` と `_execution_uncertain()` は、
  登録された種類の `active()` / `uncertain()` を集計するだけになる。新しい種類を足しても、
  判定の漏れが起こらない。
- 資源(`resource`)から、キューが決まる: `source` と `build` は、登録プロジェクトの経路
  (`ProfileJobs` のキュー)、`device` は、デバイスの経路(リースの待ち行列)。今は、
  二つのワーカーが別にある。最初は、そのまま残し、インターフェースの裏に隠す。
- 既存の実装との対応(確認済み): `FirmwareSource`(`prepare`・`execute`・`request_stop`・
  `active`)、`FirmwareBuild`(`prepare`・`execute`・`request_stop`、`process.active`)、
  `FirmwareFlash`(`prepare`・`execute`・`request_stop`・`active`・`uncertain`)、
  `app_sessions`、`home_recovery`、`backend.prepare`(診断など)。いずれも、似た形の
  メソッドをすでに持っている。
- `resolve` と `verify` は、`DESIGN-CONTINUATION-TICKETS.md` の、実行時の解決と検証を
  載せる場所。

## 8. `AGENTS.md` の境界との照合

| 境界 | 新しい面での扱い | 確認 |
|---|---|---|
| 承認・明示性 | `submit` は plan を作るだけ(M)。効果は `execute`(X)の明示的な要求でのみ起こる。plan は不変で、`plan_hash` が一致しなければ実行しない | 今の `prepare_test` / `execute_test` と同じ分離 |
| backup / flash / restore は別々の承認済み操作 | `kind` が `backup` / `flash` / `restore` で別々。一つの `execute` で複数をまとめない。ブローカーが自分で足さない | 設計上 |
| 暗黙の rollback・再試行なし | 失敗は固定コードで終わる。`session` / `execute` に再試行の引数はない | 設計上 |
| 所有権 | `cancel`、`status` は、plan の principal で所有を検査する(今と同じ)。`session` は principal に結び付く | 今の検査を保つ |
| キャンセル | `cancel(target=job)` は、ジョブ単位。実行中の取り消しは、停止の要求で、結果は確定後に返る(今と同じ) | 今の `cancel_job` と同じ |
| 秘匿 | `status` は、自分のジョブの詳細だけ。他の principal は、集計だけ。`field` は固定の引数名のみ。値や例外の文章は返さない | 設計上 |
| 読み取りは効果なし | R は、ブローカーの状態もデバイスも変えない(§11 のテストで検証) | 設計上 |
| デバイスの所有は、作業が残るあいだは手放さない | 変更なし(`session` の `release` は、実行中なら `active_job_cannot_release`) | 今の `release` と同じ |
| 「ブローカーの起動はデバイス接続の証明ではない」 | `status(view=device)` は、観測した事実と、そうでないものを分けて返す(今と同じ項目) | 今の項目を保つ |
| X クラスの分類 | `app_input`、`home_recovery` の実行は、デバイスへの効果として X | 現在の注釈より厳しい。§13 |

## 9. チケット・チェーン・mutex・進捗の、新しい面での表現

`DESIGN-CONTINUATION-TICKETS.md` の機能は、**ツールを足さずに**、次の引数と戻り値で表す。

| 機能 | 新しい面での表現 |
|---|---|
| チケットの発行 | `execute`(`op=run`)の任意の `continues: true`。戻り値の `continuation_ticket` |
| チケットの使用 | `submit` の任意の `after`(plan に親を記録する)。消費は、後続の `execute` の積み込みの時点(方式 A)、または `session`(`action=request`、`after`)の時点(方式 B) |
| デバイスの保持 | `session`(`action=request`)の付与が、同じ principal のチケットを、他のチェーンより優先 |
| チケットの取り消し・保持の解放 | `cancel`(`target=ticket`) |
| 親を待つジョブ | ジョブの状態 `waiting_parent`(`status` の `view=job`) |
| 進捗 | `status`(`view=job`)の `chain`(`chain_position`、`chain_jobs_known`、`chain_open`、`chain_state`、`chain_budget_remaining_seconds`)。`status`(`view=device`)の `device_hold`(保持中か、残り秒数)と `queue_ahead`(前にいるチェーンの数) |
| 表示用の総数ヒント | `execute` の任意の `chain_total_hint` |

追加する任意の引数は、`submit.after`、`execute.continues`、`execute.chain_total_hint`、
`session.after`(方式 B)の 4 つ。ツールは増えない。

## 10. 切り替え計画

### 10.1 前提

- ツール名とスキーマの変更は、クライアントにとって破壊的である。
- この環境では、ホストのメタデータの更新(Refresh)ができない(`AGENTS.md`)。サーバーを
  変えても、ホストが取り込み済みの `tools/list` は、自動では変わらない。更新の手順は、
  サーバーの再起動 → ホストの接続の「更新」→ 新しい会話での再確認、であり、**今の会話が
  更新されたとは言えない**。
- 旧名を使う側: 他のエージェント(MCP だけで操作している)、ローカルの CLI、GUI、
  Tunnel の `check_broker`、診断ツール、テスト 43 ファイル、ドキュメント 17 本(§2)。

### 10.2 方針

1. **一括の置き換え**。新旧を、同じ `tools/list` に並べない(並べると、ツール数が一時的に
   30 本になり、削減の目的に反する)。
2. サーバーのバージョンと、ツール集合のバージョンを上げる。`status`(`view=identity`)に
   `tool_set_version` を含める(新規。整数)。
3. 切り替え時のチェックポイント(`AGENTS.md` の MCP メタデータのチェックポイント):
   サーバーの `tools/list` の名前・入力スキーマ・注釈を、親のクラウドクライアントから
   呼べるツールと、比較する。サーバー側だけの確認は、受け入れにならない。更新後は、
   新しい会話で再確認する。
4. 他のエージェントとの調整: 旧ツール名を使うプロンプト・手順・スキル(ドキュメントの
   一覧は §2)を、同じ変更で更新する。切り替えの日時を、事前に伝える。

### 10.3 短い別名の期間は、置くか

**置く価値がある。ただし、`tools/list` には載せない隠しの別名にする**(推奨)。

- 理由: ホストの取り込み済みの一覧が古いままのクライアントが、旧名を呼んで、
  `unknown_tool` で失敗するのを避けられる。一覧に載せないので、コンテキストを増やさない。
- 実装: 旧名の呼び出しを、サーバーの呼び出しの入口で、新しい呼び方に変換する。
  実現できるかは、FastMCP の呼び出し経路を読んで確認する(**未確認**)。
- 制約: 別名は、**効果が今と完全に同じ**ものだけ。すべての旧名の呼び出しを、
  ログに残し(旧名と呼び出し元の principal のみ。内容は残さない)、使われなくなったことを
  確認してから、削除する。
- 期間: 短く固定する(案: 2 週間か、使用が 0 になるまでの、早いほう。上限を超えたら、
  削除する)。長くすると、新しい面への移行が進まない。
- 注意(注釈): 古い一覧を持つホストは、旧名の注釈(たとえば `app_command` は `変更`)を
  根拠に承認を決めている。別名経由の呼び出しは、効果が今と同じなので、承認の根拠は変わらない。
  新しい面で X に分類を上げた操作は、新しい一覧を取り込んだあとの呼び出しにだけ、
  厳しい承認が働く。

### 10.4 リスクと、起きたときの扱い

- スキーマが大きくなり、`tools/list` の文字数が減らない可能性(§12)。段階 0 の試作で、
  文字数を測ってから、実装に進む。
- 区別付きのスキーマを、クライアントのモデルが正しく扱えない可能性。段階 3 の前に、
  代表的なクライアントで、`submit` の 16 種類の呼び出しを試す。
- 戻すとき: 旧名の別名が残っている間は、ブローカーの内部は、旧ツールの実装と共通。旧の
  面に戻す変更は、一つの変更で行える(旧の面を残してあるため)。

## 11. 実装の段階とテスト

| 段階 | 内容 | テスト |
|---|---|---|
| 0 | 計測と上限。§2 の計測スクリプトを、テストにする。新しい面の試作スキーマ(実装なし)の文字数を測る | `tools/list` の件数・文字数・ツールごとの文字数の上限。`tools/list` の名前・スキーマ・注釈のスナップショット |
| 1 | エラーの語彙と引数の検証層。**既存のツールのまま**(名前を変えない)に適用する。`ValueError` の理由を、固定の語彙で返す | 語彙の網羅テスト。語彙外は `invalid_request`。パス・例外の文章が出ないこと |
| 2 | `JobKind` の登録。内部のリファクタリング(面は変えない)。`_physical_busy` などを集計に置き換える | 既存のテスト一式(変更なしで通ること)。種類を足しても判定が漏れないことのテスト |
| 3 | 新しい 5 本のツールを、同じ内部の上に載せる薄い層として実装する。旧名は隠しの別名にする。内部のクライアント(CLI、GUI、Tunnel、診断)を新しい面へ | 旧→新の対応テスト(§6 の 25 行すべて。旧の呼び出しと新の呼び出しが、ブローカーの状態として同じ結果を作ること)。注釈のクラスのテスト(各ツールが一つのクラスだけ。`status` の呼び出しの前後で、履歴・状態のハッシュが変わらないこと)。不明な組み合わせの拒否のテスト(各 `kind` の必須・禁止) |
| 4 | チケット・保持・進捗(`DESIGN-CONTINUATION-TICKETS.md` の段階 3・4) | 同文書の不変条件のチェックリスト |
| 5 | 隠しの別名を削除する。ドキュメントの旧名を除く | 旧名の呼び出しが `unknown_tool` になること |

互換性: 段階 1・2 は、クライアントから見えない(段階 1 は、エラーコードの細分化のみ)。
段階 3 が、切り替えの本番(§10)。

## 12. 未確認

- 新しい面の `tools/list` の文字数(減る見込みは、推測。`oneOf` ではなく、`allOf` の
  `if`/`then` を使う前提でも、増える可能性がある)。
- mock 設定での計測が、serial・認証付きの設定でも同じ集合か。
- `JobKind` に収まらない例外の有無(`backend.prepare` の診断系の種類)。
- `observe`、`workflow`、`hello`、`diagnostics`、`observe_firmware` が、今も必要か。
- 各 `kind` の必須・禁止の完全な表(§4.2 の、`backup`/`flash`/`restore`/`fetch`/`build` 以外)。
- `status`(`view=app_status`)が、旧 `app_command` の `operation=status` と同じ内容を返すか。
- `request_home_recovery` を 2 回の呼び出しに分けたとき、15 秒の単回の権限をどこから
  数えるのが、今の意味に合うか。
- FastMCP で、`tools/list` に載せない別名を、呼び出しの入口で受けられるか。
- ホストが、`allOf` の `if`/`then` と、新しい注釈の分類を、どう扱うか。
- 既存のエラーコードの全件数。

## 13. 未決事項

1. `app_input` と、`home_recovery` の実行を、X クラスにする(現在の `変更` より厳しい
   注釈になる)。ホストの承認の挙動が変わる。これでよいか。
2. 旧 `acquire_session`(作業セッションを作らない即時取得)を、`session(action=open)` に
   畳むと、作業セッションが一つできる(上限 128 に数えられる)。これでよいか。
3. 隠しの別名の期間(案: 2 週間か、使用が 0 になるまで)と、切り替えの日時。
4. `status` の `view` の、ここでの 10 種類で足りるか。`logs` の `stream` を、
   別の `view` に分けたほうが分かりやすいか。
5. `tools/list` の文字数と本数の上限(段階 0 のテスト)を、いくつにするか。初期の案は、
   新しい面を実装した時点の値を固定し、増やす変更を、理由つきでしか許さない。

## 14. 今後の規則

- 新しい機能は、ツール面の予算に照らして、正当化する。新しいツールを足す変更には、
  既存のツールの `view` / `kind` / `op` / `target` / `action` の追加や、任意の引数では
  足りない理由を、書く。
- 新しいツールを足すより先に、どの安全クラス(R / M / X)に属するかを決める。クラスが
  違う操作は、同じツールに混ぜない。
- 「こういう呼び方もありそう」という推測で、ツールや引数を足さない。実際に必要になった
  依頼だけを、既存の区別の引数の一つの値として足す。
- 段階 0 のテストの上限を、緩める変更は、利用者の明示的な承認を必要とする。

## 15. 蓄積の兆候(観察。既存の実装が壊れている、という意味ではない)

CI は通っており、この文書は「動かない」と言っているのではない。ただし、機能を足していく
うちに、次のような蓄積の跡が見られる。

- principal ごとのキャッシュ保護: 新しいキーが、古い principal の領域を再利用できず、
  上限に達する、という問題を PR #79 で直した(容量を、プロジェクト全体で共有する形に)。
- `ValueError` → `invalid_request` の潰れ(`DESIGN-CONTINUATION-TICKETS.md` §2.5): 理由の
  情報が、境界で失われている。
- ジョブの種類ごとの分岐と、「実行中か」の判定の手書きの列挙(`_physical_busy`、
  `ProfileJobs._uncertain`、`_execution_uncertain`): 新しい種類を足すたびに、漏れの危険が
  ある(§7 で解消する案)。
- セッションの概念の重複(§2): 旧リースとワークセッションが並存している。
- ツール面の肥大(§2): 25 本、32,207 文字。1 本の `prepare_test` が約 28%。
- 注釈と効果のずれ(§2、§3): 読み取り専用の注釈で、capability を返すもの。

これらは、機能の追加が先に進み、整理が後になった、通常の成長の跡と見るのが妥当だと考えて
いる。この文書の作り直しと、段階 0〜2 は、その整理にあたる。
