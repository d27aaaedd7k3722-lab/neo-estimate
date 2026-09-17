# -*- coding: utf-8 -*-
"""設定系の単体テスト（コグニ実機 2026-09-08、J87 N-BOX。根拠 = NEO_check/_eva_exp/cogni_frame_F1/F2/F2_r100/F2_taxfloor.neo）
  - 骨格部品の組合せ指数（1400 バルクヘッド 1.6 / 1410 ステー 1.1 / 1420 は指数なし、脱着の 1410 は標準なし）
  - 工賃単位 100 円 → Setting.wb_Round 100・wi_Round 10
  - 消費税 切り捨て → Setting.tx_ArrangeFlag 2・Total.tx_TotalOutTax は切り捨て
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_settings.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
from estimate_to_neo import NeoBuilder  # noqa: E402
import neo_diff  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': ''}
F1 = [{'code': '1400', 'name': 'Fﾊﾞﾙｸﾍｯﾄﾞ', 'method': '取替', 'qty': 1}, {'code': '1410', 'name': 'LFﾊﾞﾙｸﾍｯﾄﾞｻｲﾄﾞｽﾃｰ', 'method': '取替', 'qty': 1}, {'code': '1420', 'name': 'RFﾊﾞﾙｸﾍｯﾄﾞｻｲﾄﾞｽﾃｰ', 'method': '取替', 'qty': 1}]
F2 = [{'code': '1400', 'name': 'Fﾊﾞﾙｸﾍｯﾄﾞ', 'method': '取替', 'qty': 1}, {'code': '1410', 'name': 'LFﾊﾞﾙｸﾍｯﾄﾞｻｲﾄﾞｽﾃｰ', 'method': '脱着', 'qty': 1},
      {'code': '2620', 'name': 'LFｲﾝｻｲﾄﾞｼﾙ', 'method': '取替', 'qty': 1}, {'code': '5600', 'name': 'Rﾌﾛｱ', 'method': '取替', 'qty': 1}]


def build(items, **extra):
    nb = NeoBuilder()
    est = {'source': 'unit_settings', 'issuer': '', 'est_date': '20260908', 'vehicle': VEH, 'customer': {}, 'insurance': {}, 'labor_rate': 8000,
           'items': items, 'paint': {}, 'expenses': [], 'totals': {}, 'hints': {}}
    est.update(extra)
    neo, rep = nb.build(est, VEH, hints=est.get('hints'), labor_rate=8000, est_date='20260908', insurance={})
    tmp = os.path.join(os.environ.get('TEMP', HERE), 'unit_settings.neo')
    open(tmp, 'wb').write(neo)
    d = neo_diff.load(tmp)
    em, ifc = d['AnSvEm0001.sld'], d['AnSvIf0001.sld']
    cols = [c[1] for c in em.execute('pragma table_info(ERParts)')]
    rows = {r[cols.index('PartsCode')]: dict(zip(cols, r)) for r in em.execute('select * from ERParts order by RecordNo')}
    scols = [c[1] for c in ifc.execute('pragma table_info(Setting)')]
    st = dict(zip(scols, ifc.execute('select * from Setting').fetchone()))
    tcols = [c[1] for c in em.execute('pragma table_info(Total)')]
    tot = dict(zip(tcols, em.execute('select * from Total').fetchone()))
    return rows, st, tot, rep


def main() -> int:
    fails = 0

    def check(cond, msg):
        nonlocal fails
        if not cond:
            fails += 1; print('FAIL', msg)
    rows, st, tot, rep = build(F1)
    check(abs(rows['1400']['Time'] - 1.6) < 1e-6 and rows['1400']['WageOutTax'] == 12800, f"F1 1400 {rows['1400']['Time']}")
    check(abs(rows['1410']['Time'] - 1.1) < 1e-6 and rows['1410']['WageOutTax'] == 8800, f"F1 1410 {rows['1410']['Time']}")
    check(rows['1420']['Time'] == -1 and rows['1420']['WageOutTax'] == -1, f"F1 1420 {rows['1420']['Time']}")
    check(tot['Total'] == 65758, f"F1 total {tot['Total']}")
    check(st['wb_Round'] == 10 and st['wi_Round'] == 10 and st['tx_ArrangeFlag'] == 1, f"F1 setting {st['wb_Round']} {st['wi_Round']} {st['tx_ArrangeFlag']}")
    rows, st, tot, rep = build(F2)
    r = rows['1410']
    check(r['DisposalCode'] == 1 and r['PartsNoStandard'] == '' and r['PartsPriceStandardOutTax'] == -1 and r['ChangeTotalOutTax'] == -1 and r['Time'] == -1, f"F2 1410 脱着 {r['PartsNoStandard']!r} {r['PartsPriceStandardOutTax']} {r['ChangeTotalOutTax']}")
    check(abs(rows['2620']['Time'] - 2.55) < 1e-6 and rows['2620']['WageByManual'] == '$', f"F2 2620 {rows['2620']['Time']} {rows['2620']['WageByManual']!r}")
    check(abs(rows['5600']['Time'] - 1.9) < 1e-6, f"F2 5600 {rows['5600']['Time']}")
    check(tot['Total'] == 178750, f"F2 total {tot['Total']}")
    rows, st, tot, rep = build(F2, wage_round=100)
    check(st['wb_Round'] == 100 and st['wi_Round'] == 10, f"r100 setting {st['wb_Round']} {st['wi_Round']}")
    check(tot['Total'] == 178750, f"r100 total {tot['Total']}")
    rows, st, tot, rep = build(F2, tax_round='切り捨て')
    check(st['tx_ArrangeFlag'] == 2, f"taxfloor flag {st['tx_ArrangeFlag']}")
    check(tot['tx_TotalOutTax'] == 16250 and tot['Total'] == 178750, f"taxfloor tax {tot['tx_TotalOutTax']} total {tot['Total']}")
    # 切り捨てが効く額: 課税小計 162,505 相当は作れないので、計算式だけ確認
    rows, st, tot, rep = build(F2 + [{'name': '雑費', 'method': '', 'qty': 1, 'price': 5, 'manual': True}], tax_round='切り捨て')
    check(tot['SubTotal'] == 162505 and tot['tx_TotalOutTax'] == 16250, f"taxfloor 162,505 → {tot['tx_TotalOutTax']}")
    rows, st, tot, rep = build(F2 + [{'name': '雑費', 'method': '', 'qty': 1, 'price': 5, 'manual': True}], tax_round='切り上げ')
    check(st['tx_ArrangeFlag'] == 3 and tot['tx_TotalOutTax'] == 16251, f"taxceil 162,505 → {tot['tx_TotalOutTax']}")
    # 消費税の表示方法（コグニの「消費税設定」）: 既定は外税、金額が税込で印字された見積書（10-4）は内税 = 実機 NEO と同じ TaxKindFlag 1
    rows, st, tot, rep = build(F2)
    check(st['TaxKindFlag'] == 0, f"既定は外税 {st['TaxKindFlag']}")
    rows2, st2, tot2, rep2 = build(F2, tax_included=10)
    check(st2['TaxKindFlag'] == 1, f"税込印字は内税 {st2['TaxKindFlag']}")
    check(tot2['Total'] == tot['Total'] and rows2['5600']['WageOutTax'] == rows['5600']['WageOutTax'],
          f"表示方法だけの違いで金額は変わらない {tot2['Total']} / {tot['Total']}")
    print('unit_settings:', 'all ok' if not fails else f'{fails} failed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
