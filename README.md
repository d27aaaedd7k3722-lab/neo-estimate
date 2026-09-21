---
title: Shouchiku8 Neo
emoji: 🚗
colorFrom: blue
colorTo: indigo
sdk: streamlit
sdk_version: "1.45.1"
python_version: "3.11"
app_file: app.py
pinned: false
---

# NEO見積変換アプリ

自動車の修理見積書（PDF・写真）を読み取り、**コグニセブンが読む `.neo` ファイル**を作る Streamlit アプリ。

- 本番: https://shouchiku-neo-estimate.streamlit.app/
- main に push すると Streamlit Community Cloud が自動で入れ替える

## 最初に読むもの

| 文書 | 中身 |
|---|---|
| **`docs/次のセッションへ.md`** | **いちばん最初に読む。** いまの状態・次にやること・落とし穴・道具 |
| **`docs/引き継ぎ書.md` の §13「総まとめ」** | いまの状態・構造上の弱点・残っていること。**ここだけ読めば現状が分かる** |
| `docs/引き継ぎ書.md` の §1〜§12 | これまでの経緯（何をなぜ直したか） |
| `docs/コグニ本体_解析メモ.md` | コグニ本体の解析で分かったこと／**まだ実装していないもの** |
| `CLAUDE.md` | この repo で作業するときの決めごと |
| files 側の `NEO_FILE_SPEC_COMPLETE.md` | NEO ファイルの仕様（実機で確定した項目） |

## 絶対ルール

1. **見積書に印字された金額と違う NEO を黙って出さない。止まってよい。**
   合計合わせのための行削除・金額移動はしない
2. **顧客情報（氏名・登録番号・車台番号）をログ・リポジトリ・画面のエラー文に出さない**
3. **同じ入力なら同じ NEO**（AI の読み取りは毎回ゆれる。ゆれても金額が動かない設計で受ける）
4. **塗装は必ず実額**（InputType=0・合計 1 つ）。ベタ打ちは記載内容をそのまま転記
5. CSV 取り込みは「**決められなければ止める**」

## 3 つの生成経路

| | 🚀 NEOを生成 | ✏️ ベタ打ちで生成 | CSV 取り込み |
|---|---|---|---|
| 中身 | `neo_skill`（vendor のスキル） | `pdf_to_neo_pipeline.py` | `parse_csv_to_items` → プレビュー |
| Addata | 要る | 要らない | 要らない |
| 部品コード・標準指数 | 入る | 入らない | Addata があれば引く |
| 税込/税抜 | 合計欄から**自動判定** | 画面のラジオ（手動） | 画面のラジオ（手動） |
| 原本との照合 | する | する | **できない**（印字の小計とだけ突き合わせる） |

**決めごとが 3 つとも別実装**なので、片方を直したら必ず 3 つとも確かめること。

## 動かす

```bash
streamlit run app.py
```

API キーは `.env` の `GEMINI_API_KEY` / `ANTHROPIC_API_KEY`（本番は Streamlit の Secrets）。
Addata は画面のサイドバー「🖥️ PC の Addata をこの画面から使う」で PC の `C:\Addata` を選ぶ
（Chrome / Edge。選んだフォルダはブラウザが覚える）。

## テスト

```bash
python tests/reg_insurance.py
```

回帰テストは `tests/reg_*.py` の **20 本**。受け入れテストは `tests/reg_vendor.py`（実案件 20 件と突き合わせ・不一致 0 が合格）。

**`reg_vendor_units` は「23 本 OK / 1 本が不合格」が正常**です。vendor に取り込んだ単体テストの写しだけが古く、
中身は正しく金額も動いていません（引き継ぎ書 §13-6「既知の不合格」を参照）。**数が変わったら本物の退行**。

**テストを足したら「わざと壊して落ちること」まで確かめる。**
`sys.exit()` の後ろに書くと素通りする（2026-09-21 に実際にやってしまった）。

## vendor（pdf-to-neo スキル）

判断・生成・検算は files リポジトリのスキルをそのまま呼ぶ。commit 固定で取り込む。

```bash
python tools/vendor_sync.py --check
python tools/vendor_sync.py --source <filesのパス> --commit <SHA>
```

## 本番に入ったかの確かめ方

`neo_skill` を触ったなら画面の印（`アプリ側 neo_skill: xxxxxxxx`）が変わる。
**手元の `python -c` と比べてはいけない** — Windows は CRLF・本番 Linux は LF なので必ず食い違う。
git の blob から LF のまま計算すること。

`app.py` だけ触ったコミットは印が変わらないので、無条件で出る CSS を見るのが速い。
