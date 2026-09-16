# -*- coding: utf-8 -*-
"""顧客欄の分割規則（生成器 estimate_to_neo）の単体テスト。2026-09-15 アプリのバグハントで判明:
- 登録番号の分類番号は 2〜3 桁のほか、英字入り（30A・3ZX。希望番号）と旧式の 1 桁も受ける（受けないと 4 欄すべて空になる）
- 1 本の住所文字列の分割は「四日市市」「蒲郡市」を切り違えるので、構造化された住所（車検証 OCR）は
  customer.prefecture / municipality / address_other で渡し、生成器はそのまま使う

    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_customer_fields.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import skill_env  # noqa: E402

skill_env.apply()
import draft_estimate as _de  # noqa: E402,F401  claude_neo_pipeline を sys.path に載せる
from estimate_to_neo import REG_NO_RE, split_address_text, split_reg_no  # noqa: E402
import neo_container as nc  # noqa: E402


def test_reg_no_division_letters_and_one_digit():
    for text, want in (('北九州 30A な 12', ('北九州', '30A', 'な', '12')),
                       ('横浜 3ZX あ 1', ('横浜', '3ZX', 'あ', '1')),
                       ('福岡 5 な 12', ('福岡', '5', 'な', '12')),          # 旧式の 1 桁
                       ('品川 346 の 1224', ('品川', '346', 'の', '1224')),
                       ('福岡 300 あ', None)):
        m = REG_NO_RE.match(text)
        got = m.groups() if m else None
        assert got == want, (text, got)


def test_split_reg_no_plate_notation():
    """一連番号はプレートの表記（10-31・・・12・・123）でも受けて数字だけに（2026-09-15: ハイフン区切りで 4 欄が空になっていた）"""
    for text, want in (('北九州 539 な 10-31', ('北九州', '539', 'な', '1031')),
                       ('北九州 539 な ・・12', ('北九州', '539', 'な', '12')),
                       ('品川 300 あ ・123', ('品川', '300', 'あ', '123')),
                       ('練馬 500 さ 12-34', ('練馬', '500', 'さ', '1234')),
                       ('北九州 30A な 1', ('北九州', '30A', 'な', '1')),
                       ('福岡 300 あ 12345', None),
                       ('福岡 300 あ', None)):
        assert split_reg_no(text) == want, (text, split_reg_no(text))


def test_xml_and_ini_values_are_literal():
    """値の \\1・\\x・& < > を正規表現の後方参照や XML の記号として解釈しない。INI の値の改行で行を増やさない"""
    t = '<A>old</A><B/>'
    assert nc.replace_xml_tag(t, 'A', '\\12345') == '<A>\\12345</A><B/>'
    assert nc.replace_xml_tag(t, 'A', 'C:\\x & <y>') == '<A>C:\\x &amp; &lt;y&gt;</A><B/>'
    assert nc.replace_xml_tag(t, 'B', 'z') == '<A>old</A><B>z</B>'
    ini = 'Key=old\r\nNext=1'
    assert nc.replace_ini_value(ini, 'Key', '\\1a\r\nX=2') == 'Key=\\1a X=2\r\nNext=1'


def test_xml_owner_user_fit_like_db():
    """XML の所有者名・使用者名も DB と同じ 20 バイトで切る（XML だけ 50 バイトのまま出ていた。2026-09-15 アプリのレビュー）"""
    from estimate_to_neo import NeoBuilder
    xml = '<OwnerName></OwnerName><UserName></UserName><CustomerName1></CustomerName1>'.encode('cp932')
    out = NeoBuilder.build_xml(xml, {}, {'name': 'ア' * 25, 'owner_name': 'イ' * 25, 'user_name': 'ウ' * 25}, {}, 0, '20260915').decode('cp932')
    assert '<OwnerName>' + 'イ' * 10 + '</OwnerName>' in out, out
    assert '<UserName>' + 'ウ' * 10 + '</UserName>' in out, out
    assert '<CustomerName1>' + 'ア' * 15 + '</CustomerName1>' in out, out


def test_split_address_text_known_shape():
    assert split_address_text('福岡県北九州市小倉北区検証町1-2-3') == ('福岡県', '北九州市', '小倉北区検証町1-2-3')
    assert split_address_text('東京都千代田区丸の内1-1') == ('東京都', '千代田区', '丸の内1-1')
    assert split_address_text('') == ('', '', '')


def test_split_address_text_limitation_is_documented():
    """最短一致の癖（四日市市 → 四日市 / 市日永）は残す（構造化された住所は write_ansvem が別経路で使う）"""
    assert split_address_text('三重県四日市市日永1-1') == ('三重県', '四日市', '市日永1-1')


def main() -> int:
    ng = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith('test_')):
        try:
            fn()
            print('ok  ', name)
        except AssertionError as e:
            print('FAIL', name, e)
            ng += 1
    print('customer_fields tests:', 'all ok' if not ng else f'{ng} 件 NG')
    return 1 if ng else 0


if __name__ == '__main__':
    sys.exit(main())
