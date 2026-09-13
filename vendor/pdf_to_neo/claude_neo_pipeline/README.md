# claude_neo_pipeline — 工場見積 → ADDATA 車両特定 → コグニセブン同等 NEO

Claude が手元で「見積 PDF の内容 → 車検証で車両を確定 → ADDATA から部品・工賃・装備・カラー・塗装指数を逆引き → NEO 生成」を実演するためのモジュール群。
Streamlit 版 app.py とは独立（app.py は import しない）。仕様の根拠は `../ADDATA_REVERSE_LOOKUP_SPEC.md`（§11 塗装指数）と `../NEO_FILE_SPEC_COMPLETE.md`。

## ファイル

| ファイル | 役割 |
|---|---|
| `addata_vehicle_resolver.py` | 車検証項目（型式・車台番号・型式指定・類別・初度登録・カラー）→ NEO Car/CarSearch/CarEVA の値。KA06_ALL / KA81 / 01・05・07・08・09・10・25・26・66.DB / KATB010・021・030・040 / Katashiki.DB |
| `estimate_to_neo.py` | 見積の構造化データ → ERParts（11/12/15/17.DB 照合・指数・レバーレート逆算・装備推定）→ 塗装（パネル別 or 一括）→ 費用 → NEO。`load_human_csv()` はサンプル CSV（4 種類の工場書式）用ローダ |
| `paint_index.py` | 塗装指数の再現: 20.DB（パネルマスタ）、車種 CHM「補修塗装指数」表（塗り数値）、COM T_KEI_3（加算基礎数値）/ BOOTH（ブース）、23.DB（バンパ） |
| `run_case.py` | `estimate.json`（Claude が PDF を読んで起こした構造化見積）→ NEO。実案件用の入口 |
| `neo_header.py` | NEO 先頭 424B 管理領域（既存見積一覧のサマリ: 相手工場・顧客・車名・作成日・5 金額・登録番号・保存日時・ライセンス ID）の復号／生成。XOR+ビットシフト |
| `neo_container.py` | NEO コンテナの展開・再圧縮（app.py から純関数を切り出し）。Windows 互換 cp932（IBM 拡張漢字）コーデック `cp932w` を登録 |
| `reference/neo_04011103_reference.json` | 実 NEO の全テーブル（スキーマ＋代表行）。**開発機のみ**（git・配布 zip には入れない。無くても生成には不要） |
| `reference/Katashiki.DB`, `T_KEI_3.DB`, `BOOTH.DB`, `HYOJI3.DB`, `DATAUP.DB` | COM.CAB から展開したマスタ（DATAUP.DB = 車種データ更新年月 → Car.WorkCodeUpdateDate） |
| `out/` | 生成 NEO |

## 使い方

（Git Bash 形式。PowerShell では `$env:PYTHONIOENCODING = 'utf-8'` を設定してから、先頭の `PYTHONIOENCODING=utf-8 ` を外して実行）

```bash
cd files
# 通常の入口（スキル pdf-to-neo）: 案件フォルダの reading.json → estimate.json → NEO → 検算 を 1 コマンドで
PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/make_neo.py "<NEO_CHECK_ROOT>/<案件>" --name "<顧客>_<車名>"
# 生成器を直接（estimate.json を手で書いたとき）
PYTHONIOENCODING=utf-8 python claude_neo_pipeline/run_case.py "<NEO_CHECK_ROOT>/<案件>/estimate.json" "<NEO_CHECK_ROOT>/<案件>/out.neo"
```
（旧 CSV 入口 `estimate_to_neo.py <csv>` は開発初期のもの。サンプル CSV は git に入れていない）

Python から:

```python
import sys; sys.path.insert(0, 'claude_neo_pipeline'); sys.path.insert(0, '.claude/skills/pdf-to-neo/scripts')
import skill_env; skill_env.apply()             # この PC の設定（env_check --save）から ADDATA の場所を解決
from addata_vehicle_resolver import AddataVehicleResolver
r = AddataVehicleResolver()
r.resolve(model_code='ZYX11', serial_no='ZYX11-0000001', desig='19417', category='0005',
          reg_date='R5.3', color_code='224')    # → confidence / neo_car / candidates / evidence（車台番号はダミー）

from claude_neo_pipeline.estimate_to_neo import NeoBuilder
neo_bytes, report = NeoBuilder().build(est, est['vehicle'], labor_rate=8750, insurance=est['insurance'])

from claude_neo_pipeline.paint_index import PaintIndex
pi = PaintIndex(os.environ['ADDATA_ROOT'], 'W69')   # skill_env.apply() 後
pi.standard_times('1000', hf=2, n_panels=4)     # → new/s1/s2/s3/hf（コグニ PaintingPanel と同値）
pi.base_time('6', paint=3, coat=3, hf=2, n_panels=4)  # → 3.7
```

### estimate.json の形（run_case.py）

`vehicle`（車検証）/ `customer` / `insurance` / `labor_rate` / `items`（code, name, parts_no, method, qty, price, wage, index）/
`paint`（total, material, material_rate, paint, coat, hf, panels[{code, name, method, area, ratio, index, wage}], booth, base, bumper_front{method, color, form, index, wage}, wax{count, index, wage}）/
`expenses`（name, amount, kind, taxfree）/ `totals`（照合用）

非収録車: `vehicle.generic=true` + `car_code`（Z10 乗用車/Z20 1BOX/Z30 トラック）+ `maker_code` + `car_name` + `engine` + `color_code`。`items[].manual=true` で ADDATA 照合をせず見積書の名称を使う。

任意（コグニ実機で書式確定済み）: `items[].comment`（明細コメント）、`items[].reserve: true`（保留部品。金額は合計に入らず「保留」行として印字）、`items[].recycle{name, price, stock_price}`（リサイクル部品に置換。純正部品の工賃は消える）、
`index_policy`（'manual' = 見積書の指数をすべて手入力指数 `#` にする。非コグニ書式の工場見積で必須）、`totals.neo_total` / `totals.tolerance`（コグニ丸めで見積書と差が出る案件の合格条件: 期待合計と項目差分の許容円）、`items[].bankin{area, yes:[1,0,0], fuka}`（板金ランク）、`items[].wage` は 0 = 工賃なし（付属部品）、null/省略 = 不明（取替/脱着行はコグニの標準指数で補完）、`discount{parts, wage}`（+ 割増 / − 値引、円）、`frame{basic, items[{code, rank A|B|C, index, wage}]}`（内板骨格修正: 基本修正作業 + 部位区分）、
`paint.frame{engine_room 1-3, front_pillar 1-2, center_pillar 1-2, rear_floor 1-2}`（内板骨格塗装）、`paint.sealing{m}`（ボデーシーリング）、`paint.other[{name, index, wage}]`（追加項目: 内板調色 等）、
`paint.bumper_front / bumper_rear{method 新品|変形修正|外傷修正小|外傷修正大, color 一色|二色, draft true（絞模様 +0.4）, index, wage}`（23.DB の標準を補完）、
`paint.door_sash{count}`（ドアサッシュ黒塗り）、`paint.stripe{count}`（ボデーストライプ）、`paint.low_cover{roof なし|取替|修理, change n, repair n}`（低隠蔽性塗色）、`paint.two_coat_solid{roof true, count n}`（2コートソリッド）、`paint.two_tone{coat 下部塗膜, count, material_rate}`（2トーン加算。PaintingPlan の TwoTone* も書く）— いずれも index/wage を省略すれば COM 表（fukaetc/2TONE）の標準値、2026-09-05 コグニ保存版と一致

## 検証結果（2026-09-04 時点の記録 —— **履歴**。現行の検証状態は `README.md` §4 と `HANDOFF.md` §5-1、案件コードは `NEO_check/_cases.json` が正）

- 車両特定: 実 NEO 9 件＋PDF 2 件 → Car テーブル 13 項目 × 11 台 = 117/119 一致（残 2 は「ﾂ/ｯ」表記差）
- 車検証だけの特定: 登録番号は伏せる（A200A / 19403 / 0005）→ ライズ A200 Z 1000（W64・グレード Z・1KR-VET 2WD）confirmed
- サンプル 4 件（N-BOX / エスクァイア / フリード / ジムニー）: 合計一致（フリードのみ税込→税抜換算の +2 円）
- **実案件 N-ONE（案件 N-ONE、ディーラー概算見積 FAX 4 ページ。コグニ収録車だが非コグニ書式）**: 速報 PDF の車検証で J52 N ONE JG1 PREMIUM confirmed。FAX の Honda 純正品番（`71101T4GN00ZF` 等、ハイフン無し）を 11.DB の品番で照合し 42/43 行がコード確定（残 1 は O リング手入力）、部品価格は 42 行すべて ADDATA 標準価格と一致。日産系見積の「取替/部品/脱着/修理」区分をコグニの 取替(0)/脱着(1)/修理(2) に写像、骨格 6 行は明細の修理指数、ぼかし塗装の左 Fr ドアはコメント行。塗装は 左Frフェンダ新品 1.20（コグニ標準 1.3 と異なるため手入力指数 `#`）＋内板骨格 ラジエータサポート両側＋Fフェンダエプロン両側 1.90（NAIKOKUA 車形 7 の標準と一致）＋加算基礎 2.80（T_KEI_3 と一致）＋ブース 0.50、材料 29%。合計 693,732 円（FAX 693,740 円。差 −8 円は FAX が 8,610×指数を円未満まで計上するのに対しコグニは 10 円丸め）。コグニ印刷 2 ページ、部品計 416,885 は FAX と一致
- **実案件 ボルボ V40（案件 C03、ディーラー概算見積 2 ページ。コグニ非収録車）**: 汎用車種 Z10（トヨタ 汎用 乗用車）で生成、車名・エンジン・カラー 452 手入力、13 行（コード付き 5・手入力 8）、塗装一括 200,000、合計 1,060,884 円。コグニ印刷 2 ページ一致
- **実案件 シエンタ（案件 C02、工場の概算見積 FAX 5 ページ、143 行）**: 塗装一括 191,360、内板骨格修正（基本＋ランク B/A/A）、保留 2 件、費用 5 種を再現し合計 1,498,827 円。コグニ印刷 5 ページ、ページ小計は費用行の掲載ページ差以外一致
- **実案件 C-HR（案件 C01、工場見積 3 ページ）**: 74/74 行照合、塗装パネル 4 枚・加算基礎 3.70・F バンパ・防錆ワックス・材料代 31% を指数入力で再現、合計 1,424,676 円。コグニ印刷 3 ページのページ小計（113,320/35,010・376,130/188,160・275,250/307,290）まで工場見積と完全一致

## コグニセブン実機検証

- `AudaMenu.exe "<neo>"` で生成 NEO を直接起動（既存見積一覧は件数過多で固まるので使わない）。見積画面は AudaMain.exe（`request_access` には実行ファイルのフルパス）
- 見積結果・情報・明細・塗装（外板パネル／バンパ／付加塗装／塗装計）・費用・総合計の全画面が生成値と一致。PDF(通常) 出力、名前を付けて保存 → 保存版との差分は PaintingPanel.SortNo（内部カウンタ）と 24 バイト切詰の半端バイトのみ
- 保存版から取り込んだ規約: 部品税額は行ごと切捨の合計、AnSMB 13 桁目 = DisposalCode・104〜115 桁 = 板金ランク、Expense.Name 20 バイト、TEXT(n) は cp932 n バイト、ERParts 工賃は偶数丸め・塗装は四捨五入、ReportTitle は Unicode 順、PartsPlan/ReserveERParts 既定行、AnSvMail.ini 書換、66.DB 先頭桁 = 塗膜区分

## 塗装指数の出所（§11 参照）

| 項目 | 出所 |
|---|---|
| パネル面積・名称・区分・ボタン番号 | `<car>20.DB`（39B レコード） |
| 塗り数値（取替 複数塗/単体塗、修正 1/1・1/2・1/3） | `<car>00LTB.CHM` の「補修塗装指数（溶剤系）」表（hh.exe -decompile、`%LOCALAPPDATA%\claude_neo_pipeline\chm` にキャッシュ） |
| 高機能塗装（フッ素/耐スリ傷）加算 | `COM/F_S.DB` の式 round1((B+A×面積/1000)×216/100000)（実 NEO 4 パネル一致。無ければ経験式 floor10(0.3+0.01×面積)） |
| 標準工賃指数（工賃欄の無い見積の取替/脱着行） | 11.DB 変種行（修理方法・年式群・グレード/FVA/EVA・15.DB 区分レター列）× 15.DB。連動加算（相手部品の区分、両側）込みでコグニ生成 NEO の標準行 67/75 一致（ADDATA §11-7） |
| CHM に無いパネルの塗り数値 | `COM/T_KEI_1・T_KEI_2・H_N_SSZ・HJU_SS` の式（ADDATA §11-6。CHM 表 8 車種で 100% 一致） |
| 加算基礎数値 | `COM/T_KEI_3.DB`（車形, 塗料, 塗膜, B/F/S/T, 枚数） |
| ブース加算 | `COM/BOOTH.DB` |
| 樹脂バンパ | `<car>23.DB`（溶剤）/ `93.DB`（水性） |
| 下処理面積 | 近似式 四捨五入(切上(面積×割合)×0.345)。実機 13 例で一致、W66 ルーフ 287 だけ合わない（表示のみ・既知差 W66y。NEO 仕様書 §10-3） |

## コグニ実験保存版との突合（2026-09-04 夕方）

`estimate_exp.json`（上記の任意キーをすべて使用）から生成した NEO と、コグニで同じ操作をして別名保存した `CHR_ETO_exp.neo` を比較: RCParts / RCLinkParts / Frame / FramePlan / PaintingFrame / PaintingEtcetera / PaintingOther / Expense / AnSMB が完全一致。残差はコグニ操作中に消えた工賃（書式無関係）と ChangeTotal（表示のみ）。

## 非コグニ書式の工場見積（N-ONE 案件で確定した規約）

- 工賃は工場書式の円未満計上（8,610×1.3=11,193）をそのまま入れず、`wage` にコグニ丸め（偶数丸め 10 円）を入れる。そのままだと ERParts は `*` 付きで保持されるが総合計は FAX と一致しコグニ形式ではなくなる
- 塗装パネルの指数が標準値と異なるときは `PaintingPanel.Manual=1, WageByManual='#', AddedFrom=0`（コグニで指数セルを手入力して保存した版と一致）。`Manual=0`/`'*'` だとコグニは塗装タブ表示時に標準値へ戻す
- ブース／加算基礎の工賃は標準（四捨五入 10 円）と同額なら `*ByManual=''`。`PaintingTotal.TimeTotal` はブース指数を含まない（1.2+2.8+1.9=5.9）
- 総合計の消費税は四捨五入（630,665 → 63,067）。`ERParts.Comment1` は TEXT(40)、`Insurance.ConsultantFactory` は 30 バイト

## 精度向上の解析結果（2026-09-04 夜、亮平さん指示の 6 項目）—— **履歴**。現状は HANDOFF.md と NEO 仕様書 §10 が正（ADAS・連動加算・骨格の組合せは実装・実機検証済み）

1. **過去 NEO の大量回帰**: Z ドライブに NEO の保管は無く（見積は速報 PDF のみ）、コグニ本体の既存見積 DB もこの PC には見当たらない（AudaData/AnUsrTblSU.sld は集計ヘッダのみ）。手元の実 NEO は サンプル 2 件＋実験保存版で、これらとの突合は完了
2. **板金ランク → 標準指数**: コグニの板金ダイアログを実機で確認し、COM/BANKIN.DB（面積 1〜40 dm² × ランク A/B/C）と BAN_FUKA.DB（部品別付加作業）で完全再現（`items[].bankin`）。ランク判定は YES 3 つ=A / 0=C / 他=B
3. **COM.CAB の係数表**: RTTI 列名を回収（Scrach/F_S: CarForm, Paint, HF, PanelDivision, PanelTypeDivision, (PanelCode,) CoefficientA/B; SIRU: 低隠蔽性 Time1/Time2/Material; HJU_SS/H_N_Ssz: 下処理; TwoTone: AddTime1..5; Fbanpa; T_KEI_1/2/4）。耐スリ傷/フッ素/低隠蔽性/2 トーンをコグニ実機で保存して書式を確定（NEO_FILE_SPEC §4「塗装条件の派生」）。材料代割合の既定値は AudaData/AnUsrTblPnt.sld と判明
4. **ADAS（55/56.DB）**: 全 81 車種のパーサ（scratchpad `adas/adas_db.py`）と作業コード表（A010 基本 / A100 周囲カメラ / A110 ソナー / A120 前方カメラ / A130 ミリ波 / A135 前側方 / A140 後側方）。NEO 保存先は ReserveERParts（DLL）だが列割付は実 NEO が無く未確定。部品→ADAS 作業の自動リンクはデータに無い（手動選択設計）
5. **名称の表記ゆれ辞書と検算**: `AddataParts.ALIASES`（マウンティング→マウント、トリム→ライニング、コアサポート→バルクヘッド等）、12.DB 左右ペアで右側 ref を返す、部位文脈（直前行のブロック）で同名候補を選ぶ、品番不一致・価格不整合で棄却。名称だけの照合テスト 64 件: 76% → 98%（scratchpad `alias/alias_test.py`）。run_case に部品計/工賃計/塗装計/費用計の項目別検算を追加
7. **標準工賃指数の選択規則**（追加解析）: 11.DB 変種行（修理方法・年式群・グレード/FVA/EVA フラグ・15.DB 区分レター列）× 15.DB で単一部品の標準を再現（コグニ生成 NEO 75 行中 58）。`AddataParts.cogni_standard()`。工賃欄の無い見積では取替/脱着行を標準で埋め、標準どおりの行は WorkCode を揃える。連動・組合せの合算（バンパビーム、両側、割増）は未対応
6. **コグニ実機の未確定事項**: 手入力指数は `#`（TimeStandard 0）、板金ランクは `@`、コグニの再評価で `''` 行は標準区分へ置換・消失する（工賃消失現象の正体）→ 非コグニ書式は `index_policy: manual`。ブース/加算基礎は標準同額なら `''`。残: 15.DB 区分の装備条件ルール、`$` の意味、ADAS 画面（この PC では未表示）

## 実機検証の追加（2026-09-05、N-ONE / ボルボ）

- 点検調整(4)・分解調整(5): 標準指数なし（TimeStandard 0 / WageStandard 0）、分解調整は部品価格も消える。生成器を同形に修正
- バンパ塗装: 修理方式コード（1/2/4/5）・絞模様 +0.4・塗膜クラス切替を実機で確認し `BUMPER_DISPOSAL` / `BUMPER_DRAFT_ADD` を実装
- 付加塗装: ドアサッシュ・ストライプ・シーリング・ワックス・低隠蔽性・2コートソリッド・2トーン加算の指数を実機で読み取り、COM/fukaetc.DB・2TONE.DB と対応付けて実装。追加要素付き estimate で生成した NEO の PaintingBumper/PaintingEtcetera/PaintingPlan がコグニ保存版 NONE_bp4.neo と一致
- 汎用車（Z10）は塗装指数なし（塗装条件タブのみ）。作業項目確認 = 89.DB 連動作業。ADAS 画面はこの版に無い
- COM 係数表を全復号（XOR 0xff）。ADDATA_REVERSE_LOOKUP_SPEC §11-8
- 入力検証: 付加塗装の枚数・`paint.paint` コード（1/3/4 か名称）・バンパ method・低隠蔽性 roof は範囲外を ValueError、参照表（fukaetc/2TONE/23・93.DB）の欠落や 0 埋めも例外。水性（paint 4）はバンパ 93.DB・2トーン W2TONE.DB。詳細専用キー（bumper_*/wax/door_sash/stripe/low_cover/two_coat_solid/two_tone）は `paint.panels` がある見積でだけ書ける。Codex レビュー 17 周で合格

## 実機検証の追加（2026-09-05 夕方、コグニで新規見積を作成）

- 新規見積ウィザード（車検証から検索 → 装備・カラー）、部位画面、W/S（電子ワークシート）からの部品追加（10 方式）、帳票 11 種の PDF 出力、情報・総合計・画像画面を操作し NEO の書式を確認（NEO 仕様 §10）
- 生成器に反映: 色別部品（83.DB）の品番・価格・名称、ConstructGroup（11.DB）、PartsName の生成規則（11.DB 名称欄）、脱着板金(3) の連動加算、材料代既定値の丸め（10 円四捨五入）。N-ONE 保存版と ERParts の名称・品番・ConstructGroup が一致

- ADAS（運転支援システム再設定・調整）: `adas: [{code: '9900'}, {item: 'A120', sub: '1'}, ...]`（指数・名称は `<car>55/56.DB`、`index`/`wage`/`comment` で上書き）→ ReserveERParts・Ex.db [ADASWork]・工賃計。シエンタ W66 のコグニ保存版と一致（2026-09-05 夜）

- 塗料 `paint.paint: 4`（水性）のときは CHM の水性ページ（車種別補修塗装指数（水性））で塗り数値を引く。W66 実機と一致（2026-09-05）

- 型式指定・類別が無い見積: resolver が 01.DB のグレード一覧を候補にし、`AddataParts.infer_from_parts()` の品番ヒント（グレード記号・年式群）で絞る（補助。車検証が揃う場合は不要）

## 精度の測り方（2026-09-06）

コグニが生成した実 NEO を正解にしたラウンドトリップ・ハーネス `scratchpad/roundtrip.py`（`python roundtrip.py --json out.json`、環境変数 `NEO_PIPELINE` / `NEO_FILES_ROOT` / `NEO_CHECK_ROOT`）。ERParts の名称・品番・価格・数量から ref を再現する率（品番あり／品番なし／名称のみ）、車検証項目からの車両特定、標準指数の再現率を出す。2026-09-06: 10 本 722 行で 品番あり 714・品番なし 693・名称のみ 689、車両特定 10/10、標準指数 73/77。生成（estimate.json → NEO）は 1 案件 0.3 秒。

## 既知の制限
- 品番も部品コードも無い見積（ジムニー等）は名称近似照合なので精度が落ちる。PDF のグレード名・型式指定をヒントに渡すこと
- 工場見積に塗装明細が無い（一式だけ）場合: `paint.auto_panels: true` で明細の取替・板金・修理行から塗装パネルを起こし、差を材料代で埋めてコグニと同じ形にできる（判断規則 10-15。材料代が印字されている・数量 2 以上・材料代が 10〜90% に収まらない案件は一括計上「塗装費用(工場見積)」1 行に戻す）
- ADAS は 55/56.DB を持つ 81 車種でだけ使える（他の車種ではコグニの「再設定」自体が無効）。24.DB の自動提案は UI の候補表示であり生成器は明示指定した作業だけ書く
- ロッカ（サイドシル）の塗り数値は CHM 表のみ（式は無し）。CHM の無い車種では `#` 手入力にする

## 人が書く JSON の真偽値欄（2026-09-11）

`manual` / `reserve` / `generic` / `taxfree` / `auto_panels` / `four_wd` / `basic` / `draft` などは
**人が書く**ので、`"false"` や `"0"`、`"しない"`、空白だけ、といった値が入る。
`if d.get('generic'):` だと空でない文字列はすべて真になり、実在車種を汎用車種で作ってしまう。

- 読み取りは `_flag()`（生成器）/ `skill_env.flag()`（スキル側）を使う。判断できない値は **ValueError**
  （黙って既定に落とすと、人は書き損じに気づけない）
- **箇所ごとに直さない**。行や estimate を読み込んだ**入口で 1 回だけ正規化**し、以降は正規化済みの値を見る
  （`skill_env.normalise_flags(rows)`、生成器は `build_rows` のループ入口）
- 車種特定のヒント（`four_wd` / `hybrid` / `candidate.four_wd`）が空・空白のときは **キーごと落とす**。
  後段は「キーがある = 指定あり」と見るので、`False` にすると 2WD 指定になって 4WD 候補を落とす
- `recycle` は真偽値ではなくリサイクル部品の情報（dict）。正規化の対象に入れない

## 失敗しても続ける箇所の扱い（2026-09-11）

ADDATA の一部（11.DB・20.DB・13/83.DB）は車種によって無いことがあるので、読めなくても生成は続ける。
ただし **黙って続けると「標準品番が全行で空」「板金行の既定ランクが変わる」といった退行が静かに起きる**ので、
理由を `NeoBuilder.silent_errors` に集め、次の 3 か所に出す。

- `build()` → `report['silent_errors']` → `run_case.py` が `★` で表示
- `build_rows()` → `stats['silent_errors']`（この呼び出し分だけ。生成器を直に呼ぶ検査スクリプト用）
- `inspect_estimate.py` → 明細の `★` フラグ（色別部品の照合失敗）

新しく `except Exception` を足すときは、`self._note_silent(どこで, 例外, 何が変わるか)` を必ず併せて呼ぶ。
`AddataParts` など `NeoBuilder` 以外のクラスには `_note_silent` が無いので、そこでは属性に控え、
`build_rows` 側で拾って載せる（`parts._r11_error` がその形）。

## 検証（tests/）
`bash claude_neo_pipeline/tests/verify_all.sh` で一括実行（**開発機専用**。新しい PC は `env_check.py --self-test`）。実 NEO（他工場のコグニ生成 NEO 10 本と実機 fixture `NEO_check/_eva_exp/cogni_*.neo`）と、個人情報を含む検証物 `%USERPROFILE%\Documents\NEO_check\`（環境変数 `NEO_CHECK_ROOT` で変更可）を使う。
- `unit_frame.py`: 骨格組合せ 23 パターン（コグニ実機 J52、ADDATA 仕様 §11-7c）
- `test_frame_gen.py` / `test_frame_wage.py`: 生成 NEO とコグニ保存 NEO（FRAME_p7/p8）の ERParts セル比較
- `roundtrip.py`: 他工場 NEO 10 本 722 行の部品照合・車両特定・標準指数・塗装の再現率
- `test_color13.py`: 83.DB / 13.DB の色別・期間別品番の突合せ（回帰 NEO 10 本 + COLOR_D98 の取替行 102 件中 94 一致。残りはコグニのダイアログで担当者が選んだ行と旧データ版の '*' 付き品番）
- `ct_frame.py`: 骨格行の ChangeTotal 検算
- `audit_cogni_files.py`: 実機保存 NEO との総当たり（ERParts 全列）。`--ansmb` で AnSMB 142 桁、**`--full-files` で「そのまま保存」した実機 NEO（25 本。実案件 2 件を含む）と両 SQLite の全テーブル・スキーマと AnSMB.txt / AnSvEm0001Ex.db / AnNote.ini の完全一致を見る**
- 回帰 4 案件（C-HR / シエンタ / ボルボ / N-ONE）の合計一致（N-ONE は −8 円が正）
