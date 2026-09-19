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
    # 名称欄のカタカナは半角（亮平さん指示 2026-09-17。実案件 NEO 400 本: 明細 19,454 行中 19,280 行・
    # 外板パネル 851 行中 849 行・追加塗装 2,915 行中 2,864 行が半角）。estimate に全角で書かれていても生成器が直す
    import re as _re
    # 半角形のある全角カナ（ヵ ヶ ヰ ヱ ヮ は半角が無いので除く）と、半角化されずに残りやすい中黒・繰り返し記号
    fw = _re.compile(r'[ァ-ヴヷ-ヺ]|[ヽヾ・]')
    kana = [{'code': '1400', 'name': 'Fバルクヘッド', 'method': '取替', 'qty': 1},
            {'name': 'フロントバンパー・カバー', 'method': '取替', 'qty': 1, 'price': 50000, 'manual': True},
            {'name': 'リヤスポイラー', 'method': '', 'qty': 1, 'price': 3000, 'manual': True,
             'recycle': {'name': 'リサイクルバンパー', 'price': 3000}}]
    def names(db, tbl, col) -> list:
        return [v for (v,) in db.execute('select %s from %s' % (col, tbl)) if isinstance(v, str) and v.strip()]
    import shutil as _sh
    _neo = os.path.join(os.environ.get('TEMP', HERE), 'unit_settings.neo')
    _neo0 = os.path.join(os.environ.get('TEMP', HERE), 'unit_settings_base.neo')
    build(F2)   # 先に雛形そのままの NEO を作る（費用の既定行など、コグニ側の固定名を除くため）
    _sh.copyfile(_neo, _neo0)   # build は同じ名前に上書きするので、別名にしてから読む
    em0 = neo_diff.load(_neo0)['AnSvEm0001.sld']
    build(kana, paint={'panels': [{'name': 'フロントフエンダパネル', 'index': 2.0, 'wage': 16000}],
                       'other': [{'name': 'ハクリ・ミガキ工程', 'index': 1.7, 'wage': 14880}],
                       'total': 40000, 'material': 10000},
          expenses=[{'name': 'ボデーコーテイング', 'amount': 8000, 'kind': 'wage'}])
    em = neo_diff.load(_neo)['AnSvEm0001.sld']
    bad, seen = [], 0
    for tbl, col, want in (('ERParts', 'PartsName', 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ･ｶﾊﾞｰ'), ('RCParts', 'PartsName', 'ﾘｻｲｸﾙﾊﾞﾝﾊﾟｰ'),
                           ('PaintingPanel', 'PanelName', 'ﾌﾛﾝﾄﾌｴﾝﾀﾞﾊﾟﾈﾙ'), ('PaintingOther', 'Name', 'ﾊｸﾘ･ﾐｶﾞｷ工程'),
                           ('Expense', 'Name', 'ﾎﾞﾃﾞｰｺｰﾃｲﾝｸﾞ')):
        vals = names(em, tbl, col)
        check(vals, f'{tbl}.{col} を 1 件も読めていない（表名・列名の打ち間違い）')
        check(any(want in v for v in vals), f'{tbl}.{col} に半角カナの {want} が無い: {vals[:4]}')
        fixed_ = set(names(em0, tbl, col))       # 雛形にもとから入っている固定名は見ない
        new = [v for v in vals if v not in fixed_]
        seen += len(new)
        bad += [f'{tbl}.{col} {v!r}' for v in new if fw.search(v)]
    check(seen >= 5, f'この build で書いた名称が少なすぎる（{seen} 件）')
    check(not bad, '名称欄に全角カナ・全角中黒が残っている: ' + ' / '.join(bad[:4]))
    # 塗装の入力方式「実額」（コグニ: その他 → 塗装 → 入力方式。実案件 NEO 400 本中 91 本）。
    # 工場見積が「塗装費用 一式」しか出していない案件を、パネルを作らず総額 1 つで書く
    def paint_neo(paint):
        build(F2, paint=paint)
        d = neo_diff.load(os.path.join(os.environ.get('TEMP', HERE), 'unit_settings.neo'))
        em = d['AnSvEm0001.sld']
        pl = tuple(em.execute('SELECT InputType, InputTypeName, BoothFlag, BaseTime FROM PaintingPlan').fetchone())
        pt = tuple(em.execute('SELECT WageTotalOutTax, MaterialTotalOutTax, TotalOutTax, WageTotalByManual FROM PaintingTotal').fetchone())
        tt = tuple(em.execute('SELECT pn_TotalOutTax, pn_MaterialTotalOutTax, Total FROM Total').fetchone())
        npan = em.execute('SELECT COUNT(*) FROM PaintingPanel').fetchone()[0]
        oth = [r[0] for r in em.execute('SELECT Name FROM PaintingOther WHERE WageOutTax > 0')]
        return pl, pt, tt, npan, oth
    pl, pt, tt, npan, oth = paint_neo({'total': 100000, 'input_type': '実額'})
    check(pl[:2] == (0, '実額'), f'実額の入力方式が書かれていない: {pl}')
    check(pl[2] == 0 and pl[3] == -1, f'実額なのにブース・加算基礎が生きている: {pl}')
    check(npan == 0 and not oth, f'実額なのに塗装パネル {npan} 枚 / 追加項目 {oth}')
    check(pt == (0, 0, 100000, ''), f'実額の塗装計がおかしい（工賃計・材料計は 0、計に総額、印は付かない）: {pt}')
    check(tt[:2] == (100000, 0), f'合計欄の塗装計がおかしい: {tt}')
    pl2, pt2, tt2, npan2, oth2 = paint_neo({'total': 100000})          # 指定が無ければ今までどおり（追加項目に 1 行）
    check(pl2[:2] == (1, '指数'), f'指定が無いのに実額になっている: {pl2}')
    check(oth2 == ['塗装費用(工場見積)'], f'一式の追加項目が今までどおりでない: {oth2}')
    check(tt2[2] == tt[2], f'入力方式で総額が変わってはいけない: {tt2[2]} / {tt[2]}')
    pl3, _pt3, _tt3, _n3, oth3 = paint_neo({'total': 100000, 'input_type': '実額', 'other': [{'name': 'ﾌﾟﾗｲﾏ-塗装', 'index': 1.0}]})
    check(pl3[:2] == (1, '指数'), f'追加項目があるのに実額にしている（内訳を持てない）: {pl3}')
    print('unit_settings:', 'all ok' if not fails else f'{fails} failed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
