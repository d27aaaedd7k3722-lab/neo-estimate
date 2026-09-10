#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
PDFGradeIdentifier — PDF見積の部品名称+価格からグレード・装備品を推定

[設計概要]
  PDF見積書の部品名称（表記ゆれあり）と価格を入力として、
  ADDATAの*11.DB/*12.DB に照合し、grade_flags の投票によって
  車種のグレードおよびオプション装備品を推定する。

[使用例]
  from _grade_identifier import PDFGradeIdentifier
  from _addata_db_search import AddataSearchEngine

  engine = AddataSearchEngine(r'C:\Addata')
  gid = PDFGradeIdentifier(engine, vcode='J97', body_code=10)

  for part_name, price in pdf_parts:
      gid.add_part(part_name, price)

  result = gid.identify()
  # → {
  #     'grade_code':  'B',
  #     'grade_name':  'BＧ・Ｌ',
  #     'confidence':  72,
  #     'grade_votes': {'A':3, 'B':8, 'C':2},
  #     'option_codes': [('IK',4), ('B3',2), ('Z',1)],
  #     'discriminating_parts': 13,
  #     'matched_log': [...],
  #   }

[略称辞書 ABBR_MAP について]
  PDF見積書でよく見られる英語略称を正規化するマッピング。
  正規表現ベースで case-insensitive に適用する。
  順序重要: 長いパターンを先に記述（'ASSY' より先に 'SUB-ASSY' を処理 等）
"""

import re
from collections import defaultdict
from typing import Optional

try:
    import jaconv
    HAS_JACONV = True
except ImportError:
    HAS_JACONV = False


# ============================================================
# 略称→全角カナ変換辞書
# PDF見積書でよく見られる英語略称 → ADDATAカナ表記
# ============================================================
ABBR_MAP = [
    # 位置・方向（先に処理）
    (r'\bSUB[\s\-]?ASSY\b',    'ｻﾌﾞｱｯｼｰ'),
    (r'\bSUB[\s\-]?ASSM\b',    'ｻﾌﾞｱｯｼｰ'),
    (r'\bSUB\b',                'ｻﾌﾞ'),
    (r'\bASSY\b',               'ｱｯｼｰ'),
    (r'\bASSEM\b',              'ｱｯｼｰ'),
    (r'\bW/S\b',                'ｳｲﾝﾄﾞｼｰﾙﾄﾞ'),
    (r'\bW[\s\-]?SHIELD\b',    'ｳｲﾝﾄﾞｼｰﾙﾄﾞ'),
    (r'\bFRT\b|\bFR\b|\bFr\b', 'F'),       # フロント系は先頭Fに統一
    (r'\bRR\b|\bRr\b',          'R'),       # リヤ系は先頭Rに統一
    (r'\bFRONT\b',              'F'),
    (r'\bREAR\b',               'R'),
    (r'\bLH\b',                 '左'),
    (r'\bRH\b',                 '右'),
    (r'\bLWR\b|\bLOWER\b',     'ﾛｱ'),
    (r'\bUPR\b|\bUPPER\b',     'ｱｯﾊﾟ'),
    (r'\bOTR\b|\bOUTER\b',     'ｱｳﾀ'),
    (r'\bINR\b|\bINNER\b',     'ｲﾝﾅ'),
    (r'\bCTR\b|\bCENTER\b|\bCENTRE\b', 'ｾﾝﾀ'),
    # 部品種別
    (r'\bBRKT\b|\bBRACKET\b',  'ﾌﾞﾗｹｯﾄ'),
    (r'\bPNL\b|\bPANEL\b',     'ﾊﾟﾈﾙ'),
    (r'\bMTG\b|\bMOUNTING\b',  'ﾏｳﾝﾃｨﾝｸﾞ'),
    (r'\bRINF\b|\bRNFR\b|\bREINF\b|\bREINFORCE(MENT)?\b', 'ﾚｲﾝﾌｫｰｽ'),
    (r'\bCOVER\b|\bCVR\b',     'ｶﾊﾞｰ'),
    (r'\bSUPPORT\b|\bSUPT\b',  'ｻﾎﾟｰﾄ'),
    (r'\bHINGE\b',              'ﾋﾝｼﾞ'),
    (r'\bGASKET\b|\bGSKT\b',   'ｶﾞｽｹｯﾄ'),
    (r'\bBRACE\b',              'ﾌﾞﾚｰｽ'),
    (r'\bLINING\b',             'ﾗｲﾆﾝｸﾞ'),
    (r'\bLINER\b',              'ﾗｲﾅ'),
    (r'\bSHIELD\b',             'ｼｰﾙﾄﾞ'),
    (r'\bSEAL\b',               'ｼｰﾙ'),
    (r'\bBUMPER\b',             'ﾊﾞﾝﾊﾟ'),
    (r'\bFENDER\b|\bFNDR\b',   'ﾌｴﾝﾀﾞ'),
    (r'\bHOOD\b',               'ﾎｰﾄﾞ'),
    (r'\bDOOR\b|\bDR\b',        'ﾄﾞｱ'),
    (r'\bHEADLAMP\b|\bHEADLIGHT\b|\bH/L\b', 'ﾍﾂﾄﾞﾗﾝﾌﾟ'),
    (r'\bTAILLAMP\b|\bTAILLIGHT\b|\bT/L\b', 'ﾃｰﾙﾗﾝﾌﾟ'),
    (r'\bFOGLAMP\b|\bFOGLIGHT\b|\bF/L\b',   'ﾌｫｸﾞﾗﾝﾌﾟ'),
    (r'\bRADIATOR\b|\bRAD\b',  'ﾗｼﾞｴｰﾀ'),
    (r'\bSENSOR\b|\bSENSR\b',  'ｾﾝｻ'),
    (r'\bMIRROR\b|\bMRR\b',    'ﾐﾗ'),
    (r'\bSPOILER\b',            'ｽﾎﾟｲﾗ'),
    (r'\bMOLDING\b|\bMLDG\b',  'ﾓｰﾙ'),
    (r'\bSTRUT\b',              'ｽﾄﾗｯﾄ'),
    (r'\bGRILLE?\b|\bGRILL?\b','ｸﾞﾘﾙ'),
    (r'\bSKID\b',               'ｽｷｯﾄﾞ'),
    (r'\bPROTECTOR\b|\bPROT\b','ﾌﾟﾛﾃｸﾀ'),
    (r'\bEXTENSION\b|\bEXT\b', 'ｴｸｽﾃﾝｼｮﾝ'),
    (r'\bBRACKET\b',            'ﾌﾞﾗｹｯﾄ'),
    (r'\bFLANGE\b',             'ﾌﾗﾝｼﾞ'),
    (r'\bSILL\b',               'ｼﾙ'),
    (r'\bCOLLAR\b',             'ｶﾗ'),
    (r'\bRETAINER\b',           'ﾘﾃｰﾅ'),
    (r'\bADAPTOR?\b',           'ｱﾀﾞﾌﾟﾀ'),
]

# コンパイル済みパターンリスト (case-insensitive)
_ABBR_COMPILED = [(re.compile(p, re.IGNORECASE), r) for p, r in ABBR_MAP]


def normalize_pdf_name(name: str) -> str:
    """PDF見積書の部品名称をADDATA表記に近い形に正規化

    処理順:
      1. 略称辞書 (ABBR_MAP) 適用
      2. jaconv 全角→半角カナ変換
      3. 大文字化・スペース除去
      4. 長音記号統一 (-/ー → ｰ)
    """
    n = str(name).strip()
    # 略称変換
    for pat, repl in _ABBR_COMPILED:
        n = pat.sub(repl, n)
    # jaconv で全角→半角カナ変換
    if HAS_JACONV:
        n = jaconv.z2h(n, ascii=True, digit=True, kana=True)
    n = n.upper().replace(' ', '').replace('　', '')
    # 長音記号統一
    n = n.replace('-', 'ｰ').replace('ー', 'ｰ')
    # 小文字カナ→大文字カナ
    _S2L = str.maketrans('ｧｨｩｪｫｯｬｭｮ', 'ｱｲｳｴｵﾂﾔﾕﾖ')
    n = n.translate(_S2L)
    return n


# ============================================================
# *09.DB から grade_code → grade_name マップを構築
# ============================================================
def build_grade_code_map(engine, vcode: str, body_code: int = 0) -> dict:
    """grade_code → grade_name のマップを返す

    body_code=0 の場合は全 body_code を統合（最初の出現を優先）
    例: W53 → {'A':'AＸ','B':'BＧ','C':'CＺ','D':'DＵ'}
    例: J97 → {'A':'AＧ','B':'BＧ・Ｌ','C':'CＧ・Ｌターボ',...}
    """
    lines = engine.load_09db(vcode)
    grade_code_map = {}

    for line in lines:
        if not line.startswith('09' + vcode):
            continue
        rest_full = line[2 + len(vcode):]
        body_str = rest_full[:4]
        try:
            body = int(body_str)
        except Exception:
            continue

        if body_code != 0 and body != body_code:
            continue

        rest = rest_full[4:]

        grade_code = None
        g_start = -1
        for j in range(len(rest) - 1):
            if ord(rest[j]) < 127 and rest[j].isalpha() and ord(rest[j + 1]) > 127:
                grade_code = rest[j]
                g_start = j
                break
        if not grade_code:
            continue

        ne = g_start + 1
        while ne < len(rest) and ord(rest[ne]) > 127:
            ne += 1
        grade_name = rest[g_start:ne].strip()

        if grade_code not in grade_code_map:
            grade_code_map[grade_code] = grade_name

    return grade_code_map


# ============================================================
# grade_flags パーサー
# ============================================================
def parse_grade_flags(gf: str, valid_grade_codes: set):
    """grade_flags を primary grades + option codes に分割

    valid_grade_codes: *09.DB から得た有効 grade_code の集合
    例: J97={'A','B','C','D','E'}, W53={'A','B','C','D'}

    grade_flags 例:
      'CE   Z C7' → primary={'C','E'}, options={'Z','C7'}
      'A    B'    → primary={'A','B'}, options=set()
      'GH'        → primary=set() (J97でGは無効), options={'GH'}
    """
    if not gf:
        return set(), set()
    tokens = gf.split()
    primary = set()
    options = set()
    for tok in tokens:
        if tok and all(c in valid_grade_codes for c in tok):
            primary.update(tok)
        else:
            options.add(tok)
    return primary, options


# ============================================================
# PDFGradeIdentifier 本体
# ============================================================
class PDFGradeIdentifier:
    """PDF見積の部品名称+価格からグレード・装備品を推定

    [マッチング優先順位]
      1. 名称完全一致 (normalize後) + 価格一致 → confidence×3
      2. 名称完全一致 (normalize後)              → confidence×2
      3. 名称部分一致 (core_t in core_m)         → confidence×1.5
      4. 価格一致のみ (差額<=500円)              → confidence×1

    [アルゴリズム]
      - *12.DB から ref_no を名称引き
      - *11.DB から ref_no の全エントリを取得
      - grade_flags が複数種あるエントリのみ識別に使用（識別不能エントリは除外）
      - primary grade_code に投票、option code にも投票
    """

    PRICE_TOLERANCE = 500   # 価格フォールバック許容誤差 (円)
    MIN_CORE_LEN    = 4     # 名称部分一致で有効とする最低文字数

    def __init__(self, engine, vcode: str, body_code: int = 0):
        self.engine     = engine
        self.vcode      = vcode
        self.body_code  = body_code

        # grade_code → grade_name マップ
        self.grade_map  = build_grade_code_map(engine, vcode, body_code)
        self.valid_gc   = set(self.grade_map.keys())

        # *11.DB: ref_no → entries
        self._recs_11   = engine.load_11db(vcode)
        self._by_ref    = defaultdict(list)
        for r in self._recs_11:
            if r['price'] > 0:
                self._by_ref[r['ref_no']].append(r)

        # *12.DB: ref_no → {name, quantity, methods}
        self._parts_12  = engine.load_12db(vcode)

        # 名称 → ref_no 逆引きインデックス (normalize済み)
        self._name_idx  = {}   # normalized_name → ref_no
        self._name_raw  = {}   # normalized_name → original_name (デバッグ用)
        for ref_no, info in self._parts_12.items():
            nn = self._normalize(info['name'])
            if nn:
                if nn not in self._name_idx:
                    self._name_idx[nn] = ref_no
                    self._name_raw[nn]  = info['name']

        # 投票集計
        self._grade_votes  = defaultdict(float)
        self._option_votes = defaultdict(float)
        self._disc_count   = 0
        self._matched_log  = []

    @staticmethod
    def _normalize(name: str) -> str:
        return normalize_pdf_name(name)

    @staticmethod
    def _norm_pno(s: str) -> str:
        return s.replace('-', '').replace(' ', '').strip()[:11]

    def add_part(self, part_name: str, price, parts_no: str = '') -> bool:
        """部品1件を追加して投票

        Returns True if this part contributed to grade voting
        """
        norm_t = self._normalize(str(part_name))
        if not norm_t:
            return False

        # ref_no を名称で検索
        ref_no = self._lookup_ref_no(norm_t)
        if ref_no is None:
            return False

        # *11.DB エントリ取得
        entries = self._by_ref.get(ref_no, [])
        if not entries:
            return False

        # 識別可能性チェック: grade_flags が複数種かつ primary が複数パターン
        flags_set = set(e['grade_flags'] for e in entries)
        if len(flags_set) <= 1:
            return False

        primary_sets = set()
        for e in entries:
            p, _ = parse_grade_flags(e['grade_flags'], self.valid_gc)
            primary_sets.add(frozenset(p))
        if len(primary_sets) <= 1:
            return False

        self._disc_count += 1

        # マッチングエントリ選択
        matched_entry, conf_mult = self._select_entry(
            entries, price, parts_no, norm_t)
        if matched_entry is None:
            return False

        gf = matched_entry['grade_flags']
        primary, options = parse_grade_flags(gf, self.valid_gc)

        for gc in primary:
            self._grade_votes[gc] += conf_mult
        for opt in options:
            self._option_votes[opt] += conf_mult

        pname_disp = str(part_name)[:18]
        self._matched_log.append({
            'part_name':   pname_disp,
            'ref_no':      ref_no,
            'grade_flags': gf,
            'primary':     sorted(primary),
            'options':     sorted(options),
            'conf_mult':   conf_mult,
            'price':       price,
        })
        return True

    def _lookup_ref_no(self, norm_name: str) -> Optional[int]:
        """normalize済み名称から ref_no を返す

        1. 完全一致
        2. 方向プレフィックス除去後の完全一致
        3. core 部分一致 (core_t in core_m, len>=MIN_CORE_LEN)
        """
        # 1. 完全一致
        if norm_name in self._name_idx:
            return self._name_idx[norm_name]

        # 方向プレフィックス除去
        core_t = re.sub(r'^[FR]', '', norm_name)

        # 2. 方向除去後完全一致
        for nn, ref_no in self._name_idx.items():
            core_m = re.sub(r'^[FR]', '', nn)
            if core_t == core_m and len(core_t) >= self.MIN_CORE_LEN:
                return ref_no

        # 3. 部分一致
        if len(core_t) >= self.MIN_CORE_LEN:
            best_ref = None
            best_score = 999
            for nn, ref_no in self._name_idx.items():
                core_m = re.sub(r'^[FR]', '', nn)
                if not core_m or len(core_m) < self.MIN_CORE_LEN:
                    continue
                if core_t in core_m or core_m in core_t:
                    score = abs(len(core_t) - len(core_m))
                    if score < best_score:
                        best_score = score
                        best_ref = ref_no
            if best_ref is not None:
                return best_ref

        return None

    def _select_entry(self, entries: list, price,
                      parts_no: str, norm_name: str):
        """エントリの中から最適なものを選択し (entry, conf_mult) を返す

        優先順位:
          1. 品番完全一致 → ×2.0
          2. 価格完全一致 → ×1.5
          3. 価格近似 (差額<=PRICE_TOLERANCE) → ×1.0
        """
        # 1. 品番マッチ
        if parts_no:
            pno_clean = self._norm_pno(str(parts_no))
            if pno_clean:
                for e in entries:
                    if self._norm_pno(e['parts_no']) == pno_clean:
                        return e, 2.0

        # 価格変換
        try:
            fprice = float(price)
        except (TypeError, ValueError):
            fprice = 0.0

        if fprice > 0:
            # 2. 価格完全一致
            for e in entries:
                if e['price'] == fprice:
                    return e, 1.5
            # 3. 価格近似
            best_diff = 999999
            best_entry = None
            for e in entries:
                diff = abs(e['price'] - fprice)
                if diff < best_diff and diff <= self.PRICE_TOLERANCE:
                    best_diff = diff
                    best_entry = e
            if best_entry is not None:
                return best_entry, 1.0

        return None, 0.0

    def identify(self) -> dict:
        """グレード推定結果を返す

        Returns dict:
          grade_code:          推定グレードコード ('A','B',... or None)
          grade_name:          推定グレード名 (全角, e.g. 'BＧ・Ｌ')
          confidence:          確信度 0-100
          grade_votes:         {grade_code: score} の全投票結果
          option_codes:        [(opt_code, score), ...] 上位12件
          discriminating_parts: 識別に使用した部品数
          grade_map:           grade_code → grade_name の全マップ
          matched_log:         マッチング詳細ログ
        """
        best_gc   = max(self._grade_votes, key=self._grade_votes.get) \
                    if self._grade_votes else None
        total     = sum(self._grade_votes.values())
        best_sc   = self._grade_votes.get(best_gc, 0) if best_gc else 0
        confidence = int(best_sc / total * 100) if total > 0 else 0

        top_opts  = sorted(self._option_votes.items(),
                           key=lambda x: -x[1])[:12]

        return {
            'grade_code':          best_gc,
            'grade_name':          self.grade_map.get(best_gc) if best_gc else None,
            'confidence':          confidence,
            'grade_votes':         dict(self._grade_votes),
            'option_codes':        top_opts,
            'discriminating_parts': self._disc_count,
            'grade_map':           self.grade_map,
            'matched_log':         self._matched_log,
        }

    def reset(self):
        """投票をリセット（同じ vehicle に対して別の見積を処理する際に使用）"""
        self._grade_votes.clear()
        self._option_votes.clear()
        self._disc_count = 0
        self._matched_log.clear()

    def summary(self) -> str:
        """識別結果の1行サマリを返す"""
        r = self.identify()
        gc   = r['grade_code'] or '?'
        gn   = r['grade_name'] or '(不明)'
        conf = r['confidence']
        disc = r['discriminating_parts']
        opts = ', '.join(o for o, _ in r['option_codes'][:5])
        return (f"グレード: {gc} {gn} (確信度{conf}%) "
                f"識別部品{disc}件 | オプション: {opts}")


# ============================================================
# スタンドアロン動作確認（NEOファイルで検証）
# ============================================================
if __name__ == '__main__':
    import sys, os, sqlite3, tempfile, zlib, glob
    sys.stdout.reconfigure(encoding='utf-8')
    from _addata_db_search import AddataSearchEngine

    ADDATA_DIR = r'C:\Addata'
    NEO_DIR    = r'Z:\ドキュメント\４月'
    MAX_FILES  = 100

    engine = AddataSearchEngine(ADDATA_DIR)

    def extract_neo_simple(neo_path):
        try:
            with open(neo_path, 'rb') as f:
                data = f.read()
        except Exception:
            return [], {}
        ck_pos = []
        pos = 0
        while pos < len(data):
            idx = data.find(b'CK', pos)
            if idx == -1: break
            ck_pos.append(idx)
            pos = idx + 2
        if ck_pos:
            ad = bytearray()
            zd = b''
            for i, cp in enumerate(ck_pos):
                np2 = ck_pos[i + 1] if i + 1 < len(ck_pos) else len(data)
                ch = data[cp + 2:np2]
                try:
                    do = zlib.decompressobj(-zlib.MAX_WBITS, zdict=zd)
                    d = do.decompress(ch)
                    ad.extend(d)
                    zd = bytes(ad[-32768:]) if len(ad) >= 32768 else bytes(ad)
                except Exception:
                    try:
                        d = zlib.decompress(ch, -zlib.MAX_WBITS)
                        ad.extend(d)
                        zd = bytes(ad[-32768:]) if len(ad) >= 32768 else bytes(ad)
                    except Exception:
                        pass
            data = bytes(ad)
        sig = b'SQLite format 3\x00'
        parts = []
        car = {}
        pos = 0
        while True:
            idx = data.find(sig, pos)
            if idx == -1: break
            ni = data.find(sig, idx + 16)
            sd = data[idx:ni] if ni != -1 else data[idx:]
            # PID固定名だと同時実行のセッション同士で衝突するため一意名にする
            _fd, tmp = tempfile.mkstemp(suffix='.db', prefix='neo_')
            os.close(_fd)
            try:
                with open(tmp, 'wb') as f:
                    f.write(sd)
                co = sqlite3.connect(tmp)
                cu = co.cursor()
                tabs = [r[0] for r in cu.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                if 'ERParts' in tabs and not parts:
                    cu.execute(
                        'SELECT PartsCode,PartsName,PartsNo,PartsPriceOutTax '
                        'FROM ERParts ORDER BY LineNo')
                    for row in cu.fetchall():
                        parts.append(row)
                if 'Car' in tabs and not car:
                    try:
                        cu.execute(
                            'SELECT CarCode,BodyCode,GradeCode,ColorCode,CarName '
                            'FROM Car LIMIT 1')
                        row = cu.fetchone()
                        if row:
                            car = {'CarCode': row[0], 'BodyCode': row[1],
                                   'GradeCode': row[2], 'ColorCode': row[3],
                                   'CarName': row[4]}
                    except Exception:
                        pass
                co.close()
            except Exception:
                pass
            finally:
                try: os.remove(tmp)
                except Exception: pass
            pos = idx + 16
        return parts, car

    neo_files = sorted(glob.glob(os.path.join(NEO_DIR, '*.neo')))
    print(f'=== PDFGradeIdentifier 検証 (NEOファイル {min(len(neo_files), MAX_FILES)}件) ===')
    print()

    results = []
    for neo_path in neo_files[:MAX_FILES]:
        parts_raw, car = extract_neo_simple(neo_path)
        if not parts_raw or not car:
            continue
        vc = car.get('CarCode', '')
        bc = int(car.get('BodyCode', 0) or 0)
        gc_true = (car.get('GradeCode', '') or '').strip()
        if not vc or not gc_true:
            continue

        try:
            gid = PDFGradeIdentifier(engine, vcode=vc, body_code=bc)
        except Exception as e:
            print(f'  SKIP {vc}: {e}')
            continue

        for row in parts_raw:
            pname = str(row[1] or '').strip()
            price = row[3] or 0
            pno   = str(row[2] or '').strip()
            if pname:
                gid.add_part(pname, price, parts_no=pno)

        r = gid.identify()
        pred_gc   = r['grade_code'] or '?'
        pred_name = r['grade_name'] or '(不明)'
        conf      = r['confidence']
        disc      = r['discriminating_parts']
        true_name = r['grade_map'].get(gc_true, f'grade={gc_true}')
        correct   = (pred_gc == gc_true)
        mark      = '✓' if correct else '✗'

        results.append({
            'mark': mark, 'vc': vc, 'bc': bc,
            'gc_true': gc_true, 'true_name': true_name,
            'pred_gc': pred_gc, 'pred_name': pred_name,
            'conf': conf, 'disc': disc,
        })

    correct_n = sum(1 for r in results if r['mark'] == '✓')
    total_n   = len(results)
    rate      = correct_n / total_n * 100 if total_n else 0
    print(f'正解率: {correct_n}/{total_n} = {rate:.1f}%')
    print()
    print(f"{'判':2} {'車種':5} {'真GC':4} {'真グレード名':20} {'推定GC':5} {'推定名':20} {'確信':5} {'識別':4}")
    print('-' * 70)
    for r in results[:50]:
        print(f"{r['mark']:2} {r['vc']:5} {r['gc_true']:4} {str(r['true_name'])[:20]:20} "
              f"{r['pred_gc']:5} {str(r['pred_name'])[:20]:20} {r['conf']:5}% {r['disc']:4}件")
