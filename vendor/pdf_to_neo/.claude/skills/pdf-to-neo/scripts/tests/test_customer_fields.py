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
from estimate_to_neo import REG_NO_RE, split_address_text  # noqa: E402


def test_reg_no_division_letters_and_one_digit():
    for text, want in (('北九州 30A な 12', ('北九州', '30A', 'な', '12')),
                       ('横浜 3ZX あ 1', ('横浜', '3ZX', 'あ', '1')),
                       ('福岡 5 な 12', ('福岡', '5', 'な', '12')),          # 旧式の 1 桁
                       ('品川 346 の 1224', ('品川', '346', 'の', '1224')),
                       ('練馬 500 さ 12-34', None),                            # 一連番号のハイフン入りは従来どおり不可
                       ('福岡 300 あ', None)):
        m = REG_NO_RE.match(text)
        got = m.groups() if m else None
        assert got == want, (text, got)


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
