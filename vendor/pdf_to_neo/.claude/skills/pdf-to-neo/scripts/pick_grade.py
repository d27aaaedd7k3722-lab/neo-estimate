# -*- coding: utf-8 -*-
"""グレードが決まらない案件を、見積の部品金額から絞り込む。

型式指定番号・類別区分番号が資料に無いと車両特定は確度 `low` になり、
グレードは候補の先頭が仮採用される。純正定価で出す工場なら
**正しいグレードなら見積の部品金額が ADDATA の標準価格と一致する**ので、
候補ごとに生成して一致数を数えれば絞り込める。

    cd files
    python .claude/skills/pdf-to-neo/scripts/pick_grade.py "<NEO_check>/<案件>/estimate.json"

一致数が並んだら、それ以上は金額では決まらない（外装部品はグレードで変わらないことが多い）。
車検証・型式の表記・装備（ホイールキャップの有無など）で決め、決め手を報告に書く。
結果は `hints.grade_name` に書いて確定させる。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402
skill_env.apply()
FILES = skill_env.FILES
sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))
sys.path.insert(0, FILES)
from estimate_to_neo import NeoBuilder  # noqa: E402


# 候補を見分ける鍵。four_wd だけ真偽値で、ほかは文字列
KEYS = ('car_code', 'year_code', 'body_code', 'grade_code', 'fva_code', 'four_wd')
TEXT_KEYS = tuple(k for k in KEYS if k != 'four_wd')


def candidates(rep: dict) -> list:
    """車両特定の候補を、重複を除いた **具体的な候補（車種・年式・ボディ・グレード・エンジン）** で返す。
    グレード名は同じでもボディ・エンジン違いで別物なので、名前や記号でまとめない"""
    v = rep.get('vehicle') or {}
    out, seen = [], set()
    for c in v.get('candidates') or []:
        pin = {k: str(c.get(k) or '').strip() for k in TEXT_KEYS}
        pin['four_wd'] = bool(c.get('four_wd'))
        if not pin['car_code'] or not pin['grade_code']:
            continue
        key = tuple(str(pin[k]) for k in KEYS)
        if key in seen:
            continue
        seen.add(key)
        out.append({'pin': pin, 'grade': str(c.get('grade_name') or '').strip(),
                    'body': str(c.get('body_name') or '').strip(), 'fva': str(c.get('fva_name') or '').strip()})
    # 同じ「グレード名(記号)」が複数あるときだけ、年式・ボディを添えて見分けられるようにする
    label = {}
    for c in out:
        label.setdefault((c['grade'], c['pin']['grade_code']), []).append(c)
    for (nm, cd), group in label.items():
        for c in group:
            wd = '4WD' if c['pin']['four_wd'] else '2WD'
            detail = f"年式{c['pin']['year_code']}/ﾎﾞﾃﾞｨ{c['pin']['body_code']}/{c['pin']['fva_code']}/{wd}"
            c['label'] = f'{nm}({cd})' if len(group) == 1 else f'{nm}({cd}) {detail}'
    return out


def score(nb, est: dict, cand: dict) -> dict:
    """その候補で生成し、標準価格と一致した行数を数える。
    候補は `hints.candidate` で 1 件に固定する（グレード名で切り替えると同名の別候補と取り違える）"""
    e = json.loads(json.dumps(est))
    e.setdefault('hints', {})['candidate'] = dict(cand['pin'])
    _neo, rep = nb.build(e, e['vehicle'], hints=e.get('hints'), labor_rate=e.get('labor_rate'),
                         est_date=e.get('est_date'), insurance=e.get('insurance') or {})
    pairs = []
    for r in rep['rows']:
        try:
            std, pr = int(r.get('PartsPriceStandardOutTax') or -1), int(r.get('PartsPriceOutTax') or -1)
            qty = max(1, int(r.get('PartsCount') or 1))
        except (TypeError, ValueError):
            continue
        if std > 0 and pr > 0:
            pairs.append(pr == std * qty)
    got = (rep.get('vehicle') or {}).get('best') or {}
    used = {k: str(got.get(k) or '').strip() for k in TEXT_KEYS}
    used['four_wd'] = bool(got.get('four_wd'))
    return {'label': cand.get('label') or cand['grade'], 'grade': cand['grade'],
            'code': used.get('grade_code', ''), 'pinned': used == cand['pin'],
            'n': len(pairs), 'ok': sum(1 for x in pairs if x)}


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description='見積の部品金額からグレードを絞り込む')
    ap.add_argument('estimate', help='estimate.json のパス')
    a = ap.parse_args(argv)
    est = json.load(io.open(a.estimate, encoding='utf-8-sig'))
    nb = NeoBuilder()
    h = dict(est.get('hints') or {})
    h['candidate_limit'] = 0          # 表示用の 12 件打ち切りを外し、全候補を採点する
    _neo, rep = nb.build(est, est['vehicle'], hints=h, labor_rate=est.get('labor_rate'),
                         est_date=est.get('est_date'), insurance=est.get('insurance') or {})
    v = rep.get('vehicle') or {}
    cands = candidates(rep)
    print(f"車両特定の確度: {v.get('confidence')} / 候補 {len(cands)} 件")
    total = int(v.get('candidates_total') or len(v.get('candidates') or []))
    truncated = total > len(v.get('candidates') or [])
    if truncated:
        print(f'  ※ 候補 {total} 件のうち {len(v.get("candidates") or [])} 件しか取れていない。'
              '採点に出ない候補があるので結果は参考にとどめ、型式指定・類別を資料から探すこと')
    if not cands:
        print('  候補が取れない（汎用車種か、ADDATA に無い車）')
        return 1
    if v.get('confidence') in ('confirmed', 'high') and len({(c['grade'], c['pin']['grade_code']) for c in cands}) <= 1:
        print('  グレードは確定している。絞り込みは要らない')
        return 0
    rows = []
    for c in cands:
        try:
            r = score(nb, est, c)
        except Exception as e:  # noqa: BLE001  その候補で生成できないものは飛ばす
            print(f"  {c.get('label')}: 生成できない（{str(e)[:50]}）")
            continue
        if not r['pinned']:
            print(f"  {c.get('label')}: 候補を固定できなかったので数えない")
            continue
        rows.append(r)
    if not rows:
        return 1
    best = max(r['ok'] for r in rows)
    print(f"{'候補':<28}{'標準価格の一致':>14}")
    for r in sorted(rows, key=lambda x: -x['ok']):
        mark = ' ←' if r['ok'] == best else ''
        print(f"  {r['label']:<28}{r['ok']:>3}/{r['n']:<3}{mark}")
    top = [r for r in rows if r['ok'] == best]
    # 同点の並びは「グレード名(記号)」でまとめて読む（2WD/4WD・年式違いは部品金額に出ないことが多い）
    names = sorted({f"{r['grade']}({r['code']})" for r in top})
    if truncated:
        print(f'→ 取れた候補の中では {names} が最大。ただし候補を全部見られていないので確定にしない')
        return 0
    if len(top) == 1:
        print(f"→ {top[0]['label']} で決まる。hints.grade_name（同名が複数なら hints.candidate）に書く")
    elif len(names) == 1:
        print(f'→ グレードは {names[0]}。残る違い（2WD/4WD・年式・ボディ）は部品金額に出ないので'
              '車検証・型式の表記で決め、決め手を報告に書く')
    else:
        print(f'→ {names} が同点。金額では決まらないので車検証・型式の表記・装備で決め、決め手を報告に書く')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
