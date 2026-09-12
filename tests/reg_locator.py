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

    # ── 8. 同じルートの下に版違いが並んでいたら、新しい方を選ぶ ──────
    # 深い探索は「ルートごとに最初に見つけた1つ」しか候補にしていなかった。
    # 1つの OneDrive の下に古い Addata と新しい Addata が並んでいると、
    # 走査順しだいで古い方を掴む。
    od = os.path.join(TMP, 'od')
    make_addata(os.path.join(od, 'aaa_furui', 'Addata'), '2019/03')
    make_addata(os.path.join(od, 'zzz_atarashii', 'Addata'), '2026/08')
    bag = []
    L._walk_for_addata(od, max_depth=5, max_dirs=30000, collect=bag)
    chk(len(bag) >= 2,
        '8a: 深い探索が %d 個しか拾っていない（同じルートに2つあるのに）'
        % len(bag))
    _std2, _sub2 = L._STANDARD_PATHS, L._ONEDRIVE_SUBPATHS
    _od2, _cfg2 = L._candidate_onedrive_roots, L.config_addata_root
    try:
        L._STANDARD_PATHS = ()
        L._ONEDRIVE_SUBPATHS = ()
        L._candidate_onedrive_roots = lambda: [od]
        L.config_addata_root = lambda: None
        os.environ.pop('ADDATA_ROOT', None)
        L._cache.clear()
        got = L.find_addata(force_refresh=True)
        chk(got and os.path.basename(os.path.dirname(got)) == 'zzz_atarashii',
            '8b: 同じルートの下で古い版(2019/03)を選んだ → %s' % got)
    finally:
        L._STANDARD_PATHS, L._ONEDRIVE_SUBPATHS = _std2, _sub2
        L._candidate_onedrive_roots = _od2
        L.config_addata_root = _cfg2
        L._cache.clear()

    # ── 9. 締切を過ぎたらフォルダに触らないこと ─────────────────────
    # 「最低0.5秒は待つ」にしていたため、締切を過ぎてからも候補の数だけ
    # 待ち直し、約束した上限（既定20秒）を超えていた。
    _orig_valid = L._is_valid_addata
    _orig_sec = L._SEARCH_SECONDS
    try:
        def _slow(path, max_check=3):
            time.sleep(2.0)          # 応答しない共有のつもり
            return False
        L._is_valid_addata = _slow
        L._SEARCH_SECONDS = 3.0
        L._STANDARD_PATHS = tuple('Z:\dummy%d' % i for i in range(20))
        L._ONEDRIVE_SUBPATHS = ()
        L._candidate_onedrive_roots = lambda: []
        L.config_addata_root = lambda: None
        L._cache.clear()
        t0 = time.time()
        L.find_addata(force_refresh=True)
        el = time.time() - t0
        chk(el < 10.0,
            '9: 20 個の応答しない候補で %.1f 秒かかった'
            '（上限 3 秒と決めたのに守られていない。画面が固まる）' % el)
    finally:
        L._is_valid_addata = _orig_valid
        L._SEARCH_SECONDS = _orig_sec
        L._STANDARD_PATHS, L._ONEDRIVE_SUBPATHS = _std2, _sub2
        L._candidate_onedrive_roots = _od2
        L.config_addata_root = _cfg2
        L._cache.clear()

    # ── 10. 版を読めなかったら、順位を付けたふりをしないこと ────────
    # 締切を理由に版の読み取りを諦めると、全候補が同じ点数になって
    # 「並び順で選ぶ」ことになり、古い Addata を静かに掴む。
    _orig_key = L.addata_version_key
    try:
        def _hang(path):
            time.sleep(30)
            return (0.0, 0.0)
        L.addata_version_key = _hang
        L._STANDARD_PATHS = (old_v, new_v)
        L._ONEDRIVE_SUBPATHS = ()
        L._candidate_onedrive_roots = lambda: []
        L.config_addata_root = lambda: None
        os.environ.pop('ADDATA_ROOT', None)
        L._cache.clear()
        t0 = time.time()
        L.find_addata(force_refresh=True)
        el = time.time() - t0
        chk(el < 15.0, '10a: 版が読めないときに %.1f 秒待っている' % el)
        chk(L.rank_incomplete(),
            '10b: 版を読めなかったのに「順位を付けられなかった」ことが'
            '外から分からない（古い Addata を掴んでいても気づけない）')
    finally:
        L.addata_version_key = _orig_key
        L._STANDARD_PATHS, L._ONEDRIVE_SUBPATHS = _std2, _sub2
        L._candidate_onedrive_roots = _od2
        L.config_addata_root = _cfg2
        L._cache.clear()

    # ── 11. 締切を迂回する重複の段が残っていないこと ─────────────────
    import inspect
    _src = inspect.getsource(L.find_addata)
    # _valid() の中の1回だけが正しい姿。条件式で直に呼ぶ形（旧 2b の段）は
    # 時間の上限が効かず、画面が固まるうえ版も比べない。
    chk('if _is_valid_addata(' not in _src,
        '11: find_addata が時間の上限なしで _is_valid_addata を呼んでいる'
        '（旧 2b の段が戻っている）')
    chk(_src.count('_is_valid_addata(') == 1,
        '11b: _is_valid_addata の呼び出しが %d か所ある'
        '（_valid() の中の1か所だけのはず）' % _src.count('_is_valid_addata('))

    # ── 12. 締切で見送った候補は「無効」と同じ扱いにしないこと ────────
    # 時間切れで見ていないだけなのに未検出にすると、Addata が手元に
    # あるのにベタ打ちモードに落ちる。しかも None をキャッシュすると、
    # 以後ずっと未検出のままになる。
    _orig_valid2 = L._is_valid_addata
    _orig_sec2 = L._SEARCH_SECONDS
    try:
        def _slow2(path, max_check=3):
            time.sleep(5.0)
            return True          # 本当は有効な Addata
        L._is_valid_addata = _slow2
        L._SEARCH_SECONDS = 1.0
        L._STANDARD_PATHS = (new_v,)
        L._ONEDRIVE_SUBPATHS = ()
        L._candidate_onedrive_roots = lambda: []
        L.config_addata_root = lambda: None
        os.environ.pop('ADDATA_ROOT', None)
        L._cache.clear()
        got = L.find_addata(force_refresh=True)
        chk(got is None, '12a: 時間切れなのに何かを返している')
        chk(L.search_skipped(),
            '12b: 締切で見送った候補があることが外から分からない'
            '（Addata が手元にあるのにベタ打ちモードに落ちたまま気づけない）')
        # 毎回の画面更新で全部を探し直すと、遅い PC では操作のたびに
        # 固まる。しばらく（既定60秒）は未検出のまま返し、そのあと
        # 探し直せるようにしておく。
        chk(L._cache.get(L.RETRY_KEY),
            '12c: 時間切れの未検出に「探し直す時刻」が付いていない'
            '（永久に未検出のまま、または毎回の画面更新で全部探し直す）')
        t1 = time.time()
        chk(L.find_addata() is None and time.time() - t1 < 1.0,
            '12d: 探し直す時刻の前なのに、毎回フォルダを探し直している')
        L._cache[L.RETRY_KEY] = 0          # 時間が経ったことにする
        L._SEARCH_SECONDS = 0.2
        t1 = time.time()
        L.find_addata()
        chk(time.time() - t1 > 0.05,
            '12e: 探し直す時刻を過ぎても探し直していない'
            '（Addata が手元にあってもベタ打ちモードから戻れない）')
    finally:
        L._is_valid_addata = _orig_valid2
        L._SEARCH_SECONDS = _orig_sec2
        L._STANDARD_PATHS, L._ONEDRIVE_SUBPATHS = _std2, _sub2
        L._candidate_onedrive_roots = _od2
        L.config_addata_root = _cfg2
        L._cache.clear()

    # ── 13. 控えは毎回の探索のものであること（前回の値を残さない） ──────
    try:
        L._STANDARD_PATHS = (new_v,)
        L._ONEDRIVE_SUBPATHS = ()
        L._candidate_onedrive_roots = lambda: []
        L.config_addata_root = lambda: None
        os.environ['ADDATA_ROOT'] = new_v
        L._cache[L.RANK_KEY] = ['前回の残り']
        L._cache[L.SKIPPED_KEY] = ['前回の残り']
        L.find_addata(force_refresh=True)
        chk(L.rank_incomplete() == [],
            '13a: 環境変数で決まったのに前回の「版を読めなかった候補」が残る')
        chk(L.search_skipped() == [],
            '13b: 環境変数で決まったのに前回の「見ていない候補」が残る')
    finally:
        os.environ.pop('ADDATA_ROOT', None)
        L._STANDARD_PATHS, L._ONEDRIVE_SUBPATHS = _std2, _sub2
        L._candidate_onedrive_roots = _od2
        L.config_addata_root = _cfg2
        L._cache.clear()

    # ── 14. AnVer.DB が無い候補は「版で比べた」ことにしないこと ──────
    nover = os.path.join(TMP, 'nover')
    os.makedirs(os.path.join(nover, 'A', 'ZZZ'), exist_ok=True)
    with open(os.path.join(nover, 'A', 'ZZZ', 'ZZZ01.DB'), 'wb') as f:
        f.write(b'dummy')
    try:
        L._STANDARD_PATHS = (nover, new_v)
        L._ONEDRIVE_SUBPATHS = ()
        L._candidate_onedrive_roots = lambda: []
        L.config_addata_root = lambda: None
        os.environ.pop('ADDATA_ROOT', None)
        L._cache.clear()
        got = L.find_addata(force_refresh=True)
        chk(got == new_v, '14a: 版のある方(%s)ではなく %s を選んだ'
            % (os.path.basename(new_v), got))
        chk(any(os.path.normcase(x) == os.path.normcase(nover)
                for x in L.rank_incomplete()),
            '14b: AnVer.DB の無い候補を「版で比べた」ことにしている'
            '（更新日時で順位が付くだけなのに警告が出ない）')
    finally:
        L._STANDARD_PATHS, L._ONEDRIVE_SUBPATHS = _std2, _sub2
        L._candidate_onedrive_roots = _od2
        L.config_addata_root = _cfg2
        L._cache.clear()

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
