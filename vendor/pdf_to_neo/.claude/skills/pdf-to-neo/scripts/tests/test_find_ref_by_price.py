# -*- coding: utf-8 -*-
"""単価から部品コードを探す道具の単体テスト（この PC の ADDATA を使う）。
    cd files && python .claude/skills/pdf-to-neo/scripts/tests/test_find_ref_by_price.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import find_ref_by_price as f  # noqa: E402

CAR = 'J87'  # N-BOX。どの PC の ADDATA にもある


def test_finds_by_name():
    rows = f.candidates(CAR, 'ｸﾘﾂﾌﾟ')
    assert rows, 'クリツプが 1 件も見つからない'
    assert all('ｸﾘﾂﾌﾟ' in nm.replace('-', 'ー') for _r, nm, _b, _p, _n in rows), '名称で絞れていない'


def test_block_filter():
    all_rows = f.candidates(CAR, 'ｸﾘﾂﾌﾟ')
    blocks = {b for _r, _n, b, _p, _q in all_rows if b}
    if not blocks:
        print('   skip test_block_filter（部位の付いた候補が無い）')
        return
    b0 = sorted(blocks)[0]
    got = f.candidates(CAR, 'ｸﾘﾂﾌﾟ', b0)
    assert got and all(b == b0 for _r, _n, b, _p, _q in got), '部位で絞れていない'
    assert len(got) < len(all_rows), '部位で絞っても件数が減っていない'


def test_variant_prices_are_all_listed():
    """同じ ref に単価の違う変種があるとき、全部を候補に含める
    （主変種だけ見ると「一致なし」と誤答する。Codex 指摘 2026-09-10）"""
    rows = f.candidates(CAR, 'ﾊﾟﾈﾙ')   # J87 の 0800/1000 に単価 2 通りの変種がある
    multi = [r for r in rows if len(r[3]) > 1]
    assert multi, 'この ADDATA に単価が複数ある変種が見つからない（車種か名称を見直す）'
    ref, _nm, _blk, prices, pns = multi[0]
    assert len(pns) == len(prices), f'品番と単価の数が合わない（ref {ref}）'
    for pr in prices:
        assert any(pr in r[3] for r in rows), f'変種の単価 {pr} で引けない（主変種しか見ていない）'
    # 主変種でない側の単価でも、その ref が候補に残ること
    second = prices[1]
    assert any(r[0] == ref and second in r[3] for r in rows), '主変種以外の単価で ref を引けない'


def test_body_specific_variants_are_included():
    """ボディ固有の変種にしか無い単価も候補に入れる。
    `variant()` はボディ未指定だと共通行だけに絞るので、調べ物では 11.DB の変種を直接並べる
    （Codex 指摘 2026-09-10。この対応で Y62 の「パネル」の複数単価 8 件 → 33 件になった）"""
    import estimate_to_neo as e
    nb = e.NeoBuilder()
    ap = e.AddataParts(nb.engine, CAR)
    ap._load_12_blocks(CAR)
    rows = f.candidates(CAR, 'ﾊﾟﾈﾙ')
    for ref, _nm, _blk, prices, _pns in rows:
        raw = {int(v['price']) for v in ap.by_ref.get(ref, [])
               if '-' in str(v.get('parts_no') or '') and str(v.get('price') or '').strip().isdigit()
               and int(v['price']) > 0}
        assert raw <= set(prices), f'ref {ref} の変種の単価を取りこぼしている（{sorted(raw - set(prices))}）'


def test_unknown_name():
    assert f.candidates(CAR, 'ｿﾝﾅﾌﾞﾋﾝﾊﾅｲ') == [], '存在しない名称で候補が出ている'


def main() -> int:
    ng = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith('test_')):
        try:
            fn()
            print('ok  ', name)
        except AssertionError as e:
            print('FAIL', name, e)
            ng += 1
    print('find_ref_by_price tests:', 'all ok' if not ng else f'{ng} 件 NG')
    return 1 if ng else 0


if __name__ == '__main__':
    sys.exit(main())
