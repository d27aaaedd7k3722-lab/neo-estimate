# CLAUDE.md

## コミュニケーション規則

- 返答は常に日本語で行うこと。ファイルの修正提案や説明もすべて日本語で行うこと。

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

## 変更するときの手順

1. 実装する
2. 回帰テスト6本を通す（このPCでも動く）

```
cd tests
python reg_neoacc.py && python reg_misread.py && python reg_cache.py
python reg_expense.py && python reg_pipeline.py && python suite.py
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
