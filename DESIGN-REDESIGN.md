# 設計: 内部構造の作り直し(段階 0: 契約の棚卸し)

状態: **確定**(コード変更なし。§5 の決定事項は 2026-10-25 に利用者が確認)。対象: `main` の `5d79d2d`(0.13.0、未タグ)。

0.13.0 で外向きの面を 6 ツールに絞った。今回は、その内側を作り直す。外から見える振る舞いと、
既存環境の状態の形式は変えない。この文書は、守る契約、見つかった穴、目指す構造、移行の順番を決める。

詳しい台帳(英語、file:line 付き)は `docs/redesign/contracts/` に置く。

| 台帳 | 範囲 |
| --- | --- |
| `surface.md` | 6 ツールの入出力、注釈、理由コード、stdio/HTTP の差 |
| `jobs.md` | ジョブの状態遷移、チケット/hold、所有権、キャンセル、`_work` の許可判定 |
| `flash.md` | フラッシュ/リストア/バックアップの手順、LKG のコミット、保持 |
| `redaction-auth.md` | 秘匿情報除去、公開エラー、主体とスコープ、子プロセス環境 |
| `persistence.md` | SQLite・JSON・TOML のすべての永続化形式と、旧形式の読み取り |

台帳は読み取りだけの調査で作った。各項目の「SPEC」「speculation」は、コードから推測しただけで、実行していないもの。

## 1. 守る契約

### 1.1 外向きの面(`surface.md`)

- MCP ツールは `status` / `submit` / `execute` / `flash` / `cancel` / `session` の 6 本だけ。旧 25 本の `@op` は、
  内部の `ops` 辞書(`server.py:596-599`)の入口にすぎない。したがって、旧層は外部の契約を変えずに畳める。
- 入力は、どのツールも `{"request": {...}}`。余分なフィールドは拒否する。
- 注釈: 読み取り専用は `status` だけ、破壊的は `flash` だけ。`openWorldHint` はすべて false。
  `idempotentHint` は付けない。
- 出力: `status` は `json.dumps(sort_keys=True)`。ほかの 5 本は SDK の indent=2 で、挿入順のまま。
  `status` には outputSchema がない。ほかの 5 本は、開いたオブジェクトの schema を持つ。
- 拒否は二種類ある。
  - 形の誤り: `invalid_tool_request`(`isError=true`)。
  - 規則の誤り: `argument_*` と field(`isError=false`)。
  - 例外: `status(view=capture)` だけは、ドメインの誤りでも `isError=true` を返す。
- `listChanged` は、どちらの経路でも false。stdio アダプターは、転送の前に毎回 `status(view=identity)` を呼び、
  その応答の形に依存している。Tunnel は 6 本の名前を、GUI は `$defs` の `Kind` と `View` を前提にしている。
- 理由コードの語彙(`PUBLIC_REASON_CODES`)。ただし、チケットとアプリセッションの
  コード(`ticket_invalid`、`stale_boot`、`conflict`、`rate_limit` など)は、`BrokerError` 経由で外に出ているのに、
  語彙に入っていない(§2.4)。

### 1.2 ジョブ・チケット・所有権(`jobs.md`)

- **状態を直列化しているのは、「確認と書き込みの間に `await` を挟まない」ことだけ。** ジョブ・チケット・hold には
  ロックがない。どの再編成でも、この同期区間を壊さないことを最優先の不変条件とする。
- 状態遷移の正当性は、書き込みの直前の Python 側の読み取りに頼っている。`Store.set_job`(`store.py:459`)は、
  今の状態を見ない。呼び出しは 36 か所。SQL で状態を確かめているのは、次の書き込みだけ。
  - `finish_firmware`
  - `finish_fixed_trial`
  - `consume_home_attempt`
  - `finish_home_recovery`
  - 移行(`awaiting_approval` の書き換え)と `recover()`
- 旧行の `awaiting_approval` は、起動時の移行(`store.py:155`)と `recover()`(`store.py:178`)で消える。
  読み取り側に残っている分岐は、実行時には到達しない。ただし、旧 DB を開ける限り、移行は残す。
- `self.session` に代入しているのは 4 か所(`broker.py:232/715/836/1313`)。解放は `_end_ownership`(`broker.py:600`)だけ。
  物理的な作業中(`_physical_busy()`)は解放を拒む。プロファイルのソース/ビルドは、意図して busy に数えない。
- `_grant_job_lease` の中で、`maintenance_tick` が再入する(tick の中で tick が走る)。
- `_work` の許可判定は 17 項目あり、順番に意味がある(`jobs.md` §5)。特に注意が要るのは次の 2 つ。
  - 許可判定の途中の `_expire()`(`broker.py:2348`)。
  - `job_not_found` が `execution_uncertain` のラッチを立てること(`broker.py:2337`)。
- キャンセルの入口は約 17 か所あり、それぞれに所有権の確認と止め方がある(`jobs.md` §4)。

### 1.3 フラッシュ・LKG・リストア(`flash.md`)

- **書き込みの経路(`FlashLifecycle.run` → `driver.write`、`flash_lifecycle.py:294`)は変えない。**
  `run` の中の順番は次のとおり。
  1. busy の確認
  2. ダイジェストと引き渡しの確認
  3. probe とセキュリティ
  4. 回復用バイトの確認
  5. 書き込み前の現行ファームウェアのフェンス(回復点を明示したときだけ。§5.3)
  6. 書き込み
  7. readback
  8. 起動の ELF/IDF の確認
  9. 確認付きのクローズ
  10. クローズ後のキャンセル確認
- LKG を選ぶ書き込みは `Store.finish_firmware`(`store.py:614-687`)だけ。1 つの `BEGIN IMMEDIATE` の中で、
  次をまとめて行う。ガードは、トランザクションの中で 2 回走る。
  - ジョブを succeeded にする
  - 要約を挿入する
  - `device_writes.seq` を進める
  - 履歴を書く
  - `firmware_lkg` を upsert する
- 再編成で守ること:
  - 書き込みの処理が戻ってから `commit_success` までに、`await` を挟まない。
  - ガードは、同じ接続のトランザクションの中で走らせる。
  - `defer_commit=True` を残す。
  - 不確定のラッチを持つエンジンは、busy/uncertain の確認の後でしか差し替えない。
- 計画ダイジェストには、リセット/再オープンの説明文(`FLASH_EFFECTS`)が文字どおり入っている。
  文言を変えると、保存済みの計画がすべて無効になる。
- リストアは、凍結した 3 種類の回復(全体バックアップ、LKG のアーティファクト一式、LKG の全体バックアップ)のどれかに、
  受領書で結び付く。バックアップ、フラッシュ、リストアは、別々の明示的な操作のまま。
- 保持: バックアップと LKG オブジェクトを消すコードはない。ビルドの出力は、同じ枠の再ビルドで上書きされうる。
  そのため、フラッシュの時点でバイトを内容ハッシュの置き場へ写す。

### 1.4 秘匿情報除去と認証(`redaction-auth.md`)

- 外に出る経路: MCP の結果、HTTP、stdio、operator API、ログ、GUI。
- 今のエラー処理の形:
  - `server.py` の 4 つの except ラダー(448/705/826/1090)は同じ形。
    - `BrokerError` は `str(exc)` をそのまま返す。
    - `SourceError` は `exc.public()` を返す。
    - それ以外の `ValueError` は、完全一致の許可リスト(`public_reason`)に通す。
  - `surface._convert`(`surface.py:300`)は別の形で、`argument_invalid` と field 名を返す。
  - `SafeMCPServer`(`server.py:169`)は、それ以外の例外を `invalid_arguments` にする。そのため、内部の失敗が
    引数の誤りとして報告される。
  - operator API は、`BrokerError` を 200、`ValueError` を 400 で返し、許可リストがない。
- 主体の確認: `principal(ctx, scope)` を 36 か所で呼んでいる。ラッパーは 5 つで、そのうち `read` / `owned` /
  `acquire_owned` だけが `auth_required` の off を尊重する。
- 鍵: 要求ごとに読み直し、キャッシュしない。失効・更新・監査ログは、アトミック。
- 子プロセスの環境: ビルドは `OS_ENV`/`ALLOWED_ENV`、フラッシュは `OS_ENV`、Tunnel は `SAFE_ENV` に資格情報 2 つ。
  MCP の要求が運べるのは、`Identifier`/`Digest` だけ。プロジェクト・プロファイル・ツールは、ローカルの TOML から来る。

### 1.5 永続化形式(`persistence.md`)

既存環境の状態は移行しない。読み取りの互換を保つ。

- `history.sqlite3`:
  - 23 テーブル。スキーマ版はない(`user_version` は 0)。
  - 変更は、`CREATE TABLE IF NOT EXISTS` と、その場しのぎの 3 つの手順で行っている。
    - `diagnostic` 列の追加
    - `operation_migrations` のマーカー
    - `timeline_order` のトリガーの有無
  - 時刻は、TEXT の `CURRENT_TIMESTAMP` と REAL の `time.time()` が混ざっている。
- **計画は、保存の文字列とハッシュのバイト列が別。** 保存は `json.dumps(sort_keys)` に既定の区切り(`store.py:370`)、
  ハッシュはコンパクトな正規化バイト列(`broker.py:177`)。厳格なモデルに置き換えるときも、**検証だけにして、
  書き直さない。** 浮動小数の表記、既定値の追加、キーの変更があると、保存済みのハッシュがすべて合わなくなる。
- **設定モデルは `extra="forbid"` と `frozen=True`。登録ダイジェストは `model_dump_json` のハッシュ。**
  `SourceRepository` / `BuildProject` / `FlashDevice` にフィールドを 1 つ足すだけで、すべてのダイジェストが変わる。
  その結果、既存の計画と受領書がフェイルクローズで弾かれる。フィールドの構成と並びは固定する。
- `state_dir` の解決: 相対パスは、プロセスの cwd に対して解決する。パスから作るハッシュが 3 つある。
  - キャッシュロックの所有者
  - ビルドの `root_id`
  - `build_artifact_references.root_sha256`
- `keys.sqlite3` は state_dir と別の場所(`%LOCALAPPDATA%\DeviceBroker`)にあり、`user_version=1` を厳格に要求する。
- 旧形式の読み取り:
  - 受領書 schema_version 2
  - DERBY v1 の計画
  - J1 の例外
  - export のないフラッシュ計画
  - 平らな `tunnel.local.toml`
  - `build_location` のないマニフェスト
  - LKG の `compat` の欠落/null

## 2. 見つかった穴

作り直しは、**振る舞いを変えない**ことを原則にする。下の穴は、作り直しに混ぜない。先に、今の振る舞いを
固定するテストを書く。そのうえで、直すものは、別の小さな PR で、直す前後の差をテストで示して直す。

### 2.1 正しさ

1. **チケットのコミットが分かれている。** `consume` は、のぞき見てから `UPDATE` し、rowcount を見ない。
   `execute_deferred` は、`deferred.add` / `consume` / `note_consumer` を別々にコミットする。
   `consume` の直後にクラッシュすると、チェーンのつながりが空のまま残る。
2. **フェンスの穴。** `fence_seq` は、親が終わる時点でチケットがまだ使われていないときだけ付く(`broker.py:1050`)。
   親の実行中に使われたチケットには、hold も優先もフェンスもかからない。`request_device(after=)` の経路は、
   フェンスを見ない。`DESIGN-CONTINUATION-TICKETS.md` §13.3(10) と食い違う。
3. **成功の上書き。** `_work` の except の分岐(`broker.py:2875/2877`)は、コミット済みの成功を上書きできる(SPEC)。
4. **プロファイルビルドはアトミックでない。** 成果物をディスクに出す(`firmware_build.py:1069-1072`)のが、
   `succeeded` の書き込み(`profile_jobs.py:658`)より先。
5. (取り下げ)「`expected_app_bin_sha256` を実行時に確かめていない」は誤り。この値は、バンドルのセグメントの
   ハッシュから作る(`firmware_flash.py:387`)。セグメントは、確かめ済みの `manifest_sha256` に含まれる。
   LKG のメタデータと、collector の結び付き(`collector_binding.py:147/256`)で照合している。
6. **時計が 2 つ混ざっている。** hold の期限は差し替えられる `Broker.clock` で、経過時間・チケットの TTL・
   チェーンの予算は `time.time()` と SQLite の 1 秒刻みの時刻を比べている。
7. **ジョブのリースは更新できない**(最大 300 秒)。長い先積みのフラッシュや DERBY が、途中で権限を失いうる(SPEC)。
8. 再起動時に生きていた先積みジョブの待ちは、`needs_review` の行になり、ホームリカバリを止める。テストがない。

### 2.2 秘匿情報・認可

1. **`BrokerError` の文字列は、許可リストを通らない。** 任意の `ValueError` を `BrokerError` に包み直している
   箇所が 9 つある(`broker.py:895/985/1114/1521/2207/2216`、`profile_jobs.py:292/382`、`server.py:1056`)。
2. **呼び出し側の入力が返る(再現済み)。** `surface._convert` は、`cursor` の未知のキーの名前を、
   `field` としてそのまま返す。`status` の `cursor` は自由な辞書(`surface.py:155`)なので、`status(view=logs)` から届く。
   返るのは呼び出し側自身の入力で、他人のデータは出ない。`input_diagnostics` の規則(呼び出し側が選んだ名前は返さない)に反する。
3. **`cancel(target=job)` でセッションなしのとき、`shared_job` を通らない**(`surface.py:706-714`)。
   旧経路では除いていた DERBY の来歴や診断が、そのまま返る(コードで確認)。`cancel_job_only` はジョブの持ち主しか
   通さないので、他人には出ない。キャンセルの時点では、診断がまだ埋まっていないことが多い。
4. **ツールの取り違えのガードは、認証が必須のときだけ動く。** 主体のキーを持たない普通のデバイス計画は、
   `flash` に渡すと、`wrong_flash_tool` にならずに積まれる可能性がある(推測)。逆向き(フラッシュの計画を
   `execute` に通すこと)は塞がれている。
5. **認可より前に、情報を漏らす判定がある。** `continues` の判定と、`cancel` の `job_plan_mismatch` は、
   主体を確かめる前に store を読む。
6. `SUPPRESSED_REASON_CODES` は、実行時には使われていない。`profile_or_principal_no_longer_valid` は
   ここに載っているのに、`BrokerError` として外に出ている。
7. フラッシュとソースのログには、パスの除去がない(ビルドのログにはある)。
8. Windows の鍵ファイルの ACL は確かめていない(運用の前提として文書化されている)。git チェックアウトの中の
   鍵レジストリを拒む処理はない。

### 2.3 テストのない契約(主なもの)

- 6 ツール:
  - `argument_invalid`、`job_plan_mismatch`、`continuation_error`
  - `wrong_execute_tool` / `wrong_flash_tool`
  - outputSchema、本文の直列化、明示的な null
- ジョブ:
  - `cancel_job_only`、`promotion_failed`、`wait_cancelled`、`ticket_limit`
  - チケットの TTL の 1800 秒
  - `_work` の許可判定の理由コードの一部
- フラッシュ:
  - readback の不一致、LKG の書き込み前の不一致、`lkg_bytes_changed`
  - CAS オブジェクトの衝突、バックアップの保存失敗
  - ロールバック後の書き込み番号が変わらないこと
- 秘匿情報:
  - 6 ツールの `status` に再開トークンが出ないこと(今はブローカー層でしか試していない)
  - GUI の表示エラーでの私的パス
  - stdio の実通信での annotations の透過
- 永続化:
  - `timeline_order` の後付けの補完
  - `build_location` のないマニフェスト
  - LKG の `compat` の欠落/null
  - バックアップの付帯ファイル
  - 計画の保存文字列とハッシュバイトの差
- PR #84 のテスト削減で消えたガード:
  - 書き込み衝突のとき、どのファイルも触らないこと
  - stdio の実サブプロセスでの秘匿情報除去
  - 鍵の読み取りと失効
  - hold の FIFO
  - レイアウト受領書のリンク/reparse の拒否
  - ビルドの場所の境界(ルート、長いパス、空白)

## 3. 目指す構造

層を下から上へ並べる。**下の層で網羅的にテストし、上の層は薄い配線のテストだけにする。**

| 層 | 責務 | 今の場所 → 新しい形 |
| --- | --- | --- |
| 永続化 | SQL はここだけ。状態遷移を SQL で保証する | `Store` に加えて、散らばった SQL(`continuation.py`、`operator.py:37` など) → `Store` と `JobLedger` |
| 遷移 | 正当な遷移の表。`transition(job, from, to)` は、SQL の `WHERE state IN (...)` と rowcount で確かめる | `set_job` の 36 か所 → `JobLedger.transition()` |
| 所有権 | `self.session` の唯一の代入元。解放は `_end_ownership` 経由だけ | 4 か所 → `LeaseManager` |
| キャンセル | 1 つの入口で、対象の種類ごとの止め方に振り分ける | 約 17 か所 → `Broker.cancel_job` と種類ごとの止め方 |
| 実行 | 許可判定、種類ごとの実行器、終了処理 | `Broker._work`(約 600 行) → `admit()` / `JobKind` の登録表 / `finish()` |
| 書き込み権限 | 書き込み前の判定を 1 つに | enqueue / dispatch / `flash_live` の 3 つのほぼ複製 → `ExecutionAuthority`(差を意図して残す) |
| 関所 | 外に出るエラーを 1 か所で公開形にする。主体とスコープの検査も 1 か所に | 5 つの except ラダーと 5 つのラッパー → `PublicError` / `to_public()` と `require(ctx, scope)` |
| 面 | 6 ツール。引数の規則を検査して、下に渡す | `surface.py` と旧 `@op` 層 → `surface.py` だけ |

既存の関所は変えない:

- 書き込みの開始: `FlashLifecycle.run` → `driver.write`
- LKG のコミット: `Store.finish_firmware`
- ビルド/フラッシュの起動: `BuildProcess`
- デバイスの解放: `_end_ownership`

**同期区間の規則。** 確認と書き込みを、1 つの同期関数(`await` を含まない関数)にまとめる。区間を明示した関数名を付け、
その中に `await` がないことをテストで機械的に確かめる(例: AST で `async def` を禁止する)。
`admit()` と `finish()` は同期関数にする。待つのは実行器の中だけにする。

## 4. 移行の順番

段階ごとに PR を分ける。各段階の前後で、統合テストを回す。デバイスに触れる段階(段階 3)は、実機で確かめる。
フラッシュ/リストアの実機チェーンは、種類ごとに明示の承認を取ってから行う。

| 段階 | 内容 | 振る舞い | 実機 |
| --- | --- | --- | --- |
| 0 | この文書と台帳 | 不変 | 不要 |
| 0.5 | **今の振る舞いを固定するテスト**(§2.3 のうち、作り直しで壊れうるもの)。PR #84 で消えたガードのうち必要なものを、下の層のテストとして戻す | 不変 | 不要 |
| 1 | 不要コードの削除(台帳は `handover` の記録。削除前に再 grep する)。HOME-line は単独の PR にする | 不変 | HOME-line だけ読み取りで確認 |
| 2a | `PublicError` / `to_public()`。§2.2 の 1 と 6 は、外に出ている理由コードを語彙に加えて、今の出力を保つ | 不変(語彙の追加だけ) | 不要 |
| 2b | `JobLedger.transition()` と、散らばった SQL の `Store` への集約 | 不変。不正な遷移は今まで黙って書いていたが、記録してから拒否する | 不要 |
| 2c | `LeaseManager`、`Broker.cancel_job`、`require(ctx, scope)` | 不変 | 不要 |
| 3a | `_work` の分割(`admit` / 登録表 / `finish`)と `JobKind` | 不変 | **要**(書き込みなしの hello→observe) |
| 3b | `ExecutionAuthority` | 不変 | **要**(フラッシュは明示の承認後) |
| 3c | 旧 `@op` 層を 6 ツールに畳む | 不変(tools/list も同じ) | 不要 |
| 4 | 周辺(`broker_link.py`、永続 JSON を検証だけの厳格モデルへ、小モジュールの統合)。Tunnel の監督の統一は、最後 | 不変 | Tunnel だけ確認 |
| 修正 | §2.1 と §2.2 の穴を 1 つずつ別の PR で | **変わる** | 内容による |

`tools/list` が変わらない限り、ChatGPT 側の Refresh は要らない。段階ごとに、`tools/list` の名前・schema・annotations を、
直前の版と機械的に比べる。

## 5. 決定事項(2026-10-25、利用者の確認済み)

1. **段階 0.5 を入れる。** 作り直しの前に、今の振る舞いを固定するテストを足す。`_work` の分割は、
   テストがない理由コードが多く、壊れても気付けないため。PR #84 で消えたガードのうち必要なものも、ここで下の層のテストとして戻す。
2. **穴は 2 つに分けて直す。**
   - 秘匿情報の 2 と 3(入力の反射と `shared_job` の迂回)は、小さく閉じているので、作り直しの前に直す。
   - それ以外は、作り直しの後に直す。関所と遷移表ができてからのほうが、小さく直せる。
3. **フェンスの穴(§2.1 の 2)は、実装を設計書 §13.3(10) に合わせる。** 親の実行中に使ったチケットにもフェンスをかける。
4. **ツールの取り違えのガード(§2.2 の 4)は、認証なしのときも動かす。** モックの利用者にも、同じ拒否を返す。

未決のまま残すもの: GUI のロングポーリングの層(約 1,380 行)を定期ポーリングにするか。段階 4 の前に改めて確認する。

### 5.1 その後の決定と変更(2026-10-25)

- **LKG は実機単位にした(PR #88、マージ済み)。** 現在の LKG と履歴の検索は、主体で絞らない。焼く権限は、
  スコープ、登録の `allow_*`、書き込み前のフェンスで決まる。記録した主体は残る。`full_backup` 種の LKG は、
  バックアップを持ち主として内部で読む。他の主体のバックアップのジョブを直接指定することは、今も拒否する。
  理由: 現在の指し先は 1 台に 1 行なのに、検索だけ主体で絞っていたため、別の主体(新しいローカルの鍵と、
  Tunnel の一時的な主体など)からは、検証済みの状態があるのに「使える LKG がない」ように見えていた。
- **`status(view=device)` に `connection` を足した(PR #88)。** `hardware_connected` は「ブローカーがポートを
  開いているか」で、待機中は常に false。これを実機が抜けていると読む誤りがあった。`serial_port_open` と
  その意味、ポートを開かない USB の列挙による `usb_presence` を返す。
- ~~**最初のフラッシュには、今も全体バックアップが要る。**~~ §5.3 で取り消した。

### 5.2 段階 0.5 で見つかったこと(PR #87 の xfail と報告)

- フェンスの穴は、2 つの経路で再現した(§2.1 の 2)。
- ツールの取り違えのガードは、認証ありでも抜ける。主体のキーを持たない普通の計画を `flash` で送ると、積まれて成功する。
- `listChanged` は、従来の `initialize` では false、`2026-07-28` の handshake では true と報告される(SDK の既定値と推測)。
  `AGENTS.md` の記述と食い違う。
- `lkg_object_changed` には到達できない。`lkg_object_conflict` は別のコードに付け替えられる。既にある
  バックアップの `.bin` は、`backend_error_no_retry` になる。
- ハンドラーの中の内部の例外は、`invalid_arguments` として報告される。保守のタスクも黙って止まる。
- 最初のフラッシュの行き止まり(§5.1)を、`status(view=targets)` か `status(view=device)` で示す。

### 5.3 回復点なしのフラッシュ(2026-10-25、利用者の確認済み)

- **フラッシュは、LKG も全体バックアップも前提にしない。** `backup_job_id` と `lkg_receipt` のどちらも
  指定しないフラッシュは、計画の `recovery_kind` を `"none"` にする。`lkg_receipt`、`backup_sha256`、
  `backup_job_id`、`expected_device_sha256` は null。準備でも実行でも、LKG とバックアップを引かない。
- 理由: ESP32-S3 の ROM ダウンロードモードは、アプリ領域への書き込みでは壊れない。悪いビルドからの回復は、
  直したビルドを焼くこと(前へ進める)。正確な回復用バイトと書き込み前のフェンスを求めると、行き止まりが
  できるだけだった。実例: 前のフラッシュが書き込み後に確定しなかった(readback 中に権限が変わった)ため、
  実機の中身が LKG と一致しなくなった。`allow_backup = false`、`allow_restore = false` の設定では、
  すべてのフラッシュが `flash_backup_prewrite_check` の段で `lkg_current_firmware_changed_needs_review`
  になり、出口がなかった。
- `"none"` の実行では、回復用バイトの確認、書き込み前のフェンス、前の LKG との `same_layout` の照合をしない。
  パーティションテーブルは一式の一部として書く。NVS やデータとの互換は確かめない(要約に
  `data_compatibility: "not_checked"`)。
- 実機の同一性は、実行時に、書き込み直前の probe で結ぶ。probe で実機を特定できなければ書かない。
  その値を、書き込み・readback・LKG のメタデータの期待値にし、要約に `bound_device_sha256` として残す。
  起動後の再接続は、今までどおり同じ USB を要する。
- 残すもの: 登録・`allow_flash`・ツールのハッシュ、所有する検証済みビルドとマニフェストの結び付き、
  セキュリティの probe、キャンセルと権限の確認、`driver.write` の 1 回の書き込み、全体の readback、
  起動の ELF/IDF の確認、確認付きのクローズ、最後の権限確認、そしてこのビルドを LKG にする
  `Store.finish_firmware` の不可分な確定。
- `firmware_lkg.baseline` は NOT NULL なので、`"none"` のフラッシュは自分のジョブ ID を入れる。
  表と列は増やさない。
- 計画の `effects` は、回復点を求めも残しもしないことを書いた別の定数(`FLASH_EFFECTS_NO_RECOVERY_POINT`)。
  `FLASH_EFFECTS` は変えないので、保存済みの計画はそのまま有効。`"none"` は新しい firmware_flash の計画
  (`manifest_export_sha256` を持つもの)だけに許す。
- `lkg_receipt` か `backup_job_id` を明示したフラッシュ、リストア、バックアップは変えない(フェンスも残す)。
- `status(view=targets)` の `flash_baseline` は、`allow_flash` が true なら `"not_required"`。
  `lkg_recorded` を足した。§5.2 の「行き止まりを示す」は、行き止まりがなくなったので不要になった。

## 6. 未検証

- 台帳の「SPEC」「speculation」の項目は、実行して確かめていない。各段階の始めに、テストで確かめる。
- 実機で確かめたのは、書き込みのない hello→observe のチェーンだけ。フラッシュ/リストアのチェーンと、
  ファームウェアのハッシュのフェンスは、未確認。
- 親クラウドクライアントからの MCP メタデータの照合は、0.13.0 では未実施。
