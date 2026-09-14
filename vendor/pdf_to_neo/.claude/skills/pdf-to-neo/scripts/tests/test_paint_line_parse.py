# -*- coding: utf-8 -*-
"""コグニ印刷の塗装明細の書き方を下書きが読めるか（2026-09-14）。ADDATA 不要（文字列の整形だけ）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_paint_line_parse.py
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import draft_estimate as de  # noqa: E402

PAT = re.compile(r'^(.*?)(取替|新品|交換|修正|修理)\s*(1/[123])?$')


def parse(name: str):
    n = unicodedata.normalize('NFKC', name).replace(' ', '')
    m = PAT.match(de._clean_panel_line(n))
    return (m.group(1), m.group(2), m.group(3) or '') if m else None


CASES = [
    ('Rrﾊﾟﾈﾙ 修理 20d㎡ (1/2)', ('Rrﾊﾟﾈﾙ', '修理', '1/2')),
    ('ﾃ-ﾙｹﾞ-ﾄ 取替 121d㎡', ('ﾃ-ﾙｹﾞ-ﾄ', '取替', '')),
    ('左 ｸｵ-ﾀﾊﾟﾈﾙ(ｺｳﾁﾝ) 修理 62d㎡ (1/3)', ('左ｸｵ-ﾀﾊﾟﾈﾙ(ｺｳﾁﾝ)', '修理', '1/3')),
    ('ﾎﾞﾃﾞ-ﾛﾜﾊﾞﾂｸﾊﾟﾈﾙ 修理 56d㎡ (1/2)', ('ﾎﾞﾃﾞ-ﾛﾜﾊﾞﾂｸﾊﾟﾈﾙ', '修理', '1/2')),
    ('右 ﾌﾛﾝﾄﾌｪﾝﾀﾞﾊﾟﾈﾙ 修正 1/3', ('右ﾌﾛﾝﾄﾌｪﾝﾀﾞﾊﾟﾈﾙ', '修正', '1/3')),   # 従来の書き方はそのまま
    ('ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ 取替', ('ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ', '取替', '')),
    ('左Rrｱｳﾄｻｲﾄﾞﾊﾟﾈﾙ 修理 96dm² (1/3)', ('左Rrｱｳﾄｻｲﾄﾞﾊﾟﾈﾙ', '修理', '1/3')),
    ('加算基礎数値', None),  # パネル行でないものは今までどおり合わない
]


def main() -> int:
    def norm(want):
        return (unicodedata.normalize('NFKC', want[0]).replace(' ', ''), want[1], want[2]) if want else None
    fails = [f'{name!r} → {parse(name)!r}（期待 {norm(want)!r}）' for name, want in CASES if parse(name) != norm(want)]
    for f in fails:
        print('FAIL', f)
    print('test_paint_line_parse:', 'all ok' if not fails else f'{len(fails)} 件が不合格', f'/ {len(CASES)} 件')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
