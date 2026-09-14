# -*- coding: utf-8 -*-
"""ページ小計の別解（明細 + そのページの塗装行・費用）の単体テスト（2026-09-14）。ADDATA 不要。
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_page_subtotal.py
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import reading_pages as rp  # noqa: E402

HEADER = {'source': 'test.pdf', 'issuer': 'テスト鈑金', 'est_date': '20260901', 'format': 'A', 'labor_rate': 8000,
          'vehicle': {'model_code': 'X'}, 'paint': {'total': 22140, 'material': 5000, 'coat': 'ソリッド'},
          'totals': {'parts': 51000, 'wage': 42140, 'taxable': 93140, 'tax': 9314, 'total': 102454}}
ROWS = ['|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ|取替|52119-11111|1.50|1|50000|12000||', '|ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ|脱着|||1||8000||']
LINES = [{'name': '加算基礎数値', 'index': 3.0, 'wage': 22140}]
EXPS = [{'name': 'ショートパーツ', 'amount': 1000, 'in': '部品計'}, {'name': '廃棄費用', 'amount': 3000, 'in': '作業計'}]


def page(subtotal, lines=None, exps=None):
    p = {'page': 1, 'rows_printed': 2, 'subtotal': subtotal, 'marks': {}, 'blocks': [{'title': '', 'rows': list(ROWS)}]}
    if lines:
        p['paint_lines'] = copy.deepcopy(lines)
    if exps:
        p['expenses'] = copy.deepcopy(exps)
    return p


def main() -> int:
    # 1) 従来どおり: 小計 = 明細だけ
    r = rp.validate_page(HEADER, page({'parts': 50000, 'wage': 20000}))
    assert r['ok'], r['fail']
    # 2) 別解: 小計が塗装行を含む（工賃 20000 + 22140）
    r = rp.validate_page(HEADER, page({'parts': 50000, 'wage': 42140}, lines=LINES))
    assert r['ok'], r['fail']
    # 3) 別解: 小計が費用を含む（部品 50000 + 1000、工賃 20000 + 3000）
    r = rp.validate_page(HEADER, page({'parts': 51000, 'wage': 23000}, exps=EXPS))
    assert r['ok'], r['fail']
    # 4) 別解: 塗装行 + 費用（作業計）の両方
    r = rp.validate_page(HEADER, page({'parts': 51000, 'wage': 45140}, lines=LINES, exps=EXPS))
    assert r['ok'], r['fail']
    # 4b) 集計先の語彙は _in_kind と同じ（'部品' / 'kind': 'wage' でも効く。Codex 指摘）
    r = rp.validate_page(HEADER, page({'parts': 51000, 'wage': 23000}, exps=[{'name': 'ショートパーツ', 'amount': 1000, 'in': '部品'}, {'name': '廃棄費用', 'amount': 3000, 'kind': 'wage'}]))
    assert r['ok'], r['fail']
    # 5) 塗装行・費用が無ければ別解は効かない（従来どおり不合格）
    r = rp.validate_page(HEADER, page({'parts': 50000, 'wage': 42140}))
    assert not r['ok'] and any('工賃計 印字 42,140 / 転記 20,000' in t for t in r['fail']), r['fail']
    # 6) 足しても合わなければ不合格（別解で甘くならない）
    r = rp.validate_page(HEADER, page({'parts': 50000, 'wage': 42000}, lines=LINES))
    assert not r['ok'], r
    # 7) merge した reading でも同じ別解が効く（ページ由来の行に page が付く。header 由来には付かない）
    case = tempfile.mkdtemp(prefix='test_page_subtotal_')
    try:
        os.makedirs(os.path.join(case, 'pages'))
        hdr = dict(HEADER, expenses=[{'name': 'コーティング', 'amount': 10000, 'in': '諸費用計'}])
        json.dump(hdr, open(os.path.join(case, 'pages', 'header.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        json.dump(page({'parts': 51000, 'wage': 45140}, lines=LINES, exps=EXPS), open(os.path.join(case, 'pages', 'page_1.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        rd, msgs = rp.merge(case)
        assert rd is not None, msgs
        assert rd['paint']['lines'][0].get('page') == 1 and rd['expenses'][0].get('page') is None and rd['expenses'][1].get('page') == 1, rd['expenses']
        sys.path.insert(0, os.path.dirname(HERE))
        import reading_check as rc
        ck = rc.Checker(rd)
        ck.check_subtotals()
        assert not ck.result()['fail'], ck.result()['fail']
        # 重複検出は page を付ける前のキーで行う（同じ費用を 2 ページに書いたら今までどおり気づく）
        json.dump(dict(page({'parts': 51000, 'wage': 45140}, lines=LINES, exps=EXPS), page=2), open(os.path.join(case, 'pages', 'page_2.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        rd2, msgs2 = rp.merge(case)
        assert any('同じ行が 2 回' in m for m in msgs2), msgs2
    finally:
        shutil.rmtree(case, ignore_errors=True)
    print('test_page_subtotal: all ok')
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
