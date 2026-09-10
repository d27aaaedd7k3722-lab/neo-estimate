# 回帰テスト

生成される `.neo` は協定見積として保険会社に提出されるため、
**元の見積書と同じ明細行・同じ金額・同じ部品・同じ車であること**が絶対条件。
ここにあるテストは、その条件を壊した実績のある不具合を1件ずつ固定したもの。

## 実行

```
cd tests
python3 reg_neoacc.py     # NEO生成 16項目
python3 reg_pipeline.py   # PDF経路 144ケース
python3 reg_misread.py    # 小計の誤読・少額行の読み落とし
python3 reg_cache.py      # 同じ入力から同じ .neo が出ること
python3 reg_expense.py    # 諸経費と消費税の丸め（乱数120ケース×税抜/税込）
python3 suite.py          # CSV経路のスイート
```

`XROOT=<別のツリー>` を付けると、そのツリーの `app.py` に対して実行できる。
新しいテストを書いたら、**修正前のコミットで実際に FAIL することを必ず確認する**こと。

```
git worktree add -f /tmp/wt_pre <修正前のコミット>
XROOT=/tmp/wt_pre python3 reg_xxx.py     # ここで FAIL すること
git worktree remove --force /tmp/wt_pre
```

過去に、テスト自身に穴があって不具合を素通りさせた例が4件ある
（キャッシュを毎回消していた／内部の整合しか見ていなかった／
±1円の許容を置いて±1円のずれを見逃した／金額が全部1000円単位で
消費税に端数が出なかった）。詳細は `docs/引き継ぎ書.md` §7。

## 実機のNEOを解析する

コグニセブンが作った `.neo` を渡すと、実機でないと確定できない項目
（作業区分コード・非課税の符号・元号コード・`AnNote.ini` の1レコード長など）を
まとめて出す。

```
python3 analyze_real_neo.py <実機が作った.neo>
```

何を作ってもらえばよいかは `docs/引き継ぎ書.md` §6 に書いてある。

## ファイル

| ファイル | 役割 |
|---|---|
| `neogen.py` | `.neo` を生成／展開／SQLiteとして開くヘルパ |
| `zz_h.py` / `zz_cases.py` | PDF経路を偽Gemini経由で通すハーネスとケース生成 |
| `gm/` | `google.genai` の偽実装（APIキー無しで経路を通すため） |
| `harness.py` | `app.py` から関数をAST抽出する（CSV経路のスイート用） |
| `fixtures/a4_1p.pdf` | ハーネスに食わせる最小のPDF |
