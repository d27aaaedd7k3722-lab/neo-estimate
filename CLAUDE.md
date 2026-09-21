# CLAUDE.md

## コミュニケーション規則

- 返答は常に日本語で行うこと。ファイルの修正提案や説明もすべて日本語で行うこと。

## セッションを始めるとき

**まず `docs/次のセッションへ.md` を読む。** いまの状態・次にやること・落とし穴・道具がまとまっている。
全体像は `docs/引き継ぎ書.md` の §13「総まとめ」。

## このリポジトリについて

車両修理の見積書（PDF／CSV）を、コグニセブンの `.neo` ファイルに変換する Streamlit アプリ。

| | |
|---|---|
| 本番 | https://shouchiku-neo-estimate.streamlit.app/ （このリポジトリの `main` を Streamlit Community Cloud が配信） |
| ローカルの作業場所 | `C:\Users\R-T\dev\neo-estimate` |
| 検証用 venv | `C:\Users\R-T\.venvs\shouchiku8`（Python 3.11 + requirements.txt） |

開発は 2026-09-10 に `shouchiku8-app` からこのリポジトリへ**一本化した**。
`shouchiku8-app` は履歴の保存用で、もう触らない。

## 絶対条件（他のすべてに優先する）

生成された `.neo` は協定見積として保険会社に提出される。
**元の見積書と、同じ明細行・同じ金額・同じ部品・同じ車であること。**
行が1本増えても減っても、1円違っても使えない。「合計だけ合っていればよい」ではない。

## 画面の考え方（2026-09-10 亮平さんの整理）

- **見積書PDF を投げ込むのが主導線**。step1 の一番上に置く
- **Gemini に読ませて CSV 化するのは、PDF の読み取り精度が出ないときの代替手段**。下に置き、そう分かる文言にする

## 見積 PDF → NEO は pdf-to-neo スキル経由（2026-09-13〜）

判断・生成・検算は files リポジトリ（`pdf-to-neo` ブランチ）のスキルを `vendor/pdf_to_neo/` に
**コミット固定で取り込んで**そのまま呼ぶ（`neo_skill/`）。アプリが持つのは「見積書を Claude API に読ませ、
ページごとに検算し、落ちたページだけ読み直す」ところだけ。**`vendor/` 配下は書き換えない**（規則は files で直して取り直す）。

- 取り込んでいるコミット: `vendor/pdf_to_neo/VENDOR_COMMIT.json` と `neo_skill/vendor.py` の `EXPECTED_COMMIT`（コミットの番号はここに書かない。古くなるので `VENDOR_COMMIT.json` を見る。取り直しの履歴は docs/引き継ぎ書.md の表）
- 取り直し: `python tools/vendor_sync.py --source "<files>" --commit <ID>`／改変チェック `--check`
- 見積書を読む LLM は Gemini API（`.env` の `GEMINI_API_KEY`）が主。Claude API（`ANTHROPIC_API_KEY`）もあれば画面で選ぶ（既定 Gemini）、Gemini だけならそれで動く。判断・生成・検算は同じ
- 合格の条件は vendor の `make_neo.py` と同じ。NEO と確認箇所シート（xlsx）は必ず組で出す。
  **検算に通らなかった案件でも NEO と確認箇所シートは渡す**（2026-09-17 亮平さん指示）。ファイル名に `_要確認` を付け、
  画面と報告文に「印字との違い」を必ず出す（合格したものと取り違えさせない。金額を合わせるための行の削除・改変はしない）
- **読み手には金額を印字どおりに写させる**（税込・税抜の判断をさせない）。各行の金額まで税込で刷る見積書（ディーラー・二輪。
  判断規則 10-4）は、vendor の `reading_pages.merge` が**印字の合計欄と明細の積み上げだけ**で見分けて税抜に直し、
  生成器がコグニの消費税設定を内税（`Setting.TaxKindFlag=1`）にする。引き継ぎ書 §12.25
- 旧経路（`pdf_to_neo_pipeline` / `auto_matching`）は UI から外した。関数は残してある
- 詳細は `docs/引き継ぎ書.md` §12、方針は files の `docs/pdf-to-neo_アプリ移植ガイド.md`
- **別 PC の ADDATA**: `C:\Addata`／`D:\Addata` なら設定不要（自動検出）。他の場所はサイドバーのパスか `env_check.py --save`。詳細は引き継ぎ書 §12.9
- **本番（Cloud）から使う ADDATA**: サーバは PC を読めないので、サイドバー「🖥️ PC の Addata をこの画面から使う」で PC の `C:\Addata` を選ぶ（ブラウザの File System Access API で COM 9MB と見積の車種フォルダだけを送る橋渡し。`neo_skill/bridge.py`・`neo_skill/addata_bridge/index.html`。Chrome / Edge）。取得URL（`?addata_url=`）は 300MB までの代替。詳細は引き継ぎ書 §12.10
- **Addata が決まらないとき**は pdf-to-neo の経路は動かさず、**ベタ打ち（旧経路 `run_pdf_to_neo_pipeline`、部品コード・標準指数なし）** のボタンを出す（2026-09-14 亮平さん指示。引き継ぎ書 §12.11）。試験用 `NEO_ADDATA_NO_AUTODETECT=1` で自動検出を飛ばせる
- **添付の車検証・事故/保険の書類**（見積書の uploader の直下。生成ボタンより前に描く: ボタン処理内の `st.rerun()` は未描画の uploader の値を捨てる）は Gemini で読み、`neo_skill/doc_hints.py` がスキル経路の hint・ベタ打ちの vehicle_info・サイドバーの保険欄に写す（優先順: 見積書の印字 ＞ サイドバー ＞ 書類 ＞ 車検証）。登録番号の分類番号・一連番号は実機どおり半角数字・ハイフン無し、かなは全角ひらがな（`_reg_no_part`）。住所は市区郡 '〜市' まで（`split_address`）。見積書が別のファイルに変わったら案件の入力を消す（`_reset_case_inputs`）。生成結果には入力の指紋 `inputs_sig`（変わったらダウンロード無効）。引き継ぎ書 §12.12・§12.13
- **Linux（Community Cloud）で Windows と同じ NEO にする条件**（2026-09-14）: `packages.txt` の `p7zip-full`（塗装指数表 CHM の展開。無いと修正塗装のあるパネルの見積は理由付きで不合格になる）。COM.CAB は純 Python で展開する。展開キャッシュは `LOCALAPPDATA`（無ければ一時フォルダ。`neo_skill/vendor.py subprocess_env` が渡す）で、vendor の中には書かない。詳細は移植ガイド §4-1

## 変更するときの手順

1. 実装する
2. 回帰テストを通す（このPCでも動く）

```
cd tests
python reg_neoacc.py && python reg_misread.py && python reg_cache.py
python reg_expense.py && python reg_pipeline.py && python suite.py
python reg_reader.py      # 読む段のループ（LLM 差し替え・課金なし）
python reg_vendor.py      # 受け入れ §5-2: NEO_check の案件で files と vendor の NEO が全列一致
python reg_vendor_units.py   # 取り込んだ vendor 自身の単体テスト 22 本（判断規則の回帰。vendor を取り直したら必ず）
```

3. `codex-loop` スキルで Codex レビュー（gpt-5.5 / xhigh）を通す
4. `git push origin main` → Streamlit Community Cloud が自動で再デプロイ
5. 本番確認は `https://shouchiku-neo-estimate.streamlit.app/~/+/` を開く
   （トップURLだと iframe 越しでサイドバーが折りたたまれ、見積日などが読めない）

## 実機のNEOを解析する

コグニセブンが作った `.neo` を渡すと、実機でないと確定できない項目をまとめて出す。

```
python tests/analyze_real_neo.py <実機が作った.neo>
```

何を作ってもらえばよいかは `docs/引き継ぎ書.md` §6 に書いてある。

## 詳しい引き継ぎ

`docs/引き継ぎ書.md` にすべて書いてある（何を直したか・その判断の根拠・実機で確認したい項目・環境の地雷）。

## 環境の地雷

- `requirements.txt` のバージョンは固定してある。上げるときは Python 3.9〜3.14 のどれでも入るかを必ず確認する
  （固定しないと再ビルドで依存が上がり「なんてこった。／Oh no. Error running app.」で起動しなくなる）
- `.streamlit/config.toml` に Cloud Run 向けの設定（`server.port` / `enableCORS` など）を書かない。
  Community Cloud が起動しなくなる
- Streamlit Community Cloud は1プロセスを全利用者で共有する。
  モジュールレベルのキャッシュ・グローバル変数は他人にも見える
- `-1` はコグニセブンでは空白表示。`0` や `NULL` は「0」と表示される
- 列幅は CP932 の**バイト数**であって文字数ではない。`cp932_trim` で切る
- 日付は `now_jst()` を使う。`datetime.now()` は本番コンテナが日本時間でないため1日ずれる
- `.gitignore` に `analyze_*.py` などの一時ファイルパターンがある。
  `tests/` 直下は `!tests/*.py` で除外を打ち消してある（過去にこれで `tests/analyze_real_neo.py` が消えた）
