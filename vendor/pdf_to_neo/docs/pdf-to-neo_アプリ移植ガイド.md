# pdf-to-neo をアプリ（neo-estimate）で再現するための移植ガイド

作成 2026-09-13。対象は Streamlit アプリ `%USERPROFILE%\dev\neo-estimate`（CLAUDE.md の「B」）。
このリポジトリ（`files`、ブランチ `pdf-to-neo`）で Claude が行っている「見積 PDF → NEO」を、アプリで**同じ結果になるように**動かすための文書。
アプリ側で作業する担当者（別セッションの Claude を含む）は、これを最初に読む。

---

## 1. 結論: 作り直さず、同じコードを部品として使う

このセッションの NEO 化は、次の 2 つでできている。

| 部分 | 中身 | アプリでの扱い |
|---|---|---|
| **読む**（人・Claude の仕事） | 見積 PDF を目で読み、**印字どおり** `reading.json` に写す。ページごとに機械検算し、落ちたページだけ読み直す | **LLM の呼び出しに置き換える**（§3） |
| **決める・作る**（プログラムの仕事） | `reading.json` → 判断規則で `estimate.json` → ADDATA 突合せ → NEO 生成 → 検算 → 確認箇所シート | **同じ Python コードをそのまま呼ぶ**（§2）。書き直さない |

判断規則（部品コードの決め方・左右分割・数量の読み替え・塗装の組み直し・レバーレート…）はすべて
`draft_estimate.py` と `claude_neo_pipeline/` の中にある。**アプリ側で同じ規則を別に実装すると、必ずどこかで食い違う**
（`neo-estimate/docs/files_調査_相違点一覧.md` がその実例）。規則を直すときはこのリポジトリで直し、アプリは取り込み直す。

---

## 2. 取り込むもの・呼び方

### 2-1. 取り込むファイル

`make_bundle.py` の配布 zip と同じ範囲（`python .claude/skills/pdf-to-neo/scripts/make_bundle.py`）:

- `claude_neo_pipeline/`（生成器・車両特定・塗装指数・NEO の器。`tests/` は `make_bundle.py` の `SELFTEST_TESTS` に並べた自己診断用のテストと `neo_diff.py` だけ入る。§5-1）
- `.claude/skills/pdf-to-neo/scripts/`（下書き・検算・突合せ・確認箇所シート・一括実行）と `reference/`（規則の文書と辞書）
- `_addata_db_search.py`

取り込み方は **git の特定コミットで固定**する（submodule か、`vendor/pdf_to_neo/` にコピーして元のコミット ID を記録）。
更新はこのリポジトリの `pdf-to-neo` ブランチから取り直す。アプリ側で中身を書き換えない。

### 2-2. 呼び方（どちらか）

**A. 一括実行をそのまま呼ぶ（いちばん確実に同じ結果になる）**

```
python .claude/skills/pdf-to-neo/scripts/make_neo.py <作業フォルダ> --name <顧客>_<車名>
```

- 作業フォルダに `reading.json`（または `pages/header.json` + `pages/page_N.json`）を置いて呼ぶ
- 出てくるもの: `<name>.neo` / `<name>_確認箇所.xlsx`（openpyxl が無い環境では `<name>_確認箇所.csv`）/ `report.md` / `estimate.json` / `inspect.json` / `reading_check.json`
- 終了コード 0 = 合格。1 = 不合格（検算差・未照合・前後左右の食い違い・低照合率・できあがった NEO が下書きの意図と違う（`intent_check.py`）・確認箇所シートが作れない・例外）。
  **この実行で作った NEO は、不合格なら `<name>.ng.neo` に隔離される**（run_case の検算で落ちたときも、make_neo 側の関門で落ちたときも）
- ただし**前回の実行で合格した `<name>.neo` は消されずに残る**。アプリは要求ごとに新しい作業フォルダ（一時フォルダ）で呼び、
  **終了コード 0 のときだけ** `<name>.neo` と確認箇所シート（`<name>_確認箇所.xlsx` か `.csv`。拡張子を決め打ちしない）を組で渡す（ファイルがあるかどうかで合否を判断しない）

**B. 関数で呼ぶ（画面で途中を見せたいとき）**

| 段 | 入口 | 戻り値・出力 |
|---|---|---|
| ページの検算 | `reading_pages.validate_page(header, page)` | `{'ok', 'fail', 'warn', 'rows'}`。fail の文言をそのまま LLM に返して読み直させる |
| ページの束ね | `reading_pages.merge(case_dir, force=False)`（`case_dir/pages/` を読む。**ファイルは書かない**。`reading.json` への保存・全体の検算は呼ぶ側で行う。CLI の `reading_pages.py merge <case_dir>` は保存と `Checker(...).run()` まで行う） | `(reading or None, messages)` |
| 紙上検算 | `reading_check.Checker(reading).run()`（個別の `check_*` は引数と順序があるので直接呼ばない。CLI は `reading_check.py <reading.json> --json <out>`） | `{'fail', 'warn', 'note', 'settings', 'rows'}` |
| 下書き | `draft_estimate.Drafter(reading).build()` | estimate（`_draft_notes` と確認点 `_review` 付き） |
| 突合せ | `inspect_estimate.main(estimate_path, out_json)` | 戻り値は終了コード（0/1）。要確認（★）は `out_json` の `warnings` に書かれる |
| 生成・検算 | `run_case.main(estimate_path, out_neo)`（CLI は `run_case.py <estimate.json> <out.neo>`） | 戻り値 True = 合格（NEO を `out_neo` に置く）/ False（`.ng.neo` に隔離）。検算 11 項目と ★ は標準出力。**`NeoBuilder().build()` は NEO のバイト列と行を返すだけで検算・関門を通らないので、合否には使わない** |
| 意図との突き合わせ | `intent_check.check(estimate, neo_path)` | `{'hard': [...], 'soft': [...], 'rows'}`。hard が 1 件でもあれば不合格（make_neo と同じ） |
| 確認箇所シート | `review_sheet.collect(est, rows, inspect_warn, check, audit_lines, run_out, extra=hard+soft)` → `review_sheet.write(path, entries, est, rows, rep)`（rows は `run_case._rows_in_source_order(rep['rows'])` で見積の並びに戻したもの） | xlsx（openpyxl が無ければ .csv）のパス |

| 答え合わせ（任意・納品後） | `neo_compare.compare(neo_compare.load(納品した.neo), neo_compare.load(確報に使った.neo))` → `neo_compare.report(res)` | `{'rows', 'matched', 'diffs', 'only_mine', 'only_other', 'style', 'totals'}`。合否には使わない（振り返り用。判断規則 10-24） |

B で組むときも、**合否の判定は `make_neo.py` の `main()` と同じ条件**にすること（部分的に真似ると関門が抜ける）。

---

## 3. 「読む」段を LLM に置き換える

このセッションでは Claude が PDF を画像で読み、次の手順で写している（`SKILL.md` 手順 2・4）。アプリはこれを API 呼び出しにする。

### 3-1. LLM に渡すもの

- 見積 PDF（ページ画像。FAX は 1 ページずつ。細かい表は上下 2 分割すると読み違いが減る）
- 指示文として、次の文書を**そのまま**入れる（要約しない。要約すると規則が落ちる）:
  - `reference/reading_schema.md`（出力の形。**短縮記法** `code|name|method|parts_no|index|qty|price|wage|flags|comment`）
  - `reference/format_catalog.md`（書式ごとの写し方。書式 F の「〃 交換工賃」「作業区分空欄」など）
  - `SKILL.md` 手順 4 の「ページを写す順番と自己チェック」8 項目
  - 手入力の行で名称が 24 バイトを超えるときは `neo_name`（判断規則 10-23）
- 車検証・速報（あれば）: 型式・車台番号・型式指定・類別・初度登録・カラー

### 3-2. LLM にさせないこと（判断はプログラムがする）

部品コードの推測、左右の分割、数量の読み替え、区分の言い換え、レバーレートの決定、塗装パネルの組み立て、費用の分類 —— **すべて `draft_estimate.py` がする**。
LLM は「紙に書いてあるとおり」だけを出す。人が読んで気になった点は comment に書き、確かめてほしい点は `要確認:` で始める
（確認箇所シートの 要確認 になる）。見積書に**印字された**明細コメントだけ `NEO:` を付ける（NEO の明細コメントになる。それ以外は NEO に書かない）。

### 3-3. 読み直しのループ（このセッションと同じ進め方）

1. ページごとに LLM に写させる → `validate_page` で検算
2. FAIL（行数・ページ小計・印の数・数量×単価）が出たら、**FAIL の文言と該当ページの画像だけ**を渡して読み直させる（最大 3 回）
3. 全ページ合格 → `merge` → 合計欄の検算。落ちたら差額のヒント（「差額と同じ額の行」）を渡して該当ページだけ読み直す
4. それでも合わなければ人に回す（画面で該当ページと差額を見せる）。**合計合わせのために行を消したり金額を動かしたりしない**

### 3-4. モデル

このセッションで読んだのは Claude（PDF・画像入力）。同じ精度を狙うなら Claude API（`claude-opus-5` / `claude-sonnet-5`）で PDF を渡すのが近い。
Gemini を使い続ける場合も、指示文・出力形式・読み直しループは上と同じにする。モデルを変えたら §5 の受け入れテストで読み取りの合格率を比べる。

---

## 4. 動かす環境の条件

| 条件 | 内容 | Streamlit Cloud での注意 |
|---|---|---|
| ADDATA | コグニの車種データ（毎月更新。版で標準品番・標準指数・部品価格適応日が変わる） | サーバからは利用者 PC の `C:\Addata` は読めない。アプリの「Addata の場所を設定」（ZIP / URL）で渡す |
| COM.CAB の展開 | 毎月変わる表（DATAUP・Katashiki）は `expand.exe`（Windows）で展開して読む（`com_tables.py`） | Linux には無い → **純 Python で展開する**（§4-1。2026-09-14 から） |
| 塗装指数（CHM） | 車種ごとの CHM を `hh.exe`（Windows）で展開（`paint_index.py`、キャッシュ `%LOCALAPPDATA%\claude_neo_pipeline\chm`） | Linux には無い → **7z（p7zip-full）で展開する**（§4-1）。7z も無ければ修正塗装のあるパネルの見積は理由付きで不合格 |
| Python | 3.11 以上。生成は標準ライブラリだけ。確認箇所シートは `openpyxl`（無ければ CSV） | `requirements.txt` に `openpyxl` を固定版で足す |
| 雛形 NEO | `claude_neo_pipeline/reference/template.neo`（顧客情報なし） | そのまま同梱 |
| 個人情報 | 見積・NEO・確認箇所シートには顧客情報が入る | Community Cloud は 1 プロセスを全利用者で共有。作業フォルダは要求ごとに一時フォルダを作り、終わったら消す |

いちばん簡単で確実なのは、**ADDATA とコグニのある Windows PC でアプリを動かす**（ローカル起動 or 社内サーバ）。
Cloud で動かすなら、§4-1 の条件（`packages.txt` の `p7zip-full`）を満たせば Windows と同じ NEO になる（2026-09-14 に 9 案件で確認）。

---

### 4-1. Linux（Streamlit Community Cloud など）で Windows と同じ NEO にする条件 — 2026-09-14

生成器は 2 か所で Windows 付属のツールを使っていた。Linux では次のように動く（1bd202b の次のコミットから）:

| 段 | Windows | Linux | 無いとどうなるか |
|---|---|---|---|
| ADDATA の COM.CAB（毎月変わる DATAUP.DB・Katashiki.DB）の展開 | `expand.exe` | **純 Python**（`com_tables.extract_cab`。MSZIP を zlib で展開。expand.exe と全 53 ファイル一致を確認）→ 7z | 同梱の予備 `reference/`（★ 古い可能性） |
| 車種別の塗装指数表（CHM）の展開 | `hh.exe -decompile` | **7z**（p7zip の `7z x`。hh.exe と同じ .hhc / html/*.htm ができる） | 修正塗装の標準指数が取れない。そのパネルがある見積は **ValueError で不合格**（黙って 0 を書いて Windows と違う NEO を作らない。実測: 9 案件中 1 件が変わっていた） |

- 7z の探し方: 環境変数 `PDF_TO_NEO_7Z` → PATH の `7z` / `7za` / `7zz` → Windows の Program Files。Streamlit Community Cloud は `packages.txt` に `p7zip-full` を書けば入る。Docker は `apt-get install p7zip-full`
- 展開キャッシュは `%LOCALAPPDATA%\claude_neo_pipeline\{com,chm}`。無い Linux では一時フォルダ（`tempfile.gettempdir()`）。**配布物の中には書かない**（アプリは vendor の内容ハッシュを照合する）
- 確かめ方（Windows 上で Linux を模す）: `WINDIR` を存在しないパスに、`PATH` から System32 を外し、`LOCALAPPDATA` を空フォルダにして `make_neo.py` を回し、通常環境の NEO と `neo_diff.py` で比べる。7z ありで 9/9 一致、7z 無しで 8/9 一致＋1 件は理由付きで不合格（2026-09-14）
- 単体テスト: `claude_neo_pipeline/tests/unit_cab.py`（純 Python の展開 = expand.exe、壊れた CAB は ValueError）

## 5. 同じ結果になっていることの確かめ方（受け入れテスト）

1. **プログラム部分が同じか**: 取り込んだコミットと同じコミットの **git の作業ツリー（全体）** で
   `bash claude_neo_pipeline/tests/verify_all.sh` と `python .claude/skills/pdf-to-neo/scripts/regress_cases.py` を回す
   （実機 NEO との全列比較・全ファイル一致・案件回帰。合格の基準は「差のある実験 0・全ファイル一致の不一致 0・不合格 0・`=== verify_all exit 0`」。
   件数は NEO_check にある実機 NEO と案件の数で変わる（この PC では 2026-09-13 に 73 実験・25 本・案件 4 + 8）ので、分母を合格条件にしない。
   実機 NEO と案件は NEO_check にあり git にも無いので、**この PC で回す**）
   配布 zip の `claude_neo_pipeline/tests/` には `make_bundle.py` の `SELFTEST_TESTS` に並べた自己診断用のテスト
   （`unit_types` / `unit_consistency` / `unit_guards` / `unit_manual_rows` / `unit_settings` / `unit_eva_slot` / `unit_link_absorb` / `unit_frame` / `unit_handoff` と `neo_diff.py`）だけが入る
   （`verify_all.sh`・`audit_cogni_files.py`・`unit_struct.py` などは入らない）。配布 zip だけの環境では `python .claude/skills/pdf-to-neo/scripts/env_check.py --self-test` が **`unit_` で始まるテストだけ**を回す（`neo_diff.py` は引数に NEO を 2 本渡して使う比較ツールで、自己診断では回らない。NEO の全列比較は下の 2 で個別に行う）。取り込むときにこれらを消さない
2. **同じ reading.json から同じ NEO か**: NEO_check の各案件の `reading.json` をアプリに通し、
   `python claude_neo_pipeline/tests/neo_diff.py <アプリの NEO> <このリポジトリの NEO>` で全列一致を確かめる
3. **読む段の精度**: 同じ PDF を LLM に読ませた `reading.json` と、NEO_check の `reading.json`（Claude が読んで合格したもの）を比べる。
   指標は「ページ検算の初回合格率」「合計一致までの読み直し回数」「make_neo 合格率」。この 3 つが揃えば同じ水準
4. ADDATA の版は両方で揃える（`env_check.py` の「ADDATA データ版」）。版が違うと標準品番・指数・部品価格適応日が変わる

---

## 6. 更新の流れ

1. 規則・不具合はこのリポジトリ（`pdf-to-neo` ブランチ）で直し、codex-loop と verify_all を通す
2. アプリは取り込んだコミットを上げる（submodule 更新 or `vendor/` を差し替えてコミット ID を記録）
3. アプリの回帰テストと §5 の 2 を回す
4. 規則の文書（`reference/*.md`）が変わったら、§3-1 の指示文も同じ版に上げる（指示文は文書から組み立て、アプリ側に写しを持たない）

## 7. 参照

- `.claude/skills/pdf-to-neo/HANDOFF.md` — 全体の地図・絶対ルール・実機で確かめた事実
- `.claude/skills/pdf-to-neo/SKILL.md` — 手順 9 段と仕上がりの水準
- `.claude/skills/pdf-to-neo/reference/judgment_rules.md` — 判断規則（10-21 数量の読み替え、10-22 確認箇所シート ほか）
- `neo-estimate/docs/files_調査_相違点一覧.md` — 現行アプリとの相違点（取り込むと解消するもの）


### 4-2. 部分 Addata（COM ＋ 見積の車種フォルダだけ）を渡すとき — 2026-09-14

アプリがブラウザ経由で利用者の PC から COM と見積の車種フォルダだけを取り寄せた一時フォルダ（`addata_bridge_<id>`）を `ADDATA_ROOT` に渡すときは、**`ADDATA_ROOT_PARTIAL=1` も一緒に渡す**。`skill_env.resolve()` の環境変数の検証は `is_addata()`（メーカーフォルダ 5 つ以上）なので、旗が無いと部分コピーは弾かれて自動検出に落ちる ＝ 開発機では `C:\Addata` に化けて気づけず、Cloud では見つからず失敗する。旗があるときだけ `is_addata_or_partial()` が 環境変数 `ADDATA_ROOT` そのものに限って部分コピー（`COM/KA06_ALL.DB` と `AnVer.DB` か `COM.CAB`）を採る。設定ファイル・自動検出の候補は旗があっても部分コピーを採らない（`tests/test_skill_env.py::test_partial_root_only_with_flag`）。
