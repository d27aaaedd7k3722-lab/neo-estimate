# -*- coding: utf-8 -*-
"""技術料（円）だけが印字された見積書から、レバーレート（円/h）を逆算する。

指数の列が無い工場書式（ディーラー・二輪・板金工場の独自帳票）では、
「技術料 = 指数 × レバーレート を丸め単位で丸めた値」という関係だけが手掛かりになる。
候補レートを総当たりし、**全部の技術料を 0.1 刻みの指数で説明できる**レートを挙げる。

    cd files && python .claude/skills/pdf-to-neo/scripts/guess_labor_rate.py 8160 3630 20860 1810 18140 910
    cd files && python .claude/skills/pdf-to-neo/scripts/guess_labor_rate.py --round 10 --max-index 20 8160 3630

金額は**税抜**で渡すこと（税込印字の見積書は (100+税率)/100 で割ってから。10% なら 1.1、8% なら 1.08）。
候補が複数出たら、速報報告書の「工賃単価」や工場への確認で決める。決め手は報告に書く。
"""
from __future__ import annotations

import argparse
import sys


def rounded(value: float, unit: int) -> int:
    """コグニと同じ丸め（unit 円単位の四捨五入）"""
    if unit <= 1:
        return int(value + 0.5)
    return int((value / unit) + 0.5) * unit


def index_for(wage: int, rate: int, unit: int, max_index: float) -> float:
    """その技術料を説明できる 0.1 刻みの指数。無ければ 0.0"""
    n = int(round(max_index * 10))
    for k in range(1, n + 1):
        if rounded(rate * k / 10.0, unit) == wage:
            return k / 10.0
    return 0.0


def guess(wages: list[int], unit: int = 10, lo: int = 5000, hi: int = 20000,
          step: int = 10, max_index: float = 20.0) -> list[tuple[int, dict]]:
    """全部の技術料を説明できるレートを（レート, {技術料: 指数}）で返す"""
    out = []
    for rate in range(lo, hi + 1, step):
        got = {}
        for w in wages:
            i = index_for(w, rate, unit, max_index)
            if not i:
                got = {}
                break
            got[w] = i
        if got:
            out.append((rate, got))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description='技術料からレバーレートを逆算する（金額は税抜）')
    ap.add_argument('wages', nargs='+', type=int, help='技術料（税抜・円）。0 の行は渡さない')
    ap.add_argument('--round', dest='unit', type=int, default=10, help='工賃の丸め単位（既定 10 円）')
    ap.add_argument('--lo', type=int, default=5000)
    ap.add_argument('--hi', type=int, default=20000)
    ap.add_argument('--step', type=int, default=10)
    ap.add_argument('--max-index', type=float, default=20.0)
    a = ap.parse_args(argv)
    wages = sorted({w for w in a.wages if w > 0})
    if not wages:
        print('技術料が空です')
        return 1
    hits = guess(wages, a.unit, a.lo, a.hi, a.step, a.max_index)
    if not hits:
        print(f'{len(wages)} 個の技術料を 0.1 刻みの指数で説明できるレートが {a.lo}〜{a.hi} 円に無い。'
              '税込のまま渡していないか（税抜に直すには (100+税率)/100 で割る）、'
              '定額の費用行（コーティング・写真代など）を混ぜていないか確認する')
        return 1
    print(f'技術料 {len(wages)} 個 / 丸め {a.unit} 円 → 候補 {len(hits)} 件')
    for rate, got in hits[:10]:
        s = ' '.join(f'{w:,}→{i:.1f}' for w, i in sorted(got.items()))
        print(f'  {rate:,} 円: {s}')
    if len(hits) > 10:
        print(f'  …他 {len(hits) - 10} 件。--lo/--hi で範囲を絞るか、速報の「工賃単価」で決める')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
