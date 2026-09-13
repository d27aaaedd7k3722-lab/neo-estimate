# -*- coding: utf-8 -*-
"""neo_skill — files リポジトリの pdf-to-neo スキル（vendor/pdf_to_neo）をアプリから呼ぶ層。

  vendor.py   取り込んだスキルの場所・取り込みの照合（コミット・内容ハッシュ）・subprocess の環境変数（REPO_ROOT）
  maker.py    make_neo.py をそのまま呼ぶ（下書き → 突合せ → 生成 → 検算 → 確認箇所シート）。合否も make_neo と同じ
  llm.py      見積 PDF を読む LLM（Claude API）の薄い層
  prompts.py  指示文の組み立て（reading_schema.md / format_catalog.md / SKILL.md 手順 4 を要約せずそのまま使う）
  reader.py   PDF → ページごとに写す → 検算 → 落ちたページだけ読み直す → reading.json
  _runner.py  vendor の検算関数を別プロセスで呼ぶ（reader.py が使う）

規則（部品コードの決め方・左右分割・数量の読み替え・塗装・レバーレート…）はここには書かない。
すべて vendor の draft_estimate.py と claude_neo_pipeline/ が決める（移植ガイド §1）。

vendor のコードは **必ず subprocess で動かす**。vendor のスクリプトは import 時に os.environ と sys.path を
書き換えるので、このプロセス（Streamlit は 1 プロセスを全利用者で共有。アプリ側にも同名の
_addata_db_search.py がある）では import しない。
"""
