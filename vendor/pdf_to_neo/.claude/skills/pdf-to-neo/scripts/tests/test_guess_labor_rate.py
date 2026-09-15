# -*- coding: utf-8 -*-
"""技術料からレバーレートを逆算する道具の単体テスト。
    cd files && python .claude/skills/pdf-to-neo/scripts/tests/test_guess_labor_rate.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import guess_labor_rate as g  # noqa: E402


def test_aqua_is_unique():
    """ディーラー BP センター（トヨタ系）のアクア（2026-09-09）の技術料は 9,070 円だけで説明できる"""
    hits = g.guess([8160, 3630, 20860, 1810, 18140, 910])
    assert [r for r, _ in hits] == [9070], f'候補が 9,070 円だけになっていない（{[r for r, _ in hits]}）'
    assert hits[0][1][20860] == 2.3, f'指数の割り出しが違う（{hits[0][1]}）'


def test_round_amounts_are_ambiguous():
    """千円単位の技術料しか無い見積（工場 R ヴァンガード）は候補が絞れない。
    このとき速報報告書の「工賃単価」で決める、と分かることがこの道具の役目"""
    hits = g.guess([8000, 4000, 11000, 24000, 42000, 15000, 3000, 5000, 10000])
    assert len(hits) > 1, '絞れないはずの見積で 1 つに決めてしまっている'
    assert 8360 not in [r for r, _ in hits], '実際のレート 8,360 円は技術料からは出ない（速報の工賃単価が要る）'


def test_tax_included_amounts_find_nothing():
    """税込のまま渡すと候補が出ない（税抜に直す合図になる）"""
    assert g.guess([8976, 3993, 22946, 1991, 19954, 1001]) == []


def test_rounding_unit():
    """丸め単位 100 円の工場でも指数を割り出せる（9,070 × 0.3 = 2,721 → 2,700）"""
    assert g.rounded(9070 * 0.3, 100) == 2700, '100 円丸めの計算が違う'
    hits = g.guess([2700], unit=100, lo=9070, hi=9070)
    assert hits and hits[0][1][2700] == 0.3, f'100 円丸めを扱えていない（{hits}）'
    assert g.guess([2700], unit=10, lo=9070, hi=9070) == [], '10 円丸めなら 2,721→2,720 なので候補は無いはず'


def test_one_yen_wages():
    """コグニの工賃丸めが 1 円の工場（2026-09-15 スペーシア FAX 見積: 13,716 = 1.8 × 7,620）は unit=1 でだけ 7,620 円に決まる。
    10 円丸めでは 13,716 を作れないので候補なし（draft は 10 円の倍数でない技術料があれば unit=1 で試す）"""
    wages = [13716, 3048, 762, 3810, 15240, 23622, 7620]
    hits = g.guess(wages, unit=1)
    assert [r for r, _ in hits] == [7620], f'1 円丸めで 7,620 円だけにならない（{[r for r, _ in hits]}）'
    assert hits[0][1][13716] == 1.8 and hits[0][1][762] == 0.1, hits[0][1]
    assert g.guess(wages, unit=10) == [] and g.guess(wages, unit=100) == [], '10 円 / 100 円丸めでは 13,716 を説明できないはず'


def main() -> int:
    ng = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith('test_')):
        try:
            fn()
            print('ok  ', name)
        except AssertionError as e:
            print('FAIL', name, e)
            ng += 1
    print('guess_labor_rate tests:', 'all ok' if not ng else f'{ng} 件 NG')
    return 1 if ng else 0


if __name__ == '__main__':
    sys.exit(main())
