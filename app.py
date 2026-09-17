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


# 読み込みを始めたときのコードの指紋（ファイルの最後で読み直し、同じ中身のときだけ __app_src_digest__ に控える。読み込みの途中で
# push されたら控えず、古い扱いにして読み直させる。レビュー 3 周目）
try:
    import hashlib as _stamp_hashlib0
    with open(__file__, 'rb') as _stamp_f0:
        _stamp_digest_at_start = _stamp_hashlib0.sha256(_stamp_f0.read()).hexdigest()
    del _stamp_hashlib0, _stamp_f0
except Exception:  # noqa: BLE001
    _stamp_digest_at_start = None

from dotenv import load_dotenv
load_dotenv()
from neo_skill import doc_hints as _doc_hints  # noqa: E402  車検証・事故/保険の書類の OCR 結果を hint に写す（2026-09-14）

import streamlit as st
import struct
import uuid as _uuid
import zlib
import sqlite3
import tempfile
import time
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
import hashlib
import hmac
import contextlib
from concurrent.futures import ThreadPoolExecutor
# コグニセブンの「既存見積」一覧が読む先頭424Bの管理領域を書くために使う
import neo_header
# 見積の読み取り規則のうち、3つの入口（CSV取り込み・PDF直接変換・Addata照合）で
# 同じでなければならないもの。片方だけ直す取り残しを二度出したので1か所にまとめた。
import neo_rules

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
# 実機の元号設定ファイル（Auda7/AudaData/Const/AnEra.ini）の [EraValue] が
# 西暦=1 令和=4 平成=3 昭和=2 と定めている。実機の .neo 202件でも
# Era=4 は 2020〜2023年、Era=3 は 2009〜2018年で、この対応で一致した。
# 以前は明治を1として1つずつ後ろにずらしており、令和の車が 5 として
# 書かれていた。コグニセブンに 5 という元号は無く、初度登録が化ける。
# コグニセブンに明治・大正は無いので、来たら空欄（元号なし）に落とす。
_ERA_CODE = {'西暦': '1', '昭和': '2', '平成': '3', '令和': '4'}


def normalized_reg_date(raw) -> str:
    """初度登録年月を YYYYMM00 に正規化する。元号を決められないものは '00000000'。

    コグニセブンが扱える元号は昭和・平成・令和だけ（AnEra.ini）。
    大正以前や月が 00 の値をそのまま通すと、見積本体DBには 19260100 が入り、
    ヘッダXMLは空欄になってテンプレートの値が残るため、同じ .neo の中に
    初度登録が2通り入る。ここで1か所に決めて、DB・XML の両方から使う。
    """
    d = _normalize_ym8(raw) or '00000000'
    if d == '00000000':
        return '00000000'
    era, era_year = get_era_info(d)
    if era_year == '0000' or era not in _ERA_CODE or d[4:6] == '00':
        return '00000000'
    return d


def best_intax_for(intax_total):
    """税抜＋消費税（10%の四捨五入）で表せる、いちばん近い税込総額を返す。

    **これは「原本と1円ずれてもよい」という意味ではない。**
    以前ここには「コグニセブンは税抜で保存して消費税を計算し直すので
    .neo の総額は必ず S + round(S*0.1) になる」と書いてあったが、
    **実機が作った .neo 176件を調べたところ誤りだった**。
    10件（約6%）で税額が税抜の10%ちょうどでなく（例: 税抜295,455 に対し
    税額29,545。10%なら29,546）、コグニは書かれた税額をそのまま持っている。

    そのため生成側は、税込表記のとき税額を「原本の税込 − 逆算した税抜」で
    書き、**総額を原本にぴったり合わせている**（`_update_ansmb_body`）。
    この関数は「10%ちょうどで表すとどうなるか」を知りたい場面
    （画面の目安表示）にだけ使う。金額の正解として使ってはいけない。
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
# 見積書を読む Claude API（pdf-to-neo スキル経路）。Gemini は CSV 取り込み・車検証 OCR で使う
try:
    ANTHROPIC_API_KEY = st.secrets.get('ANTHROPIC_API_KEY', os.environ.get('ANTHROPIC_API_KEY', ''))
except Exception:
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
# 合言葉（任意）: st.secrets / 環境変数 APP_PASSCODE があるときだけ入場を求める（公開 URL で所有者の API キーが無制限に使われないように。バグハント J4）
try:
    APP_PASSCODE = str(st.secrets.get('APP_PASSCODE', os.environ.get('APP_PASSCODE', '')) or '')
except Exception:
    APP_PASSCODE = str(os.environ.get('APP_PASSCODE', '') or '')
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
    """再実行をまたいで保持されるストアを返す（session_state が使えない場合はモジュール変数）。

    画面の外（ベタ打ちの明細解析の worker スレッド）では session_state を使わない。Streamlit 1.63 はそこで
    プロセスに 1 つの代用品を返すので、書いた記録が本人のセッションには届かず、別のセッションの worker から
    読めていた（バグハント 3 回目 N6/P18）。worker の記録はモジュール変数に置き、画面側は読むときに合わせる"""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx as _grc
        if _grc(suppress_warning=True) is None:
            return _FALLBACK_STORE
    except Exception:
        pass
    try:
        store = st.session_state.setdefault('_gemini_model_store', {})
        if isinstance(store, dict):
            return store
    except Exception:
        pass
    return _FALLBACK_STORE


def _quota_exhausted_set() -> set:
    """クォータ超過で利用不可になったモデルの集合（worker スレッドで記録したぶんも合わせる。N6）"""
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


def _is_quota_error(err_msg: str) -> bool:
    """クォータ超過（429 RESOURCE_EXHAUSTED）かどうか。

    同じ条件が3箇所に散らばっていたのでここにまとめた。
    このエラーは待っても同じモデルでは通らず、投げ直すこと自体が
    さらにクォータを食うので、リトライしてはいけない。
    """
    m = str(err_msg or '')
    return ('429' in m) or ('RESOURCE_EXHAUSTED' in m) or ('クォータが上限' in m)


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
        return jpy_round(val)   # .5 は四捨五入（round() の偶数丸めだと '1234.5' が 1234。バグハント 3 回目 O12）
    s = _normalize_number_text(str(val))
    if s is None:
        return default
    try:
        f = float(s)
    except (ValueError, OverflowError):
        return default
    if f != f or f in (float('inf'), float('-inf')):
        return default
    return jpy_round(s)


def qty_int(v, default: int = 1) -> int:
    """明細の数量を整数に。整数でない数量（2.5 L など）は 1（金額はその行のまま。コグニの数量欄は整数。L7）"""
    return 1 if is_fractional_qty(v) else safe_int(v, default)


def is_fractional_qty(v) -> bool:
    """数量が整数でない（'2.5'・1.5・'0.5L'）か。'1.00'・'3個'・空は整数扱い（バグハント 3 回目 L7）"""
    if v is None or isinstance(v, (bool, int)):
        return False
    if isinstance(v, float):
        return v == v and v not in (float('inf'), float('-inf')) and v != int(v)
    s = _normalize_number_text(str(v))
    if s is None:
        return False
    try:
        f = float(s)
    except (ValueError, OverflowError):
        return False
    return math.isfinite(f) and f != int(f)


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


def _num_code(value, width: int) -> str:
    """類別区分番号（4 桁）・型式指定番号（5 桁）を実機 NEO の形に: 数字だけにして左を 0 で埋める（'2' → '0002'）。
    数字が無ければ空。実機 299 本: CarKindNo は全て 4 桁、CarMouldNo は全て 5 桁（2026-09-15 集計。Codex hunt B8）"""
    s = re.sub(r'\D', '', unicodedata.normalize('NFKC', _strip_control_chars(value)))
    return s.zfill(width) if s else ''


def _reg_no_part(value, digits: bool = False, strip_hyphen: bool = False, kana: bool = False) -> str:
    """登録番号の 1 区画（地名・分類番号・かな・一連番号）をコグニの NEO と同じ形に揃える。

    実機 NEO 108 本（登録番号あり）の集計（2026-09-14）: 分類番号・一連番号は
    すべて半角数字、かなは全角ひらがな、一連番号にハイフンや「・」は無い。
    旧来の車検証 OCR（analyze_vehicle_registration）は全角数字（"３４６"）で返して
    いたので、DB・ヘッダ XML・INI に書く直前でここを通して半角にする。
    地名・かなは幅を変えない（NFKC は半角カナを全角にしてしまう）。
    """
    s = _strip_control_chars(value).strip()
    if digits:
        s = unicodedata.normalize('NFKC', s)
    if kana:
        # かなは全角ひらがな（実機 108 本すべて）。OCR の半角カナ 'ｱ'・カタカナ 'ア' を 'あ' に（Codex hunt B5）
        s = unicodedata.normalize('NFKC', s)
        s = ''.join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in s)
    if strip_hyphen:
        s = re.sub(r'[\s\-‐‑―ー・･.]', '', s)   # '12-34' → '1234'、'・・12' → '12'
    return s


def policy_no_or_accept(ins, existing: str = '', merge_mode: bool = False) -> str:
    """証券番号の欄（Insurance.PolicyNo / ヘッダ XML の TicketNo）に入れる値。
    読めた証券番号が無ければ、事故番号・受付番号をそこにも入れる（2026-09-16 亮平さん指示: 認識した事故番号 OR 受付番号は
    必ず NEO の証券番号の欄に出す。速報報告書に証券番号が印字されない案件が多い）。受付番号の欄（FileInfo.AcceptNo）は別に残る。
    ただし**マージモード**（カスタムのテンプレート NEO の値を残す経路）で、テンプレートに証券番号があるなら空を返して残す
    ＝ 本物の証券番号を事故番号で塗り替えない。スキル経路は draft_estimate.Drafter._insurance が同じ規則"""
    d = ins or {}
    pol = safe_str(d.get('policy_no', '')).strip()
    if pol:
        return pol
    acc = safe_str(d.get('accept_no', '')).strip()
    if acc and merge_mode and safe_str(existing).strip():
        return ''
    return acc


def cp932_trim(value, max_bytes: int) -> str:
    """コグニセブンの列幅（CP932のバイト数）に収まるよう切り詰める。

    日本語は1文字2バイトなので、文字数で切ると宣言幅の2倍入ってしまう。
    多バイト文字の途中で切れないよう、デコードできる位置まで戻す。
    """
    s = str(value if value is not None else '')
    if not s:
        return ''
    # 符号化は cp932w（Windows と同じ IBM 拡張漢字）。「﨑」「德」「髙」は
    # Python の cp932 だと ED/EE 行、Windows は FA〜FC 行に書く。
    # 実機の .neo 202件を調べたところ ED/EE 行は1箇所も無く、FA〜FC 行だけだった。
    # バイト数は同じ2バイトなので、切り詰めの幅計算は変わらない。
    b = neo_header.encode_cp932w(s)[:max_bytes]
    while b:
        try:
            return b.decode('cp932')
        except UnicodeDecodeError:
            b = b[:-1]
    return ''


def _round_tax10(sub: int, mode: str = '四捨五入') -> int:
    """請求書単位の消費税（10%）。mode は '四捨五入'（既定）/ '切り捨て' / '切り上げ'（vendor と同じ整数の計算）"""
    sub = int(sub)
    if mode == '切り捨て':
        return (sub * 10) // 100
    if mode == '切り上げ':
        return -((-sub * 10) // 100)
    return jpy_round(sub * TAX_RATE)


_TAX_ARRANGE = {1: '四捨五入', 2: '切り捨て', 3: '切り上げ'}


def _template_tax_round(em_db_bytes) -> tuple:
    """テンプレートの顧客・設定 DB（このアプリのキーでは 'AnSvEm0001Ex.db'）の Setting.tx_ArrangeFlag → (端数処理, 旗)。
    読めなければ ('四捨五入', 1)"""
    try:
        c = sqlite3.connect(':memory:')
        try:
            c.deserialize(em_db_bytes)
            r = c.execute('SELECT tx_ArrangeFlag FROM Setting').fetchone()
        finally:
            c.close()
        f = safe_int(r[0], 1) if r else 1
        return (_TAX_ARRANGE.get(f, '四捨五入'), f if f in _TAX_ARRANGE else 1)
    except Exception:
        return ('四捨五入', 1)


import functools as _functools


@_functools.lru_cache(maxsize=8)
def _neo_tax_round(neo_bytes: bytes) -> tuple:
    """NEO（テンプレート）の消費税の端数処理 → (端数処理, 旗)。ステップ③の合計・照合を、生成（generate_neo_file）と同じ
    端数処理で出すため（生成だけテンプレートに従い、画面と照合は四捨五入のままだった。レビュー 2 周目）。読めなければ ('四捨五入', 1)"""
    try:
        ck = find_real_cks(neo_bytes)
        full = decompress_neo(neo_bytes, ck)
        _m, ent = parse_entries(neo_bytes, ck[0])
        return _template_tax_round(extract_files(full, ent).get('AnSvEm0001Ex.db') or b'')
    except Exception:  # noqa: BLE001
        return ('四捨五入', 1)


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


_ERA_BASE = {'R': 2018, '令和': 2018, 'H': 1988, '平成': 1988, 'S': 1925, '昭和': 1925}   # 元年 = base + 1
# 各元号の期間（この外の日付は「その元号の日付として存在しない」ので空にする。黙って別の日にしない）
_ERA_SPAN = {2018: ((2019, 5, 1), (9999, 12, 31)), 1988: ((1989, 1, 8), (2019, 4, 30)), 1925: ((1926, 12, 25), (1989, 1, 7))}


def _normalize_date8(raw) -> str:
    """日付入力を YYYYMMDD の8桁に正規化する。解釈できなければ空文字。

    受け付ける形: 「20260901」「2026/09/01」「2026-9-1」「2026.09.01」「2026年9月1日」、
    和暦「R6.9.1」「R6/9/1」「令和6年9月1日」「H30.4.5」「S60.1.1」（元号 R/H/S・令和/平成/昭和、元年は 1 か 元）。
    年の無い「9/1」や、13 月などの妥当でない日付は空文字（黙って別の日にしない）。
    以前は「数字だけ抜いて 8 桁」だったため、2026-9-1 や和暦が黙って空になっていた（2026-09-14）。
    """
    import unicodedata as _ud
    t = _ud.normalize('NFKC', str(raw or '')).strip()
    if not t:
        return ''
    y = m = d = None
    mo = re.fullmatch(r'(\d{8})', t)
    if mo:
        y, m, d = int(t[:4]), int(t[4:6]), int(t[6:8])
    else:
        mo = re.fullmatch(r'(\d{4})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})\s*日?', t)
        if mo:
            y, m, d = int(mo.group(1)), int(mo.group(2)), int(mo.group(3))
        else:
            mo = re.fullmatch(r'(令和|平成|昭和|[RHS])\s*(\d{1,2}|元)\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})\s*日?', t, re.I)
            if mo:
                era = mo.group(1).upper() if len(mo.group(1)) == 1 else mo.group(1)
                n = 1 if mo.group(2) == '元' else int(mo.group(2))
                if n < 1:
                    return ''
                y, m, d = _ERA_BASE[era] + n, int(mo.group(3)), int(mo.group(4))
                era_span = _ERA_SPAN[_ERA_BASE[era]]
    if y is None:
        return ''
    try:
        dt = datetime.datetime(y, m, d)
    except ValueError:
        return ''
    if mo and mo.re.pattern.startswith('(令和|平成|昭和'):
        lo, hi = era_span
        if not (datetime.datetime(*lo) <= dt <= datetime.datetime(*hi)):
            return ''   # 平成31年5月1日・昭和64年1月8日 のような、その元号に無い日付
    # 昭和より前は和暦に変換できず、日付だけ入って元号が空になるため受け付けない
    if dt.year < 1926:
        return ''
    return dt.strftime('%Y%m%d')


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
    """comp_len連鎖法でCK位置を特定（偽CK除外）

    連鎖の始まりは「最後の塊がファイルの終わりでちょうど閉じる」ものを選ぶ（実機 307 本と雛形すべてで
    最後の CK 位置 + comp_len = ファイル長）。ファイル表の中に DOS 時刻 0x4B43 などの偽の 'CK' があると、
    以前はそこから連鎖を始めて展開に失敗し、その秒に保存された .neo がテンプレートとして弾かれ、
    ベタ打ちの検算も「検証できませんでした」になっていた（バグハント 3 回目 L9）。
    閉じる連鎖が無いときは従来どおり最初の候補からの連鎖を返す。
    """
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
    pos = set(all_ck)

    def _chain(first):
        chain = [first]
        ck = first
        while True:
            cl = struct.unpack('<H', data[ck - 4:ck - 2])[0]
            exp = ck + cl + 8
            if exp in pos:
                chain.append(exp)
                ck = exp
            else:
                return chain, (ck + cl == len(data))

    first_chain = None
    # 始まりの候補は先頭付近だけ試す（ファイル表は数百バイト。細工したファイルで試行が膨らまないように）
    for n, cand in enumerate(all_ck):
        if n >= 64 or cand > all_ck[0] + 65536:
            break
        chain, closed = _chain(cand)
        if closed:
            return chain
        if first_chain is None:
            first_chain = chain
    return first_chain or []


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

def update_ansmb(db_bytes, items, short_parts_wage, expenses=None, is_tax_inclusive=False, is_beta_mode=False,
                 tax_round='四捨五入'):
    """ERParts/Expense/Total を更新（値引き行の負工賃も対応）
    expenses: {
        'towing': レッカー費用,              # LineNo=5「レッカー代１」（固定費目名）
        'rental_car': 代車費用,              # 自由行（LineNo=9 から。費目名も書く）
        'tax_exempt': 非課税費用,            # 自由行（代車の次）に「非課税費用」（OutTaxFlag=1）
    tax_round: 消費税（請求書単位）の端数処理。テンプレートの Setting.tx_ArrangeFlag（1 四捨五入 / 2 切り捨て / 3 切り上げ）
    ショートパーツは expenses ではなく引数 short_parts_wage で受け取り、
    LineNo=4 の部品欄に入れる（実機も部品列に出す）。
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
                                  is_tax_inclusive, is_beta_mode, tax_round=tax_round)
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass


def _update_ansmb_impl(_tmp_db_path, items, short_parts_wage, expenses,
                       is_tax_inclusive, is_beta_mode, tax_round='四捨五入'):
    # 途中で落ちても必ず閉じる。閉じないまま抜けると、Windows では
    # SQLite がファイルを掴んだままで呼び出し元の unlink が失敗し、
    # **顧客情報の入った一時DBが消えずに残る**。
    conn = sqlite3.connect(_tmp_db_path)
    try:
        return _update_ansmb_body(conn, _tmp_db_path, items, short_parts_wage,
                                  expenses, is_tax_inclusive, is_beta_mode, tax_round=tax_round)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _update_ansmb_body(conn, _tmp_db_path, items, short_parts_wage, expenses,
                       is_tax_inclusive, is_beta_mode, tax_round='四捨五入'):
    # 消した明細（前の案件の品名・品番・金額）がファイルの空き領域に残らないよう 0 で消す（レビュー: 顧客 DB だけだった）
    conn.execute('PRAGMA secure_delete=ON')
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
        # 加算基礎（Base* / BumperBase*）も消す。消し忘れると、前案件の
        # .neo をテンプレートにしたとき、塗装計を 0 にしていても
        # コグニで塗装ページを開いた瞬間に前案件の加算基礎が生き返り、
        # 金額に乗る。FramePlan・PaintingEtcetera は同じ理由で消している。
        #
        # 文は**実在する欄だけ**で組み立てる。1つでも欄を持たない
        # テンプレートを渡されると UPDATE 全体が失敗し、例外が握り潰されて
        # もともと消していた Booth 系まで前案件の値が残るため。
        _pp_cols = {c[1] for c in cur.execute('PRAGMA table_info(PaintingPlan)')}
        _pp_want = (
            ('BoothFlag', 0), ('BoothTime', -1), ('BoothWageOutTax', -1),
            ('BoothWageInTax', -1), ('BoothWageTax', -1),
            ('BoothWageByManual', "''"),
            ('PaintingType', 0), ('PaintingTypeName', "''"),
            ('MaterialRate', 0), ('TwoToneFlag', 0),
            ('BaseTime', -1), ('BaseWageOutTax', -1), ('BaseWageInTax', -1),
            ('BaseWageTax', -1), ('BaseWageByManual', "''"),
            ('BaseByManual', 0),
            ('BumperBaseTime', -1), ('BumperBaseWageOutTax', -1),
            ('BumperBaseWageInTax', -1), ('BumperBaseWageTax', -1),
            ('BumperBaseWageByManual', "''"), ('BumperBaseManual', 0),
        )
        _pp_set = ['%s=%s' % (c, v) for c, v in _pp_want if c in _pp_cols]
        if _pp_set:
            cur.execute('UPDATE PaintingPlan SET ' + ', '.join(_pp_set))
    except sqlite3.Error:
        pass
    # 塗装セクションの「あり」フラグも消す。工賃だけ -1 にすると
    # 「調色あり・工賃ブランク」という説明できない状態になる。
    # 数え上げの列（TwoCSolidOther = ルーフ以外の枚数）も戻す。
    # 前案件の .neo をテンプレートにしたとき、ボデーシーリング・防錆ワックス・
    # 2コートソリッドのチェックが残り、コグニで開いて再計算すると
    # 前案件ぶんの塗装工賃（実機の例で 730+730+2200 = 3,660円）が乗る。
    #
    # 列名を並べた1本の UPDATE にすると、テンプレートに1列でも無いものが
    # あった時点で文まるごと失敗し、except で握りつぶされて
    # **1つもクリアされない**。テンプレートは利用者が持ち込む .neo なので、
    # 列構成が違うことがありうる。実際にある列だけで組み立てる。
    try:
        _pe_cols = {c[1] for c in cur.execute('PRAGMA table_info(PaintingEtcetera)')}
    except sqlite3.Error:
        _pe_cols = set()
    _pe_want = ('DSBlack', 'BStripe', 'BSealing', 'ARWax',
                'LCColorFlag', 'LCColorRoof', 'LCColorOtherChange', 'LCColorOtherRepair',
                'TwoCSolidFlag', 'TwoCSolidRoof', 'TwoCSolidOther', 'TwoTone')
    _pe_set = [f'{c}=0' for c in _pe_want if c in _pe_cols]
    if _pe_set:
        try:
            cur.execute('UPDATE PaintingEtcetera SET ' + ', '.join(_pe_set))
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
    total_parts_rowtax = 0      # 行ごとの部品税の合計（内訳の税額欄に使う）
    total_wages_rowtax = 0      # 行ごとの工賃税の合計
    _adj_parts_line = None
    _adj_wage_line  = None
    _adj_parts_amount = 0
    _adj_wage_amount  = 0
    _minus_one_rows = []   # ちょうど −1 円になる金額の行（L2）
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
        if not parts_no:
            # 見積書に品番が無いとき、Addata 照合が引いた品番で補う。
            # これまで db_parts_no はどこからも読まれず、照合しても
            # 品番が空のままの .neo ができていた。
            # 採用するのは L1/L2（品名が当たり、価格でも裏が取れた行）だけ。
            # L3 は「品名は当たったが価格が合わない／DBに価格が無い」状態で、
            # 部品そのものが違う可能性が残る。L4 は該当なし。
            # PDF経路（pdf_to_neo_pipeline）も L1/L2 でしか品番を採用していない。
            # ただし L2 には「見積側の金額が 0 で DB に価格がある」場合も含まれ
            # （auto_matching の level 判定）、そのときは価格の裏が取れていない。
            # 見積に金額のある行だけに絞る。
            if (str(item.get('match_level', '') or '') in ('L1', 'L2')
                    and safe_int(item.get('parts_amount', 0)) > 0):
                parts_no = str(item.get('db_parts_no', '') or '')
        
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
            if _m_side and len(neo_header.encode_cp932w(name)) > _avail:
                # 正規表現の先頭が \s* なので、re.search は左右記号の直前の
                # 空白の連なりからマッチする。そのまま温存すると22バイトの
                # 持ち分を空白が食い、識別に必要な語尾から先に消える。
                # 「…アウタ R」と「…インナ R」が両方「※フロントドアパネル  R」
                # になり、別部品が同じ文字列で並ぶ。空白は落として詰める。
                _side_txt = re.sub(r'\s+', '', name[_m_side.start():])
                _side_len = len(neo_header.encode_cp932w(_side_txt))
                _body = re.sub(r'\s+', '', name[:_m_side.start()])
                name = cp932_trim(_body, max(_avail - _side_len, 0)) + _side_txt
            name = '※' + cp932_trim(name, _avail)
        
        # 区分: work_code（Markdownパーサー保存先）または method から取得
        method = _ctrl_to_space(item.get('method', '') or item.get('work_code', ''))
        # 改行・タブなどの制御文字は空白にしてから書く。ERParts にそのまま入り、AnSMB（注記）側だけ消していたため、
        # 同じ行の品名・品番が 2 通りになっていた（バグハント 3 回目 L5）。AnSMB には ERParts に書いた値をそのまま置く
        name     = _ctrl_to_space(name)
        parts_no = _ctrl_to_space(parts_no)
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
            # 見積書の品名は半角カナで書かれることが多い（「ｼｮｰﾄﾊﾟｰﾂ」「ﾍﾟｲﾝﾄ」）。
            # 生の文字列で照合すると、全角で書いたキーワードに当たらず、
            # 区分なしの行になってしまう。区分の文字列と同じく NFKC で
            # 揃えてから探す（半角カナ→全角カナ、全角英数→半角英数）。
            _name_for_detect = unicodedata.normalize(
                'NFKC', str(item.get('name', '')))
            _parts_amt = safe_int(item.get('parts_amount', 0))
            _wage_amt  = safe_int(item.get('wage', 0))
            # 上から順に見て、最初に当たった語を区分にする。
            # (探す語, 区分として書く文字列)。書く文字列が None なら、
            # 当たった語そのものを使う。DisposalName は帳票の「修理方法」に
            # そのまま印字されるので、見積書に「脱着板金」と書いてあった行を
            # 「脱着修理」に書き換えない（区分コードはどちらも 3 で同じ）。
            #
            # 並び順に意味がある。複合語（2語以上）を単独の語より先に置く。
            # 「脱着修理」を「脱着」より後ろに置くと脱着（コード1）に取られ、
            # 実機の 3 にならない。
            _INFER = (
                # 複合区分
                (('脱着修理', '脱着鈑金', '脱着板金'), None),
                (('点検調整', '点検清掃'), None),
                (('磨き調整',), None),
                (('分解調整', '分解清掃'), None),
                # 研磨・磨き・写真代・ショートパーツは区分なしのまま。
                # 原本に区分が書かれていないものを勝手に決めない。
                # （「磨き調整」は上で拾うのでここには来ない）
                (('研磨', '磨き', '写真代', 'ショートパーツ'), ''),
                # 単独区分
                (('取替', '交換', '取換', '取り替え'), '取替'),
                (('脱着', '取外', '取付', '組付', '脱外'), '脱着'),
                (('鈑金', '板金'), None),
                (('塗装', 'ペイント', 'ワックス', '加算', 'ブース'), '塗装'),
                (('分解',), '分解調整'),
                (('点検', '診断'), '点検'),
                (('調整', '光軸', 'フィッティング', 'コーディング', '設定', '消去'), '調整'),
                (('修理', '補修', '修正', '穴あけ', 'シーリング'), '修理'),
            )
            # 部品金額があって工賃が無い行は取替。ただし「研磨」等の
            # 区分なし語が入っている行はそちらを優先する（上の並びの前に置く）。
            _no_method = any(kw in _name_for_detect
                             for kw in ('研磨', '磨き', '写真代', 'ショートパーツ'))                 and '磨き調整' not in _name_for_detect
            if not _no_method and _parts_amt > 0 and _wage_amt == 0:
                method = '取替'
            else:
                for _kws, _label in _INFER:
                    _hit = next((k for k in _kws if k in _name_for_detect), None)
                    if _hit is not None:
                        method = _hit if _label is None else _label
                        break

        qty    = safe_int(item.get('quantity', 1), 1)
        # 整数でない数量（'2.5' L など）はコグニの数量欄（整数）に書けない。丸めた数量で「単価の税×数量」の規則を当てると
        # 行の税が原本と変わる（2.5 L ¥2,750 → 数量 2・税 276）。数量 1・金額そのままで書く（バグハント 3 回目 L7）
        if is_fractional_qty(item.get('quantity')):
            qty = 1
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
                # 数量が2以上の行は、行合計から一気に逆算するのではなく
                # 「単価から逆算して数量倍」する。税抜表記のとき（下の else）と
                # 同じ実機の規則。入力が税抜か税込かで .neo の中身が変わっては
                # いけない。以前はここだけ一気に逆算していて、
                # 単価171×10個で税抜が 1,555（正しくは 1,550）と 5円ずれた。
                _q_in = qty if isinstance(qty, int) and qty > 1 else 0
                if _q_in and parts_total % _q_in == 0:
                    _unit_in = parts_total // _q_in
                    parts_outtax = jpy_round(_unit_in / (1 + TAX_RATE)) * _q_in
                else:
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
            # 数量が2以上の行は、行合計に税率を掛けるのではなく
            # 「単価に税率を掛けて四捨五入したものを数量倍」する。
            # 実機の .neo 202件で、数量>1 の行のうち両者が食い違う 32 行は
            # すべて後者だった（前者だけが正解になる行は 1 行も無い）。
            # 例: 単価155×10個 → 四捨五入(15.5)×10 = 160（行合計1550の10%＝155 ではない）。
            parts_tax = 0
            if parts_total != 0:
                _q_tax = qty if isinstance(qty, int) and qty > 1 else 0
                if _q_tax and parts_total % _q_tax == 0:
                    parts_tax = jpy_round((parts_total // _q_tax) * TAX_RATE) * _q_tax
                else:
                    parts_tax = jpy_round(parts_total * TAX_RATE)
            parts_intax  = parts_total + parts_tax if parts_total != 0 else 0
            wage_outtax  = wage_total
            wage_tax_abs = jpy_round(abs(wage_total) * TAX_RATE) if wage_total != 0 else 0
            wage_tax     = wage_tax_abs if wage_total >= 0 else -wage_tax_abs
            wage_intax   = wage_total + wage_tax if wage_total != 0 else 0
        # 行の税がちょうど −1（−5〜−14 円程度の値引き行）だと、税の欄が「空欄」に見える。行の税は 0 にする。
        # 税抜表記は請求書単位の消費税で総額が決まるので総額は変わらない。税込表記は税抜を税込と同じにし、差は下の
        # 配分で他の行が吸収する（レビュー 2026-09-15）
        if parts_total != 0 and parts_tax == -1:
            if is_tax_inclusive:
                parts_outtax = parts_intax
            else:
                parts_intax = parts_total
            parts_tax = 0
        if wage_total != 0 and wage_tax == -1:
            if is_tax_inclusive:
                wage_outtax = wage_intax
            else:
                wage_intax = wage_total
            wage_tax = 0
        total_parts += parts_outtax
        total_wages += wage_outtax
        # 部品計・工賃計の税額欄は「行ごとの税の合計」。仕様書 §6・§4 が
        # そう書いており、実機 200 件でも部品計は行ごとの合計と 100% 一致
        # （合計×10% の一括丸めは 82% しか合わない）。
        if parts_total != 0:
            total_parts_rowtax += parts_tax
        if wage_total != 0:
            total_wages_rowtax += wage_tax
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
        # −1 はコグニの「空欄」の印。ちょうど −1 円の金額（端数値引 −1 など）は書けない（下で止める。L2）
        if ((parts_total != 0 and -1 in (parts_outtax, parts_intax))
                or (wage_total != 0 and -1 in (wage_outtax, wage_intax))):
            _minus_one_rows.append((line_no, name, '部品' if parts_total != 0 and -1 in (parts_outtax, parts_intax) else '工賃'))
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
        # 区分コードは実機の基本定義ファイル（Auda7/AudaData/Const/AnDefine.ini）の
        # [WorkSheet] が定めている。Repair<N>=<表示順>,<区分コード>,<名称>,… で、
        #   0=取替  1=脱着  2=修理  6=板金  3=脱着修理/脱着板金
        #   4=点検/調整/点検調整  5=分解調整
        # 実機の .neo 202件・明細11,254行でもこの対応どおりだった
        # （板金121行はすべて 6、分解調整206行はすべて 5）。
        # 以前は板金・点検・調整・分解をまとめて 2（修理）にしていた。
        # 区分名そのものは DisposalName に原文が入るので帳票の見た目は
        # 変わらないが、コグニセブン側の再計算は区分コードで動く。
        # 並び順に意味がある。下の部分一致フォールバックは登録順に見るので、
        # 「脱着修理」を「脱着」より後ろに置くと 1（脱着）に取られる。
        # 対応表と引き方は neo_rules に置いてある（3つの入口で同じ規則を使うため）。
        disposal_code = neo_rules.disposal_code(_method_full)
        # 指数（工数）。画面まで往復させておきながら NEO には書いていなかったため、
        # コグニセブン側では全行が指数ゼロの見積として開かれていた。
        # 単位は時間の小数（1.0 = 100WI, _addata_db_search.match_wage_by_time 参照）。
        # 素の float() だと全角「１．５」「(0.8)」「1.5h」を落とし、
        # 同じ行の全角金額は読めるのに指数だけ欠ける。金額と同じ正規化を通す。
        # （auto_matching も同じ index_value を正規化して工数照合に使っている）
        # 括弧書きは金額なら会計表記のマイナス（値引きの「(5,000)」）だが、
        # 指数の括弧は**ただの印字**。コグニセブンが印刷する見積書は
        # 「加算基礎数値 ( 1.50) 11,000」「ブース加算 ( 0.50) 3,670」のように
        # 指数を括弧付きで出す（実機の帳票 PDF で確認）。
        # 金額と同じ正規化を通すと (0.8) が -0.8 になり、下の
        # 「負値は -1（空欄）」に落ちて、指数のある行が指数ゼロで出ていた。
        _idx_src = neo_rules.strip_index_parens(item.get('index_value', ''))
        _idx_raw = _normalize_number_text(_idx_src)
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
        # ERParts.PartsCode は部品マスタの参照番号を4桁ゼロ埋めした文字列。
        # 帳票のいちばん左「ｺｰﾄﾞ」列にそのまま印字される。
        # 照合できなかった行は空欄（実機の自由入力行 536行中 533行も空欄）。
        parts_code = str(item.get('_master_ref_no', '') or '')
        if parts_code and not (len(parts_code) == 4 and parts_code.isdigit()):
            parts_code = ''      # 4桁でないものは書かない（帳票の桁が崩れる）
        # PartsCodeSub（枝番）は実機では -1 が既定で、同じ参照番号に
        # ぶら下がる手入力材料（接着剤など）にだけ 1,2,… が入る。
        # 実機の自由入力行は 536行すべてが -1 だった。
        # 以前は部品マスタの別の欄を枝番として書こうとしており、
        # '1AA' のような英字混じりが INTEGER 列に入らず、それを理由に
        # 部品コードごと捨てていた（主要部品のコードが常に空欄だった）。
        parts_code_sub = -1
        # 手入力の印と数量行の単価欄（バグハント 3 回目 L11。vendor・実機と同じ）。
        # 実機 307 本の手入力行（部品コード無し）: 部品代の無い行の PartsPriceByManual は 165 行すべて ''、
        # 工賃も指数も無い行の WageByManual は 386 行すべて ''。以前はどちらも '*' 固定だった。
        # 部品コードのある行（照合した行）は従来どおり '*'（'' にするとコグニが標準価格・標準工賃で埋め直しうる）。
        _manual_row = not parts_code
        db_parts_manual = '*' if (parts_total != 0 or not _manual_row) else ''
        db_wage_manual = '*' if (wage_total != 0 or db_time > 0 or not _manual_row) else ''
        # 数量 2 以上の行は単価欄にも書く（実機 64/64 行。単価の税は切り捨て: 155 → 15。vendor と同じ）
        db_unit_out = db_unit_in = db_unit_tax = -1
        if (not is_tax_inclusive and isinstance(qty, int) and qty > 1 and parts_total != 0
                and db_parts_total > 0 and db_parts_total % qty == 0):
            db_unit_out = db_parts_total // qty
            db_unit_tax = (db_unit_out * 10) // 100
            db_unit_in = db_unit_out + db_unit_tax
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
            ?, ?, ?,
            -- 空欄は -1。NULL や 0 にすると帳票に「0」と表示され、
            -- 標準部品価格ゼロ・標準指数ゼロの見積として読まれてしまう。
            -1, -1, -1,
            ?,
            -- 指数は入力値、標準指数は 0。実機の自由入力行 536行すべてが
            -- TimeStandard=0 で、-1（空欄）を持つ行は1行も無かった。
            ?, 0,
            ?, ?, ?,
            -- 標準工賃も 0。実機の自由入力行 527行（98.3%）が 0 で、
            -- 標準部品価格だけが -1 のまま、という非対称な形をしている。
            0, 0, 0,
            ?, ?,
            -1, -1, -1,
            '', '', '',
            ?, '', 0, 0,
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
            db_unit_out, db_unit_in, db_unit_tax,
            db_parts_manual,
            db_time,
            db_wage_total, db_wage_intax, db_wage_tax,
            db_wage_manual,
            # 部品代の無い行（工賃だけの行）の数量。実機 150 件では
            # **-1 が 90.3% ／ 1 が 9.7% ／ 2以上は 1 行も無い**（AnSMB の
            # 数量欄は全部 '01'）。数量1のときに 1 を書くと「部品が無いのに
            # 1個」と読めるので -1（空欄）にする。
            # ただし数量2以上はそのまま残す。消すと ERParts=-1 なのに
            # AnSMB='03'・画面=3 となり、同じ行の数量が3通りになる
            # （その食い違いは過去に直してある）。実機は2以上を作らないので、
            # 実機との食い違いは生まれない。
            (db_qty if (db_parts_total > 0 or (isinstance(db_qty, int)
                                               and db_qty > 1)) else -1),
            # 行の由来。実機では ERParts.OrderFlag と AnSMB の [100] バイトが
            # **同じ欄**で、実機 120 件 4,875 行で1行も食い違わなかった。
            # ここに '9' を固定で書いていたため、同じ .neo の中で
            # ERParts は '9'、AnSMB は '0'/' ' という矛盾した値を持っていた。
            # '9' は実機 6,024 行のうち 2.3% しか無い少数派で、
            # 多数派は '0'（マスタ由来）と ' '（手入力）。AnSMB 側と同じ規則にする。
            '0' if parts_code else ' ',
        ))
        # AnNote.ini は ERParts と同じ値でなければならない。生の items から
        # 別に組み立てると、マスタ名への置換・「※」付与・数量ブランクが
        # 反映されず、同じ行なのに品名と数量が2通り存在することになる。
        annote_rows.append({'line_no': line_no, 'name': name, 'qty': db_qty,
                            'parts_code': parts_code, 'disposal_code': disposal_code,
                            'parts_no': parts_no})
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
    # ショートパーツは部品費。実機は部品欄（PartsEnabled）に入れており、
    # 帳票でも「部品価格」列に出る。工賃欄に入れると金額は合っていても
    # 列が1つずれた見積書になる。
    cur.execute("""UPDATE Expense SET
        PartsEnabled=?, PartsPriceOutTax=?, PartsPriceInTax=?, PartsPriceTax=?,
        WageEnabled=0, WageOutTax=0, WageInTax=0, WageTax=0
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

    # LineNo=9: 自由入力の費用行（NameFix=0）に「代車費用」という費目名で入れる。
    # LineNo=1〜8 は NameFix=1 の固定費目で、名前を変えられない。以前は
    # LineNo=7「写真代他」に金額だけ入れていたため、帳票の費目名が
    # 「写真代他」と印字されていた（協定見積に出す書類として別物になる）。
    # 実機も代車・室内清掃・エーミング等は LineNo=9 以降に名前を付けて使う。
    rental_car = safe_int(expenses.get('rental_car', 0))
    rent_out, rent_intax, rent_tax = _calc_tax(rental_car, False)
    _free_lines = [r[0] for r in cur.execute(
        'SELECT LineNo FROM Expense WHERE NameFix=0 AND LineNo>=9 ORDER BY LineNo').fetchall()]

    def _take_free_line(_what):
        if _free_lines:
            return _free_lines.pop(0)
        raise ValueError(f"テンプレートの費用欄に空いた行が無いので「{_what}」を書けません（自由入力の費用行が要ります）。"
                         "別のテンプレートを使うか、費用を 0 にしてください。")
    if rental_car > 0:
        cur.execute("""UPDATE Expense SET
            Name=?, WageEnabled=1, WageOutTax=?, WageInTax=?, WageTax=?
            WHERE LineNo=?""", (cp932_trim('代車費用', 30), rent_out, rent_intax, rent_tax, _take_free_line('代車費用')))

    # LineNo=10: 自由入力の費用行に「非課税費用」という費目名で入れる。OutTaxFlag=1 で非課税であることを示す。
    # 以前は LineNo=8（固定費目「その他控除」）に入れていたため、帳票に「その他控除 3,000」と印字されていた。
    # 実機 307 本で LineNo 8 に金額の入った .neo は 1 本も無く、非課税の行は自由行にも置かれている。vendor も
    # 名前付きの自由行に置く（バグハント 3 回目 L10）
    tax_exempt = safe_int(expenses.get('tax_exempt', 0))
    if tax_exempt > 0:
        if rental_car <= 0 and _free_lines and _free_lines[0] == 9 and len(_free_lines) > 1:
            _free_lines.pop(0)   # 9 行目は代車の置き場として空けておく（いつも同じ行に同じ費目）
        cur.execute("""UPDATE Expense SET
            Name=?, OutTaxFlag=1, WageEnabled=1, WageOutTax=?, WageInTax=?, WageTax=0
            WHERE LineNo=?""", (cp932_trim('非課税費用', 30), tax_exempt, tax_exempt, _take_free_line('非課税費用')))

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
            _err = abs(_cand + _round_tax10(_cand, tax_round) - _items_intax)   # 端数処理はテンプレートの設定（レビュー 2 周目）
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
        for _target, _cur, _col_out, _col_in, _col_tax, _skip_qty in (
            (_target_parts, total_parts,
             'PartsPriceOutTax', 'PartsPriceInTax', 'PartsPriceTax', True),
            (_target_wages, total_wages,
             'WageOutTax', 'WageInTax', 'WageTax', False),
        ):
            _delta = _target - _cur
            if _delta == 0:
                continue
            # 金額の入っている行を、税込額の大きい順に並べる。
            # **数量が2以上の部品行は外す。** その行の税抜額は
            # 「単価から逆算して数量倍」という実機の規則で決まっていて、
            # 1円動かすと規則から外れる（数量10の行なら本来10円刻み）。
            # 調整は数量1の行だけで吸収する。全部が数量2以上なら調整しない
            # ——総額は下で税額を差額にして合わせるので、ずれない。
            _qty_cond = ' AND (PartsCount IS NULL OR PartsCount <= 1)' if _skip_qty else ''
            _rows = cur.execute(
                f'SELECT LineNo, {_col_out}, {_col_in} FROM ERParts'
                f' WHERE {_col_out} IS NOT NULL AND {_col_out} != -1{_qty_cond}'
                f' ORDER BY ABS({_col_in}) DESC, LineNo ASC').fetchall()
            if not _rows:
                continue
            _step = 1 if _delta > 0 else -1
            # 税込額の大きい行から 1 円ずつ何周も配る（従来どおり。ふつうはどの行も自然な逆算値から ±1 円に収まり、
            # 消費税 = 課税額計の 10% が保たれる）。ただし 1 行に寄せるのは税込額の 1%（最低 1 円）まで、税の符号が
            # 反転する（税抜が税込を超える）・−1（空欄の印）・0 になる動きはしない。以前は上限が無く、数量 2 以上の行の
            # 丸め差が数量 1 の少数の行に寄って、ステッカー 110 円の行が 税抜 145／税額 −35 になっていた（O4）。
            # 配りきれない端数は、下で税額（税込 − 税抜）が吸収するので総額は変わらない
            _moved = [0] * len(_rows)
            _cur_out = [r[1] for r in _rows]

            def _can(_k2, _nv):
                _in2 = _rows[_k2][2]
                if _nv in (0, -1) or _in2 == 0 or (_nv > 0) != (_in2 > 0):
                    return False
                _tx2 = _in2 - _nv
                if _in2 > 0 and _tx2 < 0:
                    return False
                if _in2 < 0 and (_tx2 > 0 or _tx2 == -1):
                    return False
                return abs(_moved[_k2] + _step) <= max(1, abs(_in2) // 100)

            while _delta != 0:
                _progress = False
                for _k in range(len(_rows)):
                    if _delta == 0:
                        break
                    _nv = _cur_out[_k] + _step
                    if not _can(_k, _nv):
                        continue
                    _cur_out[_k] = _nv
                    _moved[_k] += _step
                    _delta -= _step
                    _progress = True
                if not _progress:
                    break
            for _k, (_ln, _out, _in) in enumerate(_rows):
                if _cur_out[_k] != _out:
                    cur.execute(
                        f'UPDATE ERParts SET {_col_out}=?, {_col_tax}=? WHERE LineNo=?',
                        (_cur_out[_k], _in - _cur_out[_k], _ln))
            _applied = _target - _cur - _delta
            if _col_out == 'PartsPriceOutTax':
                total_parts += _applied
            else:
                total_wages += _applied

    if _minus_one_rows:
        _ln0, _nm0, _kind0 = _minus_one_rows[0]
        raise ValueError(
            f"明細「{_nm0 or f'{_ln0 // 10}行目'}」の{_kind0}がちょうど −1 円です"
            + (f"（ほか {len(_minus_one_rows) - 1} 行）" if len(_minus_one_rows) > 1 else "")
            + "。コグニセブンでは −1 は「空欄」の印なので、この金額は .neo に書けません"
            "（書くと行は空欄に見えるのに、合計だけ 1 円ずれます）。見積書どおりか確かめ、"
            "端数の値引きはコグニセブンで開いてから値引き欄に入れてください。")

    # ── Total計算 ──
    # total_parts / total_wages は既に税抜値（is_tax_inclusive時は逆算済み）
    taxable_expenses = sp_out + tow_out + rent_out
    sub_total         = total_parts + total_wages + taxable_expenses
    # 行ごとの税の合計を使う（上で足してある）。以前は「合計×10%」を
    # 1回丸めていたため、実機と 18% の見積で部品計の税額欄が食い違った。
    parts_tax_total   = total_parts_rowtax
    wages_tax_total   = total_wages_rowtax
    sp_tax_total      = _round_tax10(sp_out, tax_round)
    expenses_tax_total = _round_tax10(taxable_expenses, tax_round)   # 税込表記の総額の税もテンプレートの端数処理（レビュー 2 周目）
    # 消費税は請求書単位で1回だけ丸める。これが見積書に印字された税込総額の
    # 作り方であり、画面もこの刻みで出している。
    # バケットごとに丸めて足すと、約4件に1件で原本の税込総額から1円離れる。
    # 税込表記のときは、上で「税を足すと原本の税込総額に戻る」税抜額を
    # わざわざ探索しているので、明細ぶんと費用ぶんを分けて丸めないと
    # その探索の成果が壊れる。
    if is_tax_inclusive:
        # **税は「原本の税込 − 逆算した税抜」で決める。区分ごとに。**
        #
        # 10% を掛け直すと、原本の税込総額が「税抜＋消費税」で表せない場合に
        # 1円ずれる。表せない額は 11円ごとに1つあり（1〜3000円で273個）、
        # 以前は「いちばん近い税抜額」を採って**警告も出さずに1円ずれた
        # 協定見積**を出していた。原本に書いてある総額こそが正。
        #
        # **実機が印刷した見積書と、対になる .neo を突き合わせて確かめた。**
        # 紙に出ている消費税額と総額は、.neo に書かれた値そのものだった
        # （3組とも一致）。コグニは書かれた税額をそのまま持ち、
        # 計算し直していない。だから差額で書いてよい。詳しくは引き継ぎ書 §6.1。
        #
        # 部品と工賃は**別々に**差額で確定させる。まとめて差額にすると、
        # 部品側で出た端数が「いちばん金額の大きい欄」に寄せられ、
        # 総額は合っていても部品計・工賃計の税込内訳が原本からずれる。
        parts_tax_total = total_parts_intax - total_parts
        wages_tax_total = total_wages_intax - total_wages
        tax_total = parts_tax_total + wages_tax_total + expenses_tax_total
    else:
        tax_total = _round_tax10(sub_total, tax_round)
    # 内訳の税額欄（部品計・工賃計・諸経費計）の合計は tx_Total と
    # 一致させる。同じ .neo の中で「内訳の和 ≠ 合計」になっていると、
    # コグニの画面でも紙でも説明がつかない（この不一致は過去に
    # tests/reg_expense.py で固めてある）。
    #
    # 実機は必ずしも一致させていない（塗装の無い 25 件中 19 件が一致、
    # 残りは +2/+5/+8 円）が、一致していない .neo を出す理由は無い。
    # 内訳の出発点を「行ごとの税の合計」にしたので、寄せる端数は
    # ±1円程度で済み、いちばん大きい欄に乗らない区分は行ごとの値が残る。
    _tax_resid = tax_total - (parts_tax_total + wages_tax_total
                              + expenses_tax_total)
    if _tax_resid:
        _biggest = max((abs(total_parts), 'p'), (abs(total_wages), 'w'),
                       (abs(taxable_expenses), 'e'))[1]
        if _biggest == 'p':
            parts_tax_total += _tax_resid
        elif _biggest == 'w':
            wages_tax_total += _tax_resid
        else:
            expenses_tax_total += _tax_resid
    # 諸経費の内訳を部品側（ショートパーツ）と工賃側（レッカー・代車）に割る。
    # 足せば taxable_expenses / expenses_tax_total に戻る＝合計は変わらない。
    _hy_p_out = sp_out
    _hy_w_out = taxable_expenses - sp_out
    _hy_p_tax = sp_tax_total if _hy_p_out > 0 else 0
    _hy_w_tax = expenses_tax_total - _hy_p_tax
    if _hy_w_out <= 0:
        # 工賃側が空なら端数も部品側に寄せる（両方に分けると片方が
        # 「金額ゼロなのに税だけある」欄になる）。
        _hy_p_tax, _hy_w_tax = expenses_tax_total, 0
    if _hy_p_out <= 0:
        _hy_p_tax, _hy_w_tax = 0, expenses_tax_total
    # expenses_tax_total には請求書単位のまとめ丸めの端数が寄せてあるため、
    # 部品側の税をそのまま引くと工賃側が負になることがある
    # （諸経費がいちばん大きい欄で、かつ端数がマイナスのとき）。
    # 税額欄が負の見積書は帳票として成立しない。はみ出したぶんは
    # もう一方の欄で吸収する。合計（_hy_p_tax + _hy_w_tax）は変えない。
    if _hy_w_tax < 0:
        _hy_p_tax += _hy_w_tax
        _hy_w_tax = 0
    if _hy_p_tax < 0:
        _hy_w_tax += _hy_p_tax
        _hy_p_tax = 0
    grand_total       = sub_total + tax_total + tax_exempt  # 非課税は税計算後に加算
    cur.execute("""UPDATE Total SET
        ms_PartsTotalOutTax=?,
        ms_PartsTotalInTax=?,
        ms_PartsTotalTax=?,
        ms_WageTotalOutTax=?,
        ms_WageTotalInTax=?,
        ms_WageTotalTax=?,
        hy_PartsTaxTotalOutTax=?,
        hy_PartsTaxTotalInTax=?,
        hy_PartsTaxTotalTax=?,
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
        # 諸経費は部品側（ショートパーツ）と工賃側（レッカー・代車）に分かれる。
        # 実機もこの2欄を使い分けており、帳票の部品列・工賃列に別々に出る。
        # 分けても足せば元の taxable_expenses / expenses_tax_total に戻るので、
        # 課税額計も消費税も総額も1円も動かない。
        _hy_p_out, _hy_p_out + _hy_p_tax, _hy_p_tax,
        _hy_w_out, _hy_w_out + _hy_w_tax, _hy_w_tax,
        # 非課税ぶんは工賃側の非課税欄に計上する。どの内訳にも入れないと
        # 小計＋消費税が合計に届かず、帳票の検算が合わなくなる。
        0, 0, 0,
        tax_exempt, tax_exempt, 0,
        # レッカー専用欄（hy_Wrecker1）は実機が一度も使っていない。
        # 実機の .neo 202件はレッカー案件も含めてすべて 0 で、レッカー代は
        # 費用行（LineNo=5 レッカー代１）か明細行に入っていた。
        # 課税額計には入らない欄なので総額は変わらないが、専用欄を持つ
        # 帳票を出したときに同じ金額が2か所に出る。実機に合わせて空にする。
        0, 0, 0, 0,
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

def update_em_db(db_bytes, cust, insurance_info, estimated_date, is_tax_inclusive=False, merge_mode=False,
                 tax_arrange_flag=1):
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
        # 途中で落ちても接続を閉じる。閉じないまま抜けると、Windows では一時 DB（顧客情報入り）が消せずに残る（バグハント 3 回目 L19）
        conn = sqlite3.connect(tf.name)
        try:
            _update_em_db_impl(conn, cust, insurance_info, estimated_date, is_tax_inclusive, merge_mode,
                               tax_arrange_flag=tax_arrange_flag)
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        with open(tf.name, 'rb') as f:
            return f.read()
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
    cust = dict(cust or {})
    # 住所は実機 NEO と同じく 都道府県 / 市区郡（'〜市' まで。政令市の区は以降側）/ 以降 に分け直す（Codex hunt B2）
    if any(str(cust.get(k) or '').strip() for k in ('prefecture', 'municipality', 'address_other')):
        cust['prefecture'], cust['municipality'], cust['address_other'] = _doc_hints.split_address(
            _strip_control_chars(cust.get('prefecture', '')), _strip_control_chars(cust.get('municipality', '')),
            _strip_control_chars(cust.get('address_other', '')))
    return {
        'customer_name': cp932_trim(_strip_control_chars(cust.get('customer_name', '')), _CUST_WIDTH['Name1']),
        # 使用者欄: vehicle_info に user_name があればそれ（車検証の使用者が同上なら '同上'。Codex hunt B7）、無ければ従来どおり顧客名
        'user_name':     cp932_trim(_strip_control_chars(cust.get('user_name') if str(cust.get('user_name') or '').strip() else cust.get('customer_name', '')), _CUST_WIDTH['UserName']),
        'owner_name':    cp932_trim(_strip_control_chars(cust.get('owner_name', '')),    _CUST_WIDTH['OwnerName']),
        'postal_no':     cp932_trim(_strip_control_chars(cust.get('postal_no', '')),     _CUST_WIDTH['PostalNo']),
        'prefecture':    cp932_trim(_strip_control_chars(cust.get('prefecture', '')),    _CUST_WIDTH['Prefecture']),
        'municipality':  cp932_trim(_strip_control_chars(cust.get('municipality', '')),  _CUST_WIDTH['Municipality']),
        'address_other': cp932_trim(_strip_control_chars(cust.get('address_other', '')), _CUST_WIDTH['AddressOther1']),
        # 登録番号はコグニと同じ半角数字・ハイフン無しに揃える（_reg_no_part。実機 NEO 108 本の集計）
        'car_dept':      cp932_trim(_reg_no_part(cust.get('car_reg_department', '')), _CUST_WIDTH['CarRegNoDepartment']),
        'car_div':       cp932_trim(_reg_no_part(cust.get('car_reg_division', ''), digits=True), _CUST_WIDTH['CarRegNoDivision']),
        'car_biz':       cp932_trim(_reg_no_part(cust.get('car_reg_business', ''), kana=True), _CUST_WIDTH['CarRegNoBusiness']),
        'car_serial':    cp932_trim(_reg_no_part(cust.get('car_reg_serial', ''), digits=True, strip_hyphen=True), _CUST_WIDTH['CarRegNoSerial']),
        'car_serial_no': cp932_trim(_strip_control_chars(cust.get('car_serial_no', '')),      _CUST_WIDTH['CarSerialNo']),
        'model_desig':   cp932_trim(_num_code(cust.get('car_model_designation', ''), 5), _CUST_WIDTH['CarMouldNo']),
        'category_num':  cp932_trim(_num_code(cust.get('car_category_number', ''), 4),   _CUST_WIDTH['CarKindNo']),
        'car_name':      cp932_trim(_strip_control_chars(cust.get('car_name', '')),      _CAR_WIDTH['CarName']),
        'body_color':    cp932_trim(_strip_control_chars(cust.get('body_color', '')),    _CAR_WIDTH['ColorName']),
        'color_code':    cp932_trim(_strip_control_chars(cust.get('color_code', '')),    _CAR_WIDTH['ColorCode']),
        'trim_code':     cp932_trim(_strip_control_chars(cust.get('trim_code', '')),     _CAR_WIDTH['TrimCode']),
    }


def _update_em_db_impl(conn, cust, insurance_info, estimated_date,
                       is_tax_inclusive, merge_mode, tax_arrange_flag=1):
    """顧客・車両・保険・案件の欄を書く（接続の開け閉めは update_em_db）"""
    # 書き換えで空いた領域を 0 で消す。過去の案件の .neo をテンプレートにすると、上書きした氏名・車台番号の古い値が
    # ページの空き領域に残り、提出する .neo の中から読めてしまう（バグハント 3 回目 L1）
    conn.execute('PRAGMA secure_delete=ON')
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
    car_reg_date   = normalized_reg_date(cust.get('car_reg_date', ''))
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
        # 走行距離は 0 より大きいときだけ書く。画面の数値欄は未入力でも 0 なので、0 で上書きするとテンプレートの距離が消える（L13）
        if kilometer > 0:
            _cust_updates.append('Kilometer=?')
            _cust_values.append(kilometer)
        # 住所を書き換えたら住所コードは前の住所のもの。残さない（L1）
        if any((postal_no, prefecture, municipality, address_other)) and 'AddressCode' in {
                r[1] for r in cur.execute('PRAGMA table_info(Customer)').fetchall()}:
            _cust_updates.append('AddressCode=?')
            _cust_values.append('')
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
        # 画面に入力欄の無い顧客欄（氏名 2・3、住所 2、電話、FAX、住所コード）も前の案件の値を残さない
        # （バグハント 3 回目 L1。vendor と同じ。実機 307 本: Name2・住所コードは全件空）
        _cu_cols = {r[1] for r in cur.execute('PRAGMA table_info(Customer)').fetchall()}
        _cu_blank = [c for c in ('Name2', 'Name3', 'AddressOther2', 'Phone', 'Fax', 'AddressCode') if c in _cu_cols]
        if _cu_blank:
            cur.execute('UPDATE Customer SET ' + ', '.join(f"{c}=''" for c in _cu_blank))

    # Car テーブル更新（車名・カラーコード・トリムコード） — 非空の値のみ更新（通常・マージ共通）
    car_cols = {row[1] for row in cur.execute("PRAGMA table_info(Car)").fetchall()}
    car_update = [
        ('CarName', car_name), ('CarNameByUser', car_name),
        ('ColorCode', color_code), ('ColorName', body_color),
        ('TrimCode', trim_code),
        # カラーコードを書いたら旗も 1（実機 307 本: コードあり 293 本が 1、コード無し 12 本が 0。vendor と同じ。バグハント 3 回目 L18）
        ('ColorCodeFlag', 1 if color_code else 0),
        ('TrimCodeFlag', 1 if trim_code else 0),
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
    _update_car_search(cur, merge_mode, car_name, color_code, body_color, trim_code,
                       car_serial_no, model_desig, category_num, car_reg_date, reg_era, reg_era_year)
    est_era, est_era_year = get_era_info(estimated_date)
    cur.execute('''UPDATE FileInfo SET
        EstimatedDate=?, EstimatedEra=?, EstimatedEraYear=?
    ''', (estimated_date, est_era, est_era_year))
    # コグニセブンの列幅に合わせて切り詰める。SQLite は TEXT(n) を強制しないため
    # ここで守らないと、桁あふれした値がそのまま入る。
    try:   # マージモードでテンプレートに残っている証券番号（これがあるなら事故番号で塗り替えない）
        _tpl_policy = safe_str((cur.execute('SELECT PolicyNo FROM Insurance').fetchone() or ('',))[0])
    except sqlite3.Error:
        _tpl_policy = ''
    policy_no     = cp932_trim(policy_no_or_accept(insurance_info, _tpl_policy, merge_mode), 20)
    contractor    = cp932_trim(insurance_info.get('contractor_name', ''), 20)
    agency_name   = cp932_trim(insurance_info.get('agency_name', ''), 20)
    adjuster_name = cp932_trim(insurance_info.get('adjuster_name', ''), 20)
    adjuster_post = cp932_trim(insurance_info.get('adjuster_post', ''), 40)   # 支店・所属（Insurance.AdjusterPost は TEXT(40)。L20）
    factory_name  = cp932_trim(insurance_info.get('factory_name', ''), 30)   # 立会工場（Insurance.ConsultantFactory。vendor と同じ 30 バイト）
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
                       ('AgencyName', agency_name), ('AdjusterName', adjuster_name), ('AdjusterPost', adjuster_post)):
        if _val or not merge_mode:
            _ins_updates.append(f'{_col}=?')
            _ins_values.append(_val)
    if factory_name:   # 立会工場は入れたときだけ書く（空ならテンプレートの値を残す。以前からこの欄には触れていなかった。レビュー 2026-09-15）
        _ins_updates.append('ConsultantFactory=?')
        _ins_values.append(factory_name)
    if not merge_mode:   # 過去案件の NEO をテンプレートにしても、立会日・協定日・立会者は前の案件のまま残さない（バグハント I5）
        for _col, _val in (('PresenceDate', '00000000'), ('PresenceEraYear', '0000'), ('AgreedDate', '00000000'), ('AgreedEraYear', '0000'), ('ConsultantName', '')):
            _ins_updates.append(f'{_col}=?')
            _ins_values.append(_val)
        # 時価額も前の案件の値を残さない（-1 = 空欄。実機 307 本すべて -1。vendor と同じ。バグハント 3 回目 L1）
        _ins_cols = {r[1] for r in cur.execute('PRAGMA table_info(Insurance)').fetchall()}
        for _col in ('TimelyPriceOutTax', 'TimelyPriceInTax', 'TimelyPriceTax'):
            if _col in _ins_cols:
                _ins_updates.append(f'{_col}=?')
                _ins_values.append(-1)
    if repair_days > 0 or (not merge_mode and 'RepairDays' in {
            r[1] for r in cur.execute('PRAGMA table_info(Insurance)').fetchall()}):   # 非マージで入力が無ければ -1（空欄）。前の案件の日数を残さない（L1）
        _ins_updates.append('RepairDays=?')
        _ins_values.append(repair_days if repair_days > 0 else -1)
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
        elif not merge_mode and f'{_prefix}Date' in {r[1] for r in cur.execute('PRAGMA table_info(FileInfo)').fetchall()}:
            # 前の案件の入出庫日を残さない（バグハント 3 回目 L1）。列の無いテンプレートでは書かない（UPDATE ごと落ちる）
            _fi_updates += [f'{_prefix}Date=?', f'{_prefix}Era=?', f'{_prefix}EraYear=?']
            _fi_values  += ['00000000', '令和', '0000']
    if not merge_mode:   # 入力欄の無い 備考 2・3 とグループキーも前の案件の値を残さない（L1。実機 307 本すべて空）
        _fi_cols = {r[1] for r in cur.execute('PRAGMA table_info(FileInfo)').fetchall()}
        for _col in ('Note2', 'Note3', 'GroupKey'):
            if _col in _fi_cols:
                _fi_updates.append(f'{_col}=?')
                _fi_values.append('')
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
                EstimationId='', ProjectNo='', ProjectCompletedFlag='0',
                DefiniteOutTax=-1, DefiniteInTax=-1, DefiniteTax=-1,
                AccidentLargeCategoryCode='', AccidentSmallCategoryCode='',
                DisasterFlag='0', DisasterIdentificationCode=''""")
            conn.commit()
        except Exception as e:
            print("Statistics reset failed:", e)

    # TaxKindFlag 更新 (1=内税, 0=外税)。消費税の率・計算単位は、この経路の計算（10%・請求書単位）にそろえる。端数処理は
    # テンプレートの設定（tax_arrange_flag。明細 DB の税もこの端数処理で計算している）。1 つの .neo の中で設定と税額が食い違わないように
    # （バグハント 3 回目 O3。実機 307 本: (TaxRate, tx_CalculateFlag, tx_Unit, tx_ArrangeFlag) = (10,1,1,1) が 300 本、切り捨て (10,1,1,2) が 7 本）
    try:
        tax_flag = 1 if is_tax_inclusive else 0
        _set_cols = {r[1] for r in cur.execute('PRAGMA table_info(Setting)').fetchall()}
        _set_upd = [(c, v) for c, v in (('TaxKindFlag', tax_flag), ('TaxRate', 10), ('tx_CalculateFlag', 1),
                                         ('tx_Unit', 1), ('tx_ArrangeFlag', int(tax_arrange_flag or 1)))
                    if c in _set_cols]
        if _set_upd:
            cur.execute('UPDATE Setting SET ' + ', '.join(f'{c}=?' for c, _ in _set_upd), [v for _, v in _set_upd])
        conn.commit()
    except Exception as e:
        print("TaxKindFlag update failed:", e)


def _update_car_search(cur, merge_mode, car_name, color_code, body_color, trim_code,
                       car_serial_no, model_desig, category_num, car_reg_date, reg_era, reg_era_year):
    """CarSearch（車種の検索条件）を Car・Customer と同じ値にそろえる（バグハント 3 回目 L1）。

    実機 307 本: nm_CarNameByUser = Car.CarNameByUser が全件、ev_CarName = Car.CarName が 299 本。
    車検証から検索した .neo（SearchMethod=3）は ps_CarSerialNo = Customer.CarSerialNo = Head + '-' + Tail、
    ps_CarMouldNo・ps_CarRegDate も Customer と同じ（243/243 本）。メーカーから検索した汎用車種
    （SearchMethod=1）は ps_* が空（9/9 本）、ms_CarSerialNoHead/Tail は全件空。
    過去の案件の .neo をテンプレートにすると、ここに前の車の車台番号が残っていた。
    マージでは空の値は書かない（テンプレートの値を残す。Car と同じ規則）。
    """
    try:
        _cols = {r[1] for r in cur.execute('PRAGMA table_info(CarSearch)').fetchall()}
        _row = cur.execute('SELECT SearchMethod FROM CarSearch').fetchone() if 'SearchMethod' in _cols else None
    except sqlite3.Error:
        return
    if not _cols:
        return
    _sm = safe_int(_row[0], 0) if _row else 0
    _want = [('nm_CarNameByUser', car_name), ('ev_CarName', car_name),
             ('ev_ColorCode', color_code), ('ev_ColorName', body_color), ('ev_TrimCode', trim_code)]
    _ps = ('ps_CarSerialNo', 'ps_CarSerialNoHead', 'ps_CarSerialNoTail', 'ps_CarMouldNo', 'ps_CarKindNo',
           'ps_CarRegDate', 'ps_CarRegEra', 'ps_CarRegEraYear')
    if _sm == 3:
        _head, _sep, _tail = car_serial_no.partition('-')
        _want += [('ps_CarSerialNo', car_serial_no), ('ps_CarSerialNoHead', _head if _sep else car_serial_no),
                  ('ps_CarSerialNoTail', _tail if _sep else ''), ('ps_CarMouldNo', model_desig),
                  ('ps_CarKindNo', category_num)]
        if car_reg_date != '00000000':
            _want += [('ps_CarRegDate', car_reg_date), ('ps_CarRegEra', reg_era), ('ps_CarRegEraYear', reg_era_year)]
        else:
            _want += [('ps_CarRegDate', ''), ('ps_CarRegEra', ''), ('ps_CarRegEraYear', '')]
    else:
        _want += [(c, '') for c in _ps + ('ps_YearName',)]
    _want += [('ms_CarSerialNoHead', ''), ('ms_CarSerialNoTail', '')]
    _upd = [(c, v) for c, v in _want if c in _cols and (v or not merge_mode)]
    if _upd:
        cur.execute('UPDATE CarSearch SET ' + ', '.join(f'{c}=?' for c, _ in _upd), [v for _, v in _upd])


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
    car_reg_date  = normalized_reg_date(cust.get('car_reg_date', ''))
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
        'Total':         str(safe_int(grand_total)),
        # 作成日を更新しないと、どの見積にもテンプレート作成時の日付が残る
        'CreatedDate':   now_jst().strftime('%Y/%m/%d'),
        # 事故・保険情報。DBに書くのと同じ値をヘッダXMLにも書かないと、
        # 過去のNEOをテンプレートに使ったとき前の案件の値が残ってしまう。
        'AcceptNo':      cp932_trim(ins.get('accept_no', ''), 37),
        # 事故日は 'YYYY/MM/DD'（実機 307 本: 日付あり 67 本すべてこの形、無ければ空。vendor と同じ。バグハント 3 回目 L14）
        'AccidentDate':  _slash_date(_normalize_date8(ins.get('accident_date', ''))),
        'AdjusterName':  cp932_trim(ins.get('adjuster_name', ''), 20),
        'Note1':         cp932_trim(
            re.sub(r'\s+', ' ', safe_str(ins.get('note1', ''))).strip(), 40),
        # 入出庫日はヘッダ XML には書かない（実機 307 本すべて空。DB の FileInfo には書く。vendor と同じ。L14）
        'GarageInDate':  '',
        'GarageOutDate': '',
        'CarMouldNo':    _t['model_desig'],
        'CarKindNo':     _t['category_num'],
        'ColorCode':     _t['color_code'],
        # 以下はアプリが値を持たない案件固有欄。書かずに放置すると、
        # 過去の .neo をテンプレートにしたとき前の案件の立会者名・伝票番号・
        # 備考・グレードがそのまま新しい見積に残る（立会者名は個人情報）。
        # マージモードでは空値はスキップされるので、テンプレート保持は壊れない。
        'CustomerName2':        '',
        'TicketNo':             cp932_trim(policy_no_or_accept(ins, read_xml_tag(text, 'TicketNo'), merge_mode), 20),   # 証券番号（vendor と同じ。DB の Insurance.PolicyNo と揃える。空なら事故番号・受付番号）
        'Note2':                '',
        'Note3':                '',
        'ii_CustomerName':      cp932_trim(ins.get('contractor_name', ''), 20),  # 契約者（vendor と同じ。DB の Insurance.ContractorName と揃える）
        'ii_PresenceDate':      '',
        'ii_AgreedDate':        '',
        'ii_RepairDays':        '',
        'ii_TimePrice':         '',
        'GradeName':            '',
        'CarYearName':          '',
        'BodyName':             '',
        'FVariationNameByUser': '',
    }
    term_month = term_date[4:6] if len(term_date) >= 6 else ''
    term_day   = term_date[6:8] if len(term_date) >= 8 else ''
    if (term_date and term_date != '00000000'
            and term_month.isdigit() and term_month != '00'
            and term_day.isdigit() and term_day != '00'):
        # タグ名に Era と付くが、実機は西暦の 'YYYY/MM/DD' を書いている
        # （実機70件すべて）。和暦の「令和10年2月25日」という形は無かった。
        tag_values['CarTermEraDate'] = f'{term_date[:4]}/{term_month}/{term_day}'
    else:
        tag_values['CarTermEraDate'] = ''
    reg_era, reg_era_year = get_era_info(car_reg_date)
    reg_month = car_reg_date[4:6] if len(car_reg_date) >= 6 else ''
    # get_era_info は元号を決められないとき（空・不正・大正以前）に
    # ('令和', '0000') を返す。これは「不明」の印であって令和0年ではない。
    # そのまま書くと初度登録が「令和0年0月」として保険会社に出る。
    # normalized_reg_date が元号を決められない値を '00000000' に落としている。
    _reg_known = car_reg_date != '00000000'
    if _reg_known:
        # 初度登録は西暦の 'YYYY/MM'（実機70件すべて）。
        tag_values['CarRegistedDate'] = f'{car_reg_date[:4]}/{reg_month}'
        # 合成文字列だけ書いて構造化タグを空のまま残すと、同じ .neo の中に
        # 初度登録が2通り入る（DB側は Customer.CarRegDate に8桁で入っている）。
        # 年（和暦）と月はゼロ埋めしない。実機は 1〜2桁で、
        # '0006' のような4桁ゼロ埋めは70件中1件も無かった。
        tag_values['CarRegistedDateYear']  = str(int(reg_era_year))
        tag_values['CarRegistedDateMonth'] = str(int(reg_month))
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
        # 総額と入出庫日はマージでも必ず書く（総額 0 のときにテンプレートの総額が残っていた。バグハント 3 回目 L15）
        if merge_mode and not value and tag_name not in ('Total', 'GarageInDate', 'GarageOutDate'):
            continue  # マージモード: 空値はスキップ（テンプレートの既存値を保持）
        # 値に & や < が入るとXMLが壊れるためエスケープする（法人名の「＆」等）
        text = replace_xml_tag(text, tag_name, _xml_escape(value))
    return neo_header.encode_cp932w(text)


# ============================================================
# 内部ファイル更新: AnSvImge.ini（INI）
# ============================================================

def update_imge_ini(orig_bytes, em_db_bytes):
    """実体の AnSvMail.ini（NEOMAIL2。このアプリのキーでは 'AnSvImge.ini'）を、書き終わった顧客・保険 DB の値から作り直す。

    仕様（NEO_FILE_SPEC_COMPLETE.md §2「全キー書換」）と実機 307 本: CustomerName = Insurance.ContractorName（契約者。
    顧客名ではない）、TicketNo = PolicyNo（証券番号）、AgreedName = ConsultantFactory（相手工場）、CarName = Car.CarNameByUser、
    AccidentDate = Insurance.AccidentDate（無ければ 00000000）。以前は顧客名を書き、証券番号・相手工場は書いておらず、
    テンプレートの前の案件の値が残っていた（バグハント 3 回目 L1/L4）。DB から読むので、マージ（テンプレートの値を
    残す）でも DB と食い違わない。DB が読めなければ全部空で作る（前の案件の値を残さない）。
    """
    vals = {'CustomerName': '', 'CarNoDepartment': '', 'CarNoDivision': '', 'CarNoBusiness': '', 'CarNoSerial': '',
            'TicketNo': '', 'AcceptNo': '', 'AccidentDate': '00000000', 'AgreedName': '', 'CarName': ''}
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    try:
        tf.write(em_db_bytes or b'')
        tf.close()
        conn = sqlite3.connect(tf.name)
        try:
            def _one(sql):
                try:
                    return conn.execute(sql).fetchone()
                except sqlite3.Error:
                    return None
            c = _one('SELECT CarRegNoDepartment, CarRegNoDivision, CarRegNoBusiness, CarRegNoSerial FROM Customer')
            i = _one('SELECT ContractorName, PolicyNo, AccidentDate, ConsultantFactory FROM Insurance')
            f = _one('SELECT AcceptNo FROM FileInfo')
            k = _one('SELECT CarNameByUser FROM Car')
        finally:
            conn.close()
        if c:
            vals['CarNoDepartment'], vals['CarNoDivision'], vals['CarNoBusiness'], vals['CarNoSerial'] = (safe_str(x) for x in c)
        if i:
            vals['CustomerName'], vals['TicketNo'] = safe_str(i[0]), safe_str(i[1])
            _acc = safe_str(i[2])
            vals['AccidentDate'] = _acc if re.fullmatch(r'\d{8}', _acc) else '00000000'
            vals['AgreedName'] = safe_str(i[3])
        if f:
            vals['AcceptNo'] = safe_str(f[0])
        if k:
            vals['CarName'] = safe_str(k[0])
    except Exception:
        pass
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass
    lines = ['[General]', 'Signature=NEOMAIL2', '[Audaneo2]']
    lines += [f'{key}={_ctrl_to_space(v)}' for key, v in vals.items()]
    return neo_header.encode_cp932w('\r\n'.join(lines) + '\r\n')


def _slash_date(d8) -> str:
    """'YYYYMMDD' → 'YYYY/MM/DD'。空・'00000000' は ''"""
    d8 = str(d8 or '')
    return f'{d8[:4]}/{d8[4:6]}/{d8[6:8]}' if re.fullmatch(r'\d{8}', d8) and d8 != '00000000' else ''


def _ctrl_to_space(value) -> str:
    """制御文字（改行・タブ・NUL など）だけを空白に置き換える。空白を詰めたり前後を落としたりはしない
    （明細の ERParts と AnSMB で同じ値を書くため。_strip_control_chars は空白も詰める）"""
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(value if value is not None else ''))


def _reset_note_flags(note_bytes, details_db_bytes, merge_mode=False):
    """実体の AnNote.ini（このアプリのキーでは 'AnFlInfo'）の [Reserve] / [Comment] Flag を、書き終わった明細から数える
    （vendor と同じ）。過去の案件の .neo をテンプレートにすると Flag=1 が残り、保留・コメントの無い見積に印が付いていた。
    非マージでは備考（Note=）も空にする（バグハント 3 回目 L1）"""
    if not note_bytes:
        return note_bytes
    has_res = has_com = False
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    try:
        tf.write(details_db_bytes or b'')
        tf.close()
        conn = sqlite3.connect(tf.name)
        try:
            has_res = conn.execute('SELECT COUNT(*) FROM ERParts WHERE ReserveFlag=1').fetchone()[0] > 0
            has_com = conn.execute('SELECT COUNT(*) FROM ERParts WHERE CommentFlag=1').fetchone()[0] > 0
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    except Exception:
        pass
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass
    t = note_bytes.decode('cp932', errors='replace')
    t = re.sub(r'(\[Reserve\]\s*\r?\n(?:[^\[]*?\r?\n)??Flag\s*=)[^\r\n]*',
               lambda m: m.group(1) + ('1' if has_res else '0'), t, count=1)
    t = re.sub(r'(\[Comment\]\s*\r?\n(?:[^\[]*?\r?\n)??Flag\s*=)[^\r\n]*',
               lambda m: m.group(1) + ('1' if has_com else '0'), t, count=1)
    if not merge_mode:
        t = replace_ini_value(t, 'Note', '')
    return neo_header.encode_cp932w(t)


_ADAS_WORK_BLANK = '[ADASWork]\r\nIdx1.PartsCode=``\r\nIdx1.ItemName=``\r\nIdx1.Comment=``\r\n'


def _reset_adas_work(ini_bytes):
    """実体の AnSvEm0001Ex.db（INI。このアプリのキーでは 'AnSvEm0001.sld'）の [ADASWork] を雛形の空の 1 件に戻す。
    ADAS の作業は予備明細（ReserveERParts）と対で、予備明細はどちらのモードでも空に戻している（バグハント 3 回目 L1）"""
    if not ini_bytes:
        return ini_bytes
    t = ini_bytes.decode('cp932', errors='replace')
    m = re.search(r'(?m)^\[ADASWork\][^\r\n]*(?:\r?\n|$)(?:(?!\[)[^\r\n]*\r?\n)*(?:(?!\[)[^\r\n]+$)?', t)
    if not m or m.group(0).replace('\r\n', '\n') == _ADAS_WORK_BLANK.replace('\r\n', '\n'):
        return ini_bytes
    return neo_header.encode_cp932w(t[:m.start()] + _ADAS_WORK_BLANK + t[m.end():])


def _clear_image_db(db_bytes):
    """画像 DB（このアプリのキーでは 'AnSvIf0001.sld'）の画像を消す。非マージ（ベタ打ち）で過去の案件の .neo を
    テンプレートにすると、前の案件の写真が新しい見積に入ったまま出ていた（バグハント 3 回目 L1）。
    消した画像がファイルの空き領域に残らないよう secure_delete で消す"""
    if not db_bytes or db_bytes[:15] != b'SQLite format 3':
        return db_bytes
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    try:
        tf.write(db_bytes)
        tf.close()
        conn = sqlite3.connect(tf.name)
        try:
            conn.execute('PRAGMA secure_delete=ON')
            _tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            _n = 0
            for _t in ('Image', 'ImageAnnotation'):
                if _t in _tables:
                    _n += conn.execute(f'SELECT COUNT(*) FROM {_t}').fetchone()[0]
            if not _n:
                return db_bytes
            for _t in ('Image', 'ImageAnnotation'):
                if _t in _tables:
                    conn.execute(f'DELETE FROM {_t}')
            conn.commit()
        finally:
            conn.close()
        with open(tf.name, 'rb') as f:
            return f.read()
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass


def _reset_image_ini(ini_bytes):
    """実体の AnSvImge.ini（画像の目録。このアプリのキーでは 'AnSvIg0001.sld'）を、画像の無い雛形と同じ形に戻す（L1）"""
    if not ini_bytes:
        return ini_bytes
    t = ini_bytes.decode('cp932', errors='replace')
    i = t.find('[ImageSections]')
    blank = '[ImageSections]\r\n\r\n[Image1]\r\n'
    if i < 0 or t[i:].replace('\r\n', '\n') == blank.replace('\r\n', '\n'):
        return ini_bytes
    return neo_header.encode_cp932w(t[:i] + blank)


# ============================================================
# 内部ファイル更新: AnNote.ini（明細簡易表現）
# ============================================================

def generate_annote(rows):
    """144B固定長（本体142B＋CRLF）× 行数 の AnNote.ini を生成

    rows は _update_ansmb_impl が ERParts に実際に書いた値
    （line_no / name / qty / parts_code / disposal_code / parts_no）。
    生の items から組み直すと、マスタ名への置換や「※」付与、
    数量ブランクが反映されず、同じ行なのに DB と AnNote で
    品名・数量が食い違う。

    レコードの中身は実機の .neo 202件から割り出した:
        [  0:  8] 行番号（8桁ゼロ埋め）
        [  8: 12] 部品コード（参照番号4桁。手入力行は空欄）
        [ 12: 13] 枝番（-1 は空欄）
        [ 13: 14] 作業区分コード（区分なしは空欄）
        [ 14: 38] 品名
        [ 38: 62] 標準品名（マスタ由来のときだけ）
        [ 62: 80] 品番
        [ 80: 98] 標準品番（マスタ由来のときだけ）
        [ 98:100] 数量（2桁）
        [100:105] 由来。マスタ由来 '00000' / 手入力 ' 0000'
        [127:133] 'F99999'
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
        # 部品コード・作業区分・品番は ERParts と同じ値を書く。
        # 以前はここを空白のままにしていたので、明細テーブルには
        # コードが入っているのに注記だけ空、という食い違いが起きていた。
        _pc = str(row.get('parts_code', '') or '')
        if len(_pc) == 4 and _pc.isdigit():
            for j, c in enumerate(_pc):
                line[8 + j] = ord(c)
        _dc = row.get('disposal_code', -1)
        if isinstance(_dc, int) and 0 <= _dc <= 9:
            line[13] = ord(str(_dc))
        # バイト数で単純に切ると2バイト文字の途中で割れ、末尾に
        # 復号できない片割れが残る。文字境界で切り詰める。
        # 改行やタブが混ざると142B固定長レコードが行単位で割れる。
        # 見積書の部品名が2行に折り返された表をCSV化すると普通に起きる。
        # 品名欄は [14:38] の24バイト。ERParts.PartsName と同じ幅で切る。
        # ここを広く取ると、隣の標準品名欄（[38:62]）へはみ出す。
        name_bytes = neo_header.encode_cp932w(
            cp932_trim(_ctrl_to_space(name), _ERPARTS_WIDTH['PartsName']))
        for j, b in enumerate(name_bytes):
            line[14 + j] = b
        # 品番欄は [62:80] の18バイト。
        _pno = neo_header.encode_cp932w(
            cp932_trim(_ctrl_to_space(str(row.get('parts_no', '') or '')),
                       _ERPARTS_WIDTH['PartsNo']))
        for j, b in enumerate(_pno):
            line[62 + j] = b
        # 注記の数量欄は2桁固定。3桁以上は入らないので丸めるしかないが、
        # 明細テーブルには150、注記には99と書かれ、同じ .neo の中で
        # 数量が食い違う。丸めたことは画面で知らせる（下の警告で拾う）。
        qty_str = f'{min(qty, 99):02d}'
        line[98] = ord(qty_str[0])
        line[99] = ord(qty_str[1])
        # [100:105] は行の由来。実機は部品マスタから採った行が '00000'、
        # 手で打った行が ' 0000'（先頭が空白）だった。自由入力行 83件は
        # すべて ' 0000'。以前ここに書いていた '90000' は、実機の
        # 202ファイル・11,254行のどこにも現れない値だった。
        for j, c in enumerate(('0' if _pc else ' ') + '0000'):
            line[100 + j] = ord(c)
        for j, c in enumerate('F99999'):
            line[127 + j] = ord(c)
        lines.append(bytes(line) + b'\r\n')
    return b''.join(lines)


def update_file_info(orig_bytes):
    """ファイル情報（作成日）を今日に更新する。

    ※ ここで扱うキー 'AnDBVersion.ini' は**中身の実体とは違う名前**。
      NEO のファイルテーブルは「名前の次に来る size/offset が、その名前の
      ファイルのもの」ではなく **次のエントリのもの**という構造をしていて、
      このアプリの parse_entries は当該エントリのものとして読んでいる。
      そのため extract_files が返すキーは実体より1つ前にずれている:

          このアプリのキー          実体
          AnCooperate.txt      →  AnDBVersion.ini
          AnDBVersion.ini      →  AnFlInfo         ← ここで扱うもの
          AnFlInfo             →  AnNote.ini
          AnNote.ini           →  AnSMB.txt（144B固定長の明細）
          AnSMB.txt            →  AnSvEm0001.sld（明細・費用・合計のSQLite）
          AnSvEm0001Ex.db      →  AnSvIf0001.sld（顧客・車両・保険のSQLite）
          AnSvImge.ini         →  AnSvMail.ini（NEOMAIL2 の INI）
          AnSvMail.ini         →  <見積名>.xml（ヘッダXML）

      実機の .neo は各ファイルの先頭に「; ファイル名 : …(本当の名前)」と
      書いてあり、それで確認した（2026-09-10）。
      読み書きが同じずれ方をしているのでバイト列は往復で壊れない。
      **名前を直すには読み・書き・呼び出しを同時に直す必要がある**ので、
      ここでは直さず、取り違えないようにこの対応表を残す。

    実体の AnFlInfo は `[General] NewCreate=<作成日>` を持つ。
    このアプリは今までここを触っておらず、生成物すべてが
    テンプレートの作成日（2026/03/11）のままだった。
    """
    if not orig_bytes:
        return orig_bytes
    try:
        text = orig_bytes.decode('cp932', errors='replace')
    except Exception:
        return orig_bytes
    if 'NewCreate' not in text:
        return orig_bytes      # 想定と違う中身。触らない
    # 車種データ版（AnVer.db）は書き換えない。このアプリは部品価格を
    # 見積書から取っていて ADDATA の版を使っていないので、
    # 使っていない版を名乗ることになる。
    # `.*$` にすると行末の CR まで食べてしまい、この行だけ改行が LF になる。
    # 他の行が CRLF なので、1 行だけ改行の違うファイルができる。
    text = re.sub(r'^NewCreate[^\r\n]*',
                  'NewCreate=' + now_jst().strftime('%Y/%m/%d'),
                  text, count=1, flags=re.M)
    return neo_header.encode_cp932w(text)


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
    # 消費税の端数処理はテンプレート（工場のコグニ設定）に従う。切り捨ての工場の .neo をテンプレートにしたら切り捨てで計算し、
    # 設定もそのまま残す（以前は四捨五入で計算して設定だけ残す／設定も四捨五入に上書きしていた。O3・レビュー）
    _tax_round, _tax_flag = _template_tax_round(files.get('AnSvEm0001Ex.db') or b'')
    files['AnSMB.txt'], total_parts, total_wages, grand_total, _annote_rows = update_ansmb(
        files['AnSMB.txt'], normalized_items, short_parts_wage,
        expenses=expenses, is_tax_inclusive=is_tax_inclusive, is_beta_mode=is_beta_mode,
        tax_round=_tax_round,
    )
    files['AnNote.ini']       = generate_annote(_annote_rows)
    files['AnSvEm0001Ex.db']  = update_em_db(
        files['AnSvEm0001Ex.db'], customer_info, insurance_info, estimated_date,
        is_tax_inclusive=is_tax_inclusive, merge_mode=merge_mode, tax_arrange_flag=_tax_flag,
    )
    files['AnSvMail.ini'] = update_mail_ini(files['AnSvMail.ini'], customer_info, grand_total,
                                            insurance_info=insurance_info, merge_mode=merge_mode)
    # 実体の AnSvMail.ini（NEOMAIL2）は書き終わった DB から作り直す（DB と同じ値。バグハント 3 回目 L1/L4）
    files['AnSvImge.ini'] = update_imge_ini(files['AnSvImge.ini'], files['AnSvEm0001Ex.db'])
    # 実体の AnNote.ini の Flag は明細から数え、[ADASWork] は予備明細と一緒に空へ戻す（L1）
    if 'AnFlInfo' in files:
        files['AnFlInfo'] = _reset_note_flags(files['AnFlInfo'], files['AnSMB.txt'], merge_mode=merge_mode)
    if 'AnSvEm0001.sld' in files:
        files['AnSvEm0001.sld'] = _reset_adas_work(files['AnSvEm0001.sld'])
    if not merge_mode:
        # ベタ打ちは「テンプレートの工場名・車種の設定だけ引き継ぐ」。前の案件の写真は持ち込まない（L1）
        if 'AnSvIf0001.sld' in files:
            files['AnSvIf0001.sld'] = _clear_image_db(files['AnSvIf0001.sld'])
        if 'AnSvIg0001.sld' in files:
            files['AnSvIg0001.sld'] = _reset_image_ini(files['AnSvIg0001.sld'])
    files['AnDBVersion.ini'] = update_file_info(files.get('AnDBVersion.ini', b''))
    neo_data = repack_neo(template_data, files, mgmt, entries)
    # コグニセブンの「既存見積」一覧は、内包ファイルではなく
    # ファイル先頭 424 バイトの管理領域から 登録番号・顧客名・車名 を読む。
    # ここを書かないと、一覧に並んでも「どの車の誰の見積か」が空欄で出る。
    # （2026-09-10 に実機で確認。内包ファイルを実機のものと差し替えても
    #   直らず、先頭424Bを差し替えたときだけ直った）
    neo_data = _apply_neo_header(neo_data, files['AnSvEm0001Ex.db'], files['AnSMB.txt'])
    return neo_data, total_parts, total_wages, grand_total


def _summary_totals(ansmb_bytes):
    """管理領域に書く5つの金額を、生成済みの見積本体から読む。

    実機の並びは [部品計, 工賃計, 塗装計, 諸経費計, 総額(税込)]。
    実機 202 件を復号して Total テーブルと突き合わせて確認した。

    諸経費計には**非課税ぶんも入れる**。非課税は課税額計(SubTotal)には
    入らないが、この欄には入っていた（非課税のある実機 12 件すべてで
    ヘッダの値 = 課税諸経費 + 非課税 だった）。
    """
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    try:
        tf.write(ansmb_bytes)
        tf.close()
        conn = sqlite3.connect(tf.name)
        try:
            r = conn.execute(
                'SELECT ms_PartsTotalOutTax, ms_WageTotalOutTax, pn_TotalOutTax,'
                ' hy_PartsTaxTotalOutTax, hy_WageTaxTotalOutTax,'
                ' hy_PartsNoTaxTotalOutTax, hy_WageNoTaxTotalOutTax,'
                ' Total FROM Total').fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass
    if not r:
        return None
    v = [safe_int(x) for x in r]
    return [v[0], v[1], v[2], v[3] + v[4] + v[5] + v[6], v[7]]


def _neo_header_source(em_db_bytes):
    """管理領域に書く 顧客名・車名・登録番号・協定工場名 を、生成後の見積本体から読む。

    マージモードかどうかで場合分けせず、**出来上がった見積そのもの**を見る。
    こうしておけば「一覧に出る名前」と「開いたときの名前」は必ず一致する。
    テンプレート側の管理領域を頼りにすると、本体には顧客が入っているのに
    管理領域だけ空、という古いアプリ製の .neo をテンプレートにしたときに
    一覧が空欄のままになる。
    """
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    try:
        tf.write(em_db_bytes)
        tf.close()
        conn = sqlite3.connect(tf.name)
        try:
            cur = conn.cursor()
            cur.execute('SELECT Name1, CarRegNoDepartment, CarRegNoDivision,'
                        ' CarRegNoBusiness, CarRegNoSerial FROM Customer')
            c = cur.fetchone() or ('', '', '', '', '')
            try:
                cur.execute('SELECT CarNameByUser, CarName FROM Car')
                _car = cur.fetchone() or ('', '')
            except sqlite3.Error:
                _car = ('', '')
            try:
                cur.execute('SELECT ConsultantFactory FROM Insurance')
                _ins = cur.fetchone() or ('',)
            except sqlite3.Error:
                _ins = ('',)
        finally:
            conn.close()
    except Exception:
        return None
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass
    # 車名は実機の管理領域が CarNameByUser と同じ値を持っていた
    # （実機 04011406.neo で確認。末尾の全角スペースまで一致）
    car_name = safe_str(_car[0]) or safe_str(_car[1])
    return {
        'name1': safe_str(c[0]),
        'car_name': car_name,
        'carno': (safe_str(c[1]), safe_str(c[2]), safe_str(c[3]), safe_str(c[4])),
        'agreed': safe_str(_ins[0]),
    }


def _apply_neo_header(neo_data, em_db_bytes, ansmb_bytes):
    """先頭424Bの管理領域に、この見積の 顧客名・車名・登録番号・金額 を書く"""
    src = _neo_header_source(em_db_bytes)
    if src is None:
        # 本体が読めないなら触らない。空欄で一覧に出るだけで、見積の中身は無事。
        return neo_data
    try:
        _now = now_jst()
        return neo_header.apply(
            neo_data,
            agreed=src['agreed'],
            name1=src['name1'],
            car_name=src['car_name'],
            created=_now.date(),
            # 読めなかったときに None を渡すと、`neo_header.build` は金額欄に
            # 触らず、**テンプレート（＝前案件）の金額がそのまま残る**。
            # 顧客名だけ新しくて金額は前案件、という .neo は出してはいけない。
            # ゼロで埋めれば一覧に空欄で出るだけで、間違った金額は出ない。
            totals=_summary_totals(ansmb_bytes) or [0, 0, 0, 0, 0],
            carno=src['carno'],
            saved=_now.replace(tzinfo=None),
        )
    except Exception:
        # 管理領域を書けなくても見積本体は正しい。空欄で出るだけなので握りつぶす。
        return neo_data


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
        # [頁ページ] と書くと文字クラスになり「ー」「ペ」「ジ」1文字でも当たる。
        # 「数量 1/2ー」「ﾊﾞﾝﾊﾟｰ 3/4ペ」のような明細行をページ番号と誤検出し、
        # 見積のページ順を勝手に入れ替えてしまうため、交替（|）で書く。
        m = re.search(r'(\d+)\s*[/／]\s*(\d+)\s*(?:頁|ページ|ﾍﾟｰｼﾞ)', text)
        if m:
            cur_page = int(m.group(1))
            page_numbers.append((idx, cur_page, int(m.group(2))))
            continue
        # パターン3: "頁1/2" 逆形式（ここも文字クラスではなく交替で書く）
        m = re.search(r'(?:頁|ページ|ﾍﾟｰｼﾞ)\s*(\d+)\s*[/／]\s*(\d+)', text)
        if m:
            cur_page = int(m.group(1))
            page_numbers.append((idx, cur_page, int(m.group(2))))
            continue
        # 検出できないページ
        page_numbers.append((idx, None, None))

    # 全ページでページ番号が検出でき、かつ番号の並びに矛盾が無いときだけ並び替える。
    # 確かめないと、明細の中の分数表記を拾った誤検出でページ順を壊す。
    #   - 総ページ数の表記が全ページで同じ（"1/2" と "2/3" が混ざるのは誤検出）
    #   - ページ番号が重複しない
    #   - 総ページ数が実際のページ数以上
    # 「1..N がすべて揃うこと」は条件にしない。FAX送付状を除いたあとは
    # 残りが 2/3・3/3 のようになり、正当な並べ替えができなくなるため。
    _nums = [pn[1] for pn in page_numbers]
    _totals = {pn[2] for pn in page_numbers}
    _consistent = (
        page_numbers
        and all(pn[1] is not None for pn in page_numbers)
        and len(_totals) == 1
        and next(iter(_totals)) >= len(pages)
        and len(set(_nums)) == len(_nums)
        # 番号が総ページ数の範囲に収まり、かつ抜けの無い連続した並びであること。
        # 「3/3 と 1/3 の2枚」（間が抜けている）や「4/3」（3ページ文書に4頁目）は
        # 検出そのものが怪しいので、並べ替えずに人が気づけるようにする。
        and all(1 <= n <= next(iter(_totals)) for n in _nums)
        and (max(_nums) - min(_nums) + 1) == len(_nums)
    )
    if _consistent:
        original_order = [pn[0] for pn in page_numbers]
        page_numbers.sort(key=lambda x: x[1])
        new_order = [pn[0] for pn in page_numbers]
        if new_order != original_order:
            import sys
            print(f"[INFO] ページ順序を自動修正: {[p[1] for p in page_numbers]} "
                  f"(物理順 {original_order} → 文書順 {new_order})", file=sys.stderr)
        return [pages[pn[0]] for pn in page_numbers]

    return pages


# Gemini に 1 回で送れる大きさ。公式（ai.google.dev の Files API の説明、2026-09 に確認）は「要求全体が 100MB を超えるとき・
# PDF は 50MB を超えるときは Files API を使う」。PDF の 50MB から余裕を見て 45MB。これを超えるファイルは送っても失敗するうえ、
# 送信の組み立てでメモリがファイルの何倍にも膨れ、共有プロセスごと落ちうる（P1-1/P8）
GEMINI_MAX_INLINE_BYTES = 45 * 1024 * 1024
# 応答を待つ上限（ミリ秒）。無いと、応答しない宛先で画面が止まったままになる（P7）。長い明細の返事（出力 65,536 トークン）が
# 3 分を超えて毎回切れないよう 10 分（読み手 GeminiReader と同じ）。締め切りで切れたら送り直さない（また 10 分待たせない）
GEMINI_TIMEOUT_MS = 600_000


@st.cache_resource(max_entries=16)
def _get_genai_client(api_key):
    """google.genai クライアントを取得（セッション間で再利用）"""
    from google import genai
    from google.genai import types
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))


def _gemini_ready_bytes(file_bytes, mime_type):
    """Gemini に送れる形にする。TIFF・BMP・GIF は Gemini の対応外なので PDF にする（多ページの FAX TIFF は全ページ。
    P11）。大きすぎるファイルは送る前に日本語で断る（P1-1）"""
    mt = str(mime_type or '').lower()
    if mt in ('image/tiff', 'image/tif', 'image/bmp', 'image/x-ms-bmp', 'image/gif'):
        try:
            from neo_skill import llm as _nllm
            file_bytes, mt = _nllm.image_to_pdf(file_bytes), 'application/pdf'
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"画像を開けません（{mt}）: {str(e)[:120]}")
    if len(file_bytes or b'') > GEMINI_MAX_INLINE_BYTES:
        raise ValueError(
            f"ファイルが大きすぎます（{len(file_bytes) / 1024 / 1024:.1f}MB。AI に送れるのは "
            f"{GEMINI_MAX_INLINE_BYTES // 1024 // 1024}MB まで）。PDF ならページを減らす、写真なら解像度を下げてから入れ直してください。")
    return file_bytes, mt


def _is_gemini_timeout(e, msg: str = '') -> bool:
    """締め切り切れ（クライアント側の httpx.TimeoutException、サーバー側の 504 DEADLINE_EXCEEDED）か。接続の失敗
    （ConnectError「Connection timed out」）は含めない（送り直せば通ることがある。文言では見分けない。レビュー 3 周目）"""
    try:
        import httpx
        if isinstance(e, httpx.TimeoutException):
            return True
    except ImportError:  # pragma: no cover
        pass
    return getattr(e, 'code', None) == 504 or 'DEADLINE_EXCEEDED' in (msg or str(e)).upper()


def call_gemini(api_key, file_bytes, mime_type, prompt_text, model_name=None, use_json_mode=False):
    """Gemini APIにファイルを送信して解析結果テキストを取得（最大3回リトライ）
    use_json_mode=True の場合、構造化JSON出力モードを使用（解析精度向上）
    """
    from google.genai import types
    file_bytes, mime_type = _gemini_ready_bytes(file_bytes, mime_type)
    client = _get_genai_client(api_key)
    model = model_name or GEMINI_MODEL
    file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    config = {"temperature": 0.0, "max_output_tokens": 65536}
    if use_json_mode:
        config["response_mime_type"] = "application/json"
    # 例外そのものは持ち越さない（送信内容を抱えたまま残り、メモリが解放されない。P1-1）。文言だけ持つ
    last_msg = ''
    fatal_msg = None
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
            last_msg = str(e)
            _code = getattr(e, 'code', None)
            # モデルが無い（404）・クォータ切れ（429）は、1秒待って同じモデルに
            # 投げ直しても絶対に通らない。とくに 429 はリトライ自体がクォータを
            # さらに食う。ここで即座に諦めて、呼び出し側のモデル切り替えに任せる。
            # 400（不正な要求・対応外の形式・大きすぎる）・401/403（キー）など 4xx も同じく送り直さない（P11）
            if (_is_model_unavailable_error(last_msg) or _is_quota_error(last_msg)
                    or (isinstance(_code, int) and 400 <= _code < 500 and _code != 408)):
                fatal_msg = f"Gemini API呼び出しに失敗しました: {last_msg[:500]}"
            elif _is_gemini_timeout(e, last_msg):
                fatal_msg = ('Gemini の応答が時間内（10 分）に返りませんでした。ページ数を減らすか、'
                             'しばらくしてからもう一度お試しください。')
        if fatal_msg:
            raise ValueError(fatal_msg)
        if attempt < 2:
            # 一時的な障害（500 など）は待って再送する。
            # 固定1秒だと復旧前に打ち切ることがあるので、少しずつ延ばす。
            import time; time.sleep(1 + attempt)
            continue
    raise ValueError(f"Gemini API呼び出しに失敗しました（3回試行）: {last_msg[:500]}")


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
# 画面で設定した Addata の場所を URL のクエリに残すときのキー。
# ここに残しておくと、そのURLをブックマークして別のPCで開いても同じ設定になる。
_QS_ADDATA_DIR = 'addata_dir'
_QS_ADDATA_URL = 'addata_url'
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


# 目録1本ぶんの最小の長さ（中央ディレクトリの見出し）。
# 本数の上限とかけ合わせて「目録の大きさ」の上限にする。
_ZIP_CD_ENTRY_MIN = 46


def _zip_declared_index(zip_src):
    """ZIP の目録が「何本・何バイト」と言っているかを、**開く前に**読む。

    `zipfile.ZipFile()` は組み立ての時点で目録を丸ごとメモリに載せる。
    しかも読み進める条件は**本数ではなく目録のバイト数**なので、
    「本数は小さく、バイト数は巨大」と書いておけば、
    本数の検査に辿り着く前にメモリを使い切らせられる。
    だから本数とバイト数の**両方**を先に見る。
    (本数, バイト数) を返す。分からないものは None
    （その場合は従来どおり開いてから数える）。
    """
    TAIL = 66 * 1024        # 目録の末尾はコメント込みで最大 64KB + 22B
    try:
        if isinstance(zip_src, (str, os.PathLike)):
            size = os.path.getsize(zip_src)
            with open(zip_src, 'rb') as f:
                f.seek(max(0, size - TAIL))
                tail = f.read()
        else:
            tail = bytes(zip_src)[-TAIL:]
    except OSError:
        return (None, None)
    i = tail.rfind(bytes.fromhex('504b0506'))       # EOCD
    if i < 0 or len(tail) - i < 22:
        return (None, None)
    n = int.from_bytes(tail[i + 10:i + 12], 'little')
    sz = int.from_bytes(tail[i + 12:i + 16], 'little')
    if n != 0xFFFF and sz != 0xFFFFFFFF:
        return (n, sz)
    # ZIP64。本数もバイト数も 8 バイトで別の場所に書いてある
    j = tail.rfind(bytes.fromhex('504b0606'))       # EOCD64
    if j >= 0 and len(tail) - j >= 48:
        return (int.from_bytes(tail[j + 32:j + 40], 'little'),
                int.from_bytes(tail[j + 40:j + 48], 'little'))
    return (None if n == 0xFFFF else n, None if sz == 0xFFFFFFFF else sz)


def _zip_declared_members(zip_src):
    """目録が言っている本数だけ返す（古い呼び出し向け）。"""
    return _zip_declared_index(zip_src)[0]


def extract_addata_zip(zip_src, dest_dir: str, max_total=None) -> tuple:
    """Addata の ZIP を dest_dir に安全に展開し、(ルートパス, 説明) を返す。

    ルートが見つからない場合は (None, 理由) を返す。
    ZIP の中身は利用者が持ち込む外部データなので、
    パス抜け（zip slip）・容量爆弾・シンボリックリンクを弾く。

    zip_src は**バイト列でもファイルの場所でもよい**。URL からの取り込みは
    300MB まで許すので、丸ごとメモリに置かずファイルのまま渡してもらう。
    """
    import zipfile
    dest_real = os.path.realpath(dest_dir)
    # max_total は「この1本で展開してよい量」。URL からの取り込みでは
    # 置いておける合計（_ADDATA_URL_CACHE_MAX_BYTES）に合わせる。
    # ここを緩くしておくと、1本で上限を超えたまま居座って回収できない。
    if isinstance(max_total, int) and max_total > 0:
        _cap = max_total
    else:
        _cap = ADDATA_ZIP_MAX_TOTAL_BYTES
    total = 0
    count = 0
    # 目録の大きさを**開く前に**見る。zipfile は組み立ての時点で目録を丸ごと
    # メモリに載せるので、下の本数の検査では間に合わない。
    # しかも読み進める条件は本数ではなく**目録のバイト数**なので、
    # 「本数は小さく、バイト数は巨大」と書かれた ZIP はバイト数で弾くしかない。
    _declared, _cd_size = _zip_declared_index(zip_src)
    if _declared is not None and _declared > ADDATA_ZIP_MAX_MEMBERS:
        return (None, f'ZIP内のファイル数が多すぎます（{_declared:,}件）')
    if _cd_size is not None and _cd_size > ADDATA_ZIP_MAX_MEMBERS * _ZIP_CD_ENTRY_MIN:
        return (None, 'ZIPの目録が大きすぎます（%d MB）'
                % (_cd_size // (1024 * 1024)))
    if isinstance(zip_src, (str, os.PathLike)):
        _src = zip_src              # ファイルの場所（URL からの取り込み）
    else:
        _src = io.BytesIO(zip_src)  # バイト列（画面からのアップロード）
    try:
        with zipfile.ZipFile(_src) as zf:
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
                if total > _cap:
                    return (None, 'ZIPの展開後サイズが大きすぎます（上限 %d MB）'
                            % (_cap // (1024 * 1024)))
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


def _forget_vendor_cache_under(path):
    """Addata の置き場（とその中の Addata の root）用に vendor が作った COM.CAB の展開キャッシュを消す。ZIP・取得URL の
    展開先は毎回 root が変わるので、置き場を消すときに一緒に消さないと 1 件約 5.6MB ずつ溜まり続ける（バグハント 3 回目 N8）"""
    try:
        from neo_skill import bridge as _nbr
    except Exception:       # noqa: BLE001
        return
    cands = [path]
    try:
        cands.append(os.path.realpath(path))
        for n1 in os.listdir(path)[:200]:
            p1 = os.path.join(path, n1)
            if os.path.isdir(p1):
                cands.append(p1)
                if not os.path.isdir(os.path.join(p1, 'COM')):
                    for n2 in os.listdir(p1)[:200]:
                        p2 = os.path.join(p1, n2)
                        if os.path.isdir(os.path.join(p2, 'COM')):
                            cands.append(p2)
    except OSError:
        pass
    for c in dict.fromkeys(cands):
        try:
            _nbr.forget_vendor_cache(c)
        except Exception:   # noqa: BLE001
            pass


def _rmtree_addata(path):
    """Addata の置き場を消す（vendor の展開キャッシュも一緒に。N8）"""
    import shutil as _sh
    _forget_vendor_cache_under(path)
    _sh.rmtree(path, ignore_errors=True)


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
                _rmtree_addata(rd)
        # 取得の途中で落ちた（プロセスごと止められた等）ぶんの ZIP も拾う。
        # ふだんは download_zip / _addata_from_url が自分で消している。
        for f in _glob.glob(os.path.join(base, 'addata_dl_*.zip')):
            rf = os.path.realpath(f)
            try:
                if (os.path.dirname(rf) == base and os.path.isfile(rf)
                        and now - os.path.getmtime(rf) > _ADDATA_TMP_TTL_SEC):
                    os.remove(rf)
            except OSError:
                pass
    except Exception:
        pass


# URL から取り込んだ Addata を置いておける合計。
# URL は誰でも仕込める（`?addata_url=…` を踏ませるだけ）ので、違う URL を
# 並べられると際限なく貯まって一時領域が尽き、**アプリごと止まる**。
# 本番のクラウドは一時領域が狭いので、合計で頭打ちにして古いものから捨てる。
_ADDATA_URL_CACHE_MAX_BYTES = 1536 * 1024 * 1024      # 1.5GB


def _dir_size(path):
    total = 0
    for base, _d, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(base, fn))
            except OSError:
                pass
    return total


# 「いまこのセッションが使っている」と印を置いておく時間。
# 印は Addata の場所を渡すときに打つが、そのあと OCR・解析・照合と続き、
# 大きなPDFだと数分かかる。**生成が終わる前に印が切れると、
# その最中に展開先を回収されうる**（部品コードだけ静かに落ちる）。
# 余裕を見て30分。掃除の側は6時間なので、残っても長くは居座らない。
_ADDATA_LEASE_SEC = 30 * 60
_ADDATA_LEASE_DIR = '.inuse'


def _addata_session_token():
    """このセッションを表す短い名前（印のファイル名に使う）。"""
    tok = st.session_state.get('_addata_session_token')
    if not tok:
        import uuid as _uuid
        tok = _uuid.uuid4().hex[:12]
        st.session_state['_addata_session_token'] = tok
    return tok


def _addata_mark_in_use(path):
    """「いま使っている」印を置く／打ち直す。

    Streamlit Cloud では複数の利用者が同じプロセス・同じ一時領域を使う。
    量による回収が、**別のセッションが生成に使っている最中の展開先**を
    消してしまうと、その場で照合が崩れる。印を見て避ける。
    """
    try:
        d = os.path.join(path, _ADDATA_LEASE_DIR)
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, _addata_session_token())
        with open(f, 'w', encoding='utf-8') as fp:
            fp.write('')
        os.utime(f, None)
    except Exception:
        pass


def _addata_in_use(path):
    """まだ新しい「使っている」印があるか。"""
    d = os.path.join(path, _ADDATA_LEASE_DIR)
    try:
        now = time.time()
        for fn in os.listdir(d):
            try:
                if now - os.path.getmtime(os.path.join(d, fn)) <= _ADDATA_LEASE_SEC:
                    return True
            except OSError:
                pass
    except OSError:
        pass
    return False


def _addata_url_cache_size(exclude=None):
    """URL から取り込んだぶんの合計。exclude はこれから置き換えるぶん。"""
    import glob as _glob
    total = 0
    try:
        base = os.path.realpath(tempfile.gettempdir())
        skip = os.path.realpath(exclude) if exclude else None
        for d in _glob.glob(os.path.join(base, 'addata_url_*')):
            rd = os.path.realpath(d)
            if os.path.dirname(rd) != base or not os.path.isdir(rd) or rd == skip:
                continue
            total += _dir_size(rd)
    except Exception:
        pass
    return total


def _evict_addata_url_cache(keep=None):
    """URL から取り込んだぶんの合計が上限を超えていたら、古いものから捨てる。

    捨てる順は「最後に使った時刻」が古いものから（`cached_root` が使うたびに
    更新時刻を打ち直している）。掃除（`_sweep_stale_addata_dirs`）は時間で
    消す係、こちらは量で消す係。

    **使っている最中のものは後回しにする。** 同じプロセスを複数の利用者が
    使うので、生成の最中に展開先を消されると照合がその場で崩れる。
    印（`.inuse`）が新しいものは、印の無いものを全部捨ててもまだ上限を
    超えている場合にだけ捨てる（一時領域が尽きるとアプリごと止まるため、
    最後は量を優先する）。
    """
    import glob as _glob, shutil as _sh
    try:
        base = os.path.realpath(tempfile.gettempdir())
        keep_real = os.path.realpath(keep) if keep else None
        free, busy = [], []     # 捨ててよいもの／使っている最中のもの
        total = 0               # 合計は keep も使用中も数える
        for d in _glob.glob(os.path.join(base, 'addata_url_*')):
            rd = os.path.realpath(d)
            if os.path.dirname(rd) != base or not os.path.isdir(rd):
                continue
            try:
                size = _dir_size(rd)
                mt = os.path.getmtime(rd)
            except OSError:
                continue
            total += size
            if rd == keep_real:
                continue
            (busy if _addata_in_use(rd) else free).append((mt, rd, size))
        if total <= _ADDATA_URL_CACHE_MAX_BYTES:
            return
        for _mt, rd, size in sorted(free) + sorted(busy):
            _rmtree_addata(rd)
            total -= size
            if total <= _ADDATA_URL_CACHE_MAX_BYTES:
                break
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
        _rmtree_addata(base)


def addata_setting(key):
    """画面で設定した Addata の場所を読む（URLのクエリ → セッションの順）。

    URL に残しておけば、**どのPCでもその URL を開くだけで同じ設定になる**。
    本番はクラウドで動いていて利用者のPCが見えないので、
    「設定を持ち歩ける」ことが実用上いちばん効く。
    """
    try:
        v = st.query_params.get(key)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ''
        if v:
            return str(v).strip()
    except Exception:
        pass
    return safe_str(st.session_state.get('_setting_' + key, '')).strip()


# 取得に失敗した URL を、これだけの間は叩き直さない。
# `find_addata_dir()` は1回の描き直しの中で何度も呼ばれ、描き直しのたびに
# また呼ばれる。失敗するURLをそのつど取りに行くと、上限120秒 × 呼ばれた回数だけ
# 画面が止まり、**設定を消すことすらできなくなる**。
_ADDATA_URL_FAIL_SEC = 60


def _addata_url_remember_failure(url, why):
    """取得に失敗したことを覚える（理由は画面に出す）。"""
    st.session_state['_addata_url_error'] = why
    st.session_state['_addata_url_failed'] = {
        'url': url, 'at': time.time(), 'why': why}


def _addata_url_recent_failure(url):
    """さっき失敗したばかりの URL か。そうなら理由を返す。"""
    rec = st.session_state.get('_addata_url_failed')
    if not isinstance(rec, dict) or rec.get('url') != url:
        return ''
    try:
        if time.time() - float(rec.get('at') or 0) > _ADDATA_URL_FAIL_SEC:
            return ''
    except (TypeError, ValueError):
        return ''
    return str(rec.get('why') or '取得できませんでした')


def _addata_url_forget_failure():
    st.session_state.pop('_addata_url_failed', None)
    st.session_state.pop('_addata_url_error', None)


def _addata_from_url(url):
    """設定された URL から Addata を取り込む。ルートを返す（失敗なら空）。

    同じ URL なら展開済みのものを使い回す。毎回落とし直すと数十MBを
    そのたびに転送することになり、画面が固まる。
    """
    url = safe_str(url).strip()
    if not url:
        return ''
    # 取得の間はモジュールを差し替えさせない。最大120秒かかり、その最中に
    # addata_settings を読み直されると、時間切れの見張り役が持ち場
    # （繋いでいる口を控えた入れ物）を失う。
    with _conversion_guard():
        import addata_settings as _as
        # 使ってよい URL かを**取り込み済みのものを使う前に**見る。
        # 後回しにすると、いま許されない宛先でも「前に取れているから」で
        # 通ってしまう。ここで見れば通信もせずに理由を返せる。
        #
        # 検査は **書かれたそのままの URL に対して先に**行う。正規化は
        # 共有リンクを組み立て直すので、たとえば Google ドライブの
        # `https://利用者名:合言葉@drive.google.com/file/d/…` は
        # 合言葉が落ちた形に化ける。後で検査すると「合言葉入りのURL」を
        # 受け付けてしまい、そのURLがブックマークとして残り続ける。
        for _cand in (safe_str(url).strip(), _as.normalize_share_url(url)):
            _ok, _why = _as.validate_url(_cand)
            if not _ok:
                st.session_state['_addata_url_error'] = _why
                return ''
        real = _as.normalize_share_url(url)
        # さっき失敗したばかりなら、また取りに行かない（画面が止まるため）
        _recent = _addata_url_recent_failure(real)
        if _recent:
            st.session_state['_addata_url_error'] = _recent
            return ''
        cached = _as.cached_root(real, _addata_is_valid)
        if cached:
            _mark = (_as.read_marker(real) or {}).get('base') or cached
            _addata_mark_in_use(_mark)
            return cached
        zip_path, why = _as.download_zip(real)
        if not zip_path:
            _addata_url_remember_failure(real, why)
            return ''
        _sweep_stale_addata_dirs()      # 時間で捨てる
        _evict_addata_url_cache()       # 量で捨てる
        # 空けたうえで、**いま置ける量**まで展開を許す。上限いっぱいを毎回許すと、
        # 既に上限近くまで埋まっているときに新しいぶんが丸ごと乗って超えてしまう。
        # 同じURLの古いぶんは、これから置き換わるので数に入れない。
        #
        # 取り込み直している最中だけは「古いぶん＋落とした ZIP＋新しいぶん」が
        # 同時に載るので、一時的に上限を超える。これは承知のうえ。
        # 先に古いぶんを消してしまうと、**それを使って生成している別のセッションの
        # 足元が崩れる**（部品コードだけ静かに落ちる）。落ち着いた状態では、
        # 展開のあとの回収で上限に戻る。
        _old_base = (_as.read_marker(real) or {}).get('base') or ''
        _room = _ADDATA_URL_CACHE_MAX_BYTES - _addata_url_cache_size(exclude=_old_base)
        if _room < 16 * 1024 * 1024:
            _addata_url_remember_failure(
                real, 'サーバの一時領域に空きがありません。'
                      'しばらく置いてからもう一度お試しください')
            try:
                os.remove(zip_path)
            except OSError:
                pass
            return ''
        # 展開先は取り込みごとに新しく作る。同じ URL をブックマークした利用者が
        # 同時に開くことがあるので、**使っている最中の展開先を消してはいけない**。
        # 古くなったものは掃除が回収する。
        dest = _as.new_payload_dir(real)
        import shutil as _sh
        try:
            os.makedirs(dest, exist_ok=True)
            # 印は**展開を始める前**に打つ。展開先は `addata_url_*` なので、
            # 展開している最中に別のセッションの回収に消されうる。
            _addata_mark_in_use(dest)
            root, why = extract_addata_zip(
                zip_path, dest,
                max_total=min(_ADDATA_URL_CACHE_MAX_BYTES, _room))
        except Exception as e:      # noqa: BLE001
            root, why = None, '展開に失敗しました: %s' % str(e)[:100]
        finally:
            try:
                os.remove(zip_path)
            except OSError:
                pass
        if not root:
            _rmtree_addata(dest)
            _addata_url_remember_failure(real, why)
            return ''
        # 展開したぶんも数に入れてもう一度均す。展開の前だけだと、
        # いま入れたものが上限の外に置かれたままになる。
        _evict_addata_url_cache(keep=dest)
        _addata_mark_in_use(dest)
        _as.remember_root(real, dest, root)
        _addata_url_forget_failure()
        return root


# ── 配布物としての版を揃える ─────────────────────────────────────────
#
# **本番で実際に起きた不具合への備え（2026-09-11）。**
# Streamlit はメインスクリプト（app.py）を実行のたびに読み直すが、
# `import` したモジュールは `sys.modules` に残ったままで、
# **プロセスを再起動しない限り古い版がメモリに居座る**。
# そのため「app.py は新しいのに pdf_to_neo_pipeline は古い」という
# 食い違いが起き、`process_pdf_to_neo() got an unexpected keyword
# argument 'source_mime'` で PDF→NEO 変換が丸ごと失敗していた。
#
# 引数が増えた場合は例外で止まるので気づけるが、**中身だけが変わった場合は
# 黙って古い規則で .neo が出る**（たとえば neo_rules の作業区分の対応表）。
# 協定見積として保険会社に出すファイルなので、こちらのほうが危ない。
# そこで、ファイルの中身そのもの（sha256）でメモリとディスクを突き合わせ、
# ずれていれば読み直す。**揃えられないときは、古いまま走らせずに断る。**
#
# 並びは依存の浅い順。先に読み直したものを、あとのものが取り込む。
#
# `app` が入っているのは、pipeline が `from app import generate_neo_file` と
# **生成の本体を app から取っている**ため。`streamlit run app.py` では
# 画面側は `__main__` として動くので、`sys.modules['app']` はそれとは
# 別の二重読み込みであり、独立に古くなる。
# 読み直しても画面の描画は走らない（描画は `if __name__ == '__main__':` の中）。
#
# `_pdfium_lock_mod` は**入れてはいけない**。pdfium のロックを
# プロセス全体で1つにするためだけのモジュールで、読み直すと別のロックが
# できて共有の意味が消える（Cヒープが壊れてプロセスごと落ちる）。
# neo_skill.* を先に（app が neo_skill を import する。依存の順: vendor → prompts → llm → doc_hints → _runner → reader → maker → bridge）。
# 入れていなかったため、本番（Streamlit Cloud は push 後もプロセスが残る）で app.py だけ新しくなり import 済みの
# neo_skill/reader.py が古いまま「read_estimate() got an unexpected keyword argument 'customer_hint'」になった（2026-09-15）
_APP_MODULES = ('neo_skill.vendor', 'neo_skill.prompts', 'neo_skill.llm', 'neo_skill.doc_hints', 'neo_skill._runner',
                'neo_skill.reader', 'neo_skill.maker', 'neo_skill.bridge',
                'neo_rules', 'neo_header', 'addata_locator', 'addata_settings',
                '_addata_db_search', '_grade_identifier',
                'addata_vehicle_resolver', 'app', 'auto_matching',
                'pdf_to_neo_pipeline')

# 読み直してはいけないモジュール。
# `_pdfium_lock_mod` は上の理由（共有ロック）。
# `_process_state` は「いま変換中か」を数える場所で、読み直すと数が 0 に戻り、
# 走っている変換の最中に差し替えが起きる穴が開く。
_APP_MODULES_NEVER_RELOAD = ('_pdfium_lock_mod', '_process_state')

# 読み直しと変換の取り合いを避けるための錠と数。
#
# **`app.py` のモジュール変数に置いてはいけない。** Streamlit は `app.py` を
# rerun のたびに `__main__` として実行し直すので、ここに置くと
# セッションごとに別物になり、**別のセッションからは「誰も変換していない」
# ように見えて**、走っている変換の足元でモジュールが差し替わる。
# プロセスで1つの置き場（`_process_state`）に持たせる。
import _process_state as _pstate     # noqa: E402
_module_lock = _pstate.module_lock


def _file_digest(path):
    """ファイルの中身の指紋。更新時刻ではなく中身で見る
    （配布のやり方によっては時刻が当てにならない）。"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def _module_src_state(mod):
    """(いま読み込まれている版の指紋, ディスク上の版の指紋)。分からなければ None。"""
    path = getattr(mod, '__file__', '') or ''
    if not path or not os.path.isfile(path):
        return (None, None)
    try:
        return (getattr(mod, '__app_src_digest__', None), _file_digest(path))
    except OSError:
        return (None, None)


def _stamp_module(mod):
    """いま読み込んでいる版の指紋を、そのモジュールに控える。"""
    try:
        mod.__app_src_digest__ = _file_digest(mod.__file__)
    except Exception:       # noqa: BLE001
        pass


def _stale_modules():
    """メモリとディスクで中身が食い違っているモジュールの名前。"""
    out = []
    for name in _APP_MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            continue        # まだ読まれていない → 次の import で新しいものが載る
        if getattr(getattr(mod, '__spec__', None), '_initializing', False):
            continue        # 初めて import している途中（指紋はファイルの最後で控えるので、まだ無い。別の利用者の変換を偽の版ずれで断っていた。レビュー 2 周目）
        if name in _reload_failures():
            out.append(name)    # 読み直しが途中で失敗したまま（ファイルを元の版に戻して指紋が合っても、中身は新旧が混ざっている。レビュー 2 周目）
            continue
        loaded, on_disk = _module_src_state(mod)
        if on_disk is None:
            continue        # 調べられないものには口を出さない
        if loaded != on_disk:
            out.append(name)
    return out


def sync_app_modules():
    """メモリ上のコードをディスクに揃える。揃えられなかったものの名前を返す。

    読み直しが起きるのはデプロイ直後の1回だけ（指紋が一致したら素通り）。
    変換中は見送る — 走っている変換の足元でモジュールの中身を
    差し替えると、途中で規則が変わってしまう。
    """
    import importlib
    with _module_lock:
        stale = _stale_modules()
        if not stale:
            return []
        if _pstate.busy():
            return stale        # いま誰かが変換中。差し替えず、そのまま知らせる
        for name in _APP_MODULES:
            mod = sys.modules.get(name)
            if mod is None:
                continue
            if getattr(getattr(mod, '__spec__', None), '_initializing', False):
                continue
            loaded, on_disk = _module_src_state(mod)
            if on_disk is None or (loaded == on_disk and name not in _reload_failures()):
                continue
            try:
                # 前の版のバイトコード（.pyc）を消してから読み直す。Python は .pyc を「更新時刻（秒）と大きさ」で確かめるので、
                # 同じ秒に同じ大きさで書き換わった版だと古いコードのまま動き、指紋（ファイルから読む）だけ新しくなる（レビュー 2 周目）
                import importlib.util as _ilu
                _pyc = _ilu.cache_from_source(mod.__file__)
                if os.path.exists(_pyc):
                    os.remove(_pyc)
            except Exception:       # noqa: BLE001  消せなくても読み直しは続ける
                pass
            # 読み直しの前に、ディスクの指紋を読み、前の版の指紋と「最初の指紋」を消す（前の版の値が残ると、読み直しの途中で
            # push されたときに古い版の指紋のまま「揃った」と見ていた。レビュー 4 周目）
            _pre = None
            try:
                _pre = _file_digest(mod.__file__)
            except Exception:       # noqa: BLE001
                pass
            vars(mod).pop('__app_src_digest__', None)
            vars(mod).pop('_stamp_digest_at_start', None)
            try:
                importlib.reload(mod)
            except Exception:       # noqa: BLE001  下の再判定で拾う（指紋はファイルの最後で控えるので、途中で落ちれば古いまま）
                _reload_failures().add(name)
                continue
            _reload_failures().discard(name)
            # 指紋はモジュール自身が読み込みの最初と最後で同じ中身のときだけ控える。ここでディスクの指紋で上書きすると、読み直しの
            # 途中で push された版を「揃った」と見てしまう（レビュー 3 周目）。自分で控えないモジュールだけ、ここで控える
            if '_stamp_digest_at_start' not in vars(mod):
                _stamp_module(mod)
            elif vars(mod).get('_stamp_digest_at_start') != _pre:
                # 読み直しの前に読んだ版と、モジュールが読み込みの最初に読んだ版が違う（コンパイル中に push された）。
                # Python が読んだのがどちらか分からないので、古い扱いにして次の sync で読み直す
                vars(mod).pop('__app_src_digest__', None)
        # 読み直したら、古いコードで作った .neo の控えを捨てる（N5）
        try:
            _pp = sys.modules.get('pdf_to_neo_pipeline')
            if _pp is not None and hasattr(_pp, 'clear_pipeline_cache'):
                _pp.clear_pipeline_cache()
        except Exception:       # noqa: BLE001
            pass
        return _stale_modules()


@contextlib.contextmanager
def _conversion_guard():
    """変換中であることを示す（この間はモジュールを差し替えない）。

    数はプロセスで1つの置き場に持たせる（`_process_state`）。
    `app.py` の変数だと、rerun ごとに作り直されて別のセッションから見えない。
    """
    _pstate.enter()
    try:
        yield
    finally:
        _pstate.leave()


# app.py が `process_pdf_to_neo` に渡すつもりの引数。
# ここに無い引数を増やしたら、このリストにも足すこと
# （足さないと、古い版の検出から漏れる）。
_PIPE_ARGS_EXPECTED = (
    'source_mime', 'addata_root', 'template_path', 'mode_override',
    'model_name', 'api_key', 'cache_scope', 'is_tax_inclusive',
    'merge_mode', 'expenses', 'insurance_info', 'vehicle_info',
)


def _pipeline_accepts(fn, names):
    """fn が names の引数を受けられるか。受けられないものを返す。"""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return ()       # 調べられないときは口を出さない
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return ()       # **kwargs があるなら何でも受けられる
    return tuple(n for n in names if n not in params)


def _reload_failures() -> set:
    """読み直しが例外で失敗したモジュールの名前（プロセスで 1 つ。待っても揃わないので案内を変える）"""
    s = getattr(_pstate, 'reload_failures', None)
    if s is None:
        s = set()
        _pstate.reload_failures = s
    return s


def _version_skew_message(names):
    if any(n in _reload_failures() for n in names):
        return ('アプリの内部で新しいコードの読み込みに失敗しました（%s）。'
                'お手数ですが、アプリを再起動してからもう一度お試しください。' % '・'.join(names))
    return ('アプリの内部で新旧のコードが混ざっています（%s）。'
            '更新の直後で、ほかの人の変換が終わると自動で新しいコードに揃います。少し待ってからもう一度お試しください'
            '（続くときはアプリを再起動してください）。'
            % '・'.join(names))


@contextlib.contextmanager
def _synced_conversion():
    """版を揃えてから「変換中」の印を立てる（揃えることと印を立てることを錠の中で一度に行う。間に別のセッションが
    モジュールを差し替えないように）。スキル経路も、ベタ打ち（_call_pipeline）と同じくこの中で動かす（バグハント 3 回目 N1）"""
    with _module_lock:
        stale = sync_app_modules()
        if not stale:
            _pstate.enter()
    if stale:
        raise RuntimeError(_version_skew_message(stale))
    try:
        yield
    finally:
        _pstate.leave()


def _guarded_call(fn, *args, **kwargs):
    """_synced_conversion の中で fn を呼ぶ。版が揃わなければ失敗の dict を返す（p2n_read / p2n_make / run_pdf_to_neo_skill 用）"""
    try:
        cm = _synced_conversion()
        cm.__enter__()
    except RuntimeError as e:
        # 断ったら取り置きの作業フォルダ（顧客情報入りの reading.json）を残さない（p2n_make の state。レビュー）
        if args and isinstance(args[0], dict) and args[0].get('case_dir'):
            try:
                from neo_skill import maker as _nsk_maker_g
                _nsk_maker_g.remove_case_dir(args[0].get('case_dir'))
            except Exception:  # noqa: BLE001
                pass
        return {'ok': False, 'stage': 'error', 'error': str(e)}
    try:
        return fn(*args, **kwargs)
    finally:
        cm.__exit__(None, None, None)


def _defer_sidebar_rerun():
    """サイドバーの処理から st.rerun() を直に呼ばない: まだ描いていない本文の uploader（見積書・車検証・書類）の値が捨てられる
    （Streamlit の仕様。バグハント H3: Addata を選んだだけで見積書と書類が全部消えていた）。旗を立てて main の最後で rerun する"""
    st.session_state['_sidebar_rerun'] = True


def _consume_sidebar_rerun():
    if st.session_state.pop('_sidebar_rerun', False):
        st.rerun()


def _load_pipeline():
    """版を揃えたうえで `pdf_to_neo_pipeline` を返す。揃わなければ断る。

    **古いコードで .neo を作らない**のがここの役目。
    引数が増えた場合は例外で気づけるが、中身だけ変わった場合は
    黙って古い規則で出てしまうので、実行の前に必ず揃える。
    """
    stale = sync_app_modules()
    if stale:
        raise RuntimeError(_version_skew_message(stale))
    import pdf_to_neo_pipeline as _pipe
    return _pipe


def _call_pipeline(_pipe, pdf_path, **kwargs):
    """`process_pdf_to_neo` を呼ぶ。受けられない引数が1つでもあれば断る。

    ここは最後の砦。**引数を黙って落としてはいけない。**
    `is_tax_inclusive` が落ちれば税込の見積が消費税ぶん膨らみ、
    `expenses` が落ちればレッカー代が1円も入らない。`source_mime` が
    落ちれば写真を PDF として読もうとして中身が拾えない。
    協定見積として出すファイルなので、古い版で作るくらいなら断る。
    """
    missing = _pipeline_accepts(_pipe.process_pdf_to_neo, tuple(kwargs))
    if missing:
        raise RuntimeError(_version_skew_message(missing))
    with _conversion_guard():
        return _pipe.process_pdf_to_neo(pdf_path, **kwargs)


def find_addata_dir():
    """Addata ルートを返す。見つからなければ None。

    探す順番（上が優先）:
      1. この画面でアップロードされた ZIP
      2. 設定した「フォルダのパス」（アプリが動いているマシンから見える場所）
      3. 設定した「取得URL」（クラウドでも効く。一度設定すればどのPCでも）
         ※ これが設定されていて取りに行けなかった場合は **None を返す**。
            黙って別の Addata に落ちると、同じ設定なのに違うデータベースで
            照合した見積が出てしまう。
      4. 環境変数 / secrets
      5. ローカルの標準的な設置場所（自動検出）
    """
    # 0. ブラウザ経由で PC から取り込んだ Addata（neo_skill.bridge。クラウドで PC の C:\Addata を使う経路）
    try:
        from neo_skill import bridge as _br
        _bp = st.session_state.get('_bridge_path')
        if _bp and _br.has_com(_bp):
            _br.touch(_bp)
            return _bp
    except Exception:
        pass
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
    # 2. 設定したフォルダのパス
    try:
        _dir = addata_setting(_QS_ADDATA_DIR)
        if _dir and _addata_is_valid(_dir):
            return _dir
    except Exception:
        pass
    # 3. 設定した取得URL
    #
    # **ここで失敗したら、下は見ずに諦める。** 取得URLは「どのPCでも同じ
    # データベースを使う」ための設定なので、取りに行けなかったときに黙って
    # そのマシンにあった別の Addata を使うと、**同じブックマークから開いた
    # のに違うデータベースで照合した見積**が出る。版が違えば標準品番も
    # 標準指数も変わるのに、画面はいつもどおりで気づけない。
    # 諦めればベタ打ち（モードA）になり、理由は設定画面に出る。
    try:
        _url = addata_setting(_QS_ADDATA_URL)
    except Exception:
        _url = ''
    if _url:
        try:
            _root = _addata_from_url(_url)
        except Exception as _e:     # noqa: BLE001
            _root = ''
            try:
                _addata_url_remember_failure(
                    _url, '取得できませんでした: %s' % str(_e)[:120])
            except Exception:
                pass
        if _root and _addata_is_valid(_root):
            return _root
        return None
    # 4. 環境変数 / secrets（Docker・Cloud Run で外部ボリュームを渡す場合）
    for _env in (os.environ.get('ADDATA_ROOT'), _secret_addata_root()):
        if _env and _addata_is_valid(_env):
            return _env
    # 5. ローカルの標準的な設置場所
    #    （NEO_ADDATA_NO_AUTODETECT=1 で飛ばす: クラウドと同じ「Addata 無し」をこの PC で再現する試験用。本番では設定しない）
    if str(os.environ.get('NEO_ADDATA_NO_AUTODETECT') or '').lower() in ('1', 'true', 'yes'):
        return None
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
    # KA81（型式指定番号＋類別区分番号）での逆引きは
    # auto_matching.identify_vehicle_wrapper の中でまとめて行う
    # （PDF直接経路も同じ関数を通るため、そちらに置いてある）。
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


def _clear_addata_codes(estimate_data):
    """前回の Addata 照合で入った部品コードを捨てる。

    生成直前の再照合が「車種が特定できない」等で見送られたとき、
    前の車両で引いた部品コードが残っていると、車両情報を直したのに
    部品コードだけ前の車種のもの、という .neo ができてしまう。
    協定見積は「同じ車・同じ部品」が絶対条件なので、
    間違ったコードを残すくらいなら空にする。
    """
    estimate_data['_addata_matched'] = False
    estimate_data.pop('_addata_reverse', None)
    for it in (estimate_data.get('items') or []):
        if not isinstance(it, dict):
            continue
        it['_master_ref_no'] = ''
        it['_master_section_code'] = ''
        it['_master_branch_code'] = ''
        # 照合レベルと品番も一緒に捨てる。残すと、車両を直したあとに
        # 前の車で引き当てた品番が ERParts.PartsNo に書かれてしまう。
        it['match_level'] = ''
        it['db_parts_no'] = ''
        it['db_price'] = None
        it['db_work_index'] = None


def apply_addata_matching(estimate_data, vehicle_data, progress=None):
    """Addata があれば車種を特定して部品照合を通す。照合したかどうかを返す。

    step2（車検証＋見積書）と CSV 取り込みの両方から呼ぶ。CSV 取り込みは
    step2 を通らず step3 へ進むため、ここを共通で通さないと CSV 経路だけ
    部品コード（_master_ref_no → NEO の PartsCode）が空のままになる。

    照合が items に書き込むのは部品コードと照合レベル（未マッチ行の「※」判定に
    使う match_level）だけで、金額・品名・工数は見積の値のまま。
    app.py は db_price / db_parts_no / db_work_index を読まないので、
    Addata の値で原本の金額が書き換わることはない。
    """
    def _tick(pct, text):
        if progress is None:
            return
        try:
            progress.progress(pct, text=text)
        except Exception:
            pass

    addata_dir = find_addata_dir()
    if not (addata_dir and vehicle_data):
        estimate_data['_veh_match_result'] = {
            'match_layer': 3, 'is_supported': False,
            'reason': ('Addata未検出（そのまま転記）' if not addata_dir
                       else '車両情報なし（そのまま転記）')}
        _clear_addata_codes(estimate_data)
        _tick(92, "✏️ Addata照合なし — 見積の内容をそのまま転記...")
        return False

    _tick(92, "Addata マスタとの照合を実行中...")
    veh_match_result = identify_vehicle(addata_dir, vehicle_data)
    estimate_data['_veh_match_result'] = veh_match_result

    # is_template（TOYOTA_GENERIC 代用）は実車種が当たっていないので照合しない。
    # PDF→NEO 側の decide_mode_from_identify がモードAへ落とすのと揃える。
    if not veh_match_result.get('is_supported') or veh_match_result.get('is_template'):
        _clear_addata_codes(estimate_data)
        return False
    # 候補が複数残ったまま先頭を採っている状態では照合しない。
    # 別型式の部品マスタから引いた部品コードが協定見積に入るくらいなら、
    # 空のまま人に埋めてもらうほうが安全（元見積と同じ車・同じ部品が絶対条件）。
    if veh_match_result.get('ambiguous'):
        _clear_addata_codes(estimate_data)
        return False
    if not estimate_data.get('items'):
        _clear_addata_codes(estimate_data)
        return False

    # 特定できた車種を照合にも渡す。渡さないと照合側が車種を引き直し、
    # KA81 で特定した車種と食い違って部品名が当たらなくなる。
    _veh_for_match = dict(vehicle_data or {})
    _vc = str(veh_match_result.get('vehicle_code') or '').strip()
    if _vc:
        _veh_for_match['vehicle_code'] = _vc
    matched_items, has_rev = match_parts_with_addata(
        estimate_data['items'], addata_dir, _veh_for_match)
    estimate_data['items'] = matched_items
    # has_rev は「照合器が結果を返したか」であって、PDF総額の突き合わせ
    # （_reverse_match）とは意味が違う。_reverse_match を上書きすると、
    # 金額が原本と合っていなくても step3/4 の不一致警告と確認欄が消える。
    estimate_data['_addata_reverse'] = has_rev
    # has_rev は「照合器が非空のリストを返したか」でしかなく、全行 L4（該当なし）
    # でも True になる。実際に部品コードが入った行があるかで成功を判定しないと、
    # PartsCode が空のままなのに画面に「Addata照合済み」と出て気づけない。
    def _code_writable(it):
        # 生成側（_update_ansmb_impl）は、4桁の数字でない参照番号を捨てる。
        # ここで同じ条件を使わないと、実際には PartsCode が空になる行を
        # 「Addata照合済み」と数えてしまい、画面と生成物が食い違う。
        _ref = str(it.get('_master_ref_no') or '').strip()
        return len(_ref) == 4 and _ref.isdigit()

    _hit = any(_code_writable(it) for it in matched_items)
    if not has_rev:
        # 照合器が結果を返せなかった。前回の照合結果を残さない。
        _clear_addata_codes(estimate_data)
        return False
    if not _hit:
        # 照合自体は動いたが、.neo に書ける部品コード（4桁の参照番号）を
        # 持つ行が1つも無い。部品コードは落とすが、品番（db_parts_no）は
        # 価格の裏が取れた照合結果なので捨てない。
        # 画面上は「照合済み」とは言わない。
        for _it in (estimate_data.get('items') or []):
            if isinstance(_it, dict):
                _it['_master_ref_no'] = ''
                _it['_master_section_code'] = ''
                _it['_master_branch_code'] = ''
        estimate_data['_addata_matched'] = False
        return False
    estimate_data['_addata_matched'] = True
    return True


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

def generate_beta_discrepancy_report_pdf(estimate_data, calc_parts, calc_wages, pdf_parts, pdf_wages, vehicle_info, verdict=None):
    """
    ReportLabを使用してベタ打ちモード用の金額ズレ検証レポート(PDF)を生成する。
    verdict はステップ③の照合の結果（parts_ok / wage_ok / grand_ok と総額）。③で一致とした小計（値引き前の小計など）は赤にせず、
    総額の差は総額の行で示す（小計を完全一致で比べ直して別の差を赤で示し、総額の差を載せていなかった。レビュー 4 周目）
    """
    verdict = verdict or {}
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

    def _judge(pdf_v, diff_v, ok, gross, sp_=False):
        # 判定は差額と別の列に書く（差額の欄に続けて書くと隣の欄にはみ出していた。レビュー 5 周目）
        if not pdf_v:
            return '未照合'          # 見積書から読めなかった小計は「一致」と書かない
        if ok is False:
            return '相違'            # ③で相違としたものを「一致」と書かない（判定が無いときは書かない。レビュー 6・7 周目）
        if diff_v == 0:
            return '一致'
        if gross:
            return '一致（値引き前）'
        if sp_:
            return '一致(ｼｮｰﾄﾊﾟｰﾂ)'    # 半角で入れる（全角だと判定の欄からはみ出す。レビュー 7 周目に実測）
        if verdict.get('rev'):
            return '一致（逆算）'
        return '一致（③で確認）' if ok else '未照合'
    _pj = _judge(pdf_parts, parts_diff, verdict.get('parts_ok'), verdict.get('parts_gross'), verdict.get('parts_sp'))
    _wj = _judge(pdf_wages, wage_diff, verdict.get('wage_ok'), verdict.get('wage_gross'), verdict.get('wage_sp'))

    def _cell_or_dash(pdf_v, text):
        # 照合していない（印字が読めなかった）欄には印字も差額も出さない（レビュー 6・7 周目）
        return text if pdf_v else '—'
    sum_data = [
        ['項目', '見積書の印字', '明細の合算', '差額', '判定'],
        ['部品合計', _cell_or_dash(pdf_parts, f"¥{pdf_parts:,}"), f"¥{calc_parts:,}",
         _cell_or_dash(pdf_parts, f"{'+' if parts_diff > 0 else ''}{parts_diff:,}円"), _pj],
        ['工賃合計', _cell_or_dash(pdf_wages, f"¥{pdf_wages:,}"), f"¥{calc_wages:,}",
         _cell_or_dash(pdf_wages, f"{'+' if wage_diff > 0 else ''}{wage_diff:,}円"), _wj],
    ]
    _g_bad = verdict.get('grand_ok') is False
    if _g_bad:
        _gd = safe_int(verdict.get('grand_diff'))
        sum_data.append(['総額（明細・税込）', f"¥{safe_int(verdict.get('grand_want')):,}", f"¥{safe_int(verdict.get('grand_neo')):,}",
                         f"{'+' if _gd > 0 else ''}{_gd:,}円", '相違'])
    _red = [('TEXTCOLOR', (3, r_), (4, r_), colors.red) for r_, j_ in ((1, _pj), (2, _wj), (3, '相違' if _g_bad else '')) if j_ == '相違']
    # 差額の欄は 9 桁（打ち間違いで 0 を 1 つ多く入れた額）でも隣と重ならない幅にする（レビュー 6 周目）
    t_sum = Table(sum_data, colWidths=[36*2.83, 30*2.83, 30*2.83, 32*2.83, 32*2.83])
    t_sum.setStyle(TableStyle([
        ('FONT', (0,0), (-1,-1), font_name, 10),
        ('ALIGN', (0,0), (-1,0), 'CENTER'),
        ('ALIGN', (1,1), (3,-1), 'RIGHT'),
        ('ALIGN', (4,1), (4,-1), 'CENTER'),
        ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
        ('GRID', (0,0), (-1,-1), 0.5, colors.black),
    ] + _red))
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
4. 【数値の正規化】金額は、カンマ(,)を除去した半角整数の数値型(Number)で出力すること。数量は印字どおりの数値で出力すること（2.5 L のような小数は 2.5 のまま。整数に丸めない）。読み取れない数値は 0 とし、読み取れない文字は "不明" とする。
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
- 区分の判定ルール（**番号の小さいルールが優先**。複数当てはまるときは必ず上を採る）:
  1. 【最優先】「研磨」「磨き」「写真代」「ショートパーツ」を含む行 → ""（空白）
     ※ 部品金額だけの行でも空白のまま。原本に区分が書かれていないものを勝手に決めない。
     ※ ただし「磨き調整」は区分なので 5 で拾う（ここでは空白にしない）。
  2. 【重要】部品名称および部品金額の計上があるが、技術料（工賃）の計上がない行 → "取替"
  ── ここから先は「2語以上の複合区分」を先に見る。単独の語より必ず優先すること ──
  【重要】3〜5 と 9 は、**見積書に書かれていた語をそのまま**出すこと。
          言い換えたり代表的な語にまとめたりしない。この文字列は帳票の
          「修理方法」欄にそのまま印字されるので、原本と違う紙になる。
  3. 「脱着修理」「脱着鈑金」「脱着板金」を含む → 当たった語をそのまま
  4. 「点検調整」「点検清掃」を含む → 当たった語をそのまま
  5. 「分解調整」「分解清掃」を含む → 当たった語をそのまま／単に「分解」なら "分解調整"
  6. 「磨き調整」を含む → "磨き調整"
  ── ここから単独の語 ──
  7. 「取替」「交換」「取換」「取り替え」を含む → "取替"
  8. 「脱着」「取外」「取付」「組付」を含む → "脱着"
  9. 「鈑金」「板金」を含む → 当たった語をそのまま
  10. 「塗装」「ペイント」「ワックス」「加算」「ブース」を含む → "塗装"
  11. 「点検」「診断」を含む → "点検"
  12. 「調整」「光軸」「フィッティング」「コーディング」「設定」「消去」を含む → "調整"
  13. 「修理」「補修」「修正」「穴あけ」「シーリング」を含む → "修理"
  ※ 例: 「ホイール研磨」は部品金額だけでも 1 で空白（2 の "取替" にしない）。
        「ショートパーツ」も同じく空白。
        「ドア脱着板金」は 3 で "脱着板金"（"脱着修理" に言い換えず、8 の "脱着" にもしない）。
        「エンジン分解清掃」は 5 で "分解清掃"（"分解調整" に言い換えない）。
        「センサー磨き調整」は 6 で "磨き調整"（1 の空白、12 の "調整" にしない）。
</extraction_logic>"""


TASK_PROMPTS = {}

TASK_PROMPTS["insurance_doc_ocr"] = _doc_hints.INSURANCE_DOC_PROMPT   # 速報報告書・事故受付票など（neo_skill/doc_hints.py）

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
- postal_no: 車検証に印字された郵便番号（印字が無ければ ""。住所から推測しない）
- prefecture: 使用者の住所（使用者欄が *** なら所有者の住所） → 都道府県
- municipality: 使用者の住所（同上） → 市区町村
- address_other: 使用者の住所（同上） → 町名・番地以降
- car_reg_department: 自動車登録番号の地名部分（例: "北九州", "品川", "福岡"）
- car_reg_division: 自動車登録番号の分類番号（例: "346"） → 半角数字で出力（コグニの NEO と同じ）
- car_reg_business: 自動車登録番号のひらがな（例: "の"） → 全角ひらがなで出力
- car_reg_serial: 自動車登録番号の一連番号（例: "1224"） → 半角数字で出力（ハイフンや「・」は付けない）
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


def analyze_insurance_document(api_key, file_bytes, mime_type, model_name=None):
    """事故・保険の書類（速報報告書・事故受付票・立会依頼書などの写真/PDF/スクリーンショット）を AI-OCR で読む。
    返り値は neo_skill.doc_hints.INSURANCE_DOC_KEYS の dict（値は文字列）。失敗は {'_error': 理由}（黙って空を返さない）"""
    if not api_key:
        return {'_error': 'Gemini APIキーが設定されていません'}
    try:
        # 送る前に大きさ・形式を確かめる（大きな添付で送り直しを重ねてメモリが膨れていた。P1-1/P11）
        file_bytes, mime_type = _gemini_ready_bytes(file_bytes, mime_type)
    except ValueError as _ge:
        return {'_error': str(_ge)}
    if not model_name:
        try:
            model_name = st.session_state.get('selected_model')
        except Exception:
            model_name = None
    if not model_name:
        model_name = get_default_gemini_model(api_key)
    prompt = _build_prompt("insurance_doc_ocr")
    result = {}
    _last = None
    try:
        from google.genai import types
        client = _get_genai_client(api_key)
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, types.Part.from_bytes(data=file_bytes, mime_type=mime_type)],
            config={"temperature": 0.0, "max_output_tokens": 4096,
                    "response_mime_type": "application/json", "response_schema": _doc_hints.INSURANCE_DOC_SCHEMA},
        )
        if response.text and response.text.strip():
            try:
                result = json.loads(response.text)
            except (json.JSONDecodeError, TypeError):
                result = extract_json_from_response(response.text)
    except Exception as e:  # noqa: BLE001
        _last = e
    def _has_data(r):
        # 許可キーに絞ってから判定（未知のキーだけの返事を「読めた」としない）。confidence だけも「読めた」ではない
        return isinstance(r, dict) and any(v for k, v in r.items() if k in _doc_hints.INSURANCE_DOC_KEYS and k != 'confidence' and v and str(v).strip())
    if not _has_data(result):
        try:
            _txt = call_gemini(api_key, file_bytes, mime_type, prompt, model_name=model_name, use_json_mode=True)
            result = json.loads(_txt) if _txt else {}
        except Exception as e2:  # noqa: BLE001
            _last = e2
            result = {}
    if not _has_data(result):
        return {'_error': str(_last) if _last else '書類から事故・車両の情報を読み取れませんでした'}
    return {k: ('' if v is None else str(v).strip()) for k, v in result.items() if k in _doc_hints.INSURANCE_DOC_KEYS}


def _shaken_has_data(r) -> bool:
    """車検証 OCR の返事に中身があるか。confidence だけ（他の項目が全部空）や '_' で始まるキーだけは「読めた」ではない
    （Codex 66: confidence は文字列で返るので、values() の any() では空の返事が成功に見えていた）"""
    return isinstance(r, dict) and any(v for k, v in r.items()
                                       if k != 'confidence' and not str(k).startswith('_') and v and str(v).strip())


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
        # 送る前に大きさ・形式を確かめる（P1-1/P11）
        file_bytes, mime_type = _gemini_ready_bytes(file_bytes, mime_type)
    except ValueError as _ge:
        return {'_error': str(_ge)}
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
        print(f"[shaken_ocr] response_schema failed: {type(e).__name__}")   # 生のエラー文は出さない（キーや値が混ざる。Codex hunt C2）
        _method_used = "fallback"
        _last_error = e

    # 方式1で空結果 → 方式2: シンプルなJSON modeにフォールバック
    if not _shaken_has_data(result):
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
            print(f"[shaken_ocr] json_mode fallback also failed: {type(e2).__name__}")
            result = {}
            _last_error = e2

    # 2方式とも空 → 失敗として理由を返す。空dictを返すと呼び出し側が
    # 「読み取れたが全項目が空」と区別できず、車両情報なしのNEOが
    # 黙って作られてしまう。
    if not _shaken_has_data(result):
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
        elif _is_quota_error(_msg):
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

    # 共有サーバのログに車検証の値を残さない（Codex hunt C2）: 方式と埋まった項目数だけ
    print(f"[shaken_ocr] method={_method_used}, filled_fields="
          f"{sum(1 for _k, _v in result.items() if _k != 'confidence' and _v and str(_v).strip())}")

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
    'name':         ('品名', '部品名', '品目', '名称', '摘要', '作業内容', '項目', '作業項目', '作業名', '内容'),
    'work_code':    ('区分', '作業区分'),
    'quantity':     ('数量', '個数', '数', '個', '員数', '点数', '使用数'),
    # 「部品単価」「単価」は行の金額ではない（数量 10・単価 150 を 150 円にしていた。O5/M6）。unit_price に分け、
    # 金額の列が無い・空の行だけ 単価×数量 にする。「金額」「合計」などは別名にしない（部品だけの金額のことも、行の合計の
    # こともある。_resolve_columns_by_data が明細の値で決め、決まらなければ止める）
    'parts_amount': ('部品金額', '部品代', '部品価格', '部品油脂', '部品費', '部品代金', '部品料金', '部品額'),
    'unit_price':   ('部品単価', '単価'),
    'wage':         ('工賃', '技術料', '作業工賃', '工賃金額', '技術料金額', '作業料金', '工賃額', '技術料額', '作業金額',
                     '技術金額', '作業料', '作業代', '作業費', '技術費', '技術料金', '工賃代'),
    'part_no':      ('部品コード', '部品番号', '品番', '部品NO', '品番NO', 'パーツNO', 'パーツ番号', '部品NO.', '品番NO.', 'パーツNO.'),
    'index_value':  ('工数', '指数'),
}
# 見出しの名前だけでは「部品だけの金額」か「行の合計（部品＋工賃）」か決まらない列（明細の値で決める。決まらなければ止める）。
# 「値引金額」「塗装金額」「外注金額」など、ここに無い「〜金額」は何の金額か分からない列として止める（「〜金額」をまとめて
# 部品か行の合計とみなし、作業金額を消したり値引きの額を部品にしたりしていた。レビュー 4 周目）。「値引後金額」「掛率後金額」は
# 部品だけにも行の合計にも値引きがかかり、明細の値では決められないので入れない（止める。レビュー 5 周目）
_COLUMN_AMBIGUOUS_AMOUNT = ('金額', '合計', '計', '小計', '合計金額', '金額計', '総額', '税込', '税込金額', '税込合計', '税抜金額',
                            '行合計', '明細金額', '税抜合計', '税抜合計金額', '合計額', '金額合計', '小計金額', '請求金額',
                            '見積金額')
# 品名の列らしい見出しの一部（「品名・作業内容」「品名/作業」など別名表に無い形）
_COLUMN_NAME_HINTS = ('品名', '作業内容', '項目', '内容', '名称', '品目', '摘要', '部品名')
_COLUMN_ROWNO_RE = re.compile(r'(?i)(明細|行)?(no\.?|#)')


def _norm_col_header(c) -> str:
    """見出しのセルを比べる形に: 全角英数・括弧を半角に（英字は大文字）、空白・区切りを詰め、丸括弧の注記（（税抜）・(円)）を落とし、
    全体が【】・[] で囲まれていれば中身、【税込】などの角括弧の注記は落とす（「【部品金額】(円)」を読めなかった。レビュー 4 周目）"""
    c = unicodedata.normalize('NFKC', str(c or '')).upper()
    c = re.sub(r'[\s\u3000・、，,/／]', '', c)
    # 全体が括弧で囲まれた見出し（「（品名）」「【品名】」）は中身（丸括弧の注記として消すと空になり、見出しを見失って位置で
    # 金額を読んでいた。レビュー 4 周目）
    m = re.fullmatch(r'\(([^()]+)\)|【([^【】]+)】|\[([^\[\]]+)\]', c)
    if m:
        return m.group(1) or m.group(2) or m.group(3)
    c1 = re.sub(r'\([^()]*\)', '', c)
    c1 = re.sub(r'\(.*$', '', c1)
    m = re.fullmatch(r'【([^【】]+)】|\[([^\[\]]+)\]', c1)
    if m:
        return m.group(1) or m.group(2)
    c2 = re.sub(r'【[^【】]*】|\[[^\[\]]*\]', '', c1)
    return c2 or c1 or c


def _build_column_map(header_row) -> dict:
    """見出し行から「内部キー → 列位置」を作る。判別できない場合は空dict。"""
    if not header_row:
        return {}
    cells = [_norm_col_header(c) for c in header_row]
    colmap = {}
    # 「金額（部品）」「金額（工賃）」は括弧を落とす前に見分ける（落とすと両方「金額」になる）
    for i, c in enumerate(header_row):
        rc = re.sub(r'[\s\u3000・、，]', '', unicodedata.normalize('NFKC', str(c or '')))
        if 'parts_amount' not in colmap and re.fullmatch(r'金額\((部品|部品代|部品油脂)\)', rc):
            colmap['parts_amount'] = i
        elif 'wage' not in colmap and re.fullmatch(r'金額\((工賃|技術料)\)', rc):
            colmap['wage'] = i
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
    # 品名の見出しが別名に無い（「品名・作業内容」「品名/作業」など）: 品名らしい語を含む列。それも無ければ、金額の列が
    # 分かっているときに限り、残った左端の列を品名にする（見出しを無視して位置で金額まで読んでいた。レビュー 3 周目）
    if 'name' not in colmap:
        for i, c in enumerate(cells):
            if i not in colmap.values() and any(h in c for h in _COLUMN_NAME_HINTS):
                colmap['name'] = i
                break
    if 'name' not in colmap and any(k in colmap for k in ('parts_amount', 'wage', 'unit_price')):
        for i, c in enumerate(cells):
            if (i not in colmap.values() and c and not _COLUMN_ROWNO_RE.fullmatch(c)
                    and c not in _COLUMN_AMBIGUOUS_AMOUNT and not c.endswith('金額')):
                colmap['name'] = i
                break
    # 品名の列が見つからないなら、この見出しは当てにならないので位置決め打ちに戻す
    if 'name' not in colmap:
        return {}
    # 見出しだけでは中身が決まらない列は、明細の値を見て _resolve_columns_by_data が決める（決まらなければ止める）。
    #  「金額」「合計」「税込金額」など: 部品だけの金額か行の合計（部品＋工賃）か／「部品」: 部品の金額か品番か／「番号」: 品番か行の
    #  番号か。決まるまでは位置の補いにも使わせない
    for i, c in enumerate(cells):
        if i in colmap.values():
            continue
        if c in _COLUMN_AMBIGUOUS_AMOUNT:
            colmap[f'_kingaku_{i}'] = i
        elif c == '部品':
            colmap[f'_buhin_{i}'] = i
        elif c == '番号':
            colmap[f'_bango_{i}'] = i
    # 金額でない見出し（備考・メモ・単位・行の番号・率・税額など）は位置の補いに使わせない（「備考」を工賃として読んで止まっていた）
    for i, c in enumerate(cells):
        if i in colmap.values():
            continue
        if (c in ('備考', 'メモ', 'コメント', '単位', '項番', '行', '行番号', '連番', 'ページ', '頁', '消費税', '税額', '税')
                or _COLUMN_ROWNO_RE.fullmatch(c) or c.endswith('率') or '%' in c):
            colmap[f'_other_{i}'] = i
    return colmap


def _looks_like_part_no(v) -> bool:
    """品番らしい値か: 英字か「数字-数字」を含み数字もある 5 字以上（52119-12345・90467A1234 など）、または 8 桁以上の数字だけ
    （ハイフン無しの品番。明細 1 行の金額としてはあり得ない大きさ）。全角・空白入りも見る。「-」「OEM」「45000」は品番とみなさない"""
    s = re.sub(r'\s', '', unicodedata.normalize('NFKC', str(v or '')))
    if not re.search(r'\d', s):
        return False
    if re.fullmatch(r'\d{8,}', s):
        return True
    return (len(s) >= 5 and bool(re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z\-‐－]*[0-9A-Za-z]', s))
            and bool(re.search(r'[A-Za-z]|\d[\-‐－]\d', s)))


def _csv_row_parts(pa_raw, up_raw, qty_raw, wage_amt, unit_generic: bool, has_pa_col: bool, index_raw='',
                   unit_is_parts: bool = False):
    """CSV の 1 行の部品の金額: 部品金額の欄の値。空で単価があれば 単価×数量。取り込みと「金額」の列の確かめで同じ値を使う
    （確かめは部品 0、取り込みは 単価×数量 とみて食い違い、確かめを通った行に部品を足していた。レビュー 5 周目）。
    見出しが「単価」で 単価×数量（または 単価×工数）がちょうど工賃と同じ行は、単価がレバーレート（工賃の時間単価）かもしれない:
    部品金額の列があれば、その欄が空なのは「部品が無い」という意味なので 0。部品金額の列が無ければ部品の単価と区別できないので
    None を返す（呼び出し側は部品 0 円にして、そう読んだことを警告に出す。単価をそのまま部品にして水増ししていた。レビュー 6 周目）。
    行の合計の列で 単価×数量＋工賃 と裏が取れているときは、単価は部品の単価と分かる（unit_is_parts）"""
    pa_raw = str(pa_raw or '').strip()
    up_raw = str(up_raw or '').strip()
    if pa_raw or not up_raw:
        return safe_int(pa_raw)
    try:
        qf = float(_normalize_number_text(qty_raw) or 1)
    except (TypeError, ValueError):
        qf = 1.0
    try:
        xf = float(_normalize_number_text(index_raw) or 0)
    except (TypeError, ValueError):
        xf = 0.0
    if (unit_generic and wage_amt > 0 and not unit_is_parts
            and (abs(safe_int(up_raw) * qf - wage_amt) <= 1 or (xf and abs(safe_int(up_raw) * xf - wage_amt) <= 1))):
        # 単価の欄にレバーレート 8,000・数量 1.5 で工賃 12,000 の行（部品 12,000 を足していた。レビュー 3 周目）。
        # 時間が「工数」の列にある形も同じ（単価×工数 ＝ 工賃。部品 8,000 を作っていた。レビュー 5 周目）。
        # 単価×数量 も 単価×工数 も工賃と合わなければ、単価はレバーレートでは説明が付かないので部品の単価として足す
        return 0 if has_pa_col else None
    from decimal import Decimal as _Dec, InvalidOperation as _DecErr
    try:
        # 数量が小数でも元の数量で掛け、単価は丸めずに掛ける（155.5 × 10 を 1,560 にしていた。レビュー 2 周目）。
        # 負の数量（返品 −1 × 単価）は符号を残す（+5,000 にしていた。レビュー 3 周目）
        _qd = _Dec(_normalize_number_text(qty_raw) or '1') if qf != 0 else _Dec(1)
        return jpy_round(_Dec(_normalize_number_text(up_raw) or '0') * _qd)
    except (_DecErr, ValueError, TypeError):
        return jpy_round(safe_int(up_raw) * (qf if qf > 0 else 1))


def _resolve_columns_by_data(colmap: dict, data_rows: list, header_row) -> list:
    """見出しだけでは決まらない列（_kingaku_ / _buhin_ / _bango_・位置 2 の見出しの無い列）を明細の値で決め、何の金額か
    分からない列を探す。colmap をその場で直し、取り込みを止める誤り（'❌ ' で始まる文言）を返す。
    data_rows は集計行・見出しの繰り返しを除いた明細。推し量れる形は狭くし、合わなければ止める（CSV は取り込んだあと原本と
    照合しないので、黙って誤った金額にするより止めるほうがよい）"""
    errs = []
    hdr = [str(c or '').strip() for c in (header_row or [])]
    name_i = colmap.get('name', 0)

    def vals(i):
        return [str(r[i] or '').strip() for r in data_rows if i < len(r) and str(r[i] or '').strip()]

    def num(r, i):
        if i is None or i >= len(r):
            return None
        raw = str(r[i] or '').strip()
        t = _normalize_number_text(raw) if raw else None
        try:
            return float(t) if t is not None else None
        except ValueError:
            return None

    def _amount_like(v):
        core = re.sub(r'[\s¥￥\\円]', '', v)
        if not core or re.fullmatch(r'[-‐‑‒–—―−ー－ｰ*＊・…]+', core):
            return True
        return _normalize_number_text(v) is not None and not _looks_like_part_no(v)

    def _drop(key, i):
        colmap.pop(key, None)
        colmap[f'_other_{i}'] = i

    def _nm(r):
        return str(r[name_i] if name_i < len(r) else '').strip()[:16] or '（品名なし）'

    def _few(names):
        return '・'.join(names[:3]) + (' など' if len(names) > 3 else '')

    # 「番号」: 品番らしい値があれば品番、無ければ行の番号（金額には使わない）
    for key, i in [(k, v) for k, v in colmap.items() if k.startswith('_bango_')]:
        if 'part_no' not in colmap and any(_looks_like_part_no(v) for v in vals(i)):
            colmap.pop(key)
            colmap['part_no'] = i
        else:
            _drop(key, i)
    # 「部品」: 値がどれも金額なら部品金額、品番らしい値があれば品番
    for key, i in [(k, v) for k, v in colmap.items() if k.startswith('_buhin_')]:
        vs = vals(i)
        if vs and 'parts_amount' not in colmap and all(_amount_like(v) for v in vs):
            colmap.pop(key)
            colmap['parts_amount'] = i
        elif vs and 'part_no' not in colmap and any(_looks_like_part_no(v) for v in vs):
            colmap.pop(key)
            colmap['part_no'] = i
        else:
            _drop(key, i)
    # 位置 2 の列（数量の既定の位置）で見出しが別名に無いもの: 見出しが空か数量らしい語（〜数・QTY・個）で、値がどれも数量らしい
    # ときだけ数量にする。それ以外は取らない（金額なら下の「何の金額か分からない列」で止まり、文字の列はそのまま読み飛ばす）。
    # 数量らしくない値の列を「金額でない列」として外し、見出しが「作業代」「値引」の列の金額を黙って捨てていた（レビュー 5 周目）。
    # 注意書きの行（⚠・❌・※）の値は見ない（注意書きが 1 行混ざるだけで数量の列を外し、部品を 単価×1 にしていた）
    if 'quantity' not in colmap and 2 < len(hdr) and 2 not in colmap.values():
        vs2 = [str(r[2] or '').strip() for r in data_rows
               if 2 < len(r) and str(r[2] or '').strip() and not _nm(r).startswith(('⚠', '❌', '※'))]
        _h2 = _norm_col_header(hdr[2])
        # 見出しが空の列は 2 桁までの数だけ数量とみる（3 桁の金額を数量として取り込み、その列の金額を黙って捨てていた。
        # レビュー 6 周目）。見出しが数量らしい語のときは 3 桁（100 個のクリップ）まで
        _qmax = 1000 if _h2 else 100
        _qlike = [v for v in vs2 if re.fullmatch(r'(一式|式|ｾｯﾄ|セット|(?i:set)|[-‐－ーｰ―*＊])', v)
                  or (_normalize_number_text(v) is not None and abs(float(_normalize_number_text(v))) < _qmax)]
        if vs2 and len(_qlike) == len(vs2) and (not _h2 or re.search(r"数|Q'?TY|PCS|個", _h2)):
            colmap['quantity'] = 2

    up_i, wg_i, q_i = colmap.get('unit_price'), colmap.get('wage'), colmap.get('quantity')

    def _c(r, i):
        return str(r[i] or '').strip() if i is not None and i < len(r) else ''

    def _wb(r):
        w = (num(r, wg_i) or 0.0) if wg_i is not None else 0.0
        u = num(r, up_i) if up_i is not None else None
        q = num(r, q_i) if q_i is not None else None
        base = (u * (q if q else 1.0)) if u else None      # 返品（数量 −1）は符号も
        return w, base

    kg_cols = sorted(v for k, v in colmap.items() if k.startswith('_kingaku_'))
    parts_src = 'col' if 'parts_amount' in colmap else None     # 部品の出どころ: 部品金額の列 / 単価×数量
    if kg_cols and parts_src is None:
        kg = kg_cols[0]
        label = hdr[kg] if kg < len(hdr) else '金額'
        rows_k = []
        for r in data_rows:
            k = num(r, kg)
            if k is not None:
                w, base = _wb(r)
                rows_k.append((k, w, base, _nm(r)))
        # 行ごとの証拠: 部品だけの金額（par）・行の合計（tot）・どちらとも言えない（amb）。部品だけの証拠は
        #  ・工賃のある行の 0 円（行の合計なら工賃より小さくならない）
        #  ・単価×数量 そのもの（単価×数量 が工賃と違う行。工賃は別の列）
        # に絞る。値引き後の行の合計は工賃や 単価×数量 より小さくなり得るので、工賃のある行の 金額 ＜ 工賃・金額 ＜ 単価×数量 や、
        # 単価×数量 ＝ 工賃（単価がレバーレートかもしれない）の行は、どちらとも言えないとして止める（部品の証拠にして工賃を二重に
        # 数えていた。レビュー 5 周目）
        par, tot, amb, par_strong = [], [], [], []
        for k, w, base, nm_ in rows_k:
            if w > 0:
                if abs(k - ((base or 0.0) + w)) <= 1.0:
                    tot.append(nm_)     # 単価×数量＋工賃（単価が無ければ工賃だけ）＝ 行の合計
                elif abs(k) <= 0.5:
                    par.append(nm_)     # 工賃のある行の 0 円（行の合計の列に 1 行だけ混ざることがあるので、下で 2 行以上を求める）
                elif base is not None and abs(base - w) > 1.0 and abs(k - base) <= 1.0:
                    par.append(nm_)
                    par_strong.append(nm_)   # 単価×数量 そのもの（行の合計なら 単価×数量＋工賃 になるはずで、見間違えない）
                elif base is None and k > w + 1.0:
                    pass                # 工賃より大きい金額は、部品だけの金額とも行の合計とも読める（証拠にしない）
                else:
                    amb.append(nm_)
            elif base is not None:
                if k < base - 1.0:
                    par.append(nm_)     # 工賃の無い行の値引き後の部品（行の合計でも同じ額）
                elif k > base + 1.0:
                    amb.append(nm_)     # 単価×数量より大きいのに工賃が無い（工賃込みか税込か読めない）
            # 工賃も単価も無い行は、どちらで読んでも同じ
        # 工賃の列が無い・工賃が 1 つも無い CSV は、工賃の行の金額もこの列にあるはずで、部品と工賃を分けられない（工賃の列が無いと
        # 「金額のある行に工賃が無い」がいつも成り立ち、工賃の行を部品にしていた。レビュー 5 周目）
        _wage_used = wg_i is not None and any((num(r, wg_i) or 0.0) > 0 for r in data_rows)
        role = None
        if not rows_k:
            role = 'parts'              # 列が全部空: 部品金額の列として扱い、単価×数量で埋める
        elif not _wage_used:
            role = None
        elif amb or (par and tot):
            role = None
        elif tot:
            # 単価の列が無いときの「行の合計」の証拠は 金額 ＝ 工賃 の行だけ。1 行では部品がたまたま工賃と同じ額かもしれないので、
            # 2 行以上そろったときだけ決める（1 行の「8000,8000」を部品 0 円にしていた。レビュー 4 周目）
            if (all(abs(k - ((base or 0.0) + w)) <= 1.0 for k, w, base, _n in rows_k)
                    and (up_i is not None or len(tot) >= 2)):
                role = 'total'          # どの行も 単価×数量＋工賃（部品は 単価×数量）
        elif par_strong or len(par) >= 2 or not any(w > 0 for _k, w, _b, _n in rows_k):
            # 部品だけの証拠がある（単価で裏が取れない「工賃のある行の 0 円」だけのときは 2 行以上。行の合計の列に 0 円の行が
            # 1 行混ざるだけで全部を部品と読み、工賃を二重に数えていた。レビュー 6 周目）／金額のある行に工賃が無い
            role = 'parts'
        # それ以外（工賃のある行が、どちらとも読める形だけ）は決められない
        if role == 'parts':
            colmap.pop(f'_kingaku_{kg}', None)
            colmap['parts_amount'] = kg
            parts_src = 'col'
        elif role == 'total':
            parts_src = 'unit'
            # 行の合計（単価×数量＋工賃）で確かめた ＝ その行の単価は部品の単価（レバーレートではない）。金額の欄が空の行は
            # 確かめていないので、行ごとに見る（CSV 全体の旗にしていたため、金額の欄が空の行があると裏の取れている行まで
            # レバーレート扱いになり、関係ない行を名指しして止まっていた。レビュー 7・8 周目）
            colmap['_unit_parts_col'] = kg
        elif not _wage_used:
            errs.append(('❌ 工賃の列が無いので' if wg_i is None else '❌ 工賃の列に金額が 1 つも無いので')
                        + f'、見出しの「{label}」を部品と工賃に分けられません（工賃の行の金額も部品になります）。'
                        '部品の金額は「部品金額」、工賃は「工賃」の列に分けてください。（例: 品名,区分,数量,部品金額,工賃,部品コード）')
        else:
            errs.append(f'❌ 見出しの「{label}」が、部品だけの金額か、部品と工賃を足した行の合計か決められません'
                        '（値引きの行・金額だけの行・単価×数量と合わない行があるときも止めます）。部品だけの金額なら見出しを'
                        '「部品金額」に、行の合計なら部品と工賃を「部品金額」「工賃」の列に分けてください。'
                        '（例: 品名,区分,数量,部品金額,工賃,部品コード）')
    # 残りの「金額」「合計」などの列: 行ごとに 部品＋工賃（税抜・税込）か部品と合うときだけ読み飛ばす。合わない行があれば止める
    # （部品金額の列があると行の合計の列を確かめずに捨て、外注・値引きの行が消えていた。レビュー 4 周目）
    pa_i = colmap.get('parts_amount')
    _upc = colmap.get('_unit_parts_col')    # 行の合計の列（この欄に金額のある行は 単価×数量＋工賃 で裏が取れている）
    _ug = up_i is not None and up_i < len(hdr) and _norm_col_header(hdr[up_i]) == '単価'
    for key in [k for k in list(colmap) if k.startswith('_kingaku_')]:
        kg = colmap[key]
        label = hdr[kg] if kg < len(hdr) else '金額'
        if not errs and parts_src is not None:
            bad = []
            for r in data_rows:
                k = num(r, kg)
                if k is None:
                    continue
                w = safe_int(_c(r, wg_i))
                # 部品は取り込みと同じ決め方で（部品金額の欄が空なら 単価×数量。確かめだけ部品 0 とみて通していた。レビュー 5 周目）
                pv0 = _csv_row_parts(_c(r, pa_i), _c(r, up_i), _c(r, q_i), w, _ug, pa_i is not None,
                                     _c(r, colmap.get('index_value')),
                                     bool(_upc is not None and num(r, _upc) is not None))
                pv = float(pv0 or 0)    # 取り込み側もレバーレートの行は部品 0 円にする
                if not any(abs(k - c) <= 1.0 for c in (pv + w, pv, float(jpy_round((pv + w) * 1.1)))):
                    bad.append(_nm(r))
            if bad:
                errs.append(f'❌ 見出し「{label}」の列が、部品金額＋工賃（または部品金額）と合わない行があります（{_few(bad)}）。'
                            '何の金額か分からないので止めました。その行の金額を「部品金額」「工賃」の列に入れてください'
                            '（金額でなければ見出しを「備考」に）。')
        _drop(key, kg)
    # 見出しはあるのに何の金額か分からない列（「値引金額」「外注金額」「部品価格(円)」など）: 位置で補うと別の列を読むことがあり、
    # 補わないと金額が黙って消える。どちらも協定見積では困るので止める（レビュー 2 周目）
    taken = set(colmap.values())
    unknown = []
    for i, h in enumerate(hdr):
        # 見出しが空の列も、金額があれば止める（位置で品番として読むか、黙って捨てていた。レビュー 5 周目）。
        # 品名より左の見出し無しの列は、行番号（1・2・3…）なら読み飛ばす（レビュー 6 周目）
        if i in taken:
            continue
        amounts = [v for v in vals(i) if _normalize_number_text(v) is not None and not _looks_like_part_no(v)
                   and re.search(r'[1-9]', v)]
        if not h and i <= name_i:
            # 行番号の列は 1（か 2）から 1 ずつ増える形だけ。「100・300」のような小さな金額を行番号とみて黙って捨てていた
            # （レビュー 7 周目）
            _vs = vals(i)
            _nums = [float(_normalize_number_text(v)) for v in _vs if _normalize_number_text(v) is not None]
            if (len(_nums) == len(_vs) and _nums and all(n == int(n) for n in _nums)
                    and _nums[0] in (1.0, 2.0) and _nums == sorted(set(_nums))
                    and _nums[-1] <= len(data_rows) + 3):   # 読み落として番号が飛ぶこともある（レビュー 8 周目）
                continue        # 見出しの無い行番号の列
        if amounts:
            unknown.append(h or f'（見出しなし・{i + 1} 列目）')
    if unknown:
        # 「合計」に直すと、単価の列が無いときは決められないまま（案内が堂々巡りになる）。部品金額の書き方を案内する（レビュー 6 周目）
        errs.append('❌ 見出し「' + '」「'.join(unknown[:3]) + '」の列は金額（数字）に見えますが、何の金額か分かりません。'
                    '部品だけの金額なら見出しを「部品金額」、工賃なら「工賃」にしてください'
                    '（部品と工賃を足した行の合計なら、部品金額＝合計−工賃 を「部品金額」の列に。品番なら「部品コード」、'
                    '行番号なら「No」、金額でなければ「備考」に）。')
    return errs


def _is_total_row_name(name: str) -> bool:
    """品名が集計行のものか判定する。

    見積書の合計欄は「合計」「小計(税抜)」「税込合計」「合計金額」
    「【合計】」「小計①」「合　計　金　額」など表記が揺れる。
    空白・括弧・丸数字・通貨記号を落としたうえで、末尾が集計語かで判断する。
    「合計表示灯」「総額メーター」「温度計」のような部品名は末尾が
    集計語ではないので残る。
    """
    nm = re.sub(r'[\s\u3000【】\[\]「」『』¥￥:：･・*＊_~]', '', str(name or ''))   # Markdown の太字（**合計**）も（レビュー 3 周目）
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
    _errors: list = []      # 取り込みを止める誤り（'❌ ' で始まる注記として返す。M2）
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
    # 表計算ソフトから貼るとタブ区切り、AI の回答をそのまま貼ると Markdown の表になる（M6）。区切りを見分ける
    _lines = [l for l in text.split('\n') if l.strip()]
    _md = [l for l in _lines if l.strip().startswith('|')]
    _delim = ','
    _is_md = False      # Markdown の表（品名の「|」で欄が割れることがある）
    if len(_md) >= 2 and len(_md) >= len(_lines) // 2:
        _is_md = True
        _conv = []
        for l in text.split('\n'):
            ls = l.strip()
            if not ls.startswith('|'):
                _conv.append(l)
                continue
            _ls2 = ls[1:] if ls.startswith('|') else ls
            _ls2 = _ls2[:-1] if _ls2.endswith('|') else _ls2
            # 太字の記号は外し（レビュー 3 周目）、取り消し線は中身ごと消す（消した値を足していた。レビュー 4 周目）
            _cells = [re.sub(r'(\*\*|__)', '', re.sub(r'~~.*?~~', '', c)).strip() for c in _ls2.split('|')]
            if all(re.fullmatch(r':?-{2,}:?', c) for c in _cells if c) and any(_cells):
                continue   # 区切り線 |---|---|
            _conv.append('\t'.join(c.replace('\t', ' ') for c in _cells))
        text = '\n'.join(_conv)
        _delim = '\t'
    elif _lines:
        _head = _lines[:5]
        _cnt = {d: sum(l.count(d) for l in _head) for d in (',', '\t', ';')}
        _best = max(_cnt, key=lambda d: _cnt[d])
        if _best != ',' and _cnt[_best] > _cnt[',']:
            _delim = _best
    try:
        reader = _csv.reader(_io.StringIO(text), delimiter=_delim)
        rows = list(reader)
    except Exception:
        return (items, _trailer_notes) if return_notes else items

    if not rows:
        return (items, _trailer_notes) if return_notes else items

    # ヘッダ行を特定する。「品名」が無くてもヘッダらしい行なら読み飛ばす。
    # 以前は先頭セルに「品名」が無いとヘッダ行をそのまま明細として
    # 取り込み、「品目」のような別表記で先頭行がゴミ明細になっていた。
    # 見出しの判定語は列の別名から作る（別名にだけ足した「部品代」「技術料」「項目」などの見出しを見出しと認識せず、
    # 明細として読んでいた。レビュー 4 周目）
    _HEADER_WORDS = set(sum((list(v) for v in _COLUMN_ALIASES.values()), [])) | set(_COLUMN_AMBIGUOUS_AMOUNT) \
        | {'番号', '備考', '単位', '部品', 'NO', 'NO.'}

    def _norm_header_cell(c):
        # 「部品金額（税抜）」「数 量」「【品名】」のような装飾を外して見出し語と比べる（列の対応付けと同じ形）
        return _norm_col_header(c)

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
    if _colmap:
        _name_i = _colmap.get('name', 0)
        _data_rows = [r for r in rows[header_idx:]
                      if r and any(str(c or '').strip() for c in r) and not _looks_like_header(r)
                      and str(r[0] if r else '').strip() != '品名'
                      and not _is_total_row_name(str(r[_name_i] if _name_i < len(r) else ''))]
        _errors.extend(_resolve_columns_by_data(_colmap, _data_rows, rows[header_idx - 1]))

    _claimed = set(_colmap.values())
    # 単価の見出しが「単価」（部品単価でない）か。工賃のレバーレートを入れる書式がある（レビュー 4 周目）
    _unit_generic = bool(header_idx > 0 and 'unit_price' in _colmap
                         and _norm_col_header(rows[header_idx - 1][_colmap['unit_price']]) == '単価')
    # 見出しの列数（右端の空の見出しは数えない）。これより右に値のある行は列がずれている（M2）
    _hdr_cells = rows[header_idx - 1] if header_idx > 0 else []
    _hdr_len = len(_hdr_cells)
    while _hdr_len and not str(_hdr_cells[_hdr_len - 1] or '').strip():
        _hdr_len -= 1
    if not _hdr_len:
        _hdr_len = 6   # 見出しが無いときは既定の 6 列（品名,区分,数量,部品金額,工賃,部品コード）
    def _amount_ok(raw):
        """金額の欄として読める（空・ダッシュ・数字）か。「4万5千」「45,000円也」などは読めない（P19）。
        表計算ソフトの会計表示のゼロ（「¥ -」「¥-」）・「***」・ダッシュの類は 0 円として受ける（レビュー）"""
        raw = str(raw or '').strip()
        core = re.sub(r'[\s¥￥\\円]', '', raw)
        if not core or re.fullmatch(r'[-‐‑‒–—―−ー－ｰ*＊・…]+', core):
            return True
        return _normalize_number_text(raw) is not None

    def _cell(row, key, pos):
        # 見出しに無い項目は位置で補う。「部品、油脂」「金額（部品）」の
        # ように別名表に無い列名だと、補わなければ部品代が全額消える。
        # ただし他のキーが既に確保した列（品名・区分など）は横取りしない。
        idx = _colmap.get(key)
        if idx is None:
            # 見出しがあるときは金額（部品金額・工賃）を位置で補わない。見出しの別の列を金額として読んでいた
            # （「金額」だけの列の部品金額が 0 円、「部品」の列が区分に入る。レビュー 2 周目）。何の金額か分からない列は
            # _resolve_columns_by_data が止める
            if _colmap and key in ('parts_amount', 'wage', 'quantity'):
                idx = None
            else:
                idx = pos if (pos is not None and pos not in _claimed) else None
        return row[idx].strip() if idx is not None and 0 <= idx < len(row) else ''

    row_idx = 0
    # 明細の行のセル数。どの行も見出しより同じだけ多い（全行の末尾に余分なカンマがある書き出し）ときは、部品 1〜3 桁・工賃 3 桁の
    # 行を「割れた金額」とみなさない（クリップ 300 円・工賃 500 円の正しい行を止めていた。レビュー 3 周目）
    # 1 行だけのときは、正しい行と割れた金額の行の形が同じで見分けられないので止める側に倒す
    _data_lens = [len(r) for r in rows[header_idx:] if r and any(str(c or '').strip() for c in r)
                  and not _looks_like_header(r) and str(r[0] if r else '').strip() != '品名']
    _amt_pos = [q for q in ((_colmap.get('parts_amount'), _colmap.get('wage')) if _colmap else (3, 4)) if q is not None]
    _name_i0 = _colmap.get('name', 0) if _colmap else 0

    def _is_detail_row(r):
        # 集計行・注意書き・見出しの繰り返しは「明細」ではない（合計行の 4 桁の金額で「桁区切りを使っていない CSV」と
        # 判断し、割れた金額を通していた。レビュー 7 周目）
        if not r or not any(str(c or '').strip() for c in r) or _looks_like_header(r):
            return False
        nm0 = str(r[_name_i0] if _name_i0 < len(r) else '').strip()
        return not nm0.startswith(('⚠', '❌', '※')) and not _is_total_row_name(nm0)
    # 桁区切りの無い 4 桁以上の金額が明細に 2 つ以上あれば、この CSV は桁区切りを使っていない
    _no_sep = sum(1 for r in rows[header_idx:] if _is_detail_row(r)
                  for q in _amt_pos if q < len(r) and re.fullmatch(r'-?\d{4,}', str(r[q] or '').strip())) >= 2
    _hdr_n = len(_hdr_cells) if header_idx > 0 else 6   # 見出しの無い CSV は既定の 6 列（品名,区分,数量,部品金額,工賃,部品コード）
    _uniform_extra = (len(_data_lens) >= 2 and len(set(_data_lens)) == 1
                      and _data_lens[0] > _hdr_n and _no_sep)   # 全行が同じだけ割れた CSV と見分ける（レビュー 4 周目）
    _name_col = _colmap.get('name', 0) if _colmap else 0
    _has_qty_col = (not _colmap) or ('quantity' in _colmap)
    _split_like: list = []      # 割れた行と同じ形だが、桁区切りを使っていない CSV なので通した行（知らせる）
    _lever_rows: list = []      # 単価をレバーレートとみて部品 0 円にした行
    _short_rows: list = []      # 列が見出しより少ない行
    _frac_rows: list = []       # 数量が整数でない行

    def _few_rows(names):
        # 知らせは 1 件にまとめる（行ごとに出すと行数ぶん並び、読み飛ばされる。レビュー 9 周目）
        return '・'.join(names[:3]) + (f' ほか {len(names) - 3} 行' if len(names) > 3 else '')
    # その CSV のほかの行に数量が書いてあるか（数量が空欄なのが例外のときだけ、空欄を列ずれの印にする。数量を落とした CSV を
    # まるごと止めていた。レビュー 6 周目）
    _qty_used = any(_cell(r, 'quantity', 2).strip() for r in rows[header_idx:] if _is_detail_row(r))

    def _shift_reason(row, name, qty_raw, pa_raw, wg_raw, up_raw, part_no, qty_ok, nums_ok, extra_cells):
        '''1 列ずれた行の形か（品名のカンマ・桁区切りのカンマで割れた行）。ずれて見えるわけを返す（無ければ ''）。
        見出しより 1 セル多い行（部品コードが空の行の末尾のカンマ。アプリの指示文どおりの形）と同じセル数の行に同じ確かめを
        かける（多い行には品名のカンマの確かめがかからず、工賃が品番の欄に入って消えていた。レビュー 5 周目）'''
        q, pa, wg, up, pn = (str(v or '').strip() for v in (qty_raw, pa_raw, wg_raw, up_raw, part_no))
        if not qty_ok or (extra_cells and not nums_ok):
            return '数量か金額の欄が数字として読めません'
        if _delim != ',' and not _is_md:
            return ''       # タブ区切り（表計算ソフト）の欄は割れない
        nxt = str(row[_name_col + 1] if _name_col + 1 < len(row) else '')
        if ((name.count('(') + name.count('（')) > (name.count(')') + name.count('）'))
                and (nxt.count(')') + nxt.count('）')) > (nxt.count('(') + nxt.count('（'))):
            # 品名の括弧が次の欄で閉じる（「写真代(事故,修理後)」が割れた形）。括弧が閉じないだけなら、元の見積で品名が途中で
            # 切れたこともあるので止めない（レビュー 5 周目）
            return '品名の括弧が次の欄で閉じています（品名のカンマで割れた形）'
        if re.fullmatch(r'0\d{1,2}', pn):
            # 「12,000」が割れた後半（012・000）。工場の社内コードには 3 桁もあるので、0 で始まらない数字だけでは止めない
            # （正しい行をまるごと止めていた。レビュー 6 周目）
            return f'品番の欄が 0 で始まる数字（{pn}）です（桁区切りのカンマで割れた形）'
        # 区分が空欄の行（指示文では 写真代・研磨・ショートパーツ）の品名のカンマ: 数量の欄に空の区分、部品金額の欄に数量、品番の欄に
        # 工賃が来る。指示文は数量を必ず書かせる（不明は 1）ので、ほかの行に数量があるのにこの行だけ空欄なら止める
        _q_blank = (not (q if _has_qty_col else pa)) and (_qty_used or not _has_qty_col)
        if _has_qty_col and _q_blank and (re.fullmatch(r'-?[1-9]\d?', pa) or re.fullmatch(r'-?[1-9]\d?', up)):
            return '数量が空欄で、部品金額が 1〜99 です（区分が空欄の行の品名のカンマで割れた形）'
        if _q_blank and re.fullmatch(r'\d{1,7}', pn):
            return (('数量' if _has_qty_col else '部品金額')
                    + f'が空欄で、品番の欄が数字だけ（{pn}）です（区分が空欄の行の品名のカンマで割れた形）')
        if (not (q if _has_qty_col else pa)) and re.fullmatch(r'\d{1,7}', pn) and safe_int(pa) == 0 and safe_int(wg) == 0:
            # 部品も工賃も 0 なのに品番の欄にだけ数字がある: 正しい明細ではあり得ない（本当の工賃が品番の欄に来た形）。
            # どの行にも数量が無い CSV でも止める（レビュー 7 周目）
            return f'部品金額も工賃も 0 で、品番の欄にだけ数字（{pn}）があります（品名のカンマで割れた形）'
        if _delim != ',':
            return ''
        # 桁区切りのカンマで割れた金額（「45,500」→ 45 と 500、「12,000」→ 12 と 000）
        if any(re.fullmatch(r'0\d{2}', v) for v in (pa, wg)):
            return '金額が 0 で始まる 3 桁です（桁区切りのカンマで割れた形）'
        if re.fullmatch(r'-?[1-9]\d{0,2}', pa) and re.fullmatch(r'\d{3}', wg):
            # 「45,500」が割れた形（部品 45・工賃 500）。止めるのは、割れた行の品番の欄に本当の工賃が来ている（品番が数字）か、
            # 見出しより多いセルのある行（全行に同じ余分なカンマがある書き出しを除く）。
            # 桁区切りの無い 4 桁以上の金額がその CSV にあれば、桁区切りを使っていない CSV なので止めない（部品 300 円・
            # 工賃 500 円・社内品番の正しい行を止め、直しようが無かった。レビュー 6 周目）。
            # 止めなかった行は必ず読んだ値を知らせる（品番の欄が空の行が ❌ にも ⚠️ にもかからず黙って通っていた。レビュー 10 周目）
            if ((extra_cells and not _uniform_extra)
                    or (not extra_cells and not _no_sep and re.fullmatch(r'0|[1-9]\d*', pn))):
                return '部品金額が 1〜3 桁で、工賃がちょうど 3 桁です（桁区切りのカンマで割れた形）'
            _split_like.append(f'「{name}」（部品金額 {pa}・工賃 {wg}・品番 {pn or "（空）"}）')
        return ''
    for row in rows[header_idx:]:
        if not row or not any(c.strip() for c in row):
            continue
        _row_len_orig = len(row)
        # 列数が足りない場合は右側を空文字で補完
        while len(row) < 6:
            row.append('')

        # 2ページ目以降で繰り返される見出し行が、品名「品名」の
        # 0円明細としてNEOに書き込まれるのを防ぐ
        if _looks_like_header(row) or (row[0] or '').strip() == '品名':
            continue

        name      = _cell(row, 'name', 0)
        category  = _cell(row, 'work_code', 1)
        _qty_raw  = _cell(row, 'quantity', 2)
        _frac_row = False
        _pa_raw   = _cell(row, 'parts_amount', 3)
        _wg_raw   = _cell(row, 'wage', 4)
        _up_raw   = _cell(row, 'unit_price', None)
        qty       = safe_int(_qty_raw, 1)
        if is_fractional_qty(_qty_raw):
            # コグニの数量は整数。数量 1・金額そのままで取り込む（丸めた数量で単価の税×数量を当てると税が変わる。L7）。
            # 知らせるのは取り込む行だけ（集計行・品名の無い行・注意書きの行にも出していた。レビュー 10 周目）
            qty = 1
            _frac_row = True
        wage_amt  = safe_int(_wg_raw)
        # 行の合計の列で 単価×数量＋工賃 と裏が取れた行か（その欄に金額が書いてある行だけ。レビュー 8 周目）。
        # 数として読めるときだけ（「-」「***」「¥ -」のような会計表示のゼロでも旗が立ち、レバーレートの単価を無警告で
        # 部品に足していた。レビュー 9 周目）
        _upc_i = _colmap.get('_unit_parts_col')
        _upc_raw = str(row[_upc_i] or '').strip() if (_upc_i is not None and _upc_i < len(row)) else ''
        _row_total_ok = bool(_upc_raw) and _normalize_number_text(_upc_raw) is not None
        # 金額の列が無い・空の行で単価だけある: 行の金額は 単価×数量（M6。「金額」の列が空の行も 0 円にしていた。レビュー 2 周目）。
        # 数量が小数でも元の数量で掛ける（数量 1 にする前に）。「金額」の列の確かめと同じ関数で決める（レビュー 5 周目）
        parts_amt = _csv_row_parts(_pa_raw, _up_raw, _qty_raw, wage_amt, _unit_generic, 'parts_amount' in _colmap,
                                   _cell(row, 'index_value', None), _row_total_ok)
        _lever_row = parts_amt is None
        if _lever_row:
            # 単価×数量（または単価×工数）がちょうど工賃と同じで、部品金額の列が無い行（レビュー 6 周目）。
            # 単価はレバーレート（工賃の時間単価）とみて部品 0 円にする。知らせるのは取り込む行だけ（集計行・注意書きの行にも
            # 出して、消えた金額があるように見せていた。レビュー 7 周目）
            parts_amt = 0
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
        _mark = name.strip().startswith(('⚠', '❌', '※'))
        _strict_amt = [v for v in (_pa_raw, _wg_raw)
                       if re.fullmatch(r'[-+−△▲]?[¥￥]?\d[\d,，]*(\.\d+)?円?', str(v or '').strip())]
        # 「⚠」「❌」で始まるのに金額の欄に数字がある行は、注意書きなのか明細なのか分からない（AI が注意書きの中に明細を写した形で、
        # 明細として数えると二重になる。レビュー 4 周目）
        if name.strip().startswith(('⚠', '❌')) and _strict_amt:
            _errors.append(f'❌ 「{name.strip()[:24]}」の行は、注意書きなのか明細なのか分かりません（金額の欄に数字が入っています）。'
                           '明細なら品名の先頭の記号を消し、注意書きなら金額を消してください。')
            continue
        # 「※」で始まり金額の欄が数字だけでない（「5行目」など）行も注意書き（部品 7・工賃 9 の明細にしていた。レビュー 4 周目）
        _marker_note = _mark and not _strict_amt
        if ((_marker_note or (parts_amt == 0 and wage_amt == 0 and not part_no))
                and (_marker_note
                     or (sum(1 for c in row if str(c or '').strip()) == 1
                         and (re.search(r'[。．!！?？]', name)
                              or re.search(r'(です|ます|ください|とおり|下さい)', name)
                              or len(name) > 24)))):
            # 注意書きの中のカンマでセルが割れていてもつなげる（列ずれで取り込みが止まる・0 円の明細になっていた。レビュー 3 周目）
            _tn = ','.join(str(c).strip() for c in row if str(c or '').strip()) if _marker_note else name.strip()
            # AI が CSV の後ろに書いた「❌」「⚠️」の注意（「3 行読み取れませんでした」など）は、閉じた欄に隠さず警告として出す。
            # 取り込みは止めない（先頭が「⚠️ CSV の中の注意」なので、取り込みを止める ❌ とは区別される。レビュー 2 周目）
            _trailer_notes.append(('⚠️ CSV の中の注意: ' + _tn.lstrip('❌⚠\ufe0f ').strip())
                                  if _tn.startswith(('❌', '⚠', '※')) else _tn)
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
            if str(_qty_raw or '').strip():
                _trailer_notes.append(f'⚠️ 「{name}」の数量 {_qty_raw} は 1 未満なので 1 にしました（値引きは金額のマイナスで書きます）。')
            qty = 1
        # 列のずれ: 見出しより右に値がある行（引用符の無いカンマ付き金額「45,000」や品名のカンマ）。
        # 黙って取り込むと金額が千分の一になったり工賃に移ったりする（バグハント 3 回目 M2）
        # 見出しがあるときは見出しより「セルの数」が多い行（空のセルでも）。「クリップ,取替,10,1,550,0,」のように
        # 末尾の部品コードが空だと、ずれたセルは空になるので値の有無では見分けられない
        _extra = ([c for c in row[len(_hdr_cells):] if str(c or '').strip()] if header_idx > 0
                  else [c for c in row[_hdr_len:] if str(c or '').strip()])
        # 数量・金額の欄が数字として読めるか（品名・区分のカンマで 1 列ずれると、数量の欄に「取替」などが来る）
        _qty_ok = ((not str(_qty_raw or '').strip()) or _normalize_number_text(_qty_raw) is not None
                   or bool(re.fullmatch(r'(一式|式|ｾｯﾄ|セット|(?i:set)|[-‐－ーｰ―*＊])', str(_qty_raw).strip())))
        _nums_ok = _qty_ok and _amount_ok(_pa_raw) and _amount_ok(_wg_raw) and _amount_ok(_up_raw)
        _why = ''
        if _extra:
            if (len(_extra) == 1 and 'part_no' not in _colmap and header_idx > 0 and _nums_ok
                    and _looks_like_part_no(_extra[0]) and str(part_no or '').strip() in ('', str(_extra[0]).strip())
                    and not _shift_reason(row, name, _qty_raw, _pa_raw, _wg_raw, _up_raw, part_no, _qty_ok, _nums_ok, False)):
                # 見出しの無い品番の列（見出しより 1 つ右。英字・ハイフン入り）: 品番として受ける。数量・金額が数字として読める行
                # だけ（品名のカンマで 1 列ずれた行の「-」「OEM」を品番として受け、部品 1・工賃 45,000 で入っていた。レビュー 2 周目）
                part_no = str(_extra[0]).strip()
                _extra = []
            else:
                _why = '見出しより右の欄に値があります'
        else:
            # 見出しより多いセルが全部空の行（末尾のカンマ）も、見出しと同じセル数の行も、同じ確かめで 1 列ずれた形を探す
            # （「45,500」が 2 つのセルに割れて部品 45・工賃 500、品名のカンマで数量の欄に「取替」。レビュー 2・3・5 周目）
            _why = _shift_reason(row, name, _qty_raw, _pa_raw, _wg_raw, _up_raw, part_no, _qty_ok, _nums_ok,
                                 _row_len_orig > _hdr_n)
            if _why:
                _extra = ['']
        if _extra:
            _errors.append(f'❌ 「{name}」の行は列がずれています（{",".join(str(c).strip() for c in row if str(c).strip())[:60]}。'
                           f'数量 {_qty_raw or "（空）"}・部品金額 {_pa_raw or "（空）"}・工賃 {_wg_raw or "（空）"}・品番 {part_no or "（空）"} と'
                           f'して読めました。{_why}）。金額の桁区切りのカンマ（45,000）・品名のカンマ・見出しの無い列が考えられます。'
                           '金額はカンマ無し（45000）にし、カンマを含む欄は「"」で囲み、列には見出しを付けてください。'
                           + ('（品名のカンマでなければ、数量の欄に数量を入れてください）'
                              if _why.startswith(('数量が空欄', '部品金額も工賃も 0')) else '')
                           + ('（金額がこのとおりで正しいなら、品番の欄を「P-12345」のように数字だけでない形にするか、'
                              'タブ区切り〔表計算ソフトからの貼り付け〕で貼り直してください）'
                              if _why.startswith(('部品金額が 1〜3 桁', '部品金額も工賃も 0')) else '')
                           + ('（金額がこのとおりで正しいなら、行の終わりの余分なカンマを消してください）'
                              if _row_len_orig > _hdr_n and not any(str(c or '').strip() for c in row[_hdr_n:]) else ''))
            continue
        if header_idx > 0 and _row_len_orig < len(_hdr_cells) and any(
                k in _colmap and _colmap[k] >= _row_len_orig for k in ('parts_amount', 'wage', 'unit_price')):
            _short_rows.append(f'「{name}」')
        # 桁区切りを「.」で書いた金額（「45.000」）は 1/1000 になる。単価は「155.5」が正しいことがあるので部品金額・工賃だけ
        # （レビュー 10 周目）
        _dot_amt = [f'{lbl}「{raw}」' for lbl, raw in (('部品金額', _pa_raw), ('工賃', _wg_raw))
                    if re.fullmatch(r'[-+]?\d+\.\d{3}', str(raw or '').strip())]
        if _dot_amt:
            _errors.append(f'❌ 「{name}」の{"・".join(_dot_amt)}は小数点付きです（桁区切りのカンマを「.」と書くと 1/1000 に'
                           'なります）。金額は「45000」のように、区切りを入れずに書いてください。')
            continue
        _bad_amt = [lbl for lbl, raw in (('部品金額', _pa_raw), ('工賃', _wg_raw), ('単価', _up_raw)) if not _amount_ok(raw)]
        if _bad_amt:
            _errors.append(f'❌ 「{name}」の{"・".join(_bad_amt)}が数字として読めません'
                           f'（{" / ".join(str(r) for r in (_pa_raw, _wg_raw, _up_raw) if str(r or "").strip())[:40]}）。'
                           '半角の数字（例 45000）で書いてください。')
            continue

        if _lever_row:
            _lever_rows.append(f'「{name}」（工賃 {wage_amt:,}円）')
        if _frac_row:
            _frac_rows.append(f'「{name}」（数量 {_qty_raw}）')
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
    if _short_rows:
        _trailer_notes.append('⚠️ ' + _few_rows(_short_rows) + 'の行は列が見出しより少なく、金額の欄がありません'
                              '（0 円として取り込みました）。')
    if _frac_rows:
        _trailer_notes.append('⚠️ ' + _few_rows(_frac_rows) + 'の数量は整数でないので、数量 1・金額そのままで取り込みました。')
    if _lever_rows:
        _trailer_notes.append('⚠️ ' + _few_rows(_lever_rows) + 'は「単価」×数量（または工数）が工賃と同じなので、'
                              '単価を工賃のレバーレート（時間単価）とみて部品 0 円で取り込みました。'
                              '部品の金額なら「部品金額」の列に書いてください。')
    if _split_like:
        _trailer_notes.append('⚠️ 次の行は、桁区切りのカンマで割れた行と同じ形です（この CSV はほかの行で桁区切りを使っていないので'
                             'そのまま読みました）: ' + _few_rows(_split_like)
                             + '。金額が違っていたら、金額をカンマ無しで書き直してください。')
    if _dropped_amount:
        # 金額のある行を落とした知らせは先頭に置く（注記が多いと隠れていた。M7）
        _trailer_notes.insert(0,
            f'⚠️ 品名が空欄の行を{len(_dropped_amount)}行読み飛ばしました'
            f'（金額の合計 {sum(_dropped_amount):,}円）。'
            'CSVの品名欄をご確認ください。')
    if header_idx > 0 and _colmap and not any(k in _colmap for k in ('parts_amount', 'wage', 'unit_price')):
        # 見出しはあるのに金額の列が分からない（「値段」など）。位置で推し量ると別の列を金額として読む（M6）
        _errors.insert(0, '❌ 見出しに金額の列（部品金額・工賃・技術料・単価）が見つかりません。'
                          '1行目を「品名,区分,数量,部品金額,工賃,部品コード」にしてください。'
                          '（AI の返事に説明文が多いときは、表の部分だけを貼ってください）')
    if _errors:
        # 誤りのある CSV は取り込まない（1 行でも列がずれていると、ほかの行も同じずれ方をしている疑いがある）
        return ([], _errors + _trailer_notes) if return_notes else []
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
        qty   = qty_int(qty_raw, 1)
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
        qty         = qty_int(_md_cell('quantity', 4), 1)   # 小数の数量（2.5 L）は数量 1・金額そのまま（切り捨てて 2 にしていた。レビュー 2 周目）
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
                # 返事が途中で切れた（max_tokens）・安全性で止まった返事は、補って使っても明細が欠けている。
                # 「不完全」の印を付けて控えない・知らせる（以前は 6 行中 3 行だけ採用して成功として控えていた。P2）
                try:
                    _fr = response.candidates[0].finish_reason if response.candidates else None
                    _fin = str(getattr(_fr, 'name', _fr) or '')
                except Exception:  # noqa: BLE001
                    _fin = ''
                _res = {
                    'items':           items,
                    'discount_amount': 0,
                    'confidence':      0.9,
                }
                if _fin and _fin != 'STOP':
                    _res['_incomplete'] = True
                    _res['_incomplete_reason'] = f'明細の読み取りの返事が途中で終わりました（finish_reason={_fin}）'
                return _res
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
            if _is_quota_error(err_msg):
                _quota_exhausted_set().add(model_name)
                cache_key = api_key[-8:] if api_key else ''
                if cache_key in _availability_cache():
                    del _availability_cache()[cache_key]
                raise ValueError(
                    f"モデル '{model_name}' のクォータが上限に達しました。"
                    "しばらく待つか、サイドバーで別のモデルを選んでからやり直してください。"
                ) from e
            # 決まって失敗する 4xx（408 以外）と締め切り切れは送り直さない（call_gemini と同じ。締め切り 10 分を 3 回待たせない）
            _code = getattr(e, 'code', None)
            if ((isinstance(_code, int) and 400 <= _code < 500 and _code != 408)
                    or _is_gemini_timeout(e, err_msg)):
                raise ValueError(f"Gemini API呼び出しに失敗しました: {err_msg[:500]}") from e
            if attempt < 2:
                import time; time.sleep(1)
                continue
            raise ValueError(f"Gemini API呼び出しに失敗しました: {str(last_error)}")


def validate_and_correct_items(items):
    """
    辞書ベースの点検: 作業区分と金額の欄が合わない行を「要確認」として知らせる。**金額は動かさない。**

    以前は「脱着」の行の部品代を 0 にし、「修理・板金・塗装」の行の部品代を 0 にするか工賃へ移していた
    （読み取りが 1 行ずれたときの救済）。しかし正しく読めた見積でも、塗装の行の材料代（部品欄）・
    脱着修理の行の部品代・板金の行の金額が毎回原本と違う .neo になり、その差を「※金額調整」の行が
    埋めていた（行も金額も原本と違う協定見積になる。バグハント 3 回目 O6）。読み取りのずれは総額・
    小計の照合と調整行の確認で捕まるので、ここでは印を付けて知らせるだけにする。

    **品名では判断しない**（「Rﾊﾞﾝﾊﾟ(塗装済)」「ｸﾛｽﾒﾝﾊﾞ(修理)」のような正式な部品名に作業の語が入るのは普通）。

    戻り値: (明細, 要確認の記録) の組。明細は写しを返す（中身は元のまま）。
    """
    # 部品代計上が普通の作業区分
    PARTS_OK_METHODS = {'取替', '交換', '脱着組替', '取外組付'}
    # 脱着系: 部品代が付いているのは読み取りのずれの疑い
    REMOVAL_METHODS  = {'脱着', '取外', '取付', '組付', '脱外'}
    # 修理・塗装系: 部品欄だけに金額があるのは工賃の読み違いの疑い（部品欄と工賃欄の両方に金額がある塗装行は
    # 「材料＋工賃」の正当な形なので知らせない）
    REPAIR_METHODS   = {'修理', '調整', '板金', '塗装', 'ペイント', '研磨',
                        '清掃', '点検', '作業', '修正', '施工', '補修'}

    out = []
    notes = []
    for item in items:
        item      = dict(item)
        method    = str(item.get('method', '') or item.get('work_code', '') or '').strip()
        name      = str(item.get('name', ''))
        parts_amt = safe_int(item.get('parts_amount', 0))
        wage      = safe_int(item.get('wage', 0))
        out.append(item)
        if not parts_amt or not method or any(kw in method for kw in PARTS_OK_METHODS):
            continue
        if any(kw in method for kw in REMOVAL_METHODS):
            if wage == 0 or not any(kw in method for kw in REPAIR_METHODS):
                notes.append(
                    f"「{name}」（{method}）に部品代 {parts_amt:,}円 が付いています（金額は原本の読み取りどおり）。"
                    "脱着の行の部品代は、読み取りが1行ずれた可能性があります。原本をご確認ください。")
            continue
        if any(kw in method for kw in REPAIR_METHODS) and wage == 0:
            notes.append(
                f"「{name}」（{method}）の {parts_amt:,}円 が部品代の欄に入っています（金額は原本の読み取りどおり）。"
                "原本で部品・工賃のどちらの欄かご確認ください。")
    return out, notes


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

    # 送る前に形と大きさを確かめる（バグハント 3 回目 P8/P10/P13）。スキル経路と同じくページ数の上限を置き、権限
    # パスワードだけの暗号化 PDF は暗号を外し、ページ木の申告が巨大な PDF は展開せずに断る。大きすぎるファイルは
    # 送っても必ず失敗するうえ、メモリをファイルの十数倍使うので送らない
    if str(mime_type or '').lower() == 'application/pdf':
        from neo_skill import llm as _nllm
        from neo_skill import reader as _nreader
        try:
            file_bytes = _nllm.pdf_prepare(file_bytes)
            _np_in = _nllm.pdf_page_count(file_bytes)
        except _nllm.LLMError as _pe:
            if 'パスワード' in str(_pe):
                raise ValueError(str(_pe))
            _np_in = None
        except Exception:  # noqa: BLE001  pypdf が読めない（xref・trailer が欠けた等）
            _np_in = None
        if _np_in is None:
            # pypdf で開けない PDF でも PyMuPDF で開けることがある（以前は続けていた。レビュー）。ページ数はそちらで数える
            try:
                import fitz as _fitz_in
                with _PDFIUM_LOCK:
                    with _fitz_in.open(stream=file_bytes, filetype='pdf') as _doc_in:
                        _np_in = int(_doc_in.page_count)
            except Exception:  # noqa: BLE001
                raise ValueError('見積書の PDF を開けません（壊れているか PDF ではありません）。印刷し直した PDF か写真で入れてください。')
        if _np_in > _nreader.MAX_PAGES:
            raise ValueError(f"見積書の PDF が {_np_in} ページあります（上限 {_nreader.MAX_PAGES}）。見積書だけの PDF にして入れてください。")
    file_bytes, mime_type = _gemini_ready_bytes(file_bytes, mime_type)

    def _logw(msg: str):
        """ログをリストと stderr 両方に出力する"""
        _log.append(msg)
        print(f"[ANALYZE] {msg}", file=sys.stderr)

    # ── ファイルハッシュキャッシュ: 同一ファイルの再解析を防ぐ ──────────────
    # mime も鍵に入れる。同じバイト列でも PDF として送るか画像として送るかで
    # 読み取り結果が変わるため、入れないと前の mime での結果がそのまま返る。
    _cache_key = (hashlib.md5(file_bytes).hexdigest()
                  + f"_{used_model}_{use_rasterize}_{use_fax_filter}_{use_enhance}_{enable_self_correction}"
                  + f"_{mime_type}_tax{int(bool(tax_inclusive))}"   # 税区分でプロンプトが変わる（Codex hunt E2）
                  # キーの持ち主ごとに分ける（別の利用者・別のキーの読み取りを返さない。N3/P2）
                  + '_k' + hashlib.sha256(str(api_key or '').encode('utf-8')).hexdigest()[:16])
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

    # 明細抽出は一番重い呼び出し（30秒〜2分）で、このあとの合計欄の解析とは
    # 入力も結果も独立している（合計欄の値は結果のマージにしか使わない）。
    # 合計欄の後ろに直列で置くと、その待ち時間がまるごと積み上がるので、
    # ページ並べ替えが終わって file_bytes が確定したこの時点で先に投げ、
    # 合計欄の処理が終わってから結果だけ受け取る。
    _detail_ex = ThreadPoolExecutor(max_workers=1)
    _fut_detail = None
    try:
        _fut_detail = _detail_ex.submit(
            # 見積書は PDF とは限らない（スマホで撮った JPG/HEIC もある）。
            # ここを 'application/pdf' で固定すると、画像を送っても Gemini が
            # PDF として受け取り、読み取りに失敗する。
            analyze_estimate_single, api_key, file_bytes, mime_type,
            used_model, 1, 1, bool(tax_inclusive))
    except Exception as _e_sub:
        _logw(f"③ 明細解析の先行実行に失敗（直列で続行）: {_e_sub}")
        _detail_ex.shutdown(wait=False)
        _detail_ex = None

    # 先に投げた明細解析は、このあとの処理（ラスタライズ・合計欄の解析など）が
    # 途中で例外を投げても必ず閉じる。閉じないと、画面がエラーを出したあとも
    # ワーカースレッドが残る。
    try:


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
        # 税込表記であることをモデルに伝える指示は、先に投げた呼び出しに含めてある。
        if _fut_detail is not None:
            result = _fut_detail.result() or {}
        else:
            result = analyze_estimate_single(
                api_key, file_bytes, mime_type, used_model, 1, 1,
                tax_inclusive=bool(tax_inclusive)
            ) or {}
    except BaseException:
        # 途中で失敗したときは、先に投げた明細解析の結果を捨てる。
        # まだ動き出していなければ取り消せる（走り始めていたら止められないが、
        # 少なくとも結果を待たずに抜ける）。
        if _fut_detail is not None:
            _fut_detail.cancel()
        raise
    finally:
        if _detail_ex is not None:
            _detail_ex.shutdown(wait=False)
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
    result['items'], _vc_notes = validate_and_correct_items(result['items'])
    if _vc_notes:
        # 区分と金額の欄が合わない行は黙って済ませない（金額は動かさない。O6）
        result.setdefault('_amount_changes', []).extend(_vc_notes)

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
            # 置き換えたことを印で残す。印字の値も別のキーに残す。印が無いと、下流（ベタ打ちの検算）が明細合算を
            # 「見積書に印字された小計」として自分の読み取り結果と比べ、明細の誤りを見逃す（バグハント 3 回目 O2）
            result['pdf_parts_total_printed'] = _cv_p_stated
            result['pdf_wage_total_printed']  = _cv_w_stated
            result['_subtotals_from_items']   = True
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
                result['items'], _vc_notes2 = validate_and_correct_items(retry['items'])
                # 作り直した明細のぶんだけを残す。足すと、捨てたほうの
                # 読み取りで動かした行の警告が出続ける（直った行なのに
                # 「金額を動かした」と表示される）。
                result['_amount_changes'] = list(_vc_notes2)
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
    # 控えるのは「明細があり、印字の合計が読めて、返事が途中で切れていない」読み取りだけ。以前は失敗や欠けた読み取り
    # （明細 0 行・合計欄の呼び出しだけ 429・max_tokens で途中切れ）も控え、押し直しても同じ欠けた結果を返し続けた（P2）
    _cache_ok = (bool(result.get('items')) and not result.get('_incomplete')
                 and any(safe_int(result.get(_k, 0)) > 0 for _k in ('pdf_grand_total', 'pdf_parts_total', 'pdf_wage_total')))
    if _cache_ok:
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
                      has_estimate, reverse_match=False, short_parts_wage=0,
                      parts_ok=False, wage_ok=False, grand_ok=True):
    """
    登録番号から出力ファイル名を生成。
    reverse_match=True の場合は部品・工賃相違を抑制する。
    ショートパーツがPDF側の部品合計に含まれているケースも考慮して比較する。
    parts_ok / wage_ok はステップ③の照合で一致とした（値引き前の小計・総額の一致を含む）もの。grand_ok=False は
    見積書の総額と合わないまま確認して生成したもの（「総額相違」を付ける。レビュー 3 周目）
    """
    dept   = safe_str(cust.get('car_reg_department', ''))
    div    = safe_str(cust.get('car_reg_division', ''))
    biz    = safe_str(cust.get('car_reg_business', ''))
    serial = safe_str(cust.get('car_reg_serial', ''))
    base   = f'{dept}{div}{biz}{serial}'
    if not base.strip():
        # 登録番号が読めていないときに固定の「新規見積」だけを返していたため、
        # 車検証を読ませずに続けて作ると、何件でも「新規見積_見積.neo」になり、
        # ダウンロード先で上書きされたり別案件と取り違えたりする。
        # 協定見積として保険会社に出すファイルなので、車名と日時で見分けられるようにする。
        _cname = re.sub(r'[\\/:*?"<>|\s\x00-\x1f]', '', safe_str(cust.get('car_name', '')))[:20]
        _stamp = now_jst().strftime('%m%d_%H%M%S')
        base = f'{_cname}_{_stamp}' if _cname else f'新規見積_{_stamp}'
    # short_parts_wage（ショートパーツ）は呼び出し側との約束で残すが、判定には使わない（③の parts_ok / wage_ok に入っている）
    discrepancies = []
    if not reverse_match and has_estimate:
        # ショートパーツ（印字の部品計・工賃計に入っている見積がある）の逃げ道は③の判定に入っている。ここで独立に当てると、
        # ③が「相違」としたもの（部品計と工賃計の両方がショートパーツで合う＝印字どうしが矛盾）に印が付かない（レビュー 8 周目）
        parts_match = (calc_parts == pdf_parts) or parts_ok
        if pdf_parts is not None and pdf_parts > 0 and not parts_match:
            discrepancies.append('部品相違')
        wage_match = (calc_wages == pdf_wages) or wage_ok
        if pdf_wages is not None and pdf_wages > 0 and not wage_match:
            discrepancies.append('工賃相違')
    # 総額の 1 円違いは、逆算一致（印字の小計から総額を逆算できたという話）とは別の事実。③が確認を求めた相違が、
    # 逆算一致の見積ではファイル名に出ていなかった（レビュー 6 周目）
    if not grand_ok:
        discrepancies.append('総額相違')
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


def _p2n_new_out(reader_kind, model_name):
    from neo_skill import vendor as _nsk_vendor
    return {'ok': False, 'stage': 'error', 'error': '', 'vendor_commit': _nsk_vendor.commit_short(),
            'reader': {'kind': reader_kind, 'model': model_name or ''}}


def p2n_read(pdf_bytes, file_name, api_key, mime_type='application/pdf',
             vehicle_hint=None, insurance_hint=None, customer_hint=None, progress=None, record_profile=False,
             addata_root=None, reader_kind='claude', model_name=''):
    """見積書 PDF を読む段（LLM ＋ ページ検算 ＋ 合計欄の検算）。成功すると作業フォルダ（pages/・reading.json）を残したまま
    state を返す（p2n_make がそれを使って NEO を作り、作業フォルダを消す）。失敗したら作業フォルダを消して repair_zip を付ける。
    state のキー: ok / stage / error / read / reader / vendor_commit / case_dir / reading / record_profile
    （PC の Addata を橋渡しする経路では、読んだあとに車種フォルダの取り込みを待つので、読む段と作る段を分ける）"""
    from neo_skill import llm as _nsk_llm
    from neo_skill import maker as _nsk_maker
    from neo_skill import reader as _nsk_reader
    from neo_skill import vendor as _nsk_vendor
    out = _p2n_new_out(reader_kind, model_name)
    _why = _nsk_vendor.readiness_error()
    if _why:
        out['error'] = 'pdf-to-neo スキル（vendor）が使えません: ' + _why
        return out
    if not addata_root or not os.path.isdir(str(addata_root)):
        out['error'] = ('Addata（コグニの車種データ）が決まっていないので生成しません。'
                        'サイドバーの「Addata の場所を設定する」で確かめてください'
                        '（別の版の Addata で作らないよう、自動検出には落としません）')
        return out
    _com_dir = os.path.join(str(addata_root), 'COM')
    if not (os.path.isfile(os.path.join(_com_dir, 'KA06_ALL.DB'))
            and (os.path.isfile(os.path.join(_com_dir, 'AnVer.DB')) or os.path.isfile(os.path.join(_com_dir, 'COM.CAB')))):
        # vendor が部分 Addata と認める条件（skill_env.is_addata_partial・bridge.has_com と同じ）。欠けたまま渡すと vendor は何も使えず、
        # 画面は「Addata あり」のまま生成に失敗する（2026-09-15 バグハント 3 回目 Q1）
        out['error'] = ('Addata に COM（車種マスタ KA06_ALL.DB と、データ版 AnVer.DB か COM.CAB）がありません。'
                        'Addata の ZIP・フォルダには COM フォルダを含めてください')
        return out
    case_dir = None
    try:
        if str(mime_type or '').startswith('image/'):
            pdf_bytes = _nsk_llm.image_to_pdf(pdf_bytes)
        try:
            reader = _nsk_llm.make_reader(reader_kind, api_key=api_key or None, model=model_name or '')
            out['reader']['model'] = getattr(reader, 'model', model_name or '')
        except Exception as e:
            out['error'] = f'{reader_kind} の API を使えません（パッケージ／キー）: {e}'
            return out
        case_dir = _nsk_maker.new_case_dir()
        rd = _nsk_reader.read_estimate(pdf_bytes, reader=reader, case_dir=case_dir,
                                       source_name=os.path.basename(str(file_name or 'estimate.pdf')),
                                       vehicle_hint=vehicle_hint, insurance_hint=insurance_hint, customer_hint=customer_hint, progress=progress,
                                       addata_root=addata_root)
        out['read'] = {
            'ok': rd.ok, 'n_pages': rd.n_pages, 'stats': rd.stats, 'usage': rd.usage,
            'fails': rd.fails(), 'warn': list((rd.check or {}).get('warn') or []),
            'traces': [{'page': t.page, 'attempts': t.attempts, 'ok': t.ok, 'rows': t.rows,
                        'fail': list(t.fail), 'warn': list(t.warn)} for t in rd.traces],
            'settings': dict((rd.check or {}).get('settings') or {}),
        }
        out['stage'] = 'read'
        if rd.error or not rd.ok:
            # 読み取りが検算に通らない。NEO は作らない（人が該当ページと差額を見る）。
            # 作業フォルダは消すので、直すための pages/ と merge 結果を zip で渡す
            out['error'] = rd.error
            out['repair_zip'] = _nsk_maker.repair_bundle(case_dir, rd.reading)
            _left = _nsk_maker.remove_case_dir(case_dir)
            if _left:
                out['cleanup_warning'] = f'作業フォルダを消せませんでした。手で削除してください: {_left}'
            return out
        out['ok'] = True
        out['case_dir'] = case_dir
        out['reading'] = rd.reading
        out['record_profile'] = bool(record_profile)
        return out
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
        if case_dir:
            _left = _nsk_maker.remove_case_dir(case_dir)
            if _left:
                out['cleanup_warning'] = f'作業フォルダを消せませんでした。手で削除してください: {_left}'
        return out
    except BaseException:   # RerunException（生成中に画面を触った）など: 読みかけの作業フォルダ（reading.json 入り）を残さない（H8）
        if case_dir:
            _nsk_maker.remove_case_dir(case_dir)
        raise


def p2n_make(state, addata_root=None, record_profile=None, progress=None):
    """p2n_read の state から NEO と確認箇所シートを作る（vendor の make_neo.py）。作業フォルダは終わったら消す。
    戻り値は run_pdf_to_neo_skill と同じ形"""
    from neo_skill import maker as _nsk_maker
    out = dict(state or {})
    out['ok'] = False
    case_dir = out.pop('case_dir', None)
    reading = out.pop('reading', None)
    if record_profile is None:
        record_profile = bool(out.pop('record_profile', False))
    else:
        out.pop('record_profile', None)
    if not case_dir or not os.path.isdir(str(case_dir)):
        out['stage'] = 'error'
        out['error'] = '読み取りの作業フォルダが無くなっています（時間が経ちすぎたか、アプリが再起動しました）。もう一度読み取ってください'
        return out
    if not addata_root or not os.path.isdir(str(addata_root)):
        out['stage'] = 'error'
        out['error'] = 'Addata（コグニの車種データ）が決まっていないので生成しません'
        _nsk_maker.remove_case_dir(case_dir)
        return out
    try:
        if progress:
            progress('下書き → ADDATA 突合せ → NEO 生成 → 検算（pdf-to-neo スキル make_neo.py）')
        # allow_neo_total: 工場の単価に円未満の端数がある見積（コグニの円計算では印字の合計を再現できない案件）を、
        # 下書きが書いた 3 点セット（totals.neo_total / tolerance / tolerance_reason）で合格にする。
        # make_neo 側が 3 点セットの有無と run_case の合格を確かめるので、いつも渡してよい（2026-09-16 フリード）
        mk = _nsk_maker.make_neo(case_dir, 'estimate', no_profile=not record_profile, addata_root=addata_root, allow_neo_total=True)
        # 検算に差があり、工賃欄が空欄（工賃も指数も無い）の行があるときは、その行を 0 円（印字どおり）にして作り直す。
        # 生成器は空欄に標準指数を補うが、実案件（精算見積・工場見積・コグニ印刷）では空欄 = 0 円のことが多く、
        # 見積書合計と合わずに不合格になっていた（2026-09-15 実機テスト 6 本中 4 本）。合うときだけ採用し、行名を注意に出す
        _bw_names = []
        if not mk.ok and reading and ('検算に差' in str(mk.error or '') or any('検算に差' in str(r) for r in (mk.reasons or []))):
            _bw = _blank_wage_rows(reading)
            if _bw:
                import copy as _copy
                _rd2 = _copy.deepcopy(reading)
                for _bi, _ri, _nm in _bw:
                    _rd2['blocks'][_bi]['rows'][_ri] = _row_with_wage_zero(
                        _rd2['blocks'][_bi]['rows'][_ri], '要確認: 工賃欄が空欄のため 0 円で作成（標準指数では見積書合計に合わなかった）')
                _first_report = _nsk_maker.read_text(mk.report_path)
                _first_repair = _nsk_maker.repair_bundle(case_dir, reading)   # 1 回目の estimate/report と元の reading（再試行で上書きされる前）
                _nsk_maker.write_reading(case_dir, _rd2)
                if progress:
                    progress(f'標準指数では見積書合計に合わないので、工賃欄が空欄の {len(_bw)} 行を 0 円として作り直しています')
                mk2 = _nsk_maker.make_neo(case_dir, 'estimate', no_profile=not record_profile, addata_root=addata_root, force_draft=True, allow_neo_total=True)
                if mk2.ok:
                    mk = mk2
                    _bw_names = [n for _, _, n in _bw]
                    _rd_info = out.get('read') if isinstance(out.get('read'), dict) else {}
                    _warns = list(_rd_info.get('warn') or [])
                    _warns.append(f'工賃欄が空欄の {len(_bw)} 行（' + '、'.join(n[:12] for n in _bw_names[:5]) + ('…' if len(_bw_names) > 5 else '')
                                  + '）は、標準指数だと見積書合計に合わなかったため、印字どおり 0 円で作りました。確認箇所シートに要確認として載せています')
                    _rd_info['warn'] = _warns
                    out['read'] = _rd_info
                    out['blank_wage_zero'] = _bw_names
                else:
                    _nsk_maker.write_reading(case_dir, reading)   # 元の reading に戻す
                    out['blank_wage_retry'] = {'rows': [n for _, _, n in _bw], 'reasons': list(mk2.reasons or []), 'error': mk2.error}
                    out['_first_report_md'] = _first_report
                    out['_first_repair_zip'] = _first_repair
        out['stage'] = 'make'
        out['make'] = {'ok': mk.ok, 'match_line': mk.match_line, 'reasons': list(mk.reasons),
                       'error': mk.error, 'tail': '\n'.join(mk.stdout.splitlines()[-40:])}
        out['report_md'] = out.pop('_first_report_md', None) or _nsk_maker.read_text(mk.report_path)
        if not mk.ok:
            out['error'] = mk.error
            out['repair_zip'] = out.pop('_first_repair_zip', None) or _nsk_maker.repair_bundle(case_dir, reading)
            return out
        out['neo_bytes'] = _nsk_maker.read_bytes(mk.neo_path)
        out['review_bytes'] = _nsk_maker.read_bytes(mk.review_path)
        out['review_ext'] = os.path.splitext(mk.review_path or '')[1] or '.xlsx'
        # ダウンロード名は <顧客>_<車名>_claude（HANDOFF §4 段 8 の規則）。読めなければ 見積_claude
        cust = ''
        car = ''
        try:
            est = json.load(open(mk.estimate_path, encoding='utf-8-sig')) if mk.estimate_path else {}
            cust = str(((est.get('customer') or {}).get('name')) or '').strip()
        except Exception:
            est = {}
        m = re.search(r'^- 車両: (.+?) /', out['report_md'] or '', re.M)
        if m:
            car = m.group(1).strip()
        stem = '_'.join(x for x in (cust, car) if x) or '見積'
        out['download_name'] = re.sub(r'[\\/:*?"<>|\r\n\t\s]+', '_', stem) + '_claude'
        out['ok'] = bool(out['neo_bytes'] and out['review_bytes'])
        out['stage'] = 'done' if out['ok'] else 'make'
        if not out['ok']:
            out['error'] = 'NEO と確認箇所シートが組で作られなかった'
        return out
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
        return out
    finally:
        _left = _nsk_maker.remove_case_dir(case_dir)
        if _left:
            out['cleanup_warning'] = ('作業フォルダを消せませんでした。見積書・NEO・確認箇所シートが残っています。'
                                      f'手で削除してください: {_left}')


def _render_beta_result(_p2n_res, selected_model):
    """Addata なしのベタ打ち（旧経路 run_pdf_to_neo_pipeline）の結果を出す。
    b15bd06 で UI から外した表示を、部品コード無しの断りを添えて戻した（2026-09-14）"""
    st.info("✏️ ベタ打ち（Addata なし）で作った NEO です: 明細・金額・品名は見積書のとおりですが、"
            "部品コード・標準品番・標準指数は入っていません。部品コードまで入れるなら、"
            "サイドバー「🖥️ PC の Addata をこの画面から使う」で C:\\Addata を選んで作り直してください。")
    if _p2n_res.get('error'):
        st.error(f"❌ {_p2n_res['error']}")
        for _w in (_p2n_res.get('warnings') or []):
            st.caption(f"・{_w}")
        return
    if not _p2n_res.get('ok'):
        st.error("❌ 見積書からNEOを生成できませんでした。")
        for _w in (_p2n_res.get('warnings') or []):
            st.caption(f"・{_w}")
        return
    _p2n_items = _p2n_res.get('items') or []
    if not _p2n_items:
        st.error("❌ 見積書から明細を1行も読み取れませんでした。スキャン画像で文字が読めない、APIのクォータ超過、"
                 "対応していない書式のいずれかが考えられます。")
        for _w in (_p2n_res.get('warnings') or []):
            st.caption(f"・{_w}")
        return
    _p2n_parts = sum(safe_int(it.get('parts_amount', 0)) for it in _p2n_items)
    _p2n_wage = sum(safe_int(it.get('wage', 0)) for it in _p2n_items)
    st.success(f"✅ 解析完了（ベタ打ち） — {len(_p2n_items)}行 ／ 部品 ¥{_p2n_parts:,} ／ 工賃 ¥{_p2n_wage:,}")
    for _w in (_p2n_res.get('warnings') or []):
        st.warning(f"⚠️ {_w}")
    _p2n_qty_over = [str(_it.get('name', '') or '') for _it in _p2n_items if safe_int(_it.get('quantity', 1), 1) > 99]
    if _p2n_qty_over:
        st.warning(f"⚠️ 数量が100以上の行が{len(_p2n_qty_over)}件あります（{'、'.join(_p2n_qty_over[:3])}"
                   f"{'ほか' if len(_p2n_qty_over) > 3 else ''}）。コグニセブンの注記欄は数量が2桁までのため、"
                   "注記側は99として書かれます（明細欄には原本どおりの数量が入ります）。")
    _p2n_v = _p2n_res.get('verify') or {}
    if _p2n_v.get('error'):
        st.warning(f"🔍 検証できませんでした（{_p2n_v['error']}）。生成NEOと原本を突き合わせていません。"
                   "「プレビューに取り込む」で1行ずつご確認ください。")
    elif _p2n_v.get('ok'):
        st.caption("🔍 検証OK: 生成NEOの明細件数と"
                   + ("部品・工賃の金額（税抜）" if _p2n_v.get('wage_match') else "部品金額（税抜）")
                   + ("、および総額" if _p2n_v.get('grand_match') else "") + "が原本と一致しました。"
                   + ("" if _p2n_v.get('wage_match') else "（見積書に工賃計が印字されていないため、工賃は突き合わせていません）"))
    elif not _p2n_v.get('verified_against_pdf'):
        st.warning("🔍 検証できていません: 見積書に印字された部品計が読み取れなかったため、生成NEOと突き合わせていません。"
                   "「プレビューに取り込む」で原本と1行ずつご確認ください。")
    else:
        st.warning("🔍 検証: 原本と生成NEOに差異があります。"
                   f"件数 NEO {_p2n_v.get('neo_count')} / 原本 {_p2n_v.get('pdf_count')}、"
                   f"部品金額(税抜) NEO ¥{safe_int(_p2n_v.get('neo_total')):,} / 原本 ¥{safe_int(_p2n_v.get('pdf_parts_total')):,}"
                   + (f"、工賃(税抜) NEO ¥{safe_int(_p2n_v.get('neo_wage_total')):,} / 原本 ¥{safe_int(_p2n_v.get('pdf_wage_total')):,}"
                      if _p2n_v.get('wage_match') is False else "")
                   + "。「プレビューに取り込む」で内容を確認・修正してください。")
    _p2n_ac = _p2n_res.get('amount_changes') or []
    if _p2n_ac:
        st.warning(f"⚠️ 作業区分と金額の欄が合わない行が {len(_p2n_ac)} 行あります（金額は読み取りどおり。読み取りのずれの疑い）: "
                   + ' / '.join(str(x)[:60] for x in _p2n_ac[:4]) + ('…' if len(_p2n_ac) > 4 else '') + "。原本と突き合わせてください")
    _p2n_neo = _p2n_res.get('neo_bytes')
    # 原本と差がある可能性のある結果（金額調整の行・検証の差・金額列の補正）は、確認のチェックを入れないと落とせない
    # （プレビュー取り込みで直す道は残す。Codex hunt E1/E4 2026-09-15）
    _p2n_adj = bool(_p2n_res.get('adjustment_amount')) or any(isinstance(_it, dict) and _it.get('is_adjustment_row') for _it in _p2n_items)
    _p2n_vfail = bool(_p2n_v.get('verified_against_pdf')) and not _p2n_v.get('ok') and not _p2n_v.get('error')
    # 印字の合計が読めず突き合わせできなかった／検証が例外で欠けた／ページ境界の行を統合した結果も、確認してから（バグハント K6）
    _p2n_unverified = (not _p2n_v.get('verified_against_pdf')) or bool(_p2n_v.get('error'))
    _p2n_dedup = bool(_p2n_res.get('dedup_merged'))
    _p2n_needs_ack = _p2n_adj or _p2n_vfail or bool(_p2n_ac) or _p2n_unverified or _p2n_dedup
    _p2n_ack = True
    if _p2n_needs_ack and _p2n_neo and not _p2n_res.get('stale'):
        st.warning("⚠️ この NEO は原本と差がある可能性があります（金額調整の行／検証の差／金額列の補正）。「プレビューに取り込んで修正する」で"
                   "直すか、内容を確かめた上でチェックを入れてからダウンロードしてください。")
        _p2n_ack = st.checkbox("差異と警告を確認しました（このままダウンロードする）", key='pdf2neo_beta_ack', value=False)
    if _p2n_neo:
        _p2n_name = st.session_state.get('_pdf2neo_filename')
        if not _p2n_name:
            _p2n_name = generate_filename(_p2n_res.get('vehicle_info') or {}, 0, 0, 0, 0, False, reverse_match=True)
            st.session_state['_pdf2neo_filename'] = _p2n_name
        st.download_button("📥 NEOファイルをダウンロード（ベタ打ち）", data=_p2n_neo, file_name=_p2n_name,
                           mime="application/octet-stream", key='pdf2neo_dl_beta', width='stretch',
                           disabled=bool(_p2n_res.get('stale')) or not _p2n_ack)   # 入力が変わった／未確認の結果は落とさせない（Codex hunt A1/E1）
    if st.button("📝 プレビューに取り込んで修正する", key='pdf2neo_to_preview_beta', width='stretch',
                 disabled=bool(_p2n_res.get('stale'))):   # 入力が変わった結果は取り込ませない（Codex 70）
        st.session_state['csv_items'] = _p2n_items
        st.session_state['csv_mode'] = True
        _carry = '税込み（内税）' if st.session_state.get('pdf2neo_tax_inclusive') else '税抜き（外税）'
        st.session_state['tax_override'] = _carry
        st.session_state['_tax_carry_pending'] = _carry
        st.session_state['pdf2neo_vehicle_info'] = _p2n_res.get('vehicle_info') or {}
        # 取り込んだ明細の指紋と、見積書に印字された合計・確認の要否・費用の扱いを一緒に持つ。車両情報と印字の合計は
        # この明細のときだけ使う（別の CSV を入れた案件に前の案件の氏名・登録番号が入っていた。バグハント 3 回目 M3/M8/O9）
        _pm_exp_any = any(safe_int(st.session_state.get(k, 0)) for k in ('exp_towing', 'exp_rental', 'exp_exempt'))
        st.session_state['pdf2neo_preview_meta'] = {
            'sig': _items_sig(_p2n_items),
            'pdf_parts_total': safe_int(_p2n_v.get('pdf_parts_total')) if _p2n_v.get('total_source') == 'pdf_header' else 0,
            'pdf_wage_total': safe_int(_p2n_v.get('pdf_wage_total')) if _p2n_v.get('wage_source') == 'pdf_header' else 0,
            'pdf_grand_total': safe_int(_p2n_v.get('pdf_grand_total')) if _p2n_v.get('grand_match') is not None else 0,
            'grand_is_intax': bool(_p2n_v.get('grand_is_intax', True)),
            'needs_ack': bool(_p2n_needs_ack),
            # 「費用を入れない」を選んでいた（費用があったのにチェックを入れなかった）ときだけ、ステップ④でも入れない
            'exp_declined': bool(_pm_exp_any) and not bool(st.session_state.get('pdf2neo_beta_use_exp',
                                                                                   st.session_state.get('_beta_use_exp_val'))),
        }
        # 前に貼った CSV は消す（ステップ①に戻ったとき、前の CSV を読み直してこの明細を置き換えないように）
        st.session_state.pop('_csv_paste_saved', None)
        _cseq = st.session_state.get('csv_area_seq', 0)
        st.session_state.pop(f'csv_paste_area_{_cseq}', None)
        st.session_state['csv_area_seq'] = _cseq + 1
        st.session_state['vehicle_file_bytes'] = None
        st.session_state['vehicle_file_name'] = None
        st.session_state['estimate_file_bytes'] = None
        st.session_state['estimate_file_name'] = None
        st.session_state['selected_model'] = selected_model
        st.session_state['step'] = 2
        st.rerun()


def run_pdf_to_neo_skill(pdf_bytes, file_name, api_key, mime_type='application/pdf',
                         vehicle_hint=None, insurance_hint=None, customer_hint=None, progress=None, record_profile=False,
                         addata_root=None, reader_kind='claude', model_name=''):
    """見積書 PDF → pdf-to-neo スキル（vendor/pdf_to_neo）で NEO と確認箇所シートを作る（読む → 作る を続けて行う）。

    読む段だけ LLM（neo_skill.reader。reader_kind='claude' か 'gemini'。指示文・検算・読み直しは同じ）。
    判断・生成・検算・合否は vendor の make_neo.py そのもの
    （docs/pdf-to-neo_アプリ移植ガイド.md）。合計を合わせるための調整はどこにも無い。
    戻り値 dict:
      ok / stage('read' | 'make' | 'done' | 'error') / error
      read: {ok, n_pages, stats, usage, fails[], traces[], warn[]}
      make: {ok, match_line, reasons[], tail}
      neo_bytes / review_bytes / review_ext / report_md / download_name / vendor_commit / cleanup_warning
      reader: {kind, model}（どの AI が読んだか）
      repair_zip: 不合格のとき、人が直して続きをするための一式（pages/*.json・reading.json・report.md 等。NEO は入れない）
    insurance_hint: サイドバーの保険情報（policy_no / contractor / accident_date(8桁) / company …）。
      見積書に印字が無い項目にだけ補う（reading の insurance → 生成器の Insurance テーブル）。
    record_profile: 合格時に工場の設定を NEO_check/_profiles に記録する（vendor の既定動作）。
      作業フォルダの外に取引先名が残るので、共有プロセスのアプリでは既定で記録しない。
    addata_root: アプリで決めた ADDATA（サイドバー / URL / ZIP / PC からの橋渡し。find_addata_dir()）。vendor の生成器と
      検算ランナーに環境変数 ADDATA_ROOT として渡し、アプリと同じ版を使わせる。**無ければ止める**。
    作業フォルダは要求ごとに作り、終わったら消す（見積書・NEO・シートには顧客情報が入る）。
    PC の Addata を橋渡しする経路（車種フォルダを読んだあとに取り込む）は p2n_read / p2n_make を分けて呼ぶ（STEP 1-A）。
    """
    state = p2n_read(pdf_bytes, file_name, api_key, mime_type=mime_type, vehicle_hint=vehicle_hint,
                     insurance_hint=insurance_hint, customer_hint=customer_hint, progress=progress, record_profile=record_profile,
                     addata_root=addata_root, reader_kind=reader_kind, model_name=model_name)
    if not state.get('ok'):
        return state
    return p2n_make(state, addata_root=addata_root, record_profile=record_profile, progress=progress)


def _md_literal(text) -> str:
    """vendor の報告文・検算の理由・読み取りの注意を Markdown として描くときの逃がし（バグハント 3 回目 Q8）。
    「45,000(*)」が 2 つある行で間が斜体になり手入力の印が消える、「印字 $ / 生成 #*」の $ … $ が数式になる、
    PDF 由来の「![](//…)」が外の画像を読み込む、を防ぐ。見出し・箇条書き・表の形（# - |）はそのまま"""
    s = str(text if text is not None else '')
    s = s.replace('\\', '\\\\')
    for ch in ('*', '_', '$', '~', '`', '[', ']', '<', '>'):
        s = s.replace(ch, '\\' + ch)
    return s


# 入力そのものが読めない（ベタ打ちでも同じ理由で読めない）ときの文言。検算の不合格とは分けて出す（P13）
_P2N_INPUT_ERRORS = ('パスワード', 'ページあります', 'ページが多すぎ', '開けません')


def _items_sig(items) -> str:
    """明細の並びの指紋（プレビュー取り込みの明細と、あとで入れた別の CSV を見分ける。M3/M8）"""
    try:
        return hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()
    except Exception:  # noqa: BLE001
        return ''


def _sidebar_insurance_hint():
    """サイドバーの「事故・保険情報」を reading の insurance の形にする（キー名は生成器 estimate_schema.md の insurance）。
    証券番号 policy_no / 契約者 contractor / 事故日 accident_date / 受付番号 accept_no / 代理店 agency / アジャスター adjuster /
    入庫日 garage_in / 出庫日 garage_out / 修理日数 repair_days。見積書に印字が無い項目にだけ補われる。
    日付は _normalize_date8 で YYYYMMDD（読めない形は空 = 渡さない）。生成器が読まないキーは無視されるだけで害は無い"""
    _h = {
        'policy_no': str(st.session_state.get('policy_no', '') or '').strip(),
        'contractor': str(st.session_state.get('contractor_name', '') or '').strip(),
        'accident_date': _normalize_date8(st.session_state.get('accident_date', '')),
        'accept_no': str(st.session_state.get('accept_no', '') or '').strip(),
        'agency': str(st.session_state.get('agency_name', '') or '').strip(),
        'adjuster': str(st.session_state.get('adjuster_name', '') or '').strip(),
        'adjuster_post': str(st.session_state.get('adjuster_post', '') or '').strip(),   # 支店・所属（Insurance.AdjusterPost）
        'factory': str(st.session_state.get('factory_name', '') or '').strip(),   # 立会工場（Insurance.ConsultantFactory。画像鑑定は「写真鑑定」）
        'company': str(st.session_state.get('agency_name', '') or '').strip(),   # 「保険会社・代理店名」の欄（記録用）
        'garage_in': _normalize_date8(st.session_state.get('garage_in_date', '')),
        'garage_out': _normalize_date8(st.session_state.get('garage_out_date', '')),
        'repair_days': str(st.session_state.get('repair_days', '') or '').strip(),
    }
    return {k: v for k, v in _h.items() if v and v != '0'}


def _doc_upload_keys():
    """STEP 1-B の uploader のキー（車検証, 事故・保険の書類）。連番 upload_seq を付け、「新しい見積を作成する」で作り直して確実に空にする
    （Streamlit の uploader は値をプログラムから消せない。前の案件の車検証・書類が次の案件に付いたままにならないように。Codex 56）"""
    n = int(st.session_state.get('upload_seq', 0))
    return f'vehicle_upload_{n}', f'insurance_doc_upload_{n}'


def _sidebar_insurance_values():
    """旧経路（ベタ打ち・Step 4）に渡す insurance_info（サイドバーの値そのまま）"""
    return {k: st.session_state.get(k, 0 if k == 'repair_days' else '') for k in (
        'policy_no', 'contractor_name', 'accept_no', 'accident_date', 'agency_name',
        'adjuster_name', 'adjuster_post', 'factory_name', 'garage_in_date', 'garage_out_date', 'repair_days', 'note1')}


def _fresh_doc_fill(doc):
    """添付した事故・保険の書類の読み取りが、まだサイドバーに入っていない（この run で初めて読めた）ときに、
    空欄だけを埋めるための値（サイドバーへの反映は STEP 1-B の描画で次の run に行われるため。Codex 60）。
    反映済みなら {}（利用者が消した項目を書類から戻さない）"""
    if not doc:
        return {}
    oid = st.session_state.get('_insdoc_ocr_id')
    if oid and st.session_state.get('_insdoc_applied') == oid:
        return {}
    return _doc_hints.sidebar_insurance_from_doc(doc)


def _insurance_hint_now(doc):
    """スキル経路に渡す保険の hint: サイドバーの値 ＋（この run で初めて読めた書類なら）空の項目だけ書類から。
    読むときも取り置きの再開時も同じ作り方にする（照合の鍵がずれて取り置きを捨てないように。Codex 63）"""
    h = _sidebar_insurance_hint()
    if _fresh_doc_fill(doc):
        for k, v in _doc_hints.insurance_hint_from_doc(doc).items():
            h.setdefault(k, v)
    return h


def _doc_key(*hints):
    """添付の書類・サイドバーの保険から作った hint の同一性（車種フォルダ待ちの取り置きが、書類の差し替え後に再開されないよう照合する）"""
    return hashlib.sha256(json.dumps(list(hints), ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()


# 事故・保険の書類から埋めるサイドバーのキー（差し替え・取り外しのときに消す範囲。備考・入出庫日・修理日数は手入力のまま残す）
_DOC_INSURANCE_KEYS = ('accept_no', 'accident_date', 'policy_no', 'contractor_name', 'agency_name', 'adjuster_name', 'adjuster_post')


def _doc_fill_plan(att_fill, prev_filled, current) -> tuple:
    """書類から読めた値をサイドバーの欄に入れる計画 → (入れる {欄: 値}, 追跡する {欄: 値})。
    空欄か、前に書類から入れたままの項目にだけ入れる（利用者が手で入れた・直した値は書き換えない。Codex 62）。
    今回の読みに無い項目でも、前に書類から入れたままなら追跡を続ける（同じ書類を別モデル/キーで読み直したとき、
    追跡から外れた値が差し替え・取り外しで消えず次の案件の .neo に残るのを防ぐ。Codex 67）"""
    prev = prev_filled or {}
    cur_of = lambda k: str((current or {}).get(k, '') or '')  # noqa: E731
    updates, now = {}, {}
    for k, v in (att_fill or {}).items():
        cur = cur_of(k)
        if not cur.strip() or cur == str(prev.get(k) or ''):
            updates[k] = v
            now[k] = v
    for k, pv in prev.items():
        if k not in now and str(pv or '').strip() and cur_of(k) == str(pv or ''):
            now[k] = pv
    return updates, now


_CASE_INPUT_KEYS = (
    # サイドバーの事故・保険情報と費用
    'policy_no', 'contractor_name', 'accept_no', 'accident_date', 'agency_name', 'adjuster_name', 'adjuster_post', 'factory_name',
    'garage_in_date', 'garage_out_date', 'repair_days', 'note1', 'exp_towing', 'exp_rental', 'exp_exempt',
    # 添付の書類の読み取りの控えと反映の印
    '_doc_ocr_cache', '_insdoc_applied', '_insdoc_sha', '_insdoc_cleared_sha', '_insdoc_filled', '_insdoc_ocr_id', '_doc_ocr_error',
    # 生成結果・ベタ打ちの費用チェック
    '_beta_exp_file_key', 'pdf2neo_beta_use_exp', '_beta_use_exp_val', 'pdf2neo_beta_ack', 'pdf2neo_result', '_pdf2neo_filename', 'pdf2neo_vehicle_info',
)


_DOC_STATE_KEYS = ('_doc_ocr_cache', '_insdoc_applied', '_insdoc_sha', '_insdoc_cleared_sha', '_insdoc_filled', '_insdoc_ocr_id', '_doc_ocr_error')


def _reset_case_inputs(keep_docs: bool = False):
    """案件の入力を消す（見積書が別のファイルに変わった ＝ 別の案件）: サイドバーの事故・保険情報と費用、添付の書類の控え、
    車検証・書類の uploader（キーを進めて空にする）、生成結果、車種フォルダ待ちの取り置き。
    見積書そのもの・API キー・モデル・Addata の設定は残す。
    無いと、見積書だけ入れ替えた次の案件に前の案件の車検証・速報・受付番号が付いた NEO ができる（Codex hunt A2 2026-09-15）。
    keep_docs=True: 見積書を外した後に入れ替えた書類（＝次の案件の書類）と、その書類から入れた欄は残し、
    手で入れた保険欄・費用・結果だけ消す（前の案件の手入力を次の NEO に持ち越さない。Codex 72）"""
    _filled = dict(st.session_state.get('_insdoc_filled') or {}) if keep_docs else {}
    for key in _CASE_INPUT_KEYS:
        if keep_docs and key in _DOC_STATE_KEYS:
            continue
        if keep_docs and key in _filled and str(st.session_state.get(key, '') or '') == str(_filled.get(key) or ''):
            continue   # 今の書類から入れたまま ＝ 残す
        st.session_state.pop(key, None)
    _pend = st.session_state.pop('_bridge_pending', None)
    if _pend:
        try:
            from neo_skill import maker as _nsk_maker
            _nsk_maker.remove_case_dir(_pend.get('case_dir'))
        except Exception:  # noqa: BLE001
            pass
    st.session_state['_bridge_want'] = ''
    st.session_state['form_seq'] = int(st.session_state.get('form_seq', 0)) + 1      # サイドバーの入力欄を作り直す（残す値は value= で戻る）
    if not keep_docs:
        st.session_state['upload_seq'] = int(st.session_state.get('upload_seq', 0)) + 1  # 車検証・書類の uploader を作り直して空に


def _docs_ocr_state(s, api_key, model_name, keys) -> str:
    """添付の書類の OCR の状態（控え _doc_ocr_cache の中身のハッシュ。無い／失敗は ''）。API は呼ばない。
    同じファイルでも、キーを直して読めるようになれば状態が変わる ＝ 前の（書類なしで作った）結果を落とさせない（Codex 72）"""
    cache = s.get('_doc_ocr_cache') or {}
    out = []
    for kind, key in (('shaken', keys[0]), ('insdoc', keys[1])):
        f = s.get(key)
        if f is None:
            out.append('')
            continue
        try:
            h = hashlib.sha256(f.getvalue()).hexdigest()
        except Exception:  # noqa: BLE001
            out.append('?')
            continue
        hit = cache.get((kind, h, str(model_name or ''), hashlib.sha256((api_key or '').encode('utf-8')).hexdigest()[:12]))
        if isinstance(hit, dict) and not hit.get('_error'):
            out.append(hashlib.sha256(repr(sorted((str(k), str(v)) for k, v in hit.items())).encode('utf-8')).hexdigest())
        else:
            out.append('')
    return '|'.join(out)


def _p2n_inputs_signature(file_key, state=None, api_key='', model_name='', beta=False, addata_id='') -> str:
    """生成結果に添える「入力の指紋」: 見積書・添付の書類（内容ハッシュ）・事故/保険欄・費用とそのチェック・税区分・テンプレート。
    画面に出すとき今の指紋と違えば、その結果は前の入力で作ったもの ＝ ダウンロードさせない（別の見積の NEO や、直す前の
    保険欄で作った NEO を落とせてしまう。Codex hunt A1 2026-09-15）"""
    s = st.session_state if state is None else state
    parts = [str(file_key or '')]
    _keys = _doc_upload_keys() if state is None else ('vehicle_upload', 'insurance_doc_upload')
    parts.append(_docs_ocr_state(s, api_key, model_name, _keys))   # 読めたか・何が読めたか（Codex 72）
    parts.append(str(addata_id or ''))   # スキル経路が使った Addata（場所＋データ版。ベタ打ちは ''。Codex hunt F1）
    for _k in _keys:
        _f = s.get(_k)
        try:
            parts.append(hashlib.sha256(_f.getvalue()).hexdigest() if _f is not None else '')
        except Exception:  # noqa: BLE001
            parts.append('?')
    def _sv(k):
        v = str(s.get(k, 0 if k == 'repair_days' else '') or '')
        if k in ('accident_date', 'garage_in_date', 'garage_out_date'):
            v = _normalize_date8(v) or v   # 生成は日付を YYYYMMDD に正規化して書く。'2026/09/01' と '20260901' は同じ入力（Codex 76）
        return v
    parts.append(repr([(k, _sv(k)) for k in (
        'policy_no', 'contractor_name', 'accept_no', 'accident_date', 'agency_name',
        'adjuster_name', 'adjuster_post', 'factory_name', 'garage_in_date', 'garage_out_date', 'repair_days', 'note1')]))
    # 費用の額はベタ打ちでチェックが入っているときだけ NEO に入る ＝ それ以外（スキル経路・チェック無し）は額を変えても結果は同じ（Codex 75/77）
    # ウィジェットの値があればそれ（その run の最新。結果の下でチェックを切り替えた run でも陳腐化を見逃さない）、
    # 描かれていない run では控え（_beta_use_exp_val。消えたウィジェットの値で結果を陳腐化させない。H1、レビュー 2026-09-15）
    _use_exp = bool(beta) and bool(s['pdf2neo_beta_use_exp'] if 'pdf2neo_beta_use_exp' in s else s.get('_beta_use_exp_val'))
    _exp_any = any(safe_int(s.get(k, 0)) for k in ('exp_towing', 'exp_rental', 'exp_exempt'))
    _use_exp = _use_exp and _exp_any   # 費用が全部 0 ならチェックの有無で結果は変わらない（チェックボックスも描かれない。レビュー 2026-09-15）
    parts.append(repr((_use_exp, safe_int(s.get('exp_towing', 0)) if _use_exp else 0,
                       safe_int(s.get('exp_rental', 0)) if _use_exp else 0, safe_int(s.get('exp_exempt', 0)) if _use_exp else 0)))
    # 税区分の選択とテンプレート NEO はベタ打ちだけが使う（スキル経路は見積書の合計欄から判定し、テンプレートも使わない。Codex 78）
    parts.append(str(s.get('pdf_tax_radio', '')) if beta else '')
    # テンプレートは受け付けた（検証済みの）custom_neo_bytes で取る。uploader は照合より後に描かれるので、受け付けが変わった run は
    # uploader の側で描き直す（_tpl_sig_seen。Codex 70/73: 弾いたファイルの中身で照合しない）
    _tb = (s.get('custom_neo_bytes') or b'') if beta else b''
    parts.append(hashlib.sha256(_tb).hexdigest() if _tb else '')
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()


def _docs_sig() -> str:
    """添付の書類（車検証・事故/保険の書類）の同一性（内容ハッシュの組）。見積書を変えたときに、添付が前の見積書のときと
    同じなら前の案件の書類 ＝ 消す、変わっていれば新しい案件のために入れ替えたもの ＝ 残す（Codex 71）"""
    parts = []
    for _k in _doc_upload_keys():
        _f = st.session_state.get(_k)
        try:
            parts.append(hashlib.sha256(_f.getvalue()).hexdigest() if _f is not None else '')
        except Exception:  # noqa: BLE001
            parts.append('?')
    return '|'.join(parts)


def _attached_docs_caption(api_key, model_name) -> str:
    """生成ボタンの上の案内。添付したファイル名ではなく「実際に読めたか」で文を変える（読めていない書類は NEO に入らない。
    Codex hunt A3 2026-09-15）。読み取りは控え（_doc_ocr_cache）から返るので、ここで API を余分に呼ぶことはない"""
    _kv, _kd = _doc_upload_keys()
    fv, fd = st.session_state.get(_kv), st.session_state.get(_kd)
    if fv is None and fd is None:
        return ("📎 車検証や事故・保険の書類（速報報告書など）があれば、上の「車検証」「事故・保険の書類」に入れてから生成すると、"
                "車両・顧客・保険の情報が NEO に入ります")
    if not api_key:
        return "📎 添付の書類はまだ読めていません（Gemini API キーが無いため）。このまま生成すると書類の値は NEO に入りません"
    vd, doc = _attached_docs_ocr(api_key, model_name)
    parts = []
    if fv is not None:
        parts.append("車検証: " + ("読み取り済み → 車両・顧客の情報に使います" if vd else "読めていません → 使われません"))
    if fd is not None:
        parts.append("事故・保険の書類: " + ("読み取り済み → サイドバーの事故・保険情報と車両の情報に使います" if doc
                                     else "読めていません → 使われません"))
    return "📎 " + " ／ ".join(parts)


_ROW_FIELDS = ('code', 'name', 'method', 'parts_no', 'index', 'qty', 'price', 'wage', 'flags', 'comment')   # 短縮記法の列順（reading_schema）


def _row_view(row) -> dict:
    """reading の明細行を dict の形で見る。merge 後の rows は短縮記法の文字列（code|name|method|parts_no|index|qty|price|wage|flags|comment）
    のことが多い（vendor reading_pages）。dict はそのまま。注記行（flags N / note だけ）は name 無しにする"""
    def _flags(v):
        return unicodedata.normalize('NFKC', str(v or '')).upper()   # vendor と同じ（全角・小文字も見る）
    if isinstance(row, dict):
        d = dict(row)
        fl = _flags(d.get('flags'))
        if 'N' in fl or 'R' in fl or d.get('reserve') or (not d.get('name') and d.get('note')):
            d['name'] = ''   # 注記行・保留行は対象外
        if 'M' in fl:
            d['manual'] = True
        return d
    if isinstance(row, str):
        f = row.split('|')
        f += [''] * (len(_ROW_FIELDS) - len(f))
        d = dict(zip(_ROW_FIELDS, f[:len(_ROW_FIELDS)]))
        fl = _flags(d.get('flags'))
        if 'N' in fl or 'R' in fl:
            d['name'] = ''   # 注記行・保留行は対象外
        d['manual'] = 'M' in fl
        return d
    return {}


_NEO_COMMENT_RE = re.compile(r'^\s*(?:NEO|ＮＥＯ)\s*[:：]\s*(.*)$', re.S | re.I)   # 印字の明細コメント（生成器は行頭の NEO: を見て NEO に書く。vendor と同じく大文字小文字を問わない）


def _row_with_wage_zero(row, note: str):
    """明細行に wage: 0 と要確認コメントを入れて返す（dict / 短縮記法の文字列どちらも）。
    既存のコメントが `NEO:`（印字の明細コメント ＝ NEO に書く）なら、その前に注記を付けると生成器の行頭一致が外れて
    NEO から消えるので、dict 行にして neo_comment（印字）と comment（注記）に分ける（レビュー 2026-09-15）"""
    if isinstance(row, dict):
        d = dict(row)
    else:
        f = str(row).split('|')
        f += [''] * (len(_ROW_FIELDS) - len(f))
        d = {k: v for k, v in zip(_ROW_FIELDS, f[:len(_ROW_FIELDS)]) if v != ''}
        d.setdefault('name', f[1])
    d['wage'] = 0
    cur = str(d.get('comment') or '')
    m = _NEO_COMMENT_RE.match(cur)
    if m and not d.get('neo_comment'):
        d['neo_comment'] = m.group(1).strip()
        d['comment'] = note
    else:
        d['comment'] = note + ((' / ' + cur) if cur else '')
    return d


def _blank_wage_rows(reading) -> list:
    """工賃欄がある書式で、工賃も指数も無い明細行（注記行・保留行・手入力行を除く）を [(block, row, name)] で返す。
    生成器はこの行に標準指数を補うが、実案件（精算見積・工場見積・コグニ印刷）では空欄 = 0 円のことが多く、
    見積書合計と合わずに不合格になる（2026-09-15 実機テスト: タンク +858・ハイエース +6,160・カローラ +15,379・シエンタ +117,040）"""
    rows = []
    for bi, blk in enumerate((reading or {}).get('blocks') or []):
        for ri, row in enumerate((blk or {}).get('rows') or []):
            d = _row_view(row)
            if str(d.get('name') or '').strip():
                rows.append((bi, ri, d))
    def _has(v):
        # 数字を含むときだけ「値あり」。'-'・'**'（印字の印だけ）・空白は空欄扱い（vendor も '-' を空欄とみなす。タンク・カローラで発覚）
        return v not in (None, '') and bool(re.search(r'\d', str(v)))
    if not any(_has(r.get('wage')) or _has(r.get('index')) for _, _, r in rows):
        return []   # 工賃欄自体が無い書式（標準に任せる。判断規則）
    def _std_fill(r):
        # 生成器が標準指数を補う行だけ（部品代のある取替行は生成器が元々 0 円にする ＝ 対象外。要確認の水増しを避ける）
        m = unicodedata.normalize('NFKC', str(r.get('method') or '')).strip()
        return not (_has(r.get('price')) and m in ('', '取替', '交換', '部品'))
    return [(bi, ri, str(r.get('name') or '')) for bi, ri, r in rows
            if not _has(r.get('wage')) and not _has(r.get('index')) and not r.get('manual') and _std_fill(r)]


def _beta_generate_ui(_p2n_file, _p2n_bytes, _p2n_file_key, api_key, selected_model, _pdf_tax_sel, fallback=False):
    """ベタ打ち（旧経路 run_pdf_to_neo_pipeline）の生成ボタンとその処理。Addata が決まらないとき（本来の置き場）と、
    スキル経路が不合格・車種未収録で NEO が出なかったときの逃げ道（fallback=True。2026-09-15 実機テスト: ボルボ V40 は
    車種マスタに無く汎用車種の生成で例外、精算見積 4 本は検算差で不合格 → 以前は行き止まりだった）の 2 か所から呼ぶ"""
    _p2n_beta_exp = {'towing': safe_int(st.session_state.get('exp_towing', 0)),
                     'rental_car': safe_int(st.session_state.get('exp_rental', 0)),
                     'tax_exempt': safe_int(st.session_state.get('exp_exempt', 0))}
    st.caption(_attached_docs_caption(api_key, selected_model))
    _p2n_beta_use_exp = False
    if st.session_state.get('_beta_exp_file_key') != _p2n_file_key:
        # 見積が変わったらチェックは外す（前の見積で入れた同意を次の見積に持ち越さない。Codex 52）
        st.session_state['pdf2neo_beta_use_exp'] = False
        st.session_state['_beta_use_exp_val'] = False
        st.session_state['_beta_exp_file_key'] = _p2n_file_key
    if any(_p2n_beta_exp.values()):
        # 前の案件の入力が残っていても黙って足さない（合計が原本と食い違う）。チェックしたときだけ入れる（Codex 51）
        _p2n_beta_use_exp = st.checkbox(
            f"サイドバーの費用を NEO に入れる（レッカー ¥{_p2n_beta_exp['towing']:,}・代車 ¥{_p2n_beta_exp['rental_car']:,}・"
            f"非課税 ¥{_p2n_beta_exp['tax_exempt']:,}）。見積書に印字の無い費用なので、入れると原本の合計とは一致しません",
            value=False, key='pdf2neo_beta_use_exp')
    # チェックの値はウィジェットとは別のキーに控える（このブロックを描かない run ではウィジェットの値が消え、指紋が食い違って
    # 結果が陳腐化・行き止まりになる。バグハント H1）
    st.session_state['_beta_use_exp_val'] = bool(_p2n_beta_use_exp)
    if not api_key:
        st.caption("ベタ打ちの読み取りは Gemini を使います。サイドバーの「APIキー設定」に Gemini API キーを入れてください。")
        return
    _label = ("✏️ ベタ打ちで作る（部品コード・標準指数なし。明細・金額は見積書のとおり、合計に合わせる金額調整の行が入ることがあります）"
              if fallback else "✏️ ベタ打ちで生成（Addata なし・部品コード/標準指数は入りません）")
    if st.button(_label, key='pdf2neo_run_beta', width='stretch'):
        st.session_state.pop('pdf2neo_result', None)
        st.session_state.pop('_pdf2neo_filename', None)
        st.session_state.pop('pdf2neo_beta_ack', None)   # 前の結果の「差異を確認した」チェックを次の結果に持ち越さない
        _p2n_beta_tax = ('内税' in str(_pdf_tax_sel) or '税込' in str(_pdf_tax_sel))
        _p2n_beta_vd, _p2n_beta_doc = _attached_docs_ocr(api_key, selected_model)
        _p2n_beta_vi = _doc_hints.vehicle_info_for_legacy(_p2n_beta_vd, _p2n_beta_doc)
        _p2n_beta_ins = _sidebar_insurance_values()
        for _k, _v in _fresh_doc_fill(_p2n_beta_doc).items():
            if not str(_p2n_beta_ins.get(_k) or '').strip():
                _p2n_beta_ins[_k] = _v
        with st.spinner("見積書を読んでベタ打ちの NEO を作っています…（1〜3 分）"):
            _p2n_beta = run_pdf_to_neo_pipeline(
                _p2n_bytes, api_key,
                # 添付の車検証・書類の車両/顧客情報。無ければ None（旧経路は見積書を車検証として読もうとして空になる）
                vehicle_info=(_p2n_beta_vi or {}),
                mime_type=get_mime_type(_p2n_file.name),
                model_name=selected_model,
                template_bytes=st.session_state.get('custom_neo_bytes'),
                is_tax_inclusive=_p2n_beta_tax,
                # 費用はチェックしたときだけ（上）。事故・保険欄は旧経路と同じ扱い（b15bd06 で外す前の呼び方）
                expenses=(_p2n_beta_exp if _p2n_beta_use_exp else None),
                insurance_info=_p2n_beta_ins,
                force_beta=True,
            )
        if not isinstance(_p2n_beta, dict):
            _p2n_beta = {'ok': False, 'error': 'ベタ打ち生成が想定外の値を返しました'}
        _p2n_beta['legacy_beta'] = True
        _p2n_beta['fallback_from_skill'] = bool(fallback)
        _p2n_beta['inputs_sig'] = _p2n_inputs_signature(_p2n_file_key, api_key=api_key, model_name=selected_model, beta=True)
        st.session_state['pdf2neo_tax_inclusive'] = _p2n_beta_tax
        st.session_state['pdf2neo_result'] = _p2n_beta
        st.rerun()


def _p2n_addata_identity(root) -> str:
    """生成に使う Addata の同一性（場所＋データ版）。指紋に入れて、Addata を切り替え・外した後に前の結果を落とさせない（Codex hunt F1）"""
    if not root:
        return ''
    try:
        from neo_skill import bridge as _brg
        ver = _brg.version(str(root)) or ''
    except Exception:  # noqa: BLE001
        ver = ''
    return f"{os.path.normpath(str(root))}|{ver}"


def _nsk_code_stamp() -> str:
    """いま読み込まれている neo_skill/reader.py の中身の印（8 文字）。本番で古いモジュールが残っていないかを画面で確かめる
    （sync_app_modules が差し替えた後は手元の `python -c` の値と一致する）"""
    try:   # reader.py だけでなく neo_skill の全モジュールの中身から作る（どれを直しても本番で版が変わったと分かる。2026-09-15）
        from neo_skill import reader as _r
        _d = os.path.dirname(_r.__file__)
        _h = hashlib.sha256()
        for _n in sorted(x for x in os.listdir(_d) if x.endswith('.py')):
            _h.update(_n.encode('utf-8') + b'\0' + _file_digest(os.path.join(_d, _n)).encode('ascii'))
        return _h.hexdigest()[:8]
    except Exception:  # noqa: BLE001
        return '?'


def _attached_docs_ocr(api_key, model_name=None, progress=None):
    """STEP 1-B に添付した車検証（vehicle_upload）と事故・保険の書類（insurance_doc_upload）を Gemini で読み、
    (車検証の dict, 書類の dict) を返す。読めなかったものは {}。同じファイルは内容ハッシュでセッションに控えて二度読まない。
    書類の内容ハッシュは session_state['_insdoc_sha'] に置く（サイドバーへの一度きりの反映に使う）"""
    out = []
    cache = st.session_state.setdefault('_doc_ocr_cache', {})
    _k_veh, _k_doc = _doc_upload_keys()
    for kind, key, fn, label in (('shaken', _k_veh, analyze_vehicle_registration, '車検証'),
                                 ('insdoc', _k_doc, analyze_insurance_document, '事故・保険の書類')):
        f = st.session_state.get(key)
        data = {}
        if f is not None and api_key:
            try:
                b = f.getvalue()
            except Exception:  # noqa: BLE001
                b = None
            if b:
                h = hashlib.sha256(b).hexdigest()
                if kind == 'insdoc':
                    st.session_state['_insdoc_sha'] = h
                # 控えはモデルと API キーごと（モデルを切り替えた・キーを直したら読み直す。Codex 59）
                _ck = (kind, h, str(model_name or ''), hashlib.sha256((api_key or '').encode('utf-8')).hexdigest()[:12])
                if kind == 'insdoc':
                    st.session_state['_insdoc_ocr_id'] = '|'.join(_ck[1:])   # 同じファイルでもモデル・キーが変われば別の読み取り（Codex 61）
                _hit = cache.get(_ck)
                if isinstance(_hit, dict) and (not _hit.get('_error') or time.time() - float(_hit.get('_t') or 0) < 120):
                    data = _hit   # 成功はずっと、失敗は 2 分だけ控える（rerun のたびに同じファイルを送らない。Codex 57）
                else:
                    if progress:
                        progress(f'{label}を読んでいます（{f.name}）')
                    try:
                        data = fn(api_key, b, get_mime_type(f.name), model_name) or {}
                    except Exception as e:  # noqa: BLE001
                        data = {'_error': f'{type(e).__name__}: {e}'}
                    if kind == 'shaken' and isinstance(data, dict) and not data.get('_error') and not _shaken_has_data(data):
                        # confidence だけの返事を「読めた」として控えない（Codex 66。書類側は analyze_insurance_document が同じ判定を持つ）
                        data = {'_error': '車検証のページを判別できませんでした'}
                    if isinstance(data, dict) and not data.get('_error'):
                        cache[_ck] = data
                    elif isinstance(data, dict) and data.get('_error'):
                        cache[_ck] = dict(data, _t=time.time())
                        st.session_state['_doc_ocr_error'] = f"{label}: {data['_error']}"
        out.append(data if isinstance(data, dict) and not data.get('_error') else {})
    return out[0], out[1]


def run_pdf_to_neo_pipeline(pdf_bytes, api_key, model_name=None, template_bytes=None,
                           is_tax_inclusive=False, expenses=None,
                           mime_type='application/pdf', insurance_info=None, vehicle_info=None, force_beta=False):
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
            _pipe = _load_pipeline()
        except Exception as e:
            return {'ok': False, 'error': f'PDF→NEO変換モジュールを読み込めません: {e}'}

        # 画像で来ることもあるので、拡張子は mime に合わせる。
        # 常に .pdf にすると、パイプライン側が PDF として開こうとして失敗する。
        _suffix = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
                   'image/bmp': '.bmp', 'image/tiff': '.tif',
                   'image/heic': '.heic', 'image/heif': '.heif'}.get(
            str(mime_type or ''), '.pdf')
        with tempfile.NamedTemporaryFile(suffix=_suffix, delete=False) as _f:
            _f.write(pdf_bytes)
            tmp_pdf = _f.name

        if template_bytes:
            with tempfile.NamedTemporaryFile(suffix='.neo', delete=False) as _f:
                _f.write(template_bytes)
                tmp_tpl = _f.name
            template_path = tmp_tpl
        else:
            template_path = TEMPLATE_PATH

        # ベタ打ち（force_beta）は Addata を渡さずモード A に固定する。Addata があると旧経路がモード B/C で品番・部品コードを
        # 書き換え（品名に ※）、「部品コード無し」の表示と食い違う NEO を検証 OK で落とせた（バグハント K1/H5/J1）
        addata_root = '' if force_beta else find_addata_dir()
        mode_override = 'A' if (force_beta or not addata_root) else None

        # APIキーは引数で直接渡す。os.environ に書くと、プロセスを共有する
        # 他の利用者のセッションからも読めてしまう（キーの流用・課金事故）。
        result = _call_pipeline(
            _pipe, tmp_pdf,
            # 見積書は写真（JPG/PNG/HEIC 等）で入れられることもある。
            # PDF 固定で渡すと、画像を PDF として解析しようとして読み取れない。
            source_mime=mime_type,
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
            # サイドバーの事故・保険欄。渡していなかったため、この経路で
            # 作った .neo には事故受付番号・証券番号・契約者名・保険会社・
            # アジャスター名が1つも入らなかった（2026-09-11）。
            # プレビュー経由では入っていたので、同じ見積でも入口によって
            # 中身が違う .neo が出ていた。
            insurance_info=insurance_info or None,
            # 添付の車検証・書類から読んだ車両/顧客情報（無ければ None → パイプラインは見積書を車検証として読もうとして空になる）
            # ベタ打ちは添付が無ければ {}（None だと旧経路が見積書を車検証として読み、余分な API 呼び出しと「車検証OCR失敗」が出る。K5）
            vehicle_info=(vehicle_info if force_beta else (vehicle_info or None)),
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
    if APP_PASSCODE and not st.session_state.get('_passcode_ok'):
        st.markdown("### 🔒 合言葉")
        _pc = st.text_input("このアプリの合言葉を入力してください", type="password", key="app_passcode_input")
        if st.button("入る", key="app_passcode_btn"):
            if hmac.compare_digest(str(_pc or '').strip().encode('utf-8'), APP_PASSCODE.strip().encode('utf-8')):   # str だと ASCII 以外で TypeError
                st.session_state['_passcode_ok'] = True
                st.rerun()
            st.error("合言葉が違います")
        st.stop()
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
    @st.cache_data(show_spinner=False, max_entries=4)
    def _load_template(path: str, mtime: float = 0.0, size: int = 0) -> bytes:
        # 更新時刻と大きさも鍵に入れる（push で雛形が変わっても、再起動まで古い雛形のままだった。N10）
        with open(path, 'rb') as f:
            return f.read()

    if not os.path.exists(TEMPLATE_PATH):
        st.error(
            f"⚠️ テンプレートファイルが見つかりません: {TEMPLATE_FILENAME}\n\n"
            f"app.py と同じフォルダに「{TEMPLATE_FILENAME}」を配置してください。"
        )
        st.stop()
    _tpl_st = os.stat(TEMPLATE_PATH)
    template_data = _load_template(TEMPLATE_PATH, _tpl_st.st_mtime, _tpl_st.st_size)

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
        # 見積書 PDF → NEO（pdf-to-neo スキル経路）は Claude か Gemini で読む（両方あれば画面で選ぶ）。
        # Gemini は CSV 取り込み・車検証 OCR・旧経路でも使う。
        if ANTHROPIC_API_KEY:
            claude_api_key = ANTHROPIC_API_KEY
            st.success("Claude APIキー: 設定済み (.env)")
        else:
            claude_api_key = (st.text_input(
                "Claude APIキー（見積書の読み取り）",
                type="password",
                key='claude_api_key_input',
                help=".envファイルの ANTHROPIC_API_KEY にキーを設定すれば毎回入力不要"
            ) or '').strip()   # 空白だけの入力を「キーあり」にしない
        if GEMINI_API_KEY:
            api_key = GEMINI_API_KEY
            st.success("Gemini APIキー: 設定済み (.env)")
        else:
            api_key = (st.text_input(
                "Gemini APIキー（見積書の読み取り・CSV取り込み・車検証OCR）",
                type="password",
                help=".envファイルの GEMINI_API_KEY にキーを設定すれば毎回入力不要"
            ) or '').strip()
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
        # ── PC の Addata をブラウザ経由で使う（クラウド向け。neo_skill.bridge / addata_bridge/index.html）──
        # サーバは利用者の PC を読めないので、ブラウザ側で PC のフォルダを選んでもらい、
        # 車種マスタ（COM）と見積の車種フォルダだけをこの画面のアプリに送ってもらう
        with st.expander("🖥️ PC の Addata をこの画面から使う（クラウド向け・おすすめ）",
                         expanded=bool(st.session_state.get('_bridge_path') or not find_addata_dir())):
            st.caption("PC の Addata フォルダ（C:\\Addata）を一度選ぶと、車種マスタ（約 9MB）と見積の車種フォルダ（数 MB）だけを"
                       "この画面に送って照合に使います。5GB を上げる必要はありません。Chrome / Edge で使えます。")
            try:
                from neo_skill import bridge as _br
                _bpath = _br.root(st.session_state)
                _br.touch(_bpath)   # 使用中の印を先に付けてから古いものを掃除する（自分のフォルダを消さない。Codex 42）
                _br.sweep()
                from neo_skill import maker as _nsk_maker_sw
                _nsk_maker_sw.sweep_case_dirs()   # 車種フォルダ待ちのまま放置された作業フォルダ（reading.json 入り）も消す
                # 同じ PC・同じブラウザに覚えさせる（読み直しても送り直さずに済む）。解除したら覚えも消させる
                _forget = bool(st.session_state.pop('_bridge_forget', False))
                # 覚えさせるのは、部品から hello（覚えていた内容）を受け取ったあとだけ。先に渡すと、まだ空の設定で
                # ブラウザの覚えを消してしまう（レビュー）
                _keep = ({'addata_dir': addata_setting(_QS_ADDATA_DIR), 'addata_url': addata_setting(_QS_ADDATA_URL)}
                         if st.session_state.get('_bridge_hello_done') else {})
                _bval = _br.render(want=st.session_state.get('_bridge_want', ''), have=_br.cars(_bpath),
                                   com=_br.has_com(_bpath), key='addata_bridge',
                                   bid=st.session_state.get('_bridge_id', ''), forget=_forget, keep=_keep)
                _bmsg = _br.ingest(st.session_state, _bval)
                # COM を受け取った直後は、部品はまだ com=false の render しか見ていない（render は ingest より先）。
                # もう一度 rerun して com=true を届ける: 車種フォルダ待ちがあれば部品はその後それを送る（Codex 50）
                # 引き継ぎ（hello）で送り先が変わったら、それを先に読み直してから描き直しの判断をする
                _bpath = st.session_state.get('_bridge_path') or _bpath
                _bridge_rerun = bool(_bmsg and isinstance(_bval, dict) and _bval.get('phase') in ('com', 'hello') and _br.has_com(_bpath)
                                     # 同じ run で「生成」ボタンが押されていたら rerun しない（rerun するとその押下が消える。ボタンの run は最後に rerun する）
                                     and not st.session_state.get('pdf2neo_run') and not st.session_state.get('pdf2neo_run_beta'))
                if _br.has_com(_bpath):
                    st.session_state['_bridge_path'] = _bpath
                    # いつ送ったものかを出す（覚えていた接続をそのまま使うと、PC で Addata を入れ替えても古い版のままになる）
                    _sent = _br.sent_at(_bpath)
                    _sent_txt = ''
                    if _sent:
                        # 本番（クラウド）のコンテナは日本時間ではないので JST で出す（そのままだと 9 時間ずれ、
                        # 「いつの Addata か」の判断を誤らせる。レビュー）
                        _ago = max(0.0, time.time() - _sent)
                        _ago_txt = ('{:.0f} 日前'.format(_ago / 86400) if _ago >= 86400 else '{:.0f} 時間前'.format(_ago / 3600))
                        _sent_txt = (' ／ 送った日時 ' + datetime.datetime.fromtimestamp(_sent, JST).strftime('%m/%d %H:%M')
                                     + ('（{}。PC の Addata を入れ替えたときは「🔄 送り直す」）'.format(_ago_txt) if _ago >= 3600 else ''))
                    st.success(f"PC の Addata（{st.session_state.get('_bridge_root_name') or 'フォルダ'}）を使用中 ／ "
                               f"データ版 {_br.version(_bpath) or '不明'} ／ 取り込んだ車種: "
                               f"{', '.join(_br.cars(_bpath)) or 'なし（見積を入れると自動で送ります）'}" + _sent_txt)
                    if st.button("🔄 いまの PC の Addata を送り直す", key='bridge_resend',
                                 help="PC で Addata を入れ替えたときに押します。いま送ってある車種マスタを消し、選んである PC のフォルダから送り直します"):
                        _br.resend(st.session_state)
                        _defer_sidebar_rerun()
                    if st.button("🔌 PC の Addata との接続を解除", key='bridge_disconnect',
                                 help="この画面に送った車種マスタ・車種フォルダを消し、他の設定（ZIP・パス・取得URL・自動検出）に戻します"):
                        _br.disconnect(st.session_state)
                        _defer_sidebar_rerun()
                if _bmsg:
                    st.caption(_bmsg)
                if st.session_state.get('_bridge_want'):
                    if _br.has_com(_bpath):
                        st.info(f"車種 {st.session_state['_bridge_want']} のフォルダを PC から送っています…（届くと自動で続きます）")
                    else:
                        st.warning(f"車種 {st.session_state['_bridge_want']} のフォルダ待ちです。上のボタンで PC の Addata フォルダ（COM があるもの）を"
                                   "選び直すと、読み取り結果はそのまま続きから生成します")
            except Exception as _be:  # noqa: BLE001
                st.caption(f"PC の Addata 連携を表示できません: {_be}")
                _bridge_rerun = False
        if _bridge_rerun:
            _defer_sidebar_rerun()
        addata_status = find_addata_dir()
        if addata_status:
            _ka06 = find_ka06_path(addata_status)
            st.success("Addata検出済み")
            st.caption(addata_status)
            # データ版（COM/AnVer.DB の Number）。版が違うと標準品番・標準指数が
            # 変わるため、どの版で照合したかを見えるようにしておく。
            # 社内で版が揃っているかの確認にも使う。
            try:
                from addata_locator import addata_version as _ad_ver
                _ver = _ad_ver(addata_status)
            except Exception:
                _ver = ''
            st.caption(f"データ版: {_ver}" if _ver
                       else "データ版: 不明（COM/AnVer.DB が読めません）")
            st.caption(("車種マスタ KA06_ALL.DB あり" if _ka06
                        else "※ COM/KA06_ALL.DB が無いため車種の自動特定はできません"))
            # この PC に、いま使っているものより**新しい版**の Addata が
            # 置いてあることがある（古い C:\Addata を残したまま新しい版を
            # 別の場所に入れた PC）。版が違うと標準品番・標準指数が変わり、
            # 協定見積に載る部品コードや指数が実機と食い違う。
            # pdf-to-neo スキルの env_check.py と同じ確認を画面でも出す。
            try:
                from addata_locator import newer_addata_candidates as _newer
                from addata_locator import rank_incomplete as _rankbad
                from addata_locator import search_skipped as _skipbad
                _nw = _newer(addata_status, budget=8.0)
                _rb = _rankbad()
                _sk0 = _skipbad()
            except Exception:
                _nw, _rb, _sk0 = [], [], []
            if _sk0:
                # 見つかったからといって、それが一番新しいとは限らない。
                # 時間切れで見ていない候補があると、古い Addata を
                # 使ったまま「検出済み」と表示されることになる。
                st.warning(
                    f"⚠️ 探索の制限時間内に見きれなかった候補が{len(_sk0)}件"
                    "あります。ここに表示している Addata より新しい版が"
                    "見落とされている可能性があります。"
                    "使いたい Addata は設定で指定してください。")
            if _rb:
                # データ版を読めなかった候補があると、順位を付けられず
                # 「並び順で選ぶ」ことになる。古い Addata を掴んでいても
                # 気づけないので、そのことを伝える。
                st.warning(
                    "⚠️ データ版を読めなかった Addata の候補が"
                    f"{len(_rb)} 件あります。いちばん新しい版を選べて"
                    "いない可能性があります（応答しない共有や未同期の"
                    "OneDrive が原因のことが多い）。使いたい Addata を"
                    "設定で指定してください。")
            if _nw:
                st.warning(
                    "⚠️ この PC には、もっと新しい版の Addata があります"
                    + "（" + "／".join(f"{_p}（{_v}）" for _p, _v in _nw[:2]) + "）。"
                    "版が違うと標準品番・標準指数が変わるため、"
                    "協定見積の部品コードや指数が実機と食い違います。"
                    "使いたい方を指定するか、古い方を消してください。")
            if st.button("🗑️ Addataを解除", key='addata_clear_btn'):
                _discard_uploaded_addata()
                # ZIP の uploader を別のウィジェットにして空にする（以前は st.rerun() が描く前の uploader の値を捨てていたので解除できていた。
                # rerun を本文の後に遅らせたので、同じ run で残った ZIP を展開し直さないように。レビュー 2026-09-15）
                st.session_state['_addata_zip_nonce'] = int(st.session_state.get('_addata_zip_nonce', 0) or 0) + 1
                _defer_sidebar_rerun()
        else:
            st.warning("Addata 未検出 — 見積 PDF は「ベタ打ちで生成」（部品コード・標準指数なし）になります。"
                       "部品コードまで入れるなら、上の「🖥️ PC の Addata をこの画面から使う」で PC の C:\\Addata を選んでください")
            # 「無いと分かった」のか「時間切れで探しきれていない」のかは
            # 別の話。応答しない共有や未同期の OneDrive があると、Addata が
            # 手元にあるのにベタ打ちモードに落ちたまま気づけない。
            try:
                from addata_locator import search_skipped as _skipped
                _sk = _skipped()
            except Exception:
                _sk = []
            if _sk:
                st.info(
                    f"※ 探索の制限時間内に見きれなかった候補が{len(_sk)}件"
                    "あります（応答しない共有や未同期の OneDrive が"
                    "原因のことが多い）。Addata が手元にある場合は、"
                    "設定で場所を直接指定してください。")
            # 取得URLが設定されていて失敗している場合は理由をここにも出す。
            # 設定画面をたたまれていると気づけないため。
            _url_err = st.session_state.get('_addata_url_error')
            if _url_err and addata_setting(_QS_ADDATA_URL):
                st.error('取得URL: %s' % _url_err)

        with st.expander("⚙️ Addata の場所を設定する", expanded=not addata_status):
            st.caption(
                "Addata があると、部品名・品番・価格をコグニセブンのマスタと"
                "突き合わせて部品コードを引き当てます。無い場合は"
                "ベタ打ち（モードA）で生成します。金額・明細・品名は"
                "どちらでも見積書のとおりです。"
            )
            st.info(
                "**このアプリはクラウド（Linuxサーバ）で動いています。**\n\n"
                "そのため、お使いのPCの `C:\\Addata` をサーバから読むことはできません。"
                "パスを入れて効くのは、**このアプリをそのPCで直接起動している場合**か、"
                "社内サーバ・Docker でフォルダを渡している場合です。\n\n"
                "クラウドから使うときは、上の **「🖥️ PC の Addata をこの画面から使う」** で PC の `C:\\Addata` を選ぶのが"
                "おすすめです（車種マスタ 約 9MB と見積の車種フォルダだけを送ります）。"
                "**② 取得URL** は、300MB までの ZIP を置ける場合の代替です。"
            )

            _cur_dir = addata_setting(_QS_ADDATA_DIR)
            _cur_url = addata_setting(_QS_ADDATA_URL)
            # 入力欄の名前に番号を付けて、「設定を消す」で番号を進める。
            # Streamlit は key を付けた入力の値を持ち続け、描き直しでは
            # value= より持っている方を使う。**確定したあとに
            # session_state から消そうとしても拒まれる**ので、
            # 名前ごと変えて別の入力として作り直すのが確実。
            _w = int(st.session_state.get('_addata_widget_nonce', 0) or 0)

            st.markdown("**① フォルダのパス**（このアプリが動いているマシンから見える場所）")
            _in_dir = st.text_input(
                "Addata フォルダ", value=_cur_dir, key='addata_dir_input_%d' % _w,
                placeholder=r'例: C:\Addata',
                label_visibility='collapsed',
                help='「A〜Zの1文字フォルダ」と「COM」を含むフォルダを指定します。'
                     'ZIP に固める必要はありません。',
            )

            st.markdown("**② 取得URL**（ZIP の置き場所。クラウドでも効きます）")
            _in_url = st.text_input(
                "Addata の ZIP の URL", value=_cur_url, key='addata_url_input_%d' % _w,
                placeholder='例: https://…/Addata.zip',
                label_visibility='collapsed',
                help='OneDrive・SharePoint・Google ドライブの共有リンクも使えます'
                     '（ダウンロード用の形に自動で直します）。'
                     '社内の HTTP サーバでも構いません。',
            )

            _keep_in_url = st.checkbox(
                "設定をURLに残す（ブックマークすれば別のPCでもそのまま使えます）",
                value=True, key='addata_keep_in_url',
                help='ブラウザのアドレス欄に設定が入ります。'
                     'そのURLをブックマーク・共有すれば、開いた人は設定済みの状態で始められます。',
            )

            _c1, _c2 = st.columns(2)
            with _c1:
                _save = st.button("💾 保存して使う", key='addata_save_btn',
                                  type='primary', width='stretch')
            with _c2:
                _clear = st.button("↩️ 設定を消す", key='addata_reset_btn',
                                   width='stretch')

            if _clear:
                for _k in (_QS_ADDATA_DIR, _QS_ADDATA_URL):
                    st.session_state.pop('_setting_' + _k, None)
                    try:
                        if _k in st.query_params:
                            del st.query_params[_k]
                    except Exception:
                        pass
                # 入力欄そのものの中身も消す。番号を進めると別の入力として
                # 作り直されるので、持ち越された値が付いてこない。
                # （確定後に session_state から消す手は Streamlit に拒まれる）
                st.session_state['_addata_widget_nonce'] = _w + 1
                _addata_url_forget_failure()
                st.session_state.pop('_addata_dir_warn', None)
                _defer_sidebar_rerun()

            if _save:
                _d = safe_str(_in_dir).strip()
                _u = safe_str(_in_url).strip()
                _problems = []
                # フォルダのパスが見えないのは**保存を止める理由にしない**。
                # そもそもパスはマシン依存で、クラウドでは必ず見えない。
                # 「自分のPC用にパスを入れ、クラウド用に取得URLも入れる」は
                # 正しい使い方なので、止めると取得URLを保存できなくなる。
                # 見えないパスは探す順番の中で黙って飛ばされる（下の取得URLへ進む）。
                _dir_warn = ''
                if _d and not _addata_is_valid(_d):
                    _dir_warn = (
                        'フォルダ「%s」は、このアプリが動いているマシンからは見えません'
                        '（クラウドで動いている場合、お使いのPCのフォルダは指定できません）。'
                        % _d) + ('そのまま保存しますが、実際に使われるのは下の取得URLです。' if _u else
                                 # クラウドでの正しい経路（PC のフォルダを選ぶ橋渡し）に案内する（「取得URLも設定して
                                 # ください」だけでは、おすすめの手が分からない。亮平さんの指摘 2026-09-16）
                                 'この欄は空のままで構いません。クラウドでお使いのときは、上の'
                                 '「🖥️ PC の Addata をこの画面から使う」でフォルダを選んでください'
                                 '（一度選べば、同じブラウザで開くかぎり覚えています）。ZIP を置ける場所があれば、取得URLでも使えます。')
                if _u:
                    import addata_settings as _as
                    _ok_u, _why_u = _as.validate_url(_u)
                    if not _ok_u:
                        _problems.append('取得URL: %s' % _why_u)
                # 注意書きは保存のあとの画面の描き直しで消えてしまうので、
                # セッションに置いて描き直したあとに出す。
                if _dir_warn:
                    st.session_state['_addata_dir_warn'] = _dir_warn
                else:
                    st.session_state.pop('_addata_dir_warn', None)
                if _problems:
                    for _pb in _problems:
                        st.error('❌ ' + _pb)
                else:
                    # セッションに覚える（URLに残さない選択でも今回は効くように）
                    st.session_state['_setting_' + _QS_ADDATA_DIR] = _d
                    st.session_state['_setting_' + _QS_ADDATA_URL] = _u
                    try:
                        for _k, _v in ((_QS_ADDATA_DIR, _d), (_QS_ADDATA_URL, _u)):
                            if _keep_in_url and _v:
                                st.query_params[_k] = _v
                            elif _k in st.query_params:
                                del st.query_params[_k]
                    except Exception:
                        pass
                    _addata_url_forget_failure()
                    if _u:
                        with st.spinner('Addata を取得しています…'):
                            _got = _addata_from_url(_u)
                        if not _got:
                            st.error('❌ %s' % st.session_state.get(
                                '_addata_url_error', '取得できませんでした'))
                        else:
                            _defer_sidebar_rerun()
                    else:
                        _defer_sidebar_rerun()

            if st.session_state.get('_addata_dir_warn'):
                st.warning('⚠️ ' + st.session_state['_addata_dir_warn'])
            if st.session_state.get('_addata_url_error'):
                st.error('❌ 取得URL: %s' % st.session_state['_addata_url_error'])

            st.markdown("---")
            st.markdown("**③ ZIP をアップロード**（その場かぎり。設定は残りません）")
            st.caption(
                "ZIPの中身は「A〜Zの1文字フォルダ ／ 車種コード ／ *.DB」の構造。"
                "車種の自動特定には COM/KA06_ALL.DB も必要です。"
                "全体が大きい場合は、対象車種のフォルダと COM だけでも動きます。"
            )
            _addata_zip = st.file_uploader(
                "Addata の ZIP",
                type=['zip'],
                key=f"addata_zip_upload_{int(st.session_state.get('_addata_zip_nonce', 0) or 0)}",
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
                    _defer_sidebar_rerun()
                else:
                    _rmtree_addata(_dest)
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
        adjuster_post   = st.text_input("支店・所属（アジャスター）", value=st.session_state.get('adjuster_post', ''),
                                        key=f'adjuster_post_input_{_fseq}', max_chars=20,
                                        help="速報報告書の支店名・サービスセンター名など。NEO の保険欄（アジャスター所属）に入ります")
        factory_name    = st.text_input("立会工場（協定の相手）", value=st.session_state.get('factory_name', ''),
                                        key=f'factory_name_input_{_fseq}', max_chars=30,
                                        placeholder="例: ｶｰﾎﾞﾃﾞｰ○○ 09XXXXXXXX ／ 写真鑑定",
                                        help="NEO の保険欄の立会工場（ConsultantFactory）。工場は「半角カナの略称＋半角スペース＋ハイフン無しの電話番号」、"
                                             "写真だけの案件（画像鑑定）は「写真鑑定」と書きます（過去 NEO の書き方）。半角 30 文字（全角 15 文字）まで")
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
            ('agency_name', agency_name), ('adjuster_name', adjuster_name), ('adjuster_post', adjuster_post),
            ('factory_name', factory_name),
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
        # STEP 1-A: 見積書PDF → NEO（pdf-to-neo スキル。これが主導線）
        # ================================================================
        # 判断・生成・検算・合否は files の pdf-to-neo スキル（vendor/pdf_to_neo、コミット固定）を
        # そのまま呼ぶ。アプリが持つのは「見積書を Claude に読ませ、ページごとに検算し、
        # 落ちたページだけ読み直す」ところだけ（docs/pdf-to-neo_アプリ移植ガイド.md）。
        # 旧経路（run_pdf_to_neo_pipeline）は規則を別に実装していて食い違うため UI から外した。
        try:
            from neo_skill import vendor as _nsk_vendor
            _nsk_why = _nsk_vendor.readiness_error()
            _nsk_commit = _nsk_vendor.commit_short()
        except Exception as _e:
            _nsk_why, _nsk_commit = f'neo_skill を読み込めない: {_e}', ''
        _nsk_ready = not _nsk_why
        st.markdown(
            '<div style="background:#eff6ff;border:2px dashed #60a5fa;'
            'border-radius:14px;padding:20px 22px;margin-bottom:14px;">'
            '<div style="font-size:18px;font-weight:800;color:#1d4ed8;'
            'letter-spacing:.02em;">📄 見積書（PDF・写真）をここに入れてください</div>'
            '<div style="font-size:13px;color:#334155;margin-top:8px;line-height:1.7;">'
            '見積書を AI（Claude または Gemini。下で選びます）が<b>印字どおり</b>に写し、ページごとに機械検算して落ちたページだけ読み直します。'
            '部品コード・標準品番・指数・塗装・費用の判断とNEOの生成・検算は '
            '<b>pdf-to-neo スキル</b>（コグニ実機で確かめた判断規則）がそのまま行います。'
            '合格したときだけ、NEO と<b>確認箇所シート（xlsx）</b>を組でお渡しします。'
            '合計を合わせるための金額調整はしません。</div>'
            + (f'<div style="font-size:11px;color:#64748b;margin-top:6px;">スキル: commit {_nsk_commit} ／ アプリ側 neo_skill: {_nsk_code_stamp()}</div>' if _nsk_commit else '')
            + '</div>', unsafe_allow_html=True)
        # 税区分のラジオは下の CSV 取り込みが session_state['tax_override'] を読むので残す。
        # この経路（PDF→NEO）は見積書の合計欄から税込印字を見分ける（reading_schema.md）ので使わない。
        _pdf_tax_options = ['税抜き（外税）', '税込み（内税）']
        _saved_pdf_tax = st.session_state.get('pdf_tax_override',
                                              st.session_state.get('tax_override', '税抜き（外税）'))
        _pdf_tax_idx = 1 if ('内税' in str(_saved_pdf_tax) or '税込' in str(_saved_pdf_tax)) else 0
        _pdf_tax_sel = st.radio(
            "💴 見積書の金額表記（CSV 取り込みとベタ打ちで使います。pdf-to-neo スキルの経路は見積書の合計欄から自動判定）",
            options=_pdf_tax_options,
            index=_pdf_tax_idx,
            horizontal=True,
            key='pdf_tax_radio',
        )
        st.session_state['pdf_tax_override'] = _pdf_tax_sel
        st.session_state['tax_override'] = _pdf_tax_sel

        _p2n_file = st.file_uploader(
            "📄 見積書（PDF・写真）をここにドロップ、またはクリックして選択",
            type=['pdf', 'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff', 'tif', 'heic', 'heif'],
            key='pdf2neo_upload',
        )
        # 見積書が**別のファイル**に変わったら別の案件: 前の見積書のときの車検証・書類の添付、サイドバーの事故・保険情報と費用、
        # 生成結果を消す（Codex hunt A2 2026-09-15）。最初の 1 枚目（前が無い）では消さない ＝ 書類を先に入れてから見積書でもよい
        _p2n_early_key = ''
        if _p2n_file is not None:
            try:
                _p2n_early_bytes = _p2n_file.getvalue()
                _p2n_early_key = f"{_p2n_file.name}|{len(_p2n_early_bytes)}|{hashlib.sha256(_p2n_early_bytes).hexdigest()}"
            except Exception:  # noqa: BLE001
                _p2n_early_key = f"{_p2n_file.name}|?"
            _p2n_prev_key = str(st.session_state.get('_p2n_last_file_key') or '')
            if _p2n_prev_key and _p2n_prev_key != _p2n_early_key:
                st.session_state['_p2n_last_file_key'] = _p2n_early_key
                _docs_now = _docs_sig()
                _docs_prev = str(st.session_state.get('_p2n_last_docs_sig') or '')
                # 片方の書類だけ前の見積書のときのまま（もう片方だけ入れ替えた）なら、残った方は前の案件の書類 ＝ 全部消す（Codex 76）
                _slot_same = any(p and p == n for p, n in zip(_docs_prev.split('|'), _docs_now.split('|')))
                if not any(_docs_now.split('|')) or _docs_now == _docs_prev or _slot_same:
                    # 添付が無い、前の見積書のときのまま、または片方が前のまま ＝ 前の案件の書類・保険欄。消して案内する（Codex 73/76）
                    _reset_case_inputs()
                    st.session_state['_p2n_reset_msg'] = (
                        f"見積書が「{_p2n_file.name}」に変わったので、前の見積書のときの車検証・事故/保険の書類の添付、サイドバーの"
                        "事故・保険情報と費用、生成結果を消しました（別の案件の値を持ち越さないため）。同じ案件なら入れ直してください。")
                    st.rerun()
                else:
                    # 見積書を外した後に書類を入れ替えてから次の見積書を入れた ＝ 新しい案件の書類。書類とその書類から入れた欄は残し、
                    # 前の案件の手入力の保険欄・費用・結果は消す（Codex 71/72）。
                    # ここで st.rerun() すると、この run でまだ描いていない書類の uploader の値が捨てられて書類が消える
                    # （実ブラウザで確認 2026-09-15）ので、書類の uploader を描き終えてから描き直す（_p2n_deferred_rerun）
                    _reset_case_inputs(keep_docs=True)
                    st.session_state['_p2n_reset_msg'] = (
                        f"見積書が「{_p2n_file.name}」に変わりました。添付の車検証・事故/保険の書類は見積書を外した後に入れ替えたものなので"
                        "そのまま使います（書類から入れた欄も残します）。手で入れた事故・保険情報・費用と前の生成結果は消しました。")
                    st.session_state['_p2n_deferred_rerun'] = True
            st.session_state['_p2n_last_file_key'] = _p2n_early_key
            st.session_state['_p2n_last_docs_sig'] = _docs_sig()   # この見積書のときの添付（見積書を外している間は更新しない）
        if st.session_state.get('_p2n_reset_msg') and not st.session_state.get('_p2n_deferred_rerun'):
            st.info("🔄 " + str(st.session_state.pop('_p2n_reset_msg')))
        # 添付の書類（車検証・事故/保険の書類）は生成ボタンより前に描く: ボタンの処理が st.rerun() したとき、まだ描いていない
        # uploader の値は Streamlit に捨てられ、車種フォルダ待ちからの再開で添付が消えてしまう（2026-09-14 実ブラウザで発覚）
        with st.container():
            vehicle_file = st.file_uploader(
                "📋 車検証（任意）PDF・JPG・PNG 対応",
                type=['pdf', 'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff', 'tif', 'heic', 'heif'],
                key=_doc_upload_keys()[0],
            )
            if vehicle_file:
                st.success(f"✅ {vehicle_file.name}")
            insurance_doc_file = st.file_uploader(
                "🛡️ 事故・保険の書類（任意）速報報告書・受付票などの写真/PDF",
                type=['pdf', 'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff', 'tif', 'heic', 'heif'],
                key=_doc_upload_keys()[1],
                help="保険会社・共済からの速報報告書や事故受付票の写真・スクリーンショット。依頼会社名・支店・担当者・事故番号・事故日・"
                     "契約者名・カラーNo・走行距離などを読み取り、サイドバーの事故・保険情報と NEO の車両情報に使います",
            )
            if insurance_doc_file:
                st.success(f"✅ {insurance_doc_file.name}")
            # 事故・保険の書類の差し替え／取り外しに合わせて、書類から埋めた欄を消す（API キーの有無・読み取りの成否に関係なく。
            # 前の案件の受付番号や担当者を次の案件に残さない。Codex 54〜58）。消したあと、読めたら下で入れ直す
            _ins_sha = ''
            if insurance_doc_file is not None:
                try:
                    _ins_sha = hashlib.sha256(insurance_doc_file.getvalue()).hexdigest()
                except Exception:  # noqa: BLE001
                    _ins_sha = ''
            if _ins_sha != st.session_state.get('_insdoc_cleared_sha', ''):
                # 前の書類から入れた値のまま（利用者が直していない）項目だけ消す。手で入れた・直した値は残す（Codex 61）
                _filled_before = st.session_state.get('_insdoc_filled') or {}
                for _k in _DOC_INSURANCE_KEYS:
                    if _k in _filled_before and str(st.session_state.get(_k, '') or '') == str(_filled_before.get(_k) or ''):
                        st.session_state[_k] = ''
                st.session_state['_insdoc_cleared_sha'] = _ins_sha
                st.session_state.pop('_insdoc_applied', None)
                st.session_state.pop('_insdoc_filled', None)
                st.session_state['form_seq'] = int(st.session_state.get('form_seq', 0)) + 1   # 入力欄を作り直して空にする
                st.rerun()
            # 添付した書類はその場で読み取る（同じファイルは二度読まない）。書類の事故・保険情報はサイドバーの欄に入れ、
            # 生成時はサイドバーの値（利用者が直せる）と車検証の車両情報を使う
            if api_key and (vehicle_file or insurance_doc_file):
                with st.spinner("添付の書類を読み取っています…"):
                    _att_vd, _att_doc = _attached_docs_ocr(api_key, selected_model)
                _att_sum = _doc_hints.summary(_att_vd, _att_doc)
                if _att_sum:
                    st.caption("読み取り済み → " + _att_sum + "（生成時に車両・顧客・保険の情報に使います）")
                if st.session_state.get('_doc_ocr_error'):
                    st.warning("⚠️ 読み取れませんでした: " + str(st.session_state.pop('_doc_ocr_error')))
                _att_fill = _doc_hints.sidebar_insurance_from_doc(_att_doc) if _att_doc else {}
                _att_oid = st.session_state.get('_insdoc_ocr_id') if insurance_doc_file is not None else ''
                if _att_fill and _att_oid and st.session_state.get('_insdoc_applied') != _att_oid:
                    # 読めた書類の値をサイドバーに入れる（入れる値が無い・読めなかったときは印を付けない ＝ あとで読めたら入る。Codex 58・60）。
                    # 印は「ファイル＋モデル＋キー」の同一性（モデルを切り替えて読み直したら入れ直す。Codex 61）
                    # 空欄か、前に書類から入れたままの項目にだけ入れる（利用者が手で入れた・直した値は書き換えない。Codex 62）
                    _prev_filled = st.session_state.get('_insdoc_filled') or {}
                    _updates, _now_filled = _doc_fill_plan(
                        _att_fill, _prev_filled,
                        {_k: st.session_state.get(_k, '') for _k in set(_att_fill) | set(_prev_filled)})
                    for _k, _v in _updates.items():
                        st.session_state[_k] = _v
                    st.session_state['_insdoc_filled'] = _now_filled   # どの値を書類から入れたか（差し替え・取り外しで消す範囲。読み直しで無かった項目も追跡を続ける）
                    st.session_state['form_seq'] = int(st.session_state.get('form_seq', 0)) + 1   # 入力欄を作り直して値を出す
                    st.session_state['_insdoc_applied'] = _att_oid
                    st.rerun()
            elif (vehicle_file or insurance_doc_file) and not api_key:
                st.caption("書類の読み取りには Gemini API キーが必要です（サイドバーの「APIキー設定」）")
        if st.session_state.pop('_p2n_deferred_rerun', False):
            st.rerun()   # 書類の uploader を描き終えたので、サイドバーの入力欄（form_seq）と案内を描き直す
        _p2n_beta_ui_shown = False   # この run でベタ打ちの UI を描いたか（locals() で見ない。バグハント H7）
        if _p2n_file is not None:
            _p2n_bytes = _p2n_file.read()
            _p2n_file.seek(0)
            # この見積の同一性（車種フォルダ待ちの取り置きが別の見積で再開されないよう照合する）
            _p2n_file_key = f"{_p2n_file.name}|{len(_p2n_bytes)}|{hashlib.sha256(_p2n_bytes).hexdigest()}"
            _p2n_beta_ui_shown = False   # この run でベタ打ちの UI を描いたか（Addata なしの枝で立てる。結果の下の逃げ道と二重に描かない）
            st.caption(f"📄 {_p2n_file.name}（{len(_p2n_bytes):,} bytes）")
            st.caption("サイドバーの「事故・保険情報」（証券番号・契約者名・事故日・受付番号・代理店・アジャスター・入出庫日・修理日数）は、"
                       "見積書に印字が無ければ NEO に補われます。"
                       "「費用（Expense）」欄はこの経路では使いません — 見積書に印字された費用だけを写します"
                       "（印字に無い費用を足すと、原本との照合が崩れるため）。Addata なしの「ベタ打ちで生成」だけは、"
                       "下のチェックを入れたときに限りサイドバーの費用を NEO に入れます。")
            if not _nsk_ready:
                st.error("❌ pdf-to-neo スキル（vendor/pdf_to_neo）が使えません: " + _nsk_why
                         + "  → `python tools/vendor_sync.py --source <files> --commit <ID>` で取り込み、"
                         "アプリを再起動してください。")
            elif not (claude_api_key or api_key):
                st.warning("⚠️ 見積書の読み取りには Claude または Gemini の APIキーが必要です。"
                           "サイドバーの「APIキー設定」で入力するか、.env の ANTHROPIC_API_KEY / GEMINI_API_KEY に設定してください。")
            elif not (_p2n_addata := find_addata_dir()):
                # Addata が決まらないとき: pdf-to-neo スキルの経路（部品コード・標準指数を引く）は vendor の自動検出に
                # 落とさず止める（別の版で作らない）。代わりに、以前からある「ベタ打ち（モードA）」で作れるようにする
                # （2026-09-14 亮平さん指示: Addata が特定できない状態ではベタ打ちも使えなくなっていた）。
                # ベタ打ち = 見積書の明細・金額・品名をそのまま写す。部品コード・標準品番・標準指数は入らない
                _p2n_url_err = st.session_state.get('_addata_url_error')
                st.warning("⚠️ Addata（コグニの車種データ）が決まっていないので、部品コード・標準指数を引く生成（pdf-to-neo スキル）はできません。"
                           + (f" 取得URLの失敗: {_p2n_url_err}" if _p2n_url_err else "")
                           + " サイドバー「🖥️ PC の Addata をこの画面から使う」で PC の C:\\Addata を選ぶか、"
                           "下の「ベタ打ちで生成」で部品コード無しの NEO を作れます（明細・金額・品名は見積書のとおり）。")
                _p2n_beta_ui_shown = True
                _beta_generate_ui(_p2n_file, _p2n_bytes, _p2n_file_key, api_key, selected_model, _pdf_tax_sel)
            else:
                # 読み手: 両方のキーがあれば選べる。**既定は Gemini**（2026-09-16 亮平さん指示: API は Gemini をメインで使う。
                # 本番の Secrets も Gemini だけ）。片方だけならそれを使う。指示文・検算・読み直し・生成は同じなので、
                # 違うのは読み取りの精度だけ（読み取りの揺れは、ページごとの検算・読み直しと下書きの判断で受け止める）
                _p2n_choices = []
                if api_key:
                    _p2n_choices.append(('gemini', f"Gemini（{selected_model}）"))
                if claude_api_key:
                    _p2n_choices.append(('claude', f"Claude（{os.environ.get('NEO_READER_MODEL') or 'claude-opus-5'}）"))
                if len(_p2n_choices) > 1:
                    _p2n_labels = [c[1] for c in _p2n_choices]
                    _p2n_prev = st.session_state.get('_p2n_reader_label')   # 書類添付の rerun でラジオが描かれる前に状態が捨てられても選択を保つ（H6）
                    _p2n_pick = st.radio("🤖 見積書を読む AI", options=_p2n_labels, index=(_p2n_labels.index(_p2n_prev) if _p2n_prev in _p2n_labels else 0),
                                         horizontal=True, key='pdf2neo_reader',
                                         help="判断・生成・検算は同じです。読み取りの精度だけが変わります。"
                                              "読み取り結果の行（初回検算合格・読み直し回数）で比べられます。")
                    _p2n_kind = next(c[0] for c in _p2n_choices if c[1] == _p2n_pick)
                    st.session_state['_p2n_reader_label'] = _p2n_pick
                else:
                    _p2n_kind = _p2n_choices[0][0]
                    st.caption(f"読み取りに使う AI: {_p2n_choices[0][1]}")
                _p2n_key = claude_api_key if _p2n_kind == 'claude' else api_key
                _p2n_model = '' if _p2n_kind == 'claude' else selected_model
                from neo_skill import bridge as _br
                _p2n_bridge = _br.is_bridge(_p2n_addata)   # PC の Addata をブラウザ経由で使っている
                _p2n_profile = (os.environ.get('NEO_SKILL_PROFILE') == '1')  # 工場プロファイルは既定で書かない
                _p2n_pending = st.session_state.get('_bridge_pending')
                _p2n_stale_why = ''
                if _p2n_pending:
                    import time as _p2n_time
                    if _p2n_pending.get('file_key') != _p2n_file_key:
                        _p2n_stale_why = '見積書が変わったので'          # 別の見積の NEO を出さない
                    elif _p2n_time.time() - float(_p2n_pending.get('parked_at') or 0) > 2 * 3600:
                        _p2n_stale_why = '車種フォルダを 2 時間待っても届かなかったので'   # 取り置き（顧客情報）を残し続けない
                    elif _p2n_pending.get('doc_key'):
                        _vd0, _doc0 = _attached_docs_ocr(api_key, selected_model)   # 控え済みなら Gemini は呼ばない
                        if _p2n_pending['doc_key'] != _doc_key(_doc_hints.vehicle_hint(_vd0, _doc0), _doc_hints.customer_hint(_vd0, _doc0),
                                                              _insurance_hint_now(_doc0)):
                            _p2n_stale_why = '添付の書類や事故・保険情報が変わったので'
                if _p2n_stale_why:
                    from neo_skill import maker as _nsk_maker
                    _nsk_maker.remove_case_dir((st.session_state.pop('_bridge_pending') or {}).get('case_dir'))
                    st.session_state['_bridge_want'] = ''
                    _p2n_pending = None
                    st.info(f"{_p2n_stale_why}、前の読み取り結果は捨てました。もう一度「見積書からNEOを生成」を押してください。")
                if _p2n_bridge and _p2n_pending and not st.session_state.get('_bridge_want'):
                    # 車種フォルダの取り込みを待っていた読み取りの続き（部品が送り終わると rerun されてここに来る）
                    _p2n_car = str(_p2n_pending.get('car_code') or '')
                    st.session_state.pop('_bridge_pending', None)
                    if _p2n_car and not _br.has_car(_p2n_addata, _p2n_car):
                        _p2n_out = dict(_p2n_pending, ok=False, stage='error',
                                        error=f"車種 {_p2n_car} のフォルダを PC の Addata から取り込めませんでした"
                                              f"（{st.session_state.get('_bridge_msg') or '理由不明'}）。"
                                              "PC の Addata に その車種フォルダがあるか（版が新しいか）を確かめてください")
                        from neo_skill import maker as _nsk_maker
                        _nsk_maker.remove_case_dir(_p2n_pending.get('case_dir'))
                    else:
                        with st.status("車種フォルダが届いたので NEO を作っています…", expanded=True) as _p2n_status:
                            def _p2n_progress(msg):
                                _p2n_status.write(msg)
                            from neo_skill import vendor as _p2n_vendor_now
                            if _p2n_pending.get('vendor_commit') and _p2n_pending.get('vendor_commit') != _p2n_vendor_now.commit_short():
                                # 読み取りの後（車種フォルダ待ちの間）にアプリが更新された: 前の版で読んだ結果を新しい版で NEO に
                                # しない（N1）
                                from neo_skill import maker as _nsk_maker
                                _nsk_maker.remove_case_dir(_p2n_pending.get('case_dir'))
                                _p2n_out = dict(_p2n_pending, ok=False, stage='error',
                                                error='読み取りの後でアプリ（生成器）が更新されました。お手数ですが、もう一度「見積書からNEOを生成」を押してください')
                            else:
                                _p2n_out = _guarded_call(p2n_make, _p2n_pending, addata_root=_p2n_addata, record_profile=_p2n_profile,
                                                         progress=_p2n_progress)
                            _p2n_status.update(
                                label=("✅ 合格" if _p2n_out.get('ok') else "❌ 不合格（下の理由をご確認ください）"),
                                state=('complete' if _p2n_out.get('ok') else 'error'), expanded=False)
                    if isinstance(_p2n_out, dict):
                        _p2n_out['inputs_sig'] = _p2n_inputs_signature(_p2n_file_key, api_key=api_key, model_name=selected_model,
                                                                       addata_id=_p2n_addata_identity(_p2n_addata))
                    st.session_state['pdf2neo_result'] = _p2n_out
                    st.rerun()
                st.caption(_attached_docs_caption(api_key, selected_model))
                if st.button("🚀 見積書からNEOを生成", key='pdf2neo_run', type="primary",
                             width='stretch'):
                    _p2n_skew = sync_app_modules()   # push 後にプロセスが残る本番で古い neo_skill を使わない（ベタ打ちと同じ扱い。バグハント H4）
                    if _p2n_skew:
                        st.error(_version_skew_message(_p2n_skew))
                        st.stop()
                    st.session_state.pop('pdf2neo_result', None)
                    _p2n_stale = st.session_state.pop('_bridge_pending', None)
                    if _p2n_stale:
                        # 車種フォルダ待ちのまま押し直した: 取り置きの作業フォルダ（reading.json 入り）を消してから読み直す
                        from neo_skill import maker as _nsk_maker
                        _nsk_maker.remove_case_dir(_p2n_stale.get('case_dir'))
                    st.session_state['_bridge_want'] = ''
                    with st.status("見積書を読んで NEO を作っています…（ページ数により 1〜5 分）",
                                   expanded=True) as _p2n_status:
                        def _p2n_progress(msg):
                            _p2n_status.write(msg)
                        # 添付の車検証・事故/保険の書類（STEP 1-B）の読み取りを hint に写す（見積書に印字が無い項目にだけ補われる）。
                        # 保険はサイドバーの値（書類から埋めたものを利用者が直せる）を優先する
                        _p2n_vd, _p2n_doc = _attached_docs_ocr(api_key, selected_model, _p2n_progress)
                        _p2n_vhint = _doc_hints.vehicle_hint(_p2n_vd, _p2n_doc)
                        _p2n_chint = _doc_hints.customer_hint(_p2n_vd, _p2n_doc)
                        # 保険はサイドバーの値だけを使う（書類から読んだ値は添付時にサイドバーへ入れてあり、利用者が消した項目を書類から戻さない。Codex 54）
                        _p2n_ihint = _insurance_hint_now(_p2n_doc)
                        _p2n_doc_key = _doc_key(_p2n_vhint, _p2n_chint, _p2n_ihint)
                        _p2n_kw = dict(
                            mime_type=get_mime_type(_p2n_file.name),
                            # 車検証（車両・顧客）と サイドバーの「事故・保険情報」。見積書に印字が無い項目にだけ補われる
                            vehicle_hint=_p2n_vhint, customer_hint=_p2n_chint,
                            insurance_hint=_p2n_ihint,
                            progress=_p2n_progress,
                            record_profile=_p2n_profile,
                            # サイドバー / URL / ZIP / PC からの橋渡し で決めた ADDATA を vendor にも使わせる（版の食い違いを防ぐ）
                            addata_root=_p2n_addata,
                            reader_kind=_p2n_kind, model_name=_p2n_model,
                        )
                        if _p2n_bridge:
                            # PC の Addata: 読む → 車種を決める（COM だけで足りる）→ 車種フォルダが無ければ部品に頼んで待つ
                            _p2n_state = _guarded_call(p2n_read, _p2n_bytes, _p2n_file.name, _p2n_key, **_p2n_kw)
                            if not _p2n_state.get('ok'):
                                _p2n_out = _p2n_state
                            else:
                                _p2n_progress('車種を決めています（PC の Addata の車種マスタ）')
                                _p2n_res = _br.resolve_car(_p2n_addata, _p2n_state.get('reading') or {})
                                _p2n_car = str(_p2n_res.get('car_code') or '')
                                _p2n_state['car_code'] = _p2n_car
                                _p2n_state['file_key'] = _p2n_file_key
                                _p2n_state['doc_key'] = _p2n_doc_key   # 添付の書類・保険が変われば取り置きを捨てる（Codex 60）
                                import time as _p2n_time
                                _p2n_state['parked_at'] = _p2n_time.time()
                                if _p2n_car and not _br.has_car(_p2n_addata, _p2n_car):
                                    st.session_state['_bridge_pending'] = _p2n_state
                                    st.session_state['_bridge_want'] = _p2n_car
                                    _p2n_status.update(label=f"車種 {_p2n_car} のフォルダを PC から取り込んでいます…",
                                                       state='running', expanded=True)
                                    st.rerun()
                                if not _p2n_car:
                                    _p2n_progress('車種マスタで車種を決められませんでした（' + str(_p2n_res.get('error') or _p2n_res.get('evidence') or '')[:120]
                                                  + '）。そのまま生成に進みます')
                                _p2n_out = _guarded_call(p2n_make, _p2n_state, addata_root=_p2n_addata, record_profile=_p2n_profile,
                                                         progress=_p2n_progress)
                        else:
                            _p2n_out = _guarded_call(run_pdf_to_neo_skill, _p2n_bytes, _p2n_file.name, _p2n_key, **_p2n_kw)
                        _p2n_status.update(
                            label=("✅ 合格" if _p2n_out.get('ok') else "❌ 不合格（下の理由をご確認ください）"),
                            state=('complete' if _p2n_out.get('ok') else 'error'), expanded=False)
                    if isinstance(_p2n_out, dict):
                        _p2n_out['inputs_sig'] = _p2n_inputs_signature(_p2n_file_key, api_key=api_key, model_name=selected_model,
                                                                       addata_id=_p2n_addata_identity(_p2n_addata))
                    st.session_state['pdf2neo_result'] = _p2n_out
                    st.rerun()

        if _p2n_file is None and st.session_state.get('_bridge_pending'):
            # 車種フォルダ待ちの途中で見積を外した: 取り置きを捨てる（作業フォルダも消す。部品への依頼も取り下げる）
            from neo_skill import maker as _nsk_maker
            _nsk_maker.remove_case_dir((st.session_state.pop('_bridge_pending') or {}).get('case_dir'))
            st.session_state['_bridge_want'] = ''

        _p2n_res = st.session_state.get('pdf2neo_result')
        # Addata が外れた後（接続解除・掃除・URL 失敗）に不合格の結果が残っていると、Addata なしの枝でもベタ打ちを描く。
        # 同じ run で 2 回描くとウィジェットのキーが重複して落ちるので、描いた印を見る（レビュー 2026-09-15）
        _p2n_offer_beta = (isinstance(_p2n_res, dict) and _p2n_file is not None and bool(api_key) and not _p2n_beta_ui_shown
                           and ((not _p2n_res.get('legacy_beta') and not _p2n_res.get('ok'))
                                or bool(_p2n_res.get('fallback_from_skill')))   # 逃げ道で作った後も、入力を直したら作り直せる（H2/K4）
                           # 入力そのものが読めない（パスワード・ページ数・大きさ・キー）ときは、ベタ打ちでも同じ理由で読めないので出さない（P13）
                           and not any(k in str(_p2n_res.get('error') or '') for k in _P2N_INPUT_ERRORS))
        if isinstance(_p2n_res, dict) and _p2n_res.get('inputs_sig'):
            # 生成したあとに入力（見積書・添付・事故/保険欄・費用・税区分・テンプレート）が変わっていたら、前の入力の結果 ＝ 落とさせない
            _p2n_is_beta = bool(_p2n_res.get('legacy_beta'))
            if _p2n_res['inputs_sig'] != _p2n_inputs_signature(_p2n_early_key, api_key=api_key, model_name=selected_model, beta=_p2n_is_beta,
                                                              addata_id='' if _p2n_is_beta else _p2n_addata_identity(find_addata_dir())):
                _p2n_res = dict(_p2n_res, stale=True)
                st.warning("⚠️ 生成したあとに 見積書・添付の書類・事故/保険情報・費用 のどれかが変わりました。下の結果は前の入力で作ったもので、"
                           "ダウンロードは止めています。生成ボタンを押して作り直してください。")
        if _p2n_res and _p2n_res.get('legacy_beta'):
            _render_beta_result(_p2n_res, selected_model)
            if _p2n_offer_beta:
                # スキル経路が不合格で、逃げ道のベタ打ちで作った結果: 事故・保険欄・費用・税区分を直したらここから作り直す
                # （結果が陳腐化するとダウンロードは止まるので、作り直す入口が要る。H2/K4、レビュー 2026-09-15）
                st.markdown("---")
                st.caption("入力（事故・保険欄・費用・税区分・添付の書類）を直したときは、ここからベタ打ちで作り直せます。")
                _beta_generate_ui(_p2n_file, _p2n_bytes, _p2n_early_key, api_key, selected_model, _pdf_tax_sel, fallback=True)
        elif _p2n_res:
            _p2n_rd = _p2n_res.get('read') or {}
            _p2n_mk = _p2n_res.get('make') or {}
            _p2n_st = _p2n_rd.get('stats') or {}
            if _p2n_rd:
                _p2n_reader = _p2n_res.get('reader') or {}
                st.caption(
                    f"読み取り（{_p2n_reader.get('kind', '?')} {_p2n_reader.get('model', '')}）: {_p2n_rd.get('n_pages')} ページ ／ "
                    f"初回検算合格 {int(round(100 * float(_p2n_st.get('first_try_ok_rate') or 0)))}% ／ "
                    f"読み直し {_p2n_st.get('retries', 0)} 回 ／ API 呼び出し {_p2n_st.get('calls', 0)} 回 ／ "
                    f"{_p2n_st.get('seconds', 0)} 秒")
            if _p2n_res.get('cleanup_warning'):
                st.warning("⚠️ " + _p2n_res['cleanup_warning'])
            if _p2n_res.get('error') and _p2n_res.get('stage') in ('error',):
                st.error(f"❌ {_md_literal(_p2n_res['error'])}")
            elif _p2n_res.get('stage') == 'read' and not _p2n_rd.get('ok'):
                if _p2n_res.get('error'):
                    st.error(f"❌ 読み取りを続けられませんでした: {_md_literal(_p2n_res['error'])}")
                if not (_p2n_res.get('error') and not (_p2n_rd.get('fails') or _p2n_rd.get('traces'))):
                    # 読み取れた写しが検算に通らなかったときだけ（PDF が開けない・ページ数の上限・キーの誤りなど、
                    # 写す前に止まったときは「検算に通らない」とは言わない。P13）
                    st.error("❌ 見積書の写しが機械検算に通りませんでした。NEO は作っていません"
                             "（合計を合わせるために行を消したり金額を動かしたりはしません）。"
                             "下の項目を見積書と突き合わせてください。")
                for _f in (_p2n_rd.get('fails') or []):
                    st.markdown(f"- {_md_literal(_f)}")
                _p2n_tr = _p2n_rd.get('traces') or []
                if _p2n_tr:
                    st.dataframe(pd.DataFrame([{
                        'ページ': t['page'], '検算': '合格' if t['ok'] else '不合格', '明細行': t['rows'],
                        '読んだ回数': t['attempts'], '不合格の理由': ' / '.join(t['fail'])[:120]} for t in _p2n_tr]),
                        hide_index=True, width='stretch')
                if _p2n_res.get('repair_zip'):
                    st.download_button(
                        "🧰 修正用ファイル一式をダウンロード（pages/・reading.json）",
                        data=_p2n_res['repair_zip'], file_name="neo_repair.zip", mime="application/zip",
                        key='pdf2neo_dl_repair_read', width='stretch', disabled=bool(_p2n_res.get('stale')),
                    )
                    st.caption("NEO_check の案件フォルダに展開し、該当ページの pages/page_N.json を見積書と突き合わせて直してから"
                               " `make_neo.py <案件フォルダ>` を回すと続きができます。")
            elif _p2n_mk and not _p2n_mk.get('ok'):
                st.error("❌ NEO の生成が不合格でした（pdf-to-neo スキル make_neo.py の判定）。NEO は出しません。")
                for _r in (_p2n_mk.get('reasons') or []):
                    st.markdown(f"- {_md_literal(_r)}")
                # 読み取り（AI）は同じ見積書でも毎回少しずつ違う（欄の割り当て・費用の置き場所）。
                # もう一度押すと通ることがあるので、先に案内する（2026-09-16 Gemini 4 回の実測）
                st.info("もう一度「見積書からNEOを生成」を押すと、読み取りからやり直します。"
                        "AI の読み取りは同じ見積書でも毎回少し変わるので、これで通ることがあります"
                        "（金額が印字と合わないときは NEO を出さないので、作り直しても中身が甘くなることはありません）。"
                        "下の報告文に「差額と同じ額: 行N …」が出ていれば、その行の印字を確かめてください。")
                if _p2n_mk.get('error'):
                    st.caption(_p2n_mk['error'])
                if _p2n_mk.get('match_line'):
                    st.caption(_p2n_mk['match_line'])
                if _p2n_res.get('blank_wage_retry'):
                    _p2n_bwr = _p2n_res['blank_wage_retry']
                    st.caption(f"工賃欄が空欄の {len(_p2n_bwr.get('rows') or [])} 行を 0 円にして作り直しても合いませんでした: "
                               + ' / '.join(str(r)[:80] for r in (_p2n_bwr.get('reasons') or [])[:3]))
                if _p2n_res.get('repair_zip'):
                    st.download_button(
                        "🧰 修正用ファイル一式をダウンロード（pages/・reading.json・report.md）",
                        data=_p2n_res['repair_zip'], file_name="neo_repair.zip", mime="application/zip",
                        key='pdf2neo_dl_repair_make', width='stretch', disabled=bool(_p2n_res.get('stale')),
                    )
                    st.caption("NEO_check の案件フォルダに展開し、report.md の理由に沿って reading.json（または pages/）を直してから"
                               " `make_neo.py <案件フォルダ>` を回すと続きができます。")
                if _p2n_res.get('report_md'):
                    with st.expander("📝 報告文（report.md）", expanded=True):
                        st.markdown(_md_literal(_p2n_res['report_md']))
                with st.expander("生成ログ（make_neo）", expanded=False):
                    st.code(_p2n_mk.get('tail') or '', language='text')
            elif _p2n_res.get('ok'):
                st.success(f"✅ 合格 — {_p2n_mk.get('match_line') or '見積書合計との一致: OK'}")
                if 'コグニ計算' in str(_p2n_mk.get('match_line') or ''):
                    # 工場の単価に円未満の端数がある見積（コグニの円計算では印字の合計を再現できない案件。2026-09-16 フリード）
                    st.info("この見積は**部品の単価に円未満の端数**があります（例: 単価 154.5 円 × 3 個 = 463.5 → 印字 464）。"
                            "工場は端数のまま合計するので、行ごとに円で足すコグニとは数円ずれます。"
                            "**明細の金額は見積書のとおり**で、差の理由は報告文と確認箇所シートの「要確認」に入れてあります。")
                for _w in (_p2n_rd.get('warn') or []):
                    st.warning(f"⚠️ 読み取りの注意: {_md_literal(_w)}")
                _p2n_name = _p2n_res.get('download_name') or '見積_claude'
                _p2n_c1, _p2n_c2 = st.columns(2)
                with _p2n_c1:
                    st.download_button(
                        "📥 NEOファイルをダウンロード",
                        data=_p2n_res.get('neo_bytes') or b'',
                        file_name=f"{_p2n_name}.neo",
                        mime="application/octet-stream",
                        key='pdf2neo_dl', disabled=bool(_p2n_res.get('stale')),   # 入力が変わった結果は落とさせない（Codex hunt A1）
                        width='stretch',
                    )
                with _p2n_c2:
                    _p2n_ext = _p2n_res.get('review_ext') or '.xlsx'
                    st.download_button(
                        "📥 確認箇所シートをダウンロード",
                        data=_p2n_res.get('review_bytes') or b'',
                        file_name=f"{_p2n_name}_確認箇所{_p2n_ext}",
                        mime=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                              if _p2n_ext == '.xlsx' else "text/csv"),
                        key='pdf2neo_dl_review', disabled=bool(_p2n_res.get('stale')),
                        width='stretch',
                    )
                st.caption("NEO と確認箇所シートは必ず組で保険会社・担当者に渡してください"
                           "（人が確かめる点は NEO の明細コメントではなくシートにあります）。")
                if _p2n_res.get('report_md'):
                    with st.expander("📝 報告文（report.md）", expanded=True):
                        st.markdown(_md_literal(_p2n_res['report_md']))
            else:
                st.error(f"❌ {_p2n_res.get('error') or '変換できませんでした'}")
            if _p2n_offer_beta:
                # スキル経路で NEO が出なかった（検算差・車種未収録・生成の例外）。部品コード無しのベタ打ちで作る道を出す
                # （2026-09-15 実機テスト: 以前はここで行き止まりだった。連鎖の外に置く: 中に挟むと合格結果に誤エラーが出る）
                st.markdown("---")
                st.info("ℹ️ pdf-to-neo スキルの経路では NEO を作れませんでした。部品コード・標準指数の無い **ベタ打ち** で作る手もあります"
                        "（明細・金額・品名は見積書のとおり。合計に合わせる金額調整の行が入ることがあるので、結果の警告を確かめてください）。")
                _beta_generate_ui(_p2n_file, _p2n_bytes, _p2n_early_key, api_key, selected_model, _pdf_tax_sel, fallback=True)

        # ================================================================
        # STEP 1-B: 車検証・テンプレートNEO（任意）
        # ================================================================
        st.markdown('<div class="section-title">📁 テンプレートNEO（任意）</div>',
                    unsafe_allow_html=True)
        st.caption("車検証・事故/保険の書類は上の見積書の下に入れます。テンプレートNEO は任意です。")
        _up_col2 = st.container()
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
                st.caption("📋 テンプレートの工場名・車種の設定は引き継ぎます。立会工場はサイドバーに入れたときだけ書き換えます。" "見積書 PDF からのベタ打ちでは、証券番号・契約者・代理店・アジャスター・受付番号・備考をサイドバーの値で上書きし（空なら空）、立会日・協定日・立会者は空にします。" "CSV 取り込み → プレビュー → NEO 生成（ステップ④）では、空欄の項目はテンプレートの値が残るので、別の案件として出す項目は入力し直してください")
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
            # 受け付けたテンプレートが変わったら描き直す: 生成結果の陳腐化の照合はこの uploader より前にあるので、その run では
            # 前のテンプレートで照合している（Codex 70/73）
            _tpl_now = (hashlib.sha256(st.session_state['custom_neo_bytes']).hexdigest()
                        if st.session_state.get('custom_neo_bytes') else '')
            if str(st.session_state.get('_tpl_sig_seen', '') or '') != _tpl_now:
                st.session_state['_tpl_sig_seen'] = _tpl_now
                st.rerun()

        # ================================================================
        # STEP 1-C: Gemini で CSV 化（PDF で読み取れないときの代替手段）
        # ================================================================
        st.markdown("---")
        st.markdown(
            '<div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;'
            'padding:14px 18px;font-size:14px;margin-bottom:12px;">'
            '🛟 <b>PDF でうまく読み取れないときだけ使う手順</b><br>'
            '<span style="font-size:12px;color:#555;line-height:1.7;">'
            '上の「見積書PDF」で明細がきちんと拾えない見積書は、'
            'こちらで Gemini に読ませて CSV にしてから取り込みます。'
            'ふだんは使いません。</span></div>', unsafe_allow_html=True)
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
* 区分：元の記載から下記のいずれか1語を割り当てる。上に書いたものほど優先する。 【最優先・空欄】「研磨」「磨き」「写真代」「ショートパーツ」を含む行は空欄にする。**部品金額だけの行でも空欄のまま**（「取替」にしない）。ただし「磨き調整」は区分なので次の行を採る。 【重要】次の語群は**見積書に書かれていた語をそのまま**出すこと（言い換えない）: 「脱着修理」「脱着鈑金」「脱着板金」／「点検調整」「点検清掃」／「分解調整」「分解清掃」／「鈑金」「板金」。この文字列は帳票の「修理方法」欄にそのまま印字されるため。 ・磨き調整：「磨き調整」 ・取替：「取替」「交換」「取換」（※部品金額のみで工賃0の行も「取替」とする。ただし上の空欄ルールに当たる行を除く） ・脱着：「脱着」「取外」「取付」「組付」 ・塗装：「塗装」「ペイント」「ワックス」「加算」「ブース」 ・分解調整：単に「分解」とだけ書かれている場合 ・点検：「点検」「診断」 ・調整：「調整」「光軸」「フィッティング」「コーディング」「設定」「消去」 ・修理：「修理」「補修」「修正」「穴あけ」「シーリング」 ・該当なし：空欄
* 数量：見積書の数量（半角。整数でなければ 2.5 のようにそのまま。空欄や不明な場合は 1 を補完）
* 部品金額：「部品、油脂」列の金額。半角整数・カンマなし（記載なしは 0）
* 工賃：「技術料」列の金額。半角整数・カンマなし（記載なしは 0）
* 部品コード：品番・部品番号（記載なしは空欄）
【データ処理の重要ルール（高速化・精度向上）】
1. 1行1明細：1つの項目に「部品、油脂」「技術料」両方の金額がある場合も、見積書と同じ1行のまま部品金額と工賃の両方を書く（2行に分けない。行数を見積書と同じにする）。
2. 品名などにカンマ（,）が入るときは、その欄を「"」で囲む。金額にはカンマを入れない。
3. 列の厳密照合：金額が部品列か技術料列か、PDFの表ヘッダーを厳密に確認する（例：「ショートパーツ」等、技術料列のみの数値を部品列に入れない）。
4. 対象外：合計行、小計行、消費税行は出力しない。全ページ・全明細行を漏れなく処理する。
【合計額の自動検算と出力】 明細抽出後、内部で以下の検算を実施すること。
1. 抽出した全明細の「部品金額」の合計と「工賃」の合計を算出。
2. 見積書原本の最終的な「部品代合計」「技術料（工賃）合計」と照合。
3. 不一致の場合のみ、CSVの末尾に改行して以下を出力（一致時は出力しない）。行全体を「"」で囲むこと。 "部品相違〇,〇〇〇円 工賃相違●,●●●円"
出力はCSVデータおよび相違確認結果のみ。説明文・コメントは一切不要。"""

        # ── ボタン行: プロンプトコピー ＋ Geminiを開く ──
        # st.components.v1.html は 2026-06-01 で削除予定（起動時に警告が出る）。
        # 代替の st.iframe は src しか受け取れず HTML を直接描けないため、
        # Streamlit ネイティブの部品に置き換えてある。
        # st.code は右上に標準のコピーボタンが付くので、コピー機能は保たれる。
        _btn_col1, _btn_col2 = st.columns(2)
        with _btn_col1:
            with st.popover("📋 プロンプトをコピー", width='stretch'):
                st.caption("右上のコピーアイコンで全文をコピーできます")
                st.code(_CSV_PROMPT, language=None)
        with _btn_col2:
            st.link_button("🌐 Geminiを開く（別タブ）",
                           "https://gemini.google.com/",
                           width='stretch')

        # ── CSV取り込みエリア ──
        st.markdown("")
        st.markdown('<div class="section-title">📊 CSV取り込み（代替手段）</div>',
                    unsafe_allow_html=True)

        # 税区分はいちばん上の「見積書の金額表記」で選んだものを使う。
        # ここに2つ目のラジオを置いていたため、利用者がどちらを操作すべきか分からず、
        # 取り違えると NEO の総額が消費税ぶん（10%）ずれていた。
        _pending_tax = st.session_state.pop('_tax_carry_pending', None)
        if _pending_tax:
            st.session_state['tax_override'] = _pending_tax
        _tax_sel = st.session_state.get('tax_override', '税抜き（外税）')
        st.caption(f"💴 金額表記: **{_tax_sel}** — 変えるときは、いちばん上の"
                   "「見積書の金額表記」で切り替えてください")

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
                # 連番で作り直せるようにする（「取り込みをクリア」でファイルも外す。以前はファイルが残り、クリアが効かなかった。M12）
                key=f"csv_file_upload_{st.session_state.get('csv_area_seq', 0)}",
                label_visibility='collapsed',
            )
        _csv_text = ''
        if _csv_file and _csv_paste and _csv_paste.strip():
            st.info("ℹ️ CSV ファイルと貼り付けの両方があります。ファイルの内容を取り込んでいます（貼り付けは使っていません）。")
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
            _csv_errs  = [n for n in _csv_notes if str(n).startswith('❌')]
            _csv_warns = [n for n in _csv_notes if str(n).startswith('⚠️')]
            _csv_diffs = [n for n in _csv_notes if re.match(r'^(部品|工賃)相違', str(n))]
            _csv_other = [n for n in _csv_notes if n not in _csv_errs and n not in _csv_warns and n not in _csv_diffs]
            for _note in _csv_errs:
                st.error(_note)
            for _note in _csv_warns:
                # 金額のある行を読み飛ばした知らせは赤で（注記が多いと 4 件目以降が出ず、行が黙って消えていた。M7）
                (st.error if '読み飛ばしました' in _note else st.warning)(_note)
            for _note in _csv_diffs:
                st.warning(f"⚠️ 見積書との差異が記録されています: {_note}")
            if _csv_other:
                with st.expander(f"CSV の後ろの説明文 {len(_csv_other)} 行（明細には取り込んでいません）", expanded=False):
                    for _note in _csv_other:
                        st.text(str(_note))
            if _csv_errs:
                st.session_state.pop('csv_items', None)
                st.session_state.pop('csv_mode', None)
            elif _preview_items:
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
            _btn_label = ("🚀 NEO生成を開始 →" if _csv_mode_active
                          else "🚗 車検証だけで NEO を作る（明細なし） →")
            if st.button(_btn_label, type="primary", width='stretch'):
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
        elif _p2n_file is None:
            # 見積書が入っているときは、上の生成ボタンが主導線なので出さない。
            st.info("📄 いちばん上で見積書（PDF・写真）を入れて「見積書からNEOを生成」を押してください。"
                    "／ CSVを貼り付けた場合や、車検証だけでNEOを作る場合は、"
                    "この下の「NEO生成を開始」を使います")

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
            _pmeta_s2 = st.session_state.get('pdf2neo_preview_meta') or {}
            _is_preview_s2 = bool(_pmeta_s2) and bool(_pmeta_s2.get('sig')) and _pmeta_s2.get('sig') == _items_sig(_csv_items_s2)
            if _p2n_vi and not vehicle_bytes and _is_preview_s2:
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
                'items':            copy.deepcopy(_csv_items_s2),
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
                # 画面は「逆算一致」ではなく「照合なし」と出す（M8）。
                '_reverse_match':   True,
            }
            if _is_preview_s2:
                # プレビュー取り込み（ベタ打ちの結果）: 見積書に印字された部品計・工賃計と照合する。以前は照合も警告も
                # 確認のチェックも消えて「逆算一致」になっていた（M8/O9）。費用はベタ打ちで入れる選択のときだけ
                # 印字の小計が読めていなければ 0（未照合）。明細の合算を「印字」として比べると必ず一致し、偽の「合格」になる（レビュー）
                estimate_data['pdf_parts_total'] = safe_int(_pmeta_s2.get('pdf_parts_total'))
                estimate_data['pdf_wage_total'] = safe_int(_pmeta_s2.get('pdf_wage_total'))
                estimate_data['pdf_grand_total'] = safe_int(_pmeta_s2.get('pdf_grand_total'))
                estimate_data['_csv_import'] = False
                estimate_data['_preview_import'] = True
                estimate_data['_preview_needs_ack'] = bool(_pmeta_s2.get('needs_ack'))
                estimate_data['_reverse_match'] = False
                estimate_data['_expenses_off'] = bool(_pmeta_s2.get('exp_declined'))
                # 印字の総額が税込の基準か（税抜の印字を税込と比べて偽の不一致を出していた）・取り込んだときの値引きの合計
                # （値引き前の小計との一致は、値引きの行を直していないときだけ認める。レビュー 2 周目）
                estimate_data['_grand_is_intax'] = bool(_pmeta_s2.get('grand_is_intax', True))
                estimate_data['_neg_at_import'] = [
                    sum(min(0, safe_int(_it.get('parts_amount', 0))) for _it in _csv_items_s2),
                    sum(min(0, safe_int(_it.get('wage', 0))) for _it in _csv_items_s2)]
            if _is_tax_incl_csv:
                st.info("💴 税込モード: CSVの金額は税込みとして処理されます")
            else:
                st.info("💴 税抜モード: CSVの金額は税抜きとして処理されます")
            # CSV 取り込みは step2 を通らずに step3 へ進むため、ここで通さないと
            # この経路だけ部品コードが入らない。車検証を読ませていない場合
            # （vehicle_data が無い場合）は、中で何もせずそのまま転記になる。
            apply_addata_matching(estimate_data, st.session_state.get('vehicle_data'))
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
                # 以前はここに `_current_mode == 'db'` の条件が入っていたが、
                # step1 の冒頭で selected_mode は必ず 'beta' に固定されるため、
                # この照合は一度も実行されず、Addata を読み込ませても
                # NEO の部品コード（_master_ref_no → PartsCode）が常に空だった。
                apply_addata_matching(estimate_data, vehicle_data, progress)
                
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
                # 作業区分と金額の欄が合わない行（「脱着」「板金」等の行に部品代）は読み取りのずれの疑い。
                # 金額は動かさずに知らせる（以前は消す／工賃へ移していた。O6）
                for _ac in (estimate_data.get('_amount_changes') or []):
                    info_msgs.append("⚠️ 要確認（金額は原本の読み取りどおり）: " + _ac)
                if not vehicle_bytes:
                    info_msgs.append("📋 車検証なしモード: 見積書から読み取れた車両情報のみでNEOを作成します。ステップ③で車両情報を確認・補完してください。")
                if not estimate_data.get('_addata_matched'):
                    info_msgs.append("✏️ そのまま転記: 見積の全明細をそのままNEOファイルに転記します（Addata照合なし）")
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
            elif _is_quota_error(err_str):
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
            rev_icon = ('— 照合なし（CSV の金額をそのまま使います）' if estimate_data.get('_csv_import')
                        else ('— ベタ打ちの結果の取り込み（見積書の小計と下で照合）' if estimate_data.get('_preview_import')
                              else ('✅ 逆算一致' if rev_match else '⚠️ 逆算不一致（金額を確認してください）')))
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
            # 住所は書く前に 都道府県 / 市区郡 / 以降 に分け直す（政令市の区は以降側へ）。警告も分け直した後の値で見る
            # （以前は入力のままで見ていて、以降側で切れる部屋番号を知らせなかった。バグハント 3 回目 L6）
            _w_pref, _w_muni, _w_addr = v_pref, v_muni, v_addr
            if any(str(x or '').strip() for x in (v_pref, v_muni, v_addr)):
                _w_pref, _w_muni, _w_addr = _doc_hints.split_address(
                    _strip_control_chars(v_pref), _strip_control_chars(v_muni), _strip_control_chars(v_addr))
            for _lbl, _val, _w in (
                ('使用者名',       v_customer,   _CUST_WIDTH['UserName']),
                ('所有者名',       v_owner,      _CUST_WIDTH['OwnerName']),
                ('郵便番号',       v_postal,     _CUST_WIDTH['PostalNo']),
                ('都道府県',       _w_pref,      _CUST_WIDTH['Prefecture']),
                ('市区町村',       _w_muni,      _CUST_WIDTH['Municipality']),
                ('その他住所',     _w_addr,      _CUST_WIDTH['AddressOther1']),
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
                # 列幅の比較は「cp932 に直した全文」と。「〜」→「～」のように字が置き換わるだけで長さが同じものを
                # 「超えています」と誤って出していた（バグハント 3 回目 L12）
                _full = cp932_trim(_val, 10 ** 6)
                if _val and _cut != _full:
                    st.warning(
                        f"⚠️ {_lbl}はコグニセブンの列幅（{_w}バイト＝全角{_w // 2}文字）を"
                        f"超えています。NEOには「{_cut}」までしか入りません。"
                        "短い表記に直してください。")
                _bad_ch = neo_header.unencodable_chars(safe_str(_val))
                if _bad_ch:
                    st.warning(
                        f"⚠️ {_lbl}の「{'」「'.join(_bad_ch[:5])}」はコグニセブンの文字（Shift_JIS）に無いため、"
                        "NEO では「?」になります。近い字に直してください。")

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
        _s3_verdict_now = None  # ③の照合の結果（④の不一致の表示とファイル名が同じ判定を使う）
        # tab_totals 内の条件分岐に依存する変数を安全のため事前初期化
        _step3_mode       = st.session_state.get('selected_mode', 'db')
        discrepancies     = []
        total_diff        = 0
        edited_items      = []

        with tab_totals:
          if estimate_data and (estimate_data.get('items') or estimate_data.get('_csv_import') or estimate_data.get('_preview_import')):   # 全部消しても表は出す（行を足し直せる。M12）

            # ── 明細行一覧 (編集可) ──────────────────────────────
            st.markdown('<div class="section-title">📋 明細行一覧（全項目・編集可）</div>', unsafe_allow_html=True)

            _items_src = estimate_data['items']
            # ── 明細表の元の表（バグハント 3 回目 M1）──
            # Streamlit の data_editor は num_rows="dynamic" のとき、表の識別子を「渡した元の表の中身」から作る。
            # 以前は毎回、直した明細から元の表を作り直していたため、直すたびに識別子が変わって表が作り直され、
            # 続けて入れた修正が 1 つおきに捨てられていた（行の削除・追加直後の入力も同じ）。元の表はセッションに
            # 固定し、明細が表の外で変わったとき（行挿入・コピー・読み直し）だけ作り直してキーを進める
            _ed_base = st.session_state.get('_items_editor_base')
            if not (isinstance(_ed_base, dict) and _ed_base.get('out') == _items_src):
                _ed_ver_new = int(st.session_state.get('items_editor_ver', 0)) + 1
                st.session_state['items_editor_ver'] = _ed_ver_new
                _ed_base = {'items': copy.deepcopy(_items_src), 'out': None}
                st.session_state['_items_editor_base'] = _ed_base
            _base_items = _ed_base['items']
            # ── 行操作ボタン（挿入・コピー・削除） ──────────────────────
            _op_col1, _op_col2, _op_col3, _op_col4 = st.columns([1, 1, 1, 5])
            # 行挿入・コピーは押した印だけ残し、表の出力（その run の入力を含む）の後で当てる。ボタンの処理が表より先に
            # 走ると、表に入れた直後の入力が同じ run で捨てられていた（レビュー 2026-09-15）
            def _queue_row_op(_kind):
                st.session_state['_pending_row_op'] = (_kind, int(st.session_state.get('row_copy_no') or 1))
            with _op_col1:
                st.button("➕ 行挿入", key="row_insert_btn", help="最終行に空白行を追加",
                          on_click=_queue_row_op, args=('insert',))
            with _op_col2:
                # 上限は固定（行数で上限を変えると入力欄が 1 に戻り、別の行が複製されていた。M5）
                _copy_no = st.number_input("コピーNo", min_value=1, max_value=9999,
                                           value=1, step=1, key="row_copy_no", label_visibility="collapsed")
            with _op_col3:
                st.button("📋 コピー", key="row_copy_btn", help="指定No行を複製して最終行に追加",
                          on_click=_queue_row_op, args=('copy',))
            _row_op_msg = st.session_state.pop('_row_op_msg', None)
            if _row_op_msg:
                st.warning(_row_op_msg)

            # 表示用DataFrame（8列）: No / 部品番号 / 品名 / 区分 / 数量 / 部品金額 / 工数 / 工賃（元の表 _base_items から作る）
            _edit_rows = []
            for _i, _item in enumerate(_base_items):
                # この列は編集できて、編集後の値がそのまま part_no（見積書の品番）として
                # 保存される。ここに Addata 由来の品番を出すと、それが「見積書に
                # 書いてあった品番」に化けてしまい、L1/L2・金額ありの条件を迂回して
                # .neo に入る。Addata を外したあとも残る。
                # そのため画面には見積書の値だけを出し、補完が起きることは
                # 表の下の注記で伝える。
                _part_code   = str(_item.get('part_no', '') or _item.get('_master_part_no', '') or '')
                _index_value = str(_item.get('index_value', '') or '')
                _edit_rows.append({
                    'No':     _i + 1,
                    '部品番号': _part_code,
                    '品名':   str(_item.get('name', '')),
                    # 区分（修理方法）も見せて直せるようにする。帳票の「修理方法」にそのまま出る（M11）
                    '区分':   str(_item.get('work_code', '') or _item.get('method', '') or ''),
                    '数量':   qty_int(_item.get('quantity', 1), 1),
                    '部品金額': safe_int(_item.get('parts_amount', 0)),
                    '工数':   _index_value,
                    '工賃':   safe_int(_item.get('wage', 0)),
                })
            _df_edit = pd.DataFrame(_edit_rows) if _edit_rows else pd.DataFrame(
                columns=['No', '部品番号', '品名', '区分', '数量', '部品金額', '工数', '工賃'])
            # キーを行数と連動させることで行挿入後に data_editor を強制再初期化する
            # 完全な固定キーにすると、行を削除したときのフロント側の
            # 編集状態が残り、振り直した No が画面に反映されない。
            # 画面の No と「コピー」が指す行がずれ、別の行が複製される。
            # 行数が変わったときだけ作り直す（セル編集では行数は変わらない
            # ので、入力中の内容は捨てられない）。
            # 生成側は、見積書に品番が無い行を Addata の品番で補うことがある
            # （価格まで一致した L1/L2 の行だけ）。画面の「部品番号」は見積書の値
            # そのままなので、補完が起きる行は空欄に見える。その旨を伝えておく。
            if any(not str(_r.get('部品番号', '') or '') for _r in _edit_rows):
                st.caption("※ 部品番号が空欄の行は、Addata で価格まで一致した部品が"
                           "見つかればその品番を NEO に書きます（画面には出ません）。")
            # キーは元の表を作り直したときだけ進める（上。行数では進めない: 行を消した・足した直後の入力が消えていた。M1）
            _ed_ver = st.session_state.get('items_editor_ver', 0)
            _editor_key = f'items_editor_{_ed_ver}'
            # height を固定して描画行数を制限（全行フル展開すると100行超で重くなるため）
            _editor_height = min(600, max(200, len(_base_items) * 35 + 60))
            _edited_df = st.data_editor(
                _df_edit,
                width='stretch',
                hide_index=True,
                num_rows="dynamic",
                height=_editor_height,
                column_config={
                    'No':     st.column_config.NumberColumn('No', disabled=True, width='small'),
                    '部品番号': st.column_config.TextColumn('部品番号'),
                    '品名':   st.column_config.TextColumn('品名', width='large'),
                    '区分':   st.column_config.TextColumn('区分', width='small',
                                                         help='修理方法（取替・脱着・修理・板金・塗装 など）。帳票の「修理方法」欄にそのまま出ます'),
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
                _kv = _row.get('区分', '');     _kv = '' if pd.isna(_kv) else str(_kv).strip()
                # 既存行のメタデータを引き継ぐ（新規追加行はデフォルト）
                _src_idx = None
                if _i < len(_orig_no_list):
                    _no_val = _orig_no_list[_i]
                    if _no_val is not None and not pd.isna(_no_val):
                        try:
                            _src_idx = int(_no_val) - 1
                        except (TypeError, ValueError):
                            _src_idx = None
                # No は元の表（_base_items）の位置。元の表は直しても作り直さないので、No で引けば必ず同じ行
                _orig = (_base_items[_src_idx]
                         if _src_idx is not None and 0 <= _src_idx < len(_base_items)
                         else {})
                _wk   = _kv
                edited_items.append({
                    '_ed_no': (_src_idx + 1) if _orig else None,   # 画面の No（コピー・空行の知らせに使う）
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
                    # '_master_ref_no' を落とすと NEO の部品コードが空になる。
                    'match_level': _orig.get('match_level', ''),
                    '_master_ref_no': _orig.get('_master_ref_no', ''),
                    '_master_section_code': _orig.get('_master_section_code', ''),
                    # 'db_parts_no' を落とすと、見積書に品番が無い行で
                    # Addata が引き当てた品番が生成前に消え、PartsNo が空になる。
                    'db_parts_no': _orig.get('db_parts_no', ''),
                    '_match_level': _orig.get('_match_level', 0),
                    '_original_name': _orig.get('name', _nv),
                    '_original_parts_amount': _orig.get('parts_amount', safe_int(_row.get('部品金額', 0))),
                })
            estimate_data['items'] = edited_items
            # 表から出た明細の写し。次の run で明細がこれと同じなら元の表は作り直さない（M1）。写しで持つのは、行挿入・
            # コピーが明細のリストに追記するため（同じリストを指していると表の外の変化に気づけない）
            _ed_base['out'] = copy.deepcopy(edited_items)
            _pending_op = st.session_state.pop('_pending_row_op', None)
            if _pending_op:
                _op_kind, _op_no = _pending_op
                if _op_kind == 'insert':
                    edited_items.append({
                        'name': '', 'method': '', 'work_code': '', 'index_value': '',
                        'quantity': 1, 'parts_amount': 0, 'wage': 0, 'part_no': '',
                        '_master_name': '', '_master_price': 0, '_master_part_no': '',
                        '_master_repair_code': '', '_master_branch_code': '',
                        '_master_part_code_r': '', '_master_part_code_l': '',
                        '_master_ref_no': '',
                        '_master_section_code': '', 'match_level': '',
                        '_match_level': 0, '_original_name': '', '_original_parts_amount': 0,
                    })
                else:
                    # No は画面の表の No（行を消しても振り直さない）で引く。位置で引くと、行を消した直後に別の行が
                    # 複製されていた（バグハント 3 回目 M5）
                    _hit = [it for it in edited_items if it.get('_ed_no') == int(_op_no)]
                    if _hit:
                        _copied = copy.deepcopy(_hit[0])
                        _copied.pop('_ed_no', None)
                        edited_items.append(_copied)
                    else:
                        st.session_state['_row_op_msg'] = f"No {int(_op_no)} の行が表にありません（消した行かもしれません）。"
                estimate_data['items'] = edited_items
                st.session_state['estimate_data'] = estimate_data
                st.rerun()
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
            # ショートパーツは印字の部品計・工賃計に含まれている見積がある。ここで先に読む（0 のままだと③の判定だけ
            # ショートパーツを見ず、④・ファイル名・差異レポートと食い違っていた。レビュー 6 周目）
            sp = safe_int((estimate_data or {}).get('short_parts_wage', 0))
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
            # 税込モードでは明細合算とPDF記載値の小差（明細行数×1円以内）も一致とみなす（旧 PDF 解析経路の名残）。プレビュー取り込みは
            # 印字と明細が同じ基準なので許容しない（1 行を 9 円打ち間違えても一致になっていた。レビュー 3 周目）
            _tol_on = bool(is_tax_incl_s3) and not estimate_data.get('_preview_import')
            _parts_tol = len(edited_items) if _tol_on else 0
            parts_match_tol = (abs(calc_parts - pdf_parts) <= _parts_tol) if pdf_parts > 0 else False
            parts_match_tol_sp = (abs(calc_parts + sp - pdf_parts) <= _parts_tol) if pdf_parts > 0 else False
            # NEO と同じ消費税の端数処理（テンプレートの設定）。生成だけ従い、画面の合計が 1 円ずれていた（レビュー 2 周目）
            _s3_round, _s3_flag = _neo_tax_round(st.session_state.get('custom_neo_bytes') or template_data)
            # 見積書の総額と NEO の総額（明細ぶん。サイドバーの費用は除く）の照合は、小計の判定より先に出す（小計の判定と
            # 「合格」の帯もこれを見る。レビュー 3 周目）。税抜で印字された総額は税込にしてから比べる
            pdf_grand = 0 if _csv_mode_s3 else safe_int(estimate_data.get('pdf_grand_total', 0))
            _sp_s3 = safe_int((estimate_data or {}).get('short_parts_wage', 0)) or sp
            _gi_s3 = bool(estimate_data.get('_grand_is_intax', True))
            _grand_mismatch_s3 = None   # 見積書の総額と合わないときの (印字, NEO の明細ぶん)
            _grand_ok_s3 = None
            if pdf_grand > 0:
                if is_tax_incl_s3:
                    _items_grand_s3 = calc_parts + calc_wages + _sp_s3 + _round_tax10(_sp_s3, _s3_round)
                else:
                    _items_grand_s3 = (calc_parts + calc_wages + _sp_s3) + _round_tax10(calc_parts + calc_wages + _sp_s3, _s3_round)
                _want_grand_s3 = pdf_grand if (is_tax_incl_s3 or _gi_s3) else pdf_grand + _round_tax10(pdf_grand, _s3_round)
                _grand_ok_s3 = (_items_grand_s3 == _want_grand_s3)
                if not _grand_ok_s3:
                    _grand_mismatch_s3 = (_want_grand_s3, _items_grand_s3)
            # 印字の部品計が値引き前（明細のマイナスの行を含まない）の書式もある（ベタ打ちの検証と同じく両方の基準で見る）
            _calc_parts_plus = sum(max(0, safe_int(_it.get('parts_amount', 0))) for _it in edited_items)
            # 値引き前の一致は、値引き（マイナスの行）の合計が取り込んだときのままのとき、または見積書の総額と 1 円単位で合う
            # ときだけ（値引き −5,000 を −500 に打ち間違えても「一致」になっていた。レビュー 2 周目。読み違えた値引きを原本どおりに
            # 直すと、総額は合うのに「部品相違」になっていた。レビュー 3 周目）
            _neg_imp = estimate_data.get('_neg_at_import')
            _neg_same_p = (_neg_imp is None or bool(_grand_ok_s3)
                           or sum(min(0, safe_int(_it.get('parts_amount', 0))) for _it in edited_items) == safe_int(_neg_imp[0]))
            _neg_same_w = (_neg_imp is None or bool(_grand_ok_s3)
                           or sum(min(0, safe_int(_it.get('wage', 0))) for _it in edited_items) == safe_int(_neg_imp[1]))
            wage_diff = calc_wages - pdf_wages if pdf_wages > 0 else 0
            # SP込みでも一致チェック（工賃）: Honda Cars等でSPが工賃列に含まれる場合
            wage_match_sp = (calc_wages + sp == pdf_wages) if pdf_wages > 0 else False
            _wages_tol = len(edited_items) if _tol_on else 0
            wage_match_tol = (abs(calc_wages - pdf_wages) <= _wages_tol) if pdf_wages > 0 else False
            wage_match_tol_sp = (abs(calc_wages + sp - pdf_wages) <= _wages_tol) if pdf_wages > 0 else False
            # ショートパーツを足して初めて一致した側（片方にしか入らない。両方で一致するのは読み違いなので、どちらも一致としない。
            # レビュー 7 周目）
            _parts_by_sp = bool(sp) and pdf_parts > 0 and calc_parts != pdf_parts and (parts_match_sp or parts_match_tol_sp)
            _wage_by_sp = bool(sp) and pdf_wages > 0 and calc_wages != pdf_wages and (wage_match_sp or wage_match_tol_sp)
            if _parts_by_sp and _wage_by_sp:
                parts_match_sp = parts_match_tol_sp = wage_match_sp = wage_match_tol_sp = False
                _parts_by_sp = _wage_by_sp = False
            parts_match = ((calc_parts == pdf_parts) or parts_match_sp or parts_match_tol or parts_match_tol_sp
                           or (pdf_parts > 0 and _neg_same_p and _calc_parts_plus != calc_parts and _calc_parts_plus == pdf_parts))
            _calc_wages_plus = sum(max(0, safe_int(_it.get('wage', 0))) for _it in edited_items)
            wage_match = ((calc_wages == pdf_wages) or wage_match_sp or wage_match_tol or wage_match_tol_sp
                          or (pdf_wages > 0 and _neg_same_w and _calc_wages_plus != calc_wages and _calc_wages_plus == pdf_wages))
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
                    st.markdown('<div class="success-box">✅ PDF金額と一致'
                                + ('（ショートパーツ込み）' if _parts_by_sp else '') + '</div>', unsafe_allow_html=True)
            with scol2:
                st.metric(f"工賃合計（{tax_label_sfx}）", f"¥{calc_wages:,}")
                if pdf_wages > 0 and not wage_match and not rev_match:
                    has_discrepancy = True
                    st.markdown(
                        f'<div class="error-box">⚠️ <b>工賃相違</b>: PDF ¥{pdf_wages:,} ≠ 計算 ¥{calc_wages:,}（差額: {wage_diff:+,}円）</div>',
                        unsafe_allow_html=True
                    )
                elif pdf_wages > 0:
                    st.markdown('<div class="success-box">✅ PDF金額と一致'
                                + ('（ショートパーツ込み）' if _wage_by_sp else '') + '</div>', unsafe_allow_html=True)
            with scol3:
                exp_tow = st.session_state.get('exp_towing', 0)
                exp_ren = st.session_state.get('exp_rental', 0)
                exp_exm = st.session_state.get('exp_exempt', 0)
                if estimate_data.get('_expenses_off'):
                    # ベタ打ちで「費用を入れない」を選んでいた見積（見積書に同じ費用が載っている等）は、ここでも足さない（O9）
                    if any(safe_int(x) for x in (exp_tow, exp_ren, exp_exm)):
                        st.caption('サイドバーの費用はベタ打ちで入れない選択だったので、NEO に入れません')
                    exp_tow = exp_ren = exp_exm = 0
                # ショートパーツを合計に含める（0のままだと画面だけ少なくなる）
                sp = safe_int((estimate_data or {}).get('short_parts_wage', 0)) or sp
                sub = calc_parts + calc_wages + sp + exp_tow + exp_ren
                if is_tax_incl_s3:
                    # 税込モード: 明細金額は既に税込。ただし費用欄は「税抜」で
                    # 入力させているため、費用ぶんの消費税は別に足す。
                    # これを忘れると画面の合計とNEOの合計が食い違う。
                    tax   = _round_tax10(sp + exp_tow + exp_ren, _s3_round)
                    total = sub + tax + exp_exm
                else:
                    tax   = _round_tax10(sub, _s3_round)
                    total = sub + tax + exp_exm
                st.metric("合計（税込）", f"¥{total:,}")
                if _s3_flag != 1:
                    st.caption(f'消費税の端数はテンプレートの設定（{_s3_round}）で計算します')
                # ここには以前、「この税込額は .neo では作れないので合計が
                # 1円変わります」という断りを出していた。
                # **その前提が実機データで否定されたので消した。**
                # 実機が作った .neo 176件のうち10件（約6%）は税額が税抜の
                # 10%ちょうどではなく、コグニは書かれた税額をそのまま持つ。
                # いまは税込表記のとき税額を「原本の税込 − 逆算した税抜」で
                # 書いており、**.neo の総額は原本とぴったり一致する**。
                # 断りを残すと、ずれないものを「ずれる」と伝えることになる。
                if estimate_data.get('_csv_import'):
                    st.caption('照合なし: CSV の金額をそのまま使います（見積書との照合はしていません）')
                elif rev_match:
                    st.markdown('<div class="success-box">✅ 逆算一致</div>', unsafe_allow_html=True)

            # ── STEP 3 バリデーション結果パネル ──
            # parts_match / wage_match はこの直上でリアルタイム再計算済みの値を使用する
            # (estimate_data['totals_verification'] は解析時の古い判定のため使わない)
            _tv_has_data = (pdf_parts > 0 or pdf_wages > 0)
            if _tv_has_data:
                _tv_p_mismatch = pdf_parts > 0 and not parts_match and not rev_match
                _tv_w_mismatch = pdf_wages > 0 and not wage_match and not rev_match
                _tv_unv = [_n for _n, _v in (('部品計', pdf_parts), ('工賃計', pdf_wages)) if _v <= 0]
                if not _tv_p_mismatch and not _tv_w_mismatch and _grand_mismatch_s3:
                    # 小計は合うが総額が合わない（値引きの行など）。「合格」とは言わない（レビュー 3 周目）
                    st.markdown(
                        '<div class="warning-box" style="padding:10px 16px;margin-bottom:12px">'
                        '⚠️ <b>【STEP 3】</b> 小計は見積書の印字と一致していますが、見積書の総額と NEO の総額が合いません'
                        '（下の検証表と注意を確かめてください）。</div>', unsafe_allow_html=True)
                elif not _tv_p_mismatch and not _tv_w_mismatch and (_parts_by_sp or _wage_by_sp):
                    # ショートパーツを足して一致した（明細の合算そのものは違う。レビュー 7 周目）。片方が未照合でもこちらを先に
                    # 出す（未照合の文だけになり、ショートパーツに触れなかった。レビュー 8 周目）
                    st.markdown(
                        '<div class="success-box" style="padding:10px 16px;margin-bottom:12px">'
                        f'✅ <b>【STEP 3】</b> 見積書の{"部品計" if _parts_by_sp else "工賃計"}は、明細の合算に'
                        f'ショートパーツ ¥{sp:,} を足すと一致します（明細の合算そのものは ¥{sp:,} 少ない額です）'
                        + (f'。{"・".join(_tv_unv)}は見積書から読めず、照合していません' if _tv_unv else '')
                        + '。</div>', unsafe_allow_html=True)
                elif not _tv_p_mismatch and not _tv_w_mismatch and _tv_unv:
                    # 片方しか読めていない: 読めた側だけの一致（工賃が未照合でも「合格」と出ていた。レビュー 2 周目）
                    st.markdown(
                        '<div class="success-box" style="padding:10px 16px;margin-bottom:12px">'
                        f'✅ <b>【STEP 3】</b> 読めた小計は見積書の印字と一致しています（{"・".join(_tv_unv)}は見積書から読めず、照合していません）。'
                        '</div>', unsafe_allow_html=True)
                elif not _tv_p_mismatch and not _tv_w_mismatch:
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

                # 合算値の一致確認。見積書から読めなかった項目（印字が 0）は「未照合」と出し、合否にも一致率にも数えない
                # （部品計が読めないのに合格扱い・総額だけの照合で「全項目一致（一致率 33%）…完全一致」と出ていた。レビュー 2 周目）
                _verify_items = []
                parts_ok = parts_match or rev_match
                _verify_items.append(('部品合計', pdf_parts, calc_parts, parts_ok if pdf_parts > 0 else None))
                wage_ok = wage_match or rev_match
                _verify_items.append(('工賃合計', pdf_wages, calc_wages, wage_ok if pdf_wages > 0 else None))
                if pdf_grand > 0 and _grand_ok_s3 is not None:
                    # 見積書の総額は、NEO と同じ計算（明細ぶん。サイドバーの費用は除く・消費税の端数はテンプレートの設定）で
                    # 1 円単位で比べる。以前は最大 50 円の許容で表示するだけで、値引きの打ち間違いも確認なしで生成できた
                    # （計算は小計の判定の前。レビュー 2・3 周目）
                    _verify_items.append(('見積合計（税込）' if (is_tax_incl_s3 or _gi_s3) else '見積合計（税抜の印字を税込に）',
                                          _want_grand_s3, _items_grand_s3, _grand_ok_s3))
                for _vi in _verify_items:
                    if _vi[3] is False:
                        _beta_all_ok = False

                verify_html = '<table style="width:100%;border-collapse:collapse;font-size:13px;margin:8px 0">'
                verify_html += '<tr style="background:#f1f5f9;font-weight:600"><td style="padding:6px 10px">検証項目</td><td style="padding:6px 10px;text-align:right">PDF記載</td><td style="padding:6px 10px;text-align:right">計算値</td><td style="padding:6px 10px;text-align:center">結果</td></tr>'
                for v_label, v_pdf, v_calc, v_ok in _verify_items:
                    if v_ok is None:
                        v_icon, v_color, v_pdf_txt, v_diff_text = '—', '#64748b', '読めず（未照合）', ''
                    else:
                        v_icon = '✅' if v_ok else '❌'
                        v_color = '#16a34a' if v_ok else '#dc2626'
                        v_pdf_txt = f'¥{v_pdf:,}'
                        v_diff_text = f' ({v_calc - v_pdf:+,}円)' if not v_ok else ''
                    verify_html += f'<tr style="border-bottom:1px solid #e2e8f0"><td style="padding:6px 10px">{v_label}</td><td style="padding:6px 10px;text-align:right">{v_pdf_txt}</td><td style="padding:6px 10px;text-align:right">¥{v_calc:,}{v_diff_text}</td><td style="padding:6px 10px;text-align:center;color:{v_color};font-weight:600">{v_icon}</td></tr>'
                verify_html += '</table>'
                st.markdown(verify_html, unsafe_allow_html=True)

                _checked = [_v for _v in _verify_items if _v[3] is not None]
                _unverified = [_v[0] for _v in _verify_items if _v[3] is None]
                # 比較相手（見積書に印字された部品計・工賃計・総合計）が 1 つも取れていないときは、何も突き合わせていない。
                # CSV取り込みは常にこれに当たる。「一致」と断言すると、利用者が原本との突き合わせを打ち切る根拠になる
                if not _checked:
                    _what = '・'.join(_unverified[:2]) if _unverified else '照合の基準'
                    _note = '（CSV取り込みでは常にこの状態です）' if estimate_data.get('_csv_import') else ''
                    st.markdown(
                        '<div class="warning-box" style="padding:10px 16px;margin:8px 0">'
                        f'ℹ️ <b>{_what}を見積書から読み取れていないため、'
                        f'この項目は検証していません</b>{_note}。'
                        'アプリ側で突き合わせる相手がないので、'
                        '下の明細と金額を、原本とご自身で突き合わせてください。</div>',
                        unsafe_allow_html=True)
                elif _beta_all_ok:
                    _uv_txt = f'（{"・".join(_unverified)}は見積書から読めず、照合していません）' if _unverified else ''
                    st.markdown(f'<div class="success-box" style="padding:10px 16px;margin:8px 0">✅ <b>ベタ打ち検証: 照合した {len(_checked)} 項目はすべて一致</b>{_uv_txt} — 見積書の印字と 1 円の差もありません。</div>', unsafe_allow_html=True)
                else:
                    _n_ok = sum(1 for _v in _checked if _v[3])
                    st.markdown(f'<div class="error-box" style="padding:10px 16px;margin:8px 0">⚠️ <b>ベタ打ち検証: 不一致あり（照合した {len(_checked)} 項目のうち {_n_ok} 項目が一致）</b> — 見積書との差を確かめてください。協定見積は 1 円でも違うと使えません。</div>', unsafe_allow_html=True)

            # ── ベタ打ちモード専用: 部品・工賃区分確認パネル ──────────────────────────
            _classification_alerts = []
            _classification_confirmed = True
            _error_alerts = []
            if _step3_mode == 'beta':
                # アイテムが変わった時だけ再計算（session_stateでキャッシュ）
                _items_hash = hash(str([(it.get('name',''), it.get('parts_amount',0), it.get('wage',0),
                                         it.get('work_code', '') or it.get('method', '')) for it in edited_items]))
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
                        'NEOの部品コード': d.get('_master_ref_no', ''),
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

            # ③の照合の結果。④の不一致の表示とファイル名の「（部品相違）」などは、これと同じ判定を使う（④は完全一致だけを見て、
            # 値引き前の小計の見積で正しい NEO に「（工賃相違）」の名前と不一致の警告を付けていた。レビュー 3 周目）
            _s3_verdict_now = {
                'parts_ok': not (pdf_parts > 0 and not parts_match and not rev_match),
                'wage_ok': not (pdf_wages > 0 and not wage_match and not rev_match),
                'grand_ok': _grand_mismatch_s3 is None,
                'grand_diff': (_grand_mismatch_s3[1] - _grand_mismatch_s3[0]) if _grand_mismatch_s3 else 0,
                'grand_want': _grand_mismatch_s3[0] if _grand_mismatch_s3 else 0,
                'grand_neo': _grand_mismatch_s3[1] if _grand_mismatch_s3 else 0,
                # 値引き前の小計（マイナスの行を含まない合算）で一致としたか（差異レポートの判定の書き分け。レビュー 5 周目）
                'parts_gross': bool(pdf_parts > 0 and calc_parts != pdf_parts and _calc_parts_plus == pdf_parts and parts_match),
                'wage_gross': bool(pdf_wages > 0 and calc_wages != pdf_wages and _calc_wages_plus == pdf_wages and wage_match),
                # ショートパーツを足して初めて一致した／逆算一致で小計の相違を出していない（レポートの判定の書き分け。レビュー 7 周目）
                'parts_sp': bool(_parts_by_sp), 'wage_sp': bool(_wage_by_sp),
                'rev': bool(rev_match),
            }
            # 確認のチェックは、確かめた差（小計の差・総額の差）が変わったら外す（小計の差で入れたチェックが、あとで総額の差に
            # 変わっても残り、確認なしで生成できた。レビュー 3 周目）。ウィジェットを作る前に消す
            # 差の値が同じまま、どの小計が合っているか（③の判定）だけ変わったときも外す（レビュー 5 周目）
            _disc_sig = (parts_diff if has_discrepancy else 0, wage_diff if has_discrepancy else 0,
                         (_grand_mismatch_s3[1] - _grand_mismatch_s3[0]) if _grand_mismatch_s3 else 0,
                         _s3_verdict_now['parts_ok'], _s3_verdict_now['wage_ok'], _s3_verdict_now['grand_ok'],
                         _s3_verdict_now['parts_sp'], _s3_verdict_now['wage_sp'])
            if st.session_state.get('_amount_confirmed_sig') != _disc_sig:
                st.session_state['_amount_confirmed_sig'] = _disc_sig
                # 代入して外す（pop はサーバーの値だけを消し、ブラウザのチェックは入ったまま、次の再描画で True が送り直されて
                # いた。レビュー 4 周目）。value=False のチェックなので既定値の二重指定の警告は出ない
                st.session_state['amount_confirmed'] = False
            # 見積書の総額が読めない見積では、ショートパーツを足して一致とした小計を裏づけるものが無い（工賃の行を 1 行落として
            # いても同じ数字になる）。確認のチェックを出す（6 周目まで「相違」として止めていた形が無警告で通っていた。レビュー 7 周目）
            _sp_unverified = (_grand_ok_s3 is None) and (_parts_by_sp or _wage_by_sp)
            DISCREPANCY_THRESHOLD = 1000
            if _sp_unverified and not has_discrepancy and not _grand_mismatch_s3:
                st.warning(f"⚠️ 見積書の{'部品計' if _parts_by_sp else '工賃計'}（¥{(pdf_parts if _parts_by_sp else pdf_wages):,}）は、"
                           f"明細の合算にショートパーツ ¥{sp:,} を足すと一致します。見積書の総額が読めていないので、"
                           "ショートパーツがその計に含まれているのか、明細の行が落ちているのかを確かめてください。")
                amount_confirmed = st.checkbox(
                    "見積書と突き合わせました。このまま生成を続行します。",
                    value=False,
                    key='amount_confirmed'
                )
            elif _grand_mismatch_s3 and not has_discrepancy:
                # 小計は合う（または読めていない）が、見積書の総額と NEO の総額（明細ぶん）が合わない（レビュー 2 周目）
                _gw, _gn = _grand_mismatch_s3
                st.warning(f"⚠️ 見積書の総額 ¥{_gw:,} と、NEO の総額（明細ぶん） ¥{_gn:,} が合いません（差 {_gn - _gw:+,} 円）。"
                           "協定見積は 1 円でも違うと使えません。明細（値引きの行を含む）を確かめてください。")
                amount_confirmed = st.checkbox(
                    "金額の差異を確認しました。このまま生成を続行します。",
                    value=False,
                    key='amount_confirmed'
                )
            elif has_discrepancy:
                if abs(parts_diff) >= DISCREPANCY_THRESHOLD or abs(wage_diff) >= DISCREPANCY_THRESHOLD:
                    st.markdown(
                        '<div class="mismatch-banner">'
                        '<div class="mismatch-title">🚨 金額不一致警告</div>'
                        '<div class="mismatch-body">AI読み取り金額とPDF記載金額に大きな差があります。<br>'
                        '明細行の内容を確認・修正してから生成してください。</div>'
                        '</div>',
                        unsafe_allow_html=True
                    )
                else:
                    # 差が小さくても確認を求める（協定見積は 1 円でも違うと使えない。以前は 1,000 円未満なら確認なしで進めた。O8）
                    st.warning("⚠️ 見積書の小計と明細の合計が合いません（協定見積は 1 円でも違うと使えません）。"
                               + (f"見積書の総額とも {_grand_mismatch_s3[1] - _grand_mismatch_s3[0]:+,} 円違います。" if _grand_mismatch_s3 else '')
                               + "明細を確かめてください。")
                amount_confirmed = st.checkbox(
                    "金額の差異を確認しました。このまま生成を続行します。",
                    value=False,
                    key='amount_confirmed'
                )
            else:
                amount_confirmed = True
            if estimate_data.get('_preview_needs_ack'):
                # ベタ打ちの結果で確認が必要だった（金額調整の行・検証の差・金額列の補正・未照合・ページ境界の統合）もの。
                # 取り込んでも確認は外さない（以前は取り込むと確認のチェックが消えていた。レビュー）
                st.warning("⚠️ 取り込んだベタ打ちの結果には、原本と差がある可能性がありました（金額調整の行／検証の差／金額列の補正／"
                           "見積書の合計が読めず未照合 など）。明細を原本と突き合わせてください。")
                _pv_ok = st.checkbox("原本と突き合わせました（このまま NEO に進む）", value=False, key='preview_ack_confirmed')
                amount_confirmed = bool(amount_confirmed) and bool(_pv_ok)
            # 「※金額調整」の行（読み取った明細と見積書の合計の差を埋めた行）は原本に無い。確かめてから（O9）
            _adj_nos = [str(_it.get('_ed_no') or _ai) for _ai, _it in enumerate(edited_items, 1)
                        if str(_it.get('name', '') or '').startswith('※金額調整')]
            if _adj_nos:
                st.warning(f"⚠️ No {', '.join(_adj_nos)} は、読み取った明細と見積書の合計の差を埋めた「※金額調整」の行で、"
                           "原本にはありません。原本の明細を確かめて直すか、行を削除してください。")
                _adj_ok = st.checkbox("「※金額調整」の行を確認しました（このまま NEO に入れる）", value=False, key='adj_rows_confirmed')
                amount_confirmed = bool(amount_confirmed) and bool(_adj_ok)

            # ── Total strip ──
            # sp はここでも同じ値を読み直すだけ（③の頭で読んでいる）
            # ショートパーツぶん少なく表示されていた（short_parts_wage の
            # 定義はこの後なので estimate_data から直接読む）
            sp = safe_int((estimate_data or {}).get('short_parts_wage', 0)) or sp
            _exp_off_strip = bool((estimate_data or {}).get('_expenses_off'))   # 費用を入れない選択（ステップ④と同じ）
            _exp_tow_s4 = 0 if _exp_off_strip else st.session_state.get('exp_towing', 0)
            _exp_ren_s4 = 0 if _exp_off_strip else st.session_state.get('exp_rental', 0)
            sub   = calc_parts + calc_wages + sp + _exp_tow_s4 + _exp_ren_s4
            _is_tax_incl_strip = (estimate_data.get('_is_tax_inclusive', False) if estimate_data else False)
            if _is_tax_incl_strip:
                # 費用欄は「税抜」入力なので、税込モードでも費用ぶんの税は加算する
                tax   = _round_tax10(sp + _exp_tow_s4 + _exp_ren_s4, _s3_round)
                total = sub + tax + (0 if _exp_off_strip else st.session_state.get('exp_exempt', 0))
            else:
                tax   = _round_tax10(sub, _s3_round)   # NEO と同じ端数処理（テンプレートの設定。レビュー 2 周目）
                total = sub + tax + (0 if _exp_off_strip else st.session_state.get('exp_exempt', 0))
            # 費用（レッカー・代車・非課税）は合計に加算されるのに画面に
            # 出ていなかったため、部品代＋工賃＋消費税と合計が一致せず
            # 「計算が合っていない」ように見えていた。金額がある時だけ表示する。
            _exp_sum_strip = 0 if _exp_off_strip else (st.session_state.get('exp_towing', 0)
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
            if st.button("← ステップ①に戻る", width='stretch'):
                st.session_state['step'] = 1
                st.session_state['vehicle_data']  = None
                st.session_state['estimate_data'] = None
                st.session_state.pop('_s3_verdict', None)   # ③の判定も消す（次の案件に持ち越さない。レビュー 9 周目）
                st.rerun()
            # 戻ると明細表と車両情報の修正は消える（CSV を読み直す）。黙って消さない（M9）
            st.caption("※ ステップ①に戻ると、ここで直した明細と車両情報は消えます（CSV から読み直します）")
        with bcol2:
            # 金額差異未確認時のみボタンを無効化（分類エラーではブロックしない）
            gen_disabled = not amount_confirmed
            _blank_nos = [str(_it.get('_ed_no') or _bi) for _bi, _it in enumerate(edited_items or [], 1)
                          if not str(_it.get('name', '') or '').strip()]
            _all_deleted = bool(estimate_data) and (estimate_data.get('_csv_import') or estimate_data.get('_preview_import')) \
                and not (estimate_data.get('items') or [])
            if st.button("📦 NEOファイルを生成する →", type="primary", width='stretch', disabled=gen_disabled):
                if _all_deleted:
                    st.error("❌ 明細が 0 行です（表の行を全部消しています）。ステップ①に戻って取り込み直してください。")
                elif _blank_nos:
                    # 品名の無い行は協定見積に出せない（行挿入の直後の空行など。以前はそのまま NEO に入った。M10）
                    st.error(f"❌ 品名が空の行があります（No {', '.join(_blank_nos)}）。品名を入れるか、行を削除してから生成してください。")
                else:
                    st.session_state['updated_vehicle'] = updated_vehicle
                    st.session_state['calc_parts']      = calc_parts
                    st.session_state['calc_wages']      = calc_wages
                    st.session_state['pdf_parts']       = pdf_parts if estimate_data else None
                    st.session_state['pdf_wages']       = pdf_wages if estimate_data else None
                    st.session_state['discrepancies']   = discrepancies
                    st.session_state['total_diff']      = total_diff
                    st.session_state['_s3_verdict']     = _s3_verdict_now
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
            'adjuster_post':    st.session_state.get('adjuster_post', ''),
            'factory_name':     st.session_state.get('factory_name', ''),
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
        if (estimate_data or {}).get('_expenses_off'):
            # ベタ打ちで「費用を入れない」を選んでいたプレビュー取り込みは、ここでも入れない（ステップ③の合計と同じ。O9）
            expense_info = {'towing': 0, 'rental_car': 0, 'tax_exempt': 0}
        items            = []
        short_parts_wage = 0
        has_estimate     = False
        reverse_match    = False
        if estimate_data and estimate_data.get('items'):
            items            = estimate_data['items']
            # step3 で車両情報や明細を直していると、step2 で取った照合結果
            # （部品コード・照合レベル）が修正前のままになる。修正後の車両で
            # NEO を作るのに部品コードだけ修正前の車種のもの、という食い違いを
            # 防ぐため、生成の直前にもう一度照合し直す。
            # 二重に「※」が付くことは、品名側（startswith('※')）と
            # 品番側（auto_matching の二重付与防止）の両方で防がれている。
            apply_addata_matching(estimate_data, updated_vehicle)
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
            _s3v = st.session_state.get('_s3_verdict') or {}
            filename = generate_filename(
                updated_vehicle, calc_parts, calc_wages, pdf_parts, pdf_wages,
                has_estimate, reverse_match, short_parts_wage,
                parts_ok=bool(_s3v.get('parts_ok')), wage_ok=bool(_s3v.get('wage_ok')),
                grand_ok=_s3v.get('grand_ok', True) is not False,
            )
            # step4 は再描画のたびにこのブロックを通る。登録番号が無い案件は
            # ファイル名に日時が入るため、そのままだと同じ見積なのに秒が変わって
            # 名前がぶれ、画面の表示とダウンロードされる名前が食い違う。
            # 中身（車両・明細数・金額）が同じ間は、最初に決めた名前を使い回す。
            _name_key = (
                safe_str(updated_vehicle.get('car_reg_department', '')),
                safe_str(updated_vehicle.get('car_reg_division', '')),
                safe_str(updated_vehicle.get('car_reg_business', '')),
                safe_str(updated_vehicle.get('car_reg_serial', '')),
                safe_str(updated_vehicle.get('car_name', '')),
                len(items), calc_parts, calc_wages,
            )
            if (st.session_state.get('neo_filename') and st.session_state.get('_neo_name_base')
                    and st.session_state.get('_neo_name_key') == _name_key):
                # 使い回すのは日時の入った前半だけ。末尾の「（部品相違）」などは③の判定で毎回つけ直す（名前ごと使い回して、
                # 取り込み直した正しい NEO に前の「（総額相違）」が残り、直して作った誤った NEO に印が付かなかった。レビュー 4 周目）
                filename = st.session_state['_neo_name_base'] + '_見積' + filename.rsplit('_見積', 1)[1]
            st.session_state['_neo_name_base'] = filename.rsplit('_見積', 1)[0]
            st.session_state['_neo_name_key'] = _name_key
            st.session_state['neo_bytes']    = neo_data
            st.session_state['neo_filename'] = filename
            progress.progress(100, text="✅ 生成完了！")
            _neo_wait.empty()  # 待機メッセージを消去

            # 不一致チェック
            _discrepancies_step4 = []
            _total_diff_step4 = 0
            if has_estimate and not reverse_match:
                # ③と同じ判定（値引き前の小計・ショートパーツ込み・総額の一致を含む。レビュー 3・8 周目）
                parts_match_s4 = (calc_parts == pdf_parts) or bool(_s3v.get('parts_ok'))
                if pdf_parts is not None and pdf_parts > 0 and not parts_match_s4:
                    _discrepancies_step4.append(f"部品相違（PDF: ¥{pdf_parts:,} / 計算: ¥{calc_parts:,}）")
                    _total_diff_step4 += calc_parts - pdf_parts
                wage_match_s4 = (calc_wages == pdf_wages) or bool(_s3v.get('wage_ok'))
                if pdf_wages is not None and pdf_wages > 0 and not wage_match_s4:
                    _discrepancies_step4.append(f"工賃相違（PDF: ¥{pdf_wages:,} / 計算: ¥{calc_wages:,}）")
                    _total_diff_step4 += calc_wages - pdf_wages
            # 総額相違は逆算一致の外で見る（③が確認を求めた相違が④に出ていなかった。レビュー 6 周目）
            if _s3v.get('grand_ok') is False:
                _discrepancies_step4.append(f"総額相違（見積書の総額と NEO の総額が {safe_int(_s3v.get('grand_diff')):+,} 円違うまま、確認して生成）")
                if not _total_diff_step4:
                    _total_diff_step4 = safe_int(_s3v.get('grand_diff'))

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
                # 「明細の合算」の欄には明細の合算をそのまま出す（ショートパーツを足した値を入れていたため、④とファイル名が
                # 「工賃相違」と言っている行にレポートだけ「一致」と書いていた。一致の理由は判定の欄に出る。レビュー 6 周目）
                beta_pdf_bytes = generate_beta_discrepancy_report_pdf(estimate_data, calc_parts, calc_wages, pdf_parts or 0, pdf_wages or 0, updated_vehicle,
                                                                      verdict=_s3v)
                st.download_button(
                    label="📄 差異レポートをダウンロード(PDF)",
                    data=beta_pdf_bytes,
                    file_name=filename.replace('.neo', '_ベタ打ち差異レポート.pdf'),
                    mime="application/pdf",
                    width='stretch',
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
                if (estimate_data or {}).get('_csv_import'):
                    summary_items.append(("金額検証", "照合なし（CSV の金額のまま）"))
                elif reverse_match:
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
                    width='stretch'
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
                        width='stretch'
                    )
                else:
                    st.button("📄 差額なし (PDF生成不要)", disabled=True, width='stretch')
            
            st.markdown("")
            # 生成した後で直したいとき（以前は「新しい見積を作成する」＝全消去しか無かった。M12）
            if st.button("← ステップ③に戻って直す", width='stretch', key='back_to_step3_after_gen'):
                st.session_state['step'] = 3
                st.rerun()
            if st.button("🔄 新しい見積を作成する", width='stretch'):
                for key in [
                    'step', 'vehicle_data', 'estimate_data', 'neo_bytes', 'neo_filename',
                    'vehicle_file_bytes', 'vehicle_file_name', 'estimate_file_bytes',
                    'estimate_file_name', 'updated_vehicle', 'calc_parts', 'calc_wages',
                    'pdf_parts', 'pdf_wages',
                    # 事故・保険情報
                    'policy_no', 'contractor_name', 'accept_no', 'accident_date',
                    'agency_name', 'adjuster_name', 'adjuster_post', 'factory_name', 'garage_in_date', 'garage_out_date',
                    'repair_days', 'note1', '_beta_use_exp_val', '_p2n_reader_label',
                    # 添付の書類の読み取り（車検証・事故/保険の書類）の控え。残すと次の案件に前の値が付く／同じ書類を入れ直しても埋まらない
                    '_doc_ocr_cache', '_insdoc_applied', '_insdoc_sha', '_insdoc_cleared_sha', '_insdoc_filled', '_insdoc_ocr_id',
                    '_doc_ocr_error', '_beta_exp_file_key',
                    # 見積書の同一性の控え（残すと次の案件で書類を先に入れたときに「見積書が変わった」扱いで消される。Codex 69）
                    '_p2n_last_file_key', '_p2n_last_docs_sig', '_p2n_reset_msg',
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
                    'discrepancies', 'total_diff', '_s3_verdict', '_amount_confirmed_sig',
                    'amount_confirmed',
                    # CSV取り込み関連
                    'csv_mode', 'csv_items', '_csv_paste_saved',
                    # PDF→NEO変換関連
                    'pdf2neo_result', 'pdf2neo_vehicle_info', '_pdf2neo_filename',
                    'pdf2neo_preview_meta', '_items_editor_base', 'adj_rows_confirmed',
                    '_neo_name_key', '_neo_name_base',
                    # その他の残留データ
                    'use_fax_filter', 'use_rasterize', 'use_enhance', 'selected_model',
                    'short_parts_wage',
                ]:
                    if key in st.session_state:
                        del st.session_state[key]
                # サイドバー入力のウィジェットを作り直して確実に空にする
                st.session_state['form_seq'] = st.session_state.get('form_seq', 0) + 1
                # 車検証・事故/保険の書類の uploader も作り直す（前の案件の書類を持ち越さない）
                st.session_state['upload_seq'] = int(st.session_state.get('upload_seq', 0)) + 1
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
    _consume_sidebar_rerun()   # サイドバーで頼まれた描き直し（本文の uploader を描き終えてから）

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
