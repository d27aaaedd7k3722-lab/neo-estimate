# -*- coding: utf-8 -*-
"""手入力（'*' 工賃 / '#' 指数 / '*' 価格 / 修理方法空欄の手入力行）の単体テスト。
根拠 = コグニ実機で手入力して保存した NEO_check/_eva_exp/exp_manual.neo（2026-09-08、J87 N-BOX、装備 Q、レート 8,000）。
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_manual_rows.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from estimate_to_neo import NeoBuilder  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': ''}
ITEMS = [
    {'code': '0400', 'name': 'L ﾍｯﾄﾞﾗｲﾄ', 'method': '取替', 'qty': 1, 'price': 45000},          # 標準 43,000 に手入力価格
    {'code': '2700', 'name': 'L ｽﾗｲﾄﾞﾄﾞｱﾊﾟﾈﾙ', 'method': '取替', 'qty': 1, 'wage': 25000},      # 標準 2.8h/22,400 に手入力工賃
    {'code': '3500', 'name': 'R ｽﾗｲﾄﾞﾄﾞｱﾊﾟﾈﾙ', 'method': '取替', 'qty': 1, 'index': 3.0},       # 標準 2.8 に手入力指数
    {'name': '塗装費用', 'method': '', 'qty': 1, 'price': 61900, 'manual': True},                # 修理方法空欄の手入力行
    {'name': '産廃処理費用', 'method': '', 'qty': 1, 'price': 5000, 'manual': True},
    {'name': '材料代', 'method': '', 'qty': 1, 'price': 1000, 'wage': 3000, 'manual': True},     # 手入力行に工賃も
]
# exp_manual.neo の ERParts から写した期待値（列: 値）
EXPECT = {
    '0400': {'PartsPriceOutTax': 45000, 'PartsPriceStandardOutTax': 43000, 'PartsPriceByManual': '*', 'Time': 0.3, 'TimeStandard': 0.3, 'WageOutTax': 2400, 'WageByManual': '', 'WageFileTime': '0.3', 'DisposalCode': 0},
    '2700': {'PartsPriceOutTax': 69600, 'PartsPriceByManual': '', 'Time': -1, 'TimeStandard': 2.8, 'WageOutTax': 25000, 'WageStandardOutTax': 22400, 'WageByManual': '*', 'WorkCode': 'Q1T1      ', 'WageFileTime': '2.8', 'ChangeTotalOutTax': 92000},
    '3500': {'Time': 3.0, 'TimeStandard': 2.8, 'WageOutTax': 24000, 'WageStandardOutTax': 22400, 'WageByManual': '#', 'WageFileTime': '2.8'},
    '塗装費用': {'PartsCode': '', 'DisposalCode': -1, 'DisposalName': '', 'PartsName': '塗装費用', 'PartsNameStandard': '', 'PartsPriceOutTax': 61900, 'PartsPriceStandardOutTax': -1, 'PartsPriceByManual': '*', 'Time': -1, 'WageOutTax': -1, 'WageByManual': '', 'ChangeTotalOutTax': -1, 'PartsFileTime': '', 'ConstructGroup': ''},
    '産廃処理費用': {'DisposalCode': -1, 'PartsName': '産廃処理費用', 'PartsPriceOutTax': 5000, 'PartsPriceStandardOutTax': -1, 'PartsPriceByManual': '*'},
    '材料代': {'DisposalCode': -1, 'PartsName': '材料代', 'PartsPriceOutTax': 1000, 'PartsPriceByManual': '*', 'WageOutTax': 3000, 'WageByManual': '*', 'Time': -1, 'TimeStandard': 0, 'WageStandardOutTax': 0, 'ChangeTotalOutTax': -1},
}
TOTAL = {'parts': 252100, 'wage': 54400, 'subtotal': 306500, 'tax': 30650, 'total': 337150}


def main() -> int:
    nb = NeoBuilder()
    est = {'source': 'unit_manual_rows', 'issuer': '', 'est_date': '20260908', 'vehicle': VEH, 'customer': {}, 'insurance': {}, 'labor_rate': 8000,
           'items': ITEMS, 'paint': {}, 'expenses': [], 'totals': {}, 'hints': {'eva_codes': ['Q']}}
    neo, rep = nb.build(est, VEH, hints=est['hints'], labor_rate=8000, est_date='20260908', insurance={})
    # 書き出した NEO から ERParts を読む（build の rep['rows'] ではなく保存形）
    sys.path.insert(0, HERE)
    import neo_diff
    tmp = os.path.join(os.environ.get('TEMP', HERE), 'unit_manual_rows.neo')
    open(tmp, 'wb').write(neo)
    d = neo_diff.load(tmp)
    em = next(v for k, v in d.items() if k.endswith('AnSvEm0001.sld'))
    cols = [c[1] for c in em.execute('pragma table_info(ERParts)')]
    rows = [dict(zip(cols, r)) for r in em.execute('select * from ERParts order by RecordNo')]
    fails = 0
    for r in rows:
        key = r['PartsCode'].strip() or r['PartsName'].strip()
        exp = EXPECT.get(key)
        if not exp:
            continue
        for k, v in exp.items():
            got = r.get(k)
            ok = (abs(float(got) - float(v)) < 1e-6) if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(got, (int, float)) else (str(got) == str(v))
            fails += 0 if ok else 1
            if not ok:
                print(f'FAIL {key} {k}: got {got!r} expected {v!r}')
    for k, v in TOTAL.items():
        if int(rep['totals'].get(k) or 0) != v:
            fails += 1; print(f"FAIL totals {k}: {rep['totals'].get(k)} expected {v}")
    print('unit_manual_rows:', 'all ok' if not fails else f'{fails} failed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
