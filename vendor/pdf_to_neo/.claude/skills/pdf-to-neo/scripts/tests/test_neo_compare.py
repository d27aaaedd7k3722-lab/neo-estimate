# -*- coding: utf-8 -*-
"""納品した NEO と最後に使われた NEO の答え合わせ（neo_compare.py）の単体テスト（明細の dict だけで確かめる。ADDATA は要らない）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_neo_compare.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import neo_compare as nc  # noqa: E402


def R(no, name, pn='', qty=1, price=0, wage=-1, code='', disp=0, reserve=0):
    return {'RecordNo': no, 'PartsCode': code, 'DisposalCode': disp, 'PartsName': name, 'PartsNo': pn, 'PartsCount': qty,
            'PartsPriceOutTax': price, 'WageOutTax': wage, 'ReserveFlag': reserve}


def test_same_amount_rows_pair_by_name():
    """同じ金額の作業行が 2 つあり片方が削られたとき、名称の近い方と揃えて、削られた方を「納品の側だけ」に出す"""
    mine = [R(1, '燃料誤給油点検', qty=-1, wage=25200), R(2, 'ﾃｽﾄ走行(修理後)', qty=-1, wage=25200)]
    other = [R(1, 'ﾃｽﾄ走行(後)', qty=-1, wage=25200)]
    res = nc.compare(mine, other)
    assert [r['name'] for r in res['only_mine']] == ['燃料誤給油点検'], res
    assert res['matched'] == 1 and not res['only_other'], res


def test_parts_no_difference_and_quantity():
    """金額で揃った行でも品番が違えば「品番の違い」。数量だけ違う行（金額は同じ）も挙げる。保留の行は数えない"""
    mine = [R(1, 'O-ﾘﾝｸﾞ', '028997144565', price=1980), R(2, 'ｸﾘｯﾌﾟ', '90467-07215', qty=2, price=180, code='0100'),
            R(3, 'ﾌｰﾄﾞ', '53301-60B00', price=79100, wage=7200, code='0600'), R(4, '保留の行', 'X1', price=500, reserve=1)]
    other = [R(1, '0-ﾘﾝｸﾞ', '028997144564', price=1980), R(2, 'ｸﾘｯﾌﾟ', '', qty=1, price=180),
             R(3, 'ﾌｰﾄﾞ', '5330160B00', price=79100, wage=7200, code='0600')]
    res = nc.compare(mine, other)
    kinds = sorted(d['kind'] for d in res['diffs'])
    assert kinds == ['品番の違い', '数量の違い'], res['diffs']
    assert res['matched'] == 1 and res['rows'] == [3, 3], res        # ハイフンの有無は同じ品番
    assert res['style'].get('部品コード あり/なし') == 1, res['style']
    assert res['totals']['parts'] == [81260, 81260], res['totals']
    assert '品番の違い' in nc.report(res)


def test_added_rows_and_wage_placement():
    """協定で足した行は「相手の側だけ」、同じ部品金額で工賃の付いた行が違うものは「部品金額が同じ」の違いとして出す"""
    mine = [R(1, 'Frｽﾎﾟｲﾗ', '76851-60020-A2', price=60400, wage=18000), R(2, 'Frｽﾎﾟｲﾗｰ(ﾓﾃﾞﾘｽﾀ)', price=93000, wage=0)]
    other = [R(1, 'Frｽﾎﾟｲﾗｰ', price=60400, wage=0), R(2, 'Frｽﾎﾟｲﾗｰ (ﾓﾃﾞﾘｽﾀ)', price=93000, wage=18000), R(3, 'ｴｰﾐﾝｸﾞ 関係', qty=-1, wage=30000)]
    res = nc.compare(mine, other)
    assert [r['name'] for r in res['only_other']] == ['ｴｰﾐﾝｸﾞ 関係'], res
    assert len(res['diffs']) == 2 and all(d['kind'] == '部品金額が同じ' for d in res['diffs']), res['diffs']
    assert res['totals']['wage'] == [18000, 48000], res['totals']


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
    print('neo_compare tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
