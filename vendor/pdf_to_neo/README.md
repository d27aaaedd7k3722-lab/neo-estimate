# SHOUCHIKU8 NEO — 工場見積 PDF → コグニセブン NEO 自動生成

**最終更新: 2026-09-08**（このファイルを読めば、いまの状態と使い方が分かるように保つ）

## 1. いまの姿（結論）

- 主経路は **Claude Code スキル `pdf-to-neo`**（`.claude/skills/pdf-to-neo/`）＋ **生成器 `claude_neo_pipeline/`** です。工場から届く見積 PDF（FAX 含む、5 書式）と車検証から、コグニセブンで人が作ったのと同じ NEO を作ります。
- 実案件 7 件（C-HR / シエンタ / ボルボ V40 / N-ONE / アルファード / オデッセイ / N-BOX 協定額）で見積書合計と一致し、コグニセブン実機の画面とも一致しています。
- 2026-09-08 にコグニ実機で装備（EVA）を切り替えた NEO 9 本を保存して解析し、生成器の部品番号・価格・指数の選択がコグニの再検索と一致することを確認しました（差があった「枠の取り合い」規則は実装済み）。
- **このフォルダの** Streamlit アプリ（`app.py` / `pdf_to_neo_pipeline.py` ほか）は **2026-08-11 で止まった古いコピー**です。触らないでください。
- Streamlit アプリそのものは別リポジトリで**現役**です（`%USERPROFILE%\dev\neo-estimate`、本番 https://shouchiku-neo-estimate.streamlit.app/ ）。アプリを直すときはそちらで作業します。スキル経路とアプリ経路は**並行して使われている別の道具**で、どちらかが廃止されたわけではありません。

## 2. フォルダ構成（追跡対象）

| 場所 | 役割 |
|---|---|
| `.claude/skills/pdf-to-neo/HANDOFF.md` | 引き継ぎ文書（全体の地図・絶対ルール・実機で確かめた事実・落とし穴・未解決）。**初めての人はここから読む** |
| `.claude/skills/pdf-to-neo/SKILL.md` | 手順書（9 段: 入力 → 読取 → 車両特定 → ページ単位転記 → make_neo → 検算 → コグニ実機 → 納品 → 記録）。HANDOFF の次に読む |
| `.claude/skills/pdf-to-neo/reference/` | `reading_schema.md`（転記ファイルの形）、`estimate_schema.md`、`format_catalog.md`（書式 A〜E）、`judgment_rules.md`（判断規則の根拠）、`checklist.md`、`part_code_names.json`（全車種 12.DB から作った 部品名 → 部品コード 辞書） |
| `.claude/skills/pdf-to-neo/scripts/` | `make_neo.py`（1 コマンド実行）、`reading_pages.py`（ページ単位転記の検算・束ね）、`reading_check.py`（紙上検算）、`ocr_prefill.py` + `winocr.ps1`（Windows OCR 先読み）、`draft_estimate.py`（判断規則の適用）、`inspect_estimate.py`（ADDATA 突合せ）、`option_audit.py`（装備監査）、`regress_cases.py`（回帰）、`skill_env.py` / `env_check.py`（PC ごとの ADDATA・コグニの場所解決）、`make_bundle.py`（配布 zip）、`build_part_names.py`（別名辞書の生成）、`tests/` |
| `claude_neo_pipeline/` | NEO 生成器。`estimate_to_neo.py`（ADDATA 照合・指数・装備・塗装・NEO 書き出し）、`addata_vehicle_resolver.py`（車検証 → 車両）、`paint_index.py`、`neo_container.py` / `neo_header.py`、`run_case.py`、`reference/`（COM マスタと雛形 `template.neo`）、`tests/`（`verify_all.sh` = 全検証） |
| `NEO_FILE_SPEC_COMPLETE.md` | NEO ファイル仕様（実 NEO と実機で確定した項目。§10-7 = 装備変更と再検索の実測） |
| `ADDATA_REVERSE_LOOKUP_SPEC.md` | ADDATA の逆引き仕様（11/12/13/15/17/20/23/83.DB、COM.CAB） |
| `ADDATA_FULL_STRUCTURE_SPEC.md` | ADDATA 全 DB の構造解析（2026-07） |
| `AGENTS.md` | Codex（レビュー AI）向けの短い案内（正本は CLAUDE.md → README → HANDOFF） |
| （リポジトリ外）`%USERPROFILE%\Documents\NEO_check\_profiles\factory_profiles.json` | 工場ごとの設定の学習データ（取引先名を含むので PC ローカル。make_neo が合格時に記録） |
| `docs/pdf-to-neo_ロードマップ.md` | 不足している情報・必要な解析・進め方 |
| `docs/` のその他 | 旧アプリのマニュアル類（レガシー） |
| `_addata_db_search.py` | ADDATA の DB を直接引く補助スクリプト |

追跡しないもの（`.gitignore`）: 顧客情報を含む案件データ（`%USERPROFILE%\Documents\NEO_check\`、`*.neo`、`*.pdf`）、`_archive/`（旧ハーネス v3〜v26、2026-05 の旧設計書、生成物）、`サンプル見積PDF/`、`claude_neo_pipeline/out/`（生成器のテスト用出力）、配布 zip、`.claude/` のスキル以外。

## 3. 使い方（新しい PC も同じ）

新しい PC で最初に打つのは次の 3 つ（`files` フォルダで実行）。**PowerShell 版**:

```powershell
Set-Location <files のパス>
$env:PYTHONIOENCODING = 'utf-8'
# 初回だけ: ADDATA とコグニセブンを自動検出して設定を保存し、個人スキル領域にスキルを登録
python .claude\skills\pdf-to-neo\scripts\env_check.py --save --install-skill
# 別名辞書を作る（ADDATA を更新したときも）
python .claude\skills\pdf-to-neo\scripts\build_part_names.py
# その PC で正しく作れるかの確認（ADDATA だけで完結する単体テスト 9 本。「自己診断: すべて合格」なら開発機と同じ結果になる）
python .claude\skills\pdf-to-neo\scripts\env_check.py --self-test
```

Git Bash 版は `cd files && PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/env_check.py --save --install-skill` のように、環境変数をコマンドの前に付ける。

開発機だけの全検証（実機 fixture `NEO_check/_eva_exp` と実案件フォルダが必要。配布 zip には入っていない）:

```bash
bash claude_neo_pipeline/tests/verify_all.sh
```

案件の流れは `SKILL.md` のとおり。要点は「PDF を印字のまま `pages/page_N.json` に写す → `reading_pages.py validate` でページごとに検算 → `make_neo.py <案件フォルダ> --name <名>` で 紙上検算 → 下書き → 突合せ → 生成 → 検算 → 装備監査 → 報告文（report.md）→ `--deliver` で納品コピー」。合計が全部一致するまで終わらない。

## 4. 検証の状態（2026-09-12 更新）

| 検証 | 結果 |
|---|---|
| **実案件 NEO 638 本**（亮平さんがコグニで作成、この PC と同じ ADDATA 2026/08 版。`tests/corpus_scan.py roundtrip`） | 車両 87.8% / 部品照合 98.2%（品番あり）・94.7%（名称のみ）/ 標準指数 95.9% / 塗装パネル 94.7% / 加算基礎 94.1% |
| 生成器の単体（セル比較 285 / 標準指数 77 / 塗装 44 / 骨格 / バンパ / 色別部品） | 全合格（開発機の `verify_all.sh`。新しい PC では `env_check.py --self-test` で 9 本） |
| 実案件 4 件（C01 C-HR、C02 シエンタ、C03 ボルボ、N-ONE）の再生成 | 合計一致（N-ONE は工場書式の円未満計上により −8 円が正） |
| 実案件 3 件（C04 アルファード、C05 オデッセイ、C06 N-BOX）の reading.json 回帰 | 下書きが正解と一致・合計一致 |
| コグニ実機: 装備 9 条件（N-BOX J87） | 部品番号・価格 全件一致、指数は「枠の取り合い」実装後に一致（`tests/unit_eva_slot.py` 20 件） |
| コグニ実機: 手入力・塗装 4 条件・骨格 2 条件・工賃単位・消費税設定 | 保存 NEO と全列一致（`unit_manual_rows.py` / `unit_settings.py`、帳票 PDF からの読み戻し 2 案件） |
| コグニ実機: 連動・吸収 7 条件 + 複合 3 条件（カラー無し・年式群 00 含む）+ 他ブロック 3 条件 + 板金ランク | 合計・指数・WorkCode・取替合計が一致（`unit_link_absorb.py` 34 件） |
| コグニ実機: 実案件 3 件（C-HR / アルファード / N-BOX）の再検索 | 差は装備選択・データ版・再検索の副作用のみ（仕様書 §10-12）。消費税小計・3コートパール・下処理面積も確認 |
| コグニ実機: オデッセイ 69 行の再検索 | 合計 1,332,012 円が再現 |
| スキルのテスト（紙上検算 17 / ページ単位 12 / OCR 12 / 別名辞書 3） | 全合格 |
| 入力の境界（数量 0・未知の修理方法・費用の行あふれ・負のレート ほか 12 通り） | 誤りは明確なエラーで停止（`tests/unit_guards.py`） |
| AnSMB.txt の実機一致 | 142 桁すべて一致（`tests/audit_cogni_files.py --ansmb`） |
| 状態リーク・再現性 | 同じ入力を 2 回・別案件を挟んで・新インスタンスで作っても NEO は同一 |

案件データの置き場は `NEO_check\<損保>_<Cnn>_<車名>\`（顧客姓は匿名コード C01〜C06。対応表は Vault の日誌）。装備・連動・吸収・骨格・設定の実機実験 NEO は `NEO_check\_eva_exp\`（README 付き。仕様書 §10-7〜§10-11 の根拠）。

## 5. これから

不足している情報・必要な解析・進め方は `docs/pdf-to-neo_ロードマップ.md` にまとめてあります。

## 6. Streamlit アプリ（このフォルダのものは古いコピー）

`app.py`（Streamlit）、`pdf_to_neo_pipeline.py`、`auto_matching.py`、`addata_locator.py`、`simple_mode_v11.py`、`Dockerfile`。

**このフォルダにあるのは 2026-08-11 で止まった古いコピーで、使ってはいけません。**
そのあとに直した不具合が1つも入っていません（ページ境界の重複行で総額が
42,460 円不足する、保険欄が空になる、検証が素通りする など）。

現役の本体は **`%USERPROFILE%\dev\neo-estimate`**（本番
https://shouchiku-neo-estimate.streamlit.app/ を配信）。アプリを直すときは
必ずそちらで作業します。引き継ぎ書は `dev\neo-estimate\docs\引き継ぎ書.md`。

`起動.bat` は `dev\neo-estimate` があればそちらを起動します（2026-09-12 に変更。
元の内容は `起動.bat.bak`）。手順書 `docs/操作マニュアル.md` は 2026-05 当時の
もので、いまの画面とは違います。2026-05 の設計書は `_archive/docs_2026-05_old_app/`。
