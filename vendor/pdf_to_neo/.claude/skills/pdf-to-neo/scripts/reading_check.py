# -*- coding: utf-8 -*-
"""reading_check.py — reading.json（見積書を印字のまま写したもの）を、ADDATA を使わずに紙の上だけで検算する。

使い方（files ディレクトリで）:
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/reading_check.py <reading.json> [--json <out.json>] [--save-profile]

目的: これまで実際に起きた転記ミスを、下書き（draft_estimate）や NEO 生成の前に紙の段階で捕まえる。
  1. 区分ごとの小計（ブロック / ページの `subtotal`）と印字小計の突合（行数・部品計・工賃計・印の数）
  2. 数量×単価 と 数量分の金額 の取り違え（`unit` があれば unit×qty=price、無ければ割り切れない金額に注意）
  3. 費用の集計先（expenses[].in）の欠落・語彙外、合計欄の再計算で「どの費用を動かせば合うか」の提示
  4. 左右の表記矛盾（見出しの左右と行の左右、同じ品番が同じ側に 2 行、左右で同じ品番）
  5. 工場ごとの設定（レバーレート・工賃の丸め単位・消費税の丸め・材料代の丸め）を総当たりで推定し、
     印字の合計欄がどの設定で一致するかを示す。既知の工場（<NEO_CHECK_ROOT>/_profiles/factory_profiles.json。PC ごと・git に入れない）と違えば警告
  6. 書式（A〜E）の自動判定と reading.format の食い違い

出力: FAIL（直すまで先に進めない）/ WARN（確認して理由が言えれば進める）/ NOTE（情報）。終了コード 0 = FAIL なし、1 = FAIL あり。
--save-profile: 検算が合格した案件の工場設定を factory_profiles.json に記録する（make_neo が合格時に呼ぶ）。
"""
from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from itertools import combinations
from typing import Optional

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
from draft_estimate import PARTS_NAME_BYTES, ROW_FIELDS, is_small_name, _cp932_len, _flag, _hw_kana, _num, _side_of, detect_wage_round, expand_row, hw, infer_labor_rate, labor_pairs, rate_score  # noqa: E402
from estimate_to_neo import is_bumper_only_paint  # noqa: E402  （draft_estimate の import で claude_neo_pipeline が sys.path に入る）

def _profiles_path() -> str:
    """工場プロファイル（工場名・レート・丸め）の置き場 = <NEO_CHECK_ROOT>/_profiles/factory_profiles.json。
    取引先名を含むので git・配布 zip には入れない（以前は reference/ にあった。2026-09-12 に移動。Codex 監査）。
    NEO_CHECK_ROOT は 環境変数 → %USERPROFILE%/.claude/pdf-to-neo.local.json → 既定 の順（skill_env と同じ規則）"""
    root = os.environ.get('NEO_CHECK_ROOT') or ''
    if not root:
        cfg = os.path.join(os.path.expanduser('~'), '.claude', 'pdf-to-neo.local.json')
        try:
            with open(cfg, encoding='utf-8-sig') as fh:
                root = str((json.load(fh) or {}).get('NEO_CHECK_ROOT') or '')
        except (OSError, ValueError):
            root = ''
    if not root:
        root = os.path.join(os.path.expanduser('~'), 'Documents', 'NEO_check')
    return os.path.join(root, '_profiles', 'factory_profiles.json')


PROFILES = _profiles_path()
IN_PARTS = ('部品計', '部品')
IN_WAGE = ('作業計', '工賃計', '工賃', '作業')
IN_EXPENSE = ('諸費用計', '諸費用', 'その他課税', '費用計', '費用')
IN_TAXFREE = ('非課税',)
IN_KNOWN = IN_PARTS + IN_WAGE + IN_EXPENSE + IN_TAXFREE


def _nfkc(s) -> str:
    return unicodedata.normalize('NFKC', str(s or ''))


def _int(v) -> Optional[int]:
    s = _num(v)
    if s == '':
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def _float(v) -> Optional[float]:
    s = _num(v)
    if s == '':
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _round_to(x: float, u: int) -> int:
    return int(x // u + (1 if (x % u) >= u / 2 else 0)) * u


def _expense_kinds(expenses: list) -> dict:
    """費用の名前（空白を除く）→ 集計先。部品と工賃の両方に金額がある費用は同じ名前で 2 行に写すので 'parts+wage' のようにまとめる
    （1 行ずつ比べると「過去は wage、今回は parts」と誤って WARN になる。2026-09-14 C-HR の TRD ドアハンドルプロテクター・モデリスタ 2 品）"""
    kinds: dict = {}
    for e in expenses:
        k = _in_kind(e)
        if k and k != '?':
            kinds.setdefault(_nfkc(e.get('name') or '').replace(' ', ''), set()).add(k)
    return {n: '+'.join(sorted(ks)) for n, ks in kinds.items()}


def _side_key(name) -> str:
    return _nfkc(name or '').replace(' ', '')


def _expense_names(expenses: list) -> set:
    return {k for k in (_side_key(e.get('name')) for e in expenses or []) if k}


def _confusable_row_names(rd: dict) -> set:
    """明細行のうち **費用に回されてもおかしくない行** の名前。部品番号も指数も無い手入力の実費・手続き行
    （リヤナンバー再封印・写真代 など）だけを拾う。部品や作業の行は費用と取り違えようがないので入れない。
    費用と明細のどちらに写すかは印字に区画が無いと読み手ごとにぶれ、金額は合うので検算では気づけない
    （2026-09-19 同じ見積の 2 回の読み取りで「リヤナンバー再封印 9,900」が明細→費用に動いた）"""
    out = set()
    for blk in rd.get('blocks') or []:
        for row in blk.get('rows') or []:
            try:
                r = expand_row(row)
            except ValueError:
                continue
            if str(r.get('code') or '').strip() or str(r.get('parts_no') or '').strip() or _num(r.get('index')):
                continue
            nm = _side_key(r.get('name'))
            if nm and not r.get('note'):
                out.add(nm)
    return out


def _in_kind(e: dict) -> str:
    """expenses[].in（見積書のどの合計欄に入っているか）→ parts / wage / expense / taxfree / ''（不明）"""
    s = _nfkc(e.get('in') or '').replace(' ', '')
    if not s:
        k = e.get('kind')
        if k == 'parts':
            return 'parts'
        if k == 'wage':
            return 'wage'
        return 'taxfree' if _flag(e.get('taxfree'), 'expenses[].taxfree') else ''
    if _flag(e.get('taxfree'), 'expenses[].taxfree') or any(w in s for w in IN_TAXFREE):
        return 'taxfree'
    if any(w in s for w in IN_PARTS):
        return 'parts'
    if any(w in s for w in IN_WAGE):
        return 'wage'
    if any(w in s for w in IN_EXPENSE):
        return 'expense'
    return '?'


# ---------------------------------------------------------------------- 税込で印字された見積書（judgment_rules 10-4）
TAX_TOTALS_KEYS = ('parts', 'wage', 'paint', 'paint_total', 'material', 'expense', 'expense_parts',
                   'expense_wage', 'frame', 'taxable', 'discount', 'recycle')
"""totals のうち税抜に直す金額のキー（肯定リスト）。tax（消費税）と total（税込の御見積額）は印字どおり、
制御キーと協定額 target_total も触らない。ここに無い数値キーは「決められない」ので直さず WARN にする"""
PAINT_WAGE_KEYS = ('base', 'booth', 'bumper_front', 'bumper_rear', 'bumper_base', 'wax', 'sealing',
                   'door_sash', 'stripe', 'low_cover', 'two_coat_solid', 'two_tone', 'frame')
"""paint の中で工賃・材料代が入るキー（額そのものか {index, wage} の dict）"""
TAX_AUTO_RATE = 10
"""自動で割り戻す税率（％）。生成器の消費税は 10% 固定（Setting.TaxRate=10）なので、8% などは自動で直さず止める（10-4）"""
_UNIT_RE = re.compile(r'(unit\s*[=:]\s*)([\d,]+)')


def tax_excluded_amount(v, rate: int = 10):
    """税込の金額を税抜に直す（四捨五入）。数値でなければそのまま返す"""
    n = _int(v)
    if n is None:
        return v
    s = -1 if n < 0 else 1  # 値引きの行（マイナス）も絶対値で丸めてから符号を戻す
    return s * int((abs(n) * 100 + (100 + rate) // 2) // (100 + rate))


def to_tax_excluded(rd: dict, rate: int = 10, warn=None) -> int:
    """『各行の金額まで税込』で刷られた見積書の読み取りを税抜に直す（judgment_rules 10-4 / reading_schema.md）。
    直すのは明細の price / wage / unit、ページ小計・ブロック小計、塗装、内板骨格、費用（非課税を除く）、
    値引き、ADAS、totals の金額項目、レバーレート。印字のまま残すのは totals の tax・total と制御キー、
    協定額の target_total（税込）、**非課税の費用**（税が乗っていないので割ると合計が合わなくなる）。
    warn を渡すと、割ってよいか決められなかったキーを知らせる。直した金額の数を返す"""
    n = [0]

    def d(v):
        if _int(v) is None:
            return v
        n[0] += 1
        return tax_excluded_amount(v, rate)

    def d_keys(obj, keys) -> None:
        if isinstance(obj, dict):
            for k in keys:
                if obj.get(k) not in (None, ''):
                    obj[k] = d(obj[k])

    def d_each(seq, keys) -> None:
        for it in seq or []:
            d_keys(it, keys)

    def d_unit_comment(s: str) -> str:
        """comment の `unit=単価` も税抜に直す（reading_check が単価×数量の検算に使う）"""
        return _UNIT_RE.sub(lambda m: m.group(1) + str(tax_excluded_amount(m.group(2), rate)), s)

    for b in rd.get('blocks') or []:
        rows = b.get('rows') or []
        for i, r in enumerate(rows):
            if isinstance(r, dict):
                d_keys(r, ('price', 'wage', 'unit'))
                d_keys(r.get('recycle'), ('price', 'stock_price'))  # リサイクル部品の売価・仕入値（生成器が部品計に入れる）
                if r.get('comment'):
                    r['comment'] = d_unit_comment(str(r['comment']))
            elif isinstance(r, str) and '|' in r:  # 短縮記法 code|name|method|parts_no|index|qty|price|wage|flags|comment
                p = r.split('|')
                for idx in (6, 7):
                    if len(p) > idx and _int(p[idx]) is not None:
                        p[idx] = str(d(p[idx]))
                if len(p) > 9:
                    p[9] = d_unit_comment(p[9])
                rows[i] = '|'.join(p)
        d_keys(b.get('subtotal'), ('parts', 'wage'))
    for sub in (rd.get('pages') or {}).values():
        d_keys(sub, ('parts', 'wage'))
    paint = rd.get('paint') or {}
    d_keys(paint, ('total', 'material', 'wage'))
    for key in ('lines', 'panels', 'other'):
        d_each(paint.get(key), ('wage', 'material', 'price'))
    for key in PAINT_WAGE_KEYS:
        v = paint.get(key)
        if isinstance(v, dict):  # frame は位置ごとの {'engine_room': {'option':.., 'index':.., 'wage':..}}
            d_each(v.values(), ('wage', 'material'))
            d_keys(v, ('wage', 'material'))
        elif _int(v) is not None and not isinstance(v, bool):
            paint[key] = d(v)
    fr = rd.get('frame') or {}      # 内板骨格（書式 E）: 基本工賃と部位ごとの工賃
    d_keys(fr, ('basic_wage', 'wage'))
    d_each(fr.get('items'), ('wage',))
    d_each(rd.get('adas'), ('wage', 'price', 'amount'))
    for e in rd.get('expenses') or []:   # 非課税の費用（リサイクル預託金・自賠責・税金）は税が乗っていない = 印字が素の金額
        if isinstance(e, dict) and _in_kind(e) != 'taxfree':
            d_keys(e, ('amount',))
    d_keys(rd.get('discount'), ('parts', 'wage'))   # 合計欄の値引き・割増（マイナスもある）
    t = rd.get('totals') or {}
    for k in list(t):
        if k in TAX_TOTALS_KEYS:
            d_keys(t, (k,))
        elif k not in TAX_KEEP_TOTALS and not isinstance(t[k], bool) and _int(t[k]) is not None and warn:
            warn(f'合計欄の {k} は税込・税抜のどちらか決められないので印字のまま残した（税込の見積: judgment_rules 10-4）')
    # レバーレートは税込で刷られていることも、税抜のまま刷られていることもある（同じ紙の中でレートだけ税抜の書式がある）。
    # **割ったあとの工賃**に対して、印字のレートと割ったレートのどちらが多くの行を説明するかで決める
    # （無条件に割ると、工賃 ÷ レート の指数が全行合わなくなり、標準工賃の違う NEO を「合格」で作ってしまう）
    printed_rate = _int(rd.get('labor_rate'))
    if printed_rate:
        div_rate = tax_excluded_amount(printed_rate, rate)
        keep = div_rate
        try:
            _rows = []
            for b in rd.get('blocks') or []:
                for r in b.get('rows') or []:
                    try:
                        _rows.append(expand_row(r))
                    except ValueError:
                        pass
            _rows += [l for l in ((rd.get('paint') or {}).get('lines') or []) if isinstance(l, dict)]
            _pairs = labor_pairs(_rows)
            if _pairs and rate_score(_pairs, printed_rate) > rate_score(_pairs, div_rate):
                keep = printed_rate   # レートだけ税抜で刷られていた
                if warn:
                    warn(f'レバーレート {printed_rate:,} は税抜のまま印字されている（割った {div_rate:,} より工賃÷指数に合う）ので割らない')
        except Exception:  # noqa: BLE001  決められないときは他の金額と同じく割る
            pass
        rd['labor_rate'] = keep
        if keep != printed_rate:
            n[0] += 1
    return n[0]


TAX_KEEP_TOTALS = ('tax', 'total', 'tax_rate', 'tax_round', 'wage_round', 'material_rate',
                   'neo_total', 'tolerance', 'tolerance_reason', 'tolerance_keys', 'target_total')
"""totals のうち「触らないと分かっている」キー（消費税・御見積額・制御キー・協定額）。
TAX_TOTALS_KEYS にもここにも無い数値キーは、割ってよいか決められないので WARN にして印字のまま残す"""


def tax_included_rate(rd: dict, why: Optional[list] = None) -> Optional[int]:
    """読み取りが『各行の金額まで税込』のままかどうかを Checker に判定させる。税込と決まったときだけ税率を返す。
    印字の合計欄と明細の積み上げだけで決めるので、読み手（人・LLM）の申告には頼らない。
    **税込の指摘以外に FAIL がある読み取りでは直さない** —— 転記の誤り（二重計上・写し落とし）で
    積み上げがたまたま御見積額と揃っただけの読み取りを、黙って全金額 1/1.1 にしないため"""
    try:
        ck = Checker(copy.deepcopy(rd))
        res = ck.run()
    except Exception as e:  # noqa: BLE001  検算が途中で落ちる読み取りは「決められない」= 直さない
        if why is not None:
            why.append(f'紙上検算が途中で落ちたので税込かどうか決められない: {type(e).__name__}: {e}')
        return None
    if not ck.tax_included:
        return None
    other = [f for f in res['fail'] if '税込で印字された見積書' not in f]
    if other:
        if why is not None:
            why.append('税込で印字された見積書に見えるが、ほかにも紙上検算の FAIL があるので自動では直さない: ' + other[0])
        return None
    if ck.tax_included != TAX_AUTO_RATE:  # 生成器の消費税は 10% 固定。8% などは税抜に直しても消費税・合計が 10% で計算される
        if why is not None:
            why.append(f'税率 {ck.tax_included}% の税込見積は自動で直さない（生成器の消費税は 10% 固定。judgment_rules 10-4）')
        return None
    return ck.tax_included


class Checker:
    def __init__(self, rd: dict):
        self.rd = rd
        self.msgs: list[tuple[str, str]] = []  # (level, text)
        self.rows: list[dict] = []  # 明細行（注記行を除く）。_block / _page / _no を付ける
        self.settings: dict = {}
        self.tax_included: Optional[int] = None  # 『各行の金額まで税込』で刷られた見積と決まったときの税率（judgment_rules 10-4）

    # ------------------------------------------------------------------ 出力
    def fail(self, t: str) -> None:
        self.msgs.append(('FAIL', t))

    def warn(self, t: str) -> None:
        self.msgs.append(('WARN', t))

    def note(self, t: str) -> None:
        self.msgs.append(('NOTE', t))

    # ------------------------------------------------------------------ 1. 行の展開
    def load_rows(self) -> None:
        n = 0
        for bi, blk in enumerate(self.rd.get('blocks') or []):
            title = str(blk.get('title') or '')
            for row in blk.get('rows') or []:
                try:
                    r = expand_row(row)
                except ValueError as e:
                    self.fail(f'行の形式（ブロック【{title}】）: {e}')
                    continue
                for _bk in ('manual', 'reserve'):   # 真偽値欄はここで 1 回だけ正規化（Codex 指摘）
                    if _bk in r:
                        r[_bk] = _flag(r[_bk], f'rows[].{_bk}')
                if r.get('note') and not r.get('name'):
                    continue
                n += 1
                r['_no'] = n
                r['_block'] = title
                r['_bi'] = bi
                r['_page'] = blk.get('page')
                r['_raw'] = row if isinstance(row, str) else ''
                self.rows.append(r)
        if not self.rows:
            self.fail('明細行が 1 行も無い（blocks[].rows）')

    # ------------------------------------------------------------------ 2. 行ごとの検査
    def check_rows(self, labor: int, wage_round: int) -> None:
        for r in self.rows:
            label = f"行{r['_no']} {str(r.get('name') or '')[:16]}"
            _q = _int(r.get('qty'))
            qty = 1 if _q is None else _q  # 数量 0 を 1 に化けさせない（0 以下は下の FAIL で拾う）
            price = _int(r.get('price'))
            wage = _int(r.get('wage'))
            index = _float(r.get('index'))
            mark = str(r.get('mark') or '')
            unit = _int(r.get('unit'))
            if unit is None:
                m = re.search(r'unit\s*[=:]\s*([\d,]+)', str(r.get('comment') or ''))
                if m:
                    unit = _int(m.group(1))
            if qty <= 0:
                self.fail(f'{label}: 数量 {qty} が 0 以下')
            if price is not None and price < 0:
                self.fail(f'{label}: 金額 {price:,} が負')
            if unit is not None and price is not None and unit * qty != price:
                # 税込印字を税抜に割り戻した案件（10-4）は、単価と金額を別々に四捨五入するので数量分までずれる
                if _int(self.rd.get('tax_included')) and abs(unit * qty - price) <= qty:
                    self.warn(f'{label}: 単価 {unit:,} × 数量 {qty} = {unit * qty:,} ≠ 金額 {price:,}（差 {unit * qty - price:+,}）。税込から税抜に直した丸めの差とみて止めない')
                else:
                    self.fail(f'{label}: 単価 {unit:,} × 数量 {qty} = {unit * qty:,} ≠ 金額 {price:,}（price は数量分の金額）')
            elif unit is None and qty > 1 and price and price % qty != 0:
                self.warn(f'{label}: 金額 {price:,} が数量 {qty} で割り切れない。単価ではなく数量分の金額か確認（単価なら comment に unit=単価）')
            if index is not None and index < 0:
                self.fail(f'{label}: 指数 {index} が負')
            if wage is not None and wage < 0:
                self.fail(f'{label}: 工賃 {wage:,} が負')
            if r.get('reserve') and (price or wage):
                self.note(f'{label}: 保留行（金額は合計に入れない）')
            # 工賃 = 指数 × レート の丸め（手入力工賃 '*' の行は対象外）
            if labor and index and wage is not None and '*' not in mark and not r.get('manual'):
                x = round(index * labor, 2)
                cands = {_round_to(x, 10), _round_to(x, 100), int(x)}
                if wage not in cands:
                    self.warn(f'{label}: 工賃 {wage:,} が 指数 {index} × レート {labor:,} の丸め（10 円 {_round_to(x, 10):,} / 100 円 {_round_to(x, 100):,}）と合わない。指数か工賃の読み違いか、手入力工賃なら flags に *')
                elif wage != _round_to(x, wage_round) and wage == _round_to(x, 100 if wage_round == 10 else 10):
                    self.warn(f"{label}: 工賃 {wage:,} は {100 if wage_round == 10 else 10} 円丸め。この案件の丸め単位は {wage_round} 円と推定しているので、行だけ違うなら手入力工賃（flags に *）")
            # flags の未知文字は draft_estimate.expand_row が ValueError で弾く（ここに来る行は展開済みで flags を持たない）
            # 短縮記法の列数

    # ------------------------------------------------------------------ 3. 左右
    def check_long_names(self) -> None:
        """手入力の行（M・汎用車種）の名称は見積の印字がそのまま NEO の名称欄（24 バイト）に入る。入らない行を先に知らせ、
        reading の行に neo_name（24 バイト以内の短い名前）を書いてもらう（書かなければ下書きが自動で短くし、確認箇所シートの 要確認 になる）"""
        generic = _flag((self.rd.get('vehicle') or {}).get('generic'), 'vehicle.generic')
        long_rows = [r for r in self.rows if (r.get('manual') or generic) and not r.get('neo_name')
                     and _cp932_len(hw(_hw_kana(str(r.get('name') or ''))).strip()) > PARTS_NAME_BYTES]
        if long_rows:
            self.warn(f'手入力の行の名称が NEO の名称欄 {PARTS_NAME_BYTES} バイトを超える: {len(long_rows)} 行（'
                      + '、'.join(f"行{r['_no']} {str(r.get('name') or '')[:14]}" for r in long_rows[:5])
                      + '）。dict の行にして neo_name（24 バイト以内）を書くと、その名前で NEO に入る（書かなければ自動で短くする）')

    def check_sides(self) -> None:
        for r in self.rows:
            side_row = _side_of(str(r.get('name') or ''))
            side_blk = _side_of(r.get('_block') or '')
            if side_row and side_blk and side_row != side_blk:
                self.warn(f"行{r['_no']} {r.get('name')}: 見出し【{r['_block']}】は {'右' if side_blk == 'R' else '左'} なのに行は {'右' if side_row == 'R' else '左'}。工場の誤記か、見出しをまたいだ行か確認")
        by_pn: dict[str, list[dict]] = {}
        for r in self.rows:
            pn = re.sub(r'[\s\-‐−ｰー]', '', _nfkc(r.get('parts_no') or '')).upper()
            if pn and re.search(r'\d{3}', pn):  # '新品' '脱着板金' のような区分語が品番欄に写された行は対象外
                by_pn.setdefault(pn, []).append(r)

        def _small(r: dict) -> bool:  # クリップ・ボルト等の小物や安価品は左右で同じ品番が普通
            unit = (_int(r.get('price')) or 0) // max(1, _int(r.get('qty')) or 1)
            return is_small_name(r.get('name') or '', unit) or 0 < unit < 1000   # ｸﾘｯﾌﾟ（小書き）も小物（2026-09-14 まで ｸﾘﾂﾌﾟ としか比べていなかった）

        def _plain(r: dict) -> str:
            return re.sub(r'^(右|左|RH|LH|R/H|L/H|R\.|L\.)\s*', '', _hw_kana(r.get('name') or '')).replace(' ', '')

        for pn, rs in by_pn.items():
            if len(rs) < 2:
                continue
            sides = [(_side_of(str(r.get('name') or '')) or _side_of(r.get('_block') or ''), r) for r in rs]
            for s in ('R', 'L'):
                same = [r for sd, r in sides if sd == s]
                for a, b in combinations(same, 2):
                    if a['_bi'] == b['_bi'] and _plain(a) == _plain(b) and str(a.get('method') or '') == str(b.get('method') or ''):
                        self.warn(f"品番 {rs[0].get('parts_no')} {a.get('name')} が同じブロックの {'右' if s == 'R' else '左'} に 2 行（行{a['_no']}, 行{b['_no']}）。重複か、片方は反対側の写し違いか確認")
            both = [r for sd, r in sides if sd == 'R'], [r for sd, r in sides if sd == 'L']
            if both[0] and both[1] and not all(_small(r) for r in both[0] + both[1]):
                self.warn(f"品番 {rs[0].get('parts_no')} が左右両方の行にある（行{both[0][0]['_no']} と 行{both[1][0]['_no']}）。左右で品番が違う部品（フェンダ・ドア・ランプ等）なら片方の写し間違い")

    # ------------------------------------------------------------------ 4. 小計（ブロック / ページ）
    def check_subtotals(self) -> None:
        def _sum(rows: list[dict]) -> tuple[int, int, int, dict]:
            p = sum(_int(r.get('price')) or 0 for r in rows if not r.get('reserve'))
            w = sum(_int(r.get('wage')) or 0 for r in rows if not r.get('reserve'))
            marks: dict[str, int] = {}
            for r in rows:
                for ch in str(r.get('mark') or ''):
                    marks[ch] = marks.get(ch, 0) + 1
            return len(rows), p, w, marks

        def _page_extras(pg) -> dict:
            """そのページに写した塗装行の工賃と費用（集計先別）。ページ小計が塗装・費用の区画を含んで印字される書式（コグニ印刷）の別解に使う"""
            if pg is None:
                return {}
            lines = [l for l in ((self.rd.get('paint') or {}).get('lines') or []) if isinstance(l, dict) and str(l.get('page')) == str(pg)]
            exps = [e for e in (self.rd.get('expenses') or []) if isinstance(e, dict) and str(e.get('page')) == str(pg)]
            return {'塗装行': sum(_int(l.get('wage')) or 0 for l in lines),
                    '費用（部品計）': sum(_int(e.get('amount')) or 0 for e in exps if _in_kind(e) == 'parts'),   # 集計先の語彙は _in_kind に揃える（Codex 指摘）
                    '費用（作業計）': sum(_int(e.get('amount')) or 0 for e in exps if _in_kind(e) == 'wage')}

        def _cmp(where: str, sub: dict, rows: list[dict], pg=None) -> None:
            n, p, w, marks = _sum(rows)
            extras = _page_extras(pg)
            alts = {'parts': [('費用（部品計）',)], 'wage': [('塗装行',), ('費用（作業計）',), ('塗装行', '費用（作業計）')]}
            for key, got, label in (('rows', n, '行数'), ('parts', p, '部品計'), ('wage', w, '工賃計')):
                exp = _int(sub.get(key))
                if exp is None:
                    continue
                if exp != got:
                    hit = next((names for names in alts.get(key, []) if all(extras.get(nm) for nm in names)
                                and got + sum(extras[nm] for nm in names) == exp), None)
                    if hit:  # 印字の小計が、そのページに写した塗装行・費用を含んでいる（コグニ印刷の最終ページ）
                        self.note(f'{where}: {label} 印字 {exp:,} = 明細 {got:,} + ' + ' + '.join(f'{nm} {extras[nm]:,}' for nm in hit) + '（小計が塗装・費用を含む書式）')
                        continue
                    self.fail(f'{where}: {label} 印字 {exp:,} / 転記 {got:,}（差 {got - exp:+,}）' + self._hint(got - exp, rows))
                else:
                    self.note(f'{where}: {label} {got:,} 一致')
            for ch, cnt in (sub.get('marks') or {}).items():
                if marks.get(ch, 0) != int(cnt):
                    self.fail(f"{where}: 印 {ch} の数 印字 {cnt} / 転記 {marks.get(ch, 0)}")

        for bi, blk in enumerate(self.rd.get('blocks') or []):
            if blk.get('subtotal'):
                _cmp(f"ブロック【{blk.get('title') or bi}】", blk['subtotal'], [r for r in self.rows if r['_bi'] == bi])
        pages = self.rd.get('pages') or {}
        if pages and not any(r.get('_page') for r in self.rows):
            self.warn('pages（ページ小計）があるのに blocks[].page が無い。ブロックに page を付けるとページ単位で検算できる')
        for pg, sub in (pages.items() if isinstance(pages, dict) else ((str(x.get('page')), x) for x in pages)):
            rows = [r for r in self.rows if str(r.get('_page')) == str(pg)]
            if not rows:
                if any(r.get('_page') for r in self.rows):
                    self.fail(f'ページ {pg}: 小計があるのに、このページの行が無い（blocks[].page を確認）')
                continue
            _cmp(f'ページ {pg}', sub, rows, pg)

    def _hint(self, diff: int, rows: list[dict]) -> str:
        """差額が 1 行の金額/工賃、または 1 つの費用と一致すれば、その行の集計先違いを示唆する"""
        if not diff:
            return ''
        d = abs(diff)
        hits = []
        for r in rows:
            for k in ('price', 'wage'):
                v = _int(r.get(k))
                if v and v == d:
                    hits.append(f"行{r['_no']} {str(r.get('name') or '')[:12]} の{'金額' if k == 'price' else '工賃'}")
        for e in self.rd.get('expenses') or []:
            if _int(e.get('amount')) == d:
                hits.append(f"費用 {e.get('name')}")
        if hits:
            return '。差額と同じ額: ' + '、'.join(hits[:4]) + '（集計先の違いか読み落とし）'
        return ''

    # ------------------------------------------------------------------ 5. 費用
    def check_expenses(self) -> None:
        for e in self.rd.get('expenses') or []:
            k = _in_kind(e)
            amt = _int(e.get('amount'))
            if amt is None:
                self.fail(f"費用 {e.get('name')}: amount が無い")
            if not k:
                self.fail(f"費用 {e.get('name')}: in（どの合計欄に入っているか: {' / '.join(('部品計', '作業計', '諸費用計', '非課税'))}）が無い")
            elif k == '?':
                self.warn(f"費用 {e.get('name')}: in={e.get('in')!r} は既知の語彙（{', '.join(IN_KNOWN)}）に無い。draft は部品計以外を工賃扱いにする")

    # ------------------------------------------------------------------ 6. 合計欄と設定の推定
    def check_totals(self, labor: int, wage_round: int) -> None:
        t = self.rd.get('totals') or {}
        if not t:
            self.warn('totals（見積書の合計欄）が無い。合計欄は必ず写す（検算の拠り所）')
            return
        rows = [r for r in self.rows if not r.get('reserve')]
        parts_sum = sum(_int(r.get('price')) or 0 for r in rows)
        unknown_w = []
        wage_sum = 0
        for r in rows:
            w = _int(r.get('wage'))
            idx = _float(r.get('index'))
            if w is not None:
                wage_sum += w
            elif idx and labor:
                wage_sum += _round_to(round(idx * labor, 2), wage_round)
            elif idx is None and w is None and (_int(r.get('price')) or 0) <= 0 and not r.get('manual'):
                unknown_w.append(r)  # 工賃も指数も金額も無い行（脱着・修理など）: 生成器が標準指数で補完する
        ex = self.rd.get('expenses') or []
        ex_by = {'parts': 0, 'wage': 0, 'expense': 0, 'taxfree': 0}
        for e in ex:
            k = _in_kind(e)
            if k in ex_by:
                ex_by[k] += _int(e.get('amount')) or 0
            elif k == '?':
                ex_by['wage'] += _int(e.get('amount')) or 0
        p = self.rd.get('paint') or {}
        # 塗装工賃 = 印字の塗装工賃計（無ければ塗装行・パネル＋内板骨格塗装の合算）＋ 追加項目（paint.other）。
        # 印字の塗装工賃計は内板骨格塗装を含み、追加項目（コグニ印刷の「追加塗装費用計」）を含まない（2026-09-14 C-HR: 180,280 = 塗装行 167,150 + ﾗｼﾞｴｰﾀｻﾎﾟｰﾄ 13,130、
        # 塗装費用計 251,050 = 180,280 + 材料代 55,890 + プライマー塗装 14,880）。追加項目はどの書き方でも 1 回だけ足す
        pf = p.get('frame') if isinstance(p.get('frame'), dict) else {}
        frame_pw = sum(_int((pf.get(k) or {}).get('wage')) or 0 for k in ('engine_room', 'front_pillar', 'center_pillar', 'rear_floor') if isinstance(pf.get(k), dict))
        other_w = sum(_int(x.get('wage')) or 0 for x in p.get('other') or [] if isinstance(x, dict))
        paint_w = _int(p.get('total'))
        # 内訳（塗装行・パネル・加算基礎・バンパ・付加塗装・内板骨格塗装）の合算。塗装工賃計の印字が無いときの塗装工賃で、
        # 印字があるときは「追加項目を含めて写したか」の見分けに使う（下）
        comp = None
        lines = p.get('lines') or []
        if lines:
            comp = sum(_int(l.get('wage')) or 0 for l in lines) + frame_pw
        elif p.get('panels') or is_bumper_only_paint(p):   # panels: [] はバンパだけの形のときだけ詳細塗装（生成器・inspect と同じ判定。Codex 指摘）
            comp = (sum(_int(x.get('wage')) or 0 for x in p.get('panels') or [])
                       + sum(_int((p.get(k) or {}).get('wage')) or 0 for k in ('base', 'booth', 'wax', 'bumper_front', 'bumper_rear', 'bumper_base', 'sealing', 'door_sash', 'stripe',
                                                                               'low_cover', 'two_coat_solid', 'two_tone') if isinstance(p.get(k), dict))
                       + frame_pw)   # 付加塗装も塗装工賃計（生成器・inspect と同じ範囲。Codex 指摘）
        if paint_w is None:
            paint_w = (comp if comp is not None else frame_pw) + other_w
        elif other_w and comp is not None and paint_w == comp + other_w:
            # 転記の塗装工賃計が「内訳 + 追加項目」とちょうど一致 = 追加項目を含めて写した（2026-09-15 N-BOX: 内訳 94,210 + アンダーコート 3,680 = 97,890）。
            # 足し直すと塗装計（材料込）・課税小計が追加項目の分だけ多くなり、正しい転記を不合格にしていた（生成器・inspect は内訳 + 追加項目で数える）
            self.note(f'塗装工賃計 {paint_w:,} は追加項目 {other_w:,} を含めて写されている（内訳 {comp:,} + 追加項目）。追加項目は足し直さない')
        else:
            paint_w += other_w
        other_in = other_w   # paint_w に含めた追加項目の工賃（材料代の割合を出すときは外す。追加項目は材料率の対象外）
        material = _int(p.get('material')) or 0
        # 材料の列に金額のある塗装行が塗装一式のほかにもある書式（ショートパーツ・写真代 … を材料の列に刷る工場）。
        # 印字の材料計と塗装行の材料の合計が一致するならそれで数える（下書き draft_estimate.paint と同じ条件。
        # 2026-09-16 アクセラ: 材料計 39,440 = 塗装一式 37,440 + 1,000 + 1,000 で、2,000 円足りないまま不合格になっていた）
        _mat_lines = sum(_int(l.get('material')) or 0 for l in lines if isinstance(l, dict))
        if _mat_lines and _mat_lines != material and _int(t.get('material')) == _mat_lines:
            self.note(f'材料計 {_mat_lines:,} は塗装行の材料の合計（材料の列だけに金額のある行を含む）。印字と一致するのでそれで数える')
            material = _mat_lines
        disc = self.rd.get('discount') or {}
        disc_sum = (_int(disc.get('parts')) or 0) + (_int(disc.get('wage')) or 0)
        frame = self.rd.get('frame') or {}
        frame_w = sum(_int(x.get('wage')) or 0 for x in frame.get('items') or []) + (_int(frame.get('basic_wage')) or 0) if frame else 0
        _ex_nonparts = ex_by['wage'] + ex_by['expense'] + ex_by['taxfree']   # 下書きが「費用の工賃分」として数える範囲（部品計に入る費用以外）
        # 塗装の一式が明細の手入力行と paint の両方にある reading（読み取りの二重計上）。
        # 下書き（draft_estimate._drop_double_paint）は印字の工賃計で片方に寄せるので、検算も同じ寄せ方で数える。
        # ここで寄せないと、下書きが直せる案件を紙上検算で止めてしまう（2026-09-16 シエンタ: 塗装費用 191,360 が二重）
        _kw_paint = re.compile(r'塗装|ﾄｿｳ|塗料|材料')
        dup_rows = [r for r in rows if r.get('manual') and (_int(r.get('wage')) or 0) > 0
                    and (_int(r.get('price')) or 0) <= 0              # 部品代のある行は寄せない（下書きと同じ条件）
                    and _kw_paint.search(_hw_kana(r.get('name') or ''))]
        dup_sum = sum(_int(r.get('wage')) or 0 for r in dup_rows)
        drop_paint = False
        _gw = _int(t.get('wage')) or 0
        _printed_paint = _int(t.get('paint')) or 0
        if dup_rows and paint_w > 0 and _gw > 0 and dup_sum in (paint_w, paint_w + material):
            # paint を数えないのは「手入力行と同じ額しか入っていない一式」のときだけ（材料代・追加項目・内板骨格塗装・
            # パネル別の内訳があるのに落とすと、証拠の無い金額まで消える）。draft_estimate._drop_double_paint と同じ条件
            _lump_only = (dup_sum == paint_w + material and not (p.get('other') or p.get('frame') or p.get('lines') or p.get('panels'))
                          and all(t.get(k) is None for k in ('paint', 'paint_total', 'material')))
            if _lump_only and _gw in (wage_sum, wage_sum + _ex_nonparts):
                drop_paint = True   # 手入力行が印字の工賃計に入っている = 塗装は明細側。paint は数えない
                self.note(f'塗装 {dup_sum:,} 円が明細の手入力行と paint の両方にある。印字の工賃計 {_gw:,} は手入力行を含む金額なので、'
                          '塗装計は明細の行で数える（下書きも paint を書かない）')
            elif _gw in (wage_sum - dup_sum, wage_sum - dup_sum + _ex_nonparts) and _printed_paint in (0, paint_w, paint_w + material):
                wage_sum -= dup_sum   # 印字の工賃計に入っていない = 塗装は paint 側。明細の手入力行は数えない
                self.note(f'塗装 {dup_sum:,} 円が明細の手入力行と paint の両方にある。印字の工賃計 {_gw:,} は手入力行を含まない金額なので、'
                          '塗装計は paint で数える（下書きも明細の手入力行を外す）')
        paint_w_sub = 0 if drop_paint else paint_w        # 課税小計に足す塗装工賃
        material_sub = 0 if drop_paint else material      # 同じく材料代

        # 工場の単価に円未満の端数がある見積（印字の金額が数量で割り切れない行がある）は、行ごとに円で足すコグニでは
        # 印字の合計をどうやっても再現できない（工場は端数のまま合計する）。差が「端数のある行数」に収まるなら
        # 読み取りの誤りではないので止めない。下書きが 3 点セット（neo_total/tolerance/理由）を書いて run_case が扱う
        # （2026-09-16 フリード: 単価 154.5・184.5 円のクリップで部品計が 2 円・課税小計が 1 円ずれた）
        _frac_rows = [r for r in rows if (_int(r.get('qty')) or 1) > 1 and (_int(r.get('price')) or 0) > 0
                      and (_int(r.get('price')) or 0) % (_int(r.get('qty')) or 1)]
        _frac_max = min(len(_frac_rows), 10)
        # 税込印字を税抜に割り戻した案件（10-4）は、印字の合計欄を割った値と、割った明細を積み上げた値が
        # 行ごとの四捨五入の分だけずれる（1 か所あたり 0.5 円まで）。どちらも読み取りは正しいので止めない
        _tax_on = bool(_int(self.rd.get('tax_included')))

        def _slack(n: int) -> int:
            """割り戻しの丸めで許される幅。印字を割った値と、割った明細の積み上げのそれぞれに 0.5 円まで乗る"""
            return (n + 3) // 2 if _tax_on else 0
        _n_price = sum(1 for r in rows if _int(r.get('price')))
        _n_wage = sum(1 for r in rows if _int(r.get('wage')))
        _n_lines = len(p.get('lines') or []) + len(p.get('other') or []) + len(p.get('panels') or [])
        _n_exp = len(self.rd.get('expenses') or [])
        _TAX_SLACK = {'部品計': _slack(_n_price), '工賃計（作業計）': _slack(_n_wage + _n_lines + _n_exp),
                      '塗装計': _slack(_n_lines), '塗装計（材料込）': _slack(_n_lines + 1),
                      '材料計': _slack(0), '諸費用計': _slack(_n_exp),
                      '課税小計': _slack(_n_price + _n_wage + _n_lines + _n_exp)}

        def cmp(label: str, calc: int, given, alts: Optional[dict] = None) -> bool:
            g = _int(given)  # 印字どおり '95,000' や空欄 '' でも落ちない（空欄・非数値は未記入扱い）
            if g is None:
                if given not in (None, ''):
                    self.warn(f'合計欄 {label}: {given!r} が数値として読めない')
                return True
            if g == calc:
                self.note(f'合計欄 {label}: {calc:,} 一致')
                return True
            for desc, alt in (alts or {}).items():
                if g == alt:
                    self.note(f'合計欄 {label}: 印字 {g:,} は「{desc}」として一致（明細だけの合計 {calc:,} とは違う）')  # report.md に必ず出す（make_neo）
                    return True
            _cands = [calc] + [int(v) for v in (alts or {}).values() if v is not None]
            _sl = _TAX_SLACK.get(label, 0)
            if _sl and any(abs(c - g) <= _sl for c in _cands):
                self.warn(f'合計欄 {label}: 印字を税抜に直した {g:,} / 転記から {calc:,}（差 {calc - g:+,}）。'
                          f'税込で印字された見積書（10-4）を割り戻した丸めの差（この項目は最大 {_sl:,} 円）とみて止めない')
                return False   # 見逃すが「一致」ではない（消費税の丸めは印字の課税小計で判定させる。下の _frac_max と同じ）
            if label in ('部品計', '課税小計') and _frac_max and any(0 < c - g <= _frac_max for c in _cands):
                # 見逃すが **False を返す**（呼び出し側の ok_sub が偽になり、消費税の丸めは「印字の課税小計」で判定される）。
                # True を返すと base がコグニ側の合計になり、四捨五入の工場を「切り捨て」と誤判定して
                # Setting.tx_ArrangeFlag の違う NEO を作ってしまう（レビュー指摘 2026-09-17）
                self.warn(f'合計欄 {label}: 印字 {g:,} / 転記から {calc:,}（差 {calc - g:+,}）。'
                          f'単価に円未満の端数がある行が {len(_frac_rows)} 行あるので、工場の端数計算とコグニの円計算の差とみて止めない'
                          f'（例 {" / ".join(str(r.get("name") or "")[:12] for r in _frac_rows[:3])}）。'
                          '下書きが totals.neo_total / tolerance に理由を書き、NEO は明細の金額どおりに作る')
                return False   # 見逃したが「一致」ではない（上のコメント: 消費税の丸めは印字の課税小計で判定させる）
            self.fail(f'合計欄 {label}: 印字 {g:,} / 転記から {calc:,}（差 {calc - g:+,}）' + self._hint(calc - g, rows))
            return False

        cmp('部品計', parts_sum, t.get('parts'), {'明細 + 費用（部品計）': parts_sum + ex_by['parts'], '明細 − 値引': parts_sum + (_int(disc.get('parts')) or 0)})
        # 工賃も指数も無い行があっても、印字の工賃計が明細の工賃の合計（＋費用の工賃分）とぴったり一致するなら
        # その空欄は 0 円（下書きが 0 円で渡し、生成器は標準指数で埋めない。draft_estimate._blank_wage_is_zero と同じ条件）
        blank_zero = bool(unknown_w) and _gw > 0 and _gw in (wage_sum, wage_sum + _ex_nonparts)
        if unknown_w and not blank_zero:
            self.warn(f'工賃も指数も無い行が {len(unknown_w)} 行（{", ".join(str(r.get("name") or "")[:10] for r in unknown_w[:4])}）。生成器が標準指数で補完するので、工賃計は検算できない。印字に工賃があるなら写す')
        else:
            if blank_zero:
                self.note(f'工賃も指数も無い行が {len(unknown_w)} 行あるが、印字の工賃計 {_gw:,} は明細の工賃の合計と一致する。'
                          'この見積の空欄は 0 円（下書きが 0 円で渡すので標準指数では埋めない）')
            wage_alts = {}
            for n in range(1, 5):
                for comb in combinations((('塗装工賃', paint_w), ('材料代', material), ('内板骨格', frame_w), ('費用（作業計）', ex_by['wage']), ('費用（諸費用）', ex_by['expense'])), n):
                    wage_alts['明細 + ' + ' + '.join(c[0] for c in comb)] = wage_sum + sum(c[1] for c in comb)
            cmp('工賃計（作業計）', wage_sum, t.get('wage'), wage_alts)
        if t.get('paint') is not None:
            cmp('塗装計', paint_w, t.get('paint'), {'塗装工賃 + 材料': paint_w + material, '塗装工賃計（追加項目を除く）': paint_w - other_in,
                                                    '塗装工賃計（追加項目を除く）+ 材料': paint_w - other_in + material})
        if t.get('paint_total') is not None:
            cmp('塗装計（材料込）', paint_w + material, t.get('paint_total'))
        if t.get('material') is not None:
            cmp('材料計', material, t.get('material'))
        if t.get('expense') is not None:
            cmp('諸費用計', ex_by['expense'], t.get('expense'), {'作業計扱いの費用を含む': ex_by['expense'] + ex_by['wage'], '費用すべて': sum(ex_by.values()), '非課税を含む': ex_by['expense'] + ex_by['taxfree']})
        if _num(self.rd.get('target_total')) != '':
            self.note(f"target_total {self.rd.get('target_total')}: 課税小計・消費税・合計は draft が材料代で合わせるので検算しない")
            # 協定額のある見積でも、合計欄が税込で印字されていれば気づけるように示す（自動では直さない。draft が材料代で解くので
            # 明細が税込のままだと材料代が大きくマイナスに寄る）
            _gs, _gt, _gx = _int(t.get('taxable')), _int(t.get('total')), _int(t.get('tax'))
            _r = _int(t.get('tax_rate')) or 10
            if _gs and _gt and _gx and _gs == _gt and 1 <= _r <= 30 and _gx in (
                    _gt * _r // (100 + _r), (_gt * _r * 2 + 100 + _r) // (2 * (100 + _r))):
                self.warn(f'合計欄: 課税小計の印字 {_gs:,} が御見積額と同じで、消費税 {_gx:,} はその内税。'
                          '金額が税込で印字された見積書（judgment_rules 10-4）かもしれない。明細も税込なら税抜に直してから協定額を入れる')
            return
        if unknown_w and not blank_zero:  # 工賃の無い行があると明細からの課税小計は出せないが、合計欄どうしの整合（課税小計 + 消費税 + 非課税 = 御見積額、税の丸め）は必ず見る
            self.wage_unchecked = True
            g_sub, g_tax, g_tot = _int(t.get('taxable')), _int(t.get('tax')), _int(t.get('total'))
            if g_sub is not None and g_tax is not None:
                for nm_, f_ in (('四捨五入', lambda x: (x * 10 + 50) // 100), ('切り捨て', lambda x: (x * 10) // 100), ('切り上げ', lambda x: -((-x * 10) // 100))):
                    if f_(g_sub) == g_tax:
                        self.note(f'消費税 {g_tax:,} は課税小計 {g_sub:,} の 10% {nm_}'); break
                else:
                    # 課税小計の印字が御見積額と同じで、消費税がその内税なら「金額が税込で印字された見積書」（10-4）。
                    # ただし工賃の無い行があると明細の積み上げで裏を取れないので、自動では直さず読み手に示す
                    r_ = _int(t.get('tax_rate')) or 10
                    hint_ = ('。課税小計の印字が御見積額と同じで、消費税はその内税（× {r} ÷ {rr}）: '
                             '金額が税込で印字された見積書（judgment_rules 10-4）かもしれない'.format(r=r_, rr=100 + r_)
                             if g_sub == _int(t.get('total')) and 1 <= r_ <= 30
                             and g_tax in (g_sub * r_ // (100 + r_), (g_sub * r_ * 2 + 100 + r_) // (2 * (100 + r_))) else '')
                    self.fail(f'合計欄 消費税: 印字 {g_tax:,} が課税小計 {g_sub:,} の 10%（四捨五入 {(g_sub * 10 + 50) // 100:,} / 切り捨て {(g_sub * 10) // 100:,} / 切り上げ {-((-g_sub * 10) // 100):,}）のどれとも一致しない' + hint_)
            _tf = ex_by['taxfree']
            if g_sub is not None and g_tax is not None and g_tot is not None and g_sub + g_tax + _tf != g_tot:
                self.fail(f'合計欄 御見積額: 印字 {g_tot:,} ≠ 課税小計 {g_sub:,} + 消費税 {g_tax:,} + 非課税 {_tf:,} = {g_sub + g_tax + _tf:,}')
            self.warn('工賃の無い行があるので「明細からの課税小計」は未検算（合計欄どうしの整合だけ確認した）')
            return
        sub = parts_sum + wage_sum + paint_w_sub + material_sub + frame_w + ex_by['parts'] + ex_by['wage'] + ex_by['expense'] + disc_sum
        if self._check_tax_included(sub, t, ex_by['taxfree']):
            return   # 行が税込のまま。課税小計・消費税・御見積額を突き合わせても「税込だから合わない」としか出ない
        # 自動では直せなかった（写し誤りがある・合計欄だけ税抜 など）税込の見積に気づけるようにする。
        # 明細の積み上げが「御見積額 − 消費税」より「御見積額」に近いなら、行が税込のままの疑いが濃い
        _gt0, _gx0 = _int(t.get('total')), _int(t.get('tax'))
        if not _tax_on and sub and _gt0 and _gx0 and \
                abs(sub - (_gt0 - ex_by['taxfree'])) < abs(sub - (_gt0 - _gx0 - ex_by['taxfree'])):
            self.fail(f'明細の積み上げ {sub:,} が「御見積額 {_gt0:,} − 消費税 {_gx0:,}」よりも御見積額そのものに近い: '
                      f'各行の金額まで税込で印字された見積書（judgment_rules 10-4）の疑い。'
                      f'そうなら明細・塗装・費用・合計欄を 1.1 で割って税抜に直す（消費税と御見積額は印字どおり）')
        ok_sub = cmp('課税小計', sub, t.get('taxable'))
        g_sub = _int(t.get('taxable'))
        base = g_sub if (g_sub is not None and not ok_sub) else sub  # 課税小計が印字と違うときは、税の丸めは印字の課税小計で判定する（原因を分けるため）
        if g_sub is None and _tax_on:
            # 税込を割り戻した案件で課税小計の印字が無いと、積み上げに丸めが乗っていて消費税の丸め方を取り違える
            # （四捨五入の工場を「切り捨て」と誤判定し、Setting.tx_ArrangeFlag の違う NEO になる）。印字から出す
            _gt, _gx = _int(t.get('total')), _int(t.get('tax'))
            if _gt is not None and _gx is not None and _gt - _gx - ex_by['taxfree'] > 0:
                base = _gt - _gx - ex_by['taxfree']
        tax_modes = {'四捨五入': (base * 10 + 50) // 100, '切り捨て': base * 10 // 100, '切り上げ': -(-base * 10 // 100)}
        g_tax = _int(t.get('tax'))
        if g_tax is not None:
            hit = [k for k, v in tax_modes.items() if v == g_tax]
            if not hit:
                self.fail(f"合計欄 消費税: 印字 {g_tax:,} は課税小計 {base:,} の 10%（四捨五入 {tax_modes['四捨五入']:,} / 切り捨て {tax_modes['切り捨て']:,}）のどれとも違う。課税小計か消費税の読み違い、または非課税費用の混入")
            elif '四捨五入' not in hit:
                self.warn(f"合計欄 消費税: {hit[0]} で一致（既定は四捨五入）。reading に tax_round: '{hit[0]}' を書く（下書きが自動で入れる）。tolerance で逃げない")
                self.settings['tax_round'] = hit[0]
            else:
                self.settings['tax_round'] = '四捨五入'
        g_total = _int(t.get('total'))
        if g_total is not None and g_tax is not None and g_sub is not None:
            if g_sub + g_tax + ex_by['taxfree'] != g_total:
                self.fail(f"合計欄 御見積額: 印字 {g_total:,} ≠ 課税小計 {g_sub:,} + 消費税 {g_tax:,} + 非課税 {ex_by['taxfree']:,} = {g_sub + g_tax + ex_by['taxfree']:,}（合計欄自体の読み違いか、非課税費用の見落とし）")
        # 材料代の割合と丸め方
        if material and paint_w - other_in:
            rate = material * 100.0 / (paint_w - other_in)
            self.settings['material_rate'] = round(rate, 1)
            near = min((abs(rate - x), x) for x in range(10, 101, 5))
            if near[0] > 0.6:
                self.note(f'材料代 {material:,} ÷ 塗装工賃 {paint_w - other_in:,} = {rate:.1f}%（5% 刻みでない → 行ごと四捨五入の合算か、材料代に他の費目が混ざっている）')

    # ------------------------------------------------------------------ 6a. 税込で印字された見積書（judgment_rules 10-4）
    def _check_tax_included(self, sub: int, t: dict, taxfree: int) -> bool:
        """『各行の金額まで税込』で刷られた見積書（ディーラー・二輪）を、印字の合計欄と明細の積み上げだけで見分ける。
        次がすべて成り立つときだけ税込と決める:
          1. 明細の積み上げが「税込の御見積額 − 非課税費用」と同じ（税抜の見積なら 御見積額 − 消費税 になる）
          2. 印字の消費税が、その額の内税（額 × r ÷ (100+r)）
          3. 税抜の見積で必ず成り立つ「課税小計 + 消費税 + 非課税 = 御見積額」が**成り立たない**
             （成り立つならふつうの税抜の見積。1 がたまたま揃っただけ）
          4. 合計欄に部品計・工賃計・塗装計・課税小計のどれかが印字されている
             （積み上げを突き合わせる相手が印字に無い読み取りでは、写し誤りと見分けられない）
        見分けたら税率を self.tax_included に入れて FAIL にする（reading_pages.merge が税抜に直して束ね直す）"""
        g_tax, g_tot = _int(t.get('tax')), _int(t.get('total'))
        if not g_tax or not g_tot or g_tot <= g_tax:
            return False
        rate = _int(t.get('tax_rate')) or 10
        if not 1 <= rate <= 30:
            return False
        base = g_tot - taxfree  # 非課税費用は税の対象外（御見積額には入っている）
        # 税抜の見積なら積み上げは 御見積額 − 消費税 になる（消費税は御見積額の 1 割前後）ので、数円の幅で取り違えることはない
        if base <= 0 or abs(sub - base) > max(2, min(len(self.rows), 10)):
            return False
        if g_tax not in (base * rate // (100 + rate), (base * rate * 2 + 100 + rate) // (2 * (100 + rate)),
                         -(-base * rate // (100 + rate))):
            return False
        g_sub = _int(t.get('taxable'))
        if g_sub is not None and g_sub + g_tax + taxfree == g_tot:
            return False   # 3. 合計欄が税抜として筋が通っている
        if not any(_int(t.get(k)) for k in ('parts', 'wage', 'paint', 'paint_total', 'taxable')):
            return False   # 4. 突き合わせる印字が合計欄に無い
        self.tax_included = rate
        self.fail(f'金額が税込で印字された見積書（judgment_rules 10-4）: 明細の積み上げ {sub:,} が印字の御見積額 {g_tot:,} と同じで、'
                  f'印字の消費税 {g_tax:,} はその内税（{base:,} × {rate} ÷ {100 + rate}）。'
                  f'明細・塗装・費用・合計欄の金額を {(100 + rate) / 100:g} で割って税抜に直す（tax と total は印字どおり）')
        return True

    # ------------------------------------------------------------------ 6b. 手入力行モード（塗装・費用を塗装/費用画面ではなく明細の手入力行で入れる工場）
    def check_layout_mode(self) -> None:
        p = self.rd.get('paint') or {}
        has_paint = bool(p.get('lines') or p.get('panels') or _int(p.get('total')))
        has_expenses = bool(self.rd.get('expenses'))
        kw_paint = re.compile(r'塗装|ﾄｿｳ|塗料|材料')
        kw_exp = re.compile(r'産廃|廃棄|写真|ｺｰﾃｨﾝｸﾞ|コーティング|環境|洗車|代車|ﾚｯｶｰ|レッカー|ｼｮｰﾄﾊﾟｰﾂ|ショートパーツ')
        m_paint = [r for r in self.rows if r.get('manual') and kw_paint.search(_hw_kana(r.get('name') or ''))]
        m_exp = [r for r in self.rows if r.get('manual') and kw_exp.search(_hw_kana(r.get('name') or ''))]
        mode = False
        for label, rows_, explicit, where in (('塗装', m_paint, has_paint, 'paint'), ('費用', m_exp, has_expenses, 'expenses')):
            if not rows_:
                continue
            if explicit:
                self.warn(f"{label}らしい手入力行が {len(rows_)} 行（{', '.join(str(r.get('name') or '')[:10] for r in rows_[:3])}）あり {where} にも書かれている。二重計上でないか合計欄で確かめる"
                          + ('（印字の工賃計でどちらが正か決まる場合は、下書きが片方に寄せて報告書に残す）' if label == '塗装' else ''))
            else:
                mode = True
        if mode:
            self.settings['manual_rows_mode'] = True
            self.note(f'手入力行モード: 塗装/費用らしい手入力行が {len(m_paint) + len(m_exp)} 行あり、対応する paint/expenses が空（工場は塗装/費用画面を使わず明細に手入力。format_catalog A の細則どおり manual 行で写す）')

    # ------------------------------------------------------------------ 7. 書式
    def detect_format(self) -> str:
        rows = self.rows
        has_code = any(r.get('code') for r in rows)
        has_index = any(_float(r.get('index')) for r in rows)
        methods = {_nfkc(r.get('method') or '').replace(' ', '') for r in rows}
        titles = [b.get('title') for b in self.rd.get('blocks') or [] if b.get('title')]
        has_mark = any(r.get('mark') for r in rows)
        if has_code and (has_mark or any(k.startswith('page') for k in (self.rd.get('totals') or {}))):
            return 'A'
        if has_code:
            return 'A/B'
        if '部品' in methods and has_index:
            return 'C'
        if self.rd.get('frame') or any(_flag(r.get('reserve'), 'rows[].reserve') for r in rows) or any(_flag(e.get('taxfree'), 'expenses[].taxfree') for e in self.rd.get('expenses') or []):
            return 'E'
        if not has_index and not any(_int(r.get('wage')) for r in rows):
            return 'D'
        return 'B' if titles or has_index else 'D'

    def check_format(self) -> None:
        det = self.detect_format()
        given = str(self.rd.get('format') or '').strip().upper()[:1]
        self.settings['format'] = given if given in det.split('/') else det  # 記録には reading の書式（判定と矛盾しなければ）
        if not given:
            self.note(f'format 未記入。特徴からは書式 {det}（format_catalog.md）')
        elif given not in det.split('/'):
            self.warn(f'reading.format={given} だが特徴からは書式 {det}（コード列 {"あり" if det == "A" else "なし"} 等）。format_catalog.md の判定表で確認し、どちらでもなければ新しい書式として追記')

    # ------------------------------------------------------------------ 8. 工場プロファイル
    def check_profile(self, labor: int, wage_round: int) -> None:
        issuer = issuer_key(self.rd.get('issuer'))
        prof = load_profiles().get(issuer) if issuer else None
        if not issuer:
            self.warn('issuer（工場名）が無い。工場ごとの設定（丸め・費用の集計先）を記録・照合できない')
            return
        if not prof:
            self.note(f'工場「{issuer}」は初めて（factory_profiles.json に未登録。合格時に記録する）')
            return
        self.note(f"工場「{issuer}」: 過去 {prof.get('count', 1)} 件（レート {prof.get('labor_rate')} / 丸め {prof.get('wage_round')} 円 / 書式 {prof.get('format')}）")
        if prof.get('wage_round') and int(prof['wage_round']) != wage_round:
            self.warn(f"工場「{issuer}」の工賃丸めは過去 {prof['wage_round']} 円、今回の推定は {wage_round} 円。工賃の読み違いか設定変更か確認")
        if prof.get('labor_rate') and labor and int(prof['labor_rate']) != labor:
            self.note(f"工場「{issuer}」のレートは過去 {prof['labor_rate']:,}、今回 {labor:,}（保険会社や協定で変わるので情報のみ）")
        if prof.get('format') and self.settings.get('format') and prof['format'] != self.settings['format']:
            self.warn(f"工場「{issuer}」の書式は過去 {prof['format']}、今回の判定は {self.settings['format']}")
        if prof.get('manual_rows_mode') and not self.settings.get('manual_rows_mode'):
            self.note(f"工場「{issuer}」は過去、塗装・費用を明細の手入力行で入れていた。今回 paint/expenses に書いたなら印字を見直す（手入力行モードなら manual 行で）")
        for nm, now in _expense_kinds(self.rd.get('expenses') or []).items():
            past = (prof.get('expense_in') or {}).get(nm)
            # 旧形式（同名 2 行を 1 つの区分で保存: 'wage'）→ 今回 'parts+wage' への移行だけ許す。'parts+wage' から片方に減ったときは写し漏れの疑いなので WARN（Codex 指摘）
            if past and past != now and not (now == 'parts+wage' and past in ('parts', 'wage')):   # 例外は旧形式の単一区分 → 'parts+wage' だけ（expense+wage 等は区分の変更として WARN。Codex 指摘）
                self.warn(f"費用 {nm}: 過去この工場では {past} 扱い、今回は {now}。合計欄で確かめる")
        self.check_side_drift(prof)

    def check_side_drift(self, prof: dict) -> None:
        """同じ名前が、前は費用・今度は明細（またはその逆）になっていないか。
        費用の区画が印字されていない見積では読み手ごとに振り分けがぶれるが、金額は合うので検算では出ない。
        NEO では費用画面と明細画面のどちらに出るかが変わるので、過去と違うときだけ知らせる"""
        past_ex = set(prof.get('expense_in') or {})
        past_rows = set(prof.get('row_names') or [])
        both = past_ex & past_rows   # この工場では両方に印字される名前（判断の material にならない）
        for nm in sorted((_confusable_row_names(self.rd) & past_ex) - both):
            self.warn(f"「{nm}」は今回 明細行。過去この工場では費用に写していた（金額は合っていても NEO で出る画面が変わる）。印字の区画で確かめる")
        for nm in sorted((_expense_names(self.rd.get('expenses')) & past_rows) - both):
            self.warn(f"「{nm}」は今回 費用。過去この工場では明細行に写していた（金額は合っていても NEO で出る画面が変わる）。印字の区画で確かめる")

    # ------------------------------------------------------------------ 実行
    def run(self) -> dict:
        self.load_rows()
        r_ = _int(self.rd.get('tax_included'))
        if r_:  # 束ねるときに税抜へ直した見積（judgment_rules 10-4）。報告文に必ず出す（印字の金額と読み取りが違って見えるので）
            t_ = self.rd.get('totals') or {}
            back = ' / '.join(f'{nm} {int((v * (100 + r_) + 50) // 100):,}'
                              for nm, k in (('部品計', 'parts'), ('工賃計', 'wage'), ('課税小計', 'taxable'))
                              if (v := _int(t_.get(k))) is not None)
            self.warn(f'金額が税込で印字された見積書（judgment_rules 10-4）なので、明細・塗装・費用・合計欄を {(100 + r_) / 100:g} で割って税抜に直してある。'
                      '報告の「見積書」の欄は税抜、消費税と御見積額は印字どおり'
                      + (f'。見積書の印字（税込）は {back}' if back else '')
                      + '。コグニの消費税設定は内税にしてあるので、画面・帳票は印字と同じ税込で並ぶ')
        if not self.rows:
            return self.result()
        lines = list((self.rd.get('paint') or {}).get('lines') or [])
        labor = _int(self.rd.get('labor_rate')) or infer_labor_rate(self.rows + lines)
        wage_round = _int(self.rd.get('wage_round')) or detect_wage_round(self.rows + lines, labor)
        self.settings.update({'labor_rate': labor, 'wage_round': wage_round})
        if _int(self.rd.get('labor_rate')):
            pairs = labor_pairs(self.rows + lines)
            guess = infer_labor_rate(self.rows + lines)
            if guess and guess != labor and rate_score(pairs, guess) > rate_score(pairs, labor):  # 推定の方が多くの行を説明できるときだけ（同点なら指定を信じる）
                self.warn(f"labor_rate {labor:,} が指定されているが、工賃÷指数からは {guess:,} の方が多くの行に合う（{rate_score(pairs, guess)}/{len(pairs)} 行、指定は {rate_score(pairs, labor)} 行）。協定レートで指数だけ写した案件なら問題ない")
        self.note(f'推定: レバーレート {labor:,} / 工賃丸め {wage_round} 円')
        self.check_rows(labor, wage_round)
        self.check_sides()
        self.check_long_names()
        self.check_subtotals()
        self.check_expenses()
        self.check_totals(labor, wage_round)
        self.check_layout_mode()
        self.check_format()
        self.check_profile(labor, wage_round)
        return self.result()

    def result(self) -> dict:
        return {'fail': [t for l, t in self.msgs if l == 'FAIL'], 'warn': [t for l, t in self.msgs if l == 'WARN'], 'note': [t for l, t in self.msgs if l == 'NOTE'],
                'settings': self.settings, 'rows': len(self.rows)}


# ---------------------------------------------------------------------- 工場プロファイル
def issuer_key(issuer) -> str:
    """工場名の鍵: 最初の括弧・住所・TEL より前の部分だけ（住所・電話・担当者名はプロファイルに入れない）"""
    s = _nfkc(issuer).strip()
    s = re.split(r'[（(〒]|(?<![A-Za-z])[Tt][Ee][Ll][.:：\s\-]*\d|\s*電話|\s*担当|\s*FAX|\s*ファックス', s)[0]
    s = re.sub(r'\s+(北海道|東京都|京都府|大阪府|.{2,3}県).*$', '', s)
    return s.strip()


def load_profiles() -> dict:
    """読めなければ {}（検査は続ける）。壊れた JSON は save_profile 側で退避してから書く"""
    try:
        with open(PROFILES, encoding='utf-8-sig') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}


class _Lock:
    """factory_profiles.json の排他（並行実行の保護）: <file>.lock を O_EXCL で作る。取れなければ最大 5 秒待つ"""
    def __init__(self, path: str):
        self.path = path + '.lock'
        self.fd = None

    def __enter__(self):
        deadline = time.time() + 5.0
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 60:  # 落ちたプロセスのロックは捨てる
                        os.remove(self.path); continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError(f'ロックが取れない: {self.path}')
                time.sleep(0.1)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.remove(self.path)
        except OSError:
            pass


def save_profile(rd: dict, settings: dict) -> str:
    """合格した案件の工場設定を記録する（工場名・レート・丸め・書式・費用名→集計先。顧客情報は入れない）"""
    issuer = issuer_key(rd.get('issuer'))
    if not issuer:
        return ''
    try:  # 記録は補助。書けない場所（読み取り専用の共有フォルダ等）でも検算の結果は返す（監査 15）
        os.makedirs(os.path.dirname(PROFILES), exist_ok=True)
        with _Lock(PROFILES):
            return _save_profile_locked(rd, settings, issuer)
    except (TimeoutError, OSError) as e:
        print(f'工場プロファイルを書けなかった（検算は終わっている）: {type(e).__name__}: {e}')
        return ''


def _save_profile_locked(rd: dict, settings: dict, issuer: str) -> str:
    if os.path.exists(PROFILES):
        try:
            with open(PROFILES, encoding='utf-8-sig') as fh:
                data = json.load(fh)
            profs = data if isinstance(data, dict) else {}
        except (OSError, ValueError):  # 壊れたファイルは消さずに退避し、空から作り直す（中身は退避先に残る）
            bak = PROFILES + '.corrupt-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S')
            try:
                os.replace(PROFILES, bak)
                print(f'factory_profiles.json が読めないので {os.path.basename(bak)} に退避した')
            except OSError:
                return ''
            profs = {}
    else:
        profs = {}
    p = profs.get(issuer) or {}
    ex_in = dict(p.get('expense_in') or {})
    for _nm, _now in _expense_kinds(rd.get('expenses') or []).items():
        _past = ex_in.get(_nm)
        if str(_past) == 'parts+wage' and _now in ('parts', 'wage'):
            continue   # 'parts+wage' を今回の片方だけで上書きしない（1 回の写し漏れで次から WARN が消えるのを防ぐ。Codex 指摘）
        ex_in[_nm] = _now
    # 費用に回されてもおかしくない明細行の名前を覚えておく（次に費用へ動いたら check_side_drift が気づく）
    rn = sorted(set(p.get('row_names') or []) | _confusable_row_names(rd))[:200]
    t = rd.get('totals') or {}
    case_id = hashlib.sha1(f"{rd.get('est_date', '')}|{t.get('total', '')}|{t.get('parts', '')}".encode('utf-8')).hexdigest()[:10]  # 案件の識別（顧客情報は使わない）
    seen = list(p.get('seen') or [])
    if case_id not in seen:
        seen.append(case_id)
    p.update({'labor_rate': settings.get('labor_rate'), 'wage_round': settings.get('wage_round'), 'format': settings.get('format'),
              'tax_round': settings.get('tax_round', p.get('tax_round')), 'material_rate': settings.get('material_rate', p.get('material_rate')),
              'manual_rows_mode': bool(settings.get('manual_rows_mode', p.get('manual_rows_mode'))),
              'expense_in': ex_in, 'row_names': rn, 'count': len(seen), 'seen': seen[-50:], 'last': datetime.date.today().isoformat()})
    profs[issuer] = p
    tmp = f'{PROFILES}.{os.getpid()}.tmp'  # 並行実行で一時ファイルを共有しない
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(profs, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, PROFILES)  # 途中で落ちても元のファイルは壊れない
    return PROFILES


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('reading')
    ap.add_argument('--json', default='')
    ap.add_argument('--save-profile', action='store_true')
    ap.add_argument('--quiet', action='store_true', help='NOTE を表示しない')
    a = ap.parse_args()
    rd = json.load(open(a.reading, encoding='utf-8-sig'))
    res = Checker(rd).run()
    for level in ('FAIL', 'WARN', 'NOTE'):
        if level == 'NOTE' and a.quiet:
            continue
        for t in res[level.lower()]:
            print(f'{level}: {t}')
    print(f"reading_check: FAIL {len(res['fail'])} / WARN {len(res['warn'])} / 明細 {res['rows']} 行 / 推定 {res['settings']}")
    if a.json:
        json.dump(res, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if a.save_profile and not res['fail']:
        p = save_profile(rd, res['settings'])
        if p:
            print('工場プロファイルを記録:', p)
    return 1 if res['fail'] else 0


if __name__ == '__main__':
    sys.exit(main())
