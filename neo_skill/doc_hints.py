# -*- coding: utf-8 -*-
"""doc_hints.py — 添付した書類の OCR 結果を、2 つの生成経路が受け取れる形に写す（2026-09-14）。

  車検証: app.analyze_vehicle_registration の dict（customer_name / owner_name / car_reg_* / car_serial_no / car_model /
          car_model_designation / car_category_number / car_reg_date(YYYYMM00) / term_date(YYYYMMDD) / kilometer / color_code …）
  事故・保険の書類（速報報告書・事故受付票・立会依頼書など）: app.analyze_insurance_document の dict（INSURANCE_DOC_KEYS）

  - pdf-to-neo スキル経路: reading の header に補う hint（vehicle / customer / insurance）。reader._apply_hint は
    「見積書に印字が無い項目にだけ補う」ので、見積書の印字が常に優先される
  - ベタ打ち（旧経路 run_pdf_to_neo_pipeline）: vehicle_info（車検証 OCR の dict ＋ 書類から分かった色・走行距離）
  - サイドバー「事故・保険情報」: 書類から読めた項目で入力欄を埋める（利用者が直せる。生成時はサイドバーの値が使われる）
Streamlit・Gemini に依存しない（tests/reg_hints.py）。
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

import datetime as _dt
import re
import unicodedata
from typing import Optional

# 事故・保険の書類の OCR が返すキー（値はすべて文字列。無いものは ''）
INSURANCE_DOC_KEYS = (
    'company', 'branch', 'staff', 'accept_no', 'report_date', 'accident_date', 'accident_place', 'contractor', 'counterpart',
    'policy_no', 'coverage', 'market_value',
    'reg_no', 'car_name', 'model', 'grade', 'first_reg', 'serial_no', 'reg_date', 'engine_model', 'desig', 'category',
    'term_date', 'owner', 'user', 'mileage', 'color_code', 'color_name', 'equipment', 'confidence',
)
INSURANCE_DOC_SCHEMA = {'type': 'object', 'properties': {k: {'type': 'string'} for k in INSURANCE_DOC_KEYS}}

INSURANCE_DOC_PROMPT = """<task_execution>
<task>事故・保険の書類の読み取り（AI-OCR）</task>
<description>
入力は損害保険会社・共済からの「速報報告書」「事故受付票」「立会依頼書」「連絡票」など、事故と車両の情報が印字された書類（写真・スキャン・画面のスクリーンショット）です。
印字されている文字をそのまま抽出し、下の JSON で返してください。推測で補完しない・無い項目は "" にする。
</description>

読み取り対象（書類での呼び方の例）:
- company: 依頼会社名・保険会社名・共済名（例: ○○損害保険、○○共済）
- branch: 支店・部署・サービスセンター名（例: ○○損調サービスセンター）
- staff: 担当者名（保険会社側の担当・アジャスター。「様」は付けない）
- accept_no: 事故番号・受付番号・事故受付番号（ハイフン込みで印字どおり）
- report_date: 速報日 → YYYYMMDD
- accident_date: 事故日・事故発生日 → YYYYMMDD
- accident_place: 事故場所
- contractor: 契約者名・被保険者名
- counterpart: 相手者名・相手方
- policy_no: 証券番号
- coverage: 担保種目（対物・車両 など。金額や免責があれば「対物 無制限 免責0」のように続ける）
- market_value: 時価額（数字だけ。カンマ・円は除く）
- reg_no: 登録番号（「北九州 539 な 1031」のように 地名 分類番号 かな 一連番号 を半角スペース区切り）
- car_name: 車名（メーカー名や型式が並んでいれば印字どおり）
- model: 型式（例: 5BA-KSP210）
- grade: グレード
- first_reg: 初度登録（年月。和暦のままでよい。例: 令和3年11月）
- serial_no: 車台番号（例: KSP210-0057662）
- reg_date: 登録日 → YYYYMMDD
- engine_model: 原動機型式
- desig: 型式指定番号（5 桁）
- category: 類別区分番号（4 桁）
- term_date: 有効期限・車検満了日 → YYYYMMDD
- owner: 所有者
- user: 使用者（「同上」ならそのまま「同上」）
- mileage: 走行距離（km の数字だけ）
- color_code: カラーNo・カラーコード（例: 3T3）
- color_name: 色名
- equipment: 主要装備
- confidence: 読み取り信頼度 0.0〜1.0

重要ルール:
- 印字されている文字を一言一句そのまま抽出する。読めない項目は "" にする
- 日付は YYYYMMDD の 8 桁（和暦→西暦: 令和N年 = 2018+N 年、平成N年 = 1988+N 年）。年月だけの項目（first_reg）は印字どおりでよい
- 数字は半角にする（登録番号の分類番号・一連番号も半角）
- 見積書（部品や工賃の明細）が写っているだけで事故・車両の情報が無い場合は、すべて "" にする
</task_execution>"""


def _nfkc(s) -> str:
    return unicodedata.normalize('NFKC', '' if s is None else str(s)).strip()   # 数値の 0 は '0'（Codex 69）


_MAX_VALUE_LEN = 120   # OCR の 1 項目の上限（住所でもこの程度。長い文はプロンプトに命令文を紛れ込ませる余地になる。Codex hunt C1）


def _clean(s) -> str:
    """OCR の値を「データ」として安全な形に: 制御文字・改行を空白に、連続空白を 1 つに、長さを抑える"""
    t = re.sub(r'[\x00-\x1f\x7f\u2028\u2029]+', ' ', '' if s is None else str(s))   # 数値の 0 は '0'（Codex 69）
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:_MAX_VALUE_LEN]


def _get(d: Optional[dict], key: str) -> str:
    if not isinstance(d, dict):
        return ''
    v = d.get(key)
    if v is None or isinstance(v, (dict, list, tuple, set)):
        return ''
    if isinstance(v, bool):
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return _clean(v)


def kana_hira(s) -> str:
    """登録番号のかな: 半角カナ・全角カタカナをひらがなに（実機 NEO 108 本はすべて全角ひらがな。Codex hunt B5）"""
    t = _nfkc(s)
    return ''.join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in t)


def parse_km(s) -> str:
    """走行距離を km の整数（文字列）に: '15,345km' → '15345'、'1.5万km' → '15000'、'1万5000km' → '15000'、15345 → '15345'。
    読めなければ ''（Codex hunt B4: 数字だけ拾うと '1.5万km' が 15 になる）"""
    t = re.sub(r'^\D+', '', _nfkc(s).replace(',', '').replace(' ', ''))   # '約 2.3 万 km' → '2.3万km'
    if not t:
        return ''
    m = re.match(r'^(\d+(?:\.\d+)?)万(\d*)', t)
    if m:
        return str(int(round(float(m.group(1)) * 10000 + (int(m.group(2)) if m.group(2) else 0))))
    m = re.match(r'^(\d+(?:\.\d+)?)', t)
    return str(int(round(float(m.group(1))))) if m else ''


def _fold_fullwidth_ascii(s: str) -> str:
    """全角の英数・記号（U+FF01〜FF5E）を半角に、全角空白を半角空白に。それ以外（漢字・かな・半角カナ）は触らない"""
    return ''.join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else (' ' if c == '\u3000' else c) for c in s)


def split_address(pref='', muni='', other='') -> tuple:
    """住所を生成器（vendor estimate_to_neo）と同じ規則で 都道府県 / 市区郡 / 以降 に分ける。
    実機 NEO 51 件の Municipality は '〜市' まで（政令市の区は AddressOther1 側。'市…区' は 0 件。2026-09-15 集計）。
    車検証 OCR は '北九州市小倉北区' を municipality に返すので、書く前にここで揃える（Codex hunt B2）"""
    # 書く値は元の字のまま（空白だけ詰める。実機 307 本の住所欄に空白は 1 件も無い）。以前は NFKC した値を書いていたため、
    # 半角カナの建物名が全角になってバイト数が倍になり、30 バイトの欄で部屋番号が黙って消えていた（バグハント 3 回目 L6）。
    # 実機も全角数字の住所がそのまま入っている（68 本中 11 本）。区切りの判定（都道府県・市・区 は漢字）は元の字でも同じ
    # 全角の英数・記号（Ａ〜Ｚ・０〜９・－ など U+FF01〜FF5E）と全角空白だけは半角に畳む（実機も 68 本中 57 本は半角数字。全角のまま
    # だと 2 バイトずつ食って 30 バイトの欄で部屋番号が欠ける。レビュー 2026-09-15）。半角カナは畳まない（上の理由）
    pref_s, muni_s, other_s = (re.sub(r'\s+', '', _fold_fullwidth_ascii('' if x is None else str(x))) for x in (pref, muni, other))
    if muni_s and (pref_s or not re.match(r'^.{2,3}?[都道府県]', _nfkc(muni_s))):
        # 市区郡が構造化されて来ている（車検証 OCR・Step 4 の vehicle_info）: 名前の中の 市・郡（四日市市・余市郡余市町）で切らず、
        # 政令市の区（'北九州市小倉北区'）だけを以降側へ移す（Codex 75）
        m = re.match(r'^(.+?市)(.+区)$', muni_s)
        if m:
            return pref_s, m.group(1), m.group(2) + other_s
        return pref_s, muni_s, other_s
    addr = pref_s + muni_s + other_s
    if not addr:
        return ('', '', '')
    m = re.match(r'^(.{2,3}?[都道府県])(.*)$', addr)
    pref_, rest = (m.group(1), m.group(2)) if m else ('', addr)
    # 1 本の文字列から分けるとき（生成器と同じ規則。名前の中の 市・郡 で切れる癖も同じ ＝ 印字と揃える）
    m = re.match(r'^((?:.{1,8}?(?:市|区|郡|町|村))+?)(.*)$', rest)
    muni_, other_ = (m.group(1), m.group(2)) if m else ('', rest)
    # 以降が無い ＝ 住所が市区郡（町・村を含む）までしかない。'福岡県北九州市'・'福岡県志免町' はそのまま Municipality に残す
    # （Codex 69/74。生成器は同じ場合に市区郡を以降側へ落とす癖があるが、実機は Municipality に入るのでこちらが正）。
    # 市区郡が 30 バイトを超えるときだけ分けない
    if len(muni_.encode('cp932', 'replace')) > 30:
        muni_, other_ = '', rest
    return pref_, muni_, other_


def strip_emission_prefix(model: str) -> str:
    """型式から排ガス規制記号を外す: '5BA-KSP210' → 'KSP210'、'6AA-AYH30W' → 'AYH30W'、'DBA-ZRR80G' → 'ZRR80G'。
    記号の無い 'KSP210' はそのまま。全角・全角ハイフンは半角に揃える（生成器の resolver は KA06 の型式と突き合わせる）"""
    s = _nfkc(model).upper().replace('－', '-').replace('‐', '-').replace('—', '-')
    m = re.match(r'^([0-9A-Z]{2,3})-([0-9A-Z][0-9A-Z-]*)$', s)
    if m and re.search(r'[A-Z]', m.group(2)) and re.search(r'\d', m.group(2)):
        return m.group(2)
    return s


def date8(s, allow_yy: bool = False) -> str:
    """いろいろな書き方の日付を YYYYMMDD に。読めなければ ''。
    受ける形: 20251026 / 2025-10-26 / 2025/10/26 / 2025.10.26 / 令和7年10月26日 / R7.10.26 / 令和7年10月（日なし → 00）"""
    t = _nfkc(s).replace(' ', '')
    if not t:
        return ''
    def _valid(y, mo, d):
        # 暦どおりか（20251340・2 月 31 日のような OCR の誤りを NEO に書かない。Codex 57）。日 0 は年月だけ
        if not (1900 <= y <= 2100 and 1 <= mo <= 12 and 0 <= d <= 31):
            return ''
        if d:
            try:
                _dt.date(y, mo, d)
            except ValueError:
                return ''
        return f'{y:04d}{mo:02d}{d:02d}'
    m = re.match(r'^(\d{4})(\d{2})(\d{2})$', t)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r'^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$', t)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r'^(\d{4})[-/.年](\d{1,2})月?$', t)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)), 0)
    m = re.match(r'^(\d{2})[-/.](\d{1,2})[-/.](\d{1,2})$', t)   # '25.10.26'（保険書類の 2 桁年は 2000 年代。1 桁年は元号か分からないので受けない。Codex hunt B3）
    if m and allow_yy:   # 事故日など保険書類の日付だけ。車検証の初度登録は元号抜けの '25.10.26'（平成 25 年）と区別できないので受けない（Codex 74）
        return _valid(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r'^(令和|平成|昭和|R|H|S)\s*(\d{1,2}|元)[.年/](\d{1,2})(?:[.月/](\d{1,2})?)?日?$', t)
    if m:
        era = m.group(1)[0]
        y = 1 if m.group(2) == '元' else int(m.group(2))
        base = {'令': 2018, 'R': 2018, '平': 1988, 'H': 1988, '昭': 1925, 'S': 1925}[era]
        mo = int(m.group(3))
        d = int(m.group(4)) if m.group(4) else 0
        return _valid(base + y, mo, d)
    return ''


def date8_full(s, allow_yy: bool = False) -> str:
    """日まで要る日付（有効期限・事故日など）。年月だけ（日 00）は '' にする（Codex 59）"""
    d = date8(s, allow_yy)
    return d if (len(d) == 8 and d[6:8] != '00') else ''


def reg_date_wareki(s) -> str:
    """初度登録年月を生成器の reading が期待する印字風（'R3.11' / 'H27.5'）に。
    受ける形: 車検証 OCR の 'YYYYMM00' / '2021-11' / '令和3年11月' / 'R3.11'（そのまま）。読めなければ ''"""
    t = _nfkc(s).replace(' ', '')
    if not t:
        return ''
    m = re.match(r'^([RHS])(\d{1,2})\.(\d{1,2})$', t.upper())
    if m:
        return f'{m.group(1)}{int(m.group(2))}.{int(m.group(3))}'
    d8 = date8(t)
    if len(d8) != 8:
        return ''
    y, mo = int(d8[:4]), int(d8[4:6])
    if not (1 <= mo <= 12):
        return ''
    if y >= 2019 and not (y == 2019 and mo < 5):
        return f'R{y - 2018}.{mo}'
    if y >= 1989:
        return f'H{y - 1988}.{mo}'
    return f'S{y - 1925}.{mo}'


# 一連番号に混ざるハイフン（'-'・U+2010・U+2011・U+2015・長音）、「・」、小数点、空白。'12-34' → '1234'、'・・12' → '12'
_SERIAL_JUNK = re.compile(r'[\s\-‐‑―ー・･.]')
# 地名 分類番号 かな 一連番号（一連番号は数字の前後に「・」やハイフンが混ざっていてもよい。掃除は _serial_clean）。
# 分類番号は 3 桁の数字のほか、下 2 桁にアルファベットが入る形（30A・3AC。2018 年〜の希望番号）も受ける（Codex 65）。
# ※ 生成器（vendor estimate_to_neo）の登録番号の正規表現は数字 2〜3 桁しか受けないので、スキル経路では
#    アルファベット入りの分類番号は空欄になる（files 側の残課題。ベタ打ち側は app._reg_no_part がそのまま書く）
_REG_SPLIT = re.compile(r'^\s*(\S+?)\s*(\d[0-9A-Z]{0,2})\s*([ぁ-んア-ン])\s*([-‐‑―ー・･.\s\d]+?)\s*$')   # 分類番号は旧式の 1 桁も（vendor の REG_NO_RE と同じ）


def _serial_clean(s) -> str:
    """一連番号を実機 NEO と同じ半角数字だけに（全角→半角、ハイフン・「・」・空白を外す）"""
    return _SERIAL_JUNK.sub('', _nfkc(s or ''))


def _split_reg(whole: str) -> tuple:
    """'北九州 539 な 10-31' / '北九州539な・・12' → ('北九州', '539', 'な', '1031' / '12')。分けられなければ ('', '', '', '')"""
    m = _REG_SPLIT.match(_nfkc(whole or ''))
    if not m:
        return ('', '', '', '')
    ser = _serial_clean(m.group(4))
    return (m.group(1), m.group(2), kana_hira(m.group(3)), ser) if 1 <= len(ser) <= 4 else ('', '', '', '')   # 一連番号は 4 桁まで


def reg_no_text(vd: Optional[dict], doc: Optional[dict] = None) -> str:
    """登録番号を '北九州 539 な 1031' の形に（生成器は 地名 分類 かな 一連 を正規表現で分ける）。数字は半角。
    一連番号はベタ打ち側（app._reg_no_part）と同じ規則で掃除する（Codex 64: 掃除が片側だけだと U+2011 のハイフンや
    '・・12' が生成器の正規表現に合わず、スキル経路の NEO だけ登録番号が空になる）"""
    dep, div = (_nfkc(_get(vd, k)).strip() for k in ('car_reg_department', 'car_reg_division'))
    biz = kana_hira(_get(vd, 'car_reg_business'))
    ser = _serial_clean(_get(vd, 'car_reg_serial'))
    if dep and div and biz and ser:
        return f'{dep} {div} {biz} {ser}'
    whole = _nfkc(_get(vd, 'registration_number')) or _nfkc(_get(doc, 'reg_no'))
    parts = _split_reg(whole)
    if all(parts):
        return ' '.join(parts)
    return whole


def parse_reg_no(text: str) -> tuple:
    """'北九州 539 な 1031' → ('北九州', '539', 'な', '1031')。分けられなければ ('', '', '', '')"""
    return _split_reg(text)


def address_text(vd: Optional[dict]) -> str:
    return ''.join(_get(vd, k) for k in ('prefecture', 'municipality', 'address_other')).strip()


def _name(vd: Optional[dict], doc: Optional[dict]) -> tuple:
    """(使用者=顧客名, 所有者)。使用者が '同上' / '***' / 空なら所有者を顧客名にする"""
    user = _get(vd, 'customer_name') or _get(doc, 'user')
    owner = _get(vd, 'owner_name') or _get(doc, 'owner')
    if user in ('', '同上', '***', '＊＊＊') or set(user) <= set('*＊'):
        user = owner
    return user, owner


def vehicle_hint(vd: Optional[dict], doc: Optional[dict] = None) -> dict:
    """reading.vehicle に補う値（resolver が車種を決める材料）。書類（速報）は車検証の無い項目だけ補う"""
    model = _get(vd, 'car_model') or _get(vd, 'model_code') or _get(doc, 'model')
    desig = re.sub(r'\D', '', _nfkc(_get(vd, 'car_model_designation') or _get(doc, 'desig')))
    cat = re.sub(r'\D', '', _nfkc(_get(vd, 'car_category_number') or _get(doc, 'category')))
    if len(desig) > 5 or len(cat) > 4:   # 桁あふれ（'12345-0002' のような書き方）は写さない（要確認は resolver が出す。バグハント I12）
        desig, cat = (desig if len(desig) <= 5 else ''), (cat if len(cat) <= 4 else '')
    out = {
        'model_code': strip_emission_prefix(model) if model else '',
        'serial_no': _nfkc(_get(vd, 'car_serial_no') or _get(doc, 'serial_no')).upper(),
        'desig': desig.zfill(5) if desig else '',   # 型式指定番号は 5 桁（Codex 70）
        'category': cat.zfill(4) if cat else '',
        'reg_date': reg_date_wareki(_get(vd, 'car_reg_date')) or reg_date_wareki(_get(doc, 'first_reg')),   # 解析後の値で書類に落とす（I1）
        'color_code': _nfkc(_get(vd, 'color_code') or _get(doc, 'color_code')).upper(),
    }
    return {k: v for k, v in out.items() if v}


def postal_text(v) -> str:
    """郵便番号を 'NNN-NNNN' に（〒・空白・全角を除く。7 桁にならなければ ''。推測値や壊れた値を NEO に入れない。バグハント I7）"""
    t = re.sub(r'[〒\s\-‐－ー]', '', _nfkc(v))
    m = re.fullmatch(r'(\d{3})(\d{4})', t)
    return f'{m.group(1)}-{m.group(2)}' if m else ''


def customer_hint(vd: Optional[dict], doc: Optional[dict] = None) -> dict:
    """reading.customer に補う値（NEO の顧客欄: 名前・登録番号・住所・有効期限・所有者・走行距離）"""
    user, owner = _name(vd, doc)
    km = parse_km(_get(vd, 'kilometer'))
    if km.lstrip('0') == '':   # 車検証 OCR の kilometer は読めないと 0（'0' は真なので or では落ちない）: 書類の走行距離を使う（バグハント I1）
        km = parse_km(_get(doc, 'mileage'))
    pref, muni, other = split_address(_get(vd, 'prefecture'), _get(vd, 'municipality'), _get(vd, 'address_other'))
    out = {
        'name': user,
        'owner': owner,
        # NEO の所有者欄・使用者欄に入るのは owner_name / user_name（estimate_schema）。使用者と所有者が違うときだけ渡す
        # （同じなら生成器の既定: 所有者=顧客名、使用者='同上'）
        'owner_name': owner if (owner and user and owner != user) else '',
        'user_name': user if (owner and user and owner != user) else '',
        'reg_no': reg_no_text(vd, doc),
        'address': address_text(vd),
        # 車検証の構造化住所（生成器は 1 本の住所を正規表現で分けるので「四日市市」を切り違える。あればそのまま使う。I2）
        'prefecture': pref, 'municipality': muni, 'address_other': other,
        'postal': postal_text(_get(vd, 'postal_no')),
        'term_date': date8_full(_get(vd, 'term_date')) or date8_full(_get(doc, 'term_date')),
        'kilometer': km.lstrip('0') or ('0' if km else ''),
    }
    return {k: v for k, v in out.items() if v}


def insurance_hint_from_doc(doc: Optional[dict]) -> dict:
    """reading.insurance に補う値（estimate_schema の insurance キー）。保険会社・共済名は agency（Insurance.AgencyName）にも入れる
    （NEO に保険会社の欄は無く、コグニ運用ではここに保険会社名を入れている。company は記録用）"""
    company = _get(doc, 'company')
    out = {
        'company': company,
        'agency': company,
        'policy_no': _nfkc(_get(doc, 'policy_no')),
        'contractor': _get(doc, 'contractor'),
        'accident_date': date8_full(_get(doc, 'accident_date'), allow_yy=True),   # 保険書類の '25.10.26' は 2025 年（Codex 74）
        'accept_no': _nfkc(_get(doc, 'accept_no')),
        'adjuster': _get(doc, 'staff'),
        'adjuster_post': _get(doc, 'branch'),
    }
    return {k: v for k, v in out.items() if v}


def sidebar_insurance_from_doc(doc: Optional[dict]) -> dict:
    """サイドバー「事故・保険情報」の session_state キーに写す（読めた項目だけ）"""
    h = insurance_hint_from_doc(doc)
    m = {'accept_no': h.get('accept_no'), 'accident_date': h.get('accident_date'), 'policy_no': h.get('policy_no'),
         'contractor_name': h.get('contractor'), 'agency_name': h.get('agency'), 'adjuster_name': h.get('adjuster'),
         'adjuster_post': h.get('adjuster_post')}
    return {k: v for k, v in m.items() if v}


_PLACEHOLDERS = ('', '同上', '***', '＊＊＊')


def _with_user_name(out: dict) -> dict:
    """使用者欄（Customer.UserName）を決めて返す（実機 307 本: '同上' 270・空 25・名前 12。Codex hunt B7）。
    使用者 = 車検証の使用者（'_raw_user'。同上に置き換える前の値）、それが穴なら customer_name（書類から足した使用者）。
    穴・所有者と同じなら '同上'、違えば使用者名。書類から名前を足した後に呼ぶ（先に決めると書類の使用者が落ちる。Codex 69）"""
    if not out:
        return out
    raw = str(out.pop('_raw_user', '') or '').strip()
    u = raw if (raw not in _PLACEHOLDERS and not set(raw) <= set('*＊')) else str(out.get('customer_name') or '').strip()
    o = str(out.get('owner_name') or '').strip()
    out['user_name'] = '同上' if (u in _PLACEHOLDERS or set(u) <= set('*＊') or u == o) else u
    return out


def vehicle_info_for_legacy(vd: Optional[dict], doc: Optional[dict] = None) -> dict:
    """ベタ打ち（旧経路）の vehicle_info。車検証 OCR の dict をそのまま使い、無い項目を書類から補う。
    車検証が無く書類だけのときは、書類の車両欄から同じ形を組み立てる"""
    out = {k: v for k, v in (vd or {}).items() if not str(k).startswith('_')} if isinstance(vd, dict) else {}
    # 登録番号は実機 NEO と同じ半角数字・ハイフン無しに（車検証 OCR は全角数字で返すことがある。実機 108 本の集計 2026-09-14）
    for k in ('car_reg_department', 'car_reg_division', 'car_reg_business', 'car_reg_serial'):
        if out.get(k):
            out[k] = str(out[k]).strip()
    for k in ('car_reg_division', 'car_reg_serial'):   # 数字の区画だけ半角に（地名は幅を変えない）
        if out.get(k):
            out[k] = _nfkc(out[k])
    if out.get('car_reg_serial'):
        out['car_reg_serial'] = _serial_clean(out['car_reg_serial'])
    if out.get('car_reg_business'):
        out['car_reg_business'] = kana_hira(out['car_reg_business'])   # かなはひらがな（Codex hunt B5）
    # 類別区分番号は 4 桁・型式指定番号は 5 桁（実機 299 本すべて。車検証 OCR が '2' と返しても '0002'。Codex hunt B8）。
    # 桁を超えた値（'12345-0002' のように型式指定と類別を続けて読んだ など）は入れない。
    # 5 桁の欄に 9 桁を入れると、どこで切れるか分からないまま NEO に載る（2026-09-21 バグハント。hint 側と同じ扱いに揃えた）
    for k, w in (('car_category_number', 4), ('car_model_designation', 5)):
        if out.get(k):
            d = re.sub(r'\D', '', _nfkc(out[k]))
            out[k] = d.zfill(w) if (d and len(d) <= w) else ''
    # 住所は生成器と同じ規則で 都道府県 / 市区郡 / 以降（'北九州市小倉北区' → 市区郡 '北九州市'、以降 '小倉北区…'。Codex hunt B2）
    if any(str(out.get(k) or '').strip() for k in ('prefecture', 'municipality', 'address_other')):
        out['prefecture'], out['municipality'], out['address_other'] = split_address(
            out.get('prefecture'), out.get('municipality'), out.get('address_other'))
    if out:
        out['_raw_user'] = str(out.get('customer_name') or '').strip()   # 車検証の使用者（同上に置き換える前）。_with_user_name で外す
    # 旧経路は customer_name をそのまま顧客名（Customer.Name1）に書く。使用者が「同上」「***」なら所有者を顧客名にする
    # （e2e で Name1 が「同上」になった 2026-09-14）
    if out and str(out.get('customer_name') or '').strip() in ('', '同上', '***', '＊＊＊') or (out and set(str(out.get('customer_name') or '')) <= set('*＊')):
        if str(out.get('owner_name') or '').strip():
            out['customer_name'] = str(out['owner_name']).strip()
        else:
            # 所有者も読めなかった: 「同上」「***」を顧客名（Customer.Name1）に書かない。
            # 空にして人に入れてもらう（車検証の「同上」は所有者と同じという印で、名前ではない。2026-09-21 バグハント）
            out['customer_name'] = ''
    if not isinstance(doc, dict) or not doc:
        return _with_user_name(out)

    def fill(key, val):
        val = val if isinstance(val, str) else str(val or '')
        if val and not str(out.get(key) or '').strip():
            out[key] = val

    user, owner = _name(vd, doc)
    if str(out.get('customer_name') or '').strip() in ('同上', '***', '＊＊＊') or set(str(out.get('customer_name') or '').strip()) <= set('*＊'):
        out['customer_name'] = ''   # 穴（同上・***）は書類の名前で埋める（車検証の所有者が読めず書類にあるとき。バグハント I6）
    fill('customer_name', user)
    fill('owner_name', owner)
    dep, div, biz, ser = parse_reg_no(reg_no_text(vd, doc))
    fill('car_reg_department', dep); fill('car_reg_division', div); fill('car_reg_business', biz); fill('car_reg_serial', ser)
    fill('car_serial_no', _nfkc(_get(doc, 'serial_no')).upper())
    # 車検証の「車名」はメーカー名（トヨタ）だけのことが多い。書類に車名（ヤリス KSP210 G 1000）があればそちらを使う
    cur_name = str(out.get('car_name') or '').strip()
    if _get(doc, 'car_name') and (not cur_name or (len(cur_name) <= 5 and not re.search(r'\d', cur_name))):
        out['car_name'] = _get(doc, 'car_name')
    fill('car_model', _nfkc(_get(doc, 'model')).upper())
    # 型式指定番号は 5 桁・類別区分番号は 4 桁。書類から補うときも、桁を超えた値（'12345-0002' を続けて読んだ 等）は入れない
    # （車検証側だけ見ていて、書類からの補完で混じり直していた。Codex 指摘 2026-09-21）
    _desig = re.sub(r'\D', '', _nfkc(_get(doc, 'desig')))
    fill('car_model_designation', _desig.zfill(5) if (_desig and len(_desig) <= 5) else '')
    cat = re.sub(r'\D', '', _nfkc(_get(doc, 'category')))
    fill('car_category_number', cat.zfill(4) if (cat and len(cat) <= 4) else '')
    fill('engine_model', _get(doc, 'engine_model'))
    fill('color_code', _nfkc(_get(doc, 'color_code')).upper())
    fill('body_color', _get(doc, 'color_name'))
    km = parse_km(_get(doc, 'mileage'))
    if km and not out.get('kilometer'):
        out['kilometer'] = int(km)
    fill('term_date', date8_full(_get(doc, 'term_date')))
    first = date8(_get(doc, 'first_reg'))
    fill('car_reg_date', first[:6] + '00' if len(first) == 8 else '')
    return _with_user_name(out)


def summary(vd: Optional[dict], doc: Optional[dict]) -> str:
    """画面に出す短い要約（読めた項目の見出しだけ。個人名は出さない）"""
    got = []
    if vd:
        got.append('車検証: ' + '・'.join(n for n, k in (('登録番号', 'car_reg_serial'), ('車台番号', 'car_serial_no'), ('型式', 'car_model'),
                                                     ('型式指定/類別', 'car_model_designation'), ('使用者', 'customer_name'), ('有効期限', 'term_date'))
                                        if _get(vd, k)))
    if doc:
        got.append('書類: ' + '・'.join(n for n, k in (('依頼会社', 'company'), ('支店', 'branch'), ('担当者', 'staff'), ('事故番号', 'accept_no'),
                                                    ('事故日', 'accident_date'), ('契約者', 'contractor'), ('登録番号', 'reg_no'), ('車台番号', 'serial_no'),
                                                    ('カラーNo', 'color_code'), ('走行距離', 'mileage')) if _get(doc, k)))
    return ' ／ '.join(g for g in got if not g.endswith(': '))

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
