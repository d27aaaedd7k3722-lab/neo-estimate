# -*- coding: utf-8 -*-
"""コグニ実機 J52 で観測した 23 パターン（2026-09-06）を _frame_combination で再現できるか"""
import sys, os
F = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # <repo>/files（配布先でも動くよう __file__ から求める）
sys.path.insert(0, os.path.join(F, 'claude_neo_pipeline')); sys.path.insert(0, F)
import estimate_to_neo as e
nb = e.NeoBuilder(); ap = e.AddataParts(nb.engine, 'J52')


def run(members):
    t = ap._frame_combination([(m, 0) for m in members], 'A', 'A', set(), '1', '')
    return {m: t.get(m, 0) / 100 for m in members}


CASES = [([1400], {1400: 1.6}), ([1400, 1512], {1400: 5.7}), ([1400, 1500, 1512], {1400: 7.1}), ([1400, 1500], {1400: 5.6}),
         ([1410], {1410: 1.1}), ([1410, 1420], {1410: 1.3}), ([1420], {1420: 1.1}), ([1420, 1600], {1420: 6.2}), ([1420, 1612], {1420: 6.4}),
         ([1420, 1612, 1600], {1420: 8.3}), ([1420, 1612, 1603], {1420: 6.5}), ([1420, 1603, 1611], {1420: 7.3}),
         ([1410, 1430, 1500, 1512], {1410: 7.7}), ([1410, 1420, 1430, 1434, 1500, 1511, 1600], {1410: 9.2, 1420: 5.1}),
         ([1500], {1500: 0}), ([1430], {1430: 1.7}), ([1434], {1434: 0.8}), ([1511], {1511: 0}), ([1600], {1600: 0}), ([1503], {1503: 0}),
         ([1400, 1503], {1400: 3.9, 1503: 0}), ([1400, 1503, 1511], {1400: 6.6, 1503: 0, 1511: 0}), ([1400, 1508], {1400: 1.6, 1508: 0}), ([1500, 1508], {1500: 0, 1508: 0}), ([1400, 1442], {1400: 1.6, 1442: 0}),
         ([1902, 1904], {1902: 1.35, 1904: 2.25}), ([1904], {1904: 3.15}), ([1950], {1950: 4.5}), ([1410, 1500], {1410: 5.6})]
ok = 0
for m, exp in CASES:
    got = run(m)
    if all(abs(got.get(k, 0) - v) < 0.001 for k, v in exp.items()) and all(abs(got[k]) < 0.001 for k in got if k not in exp):
        ok += 1
    else:
        print('NG', m, got, exp)
print('unit', ok, '/', len(CASES))
# 単独値（ChangeTotal 用、own_all）: FRAME_p7/p9 の ChangeTotal から
BASE = [(1500, 4.5), (1503, 2.7), (1511, 3.4), (1600, 5.1), (1430, 1.7), (1434, 0.8), (1400, 1.6), (1410, 1.1)]
okb = 0
for ref, exp in BASE:
    got = ap._frame_combination([(ref, 0)], 'A', 'A', set(), '1', '', set(), own_all=True).get(ref, 0) / 100
    okb += abs(got - exp) < 0.001 or print('NG base', ref, got, exp) is not None
print('base', okb, '/', len(BASE))
_bad = (len(CASES) - ok) + (len(BASE) - okb)
print('unit_frame:', 'all ok' if not _bad else f'{_bad} failed')
sys.exit(1 if _bad else 0)  # 失敗を終了コードで返す（env_check --self-test が拾えるように）
