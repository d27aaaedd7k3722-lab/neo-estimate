# -*- coding: utf-8 -*-
"""元号の単体テスト（2026-09-14、Codex 指摘）
  neo_container.get_era_info は改元日で分ける（年だけで分けると 2019-01〜04 が令和 1 になる）。
  CarRegEra / AccidentEra / EstimatedEra / GarageIn*Era はみなここを通る。
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_era.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
import neo_container as nc  # noqa: E402

CASES = [
    ('20190430', ('平成', '0031')), ('20190501', ('令和', '0001')), ('20190101', ('平成', '0031')), ('20181231', ('平成', '0030')),
    ('20190400', ('平成', '0031')), ('201904', ('平成', '0031')), ('201905', ('令和', '0001')),   # 初度登録年月（日なし）は月初とみなす
    ('19890107', ('昭和', '0064')), ('19890108', ('平成', '0001')), ('19881231', ('昭和', '0063')),
    ('19261225', ('昭和', '0001')), ('19261224', ('令和', '0000')),
    ('20260901', ('令和', '0008')), ('20240101', ('令和', '0006')), ('20161001', ('平成', '0028')),
    ('', ('令和', '0000')), (None, ('令和', '0000')), ('00000000', ('令和', '0000')), ('abcd', ('令和', '0000')), ('2019', ('平成', '0031')),
    # 区切り付き・文字混じり・桁数違いは番兵（1 月 1 日扱いにして平成 31 と答えない。Codex 指摘 2026-09-14）
    ('2019-05-01', ('令和', '0000')), ('2019/04/30', ('令和', '0000')), ('2019ABCD', ('令和', '0000')), ('20190', ('令和', '0000')), ('2019043', ('令和', '0000')),
    (' 20190430 ', ('平成', '0031')), (20190501, ('令和', '0001')),
]


def main() -> int:
    fails = [f'{d!r} → {nc.get_era_info(d)!r}（期待 {want!r}）' for d, want in CASES if nc.get_era_info(d) != want]
    for f in fails:
        print('FAIL', f)
    print('unit_era:', 'all ok' if not fails else f'{len(fails)} 件が不合格', f'/ {len(CASES)} 件')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
