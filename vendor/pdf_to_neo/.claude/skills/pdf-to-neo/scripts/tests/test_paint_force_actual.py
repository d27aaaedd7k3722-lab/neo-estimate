# -*- coding: utf-8 -*-
"""塗装: reading が入力方式「実額」を**指定した**ときは、内訳があっても実額で入れる（2026-09-20 亮平さん指示）。
総額 ＝ 塗装工賃計 ＋ 追加項目 ＋ 材料代。内板骨格塗装・ボデーシーリングは別の欄なので畳まない。ADDATA 不要
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_paint_force_actual.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import draft_estimate as de  # noqa: E402


class _D(de.Drafter):
    """Drafter の塗装まわりだけを使う（読み取りの dict を差し替えて呼ぶ）"""
    def __init__(self, rd):
        self.rd = rd
        self.notes = []
        self.generic = False          # 汎用車種ではない（paint() が見る）
        self.pi = None                # 20.DB（塗装パネル一覧）は使わない
        self._rev_rows = []

    def _rev(self, *a, **kw):         # 確認箇所シートの行（テストでは捨てる）
        self._rev_rows.append((a, kw))


def test_forced_actual_uses_printed_totals():
    """基準は印字の合計欄（塗装計（材料込）− 材料代）。下書きの total より優先する"""
    rd = {'paint': {'input_type': '実額'}, 'totals': {'paint_total': 85520, 'material': 19226}}
    out = {'total': 60000, 'panels': [{'name': 'ﾄﾞｱ'}], 'material_rate': 29}
    got = _D(rd)._paint_input_type(out)
    assert got.get('total') == 85520 - 19226, got   # 塗装工賃計 66,294（生成器が材料代を足して 85,520）
    assert got.get('material') == 19226, got


def test_forced_actual_keeps_detail_when_total_unknown():
    """印字の塗装計が無く、独立した工賃を持つ内訳（バンパ・加算基礎 等）があるときは畳まない（金額を動かさない）"""
    rd = {'paint': {'input_type': '実額'}, 'totals': {}}
    out = {'total': 50000, 'panels': [{'name': 'ﾄﾞｱ'}], 'bumper_front': {'wage': 20000}}
    d = _D(rd)
    got = d._paint_input_type(out)
    assert got.get('panels') and got.get('bumper_front'), got
    assert any('畳まない' in n for n in d.notes), d.notes


def test_forced_actual_folds_details():
    """パネル・追加項目・材料代があっても、指定されたら実額 1 本に畳む"""
    rd = {'paint': {'input_type': '実額'}, 'totals': {}}
    out = {'total': 100000, 'material': 20000, 'material_rate': 20,
           'panels': [{'name': 'ﾄﾞｱ'}], 'lines': [{'name': 'ﾄﾞｱ', 'wage': 100000}],
           'other': [{'name': 'ｱﾝﾀﾞｰｺｰﾄ', 'wage': 5000}]}
    got = _D(rd)._paint_input_type(out)
    assert got.get('input_type') == '実額', got
    assert got.get('total') == 105000, got          # 塗装工賃計 + 追加項目（材料代は生成器が総額に足す）
    assert got.get('material') == 20000, got
    for k in ('panels', 'lines', 'other', 'material_rate'):
        assert k not in got, (k, got)


def test_forced_actual_folds_booth_when_printed_total_is_there():
    """ブース・加算基礎があっても、印字の塗装工賃計があればその額で畳める"""
    rd = {'paint': {'input_type': '実額'}, 'totals': {'paint': 120000}}
    out = {'total': 100000, 'panels': [{'name': 'ﾄﾞｱ'}], 'booth': {'time': 0.5}, 'base': {'count': 2}}
    got = _D(rd)._paint_input_type(out)
    assert got.get('total') == 120000 and 'booth' not in got and 'base' not in got, got


def test_forced_actual_does_not_double_count_other():
    """塗装行から作った total は追加項目の工賃をすでに含むので、畳むときに足し直さない（二重計上しない）"""
    rd = {'paint': {'input_type': '実額'}, 'totals': {}}
    out = {'total': 200000, '_total_from_lines': 30000, 'lines': [{'name': '塗装費用', 'wage': 170000}],
           'other': [{'name': '塗装費用', 'wage': 30000}]}
    got = _D(rd)._paint_input_type(out)
    assert got.get('total') == 200000, got     # 30,000 を足し直さない
    assert 'other' not in got and '_total_from_lines' not in got, got


def test_forced_actual_restores_material_from_totals():
    """費用割合モードで額を落としていたら、印字の材料代（合計欄）を戻す（実額は割合を持てない）"""
    rd = {'paint': {'actual': True}, 'totals': {'material': 19226}}
    out = {'total': 66294, 'material_rate': 29, 'panels': [{'name': 'ﾄﾞｱ'}]}
    got = _D(rd)._paint_input_type(out)
    assert got.get('input_type') == '実額', got
    assert got.get('material') == 19226, got
    assert 'material_rate' not in got and 'panels' not in got, got


def test_forced_actual_keeps_frame_and_sealing():
    """内板骨格塗装・ボデーシーリングは塗装計とは別の欄なので畳まない（残っていれば生成器は今までどおり一括計上）"""
    rd = {'paint': {'input_type': '実額'}, 'totals': {}}
    out = {'total': 50000, 'frame': {'engine_room': 1}, 'sealing': {'wage': 3000}, 'panels': [{'name': 'ﾄﾞｱ'}]}
    got = _D(rd)._paint_input_type(out)
    assert got.get('frame') and got.get('sealing'), got
    assert 'panels' not in got, got


def test_index_request_is_untouched():
    """「指数」の指定は畳まない（人の指定が優先）"""
    rd = {'paint': {'input_type': '指数'}, 'totals': {}}
    out = {'total': 50000, 'panels': [{'name': 'ﾄﾞｱ'}], 'material': 10000}
    got = _D(rd)._paint_input_type(out)
    assert got.get('input_type') == '指数' and got.get('panels') and got.get('material') == 10000, got


def test_auto_rule_is_unchanged():
    """自動で実額を選ぶ規則は変えない: 一式だけなら実額、材料代が別に出ていれば実額にしない"""
    d1 = _D({'paint': {}, 'totals': {}})
    got1 = d1._paint_input_type({'total': 99080})
    assert got1.get('input_type') == '実額', got1
    d2 = _D({'paint': {}, 'totals': {}})
    got2 = d2._paint_input_type({'total': 85520, 'material': 19226})
    assert 'input_type' not in got2, got2


def test_material_line_not_in_wage_total():
    """「塗装材料代」の行は塗装工賃計にも材料代にも二重に入らない（印字の塗装計が無い見積。Codex 第27周）"""
    rd = {'paint': {}, 'totals': {}, 'blocks': []}
    d = _D(rd)
    d.rd = {'paint': {'lines': [
        {'name': 'ﾙ-ﾌ 修理 (1/2)', 'wage': 20000},
        {'name': '塗装材料代', 'wage': 5000},
    ]}, 'totals': {}, 'blocks': []}
    out = d.paint()
    # 塗装工賃計は材料代の行を含まない／材料代は 5,000
    assert int(out.get('total') or 0) == 20000, out
    assert int(out.get('material') or 0) == 5000, out
    assert not any('材料代' in str((o or {}).get('name') or '') for o in (out.get('other') or [])), out


def test_material_line_name_variants():
    """NFKC 後の全角（ペイント材料代・マテリアル費）も材料代の行として拾う（Codex 第27周）"""
    import draft_estimate as _de
    for n in ('材料代', '材料費', '塗装材料代', 'ペイント材料代', 'マテリアル代', 'ﾏﾃﾘｱﾙ費'):
        assert _de._MATERIAL_LINE_RE.search(n), n
    for n in ('シーリング材料費', '材料代の内訳', '塗装材料代金'):
        assert not _de._MATERIAL_LINE_RE.search(n), n


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
    print('paint_force_actual tests:', 'all ok' if not fails else f'{fails} failed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
