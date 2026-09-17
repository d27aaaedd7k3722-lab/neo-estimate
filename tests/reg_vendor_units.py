# -*- coding: utf-8 -*-
"""reg_vendor_units.py — **取り込んだ vendor 自身の単体テスト**をアプリ側からも回す。

vendor（files リポジトリの写し）には判断・生成の単体テストが一緒に入っているのに、アプリのテストからは
1 本も呼んでいなかった。vendor を取り直したときに、判断規則の回帰（税込の割り戻し・コグニの消費税設定など）を
誰も確かめないまま本番へ出る状態だったので、ここでまとめて回す。

    python tests/reg_vendor_units.py
終了コード: 0 全部 OK / 1 失敗（ADDATA が要るテストは、ADDATA が無い環境では SKIP）
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
from neo_skill import vendor  # noqa: E402

ADDATA_TESTS = ('unit_settings.py',)   # ADDATA（コグニのマスタ）と雛形 NEO が要るもの


def run(path: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, path], cwd=vendor.VENDOR_ROOT, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    return p.returncode, ((p.stdout or '') + (p.stderr or '')).strip()


def main() -> int:
    why = vendor.readiness_error()
    if why:
        print('vendor が使えない:', why)
        return 1
    have_addata = bool(os.environ.get('ADDATA_DIR') or os.path.isdir(r'C:\Addata') or os.path.isdir(r'D:\Addata'))
    targets = sorted(glob.glob(os.path.join(vendor.SCRIPTS_DIR, 'tests', 'test_*.py')))
    targets += [os.path.join(vendor.PIPELINE_DIR, 'tests', n) for n in ADDATA_TESTS]
    targets = [t for t in targets if os.path.isfile(t)]
    if len(targets) < 5:
        print('vendor の単体テストが見つからない（取り込みが壊れている）:', len(targets))
        return 1
    fails, skipped = [], []
    for t in targets:
        name = os.path.basename(t)
        if name in ADDATA_TESTS and not have_addata:
            skipped.append(name)
            continue
        rc, out = run(t)
        if rc != 0:
            fails.append((name, out[-700:]))
            print('*** FAILED:', name)
            print(out[-700:])
        else:
            print('ok  ', name)
    if skipped:
        print('SKIP（ADDATA が無い）:', ' / '.join(skipped))
    print('reg_vendor_units:', f'{len(targets) - len(fails) - len(skipped)} 本 OK'
          + (f' / {len(fails)} 本が不合格' if fails else ''))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
