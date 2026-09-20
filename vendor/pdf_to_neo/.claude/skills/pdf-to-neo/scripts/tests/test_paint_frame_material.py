# -*- coding: utf-8 -*-
"""塗装: 内板骨格塗装の振り分けと、材料代の割合 × 端数処理（2026-09-20）。ADDATA 不要（純関数だけ）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_paint_frame_material.py
"""
from __future__ import annotations

import os
import sys
import unicodedata

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import draft_estimate as de  # noqa: E402
from estimate_to_neo import material_default, material_round_of  # noqa: E402


def _n(s: str) -> str:
    """下書きが塗装行の名前に当てるのと同じ正規化（半角カナ → NFKC → 空白を詰める）"""
    return unicodedata.normalize('NFKC', de._hw_kana(s)).replace(' ', '')


def test_paint_frame_line():
    """印字の塗装明細の行 → コグニの内板骨格タブの欄と区分"""
    cases = [
        ('ラジエータサポート 両側新品', ('engine_room', 1)),
        ('ﾗｼﾞｴｰﾀｻﾎﾟｰﾄ 両側新品または修正', ('engine_room', 1)),
        ('ラジエタサポート', ('engine_room', 1)),
        ('フロントフェンダエプロン 片側新品または修正', ('engine_room', 2)),
        ('Fフェンダエプロン 両側新品', ('engine_room', 3)),
        ('フロントピラー 片側新品', ('front_pillar', 1)),
        ('Fﾋﾟﾗｰ 両側新品', ('front_pillar', 2)),
        ('センタピラー 片側新品', ('center_pillar', 1)),
        ('ｾﾝﾀｰﾋﾟﾗｰ 両側新品', ('center_pillar', 2)),
        ('リヤフロア 1台小修正', ('rear_floor', 1)),
        ('ﾘﾔﾌﾛｱﾊﾟﾝ １台大修正', ('rear_floor', 2)),
        # 内板骨格にしてはいけないもの
        ('内板調色', None),                      # 追加項目（PaintingOther）
        ('フロントピラー 修理 1/2', None),        # 外板パネル（区分の語が無い）
        ('左 ｸｵｰﾀﾊﾟﾈﾙ 修理 1/3', None),
        ('加算基礎数値', None),
        ('ブース', None),
    ]
    bad = [(s, de.paint_frame_line(_n(s)), want) for s, want in cases if de.paint_frame_line(_n(s)) != want]
    assert not bad, bad


def test_material_default_rounding():
    """材料代 = 塗装工賃計 × 割合。丸めは工場ごと（既定 10 円四捨五入）"""
    assert material_default(15500, 15) == 2330          # コグニ実機 NEW3
    assert material_default(18950, 15) == 2840          # double で 2842.4999… → 2,840（切り上げなら 2,850）
    assert material_default(46490, 14) == 6510
    assert material_default(80230, 28) == 22460
    assert material_default(80230, 28, '10円切り上げ') == 22470      # 実案件（10 円切り上げの工場）
    assert material_default(75240, 26, {'unit': 1, 'mode': '切り上げ'}) == 19563   # 1 円単位の工場
    assert material_default(127466, 16, {'unit': 1, 'mode': '四捨五入'}) == 20395  # JPN タクシー
    assert material_round_of(None) == (10, '四捨五入')
    assert material_round_of('1円四捨五入') == (1, '四捨五入')
    assert material_round_of({'unit': 100, 'mode': '切り捨て'}) == (100, '切り捨て')
    for bad in ('5円四捨五入', {'unit': 10, 'mode': '切り上'}, {'unit': 'あ'}):
        try:
            material_round_of(bad)
        except ValueError:
            continue
        raise AssertionError(f'{bad!r} は止めるべき')


def test_bumper_only_paint_allows_the_new_paint_keys():
    """バンパだけ塗装（panels 空 + bumper_*）の許可リストに、新しい欄（material_round / input_type）が入っている
    （入っていないと一括計上に落ちるか、生成器が止まる。Codex 指摘）"""
    from estimate_to_neo import is_bumper_only_paint
    p = {'paint': '2K', 'coat': 'メタリック', 'panels': [], 'total': 10400,
         'bumper_front': {'method': '新品', 'color': '一色', 'index': 1.3, 'wage': 10400},
         'material_rate': 26, 'material_round': {'unit': 10, 'mode': '切り上げ'}, 'input_type': '指数'}
    assert is_bumper_only_paint(p), p


def test_material_mode():
    """印字の材料代から 割合（整数 %）と端数処理を割り出す"""
    assert de.material_mode(15500, 2330) == (15, None)                                   # 既定の丸めなら端数処理は書かない
    assert de.material_mode(80230, 22470) == (28, {'unit': 10, 'mode': '切り上げ'})
    assert de.material_mode(75240, 19563) == (26, {'unit': 1, 'mode': '切り上げ'})
    assert de.material_mode(127466, 20395) == (16, {'unit': 1, 'mode': '四捨五入'})
    assert de.material_mode(100000, 26000) == (26, None)
    assert de.material_mode(100000, 26000, rate=26) == (26, None)                        # 印字の割合を確かめる使い方
    assert de.material_mode(100000, 26000, rate=30) is None                              # 印字の割合では戻らない
    assert de.material_mode(0, 1000) is None and de.material_mode(1000, 0) is None
    # 割合で説明できない材料代（一式の見積など）は None
    assert de.material_mode(263640, 65065) is None


def main() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                fails += 1
                print('FAIL', name, e)
    print('paint_frame_material tests:', 'all ok' if not fails else f'{fails} failed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
