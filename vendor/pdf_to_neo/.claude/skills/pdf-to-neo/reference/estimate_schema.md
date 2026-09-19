# estimate.json スキーマ（run_case.py / NeoBuilder.build が読むキー）

正は `claude_neo_pipeline/estimate_to_neo.py`（`build` / `build_rows` / 塗装・費用の各処理）と `README.md`「estimate.json の形」。ここでは 2026-09-07 時点で使っている全キーを、実案件での使い方と一緒に書く。文字列は UTF-8、金額は円の整数、指数は小数（0.1 刻み）。

```json
{
 "source": "元 PDF のパスと書式の説明（人が読むメモ。パス区切りは / を使う）",
 "issuer": "見積作成者（工場名・住所）",
 "est_date": "20260803",
 "vehicle": {...}, "customer": {...}, "insurance": {...},
 "labor_rate": 8500,
 "wage_round": 10,
 "index_policy": "auto",
 "hints": {"eva_codes": ["K", "N"]},
 "items": [...], "paint": {...}, "expenses": [...],
 "frame": {...}, "discount": {...}, "adas": [...],
 "totals": {...}
}
```

## vehicle（車検証）

| キー | 例 | 備考 |
|---|---|---|
| model_code | `AGH30W` | 型式（車検証の「型式」から排ガス記号 `3BA-` を除く。resolver が KA06 で車台番号レンジと突き合わせる） |
| serial_no | `AGH30-0000001` | 車台番号 |
| desig / category | `19553` / `0384` | 型式指定番号 / 類別区分番号。KA81 でグレード・FVA・年式が決まる最重要キー。類別は 4 桁ゼロ埋め |
| reg_date | `R4.3` / `H27.5` | 初度登録年月。KA81 の生産期間で年式（Car.YearCode）を選ぶ |
| color_code | `070` / `B589P` | 車検証には無い。工場見積の「カラーNo」か車体のコーションプレート。13.DB/83.DB の色別品番に効く |
| color_name / engine / cc | 任意 | 汎用車種や表示補助 |
| generic / car_code / maker_code / car_name | `true` / `Z10` / `I` / `ﾎﾞﾙﾎﾞ V40 MB4164T` | コグニ非収録車のとき。Z10 乗用車 / Z20 1BOX / Z30 トラック |
| trim_code | `20` | トヨタのトリムコード（`<car>29.DB` の候補に無ければ例外） |

## labor_rate / wage_round / tax_round / index_policy

| キー | 例 | 備考 |
|---|---|---|
| labor_rate | 8500 | レバーレート（円/h）。省略時は明細から推定 |
| tax_round | "四捨五入" / "切り捨て" / "切り上げ" | 消費税の計算単位（コグニの消費税設定 Setting.tx_ArrangeFlag）。既定は四捨五入。draft_estimate が合計欄から自動判定して書く。切り捨ての工場でここを省くと合計が 1 円ずれる |
| wage_round | 1 / 10 / 100 | 工賃の丸め単位（工場のコグニ設定 Setting.wb_Round）。技術料に 10 円の倍数でない値があれば draft_estimate が 1 に推定する（判断規則 10-6 の 5）。100 円丸めの工場では 0.25h×11,000 = 2,750 → 2,800 と印字される（オデッセイ 2026-09-07）。draft_estimate が印字工賃から自動推定。既定 10 |
| index_policy | auto / manual | 非コグニ書式で全指数を手入力 `#` にするとき manual |
| tax_included | 10 | 金額が税込で印字された見積書（判断規則 10-4）の税率（％）。生成器が消費税設定の表示方法を内税（Setting.TaxKindFlag=1）にして、コグニの画面・帳票を見積書と同じ税込の金額で並べる。**明細に入れる金額は税抜のまま**（`reading_pages.merge` が割り戻し済み）。draft_estimate が `reading.tax_included` から写す |

## hints

| キー | 意味 |
|---|---|
| eva_codes | 装備バリエーションのレター配列（10.DB の A〜Z）。11.DB/13.DB の条件付き行（flags[5:7]）を選ぶ材料。4WD の `Z` は resolver が自動付与 |
| car_name | 車名ヒント（型式指定が無い見積の補助） |
| grade_name | グレード名で候補を絞る。同じ名前の候補が複数あるときは決まらないので `candidate` を使う |
| grade_codes | グレード記号（A/B/C…）の配列で絞る。品番の変種行から逆引きしたときに使う |
| candidate | 候補を 1 件に固定する `{car_code, year_code, body_code, grade_code, fva_code, four_wd}`（書いた欄だけ見る。`four_wd` は true/false）。同名・同記号でボディ・年式・駆動が違う候補を選び分けるとき用。一致する候補が無ければ車両特定は失敗する。`pick_grade.py` が使う |
| note | 人向けメモ（生成器は読まない）。装備を付けた根拠を書く |

## customer / insurance

| キー | 出所 |
|---|---|
| customer.name / reg_no / postal / address / prefecture / municipality / address_other / kilometer / term_date / owner / phone | 車検証（使用者名・住所・登録番号・有効期間満了日・所有者）と見積書（走行距離）。郵便番号は車検証に無いので空でよい |
| customer.owner_name / user_name | **NEO の所有者欄・使用者欄に入るのはこの 2 つだけ**。`owner` は車検証の所有者を控えるメモで NEO には書かれない（コグニ運用では所有者欄に顧客名を入れることが多い。実機 cogni_R1/R2）。所有者を出したい案件だけ `owner_name` に書く |
| insurance.company / policy_no / contractor / accident_date / presence_date / factory | 案件フォルダ名（損保）・速報 PDF。分からない項目は空文字。`factory` は「工場名 電話番号」 |
| insurance.accept_no / agency / adjuster / garage_in / garage_out / repair_days | 事故受付番号（FileInfo.AcceptNo・XML・AnSvMail）／代理店（Insurance.AgencyName）／アジャスター（Insurance.AdjusterName・XML）／入庫日・出庫日（YYYYMMDD → FileInfo.GarageIn*/GarageOut*。XML には書かない: 実機 178 本で日付があっても空）／修理日数（Insurance.RepairDays、無ければ -1）。無ければ従来どおり空（2026-09-14） |
| insurance.adjuster_post | アジャスターの支店・所属（速報報告書の「支店」「サービスセンター」）。NEO の Insurance.AdjusterPost（20 バイト）。分からなければ空文字（2026-09-14） |

## items（明細行、見積書の並び順で）

| キー | 例 | 備考 |
|---|---|---|
| code | `0010` | コグニ部品コード（12.DB の ref 4 桁）。空なら品番・名称・部位文脈から生成器が決める |
| name | `右Fﾌｪﾝﾀﾞ` | 半角カナ。生成器は 11.DB の 20 文字名称に置き換えるので表記は近似でよい |
| method | `取替` | 取替(0) / 脱着(1) / 修理(2) / 脱着修理・脱着板金(3) / 点検調整(4) / 分解調整(5) / 板金(6)。日産系の「部品」は取替扱い |
| parts_no | `52119-58988-A0` | 品番（ハイフン有無・末尾色記号込みで可）。**品番一致が最優先の照合キー** |
| qty | 2 | 数量。`price` は数量分の合計金額（単価×数量）。C-HR の JSON は `parts_price`、シエンタ以降は `price`（どちらも可） |
| price | 101000 | 部品金額（数量分）。0 = 部品代なし |
| wage | 12750 | 工賃（コグニ丸め 10 円）。0 = 付属部品（工賃なし）。**省略 = 不明**（取替/脱着行は標準指数で補完） |
| index | 1.5 | 見積書の指数。`wage ÷ labor_rate` と一致すること |
| manual | true | ADDATA 照合をせず名称・品番をそのまま書く（ステッカー・輸入車・ADDATA に無い品目） |
| bankin | `{"area": 5, "yes": [1,0,0], "fuka": [...]}` | 板金ランク。area = 損傷面積 d㎡、yes = ダイアログの YES 3 つ（3 つ=A / 0=C / 他=B）、fuka = 付加作業名の配列（省略可） |
| comment | 文字列 | 明細コメント（TEXT(40)）。**NEO に書かれてコグニの画面・印刷に出る**ので、見積書に印字された明細コメントだけにする（人向けのメモは `_memo` / 確認箇所シート。判断規則 10-22） |
| _memo / _page / _qty_from_price / _full_name | — | 下書きが付ける内部キー（転記メモ・見積のページ・金額から数量を直した印・名称を短くする前の印字の全文）。生成器は読まない |
| paint._total_from_lines | — | 下書きが `paint.total` を塗装行の合計から作ったときだけ付く（値はその中に入れた追加項目の工賃）。印字の塗装工賃計と区別して、突合せが追加項目を二重に数えないため。生成器は読まない |
| reserve | true | 保留部品（合計に入らず「保留」行として印字） |
| recycle | `{"name", "price", "stock_price"}` | リサイクル部品への置換（純正部品の工賃は消える） |

## paint

| キー | 例 | 備考 |
|---|---|---|
| total | 74800 | 塗装工賃計（材料代を含まない） |
| material / material_rate | 41141 / 55.0 | 材料代と割合。工場が行ごと丸めで合計が一括計算と 1 円違うときは見積値をそのまま（`*` で保持） |
| paint | `２Ｋ` / `水性` / `速乾` | 塗料。水性は CHM の水性ページ・93.DB・W2TONE.DB を使う |
| coat | `３コートパール` | ソリッド / メタリック / ２コートパール / ３コートパール |
| hf | `耐スリ傷` | しない / フッ素 / 耐スリ傷 |
| input_type | `"実額"` / `"指数"` | コグニの塗装の**入力方式**（その他 → 塗装 → 入力方式）。`実額` は総額を 1 つの金額で入れる方式で、**外板パネル・加算基礎・ブース・材料計を持たない**（NEO は `PaintingPlan.InputType=0`、`PaintingTotal` は `TotalOutTax` だけ）。工場見積が「塗装費用 一式」しか出していない案件に使う（実案件 NEO 400 本中 91 本がこれ）。`paint.actual: true` でも同じ。**パネル・塗装行・加算基礎・ブース・バンパ・内板骨格塗装・追加項目・材料代のどれかがあるときは指数のまま**（実額は内訳を持てない）。draft が自動で判断し、reading に書いてあればそちらを優先する。判断規則 10-15 |
| panels[] | `{"code": "1000", "name": "右Fﾌｪﾝﾀﾞ", "method": "修理", "area": 47, "ratio": "1/3", "index": 2.5, "wage": 21250}` | 20.DB のパネルコード。method 取替/修理、ratio 1/1 1/2 1/3（取替は空）。index を省略すると標準（CHM）。指定して標準と違えば Manual/`#` |
| panels[]（手入力の塗装行） | `{"manual": true, "name": "左右ﾙｰﾌｻｲﾄﾞ", "method": "修理", "index": 4.0}` / `{"manual": true, "name": "Rrﾎﾞﾃﾞｰﾌﾛｱ", "wage": 12000}` | 20.DB に無い部位を、コグニの外板パネル画面で「行追加」して手入力した行（PaintingPanel.DisposalCode 9）。`manual: true` か `code` 無しの行がこれになる。name（20 バイトまで）と、index か wage のどちらかが必須。index あり → 工賃 = 指数 × レバーレート・印 `#`、wage だけ → 指数 -1・印 `*`。method は 取替/修理/省略（省略 = 修理方法なし）。**材料代の対象**（追加項目 `other` は対象外）で、加算基礎の枚数には数えない。部品コードのあるパネルの後ろに入力順で並ぶ。判断規則 10-20 |
| base | `{"index": 4.1, "wage": 34850}` | 加算基礎数値（T_KEI_3: 車形×塗料×塗膜×高機能×枚数）。省略で標準 |
| booth | `{"index": 0.3, "wage": 2550}` | ブース加算。見積書に「ブース」行があるときだけ書く。省略 = ブース使用オフ（コグニの既定。実機 2026-09-08） |
| bumper_front / bumper_rear | `{"method": "新品", "color": "一色", "draft": false, "index": 2.2, "wage": 18700}` | 23.DB（水性は 93.DB）。method 新品/変形修正/外傷修正小/外傷修正大 |
| wax / door_sash / stripe / low_cover / two_coat_solid / two_tone | README 参照 | 付加塗装。**`panels` があるときだけ書ける**（一括計上と併用すると ValueError。生成器 `PAINT_DETAIL_KEYS`） |
| sealing / frame / other | README 参照 | ボデーシーリング・内板骨格塗装・その他。**一括計上（`panels` なし）でも書ける**（2026-09-12 訂正。ボデーシーリングを `other` に落とすと塗装工賃計と材料代が狂う。判断規則 10-13） |

塗装明細が無い見積（ディーラー概算など）は `panels` を省き `total` だけ → 下書きが `input_type: "実額"` を付け、コグニの**実額入力**（総額 1 つ）で書く（判断規則 10-15）。材料代が別に印字されている案件と `auto_panels` の案件は今までどおり（実額は材料計の欄を持てないので、「塗装費用(工場見積)」1 行の一括計上になる）。

**パネルが無くバンパだけ塗る見積**は `"panels": []` と `bumper_front` / `bumper_rear` を書く（2026-09-12 実機 W66 `cogni_W66w` で全列一致）。このとき加算基礎数値は無し（PaintingPlan.Base* = -1）で、代わりに **バンパ加算基礎**（COM/BAN.DB 車形 × 塗膜クラス。W66 2コートパール 0.5）が `BumperBase*` に入り、バンパ計に含まれる。見積書に印字されていれば `"bumper_base": {"index": 0.5, "wage": 4000}` で上書きできる（省略時は BAN.DB の標準 × レバーレート）。

## expenses（費用）

| キー | 例 | 備考 |
|---|---|---|
| name | `ショートパーツ` | 20 バイトに切詰 |
| amount | 3000 | 円 |
| kind | `parts` / `wage` | 見積書の集計先で決める: 「部品計」に入っていれば parts、「作業計・諸費用計」なら wage |
| taxfree | true | 非課税費用（合計にそのまま加算） |

## frame / discount / adas

- `frame`: `{"basic": true, "basic_index": 1.5, "basic_wage": 12000, "items": [{"code": "1400", "rank": "A|B|C", "index": 2.0, "wage": 16000}]}`（`basic` は基本修正作業の有無。指数・工賃は `basic_index` / `basic_wage`。省略で N_KIHON.DB の標準）
- `discount`: `{"parts": -1000, "wage": 0}` 値引（−）/ 割増（+）
- `adas`: `[{"code": "9900"}, {"item": "A120", "sub": "1", "index": 0.6}]` 運転支援システム再設定（55/56.DB を持つ車種のみ）

## totals（見積書の合計欄をそのまま）

| キー | 例 | 備考 |
|---|---|---|
| parts | 579740 | 見積書の「部品計」（費用部品込みでもそのまま。検算が組合せで一致判定する） |
| wage | 37400 | 「作業計」「工賃計」 |
| paint / material / paint_total | 74800 / 41141 / 115941 | 塗装工賃・材料代・塗装計 |
| expense_parts / expense_wage / expense | 3800 / 15000 / 18800 | 費用の内訳 |
| frame | 内板骨格の合計 | あれば |
| taxable / tax / total | 748081 / 74808 / 822889 | 課税小計・消費税・御見積額 |
| page1..pageN | `[113320, 35010]` | ページ小計（部品・工賃）。生成器も run_case も読まない。ページ小計の検算は reading.json の `pages` と `blocks[].page` で行う（reading_check）。ここに書いた値は書式 A の判定ヒントになるだけ |
| tolerance_keys | `["parts", "taxable", "tax"]` | tolerance を効かせてよい項目の名指し。既定は工賃・塗装・内骨・課税小計・消費税だけで、**部品計・材料代・費用は 0 円一致**。単価に円未満の端数がある見積（10-31）だけ下書きが `parts` を足す |
| neo_total / tolerance / tolerance_reason | 693732 / 8 / "FAX は工賃を円未満まで計上…" | 工場書式の丸めでコグニと差が出る案件だけ。**3 つ揃えて書く**（コグニ計算の合計 / 許容幅・円 / なぜ差が出るか）。1 つでも欠けると `run_case.py` が不合格にする。理由は `tolerance_reason` が正式名（古い案件の `note` も読む） |
| tax_rate | 10 | 消費税率（％）。省略時 10。`run_case.py` の「金額が税込で印字されている見積」の判定に使う |
| allow_unmatched | 使わない | 照合漏れの許容件数。原則 0 |
