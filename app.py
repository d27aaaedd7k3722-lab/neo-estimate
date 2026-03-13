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
import zlib
import sqlite3
import tempfile
import os
import datetime
import json
import base64
import io
import re
import math
import traceback
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 定数・設定
# ============================================================
SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILENAME = "テンプレート_トヨタ汎用_.neo"
TEMPLATE_PATH     = os.path.join(SCRIPT_DIR, TEMPLATE_FILENAME)
TAX_RATE          = 0.10
GEMINI_API_KEY    = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL      = "gemini-3.1-pro-preview"
CONFIDENCE_THRESHOLD = 0.6
SELF_CORRECTION_THRESHOLD = 500   # 差額(円)がこれ以上の場合に自己修復を実行

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
    """全角カタカナを半角カタカナに変換（部品名用）"""
    if not text:
        return text
    return ''.join(FULL_TO_HALF_KANA.get(ch, ch) for ch in text)


def datetime_to_dos(dt):
    """Python datetime → DOS日時バイト列(4B)"""
    dos_date = ((dt.year - 1980) << 9) | (dt.month << 5) | dt.day
    dos_time = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
    return struct.pack('<HH', dos_date, dos_time)


def get_era_info(date_str):
    """YYYYMMDD文字列 → (和暦名, 和暦年4桁ゼロ埋め)"""
    if not date_str or len(date_str) < 4 or date_str == '00000000':
        return '令和', '0000'
    year = int(date_str[:4])
    if year >= 2019:
        return '令和', f'{year - 2018:04d}'
    elif year >= 1989:
        return '平成', f'{year - 1988:04d}'
    elif year >= 1926:
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
        return int(round(val))
    s = str(val).strip()
    # 単位除去
    s = re.sub(r'[個本枚セット台式時間]$', '', s)
    s = re.sub(r'[円¥,，\s]', '', s)
    s = re.sub(r'[^\d.\-]', '', s)
    if not s or s == '-':
        return default
    try:
        return int(round(float(s)))
    except (ValueError, OverflowError):
        return default


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


def replace_xml_tag(text, tag_name, value):
    """XMLタグの中身を現在値に関係なく置換"""
    pattern = rf'<{re.escape(tag_name)}>[^<]*</{re.escape(tag_name)}>'
    replacement = f'<{tag_name}>{value}</{tag_name}>'
    result = re.sub(pattern, replacement, text)
    # 空タグ形式も対応
    empty_pattern = rf'<{re.escape(tag_name)}/>'
    result = result.replace(empty_pattern, replacement)
    return result


def replace_ini_value(text, key, value):
    """INIキー値を確実に更新"""
    pattern = rf'^({re.escape(key)}\s*=).*$'
    replacement = rf'\g<1>{value}'
    return re.sub(pattern, replacement, text, flags=re.MULTILINE)


# ============================================================
# NEO バイナリ解析
# ============================================================

def find_real_cks(data, start=424):
    """comp_len連鎖法でCK位置を特定（偽CK除外）"""
    all_ck = []
    for i in range(start, len(data) - 1):
        if data[i] == 0x43 and data[i + 1] == 0x4B:
            all_ck.append(i)
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


def decompress_neo(data, real_ck):
    """辞書連鎖展開でrawデータを復元"""
    full_raw = b''
    for i, ck in enumerate(real_ck):
        start = ck + 2
        end   = real_ck[i + 1] - 8 if i + 1 < len(real_ck) else len(data)
        chunk = data[start:end]
        if i == 0:
            raw = zlib.decompress(chunk, -15)
        else:
            dobj = zlib.decompressobj(-15, zdict=full_raw[-32768:])
            raw  = dobj.decompress(chunk)
        full_raw += raw
    return full_raw


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

def update_ansmb(db_bytes, items, short_parts_wage, expenses=None):
    """ERParts/Expense/Total を更新（値引き行の負工賃も対応）
    expenses: {
        'towing': レッカー費用(税抜),        # LineNo=1
        'rental_car': 代車費用(税抜),        # LineNo=2
        'short_parts': ショートパーツ(税抜), # LineNo=4（short_parts_wageと同義）
        'tax_exempt': 非課税費用,            # LineNo=5（消費税なし）
    }
    """
    if expenses is None:
        expenses = {}
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tf.write(db_bytes)
    tf.close()
    conn = sqlite3.connect(tf.name)
    cur  = conn.cursor()
    cur.execute('DELETE FROM ERParts')
    # 全Expense行をクリア（LineNo=1,2,3,4,5）
    for lno in (1, 2, 3, 4, 5):
        cur.execute("""UPDATE Expense SET
            WageEnabled=0, WageOutTax=0, WageInTax=0, WageTax=0
            WHERE LineNo=?""", (lno,))
    total_parts = 0
    total_wages = 0
    for i, item in enumerate(items):
        name   = item.get('name', '')
        method = item.get('method', '')
        qty    = safe_int(item.get('quantity', 1), 1)
        if qty < 1:
            qty = 1
        if 'parts_amount' in item:
            parts_total = safe_int(item.get('parts_amount', 0))
        else:
            unit_price  = safe_int(item.get('unit_price', 0))
            parts_total = unit_price * qty
        wage     = safe_int(item.get('wage', 0))
        rec_no   = i + 1
        line_no  = rec_no * 10
        wage_total  = wage
        parts_tax   = round(parts_total * TAX_RATE) if parts_total != 0 else 0
        parts_intax = parts_total + parts_tax if parts_total != 0 else 0
        wage_tax_abs = round(abs(wage_total) * TAX_RATE) if wage_total != 0 else 0
        wage_tax    = wage_tax_abs if wage_total >= 0 else -wage_tax_abs
        wage_intax  = wage_total + wage_tax if wage_total != 0 else 0
        total_parts += parts_total
        total_wages += wage_total
        # コグニセブンは -1 を空白として表示する（0やNULLは「0」と表示される）
        db_parts_total = parts_total if parts_total != 0 else -1
        db_parts_intax = parts_intax if parts_total != 0 else -1
        db_parts_tax   = parts_tax   if parts_total != 0 else -1
        db_wage_total  = wage_total  if wage_total  != 0 else -1
        db_wage_intax  = wage_intax  if wage_total  != 0 else -1
        db_wage_tax    = wage_tax    if wage_total  != 0 else -1
        # 部品金額がある行のみ数量を設定。脱着など部品なし行は -1（空白）
        db_qty = qty if parts_total != 0 else -1
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
            ?, ?, '', -1, -1,
            ?, '', ?, '',
            '', '',
            ?, ?, ?,
            -1, -1, -1,
            NULL, NULL, NULL,
            '*',
            -1, 0,
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
            rec_no, line_no,
            method, name,
            db_parts_total, db_parts_intax, db_parts_tax,
            db_wage_total, db_wage_intax, db_wage_tax,
            db_qty
        ))
    # ── Expense各行を更新 ──
    # LineNo=4: ショートパーツ
    sp_wage = safe_int(short_parts_wage)
    sp_tax   = round(sp_wage * TAX_RATE) if sp_wage > 0 else 0
    sp_intax = sp_wage + sp_tax if sp_wage > 0 else 0
    cur.execute("""UPDATE Expense SET
        WageEnabled=?, WageOutTax=?, WageInTax=?, WageTax=?
        WHERE LineNo=4""", (1 if sp_wage > 0 else 0, sp_wage, sp_intax, sp_tax))

    # LineNo=1: レッカー費用（課税）
    towing = safe_int(expenses.get('towing', 0))
    towing_tax   = round(towing * TAX_RATE) if towing > 0 else 0
    towing_intax = towing + towing_tax if towing > 0 else 0
    cur.execute("""UPDATE Expense SET
        WageEnabled=?, WageOutTax=?, WageInTax=?, WageTax=?
        WHERE LineNo=1""", (1 if towing > 0 else 0, towing, towing_intax, towing_tax))

    # LineNo=2: 代車費用（課税）
    rental_car = safe_int(expenses.get('rental_car', 0))
    rental_tax   = round(rental_car * TAX_RATE) if rental_car > 0 else 0
    rental_intax = rental_car + rental_tax if rental_car > 0 else 0
    cur.execute("""UPDATE Expense SET
        WageEnabled=?, WageOutTax=?, WageInTax=?, WageTax=?
        WHERE LineNo=2""", (1 if rental_car > 0 else 0, rental_car, rental_intax, rental_tax))

    # LineNo=5: 非課税費用（消費税なし）
    tax_exempt = safe_int(expenses.get('tax_exempt', 0))
    cur.execute("""UPDATE Expense SET
        WageEnabled=?, WageOutTax=?, WageInTax=?, WageTax=?
        WHERE LineNo=5""", (1 if tax_exempt > 0 else 0, tax_exempt, tax_exempt, 0))

    # ── Total計算 ──
    # 課税対象小計（部品＋工賃＋SP＋レッカー＋代車）
    taxable_expenses = sp_wage + towing + rental_car
    sub_total         = total_parts + total_wages + taxable_expenses
    tax_total         = round(sub_total * TAX_RATE)
    grand_total       = sub_total + tax_total + tax_exempt  # 非課税は税計算後に加算
    parts_tax_total   = round(total_parts * TAX_RATE)
    wages_tax_total   = round(total_wages * TAX_RATE)
    sp_tax_total      = round(sp_wage * TAX_RATE)
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
        tx_TotalOutTax=?,
        tx_TotalInTax=?,
        SubTotal=?,
        Total=?
    """, (
        total_parts, total_parts + parts_tax_total, parts_tax_total,
        total_wages, total_wages + wages_tax_total, wages_tax_total,
        taxable_expenses, taxable_expenses + round(taxable_expenses * TAX_RATE), round(taxable_expenses * TAX_RATE),
        tax_total,   tax_total,
        sub_total,   grand_total
    ))
    conn.commit()
    conn.close()
    with open(tf.name, 'rb') as f:
        result = f.read()
    os.unlink(tf.name)
    return result, total_parts, total_wages, grand_total


# ============================================================
# 内部ファイル更新: AnSvEm0001Ex.db（顧客・車両・保険）
# ============================================================

def update_em_db(db_bytes, cust, insurance_info, estimated_date):
    """Customer/FileInfo/Insurance テーブルを更新"""
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tf.write(db_bytes)
    tf.close()
    conn = sqlite3.connect(tf.name)
    cur  = conn.cursor()
    customer_name = safe_str(cust.get('customer_name', ''))
    owner_name    = safe_str(cust.get('owner_name', ''))
    postal_no     = safe_str(cust.get('postal_no', ''))
    prefecture    = safe_str(cust.get('prefecture', ''))
    municipality  = safe_str(cust.get('municipality', ''))
    address_other = safe_str(cust.get('address_other', ''))
    car_dept      = safe_str(cust.get('car_reg_department', ''))
    car_div       = safe_str(cust.get('car_reg_division', ''))
    car_biz       = safe_str(cust.get('car_reg_business', ''))
    car_serial    = safe_str(cust.get('car_reg_serial', ''))
    car_serial_no = safe_str(cust.get('car_serial_no', ''))
    car_name       = safe_str(cust.get('car_name', ''))
    car_model      = safe_str(cust.get('car_model', ''))
    engine_model   = safe_str(cust.get('engine_model', ''))
    body_color     = safe_str(cust.get('body_color', ''))
    color_code     = safe_str(cust.get('color_code', ''))
    trim_code      = safe_str(cust.get('trim_code', ''))
    car_weight     = safe_int(cust.get('car_weight', 0))
    displacement   = safe_int(cust.get('engine_displacement', 0))
    model_desig    = safe_str(cust.get('car_model_designation', ''))
    category_num   = safe_str(cust.get('car_category_number', ''))
    kilometer      = safe_int(cust.get('kilometer', -1), -1)
    term_date      = safe_str(cust.get('term_date', '00000000'))
    car_reg_date   = safe_str(cust.get('car_reg_date', '00000000'))
    if not term_date    or len(term_date) < 8:    term_date    = '00000000'
    if not car_reg_date or len(car_reg_date) < 8: car_reg_date = '00000000'
    term_era, term_era_year = get_era_info(term_date)
    reg_era,  reg_era_year  = get_era_info(car_reg_date)
    # Customer テーブル更新（スキーマ確認済みカラムのみ）
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
        customer_name, customer_name, owner_name,
        postal_no, prefecture, municipality, address_other,
        car_dept, car_div, car_biz, car_serial,
        car_serial_no, model_desig, category_num,
        term_date, term_era, term_era_year,
        car_reg_date, reg_era, reg_era_year,
        kilometer
    ))
    # Car テーブル更新（車名・カラーコード・トリムコード）
    car_cols = {row[1] for row in cur.execute("PRAGMA table_info(Car)").fetchall()}
    car_update = [
        ('CarName', car_name), ('CarNameByUser', car_name),
        ('ColorCode', color_code), ('ColorName', body_color),
        ('TrimCode', trim_code),
    ]
    valid_car = [(col, val) for col, val in car_update if col in car_cols and val]
    if valid_car:
        set_clause = ', '.join(f'{col}=?' for col, _ in valid_car)
        values = [val for _, val in valid_car]
        cur.execute(f'UPDATE Car SET {set_clause}', values)
    est_era, est_era_year = get_era_info(estimated_date)
    cur.execute('''UPDATE FileInfo SET
        EstimatedDate=?, EstimatedEra=?, EstimatedEraYear=?
    ''', (estimated_date, est_era, est_era_year))
    policy_no  = safe_str(insurance_info.get('policy_no', ''))
    contractor = safe_str(insurance_info.get('contractor_name', ''))
    cur.execute('''UPDATE Insurance SET
        PolicyNo=?, ContractorName=?,
        AccidentDate='00000000', AccidentEra='令和'
    ''', (policy_no, contractor))
    conn.commit()
    conn.close()
    with open(tf.name, 'rb') as f:
        result = f.read()
    os.unlink(tf.name)
    return result


# ============================================================
# 内部ファイル更新: AnSvMail.ini（XML）
# ============================================================

def update_mail_ini(orig_bytes, cust, grand_total):
    """Shift_JIS XMLの顧客・車両情報を更新"""
    text         = orig_bytes.decode('cp932', errors='replace')
    customer_name = safe_str(cust.get('customer_name', ''))
    owner_name    = safe_str(cust.get('owner_name', ''))
    car_dept      = safe_str(cust.get('car_reg_department', ''))
    car_div       = safe_str(cust.get('car_reg_division', ''))
    car_biz       = safe_str(cust.get('car_reg_business', ''))
    car_serial    = safe_str(cust.get('car_reg_serial', ''))
    car_no_full   = f'{car_dept}{car_div}{car_biz}{car_serial}'
    car_name      = safe_str(cust.get('car_name', ''))
    car_serial_no = safe_str(cust.get('car_serial_no', ''))
    kilometer     = safe_str(cust.get('kilometer', ''))
    car_reg_date  = safe_str(cust.get('car_reg_date', ''))
    term_date     = safe_str(cust.get('term_date', ''))
    tag_values = {
        'CustomerName1': customer_name,
        'OwnerName':     owner_name,
        'UserName':      customer_name,
        'CarNo':         car_no_full,
        'CarName':       car_name,
        'CarSerialNo':   car_serial_no,
        'Kilometrage':   kilometer,
        'CarNoArea':     car_dept,
        'CarNoClass':    car_div,
        'CarNoKana':     car_biz,
        'CarNoSeries':   car_serial,
        'Total':         grand_total,
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
    else:
        tag_values['CarRegistedDate'] = ''
    for tag_name, value in tag_values.items():
        text = replace_xml_tag(text, tag_name, value)
    return text.encode('cp932', errors='replace')


# ============================================================
# 内部ファイル更新: AnSvImge.ini（INI）
# ============================================================

def update_imge_ini(orig_bytes, cust):
    """INIファイルの顧客・車両情報を更新"""
    text      = orig_bytes.decode('cp932', errors='replace')
    ini_values = {
        'CustomerName':    safe_str(cust.get('customer_name', '')),
        'CarNoDepartment': safe_str(cust.get('car_reg_department', '')),
        'CarNoDivision':   safe_str(cust.get('car_reg_division', '')),
        'CarNoBusiness':   safe_str(cust.get('car_reg_business', '')),
        'CarNoSerial':     safe_str(cust.get('car_reg_serial', '')),
        'CarName':         safe_str(cust.get('car_name', '')),
    }
    for key, value in ini_values.items():
        text = replace_ini_value(text, key, value)
    return text.encode('cp932', errors='replace')


# ============================================================
# 内部ファイル更新: AnNote.ini（明細簡易表現）
# ============================================================

def generate_annote(items):
    """142B固定長 × 行数 の AnNote.ini を生成"""
    if not items:
        return b''
    lines = []
    for i, item in enumerate(items):
        name    = item.get('name', '')
        qty     = safe_int(item.get('quantity', 1), 1)
        rec_no  = i + 1
        line_no = rec_no * 10
        line    = bytearray(142)
        for j in range(142):
            line[j] = 0x20
        ln_str = f'{line_no:08d}'
        for j, c in enumerate(ln_str):
            line[j] = ord(c)
        name_bytes = name.encode('cp932', errors='replace')[:30]
        for j, b in enumerate(name_bytes):
            line[14 + j] = b
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
    now     = datetime.datetime.now()
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


def generate_neo_file(template_data, customer_info, items, short_parts_wage, insurance_info, expenses=None):
    """テンプレートNEOから更新済みNEOを生成"""
    real_ck  = find_real_cks(template_data)
    if not real_ck:
        raise ValueError("テンプレートNEOのCKチャンクが見つかりません")
    full_raw     = decompress_neo(template_data, real_ck)
    mgmt, entries = parse_entries(template_data, real_ck[0])
    files        = extract_files(full_raw, entries)
    estimated_date = datetime.datetime.now().strftime('%Y%m%d')
    normalized_items = items or []
    files['AnSMB.txt'], total_parts, total_wages, grand_total = update_ansmb(
        files['AnSMB.txt'], normalized_items, short_parts_wage, expenses=expenses
    )
    files['AnNote.ini']       = generate_annote(normalized_items)
    files['AnSvEm0001Ex.db']  = update_em_db(files['AnSvEm0001Ex.db'], customer_info, insurance_info, estimated_date)
    files['AnSvMail.ini']     = update_mail_ini(files['AnSvMail.ini'], customer_info, grand_total)
    files['AnSvImge.ini']     = update_imge_ini(files['AnSvImge.ini'], customer_info)
    neo_data = repack_neo(template_data, files, mgmt, entries)
    return neo_data, total_parts, total_wages, grand_total


# ============================================================
# AI-OCR サポート関数
# ============================================================

def enhance_image_for_ocr(image_bytes):
    """
    OCR精度向上のための画像前処理。
    FAX品質の低画質画像に対して、コントラスト・シャープネスを強化する。
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        img = Image.open(io.BytesIO(image_bytes))
        # コントラスト強化（FAXのかすれた文字を読みやすくする）
        img = ImageEnhance.Contrast(img).enhance(1.5)
        # シャープネス強化（ぼやけた文字のエッジを明確にする）
        img = ImageEnhance.Sharpness(img).enhance(2.0)
        # 明るさ微調整（暗すぎる画像を補正）
        img = ImageEnhance.Brightness(img).enhance(1.1)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=92)
        return buf.getvalue()
    except Exception:
        return image_bytes


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
        try:
            import pypdfium2 as pdfium
            doc     = pdfium.PdfDocument(pdf_bytes)
            page    = doc[page_index]
            scale   = dpi / 72.0
            bitmap  = page.render(scale=scale)
            pil_img = bitmap.to_pil()
            buf     = io.BytesIO()
            pil_img.save(buf, format='JPEG', quality=90)
            doc.close()
            result = buf.getvalue()
        except Exception:
            pass

    # ── 画像前処理（FAX品質改善用） ──────────────────
    if result and enhance:
        result = enhance_image_for_ocr(result)

    return result


def try_fix_landscape_pdf(pdf_bytes):
    """横向きPDFを検出して縦向きに回転する"""
    try:
        from pypdf import PdfReader, PdfWriter
        reader       = PdfReader(io.BytesIO(pdf_bytes))
        needs_rotation = False
        for page in reader.pages:
            box = page.mediabox
            if float(box.width) > float(box.height) * 1.2:
                needs_rotation = True
                break
        if not needs_rotation:
            return pdf_bytes
        writer = PdfWriter()
        for page in reader.pages:
            box = page.mediabox
            if float(box.width) > float(box.height) * 1.2:
                page.rotate(270)
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


def _get_genai_client(api_key):
    """google.genai クライアントを取得（キャッシュ付き）"""
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
    config = {"temperature": 0.1, "max_output_tokens": 65536}
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
                import time; time.sleep(2)
                continue
            raise ValueError("Geminiから有効な応答が得られませんでした。")
        except ValueError:
            raise
        except Exception as e:
            last_error = e
            if attempt < 2:
                import time; time.sleep(2)
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
        prompt = """この画像はPDFの1ページ目です。
このページがFAX送付状・送信票・表紙（本文ではないカバーページ）かどうか判定してください。
{"is_fax_cover": true, "page_type": "fax_cover", "reason": "理由"}
または
{"is_fax_cover": false, "page_type": "estimate/vehicle/other", "reason": "理由"}
JSONのみ返してください。"""
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


def analyze_vehicle_registration(api_key, file_bytes, mime_type):
    """車検証をAI-OCRで解析"""
    prompt = """あなたは日本の車検証（自動車検査証）を読み取るOCRエキスパートです。
提供された画像またはPDFから以下の情報を正確に読み取ってください。
**必ず有効なJSONのみを返してください。それ以外のテキストは一切不要です。**

返すべきJSON形式:
{
  "customer_name": "使用者の氏名又は名称（法人名含む）",
  "owner_name": "所有者の氏名又は名称",
  "postal_no": "使用者の住所の郵便番号（例: 339-0014）",
  "prefecture": "都道府県名",
  "municipality": "市区町村名",
  "address_other": "それ以降の住所",
  "car_reg_department": "登録番号の地名（例: 大宮）",
  "car_reg_division": "分類番号（例: １０２）※全角数字で",
  "car_reg_business": "用途かな文字（例: う）※全角で",
  "car_reg_serial": "一連番号（例: ８０００）※全角数字で",
  "car_serial_no": "車台番号（例: FD2AB-118821）",
  "car_name": "車名（メーカー名。例: トヨタ, 日産, アウディ）",
  "car_model": "型式（例: 3BA-FD2AB）",
  "car_model_designation": "型式指定番号（5桁の数字。例: 12345）",
  "car_category_number": "類別区分番号（4桁の数字。例: 0001）",
  "engine_model": "原動機の型式（エンジン型式。例: CZE, 2GR-FE, N20B20A）",
  "body_color": "車体の色（例: 白, 黒, シルバー）",
  "color_code": "カラーコード（車検証に記載がある場合。例: 040, 2T, LY9T）",
  "trim_code": "トリムコード（内装コード。車検証に記載がある場合。例: FK, QE）",
  "car_weight": 車両重量（kg単位の整数、不明なら0）,
  "engine_displacement": 総排気量（cc単位の整数、不明なら0）,
  "kilometer": 走行距離の数値（km単位の整数、不明なら0）,
  "term_date": "有効期間の満了日 YYYYMMDD形式（例: 20261125）",
  "car_reg_date": "初度登録年月 YYYYMM00形式（例: 20211100）",
  "confidence": 読み取り全体の信頼度を0.0から1.0で評価
}

注意事項:
- 和暦（令和/平成/昭和）は西暦に変換してください
  令和1年=2019年, 令和2年=2020年 ... 令和8年=2026年
  平成31年=2019年, 平成30年=2018年 ...
- 読み取れない項目は空文字列""または数値0にしてください
- 推測での補完は行わない
- 「車名」欄にはメーカー名のみが記載されていることが多い（例: トヨタ, ニッサン, アウディ）
- 「原動機の型式」はエンジン型式とも呼ばれる。必ず記載通りに正確に転記する
- 「型式指定番号」「類別区分番号」は車検証の右側に記載されていることが多い
- カラーコード・トリムコードは車検証の備考欄や車体の色の近くに記載されている場合がある
"""
    result_text = call_gemini(api_key, file_bytes, mime_type, prompt, use_json_mode=True)
    # JSON modeの場合は直接パース、フォールバックで従来のextract
    try:
        result = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        result = extract_json_from_response(result_text)

    # 車名が空欄の場合、車台番号からメーカーを推定
    if not result.get('car_name') and result.get('car_serial_no'):
        maker, _ = guess_manufacturer_from_vin(result['car_serial_no'])
        if maker:
            result['car_name'] = maker

    return result


def analyze_estimate_totals(api_key, file_bytes, mime_type, model_name):
    """1パス目: 合計値・税込/税抜の判定 + 見積書ヘッダの車両情報読み取り"""
    prompt = """この見積書から以下の情報を読み取ってください。
**必ず有効なJSONのみを返してください。**

{
  "_thought_process": "判断の根拠を50字以内で",
  "amount_basis": "明細・合計が税込か税抜かを判定: tax_inclusive(税込)/tax_exclusive(税抜)/unknown",
  "pdf_parts_total": 見積書に印刷されている部品合計/部品代/部品計の値（整数）,
  "pdf_wage_total": 見積書に印刷されている工賃合計/技術料/工賃計の値（整数）,
  "pdf_grand_total": 見積書に印刷されている最終合計金額（税込・整数）,
  "discount_amount": 値引き額（税込なら税込額、税抜なら税抜額、なければ0・整数）,
  "confidence": 読み取り信頼度 0.0〜1.0,

  "vehicle_info": {
    "car_name": "車名・車種名（例: A3 スポーツバック, プリウス, Cクラス）記載があれば",
    "car_model": "型式（例: 3BA-8VCUK, DBA-ZVW30）記載があれば",
    "engine_model": "エンジン型式（例: CZE, 2ZR-FXE, 274M20）記載があれば",
    "color_code": "カラーコード（例: LY9T, 040, 197, 2T）記載があれば",
    "color_name": "色名（例: ミトスブラック, ホワイトパール）記載があれば",
    "trim_code": "トリムコード/内装コード（例: FK, QE）記載があれば",
    "grade": "グレード名（例: 30TFSI, S-line, G）記載があれば",
    "model_year": "年式（例: 2021, R3）記載があれば",
    "chassis_no": "車台番号（例: WUAZZF10MD024124）記載があれば",
    "mileage": 走行距離（km、整数。記載があれば。なければ0）
  }
}

金額の注意:
- 一般的に左列が部品合計、右列が工賃合計
- 「部品計」「部品代」「部品合計額」「部品・油脂」等を探す
- 「工賃計」「技術料」「工賃合計額」等を探す
- 「合計」「税込合計」「総合計」が最終合計
- 金額はカンマを除去して整数で返す
- 読み取れない場合は0（nullではなく）
- amount_basis: 合計欄の金額が税込表示なら tax_inclusive、税抜表示なら tax_exclusive

車両情報の注意:
- 見積書のヘッダ部分（上部）に車名・型式・カラーコード等が記載されていることが多い
- 記載がない項目は空文字列""にする（推測しない）
- 「車名」「車種」「Car」等のラベルの横に書かれた値を読む
- 「E/G型式」「エンジン」「原動機」等のラベルの横がエンジン型式
- 「C/C」「カラー」「塗色」等のラベルの横がカラーコード
- 「T/C」「トリム」「内装」等のラベルの横がトリムコード
"""
    try:
        from google.genai import types
        client = _get_genai_client(api_key)
        file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, file_part],
            config={
                "temperature": 0.0,
                "max_output_tokens": 1024,
                "response_mime_type": "application/json",
            },
        )
        if response.text:
            try:
                return json.loads(response.text)
            except (json.JSONDecodeError, TypeError):
                return extract_json_from_response(response.text)
    except Exception:
        pass
    return None


def analyze_estimate_single(api_key, file_bytes, mime_type, model_name, page_num=1, total_pages=1):
    """2パス目: 見積書明細行を全フォーマット対応で読み取る"""
    page_instruction = ''
    if total_pages > 1:
        page_instruction = (
            f'\n\n★★★ これは全{total_pages}ページ中の{page_num}ページ目です。\n'
            'このページに記載されている明細行を全て読み取ってください。\n'
            '特に重要: ページの先頭行・末尾行を見落とさないでください。\n'
            'ページ上部や下部にある行も必ず含めてください。\n'
            '合計行・小計行・ページ小計行はitemsに含めないでください。 ★★★\n'
        )
    prompt = """あなたは日本の自動車修理見積書を正確に読み取るOCRの最高精度エキスパートです。
提供された見積書の画像またはPDFから、すべての明細行を1行も漏らさず正確に読み取ってください。
**必ず有効なJSONのみを返してください。それ以外のテキストは一切不要です。**
**JSONは完結させてください。途中で切れないようにしてください。**

━━━━━━━━━━━━━━━━━━━━━━━━━━
■ 見積書フォーマット自動判定ガイド
━━━━━━━━━━━━━━━━━━━━━━━━━━

見積書には主に10種類のフォーマットがあります。まず全体を見てフォーマットを判定してください。

【フォーマットA: コグニセブン系（部品価格列と工賃列が分離）】
特徴: 「コード | 修理項目/部品名称 | 修理方法/部品番号 | 部品価格(円) | 工賃(円)」
- 部品価格と工賃が別々の列に記載される
- 塗装明細が別セクション「【塗装明細】」にある場合がある
- 費用セクション「【費用】」にショートパーツ・内張り費用等がある
- 小計行に「部品計」「工賃計」「課税額計」「消費税」「合計」がある
→ 部品価格列の値をparts_amountに、工賃列の値をwageに入れる

【フォーマットB: 修理工場系（種別列で作業/部品を区分）】
特徴: 「作業内容/使用部品名 | 作業部位 | 種別 | 数量 | 単価 | 技術料/部品金額」
- 「種別」列に「作業」「部品」と明記されている
- 種別が「作業」→ 金額をwageに入れる（parts_amount=0）
- 種別が「部品」→ 金額をparts_amountに入れる（wage=0）
- 最下部に「技術料計」「部品計」「整備合計」がある

【フォーマットC: メルセデスベンツ系ディーラー（作業コード＋金額の1列）】
特徴: 「作業コード/部品番号 | 作業内容/部品名 | 時間/数量 | 金額」
- 作業と部品が混在し、金額が1列のみ
- 作業コードがBPで始まる → 作業行（wage=金額, parts_amount=0）
- 作業コードがMAまたはMNで始まる → 部品行（parts_amount=金額, wage=0）
- 作業名に「ペイント」「塗装」を含む → 作業行
- 作業名に「脱着」「交換」「修理」「板金」を含む → 作業行
- 最下部に「技術料」「部品代」「整備代合計」「消費税」「合計金額」がある

【フォーマットD: BMW系ディーラー（概算見積書・セクション分け＋作業CD）】
特徴: 「項目 | 作業CD/部品No. | 作業項目/部品名 | 工数/数量 | 単価 | 金額」
- セクション分け（A: 事故修理、B: 搬送費用 等）がある
- 作業CDが「MM99」で始まる行 → 工賃行（wage=金額, parts_amount=0）
- 作業CDが「UU99」で始まる行 → その他費用行（wage=金額, parts_amount=0）
- 作業CDが数字のみ（例: 0711 9904 207）→ 部品行（parts_amount=金額, wage=0）
- 工賃行のquantityは1にする（工数は時間なので数量ではない）
- 最下部に「工賃合計額」「部品合計額」「税込合計金額」がある

【フォーマットE: 町工場系（品名＋数量＋単価＋金額＋工賃の5列構成）】
特徴: 「品名 | 数量 | 単価 | 金額 | 工賃」の列構成
- 作業行: 「○○脱着組替」等、右端の「工賃」列に金額がある → wage=工賃列の値
- 部品行: 部品名＋数量＋単価＋金額 → parts_amount=金額列の値
- 最下部に「部品代」「工賃」「値引き」「小計」「消費税」「合計」がある

【フォーマットF: トヨタディーラー系（概算見積書・作業内容＋使用部品＋技術料）】
特徴: 「作業内容 | 使用部品 | 個数 | 部品・油脂 | 技術料 | 計」の列構成
- 作業行は「○○脱着」「○○修理」等 → 技術料列に金額がある（wage=技術料）
- 部品行は使用部品名＋個数＋部品金額 → parts_amount=部品・油脂列の値
- 「ショートパーツ」→ short_parts_wageに合算
- 最下部に「整備代金合計」が部品・技術料の内訳付きで記載される

【フォーマットG: 整備工場系（整備内容＋技術料＋部品単価＋部品小計の4列）】
特徴: 「整備内容 | 技術料(円) | 数量 | 部品単価(円) | 部品小計(円)」
- 1行に技術料と部品小計の両方が存在する場合がある
- 数量に単位が付く場合（「1個」「10個」「2本」）→ 数値部分のみ抽出
- 最下部に「A 技術料」「B 部品代」の合計が記載される

【フォーマットH: ヤナセ系（左右2カラム構成・作業と部品が左右に分離）】
特徴: 左側に「作業内容 | 金額」、右側に「使用部品 | 数量 | 金額」が並ぶ
- 左カラム: 作業内容と作業金額 → wageに入れる
- 右カラム: 使用部品名＋数量＋部品金額 → parts_amountに入れる
- 最下部に「定価合計:（作業）金額（部品）金額（全体）金額」がある

【フォーマットI: トラック整備系（部品列と工賃列が分離した一般的な4列）】
特徴: 「作業内容及び部品明細 | 数量 | 単価 | 部品 | 工賃」の5列
- 工賃列に値がある行 → wage=工賃列の値
- 部品列に値がある行 → parts_amount=部品列の値
- 「ショートパーツ」「材料費」は部品扱い（parts_amountに入れる）

【フォーマットJ: UDトラックス系（区コードで作業/部品を判別）】
特徴: 「区 | 作業コード(部品番号) | 作業内容(部品名称) | 数量 | 定価/単価 | 金額」
- 区=11 → 作業行（wage=金額, parts_amount=0, quantity=1）
- 区=1  → 部品行（parts_amount=金額, wage=0）
- 区=5  → セクションヘッダ → itemsに含めない
- 区=7  → その他費用行（wage=金額）

━━━━━━━━━━━━━━━━━━━━━━━━━━
■ 返すべきJSON形式
━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "_thought_process": "フォーマット判定の根拠と税込/税抜の判断理由を100字以内で",
  "amount_basis": "tax_inclusive(明細が税込) / tax_exclusive(明細が税抜) / unknown",
  "items": [
    {
      "name": "部品名または作業名（見積書に記載のまま正確に転記）",
      "method": "区分（取替/脱着/修理/塗装/交換/調整/板金/部品 等）",
      "quantity": 数量（整数に丸める。1.00→1）,
      "parts_amount": その行の部品金額合計（整数。作業行は0）,
      "wage": その行の工賃/技術料（整数。部品行は0）
    }
  ],
  "short_parts_wage": ショートパーツ・雑品代・小物部品代の合計（整数。なければ0。itemsには含めない）,
  "tax_exempt_amount": 預託金・廃棄処分費用等の非課税費用合計（整数。なければ0。itemsには含めない）,
  "discount_amount": 値引き額（整数。なければ0。items には含めない）,
  "pdf_parts_total": 見積書記載の「部品計」「部品代」の値（整数）,
  "pdf_wage_total": 見積書記載の「工賃計」「技術料」の値（整数）,
  "confidence": 読み取り信頼度 0.0〜1.0
}

━━━━━━━━━━━━━━━━━━━━━━━━━━
■ 読み取りルール（厳守）
━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 明細テーブルの全行を1行ずつ正確に読み取る。行を絶対に飛ばさない。
2. 複数ページにまたがる場合、全ページの明細を必ず結合する。
3. 部品名は見積書に記載のとおり正確に転記する。
4. 金額のカンマは除去して整数にする（例: 19,550 → 19550）。
5. 数量が小数（1.00, 13.00）の場合は整数に丸める。
6. 金額0の行も含める（0は0として記録する）。
7. 塗装明細の各パネル行は通常の明細行として含める（wage=塗装工賃）。
8. 費用セクションの扱い:
   - 「ショートパーツ」「小物部品」「雑品代」→ short_parts_wageに合算
   - 「内張り費用」「室内清掃費」「写真代」等 → 通常の明細行（wage）
9. parts_amountとwageの両方に値がある行もある。
10. 合計行・小計行・ページ小計行はitemsに含めない。
11. 「値引き」行はitemsに含めず discount_amount に記録する。
12. FAX受信で画質が悪い場合でも、読み取れる文字は最大限読み取る。
13. 「塗装に含む」「塗装費用」は工賃行として扱う（wage=金額）。
14. 「油脂代」は部品扱い（parts_amountに入れる）。
15. 数量が品名の後ろに「(数字」「（数字」形式で記載されている場合がある。
    例: 「ｸﾘｯﾌﾟﾅｯﾄ 取替 (14 1,960」→ 品名=ｸﾘｯﾌﾟﾅｯﾄ, method=取替, quantity=14, parts_amount=1960
    例: 「ｸﾘｯﾌﾟ 取替 (02 1,120」→ 品名=ｸﾘｯﾌﾟ, method=取替, quantity=2, parts_amount=1120
    この「(数字」は数量であり、品名に含めない。先頭のゼロも除去して整数にする。
    単価は parts_amount ÷ quantity で逆算できる（例: 1960÷14=140円/個）。
16. 数量が別の列やセルにない場合でも、品名の横や直後に括弧付きで書かれていることがある。
    必ず数量として認識すること。
17. 工賃欄に「**」「＊＊」「***」等のアスタリスクのみが記載されている場合は工賃なし（wage=0）として扱う。
    「**」は金額ではない。必ず0にすること。
18. 「ショートパーツ」「ｼｮｰﾄﾊﾟｰﾂ」「雑品代」「小物部品代」はitemsに含めず、short_parts_wageに合算する。
19. 「預託金」「廃棄処分費用」「預託/廃棄処分費用」「リサイクル預託金」は非課税費用。
    itemsに含めず、別途 tax_exempt_amount として返す。
"""
    if page_instruction:
        prompt = page_instruction + prompt
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
                    "temperature": 0.1,
                    "max_output_tokens": 65536,
                    "response_mime_type": "application/json",
                },
            )
            if response.text and response.text.strip():
                result_text = response.text
                try:
                    return json.loads(result_text)
                except (json.JSONDecodeError, TypeError):
                    return extract_json_from_response(result_text)
            if attempt < 2:
                import time; time.sleep(2)
                continue
            raise ValueError("Geminiから有効な応答が得られませんでした。")
        except ValueError:
            raise
        except Exception as e:
            last_error = e
            if attempt < 2:
                import time; time.sleep(2)
                continue
            raise ValueError(f"Gemini API呼び出しに失敗しました: {str(last_error)}")


def validate_and_correct_items(items):
    """
    辞書ベースバリデーション: AIの誤分類を後処理で修正する。
    - 「脱着」「交換」等の作業系methodなのにparts_amountだけ入っている → wageへ移動
    """
    WAGE_METHODS  = {'脱着', '取外', '取付', '修理', '調整', '板金', '塗装',
                     'ペイント', '研磨', '清掃', '点検', '作業', '交換', '脱外組付',
                     '修正', '組付', '施工', '補修'}
    corrected = []
    for item in items:
        item = dict(item)
        method    = str(item.get('method', ''))
        name      = str(item.get('name', ''))
        parts_amt = safe_int(item.get('parts_amount', 0))
        wage      = safe_int(item.get('wage', 0))
        # 作業系キーワードがあるのに部品金額のみ → wageへ
        is_wage_method = any(kw in method for kw in WAGE_METHODS)
        is_wage_name   = any(kw in name   for kw in {'脱着', '板金', '塗装', 'ペイント', '修理', '研磨'})
        if (is_wage_method or is_wage_name) and parts_amt > 0 and wage == 0:
            item['wage']         = parts_amt
            item['parts_amount'] = 0
        corrected.append(item)
    return corrected


def extract_special_items(items, existing_sp=0, existing_exempt=0):
    """
    ショートパーツ・預託金等の特殊項目を明細行から抽出してExpense用に分離する。
    二重計上を防止するため、明細行からは除去してExpense値として返す。
    Returns: (filtered_items, short_parts_wage, tax_exempt_amount)
    """
    SP_KEYWORDS = {'ショートパーツ', 'ｼｮｰﾄﾊﾟｰﾂ', '雑品代', '小物部品代', '雑品', 'ショートパーツ代'}
    EXEMPT_KEYWORDS = {'預託金', '廃棄処分費用', '預託/廃棄処分費用', 'リサイクル預託金',
                       '預託/廃棄処分', '廃棄処分', 'ﾘｻｲｸﾙ預託金'}
    filtered = []
    sp_total = existing_sp
    exempt_total = existing_exempt
    for item in items:
        name = str(item.get('name', '')).strip()
        parts = safe_int(item.get('parts_amount', 0))
        wage = safe_int(item.get('wage', 0))
        amount = parts + wage
        # ショートパーツ判定
        if any(kw in name for kw in SP_KEYWORDS):
            sp_total += amount
            continue
        # 預託金・廃棄処分費用判定
        if any(kw in name for kw in EXEMPT_KEYWORDS):
            exempt_total += amount
            continue
        filtered.append(item)
    return filtered, sp_total, exempt_total


def deduplicate_page_items(all_items):
    """
    複数ページ分割時のページ境界での重複行を除去する。
    同一品名＋同一金額の行がページ境界付近で連続する場合、重複とみなして除去。
    """
    if len(all_items) <= 1:
        return all_items
    deduped = [all_items[0]]
    for i in range(1, len(all_items)):
        curr = all_items[i]
        prev = deduped[-1]
        # 品名・部品金額・工賃が全て一致する場合は重複と判定
        same_name  = str(curr.get('name', '')).strip() == str(prev.get('name', '')).strip()
        same_parts = safe_int(curr.get('parts_amount', 0)) == safe_int(prev.get('parts_amount', 0))
        same_wage  = safe_int(curr.get('wage', 0)) == safe_int(prev.get('wage', 0))
        if same_name and same_parts and same_wage and str(curr.get('name', '')).strip():
            continue  # 重複 → スキップ
        deduped.append(curr)
    return deduped


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
                           pdf_wage_total, pdf_grand_total, discount_amount=0):
    """
    税込/税抜を自動判定してNEO書込み用正規化サマリーを返す。
    Returns: {
      'basis': 'tax_inclusive' / 'tax_exclusive' / 'unknown',
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

    if grand <= 0:
        return {
            'basis': 'tax_exclusive',
            'norm_parts': calc_parts, 'norm_wage': calc_wage,
            'norm_sp': sp, 'norm_disc': disc,
            'grand': 0, 'reverse_match': False,
        }

    TOLERANCE = 50  # 円
    # 税込判定: (items_sum) ≈ grand (既に税込)
    tax_incl = abs(calc_parts + calc_wage + sp - disc - grand) <= TOLERANCE
    # 税抜判定: round((sum) * 1.1) ≈ grand
    tax_excl = abs(round((calc_parts + calc_wage + sp - disc) * (1 + TAX_RATE)) - grand) <= TOLERANCE

    if tax_incl and not tax_excl:
        basis = 'tax_inclusive'
    elif tax_excl:
        basis = 'tax_exclusive'
    else:
        # どちらにも合わない場合: pdf_grand_totalと比較
        # pdf_parts_total + pdf_wage_total が taxes込みに近ければ tax_inclusive
        pdf_sum = safe_int(pdf_parts_total) + safe_int(pdf_wage_total) + sp - disc
        if abs(pdf_sum - grand) <= TOLERANCE:
            basis = 'tax_inclusive'
        else:
            basis = 'unknown'

    if basis == 'tax_inclusive':
        norm_parts = round(calc_parts / (1 + TAX_RATE))
        norm_wage  = round(calc_wage  / (1 + TAX_RATE))
        norm_sp    = round(sp         / (1 + TAX_RATE))
        norm_disc  = round(disc       / (1 + TAX_RATE))
    else:
        norm_parts = calc_parts
        norm_wage  = calc_wage
        norm_sp    = sp
        norm_disc  = disc

    reverse_grand = round((norm_parts + norm_wage + norm_sp - norm_disc) * (1 + TAX_RATE))
    reverse_match = abs(reverse_grand - grand) <= TOLERANCE

    return {
        'basis':       basis,
        'norm_parts':  norm_parts,
        'norm_wage':   norm_wage,
        'norm_sp':     norm_sp,
        'norm_disc':   norm_disc,
        'grand':       grand,
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
    parts_diff = calc_parts - target_parts
    wage_diff  = calc_wage  - target_wage

    if (abs(parts_diff) <= SELF_CORRECTION_THRESHOLD and
            abs(wage_diff) <= SELF_CORRECTION_THRESHOLD):
        return None  # 差額が閾値以下 → 修正不要

    correction_prompt = f"""[自己修復モード]
前回の読み取り結果に以下の誤差が検出されました:
- 部品合計: 計算値 ¥{calc_parts:,} ≠ PDF記載 ¥{target_parts:,} （差額 {parts_diff:+,}円）
- 工賃合計: 計算値 ¥{calc_wage:,} ≠ PDF記載 ¥{target_wage:,} （差額 {wage_diff:+,}円）

見積書を再度精読して誤差の原因を特定してください。
よくある原因:
- 行の見落とし（合計に含まれているのにitemsにない行がある）
- 部品と工賃の取り違え（wageに入れるべきものがparts_amountに入っている、またはその逆）
- 数量の誤読（10を1と読んでいる等）
- ショートパーツをitemsに入れていて合計がずれている

正しいJSONで再度出力してください（形式は前回と同じ）。
合計が一致するよう修正してください。
**必ず有効なJSONのみを返してください。**
"""
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
        new_items  = new_result.get('items', [])
        if not new_items:
            return None
        new_parts  = sum(safe_int(it.get('parts_amount', 0)) for it in new_items)
        new_wage   = sum(safe_int(it.get('wage', 0))         for it in new_items)
        old_error  = abs(parts_diff) + abs(wage_diff)
        new_error  = abs(new_parts - target_parts) + abs(new_wage - target_wage)
        if new_error < old_error:
            return new_result  # 改善された → 採用
        return None
    except Exception:
        return None


def analyze_estimate(api_key, file_bytes, mime_type, model_name=None,
                     use_fax_filter=False, use_rasterize=False, use_enhance=True):
    """
    見積書をAI-OCRで解析するメイン関数。
    新機能:
      use_fax_filter: FAXページを自動除外する（追加APIコール1回）
      use_rasterize: PDF→JPEG変換してから送信（行ズレ防止）
    """
    used_model = model_name or GEMINI_MODEL

    # ① FAXページフィルタリング（オプション）
    filtered_count = 0
    if use_fax_filter and mime_type == 'application/pdf':
        original_size = len(file_bytes)
        file_bytes    = filter_fax_pages(api_key, file_bytes, used_model)
        if len(file_bytes) < original_size:
            filtered_count = 1

    # ② 横向きPDF補正
    if mime_type == 'application/pdf':
        file_bytes = try_fix_landscape_pdf(file_bytes)

    # ③ ページ分割
    pages = try_split_pdf_pages(file_bytes) if mime_type == 'application/pdf' else None

    # ③-b ラスタライズ: PDF→JPEG変換（行ズレ防止）
    # 1パス目用: 合計欄が最終ページにある場合を考慮し、最終ページをラスタライズ
    raster_bytes = file_bytes
    raster_mime  = mime_type
    if use_rasterize and mime_type == 'application/pdf':
        try:
            from pypdf import PdfReader
            num_pages = len(PdfReader(io.BytesIO(file_bytes)).pages)
        except Exception:
            num_pages = 1
        # 最終ページをラスタライズ（合計欄は通常最終ページにある）
        last_page_idx = max(0, num_pages - 1)
        img = rasterize_pdf_page(file_bytes, last_page_idx, dpi=250, enhance=use_enhance)
        if img:
            raster_bytes = img
            raster_mime  = 'image/jpeg'

    # ③-c 1ページ目もラスタライズ（車両情報ヘッダ読み取り用）
    first_raster_bytes = file_bytes
    first_raster_mime  = mime_type
    if use_rasterize and mime_type == 'application/pdf':
        img1 = rasterize_pdf_page(file_bytes, 0, dpi=250, enhance=use_enhance)
        if img1:
            first_raster_bytes = img1
            first_raster_mime  = 'image/jpeg'

    # ④ 1パス目: 合計値＋車両情報抽出
    # 合計欄は最終ページ、車両情報は1ページ目にあることが多い
    # 単一ページ or 1ページ目=最終ページ の場合はそのまま
    totals_data = analyze_estimate_totals(api_key, raster_bytes, raster_mime, used_model) or {}
    # 複数ページで1ページ目≠最終ページの場合、1ページ目から車両情報を別途取得
    if pages and len(pages) > 1 and first_raster_bytes != raster_bytes:
        first_page_data = analyze_estimate_totals(api_key, first_raster_bytes, first_raster_mime, used_model) or {}
        # 車両情報は1ページ目の結果を優先
        vinfo_first = first_page_data.get('vehicle_info', {})
        vinfo_last  = totals_data.get('vehicle_info', {})
        merged_vinfo = {k: (vinfo_first.get(k) or vinfo_last.get(k, '')) for k in
                        set(list(vinfo_first.keys()) + list(vinfo_last.keys()))}
        totals_data['vehicle_info'] = merged_vinfo
    target_parts = safe_int(totals_data.get('pdf_parts_total', 0))
    target_wage  = safe_int(totals_data.get('pdf_wage_total', 0))
    pdf_grand    = safe_int(totals_data.get('pdf_grand_total', 0))
    discount     = safe_int(totals_data.get('discount_amount', 0))

    # ⑤ 2パス目: 明細抽出
    if pages and len(pages) > 1:
        all_items   = []
        total_sp    = 0
        confidences = []
        # 各ページのラスタライズ前処理
        page_data = []
        for idx, page_bytes in enumerate(pages):
            send_bytes = page_bytes
            send_mime  = 'application/pdf'
            if use_rasterize:
                img = rasterize_pdf_page(page_bytes, 0, dpi=250, enhance=use_enhance)
                if img:
                    send_bytes = img
                    send_mime  = 'image/jpeg'
            page_data.append((idx, send_bytes, send_mime))
        # 全ページを並列でAPI呼び出し（高速化）
        def _analyze_page(args):
            idx, sb, sm = args
            try:
                return idx, analyze_estimate_single(
                    api_key, sb, sm, used_model, idx + 1, len(pages)
                ) or {}
            except Exception:
                return idx, {}
        with ThreadPoolExecutor(max_workers=min(4, len(page_data))) as executor:
            results = list(executor.map(_analyze_page, page_data))
        # ページ順に結合
        results.sort(key=lambda x: x[0])
        for idx, res in results:
            all_items.extend(res.get('items', []))
            total_sp += safe_int(res.get('short_parts_wage', 0))
            confidences.append(safe_float(res.get('confidence', 0.0)))
        # ページ境界の重複行を除去
        all_items = deduplicate_page_items(all_items)
        result = {
            'items':           all_items,
            'short_parts_wage': total_sp,
            'pdf_parts_total': target_parts,
            'pdf_wage_total':  target_wage,
            'pdf_grand_total': pdf_grand,
            'discount_amount': discount,
            'confidence':      (sum(confidences) / len(confidences)) if confidences else 0.5,
            '_fax_filtered':   filtered_count,
            '_page_count':     len(pages),
            '_vehicle_info':   totals_data.get('vehicle_info', {}),
        }
    else:
        # 単一ページ: ラスタライズ済みがあればそちらを使用
        result = analyze_estimate_single(
            api_key, raster_bytes, raster_mime, used_model, 1, 1
        ) or {}
        result.setdefault('items', [])
        result.setdefault('short_parts_wage', 0)
        result['pdf_parts_total'] = target_parts or safe_int(result.get('pdf_parts_total', 0))
        result['pdf_wage_total']  = target_wage  or safe_int(result.get('pdf_wage_total', 0))
        result['pdf_grand_total'] = pdf_grand    or safe_int(result.get('pdf_grand_total', 0))
        result['discount_amount'] = discount     or safe_int(result.get('discount_amount', 0))
        result['confidence']      = safe_float(result.get('confidence', 0.5))
        result['_fax_filtered']   = filtered_count
        result['_vehicle_info']   = totals_data.get('vehicle_info', {})

    # ⑥ 辞書ベースバリデーション
    result['items'] = validate_and_correct_items(result['items'])

    # ⑥-b 明細行ごとの整合性チェック
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

    # ⑦ 自己修復ループ（合計値が取得できた場合のみ）
    if (result['pdf_parts_total'] > 0 or result['pdf_wage_total'] > 0):
        retry = _self_correction_retry(
            api_key, raster_bytes, raster_mime, used_model,
            result['items'],
            result['pdf_parts_total'],
            result['pdf_wage_total'],
        )
        if retry:
            result['items']          = validate_and_correct_items(retry.get('items', result['items']))
            result['short_parts_wage'] = safe_int(retry.get('short_parts_wage', result['short_parts_wage']))
            result['_self_corrected']  = True

    # ⑧ 税込/税抜 自動判定
    summary = build_estimate_summary(
        result['items'],
        result.get('short_parts_wage', 0),
        result.get('pdf_parts_total', 0),
        result.get('pdf_wage_total', 0),
        result.get('pdf_grand_total', 0),
        result.get('discount_amount', 0),
    )
    result['_tax_basis']     = summary['basis']
    result['_reverse_match'] = summary['reverse_match']

    # ⑨ 税込明細の場合、税抜に正規化
    if summary['basis'] == 'tax_inclusive':
        for item in result['items']:
            pa = safe_int(item.get('parts_amount', 0))
            wg = safe_int(item.get('wage', 0))
            if pa != 0:
                item['parts_amount'] = round(pa / (1 + TAX_RATE))
            if wg != 0:
                item['wage'] = round(wg / (1 + TAX_RATE))
        result['short_parts_wage'] = round(safe_int(result.get('short_parts_wage', 0)) / (1 + TAX_RATE))
        result['_tax_converted']   = True

    # ⑩ 値引きを負の工賃行として items に追加
    disc_outtax = summary.get('norm_disc', 0)
    if disc_outtax > 0:
        result['items'].append({
            'name':         '値引き',
            'method':       '値引き',
            'quantity':     1,
            'parts_amount': 0,
            'wage':         -disc_outtax,
        })

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

def main():
    st.set_page_config(
        page_title="NEO自動生成アプリ",
        page_icon="🚗",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    st.markdown("""
    <style>
    /* ドラッグ＆ドロップエリア */
    [data-testid="stFileUploader"] {
        border: 2px dashed #3498db !important;
        border-radius: 10px !important;
        padding: 12px !important;
        background-color: #f0f8ff !important;
    }
    [data-testid="stFileUploader"]:hover {
        background-color: #e0f0ff !important;
    }
    .main-title   { font-size: 1.8rem; font-weight: bold; color: #1a5276; margin-bottom: 0.5rem; }
    .step-header  { font-size: 1.3rem; font-weight: bold; color: #2c3e50; padding: 0.5rem 0; border-bottom: 2px solid #3498db; margin-bottom: 1rem; }
    .success-box  { background: #d4edda; border: 1px solid #c3e6cb; border-radius: 8px; padding: 1rem; margin: 0.5rem 0; }
    .warning-box  { background: #fff3cd; border: 1px solid #ffeaa7; border-radius: 8px; padding: 1rem; margin: 0.5rem 0; }
    .error-box    { background: #f8d7da; border: 1px solid #f5c6cb; border-radius: 8px; padding: 1rem; margin: 0.5rem 0; }
    .info-box     { background: #d1ecf1; border: 1px solid #bee5eb; border-radius: 8px; padding: 1rem; margin: 0.5rem 0; }
    .tax-box      { background: #e8f5e9; border: 1px solid #a5d6a7; border-radius: 8px; padding: 0.8rem; margin: 0.5rem 0; font-size: 0.9rem; }
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="main-title">🚗 AI-OCR連携 NEOファイル自動生成 v3.2</div>', unsafe_allow_html=True)
    st.caption("車検証PDF＋見積書PDF → コグニセブン用NEOファイルを自動生成します")

    # テンプレートチェック
    if not os.path.exists(TEMPLATE_PATH):
        st.error(
            f"⚠️ テンプレートファイルが見つかりません: {TEMPLATE_FILENAME}\n\n"
            f"app.py と同じフォルダに「{TEMPLATE_FILENAME}」を配置してください。"
        )
        st.stop()
    with open(TEMPLATE_PATH, 'rb') as f:
        template_data = f.read()

    # ─── サイドバー ───────────────────────────────────
    with st.sidebar:
        st.header("⚙️ 設定")
        if GEMINI_API_KEY:
            api_key = GEMINI_API_KEY
            st.success("🔑 APIキー: 設定済み")
        else:
            api_key = st.text_input(
                "🔑 Gemini APIキー",
                type="password",
                help=".envファイルの GEMINI_API_KEY にキーを設定すれば毎回入力不要"
            )
        selected_model = st.selectbox(
            "🤖 AIモデル",
            options=["gemini-3.1-pro-preview", "gemini-2.5-flash", "gemini-2.5-pro"],
            index=0,
            help="3.1 Pro Preview=最新・最高精度、2.5 Flash=コスパ良好、2.5 Pro=高精度"
        )
        st.markdown("---")
        st.header("🔬 精度オプション")
        use_fax_filter = st.checkbox(
            "FAXページ自動除外",
            value=False,
            help="FAX送付状が混在するPDFの1ページ目を自動検出・除外します。APIコールが1回増えます。"
        )
        use_rasterize = st.checkbox(
            "PDF→画像変換（行ズレ防止）",
            value=True,
            help="PDFをJPEG画像に変換してからAIに送ります。テキストレイヤーの行ズレ問題を防ぎます。デフォルト有効。"
        )
        use_enhance = st.checkbox(
            "画像前処理（FAX品質改善）",
            value=True,
            help="コントラスト・シャープネスを強化してFAX品質の画像を読みやすくします。ラスタライズ有効時のみ機能します。"
        )
        st.markdown("---")
        st.header("🛡️ 保険情報")
        policy_no       = st.text_input("証券番号",  value=st.session_state.get('policy_no', ''))
        contractor_name = st.text_input("契約者名", value=st.session_state.get('contractor_name', ''))
        st.session_state['policy_no']       = policy_no
        st.session_state['contractor_name'] = contractor_name
        st.markdown("---")
        st.header("💰 費用（Expense）")
        exp_towing    = st.number_input("レッカー費用（税抜）",  value=st.session_state.get('exp_towing', 0),    min_value=0, step=1000, key='exp_towing_input')
        exp_rental    = st.number_input("代車費用（税抜）",      value=st.session_state.get('exp_rental', 0),    min_value=0, step=1000, key='exp_rental_input')
        exp_exempt    = st.number_input("非課税費用",            value=st.session_state.get('exp_exempt', 0),    min_value=0, step=1000, key='exp_exempt_input')
        st.session_state['exp_towing'] = exp_towing
        st.session_state['exp_rental'] = exp_rental
        st.session_state['exp_exempt'] = exp_exempt
        st.markdown("---")
        st.caption(f"消費税率: {int(TAX_RATE * 100)}%（固定）")
        st.caption(f"見積日: {datetime.datetime.now().strftime('%Y/%m/%d')}（自動）")

    # セッション状態初期化
    for key, default in [
        ('step', 1), ('vehicle_data', None), ('estimate_data', None),
        ('neo_bytes', None), ('neo_filename', None)
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ステップ表示
    steps       = ["① アップロード", "② AI解析", "③ プレビュー・修正", "④ NEO生成"]
    current_step = st.session_state['step']
    cols        = st.columns(4)
    for i, (col, label) in enumerate(zip(cols, steps)):
        step_num = i + 1
        if step_num < current_step:
            col.success(label + " ✅")
        elif step_num == current_step:
            col.info("▶ " + label)
        else:
            col.markdown(f"<span style='color:#aaa'>{label}</span>", unsafe_allow_html=True)
    st.markdown("---")

    # =========================================
    # STEP 1: アップロード
    # =========================================
    if current_step == 1:
        st.markdown('<div class="step-header">① ファイルアップロード</div>', unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("📋 車検証（必須）")
            st.markdown("PDFまたは画像をここにドロップ、またはクリックして選択")
            vehicle_file = st.file_uploader(
                "車検証",
                type=['pdf', 'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff', 'tif', 'heic', 'heif'],
                key='vehicle_upload',
                label_visibility='collapsed'
            )
            if vehicle_file:
                st.success(f"✅ {vehicle_file.name} ({vehicle_file.size:,} bytes)")
        with col2:
            st.subheader("📄 見積書（任意）")
            st.markdown("PDFまたは画像をここにドロップ、またはクリックして選択")
            estimate_file = st.file_uploader(
                "見積書",
                type=['pdf', 'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff', 'tif', 'heic', 'heif'],
                key='estimate_upload',
                label_visibility='collapsed'
            )
            if estimate_file:
                st.success(f"✅ {estimate_file.name} ({estimate_file.size:,} bytes)")
            else:
                st.info("💡 見積書なしの場合、車両情報のみのNEOを作成します")
        st.markdown("")
        if vehicle_file:
            if st.button("🔍 AI解析を開始する →", type="primary", use_container_width=True):
                if not api_key:
                    st.error("⚠️ APIキーが未設定です。サイドバーで入力するか、app.py冒頭の GEMINI_API_KEY に貼り付けてください。")
                else:
                    st.session_state['vehicle_file_bytes'] = vehicle_file.read()
                    st.session_state['vehicle_file_name']  = vehicle_file.name
                    if estimate_file:
                        st.session_state['estimate_file_bytes'] = estimate_file.read()
                        st.session_state['estimate_file_name']  = estimate_file.name
                    else:
                        st.session_state['estimate_file_bytes'] = None
                        st.session_state['estimate_file_name']  = None
                    st.session_state['use_fax_filter'] = use_fax_filter
                    st.session_state['use_rasterize']  = use_rasterize
                    st.session_state['use_enhance']    = use_enhance
                    st.session_state['selected_model'] = selected_model
                    st.session_state['step'] = 2
                    st.rerun()
        else:
            st.warning("車検証ファイルをアップロードしてください")

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
        _use_raster    = st.session_state.get('use_rasterize', False)
        _use_enhance   = st.session_state.get('use_enhance', True)
        _model         = st.session_state.get('selected_model', GEMINI_MODEL)

        if vehicle_bytes is None:
            st.error("車検証データが見つかりません。ステップ①に戻ってください。")
            if st.button("← ステップ①に戻る"):
                st.session_state['step'] = 1
                st.rerun()
            st.stop()

        progress = st.progress(0, text="AI解析を開始しています...")
        try:
            vehicle_mime  = get_mime_type(vehicle_name)
            estimate_mime = get_mime_type(estimate_name) if estimate_bytes else None

            # 車検証＋見積書を並列で解析（高速化）
            if estimate_bytes:
                progress.progress(10, text="🔍 車検証＋見積書を同時解析中...")
                with ThreadPoolExecutor(max_workers=2) as executor:
                    fut_vehicle = executor.submit(
                        analyze_vehicle_registration, api_key, vehicle_bytes, vehicle_mime
                    )
                    fut_estimate = executor.submit(
                        analyze_estimate, api_key, estimate_bytes, estimate_mime, _model,
                        _use_fax, _use_raster, _use_enhance
                    )
                    vehicle_data  = fut_vehicle.result()
                    progress.progress(50, text="✅ 車検証の解析完了、見積書を処理中...")
                    estimate_data = fut_estimate.result() or {}
            else:
                progress.progress(10, text="🔍 車検証を解析中...")
                vehicle_data  = analyze_vehicle_registration(api_key, vehicle_bytes, vehicle_mime)
                estimate_data = None

            st.session_state['vehicle_data'] = vehicle_data
            progress.progress(60, text="✅ 解析完了")

            v_conf = safe_float(vehicle_data.get('confidence', 1.0), 1.0)
            if v_conf < CONFIDENCE_THRESHOLD:
                st.warning(f"⚠️ 車検証の読み取り信頼度が低いです（{v_conf:.0%}）。プレビュー画面で内容をご確認ください。")

            # 見積書の後処理
            if estimate_data:
                # 部品名を半角カタカナに変換
                for item in estimate_data.get('items', []):
                    if item.get('name'):
                        item['name'] = to_halfwidth_katakana(item['name'])

                # 見積書ヘッダの車両情報で車検証データの空欄を補完
                est_vinfo = estimate_data.get('_vehicle_info', {})
                if est_vinfo and vehicle_data:
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
                    st.warning(f"⚠️ 見積書の読み取り信頼度が低いです（{e_conf:.0%}）。プレビュー画面で内容をご確認ください。")

                # 精度処理の結果を表示
                info_msgs = []
                if estimate_data.get('_fax_filtered', 0) > 0:
                    info_msgs.append("🗑️ FAX送付状を自動除外しました")
                if estimate_data.get('_self_corrected'):
                    info_msgs.append("🔧 自己修復ループが有効になりました")
                page_count = estimate_data.get('_page_count', 1)
                if page_count > 1:
                    info_msgs.append(f"📄 {page_count}ページ分割処理（重複除去済み）")
                tax_basis = estimate_data.get('_tax_basis', 'unknown')
                if tax_basis == 'tax_inclusive':
                    info_msgs.append("💱 税込明細を検出 → 税抜に自動変換しました")
                elif tax_basis == 'tax_exclusive':
                    info_msgs.append("✅ 税抜明細を検出")
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
            st.session_state['step'] = 3
            st.rerun()
        except Exception as e:
            progress.empty()
            st.error(f"⚠️ AI解析中にエラーが発生しました:\n\n{str(e)}")
            st.code(traceback.format_exc())
            if st.button("← ステップ①に戻る"):
                st.session_state['step'] = 1
                st.rerun()

    # =========================================
    # STEP 3: プレビュー・修正
    # =========================================
    elif current_step == 3:
        st.markdown('<div class="step-header">③ プレビュー・修正</div>', unsafe_allow_html=True)
        vehicle_data  = st.session_state.get('vehicle_data', {})
        estimate_data = st.session_state.get('estimate_data')

        if not vehicle_data:
            st.error("解析データがありません。ステップ①に戻ってください。")
            if st.button("← ステップ①に戻る"):
                st.session_state['step'] = 1
                st.rerun()
            st.stop()

        # 信頼度表示
        v_conf = safe_float(vehicle_data.get('confidence', 1.0), 1.0)
        if v_conf < CONFIDENCE_THRESHOLD:
            st.markdown(
                f'<div class="warning-box">⚠️ 車検証の読み取り信頼度: <b>{v_conf:.0%}</b> — 内容をよくご確認ください。</div>',
                unsafe_allow_html=True
            )
        if estimate_data:
            e_conf = safe_float(estimate_data.get('confidence', 1.0), 1.0)
            if e_conf < CONFIDENCE_THRESHOLD:
                st.markdown(
                    f'<div class="warning-box">⚠️ 見積書の読み取り信頼度: <b>{e_conf:.0%}</b> — 内容をよくご確認ください。</div>',
                    unsafe_allow_html=True
                )
            # 税込/税抜判定の結果表示
            tax_basis = estimate_data.get('_tax_basis', 'unknown')
            rev_match = estimate_data.get('_reverse_match', False)
            basis_label = {'tax_inclusive': '税込明細（税抜に自動変換済み）',
                           'tax_exclusive': '税抜明細',
                           'unknown':       '判定不能（手動確認推奨）'}.get(tax_basis, '')
            rev_icon = '✅ 逆算一致' if rev_match else '⚠️ 逆算不一致（金額を確認してください）'
            st.markdown(
                f'<div class="tax-box">💱 <b>税区分判定:</b> {basis_label} ／ {rev_icon}</div>',
                unsafe_allow_html=True
            )

        # 車両情報（修正可能なフォーム）
        st.subheader("🚗 車両・顧客情報")
        col1, col2 = st.columns(2)
        with col1:
            v_customer = st.text_input("使用者名",    value=safe_str(vehicle_data.get('customer_name', '')),    key='v_customer')
            v_owner    = st.text_input("所有者名",    value=safe_str(vehicle_data.get('owner_name', '')),       key='v_owner')
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
            v_carname = st.text_input("車名",          value=safe_str(vehicle_data.get('car_name', '')),             key='v_carname')
        col3, col4, col5 = st.columns(3)
        with col3:
            v_km      = st.number_input("走行距離 (km)", value=safe_int(vehicle_data.get('kilometer', 0)), min_value=0, step=1000, key='v_km')
        with col4:
            v_term    = st.text_input("有効期限 (YYYYMMDD)",   value=safe_str(vehicle_data.get('term_date', '')),    key='v_term')
        with col5:
            v_regdate = st.text_input("初度登録年月 (YYYYMM00)", value=safe_str(vehicle_data.get('car_reg_date', '')), key='v_regdate')

        # 車両詳細情報
        st.subheader("🔧 車両詳細")
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

        # 見積明細
        calc_parts = 0
        calc_wages = 0
        pdf_parts  = 0
        pdf_wages  = 0
        if estimate_data and estimate_data.get('items'):
            st.markdown("---")
            st.subheader("📋 見積明細（直接編集可能）")
            items = estimate_data['items']
            edit_rows = []
            for i, item in enumerate(items):
                edit_rows.append({
                    'No': i + 1,
                    '品名': str(item.get('name', '')),
                    '作業': str(item.get('method', '')),
                    '数量': safe_int(item.get('quantity', 1), 1),
                    '部品金額': safe_int(item.get('parts_amount', 0)),
                    '工賃': safe_int(item.get('wage', 0)),
                })
            df = pd.DataFrame(edit_rows)
            edited_df = st.data_editor(
                df,
                use_container_width=True,
                hide_index=True,
                num_rows="dynamic",
                column_config={
                    'No': st.column_config.NumberColumn('No', disabled=True, width='small'),
                    '品名': st.column_config.TextColumn('品名', width='large'),
                    '作業': st.column_config.TextColumn('作業'),
                    '数量': st.column_config.NumberColumn('数量', min_value=1, step=1),
                    '部品金額': st.column_config.NumberColumn('部品金額', step=1, format="¥%d"),
                    '工賃': st.column_config.NumberColumn('工賃', step=1, format="¥%d"),
                },
                key='items_editor',
            )
            # 編集後データを反映（No列を自動採番）
            edited_df['No'] = range(1, len(edited_df) + 1)
            edited_items = []
            for _, row in edited_df.iterrows():
                name_val = row.get('品名', '')
                if pd.isna(name_val):
                    name_val = ''
                method_val = row.get('作業', '')
                if pd.isna(method_val):
                    method_val = ''
                edited_items.append({
                    'name': str(name_val),
                    'method': str(method_val),
                    'quantity': safe_int(row.get('数量', 1), 1),
                    'parts_amount': safe_int(row.get('部品金額', 0)),
                    'wage': safe_int(row.get('工賃', 0)),
                })
            estimate_data['items'] = edited_items
            st.session_state['estimate_data'] = estimate_data
            for it in edited_items:
                calc_parts += safe_int(it.get('parts_amount', 0))
                calc_wages += safe_int(it.get('wage', 0))
            sp = safe_int(estimate_data.get('short_parts_wage', 0))
            est_exempt = safe_int(estimate_data.get('tax_exempt_amount', 0))
            if sp > 0:
                st.info(f"🔧 ショートパーツ（雑品代）: ¥{sp:,}（Expense自動振り分け済み）")
            if est_exempt > 0:
                st.info(f"🏷️ 非課税費用（預託/廃棄処分等）: ¥{est_exempt:,}（Expense自動振り分け済み）")
                # 非課税費用をサイドバーのexp_exemptに自動加算
                current_exempt = st.session_state.get('exp_exempt', 0)
                if current_exempt == 0:
                    st.session_state['exp_exempt'] = est_exempt
            pdf_parts = safe_int(estimate_data.get('pdf_parts_total', 0))
            pdf_wages = safe_int(estimate_data.get('pdf_wage_total', 0))
            st.markdown("---")
            st.subheader("💰 金額サマリー")
            scol1, scol2, scol3 = st.columns(3)
            rev_match = estimate_data.get('_reverse_match', False)

            # 金額差額の計算
            parts_diff = calc_parts - pdf_parts if pdf_parts > 0 else 0
            # SP込みでも一致チェック
            parts_match_sp = (calc_parts + sp == pdf_parts) if pdf_parts > 0 else False
            parts_match = (calc_parts == pdf_parts) or parts_match_sp
            wage_diff = calc_wages - pdf_wages if pdf_wages > 0 else 0
            wage_match = (calc_wages == pdf_wages)
            has_discrepancy = False

            with scol1:
                st.metric("部品合計（税抜）", f"¥{calc_parts:,}")
                if pdf_parts > 0 and not parts_match and not rev_match:
                    has_discrepancy = True
                    st.markdown(
                        f'<div class="error-box">⚠️ <b>部品相違</b>: PDF ¥{pdf_parts:,} ≠ 計算 ¥{calc_parts:,}（差額: {parts_diff:+,}円）</div>',
                        unsafe_allow_html=True
                    )
                elif pdf_parts > 0:
                    st.markdown('<div class="success-box">✅ PDF金額と一致</div>', unsafe_allow_html=True)
            with scol2:
                st.metric("工賃合計（税抜）", f"¥{calc_wages:,}")
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
                sub   = calc_parts + calc_wages + sp + exp_tow + exp_ren
                tax   = round(sub * TAX_RATE)
                total = sub + tax + exp_exm
                st.metric("合計（税込）", f"¥{total:,}")
                if rev_match:
                    st.markdown('<div class="success-box">✅ 逆算一致</div>', unsafe_allow_html=True)

            # 金額検証アラート（差額1,000円以上で赤字警告）
            DISCREPANCY_THRESHOLD = 1000
            if has_discrepancy and (abs(parts_diff) >= DISCREPANCY_THRESHOLD or abs(wage_diff) >= DISCREPANCY_THRESHOLD):
                st.markdown("---")
                st.markdown(
                    '<div class="error-box" style="border: 3px solid #dc3545; font-size: 1.1rem;">'
                    '🚨 <b>金額不一致警告</b><br>'
                    'AI読み取り金額とPDF記載金額に大きな差があります。<br>'
                    '明細行の内容を確認・修正してから生成してください。<br>'
                    '<b>確認後、下のチェックボックスにチェックを入れてから生成してください。</b></div>',
                    unsafe_allow_html=True
                )
                amount_confirmed = st.checkbox(
                    "金額の差異を確認しました。このまま生成を続行します。",
                    value=False,
                    key='amount_confirmed'
                )
            else:
                amount_confirmed = True  # 差額なし or 閾値以下

        else:
            amount_confirmed = True
            if not estimate_data:
                st.info("💡 見積書なし — 車両情報のみのNEOファイルを作成します")

        # NEO生成ボタン
        st.markdown("---")
        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← ステップ①に戻る", use_container_width=True):
                st.session_state['step'] = 1
                st.session_state['vehicle_data']  = None
                st.session_state['estimate_data'] = None
                st.rerun()
        with bcol2:
            gen_disabled = not amount_confirmed
            if st.button("📦 NEOファイルを生成する →", type="primary", use_container_width=True, disabled=gen_disabled):
                st.session_state['updated_vehicle'] = updated_vehicle
                st.session_state['calc_parts']      = calc_parts
                st.session_state['calc_wages']      = calc_wages
                st.session_state['pdf_parts']       = pdf_parts if estimate_data else None
                st.session_state['pdf_wages']       = pdf_wages if estimate_data else None
                st.session_state['step'] = 4
                st.rerun()
            if gen_disabled:
                st.caption("⬆️ 金額差異を確認してチェックを入れてください")

    # =========================================
    # STEP 4: NEO生成・ダウンロード
    # =========================================
    elif current_step == 4:
        st.markdown('<div class="step-header">④ NEOファイル生成</div>', unsafe_allow_html=True)
        updated_vehicle = st.session_state.get('updated_vehicle', {})
        estimate_data   = st.session_state.get('estimate_data')
        calc_parts      = st.session_state.get('calc_parts', 0)
        calc_wages      = st.session_state.get('calc_wages', 0)
        pdf_parts       = st.session_state.get('pdf_parts')
        pdf_wages       = st.session_state.get('pdf_wages')
        insurance_info  = {
            'policy_no':        st.session_state.get('policy_no', ''),
            'contractor_name':  st.session_state.get('contractor_name', ''),
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
        try:
            progress.progress(30, text="📦 テンプレートを処理中...")
            neo_data, total_parts, total_wages, grand_total = generate_neo_file(
                template_data, updated_vehicle, items, short_parts_wage, insurance_info,
                expenses=expense_info
            )
            progress.progress(80, text="📝 ファイル名を生成中...")
            filename = generate_filename(
                updated_vehicle, calc_parts, calc_wages, pdf_parts, pdf_wages,
                has_estimate, reverse_match, short_parts_wage
            )
            st.session_state['neo_bytes']    = neo_data
            st.session_state['neo_filename'] = filename
            progress.progress(100, text="✅ 生成完了！")

            st.markdown('<div class="success-box">✅ NEOファイルの生成が完了しました！</div>', unsafe_allow_html=True)
            st.markdown(f"**ファイル名:** `{filename}`")
            st.markdown(f"**ファイルサイズ:** {len(neo_data):,} bytes")
            if has_estimate:
                st.markdown(f"**明細行数:** {len(items)} 行")
                st.markdown(f"**合計金額:** ¥{grand_total:,}（税込）")
                discrepancies = []
                if not reverse_match:
                    sp_val = safe_int(short_parts_wage)
                    parts_match = (calc_parts == pdf_parts) or (calc_parts + sp_val == pdf_parts)
                    if pdf_parts is not None and pdf_parts > 0 and not parts_match:
                        discrepancies.append(f"部品相違（PDF: ¥{pdf_parts:,} / 計算: ¥{calc_parts:,}）")
                    if pdf_wages is not None and pdf_wages > 0 and calc_wages != pdf_wages:
                        discrepancies.append(f"工賃相違（PDF: ¥{pdf_wages:,} / 計算: ¥{calc_wages:,}）")
                else:
                    st.markdown('<div class="success-box">✅ 逆算一致 — 金額は正常です</div>', unsafe_allow_html=True)
                for d in discrepancies:
                    st.markdown(f'<div class="warning-box">⚠️ {d}</div>', unsafe_allow_html=True)
            else:
                st.markdown("**内容:** 車両情報のみ（明細なし）")

            st.markdown("---")
            st.download_button(
                label="📥 NEOファイルをダウンロード",
                data=neo_data,
                file_name=filename,
                mime="application/octet-stream",
                type="primary",
                use_container_width=True
            )
            st.markdown("")
            if st.button("🔄 新しい見積を作成する", use_container_width=True):
                for key in [
                    'step', 'vehicle_data', 'estimate_data', 'neo_bytes', 'neo_filename',
                    'vehicle_file_bytes', 'vehicle_file_name', 'estimate_file_bytes',
                    'estimate_file_name', 'updated_vehicle', 'calc_parts', 'calc_wages',
                    'pdf_parts', 'pdf_wages',
                    'policy_no', 'contractor_name',
                    'exp_towing', 'exp_rental', 'exp_exempt',
                ]:
                    if key in st.session_state:
                        del st.session_state[key]
                st.session_state['step'] = 1
                st.rerun()
        except Exception as e:
            progress.empty()
            st.error(f"⚠️ NEO生成中にエラーが発生しました:\n\n{str(e)}")
            st.code(traceback.format_exc())
            if st.button("← ステップ③に戻る"):
                st.session_state['step'] = 3
                st.rerun()


if __name__ == '__main__':
    main()
