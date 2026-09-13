# -*- coding: utf-8 -*-
"""グレード絞り込みの単体テスト（この PC の ADDATA を使う。案件データは見ない）。
    cd files && python .claude/skills/pdf-to-neo/scripts/tests/test_pick_grade.py
"""
from __future__ import annotations

import contextlib
import io as _io
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pick_grade as g  # noqa: E402
from estimate_to_neo import NeoBuilder  # noqa: E402

# ADDATA だけで完結する見積（N-BOX。型式指定・類別があるので確度は high）
VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061',
       'reg_date': 'H28.10', 'color_code': 'YR586P'}
EST = {'source': 'test_pick_grade', 'issuer': '', 'est_date': '20260909', 'vehicle': VEH,
       'customer': {}, 'insurance': {}, 'labor_rate': 8000, 'paint': {}, 'expenses': [], 'totals': {},
       'items': [{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'qty': 1, 'price': 30000,
                  'wage': 8000, 'index': 1.0}]}


def _run(est):
    import json
    import tempfile
    d = tempfile.mkdtemp(prefix='pg_')
    p = os.path.join(d, 'e.json')
    _io.open(p, 'w', encoding='utf-8').write(json.dumps(est, ensure_ascii=False))
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = g.main([p])
    return rc, buf.getvalue()


def test_confirmed_vehicle_needs_no_pick():
    """型式指定・類別がある案件は確定しているので絞り込みを勧めない"""
    rc, out = _run(EST)
    assert rc == 0, f'確定案件で異常終了している（{out[:80]}）'
    assert '確定している' in out, f'確定案件なのに絞り込もうとしている（{out[:120]}）'


def test_generic_vehicle_is_out_of_scope():
    """汎用車種（二輪・輸入車）は ADDATA に候補が無いので対象外"""
    est = dict(EST)
    est['vehicle'] = {'generic': True, 'car_code': 'Z10', 'maker_code': 'I', 'car_name': 'ﾃｽﾄ'}
    est['items'] = [{'code': '', 'name': 'ﾃｽﾄ部品', 'method': '', 'qty': 1, 'price': 1000,
                     'wage': 0, 'manual': True}]
    rc, out = _run(est)
    assert rc == 1 and '候補が取れない' in out, f'汎用車種の扱いが違う（{out[:120]}）'


def _cands(est):
    nb = NeoBuilder()
    _neo, rep = nb.build(est, est['vehicle'], hints=est.get('hints'), labor_rate=est.get('labor_rate'),
                         est_date=est.get('est_date'), insurance=est.get('insurance') or {})
    return nb, rep, g.candidates(rep)


def test_score_counts_price_matches():
    """score() は「見積金額 = 標準価格 × 数量」の行を数える"""
    nb, _rep, cands = _cands(EST)
    assert cands, '候補を取れていない'
    r = g.score(nb, EST, cands[0])
    assert r['n'] >= 1, '標準価格のある行を数えられていない'
    assert 0 <= r['ok'] <= r['n'], '一致数が行数を超えている'
    assert r['pinned'], '指定した候補どおりに生成できていない'


def test_candidates_keep_same_name_variants():
    """グレード名も記号も同じでボディ・年式が違う候補を、まとめて消さない"""
    est = EST
    nb, rep, cands = _cands(est)
    raw = (rep.get('vehicle') or {}).get('candidates') or []
    uniq = {tuple(str(c.get(k) or '').strip() for k in g.KEYS) for c in raw
            if str(c.get('car_code') or '').strip() and str(c.get('grade_code') or '').strip()}
    assert len(cands) == len(uniq), f'候補 {len(uniq)} 件が {len(cands)} 件に減っている'
    if len(raw) >= 2 and len({(str(c.get('grade_name') or ''), str(c.get('grade_code') or '')) for c in raw}) == 1:
        assert len(cands) >= 2, '同名・同記号の別候補が 1 件に潰れている'
        assert len({c['label'] for c in cands}) == len(cands), '同名の候補に見分けが付かない'


def test_score_pins_the_requested_candidate():
    """候補を指定したら、その候補どおりに生成される（別候補にすり替わらない）"""
    nb, _rep, cands = _cands(EST)
    if len(cands) < 2:
        return
    for c in cands[:3]:
        r = g.score(nb, EST, c)
        assert r['pinned'], f"{c['label']} を指定したのに別の候補で生成された"


def main() -> int:
    ng = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith('test_')):
        try:
            fn()
            print('ok  ', name)
        except AssertionError as e:
            print('FAIL', name, e)
            ng += 1
    print('pick_grade tests:', 'all ok' if not ng else f'{ng} 件 NG')
    return 1 if ng else 0


if __name__ == '__main__':
    sys.exit(main())
