#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-OCR連携 NEOファイル自動生成Webアプリ v3.2
コグニセブン用NEOファイルを車検証PDF＋見積書PDFから自動生成

【v3.2 追加・修正内容】
- google.generativeai → google.genai SDK移行（FutureWarning解消）
- 並列API呼び出しによる解析高速化（車検証＋見積書同時解析、マルチページ並列処理）
- NEO生成前の金額検証アラート（部品/工賃の差額チェック＋確認必須）
- 「**」工賃の0円処理（工賃欄の「**」は工賃なしとして読み取り）
- ショートパーツ二重計上防止（明細行→Expense自動移行）
- 預託/廃棄処分費用の非課税Expense自動振り分け（LineNo=5）

【v3.1 追加・修正内容】
- 画像前処理（コントラスト・シャープネス強化）によるFAX品質改善
- Gemini構造化JSON出力モード（response_mime_type: application/json）
- 明細行ごとの整合性チェック（数量×単価≠金額の検出・警告）
- 画像前処理のON/OFFオプション（サイドバー）

【v3.0 追加・修正内容】
- APIキーのハードコード除去（サイドバー入力のみ）
- FAXページ自動除外（ページ分類機能）
- 税込/税抜 自動判定（build_estimate_summary）
- 自己修復ループ（_self_correction_retry）
- 辞書ベースバリデーション（validate_and_correct_items）
- PDF→JPEG ラスタライズ（行ズレ防止オプション）
- プロンプト強化（_thought_process + discount_amount + amount_basis）
- 逆算一致時の誤警告抑制（reverse_match）
"""

import warnings
warnings.filterwarnings("ignore", message=".*use_container_width.*")

from dotenv import load_dotenv
load_dotenv()

import streamlit as st
import struct
import uuid as _uuid
import zlib
import sqlite3
import tempfile
import os
import datetime
import json
import io
import re
import sys
import copy
import math
import unicodedata
import traceback
import pandas as pd
import threading
from concurrent.futures import ThreadPoolExecutor

# ============================================================
# 定数・設定
# ============================================================
SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILENAME = "template_toyota.neo"
TEMPLATE_PATH     = os.path.join(SCRIPT_DIR, TEMPLATE_FILENAME)
ANALYSIS_LOG_PATH = os.path.join(SCRIPT_DIR, "analysis.log")
TAX_RATE          = 0.10

# 日本標準時（JST）。Streamlit Community Cloud のコンテナは日本時間ではないため、
# datetime.datetime.now() をそのまま使うと、NEO に書く見積日・作成日が
# 日本の日付より1日前になることがある（本番で実際に発生していた）。
# 夏時間が無い固定オフセットなので、tzdata に依存しない timezone で表す。
JST = datetime.timezone(datetime.timedelta(hours=9))


def now_jst():
    """日本時間の現在日時（タイムゾーン付き）。日付・時刻を出すときは必ずこれを使う。"""
    return datetime.datetime.now(JST)

# ヘッダXML <CarRegistedDateEra> の元号コード。
# 【実機確認が必要】この対応は日本のシステムで一般的な並び順によるもので、
# コグニセブンの実機で確認できていない。テンプレート原本は初度登録が
# 空欄のまま 4 を持っているだけで、根拠にならなかった。
# 誤っていた場合はここだけ直せば済むように1か所にまとめている。
_ERA_CODE = {'明治': '1', '大正': '2', '昭和': '3', '平成': '4', '令和': '5'}


def best_intax_for(intax_total):
    """税込総額に最も近い「実現できる税込総額」を返す。

    コグニセブンは税抜で保存して消費税を計算するので、実際に .neo に
    入る総額は必ず S + round(S*0.1) の形になる。見積書に印字された
    税込総額がこの形で表せない場合（およそ11件に1件）、生成される
    .neo は原本と1円ずれる。画面とファイルで別々に計算すると、
    画面が「完全一致」と出したまま1円違うファイルが出るので、
    両方でこの関数を使う。
    """
    if not intax_total:
        return 0
    _sign = -1 if intax_total < 0 else 1
    _v = abs(int(intax_total))
    base = jpy_round(_v / (1 + TAX_RATE))
    best, best_err = base, None
    for off in (0, -1, 1, -2, 2):
        cand = base + off
        err = abs(cand + jpy_round(cand * TAX_RATE) - _v)
        if best_err is None or err < best_err:
            best, best_err = cand, err
        if err == 0:
            break
    return _sign * (best + jpy_round(best * TAX_RATE))
# Streamlit Cloud の st.secrets にも対応（ローカルは .env を使用）
try:
    GEMINI_API_KEY = st.secrets.get('GEMINI_API_KEY', os.environ.get('GEMINI_API_KEY', ''))
except Exception:
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL      = "gemini-3.5-flash"          # フォールバック（動的に上書きされる）
CONFIDENCE_THRESHOLD = 0.6

# 優先順位付きのモデル候補リスト（上位が最優先）
# ※ Gemini 2.5 系は 2026年に提供終了（gemini-2.5-flash は予告より早く停止）。
#    実際に使えるモデルは API の models.list で動的に検出し、このリストは
#    「検出結果の並び順」と「API検出に失敗した時の静的フォールバック」に使う。
_PREFERRED_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-pro",
    "gemini-3-pro",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]
_FALLBACK_MODEL = "gemini-3.5-flash"

# 汎用の文書/画像理解に使えないモデル種別（models.list の結果から除外）
_EXCLUDED_MODEL_KEYWORDS = (
    'tts', 'image', 'live', 'embedding', 'omni', 'transcribe', 'audio',
    'robotics', 'computer-use', 'veo', 'imagen', 'aqa', 'learnlm', 'gemma',
    'deep-research', 'latest', 'exp',
)

# Streamlitはユーザー操作のたびにスクリプト全体を再実行するため、モジュール変数は
# 毎回初期化されてしまう。モデル一覧・利用不可モデルの記録は st.session_state に
# 逃がして再実行をまたいで保持する（毎回 models.list を叩かないため）。
_FALLBACK_STORE: dict = {}


def _persist_store() -> dict:
    """再実行をまたいで保持されるストアを返す（session_state が使えない場合はモジュール変数）"""
    try:
        store = st.session_state.setdefault('_gemini_model_store', {})
        if isinstance(store, dict):
            return store
    except Exception:
        pass
    return _FALLBACK_STORE


def _quota_exhausted_set() -> set:
    """クォータ超過で利用不可になったモデルの集合"""
    store = _persist_store()
    val = store.get('quota_exhausted')
    if not isinstance(val, set):
        val = set()
        store['quota_exhausted'] = val
    return val


def _unavailable_set() -> set:
    """提供終了（404 NOT_FOUND / no longer available）と判明したモデルの集合"""
    store = _persist_store()
    val = store.get('unavailable')
    if not isinstance(val, set):
        val = set()
        store['unavailable'] = val
    return val


def _availability_cache() -> dict:
    """APIキーごとの利用可能モデル一覧キャッシュ"""
    store = _persist_store()
    val = store.get('availability')
    if not isinstance(val, dict):
        val = {}
        store['availability'] = val
    return val

# 解析結果キャッシュ: 同一ファイル（md5）の再解析を防ぐ（セッション中に有効）
# key: md5_hex + "_" + model_name + "_" + str(use_rasterize) → value: 解析結果dict
_analyze_result_cache: dict = {}


def _model_cache_key(api_key: str) -> str:
    return api_key[-8:] if api_key else ''


def _is_model_unavailable_error(err_msg: str) -> bool:
    """モデル提供終了・存在しないモデルを示すエラーかどうか"""
    m = str(err_msg)
    return ('no longer available' in m) or ('NOT_FOUND' in m) or ('404' in m) or ('is not found' in m)


def _mark_model_unavailable(api_key: str, model_name: str):
    """提供終了モデルを記録し、モデル一覧キャッシュを破棄する"""
    if model_name:
        _unavailable_set().add(model_name)
    _availability_cache().pop(_model_cache_key(api_key), None)


def _model_sort_key(name: str):
    """モデルIDを優先順位でソートするためのキー（小さいほど優先）。
    GA版 > preview、通常 > lite、flash > pro（クォータ・速度優先）、新バージョン > 旧バージョン"""
    m = re.match(r'^gemini-(\d+)(?:\.(\d+))?-(flash|pro)(-lite)?(.*)$', name)
    if not m:
        return (9, 0, 0, 0, name)
    major = int(m.group(1)); minor = int(m.group(2) or 0)
    kind = 0 if m.group(3) == 'flash' else 1
    lite = 1 if m.group(4) else 0
    preview = 1 if 'preview' in (m.group(5) or '') else 0
    return (preview, lite, kind, -(major * 100 + minor), name)


def _list_models_from_api(api_key: str) -> list:
    """Gemini API の models.list から generateContent 対応モデルIDを取得する。失敗時は空リスト"""
    try:
        client = _get_genai_client(api_key)
        names = []
        for m in client.models.list():
            name = (getattr(m, 'name', '') or '')
            if name.startswith('models/'):
                name = name[len('models/'):]
            if not name.startswith('gemini-'):
                continue
            actions = getattr(m, 'supported_actions', None) or []
            if actions and 'generateContent' not in actions:
                continue
            if any(k in name for k in _EXCLUDED_MODEL_KEYWORDS):
                continue
            names.append(name)
        return names
    except Exception as e:
        print("Gemini models.list error:", e)
        return []


def get_available_gemini_models(api_key: str) -> list:
    """利用可能なGeminiモデルを返す（優先順位付き）。
    API の models.list で実際に使えるモデルを検出し、提供終了・クォータ超過モデルを除外する。
    API検出に失敗した場合は静的な優先リストにフォールバックする。"""
    if not api_key:
        return [_FALLBACK_MODEL]
    cache_key = _model_cache_key(api_key)
    if cache_key in _availability_cache():
        return _availability_cache()[cache_key]
    api_models = _list_models_from_api(api_key)
    if api_models:
        candidates = sorted(set(api_models), key=_model_sort_key)
    else:
        candidates = list(_PREFERRED_MODELS)
    result = [m for m in candidates
              if m not in _quota_exhausted_set() and m not in _unavailable_set()]
    if not result:
        result = [m for m in candidates if m not in _unavailable_set()] or [_FALLBACK_MODEL]
    _availability_cache()[cache_key] = result
    return result


def get_default_gemini_model(api_key: str) -> str:
    """利用可能なモデルの中から最優先モデルを返す。クォータ超過・提供終了モデルは除外。"""
    models = get_available_gemini_models(api_key)
    for m in models:
        if m not in _quota_exhausted_set() and m not in _unavailable_set():
            return m
    # 全モデルがクォータ超過の場合はフォールバック
    return models[0] if models else _FALLBACK_MODEL


def get_alternative_gemini_model(api_key: str, failed_model: str) -> str:
    """failed_model 以外で利用可能な代替モデルを返す（無ければ空文字）"""
    for m in get_available_gemini_models(api_key):
        if m != failed_model and m not in _quota_exhausted_set() and m not in _unavailable_set():
            return m
    return ''
SELF_CORRECTION_THRESHOLD = 1000  # 差額が1000円以上の場合のみ自己修復を試行（高速化）

DOS_DBVER = bytes.fromhex('334cc198')   # AnDBVersion.ini 固定値
DOS_IMGE  = bytes.fromhex('2c365a67')   # AnSvImge.ini 固定値

# ============================================================
# 全角→半角カタカナ変換テーブル
# ============================================================
FULL_TO_HALF_KANA = {
    'ア': 'ｱ', 'イ': 'ｲ', 'ウ': 'ｳ', 'エ': 'ｴ', 'オ': 'ｵ',
    'カ': 'ｶ', 'キ': 'ｷ', 'ク': 'ｸ', 'ケ': 'ｹ', 'コ': 'ｺ',
    'サ': 'ｻ', 'シ': 'ｼ', 'ス': 'ｽ', 'セ': 'ｾ', 'ソ': 'ｿ',
    'タ': 'ﾀ', 'チ': 'ﾁ', 'ツ': 'ﾂ', 'テ': 'ﾃ', 'ト': 'ﾄ',
    'ナ': 'ﾅ', 'ニ': 'ﾆ', 'ヌ': 'ﾇ', 'ネ': 'ﾈ', 'ノ': 'ﾉ',
    'ハ': 'ﾊ', 'ヒ': 'ﾋ', 'フ': 'ﾌ', 'ヘ': 'ﾍ', 'ホ': 'ﾎ',
    'マ': 'ﾏ', 'ミ': 'ﾐ', 'ム': 'ﾑ', 'メ': 'ﾒ', 'モ': 'ﾓ',
    'ヤ': 'ﾔ', 'ユ': 'ﾕ', 'ヨ': 'ﾖ',
    'ラ': 'ﾗ', 'リ': 'ﾘ', 'ル': 'ﾙ', 'レ': 'ﾚ', 'ロ': 'ﾛ',
    'ワ': 'ﾜ', 'ヲ': 'ｦ', 'ン': 'ﾝ',
    'ァ': 'ｧ', 'ィ': 'ｨ', 'ゥ': 'ｩ', 'ェ': 'ｪ', 'ォ': 'ｫ',
    'ッ': 'ｯ', 'ャ': 'ｬ', 'ュ': 'ｭ', 'ョ': 'ｮ',
    'ガ': 'ｶﾞ', 'ギ': 'ｷﾞ', 'グ': 'ｸﾞ', 'ゲ': 'ｹﾞ', 'ゴ': 'ｺﾞ',
    'ザ': 'ｻﾞ', 'ジ': 'ｼﾞ', 'ズ': 'ｽﾞ', 'ゼ': 'ｾﾞ', 'ゾ': 'ｿﾞ',
    'ダ': 'ﾀﾞ', 'ヂ': 'ﾁﾞ', 'ヅ': 'ﾂﾞ', 'デ': 'ﾃﾞ', 'ド': 'ﾄﾞ',
    'バ': 'ﾊﾞ', 'ビ': 'ﾋﾞ', 'ブ': 'ﾌﾞ', 'ベ': 'ﾍﾞ', 'ボ': 'ﾎﾞ',
    'パ': 'ﾊﾟ', 'ピ': 'ﾋﾟ', 'プ': 'ﾌﾟ', 'ペ': 'ﾍﾟ', 'ポ': 'ﾎﾟ',
    'ヴ': 'ｳﾞ', 'ー': 'ｰ',
    '。': '｡', '「': '｢', '」': '｣', '、': '､', '・': '･',
}


# ============================================================
# ユーティリティ関数
# ============================================================

def to_halfwidth_katakana(text):
    """全角カタカナ・全角英数字・全角記号を半角に変換（部品名用）
    変換対象: カタカナ→半角カタカナ、英数字→半角英数字、一部記号→半角記号
    漢字など半角変換不可の文字はそのまま全角を維持する。
    """
    if not text:
        return text
    result = []
    for ch in text:
        # まずカタカナ変換テーブルをチェック
        if ch in FULL_TO_HALF_KANA:
            result.append(FULL_TO_HALF_KANA[ch])
        # 全角英大文字 Ａ-Ｚ → A-Z
        elif '\uff21' <= ch <= '\uff3a':
            result.append(chr(ord(ch) - 0xFEE0))
        # 全角英小文字 ａ-ｚ → a-z
        elif '\uff41' <= ch <= '\uff5a':
            result.append(chr(ord(ch) - 0xFEE0))
        # 全角数字 ０-９ → 0-9
        elif '\uff10' <= ch <= '\uff19':
            result.append(chr(ord(ch) - 0xFEE0))
        # 全角スペース → 半角スペース
        elif ch == '\u3000':
            result.append(' ')
        # 全角記号の一部 → 半角記号
        elif ch == '\uff08':  # （ → (
            result.append('(')
        elif ch == '\uff09':  # ） → )
            result.append(')')
        elif ch == '\uff0d':  # － → -
            result.append('-')
        elif ch == '\uff0f':  # ／ → /
            result.append('/')
        elif ch == '\uff0e':  # ． → .
            result.append('.')
        elif ch == '\uff0c':  # ， → ,
            result.append(',')
        else:
            result.append(ch)
    return ''.join(result)


def datetime_to_dos(dt):
    """Python datetime → DOS日時バイト列(4B)"""
    dos_date = ((dt.year - 1980) << 9) | (dt.month << 5) | dt.day
    dos_time = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
    return struct.pack('<HH', dos_date, dos_time)


def _normalize_ym8(raw) -> str:
    """初度登録年月を YYYYMM00 に正規化する。

    YYYYMM / YYYYMMDD / 「2019/03/01」のような区切り付きを受ける。
    読み取れない場合は空文字を返す。
    """
    s = re.sub(r'[^\d]', '', str(raw or ''))
    if len(s) == 6:
        s += '00'
    if len(s) != 8:
        return ''
    try:
        y, m = int(s[:4]), int(s[4:6])
    except ValueError:
        return ''
    if not (1926 <= y <= 2999) or not (1 <= m <= 12):
        return ''
    if s[6:8] == '00':
        return s
    return s if _normalize_date8(s) else ''


def get_era_info(date_str):
    """YYYYMMDD文字列 → (和暦名, 和暦年4桁ゼロ埋め)

    年だけで分岐すると改元日をまたぐ月が必ず狂う。
    平成31年3月登録（2019年1〜4月）の車は実際に多く、
    年だけ見ると令和1年3月になってしまう。
    """
    if not date_str or len(date_str) < 4 or date_str == '00000000':
        return '令和', '0000'
    try:
        year  = int(date_str[:4])
        month = int(date_str[4:6]) if len(date_str) >= 6 else 0
        day   = int(date_str[6:8]) if len(date_str) >= 8 else 0
    except ValueError:
        return '令和', '0000'
    # 初度登録年月は YYYYMM00 で日が無い。その場合は月初とみなす。
    ymd = (year, month or 1, day or 1)
    if ymd >= (2019, 5, 1):
        return '令和', f'{year - 2018:04d}'
    if ymd >= (1989, 1, 8):
        return '平成', f'{year - 1988:04d}'
    if ymd >= (1926, 12, 25):
        return '昭和', f'{year - 1925:04d}'
    return '令和', '0000'


def repair_truncated_json(text):
    """途中で切れたJSONを修復して読み取り可能にする"""
    if not text:
        return text
    text = text.strip()
    # 配列が未閉じ
    open_brackets = text.count('[') - text.count(']')
    open_braces   = text.count('{') - text.count('}')
    for _ in range(open_brackets):
        text += ']'
    for _ in range(open_braces):
        text += '}'
    # 末尾のカンマ除去
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def extract_json_from_response(text):
    """GeminiレスポンスからJSONオブジェクトを抽出"""
    if not text:
        return {}
    # コードブロック除去
    cleaned = re.sub(r'```(?:json)?', '', text)
    cleaned = re.sub(r'```', '', cleaned).strip()
    # JSON部分を抽出
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match:
        json_str = match.group(0)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            try:
                return json.loads(repair_truncated_json(json_str))
            except Exception:
                pass
    return {}


def get_mime_type(filename):
    """ファイル名からMIMEタイプを判定"""
    if not filename:
        return 'application/octet-stream'
    ext = filename.lower().rsplit('.', 1)[-1]
    mime_map = {
        'pdf':  'application/pdf',
        'jpg':  'image/jpeg',
        'jpeg': 'image/jpeg',
        'png':  'image/png',
        'webp': 'image/webp',
        'bmp':  'image/bmp',
        'tiff': 'image/tiff',
        'tif':  'image/tiff',
        'heic': 'image/heic',
        'heif': 'image/heif',
    }
    return mime_map.get(ext, 'application/octet-stream')


def safe_int(val, default=0):
    """OCR由来の「1個」「19,550円」「1.00」「8本」「**」なども整数化"""
    if val is None or val == '' or val == '*' or val == '**':
        return default
    if isinstance(val, str) and val.strip().replace('*', '') == '':
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        # 明細エディタでセルを空にすると NaN が入る。int(round(nan)) は
        # 例外になり、画面が操作不能になるため既定値に倒す。
        if val != val or val in (float('inf'), float('-inf')):
            return default
        return int(round(val))
    s = _normalize_number_text(str(val))
    if s is None:
        return default
    try:
        return int(round(float(s)))
    except (ValueError, OverflowError):
        return default


def _xml_escape(value) -> str:
    """ReportLabのParagraphに渡す前のエスケープ。

    Paragraphは簡易XMLを解釈するため、品名に & や < が含まれると
    描画時に例外になったり文字が消えたりする。
    """
    return (str(value if value is not None else '')
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _strip_control_chars(value) -> str:
    """改行・タブ・NUL などの制御文字を空白1つに潰す。

    NEO の内部ファイルには142バイト固定長のレコードがあり、
    制御文字が入ると行構造そのものが壊れる。
    """
    s = re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or ''))
    return re.sub(r'[ \t]+', ' ', s).strip()


def cp932_trim(value, max_bytes: int) -> str:
    """コグニセブンの列幅（CP932のバイト数）に収まるよう切り詰める。

    日本語は1文字2バイトなので、文字数で切ると宣言幅の2倍入ってしまう。
    多バイト文字の途中で切れないよう、デコードできる位置まで戻す。
    """
    s = str(value if value is not None else '')
    if not s:
        return ''
    b = s.encode('cp932', 'replace')[:max_bytes]
    while b:
        try:
            return b.decode('cp932')
        except UnicodeDecodeError:
            b = b[:-1]
    return ''


def jpy_round(value) -> int:
    """日本の商習慣どおり四捨五入して整数の円にする。

    Python の round() は偶数丸め（round(10.5)==10）なので、
    消費税の計算に使うと約20件に1件、1円少なくなる。
    """
    from decimal import Decimal, ROUND_HALF_UP
    try:
        return int(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except Exception:
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return 0


def _normalize_date8(raw) -> str:
    """日付入力を YYYYMMDD の8桁に正規化する。解釈できなければ空文字。

    「2026/09/01」「2026-09-01」「20260901」いずれも受け付ける。
    妥当でない日付（13月など）は書き込まない。
    """
    s = re.sub(r'[^\d]', '', str(raw or ''))
    if len(s) != 8:
        return ''
    try:
        d = datetime.datetime.strptime(s, '%Y%m%d')
    except ValueError:
        return ''
    # 昭和より前は和暦に変換できず、日付だけ入って元号が空になるため受け付けない
    if d.year < 1926:
        return ''
    return s


def _normalize_number_text(raw):
    """金額・数量の文字列を符号付きの数値文字列に正規化する。解釈不能なら None。

    見積書では値引きが「△5,000」「▲5,000」「(5,000)」「－5,000」と書かれ、
    車検証には「12,345km」「1,230kg」「1,490cc」のように単位が付く。
    記号を一律に削ると値引きが加算に化け、逆に厳格に弾くと単位付きの数字が
    0 になる。ここでは
      1. 通貨・区切り・既知の単位を落とす
      2. 先頭の符号（△▲ 各種マイナス）を符号として解釈して落とす
      3. 末尾のハイフン／長音（「1,234-」＝1,234円の慣用表記）を落とす
      4. 残りに数字のかたまりが「ちょうど1つ」ある時だけ採用する
    とする。「1,000～2,000」「2/3」のように数字が2つ以上あるものは
    どちらを採るか決められないので採用しない。
    """
    import unicodedata as _ud
    s = _ud.normalize('NFKC', str(raw)).strip()
    if not s:
        return None
    # 会計表記の括弧はマイナス
    is_negative = False
    if re.fullmatch(r'\(\s*[^()]*\s*\)', s):
        is_negative = True
        s = s[1:-1].strip()
    # 通貨・区切り・単位を先に落とす（「¥-1,000」の符号を見失わないため）
    s = re.sub(r'[円¥￥,\s]', '', s)
    s = re.sub(r'(個|本|枚|セット|台|式|時間)', '', s)
    # 先頭の符号
    if re.match(r'^[△▲▽▼\-\u2212\u30fc\u2010-\u2015]', s):
        is_negative = True
    s = re.sub(r'^[△▲▽▼\-\u2212\u30fc\u2010-\u2015]+', '', s)
    # 末尾のハイフン・長音（「1,234-」は 1,234円 の意味）
    s = re.sub(r'[\-\u2212\u30fc\u2010-\u2015]+$', '', s)
    if not s:
        return None
    runs = re.findall(r'\d+(?:\.\d+)?', s)
    if len(runs) != 1:
        return None  # 数字が無い、または範囲・分数のように2つ以上ある
    value = runs[0]
    return ('-' + value) if is_negative else value


def safe_float(val, default=0.0):
    """安全な浮動小数変換"""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_str(val, default=''):
    """安全な文字列変換"""
    if val is None:
        return default
    return str(val)


def read_xml_tag(text, tag_name):
    """XMLタグの現在値を読む。マージモードで「書き込み後の実効値」を
    知りたいときに使う（テンプレートに残る値も含めて突き合わせるため）。"""
    m = re.search(rf'<{re.escape(tag_name)}>([^<]*)</{re.escape(tag_name)}>', text)
    return m.group(1) if m else ''


def replace_xml_tag(text, tag_name, value):
    """XMLタグの中身を現在値に関係なく置換"""
    pattern = rf'<{re.escape(tag_name)}>[^<]*</{re.escape(tag_name)}>'
    replacement = f'<{tag_name}>{value}</{tag_name}>'
    # 置換文字列を生で渡すと、値の中の「\1」が後方参照として解釈されて
    # 落ちる。JIS配列の ¥ キーは U+005C を送るので、備考に「¥1,200」と
    # 打っただけで NEO 生成が失敗していた。lambda で解釈を止める。
    result = re.sub(pattern, lambda _m: replacement, text)
    # 空タグ形式も対応
    empty_pattern = rf'<{re.escape(tag_name)}/>'
    result = result.replace(empty_pattern, replacement)
    return result


def replace_ini_value(text, key, value):
    """INIキー値を確実に更新"""
    # `.` は MULTILINE でも \r に一致するため、`.*$` にすると
    # 書き換えた行だけ CRLF が LF に変わり、テンプレートと改行が混ざる。
    pattern = rf'^({re.escape(key)}\s*=)[^\r\n]*'
    # 上と同じ理由で、値をそのまま置換文字列にしない
    return re.sub(pattern, lambda m: m.group(1) + str(value), text,
                  flags=re.MULTILINE)


# ============================================================
# NEO バイナリ解析
# ============================================================

# CKマーカーの位置を貯める上限。正規のNEOは1500明細でも数百KBで、
# CKは多くても数千個しかない。「CK」を敷き詰めただけのファイルを
# 投げられると、位置のリストが入力の十数倍のメモリを食い、
# 1プロセスを共有する本番では全利用者を巻き添えにして落ちる。
MAX_CK_MARKS = 1_000_000
# テンプレートNEOのアップロード上限。実データは数百KB。
MAX_NEO_UPLOAD_BYTES = 8 * 1024 * 1024


def find_real_cks(data, start=424, max_marks=MAX_CK_MARKS):
    """comp_len連鎖法でCK位置を特定（偽CK除外）"""
    all_ck = []
    for i in range(start, len(data) - 1):
        if data[i] == 0x43 and data[i + 1] == 0x4B:
            all_ck.append(i)
            if len(all_ck) > max_marks:
                raise ValueError(
                    "NEOファイルの構造が異常です（CKマーカーが多すぎます）。"
                    "壊れているか、コグニセブンのNEOファイルではありません。")
    if not all_ck:
        return []
    real_ck = []
    idx = 0
    while idx < len(all_ck):
        ck = all_ck[idx]
        real_ck.append(ck)
        cl = struct.unpack('<H', data[ck - 4:ck - 2])[0]
        exp = ck + cl + 8
        found = False
        for j in range(idx + 1, len(all_ck)):
            if all_ck[j] == exp:
                idx = j
                found = True
                break
        if not found:
            break
    return real_ck


# 展開後サイズの上限。実データは1500明細でも約620KBなので、64MBは十分に余裕がある。
# 上限なしで展開すると、数百KBのNEOが数百MBに膨らむ細工ファイル（展開爆弾）で
# プロセス全体のメモリを枯渇させられる。
MAX_DECOMPRESSED_SIZE = 64 * 1024 * 1024


def decompress_neo(data, real_ck):
    """辞書連鎖展開でrawデータを復元"""
    chunks = []
    total = 0
    for i, ck in enumerate(real_ck):
        start = ck + 2
        end   = real_ck[i + 1] - 8 if i + 1 < len(real_ck) else len(data)
        chunk = data[start:end]
        remaining = MAX_DECOMPRESSED_SIZE - total
        if remaining <= 0:
            raise ValueError(
                f"NEOファイルの展開後サイズが上限（{MAX_DECOMPRESSED_SIZE // (1024*1024)}MB）を超えました。"
                "ファイルが壊れているか、想定外のファイルです。"
            )
        if i == 0:
            dobj = zlib.decompressobj(-15)
        else:
            dobj = zlib.decompressobj(-15, zdict=b''.join(chunks)[-32768:])
        raw = dobj.decompress(chunk, remaining)
        if dobj.unconsumed_tail:
            raise ValueError(
                f"NEOファイルの展開後サイズが上限（{MAX_DECOMPRESSED_SIZE // (1024*1024)}MB）を超えました。"
                "ファイルが壊れているか、想定外のファイルです。"
            )
        if not dobj.eof:
            raise ValueError(
                "NEOファイルの展開が完了しませんでした。ファイルが壊れているか、"
                "コグニセブンのNEOファイルではない可能性があります。"
            )
        chunks.append(raw)
        total += len(raw)
    return b''.join(chunks)


def parse_entries(data, first_ck):
    """管理領域とファイルテーブルを解析"""
    table       = data[424:first_ck]
    first_entry = None
    for i in range(len(table) - 7):
        if table[i + 6] == 0x5C and struct.unpack_from('<H', table, i + 4)[0] == 0x0020:
            first_entry = i
            break
    if first_entry is None:
        raise ValueError("ファイルテーブルのエントリが見つかりません")
    mgmt    = table[:first_entry]
    entries = []
    pos     = first_entry
    while pos < len(table):
        if pos + 6 >= len(table):
            break
        if table[pos + 6] != 0x5C:
            pos += 1
            continue
        dos_bytes = table[pos:pos + 4]
        nul       = table.find(b'\x00', pos + 7)
        if nul == -1:
            break
        fn        = table[pos + 7:nul].decode('cp932', errors='replace')
        remaining = len(table) - (nul + 1)
        if remaining >= 10:
            sz       = struct.unpack_from('<I', table, nul + 1)[0]
            off      = struct.unpack_from('<I', table, nul + 5)[0]
            is_normal = sz < 10_000_000 and off < 10_000_000
        else:
            sz, off, is_normal = None, None, False
        if is_normal:
            entries.append({'name': fn, 'size': sz, 'offset': off, 'is_last': False, 'dos': dos_bytes})
            pos = nul + 11
        else:
            entries.append({'name': fn, 'size': None, 'offset': None, 'is_last': True, 'dos': dos_bytes})
            pos = len(table)
    return mgmt, entries


def extract_files(full_raw, entries):
    """rawデータから12ファイルを切り出し"""
    files        = {}
    normal_total = 0
    for e in entries:
        if not e['is_last']:
            files[e['name']] = full_raw[e['offset']:e['offset'] + e['size']]
            normal_total    += e['size']
    last_entry = [e for e in entries if e['is_last']]
    if not last_entry:
        raise ValueError("最後エントリ（hidden先頭ファイル）が見つかりません")
    last_name         = last_entry[0]['name']
    files[last_name]  = full_raw[0:len(full_raw) - normal_total]
    return files


# ============================================================
# 内部ファイル更新: AnSMB.txt（見積本体SQLite）
# ============================================================

def update_ansmb(db_bytes, items, short_parts_wage, expenses=None, is_tax_inclusive=False, is_beta_mode=False):
    """ERParts/Expense/Total を更新（値引き行の負工賃も対応）
    expenses: {
        'towing': レッカー費用,              # LineNo=5「レッカー代１」
        'rental_car': 代車費用,              # LineNo=7「写真代他」（専用行が無いため）
        'short_parts': ショートパーツ,       # LineNo=4（short_parts_wageと同義）
        'tax_exempt': 非課税費用,            # LineNo=8「その他控除」（消費税なし）
    }
    is_tax_inclusive: True の場合、items の金額は税込値として扱い、
                     OutTax/InTax/Tax を正しく逆算する。
    is_beta_mode: True の場合、ベタ打ちモード（未マッチ部品に※を付与しない）
    """
    if expenses is None:
        expenses = {}
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    try:
        tf.write(db_bytes)
    finally:
        tf.close()
    # 途中で例外が出ても一時ファイル（顧客情報を含む）を残さない
    try:
        return _update_ansmb_impl(tf.name, items, short_parts_wage, expenses,
                                  is_tax_inclusive, is_beta_mode)
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass


def _update_ansmb_impl(_tmp_db_path, items, short_parts_wage, expenses,
                       is_tax_inclusive, is_beta_mode):
    conn = sqlite3.connect(_tmp_db_path)
    cur  = conn.cursor()
    cur.execute('DELETE FROM ERParts')
    # ── 塗装セクション・その他テーブルをリセット ──
    # PaintingPanel: 複数行テーブル（パネル塗装行）→ 全削除
    cur.execute('DELETE FROM PaintingPanel')
    # PaintingLinkParts: 塗装リンクパーツ → 全削除
    cur.execute('DELETE FROM PaintingLinkParts')
    # PaintingOther: 1行固定テーブル。前案件の .neo をテンプレートにすると
    # Name に前案件の作業名（例「前案件のスポイラー塗装」）が残り、
    # 工賃だけブランクの幽霊行として新しい見積に出てしまう。名前も消す。
    cur.execute("""UPDATE PaintingOther SET Name='',
        Time=-1, WageOutTax=-1, WageInTax=-1, WageTax=-1, WageByManual=''""")
    # PaintingTotal: 合計テーブルをゼロリセット
    cur.execute("""UPDATE PaintingTotal SET
        TimeTotalPanel=0, TimeTotalBumper=0, TimeTotalFrame=0,
        TimeTotalEtcetera=0, TimeTotalOther=0, TimeTotal=0,
        WageTotalPanelOutTax=0, WageTotalPanelInTax=0, WageTotalPanelTax=0,
        WageTotalBumperOutTax=0, WageTotalBumperInTax=0, WageTotalBumperTax=0,
        WageTotalFrameOutTax=0, WageTotalFrameInTax=0, WageTotalFrameTax=0,
        WageTotalEtceteraOutTax=0, WageTotalEtceteraInTax=0, WageTotalEtceteraTax=0,
        WageTotalOtherOutTax=0, WageTotalOtherInTax=0, WageTotalOtherTax=0,
        WageTotalOutTax=0, WageTotalInTax=0, WageTotalTax=0, WageTotalByManual='',
        MaterialTotalPanelOutTax=0, MaterialTotalPanelInTax=0, MaterialTotalPanelTax=0,
        MaterialTotalBumperOutTax=0, MaterialTotalBumperInTax=0, MaterialTotalBumperTax=0,
        MaterialTotalFrameOutTax=0, MaterialTotalFrameInTax=0, MaterialTotalFrameTax=0,
        MaterialTotalEtceteraOutTax=0, MaterialTotalEtceteraInTax=0, MaterialTotalEtceteraTax=0,
        MaterialTotalOutTax=0, MaterialTotalInTax=0, MaterialTotalTax=0, MaterialTotalbyManual='',
        TotalOutTax=0, TotalInTax=0, TotalTax=0""")
    # 明細に紐づくテーブルは、前案件の .neo をテンプレートにしたときに
    # 残ると別案件の塗装工賃・損害コメント・リサイクル部品が混入する。
    # ERParts と同じタイミングで必ず消す。
    for _t in ('DamageParts', 'DamageBlock', 'DamageComment', 'DamageImage',
               'RCParts', 'RCLinkParts', 'RWLinkParts', 'EPCLinkParts', 'Frame'):
        try:
            cur.execute(f'DELETE FROM {_t}')
        except sqlite3.Error:
            pass   # テンプレートに無いテーブルは無視する

    # ReserveERParts（予備明細）は ERPartsRecordNo で ERParts を指す。
    # 全消しせずに残すと、前案件の予備行が品名・金額つきで生き残り、
    # しかもその参照先 RecordNo は新しい ERParts に存在しない。
    # ただしテンプレート原本でも「空欄行が1行」あるのが実機の正常な状態なので、
    # 0行にはせず、実機が書いた空行と同じ値へ戻す。
    # 値はテンプレート原本の ReserveERParts 1行をそのまま採取したもの。
    try:
        _r_cols = [c[1] for c in cur.execute('PRAGMA table_info(ReserveERParts)').fetchall()]
    except sqlite3.Error:
        _r_cols = []
    if _r_cols:
        _R_BLANK = {'RecordNo': 1, 'LineNo': 1, 'DisposalCode': 3,
                    'WageByManual': '*', 'ERPartsRecordNo': 0}
        _R_MINUS1 = {'PartsCodeSub', 'PartsPriceOutTax', 'PartsPriceInTax', 'PartsPriceTax',
                     'PartsUnitPriceOutTax', 'PartsUnitPriceInTax', 'PartsUnitPriceTax',
                     'PartsPriceStandardOutTax', 'PartsPriceStandardInTax',
                     'PartsPriceStandardTax', 'Time', 'WageOutTax', 'WageInTax', 'WageTax',
                     'PartsCount', 'ChangeTotalOutTax', 'ChangeTotalInTax', 'ChangeTotalTax',
                     'SATime1', 'SATime2', 'SATime3', 'SATime4', 'SATime5'}
        _R_EMPTY = {'PartsCode', 'DisposalName', 'DisposalNameStandard', 'PartsName',
                    'PartsNameStandard', 'PartsNo', 'PartsNoStandard', 'PartsPriceByManual',
                    'PartsFileTime', 'WorkCode', 'ConstructGroup', 'OrderFlag', 'Provisional',
                    'BlockCode', 'WageFileTime', 'ShapeModifyTime', 'DamageArea', 'DamageRank',
                    'SATime1ByManual', 'SATime2ByManual', 'SATime3ByManual',
                    'SATime4ByManual', 'SATime5ByManual', 'Comment1', 'Comment2', 'Comment3'}
        _vals = []
        for _c in _r_cols:
            if _c in _R_BLANK:   _vals.append(_R_BLANK[_c])
            elif _c in _R_MINUS1: _vals.append(-1)
            elif _c in _R_EMPTY:  _vals.append('')
            else:                 _vals.append(0)   # 残りはすべてフラグ列で 0
        try:
            cur.execute('DELETE FROM ReserveERParts')
            cur.execute('INSERT INTO ReserveERParts ({}) VALUES ({})'.format(
                ', '.join(_r_cols), ', '.join(['?'] * len(_r_cols))), _vals)
        except sqlite3.Error:
            pass
    # 1行固定の塗装テーブルは、PaintingOther と同じくブランク(-1)へ戻す。
    # 列構成がテンプレートによって違うので、時間・工賃・材料の列を
    # 名前で拾って一括で戻す。
    for _t in ('PaintingBumper', 'PaintingFrame', 'PaintingEtcetera'):
        try:
            _cols = [c[1] for c in cur.execute(f'PRAGMA table_info({_t})').fetchall()]
        except sqlite3.Error:
            continue
        if not _cols:
            continue
        # ByManual は「手動入力したか」を表す TEXT(1) のフラグ列で、
        # 値域は '' と '*'。ここに -1 を入れると値域外の2文字 '-1' が
        # 入り、宣言幅も超える。PaintingOther と同じく '' に戻す。
        _blank = [c for c in _cols
                  if ('Time' in c or 'Wage' in c or 'Material' in c or 'Total' in c)
                  and 'ByManual' not in c]
        _flag  = [c for c in _cols if 'ByManual' in c]
        _zero  = [c for c in _cols if 'Disposal' in c]
        _sets  = ([f'{c}=-1' for c in _blank]
                  + [f"{c}=''" for c in _flag]
                  + [f'{c}=0' for c in _zero])
        if _sets:
            try:
                cur.execute(f"UPDATE {_t} SET {', '.join(_sets)}")
            except sqlite3.Error:
                pass

    # 明細を消した以上、その入力条件（計画テーブル）も前案件のまま
    # 残してはいけない。残すと「フレーム修正あり・指数6.5・工賃52,000」
    # のような前案件の条件だけが生き残る。
    try:
        cur.execute("""UPDATE FramePlan SET FrameFlag=0, PartsCode='',
            Time=-1, TimeStandard=-1,
            WageOutTax=-1, WageInTax=-1, WageTax=-1,
            WageStandardOutTax=-1, WageStandardInTax=-1, WageStandardTax=-1,
            WageByManual=''""")
    except sqlite3.Error:
        pass
    try:
        cur.execute("""UPDATE DamageBlockPlan SET
            DamageCode='', FrontArea=0, RearArea=0, AllArea=0""")
    except sqlite3.Error:
        pass
    try:
        cur.execute("""UPDATE PaintingPlan SET
            BoothFlag=0, BoothTime=-1, BoothWageOutTax=-1,
            BoothWageInTax=-1, BoothWageTax=-1, BoothWageByManual='',
            PaintingType=0, PaintingTypeName='', MaterialRate=0,
            TwoToneFlag=0""")
    except sqlite3.Error:
        pass
    # 塗装セクションの「あり」フラグも消す。工賃だけ -1 にすると
    # 「調色あり・工賃ブランク」という説明できない状態になる。
    try:
        cur.execute("""UPDATE PaintingEtcetera SET
            LCColorFlag=0, LCColorRoof=0, TwoCSolidFlag=0, TwoCSolidRoof=0""")
    except sqlite3.Error:
        pass

    # 全Expense行をクリア（LineNo=1〜8: 文字書き/内張り/配線/ショートパーツ/レッカー代１/レッカー代２/写真代他/その他控除）
    # LineNo 9 以降は自由入力の費用行。前案件の .neo をテンプレートに
    # 使うと、そこに書かれた費目と金額がそのまま残る。全行を消す。
    # Name は NameFix で扱いが分かれる。1 は「文字書き費用」等の固定費目名で
    # 消してはいけない。0 は自由入力行で、消さないと出荷テンプレートに入っている
    # 「ｺｰﾃｨﾝｸﾞ修正部再施工」(LineNo=9) が全生成物に付いて回り、
    # 前案件の .neo を使えば前案件の費目名が金額ブランクで残る。
    cur.execute("""UPDATE Expense SET
        OutTaxFlag=0, WageEnabled=0, WageOutTax=0, WageInTax=0, WageTax=0,
        PartsEnabled=0, PartsPriceOutTax=0, PartsPriceInTax=0, PartsPriceTax=0,
        Comment='',
        Name = CASE WHEN NameFix = 1 THEN Name ELSE '' END""")
    # Fixer は金額付きの調整行。前案件の .neo をテンプレートにすると
    # 有効フラグごと残り、別案件の調整額が新しい見積に同居する。
    try:
        cur.execute("UPDATE Fixer SET Name='', Enabled=0, Price=0")
    except sqlite3.Error:
        pass
    total_parts = 0
    annote_rows = []
    # 税込モードの丸め調整用
    total_parts_intax = 0
    total_wages_intax = 0
    _adj_parts_line = None
    _adj_wage_line  = None
    _adj_parts_amount = 0
    _adj_wage_amount  = 0
    total_wages = 0
    for i, item in enumerate(items):
        name   = item.get('name', '')
        
        # Addataのマスタと一致しており、ユーザーがUIで名前を意図的に上書き変更していない場合はマスタ名称と品番を採用
        parts_no = ''
        # 照合側は 'match_level' に "L1".."L4" の文字列を書く。
        # '_match_level'（数値）は旧UIの名残。以前はこちらしか見ておらず、
        # 既定値99が採用されて全行が「未マッチ」扱いになり、
        # Addataに完全一致した部品まで品名の先頭に ※ が付いていた。
        # dict.get の第2引数は「キーが無いとき」しか使われない。明細タブは
        # '_match_level' を必ず 0 で埋めるため、先に '_match_level' を見ると
        # 照合が付けた 'L4' に永久に落ちず、未マッチ部品の ※ が消えていた。
        _ml_raw = item.get('match_level')
        if _ml_raw in (None, '', 'NA'):
            _ml_raw = item.get('_match_level')
        if isinstance(_ml_raw, str) and _ml_raw[:1].upper() == 'L' and _ml_raw[1:].isdigit():
            m_level = int(_ml_raw[1:])
        elif isinstance(_ml_raw, (int, float)):
            m_level = int(_ml_raw)
        else:
            # 照合情報が無い行は「未マッチ」ではない（CSV取り込み等）
            m_level = 0
        if m_level <= 3 and item.get('_master_name'):
            # ユーザーが編集画面でOCR名称をそのままにしていた場合のみマスタ名に置換
            # （手動で全く違う名前に直した場合はそちらを尊重する）
            if name == item.get('_original_name', name) or name == item.get('_master_name'):
                name = item.get('_master_name')
                parts_no = item.get('_master_part_no', '')
        # DBマッチなし（CSV取り込み等）の場合はCSVの部品コードをPartsNoに使用
        if not parts_no:
            parts_no = str(item.get('part_no', '') or '')
        
        # 未マッチ（またはそれに準ずる低マッチレベル）部品には先頭に「※」を付与
        # ベタ打ちモードではDB照合を行わないため※を付けない
        # 品名が空の行に ※ を付けると「※」1文字だけの明細行になる。
        # 説明できない行がコグニセブンと帳票の両方に出るので付けない。
        _needs_mark = (not is_beta_mode and m_level >= 4
                       and name.strip() and not name.startswith('※'))
        if _needs_mark:
            # ※ の2バイトぶん先に詰めてから付ける。後から付けると
            # 列幅24バイトの切り詰めで品名の末尾が余計に落ちる
            # （「…カバー下部」が「…カバー」になり別部品に読める）。
            _avail = _ERPARTS_WIDTH['PartsName'] - 2
            # 末尾の左右は最後まで残す。列幅ちょうどの品名だと
            # 「フロントバンパーカバー左」と「…右」が両方
            # 「※フロントバンパーカバー」になり、左右の部品が
            # 同じ文字列で並ぶ。しかも未マッチ行、つまり利用者が
            # 最も見分ける必要のある行で起きる。
            # 「左側」「右側」のように左右の後ろに1字続く形も拾う。
            _m_side = re.search(
                r'[（(\[]?\s*(左|右|Ｌ|Ｒ|LH|RH|L|R)\s*(側|前|後)?\s*[)）\]]?$', name)
            if _m_side and len(name.encode('cp932', 'replace')) > _avail:
                # 正規表現の先頭が \s* なので、re.search は左右記号の直前の
                # 空白の連なりからマッチする。そのまま温存すると22バイトの
                # 持ち分を空白が食い、識別に必要な語尾から先に消える。
                # 「…アウタ R」と「…インナ R」が両方「※フロントドアパネル  R」
                # になり、別部品が同じ文字列で並ぶ。空白は落として詰める。
                _side_txt = re.sub(r'\s+', '', name[_m_side.start():])
                _side_len = len(_side_txt.encode('cp932', 'replace'))
                _body = re.sub(r'\s+', '', name[:_m_side.start()])
                name = cp932_trim(_body, max(_avail - _side_len, 0)) + _side_txt
            name = '※' + cp932_trim(name, _avail)
        
        # 区分: work_code（Markdownパーサー保存先）または method から取得
        method = item.get('method', '') or item.get('work_code', '')
        # 明細もコグニセブンの宣言列幅（CP932バイト）に収める。
        # 顧客欄と同じ理由で、ここで守らないと桁あふれした値が入る。
        # 「フロントバンパーカバーASSY」程度の普通の部品名で超える。
        name     = cp932_trim(name, _ERPARTS_WIDTH['PartsName'])
        parts_no = cp932_trim(parts_no, _ERPARTS_WIDTH['PartsNo'])

        # 品名から作業種別を自動推定（区分が空白の場合）
        # ただし「※金額調整」は自動で足した差額の行で、作業ではない。
        # 推定に掛けると「調整」の2文字が修理系の語に当たって
        # 「修理」区分の部品行になり、コグニセブン上で説明できない行になる。
        if not method and not str(item.get('name', '')).startswith('※金額調整'):
            _name_for_detect = str(item.get('name', ''))
            _parts_amt = safe_int(item.get('parts_amount', 0))
            _wage_amt  = safe_int(item.get('wage', 0))
            # ルール7: 研磨・磨き・写真代・ショートパーツは空白のまま（最優先）
            if any(kw in _name_for_detect for kw in ('研磨', '磨き', '写真代', 'ショートパーツ')):
                method = ''
            # ルール1: 部品金額あり・工賃なし → 取替
            elif _parts_amt > 0 and _wage_amt == 0:
                method = '取替'
            elif any(kw in _name_for_detect for kw in ('取替', '交換', '取換', '取り替え')):
                method = '取替'
            elif any(kw in _name_for_detect for kw in ('脱着', '取外', '取付', '組付', '脱外')):
                method = '脱着'
            elif any(kw in _name_for_detect for kw in ('鈑金', '板金')):
                method = '鈑金'
            elif any(kw in _name_for_detect for kw in ('塗装', 'ペイント', 'ワックス', '加算', 'ブース')):
                method = '塗装'
            elif any(kw in _name_for_detect for kw in (
                '修理', '補修', '分解', '修正',
                '光軸', 'フィッティング', 'コーディング', '穴あけ',
                'シーリング', '点検', '消去', '設定', '調整',
            )):
                method = '修理'

        qty    = safe_int(item.get('quantity', 1), 1)
        if qty < 1:
            qty = 1
        if 'parts_amount' in item:
            parts_total = safe_int(item.get('parts_amount', 0))
        else:
            unit_price  = safe_int(item.get('unit_price', 0))
            parts_total = unit_price * qty
            
        # (変更: 自動的に _master_price で上書きしないことで、OCRの合計額と常に一致させる運用とする)
                
        wage     = safe_int(item.get('wage', 0))
        rec_no   = i + 1
        line_no  = rec_no * 10
        wage_total  = wage
        if is_tax_inclusive:
            # 税込: 金額は既に税込値 → OutTax=税抜逆算, InTax=そのまま, Tax=差額
            if parts_total != 0:
                parts_outtax = jpy_round(parts_total / (1 + TAX_RATE))
                parts_intax  = parts_total
                parts_tax    = parts_total - parts_outtax
            else:
                parts_outtax = 0; parts_intax = 0; parts_tax = 0
            if wage_total != 0:
                wage_outtax  = jpy_round(abs(wage_total) / (1 + TAX_RATE))
                if wage_total < 0:
                    wage_outtax = -wage_outtax
                wage_intax   = wage_total
                wage_tax     = wage_total - wage_outtax
            else:
                wage_outtax = 0; wage_intax = 0; wage_tax = 0
        else:
            # 税抜: 従来通り
            parts_outtax = parts_total
            parts_tax    = jpy_round(parts_total * TAX_RATE) if parts_total != 0 else 0
            parts_intax  = parts_total + parts_tax if parts_total != 0 else 0
            wage_outtax  = wage_total
            wage_tax_abs = jpy_round(abs(wage_total) * TAX_RATE) if wage_total != 0 else 0
            wage_tax     = wage_tax_abs if wage_total >= 0 else -wage_tax_abs
            wage_intax   = wage_total + wage_tax if wage_total != 0 else 0
        total_parts += parts_outtax
        total_wages += wage_outtax
        # 税込モードでは行ごとに税抜を逆算するため、丸め誤差が積み上がって
        # 見積書に書かれた税込総額と生成NEOの合計がずれる。合計から1回で
        # 逆算し直せるよう、税込の合計と、差額を寄せる行を覚えておく。
        total_parts_intax += parts_intax
        total_wages_intax += wage_intax
        if parts_total != 0 and abs(parts_outtax) >= abs(_adj_parts_amount):
            _adj_parts_amount = parts_outtax
            _adj_parts_line   = line_no
        if wage_total != 0 and abs(wage_outtax) >= abs(_adj_wage_amount):
            _adj_wage_amount = wage_outtax
            _adj_wage_line   = line_no
        # コグニセブンは -1 を空白として表示する（0やNULLは「0」と表示される）
        db_parts_total = parts_outtax if parts_total != 0 else -1
        db_parts_intax = parts_intax  if parts_total != 0 else -1
        db_parts_tax   = parts_tax    if parts_total != 0 else -1
        db_wage_total  = wage_outtax  if wage_total  != 0 else -1
        db_wage_intax  = wage_intax   if wage_total  != 0 else -1
        db_wage_tax    = wage_tax     if wage_total  != 0 else -1
        # 数量は原本の値をそのまま書く。以前は部品金額が0の行を -1（空白）に
        # していたが、画面のプレビューには数量3と出たまま .neo には入らず、
        # 同じ .neo の中の AnNote は -1 を 1 に読み替えるため、同じ行の数量が
        # 画面・明細テーブル・注記で3通りになっていた。「クリップ脱着 3個」の
        # ように工賃行でも数量に意味がある。原本と同じ値を残す。
        db_qty = qty
        # ── Addata マスタ照合結果から PartsCode / PartsCodeSub / DisposalCode を設定 ──
        # 区分の判定は切り詰める前の文字列で行う。先に8バイトへ切ると
        # 「ｱｯｾﾝﾌﾞﾘ取替」から「取替」が落ちて区分不明になる。
        _method_full = method
        method = cp932_trim(method, _ERPARTS_WIDTH['DisposalName'])
        # 区分コードは 0=取替 / 1=脱着 / 2=修理(鈑金・塗装含む)。
        # 実機NEO 244件を解析して確定した値で、_addata_db_search.py の
        # 冒頭「検証結果」節と auto_matching.py:1877 が同じ対応を使う。
        # 3 は「区分なし」の番兵で、テンプレートの空行が実際に 3 を持つ。
        _disposal_map = {
            '取替': 0, '交換': 0, '取換': 0, '取り替え': 0, '取替え': 0,
            '脱着': 1, '取外': 1, '取付': 1, '組付': 1, '脱外': 1,
            '修理': 2, '補修': 2, '分解': 2, '修正': 2, '調整': 2,
            '光軸': 2, 'フィッティング': 2, 'コーディング': 2, '穴あけ': 2,
            'シーリング': 2, '点検': 2, '消去': 2, '設定': 2,
            '鈑金': 2, '板金': 2, '塗装': 2, 'ペイント': 2, 'ワックス': 2, '加算': 2, 'ブース': 2,
        }
        # 区分は完全一致だけで引くと、末尾に空白が付いただけ、
        # 「脱着（左）」のように補足が付いただけで -1（区分不明）になる。
        # 記号や括弧書きを落として正規化し、それでも決まらなければ
        # 部分一致に落とす。CSV取込では区分欄が埋まっているのが普通なので、
        # ここで取りこぼすと全行が区分不明になる。
        _m_key = unicodedata.normalize('NFKC', str(_method_full or ''))
        _m_key = re.sub(r'[（(\[【][^）)\]】]*[）)\]】]?', '', _m_key)
        _m_key = re.sub(r'[\s\u3000※*・/／,、]', '', _m_key).strip()
        disposal_code = _disposal_map.get(_m_key, -1)
        if disposal_code == -1 and _m_key:
            for _kw, _cd in _disposal_map.items():
                if _kw in _m_key:
                    disposal_code = _cd
                    break
        # 指数（工数）。画面まで往復させておきながら NEO には書いていなかったため、
        # コグニセブン側では全行が指数ゼロの見積として開かれていた。
        # 単位は時間の小数（1.0 = 100WI, _addata_db_search.match_wage_by_time 参照）。
        # 素の float() だと全角「１．５」「(0.8)」「1.5h」を落とし、
        # 同じ行の全角金額は読めるのに指数だけ欠ける。金額と同じ正規化を通す。
        # （auto_matching も同じ index_value を正規化して工数照合に使っている）
        _idx_raw = _normalize_number_text(item.get('index_value', ''))
        try:
            _idx = float(_idx_raw) if _idx_raw is not None else 0.0
        except (TypeError, ValueError):
            _idx = 0.0
        if not math.isfinite(_idx):
            _idx = 0.0          # 'inf' がそのままDBに入るのを防ぐ
        # 丸めてから判定する。先に判定すると 0.001 が Time=0 として書かれ、
        # すぐ下のコメントが戒めている「指数ゼロの見積」を自分で作ってしまう。
        _idx = round(_idx, 2)
        db_time = _idx if _idx > 0 else -1   # 未入力・負値は -1（空欄）
        parts_code = item.get('_master_section_code', '')  # 部品コード大区分（例: '01'）
        _branch_raw = item.get('_master_branch_code', '')  # 枝番（例: '00101', '001AA'）
        # PartsCodeSub は SQLite integer 型。数値変換できる枝番のみ整数で保存
        try:
            parts_code_sub = int(_branch_raw) if _branch_raw and _branch_raw.isdigit() else -1
        except Exception:
            parts_code_sub = -1
        cur.execute("""INSERT INTO ERParts (
            RecordNo, LineNo, PartsCode, PartsCodeSub, DisposalCode,
            DisposalName, DisposalNameStandard, PartsName, PartsNameStandard,
            PartsNo, PartsNoStandard,
            PartsPriceOutTax, PartsPriceInTax, PartsPriceTax,
            PartsUnitPriceOutTax, PartsUnitPriceInTax, PartsUnitPriceTax,
            PartsPriceStandardOutTax, PartsPriceStandardInTax, PartsPriceStandardTax,
            PartsPriceByManual,
            Time, TimeStandard,
            WageOutTax, WageInTax, WageTax,
            WageStandardOutTax, WageStandardInTax, WageStandardTax,
            WageByManual, PartsCount,
            ChangeTotalOutTax, ChangeTotalInTax, ChangeTotalTax,
            PartsFileTime, WorkCode, ConstructGroup,
            OrderFlag, Provisional, PartsPriceFlag, DuplicateFlag,
            BlockCode, WageFileTime, ShapeModifyTime,
            DamageArea, DamageRank,
            DamageRankBtn1, DamageRankBtn2, DamageRankBtn3,
            SATime1, SATime1ByManual, SATime1Flag,
            SATime2, SATime2ByManual, SATime2Flag,
            SATime3, SATime3ByManual, SATime3Flag,
            SATime4, SATime4ByManual, SATime4Flag,
            SATime5, SATime5ByManual, SATime5Flag,
            BlockListFlag, RecycleFlag, RCRecordNo,
            ReserveFlag, ReserveRecordNo,
            CommentFlag, Comment1, Comment2, Comment3, RWLinkFlag
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, '', ?, '',
            ?, '',
            ?, ?, ?,
            -1, -1, -1,
            -- 空欄は -1。NULL や 0 にすると帳票に「0」と表示され、
            -- 標準部品価格ゼロ・標準指数ゼロの見積として読まれてしまう。
            -1, -1, -1,
            '*',
            ?, -1,
            ?, ?, ?,
            -1, -1, -1,
            '*', ?,
            -1, -1, -1,
            '', '', '',
            '9', '', 0, 0,
            '', '', '',
            '', '',
            0, 0, 0,
            -1, '', 0,
            -1, '', 0,
            -1, '', 0,
            -1, '', 0,
            -1, '', 0,
            0, 0, 0,
            0, 0,
            0, '', '', '', 0
        )""", (
            rec_no, line_no, parts_code, parts_code_sub, disposal_code,
            method, name,
            parts_no,
            db_parts_total, db_parts_intax, db_parts_tax,
            db_time,
            db_wage_total, db_wage_intax, db_wage_tax,
            db_qty
        ))
        # AnNote.ini は ERParts と同じ値でなければならない。生の items から
        # 別に組み立てると、マスタ名への置換・「※」付与・数量ブランクが
        # 反映されず、同じ行なのに品名と数量が2通り存在することになる。
        annote_rows.append({'line_no': line_no, 'name': name, 'qty': db_qty})
    # ── 税込/税抜に応じた費用計算ヘルパー ──
    def _calc_tax(amount, inclusive=False):
        """金額から OutTax, InTax, Tax を計算"""
        if amount == 0:
            return 0, 0, 0
        if inclusive:
            outtax = jpy_round(amount / (1 + TAX_RATE))
            intax  = amount
            tax    = amount - outtax
        else:
            outtax = amount
            tax    = jpy_round(amount * TAX_RATE)
            intax  = amount + tax
        return outtax, intax, tax

    # ── Expense各行を更新 ──
    # LineNo=4: ショートパーツ
    sp_wage = safe_int(short_parts_wage)
    # サイドバーの費用欄は「（税抜）」と明示しているため、明細の税区分に
    # かかわらず常に税抜として扱う。以前は税込モードで9.1%目減りしていた。
    sp_out, sp_intax, sp_tax = _calc_tax(sp_wage, False)
    cur.execute("""UPDATE Expense SET
        WageEnabled=?, WageOutTax=?, WageInTax=?, WageTax=?
        WHERE LineNo=4""", (1 if sp_wage > 0 else 0, sp_out, sp_intax, sp_tax))

    # Expense の行名は NameFix=1 の固定名で、コグニセブンはその名前のまま
    # 表示する。以前はレッカーを LineNo=1（文字書き費用）、代車を
    # LineNo=2（内張り費用）、非課税を LineNo=5（レッカー代１）に
    # 書き込んでいたため、金額は合っていても費目名が全部別物だった。

    # LineNo=5: レッカー代１（課税）
    towing = safe_int(expenses.get('towing', 0))
    tow_out, tow_intax, tow_tax = _calc_tax(towing, False)
    cur.execute("""UPDATE Expense SET
        WageEnabled=?, WageOutTax=?, WageInTax=?, WageTax=?
        WHERE LineNo=5""", (1 if towing > 0 else 0, tow_out, tow_intax, tow_tax))

    # LineNo=7: 写真代他（課税）。テンプレートに代車専用の行が無いため、
    # 汎用の「その他課税費用」行に入れ、Comment に費目を書き添える。
    rental_car = safe_int(expenses.get('rental_car', 0))
    rent_out, rent_intax, rent_tax = _calc_tax(rental_car, False)
    cur.execute("""UPDATE Expense SET
        WageEnabled=?, WageOutTax=?, WageInTax=?, WageTax=?, Comment=?
        WHERE LineNo=7""", (1 if rental_car > 0 else 0, rent_out, rent_intax, rent_tax,
                            cp932_trim('代車費用', 30) if rental_car > 0 else ''))

    # LineNo=8: その他控除（非課税）。OutTaxFlag で非課税であることを示す。
    tax_exempt = safe_int(expenses.get('tax_exempt', 0))
    cur.execute("""UPDATE Expense SET
        OutTaxFlag=?, WageEnabled=?, WageOutTax=?, WageInTax=?, WageTax=?, Comment=?
        WHERE LineNo=8""", (1 if tax_exempt > 0 else 0, 1 if tax_exempt > 0 else 0,
                            tax_exempt, tax_exempt, 0,
                            cp932_trim('非課税費用', 30) if tax_exempt > 0 else ''))

    # ── 税込モードの丸め調整 ──
    # 行ごとの逆算をそのまま足すと、見積書の税込総額と生成NEOの合計が
    # 1〜5円ずれる（明細が増えるほど外れる）。総額から1回で逆算した値を
    # 正とし、差額を最も金額の大きい行に寄せて、行と合計の整合を保つ。
    if is_tax_inclusive:
        def _outtax_from_intax(v):
            if not v:
                return 0
            o = jpy_round(abs(v) / (1 + TAX_RATE))
            return -o if v < 0 else o

        # 部品と工賃を別々に丸めると、その2つの誤差がさらに積み上がる。
        # 明細ぶんの税抜合計 S を「S + 消費税 が見積書の税込総額に一致する」
        # ように選び直してから、部品→工賃の順に差額を割り当てる。
        _items_intax = total_parts_intax + total_wages_intax
        _base = _outtax_from_intax(_items_intax)
        _best, _best_err = _base, None
        for _off in (0, -1, 1, -2, 2):
            _cand = _base + _off
            _err = abs(_cand + jpy_round(_cand * TAX_RATE) - _items_intax)
            if _best_err is None or _err < _best_err:
                _best, _best_err = _cand, _err
            if _err == 0:
                break
        _target_parts = _outtax_from_intax(total_parts_intax)
        _target_wages = _best - _target_parts
        # 寄せ先の行が無い側には差額を割り当てられないので、もう一方に回す
        if _adj_parts_line is None:
            _target_wages = _best
            _target_parts = total_parts
        elif _adj_wage_line is None:
            _target_parts = _best
            _target_wages = total_wages

        # 差額は1行に寄せず、全行に1円ずつ配る。
        # 1行に寄せると、行ごとの逆算で積み上がった誤差がまるごとそこに乗り、
        # 明細が増えるほどその1行だけ税抜額が原本から離れる
        # （1,000行の見積で1行が455円ずれた）。同じ部品・同じ税込額なのに
        # 1行だけ単価が違う見積になり、協定の場で説明できない。
        # 税込額の大きい行から順に1円ずつ配れば、どの行も自然な逆算値から
        # ±1円以内に収まり、合計は厳密に一致する（最大剰余法と同じ考え方）。
        for _target, _cur, _col_out, _col_in, _col_tax in (
            (_target_parts, total_parts,
             'PartsPriceOutTax', 'PartsPriceInTax', 'PartsPriceTax'),
            (_target_wages, total_wages,
             'WageOutTax', 'WageInTax', 'WageTax'),
        ):
            _delta = _target - _cur
            if _delta == 0:
                continue
            # 金額の入っている行を、税込額の大きい順に並べる
            _rows = cur.execute(
                f'SELECT LineNo, {_col_out}, {_col_in} FROM ERParts'
                f' WHERE {_col_out} IS NOT NULL AND {_col_out} != -1'
                f' ORDER BY ABS({_col_in}) DESC, LineNo ASC').fetchall()
            if not _rows:
                continue
            _step = 1 if _delta > 0 else -1
            _i = 0
            while _delta != 0 and _i < abs(_target - _cur) + len(_rows):
                _ln, _out, _in = _rows[_i % len(_rows)]
                _new_out = _out + _step
                cur.execute(
                    f'UPDATE ERParts SET {_col_out}=?, {_col_tax}=? WHERE LineNo=?',
                    (_new_out, _in - _new_out, _ln))
                _rows[_i % len(_rows)] = (_ln, _new_out, _in)
                _delta -= _step
                _i += 1
            _applied = _target - _cur - _delta
            if _col_out == 'PartsPriceOutTax':
                total_parts += _applied
            else:
                total_wages += _applied

    # ── Total計算 ──
    # total_parts / total_wages は既に税抜値（is_tax_inclusive時は逆算済み）
    taxable_expenses = sp_out + tow_out + rent_out
    sub_total         = total_parts + total_wages + taxable_expenses
    parts_tax_total   = jpy_round(total_parts * TAX_RATE)
    wages_tax_total   = jpy_round(total_wages * TAX_RATE)
    sp_tax_total      = jpy_round(sp_out * TAX_RATE)
    expenses_tax_total = jpy_round(taxable_expenses * TAX_RATE)
    # 消費税は請求書単位で1回だけ丸める。これが見積書に印字された税込総額の
    # 作り方であり、画面もこの刻みで出している。
    # バケットごとに丸めて足すと、約4件に1件で原本の税込総額から1円離れる。
    # 税込表記のときは、上で「税を足すと原本の税込総額に戻る」税抜額を
    # わざわざ探索しているので、明細ぶんと費用ぶんを分けて丸めないと
    # その探索の成果が壊れる。
    if is_tax_inclusive:
        tax_total = (jpy_round((total_parts + total_wages) * TAX_RATE)
                     + expenses_tax_total)
    else:
        tax_total = jpy_round(sub_total * TAX_RATE)
    # 内訳の税額欄（部品計・工賃計・諸経費計）の合計は tx_Total と
    # 一致していなければならない。まとめ丸めとの差を、いちばん金額の
    # 大きい欄に寄せて整合を保つ。
    _tax_resid = tax_total - (parts_tax_total + wages_tax_total + expenses_tax_total)
    if _tax_resid:
        _biggest = max((abs(total_parts), 'p'), (abs(total_wages), 'w'),
                       (abs(taxable_expenses), 'e'))[1]
        if _biggest == 'p':
            parts_tax_total += _tax_resid
        elif _biggest == 'w':
            wages_tax_total += _tax_resid
        else:
            expenses_tax_total += _tax_resid
    grand_total       = sub_total + tax_total + tax_exempt  # 非課税は税計算後に加算
    cur.execute("""UPDATE Total SET
        ms_PartsTotalOutTax=?,
        ms_PartsTotalInTax=?,
        ms_PartsTotalTax=?,
        ms_WageTotalOutTax=?,
        ms_WageTotalInTax=?,
        ms_WageTotalTax=?,
        hy_WageTaxTotalOutTax=?,
        hy_WageTaxTotalInTax=?,
        hy_WageTaxTotalTax=?,
        hy_PartsNoTaxTotalOutTax=?,
        hy_PartsNoTaxTotalInTax=?,
        hy_PartsNoTaxTotalTax=?,
        hy_WageNoTaxTotalOutTax=?,
        hy_WageNoTaxTotalInTax=?,
        hy_WageNoTaxTotalTax=?,
        hy_Wrecker1OutTax=?,
        hy_Wrecker1InTax=?,
        hy_Wrecker1Tax=?,
        hy_Wrecker1TaxFlag=?,
        -- このアプリが値を持たない集計欄。テンプレートの既定値へ戻す。
        -- 戻さないと、過去案件の .neo をテンプレートに使ったとき
        -- 前の案件の塗装費・材料費・リサイクル部品費・掛率割増が
        -- 金額付きで残り、内訳と合計が一致しない見積になる。
        ms_RecyclePartsTotalOutTax=0, ms_RecyclePartsTotalInTax=0,
        ms_RecyclePartsTotalTax=0,
        pn_TotalOutTax=0, pn_TotalInTax=0, pn_TotalTax=0,
        pn_MaterialTotalOutTax=0, pn_MaterialTotalInTax=0, pn_MaterialTotalTax=0,
        nk_TotalOutTax=0, nk_TotalInTax=0, nk_TotalTax=0,
        hy_PartsTaxTotalOutTax=0, hy_PartsTaxTotalInTax=0, hy_PartsTaxTotalTax=0,
        hy_Wrecker2OutTax=0, hy_Wrecker2InTax=0, hy_Wrecker2Tax=0,
        hy_Wrecker2TaxFlag=0,
        pt_ExtraTotalOutTax=0, pt_ExtraTotalInTax=0, pt_ExtraTotalTax=0,
        pt_ExtraRate=-1, pt_ExtraFlag=0, pt_ExtraUnit=1,
        pt_ExtraArrangeFlag=1, pt_IncludeRecycle=1,
        wg_ExtraTotalOutTax=0, wg_ExtraTotalInTax=0, wg_ExtraTotalTax=0,
        wg_ExtraRate=-1, wg_ExtraFlag=0, wg_ExtraUnit=1,
        wg_ExtraArrangeFlag=1, wg_IncludeMaterial=1,
        tx_TotalOutTax=?,
        tx_TotalInTax=?,
        SubTotal=?,
        Total=?
    """, (
        total_parts, total_parts + parts_tax_total, parts_tax_total,
        total_wages, total_wages + wages_tax_total, wages_tax_total,
        taxable_expenses,
        taxable_expenses + expenses_tax_total if taxable_expenses > 0 else 0,
        expenses_tax_total if taxable_expenses > 0 else 0,
        # 非課税ぶんは工賃側の非課税欄に計上する。どの内訳にも入れないと
        # 小計＋消費税が合計に届かず、帳票の検算が合わなくなる。
        0, 0, 0,
        tax_exempt, tax_exempt, 0,
        # レッカーは専用欄にも入れる
        tow_out, tow_intax, tow_tax, (1 if towing > 0 else 0),
        tax_total,   tax_total,
        sub_total,   grand_total
    ))
    conn.commit()
    conn.close()
    with open(_tmp_db_path, 'rb') as f:
        result = f.read()
    return result, total_parts, total_wages, grand_total, annote_rows


# ============================================================
# 内部ファイル更新: AnSvEm0001Ex.db（顧客・車両・保険）
# ============================================================

def update_em_db(db_bytes, cust, insurance_info, estimated_date, is_tax_inclusive=False, merge_mode=False):
    """Customer/FileInfo/Insurance/Setting テーブルを更新
    merge_mode=True の場合、OCRで取得した非空の値のみでテンプレートの既存値を上書きする。
    空値のフィールドはテンプレートNEOの値を保持する。
    """
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    try:
        tf.write(db_bytes)
    finally:
        tf.close()
    try:
        return _update_em_db_impl(tf.name, cust, insurance_info, estimated_date,
                                  is_tax_inclusive, merge_mode)
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass


# AnSMB.txt の ERParts の宣言列幅（CP932バイト数）
_ERPARTS_WIDTH = {'DisposalName': 8, 'PartsName': 24, 'PartsNo': 18}

# AnSvEm0001Ex.db の宣言列幅（CP932バイト数）
_CUST_WIDTH = {
    'Name1': 30, 'UserName': 20, 'OwnerName': 20, 'PostalNo': 10,
    'Prefecture': 8, 'Municipality': 30, 'AddressOther1': 30,
    'CarRegNoDepartment': 8, 'CarRegNoDivision': 6, 'CarRegNoBusiness': 4,
    'CarRegNoSerial': 10, 'CarSerialNo': 41, 'CarMouldNo': 5, 'CarKindNo': 4,
}
_CAR_WIDTH = {
    'CarName': 50, 'CarNameByUser': 50, 'ColorCode': 12,
    'ColorName': 30, 'TrimCode': 6,
}


def _queue_step2_msg(kind: str, text: str):
    """ステップ②の通知を、ステップ③で表示できるように預ける。

    ステップ②は解析後すぐ st.rerun() でステップ③へ進むため、
    その場で st.warning しても画面には一瞬も残らない。車検証を
    読み取れなかったことが利用者に一切伝わらず、空欄のまま
    .neo が作られてしまうのを防ぐ。
    """
    try:
        st.session_state.setdefault('_step2_msgs', []).append((kind, text))
    except Exception:
        pass


def _trimmed_cust_values(cust: dict) -> dict:
    """DB・ヘッダXML・INI で同じ値を書くための、列幅で切り詰め済みの束。

    片側だけ切り詰めると、1つの .neo の中で使用者名や車名が
    2種類存在する状態になってしまう。
    """
    cust = cust or {}
    return {
        'customer_name': cp932_trim(_strip_control_chars(cust.get('customer_name', '')), _CUST_WIDTH['Name1']),
        'user_name':     cp932_trim(_strip_control_chars(cust.get('customer_name', '')), _CUST_WIDTH['UserName']),
        'owner_name':    cp932_trim(_strip_control_chars(cust.get('owner_name', '')),    _CUST_WIDTH['OwnerName']),
        'postal_no':     cp932_trim(_strip_control_chars(cust.get('postal_no', '')),     _CUST_WIDTH['PostalNo']),
        'prefecture':    cp932_trim(_strip_control_chars(cust.get('prefecture', '')),    _CUST_WIDTH['Prefecture']),
        'municipality':  cp932_trim(_strip_control_chars(cust.get('municipality', '')),  _CUST_WIDTH['Municipality']),
        'address_other': cp932_trim(_strip_control_chars(cust.get('address_other', '')), _CUST_WIDTH['AddressOther1']),
        'car_dept':      cp932_trim(_strip_control_chars(cust.get('car_reg_department', '')), _CUST_WIDTH['CarRegNoDepartment']),
        'car_div':       cp932_trim(_strip_control_chars(cust.get('car_reg_division', '')),   _CUST_WIDTH['CarRegNoDivision']),
        'car_biz':       cp932_trim(_strip_control_chars(cust.get('car_reg_business', '')),   _CUST_WIDTH['CarRegNoBusiness']),
        'car_serial':    cp932_trim(_strip_control_chars(cust.get('car_reg_serial', '')),     _CUST_WIDTH['CarRegNoSerial']),
        'car_serial_no': cp932_trim(_strip_control_chars(cust.get('car_serial_no', '')),      _CUST_WIDTH['CarSerialNo']),
        'model_desig':   cp932_trim(_strip_control_chars(cust.get('car_model_designation', '')), _CUST_WIDTH['CarMouldNo']),
        'category_num':  cp932_trim(_strip_control_chars(cust.get('car_category_number', '')),   _CUST_WIDTH['CarKindNo']),
        'car_name':      cp932_trim(_strip_control_chars(cust.get('car_name', '')),      _CAR_WIDTH['CarName']),
        'body_color':    cp932_trim(_strip_control_chars(cust.get('body_color', '')),    _CAR_WIDTH['ColorName']),
        'color_code':    cp932_trim(_strip_control_chars(cust.get('color_code', '')),    _CAR_WIDTH['ColorCode']),
        'trim_code':     cp932_trim(_strip_control_chars(cust.get('trim_code', '')),     _CAR_WIDTH['TrimCode']),
    }


def _update_em_db_impl(_tmp_db_path, cust, insurance_info, estimated_date,
                       is_tax_inclusive, merge_mode):
    conn = sqlite3.connect(_tmp_db_path)
    cur  = conn.cursor()
    # コグニセブンの列幅（CP932バイト数）に合わせて切り詰める。
    # SQLite は TEXT(n) を強制しないため、ここで守らないと桁あふれした値が
    # そのまま入る。同じ束をヘッダXML・INIでも使い、表記を一致させる。
    _t = _trimmed_cust_values(cust)
    customer_name = _t['customer_name']
    user_name     = _t['user_name']
    owner_name    = _t['owner_name']
    postal_no     = _t['postal_no']
    prefecture    = _t['prefecture']
    municipality  = _t['municipality']
    address_other = _t['address_other']
    car_dept      = _t['car_dept']
    car_div       = _t['car_div']
    car_biz       = _t['car_biz']
    car_serial    = _t['car_serial']
    car_serial_no = _t['car_serial_no']
    car_name       = _t['car_name']
    car_model      = safe_str(cust.get('car_model', ''))
    engine_model   = safe_str(cust.get('engine_model', ''))
    body_color     = _t['body_color']
    color_code     = _t['color_code']
    trim_code      = _t['trim_code']
    car_weight     = safe_int(cust.get('car_weight', 0))
    displacement   = safe_int(cust.get('engine_displacement', 0))
    model_desig    = _t['model_desig']
    category_num   = _t['category_num']
    kilometer      = safe_int(cust.get('kilometer', -1), -1)
    # 「2026/03/01」のような区切り付きをそのまま通すと、和暦の組み立てで
    # int('/0') となり NEO 生成が丸ごと失敗する。必ず正規化してから使う。
    term_date      = _normalize_date8(cust.get('term_date', '')) or '00000000'
    car_reg_date   = _normalize_ym8(cust.get('car_reg_date', '')) or '00000000'
    term_era, term_era_year = get_era_info(term_date)
    reg_era,  reg_era_year  = get_era_info(car_reg_date)

    if merge_mode:
        # マージモード: 非空の値のみでテンプレートの既存値を上書き
        _cust_updates = []
        _cust_values  = []
        _field_map = [
            ('Name1', customer_name), ('UserName', user_name), ('OwnerName', owner_name),
            # 住所欄も書き込む（画面に入力欄があるのに反映されないと分かりにくいため）
            ('PostalNo', postal_no), ('Prefecture', prefecture),
            ('Municipality', municipality), ('AddressOther1', address_other),
            ('CarRegNoDepartment', car_dept), ('CarRegNoDivision', car_div),
            ('CarRegNoBusiness', car_biz), ('CarRegNoSerial', car_serial),
            ('CarSerialNo', car_serial_no), ('CarMouldNo', model_desig), ('CarKindNo', category_num),
        ]
        for col, val in _field_map:
            if val:  # 非空のみ上書き
                _cust_updates.append(f'{col}=?')
                _cust_values.append(val)
        # 日付系: 有効な日付（00000000以外）のみ上書き
        if term_date != '00000000':
            _cust_updates += ['TermDate=?', 'TermEra=?', 'TermEraYear=?']
            _cust_values  += [term_date, term_era, term_era_year]
        if car_reg_date != '00000000':
            _cust_updates += ['CarRegDate=?', 'CarRegEra=?', 'CarRegEraYear=?']
            _cust_values  += [car_reg_date, reg_era, reg_era_year]
        if kilometer >= 0:
            _cust_updates.append('Kilometer=?')
            _cust_values.append(kilometer)
        if _cust_updates:
            cur.execute(f"UPDATE Customer SET {', '.join(_cust_updates)}", _cust_values)
    else:
        # 通常モード: 全フィールドを上書き
        cur.execute('''UPDATE Customer SET
            Name1=?, UserName=?, OwnerName=?,
            PostalNo=?, Prefecture=?, Municipality=?, AddressOther1=?,
            CarRegNoDepartment=?, CarRegNoDivision=?,
            CarRegNoBusiness=?, CarRegNoSerial=?,
            CarSerialNo=?, CarMouldNo=?, CarKindNo=?,
            TermDate=?, TermEra=?, TermEraYear=?,
            CarRegDate=?, CarRegEra=?, CarRegEraYear=?,
            Kilometer=?
        ''', (
            customer_name, user_name, owner_name,
            postal_no, prefecture, municipality, address_other,
            car_dept, car_div, car_biz, car_serial,
            car_serial_no, model_desig, category_num,
            term_date, term_era, term_era_year,
            car_reg_date, reg_era, reg_era_year,
            kilometer
        ))

    # Car テーブル更新（車名・カラーコード・トリムコード） — 非空の値のみ更新（通常・マージ共通）
    car_cols = {row[1] for row in cur.execute("PRAGMA table_info(Car)").fetchall()}
    car_update = [
        ('CarName', car_name), ('CarNameByUser', car_name),
        ('ColorCode', color_code), ('ColorName', body_color),
        ('TrimCode', trim_code),
    ]
    # 非マージモードでは空欄でも書いてテンプレートの値を消す。
    # ここだけ「非空のみ」だったため、前案件の車の色・カラーコードが
    # DBに残る一方でヘッダXMLには空が書かれ、同じ .neo の中で食い違っていた。
    # 塗色は塗装工賃の根拠になるので、別の車の色が残るのは危険。
    valid_car = [(col, val) for col, val in car_update
                 if col in car_cols and (val or not merge_mode)]
    if valid_car:
        set_clause = ', '.join(f'{col}=?' for col, _ in valid_car)
        values = [val for _, val in valid_car]
        cur.execute(f'UPDATE Car SET {set_clause}', values)
    est_era, est_era_year = get_era_info(estimated_date)
    cur.execute('''UPDATE FileInfo SET
        EstimatedDate=?, EstimatedEra=?, EstimatedEraYear=?
    ''', (estimated_date, est_era, est_era_year))
    # コグニセブンの列幅に合わせて切り詰める。SQLite は TEXT(n) を強制しないため
    # ここで守らないと、桁あふれした値がそのまま入る。
    policy_no     = cp932_trim(insurance_info.get('policy_no', ''), 20)
    contractor    = cp932_trim(insurance_info.get('contractor_name', ''), 20)
    agency_name   = cp932_trim(insurance_info.get('agency_name', ''), 20)
    adjuster_name = cp932_trim(insurance_info.get('adjuster_name', ''), 20)
    accept_no     = cp932_trim(insurance_info.get('accept_no', ''), 37)
    accident_date = _normalize_date8(insurance_info.get('accident_date', ''))
    garage_in     = _normalize_date8(insurance_info.get('garage_in_date', ''))
    garage_out    = _normalize_date8(insurance_info.get('garage_out_date', ''))
    repair_days   = safe_int(insurance_info.get('repair_days', 0))
    # 備考は改行を含むと固定長レコードが崩れるため1行に潰す
    note1         = cp932_trim(
        re.sub(r'\s+', ' ', safe_str(insurance_info.get('note1', ''))).strip(), 40)

    # Insurance テーブル: 入力があった項目だけ書き込む。
    # 空欄で既存値を消すと、テンプレート由来の工場情報などが失われるため。
    _ins_updates, _ins_values = [], []
    # 非マージモードでは、空欄でも書いてテンプレートの値を消す。
    # Customer は非マージなら全上書きなのに、ここだけ「非空のみ」だったため、
    # 前案件のアジャスター名がDBに残る一方でヘッダXMLには空が書かれ、
    # 同じ .neo の中で食い違っていた。
    for _col, _val in (('PolicyNo', policy_no), ('ContractorName', contractor),
                       ('AgencyName', agency_name), ('AdjusterName', adjuster_name)):
        if _val or not merge_mode:
            _ins_updates.append(f'{_col}=?')
            _ins_values.append(_val)
    if repair_days > 0:
        _ins_updates.append('RepairDays=?')
        _ins_values.append(repair_days)
    if accident_date:
        _acc_era, _acc_era_year = get_era_info(accident_date)
        _ins_updates += ['AccidentDate=?', 'AccidentEra=?', 'AccidentEraYear=?']
        _ins_values  += [accident_date, _acc_era, _acc_era_year]
    elif not merge_mode:
        # 新規作成時は事故日を未入力状態で初期化する（和暦の年も併せて消す）
        _ins_updates += ['AccidentDate=?', 'AccidentEra=?', 'AccidentEraYear=?']
        _ins_values  += ['00000000', '令和', '0000']
    if _ins_updates:
        cur.execute(f"UPDATE Insurance SET {', '.join(_ins_updates)}", _ins_values)

    # FileInfo テーブル: 受付番号・入出庫日・備考
    # 非マージモードでは空欄でも書く（Insurance と同じ理由）。
    _fi_updates, _fi_values = [], []
    if accept_no or not merge_mode:
        _fi_updates.append('AcceptNo=?')
        _fi_values.append(accept_no)
    if note1 or not merge_mode:
        _fi_updates.append('Note1=?')
        _fi_values.append(note1)
    for _prefix, _date in (('GarageIn', garage_in), ('GarageOut', garage_out)):
        if _date:
            _era, _era_year = get_era_info(_date)
            _fi_updates += [f'{_prefix}Date=?', f'{_prefix}Era=?', f'{_prefix}EraYear=?']
            _fi_values  += [_date, _era, _era_year]
    if _fi_updates:
        cur.execute(f"UPDATE FileInfo SET {', '.join(_fi_updates)}", _fi_values)
    conn.commit()

    # Statistics は案件そのものを識別する欄。過去の .neo をテンプレートに
    # 使うと、前案件の見積ID・案件番号・協定額が新しい見積に同居する。
    # 保険会社への提出物としては危険なので、案件固有の欄だけ初期化する。
    # 工場区分・保険会社区分など工場固有の設定は残す（消すと毎回入れ直しになる）。
    # 見積ID・案件番号・協定額は案件そのものを識別する値で、テンプレートに
    # 何を使おうと引き継いではいけない。マージモードを除外していたため、
    # 過去の .neo をテンプレートにすると前案件の協定額が新しい見積に
    # 同居していた。アプリにこれらの入力欄は無く、利用者は消せない。
    if True:
        try:
            cur.execute("""UPDATE Statistics SET
                EstimationId='', ProjectNo='', ProjectCompletedFlag='',
                DefiniteOutTax=-1, DefiniteInTax=-1, DefiniteTax=-1,
                AccidentLargeCategoryCode='', AccidentSmallCategoryCode='',
                DisasterFlag='', DisasterIdentificationCode=''""")
            conn.commit()
        except Exception as e:
            print("Statistics reset failed:", e)

    # TaxKindFlag 更新 (1=内税, 0=外税)
    try:
        tax_flag = 1 if is_tax_inclusive else 0
        cur.execute('UPDATE Setting SET TaxKindFlag=?', (tax_flag,))
        conn.commit()
    except Exception as e:
        print("TaxKindFlag update failed:", e)

    conn.close()
    with open(_tmp_db_path, 'rb') as f:
        result = f.read()
    return result


# ============================================================
# 内部ファイル更新: AnSvMail.ini（XML）
# ============================================================

def update_mail_ini(orig_bytes, cust, grand_total, insurance_info=None, merge_mode=False):
    """Shift_JIS XMLの顧客・車両情報を更新
    merge_mode=True の場合、非空の値のみ上書きする。
    """
    text         = orig_bytes.decode('cp932', errors='replace')
    # DB と同じ切り詰め済みの値を使う。片側だけ切ると、1つの .neo の中で
    # 使用者名や車名が2種類存在する状態になる。
    _t = _trimmed_cust_values(cust)
    customer_name = _t['customer_name']
    user_name     = _t['user_name']
    owner_name    = _t['owner_name']
    car_dept      = _t['car_dept']
    car_div       = _t['car_div']
    car_biz       = _t['car_biz']
    car_serial    = _t['car_serial']
    car_no_full   = f'{car_dept}{car_div}{car_biz}{car_serial}'
    car_name      = _t['car_name']
    car_serial_no = _t['car_serial_no']
    kilometer     = safe_str(cust.get('kilometer', ''))
    # DB側と同じ正規化を通す。ここだけ生の値を使うと、同じNEOの中で
    # DBとヘッダXMLが食い違ったり int('/0') で落ちたりする。
    car_reg_date  = _normalize_ym8(cust.get('car_reg_date', ''))
    term_date     = _normalize_date8(cust.get('term_date', ''))
    ins = insurance_info or {}
    tag_values = {
        'CustomerName1': customer_name,
        'OwnerName':     owner_name,
        'UserName':      user_name,
        'CarNo':         car_no_full,
        'CarName':       car_name,
        'CarSerialNo':   car_serial_no,
        'Kilometrage':   kilometer,
        'CarNoArea':     car_dept,
        'CarNoClass':    car_div,
        'CarNoKana':     car_biz,
        'CarNoSeries':   car_serial,
        'Total':         grand_total,
        # 作成日を更新しないと、どの見積にもテンプレート作成時の日付が残る
        'CreatedDate':   now_jst().strftime('%Y/%m/%d'),
        # 事故・保険情報。DBに書くのと同じ値をヘッダXMLにも書かないと、
        # 過去のNEOをテンプレートに使ったとき前の案件の値が残ってしまう。
        'AcceptNo':      cp932_trim(ins.get('accept_no', ''), 37),
        'AccidentDate':  _normalize_date8(ins.get('accident_date', '')),
        'AdjusterName':  cp932_trim(ins.get('adjuster_name', ''), 20),
        'Note1':         cp932_trim(
            re.sub(r'\s+', ' ', safe_str(ins.get('note1', ''))).strip(), 40),
        'GarageInDate':  _normalize_date8(ins.get('garage_in_date', '')),
        'GarageOutDate': _normalize_date8(ins.get('garage_out_date', '')),
        'CarMouldNo':    _t['model_desig'],
        'CarKindNo':     _t['category_num'],
        'ColorCode':     _t['color_code'],
        # 以下はアプリが値を持たない案件固有欄。書かずに放置すると、
        # 過去の .neo をテンプレートにしたとき前の案件の立会者名・伝票番号・
        # 備考・グレードがそのまま新しい見積に残る（立会者名は個人情報）。
        # マージモードでは空値はスキップされるので、テンプレート保持は壊れない。
        'CustomerName2':        '',
        'TicketNo':             '',
        'Note2':                '',
        'Note3':                '',
        'ii_CustomerName':      '',
        'ii_PresenceDate':      '',
        'ii_AgreedDate':        '',
        'ii_RepairDays':        '',
        'ii_TimePrice':         '',
        'GradeName':            '',
        'CarYearName':          '',
        'BodyName':             '',
        'FVariationNameByUser': '',
    }
    if term_date and term_date != '00000000':
        term_era, term_era_year = get_era_info(term_date)
        era_year_int = int(term_era_year)
        term_month = term_date[4:6] if len(term_date) >= 6 else ''
        term_day   = term_date[6:8] if len(term_date) >= 8 else ''
        tag_values['CarTermEraDate'] = (
            f'{term_era}{era_year_int}年{int(term_month)}月{int(term_day)}日'
            if term_month and term_day else ''
        )
    else:
        tag_values['CarTermEraDate'] = ''
    if car_reg_date and car_reg_date != '00000000':
        reg_era, reg_era_year = get_era_info(car_reg_date)
        reg_year_int = int(reg_era_year)
        reg_month    = car_reg_date[4:6] if len(car_reg_date) >= 6 else ''
        tag_values['CarRegistedDate'] = (
            f'{reg_era}{reg_year_int}年{int(reg_month)}月'
            if reg_month and reg_month != '00' else ''
        )
        # 合成文字列だけ書いて構造化タグを空のまま残すと、同じ .neo の中に
        # 初度登録が2通り入る（DB側は Customer.CarRegDate に8桁で入っている）。
        tag_values['CarRegistedDateYear']  = reg_era_year
        tag_values['CarRegistedDateMonth'] = reg_month
        # 元号コードも一緒に書く。年月だけ入れて元号コードを
        # テンプレートの値のまま残すと、「元号は平成・年は令和の年」という
        # 組み合わせになり、令和6年が平成6年（1994年）として読まれる。
        tag_values['CarRegistedDateEra'] = _ERA_CODE.get(reg_era, '')
    else:
        tag_values['CarRegistedDate']      = ''
        tag_values['CarRegistedDateYear']  = ''
        tag_values['CarRegistedDateMonth'] = ''
        tag_values['CarRegistedDateEra']   = ''
    # CarNo（連結された登録番号）は、分解4欄とマージ判定の粒度が違う。
    # 4欄はタグ単位で「空ならテンプレートの値を残す」のに、CarNo は
    # 今回の入力だけから合成していたため、1欄でも空だと
    # 「品川あ１２３４」のように桁の抜けた、存在しない登録番号になり、
    # 同じ .neo の中で分解4欄と食い違っていた。
    # 書き込み後に実際に入る4欄の値から合成し直す。
    if merge_mode:
        _eff = []
        for _tag, _val in (('CarNoArea', car_dept), ('CarNoClass', car_div),
                           ('CarNoKana', car_biz), ('CarNoSeries', car_serial)):
            _eff.append(_val if _val else read_xml_tag(text, _tag))
        tag_values['CarNo'] = ''.join(_eff)

    for tag_name, value in tag_values.items():
        if merge_mode and not value:
            continue  # マージモード: 空値はスキップ（テンプレートの既存値を保持）
        # 値に & や < が入るとXMLが壊れるためエスケープする（法人名の「＆」等）
        text = replace_xml_tag(text, tag_name, _xml_escape(value))
    return text.encode('cp932', errors='replace')


# ============================================================
# 内部ファイル更新: AnSvImge.ini（INI）
# ============================================================

def update_imge_ini(orig_bytes, cust, insurance_info=None, merge_mode=False):
    """INIファイルの顧客・車両情報を更新
    merge_mode=True の場合、非空の値のみ上書きする。
    """
    text      = orig_bytes.decode('cp932', errors='replace')
    # DB・ヘッダXML と同じ切り詰め済みの値を使う
    _t = _trimmed_cust_values(cust)
    ini_values = {
        'CustomerName':    _t['customer_name'],
        'CarNoDepartment': _t['car_dept'],
        'CarNoDivision':   _t['car_div'],
        'CarNoBusiness':   _t['car_biz'],
        'CarNoSerial':     _t['car_serial'],
        'CarName':         _t['car_name'],
        # 事故情報。書かないとテンプレート再利用時に前の案件の値が残る。
        'AcceptNo':        cp932_trim((insurance_info or {}).get('accept_no', ''), 37),
        # 8桁固定の欄。未入力は純正テンプレートと同じ 00000000 にする
        # （空文字だと DB の Insurance.AccidentDate='00000000' と食い違う）
        # 未入力を '00000000' に既定化するのは非マージモードのときだけ。
        # マージモードでも真の値になってしまうと下の空値スキップに
        # 引っかからず、DB・ヘッダXMLには前案件の事故日が残るのに
        # ここだけ 00000000 に潰れ、同じ .neo で事故日が2通りになる。
        'AccidentDate':    (_normalize_date8((insurance_info or {}).get('accident_date', ''))
                            or ('' if merge_mode else '00000000')),
    }
    for key, value in ini_values.items():
        if merge_mode and not value:
            continue  # マージモード: 空値はスキップ（テンプレートの既存値を保持）
        text = replace_ini_value(text, key, value)
    return text.encode('cp932', errors='replace')


# ============================================================
# 内部ファイル更新: AnNote.ini（明細簡易表現）
# ============================================================

def generate_annote(rows):
    """142B固定長 × 行数 の AnNote.ini を生成

    rows は _update_ansmb_impl が ERParts に実際に書いた値
    （line_no / name / qty）。生の items から組み直すと、
    マスタ名への置換や「※」付与、数量ブランクが反映されず、
    同じ行なのに DB と AnNote で品名・数量が食い違う。
    """
    if not rows:
        return b''
    lines = []
    for row in rows:
        name    = row.get('name', '')
        _q      = safe_int(row.get('qty', 1), 1)
        qty     = _q if _q > 0 else 1     # -1（ブランク）は表示上1として扱う
        line_no = row.get('line_no', 0)
        line    = bytearray(142)
        for j in range(142):
            line[j] = 0x20
        ln_str = f'{line_no:08d}'
        for j, c in enumerate(ln_str):
            line[j] = ord(c)
        # バイト数で単純に切ると2バイト文字の途中で割れ、末尾に
        # 復号できない片割れが残る。文字境界で切り詰める。
        # 改行やタブが混ざると142B固定長レコードが行単位で割れる。
        # 見積書の部品名が2行に折り返された表をCSV化すると普通に起きる。
        name_bytes = cp932_trim(_strip_control_chars(name), 30).encode('cp932', errors='replace')
        for j, b in enumerate(name_bytes):
            line[14 + j] = b
        # 注記の数量欄は2桁固定。3桁以上は入らないので丸めるしかないが、
        # 明細テーブルには150、注記には99と書かれ、同じ .neo の中で
        # 数量が食い違う。丸めたことは画面で知らせる（下の警告で拾う）。
        qty_str = f'{min(qty, 99):02d}'
        line[98] = ord(qty_str[0])
        line[99] = ord(qty_str[1])
        for j, c in enumerate('90000'):
            line[100 + j] = ord(c)
        for j, c in enumerate('F99999'):
            line[127 + j] = ord(c)
        lines.append(bytes(line) + b'\r\n')
    return b''.join(lines)


# ============================================================
# NEO リパッカー
# ============================================================

def repack_neo(orig_data, files, mgmt, entries):
    """更新済みファイルをNEOバイナリに再パック"""
    now     = now_jst()
    now_dos = datetime_to_dos(now)
    entry_names  = [e['name'] for e in entries if e['name'] in files]
    missing_names = [name for name in files.keys() if name not in entry_names]
    ordered_names = entry_names + sorted(missing_names, key=lambda x: x.encode('cp932'))
    hidden_entries = [e for e in entries if e.get('is_last')]
    hidden_name    = hidden_entries[0]['name'] if hidden_entries else ordered_names[-1]
    normal_names   = [name for name in ordered_names if name != hidden_name]
    raw     = files[hidden_name]
    offsets = {}
    sizes   = {}
    for name in normal_names:
        offsets[name] = len(raw)
        sizes[name]   = len(files[name])
        raw += files[name]
    table_bytes = b''
    for name in ordered_names:
        if name == 'AnDBVersion.ini':
            dos = DOS_DBVER
        elif name == 'AnSvImge.ini':
            dos = DOS_IMGE
        else:
            dos = now_dos
        attr      = struct.pack('<H', 0x0020)
        name_enc  = ('\\' + name).encode('cp932') + b'\x00'
        if name == hidden_name:
            table_bytes += dos + attr + name_enc
        else:
            sz  = struct.pack('<I', sizes[name])
            off = struct.pack('<I', offsets[name])
            table_bytes += dos + attr + name_enc + sz + off + b'\x00\x00'
    CHUNK_SIZE      = 32768
    raw_chunks      = [raw[i:i + CHUNK_SIZE] for i in range(0, len(raw), CHUNK_SIZE)]
    num_chunks      = len(raw_chunks)
    compressed_chunks = []
    prev_raw        = b''
    for i, raw_chunk in enumerate(raw_chunks):
        if i == 0:
            c = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15)
        else:
            dict_data = prev_raw[-32768:]
            c = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15, zdict=dict_data)
        compressed = c.compress(raw_chunk) + c.flush()
        compressed_chunks.append(compressed)
        prev_raw += raw_chunk
    new_mgmt  = bytearray(mgmt)
    last_size = len(files[hidden_name])
    struct.pack_into('<H', new_mgmt, len(new_mgmt) - 14, num_chunks)
    struct.pack_into('<H', new_mgmt, len(new_mgmt) - 10, last_size)
    header  = orig_data[:424]
    ck_data = b''
    for i, comp in enumerate(compressed_chunks):
        comp_len  = len(comp) + 2
        decomp_len = len(raw_chunks[i])
        ck_data   += b'\x00\x00\x00\x00' + struct.pack('<HH', comp_len, decomp_len) + b'CK' + comp
    return header + bytes(new_mgmt) + table_bytes + ck_data


def generate_neo_file(template_data, customer_info, items, short_parts_wage, insurance_info, expenses=None, is_tax_inclusive=False, is_beta_mode=False, merge_mode=False):
    """テンプレートNEOから更新済みNEOを生成
    merge_mode=True の場合、ユーザーアップロードのテンプレートNEOをベースとし、
    車検証OCRで取得した値（非空のみ）でヘッダ情報を訂正し、明細欄はPDF解析結果で上書きする。
    テンプレートにのみ存在する情報（工場名・証券番号等）は保持される。
    """
    real_ck  = find_real_cks(template_data)
    if not real_ck:
        raise ValueError("テンプレートNEOのCKチャンクが見つかりません")
    full_raw     = decompress_neo(template_data, real_ck)
    mgmt, entries = parse_entries(template_data, real_ck[0])
    files        = extract_files(full_raw, entries)
    estimated_date = now_jst().strftime('%Y%m%d')
    normalized_items = items or []
    files['AnSMB.txt'], total_parts, total_wages, grand_total, _annote_rows = update_ansmb(
        files['AnSMB.txt'], normalized_items, short_parts_wage,
        expenses=expenses, is_tax_inclusive=is_tax_inclusive, is_beta_mode=is_beta_mode
    )
    files['AnNote.ini']       = generate_annote(_annote_rows)
    files['AnSvEm0001Ex.db']  = update_em_db(
        files['AnSvEm0001Ex.db'], customer_info, insurance_info, estimated_date,
        is_tax_inclusive=is_tax_inclusive, merge_mode=merge_mode
    )
    files['AnSvMail.ini'] = update_mail_ini(files['AnSvMail.ini'], customer_info, grand_total,
                                            insurance_info=insurance_info, merge_mode=merge_mode)
    files['AnSvImge.ini'] = update_imge_ini(files['AnSvImge.ini'], customer_info,
                                            insurance_info=insurance_info, merge_mode=merge_mode)
    neo_data = repack_neo(template_data, files, mgmt, entries)
    return neo_data, total_parts, total_wages, grand_total


# ============================================================
# AI-OCR サポート関数
# ============================================================

def enhance_image_for_ocr(image_bytes):
    """
    OCR精度向上のための画像前処理（300dpi FAX品質対応強化版）。
    グレースケール変換 → デスペックル → コントラスト補正 → アンシャープマスク の順で処理。
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        img = Image.open(io.BytesIO(image_bytes))
        # グレースケール変換（色ノイズを除去してOCR精度向上）
        if img.mode not in ('L', 'LA'):
            img = img.convert('L')
        # デスペックル（MedianFilter でノイズ除去）
        img = img.filter(ImageFilter.MedianFilter(size=3))
        # コントラスト強化（FAXのかすれた文字を読みやすくする）
        img = ImageEnhance.Contrast(img).enhance(1.8)
        # アンシャープマスク（エッジを鮮明化）
        img = img.filter(ImageFilter.UnsharpMask(radius=1, percent=150, threshold=2))
        # 明るさ微調整（暗すぎる画像を補正）
        img = ImageEnhance.Brightness(img).enhance(1.05)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=94)
        return buf.getvalue()
    except Exception:
        return image_bytes


# pdfium(pypdfium2) はスレッドセーフでないため、呼び出しを直列化する。
# `streamlit run app.py` では app.py は __main__ なので、pipeline 側の
# `from app import ...` は別インスタンスを掴んでしまう。専用モジュールに置く。
from _pdfium_lock_mod import PDFIUM_LOCK as _PDFIUM_LOCK
# ラスタライズ時の総画素数上限（約40メガピクセル）。A3@300dpi でも約17Mpxなので余裕がある。
MAX_RASTER_PIXELS = 40_000_000


def rasterize_pdf_page(pdf_bytes, page_index, dpi=200, enhance=False):
    """
    PDFの指定ページをJPEG画像バイト列に変換。
    行ズレ防止のためGeminiに送る前に使用。
    enhance=True の場合、画像前処理（コントラスト・シャープネス強化）を適用。
    優先順位: pdf2image(poppler) → pypdfium2 → None
    """
    result = None

    # ── 方法1: pdf2image (poppler) ─────────────────────────
    try:
        from pdf2image import convert_from_bytes
        images = convert_from_bytes(
            pdf_bytes,
            dpi=dpi,
            first_page=page_index + 1,
            last_page=page_index + 1,
        )
        if images:
            buf = io.BytesIO()
            images[0].save(buf, format='JPEG', quality=90)
            result = buf.getvalue()
    except Exception:
        pass

    # ── 方法2: pypdfium2 (フォールバック) ──────────────────
    if result is None:
        # pdfium はスレッドセーフではない。複数スレッドから同時に呼ぶと
        # Cヒープが壊れてプロセスごと落ちる（Streamlitのサーバ全体が死ぬ）ため、
        # ここはプロセス内で必ず直列に実行する。
        with _PDFIUM_LOCK:
            try:
                import pypdfium2 as pdfium
                doc     = pdfium.PdfDocument(pdf_bytes)
                try:
                    page    = doc[page_index]
                    scale   = dpi / 72.0
                    # 巨大ページ（A0など）を高DPIで描くと1GB超のメモリを使い
                    # コンテナごとOOMで落ちるため、総画素数で上限を掛ける。
                    try:
                        w_pt, h_pt = page.get_size()
                        px = (w_pt * scale) * (h_pt * scale)
                        if px > MAX_RASTER_PIXELS and px > 0:
                            scale *= (MAX_RASTER_PIXELS / px) ** 0.5
                            print(f"[rasterize] ページが大きいため解像度を下げました "
                                  f"(scale={scale:.3f})")
                    except Exception:
                        pass
                    bitmap  = page.render(scale=scale)
                    pil_img = bitmap.to_pil()
                    buf     = io.BytesIO()
                    pil_img.save(buf, format='JPEG', quality=90)
                    result = buf.getvalue()
                finally:
                    doc.close()
            except Exception as _rast_err:
                print(f"[rasterize] pypdfium2でのページ画像化に失敗: {_rast_err}")

    # ── 画像前処理（FAX品質改善用） ──────────────────
    if result and enhance:
        result = enhance_image_for_ocr(result)

    return result


def _pdf_visual_size(page):
    """ページの「見た目の」幅と高さ。/Rotate 90・270 なら縦横が入れ替わる。"""
    box = page.mediabox
    w, h = float(box.width), float(box.height)
    try:
        rot = int(page.get('/Rotate', 0) or 0) % 360
    except (TypeError, ValueError):
        rot = 0
    return (h, w) if rot in (90, 270) else (w, h)


def try_fix_landscape_pdf(pdf_bytes):
    """横向きPDFを検出して縦向きに回転する。

    MediaBox の縦横だけで判断すると、スキャナやFAXが作る
    「MediaBox は横長だが /Rotate 90 で正立している」PDF を横向きと
    誤認し、正立していたページをわざわざ倒してしまう。見た目の向きで
    判定し、既存の /Rotate に加算した結果も 0〜359 に正規化する。
    """
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject, NumberObject
        reader = PdfReader(io.BytesIO(pdf_bytes))
        targets = set()
        for i, page in enumerate(reader.pages):
            w, h = _pdf_visual_size(page)
            if w > h * 1.2:
                targets.add(i)
        if not targets:
            return pdf_bytes
        writer = PdfWriter()
        for i, page in enumerate(reader.pages):
            if i in targets:
                try:
                    _cur = int(page.get('/Rotate', 0) or 0)
                except (TypeError, ValueError):
                    _cur = 0
                page[NameObject('/Rotate')] = NumberObject((_cur + 270) % 360)
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        return pdf_bytes


def try_split_pdf_pages(pdf_bytes):
    """PDFを個別ページに分割（2ページ以上の場合のみ）"""
    try:
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if len(reader.pages) <= 1:
            return None
        pages = []
        for page in reader.pages:
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            pages.append(buf.getvalue())
        return pages
    except Exception:
        return None


def detect_and_reorder_pages(pages):
    """
    PDFの各ページから「X/Y頁」「P.001/002」等のページ番号表記を検出し、
    正しい順序に並び替えて返す。
    【対応パターン】
      - FAXヘッダ形式: "P.001/002" → 1ページ目
      - 日本語形式: "1/2頁" "1／2頁" "1/2 頁"
      - 逆形式: "頁1/2"
    検出できない・全ページ揃わない場合は元の順序をそのまま返す。
    """
    import re
    try:
        from pypdf import PdfReader
    except ImportError:
        return pages

    page_numbers = []
    for idx, page_bytes in enumerate(pages):
        try:
            reader = PdfReader(io.BytesIO(page_bytes))
            text = reader.pages[0].extract_text() or ''
        except Exception:
            text = ''

        cur_page = None
        # パターン1: FAXヘッダ "P.001/002" 形式（大文字・小文字両対応）
        m = re.search(r'[Pp]\.(\d+)\s*/\s*(\d+)', text)
        if m:
            cur_page = int(m.group(1))
            page_numbers.append((idx, cur_page, int(m.group(2))))
            continue
        # パターン2: "1/2頁" "1／2頁" "1/2 頁" 形式
        m = re.search(r'(\d+)\s*[/／]\s*(\d+)\s*[頁ページ]', text)
        if m:
            cur_page = int(m.group(1))
            page_numbers.append((idx, cur_page, int(m.group(2))))
            continue
        # パターン3: "頁1/2" 逆形式
        m = re.search(r'[頁ページ]\s*(\d+)\s*[/／]\s*(\d+)', text)
        if m:
            cur_page = int(m.group(1))
            page_numbers.append((idx, cur_page, int(m.group(2))))
            continue
        # 検出できないページ
        page_numbers.append((idx, None, None))

    # 全ページでページ番号が検出できた場合のみ並び替え
    if page_numbers and all(pn[1] is not None for pn in page_numbers):
        original_order = [pn[0] for pn in page_numbers]
        page_numbers.sort(key=lambda x: x[1])
        new_order = [pn[0] for pn in page_numbers]
        if new_order != original_order:
            import sys
            print(f"[INFO] ページ順序を自動修正: {[p[1] for p in page_numbers]} "
                  f"(物理順 {original_order} → 文書順 {new_order})", file=sys.stderr)
        return [pages[pn[0]] for pn in page_numbers]

    return pages


@st.cache_resource
def _get_genai_client(api_key):
    """google.genai クライアントを取得（セッション間で再利用）"""
    from google import genai
    return genai.Client(api_key=api_key)


def call_gemini(api_key, file_bytes, mime_type, prompt_text, model_name=None, use_json_mode=False):
    """Gemini APIにファイルを送信して解析結果テキストを取得（最大3回リトライ）
    use_json_mode=True の場合、構造化JSON出力モードを使用（解析精度向上）
    """
    from google.genai import types
    client = _get_genai_client(api_key)
    model = model_name or GEMINI_MODEL
    file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    config = {"temperature": 0.0, "max_output_tokens": 65536}
    if use_json_mode:
        config["response_mime_type"] = "application/json"
    last_error = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=model,
                contents=[prompt_text, file_part],
                config=config,
            )
            if response.text and response.text.strip():
                return response.text
            if attempt < 2:
                import time; time.sleep(1)
                continue
            raise ValueError("Geminiから有効な応答が得られませんでした。")
        except ValueError:
            raise
        except Exception as e:
            last_error = e
            if attempt < 2:
                import time; time.sleep(1)
                continue
            raise ValueError(f"Gemini API呼び出しに失敗しました（{attempt+1}回試行）: {str(last_error)}")


def classify_first_page_as_fax(api_key, pdf_bytes, model_name):
    """
    PDFの1ページ目がFAX送付状かどうかをAIで判定する。
    True=FAX送付状 → 除外すべき、False=見積書・車検証などの本体ページ
    """
    try:
        img_bytes = rasterize_pdf_page(pdf_bytes, 0, dpi=120)
        if img_bytes is None:
            return False
        from google.genai import types
        client = _get_genai_client(api_key)
        prompt = _build_prompt("estimate_cover_check")
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")],
            config={"temperature": 0.0, "max_output_tokens": 256, "response_mime_type": "application/json"},
        )
        result = extract_json_from_response(response.text)
        return bool(result.get('is_fax_cover', False))
    except Exception:
        return False


def filter_fax_pages(api_key, pdf_bytes, model_name):
    """
    FAX送付状（1ページ目）を除去した新しいPDFバイト列を返す。
    2ページ以上かつ1ページ目がFAXと判定された場合のみ除去する。
    """
    try:
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if len(reader.pages) <= 1:
            return pdf_bytes
        is_fax = classify_first_page_as_fax(api_key, pdf_bytes, model_name)
        if not is_fax:
            return pdf_bytes
        writer = PdfWriter()
        for i in range(1, len(reader.pages)):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        return pdf_bytes


def guess_manufacturer_from_vin(vin):
    """車台番号の先頭文字列からメーカー・車種を推定する"""
    if not vin:
        return '', ''
    vin = vin.upper().strip()
    # WMI（先頭3文字）ベースのメーカー判定テーブル
    WMI_MAP = {
        # ドイツ車
        'WUA': ('アウディ', ''), 'WAU': ('アウディ', ''), 'WA1': ('アウディ', ''),
        'WBA': ('BMW', ''), 'WBS': ('BMW', 'M'), 'WBY': ('BMW', 'i'),
        'WDB': ('メルセデス・ベンツ', ''), 'WDC': ('メルセデス・ベンツ', ''), 'WDD': ('メルセデス・ベンツ', ''),
        'W1K': ('メルセデス・ベンツ', ''), 'W1N': ('メルセデス・ベンツ', ''),
        'WVW': ('フォルクスワーゲン', ''), 'WV1': ('フォルクスワーゲン', ''), 'WV2': ('フォルクスワーゲン', ''),
        'WP0': ('ポルシェ', ''), 'WP1': ('ポルシェ', ''),
        # 日本車
        'JTD': ('トヨタ', ''), 'JTE': ('トヨタ', ''), 'JTN': ('トヨタ', ''),
        'JHM': ('ホンダ', ''), 'JHL': ('ホンダ', ''),
        'JN1': ('日産', ''), 'JN3': ('日産', ''),
        'JMA': ('マツダ', ''), 'JMZ': ('マツダ', ''),
        'JSA': ('スズキ', ''), 'JS1': ('スズキ', ''),
        'JF1': ('スバル', ''), 'JF2': ('スバル', ''),
        'JDA': ('ダイハツ', ''),
        'JMB': ('三菱', ''), 'JMY': ('三菱', ''),
        # 韓国車
        'KMH': ('ヒュンダイ', ''), 'KNA': ('キア', ''),
        # イタリア車
        'ZAR': ('アルファロメオ', ''), 'ZFF': ('フェラーリ', ''), 'ZHW': ('ランボルギーニ', ''),
        'ZFA': ('フィアット', ''), 'ZAM': ('マセラティ', ''),
        # イギリス車
        'SAL': ('ランドローバー', ''), 'SAJ': ('ジャガー', ''), 'SAR': ('ランドローバー', ''),
        'SCF': ('アストンマーティン', ''), 'SCC': ('ロータス', ''),
        # フランス車
        'VF1': ('ルノー', ''), 'VF3': ('プジョー', ''), 'VF7': ('シトロエン', ''),
        # アメリカ車
        '1FA': ('フォード', ''), '1FT': ('フォード', ''), '1G1': ('シボレー', ''),
        '1GC': ('シボレー', ''), '1GM': ('GM', ''), '2T1': ('トヨタ(北米)', ''),
        '3FA': ('フォード(メキシコ)', ''),
        # スウェーデン車
        'YV1': ('ボルボ', ''), 'YS3': ('サーブ', ''),
    }
    # 先頭3文字で判定
    wmi3 = vin[:3]
    if wmi3 in WMI_MAP:
        return WMI_MAP[wmi3]
    # 先頭2文字でフォールバック
    COUNTRY_PREFIX = {
        'WU': ('アウディ/VW系', ''), 'WB': ('BMW', ''), 'WD': ('メルセデス・ベンツ', ''),
        'WV': ('フォルクスワーゲン', ''), 'WP': ('ポルシェ', ''), 'WF': ('フォード(独)', ''),
        'JT': ('トヨタ', ''), 'JH': ('ホンダ', ''), 'JN': ('日産', ''),
        'JM': ('マツダ/三菱', ''), 'JS': ('スズキ', ''), 'JF': ('スバル', ''),
        'JD': ('ダイハツ', ''), 'ZA': ('イタリア車', ''), 'SA': ('イギリス車', ''),
        'VF': ('フランス車', ''), 'YV': ('ボルボ', ''),
    }
    wmi2 = vin[:2]
    if wmi2 in COUNTRY_PREFIX:
        return COUNTRY_PREFIX[wmi2]
    return '', ''


# ============================================================
# Addata / マスタ連携
# ============================================================
# Addata は「A〜Z の1文字フォルダ / 車種コード / *NN.DB」という配置の
# 車種データベースで、コグニセブン本体に同梱される。これがあると
# 部品名・品番・価格をマスタと突き合わせ、部品コードや損害コードを
# 引き当てられる（モードB/C）。無ければベタ打ち（モードA）になる。
#
# 取得経路は3つ。上から順に見る。
#   1. 画面からアップロードされた ZIP を展開したもの（本番はこれだけ）
#   2. 環境変数 ADDATA_ROOT / st.secrets の ADDATA_ROOT
#   3. ローカルの標準的な設置場所（Windows の C:\Addata など）
# 本番の Streamlit Cloud は Linux で利用者のPCも見えないため、
# 1 以外は基本的に当たらない。

# アップロードされた Addata の「ルート」と「展開先ディレクトリ」。
# ルートは ZIP の作り方によって展開先より下の階層になることがあるため、
# 消すときは必ず展開先の方を消す（ルートの親を消すと /tmp を消しかねない）。
_ADDATA_UPLOAD_KEY = '_addata_upload_root'
_ADDATA_UPLOAD_BASE_KEY = '_addata_upload_base'
# ZIP 展開の上限。壊れた/悪意ある ZIP でディスクを埋めないための歯止め。
ADDATA_ZIP_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024   # 展開後 合計2GB
ADDATA_ZIP_MAX_MEMBERS     = 200_000                   # ファイル数


def _addata_is_valid(path) -> bool:
    """Addata ルートとして妥当か（A-Z1文字フォルダ配下に *.DB があるか）。"""
    if not path or not os.path.isdir(path):
        return False
    try:
        import addata_locator as _loc
        return bool(_loc._is_valid_addata(path))
    except Exception:
        return False


def extract_addata_zip(zip_bytes: bytes, dest_dir: str) -> tuple:
    """Addata の ZIP を dest_dir に安全に展開し、(ルートパス, 説明) を返す。

    ルートが見つからない場合は (None, 理由) を返す。
    ZIP の中身は利用者が持ち込む外部データなので、
    パス抜け（zip slip）・容量爆弾・シンボリックリンクを弾く。
    """
    import zipfile
    dest_real = os.path.realpath(dest_dir)
    total = 0
    count = 0
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                count += 1
                if count > ADDATA_ZIP_MAX_MEMBERS:
                    return (None, f'ZIP内のファイル数が多すぎます（{ADDATA_ZIP_MAX_MEMBERS:,}件を超過）')
                # シンボリックリンクは展開しない（外部を指しうる）
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue
                if info.is_dir():
                    continue
                total += info.file_size
                if total > ADDATA_ZIP_MAX_TOTAL_BYTES:
                    return (None, 'ZIPの展開後サイズが大きすぎます（2GBを超過）')
                # 展開先が dest_dir の外に出ないことを実パスで確認する
                target = os.path.realpath(os.path.join(dest_real, info.filename))
                if not (target == dest_real or target.startswith(dest_real + os.sep)):
                    return (None, f'ZIP内に不正なパスが含まれています: {info.filename}')
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as _s, open(target, 'wb') as _d:
                    while True:
                        chunk = _s.read(1024 * 1024)
                        if not chunk:
                            break
                        _d.write(chunk)
    except zipfile.BadZipFile:
        return (None, 'ZIPファイルとして読み取れません')
    except Exception as e:
        return (None, f'ZIPの展開に失敗しました: {e}')

    # 展開結果から Addata ルートを探す。ZIP の作り方によって
    # 直下だったり Addata/ で1階層包まれていたりするため両方見る。
    if _addata_is_valid(dest_real):
        return (dest_real, 'ZIP直下')
    try:
        for entry in sorted(os.listdir(dest_real)):
            cand = os.path.join(dest_real, entry)
            if _addata_is_valid(cand):
                return (cand, entry)
            # もう1階層だけ潜る（OneDrive等で余計な親が付く場合）
            if os.path.isdir(cand):
                for sub in sorted(os.listdir(cand)):
                    cand2 = os.path.join(cand, sub)
                    if _addata_is_valid(cand2):
                        return (cand2, os.path.join(entry, sub))
    except OSError:
        pass
    return (None, 'Addataの構造（A〜Zの1文字フォルダ／車種コード／*.DB）が見つかりません')


# 展開先を掃除するまでの猶予。これより古いものは他セッションの
# 置き土産とみなして回収する。
_ADDATA_TMP_TTL_SEC = 6 * 3600


def _sweep_stale_addata_dirs():
    """古い Addata 展開先を回収する。

    Streamlit にはセッション終了フックが無く、解除せずにタブを閉じられると
    展開先が残り続ける。Addata は大きいので、放置するとディスクが尽きて
    アプリごと止まる。自分が作った形（一時ディレクトリ直下の addata_*）で、
    かつ十分に古いものだけを消す。
    """
    import glob as _glob, shutil as _sh, time as _t
    try:
        base = os.path.realpath(tempfile.gettempdir())
        # 使用中の展開先。os.path.realpath('\0') は全バージョンで例外に
        # なるため、番兵文字列を渡してはいけない（関数ごと死ぬ）。
        _keep_src = st.session_state.get(_ADDATA_UPLOAD_BASE_KEY)
        keep = os.path.realpath(_keep_src) if _keep_src else None
        now = _t.time()
        for d in _glob.glob(os.path.join(base, 'addata_*')):
            rd = os.path.realpath(d)
            if (os.path.dirname(rd) == base
                    and os.path.basename(rd).startswith('addata_')
                    and os.path.isdir(rd) and rd != keep
                    and now - os.path.getmtime(rd) > _ADDATA_TMP_TTL_SEC):
                _sh.rmtree(rd, ignore_errors=True)
    except Exception:
        pass


def _discard_uploaded_addata():
    """アップロードされた Addata の展開先を消し、セッションから外す。

    消すのは mkdtemp で作った展開先そのものだけにする。ルートの親を
    たどって消すと、ZIPが直下構造だったときに /tmp ごと消してしまう。
    """
    base = st.session_state.pop(_ADDATA_UPLOAD_BASE_KEY, None)
    st.session_state.pop(_ADDATA_UPLOAD_KEY, None)
    st.session_state.pop('_addata_upload_label', None)
    st.session_state.pop('_addata_zip_id', None)
    if base and os.path.isdir(base) and os.path.basename(base).startswith('addata_'):
        import shutil as _sh
        _sh.rmtree(base, ignore_errors=True)


def find_addata_dir():
    """Addata ルートを返す。見つからなければ None。"""
    # 1. この画面でアップロードされたもの
    try:
        up = st.session_state.get(_ADDATA_UPLOAD_KEY)
        if up and _addata_is_valid(up):
            # 使用中であることを更新時刻で示す。展開したきりだと、
            # 長時間開いている別セッションの Addata を掃除で消してしまう。
            try:
                os.utime(st.session_state.get(_ADDATA_UPLOAD_BASE_KEY) or up, None)
            except OSError:
                pass
            return up
    except Exception:
        pass
    # 2. 環境変数 / secrets（Docker・Cloud Run で外部ボリュームを渡す場合）
    for _env in (os.environ.get('ADDATA_ROOT'), _secret_addata_root()):
        if _env and _addata_is_valid(_env):
            return _env
    # 3. ローカルの標準的な設置場所
    try:
        import addata_locator as _loc
        found = _loc.find_addata()
        if found and _addata_is_valid(found):
            return found
    except Exception:
        pass
    return None


def _secret_addata_root():
    """st.secrets の ADDATA_ROOT（未設定でも例外にしない）。"""
    try:
        return st.secrets.get('ADDATA_ROOT', '')
    except Exception:
        return ''


def find_ka06_path(addata_base):
    """KA06_ALL.DB（車種マスタ）のパス。無ければ None。"""
    if not addata_base:
        return None
    p = os.path.join(addata_base, 'COM', 'KA06_ALL.DB')
    return p if os.path.exists(p) else None


def identify_vehicle(addata_base, vehicle_data):
    """車検証情報から Addata の車種コードを特定する。"""
    if not addata_base:
        return {'match_layer': 3, 'is_supported': False, 'reason': 'Addata未検出'}
    try:
        from auto_matching import identify_vehicle_wrapper
    except Exception as e:
        return {'match_layer': 3, 'is_supported': False,
                'reason': f'車種特定モジュールを読み込めません: {e}'}
    try:
        return identify_vehicle_wrapper(addata_base, vehicle_data or {})
    except Exception as e:
        return {'match_layer': 3, 'is_supported': False,
                'reason': f'車種特定に失敗しました: {e}'}


def match_parts_with_addata(items, addata_folder, vehicle_info=None):
    """明細を Addata マスタと突き合わせる。(items, 照合できたか) を返す。

    照合できなかった場合は元の items をそのまま返す。ここで例外を
    投げると NEO 生成まるごとが失敗するので、必ず握って戻す。
    """
    if not items or not addata_folder:
        return (items, False)
    try:
        from auto_matching import match_pdf_items_to_addata
    except Exception:
        return (items, False)
    try:
        matched = match_pdf_items_to_addata(items, vehicle_info or {}, addata_folder)
    except Exception:
        return (items, False)
    if isinstance(matched, tuple):
        matched = matched[0]
    if not isinstance(matched, list) or not matched:
        return (items, False)
    return (matched, True)


def complement_vehicle_info_with_gemini(api_key, model_code, current_car_name, current_engine):
    """
    車両特定(KA06_ALL)に失敗した場合、型式(model_code)からGemini Web検索等で補完を試みる。
    Google Search Tool を有効化して正確な車種名とエンジン型式を取得する。
    """
    if not api_key or not model_code:
        return {}

    import json
    from google import genai
    from google.genai import types

    client = _get_genai_client(api_key)

    prompt = f'''あなたは日本の自動車の専門家です。
以下の型式（Model Code）を持つ自動車の「一般的な車種名（通称名）」と「エンジン型式」を特定し、厳密なJSON形式で出力してください。
型式: {model_code}
現在の情報（空欄の場合あり）:
- 車種名: {current_car_name}
- エンジン型式: {current_engine}

出力形式は必ず以下のJSONだけにしてください。マークダウンや説明は不要です。
{{
    "car_name": "車種名（例: プリウス, Ｎ－ＢＯＸ, アトレー など。メーカー名は含めない）",
    "engine_model": "エンジン型式（例: 2ZR-FXE, S07B など）"
}}
もし明確に不明な場合は、無理に嘘をつかず空文字列にしてください。'''

    try:
        _veh_model = get_default_gemini_model(api_key)
        response = client.models.generate_content(
            model=_veh_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=512,
                response_mime_type='application/json',
            ),
        )
        text = response.text.strip()
        if text.startswith('```json'):
            text = text[7:]
        if text.endswith('```'):
            text = text[:-3]
        info = json.loads(text.strip())
        return info
    except Exception as e:
        print("Gemini Fallback Error:", e)
        return {}

def generate_discrepancy_report_pdf(discrepancies, total_diff, vehicle_info):
    """
    ReportLabを使用して部品価格の差額レポート(PDF)を生成する
    """
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    import io

    # 日本語フォントの登録 (Windows標準のメイリオを試行)
    try:
        pdfmetrics.registerFont(TTFont('Meiryo', 'meiryo.ttc'))
        font_name = 'Meiryo'
    except Exception:
        try:
            pdfmetrics.registerFont(TTFont('MSGothic', 'msgothic.ttc'))
            font_name = 'MSGothic'
        except Exception:
            # フォールバック (ビルトインの HeiseiKakuGo-W5)
            pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
            font_name = 'HeiseiKakuGo-W5'

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=20 * mm, leftMargin=20 * mm,
                            topMargin=20 * mm, bottomMargin=20 * mm)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='JapaneseTitle', fontName=font_name, fontSize=18, alignment=1, spaceAfter=20))
    styles.add(ParagraphStyle(name='JapaneseNormal', fontName=font_name, fontSize=10, spaceAfter=10))
    styles.add(ParagraphStyle(name='JapaneseBold', fontName=font_name, fontSize=12, spaceAfter=10, textColor=colors.red))

    elements = []

    # タイトル
    elements.append(Paragraph("Addata マスタ連携 金額差分レポート", styles['JapaneseTitle']))

    # 車両情報
    if vehicle_info:
        v_str = f"対象車両: {vehicle_info.get('car_name', '')} {vehicle_info.get('car_model', '')} (車台番号: {vehicle_info.get('car_serial_no', '')})"
        elements.append(Paragraph(v_str, styles['JapaneseNormal']))

    date_str = f"出力日時: {now_jst().strftime('%Y/%m/%d %H:%M:%S')}"
    elements.append(Paragraph(date_str, styles['JapaneseNormal']))
    elements.append(Spacer(1, 10 * mm))

    # テーブル構築
    # ヘッダ
    table_data = [['No.', '判定', 'OCR 部品名', 'OCR 価格', '=> マスタ正式名称', 'マスタ定価', '数量', '差額 (小計)']]

    for i, d in enumerate(discrepancies):
        no_str = str(i + 1)
        
        m_level = d.get('_match_level', 0)
        judgment = "未合致" if m_level >= 4 or m_level == 0 else "合致"
        
        ocr_name = d.get('_original_name', '')
        ocr_price = d.get('_original_parts_amount', 0)
        
        master_name = d.get('_master_name', '')
        master_price = d.get('_master_price', 0)
        
        qty = d.get('quantity', 1)
        diff = (master_price - ocr_price) * qty
        
        # 品名は Paragraph に包む。素の文字列だと ReportLab が折り返さず、
        # 長い品名が右隣の金額欄に重なって数字が読めなくなる。
        _name_style = styles['JapaneseNormal']
        table_data.append([
            no_str,
            judgment,
            Paragraph(_xml_escape(ocr_name), _name_style),
            f"¥{ocr_price:,}",
            Paragraph(_xml_escape(master_name), _name_style),
            f"¥{master_price:,}",
            str(qty),
            f"¥{diff:,}"
        ])

    # テーブルスタイル
    t = Table(table_data, colWidths=[10*mm, 15*mm, 35*mm, 20*mm, 35*mm, 20*mm, 10*mm, 25*mm],
              repeatRows=1)  # 改ページ後も見出し行を繰り返す
    t.setStyle(TableStyle([
        ('FONT', (0,0), (-1,-1), font_name, 9),
        ('ALIGN', (0,0), (-1,0), 'CENTER'),
        ('ALIGN', (3,1), (3,-1), 'RIGHT'),
        ('ALIGN', (5,1), (5,-1), 'RIGHT'),
        ('ALIGN', (6,1), (6,-1), 'CENTER'),
        ('ALIGN', (7,1), (7,-1), 'RIGHT'),
        ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
        ('TEXTCOLOR', (0,0), (-1,0), colors.black),
        ('GRID', (0,0), (-1,-1), 0.5, colors.black),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('PADDING', (0,0), (-1,-1), 4),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 10 * mm))

    # 合計
    diff_color = 'red'
    if total_diff > 0:
        diff_str = f"マスタ適用による総額変動: +¥{total_diff:,}"
        diff_color = 'blue'
    elif total_diff < 0:
        diff_str = f"マスタ適用による総額変動: ¥{total_diff:,}"
    else:
        diff_str = "マスタ適用による総額変動: なし (¥0)"
        diff_color = 'black'

    # 赤・青など色付きスタイル
    styles.add(ParagraphStyle(name='DiffStyle', fontName=font_name, fontSize=14, alignment=2, textColor=diff_color))
    elements.append(Paragraph(diff_str, styles['DiffStyle']))

    doc.build(elements)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes

def generate_beta_discrepancy_report_pdf(estimate_data, calc_parts, calc_wages, pdf_parts, pdf_wages, vehicle_info):
    """
    ReportLabを使用してベタ打ちモード用の金額ズレ検証レポート(PDF)を生成する
    """
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    import io

    # 日本語フォントの登録 (Windows標準のメイリオを試行)
    try:
        pdfmetrics.registerFont(TTFont('Meiryo', 'meiryo.ttc'))
        font_name = 'Meiryo'
    except Exception:
        try:
            pdfmetrics.registerFont(TTFont('MSGothic', 'msgothic.ttc'))
            font_name = 'MSGothic'
        except:
            # フォールバック (ビルトイン)
            pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
            font_name = 'HeiseiKakuGo-W5'

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(595.27, 841.89), # A4
                            rightMargin=15 * 2.83, leftMargin=15 * 2.83,
                            topMargin=15 * 2.83, bottomMargin=15 * 2.83)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='JapaneseTitle', fontName=font_name, fontSize=16, alignment=1, spaceAfter=15))
    styles.add(ParagraphStyle(name='JapaneseNormal', fontName=font_name, fontSize=10, spaceAfter=8))
    styles.add(ParagraphStyle(name='JapaneseBold', fontName=font_name, fontSize=12, spaceAfter=8, textColor=colors.red))

    elements = []
    elements.append(Paragraph("ベタ打ちモード 金額ズレ検証レポート", styles['JapaneseTitle']))

    if vehicle_info:
        v_str = f"対象車両: {vehicle_info.get('car_name', '')} {vehicle_info.get('car_model', '')} (車台番号: {vehicle_info.get('car_serial_no', '')})"
        elements.append(Paragraph(v_str, styles['JapaneseNormal']))
    
    import datetime
    date_str = f"出力日時: {now_jst().strftime('%Y/%m/%d %H:%M:%S')}"
    elements.append(Paragraph(date_str, styles['JapaneseNormal']))
    elements.append(Spacer(1, 5 * 2.83))

    parts_diff = calc_parts - pdf_parts
    wage_diff = calc_wages - pdf_wages

    sum_data = [
        ['項目', 'PDF原本 記載値', 'AI抽出 明細合算値', '差額'],
        ['部品合計', f"¥{pdf_parts:,}", f"¥{calc_parts:,}", f"{'+' if parts_diff>0 else ''}{parts_diff:,}円"],
        ['工賃合計', f"¥{pdf_wages:,}", f"¥{calc_wages:,}", f"{'+' if wage_diff>0 else ''}{wage_diff:,}円"]
    ]
    t_sum = Table(sum_data, colWidths=[30*2.83, 40*2.83, 40*2.83, 30*2.83])
    t_sum.setStyle(TableStyle([
        ('FONT', (0,0), (-1,-1), font_name, 10),
        ('ALIGN', (0,0), (-1,0), 'CENTER'),
        ('ALIGN', (1,1), (-1,-1), 'RIGHT'),
        ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
        ('GRID', (0,0), (-1,-1), 0.5, colors.black),
        ('TEXTCOLOR', (3,1), (3,1), colors.red if parts_diff != 0 else colors.black),
        ('TEXTCOLOR', (3,2), (3,2), colors.red if wage_diff != 0 else colors.black),
    ]))
    elements.append(t_sum)
    elements.append(Spacer(1, 10 * 2.83))

    elements.append(Paragraph("【AIが抽出した全明細行】（※ズレ箇所特定のためのリスト）", styles['JapaneseNormal']))
    table_data = [['No.', '部品/作業名', '区分', '数量', '部品金額', '工賃']]
    items = estimate_data.get('items', [])
    for i, it in enumerate(items):
        name = it.get('name', '')
        method = it.get('method', '')
        # quantity might be float in some edges
        try:
            qty = int(float(it.get('quantity', 1)))
        except:
            qty = 1
        
        try:
            p_amt = int(float(it.get('parts_amount', 0)))
        except:
            p_amt = it.get('_original_parts_amount', 0)
            
        try:
            w_amt = int(float(it.get('wage', 0)))
        except:
            w_amt = 0

        table_data.append([
            str(i + 1), name, method, str(qty), f"¥{p_amt:,}", f"¥{w_amt:,}"
        ])
    
    t_items = Table(table_data, colWidths=[10*2.83, 75*2.83, 25*2.83, 15*2.83, 27*2.83, 27*2.83])
    t_items.setStyle(TableStyle([
        ('FONT', (0,0), (-1,-1), font_name, 8),
        ('ALIGN', (0,0), (-1,0), 'CENTER'),
        ('ALIGN', (3,1), (-1,-1), 'RIGHT'),
        ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
        ('GRID', (0,0), (-1,-1), 0.5, colors.black),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('PADDING', (0,0), (-1,-1), 3),
    ]))
    elements.append(t_items)
    doc.build(elements)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes


# ============================================================
# Gemini 2.5 Flash 向け共通プロンプト定義
# 送信形式: CORE_PROMPT + "\n\n" + TASK_PROMPTS[task_type]
# ============================================================

CORE_PROMPT = """<system_instruction>
あなたは日本語の自動車関連業務帳票（自動車修理見積書、車検証、FAX表紙など）を解析し、後続のNEOシステム連携用に構造化データを抽出する高精度OCR・解析APIエンジンです。
入力された画像またはPDFを精読し、推測を一切排除して、指定された<output_format>の厳格なJSONのみを出力してください。
</system_instruction>

<golden_rules>
1. 【完全転写】入力画像に記載されているテキスト・数値をそのまま抽出すること。存在しない値の推測、補完、勝手な計算は絶対に行わない。
2. 【欠落防止】ページ跨ぎ、折り返し行、セクション区切り、ページ最下部の行などを絶対に漏らさないこと。
3. 【ノイズ排除】挨拶、説明文、Markdownの装飾（```json など）は一切出力しない。純粋なJSON文字列のみを返すこと。
4. 【数値の正規化】金額や数量は、カンマ(,)を除去した半角整数の数値型(Number)で出力すること。読み取れない数値は 0 とし、読み取れない文字は "不明" とする。
</golden_rules>

<extraction_logic>
- カンマと空白: 連続するカンマ（例: ,,,,,）は「空白セル」を意味する。列の右ズレを防ぐこと。
- 行の結合: 部品名などが不自然に改行されている場合は、文脈から1つのレコードに結合する。
- 金額の分離（単一列に混在している場合）:
  - 品番がある行、または「部品」「材料」の名称行 → 「部品金額」へ。
  - 「交換」「脱着」「調整」「修理」「鈑金」「塗装」「点検」「診断」「設定」等の作業名行 → 「技術料」へ。
- 外車ディーラー見積（BMW、ベンツ等）:
  - 「Labor」「工賃」に相当する金額 → 「技術料」へ。
  - 「Parts」「部品」に相当する金額 → 「部品金額」へ。
  - 英数字・ハイフン混じりの品番は必ず「部品品番」へ。
- 区分の判定ルール（作業内容から以下の優先順位で判定して文字列を割り当てる）:
  1. 【重要】部品名称および部品金額の計上があるが、技術料（工賃）の計上がない行 → "取替"
  2. 「取替」「交換」「取換」「取り替え」を含む → "取替"
  3. 「脱着」「取外」「取付」「組付」を含む → "脱着"
  4. 「鈑金」「板金」を含む → "鈑金"
  5. 「塗装」「ペイント」「ワックス」「加算」「ブース」を含む → "塗装"
  6. 「修理」「補修」「分解」「修正」「光軸」「フィッティング」「コーディング」「穴あけ」「シーリング」「点検」「消去」「設定」「調整」を含む → "修理"
  7. 「研磨」「磨き」「写真代」「ショートパーツ」を含む → ""（空白）
</extraction_logic>"""


TASK_PROMPTS = {}

TASK_PROMPTS["shaken_ocr"] = """<task_execution>
タスク名: shaken_ocr（車検証OCR）

あなたは日本の車検証（自動車検査証）を読み取るOCRエキスパートです。
提供された画像またはPDFから以下の情報を正確に読み取ってください。

【複数ページPDFの場合の重要ルール】
- PDFが複数ページある場合、全ページを確認し「自動車検査証」または「自動車検査証記録事項」が記載されたページを特定すること
- 照会状、FAX送付状、見積書、写真などの車検証以外のページは無視すること
- 「自動車検査証記録事項」（電子車検証のA4印刷版）と「自動車検査証」（従来のカード型）の両方がある場合は、「自動車検査証記録事項」を優先すること（情報量が多いため）
- 車検証ページが見つからない場合は全フィールドを空にしてconfidence=0.0を返すこと

対応書式:
- 従来の車検証（自動車検査証）— カード型、透かし模様あり
- 電子車検証の「自動車検査証記録事項」（A4用紙に印刷されたもの、セクション番号付き）

読み取り対象フィールドと対応する記載欄:
- customer_name: 使用者の氏名又は名称（***の場合は所有者名で代替）
- owner_name: 所有者の氏名又は名称
- postal_no: 所有者の住所から郵便番号を推定（不明なら""）
- prefecture: 所有者の住所 → 都道府県
- municipality: 所有者の住所 → 市区町村
- address_other: 所有者の住所 → 町名・番地以降
- car_reg_department: 自動車登録番号の地名部分（例: "北九州", "品川", "福岡"）
- car_reg_division: 自動車登録番号の分類番号（例: "３４６"） → 全角数字で出力
- car_reg_business: 自動車登録番号のひらがな（例: "の"） → 全角ひらがなで出力
- car_reg_serial: 自動車登録番号の一連番号（例: "１２２４"） → 全角数字で出力
- car_serial_no: 車台番号（例: "AYH30-0145328"）
- car_name: 車名（例: "トヨタ"）
- car_model: 型式（例: "6AA-AYH30W"）
- car_model_designation: 型式指定番号（例: "19557"）
- car_category_number: 類別区分番号（例: "0172"）
- engine_model: 原動機の型式（例: "2AR-2JM-2FM"）
- body_color: 車体の色（記載がなければ""）
- color_code: カラーコード（記載がなければ""）
- trim_code: トリムコード（記載がなければ""）
- car_weight: 車両重量（kg、整数）
- engine_displacement: 総排気量又は定格出力の数値（cc/L → cc整数に統一。例: 2.49L → 2490）
- kilometer: 走行距離計表示値（km、整数。備考欄の「走行距離計表示値」から読み取る）
- term_date: 有効期間の満了する日 → YYYYMMDD形式（和暦→西暦変換必須）
- car_reg_date: 初度登録年月 → YYYYMM00形式（和暦→西暦変換必須）
- confidence: 読み取り信頼度 0.0〜1.0

重要ルール:
- 入力資料に記載されている文字を一言一句そのまま抽出する
- 推測での補完は絶対に行わない
- 読み取り不能な文字列は "" にする
- 読み取り不能な数値は 0 にする
- 和暦→西暦変換: 令和1=2019, 令和2=2020, ..., 令和7=2025, 令和8=2026, 令和9=2027 / 平成31=2019, 平成30=2018
- 自動車登録番号は「地名 分類番号 ひらがな 一連番号」の4要素に正確に分割する
- 「自動車検査証記録事項」の場合、「1.基本情報」「2.所有者情報」「3.車両詳細情報」「4.備考」の各セクションを漏れなく読み取る

<output_format>
{
  "customer_name": "",
  "owner_name": "",
  "postal_no": "",
  "prefecture": "",
  "municipality": "",
  "address_other": "",
  "car_reg_department": "",
  "car_reg_division": "",
  "car_reg_business": "",
  "car_reg_serial": "",
  "car_serial_no": "",
  "car_name": "",
  "car_model": "",
  "car_model_designation": "",
  "car_category_number": "",
  "engine_model": "",
  "body_color": "",
  "color_code": "",
  "trim_code": "",
  "car_weight": 0,
  "engine_displacement": 0,
  "kilometer": 0,
  "term_date": "",
  "car_reg_date": "",
  "confidence": 0.0
}
</output_format>
</task_execution>"""


TASK_PROMPTS["estimate_cover_check"] = """<task_execution>
タスク名: fax_cover_check

このページが宛先・件名・枚数・メッセージのみのFAX送付状（カバーシート）であるかを判定します。見積明細や金額合計が含まれていればfalseです。

重要な判定基準:
- ページ上部に「送信日時」「FAX番号」等が印字されていても、見積書の明細・合計欄を含んでいれば is_fax_cover = false
- 「御見積書」「修理費用明細書」「部品代」「工賃」「合計」などが記載されていれば is_fax_cover = false

page_type の値: fax_cover / estimate / vehicle / other

<output_format>
{
  "step_by_step_reasoning": "見積明細や合計金額の有無を確認した結果",
  "is_fax_cover": false,
  "page_type": "estimate",
  "reason": ""
}
</output_format>
</task_execution>"""


TASK_PROMPTS["estimate_header_totals"] = """<task_execution>
タスク名: header_total_extraction

合計値・修理工場名・車両情報を抽出します。「ページ小計」は総合計に使用しないでください。
「部品計」「工賃計」の明示的な小計行が存在しない単列金額形式の場合は、部品計・工賃計を 0 とし、総合計のみを抽出してください。

金額探索ルール:
- 「部品計」「部品代」「部品合計」「部品・油脂」等の明示的な小計行 → pdf_parts_total
- 「工賃計」「技術料合計」「工賃合計」等の明示的な小計行 → pdf_wage_total
- 「合計」「総合計」「御見積合計金額」「御見積金額」「見積金額」「請求金額」等 → pdf_grand_total
- 値引き額 → discount_amount
- Honda Cars系はページ1上部サマリーボックスの合計値も確認する

<output_format>
{
  "step_by_step_reasoning": "金額がどこに記載されていたか、どのように数値を判定したかの簡潔な思考プロセス",
  "repair_shop_name": "不明",
  "vehicle_info": {
    "car_name": "", "car_model": "", "engine_model": "",
    "color_code": "", "color_name": "", "trim_code": "",
    "grade": "", "model_year": "", "chassis_no": "", "mileage": ""
  },
  "pdf_parts_total": 0,
  "pdf_wage_total": 0,
  "discount_amount": 0,
  "pdf_grand_total": 0
}
</output_format>
</task_execution>"""


TASK_PROMPTS["estimate_detail_page"] = """<task_execution>
タスク名: detail_extraction

文書上部から基本情報を抽出し、明細行を配列で抽出してください。合計行・小計行・消費税行、および値引き（値引/割引/サービス）行は明細配列に含めないでください（値引きは別途ヘッダから取得します）。

<output_format>
{
  "step_by_step_reasoning": "行のズレや欠落がないか、部品と工賃の分離をどう行ったかの簡潔な思考プロセス",
  "basic_info": {
    "estimate_date": "文字列",
    "customer_name": "文字列",
    "car_type": "文字列",
    "registration_number": "文字列",
    "model_code": "文字列"
  },
  "details": [
    {
      "work_or_part_name": "文字列",
      "category": "文字列 (区分判定ルールに従う)",
      "index_value": "文字列 (指数)",
      "labor_fee": 0,
      "quantity": 0,
      "part_price": 0,
      "part_number": "文字列"
    }
  ]
}
</output_format>
</task_execution>"""


TASK_PROMPTS["estimate_validation_repair"] = """あなたは、見積書抽出結果の金額検算エンジンです。
前回の読み取り結果に金額誤差が検出されたため、見積書を最初から再精読して完全に正確な抽出を行ってください。

原則:
- 1円の狂いも許されない
- 見積書記載額を一言一句正確に読む
- 勝手な査定、減額、工法変更、項目削除をしない
- 前回結果を盲信せず最初から読み直す
- 不一致原因を行単位で特定する

必須チェック:
1. 行の見落とし
2. 部品列と工賃列の取り違え
3. 数量の誤読
4. 金額の読み取りミス（桁ずれ）
5. 複数行を1行に合算している
6. ページ下端の取りこぼし
7. 小計/合計の誤加算
8. 行ずれによる列誤認
9. 値引き行を items に混入していないか
10. 明細行を誤って除外していないか

返却JSON形式は前回と同じ estimate_detail_page 形式で返すこと。
status フィールドを追加すること:
- "success": 期待合計と一致
- "failed": 不一致あり

成功条件:
- expected_totals と calculated_totals が完全一致
- ページ下端の取りこぼしなし
- 不確定数字なし"""


def _build_prompt(task_type: str, extra: str = "") -> str:
    """CORE_PROMPT + TASK_PROMPTS[task_type] + extra を結合して返す"""
    task_part = TASK_PROMPTS.get(task_type, "")
    parts = [CORE_PROMPT, task_part, extra]
    return "\n\n".join(p for p in parts if p)


def analyze_vehicle_registration(api_key, file_bytes, mime_type, model_name=None,
                                 _retried=False):
    """車検証をAI-OCRで解析（JSON mode + プロンプトベースの構造化出力）

    model_name を省略した場合は、サイドバーで選択中のモデル →
    利用可能なモデルの既定 の順に解決する。定数 GEMINI_MODEL を直接使うと、
    そのモデルが提供終了したときに車検証OCRだけが恒久的に失敗するため。
    """
    prompt = _build_prompt("shaken_ocr")
    if not model_name:
        try:
            model_name = st.session_state.get('selected_model')
        except Exception:
            model_name = None
    if not model_name:
        model_name = get_default_gemini_model(api_key)

    # 方式1: response_schema を使用（全フィールドstring型で安全にパース）
    _schema_shaken = {
        "type": "object",
        "properties": {
            "customer_name":         {"type": "string"},
            "owner_name":            {"type": "string"},
            "postal_no":             {"type": "string"},
            "prefecture":            {"type": "string"},
            "municipality":          {"type": "string"},
            "address_other":         {"type": "string"},
            "car_reg_department":    {"type": "string"},
            "car_reg_division":      {"type": "string"},
            "car_reg_business":      {"type": "string"},
            "car_reg_serial":        {"type": "string"},
            "car_serial_no":         {"type": "string"},
            "car_name":              {"type": "string"},
            "car_model":             {"type": "string"},
            "car_model_designation": {"type": "string"},
            "car_category_number":   {"type": "string"},
            "engine_model":          {"type": "string"},
            "body_color":            {"type": "string"},
            "color_code":            {"type": "string"},
            "trim_code":             {"type": "string"},
            "car_weight":            {"type": "string"},
            "engine_displacement":   {"type": "string"},
            "kilometer":             {"type": "string"},
            "term_date":             {"type": "string"},
            "car_reg_date":          {"type": "string"},
            "confidence":            {"type": "string"},
        },
    }
    result = {}
    _method_used = ""
    _last_error = None
    if not api_key:
        # キーが無ければ呼ぶ前に失敗を返す（黙って空の車両情報を返さない）
        return {'_error': 'Gemini APIキーが設定されていません'}
    try:
        from google.genai import types
        client = _get_genai_client(api_key)
        file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, file_part],
            config={
                "temperature": 0.0,
                "max_output_tokens": 4096,
                "response_mime_type": "application/json",
                "response_schema": _schema_shaken,
            },
        )
        if response.text and response.text.strip():
            try:
                result = json.loads(response.text)
                _method_used = "response_schema"
            except (json.JSONDecodeError, TypeError):
                result = extract_json_from_response(response.text)
                _method_used = "response_schema+extract"
    except Exception as e:
        print(f"[shaken_ocr] response_schema failed: {e}")
        _method_used = "fallback"
        _last_error = e

    # 方式1で空結果 → 方式2: シンプルなJSON modeにフォールバック
    if not result or not any(v for v in result.values() if v and str(v).strip()):
        try:
            print(f"[shaken_ocr] Method '{_method_used}' returned empty, trying json_mode fallback")
            result_text = call_gemini(api_key, file_bytes, mime_type, prompt,
                                      model_name=model_name, use_json_mode=True)
            if result_text:
                try:
                    result = json.loads(result_text)
                    _method_used = "json_mode"
                except (json.JSONDecodeError, TypeError):
                    result = extract_json_from_response(result_text)
                    _method_used = "json_mode+extract"
        except Exception as e2:
            print(f"[shaken_ocr] json_mode fallback also failed: {e2}")
            result = {}
            _last_error = e2

    # 2方式とも空 → 失敗として理由を返す。空dictを返すと呼び出し側が
    # 「読み取れたが全項目が空」と区別できず、車両情報なしのNEOが
    # 黙って作られてしまう。
    if not result or not any(v for v in result.values() if v and str(v).strip()):
        _msg = str(_last_error) if _last_error else '車検証のページを判別できませんでした'
        # 失敗の理由を記録しないと、提供終了やクォータ超過のモデルを
        # 毎回選び直して4回ずつ無駄に叩き続ける（明細側には同じ記録が
        # あるのに、車検証側だけ抜けていた）。
        _model_used = model_name or get_default_gemini_model(api_key)
        _switch = False
        if _is_model_unavailable_error(_msg):
            _mark_model_unavailable(api_key, _model_used)
            _msg = f'モデル「{_model_used}」は利用できません（提供終了の可能性があります）'
            _switch = True
        elif '429' in _msg or 'RESOURCE_EXHAUSTED' in _msg:
            _quota_exhausted_set().add(_model_used)
            try:
                _availability_cache().pop(_model_cache_key(api_key), None)
            except Exception:
                pass
            _msg = 'Gemini APIのクォータが上限に達しました'
            _switch = True
        elif 'API key not valid' in _msg or 'API_KEY_INVALID' in _msg:
            _msg = 'Gemini APIキーが正しくありません'
        # モデルが原因なら、使える別モデルで1度だけやり直す
        if _switch and not _retried:
            _alt = get_alternative_gemini_model(api_key, _model_used)
            if _alt and _alt != _model_used:
                print(f"[shaken_ocr] '{_model_used}' が使えないため "
                      f"'{_alt}' で再試行します", file=sys.stderr)
                return analyze_vehicle_registration(api_key, file_bytes, mime_type,
                                                    _alt, _retried=True)
        return {'_error': _msg}

    # 数値フィールドを文字列→数値に変換（response_schema が string 型で返すため）
    for int_key in ('car_weight', 'engine_displacement', 'kilometer'):
        if int_key in result:
            result[int_key] = safe_int(result[int_key])
    if 'confidence' in result:
        result['confidence'] = safe_float(result.get('confidence', 0), 0.0)

    print(f"[shaken_ocr] method={_method_used}, car_name={result.get('car_name','')}, "
          f"car_model={result.get('car_model','')}, reg={result.get('car_reg_department','')}")

    # 車名が空欄の場合、車台番号からメーカーを推定
    if not result.get('car_name') and result.get('car_serial_no'):
        maker, _ = guess_manufacturer_from_vin(result['car_serial_no'])
        if maker:
            result['car_name'] = maker

    return result


def analyze_estimate_totals(api_key, file_bytes, mime_type, model_name):
    """1パス目: 合計値 + 見積書ヘッダの修理工場名・車両情報読み取り"""
    _combined_prompt = _build_prompt("estimate_header_totals")
    # response schema: totals + vehicle_info のみ
    _schema_totals = {
        "type": "object",
        "properties": {
            "repair_shop_name": {"type": "string"},
            "pdf_parts_total":  {"type": "integer"},
            "pdf_wage_total":   {"type": "integer"},
            "pdf_grand_total":  {"type": "integer"},
            "discount_amount":  {"type": "integer"},
            "confidence":       {"type": "number"},
            "vehicle_info": {
                "type": "object",
                "properties": {
                    "car_name":    {"type": "string"},
                    "car_model":   {"type": "string"},
                    "engine_model":{"type": "string"},
                    "color_code":  {"type": "string"},
                    "color_name":  {"type": "string"},
                    "trim_code":   {"type": "string"},
                    "grade":       {"type": "string"},
                    "model_year":  {"type": "string"},
                    "chassis_no":  {"type": "string"},
                    "mileage":     {"type": "string"},
                },
            },
        },
    }
    try:
        from google.genai import types
        client = _get_genai_client(api_key)
        file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        response = client.models.generate_content(
            model=model_name,
            contents=[_combined_prompt, file_part],
            config={
                "temperature": 0.0,
                "max_output_tokens": 4096,
                "response_mime_type": "application/json",
                "response_schema": _schema_totals,
            },
        )
        if response.text:
            try:
                return json.loads(response.text)
            except (json.JSONDecodeError, TypeError):
                return extract_json_from_response(response.text)
    except Exception as e:
        err_msg = str(e)
        if _is_model_unavailable_error(err_msg):
            _mark_model_unavailable(api_key, model_name)
            raise RuntimeError(f"モデル '{model_name}' は利用できません（提供終了）。サイドバーで別のモデルを選択してください。\n詳細: {err_msg}") from e
    return None


# 明細ではなく集計を表す語（これで「終わる」品名は明細として扱わない）
_TOTAL_SUFFIXES = ('合計', '小計', '総額', '総計', '消費税', '税額')
# 単独で使われた場合だけ集計とみなす語
# （「税」は入れない。「税金」「重量税」のような正当な明細まで消えるため）
_TOTAL_EXACT = ('内税', '外税', '請求', 'ご請求', '計', '以上', '総合計', '税込', '税抜')
# 「部品計」「工賃計」のように、この語に「計」が続く形も集計行
_TOTAL_PREFIXES_FOR_KEI = (
    '部品', '部品代', '工賃', '技術料', '諸費用', '費用', '材料', '塗装',
    '作業', '整備', '修理', '合計', '小計', '値引', 'その他',
    '課税', '非課税', '税込', '税抜',
)


# CSVの見出し名 → 内部キー
# 見出し名 → 内部キー。曖昧な短い別名は最後に置き、具体的な名前を優先する。
# 「部品」だけの列は品番のことも金額のこともあるため候補に入れない。
_COLUMN_ALIASES = {
    'name':         ('品名', '部品名', '品目', '名称', '摘要', '作業内容'),
    'work_code':    ('区分', '作業区分'),
    'quantity':     ('数量', '個数'),
    'parts_amount': ('部品金額', '部品代', '部品価格', '部品単価', '部品油脂'),
    'wage':         ('工賃', '技術料', '作業工賃'),
    'part_no':      ('部品コード', '部品番号', '品番'),
    'index_value':  ('工数', '指数'),
}


def _build_column_map(header_row) -> dict:
    """見出し行から「内部キー → 列位置」を作る。判別できない場合は空dict。"""
    if not header_row:
        return {}
    # 空白で切り捨てると「部品 コード」が「部品」になり、品番列を見失う。
    # 空白は詰めるだけにし、括弧書きの注記だけを落とす。
    cells = []
    for c in header_row:
        c = re.sub(r'[\s\u3000・、，]', '', str(c or ''))
        # 括弧書きの注記だけを落とす。閉じ括弧が無い場合は以降を捨てる。
        c = re.sub(r'[（(\[【][^）)\]】]*[）)\]】]', '', c)
        c = re.sub(r'[（(\[【].*$', '', c)
        cells.append(c)
    colmap = {}
    # 別名を外側で回し、具体的な名前から順に列を確保する。
    # 列を外側で回すと「部品 コード」→「部品」のような弱い一致が
    # 先に金額列を奪い、部品代が全部0になる。
    for key, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            for i, c in enumerate(cells):
                if c == alias and i not in colmap.values():
                    colmap[key] = i
                    break
            if key in colmap:
                break
    # 品名の列が見つからないなら、この見出しは当てにならないので位置決め打ちに戻す
    if 'name' not in colmap:
        return {}
    return colmap


def _is_total_row_name(name: str) -> bool:
    """品名が集計行のものか判定する。

    見積書の合計欄は「合計」「小計(税抜)」「税込合計」「合計金額」
    「【合計】」「小計①」「合　計　金　額」など表記が揺れる。
    空白・括弧・丸数字・通貨記号を落としたうえで、末尾が集計語かで判断する。
    「合計表示灯」「総額メーター」「温度計」のような部品名は末尾が
    集計語ではないので残る。
    """
    nm = re.sub(r'[\s\u3000【】\[\]「」『』¥￥:：･・]', '', str(name or ''))
    nm = re.sub(r'[（(].*?[）)]', '', nm)          # 括弧書きを除去
    nm = re.sub(r'[0-9０-９①-⑳%％]+$', '', nm)     # 末尾の番号・率を除去
    nm = nm.replace('御', 'ご')                     # 御請求額 → ご請求額
    if not nm:
        return False
    if re.fullmatch(r'(?i)(sub)?total', nm):        # 英語表記の合計欄
        return True
    if nm in _TOTAL_EXACT or nm.endswith(_TOTAL_SUFFIXES):
        return True
    # 「部品計」「工賃計」「諸費用計」のように、集計対象＋「計」の形
    if nm.endswith('計') and nm[:-1] in _TOTAL_PREFIXES_FOR_KEI:
        return True
    # 「合計金額」「ご請求額」のように集計語の後ろに金額表現が付く形
    nm2 = re.sub(r'(金額|額|計)$', '', nm)
    if nm2 and nm2 != nm and (nm2 in _TOTAL_EXACT or nm2.endswith(_TOTAL_SUFFIXES)):
        return True
    return False


def parse_csv_to_items(csv_text: str, return_notes: bool = False):
    """Claude.ai / Gemini.ai 等から出力されたCSVテキストをitemsリストに変換する。
    期待フォーマット（ヘッダあり）:
        品名,区分,数量,部品金額,工賃,部品コード
    """
    import csv as _csv
    import io as _io

    _trailer_notes: list = []
    _dropped_amount: list = []

    # BOM除去・改行正規化
    text = csv_text.strip().lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    # AIの回答をそのまま貼り付けたときの前後のコードフェンスだけを外す。
    # 全行から除去すると、引用符で囲まれた複数行フィールドを壊してしまう。
    text = re.sub(r'^[^\n]*```[a-zA-Z]*\n', '', text)
    text = re.sub(r'\n```[^\n]*$', '', text)
    # フェンスの後ろに説明文が続くと閉じフェンスが行の途中に残り、
    # 「```」だけの行が金額0円の明細として取り込まれてしまう。
    # 行全体がフェンスだけの行に限って落とす（引用中の本文は壊さない）。
    text = '\n'.join(l for l in text.split('\n')
                     if not re.fullmatch(r'\s*```[a-zA-Z]*\s*', l))
    items = []
    try:
        reader = _csv.reader(_io.StringIO(text))
        rows = list(reader)
    except Exception:
        return (items, _trailer_notes) if return_notes else items

    if not rows:
        return (items, _trailer_notes) if return_notes else items

    # ヘッダ行を特定する。「品名」が無くてもヘッダらしい行なら読み飛ばす。
    # 以前は先頭セルに「品名」が無いとヘッダ行をそのまま明細として
    # 取り込み、「品目」のような別表記で先頭行がゴミ明細になっていた。
    _HEADER_WORDS = ('品名', '品目', '部品名', '名称', '摘要', '区分', '作業内容',
                     '数量', '個数', '数', '単価', '金額', '部品金額', '工賃',
                     '部品コード', '部品番号', '工数', '番号', '備考', '単位')

    def _norm_header_cell(c):
        # 「部品金額（税抜）」「数 量」のような装飾を外して見出し語と比べる
        c = re.sub(r'[\s\u3000・]', '', c)
        c = re.sub(r'[（(\[【][^）)\]】]*[）)\]】]', '', c)
        return c

    def _looks_like_header(r):
        if not r:
            return False
        cells = [c.strip() for c in r if c is not None]
        if not any(cells):
            return False
        # 金額・数量らしいセルが1つでもあれば明細行とみなす。
        # 「¥45,000」のように通貨記号や「円」が付く形も明細である。
        for c in cells[1:]:
            if c and re.fullmatch(r'[¥￥]?[\d,，．.\-]+円?', c):
                return False
        # 部分一致だと「部品コード」を含む品名などでヘッダ扱いになり、
        # 実データ行が1行まるごと捨てられる。セル全体の一致だけを数える。
        return sum(1 for c in cells if _norm_header_cell(c) in _HEADER_WORDS) >= 2

    # 「品名」見出しは何行目にあっても拾う。一方、見出しらしさによる推測は
    # 先頭数行に限る。表の途中の小見出し（「工賃」など）をヘッダと誤認すると、
    # それより前の明細が全部捨てられてしまう。
    def _looks_like_data(r):
        """品名と金額らしきセルを併せ持つ、明細とみなせる行か。"""
        if not r:
            return False
        cells = [str(c or '').strip() for c in r]
        if not any(cells):
            return False
        has_name = any(c and not re.fullmatch(r'[¥￥]?[\d,，．.\-]+円?', c) for c in cells)
        has_amount = any(
            c and re.fullmatch(r'[¥￥]?[\d,，．.\-]+円?', c)
            and re.search(r'[1-9]', c)
            for c in cells[1:]
        )
        return has_name and has_amount

    def _is_detail_start(r):
        """見出しの探索を打ち切るべき「本物の明細行」か。

        「見積番号,12345」のようなメタ情報行や、表の上に置かれた
        「御見積金額,,,203170,」で打ち切ると見出しを見失い、全列が
        ずれたうえ合計行が明細として二重計上されてしまう。
        """
        if not _looks_like_data(r):
            return False
        cells = [str(c or '').strip() for c in r]
        if sum(1 for c in cells if c) < 3:
            return False          # 2セルだけの行はメタ情報
        return not _is_total_row_name(cells[0])

    # 見出しは表の先頭付近にしかない。8行より下にある「品名,…」は
    # 2ページ目のページ見出しなので見出しとして採らない
    # （採ると、それより上の明細が丸ごと捨てられてしまう）。
    _HEADER_SCAN = 8
    header_idx = 0
    for i, row in enumerate(rows[:_HEADER_SCAN]):
        if _is_detail_start(row):
            break
        if row and '品名' in (row[0] or ''):
            header_idx = i + 1
            break
        if _looks_like_header(row):
            header_idx = i + 1
            break

    # 見出し行があれば列名で対応付ける。位置決め打ちだと、先頭に「No」列が
    # 付いただけで全列が1つずれ、部品代が工賃に化けてしまう。
    _colmap = _build_column_map(rows[header_idx - 1]) if header_idx > 0 else {}

    _claimed = set(_colmap.values())

    def _cell(row, key, pos):
        # 見出しに無い項目は位置で補う。「部品、油脂」「金額（部品）」の
        # ように別名表に無い列名だと、補わなければ部品代が全額消える。
        # ただし他のキーが既に確保した列（品名・区分など）は横取りしない。
        idx = _colmap.get(key)
        if idx is None:
            idx = pos if (pos is not None and pos not in _claimed) else None
        return row[idx].strip() if idx is not None and 0 <= idx < len(row) else ''

    row_idx = 0
    for row in rows[header_idx:]:
        if not row or not any(c.strip() for c in row):
            continue
        # 列数が足りない場合は右側を空文字で補完
        while len(row) < 6:
            row.append('')

        # 2ページ目以降で繰り返される見出し行が、品名「品名」の
        # 0円明細としてNEOに書き込まれるのを防ぐ
        if _looks_like_header(row) or (row[0] or '').strip() == '品名':
            continue

        name      = _cell(row, 'name', 0)
        category  = _cell(row, 'work_code', 1)
        qty       = safe_int(_cell(row, 'quantity', 2), 1)
        parts_amt = safe_int(_cell(row, 'parts_amount', 3))
        wage_amt  = safe_int(_cell(row, 'wage', 4))
        part_no   = _cell(row, 'part_no', 5)
        # 見出しに「工数」「指数」列があれば取り込む（従来は常に空だった）
        index_val = _cell(row, 'index_value', None)

        if not name:
            # 品名が無い行は明細として扱えない。ただし金額が入っているなら
            # 落としたことを知らせる。黙って落とすと、300行貼って「295行
            # 読み込み完了」とだけ出て、5行と その金額が消えたことに
            # 気づけない（CSV経路は原本合計との突き合わせも効かない）。
            if parts_amt or wage_amt:
                _dropped_amount.append(parts_amt + wage_amt)
            continue
        # 表の後ろにAIが書き足す説明文（「上記のとおりです。」など）は明細ではない。
        # 金額も数量も品番も無く、1セルだけの文章行に限って落とす。
        if (parts_amt == 0 and wage_amt == 0 and not part_no
                and sum(1 for c in row if str(c or '').strip()) == 1
                and (re.search(r'[。．!！?？]', name)
                     or re.search(r'(です|ます|ください|とおり|下さい)', name)
                     or len(name) > 24)):
            _trailer_notes.append(name.strip())
            continue
        # アプリ自身のプロンプトが末尾に付ける差異メモは明細ではない
        _nm_s = name.strip()
        if (re.fullmatch(r'(部品|工賃)相違[\s\u3000\d,，円]*', _nm_s)
                or (re.match(r'^(部品|工賃)相違', _nm_s)
                    and parts_amt == 0 and wage_amt == 0)):
            _trailer_notes.append(','.join(c.strip() for c in row if c.strip()))
            continue
        # 集計行の除外。
        # 「合計」「小計(税抜)」「税込合計」「合計金額」「【合計】」「小計①」など、
        # 装飾を取り除くと集計語で終わる行は、金額があっても明細ではない。
        # 一方「合計表示灯」「総額メーター」「温度計」のような正当な品名は
        # 集計語で終わらないので残る。
        if _is_total_row_name(name):
            continue
        # 「値引」単独で金額が無い行だけ落とす（金額のある値引きは明細として残す）
        _nm = re.sub(r'[\s\u3000]', '', name)
        if _nm in ('値引', '値引き') and parts_amt == 0 and wage_amt == 0:
            continue
        if qty < 1:
            qty = 1

        row_idx += 1
        items.append({
            'page':         1,
            'row_type':     'detail',
            'name':         _strip_control_chars(to_halfwidth_katakana(name)),
            'description':  '',
            'work_code':    _strip_control_chars(category),
            'method':       _strip_control_chars(category),
            'part_no':      part_no,
            'quantity':     qty,
            'parts_amount': parts_amt,
            'wage':         wage_amt,
            'line_total':   parts_amt + wage_amt,
            'index_value':  index_val,
            'raw_text':     ','.join(row),
            'row_id':       f'p1_r{row_idx:03d}',
            'row_bbox':     {'x1': 0, 'y1': 0, 'x2': 1000, 'y2': 50},
        })
    if _dropped_amount:
        _trailer_notes.append(
            f'⚠️ 品名が空欄の行を{len(_dropped_amount)}行読み飛ばしました'
            f'（金額の合計 {sum(_dropped_amount):,}円）。'
            'CSVの品名欄をご確認ください。')
    return (items, _trailer_notes) if return_notes else items


def parse_detail_json_to_items(json_text: str, page_num: int = 1) -> list:
    """Geminiが出力したdetail_extraction JSONをitemsリスト（JSON互換）に変換する。
    失敗した場合は空リストを返す（呼び出し元でMarkdownフォールバック）。
    """
    import re, json as _json
    text = json_text.strip()
    # ```json ... ``` ブロックを除去
    text = re.sub(r'^```json\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^```\s*$', '', text, flags=re.MULTILINE)
    # JSON部分を抽出
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return []
    try:
        data = _json.loads(m.group(0))
    except Exception:
        try:
            data = _json.loads(repair_truncated_json(m.group(0)))
        except Exception:
            return []
    details = data.get('details', [])
    # 'details' キーが空なら 'items' キーもフォールバック確認
    if not details:
        details = data.get('items', [])
    if not isinstance(details, list):
        return []
    items = []
    for row_idx, detail in enumerate(details, 1):
        name        = str(detail.get('work_or_part_name', '') or '').strip()
        category    = str(detail.get('category', '') or '').strip()
        index_value = str(detail.get('index_value', '') or '').strip()
        wage_raw    = detail.get('labor_fee', 0)
        qty_raw     = detail.get('quantity', 1)
        parts_raw   = detail.get('part_price', 0)
        part_no     = str(detail.get('part_number', '') or '').strip()
        wage  = safe_int(wage_raw)
        qty   = safe_int(qty_raw, 1)
        parts = safe_int(parts_raw)
        if wage == 0 and parts == 0:
            continue
        if qty < 1:
            qty = 1
        items.append({
            'page':         page_num,
            'row_type':     'detail',
            'name':         name if name else '不明',
            'description':  '',
            'work_code':    category,
            'index_value':  index_value,
            'part_no':      part_no,
            'quantity':     qty,
            'parts_amount': parts,
            'wage':         wage,
            'line_total':   wage + parts,
            'raw_text':     str(detail),
            'row_id':       f'p{page_num}_r{row_idx:03d}',
            'row_bbox':     {'x1': 0, 'y1': 0, 'x2': 1000, 'y2': 50},
        })
    return items


# Markdown表の見出し → 内部キー
_MD_COLUMN_ALIASES = {
    'name':         ('品名', '部品名', '作業内容', '作業内容・使用部品名', '品目', '名称'),
    'work_code':    ('区分', '作業区分'),
    'index_value':  ('指数', '工数'),
    'wage':         ('技術料', '工賃', '作業工賃'),
    'quantity':     ('数量', '個数'),
    'parts_amount': ('部品金額', '部品代', '部品', '部品・油脂', '部品、油脂', '部品油脂'),
    'part_no':      ('部品品番', '部品コード', '部品番号', '品番'),
}


def _build_md_colmap(header_cells) -> dict:
    """Markdown表の見出し行から「内部キー → 列位置」を作る。"""
    norm = []
    for c in header_cells:
        c = re.sub(r'[\s\u3000・、，]', '', str(c or ''))
        c = re.sub(r'[（(\[【][^）)\]】]*[）)\]】]', '', c)
        c = re.sub(r'[（(\[【].*$', '', c)
        norm.append(c)
    colmap = {}
    for key, aliases in _MD_COLUMN_ALIASES.items():
        for alias in aliases:
            for i, c in enumerate(norm):
                if c == alias and i not in colmap.values():
                    colmap[key] = i
                    break
            if key in colmap:
                break
    return colmap if 'name' in colmap else {}


def parse_markdown_to_items(md_text: str, page_num: int = 1) -> list:
    """Geminiが出力したMarkdown表をitemsリスト（JSON互換）に変換する"""
    import re
    items = []
    row_idx = 0

    def to_int(s):
        # 品番のような数値でない文字列は0にする。以前は記号を消して
        # から int にしていたため「52119-47010」が 5,211,947,010 になった。
        raw = str(s).strip()
        if not raw:
            return 0
        if not re.fullmatch(r'[¥￥]?[\d,，\.\s△▲()（）+\-]*円?', raw):
            return 0
        neg = bool(re.search(r'[△▲\-]|^\(.*\)$|^（.*）$', raw))
        s2 = re.sub(r'[,，\s¥￥円△▲+\-()（）]', '', raw)
        if not s2:
            return 0
        try:
            v = int(float(s2))
        except Exception:
            return 0
        return -v if neg else v

    _md_colmap = {}
    for line in md_text.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.split('|')[1:-1]]
        if len(cells) < 3:
            continue
        # ヘッダー行をスキップ
        if cells[0] in ('作業内容・使用部品名', '作業内容', '品名', '部品名'):
            # 見出し行から列位置を覚える（以降の行はこれに従って読む）
            _md_colmap = _build_md_colmap(cells)
            continue
        # 区切り行（--- のみ）をスキップ
        if re.match(r'^[-: ]*$', cells[0]) and cells[0]:
            continue
        # 全セルが空または記号のみの行をスキップ
        if all(re.match(r'^[-:= ]*$', c) for c in cells):
            continue

        # 列は見出しから引き当てる。位置決め打ちだと、画面のプロンプトが
        # 案内する並び（品名,区分,数量,部品金額,工賃,部品コード）で
        # 返ってきたときに全列がずれる。見出しが無ければ従来の位置。
        def _md_cell(key, pos):
            idx = _md_colmap.get(key, None if _md_colmap else pos)
            if idx is None or idx >= len(cells):
                return ''
            return cells[idx]
        name        = _md_cell('name', 0) or '不明'
        method      = _md_cell('work_code', 1)
        index_value = _md_cell('index_value', 2).strip()
        wage        = to_int(_md_cell('wage', 3))
        qty         = to_int(_md_cell('quantity', 4))
        parts       = to_int(_md_cell('parts_amount', 5))
        part_no     = _md_cell('part_no', 6).strip()

        if wage == 0 and parts == 0:
            continue  # 両方0は除外

        row_idx += 1
        items.append({
            'page':         page_num,
            'row_type':     'detail',
            'name':         name,
            'description':  '',
            'work_code':    method,
            'index_value':  index_value,
            'part_no':      part_no,
            'quantity':     qty if qty > 0 else 1,
            'parts_amount': parts,
            'wage':         wage,
            'line_total':   wage + parts,
            'raw_text':     line,
            'row_id':       f'p{page_num}_r{row_idx:03d}',
            'row_bbox':     {'x1': 0, 'y1': 0, 'x2': 1000, 'y2': 50},
        })
    return items


def analyze_estimate_single(api_key, file_bytes, mime_type, model_name, page_num=1, total_pages=1, tax_inclusive=False):
    """見積書明細行をJSON出力プロンプトで読み取り、itemsリストに変換して返す"""
    extra_notes = ''
    if tax_inclusive:
        extra_notes += '\n\n【税込表記】この見積書は税込表記です。記載されている金額はすべて税込金額として読み取り、そのまま転写してください。税抜きへの変換は不要です。'
    if total_pages > 1:
        extra_notes += f'\n\n【ページ指定】これは全{total_pages}ページ中の{page_num}ページ目です。このページの全明細行を漏れなく読み取ってください。'

    prompt = _build_prompt("estimate_detail_page", extra_notes)

    from google.genai import types
    client = _get_genai_client(api_key)
    file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    last_error = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[prompt, file_part],
                config={
                    "temperature": 0.0,
                    "max_output_tokens": 65536,
                },
            )
            if response.text and response.text.strip():
                # まずJSONパーサーで試みる（新形式）
                items = parse_detail_json_to_items(response.text, page_num)
                # JSONパースが空ならMarkdownフォールバック
                if not items:
                    items = parse_markdown_to_items(response.text, page_num)
                return {
                    'items':           items,
                    'discount_amount': 0,
                    'confidence':      0.9,
                }
            if attempt < 2:
                import time; time.sleep(1)
                continue
            raise ValueError("Geminiから有効な応答が得られませんでした。")
        except ValueError:
            raise
        except Exception as e:
            last_error = e
            err_msg = str(e)
            # モデル廃止エラーはリトライせずに即座に再送出
            if _is_model_unavailable_error(err_msg):
                _mark_model_unavailable(api_key, model_name)
                _alt = get_alternative_gemini_model(api_key, model_name)
                raise RuntimeError(
                    f"モデル '{model_name}' は利用できません（提供終了）。"
                    + (f"代替モデル「{_alt}」に自動切り替えします。" if _alt else "サイドバーで別のモデルを選択してください。")
                ) from e
            # クォータ超過エラー
            if '429' in err_msg or 'RESOURCE_EXHAUSTED' in err_msg:
                _quota_exhausted_set().add(model_name)
                cache_key = api_key[-8:] if api_key else ''
                if cache_key in _availability_cache():
                    del _availability_cache()[cache_key]
                raise ValueError(
                    f"モデル '{model_name}' のクォータが上限に達しました。"
                    "自動的に代替モデルに切り替えます。"
                ) from e
            if attempt < 2:
                import time; time.sleep(1)
                continue
            raise ValueError(f"Gemini API呼び出しに失敗しました: {str(last_error)}")


def validate_and_correct_items(items):
    """
    辞書ベースバリデーション: AIの誤分類を後処理で修正する。

    【部品価格計上の必須ルール】
    基本ルール: 部品価格を計上するのは、作業内容（method）が「取替」の場合のみ。
    例外ルール: 作業内容が空白でも、部品価格欄に金額がある場合は「取替」とみなし計上。
    それ以外（脱着・修理・板金等）の場合、parts_amount は強制的に 0 とする。
    これにより「脱着」行の空欄によって次行の金額がズレて取り込まれる連鎖エラーを防ぐ。

    【作業区分ごとの挙動】
    - 取替/交換系:    parts_amount 有効（工賃も同行にある場合はそのまま）
    - 脱着/取外系:    parts_amount = 0 強制（部品代なし）。工賃はそのまま保持。
    - 修理/板金等:    parts_amount = 0 強制。wage==0 のとき parts_amt を wage へ移動
                      （AIが工賃を parts 列に誤分類したケースを救済）
    - 空白method:     parts_amount > 0 ならそのまま（取替とみなす）
    """
    # 部品代計上が有効な作業区分
    PARTS_OK_METHODS = {'取替', '交換', '脱着組替', '取外組付'}

    # 脱着系: parts_amount を 0 に強制。工賃は保持。wage への移動も行わない。
    REMOVAL_METHODS  = {'脱着', '取外', '取付', '組付', '脱外'}

    # 修理・塗装系: parts_amount = 0 強制。wage==0 なら parts_amt を wage へ移動
    REPAIR_METHODS   = {'修理', '調整', '板金', '塗装', 'ペイント', '研磨',
                        '清掃', '点検', '作業', '修正', '施工', '補修'}

    corrected = []
    for item in items:
        item      = dict(item)
        method    = str(item.get('method', '')).strip()
        name      = str(item.get('name', ''))
        parts_amt = safe_int(item.get('parts_amount', 0))
        wage      = safe_int(item.get('wage', 0))

        # ケース1: 取替/交換系 → 部品代・工賃ともに有効（変更なし）
        if any(kw in method for kw in PARTS_OK_METHODS):
            corrected.append(item)
            continue

        # ケース2: 作業内容空白 + 部品価格あり → 「取替」とみなしそのまま計上
        if not method and parts_amt > 0:
            corrected.append(item)
            continue

        # ケース3: 脱着/取外系 → parts_amount を強制ゼロ（wage は触らない）
        is_removal = any(kw in method for kw in REMOVAL_METHODS)
        is_removal_name = any(kw in name for kw in {'脱着', '取外', '取付', '組付'})
        if is_removal or is_removal_name:
            item['parts_amount'] = 0
            corrected.append(item)
            continue

        # ケース4: 修理・塗装等 → parts_amount = 0 強制。wage==0 なら wage へ救済移動
        is_repair = any(kw in method for kw in REPAIR_METHODS)
        is_repair_name = any(kw in name for kw in {'板金', '塗装', 'ペイント', '修理', '研磨'})
        if (is_repair or is_repair_name) and parts_amt > 0:
            if wage == 0:
                # AIが工賃を parts 列に誤分類したとみなして wage へ移動
                item['wage']  = parts_amt
            item['parts_amount'] = 0

        corrected.append(item)
    return corrected


def check_parts_labor_classification(items):
    """
    部品/工賃区分の疑わしい行を検出する。
    AI抽出結果の parts_amount / wage の割り当てが不自然な行をフラグ付きで返す。

    検出パターン:
    1. 「脱着」「取外」系の行に parts_amount > 0 がある（脱着に部品代は不要）
    2. 「材料」「ウレタン」「シーリング」等の材料系名称なのに wage > 0, parts_amount = 0
       （材料費は部品・油脂列に入るべき）
    3. 両方ゼロの行（金額未読み取りの可能性）
    4. 「修理」「板金」「塗装」等の作業系なのに parts_amount > 0, wage = 0
       （作業費は技術料列に入るべき）

    Returns: list of dicts {
        'row_no': int,      # 1始まりの行番号
        'name': str,        # 品名
        'parts_amount': int,
        'wage': int,
        'flag': str,        # 'parts_in_labor' / 'labor_in_parts' / 'both_zero' / 'ambiguous'
        'message': str,     # 日本語の警告メッセージ
        'severity': str,    # 'error' / 'warning'
    }
    """
    # 脱着系キーワード（部品代なし）
    REMOVAL_KW = {'脱着', '取外', '取付', '組付', '脱外'}
    # 材料系キーワード（部品・油脂列に入るべき）
    MATERIAL_KW = {'ウレタン', 'シーリング', 'アンダーコート', '防錆', '塗料', '材料',
                   '充填', '発泡', '発砲', 'パネルボンド', '接着', '油脂', 'オイル'}
    # 作業・修理系キーワード（技術料列に入るべき）
    WORK_KW = {'修理', '板金', 'ペイント', '研磨', '塗装', '清掃', '点検', '作業', '施工', '補修'}

    alerts = []
    for i, item in enumerate(items):
        name      = str(item.get('name', '')).strip()
        method    = str(item.get('method', '')).strip()
        parts_amt = safe_int(item.get('parts_amount', 0))
        wage      = safe_int(item.get('wage', 0))
        row_no    = i + 1

        base = {'row_no': row_no, 'name': name, 'parts_amount': parts_amt, 'wage': wage}

        # パターン1: 脱着系で parts_amount > 0
        if any(kw in name or kw in method for kw in REMOVAL_KW) and parts_amt > 0:
            alerts.append({**base,
                'flag': 'parts_in_labor',
                'message': f'行{row_no}「{name}」: 脱着系作業なのに部品金額 ¥{parts_amt:,} が計上されています。'
                           '技術料欄の値が誤って部品欄に入った可能性があります。',
                'severity': 'error'
            })
            continue

        # パターン2: 材料系名称で wage > 0 かつ parts_amount = 0
        if any(kw in name for kw in MATERIAL_KW) and wage > 0 and parts_amt == 0:
            alerts.append({**base,
                'flag': 'labor_in_parts',
                'message': f'行{row_no}「{name}」: 材料系品名なのに工賃 ¥{wage:,} のみ計上されています。'
                           '部品・油脂列の値が誤って技術料欄に入った可能性があります。',
                'severity': 'error'
            })
            continue

        # パターン3: 作業系名称で parts_amount > 0 かつ wage = 0 （取替除く）
        if any(kw in name or kw in method for kw in WORK_KW):
            if parts_amt > 0 and wage == 0 and '取替' not in method and '交換' not in method:
                alerts.append({**base,
                    'flag': 'parts_in_labor',
                    'message': f'行{row_no}「{name}」: 作業系名称なのに部品金額 ¥{parts_amt:,} のみ計上されています。'
                               '技術料列の値が誤って部品欄に入った可能性があります。',
                    'severity': 'warning'
                })
                continue

        # パターン4: 両方ゼロ（品名があるのに金額なし）
        if parts_amt == 0 and wage == 0 and name:
            zero_ok = {'脱着', '取外', '取付', '組付', '点検', '調整', '清掃'}
            if not any(kw in name or kw in method for kw in zero_ok):
                alerts.append({**base,
                    'flag': 'both_zero',
                    'message': f'行{row_no}「{name}」: 部品金額・工賃ともに0円です。金額の読み取り漏れがないか確認してください。',
                    'severity': 'warning'
                })

    return alerts


def extract_special_items(items, existing_sp=0, existing_exempt=0):
    """
    【廃止済み: 特殊分離は行わない】
    明細欄に記載されている行はすべて items として通過させる。
    ショートパーツ・雑品代・預託金・廃棄処分費用も通常明細として扱う。
    Returns: (items, 0, 0)  ← 常に元のリストをそのまま返す
    """
    return list(items), 0, 0


def global_dedup_items(items):
    """
    全ページ横断の重複行除去。
    ページ境界に限らず、全 items の中で重複を検出して除去する。

    除去ルール:
      0. raw_text が完全一致 → 後出の行を除去（チャンク境界の重複対策）
      1. (normalized_name, parts_amount, wage) が一致 → 後出の行を除去
         ※ normalized_name は to_halfwidth_katakana で正規化（全角/半角の違いを吸収）
      2. name が「不明」または空欄 の行と、同一 (parts_amount, wage) を持つ
         名前付き行が存在する場合 → 「不明」側を除去（OCR誤読を優先排除）
    """
    # Pass0: raw_text ベースの重複除去（チャンク分割のオーバーラップ起因重複を除去）
    seen_raw = set()
    pass0 = []
    for it in items:
        raw   = str(it.get('raw_text', '')).strip()
        parts = safe_int(it.get('parts_amount', 0))
        wage  = safe_int(it.get('wage', 0))
        if parts == 0 and wage == 0:
            pass0.append(it)
            continue
        if raw:
            if raw in seen_raw:
                continue
            seen_raw.add(raw)
        pass0.append(it)

    # Pass1: 正規化name + (parts, wage) による重複除去（全角/半角の表記ゆれを吸収）
    seen_exact = set()
    pass1 = []
    for it in pass0:
        name  = to_halfwidth_katakana(str(it.get('name', ''))).strip()
        parts = safe_int(it.get('parts_amount', 0))
        wage  = safe_int(it.get('wage', 0))
        if parts == 0 and wage == 0:
            pass1.append(it)
            continue
        key = (name, parts, wage)
        if key in seen_exact:
            continue
        seen_exact.add(key)
        pass1.append(it)

    # Pass2: 「不明」行 vs 名前付き行の同一金額重複除去
    named_amount_keys = set()
    for it in pass1:
        name  = str(it.get('name', '')).strip()
        parts = safe_int(it.get('parts_amount', 0))
        wage  = safe_int(it.get('wage', 0))
        if name and name != '不明' and (parts > 0 or wage > 0):
            named_amount_keys.add((parts, wage))

    pass2 = []
    for it in pass1:
        name  = str(it.get('name', '')).strip()
        parts = safe_int(it.get('parts_amount', 0))
        wage  = safe_int(it.get('wage', 0))
        if name in ('不明', '') and (parts, wage) in named_amount_keys:
            continue  # 「不明」行と同額の名前付き行が存在 → 「不明」側を除去
        pass2.append(it)

    return pass2


def validate_row_consistency(items):
    """
    明細行ごとの整合性チェック。
    数量 × 単価 ≠ 部品金額 の不整合を検出し、可能な限り自動修正する。
    Returns: (corrected_items, warnings_list)
    """
    corrected = []
    warnings = []
    for i, item in enumerate(items):
        item = dict(item)
        qty   = safe_int(item.get('quantity', 1), 1)
        parts = safe_int(item.get('parts_amount', 0))
        wage  = safe_int(item.get('wage', 0))
        name  = str(item.get('name', ''))

        if parts > 0 and qty > 1:
            # 単価を逆算して整合性チェック
            unit_price = parts / qty
            # 単価が整数でなければ不整合の可能性
            if abs(unit_price - round(unit_price)) > 0.01:
                # 数量=1として全額を部品金額とみなす方が正しいか判定
                # 例: parts_amount=1960, qty=14 → 140円/個 → 整合（OK）
                # 例: parts_amount=5000, qty=3 → 1666.67円/個 → 不整合
                warnings.append(
                    f"行{i+1}「{name}」: 数量{qty} × 単価{unit_price:.1f} ≠ 部品金額¥{parts:,}（端数あり）"
                )
            # 単価が極端に小さい場合（1円未満）も警告
            elif round(unit_price) < 1:
                warnings.append(
                    f"行{i+1}「{name}」: 単価が極端に小さい（¥{unit_price:.0f}/個）、数量{qty}を確認してください"
                )

        # 部品金額も工賃もゼロの行は警告（0円行が意図的でない可能性）
        if parts == 0 and wage == 0 and name and name != '値引き':
            # ただし method が「脱着」「取外」「組付」など工賃0円が妥当な作業は除外
            method = str(item.get('method', ''))
            zero_ok_methods = {'脱着', '取外', '取付', '組付', '点検', '調整', '清掃'}
            if not any(kw in method for kw in zero_ok_methods) and not any(kw in name for kw in zero_ok_methods):
                warnings.append(
                    f"行{i+1}「{name}」: 部品金額・工賃ともに0円です"
                )

        corrected.append(item)
    return corrected, warnings


def build_estimate_summary(items, short_parts_wage, pdf_parts_total,
                           pdf_wage_total, pdf_grand_total, discount_amount=0,
                           user_tax_basis='tax_exclusive'):
    """
    税区分はUIユーザー選択値のみを使用してNEO書込み用正規化サマリーを返す。
    （AI自動判定は廃止。user_tax_basis='tax_inclusive' または 'tax_exclusive'）
    Returns: {
      'basis': 'tax_inclusive' / 'tax_exclusive',
      'norm_parts': 税抜部品合計,
      'norm_wage':  税抜工賃合計,
      'norm_sp':    税抜ショートパーツ,
      'norm_disc':  税抜値引き,
      'grand':      税込総合計,
      'reverse_match': bool  ← 逆算で総合計が一致するか
    }
    """
    calc_parts = sum(safe_int(it.get('parts_amount', 0)) for it in items)
    calc_wage  = sum(safe_int(it.get('wage', 0))         for it in items)
    sp    = safe_int(short_parts_wage)
    disc  = safe_int(discount_amount)
    grand = safe_int(pdf_grand_total)

    # 税区分はUIユーザー選択値のみを使用（AI判定廃止）
    basis = user_tax_basis if user_tax_basis in ('tax_inclusive', 'tax_exclusive') else 'tax_exclusive'

    if basis == 'tax_inclusive':
        norm_parts = jpy_round(calc_parts / (1 + TAX_RATE))
        norm_wage  = jpy_round(calc_wage  / (1 + TAX_RATE))
        norm_sp    = jpy_round(sp         / (1 + TAX_RATE))
        norm_disc  = jpy_round(disc       / (1 + TAX_RATE))
    else:
        norm_parts = calc_parts
        norm_wage  = calc_wage
        norm_sp    = sp
        norm_disc  = disc

    TOLERANCE = 50  # 円
    reverse_grand = jpy_round((norm_parts + norm_wage + norm_sp - norm_disc) * (1 + TAX_RATE))
    reverse_match = abs(reverse_grand - grand) <= TOLERANCE if grand > 0 else False

    return {
        'basis':         basis,
        'norm_parts':    norm_parts,
        'norm_wage':     norm_wage,
        'norm_sp':       norm_sp,
        'norm_disc':     norm_disc,
        'grand':         grand,
        'reverse_match': reverse_match,
    }


def _self_correction_retry(api_key, file_bytes, mime_type, model_name,
                           original_items, target_parts, target_wage):
    """
    合計値との差額をフィードバックして再抽出し、改善された場合のみ採用する。
    Returns: 改善後の result dict、または None（改善なし）
    """
    calc_parts = sum(safe_int(it.get('parts_amount', 0)) for it in original_items)
    calc_wage  = sum(safe_int(it.get('wage', 0))         for it in original_items)
    # 合計抽出プロンプトは「小計行が無い場合は0を返せ」と指示しており、
    # 0 は「読めなかった」の意味。真値0として扱うと、その科目を
    # 全額削除しろという指示を書いてしまう。
    _has_parts_target = target_parts > 0
    _has_wage_target  = target_wage  > 0
    if not _has_parts_target and not _has_wage_target:
        return None
    parts_diff = (calc_parts - target_parts) if _has_parts_target else 0
    wage_diff  = (calc_wage  - target_wage)  if _has_wage_target  else 0

    if (abs(parts_diff) <= SELF_CORRECTION_THRESHOLD and
            abs(wage_diff) <= SELF_CORRECTION_THRESHOLD):
        return None  # 差額が閾値以下 → 修正不要

    _lines_err, _lines_fix = [], []
    if _has_parts_target and abs(parts_diff) > SELF_CORRECTION_THRESHOLD:
        _lines_err.append(f"- 部品合計: 計算値 ¥{calc_parts:,} ≠ PDF記載 ¥{target_parts:,} "
                          f"（差額 {parts_diff:+,}円）")
        _lines_fix.append(f"- 部品金額を合計{abs(parts_diff):,}円分"
                          f"{'追加' if parts_diff < 0 else '削減'}すること")
    if _has_wage_target and abs(wage_diff) > SELF_CORRECTION_THRESHOLD:
        _lines_err.append(f"- 工賃合計: 計算値 ¥{calc_wage:,} ≠ PDF記載 ¥{target_wage:,} "
                          f"（差額 {wage_diff:+,}円）")
        _lines_fix.append(f"- 工賃を合計{abs(wage_diff):,}円分"
                          f"{'追加' if wage_diff < 0 else '削減'}すること")
    if not _lines_fix:
        return None
    _extra = ("【検出された誤差】\n" + "\n".join(_lines_err) + "\n\n"
              + "【修復指示】\n" + "\n".join(_lines_fix) + "\n")
    correction_prompt = _build_prompt("estimate_validation_repair", _extra)
    try:
        from google.genai import types
        client = _get_genai_client(api_key)
        file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        response = client.models.generate_content(
            model=model_name,
            contents=[correction_prompt, file_part],
            config={"temperature": 0.0, "max_output_tokens": 65536, "response_mime_type": "application/json"},
        )
        if not response.text:
            return None
        try:
            new_result = json.loads(response.text)
        except (json.JSONDecodeError, TypeError):
            new_result = extract_json_from_response(response.text)
        new_items  = new_result.get('items', []) or new_result.get('details', [])
        if not new_items:
            return None
        # 明細抽出と同じパーサでアプリ内部キーへ正規化する。
        # Gemini はプロンプトどおり work_or_part_name / part_price / labor_fee で
        # 返すので、生のまま使うと合計が常に0になり、採用された場合は
        # 品名も金額も空の行だけが .neo に並ぶ。
        new_items = parse_detail_json_to_items(
            json.dumps({'details': new_items}, ensure_ascii=False))
        if not new_items:
            return None
        new_result['items'] = new_items
        new_parts  = sum(safe_int(it.get('parts_amount', 0)) for it in new_items)
        new_wage   = sum(safe_int(it.get('wage', 0))         for it in new_items)
        old_error  = abs(parts_diff) + abs(wage_diff)
        new_error  = abs(new_parts - target_parts) + abs(new_wage - target_wage)
        if new_error >= old_error:
            return None  # 改善なし
        # 明細数が大きく減る修正は、金額だけ合わせて中身を失っている可能性が高い。
        # 完全一致するのでない限り採用しない。
        old_count = len(original_items or [])
        if old_count and len(new_items) < old_count * 0.7 and new_error != 0:
            print(f"[WARN] 自己修復が明細を {old_count}行 → {len(new_items)}行 に"
                  f"減らしたため不採用（残差 {new_error:,}円）")
            return None
        return new_result  # 改善された → 採用
    except Exception as e:
        import sys
        print(f"[WARN] _self_correction_retry 例外: {e}", file=sys.stderr)
        return None


def extract_honda_cars_subtotals(file_bytes):
    """
    Honda Cars形式PDFから pypdf テキスト解析で部品/工賃合計を確実に抽出する。
    Geminiが誤読するケースを防ぐため、pypdfのテキスト抽出結果を使用する。

    pypdf特有のパターン:
      - 「小      計 195,398 482,976」行 → 最終的な部品/工賃合計
      - 「ページ小計 140,547 175,246」行 → ページ別小計（全ページを加算）

    戻り値: (parts_total, wages_total) または None（Honda Cars形式でない場合）
    """
    import re
    try:
        from pypdf import PdfReader
        import io as _io
        reader = PdfReader(_io.BytesIO(file_bytes))
        # reader.pages は遅延評価で、パスワード付きPDFではここで
        # FileNotDecryptedError を投げる。try の外に出すと、成功済みの
        # 合計OCRごと解析全体が中断してしまう。
        _pages = list(reader.pages)
    except Exception:
        return None

    all_text = ''
    for page in _pages:
        try:
            t = page.extract_text() or ''
            all_text += t + '\n'
        except Exception:
            pass

    if not all_text.strip():
        return None

    # Honda Cars形式を識別: 「部品価格(税込)」列ヘッダが存在する
    if '部品価格(税込)' not in all_text and '部品価格（税込）' not in all_text:
        return None

    # パターン1: 「小      計 195,398 482,976」（最終合計行・スペース多め）
    m = re.search(r'小\s{2,}計\s+([\d,]+)\s+([\d,]+)', all_text)
    if m:
        parts = int(m.group(1).replace(',', ''))
        wages = int(m.group(2).replace(',', ''))
        if parts > 0 or wages > 0:
            return (parts, wages)

    # パターン2: 「小計 NNN NNN」（スペース少ない場合）
    m = re.search(r'小\s*計\s+([\d,]+)\s+([\d,]+)', all_text)
    if m:
        parts = int(m.group(1).replace(',', ''))
        wages = int(m.group(2).replace(',', ''))
        if parts > 0 or wages > 0:
            return (parts, wages)

    # パターン3: ページ小計を全ページ合算
    page_totals = re.findall(r'ページ小計\s+([\d,]+)\s+([\d,]+)', all_text)
    if page_totals:
        total_parts = sum(int(p.replace(',', '')) for p, w in page_totals)
        total_wages = sum(int(w.replace(',', '')) for p, w in page_totals)
        if total_parts > 0 or total_wages > 0:
            return (total_parts, total_wages)

    return None


def analyze_estimate(api_key, file_bytes, mime_type, model_name=None,
                     use_fax_filter=False, use_rasterize=False, use_enhance=True,
                     enable_self_correction=True, progress_cb=None,
                     tax_inclusive=False):
    """
    見積書をAI-OCRで解析するメイン関数。
    progress_cb: (pct: int, text: str) -> None  進捗コールバック（Noneなら使用しない）
    新機能:
      use_fax_filter: FAXページを自動除外する（追加APIコール1回）
      use_rasterize: PDF→JPEG変換してから送信（行ズレ防止）
    """
    import hashlib, sys
    used_model = model_name or GEMINI_MODEL
    _log: list = []  # 解析ログ収集リスト

    def _logw(msg: str):
        """ログをリストと stderr 両方に出力する"""
        _log.append(msg)
        print(f"[ANALYZE] {msg}", file=sys.stderr)

    # ── ファイルハッシュキャッシュ: 同一ファイルの再解析を防ぐ ──────────────
    _cache_key = (hashlib.md5(file_bytes).hexdigest()
                  + f"_{used_model}_{use_rasterize}_{use_fax_filter}_{use_enhance}_{enable_self_correction}")
    def _cb(pct, text):
        """進捗コールバック呼び出し（Noneなら何もしない）"""
        if progress_cb:
            try:
                progress_cb(pct, text)
            except Exception:
                pass

    if _cache_key in _analyze_result_cache:
        print("[INFO] キャッシュヒット: 再解析をスキップします", file=sys.stderr)
        # 参照のまま返すと、呼び出し側の書き換えがキャッシュに残る。
        # このキャッシュはプロセス全体で共有（セッション跨ぎ・利用者跨ぎ）
        # なので、1回目の生成が書いた値を2回目が入力として読み、
        # 同じPDFから内訳の違う .neo が出ていた。
        # items の dict も共有されており、照合結果の書き戻しでも同じ事故が起きる。
        return copy.deepcopy(_analyze_result_cache[_cache_key])
    # ──────────────────────────────────────────────────────────────────────────

    # クォータ超過・提供終了のモデルを避けて使用モデルを決定する。
    # 以前は静的な _PREFERRED_MODELS の先頭から選んでいたため、
    # そこに実在しないモデルが並んでいると 404 になり、しかも
    # 提供終了と分かっているモデルを除外していなかったため、
    # 同じ死んだモデルを毎回選び直して復旧しなかった。
    # APIが実際に返したモデルから選ぶ。
    if used_model in _quota_exhausted_set() or used_model in _unavailable_set():
        _alt = get_alternative_gemini_model(api_key, used_model)
        if _alt and _alt != used_model:
            print(f"[INFO] モデル '{used_model}' は利用できないため "
                  f"'{_alt}' に切り替えます", file=sys.stderr)
            used_model = _alt

    _logw(f"🤖 使用モデル: {used_model}")
    _logw(f"📂 ファイルサイズ: {len(file_bytes):,} bytes / MIMEタイプ: {mime_type}")
    _logw(f"⚙️ オプション: FAXフィルター={'ON' if use_fax_filter else 'OFF'} / ラスタライズ={'ON' if use_rasterize else 'OFF'} / 自己修復={'ON' if enable_self_correction else 'OFF'}")

    # ① FAXページフィルタリング（オプション）
    _cb(8, "① FAXページを確認中...")
    filtered_count = 0
    if use_fax_filter and mime_type == 'application/pdf':
        original_size = len(file_bytes)
        file_bytes    = filter_fax_pages(api_key, file_bytes, used_model)
        if len(file_bytes) < original_size:
            filtered_count = 1
    _logw(f"① FAXフィルター: {'1ページ除外' if filtered_count > 0 else '除外なし'}")

    # ② 横向きPDF補正
    _cb(12, "② ページ補正・分割中...")
    if mime_type == 'application/pdf':
        file_bytes = try_fix_landscape_pdf(file_bytes)

    # ③ ページ分割
    pages = try_split_pdf_pages(file_bytes) if mime_type == 'application/pdf' else None
    # ③-a ページ順序自動補正（FAXヘッダ等で逆順になっている場合を修正）
    if pages and len(pages) > 1:
        _reordered = detect_and_reorder_pages(pages)
        if _reordered != pages:
            # 並べ替えた順で1つのPDFに組み直す。組み直さないと、
            # 以降の処理（Geminiへの送信・合計欄のラスタライズ）は
            # 物理順の file_bytes を見るため、補正が効かない。
            try:
                from pypdf import PdfReader as _PR, PdfWriter as _PW
                _w = _PW()
                for _pb in _reordered:
                    _w.add_page(_PR(io.BytesIO(_pb)).pages[0])
                _buf = io.BytesIO()
                _w.write(_buf)
                file_bytes = _buf.getvalue()
            except Exception:
                pass   # 組み直しに失敗したら元のまま（順序は直らないがデータは壊さない）
        pages = _reordered
    _logw(f"③ ページ分割: {len(pages) if pages else 1}ページ")

    # ③-b&c ラスタライズ: PDF→JPEG変換（行ズレ防止）
    # 最終ページ（合計欄）と1ページ目（車両情報）を並列でラスタライズ
    raster_bytes       = file_bytes
    raster_mime        = mime_type
    first_raster_bytes = file_bytes
    first_raster_mime  = mime_type
    if use_rasterize and mime_type == 'application/pdf':
        try:
            from pypdf import PdfReader
            num_pages = len(PdfReader(io.BytesIO(file_bytes)).pages)
        except Exception:
            num_pages = 1
        last_page_idx = max(0, num_pages - 1)
        # 最終ページと1ページ目を並列ラスタライズ（直列から並列化 → ~2s節約）
        def _raster_last(_):
            return rasterize_pdf_page(file_bytes, last_page_idx, dpi=300, enhance=use_enhance)
        def _raster_first(_):
            return rasterize_pdf_page(file_bytes, 0, dpi=300, enhance=use_enhance)
        with ThreadPoolExecutor(max_workers=2) as _ex:
            _fut_last  = _ex.submit(_raster_last, None)
            _fut_first = _ex.submit(_raster_first, None)
            img  = _fut_last.result()
            img1 = _fut_first.result()
        if img:
            raster_bytes = img
            raster_mime  = 'image/jpeg'
        if img1:
            first_raster_bytes = img1
            first_raster_mime  = 'image/jpeg'

    # ④ 1パス目: 合計値＋車両情報抽出
    _cb(20, "③ 合計金額・車両情報を読み取り中...（10〜20秒）")
    # 合計欄は最終ページ、車両情報は1ページ目にあることが多い
    # 複数ページの場合は最終ページと1ページ目を並列でAPI呼び出し（直列から並列化 → ~10s節約）
    _need_first_page = bool(pages and len(pages) > 1 and first_raster_bytes != raster_bytes)
    if _need_first_page:
        with ThreadPoolExecutor(max_workers=2) as _ex:
            _fut_totals = _ex.submit(
                analyze_estimate_totals, api_key, raster_bytes, raster_mime, used_model)
            _fut_first  = _ex.submit(
                analyze_estimate_totals, api_key, first_raster_bytes, first_raster_mime, used_model)
            totals_data     = _fut_totals.result() or {}
            first_page_data = _fut_first.result() or {}
    else:
        totals_data = analyze_estimate_totals(api_key, raster_bytes, raster_mime, used_model) or {}
    if _need_first_page:
        # 車両情報は1ページ目の結果を優先
        vinfo_first = first_page_data.get('vehicle_info', {})
        # 応答がトップレベルに car_name 等を返してきた場合も拾う。
        # プロンプトの出力形式と消費側のキーがずれていた名残で、
        # 拾わないと車両情報が丸ごと捨てられる。
        _flat_vi = {k: totals_data.get(k) for k in
                    ('car_name', 'car_model', 'engine_model', 'color_code',
                     'color_name', 'trim_code', 'grade', 'model_year',
                     'chassis_no', 'mileage')
                    if totals_data.get(k) and str(totals_data.get(k)).strip()
                    and str(totals_data.get(k)).strip() != '不明'}
        vinfo_last  = dict(_flat_vi)
        vinfo_last.update(totals_data.get('vehicle_info', {}) or {})
        merged_vinfo = {k: (vinfo_first.get(k) or vinfo_last.get(k, '')) for k in
                        set(list(vinfo_first.keys()) + list(vinfo_last.keys()))}
        totals_data['vehicle_info'] = merged_vinfo
        # 税区分判定: 最終ページ不明の場合、1ページ目の判定を優先採用
        # 例: 「内消費税」「(税込)」表記は1ページ目にある場合が多い
        last_basis  = totals_data.get('amount_basis', 'unknown')
        first_basis = first_page_data.get('amount_basis', 'unknown')
        if last_basis not in ('tax_inclusive', 'tax_exclusive') and first_basis in ('tax_inclusive', 'tax_exclusive'):
            totals_data['amount_basis'] = first_basis
            totals_data['tax_reason']   = first_page_data.get('tax_reason', totals_data.get('tax_reason', ''))
        # 合計値の補完: 最終ページに合計がない場合（pdf_grand_total=0）、1ページ目の値を使用
        # 例: Honda Cars系フォーマット（合計がページ1ヘッダのサマリーボックスに記載）
        last_grand = safe_int(totals_data.get('pdf_grand_total', 0))
        if last_grand == 0:
            first_grand = safe_int(first_page_data.get('pdf_grand_total', 0))
            if first_grand > 0:
                totals_data['pdf_grand_total'] = first_grand
                # 部品合計・工賃合計・値引きも1ページ目から補完（最終ページに0の場合のみ）
                if safe_int(totals_data.get('pdf_parts_total', 0)) == 0:
                    totals_data['pdf_parts_total'] = first_page_data.get('pdf_parts_total', 0)
                if safe_int(totals_data.get('pdf_wage_total', 0)) == 0:
                    totals_data['pdf_wage_total'] = first_page_data.get('pdf_wage_total', 0)
                if safe_int(totals_data.get('discount_amount', 0)) == 0:
                    totals_data['discount_amount'] = first_page_data.get('discount_amount', 0)
    target_parts = safe_int(totals_data.get('pdf_parts_total', 0))
    target_wage  = safe_int(totals_data.get('pdf_wage_total', 0))
    pdf_grand    = safe_int(totals_data.get('pdf_grand_total', 0))
    discount     = safe_int(totals_data.get('discount_amount', 0))
    _logw(f"④ 合計抽出: 部品計={target_parts:,} / 工賃計={target_wage:,} / 総合計={pdf_grand:,} / 値引={discount:,}")

    # ④-a Honda Cars形式: pypdfで正確な合計値を取得（Gemini誤読を防ぐ）
    # Geminiは「小計 195,398 482,976」の数値を誤認することがある。
    # pypdf解析は列レイアウトに依存しないため確実。
    if mime_type == 'application/pdf':
        # 補助的な抽出。ここでの失敗が明細抽出を巻き込まないようにする。
        try:
            _pypdf_totals = extract_honda_cars_subtotals(file_bytes)
        except Exception:
            _pypdf_totals = None
        if _pypdf_totals:
            _pypdf_parts, _pypdf_wages = _pypdf_totals
            import sys
            print(f"[INFO] Honda Cars pypdf合計: 部品={_pypdf_parts:,}, 工賃={_pypdf_wages:,} "
                  f"(Gemini推測: 部品={target_parts:,}, 工賃={target_wage:,})", file=sys.stderr)
            target_parts = _pypdf_parts
            target_wage  = _pypdf_wages
            totals_data['pdf_parts_total'] = _pypdf_parts
            totals_data['pdf_wage_total']  = _pypdf_wages

    # ⑤ 2パス目: 明細抽出（PDF全ページを一括送信 — ページ境界ズレを防ぐ）
    _cb(45, f"④ 明細行を解析中...（{len(pages) if pages else 1}ページ / 30秒〜2分かかる場合があります）")
    _page_count = len(pages) if pages else 1
    _logw(f"⑤ 全ページ一括解析開始 ({_page_count}ページ)")
    # 全ページを1リクエストで送っているので、ページ指定の文言は付けない。
    # 「これは全Nページ中の1ページ目です」と指示すると、2ページ目以降の
    # 明細を読ませない方向にモデルを誘導してしまう。
    result = analyze_estimate_single(
        api_key, file_bytes, 'application/pdf', used_model, 1, 1,
        # 税込表記であることをモデルに伝える。伝えないと、税込金額を
        # 勝手に税抜へ割り戻される誤読を防ぐ指示が届かない。
        tax_inclusive=bool(tax_inclusive)
    ) or {}
    result.setdefault('items', [])
    result.setdefault('short_parts_wage', 0)
    result['pdf_parts_total']   = target_parts or safe_int(result.get('pdf_parts_total', 0))
    result['pdf_wage_total']    = target_wage  or safe_int(result.get('pdf_wage_total', 0))
    result['pdf_grand_total']   = pdf_grand    or safe_int(result.get('pdf_grand_total', 0))
    result['discount_amount']   = discount     or safe_int(result.get('discount_amount', 0))
    # 明細抽出は 0.9 を固定で返すため、合計抽出が返した実測値を優先する。
    # 固定値のままだと低信頼度の警告が構造上一度も出ない。
    _hdr_conf = safe_float(totals_data.get('confidence', 0), 0.0)
    result['confidence']        = (_hdr_conf if _hdr_conf > 0
                                   else safe_float(result.get('confidence', 0.5)))
    result['_fax_filtered']     = filtered_count
    result['_page_count']       = _page_count
    result['_vehicle_info']     = totals_data.get('vehicle_info', {})
    result['_repair_shop_name'] = totals_data.get('repair_shop_name', '')
    _p = sum(safe_int(it.get('parts_amount', 0)) for it in result['items'])
    _w = sum(safe_int(it.get('wage', 0)) for it in result['items'])
    _logw(f"  → {len(result['items'])}行 / 部品={_p:,} / 工賃={_w:,}")

    _cb(80, "⑤ データを整理中...")
    # ⑥ 全ページ横断重複除去（AI出力のページ先読み・同一行二重出力を除去）
    # ページ境界に限らず全行を対象にした重複除去。
    # ・Page1のAIがPage2の明細を合計額に合わせて先読み出力するケースを防止
    # ・同一ページ内で先頭数行を2回出力するAIの誤動作を防止
    # ※ この重複除去は「PDFを分割して複数回AIに投げる」時代のチャンク重複対策。
    #    現在は1回のリクエストでPDF全体を解析するため重複の発生源が無く、
    #    同じ部品が2行並ぶ正当な明細（左右のクリップ等）を消して金額を
    #    欠落させるだけになっていた。分割解析した場合のみ適用する。
    # ※ 現在このフラグを立てる経路は無く、重複除去は事実上オフ。
    #    正当な重複明細（左右のクリップ等）を消して金額を欠落させる実害の方が
    #    大きいため、意図的にオフのままにしている。分割解析を再導入する場合は
    #    このフラグを立てる前に、隣接判定と品番までキーに含める修正が必要。
    if result.get('_chunked'):
        _before_dedup = len(result['items'])
        result['items'] = global_dedup_items(result['items'])
        _after_dedup = len(result['items'])
        _logw(f"⑥ 全体重複除去: {_before_dedup}行 → {_after_dedup}行 ({_before_dedup - _after_dedup}件除去)")
    else:
        _logw("⑥ 全体重複除去: 分割解析ではないためスキップ（正当な重複明細を保持）")

    # ⑥-b 辞書ベースバリデーション
    result['items'] = validate_and_correct_items(result['items'])

    # ⑥-c 品名空白フォールバック（AIが名称を読み取れなかった行を保護）
    # work_code が非空なら品名の代替として使用し、それもなければ「不明」を設定
    for _it in result['items']:
        if not str(_it.get('name', '')).strip():
            _wc = str(_it.get('work_code', '')).strip()
            _it['name'] = _wc if _wc else '不明'

    # ⑥-d 明細行ごとの整合性チェック
    result['items'], row_warnings = validate_row_consistency(result['items'])
    if row_warnings:
        result['_row_warnings'] = row_warnings

    # ⑥-c ショートパーツ・預託金の明細行からの分離（二重計上防止）
    existing_sp = safe_int(result.get('short_parts_wage', 0))
    existing_exempt = safe_int(result.get('tax_exempt_amount', 0))
    result['items'], sp_total, exempt_total = extract_special_items(
        result['items'], existing_sp, existing_exempt
    )
    result['short_parts_wage'] = sp_total
    result['tax_exempt_amount'] = exempt_total

    # ⑥-d スキャンPDF等でpypdf取得失敗時のクロスバリデーション
    # Geminiが誤った小計値を返した場合（スキャンPDF等）、明細合算＋grand_totalで検証して上書き
    _cv_items_nd = [it for it in result['items'] if it.get('name') != '値引き']
    _cv_calc_p   = sum(safe_int(it.get('parts_amount', 0)) for it in _cv_items_nd)
    _cv_calc_w   = sum(safe_int(it.get('wage', 0))         for it in _cv_items_nd)
    _cv_sp       = safe_int(result.get('short_parts_wage', 0))
    _cv_grand    = safe_int(result.get('pdf_grand_total', 0))
    _cv_p_stated = safe_int(result.get('pdf_parts_total', 0))
    _cv_w_stated = safe_int(result.get('pdf_wage_total', 0))
    if _cv_p_stated > 0 and _cv_w_stated > 0 and _cv_grand > 0 and _cv_calc_p > 0:
        _cv_item_total = _cv_calc_p + _cv_calc_w + _cv_sp
        # 明細合算＋sp が grand_total に近い（2%以内）か確認
        _cv_match_grand = abs(_cv_item_total - _cv_grand) / _cv_grand < 0.02
        # Geminiのstated totalsが明細合算と大きくずれているか（10%超）
        _cv_p_wrong = abs(_cv_calc_p - _cv_p_stated) / max(_cv_calc_p, 1) > 0.10
        _cv_w_wrong = abs(_cv_calc_w + _cv_sp - _cv_w_stated) / max(_cv_calc_w + _cv_sp, 1) > 0.10
        # 印字された部品計＋工賃計が総合計と辻褄が合っている（税抜・税込の
        # どちらの解釈でも可）なら、小計のほうが正しく、足りないのは明細。
        # そこで小計を明細合算で上書きすると、行が落ちている唯一の証拠を
        # 消してしまい、1行少ない見積が無警告で出る。上書きしない。
        # 印字された部品計・工賃計は値引き前の小計、総合計は値引き後。
        # 値引きを引いてから突き合わせないと、値引きのある見積では
        # 必ず辻褄が合わないと判定され、上書きを止められない。
        _cv_disc = safe_int(result.get('discount_amount', 0))
        _cv_pw_stated = _cv_p_stated + _cv_w_stated - _cv_disc
        _cv_eps = max(int(_cv_grand * 0.01), 100)
        _cv_pw_ok = (abs(_cv_pw_stated - _cv_grand) <= _cv_eps
                     or abs(int(round(_cv_pw_stated * 1.10)) - _cv_grand) <= _cv_eps)
        # 明細合算が印字小計より「少ない」側は、行が落ちている可能性がある。
        # そこを上書きすると、落ちている唯一の証拠を消して無警告で通してしまう。
        # 上書きしてよいのは、明細のほうが多い／列の振り分けが違うだけの場合。
        _cv_floor = max(int(_cv_grand * 0.001), 100)
        _cv_p_short = _cv_calc_p < _cv_p_stated - _cv_floor
        _cv_w_short = (_cv_calc_w + _cv_sp) < _cv_w_stated - _cv_floor
        if (_cv_match_grand and (_cv_p_wrong or _cv_w_wrong)
                and not _cv_pw_ok and not (_cv_p_short or _cv_w_short)):
            import sys as _sys_cv
            print(f"[INFO] cross-validation: Gemini stated totals誤り検出 → 明細合算値で上書き", file=_sys_cv.stderr)
            print(f"  Gemini: 部品={_cv_p_stated:,}, 工賃={_cv_w_stated:,}", file=_sys_cv.stderr)
            print(f"  明細合算: 部品={_cv_calc_p:,}, 工賃={_cv_calc_w+_cv_sp:,} (sp={_cv_sp:,}), 合計={_cv_item_total:,}≈{_cv_grand:,}", file=_sys_cv.stderr)
            result['pdf_parts_total'] = _cv_calc_p
            result['pdf_wage_total']  = _cv_calc_w + _cv_sp

    # ⑦ 自己修復ループ（合計値が取得できた場合のみ、最大2回）
    # enable_self_correction=False（ベタ打ちモード等）の場合はスキップして高速化
    if enable_self_correction and (result['pdf_parts_total'] > 0 or result['pdf_wage_total'] > 0):
        _correction_rounds = 0
        for _sc_round in range(2):
            _cur_items = result['items']
            _cur_parts = sum(safe_int(it.get('parts_amount', 0)) for it in _cur_items)
            _cur_wage  = sum(safe_int(it.get('wage', 0))         for it in _cur_items)
            _sc_sp     = safe_int(result.get('short_parts_wage', 0))
            _p_diff = abs(_cur_parts - result['pdf_parts_total'])
            # 工賃比較はショートパーツを加味（Honda Cars等でspが工賃列に含まれるため）
            _w_diff = abs((_cur_wage + _sc_sp) - result['pdf_wage_total'])
            if _p_diff == 0 and _w_diff == 0:
                break  # 完全一致 → 修正不要
            # 修復には明細解析と同じ入力（PDF全体）を渡す。
            # ラスタ画像は最終ページ1枚だけなので、全体の再抽出を頼むと
            # 最終ページの内容で全明細が置き換わってしまう。
            retry = _self_correction_retry(
                api_key, file_bytes, mime_type, used_model,
                result['items'],
                result['pdf_parts_total'],
                result['pdf_wage_total'],
            )
            if retry and retry.get('items'):
                # retry['items'] は _self_correction_retry 内で正規化済み
                result['items']            = validate_and_correct_items(retry['items'])
                result['short_parts_wage'] = safe_int(retry.get('short_parts_wage', result.get('short_parts_wage', 0)))
                _correction_rounds += 1
            else:
                break  # 改善なし → 終了
        if _correction_rounds > 0:
            result['_self_corrected'] = True
            result['_correction_rounds'] = _correction_rounds

    # ⑧ 税区分はUIユーザー選択値のみを使用（AI自動判定廃止）
    # 注: analyze_estimate 呼び出し後にセッションstate(tax_override)で上書きされる（line 4580付近）
    # ここでは仮に tax_exclusive を設定しておき、後段のUI処理で正式に上書きされる
    summary = build_estimate_summary(
        result['items'],
        result.get('short_parts_wage', 0),
        result.get('pdf_parts_total', 0),
        result.get('pdf_wage_total', 0),
        result.get('pdf_grand_total', 0),
        result.get('discount_amount', 0),
        user_tax_basis='tax_exclusive',  # 後段UIで上書きされる仮値
    )
    result['_tax_basis']     = summary['basis']
    result['_reverse_match'] = summary['reverse_match']
    # _is_tax_inclusive は後段のUIオーバーライドで設定されるため、ここでは設定しない

    # ⑩ 値引きを負の工賃行として items に追加
    disc_outtax = summary.get('norm_disc', 0)
    # 明細側にも値引き行が入っていると、同じ値引きが2回引かれる。
    _has_disc_row = any(
        re.search(r'(値引|割引)', str(it.get('name', '') or ''))
        or safe_int(it.get('wage', 0)) < 0
        or safe_int(it.get('parts_amount', 0)) < 0
        for it in result['items'])
    if disc_outtax > 0 and not _has_disc_row:
        result['items'].append({
            'name':         '値引き',
            'method':       '値引き',
            'quantity':     1,
            'parts_amount': 0,
            'wage':         -disc_outtax,
        })

    # ⑪ STEP 3 最終バリデーション結果の生成（APIが返したものを優先、なければ再計算）
    final_items = [it for it in result['items'] if it.get('name') != '値引き']
    calc_p = sum(safe_int(it.get('parts_amount', 0)) for it in final_items)
    calc_w = sum(safe_int(it.get('wage', 0))         for it in final_items)
    doc_p  = safe_int(result.get('pdf_parts_total', 0))
    doc_w  = safe_int(result.get('pdf_wage_total', 0))
    p_diff = calc_p - doc_p
    w_diff = calc_w - doc_w
    # doc=0 は「PDF未記載」扱い → 不一致カウントしない
    p_mismatch = (doc_p > 0) and (p_diff != 0)
    w_mismatch = (doc_w > 0) and (w_diff != 0)
    is_match = (not p_mismatch and not w_mismatch)
    # 合計欄の解析自体に失敗した（部品計・工賃計とも取得できなかった）場合、
    # 「差が無い＝一致」と報告してはいけない。検証できていないだけで、
    # 通信エラーとの区別がつかなくなる。
    _totals_unavailable = (doc_p <= 0 and doc_w <= 0)
    if _totals_unavailable:
        result['_totals_unavailable'] = True
        is_match = None
    # Geminiが返したtotals_verificationがある場合はそちらを優先
    if not result.get('totals_verification'):
        _err_parts = []
        if p_mismatch: _err_parts.append(f'部品差額{p_diff:+,}円')
        if w_mismatch: _err_parts.append(f'工賃差額{w_diff:+,}円')
        if _totals_unavailable:
            _err_parts.append('見積書の合計欄を読み取れませんでした（検証未実施）')
        result['totals_verification'] = {
            'calculated_parts_total': calc_p,
            'calculated_labor_total': calc_w,
            'document_parts_total':   doc_p,
            'document_labor_total':   doc_w,
            'parts_diff':             p_diff if doc_p > 0 else 0,
            'labor_diff':             w_diff if doc_w > 0 else 0,
            'is_match':               is_match,
            'validation_error':       '・'.join(_err_parts) if _err_parts else None,
        }
    else:
        # Gemini返却値がある場合も、PDF未記載(=0)の工賃・部品は不一致扱いしない
        _tv = result['totals_verification']
        _tv_doc_p = safe_int(_tv.get('document_parts_total', 0))
        _tv_doc_w = safe_int(_tv.get('document_labor_total', 0))
        _tv_p_diff = safe_int(_tv.get('parts_diff', 0))
        _tv_w_diff = safe_int(_tv.get('labor_diff', 0))
        if _tv_doc_w == 0 and _tv_w_diff != 0:
            _tv['labor_diff'] = 0
        if _tv_doc_p == 0 and _tv_p_diff != 0:
            _tv['parts_diff'] = 0
        _tv_p_mis = (_tv_doc_p > 0) and (_tv['parts_diff'] != 0)
        _tv_w_mis = (_tv_doc_w > 0) and (_tv['labor_diff'] != 0)
        _tv['is_match'] = not _tv_p_mis and not _tv_w_mis
        _err_parts = []
        if _tv_p_mis: _err_parts.append(f'部品差額{_tv["parts_diff"]:+,}円')
        if _tv_w_mis: _err_parts.append(f'工賃差額{_tv["labor_diff"]:+,}円')
        _tv['validation_error'] = '・'.join(_err_parts) if _err_parts else None
        result['totals_verification'] = _tv

    # 最終集計ログ
    _final_items = result.get('items', [])
    _final_p = sum(safe_int(it.get('parts_amount', 0)) for it in _final_items)
    _final_w = sum(safe_int(it.get('wage', 0)) for it in _final_items)
    _final_total = _final_p + _final_w
    _final_grand = safe_int(result.get('pdf_grand_total', 0))
    _diff = _final_total - _final_grand
    _match_str = "✅ 完全一致" if abs(_diff) <= 1 else f"⚠️ 差額 {_diff:+,}円"
    _logw(f"─────────────────────────────")
    _logw(f"📊 最終結果: {len(_final_items)}行 / 部品={_final_p:,} / 工賃={_final_w:,} / 計={_final_total:,}")
    _logw(f"  PDF総合計={_final_grand:,} → {_match_str}")
    result['_analysis_log'] = _log

    # ── 解析ログをファイルに書き出し ──────────────────────────────────────────
    try:
        _ts = now_jst().strftime('%Y-%m-%d %H:%M:%S')
        # 追記のみで際限なく育つため、一定サイズを超えたら作り直す
        try:
            if os.path.exists(ANALYSIS_LOG_PATH) and \
                    os.path.getsize(ANALYSIS_LOG_PATH) > 5 * 1024 * 1024:
                os.replace(ANALYSIS_LOG_PATH, ANALYSIS_LOG_PATH + '.1')
        except OSError:
            pass
        with open(ANALYSIS_LOG_PATH, 'a', encoding='utf-8') as _lf:
            _lf.write(f"\n{'='*60}\n")
            _lf.write(f"[{_ts}] 解析開始\n")
            for _entry in _log:
                _lf.write(f"  {_entry}\n")
            _lf.write(f"[{_ts}] 解析終了\n")
    except Exception as _le:
        print(f"[WARN] analysis.log 書き込み失敗: {_le}", file=sys.stderr)
    # ──────────────────────────────────────────────────────────────────────────

    # ── キャッシュ保存 ──────────────────────────────────────────────────────────
    # 保存側もコピーする。参照のまま入れると、この呼び出しの後段（生成側）が
    # result を書き換えたときにキャッシュに残り、2回目の入力になる。
    # 取り出し側だけ守っても、1回目の書き換えは防げない。
    _analyze_result_cache[_cache_key] = copy.deepcopy(result)
    # キャッシュが大きくなりすぎないよう古いエントリを削除（最大20件）
    while len(_analyze_result_cache) > 20:
        oldest_key = next(iter(_analyze_result_cache))
        del _analyze_result_cache[oldest_key]
    # ──────────────────────────────────────────────────────────────────────────

    return result


# ============================================================
# ファイル名生成
# ============================================================

def generate_filename(cust, calc_parts, calc_wages, pdf_parts, pdf_wages,
                      has_estimate, reverse_match=False, short_parts_wage=0):
    """
    登録番号から出力ファイル名を生成。
    reverse_match=True の場合は部品・工賃相違を抑制する。
    ショートパーツがPDF側の部品合計に含まれているケースも考慮して比較する。
    """
    dept   = safe_str(cust.get('car_reg_department', ''))
    div    = safe_str(cust.get('car_reg_division', ''))
    biz    = safe_str(cust.get('car_reg_business', ''))
    serial = safe_str(cust.get('car_reg_serial', ''))
    base   = f'{dept}{div}{biz}{serial}'
    if not base.strip():
        base = '新規見積'
    sp = safe_int(short_parts_wage)
    discrepancies = []
    if not reverse_match and has_estimate:
        # ショートパーツがPDF部品合計に含まれている場合も一致とみなす
        parts_match = (calc_parts == pdf_parts) or (calc_parts + sp == pdf_parts)
        if pdf_parts is not None and pdf_parts > 0 and not parts_match:
            discrepancies.append('部品相違')
        if pdf_wages is not None and pdf_wages > 0 and calc_wages != pdf_wages:
            discrepancies.append('工賃相違')
    if discrepancies:
        suffix = '（' + '・'.join(discrepancies) + '）'
    else:
        suffix = ''
    return f'{base}_見積{suffix}.neo'


# ============================================================
# Streamlit UI
# ============================================================

# ============================================================
# PDF見積 → NEO 自動変換（pdf_to_neo_pipeline のラッパ）
# ============================================================
def esc_html(value) -> str:
    """HTMLに埋め込む前のエスケープ。

    車検証OCRの結果や品名など、アップロードされた文書に由来する文字列を
    unsafe_allow_html のHTMLへ直接埋め込むと、画面の崩しやリンクの差し込みが
    できてしまう。表示直前にこれを通す。
    """
    import html as _html
    return _html.escape(str(value if value is not None else ''), quote=True)


def _make_estimate_token(items, vehicle_bytes=None, estimate_bytes=None) -> str:
    """見積の識別子。入力が同じなら同じ値になる。

    ステップ①に戻って同じ見積を再開したときは編集内容を復元し、
    別の見積を読み込んだときは復元しないための判定に使う。
    """
    import hashlib as _hashlib
    parts = [
        str(len(vehicle_bytes or b'')),
        str(len(estimate_bytes or b'')),
    ]
    for it in (items or [])[:20]:
        if isinstance(it, dict):
            parts.append('{}|{}|{}'.format(
                it.get('name', ''),
                safe_int(it.get('parts_amount', 0)),
                safe_int(it.get('wage', 0)),
            ))
    return _hashlib.md5('/'.join(parts).encode('utf-8', 'ignore')).hexdigest()


def _session_cache_scope() -> str:
    """このセッション固有のキャッシュ識別子。

    pdf_to_neo_pipeline のキャッシュはプロセス全体で共有されるため、
    識別子を渡さないと、同じ見積PDFを扱った別の利用者に前の利用者の
    解析結果や生成済みNEOが返ってしまう。
    """
    try:
        scope = st.session_state.get('_pipeline_cache_scope')
        if not scope:
            scope = _uuid.uuid4().hex
            st.session_state['_pipeline_cache_scope'] = scope
        return scope
    except Exception:
        return _uuid.uuid4().hex


def run_pdf_to_neo_pipeline(pdf_bytes, api_key, model_name=None, template_bytes=None,
                           is_tax_inclusive=False, expenses=None):
    """見積書PDFから直接NEOファイルを生成する。

    pdf_to_neo_pipeline.process_pdf_to_neo をStreamlitから安全に呼ぶための薄いラッパ。
    - APIキーはサイドバー入力を環境変数に一時的に渡す（パイプラインが環境変数を読むため）
    - Addataが無い環境ではモードA（ベタ打ち）を強制する。
      マーカー付きのモードB/Cは車種DBが存在する場合のみ意味を持ち、
      DBが無いまま実行すると全部品に「※ADDATA該当なし」が付いてしまうため。
    戻り値: process_pdf_to_neo の結果dict。失敗時は {'ok': False, 'error': '...'}
    """
    tmp_pdf = None
    tmp_tpl = None
    try:
        try:
            import pdf_to_neo_pipeline as _pipe
        except Exception as e:
            return {'ok': False, 'error': f'PDF→NEO変換モジュールを読み込めません: {e}'}

        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as _f:
            _f.write(pdf_bytes)
            tmp_pdf = _f.name

        if template_bytes:
            with tempfile.NamedTemporaryFile(suffix='.neo', delete=False) as _f:
                _f.write(template_bytes)
                tmp_tpl = _f.name
            template_path = tmp_tpl
        else:
            template_path = TEMPLATE_PATH

        addata_root = find_addata_dir()
        mode_override = None if addata_root else 'A'

        # APIキーは引数で直接渡す。os.environ に書くと、プロセスを共有する
        # 他の利用者のセッションからも読めてしまう（キーの流用・課金事故）。
        result = _pipe.process_pdf_to_neo(
            tmp_pdf,
            addata_root=addata_root or '',
            template_path=template_path,
            mode_override=mode_override,
            model_name=model_name or None,
            api_key=api_key or None,
            cache_scope=_session_cache_scope(),
            # 見積書の明細が税込表記かどうか。画面で利用者が指定する。
            # 決め打ちにすると、税込表記の見積で総額が消費税ぶん膨らむ。
            is_tax_inclusive=bool(is_tax_inclusive),
            # この経路には車両情報の入力欄が無く、利用者が上書きする手段が
            # ない。マージモードにすると前案件の登録番号・使用者名・事故日が
            # そのまま残り、別の車の見積になってしまうので使わない。
            # DBとヘッダXMLの食い違いは、非マージモードで Car/Insurance/
            # FileInfo も Customer と同じく全上書きにすることで解消している。
            merge_mode=False,
            # サイドバーの費用欄。渡さないと、この経路で作った .neo に
            # レッカー代・代車費用・非課税費用が1円も入らない。
            expenses=expenses or None,
        )
        if not isinstance(result, dict):
            return {'ok': False, 'error': 'PDF→NEO変換が想定外の値を返しました'}
        return result
    except Exception as e:
        return {'ok': False, 'error': f'PDF→NEO変換に失敗しました: {e}'}
    finally:
        for _path in (tmp_pdf, tmp_tpl):
            if _path:
                try:
                    os.unlink(_path)
                except OSError:
                    pass


def main():
    st.set_page_config(
        page_title="NEO自動生成アプリ",
        page_icon="🚗",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    st.markdown("""
    <style>
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: 'Segoe UI', 'Hiragino Sans', 'Meiryo', sans-serif; }

    /* 上部の余白を完全に詰める */
    .block-container { padding-top: 0px !important; margin-top: 0px !important; }
    header[data-testid="stHeader"] { display: none !important; height: 0 !important; }
    #root > div:first-child { padding-top: 0 !important; }
    .stApp > header { display: none !important; }
    .stApp { margin-top: 0 !important; }
    section.main > div { padding-top: 0 !important; }

    /* file_uploader の「Drag and drop」「Limit」テキストを非表示（複数セレクタで対応） */
    [data-testid="stFileUploaderDropzoneInstructions"] { display: none !important; }
    [data-testid="stFileUploaderDropzone"] small,
    [data-testid="stFileUploaderDropzone"] span:not(.st-emotion-cache-9ycgxx),
    .uploadedFileName ~ small,
    section[data-testid="stFileUploaderDropzone"] div > small { display: none !important; }
    [data-testid="stFileUploaderDropzone"] { min-height: 56px !important; padding: 8px 12px !important; }
    /* アイコンとBrowseボタンだけ残す */
    [data-testid="stFileUploaderDropzone"] > div > div:first-child > span { display: none !important; }
    [data-testid="stFileUploaderDropzone"] > div > div:first-child > small { display: none !important; }

    /* Topbar */
    .topbar { background: #1a2744; color: #fff; padding: 0 24px; height: 52px;
              display: flex; align-items: center; justify-content: space-between;
              box-shadow: 0 2px 8px rgba(0,0,0,.25); border-radius: 8px; margin-bottom: 20px; }
    .topbar-title { font-size: 16px; font-weight: 700; letter-spacing: .04em;
                    display: flex; align-items: center; gap: 10px; }
    .topbar-badge { background: #3b82f6; font-size: 10px; padding: 2px 7px;
                    border-radius: 10px; font-weight: 600; }
    .topbar-right { display: flex; align-items: center; gap: 16px; font-size: 12px; color: #94a3b8; }
    .api-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 4px; vertical-align: middle; }

    /* Step bar */
    .step-bar { display: flex; align-items: center; background: #fff; border-radius: 10px;
                padding: 16px 24px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.07); }
    .step-item { display: flex; align-items: center; gap: 10px; }
    .step-circle { width: 32px; height: 32px; border-radius: 50%; display: inline-flex;
                   align-items: center; justify-content: center; font-weight: 700; font-size: 13px; flex-shrink: 0; }
    .step-circle-done { background: #22c55e; color: #fff; }
    .step-circle-active { background: #1d4ed8; color: #fff; box-shadow: 0 0 0 4px #bfdbfe; }
    .step-circle-pending { background: #e2e8f0; color: #94a3b8; }
    .step-label-active { font-size: 12px; font-weight: 600; color: #1d4ed8; }
    .step-label-done { font-size: 12px; font-weight: 600; color: #15803d; }
    .step-label-pending { font-size: 12px; font-weight: 600; color: #94a3b8; }
    .step-connector { flex: 1; height: 2px; background: #e2e8f0; margin: 0 8px; min-width: 20px; }
    .step-connector-done { background: #22c55e; }

    /* Mode selector */
    .mode-selector { display: flex; gap: 8px; margin-bottom: 16px; }
    .mode-btn { flex: 1; padding: 12px 16px; border: 2px solid #e2e8f0; border-radius: 8px;
                background: #fff; text-align: center; }
    .mode-btn-active { border-color: #1d4ed8; background: #eff6ff; }
    .mode-icon { font-size: 22px; display: block; margin-bottom: 4px; }
    .mode-label { font-size: 13px; font-weight: 700; color: #1e293b; }
    .mode-label-active { color: #1d4ed8; }
    .mode-desc { font-size: 11px; color: #94a3b8; margin-top: 2px; }

    /* Vehicle strip */
    .vehicle-strip { background: linear-gradient(135deg, #1a2744 0%, #1e3a5f 100%); color: #fff;
                     border-radius: 10px; padding: 16px 20px; margin-bottom: 16px;
                     display: flex; align-items: flex-start; gap: 16px; }
    .vehicle-strip-name { font-size: 18px; font-weight: 700; }
    .vehicle-strip-detail { font-size: 12px; color: #94a3b8; margin-top: 2px; }
    .vehicle-strip-badges { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }

    /* Total strip */
    .total-strip { background: #1a2744; color: #fff; border-radius: 10px; padding: 16px 24px;
                   display: flex; align-items: center; gap: 20px; margin-top: 16px; flex-wrap: wrap; }
    .total-item { text-align: center; }
    .total-label { font-size: 10px; color: #94a3b8; font-weight: 600; letter-spacing: .05em; }
    .total-value { font-size: 18px; font-weight: 700; }
    .total-value-highlight { font-size: 22px; font-weight: 700; color: #fbbf24; }
    .total-sep { color: #334155; font-size: 18px; }

    /* Badges */
    .badge-green  { background: #dcfce7; color: #15803d; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .badge-blue   { background: #dbeafe; color: #1d4ed8; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .badge-orange { background: #ffedd5; color: #c2410c; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .badge-red    { background: #fee2e2; color: #b91c1c; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .badge-gray   { background: #f1f5f9; color: #475569; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .badge-purple { background: #f3e8ff; color: #7e22ce; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }

    /* Alert boxes */
    .alert { border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; font-size: 13px; }
    .alert-info    { background: #eff6ff; border: 1px solid #bfdbfe; color: #1d4ed8; }
    .alert-warn    { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; }
    .alert-success { background: #f0fdf4; border: 1px solid #bbf7d0; color: #15803d; }
    .alert-error   { background: #fef2f2; border: 1px solid #fca5a5; color: #991b1b; }

    /* Mismatch banner */
    .mismatch-banner { background: #fef2f2; border: 1px solid #fca5a5; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
    .mismatch-title  { font-weight: 700; color: #991b1b; font-size: 14px; margin-bottom: 4px; }
    .mismatch-body   { font-size: 12px; color: #7f1d1d; line-height: 1.6; margin-bottom: 8px; }

    /* DB status */
    .db-status-item { font-size: 11px; color: #475569; line-height: 1.8; }
    .db-dot-green { color: #22c55e; }
    .db-dot-yellow { color: #f59e0b; }

    /* Section title */
    .section-title { font-size: 13px; font-weight: 700; color: #334155; margin-bottom: 12px;
                     padding-bottom: 8px; border-bottom: 1px solid #f1f5f9; display: flex; align-items: center; gap: 8px; }

    /* Legacy classes (keep for backward compat) */
    .main-title   { font-size: 1.8rem; font-weight: bold; color: #1a5276; margin-bottom: 0.5rem; }
    .step-header  { font-size: 1.3rem; font-weight: bold; color: #2c3e50; padding: 0.5rem 0; border-bottom: 2px solid #3498db; margin-bottom: 1rem; }
    .success-box  { background: #d4edda; border: 1px solid #c3e6cb; border-radius: 8px; padding: 1rem; margin: 0.5rem 0; }
    .warning-box  { background: #fff3cd; border: 1px solid #ffeaa7; border-radius: 8px; padding: 1rem; margin: 0.5rem 0; }
    .error-box    { background: #f8d7da; border: 1px solid #f5c6cb; border-radius: 8px; padding: 1rem; margin: 0.5rem 0; }
    .info-box     { background: #d1ecf1; border: 1px solid #bee5eb; border-radius: 8px; padding: 1rem; margin: 0.5rem 0; }
    .tax-box      { background: #e8f5e9; border: 1px solid #a5d6a7; border-radius: 8px; padding: 0.8rem; margin: 0.5rem 0; font-size: 0.9rem; }

    /* File uploader */
    [data-testid="stFileUploader"] {
        border: 2px dashed #cbd5e1 !important;
        border-radius: 10px !important;
        padding: 12px !important;
        background-color: #f8fafc !important;
    }
    [data-testid="stFileUploader"]:hover {
        border-color: #3b82f6 !important;
        background-color: #eff6ff !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # テンプレートチェック（キャッシュ付き — 毎リランで再読込しない）
    @st.cache_data(show_spinner=False)
    def _load_template(path: str) -> bytes:
        with open(path, 'rb') as f:
            return f.read()

    if not os.path.exists(TEMPLATE_PATH):
        st.error(
            f"⚠️ テンプレートファイルが見つかりません: {TEMPLATE_FILENAME}\n\n"
            f"app.py と同じフォルダに「{TEMPLATE_FILENAME}」を配置してください。"
        )
        st.stop()
    template_data = _load_template(TEMPLATE_PATH)

    # ─── サイドバー ───────────────────────────────────
    with st.sidebar:
        # ── メニュー ──
        st.markdown('<div style="font-size:10px;font-weight:700;color:#94a3b8;letter-spacing:.08em;text-transform:uppercase;padding:8px 0 4px">メニュー</div>', unsafe_allow_html=True)
        st.markdown('🏠 **ホーム**')
        st.markdown('<div style="background:#eff6ff;color:#1d4ed8;padding:6px 10px;border-radius:6px;font-size:13px;font-weight:600;margin-bottom:2px">📝 新規作成</div>', unsafe_allow_html=True)
        st.markdown('📂 作成履歴（準備中）')
        st.markdown("---")

        # ── 設定 ──
        st.markdown('<div style="font-size:10px;font-weight:700;color:#94a3b8;letter-spacing:.08em;text-transform:uppercase;padding:4px 0">設定</div>', unsafe_allow_html=True)
        st.header("🔑 APIキー設定")
        if GEMINI_API_KEY:
            api_key = GEMINI_API_KEY
            st.success("APIキー: 設定済み (.env)")
        else:
            api_key = st.text_input(
                "Gemini APIキー",
                type="password",
                help=".envファイルの GEMINI_API_KEY にキーを設定すれば毎回入力不要"
            )
        # 利用可能なモデルをAPIで動的取得（APIキーがある場合のみ）
        if api_key:
            _ck = _model_cache_key(api_key)
            if _ck in _availability_cache():
                _avail_models = _availability_cache()[_ck]
            else:
                with st.spinner("利用可能なモデルを確認中..."):
                    _avail_models = get_available_gemini_models(api_key)
        else:
            _avail_models = [_FALLBACK_MODEL]
        # 自動切り替え済みのモデルがあればそれを初期選択にする
        _pref_model = st.session_state.get('selected_model')
        _model_index = _avail_models.index(_pref_model) if _pref_model in _avail_models else 0
        selected_model = st.selectbox(
            "🤖 AIモデル",
            options=_avail_models,
            index=_model_index,
            key="model_selector_v2",
            help="Gemini APIで実際に利用可能なモデルを自動検出（提供終了モデルは除外）。Flash=高速・コスパ良好、Pro=高精度"
        )
        st.markdown("---")
        st.markdown("**🗂 Addata（車種データベース）**")
        addata_status = find_addata_dir()
        if addata_status:
            _ka06 = find_ka06_path(addata_status)
            st.success("Addata検出済み")
            st.caption(addata_status)
            st.caption(("車種マスタ KA06_ALL.DB あり" if _ka06
                        else "※ COM/KA06_ALL.DB が無いため車種の自動特定はできません"))
            if st.button("🗑️ Addataを解除", key='addata_clear_btn'):
                _discard_uploaded_addata()
                st.rerun()
        else:
            st.warning("Addataフォルダ未検出（ベタ打ちモードで生成します）")

        with st.expander("Addataを読み込む", expanded=not addata_status):
            st.caption(
                "Addata があると、部品名・品番・価格をコグニセブンのマスタと"
                "突き合わせて部品コードや損害コードを引き当てます。"
                "無い場合はベタ打ち（モードA）で生成します。"
            )
            st.caption(
                "このアプリはクラウド上で動いているため、お使いのPCの "
                "C:\\Addata を直接読むことはできません。ZIPにして"
                "アップロードしてください。"
            )
            st.caption(
                "ZIPの中身は「A〜Zの1文字フォルダ ／ 車種コード ／ *.DB」の構造。"
                "車種の自動特定には COM/KA06_ALL.DB も必要です。"
                "全体が大きい場合は、対象車種のフォルダと COM だけでも動きます。"
            )
            _addata_zip = st.file_uploader(
                "Addata の ZIP",
                type=['zip'],
                key='addata_zip_upload',
                help="アップロードできるZIPは200MBまでです。"
                     "セッション内でのみ保持し、他の利用者からは見えません。",
            )
            # 同じファイルで再実行するたびに展開し直さないよう、
            # 何を展開済みかを名前とサイズで覚えておく。別のZIPが
            # 選ばれたら、前の展開先を消してから入れ替える。
            _zip_id = (f'{_addata_zip.name}:{_addata_zip.size}'
                       if _addata_zip is not None else None)
            if _zip_id and st.session_state.get('_addata_zip_id') != _zip_id:
                _discard_uploaded_addata()
                with st.spinner("Addataを展開しています…"):
                    _sweep_stale_addata_dirs()
                    _dest = tempfile.mkdtemp(prefix='addata_')
                    try:
                        _root, _why = extract_addata_zip(_addata_zip.getvalue(), _dest)
                    except Exception as _e:
                        _root, _why = None, f'展開に失敗しました: {_e}'
                if _root:
                    st.session_state[_ADDATA_UPLOAD_BASE_KEY] = _dest
                    st.session_state[_ADDATA_UPLOAD_KEY] = _root
                    st.session_state['_addata_upload_label'] = _why
                    st.session_state['_addata_zip_id'] = _zip_id
                    st.rerun()
                else:
                    import shutil as _sh
                    _sh.rmtree(_dest, ignore_errors=True)
                    st.session_state['_addata_zip_id'] = _zip_id
                    st.error(f"❌ {_why}")
        st.markdown("---")
        st.header("🔬 精度オプション")
        use_fax_filter = st.checkbox(
            "FAXページ自動除外",
            value=True,
            help="FAX送付状が混在するPDFの1ページ目を自動検出・除外します。APIコールが1回増えます。"
        )
        use_rasterize = st.checkbox(
            "PDF→画像変換（行ズレ防止）",
            value=False,
            help="PDFをJPEG画像に変換してからAIに送ります。通常はOFFのままで精度が高くなります。"
        )
        use_enhance = st.checkbox(
            "画像前処理（FAX品質改善）",
            value=True,
            help="コントラスト・シャープネスを強化してFAX品質の画像を読みやすくします。ラスタライズ有効時のみ機能します。"
        )
        st.markdown("---")
        st.header("🛡️ 事故・保険情報")
        st.caption("コグニセブンの受付／保険欄に書き込まれます。空欄はテンプレートの値を維持します。")
        # ウィジェットキーに連番を付ける。Streamlit ではキーを del しても
        # ブラウザが直前の値を送り直すため入力が復活し、前のお客様の事故情報が
        # 次の見積に混入する。連番を進めれば別のウィジェットになり確実に空になる。
        _fseq = st.session_state.setdefault('form_seq', 0)
        accept_no       = st.text_input("事故受付番号", value=st.session_state.get('accept_no', ''),
                                        key=f'accept_no_input_{_fseq}', placeholder="例: 2026-001234",
                                        max_chars=37, help="全角なら18文字までNEOに入ります")
        accident_date   = st.text_input("事故日（YYYYMMDD）", value=st.session_state.get('accident_date', ''),
                                        key=f'accident_date_input_{_fseq}', placeholder="例: 20260901")
        policy_no       = st.text_input("証券番号", value=st.session_state.get('policy_no', ''),
                                        key=f'policy_no_input_{_fseq}', max_chars=20, help="全角なら10文字までNEOに入ります")
        contractor_name = st.text_input("契約者名", value=st.session_state.get('contractor_name', ''),
                                        key=f'contractor_name_input_{_fseq}', max_chars=20, help="全角なら10文字までNEOに入ります")
        agency_name     = st.text_input("保険会社・代理店名", value=st.session_state.get('agency_name', ''),
                                        key=f'agency_name_input_{_fseq}', max_chars=20, help="全角なら10文字までNEOに入ります")
        adjuster_name   = st.text_input("アジャスター名", value=st.session_state.get('adjuster_name', ''),
                                        key=f'adjuster_name_input_{_fseq}', max_chars=20, help="全角なら10文字までNEOに入ります")
        with st.expander("入庫・出庫・修理日数", expanded=False):
            garage_in_date  = st.text_input("入庫日（YYYYMMDD）", value=st.session_state.get('garage_in_date', ''),
                                            key=f'garage_in_input_{_fseq}')
            garage_out_date = st.text_input("出庫日（YYYYMMDD）", value=st.session_state.get('garage_out_date', ''),
                                            key=f'garage_out_input_{_fseq}')
            repair_days     = st.number_input("修理日数", value=st.session_state.get('repair_days', 0),
                                              min_value=0, step=1, key=f'repair_days_input_{_fseq}')
            note1           = st.text_area("備考", value=st.session_state.get('note1', ''),
                                           key=f'note1_input_{_fseq}', height=70, max_chars=40, help="全角なら20文字までNEOに入ります")
        # 日付は YYYYMMDD / YYYY-MM-DD / YYYY/MM/DD を受け付ける。
        # 解釈できない入力は書き込まれないので、その場で知らせる。
        for _dlabel, _dval in (('事故日', accident_date), ('入庫日', garage_in_date),
                               ('出庫日', garage_out_date)):
            if _dval and not _normalize_date8(_dval):
                st.warning(f"⚠️ {_dlabel}「{_dval}」は日付として読み取れません。"
                           "YYYYMMDD で入力してください（このままではNEOに書き込まれません）。")
        for _k, _v in [
            ('accept_no', accept_no), ('accident_date', accident_date),
            ('policy_no', policy_no), ('contractor_name', contractor_name),
            ('agency_name', agency_name), ('adjuster_name', adjuster_name),
            ('garage_in_date', garage_in_date), ('garage_out_date', garage_out_date),
            ('repair_days', repair_days), ('note1', note1),
        ]:
            st.session_state[_k] = _v
        st.markdown("---")
        st.header("💰 費用（Expense）")
        exp_towing    = st.number_input("レッカー費用（税抜）",  value=st.session_state.get('exp_towing', 0),    min_value=0, step=1000, key=f'exp_towing_input_{_fseq}')
        exp_rental    = st.number_input("代車費用（税抜）",      value=st.session_state.get('exp_rental', 0),    min_value=0, step=1000, key=f'exp_rental_input_{_fseq}')
        exp_exempt    = st.number_input("非課税費用",            value=st.session_state.get('exp_exempt', 0),    min_value=0, step=1000, key=f'exp_exempt_input_{_fseq}')
        st.session_state['exp_towing'] = exp_towing
        st.session_state['exp_rental'] = exp_rental
        st.session_state['exp_exempt'] = exp_exempt
        st.markdown("---")
        st.markdown('<div style="font-size:10px;font-weight:700;color:#94a3b8;letter-spacing:.08em;text-transform:uppercase;padding:4px 0">DB状態</div>', unsafe_allow_html=True)
        _addata_dir_check = find_addata_dir()
        _ka06_exists = False
        _parts_count_approx = "—"
        if _addata_dir_check:
            _ka06_path = find_ka06_path(_addata_dir_check)
            _ka06_exists = _ka06_path is not None and os.path.exists(_ka06_path)
            if _ka06_exists:
                try:
                    _ka06_size = os.path.getsize(_ka06_path)
                    _vehicle_count = _ka06_size // 32  # 概算
                    _parts_count_approx = f"〜{_vehicle_count:,}件"
                except Exception:
                    pass
        dot_g = '<span style="color:#22c55e">●</span>'
        dot_y = '<span style="color:#f59e0b">●</span>'
        ka06_dot = dot_g if _ka06_exists else dot_y
        addata_dot = dot_g if _addata_dir_check else dot_y
        st.markdown(f"""
        <div style="font-size:11px;color:#475569;line-height:2">
            {ka06_dot} vehicle_index (KA06)<br>
            {addata_dot} Addataフォルダ<br>
            {dot_y} grade_codes: —<br>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("---")
        st.caption(f"消費税率: {int(TAX_RATE * 100)}%（固定）")
        st.caption(f"見積日: {now_jst().strftime('%Y/%m/%d')}（自動）")

    # セッション状態初期化
    for key, default in [
        ('step', 1), ('vehicle_data', None), ('estimate_data', None),
        ('neo_bytes', None), ('neo_filename', None)
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    current_step = st.session_state['step']

    # ── Topbar ──
    api_dot_color = "#22c55e" if api_key else "#ef4444"
    api_status_text = "接続中" if api_key else "未設定"
    st.markdown(f"""
    <div class="topbar">
        <div class="topbar-title">
            🚗 SHOUCHIKU8 — NEO自動生成
            <span class="topbar-badge">v4.0</span>
        </div>
        <div class="topbar-right">
            <span><span class="api-dot" style="background:{api_dot_color}"></span>Gemini API {api_status_text}</span>
            <span>|</span>
            <span>モデル: {selected_model}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Step progress bar ──
    step_labels = ["① アップロード", "② AI解析", "③ プレビュー・修正", "④ NEO生成"]
    step_html = '<div class="step-bar">'
    for i, label in enumerate(step_labels):
        sn = i + 1
        if sn < current_step:
            c_cls = "step-circle step-circle-done"
            c_txt = "✓"
            l_cls = "step-label-done"
        elif sn == current_step:
            c_cls = "step-circle step-circle-active"
            c_txt = str(sn)
            l_cls = "step-label-active"
        else:
            c_cls = "step-circle step-circle-pending"
            c_txt = str(sn)
            l_cls = "step-label-pending"
        step_html += f'<div class="step-item"><div class="{c_cls}">{c_txt}</div><span class="{l_cls}" style="font-size:12px;font-weight:600;margin-left:8px">{label}</span></div>'
        if i < len(step_labels) - 1:
            conn_cls = "step-connector step-connector-done" if sn < current_step else "step-connector"
            step_html += f'<div class="{conn_cls}"></div>'
    step_html += '</div>'
    st.markdown(step_html, unsafe_allow_html=True)

    # =========================================
    # STEP 1: アップロード
    # =========================================
    if current_step == 1:
        # ベタ打ちモード固定
        st.session_state['selected_mode'] = 'beta'

        # ================================================================
        # STEP 1-A: ファイルアップロード（車検証 + テンプレートNEO）
        # ================================================================
        st.markdown('<div class="section-title">📁 ファイルアップロード</div>', unsafe_allow_html=True)
        _up_col1, _up_col2 = st.columns(2)
        with _up_col1:
            vehicle_file = st.file_uploader(
                "📋 車検証（任意）PDF・JPG・PNG 対応",
                type=['pdf', 'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff', 'tif', 'heic', 'heif'],
                key='vehicle_upload',
            )
            if vehicle_file:
                st.success(f"✅ {vehicle_file.name}")
        with _up_col2:
            custom_neo_file = st.file_uploader(
                "📁 テンプレートNEOファイル（任意）",
                type=['neo'],
                key='custom_neo_upload',
                help="コグニセブンで作成した.neoファイル。証券番号・工場名・車両情報等が入力済みのものを使用してください。"
            )
            if custom_neo_file:
                _neo_bytes_read = custom_neo_file.read()
                custom_neo_file.seek(0)
                # 解析にかける前にサイズで弾く。巨大なファイルは
                # 解析そのものがメモリを食い、共有プロセスを落としうる。
                if len(_neo_bytes_read) > MAX_NEO_UPLOAD_BYTES:
                    st.session_state.pop('custom_neo_bytes', None)
                    st.session_state.pop('custom_neo_name', None)
                    st.error(
                        f"❌ {custom_neo_file.name} はサイズが大きすぎます"
                        f"（{MAX_NEO_UPLOAD_BYTES // (1024 * 1024)}MBまで）。"
                        "コグニセブンのNEOファイルではない可能性があります。")
                    _neo_bytes_read = b''
                # 中身がNEOかどうかをこの場で確かめる。最後の生成時まで
                # 気づけないと、入力をやり直す手間が大きい。
                _tpl_ok = False
                try:
                    _tpl_ck = find_real_cks(_neo_bytes_read)
                    if _tpl_ck:
                        # CKの並びがあるだけでは不十分（"CK"を含むPDF等が通る）。
                        # 実際に展開して明細DBが入っているところまで確かめる。
                        _tpl_raw = decompress_neo(_neo_bytes_read, _tpl_ck)
                        _tpl_mgmt, _tpl_entries = parse_entries(_neo_bytes_read, _tpl_ck[0])
                        _tpl_files = extract_files(_tpl_raw, _tpl_entries)
                        # 内包ファイルは12個で固定。管理領域の長さが想定と違う
                        # NEOだと、ファイルテーブルの途中から読み始めてしまい
                        # 先頭の数ファイルを黙って落としたまま「生成成功」に
                        # なる。壊れたNEOを出荷しないよう件数でも弾く。
                        _tpl_ok = ('AnSMB.txt' in _tpl_files
                                   and len(_tpl_entries) == 12)
                except Exception:
                    _tpl_ok = False
                if not _tpl_ok:
                    st.session_state.pop('custom_neo_bytes', None)
                    st.session_state.pop('custom_neo_name', None)
                    st.error(
                        f"❌ {custom_neo_file.name} はコグニセブンのNEOファイルとして読み取れません。"
                        "別のファイルを選択してください（デフォルトテンプレートで続行できます）。"
                    )
                else:
                    st.session_state['custom_neo_bytes'] = _neo_bytes_read
                    st.session_state['custom_neo_name']  = custom_neo_file.name
                    st.success(f"✅ {custom_neo_file.name} ({len(_neo_bytes_read):,} bytes)")
                st.caption("📋 テンプレートの工場名・証券番号等はそのまま引き継ぎます。画面で入力しなかった項目（使用者名・車台番号・事故受付番号など）もテンプレートの値が残るため、別の案件として出す項目は入力し直してください")
            elif st.session_state.get('custom_neo_bytes'):
                _saved_name = st.session_state.get('custom_neo_name', 'テンプレートNEO')
                _saved_size = len(st.session_state['custom_neo_bytes'])
                st.success(f"✅ {_saved_name} ({_saved_size:,} bytes) — 前回アップロード済み")
                if st.button("🗑️ リセット", key='clear_custom_neo'):
                    st.session_state.pop('custom_neo_bytes', None)
                    st.session_state.pop('custom_neo_name', None)
                    st.rerun()
            else:
                st.caption(f"未選択 → デフォルトテンプレート（{TEMPLATE_FILENAME}）を使用")

        # ================================================================
        # STEP 1-B: Gemini解析 → CSV取り込み（メインフロー）
        # ================================================================
        st.markdown("---")
        st.markdown(
            '<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:10px;'
            'padding:14px 18px;font-size:14px;margin-bottom:12px;">'
            '🤖 <b>見積書の解析手順</b><br>'
            '<span style="font-size:12px;color:#555;">'
            '① 下の「プロンプトをコピー」をクリック → '
            '② 「Geminiを開く」でGoogle Geminiへ → '
            '③ プロンプトを貼り付け＋見積書PDFを添付して送信 → '
            '④ 結果のCSVをコピーして下欄に貼り付け'
            '</span>'
            '</div>',
            unsafe_allow_html=True
        )

        # ── プロンプト定義 ──
        _CSV_PROMPT = """添付の自動車修理見積書PDFを、以下のCSV形式で全明細行を転写してください。
【出力形式（ヘッダー行必須）】 品名,区分,数量,部品金額,工賃,部品コード
【各列の抽出・加工ルール】
* 品名：元の記載から「取替」「脱着」「修理」「鈑金」「塗装」などの作業を示す文言（後述の区分ルールに該当する語）を削除した、純粋な部品名・対象名。
* 区分：元の記載に含まれる以下のキーワードを1語のみ抽出（該当なしは空欄）。 ・取替：「取替」「交換」「取換」（※部品金額のみで工賃0の行も「取替」とする） ・脱着：「脱着」「取外」「取付」「組付」 ・鈑金：「鈑金」「板金」 ・塗装：「塗装」「ペイント」「ワックス」「加算」「ブース」 ・修理：「修理」「補修」「調整」「点検」「設定」「分解」「修正」
* 数量：半角整数（空欄や不明な場合は 1 を補完）
* 部品金額：「部品、油脂」列の金額。半角整数・カンマなし（記載なしは 0）
* 工賃：「技術料」列の金額。半角整数・カンマなし（記載なしは 0）
* 部品コード：品番・部品番号（記載なしは空欄）
【データ処理の重要ルール（高速化・精度向上）】
1. 行の分割：1つの項目に対し「部品、油脂」「技術料」両方に金額がある場合、必ず2行に分割する。 ・1行目：部品金額のみ記載（工賃は0） ・2行目：工賃のみ記載（部品金額は0）
2. 列の厳密照合：金額が部品列か技術料列か、PDFの表ヘッダーを厳密に確認する（例：「ショートパーツ」等、技術料列のみの数値を部品列に入れない）。
3. 対象外：合計行、小計行、消費税行は出力しない。全ページ・全明細行を漏れなく処理する。
【合計額の自動検算と出力】 明細抽出後、内部で以下の検算を実施すること。
1. 抽出した全明細の「部品金額」の合計と「工賃」の合計を算出。
2. 見積書原本の最終的な「部品代合計」「技術料（工賃）合計」と照合。
3. 不一致の場合のみ、CSVの末尾に改行して以下を出力（一致時は出力しない）。※金額には必ずカンマ（,）を含めること。 部品相違〇,〇〇〇円 工賃相違●,●●●円
出力はCSVデータおよび相違確認結果のみ。説明文・コメントは一切不要。"""

        _escaped = _CSV_PROMPT.replace('`', '\\`').replace('\\', '\\\\').replace('\n', '\\n')

        # ── ボタン行: プロンプトコピー ＋ Geminiを開く ──
        _btn_col1, _btn_col2 = st.columns(2)
        with _btn_col1:
            st.components.v1.html(f"""
<button onclick="navigator.clipboard.writeText(`{_escaped}`).then(()=>{{
    this.textContent='✅ コピーしました！';
    this.style.background='#16a34a';
    setTimeout(()=>{{this.textContent='📋 プロンプトをコピー';this.style.background='#2563eb';}},2000);
}})" style="
    background:#2563eb;color:white;border:none;border-radius:8px;
    padding:12px 14px;font-size:14px;cursor:pointer;font-weight:700;width:100%;height:52px;
">📋 プロンプトをコピー</button>
""", height=56)
        with _btn_col2:
            st.components.v1.html("""
<a href="https://gemini.google.com/" target="_blank" rel="noopener noreferrer" style="
    display:flex;align-items:center;justify-content:center;gap:8px;
    background:#ea4335;color:white;border:none;border-radius:8px;
    padding:12px 14px;font-size:14px;cursor:pointer;font-weight:700;width:100%;height:52px;
    text-decoration:none;
">🌐 Geminiを開く（別タブ）</a>
""", height=56)

        # ── CSV取り込みエリア ──
        st.markdown("")
        st.markdown('<div class="section-title">📊 CSV取り込み</div>', unsafe_allow_html=True)

        # 税区分選択
        _tax_options = ['税抜き（外税）', '税込み（内税）']
        # PDF側から引き継いだ税区分を、ウィジェットを描画する前に反映する。
        # 描画後に代入すると Streamlit が例外を投げる。
        _pending_tax = st.session_state.pop('_tax_carry_pending', None)
        if _pending_tax:
            st.session_state['tax_override'] = _pending_tax
            st.session_state['csv_tax_radio'] = _pending_tax
        _saved_tax_override = st.session_state.get('tax_override', '税抜き（外税）')
        _tax_default_idx = 1 if '内税' in str(_saved_tax_override) or '税込' in str(_saved_tax_override) else 0
        _tax_sel = st.radio(
            "💴 見積書の金額表記",
            options=_tax_options,
            index=_tax_default_idx,
            horizontal=True,
            key='csv_tax_radio',
        )
        st.session_state['tax_override'] = _tax_sel

        _csv_col1, _csv_col2 = st.columns([2, 1])
        with _csv_col1:
            _csv_paste = st.text_area(
                "Geminiの解析結果CSVを貼り付け（ヘッダー行必須）",
                height=180,
                placeholder="品名,区分,数量,部品金額,工賃,部品コード\nフロントバンパー,取替,1,45000,0,\nバンパー交換工賃,取替,1,0,12000,",
                key=f"csv_paste_area_{st.session_state.get('csv_area_seq', 0)}",
                value=st.session_state.get('_csv_paste_saved', ''),
            )
        with _csv_col2:
            st.markdown("**CSVファイル（.csv/.txt）**")
            _csv_file = st.file_uploader(
                "CSVファイル",
                type=['csv', 'txt'],
                key='csv_file_upload',
                label_visibility='collapsed',
            )
        _csv_text = ''
        if _csv_file:
            try:
                _raw = _csv_file.read()
                for _enc in ('utf-8-sig', 'utf-8', 'shift-jis', 'cp932'):
                    try:
                        _csv_text = _raw.decode(_enc)
                        break
                    except Exception:
                        continue
            except Exception:
                pass
            if not _csv_text:
                st.error(
                    "❌ CSVファイルの文字コードを判別できません。"
                    "UTF-8 または Shift_JIS で保存し直してください"
                    "（Excelの「Unicodeテキスト」形式は非対応です）。"
                )
            elif not _csv_text.strip():
                st.error("❌ CSVファイルが空です。")
                _csv_text = ''
        elif _csv_paste and _csv_paste.strip():
            _csv_text = _csv_paste.strip()

        if not _csv_text and not _csv_file and st.session_state.get('csv_mode'):
            # 貼り付け欄を空にしたのに前回の取込が残っていると、
            # 消したはずの見積がそのまま生成されてしまう
            st.session_state.pop('csv_items', None)
            st.session_state.pop('csv_mode', None)
            st.session_state.pop('_csv_paste_saved', None)

        if _csv_text:
            _preview_items, _csv_notes = parse_csv_to_items(_csv_text, return_notes=True)
            for _note in _csv_notes[:3]:
                st.warning(f"⚠️ 見積書との差異が記録されています: {_note}")
            if _preview_items:
                st.success(f"✅ {len(_preview_items)}行 読み込み完了 — 部品: ¥{sum(safe_int(it.get('parts_amount',0)) for it in _preview_items):,} / 工賃: ¥{sum(safe_int(it.get('wage',0)) for it in _preview_items):,}")
                st.session_state['csv_items'] = _preview_items
                st.session_state['csv_mode']  = True
                st.session_state['_csv_paste_saved'] = _csv_text
            else:
                st.error("❌ CSVの読み込みに失敗しました。1行目にヘッダー（品名,区分,数量,部品金額,工賃,部品コード）が必要です。")
                st.session_state.pop('csv_items', None)
                st.session_state.pop('csv_mode', None)

        # クリアは取り込みの後ろに置く。前に置くと、貼り付けた直後の描画では
        # まだ csv_items が無いためボタンが1回遅れて出る。
        if st.session_state.get('csv_mode') and st.session_state.get('csv_items'):
            if st.button("🗑️ 取り込みをクリア", key='csv_clear_btn'):
                st.session_state.pop('csv_items', None)
                st.session_state.pop('csv_mode', None)
                st.session_state.pop('_csv_paste_saved', None)
                # 貼り付け欄も空にしないと、ブラウザが直前の値を送り直して
                # 同じ実行内で再取込され、クリアが効かない。キーを消すだけ
                # では戻ってくるので、版番号を上げて別ウィジェットにする。
                _seq = st.session_state.get('csv_area_seq', 0)
                st.session_state.pop(f'csv_paste_area_{_seq}', None)
                st.session_state['csv_area_seq'] = _seq + 1
                st.rerun()

        # ── PDF見積 → NEO 自動変換 ──────────────────────
        st.markdown("---")
        st.markdown("#### 📄 PDF見積 → NEO 自動変換")
        st.caption(
            "見積書PDFをそのままアップロードすると、AI-OCRで明細を読み取り、"
            "NEOファイルまで一気に生成します。Geminiへのコピペは不要です。"
        )
        _pdf_tax_options = ['税抜き（外税）', '税込み（内税）']
        _saved_pdf_tax = st.session_state.get('pdf_tax_override',
                                              st.session_state.get('tax_override', '税抜き（外税）'))
        _pdf_tax_idx = 1 if ('内税' in str(_saved_pdf_tax) or '税込' in str(_saved_pdf_tax)) else 0
        _pdf_tax_sel = st.radio(
            "💴 添付する見積書の金額表記",
            options=_pdf_tax_options,
            index=_pdf_tax_idx,
            horizontal=True,
            key='pdf_tax_radio',
            help="明細の金額が税込みで書かれている見積書は「税込み（内税）」を選んでください。"
                 "取り違えると、NEOの合計が消費税ぶん（10%）ずれます。",
        )
        st.session_state['pdf_tax_override'] = _pdf_tax_sel
        _pdf_is_tax_incl = ('内税' in _pdf_tax_sel or '税込' in _pdf_tax_sel)

        _p2n_file = st.file_uploader(
            "見積書PDF",
            type=['pdf'],
            key='pdf2neo_upload',
        )
        if _p2n_file is not None:
            _p2n_bytes = _p2n_file.read()
            _p2n_file.seek(0)
            st.caption(f"📄 {_p2n_file.name}（{len(_p2n_bytes):,} bytes）")
            if not api_key:
                st.warning(
                    "⚠️ この機能にはGemini APIキーが必要です。"
                    "サイドバーの「APIキー設定」でキーを入力してください。"
                )
            elif st.button("🚀 PDFからNEOを生成", key='pdf2neo_run', type="primary",
                           use_container_width=True):
                st.session_state.pop('pdf2neo_result', None)
                with st.spinner("PDFを解析してNEOを生成しています…（AI-OCRのため30〜90秒かかります）"):
                    st.session_state['pdf2neo_result'] = run_pdf_to_neo_pipeline(
                        _p2n_bytes,
                        api_key,
                        model_name=selected_model,
                        template_bytes=st.session_state.get('custom_neo_bytes'),
                        is_tax_inclusive=_pdf_is_tax_incl,
                        expenses={
                            'towing':     st.session_state.get('exp_towing', 0),
                            'rental_car': st.session_state.get('exp_rental', 0),
                            'tax_exempt': st.session_state.get('exp_exempt', 0),
                        },
                    )
                st.session_state['pdf2neo_tax_inclusive'] = _pdf_is_tax_incl
                st.rerun()

        _p2n_res = st.session_state.get('pdf2neo_result')
        if _p2n_res:
            if _p2n_res.get('error'):
                st.error(f"❌ {_p2n_res['error']}")
            elif not _p2n_res.get('ok'):
                st.error("❌ PDFからNEOを生成できませんでした。")
                for _w in (_p2n_res.get('warnings') or [])[:5]:
                    st.caption(f"・{_w}")
            elif not (_p2n_res.get('items') or []):
                st.error(
                    "❌ 見積書から明細を1行も読み取れませんでした。"
                    "スキャン画像で文字が読めない、APIのクォータ超過、"
                    "対応していない書式のいずれかが考えられます。"
                )
                for _w in (_p2n_res.get('warnings') or [])[:5]:
                    st.caption(f"・{_w}")
            else:
                _p2n_items = _p2n_res.get('items') or []
                _p2n_parts = sum(safe_int(it.get('parts_amount', 0)) for it in _p2n_items)
                _p2n_wage  = sum(safe_int(it.get('wage', 0)) for it in _p2n_items)
                st.success(
                    f"✅ 解析完了 — {len(_p2n_items)}行 ／ "
                    f"部品 ¥{_p2n_parts:,} ／ 工賃 ¥{_p2n_wage:,}"
                )
                # 車検証OCRの失敗など、成功扱いでも伝えるべき警告がある
                for _w in (_p2n_res.get('warnings') or [])[:5]:
                    st.warning(f"⚠️ {_w}")
                _p2n_v = _p2n_res.get('verify') or {}
                if _p2n_v.get('count_match') and _p2n_v.get('total_match'):
                    st.caption("🔍 検証OK: 生成NEOの明細件数と部品金額（税抜）がPDFと一致しました。")
                elif _p2n_v.get('error'):
                    st.caption(f"🔍 検証スキップ: {_p2n_v['error']}")
                else:
                    st.warning(
                        "🔍 検証: PDFと生成NEOに差異があります。"
                        f"件数 NEO {_p2n_v.get('neo_count')} / PDF {_p2n_v.get('pdf_count')}、"
                        f"部品金額(税抜) NEO ¥{safe_int(_p2n_v.get('neo_total')):,} / "
                        f"PDF ¥{safe_int(_p2n_v.get('pdf_parts_total')):,}。"
                        "「プレビューに取り込む」で内容を確認・修正してください。"
                    )
                _p2n_neo = _p2n_res.get('neo_bytes')
                _p2n_c1, _p2n_c2 = st.columns(2)
                with _p2n_c1:
                    if _p2n_neo:
                        st.download_button(
                            "📥 NEOファイルをダウンロード",
                            data=_p2n_neo,
                            file_name="PDF変換_見積.neo",
                            mime="application/octet-stream",
                            key='pdf2neo_dl',
                            use_container_width=True,
                        )
                with _p2n_c2:
                    if _p2n_items and st.button("📝 プレビューに取り込んで修正する",
                                                key='pdf2neo_to_preview',
                                                use_container_width=True):
                        st.session_state['csv_items'] = _p2n_items
                        st.session_state['csv_mode']  = True
                        # PDF側で選んだ税区分をプレビュー側にも引き継ぐ。
                        # 引き継がないと、下流はCSV側のラジオを見るため
                        # 税区分が食い違い、合計が10%ずれる。
                        _carry = ('税込み（内税）'
                                  if st.session_state.get('pdf2neo_tax_inclusive')
                                  else '税抜き（外税）')
                        st.session_state['tax_override'] = _carry
                        # ここで csv_tax_radio に直接代入すると、同じ実行の前半で
                        # 既に描画済みのウィジェットへの代入となり Streamlit が
                        # 例外を投げ、取り込みが中断してしまう。次の実行の
                        # 描画前に反映させるため、一時キーに預けておく。
                        st.session_state['_tax_carry_pending'] = _carry
                        st.session_state['pdf2neo_vehicle_info'] = _p2n_res.get('vehicle_info') or {}
                        st.session_state['vehicle_file_bytes']  = None
                        st.session_state['vehicle_file_name']   = None
                        st.session_state['estimate_file_bytes'] = None
                        st.session_state['estimate_file_name']  = None
                        st.session_state['selected_model'] = selected_model
                        st.session_state['step'] = 2
                        st.rerun()

        # ── オプション設定 ──
        with st.expander("⚙️ オプション設定", expanded=False):
            opt_col1, opt_col2, opt_col3 = st.columns(3)
            with opt_col1:
                # ここで入力された値はどこにも使われておらず、証券番号の欄が
                # サイドバーと二重に存在していた。サイドバー側に一本化する。
                st.caption("保険会社・証券番号・契約者名はサイドバーの「🛡️ 保険情報」で入力してください。")
            with opt_col2:
                st.write("")
            with opt_col3:
                st.write("")

        # ── 開始ボタン ──
        st.markdown("")
        estimate_file = None  # PDF見積書アップロード廃止（CSV取り込みに一本化）
        _csv_mode_active = st.session_state.get('csv_mode') and st.session_state.get('csv_items')
        _has_input = vehicle_file or _csv_mode_active
        if _has_input:
            _btn_label = "🚀 NEO生成を開始 →"
            if st.button(_btn_label, type="primary", use_container_width=True):
                if vehicle_file:
                    st.session_state['vehicle_file_bytes'] = vehicle_file.read()
                    st.session_state['vehicle_file_name']  = vehicle_file.name
                else:
                    st.session_state['vehicle_file_bytes'] = None
                    st.session_state['vehicle_file_name']  = None
                # PDF見積書は使用しない（CSV取り込みに一本化）
                st.session_state['estimate_file_bytes'] = None
                st.session_state['estimate_file_name']  = None
                st.session_state['use_fax_filter'] = False
                st.session_state['use_rasterize']  = False
                st.session_state['use_enhance']    = True
                st.session_state['selected_model'] = selected_model
                st.session_state['step'] = 2
                st.rerun()
        else:
            st.info("📊 CSVを貼り付けるか、車検証をアップロードして「NEO生成を開始」を押してください")

    # =========================================
    # STEP 2: AI解析
    # =========================================
    elif current_step == 2:
        st.markdown('<div class="step-header">② AI解析中...</div>', unsafe_allow_html=True)
        vehicle_bytes  = st.session_state.get('vehicle_file_bytes')
        vehicle_name   = st.session_state.get('vehicle_file_name', '')
        estimate_bytes = st.session_state.get('estimate_file_bytes')
        estimate_name  = st.session_state.get('estimate_file_name', '')
        _use_fax       = st.session_state.get('use_fax_filter', False)
        _use_raster    = st.session_state.get('use_rasterize', False)  # デフォルトFalse（PDF直接送信）
        _use_enhance   = st.session_state.get('use_enhance', True)
        _model         = st.session_state.get('selected_model', GEMINI_MODEL)
        # ベタ打ちモードでは自己修復ループを無効化（DB照合不要のため高速化）
        _is_beta_s2    = st.session_state.get('selected_mode', 'db') == 'beta'
        _enable_sc     = not _is_beta_s2

        # ── CSV取り込みモード: 見積AI解析をスキップ ──
        _csv_mode_s2   = st.session_state.get('csv_mode', False)
        _csv_items_s2  = st.session_state.get('csv_items', [])
        if _csv_mode_s2 and _csv_items_s2 and estimate_bytes is None:
            st.info(f"📊 CSVモード: {len(_csv_items_s2)}行を取り込みます（AI解析をスキップ）")
            # 車検証のみAI解析（ある場合）
            vehicle_data = {}
            # PDF→NEO変換で読み取った車両情報があれば引き継ぐ（車検証未添付時）
            _p2n_vi = st.session_state.get('pdf2neo_vehicle_info')
            if _p2n_vi and not vehicle_bytes:
                vehicle_data = {k: v for k, v in dict(_p2n_vi).items() if k != '_error'}
            if vehicle_bytes:
                with st.spinner("🔍 車検証を解析中..."):
                    try:
                        vehicle_mime = get_mime_type(vehicle_name) if vehicle_bytes else None
                        vehicle_data = analyze_vehicle_registration(api_key, vehicle_bytes, vehicle_mime) or {}
                    except Exception as _veh_err:
                        vehicle_data = {'_error': str(_veh_err)[:120]}
                    if vehicle_data.get('_error'):
                        _queue_step2_msg(
                            'warning',
                            f"⚠️ 車検証を読み取れませんでした（{vehicle_data['_error']}）。"
                            "車両情報は空のまま進みます。下の車両情報欄で手入力できます。"
                        )
                        vehicle_data = {}
                    else:
                        # 低信頼度の注意喚起は、この CSV 取り込み経路にも要る。
                        # 以前は見積書PDF経路にしか無く、主経路である
                        # CSV 取り込みでは読み取り精度が低くても無警告だった。
                        _vc = safe_float(vehicle_data.get('confidence', 1.0), 1.0)
                        if _vc < CONFIDENCE_THRESHOLD:
                            _queue_step2_msg(
                                'warning',
                                f"⚠️ 車検証の読み取り信頼度が低いです（{_vc:.0%}）。"
                                "下の車両情報が正しいかご確認ください。")
            st.session_state['vehicle_data'] = vehicle_data
            # CSVアイテムをestimate_dataとして格納
            _tax_s2 = st.session_state.get('tax_override', '税抜き（外税）')
            _is_tax_incl_csv = '内税' in str(_tax_s2) or '税込' in str(_tax_s2)
            estimate_data = {
                'items':            _csv_items_s2,
                'discount_amount':  0,
                'short_parts_wage': 0,
                'confidence':       1.0,
                'pdf_parts_total':  sum(safe_int(it.get('parts_amount', 0)) for it in _csv_items_s2),
                'pdf_wage_total':   sum(safe_int(it.get('wage', 0)) for it in _csv_items_s2),
                'pdf_grand_total':  0,
                '_is_tax_inclusive': _is_tax_incl_csv,
                '_tax_basis':       'tax_inclusive' if _is_tax_incl_csv else 'tax_exclusive',
                '_page_count':      1,
                '_vehicle_info':    {},
                '_repair_shop_name': '',
                '_csv_import':      True,
                # CSVは貼り付けた内容がそのまま正なので、PDFとの照合や
                # 逆算チェックは対象外。以前は必ず不一致の警告が出ていた。
                '_reverse_match':   True,
            }
            if _is_tax_incl_csv:
                st.info("💴 税込モード: CSVの金額は税込みとして処理されます")
            else:
                st.info("💴 税抜モード: CSVの金額は税抜きとして処理されます")
            st.session_state['estimate_data'] = estimate_data
            st.session_state['_estimate_token'] = _make_estimate_token(
                _csv_items_s2, vehicle_bytes, None)
            st.success(f"✅ CSV取り込み完了（{len(_csv_items_s2)}行）")
            st.session_state['step'] = 3
            st.rerun()

        if vehicle_bytes is None and estimate_bytes is None:
            st.error("ファイルデータが見つかりません。ステップ①に戻ってください。")
            if st.button("← ステップ①に戻る"):
                st.session_state['step'] = 1
                st.rerun()
            st.stop()

        progress = st.progress(0, text="AI解析を開始しています...")
        _wait_msg = st.info(
            "📡 GeminiにPDFを送信し解析中です。"
            "ページ数・ファイルサイズによって **30秒〜2分** かかる場合があります。"
            "このページから移動せずにお待ちください。"
        )

        def _progress_cb(pct: int, text: str):
            try:
                progress.progress(pct, text=text)
            except Exception:
                pass

        try:
            vehicle_mime  = get_mime_type(vehicle_name) if vehicle_bytes else None
            estimate_mime = get_mime_type(estimate_name) if estimate_bytes else None

            if vehicle_bytes and estimate_bytes:
                # 車検証＋見積書を並列で解析
                progress.progress(5, text="🔍 車検証＋見積書を同時解析中...")
                with ThreadPoolExecutor(max_workers=2) as executor:
                    fut_vehicle = executor.submit(
                        analyze_vehicle_registration, api_key, vehicle_bytes, vehicle_mime
                    )
                    fut_estimate = executor.submit(
                        analyze_estimate, api_key, estimate_bytes, estimate_mime, _model,
                        _use_fax, _use_raster, _use_enhance, _enable_sc
                    )
                    try:
                        vehicle_data = fut_vehicle.result() or {}
                    except Exception as _veh_err:
                        vehicle_data = {'_error': str(_veh_err)[:120]}
                    if vehicle_data.get('_error'):
                        _queue_step2_msg(
                            'warning',
                            f"⚠️ 車検証を読み取れませんでした（{vehicle_data['_error']}）。"
                            "車両情報は空のまま進みます。下の車両情報欄で手入力できます。"
                        )
                        vehicle_data = {}
                    progress.progress(40, text="✅ 車検証の解析完了、見積書を処理中...")
                    try:
                        estimate_data = fut_estimate.result() or {}
                    except Exception as _est_err:
                        st.error(f"⚠️ 見積書解析に失敗しました: {str(_est_err)[:100]}")
                        st.warning("ネットワークが不安定な可能性があります。もう一度お試しください。")
                        estimate_data = None
            elif vehicle_bytes:
                # 車検証のみ
                progress.progress(10, text="🔍 車検証を解析中...")
                vehicle_data  = analyze_vehicle_registration(api_key, vehicle_bytes, vehicle_mime) or {}
                if vehicle_data.get('_error'):
                    _queue_step2_msg(
                        'warning',
                        f"⚠️ 車検証を読み取れませんでした（{vehicle_data['_error']}）。"
                        "車両情報は空のまま進みます。下の車両情報欄で手入力できます。"
                    )
                    vehicle_data = {}
                estimate_data = None
            else:
                # 見積書のみ（車検証なし）— 逐次解析でプログレス更新可能
                progress.progress(5, text="🔍 見積書の解析を開始中...")
                vehicle_data  = {}
                estimate_data = analyze_estimate(
                    api_key, estimate_bytes, estimate_mime, _model,
                    _use_fax, _use_raster, _use_enhance, _enable_sc,
                    progress_cb=_progress_cb,
                ) or {}

            st.session_state['vehicle_data'] = vehicle_data
            progress.progress(90, text="✅ 解析完了")
            _wait_msg.empty()  # 「お待ちください」メッセージを非表示

            if vehicle_bytes:
                v_conf = safe_float(vehicle_data.get('confidence', 1.0), 1.0)
                if v_conf < CONFIDENCE_THRESHOLD:
                    _queue_step2_msg('warning',
                        f"⚠️ 車検証の読み取り信頼度が低いです（{v_conf:.0%}）。"
                        "下の車両情報が正しいかご確認ください。")

            # 見積書の後処理
            if estimate_data:
                # 部品名を半角カタカナに変換
                for item in estimate_data.get('items', []):
                    if item.get('name'):
                        item['name'] = to_halfwidth_katakana(item['name'])

                # 見積書ヘッダの車両情報で車検証データの空欄を補完
                # （車検証なしの場合は見積書の車両情報が唯一の情報源となる）
                est_vinfo = estimate_data.get('_vehicle_info', {})
                if est_vinfo and vehicle_data is not None:
                    MERGE_MAP = {
                        'car_name':      'car_name',
                        'car_model':     'car_model',
                        'engine_model':  'engine_model',
                        'color_code':    'color_code',
                        'color_name':    'body_color',
                        'trim_code':     'trim_code',
                        'grade':         'grade',
                        'chassis_no':    'car_serial_no',
                        'mileage':       'kilometer',
                    }
                    supplemented = []
                    for est_key, veh_key in MERGE_MAP.items():
                        est_val = est_vinfo.get(est_key, '')
                        if est_val and not vehicle_data.get(veh_key):
                            vehicle_data[veh_key] = est_val
                            supplemented.append(veh_key)
                    if supplemented:
                        st.session_state['vehicle_data'] = vehicle_data
                        st.info(f"📋 見積書から車両情報を補完: {', '.join(supplemented)}")

                st.session_state['estimate_data'] = estimate_data
                progress.progress(90, text="✅ 見積書の解析完了")

                e_conf = safe_float(estimate_data.get('confidence', 1.0), 1.0)
                if e_conf < CONFIDENCE_THRESHOLD:
                    _queue_step2_msg('warning',
                        f"⚠️ 見積書の読み取り信頼度が低いです（{e_conf:.0%}）。"
                        "明細の内容が正しいかご確認ください。")

                # --- 11. Addata 連携 (車両特定 & 部品マッチング) ---
                _current_mode = st.session_state.get('selected_mode', 'db')
                addata_dir = find_addata_dir()
                if _current_mode == 'db' and addata_dir and vehicle_data:
                    progress.progress(92, text="Addata マスタとの照合を実行中...")
                    veh_match_result = identify_vehicle(addata_dir, vehicle_data)
                    estimate_data['_veh_match_result'] = veh_match_result

                    # is_template（TOYOTA_GENERIC 代用）は実車種が当たって
                    # いないので照合しない。PDF→NEO 側の decide_mode_from_identify
                    # がモードAへ落とすのと揃える。
                    if (veh_match_result.get('is_supported')
                            and not veh_match_result.get('is_template')):
                        if 'items' in estimate_data and estimate_data['items']:
                            # 渡すのは車種フォルダではなく Addata ルート。
                            # 車両情報も渡さないと照合側が車種を引けない。
                            # （以前は存在しないキー addata_folder を読んでおり、
                            #   常に None になって照合が一度も動いていなかった）
                            matched_items, has_rev = match_parts_with_addata(
                                estimate_data['items'], addata_dir, vehicle_data)
                            estimate_data['items'] = matched_items
                            estimate_data['_reverse_match'] = has_rev
                elif _current_mode == 'beta':
                    # ベタ打ちモード: Addata照合をスキップし、PDF見積の内容をそのまま転記
                    estimate_data['_veh_match_result'] = {'match_layer': 3, 'is_supported': False, 'reason': 'ベタ打ちモード（DB照合スキップ）'}
                    progress.progress(92, text="✏️ ベタ打ちモード — PDF見積をそのまま転記...")
                
                # --- 税区分 ユーザー選択値を常に適用（AI自動判定廃止）---
                _tax_override = st.session_state.get('tax_override', '税抜き（外税）')
                if _tax_override == '税込み（内税）':
                    estimate_data['_is_tax_inclusive'] = True
                    estimate_data['_tax_basis']        = 'tax_inclusive'
                else:
                    # デフォルト: 税抜き（外税）
                    estimate_data['_is_tax_inclusive'] = False
                    estimate_data['_tax_basis']        = 'tax_exclusive'
                    estimate_data.pop('_tax_converted', None)

                # --- セッションステートへの保存 ---
                st.session_state['estimate_data'] = estimate_data

                # 精度処理の結果を表示
                info_msgs = []
                if not vehicle_bytes:
                    info_msgs.append("📋 車検証なしモード: 見積書から読み取れた車両情報のみでNEOを作成します。ステップ③で車両情報を確認・補完してください。")
                if _current_mode == 'beta':
                    info_msgs.append("✏️ ベタ打ちモード: PDF見積の全明細をそのままNEOファイルに転記します（DB照合なし）")
                elif estimate_data.get('_veh_match_result', {}).get('is_supported'):
                    v_res = estimate_data['_veh_match_result']
                    _amb = v_res.get('ambiguous') or []
                    if _amb:
                        # 候補が割れたまま先頭を採っている。「連携成功」とだけ
                        # 出すと、別型式のマスタで照合したことが伝わらない。
                        info_msgs.append(
                            f"⚠️ Addata 車種が{len(_amb)}件の候補に割れています"
                            f"（{' / '.join(map(str, _amb))}）。"
                            f"いまは {v_res.get('vehicle_code')} で照合しています。"
                            "初度登録年月を入力すると絞り込めます。"
                            "部品コード・品番が別型式のものになっていないか、"
                            "生成前にプレビューでご確認ください。")
                    else:
                        info_msgs.append(f"🚙 Addata マスタ連携成功: レイヤー{v_res['match_layer']} 一致 (車種コード: {v_res.get('vehicle_code')})")
                else:
                    info_msgs.append("⚠️ Addata マスタ連携: 該当車種が見つかりませんでした (手動入力モード)")
                    # フォールバック処理 (Geminiで車種名とエンジン型式を推測)
                    if vehicle_data.get('car_model'):
                        c_name = vehicle_data.get('car_name', '')
                        e_model = vehicle_data.get('engine_model', '')
                        if not c_name or not e_model:
                            progress.progress(95, text="🌐 Gemini: 不明な車両情報を検索補完中...")
                            comp = complement_vehicle_info_with_gemini(
                                api_key,
                                vehicle_data['car_model'],
                                c_name,
                                e_model
                            )
                            if comp.get('car_name'):
                                vehicle_data['car_name'] = comp['car_name']
                                info_msgs.append(f"🌐 車種名をWebから補完: {comp['car_name']}")
                            if comp.get('engine_model'):
                                vehicle_data['engine_model'] = comp['engine_model']
                                info_msgs.append(f"🌐 エンジン型式をWebから補完: {comp['engine_model']}")
                            st.session_state['vehicle_data'] = vehicle_data

                # 修理工場名の表示
                shop_name = estimate_data.get('_repair_shop_name', '')
                if shop_name:
                    info_msgs.append(f"🏭 修理工場: {shop_name}")
                if estimate_data.get('_fax_filtered', 0) > 0:
                    info_msgs.append("🗑️ FAX送付状を自動除外しました")
                if estimate_data.get('_self_corrected'):
                    info_msgs.append("🔧 自己修復ループが有効になりました")
                page_count = estimate_data.get('_page_count', 1)
                if page_count > 1:
                    info_msgs.append(f"📄 {page_count}ページ分割処理（重複除去済み）")
                tax_basis = estimate_data.get('_tax_basis', 'tax_exclusive')
                if tax_basis == 'tax_inclusive':
                    info_msgs.append("💱 税込モード（ユーザー選択）— NEOファイルも税込で生成します")
                else:
                    info_msgs.append("✅ 税抜モード（ユーザー選択）")
                # 明細行整合性チェックの警告表示
                row_warnings = estimate_data.get('_row_warnings', [])
                if row_warnings:
                    info_msgs.append(f"🔍 明細行整合性チェック: {len(row_warnings)}件の注意事項")
                if info_msgs:
                    for msg in info_msgs:
                        st.info(msg)
                if row_warnings:
                    with st.expander(f"⚠️ 整合性チェック詳細（{len(row_warnings)}件）", expanded=False):
                        for w in row_warnings:
                            st.warning(w)
            else:
                st.session_state['estimate_data'] = None
                progress.progress(90, text="（見積書なし）")

            progress.progress(100, text="✅ 解析完了！")
            st.session_state['_estimate_token'] = _make_estimate_token(
                (estimate_data or {}).get('items') if estimate_data else None,
                vehicle_bytes, estimate_bytes)
            st.session_state['step'] = 3
            st.rerun()
        except Exception as e:
            progress.empty()
            err_str = str(e)
            _cur_model = st.session_state.get('selected_model', _model or _FALLBACK_MODEL)
            # モデル提供終了（404 NOT_FOUND）の場合、利用可能な代替モデルへ自動切り替え
            if _is_model_unavailable_error(err_str) or '提供終了' in err_str:
                _mark_model_unavailable(api_key, _cur_model)
                _alt = get_alternative_gemini_model(api_key, _cur_model)
                if _alt:
                    st.warning(
                        f"⚠️ モデル「{_cur_model}」は提供終了のため利用できません。\n\n"
                        f"**🔄 代替モデル「{_alt}」に自動切り替えました。「② AI解析」をもう一度実行してください。**",
                        icon="⚠️"
                    )
                    st.session_state['selected_model'] = _alt
                    st.session_state.pop('model_selector_v2', None)
                else:
                    st.error(
                        "⚠️ 利用可能なGeminiモデルが見つかりません。\n\n"
                        "APIキーが有効か、Google AI Studio で利用できるモデルを確認してください。\n\n"
                        f"詳細: {err_str}"
                    )
            # クォータ超過エラーの場合、分かりやすいメッセージとリトライを促す
            elif '429' in err_str or 'RESOURCE_EXHAUSTED' in err_str or 'クォータが上限' in err_str:
                _quota_exhausted_set().add(_cur_model)
                # キャッシュクリア
                _api_key_for_err = api_key
                if _api_key_for_err:
                    _ck = _api_key_for_err[-8:]
                    if _ck in _availability_cache():
                        del _availability_cache()[_ck]
                # 代替モデルを探す
                _alt = get_alternative_gemini_model(api_key, _cur_model)
                if _alt:
                    st.warning(
                        f"⚠️ モデル「{_cur_model}」の1日クォータ（250回）が上限に達しました。\n\n"
                        f"**🔄 代替モデル「{_alt}」に自動切り替えます。「② AI解析」ボタンをもう一度押してください。**",
                        icon="⚠️"
                    )
                    # 自動的に代替モデルをセッションに設定
                    st.session_state['selected_model'] = _alt
                else:
                    st.error(
                        "⚠️ 全モデルのクォータが上限に達しました。\n\n"
                        "翌日（リセット後）か、Google AI StudioでAPIキーの課金を有効化してください。"
                    )
            else:
                st.error(f"⚠️ AI解析中にエラーが発生しました:\n\n{err_str}")
                st.code(traceback.format_exc())
            if st.button("← ステップ①に戻る"):
                st.session_state['step'] = 1
                st.rerun()

    # =========================================
    # STEP 3: プレビュー・修正
    # =========================================
    elif current_step == 3:
        vehicle_data  = st.session_state.get('vehicle_data', {})
        estimate_data = st.session_state.get('estimate_data')

        if vehicle_data is None and not estimate_data:
            st.error("解析データがありません。ステップ①に戻ってください。")
            if st.button("← ステップ①に戻る"):
                st.session_state['step'] = 1
                st.rerun()
            st.stop()

        # ステップ①に戻ると vehicle_data は捨てられるが、ユーザーが車両情報
        # フォームに入力した内容は updated_vehicle として保存してある。
        # 戻って再開したときに入力が全部消えないよう、そちらを初期値に使う。
        # 復元するのは「同じ見積」の編集内容だけ。別の見積を読み込んだのに
        # 前のお客様の氏名・車台番号が復活しては困るので、入力元が一致する
        # ときに限る。さらに、新しく読み取れた値の方を常に優先する。
        _saved_vehicle = st.session_state.get('updated_vehicle') or {}
        _uv_token = st.session_state.get('_uv_token')
        _cur_token = st.session_state.get('_estimate_token')
        if _saved_vehicle and _uv_token and _uv_token == _cur_token:
            _merged_vehicle = dict(vehicle_data or {})
            for _k, _v in _saved_vehicle.items():
                if _v not in (None, '', 0) and not _merged_vehicle.get(_k):
                    _merged_vehicle[_k] = _v
            vehicle_data = _merged_vehicle
        elif _saved_vehicle and _uv_token != _cur_token:
            # 別の見積に移ったので前の入力は破棄する
            st.session_state.pop('updated_vehicle', None)
            st.session_state.pop('_uv_token', None)

        # ── 車両ストリップ ──
        veh_match_result = estimate_data.get('_veh_match_result', {}) if estimate_data else {}
        match_is_db = veh_match_result.get('is_supported', False)
        car_name_strip = esc_html(safe_str(vehicle_data.get('car_name', '')))
        car_model_strip = esc_html(safe_str(vehicle_data.get('car_model', '')))
        engine_strip = esc_html(safe_str(vehicle_data.get('engine_model', '')))
        reg_date_strip = safe_str(vehicle_data.get('car_reg_date', ''))
        if len(reg_date_strip) >= 6:
            reg_date_display = f"{reg_date_strip[:4]}/{reg_date_strip[4:6]}"
        else:
            reg_date_display = reg_date_strip
        km_strip = safe_int(vehicle_data.get('kilometer', 0))
        type_desig = esc_html(safe_str(vehicle_data.get('car_model_designation', '')))
        cat_num = esc_html(safe_str(vehicle_data.get('car_category_number', '')))
        v_code = veh_match_result.get('vehicle_code', '')
        items_count = len(estimate_data.get('items', [])) if estimate_data else 0

        # HTMLを安全に組み立て（f-string内の条件式を排除）
        _badges_parts = []
        if type_desig:
            _badges_parts.append(f'<span style="background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">型式指定 {type_desig}</span>')
        if cat_num:
            _badges_parts.append(f'<span style="background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">類別 {cat_num}</span>')
        _badges_html = ' '.join(_badges_parts)

        _mode_badge = '<span style="background:#f1f5f9;color:#475569;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">✏️ ベタ打ち</span>'

        _detail_text = f'{engine_strip} ／ {esc_html(reg_date_display)}登録 ／ {km_strip:,}km'

        _strip_html = (
            '<div style="background:linear-gradient(135deg,#1a2744 0%,#1e3a5f 100%);color:#fff;'
            'border-radius:10px;padding:16px 20px;margin-bottom:16px;display:flex;align-items:flex-start;gap:16px">'
            '<span style="font-size:32px">🚗</span>'
            '<div style="flex:1">'
            f'<div style="font-size:18px;font-weight:700">{car_name_strip} '
            f'<span style="font-size:14px;font-weight:400">{car_model_strip}</span></div>'
            f'<div style="font-size:12px;color:#94a3b8;margin-top:2px">{_detail_text}</div>'
            f'<div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">{_badges_html}</div>'
            '</div>'
            f'<div style="text-align:right">{_mode_badge}'
            f'<div style="font-size:11px;color:#94a3b8;margin-top:4px">{items_count}件</div>'
            '</div>'
            '</div>'
        )
        st.markdown(_strip_html, unsafe_allow_html=True)

        # ── 解析ログ表示 ──────────────────────────────────────────────────────────
        if estimate_data:
            _analysis_log = estimate_data.get('_analysis_log', [])
            if _analysis_log:
                # 最終行から一致・不一致を判定してラベルを変える
                _log_label_ok = any('✅' in l for l in _analysis_log)
                _log_label_ng = any('⚠️ 差額' in l for l in _analysis_log)
                _log_icon = '✅' if _log_label_ok and not _log_label_ng else ('⚠️' if _log_label_ng else '🔍')
                with st.expander(f"{_log_icon} 解析ログ（詳細）", expanded=False):
                    st.code('\n'.join(_analysis_log), language=None)

        # 信頼度・税区分の警告
        v_conf = safe_float(vehicle_data.get('confidence', 1.0), 1.0)
        if v_conf < CONFIDENCE_THRESHOLD:
            st.markdown(f'<div class="alert alert-warn">⚠️ 車検証の読み取り信頼度: <b>{v_conf:.0%}</b> — 内容をよくご確認ください。</div>', unsafe_allow_html=True)
        if estimate_data:
            e_conf = safe_float(estimate_data.get('confidence', 1.0), 1.0)
            if e_conf < CONFIDENCE_THRESHOLD:
                st.markdown(f'<div class="alert alert-warn">⚠️ 見積書の読み取り信頼度: <b>{e_conf:.0%}</b> — 内容をよくご確認ください。</div>', unsafe_allow_html=True)
            tax_basis = estimate_data.get('_tax_basis', 'tax_exclusive')
            rev_match = estimate_data.get('_reverse_match', False)
            shop_name  = estimate_data.get('_repair_shop_name', '')
            # コグニセブン設定用 税モード（ユーザー選択値）
            if tax_basis == 'tax_inclusive':
                cogni_tax_mode = '税込'
                cogni_tax_color = '#1e40af'
                cogni_tax_bg = '#dbeafe'
                cogni_tax_border = '#3b82f6'
                cogni_tax_icon = '🔵'
                basis_label = '税込明細（ユーザー設定）'
            else:
                cogni_tax_mode = '税抜'
                cogni_tax_color = '#14532d'
                cogni_tax_bg = '#dcfce7'
                cogni_tax_border = '#22c55e'
                cogni_tax_icon = '🟢'
                basis_label = '税抜明細（ユーザー設定）'
            rev_icon = '✅ 逆算一致' if rev_match else '⚠️ 逆算不一致（金額を確認してください）'
            shop_html   = f'<div style="font-size:13px;color:#374151;margin-bottom:10px">🏭 修理工場: <b>{esc_html(shop_name)}</b></div>' if shop_name else ''
            st.markdown(f'''
<div style="border:2px solid {cogni_tax_border};border-radius:8px;background:{cogni_tax_bg};padding:14px 18px;margin-bottom:12px">
  {shop_html}
  <div style="font-size:13px;color:{cogni_tax_color};font-weight:600;margin-bottom:6px">■ コグニセブン設定用 税区分</div>
  <div style="font-size:22px;font-weight:700;color:{cogni_tax_color}">{cogni_tax_icon} コグニセブンを <u>{cogni_tax_mode}モード</u> に設定してください</div>
  <div style="font-size:13px;color:{cogni_tax_color};margin-top:4px">（{basis_label} ／ {rev_icon}）</div>
</div>
''', unsafe_allow_html=True)

        # ── タブ（見積明細タブ廃止・編集は合計・費用タブへ統合）──
        # ステップ②で出せなかった通知（車検証の読み取り失敗・低信頼度など）を
        # ここで表示する。ステップ②は直後に rerun するため、あちらでは残らない。
        for _k2, _m2 in st.session_state.pop('_step2_msgs', []):
            getattr(st, _k2, st.info)(_m2)

        # テンプレートNEOを使うと、空欄のままの項目にはテンプレート側の値
        # （前の案件の氏名・車台番号・事故受付番号など）がそのまま残る。
        # 画面は空欄に見えるので、書かないと利用者は気づけない。
        if st.session_state.get('custom_neo_bytes'):
            st.warning(
                f"⚠️ テンプレートNEO「{safe_str(st.session_state.get('custom_neo_name', ''))}」"
                "を使用中です。下の車両情報とサイドバーの事故情報のうち、"
                "**空欄のままの項目はテンプレートに入っている値がそのまま .neo に残ります**"
                "（前の案件の使用者名・車台番号・事故受付番号など）。"
                "別の案件として出す項目は必ず入力し直してください。"
            )

        tab_vehicle, tab_totals = st.tabs(["🚗 車両情報", "💰 合計・費用"])

        with tab_vehicle:
            st.markdown('<div class="section-title">📋 車検証情報</div>', unsafe_allow_html=True)
            # 車両情報（修正可能なフォーム）
            col1, col2 = st.columns(2)
            with col1:
                v_customer = st.text_input("使用者名",    value=safe_str(vehicle_data.get('customer_name', '')),    key='v_customer',
                                           help="コグニセブンの列幅の都合で、全角10文字（20バイト）までがNEOに入ります")
                v_owner    = st.text_input("所有者名",    value=safe_str(vehicle_data.get('owner_name', '')),       key='v_owner',
                                           help="全角10文字（20バイト）までNEOに入ります")
                v_postal   = st.text_input("郵便番号",    value=safe_str(vehicle_data.get('postal_no', '')),        key='v_postal')
                v_pref     = st.text_input("都道府県",    value=safe_str(vehicle_data.get('prefecture', '')),       key='v_pref')
                v_muni     = st.text_input("市区町村",    value=safe_str(vehicle_data.get('municipality', '')),     key='v_muni')
                v_addr     = st.text_input("その他住所",  value=safe_str(vehicle_data.get('address_other', '')),    key='v_addr')
            with col2:
                v_dept   = st.text_input("登録番号 地名",   value=safe_str(vehicle_data.get('car_reg_department', '')), key='v_dept')
                v_div    = st.text_input("登録番号 分類番号", value=safe_str(vehicle_data.get('car_reg_division', '')),   key='v_div')
                v_biz    = st.text_input("登録番号 かな",   value=safe_str(vehicle_data.get('car_reg_business', '')),   key='v_biz')
                v_serial = st.text_input("登録番号 一連番号", value=safe_str(vehicle_data.get('car_reg_serial', '')),    key='v_serial')
                v_csn    = st.text_input("車台番号",       value=safe_str(vehicle_data.get('car_serial_no', '')),       key='v_csn')
                v_carname = st.text_input("車名",          value=safe_str(vehicle_data.get('car_name', '')),             key='v_carname',
                                          help="全角25文字（50バイト）までNEOに入ります")
            col3, col4, col5 = st.columns(3)
            with col3:
                v_km      = st.number_input("走行距離 (km)", value=safe_int(vehicle_data.get('kilometer', 0)), min_value=0, step=1000, key='v_km')
            with col4:
                v_term    = st.text_input("有効期限 (YYYYMMDD)",   value=safe_str(vehicle_data.get('term_date', '')),    key='v_term')
            with col5:
                v_regdate = st.text_input("初度登録年月 (YYYYMM00)", value=safe_str(vehicle_data.get('car_reg_date', '')), key='v_regdate')

            # 注記(AnNote.ini)の数量欄は2桁固定で、100以上は99として書かれる。
            # 明細テーブルには原本どおり入るので、同じ .neo の中で数量が
            # 食い違う。黙って丸めず知らせる。
            _qty_over = [str(_it.get('name', '') or '')
                         for _it in (estimate_data.get('items') or [])
                         if safe_int(_it.get('quantity', 1), 1) > 99]
            if _qty_over:
                st.warning(
                    f"⚠️ 数量が100以上の行が{len(_qty_over)}件あります"
                    f"（{'、'.join(_qty_over[:3])}{'ほか' if len(_qty_over) > 3 else ''}）。"
                    "コグニセブンの注記欄は数量が2桁までのため、注記側は99として"
                    "書かれます（明細欄には原本どおりの数量が入ります）。")

            # 読み取れない日付は黙って捨てられる（または和暦の組み立てで
            # 落ちる）ので、事故日と同じように画面で知らせる。
            for _lbl, _val, _norm, _hint in (
                ('有効期限', v_term, _normalize_date8, 'YYYYMMDD（例: 20280315）'),
                ('初度登録年月', v_regdate, _normalize_ym8, 'YYYYMM00（例: 20190300）'),
            ):
                if _val and not _norm(_val):
                    st.warning(f"⚠️ {_lbl}「{_val}」は日付として読み取れません。"
                               f"{_hint} の形式で入力してください。"
                               "このままでは NEO に書き込まれません。")

            # 車両詳細情報
            st.markdown('<div class="section-title" style="margin-top:16px">🔧 車両詳細</div>', unsafe_allow_html=True)
            st.caption(
                "※ 型式・エンジン型式・車両重量・排気量は、コグニセブンのNEOに対応する"
                "保存先が無いため参考表示です（ファイルには書き込まれません）。"
                "車体の色・カラーコード・トリムコード・型式指定番号・類別区分番号は書き込まれます。"
            )
            dc1, dc2, dc3 = st.columns(3)
            with dc1:
                v_model     = st.text_input("型式",         value=safe_str(vehicle_data.get('car_model', '')),              key='v_model')
                v_engine    = st.text_input("エンジン型式", value=safe_str(vehicle_data.get('engine_model', '')),           key='v_engine')
            with dc2:
                v_color     = st.text_input("車体の色",     value=safe_str(vehicle_data.get('body_color', '')),             key='v_color')
                v_colorcode = st.text_input("カラーコード", value=safe_str(vehicle_data.get('color_code', '')),             key='v_colorcode')
            with dc3:
                v_trimcode  = st.text_input("トリムコード", value=safe_str(vehicle_data.get('trim_code', '')),              key='v_trimcode')
                v_modeldesig = st.text_input("型式指定番号", value=safe_str(vehicle_data.get('car_model_designation', '')), key='v_modeldesig')
            dc4, dc5, dc6 = st.columns(3)
            with dc4:
                v_catnum    = st.text_input("類別区分番号", value=safe_str(vehicle_data.get('car_category_number', '')),    key='v_catnum')
            with dc5:
                v_weight    = st.number_input("車両重量 (kg)",  value=safe_int(vehicle_data.get('car_weight', 0)),          min_value=0, step=10, key='v_weight')
            with dc6:
                v_displace  = st.number_input("排気量 (cc)",    value=safe_int(vehicle_data.get('engine_displacement', 0)), min_value=0, step=100, key='v_displace')

            # 列幅を超えた分は無言で切り捨てられ、画面には全文が残るため
            # ユーザーは気づけない。実際に切られる項目だけを知らせる。
            # 以前は7項目しか見ておらず、車体の色（30バイト）のように
            # メーカー純正色名だとほぼ必ず切れる欄が対象外だった。
            # 塗色名が途中で切れると塗装の色種別の根拠が読めなくなる。
            # 切り詰められる欄はすべて挙げる。
            for _lbl, _val, _w in (
                ('使用者名',       v_customer,   _CUST_WIDTH['UserName']),
                ('所有者名',       v_owner,      _CUST_WIDTH['OwnerName']),
                ('郵便番号',       v_postal,     _CUST_WIDTH['PostalNo']),
                ('都道府県',       v_pref,       _CUST_WIDTH['Prefecture']),
                ('市区町村',       v_muni,       _CUST_WIDTH['Municipality']),
                ('その他住所',     v_addr,       _CUST_WIDTH['AddressOther1']),
                ('登録番号 地名',   v_dept,       _CUST_WIDTH['CarRegNoDepartment']),
                ('登録番号 分類番号', v_div,      _CUST_WIDTH['CarRegNoDivision']),
                ('登録番号 かな',   v_biz,        _CUST_WIDTH['CarRegNoBusiness']),
                ('登録番号 一連番号', v_serial,   _CUST_WIDTH['CarRegNoSerial']),
                ('車台番号',       v_csn,        _CUST_WIDTH['CarSerialNo']),
                ('型式指定番号',    v_modeldesig, _CUST_WIDTH['CarMouldNo']),
                ('類別区分番号',    v_catnum,     _CUST_WIDTH['CarKindNo']),
                ('車名',           v_carname,    _CAR_WIDTH['CarName']),
                ('車体の色',       v_color,      _CAR_WIDTH['ColorName']),
                ('カラーコード',    v_colorcode,  _CAR_WIDTH['ColorCode']),
                ('トリムコード',    v_trimcode,   _CAR_WIDTH['TrimCode']),
            ):
                _cut = cp932_trim(_val, _w)
                if _val and _cut != safe_str(_val):
                    st.warning(
                        f"⚠️ {_lbl}はコグニセブンの列幅（{_w}バイト＝全角{_w // 2}文字）を"
                        f"超えています。NEOには「{_cut}」までしか入りません。"
                        "短い表記に直してください。")

        # 入力途中の内容を毎回保存しておく。ステップ①に戻ると
        # vehicle_data が捨てられるため、保存しないと入力が全て消える。
        updated_vehicle = {
            'customer_name':      v_customer,
            'owner_name':         v_owner,
            'postal_no':          v_postal,
            'prefecture':         v_pref,
            'municipality':       v_muni,
            'address_other':      v_addr,
            'car_reg_department': v_dept,
            'car_reg_division':   v_div,
            'car_reg_business':   v_biz,
            'car_reg_serial':     v_serial,
            'car_serial_no':      v_csn,
            'car_name':           v_carname,
            'car_model':          v_model,
            'engine_model':       v_engine,
            'body_color':         v_color,
            'color_code':         v_colorcode,
            'trim_code':          v_trimcode,
            'car_model_designation': v_modeldesig,
            'car_category_number':   v_catnum,
            'car_weight':         v_weight,
            'engine_displacement': v_displace,
            'kilometer':          v_km,
            'term_date':          v_term,
            'car_reg_date':       v_regdate,
        }
        # 生成ボタンを押す前でも入力内容を保持する（ステップ①に戻っても消えない）。
        # どの見積に対する入力かを一緒に記録し、別の見積では復元しない。
        st.session_state['updated_vehicle'] = updated_vehicle
        st.session_state['_uv_token'] = st.session_state.get('_estimate_token')

        # 見積明細（合計・費用タブ内で編集）
        calc_parts    = 0
        calc_wages    = 0
        pdf_parts     = 0
        pdf_wages     = 0
        sp            = 0
        wage_match_sp = False  # Step4でも参照するため初期化
        # tab_totals 内の条件分岐に依存する変数を安全のため事前初期化
        _step3_mode       = st.session_state.get('selected_mode', 'db')
        discrepancies     = []
        total_diff        = 0
        edited_items      = []

        with tab_totals:
          if estimate_data and estimate_data.get('items'):

            # ── 明細行一覧 (編集可) ──────────────────────────────
            st.markdown('<div class="section-title">📋 明細行一覧（全項目・編集可）</div>', unsafe_allow_html=True)

            _items_src = estimate_data['items']
            # ── 行操作ボタン（挿入・コピー・削除） ──────────────────────
            _op_col1, _op_col2, _op_col3, _op_col4 = st.columns([1, 1, 1, 5])
            with _op_col1:
                if st.button("➕ 行挿入", key="row_insert_btn", help="最終行に空白行を追加"):
                    _new_row = {
                        'name': '', 'method': '', 'work_code': '', 'index_value': '',
                        'quantity': 1, 'parts_amount': 0, 'wage': 0, 'part_no': '',
                        '_master_name': '', '_master_price': 0, '_master_part_no': '',
                        '_master_repair_code': '', '_master_branch_code': '',
                        '_master_part_code_r': '', '_master_part_code_l': '',
                        '_master_section_code': '', 'match_level': '',
                        '_match_level': 0, '_original_name': '', '_original_parts_amount': 0,
                    }
                    estimate_data['items'].append(_new_row)
                    st.session_state['estimate_data'] = estimate_data
                    st.rerun()
            with _op_col2:
                _copy_no = st.number_input("コピーNo", min_value=1, max_value=max(len(_items_src), 1),
                                           value=1, step=1, key="row_copy_no", label_visibility="collapsed")
            with _op_col3:
                if st.button("📋 コピー", key="row_copy_btn", help="指定No行を複製して最終行に追加"):
                    _cidx = int(_copy_no) - 1
                    if 0 <= _cidx < len(_items_src):
                        import copy as _copy
                        _copied = _copy.deepcopy(_items_src[_cidx])
                        estimate_data['items'].append(_copied)
                        st.session_state['estimate_data'] = estimate_data
                        st.rerun()

            # 表示用DataFrame（7列）: No / 部品番号 / 品名 / 数量 / 部品金額 / 工数 / 工賃
            _edit_rows = []
            for _i, _item in enumerate(_items_src):
                _part_code   = str(_item.get('part_no', '') or _item.get('_master_part_no', '') or '')
                _index_value = str(_item.get('index_value', '') or '')
                _edit_rows.append({
                    'No':     _i + 1,
                    '部品番号': _part_code,
                    '品名':   str(_item.get('name', '')),
                    '数量':   safe_int(_item.get('quantity', 1), 1),
                    '部品金額': safe_int(_item.get('parts_amount', 0)),
                    '工数':   _index_value,
                    '工賃':   safe_int(_item.get('wage', 0)),
                })
            _df_edit = pd.DataFrame(_edit_rows) if _edit_rows else pd.DataFrame(
                columns=['No', '部品番号', '品名', '数量', '部品金額', '工数', '工賃'])
            # キーを行数と連動させることで行挿入後に data_editor を強制再初期化する
            # 完全な固定キーにすると、行を削除したときのフロント側の
            # 編集状態が残り、振り直した No が画面に反映されない。
            # 画面の No と「コピー」が指す行がずれ、別の行が複製される。
            # 行数が変わったときだけ作り直す（セル編集では行数は変わらない
            # ので、入力中の内容は捨てられない）。
            _ed_ver = st.session_state.get('items_editor_ver', 0)
            if st.session_state.get('items_editor_rows') != len(_items_src):
                st.session_state['items_editor_rows'] = len(_items_src)
                _ed_ver += 1
                st.session_state['items_editor_ver'] = _ed_ver
            _editor_key = f'items_editor_{_ed_ver}'
            # height を固定して描画行数を制限（全行フル展開すると100行超で重くなるため）
            _editor_height = min(600, max(200, len(_items_src) * 35 + 60))
            _edited_df = st.data_editor(
                _df_edit,
                use_container_width=True,
                hide_index=True,
                num_rows="dynamic",
                height=_editor_height,
                column_config={
                    'No':     st.column_config.NumberColumn('No', disabled=True, width='small'),
                    '部品番号': st.column_config.TextColumn('部品番号'),
                    '品名':   st.column_config.TextColumn('品名', width='large'),
                    '数量':   st.column_config.NumberColumn('数量', min_value=1, step=1, width='small'),
                    '部品金額': st.column_config.NumberColumn('部品金額', step=1, format="¥%d"),
                    '工数':   st.column_config.TextColumn('工数', width='small'),
                    '工賃':   st.column_config.NumberColumn('工賃', step=1, format="¥%d"),
                },
                key=_editor_key,
            )
            # 編集後データを反映（既存行の内部メタデータを保持、新規行はデフォルト値）
            _edited_df = _edited_df.reset_index(drop=True)
            # 行を削除すると位置がずれるため、連番を振り直す前に元のNoを控える。
            # 位置で元データを引くと、削除以降の行が隣の行の区分・品番・
            # マッチ結果を引き継いでしまい、NEOに誤った内容が書かれる。
            _orig_no_list = _edited_df['No'].tolist() if 'No' in _edited_df.columns else []
            _edited_df['No'] = range(1, len(_edited_df) + 1)
            edited_items = []
            for _i, _row in _edited_df.iterrows():
                _nv = _row.get('品名', '');     _nv = '' if pd.isna(_nv) else str(_nv)
                _pc = _row.get('部品番号', ''); _pc = '' if pd.isna(_pc) else str(_pc)
                _iv = _row.get('工数', '');     _iv = '' if pd.isna(_iv) else str(_iv)
                # 既存行のメタデータを引き継ぐ（新規追加行はデフォルト）
                _src_idx = None
                if _i < len(_orig_no_list):
                    _no_val = _orig_no_list[_i]
                    if _no_val is not None and not pd.isna(_no_val):
                        try:
                            _src_idx = int(_no_val) - 1
                        except (TypeError, ValueError):
                            _src_idx = None
                _orig = (_items_src[_src_idx]
                         if _src_idx is not None and 0 <= _src_idx < len(_items_src)
                         else {})
                _wk   = _orig.get('work_code', '') or _orig.get('method', '')
                edited_items.append({
                    'name': _nv, 'method': _wk, 'work_code': _wk,
                    'index_value': _iv,
                    'quantity': safe_int(_row.get('数量', 1), 1),
                    'parts_amount': safe_int(_row.get('部品金額', 0)),
                    'wage': safe_int(_row.get('工賃', 0)),
                    'part_no': _pc,
                    '_master_name': _orig.get('_master_name', ''),
                    '_master_price': _orig.get('_master_price', 0),
                    '_master_part_no': _orig.get('_master_part_no', ''),
                    '_master_repair_code': _orig.get('_master_repair_code', ''),
                    '_master_branch_code': _orig.get('_master_branch_code', ''),
                    '_master_part_code_r': _orig.get('_master_part_code_r', ''),
                    '_master_part_code_l': _orig.get('_master_part_code_l', ''),
                    # 照合結果は明細タブを通っても落としてはいけない。
                    # 'match_level'(L1..L4) を落とすと未マッチ部品の ※ が消え、
                    # '_master_section_code' を落とすと部品コードが空になる。
                    'match_level': _orig.get('match_level', ''),
                    '_master_section_code': _orig.get('_master_section_code', ''),
                    '_match_level': _orig.get('_match_level', 0),
                    '_original_name': _orig.get('name', _nv),
                    '_original_parts_amount': _orig.get('parts_amount', safe_int(_row.get('部品金額', 0))),
                })
            estimate_data['items'] = edited_items
            # 明細も列幅で無言に切られる。車両情報と同じように画面で知らせる。
            # 切られたことに気づけるのが、コグニセブンに取り込んだ後ではなく
            # ここでなければ、部品番号が切れて発注に使えないまま出荷される。
            for _wi, _wit in enumerate(edited_items, 1):
                for _wlbl, _wraw, _ww in (
                    ('品名',     _wit.get('name', ''),    _ERPARTS_WIDTH['PartsName']),
                    ('部品番号', _wit.get('part_no', ''), _ERPARTS_WIDTH['PartsNo']),
                ):
                    _wraw = safe_str(_wraw)
                    _wcut = cp932_trim(_wraw, _ww)
                    if _wraw and _wcut != _wraw:
                        st.warning(
                            f"⚠️ {_wi}行目の{_wlbl}はコグニセブンの列幅"
                            f"（{_ww}バイト＝全角{_ww // 2}文字）を超えています。"
                            f"NEOには「{_wcut}」までしか入りません。")
            st.session_state['estimate_data'] = estimate_data
            for _it in edited_items:
                calc_parts += safe_int(_it.get('parts_amount', 0))
                calc_wages += safe_int(_it.get('wage', 0))
            sp = 0
            pdf_parts = safe_int(estimate_data.get('pdf_parts_total', 0))
            pdf_wages = safe_int(estimate_data.get('pdf_wage_total', 0))

            st.markdown("---")
            # ── 金額サマリー ────────────────────────────────────
            st.markdown('<div class="section-title">💰 金額サマリー</div>', unsafe_allow_html=True)
            scol1, scol2, scol3 = st.columns(3)
            rev_match = estimate_data.get('_reverse_match', False)

            # 税込/税抜モード判定
            is_tax_incl_s3 = estimate_data.get('_is_tax_inclusive', False)
            tax_label_sfx  = "税込" if is_tax_incl_s3 else "税抜"

            # 金額差額の計算
            # CSV取り込みでは「PDF記載の金額」が存在しないため、
            # 明細を編集するたびに存在しないPDFとの差異警告が出ていた。
            _csv_mode_s3 = bool(estimate_data.get('_csv_import'))
            if _csv_mode_s3:
                pdf_parts = 0
                pdf_wages = 0
            parts_diff = calc_parts - pdf_parts if pdf_parts > 0 else 0
            # SP込みでも一致チェック（部品）
            parts_match_sp = (calc_parts + sp == pdf_parts) if pdf_parts > 0 else False
            # 税込モードでは明細合算とPDF記載値の小差（明細行数×1円以内）も一致とみなす
            _parts_tol = len(edited_items) if is_tax_incl_s3 else 0
            parts_match_tol = (abs(calc_parts - pdf_parts) <= _parts_tol) if pdf_parts > 0 else False
            parts_match_tol_sp = (abs(calc_parts + sp - pdf_parts) <= _parts_tol) if pdf_parts > 0 else False
            parts_match = (calc_parts == pdf_parts) or parts_match_sp or parts_match_tol or parts_match_tol_sp
            wage_diff = calc_wages - pdf_wages if pdf_wages > 0 else 0
            # SP込みでも一致チェック（工賃）: Honda Cars等でSPが工賃列に含まれる場合
            wage_match_sp = (calc_wages + sp == pdf_wages) if pdf_wages > 0 else False
            _wages_tol = len(edited_items) if is_tax_incl_s3 else 0
            wage_match_tol = (abs(calc_wages - pdf_wages) <= _wages_tol) if pdf_wages > 0 else False
            wage_match_tol_sp = (abs(calc_wages + sp - pdf_wages) <= _wages_tol) if pdf_wages > 0 else False
            wage_match = (calc_wages == pdf_wages) or wage_match_sp or wage_match_tol or wage_match_tol_sp
            has_discrepancy = False

            with scol1:
                st.metric(f"部品合計（{tax_label_sfx}）", f"¥{calc_parts:,}")
                if pdf_parts > 0 and not parts_match and not rev_match:
                    has_discrepancy = True
                    st.markdown(
                        f'<div class="error-box">⚠️ <b>部品相違</b>: PDF ¥{pdf_parts:,} ≠ 計算 ¥{calc_parts:,}（差額: {parts_diff:+,}円）</div>',
                        unsafe_allow_html=True
                    )
                elif pdf_parts > 0:
                    st.markdown('<div class="success-box">✅ PDF金額と一致</div>', unsafe_allow_html=True)
            with scol2:
                st.metric(f"工賃合計（{tax_label_sfx}）", f"¥{calc_wages:,}")
                if pdf_wages > 0 and not wage_match and not rev_match:
                    has_discrepancy = True
                    st.markdown(
                        f'<div class="error-box">⚠️ <b>工賃相違</b>: PDF ¥{pdf_wages:,} ≠ 計算 ¥{calc_wages:,}（差額: {wage_diff:+,}円）</div>',
                        unsafe_allow_html=True
                    )
                elif pdf_wages > 0:
                    st.markdown('<div class="success-box">✅ PDF金額と一致</div>', unsafe_allow_html=True)
            with scol3:
                exp_tow = st.session_state.get('exp_towing', 0)
                exp_ren = st.session_state.get('exp_rental', 0)
                exp_exm = st.session_state.get('exp_exempt', 0)
                # ショートパーツを合計に含める（0のままだと画面だけ少なくなる）
                sp = safe_int((estimate_data or {}).get('short_parts_wage', 0)) or sp
                sub = calc_parts + calc_wages + sp + exp_tow + exp_ren
                if is_tax_incl_s3:
                    # 税込モード: 明細金額は既に税込。ただし費用欄は「税抜」で
                    # 入力させているため、費用ぶんの消費税は別に足す。
                    # これを忘れると画面の合計とNEOの合計が食い違う。
                    tax   = jpy_round((sp + exp_tow + exp_ren) * TAX_RATE)
                    total = sub + tax + exp_exm
                else:
                    tax   = jpy_round(sub * TAX_RATE)
                    total = sub + tax + exp_exm
                st.metric("合計（税込）", f"¥{total:,}")
                if is_tax_incl_s3:
                    # 明細ぶんの税込額が「税抜＋消費税」で表せない場合、
                    # .neo に入る総額はここに出している額と1円ずれる。
                    # 画面が原本どおりの数字を出したままファイルだけ違うと、
                    # 突き合わせでは絶対に見つからない。
                    _items_intax = calc_parts + calc_wages
                    _achievable = best_intax_for(_items_intax)
                    if _achievable != _items_intax:
                        _diff = _achievable - _items_intax
                        st.caption(
                            f"⚠️ コグニセブンは税抜で保存して消費税を計算するため、"
                            f"この税込額（¥{_items_intax:,}）はそのままでは表せません。"
                            f"生成される .neo の明細合計は ¥{_achievable:,}"
                            f"（{_diff:+,}円）になります。")
                if rev_match:
                    st.markdown('<div class="success-box">✅ 逆算一致</div>', unsafe_allow_html=True)

            # ── STEP 3 バリデーション結果パネル ──
            # parts_match / wage_match はこの直上でリアルタイム再計算済みの値を使用する
            # (estimate_data['totals_verification'] は解析時の古い判定のため使わない)
            _tv_has_data = (pdf_parts > 0 or pdf_wages > 0)
            if _tv_has_data:
                _tv_p_mismatch = pdf_parts > 0 and not parts_match and not rev_match
                _tv_w_mismatch = pdf_wages > 0 and not wage_match and not rev_match
                if not _tv_p_mismatch and not _tv_w_mismatch:
                    st.markdown(
                        '<div class="success-box" style="padding:10px 16px;margin-bottom:12px">'
                        '✅ <b>【STEP 3 バリデーション: 合格】</b> 見積書記載の合計値と1円の誤差もなく一致しています。'
                        '</div>', unsafe_allow_html=True)
                else:
                    _disp_p_diff = parts_diff if _tv_p_mismatch else 0
                    _disp_w_diff = wage_diff  if _tv_w_mismatch else 0
                    _err_parts_list = []
                    if _disp_p_diff != 0: _err_parts_list.append(f'部品差額{_disp_p_diff:+,}円')
                    if _disp_w_diff != 0: _err_parts_list.append(f'工賃差額{_disp_w_diff:+,}円')
                    err_text = f'<br>推定原因: {"・".join(_err_parts_list)}' if _err_parts_list else ''
                    st.markdown(
                        f'<div class="error-box" style="padding:10px 16px;margin-bottom:12px">'
                        f'🚨 <b>【STEP 3 バリデーション: 不合格】</b> 金額の不一致が検出されています。<br>'
                        f'部品差額: {_disp_p_diff:+,}円 ／ 工賃差額: {_disp_w_diff:+,}円{err_text}'
                        f'</div>', unsafe_allow_html=True)

            # ── ベタ打ちモード専用: 包括的金額一致検証パネル ──
            _step3_mode = st.session_state.get('selected_mode', 'db')
            if _step3_mode == 'beta':
                st.markdown('<div class="section-title">📋 ベタ打ちモード — 金額一致検証レポート</div>', unsafe_allow_html=True)
                pdf_grand = safe_int(estimate_data.get('pdf_grand_total', 0))
                tax_basis_s3 = estimate_data.get('_tax_basis', 'unknown')
                _beta_verification_rows = []
                _beta_all_ok = True

                # 各行ごとの部品価格・工賃の記録
                for idx_b, item_b in enumerate(edited_items):
                    row_name = item_b.get('name', f'行{idx_b+1}')
                    row_parts = safe_int(item_b.get('parts_amount', 0))
                    row_wage = safe_int(item_b.get('wage', 0))
                    _beta_verification_rows.append({
                        'No': idx_b + 1,
                        '品名': row_name,
                        '部品価格': f"¥{row_parts:,}" if row_parts != 0 else '-',
                        '工賃': f"¥{row_wage:,}" if row_wage != 0 else '-',
                    })

                with st.expander("📊 明細行一覧（全項目）", expanded=False):
                    # 全行を削除すると空リストになる。set_index('No') が
                    # KeyError で落ちて画面が操作不能になるため列を明示する。
                    st.table(pd.DataFrame(
                        _beta_verification_rows,
                        columns=['No', '品名', '部品価格', '工賃'],
                    ).set_index('No'))

                # 合算値の一致確認
                _verify_items = []
                # ① 部品合計
                parts_ok = parts_match or rev_match
                _verify_items.append(('部品合計', pdf_parts, calc_parts, parts_ok))
                if not parts_ok and pdf_parts > 0:
                    _beta_all_ok = False
                # ② 工賃合計
                wage_ok = wage_match or rev_match
                _verify_items.append(('工賃合計', pdf_wages, calc_wages, wage_ok))
                if not wage_ok and pdf_wages > 0:
                    _beta_all_ok = False
                # ③ 見積合計（税込or税抜）
                if pdf_grand > 0:
                    grand_label = '見積合計（税込）' if tax_basis_s3 == 'tax_inclusive' else '見積合計（税抜→税込算出）'
                    # 丸め誤差の許容: 明細行数 × 1円 + 基本許容10円（最大50円）
                    _n_items = len(edited_items)
                    _grand_tolerance = min(_n_items + 10, 50)
                    grand_ok = abs(total - pdf_grand) <= _grand_tolerance
                    _verify_items.append((grand_label, pdf_grand, total, grand_ok))
                    if not grand_ok:
                        _beta_all_ok = False

                verify_html = '<table style="width:100%;border-collapse:collapse;font-size:13px;margin:8px 0">'
                verify_html += '<tr style="background:#f1f5f9;font-weight:600"><td style="padding:6px 10px">検証項目</td><td style="padding:6px 10px;text-align:right">PDF記載</td><td style="padding:6px 10px;text-align:right">計算値</td><td style="padding:6px 10px;text-align:center">結果</td></tr>'
                for v_label, v_pdf, v_calc, v_ok in _verify_items:
                    v_icon = '✅' if v_ok else '❌'
                    v_color = '#16a34a' if v_ok else '#dc2626'
                    v_diff = v_calc - v_pdf
                    v_diff_text = f' ({v_diff:+,}円)' if not v_ok and v_pdf > 0 else ''
                    verify_html += f'<tr style="border-bottom:1px solid #e2e8f0"><td style="padding:6px 10px">{v_label}</td><td style="padding:6px 10px;text-align:right">¥{v_pdf:,}</td><td style="padding:6px 10px;text-align:right">¥{v_calc:,}{v_diff_text}</td><td style="padding:6px 10px;text-align:center;color:{v_color};font-weight:600">{v_icon}</td></tr>'

                # ④ 逆算チェック: 合計から逆算して個別金額との不整合チェック
                reverse_ok = False  # ブロック外からの参照に備えて初期化
                if pdf_grand > 0 and (pdf_parts > 0 or pdf_wages > 0):
                    reverse_sub = calc_parts + calc_wages + sp
                    if is_tax_incl_s3:
                        # 税込モード: 金額は既に税込 → 消費税を加算しない
                        reverse_grand = reverse_sub + st.session_state.get('exp_exempt', 0)
                    else:
                        reverse_tax = jpy_round(reverse_sub * TAX_RATE)
                        reverse_grand = reverse_sub + reverse_tax + st.session_state.get('exp_exempt', 0)
                    _rev_tolerance = min(_n_items + 10, 50)
                    reverse_ok = abs(reverse_grand - pdf_grand) <= _rev_tolerance
                    reverse_icon = '✅' if reverse_ok else '⚠️'
                    reverse_color = '#16a34a' if reverse_ok else '#d97706'
                    if not reverse_ok:
                        _beta_all_ok = False
                    verify_html += f'<tr style="border-bottom:1px solid #e2e8f0;background:#fefce8"><td style="padding:6px 10px">逆算検証（税込総額）</td><td style="padding:6px 10px;text-align:right">¥{pdf_grand:,}</td><td style="padding:6px 10px;text-align:right">¥{reverse_grand:,}</td><td style="padding:6px 10px;text-align:center;color:{reverse_color};font-weight:600">{reverse_icon}</td></tr>'

                verify_html += '</table>'
                st.markdown(verify_html, unsafe_allow_html=True)

                # 一致率の算出・表示
                total_checks = len(_verify_items) + (1 if pdf_grand > 0 and (pdf_parts > 0 or pdf_wages > 0) else 0)
                passed_checks = sum(1 for _, _, _, ok in _verify_items if ok)
                if pdf_grand > 0 and (pdf_parts > 0 or pdf_wages > 0) and reverse_ok:
                    passed_checks += 1
                match_rate = (passed_checks / total_checks * 100) if total_checks > 0 else 0
                # 比較相手（見積書に印字された部品計・工賃計・総合計）が
                # 1つも取れていないときは、何も突き合わせていない。
                # CSV取り込みは常にこれに当たるのに「PDF原本と完全一致」と
                # 断言していたため、利用者が原本との突き合わせをここで
                # 打ち切る根拠になっていた。
                # 基準が「全く無い」ときだけ止めるのでは足りない。片側だけ
                # 読めなかった場合、読めなかった側は「検証していない」のに
                # 合格として扱われ、一致率50%と「全項目一致・完全一致」が
                # 同じ一文に並んでいた。全項目に基準があるときだけ断言する。
                _missing = [_n for _n, _v in (('部品計', pdf_parts), ('工賃計', pdf_wages))
                            if _v <= 0]
                _ref_ok = (not _missing) or pdf_grand > 0
                if not _ref_ok:
                    _what = ('・'.join(_missing) if _missing else '照合の基準')
                    _note = ('（CSV取り込みでは常にこの状態です）'
                             if len(_missing) >= 2 else '')
                    st.markdown(
                        '<div class="warning-box" style="padding:10px 16px;margin:8px 0">'
                        f'ℹ️ <b>{_what}を見積書から読み取れていないため、'
                        f'この項目は検証していません</b>{_note}。'
                        'アプリ側で突き合わせる相手がないので、'
                        '下の明細と金額を、原本とご自身で突き合わせてください。</div>',
                        unsafe_allow_html=True)
                elif _beta_all_ok:
                    st.markdown(f'<div class="success-box" style="padding:10px 16px;margin:8px 0">✅ <b>ベタ打ち検証: 全項目一致（一致率 {match_rate:.0f}%）</b> — PDF原本とNEO転記内容が完全一致しています。</div>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<div class="error-box" style="padding:10px 16px;margin:8px 0">⚠️ <b>ベタ打ち検証: 不一致あり（一致率 {match_rate:.0f}%）</b> — PDF原本との差異を確認してください。基準: 99%以上</div>', unsafe_allow_html=True)

            # ── ベタ打ちモード専用: 部品・工賃区分確認パネル ──────────────────────────
            _classification_alerts = []
            _classification_confirmed = True
            _error_alerts = []
            if _step3_mode == 'beta':
                # アイテムが変わった時だけ再計算（session_stateでキャッシュ）
                _items_hash = hash(str([(it.get('name',''), it.get('parts_amount',0), it.get('wage',0)) for it in edited_items]))
                if st.session_state.get('_cls_hash') != _items_hash:
                    st.session_state['_cls_cache'] = check_parts_labor_classification(edited_items)
                    st.session_state['_cls_hash']  = _items_hash
                _classification_alerts = st.session_state.get('_cls_cache', [])
                _error_alerts   = [a for a in _classification_alerts if a['severity'] == 'error']
                _warning_alerts = [a for a in _classification_alerts if a['severity'] == 'warning']

                if _classification_alerts:
                    st.markdown(
                        '<div class="section-title">🔍 部品・工賃区分確認（NEO転記前の必須チェック）</div>',
                        unsafe_allow_html=True
                    )
                    # エラー（要確認）
                    if _error_alerts:
                        st.markdown(
                            f'<div class="error-box" style="padding:10px 16px;margin:6px 0">'
                            f'🚨 <b>要確認: 部品/工賃の区分に疑わしい行が {len(_error_alerts)} 件あります</b><br>'
                            f'以下の行を確認し、正しい列に金額が入っているかを確認してください。'
                            f'</div>',
                            unsafe_allow_html=True
                        )
                        for a in _error_alerts:
                            st.markdown(
                                f'<div style="background:#fef2f2;border-left:4px solid #dc2626;padding:8px 12px;margin:4px 0;font-size:13px">'
                                f'🔴 <b>行{a["row_no"]}「{esc_html(a["name"])}」</b>: '
                                f'部品¥{a["parts_amount"]:,} / 工賃¥{a["wage"]:,}<br>'
                                f'⚠️ {esc_html(a["message"])}'
                                f'</div>',
                                unsafe_allow_html=True
                            )
                    # 警告（参考情報）
                    if _warning_alerts:
                        with st.expander(f"⚠️ 参考警告 ({len(_warning_alerts)} 件) — 要確認の可能性あり", expanded=False):
                            for a in _warning_alerts:
                                st.markdown(
                                    f'<div style="background:#fffbeb;border-left:4px solid #d97706;padding:8px 12px;margin:4px 0;font-size:13px">'
                                    f'🟡 <b>行{a["row_no"]}「{esc_html(a["name"])}」</b>: '
                                    f'部品¥{a["parts_amount"]:,} / 工賃¥{a["wage"]:,}<br>'
                                    f'{esc_html(a["message"])}'
                                    f'</div>',
                                    unsafe_allow_html=True
                                )

                    # 確認チェックボックス（エラーがある場合のみ）
                    if _error_alerts:
                        _classification_confirmed = st.checkbox(
                            "⬆️ 上記の部品/工賃区分を確認しました。この内容でNEOファイルに転記します。",
                            value=False,
                            key='classification_confirmed'
                        )
                        if not _classification_confirmed:
                            st.info(
                                "💡 明細を修正するには、上の「✏️ 明細行を修正」エリアで各行の部品金額・工賃を直接編集できます。"
                                "区分が正しければチェックを入れてNEO生成に進んでください。"
                            )
                    else:
                        _classification_confirmed = True
                        st.markdown(
                            '<div class="success-box" style="padding:10px 16px;margin:8px 0">'
                            '✅ <b>部品・工賃区分チェック: 参考警告のみ</b> — 重大な区分エラーは検出されませんでした。</div>',
                            unsafe_allow_html=True
                        )
                else:
                    st.markdown(
                        '<div class="success-box" style="padding:10px 16px;margin:8px 0">'
                        '✅ <b>部品・工賃区分チェック: 問題なし</b> — 全明細行の区分が正常です。</div>',
                        unsafe_allow_html=True
                    )

            # セッションに保存（STEP4で参照）
            st.session_state['classification_alerts'] = _classification_alerts
            # classification_confirmed: 別キーで管理（ウィジェットキーとの衝突回避）
            if not _error_alerts:
                st.session_state['_cls_confirmed_value'] = _classification_confirmed
            else:
                st.session_state['_cls_confirmed_value'] = st.session_state.get('classification_confirmed', False)

            # マスタ連携の差額計算とレポート表示（DBモード時のみ）
            discrepancies = []
            total_diff = 0
            if _step3_mode == 'beta':
                pass  # ベタ打ちモードではマスタ差額レポートをスキップ
            for item in edited_items if _step3_mode != 'beta' else []:
                m_level = item.get('_match_level', 0)
                ocr_price = item.get('_original_parts_amount', 0)
                master_price = item.get('_master_price', 0)
                is_reverse = item.get('_reverse_match', False)
                qty = item.get('quantity', 1)

                # マスタ単価が取れていない行は比較できない。
                # '_master_price' はリポジトリ内のどこからも実値が入らないため、
                # この条件を外すと全部品行が「差額あり」として並び、
                # 「総額変動 -（部品総額）」という嘘の合計が出ていた。
                # 常時100%誤報だと、本物の価格相違に気づけなくなる。
                if master_price <= 0:
                    continue
                # Check discrepancy if NOT reverse matched
                if not is_reverse and ocr_price > 0 and (m_level >= 4 or m_level == 0 or ocr_price != master_price):
                    d = (master_price - ocr_price) * qty
                    discrepancies.append(item)
                    total_diff += d

            if discrepancies:
                st.markdown('<div class="warning-box">⚠️ <b>マスタ適用による金額差分レポート</b><br>OCRで読み取った金額と、Addataマスタ側の定価にズレがある部品が検出されました。</div>', unsafe_allow_html=True)
                diff_data = []
                for idx, d in enumerate(discrepancies):
                    orig_p = d.get('_original_parts_amount', 0)
                    mast_p = d.get('_master_price', 0)
                    qty = d.get('quantity', 1)
                    diff = (mast_p - orig_p) * qty

                    code_r = str(d.get('_master_part_code_r', '')).strip()
                    code_l = str(d.get('_master_part_code_l', '')).strip()
                    p_code = f"R:{code_r} / L:{code_l}" if (code_r and code_l) else (code_r or code_l or '')

                    diff_data.append({
                        'No': idx + 1,
                        'OCR 品名': d.get('_original_name', ''),
                        'OCR 単価': f"¥{orig_p:,}",
                        'マスタ品名': d.get('_master_name', ''),
                        '部品コード': p_code,
                        '枝番': d.get('_master_branch_code', ''),
                        '修理': d.get('_master_repair_code', ''),
                        'マスタ単価': f"¥{mast_p:,}",
                        '数量': qty,
                        '差額小計': f"{diff:+,}円"
                    })
                st.table(pd.DataFrame(diff_data).set_index('No'))
                if total_diff > 0:
                    st.markdown(f'<div style="color: blue; font-weight: bold;">マスタ適用による総額変動: +{total_diff:,}円</div>', unsafe_allow_html=True)
                elif total_diff < 0:
                    st.markdown(f'<div style="color: red; font-weight: bold;">マスタ適用による総額変動: {total_diff:,}円</div>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<div style="font-weight: bold;">マスタ適用による総額変動: なし (0円)</div>', unsafe_allow_html=True)

            DISCREPANCY_THRESHOLD = 1000
            if has_discrepancy and (abs(parts_diff) >= DISCREPANCY_THRESHOLD or abs(wage_diff) >= DISCREPANCY_THRESHOLD):
                st.markdown(
                    '<div class="mismatch-banner">'
                    '<div class="mismatch-title">🚨 金額不一致警告</div>'
                    '<div class="mismatch-body">AI読み取り金額とPDF記載金額に大きな差があります。<br>'
                    '明細行の内容を確認・修正してから生成してください。</div>'
                    '</div>',
                    unsafe_allow_html=True
                )
                amount_confirmed = st.checkbox(
                    "金額の差異を確認しました。このまま生成を続行します。",
                    value=False,
                    key='amount_confirmed'
                )
            else:
                amount_confirmed = True

            # ── Total strip ──
            # sp は初期化のまま0で使われていたため、画面の合計だけ
            # ショートパーツぶん少なく表示されていた（short_parts_wage の
            # 定義はこの後なので estimate_data から直接読む）
            sp = safe_int((estimate_data or {}).get('short_parts_wage', 0)) or sp
            _exp_tow_s4 = st.session_state.get('exp_towing', 0)
            _exp_ren_s4 = st.session_state.get('exp_rental', 0)
            sub   = calc_parts + calc_wages + sp + _exp_tow_s4 + _exp_ren_s4
            _is_tax_incl_strip = (estimate_data.get('_is_tax_inclusive', False) if estimate_data else False)
            if _is_tax_incl_strip:
                # 費用欄は「税抜」入力なので、税込モードでも費用ぶんの税は加算する
                tax   = jpy_round((sp + _exp_tow_s4 + _exp_ren_s4) * TAX_RATE)
                total = sub + tax + st.session_state.get('exp_exempt', 0)
            else:
                tax   = jpy_round(sub * TAX_RATE)
                total = sub + tax + st.session_state.get('exp_exempt', 0)
            # 費用（レッカー・代車・非課税）は合計に加算されるのに画面に
            # 出ていなかったため、部品代＋工賃＋消費税と合計が一致せず
            # 「計算が合っていない」ように見えていた。金額がある時だけ表示する。
            _exp_sum_strip = (st.session_state.get('exp_towing', 0)
                              + st.session_state.get('exp_rental', 0)
                              + st.session_state.get('exp_exempt', 0))
            _exp_cell = (
                '<div class="total-sep">+</div>'
                '<div class="total-item">'
                '<div class="total-label">費用</div>'
                f'<div class="total-value">¥{_exp_sum_strip:,}</div>'
                '</div>'
            ) if _exp_sum_strip else ''
            _sp_cell = (
                '<div class="total-sep">+</div>'
                '<div class="total-item">'
                '<div class="total-label">ショートパーツ</div>'
                f'<div class="total-value">¥{sp:,}</div>'
                '</div>'
            ) if sp else ''
            # ショートパーツや費用が0のとき {_sp_cell} が空文字になり、
            # 「空白だけの行」ができる。Markdown はそこでHTMLブロックを
            # 終わらせるため、以降が字下げコードブロックとして生の
            # タグのまま表示されてしまう。改行を挟まない1本の文字列にする。
            _tax_label   = '消費税（税込済）' if _is_tax_incl_strip else '消費税'
            _tax_value   = '—' if _is_tax_incl_strip else f'¥{tax:,}'
            _total_label = '合計' if _is_tax_incl_strip else '合計（税込）'
            st.markdown(
                '<div class="total-strip">'
                '<div class="total-item">'
                '<div class="total-label">部品代</div>'
                f'<div class="total-value">¥{calc_parts:,}</div>'
                '</div>'
                '<div class="total-sep">+</div>'
                '<div class="total-item">'
                '<div class="total-label">工賃</div>'
                f'<div class="total-value">¥{calc_wages:,}</div>'
                '</div>'
                f'{_sp_cell}{_exp_cell}'
                '<div class="total-sep">+</div>'
                '<div class="total-item">'
                f'<div class="total-label">{_tax_label}</div>'
                f'<div class="total-value">{_tax_value}</div>'
                '</div>'
                '<div class="total-sep">=</div>'
                '<div class="total-item">'
                f'<div class="total-label">{_total_label}</div>'
                f'<div class="total-value-highlight">¥{total:,}</div>'
                '</div>'
                '</div>',
                unsafe_allow_html=True)
          else:
            amount_confirmed = True
            st.info("💡 見積書なし — 車両情報のみのNEOファイルを作成します")

        if 'amount_confirmed' not in locals():
            amount_confirmed = True

        # ── 部品/工賃区分確認チェックの取得（ベタ打ちモード）──────────────────
        _cls_confirmed   = st.session_state.get('_cls_confirmed_value', True)
        _cls_alerts      = st.session_state.get('classification_alerts', [])
        _cls_errors      = [a for a in _cls_alerts if a['severity'] == 'error']
        # ベタ打ちモード以外は常にOK
        if _step3_mode != 'beta':
            _cls_confirmed = True

        # NEO生成ボタン
        st.markdown("---")

        # 区分エラーがあって未確認の場合、警告を表示（ただし生成はブロックしない）
        if _cls_errors and not _cls_confirmed and _step3_mode == 'beta':
            st.markdown(
                '<div style="background:#fffbeb;border:1px solid #d97706;border-radius:8px;padding:10px 14px;margin-bottom:10px;font-size:13px">'
                f'⚠️ <b>部品・工賃区分に{len(_cls_errors)}件の注意事項があります</b>（上部の「🔍 部品・工賃区分確認」で確認可能）<br>'
                'そのまま生成することもできます。'
                '</div>',
                unsafe_allow_html=True
            )

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← ステップ①に戻る", use_container_width=True):
                st.session_state['step'] = 1
                st.session_state['vehicle_data']  = None
                st.session_state['estimate_data'] = None
                st.rerun()
        with bcol2:
            # 金額差異未確認時のみボタンを無効化（分類エラーではブロックしない）
            gen_disabled = not amount_confirmed
            if st.button("📦 NEOファイルを生成する →", type="primary", use_container_width=True, disabled=gen_disabled):
                st.session_state['updated_vehicle'] = updated_vehicle
                st.session_state['calc_parts']      = calc_parts
                st.session_state['calc_wages']      = calc_wages
                st.session_state['pdf_parts']       = pdf_parts if estimate_data else None
                st.session_state['pdf_wages']       = pdf_wages if estimate_data else None
                st.session_state['discrepancies']   = discrepancies
                st.session_state['total_diff']      = total_diff
                st.session_state['step'] = 4
                st.rerun()
            if not amount_confirmed:
                st.caption("⬆️ 金額差異を確認してチェックを入れてください")

    # =========================================
    # STEP 4: NEO生成・ダウンロード
    # =========================================
    elif current_step == 4:
        updated_vehicle = st.session_state.get('updated_vehicle', {})
        estimate_data   = st.session_state.get('estimate_data')
        calc_parts      = st.session_state.get('calc_parts', 0)
        calc_wages      = st.session_state.get('calc_wages', 0)
        pdf_parts       = st.session_state.get('pdf_parts')
        pdf_wages       = st.session_state.get('pdf_wages')
        insurance_info  = {
            'policy_no':        st.session_state.get('policy_no', ''),
            'contractor_name':  st.session_state.get('contractor_name', ''),
            'accept_no':        st.session_state.get('accept_no', ''),
            'accident_date':    st.session_state.get('accident_date', ''),
            'agency_name':      st.session_state.get('agency_name', ''),
            'adjuster_name':    st.session_state.get('adjuster_name', ''),
            'garage_in_date':   st.session_state.get('garage_in_date', ''),
            'garage_out_date':  st.session_state.get('garage_out_date', ''),
            'repair_days':      st.session_state.get('repair_days', 0),
            'note1':            st.session_state.get('note1', ''),
        }
        expense_info = {
            'towing':      st.session_state.get('exp_towing', 0),
            'rental_car':  st.session_state.get('exp_rental', 0),
            'tax_exempt':  st.session_state.get('exp_exempt', 0),
        }
        items            = []
        short_parts_wage = 0
        has_estimate     = False
        reverse_match    = False
        if estimate_data and estimate_data.get('items'):
            items            = estimate_data['items']
            short_parts_wage = safe_int(estimate_data.get('short_parts_wage', 0))
            has_estimate     = True
            reverse_match    = estimate_data.get('_reverse_match', False)

        progress = st.progress(0, text="NEOファイルを生成中...")
        _neo_wait = st.info("📦 NEOファイルを生成しています。しばらくお待ちください...")
        try:
            progress.progress(20, text="📦 テンプレートを読み込み中...")
            is_tax_inclusive = estimate_data.get('_is_tax_inclusive', False) if estimate_data else False
            _step4_beta = st.session_state.get('selected_mode', 'db') == 'beta'
            # カスタムテンプレートNEOが指定されている場合はそちらを使用
            # session_stateに永続化したバイト列を優先使用（file_uploaderはステップ遷移でクリアされるため）
            _custom_neo_bytes = st.session_state.get('custom_neo_bytes')
            if not _custom_neo_bytes:
                # フォールバック: file_uploaderが同一セッション内でまだ生きている場合
                _fallback_neo = st.session_state.get('custom_neo_upload')
                if _fallback_neo:
                    _custom_neo_bytes = _fallback_neo.read()
                    _fallback_neo.seek(0)
            _use_custom_neo   = _custom_neo_bytes is not None
            _active_template  = _custom_neo_bytes if _use_custom_neo else template_data
            if _use_custom_neo:
                _custom_name = st.session_state.get('custom_neo_name', 'カスタムNEO')
                progress.progress(35, text=f"📁 テンプレートNEO ({_custom_name}) を読み込み中...")
            progress.progress(50, text="⚙️ 明細データをNEOに書き込み中...")
            neo_data, total_parts, total_wages, grand_total = generate_neo_file(
                _active_template, updated_vehicle, items, short_parts_wage, insurance_info,
                expenses=expense_info, is_tax_inclusive=is_tax_inclusive, is_beta_mode=_step4_beta,
                merge_mode=_use_custom_neo
            )
            progress.progress(85, text="📝 ファイル名を生成中...")
            filename = generate_filename(
                updated_vehicle, calc_parts, calc_wages, pdf_parts, pdf_wages,
                has_estimate, reverse_match, short_parts_wage
            )
            st.session_state['neo_bytes']    = neo_data
            st.session_state['neo_filename'] = filename
            progress.progress(100, text="✅ 生成完了！")
            _neo_wait.empty()  # 待機メッセージを消去

            # 不一致チェック
            _discrepancies_step4 = []
            _total_diff_step4 = 0
            if has_estimate and not reverse_match:
                sp_val = safe_int(short_parts_wage)
                parts_match_s4 = (calc_parts == pdf_parts) or (calc_parts + sp_val == pdf_parts)
                if pdf_parts is not None and pdf_parts > 0 and not parts_match_s4:
                    _discrepancies_step4.append(f"部品相違（PDF: ¥{pdf_parts:,} / 計算: ¥{calc_parts:,}）")
                    _total_diff_step4 += calc_parts - pdf_parts
                if pdf_wages is not None and pdf_wages > 0 and calc_wages != pdf_wages:
                    _discrepancies_step4.append(f"工賃相違（PDF: ¥{pdf_wages:,} / 計算: ¥{calc_wages:,}）")
                    _total_diff_step4 += calc_wages - pdf_wages

            if _discrepancies_step4:
                diff_abs = abs(_total_diff_step4)
                st.markdown(f"""
                <div class="mismatch-banner">
                    <div class="mismatch-title">⚠️ 金額不一致が検出されました（差額 ▲¥{diff_abs:,}）</div>
                    <div class="mismatch-body">
                        元見積の合計額と本システムの算出額が一致しません。<br>
                        NEOファイルを生成する前に不一致レポートを確認・保存することを推奨します。
                    </div>
                </div>
                """, unsafe_allow_html=True)
                with st.expander("📄 不一致レポートを確認", expanded=False):
                    for d_item in _discrepancies_step4:
                        st.warning(d_item)

            # ── ベタ打ちモード: 部品・工賃区分確認済みサマリー表示 ──
            if _step4_beta:
                _cls_alerts_s4 = st.session_state.get('classification_alerts', [])
                _cls_errors_s4 = [a for a in _cls_alerts_s4 if a['severity'] == 'error']
                _cls_warnings_s4 = [a for a in _cls_alerts_s4 if a['severity'] == 'warning']
                if _cls_errors_s4:
                    st.markdown(
                        f'<div style="background:#fef9c3;border:1px solid #ca8a04;border-radius:6px;padding:10px 14px;margin:8px 0;font-size:13px">'
                        f'✅ <b>部品・工賃区分確認済み</b> — {len(_cls_errors_s4)} 件の要確認項目が確認・承認された上でNEOを生成しました。<br>'
                        + ''.join(f'<div style="margin-top:4px">⚠️ 行{a["row_no"]}「{esc_html(a["name"])}」: 部品¥{a["parts_amount"]:,} / 工賃¥{a["wage"]:,}</div>' for a in _cls_errors_s4)
                        + '</div>',
                        unsafe_allow_html=True
                    )
                elif _cls_warnings_s4:
                    st.markdown(
                        f'<div style="background:#f0fdf4;border:1px solid #16a34a;border-radius:6px;padding:10px 14px;margin:8px 0;font-size:13px">'
                        f'✅ <b>部品・工賃区分チェック: 問題なし</b> — 参考警告 {len(_cls_warnings_s4)} 件のみ（重大エラーなし）</div>',
                        unsafe_allow_html=True
                    )
                else:
                    st.markdown(
                        '<div style="background:#f0fdf4;border:1px solid #16a34a;border-radius:6px;padding:10px 14px;margin:8px 0;font-size:13px">'
                        '✅ <b>部品・工賃区分チェック: 問題なし</b></div>',
                        unsafe_allow_html=True
                    )

            # ── ベタ打ちモード: PDF原本との差異特定レポート ──
            if _step4_beta and _discrepancies_step4:
                st.markdown('<div class="section-title">📋 ベタ打ちモード — 差異特定レポート</div>', unsafe_allow_html=True)
                st.markdown('金額の不一致箇所を特定するための詳細レポートをPDF形式でダウンロードできます。')
                # Step4はStep3とは別ブランチのため、wage_match_sp/spをStep4内で再計算する
                _sp_s4 = safe_int(short_parts_wage)
                _pdf_wages_s4 = safe_int(pdf_wages or 0)
                _wage_match_sp_s4 = (_pdf_wages_s4 > 0 and calc_wages + _sp_s4 == _pdf_wages_s4)
                # ショートパーツはHonda Cars等で工賃列に含まれるため、差異レポートの計算値に加算
                _calc_wages_report = calc_wages + _sp_s4 if _wage_match_sp_s4 else calc_wages
                beta_pdf_bytes = generate_beta_discrepancy_report_pdf(estimate_data, calc_parts, _calc_wages_report, pdf_parts or 0, pdf_wages or 0, updated_vehicle)
                st.download_button(
                    label="📄 差異レポートをダウンロード(PDF)",
                    data=beta_pdf_bytes,
                    file_name=filename.replace('.neo', '_ベタ打ち差異レポート.pdf'),
                    mime="application/pdf",
                    use_container_width=True,
                    type="primary",
                    key='beta_diff_report_dl'
                )

            st.markdown('<div class="alert alert-success">✅ NEOファイルの生成が完了しました！コグニセブンで開いて内容を確認してください。</div>', unsafe_allow_html=True)

            # 生成内容サマリー
            summary_items = [
                ("ファイル名", f"`{filename}`"),
                ("ファイルサイズ", f"{len(neo_data):,} bytes"),
            ]
            if has_estimate:
                summary_items += [
                    ("明細行数", f"{len(items)} 行"),
                    ("合計金額", f"¥{grand_total:,}（税込）"),
                ]
                if reverse_match:
                    summary_items.append(("金額検証", "✅ 逆算一致"))
            else:
                summary_items.append(("内容", "車両情報のみ（明細なし）"))
            for k, v in summary_items:
                st.markdown(f"**{k}:** {v}")

            st.markdown("---")
            dcol1, dcol2 = st.columns(2)
            with dcol1:
                st.download_button(
                    label="📥 NEOファイルをダウンロード",
                    data=neo_data,
                    file_name=filename,
                    mime="application/octet-stream",
                    type="primary",
                    use_container_width=True
                )
            with dcol2:
                discrepancies_list = st.session_state.get('discrepancies', [])
                total_diff_val = st.session_state.get('total_diff', 0)
                if discrepancies_list:
                    pdf_bytes = generate_discrepancy_report_pdf(discrepancies_list, total_diff_val, updated_vehicle)
                    pdf_filename = filename.replace('.neo', '_差額レポート.pdf')
                    st.download_button(
                        label="📄 差額レポートのダウンロード(PDF)",
                        data=pdf_bytes,
                        file_name=pdf_filename,
                        mime="application/pdf",
                        use_container_width=True
                    )
                else:
                    st.button("📄 差額なし (PDF生成不要)", disabled=True, use_container_width=True)
            
            st.markdown("")
            if st.button("🔄 新しい見積を作成する", use_container_width=True):
                for key in [
                    'step', 'vehicle_data', 'estimate_data', 'neo_bytes', 'neo_filename',
                    'vehicle_file_bytes', 'vehicle_file_name', 'estimate_file_bytes',
                    'estimate_file_name', 'updated_vehicle', 'calc_parts', 'calc_wages',
                    'pdf_parts', 'pdf_wages',
                    # 事故・保険情報
                    'policy_no', 'contractor_name', 'accept_no', 'accident_date',
                    'agency_name', 'adjuster_name', 'garage_in_date', 'garage_out_date',
                    'repair_days', 'note1',
                    'exp_towing', 'exp_rental', 'exp_exempt',
                    'custom_neo_bytes', 'custom_neo_name',
                    'tax_override',
                    # PDF側の税区分と、その引き継ぎ用の一時キー。消し忘れると
                    # 次の見積で意図しない税区分が復活し、税抜の見積が
                    # 税込として処理される。
                    '_step2_msgs',
                    '_tax_carry_pending', 'pdf_tax_override',
                    'pdf2neo_tax_inclusive', 'csv_tax_radio', 'pdf_tax_radio',
                    'classification_confirmed', 'classification_alerts',
                    'discrepancies', 'total_diff',
                    'amount_confirmed',
                    # CSV取り込み関連
                    'csv_mode', 'csv_items', '_csv_paste_saved',
                    # PDF→NEO変換関連
                    'pdf2neo_result', 'pdf2neo_vehicle_info',
                    # その他の残留データ
                    'use_fax_filter', 'use_rasterize', 'use_enhance', 'selected_model',
                    'short_parts_wage',
                ]:
                    if key in st.session_state:
                        del st.session_state[key]
                # サイドバー入力のウィジェットを作り直して確実に空にする
                st.session_state['form_seq'] = st.session_state.get('form_seq', 0) + 1
                st.session_state['step'] = 1
                st.rerun()
        except Exception as e:
            progress.empty()
            try:
                _neo_wait.empty()  # 待機メッセージが残り続けるのを防ぐ
            except Exception:
                pass
            st.error(f"⚠️ NEO生成中にエラーが発生しました:\n\n{str(e)}")
            print("[NEO生成エラー]", traceback.format_exc())
            if st.button("← ステップ③に戻る"):
                st.session_state['step'] = 3
                st.rerun()


if __name__ == '__main__':
    main()
