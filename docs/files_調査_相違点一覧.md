# `files\` 全数調査 — 現行アプリとの相違点

調査日: 2026-09-12
対象: `C:\Users\R-T\OneDrive - 株式会社SHOUCHIKU\○竹下バックアップ\【自動集計システム※作成中】\files\`（5,992 ファイル）
比較先: `C:\Users\R-T\dev\neo-estimate\`（51 ファイル、`app.py` 9,169 行）

この文書に書いたことは**すべて実物で裏を取っています**。文書にそう書いてあった、ではなく、
実機の `.neo` を読むか、アプリを動かして出力を読むか、コードを読んで確かめた結果です。
裏が取れていない推測は「未確認」と明記しました。

---

## 0. まとめ — 直すべき順

| # | 相違 | 絶対条件への影響 | 確かめ方 |
|---|---|---|---|
| 1 | Car の車両識別 7 欄がテンプレートのまま | **「同じ車」に直撃** | アプリの出力を復号して実測 |
| 2 | 自己修復が明細を削って合計を合わせられる | **「同じ明細行」に直撃** | コード読解（`app.py:5631`） |
| 3 | ERParts が全行「手入力行」 | 金額は合う。見え方が変わる | 実機 80 件 2,830 行と比較 |
| 4 | Setting に税区分しか書いていない | 工賃を再計算されると崩れる | `grep` で 1 箇所のみ確認 |
| 5 | 画面 CSV 経路に価格未検証を「一致」にする穴 | 誤った部品コードが入る | コード読解（`auto_matching.py:1515`） |
| 6 | `nk_Total*` が 0 固定 | 内板骨格のある見積で小計が合わない | `app.py:1726` |

---

## 1. 🔴 Car テーブル — 車両識別 7 欄がテンプレートの値のまま

### 実測

テンプレートから `.neo` を作り、中の `Car` テーブルを読みました。

| 欄 | アプリの出力 | テンプレート | |
|---|---|---|---|
| MakerCode | `'I'` | `'I'` | **残る**（`I` = トヨタ） |
| CarCode | `'Z10'` | `'Z10'` | **残る** |
| YearCode | `'00'` | `'00'` | **残る** |
| BodyCode | `'10'` | `'10'` | **残る** |
| GradeCode | `'A'` | `'A'` | **残る** |
| FVACode | `'T'` | `'T'` | **残る** |
| BodyImageCode | `'03'` | `'03'` | **残る** |

アプリが書き換えるのは `CarName` / `CarNameByUser` / `ColorCode` / `ColorName` / `TrimCode` の **5 欄だけ**（`app.py:1940-1955`）。
`MakerCode` 以下の欄名は **`app.py` 全体に 1 箇所も出てきません**。

つまり **どの車の見積を入れても、生成される `.neo` の車両識別は「トヨタ Z10」のまま**です。
（原本 `.neo` をアップロードするマージ経路では原本の値が残るので影響しません。テンプレートから作る経路の話です。）

### 経緯

旧アプリ `files\app.py:1055-1090` には **15 欄の UPDATE** があり、合成コード（`TOYOTA_GENERIC` 等）を弾くガードも付いていました。
2026-05-01 の引き継ぎ書 §14 に「5 列→15 列に拡張、N1 が 22.12%→29.65% に改善」と記録されています。

`git log -S"CarFormCode" -- app.py` が **0 件**。`neo-estimate` リポジトリには最初から入っていません。リポジトリを移すときに落ちています。

### 片側だけ残った配線

`pdf_to_neo_pipeline.py:1041-1055` は今も `maker_code` / `car_code` / `car_form_code` / `form_code_1` / `form_code_2` /
`fva_name` / `body_code` / `grade_code` / `year_code` / `finish_code` / `color_name` の 11 キーを `cust` に詰めています。
**受け側が消えたので全部捨てられています。**

### 直すときの注意

`pdf_to_neo_pipeline.py:2588` に

```python
vehicle_info.setdefault("maker_code", vc[0])      # ADDATA フォルダ名の 1 文字目
```

が残っています。2026-05-01 の §15 が「これは誤り」と断定した実装そのものです。

| | ADDATA フォルダ | KATB010 の MakerCode |
|---|---|---|
| トヨタ | U / W / T / Y / X | **I** |
| ホンダ | H | D |
| ダイハツ | D | B |
| いすゞ | **I** | E |

正しい実装は既に**存在しますが呼ばれていません** — `_addata_db_search.py:977 get_maker_code()`（KATB010 経由）、
`addata_vehicle_resolver.py:226 maker_of_car()`（KATB030 経由）。配線するならこちらを通すこと。
`vc[0]` のまま繋ぐと、5 月に見つけた勘違いをそのまま本番に流します。

---

## 2. 🔴 自己修復が明細を削って合計を合わせられる

合計が 1,000 円以上ずれると（`SELF_CORRECTION_THRESHOLD`、`app.py:323`）、AI に見積書を読み直させます。
そのとき `app.py:5589` が**動的にこう指示します**:

```
- 部品金額を合計 38,600円分 削減すること
- 工賃を合計 12,000円分 削減すること
```

同じプロンプトの原則には「**勝手な査定、減額、工法変更、項目削除をしない**」とあります。同一指示文の中の矛盾です。

採用条件（`app.py:5631-5639`）:

```python
if new_error >= old_error:
    return None                      # 誤差が改善しなければ不採用（効く）
if old_count and len(new_items) < old_count * 0.7 and new_error != 0:
    return None                      # 行数ガード ← new_error != 0 のときだけ
```

**合計がぴったり合えば（`new_error == 0`）、明細が何行減っても採用されます。**

検証もすり抜けます。`verify_neo_against_pdf` の `pdf_count` は `len(items)` なので、
原本側の行数も減っていて `count_match` が通り、`total_match` も合計一致で通ります。

旧ハーネス v22/v23/v26 の結論（「総額を合わせにいく補正は精度劣化の隠蔽」）と
`judgment_rules.md` §10-5（「合計を合わせるために明細の金額を動かすな」）に真っ向から反します。

---

## 3. 🟠 ERParts が全行「手入力行」

実機 80 件 / 2,830 行と、アプリの出力（ADDATA 無しの環境）を全欄で比べました。

| 欄 | アプリ | 実機 | |
|---|---|---|---|
| `PartsPriceByManual` | **全行 `*`** | 空 94% / `*` 6% | **真逆** |
| `WageByManual` | **全行 `*`** | 空 83% / `#` 11% / `$` 4% | **真逆** |
| `PartsNameStandard` | 空 | 値あり 99% | |
| `PartsNoStandard` | 空 | 値あり 95% | |
| `DisposalNameStandard` | 空 | 取替 81% / 脱着 11% … | |
| `BlockCode` | 空 | A01 17% / A20 10% / X35 7% … | |
| `PartsPriceStandardOutTax` | `-1` | `-1` は 5% だけ | |
| `ChangeTotalOutTax` | `-1` | `-1` は 5% だけ | |
| `PartsFileTime` | 空 | `0` が 98% | |
| `WorkCode` / `ConstructGroup` | 空 | 空 77% / 87%（実機も多くは空） | 差は小さい |

`INSERT INTO ERParts` は `app.py` に **1 箇所だけ**。標準欄を書く経路がそもそもありません。

### 何が起きるか

**金額は原本と一致します**（`PartsPriceOutTax` / `WageOutTax` は正しく入る）。崩れるのは見え方です。
コグニで開くと全行が「手打ちで入れた行」に見えます。協定の場で「ADDATA で引いた見積ではない」と分かります。

なお `WageByManual='*'` の行は実機 54 行すべて `Time=-1` で、**アプリもこの規則には従っています**（`Time=-1` を書いている）。
問題は `*` を全行に付けていることです。実機では `#` が 11%、`$`（暫定指数）が 4% あります。

---

## 4. 🟠 Setting に税区分しか書いていない

`UPDATE Setting` は `app.py:2041` の **1 箇所だけ**:

```python
cur.execute('UPDATE Setting SET TaxKindFlag=?', (tax_flag,))
```

`wb_PriceBase`（レバーレート）は**リポジトリ全体で 0 件**。`wi_Round` / `tx_Unit` / `tx_ArrangeFlag` も 0 件。
実機は `wb_PriceBase` が 6190 / 6390 / 6110 / 6410 / 6000 / 6290 など、テンプレートは **0**。

### このPCの `AnOption.ini` から全欄が確定します

`C:\Program Files (x86)\Audatex\Auda7\AudaData\AnOption.ini` は読めました。

| AnOption.ini | Setting の欄 | 値 | 意味 |
|---|---|---|---|
| `[ConsumptionTax] InputMode` | `TaxKindFlag` | 0 | 外税 |
| `[ConsumptionTax] TaxPercentage` | `TaxRate` | 10 | |
| `[ConsumptionTax] TaxCalculation` | `tx_CalculateFlag` | 1 | |
| `[ConsumptionTax] TaxUnit` | `tx_Unit` | 1 | 円単位 |
| `[ConsumptionTax] TaxReduction` | `tx_ArrangeFlag` | **1** | **四捨五入** |
| `[Wage] BaseUnit` | `wb_Round` | 10 | |
| `[Wage] IndividualUnit` | `wi_Round` | 10 | |
| `[Wage] Base` | `wb_PriceBase` | 0 | この PC は未設定 |

**亮平さんが仰っていた「税込/税抜の設定」と「端数処理（切り捨て・切り上げ・四捨五入）」は、ここにあります。**

実機 725 件の測定（`tx_ArrangeFlag` = 1 が 703 件 / 2 が 21 件 / 3 が 1 件）と、この INI（`TaxReduction=1`）が
**独立に一致**しました。`1 = 四捨五入`、`2 = 切り捨て`、`3 = 切り上げ` で確定です。

`TaxUnit_Disable=1` もあり、工場ごとに税の単位をロックできることが分かります。

---

## 5. 🟠 画面 CSV 経路に「価格を見ていない行を一致にする」穴

同じアプリの中に、許容の違う照合経路が 2 つあります。

| 経路 | 関数 | 呼び出し元 | 許容 |
|---|---|---|---|
| PDF 直変換 | `_full_addata_match`（`auto_matching.py:1777`） | `pdf_to_neo_pipeline.py` | L1: 差 <1% かつ品番一致 / L2: 差 <2% |
| 画面の CSV 取り込み | `match_pdf_items_to_addata`（`:1373`） | **`app.py:3792`** | L1: 完全一致 / L2: 差 **<15%** |

さらに `auto_matching.py:1515`:

```python
else:
    level = 'L2' if rep else 'L3'     # PDF 側に価格が無い行を L2 にしている
```

PDF 経路（`:2158-2160`）は**同じ状況を L3 にして**、こうコメントまで残しています:

> 単価が0や負の行（脱着・工賃だけの行など）は価格を検証できていない。
> L2（価格一致）にすると、検証していない行が「一致」として扱われ、マーカーも出ない。

**片方だけ直っています。** 部品コードの採用は L1/L2 限定（`:1555-1562`）なので、
L2 になると**価格で裏を取っていない部品コードが `.neo` に入ります**。

---

## 6. 🟡 `nk_Total*` が 0 固定

`app.py:1726`:

```python
nk_TotalOutTax=0, nk_TotalInTax=0, nk_TotalTax=0,
```

内板骨格修正のある見積では小計が合いません。現状は「骨格の行も部品/工賃として計上する」ため総額は合いますが、
コグニ側の内訳表示は実機と変わります。

---

## 7. 文書が間違っていて、アプリが正しいもの（直さないこと）

| 論点 | 旧文書 | 実機 / アプリ |
|---|---|---|
| **DisposalCode の板金** | `マッチングルール.md` = 2 / `完全統合仕様書` §6.5 = 3 | **6**（実機 121 行すべて）。`neo_rules.py` が正 |
| DisposalCode の体系 | 3 値 / 6 値 / Repair0〜9 の 10 定義 | **7 値**（実機 202 件・11,254 行） |
| 12.DB の部品名位置 | `完全統合仕様書` §2.4 = `[14:44]`「★正解」 | **`[13:43]`**（同じ表の `[43]='$'` と矛盾＝旧が off-by-one） |
| 11.DB / 13.DB | 「暗号化なし UINT16 ペア配列」「B×10 = 価格」 | **LCG 暗号 72B/エントリ**。古い文書に合わせて戻さないこと |
| ADDATA フォルダ頭文字 | 「頭文字＝メーカー」 | 誤り。KATB030 で引く（`J97` はホンダ N-BOX） |
| `AnSMB.txt` の正体 | 「テンプレート NEO では SQLite がこの名前」 | ファイルテーブルの割り当て誤り。実体は別 |

`マッチングルール.md` の 3 値表は、文書自身が「実装コード（`auto_matching.py` の `_disposal_map`）を根拠に修正」と書いています。
**実機を一度も見ずに、当時のアプリのコードを写して「仕様」にした**ものです。循環参照なので採用してはいけません。

---

## 8. スキル側の生成器が誤っていて、アプリが正しいもの

`claude_neo_pipeline`（pdf-to-neo スキル）の `neo_container.py` と、アプリの `neo_header.py` の比較。

**cp932w（IBM 拡張漢字）の写像が 15 字ずれています。**

| | 生成器 | アプリ |
|---|---|---|
| 写像の範囲 | FA〜FC 行へ全部（388 字） | `cp932` が ED/EE 行に落とす字だけ（373 字） |
| `㈱ № ℡ Ⅰ〜Ⅹ ￢ ∵` | FA 行で書く | **NEC 行（`87 8A` 等）で書く** |

アプリ側のコメントに「実機 04011406.neo は `87 8A` だった」と**実機根拠**があります。**生成器が誤り**です。
`㈱` は協定工場名にも顧客名にも普通に出ます。

なお、コンテナ（CK 連鎖・辞書・チャンク・ファイルテーブル）のアルゴリズムは**完全に同一**で、
内部ファイル名のラベルが 1 つずれている件は、読み書きが同じずれ方なので**往復でバイト一致**します（実害なし・既知）。

---

## 9. 生成器にあって、アプリに無い機能（不具合ではなく機能差）

- 車検証 → ADDATA 車種特定、`trim_code` の 29.DB 検証、CarEVA（装備コード）
- 11.DB × 15.DB の標準指数の補完（連動加算・枠取り合い・骨格組合せ・吸収）
- `WorkCode` / `ConstructGroup` を 11.DB の D 行・S 行から埋める
- `PartsNameStandard` / `PartsNoStandard` / `PartsPriceStandard*` / `ChangeTotal*` の書き込み（→ 本文 3）
- 色別部品（83/13.DB）・リサイクル部品（`RCParts` / `RCLinkParts`）
- 塗装詳細（`PaintingPanel`、20/66/BAN/2TONE/fukaetc.DB、材料代自動計算）
- 内板骨格修正（`Frame` / `FramePlan`、`BANKIN.DB`）
- ADAS 作業行（`adas_db.py` → `ReserveERParts`）
- 保留行の分離（`ReserveERParts`）

逆にアプリにあって生成器に無いもの: `merge_mode` / 税込入力の逆算 / ベタ打ちモード /
展開爆弾の防御（`MAX_DECOMPRESSED_SIZE` 等）/ 顧客情報入り一時 DB の確実な削除 / `_xml_escape`。

---

## 10. まだ使っていない知見

### DLL の strings に全テーブルの DDL が平文で入っている

`files\_archive\harness_2026-05_09\_harness_v9_5_dll_exe_safe_analysis\strings_dump\`

- `AxDBAcsFlSqlEm.dll` → ERParts(74 列) / Expense / PaintingBumper / PaintingEtcetera 等の `CREATE TABLE`
- `AxDBAcsFlSqlIf.dll` → Car / Customer / Insurance / **Setting** 等 12 テーブル
- `AxDBAcsEnv.dll` → **NeoTotal の全列**、および**列の既定値が直書きされたマイグレーション SQL**

列幅が確定します: `PartsCode TEXT(4)` / `PartsName TEXT(24)` / `PartsNo TEXT(18)` / `WorkCode TEXT(10)` /
`ConstructGroup TEXT(3)` / `BlockCode TEXT(3)` / `Expense.Name TEXT(20)` / `Comment1-3 TEXT(40)`。

**`ERParts.LineNo` は `UNIQUE`** — 同じ `LineNo` を 2 行書くと INSERT が落ちます。生成前にアサートを入れる価値があります。

書き込みは常に「全消し＋全行 INSERT」で、`UPDATE` 文は 1 本もありません。

### 独立した回帰テストケース

`_archive\Addata完全解析書.md` §12 に、ライズ A201A（W64/W25）の **27 行フル突合せ表**（Total = 391,886 円）があります。
現行の `tests/` に無い独立ケースです。ADDATA のある環境で回せば価格チェーンの回帰に使えます。

---

## 11. 未確認・要判断

| 項目 | 状況 |
|---|---|
| `AnSMB.txt` の `[100:105]` | 現行は実機 202 件・11,254 行から割り出した `'00000'`/`' 0000'`。新仕様（スキル側）は `[100]=' '` 固定 + Recycle/Reserve フラグ。**衝突している。1 件突合せて確定させること** |
| 環境 DB の所在 | `AnUsrTbl.sld`（DLL 解析）vs `AnUsrTblSU.sld`（2026-04 解析書）で食い違い |
| `Rr→R` / `Fr→F` の向き | `auto_matching.py:866` と `:1220` は `Rr→R`（正）、`:1962` の `_SYN_MAP` は `Rr→RR`。**両方ある**。どちらが照合に効くかは経路次第で未確認 |
| 内訳の税と総額の税 | 実機の `Total` は欄が入れ子で未解明。総額と各行は原本と一致するので実害なし |

---

## 付録: `files\` の構成

| 場所 | 件数 | 容量 |
|---|---|---|
| `_archive\` | 5,544 | 918 MB |
| 直下 | 821 MB | うち `cogni_code_master.db` が 815 MB |

`cogni_code_master.db`（815 MB）は **廃止確定**。実テーブルは 2 個だけで、`parts_price_range` は
2,040 万件のうち 34% が `start > end` の壊れた行。**コードからの参照はゼロ**です。
