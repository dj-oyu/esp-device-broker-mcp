# 設計: ジョブの自由な積み込みと継続チケット

状態: 設計提案(実装なし)。この文書は `src/` を変更しない。

関連文書: MCP のツール面(5 本への作り直し)は `DESIGN-MCP-SURFACE.md` にある。この文書は、
その新しいツール面で表現する: 旧 `prepare_test` → `submit`、旧 `execute_test` →
`execute`(`op=run`)、旧 `request_device` 等のセッション系 → `session`、旧 `job_status` /
`device_status` → `status`(`view=job` / `view=device`)。§2(現状)だけは、現在のコードの
名前のまま書いている。
確認の区別: 「確認済み」はこの文書の作成時に該当コードを読んで確かめた事実。
「未確認」「推測」と書いたものは、コードで確かめていない。

## 1. 目的

開発中によくある次の操作を、素直に書けるようにする。

- 取り込み(fetch)・ビルド・flash・テストを、種類によらず、いつでもキューに積める。
  「今すぐ実行できるか」の保証とは別に扱う。
- 実行中のチェーン(例: flash → test)があっても、あとからビルド → flash → test を積める。
- 「この flash のあと、同じファームウェアのままテストを続けたい」を、別クライアントに
  割り込まれずに表現できる。
- 自分が積んだチェーンが、何番目まで進み、始まっているかを確認できる。

守る前提(`AGENTS.md`): MCP の副作用は明示的にする。backup・flash・restore は別々の承認済み
操作で、ブローカーが自分でジョブを足したり、実行したりしない。暗黙のロールバックや再試行は
しない。承認・所有権・キャンセル・秘匿の境界を保つ。

## 2. 現状(確認済み)

### 2.1 二つの経路

| 経路 | 対象 | 実行の入口 | 順番待ち |
|---|---|---|---|
| 登録プロジェクト経路(`ProfileJobs`) | fetch(`firmware_fetch_source`)、build(`firmware_build`) | principal 結び付き。物理リースなし | `profile_jobs.queue`(FIFO、単一ワーカー) |
| デバイス経路(`Broker`) | flash/backup/restore、test 類 | 物理リース必須 | リースの待ち行列 `waiters`(FIFO)と、リース内の `queue` |

`SESSIONS.md` も同じことを書いている(source/build はリースを使わない)。

### 2.2 デバイス経路の結び付き

- plan は `session_id`(リース ID)と `broker_epoch` を含む。`Broker.execute` は
  `plan_session_mismatch`、`_work` は `plan["session_id"] != self.session.id` で拒否する。
- `ExecutionIntent` は、リースの期限(`session.deadline`)までに実行を始めることを要求する。
  期限切れの intent は `execution_intent_expired` で取り消される。
- リースは `request_device` の FIFO で付与される(`maintenance_tick` が先頭を付与)。
  付与のたびに新しい capability と lease generation ができる。既定 30 秒、最大 300 秒。
  更新は明示的で、ブローカーは自動更新しない。
- 実行中のジョブがあるあいだは、リースの期限が来ても所有権は保持される
  (`lease_expired_active_job_retains_ownership`)。
- ローカルの operator は、`operator_reorder_wait` で待ちの順番を入れ替えられる。
  つまり、先頭に挿入する操作は、すでに存在する。
- 旧 `acquire_session` は即時取得のみで、待ち行列や実行中があれば `device_busy`。

### 2.3 plan は準備の時点で前提を取り込む

- build: `source_receipt` を渡すと、`authorize_receipt` が「その receipt を作った fetch ジョブが
  成功済みで、同じ principal のもの」であることを準備の時点で要求する
  (`successful_owned_source_fetch_required`)。
- flash: `build_job_id` を渡すと、準備の時点で「成功済みの自分のビルド」を要求し、成果物の
  ハッシュ(`manifest_sha256`、`expected_elf_sha256`、`expected_app_bin_sha256` など)を
  plan に埋め込む。実行時(`FirmwareFlash.execute`)には、`prepare_plan` をもう一度実行して、
  plan の全項目と一致しなければ `flash_inputs_changed_needs_review` で止める。
  さらに `verify_bundle` と各セグメントの再読込で、書き込むバイト列を検証してから書く。
- 結果として、前のジョブが終わっていない時点では、次のジョブの plan を作れない。
  ここが「先に積めない」根本原因である。
- plan の形は `plan_approval.py` が許可キーで検証している(`_BASE_KEYS` など)。新しい項目は
  ここに追加する必要がある。

### 2.4 「待たない」実装

- `CacheLock`(`source_cache_lock.py`)は、スレッドのロックとキャッシュ内のファイルロックを
  待たずに取り、取れなければ即座に `ValueError("source_fetch_busy")` を投げる。
- `SourceSnapshots.cache_guard` は、この `ValueError` を `SourceError(str(exc))` に変換する。
  したがって、取り込み実行中にビルドの `authorize_receipt`(`resolve`)が走ると、
  公開コード `source_fetch_busy` で断られる(確認済み。前提として、fetch の実行が
  書き込みロックを持つこと自体は、コードの読み取りに基づく推測)。
- `SourceReadWorker.run` も、別の source 準備が進行中なら `source_fetch_busy` を返す。
- `FirmwareSource.execute` は `self.active` なら `source_fetch_busy`。

### 2.5 理由が `invalid_request` に潰れる箇所

- `server.py` の `_result()` と、ソース準備のハンドラは、`ValueError` を理由に関係なく
  `{"ok": false, "error": "invalid_request"}` にする。ログには残らない(warning レベル)。
- `firmware_build.prepare` は、`project_must_not_be_inside_broker_state`、
  `idf_project_required`、`registered_node_16_required_for_source_project`、保存先の
  検査などで `ValueError` を投げる(確認済み)。登録プロジェクト経路のビルド準備でこれらが
  起きると、すべて `invalid_request` になる。
- デバイス経路の `Broker.prepare` は、別に `ValueError` を `BrokerError("backend_preflight_failed")`
  に変換する。経路ごとに扱いが違う。
- 実際に観測された `invalid_request` の原因は、まだ特定していない。この文書は原因の推定を
  含まない。

### 2.6 保護の仕組み(PR #79 で入った共有容量)

- `retention_evidence` は、未完了のジョブ(`unfinished_job`)が持つ `source_receipt` と、
  まだ実行可能な未着手 plan(`executable_plan`、`source_plan_deadline` までの 1800 秒)の
  `source_receipt` を、再利用不可として保護する。
- 保護されるのは**ソースのペイロード**である。ビルド成果物(build ディレクトリ)の削除と、
  待機中の flash plan の関係は、この文書では**未確認**(§9)。

## 3. 決定事項

利用者が決めた方針。選択肢ではなく、前提として扱う。

1. ジョブは、種類によらず自由にキューに積める。準備時の前提チェックは、実行時の確認に移す。
   ブローカーは、順番の組み換え(依存順への並べ替え)だけを行う。ジョブを自分で足さない。
   backup・flash・restore を自動では実行しない。
2. 継続チケット: 「後続あり」を宣言してジョブを積むと、ブローカーが使い捨てのチケットを
   発行し、すぐに返す(完了時ではなく、積んだ時点)。クライアントは、次のジョブを積む
   ときにそれを消費する。後続を宣言しないジョブがチェーンの終端。チェーン ID も、長命の
   セッション ID もない(並列エージェントが ID を管理しなくてよい)。
3. チケットの記録に必要なのは、チケット ID・親ジョブ ID・状態(issued / consumed / void)・
   期限だけ。principal、資源の種類、成果物・ファームウェアのハッシュは、消費時に親ジョブの
   記録から導く。
4. 二重消費は `ticket_consumed`。親が失敗・キャンセルされたら `ticket_void`。親の履歴が
   なくなったとき(pruned)も void。
5. クライアントごとの試行回数の制限は設けない(§4.8)。
6. 資源の種類は、fetch(ソースキャッシュ)、build(ビルドプロセス)、flash/test
   (デバイス、排他)。デバイスの順番待ちに入るのは、デバイスの手順だけ。fetch と build は
   デバイスの占有状況と無関係に進む。非デバイスの親のチケットは、成果物・依存の受け渡し
   だけを表す。デバイスの親のチケットは、デバイス保持の継続を表す。
7. デバイスの保持は、親のデバイスジョブが**終わった時点**から始まり、アイドルの期限は
   60 秒。消費した後続は、他のチェーンより優先してデバイスの待ち行列に入る(継続優先)。
   期限後は、同じ状態の維持は保証せず、後続は通常のジョブとして順番を待つ。
8. チェーン全体の予算は 30 分。ここでは「チェーンが最初にデバイスに入ってから、保持を
   含めて、デバイスを占有した時間の合計」と定義する(利用者に確認する解釈。§10)。
   超えたら、後続はデバイスの待ち行列の最後尾に回る。チェーンの間は、「最初のデバイス
   手順を積んだ時刻」の FIFO。
9. 失敗: 暗黙の再試行もロールバックもしない。失敗・キャンセルしたジョブは、未使用の
   チケットを void にする。すでに積まれた後続は、固定の理由コード(`ticket_void` /
   `chain_broken`)で失敗する。キャンセルはジョブ単位。
10. 実行時の検証: 開始時に、前のジョブの成果物のハッシュと、デバイスが期待するファーム
    ウェアのハッシュを再検証する(`FirmwareFlash.execute` の再検証の型を使う)。待機中の
    plan・チケットが参照する成果物は、キャッシュの掃除から保護する。
11. 前節の必須シナリオ(§6)。
12. 利用者への進捗表示(§7)。
13. 前提として、`ValueError` → `invalid_request` の潰れを、先に直す(§8、段階 1)。
14. MCP のツールは**増やさない**。ツール面そのものを `DESIGN-MCP-SURFACE.md` で作り直す
    (5 本)。`continues` と `after`(チケット)は、その `execute`、`submit`、`session` の
    任意の引数にする。進捗は `status`(`view=job` / `view=device`)の結果の中に入れる。
15. デバイスのチェーン保持は、ロックとして扱う。成立させる不変条件を §13 に定める。

## 4. 設計

### 4.1 準備と積み込みの分離

目標: `prepare` は、plan の「形」と、親への参照だけを固定する。成果物に依存する値は、親が
完了したときに解決する。

- 親を持つ plan は、新しい項目 `continues: {parent_job_id}` を持つ。`plan_approval.py` の
  許可キーに追加する。この項目がない plan は、今と完全に同じ(同じ plan_hash)。
- 親に依存する値(`source_receipt` の実体、flash の成果物ハッシュなど)は、準備の時点では
  埋めない。親が成功したときに、既存の `prepare_plan` 相当の処理で解決し、解決結果を
  不変の記録 `plan_resolutions(plan_hash, parent_job_id, resolved_digest)` として保存する。
  実行は、この解決結果と一致する間だけ進める(§4.6)。
- 準備時の前提エラー(`successful_owned_source_fetch_required` など)は、「親がまだ成功して
  いない」場合にはエラーにせず、ジョブを `waiting_parent` 状態で受け付ける。親が失敗
  済みなら、受け付けず、`chain_broken` で断る。
- 新しい状態 `waiting_parent` を、ジョブの状態に追加する(履歴のスキーマ変更)。
  親の成功で `queued`(登録プロジェクト経路の queue か、デバイスの待ち行列)に移り、
  親の失敗で `cancelled`(理由 `chain_broken`)になる。

この変更は plan の不変性に関わる。「準備した時点の plan を、そのまま実行する」から、
「親ジョブを名指しした plan を、親の成果物が決まった時点で解決して実行する」に変わる。
名指しされた親のバイト列は一つに決まるので、承認(明示的な `execute` 要求)の範囲は
変わらないと考えるが、要確認(§10)。

### 4.2 チケットの発行と消費

- 新しいツールは足さない(決定事項 14)。発行は `execute`、使用は `submit` の任意の引数で行う。
- 発行: `execute`(`op=run`)に任意の `continues: true` を足す。受け付けた時点(ジョブを積んだ
  時点)で、チケットを 1 枚発行して応答に含める。応答のフィールド名は
  `continuation_ticket`(短い文字列。Crockford base32 で 8 文字程度。発行時に既存と
  衝突しないことを確認する)。
- 使用: 次のジョブの `submit` に、任意の `after: "<ticket>"` を渡す。準備の時点では
  消費せず、チケットが `issued` であること、同じ principal の親であることだけを確認して、
  plan に `continues` を記録する。
- 消費: そのジョブの `execute`(積み込み)で、チケットを原子的に `consumed` にする。
  同じチケットで複数の plan を準備しても、積み込めるのは 1 件。2 件目は
  `ticket_consumed`。
- チェーンの継続: 消費したジョブが `continues: true` を宣言すれば、新しいチケットが返る。
  宣言しなければ、そのジョブでチェーンは終わる。
- 存在しない・他の principal のチケットは、同じ `ticket_invalid` を返す(存在の有無を
  principal 間で漏らさない)。

チケットの保存先は、新しい表 `continuation_tickets(ticket_id, parent_job_id, state,
expires_at)`。後続のジョブは、plan の `continues.parent_job_id` で親を指す。したがって
「前へ」も「後ろへ」も、親子のリンクをたどって導ける(後ろへの検索は、plans の履歴上限
10,000 件の範囲で、`json_extract` による。性能は未確認)。履歴の上限
(`history_full_archive_offline`)の扱いは、既存の表と同じにする。

### 4.3 デバイスの保持と継続優先

保持は、物理的な所有ではなく、**スケジューリング上の予約**である。USB は閉じたまま。
`AGENTS.md` の「実行中のものがあるあいだ、所有を手放さない」は、そのまま守る。

- 状態: `Broker` に `hold = {ticket_id, principal_id, deadline}` を持つ(メモリ上。
  ブローカーの再起動後は復元しない)。
- 開始: `continues: true` で積んだデバイスジョブが終わった(成功・失敗にかかわらず、
  終了を確定した)時点で、成功した場合に限り、保持を開始する。期限は終了時点から 60 秒。
  失敗・キャンセルなら保持せず、チケットは void。
- 効果: 保持中は、`maintenance_tick` の付与(先頭の待ちへのリース付与)を行わない。
  `session(action="open")` の即時取得と、`session(action="request")` の付与も、保持中は、そのチケットの持ち主以外には
  行わない。
- 消費: 保持中に、そのチケットを消費した後続のデバイスジョブは、待ち行列の先頭に入る
  (`operator_reorder_wait` が行っているのと同じ「先頭への挿入」)。通常のリース付与の
  手順で、新しいリース(新しい capability・generation)が、同じ principal に付与される。
- 失効: 60 秒たつか、チェーンの予算(§4.4)を超えたら、保持を解除する。後続は通常の
  順番(最後尾)で待つ。チケットは `expired`(消費時に `ticket_void` と同じ扱い、理由は
  `hold_expired`)。
- operator の上書き: `operator_release_idle` は、保持も解除できる。これは明示的な
  operator の操作。

### 4.4 チェーンの予算

「チェーンが最初にデバイスに入ってから、保持を含めて、デバイスを占有した時間の合計」が
30 分を超えたら、そのチェーンの後続は最後尾に回る(この解釈は §10 で確認)。
チェーンの先頭は、親をたどって求める。長期の飢餓を防ぐための上限であり、ジョブを強制的に
止めるものではない(実行中のジョブは止めない)。

### 4.5 チェーン間の順番

- デバイスの手順(flash/test など)の順番は、「そのチェーンの最初のデバイス手順を積んだ
  時刻」の FIFO。
- 保持中のチェーンの後続は、継続優先で、他のチェーンの先頭に入る。
- fetch と build は、デバイスの待ち行列に入らない。登録プロジェクト経路の FIFO のまま。

### 4.6 実行時の検証

- 後続の開始時に、次を確認する。確認に失敗したら、`needs_review` や再試行ではなく、
  既存と同じ理由コードで失敗させる。
  - 親が成功のままで、親の成果物のハッシュが、記録(`plan_resolutions`)と一致すること。
    flash の後続は、`FirmwareFlash.execute` の既存の再検証(`prepare_plan` との全項目一致、
    `verify_bundle`、バイト列の再読込)をそのまま使う。
  - test の後続は、直前の flash が期待したファームウェアのハッシュ(plan に記録された
    `expected_app_bin_sha256` など)と、現在のデバイスが一致すること。照合の方法
    (デバイスへの読み取りが要るか)は、既存の `expected_firmware_sha256` の検証と同じ
    手順で足りるか、コードで**未確認**。
- 実装の足場は、デバイス経路の `_work`(開始時の検証が集まっている)と、登録プロジェクト
  経路の `_work`(`_binding_live`、`_uncertain`)。

### 4.7 キャッシュの保護

- `retention_evidence` の `executable_plan` / `unfinished_job` の考え方を、待機中の
  `waiting_parent` ジョブにも広げる。待機中のジョブが `continues` で名指しする親の成果物が、
  掃除で消えないようにする。
- ソースの保護は、すでに `unfinished_job` が `source_receipt` を保護している。ただし、
  `waiting_parent` のジョブは `source_receipt` を準備時点では持たないため、親(fetch)の
  完了後に解決された receipt を保護対象に加える必要がある。
- ビルド成果物の保護は、§9(未確認)。

### 4.8 チケットの安全性と、試行回数の制限をしない理由

- チケットは、持ち運べる認証情報ではない。消費には、認証済みの principal が、親ジョブの
  principal と一致することが必須。
- 別の principal は、チケットを推測しても使えない。失敗の応答は、存在の有無を区別しない。
- 同じ principal(共有キーの利用者)は、`SECURITY.md` / `KEYS.md` の信頼モデルで、同じ
  セキュリティ主体として扱われている。同じ principal の別クライアントが、他のエージェントの
  チケットを使えても、すでに同じジョブを操作できる権限の範囲内であり、新しい権限は
  増えない。
- したがって、試行回数の制限や失敗の計数は、守るものがなく、状態(カウンタ)と調整の
  負担だけが増える。入れない。
- チケット ID の短さによる偶然の衝突は、発行時の一意性の確認で防ぐ。

### 4.9 失敗・キャンセル・再起動

- 親の失敗・キャンセル → 未使用のチケットは `void`。`waiting_parent` の後続は
  `cancelled`(理由 `chain_broken`)。実行中の後続は止めない(すでに親が成功している)。
- principal の失効(キーの失効・期限切れ)→ そのチケットは `void`。
- ブローカーの再起動 → 未使用のチケットは `void`、保持は復元しない、`waiting_parent` の
  ジョブは `cancelled`(再起動のため)。既存の方針(再起動後、待ち・所有・中断は
  `needs_review`、何も自動では再開しない)と整合させる。
- 暗黙の再試行・ロールバックはしない。各ジョブの承認は別々のまま。

## 5. 経路ごとの変更の大きさ

| 経路 | 変更 | 大きさ |
|---|---|---|
| 登録プロジェクト(fetch → build) | 準備時の前提を「親が成功なら解決、未完なら `waiting_parent`」に | 中 |
| デバイス(flash → test) | plan の `session_id` との結び付き(§5.1) | 大 |

### 5.1 デバイス経路の最大の課題: リースとの結び付き

デバイスの plan は、準備の時点でリース ID(`session_id`)に結び付く(§2.2)。別のチェーンが
デバイスを占有している間に、あとからビルド → flash → test を積む場合、flash と test の
plan は、まだ存在しないリースに結び付けられない。次の二つの方式がある。

- 方式 A(完全な先積み): チケットを持つデバイス plan は、`session_id` の代わりに
  `continues` を持ち、リースへの結び付きは、付与の時点で `plan_bindings(plan_hash,
  lease_id)` に記録する。`plan["session_id"] == lease.id` を前提にしている箇所
  (`Broker.execute`、`_work`、`ExecutionIntent`、`_execution_intent_live`、`plan_approval`、
  flash の `prepare_binding`)を、すべて見直す必要がある。変更が大きく、承認・所有権の
  境界に触れる。
- 方式 B(保持と優先のみ): チケットは、デバイスの保持と継続優先だけを担う。後続の
  デバイスジョブの plan は、チケットの保持中にクライアントが `session(action="request", after=...)`
  で新しいリースを取った後、そのリースの下で準備する。plan と lease の結び付きの不変条件は
  変わらない。クライアントは、前のジョブの終了から 60 秒以内に動く必要がある。
  この方式では、「消費」は積み込み(`execute`)ではなく、チケット付きの
  `session(action="request")`(リース付与)の時点になる(決定事項 2 からのずれ。§10)。

提案は、段階 3a で方式 B を実装し、段階 3b で方式 A を検討する、という二段階。方式 B だけで、
「flash と test の間に割り込まれない」ことは実現できる。方式 A がないと、「別のチェーンの
実行中に、デバイスの手順まで先積みする」ことはできない。

## 6. 必須シナリオ: チェーン A が保持中に、チェーン B を積む

前提: A = flash(`continues`) → test。B = build → flash → test。方式 A(完全な先積み)を
前提とする。方式 B の場合は、注記を付ける。

| 時刻 | 出来事 | 状態 |
|---|---|---|
| t0 | A の flash が実行中(チケット `TA1` 発行済み) | デバイス: A |
| t1 | B の build を積む(`continues`)。チケット `TB1` が返る | build は fetch/build 経路で**すぐ開始**。デバイスは使わない |
| t2 | B の flash を `after=TB1` で積む(`continues`)。チケット `TB2` | `waiting_parent`(build の完了待ち) |
| t3 | B の test を `after=TB2` で積む(終端) | `waiting_parent` |
| t4 | B の build が成功 | B の flash が `queued` になり、デバイスの待ち行列に入る(A の後ろ) |
| t5 | A の flash が成功。保持が始まる(期限 t5+60 秒) | デバイスは A の予約中。B は待つ |
| t6 | A の test を `after=TA1` で積む(終端) | 保持中なので、A の test は先頭に入る(B より先) |
| t7 | A の test が終わる。A は終端なので保持なし | デバイスが空く |
| t8 | B の flash がデバイスを得る。開始時にビルド成果物のハッシュを再検証 | B の成果物は、t4〜t8 の間、掃除から保護されている |
| t9 | B の flash が成功。保持が始まる。B の test が先頭に入る | A の後続はいない |

注記: 方式 B の場合、t2・t3 の flash と test は、あらかじめ積めない。B の build が終わった
あと、クライアントが `session(action="request")` で順番を待ち、リースを得てから、flash と test を
準備して積む。A の保持と継続優先は、同じように働く。

## 7. 利用者への進捗表示

要求: 自分が積んだジョブが、何番目まで進んだか、チェーンが始まったかを知りたい。

親子のリンクをたどって導く、読み取り専用のフィールド(`status` の `view=job` / `view=device`、operator の
スナップショット、ローカル GUI):

| フィールド | 意味 |
|---|---|
| `chain_position` | このジョブは、チェーンの n 番目(先頭を 1) |
| `chain_jobs_known` | 今わかっているチェーンのジョブ数 |
| `chain_open` | 最後のジョブが「後続あり」を宣言したが、後続がまだ積まれていない |
| `chain_state` | `waiting_parent` / `waiting_for_device` / `running`(n 番目) / `holding_device` / `finished` / `broken`(n 番目、理由コード付き) |
| `device_queue_position` | 自分のチェーンの前にいるチェーンの数(集計のみ) |
| `hold_expires_in_seconds` | 保持の残り(保持中のみ) |
| `chain_budget_remaining_seconds` | チェーンの予算の残り |
| `chain_total`(任意) | 最初のジョブに付ける「全部で m 個」の表示用ヒント |

- `chain_total` は拘束力のない表示用のヒント。ブローカーは、これを検証にも、順序づけにも
  使わない。最初の `execute` の任意の項目 `chain_total_hint` として受け取り、チケットの
  記録に持たせて、後続のチケットが引き継ぐ。
- 秘匿の境界: 自分の principal のジョブの詳細だけを返す。他の principal については、
  「N 個のチェーンが前にいる」のような集計のみ。ジョブ ID・内容・principal は返さない。
- ローカル GUI(`/operator/state`)は、すでに待ち行列とセッションを表示している。チェーンの
  情報を、同じ形式で追加する(operator は、既存の認証境界のまま)。

## 8. 前提となる修正: エラーの可視化(段階 1)

- `_result()` とソース準備のハンドラで、`ValueError` の理由を、**固定の語彙に限って**返す。
  語彙は、`ValueError` として投げる既知のコードを一か所(定数 `PUBLIC_REASON_CODES`)に
  まとめる。語彙にない文字列は、今までどおり `invalid_request` にする。パス・例外の
  文章・内部の値は、返さない。
- 語彙の網羅は、テストで保つ。`firmware_build.prepare` などが投げる `ValueError` の
  リテラルが、語彙に登録されているか、意図して潰すものとして明示されていることを確認する。
- `source_fetch_busy` が `ValueError` と `SourceError` の両方で現れる点は、
  `cache_guard` の変換(§2.4)で公開コードとしては一貫している。変更が必要なのは、
  `ValueError` を直接返す経路の統一であり、`CacheLock` 自体ではない。
- 「待たない」実装(`CacheLock`)は、この段階では変えない。待ち方の方針は、
  段階 3 以降で、ジョブの状態(`waiting_parent`)に移す。

## 9. 未検証

- 観測された `invalid_request` の本当の原因(§2.5)。
- ビルド成果物(build ディレクトリ)の掃除と、待機中の flash plan の関係。保護の仕組みが
  あるかを、コードで確かめていない。`firmware_build.py` の `resolve_flash_manifest` と、
  ビルドの保存先の管理(`build_locations.py`)を、段階 3 の前に読む必要がある。
- test の後続が、直前の flash のファームウェアのハッシュを、デバイスに照合する方法。
- fetch の実行が、キャッシュの書き込みロックを保持する範囲(§2.4 の推測)。
- `source_receipt` の値が、fetch の完了前に決まるか(おそらく決まらない)。
- plans の `json_extract` による後ろ向きの検索の性能(10,000 件の上限内)。
- `PlanApprovals.validate_current` が、`continues` を持つ plan をどう扱うか(許可キーの
  追加以外に、検証の変更が要るか)。
- GUI(`operator_snapshot`、`/operator/state`)の拡張の具体的な形。
- §13 の未確認: `self.session` の全参照の網羅、`device_requests` への `holding` 状態の追加可否、
  `needs_review` の終わり方とラッチの対応、デバイス単位の書き込み連番の有無、
  `Store.set_job` が状態変更の唯一の経路か、再起動時の復旧との整合。

## 10. 実装計画

| 段階 | 内容 | テスト | 互換性 |
|---|---|---|---|
| 0 | ツール面の計測と上限テスト(`DESIGN-MCP-SURFACE.md` の段階 0)。動作は変えない | `tools/list` の件数・文字数が上限を超えないこと | 動作は変えない |
| 1 | エラーの可視化(§8) | 語彙の網羅テスト。語彙外の `ValueError` は `invalid_request` のまま。パスや例外の文章が出ないこと | 応答の `error` が、既存のコードに置き換わる場合がある。`invalid_request` を判定しているクライアントは、新しい固定コードでも動くか、確認が要る |
| 2 | ジョブの種類の共通インターフェース: 「実行中か」「不確実か」の判定を、`broker.py` の列挙から、登録されたジョブの種類の一覧に | 既存のテスト一式。種類の追加で判定が漏れないことを確認する専用テスト | 動作は変えない |
| 3a | チケット(`continues`、`after`)・デバイスの保持・継続優先(方式 B)・`waiting_parent`(fetch → build) | チケットの状態遷移、二重消費、他 principal、親の失敗、再起動、保持の失効・予算、継続優先、シナリオ(方式 B) | `continues` / `after` を使わないクライアントは、plan も応答も同じ |
| 3b | デバイス手順の先積み(方式 A)。plan の `session_id` の結び付きの見直し | 承認・所有権・リース世代の境界を守るテスト。旧 plan の検証 | 要設計レビュー |
| 4 | 進捗フィールドと GUI(§7) | 秘匿の境界(他 principal の情報が出ないこと)、集計値 | 追加のみ |

旧ツール名の扱い(`DESIGN-MCP-SURFACE.md` の切り替え計画)に従う。新しい項目を使わなければ、
plan_hash も応答も変わらない。段階 3 以降も、各ジョブは別々に承認され、ブローカーは
自分でジョブを足さず、flash・backup・restore を自動実行しない。

## 11. `AGENTS.md` の境界との対応

| 境界 | この設計での扱い |
|---|---|
| 副作用の明示 | チケットは「順序と保持」だけを表す。ジョブを自動で足さない。後続は、利用者の明示的な `execute` で積まれる |
| backup/flash/restore は別操作 | 各ジョブは別々に承認・積み込みされる。保持は USB を開かない予約 |
| 暗黙の rollback・再試行なし | 失敗は `chain_broken`/`ticket_void` で終わる。再試行しない |
| 所有権・承認・キャンセル | チケットは親と同じ principal だけが使える。キャンセルはジョブ単位。operator は保持を解除できる |
| 秘匿 | 進捗は自分のジョブのみ。他は集計だけ。エラーの語彙は固定 |
| 実行中は所有を手放さない | 保持は、ジョブ終了後の予約であり、実行中の所有権の扱いは変えない |
| 暗号的に検証したバイト列を使う | 開始時に再検証する(§4.6)。LKG の原子的な確定は変えない |

## 12. 未決事項

1. デバイス手順の先積み(方式 A)を、段階 3b で行うか。それとも、方式 B(保持と優先のみ。
   消費がリース付与の時点)で、まず十分か。方式 B は、決定事項 2(次の積み込みで消費)から
   ずれる。
2. plan の解決(§4.1)で、「親ジョブを名指しした plan を、親の完了時に解決して実行する」を、
   「準備した時点の plan を、そのまま実行する」と同じ承認とみなしてよいか。
3. チェーンの予算 30 分の定義(§4.4)。「デバイスの占有時間の合計(保持を含む)」でよいか。
4. 非デバイスの親(fetch・build)のチケットの有効期限。デバイスの保持のような 60 秒では
   ないはずだが、成果物の再検証が通る間か、plan の期限(1800 秒)に合わせるか。
5. 保持中の要求の状態 `holding` と、デバイス単位の書き込み連番(§13.2 の (6))は、履歴の
   スキーマ変更を伴う。許容するか。
6. 方式 A(先積み)を段階 3b で行うか、方式 B(保持と優先のみ)で十分か(§5.1)。

## 13. mutex としての不変条件

チェーンのデバイス保持は、ロックとして扱う。ここでは、成立させる不変条件を、確認できた
実装と対応づけて定める。以下の「現状」の API 名(`acquire`、`request_device`)は、現在の
コードの名前で、新しいツール面では `session` の `action` に対応する
(`DESIGN-MCP-SURFACE.md`)。

### 13.1 前提: ロックは一つ(確認済み)

- デバイスの所有は `Broker.session`(`Session | None`)の一枠である。入口は二つあり、旧
  `acquire`(即時取得)と、`request_device` の FIFO 付与(`maintenance_tick`)だが、どちらも
  同じ `self.session` を設定する。`acquire` は `self.session or self.waiters or
  self._physical_busy()` のときは `device_busy` を返し、待ち行列に割り込めない。
- したがって、ロックは既に一つであり、チェーン保持は**第二のロックを作らず**、この一枠の
  状態として表す。

状態は、FREE / OWNING(session)/ HELD(reservation)の三つ。

| 遷移 | 条件 |
|---|---|
| FREE → OWNING | 既存の付与(即時取得、FIFO 先頭) |
| OWNING → HELD | `continues: true` のデバイスジョブが**成功**で終わり、`_physical_busy()` と `_execution_uncertain()` がともに偽 |
| HELD → OWNING | 同じ principal が、そのチケットを付けて取得(`session` の `action=request`、`after`)。新しい Session(新しい capability・generation)。同じ tick で HELD を解消 |
| HELD → FREE | 60 秒の期限、チェーンの予算超過、operator の解除、親の void、再起動 |

`reservation = {ticket_id, principal_id, work_session_id, deadline}`。`deadline` は
`Broker.clock`(既定 `time.monotonic`。確認済み)で測る。「枠が占有されている」は、
`session is not None or reservation is not None`。この条件を参照する場所:
`acquire` の `device_busy`、`maintenance_tick` の付与条件、`operator_release_idle`、
`status()`、`operator_snapshot`。(これらの網羅は、`self.session` の全参照を洗って確認する
必要がある。未確認。)

### 13.2 項目ごとの対策

(1) **先頭ブロック(head-of-line blocking)**
- 問題: 依存が満たされていないジョブが、待ち行列の先頭にいて、実行できる後続をブロックする。
- 対策: デバイスの待ち行列に入るのは、依存が満たされた(eligible)ジョブだけ。
  `waiting_parent` のジョブは、デバイスの順番を持たない。親が成功した時点で、eligible になり、
  順番(キー = そのチェーンの最初のデバイス手順を積んだ時刻)に従って、**ソート挿入**する
  (末尾に追加ではない)。eligible の中は、このキーの FIFO。
- 場所: `request_device`(`self.waiters.append`)と `maintenance_tick`(`self.waiters[0]`)。
  `DeviceWait` に順序キーを持たせ、挿入位置を決める。
- 確認: 現状は、待ち行列の要素 = クライアントが `request_device` を呼んだもの、という
  暗黙の前提で、eligible かどうかの概念はない(確認済み)。順序キーの導入は、新規の設計。

(2) **hold-and-wait の回避**
- 問題: デバイスを持ったまま、別の競合する資源を待つ(または取れずに失敗する)。
- 対策: デバイスを取る前に、デバイス以外の入力(成果物のバイト列、ハッシュ、解決結果)を
  すべて取得・検証する。eligible の条件に「入力の解決と検証が済んでいる」を含める。
  デバイスを持っている間は、待たないロック(`CacheLock` の `source_fetch_busy` など)を
  新たに取らない。開始時に行うのは、メモリ上のバイト列とハッシュの比較と、競合しない
  ローカルファイルの読み込みに限る。
- 場所: `FirmwareFlash.execute` は、`prepare_plan`、`_known`/`_backup`、`_build_context`、
  `verify_bundle`、セグメントの再読込を、デバイス操作(`_execute`)の前に行っている
  (確認済み)。ただし、これはすでにリースを持った状態で行っている。リースの下で行う
  部分(ローカルファイルの読み込み。最大 16 MiB)を、eligible になる前に前倒しできるかは、
  段階 3 の設計で決める。
- 確認: flash の経路は、`CacheLock` も `_receipt_read_lock`(ログ読み取り用。
  `firmware_build.py` で `read_build_logs` の経路のみ)も取らない(`grep` で確認済み)。
  `resolve_flash_manifest` が内部で取るロックの全体は、未確認。

(3) **二つのロックの重複**
- 問題: 旧 `acquire_session` のリースと、ワークセッションの FIFO(`request_device`)に、
  チェーン保持が加わり、三つ目のロックになる。
- 対策: §13.1 のとおり、ロックは一つ(`self.session` の一枠)。保持は、その枠の状態
  `HELD`。即時取得は、`HELD` のあいだは `device_busy`。FIFO の付与も、`HELD` のあいだは
  行わない(持ち主のチケットの消費を除く)。
- 場所: `acquire`、`request_device`、`maintenance_tick`。
- 確認できなかったこと: 保持中の要求の状態 `holding` を、履歴の `device_requests` の状態に
  追加できるか(`set_request` が状態を制限しているか)は、`store.py` を読んでいない。
  未確認。また、`_end_ownership` は、所有を終えるときに、キューに入っている intent を
  すべて取り消す(確認済み)ため、`OWNING → HELD` の遷移では、同じリースに積まれた
  後続の intent を、取り消さずに引き継ぐ必要がある。この扱いは、方式 A/B の違いにも
  関わる(§5.1)。

(4) **`AGENTS.md`: 実行中のものがあるあいだは、所有を手放さない**
- 対策: 保持の期限切れによる解放は、デバイスの作業が静止している(`not _physical_busy()`)
  ときだけ行う。`_end_ownership` は、`_physical_busy()` が真なら例外を出す(確認済み)。
  期限切れの処理も、同じ条件で行う。
- 不確実な終了(`_execution_uncertain()` が真、または `needs_review`)の場合: チケットは
  void。保持は始めない。デバイスは、人のレビューが終わるまで、他のチェーンに渡さない。
- 場所: `maintenance_tick` の付与条件は、`_execution_uncertain()` が真なら付与しない
  (確認済み)。これは実行時のラッチで、オフラインのレビューでは消えない(コメントに
  明記)。ただし、個々のジョブの `needs_review`(履歴)は、このラッチを自動では立てない
  可能性がある。すべての `needs_review` の終わり方が、ラッチを立てるかは、**未確認**。
  立てない終わり方があるなら、そのときだけ保持が「レビュー待ち」の状態で付与を止める
  仕組み(既存の `recovery_review_required` と同じ扱い)を足す。

(5) **原子性**
- 対策: 「チケットの消費」「保持の引き継ぎ」「期限の判定」「他のチェーンへの付与」は、
  一つの同期的な区間で決める。ブローカーはシングルのイベントループで動き、状態遷移は
  `maintenance_tick` のような、`await` を含まない関数で行われている(コメントに明記。
  確認済み)。新しいロックは作らず、同じ性質を保つ。時刻は `Broker.clock`(単調時計)だけを
  使い、区間の中で一度だけ読む。
- 場所: `request_device`(チケット付きの取得)、`maintenance_tick`(期限切れと付与)。
- 確認: 現状は、明示的なロックはなく、「`await` を挟まない」ことで直列化している(確認済み)。
  これを壊さないことを、テストで保証する(§13.3 の 12)。

(6) **フェンシング**
- 問題: 保持が切れた間に、別のチェーンがファームウェアを書き換えたあとで、後続が走ると、
  想定と違うファームウェアでテストが実行される。
- 対策: チケットを消費するジョブは、開始時に必ず、デバイスのファームウェアのハッシュが、
  前のジョブの結果(flash が書いたイメージのハッシュ)と等しいことを確認する(省略不可)。
  保持が切れていても、ハッシュが一致すれば実行してよい。一致しなければ、
  `device_state_changed` で失敗する。
- 場所: `_work` の開始時の検証(デバイス経路)。結果のハッシュは、前のジョブの plan の
  `expected_app_bin_sha256` から導く。
- 確認できなかったこと: 「デバイスの現在のハッシュ」を、何で知るか。(a) ブローカーが記録した
  最後の成功した書き込み(`firmware_current(device_id, principal)` は、**principal ごと**の
  記録で、他の principal の書き込みは見えない可能性がある。未確認)。(b) デバイス自身の
  報告(既存の実行時の識別の確認。「観測した識別は認証ではない」と `prepare_test` の説明にある)。
  principal をまたいで、他のチェーンの書き込みを検出するために、デバイス単位の書き込み連番
  (flash/restore のコミットごとに増える。新規の状態)を、チケットが親の終了時点の値と
  比較する案を提案する。ハッシュの確認に加えて必須とする。

(7) **非デバイスの後続が、デバイスの親のチケットを消費した場合**
- 対策: 消費の時点で、保持を解放する(チケットは、成果物の受け渡しだけになる)。
- 場所: `ProfileJobs.execute`(fetch/build の積み込み)。チケットの消費と、保持の解放を、
  同じ区間で行う。

(8) **チェーンの予算**
- 対策: 30 分は、保持の**アイドルの時間を含む**デバイスの占有時間の合計。親のリンクを
  たどり、各デバイスジョブの実行時間と、親の終了から子の開始までの保持の間隔を、足し合わせて
  導く(保存しない)。保持が切れたあとの間隔は、数えない。
- 場所: 親子のリンクをたどる関数(§7 の進捗と共通)。
- 確認: ジョブの開始・終了の時刻が、履歴から取れるか(`created` と `finished` は
  `retention_evidence` で使われている。確認済み。開始時刻の有無は未確認)。

(9) **ブローカーの再起動**
- 対策: すべてのチケットを void にし、保持を解放する。保持はメモリ上のみ。チケットの
  void は、親の plan の `broker_epoch` が現在の `Broker.epoch` と違うことで判定する
  (epoch は `Broker` のインスタンスごとの `uuid4`。確認済み。`retention_evidence` と同じ
  同一 epoch の考え方)。
- デバイス: 再起動前に OWNING/WAITING だった要求は、`SESSIONS.md` のとおり
  `needs_review` になり、自動では再開しない。保持中(HELD)だけの要求は、デバイスの作業が
  進行していないので、`needs_review` にせず、`broker_restart` の理由で解放する案。
  この扱いが、既存の復旧の手順(`review-request`)と整合するかは、未確認。

(10) **キャンセル**
- 対策: 実行中のジョブをキャンセルすると、保持は始まらず、チケットは void(§13.2 (4) と同じ)。
  キュー内のジョブをキャンセルしたときも、そのジョブが発行した未使用のチケットを void に
  する。`waiting_parent` の後続は、`ticket_void` で失敗する。キャンセルはジョブ単位のみ。
- 場所: ジョブの状態が終端(`cancelled`、`failed`、`succeeded`)になる箇所は多い
  (`Broker.cancel`、`_cancel_queued`、`_cancel_execution_intent`、`ProfileJobs.cancel`、
  `operator_cancel`、`_work` の各分岐)。個別に直すと、漏れが出る。`Store.set_job` を、
  終端状態への遷移の**単一の出口**として、そこでチケットの void/保持の判定を行う案。
  `set_job` が、状態変更の唯一の経路かは、**未確認**(`add_job`、`enqueue_requested` など、
  別の書き込み経路がある)。

(11) **秘匿**
- 対策: 他のチェーンには、集計だけを見せる。「保持中、残り N 秒」「自分のチェーンの前に
  N 個のチェーン」。他の principal の、ジョブ ID・内容・principal は返さない。
- 場所: `status()`(`device_status`)。現状の `status()` は、`owner` に origin(`Origin`)
  だけを返し、principal は返さない(確認済み)。保持の集計を、同じ形で足す。
  operator のスナップショットは、既存のローカル認証の境界のまま。

### 13.3 テスト向けの不変条件のチェックリスト

1. 枠(`session` と `reservation`)の保持者は、常に 0 または 1。2 つの保持者は存在しない。
2. `reservation` が立つ経路は、すべて、期限(`deadline`)を持ち、その期限までに、解放の
   経路(期限切れ、消費、void、operator の解除、再起動)のどれかに必ず到達する。
   解放を経ずに、`reservation` が消える経路はない。
3. デバイスは、eligible でないジョブ(`waiting_parent`、入力が未解決)には、付与されない。
4. eligible なジョブの中で、付与の順序は、順序キーの FIFO。保持中のチェーンの後続は、
   その前に入る。
5. `waiting_parent` のジョブは、デバイスの待ち行列の位置を持たない。
6. 保持の解放(期限切れによるもの)は、`_physical_busy()` が偽で、`_execution_uncertain()` が
   偽のときだけ起こる。
7. 不確実な終了・`needs_review` で終わったジョブは、保持を始めず、チケットを void にし、
   レビューが済むまで他のチェーンにも付与されない。
8. チケットの二重消費は、必ず `ticket_consumed`。他の principal・存在しないチケットは、
   区別できない `ticket_invalid`。
9. 親が `failed`/`cancelled`/履歴から消えた(pruned)とき、そのチケットは必ず void で、
   `waiting_parent` の後続は、必ず `chain_broken`/`ticket_void` で終わる。
10. チケットを消費するジョブは、開始時に、ファームウェアのハッシュ(と、デバイス単位の
    書き込み連番)を必ず確認する。保持が切れていても、一致すれば実行され、一致しなければ
    `device_state_changed` で失敗する。この確認を省略できる経路はない。
11. 非デバイスの後続がデバイスの親のチケットを消費すると、その時点で保持が解放される。
12. 消費・引き継ぎ・期限の判定・他のチェーンへの付与は、`await` を含まない同期的な区間の
    中で完了する。時刻は、区間の中で一度だけ読む。単調時計が戻らない(注入した時計で検証)。
13. チェーンの予算は、保持のアイドルを含む占有時間の合計。超えたあとの後続は、最後尾に
    回る。実行中のジョブは止められない。
14. ブローカーの再起動のあとは、すべてのチケットが void。保持は復元されない。
    中断されたデバイスの作業は、既存の `needs_review` の扱いに従う。
15. 他のチェーンに見える情報は、集計(保持の残り秒数、前にいるチェーンの数)だけ。
    ジョブ ID・principal・内容は漏れない。
16. ジョブが終端状態になる経路のうち、チケットの判定(void または保持の開始)を通らない
    ものはない。
17. 保持中は、即時取得も、他の principal の `session` の `action=request` も、枠を取れない。
18. 保持は USB を開かない。保持の開始・終了で、デバイスへのコマンドは送られない。
