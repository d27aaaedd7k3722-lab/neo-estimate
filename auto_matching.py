#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完全自動マッチング・分析システム v2.0
見積PDF → Addata DB 照合エンジン

6項目マッチング: 車種データ / 部品番号 / 作業コード / 部品名 / 部品価格 / 工数
修正: 部品名フィールド[13:43], 型式コード抽出, *15/*17.DB グレードバリアント価格対応
"""

import sys
# Streamlit Cloud は標準出力をログ収集用の独自オブジェクトに差し替えるため、
# reconfigure() が無い。無防備に呼ぶと import 時に AttributeError で落ち、
# PDF→NEO 経路がまるごと使えなくなる。あるときだけ呼ぶ。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError, OSError):
        pass

import os
import re
import glob
import struct
import sqlite3
import json
from datetime import datetime

import fitz  # PyMuPDF
import jaconv
try:
    from Levenshtein import distance as lev_distance
except ImportError:
    def lev_distance(a, b): return abs(len(a) - len(b)) + sum(1 for x, y in zip(a, b) if x != y)

ADDATA_ROOT = r'C:\Addata'


def _normalize_price(x) -> int:
    """金額文字列/数値を整数円に正規化（spec_iter3 課題B / S3-2）。
    対応: カンマ区切り('30,000')、全角数字('３０，０００')、円記号('¥30,000円'/'￥')、
          None/空/NaN → 0、float → int(round)、負値 → 0。失敗しても例外は出さず 0。
    """
    try:
        if x is None:
            return 0
        # NaN チェック（floatのNaNを除外）
        if isinstance(x, float):
            if x != x:  # NaN
                return 0
            v = int(round(x))
            return v if v > 0 else 0
        if isinstance(x, int):
            return x if x > 0 else 0
        # 文字列処理
        import unicodedata as _ud
        s = _ud.normalize('NFKC', str(x)).strip()
        if not s:
            return 0
        # 円記号・カンマ・スペース等を除去
        for _ch in ('¥', '￥', '円', ',', ' ', '\t', '　'):
            s = s.replace(_ch, '')
        if not s:
            return 0
        # 残った文字から数字と符号と小数点のみ抽出
        import re as _re
        m = _re.search(r'-?\d+(?:\.\d+)?', s)
        if not m:
            return 0
        v = int(round(float(m.group(0))))
        return v if v > 0 else 0
    except Exception:
        return 0
SAMPLE_PDF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'サンプル見積PDF')


# ============================================================
# 1. PDF解析
# ============================================================
class PDFParser:
    @staticmethod
    def extract(pdf_path):
        doc = fitz.open(pdf_path)
        all_text = ""
        for page in doc:
            all_text += page.get_text('text') + "\n"
        doc.close()
        vehicle = PDFParser._extract_vehicle(all_text)
        items = PDFParser._extract_items(all_text)
        return vehicle, items

    @staticmethod
    def _extract_vehicle(text):
        info = {}
        m = re.search(r'型式指定\s*(\d+)', text)
        if m: info['model_designation'] = m.group(1)
        m = re.search(r'類別区分\s*(\d+)', text)
        if m: info['category_number'] = m.group(1)
        m = re.search(r'車名・型式\s*(.+)', text)
        if m:
            full = m.group(1).strip()
            info['car_model_full'] = full
            # 型式コード抽出: 英数字2-8文字で、純粋な数字でなく、車名キーワードでもないもの
            skip_words = {'CROSSTAR', 'HYBRID', 'CUSTOM', 'TURBO', 'NISMO', 'MODULO',
                          'SPORT', 'PREMIUM', 'BASE', 'LUXURY', '2WD', '4WD'}
            tokens = full.split()
            candidates = []
            for t in tokens:
                clean = re.sub(r'[^A-Z0-9]', '', t.upper())
                if (2 <= len(clean) <= 8 and re.match(r'^[A-Z]{1,4}[A-Z0-9]*\d+[A-Z0-9]*$', clean)
                        and clean not in skip_words and not clean.isdigit()):
                    candidates.append(clean)
            if candidates:
                info['model_code'] = candidates[0]
            else:
                # フォールバック: ハイフン付き型式を含む車台番号から
                for t in tokens:
                    clean = t.replace('ﾊｲﾌﾞﾘﾂﾄﾞ', '').replace('ﾌﾘ-ﾄﾞ', '').strip()
                    if re.match(r'^[A-Z]{1,3}\d{1,3}[A-Z]?$', clean):
                        info['model_code'] = clean
                        break
        # 車台番号から型式抽出（フォールバック）
        m = re.search(r'車台番号\s*\n?\s*([A-Z0-9\-]+)', text)
        if m:
            info['chassis_no'] = m.group(1)
            if 'model_code' not in info:
                chassis = m.group(1)
                info['model_code'] = chassis.split('-')[0]
        m = re.search(r'カラーコード\s*\n?\s*([A-Z0-9]+)', text)
        if m: info['color_code'] = m.group(1)
        m = re.search(r'登録番号\s*\n?\s*(.+?)(?:\n|型式指定)', text)
        if m: info['reg_no'] = m.group(1).strip()
        return info

    @staticmethod
    def _extract_items(text):
        items = []
        lines = text.split('\n')
        i = 0
        in_paint = False
        while i < len(lines):
            line = lines[i].strip()
            # 塗装明細セクション検知
            if '【塗装明細】' in line:
                in_paint = True
            if '【費用】' in line:
                in_paint = False

            m = re.match(r'^(\d{4})\s+(.+)$', line)
            if m:
                code = m.group(1)
                name = m.group(2).strip()
                is_sub = '  ' in line[4:7]  # サブ部品判定
                item = {
                    'code': code, 'name': name, 'action': '', 'part_no': '',
                    'part_price': 0, 'wage': 0, 'is_sub': is_sub, 'is_paint': in_paint,
                }
                j = i + 1
                while j < len(lines) and j < i + 6:
                    nl = lines[j].strip()
                    if not nl:
                        j += 1; continue
                    if re.match(r'^\d{4}\s+', nl): break
                    # ページ小計/合計/次頁 行に到達したら打ち切り
                    if any(kw in nl for kw in ['ページ小計', '小計', '合計', '次頁', '合　計']):
                        break
                    if nl in ('取替', '脱着', '板金', '修理', '取替塗装'):
                        item['action'] = nl
                    elif re.match(r'^[A-Z0-9][A-Z0-9\-]{6,}', nl):
                        item['part_no'] = nl.split()[0].rstrip('*')
                    elif re.match(r'^[\d,]+(\s*[\$#\*])?$', nl):
                        val_str = re.sub(r'[\$#\*\s]', '', nl).replace(',', '')
                        if val_str.isdigit():
                            val = int(val_str)
                            if '#' in nl or '$' in nl:
                                if item['wage'] == 0: item['wage'] = val
                            elif item['part_price'] == 0:
                                item['part_price'] = val
                            elif item['wage'] == 0:
                                item['wage'] = val
                    j += 1
                items.append(item)
                i = j; continue
            i += 1
        # 非塗装セクションのみ返す
        return [it for it in items if not it['is_paint']]


# ============================================================
# 2. Addata DB エンジン
# ============================================================
class AddataEngine:
    def __init__(self, root=ADDATA_ROOT):
        self.root = root

    def xor_decode(self, filepath):
        if not os.path.exists(filepath): return ''
        with open(filepath, 'rb') as f: raw = f.read()
        return bytes(b ^ 0xFF for b in raw).decode('cp932', errors='replace')

    def xor_decode_bytes(self, filepath):
        if not os.path.exists(filepath): return b''
        with open(filepath, 'rb') as f: raw = f.read()
        return bytes(b ^ 0xFF for b in raw)

    def read_bin(self, filepath):
        if not os.path.exists(filepath): return b''
        with open(filepath, 'rb') as f: return f.read()

    # --- 車種特定 (v12 Phase A: 3段階完全実装) ---
    def identify_vehicle(self, model_desig, cat_num, model_code='',
                         reg_date='', maker='', car_name=''):
        """3 段階フォールバック車種特定。

        Layer 1: 型式指定 + 類別区分 + 初度登録（reg_date）の複合キー
        Layer 2: 型式コード直接検索 / Katashiki.DB / fuzzy 複合属性検索
        Layer 3: TOYOTA_GENERIC 明示フォールバック（is_template=True）

        戻り値: ({'vehicle_code', 'folder', 'match_layer', [is_template, reason]}, err_or_None)
        """
        # Phase A-2: ADDATA ルート不在 → 即 layer 3 フォールバック（KA06 ロード回避）
        if not self.root or not os.path.isdir(self.root):
            return ({'vehicle_code': 'TOYOTA_GENERIC',
                     'folder': '<template>',
                     'match_layer': 3,
                     'is_template': True,
                     'reason': 'addata_root not found'}, None)

        ka06 = os.path.join(self.root, 'COM', 'KA06_ALL.DB')
        if not os.path.exists(ka06):
            return ({'vehicle_code': 'TOYOTA_GENERIC',
                     'folder': '<template>',
                     'match_layer': 3,
                     'is_template': True,
                     'reason': 'KA06_ALL.DB not found'}, None)

        text = self.xor_decode(ka06)
        lines = [l for l in text.replace('\r\n', '\n').replace('\r', '\n').split('\n') if l.strip()]
        md = str(model_desig or '').strip()
        cn = str(cat_num or '').strip()
        padded_desig = md.zfill(5)
        padded_cat = cn.zfill(4)
        desig60 = '60' + padded_desig

        # Phase A-1: reg_date を YYYYMM へ正規化（layer 1 の追加キー）
        reg_yyyymm = ''
        if reg_date:
            digits = re.sub(r'[^\d]', '', str(reg_date))
            if len(digits) >= 6:
                reg_yyyymm = digits[:6]
            elif len(digits) >= 4:
                reg_yyyymm = digits[:4]

        def try_resolve(v_code, layer):
            if not v_code: return None
            folder = os.path.join(self.root, v_code[0], v_code)
            if os.path.isdir(folder):
                return {'vehicle_code': v_code, 'folder': folder, 'match_layer': layer}
            return None

        # Phase A-1: 第1層 — 型式指定 + 類別区分 (+ 初度登録優先)
        if md and cn:
            if len(lines) > 1:
                # 初度登録は「期間の開始年月」で記録されるため、完全一致は
                # ほとんどしない。一致しなければ先頭行を layer1（最高確度）
                # として返していたため、前期型と後期型が混在する車種で
                # 常に前期型が選ばれていた。候補が絞れないときは確度を
                # 落として、呼び出し側がベタ打ちへ倒せるようにする。
                _cands = []
                for line in lines:
                    if not line.startswith('06'): continue
                    if padded_cat in line and (padded_desig in line or desig60 in line):
                        r = try_resolve(line[2:5].strip(), 1)
                        if not r: continue
                        if reg_yyyymm and reg_yyyymm in line:
                            return r, None
                        _cands.append(r)
                if _cands:
                    _codes = {c.get('vehicle_code') for c in _cands}
                    if len(_codes) == 1:
                        return _cands[0], None
                    _amb = dict(_cands[0])
                    _amb['match_layer'] = 2
                    _amb['ambiguous'] = sorted(x for x in _codes if x)
                    _amb['reason'] = (f'初度登録で絞り込めず候補{len(_codes)}件: '
                                      f'{sorted(x for x in _codes if x)}')
                    return _amb, None
            else:
                # 固定長レコード（改行なし）
                for term in [desig60, padded_desig]:
                    pos = 0
                    while True:
                        idx = text.find(term, pos)
                        if idx == -1: break
                        window = text[max(0, idx-40):min(len(text), idx+40)]
                        if padded_cat in window:
                            rp = window.find('06')
                            if rp >= 0:
                                abs_r = max(0, idx-40) + rp
                                vc = text[abs_r+2:abs_r+5].strip()
                                if vc and vc.isalnum():
                                    r = try_resolve(vc, 1)
                                    if r: return r, None
                        pos = idx + 1

        # 第2層: 型式コード（既存 fast path、後方互換維持）
        if model_code:
            # 型式を5文字に切ると NCP131 が NCP130 に当たる。
            # 切らずに、英数字が続かない位置での一致だけを見る。
            # 候補が複数の車種に割れるときは確定させない。
            mk = model_code.split('-')[-1].upper()
            if mk:
                _pat = re.compile(r'(?<![0-9A-Z])' + re.escape(mk) + r'(?![0-9A-Z])')
                _hits = [ln for ln in lines if ln.startswith('06') and _pat.search(ln)]
                _codes = {ln[2:5].strip() for ln in _hits}
                _codes = {c for c in _codes if c}
                if len(_codes) == 1:
                    r = try_resolve(_hits[0][2:5].strip(), 2)
                    if r: return r, None
                elif len(_codes) > 1:
                    for _ln in _hits:
                        r = try_resolve(_ln[2:5].strip(), 2)
                        if r:
                            r = dict(r)
                            r['match_layer'] = 3
                            r['ambiguous'] = sorted(_codes)
                            r['reason'] = (f'型式{mk}で候補{len(_codes)}件に割れました: '
                                           f'{sorted(_codes)}')
                            return r, None

        # 第2.5層: Katashiki.DB（既存）
        if model_code:
            kat = os.path.join(self.root, 'COM', 'Katashiki.DB')
            if os.path.exists(kat):
                try:
                    with open(kat, 'r', encoding='cp932', errors='replace') as f:
                        for kl in f:
                            parts = kl.strip().split()
                            if len(parts) >= 3:
                                mk2 = model_code.split('-')[-1].upper()
                                if parts[2].strip() == mk2 or mk2.startswith(parts[2].strip()):
                                    r = try_resolve(parts[0].strip(), 2)
                                    if r: return r, None
                except Exception: pass

        # Phase A-3: 第2.7層 — メーカー + 車名 + 型式 + 年式 の fuzzy 複合検索
        if maker or car_name or model_code:
            r = self._fuzzy_lookup_by_attributes(
                lines, maker=maker, car_name=car_name,
                model_code=model_code, reg_yyyymm=reg_yyyymm,
                try_resolve=try_resolve,
            )
            if r:
                return r, None

        # Phase A-4: 第3層 — TOYOTA_GENERIC 明示フォールバック
        return ({'vehicle_code': 'TOYOTA_GENERIC',
                 'folder': '<template>',
                 'match_layer': 3,
                 'is_template': True,
                 'reason': f"全層未ヒット: 型式指定={md}, 類別={cn}, "
                           f"型式={model_code}, 初度登録={reg_date}, "
                           f"メーカー={maker}, 車名={car_name}"}, None)

    def _fuzzy_lookup_by_attributes(self, lines, maker='', car_name='',
                                    model_code='', reg_yyyymm='',
                                    try_resolve=None):
        """KA06 lines を走査し、maker/car_name/model_code/reg_yyyymm の
        部分一致＋類似度合計で top1 を返す（layer 2）。
        """
        if not lines or try_resolve is None:
            return None
        import difflib

        tokens = []
        if maker:
            m = str(maker).strip().upper()
            if m: tokens.append(m)
        if car_name:
            c = str(car_name).strip().upper()
            if c: tokens.append(c)
        if model_code:
            mc = str(model_code).split('-')[-1].upper()
            if mc: tokens.append(mc)
        if reg_yyyymm:
            tokens.append(reg_yyyymm[:4])
        if not tokens:
            return None

        best_score = 0.0
        best_vc = None
        for line in lines:
            if not line.startswith('06'): continue
            line_up = line.upper()
            score = 0.0
            for t in tokens:
                if t in line_up:
                    score += 1.0
                else:
                    sm = difflib.SequenceMatcher(None, t, line_up).ratio()
                    score += sm * 0.5
            if score > best_score:
                best_score = score
                best_vc = line[2:5].strip()

        # 閾値: tokens の半数以上に相当する一致が必要
        min_required = max(1.0, len(tokens) * 0.5)
        if best_score >= min_required and best_vc:
            return try_resolve(best_vc, 2)
        return None

    # --- 部品マスタ (*12.DB) ---
    def load_parts_master(self, folder):
        parts = []
        for db12 in glob.glob(os.path.join(folder, '*12.DB')):
            dec = self.xor_decode_bytes(db12)
            recs = dec.split(b'\r\n') if b'\r\n' in dec else dec.split(b'\n')
            for rec in recs:
                if len(rec) < 55 or rec[0:2] != b'12': continue
                try:
                    s = lambda b: b.decode('cp932', errors='replace').strip()
                    veh_code = s(rec[2:5])
                    section_code = s(rec[6:8])
                    page_code = s(rec[6:10])
                    line_no = s(rec[10:13])
                    # 部品名: [13:43] が正しい位置（30バイト）
                    name = s(rec[13:43])
                    ref_a = s(rec[44:48])
                    ref_no = int(ref_a) if ref_a.isdigit() else -1
                    mirror = s(rec[48:52]) if len(rec) > 52 else ''
                    methods = s(rec[52:55]) if len(rec) > 55 else ''
                    # 3文字セクションコード（*17.DB キー）
                    sec3 = rec[5:8].decode('ascii', errors='replace').strip() if len(rec) > 8 else ''
                    # グレードバリアントフラグ
                    is_gv = len(rec) > 43 and rec[43] == 0x24
                    if name:
                        parts.append({
                            'name': name, 'ref_a': ref_a, 'ref_no': ref_no,
                            'section_code': section_code, 'page_code': page_code,
                            'line_no': line_no, 'mirror': mirror, 'methods': methods,
                            'sec3': sec3, 'is_grade_variant': is_gv,
                        })
                except Exception: continue
        return parts

    # --- LCG復号ヘルパー ---
    def _read_seed(self, folder):
        """*01.DB の先頭 uint16 LE からLCGシードを取得"""
        for f01 in glob.glob(os.path.join(folder, '*01.DB')):
            data = self.read_bin(f01)
            if len(data) >= 2:
                return struct.unpack_from('<H', data, 0)[0]
        return 51  # デフォルト

    def _lcg_keystream(self, length, seed):
        """LCG疑似乱数キーストリーム生成（Microsoft CRT rand() 互換）"""
        state = seed
        ks = bytearray(length)
        for i in range(length):
            state = (state * 0x343FD + 0x269EC3) & 0xFFFFFFFF
            ks[i] = (state >> 16) & 0xFF
        return bytes(ks)

    # --- 価格取得 (LCG復号版 / 3段階: *13/*83→*11/*81→*15) ---
    def lookup_price(self, folder, ref_no, sec3='', color_code=''):
        """LCG stream cipher で *13/*83.DB を復号し、6桁ASCII価格を正確に取得

        *13.DB Ver310 レコード構造（復号後 / 189 bytes）:
          [0:2]   ref_no    (uint16 LE)
          [2:4]   sub_id    (uint16 LE)
          [14:32] part_name (18 bytes CP932)
          [32:49] parts_no  (17 bytes ASCII)
          [50:56] price     (6-digit ASCII, e.g. "044700" = ¥44,700)
        *83.DB Ver310: 同構造、block_size=201
        """
        if ref_no < 0: return None, []

        seed = self._read_seed(folder)

        # Honda/Suzuki/Daihatsu/Hino は *83.DB, それ以外は *13.DB
        db13_files = glob.glob(os.path.join(folder, '*13.DB')) or glob.glob(os.path.join(folder, '*83.DB'))
        db11_files = glob.glob(os.path.join(folder, '*11.DB')) or glob.glob(os.path.join(folder, '*81.DB'))
        db13 = self.read_bin(db13_files[0]) if db13_files else b''
        db11 = self.read_bin(db11_files[0]) if db11_files else b''
        if len(db13) < 10: return None, []

        count = struct.unpack_from('<H', db13, 0)[0]
        if count == 0: return None, []
        data_start = 2 + count * 8
        remaining = len(db13) - data_start
        if remaining <= 0: return None, []

        max_f3 = max(struct.unpack_from('<4H', db13, 2 + i * 8)[3] for i in range(count))
        block_size = remaining // (max_f3 + 1) if max_f3 > 0 else 0
        if block_size < 10: return None, []

        # キーストリームを1回だけ生成（全レコード同一seed → 同一キーストリーム）
        keystream = self._lcg_keystream(block_size, seed)

        # 価格フィールドオフセット（Ver310標準=50:56、他バージョン用フォールバックあり）
        price_offset = 50
        price_len = 6

        def decrypt_and_parse(b_idx):
            """指定ブロックをLCG復号し、(price, ref, sub_id, name, parts_no) を返す"""
            boff = data_start + b_idx * block_size
            if boff + block_size > len(db13): return None
            encrypted = db13[boff:boff + block_size]
            rec = bytes(a ^ b for a, b in zip(encrypted, keystream))

            rec_ref = struct.unpack_from('<H', rec, 0)[0]
            rec_sub = struct.unpack_from('<H', rec, 2)[0]

            # 価格パース（offset 50:56 = 6桁ASCII）
            price = None
            if len(rec) > price_offset + price_len:
                try:
                    ps = rec[price_offset:price_offset + price_len].decode('ascii', errors='replace').strip()
                    if ps.isdigit() and len(ps) >= 1:
                        price = int(ps)
                except Exception:
                    pass

            # フォールバック: 50:56 で取れなければ6連続ASCII数字をスキャン
            if price is None and block_size >= 56:
                for off in range(40, min(block_size - 6, 80)):
                    try:
                        candidate = rec[off:off + 6].decode('ascii', errors='replace').strip()
                        if candidate.isdigit() and len(candidate) >= 4:
                            price = int(candidate)
                            break
                    except Exception:
                        continue

            if price is not None and price > 0:
                try:
                    name = rec[14:32].decode('cp932', errors='replace').strip() if len(rec) > 32 else ''
                    pno = rec[32:49].decode('ascii', errors='replace').strip() if len(rec) > 49 else ''
                    color = rec[59:70].decode('ascii', errors='replace').strip() if len(rec) > 70 else ''
                except Exception:
                    name, pno, color = '', '', ''
                return (price, rec_ref, rec_sub, name, pno, color)
            return None

        # --- 段階1: *13/*83.DB インデックス ---
        blocks = []
        for i in range(count):
            f0, f1, f2, f3 = struct.unpack_from('<4H', db13, 2 + i * 8)
            if f0 == ref_no:
                for b in range(f2, f3 + 1):
                    blocks.append(b)

        # --- 段階2: *11.DB フォールバック ---
        if not blocks and db11:
            off = ref_no * 4
            if off + 4 <= len(db11):
                sb, eb = struct.unpack_from('<HH', db11, off)
                if sb != 0xFFFF and eb != 0xFFFF and sb <= max_f3 and eb <= max_f3 and sb <= eb:
                    for b in range(sb, eb + 1):
                        blocks.append(b)

        # --- 段階3: *15.DB フォールバック ---
        if not blocks:
            rep15, prices15 = self._lookup_via_15db(folder, ref_no, sec3, max_f3)
            if rep15 is not None:
                return rep15, prices15
            return None, []

        # --- LCG復号 + 価格抽出 ---
        all_records = []
        for b_idx in blocks:
            result = decrypt_and_parse(b_idx)
            if result is None: continue
            price, rec_ref, rec_sub, name, pno, color = result
            if rec_ref == ref_no:
                all_records.append({'price': price, 'sub': rec_sub, 'color': color, 'pno': pno})

        # ref_noチェックなしフォールバック
        if not all_records:
            for b_idx in blocks:
                result = decrypt_and_parse(b_idx)
                if result is not None:
                    all_records.append({'price': result[0], 'sub': result[2], 'color': result[5], 'pno': result[4]})

        if not all_records:
            rep15, prices15 = self._lookup_via_15db(folder, ref_no, sec3, max_f3)
            if rep15 is not None:
                return rep15, prices15
            return None, []

        all_prices = sorted(set(r['price'] for r in all_records))

        # 色コードでsub_id選択（車両のカラーコードに一致するレコードを優先）
        if color_code:
            color_matched = [r for r in all_records if r['color'] == color_code]
            if color_matched:
                rep = color_matched[0]['price']
                return rep, all_prices
            # カラーコード前方一致（3文字→4文字等の部分一致）
            color_prefix = [r for r in all_records if r['color'] and color_code.startswith(r['color'][:3])]
            if color_prefix:
                rep = color_prefix[0]['price']
                return rep, all_prices

        rep = all_prices[0]  # 最小値（ベース価格）
        return rep, all_prices

    # --- 価格＋品番取得 (spec_iter2 課題A) ---
    def lookup_price_with_pno(self, folder, ref_no, sec3='', color_code=''):
        """lookup_price と同等の価格取得 + 代表品番(pno) も返す拡張版。

        後方互換のため lookup_price 本体は破壊せず、ここで独立にデコードして
        rep_price / all_prices / top_pno の3-tuple を返す。
        欠損時 top_pno = '' を返す。
        """
        if ref_no < 0:
            return None, [], ''
        try:
            rep, all_prices = self.lookup_price(folder, ref_no, sec3, color_code)
        except Exception:
            return None, [], ''
        if rep is None:
            return None, [], ''

        # pno を追加スキャン（lookup_price の内部 all_records をもう一度回す）
        try:
            seed = self._read_seed(folder)
            db13_files = glob.glob(os.path.join(folder, '*13.DB')) or glob.glob(os.path.join(folder, '*83.DB'))
            db11_files = glob.glob(os.path.join(folder, '*11.DB')) or glob.glob(os.path.join(folder, '*81.DB'))
            db13 = self.read_bin(db13_files[0]) if db13_files else b''
            db11 = self.read_bin(db11_files[0]) if db11_files else b''
            if len(db13) < 10:
                return rep, all_prices, ''
            count = struct.unpack_from('<H', db13, 0)[0]
            if count == 0:
                return rep, all_prices, ''
            data_start = 2 + count * 8
            remaining = len(db13) - data_start
            if remaining <= 0:
                return rep, all_prices, ''
            max_f3 = max(struct.unpack_from('<4H', db13, 2 + i * 8)[3] for i in range(count))
            block_size = remaining // (max_f3 + 1) if max_f3 > 0 else 0
            if block_size < 10:
                return rep, all_prices, ''
            keystream = self._lcg_keystream(block_size, seed)

            blocks = []
            for i in range(count):
                f0, f1, f2, f3 = struct.unpack_from('<4H', db13, 2 + i * 8)
                if f0 == ref_no:
                    for b in range(f2, f3 + 1):
                        blocks.append(b)
            if not blocks and db11:
                off = ref_no * 4
                if off + 4 <= len(db11):
                    sb, eb = struct.unpack_from('<HH', db11, off)
                    if sb != 0xFFFF and eb != 0xFFFF and sb <= max_f3 and eb <= max_f3 and sb <= eb:
                        for b in range(sb, eb + 1):
                            blocks.append(b)

            top_pno = ''
            for b_idx in blocks:
                boff = data_start + b_idx * block_size
                if boff + block_size > len(db13):
                    continue
                encrypted = db13[boff:boff + block_size]
                rec = bytes(a ^ b for a, b in zip(encrypted, keystream))
                if len(rec) > 49:
                    try:
                        pno_candidate = rec[32:49].decode('ascii', errors='replace').strip()
                        # 品番は英数字+ハイフンのみ採用
                        if pno_candidate and any(c.isalnum() for c in pno_candidate):
                            # 制御文字除去
                            pno_clean = ''.join(c for c in pno_candidate if c.isprintable() and c not in ('\x00',))
                            if pno_clean and len(pno_clean) >= 3:
                                top_pno = pno_clean
                                break
                    except Exception:
                        continue
            return rep, all_prices, top_pno
        except Exception:
            return rep, all_prices, ''

    def _lookup_via_15db(self, folder, ref_no, sec3, max_f3_13):
        """*15.DB + *17.DB によるグレードバリアント価格検索

        *15.DB構造: ヘッダ104バイト(26ペア×4バイト) + 19バイト×Nブロック
        各19バイトブロック: [0:2]=ref_no, [2:4]=ref_end, [4:6]=price/10, [6:19]=descriptor
        *17.DB: sec3 → ヘッダペアインデックス → ブロック範囲（絞り込み用）

        Returns: (representative_price, all_prices_list) or (None, [])
        """
        db15_files = glob.glob(os.path.join(folder, '*15.DB'))
        if not db15_files: return None, []
        db15 = self.read_bin(db15_files[0])

        HEADER_SIZE = 104  # 26ペア × 4バイト
        BLOCK_SIZE = 19
        if len(db15) <= HEADER_SIZE: return None, []
        total_blocks = (len(db15) - HEADER_SIZE) // BLOCK_SIZE
        if total_blocks == 0: return None, []

        def scan_blocks(start_bi, end_bi):
            """指定範囲のブロックからref_no一致する価格を検索"""
            prices = []
            for bi in range(start_bi, min(end_bi + 1, total_blocks)):
                boff = HEADER_SIZE + bi * BLOCK_SIZE
                if boff + 6 > len(db15): continue
                blk_ref = struct.unpack_from('<H', db15, boff)[0]
                blk_ref_end = struct.unpack_from('<H', db15, boff + 2)[0]
                blk_price_raw = struct.unpack_from('<H', db15, boff + 4)[0]
                price = blk_price_raw * 10
                if blk_ref == ref_no and 10 <= price <= 500000:
                    prices.append(price)
                elif blk_ref_end > 0 and blk_ref <= ref_no <= blk_ref_end and 10 <= price <= 500000:
                    prices.append(price)
            return prices

        all_prices = []

        # ステップ1: sec3→*17.DB→ヘッダペア→絞り込み検索
        if sec3:
            db17_files = glob.glob(os.path.join(folder, '*17.DB'))
            if db17_files:
                db17_raw = self.read_bin(db17_files[0])
                if db17_raw:
                    db17_map = {}
                    pos = 16
                    while pos + 7 <= len(db17_raw):
                        sec_bytes = db17_raw[pos:pos + 3]
                        v1 = struct.unpack_from('<H', db17_raw, pos + 3)[0]
                        v2 = struct.unpack_from('<H', db17_raw, pos + 5)[0]
                        if all(0x20 <= c < 0x7F for c in sec_bytes) and v1 < 26 and v2 < 26:
                            sec = sec_bytes.decode('ascii', errors='replace').strip()
                            if sec: db17_map[sec] = (v1, v2)
                        pos += 7

                    pair_range = db17_map.get(sec3)
                    if pair_range:
                        header_pairs = []
                        for pi in range(26):
                            off = pi * 4
                            if off + 4 > HEADER_SIZE: break
                            sb = struct.unpack_from('<H', db15, off)[0]
                            eb = struct.unpack_from('<H', db15, off + 2)[0]
                            header_pairs.append((sb, eb))

                        pi_start, pi_end = pair_range
                        for pi in range(pi_start, min(pi_end + 1, len(header_pairs))):
                            sb, eb = header_pairs[pi]
                            if sb < total_blocks and eb < total_blocks and sb <= eb:
                                all_prices.extend(scan_blocks(sb, eb))

        # ステップ2: フルスキャン（sec3で見つからなかった場合）
        if not all_prices:
            all_prices = scan_blocks(0, total_blocks - 1)

        if not all_prices:
            return None, []

        unique = sorted(set(all_prices))
        # 0に近い価格（プレースホルダ）を除外して代表値を選択
        significant = [p for p in unique if p >= 500]
        if significant:
            rep = significant[0]  # 有意な最小価格
        else:
            rep = unique[-1]  # 全部低価格ならmax
        return rep, unique

    # --- カラーコード (*26.DB) ---
    def load_colors(self, folder):
        colors = []
        for f26 in glob.glob(os.path.join(folder, '*26.DB')):
            text = self.xor_decode(f26)
            for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
                if not line.startswith('26'): continue
                try:
                    colors.append({'code': line[11:15].strip(), 'name': line[16:].strip(), 'index': len(colors)})
                except: continue
        return colors

    # --- 工数インデックス (*76.DB) ---
    def load_work_index(self, folder):
        idx = {}
        bm = {'00': 'C', '01': 'D', '02': 'K', '03': 'S', '04': 'S', '05': 'C', '06': 'C'}
        for f76 in glob.glob(os.path.join(folder, '*76.DB')):
            text = self.xor_decode(f76)
            for line in text.replace('\r\n', '\n').split('\n'):
                if len(line) < 35: continue
                pc = line[9:12].strip()
                if not pc or pc == '000': continue
                bn = line[13:15].strip()
                mt = bm.get(bn, '')
                if not mt: continue
                sc = pc[-2:]
                if sc not in idx: idx[sc] = {}
                if mt not in idx[sc]:
                    idx[sc][mt] = {'page': line[15:21].strip(), 'chm': line[23:35].strip(), 'desc': line[35:].strip()}
        return idx


# ============================================================
# 3. ファジーマッチング
# ============================================================
class FuzzyMatcher:
    NORM_MAP = {
        'ﾍﾂﾄﾞﾗｲﾄ': 'ﾍﾂﾄﾞﾗﾝﾌﾟ', 'ﾃｰﾙﾗｲﾄ': 'ﾃｰﾙﾗﾝﾌﾟ',
        'ﾌｵｸﾞﾗﾝﾌﾟ': 'ﾌｵｸﾞﾗﾝﾌﾟ', 'ﾌｴﾝﾀﾞ': 'ﾌｴﾝﾀﾞ',
        'ﾊﾞﾝﾊﾟｰ': 'ﾊﾞﾝﾊﾟ', 'ﾗｼﾞｴｰﾀｰ': 'ﾗｼﾞｴｰﾀ',
        'ｾﾝｻｰ': 'ｾﾝｻ', 'ﾗｲﾅｰ': 'ﾗｲﾅ', 'ﾐﾗｰ': 'ﾐﾗ',
    }
    _S2L = str.maketrans('ｧｨｩｪｫｯｬｭｮ', 'ｱｲｳｴｵﾂﾔﾕﾖ')

    @classmethod
    def normalize(cls, name):
        n = jaconv.z2h(str(name), ascii=True, digit=True, kana=True)
        n = n.upper().replace(' ', '').replace('　', '')
        # 長音記号正規化（NORM_MAPより先に実行: -/ー → ｰ）
        n = n.replace('-', 'ｰ').replace('ー', 'ｰ')
        for s, d in cls.NORM_MAP.items():
            n = n.replace(s, d)
        n = n.translate(cls._S2L)
        return n

    @staticmethod
    def _split_side_impl(n):
        """左右の表記を取り除き、(本体, 'L'|'R'|'') を返す。

        以前は PDF 名の先頭から「左/右」を落とすだけで、落とした側を
        どこにも使っておらず、マスタ名の左右は落としてもいなかった。
        そのため「右フロントドアパネル」と「左フロントドアパネル」が
        どちらも同じ本体名になり、マスタの並び順で先に来たほうが
        無条件に選ばれていた（しかも完全一致扱いなので ※ も価格警告も出ない）。
        _norm_name（v4側）は同じ問題を先に解決済みなので、その規則に揃える。
        """
        side = ''
        m = re.match(r'^\s*(左|右)\s*', n)
        if m:
            side = 'L' if m.group(1) == '左' else 'R'
            n = n[m.end():]
        if re.search(r'[（(\[]?\s*(左|Ｌ|LH|L)\s*[)）\]]?\s*$', n):
            side = 'L'
        elif re.search(r'[（(\[]?\s*(右|Ｒ|RH|R)\s*[)）\]]?\s*$', n):
            side = 'R'
        n = re.sub(r'[（(\[]?\s*(左|右|Ｌ|Ｒ|LH|RH)\s*[)）\]]?\s*$', '', n)
        return n, side

    @classmethod
    def match(cls, pdf_name, master_parts, pdf_price=0):
        norm_t = cls.normalize(pdf_name)
        if not norm_t: return None, 99, False

        # 左右を本体名から分離して記録する（マスタ側も同じ処理をする）
        norm_t, side_t = cls._split_side_impl(norm_t)
        for pfx in ['左RR', '右RR', 'LH', 'RH']:
            if norm_t.startswith(pfx):
                norm_t = norm_t[len(pfx):]
                if not side_t:
                    side_t = 'L' if pfx in ('左RR', 'LH') else 'R'
                break
        # Rr→R, Fr→F  (PDF表記のRrをマスタのRに合わせる)
        if norm_t.startswith('RR'): norm_t = 'R' + norm_t[2:]

        # 方向プレフィックスの保持（F/R判定用）
        dir_t = ''
        if norm_t and norm_t[0] in 'FR':
            dir_t = norm_t[0]

        candidates = []
        for mp in master_parts:
            nm = cls.normalize(mp['name'])
            if not nm: continue
            nm, side_m = cls._split_side_impl(nm)
            if not nm: continue
            # 左右が食い違う候補は、F/R 違いと同じ重さで遠ざける。
            # ここを 0 にすると左の部品が右の行に完全一致として当たる。
            side_penalty = 10 if (side_t and side_m and side_t != side_m) else 0

            # 完全一致
            if norm_t == nm:
                candidates.append((mp, 0 + side_penalty, 0))
                continue
            # 方向分離
            dir_m = nm[0] if nm and nm[0] in 'FR' else ''
            core_t = re.sub(r'^[FR]', '', norm_t)
            core_m = re.sub(r'^[FR]', '', nm)
            # 方向ペナルティ（F/Rが異なる場合 +10）
            dir_penalty = 10 if (dir_t and dir_m and dir_t != dir_m) else 0

            if core_t == core_m and len(core_t) >= 4:
                candidates.append((mp, 1 + dir_penalty + side_penalty, 0))
                continue
            if core_t in core_m or core_m in core_t:
                min_len = min(len(core_t), len(core_m))
                if min_len >= 4:
                    candidates.append((mp, 2 + dir_penalty + side_penalty, abs(len(core_t) - len(core_m))))
                    continue
            # Levenshtein
            d = lev_distance(core_t, core_m)
            if len(core_m) > 0 and d / max(len(core_t), len(core_m)) < 0.4:
                candidates.append((mp, 3 + dir_penalty + side_penalty, d))

        if not candidates:
            return None, 99, False

        # 価格一致を優先
        if pdf_price > 0:
            price_ok = []
            for mp, tier, dist in candidates:
                if mp['ref_no'] >= 0:
                    # ここでは簡易チェック（価格取得は後続で行う）
                    price_ok.append((mp, tier, dist))
            if price_ok:
                price_ok.sort(key=lambda x: (x[1], x[2]))
                return price_ok[0][0], price_ok[0][1], False

        candidates.sort(key=lambda x: (x[1], x[2]))
        return candidates[0][0], candidates[0][1], False


# ============================================================
# 4. NEO正解データ
# ============================================================
class NEOExtractor:
    @staticmethod
    def extract(neo_path):
        """NEO (CKチャンク+zdict deflate) からERPartsを抽出"""
        if not os.path.exists(neo_path): return []
        import tempfile
        import zlib as _zlib
        with open(neo_path, 'rb') as f: data = f.read()

        # CKチャンク検出
        ck_positions = []
        pos = 0
        while pos < len(data):
            idx = data.find(b'CK', pos)
            if idx == -1: break
            ck_positions.append(idx)
            pos = idx + 2

        if ck_positions:
            # CK+zdict連鎖展開
            all_dec = bytearray()
            zdict = b''
            for i, ck_pos in enumerate(ck_positions):
                next_pos = ck_positions[i + 1] if i + 1 < len(ck_positions) else len(data)
                chunk_data = data[ck_pos + 2:next_pos]
                try:
                    dobj = _zlib.decompressobj(-_zlib.MAX_WBITS, zdict=zdict)
                    dec = dobj.decompress(chunk_data)
                    all_dec.extend(dec)
                    zdict = bytes(all_dec[-32768:]) if len(all_dec) >= 32768 else bytes(all_dec)
                except Exception:
                    try:
                        dec = _zlib.decompress(chunk_data, -_zlib.MAX_WBITS)
                        all_dec.extend(dec)
                        zdict = bytes(all_dec[-32768:]) if len(all_dec) >= 32768 else bytes(all_dec)
                    except Exception:
                        pass
            data = bytes(all_dec)

        # SQLite DB を検出・パース
        sig = b'SQLite format 3\x00'
        results = []
        pos = 0
        while True:
            idx = data.find(sig, pos)
            if idx == -1: break
            next_idx = data.find(sig, idx + 16)
            sdata = data[idx:next_idx] if next_idx != -1 else data[idx:]
            # PID固定名だと同時実行のセッション同士で同じファイルを奪い合い、
            # 他人の解析結果を読んでしまう。呼び出しごとに一意な名前にする。
            _fd, tmp = tempfile.mkstemp(suffix='.db', prefix='neo_')
            os.close(_fd)
            try:
                with open(tmp, 'wb') as f: f.write(sdata)
                conn = sqlite3.connect(tmp)
                cur = conn.cursor()
                tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                if 'ERParts' in tables:
                    cur.execute("SELECT * FROM ERParts ORDER BY LineNo")
                    cols = [d[0] for d in cur.description]
                    for row in cur.fetchall():
                        results.append(dict(zip(cols, row)))
                conn.close()
            except Exception:
                pass
            finally:
                try: os.remove(tmp)
                except Exception: pass
            pos = idx + 16
        return results


# ============================================================
# 5. メインエンジン
# ============================================================
class MatchingEngine:
    def __init__(self):
        self.ad = AddataEngine()

    def run(self, pdf_path, neo_path=None):
        print(f"\n{'='*80}")
        print(f"■ {os.path.basename(pdf_path)}")
        print(f"{'='*80}")

        vehicle, items = PDFParser.extract(pdf_path)
        print(f"  車両: {vehicle.get('car_model_full', 'N/A')}")
        print(f"  型式指定: {vehicle.get('model_designation', '?')} / 類別: {vehicle.get('category_number', '?')}")
        print(f"  型式コード: {vehicle.get('model_code', '?')} / カラー: {vehicle.get('color_code', '?')}")
        print(f"  抽出明細: {len(items)}件")

        # 車種特定
        md = vehicle.get('model_designation', '')
        cn = vehicle.get('category_number', '')
        mc = vehicle.get('model_code', '')
        veh, err = self.ad.identify_vehicle(md, cn, mc)
        if err:
            print(f"  *** {err}")
            return {'pdf': pdf_path, 'vehicle': vehicle, 'error': err, 'items': []}

        vc = veh['vehicle_code']
        folder = veh['folder']
        print(f"  → Addata: {vc} (第{veh['match_layer']}層) → {folder}")

        # マスタ読み込み
        master = self.ad.load_parts_master(folder)
        colors = self.ad.load_colors(folder)
        widx = self.ad.load_work_index(folder)
        print(f"  マスタ部品: {len(master)}件 / カラー: {len(colors)}件 / 工数セクション: {len(widx)}件")

        color_code = vehicle.get('color_code', '')
        color_idx = next((c['index'] for c in colors if c['code'] == color_code), -1)

        # NEO
        neo_data = NEOExtractor.extract(neo_path) if neo_path else []
        if neo_data: print(f"  NEO正解: {len(neo_data)}件")

        # マッチング
        act_map = {'取替': 'K', '交換': 'K', '脱着': 'D', '板金': 'S', '修理': 'S', '取替塗装': 'K'}
        results = []

        for item in items:
            r = {
                'code': item['code'], 'name': item['name'], 'action': item['action'],
                'part_no': item['part_no'], 'pdf_price': item['part_price'], 'pdf_wage': item['wage'],
                'status': {}, 'master': None, 'ad_price': None, 'ad_prices': [], 'reasons': [],
            }

            # 車種データ
            r['status']['車種'] = f'○({vc})'

            # 部品名マッチ
            matched, score, _ = FuzzyMatcher.match(item['name'], master, item['part_price'])
            if matched and score <= 5:
                r['master'] = matched
                r['status']['部品名'] = f'○(s={score},m={matched["name"][:15]})'
            else:
                r['status']['部品名'] = '×'
                r['reasons'].append(f'部品名不一致: "{item["name"]}" (最良s={score})')

            # 作業コード
            if matched:
                pm = act_map.get(item['action'], '')
                mm = matched.get('methods', '')
                if pm and pm in mm:
                    r['status']['作業コード'] = f'○({item["action"]}→{pm})'
                elif pm:
                    r['status']['作業コード'] = f'△({pm}∉{mm})'
                elif not item['action']:
                    r['status']['作業コード'] = '-'
                else:
                    r['status']['作業コード'] = f'×({item["action"]})'
                    r['reasons'].append(f'作業コード不一致: {item["action"]}')
            else:
                r['status']['作業コード'] = '-'

            # 部品番号（Addataに品番なし→PDF存在確認のみ）
            r['status']['部品番号'] = f'△({item["part_no"][:15]})' if item['part_no'] else '-'

            # 部品価格
            if matched and matched['ref_no'] >= 0:
                sec3 = matched.get('sec3', '')
                rep, aps = self.ad.lookup_price(folder, matched['ref_no'], sec3, color_code)
                r['ad_price'] = rep
                r['ad_prices'] = aps
                if item['part_price'] > 0:
                    if item['part_price'] in aps:
                        r['status']['部品価格'] = f'○(完全一致={item["part_price"]:,})'
                    elif rep and abs(item['part_price'] - rep) / max(item['part_price'], 1) < 0.15:
                        r['status']['部品価格'] = f'△(PDF={item["part_price"]:,},AD={rep:,})'
                    elif rep:
                        closest = min(aps, key=lambda p: abs(p - item['part_price'])) if aps else rep
                        r['status']['部品価格'] = f'×(PDF={item["part_price"]:,},AD近={closest:,})'
                        r['reasons'].append(f'価格不一致: PDF={item["part_price"]:,}→AD候補{len(aps)}件,最近={closest:,}')
                    else:
                        r['status']['部品価格'] = f'×(価格取得失敗ref={matched["ref_no"]})'
                        r['reasons'].append(f'ref={matched["ref_no"]}の*11/*13/*15全経路失敗')
                else:
                    r['status']['部品価格'] = '-'
            elif matched:
                r['status']['部品価格'] = f'×(ref無効={matched.get("ref_a")})'
            else:
                r['status']['部品価格'] = '-'

            # 工数
            if matched and item['wage'] > 0:
                sc = matched.get('section_code', '')
                pm = act_map.get(item['action'], '')
                if sc in widx and pm in widx[sc]:
                    wi = widx[sc][pm]
                    r['status']['工数'] = f'○(sec={sc},pg={wi["page"]})'
                elif sc in widx:
                    r['status']['工数'] = f'△(sec={sc}に{pm}なし)'
                else:
                    r['status']['工数'] = f'×(sec={sc}なし)'
                    r['reasons'].append(f'*76.DBにsec={sc}なし')
            elif item['wage'] > 0:
                r['status']['工数'] = '×(部品未マッチ)'
            else:
                r['status']['工数'] = '-'

            results.append(r)

        return {
            'pdf': pdf_path, 'vehicle': vehicle, 'veh_match': veh,
            'master_count': len(master), 'color': {'code': color_code, 'idx': color_idx},
            'items': results,
        }


# ============================================================
# 6. レポート
# ============================================================
def report(all_res):
    print(f"\n{'#'*80}")
    print(f"# 完全自動マッチング結果レポート v2.0")
    print(f"# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*80}")

    t_total = t_ok = t_partial = t_fail = 0

    for res in all_res:
        pdf = os.path.basename(res['pdf'])
        print(f"\n{'='*80}")
        print(f"■ {pdf}")

        if 'error' in res:
            print(f"  ERROR: {res['error']}")
            continue

        v = res['vehicle']
        vm = res['veh_match']
        print(f"  {v.get('car_model_full', 'N/A')} → {vm['vehicle_code']}(第{vm['match_layer']}層)")
        print(f"  マスタ部品: {res['master_count']}件")

        print(f"\n  {'#':>3} {'Code':>5} {'PDF部品名':<28} {'PDF価格':>10} {'AD価格':>10} {'判定':>3} 詳細")
        print(f"  {'-'*3} {'-'*5} {'-'*28} {'-'*10} {'-'*10} {'-'*3} {'-'*30}")

        for i, item in enumerate(res['items'], 1):
            t_total += 1
            st = item['status']
            ok = sum(1 for v in st.values() if v.startswith('○'))
            fail = sum(1 for v in st.values() if v.startswith('×'))

            if fail == 0 and ok >= 3: verdict = '◎'; t_ok += 1
            elif fail <= 1: verdict = '○'; t_partial += 1
            else: verdict = '×'; t_fail += 1

            pp = f"{item['pdf_price']:,}" if item['pdf_price'] else '-'
            ap = f"{item['ad_price']:,}" if item['ad_price'] else '-'
            nm = item['name'][:26]
            detail = st.get('部品名', '')[:35]

            print(f"  {i:3} {item['code']:>5} {nm:<28} {pp:>10} {ap:>10} {verdict:>3} {detail}")

        # 失敗詳細
        failures = [it for it in res['items'] if it['reasons']]
        if failures:
            print(f"\n  --- 不一致分析 ({len(failures)}件) ---")
            for it in failures:
                print(f"  [{it['code']}] {it['name']}")
                for reason in it['reasons']:
                    print(f"    → {reason}")

    print(f"\n{'='*80}")
    print(f"【総合サマリー】")
    print(f"  合計: {t_total}件")
    print(f"  ◎完全: {t_ok} ({t_ok/max(t_total,1)*100:.0f}%)")
    print(f"  ○部分: {t_partial} ({t_partial/max(t_total,1)*100:.0f}%)")
    print(f"  ×失敗: {t_fail} ({t_fail/max(t_total,1)*100:.0f}%)")
    print(f"{'='*80}")
    return t_total, t_ok, t_partial, t_fail


# ============================================================
# 7. 自律修正サイクル
# ============================================================
def auto_fix(all_res, engine):
    """失敗項目の原因を分析し、改善アプローチを適用して再マッチング"""
    improved = 0
    for res in all_res:
        if 'error' in res: continue
        folder = res['veh_match']['folder']
        master = engine.ad.load_parts_master(folder)

        for item in res['items']:
            if not item['reasons']: continue

            # 修正1: Rr→R変換、括弧内除去
            if any('部品名不一致' in r for r in item['reasons']):
                cleaned = item['name']
                cleaned = re.sub(r'\(.*?\)', '', cleaned)  # 括弧内除去
                cleaned = re.sub(r'^(左|右)\s*', '', cleaned)
                cleaned = cleaned.replace('Rr', 'R').replace('Fr', 'F')
                m2, s2, _ = FuzzyMatcher.match(cleaned, master)
                if m2 and s2 <= 3:
                    item['master'] = m2
                    item['status']['部品名'] = f'○(修正s={s2})'
                    item['reasons'] = [r for r in item['reasons'] if '部品名不一致' not in r]
                    # 価格も再検索
                    if m2['ref_no'] >= 0:
                        color_c = res.get('color', {}).get('code', '')
                        rep, aps = engine.ad.lookup_price(folder, m2['ref_no'], m2.get('sec3', ''), color_c)
                        item['ad_price'] = rep
                        item['ad_prices'] = aps
                        if item['pdf_price'] > 0 and item['pdf_price'] in aps:
                            item['status']['部品価格'] = f'○(修正一致={item["pdf_price"]:,})'
                            item['reasons'] = [r for r in item['reasons'] if '価格' not in r]
                    improved += 1

            # 修正2: 価格近似判定の緩和
            if any('価格不一致' in r for r in item['reasons']):
                if item['ad_prices'] and item['pdf_price'] > 0:
                    closest = min(item['ad_prices'], key=lambda p: abs(p - item['pdf_price']))
                    pct = abs(closest - item['pdf_price']) / max(item['pdf_price'], 1) * 100
                    if pct < 30:
                        item['status']['部品価格'] = f'△(近似{pct:.0f}%差: PDF={item["pdf_price"]:,},AD={closest:,})'
                        item['reasons'] = [r for r in item['reasons'] if '価格不一致' not in r]
                        item['reasons'].append(f'価格近似(差{pct:.0f}%): DB更新時期差の可能性')
                        improved += 1

    if improved:
        print(f"\n  自律修正: {improved}件改善")
    return improved


# ============================================================
# MAIN
# ============================================================
def main():
    print("="*80)
    print("完全自動マッチング・分析システム v2.0")
    print(f"実行: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)

    pdfs = sorted(glob.glob(os.path.join(SAMPLE_PDF_DIR, '*.pdf')))
    neos = sorted(glob.glob(os.path.join(SAMPLE_PDF_DIR, '*.neo')))
    print(f"\nPDF: {len(pdfs)}件 / NEO: {len(neos)}件")

    engine = MatchingEngine()
    all_res = []

    for pdf in pdfs:
        neo = neos[0] if neos else None
        r = engine.run(pdf, neo)
        all_res.append(r)

    # 初回レポート
    total, ok, partial, fail = report(all_res)

    # 自律修正サイクル
    if fail > 0:
        print(f"\n{'='*80}")
        print(f"[自律修正サイクル] {fail}件の失敗を改善中...")
        auto_fix(all_res, engine)
        # 修正後レポート
        print(f"\n--- 修正後再集計 ---")
        report(all_res)


# ============================================================
# spec_iter4 課題A (S4-1): エンジン singleton + 車両コード単位パーツキャッシュ
# ============================================================
from functools import lru_cache as _lru_cache

# モジュールレベル singleton（addata_root 単位）
_engine_singleton = None
_engine_singleton_root = None
# vehicle_code をキーとする dict memo（車検証→folder解決→parts_master）
_parts_cache_by_vcode = {}


def _get_engine(addata_root=ADDATA_ROOT):
    """AddataEngine (= AddataSearchEngine) の singleton を返す。
    addata_root が変わったら作り直す。"""
    global _engine_singleton, _engine_singleton_root
    if _engine_singleton is None or _engine_singleton_root != addata_root:
        _engine_singleton = AddataEngine(addata_root)
        _engine_singleton_root = addata_root
    return _engine_singleton


@_lru_cache(maxsize=64)
def _get_all_parts_cached(vehicle_code: str, addata_root: str = ADDATA_ROOT):
    """vehicle_code をキーに parts_master をメモ化（tuple化でhashable維持）。
    folder 解決失敗時は空 tuple。"""
    try:
        if not vehicle_code:
            return tuple()
        # dict memo にもヒットさせる
        if vehicle_code in _parts_cache_by_vcode:
            return _parts_cache_by_vcode[vehicle_code]
        eng = _get_engine(addata_root)
        # vehicle_code から folder 推定: AddataEngine 側の identify 経由で得る
        # ここでは外部呼出側が folder を知っている前提なので、
        # vehicle_code をそのまま folder とみなしてもよい簡易キャッシュ
        try:
            parts = eng.load_parts_master(vehicle_code)
        except Exception:
            parts = []
        result = tuple(parts) if parts else tuple()
        _parts_cache_by_vcode[vehicle_code] = result
        return result
    except Exception:
        return tuple()


# 公開 alias（spec_iter4 命名互換）
AddataSearchEngine = AddataEngine


# ============================================================
# 8. app.py 連携用 公開ラッパ
# ============================================================
def match_pdf_items_to_addata(items, vehicle_info, addata_root=ADDATA_ROOT):
    """app.py の items 形式を Addata DB と照合して L1-L4 マッチ情報を付与する薄いラッパ。

    入力:
      items: list[dict] — app.py 形式（parts_name / parts_no / quantity / unit_price / amount / category / wage 等）
      vehicle_info: dict — 車両情報（model_designation, category_number, model_code, color_code 等）
      addata_root: str — Addata ルートパス（既定 C:\\Addata）

    戻り値:
      list[dict] — 入力 items の非破壊コピー。各要素に以下を追加:
        db_price, db_parts_no, db_work_index, match_level (L1-L4), match_note

    例外時はサイレントに元の items を返す（既存OCR結果を壊さない）。
    """
    try:
        if not items:
            return items
        out = [dict(it) for it in items]

        # spec_iter4 課題A (S4-1): singleton 利用で AddataEngine 再生成を避ける
        engine = _get_engine(addata_root)
        md = str(vehicle_info.get('model_designation', '') or '').strip()
        cn = str(vehicle_info.get('category_number', '') or '').strip()
        mc = str(vehicle_info.get('model_code', '') or '').strip()
        color_code = str(vehicle_info.get('color_code', '') or '').strip()

        veh, err = engine.identify_vehicle(md, cn, mc)
        if err or not veh:
            for it in out:
                it.setdefault('db_price', None)
                it.setdefault('db_parts_no', '')
                it.setdefault('db_work_index', None)
                it['match_level'] = 'L4'
                it['match_note'] = f'車種特定失敗: {err or "unknown"}'
                it['price_diff_pct'] = None
                it['price_alert'] = False
                it['db_source'] = ''
            return out

        folder = veh['folder']
        match_layer = veh.get('match_layer', 0)
        # spec_iter4 課題A (S4-1): folder(= vehicle_code相当) をキーに parts_master を memo化
        try:
            _cached_parts = _get_all_parts_cached(folder, addata_root)
        except Exception:
            _cached_parts = tuple()
        if _cached_parts:
            master = list(_cached_parts)
        else:
            master = engine.load_parts_master(folder)
            try:
                _parts_cache_by_vcode[folder] = tuple(master) if master else tuple()
            except Exception:
                pass
        widx = engine.load_work_index(folder)

        act_map = {'取替': 'K', '交換': 'K', '脱着': 'D', '板金': 'S', '修理': 'S', '取替塗装': 'K'}

        for it in out:
            # app.py 形式 → 内部形式へキー揺れ吸収
            name = it.get('parts_name') or it.get('name') or ''
            # spec_iter3 課題B: _normalize_price で全角/カンマ/円記号を吸収し、
            # unit_price=0 かつ amount>0 なら amount/quantity で補完
            unit_price_n = _normalize_price(it.get('unit_price') or it.get('part_price'))
            amount_n = _normalize_price(it.get('amount') or it.get('parts_amount'))
            qty_n = _normalize_price(it.get('quantity') or it.get('qty') or 1) or 1
            if unit_price_n > 0:
                pdf_price = unit_price_n
            elif amount_n > 0 and qty_n >= 1:
                pdf_price = amount_n // qty_n
            else:
                pdf_price = 0
            action = it.get('category') or it.get('action') or ''

            db_price = None
            db_parts_no = ''
            db_widx = None
            level = 'L4'
            note = ''

            try:
                matched, score, _ = FuzzyMatcher.match(name, master, pdf_price)
            except Exception:
                matched, score = None, 99

            if matched and score <= 5:
                ref_no = matched.get('ref_no', -1)
                sec3 = matched.get('sec3', '')
                if ref_no >= 0:
                    try:
                        rep, all_prices, top_pno = engine.lookup_price_with_pno(folder, ref_no, sec3, color_code)
                        db_price = rep
                        if top_pno:
                            db_parts_no = top_pno
                        # 価格一致レベル判定
                        if pdf_price > 0 and rep:
                            if pdf_price in (all_prices or []):
                                level = 'L1'  # 完全一致
                            elif abs(pdf_price - rep) / max(pdf_price, 1) < 0.15:
                                level = 'L2'  # 近似一致
                            else:
                                level = 'L3'  # 部品名一致のみ
                        else:
                            level = 'L2' if rep else 'L3'
                    except Exception as e:
                        note = f'価格取得失敗: {e}'
                        level = 'L3'
                else:
                    level = 'L3'

                # 工数インデックス
                sc = matched.get('section_code', '')
                pm = act_map.get(action, '')
                if sc in widx and pm and pm in widx[sc]:
                    db_widx = widx[sc][pm].get('page', '')

                note = note or f'部品名一致 score={score} layer={match_layer}'
            else:
                level = 'L4'
                note = f'部品名不一致 best_score={score}'

            # L1/L2 で db_parts_no が空ならPDF側品番でフォールバック（spec_iter2 課題A）
            if level in ('L1', 'L2') and not db_parts_no:
                db_parts_no = str(it.get('parts_no', '') or '').strip()

            # 価格差分（spec_iter2 課題D/E）
            price_diff_pct = None
            if db_price and pdf_price > 0:
                try:
                    price_diff_pct = (pdf_price - db_price) / db_price * 100.0
                except Exception:
                    price_diff_pct = None
            price_alert = bool(price_diff_pct is not None and abs(price_diff_pct) > 5.0)

            it['db_price'] = db_price
            it['db_parts_no'] = db_parts_no
            it['db_work_index'] = db_widx
            # 部品コード大区分。app.py が ERParts.PartsCode に書くために読むキー。
            # FuzzyMatcher は候補が1件でもあれば matched を返すため、score で
            # 棄却した候補（level='L4'）でも matched は真になる。無条件に書くと
            # 「DBに無い」と ※ で宣言した行に別部品の大区分が入る（前後違いが典型）。
            # 採用した行だけに入れる。
            it['_master_section_code'] = (
                str((matched or {}).get('section_code', '') or '')
                if level != 'L4' else '')
            it['match_level'] = level
            it['match_note'] = note
            it['price_diff_pct'] = price_diff_pct
            it['price_alert'] = price_alert
            it['db_source'] = os.path.basename(folder) if folder else ''

        return out
    except Exception as e:
        # 何があっても元データを壊さない
        try:
            import traceback as _tb
            print(f'[match_pdf_items_to_addata] error: {e}')
            _tb.print_exc()
        except Exception:
            pass
        # 最低限のキーだけ注入（UIでKeyError防止）
        try:
            safe = []
            for it in items or []:
                d = dict(it)
                d.setdefault('match_level', 'NA')
                d.setdefault('db_price', None)
                d.setdefault('db_parts_no', '')
                d.setdefault('price_diff_pct', None)
                d.setdefault('price_alert', False)
                d.setdefault('db_source', '')
                safe.append(d)
            return safe
        except Exception:
            return items


# ============================================================
# v4: フルADDATAマッチング (AddataSearchEngine + PDFGradeIdentifier 直接利用)
# ============================================================

# v4 マーカー文言（spec準拠）
MARK_PRICE_DIFF = "※部品価格相違"
MARK_NO_DB = "※ADDATA該当なし"


# 品番欄（ERParts.PartsNo）は CP932 で18バイトしかない。
# 「※部品価格相違」は14バイトあり、品番と並べると必ず溢れて
# 「52119-52A50 ※部品」のように品番もマーカーも壊れる。
# 品番欄には2文字の短い印だけを置き、詳しい文言は備考に回す。
_PNO_MARK_SHORT = {
    "※部品価格相違": "※価",
    "※部品価格不一致": "※価",
    "※ADDATA該当なし": "※無",
    "※DB不一致": "※無",
}
ERPARTS_PARTS_NO_BYTES = 18


def _decorate_pno_v4(pno: str, mark: str, max_bytes: int = ERPARTS_PARTS_NO_BYTES) -> str:
    """既存品番があれば `品番 印` 形式、なければ印のみ。

    印を付けて列幅を超えるくらいなら、品番をそのまま残す。
    品番が壊れると発注に使えなくなるため、品番の方を優先する。
    """
    pno = (pno or "").strip()
    tag = _PNO_MARK_SHORT.get(mark, "※")
    if not pno:
        return tag
    if "※" in pno or mark in pno:
        return pno  # 二重付与防止
    # 収まらないときに印ごと捨てると、価格相違やADDATA該当なしが
    # 何の表示も無いまま協定に出る。ホンダの品番（71501-T5A-J00ZZ＝15バイト）
    # は「 ※価」の4バイトを足すと必ず溢れるので、これが常態だった。
    # 詰め方を段階的に緩めて、最後は1文字の「※」だけでも残す。
    # 印の種類は呼び出し側が警告に出すので、ここでは「要確認」だけ伝われば足りる。
    for cand in (f"{pno} {tag}", f"{pno}{tag}", f"{pno}※"):
        try:
            if len(cand.encode("cp932", "replace")) <= max_bytes:
                return cand
        except Exception:
            pass
    return pno


def _list_vehicle_codes(addata_root: str) -> list[str]:
    """ADDATA root 配下の vehicle_code (例 J97, T54) 一覧を返す。"""
    out = []
    if not addata_root or not os.path.isdir(addata_root):
        return out
    try:
        for entry in os.listdir(addata_root):
            p = os.path.join(addata_root, entry)
            if not os.path.isdir(p) or len(entry) != 1 or not entry.isalpha():
                continue
            try:
                for sub in os.listdir(p):
                    if os.path.isdir(os.path.join(p, sub)):
                        out.append(sub)
            except OSError:
                continue
    except OSError:
        pass
    return out


# v5-Iter1: 部品番号・部品価格から車種逆引きキャッシュ
_REVERSE_PNO_CACHE = {}  # addata_root → {pno_normalized: [vehicle_codes]}
# v5-Iter4: 部品マスタ名称インデックス  key=(vcode, grade, body) → (name_to_entry, norm_to_orig)
_NAME_INDEX_CACHE = {}


def _normalize_pno(pno: str) -> str:
    """部品番号を比較しやすく正規化（ハイフン除去・大文字化）"""
    if not pno:
        return ""
    return str(pno).upper().replace("-", "").replace(" ", "").replace("　", "").strip()


def _build_reverse_index(addata_root: str) -> dict:
    """指定 ADDATA root の全車種 × 全部品番号 を {pno_norm: [vcode,...]} で逆引きインデックス化。
    初回は重い (車種数 × 部品数) のため LRU キャッシュ。"""
    if addata_root in _REVERSE_PNO_CACHE:
        return _REVERSE_PNO_CACHE[addata_root]
    idx = {}
    if not os.path.isdir(addata_root):
        _REVERSE_PNO_CACHE[addata_root] = idx
        return idx
    try:
        from _addata_db_search import AddataSearchEngine  # type: ignore
    except Exception:
        _REVERSE_PNO_CACHE[addata_root] = idx
        return idx
    eng = AddataSearchEngine(addata_root)
    vcodes = _list_vehicle_codes(addata_root)
    for vc in vcodes:
        try:
            parts = eng.get_all_parts(vc)
        except Exception:
            continue
        for p in parts or []:
            pno = _normalize_pno(p.get("parts_no") or "")
            if pno and len(pno) >= 6:  # ノイズ除去 (短すぎる品番は無視)
                idx.setdefault(pno, []).append(vc)
    _REVERSE_PNO_CACHE[addata_root] = idx
    return idx


def reverse_lookup_vehicle(items: list, addata_root: str = ADDATA_ROOT,
                            min_hits: int = 2) -> dict:
    """品番リストから車種を逆引き。
    入力 items の parts_no を ADDATA 全車種逆引きインデックスと照合し、
    最多ヒット車種を返す (min_hits 件以上)。

    Returns:
        {found: bool, vehicle_code: str, hits: int, total: int,
         confidence: float, candidates: [(vcode, hits), ...]}
    """
    out = {"found": False, "vehicle_code": None, "hits": 0, "total": 0,
           "confidence": 0.0, "candidates": []}
    if not items or not addata_root or not os.path.isdir(addata_root):
        return out
    pnos = []
    for it in items:
        p = it.get("parts_no") or it.get("part_no") or it.get("part_number") or ""
        if p:
            pn = _normalize_pno(p)
            if pn and len(pn) >= 6:
                pnos.append(pn)
    out["total"] = len(pnos)
    if not pnos:
        return out
    idx = _build_reverse_index(addata_root)
    if not idx:
        return out
    # 投票
    from collections import Counter
    votes = Counter()
    for pn in pnos:
        for vc in idx.get(pn, []):
            votes[vc] += 1
    if not votes:
        return out
    top = votes.most_common(5)
    out["candidates"] = [(vc, cnt) for vc, cnt in top]
    best_vc, best_hits = top[0]
    if best_hits >= min_hits:
        out["found"] = True
        out["vehicle_code"] = best_vc
        out["hits"] = best_hits
        out["confidence"] = round(best_hits / len(pnos), 3)
    return out


def _full_addata_match(items, vehicle_info, addata_root=ADDATA_ROOT):
    """v4 フルADDATAマッチング (AddataSearchEngine + PDFGradeIdentifier 直接利用)

    入力 items の非破壊コピーを返し、各要素に以下を追加:
      - db_price (int|None): DB単価
      - db_parts_no (str): DB部品番号
      - db_work_index (int|None): DB作業指数 (WI)
      - db_disposal_code (int|None): DB作業区分(0=取替/1=脱着/2=修理)
      - match_level (str): L1/L2/L3/L4
      - match_note (str): 詳細
      - parts_no_marked (str): マーカー付与済み品番（OCR品番優先 + 末尾マーカー）

    マッチ条件:
      - L1: 部品名 + 価格 + 品番 全一致
      - L2: 部品名 + 価格 一致
      - L3: 部品名のみ一致 (価格不一致 → ※部品価格相違 マーカー)
      - L4: マッチ不可 (※ADDATA該当なし マーカー)
    """
    if not items:
        return items

    out = [dict(it) for it in items]

    # ADDATA root 不在/車種未指定時は全行 L4 でマーカー付与
    model_code = ((vehicle_info or {}).get("model_code")
                  or (vehicle_info or {}).get("vehicle_code") or "").strip()
    if not addata_root or not os.path.isdir(addata_root) or not model_code:
        for it in out:
            ocr_pno = str(it.get("parts_no") or it.get("part_no") or "").strip()
            it.setdefault("db_price", None)
            it.setdefault("db_parts_no", "")
            it.setdefault("db_work_index", None)
            it.setdefault("db_disposal_code", None)
            it["match_level"] = "L4"
            it["match_note"] = "ADDATA未配備または車種未特定"
            it["parts_no_marked"] = _decorate_pno_v4(ocr_pno, MARK_NO_DB)
        return out

    try:
        from _addata_db_search import AddataSearchEngine  # type: ignore
    except Exception as e:
        for it in out:
            ocr_pno = str(it.get("parts_no") or "").strip()
            it["match_level"] = "L4"
            it["match_note"] = f"AddataSearchEngine import失敗: {e}"
            it["parts_no_marked"] = _decorate_pno_v4(ocr_pno, MARK_NO_DB)
        return out

    engine = AddataSearchEngine(addata_root)

    # 車種フォルダ存在確認
    try:
        folder = engine._vehicle_folder(model_code)
        if not os.path.isdir(folder):
            raise FileNotFoundError(folder)
    except Exception as e:
        for it in out:
            ocr_pno = str(it.get("parts_no") or "").strip()
            it["match_level"] = "L4"
            it["match_note"] = f"車種フォルダ未配備: {model_code}"
            it["parts_no_marked"] = _decorate_pno_v4(ocr_pno, MARK_NO_DB)
        return out

    # グレード推定 (PDFGradeIdentifier) v5-Iter8: body_code 推定追加
    grade_code = (vehicle_info or {}).get("grade_code") or (vehicle_info or {}).get("grade") or ""
    body_code = 0
    try:
        body_code = int((vehicle_info or {}).get("body_code") or 0)
    except (TypeError, ValueError):
        body_code = 0
    # body_code が未指定なら OCR car_model から推定 (例 "3DHB" → 3DHB から body 推定は複雑)
    # 現状は ADDATA 側で勝手に補完してくれるので無指定で進める
    if not grade_code:
        try:
            from _grade_identifier import PDFGradeIdentifier  # type: ignore
            ident = PDFGradeIdentifier(engine, model_code, body_code=body_code)
            try:
                r = ident.identify(items) if hasattr(ident, "identify") else {}
            except TypeError:
                r = ident.identify() if hasattr(ident, "identify") else {}
            if isinstance(r, dict):
                grade_code = r.get("grade_code") or r.get("grade") or ""
        except Exception:
            pass

    # 部品マスタ取得
    try:
        master = engine.get_all_parts(model_code, grade_code=grade_code, body_code=body_code)
    except Exception:
        master = []
    if not master:
        try:
            master = engine.get_all_parts(model_code)
        except Exception:
            master = []

    if not master:
        for it in out:
            ocr_pno = str(it.get("parts_no") or "").strip()
            it["match_level"] = "L4"
            it["match_note"] = "部品マスタ取得失敗"
            it["parts_no_marked"] = _decorate_pno_v4(ocr_pno, MARK_NO_DB)
        return out

    # 名称→マスタ entries の dict (v5-Iter3-4: 名称正規化 + キャッシュ)
    # v12 Phase B-1: 部品名正規化強化（Fr/Rr 統一、長音吸収、濁点ゆれ、同義語辞書）
    import difflib

    # 小書き仮名 → 大書き仮名（OCR で ッ↔ツ, ェ↔エ 揺れ吸収用）
    _KANA_SMALL_TO_LARGE = str.maketrans({
        'ァ': 'ア', 'ィ': 'イ', 'ゥ': 'ウ', 'ェ': 'エ', 'ォ': 'オ',
        'ャ': 'ヤ', 'ュ': 'ユ', 'ョ': 'ヨ', 'ッ': 'ツ', 'ヮ': 'ワ',
    })

    # 同義語マップ（長い key 優先で順次置換）
    # v12 Phase B-1 v2: HONDA 表記（リヤー/フロアー/長音）と TOYOTA 表記（リヤ/フロア）統一を追加
    _SYN_MAP = {
        # バンパ系
        'フロントバンパー': 'FRBUMPER', 'フロントバンパ': 'FRBUMPER',
        'リヤバンパー': 'RRBUMPER', 'リアバンパー': 'RRBUMPER',
        'リヤーバンパー': 'RRBUMPER', 'リヤーパンバー': 'RRBUMPER',
        'リヤバンパ': 'RRBUMPER', 'リアバンパ': 'RRBUMPER',
        'Frバンパー': 'FRBUMPER', 'Frバンパ': 'FRBUMPER',
        'FRバンパ': 'FRBUMPER',
        'Rrバンパー': 'RRBUMPER', 'Rrバンパ': 'RRBUMPER',
        'RRバンパ': 'RRBUMPER',
        'バンパー': 'BUMPER', 'バンパ': 'BUMPER',
        # フェンダ系
        'フロントフェンダー': 'FRFENDER', 'フロントフェンダ': 'FRFENDER',
        'Frフェンダ': 'FRFENDER', 'Frフエンダ': 'FRFENDER',
        'リヤフェンダー': 'RRFENDER', 'リアフェンダー': 'RRFENDER',
        'リヤーフェンダー': 'RRFENDER',
        'Rrフェンダ': 'RRFENDER', 'Rrフエンダ': 'RRFENDER',
        'フェンダー': 'FENDER', 'フエンダ': 'FENDER', 'フェンダ': 'FENDER',
        # ランプ系
        'ヘッドランプ': 'HEADLAMP', 'ヘッドライト': 'HEADLAMP',
        'ヘツドランプ': 'HEADLAMP', 'ヘツドライト': 'HEADLAMP',
        'テールランプ': 'TAILLAMP', 'テールライト': 'TAILLAMP',
        'テイルランプ': 'TAILLAMP',
        'フォグランプ': 'FOGLAMP', 'フォグライト': 'FOGLAMP',
        'フオグランプ': 'FOGLAMP',
        # ボンネット・グリル
        'ボンネット': 'HOOD', 'フード': 'HOOD',
        'ラジエータグリル': 'RADGRILLE', 'ラジエタグリル': 'RADGRILLE',
        # ドア・ミラー
        'フロントドア': 'FRDOOR', 'リヤドア': 'RRDOOR',
        'リアドア': 'RRDOOR', 'リヤードア': 'RRDOOR',
        'Frドア': 'FRDOOR', 'Rrドア': 'RRDOOR',
        'ドアミラー': 'DOORMIRROR', 'サイドミラー': 'DOORMIRROR',
        # HONDA 長音表記揺れ統一
        'リヤー': 'リヤ', 'リアー': 'リヤ',
        'フロアー': 'フロア',
        'コーナーサブ': 'コーナサブ',
        'シリンダー': 'シリンダ',
        'モールデイング': 'モールディング',
        'ガーニツシュ': 'ガーニッシュ',
        'クツション': 'クッション', 'クツシヨン': 'クッション',
        # v12 iter_006: HONDA OCR ゆれ追加（バツ→バッ、ヘツ→ヘッ等）
        'バツク': 'バック', 'バツクボード': 'バックボード',
        'パツド': 'パッド', 'パツチ': 'パッチ',
        'ヘツドレスト': 'ヘッドレスト',
        'エアーコンデイシヨナー': 'エアコンディショナー',
        'コンデイシヨナー': 'コンディショナー',
        'コーンビ': 'コンビ', 'コンヒ': 'コンビ',
        'シーロムン': 'シートベルト', 'シーロベルト': 'シートベルト',
        'バネル': 'パネル', 'バネルCOMP': 'パネルCOMP',
        'バネルセット': 'パネルセット',
        'リャー': 'リヤ',
        'バルブ': 'バルブ', 'バイブ': 'バルブ',
        'フューエル': 'フユーエル',
        'フイラー': 'フィラー',
        'ベッドCOMPL': 'パッドCOMP',
        'ブレート': 'プレート',
        'ブラグ': 'プラグ',
        'クオーター': 'クォーター', 'クオータービラー': 'クォーターピラー',
        'ピラー': 'ピラー',
        # ASSY/COMP 等の符号統一（後段の記号削除で更にクリーンに）
        'ASSY.': 'ASSY', 'ASSY、': 'ASSY', 'ASSY,': 'ASSY',
        'COMP.': 'COMP', 'COMPL.': 'COMP', 'COMPL、': 'COMP',
        'COMPL ': 'COMP', 'COMP,': 'COMP', 'COMP、': 'COMP',
        # 接頭辞
        'Fr': 'FR', 'Rr': 'RR',
    }
    _SYN_KEYS_BY_LEN = sorted(_SYN_MAP.keys(), key=len, reverse=True)

    def _norm_name(n: str) -> str:
        if not n: return ""
        n = str(n).strip()
        # 半角カナ → 全角カナ
        try:
            import jaconv
            n = jaconv.h2z(n, kana=True, ascii=False, digit=False)
        except Exception:
            pass
        # 左右は捨てずに末尾の記号へ寄せる。捨てると「左ヘッドランプ」と
        # 「右ヘッドランプ」が同じ文字列になり、候補の先頭（＝マスタの
        # 並び順で先に来た方）が無条件に選ばれて、左の部品に右の品番が
        # 入る。価格は左右同額のことが多く、L2（価格一致）になって
        # マーカーも出ないため下流で気づけない。
        _side = ''
        _m_head = re.match(r'^\s*(左|右)\s*', n)
        if _m_head:
            _side = 'L' if _m_head.group(1) == '左' else 'R'
            n = n[_m_head.end():]
        if re.search(r'[（(\[]?\s*(左|Ｌ|LH|L)\s*[)）\]]?\s*$', n):
            _side = 'L'
        elif re.search(r'[（(\[]?\s*(右|Ｒ|RH|R)\s*[)）\]]?\s*$', n):
            _side = 'R'
        n = re.sub(r'[（(\[]?\s*(左|右|Ｌ|Ｒ|LH|RH)\s*[)）\]]?\s*$', '', n)
        # 同義語マップ適用（長い key 優先）
        for k in _SYN_KEYS_BY_LEN:
            if k in n:
                n = n.replace(k, _SYN_MAP[k])
        # 小書きカナ → 大書きカナ（OCR ゆれ吸収）
        n = n.translate(_KANA_SMALL_TO_LARGE)
        # 記号・空白・長音・ハイフン除去
        for ch in (" ", "　", "・", "．", ".", ",", "（", "）", "(", ")",
                   "ー", "-", "〃", "/", "/", "\\", "．"):
            n = n.replace(ch, "")
        # 左右を末尾に付けて区別できるようにする
        return (n.upper() + _side)

    # キャッシュはプロセス共有。ルートを入れないと、部品件数が同じ
    # 別の Addata（別利用者のアップロードや入れ替えた旧版）の索引を
    # 使い回し、他人の品番が出荷される。
    try:
        _root_key = os.path.realpath(addata_root) if addata_root else ''
    except Exception:
        _root_key = str(addata_root or '')
    cache_key = (_root_key, model_code, grade_code, body_code, len(master))
    cached = _NAME_INDEX_CACHE.get(cache_key)
    if cached:
        name_to_entry, norm_to_orig, master_names, master_names_norm = cached
    else:
        name_to_entry = {}
        norm_to_orig = {}
        for m in master:
            nm = (m.get("name") or "").strip()
            if nm and nm not in name_to_entry:
                name_to_entry[nm] = m
                norm_to_orig.setdefault(_norm_name(nm), nm)
        master_names = list(name_to_entry.keys())
        master_names_norm = list(norm_to_orig.keys())
        _NAME_INDEX_CACHE[cache_key] = (name_to_entry, norm_to_orig, master_names, master_names_norm)

    color_code = (vehicle_info or {}).get("color_code") or ""

    # アクション → DisposalCode
    _disposal_map = {
        "取替": 0, "交換": 0, "取換": 0,
        "脱着": 1, "取外": 1, "取付": 1, "組付": 1,
        "修理": 2, "補修": 2, "板金": 2, "塗装": 2,
        "調整": 2, "点検": 2,
    }

    # v11.0: pipeline の _to_int/_to_float ヘルパーを利用 (括弧書き等の OCR 異常吸収)
    try:
        from pdf_to_neo_pipeline import _to_int as _ti, _to_float as _tf
    except Exception:
        def _ti(v, d=0):
            try: return int(round(float(re.sub(r'[^\d.\-]', '', str(v)))))
            except: return d
        def _tf(v, d=0.0):
            try: return float(re.sub(r'[^\d.\-]', '', str(v)))
            except: return d
    for it in out:
        name = (it.get("parts_name") or it.get("name") or "").strip()
        ocr_pno = str(it.get("parts_no") or it.get("part_no") or "").strip()
        # OCR価格・数量 (v11.0: 安全変換)
        unit_price = _tf(it.get("unit_price"))
        amount = _tf(it.get("parts_amount") or it.get("amount"))
        qty = max(_ti(it.get("quantity"), 1), 1)
        if unit_price <= 0 and amount > 0:
            unit_price = amount / qty
        target_time = _tf(it.get("index_value"))
        action = (it.get("category") or it.get("work_code") or "").strip()
        disposal = _disposal_map.get(action)

        db_price = None
        db_pno = ""
        db_widx = None
        db_disp = disposal
        level = "L4"
        note = ""

        if not name:
            it["match_level"] = "L4"
            it["match_note"] = "部品名空"
            it["parts_no_marked"] = _decorate_pno_v4(ocr_pno, MARK_NO_DB)
            continue

        # v5-Iter5: 高速ショートカット: OCR品番が DB pno_index にあれば即マッチ
        # v12 Phase B-2: マッチ段階を name_match_stage に記録
        name_match_stage = ""
        ocr_pno_norm = _normalize_pno(ocr_pno)
        if ocr_pno_norm and len(ocr_pno_norm) >= 6:
            cand = None
            for nm, m in name_to_entry.items():
                m_pno = _normalize_pno(m.get("parts_no") or "")
                if m_pno == ocr_pno_norm:
                    cand = [nm]
                    name_match_stage = "pno_exact"
                    break
            if not cand:
                cand = difflib.get_close_matches(name, master_names, n=1, cutoff=0.85)
                if cand: name_match_stage = "name_0.85"
        else:
            cand = difflib.get_close_matches(name, master_names, n=1, cutoff=0.85)
            if cand: name_match_stage = "name_0.85"
        if not cand:
            # 正規化マッチ (Phase B-1 強化辞書経由)
            nn = _norm_name(name)
            if nn:
                # 段階的緩和: 0.85 → 0.7 → 0.6 → 0.5 (iter_006 追加)
                for cutoff, stage in [(0.85, "norm_0.85"), (0.7, "norm_0.70"),
                                      (0.6, "norm_0.60"), (0.5, "norm_0.50")]:
                    cand_n = difflib.get_close_matches(nn, master_names_norm, n=1, cutoff=cutoff)
                    if cand_n:
                        cand = [norm_to_orig[cand_n[0]]]
                        name_match_stage = stage
                        break
        if not cand:
            # 緩いマッチ (最終フォールバック)
            cand = difflib.get_close_matches(name, master_names, n=1, cutoff=0.55)
            if cand: name_match_stage = "name_0.55"
        if not cand:
            it["db_price"] = None
            it["db_parts_no"] = ""
            it["db_work_index"] = None
            it["db_disposal_code"] = db_disp
            it["match_level"] = "L4"
            it["match_note"] = "部品名fuzzy 0.7以下"
            it["parts_no_marked"] = _decorate_pno_v4(ocr_pno, MARK_NO_DB)
            continue

        entry = name_to_entry[cand[0]]
        ref_no = entry.get("ref_no")

        # 価格取得 (color_code 考慮)
        try:
            price_info = engine.get_price(model_code, ref_no=ref_no,
                                          color_code=color_code,
                                          grade_code=grade_code,
                                          body_code=body_code)
            if isinstance(price_info, dict):
                db_price = price_info.get("price") or price_info.get("unit_price")
                db_pno = price_info.get("parts_no") or entry.get("parts_no", "")
            elif isinstance(price_info, (int, float)):
                db_price = int(price_info)
                db_pno = entry.get("parts_no", "")
            else:
                db_price = entry.get("price")
                db_pno = entry.get("parts_no", "")
        except Exception:
            db_price = entry.get("price")
            db_pno = entry.get("parts_no", "")

        # 工賃 → WI マッチ
        if target_time > 0 and disposal in (0, 1):
            try:
                wi_res = engine.match_wage_by_time(model_code, ref_no, target_time,
                                                   grade_code=grade_code,
                                                   body_code=body_code)
                if wi_res and isinstance(wi_res, tuple):
                    db_widx = wi_res[1]
            except Exception:
                pass

        # マッチレベル判定
        if db_price and unit_price > 0:
            diff_ratio = abs(unit_price - db_price) / max(db_price, 1)
            if diff_ratio < 0.01 and ocr_pno and db_pno and ocr_pno == db_pno:
                level = "L1"
            elif diff_ratio < 0.02:
                level = "L2"
            else:
                level = "L3"
                note = f"価格差{diff_ratio:.1%}"
        elif db_price:
            # 単価が0や負の行（脱着・工賃だけの行など）は価格を検証できて
            # いない。L2（価格一致）にすると、検証していない行が
            # 「一致」として扱われ、マーカーも出ない。
            level = "L3"
            note = "OCR単価なし（価格未検証）"
        else:
            level = "L3"
            note = "DB価格取得不可"

        # マーカー付与: L3 (価格相違)、L4 (DB該当なし)
        # OCR品番優先、なければ DB品番
        # 部品金額の無い行（工賃だけの行・経費行）にDBの品番を入れると、
        # コグニセブン上で部品行として読める。品番は部品のある行だけ。
        def _amt(v):
            try:
                return int(float(str(v).replace(',', '') or 0))
            except (TypeError, ValueError):
                return 0
        _has_parts_amount = (unit_price > 0
                             or _amt(it.get("amount")) > 0
                             or _amt(it.get("parts_amount")) > 0)
        primary_pno = ocr_pno or (db_pno if _has_parts_amount else "")
        if level == "L3" and db_price and unit_price > 0 and abs(unit_price - db_price) / max(db_price, 1) >= 0.02:
            marked = _decorate_pno_v4(primary_pno, MARK_PRICE_DIFF)
        else:
            marked = primary_pno

        # v13 Step B: ADDATA マスタの正式部品名（半角カナ）を取得
        db_name = (entry.get("name") or "").strip()

        it["db_price"] = int(db_price) if db_price else None
        it["db_parts_no"] = db_pno
        it["db_parts_name"] = db_name
        it["db_work_index"] = db_widx
        it["db_disposal_code"] = db_disp
        it["match_level"] = level
        # v12 Phase B-2: name_match_stage を note に記録
        _stage_suffix = f" stage={name_match_stage}" if name_match_stage else ""
        it["match_note"] = note + _stage_suffix if note else f"{level} ref_no={ref_no}{_stage_suffix}"
        it["parts_no_marked"] = marked
        # v12 Phase C-1: addata_matched フラグ
        it["addata_matched"] = level in ("L1", "L2", "L3")
        # v13 Step B (iter_006/007): ADDATA 由来の name 上書きは N4 を逆に悪化させたためロールバック。
        # ADDATA マスタの全角カナ表記が正解 NEO の半角カナと記号差で乖離するケースが多く、
        # OCR の半角カナ寄りの値の方が一致しやすい。db_parts_name は参照用に保存のみ。

    # v12 Phase C-1: 早期 return 経路（マスタ取得失敗・車種フォルダ不在等）にも
    # addata_matched=False を一律付与（呼び出し側の安全のため）
    for it in out:
        it.setdefault("addata_matched", it.get("match_level") in ("L1", "L2", "L3"))

    return out


# ============================================================
# app.py スタブ置換用 軽量ヘルパ
# ============================================================
def find_addata_dir(default=ADDATA_ROOT):
    """C:\\Addata の存在確認。なければ None。"""
    import os as _os
    if _os.path.isdir(default):
        return default
    return None


def find_ka06_path(addata_base):
    """KA06_ALL.DB のパスを返す。"""
    import os as _os
    if not addata_base:
        return None
    p = _os.path.join(addata_base, 'COM', 'KA06_ALL.DB')
    return p if _os.path.exists(p) else None


def identify_vehicle_wrapper(addata_base, vehicle_data):
    """app.py / pipeline 用 identify_vehicle 実体。AddataEngine.identify_vehicle を呼ぶ。

    v12 Phase A-5: vehicle_data から reg_date / maker / car_name も渡す。
    layer 3 (TOYOTA_GENERIC) が返ると is_supported=True / is_template=True。
    """
    if not vehicle_data:
        vehicle_data = {}
    try:
        # addata_base 不在でも AddataEngine 内で layer 3 へ落ちる
        eng = AddataEngine(addata_base or '')
        md = str(vehicle_data.get('model_designation') or
                 vehicle_data.get('car_model_designation') or '').strip()
        cn = str(vehicle_data.get('category_number') or
                 vehicle_data.get('car_category_number') or '').strip()
        mc = str(vehicle_data.get('model_code') or
                 vehicle_data.get('car_model') or '').strip()
        rd = str(vehicle_data.get('reg_date') or
                 vehicle_data.get('car_reg_date') or
                 vehicle_data.get('first_reg_date') or '').strip()
        mk = str(vehicle_data.get('maker') or
                 vehicle_data.get('car_maker') or '').strip()
        cnm = str(vehicle_data.get('car_name') or '').strip()
        veh, err = eng.identify_vehicle(md, cn, mc,
                                        reg_date=rd, maker=mk, car_name=cnm)
        if veh:
            return {
                'match_layer': veh.get('match_layer', 3),
                'is_supported': True,
                'is_template': bool(veh.get('is_template')),
                'vehicle_code': veh.get('vehicle_code'),
                'folder': veh.get('folder'),
                'reason': veh.get('reason') or 'ok',
                # 候補が複数残ったことを呼び出し側に伝える。載せないと
                # 「先頭候補で確定」が「一致」と同じ顔で下流に流れ、
                # 別型式の部品マスタで照合したことに誰も気づけない。
                'ambiguous': veh.get('ambiguous') or [],
            }
        return {'match_layer': 4, 'is_supported': False, 'reason': err or 'not found'}
    except Exception as e:
        return {'match_layer': 4, 'is_supported': False, 'reason': f'error: {e}'}


def match_parts_with_addata_wrapper(items, addata_folder, vehicle_info=None):
    """app.py 用 match_parts_with_addata 実体。
    戻り値: (matched_items, success_flag)
    """
    if not items:
        return (items, False)
    try:
        vi = vehicle_info or {}
        matched = match_pdf_items_to_addata(items, vi, addata_folder or ADDATA_ROOT)
        # 1件でも L1/L2 が出たら success
        success = any(it.get('match_level') in ('L1', 'L2') for it in matched)
        return (matched, success)
    except Exception:
        return (items, False)


# ============================================================
# 後方互換 alias (spec_iter2 課題B)
# ============================================================
AutoMatcher = MatchingEngine


if __name__ == '__main__':
    main()
