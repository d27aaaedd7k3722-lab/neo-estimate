# -*- coding: utf-8 -*-
"""協定額に合わせる調整（判断規則 10-14）の単体テスト: 消費税の丸めで届く課税小計、指数の候補、塗装一式の自動調整（target_adjust: paint）、
候補計算（agree_calc.analyse）、make_neo の協定額の関門。ADDATA に S89 が無い PC では車種を使うテストを飛ばす
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_agree_calc.py
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import skill_env  # noqa: E402

skill_env.apply()
import agree_calc as ac  # noqa: E402
import draft_estimate as de  # noqa: E402

S89 = {'model_code': '5AA-MK94S', 'serial_no': 'MK94S-300001', 'desig': '20824', 'category': '0001', 'reg_date': 'R7.12', 'color_code': 'WBW'}
ROWS = ['|ﾊﾟﾈﾙ ﾌﾛﾝﾄﾌｰﾄﾞ|取替|||1|30700|3048||', '|ｸﾞﾘﾙ ﾗｼﾞｴｰﾀ|取替|||1|19800|0||', '|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||']
# 部品 73,500 + 工賃 6,858 + 塗装一式 20,000 + 材料 5,000 = 105,358、税 10,536、合計 115,894
READING = {'source': 't', 'issuer': 't', 'est_date': '20260823', 'format': 'B', 'vehicle': dict(S89), 'customer': {}, 'insurance': {}, 'labor_rate': 7620,
           'blocks': [{'title': '鈑金・塗装', 'rows': ROWS}], 'paint': {'paint': '2K', 'coat': 'メタリック', 'hf': 'しない', 'total': 20000, 'material': 5000},
           'expenses': [], 'totals': {'parts': 73500, 'wage': 6858, 'paint': 20000, 'material': 5000, 'paint_total': 25000,
                                      'taxable': 105358, 'tax': 10536, 'total': 115894}}


def _s89_available() -> bool:
    try:
        return de.Drafter(dict(READING, blocks=[])).car.get('CarCode') == 'S89'
    except Exception:  # noqa: BLE001  ADDATA に車種が無い・読めない
        return False


def _draft(rd: dict) -> dict:
    with contextlib.redirect_stdout(io.StringIO()):
        return de.Drafter(copy.deepcopy(rd)).build()


def test_taxable_for_target():
    assert ac.tax_of(1045455, '四捨五入') == 104546 and ac.tax_of(1045455, '切り捨て') == 104545 and ac.tax_of(1045454, '切り上げ') == 104546
    assert ac.solve_taxable(1150000, 0, '四捨五入') is None                     # 四捨五入ではちょうどにならない（JPN タクシー）
    assert ac.solve_taxable(1150000, 0, '切り捨て') == 1045455
    assert ac.solve_taxable(725000, 0, '四捨五入') == 659091
    assert ac.solve_taxable(725000 + 3000, 3000, '四捨五入') == 659091           # 非課税費用は課税小計の外


def test_index_options():
    est = {'items': [{'name': 'A', 'code': '0010', 'method': '取替', 'index': 5.0, 'wage': 38100},
                     {'name': 'B', 'code': '0020', 'method': '板金', 'index': 1.0, 'wage': 7620},
                     {'name': 'C', 'code': '0030', 'method': '取替', 'index': 0.0, 'wage': 0}]}
    opts = ac.index_options(est, -3000, 7620)
    a = next(o for o in opts if o['name'] == 'A')
    assert a['steps'] == 3 and a['index_new'] == 4.7 and a['change'] == -2290 and a['rest'] == -710, a   # 0.1 × 7,620 = 762（10 円丸め 760）× 3
    assert all(o['name'] != 'C' for o in opts)
    assert all(o['index_new'] >= o['index'] / 2 for o in opts)                  # 1 行で半分以下にしない
    assert ac.index_options(est, 0, 7620) == [] and ac.index_options(est, -3000, 0) == []


def test_index_only_converts_rounded_wages_and_frame_paint():
    """工賃だけの行は工賃の丸め（10 円）込みで指数に戻す（1.2 × 7,960 = 9,552 → 9,550）。塗装の中の内板骨格塗装（paint.frame の入れ子）も指数 × レートに"""
    est = {'labor_rate': 7960, 'wage_round': 10,
           'items': [{'name': 'A', 'wage': 9550}, {'name': 'B', 'wage': 12345}, {'name': 'C', 'index': 2.0, 'wage': 15920}],
           'paint': {'frame': {'engine_room': {'option': 1, 'index': 1.4, 'wage': 11140}}, 'panels': [{'code': '0800', 'index': 1.8, 'wage': 14330}]}}
    e, n_idx, n_fix = ac.index_only(est, 7960)
    a, b, c = e['items']
    assert a.get('index') == 1.2 and 'wage' not in a, a
    assert 'index' not in b and b['wage'] == 12345 and n_fix == 1, b          # 割り切れない金額はそのまま
    assert 'wage' not in c and 'wage' not in e['paint']['frame']['engine_room'] and 'wage' not in e['paint']['panels'][0]
    assert n_idx == 4, n_idx
    est2 = {'labor_rate': 7820, 'wage_round': 10, 'items': [], 'frame': {'basic': True, 'basic_index': 3.5, 'basic_wage': 27370},
            'paint': {'other': [{'name': 'ﾌﾟﾗｲﾏｰ', 'index': 0.5, 'wage': 3910}, {'name': 'ｺｰﾃｨﾝｸﾞ', 'wage': 1234}]}}
    e2, _, _ = ac.index_only(est2, 7820)
    assert e2['frame']['basic_index'] == 3.5 and 'basic_wage' not in e2['frame'], e2['frame']   # 骨格の基本工賃も 指数 × 新レート に
    assert ac.other_wage(e2, 7000) == 3500 + 1234                                  # 追加項目: 指数 0.5 × 7,000 と金額だけの 1,234


def test_target_adjust_paint_in_draft():
    """塗装一式の見積で target_adjust: paint なら、塗装一式の額で協定額に合わせる。パネル明細・不明な方法は断る"""
    if not _s89_available():
        print('   skip test_target_adjust_paint_in_draft（この ADDATA に S89 が無い）')
        return
    rd = dict(copy.deepcopy(READING), target_total=110000, target_adjust='paint')
    est = _draft(rd)
    assert est['totals']['total'] == 110000 and est['paint']['total'] < 20000, (est['totals'], est['paint'])
    assert est['totals']['taxable'] + est['totals']['tax'] == 110000
    rd2 = dict(copy.deepcopy(READING), target_total=110000)                      # 方法を書かない（塗装一式なので材料代では調整しない）
    est2 = _draft(rd2)
    assert est2['paint']['total'] == 20000 and any('target_adjust' in n for n in est2.get('_draft_notes') or [])
    rd3 = dict(copy.deepcopy(READING), target_total=110000, target_adjust='wage')
    assert any('使えない' in n for n in _draft(rd3).get('_draft_notes') or [])


def test_analyse_lists_rate_and_paint_options():
    if not _s89_available():
        print('   skip test_analyse_lists_rate_and_paint_options（この ADDATA に S89 が無い）')
        return
    d = tempfile.mkdtemp(prefix='agree_')
    try:
        est = _draft(READING)
        json.dump(est, open(os.path.join(d, 'estimate.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        res = ac.analyse(d, 113000)
        assert res['now']['total'] == 115894, res['now']
        o = res['options']
        assert o['paint']['total_new'] == 20000 + res['delta'], o['paint']
        r = o['rate']
        assert r['reachable'] and r['s_under'] <= res['taxable_target'] < r['s_over'] and r['rate_under'] < 7620, r
        assert r['totals_under']['wage'] < 6858
        txt = ac.report(res)
        assert 'target_adjust' in txt and 'labor_rate' in txt
        assert 'frame' in r['totals_under']
        fx = dict((k, (a, b)) for k, a, b in ac.rate_fixes(res['reading_now'], r['deltas']))
        assert fx['totals.wage'][1] == r['totals_under']['wage'] and 'paint.total' not in fx, fx   # 塗装一式（金額）はレートで変わらない
        got = ac.rate_fixes({'totals': {'paint': 99000}, 'paint_total_field': 99000}, {'paint_wage': -10600, 'wage': 0, 'paint': -10600, 'paint_material': 0, 'frame': 0})
        assert got == [('totals.paint', 99000, 88400), ('paint.total', 99000, 88400)], got   # 今の値 + 増減（追加項目の数え方は reading のまま）
        got2 = ac.rate_fixes({'totals': {'paint': 99000}, 'paint_total_field': 99000, 'other_explicit': True},
                             {'paint_wage': -10600, 'other': -1300, 'wage': 0, 'paint': -10600, 'paint_material': 0, 'frame': 0})
        assert got2 == [('totals.paint', 99000, 89700), ('paint.total', 99000, 89700)], got2   # 追加項目を別欄に書く書式は、その増減を塗装工賃計に入れない
        far = ac.analyse(d, 300000)                                                 # 今のレートの 3 倍を超えるレートでないと届かない額でも探しにいく
        assert far['options']['rate']['reachable'] and far['options']['rate']['rate_under'] > 7620 * 3, far['options']['rate']
        exact = ac.analyse(d, r['s_under'] + ac.tax_of(r['s_under'], '四捨五入'))    # レートだけでちょうど合う額なら、そのレートを under に（1 段下にしない）
        re_ = exact['options']['rate']
        assert re_['rest_under'] == 0 and re_['rate_under'] == r['rate_under'], re_
        assert '--method paint' in ac.report(res, 'material') and 'material' not in res['options']    # 使えない方法を指定したら理由を出す
        low = ac.analyse(d, 50000)                                                  # 部品だけで超える額はレートでは届かない
        assert not low['options']['rate']['reachable'] and '下げきっても' in ac.report(low, 'rate')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_make_neo_rejects_neo_not_at_target():
    """reading に target_total があるのに NEO の合計がその額でなければ make_neo は不合格。方法を書けば合格"""
    if not _s89_available():
        print('   skip test_make_neo_rejects_neo_not_at_target（この ADDATA に S89 が無い）')
        return
    d = tempfile.mkdtemp(prefix='agree_mk_')
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    try:
        rd = dict(copy.deepcopy(READING), target_total=110000)
        json.dump(rd, open(os.path.join(d, 'reading.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        cp = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'make_neo.py'), d, '--name', 't', '--no-profile'], env=env, capture_output=True, text=True, encoding='utf-8')
        assert cp.returncode == 1 and '協定額' in cp.stdout and not os.path.exists(os.path.join(d, 't.neo')), cp.stdout[-2000:]
        rd['target_adjust'] = 'paint'
        rd['target_total'] = '110,000円'                                          # 印字どおりの書き方でも読む（draft と同じ）
        json.dump(rd, open(os.path.join(d, 'reading.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        cp = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'make_neo.py'), d, '--name', 't', '--no-profile', '--force-draft'], env=env, capture_output=True, text=True, encoding='utf-8')
        assert cp.returncode == 0 and os.path.exists(os.path.join(d, 't.neo')), cp.stdout[-3000:]
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                fails += 1
                print('FAIL', name, e)
    print('agree_calc tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
