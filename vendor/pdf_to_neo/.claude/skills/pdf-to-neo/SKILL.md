---
name: pdf-to-neo
description: 工場見積 PDF（どの書式でも）＋車検証から、コグニセブンで作成したのと同じ水準の NEO ファイルを claude_neo_pipeline で生成する定型手順。「NEO にして」「NEO 化」「コグニのファイルにして」「見積 PDF を変換」と頼まれたら必ず使用する。PDF を印字のまま reading.json に写す → make_neo.py 1 コマンド（draft_estimate で判断規則を適用 → inspect_estimate で ADDATA 突合せ → run_case で生成・検算 → 納品コピー）→ 必要ならコグニ実機確認 → 日誌。個人情報はリポジトリ外（NEO_check）に置く。
---

# 工場見積 PDF → NEO（コグニセブン同等）変換手順

対象リポジトリ: `【自動集計システム※作成中】\files`（git root）。生成器は `claude_neo_pipeline/`（`README.md` と `../NEO_FILE_SPEC_COMPLETE.md`・`../ADDATA_REVERSE_LOOKUP_SPEC.md` が仕様の根拠）。
このスキルは「亮平さんが 2026-09-09 までにコグニ実機で確定した判断規則」を固定したもの（装備・連動・吸収・手入力・塗装・骨格・設定・AnSMB の実機実験は `../NEO_FILE_SPEC_COMPLETE.md` §10-7〜§10-18）。生成器のコードが精度の大半を担うので、このスキルの役割は **estimate.json を誰が作っても同じ結論になるようにすること** と **検算が全部一致するまで終わらないこと**。

**この一式を初めて触る担当者（別セッションの AI を含む）は、先に `HANDOFF.md` を読む** —— 全体の地図・絶対ルール・実機で確かめた事実（間違えると事故る点）・これまでに踏んだ落とし穴が 1 枚にまとまっている。

参照ファイル（すべて `.claude/skills/pdf-to-neo/` 配下）:

| ファイル | 内容 |
|---|---|
| `HANDOFF.md` | **引き継ぎの入口**。全体の地図・絶対ルール・実機で確かめた事実・落とし穴 |
| `reference/estimate_schema.md` | estimate.json の全キーと意味・例（生成器が読むキーの正） |
| `reference/format_catalog.md` | 届く見積書式の分類と、書式ごとの読み取り・写像規則 |
| `reference/judgment_rules.md` | ref 選択・左右分割・装備・板金ランク・塗装・材料代・費用分類・手入力の判断規則（根拠付き） |
| `reference/checklist.md` | 納品前チェックリスト（検算 11 項目・コグニ画面確認・納品・記録） |
| `reference/template_estimate.json` | 空の雛形（estimate.json を直接書くとき用） |
| `reference/reading_schema.md` | **reading.json**（見積書を印字のまま写す中間ファイル）の形。通常はこちらを書く |
| `scripts/draft_estimate.py` | reading.json → estimate.json。ref 決定・左右分割・板金ランク・装備・塗装行の解釈・費用分類・レバーレート逆算を自動で行う |
| `scripts/inspect_estimate.py` | estimate.json を ADDATA と突き合わせ、要確認点を一覧にする読み取り専用スクリプト |
| `scripts/guess_labor_rate.py` | 技術料（税抜）だけが印字された見積書からレバーレートを逆算する（指数の列が無い工場書式用） |
| `scripts/find_ref_by_price.py` | 名称と 1 個あたりの標準単価から ADDATA の部品コードを探す（同名候補が多い小物の特定） |
| `scripts/pick_grade.py` | グレードが決まらない案件を、部品金額と ADDATA 標準価格の一致数で絞る |
| `scripts/make_neo.py` | 案件フォルダを渡すと 下書き → 突合せ → NEO 生成 → 検算 → 印字の印との照合 → 報告文（report.md）→ 確認箇所シート（xlsx）→ 納品コピー を 1 コマンドで行う |
| `scripts/intent_check.py` | できあがった NEO を読み戻し、estimate.json（下書きの意図）と 1 行ずつ突き合わせる（部品コード・数量・金額・工賃・コメント・名称・顧客名）。make_neo が呼び、食い違いは不合格、欄で切れた名称などは確認箇所シートの 要確認 |
| `scripts/neo_compare.py` | 納品した NEO と、あとで確報・協定に使われた NEO（コグニで直したもの・別アプリのもの）を明細単位で突き合わせ、品番の違い・数量の違い・足された行・削られた行を出す（読むだけ。手順 9 の答え合わせ） |
| `scripts/pdf_pages.py` | 見積 PDF をページごとの拡大画像（上下に分けたもの）と文字層に切り分ける。転記の下ごしらえ |
| `scripts/review_sheet.py` | 確認箇所シート（xlsx: 確認箇所 / 手入力の行 / 下書きの判断 / 合計）を作る。make_neo が呼ぶ（openpyxl が無い PC は CSV） |
| `scripts/reading_check.py` / `reading_pages.py` / `ocr_prefill.py` | 紙上検算 / ページ単位の転記 / Windows OCR 先読み（手順 4〜5） |
| `scripts/option_audit.py` | 装備監査（印字品番 vs 装備で決まる標準品番）。make_neo が生成後に呼ぶ |
| `scripts/build_part_names.py` | 全車種 12.DB から 部品名 → 部品コード の別名辞書 `reference/part_code_names.json` を作る（ADDATA 更新後に 1 回） |
| `<NEO_CHECK_ROOT>/_profiles/factory_profiles.json`（PC ごと・git に入れない） | 工場ごとの設定の学習データ（レート・工賃丸め・消費税丸め・費用の集計先・書式・手入力行モード）。make_neo が合格時に書き、reading_check が前回との差を WARN に出す（`--no-profile` で書かない） |
| `scripts/winocr.ps1` | Windows.Media.Ocr を呼ぶ PowerShell（`ocr_prefill.py` が使う） |
| `scripts/tests/` | スキル自体の単体テスト（紙上検算 / ページ単位 / OCR / 別名辞書 / 下書きの写し取り規則 / 下書きの注記 / グレード絞り込み / 単価からの部品コード / レバーレート逆算 / 環境検出）。script を直したら **`scripts/tests/test_*.py` を全部**回す（`env_check.py --self-test` は生成器側の 9 本。本数は env_check の出力が正） |
| `scripts/regress_cases.py` | スキル自体の回帰テスト。NEO_check の reading.json 全案件を再下書きして正解（expected_estimate.json）と比較し、run_case 合格を確認。script を直したら必ず実行 |

## 前提（無ければ止まって報告）

- コグニセブンのデータ **ADDATA**（`COM` とメーカー別フォルダがある。標準は `C:\Addata` だが PC により違う）と、実機確認に使うコグニセブン本体（`AudaMenu.exe`。標準は `C:\Program Files (x86)\Audatex\Auda7\Bin`）。場所は `scripts/skill_env.py` が **環境変数 → 設定ファイル `%USERPROFILE%\.claude\pdf-to-neo.local.json` → 自動検出** の順に解決するので、スクリプト内にパスを書かない（ADDATA はコグニ本体の隣 → 固定ドライブとネットワークドライブのよくある置き方 → 見つからなければ 4 階層目まで走査。コグニ本体は既定パス → 各ドライブの Program Files → レジストリ。設定が切断済みの共有を指しているときは 3 秒で見切って次の手段に進む）。ADDATA は**浅い場所で見つかった時点で決める**ので、古い `C:\Addata` を残したまま本物を深い場所に置いている PC では古い方を選ぶ。`env_check.py` が「ADDATA 他の候補」の行で知らせるので、その PC では `--addata` で固定するか `ADDATA_SCAN_DEEP=1` を設定する
- Python 3.11 以上と `claude_neo_pipeline`（`run_case.py` が動く）。雛形 NEO は `claude_neo_pipeline/reference/template.neo`（生成器で作った顧客情報の無い雛形）を同梱。旧雛形 `template_04011103.neo` と `neo_04011103_reference.json` は実 NEO 由来なので配布 zip には入れない
- 検証物は **リポジトリ外** `%USERPROFILE%\Documents\NEO_check\<案件名>\`（環境変数 `NEO_CHECK_ROOT` で変更可）に置く（顧客名・住所・車台番号を含むため。`*.neo` と NEO_check は git に入れない）
- Codex にプロンプトを送るときは NEO_check の中身・顧客情報・API キーを出さない

## 新しい PC で使えるようにする（初回 1 回）

1. リポジトリ `files` を持ってくる（git clone、または `scripts/make_bundle.py` で作った zip を解凍）
2. Python 3.11 以上と Claude Code を入れる（NEO 生成に追加パッケージは不要。3.14 まで動作確認済み。OCR 先読みを使うときだけ `pip install pypdf pillow`）。インストール時は「Add python.exe to PATH」にチェック。`python` と打って Microsoft Store が開く PC では、以下の `python` を **`py -3`** に読み替える
3. `files` で環境確認と設定保存。ADDATA とコグニを自動検出し、個人スキル領域 `%USERPROFILE%\.claude\skills\pdf-to-neo` にジャンクションを作る:

```bash
python .claude/skills/pdf-to-neo/scripts/env_check.py --save --install-skill
```

自動検出できない PC ではパスを指定する: `env_check.py --addata "D:\Addata" --cogni "D:\Audatex\Auda7\Bin\AudaMenu.exe" --neo-check "D:\NEO_check" --save --install-skill`

4. `[OK]` が並び「使える」と出れば完了（自己テストで N BOX JF1 の車両特定と雛形 NEO の読込まで確認する）。`hh.exe` が無い・使えない PC は塗装パネルの標準指数（CHM）が取れない。その PC では生成時に `★ 塗装指数表（CHM）を展開できない` と出るので、**修正塗装の行は `paint.panels[].index` に見積書の指数を必ず書く**（書かないと塗装計が静かにずれる）
5. 受け入れ確認（推奨）: `python .claude/skills/pdf-to-neo/scripts/env_check.py --self-test` で、その PC の ADDATA を使って生成器の単体テスト 9 本（引き継ぎ文書との整合を含む）を通す（10 秒ほど）。`自己診断: すべて合格` なら開発機と同じ NEO が作れる。`ADDATA データ版`（例 2026/08）も控えて社内で揃える

補足: ADDATA が複数ある PC（古い `C:\Addata` と別ドライブの本番データ）では、**既定で走査した候補の中から** `COM\AnVer.DB` が最新のものを選ぶ。既定の走査は「よくある置き方」で 1 つ見つかった時点で止まるので、**古い `C:\Addata` を残したまま本番データを深い場所に置いている PC では古い方を選ぶ**。`env_check.py` が毎回 深い場所まで調べて「ADDATA 他の候補」で知らせるので、導入時に必ず 1 回実行する。使いたいものを固定するなら `--addata` で明示（`ADDATA_SCAN_DEEP=1` でも毎回深く探せるが数十秒かかる）。`--install-skill` は古いコピーや別の場所を指すジャンクションを退避・付け替える。配布 zip は `python .claude/skills/pdf-to-neo/scripts/make_bundle.py` で作る（NEO_check・実 NEO は入らない。雛形 NEO 1 本だけ同梱）。

このあとの手順の `<NEO_CHECK_ROOT>` は、その PC の案件置き場（既定 `%USERPROFILE%\Documents\NEO_check`。`env_check.py` が表示する）。コグニ実機確認は `make_neo.py ... --open` で検出したコグニに NEO を渡せる。

## 仕上がりの水準（これが揃って初めて「できた」）

検算が通っただけでは足りない。納品する NEO は次の 6 つを満たすこと。

1. **金額**: 検算 11 項目のうち `totals` に書いた項目が全部 OK。工場の合計と一致（説明できない差を残さない）
2. **明細**: コグニ収録車なら **部品コード・標準品番・標準価格・部位ブロックが入っている**。
   `照合 N/M` の N が M に近いこと。半分未満なら `run_case.py` が ★ を出す（判断規則 10-7）
   `標準価格との一致 N/M 行` の ★ は合図なので、出たら ref と車両を確かめる（正当な価格差なら通してよい。判断規則 10-9）。
   **前後・左右の食い違い**の ★ は誤りなので必ず直す（納品もできない。判断規則 10-9-2）
3. **手入力の行**: `manual` は ADDATA に無い品目だけ。1 行ずつ理由を reading の `comment`（転記メモ）に書く → 確認箇所シートに載る（NEO の明細コメントには書かない）
4. **車両**: 確度 `confirmed` か `high`。`low` のときは何で絞ったか（`hints.grade_name` 等）と、
   確定に足りない資料（型式指定番号・類別区分番号）を報告に書く
5. **根拠**: レバーレートの決め手、税込印字の有無、丸め差を残した理由が estimate.json と報告に残っている
6. **確認箇所シート**: 人が確かめる点・下書きが判断した点は、NEO と一緒に渡す `<顧客>_<車名>_claude_確認箇所.xlsx`（openpyxl が無い PC では `.csv`）にまとめる。NEO の明細コメントに入れるのは、見積書に印字された明細コメント（`NEO:` 付き）だけ（2026-09-13 亮平さん指示。判断規則 10-22）

**よくある取りこぼし**（2026-09-09 の 3 件で実際に起きたもの）

| 症状 | 原因 | 直し方 |
|---|---|---|
| 部品コードが入らない | 全行を `manual: true` にした | `manual` を外して名称照合。判断規則 10-7 |
| 課税小計が 1.1 倍 | 税込印字の見積を写した | 各行を (100+税率)/100 で割る。判断規則 10-4 |
| 合計が 1 円合わない | 工場が 10 円単位で丸めている | `neo_total`・`tolerance`・`tolerance_reason` の 3 点。判断規則 10-5 |
| レバーレートが分からない | 指数の印字が無い書式 | 速報の「工賃単価」→ 無ければ `guess_labor_rate.py`。判断規則 10-6 |
| 二輪・輸入車が特定できない | コグニ非収録 | 汎用車種 Z10 で作る（明細は全行 `manual` でよい） |

## 手順（全 9 段）

**コマンド例の読み方**: 以下の `bash` ブロックは Git Bash 形式。PowerShell では最初に `$env:PYTHONIOENCODING = 'utf-8'` を 1 回設定し、各行の先頭の `PYTHONIOENCODING=utf-8 ` を外して同じコマンドを打つ（`cd files &&` は `Set-Location <files>` に読み替え）。heredoc（`<<'EOF'`）の例だけは §3 の注記どおりファイルに保存して実行する。

途中で確認を求めず、検算が全部一致するまで一気に進める。判断に迷った点は最後の報告で「こう判断した」と書く。

### 1. 入力を揃える

案件フォルダ（この PC では例 `Z:\2026年\MM月\DD日\<損保>_<顧客>_<番号>_<車名>_<地域>\`。共有ドライブのレターとフォルダ構成は PC・担当者ごとに違うので、分からなければ案件フォルダのパスを直接もらう）から次を探す。

- **工場見積 PDF**: `工場最終.pdf` / `工場見積.pdf` / FAX 受信名（数字_日付.pdf）など。複数あれば「最終」「最新日付」を採る
- **車検証**: 「自動車検査証記録事項」PDF（`YYYYMMDDhhmmss_<登録番号>.pdf`）か、速報 PDF（`【速報】…pdf`）の中の車検証ページ
- 無いものがあれば、あるもので進め、欠けた項目（型式指定・類別・初度登録・カラー）は報告に書く
- **案件フォルダに既存の NEO（立会で作ったもの）があれば必ず見る**。`python claude_neo_pipeline/tests/neo_diff.py <生成.neo> <既存.neo>` で全列を突き合わせると、合計・名称・作業区分の入れ方が確認できる（既存ファイルは開くだけ。上書き・削除は禁止）

PDF は Read ツールで開く（画像 PDF でも Vision で読める。`pypdf` の文字層は FAX だとゼロなので当てにしない）。
3 ページ以上・品番の細かい見積は、先に `python .claude/skills/pdf-to-neo/scripts/pdf_pages.py <PDF> <NEO_CHECK_ROOT>/<案件>/pages/img --top 0.2 --bottom 0.9` でページを上下に分けた拡大画像にしてから読む（読み違いが減り、ページ全体を何度も開き直さずに済む）。

### 2. PDF を読み取り、書式を分類する

`reference/format_catalog.md` で書式を決める。判断材料は「部品コード列の有無」「指数列の有無」「区分の語彙」「装備注記の有無」。

読み取るもの（漏れると検算で必ず引っかかる）:

1. 車両欄: 登録番号・車台番号・型式・型式指定/類別（無ければ車検証）・初度登録（年式）・カラーNo・グレード名・エンジン・排気量
2. レバーレート: 工賃 ÷ 指数 で逆算（例 12,750 ÷ 1.50 = 8,500）。複数行で一致することを確認
3. 明細: ブロック見出し（【フロントバンパー】等）・名称・区分・指数・工賃・数量・部品金額・品番・素材/手入力印。装備注記行（「インテリジェントクリアランスソナー」等、金額の無い行）は明細ではなく **条件注記** として扱い、装備判断の材料にする
4. 塗装: 塗料（2K/水性/速乾）・塗膜（ソリッド/メタリック/2コートパール/3コートパール）・高機能塗装（しない/フッ素/耐スリ傷）・パネル行（名称・取替/修正・1/1 1/2 1/3・指数・工賃・材料代）・加算基礎数値・ブース・バンパ（新品/修正・一色/二色）・付加塗装
5. 材料代割合: 材料代 ÷ 塗装工賃 で逆算（55% など）。行ごとの材料代があれば行ごと四捨五入か一括かを見る
6. 費用・諸費用: 名称・金額・それが「部品計」「作業計」「諸費用計」のどこに集計されているか
7. 合計欄: 作業計・塗装計・諸費用計・部品計・材料計・消費税・御見積額。**JSON を書く前に電卓で全部足して合計欄と一致させる**（読み取りミスはここで潰す）

### 3. 車両を特定する

下は **Git Bash 用**（`bash` ブロックはすべて同じ）。既定シェルが PowerShell の PC では、`$env:PYTHONIOENCODING='utf-8'` を先に設定し、
`EOF` の間の Python をファイル（例 `%TEMP%\resolve.py`）に保存して `python <そのファイル>` で実行する（PowerShell には heredoc が無い）。
普段は `make_neo.py`（§5）が同じ車両特定を中で行うので、この手順を手で打つのは候補が複数あるときだけ。

```bash
PYTHONIOENCODING=utf-8 python - <<'EOF'
import sys, json
sys.path.insert(0, 'claude_neo_pipeline'); sys.path.insert(0, '.claude/skills/pdf-to-neo/scripts')
import skill_env; skill_env.apply()  # ADDATA の場所をこの PC の設定から解決（スクリプトにパスを書かない）
from addata_vehicle_resolver import AddataVehicleResolver
r = AddataVehicleResolver().resolve(model_code='AGH30W', serial_no='AGH30-0000001', desig='19553', category='0384', reg_date='R4.3', color_code='070')
print(r['confidence']); print(json.dumps({k: r['neo_car'].get(k) for k in ('CarCode','YearCode','BodyCode','GradeCode','FVACode','ColorCode','ps_YearName','grade_name','body_name','options_available')}, ensure_ascii=False, indent=1))
for c in r['candidates'][:5]: print(c)
EOF
```

- `confirmed` / `high` ならそのまま。`low` や候補複数なら、グレード名・エンジン・品番のヒント（`AddataParts.infer_from_parts`）で絞り、決め手を報告に書く
- **二輪車**もコグニ非収録。汎用車種の枠に二輪が無いので `car_code: "Z10"`（乗用車）で作り、車名に「ﾄﾞｩｶﾃｨ ﾊﾟﾆｶﾞｰﾚV4R」のように書く（2026-09-09 実施）
- **型式指定・類別が資料に無い車**（速報も車検証も ＊＊＊）は確度 `low` になる。`hints.grade_name` にグレード名を入れて絞る。ディーラーの型式表記（例 `MXPK11-AHXGB`）は4 文字目がグレードの手掛かりになる。決め手を報告に書く
- コグニ非収録車（輸入車など候補ゼロ）は `vehicle.generic=true` ＋ `car_code`（Z10 乗用 / Z20 1BOX / Z30 トラック）＋ `maker_code`・`car_name`・`engine`・`color_code`。明細は全行 `manual: true`、塗装は一括
- 特別仕様車（TYPE GOLD 等）はグレード自体は基本グレード（S）で、13.DB の仕様別行（備考に「TYPE GOLD…」）で品番が決まる。グレードを無理に変えない

### 4. ページ単位で写し、ページごとに検算する（見積書を印字のまま写す。判断はしない）

**2 ページ以上の見積は必ずページ単位**で写す（1 ページでも可）。1 ページ写すたびに機械検算し、落ちたページだけ読み直す。全ページを一度に写してから合計で悩むのが一番遅い。

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/reading_pages.py init "<NEO_CHECK_ROOT>/<案件>" --pages 3   # 雛形
#   pages/header.json … 明細以外（source / issuer / est_date / format / vehicle / customer / insurance / labor_rate / paint / expenses / totals /  ※一覧に無いキー（target_total_replaces_material 等）も merge が引き継ぐ（_ で始まるキーは除く）
#                        target_total / wage_round / index_policy / hints / discount / frame / adas / note）。init の雛形に無いキーも書けば merge が通す
#   pages/page_N.json … そのページの明細: page / rows_printed / subtotal{parts,wage} / marks{$,#,*} / blocks[{title, rows[短縮記法]}] / paint_lines / expenses
PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/reading_pages.py validate "<NEO_CHECK_ROOT>/<案件>" --page 1   # 1 ページ写すたびに
PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/reading_pages.py status "<NEO_CHECK_ROOT>/<案件>"             # 各ページの状態（未検算 / 検算後に変更 / 不合格）
PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/reading_pages.py merge "<NEO_CHECK_ROOT>/<案件>"              # 全ページ合格 → reading.json（合計欄の検算まで）
#   merge は費用行・塗装行が 2 ページに重複して写されていないかも見る（二重計上の検出）
```

**OCR 先読み（Windows 標準 OCR。pypdf と Pillow が要る。無ければ手で写す。FAX でも品番・金額・指数は 9 割読める）** — 明細が多い見積は先に走らせ、数字は OCR、名称の確認は目で行う:

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/ocr_prefill.py "<見積PDF>" "<NEO_CHECK_ROOT>/<案件>"
#   pages/page_N.json を推定行（comment「OCR未確認」）で作る。pages/ocr/page_N.png が OCR にかけた画像、page_N.ocr.json が推定の中間物、header.json の _ocr_guess が車両欄の推定
#   → 各ページの画像を見て 1 行ずつ 名称・品番（Z/2、I/1 の誤読に注意）・数値 を確認し、確認した行の「OCR未確認」を消す。無い行は足し、余計な行は消す
#   → validate は「OCR未確認」が残る行を FAIL にする（見ずに通せない）。pypdf / Pillow が無い PC では OCR を飛ばして手で写す
```

ページを写す順番と自己チェック（**page_N.json を保存する前に、そのページの画像を見ながら**）:

1. まず **rows_printed**（そのページの明細行数。注記行・小計行・繰越行は数えない）と、印字されていれば **ページ小計**（部品計・工賃計）、**印の数**（$ # * の個数）を書く。これが検算の基準になる
2. 明細を短縮記法で 1 行 1 行写す（`code|name|method|parts_no|index|qty|price|wage|flags|comment`。`|` は 9 個。名称に `|` を入れない）
3. 金額は **数量分の金額**。単価しか印字されていない行は price に 単価×数量 を書き、comment に `unit=単価`。数量 1 のまま複数個分の金額（クリップ 1,300 円）は印字どおり数量 1 で写す（下書きが標準単価の倍数から数量を直す。判断規則 10-21）
4. 左右（RH/LH・右/左）、Fr/Rr、上下は印字どおり。見出しの左右と行の左右が違う行は comment に理由を書く
5. 小計・消費税・前頁繰越・合計行は明細に入れない（header.totals / page.subtotal に）
6. 塗装行・費用がそのページに印字されていれば `paint_lines` / `expenses`（`in` 必須）に
7. 読めない数値は推測せず、その行の comment に `?` と読めた範囲を書く（validate が拾う）。人に確かめてほしい点は comment を `要確認:` で始める（確認箇所シートの 要確認 になる）。見積書に印字された明細コメント（※JAS在庫使用 等）だけは `NEO:※JAS在庫使用` と書く（NEO の明細コメントになる）。手入力の行（M・汎用車種）で名称が 24 バイト（半角カナ換算）を超える行は dict の行にして `neo_name` に 24 バイト以内の短い名前を書く（NEO の名称欄に入る。印字の全文は確認箇所シートに残る。書かなければ自動で短くして 要確認）
8. 保存 → `validate --page N`。FAIL はそのページを読み直す（他のページは触らない）。WARN は理由が言えれば進む

全ページ合格 → `merge`（全体の合計欄・費用の集計先・工場設定の検算）。FAIL があれば「差額と同じ額の行/費用」のヒントを手掛かりに header.json か該当ページを直す。

`NEO_check\<案件名>\reading.json` を直接書く従来の方法も使える（1 ページ・20 行以下の見積）。その場合も `reference/reading_schema.md` の形で **紙に書いてあるとおり** 写す。ブロック見出し・行の名称（RH/LH のまま）・区分語・指数・工賃・数量・数量分の金額・品番・注記行・塗装行・費用（どの合計に入っているか）・合計欄。ref 決定、左右分割、板金ランク、装備、塗装行の解釈、費用の分類、レバーレートは次の段で script が決めるので、ここで考えない。

**JSON は Write ツールか Python で書く**（bash の heredoc に `\` を含む日本語パスを入れると壊れる）。`source` のパス区切りは `/`。

行数が多い見積（30 行以上）は **短縮記法**（`"code|name|method|parts_no|index|qty|price|wage|flags|comment"` の 1 行文字列。`reference/reading_schema.md`）で写すと転記が半分の時間で済む。コグニ印刷（書式 A）は `code` に印字の部品コードを入れる（script がそのまま使う）。工場コグニの工賃丸め（100 円）は script が自動推定するので考えなくてよい。

（estimate.json を直接書く従来の方法も使える: `reference/template_estimate.json` と `reference/estimate_schema.md`。汎用車種や特殊な案件で reading.json の規則に乗らないときだけ）

### 5. make_neo.py で 下書き → 突合せ → 生成 → 検算 を 1 コマンドで回す

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/make_neo.py "<NEO_CHECK_ROOT>/<案件>" --name "<顧客>_<車名>"
```

- `pages/` が reading.json より新しければ先に `reading_pages.py merge`、続けて `reading_check.py`（紙上検算: ページ/ブロック小計・数量×単価・費用の集計先・左右・工賃丸め・消費税丸め・工場プロファイル）が走り、**FAIL があれば止まる**（reading を直す。理由が説明できるときだけ `--skip-check`）
- `draft_estimate.py` が reading.json から estimate.json を作り、`_draft_notes`（名称照合で決めた行・左右分割・板金ランク・採用した装備・追加項目（paint.other）と手入力の塗装行にした塗装行・manual にした行）を表示する。**必ず読む**
- 続けて `inspect_estimate.py` の突合せ（★ 要確認）と `run_case.py` の検算が出る。全部 OK なら「合格」
- 生成後に **装備監査**（`option_audit.py`）が走る。生成器が選んだ装備で決まる標準品番と印字品番を突き合わせ、「装備 X を追加/除外すると一致する行が増える」行があれば ★ で出る。装備の取り違いは合計を変えずに標準品番・指数だけを変えるので、合計一致だけでは見つからない。★ が出たら 10.DB の装備名と見積の注記で採否を決め、採るなら reading の `hints.eva_codes` に書いて再実行
- 生成の後に **意図との突き合わせ**（`intent_check.py`）が走り、できあがった NEO を読み戻して estimate.json と 1 行ずつ比べる。部品コード・数量・金額・工賃・明細コメントが違えば不合格。名称・顧客名などが欄の長さで切れたものは確認箇所シートの 要確認（2026-09-13 に追加。合計が合っていても、手入力の作業名が 24 バイトで途中まで・顧客名が「…株式」までになっていた）
- `report.md` ができる（不合格でも書かれるので、合格の行と合わせて読む）（車両・合計表・手入力行・判断点・要確認・印字の印との照合・紙上検算・装備監査）。**報告はこれを元に書く**。コグニ印刷（書式 A）で右端の印（$ # *）を flags に写しておくと、生成 NEO の印との差が出る（差ゼロ = コグニで作ったのと同じ書式）
- 工場が塗装を「一式」でしか出していない見積書は、reading の paint に `auto_panels: true` を足すと
  **明細からパネルを起こしてコグニと同じ塗装明細に組み直す**（差は材料代で埋まり、塗装費用計と合計は動かない。10-15）
- 協定額（「○○円の NEO にして」）は reading に `target_total`（税込）を書くだけ。塗装材料代で自動調整される（`reference/reading_schema.md`、`judgment_rules.md` 10-2）。
  損保が「○○の工賃で調整」と指示してきたときは、**先に工場見積そのままで検算を通してから**その行の指数を 0.1 刻みで動かして寄せ、
  残る端数だけを `target_total` に任せる。明細を動かした案件は小計も協定後の値に直す
  （ページ小計は `pages["N"]`、ブロック小計は `blocks[].subtotal`。両方書いているなら両方。`judgment_rules.md` 10-14）
- reading.json を直したら再実行（reading.json が estimate.json より新しければ自動で再下書き。script を直した後は `--force-draft`）
- 個別に動かすとき: `draft_estimate.py <reading.json> [<estimate.json>]`、`inspect_estimate.py <estimate.json>`（`--json <出力先>` で機械可読の結果を保存）、`run_case.py <estimate.json> <out.neo>`
  - **`estimate.json` を直に書く（reading.json を通さない）ときの注意**: 生成器は費用の
    `expenses[].kind`（`parts` / 既定は工賃側）と `expenses[].taxfree` を読む。**`expenses[].in` は読まない**
    （`in` は reading.json 用のキーで、`draft_estimate.py` がここから `kind` / `taxfree` に振り分ける）。
    非課税費用は `"taxfree": true` と書く（2026-09-12 明記）
- 案件フォルダには `estimate.json` / `<name>.neo` のほかに `reading_check.json`（紙上検算の結果）・`inspect.json`（突合せ）・`report.md`・`pages/status.json` が書かれる。merge は reading.json に `_merged_from` を足す
- `make_neo.py` のオプション: `--name` `--deliver` `--force-draft`（script を直したとき）`--skip-check`（紙上検算の FAIL を承知で進む）`--force-pages`（ページの不合格を承知で merge）`--skip-inspect` `--no-report` `--no-profile`（工場プロファイルに記録しない）`--allow-neo-total` `--open`
- **`make_neo.py` が不合格にする条件は 4 つ**: 検算に差がある / 未照合行が残る /
  前後・左右が食い違う行がある（ref の取り違え。10-9-2）/ 照合率が低い（手入力に逃げた行が多い。10-7）。
  いずれも reading.json を直して再実行する。
  なお `run_case.py` 単体で止まるのは前の 3 つで、低照合率は警告止まり（`make_neo.py` が合否に使う）——
  **生成器を直接呼ぶときは自分で確かめる**
- 他のスクリプトの補助オプション: `ocr_prefill.py --pages/--scale/--overwrite`、`reading_check.py --json/--save-profile/--quiet`、`regress_cases.py --only/--update`、`reading_pages.py merge --force`、`build_part_names.py --out/--min-count`、`make_bundle.py --out`

`_draft_notes` と ★ を `reference/judgment_rules.md` の規則で一つずつ解決する。主な項目:

| 出力 | 対応 |
|---|---|
| 未照合 | 同ブロックの 12.DB 名称一覧（出力の `name20`）から正しい ref を探して `code` に入れる。ADDATA に無い品目**だけ** `manual: true`（品番や指数の印字が無いことは手入力の理由にならない。判断規則 10-7） |
| 名称近似で決定 | 12.DB 名称と品番一覧を見て確認。違えば `code` を指定 |
| 左側 ref に数量 2 以上 | 右側 ref と 1 行ずつに分ける（左右別行がコグニ流。合計は変わらない） |
| 見積 ≠ 標準 | そのままでよい（`#` 手入力になる）。標準と一致するなら `#` は付かず標準行になる |
| 板金 → ランク提案 | `bankin: {area, yes}` を書く（A=[1,1,1] / B=[1,0,0] / C=[0,0,0]）。reading の行に書けるのは **dict 形式の行**のとき（短縮記法では `area`（名称の `(6dm²)`）と指数から自動判定） |
| 同じ部品コードの行が 2 行 | コグニもそのまま保持する（実機確認）。左右・前後の取り違えが無いかだけ見直す |
| 修理方法がコグニの区分に無い | `method` を区分語（取替 / 脱着 / 修理 / 脱着修理 / 点検調整 / 分解調整 / 板金）に直す。塗装行は明細ではなく `paint.lines` に写す |
| 装備の提案 | 品番が示す装備レターを 10.DB 名称と照らし、車の実態に合うものだけ `hints.eva_codes` に入れる（4WD の Z は自動） |
| 塗装 不一致 | 見積値を書く（生成器が Manual/`#` で保持）。CHM に無いパネルは `index` 必須 |
| 材料代 行ごと丸め | `paint.material` に見積値を入れる（`*` で保持される） |
| 検算 ★不一致 | 読み取りミス。PDF を読み直す（数量×単価、行の取り違え、費用の分類） |
| 装備監査 ★ | 印字品番が別の装備の標準品番と一致する。車の実態（10.DB 名称・注記行）で採否を決め、採るなら reading の `hints.eva_codes` に、draft が自動採用したものを外すなら `hints.eva_exclude` に |
| reading_check FAIL | 紙の上で合わない（ページ小計・数量×単価・費用の集計先・合計欄）。差額と同じ額の行/費用がヒント。reading（pages/page_N.json）を直す |

### 6. 検算が全部 OK になるまで繰り返す

`make_neo.py` の run_case 部分（単独なら `python claude_neo_pipeline/run_case.py <estimate.json> <out.neo>`）で、

- 検算 11 項目（部品計・工賃計・塗装工賃計・材料代・塗装計(材料込)・内板骨格・費用部品・費用工賃・費用計・課税小計・消費税）のうち **reading の `totals` に書いた項目がすべて OK**、かつ「見積書合計との一致: OK」が出るまで 4〜6 を繰り返す（`totals` に無い項目は行自体が出ない。直すのは reading.json。estimate.json を手で直したら reading.json も揃える）
- 工場が円未満・10 円単位で計上して合計がずれる書式（日産系 FAX・工場 R）は、`totals.neo_total`（コグニ計算の合計）・`totals.tolerance`（許容幅・円）・`totals.tolerance_reason`（なぜ差が出るか）の **3 つを揃えて**書き、make_neo を `--allow-neo-total` 付きで再実行する（1 つでも欠けると run_case が不合格にする）。消費税の切り捨て/切り上げは tolerance ではなく `tax_round`（draft が自動判定。コグニの設定は 1 円単位の 3 択だけ）
- `照合 N/M` を見る。コグニ収録車なのに N が半分未満なら ★ が出る。`manual` を付けすぎていないか見直す（判断規則 10-7）
- 合計を合わせるために明細の金額を動かさない。ショートパーツを 1 円削るような調整は禁止
  （協定案件で損保が指定した行の指数を動かすのは別。10-14）
- **不合格のときは `<out>.neo` を作らない**。中身の確認用に `<out>.ng.neo` へ隔離される（2026-09-12〜）。
  前回成功した `<out>.neo` が案件フォルダに残っている場合があるので、**手でコピーして納品する前に必ず  run_case が `出力: …` を出したこと（＝合格）を確かめる**。`make_neo.py` は不合格なら納品段に進まない
- **`totals.tolerance` が効くのは 工賃計 / 塗装工賃計 / 塗装計(材料込) / 内板骨格 / 課税小計 / 消費税 だけ**。
  部品計・材料代・費用部品・費用工賃・費用計は **0 円一致**を求める（工場が円単位で出す項目なので
  丸めの差が出ない。2026-09-12〜）。許容した項目は ★ で「tolerance で許容した」と出る
- `未照合行:` が出たら合格にしない（`allow_unmatched` は使わない）
- 生成後に明細を目視する（リサイクル部品の行は置換後の姿で出る: 名称はリサイクル名、品番は「リサイクル部品」、工賃は -1）（`rep['rows']` を表示: PartsNo / PartsNoStandard / Time / TimeStandard / WageByManual / ConstructGroup）。`#` の行が見積書の「標準と違う指数」の行と一致していること
- 見積書の指数が 15.DB の単独値と同じでも、連動・吸収を含む組合せ標準と違えば生成器は `#` にする（工場のコグニは入力順で計算するため）。これは正常。`report.md` の「手入力の行」に「← 連動・吸収込みの組合せ標準と差」と出るので、報告では「連動分の差」と書く（`judgment_rules.md` 6-4）

### 7. コグニ実機で確認する（新しい書式・新しい車種・`#` が多い案件・骨格/ADAS/付加塗装を含む案件では必須。それ以外は省略可）

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/make_neo.py "<NEO_CHECK_ROOT>/<案件>" --name "<顧客>_<車名>" --open
```

`--open` は `skill_env` が解決したコグニ本体（`COGNI_BIN`）で NEO を開く。パスを直接書かない（PC ごとに違う）。手で開くときも `python .claude/skills/pdf-to-neo/scripts/skill_env.py` が表示する `COGNI_BIN` と `NEO_CHECK_ROOT` を使う。

確認は見積結果・明細・塗装の画面を見るだけにする。「その他 → 装備バリエーション変更 → 完了」（再検索）は標準扱いの行を組合せ標準に置き換え、材料代割合を既定に戻すなどの副作用がある（仕様書 §10-12）ので、納品する NEO では行わない。確認で開いた窓は終わったら閉じる（亮平さんが開いている窓は触らない）。

computer-use では `コグニセブン`（AudaMenu）と、同じフォルダの `AudaMain.exe`（見積画面。request_access には `COGNI_BIN` と同じ `Bin` フォルダのフルパス）に request_access → 見積結果画面（部品代・工賃・塗装代・費用・税抜・税込）→ 明細 → 塗装（塗装条件・外板パネル・バンパ）を見て、見積書と一致することを確認。既に開いているコグニ窓（亮平さんの作業中のもの）は触らない・閉じない。

### 8. 納品する

- 同じ部品コードの行が複数あると `report.md` と画面に出る。コグニもこの形を保つので直す必要は無いが、左右や前後を取り違えて同じ ref に寄せていないか見直す
- `make_neo.py ... --deliver "Z:/2026年/MM月/DD日/<案件フォルダ>"` で `<顧客>_<車名>_claude.neo` と `<顧客>_<車名>_claude_確認箇所.xlsx`（openpyxl が無い PC では `.csv`）がコピーされる（既存ファイルは上書きせず、両方そろえて `_2` を付ける）。手でコピーするときも同じ名前規則
- SendUserFile で NEO と確認箇所シートを渡す（caption に合計と一致の旨）。報告の「確認していただきたいこと」はシートの **要確認** の行から書く
- 報告は結論先: 合計一致、車両特定の根拠、`#` 手入力にした行と理由、左右分割・装備・板金ランクなど判断した点、未確認事項

### 9. 記録する

- 日誌 `~/.claude/brain/05_日誌/YYYY-MM-DD.md` に案件名・車両・判断点・出力先を追記
- **答え合わせ**: あとで案件フォルダに確報・協定に使った NEO（`<登録番号>_見積.neo` など、納品した `_claude` 以外）が置かれたら、`python .claude/skills/pdf-to-neo/scripts/neo_compare.py <納品した.neo> <その NEO>` で突き合わせる（既存ファイルは開くだけ）。「品番の違い」は見積書でどちらが正しいか確かめる。「相手の側だけ」「納品の側だけ」は協定で足した・削った行か、読み落としかを見分ける。読み落とし・規則の違いが見つかったら `reference/judgment_rules.md` に書く（2026-09-13 の結果は 10-24）
- 新しい書式・新しい判断規則が出たら `reference/format_catalog.md` / `reference/judgment_rules.md` に追記（このスキル自体を育てる）
- 生成器の不具合や規則の追加が必要なら codex-loop で修正し、`claude_neo_pipeline/tests/verify_all.sh` を通す（実機保存 NEO との総当たり比較は `claude_neo_pipeline/tests/audit_cogni_files.py`、AnSMB の 142 桁一致は `audit_cogni_files.py --ansmb`）

## 絶対に守ること

- 合計が合わないまま「できました」と言わない。差額の原因が説明できないときは未完了として報告する
- 見積書の数字を勝手に「正しい値」に直さない（工場の指数がコグニ標準と違っても工場値を `#` で保持する。材料代の丸めも同じ）。
  **例外は協定案件（10-14）だけ** — 損保が「○○の工賃で調整」と指定した行の指数を動かすのは指示どおりの作業。
  動かした前後の値を報告に書く。指示のない行を合計合わせのために動かすのは禁止
- **合計を合わせるために明細の金額を動かさない**（協定案件で損保が「○○の工賃で調整」と指定した行だけは例外。10-14）。
  消費税の丸めが工場と違うときは `tax_round`（コグニの その他 → 消費税設定。1 円単位の 四捨五入/切り捨て/切り上げ）で合わせ、それでも合わなければ `totals.neo_total`（コグニ計算の合計）・`totals.tolerance`（許容幅・円）・`totals.tolerance_reason`（なぜその差が出るか）の **3 つ揃えて** 差として残す。1 つでも欠けると `run_case.py` が不合格にする
- 顧客情報を Codex・外部サービスに送らない。NEO・estimate.json をリポジトリに入れない
- 既存 NEO や案件フォルダのファイルを上書き・削除しない
