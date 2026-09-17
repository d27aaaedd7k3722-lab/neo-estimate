# -*- coding: utf-8 -*-
"""生成 NEO の内部整合（規模によらず成り立つべきこと）
 - 明細（ERParts）・明細一覧（AnSMB）・損傷部品（DamageParts）の行数が揃う
 - AnSMB 100 桁が ERParts.OrderFlag と一致する
 - SQLite が壊れていない
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_consistency.py
"""
from __future__ import annotations

import glob
import time
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import neo_container as _nc  # noqa: E402
from estimate_to_neo import AddataParts, NeoBuilder  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': 'YR586P'}
BASE = {'source': 'unit_consistency', 'issuer': '', 'est_date': '20260909', 'vehicle': VEH, 'customer': {}, 'insurance': {},
        'labor_rate': 8000, 'items': [], 'paint': {}, 'expenses': [], 'totals': {}}


def inspect(neo: bytes) -> dict:
    ck = _nc.find_real_cks(neo)
    dec = _nc.decompress_neo(neo, ck)
    _m, entries = _nc.parse_entries(neo, ck[0])
    files = _nc.extract_files(dec, entries)
    t = tempfile.NamedTemporaryFile(delete=False, suffix='.sld')
    t.write(files['AnSvEm0001.sld'])
    t.close()
    con = sqlite3.connect(t.name)
    con.row_factory = sqlite3.Row
    er = [dict(r) for r in con.execute('SELECT * FROM ERParts')]
    dmg = con.execute('SELECT COUNT(*) FROM DamageParts').fetchone()[0]
    integ = con.execute('PRAGMA integrity_check').fetchone()[0]
    con.close()
    os.unlink(t.name)
    smb = [l for l in files.get('AnSMB.txt', b'').split(b'\r\n') if l]
    return {'er': er, 'smb': smb, 'dmg': dmg, 'integ': integ, 'files': list(files)}


def build(items):
    est = dict(BASE)
    est['items'] = items
    return NeoBuilder().build(est, VEH, labor_rate=8000, est_date='20260909', insurance={})[0]


def refs(n):
    nb = NeoBuilder()
    ap = AddataParts(nb.engine, 'J87')
    ap._load_12_blocks('J87')
    out = []
    for ref in sorted(ap.name20_by_ref):
        names = [x.strip() for x in ap.name20_by_ref[ref] if x.strip()]
        if names:
            out.append({'code': f'{ref:04d}', 'name': names[0], 'method': '取替', 'qty': 1, 'price': 1000, 'wage': 0})
        if len(out) >= n:
            break
    return out


def _assert_consistent(neo, label):
    d = inspect(neo)
    assert d['integ'] == 'ok', f'{label}: SQLite が壊れている'
    assert len(d['er']) == len(d['smb']), f"{label}: 明細 {len(d['er'])} 行 / AnSMB {len(d['smb'])} 行"
    assert len(d['er']) == d['dmg'], f"{label}: 明細 {len(d['er'])} 行 / DamageParts {d['dmg']} 行"
    for l in d['smb']:
        ln = int(l[0:8])
        row = next((r for r in d['er'] if int(r['LineNo']) == ln), None)
        assert row is not None, f'{label}: AnSMB の行 {ln} に対応する明細が無い'
        want = ' ' if row.get('OrderFlag') in (None, '') else str(row['OrderFlag'])
        assert l[100:101].decode('ascii', 'replace') == want, f'{label}: AnSMB 100 桁が OrderFlag と違う（行 {ln}）'
    assert not any(k.startswith('04011103') for k in d['files']), f'{label}: 雛形の見積番号が残っている'


def test_small_case():
    _assert_consistent(build(refs(5)), '5 行')


def test_large_case():
    _assert_consistent(build(refs(200)), '200 行')


def test_manual_and_reserve_rows():
    items = refs(3) + [
        {'name': 'ｵﾘｼﾞﾅﾙｽﾃｯｶｰ', 'method': '取替', 'qty': 1, 'price': 3000, 'wage': 0, 'manual': True},
        {'code': '2599', 'name': '左ﾄﾞｱﾊﾞｲｻﾞ', 'method': '脱着', 'qty': 1, 'wage': 2210, 'index': 0.3, 'reserve': True},
        {'code': '7600', 'name': '右Fﾀﾞﾝﾊﾟｰ', 'method': '点検調整', 'qty': 1, 'wage': 2940, 'index': 0.4},
    ]
    _assert_consistent(build(items), '手入力・保留・点検調整')


def _dmg(neo):
    """生成 NEO から DamageParts を (PartsCode, BlockCode, PartsType) の並びで取り出す"""
    ck = _nc.find_real_cks(neo)
    dec = _nc.decompress_neo(neo, ck)
    _m, entries = _nc.parse_entries(neo, ck[0])
    files = _nc.extract_files(dec, entries)
    t = tempfile.NamedTemporaryFile(delete=False, suffix='.sld'); t.write(files['AnSvEm0001.sld']); t.close()
    con = sqlite3.connect(t.name); con.row_factory = sqlite3.Row
    er = {r['RecordNo']: r['PartsCode'] for r in con.execute('SELECT RecordNo, PartsCode FROM ERParts')}
    out = [(er.get(r['ERPartsRecordNo'], ''), r['BlockCode'], r['PartsType'])
           for r in con.execute('SELECT ERPartsRecordNo, BlockCode, PartsType FROM DamageParts ORDER BY RecordNo')]
    con.close(); os.unlink(t.name)
    return out


def _annote(neo) -> dict:
    ck = _nc.find_real_cks(neo)
    dec = _nc.decompress_neo(neo, ck)
    _m, entries = _nc.parse_entries(neo, ck[0])
    txt = _nc.extract_files(dec, entries)['AnNote.ini'].decode('cp932', 'replace')
    return {m.group(1): m.group(2) for m in re.finditer(r'\[(\w+)\]\s*\r?\nFlag=(\d)', txt)}


def _car_code() -> str:
    """このテストが使う車両（VEH）の車種コード。テストの期待値と生成 NEO で同じ 12.DB を見るために必ずここから取る"""
    return (NeoBuilder().resolve_vehicle(VEH)['neo_car'] or {})['CarCode']


def _pick(car: str, want_kd: bool, n: int = 1) -> list:
    """12.DB の可能作業に K/D がある部品 / どちらも無い部品（修理のみ 'S' 等）を n 個選ぶ"""
    nb = NeoBuilder()
    ap = AddataParts(nb.engine, car); ap._load_12_blocks(car)
    out = []
    taken: set = set()   # 選んだ部位ブロック。同じブロックから 2 つ取ると連動加算・吸収で行数が変わりうるので避ける
    pairs = set(ap.pair_right) | set(ap.pair_right.values())  # 左右ペアの部品も避ける（左右分割で行が増える）
    for ref in sorted(ap.disp_by_ref):
        d = ap.disp_by_ref[ref]
        names = [x.strip() for x in ap.name20_by_ref.get(ref, []) if x.strip()]
        blk = ap.block_of(ref)
        if not names or not d.strip() or (('K' in d or 'D' in d) != want_kd) or ref in pairs or (blk and blk in taken):
            continue
        taken.add(blk)
        out.append({'code': f'{ref:04d}', 'name': names[0], 'method': ('取替' if want_kd else '脱着'),
                    'qty': 1, 'wage': 2400, 'index': 0.3})
        if len(out) >= n:
            return out
    raise AssertionError(f'{car}: 条件に合う部品が 12.DB に {n} 個無い（K/D あり = {want_kd}）')


def _pick_ambiguous(car: str):
    """12.DB の基本版(0) に無く、複数の W/S 版に載っている部品（DamageParts の部位が決まらない = BlockCode 空）"""
    nb = NeoBuilder()
    ap = AddataParts(nb.engine, car); ap._load_12_blocks(car)
    for ref in sorted(ap.ws_versions):
        vers = ap.ws_versions[ref]
        d = ap.disp_by_ref.get(ref, '')
        names = [x.strip() for x in ap.name20_by_ref.get(ref, []) if x.strip()]
        if names and '0' not in vers and len(vers) > 1 and ('K' in d or 'D' in d) and ap.block_of(ref):
            return {'code': f'{ref:04d}', 'name': names[0], 'method': '取替', 'qty': 1,
                    'price': 5000, 'wage': 8000, 'index': 1.0}
    raise AssertionError(f'{car}: 基本版に無く複数版に載る部品が 12.DB に無い')


def test_damage_parts_rules():
    """PartsType: K/D の無い品目・手入力行は 1、保留は 0（実機 cogni_CX2/CX4/M1。仕様書 §10-19）"""
    car = _car_code()
    kd, kd2 = _pick(car, True, 2)          # 可能作業に取替 K か脱着 D がある部品を 2 つ（1 つは保留行に使う）
    nokd = _pick(car, False, 1)[0]         # 修理のみ 'S' / オーバーホールのみ 'OH' の品目
    items = [kd, nokd,
             dict(kd2, method='取替', price=5000, wage=8000, index=1.0, reserve=True),
             {'name': 'ｼｮｰﾄﾊﾟｰﾂ', 'method': '取替', 'qty': 1, 'price': 1000, 'wage': 0, 'manual': True}]
    got = _dmg(build(items))               # 明細と同じ並び（DamageParts は行と 1 対 1）
    assert len(got) == len(items), f'DamageParts {len(got)} 行 / 明細 {len(items)} 行'
    assert got[0][0] == kd['code'] and got[0][2] == 0, f'K/D のある部品が {got[0]}（期待 PartsType 0）'
    assert got[1] == (nokd['code'], '', 1), f'K/D の無い品目が {got[1]}（期待 BlockCode "" / PartsType 1）'
    assert got[2][0] == kd2['code'] and got[2][2] == 0, f'保留行が {got[2]}（期待 PartsType 0）'
    assert got[3] == ('', '', 1), f'手入力行が {got[3]}（期待 BlockCode "" / PartsType 1）'


def test_damage_block_ambiguous_ws_version():
    """基本版に無く複数の W/S 版に載る部品は DamageParts.BlockCode が空。
    ERParts.BlockCode が残る行（標準が引ける行）でも DamageParts は空のまま（実機 cogni_CX9 / cogni_CXF。仕様書 §10-19）"""
    car = _car_code()
    amb = _pick_ambiguous(car)
    got = _dmg(build([amb]))
    assert got[0] == (amb['code'], '', 0), f'{got[0]}（期待 BlockCode "" / PartsType 0）'


def test_annote_flags():
    """[Reserve]/[Comment] Flag は書き終わった ERParts から決まる（リサイクル行のコメントは残らない）"""
    base = {'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'qty': 1, 'price': 30000, 'wage': 8000, 'index': 1.0}
    assert _annote(build([dict(base)])) == {'Reserve': '0', 'Comment': '0'}
    assert _annote(build([dict(base, comment='ｷｽﾞ')])) == {'Reserve': '0', 'Comment': '1'}
    assert _annote(build([dict(base, reserve=True)])) == {'Reserve': '1', 'Comment': '0'}
    rc = dict(base, comment='ﾘｻｲｸﾙﾋﾝ', recycle={'name': 'ﾘｻｲｸﾙFﾊﾞﾝﾊﾟ', 'price': 9000})
    assert _annote(build([rc])) == {'Reserve': '0', 'Comment': '0'}, 'リサイクル置換行のコメントは ERParts に残らないので 0'
def test_no_kd_uses_lowest_ws_version():
    """可能作業が W/S 版で割れる部品は、いちばん小さい版の行で判定する（実機 cogni_CXE: C88 8700 = 版 0 'OH ' / 版 1 'OHD'）"""
    nb = NeoBuilder()
    try:
        ap = AddataParts(nb.engine, 'C88')
        ap._load_12_blocks('C88')
    except Exception:  # noqa: BLE001
        print('skip test_no_kd_uses_lowest_ws_version（C88 が ADDATA に無い）')
        return
    vers = ap.disp_by_ver.get(8700) or {}
    if len(set(vers.values())) < 2:
        print('skip test_no_kd_uses_lowest_ws_version（C88 8700 の可能作業が版で割れていない）')
        return
    assert ap.no_kd(8700), f'C88 8700 は最小版 {min(vers)} の {vers[min(vers)]!r} で判定するので K/D なし（実機 cogni_CXE）'
def test_broken_chm_cache_is_rebuilt():
    """途中で終わった CHM キャッシュ（索引だけ / 本文が欠ける）でも落ちず、作り直して塗装指数が取れる。
    共有のキャッシュを壊さないよう、この検査の間だけ LOCALAPPDATA を専用の一時フォルダに向ける。
    hh.exe が動かない PC ではキャッシュを作れないので、その場合は検査しない"""
    import glob
    import re
    import shutil
    import tempfile
    from paint_index import PaintIndex
    car = _car_code()
    root = NeoBuilder().engine.root
    sandbox = tempfile.mkdtemp(prefix='chm_test_')
    old_local, PaintIndex._warned_chm = os.environ.get('LOCALAPPDATA'), True  # 検査中の警告は出さない
    os.environ['LOCALAPPDATA'] = sandbox
    try:
        rows0 = len(PaintIndex(root, car).chm_rows)
        if rows0 == 0:
            print('   skip test_broken_chm_cache_is_rebuilt（この PC では CHM を展開できない）')
            return
        cache_root = os.path.join(sandbox, 'claude_neo_pipeline', 'chm')
        dirs = [d for d in glob.glob(os.path.join(cache_root, '*')) if os.path.isdir(d)]
        assert len(dirs) == 1, f'この検査用のキャッシュが 1 つでない（{dirs}）'
        d = dirs[0]
        shutil.rmtree(os.path.join(d, 'html'), ignore_errors=True)  # hh.exe が途中で止まった状態（索引だけ）
        rows1 = len(PaintIndex(root, car).chm_rows)
        assert rows1 == rows0, f'索引だけのキャッシュから回復していない（{rows1} 行 / 期待 {rows0} 行）'
        # 索引も本文フォルダもあるが、参照している本文 1 枚だけ欠けた状態
        hhc = glob.glob(os.path.join(d, '*.hhc'))[0]
        h = open(hhc, 'rb').read().decode('cp932', 'replace')
        pages = [l for n, l in re.findall(r'<param name="Name" value="([^"]*)">\s*<param name="Local" value="([^"]*)">', h)
                 if n.startswith('補修塗装指数') and '溶剤' in n and '#' not in l]
        if pages:
            os.remove(os.path.join(d, pages[0].replace('/', os.sep)))
            # 展開が途中でも「その見積 1 回ぶんを指数なしで作る」ことが無いよう、同じ呼び出しの中で作り直す。
            # そのとき公開済みのキャッシュを直接消すと、既に使い始めた別プロセスの足元を崩すので、
            # rmtree ではなく rename で「どける」ことも併せて確かめる
            import paint_index as _pi
            removed, real_rmtree = [], _pi.shutil.rmtree
            def _spy(path, *a, **kw):
                removed.append(os.path.normcase(os.path.abspath(path)))
                return real_rmtree(path, *a, **kw)
            _pi.shutil.rmtree = _spy
            try:
                rows2 = len(PaintIndex(root, car).chm_rows)
            finally:
                _pi.shutil.rmtree = real_rmtree
            assert rows2 == rows0, f'本文が欠けたキャッシュから同じ呼び出しで回復していない（{rows2} 行 / 期待 {rows0} 行）'
            assert os.path.normcase(os.path.abspath(d)) not in removed, \
                '公開済みのキャッシュを直接消している（使用中の別プロセスを壊す）'
    finally:
        if old_local is None:
            os.environ.pop('LOCALAPPDATA', None)
        else:
            os.environ['LOCALAPPDATA'] = old_local
        PaintIndex._warned_chm = False
        shutil.rmtree(sandbox, ignore_errors=True)
def test_chm_waits_when_cache_is_in_use():
    """本文が読めず、かつキャッシュをどけられない（別プロセスが使用中）ときは、
    諦めて指数なしにせず、相手が書き終わるのを待って読み直す"""
    import glob
    import re
    import shutil
    import tempfile
    import threading
    from paint_index import PaintIndex
    car = _car_code()
    root = NeoBuilder().engine.root
    sandbox = tempfile.mkdtemp(prefix='chm_wait_')
    old_local, PaintIndex._warned_chm = os.environ.get('LOCALAPPDATA'), True
    os.environ['LOCALAPPDATA'] = sandbox
    real_retire = PaintIndex._retire
    try:
        rows0 = len(PaintIndex(root, car).chm_rows)
        if rows0 == 0:
            print('   skip test_chm_waits_when_cache_is_in_use（この PC では CHM を展開できない）')
            return
        d = [x for x in glob.glob(os.path.join(sandbox, 'claude_neo_pipeline', 'chm', '*')) if os.path.isdir(x)][0]
        hhc = glob.glob(os.path.join(d, '*.hhc'))[0]
        h = open(hhc, 'rb').read().decode('cp932', 'replace')
        pages = [l for n, l in re.findall(r'<param name="Name" value="([^"]*)">\s*<param name="Local" value="([^"]*)">', h)
                 if n.startswith('補修塗装指数') and '溶剤' in n and '#' not in l]
        if not pages:
            print('   skip test_chm_waits_when_cache_is_in_use（この車種の CHM に対象ページが無い）')
            return
        body = os.path.join(d, pages[0].replace('/', os.sep))
        keep = open(body, 'rb').read()
        os.remove(body)
        PaintIndex._retire = staticmethod(lambda _d: False)  # 別プロセスが使用中でどけられない状況
        threading.Timer(1.0, lambda: open(body, 'wb').write(keep)).start()  # 相手が書き終わる
        rows1 = len(PaintIndex(root, car).chm_rows)
        assert rows1 == rows0, f'待たずに指数なしで作っている（{rows1} 行 / 期待 {rows0} 行）'
    finally:
        PaintIndex._retire = real_retire
        if old_local is None:
            os.environ.pop('LOCALAPPDATA', None)
        else:
            os.environ['LOCALAPPDATA'] = old_local
        PaintIndex._warned_chm = False
        shutil.rmtree(sandbox, ignore_errors=True)


def test_sweep_tmp_keeps_running_extractions():
    """異常終了で残った古い一時フォルダだけを捨て、いま展開中のものは残す"""
    import shutil
    import tempfile
    import time
    from paint_index import PaintIndex
    base = tempfile.mkdtemp(prefix='sweep_')
    try:
        cache = os.path.join(base, 'DUMMYLTB')
        old = cache + '.111.old.tmp'
        now = cache + '.222.now.tmp'
        os.makedirs(old)
        os.makedirs(now)
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        PaintIndex._sweep_tmp(cache)
        assert not os.path.exists(old), '古い一時フォルダが残っている'
        assert os.path.isdir(now), '展開中の一時フォルダを消している'
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_owner_name_only_fills_owner_field():
    """所有者・使用者欄に入るのは customer.owner_name / user_name だけ。
    customer.owner は車検証の所有者のメモで NEO には書かない（実機 cogni_R1/R2 は所有者欄が顧客名）"""
    def _cust(c):
        est = dict(BASE)
        est['items'] = [{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'qty': 1, 'price': 30000, 'wage': 8000, 'index': 1.0}]
        est['customer'] = c
        neo = NeoBuilder().build(est, VEH, labor_rate=8000, est_date='20260909', insurance={})[0]
        ck = _nc.find_real_cks(neo)
        _m, entries = _nc.parse_entries(neo, ck[0])
        files = _nc.extract_files(_nc.decompress_neo(neo, ck), entries)
        f = tempfile.NamedTemporaryFile(delete=False, suffix='.sld')
        f.write(files['AnSvIf0001.sld'])
        f.close()
        con = sqlite3.connect(f.name)
        con.row_factory = sqlite3.Row
        r = dict(con.execute('SELECT OwnerName, UserName FROM Customer').fetchone())
        con.close()
        os.unlink(f.name)
        return r
    r = _cust({'name': '甲野 太郎', 'owner_name': 'ﾄﾖﾀ西東京ｶﾛｰﾗ㈱', 'user_name': '乙野 次郎'})
    assert r['OwnerName'] == 'ﾄﾖﾀ西東京ｶﾛｰﾗ㈱' and r['UserName'] == '乙野 次郎', f'owner_name/user_name が効いていない（{r}）'
    r2 = _cust({'name': '甲野 太郎', 'owner': '株式会社ホンダファイナンス'})
    assert r2['OwnerName'] == '甲野 太郎', f'車検証メモの customer.owner を所有者欄に書いてしまっている（{r2}）'
    assert r2['UserName'] == '同上', f'使用者欄の既定が 同上 でない（{r2}）'


def test_low_match_warning_is_what_make_neo_greps():
    """make_neo.py は run_case の警告文を文字列で拾って納品を止めている。
    どちらかの文言だけ変えると関門が黙って外れるので、合言葉が一致していることを縛る"""
    import contextlib
    import io as _io
    from run_case import _warn_too_many_manual
    mk = os.path.join(os.path.dirname(os.path.dirname(HERE)), '.claude', 'skills', 'pdf-to-neo', 'scripts', 'make_neo.py')
    if not os.path.exists(mk):
        print('   skip test_low_match_warning_is_what_make_neo_greps（make_neo.py が無い）')
        return
    src = _io.open(mk, encoding='utf-8').read()
    m = re.search(r"if '([^']+)' in l and '★' in l", src)
    assert m, 'make_neo が照合率の警告を拾う条件を見つけられない'
    key = m.group(1)
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        _warn_too_many_manual({'vehicle': VEH}, {'rows': [{'PartsName': f'x{i}'} for i in range(20)]}, {'matched': 0})
    got = buf.getvalue()
    assert '★' in got and key in got, f'make_neo が探す文言「{key}」が run_case の警告に無い（{got.strip()[:80]}）'


def test_tolerance_needs_three_keys():
    """差を許すのは neo_total・tolerance・理由が 3 つ揃ったときだけ。どれか欠けると不合格。
    配布先でも動くよう、案件フォルダを見ずに ADDATA だけで完結する見積を組み立てて試す"""
    import io as _io
    import json as _json
    import subprocess
    import tempfile
    base = dict(BASE)
    base['items'] = [{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'qty': 1,
                      'price': 30000, 'wage': 8000, 'index': 1.0}]
    base['totals'] = {}
    _neo, rep = NeoBuilder().build(base, VEH, labor_rate=8000, est_date='20260909', insurance={})
    tot = rep.get('totals') or {}
    gen = int(tot.get('total') or 0)
    if not gen:
        print('   skip test_tolerance_needs_three_keys（合計を取れない）')
        return
    d = tempfile.mkdtemp(prefix='tol3_test_')
    full = {'neo_total': gen, 'tolerance': 1, 'tolerance_reason': '工場が丸めるため（検査用）', 'total': gen - 1}
    def _exit(drop):
        e = _json.loads(_json.dumps(base))
        e['totals'] = {k: v for k, v in full.items() if k != drop}
        q = os.path.join(d, 'e.json')
        _io.open(q, 'w', encoding='utf-8').write(_json.dumps(e, ensure_ascii=False))
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), 'run_case.py'), q,
                            os.path.join(d, 'x.neo')], capture_output=True, text=True,
                           encoding='utf-8', errors='replace',
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'), timeout=900)
        return r.returncode
    assert _exit(None) == 0, '3 点そろっているのに不合格になっている'
    for k in ('neo_total', 'tolerance', 'tolerance_reason'):
        assert _exit(k) != 0, f'totals.{k} が無いのに合格している'


def test_weak_match_is_reported():
    """名称の「近似」で決めた行のうち標準価格も合わない行だけを要確認に挙げる。
    どちらか片方だけなら普通に起きるので挙げない（実案件 10 件・約 500 行で 4 行だけ出る絞り込み）"""
    import contextlib
    import io as _io
    from run_case import _report_weak_matches
    def row(why, price, std, qty=1):
        return {'PartsName': '部品', '_ref_why': why, 'PartsPriceOutTax': price,
                'PartsPriceStandardOutTax': std, 'PartsCount': qty}
    def out(rows):
        b = _io.StringIO()
        with contextlib.redirect_stdout(b):
            got = _report_weak_matches({'items': []}, {'rows': rows})
        return got, b.getvalue()
    got, txt = out([row('名称近似(0.80) ｸﾘﾂﾌﾟ', 90, 110)])
    got2, _ = out([row('ブロック内名称照合(0.82)', 90, 110)])
    assert len(got2) == 1, '下書きの「ブロック内名称照合」を見落としている'
    assert len(got) == 1 and '要確認' in txt, '近似かつ価格不一致を挙げていない'
    got, _ = out([row('名称近似(0.80) ｸﾘﾂﾌﾟ', 110, 110)])
    assert got == [], '近似でも価格が合う行を挙げている'
    got, _ = out([row('名称一致(11.DB) ﾄﾞｱﾊﾟﾈﾙ', 90, 110)])
    assert got == [], '名称一致の行を挙げている（価格差は正当なことがある）'
    got, _ = out([row('名称近似(0.80) ｸﾘﾂﾌﾟ', 660, 110, 6)])
    assert got == [], '数量倍で一致する行を挙げている'
    got, _ = out([row('名称近似(0.80) ｸﾘﾂﾌﾟ', 90, -1)])
    assert got == [], '標準価格の無い行を挙げている'


def test_front_rear_side_mismatch_is_flagged():
    """見積の名称と照合先で 前後・左右 が食い違ったら知らせる。
    2026-09-10 のヴァンガードで、リヤの部品がフロントの ref に付く取り違えが 7 行あった。
    価格の一致率と違い、純正価格の改定や色別価格では誤検知しない"""
    from run_case import _check_side_front_rear, _side_tokens
    assert _side_tokens('左Rrﾄﾞｱﾊﾟﾈﾙ') == ('L', 'R')
    assert _side_tokens('LFﾄﾞｱﾊﾟﾈﾙ') == ('L', 'F')
    assert _side_tokens('右Frﾊﾞﾝﾊﾟﾋﾟｰｽ') == ('R', 'F')
    assert _side_tokens('ｼｮｰﾄﾊﾟｰﾂ') == ('', '')
    # 全角・カタカナの揺れも同じに読む（工場書式によって表記が変わる）
    assert _side_tokens('左リヤドアパネル') == ('L', 'R'), '全角カタカナのリヤを読めていない'
    assert _side_tokens('左Ｒｒドアパネル') == ('L', 'R'), '全角の Rr を読めていない'
    assert _side_tokens('フロントバンパ') == ('', 'F'), '全角カタカナのフロントを読めていない'
    # ADDATA は「英字 1 文字＋空白」で左右だけを表す。空白の有無が『右』と『リヤ』を分ける
    assert _side_tokens('L ｸﾘﾂﾌﾟ') == ('L', ''), 'ADDATA の左だけの名称を読めていない'
    assert _side_tokens('R ｸﾘﾂﾌﾟ') == ('R', ''), 'ADDATA の「R ＋空白」を右でなくリヤと誤読している'
    assert _side_tokens('Rﾊﾞﾝﾊﾟﾋﾟ-ｽ') == ('', 'R'), '空白なしの R（リヤ）を読めていない'
    assert _side_tokens('RFﾄﾞｱﾊﾟﾈﾙ') == ('R', 'F'), 'RF を読めていない'
    # LH / RH / R/H / L. も左右だけの書き方（下書きの _side_of と同じ規則）
    assert _side_tokens('RHﾄﾞｱﾊﾟﾈﾙ') == ('R', ''), 'RH を右でなくリヤと誤読している'
    assert _side_tokens('LHﾄﾞｱﾊﾟﾈﾙ') == ('L', ''), 'LH を左と読めていない'
    assert _side_tokens('R/Hﾄﾞｱ') == ('R', ''), 'R/H を読めていない'
    assert _side_tokens('ﾘﾔﾊﾞﾝﾊﾟ') == ('', 'R'), 'リヤを読めていない'
    # 材料名・型番（FRP など）を前後と読まない。英字が続く語は対象外
    assert _side_tokens('FRPﾘﾔﾊﾞﾝﾊﾟ') == ('', ''), 'FRP を前と誤読している'
    assert _side_tokens('RRCﾊﾞﾝﾊﾟ') == ('', ''), 'RRC を右後と誤読している'
    assert _side_tokens('RFIDﾀｸﾞ') == ('', ''), 'RFID を右前と誤読している'
    assert _side_tokens('Frﾊﾞﾝﾊﾟ') == ('', 'F'), 'Fr を読めていない'
    def chk(est_name, std_name, code='2300'):
        est = {'items': [{'name': est_name}]}
        rep = {'rows': [{'PartsCode': code, 'PartsNameStandard': std_name}]}
        return _check_side_front_rear(est, rep)
    assert chk('左Rrﾄﾞｱﾊﾟﾈﾙ', 'LFﾄﾞｱﾊﾟﾈﾙ'), '前後の取り違えを見つけていない'
    assert chk('左Rrﾄﾞｱﾊﾟﾈﾙ', 'RRﾄﾞｱﾊﾟﾈﾙ'), '左右の取り違えを見つけていない'
    assert not chk('左Rrﾄﾞｱﾊﾟﾈﾙ', 'LRﾄﾞｱﾊﾟﾈﾙ'), '正しい照合で誤検知している'
    assert not chk('ｼｮｰﾄﾊﾟｰﾂ', 'LFﾄﾞｱﾊﾟﾈﾙ'), '前後・左右が読めない名称で誤検知している'
    assert chk('左 ｸﾘｯﾌﾟ', 'R ｸﾘﾂﾌﾟ'), '左右だけの名称の取り違えを見つけていない'
    assert not chk('左 ｸﾘｯﾌﾟ', 'L ｸﾘﾂﾌﾟ'), '左右だけの名称で誤検知している'
    assert chk('左Rrﾄﾞｱﾊﾟﾈﾙ', 'RHﾄﾞｱﾊﾟﾈﾙ'), 'RH 表記の左右取り違えを見つけていない'
    assert not chk('左Rrﾄﾞｱﾊﾟﾈﾙ', 'LFﾄﾞｱﾊﾟﾈﾙ', code=''), '手入力行（未照合）で誤検知している'
    assert chk('左リヤドアパネル', 'LFﾄﾞｱﾊﾟﾈﾙ'), '全角表記の取り違えを見つけていない'
    # リサイクル置換行は生成器が末尾へ動かすが、元の RecordNo（_orig_recno）で並べ直せば対応が取れる
    est2 = {'items': [{'name': '左Rrﾄﾞｱﾊﾟﾈﾙ'}, {'name': 'ﾘﾍﾞｯﾄ'}]}
    rep2 = {'rows': [{'PartsCode': '0340', 'PartsNameStandard': 'ﾘﾍﾞﾂﾄ', 'RecordNo': 3},
                     {'PartsCode': '2300', 'PartsNameStandard': 'LFﾄﾞｱﾊﾟﾈﾙ', 'RecordNo': 9,
                      '_recycle': {'name': 'ﾘｻｲｸﾙ品'}, '_orig_recno': 1}]}
    got2 = _check_side_front_rear(est2, rep2)
    assert len(got2) == 1, f'リサイクルで末尾へ動いた行の取り違えを見つけていない（{got2}）'
    # 行数が食い違うとき（リサイクル置換などで行が増減）は判定しない
    assert _check_side_front_rear({'items': [{'name': '左Rrﾄﾞｱﾊﾟﾈﾙ'}]},
                                  {'rows': [{'PartsCode': '2300', 'PartsNameStandard': 'LFﾄﾞｱﾊﾟﾈﾙ'},
                                            {'PartsCode': '2700', 'PartsNameStandard': 'LRﾄﾞｱﾊﾟﾈﾙ'}]}) == [], \
        '行数が対応しないのに判定している'
    # ADDATA の 'Rﾊﾞﾝﾊﾟ'（リヤ）と見積の 'Rrﾊﾞﾝﾊﾟ' は同じ。ここを誤検知しないこと
    assert not chk('Rrﾊﾞﾝﾊﾟｶﾊﾞｰ', 'Rﾊﾞﾝﾊﾟｶﾊﾞ-'), 'リヤ同士で誤検知している'


def test_standard_price_mismatch_is_flagged():
    """見積の部品金額が ADDATA 標準価格と食い違う行が多いと知らせる。
    2026-09-10 のヴァンガードで、リヤドアパネル 75,500 円がフロントの ref に付いていた取り違えを
    この検査が見つけた。安い小物は工場と ADDATA で価格が普通に食い違うので 1,000 円以上で判定する"""
    import contextlib
    import io as _io
    from run_case import _report_standard_price
    def row(price, std, qty=1):
        return {'PartsName': f'部品{price}', 'PartsPriceOutTax': price,
                'PartsPriceStandardOutTax': std, 'PartsCount': qty}
    def out(rows):
        b = _io.StringIO()
        with contextlib.redirect_stdout(b):
            _report_standard_price({'rows': rows})
        return b.getvalue()
    ok = [row(5000, 5000), row(4000, 4000), row(3000, 3000), row(2000, 2000)]
    assert '★' not in out(ok), '全部一致しているのに知らせている'
    assert '4/4' in out(ok), f'件数が出ていない（{out(ok)!r}）'
    ng = [row(75500, 70700), row(4050, 5300), row(3710, 2440), row(2000, 2000)]
    got = out(ng)
    assert '★' in got, f'取り違えを見つけていない（{got!r}）'
    assert '75,500' in got, f'金額の大きい行を挙げていない（{got!r}）'
    cheap = [row(5000, 5000), row(4000, 4000), row(3000, 3000), row(90, 110), row(660, 1320, 6)]
    assert '★' not in out(cheap), '1,000 円未満の小物だけの差で騒いでいる'
    assert out([]) == '', '標準価格の無い見積で何か出している'


def test_too_many_manual_rows_is_flagged():
    """ADDATA に載っている車なのに明細をほとんど手入力にしていたら run_case が知らせる。
    2026-09-09 アクアで全 47 行を手入力にして部品コードの無い NEO を作ってしまった失敗の再発防止"""
    import contextlib
    import io as _io
    from run_case import _warn_too_many_manual
    rows = [{'PartsName': f'x{i}'} for i in range(20)]
    def _run(est, matched):
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            _warn_too_many_manual(est, {'rows': rows}, {'matched': matched})
        return buf.getvalue()
    assert '★' in _run({'vehicle': VEH}, 0), '全部手入力なのに知らせていない'
    assert '★' in _run({'vehicle': VEH}, 9), '照合率 半分未満なのに知らせていない'
    assert _run({'vehicle': VEH}, 10).strip() == '', '照合率 半分で誤検知している'
    assert _run({'vehicle': dict(VEH, generic=True)}, 0).strip() == '', '汎用車種（二輪・輸入車）で誤検知している'
    assert _warn_too_many_manual({'vehicle': VEH}, {'rows': rows[:4]}, {'matched': 0}) is None, '数行の見積で騒がない'


def test_tax_included_estimate_is_flagged():
    """金額が税込で印字された見積書をそのまま写すと、run_case が「税込かもしれない」と教える。
    ディーラー・二輪の見積で 2 件続けて起きた取り違え（2026-09-09）"""
    import contextlib
    import io as _io
    from run_case import _warn_tax_included
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        _warn_tax_included({'taxable': 428868, 'tax': 38988})   # 税込のまま写した
    assert '税込' in buf.getvalue(), '税込の取り違えを見つけられていない'
    buf2 = _io.StringIO()
    with contextlib.redirect_stdout(buf2):
        _warn_tax_included({'taxable': 389880, 'tax': 38988})   # 正しい写し方
    assert buf2.getvalue().strip() == '', f'正しい見積で誤検知している（{buf2.getvalue()!r}）'
    buf3 = _io.StringIO()
    with contextlib.redirect_stdout(buf3):
        _warn_tax_included({'taxable': 432000, 'tax': 32000, 'tax_rate': 8})  # 8% の税込書式
    assert '税込' in buf3.getvalue(), '税率 10% 以外の税込書式を見落としている'
    buf4 = _io.StringIO()
    with contextlib.redirect_stdout(buf4):
        _warn_tax_included({'taxable': 400000, 'tax': 32000, 'tax_rate': 8})  # 8% の正しい写し方
    assert buf4.getvalue().strip() == '', f'税率 8% で誤検知している（{buf4.getvalue()!r}）'


def test_chm_cache_is_thread_safe():
    """同じプロセスの 2 スレッドが同時に同じ CHM を展開しても、一時フォルダを共用しない。
    共用すると片方が相手の展開途中を消し、その見積だけ塗装指数 0 行になる。
    hh.exe を偽物に差し替え、2 スレッドが同時に展開している状態を作って検査する"""
    import shutil
    import tempfile
    import threading
    import paint_index as _pi
    from paint_index import PaintIndex
    sandbox = tempfile.mkdtemp(prefix='chm_thread_')
    old_local = os.environ.get('LOCALAPPDATA')
    os.environ['LOCALAPPDATA'] = sandbox
    old_win = os.environ.get('WINDIR')
    os.environ['WINDIR'] = sandbox  # hh.exe は実在するものだけ起動する（無ければ 7z へ）ので、偽物を置く（2026-09-14）
    open(os.path.join(sandbox, 'hh.exe'), 'wb').write(b'x')
    real_run = _pi.subprocess.run
    seen, lock, gate, out = [], threading.Lock(), threading.Barrier(2, timeout=120), []
    def fake_run(args, **kw):  # hh.exe の代わりに最小限の展開結果を書く
        tmp = args[2]
        with lock:
            seen.append(os.path.normcase(os.path.abspath(tmp)))
        gate.wait()  # 2 スレッドが同時に展開中の状態にする
        os.makedirs(os.path.join(tmp, 'html'), exist_ok=True)
        open(os.path.join(tmp, 'X.hhc'), 'wb').write(b'x')
        open(os.path.join(tmp, 'html', 'a.htm'), 'wb').write(b'x')
        return None
    _pi.subprocess.run = fake_run
    inst = PaintIndex.__new__(PaintIndex)  # _decompile だけ使うので初期化は要らない
    chm = os.path.join(sandbox, 'DUMMYLTB.CHM')
    open(chm, 'wb').write(b'dummy chm')  # 実在するファイルにする（読めない CHM は展開不可扱い。2026-09-14）
    try:
        def work():
            try:
                out.append(inst._decompile(chm))
            except Exception as e:  # noqa: BLE001
                out.append('例外 %s' % e)
        ts = [threading.Thread(target=work) for _ in range(2)]
        for th in ts:
            th.start()
        for th in ts:
            th.join(180)
        assert len(set(seen)) == 2, f'2 スレッドが同じ一時フォルダに展開している（{seen}）'
        assert all(isinstance(o, str) and o for o in out) and len(out) == 2, f'展開に失敗している（{out}）'
        assert len(set(out)) == 1, f'公開先が食い違う（{out}）'
    finally:
        if old_win is None:
            os.environ.pop('WINDIR', None)
        else:
            os.environ['WINDIR'] = old_win
        _pi.subprocess.run = real_run
        if old_local is None:
            os.environ.pop('LOCALAPPDATA', None)
        else:
            os.environ['LOCALAPPDATA'] = old_local
        shutil.rmtree(sandbox, ignore_errors=True)


def test_retire_keeps_a_cache_in_use():
    """使用中（別プロセスが読んでいる）キャッシュは どけられない = 消さずに残す。
    Windows では中のファイルが開かれていると os.rename が失敗するので、それを合図に使う"""
    import shutil
    import tempfile
    from paint_index import PaintIndex
    d = tempfile.mkdtemp(prefix='retire_test_')
    try:
        os.makedirs(os.path.join(d, 'html'), exist_ok=True)
        f = open(os.path.join(d, 'html', 'a.htm'), 'wb')
        f.write(b'x')
        f.flush()
        try:
            assert PaintIndex._retire(d) is False, '使用中のキャッシュをどけられてしまった'
            assert os.path.isdir(d), '使用中のキャッシュを消してしまった'
        finally:
            f.close()
        assert PaintIndex._retire(d) is True, '解放後にどけられていない'
        assert not os.path.exists(d), 'どけたキャッシュが残っている'
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chm_body_htm_extension_is_accepted():
    """CHM の本文が .htm の車種（三菱 C88 の C9500LTB）でも「展開できた」と判定する。
    .html だけを本文と見なすと、この種の CHM を毎回壊れ扱いにして展開し直し、★ 警告も誤発報する"""
    import shutil
    import subprocess
    import tempfile
    from paint_index import PaintIndex
    nb = NeoBuilder()
    car = 'C88'
    pi = PaintIndex.__new__(PaintIndex)
    pi.root, pi.car = nb.engine.root, car
    pi.car_dir = os.path.join(nb.engine.root, car[0], car)
    if not os.path.isdir(pi.car_dir) or not pi._chm_path():
        print('   skip test_chm_body_htm_extension_is_accepted（この ADDATA に C88 の CHM が無い）')
        return
    hh = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'hh.exe')
    if not os.path.isfile(hh):
        print('   skip test_chm_body_htm_extension_is_accepted（hh.exe が無い）')
        return
    tmp = tempfile.mkdtemp(prefix='chm_htm_')
    try:  # 判定関数だけを見たいので、キャッシュ機構を通さず素で展開する
        subprocess.run([hh, '-decompile', tmp, pi._chm_path()], timeout=60)
        for _ in range(40):
            if glob.glob(os.path.join(tmp, '*.hhc')) and os.path.isdir(os.path.join(tmp, 'html')):
                break
            time.sleep(0.25)
        htm = glob.glob(os.path.join(tmp, 'html', '*.htm'))
        if not htm:
            print('   skip test_chm_body_htm_extension_is_accepted（この CHM の本文が .htm ではない）')
            return
        assert PaintIndex._cache_ok(tmp), f'.htm の本文を「展開できていない」と判定している（{sorted(os.listdir(tmp))}）'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_candidate_pin_selects_that_candidate():
    """hints.candidate は指定した候補（2WD/4WD の別を含む）に固定する"""
    from addata_vehicle_resolver import AddataVehicleResolver
    r = AddataVehicleResolver()
    rep = r.resolve(**VEH)
    cands = rep.get('candidates') or []
    assert cands, '候補が取れない'
    keys = ('car_code', 'year_code', 'body_code', 'grade_code', 'fva_code')
    for c in cands[:3]:
        pin = {k: c.get(k) for k in keys}
        pin['four_wd'] = bool(c.get('four_wd'))
        got = r.resolve(hints={'candidate': pin}, **VEH)['best']
        assert all(str(got.get(k) or '') == str(pin[k] or '') for k in keys), '指定した候補で固定できていない'
        assert bool(got.get('four_wd')) == pin['four_wd'], '2WD/4WD が指定と違う'


def test_candidate_pin_without_match_is_an_error():
    """一致する候補が無い candidate は、黙って別の車で作らずに失敗する"""
    from addata_vehicle_resolver import AddataVehicleResolver
    try:
        AddataVehicleResolver().resolve(hints={'candidate': {'grade_code': '#'}}, **VEH)
    except ValueError:
        return
    raise AssertionError('一致しない candidate が素通りしている')


def test_candidate_limit_bad_value_falls_back():
    """candidate_limit は 0 だけが無制限。負数・文字は既定の 12 件に戻す"""
    from addata_vehicle_resolver import AddataVehicleResolver
    r = AddataVehicleResolver()
    base = len(r.resolve(**VEH)['candidates'])
    for bad in (-1, 'たくさん', None):
        n = len(r.resolve(hints={'candidate_limit': bad}, **VEH)['candidates'])
        assert n == base, f'candidate_limit={bad!r} で件数が変わった（{n} ≠ {base}）'
    assert len(r.resolve(hints={'candidate_limit': 0}, **VEH)['candidates']) ==         r.resolve(**VEH)['candidates_total'], '0 を渡しても全候補にならない'


def test_removal_row_keeps_left_right():
    """脱着・板金など品番の出ない行でも、左右のある部品は名称に左右が入る
    （12.DB の名称は左右を持たないので 11.DB の名称欄から補う。実機 cogni_T1 1410 / H24 2599）"""
    est = dict(BASE, items=[{'code': '1410', 'name': 'LFﾊﾞﾙｸﾍｯﾄﾞｻｲﾄﾞｽﾃｰ', 'method': '脱着', 'qty': 1, 'wage': 2400, 'index': 0.3},
                            {'code': '2599', 'name': '左ﾄﾞｱﾊﾞｲｻﾞ', 'method': '脱着', 'qty': 1, 'wage': 2210, 'index': 0.3}])
    _neo, rep = NeoBuilder().build(est, est['vehicle'], labor_rate=8000, est_date='20260910', insurance={})
    for r in rep['rows']:
        std = str(r.get('PartsNameStandard') or '')
        disp = str(r.get('PartsName') or '')
        assert std[:1] == 'L', f'標準名称に左右が入っていない: {std!r}'
        assert disp[:1] == '左', f'表示名称に左右が入っていない: {disp!r}'


def test_work_only_item_keeps_12db_name():
    """取替できない作業項目（12.DB の「…(修理)」など）は 12.DB の名称のまま
    （11.DB の部品名で上書きしない。実機 cogni_CX1 0005 / H24 7600）"""
    est = dict(BASE, items=[{'code': '0005', 'name': 'Fﾗｲｾﾝｽﾌﾟﾚｰﾄ(修理)', 'method': '修理', 'qty': 1, 'wage': 2400, 'index': 0.3}])
    _neo, rep = NeoBuilder().build(est, est['vehicle'], labor_rate=8000, est_date='20260910', insurance={})
    std = str(rep['rows'][0].get('PartsNameStandard') or '')
    assert '(' in std, f'12.DB の作業名称（括弧書き）が消えている: {std!r}'
    assert std[:1] == ' ', f'左右のない品目に左右が付いている: {std!r}'


def test_side_letter_reads_estimate_names():
    """左右の読み取りは run_case の前後左右チェックと同じ結論になる（規則が二重管理でずれない）"""
    import importlib.util
    from estimate_to_neo import side_letter
    spec = importlib.util.spec_from_file_location('_rc', os.path.join(os.path.dirname(HERE), 'run_case.py'))
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)
    for nm in ('左Fﾄﾞｱ', '右Rrﾊﾟﾈﾙ', 'LFﾊﾞﾙｸﾍｯﾄﾞ', 'RRﾄﾞｱ', 'L ｸﾘﾂﾌﾟ', 'RHﾄﾞｱ', 'R/Hﾐﾗｰ',
               'RRCｾﾝｻｰ', 'RFIDﾕﾆｯﾄ', 'Fﾊﾞﾝﾊﾟ', 'ﾗｼﾞｴｰﾀ', 'LEDﾍｯﾄﾞﾗｲﾄ', 'FRPﾘﾔﾊﾞﾝﾊﾟ'):
        assert side_letter(nm) == rc._side_tokens(nm)[0], f'{nm}: 左右の読み方が run_case と違う'


def _audit_module():
    """実機 NEO との総当たり（audit_cogni_files）。配布物には入らないので、無い環境では None を返す。
    リポジトリにあるのに import できないときは、隠さず例外にする"""
    if not os.path.exists(os.path.join(HERE, 'audit_cogni_files.py')):
        return None
    import audit_cogni_files as a
    return a


def test_audit_gate_catches_name_and_flag_regressions():
    """実機との総当たりで許す差は「工場 NEO 由来のファイル」と「品番に * が付いた行」だけ。
    それ以外で名称・品番・OrderFlag が食い違ったら退行として落とす"""
    a = _audit_module()
    if a is None:
        print('     （総当たりの許可条件は配布物に入らないので検査しない）')
        return
    ng = [('左右が消えた', a.known_side_effect(1, 'PartsNameStandard', ' Fﾄﾞｱﾊﾟﾈﾙ', 'LFﾄﾞｱﾊﾟﾈﾙ', ws=True)),
          ('別部品の名称', a.known_side_effect(0, 'PartsName', '  Frﾊﾞﾝﾊﾟ', '  Rrﾊﾞﾝﾊﾟ', ws=True)),
          ('品番が消えた', a.known_side_effect(0, 'PartsNo', '', '12345-67890', ws=True)),
          ('品番が違う', a.known_side_effect(0, 'PartsNo', '12345-67890', '99999-00000', ws=True)),
          ('非 W/S で標準品番', a.known_side_effect(1, 'PartsNoStandard', '12345-67890', '', ws=False)),
          ('非 W/S で OrderFlag', a.known_side_effect(0, 'OrderFlag', '', '0', ws=False)),
          ('* が無いのに 1', a.known_side_effect(0, 'OrderFlag', '', '1', cog_row={'PartsNo': '12345-67890'})),
          ('(修理) が消えた', a.known_side_effect(1, 'PartsNameStandard', ' Fﾗｲｾﾝｽﾌﾟﾚｰﾄ', ' Fﾗｲｾﾝｽﾌﾟﾚｰﾄ(修理)', ws=True)),
          ('(片側) が消えた', a.known_side_effect(1, 'PartsNameStandard', ' Fｻｽﾍﾟﾝｼﾖﾝ', ' Fｻｽﾍﾟﾝｼﾖﾝ(片側)', ws=True))]
    bad = [n for n, v in ng if v]
    assert not bad, f'退行を見逃す許可条件がある: {bad}'
    ok = [('固定長の詰め', a.known_side_effect(0, 'PartsName', '  Frﾊﾞﾝﾊﾟ', '  Frﾊﾞﾝﾊﾟ   ', ws=True)),
          ('W/S の標準品番', a.known_side_effect(1, 'PartsNoStandard', '12345-67890', '', ws=True)),
          ('W/S の OrderFlag', a.known_side_effect(0, 'OrderFlag', '', '0', ws=True)),
          ('* と対の 1', a.known_side_effect(0, 'OrderFlag', '', '1', cog_row={'PartsNo': '12345-67890  *'})),
          ('塗装済みの語尾', a.known_side_effect(2, 'PartsNameStandard', ' Fﾊﾞﾝﾊﾟﾌｴｲｽ(ﾄｿｳｽﾞﾐ)', ' Fﾊﾞﾝﾊﾟﾌｴｲｽ', ws=True)),
          ('* 付きの品番', a.known_side_effect(0, 'PartsNo', '12345-67890', '12345-67890  *',
                                            cog_row={'PartsNo': '12345-67890  *'}))]
    miss = [n for n, v in ok if not v]
    assert not miss, f'説明の付く実機差を退行として落としている: {miss}'


def test_ws_origin_tells_factory_neo_from_new_estimate():
    """工場 NEO 由来（固定長の詰めが残る）と、コグニで新規作成した見積を見分ける"""
    a = _audit_module()
    if a is None:
        print('     （総当たりの許可条件は配布物に入らないので検査しない）')
        return
    assert a.ws_origin([{'PartsNameStandard': 'LFﾄﾞｱﾊﾟﾈﾙ          ', 'PartsNo': '', 'PartsNoStandard': ''}])
    assert not a.ws_origin([{'PartsNameStandard': 'LFﾄﾞｱﾊﾟﾈﾙ', 'PartsNo': '12345-67890', 'PartsNoStandard': ''}])
    assert not a.ws_origin([])


def test_paint_panel_is_picked_by_body():
    """20.DB は同じパネルをボディごとに別行（面積違い）で持つ。車のボディに合う行を選ぶ。
    W90 ハイエース: L ｽﾗｲﾄﾞﾄﾞｱﾊﾟﾈﾙ は 178（共通/ボディ 10）と 196（ボディ 20）"""
    from paint_index import PaintIndex
    nb = NeoBuilder()
    rows = [p for p in PaintIndex(nb.engine.root, 'W90').panels if p['code'] == '2700']
    if len({p['area'] for p in rows}) < 2:
        print('     （この ADDATA ではボディ違いの行が無いので検査しない）')
        return
    a20 = PaintIndex(nb.engine.root, 'W90', body='20').panel_exact('2700')
    a10 = PaintIndex(nb.engine.root, 'W90', body='10').panel_exact('2700')
    assert a20 and a10, 'パネルを引けない'
    assert a20['area'] != a10['area'], f'ボディで面積が変わらない（{a10["area"]} / {a20["area"]}）'
    assert a20['body'] == 20, f'ボディ 20 の行を選べていない（{a20}）'


def test_paint_panel_falls_back_to_common_row():
    """そのボディ専用の行が無ければ全ボディ共通（body 0）の行、それも無ければ先頭を使う"""
    from paint_index import PaintIndex
    nb = NeoBuilder()
    pi = PaintIndex(nb.engine.root, 'W90', body='99')   # 存在しないボディ
    p = pi.panel_exact('2700')
    assert p, '共通行にも落ちていない'
    assert not p['body'] or p['body'] == 0, f'他ボディ専用の行を選んでいる（{p}）'


def test_paint_panel_follows_the_body_branch_code():
    """同じパネルがボディごとに別コードになっている車では、明細のコードから枝番の行へ進む
    （W90 ハイエース: 明細 4800 → ボディ 20 の塗装パネルは 4801・面積 236）"""
    from paint_index import PaintIndex
    nb = NeoBuilder()
    pi20 = PaintIndex(nb.engine.root, 'W90', body='20')
    pi10 = PaintIndex(nb.engine.root, 'W90', body='10')
    a, b = pi20.panel('4800'), pi10.panel('4800')
    if not a or not b or a['code'] == b['code']:
        print('     （この ADDATA ではボディで枝番が分かれていないので検査しない）')
        return
    assert a['code'] == '4801' and a['body'] == 20, f'ボディ 20 で枝番に進めていない（{a}）'
    assert b['code'] == '4800', f'ボディ 10 で余計に枝番へ進んでいる（{b}）'
    # panel_exact は厳密一致のまま（枝番へ進まない）
    assert pi20.panel_exact('4800')['code'] == '4800', 'panel_exact が枝番へ進んでいる'
    assert a['name'].strip() == b['name'].strip(), '枝番へ進む条件は「同じパネル名」のはず'


def test_paint_panel_does_not_jump_to_a_different_panel():
    """3 桁が同じでも名前が違う枝番には飛ばない（別パネルの面積を黙って使わない）"""
    from paint_index import PaintIndex
    nb = NeoBuilder()
    pi = PaintIndex(nb.engine.root, 'W90', body='20')
    base = {x['code']: x for x in pi.panels}
    for code, row in base.items():
        got = pi.panel(code)
        if got and got['code'] != code:
            assert got['name'].replace(' ', '') == row['name'].replace(' ', ''), \
                f'{code} -> {got[chr(39)+chr(99)+chr(111)+chr(100)+chr(101)+chr(39)]} で名前が変わっている'


def test_paint_panel_records_when_body_row_cannot_be_chosen():
    """ボディ用の行を選べなかったときは控える（黙って他ボディの面積を使わない）。
    厳密一致（panel_exact）は枝番へ進まない用途なので控えない"""
    from paint_index import PaintIndex
    nb = NeoBuilder()
    pi = PaintIndex(nb.engine.root, 'D29', body='10')
    pi.panel('2602')
    if not pi.body_unresolved:
        print('     （この ADDATA では曖昧な枝番が無いので検査しない）')
    else:
        u = pi.body_unresolved[0]
        assert u['code'] == '2602' and u['candidates'], f'控えの中身が足りない（{u}）'
    pi2 = PaintIndex(nb.engine.root, 'W90', body='20')
    pi2.panel_exact('4800')
    assert not pi2.body_unresolved, 'panel_exact で控えている（枝番へ進まない用途なので誤検知になる）'
    pi3 = PaintIndex(nb.engine.root, 'W90', body='20')
    pi3.panel('4800'); pi3.panel('2700')
    assert not pi3.body_unresolved, f'選べているのに控えている（{pi3.body_unresolved}）'


def test_paint_panel_branch_code_is_added_not_linked():
    """枝番でコードが変わるパネル（W90 ハイエース: 明細 4800 → ボディ 20 の塗装パネル 4801）は
    「パネル追加」（AddedFrom 1・工賃 '*'）で書く。**連動（AddedFrom 0）にしてはいけない**。
    コグニ実機 2026-09-11: 連動にすると塗装ページを開いたときに行ごと消え、
    塗装計が 176,560 → 142,610 に落ちた（コグニは連動行を明細から作り直すため）"""
    from paint_index import PaintIndex
    nb = NeoBuilder()
    pi = PaintIndex(nb.engine.root, 'W90', body='20')
    ex = pi.panel_exact('4800')
    alt = pi.panel('4800')
    assert ex and ex['code'] == '4800', f'20.DB に 4800 が無い（{ex}）'
    assert alt and alt['code'] == '4801', f'ボディ 20 の枝番 4801 に進んでいない（{alt}）'
    # 明細の部品コード（4800）と塗装パネルのコード（4801）が違うことが、連動にできない理由そのもの
    assert ex['code'] != alt['code'], '枝番でコードが変わる例になっていない'
    with open(os.path.join(HERE, os.pardir, 'estimate_to_neo.py'), encoding='utf-8') as _f:
        src = _f.read()
    assert '_with_branch' not in src, '枝番補正で連動判定をしている（実機では行が消える）'


def test_silent_errors_reach_the_report():
    """「失敗しても続ける」箇所の理由が report に出ること。
    控えるだけだと誰も読まないので、run_case が ★ で出せるよう report['silent_errors'] に載せる。
    build ごとに初期化され、前の案件の失敗が次の報告に残らないことも見る"""
    base = dict(BASE)
    base['items'] = [{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'qty': 1,
                      'price': 30000, 'wage': 8000, 'index': 1.0}]
    base['totals'] = {}
    nb = NeoBuilder()
    _neo, rep = nb.build(base, VEH, labor_rate=8000, est_date='20260909', insurance={})
    assert 'silent_errors' in rep, "report に silent_errors が無い（握り潰しが報告に出ない）"
    assert not rep['silent_errors'], f"この見積で握り潰しが起きている（{rep['silent_errors']}）"
    nb._note_silent('試験', ValueError('わざと'), '影響の説明')
    nb._note_silent('試験', ValueError('わざと'), '影響の説明')
    assert len(nb.silent_errors) == 1, f'同じ理由を重ねて控えている（{nb.silent_errors}）'
    _neo2, rep2 = nb.build(base, VEH, labor_rate=8000, est_date='20260909', insurance={})
    assert not rep2['silent_errors'], f'前回の控えが次の報告に残っている（{rep2["silent_errors"]}）'
    # build_rows の直呼びは build と違って初期化されないので、この呼び出しで増えた分だけを stats に載せる
    nb._note_silent('前の案件', ValueError('古い失敗'), '次の案件には関係ない')
    _rows, st = nb.build_rows(base['items'], (rep.get('car') or {}).get('CarCode') or 'J97', labor_rate=8000)
    assert st.get('silent_errors') == [], f'build_rows 直呼びで前回分を引き継いでいる（{st.get("silent_errors")}）'
    # 同じ失敗が続けて起きても、毎回その呼び出しの stats に出る（重複除去で 2 回目が消えない）
    got = []
    for _ in (1, 2):
        _r, st2 = nb.build_rows(base['items'], 'ZZZ', labor_rate=8000)   # 存在しない車種 → 11.DB が開けない
        got.append(st2.get('silent_errors') or [])
    assert got[0] and got[1] == got[0], f'2 回目の同じ失敗が stats から消えている（{got}）'
    assert '11.DB' in got[1][0], f'11.DB の失敗として出ていない（{got[1]}）'


def test_parts_db_failure_does_not_raise():
    """11.DB が無い・壊れているときは「空で続ける」のが仕様。
    例外処理の中で別クラスのメソッドを呼ぶと AttributeError になって build ごと落ちる（Codex 指摘）"""
    from estimate_to_neo import AddataParts
    nb = NeoBuilder()
    parts = AddataParts(nb.engine, 'ZZZ')   # 存在しない車種コード → 11.DB が開けない
    out = parts._load_11_raw()
    assert out == {}, f'失敗時は空で返すはず（{list(out)[:3]}）'
    assert getattr(parts, '_r11_error', ''), '失敗の理由が控えられていない'
    assert not hasattr(parts, '_note_silent'), 'AddataParts に NeoBuilder のメソッドを生やしている'


def test_flag_reads_false_as_false():
    """人が書く JSON の真偽値欄。文字列 "false" が真になると、実在車種を汎用車種で作ってしまう。
    判断できない値は黙って既定に落とさず ValueError にする"""
    from estimate_to_neo import _flag
    for v in (None, '', False, 0, '0', 'false', 'FALSE', 'no', '無し', 'なし'):
        assert _flag(v, 'x') is False, f'{v!r} を真と読んでいる'
    for v in (True, 1, '1', 'true', 'TRUE', 'yes', 'あり', 'はい'):
        assert _flag(v, 'x') is True, f'{v!r} を偽と読んでいる'
    for v in ('maybe', 2, -1, 'ー'):
        try:
            _flag(v, 'x')
        except ValueError:
            continue
        raise AssertionError(f'{v!r} を黙って受けている（人の書き損じに気づけない）')


def test_painting_panel_linked_needs_same_disposal():
    """塗装パネルを W/S 連動（AddedFrom 0）にしてよいのは、明細に**同じ部品コードかつ同じ修理方法**の
    行があるときだけ。実機 NEO 29 本の連動パネル 68 行はすべてそうなっている（食い違い 0）。
    部品コードだけで連動と決めると、コグニが塗装ページを開いたときに行が作り直されて形が変わる"""
    with open(os.path.join(HERE, os.pardir, 'estimate_to_neo.py'), encoding='utf-8') as _f:
        src = _f.read()
    assert 'linked_disp' in src, '連動判定が修理方法を見ていない'
    assert 'linked = disp_pnl in linked_disp' in src, '連動判定が部品コードだけになっている'
    # 関門: 明細に無い連動パネルを書こうとしたら止まること
    base = dict(BASE)
    base['items'] = [{'code': '0600', 'name': 'ﾌ-ﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000, 'wage': 8000, 'index': 1.0}]
    base['totals'] = {}
    base['paint'] = {'paint': '2K', 'coat': 'ソリッド', 'hf': 'しない',
                    'panels': [{'code': '0600', 'name': 'ﾌ-ﾄﾞ', 'method': '取替'}]}
    _neo, rep = NeoBuilder().build(base, VEH, labor_rate=8000, est_date='20260909', insurance={})
    pp = rep.get('totals')
    assert pp, '塗装ありの見積を作れていない'


def test_base_time_standard_comes_from_addata():
    """加算基礎・ブースの「標準値」欄には ADDATA の標準値を入れる（見積値ではない）。
    見積値を標準として書くと、工場が標準と違う加算基礎を出したときに「標準どおり」の顔をしてしまい、
    コグニの再計算で戻る。実機 NEO 29 本はすべて BaseTime == BaseTimeStandard"""
    with open(os.path.join(HERE, os.pardir, 'estimate_to_neo.py'), encoding='utf-8') as _f:
        src = _f.read()
    assert 'st_std = sb if sb is not None else st' in src, '加算基礎の標準値欄に見積値を入れている'
    assert 'st, st_std,' in src, 'BaseTime / BaseTimeStandard の組が直っていない'
    # 標準値は取れないことがある（汎用車種・CHM の無い車種）。未代入のまま参照すると生成が落ちる
    assert 'bb = sb = None' in src, '標準値を初期化していない（UnboundLocalError になる）'
    # ブースの標準は 0.0 が「加算なし」という有効値。0 を偽と見て見積工賃で埋めてはいけない
    assert 'rp(bt_std) if bt_std is not None else bw' in src, '標準工賃 0 を見積工賃で上書きしている'


def test_row_flags_are_normalised_at_the_entrance():
    """明細の真偽値欄（manual / reserve）は入口で 1 回正規化し、以降は正規化済みの値を見る。
    箇所ごとに直すと必ず取りこぼす（実際 manual が入口だけ直って後段に生値が残っていた）。
    recycle は真偽値ではなくリサイクル部品の情報（dict）なので触らない"""
    base = dict(BASE)
    base['items'] = [
        {'code': '0600', 'name': 'ﾌ-ﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000,
          'wage': 8000, 'index': 1.0, 'manual': 'false', 'reserve': 'false'},
        {'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟ', 'method': '取替', 'qty': 1, 'price': 30000,
          'wage': 8000, 'index': 1.0,
          'recycle': {'name': 'ﾘｻｲｸﾙFﾊﾞﾝﾊﾟ', 'price': 9000}},   # dict のまま渡っても壊れない
    ]
    base['totals'] = {}
    _neo, rep = NeoBuilder().build(base, VEH, labor_rate=8000, est_date='20260909', insurance={})
    rows = rep['rows']
    assert len(rows) >= 2, f'明細が作れていない（{len(rows)} 行）'
    r0 = rows[0]
    assert r0.get('_manual') is False, f'"manual": "false" を手入力扱いにしている（{r0.get("_manual")!r}）'
    assert r0.get('_reserve') is False, f'"reserve": "false" を保留扱いにしている（{r0.get("_reserve")!r}）'
    assert r0.get('PartsCode'), '"manual": "false" の行が照合されていない'


def test_eva_exclude_reaches_row_generation():
    """hints.eva_exclude は最終の装備リストだけでなく、**行生成に使う EVA（_row_ctx）にも効く**こと。
    ここに効かないと、11/13/83.DB の変種選択が除外前の装備で進み、品番・価格がずれる。
    4WD の Z（車種特定が 4WD なら自動で付く）も外せる"""
    with open(os.path.join(HERE, os.pardir, 'estimate_to_neo.py'), encoding='utf-8') as _f:
        src = _f.read()
    assert "eva_exclude') or []) if str(x).strip())}" in src or 'eva_exclude' in src.split("'eva':")[1][:600],         '_row_ctx の eva に eva_exclude が効いていない'
    assert "'Z' not in _excl" in src, '4WD の Z が eva_exclude を無視している'
    base = dict(BASE)
    base['items'] = [{'code': '0600', 'name': 'ﾌ-ﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000,
                      'wage': 8000, 'index': 1.0}]
    base['totals'] = {}
    _n1, r1 = NeoBuilder().build(base, VEH, labor_rate=8000, est_date='20260909', insurance={})
    _n2, r2 = NeoBuilder().build(base, VEH, hints={'eva_codes': ['U'], 'eva_exclude': ['U']},
                               labor_rate=8000, est_date='20260909', insurance={})
    assert 'U' not in (r2.get('eva') or []), f'eva_exclude が効いていない（{r2.get("eva")}）'
    assert r1 is not None


def test_split_panel_area_is_reported():
    """同じパネルコードに面積の違う行が複数あるとき、黙って先頭を採らずに控えること。
    面積 = 塗装指数なので、どちらを採るかで見積が変わる。
    ADDATA 全 1,204 車種で 258 組（C39 ﾌ-ﾄﾞ 0600 が 131 と 149、C48 LRﾄﾞｱ 2700 が 67 と 80）。
    2026-09-12 まで警告も出ていなかった"""
    from paint_index import PaintIndex
    nb = NeoBuilder()
    for car, code, want in (('C39', '0600', [131, 149]), ('C48', '2700', [67, 80])):
        pi = PaintIndex(nb.engine.root, car)
        r = pi.panel(code)
        if r is None:
            print('     （この ADDATA に %s が無いので飛ばす）' % car)
            continue
        u = [x for x in pi.body_unresolved if x['code'] == code]
        assert u, f'{car} {code}: 面積が割れているのに控えていない（黙って {r["area"]} を採った）'
        assert u[0].get('areas') == want, f'{car} {code}: 控えた面積が違う（{u[0].get("areas")} / 期待 {want}）'
    # 面積が 1 つしかないパネルでは控えない（誤検知しない）
    pj = PaintIndex(nb.engine.root, 'J97')
    pj.panel('0600')
    assert not [x for x in pj.body_unresolved if x.get('areas')], f'誤検知している（{pj.body_unresolved}）'



def test_prepare_area_formula():
    """下処理面積の近似 = 四捨五入(切上(面積 × 割合) × 0.345)。実機の 13 例が合う（W66 ルーフ 287 だけ合わない = 既知差 W66y。judgment_rules 10-18）"""
    from paint_index import PaintIndex
    cases = [(81, '1/2', 14), (88, '1/2', 15), (40, '1/2', 7), (68, '1/2', 12), (45, '1/1', 16), (285, '1/3', 33), (94, '1/2', 16), (22, '1/3', 3),
             (28, '1/2', 5), (47, '1/3', 6), (79, '1/1', 27), (37, '1/2', 7), (20, '1/1', 7), (88, '1/1', 30)]
    for area, ratio, want in cases:
        got = PaintIndex.prepare_area(area, ratio)
        assert got == want, (area, ratio, got, want)
    assert PaintIndex.prepare_area(81, '') == -1  # 取替（新品）は -1


def test_repair_row_construct_group_and_price_flag():
    """修理(2) 行: ConstructGroup は 11.DB の修理(S) 行の値（取替行ではない）、価格未入力なら PartsPriceFlag 1（標準品番 '-' でも）。
    実機 2026-09-12 W66 4802（S 行 'Z0'）/ 4600（取替行 'M8'・S 行 '  '）"""
    veh = {'model_code': 'NHP170', 'serial_no': '', 'desig': '19020', 'category': '0005', 'reg_date': 'R2.2', 'color_code': '209'}
    est = dict(BASE, vehicle=veh, items=[{'code': '4802', 'name': '左 ｸｵｰﾀﾊﾟﾈﾙ', 'method': '修理', 'qty': 1, 'price': 0},
                                       {'code': '4600', 'name': 'ﾎﾞﾃﾞｰﾛﾜﾊﾞｯｸﾊﾟﾈﾙｱｳﾀ', 'method': '修理', 'qty': 1, 'price': 0},
                                       {'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '修理', 'qty': 1, 'price': 0, 'wage': 19400}],
              paint={'paint': '2K', 'coat': '2コートパール', 'hf': 'しない', 'panels': []})
    neo, rep = NeoBuilder().build(est, veh, labor_rate=97000, est_date='20260912', insurance={})
    rows = {r['PartsCode']: r for r in rep['rows']}
    assert rows['4802']['ConstructGroup'] == 'Z0', rows['4802']['ConstructGroup']
    assert rows['4600']['ConstructGroup'] == '  ', repr(rows['4600']['ConstructGroup'])
    assert rows['0600']['ConstructGroup'] == '  ', repr(rows['0600']['ConstructGroup'])  # '#' の修理行も S 行の値（'  '）
    assert rows['4802']['PartsPriceFlag'] == 1 and rows['4600']['PartsPriceFlag'] == 1 and rows['0600']['PartsPriceFlag'] == 1
    assert rows['4802']['PartsNoStandard'].strip() == '-' and rows['4802']['PartsPriceStandardOutTax'] == 0


def test_bumper_only_paint_writes_bumper_base():
    """パネル無しでバンパだけ塗装: 加算基礎数値は -1、BAN.DB のバンパ加算基礎が BumperBase* に入りバンパ計に含まれる（実機 2026-09-12 W66 cogni_W66w）"""
    import sqlite3
    veh = {'model_code': 'NHP170', 'serial_no': '', 'desig': '19020', 'category': '0005', 'reg_date': 'R2.2', 'color_code': '209'}
    est = dict(BASE, vehicle=veh, items=[{'code': '0010', 'name': 'F ﾊﾞﾝﾊﾟｶﾊﾞｰ', 'method': '取替', 'qty': 1, 'price': 58700}],
               paint={'paint': '2K', 'coat': '2コートパール', 'hf': 'しない', 'panels': [], 'bumper_front': {'method': '新品', 'color': '一色'}, 'material_rate': 15})  # 実機はコグニ既定 15% のまま
    neo, rep = NeoBuilder().build(est, veh, labor_rate=97000, est_date='20260912', insurance={})
    ck = _nc.find_real_cks(neo); dec = _nc.decompress_neo(neo, ck); _m, entries = _nc.parse_entries(neo, ck[0]); files = _nc.extract_files(dec, entries)
    t = tempfile.NamedTemporaryFile(delete=False, suffix='.sld'); t.write(files['AnSvEm0001.sld']); t.close()
    em = sqlite3.connect(t.name)
    plan = em.execute('SELECT BaseTime, BaseWageOutTax, BaseWageByManual, BumperBaseTime, BumperBaseTimeStandard, BumperBaseWageOutTax, BumperBaseWageByManual FROM PaintingPlan').fetchone()
    assert tuple(plan) == (-1, -1, '', 0.5, 0.5, 48500, '$'), tuple(plan)
    fb = em.execute('SELECT fb_Disposal, fb_Time, fb_WageOutTax, fb_WageByManual FROM PaintingBumper').fetchone()
    assert tuple(fb) == (1, 2.0, 194000, '$'), tuple(fb)
    tot = em.execute('SELECT TimeTotalPanel, TimeTotalBumper, TimeTotal, WageTotalBumperOutTax, WageTotalOutTax, MaterialTotalOutTax, TotalOutTax FROM PaintingTotal').fetchone()
    assert tuple(tot) == (0, 2.5, 2.5, 242500, 242500, 36380, 278880), tuple(tot)
    n_panel = em.execute('SELECT COUNT(*) FROM PaintingPanel').fetchone()[0]
    em.close(); os.unlink(t.name)
    assert n_panel == 0


def test_bumper_only_paint_rejects_other_detail_keys():
    """panels: [] + bumper_front に付加塗装（wax 等）が混じる組合せは実機未確認なので従来どおり ValueError（Codex 指摘）"""
    veh = {'model_code': 'NHP170', 'serial_no': '', 'desig': '19020', 'category': '0005', 'reg_date': 'R2.2', 'color_code': '209'}
    est = dict(BASE, vehicle=veh, items=[{'code': '0010', 'name': 'F ﾊﾞﾝﾊﾟｶﾊﾞｰ', 'method': '取替', 'qty': 1, 'price': 58700}],
               paint={'paint': '2K', 'coat': '2コートパール', 'hf': 'しない', 'panels': [], 'bumper_front': {'method': '新品', 'color': '一色'}, 'wax': {'count': 2}})
    try:
        NeoBuilder().build(est, veh, labor_rate=97000, est_date='20260912', insurance={})
    except ValueError as e:
        assert 'paint.wax' in str(e) or 'panels' in str(e), str(e)
    else:
        raise AssertionError('wax 付きのバンパ単独塗装が通ってしまった')
    for extra in ({'sealing': {'length': 2.0}}, {'other': [{'name': '内板調色', 'wage': 5000}]}, {'frame': {'front_pillar': 1}}, {'base': {'index': 3.0}}):
        est2 = dict(est, paint={'paint': '2K', 'coat': '2コートパール', 'hf': 'しない', 'panels': [], 'bumper_front': {'method': '新品', 'color': '一色'}, **extra})
        try:
            NeoBuilder().build(est2, veh, labor_rate=97000, est_date='20260912', insurance={})
        except ValueError:
            pass
        else:
            raise AssertionError(f'{list(extra)} 付きのバンパ単独塗装が通ってしまった')


def test_change_total_uses_body_alias_row_when_time_is_missing():
    """標準なし（Time -1）でも取替合計にはボディ専用行（sub が枝番違い 4801）の指数が入る（実機 2026-09-12 W90b 4800: 44,900 + 9.7h × 97,000 = 985,800）"""
    veh = {'model_code': 'TRH229W', 'serial_no': '', 'desig': '19660', 'category': '0004', 'reg_date': 'R7.3', 'color_code': '070'}
    est = dict(BASE, vehicle=veh, items=[{'code': '2700', 'name': '左 ｽﾗｲﾄﾞﾄﾞｱ', 'method': '取替', 'qty': 1, 'price': 78700, 'parts_no': '67004-26620'},
                                       {'code': '4800', 'name': '左 ｸｵｰﾀﾊﾟﾈﾙ', 'method': '取替', 'qty': 1, 'price': 44900, 'parts_no': '61612-26560'}],
              paint={'paint': '2K', 'coat': '3コートパール', 'hf': 'しない', 'panels': [{'code': '2700', 'name': 'L ｽﾗｲﾄﾞﾄﾞｱﾊﾟﾈﾙ', 'method': '取替'}]})
    neo, rep = NeoBuilder().build(est, veh, labor_rate=97000, est_date='20260912', insurance={})
    rows = {r['PartsCode']: r for r in rep['rows']}
    assert rep['car']['BodyCode'] == '20', rep['car']
    assert rows['4800']['Time'] == -1 and rows['4800']['WageOutTax'] == -1, (rows['4800']['Time'], rows['4800']['WageOutTax'])
    assert rows['4800']['ChangeTotalOutTax'] == 985800, rows['4800']['ChangeTotalOutTax']
    assert rows['2700']['Time'] == 1.9, rows['2700']['Time']  # 共通行へ逃げる側（装備不一致）は従来どおり


def test_assumed_labor_rate_is_reported():
    """labor_rate が無く工賃÷指数からも決められないときは 7,280 円を仮定し、stats.labor_rate_assumed で知らせる（黙って通さない）"""
    est = dict(BASE, items=[{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 50000}])
    est.pop('labor_rate', None)
    neo, rep = NeoBuilder().build(est, VEH, labor_rate=None, est_date='20260912', insurance={})
    assert rep['stats']['labor_rate'] == 7280 and rep['stats'].get('labor_rate_assumed') is True, rep['stats']
    est2 = dict(est, labor_rate=8000)
    neo2, rep2 = NeoBuilder().build(est2, VEH, labor_rate=8000, est_date='20260912', insurance={})
    assert rep2['stats']['labor_rate'] == 8000 and rep2['stats'].get('labor_rate_assumed') is False, rep2['stats']


def test_run_case_warns_known_unresolved_combination():
    """金額に影響する既知の未解決（4600 取替 + クオータ取替の連動加算）は run_case が ★ で知らせる"""
    import run_case
    est = {'items': [{'code': '4600', 'method': '取替'}, {'code': '4800', 'method': '取替'}]}
    assert run_case._warn_known_unresolved(est), '4600+4800 で警告が出ない'
    assert not run_case._warn_known_unresolved({'items': [{'code': '4600', 'method': '取替'}, {'code': '2700', 'method': '取替'}]})
    assert not run_case._warn_known_unresolved({'items': [{'code': '4600', 'method': '修理'}, {'code': '4800', 'method': '取替'}]})


def test_material_rate_default_uses_guideline_when_present():
    """材料代割合の既定 = ガイドライン表の既定列（default_band）。表が無ければコグニ既定（AnUsrTblPnt）"""
    import json, tempfile
    import estimate_to_neo as e_
    # 値は合成のダミー（実際の社内表の値はリポジトリに入れない）。列の選び方と塗料・クリヤー・塗膜の対応だけを確かめる
    g = {'material_rate': {'bands': [1000, 2000, 3000], 'default_band': 2000,
                           '2K': {'標準': {'ソリッド': [1, 2, 3], 'メタリック': [4, 5, 6], '2P': [7, 8, 9], '3P': [10, 11, 12]},
                                  '耐擦傷性': {'2P': [13, 14, 15]}, 'スクラッチシールド': {'2P': [16, 17, 18]}},
                           '水性': {'標準': {'2P': [19, 20, 21]}}}}
    d = tempfile.mkdtemp(prefix='guide_'); p = os.path.join(d, 'g.json')
    json.dump(g, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
    old = os.environ.get('PDF_TO_NEO_GUIDELINE'); os.environ['PDF_TO_NEO_GUIDELINE'] = p
    try:
        assert e_.default_material_rate(3, 3, 0) == 8.0           # 2K 2P 標準 の既定列（2000〜 = 2 列目）
        assert e_.default_material_rate(3, 3, 2) == 14.0          # 耐スリ傷 = 耐擦傷性
        assert e_.default_material_rate(3, 3, 3) == 17.0          # スクラッチ = スクラッチシールド
        assert e_.default_material_rate(4, 3, 0) == 20.0          # 水性
        assert e_.default_material_rate(3, 3, 1) == e_.cogni_default_material_rate(3, 3, 1)  # フッ素は表に無い → コグニ既定
        g['material_rate']['2K']['標準']['メタリック'] = [4, 6500, 6]  # 壊れた値（割合でない）は使わない
        json.dump(g, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
        assert e_.default_material_rate(3, 2, 0) == e_.cogni_default_material_rate(3, 2, 0)
        os.environ['PDF_TO_NEO_GUIDELINE'] = os.path.join(d, 'none.json')
        assert e_.default_material_rate(3, 3, 0) == e_.cogni_default_material_rate(3, 3, 0)  # 表が無い PC
    finally:
        if old is None:
            os.environ.pop('PDF_TO_NEO_GUIDELINE', None)
        else:
            os.environ['PDF_TO_NEO_GUIDELINE'] = old


def test_scratch_high_function_paint():
    """高機能塗装 スクラッチ（HFPainting 3 'ｽｸﾗｯﾁ'）: 加算基礎は T_KEI_3 の S 列、パネル加算は式が未同定なので標準なし（index 必須）。
    知らない高機能塗装の名前は黙って「しない」にせず止める（実案件 NEO 100 本で 3 を確認。2026-09-12）"""
    import paint_index as pi_
    assert pi_.HF_CODE['スクラッチ'] == 3 and pi_.HF_KIND[3] == 'S' and pi_.HF_NAME[3] == 'ｽｸﾗｯﾁ'
    root = NeoBuilder().engine.root
    p = pi_.PaintIndex(root, 'W66', body='10')
    st = p.standard_times('0600', 3, 2, 3)
    assert st is not None and st['new'] is None and st['s1'] is None, st
    form = p.form_codes()[0]
    assert p.base_time(form, 3, 3, 3, 2) is not None
    veh = {'model_code': 'NHP170', 'serial_no': '', 'desig': '19020', 'category': '0005', 'reg_date': 'R2.2', 'color_code': '209'}
    est = dict(BASE, vehicle=veh, items=[{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 30000}],
               paint={'paint': '2K', 'coat': '2コートパール', 'hf': 'スクラッチシールド', 'material_rate': 30,
                      'panels': [{'code': '0600', 'name': 'ﾌ-ﾄﾞ', 'method': '取替', 'index': 2.9}]})
    neo, rep = NeoBuilder().build(est, veh, labor_rate=7280, est_date='20260912', insurance={})
    ck = _nc.find_real_cks(neo); dec = _nc.decompress_neo(neo, ck); _m, entries = _nc.parse_entries(neo, ck[0]); files = _nc.extract_files(dec, entries)
    t = tempfile.NamedTemporaryFile(delete=False, suffix='.sld'); t.write(files['AnSvEm0001.sld']); t.close()
    em = sqlite3.connect(t.name)
    plan = em.execute('SELECT HFPainting, HFPaintingName FROM PaintingPlan').fetchone()
    em.close(); os.unlink(t.name)
    assert tuple(plan) == (3, 'ｽｸﾗｯﾁ'), tuple(plan)
    try:
        NeoBuilder().build(dict(est, paint=dict(est['paint'], hf='セラミック')), veh, labor_rate=7280, est_date='20260912', insurance={})
    except ValueError as ex:
        assert 'paint.hf' in str(ex), str(ex)
    else:
        raise AssertionError('知らない高機能塗装を通してしまった')


def _paint_db(neo):
    ck = _nc.find_real_cks(neo); dec = _nc.decompress_neo(neo, ck); _m, entries = _nc.parse_entries(neo, ck[0]); files = _nc.extract_files(dec, entries)
    t = tempfile.NamedTemporaryFile(delete=False, suffix='.sld'); t.write(files['AnSvEm0001.sld']); t.close()
    return t.name


def test_manual_paint_rows_are_disposal_9():
    """手入力の塗装行（外板パネル画面の「行追加」= PaintingPanel.DisposalCode 9。実案件 NEO 354 本・631 行。2026-09-13）:
    部品コード空・面積と標準欄 -1・指数あり '#'/Manual 1・工賃だけ '*'/Manual 2/Time -1・SortNo 15・AddedFrom 1・部品コードのあるパネルの後ろに入力順。
    塗装工賃計と材料代の対象に入り、加算基礎の枚数には数えない。手入力の塗装行だけの追加では加算基礎に '*' は付かない"""
    veh = {'model_code': 'NHP170', 'serial_no': '', 'desig': '19020', 'category': '0005', 'reg_date': 'R2.2', 'color_code': '209'}
    items = [{'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 30000}]
    coded = {'code': '0600', 'name': 'ﾌ-ﾄﾞ', 'method': '取替'}
    base_paint = {'paint': '2K', 'coat': '2コートパール', 'hf': 'しない', 'material_rate': 20}
    neo0, _ = NeoBuilder().build(dict(BASE, vehicle=veh, items=items, paint=dict(base_paint, panels=[coded])), veh, labor_rate=7360, est_date='20260913', insurance={})
    p0 = _paint_db(neo0); em0 = sqlite3.connect(p0)
    plan0 = tuple(em0.execute('SELECT BaseTime, BaseTimeStandard, BaseWageOutTax, BaseWageByManual FROM PaintingPlan').fetchone())
    tot0 = tuple(em0.execute('SELECT TimeTotalPanel, WageTotalPanelOutTax FROM PaintingTotal').fetchone())
    em0.close(); os.unlink(p0)
    # 手入力の塗装行は部品コード昇順の並べ替えに巻き込まれず、コードのあるパネル（入力は後ろ）の後に入力順で並ぶ
    panels = [{'manual': True, 'name': '左右ﾙｰﾌｻｲﾄﾞ', 'index': 4.0, 'method': '修理'}, {'name': 'Rrﾎﾞﾃﾞ-ﾌﾛｱ', 'wage': 12000}, coded]
    neo, rep = NeoBuilder().build(dict(BASE, vehicle=veh, items=items, paint=dict(base_paint, panels=panels)), veh, labor_rate=7360, est_date='20260913', insurance={})
    p = _paint_db(neo); em = sqlite3.connect(p)
    rows = em.execute('SELECT RecordNo, LineNo, PartsCode, DisposalCode, DisposalName, PanelName, Time, WageOutTax, WageByManual, Manual, AddedFrom, SortNo,'
                      ' PanelArea, PrepareArea, PaintingArea, TimeStandardNew, TimeStandardHF, WageStandardNewOutTax, MaterialOutTax, PanelCode, ButtonNo FROM PaintingPanel ORDER BY RecordNo').fetchall()
    plan = tuple(em.execute('SELECT BaseTime, BaseTimeStandard, BaseWageOutTax, BaseWageByManual FROM PaintingPlan').fetchone())
    tot = tuple(em.execute('SELECT TimeTotalPanel, WageTotalPanelOutTax, WageTotalOutTax, MaterialTotalOutTax FROM PaintingTotal').fetchone())
    em.close(); os.unlink(p)
    assert [r[2] for r in rows] == ['0600', '', ''] and [r[0] for r in rows] == [1, 2, 3] and [r[1] for r in rows] == [0, 1, 2], rows
    assert rows[1][3:12] == (9, '修理', '左右ﾙｰﾌｻｲﾄﾞ', 4.0, 29440, '#', 1, 1, 15), rows[1]
    assert rows[2][3:12] == (9, '', 'Rrﾎﾞﾃﾞ-ﾌﾛｱ', -1, 12000, '*', 2, 1, 15), rows[2]
    assert all(v == -1 for v in rows[1][12:]) and all(v == -1 for v in rows[2][12:]), rows
    assert plan == plan0, (plan, plan0)  # 加算基礎は部品コードのあるパネル 1 枚で数え、'*' も付かない
    assert abs(tot[0] - (tot0[0] + 4.0)) < 0.05 and tot[1] == tot0[1] + 29440 + 12000, (tot, tot0)
    assert tot[3] == round(tot[2] * 20 / 100 / 10) * 10, tot  # 材料代の対象に入る
    # 手入力の塗装行だけ: 加算基礎の標準は無い（-1）。バンパ加算基礎も付かない
    neo2, _ = NeoBuilder().build(dict(BASE, vehicle=veh, items=items, paint=dict(base_paint, panels=[{'name': 'Rrｹﾞ-ﾄﾊﾟﾈﾙ', 'index': 2.0}])), veh, labor_rate=7360, est_date='20260913', insurance={})
    p2 = _paint_db(neo2); em2 = sqlite3.connect(p2)
    plan2 = tuple(em2.execute('SELECT BaseTime, BaseTimeStandard, BaseWageOutTax, BaseWageByManual, BumperBaseTime FROM PaintingPlan').fetchone())
    tot2 = tuple(em2.execute('SELECT TimeTotalPanel, WageTotalPanelOutTax, TimeTotal FROM PaintingTotal').fetchone())
    em2.close(); os.unlink(p2)
    assert plan2 == (-1, -1, -1, '', -1), plan2
    assert tot2 == (2.0, 14720, 2.0), tot2
    # 見積に加算基礎があれば手入力 '#'（標準 -1）
    neo3, _ = NeoBuilder().build(dict(BASE, vehicle=veh, items=items, paint=dict(base_paint, panels=[{'name': 'Rrｹﾞ-ﾄﾊﾟﾈﾙ', 'index': 2.0}], base={'index': 2.5})), veh, labor_rate=7360, est_date='20260913', insurance={})
    p3 = _paint_db(neo3); em3 = sqlite3.connect(p3)
    plan3 = tuple(em3.execute('SELECT BaseTime, BaseTimeStandard, BaseWageOutTax, BaseWageByManual FROM PaintingPlan').fetchone())
    em3.close(); os.unlink(p3)
    assert plan3 == (2.5, -1, 18400, '#'), plan3
    # 名称のカタカナは半角にする（判断規則 10-22b。亮平さん指示 2026-09-17。実案件 NEO の外板パネル 851 行中 849 行が半角）。
    # 半角にすると 22 バイト → 13 バイトになるので、以前は 20 バイトで切れていた名前も切れなくなる
    neo6, _ = NeoBuilder().build(dict(BASE, vehicle=veh, items=items, paint=dict(base_paint, panels=[{'name': 'リヤボデーフロアパネル', 'index': 2.0}])), veh, labor_rate=7360, est_date='20260913', insurance={})
    p6 = _paint_db(neo6); em6 = sqlite3.connect(p6)
    n6 = em6.execute('SELECT PanelName FROM PaintingPanel WHERE DisposalCode = 9').fetchone()[0]
    em6.close(); os.unlink(p6)
    assert n6 == 'ﾘﾔﾎﾞﾃﾞｰﾌﾛｱﾊﾟﾈﾙ', n6
    # 手入力の塗装行だけでもブースの標準（BOOTH.DB。枚数と無関係）は引く（Codex 指摘）
    neo5, _ = NeoBuilder().build(dict(BASE, vehicle=veh, items=items, paint=dict(base_paint, panels=[{'name': 'Rrｹﾞ-ﾄﾊﾟﾈﾙ', 'index': 2.0}], booth={'use': True})), veh, labor_rate=7360, est_date='20260913', insurance={})
    p5 = _paint_db(neo5); em5 = sqlite3.connect(p5)
    b5 = tuple(em5.execute('SELECT BoothFlag, BoothTime, BoothTimeStandard FROM PaintingPlan').fetchone())
    em5.close(); os.unlink(p5)
    assert b5[0] == 1 and b5[1] > 0 and b5[1] == b5[2], b5
    # 指数と工賃の両方があり 工賃 ≠ 指数 × 単価: 見積の工賃のまま（行の金額は動かさない）で、知らせる
    nb = NeoBuilder()
    neo4, _ = nb.build(dict(BASE, vehicle=veh, items=items, paint=dict(base_paint, panels=[coded, {'name': 'Rrｹﾞ-ﾄﾊﾟﾈﾙ', 'index': 2.0, 'wage': 15000}])), veh, labor_rate=7360, est_date='20260913', insurance={})
    p4 = _paint_db(neo4); em4 = sqlite3.connect(p4)
    r4 = tuple(em4.execute('SELECT Time, WageOutTax, WageByManual FROM PaintingPanel WHERE DisposalCode = 9').fetchone())
    em4.close(); os.unlink(p4)
    assert r4 == (2.0, 15000, '#'), r4
    assert any('手入力の塗装行' in n_ and '14,720' in n_ for n_ in nb._paint_notes), nb._paint_notes
    # 指数も工賃も無い・名称が無い手入力の塗装行は止める
    for bad in ({'name': 'ﾙｰﾌｻｲﾄﾞ'}, {'manual': True, 'index': 2.0}):
        try:
            NeoBuilder().build(dict(BASE, vehicle=veh, items=items, paint=dict(base_paint, panels=[coded, bad])), veh, labor_rate=7360, est_date='20260913', insurance={})
        except ValueError as ex:
            assert '手入力の塗装行' in str(ex), str(ex)
        else:
            raise AssertionError(f'{bad} が通ってしまった')


def test_com_tables_follow_the_addata_in_use():
    """毎月変わる COM の表（DATAUP / Katashiki）は使っている ADDATA の COM.CAB から読む（同梱の写しは予備）"""
    import com_tables
    root = NeoBuilder().engine.root
    d = com_tables.com_dir(root)
    if not d:
        print('   skip test_com_tables_follow_the_addata_in_use（この PC では COM.CAB を展開できない）')
        return
    assert os.path.isfile(os.path.join(d, 'DATAUP.DB')) and os.path.isfile(os.path.join(d, 'Katashiki.DB'))
    assert com_tables.com_path(root, 'DATAUP.DB').startswith(d)
    assert com_tables.com_path('', 'DATAUP.DB').endswith(os.path.join('reference', 'DATAUP.DB'))  # ADDATA が無ければ予備
    com_tables.reset_sources(); com_tables.com_path('', 'DATAUP.DB')
    assert com_tables.stale_reference_used() == ['DATAUP.DB']  # 予備を使ったら報告される
    com_tables.reset_sources(); com_tables.com_path(root, 'DATAUP.DB')
    assert com_tables.stale_reference_used() == []
    import tempfile as _t
    fake = _t.mkdtemp(prefix='com_part_'); open(os.path.join(fake, 'DATAUP.DB'), 'wb').write(b'x')
    assert not com_tables._ok(fake)  # DATAUP だけの部分展開は揃ったとみなさない


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                fails += 1
                print('FAIL', name, e)
    print('unit_consistency:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
