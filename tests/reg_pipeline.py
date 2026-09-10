# -*- coding: utf-8 -*-
"""PDF経路の「元見積と同じ行・同じ金額」を守る回帰テスト。

このアプリの絶対条件は、生成した .neo の明細行と金額が元の見積書と一致すること。
過去に何度も「合計を合わせるために行を捏造する」「読み落としが消費税に化けて
無警告で総額が減る」を作り込んでいるので、網羅ケースで毎回確かめる。
"""
import sys, os, json, io, contextlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from zz_h import run
from zz_cases import mk

FAIL = []
CASES = []
for tax_incl in (False, True):
    for basis in (('in', 'out') if not tax_incl else ('in',)):
        for disc, disc_row in ((0, True), (20000, True), (20000, False), (60000, True)):
            for extra in (0, 20000):
                for M in (0, 20000, 55000, 80000, 100000, 140000):
                    CASES.append(dict(M=M, disc=disc, extra=extra, basis=basis,
                                      tax_incl=tax_incl, disc_row=disc_row))

for c in CASES:
    d = mk(**c)
    lab = json.dumps(c, sort_keys=True)
    B = io.StringIO()
    with contextlib.redirect_stdout(B):
        r = run(lab, d['read'], d['hdr'], tax_incl=d['tax_incl'], show=False)
    total = r['tot'][3]          # Total.Total（税込総合計）
    warns = r.get('warn') or []
    # 1) 生成物の総額は必ず原本の総額と一致すること
    if total != d['true_total_intax']:
        FAIL.append(f"総額不一致 原本{d['true_total_intax']:,} → {total:,}  {lab}")
    # 2) 読み落としがあるケースでは必ず利用者に知らせること（無言で通さない）
    if c['M'] and not warns:
        FAIL.append(f"読み落とし{c['M']:,}円が無警告  {lab}")
    # 3) 完全に読めているケースでは誤警告を出さないこと
    if not c['M'] and warns:
        FAIL.append(f"読み落とし無しなのに警告 {warns}  {lab}")

print(f'REG_PIPELINE: {len(CASES)}ケース ->', 'ALL PASS' if not FAIL else f'FAIL {len(FAIL)}件')
for f in FAIL[:20]:
    print('  -', f)
sys.exit(1 if FAIL else 0)
