# -*- coding: utf-8 -*-
"""option_audit.py — 装備（EVA）の監査。生成器が選んだ装備で決まる標準品番と、見積書に印字された品番を突き合わせ、
「別の装備を選べば印字の品番と一致する」行を挙げる。装備の取り違いは合計を変えずに標準品番・指数だけを静かに変えるので、
合計一致の検算では見つからない。ここが唯一の検出点。

使い方（files ディレクトリで、単独実行）:
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/option_audit.py <estimate.json>
make_neo.py は生成後に自動で呼び、結果を標準出力と report.md に載せる。

判定:
  - 行ごとに、生成器の装備集合 E で選ばれる 11.DB 標準行の品番 P(E) と印字品番 Q を比べる
  - Q ≠ P(E) のとき、10.DB の各装備レター L について P(E∪{L}) / P(E∖{L}) を試し、Q に一致する変更があれば「候補」
  - 候補ごとに「その変更で一致するようになる行数 / 逆に一致しなくなる行数」を数え、純増の候補だけを ★ で警告する
  - 印字品番が 11.DB に無い（13/83.DB の色別・期間別、社外品）行は判定に使わない
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402
skill_env.apply()
FILES = skill_env.FILES
flag = skill_env.flag  # 人が書いた真偽値欄の厳密な読み取り
sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))

from estimate_to_neo import AddataParts, NeoBuilder  # noqa: E402


def _std_pn(parts: AddataParts, ref: int, dcode: int, grade: str, fva: str, eva: set, grp: str, body: str) -> str:
    try:
        row = parts._std_row(ref, dcode, grade, fva, eva, grp, body)
    except Exception:  # noqa: BLE001  15.DB/11.DB に該当行が無い部品は標準品番を出せない。監査の対象外にする
        return ''
    return parts.norm_pn(row.get('pn', '')) if row else ''


def audit(est: dict, rep: dict, nb: Optional[NeoBuilder] = None) -> dict:
    """戻り値 {'eva': [...], 'rows': n, 'checked': n, 'match': n, 'mismatch': [...], 'candidates': [{'change', 'gain', 'loss', 'rows'}], 'warnings': [...]}"""
    car = rep.get('car') or {}
    car_code = car.get('CarCode')
    out: dict = {'eva': sorted(rep.get('eva') or []), 'rows': 0, 'checked': 0, 'match': 0, 'mismatch': [], 'candidates': [], 'warnings': []}
    if not car_code or flag((est.get('vehicle') or {}).get('generic'), 'vehicle.generic'):
        out['warnings'].append('装備監査: 汎用車種か車両未特定のため省略')
        return out
    nb = nb or NeoBuilder()
    parts = AddataParts(nb.engine, car_code)
    body = str(car.get('BodyCode', '') or '')
    parts.vehicle_body = body
    grade = str(car.get('GradeCode', '') or '')
    fva = str(car.get('FVACode', '') or '').strip()[-1:]  # 生成器と同じ: 11.DB の flags は FVA の末尾 1 文字で照合する
    year = str(car.get('YearCode', '') or '')
    grp = year.strip()[-1] if year.strip().isdigit() and int(year) else ''
    eva = set(out['eva'])
    try:
        opts = nb.resolver.options(car_code)
    except Exception:  # noqa: BLE001  10.DB（装備表）が無い車種。装備レターの候補は出せないが照合は続ける
        opts = {}
        out['warnings'].append('装備表（10.DB）を読めないので「この装備に変えると一致」の提案は出さない')
    letters = sorted(ch for ch in opts if ch not in ('A', 'B', 'C', 'D', 'E') and ch != fva)
    items = est.get('items') or []
    skill_env.normalise_flags(items, where='items[]')  # 真偽値欄は入口で 1 回だけ正規化（Codex 指摘）
    rows = rep.get('rows') or []
    if len(items) != len(rows):
        out['warnings'].append(f'装備監査: items {len(items)} 行と生成行 {len(rows)} 行の数が違うので省略')
        return out
    raw11 = parts._load_11_raw()
    targets = []  # (行番号, ref, dcode, 印字品番, 名称)
    for i, (it, r) in enumerate(zip(items, rows), start=1):
        out['rows'] += 1
        if it.get('manual') or r.get('_manual') or not r.get('PartsCode'):
            continue
        q = parts.norm_pn(it.get('parts_no') or '')
        if not q:
            continue
        ref = int(r['PartsCode'])
        dcode = int(r.get('DisposalCode') or 0)
        if dcode != 0:
            continue  # 品番の比較は取替行だけ
        # 印字品番がこの ref の 11.DB のどこかにある行だけ判定に使う（無ければ 13/83.DB の色別・期間別か社外品）
        if not any(parts.norm_pn(x.get('pn', '')) == q for x in raw11.get(ref, []) if x.get('disp') == 'K'):
            continue
        targets.append((i, ref, dcode, q, str(r.get('PartsName') or it.get('name') or '').strip()))
    out['checked'] = len(targets)
    base = {t: _std_pn(parts, t[1], t[2], grade, fva, eva, grp, body) for t in targets}
    mism = [t for t in targets if base[t] != t[3]]
    out['match'] = len(targets) - len(mism)
    out['mismatch'] = [f'行{t[0]} {t[4]}: 印字 {t[3]} / 標準 {base[t] or "なし"}' for t in mism]
    if not mism:
        return out
    cands = []
    for L in letters:
        for op_, new_eva in (('追加', eva | {L}), ('除外', eva - {L})):
            if new_eva == eva:
                continue
            gain, loss, fixed = 0, 0, []
            for t in targets:
                p2 = _std_pn(parts, t[1], t[2], grade, fva, new_eva, grp, body)
                if p2 == t[3] and base[t] != t[3]:
                    gain += 1; fixed.append(f'行{t[0]} {t[4]}')
                elif p2 != t[3] and base[t] == t[3]:
                    loss += 1
            if gain:
                cands.append({'change': f"{L}={opts.get(L, '?')} を{op_}", 'letter': L, 'op': op_, 'gain': gain, 'loss': loss, 'rows': fixed})
    _excl_now = set(str(x).strip() for x in ((est.get('hints') or {}).get('eva_exclude') or []) if str(x).strip())  # 既に除外指定されているレター
    cands.sort(key=lambda c: (-(c['gain'] - c['loss']), -c['gain']))
    out['candidates'] = cands
    for c in cands:
        if c['gain'] > c['loss']:
            # 除外は eva_codes では書けない。逆に、既に eva_exclude にあるレターは eva_codes に足しても効かない
            # （生成器は eva_exclude を先に引き、eva_codes 側でも `c not in _excl` で弾く。判断規則 10-9）
            if c.get('op') == '除外':
                how_ = f"reading の hints.eva_exclude に [\"{c['letter']}\"] を足す"
            elif c['letter'] in _excl_now:
                how_ = f"reading の hints.eva_exclude から \"{c['letter']}\" を**外す**（eva_codes に足しても除外が勝つので効かない）"
            else:
                how_ = f"reading の hints.eva_codes に [\"{c['letter']}\"] を足す"
            out['warnings'].append(f"★装備 {c['change']} すると、印字品番と一致する行が {c['gain']} 行増え {c['loss']} 行減る（{', '.join(c['rows'][:4])}）。車の実態（10.DB 名称・見積の注記）で採否を決め、採るなら{how_}（判断規則 10-9）")
    if not cands:
        out['warnings'].append(f'装備監査: 印字品番と標準品番が違う行 {len(mism)} 行はどの装備を変えても一致しない（年式群・グレード・色別部品の可能性。inspect の 13/83.DB 一致を確認）')
    return out


def format_lines(res: dict) -> list[str]:
    L = [f"装備 {res.get('eva')}: 判定対象 {res.get('checked')} 行（取替・印字品番が 11.DB にある行）のうち 標準品番一致 {res.get('match')} 行"]
    for m in res.get('mismatch') or []:
        L.append(f'  不一致: {m}')
    for c in res.get('candidates') or []:
        L.append(f"  候補: {c['change']} → 一致 +{c['gain']} / −{c['loss']}")
    for w in res.get('warnings') or []:
        L.append('  ' + w)
    return L


def main(path: str) -> int:
    est = json.load(open(path, encoding='utf-8-sig'))
    nb = NeoBuilder()
    _, rep = nb.build(est, est['vehicle'], hints=est.get('hints'), labor_rate=est.get('labor_rate'), est_date=est.get('est_date'), insurance=est.get('insurance'))
    res = audit(est, rep, nb)
    for line in format_lines(res):
        print(line)
    return 0


if __name__ == '__main__':
    _a = sys.argv[1:]
    if not _a or _a[0] in ('-h', '--help'):  # 引数無し・--help で例外を出さず使い方を見せる
        print(__doc__ or '')
        print('使い方: python option_audit.py <estimate.json>')
        sys.exit(0 if _a else 1)
    sys.exit(main(_a[0]))
