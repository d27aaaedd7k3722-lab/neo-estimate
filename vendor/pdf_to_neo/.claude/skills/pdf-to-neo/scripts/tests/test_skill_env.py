# -*- coding: utf-8 -*-
"""環境解決（ADDATA / コグニ / 出力の文字コード）の単体テスト。
PC ごとに置き場所が違っても動くことを、偽の最小 ADDATA ツリーで確かめる。ADDATA 実物は要らない。
    cd files && python .claude/skills/pdf-to-neo/scripts/tests/test_skill_env.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import skill_env  # noqa: E402

ROOT = os.path.join(tempfile.gettempdir(), 'pdf_to_neo_env_test')


def _make_addata(base: str) -> str:
    """is_addata() を満たす最小の偽 ADDATA（COM/AnVer.DB + メーカー別フォルダ 7 個 + <car>01.DB）"""
    os.makedirs(os.path.join(base, 'COM'), exist_ok=True)
    with open(os.path.join(base, 'COM', 'AnVer.DB'), 'wb') as fh:
        fh.write(b'x')
    for m in 'ABCDEFG':
        os.makedirs(os.path.join(base, m, m + '01'), exist_ok=True)
    with open(os.path.join(base, 'A', 'A01', 'A0101.DB'), 'wb') as fh:
        fh.write(b'x')
    return base


def _set_version(base: str, num: str) -> None:
    """偽 ADDATA のデータ版（COM/AnVer.DB は XOR 0xff の INI）を書き換える"""
    t = ('[Version]' + '\r\n' + 'Number=' + num + '\r\n').encode('cp932')
    with open(os.path.join(base, 'COM', 'AnVer.DB'), 'wb') as fh:
        fh.write(bytes(x ^ 0xFF for x in t))


def _with_fake_drive(rel: str):
    """<偽ドライブ>/<rel> に ADDATA を置いて find_addata() を呼ぶ"""
    shutil.rmtree(ROOT, ignore_errors=True)
    base = _make_addata(os.path.join(ROOT, rel))
    orig = skill_env._drives
    skill_env._drives = lambda: [ROOT + os.sep]
    try:
        return base, skill_env.find_addata('')
    finally:
        skill_env._drives = orig
        shutil.rmtree(ROOT, ignore_errors=True)


def test_is_addata_rejects_incomplete():
    shutil.rmtree(ROOT, ignore_errors=True)
    only_com = os.path.join(ROOT, 'onlycom')
    os.makedirs(os.path.join(only_com, 'COM'), exist_ok=True)
    open(os.path.join(only_com, 'COM', 'AnVer.DB'), 'wb').write(b'x')
    assert not skill_env.is_addata(only_com), 'COM だけのフォルダを ADDATA と誤認している'
    assert not skill_env.is_addata(os.path.join(ROOT, 'nothing')), '存在しないパスを ADDATA と誤認している'
    assert skill_env.is_addata(_make_addata(os.path.join(ROOT, 'full'))), '正しい ADDATA を弾いている'
    shutil.rmtree(ROOT, ignore_errors=True)


def test_find_addata_reaches_common_layouts():
    """PC ごとに違う置き方（ドライブ直下・Audatex 配下・数階層下・版付きの名前）まで届く"""
    for rel in ('Addata', 'ADDATA', os.path.join('Audatex', 'Addata'), os.path.join('業務データ', 'Addata'),
                os.path.join('a', 'b', 'Addata'), os.path.join('a', 'b', 'c', 'Addata'), 'Addata2026'):
        base, got = _with_fake_drive(rel)
        assert got and os.path.normcase(got) == os.path.normcase(base), f'{rel} を見つけられない（{got!r}）'


def _two_roots():
    """古い C:\Addata 相当（浅い）と、新しい本物（深い）を同じ偽ドライブに置く"""
    shutil.rmtree(ROOT, ignore_errors=True)
    shallow = _make_addata(os.path.join(ROOT, 'Addata'))
    deep = _make_addata(os.path.join(ROOT, 'share', 'team', 'cogni', 'Addata2026'))
    _set_version(shallow, '2025/04')
    _set_version(deep, '2026/08')
    return shallow, deep


def test_deep_scan_is_opt_in_but_finds_newer():
    """既定は浅い候補で即決（速さ優先）。ADDATA_SCAN_DEEP=1 なら深い場所の新しい版を選ぶ"""
    shallow, deep = _two_roots()
    o_d, o_n = skill_env._drives, skill_env._drives_network
    skill_env._drives = lambda: [ROOT + os.sep]
    skill_env._drives_network = lambda: []
    old = os.environ.pop('ADDATA_SCAN_DEEP', None)
    try:
        got = skill_env.find_addata('')
        assert os.path.normcase(got) == os.path.normcase(shallow), \
            f'既定で深い探索まで走っている（毎回遅くなる）: {got}'
        os.environ['ADDATA_SCAN_DEEP'] = '1'
        got2 = skill_env.find_addata('')
        assert os.path.normcase(got2) == os.path.normcase(deep), \
            f'ADDATA_SCAN_DEEP=1 でも古い浅い方を選んでいる: {got2}'
    finally:
        os.environ.pop('ADDATA_SCAN_DEEP', None)
        if old is not None:
            os.environ['ADDATA_SCAN_DEEP'] = old
        skill_env._drives, skill_env._drives_network = o_d, o_n
        shutil.rmtree(ROOT, ignore_errors=True)


def test_find_addata_all_lists_newest_first():
    """設定の点検用。見つかる ADDATA を版の新しい順に返す（env_check が古い方を選んでいたら知らせる）"""
    shallow, deep = _two_roots()
    o_d, o_n = skill_env._drives, skill_env._drives_network
    skill_env._drives = lambda: [ROOT + os.sep]
    skill_env._drives_network = lambda: []
    try:
        got = skill_env.find_addata_all(20.0)
        paths = [os.path.normcase(q) for q, _v in got]
        assert os.path.normcase(shallow) in paths and os.path.normcase(deep) in paths, \
            f'候補を拾えていない: {got}'
        assert paths[0] == os.path.normcase(deep), f'新しい版が先頭に来ていない: {got}'
        assert got[0][1] == '2026/08', f'データ版を読めていない: {got}'
    finally:
        skill_env._drives, skill_env._drives_network = o_d, o_n
        shutil.rmtree(ROOT, ignore_errors=True)


def test_deep_scan_respects_deadline():
    """1 ドライブの走査は締切を過ぎたら打ち切る（ネットワークドライブで起動が止まらないように）"""
    shutil.rmtree(ROOT, ignore_errors=True)
    _make_addata(os.path.join(ROOT, 'a', 'b', 'c', 'Addata'))
    got: list = []
    skill_env._scan_one_drive(ROOT + os.sep, 0.0, got)  # 締切が過ぎている
    assert got == [], f'締切を過ぎても走査を続けている（{got}）'
    got2: list = []
    skill_env._scan_one_drive(ROOT + os.sep, __import__('time').time() + 10, got2)  # 締切に余裕があれば見つける
    assert got2, '締切に余裕があるのに見つけられていない'
    shutil.rmtree(ROOT, ignore_errors=True)


def test_pipeline_reads_saved_config():
    """生成器を直接呼ぶ入口（run_case.py 等）でも、env_check が保存した設定の ADDATA を使える"""
    sys.path.insert(0, os.path.join(skill_env.FILES, 'claude_neo_pipeline'))
    import addata_vehicle_resolver as R
    shutil.rmtree(ROOT, ignore_errors=True)
    base = _make_addata(os.path.join(ROOT, 'shared', 'Addata'))
    cfg = os.path.join(ROOT, 'cfg.json')
    json.dump({'ADDATA_ROOT': base}, open(cfg, 'w', encoding='utf-8'))
    old_cfg, old_cands, old_env = R.SKILL_CONFIG, R.ADDATA_ROOT_CANDIDATES, os.environ.pop('ADDATA_ROOT', None)
    try:
        R.SKILL_CONFIG, R.ADDATA_ROOT_CANDIDATES = cfg, []  # C:\Addata 等が無い PC を模す
        got = R.find_addata_root()
        assert os.path.normcase(got) == os.path.normcase(base), f'設定ファイルの ADDATA を使っていない（{got!r}）'
        os.environ['ADDATA_ROOT'] = base  # 環境変数が最優先
        assert os.path.normcase(R.find_addata_root()) == os.path.normcase(base)
    finally:
        R.SKILL_CONFIG, R.ADDATA_ROOT_CANDIDATES = old_cfg, old_cands
        os.environ.pop('ADDATA_ROOT', None)
        if old_env:
            os.environ['ADDATA_ROOT'] = old_env
        shutil.rmtree(ROOT, ignore_errors=True)


def test_use_utf8_io_is_safe():
    """差し替えられた stdout（reconfigure を持たない）でも例外にしない"""
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = io.StringIO()
        skill_env.use_utf8_io()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
def test_deep_scan_is_bounded_per_drive():
    """遅いドライブが 1 台あっても、予算どおりの時間で戻る（切断気味の共有で起動が固まらない）"""
    import time as _t
    orig = skill_env._scan_one_drive

    def slow(drive, deadline, out):
        _t.sleep(30)  # 返ってこない共有を模す
    skill_env._scan_one_drive = slow
    try:
        t0 = _t.time()
        got = skill_env._scan_addata_deep(['X:' + os.sep, 'Y:' + os.sep], 6.0)
        took = _t.time() - t0
    finally:
        skill_env._scan_one_drive = orig
    assert got == [], f'走査していないのに結果が返っている（{got}）'
    assert took < 20, f'予算 6 秒に対して {took:.1f} 秒かかっている（ドライブごとの上限が効いていない）'


def test_deep_scan_skips_junctions():
    """ジャンクションは辿らない（辿ると同じ場所を何度も降りて時間切れ・誤検出になる）"""
    import subprocess
    shutil.rmtree(ROOT, ignore_errors=True)
    real = _make_addata(os.path.join(ROOT, 'real', 'Addata'))
    link = os.path.join(ROOT, 'link')
    os.makedirs(ROOT, exist_ok=True)
    r = subprocess.run(['cmd', '/c', 'mklink', '/J', link, os.path.join(ROOT, 'real')], capture_output=True, text=True)
    if r.returncode != 0:
        print('   skip test_deep_scan_skips_junctions（ジャンクションを作れない環境）')
        shutil.rmtree(ROOT, ignore_errors=True)
        return
    try:
        got = skill_env._scan_addata_deep([ROOT + os.sep], 10.0)
        norm = {os.path.normcase(x) for x in got}
        assert os.path.normcase(real) in norm, f'実体の Addata を見つけられていない（{got}）'
        assert not any(os.path.normcase(link) in x for x in norm), f'ジャンクションを辿っている（{got}）'
    finally:
        try:
            os.rmdir(link)
        except OSError:
            pass
        shutil.rmtree(ROOT, ignore_errors=True)
def test_network_candidate_competes_with_fixed():
    """ネットワーク上に新しい ADDATA があるとき、古い固定ドライブの ADDATA に隠されない（データ版で選ぶ）"""
    shutil.rmtree(ROOT, ignore_errors=True)
    old = _make_addata(os.path.join(ROOT, 'fixed', 'Addata'))
    new = _make_addata(os.path.join(ROOT, 'net', 'Addata'))
    _set_version(old, '2024/01')
    _set_version(new, '2026/08')
    o_d, o_n, o_v = skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded
    skill_env._drives = lambda: [os.path.join(ROOT, 'fixed') + os.sep]
    skill_env._drives_network = lambda: [os.path.join(ROOT, 'net') + os.sep]
    try:
        got = skill_env.find_addata('')
        assert got and os.path.normcase(got) == os.path.normcase(new),             f'古い固定ドライブ側を選んでいる（{got!r} / 期待 {new}）'
    finally:
        skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded = o_d, o_n, o_v
        shutil.rmtree(ROOT, ignore_errors=True)


def test_network_probe_is_time_bounded():
    """応答しないネットワークドライブがあっても、検出は上限で打ち切られる（本体と同じ呼び方で差し替える）"""
    import time as _t
    orig = skill_env._addata_candidates
    called = []

    def slow(drives, shallow=False):  # 本体は shallow=True 付きで呼ぶので同じ形にする
        called.append(shallow)
        _t.sleep(30)
        return []
    skill_env._addata_candidates = slow
    try:
        t0 = _t.time()
        got = skill_env._valid_addata_bounded(['Z:' + os.sep], 2.0)
        took = _t.time() - t0
    finally:
        skill_env._addata_candidates = orig
    assert called, '差し替えた探索が呼ばれていない（引数の形が本体と違う可能性）'
    assert got == [], f'応答していないのに結果が返っている（{got}）'
    assert 1.5 < took < 10, f'上限 2 秒に対して {took:.1f} 秒（打ち切りの経路を通っていない）'


def test_deep_result_validation_is_time_bounded():
    """深い探索で拾った候補の検証（is_addata・版の読み取り）も上限の中で行う"""
    import time as _t
    o_scan, o_valid, o_is = skill_env._scan_addata_deep, skill_env.is_addata, skill_env.is_addata
    skill_env._scan_addata_deep = lambda drives, budget: ['Z:' + os.sep + 'Addata']

    def slow_is(p):
        _t.sleep(30)  # 応答しない共有の COM を読みに行った状態
        return True
    skill_env.is_addata = slow_is
    o_d, o_n, o_vb = skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded
    skill_env._drives, skill_env._drives_network = (lambda: []), (lambda: [])
    skill_env._valid_addata_bounded = lambda drives, seconds: []
    os.environ['ADDATA_SCAN_SECONDS'] = '2'
    try:
        t0 = _t.time()
        got = skill_env.find_addata('')
        took = _t.time() - t0
    finally:
        skill_env._scan_addata_deep, skill_env.is_addata = o_scan, o_is
        skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded = o_d, o_n, o_vb
        os.environ.pop('ADDATA_SCAN_SECONDS', None)
    assert got == '', f'検証が終わっていないのに結果が返っている（{got!r}）'
    assert took < 10, f'上限 2 秒に対して {took:.1f} 秒（深い探索の検証が時間制限の外にある）'
def test_slow_network_does_not_discard_local():
    """ネットワーク候補の版読み取りが返らなくても、(1) 検出は待たされない (2) 正常なローカル ADDATA を巻き添えで捨てない"""
    import time as _t
    shutil.rmtree(ROOT, ignore_errors=True)
    local = _make_addata(os.path.join(ROOT, 'fixed', 'Addata'))
    _set_version(local, '2026/08')
    o_d, o_n, o_vb, o_key = (skill_env._drives, skill_env._drives_network,
                             skill_env._valid_addata_bounded, skill_env.addata_version_key)
    skill_env._drives = lambda: [os.path.join(ROOT, 'fixed') + os.sep]
    skill_env._drives_network = lambda: ['Z:' + os.sep]
    skill_env._valid_addata_bounded = lambda drives, seconds: ['Z:' + os.sep + 'Addata']  # 検証は通った扱い
    real_key = o_key

    def slow_key(q):
        if q.upper().startswith('Z:'):
            _t.sleep(30)  # 応答しない共有の AnVer.DB
            return (99.0, 0.0)
        return real_key(q)
    skill_env.addata_version_key = slow_key
    os.environ['ADDATA_SCAN_SECONDS'] = '5'
    try:
        t0 = _t.time()
        got = skill_env.find_addata('')
        took = _t.time() - t0
    finally:
        (skill_env._drives, skill_env._drives_network,
         skill_env._valid_addata_bounded, skill_env.addata_version_key) = o_d, o_n, o_vb, o_key
        os.environ.pop('ADDATA_SCAN_SECONDS', None)
        shutil.rmtree(ROOT, ignore_errors=True)
    assert took < 8, f'応答しない共有で {took:.1f} 秒待っている（ネットワーク探索の上限 3 秒が効いていない）'
    assert got and os.path.normcase(got) == os.path.normcase(local),         f'正常なローカル ADDATA を巻き添えで捨てている（{got!r} / 期待 {local}）'
def test_scan_seconds_accepts_garbage():
    """ADDATA_SCAN_SECONDS に数値以外が入っていても落ちない（社内配布で '30s' などと書かれても動く）"""
    for v in ('abc', '30s', '', '0', '-5'):
        os.environ['ADDATA_SCAN_SECONDS'] = v
        try:
            skill_env.find_addata('')  # 例外を出さないことだけ見る（結果はその PC 次第）
        except ValueError as e:
            raise AssertionError(f'ADDATA_SCAN_SECONDS={v!r} で落ちる: {e}') from None
        finally:
            os.environ.pop('ADDATA_SCAN_SECONDS', None)


def test_dead_candidates_do_not_scale_time():
    """応答しない候補が増えても待ち時間が候補数に比例しない（締切を過ぎたら候補の有無を問わず打ち切る）"""
    import time as _t
    o_d, o_n, o_vb, o_scan, o_is = (skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded,
                                    skill_env._scan_addata_deep, skill_env.is_addata)
    skill_env._drives = lambda: []
    skill_env._drives_network = lambda: ['Z:' + os.sep]
    skill_env._valid_addata_bounded = lambda drives, seconds: []

    def dead(p):
        _t.sleep(30)
        return False
    skill_env.is_addata = dead
    took = {}
    try:
        for n in (3, 30):
            skill_env._scan_addata_deep = lambda drives, budget, _n=n: ['Z:' + os.sep + 'Addata%d' % i for i in range(_n)]
            os.environ['ADDATA_SCAN_SECONDS'] = '5'
            t0 = _t.time()
            skill_env.find_addata('')
            took[n] = _t.time() - t0
    finally:
        (skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded,
         skill_env._scan_addata_deep, skill_env.is_addata) = o_d, o_n, o_vb, o_scan, o_is
        os.environ.pop('ADDATA_SCAN_SECONDS', None)
    assert took[30] < took[3] + 8, f'候補 3 個 {took[3]:.1f} 秒 / 30 個 {took[30]:.1f} 秒 —— 候補数に比例して伸びている'
    assert took[30] < 25, f'候補 30 個で {took[30]:.1f} 秒（上限 5 秒指定に対して長すぎる）'
def test_bom_json_is_readable():
    """メモ帳などで保存した BOM 付き JSON でも読める（設定ファイル・estimate.json）"""
    import json as _json
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    base = _make_addata(os.path.join(ROOT, 'Addata'))
    cfg = os.path.join(ROOT, 'cfg.json')
    with io.open(cfg, 'w', encoding='utf-8-sig') as fh:   # BOM 付きで書く
        fh.write(_json.dumps({'ADDATA_ROOT': base}, ensure_ascii=False))
    sys.path.insert(0, os.path.join(skill_env.FILES, 'claude_neo_pipeline'))
    import addata_vehicle_resolver as R
    old_cfg, old_cands, old_env = R.SKILL_CONFIG, R.ADDATA_ROOT_CANDIDATES, os.environ.pop('ADDATA_ROOT', None)
    try:
        R.SKILL_CONFIG, R.ADDATA_ROOT_CANDIDATES = cfg, []
        got = R.find_addata_root()
        assert os.path.normcase(got) == os.path.normcase(base), f'BOM 付き設定を読めていない（{got!r}）'
    finally:
        R.SKILL_CONFIG, R.ADDATA_ROOT_CANDIDATES = old_cfg, old_cands
        os.environ.pop('ADDATA_ROOT', None)
        if old_env:
            os.environ['ADDATA_ROOT'] = old_env
        shutil.rmtree(ROOT, ignore_errors=True)
def test_local_is_checked_even_when_budget_spent():
    """応答しないネットワークに予算を全部取られても、固定ドライブの ADDATA は必ず確認する"""
    import time as _t
    shutil.rmtree(ROOT, ignore_errors=True)
    local = _make_addata(os.path.join(ROOT, 'fixed', 'Addata'))
    o_d, o_n, o_vb = skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded
    skill_env._drives = lambda: [os.path.join(ROOT, 'fixed') + os.sep]
    skill_env._drives_network = lambda: ['Z:' + os.sep]

    def eat_budget(drives, seconds):
        _t.sleep(max(0.0, seconds))  # 予算を使い切る応答しない共有
        return []
    skill_env._valid_addata_bounded = eat_budget
    o_scan = skill_env._scan_addata_deep
    skill_env._scan_addata_deep = lambda drives, budget: []  # 深い探索に助けられないようにして、1 段目だけを見る
    os.environ['ADDATA_SCAN_SECONDS'] = '1'
    try:
        got = skill_env.find_addata('')
    finally:
        skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded = o_d, o_n, o_vb
        skill_env._scan_addata_deep = o_scan
        os.environ.pop('ADDATA_SCAN_SECONDS', None)
        shutil.rmtree(ROOT, ignore_errors=True)
    assert got and os.path.normcase(got) == os.path.normcase(local),         f'固定ドライブの ADDATA を確認しないまま終わっている（{got!r} / 期待 {local}）'
def test_pipeline_scan_does_not_leak_env():
    """生成器から呼ぶ最終手段の走査が ADDATA_SCAN_SECONDS を書き換えたまま残さない"""
    sys.path.insert(0, os.path.join(skill_env.FILES, 'claude_neo_pipeline'))
    import addata_vehicle_resolver as R
    os.environ['ADDATA_SCAN_SECONDS'] = '60'
    try:
        R._addata_by_scan()
        assert os.environ.get('ADDATA_SCAN_SECONDS') == '60',             f"呼び出し後に値が変わっている（{os.environ.get('ADDATA_SCAN_SECONDS')!r}）"
    finally:
        os.environ.pop('ADDATA_SCAN_SECONDS', None)
    R._addata_by_scan()  # もともと未設定なら、呼び出し後も未設定に戻る
    assert 'ADDATA_SCAN_SECONDS' not in os.environ, '未設定だったのに環境変数が残っている'
def test_dead_share_does_not_hide_other_share():
    """応答しない共有が 1 台あっても、別の共有にある ADDATA を見逃さない（ドライブごとに独立して探す）"""
    import time as _t
    shutil.rmtree(ROOT, ignore_errors=True)
    good = _make_addata(os.path.join(ROOT, 'net2', 'Addata'))
    dead_root = os.path.join(ROOT, 'net1') + os.sep
    o_is = skill_env.is_addata
    real_is = o_is

    def is_addata_slow(p):
        if os.path.normcase(p).startswith(os.path.normcase(dead_root)):
            _t.sleep(30)  # 1 台目は返ってこない共有
            return False
        return real_is(p)
    skill_env.is_addata = is_addata_slow
    os.makedirs(os.path.join(ROOT, 'net1', 'Addata'), exist_ok=True)  # 候補としては見える
    try:
        t0 = _t.time()
        got = skill_env._valid_addata_bounded([dead_root, os.path.join(ROOT, 'net2') + os.sep], 5.0)
        took = _t.time() - t0
    finally:
        skill_env.is_addata = o_is
        shutil.rmtree(ROOT, ignore_errors=True)
    assert took < 15, f'{took:.1f} 秒かかっている（ドライブごとに並行していない）'
    assert any(os.path.normcase(x) == os.path.normcase(good) for x in got),         f'応答しない共有に隠れて 2 台目の ADDATA を見逃している（{got}）'
def test_stale_config_path_does_not_hang():
    """設定に残った「切断済みの共有」を指す ADDATA_ROOT で固まらず、次の手段に進む"""
    import time as _t
    sys.path.insert(0, os.path.join(skill_env.FILES, 'claude_neo_pipeline'))
    import addata_vehicle_resolver as R
    shutil.rmtree(ROOT, ignore_errors=True)
    good = _make_addata(os.path.join(ROOT, 'Addata'))
    o_isdir = os.path.isdir
    dead = 'Q:' + os.sep + 'dead_share'

    def slow_isdir(q):
        if os.path.normcase(str(q)).startswith(os.path.normcase(dead)):
            _t.sleep(30)   # 返ってこない共有
            return True
        return o_isdir(q)
    cfg = os.path.join(ROOT, 'cfg.json')
    import json as _json
    _json.dump({'ADDATA_ROOT': dead}, io.open(cfg, 'w', encoding='utf-8'))
    old_cfg, old_cands, old_env = R.SKILL_CONFIG, R.ADDATA_ROOT_CANDIDATES, os.environ.pop('ADDATA_ROOT', None)
    os.path.isdir = slow_isdir
    try:
        R.SKILL_CONFIG, R.ADDATA_ROOT_CANDIDATES = cfg, [good]
        t0 = _t.time()
        got = R.find_addata_root()
        took = _t.time() - t0
    finally:
        os.path.isdir = o_isdir
        R.SKILL_CONFIG, R.ADDATA_ROOT_CANDIDATES = old_cfg, old_cands
        os.environ.pop('ADDATA_ROOT', None)
        if old_env:
            os.environ['ADDATA_ROOT'] = old_env
        shutil.rmtree(ROOT, ignore_errors=True)
    assert took < 15, f'切断済みの設定で {took:.1f} 秒固まっている'
    assert os.path.normcase(got) == os.path.normcase(good), f'次の手段に進んでいない（{got!r}）'
def test_resolve_does_not_hang_on_stale_config():
    """設定に残った「切断済みの共有」を指す ADDATA_ROOT でも skill_env.resolve が固まらない"""
    import time as _t
    shutil.rmtree(ROOT, ignore_errors=True)
    good = _make_addata(os.path.join(ROOT, 'Addata'))
    dead = 'Q:' + os.sep + 'dead_share'
    o_load, o_is, o_find, o_cogni = skill_env.load_config, skill_env.is_addata, skill_env.find_addata, skill_env.find_cogni
    real_is = o_is

    def slow_is(q):
        if os.path.normcase(str(q)).startswith(os.path.normcase(dead)):
            _t.sleep(30)   # 返ってこない共有
            return True
        return real_is(q)
    skill_env.load_config = lambda: {'ADDATA_ROOT': dead}
    skill_env.is_addata = slow_is
    skill_env.find_addata = lambda cogni='': good
    skill_env.find_cogni = lambda: ''
    old_env = os.environ.pop('ADDATA_ROOT', None)
    try:
        t0 = _t.time()
        got = skill_env.resolve()['ADDATA_ROOT']
        took = _t.time() - t0
    finally:
        skill_env.load_config, skill_env.is_addata = o_load, o_is
        skill_env.find_addata, skill_env.find_cogni = o_find, o_cogni
        if old_env:
            os.environ['ADDATA_ROOT'] = old_env
        shutil.rmtree(ROOT, ignore_errors=True)
    assert took < 15, f'切断済みの設定で {took:.1f} 秒固まっている'
    assert os.path.normcase(got) == os.path.normcase(good), f'自動検出まで進んでいない（{got!r}）'
def test_network_probe_cap_is_short():
    """ローカルに ADDATA がある PC では、応答しない共有があっても起動時の待ちが短い（ネットワーク探索の上限）"""
    import time as _t
    shutil.rmtree(ROOT, ignore_errors=True)
    local = _make_addata(os.path.join(ROOT, 'fixed', 'Addata'))
    o_d, o_n, o_vb = skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded
    skill_env._drives = lambda: [os.path.join(ROOT, 'fixed') + os.sep]
    skill_env._drives_network = lambda: ['Z:' + os.sep]

    def probe(drives, seconds):
        _t.sleep(max(0.0, seconds))  # 渡された上限いっぱい返ってこない共有
        return []
    skill_env._valid_addata_bounded = probe
    try:
        t0 = _t.time()
        got = skill_env.find_addata('')
        took = _t.time() - t0
    finally:
        skill_env._drives, skill_env._drives_network, skill_env._valid_addata_bounded = o_d, o_n, o_vb
        shutil.rmtree(ROOT, ignore_errors=True)
    assert os.path.normcase(got) == os.path.normcase(local), f'ローカルの ADDATA を返していない（{got!r}）'
    assert took < 8, f'応答しない共有のせいで {took:.1f} 秒待っている（ネットワーク探索の上限が長すぎる）'
def test_same_drive_later_candidate_is_found():
    """同じ共有の先頭候補（Z:\Addata）が応答しなくても、後続の候補（Z:\Audatex\Addata）を見逃さない"""
    import time as _t
    dead = 'Z:' + os.sep + 'Addata'
    good = 'Z:' + os.sep + 'Audatex' + os.sep + 'Addata'
    o_is = skill_env.is_addata

    def fake_is(p):
        if os.path.normcase(p) == os.path.normcase(dead):
            _t.sleep(30)   # 応答しない候補
            return False
        return os.path.normcase(p) == os.path.normcase(good)
    skill_env.is_addata = fake_is
    try:
        t0 = _t.time()
        got = skill_env._valid_addata_bounded(['Z:' + os.sep], 5.0)
        took = _t.time() - t0
    finally:
        skill_env.is_addata = o_is
    assert took < 12, f'{took:.1f} 秒かかっている（候補ごとに並行していない）'
    assert any(os.path.normcase(x) == os.path.normcase(good) for x in got),         f'先頭候補に隠れて後続の ADDATA を見逃している（{got}）'


def test_find_addata_all_lists_the_obvious_one():
    """点検用の一覧は、目の前の ADDATA（よくある置き方）を必ず返す。
    深い探索を先に走らせて予算を使い切ると空を返してしまう（2026-09-11 の不具合）"""
    root = skill_env.find_addata()
    if not root:
        print('     （この PC に ADDATA が無いので検査しない）')
        return
    got = [p for p, _v in skill_env.find_addata_all(budget=20.0)]
    assert got, 'ADDATA があるのに一覧が空（深い探索が予算を食い潰していないか）'
    norm = {os.path.normcase(os.path.abspath(x)) for x in got}
    assert os.path.normcase(os.path.abspath(root)) in norm,         f'自動検出した {root} が一覧に無い（{got}）'


def test_partial_root_only_with_flag():
    """ADDATA_ROOT_PARTIAL=1 のときだけ、環境変数 ADDATA_ROOT の「COM ＋ 車種フォルダ」の部分コピー（アプリの橋渡し）を採る。
    旗が無ければ弾く（自動検出に落ちる）。設定ファイル・自動検出の候補は旗があっても部分コピーを採らない"""
    shutil.rmtree(ROOT, ignore_errors=True)
    part = os.path.join(ROOT, 'addata_bridge_test')
    os.makedirs(os.path.join(part, 'COM'), exist_ok=True)
    for n in ('AnVer.DB', 'KA06_ALL.DB'):
        open(os.path.join(part, 'COM', n), 'wb').write(b'x')
    os.makedirs(os.path.join(part, 'W', 'W54'), exist_ok=True)
    open(os.path.join(part, 'W', 'W54', 'W5401.DB'), 'wb').write(b'x')
    assert not skill_env.is_addata(part), '部分コピーを is_addata が通してしまう（自動検出で拾われる）'
    assert skill_env.is_addata_partial(part), '部分コピーを is_addata_partial が弾いている'
    assert not skill_env.is_addata_partial(os.path.join(ROOT, 'nothing')), '存在しないパスを部分コピーと誤認'
    keep = {k: os.environ.get(k) for k in ('ADDATA_ROOT', 'ADDATA_ROOT_PARTIAL')}
    try:
        os.environ['ADDATA_ROOT'] = part
        os.environ.pop('ADDATA_ROOT_PARTIAL', None)
        assert not skill_env.is_addata_or_partial(part), '旗なしで部分コピーを採ってしまう'
        os.environ['ADDATA_ROOT_PARTIAL'] = '1'
        assert skill_env.is_addata_or_partial(part), '旗ありで部分コピーを採らない'
        os.environ['ADDATA_ROOT'] = os.path.join(ROOT, 'other')
        assert not skill_env.is_addata_or_partial(part), '環境変数と違うパスまで部分コピーとして採ってしまう'
        os.environ['ADDATA_ROOT'] = part
        got = skill_env.resolve().get('ADDATA_ROOT')
        assert os.path.normcase(os.path.abspath(got or '')) == os.path.normcase(os.path.abspath(part)), \
            f'resolve() が部分コピーを採らず {got!r} に落ちた（自動検出に化けると別の版で作る）'
        os.environ.pop('ADDATA_ROOT_PARTIAL', None)
        got2 = skill_env.resolve().get('ADDATA_ROOT')
        assert os.path.normcase(os.path.abspath(got2 or '')) != os.path.normcase(os.path.abspath(part)), \
            '旗なしの resolve() が部分コピーを採ってしまう'
        # 旗ありで環境変数が無効（COM が届いていない）: 自動検出・既定パスに落とさず空（fail closed）
        os.environ['ADDATA_ROOT_PARTIAL'] = '1'
        os.environ['ADDATA_ROOT'] = os.path.join(ROOT, 'nothing')
        got3 = skill_env.resolve().get('ADDATA_ROOT')
        assert got3 == '', f'旗ありで無効な環境変数なのに {got3!r} に落ちた（別の Addata で黙って作る）'
        sys.path.insert(0, os.path.join(skill_env.FILES, 'claude_neo_pipeline'))
        import addata_vehicle_resolver as _avr
        try:
            _avr.find_addata_root()
            raise AssertionError('resolver の find_addata_root() が旗ありで無効な環境変数から既定パス・探索に落ちた')
        except FileNotFoundError:
            pass
        os.environ['ADDATA_ROOT'] = part
        assert os.path.normcase(os.path.abspath(_avr.find_addata_root())) == os.path.normcase(os.path.abspath(part)), \
            'resolver が部分コピー（COM あり）の環境変数を採らない'
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(ROOT, ignore_errors=True)


def test_find_addata_all_keeps_the_budget():
    """一覧は与えた予算をおおむね守る（呼び出し全体が返らなくならない）"""
    import time as _t
    t0 = _t.time()
    skill_env.find_addata_all(budget=6.0)
    spent = _t.time() - t0
    assert spent < 6.0 * 2.5 + 5, f'予算 6 秒に対して {spent:.1f} 秒かかっている'


if __name__ == '__main__':
    skill_env.use_utf8_io()
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                fails += 1
                print('FAIL', name, e)
    print('skill_env tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
