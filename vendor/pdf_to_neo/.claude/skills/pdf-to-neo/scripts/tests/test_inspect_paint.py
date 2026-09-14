# -*- coding: utf-8 -*-
"""inspect_estimate.py の塗装の検算（内板骨格塗装・追加項目・材料代の対象）の単体テスト（架空の見積。ADDATA 不要）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_inspect_paint.py
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import skill_env  # noqa: E402

skill_env.apply()
import inspect_estimate as ie  # noqa: E402


def _est(**paint):
    p = {'paint': '2K', 'coat': '2コートパール', 'hf': 'しない', 'material_rate': 31.0,
         'panels': [{'code': '1000', 'name': 'RFﾌｪﾝﾀﾞ', 'method': '取替', 'index': 1.7, 'wage': 14880}],
         'base': {'index': 3.7, 'wage': 32380}, 'frame': {'engine_room': {'option': 2, 'index': 1.5, 'wage': 13130}},
         'other': [{'name': 'プライマー塗装', 'index': 1.7, 'wage': 14880}]}
    p.update(paint)
    return {'items': [{'code': '1000', 'name': 'x', 'method': '取替', 'qty': 1, 'price': 30000, 'wage': 8750}], 'paint': p, 'expenses': [],
            'totals': {}}


def test_frame_paint_counts_in_material_base():
    """内板骨格塗装は位置ごとの入れ子（engine_room: {option, index, wage}）。工賃は材料率の対象に入り、追加項目は入らない（2026-09-14 C-HR）"""
    assert ie.paint_frame_wage({'engine_room': {'option': 2, 'wage': 13130}, 'front_pillar': 1}) == 13130
    assert ie.paint_frame_wage(None) == 0
    base, other = ie._paint_wages(_est()['paint'])
    assert (base, other) == (14880 + 32380 + 13130, 14880), (base, other)


def test_totals_use_components_not_printed_total():
    """印字の塗装工賃計（paint.total）は追加項目を含まない。検算は明細から 塗装工賃 = 材料率の対象 + 追加項目、材料代 = 対象 × 割合 で出す"""
    est = _est(total=60390)          # 60,390 = 14,880 + 32,380 + 13,130（追加項目を含まない印字の塗装工賃計）
    mat = ie.material_default(60390, 31.0)
    est['totals'] = {'material': mat}
    warn, rep = [], {}
    with redirect_stdout(io.StringIO()):
        ie._totals(est, warn, rep)
    assert rep['totals']['paint'] == 60390 + 14880, rep['totals']
    assert rep['totals']['material'] == mat, rep['totals']
    assert not any('material 未指定' in w for w in warn), warn     # 割合モード（印字の材料代 = 対象 × 割合）は要確認にしない
    est2 = _est(total=0)
    est2['totals'] = {'material': mat + 10}                             # 印字の材料代と合わないときだけ挙げる
    warn2 = []
    with redirect_stdout(io.StringIO()):
        ie._totals(est2, warn2, {})
    assert any('material 未指定' in w for w in warn2), warn2


def test_lump_paint_adds_other():
    """一括計上（panels なし）でも追加項目は total の外（生成器は一括の塗装費用に追加項目を足す）"""
    est = _est(total=100000)
    est['paint'].pop('panels'); est['paint'].pop('base'); est['paint'].pop('frame')   # 一括計上は panels キー自体が無い（panels: [] はバンパだけの詳細塗装）
    rep = {}
    with redirect_stdout(io.StringIO()):
        ie._totals(est, [], rep)
    assert rep['totals']['paint'] == 100000 + 14880, rep['totals']



def test_total_from_lines_not_double_counted():
    """wage の無いパネルがあって total を使うとき、下書きが塗装行から作った total（追加項目にした行も含む）に追加項目をもう一度足さない（Codex 指摘）"""
    p = {'material_rate': 31.0, 'panels': [{'code': '1000', 'name': 'RFﾌｪﾝﾀﾞ', 'method': '取替', 'index': 1.7}],
         'total': 50000, '_total_from_lines': 8000, 'other': [{'name': 'ｱﾝﾀﾞｰｺｰﾄ', 'wage': 8000}]}
    assert ie.paint_base_from_total(p) == 42000
    est = {'items': [], 'paint': p, 'expenses': [], 'totals': {}}
    rep, warn = {}, []
    with redirect_stdout(io.StringIO()):
        ie._totals(est, warn, rep)
    # 下書きが塗装行から作った total は、工賃の無い項目があると一部しか足していないので信じない（明細から数え、参考値と注意する。Codex 指摘）
    assert rep['totals']['paint'] == 8000 and any('参考値' in w for w in warn), (rep['totals'], warn)
    p2 = dict(p); p2.pop('_total_from_lines')                        # 印字の total は追加項目を含まない → 足す
    rep2 = {}
    with redirect_stdout(io.StringIO()):
        ie._totals({'items': [], 'paint': p2, 'expenses': [], 'totals': {}}, [], rep2)
    assert rep2['totals']['paint'] == 58000, rep2['totals']



def test_bumper_only_detailed_paint():
    """バンパだけの詳細塗装（panels: [] + bumper_front）は一括計上ではなく明細から数える（total が無くてもバンパ工賃と材料代を入れる。Codex 指摘）"""
    p = {'material_rate': 30.0, 'panels': [], 'bumper_front': {'method': '新品', 'color': '一色', 'index': 1.7, 'wage': 14000}}
    rep = {}
    with redirect_stdout(io.StringIO()):
        ie._totals({'items': [], 'paint': p, 'expenses': [], 'totals': {}}, [], rep)
    assert rep['totals']['paint'] == 14000 and rep['totals']['material'] == ie.material_default(14000, 30.0), rep['totals']
    p['bumper_base'] = {'index': 0.6, 'wage': 4800}                  # バンパ加算基礎も塗装工賃（Codex 指摘）
    rep = {}
    with redirect_stdout(io.StringIO()):
        ie._totals({'items': [], 'paint': p, 'expenses': [], 'totals': {}}, [], rep)
    assert rep['totals']['paint'] == 18800, rep['totals']



def test_printed_paint_wage_and_missing_wage_fallback():
    """合計欄の塗装工賃（印字の塗装工賃計）は追加項目を含まないので、それと一致すれば要確認にしない。
    パネル以外の詳細項目（加算基礎など）が指数だけで工賃が無いときは、印字の total を使う（明細の合算は過少。Codex 指摘）"""
    est = _est(total=60390)
    est['totals'] = {'paint': 60390, 'material': ie.material_default(60390, 31.0)}
    warn = []
    with redirect_stdout(io.StringIO()):
        ie._totals(est, warn, {})
    assert not any('塗装工賃' in w for w in warn), warn
    est2 = _est(total=60390)
    est2['paint']['base'] = {'index': 3.7}                          # 工賃なし（生成器がレートで補う）
    assert ie.paint_wage_missing(est2['paint'])
    assert ie.paint_wage_missing({'panels': [{'code': '1000', 'index': 1.7, 'wage': 0}]})   # 工賃 0 / 空も工賃なし
    assert ie.paint_wage_missing({'panels': [{'code': '1000', 'index': 1.7, 'wage': ''}]})
    assert ie.paint_wage_missing({'panels': [], 'bumper_front': {'method': '新品'}})           # 指数も工賃も無いバンパ（標準で補われる）
    assert not ie.paint_wage_missing({'panels': [{'code': '1000', 'index': 1.7, 'wage': 14880}], 'booth': {'index': 0.0, 'wage': 0}})   # ブース 0 は本当に 0
    rep = {}
    with redirect_stdout(io.StringIO()):
        ie._totals(est2, [], rep)
    assert rep['totals']['paint'] == 60390 + 14880, rep['totals']



def test_detailed_predicate_and_synthetic_total():
    """panels: [] は生成器と同じくバンパだけの形のときだけ詳細塗装（frame などが混じれば一括扱い）。
    下書きが塗装行から作った total（_total_from_lines）は、工賃の無い項目があっても信じない（参考値の注意を出す。Codex 指摘）"""
    assert ie.is_bumper_only_paint({'panels': [], 'bumper_front': {'wage': 1}, '_total_from_lines': 0})
    assert not ie.is_bumper_only_paint({'panels': [], 'bumper_front': {'wage': 1}, 'frame': {'engine_room': 1}})
    p = {'material_rate': 31.0, 'panels': [{'code': '1000', 'name': 'x', 'method': '取替', 'index': 1.7}, {'code': '2300', 'name': 'y', 'method': '取替', 'wage': 20000}],
         'total': 20000, '_total_from_lines': 0}
    warn, rep = [], {}
    with redirect_stdout(io.StringIO()):
        ie._totals({'items': [], 'paint': p, 'expenses': [], 'totals': {}}, warn, rep)
    assert any('参考値' in w for w in warn), warn



def test_tax_round_and_target_material():
    """消費税は estimate の tax_round（切り捨て等）で出す。協定額に合わせた材料代（_material_from_target）は既定値と違っても要確認にしない（2026-09-14 JPN タクシー）"""
    est = {'items': [{'code': '1000', 'name': 'x', 'method': '取替', 'qty': 1, 'price': 1045455}], 'paint': {}, 'expenses': [], 'tax_round': '切り捨て',
           'totals': {'tax': 104545, 'total': 1150000}}
    warn, rep = [], {}
    with redirect_stdout(io.StringIO()):
        ie._totals(est, warn, rep)
    assert rep['totals']['tax'] == 104545 and not any('消費税' in w or '合計' in w for w in warn), (rep['totals'], warn)


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
    print('inspect_paint tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
