# -*- coding: utf-8 -*-
"""
ADDATA 車両逆引きリゾルバ（コグニセブン「車検証から検索」の再現）
=================================================================
入力: 車検証 / 見積PDF から読める値
  - 型式 (model_code)        例 'JF3'
  - 車台番号 (serial_no)      例 'JF3-1471519' または '1471519'
  - 型式指定番号 (desig)      例 '19384'
  - 類別区分番号 (category)   例 '0001'
  - 初度登録年月 (reg_date)   例 '20200500' / 'R2.5' / '令和2年5月'
  - カラーコード (color_code) 例 'NH830M'
  - ヒント: 車名・グレード名・エンジン名・ハイブリッド/4WD・ドア数（PDF 由来）
出力: NEO の AnSvIf Car / CarSearch / CarEVA に書く値一式 + 根拠 + 確信度

解読済みの ADDATA 構造（2026-09-04 実測、実NEO 11件で検証）
  COM/KA06_ALL.DB : XOR 0xFF テキスト。'06'+CarCode(3)+YearCode(2)+型式(左詰)+車台番号 開始/終了
  COM/KA81.DB     : バイナリ索引。ヘッダ8B + 表1(6521×12B: recFrom,recTo,型式指定番号)
                    + 本体(44B×72020)。本体レコード k = [類別区分 u16×16 (k-1枠の末尾32B)] + [CarCode3 Year1 Grade1 Body1 Z/FVA2]
                    ※ 表1 の型式指定番号は 1 エントリ遅れて格納されている（番号 n の範囲は次エントリの recFrom..recTo）
                    ※ 類別区分リストは「直前の 44B 枠の末尾 32B」に入る
  <M>/<CarCode>/<CarCode>05.DB : YearCode → 年式名
  07.DB : YearCode → 車形名(グループ, ドア数, ボディイメージコード)
  08.DB : YearCode+Body+[Z]+FVA → エンジン名
  09.DB : YearCode+Body+[Z]+FVA+Grade → グレード名, グレード番号
  10.DB : 装備バリエーション(EVA)コード → 名称
  26.DB : カラーコード → カラー名, RGB
  COM/KATB010.DB : メーカーコード → 名称 / KATB021.DB : CarCode → 車名
"""
from __future__ import annotations
import os, re, struct, bisect, json, threading, unicodedata
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from typing import Optional

ADDATA_ROOT_CANDIDATES = [r'C:\Addata', r'D:\Addata', r'F:\Addata']


SKILL_CONFIG = os.path.join(os.path.expanduser('~'), '.claude', 'pdf-to-neo.local.json')  # env_check.py が書く PC ごとの設定


def _looks_like_addata(p: str, seconds: float = 3.0) -> bool:
    """<p>\COM があるか。切断されたネットワークドライブを指す設定が残っていると os.path.isdir が返らないので、
    別スレッドで見て指定秒で打ち切る（打ち切ったら「違う」とみなし、次の手段へ進む）"""
    if not p:
        return False
    box = [False]

    def work():
        try:
            box[0] = os.path.isdir(os.path.join(p, 'COM'))
        except OSError:
            box[0] = False
    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(seconds)
    return box[0]


def _addata_from_config() -> str:
    """env_check.py が保存した設定の ADDATA_ROOT。pipeline を直接呼んだとき（skill_env を通らないとき）に効く"""
    try:
        if os.path.isfile(SKILL_CONFIG):
            v = str(json.load(open(SKILL_CONFIG, encoding='utf-8-sig')).get('ADDATA_ROOT') or '')
            if _looks_like_addata(v):
                return v
    except (OSError, ValueError):  # 壊れた JSON・読めない設定は無視して次の手段へ
        pass
    return ''


def _addata_by_scan() -> str:
    """最後の手段: skill_env の自動検出（リポジトリ同梱 → 個人スキル領域の順に探して import）"""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(os.path.dirname(here), '.claude', 'skills', 'pdf-to-neo', 'scripts'),
              os.path.join(os.path.expanduser('~'), '.claude', 'skills', 'pdf-to-neo', 'scripts')):
        if not os.path.isfile(os.path.join(d, 'skill_env.py')):
            continue
        import importlib.util
        try:
            spec = importlib.util.spec_from_file_location('_pdf_to_neo_skill_env', os.path.join(d, 'skill_env.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # 生成器から呼ぶときの走査は必ず短め（env_check で設定するのが本筋。make_neo は子プロセス 3 本ぶん待つため）。
            # 環境変数ではなく引数で渡す: 同じプロセスで同時に呼ばれても互いの設定を壊さない
            v = mod.find_addata(mod.find_cogni(), budget=8.0)
            if _looks_like_addata(v):
                return v
        except Exception:  # noqa: BLE001  自動検出は補助なので、失敗しても最終エラーに任せる
            continue
    return ''


def find_addata_root() -> str:
    """ADDATA の場所: 環境変数 → env_check の設定ファイル → 既定パス → 自動検出。
    PC ごとに置き場所が違うので、pipeline を直接呼ぶ入口（run_case.py 等）でも設定と自動検出に届くようにしてある"""
    env = os.environ.get('ADDATA_ROOT')
    if _looks_like_addata(env or ''):
        return env
    v = _addata_from_config()
    if v:
        return v
    for p in ADDATA_ROOT_CANDIDATES:
        if _looks_like_addata(p):
            return p
    v = _addata_by_scan()
    if v:
        return v
    raise FileNotFoundError(
        'ADDATA が見つかりません。環境変数 ADDATA_ROOT を設定するか、'
        'python .claude/skills/pdf-to-neo/scripts/env_check.py --addata "<ADDATA のパス>" --save を実行してください')


def _xor_text(path: str) -> str:
    with open(path, 'rb') as f:
        b = f.read()
    return bytes(x ^ 0xFF for x in b).decode('cp932', errors='replace')


def to_halfwidth(s: str) -> str:
    """全角英数カナ → 半角（NEO CarName 用）。"""
    out = []
    for ch in s:
        n = unicodedata.normalize('NFKC', ch)
        # カタカナは半角カナへ
        out.append(n)
    txt = ''.join(out)
    # 全角カタカナ → 半角カタカナ
    table = str.maketrans('ァィゥェォャュョッーアイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン゛゜',
                          'ｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜｦﾝﾞﾟ')
    dak = {'ガ':'ｶﾞ','ギ':'ｷﾞ','グ':'ｸﾞ','ゲ':'ｹﾞ','ゴ':'ｺﾞ','ザ':'ｻﾞ','ジ':'ｼﾞ','ズ':'ｽﾞ','ゼ':'ｾﾞ','ゾ':'ｿﾞ',
           'ダ':'ﾀﾞ','ヂ':'ﾁﾞ','ヅ':'ﾂﾞ','デ':'ﾃﾞ','ド':'ﾄﾞ','バ':'ﾊﾞ','ビ':'ﾋﾞ','ブ':'ﾌﾞ','ベ':'ﾍﾞ','ボ':'ﾎﾞ',
           'パ':'ﾊﾟ','ピ':'ﾋﾟ','プ':'ﾌﾟ','ペ':'ﾍﾟ','ポ':'ﾎﾟ','ヴ':'ｳﾞ'}
    txt = ''.join(dak.get(c, c) for c in txt)
    return txt.translate(table)


# ---------------------------------------------------------------- 和暦・日付
ERA = {'令和': 2018, 'R': 2018, '平成': 1988, 'H': 1988, '昭和': 1925, 'S': 1925}


def ym_label(v: int) -> str:
    """KA81 の月シリアル → 'YYYY.MM'（0 = 現行）"""
    return '現行' if not v else f'{1900 + (v - 1) // 12}.{(v - 1) % 12 + 1:02d}'


def parse_reg_date(s: str) -> Optional[tuple[int, int]]:
    """'20200500' / '2020/05' / 'R2.5' / '令和2年5月' / 'Ｈ２９.８' → (2020, 5)"""
    if not s:
        return None
    t = unicodedata.normalize('NFKC', str(s)).strip()
    m = re.match(r'^(\d{4})[/\-年.]?(\d{1,2})', t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r'^(令和|平成|昭和|R|H|S)\s*(\d{1,2})\s*[年.．]\s*(\d{1,2})', t)
    if m:
        return ERA[m.group(1)] + int(m.group(2)), int(m.group(3))
    return None


def parse_year_range(name: str) -> tuple[Optional[tuple[int, int]], Optional[tuple[int, int]]]:
    """05.DB の 'Ｈ２９.８～Ｒ１.９' / 'Ｒ２．１２～' → ((2017,8),(2019,9)) / ((2020,12),None)"""
    t = unicodedata.normalize('NFKC', name).replace('～', '~').replace('〜', '~')
    t = re.sub(r'（.*?）|\(.*?\)', '', t)
    parts = t.split('~')
    def one(p):
        p = p.strip()
        m = re.match(r'^(R|H|S)\s*(\d{1,2})\s*[.．]\s*(\d{1,2})', p)
        if m:
            return ERA[m.group(1)] + int(m.group(2)), int(m.group(3))
        return None
    lo = one(parts[0]) if parts else None
    hi = one(parts[1]) if len(parts) > 1 else None
    return lo, hi


# ---------------------------------------------------------------- データ構造
PIN_OK = '候補を指定どおりに固定'   # hints['candidate'] に一致した印


@dataclass
class Candidate:
    car_code: str
    year_code: str
    body_code: str
    grade_code: str
    fva_code: str
    four_wd: bool
    categories: list[int]
    period: tuple = (0, 0)  # KA81 の生産期間（開始, 終了）月シリアル: (年-1900)*12+月、終了 0 = 現行
    grade_name: str = ''
    grade_no: str = ''
    fva_name: str = ''
    body_name: str = ''
    body_image_code: str = ''
    year_name: str = ''
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


class AddataVehicleResolver:
    def __init__(self, root: Optional[str] = None):
        self.root = root or find_addata_root()
        self._ka06 = None
        self._ka81 = None

    # ------------------------------------------------------------ 共通マスタ
    @lru_cache(maxsize=None)
    def maker_names(self) -> dict[str, str]:
        d = {}
        for l in _xor_text(os.path.join(self.root, 'COM', 'KATB010.DB')).splitlines():
            if l.startswith('02') and len(l) > 3:
                d[l[2]] = l[3:].strip()
        return d

    @lru_cache(maxsize=None)
    def car_names(self) -> dict[str, list[tuple[str, str, str]]]:
        """CarCode → [(body系 '00'/'20'/'30', 車名コード, 車名)]"""
        d: dict[str, list] = {}
        for l in _xor_text(os.path.join(self.root, 'COM', 'KATB021.DB')).splitlines():
            if len(l) >= 13 and l[:3].strip():
                d.setdefault(l[:3], []).append((l[5:7], l[10:13], l[13:].strip()))
        return d

    @lru_cache(maxsize=None)
    def maker_of_car(self) -> dict[str, str]:
        """KATB030.DB '03D01089J97' → CarCode J97 のメーカーコード 'D'（CarCode 先頭文字≠メーカーコード）"""
        d = {}
        for l in _xor_text(os.path.join(self.root, 'COM', 'KATB030.DB')).splitlines():
            if l.startswith('03') and len(l) >= 11:
                d.setdefault(l[8:11].strip(), l[2])
        return d

    @lru_cache(maxsize=None)
    def car_series(self) -> dict[str, str]:
        d = {}
        for l in _xor_text(os.path.join(self.root, 'COM', 'KATB040.DB')).splitlines():
            if l.startswith('04') and len(l) > 5:
                d[l[2:5]] = l[5:].split('KA04')[0].strip()
        return d

    # ------------------------------------------------------------ KA06: 型式+車台番号
    def ka06(self) -> list[tuple[str, str, str, int, int]]:
        if self._ka06 is None:
            rows = []
            for l in _xor_text(os.path.join(self.root, 'COM', 'KA06_ALL.DB')).splitlines():
                if not l.startswith('06'):
                    continue
                rest = l[7:].split()
                if len(rest) >= 3:
                    try:
                        rows.append((l[2:5], l[5:7], rest[0], int(rest[1]), int(rest[2])))
                    except ValueError:
                        pass  # 英字入り車台番号（輸入車等）は現状無視
            self._ka06 = rows
        return self._ka06

    def lookup_by_model_serial(self, model_code: str, serial_no: str) -> list[dict]:
        model = unicodedata.normalize('NFKC', model_code or '').upper().strip()
        model = re.sub(r'^[A-Z0-9]{2,4}-', '', model) if '-' in model and not model.startswith(('LA', 'JF')) else model
        sn = unicodedata.normalize('NFKC', serial_no or '').upper()
        digits = re.sub(r'\D', '', sn.split('-')[-1]) if sn else ''
        out = []
        if not model or not digits:
            return out
        n = int(digits)
        # 'DBA-ZRR80G' → 'ZRR80G' → 'ZRR80'（KA06 は末尾の仕様記号を持たない型式で収録されることがある）
        model = re.sub(r'^[0-9A-Z]{1,4}-', '', model) if re.match(r'^[0-9A-Z]{1,4}-[A-Z]', model) else model
        cands = [model]
        m2 = model
        while len(m2) > 3 and m2[-1].isalpha():
            m2 = m2[:-1]
            cands.append(m2)
        for cand in cands:
            for car, year, m, lo, hi in self.ka06():
                if m == cand and lo <= n <= hi:
                    out.append({'car_code': car, 'year_code': year, 'model': m, 'range': (lo, hi)})
            if out:
                break
        return out

    # ------------------------------------------------------------ KA81: 型式指定+類別
    def ka81(self):
        if self._ka81 is None:
            with open(os.path.join(self.root, 'COM', 'KA81.DB'), 'rb') as f:
                b = f.read()
            n1 = struct.unpack('<I', b[:4])[0]
            t1 = [struct.unpack('<III', b[8 + i * 12:20 + i * 12]) for i in range(n1)]
            base = 8 + n1 * 12 + 32  # = 78292
            self._ka81 = (b, t1, base)
        return self._ka81

    def _ka81_record(self, k: int) -> Optional[dict]:
        b, t1, base = self.ka81()
        o = base + 44 * k
        if o + 44 > len(b) or k < 1:
            return None
        idb = b[o:o + 8]
        prev = b[o - 44 + 12:o]  # 直前枠の末尾 32B = 類別区分リスト
        cats = [v for v in struct.unpack('<16H', prev) if v]
        # 直前枠の [8:12] = 生産期間（開始 u16, 終了 u16）。月シリアル (年-1900)*12+月（1321 = 2010.01、0 = 現行）。
        # コグニ実機 C10_y01.neo（2026-09-06 夜）: 型式指定 11575 × 類別 65 × 初度登録 H22.5 → 年式 01（生産期間 2010.01〜2010.07）。
        # 全 1,285 車種で年式コード順に開始月が単調（前枠読みで違反 0 / 同枠読みで 160）
        x, y = struct.unpack('<HH', b[o - 44 + 8:o - 44 + 12])
        car = idb[0:3].decode('latin1')
        if not re.match(r'^[A-Z][0-9A-Z]{2}$', car):
            return None
        yr = idb[3:4].decode('latin1')
        year_code = '00' if yr == ' ' else f'0{yr}'
        fva2 = idb[6:8].decode('latin1')
        return dict(k=k, car_code=car, year_code=year_code, grade_code=idb[4:5].decode('latin1'),
                    body_code=f'{idb[5]:02d}', four_wd=fva2[0] == 'Z',
                    fva_code=fva2[1] if fva2[0] == 'Z' else fva2[0], categories=cats, period=(x, y))

    def lookup_by_designation(self, desig: str, category: str) -> list[dict]:
        b, t1, base = self.ka81()
        try:
            d = int(re.sub(r'\D', '', str(desig)))
            c = int(re.sub(r'\D', '', str(category)))
        except ValueError:
            return []
        ks = [i for i, t in enumerate(t1) if t[2] == d]
        out = []
        for k in ks:
            if k + 1 >= len(t1):
                continue
            lo, hi, _ = t1[k + 1]  # 番号は 1 エントリ遅れ → 次エントリの範囲
            for i in range(lo, hi + 1):
                r = self._ka81_record(i)
                if r and c in r['categories']:
                    out.append(r)
        return out

    # ------------------------------------------------------------ 車種フォルダ内 DB
    # 固定幅は CP932 の「バイト」単位（全角 1 文字 = 2 バイト）。必ず bytes で切ってから decode する。
    def _vdb_bytes(self, car: str, suffix: str) -> list[bytes]:
        p = os.path.join(self.root, car[0], car, f'{car}{suffix}.DB')
        if not os.path.exists(p):
            return []
        with open(p, 'rb') as f:
            raw = bytes(x ^ 0xFF for x in f.read())
        return [l.rstrip(b'\r') for l in raw.split(b'\n') if l.strip()]

    def _vdb(self, car: str, suffix: str) -> list[str]:
        return [l.decode('cp932', 'replace').rstrip() for l in self._vdb_bytes(car, suffix)]

    @staticmethod
    def _d(b: bytes) -> str:
        return b.decode('cp932', 'replace').strip()

    @staticmethod
    def fva2(four_wd: bool, fva: str) -> str:
        """08/09/01/Katashiki 共通の 2 文字駆動＋エンジン区分: 2WD='A ', 4WD='ZA'"""
        return ('Z' + fva) if four_wd else (fva + ' ')

    def year_names(self, car: str) -> dict[str, str]:
        return {self._d(l[5:7]): self._d(l[7:]) for l in self._vdb_bytes(car, '05')}

    def body_forms(self, car: str, year: str) -> list[dict]:
        """07.DB 固定幅(bytes): [5:7]年式 [7]グループ [8]'0' [9:35]車形名 [35:37]ボディイメージコード"""
        out = []
        for l in self._vdb_bytes(car, '07'):
            if self._d(l[5:7]) != year:
                continue
            group = self._d(l[7:8])
            name = self._d(l[9:35])
            image = self._d(l[35:37])
            nk = unicodedata.normalize('NFKC', name)
            m = re.match(r'^(\d+)\s*(ﾄﾞｱ|ドア)', nk)
            doors = m.group(1) if m else ('3' if ('ﾄﾗｯｸ' in name or 'トラック' in name) else '')
            out.append({'group': group, 'name': name, 'doors': doors, 'body_image_code': image})
        return out

    def engine_name(self, car: str, year: str, body: str, four_wd: bool, fva: str) -> str:
        key = f'{year}{body}{self.fva2(four_wd, fva)}'.encode('cp932')
        for l in self._vdb_bytes(car, '08'):
            if l[5:11] == key:
                return self._d(l[11:])
        return ''

    def grade_info(self, car: str, year: str, body: str, four_wd: bool, fva: str, grade: str) -> tuple[str, str]:
        """09.DB 固定幅(bytes): [5:7]年式 [7:9]ボディ [9:11]駆動+エンジン [11]グレード [12:38]グレード名 [38:40]グレード番号"""
        key = f'{year}{body}{self.fva2(four_wd, fva)}{grade}'.encode('cp932')
        for l in self._vdb_bytes(car, '09'):
            if l[5:12] == key:
                return self._d(l[12:38]), self._d(l[38:40])
        return '', ''

    @lru_cache(maxsize=None)
    def header_db(self, car: str) -> dict:
        """01.DB（平文・車種ヘッダ）: NEO CarName の元となる半角車名レコード。
        レコード: [grade 1][body 1B][year 1][駆動+エンジン 2][00][?]['    '][半角車名 24][型式+グレード名 22][排気量 4][価格適応日 6]
        戻り値: {'title': {...}, 'records': {(year, body, grade, fva2): {...}}}"""
        p = os.path.join(self.root, car[0], car, f'{car}01.DB')
        out = {'title': {}, 'records': {}}
        if not os.path.exists(p):
            return out
        with open(p, 'rb') as f:
            b = f.read()
        m = re.search(rb'A\x00   \x00\x00    (.{24})(.{26})(\d{6})', b, re.S)
        if m:
            out['title'] = {'name': self._d(m.group(1)), 'series': self._d(m.group(2)), 'price_date': m.group(3).decode()}
        for m in re.finditer(rb'([A-Z])([\x01-\x7f])([ 0-9])([A-Z ][A-Z ])\x00.    (.{24})(.{22})(.{4})(\d{6})', b, re.S):
            grade = m.group(1).decode(); body = f'{m.group(2)[0]:02d}'
            yr = m.group(3).decode(); year = '00' if yr == ' ' else f'0{yr}'
            f2 = m.group(4).decode('latin1')
            out['records'][(year, body, grade, f2)] = {
                'name': self._d(m.group(5)), 'model_grade': self._d(m.group(6)),
                'cc': m.group(7).decode('cp932', 'replace'), 'price_date': m.group(8).decode(),
            }
        return out

    @lru_cache(maxsize=None)
    def katashiki(self) -> list[tuple[str, str, str, str, str, str]]:
        """COM.CAB 内 Katashiki.DB: (CarCode, 年式, ボディ, グレード, 駆動+エンジン, 型式)。
        CAB 未展開時は空。展開済みなら scratch/comcab または COM/ から読む。"""
        here = os.path.dirname(os.path.abspath(__file__))
        for p in (os.path.join(self.root, 'COM', 'Katashiki.DB'), os.environ.get('KATASHIKI_DB', ''), os.path.join(here, 'reference', 'Katashiki.DB')):
            if p and os.path.exists(p):
                rows = []
                for l in _xor_text(p).splitlines():
                    if len(l) >= 11 and re.match(r'^[A-Z][0-9A-Z]{2}', l):
                        yr = l[3:5].strip() or '00'
                        rows.append((l[0:3], yr.zfill(2), l[5:7], l[7], l[8:10], l[10:].strip()))
                return rows
        return []

    def model_code_for(self, car: str, year: str, body: str, grade: str, four_wd: bool, fva: str) -> str:
        f2 = self.fva2(four_wd, fva)
        for c, y, b, g, fv, model in self.katashiki():
            if c == car and y == year and b == body and g == grade and fv == f2:
                return model
        return ''

    def options(self, car: str) -> dict[str, str]:
        out = {}
        for l in self._vdb(car, '10'):
            code = l[5]
            name = re.split(r'\s{2,}|\d{3,4}cc', l[7:])[0].strip()
            out[code] = name
        return out

    def engine_cc(self, car: str, fva: str) -> str:
        for l in self._vdb(car, '10'):
            if l[5] == fva:
                m = re.search(r'(\d{3,4})cc', l)
                if m:
                    return m.group(1)
        return ''

    def _color_rows(self, car: str) -> list[dict]:
        """26.DB 固定幅(bytes): [11:23]カラーコード [23]区分 [24:54]カラー名 [54:60]RGB [66:]備考"""
        rows = []
        for l in self._vdb_bytes(car, '26'):
            if not l.startswith(b'26') or len(l) < 60:
                continue
            rows.append({'code': self._d(l[11:23]), 'kind': self._d(l[23:24]), 'name': self._d(l[24:54]),
                         'rgb': self._d(l[54:60]), 'note': self._d(l[66:])})
        return rows

    def color(self, car: str, code: str) -> dict:
        code = unicodedata.normalize('NFKC', code or '').upper().strip()
        for r in self._color_rows(car):
            if r['code'].upper() == code:
                return r
        return {}

    def color_variants(self, car: str) -> list[str]:
        return [r['code'] for r in self._color_rows(car)]

    def form_codes(self, car: str, year: str = '', body: str = '', grade: str = '', fva2: str = '') -> dict:
        """25.DB（平文）: [2B ヘッダ] + 11B レコード×N = [YearCode 2][BodyCode 2][GradeCode 1][駆動+エンジン 2][CarFormCode][FormCode1][FormCode2][FinishCode]
        先頭 7 文字が空白のレコードは全年式・全ボディ共通。実 NEO 9 台で 9/9 一致（例 N-BOX '7252' = 軽/2/5ドア/2、SAI '2142'、ハイゼットトラック '7232'）。
        コグニ DLL の TRC_XXX25 レコード定義（YearCode, BodyCode, GradeCode, FVACode, CarFormCode, FormCode1, FormCode2, FinishCode）に対応。"""
        p = os.path.join(self.root, car[0], car, f'{car}25.DB')
        out = {}
        if not os.path.exists(p):
            return out
        b = open(p, 'rb').read()
        recs = [b[2 + i * 11: 2 + (i + 1) * 11] for i in range((len(b) - 2) // 11)]
        best = None
        for r in recs:
            key = r[:7].decode('latin1'); codes = r[7:11].decode('latin1')
            if not re.match(r'^\d{4}$', codes):
                continue
            score = 0
            ok = True
            for val, seg in ((year, key[0:2]), (body, key[2:4]), (grade, key[4:5]), (fva2, key[5:7])):
                if seg.strip():
                    if val and seg == val:
                        score += 1
                    else:
                        ok = False
            if ok and (best is None or score > best[0]):
                best = (score, codes)
        if best:
            c = best[1]
            out = {'CarFormCode': c[0], 'FormCode1': c[1], 'FormCode2': c[2], 'FinishCode': c[3]}
        return out

    def finish_code(self, car: str, code: str) -> int:
        """66.DB: 'NH731P      001 00       3B02' → 先頭桁 3 = 塗膜区分（1 ソリッド / 2 メタリック / 3 2コートパール / 4 3コートパール / 9 特殊）"""
        code = unicodedata.normalize('NFKC', code or '').upper().strip()
        for l in self._vdb_bytes(car, '66'):
            if self._d(l[0:12]).upper() == code:
                m = re.search(rb'\s(\d)[A-Z]\d{2}', l[12:40])
                if m:
                    return int(m.group(1))
        return 0

    # ------------------------------------------------------------ 総合解決
    def resolve(self, model_code: str = '', serial_no: str = '', desig: str = '', category: str = '',
                reg_date: str = '', color_code: str = '', hints: Optional[dict] = None) -> dict:
        hints = hints or {}
        try:
            cand_limit = int(hints.get('candidate_limit', 12))
        except (TypeError, ValueError):
            cand_limit = 12
        if cand_limit < 0:      # 無制限は 0 だけ。負数は指定ミスなので既定に戻す
            cand_limit = 12
        ev: list[str] = []
        # 1) 型式 + 車台番号 → CarCode/YearCode（生産期間）
        by_serial = self.lookup_by_model_serial(model_code, serial_no)
        if by_serial:
            ev.append(f'KA06: 型式 {model_code} × 車台番号 → {[(r["car_code"], r["year_code"]) for r in by_serial]}')
        # 2) 型式指定 + 類別 → CarCode/Year/Body/Grade/FVA
        by_desig = self.lookup_by_designation(desig, category) if desig and category else []
        if by_desig:
            ev.append(f'KA81: 型式指定 {desig} × 類別 {category} → {len(by_desig)} 候補')
        cands: list[Candidate] = []
        for r in by_desig:
            cands.append(Candidate(r['car_code'], r['year_code'], r['body_code'], r['grade_code'], r['fva_code'], r['four_wd'], r['categories'], period=tuple(r.get('period') or (0, 0))))
        # KA81 が無ければ KA06 の車種×年式に 01.DB のグレード一覧（年式・ボディ・駆動+エンジン・グレード）を展開して候補にする
        # （型式指定・類別が読めない見積でも、品番からの逆引きヒント（grade_codes / year_group）やグレード名ヒントで絞れるように）
        if not cands and by_serial:
            for r in by_serial:
                recs = self.header_db(r['car_code']).get('records') or {}
                added = 0
                for (yr, body, grade, f2) in recs:
                    if yr != r['year_code']:
                        continue
                    cands.append(Candidate(r['car_code'], yr, body, grade, f2[-1:].strip() or f2[:1], f2[:1] == 'Z', [],
                                           reasons=['KA81 未ヒット: 01.DB のグレード一覧から候補']))
                    added += 1
                if not added:
                    cands.append(Candidate(r['car_code'], r['year_code'], '', '', '', False, [], reasons=['KA81 未ヒット: 車種のみ確定']))
        # 3) 採点
        reg = parse_reg_date(reg_date)
        serial_keys = {(r['car_code'], r['year_code']) for r in by_serial}
        serial_cars = {r['car_code'] for r in by_serial}
        for c in cands:
            c.year_name = self.year_names(c.car_code).get(c.year_code, '')
            if (c.car_code, c.year_code) in serial_keys:
                c.score += 5; c.reasons.append('車台番号レンジ一致(CarCode+Year)')
            elif c.car_code in serial_cars:
                c.score += 2; c.reasons.append('車台番号レンジ一致(CarCodeのみ)')
            if reg and c.period and c.period[0]:
                v = (reg[0] - 1900) * 12 + reg[1]
                x, y = c.period
                if x <= v and (not y or v <= y):
                    c.score += 4; c.reasons.append(f'初度登録 {reg} が KA81 生産期間 {ym_label(x)}〜{ym_label(y)} 内')
                else:
                    c.score -= 2; c.reasons.append(f'初度登録 {reg} が KA81 生産期間 {ym_label(x)}〜{ym_label(y)} 外')
            elif reg and c.year_name:
                lo, hi = parse_year_range(c.year_name)
                ok = (lo is None or reg >= lo) and (hi is None or reg <= hi)
                if ok:
                    c.score += 3; c.reasons.append(f'初度登録 {reg} が年式 {c.year_name} 内')
                else:
                    c.score -= 1
            if c.body_code and c.grade_code:
                c.grade_name, c.grade_no = self.grade_info(c.car_code, c.year_code, c.body_code, c.four_wd, c.fva_code, c.grade_code)
                c.fva_name = self.engine_name(c.car_code, c.year_code, c.body_code, c.four_wd, c.fva_code)
                forms = self.body_forms(c.car_code, c.year_code)
                grp = str(int(c.body_code) // 10) if c.body_code.isdigit() else ''
                f = next((x for x in forms if x['group'] == grp), forms[0] if forms else None)
                if f:
                    c.body_name = f['name']; c.body_image_code = f['body_image_code']
            # ヒント採点
            gh = to_halfwidth(unicodedata.normalize('NFKC', hints.get('grade_name', '') or '')).upper().replace(' ', '')
            if gh and c.grade_name:
                gn = to_halfwidth(unicodedata.normalize('NFKC', c.grade_name)).upper().replace(' ', '')
                if gn == gh:
                    c.score += 4; c.reasons.append(f'グレード名一致 {c.grade_name}')
                elif gh in gn or gn in gh:
                    c.score += 2; c.reasons.append(f'グレード名部分一致 {c.grade_name}')
            pin = hints.get('candidate')  # 具体的な候補 1 件に固定する（グレード比較で使う。指定した欄だけ見る）
            if pin:
                want = [(k, str(pin.get(k) or '').strip()) for k in
                        ('car_code', 'year_code', 'body_code', 'grade_code', 'fva_code')]
                ok = all(str(getattr(c, k) or '').strip() == v for k, v in want if v)
                given = any(v for _k, v in want)
                if pin.get('four_wd') is not None:   # 2WD/4WD も候補を分ける鍵
                    ok = ok and bool(pin['four_wd']) == bool(c.four_wd)
                    given = True
                if ok and given:
                    c.score += 50; c.reasons.append(PIN_OK)
                else:
                    c.score -= 50
            gc = hints.get('grade_codes')  # 見積の品番から逆引きしたグレード記号の集合（AddataParts.infer_from_parts）
            if gc:
                if c.grade_code in gc:
                    c.score += 3; c.reasons.append(f'品番の変種行からグレード {c.grade_code} と整合')
                else:
                    c.score -= 2
            yg = hints.get('year_group')  # 同じく年式群（YearCode の下 1 桁）
            if yg and c.year_code and c.year_code.strip().isdigit():
                if c.year_code.strip()[-1] == str(yg):  # 年式群 = YearCode の下 1 桁
                    c.score += 2; c.reasons.append(f'品番の変種行から年式群 {yg} と整合')
            if 'four_wd' in hints:
                if bool(hints['four_wd']) == c.four_wd:
                    c.score += 2; c.reasons.append('駆動(2WD/4WD)一致')
                else:
                    c.score -= 3
            if 'hybrid' in hints and c.body_name:
                is_hv = 'ﾊｲﾌﾞﾘ' in c.body_name or 'ハイブリ' in c.body_name or 'HV' in c.body_name.upper()
                if bool(hints['hybrid']) == is_hv:
                    c.score += 2; c.reasons.append('ハイブリッド区分一致')
            eh = unicodedata.normalize('NFKC', hints.get('engine', '') or '').upper().replace('-', '').replace('型', '')
            if eh and c.fva_name:
                en = unicodedata.normalize('NFKC', c.fva_name).upper().replace('-', '').replace('型', '')
                if eh[:4] and eh[:4] in en:
                    c.score += 2; c.reasons.append(f'エンジン名一致 {c.fva_name}')
        pin = hints.get('candidate')
        if pin and cands:
            # 「固定する」と言われた以上、一致しない候補は捨てる（減点だけだと元の best がそのまま残る）
            keep = [c for c in cands if PIN_OK in (c.reasons or [])]
            if not keep:
                raise ValueError(f'hints.candidate に一致する候補が無い: {pin}')
            cands = keep
        cands.sort(key=lambda c: -c.score)
        best = cands[0] if cands else None
        # 4) 確信度
        if not best:
            conf = 'unknown'
        else:
            top = [c for c in cands if c.score == best.score]
            distinct = {(c.car_code, c.year_code, c.body_code, c.grade_code, c.fva_code, c.four_wd) for c in top}
            expanded = any('01.DB のグレード一覧から候補' in r for r in (best.reasons or []))
            if len(distinct) == 1 and best.grade_code:
                conf = 'confirmed' if by_desig and by_serial else ('medium' if expanded else 'high')
            elif best.grade_code and not expanded:
                conf = 'medium'
            else:
                conf = 'low'
                if expanded and len(distinct) > 1:
                    ev.append(f'KA81 無し: 01.DB のグレード候補 {len(distinct)} 件が同点。先頭 {best.grade_code}/{best.fva_code} を仮採用（vehicle.desig/category か grade_name ヒント、または品番ヒントで確定させる）')
        # 5) NEO Car / CarSearch 値
        car_fields = {}
        if best:
            cc = self.engine_cc(best.car_code, best.fva_code) if best.fva_code else ''
            model = (by_serial[0]['model'] if by_serial else unicodedata.normalize('NFKC', model_code or '').upper())
            hdr = self.header_db(best.car_code)
            rec = hdr['records'].get((best.year_code, best.body_code, best.grade_code, self.fva2(best.four_wd, best.fva_code))) if best.grade_code else None
            if rec:
                # コグニと同じ組み立て: 半角車名 + 型式+グレード名 + 排気量(右詰4桁)
                car_name = f"{rec['name']} {rec['model_grade']} {rec['cc']}"
                cc = rec['cc'].strip() or cc
                ev.append(f"01.DB: 車名レコード一致 → '{car_name}'")
            else:
                names = self.car_names().get(best.car_code, [])
                grp_body = {'10': '00', '20': '20', '30': '30', '40': '40'}.get(best.body_code, '00')
                name = next((n for b, _, n in names if b == grp_body), names[0][2] if names else '')
                hw_name = hdr['title'].get('name') or to_halfwidth(name)
                grade_hw = to_halfwidth(best.grade_name)
                car_name = f'{hw_name} {model} {grade_hw} {cc:>4}'.strip() if best.grade_code else f'{hw_name} {model}'
            main_color = re.split(r'[/／・,]', color_code or '')[0].strip() if color_code else ''
            col = self.color(best.car_code, main_color) if main_color else {}
            reg_t = parse_reg_date(reg_date)
            doors = ''
            for f in self.body_forms(best.car_code, best.year_code):
                if f['body_image_code'] == best.body_image_code and f['doors']:
                    doors = f['doors']; break
            # 車形コード: 25.DB が正（無ければ経験則）
            fc = self.form_codes(best.car_code, best.year_code, best.body_code, best.grade_code, self.fva2(best.four_wd, best.fva_code)) if best.grade_code else self.form_codes(best.car_code)
            try:
                cc_i = int(cc or 0)
            except ValueError:
                cc_i = 0
            is_sedan4 = ('ｾﾀﾞﾝ' in to_halfwidth(best.body_name) or 'セダン' in best.body_name) and doors == '4'
            car_form = fc.get('CarFormCode') or ('7' if 0 < cc_i <= 660 else ('2' if is_sedan4 else '6'))
            form1 = fc.get('FormCode1') or ('1' if is_sedan4 else '2')
            if fc:
                doors = fc.get('FormCode2') or doors
                ev.append(f"25.DB: 車形コード {fc['CarFormCode']}{fc['FormCode1']}{fc['FormCode2']}{fc['FinishCode']}")
            maker = self.maker_of_car().get(best.car_code, '')
            car_fields = {
                'MakerCode': maker,
                'MakerName': self.maker_names().get(maker, ''),
                'CarCode': best.car_code, 'YearCode': best.year_code, 'BodyCode': best.body_code,
                'GradeCode': best.grade_code, 'FVACode': (('Z' + best.fva_code) if best.four_wd else best.fva_code), 'FVAName': best.fva_name,  # 4WD は 'Z'+区分（コグニ生成 NEO: ハイゼット S510P 4WD → 'ZA'）
                'CarName': car_name, 'CarNameByUser': car_name + '\u3000', 'FVANameByUser': best.fva_name,
                'BodyImageCode': best.body_image_code, 'LBaseCode': '00', 'SBaseCode': best.body_code,
                'CarFormCode': car_form, 'FormCode1': form1, 'FormCode2': doors or '5', 'FinishCode': fc.get('FinishCode', '2'),
                'ColorCodeFlag': 1 if col else 0, 'ColorCode': col.get('code', color_code or ''),
                'ColorName': col.get('name', ''), 'ColorRGB1': col.get('rgb', ''),
                'ps_CarMouldNo': str(desig or ''), 'ps_CarKindNo': str(category or '').zfill(4) if category else '',
                'ps_CarRegDate': f'{reg_t[0]:04d}{reg_t[1]:02d}00' if reg_t else '',
                'ps_CarSerialNoHead': (serial_no or '').split('-')[0], 'ps_CarSerialNoTail': (serial_no or '').split('-')[-1] if '-' in (serial_no or '') else '',
                'ps_CarSerialNo': serial_no or '', 'ps_YearName': best.year_name, 'ps_YearSearchFlag': 1 if by_serial else 0,
                'SearchMethod': 3,
                'four_wd': best.four_wd, 'grade_name': best.grade_name, 'grade_no': best.grade_no, 'body_name': best.body_name,
                'series': self.car_series().get(best.car_code, ''), 'engine_cc': cc,
                'options_available': self.options(best.car_code),
                'color_variants': self.color_variants(best.car_code)[:40],
            }
        return {
            'confidence': conf,
            'best': asdict(best) if best else None,
            'neo_car': car_fields,
            # 既定では 12 件で切る（表示用）。全部欲しいときは hints['candidate_limit'] = 0
            'candidates': [asdict(c) for c in (cands if cand_limit <= 0 else cands[:cand_limit])],
            'candidates_total': len(cands),
            'evidence': ev,
            'inputs': dict(model_code=model_code, serial_no=serial_no, desig=desig, category=category, reg_date=reg_date, color_code=color_code, hints=hints),
        }


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    r = AddataVehicleResolver()
    demo = dict(model_code='JF3', serial_no='JF3-1471519', desig='19384', category='0001', reg_date='20200500', color_code='NH830M')
    print(json.dumps(r.resolve(**demo), ensure_ascii=False, indent=1)[:3000])
