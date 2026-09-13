# -*- coding: utf-8 -*-
"""装備（EVA）と 15.DB の枠の取り合いの単体テスト（コグニ実機 2026-09-08、J87 N-BOX。NEO_check/_eva_exp の cogni_*.neo が根拠）
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_eva_slot.py
車両: J87 グレード B / FVA A / ボディ 10 / 年式 01（C06 N-BOX と同じ）。ADDATA が必要。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from estimate_to_neo import AddataParts, NeoBuilder  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': ''}
ITEMS = [{'code': '0400', 'name': 'L ﾍｯﾄﾞﾗｲﾄ', 'method': '取替', 'qty': 1},
         {'code': '0402', 'name': 'L ﾍｯﾄﾞﾗｲﾄﾕﾆｯﾄ', 'method': '取替', 'qty': 1},
         {'code': '0408', 'name': 'L ﾍｯﾄﾞﾗｲﾄﾊﾞﾙﾌﾞ', 'method': '取替', 'qty': 1},
         {'code': '0140', 'name': 'LFﾌｫｸﾞﾗｲﾄ', 'method': '取替', 'qty': 1},
         {'code': '2700', 'name': 'L ｽﾗｲﾄﾞﾄﾞｱﾊﾟﾈﾙ', 'method': '取替', 'qty': 1},
         {'code': '3500', 'name': 'R ｽﾗｲﾄﾞﾄﾞｱﾊﾟﾈﾙ', 'method': '取替', 'qty': 1},
         {'code': '6000', 'name': 'Fｳｲﾝﾄﾞｼｰﾙﾄﾞｶﾞﾗｽ', 'method': '取替', 'qty': 1}]
# 取替合計（ChangeTotalOutTax）の期待値: 枠を取られた行は「枠を取った行の指数」で標準工賃が入る（cogni_pair_U 0400 = 43,000 + 0.4h、cogni_pair_none 0402 = 57,600 + 0.3h）
CT_EXPECT = {(): {'0400': 45400, '0402': 60000, '0408': 2000, '0140': 14800}, ('U',): {'0400': 46200, '0402': 60800, '0408': 13000}, ('T',): {'0140': 27400, '0400': 45400}, ('T', 'U'): {'0140': 27400, '0408': 13000, '0400': 46200}}
# コグニ実機で保存した NEO（cogni_pair_none / cogni_pair_U / cogni_pair_P / cogni_pair_Q / cogni_T / cogni_TU）から写した期待値
EXPECT = {
    (): {'0400': ('33150-TY0-N11', 43000, 0.3), '0402': ('33151-TY0-J11', 57600, -1), '0408': ('33115-SV4-G01', 2000, -1), '0140': ('33950-TY0-003', 14800, -1),
         '2700': ('67550-TY0-405ZZ', 69600, 2.5), '3500': ('67510-TY0-405ZZ', 69600, 2.5), '6000': ('73111-TY0-000', 119200, 2.9)},
    ('U',): {'0400': ('33150-TY0-N11', 43000, -1), '0402': ('33151-TY0-J11', 57600, 0.4), '0408': ('33116-S0A-J11', 13000, -1)},
    ('P',): {'2700': ('67550-TY0-309ZZ', 69600, 2.8), '3500': ('67510-TY0-406ZZ', 69600, 2.5), '0400': ('33150-TY0-N11', 43000, 0.3)},
    ('Q',): {'2700': ('67550-TY0-309ZZ', 69600, 2.8), '3500': ('67510-TY0-309ZZ', 69600, 2.8)},
    ('T',): {'0140': ('33950-TY0-J01', 27400, -1), '0400': ('33150-TY0-N11', 43000, 0.3)},
    ('T', 'U'): {'0140': ('33950-TY0-J01', 27400, -1), '0408': ('33116-S0A-J11', 13000, -1), '0400': ('33150-TY0-N11', 43000, -1)},
}


def main() -> int:
    nb = NeoBuilder()
    fails = 0
    for eva, exp in EXPECT.items():
        est = {'source': 'unit_eva_slot', 'issuer': '', 'est_date': '20260908', 'vehicle': VEH, 'customer': {}, 'insurance': {}, 'labor_rate': 8000,
               'items': ITEMS, 'paint': {}, 'expenses': [], 'totals': {}, 'hints': {'eva_codes': list(eva)} if eva else {}}
        _, rep = nb.build(est, VEH, hints=est.get('hints'), labor_rate=8000, est_date='20260908', insurance={})
        assert rep['car']['CarCode'] == 'J87', rep['car']
        rows = {r['PartsCode']: r for r in rep['rows']}
        for code, (pn, price, time) in exp.items():
            r = rows[code]
            got = (r['PartsNo'], int(r['PartsPriceOutTax']), float(r['Time']))
            ok = got == (pn, price, time)
            fails += 0 if ok else 1
            print(('ok  ' if ok else 'FAIL'), f"eva={''.join(eva) or '-'} {code}: got {got} expected {(pn, price, time)}")
        for code, ct in CT_EXPECT.get(eva, {}).items():
            got_ct = int(rows[code]['ChangeTotalOutTax'])
            fails += 0 if got_ct == ct else 1
            print(('ok  ' if got_ct == ct else 'FAIL'), f"eva={''.join(eva) or '-'} {code} ChangeTotal: got {got_ct} expected {ct}")
    print('unit_eva_slot:', 'all ok' if not fails else f'{fails} failed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
