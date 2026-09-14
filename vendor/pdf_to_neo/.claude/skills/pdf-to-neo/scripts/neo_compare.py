# -*- coding: utf-8 -*-
"""neo_compare.py — 納品した NEO と「最後に使われた NEO」の答え合わせ（読むだけ。精度を上げるための振り返り用）。

案件フォルダに、あとからコグニで直した NEO・別のアプリで作った NEO（確報・協定に使ったもの）が置かれることがある。
それと納品した NEO を明細単位で突き合わせ、何が違ったかを分類して出す（2026-09-13 ランクル・ベンツで手作業していたものを 1 コマンドにした）。

    python .claude/skills/pdf-to-neo/scripts/neo_compare.py <納品した.neo> <最後に使われた.neo> [--json <出力.json>]

行の揃え方（上から順に。揃った行は次の段に回さない）:
  1. 品番（空白・ハイフンを除き大文字）が同じ行
  2. 部品金額と工賃の組が同じ行（品番の無い作業行・手入力行）
  3. 部品金額が同じで品番が 1〜2 字違いの行 → 「品番の違い」（どちらかの読み違い。見積書で確かめる）
  4. 部品金額が同じ行 → 「工賃の置き場所・金額の違い」／ 工賃が同じ行 → 「名称・部品金額の違い」
  残り: 「納品の側だけ」（協定で削った・差し替えた行）/「相手の側だけ」（協定で足した行・読み落とし）
部品コード（ADDATA の標準）の有無・名称・作業区分の違いは件数だけ数える（どちらが正しいかは書式・運用で違うので判定しない）。
顧客名・住所・登録番号は読まない（明細と合計だけ）。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import unicodedata
from collections import Counter

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import intent_check  # noqa: E402  NEO を開く処理（_open）を共用する

COLS = ('RecordNo', 'PartsCode', 'DisposalCode', 'PartsName', 'PartsNo', 'PartsCount', 'PartsPriceOutTax', 'WageOutTax', 'ReserveFlag')


def load(neo_path: str) -> list[dict]:
    em, iff, tmps = intent_check._open(neo_path)
    try:
        return [{k: r[k] for k in COLS} for r in (dict(x) for x in em.execute('SELECT * FROM ERParts ORDER BY RecordNo'))]
    finally:
        em.close(); iff.close()
        for t in tmps:
            try:
                os.unlink(t)
            except OSError:
                pass


def _pn(r: dict) -> str:
    return re.sub(r'[\s\-]', '', str(r.get('PartsNo') or '')).upper()


def _amt(r: dict, k: str) -> int:
    try:
        return max(0, int(r.get(k) or 0))
    except (TypeError, ValueError):
        return 0


def _nm(r: dict) -> str:
    return re.sub(r'[\s\-ｰー･・,、.。()（）]', '', unicodedata.normalize('NFKC', str(r.get('PartsName') or ''))).upper()


def _sim(x: dict, y: dict) -> float:
    return difflib.SequenceMatcher(None, _nm(x), _nm(y)).ratio()


def _near(a: str, b: str) -> bool:
    """品番が 1〜2 字違い（置き換え・先頭の 1 字の有無）"""
    if not a or not b or a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 2
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return len(t) - len(s) <= 2 and (t.endswith(s) or t.startswith(s))


def _row(r: dict) -> dict:
    return {'no': r.get('RecordNo'), 'name': str(r.get('PartsName') or '').strip(), 'parts_no': str(r.get('PartsNo') or '').strip(),
            'qty': r.get('PartsCount'), 'price': _amt(r, 'PartsPriceOutTax'), 'wage': _amt(r, 'WageOutTax')}


def compare(mine: list[dict], other: list[dict]) -> dict:
    a = [r for r in mine if not int(r.get('ReserveFlag') or 0)]   # 保留の行は合計に入らないので外す
    b = [r for r in other if not int(r.get('ReserveFlag') or 0)]
    left, right = list(range(len(a))), list(range(len(b)))
    pairs: list[tuple[str, int, int]] = []

    def take(kind, key_a, ok=None):
        # 条件に合う組を全部挙げ、名称の近い組 → 並び順の近い組から揃える（前から順に取ると、同じ金額の別の行に先を越される。
        # 2026-09-13 ベンツ: 25,200 円の作業行が 2 つあり、削られた「誤給油点検」が残った「テスト走行」と揃っていた）
        cand = []
        for i in left:
            ka = key_a(a[i])
            if ka is None:
                continue
            for j in right:
                if ok(a[i], b[j]) if ok else key_a(b[j]) == ka:
                    cand.append((_sim(a[i], b[j]), -abs(j - i), i, j))
        used_i, used_j = set(), set()
        for _s, _d, i, j in sorted(cand, reverse=True):
            if i not in used_i and j not in used_j:
                pairs.append((kind, i, j)); used_i.add(i); used_j.add(j)
        left[:] = [i for i in left if i not in used_i]
        right[:] = [j for j in right if j not in used_j]
    take('品番', lambda r: _pn(r) or None)
    take('金額', lambda r: (_amt(r, 'PartsPriceOutTax'), _amt(r, 'WageOutTax')))
    take('品番の違い', lambda r: _pn(r) or None, ok=lambda x, y: _amt(x, 'PartsPriceOutTax') == _amt(y, 'PartsPriceOutTax') and _near(_pn(x), _pn(y)))
    take('部品金額が同じ', lambda r: _amt(r, 'PartsPriceOutTax') or None, ok=lambda x, y: _amt(x, 'PartsPriceOutTax') and _amt(x, 'PartsPriceOutTax') == _amt(y, 'PartsPriceOutTax'))
    take('工賃が同じ', lambda r: _amt(r, 'WageOutTax') or None, ok=lambda x, y: _amt(x, 'WageOutTax') and _amt(x, 'WageOutTax') == _amt(y, 'WageOutTax'))
    diffs, style = [], Counter()
    for kind, i, j in pairs:
        x, y = a[i], b[j]
        d = {k: (x.get(k), y.get(k)) for k in ('PartsCount', 'PartsPriceOutTax', 'WageOutTax') if _norm(x, k) != _norm(y, k)}
        if _pn(x) and _pn(y) and _pn(x) != _pn(y):  # 金額で揃った行でも品番が違えば挙げる（どちらかの読み違い。2026-09-13 ベンツ 028997144565 / …564）
            d['PartsNo'] = (x.get('PartsNo'), y.get('PartsNo'))
            kind = '品番の違い'
        if d:
            diffs.append({'kind': {'品番': '数量・金額の違い', '金額': '数量の違い'}.get(kind, kind), 'mine': _row(x), 'other': _row(y),
                          'what': {k: list(v) for k, v in d.items()}})
        style['部品コード あり/なし' if bool(str(x.get('PartsCode') or '').strip()) != bool(str(y.get('PartsCode') or '').strip()) else '部品コード 同じ'] += 1
        if str(x.get('PartsName') or '').strip() != str(y.get('PartsName') or '').strip():
            style['名称の違い'] += 1
        if str(x.get('DisposalCode')) != str(y.get('DisposalCode')):
            style['作業区分の違い'] += 1
    tot = lambda rs, k: sum(_amt(r, k) for r in rs)
    return {'rows': [len(a), len(b)], 'matched': len(pairs) - len(diffs), 'diffs': diffs,
            'only_mine': [_row(a[i]) for i in left], 'only_other': [_row(b[j]) for j in right], 'style': dict(style),
            'totals': {'parts': [tot(a, 'PartsPriceOutTax'), tot(b, 'PartsPriceOutTax')], 'wage': [tot(a, 'WageOutTax'), tot(b, 'WageOutTax')]}}


def _norm(r: dict, k: str):
    v = r.get(k)
    if k == 'PartsCount':
        return v if _amt(r, 'PartsPriceOutTax') else None   # 部品代の無い行の数量（-1 / 1）は見ない
    return _amt(r, k)


def report(res: dict) -> str:
    t = res['totals']
    out = [f"明細 {res['rows'][0]} 行 / {res['rows'][1]} 行　一致 {res['matched']}　違い {len(res['diffs'])}　"
           f"納品の側だけ {len(res['only_mine'])}　相手の側だけ {len(res['only_other'])}",
           f"部品計 {t['parts'][0]:,} → {t['parts'][1]:,}（{t['parts'][1] - t['parts'][0]:+,}）　工賃計 {t['wage'][0]:,} → {t['wage'][1]:,}（{t['wage'][1] - t['wage'][0]:+,}）"]
    f = lambda r: f"No.{r['no']} {r['name']} {r['parts_no']} 数量 {r['qty']} 部品 {r['price']:,} 工賃 {r['wage']:,}"
    for d in res['diffs']:
        out.append(f"違い[{d['kind']}] 納品 {f(d['mine'])} / 相手 {f(d['other'])}")
    for r in res['only_mine']:
        out.append(f'納品の側だけ {f(r)}')
    for r in res['only_other']:
        out.append(f'相手の側だけ {f(r)}')
    if res['style']:
        out.append('書き方の違い（揃った行のうち）: ' + ' / '.join(f'{k} {v}' for k, v in sorted(res['style'].items())))
    return '\n'.join(out)


def main(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='納品した NEO と最後に使われた NEO の答え合わせ')
    ap.add_argument('mine')
    ap.add_argument('other')
    ap.add_argument('--json', help='結果を JSON で保存する先（NEO_check の案件フォルダなど、git の外に置く）')
    a = ap.parse_args(argv)
    res = compare(load(a.mine), load(a.other))
    print(report(res))
    if a.json:
        json.dump(res, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
