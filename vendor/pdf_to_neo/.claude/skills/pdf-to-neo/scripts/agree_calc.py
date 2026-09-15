# -*- coding: utf-8 -*-
"""協定額（税込）に合わせる調整の候補を、方法ごとに計算して並べる（読むだけ。reading・NEO は書かない）。判断規則 10-14 の「方法を選ぶ」段で使う。

    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/agree_calc.py "<NEO_CHECK_ROOT>/<案件>" --target 725000
        [--method rate|index|material|paint|frame] [--rows 5] [--step 10] [--json <出力先>]

前提: 工場見積そのままで make_neo が合格している（案件フォルダの estimate.json が工場見積どおり）。協定調整は、その estimate を元に
生成器（NeoBuilder）で試算して数字を出す（0.2 秒 / 回）。出すもの:
  - 協定額に届く課税小計（消費税の丸め 3 通り。四捨五入では 1 円単位でちょうどにならない額がある → 切り捨て/切り上げ）
  - レバーレートで: 指数のある行（明細・塗装・内板骨格）を 指数 × 新レート にしたときの合計。協定額を超えない最大のレートと残り
  - 指数で（行を指定）: 工賃の大きい行から、0.1 刻みで何段下げると届くか（行ごと）と残り
  - 塗装材料代で（パネル明細の塗装）/ 塗装一式で（一式の塗装）: 動かす額
  - 内板骨格の基本指数を外すと（骨格を鈑金に振るとき）: 減る額
  - それぞれ reading への書き方
損保の指示が方法を決める（「工賃で調整」「塗装で調整」「レート X 円」「骨格を鈑金に」）。指示の無い行を合計合わせに動かさない（判断規則 10-14）。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import math
import os
import sys
from typing import Optional

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402

skill_env.apply()
sys.path.insert(0, os.path.join(skill_env.FILES, 'claude_neo_pipeline'))
from estimate_to_neo import NeoBuilder, is_bumper_only_paint  # noqa: E402

TAX_MODES = ('四捨五入', '切り捨て', '切り上げ')


def tax_of(s: int, mode: str) -> int:
    """消費税 10%（生成器・draft と同じ整数演算）"""
    if mode == '切り捨て':
        return (s * 10) // 100
    if mode == '切り上げ':
        return -((-s * 10) // 100)
    return (s * 10 + 50) // 100


def solve_taxable(target: int, taxfree: int, mode: str) -> Optional[int]:
    """課税小計 S + 消費税(S) + 非課税費用 = 協定額 になる S（無ければ None）"""
    base = int((target - taxfree) / 1.1)
    return next((s for s in range(base - 3, base + 4) if s + tax_of(s, mode) + taxfree == target), None)


def _taxfree(est: dict) -> int:
    out = 0
    for e in est.get('expenses') or []:
        if str(e.get('taxfree')).lower() in ('true', '1', 'yes') or e.get('taxfree') is True:
            try:
                out += int(float(str(e.get('amount') or 0).replace(',', '')))
            except ValueError:
                pass
    return out


class Prober:
    """estimate を生成器に通して合計を試算する（NEO は書かない）"""

    def __init__(self):
        self.nb = NeoBuilder()
        self.n = 0

    def totals(self, est: dict, rate: Optional[int] = None) -> dict:
        e = copy.deepcopy(est)
        e.pop('totals', None)
        self.n += 1
        with contextlib.redirect_stdout(io.StringIO()):
            _, rep = self.nb.build(e, e['vehicle'], hints=e.get('hints'), labor_rate=(rate if rate is not None else e.get('labor_rate')),
                                   est_date=e.get('est_date'), insurance=e.get('insurance'))
        return rep['totals']


def _num(v) -> float:
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return 0.0


def _round_wage(x: float, unit: int) -> int:
    """生成器と同じ工賃の丸め（丸め単位で四捨五入）"""
    u = max(1, int(unit or 10))
    x = round(x, 2)
    return int(x // u + (1 if (x % u) >= u / 2 else 0)) * u


def index_only(est: dict, rate0: int) -> tuple[dict, int, int]:
    """レートを変えて試算するための estimate: 指数のある行は工賃を消して 指数 × レート にする。工賃だけの行は、指数（0.1 刻み、無ければ 0.01 刻み）
    × 今のレートを工賃の丸め（wage_round）で丸めた値が工賃と一致すれば指数に直す（技術料だけの書式。1.2 × 7,960 = 9,552 → 9,550 も直す）。
    直せない工賃（手入力の金額）はそのまま。塗装の中の内板骨格塗装（paint.frame の各位置）も同じ。戻り値 (estimate, 指数の行数, 固定の工賃の行数)"""
    e = copy.deepcopy(est)
    n_idx = n_fix = 0
    unit = int(_num(est.get('wage_round')) or 10)

    def conv(d: dict) -> None:
        nonlocal n_idx, n_fix
        idx, w = _num(d.get('index')), _num(d.get('wage'))
        if idx > 0:
            d.pop('wage', None)
            n_idx += 1
        elif w > 0 and rate0:
            for nd in (1, 2):
                x = round(w / rate0, nd)
                if x > 0 and _round_wage(x * rate0, unit) == int(round(w)):
                    d['index'] = x
                    d.pop('wage', None)
                    n_idx += 1
                    break
            else:
                n_fix += 1
    for it in e.get('items') or []:
        if isinstance(it, dict) and not it.get('reserve'):
            conv(it)
    p = e.get('paint') or {}
    for k, v in list(p.items()):
        if k.startswith('_'):
            continue
        if isinstance(v, dict) and ('index' in v or 'wage' in v):
            conv(v)
        elif isinstance(v, dict):   # paint.frame（内板骨格塗装）: {engine_room: {option, index, wage}, …} の入れ子（Codex 指摘）
            for x in v.values():
                if isinstance(x, dict) and ('index' in x or 'wage' in x):
                    conv(x)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict) and ('index' in x or 'wage' in x):
                    conv(x)
    fr = e.get('frame') or {}
    for x in (fr.get('items') or []) if isinstance(fr, dict) else []:
        if isinstance(x, dict):
            conv(x)
    if isinstance(fr, dict) and ('basic_index' in fr or 'basic_wage' in fr):   # 内板骨格の基本修正作業（basic_index / basic_wage）も 指数 × レート に（Codex 指摘）
        b = {'index': fr.get('basic_index'), 'wage': fr.get('basic_wage')}
        conv(b)
        if 'index' in b and _num(b.get('index')) > 0:
            fr['basic_index'] = b['index']
        if 'wage' not in b:
            fr.pop('basic_wage', None)
    return e, n_idx, n_fix


def _deltas(t0: dict, t1: dict) -> dict:
    if not t0 or not t1:
        return {}
    d = {k: int(t1.get(k) or 0) - int(t0.get(k) or 0) for k in ('parts', 'wage', 'paint', 'paint_material', 'frame')}
    d['paint_wage'] = d['paint'] - d['paint_material']
    return d


def other_wage(est: dict, rate: int) -> int:
    """塗装の追加項目（paint.other）の工賃（指数があれば 指数 × レート を丸めて）。印字の塗装工賃計（paint.total）には含まれない（判断規則 10-25）"""
    unit = int(_num(est.get('wage_round')) or 10)
    out = 0
    for o in ((est.get('paint') or {}).get('other') or []):
        if isinstance(o, dict):
            idx = _num(o.get('index'))
            out += _round_wage(idx * rate, unit) if idx > 0 and 'wage' not in o else int(_num(o.get('wage')))
    return out


def rate_option(pr: Prober, est: dict, s_target: int, step: int = 10) -> Optional[dict]:
    """協定額の課税小計を超えない最大のレート（step 円刻み）と、その 1 段上"""
    rate0 = int(_num(est.get('labor_rate')))
    if not rate0:
        return None
    e, n_idx, n_fix = index_only(est, rate0)
    if not n_idx:
        return None
    e['labor_rate'] = rate0

    last: dict = {}

    def s_of(r: int) -> int:
        if r not in last:   # 同じレートは試算し直さない
            e['labor_rate'] = r
            last[r] = pr.totals(e, r)
        return int(last[r]['subtotal'])
    s_now = s_of(rate0)
    lo = max(step, (rate0 // 4) // step * step)
    while lo > step and s_of(lo) > s_target:     # 範囲を決め打ちしない: 届くところまで下げる（Codex 指摘）
        lo = max(step, (lo // 2) // step * step)
    hi = max(lo + step, (rate0 * 3) // step * step)
    while s_of(hi) <= s_target and hi < rate0 * 50:   # hi は目標を「超える」側（ちょうど合うレートを under に入れるため。Codex 指摘）
        hi = (hi * 2) // step * step
    base = {'rate0': rate0, 'n_index': n_idx, 'n_fixed': n_fix, 's_now': s_now}
    if s_of(lo) > s_target or s_of(hi) < s_target:
        return dict(base, reachable=False, s_min=s_of(lo), s_max=s_of(hi))
    if s_of(hi) == s_target:   # 上限でちょうど合う（ほぼ無いが、境界を取りこぼさない）
        return dict(base, reachable=True, rate_under=hi, s_under=s_of(hi), rest_under=0, totals_under=_reading_totals(last.get(hi) or {}),
                    other_under=other_wage(e, hi), deltas=dict(_deltas(last.get(rate0) or {}, last.get(hi) or {}), other=other_wage(e, hi) - other_wage(e, rate0)),
                    rate_over=None, s_over=None, rest_over=None)
    a, b = lo // step, hi // step          # s_of(a*step) <= s_target < s_of(b*step) を保つ二分探索
    while b - a > 1:
        m = (a + b) // 2
        if s_of(m * step) <= s_target:
            a = m
        else:
            b = m
    r_under, r_over = a * step, b * step
    s_under, s_over = s_of(r_under), s_of(r_over)
    return {'rate0': rate0, 'n_index': n_idx, 'n_fixed': n_fix, 's_now': s_now, 'reachable': True,
            'rate_under': r_under, 's_under': s_under, 'rest_under': s_target - s_under, 'totals_under': _reading_totals(last.get(r_under) or {}),
            'other_under': other_wage(e, r_under),
            # 今のレートからの増減（生成器どうしの差）。reading の合計欄・paint.total は「今の値 + 増減」で直す（追加項目を塗装工賃計に数えるかは
            # 書式で違う: コグニ印刷は含めない・技術料だけの書式は含める。reading と同じ数え方のまま直すため。Codex 指摘の二重計上対策）
            'deltas': dict(_deltas(last.get(rate0) or {}, last.get(r_under) or {}), other=other_wage(e, r_under) - other_wage(e, rate0)),
            'rate_over': r_over, 's_over': s_over, 'rest_over': s_target - s_over}


def _reading_totals(t: dict) -> dict:
    """生成器の合計 → reading の totals の書き方（wage = 作業計、paint = 塗装工賃計、material = 材料計、paint_total = 塗装計（材料込））"""
    if not t:
        return {}
    mat = int(t.get('paint_material') or 0)
    return {'parts': int(t.get('parts') or 0), 'wage': int(t.get('wage') or 0), 'paint': int(t.get('paint') or 0) - mat, 'material': mat,
            'paint_total': int(t.get('paint') or 0), 'frame': int(t.get('frame') or 0)}   # 内板骨格の工賃もレートで変わる（Codex 指摘）


def index_options(est: dict, delta: int, rate: int, rows: int = 5, wage_round: int = 10) -> list[dict]:
    """工賃の大きい行から、0.1 刻みで何段動かすと課税小計の差 delta に届くか。届かない残りは材料代・塗装一式で"""
    if not rate or not delta:
        return []

    def rw(x: float) -> int:
        return _round_wage(x, wage_round)
    cands = [it for it in (est.get('items') or []) if isinstance(it, dict) and _num(it.get('index')) > 0 and _num(it.get('wage')) > 0]
    cands.sort(key=lambda it: -_num(it.get('wage')))
    out = []
    sign = -1 if delta < 0 else 1
    for it in cands[:rows]:
        idx0, w0 = round(_num(it.get('index')), 2), int(_num(it.get('wage')))
        best = None
        for k in range(1, int(idx0 * 10) + 60):
            idx1 = round(idx0 + sign * 0.1 * k, 2)
            if idx1 <= 0 or (sign < 0 and idx1 < idx0 / 2) or (sign > 0 and idx1 > idx0 * 1.5):   # 1 行で半分以下・1.5 倍超に動かす案は出さない（行を組み合わせる）
                break
            dw = rw(idx1 * rate) - rw(idx0 * rate)
            if abs(dw) > abs(delta):        # これ以上動かすと行き過ぎる
                break
            best = (k, idx1, dw)
        if best is None:
            continue
        k, idx1, dw = best
        if abs(delta - dw) >= abs(delta):   # 動かしても差が縮まらない
            continue
        out.append({'name': it.get('name'), 'code': it.get('code'), 'method': it.get('method') or '手入力', 'index': idx0, 'wage': w0,
                    'steps': k, 'index_new': idx1, 'wage_new': w0 + dw, 'change': dw, 'rest': delta - dw})
    return out


def analyse(case: str, target: int, step: int = 10, rows: int = 5) -> dict:
    est = json.load(open(os.path.join(case, 'estimate.json'), encoding='utf-8'))
    pr = Prober()
    tt = pr.totals(est)
    s0 = int(tt['subtotal'])
    taxfree = _taxfree(est)
    mode0 = str(est.get('tax_round') or '四捨五入')
    res = {'case': os.path.basename(case.rstrip('/\\')), 'target': target, 'now': {'taxable': s0, 'tax': int(tt['tax']), 'total': int(tt['total']),
           'parts': int(tt.get('parts') or 0), 'wage': int(tt.get('wage') or 0), 'paint': int(tt.get('paint') or 0),
           'material': int(tt.get('paint_material') or 0), 'frame': int(tt.get('frame') or 0)},
           'tax_round': mode0, 'taxfree': taxfree, 'taxable_for': {m: solve_taxable(target, taxfree, m) for m in TAX_MODES}}
    s_t = res['taxable_for'].get(mode0)
    mode = mode0
    if s_t is None:   # 今の丸めではちょうどにならない → 届く丸めを使う（コグニの消費税設定が変わる）
        mode = next((m for m in TAX_MODES if res['taxable_for'].get(m) is not None), mode0)
        s_t = res['taxable_for'].get(mode)
    res['mode'] = mode
    if s_t is None:
        res['error'] = '協定額にちょうど届く課税小計が無い'
        return res
    delta = s_t - s0
    res['taxable_target'] = s_t
    res['delta'] = delta
    paint = est.get('paint') or {}
    detailed = bool(paint.get('panels')) or is_bumper_only_paint(paint)
    mat = int(tt.get('paint_material') or 0)
    opts = {}
    if detailed:
        opts['material'] = {'ok': mat + delta > 0, 'material_now': mat, 'material_new': mat + delta,
                            'printed': _num(paint.get('material')) > 0 and not paint.get('_material_from_target')}
    elif paint.get('total'):
        pt = int(_num(paint.get('total')))
        opts['paint'] = {'ok': pt + delta > 0, 'total_now': pt, 'total_new': pt + delta}
    r = rate_option(pr, est, s_t, step)
    if r:
        opts['rate'] = r
    rate = int(_num(est.get('labor_rate')))
    opts['index'] = index_options(est, delta, rate, rows, int(_num(est.get('wage_round')) or 10))
    fr = est.get('frame') if isinstance(est.get('frame'), dict) else {}
    # frame.basic は基本修正作業の有無（真偽値。省略 = あり）。「骨格基本指数を使わずに鈑金に振る」は basic を false にする（2026-09-14 JPN タクシー）
    if fr and str(fr.get('basic', True)).strip().lower() not in ('false', '0', 'no', ''):
        e2 = copy.deepcopy(est)
        e2['frame'] = dict(fr, basic=False)
        try:
            s_nb = int(pr.totals(e2)['subtotal'])
            opts['frame'] = {'basic_index': fr.get('basic_index'), 'change': s_nb - s0, 'rest': s_t - s_nb}
        except Exception as ex:  # noqa: BLE001
            opts['frame'] = {'error': f'{type(ex).__name__}: {ex}'}
    res['options'] = opts
    try:   # reading に追加項目（paint.other）を別の欄で書いているか（コグニ印刷の書式。印字の塗装工賃計に含めない）
        _rd = json.load(open(os.path.join(case, 'reading.json'), encoding='utf-8-sig'))
        other_explicit = bool(((_rd.get('paint') or {}).get('other')))
    except (OSError, ValueError):
        other_explicit = False
    res['reading_now'] = {'totals': {k: v for k, v in (est.get('totals') or {}).items() if k in ('wage', 'paint', 'material', 'paint_total', 'frame')},
                          'paint_total_field': paint.get('total'), 'other_explicit': other_explicit}
    res['probes'] = pr.n
    return res


def rate_fixes(now: dict, d: dict) -> list:
    """レート変更で直す reading の欄: [(欄, 今, 協定後)]。reading に書かれている欄だけ。
    追加項目（paint.other）を reading に別の欄で書いている書式（コグニ印刷）では、印字の塗装工賃計（paint.total・totals.paint）に追加項目を
    含めないので、その増減は外す（塗装行から下書きが追加項目にした書式は、印字の塗装工賃計に含むのでそのまま。Codex 指摘）"""
    if not d:
        return []
    out = []
    t = now.get('totals') or {}
    d_pw = int(d.get('paint_wage') or 0) - (int(d.get('other') or 0) if now.get('other_explicit') else 0)
    for key, dv in (('wage', d.get('wage')), ('paint', d_pw), ('material', d.get('paint_material')), ('paint_total', d.get('paint')), ('frame', d.get('frame'))):
        if t.get(key) not in (None, '') and dv:
            out.append((f'totals.{key}', int(_num(t[key])), int(_num(t[key])) + int(dv)))
    if now.get('paint_total_field') not in (None, '') and d_pw:
        out.append(('paint.total', int(_num(now['paint_total_field'])), int(_num(now['paint_total_field'])) + d_pw))
    return out


def _big(now: int, new: int) -> str:
    """元の半分未満・1.5 倍超に動く案に付ける印（協定額が工場見積から離れていて、その方法だけでは不自然）"""
    if now > 0 and (new < now * 0.5 or new > now * 1.5):
        return f'  ★ 元の {new * 100 // now}% になる。この方法だけで合わせるのは不自然（損保の指示を確かめる・ほかの方法と組み合わせる）'
    return ''


def report(res: dict, method: str = '') -> str:
    L = []
    n = res['now']
    L.append(f"協定額 {res['target']:,} 円（税込）/ 今の合計 {n['total']:,} 円（課税小計 {n['taxable']:,} ＋ 消費税 {n['tax']:,}、{res['tax_round']}）")
    if res.get('error'):
        L.append('★ ' + res['error'])
        return '\n'.join(L)
    tf = res['taxable_for']
    L.append('協定額に届く課税小計: ' + ' / '.join(f"{m} {tf[m]:,}" if tf[m] is not None else f'{m} なし' for m in TAX_MODES))
    if res['mode'] != res['tax_round']:
        L.append(f"★ 消費税 {res['tax_round']} では 1 円単位でちょうどにならない → reading に \"tax_round\": \"{res['mode']}\" を書く"
                 '（NEO の消費税設定が工場と変わる。報告に書く）')
    d = res['delta']
    L.append(f"動かす額（課税小計）: {d:+,} 円（{res['taxable_target']:,} − {n['taxable']:,}）")
    o = res.get('options') or {}
    L.append('')
    if 'rate' in o and (not method or method == 'rate'):
        r = o['rate']
        L.append(f"■ レバーレートで（指数のある行 {r['n_index']} 行を 指数 × 新レート。金額だけの工賃 {r['n_fixed']} 行はそのまま）")
        if not r.get('reachable'):
            if r.get('s_min', 0) > res['taxable_target']:
                L.append(f"   レートだけでは届かない（レートを下げきっても課税小計 {r['s_min']:,}。部品・費用・金額だけの工賃で協定額を超えている）")
            else:
                L.append('   レートだけでは届かない（今のレートの 50 倍まで上げても協定額に届かない）')
        else:
            L.append(f"   {r['rate_under']:,} 円 → 課税小計 {r['s_under']:,}（残り {r['rest_under']:+,} 円）"
                     + (f" / {r['rate_over']:,} 円 → {r['s_over']:,}（残り {r['rest_over']:+,} 円）" if r.get('rate_over') else '')
                     + ('  ← レートだけでちょうど合う' if r['rest_under'] == 0 else ''))
            L.append(f"   書き方: reading の \"labor_rate\": {r['rate_under']}、行・塗装は index だけ（wage を消す）。残りは下の材料代／塗装一式の方法で（target_total）")
            tu = r.get('totals_under') or {}
            fix = rate_fixes(res.get('reading_now') or {}, r.get('deltas') or {})
            if fix:
                L.append('   合計欄も協定後に（今の値 + レート変更による増減。課税小計・消費税・合計は target_total が決める。印字の値は note に残す。直さないと紙上検算が止まる）: '
                         + '・'.join(f'{k} {a:,} → {b:,}' for k, a, b in fix))
    if o.get('index') and (not method or method == 'index'):
        L.append('■ 指数で（損保が指定した行だけを 0.1 刻みで。工賃の大きい順の候補。1 行で半分以下にはしない＝届かなければ残りを別の行・材料代で）')
        for x in o['index']:
            L.append(f"   {x['code'] or '----'} {x['name']}（{x['method']}）: 指数 {x['index']} → {x['index_new']}（{x['steps']} 段）"
                     f" 工賃 {x['wage']:,} → {x['wage_new']:,}（{x['change']:+,}）残り {x['rest']:+,} 円")
        L.append('   書き方: その行の index と wage を新しい値に、ページ小計・ブロック小計・合計欄の工賃計（totals.wage）も協定後に直す（判断規則 10-14）。残りは target_total で')
    if 'material' in o and (not method or method in ('material', 'index', 'rate')):
        m = o['material']
        L.append(f"■ 塗装材料代で（パネル明細の塗装）: 材料代 {m['material_now']:,} → {m['material_new']:,}" + ('' if m['ok'] else '  ★ 0 以下になるので不可')
                 + (_big(m['material_now'], m['material_new']) if m['ok'] else ''))
        L.append(f"   書き方: reading の直下に \"target_total\": {res['target']}" + ('、印字の材料代を動かすので "target_total_replaces_material": true' if m['printed'] else ''))
    if 'paint' in o and (not method or method in ('paint', 'index', 'rate')):
        p = o['paint']
        L.append(f"■ 塗装一式で: 塗装 {p['total_now']:,} → {p['total_new']:,}" + ('' if p['ok'] else '  ★ 0 以下になるので不可')
                 + (_big(p['total_now'], p['total_new']) if p['ok'] else ''))
        L.append(f"   書き方: reading の直下に \"target_total\": {res['target']}, \"target_adjust\": \"paint\"")
    if method == 'material' and 'material' not in o:
        L.append('■ 塗装材料代で: この見積の塗装は一式（パネル明細なし）なので材料代では合わせられない。塗装で調整するなら --method paint（target_adjust: "paint"）')
    if method == 'paint' and 'paint' not in o:
        L.append('■ 塗装一式で: この見積の塗装はパネル明細（または塗装一式の額が無い）なので一式の額では合わせられない。材料代で合わせるなら --method material')
    if method == 'index' and not o.get('index'):
        L.append('■ 指数で: 指数と工賃のある明細の行が無いか、1 行を半分以下・1.5 倍超にしないと届かない。行を組み合わせるか、ほかの方法を損保に確かめる')
    if method == 'frame' and 'frame' not in o:
        L.append('■ 内板骨格の基本指数を外すと: この見積に内板骨格の基本修正作業が無い（frame が無いか basic: false）')
    if method == 'rate' and 'rate' not in o:
        L.append('■ レバーレートで: 指数のある行が無い（技術料だけで割り切れない書式）かレートが無い。reading の行を index にしてから')
    if 'frame' in o and (not method or method == 'frame'):
        f = o['frame']
        if f.get('error'):
            L.append(f"■ 内板骨格の基本指数を外すと: 試算できない（{f['error']}）")
        else:
            L.append(f"■ 内板骨格の基本指数を外すと: 課税小計 {f['change']:+,} 円（残り {f['rest']:+,} 円）。鈑金に振るなら、残りを指定の鈑金行の指数で（上の「指数で」）")
            L.append('   書き方: reading の frame に "basic": false（基本修正作業なし）。鈑金に振った行は index と wage・小計を直す（判断規則 10-14）')
    L.append('')
    L.append('方法は損保の指示で決める。指示の無い行を合計合わせに動かさない。決めたら reading を直して make_neo を再実行し、検算が協定額で合うことを確かめる')
    return '\n'.join(L)


def main(argv: list) -> int:
    skill_env.use_utf8_io()
    ap = argparse.ArgumentParser(description='協定額に合わせる調整の候補を計算する（読むだけ）')
    ap.add_argument('case', help='案件フォルダ（NEO_check 配下。estimate.json がある）')
    ap.add_argument('--target', required=True, help='協定額（税込）。1,150,000 のようにカンマ付きでも可')
    ap.add_argument('--method', default='', choices=['', 'rate', 'index', 'material', 'paint', 'frame'], help='損保の指示の方法だけ出す')
    ap.add_argument('--rows', type=int, default=5, help='「指数で」の候補の行数')
    ap.add_argument('--step', type=int, default=10, help='レートの刻み（円）')
    ap.add_argument('--json', default='', help='結果を JSON で保存する先（NEO_check の案件フォルダなど、git の外）')
    a = ap.parse_args(argv)
    target = int(float(str(a.target).replace(',', '')))
    if not os.path.exists(os.path.join(a.case, 'estimate.json')):
        print('estimate.json が無い。先に工場見積そのままで make_neo を合格させる')
        return 1
    res = analyse(a.case, target, a.step, a.rows)
    print(report(res, a.method))
    if a.json:
        json.dump(res, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return 0 if not res.get('error') else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
