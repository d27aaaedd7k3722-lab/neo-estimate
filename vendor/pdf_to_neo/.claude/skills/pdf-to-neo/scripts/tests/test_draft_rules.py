# -*- coding: utf-8 -*-
"""draft_estimate.py の写し取り規則の単体テスト（ADDATA 不要の純関数だけ）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_draft_rules.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import draft_estimate as de  # noqa: E402
import reading_check as rc  # noqa: E402


def test_note_flag_keeps_comment():
    """N（注記行）は短縮記法でも dict 行でも comment を残す（監査 15）"""
    a = de.expand_row('|ｲﾝﾃﾘｼﾞｪﾝﾄｸﾘｱﾗﾝｽｿﾅｰ|||||||N|装備の条件')
    b = de.expand_row({'name': 'ｲﾝﾃﾘｼﾞｪﾝﾄｸﾘｱﾗﾝｽｿﾅｰ', 'flags': 'N', 'comment': '装備の条件'})
    assert a == b, (a, b)
    assert a.get('note') and a.get('comment') == '装備の条件', a
    assert 'price' not in a and 'wage' not in a, a


def test_comment_is_a_memo_and_neo_prefix_is_a_neo_comment():
    """comment は転記メモ（NEO に書かない）。見積書に印字された明細コメントは 'NEO:' を付ける（全角 ＮＥＯ： も可）。dict 行は neo_comment（2026-09-13）"""
    a = de.expand_row('0010|ﾊﾞﾝﾊﾟ|取替||1.0|1|1000|8000||NEO:※JAS在庫使用')
    assert a.get('neo_comment') == '※JAS在庫使用' and 'comment' not in a, a
    b = de.expand_row('0010|ﾊﾞﾝﾊﾟ|取替||1.0|1|1000|8000||ＮＥＯ：ﾊﾞﾝﾊﾟｰ交換に含む')
    assert b.get('neo_comment') == 'ﾊﾞﾝﾊﾟｰ交換に含む', b  # 本文の半角カナは写したまま
    c = de.expand_row('0010|ﾊﾞﾝﾊﾟ|取替||1.0|1|1000|8000||名称だけだと別の部品に寄るので指定')
    assert c.get('comment') == '名称だけだと別の部品に寄るので指定' and 'neo_comment' not in c, c
    d = de.expand_row({'name': 'ﾊﾞﾝﾊﾟ', 'neo_comment': '※再使用', 'comment': 'メモ'})
    assert d.get('neo_comment') == '※再使用' and d.get('comment') == 'メモ', d


def test_note_flag_without_comment():
    a = de.expand_row('|ｸﾘｱﾗﾝｽｿﾅｰ|||||||N|')
    assert a == {'note': 'ｸﾘｱﾗﾝｽｿﾅｰ'}, a


def test_area_rounds_not_floors():
    """板金面積の小数は切り捨てない（4.5 → 4 だとランクが 1 段軽くなる）"""
    assert de._area_of('左Fﾄﾞｱﾊﾟﾈﾙ(4.5d㎡)', {}) == 5
    assert de._area_of('', {'area': '4.5'}) == 5
    assert de._area_of('左Fﾄﾞｱﾊﾟﾈﾙ(4dm2)', {}) == 4
    assert de._area_of('左Fﾄﾞｱﾊﾟﾈﾙ', {}) is None


def test_unknown_flag_is_error():
    try:
        de.expand_row('|ﾊﾞﾝﾊﾟ|取替|||1|1000||X|')
    except ValueError as e:
        assert 'flags' in str(e), e
    else:
        raise AssertionError('未知の flags が通ってしまう')


def test_issuer_key_cuts_tel_without_space():
    """工場名の鍵: TEL・電話・FAX・住所が空白なしで続いても切る（監査 15）"""
    assert rc.issuer_key('テスト鈑金工業TEL092-000-0000') == 'テスト鈑金工業'
    assert rc.issuer_key('テスト鈑金工業 tel:092-000-0000') == 'テスト鈑金工業'
    assert rc.issuer_key('テスト鈑金工業（福岡県…）') == 'テスト鈑金工業'
    assert rc.issuer_key('テスト鈑金工業 福岡県福岡市1-2-3') == 'テスト鈑金工業'
    assert rc.issuer_key('スチール鈑金') == 'スチール鈑金'  # 語中の 'tel' で切らない


def test_side_of():
    assert de._side_of('RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ') == 'R'
    assert de._side_of('【左 フロントドア】') == 'L'
    assert de._side_of('ﾘﾔｺﾝﾋﾞﾈｰｼｮﾝﾗﾝﾌﾟ LH') == 'L'
    assert de._side_of('Rrﾄﾞｱﾊﾟﾈﾙ') == '', 'Rr はリヤであって右ではない'
    assert de._side_of('Rﾄﾞｱﾊﾟﾈﾙ') == '', '区切り無しの R は右かリヤか決められない'


def test_qty_zero_is_error_not_one():
    """数量 0 を黙って 1 にしない（印字どおりに写し、静かに直さない）"""
    import json
    import tempfile
    rd = {'source': 't', 'issuer': '', 'est_date': '20260908', 'format': 'B', 'labor_rate': 8000,
          'vehicle': {'model_code': 'X', 'generic': True, 'car_code': 'Z10', 'car_name': 'テスト'},
          'blocks': [{'title': 'テスト', 'rows': ['|ﾊﾞﾝﾊﾟ|取替|52119-11111||0|50000|12000||']}],
          'paint': {}, 'expenses': [], 'totals': {}}
    d = tempfile.mkdtemp()
    rp = os.path.join(d, 'reading.json')
    json.dump(rd, open(rp, 'w', encoding='utf-8'), ensure_ascii=False)
    try:
        de.Drafter(rd).build()
    except ValueError as e:
        assert '数量' in str(e), e
    else:
        raise AssertionError('数量 0 が通ってしまう')


def test_money_or_rejects_non_numeric():
    """'12OOO' のような写し間違いを 0 円にしない"""
    assert de._money_or('12,000') == 12000
    assert de._money_or('') == 0
    assert de._money_or('', 10) == 10
    try:
        de._money_or('12OOO')
    except ValueError as e:
        assert '数値' in str(e), e
    else:
        raise AssertionError('数値でない金額が通ってしまう')


def test_fr_of_and_fr20():
    """前後（フロント / リヤ）の読み取り。左右の取り違え検知に使う"""
    assert de._fr_of('Fﾊﾞﾝﾊﾟﾌｪｲｽ') == 'F'
    assert de._fr_of('ﾌﾛﾝﾄﾄﾞｱﾊﾟﾈﾙ') == 'F'
    assert de._fr_of('Rrﾊﾞﾝﾊﾟ') == 'R'
    assert de._fr_of('ﾘﾔｺﾝﾋﾞﾈｰｼｮﾝﾗﾝﾌﾟ') == 'R'
    assert de._fr_of('ﾎﾞﾝﾈｯﾄ') == ''
    assert de._fr_of('RH ﾌｫｸﾞﾗﾝﾌﾟ ASSY') == '', 'RH は左右記号であってリヤではない'
    assert de._fr_of('LH ﾘﾔｺﾝﾋﾞﾈｰｼｮﾝﾗﾝﾌﾟ') == 'R'
    assert de._fr_of('LFﾄﾞｱﾊﾟﾈﾙ') == 'F'
    assert de._fr_of('RRﾄﾞｱﾊﾟﾈﾙ') == 'R'
    assert de._fr_of('Rﾄﾞｱﾊﾟﾈﾙ') == '', '単独の R は右かリヤか決められない'
    assert de._fr_of('右 ﾌﾛﾝﾄﾀﾞﾝﾊﾟｰﾕﾆｯﾄ') == 'F'
    assert de._fr20('LFﾄﾞｱﾊﾟﾈﾙ') == 'F'
    assert de._fr20(' Rﾊﾞﾝﾊﾟﾌｴｲｽ') == 'R'
    assert de._fr20('ﾎﾞﾝﾈﾂﾄ') == ''


def test_side20():
    assert de._side20('LFﾄﾞｱﾊﾟﾈﾙ') == 'L'
    assert de._side20('RFﾄﾞｱﾊﾟﾈﾙ') == 'R'
    assert de._side20(' Rﾊﾞﾝﾊﾟﾌｴｲｽ') == '', '2 文字目の R は前後であって左右ではない'


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
    print('draft_rules tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
