# 設計: MCP ツール面の作り直し

状態: **実装済み**(0.13.0 向け。下の「実装状況」を参照)。以降の本文は、実装前の設計の記述で、
実装と異なる点は「実装状況」に挙げる。

## 実装状況(実装後に追記)

実装したもの(`src/cardputer_mcp/surface.py`):

- 6 本のツール `status` / `submit` / `execute` / `flash` / `cancel` / `session`。旧 25 本は、別名なしで除いた。
  旧ハンドラは `server.py` の内部の操作(`ops`)として残し、新しいツールが、区別の引数と
  必須・禁止の引数を、固定の理由コード(`argument_required` / `argument_not_allowed` /
  `argument_conflict` / `argument_invalid`、`wrong_execute_tool` / `wrong_flash_tool`)で検査してから呼ぶ。
- `tools/list` は 13,350 文字(旧 32,207 文字)。破壊的の注釈は `flash` だけ。`execute` / `flash` は、plan の種類
  (`DESTRUCTIVE_TESTS`)から、ブローカーが、使うべきツールを決めて強制する(状態を変える前に拒否)。
- `status(view=identity)` は `tool_set_version: 2` を返す。
- `ValueError` の理由を固定語彙で返す(`public_reasons.py`、`PUBLIC_REASON_CODES`)。
- `submit` の `after`、`execute` / `flash` の `continues` と `chain_total_hint`、`session`(`action=request`)の
  `after`、`cancel`(`target=ticket`)、`status(view=job)` の `chain`、`status(view=device)` の `device_hold`。

設計と違う点・実装しなかった点:

- `JobKind` の登録(§7、段階 2)は、実装していない。`DESTRUCTIVE_TESTS` と `KIND_TEST` の対応表で足りた。
  内部の判定(`_physical_busy` など)は、従来のまま。
- `session(action=open)` に `lease_seconds` を付けて `wait_seconds` が 0 か省略のとき、従来の
  `acquire_session` と同じく、**作業セッションを作らない**(§4.6 の記述と異なる)。`wait_seconds` が
  1 以上のときは、作業セッションを作って、デバイスの要求まで行う。
- `app_input` の `command_id` と `seq` は、必須ではなく任意(`status` 操作は不要なため。従来どおり)。
- 先積みは、`backup` と `app_session` を対象外にした(§13 の未決事項 4 の案どおり)。

確認の区別: 「確認済み」はこの文書の作成時に該当コードを読んで、または計測して確かめた
事実。「未確認」「推測」と書いたものは確かめていない。

関連文書: ジョブの積み込み・継続チケット・デバイス保持(mutex)・plan とリースの結び付き・
進捗は `DESIGN-CONTINUATION-TICKETS.md`。この文書は、その機能を載せるための、ツール面
そのものを定める。

## 1. 決定事項(利用者の方針)

1. 現在の 25 本のツールは、すべて置き換えてよい。場当たり的に増えたものである。
2. **同じ種類の依頼は、同じツールで扱う。** 混同してはいけないパターンは、必須の引数で
   明示する。
3. 列挙しきれない依頼のバリエーションが怖いから、という理由でツールを増やさない。
4. MCP の注釈(`readOnlyHint` など)はツール単位で、ホストは、これに基づいて承認を決める。
   したがって、**引数で一つにまとめてよいのは、同じ安全クラスの操作だけ**。読み取り・変更・
   破壊的な操作は、混ぜない。
5. 引数の、不明な組み合わせは、固定の理由コードで拒否する(これは、`ValueError` が
   `invalid_request` に潰れる問題も解消する)。
6. **注釈を、今より厳しくしない。** 利用者の明示的な承認なしに、どの操作の注釈も、今より
   厳しくしない。`app_command` と `request_home_recovery` は、今のとおり、変更(`M`:
   `readOnlyHint=false`、`destructiveHint=false`)のまま。`request_home_recovery` は、今のとおり、
   plan の作成と積み込みを**一回の呼び出し**で行う。
7. **破壊的(`destructiveHint=true`)なのは、デバイスのフラッシュを上書きする操作だけ**:
   flash と restore(restore もフラッシュに書く)。それ以外(fetch、build、backup、test 類、
   型付きの入力、home recovery)は、破壊的な注釈を付けない。backup は、デバイスを読むだけなので、
   破壊的ではない。今の `execute_test` は、すべての種類で `destructiveHint=true` なので、
   flash と restore 以外は、**緩む**(利用者の決定)。緩める変更であり、決定事項 6 とは衝突しない。
8. **ツールは、安全クラスでだけ分ける。** 新しいツールが正当化されるのは、新しい安全クラスが
   あるときだけ。注釈はホストへのヒントであり、安全の境界ではない。本当の境界は、
   ブローカー側の承認(plan_hash への明示的な要求とその再検証)・所有権の検査・15 秒などの
   単回の権限である。
9. **旧ツール名の別名の期間は 0。** 切り替えの時点で、旧名を一括で除く。

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
- 注釈と効果の組み合わせに、不揃いなものがある。`resume_work_session` は読み取り専用の
  注釈だが、制御の権限があるときは、非公開の物理リースの capability を返す
  (`SESSIONS.md`)。`app_command` は、デバイスに型付きの入力を送る(`変更`)。
  `request_home_recovery` は、デバイスに `q` を一つ送る明示的な復旧を要求する(`変更`)。
  **これらの注釈は、今回は厳しくしない**(決定事項 6)。
- 今の `execute_test` は、ツール単位で `destructiveHint=true`(確認済み)。したがって、
  fetch・build・backup・test 類の実行にも、破壊的の注釈が付いている。
- 参照の規模: 旧ツール名は、テスト 182 ファイル中 43、`src/` の 11 モジュール
  (`app_client`、`broker`、`cli`、`gui_job_status`、`gui_live`、`gui_timeline`、
  `input_diagnostics`、`mcp_diagnostics`、`server`、`tunnel`、`tunnel_manager`)、ドキュメント
  17 本に現れる。`cli.py` は、CLI のコマンドとツール名の対応表を持ち、Tunnel の
  `check_broker` も `device_status` を名前で呼ぶ。内部のクライアントも、切り替えの対象である。

## 3. 安全クラスと注釈の原則

ツールは、ちょうど一つのクラスに属する。

| クラス | 注釈 | 操作 |
|---|---|---|
| R(読み取り) | `readOnlyHint=true` | `status` が扱うすべて |
| M(変更) | `readOnlyHint=false`、`destructiveHint=false` | plan の作成、フラッシュ以外の実行(fetch、build、backup、test 類)、型付きの入力、home recovery、取り消し、セッション |
| D(破壊) | `readOnlyHint=false`、`destructiveHint=true` | デバイスのフラッシュを上書きする実行: **flash と restore だけ** |

原則:

1. ツールは、安全クラスでだけ分ける。新しいツールが正当化されるのは、新しい安全クラスが
   あるときだけ。
2. 引数で統合してよいのは、同じクラスの操作だけ。R・M・D は混ぜない。
3. `destructiveHint=true` は、デバイスのフラッシュを上書きする操作(flash と restore)の
   ためにとっておく。backup はデバイスを読むだけなので M。fetch・build・test 類も D ではない。
4. 今の `execute_test` は、すべての種類で `destructiveHint=true` である。新しい面では、
   flash と restore を除く種類は M に**緩む**。これは利用者の決定であり、「厳しくしない」
   (決定事項 6)に反しない。
5. 注釈は、ホストへのヒントであり、安全の境界ではない。本当の境界は、ブローカー側の
   承認(plan_hash への明示的な要求と、その再検証)、所有権の検査、15 秒などの単回の権限。
   plan の種類と、使うべきツールの対応は、**ブローカーが plan の種類から決めて強制する**。
   クライアントの申告は信用しない(§4.3、§4.4、§7)。
6. 注釈を厳しくする変更は、利用者の明示的な承認が要る。

この原則から、次が決まる。

- `resume_work_session` は、今のとおり R のまま(`status` の `view=session`)。リースの
  capability を返す点も、今と同じ。R から M に上げる案は、利用者の承認が要る(§13)。
- `app_command`(`operation=status` を含む)と `request_home_recovery` は、今のとおり M。
  `execute` の中の別の `op` として、M のツールに入れる(§4.3)。
- flash と restore は、別のツール `flash`(D)に入れる。それ以外の実行は、`execute`(M)。
## 4. 新しいツール面(6 本)

| ツール | クラス | 区別する必須引数 | 役割 |
|---|---|---|---|
| `status` | R | `view` | 状態の読み取りすべて |
| `submit` | M | `kind` | 不変の plan を作る(効果なし)。チケットを使う |
| `execute` | M | `op` | flash と restore 以外の plan の積み込み。型付きの入力。home recovery |
| `flash` | D | なし(`plan_hash`) | flash と restore の plan だけを積み込む |
| `cancel` | M | `target` | ジョブ・待ち・チケットの取り消し |
| `session` | M | `action` | リースと作業セッション(一つの概念に統合) |

25 本から 6 本になる。クラスは三つ(R: `status`、M: `submit` / `execute` / `cancel` /
`session`、D: `flash`)。M のツールが四つあるのは、同じクラスの中で、引数の形が大きく違う
操作(plan を作る、積む、取り消す、セッションを扱う)を、別のツールに分けているため。この 6 本
は、現在の 25 本からの置き換えとして、一度だけ決める。**今後、ツールを増やす理由になるのは、
新しい安全クラスだけ**(§3 の原則 1)。M のツールを分けた点は、§13 の未決事項に挙げる。

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
| `session` | `resume_token` | `resume_work_session`(今と同じ。R のまま) |
| `job` | `job_id` | `job_status` |
| `logs` | `stream`(`broker` / `build` / `device` / `app`)、ほか下記 | `read_logs`、`read_build_logs`、`read_device_logs`、`read_app_events` |
| `capture` | `job_id` | `read_capture` |
| `apps` | なし | `list_apps` |

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

必須: `kind`、`job_id`。デバイスの kind は、さらに `session`(リースの capability)が必須
(先積みの plan(`DESIGN-CONTINUATION-TICKETS.md` §5)は、`session` を持たず、`after` で
親を指す)。`submit` は、plan を作って返すだけで、プロセスにもデバイスにも触れない
(確認済み: `prepare_test` の説明は「シリアルの準備は、USB を開かずに列挙する」)。

`kind` は、今の `TestName` の値と一対一にする(名前は短くする)。「積み込み」の列は、その
plan を積むツール。

| `kind` | 旧 `TestName` | 積み込み | 必須 | 任意 | 禁止 |
|---|---|---|---|---|---|
| `fetch` | `firmware_fetch_source` | `execute` | `project_id`、`source_commit`(40 桁 hex) | | `session`、`device_id`、`build_job_id` など |
| `build` | `firmware_build` | `execute` | `project_id` | `source_receipt`、`derby_detail_mode`、`derby_dma_mode` | `session`、`device_id` |
| `backup` | `firmware_backup` | `execute` | `session`、`device_id` | | `build_job_id`、`backup_job_id`、`lkg_receipt`、`project_id` |
| `flash` | `firmware_flash` | **`flash`** | `session`、`device_id`、`build_job_id` | `backup_job_id` か `lkg_receipt`(どちらか一方) | `project_id`、`source_*` |
| `restore` | `firmware_restore` | **`flash`** | `session`、`device_id` | `backup_job_id` か `lkg_receipt`(どちらか一方) | `build_job_id`、`project_id` |
| `app_session` | `app_session` | `execute` | `session`、`app`(`app_id`、`start`、許可する操作、上限など) | | `project_id` |
| `derby_fixed` | `derby_fixed_workload` | `execute` | `session` | `flash_job_id` | `project_id` |
| `derby` | `derby_smoke` | `execute` | `session` | | `project_id` |
| `capture` | `capture_home` | `execute` | `session` | | `project_id` |
| `restart` | `restart_and_observe` | `execute` | `session` | | `project_id` |
| `observe` | `passive_observe` | `execute` | `session` | | `project_id` |
| `observe_firmware` | `firmware_observe` | `execute` | `session` | | `project_id` |
| `hello` | `hello` | `execute` | `session` | | `project_id` |
| `diagnostics` | `diagnostics` | `execute` | `session` | | `project_id` |
| `workflow` | `firmware_workflow` | `execute` | `session` | | `project_id` |

注意:

- 各 kind の必須・禁止の完全な表は、実装の前に、今の検証(`PrepareRequest` のスキーマと、
  `Broker.prepare` / `ProfileJobs.prepare` の分岐)から機械的に起こす。この表の、
  `backup` / `flash` / `restore` / `fetch` / `build` の行は、`prepare_test` の説明と
  スキーマの読み取りに基づく(確認済み)。それ以外の kind の細部は、推測を含む。
- `observe`、`workflow`、`hello`、`diagnostics`、`observe_firmware` など、今も必要な種類かは、
  確認していない(§12)。不要なら、`kind` から外す。
- `app_session` の `app` の中身(`AppSessionOptions`)は、今のまま。

任意の共通引数: `after`(チケット。`DESIGN-CONTINUATION-TICKETS.md`)。

戻り値: `{"ok": true, "plan_hash": ..., "plan": {...}, "execute_tool": "execute" | "flash",
"approval_required": false, "storage": {...}(あれば)}`。`execute_tool` は、クライアントへの
案内(その plan を積むツール名。固定の語彙)で、**強制はブローカーが plan の種類から行う**
(§4.3、§4.4)。それ以外は、今の `prepare_test` と同じ。

### 4.3 `execute`(M)

必須: `op`。**flash と restore 以外**の実行。注釈は M(`destructiveHint=false`)。

| `op` | 必須 | 任意 | 旧ツール |
|---|---|---|---|
| `run` | `plan_hash` | `session`(従来の結び付きのデバイス plan のとき必須)、`continues`(チケットの発行)、`chain_total_hint`(進捗の表示用) | `execute_test`(flash と restore 以外の plan) |
| `app_input` | `session`、`job_id`、`boot_id`、`run_id`、`control_session`、`operation`(`action.tap` / `status` / `suspend` / `resume` / `stop`)、`command_id`、`seq`、`action`(`action.tap` のときだけ) | | `app_command` |
| `home_recovery` | `job_id`、`source_job_id` | | `request_home_recovery` |

- `op=run`: 準備した plan を、認証済みの**明示的な要求**として、キューに積む。承認は、
  `plan_hash` への明示的な要求と、その再検証であり、チケットでは代替されない。
  戻り値は、今の `execute_test` と同じジョブの記録に、`continues` のときは
  `continuation_ticket` を加える。
- `op=run` が、flash または restore の plan の `plan_hash` を受け取ったら、**固定の理由コード
  `wrong_execute_tool` で拒否する**(§4.4 の判定と同じ。拒否は、状態を変える前に行う)。
- `op=app_input`: 今の `app_command` と同じ。`operation=status` も、今のまま、ここに含める。
- `op=home_recovery`: 今のとおり、**一回の呼び出し**で、plan の作成と積み込みを行う。
  15 秒の単回の権限も、今のまま。
- `app_input` と `home_recovery` は、`submit` / plan の積み込みを通らない。ブローカー自身の、
  別の検査(型付きの入力の検査、証明された閉鎖の確認、単回の権限)を、今のまま持つ。
- 旧 `ExecuteRequest.wait_for_approval` は、承認の仕組みが廃止されている(確認済み:
  `approval_interface_retired`)ため、新しい面には持ち込まない。

### 4.4 `flash`(D)

必須: `plan_hash`。任意: `session`(従来の結び付きのデバイス plan のとき必須)、`continues`、
`chain_total_hint`。注釈は D(`readOnlyHint=false`、`destructiveHint=true`)。

- 積むのは、**flash と restore の plan だけ**。それ以外の plan の `plan_hash` は、固定の理由
  コード `wrong_flash_tool` で拒否する。
- 区別の引数は要らない(この面では、`flash` の役割は一つ)。
- 戻り値: `execute` の `op=run` と同じジョブの記録(`continues` のときは
  `continuation_ticket` を加える)。
- これ以外は、`execute` の `op=run` と同じ。ブローカー側の承認(明示的な要求、再検証、
  所有権、リースの結び付き)は、どちらのツールも同じで、注釈の違いだけが、ホストへの
  ヒントとして違う。

**間違ったツールの plan を拒否する方法**(`execute` と `flash` で共通):

1. 認証と権限を確認する(今と同じ)。
2. `plan_hash` から、plan を取り出す(今の `execute_test` のハンドラが行っているのと同じ。
   見つからない・変更されている場合は、今と同じ `plan_not_found_or_changed`)。
3. plan の principal と呼び出しの principal が一致することを確認する(今と同じ。他の
   principal の plan には、種類を教えない)。
4. plan の `test`(種類)から、ブローカーが `JobKind`(§7)を引き、`destructive` の値と、
   呼ばれたツールを照らす。**クライアントが申告した種類は使わない。**
5. 食い違うときは、`{"ok": false, "error": "wrong_execute_tool" | "wrong_flash_tool",
   "use": "flash" | "execute"}` を返す(`use` は、固定の語彙)。ここまでに、状態は一切変えない
   (ジョブの行も、チケットの消費も、待ちエントリも作らない)。
6. 一致したときだけ、積み込みに進む。

### 4.5 `cancel`(M)

必須: `target`。

| `target` | 必須 | 任意 | 旧ツール |
|---|---|---|---|
| `job` | `job_id` | `session`(従来の結び付きのデバイスのジョブのとき必須)、`plan_hash`(指定したら、一致を確認) | `cancel_job`、`cancel_home_recovery` |
| `request` | `resume_token`、`request_id` | | `cancel_device_request` |
| `ticket` | `ticket` | | (新規) |

旧 `cancel_home_recovery` の `plan_hash` は、「その plan を取り消す」という厳密さを表す。
一般の `job` の取り消しに、任意の `plan_hash` として広げる。先積みのジョブ
(`session` の capability を持たない)は、principal の所有で検査する。

### 4.6 `session`(M)

必須: `action`。リース(物理的なデバイスの占有)と、作業セッション(論理的な継続)を、
一つの概念にまとめる。

| `action` | 必須 | 任意 | 旧ツール |
|---|---|---|---|
| `open` | `owner`(`Origin`) | `ttl_seconds`(1〜86400、既定 3600)、`lease_seconds`、`wait_seconds` | `create_work_session`、`acquire_session` |
| `request` | `resume_token` | `lease_seconds`(1〜300、既定 30)、`wait_seconds`(0〜3600、既定 300) | `request_device` |
| `renew` | `session`(capability) | `lease_seconds` | `renew_session` |
| `release` | `session` | | `release_session` |
| `close` | `resume_token` | | `close_work_session` |

- `resume` は、ここに無い。今のとおり R の `status`(`view=session`)にある(§3)。
- `open` に `lease_seconds` と `wait_seconds` を付けると、作業セッションの作成と、リースの
  要求を、一度に行う。`wait_seconds=0` は「待たない。すぐ取れなければ `device_busy`」で、
  旧 `acquire_session` と同じ規則(待ち行列や実行中があれば取れず、FIFO を飛び越さない)。
- 旧 `acquire_session` は、作業セッションを作らない。新しい面では、作業セッションが一つ
  できる(開いているセッションの上限 128 に数えられる)。この違いは、§13 の未決事項。
- `request` の `after`(チケット付きの取得)は、`DESIGN-CONTINUATION-TICKETS.md` の
  内部の足場(M1)の間だけ存在する。最終形(方式 A)では、チケットは、積み込み
  (`execute` / `flash`)で消費する。

### 4.7 戻り値とエラーの形

成功: `{"ok": true, ...}`。失敗: `{"ok": false, "error": "<固定コード>"}`(必要なら、
`"field": "<引数名>"` や、`"use": "<ツール名>"` を加える)。例外の文章、パス、値は返さない
(今の方針と同じ)。
### 4.8 規模の見積もり

| 項目 | 今 | 新しい面 |
|---|---|---|
| ツール数 | 25(R 12、M 12、破壊的の注釈 1) | 6(R 1、M 4、D 1) |
| 破壊的の注釈が付くツール | `execute_test`(すべての種類に) | `flash`(flash と restore だけ) |
| 区別の値の数(ツールではない) | | `view` 9 + `kind` 15 + `op` 3 + `target` 3 + `action` 5 = 35 |
| `tools/list` の文字数 | 32,207(確認済み) | **未計測**。段階 0 の試作で測る。減る見込みは、推測 |

区別の値は、ツールの数にはならないが、スキーマの文字数には現れる。35 個の値に、
種類ごとの必須・禁止の規則が付くため、`submit` が最も大きくなる(今の `prepare_test` の
8,897 文字と同程度か、それ以上になる可能性がある。推測)。

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

   | `wrong_execute_tool` | `execute` に、flash または restore の plan が渡された(`use: "flash"`) | なし |
   | `wrong_flash_tool` | `flash` に、flash・restore 以外の plan が渡された(`use: "execute"`) | なし |

   `field` の値は、スキーマの引数名(固定の語彙)に限る。値そのものは返さない。`use` の値は、
   ツール名(固定の語彙)に限る。
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

注釈の列は、旧→新のクラスを示す。「同じ」または「緩む」であり、厳しくなるものはない。

| # | 旧ツール | 新しい呼び方 | 注釈 | 備考 |
|---|---|---|---|---|
| 1 | `client_identity` | `status`(`view=identity`) | R→R | |
| 2 | `list_registered_targets` | `status`(`view=targets`) | R→R | |
| 3 | `read_source_resources` | `status`(`view=source`、`project_id`) | R→R | |
| 4 | `device_status` | `status`(`view=device`) | R→R | 保持・待ちの集計が加わる |
| 5 | `job_status` | `status`(`view=job`、`job_id`) | R→R | `chain` が加わる |
| 6 | `read_logs` | `status`(`view=logs`、`stream=broker`) | R→R | |
| 7 | `read_build_logs` | `status`(`view=logs`、`stream=build`、`job_id`) | R→R | |
| 8 | `read_device_logs` | `status`(`view=logs`、`stream=device`、`job_id`) | R→R | |
| 9 | `read_app_events` | `status`(`view=logs`、`stream=app`、`job_id`) | R→R | |
| 10 | `read_capture` | `status`(`view=capture`、`job_id`) | R→R | 画像の内容を返す |
| 11 | `list_apps` | `status`(`view=apps`) | R→R | |
| 12 | `resume_work_session` | `status`(`view=session`、`resume_token`) | R→R | 今と同じ。capability を返す点も同じ |
| 13 | `acquire_session` | `session`(`action=open`、`owner`、`lease_seconds`、`wait_seconds=0`) | M→M | 作業セッションが一つできる |
| 14 | `renew_session` | `session`(`action=renew`) | M→M | |
| 15 | `release_session` | `session`(`action=release`) | M→M | |
| 16 | `create_work_session` | `session`(`action=open`) | M→M | |
| 17 | `request_device` | `session`(`action=request`) | M→M | `wait_seconds` の下限が 1 から 0 に |
| 18 | `cancel_device_request` | `cancel`(`target=request`) | M→M | |
| 19 | `close_work_session` | `session`(`action=close`) | M→M | |
| 20 | `prepare_test` | `submit`(`kind=...`) | M→M | `test` の代わりに `kind`。戻り値に `execute_tool` が加わる |
| 21a | `execute_test`(flash と restore の plan) | `flash`(`plan_hash`) | 破壊的→D | 同じ(破壊的のまま) |
| 21b | `execute_test`(それ以外の plan) | `execute`(`op=run`、`plan_hash`) | 破壊的→M | **緩む**(利用者の決定)。`wait_for_approval` は廃止 |
| 22 | `cancel_job` | `cancel`(`target=job`) | M→M | |
| 23 | `app_command` | `execute`(`op=app_input`) | M→M | `operation=status` も含む。今と同じ |
| 24 | `request_home_recovery` | `execute`(`op=home_recovery`) | M→M | 一回の呼び出し(plan の作成と積み込み)。今と同じ |
| 25 | `cancel_home_recovery` | `cancel`(`target=job`、`job_id`、`plan_hash`) | M→M | |

`execute_test` は、plan の種類で二つに分かれる(21a、21b)。呼び出す側が、どちらを使うかは、
`submit` の戻り値の `execute_tool` で分かる。間違えたときは、`wrong_execute_tool` /
`wrong_flash_tool`(§4.4)で、状態を変えずに拒否される。

失われる機能はない。厳しくなる注釈はない。ただし、同じ結果が得られることは、移行のテストで
確かめる(§11 の段階 3)。
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
    destructive: bool               # flash と restore だけ true。積み込みのツールを決める
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
- `destructive` は、**ブローカーが種類ごとに決めて持つ**値(クライアントの申告ではない)。
  `execute` / `flash` は、plan の `test` から `JobKind` を引き、`destructive` と、呼ばれた
  ツール(`flash` は true、`execute` は false)が一致するときだけ、積み込みに進む(§4.4)。
  `submit` の戻り値の `execute_tool` も、この値から作る。
- `execute` の `op=app_input` と `op=home_recovery` は、plan の積み込みを通らないので、
  `JobKind` には入れない。それぞれ、今の `app_sessions` と `home_recovery` の入口を
  そのまま呼ぶ。
- 既存の実装との対応(確認済み): `FirmwareSource`(`prepare`・`execute`・`request_stop`・
  `active`)、`FirmwareBuild`(`prepare`・`execute`・`request_stop`、`process.active`)、
  `FirmwareFlash`(`prepare`・`execute`・`request_stop`・`active`・`uncertain`)、
  `app_sessions`、`backend.prepare`(診断など)。いずれも、似た形のメソッドをすでに持っている。
- `resolve` と `verify` は、`DESIGN-CONTINUATION-TICKETS.md` の、実行時の解決と検証を
  載せる場所。

## 8. `AGENTS.md` の境界との照合

| 境界 | 新しい面での扱い | 確認 |
|---|---|---|
| 明示的な要求・副作用 | `submit` は plan を作るだけ(M)。キューへの積み込みは、`execute`(M)か `flash`(D)の明示的な要求でのみ起こる。plan は不変で、`plan_hash` が一致しなければ実行しない。`execute` の `app_input` と `home_recovery` は、今と同じ別の検査を通る | 今の `prepare_test` / `execute_test` と同じ分離 |
| backup / flash / restore は別々に明示的に要求される | `kind` が `backup` / `flash` / `restore` で別々。backup は `execute`、flash と restore は `flash` で積む。一回の呼び出しで積むのは、一つの plan だけ。複数をまとめない。ブローカーが自分で足さない | 設計上 |
| 間違ったツールでの実行 | ブローカーが plan の種類から、使うべきツールを決めて強制する(`wrong_execute_tool` / `wrong_flash_tool`)。状態を変える前に拒否する | 設計上。段階 3 のテストで検証 |
| 暗黙の rollback・再試行・書き込みの再試行なし | 失敗は固定コードで終わる。どのツールにも、再試行の引数はない | 設計上 |
| 認証・所有権 | `cancel`、`status` は、plan の principal で所有を検査する(今と同じ)。`session` は principal に結び付く | 今の検査を保つ |
| キャンセル | `cancel(target=job)` は、ジョブ単位。実行中の取り消しは、停止の要求で、結果は確定後に返る(今と同じ) | 今の `cancel_job` と同じ |
| 秘匿 | `status` は、自分のジョブの詳細だけ。他の principal は、集計だけ。`field` は固定の引数名のみ。値や例外の文章は返さない | 設計上 |
| 読み取りは効果なし | R は、ブローカーの状態もデバイスも変えない。ただし `status` の `view=session` は、今の `resume_work_session` と同じく、リースの capability を返す | 今と同じ。§12 |
| デバイスの所有は、作業が残るあいだは手放さない | 変更なし(`session` の `release` は、実行中なら `active_job_cannot_release`) | 今の `release` と同じ |
| 「ブローカーの起動はデバイス接続の証明ではない」 | `status(view=device)` は、観測した事実と、そうでないものを分けて返す(今と同じ項目) | 今の項目を保つ |
| 注釈 | どの操作も、今より厳しくしない(§3、§6 の注釈の列)。破壊的は flash と restore だけ。それ以外の実行は、今の破壊的から M に緩む | 設計上。段階 3 のテストで検証 |

## 9. チケット・チェーン・mutex・進捗の、新しい面での表現

`DESIGN-CONTINUATION-TICKETS.md` の機能は、**ツールを足さずに**、次の引数と戻り値で表す。

| 機能 | 新しい面での表現 |
|---|---|
| チケットの発行 | `execute`(`op=run`)または `flash` の任意の `continues: true`。戻り値の `continuation_ticket` |
| チケットの使用 | `submit` の任意の `after`(plan に親を記録する)。消費は、後続の積み込み(`execute` または `flash`)の時点 |
| ツールをまたぐチェーン | チケットは、どのツールで積んでも同じ。親が `flash`(D)で、後続の test を `execute`(M)で積んでよい。後続の plan が flash/restore なら、`flash` で積む(plan の種類で決まる。チケットはツールの選択を変えない)。保持・フェンシングは、ツールに依らない |
| デバイスの保持 | 保持中は、同じ principal のチケットを消費した後続が、他のチェーンより優先 |
| チケットの取り消し・保持の解放 | `cancel`(`target=ticket`) |
| 親を待つジョブ | ジョブの状態 `waiting_parent`(`status` の `view=job`) |
| 先積みの plan | `submit` が `session` なしで受け付け(`after` で親を指す)、`execute` が積み込む(版 3 の plan) |
| 進捗 | `status`(`view=job`)の `chain`(`chain_position`、`chain_jobs_known`、`chain_open`、`chain_state`、`chain_budget_remaining_seconds`)。`status`(`view=device`)の `device_hold`(保持中か、残り秒数)と `queue_ahead`(前にいるチェーンの数) |
| 表示用の総数ヒント | `execute` の任意の `chain_total_hint` |

追加する任意の引数は、`submit.after`、`execute.continues`、`execute.chain_total_hint`、
`flash.continues`、`flash.chain_total_hint` の 5 つ(内部の足場 M1 の間だけ、`session.after`
がある)。ツールは増えない。

## 10. 切り替え計画

### 10.1 前提

- ツール名とスキーマの変更は、クライアントにとって破壊的である。
- この環境では、ホストのメタデータの更新(Refresh)ができない(`AGENTS.md`)。サーバーを
  変えても、ホストが取り込み済みの `tools/list` は、自動では変わらない。更新の手順は、
  サーバーの再起動 → ホストの接続の「更新」→ 新しい会話での再確認、であり、**今の会話が
  更新されたとは言えない**。ホストの更新を、回避する手段はない(権限・認証・接続の再登録を
  変えて回避しない)。
- 旧名を使う側: 他のエージェント(MCP だけで操作している)、ローカルの CLI、GUI、
  Tunnel の `check_broker`、診断ツール、テスト 43 ファイル、ドキュメント 17 本(§2)。

### 10.2 方針(決定: 別名の期間は 0)

1. **旧名は、切り替えの時点で、一括で除く。** 新旧を、同じ `tools/list` に並べない。
   隠しの別名も置かない。
2. サーバーのバージョンと、ツール集合のバージョンを上げる。`status`(`view=identity`)に
   `tool_set_version` を含める(新規。整数)。
3. 古い `tools/list` を取り込んだままのクライアントは、旧名を呼ぶと、未知のツールとして
   失敗する。これは、想定された挙動であり、更新(上の手順)と新しい会話での再確認で解消する。
4. 切り替え時のチェックポイント(`AGENTS.md` の MCP メタデータのチェックポイント):
   サーバーの `tools/list`(6 本)の名前・入力スキーマ・注釈を、親のクラウドクライアントから
   呼べるツールと、比較する。サーバー側だけの確認は、受け入れにならない。更新後は、
   新しい会話で再確認する。
5. 他のエージェントとの調整: 旧ツール名を使うプロンプト・手順・スキル(ドキュメントの
   一覧は §2)を、同じ変更で更新する。特に、`execute_test` は、plan の種類で `execute` と
   `flash` に分かれる(`submit` の戻り値の `execute_tool` で分かる)ことを伝える。切り替えの日時を、事前に伝える。切り替えの前に、
   他のエージェントが実行中のジョブがないことを確認する(実行中のジョブの途中で、
   サーバーを入れ替えない)。
6. 内部のクライアント(CLI、GUI、Tunnel の `check_broker`、診断)と、テスト、ドキュメントは、
   同じ変更で、新しい面に移す。CLI のコマンド名(`status` など)は、そのまま残してよい
   (内部の対応表だけを、新しいツールに向ける)。

### 10.3 リスクと、起きたときの扱い

- スキーマが大きくなり、`tools/list` の文字数が減らない可能性(§12)。段階 0 の試作で、
  文字数を測ってから、実装に進む。
- 区別付きのスキーマを、クライアントのモデルが正しく扱えない可能性。段階 3 の前に、
  代表的なクライアントで、`submit` の 15 種類の呼び出しを試す。
- 戻すとき: 別名がないので、新しい面と旧い面は、同時に存在しない。戻すときは、前の版の
  配置(`AGENTS.md` に従い、旧バージョンのディレクトリを残す)で、起動し直す。
  ホストの更新と新しい会話での再確認が、また必要になる。

## 11. 実装の段階とテスト

| 段階 | 内容 | テスト |
|---|---|---|
| 0 | 計測と上限。§2 の計測スクリプトを、テストにする。新しい面の試作スキーマ(実装なし)の文字数を測る | `tools/list` の件数・文字数・ツールごとの文字数の上限。`tools/list` の名前・スキーマ・注釈のスナップショット |
| 1 | エラーの語彙と引数の検証層。**既存のツールのまま**(名前を変えない)に適用する。`ValueError` の理由を、固定の語彙で返す | 語彙の網羅テスト。語彙外は `invalid_request`。パス・例外の文章が出ないこと |
| 2 | `JobKind` の登録。内部のリファクタリング(面は変えない)。`_physical_busy` などを集計に置き換える | 既存のテスト一式(変更なしで通ること)。種類を足しても判定が漏れないことのテスト |
| 3 | 切り替えの本番(§10): 新しい 6 本のツールを、同じ内部の上に載せる薄い層として実装し、旧 25 本を除く。内部のクライアント(CLI、GUI、Tunnel、診断)・テスト・ドキュメントを、同じ変更で移す | 旧→新の対応テスト(§6 の 25 行すべて。旧の呼び出しで作っていた状態と、新の呼び出しで作る状態が、同じになること)。**注釈のテスト**(§6 の注釈の列どおり、各操作の注釈が、旧より厳しくなっていないこと。`destructiveHint=true` のツールは `flash` だけ)。各ツールが一つのクラスだけであること。**間違ったツールのテスト**(`execute` が flash・restore の plan を `wrong_execute_tool` で、`flash` がそれ以外を `wrong_flash_tool` で拒否し、ジョブの行・チケットの消費・待ちエントリが一切できないこと。判定が plan の種類で決まり、リクエストの申告に依らないこと)。`status` の呼び出しの前後で、履歴・状態のハッシュが変わらないこと(`view=session` を除く、今と同じ副作用の範囲)。不明な組み合わせの拒否のテスト(各 `kind` の必須・禁止) |
| 4 | チケット・保持・先積み・進捗(`DESIGN-CONTINUATION-TICKETS.md` の段階 3・4) | 同文書の不変条件のチェックリスト |

互換性: 段階 1・2 は、クライアントから見えない(段階 1 は、エラーコードの細分化のみ)。
段階 3 が、切り替えの本番(§10)。段階 4 の機能は、新しい引数(`after`、`continues`)を
使わないクライアントには、影響しない。

## 12. 未確認

- 新しい面の `tools/list` の文字数(減る見込みは、推測。`oneOf` ではなく、`allOf` の
  `if`/`then` を使う前提でも、増える可能性がある)。
- mock 設定での計測が、serial・認証付きの設定でも同じ集合か。
- `JobKind` に収まらない例外の有無(`backend.prepare` の診断系の種類)。
- `observe`、`workflow`、`hello`、`diagnostics`、`observe_firmware` が、今も必要か。
- 各 `kind` の必須・禁止の完全な表(§4.2 の、`backup`/`flash`/`restore`/`fetch`/`build` 以外)。
- ホストが、`allOf` の `if`/`then` を、どう扱うか。
- 既存のエラーコードの全件数。
- flash と restore 以外の種類(特に `derby`、`derby_fixed`、`restart`)が、デバイスのフラッシュを
  書き換えないこと。`prepare_test` の説明には、「ROM のダウンロードモードへのリセットと、
  書き込み、観測」を開示する、とある。ここでの「書き込み」が、フラッシュの書き込みなのか、
  シリアルへの送信なのかを、コードで確かめていない。`JobKind.destructive` は種類ごとの値で、
  確認の結果に応じて直せる。

## 13. 未決事項

決定済みの事項(注釈を厳しくしない、`home_recovery` は一回の呼び出し、別名の期間は 0)は、
ここから除いた。

1. 旧 `acquire_session`(作業セッションを作らない即時取得)を、`session(action=open)` に
   畳むと、作業セッションが一つできる(上限 128 に数えられる)。これでよいか。
2. `status` の `view=session` は、リースの capability を返す(今の `resume_work_session` と
   同じ)。R の注釈のまま残すが、これは注釈と効果が合っていない(§2)。M に上げる(注釈を
   厳しくする)のは、利用者の承認が要る。今のまま残すか、承認したうえで M にするか。
3. `status` の `view` の、ここでの 9 種類で足りるか。`logs` の `stream` を、別の `view` に
   分けたほうが分かりやすいか。
4. `tools/list` の文字数と本数の上限(段階 0 のテスト)を、いくつにするか。初期の案は、
   新しい面を実装した時点の値を固定し、増やす変更を、理由つきでしか許さない。
5. M のツールが 4 本(`submit`、`execute`、`cancel`、`session`)ある。同じクラスなので、
   引数の区別でまとめる案もあるが、引数の形が大きく違い(plan の作成、積み込み、取り消し、
   セッション)、一つにすると、スキーマが複雑になる。今は 4 本に分けている。さらに減らすか。

## 14. 今後の規則

- 新しい機能は、ツール面の予算に照らして、正当化する。新しいツールを足す変更には、
  既存のツールの `view` / `kind` / `op` / `target` / `action` の追加や、任意の引数では
  足りない理由を、書く。
- ツールは、安全クラス(R / M / D)でだけ分ける。新しいツールが正当化されるのは、新しい
  安全クラスがあるときだけ。クラスが違う操作は、同じツールに混ぜない。
- **どの操作の注釈も、利用者の明示的な承認なしに、今より厳しくしない。**
- `destructiveHint=true` は、デバイスのフラッシュを上書きする操作(flash と restore)の
  ためにとっておく。それ以外の操作に付けない。注釈は、ホストへのヒントであり、安全の
  境界ではない(境界は、ブローカー側の承認・所有権・単回の権限)。
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
- 注釈と効果の組み合わせの不揃い(§2): 今回は変えない(決定事項 6)。

これらは、機能の追加が先に進み、整理が後になった、通常の成長の跡と見るのが妥当だと考えて
いる。この文書の作り直しと、段階 0〜2 は、その整理にあたる。
