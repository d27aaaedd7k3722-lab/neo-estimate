#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Addata汎用データベース検索エンジン
任意の車種コードから部品コード・部品名称・部品番号・部品価格・作業指数を取得

使用方法:
    engine = AddataSearchEngine('C:\\Addata')

    # 全部品リスト取得 (grade_code/body_code指定で最適グレード選択)
    parts = engine.get_all_parts('J55')
    parts = engine.get_all_parts('W53', grade_code='C', body_code=20)

    # 特定ref_noの価格取得
    price_info = engine.get_price('J55', ref_no=3810, color_code='PB93P')
    price_info = engine.get_price('W53', ref_no=10, grade_code='C', body_code=20)

    # 全バリアント価格取得 (グレード一覧)
    variants = engine.get_price_variants('W53', ref_no=10)

    # 作業指数取得（生データ）
    wi_list = engine.get_work_index('J55', ref_no=3810, labor_rate=72.8)

    # NEO工賃マッチング: Time フィールドベース (レバレート不要・推奨)
    result = engine.match_wage_by_time('J55', ref_no=3810, target_time=1.0)
    # → ('single', 100, [100])

    # NEO工賃マッチング: 工賃金額ベース (レバレート必要)
    result = engine.match_wage('J55', ref_no=3810, target_wage=7280, labor_rate=72.8)
    # → {'method': 'combo2', 'work_index': 100, 'computed_wage': 7280, 'entries': [...]}

    # グレードオプションコード取得 (*09.DB より / *11.DB grade_flags用)
    opt = engine.get_grade_option_code('W53', body_code=20, grade_code='C')  # → 'E'

    # グレードセクション取得 (*09.DB 行インデックス → *15.DB section文字)
    sec = engine.get_grade_section('W53', body_code=20, grade_code='C')  # → 'A'〜'Z'

    # NEO工賃マッチング: グレードセクション絞り込み付き
    result = engine.match_wage_by_time('W53', 3100, 0.7, grade_code='C', body_code=20)
    # → ('combo2/g', 70, [60, 10])  (/g = grade section filtered)

対応DB:
    *01.DB  → LCGシード (uint16 LE)
    *09.DB  → グレード名称マスタ (XOR 0xFF, CP932テキスト)
    *11.DB  → 全部品価格・品番 (72B LCG暗号化レコード, header=0x190)
    *12.DB  → 部品名称・数量マスタ (XOR 0xFF テキスト, バイト位置固定長)
    *15.DB  → 作業指数 (plaintext 19B blocks, header=104B, WI=uint16 LE at [4:6])
    *83.DB  → カラーバリアント部品価格 (LCG暗号化, block_size可変)

検証結果 (244 NEOファイル / 4月分):
    部品価格 (最新DB): ~96% (any()上限), 全体51.1% (旧DBファイルはDB版本差異)
    品番マッチ:        95.1% (244ファイル全体)
    作業指数 (Time基準): 93.1% DB内 (DisposalCode 0=取替 / 1=脱着)
      v2→v3改善: 89.0%→93.1% (+4.1%) = ①tol=5→10(+3.9%) ②combo4→5(+0.2%)
      gradeセクション絞り込み: 65件でより正確なsection使用（全体精度は同等）
      ★tolerance=10が新デフォルト (0.1h=6分を許容誤差として許容)
      残存MISS(6.9%): ref=6501型(db=[30]vs大WI), 差分>10型, 空WI型

検証済みメーカー: Honda(J), Toyota(T,W), Nissan(P,Q), Suzuki(S), Mazda(M),
                  Mitsubishi(C), Daihatsu(D), Subaru(F), Lexus(L), Hino(H)
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

import struct, os, glob, re
from collections import defaultdict
from typing import Optional


class LCG:
    """Microsoft CRT rand()互換 LCG暗号ストリーム (DBSEARCH.dll CipherCode確認済み)"""
    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def next_byte(self) -> int:
        self.state = (self.state * 0x343FD + 0x269EC3) & 0xFFFFFFFF
        return (self.state >> 16) & 0xFF

    def keystream(self, length: int) -> bytes:
        return bytes(self.next_byte() for _ in range(length))


class AddataSearchEngine:
    """Addata汎用データベース検索エンジン"""

    HEADER_11 = 0x190    # *11.DB header = 400 bytes
    RECORD_11 = 72       # *11.DB record = 72 bytes
    HEADER_15 = 104      # *15.DB header = 104 bytes
    BLOCK_15 = 19        # *15.DB block = 19 bytes

    def __init__(self, addata_root: str = r'C:\Addata'):
        self.root = addata_root
        self._cache = {}

    def _vehicle_folder(self, vehicle_code: str) -> str:
        prefix = vehicle_code[0]
        return os.path.join(self.root, prefix, vehicle_code)

    def _find_db(self, vehicle_code: str, suffix: str) -> Optional[str]:
        folder = self._vehicle_folder(vehicle_code)
        files = glob.glob(os.path.join(folder, f'*{suffix}'))
        return files[0] if files else None

    def _read_seed(self, vehicle_code: str) -> int:
        f01 = self._find_db(vehicle_code, '01.DB')
        if f01:
            with open(f01, 'rb') as f:
                data = f.read(2)
            if len(data) >= 2:
                return struct.unpack_from('<H', data, 0)[0]
        return 51

    # ================================================================
    # *11.DB: 全部品価格・品番
    # ================================================================
    def load_11db(self, vehicle_code: str) -> list[dict]:
        cache_key = f'{vehicle_code}_11'
        if cache_key in self._cache:
            return self._cache[cache_key]

        f11 = self._find_db(vehicle_code, '11.DB')
        if not f11:
            return []

        with open(f11, 'rb') as f:
            raw = f.read()

        if len(raw) <= self.HEADER_11:
            return []

        seed = self._read_seed(vehicle_code)
        total = (len(raw) - self.HEADER_11) // self.RECORD_11
        ks = LCG(seed).keystream(self.RECORD_11)

        records = []
        for ri in range(total):
            off = self.HEADER_11 + ri * self.RECORD_11
            enc = raw[off:off + self.RECORD_11]
            rec = bytes(a ^ b for a, b in zip(enc, ks))

            ref_no = struct.unpack_from('<H', rec, 0)[0]
            if ref_no == 0:
                continue

            try:
                work_code = rec[4:7].decode('ascii', errors='replace').strip()
                grade_flags = rec[8:25].decode('ascii', errors='replace').strip()
                part_name = rec[25:45].decode('cp932', errors='replace').strip()
                parts_no = rec[45:62].decode('ascii', errors='replace').strip()
                price_str = rec[62:68].decode('ascii', errors='replace').strip()
                price = int(price_str) if price_str.isdigit() else 0
            except (ValueError, UnicodeDecodeError):
                continue

            records.append({
                'ref_no': ref_no,
                'work_code': work_code,
                'grade_flags': grade_flags,
                'name': part_name,
                'parts_no': parts_no,
                'price': price,
            })

        self._cache[cache_key] = records
        return records

    # ================================================================
    # *83.DB: カラーバリアント部品価格
    # ================================================================
    def load_83db(self, vehicle_code: str) -> list[dict]:
        cache_key = f'{vehicle_code}_83'
        if cache_key in self._cache:
            return self._cache[cache_key]

        f83 = self._find_db(vehicle_code, '83.DB')
        if not f83:
            return []

        with open(f83, 'rb') as f:
            raw = f.read()

        if len(raw) < 10:
            return []

        seed = self._read_seed(vehicle_code)
        count = struct.unpack_from('<H', raw, 0)[0]
        if count == 0:
            return []

        data_start = 2 + count * 8
        max_f3 = max(struct.unpack_from('<H', raw, 2 + i * 8 + 6)[0] for i in range(count))
        remaining = len(raw) - data_start
        block_size = remaining // (max_f3 + 1) if max_f3 > 0 else 0
        if block_size < 10:
            return []

        ks = LCG(seed).keystream(block_size)
        records = []

        for bi in range(remaining // block_size):
            boff = data_start + bi * block_size
            if boff + block_size > len(raw):
                break
            enc = raw[boff:boff + block_size]
            rec = bytes(a ^ b for a, b in zip(enc, ks))

            ref_no = struct.unpack_from('<H', rec, 0)[0]
            sub_id = struct.unpack_from('<H', rec, 2)[0]

            try:
                name = rec[14:32].decode('cp932', errors='replace').strip()
                parts_no = rec[33:48].decode('ascii', errors='replace').strip()
                price_str = rec[50:56].decode('ascii', errors='replace').strip()
                price = int(price_str) if price_str.isdigit() else 0
                color = rec[59:70].decode('ascii', errors='replace').strip()
            except (ValueError, UnicodeDecodeError):
                continue

            if price > 0 or parts_no:
                records.append({
                    'ref_no': ref_no, 'sub_id': sub_id,
                    'name': name, 'parts_no': parts_no,
                    'price': price, 'color': color,
                })

        self._cache[cache_key] = records
        return records

    # ================================================================
    # *12.DB: 部品名称・数量マスタ
    # ================================================================
    def load_12db(self, vehicle_code: str) -> dict[int, dict]:
        cache_key = f'{vehicle_code}_12'
        if cache_key in self._cache:
            return self._cache[cache_key]

        f12 = self._find_db(vehicle_code, '12.DB')
        if not f12:
            return {}

        with open(f12, 'rb') as f:
            raw = f.read()

        decoded = bytes(b ^ 0xFF for b in raw)
        lines = decoded.split(b'\r\n') if b'\r\n' in decoded else decoded.split(b'\n')
        parts = {}

        for line in lines:
            if len(line) < 55:
                continue
            try:
                ref_str = line[44:48].decode('ascii', errors='replace').strip()
                if not ref_str.isdigit():
                    continue
                ref_no = int(ref_str)
                name = line[13:43].decode('cp932', errors='replace').strip()

                qty = 1
                # 半角/全角括弧内の数量: (3個), (各2本), （3個）, （各3枚）等
                m = re.search(r'[（(]各?(\d+)[^）)]*[）)]', name)
                if not m:
                    # ×N / xN 形式: ×3, x3
                    m = re.search(r'[×x](\d+)', name)
                if not m:
                    # 末尾の "N個" / "N本" / "N枚" / "N セット" 等 (括弧なし)
                    m = re.search(r'(\d+)[個本枚袋組セット]$', name)
                if m:
                    qty = int(m.group(1))

                methods = line[52:55].decode('ascii', errors='replace').strip() if len(line) > 55 else ''

                if ref_no not in parts or qty > parts[ref_no].get('quantity', 1):
                    parts[ref_no] = {'name': name, 'quantity': qty, 'methods': methods}
            except (ValueError, IndexError):
                continue

        self._cache[cache_key] = parts
        return parts

    # ================================================================
    # *15.DB: 作業指数
    # Record: 19 bytes — [0:2] ref_no, [2:4] sub_ref, [4:6] WI (uint16 LE),
    #   [6] section letter, [7] sec_no, [8] sub_type, [9] flag,
    #   [10:17] grade_flags (ASCII 7B), [17:19] ref_pair
    # ================================================================
    def load_15db(self, vehicle_code: str) -> dict[int, list[dict]]:
        """*15.DBの全レコードをref_no→リスト形式で返す"""
        cache_key = f'{vehicle_code}_15'
        if cache_key in self._cache:
            return self._cache[cache_key]

        f15 = self._find_db(vehicle_code, '15.DB')
        if not f15:
            return {}

        with open(f15, 'rb') as f:
            raw = f.read()

        if len(raw) <= self.HEADER_15:
            return {}

        total = (len(raw) - self.HEADER_15) // self.BLOCK_15
        records: dict[int, list[dict]] = {}

        for bi in range(total):
            off = self.HEADER_15 + bi * self.BLOCK_15
            blk = raw[off:off + self.BLOCK_15]
            ref_no = struct.unpack_from('<H', blk, 0)[0]
            sub_ref = struct.unpack_from('<H', blk, 2)[0]
            wi = struct.unpack_from('<H', blk, 4)[0]

            sec_letter = chr(blk[6]) if 32 <= blk[6] < 127 else ''
            sec_no = chr(blk[7]).strip() if 32 <= blk[7] < 127 else ''
            sub_type = chr(blk[8]).strip() if 32 <= blk[8] < 127 else ''
            flag = blk[9]
            grade = blk[10:17]
            grade_str = ''.join(chr(b) if 32 <= b < 127 else '' for b in grade).strip()

            if ref_no not in records:
                records[ref_no] = []
            records[ref_no].append({
                'wi': wi,
                'sub_ref': sub_ref,
                'sec_letter': sec_letter,          # 'A'-'Z': グレードバリアント識別子
                'section': sec_letter + sec_no + sub_type,
                'grade': grade_str,
                'flag': flag,
            })

        self._cache[cache_key] = records
        return records

    # ================================================================
    # *09.DB: グレード名称マスタ (XOR 0xFF)
    # ================================================================
    def load_09db(self, vehicle_code: str) -> list[str]:
        """*09.DBのグレード行リストを返す（行インデックス = セクションA-Z+）"""
        cache_key = f'{vehicle_code}_09'
        if cache_key in self._cache:
            return self._cache[cache_key]

        f09 = self._find_db(vehicle_code, '09.DB')
        if not f09:
            return []

        with open(f09, 'rb') as f:
            raw = f.read()

        decoded = bytes(b ^ 0xFF for b in raw).decode('cp932', errors='replace')
        lines = [l.strip() for l in decoded.replace('\r\n', '\n').split('\n') if l.strip()]
        self._cache[cache_key] = lines
        return lines

    def get_grade_option_code(self, vehicle_code: str, body_code: int,
                              grade_code: str) -> Optional[str]:
        """*09.DB から (BodyCode, GradeCode) に対応するオプションコードを取得

        *11.DB の grade_flags 選択に使用。
        例: W53 body=20 grade='C' → 'E'   (明示的オプションコード)
            J97 body=10 grade='A' → 'A'   (hex41=0x41=chr(65)='A' 解析)
            J97 body=10 grade='B' → 'B'   (hex42=0x42='B')
            K68 body=10 grade='B' → 'X'

        Returns: オプションコード文字列 (例 'E', 'A') / '' (存在するが空) / None (不一致)

        解析ルール:
            *09.DB 行フォーマット: 09{vehicle}{body4}{modifier}{grade_code}{JP名称} ... [{hex}] [{opt}]
            grade_code は次に非ASCII(日本語)が続く位置で識別。
            末尾トークン優先順:
              1. 単一大文字英字 → そのままopt_code (W53型)
              2. 2桁16進数 (41/42/..) → chr(int(hex,16)) がアルファベット (J97型)
        """
        lines = self.load_09db(vehicle_code)
        body_str = str(body_code).zfill(4)
        prefix = f'09{vehicle_code}{body_str}'

        for line in lines:
            if not line.startswith(prefix):
                continue
            rest = line[len(prefix):]
            # grade_code を探す: grade_code の直後に非ASCII文字 (日本語名称) が続く位置
            found = False
            for j in range(len(rest) - 1):
                if rest[j] == grade_code and ord(rest[j + 1]) > 127:
                    found = True
                    break
            if not found:
                continue

            tokens = line.split()
            # 優先1: 末尾から単一大文字英字 (W53型)
            for token in reversed(tokens):
                if len(token) == 1 and token.isupper() and token.isalpha():
                    return token
            # 優先2: 末尾から2桁16進数 (J97型: 41='A', 42='B', ...)
            for token in reversed(tokens):
                if len(token) == 2 and all(c in '0123456789ABCDEFabcdef' for c in token):
                    try:
                        decoded = chr(int(token, 16))
                        if decoded.isupper() and decoded.isalpha():
                            return decoded
                    except ValueError:
                        pass
            return ''  # 行は見つかったがオプションコードなし

        return None  # 一致する行なし

    def get_grade_section(self, vehicle_code: str, body_code: int,
                          grade_code: str) -> Optional[str]:
        """*09.DB の行インデックスから *15.DB セクション文字を返す

        *09.DB の行インデックス (0-based) が *15.DB セクション 'A','B',... に対応。
        option_code (*11.DB grade_flags用) とは独立したマッピング。

        Returns: セクション文字 ('A'-'Z') or None
        例: W53 body=20 grade='C' → *09.DB の何行目か → chr('A'+index)
        """
        lines = self.load_09db(vehicle_code)
        body_str = str(body_code).zfill(4)
        prefix = f'09{vehicle_code}{body_str}'

        line_idx = 0
        for line in lines:
            if not line.startswith(prefix):
                continue
            rest = line[len(prefix):]
            for j in range(len(rest) - 1):
                if rest[j] == grade_code and ord(rest[j + 1]) > 127:
                    return chr(ord('A') + line_idx)
            line_idx += 1

        return None

    # ================================================================
    # 高レベルAPI
    # ================================================================
    def get_all_parts(self, vehicle_code: str,
                      grade_code: str = '', body_code: int = 0) -> list[dict]:
        """車種の全部品リストを取得（*11.DB + *12.DBマージ）

        grade_code / body_code を指定すると、各 ref_no に対して
        最適グレードの価格エントリを1件選択して返す。
        未指定時は最初の非ゼロエントリを使用。
        """
        recs_11 = self.load_11db(vehicle_code)
        parts_12 = self.load_12db(vehicle_code)

        idx = defaultdict(list)
        for r in recs_11:
            idx[r['ref_no']].append(r)

        # grade選択用 option_code
        opt = None
        if grade_code and body_code:
            opt = self.get_grade_option_code(vehicle_code, body_code, grade_code)

        result = []
        seen = set()
        for ref_no, recs in sorted(idx.items()):
            if ref_no in seen:
                continue
            seen.add(ref_no)

            p12 = parts_12.get(ref_no, {})
            qty = p12.get('quantity', 1)
            name_12 = p12.get('name', '')

            # grade-aware 選択
            candidates = [r for r in recs if r['price'] > 0]
            if candidates:
                best = None
                if opt:
                    import re as _re
                    exact = [r for r in candidates if r['grade_flags'] == opt]
                    if exact:
                        best = exact[-1]
                    else:
                        word = [r for r in candidates
                                if _re.search(r'(?:^|\s)' + _re.escape(opt) + r'(?:\s|$)',
                                              r['grade_flags'])]
                        if word:
                            best = word[-1]
                if best is None:
                    best = candidates[0]

                result.append({
                    'ref_no': ref_no,
                    'name': name_12 or best['name'],
                    'parts_no': best['parts_no'],
                    'unit_price': best['price'],
                    'quantity': qty,
                    'total_price': best['price'] * qty,
                    'work_code': best['work_code'],
                    'grade_flags': best['grade_flags'],
                })
            else:
                # 価格ゼロのエントリのみ (参照用に追加)
                r = recs[0]
                result.append({
                    'ref_no': ref_no,
                    'name': name_12 or r['name'],
                    'parts_no': r['parts_no'],
                    'unit_price': 0,
                    'quantity': qty,
                    'total_price': 0,
                    'work_code': r['work_code'],
                    'grade_flags': r['grade_flags'],
                })

        return result

    def get_price(self, vehicle_code: str, ref_no: int,
                  color_code: str = '', part_no_prefix: str = '',
                  grade_code: str = '', body_code: int = 0,
                  neo_parts_no: str = '') -> dict:
        """特定ref_noの価格を取得

        優先順位:
          1. *83.DB (カラーコード一致)
          2. *11.DB 品番逆引き (neo_parts_no指定時: 品番完全一致エントリ優先) ← NEW
          3. *11.DB (grade_code指定時: option_code一致エントリ優先)
          4. *11.DB (最初の非ゼロ価格エントリ)

        Args:
            grade_code:   NEO Car.GradeCode (例 'A','B','C')。指定時にgrade-aware選択
            body_code:    NEO Car.BodyCode (例 10, 20)。grade_codeと合わせて使用
            neo_parts_no: NEO ERParts.PartsNo。指定時に*11.DBを品番で逆引きして
                          正確なグレードエントリを特定（④カテゴリ対策）
        """
        result = {'ref_no': ref_no, 'source': None, 'price': 0,
                  'parts_no': '', 'color': '', 'quantity': 1,
                  'grade_flags': ''}

        # *83.DB（カラーバリアント）
        if color_code:
            recs_83 = self.load_83db(vehicle_code)
            matched = [r for r in recs_83 if r['ref_no'] == ref_no]
            if matched:
                color_match = [r for r in matched if r['color'] == color_code]
                if color_match:
                    best = color_match[0]
                    result.update(price=best['price'], parts_no=best['parts_no'],
                                  color=best['color'], source='*83.DB/color')
                    return result
                if part_no_prefix:
                    pno_match = [r for r in matched if r['parts_no'][:len(part_no_prefix)] == part_no_prefix]
                    if pno_match:
                        best = pno_match[0]
                        result.update(price=best['price'], parts_no=best['parts_no'],
                                      color=best['color'], source='*83.DB/partno')
                        return result

        # *11.DB（全部品価格）
        recs_11 = self.load_11db(vehicle_code)
        parts_12 = self.load_12db(vehicle_code)
        candidates = [r for r in recs_11 if r['ref_no'] == ref_no and r['price'] > 0]
        if not candidates:
            return result

        qty = parts_12.get(ref_no, {}).get('quantity', 1)

        # --- 優先度1: 品番ベース逆引き (neo_parts_no) ---
        # NEO見積の実品番と*11.DBエントリの品番を照合 → 正確なグレードエントリ特定
        # 効果: ④カテゴリ（opt_codeあるが*11.DB不一致 315件）を解消
        best = None
        if neo_parts_no:
            def _norm_pno(s: str) -> str:
                """品番を正規化: ハイフン・スペース除去, 先頭11文字"""
                return s.replace('-', '').replace(' ', '').strip()[:11]

            neo_clean = _norm_pno(neo_parts_no)
            if neo_clean:
                pno_match = [r for r in candidates
                             if _norm_pno(r['parts_no']) == neo_clean]
                if pno_match:
                    best = pno_match[-1]  # 最後のエントリ = 最新改訂版
                    result.update(price=best['price'] * qty, parts_no=best['parts_no'],
                                  quantity=qty, grade_flags=best['grade_flags'],
                                  source='*11.DB/partno_rev')
                    return result

        # --- 優先度2: grade_code → option_code でグレード選択 ---
        if grade_code and body_code:
            opt = self.get_grade_option_code(vehicle_code, body_code, grade_code)
            if opt:
                # 1. grade_flags == opt 完全一致 (最後のエントリ = 最新改訂版)
                exact = [r for r in candidates if r['grade_flags'] == opt]
                if exact:
                    best = exact[-1]
                else:
                    # 2. grade_flags が opt を単語として含む
                    import re as _re
                    word = [r for r in candidates
                            if _re.search(r'(?:^|\s)' + _re.escape(opt) + r'(?:\s|$)',
                                          r['grade_flags'])]
                    if word:
                        best = word[-1]

        # フォールバック: 最初の非ゼロエントリ
        if best is None:
            best = candidates[0]

        result.update(price=best['price'] * qty, parts_no=best['parts_no'],
                      quantity=qty, grade_flags=best['grade_flags'],
                      source='*11.DB')
        return result

    def get_price_variants(self, vehicle_code: str, ref_no: int,
                           color_code: str = '') -> list[dict]:
        """ref_no の全価格バリアントを返す (グレード一覧表示用)

        Returns: grade_flags ごとの価格リスト (価格0のエントリは除外)
        """
        variants = []

        # *83.DB
        recs_83 = self.load_83db(vehicle_code)
        for r in recs_83:
            if r['ref_no'] == ref_no and r['price'] > 0:
                variants.append({
                    'source': '*83.DB', 'grade_flags': '',
                    'price': r['price'], 'parts_no': r['parts_no'],
                    'color': r.get('color', ''),
                })

        # *11.DB
        recs_11 = self.load_11db(vehicle_code)
        parts_12 = self.load_12db(vehicle_code)
        qty = parts_12.get(ref_no, {}).get('quantity', 1)
        for r in recs_11:
            if r['ref_no'] == ref_no and r['price'] > 0:
                variants.append({
                    'source': '*11.DB', 'grade_flags': r['grade_flags'],
                    'price': r['price'] * qty, 'unit_price': r['price'],
                    'parts_no': r['parts_no'], 'color': '',
                    'work_code': r['work_code'],
                })

        return variants

    def get_work_index(self, vehicle_code: str, ref_no: int,
                       labor_rate: float = 73.8, wi_round: int = 10) -> list[dict]:
        """特定ref_noの全作業指数を返す（生データ）

        Returns: ref_noに対応する*15.DBレコード全件
        各レコード: {wi, sub_ref, section, grade, flag, computed_wage}
        """
        db15 = self.load_15db(vehicle_code)
        recs = db15.get(ref_no, [])

        results = []
        for r in recs:
            wage = round(r['wi'] * labor_rate / wi_round) * wi_round if wi_round else round(r['wi'] * labor_rate)
            results.append({
                'ref_no': ref_no,
                'work_index': r['wi'],
                'computed_wage': wage,
                'section': r['section'],
                'grade': r['grade'],
                'sub_ref': r['sub_ref'],
                'source': '*15.DB',
            })
        return results

    def match_wage(self, vehicle_code: str, ref_no: int, target_wage: int,
                   labor_rate: float = 73.8, wi_round: int = 10,
                   tolerance: int = 50) -> Optional[dict]:
        """NEO工賃に最も近い*15.DB WI（またはWIの組み合わせ）を返す

        マッチング戦略:
          1. 単一WI一致 (single)
          2. 2エントリ合算 (combo2)
          3. 3エントリ合算 (combo3)
          4. 4エントリ合算 (combo4)
          5. sub_ref経由クロスリファレンス (xref)
        板金工賃は*15.DB対象外（面積ベース算出）のためNoneを返す

        Returns: {method, work_index, computed_wage, entries} or None
        """
        from itertools import combinations

        db15 = self.load_15db(vehicle_code)
        recs = db15.get(ref_no, [])
        if not recs:
            return None

        def _wage(wi_sum):
            return round(wi_sum * labor_rate / wi_round) * wi_round if wi_round else round(wi_sum * labor_rate)

        wis = [r['wi'] for r in recs if r['wi'] > 0]

        # 1. 単一WI一致
        for i, w in enumerate(wis):
            if abs(_wage(w) - target_wage) <= tolerance:
                return {'method': 'single', 'work_index': w,
                        'computed_wage': _wage(w), 'entries': [recs[i]]}

        # 2-4. コンボ（2〜4エントリの合算）
        for n in range(2, min(5, len(wis) + 1)):
            for combo in combinations(range(len(wis)), n):
                s = sum(wis[k] for k in combo)
                if abs(_wage(s) - target_wage) <= tolerance:
                    return {'method': f'combo{n}', 'work_index': s,
                            'computed_wage': _wage(s),
                            'entries': [recs[k] for k in combo]}

        # 5. sub_ref経由クロスリファレンス
        sub_refs = set(r['sub_ref'] for r in recs
                       if r['sub_ref'] > 0 and r['sub_ref'] != ref_no)
        for sr in sub_refs:
            sr_recs = db15.get(sr, [])
            sr_wis = [r['wi'] for r in sr_recs if r['wi'] > 0]
            all_wis = wis + sr_wis
            all_recs = recs + sr_recs
            for n in range(2, min(4, len(all_wis) + 1)):
                for combo in combinations(range(len(all_wis)), n):
                    s = sum(all_wis[k] for k in combo)
                    if abs(_wage(s) - target_wage) <= tolerance:
                        return {'method': f'xref_combo{n}', 'work_index': s,
                                'computed_wage': _wage(s),
                                'entries': [all_recs[k] for k in combo]}

        return None

    def match_wage_by_time(self, vehicle_code: str, ref_no: int,
                           target_time: float,
                           wi_round: int = 10,
                           tolerance: int = 20,
                           grade_code: str = '',
                           body_code: int = 0) -> Optional[tuple]:
        """NEO ERParts.Time フィールドから直接 *15.DB WI をマッチング

        NEO ERParts.Time = 作業時間 (時間単位, 例 1.0 = 100WI)
        target_WI = round(target_time × 10) × 10 (wi_round=10で0.1h単位に丸め)

        レバレートが不要なため match_wage() より信頼性が高い (推奨)

        grade_code + body_code を指定すると *15.DB セクションをグレードに絞り込む。
        セクション絞り込み後もマッチしない場合は全セクションにフォールバック。

        DisposalCode 0(取替) / 1(脱着) 専用
        板金 (DisposalCode=2) は *15.DB 対象外のため None を返す

        Returns:
            (method: str, work_index: int, wi_list: list[int]) or None
            method例: 'single', 'single~', 'combo2', 'combo3', 'xref_combo2'
            gradeフィルタ適用時は末尾に '/g' が付く (例: 'single/g', 'combo2/g')

        例:
            result = engine.match_wage_by_time('J97', 3810, target_time=1.0)
            # → ('single', 100, [100])
            result = engine.match_wage_by_time('W53', 3100, 0.7, grade_code='C', body_code=20)
            # → ('combo2/g', 70, [60, 10])
        """
        if target_time is None or target_time <= 0:
            return None

        db15 = self.load_15db(vehicle_code)
        recs = db15.get(ref_no, [])
        if not recs:
            return None

        # target_WI = 時間 × 100 (wi_round=10 で 0.1h 単位に丸め)
        target_wi = round(target_time * 10) * wi_round

        # グレードセクション絞り込み
        sec_recs = recs
        grade_suffix = ''
        if grade_code and body_code:
            sec_letter = self.get_grade_section(vehicle_code, body_code, grade_code)
            if sec_letter:
                filtered = [r for r in recs if r.get('sec_letter', '') == sec_letter]
                if filtered:
                    sec_recs = filtered
                    grade_suffix = '/g'
                # 絞り込み結果が空の場合は全セクション(sec_recs=recs)にフォールバック

        wis = [r['wi'] for r in sec_recs if r['wi'] > 0]
        if not wis:
            # グレードセクションにWIなし → 全レコードで再試行
            wis = [r['wi'] for r in recs if r['wi'] > 0]
            grade_suffix = ''
            sec_recs = recs
        if not wis:
            return None

        from itertools import combinations as _comb

        def _try_match(w_list, suffix):
            # 1. 単一完全一致
            for w in w_list:
                if w == target_wi:
                    return (f'single{suffix}', w, [w])
            # 2. 誤差±tolerance まで単一一致
            best = min(w_list, key=lambda w: abs(w - target_wi))
            if abs(best - target_wi) <= tolerance:
                return (f'single~{suffix}', best, [best])
            # 3-6. コンボ (2〜5エントリ合算)
            for n in range(2, min(6, len(w_list) + 1)):
                for combo in _comb(range(len(w_list)), n):
                    s = sum(w_list[k] for k in combo)
                    if s == target_wi:
                        return (f'combo{n}{suffix}', s, [w_list[k] for k in combo])
                    if abs(s - target_wi) <= tolerance:
                        return (f'combo{n}~{suffix}', s, [w_list[k] for k in combo])
            return None

        # グレードセクション絞り込みで試行
        res = _try_match(wis, grade_suffix)
        if res:
            return res

        # グレード絞り込みで失敗した場合 → 全セクションで再試行
        if grade_suffix:
            all_wis = [r['wi'] for r in recs if r['wi'] > 0]
            res = _try_match(all_wis, '')
            if res:
                return res

        # sub_ref 経由クロスリファレンス
        sub_refs = set(r['sub_ref'] for r in recs if r['sub_ref'] > 0 and r['sub_ref'] != ref_no)
        for sr in sub_refs:
            sr_recs = db15.get(sr, [])
            all_wis = wis + [r['wi'] for r in sr_recs if r['wi'] > 0]
            for n in range(2, min(4, len(all_wis) + 1)):
                for combo in _comb(range(len(all_wis)), n):
                    s = sum(all_wis[k] for k in combo)
                    if abs(s - target_wi) <= tolerance:
                        return (f'xref_combo{n}', s, [all_wis[k] for k in combo])

        return None

    def list_vehicles(self) -> list[str]:
        """利用可能な全車種コードを返す"""
        vehicles = []
        for prefix_dir in sorted(glob.glob(os.path.join(self.root, '?'))):
            prefix = os.path.basename(prefix_dir)
            if not prefix.isalpha():
                continue
            for vdir in sorted(glob.glob(os.path.join(prefix_dir, f'{prefix}*'))):
                vc = os.path.basename(vdir)
                if self._find_db(vc, '11.DB'):
                    vehicles.append(vc)
        return vehicles

    def find_vehicle_by_partno(self, part_number: str, prefixes: list[str] = None) -> list[tuple[str, int]]:
        """品番からAddata車種コードを自動検索
        Returns: [(vehicle_code, match_count), ...] sorted by match_count desc
        """
        pno_clean = part_number.replace('-', '').replace(' ', '').strip()[:11]
        if not pno_clean:
            return []

        search_list = prefixes or sorted(set(v[0] for v in self.list_vehicles()))
        results = []

        for prefix in search_list:
            vehicles = [v for v in self.list_vehicles() if v.startswith(prefix)]
            for vc in vehicles:
                try:
                    recs = self.load_11db(vc)
                    for r in recs:
                        rp = r['parts_no'].replace('-', '').replace(' ', '').strip()[:11]
                        if rp == pno_clean:
                            results.append((vc, r['price'], r['name']))
                            break
                except Exception:
                    pass

        return results

    def export_csv(self, vehicle_code: str, output_path: str = None) -> str:
        """車種の全部品データをCSV出力"""
        import csv, io
        parts = self.get_all_parts(vehicle_code)
        if not parts:
            return ''

        if output_path is None:
            output_path = f'{vehicle_code}_parts.csv'

        with open(output_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['ref_no', '部品名', '品番', '単価', '数量', '合計価格', '作業区分', 'グレード'])
            seen = set()
            for p in parts:
                key = (p['ref_no'], p['work_code'], p['grade_flags'])
                if key in seen:
                    continue
                seen.add(key)
                writer.writerow([
                    p['ref_no'], p['name'], p['parts_no'],
                    p['unit_price'], p['quantity'], p['total_price'],
                    p['work_code'], p['grade_flags']
                ])

        return output_path

    # ================================================================
    # v13+ 拡張: KATB010.DB / KA06_ALL.DB 連携 (Phase 2)
    # ================================================================
    # KATB010.DB レコード長 = 42 bytes (XOR 0xFF 復号後)
    # フォーマット: '02' + MakerCode(A〜J) + cp932 メーカー名 (空白パディング) + CRLF
    # 観測 (2026-05-01 phase0_db_dump.py):
    #   02A 三菱 / 02B ダイハツ / 02C スバル / 02D ホンダ / 02E いすゞ
    #   02F マツダ / 02G 日産 / 02H スズキ / 02I トヨタ / 02J フォルクスワーゲン
    _KATB010_REC_LEN = 42

    def _load_katb010(self) -> dict:
        """KATB010.DB → MakerCode (A〜J) ⇄ メーカー名 双方向辞書を返す。"""
        cache_key = '_katb010'
        if cache_key in self._cache:
            return self._cache[cache_key]
        path = os.path.join(self.root, 'COM', 'KATB010.DB')
        out: dict = {}
        if not os.path.exists(path):
            self._cache[cache_key] = out
            return out
        try:
            with open(path, 'rb') as f:
                raw = f.read()
            dec = bytes(b ^ 0xFF for b in raw)
            text = dec.decode('cp932', errors='replace')
            for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
                line = line.rstrip()
                if len(line) < 4 or not line.startswith('02'):
                    continue
                code = line[2:3]
                name = line[3:].strip()
                if code and name:
                    out[code] = name
                    out[name] = code
        except Exception:
            pass
        self._cache[cache_key] = out
        return out

    def get_maker_code(self, vehicle_code: str) -> str:
        """vehicle_code (3-byte 例: C10/W53/J55) の先頭 1 文字 (MakerCode A〜J) を返す。"""
        if not vehicle_code:
            return ''
        vc = str(vehicle_code).strip().upper()
        return vc[0] if vc else ''

    def get_maker_name(self, vehicle_code_or_makercode: str) -> str:
        """vehicle_code (例: 'C10') または MakerCode (例: 'C') から日本語メーカー名を返す。"""
        s = str(vehicle_code_or_makercode or '').strip().upper()
        if not s:
            return ''
        return self._load_katb010().get(s[0], '')

    def lookup_by_chassis_full(self, reg_date: str = '', model_designation: str = '',
                               category_number: str = '') -> Optional[dict]:
        """Layer 1: KA06_ALL.DB を初度登録 + 型式指定 + 類別区分 で完全一致検索。

        引数:
          reg_date: 'YYYYMM' / 'YYYYMMDD' / 'YYYYMM00' いずれも受理 (先頭 6 桁を使用)
          model_designation: 型式指定番号 (例: '19557')
          category_number:   類別区分番号 (例: '0172')

        戻り値: {'vehicle_code', 'reg_yyyymm', 'cat_padded', 'desig_padded',
                 'line', 'match_layer'} or None
        """
        ka06 = os.path.join(self.root, 'COM', 'KA06_ALL.DB')
        if not os.path.exists(ka06):
            return None
        md = str(model_designation or '').strip()
        cn = str(category_number or '').strip()
        if not md or not cn:
            return None
        padded_desig = md.zfill(5)
        padded_cat = cn.zfill(4)
        reg_yyyymm = ''
        if reg_date:
            digits = ''.join(ch for ch in str(reg_date) if ch.isdigit())
            if len(digits) >= 6:
                reg_yyyymm = digits[:6]
        cache_key = '_ka06_lines'
        if cache_key in self._cache:
            lines = self._cache[cache_key]
        else:
            try:
                with open(ka06, 'rb') as f:
                    raw = f.read()
                text = bytes(b ^ 0xFF for b in raw).decode('cp932', errors='replace')
                lines = [l for l in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
                         if l.startswith('06')]
            except Exception:
                lines = []
            self._cache[cache_key] = lines
        best = None
        for line in lines:
            if padded_desig not in line:
                continue
            if padded_cat not in line:
                continue
            vc = line[2:5].strip()
            if not vc:
                continue
            rec = {
                'vehicle_code': vc,
                'reg_yyyymm': reg_yyyymm,
                'line': line,
                'cat_padded': padded_cat,
                'desig_padded': padded_desig,
                'match_layer': 1,
            }
            if reg_yyyymm and reg_yyyymm in line:
                return rec
            if best is None:
                best = rec
        return best

    def lookup_by_serial_no(self, serial_no: str) -> Optional[dict]:
        """Layer 3: 車台番号 prefix 逆引き。"""
        if not serial_no:
            return None
        s = str(serial_no).strip().upper()
        head = s.split('-')[0]
        if not head:
            return None
        for n in (3, 4, 5):
            cand = head[:n]
            if not cand:
                continue
            if len(cand) >= 3 and cand[0].isalpha():
                vc = cand[:3]
                folder = self._vehicle_folder(vc)
                if os.path.isdir(folder):
                    return {
                        'vehicle_code': vc,
                        'serial_head': head,
                        'match_layer': 3,
                    }
        return None

    def get_vehicle_meta(self, vehicle_code: str, body_code: int = 0,
                         grade_code: str = '') -> dict:
        """vehicle_code に紐づくメタ情報を集約して返す。"""
        meta: dict = {'vehicle_code': vehicle_code}
        if not vehicle_code:
            return meta
        meta['maker_code'] = self.get_maker_code(vehicle_code)
        meta['maker_name'] = self.get_maker_name(vehicle_code)
        for suf in ('11.DB', '83.DB', '12.DB', '15.DB', '09.DB'):
            meta[f'has_{suf[:2]}db'] = bool(self._find_db(vehicle_code, suf))
        if body_code and grade_code:
            try:
                meta['grade_option_code'] = self.get_grade_option_code(
                    vehicle_code, body_code, grade_code)
            except Exception:
                meta['grade_option_code'] = ''
            try:
                meta['grade_section'] = self.get_grade_section(
                    vehicle_code, body_code, grade_code)
            except Exception:
                meta['grade_section'] = ''
        return meta

    def identify_vehicle_3layer(self, vehicle_data: dict) -> dict:
        """3 階層フォールバック車種特定の統合 API。"""
        if not isinstance(vehicle_data, dict):
            vehicle_data = {}
        md = str(vehicle_data.get('car_model_designation') or
                 vehicle_data.get('model_designation') or '').strip()
        cn = str(vehicle_data.get('car_category_number') or
                 vehicle_data.get('category_number') or '').strip()
        rd = str(vehicle_data.get('car_reg_date') or
                 vehicle_data.get('first_reg_date') or
                 vehicle_data.get('reg_date') or '').strip()
        sn = str(vehicle_data.get('car_serial_no') or
                 vehicle_data.get('serial_no') or '').strip()

        rec = self.lookup_by_chassis_full(rd, md, cn)
        if rec and rec.get('vehicle_code'):
            vc = rec['vehicle_code']
            return {
                'found': True,
                'vehicle_code': vc,
                'match_layer': 1,
                'method': 'layer1_full',
                'maker_code': self.get_maker_code(vc),
                'maker_name': self.get_maker_name(vc),
                'reason': f'KA06 3キー一致 desig={md} cat={cn} reg={rd}',
            }

        try:
            from auto_matching import AddataEngine  # type: ignore
            eng = AddataEngine(self.root)
            mc = str(vehicle_data.get('car_model') or
                     vehicle_data.get('model_code') or '').strip()
            mk = str(vehicle_data.get('maker') or
                     vehicle_data.get('car_maker') or '').strip()
            cnm = str(vehicle_data.get('car_name') or '').strip()
            veh, _err = eng.identify_vehicle(md, cn, mc,
                                             reg_date=rd, maker=mk, car_name=cnm)
            if veh and veh.get('match_layer') == 2 and veh.get('vehicle_code'):
                vc = veh['vehicle_code']
                return {
                    'found': True,
                    'vehicle_code': vc,
                    'match_layer': 2,
                    'method': 'layer2_attr',
                    'maker_code': self.get_maker_code(vc),
                    'maker_name': self.get_maker_name(vc),
                    'reason': 'AddataEngine fuzzy 一致',
                }
        except Exception:
            pass

        if sn:
            rec = self.lookup_by_serial_no(sn)
            if rec and rec.get('vehicle_code'):
                vc = rec['vehicle_code']
                return {
                    'found': True,
                    'vehicle_code': vc,
                    'match_layer': 3,
                    'method': 'layer3_serial',
                    'maker_code': self.get_maker_code(vc),
                    'maker_name': self.get_maker_name(vc),
                    'reason': f'serial_no={sn} prefix 一致',
                }

        return {
            'found': False,
            'vehicle_code': None,
            'match_layer': 4,
            'method': 'no_db',
            'maker_code': '',
            'maker_name': '',
            'reason': f'全層未ヒット desig={md} cat={cn} reg={rd} serial={sn}',
        }


# ============================================================
# CLI実行
# ============================================================
if __name__ == '__main__':
    import json

    engine = AddataSearchEngine()

    def print_usage():
        print("Addata汎用データベース検索エンジン")
        print("=" * 60)
        print("使用方法:")
        print("  python _addata_db_search.py <vehicle_code>           全部品一覧")
        print("  python _addata_db_search.py <vehicle_code> <ref_no>  特定部品詳細")
        print("  python _addata_db_search.py <vehicle_code> --csv     CSV出力")
        print("  python _addata_db_search.py --find <品番>            品番→車種コード検索")
        print("  python _addata_db_search.py --list                   全車種一覧")
        print()
        vehicles = engine.list_vehicles()
        print(f"利用可能: {len(vehicles)}車種")
        prefixes = sorted(set(v[0] for v in vehicles))
        for p in prefixes:
            count = len([v for v in vehicles if v.startswith(p)])
            print(f"  {p}: {count}車種")

    if len(sys.argv) < 2:
        print_usage()
        sys.exit(0)

    # --find: 品番から車種コード検索
    if sys.argv[1] == '--find':
        if len(sys.argv) < 3:
            print("使用方法: python _addata_db_search.py --find <品番> [プレフィックス]")
            sys.exit(1)
        pno = sys.argv[2]
        prefixes = list(sys.argv[3].upper()) if len(sys.argv) >= 4 else None
        print(f"品番 {pno} を検索中...")
        results = engine.find_vehicle_by_partno(pno, prefixes)
        if results:
            print(f"{len(results)}件一致:")
            for vc, price, name in results:
                print(f"  {vc}: {name} = {price:,}円")
        else:
            print("一致なし")
        sys.exit(0)

    # --list: 全車種一覧
    if sys.argv[1] == '--list':
        vehicles = engine.list_vehicles()
        for v in vehicles:
            recs = engine.load_11db(v)
            print(f"  {v}: {len(recs)} parts")
        sys.exit(0)

    vc = sys.argv[1].upper()
    folder = engine._vehicle_folder(vc)
    if not os.path.isdir(folder):
        print(f"エラー: 車種 {vc} のフォルダが見つかりません: {folder}")
        sys.exit(1)

    # --csv: CSV出力
    if len(sys.argv) >= 3 and sys.argv[2] == '--csv':
        out = engine.export_csv(vc)
        print(f"CSV出力: {out}")
        sys.exit(0)

    if len(sys.argv) >= 3 and sys.argv[2].isdigit():
        # 特定ref_noの詳細
        ref_no = int(sys.argv[2])
        print(f"=== {vc} ref_no={ref_no} ===\n")

        # *11.DB 全バリアント
        recs_11 = engine.load_11db(vc)
        matched_11 = [r for r in recs_11 if r['ref_no'] == ref_no]
        if matched_11:
            print(f"*11.DB ({len(matched_11)}件):")
            for r in matched_11:
                print(f"  price={r['price']:>8,}  pno={r['parts_no']:20} work={r['work_code']:5} grade={r['grade_flags']}")

        # *83.DB カラーバリアント
        recs_83 = engine.load_83db(vc)
        matched_83 = [r for r in recs_83 if r['ref_no'] == ref_no]
        if matched_83:
            print(f"\n*83.DB ({len(matched_83)}件):")
            for r in matched_83:
                print(f"  price={r['price']:>8,}  pno={r['parts_no']:20} color={r.get('color','')}")

        # *12.DB 名称・数量
        parts_12 = engine.load_12db(vc)
        if ref_no in parts_12:
            p = parts_12[ref_no]
            print(f"\n*12.DB: 名称={p['name']}  数量={p['quantity']}  作業={p['methods']}")

        # *15.DB 作業指数
        wages = engine.get_work_index(vc, ref_no)
        if wages:
            print(f"\n*15.DB ({len(wages)}件):")
            for w in wages:
                print(f"  指数={w['work_index']:>4} → {w['computed_wage']:>6,}円  sec={w['section']} grade={w['grade']} sub_ref={w['sub_ref']}")

        if not matched_11 and not matched_83:
            print("データなし")

    else:
        # 全部品サマリー
        parts = engine.get_all_parts(vc)
        recs_12 = engine.load_12db(vc)
        db15 = engine.load_15db(vc)

        print(f"=== {vc} 全部品データ ===")
        print(f"*11.DB: {len(engine.load_11db(vc))} records")
        print(f"*12.DB: {len(recs_12)} parts")
        print(f"*15.DB: {len(db15)} ref_nos ({sum(len(v) for v in db15.values())} entries)")
        print(f"*83.DB: {len(engine.load_83db(vc))} records")
        print(f"*09.DB: {len(engine.load_09db(vc))} grades")
        unique_refs = set(p['ref_no'] for p in parts)
        priced = [p for p in parts if p['unit_price'] > 0]
        print(f"\n部品数（ユニークref_no）: {len(unique_refs)}")
        print(f"価格あり: {len(priced)}")

        print(f"\n--- 先頭20件 ---")
        seen = set()
        count = 0
        for p in parts:
            if p['ref_no'] in seen:
                continue
            seen.add(p['ref_no'])
            wi_info = ''
            wi_recs = db15.get(p['ref_no'], [])
            if wi_recs:
                wis = sorted(set(r['wi'] for r in wi_recs if r['wi'] > 0))
                wi_info = f" WI={wis[0]}" if len(wis) == 1 else f" WI={wis[:3]}"
            print(f"  [{p['ref_no']:>5}] {p['name'][:25]:25} {p['parts_no']:20} {p['total_price']:>8,}円 {p['work_code']}{wi_info}")
            count += 1
            if count >= 20:
                break
