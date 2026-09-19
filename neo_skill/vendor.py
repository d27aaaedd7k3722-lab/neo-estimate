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

# 読み込みを始めたときのコードの指紋（ファイルの最後で読み直し、同じ中身のときだけ __app_src_digest__ に控える。読み込みの途中で
# push されたら控えず、古い扱いにして読み直させる。レビュー 3 周目）
try:
    import hashlib as _stamp_hashlib0
    with open(__file__, 'rb') as _stamp_f0:
        _stamp_digest_at_start = _stamp_hashlib0.sha256(_stamp_f0.read()).hexdigest()
    del _stamp_hashlib0, _stamp_f0
except Exception:  # noqa: BLE001
    _stamp_digest_at_start = None

import functools
import hashlib
import json
import os
import re
import tempfile
import sys
from typing import Optional

# 取り込んでいる files（pdf-to-neo ブランチ）のコミット。tools/vendor_sync.py が取り直すときに書き換える
EXPECTED_COMMIT = '43e424de1a5b27a0062636b0b9413114d7f6102c'

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


_READY_OK = False


def readiness_error() -> str:
    """vendor が使えるか（使えれば ''）。成功だけ控える（一時的な失敗 = 取り込み中・OneDrive の同期待ち などをプロセスが終わるまで
    固定しない。2026-09-15 バグハント 3 回目 N10）"""
    global _READY_OK
    if _READY_OK:
        return ''
    why = _readiness_error_uncached()
    if not why:
        _READY_OK = True
    return why


def _readiness_cache_clear() -> None:
    global _READY_OK
    _READY_OK = False


readiness_error.cache_clear = _readiness_cache_clear   # tools/vendor_sync.py が取り込み直後に呼ぶ（lru_cache のときの呼び方のまま）


def _readiness_error_uncached() -> str:
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


_SECRET_ENV = re.compile(r'(API_?KEY|_TOKEN$|^TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|ANTHROPIC|GEMINI|OPENAI|GOOGLE_API|^AWS_|^AZURE_|HF_TOKEN)', re.I)
# 子プロセスに渡す環境変数は許可リスト（拒否リストだけだと DATABASE_URL・PRIVATE_KEY・HTTPS_PROXY の認証付き URL 等を取りこぼす。
# バグハント G7/J8）。vendor が読むのは ADDATA_* / NEO_* / REPO_ROOT / COGNI_* / PDF_TO_NEO* / KATASHIKI_DB / SELFTEST_SECONDS と
# Python・OS の基本（PATH・TEMP・LOCALAPPDATA・WINDIR・LANG・TZ …）だけ
_ALLOW_ENV = re.compile(r'^(PATH|PATHEXT|COMSPEC|SYSTEMROOT|SYSTEMDRIVE|WINDIR|TEMP|TMP|TMPDIR|HOME|USERPROFILE|HOMEDRIVE|HOMEPATH|APPDATA|LOCALAPPDATA|'
                        r'PROGRAMDATA|PROGRAMFILES|PROGRAMFILES\(X86\)|PROGRAMW6432|COMMONPROGRAMFILES|COMMONPROGRAMFILES\(X86\)|ALLUSERSPROFILE|PUBLIC|OS|'
                        r'USERNAME|COMPUTERNAME|NUMBER_OF_PROCESSORS|PROCESSOR_[A-Z0-9_]+|LANG|LANGUAGE|LC_[A-Z]+|TZ|PYTHON[A-Z0-9_]*|VIRTUAL_ENV|'
                        r'CONDA[A-Z0-9_]*|SSL_CERT_FILE|SSL_CERT_DIR|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|NEO_[A-Z0-9_]+|ADDATA_[A-Z0-9_]+|REPO_ROOT|'
                        r'COGNI_[A-Z0-9_]+|PDF_TO_NEO[A-Z0-9_]*|KATASHIKI_DB|SELFTEST_SECONDS|'
                        r'LD_LIBRARY_PATH|DYLD_LIBRARY_PATH|DYLD_FALLBACK_LIBRARY_PATH|USER|LOGNAME|SHELL|PWD|HOSTNAME|XDG_[A-Z_]+|FONTCONFIG_[A-Z_]+)$', re.I)   # 共有ライブラリの場所など秘密でない実行環境（Linux の本番で子プロセスが起動できなくならないように）


_GUIDE_FILES: dict = {}
import threading as _threading
_GUIDE_LOCK = _threading.Lock()   # 同じプロセスの利用者（スレッド）が同時に初めて呼んでも、表の一時ファイルを 1 回だけ作る


def guideline_file() -> str:
    """SHOUCHIKU の見積ガイドライン（材料代割合表 shouchiku_guideline.json。案件データではない社内の参考値で、公開リポジトリには
    置かない）の場所。無ければ ''。vendor はこの表の既定列を塗装の材料代割合の既定にする（実案件 NEO の 83% がこの値）。
    1) 環境変数 PDF_TO_NEO_GUIDELINE（ファイルの場所）
    2) 環境変数 PDF_TO_NEO_GUIDELINE_JSON（表の中身。本番は Streamlit の Secrets に置くと環境変数になる）→ 一時ファイルに書く
    3) PC の NEO_check/_reference/shouchiku_guideline.json（設定ファイル ~/.claude/pdf-to-neo.local.json → 既定の場所）"""
    p = os.environ.get('PDF_TO_NEO_GUIDELINE') or ''
    if p and os.path.isfile(p):
        return p
    js = os.environ.get('PDF_TO_NEO_GUIDELINE_JSON') or ''
    if js.strip():
        key = hashlib.sha256(js.encode('utf-8')).hexdigest()[:16]
        with _GUIDE_LOCK:
            fp = _GUIDE_FILES.get(key) or ''
            if fp and os.path.isfile(fp):
                return fp
            try:
                json.loads(js)
                d = os.path.join(tempfile.gettempdir(), 'neo_skill_guideline')
                os.makedirs(d, exist_ok=True)
                fp = os.path.join(d, f'guideline_{key}.json')
                if not os.path.isfile(fp):
                    # 書き途中の中身を別の子プロセスが読まないよう、一意の別名に書いてから置き換える（プロセス番号だけの名前は
                    # 同じプロセスのスレッド同士でぶつかり、負けた変換に表が渡らなかった。レビュー 2 周目）
                    fd, tmp = tempfile.mkstemp(prefix=f'guideline_{key}.', suffix='.tmp', dir=d)
                    try:
                        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                            fh.write(js)
                        os.replace(tmp, fp)
                    except OSError:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                        if not os.path.isfile(fp):   # 別のプロセスが先に置いたならそれを使う
                            raise
                _GUIDE_FILES[key] = fp
                return fp
            except (ValueError, OSError):
                return ''   # 失敗は控えない（一時的な失敗なら次の変換でやり直す）
    root = os.environ.get('NEO_CHECK_ROOT') or ''
    if not root:
        try:
            with open(os.path.join(os.path.expanduser('~'), '.claude', 'pdf-to-neo.local.json'), encoding='utf-8-sig') as fh:
                root = str((json.load(fh) or {}).get('NEO_CHECK_ROOT') or '')
        except (OSError, ValueError):
            root = ''
    root = root or os.path.join(os.path.expanduser('~'), 'Documents', 'NEO_check')
    fp = os.path.join(root, '_reference', 'shouchiku_guideline.json')
    return fp if os.path.isfile(fp) else ''


def subprocess_env(addata_root: Optional[str] = None, neo_check_root: Optional[str] = None) -> dict:
    """vendor のスクリプトを subprocess で呼ぶときの環境変数（このプロセスの os.environ は変えない）。
    REPO_ROOT を vendor に固定し、出力を UTF-8 にする。ADDATA / NEO_check はアプリが決めたものがあれば渡す
    （無ければ skill_env が 設定ファイル → 自動検出 で解決する）"""
    # 秘密情報（API キー・トークン・パスワード）は vendor の subprocess に渡さない（検算・生成には要らない。Codex hunt F2）
    env = {k: v for k, v in os.environ.items() if _ALLOW_ENV.match(k) and not _SECRET_ENV.search(k)
           and k.upper() not in ('NEO_SKILL_PROFILE', 'NEO_TEMPLATE')}   # 実案件の工場プロファイルに書かない・雛形は vendor 同梱（本番と同じ。Q10）
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
        # アプリが決めた Addata（橋渡し・ZIP・フォルダのパス・取得URL）を渡すときは、必ず「環境変数の Addata 以外に落とさない」旗を付ける。
        # 付けないと vendor の skill_env は部分 Addata（COM ＋ 車種フォルダだけ）を無効と見て設定ファイル・自動検出に落ち、この PC では
        # 画面と別の C:\Addata で NEO を作り、本番では Addata が無く失敗していた（2026-09-15 バグハント 3 回目 N-A・Q1。以前は橋渡しだけ）
        env['ADDATA_ROOT_PARTIAL'] = '1'
    # PC で動かしても本番（NEO_check が無い）と同じ NEO にする: 実案件の NEO_check（工場プロファイル・ガイドライン表・
    # 過去 NEO の索引）・コーパス・ガイドライン表の場所を子に渡さない（バグハント 3 回目 Q10）。工場プロファイルを使う・
    # 記録すると明示したとき（NEO_SKILL_PROFILE=1。PC だけ）は従来どおり
    _local_knowledge = os.environ.get('NEO_SKILL_PROFILE') == '1'
    env.pop('PDF_TO_NEO_GUIDELINE_JSON', None)   # 表の中身は渡さない（場所だけ下で渡す）
    if not _local_knowledge:
        for k in [k for k in env if k.upper() in ('NEO_CORPUS_ROOT', 'PDF_TO_NEO_GUIDELINE', 'NEO_CHECK_ROOT')]:
            env.pop(k, None)
    # 見積ガイドライン（材料代割合の既定）は案件データではないので渡す。PC と本番で同じ既定にするため、本番は Secrets の
    # PDF_TO_NEO_GUIDELINE_JSON から作る（無ければ vendor はコグニの既定の割合を使う）
    _g = guideline_file()
    if _g:
        env['PDF_TO_NEO_GUIDELINE'] = _g
    if neo_check_root:
        env['NEO_CHECK_ROOT'] = neo_check_root
    elif not _local_knowledge:
        _empty = os.path.join(tempfile.gettempdir(), 'neo_skill_empty_check')
        try:
            os.makedirs(_empty, exist_ok=True)
            env['NEO_CHECK_ROOT'] = _empty
        except OSError:
            pass
    return env


def python_exe() -> str:
    return sys.executable

# 読み込んだときのコードの指紋（app.sync_app_modules が「メモリのコードがディスクと同じか」を見る。読み込みの時点で
# 控えないと、あとから初めて import したモジュールが「古い」と見なされ、偽の版ずれで変換を断っていた。バグハント 3 回目 N2）。
# ファイルの最後に置く: 読み直しが途中で例外になったときは古い指紋のまま残り、版ずれとして断れる（先頭に置くと
# 途中までしか新しくないモジュールを「揃った」と見ていた。レビュー 2026-09-15）
try:
    import hashlib as _stamp_hashlib
    with open(__file__, 'rb') as _stamp_f:
        _stamp_now = _stamp_hashlib.sha256(_stamp_f.read()).hexdigest()
    if _stamp_now == globals().get('_stamp_digest_at_start'):
        __app_src_digest__ = _stamp_now
    del _stamp_hashlib, _stamp_f, _stamp_now
except Exception:  # noqa: BLE001
    pass
