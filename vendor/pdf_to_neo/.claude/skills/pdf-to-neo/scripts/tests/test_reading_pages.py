# -*- coding: utf-8 -*-
"""reading_pages.py の単体テスト（架空の見積を一時フォルダに作る。ADDATA 不要）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_reading_pages.py
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

HEADER = {
    'source': 'test.pdf', 'issuer': 'テスト鈑金', 'est_date': '20260901', 'format': 'B', 'labor_rate': 8000,
    'vehicle': {'model_code': 'X'},
    'paint': {'total': 40000, 'material': 22000, 'coat': 'ソリッド'},
    'expenses': [{'name': 'コーティング', 'amount': 10000, 'in': '諸費用計'}],
    'totals': {'parts': 95000, 'wage': 28000, 'paint': 40000, 'material': 22000, 'expense': 10000, 'taxable': 195000, 'tax': 19500, 'total': 214500},
}
PAGE1 = {'page': 1, 'rows_printed': 3, 'subtotal': {'parts': 60000, 'wage': 12000}, 'marks': {'$': 1},
         'blocks': [{'title': 'フロントバンパー', 'rows': [
             '|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ|取替|52119-11111|1.50|1|50000|12000|$|',
             '|ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ|取替|90467-11111||10|5000|||',
             '|ﾊﾞﾝﾊﾟ ﾋﾟｰｽ|取替|52161-11111||1|5000|||']}]}
PAGE2 = {'page': 2, 'rows_printed': 2, 'subtotal': {'parts': 33000, 'wage': 16000},
         'blocks': [{'title': '右 フロントフェンダー', 'rows': [
             '|RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ|取替|53811-11111|2.00|1|30000|16000||',
             '|RH ﾌｪﾝﾀﾞ ﾗｲﾅ|取替|53875-11111||1|3000|||']}],
         'paint_lines': [{'name': 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ 取替', 'index': 2.0, 'wage': 16000}],
         'expenses': [{'name': 'ショートパーツ', 'amount': 2000, 'in': '部品計'}]}


def make_case(pages: list[dict], header: dict = HEADER) -> str:
    case = tempfile.mkdtemp(prefix='rp_test_')
    d = rp.pages_dir(case)
    os.makedirs(d)
    rp.save_json(os.path.join(d, 'header.json'), header)
    for p in pages:
        rp.save_json(os.path.join(d, f"page_{p['page']}.json"), p)
    return case


def test_validate_ok():
    r = rp.validate_page(HEADER, PAGE1)
    assert r['ok'] and r['rows'] == 3, r
    r = rp.validate_page(HEADER, PAGE2)
    assert r['ok'] and r['rows'] == 2, r


def test_validate_row_count_and_subtotal():
    p = copy.deepcopy(PAGE1)
    p['blocks'][0]['rows'].pop()  # 1 行写し漏れ
    r = rp.validate_page(HEADER, p)
    assert not r['ok']
    assert any('行数 印字 3 / 転記 2' in t for t in r['fail']), r['fail']
    assert any('部品計 印字 60,000 / 転記 55,000' in t and 'ﾋﾟｰｽ' not in t for t in r['fail']), r['fail']


def test_validate_marks():
    p = copy.deepcopy(PAGE1)
    p['blocks'][0]['rows'][0] = '|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ|取替|52119-11111|1.50|1|50000|12000||'  # $ を写し忘れ
    r = rp.validate_page(HEADER, p)
    assert any('印 $ の数 印字 1 / 転記 0' in t for t in r['fail']), r['fail']


def test_validate_missing_rows_printed():
    p = copy.deepcopy(PAGE1)
    del p['rows_printed']
    r = rp.validate_page(HEADER, p)
    assert any('rows_printed' in t for t in r['fail']), r['fail']


def test_validate_no_subtotal_page():
    p = copy.deepcopy(PAGE2)
    del p['subtotal']  # 小計の印字が無いページ: 行数だけで合格
    r = rp.validate_page(HEADER, p)
    assert r['ok'], r


def test_merge_ok_and_totals():
    case = make_case([PAGE1, PAGE2])
    try:
        rd, msgs = rp.merge(case)
        assert rd is not None, msgs
        assert len(rd['blocks']) == 2 and rd['blocks'][1]['page'] == 2
        assert rd['pages']['1'] == {'parts': 60000, 'wage': 12000, 'rows': 3, 'marks': {'$': 1}}
        assert rd['paint']['lines'][0]['name'] == 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ 取替' and rd['paint']['total'] == 40000
        assert [e['name'] for e in rd['expenses']] == ['コーティング', 'ショートパーツ']
        rc = rp.cmd_merge(case, False)
        assert rc == 0
        assert os.path.exists(os.path.join(case, 'reading.json'))
        assert not rp.pages_newer_than_reading(case)
        # ページを直したら merge が必要と分かる
        os.utime(os.path.join(rp.pages_dir(case), 'page_2.json'), None)
        import time
        time.sleep(0.05)
        os.utime(os.path.join(rp.pages_dir(case), 'page_2.json'), (time.time() + 5, time.time() + 5))
        assert rp.pages_newer_than_reading(case)
    finally:
        shutil.rmtree(case, ignore_errors=True)


def test_merge_refuses_failed_page():
    p2 = copy.deepcopy(PAGE2)
    p2['rows_printed'] = 3
    case = make_case([PAGE1, p2])
    try:
        rd, msgs = rp.merge(case)
        assert rd is None and any('ページ 2: 不合格' in m for m in msgs), msgs
        rd, msgs = rp.merge(case, force=True)
        assert rd is not None
    finally:
        shutil.rmtree(case, ignore_errors=True)


def test_merge_rejects_page_number_mismatch():
    p2 = copy.deepcopy(PAGE2); p2['page'] = 1  # page_2.json の中身が page: 1（コピーして直し忘れ）
    case = make_case([PAGE1, PAGE2])
    try:
        rp.save_json(os.path.join(rp.pages_dir(case), 'page_2.json'), p2)
        rd, msgs = rp.merge(case)
        assert rd is None and any('中身の page 1 が違う' in m for m in msgs), msgs
        rd, msgs = rp.merge(case, force=True)  # 構造エラーは --force でも束ねない
        assert rd is None and any('--force でも束ねない' in m for m in msgs), msgs
    finally:
        shutil.rmtree(case, ignore_errors=True)


def test_validate_printed_header_numbers():
    h = copy.deepcopy(HEADER); h['labor_rate'] = '8,000'; h['wage_round'] = ''
    r = rp.validate_page(h, PAGE1)
    assert r['ok'], r


def test_merge_gap_in_pages():
    p3 = copy.deepcopy(PAGE2); p3['page'] = 3
    case = make_case([PAGE1, p3])
    try:
        rd, msgs = rp.merge(case)
        assert rd is None and any('連続していない' in m for m in msgs), msgs
        rd, msgs = rp.merge(case, force=True)  # 欠番は --force でも束ねない
        assert rd is None, msgs
    finally:
        shutil.rmtree(case, ignore_errors=True)


def test_merge_totals_fail_reported():
    h = copy.deepcopy(HEADER)
    h['totals']['parts'] = 90000  # 合計欄の写し違い（ページは合っている）
    case = make_case([PAGE1, PAGE2], h)
    try:
        assert rp.cmd_validate(case, None) == 0
        assert rp.cmd_merge(case, False) == 1  # ページは合格、全体の合計欄で FAIL
        st = rp.load_json(os.path.join(rp.pages_dir(case), 'status.json'))
        assert st['1']['ok'] and st['2']['ok']
        assert rp.cmd_status(case) == 0
    finally:
        shutil.rmtree(case, ignore_errors=True)


def test_init_creates_skeleton():
    case = tempfile.mkdtemp(prefix='rp_test_')
    try:
        assert rp.cmd_init(case, 2) == 0
        assert sorted(os.listdir(rp.pages_dir(case))) == ['header.json', 'page_1.json', 'page_2.json']
        rp.save_json(os.path.join(rp.pages_dir(case), 'page_1.json'), PAGE1)
        assert rp.cmd_init(case, 2) == 0  # 既存を上書きしない
        assert rp.load_json(os.path.join(rp.pages_dir(case), 'page_1.json'))['rows_printed'] == 3
    finally:
        shutil.rmtree(case, ignore_errors=True)


def test_merge_detects_duplicate_expense_and_paint_line():
    """同じ費用行・塗装行が 2 ページに写っていたら二重計上として知らせる（監査 12）"""
    p2 = copy.deepcopy(PAGE2)
    p3 = {'page': 3, 'rows_printed': 0, 'subtotal': {'parts': 0, 'wage': 0}, 'blocks': [],
          'paint_lines': copy.deepcopy(p2['paint_lines']), 'expenses': copy.deepcopy(p2['expenses'])}
    case = make_case([PAGE1, p2, p3])
    try:
        rd, msgs = rp.merge(case, force=True)
        assert rd is not None, msgs
        joined = ' / '.join(msgs)
        assert '費用の同じ行が 2 回ある' in joined, joined
        assert '塗装行の同じ行が 2 回ある' in joined, joined
    finally:
        shutil.rmtree(case, ignore_errors=True)


def test_merge_no_duplicate_message_when_unique():
    case = make_case([PAGE1, PAGE2])
    try:
        rd, msgs = rp.merge(case, force=False)
        assert rd is not None, msgs
        assert not [m for m in msgs if '同じ行が' in m], msgs
    finally:
        shutil.rmtree(case, ignore_errors=True)


def test_init_creates_case_dir():
    """init は案件フォルダごと作る（親が無いときは打ち間違いとみて止める）"""
    import subprocess
    import sys as _sys
    root = tempfile.mkdtemp(prefix='rp_root_')
    case = os.path.join(root, '新しい案件')
    r = subprocess.run([_sys.executable, os.path.join(os.path.dirname(HERE), 'reading_pages.py'), 'init', case, '--pages', '1'],
                       capture_output=True, text=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.path.exists(os.path.join(case, 'pages', 'header.json')), '雛形が作られていない'
    r2 = subprocess.run([_sys.executable, os.path.join(os.path.dirname(HERE), 'reading_pages.py'), 'init', os.path.join(root, 'a', 'b'), '--pages', '1'],
                        capture_output=True, text=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert r2.returncode != 0, '親フォルダが無いのに作ってしまう'
    shutil.rmtree(root, ignore_errors=True)


def test_merge_refuses_ocr_unverified_rows_even_with_force():
    """OCR の下書き行（comment が「OCR未確認」）は --force でも束ねない（見ずに通す経路を塞ぐ。Codex 指摘）"""
    import json, os, tempfile, shutil
    from reading_pages import merge
    d = tempfile.mkdtemp(prefix='rp_force_')
    try:
        pages = os.path.join(d, 'pages'); os.makedirs(pages)
        json.dump({'vehicle': {}, 'labor_rate': 8000}, open(os.path.join(pages, 'header.json'), 'w', encoding='utf-8'))
        json.dump({'page': 1, 'blocks': [{'title': '', 'rows': [
            {'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000, 'comment': 'OCR未確認'}]}],
                   'subtotal': {}}, open(os.path.join(pages, 'page_1.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        rd, msgs = merge(d, force=True)
        assert rd is None, '--force で OCR 未確認の行を束ねている'
        assert any('OCR 未確認' in m for m in msgs), f'理由が出ていない（{msgs}）'
        # 短縮記法（文字列の行）でも同じ（Codex 指摘: dict の行だけ見ていると文字列行がすり抜ける）
        json.dump({'page': 1, 'blocks': [{'title': '', 'rows': ['ﾌｰﾄﾞ|取替|1|50000|OCR未確認']}],
                   'subtotal': {}}, open(os.path.join(pages, 'page_1.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        rd, msgs = merge(d, force=True)
        assert rd is None, '--force で文字列形式の OCR 未確認行を束ねている'
        assert any('OCR 未確認' in m for m in msgs), f'理由が出ていない（{msgs}）'
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
    print('reading_pages tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
