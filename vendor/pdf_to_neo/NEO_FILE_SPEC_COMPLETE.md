# NEO ファイル完全仕様（コグニセブン 2.1.1.3 / 車種データ 2026-07 実測版）

作成 2026-09-04。根拠: 実 NEO 11 件（コグニ作成）、コグニ保存版との全テーブル diff、コグニ実機での読込・印刷・保存検証、コグニ DLL の文字列（SQL・レコード定義）。
実装は `claude_neo_pipeline/`（`neo_container.py` = コンテナ、`estimate_to_neo.py` = テーブル書込）。ADDATA 側は `ADDATA_REVERSE_LOOKUP_SPEC.md`。

---

## 1. コンテナ

| 項目 | 値 |
|---|---|
| マジック | `NEO300101`（9B）＋ 415B の管理領域（§1-1。既存見積一覧が読むサマリ。`neo_header.py` で復号・生成） |
| 圧縮 | raw deflate（wbits=-15）。`CK` マーカーで区切られたチャンク連鎖、2 チャンク目以降は直前 32KB を辞書に使う |
| ファイルテーブル | 管理領域 424B の後、`[DOS 日時 4B][属性 2B]\ファイル名\0` ＋ 次エントリの `[size 4B][offset 4B][00 00]`。先頭エントリの size/offset は管理領域側 |
| 内部ファイル（12 個・出現順） | `<見積名>.xml`, AnCooperate.txt, AnDBVersion.ini, AnFlInfo, AnNote.ini, AnSMB.txt, AnSvEm0001.sld, AnSvEm0001Ex.db, AnSvIf0001.sld, AnSvIg0001.sld, AnSvImge.ini, AnSvMail.ini |
| ファイル名 | `MMDDhhmm.neo`（コグニは保存時刻）。編集中は同名 `.~ne`（PID/HANDLE/日時/User/PC の INI）を隣に作る |

再圧縮は `neo_container.repack_neo(template, files, mgmt, entries)`。テンプレートは実 NEO を使う（`サンプル見積PDF/04011103.neo`）。生成後に `neo_header.apply()` で管理領域を今回の見積の値に書き直す。

### 1-1. 管理領域（raw[9:424]、2026-09-04 夕方に解読）

XOR 0xFF したビット列を左シフトして格納した 2 ブロック。`decodeA = ((raw[9:424] ^ FF) << 3)`、`decodeB = ((raw[330:424] ^ FF) << 7)`。

| decodeA オフセット | 内容 |
|---|---|
| 0-6 | 固定 `f7 ff fc c7 ff ff f8` |
| 74-113 | 協定工場名（Insurance.ConsultantFactory、cp932 40B、0 埋め） |
| 114-143 | 顧客名 Name1（30B） |
| 144-205 | 車名 CarNameByUser（62B） |
| 206-209 | 作成日 u16 年 + u8 月 + u8 日 |
| 210-249 | double×5 = 部品計 / 工賃計 / 塗装計（材料込） / 費用計（部品+工賃） / 合計（税込） |
| 274-281, 282-287, 288-291, 292-301 | 登録番号 陸運支局 / 分類番号 / かな / 一連番号 |

| decodeB オフセット | 内容 |
|---|---|
| 0-3 | 保存日 u16 年 + 月 + 日 |
| 4-6 | 保存時刻 時 / 分 / 秒 |
| 7-14 | ライセンス ID 8 文字（例 `G0141016`。04011103.neo は別ライセンス `G0141924`） |
| 15- | double×6 = -1.0、以降 0 埋め |

実 NEO 4 件で往復一致。生成 NEO はこれまでテンプレートの値（N-BOX／別顧客）を引き継いでいたため、v4 から書き直すようにした。
文字は Windows 互換 cp932（IBM 拡張漢字は FA-FC 行。Python 標準の cp932 は NEC 選定 ED/EE 行になるため `neo_container` に `cp932w` コーデックを登録して xml / AnSMB / AnSvMail / 管理領域すべてに使う。例: 德 = FA BA）。

## 2. テキスト系内部ファイル

| ファイル | 内容 | 生成時の扱い |
|---|---|---|
| `<name>.xml` | Shift_JIS `<AudaNeo2Data><Svef>…`。顧客名・登録番号・車名・型式指定・類別・カラー・所有者・作成日・車台番号・走行・初度登録・契約者・Total・CarNo 分割・和暦（Era 4=令和 3=平成） | 全タグ置換（`build_xml`） |
| AnCooperate.txt | `[NeoSave] Type=0 Note=` | そのまま |
| AnDBVersion.ini | AnSvIf=v3 / AnSvEm=v3 / AnSvIg=v1（DB スキーマ世代。`AxDBFlVerChk.dll` が変換） | そのまま |
| AnFlInfo | `NewCreate=作成日`、`AnVersion.ini=2.1.1.3`、`AnVer.db=車種データ版`（旧値は `AnVer.db_Back1`）、`AudaMain.exe=20230131` | `AnVer.db` を `COM/AnVer.DB` の値に更新 |
| AnNote.ini | `[Reserve] Flag`, `[Comment] Flag` | `[Reserve] Flag` = 保留行あり・`[Comment] Flag` = 明細コメントありで 1（§10-19）。`Note` は空にする |
| AnSMB.txt | 142B 固定長 × 明細行（§5）。**全行**（実 NEO は 1 行も欠けない: 04011141 は 267/267、工場 NEO 12051345 は 97/97。2026-09-08 の総当たりで「先頭 60 行」を訂正） | 全行を生成 |
| AnSvEm0001Ex.db | INI: `[PaintingPlan] Idx1.FormShow=1`, `[ADASWork] Idx1.*` | そのまま |
| AnSvImge.ini / AnSvIg0001.sld | 画像（Image / ImageAnnotation テーブル）。未使用時は空 | そのまま |
| AnSvMail.ini | `[Audaneo2] CustomerName=契約者 CarNo* TicketNo AcceptNo AccidentDate AgreedName=相手工場 CarName` | 全キー書換（テンプレートの個人情報が残るため必須） |

## 3. AnSvIf0001.sld（車両・顧客・設定、12 テーブル）

### Car（1 行、37 列）
| 列 | 値の出所 |
|---|---|
| PartsPriceDate | `<車種>01.DB` 先頭レコードの価格適応日（例 080701） |
| （車両特定の補助）品番からの逆引き | 型式指定・類別が読めない見積では、resolver が 01.DB のグレード一覧（年式・ボディ・駆動+エンジン・グレード）を候補に展開し、`AddataParts.infer_from_parts()` が見積の品番から 11.DB 変種行のグレード記号／年式群を集めてヒント（grade_codes +3、year_group +2）にする。コグニ生成 NEO 10 本で型式指定・類別を伏せた検証: グレード 3/10（J97・J95・S64）、年式群 3/3。車検証が揃えば従来どおり 10/10（2026-09-06、証拠パック外の実行結果） |
| WorkCodeUpdateDate | **新規作成時は COM.CAB 内 DATAUP.DB の値**。既存 NEO を開いて再保存するときはファイルの既存値をそのまま温存する（生成器 NEO を開いて保存した NONE_bk 202608 / SIENTA_w 202605 / CHR_ETO_exp 202609 にはその値が残り、本 PC で一から作った NEW1〜4 / FRAME_* は DATAUP の 201706）。（XOR 0xff の CSV `CarCode,YYYYMM` = 車種データ更新年月。J52 201706、W66 202011、J97 202201、D88 202312、D98 202303、W64 202303、J95 201403、U52 201605、D82 202108 = 他工場のコグニ生成 NEO 9 本と 9/9 一致。DATAUP.DB に無い S64 はコグニも ''）。生成器は `reference/DATAUP.DB` から引き、`vehicle.workcode_date` で上書き可（2026-09-06 確定。W/S ご案内 TIF の「作成年月」とは別の値で、W66 のご案内は 令和3年12月だが DATAUP は 202011） |
| MakerCode | `KATB030.DB`（CarCode → メーカー文字。J97=D ホンダ） |
| CarCode / YearCode | `KA06_ALL.DB`（型式＋車台番号）∩ `KA81.DB`（型式指定＋類別） |
| BodyCode / GradeCode / FVACode | `KA81.DB` |
| FVAName / FVANameByUser | `08.DB` |
| CarName / CarNameByUser | `01.DB` の半角車名レコード `{車名} {型式 グレード名} {排気量 右詰4}`。末尾の全角空白は状態依存（工場のコグニ生成 NEO と車名変更後の保存では付く、新規見積直後の NEW1 では付かない）。コグニはどちらも読む。生成器は全角空白付き |
| BodyImageCode | `07.DB` 末尾 2 桁 |
| LBaseCode / SBaseCode | '00' / = BodyCode |
| CarFormCode / FormCode1 / FormCode2 / FinishCode | **`25.DB`**（例 7252） |
| ColorCodeFlag / ColorCode / ColorName / ColorRGB1 | `26.DB` |
| ColorRGB2 / UColor* / LColor* | ツートン用。通常空 |
| TrimCodeFlag / TrimCode / TrimName / TrimRGB | トヨタのみ。通常 0/空。装備バリエーション変更でトリムコードを選ぶと 1 / '20'（候補は `<car>29.DB`、TrimName・TrimRGB は空のまま。TRIM_W66b.neo 2026-09-06 夜） |
| Extension | 0 |

### コグニ非収録車（輸入車等）= 汎用車種（コグニで「メーカーから検索」→ 汎用 を選んで保存した空見積 = 証拠パック `neo_VOLVO_gen_cogni_blank.json`）（ボルボ V40 実案件で確定、2026-09-04）
ADDATA の `Z10`（乗用車）/ `Z20`（１ＢＯＸ）/ `Z30`（トラック）が全メーカー共通の「汎用」車種（KATB020 `03?02010 汎用`、KATB030 → Z10/Z20/Z30。フォルクスワーゲン(J) には無い）。11.DB は価格なし・15.DB なし・20/25/26.DB なし、12.DB（535 行の汎用部品名・コード）と 17.DB（40 ブロック）はある。
コグニで「メーカーから検索 → 汎用 → 乗用車 → 車名・エンジン名・カラーコード手入力」で作った NEO と同形にする:
- Car: `MakerCode`（選んだメーカー。I=トヨタ）, `CarCode='Z10', YearCode='00', BodyCode='10', GradeCode='A', FVACode='T', BodyImageCode='03'（Z20 '10' / Z30 '21'）, LBaseCode='00', SBaseCode='00', Extension=1, ColorCodeFlag=1, ColorCode=手入力, CarNameByUser / FVANameByUser=手入力`。CarName / FVAName / ColorName / CarFormCode 等は空、PartsPriceDate 空
- CarSearch: `SearchMethod=1, ms_MakerCode/ms_MakerName, ms_CarFormGroupCode='02', ms_CarNameCode='010', ms_CarNameName='汎用', ms_CarCode='Z10', ms_ModelName='乗用車', ms_YearCode='00', ms_BodyCode='10', ms_FVACode='T', ms_GradeCode='A', ev_* 同値, ev_TwoColorCodeFlag=0, ev_ColorCodeByManual=1`。ps_*（車検証検索）は空
- xml: `MakerName / CarNameName=汎用 / ModelName=乗用車`
- 明細: 12.DB のコードが当たる部品はコード付き（Rﾊﾞﾝﾊﾟ 3810 等）、他は手入力行（PartsCode 空）。指数が無いので工賃は手入力（Time=-1, WageByManual '*'）。印刷前の車種情報確認画面は出ない
- 生成: estimate.json の `vehicle: {"generic": true, "car_code": "Z10", "maker_code": "I", "car_name", "engine", "color_code", ...}`、`items[].manual: true` で照合を止めて見積書の名称をそのまま使う

### CarSearch（1 行）
`SearchMethod=3`（車検証から検索）。`ps_CarMouldNo / ps_CarKindNo / ps_CarRegDate(YYYYMM00) / ps_CarRegEra / ps_CarRegEraYear(4桁) / ps_CarSerialNoHead / ps_CarSerialNoTail / ps_CarSerialNo / ps_YearSearchFlag（車台番号レンジ KA06 で年式が確定したとき 1、型式指定＋類別＋初度登録だけの検索は 0: C10_y01.neo。生成器 `1 if by_serial else 0`） / ps_YearName(05.DB の年式名)`、`ev_*` に確定値、`ev_TwoColorCodeFlag=1`、`ev_ColorCodeByManual` = 26.DB の一覧から選ぶと 0・一覧に無い手入力は 1（NEW1 は 0）、`ev_UColorCodeByManual/ev_LColorCodeByManual/ev_TrimCodeByManual` は一覧から選ぶと 0・手入力すると 1 の状態依存（NEW1 は 0、他工場 NEO 04011103 は ColorCodeByManual を含め 4 つとも 1。生成器は一覧選択と同じ 0）、`nm_PartsPriceDate / nm_CarNameByUser / nm_FVANameByUser`。`ms_*`（メーカーから検索）と `ws_*`（ワークシート）は空。

### CarEVA（28 行固定）/ CarSearchEVA（28 行固定）
装備バリエーション。`EVACode`（10.DB の 1 文字）と `EVAName`。CarSearchEVA は `EVariationName / EVariationOrder(01…)`。未使用行は空文字。

### Customer（1 行）
Name1/2/3, Owner='様', PostalNo, Prefecture, Municipality, AddressOther1/2, Phone, Fax, CarRegNoDepartment/Division/Business/Serial（登録番号 4 分割）, CarSerialNo, CarMouldNo, CarKindNo, UserName('同上'), OwnerName, TermDate/TermEra/TermEraYear（車検満了）, CarRegDate/Era/EraYear（初度登録）, Kilometer, AddressCode。

### Insurance（1 行）
PolicyNo, ContractorName, AgencyName, AccidentDate/Era/EraYear, PresenceDate（立会）, AgreedDate（協定）, RepairDays=-1, TimelyPrice*=-1, AdjusterName/Post, ConsultantName, ConsultantFactory（相手工場）。未入力日付は '00000000'。

### FileInfo / Setting / SelfInfo / ReportTitle / Statistics / Unspecified
- FileInfo: EstimatedDate/Era/EraYear、GarageIn/Out（'00000000'）、Note1-3
- Setting: `WageType=0, wb_PriceBase=レバーレート円/h, wb_Round=10, wi_Round=10, TaxKindFlag=0(消費税の表示方法 = 外税。1 は内税。§「消費税設定ダイアログ」。**明細に入れる金額はどちらでも税抜**), TaxRate=10, tx_CalculateFlag=1, tx_Unit=1, tx_ArrangeFlag=1`（NEW1 = 車両確定直後の保存だけ 86,100 で、これは作成時の入力ミス値。NEW2 以降は 8,610 に直してあるので NEW1 を Setting の根拠にしない）
- SelfInfo: 自社情報 7 行（テンプレートの SHOUCHIKU 情報をそのまま利用）
- ReportTitle: 帳票 15 種のタイトル。コグニが保存時に並べ替える（値は同じ）
- Statistics / Unspecified: 統計・予備。テンプレート値のまま

## 4. AnSvEm0001.sld（見積本体、25 テーブル）

### ERParts（明細、74 列）
| 列 | 値 |
|---|---|
| RecordNo / LineNo | RecordNo は登録順の通番（1..）、LineNo は表示・帳票順の独立フィールド（新規入力では RecordNo×10 だが、並べ替え・リサイクル置換・行挿入で振り直される: 他工場 NEO 04011103 は RecordNo 2 に LineNo 100。生成器は RecordNo×10） |
| PartsCode / PartsCodeSub | ADDATA ref（4 桁ゼロ埋め）/ -1 |
| DisposalCode / DisposalName / DisposalNameStandard | **AnDefine.ini [WorkSheet] の真値: 0 取替, 1 脱着, 2 修理, 3 脱着修理・脱着板金, 4 点検・調整・点検調整, 5 分解調整, 6 板金** |
| PartsName / PartsNameStandard | 11.DB（色別なら 83.DB / 13.DB。§10-2）の名称欄 20 文字から生成（§10-2: 標準 = 名称欄そのまま ' Fﾊﾞﾝﾊﾟﾋﾞ-ﾑ' / 'LFﾊﾞﾝﾊﾟｽﾍﾟ-ｻ'、表示 = L/R→左/右・F/R→Fr/Rr・空白は 2 スペース） |
| PartsNo / PartsNoStandard | 見積の品番（部品代のある行のみ）/ ADDATA 品番。装備バリエーション変更の「再検索」を通した行は PartsNo が 17 桁の右スペース詰め（`'67005-52E50      '`、TRIM_W66b.neo）。複数候補から選んだ工場 NEO には `'…-C0   *'` の形もある（§8 末尾の 13.DB 項、ADDATA 仕様 §11-9b）。生成器は詰めも '*' も付けない |
| PartsPriceOutTax/InTax/Tax | 部品代（税抜/税込/税）。無い行は -1 |
| PartsUnitPrice* | 数量>1 のときの単価、通常 -1 |
| PartsPriceStandard* | ADDATA 標準価格の**単価**（数量倍しない。コグニが再評価した NONE_dc/bp4/pnl の Rec8: 単価 155×10 個で PartsPrice 1550・PartsPriceStandard 155・ChangeTotal 155）。旧生成器が書いた 1550（数量倍）は再評価前の NONE_bk ではそのまま温存されている。生成器 2026-09-05 に単価へ修正 |
| PartsPriceByManual | 部品金額が ADDATA の標準価格と違うとき '*'。標準価格の無い行（手入力行・11.DB/13.DB に価格の無い品番・ディーラーオプション）に金額を入れた行も '*'（他工場 NEO 23 行: 6029 ｶﾞﾗｽｾﾂﾁﾔｸｻﾞｲ 等 std 0 → '*'。例外 12241720 の 9999 Frﾌﾛｱﾏｯﾄ std −1 で ''）。コグニ印刷の右端「*#」= 金額 '*' ＋ 指数 '#'（2026-09-07 オデッセイ） |
| Time / TimeStandard | 指数（時間）。工賃なし行は Time -1 / TimeStandard 0（コグニは標準の無い行に -1 ではなく 0 を書く: 他工場 NEO 613 行すべて 0、FRAME_p9 1442/1517。WageStandard* も同じ。生成器も 0 に統一 2026-09-06 夕） |
| WageOutTax/InTax/Tax, WageStandard* | 工賃 = 指数×レバーレート（Setting.wb_Round の単位で四捨五入。通常 10 円。**100 円設定の工場あり**: 工場 Jのコグニ印刷 2026-09-07 で 0.25h×11,000 = 2,800、2.60h → 28,600。生成器は estimate.wage_round で単位を切替）。手入力は WageByManual='*'（`#` は時間手入力）。`*` 行（工賃だけ手入力）は Time −1（印刷で指数欄空欄。REAL_12051345 3800）、ADDATA に標準があれば TimeStandard/WageStandard は残す（生成器の流儀。実 NEO の `*`＋標準あり例は未取得）|
| PartsCount | 数量（部品代なし行は -1） |
| ChangeTotal* | 「取替した場合の合計」= 標準部品代（単価）+ **その部品を取替(K)したときの標準工賃（連動加算前）**。行の修理方法に関わらず同じ式（NONE_dc: 点検調整 0450 = 64,300 + 0.4h、分解調整 6500 = 部品 + 取替工賃、脱着板金 0020 = 2,750 + 0.1h（'C' 行は 0010 の連動行に置かれている）、SIENTA_w 板金 4802 = 0 + 6.6h、04011103 板金 '#' 1500 = 17,700 + 4.4h、NEW2 0010 = 50,700 + 1.3h（連動 0.1h は含まない））。取替の標準が無い部品は標準部品代のみ、両方無ければ -1。生成器は標準化パスの後に全行を取替標準で計算（2026-09-06 朝の N-ONE 実機では 38/42。残り 4 行は骨格の組合せ（ADDATA 仕様 §11-7c）で、同日夜の実装後は他工場 NEO 10 本の骨格行 71/71・FRAME_p7 の 7 行すべて一致） |
| PartsFileTime | '0' |
| WorkCode | 15.DB の section（'B','C2','D2','I7O7'…）10 文字左詰 |
| ConstructGroup | 取替行 = 11.DB K 行の [68:70]（'P6'/'G4'/'R0'/'MM'）、脱着(1) = D 行・脱着板金(3) = DS 行（無ければ D 行）の [68:70]（通常 '  '。D88 8200 'QQ'、W66 6350 'N8'、S64 0402/0452 'A1'/'A3' のように値を持つ部品もある）、板金(6) = 11.DB の S 行の [68:70]（通常 '  '。W66 4802 'Z0'、5320 'N1'、D98 1502 'G2' = 他工場 NEO の板金 28 行すべて S 行と一致。SIENTA_w 4802 'Z0' も同じ規則で、画面履歴ではない）、修理(2)・点検調整(4)・標準ありの分解調整(5) = '  '（NONE_dc 点検調整、他工場 修理 13 行））、標準の無い実額入力（WageByManual '#'）行 = ''（空文字。NULL ではない: 全 NEO で ConstructGroup が NULL の行は 0 件、NEW2/NEW4 の 0030/0050/0140/0155 は typeof text の ''。修理・点検調整・分解調整と、標準の無い脱着板金。明細画面から直接入力した '#' 行（PartsType 1）の点検調整には ' '（半角 1 文字）もある: 12151249 7600、12241720 6305。標準が引ける '#' 行は 11.DB の値を保つ = §7）。生成器由来の '' や '  ' はコグニが温存する（NONE_dc の点検調整 '  '）。生成器はコグニと同じ ''（空文字）を書く（2026-09-07 監査 39 で NULL → '' に訂正。§10-2） |
| BlockCode | **12.DB [5:8] の部位ブロック（A01/H01…）** |
| WageFileTime | ADDATA（15.DB）から引いた標準指数の文字列（'0.5','2.3'）。全 NEO 調査（他工場 10 本＋本 PC 保存版、2026-09-06）: 標準行（WageByManual ''）と '$' 行は Time と同値（432/432）、'#' 行は標準があれば TimeStandard（04011103 2300 = '0.5'）・無ければ '' または '0'、'*' 行と工賃なし行は ''（コグニ自身が作った '*' 行 61/61。生成器 NEO を開いて再保存した版（CHR_ETO_exp の 0453 '0.3'、8370 '3.1'）はファイルの値がそのまま温存される）、板金ランク '@' 行はダイアログ直後 ''・再計算後 '0'（ROUND_8750 の 1.9h でも '0'） |
| PartsPriceFlag | 部品代なし＆標準価格あり = 1 |
| DamageArea / DamageRank / DamageRankBtn1-3 | 板金の損傷面積・ランク（未使用は ''/0） |
| SATime1-5 (+ByManual/Flag) | 特殊作業時間、-1 |
| BlockListFlag / RecycleFlag / RCRecordNo / ReserveFlag / ReserveRecordNo / CommentFlag / Comment1-3 / RWLinkFlag | 既定値 0 / ''（保留 ReserveFlag 1・コメント CommentFlag 1・リサイクル RecycleFlag 1 は §4 後段、RWLinkFlag 1 はコグニが再検索した行に付く: SIENTA_w） |

### DamageBlock / DamageParts / DamageBlockPlan
- DamageBlock: 部位画面で選んだ部位（フロント部 / リヤ部 / ALL）に属する 17.DB ブロック（from<4096 の実エントリ。フロント部 31 行、ALL 41 行、W66 ALL 44 行）。生成器は ALL 相当（全ブロック）を書く
- DamageParts: 明細行ごとに `(RecordNo, ERPartsRecordNo, BlockCode, PartsType)`。**確定規則は §10-19**（2026-09-09 の実機 5 車種 + CX1〜CXF）。要点は次の 3 つ。(1) `PartsType 1` = 12.DB の可能作業（[52:55]）に 取替 K も 脱着 D も無い品目（修理のみ `S` / オーバーホールのみ `OH`）・リサイクル置換行・部品コードの無い手入力行。保留（ReserveFlag=1）は 0。(2) `BlockCode` は ERParts.BlockCode の流用ではなく 12.DB の全 W/S 版から引く。基本版(0)に無く複数の版に載る部品は空で、ERParts.BlockCode が残っていても埋め戻さない。(3) ERParts と DamageParts は別々に決まる（コグニは保存時に DamageParts を作り直すが ERParts.BlockCode は書き換えない）。
  過去の観測例もこの規則で説明できる: 明細で直接入れた SIENTA_w 3800（Rライセンスプレート(修理)）が `BlockCode ''`・`PartsType 1` だったのは 3800 が `S` のみの品目だから。NEW3 の 0400 のように部品コードを直接入力しただけの行は、K/D のある部品なら `PartsType 0`（旧版で「直接入力なら 1」と書いていたのは誤り）。明細のボタンで 取替→板金 に変えただけの行は 0 のまま（SIENTA_w 4802）。コグニが再評価した数量部品行（NONE_dc Rec8 0170 クリップ 10 個）は ERParts.BlockCode が ''（空文字、typeof text）で DamageParts も '' ・`PartsType 0`。生成器も同形
- DamageBlockPlan: `DamageCode` = 選んだ損傷部位コードの連結（フロント部 '01020304' FrontArea=1 RearArea=0 AllArea=0、リヤ部 '05060708' FrontArea=0 RearArea=1 AllArea=0（neo_REAL_12051345）、ALL '0102030405060708' と 3 フラグ 1）。KATB120 が DamageCode → BlockCode

### Painting*（塗装）
- PaintingPlan: `InputType=1(指数)`, `Paint`（1 速乾/3 ２Ｋ/4 水性。4 のときは CHM の水性ページで塗り数値を引く）, `Coat`（66.DB 先頭桁 1-4 / CoatName）, `HFPainting`（0 しない/1 フッ素/2 耐スリ傷）, `MaterialRateType=1, MaterialRate`（塗装タブを開くまでは 0 = NEW1。塗装条件で塗膜を選ぶと既定値が入る。既定値は AnUsrTblPnt.sld（§5: このPC 2K×2コートパール 15%、NEW3/NEW4/SIENTA_w = 15）。塗装条件で塗膜を選び直すとその塗膜の既定値に置き換わる。保存値は最後に設定された割合で Coat と独立: 生成器が見積の材料代から書いた 26（SIENTA_q）は塗膜を選び直した SIENTA_w で 15 に戻った）。`CalculateLevel_Panel/Base/Bumper` は塗装画面で計算が走ると 1（生成器も 1）。手で「パネル追加」した行は `AddedFrom 1, WageByManual '*', SortNo = 追加リストの順位（2602 は 11）`、連動で自動生成された行は `AddedFrom 0, WageByManual ''`。SortNo は内部の操作カウンタで自動生成行でも 1 とは限らない（NONE_pnl 2300 = 2、04011103 2602 = 6。生成器は 1 を書き、コグニは読込時に再採番）, Booth*（COM/BOOTH.DB。無しは -1・BoothFlag 0）, Base*（加算基礎数値 = COM/T_KEI_3.DB。BaseWageByManual は工場のコグニ生成 NEO（04011103）では ''、コグニで パネル追加 等の操作をした後の保存版（NEW4/NONE_pnl/SIENTA_w）では '*'。BoothWageByManual（列は存在する）は全保存版で ''。BoothByManual/BaseByManual は数値列で 0（NEW1/NEW3）。生成器は標準工賃と同じなら ''、見積の値が標準と違えば '*' を書く（コグニはどちらも受け入れる）（どちらもコグニは受け入れる））, CalculateLevel_Panel/Base=1。CalculateLevel_Bumper は新規見積の流れ（NEW1〜4、NONE_*）では 1 だが、生成器 NEO を開いてパネルだけ計算した SIENTA_w では 0 のまま（バンパタブを計算した時点で 1 になる状態依存の値。生成器は 1）。工場見積を一括計上する場合は **Booth/Base の時間・工賃を -1** にする。**`CalculateLevel` は塗装明細の有無によらず `Panel/Base/Bumper=1`、`Booth/Frame/Etcetera=0`**（コグニ保存版・工場 NEO 57 本すべて。旧記述『一括なら CalculateLevel を 0』は誤り。2026-09-12 訂正）
- PaintingPanel（パネル追加と同形。ADDATA_REVERSE_LOOKUP_SPEC §11）: `PartsCode`, `DisposalCode 0 取替 / 2 修理 / 6 板金（DisposalName は 6 でも '修理'）`（明細に 20.DB パネルの取替(0)/修理(2)/板金(6) 行があると、コグニは塗装タブを開いて再計算した時点で、その部品（20.DB にあるものだけ。クォータパネルの工賃行 4802 も 20.DB 収録で、他工場 NEO 12051345 では PaintingPanel（板金 6・AddedFrom 0・Manual 0）に出る。生成器 NEO を開いて操作した SIENTA_w では 4801 だけ自動連動し 4802 は出なかった = 操作状態依存、条件未同定）を PaintingPanel にも `DisposalCode 0/2/6（修理(2) は 12081431 0600・11261526 4800 等 12 件）, DisposalName '取替'/'修理', AddedFrom 0, Manual 0, WageByManual ''` で自動生成する（工場のコグニ生成 NEO 04011103 の 0600/0800/1000 取替・2300/3100/2602 板金、NONE_pnl の 2300。板金ダイアログ直後の NONE_bk にはまだ無い）。「パネル追加」で手で足したパネルは 0/2 で `AddedFrom 1, WageByManual '*'`。生成器は明細に**同じ部品コードかつ同じ修理方法**の行があれば自動生成形、無ければパネル追加形で書く。**修理方法まで見るのは必須**: 実機 NEO の連動パネル（本数・行数は HANDOFF §5-1）は、明細に同じ部品コードかつ同じ DisposalCode の行が必ずある（食い違い 0））, `DisposalName`, `PanelName`（20.DB 20B）, `PrepareArea`（取替 -1。修理 1/1・1/2 は生成器では floor(面積×0.35) / floor(面積×0.175) の近似。他工場 NEO には 68→12 のように合わない行があり、コグニの式は未確定 = ADDATA 仕様 §11-3）, `PanelArea`（20.DB）, `PaintingArea -1/1/2/3`, `PaintingAreaName ''/'1/1'/'1/2'/'1/3'`, `Time`, `TimeStandardNew/1/2/3`（CHM 塗り数値（PaintingPanel 1 枚なら単体塗、2 枚以上は複数塗）＋高機能加算）, `TimeStandardHF`, `Wage*`（四捨五入 10 円）, `WageStandard*`, `WageByManual`（明細の取替/板金行から自動生成された行は ''、「パネル追加」した行は '*'）, `Material* -1`, `PanelDivision/PanelTypeDivision/PanelCode/ButtonNo`（20.DB）, `SortNo`（内部カウンタ）, `AddedFrom`（自動生成 0 / パネル追加 1）, `Manual 0`。行が無効（存在しない PartsCode 等）だとコグニは表を空として再計算する。**`AddedFrom=0`（連動）なのに明細にその部品コード・修理方法が無い行は、コグニが塗装ページを開いた時点で連動行を明細から作り直すので消える**（2026-09-11 実機 W90 ハイエース TRH229・ボディ 20: 明細の部品コードは 4800、塗装パネルは枝番の 4801。4801 を `AddedFrom=0` にすると行が消えて枚数 1 で再計算され、塗装計が 176,560 → 142,610 に落ちた。`AddedFrom=1`・工賃印 `*` なら残る）。生成器はこの違反を `ValueError` で止める。**指数を標準値以外にする（工場見積の独自指数）ときは `Manual=1, WageByManual='#'`**（N-ONE 案件: コグニで指数セルに 1.2 を手入力して保存した版と一致。`Manual 0`/`'*'` のままだとコグニは塗装タブ表示時に標準 1.3 へ戻す）。**`AddedFrom` は手入力かどうかとは無関係**で、明細に同じ部品コード・同じ修理方法の行があるかどうかだけで決まる（下の実測を見よ。N-ONE のそのパネルが `AddedFrom=0` だったのは明細に取替行があったからで、手入力だからではない。2026-09-11 に訂正）
- PaintingPanel の **手入力の塗装行（DisposalCode 9）**: 外板パネル画面の「行追加」で名称・指数を手で入れた行（実案件 NEO 354 本・631 行。2026-09-13）。`PartsCode ''`, `DisposalName ''`（修理方法を選んだ行は '修理'/'取替'）, `PanelName` 入力のまま（詰めない）, `PanelArea/PrepareArea/PaintingArea -1`, `PaintingAreaName ''`, `TimeStandard*/WageStandard*/Material* -1`, `PanelDivision/PanelTypeDivision/PanelCode/ButtonNo -1`, `SortNo 15`, `AddedFrom 1`, `Provisional 0`。指数あり: `Time` = 指数・工賃 = r10(指数×単価)・`WageByManual '#'`・`Manual 1`／工賃だけ: `Time -1`・`'*'`・`Manual 2`。部品コードのあるパネルの後ろに入力順で並ぶ（LineNo = RecordNo-1）。`TimeTotalPanel`・`WageTotalPanel`・材料代の対象に入り、加算基礎の枚数（単体塗/複数塗の判定も）には数えない（60/60・16/16）。手入力の塗装行だけを足しても `BaseWageByManual` は '' のまま（241/245）。部品コードのあるパネルが無く手入力の塗装行だけなら `BaseTimeStandard -1`（加算基礎を入れれば手入力 '#'）、`BumperBase* -1`。生成器は `paint.panels[].manual`
- PaintingBumper（fb_/rb_）: `Disposal 0 なし/1 新品/2 変形修正/3 外傷修正（23.DB の無い車種: 小/大の区別なし、FBANPA_J69.neo）/4 外傷修正小/5 外傷修正大`（N-ONE 実機 2026-09-05、NONE_bp/bp2/bp4.neo）, `Name` 同名, `Form -1 / 0 大型 / 1 標準`（`FormName` '大型'/'標準'。23.DB の無い車種（COM/FBANPA.DB を使う 586 車種（evidence/car_file_counts.json））でだけ形状を選べ、J69 パートナーの保存版 FBANPA_J69.neo で 0/1 を確認。23.DB 車種は -1）, `Color 0 一色/1 黒ライン/2 二色`, `Draft 0 無し/1 有り`（絞模様。新品では選択不可）, `DraftName ''（新品）/'無し'/'有り'`, `Time`（`<car>23.DB` の 8 列 [新品一色, 新品二色, 外傷大一色, 外傷大二色, 外傷小一色, 外傷小二色, 変形一色, 変形二色]。絞模様有りは **+0.4**（修理 3 方式・ソリッド/パールとも一定）。実機 J52 2コートパール F: 新品 1.3/1.8、外傷大 2.4/2.9、外傷小 2.2/2.7、変形 3.1/3.6、絞有り 各 +0.4）, `Wage*`, `WageByManual ''`（新規見積でバンパを初めて選んだ直後の保存では `'$'`、バンパ加算も `BumperBaseWageByManual='$'`。値は標準と同じ。生成器は標準と同じなら ''、違えば '*'）。「別調色」ボタン = バンパ設定ダイアログ（バンパ別調色チェック＋バンパ用塗膜）。PaintingFrame（er_/fp_/cp_/rp_ 骨格）, PaintingEtcetera（ARWax 枚数×0.1h 等）, PaintingLinkParts（パネル追加では 0 行）, PaintingOther（追加項目タブ 内板調色…。工場塗装費一括は `Name='塗装費用(工場見積)', WageByManual='*'`）
- PaintingTotal: `TimeTotalPanel/Bumper/Frame/Etcetera/Other`、`TimeTotal = パネル + バンパ + 骨格 + 付加 + 加算基礎`（**ブース指数は含まない**。N-ONE 保存版 1.2+2.8+1.9=5.9。バンパ単独塗装のときは加算基礎の代わりに `PaintingPlan.BumperBase*`（BAN.DB）が `TimeTotalBumper` に内包される: NEW3 fb_Time 1.3 + BumperBaseTime 0.5 = TimeTotalBumper 1.8 = TimeTotal）、`WageTotal* = パネル + バンパ + 骨格 + 付加 + ブース + 加算基礎`（Other は追加項目のみ。**加算基礎は Other ではない**）、`MaterialTotal* = round10(WageTotal×割合)（10 円単位で四捨五入）`（手入力なら MaterialTotalbyManual '*'）、`Total = WageTotal + Other + Material`（Other = 追加項目タブの工賃。コグニが計算した WageTotal には入らない: SIENTA_w 52,800 + 191,360 + 7,920 = 252,080）。**例外**: 生成器の一括塗装費（非コグニ書式の見積の「塗装 ○○円」を PaintingOther 行 0 '塗装費用(工場見積)' に入れる形）は WageTotalOther と WageTotal の両方に同額を書き Total には一度だけ計上する（SIENTA_adas: 191,360/191,360/191,360）。コグニはこの形を読み・印刷し、塗装画面で再計算すると WageTotal がパネル分だけに正規化される

### 塗装条件の派生（N-ONE 実機 2026-09-04、NONE_hf/f/lc/2t.neo）
- 高機能塗装 フッ素(1)/耐スリ傷(2)（証拠: 工場のコグニ生成 NEO 04011103（証拠パック外）と N-ONE 案件の HF 保存版。証拠パックでは neo_CHR_ETO_exp（HFPainting 2 耐スリ傷、TimeStandardHF あり）が保存例で、他の NEO は HFPainting 0）: PaintingPanel `TimeStandardHF` にパネル別加算（面積 33 → 0.6。floor10(0.3+0.01×面積) と一致）、`TimeStandardNew/1/2/3` は加算込みの値に置き換わり `Time` も標準へ戻る（手入力 `#` は失われる）。加算基礎 T_KEI_3 は HF 列（2.8 → 3.4）、**ブース加算は 0**（BoothTime 0。BoothFlag はコグニ実機で HF を選んだ直後の保存版（NONE_bp4 系）は 1 のまま、生成器由来 NEO を開いて保存した CHR_ETO_exp は 0 が温存される = 状態依存。生成器は hf があれば BOOTH.DB の値（HF 時 0）でフラグを立てる）
- 低隠蔽性塗色: PaintingPlan は変化せず、PaintingEtcetera `LCColorFlag=1, LCColorOtherChange=1, LCColorTime=0.5`（1 パネル。COM/fukaetc.DB 6 行目、詳細は「付加塗装・追加項目」）, `LCColorWage*`。TimeTotalEtcetera/WageTotalEtcetera に加算
- 2 トーン（下部）: PaintingPlan `TwoToneFlag=1, TwoToneCoat=1-4, TwoToneCoatName, TwoToneMaterialRate`（AnUsrTblPnt.sld TwoToneMaterialRate）。パネル指数は 2 トーン対象パネルを選ぶまで変わらない
- **材料代割合の既定値** = コグニ環境 DB `AudaData/AnUsrTblPnt.sld` の `MaterialRate(Paint=PaintingPlan.Paint−1, Coat=Coat−1, HFPainting)`/100（このPC: ２Ｋ×２コートパール しない 15% / フッ素 25% / 耐スリ傷 18%。生成器 `default_material_rate()`、reference/ にコピー）

### Expense（36 行固定）
LineNo 1-8 は固定名（文字書き/内張り/配線配管/ショートパーツ/レッカー1/レッカー2/写真代他/その他控除、NameFix=1）、9-36 は任意名（NameFix=0）。`PartsEnabled/PartsPrice*` と `WageEnabled/Wage*`、`OutTaxFlag`（非課税）。Name は 20 バイト。

### Total（1 行）
`ms_PartsTotal*`（Σ部品、税は行ごとの PartsPriceTax の合計。行税は四捨五入、数量行は 四捨五入(単価×0.1)×数量 §6）, `ms_WageTotal*`, `ms_RecyclePartsTotal*`, `pn_Total*`（塗装計）, `pn_MaterialTotal*`, `nk_Total*`（内板）, `hy_PartsNoTax/WageNoTax/PartsTax/WageTax Total*`（費用）, `hy_Wrecker1/2*`, `pt_Extra*`（部品値引 Flag=1 Unit=1 ArrangeFlag=1 IncludeRecycle=1）, `wg_Extra*`（工賃値引）, `tx_TotalOutTax/InTax` = SubTotal×10% を四捨五入（751,595 → 75,160、598,965 → 59,897。§6）

### リサイクル部品（RCParts / RCLinkParts、コグニ「リサイクル部品登録」で確定。根拠 = コグニ実機 2026-09-04 の保存版 NEO_check/案件 C01_CHR/CHR_ETO_exp.neo・CHR_ETO_exp2.neo（RCParts 1 行）。証拠パック neo_CHR_ETO_exp.json に収録）
- 元の ERParts 行は削除して末尾に追加し直す（他行の RecordNo は据え置き、LineNo だけ 10 刻みで振り直し、追加行は RecordNo=最大+1）
- 追加行: `PartsCodeSub=1, PartsName=リサイクル部品名, PartsNo='リサイクル部品', PartsPrice*=リサイクル価格, PartsPriceByManual='R', PartsCount=1, 標準名/標準品番/標準価格/標準工賃はクリア(-1/0/''), Time/Wage=-1（純正部品の指数・工賃は削除される）, ChangeTotal=リサイクル価格 + 元部品の取替標準工賃（連動加算前）（CHR_ETO_exp 0010: 10,000 + 2.4h×8,750 = 31,000。元行の 2.5h はバンパビーム連動 0.1h 込みなので使わない）, RecycleFlag=1, RCRecordNo=n`
- RCParts: `RecordNo, PartsName(20B), StockingPrice*（仕入れ値）, PartsPrice*（=仕入れ値×係数）, PartsPriceCoefficient=1, Comment, ContactFlag=1`
- RCLinkParts: 元の ERParts 行の全列コピー + `PartsRecordNo=n, ERPartsRecordNo=元 RecordNo, LineNo=元 LineNo`（コメントも元行側に残る。証拠パック neo_CHR_ETO_exp.json の RCLinkParts 1 行）
- Total: `ms_RecyclePartsTotal*=Σリサイクル価格`（ms_PartsTotal にも含まれる）、DamageParts の該当行は `PartsType=1`、AnSMB は 12 桁目 '1'・101 桁目 '1'（行数の上限は無く、全明細行が入る）

### 内板骨格修正（内骨画面 → FramePlan / Frame、Total.nk_*）
- FramePlan: `FrameFlag=1, PartsCode='1371', Time=基本修正作業（COM/N_KIHON.DB 車形別 3.5h）, Wage*`
- Frame: 部位区分ごと `LineNo=N_KEI の No−1, PartsCode(1372…), PartsName, DamageRank(2=A, 3=B, 4=C), Time（COM/N_KEI.DB の A/B/C 列）, Wage*`。工賃は四捨五入 10 円
- Total: `nk_Total* = 基本 + Σ部位`。塗装側の内板骨格（下記）とは別

### 内板骨格塗装（塗装画面 内板骨格タブ → PaintingFrame）
`er_Disposal 1=ラジエータサポート両側新品または修正 / 2=…+Fフェンダエプロン片側 / 3=…両側`、`fp_/cp_Disposal 1=片側新品 2=両側新品`、`rp_Disposal 1=1台小修正 2=1台大修正`。指数は COM/NAIKOKUA.DB（車形, No 01-09）。PaintingTotal の Frame 系に加算

### 付加塗装・追加項目（塗装画面 付加塗装タブ。N-ONE 実機 2026-09-05、NONE_bp*.neo で全項目確定）
- PaintingEtcetera（項目ごとに `<項目>=枚数, <項目>Time/TimeStandard, Wage*/WageStandard*`。指数は COM/fukaetc.DB（XOR 0xff CSV、8 行）と 2TONE.DB）:
  - `DSBlack` ドアサッシュ黒塗り: 枚数 1〜4 → 0.4 / 0.6 / 0.7 / 0.9（fukaetc 2 行目 `040,060,070,090`。溶剤/2K のみ）。**水性（Paint 4）は指数なし**: DSBlack に枚数は入るが DSBlackTime/DSBlackTimeStandard/DSBlackWage* は -1（FRAME_p11 と 2K 版 FRAME_p10 の比較。ADDATA 仕様 §11-8、生成器 paint_c == 4 の分岐）
  - `BStripe` ボデーストライプ（塗装タイプ）: 枚数 1〜4 → 0.5 / 0.5 / 0.6 / 0.7（3 行目 `050,050,060,070`）
  - `BSealing` ボデーシーリング（外板パネル用）: m 数 × 0.1（4 行目 `010,015`）。`ARWax` 防錆ワックス: 枚数 × 0.1（5 行目）
  - `LCColorFlag=1, LCColorRoof 0 なし/1 取替/2 修理, LCColorOtherChange n（0 は -1）, LCColorOtherRepair n, LCColorTime` 低隠蔽性塗色（塗装条件のチェックで有効）: ルーフ取替 0.5 / 修理 0.3、ルーフ以外 取替 1 枚目 0.5・2 枚目以降 +0.2、修理 1 枚 0.2（実機: ルーフ取替+取替2+修理2 = 1.6、+取替1 = 1.0、なし+取替1 = 0.5、修理+取替1 = 0.8。6 行目 `050,020,030,020,030`）。コグニは枚数を減らす操作で `LCColorByManual='*'` を付けることがある（値は標準）
  - `TwoCSolidFlag=1, TwoCSolidRoof 0/1, TwoCSolidOther n, TwoCSolidTime` 2コートソリッド（塗膜ソリッド＋塗装条件のチェック）: ルーフ 0.4 ＋ ルーフ以外 0.1/枚（実機 ルーフのみ 0.4、+1 枚 0.5、+7 枚 1.1。1 行目 `040,010`）
  - `LCColorByManual / TwoCSolidByManual / DSBlackByManual …`（`…WageByManual` とは別列）: 付加塗装タブで枚数・時間を手で直した項目に '*'（NONE_bp2〜bp4 の LCColorByManual、FRAME_p10/p11 の LCColor/TwoCSolid）。fukaetc から自動計算のままなら ''（NONE_bp、NONE_lc、工場 NEO 12121650 の TwoCSolid）。`…WageByManual` は工賃側の手入力印で、上の例ではすべて ''。生成器は両方とも ''（自動計算値を書く）
  - `TwoTone n, TwoToneTime` 2トーン加算（塗装条件の 2トーン(下部) チェック＋下部塗膜）: COM/2TONE.DB `[上部塗膜, 下部塗膜, 枚数1..5]`（実機 2コートパール/ソリッド 1〜3 枚 0.9・4〜5 枚 1.0、ソリッド/ソリッド 4 枚 1.2）。水性は W2TONE.DB。PaintingPlan `TwoToneFlag=1, TwoToneCoat, TwoToneCoatName, TwoToneMaterialRate`（既定 12 = AnUsrTblPnt.sld）
  - PaintingTotal: `TimeTotalEtcetera` = 上記の合計、`TimeTotalBumper` = バンパ、`TimeTotal` にどちらも含む（実機 NONE_pnl 13.4 → NONE_bp4 20.2 = +3.1 バンパ +3.7 付加）
- PaintingOther（追加項目タブを開くまで 0 行、開くと 10 行の固定名が入る: 内板調色/水性加算/ﾋﾝｼﾞ塗装/ﾁｯﾋﾟﾝｸﾞ塗装/ﾎｲﾙﾊｳｽ/ﾎｰｽﾒﾝﾄ/ｲﾝﾅｰﾊﾟﾈﾙ/ﾌﾛｱ/ｱﾝﾀﾞｰｺｰﾄ/防錆処理。未入力行は `Time=-1, Wage*=-1, WageByManual=''`、追加項目タブから入力した行は `Time, Wage*, WageByManual='#'`。生成器はテンプレートの 10 行を常に書く）。追加項目の工賃は **PaintingTotal の Other に入り WageTotal には入らない**（Total = WageTotal + Other + Material。CHR_ETO_exp）。例外: 生成器が工場見積の塗装費を一括計上する `'塗装費用(工場見積)'` 行（`WageByManual='*'`）は WageTotalOther と WageTotal の両方に同額を入れ Total は二重にしない（生成器の流儀。コグニで読込・印刷確認済み）

### 点検調整(4)・分解調整(5)（N-ONE 実機 2026-09-05、NONE_dc.neo）
- 点検調整(4) は標準指数を持たない（11.DB の 'C' 行は点検調整用で区分レターが無い: W66 2700/3500、J52 0450）。分解調整(5) は 11.DB に **'OH'（オーバーホール）行**がある部品だけ標準指数があり（J97 7600 'I7O7' 3.0h、D88 7900 'D6J6' 3.2h = 04011103/04011141。生成器 DISP_LETTER[5] = 'OH'、監査 42 で 'C' から訂正）、無い部品（J52 6500/6600）は同様: `Time=-1, TimeStandard=0, WageOutTax=-1, WageStandardOutTax=0, WageFileTime='', WorkCode=''`。ConstructGroup は 点検調整で標準扱い（WageByManual ''）の行が '  '（NONE_dc 0450）、実額入力 '#' の行（NEW2 0140/0155）と標準の無い分解調整 6500/6600（NONE_dc）は ''（空文字。§4 表の '#' 規則と同じ）。W/S の実額入力で時間を入れると `Time=時間, WageByManual='#'`（NEW2 の 調整 0.2 / 点検 0.1）、工賃だけなら `'*'`
- 分解調整(5) は部品価格が消える（`PartsNo='', PartsPriceOutTax=-1, PartsCount=-1, PartsPriceFlag=1`、標準品番・標準価格は残る）。点検調整(4) は脱着(1) から変えると左右接頭辞の無い基準 ref を引き直す（`PartsName '右 ﾍﾂﾄﾞﾗｲﾄ' → '    ﾍﾂﾄﾞﾗｲﾄ'`）
- 連動加算を**受ける**行は取替(0)/脱着(1) の行だけ（点検調整(4)/分解調整(5) は受けない）。連動の**相手**（存在すると加算を発生させる行）は取替(0)/脱着(1)/脱着修理・脱着板金(3) のいずれでもよい（NEW2: 0020 脱着板金があると 0010 の 1.3 → 1.4）。例: 0450 脱着 → 点検調整 に変えると 0402 の標準指数が 0.5 → 0.4 に戻る
- DamageParts.PartsType が 0 → 1 に変わる（点検調整）。AnSMB.txt 13 桁目 = 4 / 5

### 骨格の組合せ作業（N-ONE 実機 2026-09-04〜05、NONE_fr1/fr2/fr3.neo）
- 12.DB [58] = Gothic（'*' = 塗装対象）、[59:61] = Construct = [G フラグ][構成レベル]（1 = COMP、2 = 構成部品。J52 1400 'G1'、1410 'G2' = evidence/J52_12DB_A25_raw.txt。ADDATA 仕様 §9-2）。構成部品を複数取替すると **15.DB の行を置く host 部品（例 1410、1420）の行に集約され、host でも sub でもない部品の行は `Time` 空欄**（TimeStandard 0。FRAME_p7: 1410 9.2h と 1420 5.1h の 2 行に集約、1430/1434/1500/1511/1600 は空欄。ADDATA 仕様 §11-7c 規則 2）。{1434,1410,1430} = 1.1 'B0C0'（CHM B131）、+1500 = 5.6（B0+C0+I0+J0 = CHM B161 左側。unit_frame CASES の [1410,1500] = 5.6）。1510 を加えた組合せは保存版が無く未検証（2026-09-04 の画面観察のみ）
- 合算規則はコグニ実機の画面読取 23 通りと保存版 FRAME_p7/p8/p9 で同定し、生成器 `_frame_combination()` が計算する（ADDATA 仕様 §11-7c、2026-09-06）。見積書に印字された合算指数を `#` で入れる旧方式（index_policy manual）も引き続き有効

### 汎用車種（Z10/Z20/Z30）の塗装（ボルボ V40 実機 2026-09-05）
- 塗装タブは「この車種には塗装指数がありません。全て手入力扱いとなります。」と出て 塗装条件 のみ（外板パネル/バンパ/内板骨格/付加塗装タブ無し、材料代割合 0%）。塗装は追加項目（PaintingOther）の手入力か明細行で計上する（実案件は `塗装費用(工場見積)` 1 行）
- 明細は部品コード入力で 12.DB 相当の名称（0010 → Frバンパ）が出るが品番 '-'・価格 0・指数無し

### 板金ランク入力（明細の 板金(6) 行でコグニが出すダイアログ。N-ONE 実機 2026-09-04、NONE_bk.neo）
- ダイアログ: 損傷面積 1〜40 dm²、損傷難易度 3 問（軽度な損傷／ラインや周辺部に及ばない損傷／損傷の裏側から作業可能）YES/NO、損傷ランク A/B/C、板金付加作業（COM/BAN_FUKA.DB: 部品コード別、例 2300 トリム.サービスホールカバー脱着 0.30、時間 0 の '*' 付きは手加算の案内）
- ランク: YES 3 つ → A、YES 0 → C、それ以外 → B。指数 = COM/BANKIN.DB[面積][ランク]（10 dm²: A 1.3 / B 1.9 / C 2.5）＋選択した付加作業時間
- ERParts: `DamageArea='10'`（文字列）, `DamageRank='B'`, `DamageRankBtn1..3 = 1 YES / 2 NO`（既定は 1/1/1 = A）, `SATime1Flag=1`（付加作業あり）, `Time=2.2`, `TimeStandard=0`, `WageStandardOutTax=0`, **`WageByManual='@'`**, `PartsNo='10d㎡ B 付加 0.30'`（表示用文字列。ダイアログ直後の NONE_bk の状態。工賃単価変更で再計算した ROUND_8750 では同じ '@' 行が `Time=1.9, TimeStandard=1.9, WageStandard=16,630, PartsNo='10d㎡ B 付加 0.00'` = 再計算後はランク指数が標準欄にも入る）, `PartsNameStandard` は 20.DB のパネル名（`LFﾄﾞｱﾊﾟﾈﾙ`）, `OrderFlag='0'`。PaintingLinkParts に板金行が追加される。生成器: `items[].bankin={area, yes:[1,0,0], fuka:true|[作業名]}`
- WageByManual の記号: `''` 標準指数、`*` 工賃を手入力（標準指数があればそのまま残るが、標準が無い行では TimeStandard 0 / WageStandard 0: neo_REAL_12051345 の 3800）、`#` 指数を手入力（標準指数が引ける行では TimeStandard / WageStandard に標準値が残る: neo_REAL_04011103 の Time 0.7 / TimeStandard 0.5 / WageStandard 3,640。標準が無い行（板金・修理）では 0。生成器も同じ）、`@` 板金ランクダイアログで決定、`$` 暫定指数（11.DB `[71]=='$'` の変種行。ERParts.Provisional='$' が常に付き、標準工賃のままの行は WageByManual も '$'、空欄行は ''、手入力指数は '#'。W/S 頁明細の工賃欄に `$` 表示。他工場 NEO 10 本の prov 行 30 件で例外なし、FRAME_p8 実機 2026-09-06）
- **コグニの再評価（工賃消失現象の正体）**: 明細で修理方法を変える等の操作で全行が再評価され、`WageByManual=''` の行は装備条件から標準区分（例 フェンダ U→U1 0.5→0.6、ラジエタ O5 1.0→0.9）を選び直し、標準に無い指数の行は `Time/Wage=-1` に消える。`#` の行は保持される。非コグニ書式の見積は `index_policy: manual`（全指数 `#`）で生成する

### コメント・保留・非課税・値引
- ERParts コメント: `CommentFlag=1, Comment1`（コメント入力ダイアログ。TEXT(40)、コグニ保存時に 40 バイトで切詰）
- 保留部品（見積書の「保留」行）: **コグニ自身の保留登録（W/S の修理方法選択ダイアログの「保留」チェック → SIENTA_hold.neo 2026-09-05）は ERParts 行に `ReserveFlag=1, ReserveRecordNo=0, PartsPriceFlag=1, PartsPrice*/Wage*/Time=-1`、DamageParts は `BlockCode` を保ったまま `PartsType 0`（実機 cogni_CX4 6800。2026-09-08 に「0005 は 1」と書いたのは、0005 が可能作業 `S` のみの品目だったための取り違え — §10-19）（合計に含めない、明細でピンク表示）を立てるだけで、ReserveERParts と Ex.db [ADASWork] には何も書かない**（ReserveERParts は ADAS 専用 §10-6。以前の「ReserveERParts にコピー」は誤りで、コグニの ADAS 画面で「車種データに存在しない作業」と警告される原因だった）。明細の保留チェックボックスはダブルクリックで表示が変わるが保存されない。生成器も同形（ReserveFlag=1, ReserveRecordNo=0）。コグニは見積結果「保留登録 n 件」、保留一覧ダイアログ（保留部品代・保留工賃）、見積書には「保留」行として印字（金額欄空）
- Expense 非課税: `OutTaxFlag=1, InTax=OutTax, Tax=0`。Total `hy_PartsNoTaxTotal*/hy_WageNoTaxTotal*` に集計し、SubTotal・消費税の外で Total に加算（「その他非課税額」）
- 値引・割増（総合計画面）: Total `pt_ExtraTotal*`（部品）/ `wg_ExtraTotal*`（工賃）、`*_ExtraFlag 1=割増(+) 0=値引(−)`、金額は絶対値、`*_ExtraUnit=1`（円単位）, `*_ExtraArrangeFlag=1`（四捨五入）, `pt_IncludeRecycle=1`（値引にリサイクル部品代を含む）, `wg_IncludeMaterial=1`（値引に塗装材料代を含む（工場ごとの設定値で状態依存: 他工場 NEO 9 本のうち 7 本が 1、同一工場の 2 本が 0。本 PC の新規見積 NEW3/NEW4 は 1。生成器は 1 を書く）。総合計画面の既定。新規見積 NEW3 も 1）, `*_ExtraRate=-1`（率入力なし）。SubTotal に加減

### その他
RWLinkParts, EPCLinkParts, ReserveERParts（ADAS 作業 §10-6。保留は ERParts の ReserveFlag で表す）, PartsPlan(`NameShift=0, SortMode=0`), Fixer（値引き行 5）, DamageComment, DamageImage。通常は空またはテンプレート値。PaintingLinkParts は 20.DB に載る部品（外板パネル・バンパ・4802 のような工賃行）の取替(0)/修理(2)/板金(6) 行（RecordNo=ERParts.RecordNo, PanelName=20.DB。他工場 NEO 10 本の 45 行 = 取替 23・修理 14・板金 8 がすべて 20.DB 収録部品。修理(2) の例は S64 12081431 の 0010/3810 = 証拠パック neo_REAL_12081431.json。バンパ 0010/3810 は PaintingPanel には出ないが PaintingLinkParts には出る）。

## 5. AnSMB.txt（142B 固定長、CP932）
```
[0:8]   LineNo 8 桁            例 00000010
[8:12]  PartsCode              例 0010
[12]    PartsCodeSub（リサイクル部品は '1'、通常 ' '）
[13]    DisposalCode           例 '1'
[14:39] PartsName（25B）
[38:62] PartsNameStandard（24B。§10-14 で確定。ここは旧記述）
[62:80] PartsNo（18B、工賃のみ行は空）
[80:98] PartsNoStandard（18B）
[98:100] 数量 2 桁
[101]   RecycleFlag（リサイクル部品 '1'）
[102]   ReserveFlag（保留行 '1'。実機 cogni_CX4 6800。§10-19）
[103]   '0'（未使用）
[104:116] **12.DB の CutWork 欄 `[65:76]`（部分切断作業）に英字があれば `'1'` + その欄、無ければ `'0'` と空白の 12 桁**（実 NEO 801 行で完全一致。§10-19・§12 の記述が正。FRAME_p7 の AnSMB '…01000011K     1K…'。**旧記述『104 桁 = 可能作業 [52:55] に S を含むか』は誤り** —— 2026-09-12 訂正）
[105:116] **[104] が '1' のときだけ** 12.DB の CutWork [65:76] 本体（'1K     1K' 等。'S' は入らない: evidence/J52_12DB_A25_raw.txt の 1511/1512 行）。**英字が無ければ [104:116] は '0' + 空白**（数字だけの '0      0   ' 等は転記しない。2026-09-12 訂正）
[127:133] 'F99999'
末尾 CRLF
```
PartsName は [14:39] の 25 バイト枠（ERParts.PartsName は 24 バイトなので実質 24 バイト＋空白 1。コグニは文字の途中でも切る）。

## 6. 金額規約
- 行ごとの税 = **四捨五入(税抜×0.1)**（PartsPrice / PartsPriceStandard / ChangeTotal / Wage*。コグニ生成 NEO の税抜末尾 5 円の行 89 件が全て四捨五入: 04011103 の 245 → 25、195 → 20、295 → 30 = neo_REAL_04011103）。例外 2 つ: **PartsUnitPriceTax は切捨**（NONE_dc 単価 155 → 15）、**数量行（PartsCount>1）の PartsPriceTax = 四捨五入(単価×0.1)×数量**（155×10 個 → 160・税込 1710、185×9 → 171。行合計 1550 の 10% = 155 ではない）。旧生成器が切捨で書いた PartsCode 空の行（195 → 19）はコグニに温存されるだけで規則ではない。Total.ms_PartsTotalTax は行ごと税の合計。生成器 `tax_of()` は四捨五入（2026-09-06 確定）
- 工賃指数の標準区分: 11.DB 変種行（修理方法×年式群×グレード/FVA/EVA フラグ）が持つ 15.DB 区分レター列を合計（ADDATA §11-7）。WorkCode は区分レターの連結（'FG'）。連動部品があると別区分の指数が加算されるが、WorkCode に区分が並ぶのは**同じ修理方法の相手**のときだけ（'GH', 'A0B0D0Z0'）。脱着板金(3) の相手による加算は時間だけで WorkCode は元のまま（NEW2 0010: 1.3 + 0.1 = 1.4、WorkCode 'B'）
- ERParts 標準工賃 = **四捨五入10**(指数 × wb_PriceBase)（指数は時間。15.DB の wi は ×100 の整数なので wi×単価/100）。確定根拠（2026-09-06）: 本 PC のコグニで工賃単価を 8,750 に変えて再計算した ROUND_8750.neo は 0.3h → 2,630、1.9h → 16,630、0.9h → 7,880。他工場のコグニ生成 NEO でも同点 10 件が全て四捨五入（04011141: 0.5h×7,730 = 3,865 → 3,870、4.5h → 34,790。12051345: 1.9h×6,950 → 13,210、0.7h → 4,870）。以前「偶数丸め」の根拠にした CHR_ETO.neo は当方の旧生成器の出力で、工場 PDF の値は 2,630（四捨五入）だった。生成器 `r10_even()` は名前のまま四捨五入に変更
- 塗装工賃 = 四捨五入10(指数×wb_PriceBase)（十進で計算。1.7×8750=14,875 → 14,880）
- ChangeTotal = 標準部品代 + 四捨五入10(連動加算前の標準指数 × レート)（§4 の ChangeTotal 行）
- SubTotal = 部品（ms_PartsTotal）+ 工賃（ms_WageTotal）+ 塗装（pn_Total）+ 内板骨格（nk_Total）+ 費用の課税分（hy_PartsTaxTotal + hy_WageTaxTotal）+ 値引・割増（pt_ExtraTotal + wg_ExtraTotal。値引は負）（SIENTA_w: 770,170 + 364,800 + 252,080 + 51,200 + 240 + 31,000 = 1,469,490。CHR_ETO_exp は pt_ExtraTotal 1,000 込みで 1,191,030。生成器 `sub = parts_total + wage_total + paint_total + nk_total + pt_x + wg_x + …` と同じ）; Total = SubTotal + tx + 非課税費用（hy_PartsNoTaxTotal + hy_WageNoTaxTotal。CHR_ETO_exp: + 5,000 で 1,315,133）; tx = **四捨五入**(SubTotal×0.1)（N-ONE 保存版 630,665 → 63,067。10 円単位の見積では切捨と同値）
- 工場見積が円未満まで工賃を計上している（日産系 8,610×1.3=11,193 等）場合、コグニ形式では指数×単価の 10 円丸めに直す。ERParts に 11,193 を入れると `*` 付きで保持はされるが総合計がコグニ計算と食い違う

## 7. コグニが保存時に自動で書き換えるもの（生成側で気にしなくてよい）
DamageBlock/DamageParts の再生成、AnFlInfo の `AnVer.db` 更新、`.~ne` 作成。生成側でも同形にしている: PartsPlan `(0,0)`、ReserveERParts 既定 1 行、ReportTitle は ReportID の Unicode 順、TEXT(n) 列の cp932 n バイト切詰（生成器の `_fit()` は文字境界で止めるので半端バイトは書かない。AnSMB の固定長はバイト単位）ト切詰、AnSvMail.ini の AgreedName 30 バイト。

## 10. コグニ新規見積フローと各画面の NEO 出力（2026-09-05 夕方、N-ONE J52 で新規作成 → NEW1〜NEW4.neo・帳票 11 種 PDF）

### 10-1. 新規見積ウィザード
1. 工賃単価の入力: 基本単価・工賃単位（1 円/10 円/100 円）→ Setting `wb_PriceBase, wb_Round=10, wi_Round=10, TaxRate=10, tx_CalculateFlag=1, tx_Unit=1, tx_ArrangeFlag=1`
2. 車種の検索: 「メーカーから検索」（メーカー→車名→モデル→年式→ボデー形状→エンジン→グレード、車台番号検索）/「ワークシートから検索」/「車検証から検索」（初度登録年月・車台番号 頭/尾・型式指定番号・類別区分番号 → KA81/KA06 で車種確定）。車検証検索の保存値: CarSearch `SearchMethod=3`, `ps_CarMouldNo/ps_CarKindNo/ps_CarRegDate/ps_CarRegEra/ps_CarRegEraYear/ps_CarSerialNoHead/Tail/ps_CarSerialNo/ps_YearSearchFlag（車台番号レンジ KA06 で年式が確定したとき 1、型式指定＋類別＋初度登録だけの検索は 0: C10_y01.neo。生成器 `1 if by_serial else 0`）`, `ev_*` に確定した Car 値、Customer にも 車台番号・型式指定・類別・初度登録
3. 装備バリエーション（10.DB の EVA 一覧を複数選択）→ CarEVA、カラーコード（26.DB の一覧選択。「上部色と下部色を指定する」で 2 色）→ Car.ColorCodeFlag/ColorCode/ColorName/ColorRGB1、トリムコード（29.DB）。「この車種はカラーコードによる部品検索が行えます」= 83.DB または 13.DB 収録車（両者は排他、evidence/car_file_counts.json）
4. 完了 → 見積結果画面。塗装タブを開くまで PaintingPlan.Coat は -1（integer）、CoatName は ''（空文字。NEW1 を sqlite の typeof で確認。Paint と MaterialRate は入っている。開いた時点で 66.DB からカラー→塗膜を引き、26.DB の備考をダイアログで表示）

### 10-2. 部位画面 → W/S（電子ワークシート）→ 明細
- 部位画面: 車両図＋「フロント部 / ALL / リヤ部」で部位ブロック（17.DB、KATB150 のグループ見出し付き）を選択。DamageBlockPlan `DamageCode='01020304', FrontArea=1`（フロント部）/ `'0102030405060708'` と 3 フラグ（ALL）。DamageBlock には選択した部位の 17.DB ブロックだけが入る（フロント部 31 行、ALL 41 行）。KATB120 が DamageCode → BlockCode の対応
- W/S 画面: `<car>WS.CAB` の TIF（1/32 頁 ご案内、2 頁〜 部位ごとの図）。右の一覧は 12.DB の TitleNo（No 列）・名称・`*`。No 列の左の `*` は Construct [59:61] の G フラグ（15.DB に行を置く host 部品: 骨格の 1400/1410/1430/1434、ADDATA 仕様 §11-7c）。Gothic [58] の '*'（塗装対象パネル）は別の列で、W/S 一覧の印ではない。部品の 左・単・右 セルをクリックすると「修理方法選択」ダイアログ（追加入力/削除、保留、コメント、取替/修理/板金/点検/調整/脱着/脱着修理/脱着板金/点検調整/分解調整 の 10 方式。左右部品は 左/右 の 2 段）。修理・調整・点検・脱着修理 は続けて「実額入力」（工賃・時間の手入力、片方を入れると他方が単価から自動計算）→ 時間を入れた（指数が確定した）行は `WageByManual='#'`（ADDATA に標準がある部品なら TimeStandard/WageStandard/WageFileTime は標準値のまま残る: REAL_04011103 の '#' 行 Time 0.7 / TimeStandard 0.5。標準の無い部品は 0 = §4）、工賃だけを入れた行は `'*'`（§4 の記号定義）
- 修理方法名と DisposalCode: 取替 0 / 脱着 1 / 修理 2 / 脱着修理 3・**脱着板金 3** / 点検 4・調整 4・**点検調整 4**（DisposalName はそれぞれの名前） / 分解調整 5 / 板金 6。生成器は estimate.json の method 文字列（脱着板金・点検・調整 等）をそのまま DisposalName に書く（2026-09-05）。明細画面のボタンは 0/1/2/6/3/4/5/9 のみ、10 方式は W/S からだけ選べる
- 脱着板金(3) は 取替 標準指数の連動加算に効く（0010 取替 1.3 → 0020 脱着板金を足すと 1.4、WorkCode は 'B' のまま。0020 取替なら 'BC'）。`ChangeTotalOutTax`（取替参考）= 標準部品代 ＋ その部品の取替標準工賃（0020 脱着板金でも 2,750＋860=3,610）
- 明細の PartsName / PartsNameStandard は 11.DB（色別なら 83.DB / 13.DB）の名称欄 20 文字から生成: 標準 = 名称欄そのまま（' Fﾊﾞﾝﾊﾟﾋﾞ-ﾑ' / 'LFﾊﾞﾝﾊﾟｽﾍﾟ-ｻ' / 'L ﾍﾂﾄﾞﾗｲﾄﾕﾆﾂﾄ' / '  ﾊﾞﾝﾊﾟｸﾘﾂﾌﾟ'）、表示 = [0] L→左 R→右 ' '→2 スペース ＋ [1] F→Fr R→Rr ' '→2 スペース（'  Frﾊﾞﾝﾊﾟﾋﾞ-ﾑ' / '左Frﾊﾞﾝﾊﾟｽﾍﾟ-ｻ' / '左  ﾍﾂﾄﾞﾗｲﾄﾕﾆﾂﾄ' / '    ﾊﾞﾝﾊﾟｸﾘﾂﾌﾟ'）。長音は 11.DB のとおり '-'
- **品番の出ない行（脱着・板金・修理）でも名称は 11.DB から作る**（2026-09-10 確認）。12.DB の名称は左右を持たないので、
  そこで作ると「左Frﾊﾞﾙｸﾍﾂﾄﾞｻｲﾄﾞｽﾃ-」が「Frﾊﾞﾙｸﾍﾂﾄﾞｻｲﾄﾞｽﾃｰ」になり左右が消える（実機 cogni_T1 1410 / cogni_H24 2599）。
  条件は「12.DB の可能作業に K（取替）がある」かつ「11.DB 側が左右記号を持ち、12.DB と同じ品目名」。
  取替できない作業項目（12.DB の「Fﾗｲｾﾝｽﾌﾟﾚｰﾄ(修理)」「Fｻｽﾍﾟﾝｼﾖﾝ(片側)」など）は 12.DB の名称が正（実機 cogni_CX1 0005 / H24 7600）
- 11.DB は修理方法ごとに名称欄が違うことがある（0010 は取替 ' Fﾊﾞﾝﾊﾟﾌｴｲｽ(ﾄｿｳｽﾞﾐ)' / 脱着・修理 ' Fﾊﾞﾝﾊﾟﾌｴｲｽ'）。
  ただし実機は**同じ部品・同じ修理方法でも両方の形で保存されている**（cogni_M1/R1 は取替名のまま、cogni_H6/H11 は脱着名）。
  コグニが部品を入れた時点の修理方法で名称が決まり、後から修理方法を変えても名称は残るため。estimate からは操作履歴が分からないので品番の取れた行の名称を正とする
- 税込・税額の列（*InTax / *Tax）は、対応する税抜列が -1 の行では -1（実験 67 ファイル 398 行で例外なし）。税抜が 0 以上なら計算値
- OrderFlag は工場から届いた NEO を開いて保存したファイルでは '0'、コグニで新規作成した見積では ''。
  品番の候補が複数ある部品には '1' が立ち、PartsNo の末尾に '*' が付く（実機 2360 ﾄﾞｱﾗｲﾆﾝｸﾞ。ADDATA の暫定フラグとは無関係で、条件は未特定）
- `ConstructGroup` = 選ばれた 11.DB 変種行の [68:70]（価格の直後 2 文字）。取替行は K 行の値（'P6'/'G4'/'R0'/'MM'）、脱着は D 行、脱着板金は標準指数と同じ DS 行（無ければ D 行）の値（通常 '  ' だが D88 8200 'QQ'、W66 6350 'N8'、S64 0402/0452 'A1'/'A3' のように値を持つ部品もある = 他工場 NEO の脱着系 55 行すべて D 行と一致）。標準の無い実額入力（'#'）行（修理・点検調整・分解調整、標準無しの脱着系）は ''（空文字。証拠 JSON は 2026-09-07 から '' を残す形式）
- **色別部品（83.DB / 13.DB）**: Car.ColorCode が確定していて選んだ 11.DB 変種（ボディ条件込み）の色別フラグ（[70]=1）が立つ部品は、83.DB（188 車種）または 13.DB（672 車種、排他）のカラーコード一致行の品番・価格・名称 '(ﾄｿｳｽﾞﾐ)' を使う（J52 0010 → 71101-T4G-N00ZG 50,700、D98 0010 → 52119-B2G40-A0 51,900 = COLOR_D98.neo。見積書に別色の品番があってもコグニの再検索で置き換わる。車種数の根拠 evidence/car_file_counts.json）
- 作業項目確認 = 89.DB（NEW3: 0400 取替（WorkCode 'FG'）→ Fグリルスキン 脱着 を提案）。「選択」にチェックして登録すると明細の末尾に行が追加される（SIENTA_w.neo 2026-09-05: 6150 右クォータウインド 脱着 = 15.DB の標準 1.5 'F5'、6048 セーフティシティカバー 脱着 = 標準なし Time -1 'Y5'。BlockCode は 12.DB の部位、DamageParts PartsType 0、WageByManual ''）。候補が残っている間は明細画面へ切り替えるたびにダイアログが出て、キャンセルすると画面切替も中止される（「前回選択しなかった作業を表示する」を外して登録すると出なくなる）。24.DB（W69 ランプ関連）は C-HR に 0400 取替を足しても提案なし（未確認のまま）

### 10-3. 塗装（バンパ単独・パネル追加）
- パネルが無くバンパだけ塗装（**2026-09-12 に生成器で対応**: `paint.panels: []` ＋ `bumper_front`/`bumper_rear` で書く。実機 W66 0010 取替 + バンパ新品一色 = `cogni_W66w` と全列一致。加算基礎数値 `Base*` は -1・印 ''、`BumperBase*` に BAN.DB の値、`TimeTotalBumper`/`WageTotalBumper*` はバンパ + バンパ加算の合計）→ PaintingPlan `BumperBaseTime/…`（バンパ加算）に COM/BAN.DB `[車形, 塗膜クラス, 値]` の値（J52: ソリッド 0.4 / メタリック・2コートパール 0.5 / 3コートパール 0.7。塗料・一色/二色・修理方式・F/R・両方でも同じ）。新規見積の初回選択では `fb_WageByManual='$'`, `BumperBaseWageByManual='$'`（値は標準）。パネルを 1 枚でも追加すると バンパ加算は消え 加算基礎数値（T_KEI_3）になる
- 材料代の既定値 = **塗装工賃計 × 割合 を 10 円単位で四捨五入**（15,500×15%=2,325→2,330、18,950×15%=2,842.5→2,840（double で 2842.49…）、46,490×15%=6,973.5→6,970、46,490×14%=6,508.6→6,510）。従来の floor10 は誤り
- 塗装計タブ: 外板パネル（パネル＋加算基礎＋ブース）/ バンパ / 内板骨格 / 付加塗装 の工賃と材料代、小計、割合、塗装費用計 = 塗装工賃計 ＋ 材料代 ＋ 追加塗装工賃計

- **塗装の入力方式（指数 / 実額）= `PaintingPlan.InputType` / `InputTypeName`**（実案件 NEO 400 本、2026-09-19 調査）:
  **`1 = 指数`** 308 本 / **`0 = 実額`** 91 本 / `2 = 参考` 1 本。**4 本に 1 本は「実額」**で作られている。
  「実額」はコグニの塗装画面で**総額を 1 つの金額として入れる**方式で、NEO はこうなる:
  - `PaintingPanel` は **0 枚**（91 本中 69 本。残り 22 本はパネル・追加項目と併用する混在型）
  - `PaintingTotal` の `WageTotal*` と `MaterialTotal*` は **0**、**`TotalOutTax` にだけ総額**（実測 106,335 / 149,645 / 213,400 …）
  - `Total.pn_TotalOutTax` が同額、`pn_MaterialTotalOutTax` は 0
  - 加算基礎・ブースは無効（`BaseTime/BaseWage = -1`、`BoothFlag = 0`）
  - **工賃計の手入力印 `WageTotalByManual` は付かない**（実額では印そのものが要らない。400 本すべて空）
  - 材料代の方式（`MaterialRateType` / `MaterialRate`）は残るが、割合 0 の案件が多い
  **生成器は `InputType` を書いておらず、雛形の `1`（指数）のまま**。工場見積が「塗装費用 ◯◯円」の一式しか
  出していない案件を、判断規則 10-15 で**パネル別に組み直している**のはこのため。実額で入れれば印字どおりに書ける（未実装）

### 10-3-2. 取替合計（ChangeTotal）と標準なし行（2026-09-12 W90b）
- ボディ専用行（15.DB の sub が枝番違い: 4800 に対して 4801）が共通行を押しのけた取替行は、表示の `Time` は -1 のまま、`ChangeTotalOutTax` にはそのボディ行の指数（W90 4800 = J3 9.0 + Q4 0.7 = 9.7h × 単価）が入る（44,900 + 940,900 = 985,800）。生成器は host_fallback でその行を採る
- W90 4600 取替に 4800 を足すと 4600 の指数が 4.0 → 11.7（ChangeTotal は 4.0 のまま）。15.DB の K 行（link MM・sub 5001・ボディ 20）の 1170 と同値だが規則は未同定（既知差 W90b）

### 10-4. 帳票（印刷ダイアログ → PDF(通常)）
出力帳票は複数選択でき 1 帳票 1 PDF。帳票名（印刷ダイアログの一覧は §9 の 15 種）: 見積り1（部品価格・工賃）/ 見積り2（＋指数）/ 見積り3 / 見積り1・2(塗装明細)（末尾に【塗装明細】: 塗装費用計・内訳・塗料・塗膜・パネル行・加算基礎・バンパ・材料代・割合）/ 指示書（指数のみ、合計指数）/ 指示書(塗装明細) / 取替部品1・2（取替行の品番・価格のみ）/ 塗装明細1・2（塗装項目のみ、2 は指数付き）/ 見積書表紙1（合計・登録番号・車種）/ 見積書表紙2(計算書)（非課税・課税の内訳、リサイクル部品、塗装工賃・材料代）/ 報告書（損害報告書: 証券番号・契約者・事故日・立会日・工場・協定…）/ 確認書(税込)（明細を税込で印字、消費税総額計算 明細税抜/税込）。帳票の印字記号は ERParts の WageByManual をそのまま出す: 指数（時間）を手入力した行は `#`、工賃だけを手入力した行は `*`（§4 の記号定義。12051345 の 3800 は '*'、NEW2 の 0030 は '#'）。塗装の手入力/標準外も `*` が数字の後ろに付く。ヘッダは 部品価格適応日・作成日・住所・氏名・登録番号・型式指定・類別・車名型式・初度登録・車台番号・走行キロ・エンジン型式・カラーコード・トリムコード・ライセンス ID。「帳票出力画面」（車種情報の確認: 初度登録・車台番号・型式指定・類別・装備バリエーション）を経て保存ダイアログ（既定名 `<ファイル名>_<帳票名>.pdf`）

### 10-6. ADAS（運転支援システム再設定・調整）— シエンタ W66 で確定（2026-09-05 夜、SIENTA_adas.neo）
- ツールバー「再設定」は `<car>55/56.DB` を持つ車種（81 車種）で有効。画面: No / 項目番号 / 枝番 / 作業名称 / 指数 / 工賃、行追加・行削除・全削除、「作業選択」ダイアログ（センサ別再設定・調整作業 = 56.DB、基本作業 = 55.DB。項目番号 A010/A100/A120…、枝番、作業名称、指数、「指数ヘルプの備考欄をヒント表示」）→ 反映
- 保存先 = **ReserveERParts**（ADAS 専用。ADAS 行は `PartsCode = 55/56.DB の PartsCode (9900/9902/9940…)`, `DisposalCode 1 = 基本作業(A0xx) / 2 = センサ別(A1xx)`, `PartsNo = ItemNo ('A010')`, `PartsNoStandard = ItemNoSub ('1')`, `Time/TimeStandard = 指数`, `Wage*/WageStandard* = 指数×単価`, `WageByManual ''`（工賃を手入力して標準と違えば '*'。生成器も同じ）, `WorkCode = 3 スペース`, **`ConstructGroup = 56.DB の ItemNoSubCombiCode`**（9940/9942 → 'A13'、無ければ 3 スペース。ADAS_D98c/D98d.neo 2026-09-06 夜）, `Provisional/DamageArea/DamageRank = 1 スペース`, **`DamageRankBtn1 = 55/56.DB の OrderNo`**（作業選択ダイアログの行番号: 9900 → 1、9904 → 3、9930 → 1、9940 → 3、9942 → 4。通番ではない）。画面の空の入力行が `DisposalCode 3, WageByManual '*'` の行として先に RecordNo を取り、LineNo は表示順（作業行→空行が最後）。センサ別作業を選ぶと 56.DB の BasePartsCode（9900/9904）の基本作業が自動で付く（作業選択ダイアログで基本作業側にチェックが入る）
- `AnSvEm0001Ex.db [ADASWork] Idx<RecordNo>.PartsCode/ItemName/Comment`（空の入力行は PartsCode 空、ADAS 行は 作業名称と備考 'GTS使用' 等）
- 合計: ADAS 工賃は `Total.ms_WageTotal*` と SubTotal に入る。見積結果の「工賃」が増える
- ReserveERParts に ADAS 以外の行（旧生成器が書いていた保留行）があると ADAS 画面に赤字で表示され「車種データに存在しない作業が計上されています」と警告される
- **24.DB** = 部品（ref＋K/S）→ 組合せコード（AAA/A00/A12/A13/A14…）。**実機 D98（2026-09-06 夜、ADAS_D98c/D98d.neo）: 0403（A00）/0404（A12）を取替で入れても再設定の作業選択ダイアログに事前選択・警告は無く、保存 NEO にも 24.DB のコードは現れない → NEO 生成には不要（用途は画面外の可能性、未確認）。** 以下は構造の記録: 同じ体系のコードが 56.DB の `ItemNoSubCombiCode` にある車種（D18 = 証拠パック `D18_24.DB` / `D18_56.DB_codes`: 24.DB は 0400 → AAA、0401 → A00、0404 → A12、0407 → A13、56.DB の 9930〜9936 は A20/AAA/A00/A12（A13 は 56.DB に無い）から、取替した部品に対応する ADAS 作業を候補にするリンクと**推定**（W66 は 56.DB に 9930/9932/9940 の行はあるが 24.DB と照合する組合せコード欄（ItemNoSubCombiCode / ItemNoCombiCode）が空。実機で候補提示は未確認。生成器は 24.DB を使わず明示指定の作業だけ書く）
- 生成器: `estimate.json` の `adas: [{code: '9900' | item: 'A010', sub: '1', index, wage, comment}]`（指数・名称は 55/56.DB から）

### 10-7. 装備バリエーション変更と再検索（N-BOX J87 実機 2026-09-08、NEO_check/_eva_exp/cogni_*.neo 9 本）

- 画面: 見積結果 → その他 → **装備バリエーション変更(V)**。装備名の一覧（10.DB の装備。エンジン A〜E と 4WD Z は出ない）をクリックで ON/OFF（トグル）、カラーコードもここ。次へ → 「入力されている内容で再検索を行いますか？ はい（部品番号・部品価格・工賃を更新）/ いいえ」「手入力(*,#)を残す」「個数を残す」「保留登録状態を残す」→ 完了。複数候補のある部品ごとに「複数部品選択」ダイアログ（色付き / 未塗装、IR カット等。既定は先頭 = 生成器の選択と同じ）
- 再検索の結果 = 生成器の `_std_row`（11.DB 変種）と `cogni_standard`（15.DB 指数）の選択と **部品番号・価格は 9 条件 × 9 部品で全件一致**（base / T / U / TU / TUPXV / pair none / U / P / Q）。実案件オデッセイ（J22、69 行、装備 P）も再検索で合計 1,332,012 が再現
- **枠の取り合い**（唯一の差分 → 生成器に実装 `AddataParts._slot_taken`）: 同じ部位ブロックで、同じ区分（レター+サイクル）・同じリンク符号の 15.DB 行を別 ref が **車両条件に一致する条件付き**（グレード一致 / EVA 一致、ボディ 0 か一致）で持つとき、無条件行しか無い ref の標準指数は消える（Time −1、WageOutTax −1、TimeStandard 0、WageFileTime ''）。部品番号・価格は影響なし。例: 0400 ヘッドライト（D 20 / E 10、link A0/B0、空フラグ）vs 0402 ヘッドライトユニット（同 link に U 行）: 装備 U なし → 0400 0.3 / 0402 なし（0402 の無条件行はボディ 20 専用）、U あり → 0400 なし / 0402 0.4
- 12.DB の行番号の百の位（0xx / 1xx / 2xx）= ワークシートの版（例 A05: 0xx ハロゲン ヘッドライト 0400、1xx/2xx ヘッドライトユニット 0402）。コグニの W/S はそのうち 1 版だけを表示する（表示版の選び方は未解明。装備 T を立てても A01 の 0xx 版が表示された）。12.DB の 1xx 版の注記行「FﾊﾞﾝﾊﾟﾌｴｲｽのLEDﾌﾛﾝﾄﾌｵｸﾞﾗｲﾄ付車の取替は指数が設定されておりませんので、弊社独自の参考値で工賃計算されます」= 暫定指数 '$' の出どころ
- コグニが再検索後に保存した ERParts.PartsNo は 17 桁の右空白埋め（`'71101-TY0-000ZS  '`）、複数候補の部品は末尾 `*`（`'83583-TY0-013ZA  *'`）。PartsNoStandard は埋めなし。工場から届く NEO には埋めが無いものも多く、合計・再評価に影響しない（生成器は埋めない）。Time は 0.30000000000000004 のような浮動小数のまま保存される
- 24.DB（`J87,01,0400,K , ,00,     ,  ,AAA`）= 部品 → 運転支援システム再設定・調整の項目コード（ADAS の対応表）。装備の適用可否ではない
- 11.DB で「全ての取替行が装備条件付き」の部品（装備が無いと計上できない部品）は J87 / J22 / W82 には無い。装備の影響は 変種（品番・価格）と 15.DB の指数（フラグ行・枠の取り合い）の 2 つ

### 10-8. 明細画面の手入力（N-BOX J87 実機 2026-09-08、NEO_check/_eva_exp/exp_manual.neo + 見積り1/2 の PDF）

- 明細のセルは クリックで選択 → Enter で編集 → 入力 → Enter。工賃・指数・部品価格・名称・部品番号が直接編集できる。修理方法は 0:取替 等のボタン（部品コードの無い手入力行には付かず、空欄のまま）。12.DB のレベル欄で親子の部品（0400 ⊃ 0402/0408）を同時に載せると、編集のたびに「重複部品コードチェック」ダイアログ（削除 / キャンセル = 枝番）
- **工賃だけ手入力（`*`）で標準指数のある行**（2700 取替 標準 2.8h/22,400 → 25,000）: `Time -1`、`TimeStandard 2.8`、`WageStandardOutTax 22,400`、`WorkCode 'Q1T1'`、`WageFileTime '2.8'`、`ChangeTotalOutTax 92,000`（= 標準部品代 69,600 + 標準工賃 22,400）。印刷は指数欄空欄・工賃 `25,000*`
- **指数を手入力（`#`）**（3500 標準 2.8 → 3.00）: `Time 3`、`TimeStandard 2.8`、`WageStandard 22,400`、`WageFileTime '2.8'`。印刷 `3.00 ... 24,000#`
- **部品価格を手入力（`*`）**（0400 43,000 → 45,000）: `PartsPriceByManual '*'`、`PartsPriceStandardOutTax 43,000` は残る。印刷 `45,000` の後ろに印は付かず、工賃側の `2,400*`（価格の印が工賃欄の右端に出る）
- **部品コードの無い手入力行**（名称と価格だけ）: `PartsCode ''`、`DisposalCode -1`、`DisposalName ''`、`PartsName` は入力どおり（付属部品の 2 スペース接頭辞なし）、`PartsNameStandard ''`、`PartsPriceStandardOutTax -1`、`PartsPriceByManual '*'`、`ChangeTotalOutTax -1`、`PartsFileTime ''`、`ConstructGroup ''`、`Time -1`、`WageOutTax -1`。工賃も入れると `WageOutTax 3,000`・`WageByManual '*'`。印刷は `塗装費用 61,900 *`、`材料代 1,000 3,000**`
- 明細で編集した行の `OrderFlag` は `'0'`（未編集の行や工場 NEO は `''` が多い。合計・再評価に影響なし）
- コグニの帳票 PDF（PDF(通常)）には文字層がある（pypdf で 700 文字/頁）。FAX 化されていなければ OCR 不要で転記できる
- 生成器: `unit_manual_rows.py`（実機期待値）で上記を固定。draft は手入力行の修理方法が空欄なら `''` のまま（`DisposalCode -1`）

### 10-9. 塗装画面の材料代・ブース・バンパ（N-BOX J87 実機 2026-09-08、NEO_check/_eva_exp/exp_paint_A/B/C2/D.neo + 塗装明細 PDF）

- 明細で取替にした外板パネル（2700/3500 スライドドア）は塗装画面に自動で載る（92 dm²、1.60、12,800）。加算基礎数値は自動（2.90、23,200）
- **ブース使用の既定はオフ**（`BoothFlag 0`、`BoothTime/BoothWage* -1`）。生成器は `paint.booth` があるときだけブースを立てる（以前は BOOTH.DB に値があれば常に立てていた）
- 材料代割合 **費用割合**（既定 15%。この PC のコグニ既定。工場によって違う）: 材料代計 = 塗装工賃計 × 割合（一括で四捨五入。A: 48,800 × 15% = 7,320、B: 62,400 × 55% = 34,320）。割合は塗装条件/塗装計タブの「割合」欄
- 材料代割合 **手入力**: 材料代計に額を直接入力（`*` 付き。C2: 30,000）。`PaintingTotal.MaterialTotalOutTax` にその額、`MaterialByManual '*'`。生成器は `paint.material` に額を書けば同じ
- 手入力 → 費用割合 に戻したあとに保存すると `PaintingTotal.MaterialTotalPanel/Bumper/Frame/Etcetera*` が `-1`（新規に費用割合のままなら 0）。合計に影響しないので生成器は 0 のまま
- バンパ **新品**（一色）: `fb_Disposal 1`、`fb_Draft -1`、`fb_DraftName ''`、1.70 / 13,600。**変形修正**: `fb_Disposal 2`、`fb_Draft 0`（絞模様 無し）、3.60 / 28,800。生成器は新品の Draft を -1 に修正
- 塗装明細付き帳票（見積り1(塗装明細)）にも文字層がある

### 10-10. 骨格の組合せ（J87）・工賃単位・消費税設定・脱着行（N-BOX J87 実機 2026-09-08、NEO_check/_eva_exp/cogni_frame_F1/F2/F2_r100/F2_taxfloor.neo）

- **骨格の組合せ指数は J52 以外（J87）でも同じ規則**: 生成器の NEO（1400 バルクヘッド取替 + 1410/1420 ステー取替、1400 取替 + 1410 脱着 + 2620 サイドシル + 5600 リヤフロア）をコグニで開き再検索しても合計・指数が変わらない（1400 1.6 / 1410 1.1 / 1420 指数なし、2620 は暫定 `$` 2.55、5600 1.9）。`unit_settings.py`
- **脱着(1) 行で 11.DB に脱着(D) 変種が無い部品**（1410 は K/S のみ）: 再検索後は `PartsNoStandard ''`、`PartsPriceStandard -1`、`ChangeTotal -1`、`PartsCount 1`（生成器は取替行の品番を流用していたので修正。PartsCount は工場 NEO の脱着行がほぼ -1 なので -1 のまま）
- **工賃単価変更ダイアログ**（その他 → 工賃単価変更(W)）: 基本単価と「工賃単位 1 円 / 10 円 / 100 円」。100 円にすると `Setting.wb_Round 100`、`wi_Round` は 10 のまま（生成器は両方 100 にしていたので修正）
- **消費税設定ダイアログ**（その他 → 消費税設定(T)）: 計算する / 税率 / 表示方法 外税・内税（`TaxKindFlag` **0 = 外税 / 1 = 内税**。実案件 NEO 1,508 本中 12 本が 1 で、どれも税込の総額を先に決めた協定。`Total` が 470,000・345,000・541,000 のような丸い税込額で、`SubTotal` はその ÷1.1。明細の `*OutTax` は内税でも税抜のまま = 表示方法の設定。2026-09-17 調査）/ 計算単位 四捨五入・切り捨て・切り上げ = `Setting.tx_ArrangeFlag 1 / 2 / 3`。生成器は `estimate.tax_included`（金額が税込で印字された見積書。判断規則 10-4）があるとき `TaxKindFlag=1` を書く。生成器は `estimate.tax_round`（'四捨五入'/'切り捨て'/'切り上げ'）で書き、消費税額もその単位で計算する。draft は合計欄の消費税が切り捨て/切り上げにだけ一致するとき自動で付ける

### 10-11. 連動・吸収・WorkCode・枠を取られた行の取替合計（N-BOX J87 実機 2026-09-08、NEO_check/_eva_exp/cogni_H1〜H7.neo・cogni_G1/G2/G3.neo）

生成器の NEO をコグニで開き「装備バリエーション変更 → 再検索」で保存した NEO と全列比較して確定（`claude_neo_pipeline/tests/unit_link_absorb.py`、`unit_eva_slot.py` の ChangeTotal）。

| 実験 | 明細 | コグニの結果 | 規則 |
|---|---|---|---|
| H1 | 0010 取替 + 0020 脱着 | 0010 0.9（B 80 + C 10）WorkCode 'B'、0020 指数なし WorkCode 'C' | 15.DB を持たない相手（0020）の区分は修理方法の組合せを問わず自分に加算。相手の WorkCode は自分の区分 |
| H6 | 0010 脱着 + 0020 取替 | 0010 0.5（A 40 + C 10）、0020 指数なし・ChangeTotal 3,550（2,750 + 0.1h） | 同上（脱着ホストでも加算）。相手の取替合計には自分の区分の工賃が入る |
| H2 | 0800 取替 + 2300 取替 | 2300 2.1（E1 200 + N1 10）WorkCode 'E1'、0800 0.5 WorkCode 'TN1' ChangeTotal 41,800 | 相手（0800）の区分 N1 は 0800 の 15.DB に無く 2300 の 15.DB にある → 2300 に加算（WorkCode は変えない） |
| H5 | 0800 取替 単独 | 0.5・WorkCode 'TN1'・ChangeTotal 41,800（T 50 + N1 10） | **WorkCode は 11.DB の区分文字列そのまま**。取替合計の標準工賃は自区分の全区分（他部品の下にある区分は車種ファイル全体で一意のホストから）。表示指数は自分の 15.DB の区分だけ |
| H3 | 2300 取替 + 2310 脱着 + 2450 取替 | 2310・2450 は指数なし（Time −1 / TimeStandard 0）、WorkCode 'G1'/'I1' は残る。2450 ChangeTotal 60,100（58,500 + 0.2h） | **吸収**: 自分の 15.DB 行のリンク符号（G1 → E1、I1 → E1）が同ブロックの見積中の他部品の区分（2300 取替 'E1'）に一致 |
| H4 | 2310 脱着 + 2450 取替（ドア無し） | 0.55 `$` / 0.2 `$` | 相手が居なければ吸収されない |
| H7 | 2300 脱着 + 2310 脱着 + 2450 取替 + 0800 脱着 | 0.6 / 0.55 / 0.2 / 0.3、0800 'SN1'、連動も吸収も無し | 2300 脱着 'D1' には E1 が無いので吸収されない。脱着ホストは 15.DB を持つ相手（0800）の区分を取り込まない |
| G1 | 上記の複合 8 行（0172×2 含む） | 合計 317,471 | 全規則の複合で一致 |
| G2 | G1 と同じ、カラーコード無し | 合計 323,521（複数部品選択は一覧先頭 NH851M） | Car/Total/明細（品番・価格）一致 |
| G3 | G1 と同じ、初度登録 H27.5（年式群 00 = H27.1〜H27.10） | 合計 317,471 | Car（YearCode）・明細一致 |
| H8 / H9 / H10 | 0600 ボンネット + 0608 ヒンジ（A15）、4300 テールゲート + 4304 ヒンジ（X10）、4304 単独 | 全列一致（ヒンジ 0.1 'O' / 'H3I3'） | sub 行（ホストの下に sub=自分 で置かれた区分）は他ブロックでも同じ規則 |
| H11 | 0800 板金ランク B（面積 5 = 1.5）+ 2300 板金 '#' 1.5 + 0010 修理 '#' 0.5 + 2450 取替 | 合計一致。ランク行（'@'）は TimeStandard 1.5 / WageStandard 12,000 / WageFileTime '0'。'#' 行は標準 0 のまま | 板金ランクの指数は標準扱い。再検索は 板金/修理 '#' 行の PartsPriceStandard を −1、WageFileTime を '0'、修理行の ConstructGroup を '  ' にする（W/S 経路の工場 NEO は PartsPriceStandard > 0・WageFileTime ''。生成器は W/S 経路のまま） |
| pair_none / pair_U | 0400 + 0402（枠の取り合い） | 0402 ChangeTotal 60,000 = 57,600 + 0400 の 0.3h、装備 U で 0400 ChangeTotal 46,200 = 43,000 + 0402 の U 行 0.4h | 車両条件に合わない／枠を取られた区分の取替合計は、同ブロックで一意の別 ref の行の指数 |

再検索後のコグニは **脱着行の PartsNoStandard を空・PartsPriceStandard −1・ChangeTotal −1・PartsCount 1** にする（H1 0020、H3/H4 2310、H7 全脱着行）。W/S から作った工場 NEO の脱着行は取替(K) 変種の品番・価格・取替合計を持つので、生成器は工場 NEO の形（W/S 経路）を正とし、再検索のこの差は比較で無視する。

### 10-12. 実案件の再検索・消費税小計・塗装条件変更・追加実験（2026-09-08 午後）

実案件 3 件（C01 C-HR ZYX11、C04 アルファード AGH30W、C06 N-BOX JF1）の生成 NEO をコグニで再検索して保存（`NEO_check/<案件>/cogni_research_20260908.neo`）し、生成版と全列比較した。装備は見積の装備を手で選択（ダイアログは NEO の CarEVA を初期選択しない）。

| 確定した規則 | 根拠 |
|---|---|
| **板金(6)・修理(2) 行の WorkCode は空欄**（取替行の区分を流用しない） | C-HR 3500 板金 '#'、工場 NEO の板金 7 行・修理 13 行 |
| 工賃 0 指定などで標準の分岐に入らない行も WorkCode は自区分 | C-HR 3430 ロッカモール脱着（wage 0）: Time −1 / 'R3' |
| 品番 '-'（価格なし部品）の取替合計は 標準価格 0 ＋ 標準工賃（工賃も無ければ 0。−1 にはしない） | C-HR タイヤ 8070/8979、工場 NEO 2 行（いずれも工賃なし。工賃のある '-' 部品は未確認） |
| **見積の指数が 15.DB の単独値とは一致するが組合せ後の標準（連動・吸収込み）と違う行は手入力指数 '#'** | C06 2300 ドア 2.0（標準 2.3 = E1 + フェンダの N1 + ハンドル 2344 の O1）、2700 スライドドア 2.8（標準 2.9）。'' のままだと再検索で標準値に置き換わる |
| **連動加算は WorkCode に出ない**（以前の 'BC' 説は撤回） | H15 2300 取替 + 2344 取替 → 'E1'、H16 0010 取替 + 0020 取替 → 'B'、C06 2300 'E1' |
| 脱着 '#' で標準の無い行: WorkCode は空文字（10 スペースではない）、価格未入力なら PartsPriceFlag 1 | C-HR 3147、C04 1002、工場 NEO の脱着 '#' 2 行 |
| 部品コード無しでも修理方法のある手入力行の PartsFileTime は '0'（'' は修理方法空欄の行だけ） | C04 ﾅﾝﾊﾞｰﾌﾟﾚｰﾄﾛｯｸ 取替 |
| **PaintingPanel は部品コード昇順、LineNo は 0 始まり** | C06、工場 NEO 5 本すべて昇順 |
| 見積書に塗膜が印字されていればカラーコードの 66.DB 塗膜より優先 | P1: 塗装条件で 3コートパールに変えると加算基礎数値 3.6 → 4.5、材料代割合の既定 18%。パネル指数は CHM の値で 2コートと同じ |
| 消費税 切り捨て設定でも **小計の税額（ms_PartsTotalTax 等）は四捨五入のまま**、総額の税だけ切り捨て | T1: 課税小計 162,505 → 部品計税 11,411（114,105 × 0.1 四捨五入）、総額税 16,250（切り捨て） |
| 下処理面積 PrepareArea ≒ 塗装面積（面積 × 割合を切上）× 0.345 を四捨五入 | 実機 13 例が合う: J87 45 1/1 → 16、28 1/2 → 5、94 1/2 → 16、22 1/3 → 3、88 1/2 → 15、285 1/3 → 33、W66 81 1/2 → 14、88 1/1 → 30、40 1/2 → 7、68 1/2 → 12、W82 47 1/3 → 6、ZYX11 79 1/1 → 27、U52 37 1/2 → 7。**合わないのは W66 のルーフ 287 だけ**（1/2 → 48 / 式 50、1/3 → 32 / 式 33。2026-09-12 w66b_real）。J87 のルーフ 285 は式どおりなので車種（車形 6/7）か COM/T_KEI_4 の係数差と推定、未解決。S64 工場 NEO の 39 1/3 → 7 は 1/2 のときの値が残ったもの |

再検索で変わるが生成器では扱わない差（再検索の副作用）: 板金/修理 '#' 行の PartsPriceStandard −1・WageFileTime '0'、点検調整(4) 行の DisposalName/BlockCode 空、材料代割合が既定値（15% 等）に戻る（H23: 55% → 15%）、PartsPriceFlag 1 の行に標準価格が入る（C-HR 7990）、選んだ装備・複数部品選択の既定行による品番/価格差。

**吸収の一般規則（偶奇リンクはブロックをまたぐ）**: C06 の 4810 ランプユニット脱着（X30、U3 = リンク F1）が指数なしになる原因を H24/H25/H27/H29/H31 で二分し、**2700 スライドドア取替（H07）の区分 T1（wi 0、リンク F0）が有効なため**と確定（H31: 2700 取替 + 4810 脱着だけで再現。4800 板金・ランク・塗装・Rバンパ・装備では再現しない = H12/H13/H17/H20/H21/H23）。骨格ブロックの「奇数リンク X(2k+1) は偶数 X(2k) が有効なら 0」の規則が、見積中の非骨格部品が実際に使う行のリンクに対してもブロックをまたいで働く。生成器は `_active_links()` で他部品の有効リンクを集め、非骨格の吸収判定と `_frame_combination` の抑止に使う。H3 の 2310/2450（リンク E1）が 2300 取替（E1 行のリンク E0）で消えるのも同じ規則。

### 10-13. 総当たり再検証で見つかった固定値（2026-09-08 夜、実 NEO 57 本・実験 35 本）

- **PaintingPlan.CalculateLevel_Panel / Base / Bumper は常に 1**（Booth / Frame / Etcetera / TwoTone は 0）。塗装明細が 1 行も無い見積でも、内板骨格塗装がある NEO でも同じ。生成器は塗装なしのとき 0 にしていたので修正（実 NEO 57 本すべて 1/0/1/1/0/0/0）
- **現在車両に脱着(D) 変種の無い部品の脱着行**: `WorkCode` と `ConstructGroup` は空文字（スペースではない）、価格未入力なら `PartsPriceFlag` 1（cogni_frame_F2 1410）。D 変種のある部品（W66 0003）は `WorkCode` に区分・`ConstructGroup` '  '・flag 1
- 実機と生成器で残る既知の差（実害なし）: 指数の内部表現（コグニは 0.30000000000000004 のような加算結果、生成器は 0.3）、一部の行の `BlockCode` をコグニが空にすることがある（0140 フォグライト、枠を取られた 0402）
### 10-14. AnSMB.txt の桁割りと部位コードの確定（2026-09-08 夜、実機実験 K1/K2/K3 と実 NEO 601〜801 行の突き合わせ）

- **AnSMB.txt は実機保存版と 142 桁すべて一致**（実験 K1: 5 行 × 142 桁が完全一致）。確定した桁:
  - 0〜7 行番号 / 8〜11 **部品コード（無い行は空白。'0000' ではない）** / 12〜13 修理方法（手入力行は空白）
  - 14〜37 部品名（ERParts のまま） / **38〜61 標準名称（11.DB 名称欄そのまま。先頭スペースを落とさない = 実 NEO 800 行一致）**
  - 62〜79 品番 / 80〜97 標準品番 / 98〜99 数量 / **100 = ERParts.OrderFlag（§10-17。触っていない見積は全行 空白。ここに書いた「部品コードのある行は '0'」は K1 が編集後だったための誤り）**
  - 101〜103 '000'（101 = リサイクル置換、102 = 保留。§10-19） / **104〜115 = 12.DB の CutWork 欄（[65:76]）に英字があれば '1' + その欄、無ければ '0'（実 NEO 801 行一致）** / 127〜132 'F99999'
- **部位コード（BlockCode）**: 12.DB の基本版（行番号の百の位が 0）に無い部品で、車両条件の標準も引けない行はコグニが空にする。実機で確認（K1/K2/K3）:
  - J87 0140 フォグライトは 12.DB に版 1/2 の行しか無い → 生成器が A01 でも A05 でも空欄に戻る（K2）。空欄で渡すとそのまま空欄（K3）
  - 2300 ドアは版 0 にあるので、空欄で渡してもコグニが H01 を書き戻す（K3）
  - 0402 ヘッドライトユニット（版 1/2 のみ）は装備 U で **表示指数 0.4 を持つときだけ** A05。装備 U が無く相手に吸収されて指数が出ない実験（pair_P / pair_Q / pair_none）では空欄（2026-09-08 追試で確定。生成器の条件は「基本版に無い」かつ「標準が引けない、または吸収されて指数が出ない」）
  - 実 NEO 601 行のうち 574 行が この規則で一致。残りは工場が明細画面で直接打った行（ボルト・穴あけ加工・配線修理など。コグニも空欄）

### 10-19. 損傷部品（DamageParts）・保留フラグ・ADAS 索引の並び（2026-09-09 実機 5 車種 + CX1〜CXF + 実案件 2 件）

トヨタ W66 シエンタ / ダイハツ D98 タント / ホンダ L10 N-BOX・J52 N-ONE / スズキ S64 ワゴンRスマイル の 5 車種で、生成 NEO をコグニで開いてそのまま保存し全列を突き合わせた。ERParts・Painting*・Total・Car・AnSMB は初回から一致し、**DamageParts の 2 列だけが食い違った**。追加実験 CX1〜CXF で規則を確定した（生成器・`audit_cogni_files.py --full-files` に反映済み）。

**DamageParts.PartsType**（1 = 部位図の部品ではない行）:

| 条件 | PartsType | 実機の証拠 |
|---|---|---|
| 12.DB の可能作業（[52:55]）に 取替 K も 脱着 D も無い品目（修理のみ `S`／オーバーホールのみ `OH`） | 1 | cogni_CX2: 0005 `S`・2000 `S`・8705 `OH` が 1、0003 `D`・6305 `DS`・7600 `OHD`・0045 `K`・0025 `KS`・6520 `KDC`・6800 `KC`・7760 `KSC` は 0 |
| リサイクル置換行 | 1 | cogni_CX4 0025 |
| 部品コードの無い手入力行 | 1 | cogni_M1 ｼｮｰﾄﾊﾟｰﾂ |
| 保留（ReserveFlag=1）だけの行 | **0** | cogni_CX4 6800。以前「保留は 1」としていたのは SIENTA_hold の 0005 が `S` 品目だったための取り違え |

「W/S 専用の方式へ変えた行」「明細で直接入れた SIENTA_w 3800」が 1 だったのも、3800 が `S` のみの品目（Rライセンスプレート(修理)）だったためで、この規則で説明できる。

同じ部品コードが W/S 版で違う可能作業を持つときは **いちばん小さい版（通常は基本版）の行** で決まる（cogni_CXE: 三菱 C88 パジェロ 8700 は 版 0 が `OH `・版 1 が `OHD`で、脱着で入れても PartsType 1 / BlockCode 空。現行 ADDATA 300 車種の走査で、K/D の有無まで割れるのは C88 8700/8850 の 2 件だけ）。

**DamageParts.BlockCode**（ERParts.BlockCode とは別に決まる。コグニは保存時に DamageParts を作り直すが ERParts.BlockCode は書き換えない）:

- 12.DB の**全 W/S 版**から部品コード → 部位を引く。ERParts.BlockCode が空でも入る（cogni_CD98 / CX8: 0012・0071・0076 は ERParts `''` のまま DamageParts `A01`）
- ただし**基本版(0)に無く、複数の版に載っている部品**はどの版の部位か決まらないので空（cogni_CX9: J87 0070・0140 = 版 1+2 → `''`、0652 = 版 1 のみ → `A15`、0185 = 版 2 のみ → `A01`）。**ERParts.BlockCode が残っていても DamageParts は空のまま**（cogni_CXF: W66 0404/0454 ヘッドランプレンズは ERParts `A05` のまま DamageParts `''`。ERParts.BlockCode で埋め戻してはいけない）
- PartsType 1 のうち「可能作業に K も D も無い品目」と「手入力行」は空。リサイクル置換行は部位が残る（cogni_CX4 0025 = `A01`）

**AnSMB 101〜103 桁**: 101 = リサイクル置換、**102 = 保留**（cogni_CX4 6800 の `0100`）、103 は未使用（`0`）。

**AnNote.ini**: `[Reserve] Flag` = 保留行が 1 件以上あれば `1`（cogni_CX4）、`[Comment] Flag` = 明細コメント（ERParts.CommentFlag）が 1 件以上あれば `1`（cogni_CXA）。どちらも無ければ `0`。

**AnSvEm0001Ex.db `[ADASWork]`**: 空の入力行 `Idx1` は**本文の最後**に置く（cogni_CX5、実機 ADAS_D98c も Idx4→Idx5→Idx2→Idx3→Idx1 の順で Idx1 が末尾）。番号は ReserveERParts の RecordNo と一致し、本文の並びとは別。

**未検証機能の確認**（いずれも生成器と実機が全列一致）: ADAS 3 作業（cogni_CX5）、水性塗料 + 3 コートパール + バンパ塗装（cogni_CX6）、内板骨格 4 部位 + 基本修正作業（cogni_CX7）、明細コメント + 費用（部品/工賃/非課税）+ 値引 + 数量 2（cogni_CXA）、付加塗装（ワックス/サッシュ/ストライプ/シーリング/内板骨格塗装/追加項目）+ 保留 + ADAS の併用（cogni_CXB）、汎用車種（コグニ非収録車 Z10、全行手入力）（cogni_CXC）、修理方法 7 種（取替/脱着/修理/脱着修理/分解調整/点検調整/板金）と数量 12（cogni_CXD）、可能作業が版で割れる部品（cogni_CXE）、あいまいな版だが ERParts には部位が残る部品（cogni_CXF）。

実案件 2 件（オデッセイ C05 = 明細 69 行・塗装一括計上、アルファード C04 = 明細 25 行・塗装パネル・費用 3 件）を同じやり方で保存しても全列一致（cogni_R1 / cogni_R2）。検証は `python claude_neo_pipeline/tests/audit_cogni_files.py --full-files`（verify_all に常設）。

### 10-18. 複数部品選択・入力順・W/S 版・下処理面積（2026-09-09 実機 N1 / O1・O2）

- **複数部品選択ダイアログ**（品番末尾 `*` の出どころ）を実機で確認した。再検索を通すと出る:
  - 色別候補（塗装済み / 未塗装）は **車両カラーに一致する行が既定**（先頭ではない）。J87 0010 は 71101-TY0-000ZS ¥59,100 YR586P が選択済みで、04711-TY0-000ZZ ¥41,500「未塗装」が対抗
  - 仕様違い（60261-TY0-901ZZ「フエンダウインカー付車」/ 60261-TY0-010ZZ「ドアミラーウインカー付車」）は先頭が既定で、品番が無ければ人が選ぶしかない
  - 生成器は色一致を選び `*` を付けない。コグニは開いて保存しただけでは付け足さない（M1）
- **入力順で連動は変わらない**。同じ明細を見積順と逆順で 2 本作り、どちらも再検索を通して保存したところ、
  指数・工賃・WorkCode・取替合計・合計額がすべて同じだった（O1/O2、差 0 件）
- **W/S のワークシート版**は現在の装備設定に従う。装備未設定なら基本版（ハロゲン）が出て
  「ディスチャージヘッドライトは装備バリエーションの選択が必要です」と注記が出る。
  版 1/2 はフロントバンパー周りの形状違い（J87: 基本版 1,082 / 版 1 は 230 / 版 2 は 77 部品）。
  W/S 画面の装備一覧 = 10.DB の装備レター（A エンジン型式・Z 4WD を除く 7 つ）
- **下処理面積 PrepareArea** は近似式（塗装面積の切上 × 0.345 四捨五入）が実機 13 例で一致。合わないのは W66 のルーフ 287 だけ
  （2026-09-12 w66b_real: 1/2 → 48、式 50）。J87 のルーフ 285 は式どおり。車種（車形）か T_KEI_4 の係数と推定、未解決。金額には影響しない表示項目

### 10-17. AnSMB 100 桁の正体と、再検索が変えるもの（2026-09-09 実機 M1/M2/M3）

- **AnSMB 100 桁 = ERParts.OrderFlag（部品発注の状態）をそのまま書いた欄**。実 NEO 199 本 6,477 行の突き合わせで
  `'0'→'0'` 1,698 行、`''→' '` 4,743 行、`'1'/'9'/'*'` もそのまま（例外 1 行）。部品の属性からは決まらない業務データ
- **触っていない見積では全行 空白**。生成 NEO をコグニで開いてそのまま保存した実験（cogni_M1）で確認。
  K1 で `'0'` だったのは、あの実験で明細画面を編集したため
- **標準品番（80〜97 桁）は修理・板金の行にも入る**。再検索（装備バリエーション変更 → 完了）を通した保存版では
  消えるが、それは再検索の副作用。納品する NEO（再検索前）は書くのが正しい
- **板金ランク行の品番欄（62〜79 桁）は `{面積}d㎡ {ランク}`**、付加作業があれば ` 付加 {時間}`（M3 で 0.3h を確認）
- **色別部品の `(ﾄｿｳｽﾞﾐ)` は修理の行にも名称に入る**（M1 の 0010）
- AnSMB の一致検査の基準は M1/M2/M3（再検索も編集もしていない保存版）。3 本とも 142 桁完全一致

### 10-16. 明細以外の表と、部品代が付く修理方法（2026-09-09 バグハント）

- **PaintingLinkParts（塗装連動部品）は塗装をしない見積にも入る**。20.DB に載る部品の 取替(0)/修理(2)/板金(6) 行が対象
  （実機 cogni_K1: 塗装明細ゼロの 5 行の見積でも 2300 LFﾄﾞｱﾊﾟﾈﾙ が 1 行）。生成器は塗装明細があるときだけ 20.DB を読んでいたので抜けていた
- **DamageParts は明細行と 1 対 1**。部位コードが空の行（12.DB 基本版に無い部品）も手入力行も入る（cogni_K1 は 5 行の見積で 5 行）
- **部品代が付くのは取替の行と手入力行だけ**。実 NEO 211 本 5,400 行の集計: 取替 5,354 / 手入力（修理方法なし）47 / 脱着 2、
  修理・板金・点検調整・分解調整は 1 例も無い。板金行に部品代がある見積は写し間違いとみてよい
- 生成 NEO に入る XML は中立な `ESTIMATE.xml`（雛形を差し替え済み。2026-09-09）。コグニは開くときに気にせず、保存時に NEO と同じ名前に付け替える

### 10-15. 同じ部品コードの重複行と ConstructGroup（2026-09-08 追試、実機 38 本総当たり）

- **同じ部品コード（ref）の明細を 2 行以上持つ NEO はコグニでもそのまま保たれる**。工場の実 NEO 36 本にも、
  修理方法違い（12081431 の脱着＋修理 9 組）と同じ修理方法（12051345 の取替 3 行）の両方が実在する。
  実験 H24（生成 NEO をコグニで再検索して保存）でも 40 行が 40 行のまま残った。
  当初「2 行目が捨てられる」と見えたのは、突合せスクリプトが部品コードだけで行を対応づけていたための誤り（2026-09-08 訂正）。
  ただし**再検索は ADDATA に無い品番を書き換える**: H24 の 2971（印字 91560-S9A-A01、11.DB に無い）は
  11.DB の品番 91560-SZW-003 に置き換わり、単価が手入力 `*`（140 円）になった。生成器は印字品番をそのまま書く（再検索前の形）。
- **ConstructGroup**: 修理（DisposalCode 2）は標準が引けない手入力指数 '#' 行でも `'  '`（実験 H11 の 0010 で確認）。空文字になるのは、修理以外で標準が引けない '#' 行と、標準の無い分解調整
- 実機の Time / TimeStandard は 0.1 の足し込みで `0.30000000000000004` のような尾を持つ。印字も入力も同じ値なので、総当たり比較では小数同士 1e-9 未満の差を一致とみなす
- この 2 点を直した結果、**実機 NEO 38 本すべてが全列一致**（`claude_neo_pipeline/tests/audit_cogni_files.py`、verify_all に常設）。H24 だけは入力と構造が違う実験として既知差に分離

- **AnSMB.txt は明細行と同数**（60 行で打ち切っていたのを修正）。コグニ標準サンプル 04011141 は 267 行 / 267 行、工場 NEO 12051345 は 97/97、再検索保存版は 66/66・74/74
- AnSMB の 104 桁目と 38 桁からの標準名称は **10-14 で同定済み**。100 桁目は **ERParts.OrderFlag そのもの**（§10-17 で確定）。標準品番（80〜97 桁）は **修理方法によらず入る**（§10-17。再検索を通した保存版では消えるので、以前「取替の行だけ」と書いたのは誤り）
  （104 桁 = 12.DB CutWork 欄に英字があれば '1' + その欄。実 NEO で '1KS' が 1 行だけ立つのは、その車種でこの欄に英字を持つ部品が 1 つしか使われていないため）
- 再検索の副作用（生成器は W/S 経路の工場 NEO を正とする）: 脱着・板金・修理行の標準品番/標準価格/取替合計、`WageFileTime` / `PartsFileTime` の '0'、点検調整行の名称・ブロック、材料代割合の既定戻り、板金/修理 '#' 行の `PartsPriceFlag`

### 10-5. その他の画面
- 情報: 見積ファイル情報（作成日・受付番号・グループキー・入庫日・出庫日・備考 3 行）/ 顧客情報（住所コード→住所、名前 3 行、電話・FAX、使用者・所有者、登録番号 4 分割、車種検索キー、車台番号、有効期限、走行キロ）/ 自社情報（自社工場名 7 行）/ 保険関連情報（証券番号・契約者・代理店・事故日・立会日・協定日・修理日数・時価額・アジャスター・協定者）。画像一覧・進捗・メモ ボタン
- 総合計: 部品/工賃の値引・割増（金額と率、＋/−、値引にリサイクル部品代・塗装材料代を含む/含まない、計算単位 1/10/100 円、四捨五入/切り捨て）、小計・消費税・その他非課税額・その他 2 行・見積金額・保留登録
- 画像: 小さな「画像」ウィンドウ（一覧/編集/前/次/小/大/縮/明）。未使用なら DamageImage は空
- その他 → 工賃単価変更（基本単価・工賃単位。変更で全行の工賃を再計算）

## 9. コグニセブンの機能と出力（2026-09-04 夕方に実機で確認）

| 機能 | 入口 | 保存先 |
|---|---|---|
| 見積結果・情報・部位・W/S（ワークシート）・内骨・再設定(ADAS)・塗装・作業項目確認・明細・費用・総合計・画像・その他・リサイクル | AudaMain ツールバー | AnSvEm / AnSvIf / AnSvIg |
| 帳票の出力（印刷ダイアログ）: 見積り1/2/3・見積り1/2(塗装明細)・指示書・指示書(塗装明細)・取替部品1/2・塗装明細1/2・報告書・確認書・見積書表紙・見積書表紙2(計算書)、ページ/シリアルプリンタ、印刷・PDF(通常)・ファイル出力・メール送信、タイトル/デザイン変更（`AudaData/Const/PrintSheetDesign/*.xml`、`AnPrintSheet.ini`）、出力項目（住所・氏名・登録番号・車台番号・電話・自社画像）、印刷範囲・部数 | 印刷ボタン | PDF は libhpdf、メールは AnSvMail.ini/AnSvImge.ini（ネオメール連携） |
| ADAS（運転支援システム再設定・調整）: `<car>55.DB`（基本作業 A010）/ `56.DB`（センサ別 A100〜）。55/56.DB を持つ 81 車種（evidence/car_file_counts.json）で「再設定」が有効（§10-6） | 再設定ボタン | ReserveERParts + AnSvEm0001Ex.db `[ADASWork]` + Total.ms_WageTotal |
| 作業項目確認（関連作業の提示 = `<car>89.DB` 連動作業。見積中の取替(K)/脱着(D) 行ごとに前提脱着部品を列挙し、未計上の部品を「脱着/取替/修理/脱着修理」で提案。N-ONE: 1410K/1500K の行から ヘッドライトユニット・ボンネット・ボンネットヒンジ・カウルトップ・Fサスペンション） | 作業項目確認ボタン / その他 | ERParts |
| リサイクル部品登録 | その他 → リサイクル部品登録（見積を分けて保存するか確認） | RCParts / RCLinkParts（§4） |
| 車種変更・装備バリエーション変更・車名エンジン名変更・工賃単価変更・消費税設定・ホームページリンク・ワークシート印刷（AxPrtWS.exe、`<car>WS.CAB` の TIF） | その他ダイアログ | AnSvIf |
| 既存見積一覧（AxFlLst.exe、`AnOption.ini [EstimatedList] CurrentFolder=C:\AdSeven`）: 管理領域 §1-1 のサマリで一覧表示 | メニュー | — |
| 見積集計（AxCustFlSum.exe、`AnUsrTblSU.sld` NeoHeader/NeoTotal…、PDF/HTML/JPG/BMP/TXT 出力） | メニュー | AudaData |
| 住所検索（AxZipSrc.dll、`ADDR/ADRA*.CAB` = 暗号化 SQLite、`AnOption.ini [AddressSearch]`） | 見積情報 | Customer.AddressCode |

ユーザーデータ（`Auda7/AudaData`）: `AnOption.ini`（消費税・一覧フォルダ・画面設定）、`AnUsrTbl.sld`（顧客/自社/備考/リサイクル名/コメント/追加部品・パネル）、`AnUsrTblPnt.sld`（材料代割合 Paint×Coat×HF）、`AnUsrTblSU.sld`（集計）、`AnUd*.ini`（陸運支局・市町村・協定工場・アジャスター等の入力履歴）、`AnUds.ini`（帳票タイトル）、`Const/AnDefine.ini`（修理方法コード・費用行名・帳票定義）、`Const/AnEra.ini`（元号）。板金ランク（ERParts.DamageArea/DamageRank/SATime、§4「板金ランク入力」）は明細の板金(6) 行の指数セルで開くダイアログで入力する。W/S の下段には「板金上限指数 = 部品価格 ÷ 単価」が表示される。

## 8. 追加検証項目の状態（「確定」と明記した項目は生成器が使う。未確定の項目だけ現行ワークフローに影響なし）
- 15.DB の工賃区分の選択規則は ADDATA 仕様 §11-7（`cogni_standard()`）、骨格ブロックの組合せは §11-7c（`_frame_combination()`。2026-09-06 コグニ実機 J52 の画面読取 23 通り + 保存版 FRAME_p7/p8/p9 で同定（unit_frame 29 ケース）、他工場のコグニ生成 NEO 10 本の標準行 77/77 を再現、FRAME_p7/p8/p9 との ERParts 285 セル一致）。「重複部品コードチェック」の親子表は 12.DB のレベル欄（ADDATA 仕様 §9-2 / §11-7c 規則 6。J52/W66/D88 の 3 車種 11 組でダイアログの有無を画面で確認（画面観察のみ、保存 NEO は DUP_W66b の 1410/1420 だけ = 証拠パック外）、生成器は report の duplicate_parts に警告）
- （確定済み・参考）ADAS 作業（55/56.DB）: §10-6 で確定（ReserveERParts＋Ex.db、工賃計に加算）。「再設定」ボタンは 55/56.DB を持つ車種でだけ有効（J52 N-ONE には無いので無効だった）
- 高機能塗装のパネル別加算は COM/F_S.DB の式に置換済み（ADDATA §11-6。経験式 floor10(0.3+0.01×面積) は予備）。（旧記述: DLL の TRC_XXX87 に TimePaintScrach があり、87.DB 収録車では直接値がある）
- ERParts.ChangeTotal の標準工賃はコグニが 15.DB の値で再計算する（PDF 指数と異なる場合 1 行程度ずれる。表示のみ）
### 10-20. 人が W90（ボディ 20）を最初から手入力した保存版との全列比較（2026-09-12、NEO_check/_eva_exp/cogni_W90.neo。記録 .claude/skills/pdf-to-neo/reference/experiments/2026-09-12_W90_実機手入力.md）

明細 2700 取替（品番ダイアログ 4 候補から 67004-26620）・4800 取替（61612-26560）、塗装ページを開き 4801 をパネル追加、装備は未選択、基本単価 97,000。
生成器を同じ入力で走らせて `neo_diff` した結果、次を直して**金額に関わる差 0** になった:

| 列 | 実機 | 直す前の生成 | 規則 |
|---|---|---|---|
| `PaintingLinkParts` | 2700 のみ | 2700・4800 | この車のボディに載る 20.DB 行がある部品だけ（`panel_for_body`） |
| `ERParts.Time`（4800 取替） | -1（標準なし） | 8.1 | 15.DB ホスト 4600 の J3/Q4 は 共通行 sub=4800 / ボディ 20 行 sub=4801。ボディ行が sub 不一致なら共通行へ逃げない（装備不一致なら逃げる: 2700 は 1.9） |
| `ERParts.WorkCode`（4800） | 'J3Q4' | '' | 標準が無くても 11.DB の区分レターは入る（実機 25 本で 47 行） |
| `PaintingPlan.BaseWageByManual` | '*' | '' | パネル追加（AddedFrom 1）が 1 枚でもあれば '*'（cogni_P1 も同じ） |
| `CarEVA` | 空 | Z | FVA 'ZA'（4WD）の車は装備一覧に４ＷＤが出ず、CarEVA に Z は入らない |

残った差（金額に無関係・状態依存・未解決）: `ERParts.PartsNo` 末尾 `*` と `OrderFlag='*'`（複数候補ダイアログで選んだ直後。実機 25 本の末尾 `*` 8 行は `OrderFlag='1'`）、
`OrderFlag='0'`（明細画面から入れた行。生成器由来は ''）、~~`ChangeTotal`（4800）= 44,900 + 9.7h×単価~~（**同日に再現**: 表示の Time は -1 のまま、ボディ 20 行 sub 4801 の標準工賃を取替合計に入れる。§10-3-2。未解決で残るのは W90b の 4600 連動加算 +7.7h）、
`SortNo` 7、`pt_ExtraFlag` 0（追加項目タブ未オープン。実機 68/69 本は 1）、`CarNameByUser` 末尾の全角空白なし（実機 68/69 本はあり）。

- コグニ操作中に一部行の工賃が消える現象（板金／取替行、実験時 7 行）は再現条件未特定。ファイル書式には無関係
- COM の係数表はすべて XOR 0xff の CSV として復号済み（ADDATA §11-8）。ロッカ（サイドシル、PanelDivision 2）の塗り数値は CHM 表を正とする: SIRU.DB（車形×塗料×…×区分 1-4、K/S）との対応は未同定（同じ車形・面積 20 で 1.1 と 1.4 の車があり、区分の出所が不明）。CHM の無い車種のロッカは手入力 `#` にする
- （確定済み）KA81.DB 本体レコードの x,y = 生産期間（月シリアル、id の 1 枠前の値。ADDATA 仕様 §2、evidence/KA81_period.json、resolver Candidate.period）。未確定は表1 ヘッダ第 2 値 1945 だけ
- 17.DB の from/to = IMG.CAB の図番号範囲（ADDATA 仕様 §12、証拠 IMG_CAB_listing.txt）。20.DB は塗装パネルマスタ。13.DB = 83.DB の無い 672 車種の色別・期間別・仕様別部品表（ADDATA 仕様 §11-9b、COLOR_D98.neo 2026-09-06 夜）: 選んだ 11.DB 変種（ボディ条件 [7] 込み）の色別フラグが 1 なら車両カラーの同語幹行の品番・価格・'(ﾄｿｳｽﾞﾐ)' 名称が ERParts に入り、期間・仕様違いが複数ならダイアログで選んだ行の品番・価格が PartsNo/PartsNoStandard/PartsPriceStandardOutTax に入る。工場 NEO の `PartsNo '…-C0   *'` はその複数候補の印（PartsNoStandard に '*' は無い。発生条件はデータ版依存で未確定、生成器は付けない）
