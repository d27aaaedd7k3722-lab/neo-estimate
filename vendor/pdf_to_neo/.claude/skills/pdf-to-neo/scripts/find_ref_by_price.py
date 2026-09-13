# -*- coding: utf-8 -*-
"""名称と単価から ADDATA の部品コード（ref）を探す。

`run_case.py` が「要確認: 名称の近似で決めた行のうち標準価格も合わないもの」を出したとき、
その行の正しい ref を突き止めるための道具。クリップ・グロメットのように同名の候補が
何十個もある部品は、**標準単価が決め手**になる。

    cd files
    python .claude/skills/pdf-to-neo/scripts/find_ref_by_price.py Y62 ｸﾘﾂﾌﾟ --unit 110
    python .claude/skills/pdf-to-neo/scripts/find_ref_by_price.py Y62 ｸﾞﾛﾒﾂﾄ --unit 90 --block A20

単価は **1 個あたり**（見積の金額 ÷ 数量）。見積が税込印字なら税抜に直してから渡す。
見つけた ref は estimate.json の `code` に書く。候補が複数なら部位（--block）で絞る。
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402
skill_env.apply()  # ADDATA の場所を PC ごとの設定と自動検出で解決する
FILES = skill_env.FILES
sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))
sys.path.insert(0, FILES)
from estimate_to_neo import AddataParts, NeoBuilder  # noqa: E402


def candidates(car: str, word: str, block: str = '') -> list:
    """名称に word を含む ref を (ref, 名称, 部位, [単価...], [品番...]) で返す。
    同じ ref でも年式・グレード・ボディ違いで**単価の違う変種**があるので、11.DB の変種を全部含める
    （例 Y62 の 2700 リヤドアパネルは 68,400 円と 75,500 円の 2 通り）"""
    nb = NeoBuilder()
    ap = AddataParts(nb.engine, car)
    ap._load_12_blocks(car)
    key = word.replace('-', 'ー')
    out = []
    for ref, names in sorted(ap.name20_by_ref.items()):
        nm = ' / '.join(x.strip() for x in names if x.strip())
        blk = ap.block_by_ref.get(ref, '')
        if key not in nm.replace('-', 'ー') or (block and blk != block):
            continue
        prices, pns = [], []
        # `variant()` は車両条件（とくにボディ）で候補を絞るので、調べ物では使わない。
        # ボディ固有の変種にしか無い単価を見落とさないよう、11.DB の変種を全部並べる
        for v in ap.by_ref.get(ref, []):
            pn = str(v.get('parts_no') or '')
            if '-' not in pn:
                continue  # 品番でない行（説明行など）
            try:
                pr = int(v.get('price') or -1)
            except (TypeError, ValueError):
                continue
            if pr > 0 and pr not in prices:
                prices.append(pr)
                pns.append(pn)
        out.append((ref, nm, blk, prices, pns))
    return out


def main(argv: list) -> int:
    ap_ = argparse.ArgumentParser(description='名称と単価から ADDATA の部品コードを探す')
    ap_.add_argument('car', help='車種コード（例 Y62）。run_case の「車両:」行に出る')
    ap_.add_argument('word', help='名称の一部（半角カナ。例 ｸﾘﾂﾌﾟ）')
    ap_.add_argument('--unit', type=int, default=0, help='1 個あたりの標準単価（税抜）。指定すると一致する候補だけ出す')
    ap_.add_argument('--block', default='', help='部位ブロック（例 A20）で絞る')
    a = ap_.parse_args(argv)
    rows = candidates(a.car, a.word, a.block)
    if not rows:
        print(f'{a.car} に「{a.word}」を含む部品が無い（名称は半角カナ。12.DB の表記に合わせる）')
        return 1
    if a.unit:
        rows = [r for r in rows if a.unit in r[3]]
        if not rows:
            print(f'単価 {a.unit:,} 円に一致する候補が無い。'
                  '見積が税込印字でないか、数量で割り忘れていないか、部位が違わないか確かめる')
            return 1
    print(f'{len(rows)} 件')
    for ref, nm, blk, prices, pns in rows[:40]:
        if not prices:
            tail = '標準単価 なし'
        elif a.unit and a.unit in prices:
            k = prices.index(a.unit)
            tail = f'標準単価 {a.unit:>8,}  品番 {pns[k]}' + (f'（他に {len(prices) - 1} 通りの変種）' if len(prices) > 1 else '')
        else:
            tail = '標準単価 ' + ' / '.join(f'{x:,}' for x in prices[:4]) + ('  品番 ' + pns[0] if pns else '')
        print(f'  {ref:04d} [{blk or "--"}] {nm[:32]:<34} {tail}')
    if len(rows) > 40:
        print(f'  …他 {len(rows) - 40} 件。--unit か --block で絞る')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
