# -*- coding: utf-8 -*-
"""reg_vendor.py — 受け入れテスト（移植ガイド §5-2）: 同じ reading.json から、
files（取り込み元）の **取り込んだコミットそのもの** の make_neo.py と、アプリ経由（neo_skill.maker → vendor の make_neo.py）で
NEO を作り、neo_diff.py で全列が一致することを確かめる。

    python tests/reg_vendor.py [--only <案件名の部分一致>] [--source <files リポジトリ>] [--keep]

- 案件は <NEO_CHECK_ROOT>/*/reading.json（顧客情報を含むので git に無い。この PC で回す）
- 案件フォルダには書き込まない。reading.json だけを一時フォルダに写して両方を回す
- 参照側（files）は作業ツリーではなく、VENDOR_COMMIT.json に記録されたコミットを `git archive` で一時フォルダに展開して使う
  （files の HEAD が進んでいたり未コミットの変更があっても、比較の相手は「取り込んだコミット」に固定される）。
  --source は files の git リポジトリの場所（省略時は VENDOR_COMMIT.json の source）
- 合否（make_neo の終了コード）が両方で同じこと、NEO（合格なら .neo、不合格なら .ng.neo）の全列一致、
  estimate.json の主要キー一致、の 3 つが揃って OK
- 陰性対照（案件ごと・案件数に依存しない）: 同じ reading.json の最初の金額を 1 円変えて作った NEO と比べ、
  neo_diff が差分を出すことを確かめる（差分を見られていないテストで OK を出さない）。
  neo_diff.py は一時 DB を「NEO のファイル名 + テーブル名」で作るので、比べる 2 本は必ず別の名前にする
終了コード: 0 全案件一致 / 1 差分・不合格・案件 0 件
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
from neo_skill import maker, vendor  # noqa: E402

PY = sys.executable
NC = os.environ.get('NEO_CHECK_ROOT') or os.path.join(os.path.expanduser('~'), 'Documents', 'NEO_check')
EST_KEYS = ('items', 'paint', 'expenses', 'totals', 'hints', 'wage_round', 'labor_rate', 'index_policy', 'frame', 'discount')


def git(repo: str, *args: str) -> bytes:
    p = subprocess.run(['git', '-C', repo] + list(args), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)} に失敗: {p.stderr.decode("utf-8", "replace")[:300]}')
    return p.stdout


def checkout_reference(files_repo: str, commit: str, dest: str) -> tuple[str, str]:
    """取り込んだコミットの内容（作業ツリーではない）を dest に展開する。戻り値 (展開した完全なコミット ID, files の HEAD)"""
    full = git(files_repo, 'rev-parse', '--verify', commit + '^{commit}').decode().strip()
    head = git(files_repo, 'rev-parse', 'HEAD').decode().strip()
    tar_bytes = git(files_repo, 'archive', '--format=tar', full)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode='r:') as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            dst = os.path.join(dest, *m.name.split('/'))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with tf.extractfile(m) as src, open(dst, 'wb') as out:
                out.write(src.read())
    if not os.path.isfile(os.path.join(dest, '.claude', 'skills', 'pdf-to-neo', 'scripts', 'make_neo.py')):
        raise RuntimeError(f'コミット {full[:12]} に make_neo.py が無い')
    return full, head


def run_ref_make_neo(ref_root: str, case_dir: str, name: str) -> tuple[int, str]:
    """参照側（取り込んだコミットを展開したフォルダ）の make_neo.py をそのまま回す。REPO_ROOT をそこに固定"""
    env = dict(os.environ, REPO_ROOT=ref_root, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    args = [PY, os.path.join(ref_root, '.claude', 'skills', 'pdf-to-neo', 'scripts', 'make_neo.py'),
            case_dir, '--name', name, '--no-profile']
    p = subprocess.run(args, cwd=ref_root, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return p.returncode, (p.stdout or '') + (('\n[stderr]\n' + p.stderr) if p.returncode != 0 else '')


def neo_diff(a: str, b: str) -> str:
    """vendor の neo_diff.py で全テーブル・AnSMB・Ex.db・AnNote を比べる。出力が空なら一致。
    a と b のファイル名が同じだと一時 DB を共有して差分が消えるので、呼ぶ側で別名にする"""
    if os.path.basename(a) == os.path.basename(b):
        raise ValueError(f'neo_diff に同名のファイルを渡している（差分が消える）: {a} / {b}')
    p = subprocess.run([PY, os.path.join(vendor.PIPELINE_DIR, 'tests', 'neo_diff.py'), a, b],
                       cwd=vendor.VENDOR_ROOT, env=vendor.subprocess_env(), capture_output=True,
                       text=True, encoding='utf-8', errors='replace')
    out = ((p.stdout or '') + (p.stderr or '')).strip()
    if p.returncode != 0 and not out:
        out = f'neo_diff.py が終了コード {p.returncode} で落ちた（出力なし）'
    return out


def strip_estimate(path: str) -> dict:
    try:
        est = json.load(open(path, encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    out = {}
    for k in EST_KEYS:
        if k in est:
            v = est[k]
            if k == 'items':
                v = [{kk: vv for kk, vv in it.items() if not kk.startswith('_')} for it in v]
            out[k] = v
    return out


def perturb_reading(rd: dict) -> bool:
    """最初に金額（price、無ければ wage）のある明細行を 1 円変える。変えられたら True。
    行は短縮記法（'code|name|method|parts_no|index|qty|price|wage|flags|comment'）か dict"""
    for b in rd.get('blocks') or []:
        rows = b.get('rows') or []
        for i, r in enumerate(rows):
            if isinstance(r, str):
                cols = r.split('|')
                if len(cols) >= 8:
                    for k in (6, 7):  # price, wage
                        v = cols[k].replace(',', '').strip()
                        if v.isdigit() and int(v) > 0:
                            cols[k] = str(int(v) + 1)
                            rows[i] = '|'.join(cols)
                            return True
            elif isinstance(r, dict):
                for k in ('price', 'wage'):
                    if isinstance(r.get(k), int) and r[k] > 0:
                        r[k] = r[k] + 1
                        return True
    return False


def negative_control(src_reading: str, app_neo: str, tmp: str) -> str:
    """同じ案件の金額を 1 円変えた reading で NEO を作り（紙上検算は --skip-check で通し、run_case の検算差で
    不合格になるので .ng.neo）、app の NEO と neo_diff に差分が出ることを確かめる。'' なら OK、文字列は問題"""
    rd = json.load(open(src_reading, encoding='utf-8-sig'))
    if not perturb_reading(rd):
        return '陰性対照: 金額のある明細行が無く、reading を変えられない'
    d = os.path.join(tmp, 'neg')
    os.makedirs(d, exist_ok=True)
    maker.write_reading(d, rd)
    res = maker.make_neo(d, 'neg', no_profile=True, skip_check=True)
    neg_neo = res.neo_path or res.ng_neo_path
    if not neg_neo:
        return '陰性対照: 金額を変えた reading から NEO（.neo / .ng.neo）が作られなかった: ' + (res.error or ' / '.join(res.reasons))
    diff = neo_diff(app_neo, neg_neo)
    if not diff:
        return '陰性対照: 金額を 1 円変えた NEO と差分が出ない（neo_diff が差分を見られていない。上の一致は信用できない）'
    return ''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default='')
    ap.add_argument('--source', default='', help='files の git リポジトリ（省略時は VENDOR_COMMIT.json の source）')
    ap.add_argument('--keep', action='store_true', help='一時フォルダを残す（差分を見るとき）')
    a = ap.parse_args()
    st = vendor.stamp()
    files_repo = a.source or st.get('source') or ''
    commit = str(st.get('commit') or '')
    if not os.path.isdir(os.path.join(files_repo, '.git')):
        print('取り込み元（files）の git リポジトリが見つからない。--source で指定:', files_repo or '(未設定)'); return 1
    why = vendor.readiness_error()
    if why:
        print('vendor が使えない:', why); return 1
    if not commit:
        print('VENDOR_COMMIT.json に commit が無い'); return 1
    if not os.path.isdir(NC):
        print('NEO_check が無い:', NC); return 1
    cases = sorted(d for d in os.listdir(NC)
                   if os.path.isfile(os.path.join(NC, d, 'reading.json')) and (not a.only or a.only in d))
    if not cases:
        # 0 件で合格にしない（--only のタイポや空の NEO_check で受け入れテストが素通りしないように）
        print('reading.json を持つ案件が 1 件も無い（--only の指定か NEO_CHECK_ROOT を確かめる）:', NC, a.only or ''); return 1
    ref_root = tempfile.mkdtemp(prefix='reg_vendor_ref_')
    try:
        try:
            full, head = checkout_reference(files_repo, commit, ref_root)
        except (RuntimeError, OSError) as e:
            print('参照側（取り込んだコミット）を展開できない:', e); return 1
        print(f'取り込み元: {files_repo}')
        print(f'参照側: commit {full[:12]} を git archive で展開（作業ツリーは使わない）' + ('' if head == full else f'  ※ files の HEAD は {head[:12]}（取り込みより進んでいる。比較は取り込みコミットで行う）'))
        print(f'vendor: commit {vendor.commit_short()}')
        print(f'案件 {len(cases)} 件（NEO_check）')
        fail = 0
        for d in cases:
            t0 = time.time()
            src = os.path.join(NC, d, 'reading.json')
            tmp = tempfile.mkdtemp(prefix='reg_vendor_')
            try:  # 途中で例外が出ても顧客情報入りの一時フォルダを残さない
                ref_dir = os.path.join(tmp, 'ref'); app_dir = os.path.join(tmp, 'app')
                os.makedirs(ref_dir); os.makedirs(app_dir)
                shutil.copy2(src, os.path.join(ref_dir, 'reading.json'))
                shutil.copy2(src, os.path.join(app_dir, 'reading.json'))
                rc_ref, out_ref = run_ref_make_neo(ref_root, ref_dir, 'ref')
                res_app = maker.make_neo(app_dir, 'app', no_profile=True)
                problems = []
                ok_ref = rc_ref == 0
                if ok_ref != res_app.ok:
                    problems.append(f'合否が違う: files={"合格" if ok_ref else "不合格"} / app={"合格" if res_app.ok else "不合格"}'
                                    + (f'（app: {res_app.error or " / ".join(res_app.reasons)}）' if not res_app.ok else ''))
                ref_neo = os.path.join(ref_dir, 'ref.neo' if ok_ref else 'ref.ng.neo')
                app_neo = res_app.neo_path or res_app.ng_neo_path or ''
                if os.path.isfile(ref_neo) and app_neo and os.path.isfile(app_neo):
                    diff = neo_diff(ref_neo, app_neo)
                    if diff:
                        problems.append('NEO に差分: ' + diff.splitlines()[0][:160] + (f' …（全 {len(diff.splitlines())} 行）' if diff.count(chr(10)) else ''))
                    neg = negative_control(src, app_neo, tmp)
                    if neg:
                        problems.append(neg)
                else:
                    problems.append(f'NEO が無い: files={os.path.isfile(ref_neo)} / app={bool(app_neo and os.path.isfile(app_neo))}')
                e_ref = strip_estimate(os.path.join(ref_dir, 'estimate.json'))
                e_app = strip_estimate(os.path.join(app_dir, 'estimate.json'))
                if e_ref != e_app or not e_ref:
                    problems.append('estimate.json が違う' if e_ref else 'estimate.json が無い')
                if ok_ref and res_app.ok:
                    ref_rev = any(f.startswith('ref_確認箇所') for f in os.listdir(ref_dir))
                    if not (ref_rev and res_app.review_path):
                        problems.append(f'確認箇所シート: files={ref_rev} / app={bool(res_app.review_path)}')
                line_ref = next((l.strip() for l in out_ref.splitlines() if '見積書合計との一致' in l), '')
                status = 'OK' if not problems else 'NG'
                if problems:
                    fail += 1
                print(f"{status} {d}: files={'合格' if ok_ref else '不合格'} app={'合格' if res_app.ok else '不合格'} "
                      f"/ {line_ref or res_app.match_line or '(検算行なし)'} / 陰性対照 {'OK' if not any('陰性対照' in p for p in problems) else 'NG'} / {time.time() - t0:.0f}s")
                for pr in problems:
                    print('    -', pr)
            finally:
                if a.keep:
                    print('    一時フォルダ:', tmp)
                else:
                    left = maker.remove_case_dir(tmp)
                    if left:
                        print('    一時フォルダを消せなかった（手で消す）:', left)
        print()
        print(f'受け入れテスト §5-2: 案件 {len(cases)} / 不一致 {fail}（参照 commit {full[:12]} / vendor {vendor.commit_short()}）')
        return 1 if fail else 0
    finally:
        if a.keep:
            print('参照側の展開:', ref_root)
        else:
            maker.remove_case_dir(ref_root)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
