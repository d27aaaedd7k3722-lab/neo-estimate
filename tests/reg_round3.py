# -*- coding: utf-8 -*-
"""reg_round3.py — 2026-09-15 バグハント 3 回目の残り（CSV の列ずれ・単価・区切り、Gemini 呼び出しの上限と送り直し、
モジュールの指紋、Addata のエンジン、パイプラインの控えの鍵、手入力の印と単価欄）。LLM の API は呼ばない。

    python tests/reg_round3.py
終了コード: 0 全部 OK / 1 失敗
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault('NEO_ADDATA_NO_AUTODETECT', '1')
import logging  # noqa: E402

logging.getLogger('streamlit').setLevel(logging.CRITICAL)
import app  # noqa: E402

FAILS: list = []


def chk(cond, msg):
    if not cond:
        FAILS.append(msg)


def test_csv_shift_and_units():
    H = '品名,区分,数量,部品金額,工賃,部品コード\n'
    it, notes = app.parse_csv_to_items(H + 'フロントバンパー,取替,1,45,000,0,52119-X\n', return_notes=True)
    chk(not it and any(n.startswith('❌') and '列がずれ' in n for n in notes), f'M2: カンマ付き金額の列ずれを止めない: {it} {notes}')
    it, notes = app.parse_csv_to_items(H + 'クリップ,取替,10,1,550,0,\n', return_notes=True)
    chk(not it and any('列がずれ' in n for n in notes), f'M2: 末尾が空のセルの列ずれを止めない: {it} {notes}')
    it, notes = app.parse_csv_to_items(H + 'バンパー,取替,1,4万5千,0,\n', return_notes=True)
    chk(not it and any('数字として読めません' in n for n in notes), f'P19: 読めない金額を 0 円にしている: {it} {notes}')
    it, notes = app.parse_csv_to_items('品名,区分,数量,部品単価,工賃\nクリップ,取替,10,150,0\n', return_notes=True)
    chk(it and it[0]['parts_amount'] == 1500, f'O5/M6: 単価の列を行の金額にしている: {it}')
    # 工賃の行を 1 行入れておく（工賃の列に金額が 1 つも無い CSV は、工賃の行の金額も「金額」の列にあるはずなので止める。レビュー 5 周目）
    it, notes = app.parse_csv_to_items('品名,区分,数量,単価,金額,工賃\nクリップ,取替,10,155,1550,0\nﾄﾞｱ脱着,脱着,1,,,8000\n',
                                      return_notes=True)
    chk(it and it[0]['parts_amount'] == 1550, f'M6: 単価・金額の見出しで単価を金額にしている: {it}')
    it, notes = app.parse_csv_to_items('品名\t区分\t数量\t部品金額\t工賃\nﾍｯﾄﾞﾗﾝﾌﾟ\t取替\t1\t38000\t0\n', return_notes=True)
    chk(it and it[0]['parts_amount'] == 38000, f'M6: タブ区切りが読めない: {it}')
    it, notes = app.parse_csv_to_items('| 品名 | 区分 | 数量 | 部品金額 | 工賃 |\n|---|---|---|---|---|\n| ﾍｯﾄﾞﾗﾝﾌﾟ | 取替 | 1 | 38000 | 0 |\n', return_notes=True)
    chk(it and it[0]['parts_amount'] == 38000, f'M6: Markdown の表が読めない: {it}')
    it, notes = app.parse_csv_to_items('品名,区分,数量,値段\nﾍｯﾄﾞﾗﾝﾌﾟ,取替,1,38000\n', return_notes=True)
    chk(not it and any('金額の列' in n for n in notes), f'M6: 金額の列が分からない見出しで位置から推し量っている: {it} {notes}')
    txt = H + 'ﾊﾞﾝﾊﾟ,取替,1,45000,0,\n,,,38000,0,\n"部品相違 1,000円",,,,,\n上記のとおりです。\n説明の文です。\n'
    it, notes = app.parse_csv_to_items(txt, return_notes=True)
    chk(notes and '読み飛ばしました' in notes[0], f'M7: 金額のある行を読み飛ばした知らせが先頭に無い: {notes}')
    # 指示文は 1 行のまま部品金額と工賃を書かせる（M4）
    src = io.open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    chk('必ず2行に分割する' not in src and '1行のまま部品金額と工賃の両方を書く' in src, 'M4: 指示文が行を 2 つに分けさせている')


def test_gemini_limits():
    big = b'%PDF-1.4\n' + b'0' * (app.GEMINI_MAX_INLINE_BYTES + 10)
    try:
        app._gemini_ready_bytes(big, 'application/pdf')
        FAILS.append('P1-1: 大きすぎるファイルを送る前に断らない')
    except ValueError as e:
        chk('大きすぎます' in str(e), f'P1-1: 断る理由: {e}')
    r = app.analyze_insurance_document('dummy-key', big, 'application/pdf', model_name='x')
    chk(isinstance(r, dict) and '大きすぎます' in str(r.get('_error')), f'P1-1: 書類 OCR が大きすぎるファイルを送ろうとする: {r}')
    from PIL import Image
    frames = [Image.new('L', (200, 280), c) for c in (255, 200)]
    buf = io.BytesIO()
    frames[0].save(buf, format='TIFF', save_all=True, append_images=frames[1:])
    b, mt = app._gemini_ready_bytes(buf.getvalue(), 'image/tiff')
    chk(mt == 'application/pdf' and b[:5] == b'%PDF-', f'P11: TIFF を Gemini の対応外の形式のまま送る: {mt}')
    # 4xx は送り直さない
    calls = {'n': 0}

    class _E(Exception):
        code = 400

    class _Models:
        def generate_content(self, **kw):
            calls['n'] += 1
            raise _E('400 INVALID_ARGUMENT: bad request')

    class _Client:
        models = _Models()

    real = app._get_genai_client
    try:
        app._get_genai_client = lambda key: _Client()
        try:
            app.call_gemini('k', b'%PDF-1.4 x', 'application/pdf', 'p', model_name='m')
            FAILS.append('P11: 400 で例外にならない')
        except ValueError:
            pass
    finally:
        app._get_genai_client = real
    chk(calls['n'] == 1, f"P11: 400 を {calls['n']} 回送った（送り直しは要らない）")
    # 締め切り切れ（httpx の締め切り・サーバーの 504）は送り直さない（締め切り 10 分を 3 回待たせない。レビュー 2・3 周目）。
    # 接続の失敗（「Connection timed out」）は送り直す（文言の timed out で見分けて送り直さなかった。レビュー 3 周目）
    import httpx

    class _E504(Exception):
        code = 504

    for exc, want in ((httpx.ReadTimeout('The read operation timed out'), 1), (_E504('504 DEADLINE_EXCEEDED'), 1),
                      (httpx.ConnectError('[Errno 110] Connection timed out'), 3)):
        calls['n'] = 0

        def _raise(exc=exc):
            calls['n'] += 1
            raise exc

        class _Models2:
            def generate_content(self, **kw):
                _raise()

        class _Client2:
            models = _Models2()
        import time as _t
        real_sleep = _t.sleep
        try:
            _t.sleep = lambda *_a, **_k: None
            app._get_genai_client = lambda key: _Client2()
            try:
                app.call_gemini('k', b'%PDF-1.4 x', 'application/pdf', 'p', model_name='m')
                FAILS.append(f'{type(exc).__name__} で例外にならない')
            except ValueError as e:
                if want == 1:
                    chk('時間内' in str(e), f'締め切り切れの理由: {e}')
        finally:
            app._get_genai_client = real
            _t.sleep = real_sleep
        chk(calls['n'] == want, f"{type(exc).__name__} の送り回数: {calls['n']}（期待 {want}）")
    chk(app.GEMINI_TIMEOUT_MS >= 600_000, f'Gemini の締め切りが短い（長い明細の返事が毎回切れる）: {app.GEMINI_TIMEOUT_MS}')
    # クライアントには締め切り（P7）
    import inspect
    chk('HttpOptions(timeout=' in inspect.getsource(app._get_genai_client.__wrapped__ if hasattr(app._get_genai_client, '__wrapped__') else app._get_genai_client),
        'P7: Gemini クライアントに締め切りが無い')


def test_module_stamps_and_engine():
    import importlib
    for name in ('neo_skill.reader', 'neo_skill.llm', 'pdf_to_neo_pipeline', 'auto_matching', 'neo_header'):
        mod = importlib.import_module(name)
        chk(bool(getattr(mod, '__app_src_digest__', None)), f'N2: {name} が読み込み時の指紋を控えていない')
    chk(not app._stale_modules(), f'N2: 読み込んだばかりのモジュールが「古い」扱い: {app._stale_modules()}')
    import auto_matching as am
    real = am.AddataEngine
    try:
        am.AddataEngine = lambda root: ('engine', root)
        am._engines_by_root.clear()
        a = am._get_engine('ROOT_A')
        b = am._get_engine('ROOT_B')
        chk(a == ('engine', 'ROOT_A') and b == ('engine', 'ROOT_B') and am._get_engine('ROOT_A') is a, 'N4: root ごとのエンジンになっていない')
    finally:
        am.AddataEngine = real
        am._engines_by_root.clear()
    import pdf_to_neo_pipeline as P
    src = io.open(P.__file__, encoding='utf-8').read()
    chk("__app_src_digest__', ''))," in src and "strftime('%Y%m%d')" in src.split('cache_key = "|".join([')[1].split('])')[0],
        'N5: パイプラインの控えの鍵にコードの版・日付が無い')


def test_reload_failure_stays_stale():
    """読み直しが途中で例外になったモジュールは「揃った」と見ない（指紋はファイルの最後で控える。レビュー）"""
    import importlib
    import tempfile
    d = tempfile.mkdtemp(prefix='reg_round3_')
    path = os.path.join(d, 'zz_stamp_probe.py')
    stamp = ("\ntry:\n    import hashlib as _h\n    with open(__file__, 'rb') as _f:\n        __app_src_digest__ = _h.sha256(_f.read()).hexdigest()\n"
             "except Exception:\n    pass\n")
    io.open(path, 'w', encoding='utf-8').write("RULE_A = 'old'\nRULE_B = 'old'\n" + stamp)
    sys.path.insert(0, d)
    saved = app._APP_MODULES
    try:
        mod = importlib.import_module('zz_stamp_probe')
        app._APP_MODULES = tuple(saved) + ('zz_stamp_probe',)
        chk(not app._stale_modules(), '読み込んだばかりのモジュールが古い扱い')
        io.open(path, 'w', encoding='utf-8').write("RULE_A = 'new'\nraise RuntimeError('boom')\nRULE_B = 'new'\n" + stamp)
        os.utime(path, None)
        stale = app.sync_app_modules()
        chk('zz_stamp_probe' in stale, f'読み直しが途中で失敗したのに揃ったと見ている: {stale}')
        chk('再起動' in app._version_skew_message(['zz_stamp_probe']), '読み直しの失敗で「待てば揃う」と案内している')
        # 元の版に戻しても、読み直すまでは揃ったと見ない（指紋は元の版と同じだが、中身は新旧が混ざっている。レビュー 2 周目）
        chk(mod.RULE_A == 'new' and mod.RULE_B == 'old', f'読み直しの途中まで新しいという前提が崩れた: {mod.RULE_A} {mod.RULE_B}')
        io.open(path, 'w', encoding='utf-8').write("RULE_A = 'old'\nRULE_B = 'old'\n" + stamp)
        os.utime(path, None)
        chk('zz_stamp_probe' in app._stale_modules(), '失敗の記録があるのに、元の版の指紋と同じなので揃ったと見ている')
        stale = app.sync_app_modules()
        chk(not stale and mod.RULE_A == 'old' and 'zz_stamp_probe' not in app._reload_failures(),
            f'元の版に戻したあと読み直していない: {stale} {mod.RULE_A}')
    finally:
        app._APP_MODULES = saved
        sys.modules.pop('zz_stamp_probe', None)
        app._reload_failures().discard('zz_stamp_probe')
        sys.path.remove(d)


def test_module_changed_during_import_is_stale():
    '''読み込みの途中でファイルが書き換わった（push された）モジュールは指紋を控えず、古い扱いにして読み直させる（レビュー 3 周目）'''
    import importlib
    import tempfile
    src_app = io.open(app.__file__, encoding='utf-8').read()
    start = src_app[src_app.index('# 読み込みを始めたときのコードの指紋'):src_app.index('    _stamp_digest_at_start = None') + len('    _stamp_digest_at_start = None')]
    end = src_app[src_app.rindex('try:\n    import hashlib as _stamp_hashlib\n'):]
    d = tempfile.mkdtemp(prefix='reg_round3_')
    path = os.path.join(d, 'zz_midpush_probe.py')
    body = ("RULE = 'v1'\n"
            "with open(__file__, 'a', encoding='utf-8') as _f:\n"
            "    _f.write('\\n# pushed while importing\\n')\n")
    io.open(path, 'w', encoding='utf-8').write(start + '\n' + body + end)
    sys.path.insert(0, d)
    saved = app._APP_MODULES
    try:
        mod = importlib.import_module('zz_midpush_probe')
        chk(not getattr(mod, '__app_src_digest__', None), '読み込みの途中で書き換わったのに指紋を控えている')
        app._APP_MODULES = tuple(saved) + ('zz_midpush_probe',)
        chk('zz_midpush_probe' in app._stale_modules(), '読み込みの途中で書き換わったモジュールを「揃った」と見ている')
    finally:
        app._APP_MODULES = saved
        sys.modules.pop('zz_midpush_probe', None)
        sys.path.remove(d)


def test_initializing_module_not_stale():
    """初めて import している途中のモジュール（指紋はファイルの最後で控えるので、まだ無い）を「古い」と見て、
    別の利用者の変換を断らない（レビュー 2 周目）"""
    import importlib.machinery
    import types
    m = types.ModuleType('zz_init_probe')
    m.__file__ = app.__file__
    m.__spec__ = importlib.machinery.ModuleSpec('zz_init_probe', None)
    m.__spec__._initializing = True
    saved = app._APP_MODULES
    sys.modules['zz_init_probe'] = m
    try:
        app._APP_MODULES = tuple(saved) + ('zz_init_probe',)
        chk('zz_init_probe' not in app._stale_modules(), 'import の途中のモジュールを古い扱い')
        m.__spec__._initializing = False
        chk('zz_init_probe' in app._stale_modules(), '指紋の無い（読み込みを終えた）モジュールを古いと見ない')
    finally:
        app._APP_MODULES = saved
        sys.modules.pop('zz_init_probe', None)


def test_guideline_file_threads():
    """ガイドライン表の一時ファイルは、同じプロセスのスレッドが同時に初めて呼んでも全員に渡る（レビュー 2 周目。以前は
    プロセス番号だけの一時ファイル名がぶつかり、負けた変換に表が渡らなかった）"""
    import threading
    import uuid
    from neo_skill import vendor as v
    saved = {k: os.environ.get(k) for k in ('PDF_TO_NEO_GUIDELINE', 'PDF_TO_NEO_GUIDELINE_JSON')}
    os.environ.pop('PDF_TO_NEO_GUIDELINE', None)
    os.environ['PDF_TO_NEO_GUIDELINE_JSON'] = '{"test_only": "%s"}' % uuid.uuid4().hex
    got = []
    try:
        start = threading.Event()

        def worker():
            start.wait()
            got.append(v.guideline_file())
        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        start.set()
        for t in ts:
            t.join()
        chk(len(got) == 8 and len(set(got)) == 1 and got[0] and os.path.isfile(got[0]),
            f'同時に呼んだスレッドの一部に表が渡らない: {got}')
    finally:
        for f in set(got):
            try:
                if f:
                    os.unlink(f)
            except OSError:
                pass
        for k, val in saved.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val


def test_csv_kingaku_and_shift():
    """見出しの「金額」「部品」「番号」は明細の値で決める。決まらなければ止める。列ずれ・見出しの無い品番（レビュー 2 周目）"""
    def parse(t):
        it, notes = app.parse_csv_to_items(t, return_notes=True)
        if not it and any(str(n).startswith('❌') for n in notes):
            return 'ERR'
        return (sum(i['parts_amount'] for i in it), sum(i['wage'] for i in it))
    # 「工賃のある行の 0 円」は行の合計の列にも 1 行だけ混ざりうるので、2 行以上そろったときだけ部品と決める（レビュー 6 周目）
    r = 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\nﾄﾞｱ脱着,脱着,1,0,8000,\nﾊﾞﾝﾊﾟｰ塗装,塗装,1,0,32000,\n'
    chk(parse('品名,区分,数量,金額,工賃,部品コード\n' + r) == (45000, 52000), '「金額」の列の部品金額が 0 円')
    chk(parse('品名,区分,数量,金額,工賃,部品コード\nﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\nﾄﾞｱ脱着,脱着,1,0,8000,\n') == 'ERR',
        '「工賃のある行の 0 円」1 行だけで部品と決める')
    chk(parse('品名,部品,工賃,金額\nﾊﾞﾝﾊﾟｰ,45000,12000,57000\nﾄﾞｱ脱着,0,8000,8000\n') == (45000, 20000), '「部品」の列を読めない')
    chk(parse('品名,区分,数量,単価,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,57000\nﾄﾞｱ脱着,脱着,1,0,8000,8000\n') == (45000, 20000),
        '単価・工賃・金額（行の合計）で工賃を二重に入れる')
    chk(parse('品名,区分,数量,金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000\nﾗﾝﾌﾟ,取替,1,38000,5000\n') == 'ERR', '決められない「金額」で止めない')
    chk(parse('品名,区分,数量,部品金額,外注金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000\n') == 'ERR', '何の金額か分からない列で止めない')
    chk(parse('品名,区分,数量,単価,金額,工賃\nﾎﾞﾙﾄ,取替,2,100,,0\n') == (200, 0), '金額が空の行の 単価×数量')
    chk(parse('品名,区分,数量,部品単価,工賃\nｸﾘｯﾌﾟ,取替,10,155.5,0\n') == (1555, 0), '単価を丸めてから数量を掛けている')
    h = '品名,区分,数量,部品金額,工賃,部品コード\n'
    for bad in ('ﾊﾞﾝﾊﾟｰ,取替,1,45,500,,\n', 'ﾊﾞﾝﾊﾟｰ,取替,1,45,500,12000,\n', 'ｸﾘｯﾌﾟ,取替,10,1,550,1200,\n'):
        chk(parse(h + bad) == 'ERR', f'2 つに割れた金額の列ずれを止めない: {bad!r}')
    for bad in ('ﾊﾞﾝﾊﾟｰ,ﾌﾛﾝﾄ,取替,1,45000,0,-\n', 'ﾊﾞﾝﾊﾟｰ,ﾌﾛﾝﾄ,取替,1,45000,0,OEM\n'):
        chk(parse('品名,区分,数量,部品金額,工賃,備考\n' + bad) == 'ERR', f'品名のカンマの列ずれを品番として受ける: {bad!r}')
    it, _n = app.parse_csv_to_items('品名,区分,数量,部品金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,52119-12345\n', return_notes=True)
    chk(it and it[0]['part_no'] == '52119-12345' and it[0]['parts_amount'] == 45000, f'見出しの無い品番の列: {it}')
    it, _n = app.parse_csv_to_items('品名,区分,数量,部品金額,工賃,番号\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,52119-12345\n', return_notes=True)
    chk(it and it[0]['part_no'] == '52119-12345', f'「番号」の列の品番が消える: {it}')
    it, notes = app.parse_csv_to_items(h + 'ﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\n⚠️ 3行読み取れませんでした。\n', return_notes=True)
    chk(it and any(str(n).startswith('⚠️ CSV の中の注意') for n in notes), f'CSV の中の注意を警告にしない: {notes}')


def test_csv_round3_shapes():
    """レビュー 3 周目: 「金額」は行ごとの証拠で決める・「計」「税込金額」も明細の値で・品名の見出しが別名に無い・Markdown の太字の
    合計行・同じセル数の列ずれ・行番号の見出し・レバーレートの単価・返品の数量・AI の注意書き"""
    def parse(t):
        it, notes = app.parse_csv_to_items(t, return_notes=True)
        if not it and any(str(n).startswith('❌') for n in notes):
            return 'ERR'
        return (sum(i['parts_amount'] for i in it), sum(i['wage'] for i in it))
    # 工賃だけの行の「金額」（行の合計）を部品に入れない
    chk(parse('品名,区分,数量,単価,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,0,45000\nﾊﾞﾝﾊﾟｰ脱着,脱着,1,,12000,12000\nﾄﾞｱ,塗装,1,,25000,25000\n')
        == (45000, 37000), '工賃だけの行の行の合計を部品に入れる')
    # 行の合計なのに 単価×数量＋工賃 と合わない行（値引き・金額だけの行）は止める
    chk(parse('品名,区分,数量,単価,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,57000\n値引き,,1,,,-5000\n') == 'ERR', '行の合計と合わない値引きの行を落とす')
    # 単価より小さい「金額」（値引き後）: 工賃のある行では値引き後の部品とも値引き後の行の合計とも読めるので止める（レビュー 5 周目）。
    # 見出しが「部品金額」なら部品の金額
    chk(parse('品名,区分,数量,単価,金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,40500,12000\nﾄﾞｱ脱着,脱着,1,,,8000\n') == 'ERR',
        '工賃のある行の値引き後の「金額」を部品と決める')
    chk(parse('品名,区分,数量,単価,部品金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,40500,12000\nﾄﾞｱ脱着,脱着,1,,,8000\n') == (40500, 20000),
        '値引き後の部品金額')
    # 「計」「税込金額」「合計」の列も明細の値で決める（部品を 0 円にしない）
    for h in ('計', '税込金額', '合計'):
        chk(parse(f'品名,区分,数量,{h},工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000\nﾄﾞｱ脱着,脱着,1,0,8000\nﾄﾞｱ塗装,塗装,1,0,32000\n')
            == (45000, 52000), f'「{h}」の列の部品金額')
    chk(parse('品名,区分,数量,部品金額,工賃,合計\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,57000\n') == (45000, 12000), '行の合計の列を二重に数える')
    # 工賃の列が無い「金額」は部品と工賃を分けられない
    chk(parse('品名,数量,金額\nﾊﾞﾝﾊﾟｰ,1,45000\n') == 'ERR', '工賃の列が無い「金額」を全部部品にしている')
    # 品名の見出しが別名に無い（項目・【品名】・品名/作業・品名・作業内容）
    for h in ('項目', '【品名】', '品名/作業', '品名・作業内容'):
        chk(parse(f'| {h} | 数量 | 部品金額 | 工賃 |\n|---|---|---|---|\n| ﾊﾞﾝﾊﾟｰ | 1 | 45000 | 12000 |\n') == (45000, 12000),
            f'見出し「{h}」を位置で読んでいる')
    # Markdown の太字の合計行は明細にしない
    md = ('| 品名 | 区分 | 数量 | 部品金額 | 工賃 | 部品コード |\n|---|---|---|---|---|---|\n| ﾊﾞﾝﾊﾟｰ | 取替 | 1 | 45000 | 12000 | |\n'
          '| **合計** | | | **45,000** | **12,000** | |\n')
    chk(parse(md) == (45000, 12000), f'Markdown の太字の合計行を明細にしている: {parse(md)}')
    # 見出しと同じセル数の列ずれ（品番の欄に金額・数量の欄に「取替」）
    h6 = '品名,区分,数量,部品金額,工賃,部品コード\n'
    for bad in ('ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45,000,12000\n', 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45000,12,000\n', 'ﾌﾛﾝﾄ,ﾊﾞﾝﾊﾟｰ,取替,1,45000,0\n'):
        chk(parse(h6 + bad) == 'ERR', f'同じセル数の列ずれを止めない: {bad!r}')
    chk(parse('ﾊﾞﾝﾊﾟｰ,取替,1,45,000,0\nﾄﾞｱ脱着,脱着,1,0,8000,\n') == 'ERR', '見出しの無い CSV の列ずれを止めない')
    # 全行に同じ余分なカンマ（書き出しの癖）なら、部品 300・工賃 500 の行は正しい行。1 行だけなら見分けられないので止める
    chk(parse(h6 + 'ｸﾘｯﾌﾟ,取替,2,300,500,,\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,,\n') == (45300, 12500), '全行の余分なカンマで正しい行を止める')
    chk(parse(h6 + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45,500,,\n') == 'ERR', '1 行だけの割れた金額を止めない')
    chk(parse(h6 + 'ﾄﾞｱ脱着,脱着,1,0,500,,\n') == (0, 500), '部品 0・工賃 500 の行を止める')
    # 行番号の見出しは金額でない
    for h in ('No.', '#', 'Ｎｏ', '明細No', '行No'):
        chk(parse(f'{h},品名,区分,数量,部品金額,工賃\n1,ﾊﾞﾝﾊﾟｰ,取替,1,45000,12000\n2,ﾄﾞｱ脱着,脱着,1,0,8000\n') == (45000, 20000),
            f'見出し「{h}」で止める')
    # 単価の欄がレバーレート（単価×数量＝工賃）の行に部品を足さない
    chk(parse('品名,区分,数量,単価,部品金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,45000,0\nﾊﾞﾝﾊﾟｰ脱着,脱着,1.5,8000,,12000\n') == (45000, 12000),
        'レバーレートの行に部品を足す')
    # 返品（数量 −1 × 単価）は負の金額
    chk(parse('品名,区分,数量,部品単価,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000\n返品,,-1,5000,0\n') == (40000, 12000), '返品の符号')
    # 「⚠️」「※」の注意書き（中にカンマ）は明細にせず、取り込みも止めない
    it, notes = app.parse_csv_to_items(h6 + 'ﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\n⚠️ 次の3行が読めません: ﾎﾞﾙﾄ,ﾅｯﾄ,ｸﾘｯﾌﾟ\n※ 2ページ目は判読できませんでした。\n',
                                      return_notes=True)
    chk(len(it) == 1 and sum(1 for n in notes if str(n).startswith('⚠️ CSV の中の注意')) == 2, f'注意書きの扱い: {it} {notes}')


def test_csv_round4_shapes():
    """レビュー 4 周目: 見出しの意味が決まらない列は推し量る範囲を狭め、合わなければ止める"""
    def parse(t):
        it, notes = app.parse_csv_to_items(t, return_notes=True)
        if not it and any(str(n).startswith('❌') for n in notes):
            return 'ERR'
        return (sum(i['parts_amount'] for i in it), sum(i['wage'] for i in it))
    r2 = 'ﾊﾞﾝﾊﾟｰ,取替,1,45000,12000\nﾄﾞｱ脱着,脱着,1,0,8000\n'
    # 作業金額・技術金額は工賃。値引金額・塗装金額・外注金額などは何の金額か分からないので止める
    for h in ('作業金額', '技術金額', '作業料'):
        chk(parse(f'品名,区分,数量,部品金額,{h}\n' + r2) == (45000, 20000), f'「{h}」を工賃として読まない')
    for h in ('値引金額', '塗装金額', '外注金額'):
        chk(parse(f'品名,区分,数量,部品金額,工賃,{h}\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,4500\n') == 'ERR', f'「{h}」の列を黙って捨てる')
    # 部品単価 × 数量が工賃と同じ行の部品を落とさない（レバーレートの除外は見出しが「単価」で部品金額の列があるときだけ）
    chk(parse('品名,区分,数量,部品単価,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000\nﾌｫｸﾞﾗﾝﾌﾟ,取替,1,8000,8000\n') == (53000, 20000),
        '部品単価×数量＝工賃 の行の部品を落とす')
    chk(parse('品名,区分,数量,単価,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,57000\nﾌｫｸﾞﾗﾝﾌﾟ,取替,1,8000,8000,16000\n') == (53000, 20000),
        '行の合計と確かめた行の部品を落とす')
    # 値引き後の行の合計（単価×数量＜金額＜単価×数量＋工賃）は部品とも合計とも読めるので止める
    chk(parse('品名,区分,数量,単価,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,52500\nﾗﾝﾌﾟ,取替,1,38000,5000,39200\n') == 'ERR',
        '値引き後の行の合計を部品金額と読む')
    # 部品金額の列があるときの行の合計の列は、合わない行（外注・値引き）があれば止める。工賃の列が無い「合計」も
    chk(parse('品名,区分,数量,部品金額,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,57000\nｴｰﾐﾝｸﾞ(外注),,1,,,20000\n') == 'ERR',
        '行の合計の列だけにある外注の金額を捨てる')
    chk(parse('品名,区分,数量,部品金額,合計\nﾊﾞﾝﾊﾟｰ,取替,1,45000,57000\n') == 'ERR', '工賃の列が無い「合計」を捨てる')
    chk(parse('品名,区分,数量,部品金額,工賃,税込金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,62700\n') == (45000, 12000), '税込の行の合計で止める')
    # 全行が同じだけ割れた CSV（4 桁以上の金額が 1 つも無い）は、全行の余分なカンマの例外にしない
    chk(parse('品名,区分,数量,部品金額,工賃,部品コード\nﾊﾞﾝﾊﾟｰ,取替,1,45,500,12000,\nﾗﾝﾌﾟ,取替,1,38,200,5000,\n') == 'ERR',
        '全行が同じだけ割れた CSV を通す')
    # 区分が空欄の行の品名のカンマ（括弧が閉じない・品番の欄が 0）
    h6 = '品名,区分,数量,部品金額,工賃,部品コード\n'
    for bad in ('ｼｮｰﾄﾊﾟｰﾂ(ｸﾘｯﾌﾟ,ﾎﾞﾙﾄ),,1,1500,0\n', '写真代(事故,修理後),,1,0,3000\n'):
        chk(parse(h6 + 'ﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\n' + bad) == 'ERR', f'品名のカンマの列ずれを止めない: {bad!r}')
    chk(parse('ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45,500,,\nﾄﾞｱ脱着,脱着,1,0,8000,\n') == 'ERR', '見出しの無い CSV の割れた金額（余分なカンマ）')
    # 位置 2 の見出しの無い文字の列は数量にしない（止めない）
    chk(parse('品名,区分,作業,部品金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,脱着,45000,12000\n') == (45000, 12000), '位置 2 の文字の列を数量として読む')
    # Markdown の取り消し線は中身ごと消す
    md = '| 品名 | 区分 | 数量 | 部品金額 | 工賃 |\n|---|---|---|---|---|\n| ﾊﾞﾝﾊﾟｰ | 取替 | 1 | ~~45,000~~ 40,500 | 12000 |\n'
    chk(parse(md) == (40500, 12000), f'Markdown の取り消し線の値を足す: {parse(md)}')
    # 「⚠」で始まり金額のある行は止める。「※」で始まり金額の欄が数字だけでない行は注意書き
    chk(parse(h6 + 'ﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\n⚠️ 次の行は要確認: ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45000,12000\n') == 'ERR', '「⚠」の行の金額を明細にする')
    chk(parse(h6 + 'ﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\n※ 読めなかった行: 3行目,5行目,7行目,9行目\n') == (45000, 12000), '「※」の注意書きを明細にする')
    # 見出し全体の括弧・【】と ( ) の混ざった見出し
    chk(parse('（品名）,（数量）,（部品金額）,（工賃）\nﾊﾞﾝﾊﾟｰ,1,45000,12000\n') == (45000, 12000), '（品名）の見出しを位置で読む')
    chk(parse('品名,区分,数量,【部品金額】(円),工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000\n') == (45000, 12000), '【部品金額】(円) の見出し')
    # 単価の列が無い「金額 ＝ 工賃」の行 1 行だけでは行の合計と決めない
    chk(parse('品名,区分,数量,金額,工賃\nﾌｫｸﾞﾗﾝﾌﾟ,取替,1,8000,8000\n') == 'ERR', '1 行の 金額＝工賃 を行の合計にする')
    # 見出しの判定語は別名から（「項目,数量,部品代,技術料」を見出しと認識する）
    chk(parse('項目,数量,部品代,技術料\nﾊﾞﾝﾊﾟｰ,1,45000,12000\nﾄﾞｱ脱着,1,0,8000\n') == (45000, 20000), '別名だけの見出しを見出しと認識しない')


def test_csv_round5_shapes():
    """レビュー 5 周目: 見出しより 1 セル多い行（行末のカンマ）の品名のカンマ・工賃の列の無い「金額」・部品の証拠の狭め方・
    確かめと取り込みの部品の値・位置 2 の金額の列・見出しが空の列"""
    def parse(t):
        it, notes = app.parse_csv_to_items(t, return_notes=True)
        if not it and any(str(n).startswith('❌') for n in notes):
            return 'ERR'
        return (sum(i['parts_amount'] for i in it), sum(i['wage'] for i in it))
    h6 = '品名,区分,数量,部品金額,工賃,部品コード\n'
    ok = 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,52119-12345\nﾄﾞｱ脱着,脱着,1,0,8000,\n'
    # 区分が空欄の行（写真代・研磨・ショートパーツ）の品名のカンマ: 行末のカンマ付き（指示文どおり）・括弧なし・見出しなし
    for bad in ('写真代(事故,修理後),,1,0,3000,\n', '研磨(ﾎﾞﾝﾈｯﾄ,ﾙｰﾌ),,1,0,6000,\n', 'ｼｮｰﾄﾊﾟｰﾂ(ｸﾘｯﾌﾟ,ﾎﾞﾙﾄ),,1,1500,,\n',
                'ﾌﾛﾝﾄ,ﾘﾔ研磨,,1,0,6000\n', 'ﾌﾛﾝﾄ,ﾘﾔ研磨,,1,0,6000,\n'):
        chk(parse(h6 + ok + bad) == 'ERR', f'品名のカンマの列ずれを止めない: {bad!r}')
        chk(parse(ok + bad) == 'ERR', f'見出しの無い CSV の品名のカンマの列ずれを止めない: {bad!r}')
    chk(parse('ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\nｼｮｰﾄﾊﾟｰﾂ(ｸﾘｯﾌﾟ,ﾎﾞﾙﾄ),,1,0,500,\n') == 'ERR', '見出しの無い CSV の品番 3 桁の列ずれ')
    # 止めた理由を文言に出す
    _it, notes = app.parse_csv_to_items(h6 + ok + '写真代(事故,修理後),,1,0,3000,\n', return_notes=True)
    chk(any('括弧が次の欄で閉じて' in str(n) for n in notes), f'止めた理由が文言に無い: {notes}')
    # 正しい行は止めない: 部品コードなしを 0 と書いた行・元の見積で途中で切れた品名の括弧
    chk(parse(h6 + 'ﾄﾞｱ脱着,脱着,1,0,8000,0\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\n') == (45000, 20000), '品番の欄の 0 で止める')
    chk(parse(h6 + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ(ﾌﾟﾗｲﾏｰ,取替,1,45000,12000,\n') == (45000, 12000), '途中で切れた品名の括弧で止める')
    # 工賃の列が無い・工賃が 1 つも無い「金額」は部品と工賃を分けられない
    chk(parse('品名,区分,数量,単価,金額\nﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45000,45000\nﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ脱着,脱着,1,,12000\n'
              'ﾍｯﾄﾞﾗﾝﾌﾟ,取替,1,38000,38000\n') == 'ERR', '工賃の列が無い「金額」の工賃の行を部品にする')
    chk(parse('| 品名 | 区分 | 単価 | 金額 |\n|---|---|---|---|\n| ﾊﾞﾝﾊﾟｰ | 取替 | 45000 | 45000 |\n'
              '| ﾊﾞﾝﾊﾟｰ脱着 | 工賃 | | 20000 |\n') == 'ERR', 'Markdown の工賃の列が無い「金額」')
    chk(parse('品名,区分,数量,単価,金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,45000,0\nﾄﾞｱ脱着,脱着,1,,8000,0\n') == 'ERR', '工賃が 1 つも無い「金額」')
    # 部品だけの証拠を狭める: 工賃より小さい行の合計（工賃の値引き）・単価×数量＝工賃（レバーレート）の行・値引後金額は止める
    chk(parse('品名,区分,数量,金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,57000,12000\nﾗﾝﾌﾟ,取替,1,38000,\nｴｰﾐﾝｸﾞ,調整,1,7200,8000\n') == 'ERR',
        '値引き後の行の合計（工賃より小さい）を部品にする')
    chk(parse('品名,区分,数量,単価,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,0,45000\nﾄﾞｱ脱着,脱着,1.5,8000,12000,12000\n') == 'ERR',
        'レバーレートの行の金額を部品にする')
    chk(parse('品名,区分,数量,値引後金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,40500,12000\nﾄﾞｱ脱着,脱着,1,0,8000\n') == 'ERR', '「値引後金額」を明細の値で決める')
    # 部品の証拠のある「金額」は読む（工賃のある行の 0 円・単価×数量 そのもの）
    chk(parse('品名,区分,数量,単価,金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,45000,12000\nﾄﾞｱ脱着,脱着,1,,0,8000\n') == (45000, 20000),
        '部品の証拠のある「金額」で止める')
    # 確かめと取り込みで部品の値を同じに（部品金額が空の行の 単価×数量）
    chk(parse('品名,区分,数量,単価,部品金額,工賃,金額\nﾌﾛﾝﾄｶﾞﾗｽ(持込),取替,1,85000,,15000,15000\n') == 'ERR',
        '確かめを通った行に 単価×数量 を足す')
    chk(parse('品名,区分,数量,部品単価,部品金額,工賃,合計\nﾎﾞﾙﾄ,取替,4,100,,0,400\nﾄﾞｱ脱着,脱着,1,,,8000,8000\n') == (400, 8000),
        '部品金額が空の行の 単価×数量 を確かめで 0 とみて止める')
    # レバーレートの時間が「工数」の列にある形（単価×工数＝工賃）は部品を足さない。合わなければ部品の単価として足す
    chk(parse('品名,区分,数量,工数,単価,部品金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,,45000,45000,\nﾊﾞﾝﾊﾟｰ脱着,脱着,1,1.5,8000,,12000\n') == (45000, 12000),
        '工数の列の時間で決まるレバーレートの行に部品を足す')
    chk(parse('品名,区分,数量,単価,部品金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45000,,12000\n') == (45000, 12000), '部品金額が空の行の 単価×数量 を足さない')
    # 位置 2 の別名に無い金額の列は止める（作業代などは工賃の別名）
    chk(parse('品名,区分,作業代,部品代\nﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,12000,45000\nﾄﾞｱ,脱着,8000,0\n') == (45000, 20000), '位置 2 の「作業代」を捨てる')
    for h in ('作業費', '技術費', '工賃代', '技術料金'):
        chk(parse(f'品名,区分,{h},部品金額\nﾊﾞﾝﾊﾟｰ,取替,12000,45000\n') == (45000, 12000), f'「{h}」を工賃として読まない')
    for h, v in (('値引', '-4500'), ('値引', '-500'), ('外注費', '20000')):
        chk(parse(f'品名,区分,{h},部品金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,{v},45000,12000\n') == 'ERR', f'位置 2 の「{h}」の金額 {v} を捨てる')
    # 数量らしい見出し（QTY・使用数）の列は、注意書きの行が混ざっても数量として読む
    for h in ('QTY', '使用数'):
        chk(parse(f'品名,区分,{h},部品単価,工賃\nﾎﾞﾙﾄ,取替,10,150,0\nﾄﾞｱ脱着,脱着,1,,8000\n※ 注意,次の行は,要確認\n') == (1500, 8000),
            f'注意書きの行で「{h}」の列を数量として読まない')
    # 行の合計らしい見出し（税抜合計・請求金額など）は明細の値で確かめる
    for h in ('税抜合計', '請求金額', '小計金額'):
        chk(parse(f'品名,区分,数量,部品金額,工賃,{h}\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,57000\n') == (45000, 12000), f'「{h}」の列で止める')
    _it, notes = app.parse_csv_to_items('品名,区分,数量,部品金額,工賃,外注\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,20000\n', return_notes=True)
    chk(any('合計−工賃' in str(n) for n in notes), f'何の金額か分からない列の案内に行の合計の直し方が無い: {notes}')
    # 「部品No.」は品番
    it, _n = app.parse_csv_to_items('品名,区分,部品No.,数量,部品金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,52119-12345,1,45000,12000\n', return_notes=True)
    chk(it and it[0]['part_no'] == '52119-12345' and it[0]['parts_amount'] == 45000, f'「部品No.」の品番: {it}')
    # 見出しが空の列の金額（品名より右）は止める。品番らしい値・0 だけなら止めない
    chk(parse('品名,区分,数量,部品金額,工賃,\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,3000\n') == 'ERR', '見出しが空の列の金額を品番にする')
    chk(parse('品名,区分,数量,部品金額,工賃,\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,52119-12345\nﾄﾞｱ脱着,脱着,1,0,8000,0\n') == (45000, 20000),
        '見出しが空の列の品番で止める')


def test_csv_round6_shapes():
    """レビュー 6 周目: 部品金額の列が無い CSV のレバーレート・部品だけの証拠は 2 行・止めすぎ（品番の数字・数量の空欄・
    案内の堂々巡り）・見出しの無い列（行番号・位置 2 の金額）"""
    def parse(t):
        it, notes = app.parse_csv_to_items(t, return_notes=True)
        if not it and any(str(n).startswith('❌') for n in notes):
            return 'ERR'
        return (sum(i['parts_amount'] for i in it), sum(i['wage'] for i in it))
    # 部品金額の列が無い CSV で 単価×数量＝工賃 の行（レバーレート）: 単価を部品にしない。そう読んだことを知らせる
    lv = ('品名,区分,数量,単価,工賃,部品コード\nﾊﾞﾝﾊﾟ脱着,脱着,1.5,8000,12000,\nﾎﾞﾝﾈｯﾄ鈑金,鈑金,3.0,8000,24000,\n'
          'ﾊﾞﾝﾊﾟ,取替,1,45000,0,52119-12345\n')
    chk(parse(lv) == (45000, 36000), f'レバーレートの単価を部品にする（部品金額の列なし）: {parse(lv)}')
    _it, notes = app.parse_csv_to_items(lv, return_notes=True)
    chk(any('レバーレート' in str(n) for n in notes), f'レバーレートとして読んだことを知らせない: {notes}')
    chk(parse('品名,区分,数量,工数,単価,工賃\nﾊﾞﾝﾊﾟ脱着,脱着,1,1.5,8000,12000\nﾊﾞﾝﾊﾟ,取替,1,,45000,0\n') == (45000, 12000),
        '工数の列の時間で決まるレバーレート（部品金額の列なし）')
    # 行の合計の列で 単価×数量＋工賃 と裏が取れていれば、単価は部品の単価（止めない・0 円にしない）
    chk(parse('品名,区分,数量,単価,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,57000\nﾌｫｸﾞﾗﾝﾌﾟ,取替,1,8000,8000,16000\n') == (53000, 20000),
        '行の合計で裏が取れる単価を 0 円にする')
    # 止めすぎない: 品番が 1〜3 桁の数字・数量が空欄（CSV のどの行にも数量が無い）・部品 300／工賃 500 と社内品番
    chk(parse('品名,区分,数量,部品金額,工賃,部品コード\nﾄﾞｱﾊﾟﾈﾙ,取替,1,51000,9000,678\n') == (51000, 9000), '品番 3 桁の正しい行を止める')
    chk(parse('品名,区分,数量,部品金額,工賃,部品コード\nﾜｯｼｬｰ,取替,,80,0,\nﾊﾞﾝﾊﾟｰ,取替,,45000,8000,12345\n') == (45080, 8000),
        '数量がどの行にも無い CSV を止める')
    chk(parse('品名,区分,数量,部品金額,工賃,部品コード\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\nｸﾘｯﾌﾟ,取替,10,300,500,12345\n') == (45300, 12500),
        '部品 300・工賃 500・社内品番の正しい行を止める')
    # ただし桁区切りを使っている CSV（4 桁以上の金額が無い）では、割れた形として止める
    chk(parse('品名,区分,数量,部品金額,工賃,部品コード\nﾊﾞﾝﾊﾟｰ,取替,1,45,500,12000\n') == 'ERR', '桁区切りで割れた金額を止めない')
    # ほかの行に数量があるのに空欄の行は、これまでどおり列ずれとして止める
    chk(parse('品名,区分,数量,部品金額,工賃,部品コード\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\nﾌﾛﾝﾄ,ﾘﾔ研磨,,1,0,6000\n') == 'ERR',
        'ほかの行に数量がある CSV の、数量が空欄の列ずれ')
    # 「部品だけ」の証拠が「工賃のある行の 0 円」1 行だけなら決めない（行の合計の列に混ざりうる）
    chk(parse('品名,区分,数量,工賃,金額,部品コード\nﾊﾞﾝﾊﾟ,取替,1,8000,53000,52119-12345\nﾄﾞｱ,取替,1,5000,35000,\n'
              'ｻｰﾋﾞｽ作業,,1,3000,0,\n') == 'ERR', '0 円の行 1 行で「部品」と決める（工賃の二重計上）')
    # 見出しの無い列: 品名より左は行番号なら通し、金額なら止める。位置 2 の見出しが空で 3 桁の金額は数量にしない
    chk(parse(',品名,区分,数量,部品金額,工賃\n1,ﾊﾞﾝﾊﾟ,取替,1,45000,8000\n2,ﾄﾞｱ脱着,脱着,1,0,8000\n') == (45000, 16000),
        '見出しの無い行番号の列で止める')
    chk(parse(',品名,区分,数量,部品金額,工賃\n1200,ﾊﾞﾝﾊﾟ,取替,1,45000,8000\n') == 'ERR', '品名より左の見出し無しの列の金額を捨てる')
    chk(parse('品名,区分,,部品金額,工賃,部品コード\nﾊﾞﾝﾊﾟ,取替,500,45000,8000,\nﾄﾞｱ,取替,300,30000,5000,\n') == 'ERR',
        '位置 2 の見出しが空の 3 桁の金額を数量にする')
    chk(parse('品名,区分,,部品金額,工賃,部品コード\nｸﾘｯﾌﾟ,取替,10,1500,0,\nﾊﾞﾝﾊﾟ,取替,1,45000,8000,\n') == (46500, 8000),
        '位置 2 の見出しが空の数量を読まない')


def test_step34_marks_and_report():
    """③の判定・ファイル名・差異レポートが同じ事実を指す（レビュー 6 周目）: 逆算一致でも総額相違は出す・ショートパーツが
    印字の工賃計に入る見積・レポートの判定は③に従う・印字の無い小計は「未照合」で差額を出さない"""
    cust = {'car_name': 'ﾃｽﾄ車'}
    n = app.generate_filename(cust, 50000, 10000, 50000, 10000, True, reverse_match=True, grand_ok=False)
    chk('総額相違' in n, f'逆算一致の見積で総額相違がファイル名に出ない: {n}')
    # ショートパーツの逃げ道は③の判定（wage_ok）に入っている。ファイル名は③の判定だけを見る（レビュー 8 周目）
    n2 = app.generate_filename(cust, 50000, 10000, 50000, 12000, True, short_parts_wage=2000, wage_ok=True)
    chk('工賃相違' not in n2, f'③が一致とした工賃を相違にする: {n2}')
    n2b = app.generate_filename(cust, 50000, 10000, 50000, 12000, True, short_parts_wage=2000)
    chk('工賃相違' in n2b, f'③が相違とした工賃（ショートパーツが両側に当たる形）に印が付かない: {n2b}')
    n3 = app.generate_filename(cust, 50000, 10000, 50000, 12000, True)
    chk('工賃相違' in n3, f'工賃相違をファイル名に出さない: {n3}')
    import pymupdf

    def rows(cp, cw, pp, pw, v):
        pdf = app.generate_beta_discrepancy_report_pdf({'items': []}, cp, cw, pp, pw, cust, verdict=v)
        doc = pymupdf.open(stream=pdf, filetype='pdf')
        out = {}
        for w in doc[0].get_text('words'):
            out.setdefault(round(w[1]), []).append((w[0], w[4]))
        return [' '.join(t for _, t in sorted(v_)) for _, v_ in sorted(out.items())]
    r = rows(50000, 10000, 50000, 11000, {'parts_ok': True, 'wage_ok': False, 'grand_ok': True})
    chk(any('工賃合計' in x and '相違' in x for x in r), f'③が相違とした工賃をレポートが「一致」と書く: {r}')
    r2 = rows(45000, 12000, 0, 12000, {'parts_ok': True, 'wage_ok': True, 'grand_ok': True})
    chk(any('部品合計' in x and '未照合' in x and '—' in x for x in r2), f'印字の無い小計の判定・差額: {r2}')
    # 差額が 9 桁（0 を 1 つ多く打った額）でも、隣の欄の文字と重ならない
    pdf = app.generate_beta_discrepancy_report_pdf(
        {'items': []}, 999999999, 10000, 1, 10000,
        cust, verdict={'parts_ok': False, 'wage_ok': True, 'grand_ok': True})
    doc = pymupdf.open(stream=pdf, filetype='pdf')
    line = {}
    for w in doc[0].get_text('words'):
        line.setdefault(round(w[1]), []).append(w)
    bad = []
    for _y, ws in line.items():
        ws = sorted(ws, key=lambda w: w[0])
        bad += [(a[4], b[4]) for a, b in zip(ws, ws[1:]) if a[2] > b[0] + 0.5]
    chk(not bad, f'差異レポートの欄の文字が重なる: {bad}')


def test_csv_round7_shapes():
    """レビュー 7 周目: 6 周目の直しが開けた穴（行の合計の旗・桁区切りの目印・数量の有無）と、見出しの無い行番号の列"""
    def parse(t):
        it, notes = app.parse_csv_to_items(t, return_notes=True)
        if not it and any(str(n).startswith('❌') for n in notes):
            return 'ERR'
        return (sum(i['parts_amount'] for i in it), sum(i['wage'] for i in it))
    # 行の合計で裏が取れても、金額の欄が空の行は裏が取れていない（レバーレートの単価を部品にしない）
    lv = ('品名,区分,数量,単価,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,57000\nﾄﾞｱ脱着,脱着,1,8000,8000,\n'
          'ﾎﾞﾝﾈｯﾄ脱着,脱着,1,8000,8000,\nﾌｪﾝﾀﾞ脱着,脱着,1,8000,8000,\n')
    chk(parse(lv) == (45000, 36000), f'金額の欄が空のレバーレートの行の単価を部品にする: {parse(lv)}')
    _it, notes = app.parse_csv_to_items(lv, return_notes=True)
    chk(any('レバーレート' in str(n) for n in notes), f'レバーレートとして読んだことを知らせない: {notes}')
    chk(parse('品名,区分,数量,単価,工賃,金額\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,57000\nﾌｫｸﾞﾗﾝﾌﾟ,取替,1,8000,8000,16000\n') == (53000, 20000),
        '全行で裏が取れた単価を 0 円にする')
    # 桁区切りの目印は明細の行だけ・2 セル以上（合計行や 1 セルで割れた金額を通さない）
    chk(parse('品名,区分,数量,部品金額,工賃,部品コード\nﾊﾞﾝﾊﾟｰ,取替,1,45,500,12000\nﾄﾞｱ脱着,脱着,1,0,8000,\n') == 'ERR',
        '1 セルの 4 桁で割れた金額を通す')
    _t = ('品名,区分,数量,部品金額,工賃,部品コード\nﾊﾞﾝﾊﾟｰ,取替,1,45000,12000,\nﾄﾞｱ脱着,脱着,1,0,8000,\n'
          'ｸﾘｯﾌﾟ,取替,10,300,500,12345\n')
    chk(parse(_t) == (45300, 20500), f'桁区切りを使っていない CSV の小物の行を止める: {parse(_t)}')
    _it, notes = app.parse_csv_to_items(_t, return_notes=True)
    chk(any('割れた行と同じ形' in str(n) for n in notes), f'割れた行と同じ形の行を知らせない: {notes}')
    # どの行にも数量が無い CSV でも、部品も工賃も 0 で品番の欄だけに数字がある行は止める
    chk(parse('品名,区分,数量,部品金額,工賃,部品コード\nﾜｯｼｬｰ,取替,,80,0,\nﾌﾛﾝﾄ,ﾘﾔ研磨,,,0,6000\n') == 'ERR',
        '数量がどの行にも無い CSV の品名のカンマを通す')
    chk(parse('品名,区分,数量,部品金額,工賃,部品コード\nﾜｯｼｬｰ,取替,,80,0,\nﾊﾞﾝﾊﾟｰ,取替,,45000,8000,12345\n') == (45080, 8000),
        '数量がどの行にも無い正しい CSV を止める')
    # 見出しの無い列: 行番号（1 か 2 から 1 ずつ）だけ読み飛ばす
    chk(parse(',品名,区分,数量,部品金額,工賃\n1,ﾊﾞﾝﾊﾟ,取替,1,45000,8000\n2,ﾄﾞｱ,取替,1,30000,5000\n') == (75000, 13000), '行番号の列で止める')
    for bad in ('100,ﾊﾞﾝﾊﾟ,取替,1,45000,8000\n300,ﾄﾞｱ,取替,1,30000,5000\n', '500,ﾊﾞﾝﾊﾟ,取替,1,45000,8000\n'):
        chk(parse(',品名,区分,数量,部品金額,工賃\n' + bad) == 'ERR', f'見出しの無い列の金額を行番号として捨てる: {bad!r}')


def test_step34_sp_and_report_labels():
    """レビュー 7 周目: ショートパーツで一致とした小計は、総額を照合していないとき確認を求める・判定の書き分け"""
    cust = {'car_name': 'ﾃｽﾄ車'}
    import pymupdf

    def rows(cp, cw, pp, pw, v):
        pdf = app.generate_beta_discrepancy_report_pdf({'items': []}, cp, cw, pp, pw, cust, verdict=v)
        doc = pymupdf.open(stream=pdf, filetype='pdf')
        out, over = {}, []
        for w in doc[0].get_text('words'):
            out.setdefault(round(w[1]), []).append((w[0], w[4]))
            if w[2] > 524.2 and 120 < w[1] < 210:
                over.append(w[4])
        return [' '.join(t for _, t in sorted(v_)) for _, v_ in sorted(out.items())], over
    r, over = rows(50000, 10000, 52000, 10000, {'parts_ok': True, 'wage_ok': True, 'parts_sp': True, 'grand_ok': True})
    chk(any('部品合計' in x and 'ｼｮｰﾄﾊﾟｰﾂ' in x for x in r), f'ショートパーツ込みの一致を書き分けない: {r}')
    chk(not over, f'判定の欄が表からはみ出す: {over}')
    r2, _ = rows(50000, 10000, 52000, 10000, {'parts_ok': True, 'wage_ok': True, 'rev': True, 'grand_ok': True})
    chk(any('部品合計' in x and '逆算' in x for x in r2), f'逆算一致を書き分けない: {r2}')
    # 判定が無い（verdict を渡さない）ときに、ぴったり一致の行を「相違」と書かない
    r3, _ = rows(50000, 10000, 50000, 10000, {})
    chk(any('部品合計' in x and '一致' in x for x in r3) and not any('相違' in x for x in r3),
        f'判定が無いときに一致の行を「相違」と書く: {r3}')

def test_csv_round8_shapes():
    """レビュー 8 周目: 行の合計で裏が取れるのは、その欄に金額が書いてある行だけ（ほかの行が空でも巻き込まない）"""
    def parse(t):
        it, notes = app.parse_csv_to_items(t, return_notes=True)
        if not it and any(str(n).startswith('❌') for n in notes):
            return 'ERR'
        return (sum(i['parts_amount'] for i in it), sum(i['wage'] for i in it))
    t = ('品名,数量,単価,工賃,金額\n'
         'ﾊﾞﾝﾊﾟｰ,1,30000,0,30000\n'
         'ﾊﾞﾝﾊﾟｰ脱着,1.5,8000,12000,24000\n'      # 24,000＝8,000×1.5＋12,000 で裏が取れている
         'ﾊﾞﾝﾊﾟｰ塗装,1,9000,9000,\n'               # 金額の欄が空（レバーレートとみて部品 0 円＋警告）
         'ﾎﾞﾝﾈｯﾄ塗装,2,5000,20000,30000\n')
    chk(parse(t) == (52000, 41000), f'金額の欄が空の行が、裏の取れている行まで巻き込む: {parse(t)}')
    _it, notes = app.parse_csv_to_items(t, return_notes=True)
    chk(any('レバーレート' in str(n) and 'ﾊﾞﾝﾊﾟｰ塗装' in str(n) for n in notes), f'詰まっている行を名指ししない: {notes}')
    # 全行に末尾のカンマがある CSV でも、割れた行と同じ形の行は知らせる
    t2 = ('品名,区分,数量,部品金額,工賃,部品コード\nﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45000,0,,\nﾊﾞﾝﾊﾟｰ脱着,脱着,1,0,12000,,\n'
          'ｸﾘｯﾌﾟ,取替,1,45,500,0,\n')
    _it2, notes2 = app.parse_csv_to_items(t2, return_notes=True)
    chk(any('割れた行と同じ形' in str(n) for n in notes2), f'末尾カンマの CSV で割れた形を知らせない: {notes2}')
    # 行番号の列は抜けがあってもよい（1,2,4）
    chk(parse(',品名,区分,数量,部品金額,工賃\n1,ﾊﾞﾝﾊﾟ,取替,1,45000,0\n2,ﾄﾞｱ,取替,1,30000,0\n4,ﾎﾞﾝﾈｯﾄ,取替,1,20000,0\n')
        == (95000, 0), '抜けのある行番号の列で止める')


def test_csv_round10_shapes():
    """レビュー 10 周目（最終確認）: 割れた形の行は必ず止めるか知らせる・桁区切りを「.」で書いた金額・知らせは取り込む行だけ"""
    def parse(t):
        it, notes = app.parse_csv_to_items(t, return_notes=True)
        if not it and any(str(n).startswith('❌') for n in notes):
            return 'ERR'
        return (sum(i['parts_amount'] for i in it), sum(i['wage'] for i in it))
    h = '品名,区分,数量,部品金額,工賃,部品コード\nﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ,取替,1,45000,0,52119-12345\n'
    # 品番の欄が空の割れた形（「1,500」）は止められない（品番の無い小物の正しい行と区別できない）が、必ず知らせる
    for bad in ('ﾎﾞﾙﾄ,取替,1,1,500\n', 'ﾎﾞﾙﾄ,取替,1,1,500,\n'):
        _it, notes = app.parse_csv_to_items(h + bad, return_notes=True)
        chk(any('割れた行と同じ形' in str(n) for n in notes), f'割れた形の行を黙って通す: {bad!r} {notes}')
    # 品番の欄に本当の工賃が来ている形は、これまでどおり止める
    chk(parse(h + 'ﾎﾞﾙﾄ,取替,1,1,500,12000\n') == 'ERR', '品番の欄に工賃が来た割れた行を止めない')
    # 桁区切りを「.」で書いた金額は止める（単価の小数は正しい値として読む）
    chk(parse('品名,区分,数量,部品金額,工賃\nﾊﾞﾝﾊﾟｰ,取替,1,45.000,0\n') == 'ERR', '「45.000」を 45 円として読む')
    chk(parse('品名,区分,数量,部品単価,工賃\nｸﾘｯﾌﾟ,取替,10,155.5,0\nﾄﾞｱ脱着,脱着,1,,8000\n') == (1555, 8000), '単価の小数を止める')
    # 小数の数量の知らせは取り込む行だけ（集計行に出さない）
    _it, notes = app.parse_csv_to_items(h + '合計,,2.5,45000,0,\n', return_notes=True)
    chk(not any('数量' in str(n) and '合計' in str(n) for n in notes), f'取り込まない集計行を数量の知らせに出す: {notes}')


def test_vendor_env_isolation():
    """子プロセスに実案件の NEO_check は読ませない（空の置き場）。見積ガイドラインは場所だけ渡す。本番は Secrets の JSON から（Q10）"""
    from neo_skill import vendor as v
    saved = {k: os.environ.get(k) for k in ('PDF_TO_NEO_GUIDELINE', 'PDF_TO_NEO_GUIDELINE_JSON', 'NEO_SKILL_PROFILE', 'NEO_CORPUS_ROOT')}
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ['NEO_CORPUS_ROOT'] = r'C:\somewhere\corpus'
        os.environ['PDF_TO_NEO_GUIDELINE_JSON'] = '{"material_rate": {"bands": [6500], "default_band": 6500}}'
        e = v.subprocess_env('C:/Addata')
        chk(e.get('NEO_CHECK_ROOT', '').endswith('neo_skill_empty_check'), f"Q10: 子の NEO_CHECK_ROOT が空の置き場でない: {e.get('NEO_CHECK_ROOT')}")
        chk('NEO_CORPUS_ROOT' not in e, 'Q10: 過去 NEO の索引の場所を子に渡している')
        chk('PDF_TO_NEO_GUIDELINE_JSON' not in e and os.path.isfile(e.get('PDF_TO_NEO_GUIDELINE', '')),
            f"Q10: ガイドラインを場所で渡していない: {e.get('PDF_TO_NEO_GUIDELINE')}")
        chk(e.get('ADDATA_ROOT_PARTIAL') == '1', 'Q1: アプリの Addata に部分 Addata の旗が無い')
    finally:
        for k, val in saved.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val


def test_manual_flags_and_unit_price():
    tpl = open(os.path.join(ROOT, 'template_toyota.neo'), 'rb').read()
    items = [{'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'quantity': 1, 'parts_amount': 0, 'wage': 8000},
             {'name': 'ｸﾘｯﾌﾟ', 'method': '取替', 'quantity': 10, 'parts_amount': 1550, 'wage': 0}]
    neo, *_ = app.generate_neo_file(tpl, {}, items, 0, {})
    ck = app.find_real_cks(neo)
    f = app.extract_files(app.decompress_neo(neo, ck), app.parse_entries(neo, ck[0])[1])
    c = sqlite3.connect(':memory:')
    c.deserialize(f['AnSMB.txt'])
    rows = c.execute('SELECT PartsName, PartsPriceByManual, WageByManual, PartsUnitPriceOutTax, PartsUnitPriceTax, PartsUnitPriceInTax, PartsPriceTax '
                     'FROM ERParts ORDER BY LineNo').fetchall()
    c.close()
    chk(rows[0][1] == '' and rows[0][2] == '*', f'L11: 部品代の無い手入力行の印: {rows[0]}')
    chk(rows[1][1] == '*' and rows[1][2] == '', f'L11: 工賃の無い手入力行の印: {rows[1]}')
    chk(rows[1][3:6] == (155, 15, 170) and rows[1][6] == 160, f'L11: 数量行の単価欄（155・税 15 切り捨て・行の税 160）: {rows[1]}')


def _erparts(neo, cols):
    ck = app.find_real_cks(neo)
    f = app.extract_files(app.decompress_neo(neo, ck), app.parse_entries(neo, ck[0])[1])
    c = sqlite3.connect(':memory:')
    c.deserialize(f['AnSMB.txt'])
    try:
        return c.execute(f'SELECT {cols} FROM ERParts ORDER BY LineNo').fetchall()
    finally:
        c.close()


def test_csv_blank_method_as_printed():
    """CSV の区分が空欄の行は、原本どおり区分なし（DisposalCode -1）で NEO に入れる（2026-09-21 §2-3 ②）。
    工場ソフトの CSV で区分が本当に空の行を「取替」にしていた。ベタ打ち・プレビュー取り込み（既定）は今までどおり推し量る。
    両方向: CSV は推し量らない／既定は推し量る／区分の書いてある行は変わらない"""
    tpl = open(os.path.join(ROOT, 'template_toyota.neo'), 'rb').read()
    h = '品名,区分,数量,部品金額,工賃,部品コード\n'
    it, notes = app.parse_csv_to_items(h + 'ｸﾘｯﾌﾟ,,1,300,0,\nﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,0,\nﾎﾞﾃﾞｰ研磨,,1,0,3000,\n'
                                       'ｺｰﾃｨﾝｸﾞ,,1,0,5000,\n', return_notes=True)
    chk([x['method'] for x in it] == ['', '取替', '', ''], f'CSV の区分を取り込みで変えている: {[x["method"] for x in it]}')
    _bn = [n for n in notes if '区分が空欄の行' in str(n)]
    chk(len(_bn) == 1 and str(_bn[0]).startswith('⚠️') and '「ｸﾘｯﾌﾟ」' in _bn[0] and '「取替」' in _bn[0],
        f'推し量れば区分が付く空欄の行を知らせていない: {notes}')
    # 推し量っても区分なしの行（研磨・区分の語の無い工賃行）と、区分の書いてある行は知らせない（知らせが増えて埋もれる）
    chk(_bn and 'ﾎﾞﾃﾞｰ研磨' not in _bn[0] and 'ｺｰﾃｨﾝｸﾞ' not in _bn[0] and 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ' not in _bn[0],
        f'区分が変わらない行まで知らせている: {_bn}')
    _it2, notes2 = app.parse_csv_to_items(h + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,0,\nﾎﾞﾃﾞｰ研磨,,1,0,3000,\n', return_notes=True)
    chk(not any('区分が空欄の行' in str(n) for n in notes2), f'区分の変わらない CSV に空欄の知らせが出る: {notes2}')
    # NEO: CSV（infer_method=False）は空欄のまま -1、既定（ベタ打ち・プレビュー取り込み）は今までどおり取替 0
    rows = [{k: v for k, v in x.items()} for x in it]
    neo_csv, *_ = app.generate_neo_file(tpl, {}, rows, 0, {}, infer_method=False)
    got = _erparts(neo_csv, 'PartsName, DisposalCode, DisposalName')
    chk([(r[1], r[2]) for r in got] == [(-1, ''), (0, '取替'), (-1, ''), (-1, '')],
        f'CSV の区分が空欄の行を推し量っている（原本どおり区分なしにする）: {got}')
    neo_def, *_ = app.generate_neo_file(tpl, {}, rows, 0, {})
    got2 = _erparts(neo_def, 'PartsName, DisposalCode, DisposalName')
    chk([(r[1], r[2]) for r in got2][:2] == [(0, '取替'), (0, '取替')],
        f'ベタ打ち・プレビュー取り込み（既定）の推し量りが変わった: {got2}')
    # 推し量りの中身は元のまま（関数に切り出しただけ）
    for nm, pa, wg, want in (('ｸﾘｯﾌﾟ', 300, 0, '取替'), ('ﾌﾛﾝﾄﾄﾞｱ脱着修理', 0, 9000, '脱着修理'), ('ｼｮｰﾄﾊﾟｰﾂ', 800, 0, ''),
                             ('ﾎﾞﾃﾞｰ磨き調整', 0, 5000, '磨き調整'), ('光軸調整', 0, 3000, '調整'), ('※金額調整', 500, 0, ''),
                             ('ﾍﾟｲﾝﾄ', 0, 9000, '塗装'), ('ﾄﾞｱ脱着板金', 0, 12000, '脱着板金'), ('ﾙｰﾌ', 0, 12000, ''),
                             ('写真代', 3000, 0, ''), ('ﾊﾞﾝﾊﾟ分解', 0, 2000, '分解調整'), ('ﾌﾛﾝﾄﾊﾟﾈﾙ 鈑金', 0, 20000, '鈑金')):
        g = app._infer_method_from_name({'name': nm, 'parts_amount': pa, 'wage': wg})
        chk(g == want, f'推し量りが変わった: {nm} 部品 {pa} 工賃 {wg} → {g!r}（元は {want!r}）')
    # ステップ④は推し量らない（区分は③の表のとおり）。プレビュー取り込みは取り込むときに推し量った区分を表に入れる
    # （④で推し量ると③で区分を消した行に区分が入った。バグハント第 3 弾 B3）
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    # ④の生成の呼び出し（`neo_data, …, grand_total = generate_neo_file(...)`）そのものを構文木で見る
    # （文字列がどこかにあるだけで通る検査にしない。Codex 講評 第 3 弾 1 周目）
    import ast as _ast
    _calls = [n.value for n in _ast.walk(_ast.parse(src))
              if isinstance(n, _ast.Assign) and isinstance(n.value, _ast.Call)
              and getattr(n.value.func, 'id', '') == 'generate_neo_file'
              and any(isinstance(t, _ast.Tuple) and any(getattr(e, 'id', '') == 'grand_total' for e in t.elts) for t in n.targets)]
    _kw = [k for c in _calls for k in c.keywords if k.arg == 'infer_method']
    chk(len(_calls) == 1 and len(_kw) == 1 and isinstance(_kw[0].value, _ast.Constant) and _kw[0].value.value is False,
        f'ステップ④が推し量りを止めていない（③で消した区分が NEO に入る）: 呼び出し {len(_calls)} 件 / infer_method '
        f'{[_ast.unparse(k.value) for k in _kw]}')
    chk("_pv_m = _infer_method_from_name(_pv_it)" in src and "st.session_state['csv_items'] = _p2n_items_pv" in src,
        'プレビュー取り込みで、推し量った区分を③の表に入れていない（ベタ打ちの NEO と区分が変わる）')


def test_csv_ai_diff_marks():
    """CSV の後ろに AI が書いた「部品相違○円 工賃相違●円」は、ステップ③の確認とファイル名の印になる（2026-09-21 §2-3 ③）。
    両方向: 相違のある申告・額の読めない申告は印にする／0 円・「なし」は印にしない"""
    m = app._csv_ai_diff_marks
    chk(m(['部品相違1,200円 工賃相違0円']) == ['部品相違'], f'部品だけの相違: {m(["部品相違1,200円 工賃相違0円"])}')
    chk(m(['部品相違0円 工賃相違３，５００円']) == ['工賃相違'], '全角の額の工賃相違を読めていない')
    chk(m(['部品相違-1,200円', '工賃相違▲500円']) == ['部品相違', '工賃相違'], 'マイナスの相違を印にしていない')
    chk(m(['部品相違〇,〇〇〇円 工賃相違●,●●●円']) == ['部品相違', '工賃相違'], '額の読めない申告を相違なしにしている')
    chk(m(['部品相違0円 工賃相違0円']) == [] and m(['部品相違なし 工賃相違無し']) == [] and m([]) == [],
        '相違 0 円・なしを印にしている')
    chk(m(['部品相違']) == ['部品相違'], '額の無い「部品相違」を印にしていない')
    # 区切りごとに見る（Codex 2 周目 P1）: 「なし」と「額の読めない相違」が混ざっても、読めない側は印にする
    chk(m(['部品相違なし 工賃相違〇,〇〇〇円']) == ['工賃相違'], f'「なし」混じりの読めない相違を見逃す: {m(["部品相違なし 工賃相違〇,〇〇〇円"])}')
    chk(m(['部品相違1,200円 工賃相違なし']) == ['部品相違'], '「なし」混じりの相違の額を読めていない')
    # つなぎの語だけの区切りは次の区切りと同じ判定（「A・B はありません」「A、B ともに 0 円」「A・B 各 1,000 円」）
    chk(m(['部品相違・工賃相違はありません（一致）']) == [], '「部品相違・工賃相違はありません」を相違にしている')
    chk(m(['部品相違、工賃相違ともに0円']) == [], '「ともに 0 円」を相違にしている')
    chk(m(['部品相違・工賃相違 各1,000円']) == ['部品相違', '工賃相違'], '「各 1,000 円」の片方を見逃す')
    chk(m(['部品相違あり']) == ['部品相違'] and m(['部品相違（1,200円）']) == ['部品相違'], '「あり」・括弧書きの額を相違にしていない')
    # 区切りの残りが丸ごと言い切りの相違なし（なし・ありません・一致・0 円。「です」・数字の無い括弧書きは付いてよい）なら印なし
    for clean in ('部品相違: 一致です', '部品相違は ありません', '部品相違（0円）', '部品相違なし、工賃相違なし', '部品相違なし（一致）',
                  '部品相違 0円 / 工賃相違 0円（照合済み）', '部品相違：なし。工賃相違：なし．', '部品相違ありませんでした'):
        chk(m([clean]) == [], f'言い切りの相違なしを相違にしている: {clean} → {m([clean])}')
    # 逆向き（Codex 4・5 周目 P1）: 否定の言い回し・0 円のあとに続く額・言い切りでない文は、相違ありとして確認を求める
    for neg, want in (('工賃相違: 一致していません', ['工賃相違']), ('部品相違なしではありません', ['部品相違']),
                      ('工賃相違は一致しておりません', ['工賃相違']), ('部品相違: 一致しません', ['部品相違']),
                      ('部品相違: 不一致', ['部品相違']), ('部品相違（1200）', ['部品相違']),
                      ('工賃相違（見積書 0円 / CSV 8,000円）', ['工賃相違']), ('部品相違0円ではなく1,200円', ['部品相違']),
                      ('部品相違0円ではありません', ['部品相違']),
                      # 括弧書きの打ち消し・片側だけの言い切り（Codex 6 周目 P1）
                      ('部品相違なし（ではありません）', ['部品相違']), ('部品相違0円（不一致）', ['部品相違']),
                      ('部品相違、工賃相違0円', ['部品相違']), ('部品相違 工賃相違なし', ['部品相違']),
                      ('工賃相違0円（見積書と照合）', ['工賃相違']),
                      # 言い切りでない文は、数字が説明でも相違ありに倒す（確認を 1 回求めるだけ。見逃すよりよい。
                      # 3 周目に「説明の数字を額と読むな」の指摘があったが、それを受けて部分的に読むと 4・5 周目の見逃しが開いた）
                      ('部品相違・工賃相違（2項目とも一致）', ['部品相違', '工賃相違']), ('部品相違: 2項目とも一致', ['部品相違']),
                      ('部品相違（1円単位で一致）', ['部品相違']), ('部品相違なし 工賃相違なし（1円単位で一致）', ['工賃相違']),
                      ('工賃相違（見積書 12,000円 / CSV 10,800円）部品は一致', ['工賃相違'])):
        chk(m([neg]) == want, f'言い切りでない申告・否定の言い回しを相違なしにしている: {neg} → {m([neg])}（{want} のはず）')
    # 取り込み: 相違のメモは明細にせず注記に回り、画面（③の確認・ファイル名）へ運ぶ
    it, notes = app.parse_csv_to_items('品名,区分,数量,部品金額,工賃,部品コード\nﾊﾞﾝﾊﾟ,取替,1,45000,0,\n"部品相違1,200円 工賃相違0円"\n',
                                       return_notes=True)
    chk(len(it) == 1 and any(app._is_ai_diff_text(n) for n in notes), f'相違のメモの扱い: {it} / {notes}')
    # 前置き付きの申告も拾う（「⚠️」「※」・長い前置き・短い前置き）。申告は**全文のまま**③の確認につなぐ（Codex 1 周目 P2。
    # 先頭を「部品相違」にそろえて前を切り捨てると「部品代相違1,200円 工賃相違0円」の部品側が消えた。バグハント第 3 弾 B1）
    _h = '品名,区分,数量,部品金額,工賃,部品コード\nﾊﾞﾝﾊﾟ,取替,1,45000,0,\n'
    for tail in ('"⚠️ 部品相違1,200円 工賃相違0円"\n', '"※部品相違1,200円"\n', '"相違確認結果: 部品相違1,200円 工賃相違0円（照合済み）"\n',
                 '"相違: 部品相違1,200円"\n', '相違確認,部品相違1,200円\n'):
        it_p, notes_p = app.parse_csv_to_items(_h + tail, return_notes=True)
        _d = [n for n in notes_p if app._is_ai_diff_text(n)]
        chk(len(it_p) == 1 and _d and m(_d) == ['部品相違'],
            f'前置き付きの相違申告（{tail.strip()}）を③の確認につないでいない: 明細 {len(it_p)} 行 / {notes_p}')
    # 逆向き: 相違の無い申告は確認にしない／相違の語を含む正しい明細（金額あり）は明細のまま
    it_n, notes_n = app.parse_csv_to_items(_h + '"※部品相違・工賃相違はありません（一致）"\n', return_notes=True)
    chk(len(it_n) == 1 and m([n for n in notes_n if app._is_ai_diff_text(n)]) == [],
        f'「相違はありません」を相違として確認にしている: {notes_n}')
    it_i, notes_i = app.parse_csv_to_items(_h + 'ﾊﾟﾈﾙ部品相違調整,調整,1,0,3000,\n', return_notes=True)
    chk(len(it_i) == 2 and not any(app._is_ai_diff_text(n) for n in notes_i),
        f'金額のある明細を相違のメモとして捨てている: {it_i} / {notes_i}')
    # 品名に「相違」の無い 0 円の行は、ほかの欄に「部品相違」の語があっても明細のまま（備考などの語を申告と取り違えない）
    it_k, notes_k = app.parse_csv_to_items(_h + 'ﾄﾞｱﾐﾗｰ,部品相違なし,1,0,0,\n', return_notes=True)
    chk(len(it_k) == 2, f'品名に「相違」の無い明細を相違のメモとして捨てている: {it_k} / {notes_k}')
    # ファイル名の印（CSV は pdf_parts が 0 なので照合の印が付かない。AI の申告を確かめて作ったものに付ける）
    n1 = app.generate_filename({'car_name': 'ﾃｽﾄ車'}, 45000, 0, 0, 0, True, extra_marks=['部品相違'])
    chk(n1.endswith('_見積（部品相違）.neo'), f'AI の申告の印がファイル名に無い: {n1}')
    n2 = app.generate_filename({'car_name': 'ﾃｽﾄ車'}, 45000, 0, 0, 0, True)
    chk('相違' not in n2, f'印の無い CSV に相違が付いた: {n2}')
    n3 = app.generate_filename({'car_name': 'ﾃｽﾄ車'}, 45000, 0, 0, 0, True, grand_ok=False, extra_marks=['総額相違', '工賃相違'])
    chk(n3.count('総額相違') == 1 and '工賃相違' in n3, f'印が重なる／落ちる: {n3}')
    chk('/' not in app.generate_filename({}, 0, 0, 0, 0, True, extra_marks=['部品/相違']), 'ファイル名に使えない文字を入れている')
    # 画面の配線: ②で明細の指紋と組にして運び、③で確認を求め、④でファイル名に付ける
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    for frag, why in (("st.session_state['_csv_ai_diffs'] = {'sig': _items_sig(_preview_items)", '①で明細と組にしていない'),
                      ("estimate_data['_csv_ai_diffs'] = [str(n) for n in _cad['notes']]", '②で運んでいない'),
                      ("key=_ack_key('csv_ai_diff_confirmed', _ack_base, _ai_notes)", '③に確認のチェックが無い'),
                      # 確認を外す鍵に取り込みごとの番号を入れる（同じ文面の申告の別の CSV で前の確認を持ち越さない。Codex 1 周目 P1）
                      ("estimate_data['_csv_import_seq'] = _cseq", '②で取り込みごとの番号を振っていない'),
                      # 確認のチェックの鍵は、取り込み（中身・番号）といまの明細から作る（バグハント第 3 弾 B4）
                      ("_ack_base = (str(st.session_state.get('_estimate_token') or ''), str((estimate_data or {}).get('_csv_import_seq', '')),",
                       '③の確認を外す鍵に取り込みの番号・いまの明細が無い（確認のあとに直した明細で生成できる。Codex 7 周目 P1）'),
                      ("                         _items_sig(edited_items))",
                       '③の確認を外す鍵にいまの明細が無い（確認のあとに直した明細で生成できる。Codex 7 周目 P1）'),
                      ("amount_confirmed = bool(amount_confirmed) and bool(_ai_ok)", '③の確認が生成を止めていない'),
                      ("extra_marks=(_s3v.get('csv_ai_marks') or ())", '④でファイル名に印を付けていない')):
        chk(frag in src, f'AI の申告の配線: {why}')


def test_qty_unit_in_classification():
    """数量 × 単価 ≠ 部品金額（validate_row_consistency の判定）を、CSV 取り込み・プレビュー取り込みのステップ③の点検にも出す
    （2026-09-21 §2-3 ①。PDF 経路にしか無く、CSV では 1 つも出なかった）。行は画面の表の No（_ed_no）で呼ぶ"""
    alerts = app.check_parts_labor_classification([
        {'name': 'ｸﾘｯﾌﾟ', 'method': '取替', 'quantity': 3, 'parts_amount': 5000, 'wage': 0, '_ed_no': 4},
        {'name': 'ﾎﾞﾙﾄ', 'method': '取替', 'quantity': 14, 'parts_amount': 1960, 'wage': 0, '_ed_no': 5},
        {'name': 'ﾅｯﾄ', 'method': '取替', 'quantity': 1, 'parts_amount': 333, 'wage': 0}])
    qa = [a for a in alerts if a['flag'] == 'qty_unit']
    chk(len(qa) == 1 and qa[0]['row_no'] == 4 and qa[0]['severity'] == 'warning' and '行4「ｸﾘｯﾌﾟ」' in qa[0]['message']
        and '1666.7' in qa[0]['message'], f'数量 3 × 単価 ≠ 5,000 円を表の No で知らせていない: {qa}')
    # ほかのパターンとは別の話なので両方出る（脱着の行の部品代 ＋ 割り切れない部品代）
    a2 = app.check_parts_labor_classification([{'name': 'ﾐﾗｰ', 'method': '脱着', 'quantity': 3, 'parts_amount': 1000, 'wage': 2000}])
    chk(sorted(a['flag'] for a in a2) == ['parts_in_labor', 'qty_unit'], f'脱着の部品代と数量の点検が両方出ない: {a2}')
    # PDF 経路の文言は元のまま（同じ判定を関数に切り出しただけ）
    _, w = app.validate_row_consistency([{'name': 'ｸﾘｯﾌﾟ', 'quantity': 3, 'parts_amount': 5000, 'wage': 0}])
    chk(w == ['行1「ｸﾘｯﾌﾟ」: 数量3 × 単価1666.7 ≠ 部品金額¥5,000（端数あり）'], f'validate_row_consistency の文言が変わった: {w}')
    # ③の控え（キャッシュ）の鍵に数量が入っている（数量を直しても古い点検が残らない）
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    chk("it.get('quantity', 1), it.get('_ed_no')) for it in edited_items]))" in src, '③の点検の控えの鍵に数量・No が無い')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                FAILS.append(f'{name}: 例外 {type(e).__name__}: {e}')
    for f_ in FAILS:
        print('*** FAILED:', f_)
    print('reg_round3:', 'all ok' if not FAILS else f'{len(FAILS)} 件が不合格')
    sys.exit(1 if FAILS else 0)
