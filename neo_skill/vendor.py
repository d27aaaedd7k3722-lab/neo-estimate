# -*- coding: utf-8 -*-
"""vendor.py — 取り込んだ pdf-to-neo スキル（vendor/pdf_to_neo）の場所・コミットの照合・実行環境。

files 側の各スクリプトは `skill_env.find_repo_root()` でリポジトリ root を決める。
その最優先が環境変数 REPO_ROOT なので、subprocess にはこれを vendor に向けて渡す。
渡さないと、この PC に files の checkout があるときに設定ファイル（pdf-to-neo.local.json の REPO_ROOT）
経由でそちらを掴み、「取り込んだコミット」と違うコードが動くことがある。

このモジュールは **このプロセスの os.environ / sys.path を変えない**（Streamlit は 1 プロセスを全利用者で共有し、
アプリ側にも同名の _addata_db_search.py があるため）。vendor のコードは必ず subprocess で動かす
（maker.py → make_neo.py、reader.py → _runner.py）。
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import tempfile
import sys
from typing import Optional

# 取り込んでいる files（pdf-to-neo ブランチ）のコミット。tools/vendor_sync.py が取り直すときに書き換える
EXPECTED_COMMIT = '5747932abd93a0581b9912a3f910ff90798fea05'

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_ROOT = os.path.join(APP_ROOT, 'vendor', 'pdf_to_neo')
SKILL_DIR = os.path.join(VENDOR_ROOT, '.claude', 'skills', 'pdf-to-neo')
SCRIPTS_DIR = os.path.join(SKILL_DIR, 'scripts')
REFERENCE_DIR = os.path.join(SKILL_DIR, 'reference')
PIPELINE_DIR = os.path.join(VENDOR_ROOT, 'claude_neo_pipeline')
STAMP_PATH = os.path.join(VENDOR_ROOT, 'VENDOR_COMMIT.json')

REQUIRED = (
    os.path.join(SCRIPTS_DIR, 'make_neo.py'), os.path.join(SCRIPTS_DIR, 'skill_env.py'),
    os.path.join(SCRIPTS_DIR, 'reading_pages.py'), os.path.join(SCRIPTS_DIR, 'reading_check.py'),
    os.path.join(PIPELINE_DIR, 'estimate_to_neo.py'), os.path.join(PIPELINE_DIR, 'reference', 'template.neo'),
    os.path.join(REFERENCE_DIR, 'reading_schema.md'), os.path.join(REFERENCE_DIR, 'format_catalog.md'),
    os.path.join(SKILL_DIR, 'SKILL.md'),
)


def script(name: str) -> str:
    return os.path.join(SCRIPTS_DIR, name)


def reference(name: str) -> str:
    return os.path.join(REFERENCE_DIR, name)


def sha256_tree(root: str) -> str:
    """vendor 配下の全ファイルの内容ハッシュ（VENDOR_COMMIT.json 自身・.pyc・__pycache__ は除く）。
    tools/vendor_sync.py が取り込み時に記録し、readiness_error() が毎起動で照合する"""
    h = hashlib.sha256()
    root = os.path.abspath(root)
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for f in sorted(files):
            p = os.path.join(cur, f)
            # root 直下の VENDOR_COMMIT.json は除く（取り込み先が一時フォルダでも同じ値になるように）
            if (os.path.abspath(cur) == root and f == os.path.basename(STAMP_PATH)) or f.endswith('.pyc'):
                continue
            rel = os.path.relpath(p, root).replace(os.sep, '/')
            h.update(rel.encode('utf-8') + b'\0')
            with open(p, 'rb') as fh:
                h.update(fh.read())
            h.update(b'\0')
    return h.hexdigest()


def stamp() -> dict:
    """取り込んだコミットの記録（tools/vendor_sync.py が書く）。無ければ空"""
    try:
        with open(STAMP_PATH, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def commit_short() -> str:
    return str(stamp().get('commit') or '')[:12]


@functools.lru_cache(maxsize=1)
def readiness_error() -> str:
    """vendor が「取り込んだコミットのまま」使える状態なら ''。使えない理由を 1 文で返す。
    取り込み直したらプロセスを再起動する（結果はプロセス内で 1 回だけ計算する）"""
    missing = [p for p in REQUIRED if not os.path.isfile(p)]
    if missing:
        return 'vendor/pdf_to_neo に必要なファイルが無い（tools/vendor_sync.py で取り込む）: ' + os.path.relpath(missing[0], VENDOR_ROOT)
    st = stamp()
    if not st:
        return 'vendor/pdf_to_neo/VENDOR_COMMIT.json が無い（tools/vendor_sync.py で取り込む）'
    if str(st.get('commit') or '') != EXPECTED_COMMIT:
        return (f"vendor のコミット {str(st.get('commit') or '')[:12]} が、アプリが想定する {EXPECTED_COMMIT[:12]} と違う"
                '（tools/vendor_sync.py で想定のコミットを取り直す）')
    try:
        now = sha256_tree(VENDOR_ROOT)
    except OSError as e:
        return f'vendor の内容を確かめられない: {e}'
    if now != str(st.get('tree_sha256') or ''):
        return 'vendor/pdf_to_neo の中身が取り込み時と違う（アプリ側で書き換えられている。files で直して取り直す）'
    return ''


def is_ready() -> bool:
    return readiness_error() == ''


def subprocess_env(addata_root: Optional[str] = None, neo_check_root: Optional[str] = None) -> dict:
    """vendor のスクリプトを subprocess で呼ぶときの環境変数（このプロセスの os.environ は変えない）。
    REPO_ROOT を vendor に固定し、出力を UTF-8 にする。ADDATA / NEO_check はアプリが決めたものがあれば渡す
    （無ければ skill_env が 設定ファイル → 自動検出 で解決する）"""
    env = dict(os.environ)
    env['REPO_ROOT'] = VENDOR_ROOT
    env['PYTHONIOENCODING'] = 'utf-8'
    env.setdefault('PYTHONUTF8', '1')
    # vendor は COM.CAB / CHM の展開キャッシュを %LOCALAPPDATA% に置く。無い Linux（Streamlit Cloud）では一時フォルダに
    # 置かせる（配布物 vendor/ の中に書かれると内容ハッシュの照合に落ちて経路が止まる）
    env.setdefault('LOCALAPPDATA', os.path.join(tempfile.gettempdir(), 'neo_skill_cache'))
    # 生成器の「今日」（est_date の既定・NEO の保存時刻）は datetime.now() = サーバの地方時。Streamlit Cloud は UTC なので
    # 日本の 0〜9 時に前日の日付になる。Linux では TZ で日本時間にする（Windows は TZ を見ないので触らない）
    if os.name != 'nt':
        env.setdefault('TZ', 'Asia/Tokyo')
    if addata_root:
        env['ADDATA_ROOT'] = addata_root
        # PC からブラウザ経由で取り寄せた部分 Addata（COM ＋ 見積の車種フォルダ。neo_skill.bridge の addata_bridge_<id>）は、
        # vendor の skill_env が「メーカーフォルダ 5 つ以上」の検証（is_addata）で弾いて自動検出に落とす
        # （この PC では C:\Addata に化けて気づけず、Cloud では見つからず失敗）。ADDATA_ROOT_PARTIAL=1 で
        # 「部分コピーを渡している」と伝える（vendor の skill_env.is_addata_or_partial。2026-09-14 Codex 43）
        if os.path.basename(os.path.normpath(addata_root)).startswith('addata_bridge_'):
            env['ADDATA_ROOT_PARTIAL'] = '1'
    if neo_check_root:
        env['NEO_CHECK_ROOT'] = neo_check_root
    return env


def python_exe() -> str:
    return sys.executable
