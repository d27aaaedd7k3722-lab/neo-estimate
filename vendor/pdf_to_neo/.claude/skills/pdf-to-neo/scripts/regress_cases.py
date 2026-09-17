# -*- coding: utf-8 -*-
"""regress_cases.py — スキル自体の回帰テスト。NEO_check 配下の reading.json を持つ案件すべてで
  draft_estimate → estimate.json を作り直し、案件フォルダの expected_estimate.json（正解）と items / paint / totals / hints / wage_round を比較し、
  run_case で合格することを確認する。

使い方（files ディレクトリで）:
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/regress_cases.py            # 検証
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/regress_cases.py --update   # 正解を今の出力で更新（変更が意図どおりと確認したときだけ）
    環境変数 NEO_CHECK_ROOT で NEO_check の場所を変えられる。
終了コード: 0 全案件一致・合格 / 1 差分または不合格あり
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402
skill_env.apply()  # ADDATA / NEO_check / 雛形 の場所を環境変数に（PC ごとの設定ファイルと自動検出）
FILES = skill_env.FILES
NC = os.environ.get('NEO_CHECK_ROOT') or os.path.join(os.path.expanduser('~'), 'Documents', 'NEO_check')
PY = sys.executable
KEYS = ('items', 'paint', 'expenses', 'totals', 'hints', 'wage_round', 'labor_rate', 'index_policy', 'frame', 'discount')


def run(args: list[str]) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.run([PY] + args, cwd=FILES, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return p.returncode, (p.stdout or '') + (p.stderr or '')


def strip(est: dict) -> dict:
    out = {}
    for k in KEYS:
        if k in est:
            v = est[k]
            if k == 'items':
                v = [{kk: vv for kk, vv in it.items() if not kk.startswith('_')} for it in v]
            elif isinstance(v, dict):   # paint の _total_from_lines など下書きの内部キーは比べない
                v = {kk: vv for kk, vv in v.items() if not str(kk).startswith('_')}
            out[k] = v
    return out


def diff_paths(a, b, path='') -> list[str]:
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f'{path}.{k}: 正解に無い → {json.dumps(b[k], ensure_ascii=False)[:80]}')
            elif k not in b:
                out.append(f'{path}.{k}: 出力に無い（正解 {json.dumps(a[k], ensure_ascii=False)[:80]}）')
            else:
                out += diff_paths(a[k], b[k], f'{path}.{k}')
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f'{path}: 要素数 正解 {len(a)} / 出力 {len(b)}')
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff_paths(x, y, f'{path}[{i}]')
    elif a != b:
        out.append(f'{path}: 正解 {json.dumps(a, ensure_ascii=False)[:60]} / 出力 {json.dumps(b, ensure_ascii=False)[:60]}')
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--update', action='store_true')
    ap.add_argument('--only', default='', help='案件フォルダ名の部分一致（1 案件だけ回す）')
    a = ap.parse_args()
    if not os.path.isdir(NC):
        print('NEO_check が無い:', NC); return 1
    cases = sorted(d for d in os.listdir(NC) if os.path.isfile(os.path.join(NC, d, 'reading.json')) and (not a.only or a.only in d))
    if not cases:
        print('reading.json を持つ案件が無い'); return 0
    fail = 0
    for d in cases:
        case = os.path.join(NC, d)
        exp_path = os.path.join(case, 'expected_estimate.json')
        with tempfile.TemporaryDirectory() as tmp:
            est_tmp = os.path.join(tmp, 'estimate.json')
            rc, out = run([os.path.join(HERE, 'draft_estimate.py'), os.path.join(case, 'reading.json'), est_tmp])
            if rc != 0:
                print(f'NG {d}: draft_estimate 失敗\n{out[-800:]}'); fail += 1; continue
            est = json.load(open(est_tmp, encoding='utf-8-sig'))
            cur = strip(est)
            if not os.path.exists(exp_path) and not a.update:
                # 正解を黙って作らない。NEO_check には作業中の案件フォルダも置かれる（アプリ・スキルの出力先）ので、
                # 自動で作ると **その時点の出力がそのまま正解になり**、途中の読み取りを回帰に焼き付けてしまう。
                # 案件を回帰に足すときは --update（HANDOFF の手順）。2026-09-17
                print(f'SKIP {d}: 正解（expected_estimate.json）が無い。回帰に足すなら --update で作る')
                continue
            if a.update:
                json.dump(cur, open(exp_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
                tag = '正解を更新'
            else:
                exp = json.load(open(exp_path, encoding='utf-8-sig'))
                diffs = diff_paths(exp, cur)
                if diffs:
                    print(f'NG {d}: 正解と {len(diffs)} 箇所違う')
                    for x in diffs[:12]:
                        print('   ', x)
                    fail += 1
                    continue
                tag = '正解と一致'
            neo_tmp = os.path.join(tmp, 'out.neo')
            rc, out = run([os.path.join(FILES, 'claude_neo_pipeline', 'run_case.py'), est_tmp, neo_tmp])
            line = next((l.strip() for l in out.splitlines() if '見積書合計との一致' in l), '')
            tt = est.get('totals') or {}
            neo_total_ok = tt.get('total') is not None and tt.get('neo_total') is not None and bool(line)  # make_neo と同じ条件
            passed = rc == 0 and '未照合行:' not in out and (('OK' in line) or neo_total_ok)
            print(f"{'OK' if passed else 'NG'} {d}: {tag} / {line or 'run_case 出力なし'}")
            if not passed:
                fail += 1
    print(f'案件 {len(cases)} / 不合格 {fail}')
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
