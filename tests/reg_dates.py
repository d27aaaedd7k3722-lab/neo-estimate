# -*- coding: utf-8 -*-
"""reg_dates.py — 日付入力の正規化（_normalize_date8）。サイドバーの事故日・入出庫日は
reading の insurance と Insurance テーブルに入るので、読める形は広く、読めない形は黙って別の日にしない。

    python tests/reg_dates.py
"""
from __future__ import annotations

import os
import sys
import warnings

warnings.filterwarnings('ignore')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import app  # noqa: E402

CASES = [
    ('20260901', '20260901'), ('2026/09/01', '20260901'), ('2026-09-01', '20260901'), ('2026.09.01', '20260901'),
    ('2026-9-1', '20260901'), ('2026/9/1', '20260901'), ('2026年9月1日', '20260901'), ('　２０２６／９／１　', '20260901'),
    ('R6.9.1', '20240901'), ('R6/9/1', '20240901'), ('令和6年9月1日', '20240901'), ('令和元年5月1日', '20190501'),
    ('H30.4.5', '20180405'), ('平成30年4月5日', '20180405'), ('S60.1.1', '19850101'), ('r6.9.1', '20240901'),
    ('9/1', ''), ('2026/13/01', ''), ('2026/02/30', ''), ('', ''), (None, ''), ('abc', ''), ('1900/1/1', ''),
    ('2026-09-01T00:00', ''),   # 時刻付きは年月日として読めない → 空（黙って別の日にしない）
    # 元号の範囲外は空（Codex 指摘 2026-09-14）: 令和 0 年・平成 31 年 5 月・昭和 64 年 1 月 8 日 はその元号に無い
    ('R0.9.1', ''), ('平成31年5月1日', ''), ('平成31年4月30日', '20190430'), ('令和元年4月30日', ''), ('令和元年5月1日', '20190501'),
    ('S64.1.8', ''), ('S64.1.7', '19890107'), ('昭和元年12月24日', ''), ('昭和元年12月25日', '19261225'), ('H1.1.7', ''), ('H1.1.8', '19890108'),
]


def main() -> int:
    fails = []
    for raw, want in CASES:
        got = app._normalize_date8(raw)
        if got != want:
            fails.append(f'{raw!r} → {got!r}（期待 {want!r}）')
    for f in fails:
        print('*** FAILED:', f)
    print('reg_dates:', 'all ok' if not fails else f'{len(fails)} 件が不合格', f'/ {len(CASES)} 件')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
