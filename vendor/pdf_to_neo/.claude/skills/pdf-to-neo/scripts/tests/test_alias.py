# -*- coding: utf-8 -*-
"""別名辞書（part_code_names.json）と draft_estimate の辞書照合のテスト。ADDATA が必要（J87 N-BOX）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_alias.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from build_part_names import norm_name  # noqa: E402
import draft_estimate as de  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': ''}


def test_norm():
    assert norm_name('RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ (5dm²)', strip_side=True) == 'Fﾌｴﾝﾀﾞ'
    assert norm_name('ﾘﾔｺﾝﾋﾞﾈｰｼｮﾝﾗﾝﾌﾟ', strip_side=True) == 'Rｺﾝﾋﾞﾈｼﾖﾝﾗﾝﾌﾟ'
    assert norm_name('Rﾗｲｾﾝｽｶﾞｰﾆﾂｼﾕ') == 'Rﾗｲｾﾝｽｶﾞﾆﾂｼﾕ'  # 12.DB 名称の先頭 R はリヤ（外さない）
    assert norm_name('左 ﾍｯﾄﾞﾗﾝﾌﾟ', strip_side=True) == 'ﾍﾂﾄﾞﾗﾝﾌﾟ'
    assert norm_name('フロントバンパー カバー', strip_side=True) == 'Fﾊﾞﾝﾊﾟｶﾊﾞ'


def test_dict_lookup():
    d = de.part_names()
    assert d, 'part_code_names.json が無い（build_part_names.py で作る）'
    assert '0010' in d['names'][norm_name('ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ', strip_side=True)]
    assert {'0802', '1002'} <= set(d['names'][norm_name('ﾌﾛﾝﾄﾌｪﾝﾀﾞﾗｲﾅ', strip_side=True)])  # 12.DB は 'Fﾌｴﾝﾀﾞﾗｲﾅ'（前後付き）


def test_drafter_alias():
    rd = {'source': 'test', 'issuer': 'テスト', 'est_date': '20260908', 'vehicle': VEH, 'labor_rate': 8000,
          'blocks': [{'title': '', 'rows': [
              '|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ|取替|||1|59100|||',
              '|RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ|取替|||1|30000|||',
              '|RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾗｲﾅ|取替|||1|3000|||',
              '|ﾘﾔｺﾝﾋﾞﾈｰｼｮﾝﾗﾝﾌﾟ LH|取替|||1|20000|||',
          ]}],
          'totals': {}}
    est = de.Drafter(rd).build()
    codes = [it.get('code') for it in est['items']]
    print('   codes:', codes)
    assert codes[0] == '0010', codes
    assert codes[1] == '1000', codes  # 右フェンダ
    assert codes[2] == '1002', codes  # 右フェンダライナ（J87 は Fｲﾝﾅﾌｴﾝﾀﾞ）
    assert codes[3] in ('4810', '4730'), codes  # 左リヤコンビネーションランプ（J87 のコード）
    assert all(not it.get('manual') for it in est['items']), est['items']


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                fails += 1
                print('FAIL', name, e)
    print('alias tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
