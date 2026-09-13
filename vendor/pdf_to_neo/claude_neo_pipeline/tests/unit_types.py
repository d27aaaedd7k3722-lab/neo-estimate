# -*- coding: utf-8 -*-
"""estimate.json を手で書いたときの型ゆれ（カンマ付き文字列・数値の部品コード・全角）の扱い。
「静かに違う結果」ではなく「正しく解釈」か「どこが悪いか分かるエラー」のどちらかになること
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_types.py
"""
from __future__ import annotations

import copy
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from estimate_to_neo import NeoBuilder, _code4, _money  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': 'YR586P'}
BASE = {
    'source': 'unit_types', 'issuer': '', 'est_date': '20260909', 'vehicle': VEH, 'customer': {}, 'insurance': {},
    'labor_rate': 8000,
    'items': [{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'parts_no': '71101-TY0-000ZS', 'qty': 1, 'price': 59100, 'wage': 6620, 'index': 0.83}],
    'paint': {}, 'expenses': [], 'totals': {},
}


def build(est, rate=8000):
    return NeoBuilder().build(copy.deepcopy(est), VEH, labor_rate=rate, est_date='20260909', insurance={})[1]


def test_code4():
    assert _code4(10) == '0010'
    assert _code4('10') == '0010'
    assert _code4('0010') == '0010'
    assert _code4(10.0) == '0010'
    assert _code4(None) == ''
    for blank in ('', '  ', '　'):  # 空白だけの欄は「指定なし」
        assert _code4(blank) == '', repr(blank)
    assert _code4('００１０') == '0010', '全角の部品コードも受ける'
    assert _code4('00010') == '0010', '先頭ゼロ付きの 5 文字も 4 桁にする'
    assert _code4('０００１０') == '0010'
    for bad in (10.5, '10.5', '１０．５', '12OOO', '-1', '12,9', 'A05', -1, -1.0, 10000, '10000', '１００００'):  # 下流が非数字を落として別の部品になる前に止める
        try:
            _code4(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f'小数の部品コード {bad!r} が通ってしまう')


def test_money_names_the_field():
    try:
        _money('12OOO', '明細 X の部品代')
    except ValueError as e:
        assert '明細 X の部品代' in str(e), e
    else:
        raise AssertionError('数値でない金額が通ってしまう')


def test_comma_string_price_is_read():
    base = build(BASE)
    est = copy.deepcopy(BASE)
    est['items'][0]['price'] = '59,100'
    assert build(est)['totals']['parts'] == base['totals']['parts']


def test_comma_string_labor_rate_is_read():
    est = copy.deepcopy(BASE)
    assert build(est, '8,000')['totals']['wage'] == build(est, 8000)['totals']['wage']


def test_numeric_code_is_read():
    base = build(BASE)
    est = copy.deepcopy(BASE)
    est['items'][0]['code'] = 10
    assert build(est)['rows'][0]['PartsCode'] == base['rows'][0]['PartsCode']


def test_bool_qty_is_error():
    est = copy.deepcopy(BASE)
    est['items'][0]['qty'] = True
    try:
        build(est)
    except ValueError as e:
        assert '数量' in str(e), e
    else:
        raise AssertionError('真偽値の数量が通ってしまう')


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
    print('unit_types:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
