# -*- coding: utf-8 -*-
"""過去 NEO の索引（corpus_lookup.py）の単体テスト。NEO の山（Z:）も ADDATA も使わない（索引は手で作った辞書）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_corpus_lookup.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import corpus_lookup as cl  # noqa: E402

IDX = {'version': cl.VERSION, 'root': 'x', 'built': 't', 'entries': {
    'a1': {'car': 'S89', 'total': 671653, 'rate': 7820, 'est_date': '20260824', 'factory': 'ｻﾝﾌﾟﾙ鈑金 0123456789'},
    'a2': {'car': 'W67', 'total': 100000, 'rate': 7820, 'est_date': '20260701', 'factory': 'ｻﾝﾌﾟﾙ鈑金 0123456789'},
    'a3': {'car': 'W67', 'total': 200000, 'rate': 8000, 'est_date': '20260801', 'factory': 'サンプル板金 012-345-6789'},
    'a4': {'car': 'W44', 'total': 300000, 'rate': 7960, 'est_date': '20260801', 'factory': '写真鑑定'},
    'a5': {'error': 'ValueError'}}}


def test_phones():
    assert cl.phones('TEL 012-345-6789 / FAX 012-345-6780') == ['0123456789', '0123456780']
    assert cl.phones('〒812-0011 092-123-4567') == ['0921234567']          # 郵便番号から読み始めない
    assert cl.phones('2-3-105 092-123-4567') == ['0921234567']
    assert cl.phones('TEL 012–345–6789') == ['0123456789'] and cl.phones('TEL(092)123-4567') == ['0921234567']
    assert cl.phones('2026-09-15') == [] and cl.phones('') == []
    assert cl.phones('TEL.092-123-4567') == ['0921234567'] and cl.phones('ＴＥＬ．０９２－１２３－４５６７') == ['0921234567']


def test_factory_forms():
    f = cl.factory_forms('0123456789', IDX)
    assert [x['factory'] for x in f] == ['ｻﾝﾌﾟﾙ鈑金 0123456789', 'サンプル板金 012-345-6789'] and f[0]['count'] == 2, f
    assert f[0]['rates'] == {'7820': 2}
    assert cl.factory_forms('写真', IDX)[0]['factory'] == '写真鑑定'
    assert cl.factory_forms('01', IDX) == []                               # 短すぎる問い合わせは引かない


def test_same_case():
    assert cl.same_case('S89', 671653, IDX) == ['a1'] and cl.same_case('S89', 1, IDX) == []


def test_hints():
    same = cl.hints({'insurance': {'factory': 'ｻﾝﾌﾟﾙ鈑金 0123456789'}, 'issuer': '株式会社サンプル TEL 012-345-6789'}, '', IDX)
    assert len(same) == 1 and same[0]['level'] == '参考', same
    diff = cl.hints({'insurance': {'factory': '写真鑑定'}, 'issuer': '株式会社サンプル TEL 012-345-6789'}, '', IDX)
    assert len(diff) == 1 and diff[0]['level'] == '要確認' and 'ｻﾝﾌﾟﾙ鈑金 0123456789' in diff[0]['text'], diff
    assert all(k in diff[0] for k in ('page', 'row', 'name', 'code'))       # 確認箇所シートの行と同じ形
    assert cl.hints({'insurance': {}, 'issuer': ''}, '', IDX) == []
    assert cl.hints({'insurance': {'factory': 'x 0123456789'}}, '', {'entries': {}}) == []   # 索引が無ければ何も出さない


def test_load_index_tolerates_broken_files():
    d = tempfile.mkdtemp(prefix='cl_')
    try:
        p = os.path.join(d, 'i.json')
        for body in ('not json', '[1, 2]', json.dumps({'version': cl.VERSION, 'entries': {'a': 1, 'b': {'car': 'S89'}}})):
            open(p, 'w', encoding='utf-8').write(body)
            idx = cl.load_index(p)
            assert isinstance(idx.get('entries'), dict) and all(isinstance(v, dict) for v in idx['entries'].values()), idx
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
    print('corpus_lookup tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
