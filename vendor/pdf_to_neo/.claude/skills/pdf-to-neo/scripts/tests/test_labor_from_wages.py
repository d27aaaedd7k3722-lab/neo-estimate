# -*- coding: utf-8 -*-
"""技術料だけの書式（指数もレートも印字なし）で、技術料が 10 円の倍数でない工場（コグニの工賃丸め 1 円）のレート逆算。
2026-09-15 スペーシア（S89）の FAX 見積: 13,716 / 3,048 / 762 / 3,810 / 15,240 / 23,622 / 7,620 → 7,620 円。
10 円 / 100 円丸めしか試さないと決まらず、生成器が 100 円刻みの推定（7,600）で標準工賃を作っていた。

    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_labor_from_wages.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import skill_env  # noqa: E402

skill_env.apply()
import draft_estimate as de  # noqa: E402

# 型式指定・類別は車種の識別（個人情報ではない）。車台番号は架空
S89 = {'model_code': '5AA-MK94S', 'serial_no': 'MK94S-300001', 'desig': '20824', 'category': '0001', 'reg_date': 'R7.12', 'color_code': 'WBW'}
READING = {'source': 't', 'issuer': 't', 'est_date': '20260823', 'format': 'F', 'vehicle': dict(S89), 'customer': {}, 'insurance': {},
           'paint': {}, 'expenses': [], 'totals': {}}
ROWS_1YEN = ['|ﾊﾞﾝﾊﾟ ﾌﾛﾝﾄ|取替|||1|40700|13716||', '|LH ﾍｯﾄﾞﾗﾝﾌﾟｱｯｼ|取替|||1|70000|3048||',
             '|RH ﾌﾛﾝﾄﾌｰﾄﾞ ﾋﾝｼﾞ|取替|||1|2950|762||', '|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||',
             '|ｺﾝﾃﾞﾝｻｱｯｼ|取替|||1|29000|7620||', '|ｳｲﾝﾄﾞｼｰﾙﾄﾞｶﾞﾗｽ|取替|||1|187600|23622||']
ROWS_10YEN = ['|ﾊﾞﾝﾊﾟ ﾌﾛﾝﾄ|取替|||1|40700|13720||', '|LH ﾍｯﾄﾞﾗﾝﾌﾟｱｯｼ|取替|||1|70000|3050||',
              '|RH ﾌﾛﾝﾄﾌｰﾄﾞ ﾋﾝｼﾞ|取替|||1|2950|760||', '|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||',
              '|ｺﾝﾃﾞﾝｻｱｯｼ|取替|||1|29000|7620||', '|ｳｲﾝﾄﾞｼｰﾙﾄﾞｶﾞﾗｽ|取替|||1|187600|23620||']


def _s89_available() -> bool:
    try:
        return de.Drafter(dict(READING, blocks=[])).car.get('CarCode') == 'S89'
    except Exception:  # noqa: BLE001  ADDATA に車種が無い・読めない
        return False


def _draft(rows: list) -> dict:
    return de.Drafter(dict(READING, blocks=[{'title': '鈑金・塗装', 'rows': rows}])).build()


def test_one_yen_wages_give_rate_and_round_1():
    """10 円の倍数でない技術料 → 1 円丸めで逆算して 7,620 円、estimate.wage_round は 1"""
    if not _s89_available():
        print('   skip test_one_yen_wages_give_rate_and_round_1（この ADDATA に S89 が無い）')
        return
    e = _draft(ROWS_1YEN)
    assert e.get('labor_rate') == 7620, f"レートが 7,620 にならない（{e.get('labor_rate')}）"
    assert e.get('wage_round') == 1, f"工賃の丸め単位が 1 円にならない（{e.get('wage_round')}）"
    revs = [r for r in e.get('_review', []) if r.get('kind') == 'レバーレート']
    assert revs and all(r.get('level') == '判断' for r in revs), f'1 円単位で説明できるのにレートの要確認が出ている（{revs}）'


def test_ten_yen_wages_keep_default_units():
    """技術料が全部 10 円の倍数なら従来どおり 10 円 → 100 円丸めで試す（1 円丸めは持ち出さない）"""
    if not _s89_available():
        print('   skip test_ten_yen_wages_keep_default_units（この ADDATA に S89 が無い）')
        return
    e = _draft(ROWS_10YEN)
    assert e.get('labor_rate') == 7620, f"レートが 7,620 にならない（{e.get('labor_rate')}）"
    assert e.get('wage_round', 10) == 10, f"10 円丸めのまま決まるべき（{e.get('wage_round')}）"


def test_declared_round_10_yields_to_one_yen_evidence():
    """reading.wage_round: 10（既定と同じ値）と書いてあっても、10 円の倍数でない技術料は 10 円丸めでは説明できないので
    1 円丸めで逆算する（印字の技術料が証拠。従来も明示の 10 は既定と同じ扱いだった）。100 円の明示はその単位だけで試す"""
    if not _s89_available():
        print('   skip test_declared_round_10_yields_to_one_yen_evidence（この ADDATA に S89 が無い）')
        return
    e = de.Drafter(dict(READING, wage_round=10, blocks=[{'title': '鈑金・塗装', 'rows': ROWS_1YEN}])).build()
    assert e.get('labor_rate') == 7620 and e.get('wage_round') == 1, (e.get('labor_rate'), e.get('wage_round'))
    e = de.Drafter(dict(READING, wage_round=100, blocks=[{'title': '鈑金・塗装', 'rows': ROWS_1YEN}])).build()
    assert not e.get('labor_rate'), f"100 円丸めの明示で 1 円単位の技術料からレートを決めてはいけない（{e.get('labor_rate')}）"
    assert [r for r in e.get('_review', []) if r.get('level') == '要確認' and r.get('kind') == 'レバーレート'], 'レートの要確認が出ていない'


def main() -> int:
    ng = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith('test_')):
        try:
            fn()
            print('ok  ', name)
        except AssertionError as e:
            print('FAIL', name, e)
            ng += 1
    print('labor_from_wages tests:', 'all ok' if not ng else f'{ng} 件 NG')
    return 1 if ng else 0


if __name__ == '__main__':
    sys.exit(main())
