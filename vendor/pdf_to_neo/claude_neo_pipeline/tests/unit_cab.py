# -*- coding: utf-8 -*-
"""COM.CAB の純 Python 展開（Linux / Streamlit Cloud 用）の単体テスト（2026-09-14）
  ADDATA の COM.CAB があれば、expand.exe（Windows）の展開結果と全ファイル一致することを確かめる。
  expand.exe が無い PC では純 Python の展開だけで必須の表が揃うことを確かめる。ADDATA が無ければ省略。
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_cab.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), '.claude', 'skills', 'pdf-to-neo', 'scripts'))
import com_tables  # noqa: E402


def tree(root: str) -> dict:
    out = {}
    for dp, _, fns in os.walk(root):
        for fn in fns:
            fp = os.path.join(dp, fn)
            out[os.path.relpath(fp, root).replace(os.sep, '/').lower()] = hashlib.sha256(open(fp, 'rb').read()).hexdigest()
    return out


def main() -> int:
    root = os.environ.get('ADDATA_ROOT', '') or ''
    if not root:
        try:
            import skill_env
            root = (skill_env.load_config() or {}).get('ADDATA_ROOT', '') or ''
        except Exception:  # noqa: BLE001
            root = ''
    cab = os.path.join(root, 'COM', 'COM.CAB') if root else ''
    if not cab or not os.path.isfile(cab):
        print('unit_cab: ADDATA の COM.CAB が無いので省略'); return 0
    fails = 0
    t_py = tempfile.mkdtemp(prefix='unit_cab_py_'); t_exp = tempfile.mkdtemp(prefix='unit_cab_exp_')
    try:
        names = com_tables.extract_cab(cab, t_py)
        h_py = tree(t_py)
        if not com_tables._ok(t_py):
            fails += 1; print('FAIL 純 Python の展開で必須の表が揃わない:', sorted(h_py)[:8])
        exe = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'System32', 'expand.exe')
        if os.path.isfile(exe):
            subprocess.run([exe, cab, '-F:*', t_exp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            h_exp = tree(t_exp)
            if h_exp != h_py:
                fails += 1
                print('FAIL expand.exe と純 Python の展開が違う: 違うファイル', [k for k in h_exp if h_exp.get(k) != h_py.get(k)][:5],
                      '/ 純 Python に無い', [k for k in h_exp if k not in h_py][:5], '/ 余分', [k for k in h_py if k not in h_exp][:5])
            else:
                print(f'expand.exe と純 Python の展開: {len(h_exp)} ファイル全部一致')
        else:
            print(f'（expand.exe が無いので純 Python の展開だけ確認: {len(names)} ファイル）')
        # 壊れた CAB: CFDATA の途中の 1 バイトを変える → 検査和で止まる（切れた表を dest に公開しない）
        raw = bytearray(open(cab, 'rb').read())
        import struct as _st
        coffFiles, = _st.unpack_from('<I', raw, 16)
        cFolders, = _st.unpack_from('<H', raw, 26)
        coffCabStart, = _st.unpack_from('<I', raw, 36)
        i = coffCabStart + 8 + 100
        raw[i] ^= 0x5A
        t_bad = tempfile.mkdtemp(prefix='unit_cab_bad_'); bad_cab = os.path.join(t_bad, 'COM.CAB')
        open(bad_cab, 'wb').write(bytes(raw))
        try:
            com_tables.extract_cab(bad_cab, os.path.join(t_bad, 'out'))
            fails += 1; print('FAIL 壊れた CAB を ValueError にしない')
        except ValueError as e:
            print('壊れた CAB:', str(e)[:40])
        finally:
            shutil.rmtree(t_bad, ignore_errors=True)
        sz = com_tables.find_7z()
        print('7z:', sz or '（無し。Linux では packages.txt の p7zip-full で入る）')
    finally:
        shutil.rmtree(t_py, ignore_errors=True); shutil.rmtree(t_exp, ignore_errors=True)
    print('unit_cab:', 'all ok' if not fails else f'{fails} 件が不合格')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
