# -*- coding: utf-8 -*-
"""連動・吸収・WorkCode の単体テスト（コグニ実機 2026-09-08、J87 N-BOX。根拠 = NEO_check/_eva_exp/cogni_H1〜H7.neo・cogni_G1.neo）
  H1 0010 取替 + 0020 脱着       → 0010 0.9（B 80 + 相手の C 10、WorkCode 'B'）、0020 指数なし・WorkCode 'C'
  H2 0800 取替 + 2300 取替       → 2300 2.1（E1 200 + フェンダの N1 10）、0800 0.5・WorkCode 'TN1'・ChangeTotal 41,800（T 50 + N1 10）
  H3 2300 取替 + 2310 脱着 + 2450 取替 → 2310/2450 は E1 に吸収（Time -1 / TimeStandard 0、WorkCode 'G1'/'I1' は残る、2450 ChangeTotal 60,100）
  H4 2310 脱着 + 2450 取替（ドア無し）→ 0.55 `$` / 0.2 `$`
  H5 0800 取替 単独               → 0.5・WorkCode 'TN1'・ChangeTotal 41,800
  H6 0010 脱着 + 0020 取替       → 0010 0.5（A 40 + C 10）、0020 指数なし・WorkCode 'C'・ChangeTotal 3,550
  H7 2300 脱着 + 2310 脱着 + 2450 取替 + 0800 脱着 → 吸収も連動も無し（0.6 / 0.55 / 0.2 / 0.3、0800 'SN1'）
  G1 上記の複合 8 行 → 合計 317,471
  H8 0600 ボンネット取替 + 0608 ヒンジ取替（A15）→ 0.8 `$` / 0.1 'O'（ヒンジの O はボンネットの下の sub 行）。他ブロックでも同じ規則
  H9 4300 テールゲート取替 + 4304 ヒンジ取替（X10）→ 1.6 / 0.1 'H3I3'、H10 4304 単独 → 0.1 'H3I3' 6,210
  H15 2300 取替 + 2344 取替 → 2.2 'E1'（連動区分 O1 は WorkCode に出ない）、H16 0010 取替 + 0020 取替 → 0.9 'B'
  H31 2700 スライドドア取替 + 4810 ランプユニット脱着 → 4810 は指数なし（2700 の T1 = リンク F0 が有効 → F1 の行を吸収。ブロックをまたぐ偶奇ペア）
  H11 0800 板金ランク B（面積 5）+ 2300 板金 指数 1.5 + 0010 修理 0.5 + 2450 取替 → 合計 96,910。ランク行は TimeStandard/WageStandard に指数が入る（'@'）、'#' 行は 0
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_link_absorb.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
from estimate_to_neo import NeoBuilder  # noqa: E402
import neo_diff  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': 'YR586P'}


def it(code, name, method):
    return {'code': code, 'name': name, 'method': method, 'qty': 1}


BUMPER_K, BEAM_D = it('0010', 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', '取替'), it('0020', 'Fﾊﾞﾝﾊﾟﾋﾞｰﾑ', '脱着')
BUMPER_D, BEAM_K = it('0010', 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', '脱着'), it('0020', 'Fﾊﾞﾝﾊﾟﾋﾞｰﾑ', '取替')
FENDER_K, FENDER_D = it('0800', 'LFﾌｪﾝﾀﾞﾊﾟﾈﾙ', '取替'), it('0800', 'LFﾌｪﾝﾀﾞﾊﾟﾈﾙ', '脱着')
DOOR_K, DOOR_D = it('2300', 'LFﾄﾞｱﾊﾟﾈﾙ', '取替'), it('2300', 'LFﾄﾞｱﾊﾟﾈﾙ', '脱着')
GLASS_D, MIRROR_K = it('2310', 'LFﾄﾞｱｶﾞﾗｽ', '脱着'), it('2450', 'LFﾄﾞｱﾐﾗｰ', '取替')

# (PartsCode) → (Time, TimeStandard, WorkCode, WageByManual, ChangeTotal)。ChangeTotal None = 比較しない（再検索後のコグニは脱着行の標準を空にするため根拠が無い）
CASES = {
    'H1': ([BUMPER_K, BEAM_D], 72930, {'0010': (0.9, 0.9, 'B', '', 65500), '0020': (-1, 0, 'C', '', None)}),
    'H2': ([FENDER_K, DOOR_K], 134750, {'0800': (0.5, 0.5, 'TN1', '', 41800), '2300': (2.1, 2.1, 'E1', '', 80700)}),
    'H3': ([DOOR_K, GLASS_D, MIRROR_K], 153120, {'2300': (2.0, 2.0, 'E1', '', 80700), '2310': (-1, 0, 'G1', '', None), '2450': (-1, 0, 'I1', '', 60100)}),
    'H4': ([GLASS_D, MIRROR_K], 70950, {'2310': (0.55, 0.55, 'G1', '$', None), '2450': (0.2, 0.2, 'I1', '$', 60100)}),
    'H5': ([FENDER_K], 45100, {'0800': (0.5, 0.5, 'TN1', '', 41800)}),
    'H6': ([BUMPER_D, BEAM_K], 7425, {'0010': (0.5, 0.5, 'A', '', None), '0020': (-1, 0, 'C', '', 3550)}),
    'H7': ([DOOR_D, GLASS_D, MIRROR_K, FENDER_D], 78870, {'2300': (0.6, 0.6, 'D1', '', None), '2310': (0.55, 0.55, 'G1', '$', None), '2450': (0.2, 0.2, 'I1', '$', 60100), '0800': (0.3, 0.3, 'SN1', '', None)}),
    'H8': ([it('0600', 'ﾎﾞﾝﾈｯﾄ', '取替'), it('0608', 'R ﾎﾞﾝﾈｯﾄﾋﾝｼﾞ', '取替')], 54989, {'0600': (0.8, 0.8, 'N', '$', 46800), '0608': (0.1, 0.1, 'O', '', 3190)}),
    'H9': ([it('4300', 'ﾃｰﾙｹﾞｰﾄ', '取替'), it('4304', 'R ﾃｰﾙｹﾞｰﾄﾋﾝｼﾞ', '取替')], 117051, {'4300': (1.6, 1.6, 'D3', '', 101000), '4304': (0.1, 0.1, 'H3I3', '', 6210)}),
    'H10': ([it('4304', 'R ﾃｰﾙｹﾞｰﾄﾋﾝｼﾞ', '取替')], 5951, {'4304': (0.1, 0.1, 'H3I3', '', 6210)}),
    'H11': ([{'code': '0800', 'name': 'LFﾌｪﾝﾀﾞﾊﾟﾈﾙ', 'method': '板金', 'qty': 1, 'bankin': {'area': 5, 'yes': [1, 0, 0]}}, {'code': '2300', 'name': 'LFﾄﾞｱﾊﾟﾈﾙ', 'method': '板金', 'qty': 1, 'index': 1.5},
             {'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '修理', 'qty': 1, 'index': 0.5}, MIRROR_K], 96910,
            {'0800': (1.5, 1.5, '', '@', 41800), '2300': (1.5, 0, '', '#', 80700), '0010': (0.5, 0, '', '#', 65500), '2450': (0.2, 0.2, 'I1', '$', 60100)}),  # 板金ランク B（面積 5）= 1.5 は標準欄にも入る（cogni_H11）
    'H15': ([DOOR_K, it('2344', 'LFﾄﾞｱｱｳﾀﾊﾝﾄﾞﾙ', '取替')], 95359, {'2300': (2.2, 2.2, 'E1', '', 80700), '2344': (-1, 0, 'O1', '', None)}),  # 連動加算は WorkCode に出ない（'E1O1' ではない）
    'H16': ([BUMPER_K, it('0020', 'Fﾊﾞﾝﾊﾟﾋﾞｰﾑ', '取替')], 75955, {'0010': (0.9, 0.9, 'B', '', 65500), '0020': (-1, 0, 'C', '', 3550)}),  # 同上（'BC' ではない）
    'H31': ([it('2700', 'L ｽﾗｲﾄﾞﾄﾞｱﾊﾟﾈﾙ', '取替'), it('4810', 'L ﾗﾝﾌﾟﾕﾆｯﾄ', '脱着')], 98560, {'2700': (2.5, 2.5, 'Q1T1', '', None), '4810': (-1, 0, 'U3', '', None)}),  # 2700 の T1（リンク F0）が居ると X30 の 4810 U3（リンク F1）は吸収（偶奇ペアはブロックをまたぐ）
    'G1': ([BUMPER_K, BEAM_D, {'code': '0172', 'name': 'Fﾊﾞﾝﾊﾟｸﾘｯﾌﾟ', 'method': '取替', 'qty': 2}, FENDER_K, it('1000', 'RFﾌｪﾝﾀﾞﾊﾟﾈﾙ', '取替'), DOOR_K, GLASS_D, MIRROR_K], 317471,
           {'0010': (0.9, 0.9, 'B', '', 65500), '0800': (0.5, 0.5, 'TN1', '', 41800), '1000': (0.5, 0.5, 'VN2', '', 41800), '2300': (2.1, 2.1, 'E1', '', 80700), '2310': (-1, 0, 'G1', '', None), '2450': (-1, 0, 'I1', '', 60100)}),
}


def build(nb, items):
    est = {'source': 'unit_link_absorb', 'issuer': '', 'est_date': '20260908', 'vehicle': VEH, 'customer': {}, 'insurance': {}, 'labor_rate': 8000,
           'items': items, 'paint': {}, 'expenses': [], 'totals': {}, 'hints': {}}
    neo, rep = nb.build(est, VEH, hints=est.get('hints'), labor_rate=8000, est_date='20260908', insurance={})
    tmp = os.path.join(os.environ.get('TEMP', HERE), 'unit_link_absorb.neo')
    open(tmp, 'wb').write(neo)
    d = neo_diff.load(tmp)
    em = d['AnSvEm0001.sld']
    cols = [c[1] for c in em.execute('pragma table_info(ERParts)')]
    rows = {r[cols.index('PartsCode')]: dict(zip(cols, r)) for r in em.execute('select * from ERParts order by RecordNo')}
    tcols = [c[1] for c in em.execute('pragma table_info(Total)')]
    return rows, dict(zip(tcols, em.execute('select * from Total').fetchone()))


def main() -> int:
    nb = NeoBuilder()
    fails = 0
    for tag, (items, total, exp) in CASES.items():
        rows, tot = build(nb, items)
        if tot['Total'] != total:
            fails += 1; print(f'FAIL {tag} total {tot["Total"]} expected {total}')
        for code, (t, ts, wc, wm, ct) in exp.items():
            r = rows[code]
            got = (r['Time'], r['TimeStandard'], r['WorkCode'].strip(), r['WageByManual'], r['ChangeTotalOutTax'])
            ok = abs(float(r['Time']) - t) < 1e-6 and abs(float(r['TimeStandard']) - ts) < 1e-6 and got[2] == wc and got[3] == wm and (ct is None or got[4] == ct)
            if not ok:
                fails += 1
            print(('ok  ' if ok else 'FAIL'), tag, code, 'got', got, 'expected', (t, ts, wc, wm, ct))
    print('unit_link_absorb:', 'all ok' if not fails else f'{fails} failed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
