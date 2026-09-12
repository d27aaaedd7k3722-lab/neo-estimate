# -*- coding: utf-8 -*-
"""ADDATA の場所の決め方の回帰テスト。

同じ PC に版の違う ADDATA が複数あることは普通にある（実測: このPCには
2026/08・2025/01・2020/03 の3つ）。**古い版を掴むと標準品番と標準指数が
変わる** ＝ 協定見積に載る部品コードや指数が実機と食い違う。

固定すること:
  1. pdf-to-neo スキルの設定（env_check.py --save の結果）を読むこと
  2. 候補が複数あるときは**データ版の新しいほう**を選ぶこと
     （浅い候補だけでなく、深い探索で見つけたものも）
  3. 全候補は版の新しい順に返すこと
  4. いま使っているものより新しい版があれば知らせられること
  5. 応答しないフォルダで固まらないこと（壁時計の上限）

**検証用のダミーだけを使う。実在の顧客データは扱わない。**
"""
import json
import os
import shutil
import sys
import tempfile
import time

R = os.environ.get('XROOT',
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, R)
os.chdir(R)
sys.stdout.reconfigure(encoding='utf-8')

import addata_locator as L  # noqa: E402

FAIL = []


def chk(cond, msg):
    if not cond:
        FAIL.append(msg)


def make_addata(root, version):
    """ADDATA らしく見えるダミーを作る。COM/AnVer.DB は XOR 0xFF の INI。"""
    os.makedirs(os.path.join(root, 'A', 'ZZZ'), exist_ok=True)
    with open(os.path.join(root, 'A', 'ZZZ', 'ZZZ01.DB'), 'wb') as f:
        f.write(b'dummy')
    os.makedirs(os.path.join(root, 'COM'), exist_ok=True)
    ini = ('[Version]\r\nNumber=%s\r\n' % version).encode('cp932')
    with open(os.path.join(root, 'COM', 'AnVer.DB'), 'wb') as f:
        f.write(bytes(x ^ 0xFF for x in ini))


TMP = tempfile.mkdtemp(prefix='reg_locator_')
try:
    old_v = os.path.join(TMP, 'old')
    new_v = os.path.join(TMP, 'new')
    make_addata(old_v, '2020/03')
    make_addata(new_v, '2026/08')

    # ── 1. 版が読めること ────────────────────────────────
    chk(L.addata_version(old_v) == '2020/03',
        '1a: 古い方の版が %r（2020/03 のはず）' % L.addata_version(old_v))
    chk(L.addata_version(new_v) == '2026/08',
        '1b: 新しい方の版が %r（2026/08 のはず）' % L.addata_version(new_v))
    chk(L.addata_version_key(new_v) > L.addata_version_key(old_v),
        '1c: 版の新しさの比べ方が逆になっている')

    # ── 2. 候補が複数あるときは新しい版を選ぶ ───────────────
    # 「浅い候補」の並びを差し替えて、古い方を先に置く。
    _std, _sub = L._STANDARD_PATHS, L._ONEDRIVE_SUBPATHS
    _od = L._candidate_onedrive_roots
    _cfg = L.config_addata_root
    try:
        L._STANDARD_PATHS = (old_v, new_v)     # わざと古い方を先に
        L._ONEDRIVE_SUBPATHS = ()
        L._candidate_onedrive_roots = lambda: []
        L.config_addata_root = lambda: None
        os.environ.pop('ADDATA_ROOT', None)
        got = L.find_addata(force_refresh=True)
        chk(got == new_v,
            '2: 古い版(%s)を先に置いたら古い方を選んだ（新しい方 %s を選ぶべき）'
            % (os.path.basename(old_v), os.path.basename(new_v)))

        # 全候補は版の新しい順
        allc = L.find_all_addata(force_refresh=True)
        chk(allc and allc[0] == new_v,
            '3: 全候補の先頭が新しい版でない %s' % [os.path.basename(x) for x in allc])

        # いま古い方を使っているなら、新しい方を知らせる
        newer = L.newer_addata_candidates(old_v, budget=5.0)
        chk(any(os.path.normcase(p) == os.path.normcase(new_v)
                for p, _v in newer),
            '4: 古い版を使っているのに、新しい版があることを知らせない'
            '（標準品番・標準指数が実機と食い違ったまま気づけない）')
        chk(not L.newer_addata_candidates(new_v, budget=5.0),
            '4b: 最新を使っているのに「もっと新しいものがある」と言っている')

        # ── 3. スキルの設定ファイルを読むこと ────────────────
        # env_check.py --save が書いた場所を、アプリも使う。
        # 同じ PC で2つの実装が別々の ADDATA を掴むと中身が変わる。
        L.config_addata_root = lambda: old_v
        got = L.find_addata(force_refresh=True)
        chk(got == old_v,
            '5: pdf-to-neo スキルの設定（env_check --save の結果）を読んでいない')
        # 環境変数の方が強いこと
        os.environ['ADDATA_ROOT'] = new_v
        try:
            chk(L.find_addata(force_refresh=True) == new_v,
                '5b: 環境変数より設定ファイルが優先されている')
        finally:
            os.environ.pop('ADDATA_ROOT', None)
    finally:
        L._STANDARD_PATHS, L._ONEDRIVE_SUBPATHS = _std, _sub
        L._candidate_onedrive_roots = _od
        L.config_addata_root = _cfg
        L._cache.clear()

    # ── 4. 設定ファイルの読み方そのもの ──────────────────────
    _cfg_path = L._SKILL_CONFIG
    try:
        p = os.path.join(TMP, 'pdf-to-neo.local.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'ADDATA_ROOT': new_v, 'COGNI_BIN': 'x'}, f)
        L._SKILL_CONFIG = p
        chk(L.config_addata_root() == new_v, '6: 設定ファイルから読めない')
        L._SKILL_CONFIG = os.path.join(TMP, 'nothing.json')
        chk(L.config_addata_root() is None, '6b: 設定ファイルが無いのに落ちる')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('{ 壊れた JSON')
        L._SKILL_CONFIG = p
        chk(L.config_addata_root() is None, '6c: 壊れた設定ファイルで例外が出る')
    finally:
        L._SKILL_CONFIG = _cfg_path

    # ── 5. 応答しないフォルダで固まらないこと ────────────────
    # 件数の上限では止まらない。壁時計の上限が要る。
    def _never():
        time.sleep(30)
        return 'これは返らない'

    t0 = time.time()
    got = L._bounded(_never, 1.0, 'あきらめた')
    el = time.time() - t0
    chk(got == 'あきらめた' and el < 5.0,
        '7: 返らない処理を打ち切れていない（%.1f秒待った）' % el)
    # 例外を投げても既定値が返ること
    chk(L._bounded(lambda: 1 / 0, 2.0, 'ok') == 'ok',
        '7b: 例外のときに既定値を返さない')

    # ── 6. 実機の設定が今どうなっているか（参考・失敗にはしない） ──
    _live = L.config_addata_root()
    print('  参考: この PC の設定ファイルの ADDATA =', _live or '(未設定)')
finally:
    shutil.rmtree(TMP, ignore_errors=True)
    L._cache.clear()

print('REG_LOCATOR:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL:
    print('  -', f)
sys.exit(1 if FAIL else 0)
