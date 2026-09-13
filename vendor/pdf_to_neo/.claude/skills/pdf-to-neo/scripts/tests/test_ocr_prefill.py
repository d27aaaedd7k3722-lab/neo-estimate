# -*- coding: utf-8 -*-
"""ocr_prefill.py の純粋関数（語の結合・列見出し・行分類・車両欄推定）の単体テスト。OCR 本体（Windows.Media.Ocr）は呼ばない
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_ocr_prefill.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ocr_prefill as op  # noqa: E402


def W(x, text, y=100, w=None, h=30, line=0):
    return {'line': line, 'x': x, 'y': y, 'w': w if w is not None else 14 * len(text), 'h': h, 'text': text}


def test_join_amount_and_pn():
    row = [W(2500, '83'), W(2530, ','), W(2545, '400'), W(1400, '52119'), W(1480, 'ー'), W(1500, '58988'), W(1580, 'ー'), W(1600, 'AO')]
    toks = op.join_tokens(sorted(row, key=lambda w: w['x']))
    texts = [t['text'] for t in toks]
    assert '52119-58988-A0' in texts, texts
    assert '83,400' in texts, texts


def test_join_pn_I_to_1():
    row = [W(1400, '73350'), W(1480, '-'), W(1495, 'T6A'), W(1545, '-'), W(1560, 'JI1')]
    toks = op.join_tokens(row)
    assert toks[0]['text'] == '73350-T6A-J11', toks


def test_group_rows():
    words = [W(100, 'a', y=100), W(400, 'b', y=104), W(100, 'c', y=180), W(700, 'd', y=182)]
    rows = op.group_rows(words)
    assert [[w['text'] for w in r] for r in rows] == [['a', 'b'], ['c', 'd']], rows


def test_find_header_positions():
    hdr = [W(200, '修理項目/部品名称', y=50), W(1400, '修理方法/部品番号/指数', y=50), W(2300, '部品価格(円)', y=50), W(2800, '工賃(円)', y=50)]
    h = op.find_header([hdr])
    assert h and 2300 < h['cols']['price'] < 2300 + 14 * len('部品価格(円)'), h  # 見出し語「部品価格」の中心（語の中）
    assert 'wage' in h['cols'] and h['cols']['wage'] > h['cols']['price'], h
    assert 'name' in h['cols'] and h['cols']['name'] < h['cols']['parts_no'], h


def test_find_header_merged_word():
    hdr = [W(200, '修理項目/部品名称', y=50), W(2300, '部品価格(円)工賃(円)', y=50, w=700)]  # OCR が 2 列の見出しを 1 語にした
    h = op.find_header([hdr])
    assert h and h['cols']['wage'] - h['cols']['price'] > 250, h


def test_classify_with_header():
    header = {'cols': {'name': 400, 'parts_no': 1500, 'index': 2200, 'qty': 2350, 'price': 2600, 'wage': 2950}, 'y': 50}
    row = [W(198, '0010'), W(409, 'Frﾊﾞﾝﾊﾟﾌｪｲｽ'), W(1182, '取替'), W(1467, '71100-T6A-Z10ZB'), W(2221, '2.00'), W(2330, '1'), W(2537, '83,400'), W(2913, '22,000'), W(3050, '$')]
    c = op.classify_row(row, header, 3200)
    assert c['code'] == '0010' and c['parts_no'] == '71100-T6A-Z10ZB' and c['index'] == '2.00' and c['qty'] == '1', c
    assert c['price'] == '83400' and c['wage'] == '22000' and c['flags'] == '$' and c['method'] == '取替', c


def test_classify_wage_without_wage_header():
    header = {'cols': {'name': 400, 'price': 2488}, 'y': 50}  # 「工賃」が読めなかった見出し（実ページの幾何: 幅 3100、部品価格 cx≈2580、工賃 cx≈2960）
    row = [W(409, 'ﾘﾔｸｵｰﾀ'), W(1182, '板金'), W(2913, '222,900')]
    c = op.classify_row(row, header, 3100)
    assert c['wage'] == '222900' and c['price'] == '', c
    row2 = [W(409, 'ﾎﾞﾃﾞｨｺｰﾄ'), W(2537, '45,800'), W(2913, '11,000')]
    c2 = op.classify_row(row2, header, 3100)
    assert c2['price'] == '45800' and c2['wage'] == '11000', c2


def test_classify_without_header():
    row = [W(409, 'ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ'), W(1467, '91505-TM8-003'), W(2330, '20'), W(2537, '3,100')]
    c = op.classify_row(row, None, 3200)
    assert c['qty'] == '20' and c['price'] == '3100' and c['wage'] == '', c
    assert op.classify_row([W(409, 'ただの文字')], None, 3200) is None


def test_header_guess():
    t = '車台番号 RC4 ー 1000001 型式 6AA-RC4 型式指定 18732 類別 0012 カラー No 070 初度登録 令和 3 年 12 月 御見積額 1,332,012 排気量 2000CC'
    g = op.header_guess(t)
    assert g.get('serial_no') == 'RC4-1000001', g
    assert g.get('desig') == '18732' and g.get('category') == '0012', g
    assert g.get('color_code') == '070' and g.get('reg_date') == 'R3.12', g
    assert g.get('total') == 1332012, g
    assert g.get('model_code_raw') == '6AA-RC4', g


def test_amount_fragments():
    assert not op.AMOUNT_RE.match('00') and not op.AMOUNT_RE.match('000')
    assert op.AMOUNT_RE.match('3,100') and op.AMOUNT_RE.match('910') and op.AMOUNT_RE.match('0') and op.AMOUNT_RE.match('04') and op.AMOUNT_RE.match('1')


def test_subtotal_guess():
    rows = [[W(100, 'ページ小計', y=3000), W(2500, '363,370', y=3000), W(2900, '63,800', y=3000)], [W(100, '部品計', y=3100), W(2500, '718,420', y=3100)]]
    g = op.subtotal_guess(rows)
    assert g['subtotal_line'] == [[363370, 63800]] and g['parts'] == 718420, g


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
    print('ocr_prefill tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
