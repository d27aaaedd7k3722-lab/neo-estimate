# -*- coding: utf-8 -*-
"""reg_bridge.py — PC の Addata をブラウザ経由で使う経路（neo_skill.bridge）の Python 側を通しで確かめる。
ブラウザの部品（File System Access API）は人の操作が要るので、部品が送る値と同じ形（{phase, files: {相対パス: base64}}）を
C:\\Addata から作って流し込む。ADDATA と NEO_check（reading.json の案件）が無い PC では省略する。

  1. COM だけ取り込む → has_com、版が読める、find の最優先（app.find_addata_dir と同じ条件）
  2. 案件の reading.json から車種コードを決める（resolve_car。COM だけで決まる）
  3. その車種フォルダを取り込む（*IMG.CAB は送らない）→ make_neo → 全 ADDATA で作った NEO と全列一致（neo_diff）
  4. store の安全性（'..'・絶対パス・COM/文字フォルダ以外は捨てる）、ingest の二重処理防止、sweep

    python tests/reg_bridge.py
"""
from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import warnings

warnings.filterwarnings('ignore')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from neo_skill import bridge, maker, vendor  # noqa: E402

ADDATA = os.environ.get('ADDATA_ROOT') or 'C:/Addata'
NC = os.environ.get('NEO_CHECK_ROOT') or os.path.expanduser('~/Documents/NEO_check')
PY = vendor.python_exe()


def files_of(src_dir: str, prefix: str, skip_img_cab: bool) -> dict:
    """部品と同じ形: {相対パス: base64}"""
    out = {}
    for name in sorted(os.listdir(src_dir)):
        p = os.path.join(src_dir, name)
        if not os.path.isfile(p):
            continue
        if skip_img_cab and name.upper().endswith('IMG.CAB'):
            continue
        out[prefix + '/' + name] = base64.b64encode(open(p, 'rb').read()).decode('ascii')
    return out


def neo_diff(a: str, b: str) -> str:
    p = subprocess.run([PY, os.path.join(vendor.PIPELINE_DIR, 'tests', 'neo_diff.py'), a, b], cwd=vendor.VENDOR_ROOT,
                       env=vendor.subprocess_env(ADDATA, None), capture_output=True, text=True, encoding='utf-8', errors='replace')
    return ((p.stdout or '') + (p.stderr or '')).strip()


def main() -> int:
    fails = []
    if not os.path.isfile(os.path.join(ADDATA, 'COM', 'KA06_ALL.DB')):
        print('reg_bridge: ADDATA が無いので省略'); return 0
    cases = [d for d in sorted(glob.glob(os.path.join(NC, '*'))) if os.path.isfile(os.path.join(d, 'reading.json'))]
    if not cases:
        print('reg_bridge: NEO_check に reading.json の案件が無いので省略'); return 0

    ss = {}  # st.session_state の代わり
    root = bridge.root(ss)
    try:
        # 1. COM
        n, total, dropped = bridge.store(root, files_of(os.path.join(ADDATA, 'COM'), 'COM', False))
        if not bridge.has_com(root) or dropped or n < 5:
            fails.append(f'COM の取り込み: n={n} dropped={dropped}')
        if not bridge.version(root):
            fails.append('橋渡し Addata の版が読めない（COM/AnVer.DB）')
        # 2. 車種を決める（reading.json の vehicle。COM だけ）
        picked = None
        for d in cases:
            rd = json.load(open(os.path.join(d, 'reading.json'), encoding='utf-8'))
            res = bridge.resolve_car(root, rd)
            car = res.get('car_code') or ''
            if car:
                picked = (d, rd, car); break
        if not picked:
            fails.append('どの案件でも COM だけで車種を決められない（resolve_car）')
        else:
            d, rd, car = picked
            if bridge.has_car(root, car):
                fails.append(f'取り込む前から has_car が真: {car}')
            # 3. 車種フォルダを取り込んで make_neo → 全 ADDATA と全列一致
            src = os.path.join(ADDATA, car[0], car)
            n2, total2, dropped2 = bridge.store(root, files_of(src, f'{car[0]}/{car}', True))
            if not bridge.has_car(root, car) or bridge.cars(root) != [car] or dropped2:
                fails.append(f'車種フォルダの取り込み: has_car={bridge.has_car(root, car)} cars={bridge.cars(root)} dropped={dropped2}')
            if any(n.upper().endswith('IMG.CAB') for n in os.listdir(os.path.join(root, car[0], car))):
                fails.append('IMG.CAB が橋渡しフォルダに入っている')
            tmp = tempfile.mkdtemp(prefix='reg_bridge_')
            try:
                for tag, addata_root in (('bridge', root), ('full', ADDATA)):
                    cd = os.path.join(tmp, tag); os.makedirs(cd)
                    shutil.copy2(os.path.join(d, 'reading.json'), os.path.join(cd, 'reading.json'))
                    # 紙上検算（reading_check）は案件の読み取りの良し悪しで、この試験の的（Addata の経路）ではないので飛ばす
                    mk = maker.make_neo(cd, tag, no_profile=True, addata_root=addata_root, skip_check=True)
                    if not mk.ok:
                        tail = ' / '.join(l.strip() for l in (mk.stdout or '').splitlines()[-3:])
                        fails.append(f'{tag}: make_neo 不合格: {(mk.error or tail)[:300]}')
                # 橋渡しのフォルダが本当に使われているか（この PC の C:\Addata に落ちていないか）: COM だけの橋渡しフォルダで
                # make_neo すると車種フォルダが無いので不合格になるはず。落ちていれば（vendor が自動検出で C:\Addata を採れば）合格してしまう
                only = bridge.root({'_bridge_id': 'regonly'})
                try:
                    bridge.store(only, files_of(os.path.join(ADDATA, 'COM'), 'COM', False))
                    cd0 = os.path.join(tmp, 'only'); os.makedirs(cd0)
                    shutil.copy2(os.path.join(d, 'reading.json'), os.path.join(cd0, 'reading.json'))
                    mk0 = maker.make_neo(cd0, 'only', no_profile=True, addata_root=only, skip_check=True)
                    if mk0.ok:
                        fails.append('COM だけの橋渡し Addata で合格した ＝ vendor が C:\\Addata に落ちている（ADDATA_ROOT_PARTIAL が効いていない）')
                finally:
                    shutil.rmtree(only, ignore_errors=True)
                a, b = os.path.join(tmp, 'bridge', 'bridge.neo'), os.path.join(tmp, 'full', 'full.neo')
                if os.path.exists(a) and os.path.exists(b):
                    diff = neo_diff(a, b)
                    if diff:
                        fails.append('橋渡し Addata の NEO が全 ADDATA と違う: ' + diff.splitlines()[0][:120])
                    else:
                        print(f'車種 {car}: 橋渡し（COM + 車種フォルダ {n2} ファイル・{total2 / 1048576:.1f}MB）の NEO は全 ADDATA と全列一致')
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        # 4. 安全性・二重処理防止・sweep
        b = lambda s: base64.b64encode(s).decode()  # noqa: E731
        n3, _t, dropped3 = bridge.store(root, {'../evil.DB': b(b'x'), '/abs/x.DB': b(b'x'), 'KCS/x.DB': b(b'x'),
                                              'W/W99/W9901.DB': b(b'x'), 'W/W99/run.EXE': b(b'x'), 'W/W99/W9902.db': b(b'y'),
                                              'W/W99/W9911.DB': b(b'x'),
                                              'W/W99/desktop.ini': b(b'x')})   # 種類違いは無視（揃っているかには関係ない）
        # 01 だけの車種フォルダは「届いた」と見なさない（11 = 部品表が要る。12/15 は無い車種が実在するので必須にしない。Codex hunt D2）
        bridge.store(root, {'W/W98/W9801.DB': b(b'x')})
        if bridge.has_car(root, 'W98'):
            fails.append('01.DB だけの車種フォルダを has_car が真にしている')
        if n3 != 3 or len(dropped3) != 5 or os.path.exists(os.path.join(root, 'evil.DB')) or not bridge.has_car(root, 'W99'):
            fails.append(f'store の安全性: n={n3} dropped={dropped3} has_car(W99)={bridge.has_car(root, "W99")}')
        # 上限: 1 ファイルの大きさを超えたものがあるフォルダは丸ごと書かない（欠けた Addata で作らない）
        _keep = bridge.MAX_FILE_BYTES
        bridge.MAX_FILE_BYTES = 8
        n4, _t, dropped4 = bridge.store(root, {'W/W97/W9701.DB': b(b'0123456789ABCDEF'), 'W/W97/W9702.DB': b(b'ok')})
        if n4 != 0 or dropped4 != ['W/W97/W9701.DB'] or bridge.has_car(root, 'W97') or os.path.exists(os.path.join(root, 'W', 'W97')):
            fails.append(f'store の大きさ上限: n={n4} dropped={dropped4} has_car(W97)={bridge.has_car(root, "W97")}')
        # 上限超えの車種フォルダが届いたら、待ち続けずに「取り込めなかった」と返し、待ち（_bridge_want）を解く
        ss['_bridge_want'] = 'W96'
        msg3 = bridge.ingest(ss, {'seq': 21, 'phase': 'car', 'car': 'W96', 'files': {'W/W96/W9601.DB': b(b'0123456789ABCDEF')}})
        bridge.MAX_FILE_BYTES = _keep
        if '取り込めませんでした' not in (msg3 or '') or ss.get('_bridge_want') != '' or bridge.has_car(root, 'W96'):
            fails.append(f'上限超えの車種フォルダの ingest: {msg3!r} want={ss.get("_bridge_want")!r}')
        # 放置された作業フォルダの掃除（maker.sweep_case_dirs）: 2 時間より古いものだけ消す
        old_case = tempfile.mkdtemp(prefix='neo_case_'); new_case = tempfile.mkdtemp(prefix='neo_case_')
        os.utime(old_case, (time.time() - 3 * 3600, time.time() - 3 * 3600))
        maker.sweep_case_dirs()
        if os.path.exists(old_case) or not os.path.exists(new_case):
            fails.append('sweep_case_dirs が古い作業フォルダを消さない／新しいものを消した')
        shutil.rmtree(new_case, ignore_errors=True)
        # 完了印の無いフォルダ（途中で切れた取り込み）は has_car が偽 ＝ 欠けた Addata で作らない
        os.makedirs(os.path.join(root, 'W', 'W97'), exist_ok=True)   # W98 は上で store 済み（完了印あり）なので別の車種コードで
        open(os.path.join(root, 'W', 'W97', 'W9701.DB'), 'wb').write(b'x')
        open(os.path.join(root, 'W', 'W97', 'W9711.DB'), 'wb').write(b'x')
        if bridge.has_car(root, 'W97') or 'W97' in bridge.cars(root):
            fails.append('完了印の無い車種フォルダを揃っていると見なした')
        # 同じ車種を送り直したら丸ごと差し替わる（古いファイルが残らない）
        bridge.store(root, {'W/W99/W9901.DB': b(b'z'), 'W/W99/W9911.DB': b(b'z')})
        if sorted(os.listdir(os.path.join(root, 'W', 'W99'))) != ['W9901.DB', 'W9911.DB'] or not bridge.has_car(root, 'W99'):
            fails.append(f'送り直しで差し替わらない: {os.listdir(os.path.join(root, "W", "W99"))}')
        # <車種>01.DB（部品表の本体）が無いフォルダは「届いた」と見なさない（PC 側のフォルダが壊れている）
        bridge.store(root, {'W/W93/W9305.DB': b(b'x'), 'W/W93/W9300LTB.CHM': b(b'x')})
        if bridge.has_car(root, 'W93') or 'W93' in bridge.cars(root):
            fails.append('01.DB の無い車種フォルダを揃っていると見なした')
        # 壊れた base64・空のファイルがあるフォルダは書かない（完了印も付けない）
        n5, _t, dropped5 = bridge.store(root, {'W/W95/W9501.DB': 'not*base64!', 'W/W95/W9502.DB': b(b'ok')})
        n6, _t, dropped6 = bridge.store(root, {'W/W94/W9401.DB': '', 'W/W94/W9402.DB': b(b'ok')})
        if n5 or n6 or bridge.has_car(root, 'W95') or bridge.has_car(root, 'W94') or not dropped5 or not dropped6:
            fails.append(f'壊れた base64／空ファイル: n5={n5} n6={n6} dropped5={dropped5} dropped6={dropped6}')
        # COM を取り直したら、前の車種フォルダと完了印は消える（別の Addata との混在を防ぐ）
        ss['_bridge_seen'] = None
        bridge.ingest(ss, {'seq': 22, 'phase': 'com', 'root_name': 'Addata2', 'files': files_of(os.path.join(ADDATA, 'COM'), 'COM', False)})
        if bridge.cars(root) or bridge.has_car(root, 'W99') or not bridge.has_com(root) or os.path.isdir(os.path.join(root, 'W')):
            fails.append(f'COM の取り直しで前の車種フォルダが残る: cars={bridge.cars(root)}')
        # COM の取り直しに失敗（壊れた payload）したら、古い COM のまま「使用中」にならない
        msg7 = bridge.ingest(ss, {'seq': 23, 'phase': 'com', 'root_name': 'Addata3', 'files': {'COM/KA06_ALL.DB': 'bad*b64', 'COM/AnVer.DB': b(b'x')}})
        if bridge.has_com(root) or '取り込めませんでした' not in (msg7 or ''):
            fails.append(f'失敗した COM の取り直しで古い COM が残る: has_com={bridge.has_com(root)} msg={msg7!r}')
        # COM の無いフォルダを選び直した（部品が error 付きの com を送る）: 前の Addata は消えて未接続に戻る
        bridge.ingest(ss, {'seq': 24, 'phase': 'com', 'root_name': 'Addata5', 'files': files_of(os.path.join(ADDATA, 'COM'), 'COM', False)})
        msg8 = bridge.ingest(ss, {'seq': 25, 'phase': 'com', 'root_name': 'Desktop', 'files': {}, 'error': 'COM フォルダがありません'})
        if bridge.has_com(root) or 'COM フォルダがありません' not in (msg8 or ''):
            fails.append(f'COM 無しの選び直しで前の Addata が残る: has_com={bridge.has_com(root)} msg={msg8!r}')
        # 車種フォルダ待ちの途中で COM の無いフォルダを選んでも、待ち（取り置きからの再開）は残す。車種フォルダ側の error は待ちを解く
        ss['_bridge_want'] = 'W54'
        bridge.ingest(ss, {'seq': 26, 'phase': 'com', 'root_name': 'Desktop', 'files': {}, 'error': 'COM フォルダがありません'})
        if ss.get('_bridge_want') != 'W54':
            fails.append('COM の error で車種フォルダ待ちが消えた（取り置きが捨てられて読み直しになる）')
        # COM を受け取っていない間の車種フォルダの error では待ちを解かない
        bridge.ingest(ss, {'seq': 27, 'phase': 'car', 'car': 'W54', 'files': {}, 'error': 'ない'})
        if ss.get('_bridge_want') != 'W54':
            fails.append('COM の無い間の車種フォルダの error で待ちが消えた')
        bridge.ingest(ss, {'seq': 28, 'phase': 'com', 'root_name': 'Addata6', 'files': files_of(os.path.join(ADDATA, 'COM'), 'COM', False)})
        # 別の（前の）車種の遅れた error では待ちを解かない。いま待っている車種の error で解く
        bridge.ingest(ss, {'seq': 29, 'phase': 'car', 'car': 'J87', 'files': {}, 'error': 'ない'})
        if ss.get('_bridge_want') != 'W54':
            fails.append('別の車種の error で待ちが消えた')
        bridge.ingest(ss, {'seq': 30, 'phase': 'car', 'car': 'W54', 'files': {}, 'error': 'ない'})
        if ss.get('_bridge_want') != '':
            fails.append('待っている車種の error で待ちが解けない')
        # AnVer.DB も COM.CAB も無い COM は「届いた」と見なさない（vendor の部分 Addata の条件と同じ）
        bridge.ingest(ss, {'seq': 31, 'phase': 'com', 'root_name': 'Addata4', 'files': {'COM/KA06_ALL.DB': b(b'x'), 'COM/KA81.DB': b(b'x')}})
        if bridge.has_com(root):
            fails.append('AnVer.DB も COM.CAB も無い COM を has_com が通した（vendor は弾く）')
        msg1 = bridge.ingest(ss, {'seq': 32, 'phase': 'car', 'car': 'W99', 'files': {}})
        msg2 = bridge.ingest(ss, {'seq': 32, 'phase': 'car', 'car': 'W99', 'files': {}})
        if not msg1 or msg2 is not None:
            fails.append(f'ingest の二重処理防止: {msg1!r} / {msg2!r}')
        # 遅れて届いた古い値（seq が小さい）も受けない。別の iframe（nonce 違い）は独立
        if bridge.ingest(ss, {'seq': 30, 'phase': 'car', 'car': 'W99', 'files': {}}) is not None:
            fails.append('古い seq の値を受けてしまう')
        if not bridge.ingest(ss, {'seq': 1, 'nonce': 'other', 'phase': 'car', 'car': 'W99', 'files': {}}):
            fails.append('別の nonce の seq 1 を受けない')
        if bridge.ingest(ss, {'seq': 2, 'nonce': 'other', 'phase': 'car', 'car': 'W98', 'files': {}, 'error': 'ない'}) != 'ない' or ss.get('_bridge_want', '') != '':
            fails.append('ingest の error の扱い')
        # 接続の解除: 一時フォルダ・取り置き・待ち・状態が全部消える
        ss2 = {}
        r2 = bridge.root(ss2)
        bridge.store(r2, files_of(os.path.join(ADDATA, 'COM'), 'COM', False))
        cd2 = maker.new_case_dir()
        ss2['_bridge_pending'] = {'case_dir': cd2}; ss2['_bridge_want'] = 'W54'; ss2['_bridge_path'] = r2; ss2['_bridge_msg'] = 'x'
        com_val = {'seq': 1, 'nonce': 'abc', 'phase': 'com', 'root_name': 'Addata', 'files': files_of(os.path.join(ADDATA, 'COM'), 'COM', False)}
        bridge.ingest(ss2, com_val)
        bridge.disconnect(ss2)
        if os.path.exists(r2) or os.path.exists(cd2) or any(k.startswith('_bridge') and k != '_bridge_seen' for k in ss2):
            fails.append(f'disconnect が消し残す: dir={os.path.exists(r2)} case={os.path.exists(cd2)} keys={[k for k in ss2 if k.startswith("_bridge")]}')
        # 解除の直後、部品が返す古い値（同じ seq・nonce）は処理しない ＝ 黙って再接続しない。iframe を読み直した新しい値（別の nonce）は処理する
        if bridge.ingest(ss2, com_val) is not None or bridge.has_com(bridge.root(ss2)):
            fails.append('解除の直後に古い COM の値で再接続した')
        if not bridge.ingest(ss2, dict(com_val, nonce='def')) or not bridge.has_com(bridge.root(ss2)):
            fails.append('別の iframe（nonce 違い）の新しい COM を処理しない')
        shutil.rmtree(bridge.root(ss2), ignore_errors=True)
        # いまの iframe（nonce 'other'）に切り替わった後、前の iframe（nonce ''）の遅れた値は seq が大きくても受けない
        if bridge.ingest(ss, {'seq': 40, 'phase': 'car', 'car': 'W98', 'files': {}}) is not None:
            fails.append('前の iframe の遅れた値を受けてしまう')
        old = os.path.join(tempfile.gettempdir(), bridge.PREFIX + 'old_test')
        os.makedirs(old, exist_ok=True); os.utime(old, (time.time() - 8 * 3600, time.time() - 8 * 3600))
        bridge.sweep(6 * 3600)
        if os.path.exists(old) or not os.path.exists(root):
            fails.append('sweep が古いものを消さない／新しいものを消した')
    finally:
        shutil.rmtree(root, ignore_errors=True)
    for f in fails:
        print('*** FAILED:', f)
    print('reg_bridge:', 'all ok' if not fails else f'{len(fails)} 件が不合格')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
