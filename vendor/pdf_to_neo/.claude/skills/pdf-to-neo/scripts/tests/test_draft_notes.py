# -*- coding: utf-8 -*-
"""下書きが出す「見落とすと静かに誤る」注記の単体テスト（ADDATA が要る。無い PC では自動でスキップ）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_draft_notes.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import skill_env  # noqa: E402

skill_env.apply()
sys.path.insert(0, os.path.join(skill_env.files_root(), 'claude_neo_pipeline') if hasattr(skill_env, 'files_root') else
                os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE)))), 'claude_neo_pipeline'))
import draft_estimate as de  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': 'YR586P'}


def notes_for(row):
    rd = {'source': 't', 'issuer': '', 'est_date': '20260909', 'format': 'A', 'labor_rate': 8000, 'vehicle': dict(VEH),
          'blocks': [{'title': 'テスト', 'rows': [row]}], 'paint': {}, 'expenses': [], 'totals': {}}
    return de.Drafter(rd).build().get('_draft_notes') or []


def has(notes, word):
    return any(word in n for n in notes)


def test_side_mismatch_is_noticed():
    """名称が右なのに左の部品コードを指定したら知らせる（合計は合うので検算では見つからない）"""
    assert has(notes_for('0400|右ﾍｯﾄﾞﾗｲﾄ|取替|||1|50000|4000||'), '左右が食い違う')


def test_front_rear_mismatch_is_noticed():
    assert has(notes_for('3810|Fﾊﾞﾝﾊﾟﾌｪｲｽ|取替|||1|50000|4000||'), '前後が食い違う')


def test_side_ok_is_quiet():
    assert not has(notes_for('0400|左ﾍｯﾄﾞﾗｲﾄ|取替|||1|50000|4000||'), '食い違う')
    assert not has(notes_for('0010|Fﾊﾞﾝﾊﾟﾌｪｲｽ|取替|||1|50000|4000||'), '食い違う')


def test_price_on_bankin_row_is_noticed():
    """板金・修理の行に部品代があるのは写し間違い（実 NEO 211 本に 1 例も無い）"""
    assert has(notes_for('0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|板金|||1|30000|8000||'), '部品代')
    assert has(notes_for('0010|Fﾊﾞﾝﾊﾟﾌｪｲｽ|修理|||1|30000|8000||'), '部品代')


def test_price_on_detach_row_is_noticed():
    """脱着・脱着修理に写し間違えた行も部品代付きなら知らせる（実 NEO では脱着の部品代は 2 行だけ）"""
    assert has(notes_for('0400|左ﾍｯﾄﾞﾗｲﾄ|脱着|||1|30000|8000||'), '部品代')
    assert has(notes_for('0400|左ﾍｯﾄﾞﾗｲﾄ|脱着修理|||1|30000|8000||'), '部品代')


def test_front_rear_from_block_title():
    """行名に前後が無くても、ブロック見出しから前後を補って食い違いを見つける"""
    rd = {'source': 't', 'issuer': '', 'est_date': '20260909', 'format': 'A', 'labor_rate': 8000, 'vehicle': dict(VEH),
          'blocks': [{'title': 'フロントバンパー', 'rows': ['3810|ﾊﾞﾝﾊﾟﾌｪｲｽ|取替|||1|50000|4000||']}],
          'paint': {}, 'expenses': [], 'totals': {}}
    import draft_estimate as _de
    assert has(_de.Drafter(rd).build().get('_draft_notes') or [], '前後が食い違う')


def test_price_on_replace_row_is_quiet():
    assert not has(notes_for('0010|Fﾊﾞﾝﾊﾟﾌｪｲｽ|取替|||1|30000|8000||'), '部品代')


def test_bad_code_is_error():
    """reading の部品コードが 4 桁の数字でなければ止める（'12OOO' が 12 に化けると別の部品になる）"""
    for bad in ('12OOO', '12,9', 'A05', '-1'):
        rd = {'source': 't', 'issuer': '', 'est_date': '20260909', 'format': 'A', 'labor_rate': 8000, 'vehicle': dict(VEH),
              'blocks': [{'title': 'テスト', 'rows': [{'code': bad, 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'qty': 1, 'price': 50000, 'wage': 4000}]}],
              'paint': {}, 'expenses': [], 'totals': {}}
        try:
            de.Drafter(rd).build()
        except ValueError as e:
            assert '部品コード' in str(e), e
        else:
            raise AssertionError(f'部品コード {bad!r} が通ってしまう')


def test_good_code_is_accepted():
    for good, want in (('0010', '0010'), (10, '0010'), ('10', '0010'), ('  ', '0010'), (None, '0010')):  # 空白だけの欄は品番で照合
        rd = {'source': 't', 'issuer': '', 'est_date': '20260909', 'format': 'A', 'labor_rate': 8000, 'vehicle': dict(VEH),
              'blocks': [{'title': 'テスト', 'rows': [{'code': good, 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'parts_no': '71101-TY0-000ZS', 'qty': 1, 'price': 50000, 'wage': 4000}]}],
              'paint': {}, 'expenses': [], 'totals': {}}
        assert de.Drafter(rd).build()['items'][0]['code'] == want, good


def _draft(reading: dict) -> dict:
    """reading を下書きして estimate を返す（Drafter を直接呼ぶ）"""
    return de.Drafter(reading).build()


def _base_reading(paint_lines: list) -> dict:
    return {'source': 't', 'issuer': 't', 'est_date': '20260910', 'format': 'A',
            'vehicle': {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075',
                        'category': '0061', 'reg_date': 'H28.10', 'color_code': 'YR586P'},
            'customer': {}, 'insurance': {}, 'labor_rate': 8000,
            'blocks': [{'title': '', 'rows': [{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟ', 'method': '取替', 'qty': 1, 'price': 10000}]}],
            'paint': {'paint': '2K', 'coat': 'ソリッド', 'hf': 'しない', 'lines': paint_lines},
            'expenses': [], 'totals': {}}


def test_sealing_goes_to_its_own_key():
    """ボデーシーリングは paint.sealing に振り分ける（paint.other に落とすと塗装工賃計から漏れる）。
    表記ゆれ（全角/半角カナ、長音とハイフン）も拾う"""
    for nm in ('ボデーシーリング 10.00m', 'ﾎﾞﾃﾞｰｼｰﾘﾝｸﾞ 10.00m', 'ﾎﾞﾃﾞ-ｼ-ﾘﾝｸﾞ 10.00m', 'ﾎﾞﾃﾞｨｰｼｰﾘﾝｸﾞ 10.00m'):
        e = _draft(_base_reading([{'name': '左 ﾌﾛﾝﾄﾄﾞｱﾊﾟﾈﾙ 取替', 'wage': 20000}, {'name': nm, 'wage': 9700}]))
        p = e['paint']
        assert 'sealing' in p, f'{nm}: paint.sealing に入っていない（{sorted(p)}）'
        assert p['sealing'].get('m') == 10.0, f"{nm}: 長さを読めていない（{p['sealing']}）"
        assert not any('ｼ' in str(o.get('name', '')) or 'シ' in str(o.get('name', '')) for o in p.get('other') or []),             f'{nm}: paint.other にも残っている'


def test_sealing_is_dropped_when_paint_is_lump_sum():
    """パネルが 1 件も取れない（一括計上）ときは sealing を落とす。
    残すと paint.total（全塗装行の合計）と二重に乗る"""
    e = _draft(_base_reading([{'name': '存在しないパネル 取替', 'wage': 6000}, {'name': 'ボデーシーリング 5.00m', 'wage': 4000}]))
    p = e['paint']
    assert 'panels' not in p, '一括計上のはずがパネルができている'
    assert 'sealing' not in p, '一括計上なのに sealing が残っている（二重計上になる）'
    assert p.get('total') == 10000, f"塗装計が塗装行の合計と違う（{p.get('total')}）"


def test_paint_panel_area_comes_from_addata():
    """塗装パネルの面積は 20.DB の値。見積書に印字される dm² は塗装面積で別物なので入れない"""
    e = _draft(_base_reading([{'name': '左 ﾌﾛﾝﾄﾄﾞｱﾊﾟﾈﾙ 取替', 'area': 999, 'wage': 20000}]))
    pn = (e['paint'].get('panels') or [{}])[0]
    assert pn.get('area') and pn['area'] != 999, f'見積書の塗装面積をパネル面積に入れている（{pn}）'


def test_sealing_does_not_swallow_material_cost_rows():
    """『シーリング材料費』のような費用系の行は paint.sealing にしない
    （sealing は生成側で BSealing として付加塗装に加算されるので、意味が変わる）"""
    # 「材」「剤」「費用」はどこに出ても材料側。長さが付いていても作業にしない
    for nm in ('シーリング材料費', 'ｼｰﾘﾝｸﾞ材', 'シーリング費用', 'シーリング材 10.00m', 'ボデーシーリング剤'):
        p = _draft(_base_reading([{'name': '左 ﾌﾛﾝﾄﾄﾞｱﾊﾟﾈﾙ 取替', 'wage': 20000},
                                  {'name': nm, 'wage': 5000}]))['paint']
        assert 'sealing' not in p, f'{nm} を paint.sealing にしている'
        assert len(p.get('other') or []) == 1, f'{nm} が paint.other に残っていない（{p.get("other")}）'


def test_sealing_without_length_is_still_sealing():
    """長さの印字が無くても『ボデーシーリング』なら sealing（生成側が既定の 1m で計算する）"""
    for nm in ('ﾎﾞﾃﾞ-ｼ-ﾘﾝｸﾞ', 'ﾎﾞﾃﾞｨｰｼｰﾘﾝｸﾞ'):
        p = _draft(_base_reading([{'name': '左 ﾌﾛﾝﾄﾄﾞｱﾊﾟﾈﾙ 取替', 'wage': 20000}, {'name': nm, 'wage': 5000}]))['paint']
        assert 'sealing' in p, f'{nm} が paint.sealing に入っていない'
        assert 'm' not in p['sealing'], f'{nm}: 印字の無い長さを作っている（{p["sealing"]}）'


def test_unmatched_body_part_becomes_manual_paint_row():
    """20.DB のパネルに無い部位（ルーフサイド等）は 手入力の塗装行（panels[].manual。DisposalCode 9・材料代の対象）、
    工程の名前（アンダーコート・内板調色 …）は 追加項目（paint.other。材料代の対象外）"""
    p = _draft(_base_reading([{'name': '左 ﾌﾛﾝﾄﾄﾞｱﾊﾟﾈﾙ 取替', 'wage': 20000},
                              {'name': 'Rrﾎﾞﾃﾞｰﾌﾛｱ 修理', 'index': 3.0, 'wage': 24000},
                              {'name': 'ｱﾝﾀﾞｰｺｰﾄ', 'wage': 5000},
                              {'name': '内板調色', 'wage': 3000}]))['paint']
    man = [x for x in p['panels'] if x.get('manual')]
    assert len(man) == 1 and man[0]['name'] == 'Rrﾎﾞﾃﾞｰﾌﾛｱ' and man[0]['method'] == '修理' and man[0]['index'] == 3.0 and man[0]['wage'] == 24000, man
    assert sorted(o['name'] for o in p.get('other') or []) == sorted(['ｱﾝﾀﾞｰｺｰﾄ', '内板調色']), p.get('other')
    assert p['panels'][0].get('code') and not p['panels'][0].get('manual'), p['panels'][0]
    notes = _draft(_base_reading([{'name': '左 ﾌﾛﾝﾄﾄﾞｱﾊﾟﾈﾙ 取替', 'wage': 20000}, {'name': 'ﾙｰﾌｻｲﾄﾞ 修理', 'wage': 6000}]))['_draft_notes']
    assert any('名前が近いだけ' in n for n in notes), notes  # 近いだけの対応付けは知らせる


def test_manual_paint_row_needs_a_matched_panel():
    """20.DB のパネルに 1 行も対応付けできないときは、全部を手入力の塗装行にせず従来どおり一括計上（名前の書き方が違う書式の疑い）"""
    p = _draft(_base_reading([{'name': 'Rrﾎﾞﾃﾞｰﾌﾛｱ 修理', 'wage': 24000}, {'name': 'ｽﾃｯﾌﾟ', 'wage': 6000}]))['paint']
    assert 'panels' not in p and p.get('total') == 30000, p


def _auto_reading(items, total):
    return {'source': 't', 'issuer': 't', 'est_date': '20260910', 'format': 'A',
            'vehicle': dict(VEH), 'customer': {}, 'insurance': {}, 'labor_rate': 8000,
            'blocks': [{'title': '', 'rows': items}],
            'paint': {'auto_panels': True, 'total': total, 'paint': '2K', 'coat': 'ソリッド', 'hf': 'しない'},
            'expenses': [], 'totals': {'paint': total}}


def _auto_wage(items) -> int:
    """その明細で自動計上したときの塗装工賃計を、注記から読む（一式をわざと外して測る）"""
    est = _draft(_auto_reading(items, 1))
    for n in est.get('_draft_notes') or []:
        m = re.search(r'塗装工賃 ([0-9,]+)', n)
        if m:
            return int(m.group(1).replace(',', ''))
    raise AssertionError(f'塗装工賃を測れない: {est.get("_draft_notes")}')


def test_auto_panels_picks_painted_items():
    """paint.auto_panels: 明細の取替・板金・修理行のうち 20.DB にあるパネルだけを起こす。
    脱着・20.DB に無い部品・バンパ（面積 9999）は入れない"""
    items = [
        {'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000},
        {'code': '0800', 'name': '左Fﾌｴﾝﾀﾞ', 'method': '板金', 'index': 2.0, 'wage': 16000},
        {'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟ', 'method': '取替', 'qty': 1, 'price': 30000},
        {'code': '2450', 'name': '左Fﾄﾞｱﾐﾗｰ', 'method': '脱着', 'wage': 800},
    ]
    est = _draft(_auto_reading(items, int(_auto_wage(items) * 1.5)))
    codes = {p['code'] for p in (est['paint'].get('panels') or [])}
    assert '0600' in codes, f'取替のフードがパネルに入っていない（{codes}）'
    assert '0800' in codes, f'板金のフェンダがパネルに入っていない（{codes}）'
    assert '0010' not in codes, 'バンパ（面積 9999）をパネルに入れている'
    assert '2450' not in codes, '脱着行をパネルに入れている'
    m = {p['code']: p['method'] for p in est['paint']['panels']}
    assert m['0600'] == '取替' and m['0800'] == '修理', f'修理方法の写像が違う（{m}）'


def test_auto_panels_refuses_qty_over_one():
    """数量 2 以上の塗装対象行があるときは自動で起こさない。
    コグニは左右別コードなので数量 2 は塗装パネル 2 枚。黙って 1 枚にすると塗装工賃が過少になり、
    その差を材料代へ移してしまう（工場の金額を動かさないという約束が崩れる）"""
    items = [{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 2, 'price': 100000}]
    est = _draft(_auto_reading(items, 90000))
    assert not est['paint'].get('panels'), '数量 2 の行からパネルを起こしている'
    notes = ' '.join(est.get('_draft_notes') or [])
    assert '数量 2 以上' in notes, f'理由が注記に出ていない（{est.get("_draft_notes")}）'


def test_auto_panels_merges_same_panel_and_prefers_replacement():
    """同じパネルに取替行と板金行が両方あるときは 1 枚にまとめ、「取替」で起こす。
    行の並び順で修理方法が変わってはいけない"""
    swap = {'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000}
    bank = {'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '板金', 'index': 2.0, 'wage': 16000}
    for items in ([swap, bank], [bank, swap]):
        est = _draft(_auto_reading(items, int(_auto_wage(items) * 1.5)))
        panels = est['paint'].get('panels') or []
        got = [(x['code'], x['method']) for x in panels]
        assert len(panels) == 1, f'同じパネルを {len(panels)} 枚起こしている（{got}）'
        assert got[0] == ('0600', '取替'), f'並び順で修理方法が変わっている（{got}）'


def test_auto_panels_refuses_when_material_is_printed():
    """工場見積に材料代の内訳が印字されているのに auto_panels が付いていたら、一括計上に戻して知らせる。
    材料代で差を吸収すると工場の材料代を黙って書き換えることになる（合計は合うのに中身が違う。Codex 指摘）"""
    items = [{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000}]
    rd = _auto_reading(items, 90000)
    rd['paint']['material'] = 20000          # 印字された材料代
    est = _draft(rd)
    assert not est['paint'].get('panels'), '材料代が印字されているのにパネルを起こしている'
    notes = ' '.join(est.get('_draft_notes') or [])
    assert '材料代' in notes and '★' in notes, f'理由が ★ で出ていない（{est.get("_draft_notes")}）'


def test_auto_panels_fits_factory_total_with_material():
    """起こしたパネルの塗装工賃と工場見積の一式の差は材料代で埋め、合計欄の内訳も直す"""
    items = [{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000}]
    want = int(_auto_wage(items) * 1.5)
    est = _draft(_auto_reading(items, want))
    p = est['paint']
    assert p.get('panels'), 'パネルが起きていない'
    assert p.get('material') and p.get('total'), f'材料代・塗装工賃計が決まっていない（{p.get("material")}, {p.get("total")}）'
    assert int(p['total']) + int(p['material']) == want, f'塗装計が工場の一式と違う（{p["total"]} + {p["material"]}）'
    t = est['totals']
    assert int(t['paint']) == int(p['total']) and int(t['material']) == int(p['material']), '合計欄の内訳が直っていない'


def test_auto_panels_backs_off_when_material_is_unreal():
    """材料代が塗装工賃に対して極端になる案件（明細に塗装対象が足りない）はパネルを起こさず一括のまま"""
    items = [{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000}]
    est = _draft(_auto_reading(items, _auto_wage(items) * 20))   # 一式が塗装工賃の 20 倍 = 材料代 1900%
    assert not est['paint'].get('panels'), '材料代が非常識な割合でもパネルを起こしている'
    assert any('材料代' in n and '%' in n for n in est.get('_draft_notes') or []), '理由の注記が出ていない'


def test_auto_panels_falls_back_when_total_is_missing():
    """工場の一式（paint.total）が無いときは詳細塗装のまま進めず、一括計上に戻す
    （材料代を決められないのに panels だけ残すと、生成器の既定率で工場と違う塗装計になる）"""
    rd = _auto_reading([{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000}], 0)
    rd['paint'].pop('total', None)
    rd['totals'] = {}
    est = _draft(rd)
    assert not est['paint'].get('panels'), '一式が無いのにパネルを残している'
    assert any('一括計上に戻す' in n for n in est.get('_draft_notes') or []), '理由の注記が出ていない'


def test_auto_panels_sets_material_rate_to_the_real_ratio():
    """材料代割合は実態に合わせる（入力に古い割合が書いてあっても上書きする）"""
    items = [{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000}]
    want = int(_auto_wage(items) * 1.5)
    rd = _auto_reading(items, want)
    rd['paint']['material_rate'] = 99.9      # 実態と違う値を先に置いておく
    p = _draft(rd)['paint']
    assert p.get('panels'), 'パネルが起きていない'
    real = round(int(p['material']) * 100.0 / int(p['total']), 1)
    assert abs(float(p['material_rate']) - real) < 0.05, f'割合が実態と違う（{p["material_rate"]} / 実態 {real}）'


def test_auto_panels_does_not_touch_the_input_reading():
    """下書きは入力の reading を書き換えない（呼び出し元の dict を汚さない）"""
    import copy
    items = [{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000}]
    rd = _auto_reading(items, int(_auto_wage(items) * 1.5))
    before = copy.deepcopy(rd)
    est = _draft(rd)
    assert est['paint'].get('panels'), 'パネルが起きていない'
    assert rd['totals'] == before['totals'], f'入力の totals を書き換えている（{before["totals"]} → {rd["totals"]}）'
    assert int(est['totals']['paint']) != int(before['totals'].get('paint') or 0),         '返した estimate 側の内訳が直っていない'


def test_auto_panels_backoff_drops_every_detail_key():
    """一括計上に戻すときは panels だけでなく詳細塗装キーを全部落とす
    （生成器は panels があるときだけ詳細塗装を書くので、残すと生成が落ちる）"""
    items = [{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000}]
    rd = _auto_reading(items, _auto_wage(items) * 20)   # 材料代が非常識な割合になる一式
    rd['paint']['bumper_front'] = {'method': '新品', 'color': '一色'}
    rd['paint']['wax'] = {'count': 2}
    est = _draft(rd)
    p = est['paint']
    left = [k for k in ('panels', 'base', 'booth', 'wax', 'sealing', 'bumper_front', 'bumper_rear') if k in p]
    assert not left, f'一括計上に戻したのに詳細キーが残っている: {left}'
    assert p.get('total'), '一括計上の塗装計が無い'


def test_auto_panels_backoff_when_no_panel_matches():
    """20.DB のパネルが 1 枚も起きない案件（バンパだけ等）でも、詳細塗装キーを残さず一括計上に戻す"""
    rd = _auto_reading([{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟ', 'method': '取替', 'qty': 1, 'price': 30000}], 50000)
    rd['paint']['bumper_front'] = {'method': '新品', 'color': '一色'}
    rd['paint']['wax'] = {'count': 2}
    rd['paint']['frame'] = {'front_pillar': 1}
    rd['paint']['other'] = [{'name': '内板調色', 'wage': 5000}]
    rd['paint']['material'] = 12345
    est = _draft(rd)
    p = est['paint']
    keep = ('total', 'paint', 'coat', 'hf', 'auto_panels', 'note')
    left = [k for k in p if k not in keep]
    assert not left, f'一括計上に戻したのに塗装の内訳が残っている: {left}'
    assert p.get('total'), '一括計上の塗装計が無い'
    # 一括計上の塗装計は工場の一式そのもの（材料代を足して過大にしない）
    assert int(est['totals'].get('paint_total') or est['totals'].get('paint') or 0) <= 50000,         f"一括計上なのに塗装計が一式を超えている（{est['totals']}）"


def test_panel_body_is_only_a_tie_breaker():
    """ボディは同名パネルの選び分けにだけ使い、採用するかどうかは名称の近さだけで決める
    （ボディ加点で採用しきい値を越えさせない）"""
    d = de.Drafter({'source': 't', 'issuer': '', 'est_date': '20260911', 'format': 'A', 'labor_rate': 8000,
                    'vehicle': dict(VEH), 'blocks': [], 'paint': {}, 'expenses': [], 'totals': {}})
    if not d.pi:
        print('     （20.DB を読めないので検査しない）')
        return
    assert d._panel_code('ｽﾞﾚﾀﾅﾏｴﾉﾊﾟﾈﾙ') is None, '名前が遠いのにパネルとして採っている'
    # 20.DB にある名前で引けること（名前は車種の 20.DB からそのまま取る）
    nm = next((x['name'].strip() for x in d.pi.panels if x['area'] not in (0, 9999)), '')
    assert nm, '20.DB にパネルが無い'
    got = d._panel_code(nm)
    assert got, f'20.DB にある名前 {nm!r} で引けない'


def test_bumper_only_paint_keeps_detail():
    """外板パネルが無くバンパだけ塗る見積は、一括計上に戻さず `panels: []` + bumper_front を残す
    （生成器がパネル無しのバンパ塗装として書く。実機 2026-09-12 W66 cogni_W66w）。
    付加塗装や対応付けできない行が混じるときは従来どおり一括計上"""
    rd = _base_reading([{'name': 'Fﾊﾞﾝﾊﾟ 新品', 'index': 2.0, 'wage': 16000}])
    est = _draft(rd)
    p = est['paint']
    assert p.get('panels') == [], f'panels: [] が残っていない（{p}）'
    assert p.get('bumper_front', {}).get('method') == '新品', p.get('bumper_front')
    assert 'bumper_rear' not in p
    # ﾜｯｸｽ（付加塗装）が混じる → 一括計上（実機未確認の組合せは書かない）
    rd2 = _base_reading([{'name': 'Fﾊﾞﾝﾊﾟ 新品', 'index': 2.0, 'wage': 16000}, {'name': 'ﾜｯｸｽ処理 2', 'index': 0.2, 'wage': 1600}])
    p2 = _draft(rd2)['paint']
    assert 'panels' not in p2 and 'bumper_front' not in p2 and p2.get('total'), p2
    # 入力済みの paint.other / frame が混じる → 一括計上（Codex 指摘: 解析中の other だけでなく入力のキーも見る）
    for extra in ({'other': [{'name': '内板調色', 'wage': 5000}]}, {'frame': {'front_pillar': 1}}):
        rd3 = _base_reading([{'name': 'Fﾊﾞﾝﾊﾟ 新品', 'index': 2.0, 'wage': 16000}])
        rd3['paint'].update(extra)
        p3 = _draft(rd3)['paint']
        assert 'panels' not in p3 and 'bumper_front' not in p3 and p3.get('total'), (extra, p3)



J87_READING = {'source': 't', 'issuer': 't', 'est_date': '20260913', 'format': 'F', 'vehicle': dict(VEH), 'customer': {}, 'insurance': {}, 'labor_rate': 8000,
               'paint': {}, 'expenses': [], 'totals': {}}


def _j87(rows: list) -> dict:
    return _draft(dict(J87_READING, blocks=[{'title': '', 'page': 2, 'rows': rows}]))


def test_quantity_from_price():
    """数量 1 のまま複数個分の金額（標準単価の整数倍）が印字された小物は数量を直す。金額はそのまま（判断規則 10-21。2026-09-13 亮平さん指示）"""
    e = _j87(['0178|Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ|取替|||1|1850|||'])
    it = e['items'][0]
    assert it['qty'] == 10 and it['price'] == 1850, it
    rv = [r for r in e['_review'] if r['kind'] == '数量']
    assert rv and rv[0]['level'] == '判断' and rv[0]['row'] == 1 and rv[0]['page'] == 2, e['_review']
    e2 = _j87(['0178|Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ|取替|||1|185|||'])  # 標準どおりなら何もしない
    assert e2['items'][0]['qty'] == 1 and not [r for r in e2['_review'] if r['kind'] == '数量'], e2['_review']
    e3 = _j87(['0178|Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ|取替|||1|200|||'])   # 倍数でなければ数量は変えない（標準価格と違う行として確認箇所へ）
    assert e3['items'][0]['qty'] == 1, e3['items'][0]
    d = de.Drafter(dict(J87_READING, blocks=[]))
    d.QTY_FROM_PRICE_UNIT_MAX = 100   # 高い部品の倍数一致は自動で直さず 要確認 に挙げるだけ（ここでは閾値を下げて確かめる）
    d.rd = dict(J87_READING, blocks=[{'title': '', 'page': 2, 'rows': ['0178|Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ|取替|||1|1850|||']}])
    e4 = d.build()
    assert e4['items'][0]['qty'] == 1 and any(r['kind'] == '数量の可能性' and r['level'] == '要確認' for r in e4['_review']), e4['_review']


def test_quantity_from_price_without_code_or_context():
    """部品コードも部位の文脈も別名辞書も効かない行でも、価格整合（標準の 2.2 倍超）で落とさず数量で合わせる"""
    d = de.Drafter(dict(J87_READING, blocks=[{'title': '', 'rows': ['|Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ|取替|||1|1850|||']}]))
    d._alias_ref = lambda *a, **k: (None, '')   # 別名辞書が無い PC・辞書に無い名称
    it = d.build()['items'][0]
    assert it.get('code') and not it.get('manual') and it['qty'] == 10, it


def test_comment_goes_to_review_not_to_neo():
    """reading の comment は NEO の明細コメントにしない（確認箇所シートへ）。「要確認:」で始まるメモは 要確認"""
    e = _j87(['0178|Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ|取替|||1|185|||要確認: 数量が読めない', '0178|Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ|取替|||1|185|||NEO:※再使用'])
    a, b = e['items']
    assert 'comment' not in a and a.get('_memo'), a
    assert b.get('comment') == '※再使用', b
    rv = [r for r in e['_review'] if r['kind'] == '転記メモ']
    assert len(rv) == 1 and rv[0]['level'] == '要確認' and rv[0]['text'].startswith('数量が読めない'), rv


def test_ditto_wage_row_is_merged():
    """「〃 交換工賃」（技術料だけの続き行）は直前の部品の行にまとめる。「〃（モデリスタ）」は直前の名称を補った別の行"""
    e = _j87([{'name': 'Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ', 'method': '取替', 'qty': 1, 'price': 185},
              {'name': '〃 交換工賃', 'wage': 2400},
              {'name': '〃（ﾓﾃﾞﾘｽﾀ）', 'method': '取替', 'qty': 1, 'price': 9000, 'manual': True}])
    assert len(e['items']) == 2, e['items']
    assert e['items'][0].get('wage') == 2400, e['items'][0]
    assert e['items'][1]['name'].startswith('Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ') and 'ﾓﾃﾞﾘｽﾀ' in e['items'][1]['name'], e['items'][1]
    mg = [r for r in e['_review'] if r['kind'] == '行のまとめ']
    assert mg and mg[0]['row'] == 1 and mg[0]['page'] == 2, e['_review']  # 確認箇所シートの明細 No・ページ


def test_small_part_with_large_wage_is_flagged():
    """1,000 円以下の小物に 1 万円以上の技術料は 要確認（別の行の工賃の写し間違い・工場の書き間違いの疑い）"""
    e = _j87(['0178|Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ|取替|||1|185|19200||'])
    assert any(r['kind'] == '小物に大きな工賃' and r['level'] == '要確認' for r in e['_review']), e['_review']


def _w73_available() -> bool:
    try:
        return de.Drafter(dict(J87_READING, vehicle=dict(W73), blocks=[])).car.get('CarCode') == 'W73'
    except Exception:  # noqa: BLE001  ADDATA に車種が無い・読めない
        return False


W73 = {'model_code': '3BA-VJA300W', 'serial_no': 'VJA300-4126281', 'desig': '20152', 'category': '0069', 'reg_date': 'R5.12', 'color_code': '090'}


def test_price_fixes_the_part_code():
    """品番の無い行で標準単価が合わないとき、名称の近い部品のうち単価が合う（割り切れる）ものへ直す（2026-09-13 ランクル 300: 人が直した行を自動で同じ結果に）"""
    rd = dict(J87_READING, vehicle=dict(W73), labor_rate=12000,
              blocks=[{'title': '', 'rows': ['|ﾗｼﾞｴｰﾀｻｲﾄﾞ(ﾛﾜ)|取替|||1|7000|||', '|左Frﾄﾞｱﾄﾘﾑﾎﾞｰﾄﾞｸﾘｯﾌﾟ|取替|||1|720|||']}])
    if not _w73_available():
        print('   skip test_price_fixes_the_part_code（この ADDATA に W73 が無い）')
        return
    e = _draft(rd)
    a, b = e['items']
    assert a['code'] == '0085' and a['qty'] == 1, a            # ﾗｼﾞｴ-ﾀｸﾞﾘﾙ(ﾛﾜ) 7,000 円（名称近似だと 0077 ｻｲﾄﾞｸﾞﾘﾙ 3,490 円）
    assert b['code'] == '2565' and b['qty'] == 8, b            # LFﾄﾞｱﾄﾘﾑﾎﾞ-ﾄﾞｸﾘﾂﾌﾟ 90 円 × 8（辞書だとｱｳﾀﾐﾗ-ｽｸﾘﾕ）
    assert sum(1 for r in e['_review'] if r['kind'] == '部品コード') == 2, e['_review']


def test_labor_rate_from_standard_index():
    """技術料だけの書式でレートが決まらない（6,000 / 12,000 の両方で説明できる）とき、技術料 ÷ レートが ADDATA の標準指数と一致する行の多いレートを採る"""
    rows = ['|Frﾊﾞﾝﾊﾟｰｶﾊﾞｰ(塗装済み)|取替|||1|34300|49200||', '|ﾌｰﾄﾞ|取替|||1|79100|7200||', '|左 ﾌｰﾄﾞﾋﾝｼﾞ|取替|||1|2360|1200||',
            '|右 ﾌｰﾄﾞﾋﾝｼﾞ|取替|||1|2360|1200||', '|左 ｽﾃｯﾌﾟﾊﾟﾈﾙ|脱着|||||2400||']
    rd = dict(J87_READING, vehicle=dict(W73), blocks=[{'title': '', 'rows': rows}])
    rd.pop('labor_rate')
    if not _w73_available():
        print('   skip test_labor_rate_from_standard_index（この ADDATA に W73 が無い）')
        return
    e = _draft(rd)
    assert e['labor_rate'] == 12000, e['labor_rate']
    assert any(r['kind'] == 'レバーレート' and r['level'] == '判断' for r in e['_review']), e['_review']


def test_paint_total_includes_frame_paint():
    """印字の塗装工賃計には内板骨格塗装（paint.frame の位置ごとの工賃）が入る。塗装行＋内板骨格塗装＝印字なら「塗装計が違う」と言わない（2026-09-14 C-HR）"""
    def notes(total):
        rd = {'source': 't', 'issuer': '', 'est_date': '20260909', 'format': 'A', 'labor_rate': 8000, 'vehicle': dict(VEH),
              'blocks': [{'title': 'テスト', 'rows': ['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||']}],
              'paint': {'paint': '2K', 'coat': '2コートパール', 'hf': 'しない', 'total': total,
                        'lines': [{'name': '左 フロントフェンダパネル 取替', 'wage': 10000}, {'name': '加算基礎数値', 'index': 3.0, 'wage': 24000}],
                        'frame': {'engine_room': {'option': 2, 'index': 1.5, 'wage': 12000}}},
              'expenses': [], 'totals': {}}
        return de.Drafter(rd).build().get('_draft_notes') or []
    assert not has(notes(46000), '塗装計: 印字'), notes(46000)
    assert has(notes(46100), '塗装計: 印字'), notes(46100)



def test_wage_round_1_and_material_rounding():
    """1 円単位の技術料（指数 × 7,820 = 7,038）は丸め 1。材料代が 1 円四捨五入でしか合わないときは割合モードにせず額を渡す（2026-09-14 JPN タクシー）"""
    assert de.detect_wage_round([{'index': 0.9, 'wage': 7038}, {'index': 1.8, 'wage': 14076}], 7820) == 1
    assert de.detect_wage_round([{'index': 0.9, 'wage': 7040}], 7820) == 10
    assert de.detect_wage_round([{'index': 0.9, 'wage': 7038}, {'index': 1.5, 'wage': 11730}], 7820) == 1   # 10 円の倍数になる行があっても 1
    assert de.detect_wage_round([{'index': 0.9, 'wage': 7038}, {'index': 0.3, 'wage': 2350}], 7820) == 10   # 丸めた値の行があれば 1 円ではない
    rd = {'source': 't', 'issuer': '', 'est_date': '20260909', 'format': 'A', 'labor_rate': 8000, 'vehicle': dict(VEH),
          'blocks': [{'title': 'テスト', 'rows': ['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||']}],
          'paint': {'paint': '2K', 'coat': '2コートパール', 'hf': 'しない', 'total': 127466, 'material': 20395, 'material_rate': 16,
                    'lines': [{'name': '左 フロントフェンダパネル 取替', 'wage': 127466}]},
          'expenses': [], 'totals': {}}
    est = de.Drafter(rd).build()
    assert est['paint'].get('material') == 20395, est['paint']      # 10 円丸めは 20,390 なので額のまま
    rd['paint']['material'] = 20390
    assert 'material' not in de.Drafter(rd).build()['paint']        # 10 円丸めと一致すれば割合モード


def test_material_includes_lines_with_only_material():
    """材料の列だけに金額のある塗装行（ショートパーツ・写真代 …）も材料代に入れる。
    印字の材料計と塗装行の材料の合計が一致するときだけ（2026-09-16 アクセラ: 2,000 円足りずに不合格だった）"""
    paint = {'total': 20000, 'material': 8000,
             'lines': [{'name': '塗装一式', 'method': '塗装', 'wage': 20000, 'material': 8000},
                       {'name': 'ｼｮｰﾄﾊﾟｰﾂ', 'material': 1000}, {'name': '写真代', 'material': 1000}]}
    est = est_for(['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||'], {'paint': 20000, 'material': 10000}, paint)
    assert est['paint'].get('material') == 10000, est['paint']
    assert has(est['_draft_notes'], '塗装行の材料の合計'), est['_draft_notes']
    est2 = est_for(['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||'], {'paint': 20000, 'material': 8000}, paint)
    assert est2['paint'].get('material') == 8000, est2['paint']      # 印字の材料計と合わなければ触らない
    assert not has(est2['_draft_notes'], '塗装行の材料の合計'), est2['_draft_notes']
    # 汎用車種（塗装行を組み立てない経路）でも同じにする。片方だけ直ると「検算は合格・生成で不合格」になる
    rd_g = {'source': 't', 'issuer': '', 'est_date': '20260909', 'format': 'A', 'labor_rate': 8000,
            'vehicle': dict(VEH, generic=True),
            'blocks': [{'title': 'テスト', 'rows': ['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||']}],
            'paint': dict(paint), 'expenses': [], 'totals': {'paint': 20000, 'material': 10000}}
    est3 = de.Drafter(rd_g).build()
    assert est3['paint'].get('material') == 10000, est3['paint']
    # 材料欄の写し崩れ（'1,0OO' のような値）があっても落ちない（HEAD は落ちなかった）
    paint_bad = dict(paint, lines=[dict(paint['lines'][0]), {'name': 'ｼｮｰﾄﾊﾟｰﾂ', 'material': '1,0OO'}])
    est4 = est_for(['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||'], {'paint': 20000, 'material': 10000}, paint_bad)
    assert est4['paint'].get('material') == 8000, est4['paint']      # 読めない行があるときは触らない（検算で止まる）


def test_accept_no_goes_to_the_policy_no_field():
    """速報報告書の事故番号・受付番号は、証券番号の欄が空なら証券番号にも入れる（2026-09-16 亮平さん指示）。
    受付番号の欄（FileInfo.AcceptNo）は消さない"""
    def build(ins):
        rd = {'source': 't', 'issuer': '', 'est_date': '20260909', 'format': 'A', 'labor_rate': 8000, 'vehicle': dict(VEH),
              'blocks': [{'title': 'テスト', 'rows': ['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||']}],
              'paint': {}, 'expenses': [], 'totals': {}, 'insurance': dict(ins)}
        return de.Drafter(rd).build()

    est = build({'accept_no': 'A12345-6789012-03'})
    assert est['insurance']['policy_no'] == 'A12345-6789012-03', est['insurance']
    assert est['insurance']['accept_no'] == 'A12345-6789012-03', est['insurance']
    assert has(est['_draft_notes'], '証券番号の欄にも入れた'), est['_draft_notes']
    est2 = build({'accept_no': 'A12345-6789012-03', 'policy_no': '1234-5678'})   # 証券番号が読めていれば上書きしない
    assert est2['insurance']['policy_no'] == '1234-5678', est2['insurance']
    assert not has(est2['_draft_notes'], '証券番号の欄にも入れた'), est2['_draft_notes']
    est3 = build({'accept_no': 'A12345-6789012-0123456789'})                     # 20 バイトを超える番号は要確認に出す
    assert has(est3['_draft_notes'], '切り詰められる'), est3['_draft_notes']
    assert any(r.get('kind') == '証券番号' and r.get('level') == '要確認' for r in est3['_review']), est3['_review']


def est_for(rows, totals, paint=None):
    rd = {'source': 't', 'issuer': '', 'est_date': '20260909', 'format': 'A', 'labor_rate': 8000, 'vehicle': dict(VEH),
          'blocks': [{'title': 'テスト', 'rows': list(rows)}], 'paint': dict(paint or {}), 'expenses': [], 'totals': dict(totals)}
    return de.Drafter(rd).build()


def test_blank_wage_is_zero_when_printed_total_matches():
    """工賃計が印字の工賃の合計と一致する見積: 工賃欄の空欄は 0 円（生成器の標準指数で埋めない）。
    2026-09-16 シエンタ: 空欄 3 行に標準 6.6h / 6.4h / 0.3h が入り工賃が +106,400 円になって不合格だった"""
    rows = ['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||', '0400|左ﾍｯﾄﾞﾗｲﾄ|脱着||||||']
    est = est_for(rows, {'wage': 8000})
    assert [it.get('wage') for it in est['items']] == [8000, 0], est['items']
    assert has(est['_draft_notes'], '工賃欄が空欄'), est['_draft_notes']


def test_blank_price_is_zero_when_printed_total_matches():
    """部品計が印字の部品代の合計と一致する見積: 部品代の空欄は 0 円（生成器の標準価格で埋めない）。
    2026-09-16 シエンタ（Gemini）: 金額の印字が無い「リヤフロアクロスメンバー 基本内」に標準価格が入り部品計が +9,400 円"""
    rows = ['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||', '0400|左ﾍｯﾄﾞﾗｲﾄ|取替|||1||||']
    est = est_for(rows, {'parts': 30000})
    assert [it.get('price') for it in est['items']] == [30000, 0], est['items']
    assert has(est['_draft_notes'], '部品代が空欄'), est['_draft_notes']


def test_blank_price_stays_open_when_total_needs_it():
    """部品計が明細より大きい見積（部品代の欄が無い書式など）は、空欄のまま生成器の標準価格に任せる"""
    est = est_for(['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||', '0400|左ﾍｯﾄﾞﾗｲﾄ|取替|||1||||'], {'parts': 80000})
    assert 'price' not in est['items'][1], est['items'][1]
    assert not has(est['_draft_notes'], '部品代が空欄'), est['_draft_notes']


def test_method_cell_with_extra_text_keeps_the_disposal_word():
    """区分の欄に備考まで入った読み取り（「修正 基本内」「修正 ランク B」）でも区分を取り違えない。
    既定に落ちると、金額の印字が無い内板骨格の行が「取替」になって標準価格・標準指数が入る"""
    assert de._dcode('修正 基本内', 0, 0) == 2 and de._dcode('修正 ランク B', 0, 12000) == 2
    assert de._dcode('取替（部品持込）', 5000, 0) == 0 and de._dcode('脱着 修理', 0, 8000) == 3
    assert de._dcode('板金 3d㎡', 0, 8000) == 6 and de._dcode('修正', 0, 0) == 2
    assert de._dcode('鈑金修正 ランクB', 0, 8000) == 2 and de._dcode('板金修正（一部）', 0, 8000) == 2   # 鈑→板 の正規化で取りこぼさない
    assert de._dcode('脱着鈑金', 0, 8000) == 3 and de._dcode('鈑金 5d㎡', 0, 8000) == 6
    assert de._dcode('基本内', 0, 0) == 0 and de._dcode('', 0, 8000) == 1   # 区分の語が無ければ今までどおり既定


def test_blank_wage_stays_open_when_total_needs_it():
    """工賃計が行の合計より大きい見積（工賃欄の無い書式など）は、空欄のまま生成器の標準指数に任せる"""
    est = est_for(['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||', '0400|左ﾍｯﾄﾞﾗｲﾄ|脱着||||||'], {'wage': 12000})
    assert 'wage' not in est['items'][1], est['items'][1]
    assert not has(est['_draft_notes'], '工賃欄が空欄'), est['_draft_notes']


def test_double_paint_drops_paint_when_wage_total_includes_the_row():
    """塗装の一式が明細の手入力行と paint の両方にある reading: 印字の工賃計に手入力行が入っているなら paint を書かない"""
    rows = ['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||', '|塗装費用||||||40000|M|']
    est = est_for(rows, {'wage': 48000}, {'total': 40000, 'material': 0})
    assert 'paint' not in est, est.get('paint')
    assert len(est['items']) == 2 and has(est['_draft_notes'], '両方にある'), est['_draft_notes']
    est2 = est_for(rows, {'wage': 48000}, {'total': 30000, 'material': 10000})   # 一式（材料込み）で写した手入力行
    assert 'paint' not in est2, est2.get('paint')


def test_double_paint_drops_the_row_when_wage_total_excludes_it():
    """逆に、印字の工賃計に手入力行が入っていない（塗装計が別に印字されている）なら明細の手入力行を外す"""
    rows = ['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||', '|塗装費用||||||40000|M|']
    est = est_for(rows, {'wage': 8000, 'paint': 40000}, {'total': 40000, 'material': 0})
    assert est.get('paint', {}).get('total') == 40000, est.get('paint')
    assert len(est['items']) == 1 and has(est['_draft_notes'], '両方にある'), (est['items'], est['_draft_notes'])


def test_double_paint_left_alone_when_totals_do_not_decide():
    """どちらとも決まらない金額なら黙って片方を消さない（reading_check の二重計上の警告と紙上検算に任せる）"""
    rows = ['0800|左Fﾌｪﾝﾀﾞﾊﾟﾈﾙ|取替|||1|30000|8000||', '|塗装費用||||||40000|M|']
    est = est_for(rows, {'wage': 60000}, {'total': 40000, 'material': 0})
    assert est.get('paint', {}).get('total') == 40000 and len(est['items']) == 2, (est.get('paint'), est['items'])
    assert not has(est['_draft_notes'], '両方にある'), est['_draft_notes']
    est2 = est_for(rows, {'wage': 48000, 'paint': 40000}, {'total': 40000, 'material': 0})  # 塗装計も工賃計も手入力行を含む形
    assert est2.get('paint', {}).get('total') == 40000 and len(est2['items']) == 2, (est2.get('paint'), est2['items'])
    assert not has(est2['_draft_notes'], '両方にある'), est2['_draft_notes']


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
    print('draft_notes tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
