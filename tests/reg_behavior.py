# -*- coding: utf-8 -*-
"""reg_insurance 12a〜12w・reg_expense の実測値を、**振る舞いで**確かめる回帰テスト（2026-09-22 バグハント第 4 弾 D）。

9/21 のテストの多くはソースに文字列があるかを見ていて、修正前の形そのままに戻したときしか落ちなかった
（102 通り壊して、狙いどおり落ちたのは 51 通り。条件の反転・`if False:`・文字列をコメントに残す形は素通り）。
ここでは関数を実際に呼び、画面は AppTest か偽の st で描いて確かめる。この書き方で 102 通りすべてが落ちることを確かめてある。
監査で見つかった不具合 2 件（車検証 OCR の 429/404 が記録されない・書類の読み取り失敗で API の返事の本文が画面に出る）も守る。

ソースの文字列を探すのではなく、関数を実際に呼び、画面は AppTest（Streamlit 公式のテスト器）か偽の st で描いて確かめる。
文字列を死んだコードに置いても、条件を反転しても、書き方を変えても落ちる（D_mutation/mutate.py で確かめた）。

- 外部 API は呼ばない（キーを環境から外し、Gemini / Claude は偽物を差し込む）
- 顧客情報は使わない（PDF・画像はここで作る。見積書のサンプルは読まない）
- XROOT で読むツリーを変えられる（ほかの reg_*.py と同じ）
- AppTest を省くときは NEO_SKIP_APPTEST=1（1 本 5 秒ほどかかる）
"""
import ast
import contextlib
import gc
import glob
import hashlib
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

R = os.environ.get('XROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, R)
sys.path.insert(0, os.path.join(R, 'tests'))
os.chdir(R)
sys.stdout.reconfigure(encoding='utf-8')
# キーは**空文字を入れておく**（消すだけだと app の import の load_dotenv が .env から読み直す。load_dotenv は入っている値を上書きしない）
for _k in ('GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOGLE_API_KEY', 'APP_PASSCODE'):
    os.environ[_k] = ''

import addata_locator  # noqa: E402
addata_locator.find_addata = lambda *a, **kw: None
import app  # noqa: E402

if app.GEMINI_API_KEY or app.ANTHROPIC_API_KEY:   # st.secrets などから本物のキーが入った: 課金の道に入らないよう止める
    print('REG_BEHAVIOR: API キーが読み込まれたので止めます（.streamlit/secrets.toml などを外して回してください）')
    sys.exit(2)

FAIL = []
_TMP_DIRS = []   # 作った一時フォルダ（最後に消す）


def _mkdtemp(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP_DIRS.append(d)
    return d


def chk(cond, msg):
    if not cond:
        FAIL.append(msg)


@contextlib.contextmanager
def section(tag):
    """節の中の例外は、その節の失敗として数える（例外で途中終了したのを「狙いどおり落ちた」と数えない）"""
    try:
        yield
    except Exception as e:  # noqa: BLE001
        FAIL.append(f'{tag}: 例外で止まった（{type(e).__name__}: {str(e)[:120]}）')


@contextlib.contextmanager
def patched(obj, **kw):
    old = {k: getattr(obj, k) for k in kw}
    for k, v in kw.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


# ── 偽の st（関数単位で画面の部品を描かせ、何を出したかを控える）────────────────────
class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Rerun(Exception):
    pass


class FakeSt:
    def __init__(self, state=None, press=False, check=None):
        self.session_state = dict(state or {})
        self.calls = []
        self._press, self._check = press, check

    def _rec(self, kind, a, k):
        self.calls.append((kind, a, k))

    def __getattr__(self, name):   # warning / error / info / success / caption / markdown など
        if name in ('warning', 'error', 'info', 'success', 'caption', 'markdown', 'write', 'toast'):
            return lambda *a, **k: self._rec(name, a, k)
        raise AttributeError(name)

    def columns(self, spec, **k):
        return [_Ctx() for _ in range(spec if isinstance(spec, int) else len(spec))]

    def radio(self, label, options=(), index=0, **k):
        self._rec('radio', (label,), k)
        return list(options)[index]

    def checkbox(self, label, value=False, **k):
        self._rec('checkbox', (label,), k)
        return self._check if self._check is not None else value

    def button(self, label, **k):
        self._rec('button', (label,), k)
        return bool(self._press) and not k.get('disabled')

    def download_button(self, label, **k):
        self._rec('download_button', (label,), k)
        return False

    def spinner(self, *a, **k):
        return _Ctx()

    expander = container = spinner

    def rerun(self):
        raise Rerun()

    def texts(self, kind):
        return [str(a[0]) for (kk, a, _k) in self.calls if kk == kind and a]


@contextlib.contextmanager
def fake_st(**kw):
    f = FakeSt(**kw)
    with patched(app, st=f):
        yield f


class UF:
    """st.file_uploader が返す UploadedFile の代わり（getvalue と name だけ）"""
    def __init__(self, b, name):
        self._b, self.name = b, name

    def getvalue(self):
        return self._b

    def read(self):   # read() は 1 回目だけ中身を返す（本物と同じく位置が進む）
        b, self._b = self._b, b''
        return b


def blank_pdf(pages=1):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595, height=842)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def png_bytes():
    from PIL import Image
    b = io.BytesIO()
    Image.new('RGB', (64, 64), 'white').save(b, 'PNG')
    return b.getvalue()


SRC = open(os.path.join(R, 'app.py'), encoding='utf-8').read()
TREE = ast.parse(SRC)

# ── P12a: 未定義の名前（pyflakes）を app.py・neo_skill だけでなく、アプリが読み込むモジュール全部で見る ─────────
with section('P12a'):
    _files = sorted(glob.glob('*.py')) + sorted(glob.glob('neo_skill/*.py'))
    _pf = subprocess.run([sys.executable, '-m', 'pyflakes'] + _files, cwd=R, capture_output=True, text=True,
                         encoding='utf-8', errors='replace')
    _out = _pf.stdout + _pf.stderr
    import importlib.util as _ilu
    if _ilu.find_spec('pyflakes') is None:
        # 入っていない環境（requirements.txt は本番用で pyflakes を含めない）では飛ばしたことを出して続ける。合格には数えない
        print('REG_BEHAVIOR: pyflakes が入っていないので P12a（未定義の名前）は飛ばしました（pip install pyflakes）')
        _out = ''
    else:
        # 入っているのに落ちたときに「未定義の名前なし」で通らないように（終了コード 0/1 は正常。1 は何か見つけたとき）
        chk(_pf.returncode in (0, 1) and 'Traceback' not in _out, f'P12a0: pyflakes が動いていない（終了コード {_pf.returncode}）')
    _undef = [ln for ln in _out.splitlines() if 'undefined name' in ln or 'invalid syntax' in ln]
    chk(not _undef, 'P12a: 未定義の名前・文法の誤りが残っている: ' + ' / '.join(_undef[:5]))

# ── P12a2: チェックを描く側と生成する側が同じ費用を使う（実際に _beta_expense_gate と _beta_generate_ui を動かす）──
with section('P12a2'):
    _st = {'exp_towing': 1000, 'exp_rental': 2000, 'exp_exempt': 0, '_beta_exp_file_key': 'k'}
    with fake_st(state=_st, check=True) as f:
        _use = app._beta_expense_gate('k')
    chk(_use is True and any('レッカー ¥1,000' in t and '代車 ¥2,000' in t for t in f.texts('checkbox')),
        f'P12a2: 費用があるのにチェックが出ない／額が違う: {f.texts("checkbox")[:1]}')
    _got = {}

    def _fake_pipe(*a, **k):
        _got.clear()
        _got.update(k)
        return {'ok': True}
    with patched(app, run_pdf_to_neo_pipeline=_fake_pipe, _attached_docs_ocr=lambda *a, **k: ({}, {}),
                 _doc_fill_to_sidebar=lambda d: {}, _sidebar_insurance_values=lambda: {},
                 _p2n_inputs_signature=lambda *a, **k: 'sig'), \
            patched(app._doc_hints, vehicle_info_for_legacy=lambda *a, **k: {}):
        for _checked, _want in ((True, {'towing': 1000, 'rental_car': 2000, 'tax_exempt': 0}), (False, None)):
            with fake_st(state=dict(_st, _beta_use_exp_val=_checked), press=True):
                try:
                    app._beta_generate_ui(UF(b'%PDF', 'a.pdf'), b'%PDF', 'k', 'KEY', 'm', '税抜き（外税）', inline=True)
                except Rerun:
                    pass
            chk(_got.get('expenses') == _want,
                f'P12a2: チェック={_checked} のとき生成に渡る費用が {_got.get("expenses")!r}（期待 {_want!r}）')

# ── P12c: 一度読めた役割を先に試す（控えに insdoc の成功・shaken の古い失敗があるとき、API を呼ばず insdoc で決まる）──
with section('P12c'):
    _img = png_bytes()
    _h = hashlib.sha256(_img).hexdigest()
    _kb = hashlib.sha256(b'KEY').hexdigest()[:12]
    _called = []
    _cache = {('insdoc', _h, 'm', _kb): {'accept_no': 'A1'}, ('shaken', _h, 'm', _kb): {'_error': 'x', '_t': 0}}
    with patched(app, analyze_vehicle_registration=lambda *a, **k: _called.append('shaken') or {'car_name': 'X', 'car_serial_no': 'Y'},
                 analyze_insurance_document=lambda *a, **k: _called.append('insdoc') or {'accept_no': 'B'}):
        with fake_st(state={'docs_upload_0': [UF(_img, '車検証.png')], '_doc_ocr_cache': _cache}):
            _vd, _doc = app._attached_docs_ocr('KEY', 'm')
    chk(_called == [] and _vd == {} and _doc == {'accept_no': 'A1'},
        f'P12c: 読めた役割より名前を先に試して役割が入れ替わった（API {_called}・車検証 {bool(_vd)}・書類 {_doc}）')

# ── P12d: テンプレート NEO を使っているときだけ、金額表記の行に注意を出す ──────────────
with section('P12d'):
    with fake_st(state={'custom_neo_bytes': b'x'}) as f:
        app._p2n_tax_row('')
    chk(any('テンプレートNEO' in t for t in f.texts('warning')), 'P12d: テンプレート NEO を使っているのに注意が出ない')
    with fake_st(state={}) as f:
        app._p2n_tax_row('')
    chk(not any('テンプレートNEO' in t for t in f.texts('warning')), 'P12d2: 使っていないのに注意が出る')

# ── P12e: 入れたファイル（st.file_uploader の戻り値とそこから取った物）に read()/seek() を使わない（構文木で見る）──
with section('P12e'):
    def _own_nodes(fn):   # その関数の中のノード（入れ子の関数・lambda は別の関数として数える）
        out, stack = [], list(ast.iter_child_nodes(fn))
        while stack:
            x = stack.pop()
            if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            out.append(x)
            stack.extend(ast.iter_child_nodes(x))
        return out
    _bad, _seen = set(), set()
    for _fn in [TREE] + [n for n in ast.walk(TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        _nodes = _own_nodes(_fn)
        _up = set()
        for _n in _nodes:   # 1) file_uploader の戻り値を受けた名前
            if isinstance(_n, ast.Assign) and isinstance(_n.value, ast.Call) and getattr(_n.value.func, 'attr', '') == 'file_uploader':
                _up |= {t.id for t in _n.targets if isinstance(t, ast.Name)}
        _grow = bool(_up)
        while _grow:   # 2) それを含む式から代入された名前・それを回す for の変数（vehicle_file = _f など）
            _grow = False
            for _n in _nodes:
                _tg, _val = [], None
                if isinstance(_n, (ast.Assign, ast.AnnAssign)) and _n.value is not None:
                    _tg, _val = (_n.targets if isinstance(_n, ast.Assign) else [_n.target]), _n.value
                elif isinstance(_n, (ast.For, ast.comprehension)):
                    _tg, _val = [_n.target], _n.iter
                if _val is None or not any(isinstance(x, ast.Name) and x.id in _up for x in ast.walk(_val)):
                    continue
                for t in _tg:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name) and x.id not in _up:
                            _up.add(x.id)
                            _grow = True
        _seen |= _up
        _bad |= {f'{n.func.value.id}.{n.func.attr}() {n.lineno} 行' for n in _nodes
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ('read', 'seek')
                 and isinstance(n.func.value, ast.Name) and n.func.value.id in _up}
    chk({'_p2n_file', '_csv_file', 'custom_neo_file', 'vehicle_file'} <= _seen, f'P12e0: 入れたファイルの名前を拾えていない: {sorted(_seen)}')
    chk(not _bad, f'P12e: 入れたファイルを read()/seek() で読んでいる（2 回目で空になる）: {sorted(_bad)}')

# ── P12f: API の返事の本文が例外の文に入らない（偽の Gemini / Claude を実際に通す）────────────
with section('P12f'):
    from neo_skill import llm as L
    SECRET = 'LEAKCHECK-本文-7Q9Z'   # 顧客情報の形をしない目印（落ちたときにログへ出ても害が無い）

    class _GErr(Exception):
        def __init__(self, msg, code):
            super().__init__(msg)
            self.code = code

    def _gemini_msg(exc):
        from google.genai import types
        r = L.GeminiReader.__new__(L.GeminiReader)
        r._types, r.model, r.max_tokens, r.calls = types, 'm', 100, 0

        class _Models:
            def generate_content(self, **k):
                raise exc
        r.client = type('C', (), {'models': _Models()})()
        try:
            r.ask('sys', [{'type': 'text', 'text': 'x'}])
            return '（例外にならなかった）'
        except L.LLMError as e:
            return str(e)

    class _A:   # 偽の anthropic モジュール（例外の型だけ）
        class AuthenticationError(Exception):
            message = SECRET

        class RateLimitError(Exception):
            message = SECRET

        class APIStatusError(Exception):
            message, status_code = SECRET, 400

        class APIConnectionError(Exception):
            message = SECRET

    def _claude_msg(exc):
        r = L.ClaudeReader.__new__(L.ClaudeReader)
        r._anthropic, r.model, r.max_tokens, r.effort, r.calls = _A, 'm', 100, 'high', 0

        class _Msgs:
            def create(self, **k):
                raise exc
        r.client = type('C', (), {'messages': _Msgs()})()
        try:
            r.ask('sys', [{'type': 'text', 'text': 'x'}])
            return '（例外にならなかった）'
        except L.LLMError as e:
            return str(e)
    with patched(L.time, sleep=lambda s: None):
        for _code in (400, 429, 500):
            _m = _gemini_msg(_GErr(f'{_code} ' + SECRET, _code))
            chk(SECRET not in _m and 'LLMError' not in _m, f'P12f: Gemini {_code} の例外の文に返事の本文が入る: {_m[:120]}')
    for _cls in (_A.AuthenticationError, _A.RateLimitError, _A.APIStatusError, _A.APIConnectionError):
        _m = _claude_msg(_cls(SECRET))
        chk(SECRET not in _m, f'P12f: Claude {_cls.__name__} の例外の文に返事の本文が入る: {_m[:120]}')
    # 読み手の途中でよその例外が出たとき（reader.read_estimate の最後の except）
    from neo_skill import reader as RR

    class _BoomReader:
        max_pdf_bytes = 0
        model = 'm'

        def ask(self, *a, **k):
            raise ValueError(SECRET)
    _cd = _mkdtemp('p12f_')
    _rd = RR.read_estimate(blank_pdf(), reader=_BoomReader(), case_dir=_cd, source_name='x.pdf')
    chk(_rd.error and SECRET not in str(_rd.error), f'P12f2: 読み取りの想定外の例外の本文が文に入る: {str(_rd.error)[:120]}')
with section('P12f3'):
    # 添付（事故・保険の書類）の読み取りが API エラーで失敗したとき、画面の知らせ（_doc_ocr_error）に返事の本文を入れない。
    # ※ HEAD では落ちる: analyze_insurance_document が {'_error': str(例外)} をそのまま返し、_attached_docs_ocr がそれを知らせに載せる
    class _GE(Exception):
        def __init__(self, m, code):
            super().__init__(m)
            self.code = code

    class _M3:
        def generate_content(self, **kw):
            raise _GE('400 INVALID_ARGUMENT ' + SECRET, 400)

    def _cg3(*a, **k):
        raise _GE('400 INVALID_ARGUMENT ' + SECRET, 400)
    with patched(app, _get_genai_client=lambda k: type('C', (), {'models': _M3()})(), call_gemini=_cg3,
                 get_alternative_gemini_model=lambda *a, **k: ''):
        with fake_st(state={'docs_upload_0': [UF(png_bytes(), 'report.png')]}) as f:
            app._attached_docs_ocr('KEY', 'm')
    chk(f.session_state.get('_doc_ocr_error') and SECRET not in str(f.session_state.get('_doc_ocr_error')),
        f"P12f3: 添付の読み取り失敗の知らせに API の返事の本文が入る: {str(f.session_state.get('_doc_ocr_error'))[:100]}")

# ── P12g / P12k5: 読めない添付（0 バイト・中身が別物）は AI に投げず、結果のところで知らせる ────────────
with section('P12g'):
    _called = []
    with patched(app, analyze_vehicle_registration=lambda *a, **k: _called.append('shaken') or {},
                 analyze_insurance_document=lambda *a, **k: _called.append('insdoc') or {}):
        for _tag, _b in (('P12g', b''), ('P12k5', b'This is not a PDF at all.' * 5)):
            _called.clear()
            with fake_st(state={'docs_upload_0': [UF(_b, '車検証.pdf')]}) as f:
                app._attached_docs_ocr('KEY', 'm')
            chk(_called == [], f'{_tag}: 読めない添付を AI に投げている（{_called}）')
            chk('1 件' in str(f.session_state.get('_doc_ocr_error') or ''),
                f'{_tag}: 読めない添付を黙って捨てている（_doc_ocr_error={f.session_state.get("_doc_ocr_error")!r}）')

# ── P12h: パイプラインの例外は、自分で書いた例外だけ本文を残す（関数と p2n_read / p2n_make の except を実際に通す）──
with section('P12h'):
    SECRET2 = 'LEAKCHECK-ファイル名-7Q9Z.pdf'
    from neo_skill import llm as L2, reader as R2, maker as MK
    chk(SECRET2 not in app._safe_pipeline_err(ValueError(SECRET2)), 'P12h: よその例外の本文が画面の文に入る')
    chk(app._safe_pipeline_err(L2.LLMError('キーを直してください')) == 'キーを直してください',
        'P12h2: 自分で書いた例外（LLMError）の本文が消えた')
    _ad = _mkdtemp('p12h_addata_')
    os.makedirs(os.path.join(_ad, 'COM'))
    for _fn in ('KA06_ALL.DB', 'AnVer.DB'):
        open(os.path.join(_ad, 'COM', _fn), 'wb').close()

    def _boom(*a, **k):
        raise ValueError(SECRET2)
    with patched(L2, make_reader=lambda *a, **k: object()), patched(R2, read_estimate=_boom):
        _o = app.p2n_read(blank_pdf(), SECRET2, 'KEY', addata_root=_ad, reader_kind='gemini', model_name='m')
    chk(_o.get('error') and SECRET2 not in _o['error'], f'P12h3: p2n_read の例外の本文が画面の文に入る: {_o.get("error")!r}')
    _cd2 = _mkdtemp('p12h_case_')
    with patched(MK, make_neo=_boom):
        _o2 = app.p2n_make({'case_dir': _cd2, 'reading': {}}, addata_root=_ad)
    chk(_o2.get('error') and SECRET2 not in _o2['error'], f'P12h4: p2n_make の例外の本文が画面の文に入る: {_o2.get("error")!r}')

# ── P12i2: ボタンのキーは構文木で数える（引用符の違いで数え漏れない）──────────────────
with section('P12i2'):
    _keys = [kw.value.value for n in ast.walk(TREE) if isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == 'button'
             for kw in n.keywords if kw.arg == 'key' and isinstance(kw.value, ast.Constant)]
    chk(_keys.count('pdf2neo_run_beta') == 1 and 'pdf2neo_run_beta_wait' in _keys,
        f"P12i2: 本物のベタ打ちボタンのキーが {_keys.count('pdf2neo_run_beta')} か所（待ち用と同じキー）")

# ── P12k3: 正しい 1 ページの PDF は通し、上限を超えるページ数は断る（サンプル PDF が無い PC でも回る）──────────
with section('P12k3'):
    chk(not app._upload_kind_problem(blank_pdf(1), 'a.pdf'), 'P12k3: 1 ページの正しい PDF を断ってしまう')
    chk(not app._upload_kind_problem(blank_pdf(3), 'a.pdf', max_pages=3), 'P12k3b: 上限ちょうどの PDF を断ってしまう')
    chk(bool(app._upload_kind_problem(blank_pdf(3), 'a.pdf', max_pages=2)), 'P12k3c: 上限を超える PDF を通してしまう')

# ── P12r / P12u4 / P12v5: 使えないモデルの記録（キーごと・worker の記録の合流・すべての書き込みでキーを渡す）───────
with section('P12r'):
    app._FALLBACK_STORE.clear()
    app._unavailable_set('keyA').add('dead-A')
    chk('dead-A' not in app._unavailable_set('keyB'), 'P12r: 別の API キーの「使えないモデル」が混ざる')
    chk('dead-A' in app._unavailable_set('keyA'), 'P12r2: 自分のキーの記録が読めない')
    app._FALLBACK_STORE.clear()
with section('P12u4'):
    app._FALLBACK_STORE.clear()
    app._mark_model_unavailable('keyA', 'dead-1')
    chk('dead-1' in app._unavailable_set('keyA'), 'P12u4: 提供終了の記録が、そのキーの集合に入らない（worker で使うと画面に届かない）')
    # 呼び出しはすべて、None でないキーを渡す（書き方の違い — 引数なし・None・api_key=None — をまとめて見る）
    _nokey = [n.lineno for n in ast.walk(TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id in ('_quota_exhausted_set', '_unavailable_set', '_model_set')
              and not [a for a in (list(n.args[1:] if n.func.id == '_model_set' else n.args) + [k.value for k in n.keywords])
                       if not (isinstance(a, ast.Constant) and a.value is None)]]
    chk(not _nokey, f'P12u4b: API キーを渡さずに使えないモデルの集合を読み書きしている行: {_nokey}')
    app._FALLBACK_STORE.clear()
with section('P12u4c'):
    # 車検証の OCR が 429 を返したら、そのモデルを「クォータ切れ」として記録する（偽の Gemini。外には出ない）
    class _Cli(Exception):
        def __init__(self, msg, code):
            super().__init__(msg)
            self.code = code

    class _M:
        def generate_content(self, **kw):
            raise _Cli('429 RESOURCE_EXHAUSTED quota', 429)

    def _cg(*a, **k):
        raise _Cli('429 RESOURCE_EXHAUSTED quota', 429)
    app._FALLBACK_STORE.clear()
    with patched(app, _get_genai_client=lambda k: type('C', (), {'models': _M()})(), call_gemini=_cg,
                 get_alternative_gemini_model=lambda *a, **k: ''):
        app.analyze_vehicle_registration('keyA', png_bytes(), 'image/png', 'm-q')
    chk('m-q' in app._quota_exhausted_set('keyA'),
        'P12u4c: 車検証の OCR の 429 でモデルがクォータ切れとして記録されない（同じモデルを選び続ける）')
    app._FALLBACK_STORE.clear()
with section('P12v5'):
    _screen = {}
    app._FALLBACK_STORE.clear()
    app._FALLBACK_STORE['unavailable:' + app._api_key_tag('keyA')] = {'dead-w'}
    with patched(app, _persist_store=lambda: _screen):
        chk('dead-w' in app._unavailable_set('keyA'), 'P12v5: worker が記録した使えないモデルが画面側に届かない')
    app._FALLBACK_STORE.clear()

# ── P12s: テンプレート NEO の生バイトをプロセスに持たない（参照が残らないことを数える）──────────────
with section('P12s'):
    _tpl = open(os.path.join(R, 'template_toyota.neo'), 'rb').read()
    _nb = bytes(bytearray(_tpl))          # 新しいオブジェクト
    _before = sys.getrefcount(_nb)
    _r = app._neo_tax_round(_nb)
    gc.collect()
    chk(sys.getrefcount(_nb) == _before, f'P12s: _neo_tax_round が渡された NEO の生バイトを持ち続ける（参照 {_before}→{sys.getrefcount(_nb)}）')
    chk(_r == ('四捨五入', 1), f'P12s2: 端数処理の読み取りが違う: {_r}')

# ── P12t: 差異ありのベタ打ち NEO だけ、名前に _要確認 を付ける ──────────────────────
with section('P12t'):
    _base = {'ok': True, 'items': [{'name': 'A', 'parts_amount': 100, 'wage': 0, 'quantity': 1}], 'neo_bytes': b'NEO',
             'vehicle_info': {}}

    def _dl_name(res):
        with fake_st(state={}) as f:
            app._render_beta_result(res, 'm')
        return [k.get('file_name') for (kd, a, k) in f.calls if kd == 'download_button']
    _n_ng = _dl_name(dict(_base, verify={'verified_against_pdf': True, 'ok': False}))
    _n_ok = _dl_name(dict(_base, verify={'verified_against_pdf': True, 'ok': True}))
    chk(_n_ng and '_要確認' in str(_n_ng[0]), f'P12t: 差異ありの NEO の名前に _要確認 が無い: {_n_ng}')
    chk(_n_ok and '_要確認' not in str(_n_ok[0]), f'P12t2: 合格の NEO の名前に _要確認 が付いた: {_n_ok}')

# ── P12u / P12m4: 小計の突き合わせの逆向きの穴（節ごと・ページごとの小計、同じ名前の総計は最後）────────────
with section('P12u'):
    def _csvchk(t):
        it, no = app.parse_csv_to_items(t, return_notes=True)
        return len(it), [n for n in no if str(n).startswith('❌')]
    _H = '品名,区分,数量,部品金額,工賃,部品コード\n'
    for _tag, _csv in (
            ('P12u: 節ごとの「部品小計」だけの正しい CSV', _H + 'A,取替,1,20000,0,\n部品小計,,,20000,,\nB,取替,1,25000,0,\n部品小計,,,25000,,\n'),
            ('P12u2: ページごとの「小計」だけの正しい CSV', _H + 'A,取替,1,20000,0,\n小計,,,20000,,\nB,取替,1,25000,0,\n小計,,,25000,,\n'),
            ('P12u3: 同じ名前の総計は最後が本物（ページの部品計のあとに総計）', _H + 'A,取替,1,20000,0,\n部品計,,,20000,,\nB,取替,1,25000,0,\n部品計,,,45000,,\n')):
        _n, _e = _csvchk(_csv)
        chk(_n == 2 and not _e, f'{_tag}を止めてしまう: {_e[:1]}')

# ── P12x: 12b / 12i / 12k4 / 12p を AppTest で実際の画面として描いて確かめる ──────────────────────
if os.environ.get('NEO_SKIP_APPTEST') != '1':
    from streamlit.testing.v1 import AppTest

    def _at():
        a = AppTest.from_file(os.path.join(R, 'app.py'), default_timeout=180)
        for _k in ('GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'APP_PASSCODE'):
            a.secrets[_k] = ''   # secrets.toml があってもキーを入れない
        a.run()
        return a

    def _btn(a):
        return {b.key: b.disabled for b in a.button if b.key}
    with section('P12i'):
        a = _at()
        chk(not a.exception, f'P12i0: 最初の画面で例外: {[str(e.value)[:80] for e in a.exception]}')
        chk(_btn(a).get('pdf2neo_run_disabled') is True and _btn(a).get('pdf2neo_run_beta_wait') is True
            and any(r.key == 'pdf_tax_radio' for r in a.radio),
            f'P12i: 見積書を入れる前に、押せないボタン 2 つと金額表記が出ていない: {_btn(a)}')
    with section('P12b'):
        a = _at()
        a.file_uploader(key='pdf2neo_upload').set_value(('見積.pdf', blank_pdf(), 'application/pdf'))
        a.run()
        chk(_btn(a).get('pdf2neo_run_disabled') is True and any(r.key == 'pdf_tax_radio' for r in a.radio)
            and any('ベタ打ちの読み取りは Gemini を使います' in c.value for c in a.caption),
            f'P12b: キーが無いときにボタン・金額表記・ベタ打ちの案内が出ず行き止まりになる: {_btn(a)}')
    with section('P12k4'):
        a = _at()
        a.file_uploader(key='pdf2neo_upload').set_value(('x.pdf', b'This is not a PDF at all.' * 5, 'application/pdf'))
        a.run()
        chk(any('この見積書は読めません' in e.value for e in a.error)
            and not [k for k, dis in _btn(a).items() if k.startswith('pdf2neo_run') and not dis],
            f'P12k4: 中身が PDF でない見積書で、断りが出ない／生成ボタンが押せる: {_btn(a)}')
    with section('P12p'):
        a = _at()
        _fu = a.file_uploader(key='csv_file_upload_0')
        _fu.set_value(('good.csv', (_H + 'A,取替,1,45000,0,\n').encode('utf-8'), 'text/csv'))
        a.run()
        chk('csv_items' in a.session_state, 'P12p0: 正しい CSV を取り込めていない（前提）')
        a.file_uploader(key='csv_file_upload_0').set_value(('bad.csv', bytes([0x81, 0x39, 0xfd]), 'text/csv'))
        a.run()
        chk('csv_items' not in a.session_state and any('文字コードを判別できません' in e.value for e in a.error),
            'P12p: 読めない CSV を入れても、前に取り込んだ明細が残る')

# ── P12j / P12w: 画面の CSS を「規則」として読み、効き方で確かめる（文字列の有無ではなく）──────────────────
with section('P12j'):
    _css = re.sub(r'/\*.*?\*/', '', '\n'.join(re.findall(r'<style>(.*?)</style>', SRC, re.S)), flags=re.S)

    def _rules(css):
        out, media, i, pre = [], [], 0, ''
        while i < len(css):
            c = css[i]
            if c == '{':
                p = pre.strip()
                pre = ''
                if p.startswith('@'):
                    media.append(p)
                else:
                    j = css.index('}', i)
                    decl = {}
                    for d in css[i + 1:j].split(';'):
                        if ':' in d:
                            k, v = d.split(':', 1)
                            decl[k.strip().lower()] = v.strip().lower()
                    out.append((tuple(media), [s.strip() for s in p.split(',') if s.strip()], decl))
                    i = j
            elif c == '}':
                if media:
                    media.pop()
                pre = ''
            else:
                pre += c
            i += 1
        return out
    RULES = _rules(_css)

    def _last(sel):   # セレクタが最後に指す要素（疑似クラスは外す）
        return re.sub(r':[\w-]+(\([^)]*\))?', '', re.split(r'\s*[>+~]\s*|\s+', sel.strip())[-1])
    _KEEP = ('header', '[data-testid="stheader"]', 'header[data-testid="stheader"]', '[data-testid="sttoolbar"]',
             '[data-testid="stexpandsidebarbutton"]')
    _hide = [(s, d) for m, ss, d in RULES for s in ss if _last(s).lower() in _KEEP
             and (d.get('display', '').startswith('none') or d.get('visibility', '').startswith('hidden')
                  or d.get('opacity', '').startswith('0'))]
    chk(not _hide, f'P12j: 「≫」かその入れ物（header・stToolbar）を消す規則がある: {_hide[:2]}')
    _hidden = {_last(s) for m, ss, d in RULES for s in ss if d.get('display', '').startswith('none')}
    chk({'[data-testid="stToolbarActions"]', '[data-testid="stAppDeployButton"]', '[data-testid="stMainMenu"]'} <= _hidden,
        'P12j3: ヘッダーの中の Deploy・⋮ メニューを隠す規則が効いていない')

    def _eff(target, prop, media_ok=lambda m: not m):   # 同じ要素への宣言を上から順に重ね、!important を優先して最後の値
        val, imp = None, False
        for m, ss, d in RULES:
            if media_ok(m) and prop in d and any(_last(s) == target and ('stHeader' in s or target == '.topbar') for s in ss):
                v = d[prop]
                vi = '!important' in v
                if vi or not imp:
                    val, imp = v.replace('!important', '').strip(), vi
        return val
    _eb = '[data-testid="stExpandSidebarButton"]'
    chk(_eff(_eb, 'position') == 'fixed' and _eff(_eb, 'pointer-events') == 'auto' and _eff(_eb, 'top') == '12px',
        f"P12j2: 「≫」が左上に固定されていない（position={_eff(_eb, 'position')} pointer-events={_eff(_eb, 'pointer-events')}）")
    # 狭い画面（〜1279px）でサイドバーが閉じている間は、トップバーを ≫ の高さ（12+34px）ぶん以上下げる
    _mq = [(m, ss, d) for m, ss, d in RULES if m and any('max-width' in x for x in m)
           and any('aria-expanded="false"' in s and _last(s) == '.topbar' for s in ss)]
    _ok = [(m, d) for m, ss, d in _mq
           if int(re.search(r'max-width:\s*(\d+)px', ' '.join(m)).group(1)) >= 1279
           and int(re.sub(r'\D', '', d.get('margin-top', '0')) or 0) >= 44]
    chk(bool(_ok), f'P12j4/P12w: 1279px 以下でサイドバーを閉じたときにトップバーを 44px 以上下げていない: {[(m, d.get("margin-top")) for m, ss, d in _mq]}')
    # 生成ボタン: 実際のボタンのキー（st-key-<key>）すべてに、高さ・折り返しの規則が当たる
    _rk = sorted({kw.value.value for n in ast.walk(TREE) if isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == 'button'
                  for kw in n.keywords if kw.arg == 'key' and isinstance(kw.value, ast.Constant)
                  and str(kw.value.value).startswith('pdf2neo_run')})
    _hit = {k: any(re.search(r'\[class\*="([^"]+)"\]\s+button$', s) and re.search(r'\[class\*="([^"]+)"\]', s).group(1) in f'st-key-{k}'
                   and d.get('min-height', '').startswith('44px') and d.get('height', '').startswith('auto')
                   and d.get('white-space', '').startswith('normal')
                   for m, ss, d in RULES for s in ss) for k in _rk}
    chk(_rk and all(_hit.values()), f'P12w2: 高さ・折り返しの規則が当たらない生成ボタンがある: {[k for k, v in _hit.items() if not v] or "キー無し"}')
    # トップバー: 最後に効く height が auto、min-height が 56px
    chk(_eff('.topbar', 'height') == 'auto' and _eff('.topbar', 'min-height') == '56px',
        f"P12w3: トップバーの高さが固定されている（height={_eff('.topbar', 'height')} min-height={_eff('.topbar', 'min-height')}）")

# ── PX: 費用行の消費税は、テンプレートの端数処理どおり（コグニ実機の 105 円 → 四捨五入 11・切り捨て 10 を NEO で確かめる）──
with section('PX'):
    import neogen

    def _tpl_with_flag(flag):
        t = open(os.path.join(R, 'template_toyota.neo'), 'rb').read()
        ck = app.find_real_cks(t)
        full = app.decompress_neo(t, ck)
        mgmt, ent = app.parse_entries(t, ck[0])
        fs = app.extract_files(full, ent)
        c = sqlite3.connect(':memory:')
        c.deserialize(fs['AnSvEm0001Ex.db'])
        c.execute('UPDATE Setting SET tx_ArrangeFlag=?', (flag,))
        c.commit()
        fs['AnSvEm0001Ex.db'] = c.serialize()
        c.close()
        return app.repack_neo(t, fs, mgmt, ent)
    for _flag, _mode, _want in ((1, '四捨五入', 11), (2, '切り捨て', 10)):
        _t = _tpl_with_flag(_flag)
        chk(app._neo_tax_round(_t) == (_mode, _flag), f'PX0: 作ったテンプレートの端数処理が読めない: {app._neo_tax_round(_t)}')
        _neo = app.generate_neo_file(_t, {}, [{'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1}],
                                     0, {}, {'towing': 105}, False, False, False)[0]
        _cur = neogen.opendb(neogen.unpack(_neo)['AnSMB.txt']).cursor()
        _row = _cur.execute('select WageOutTax, WageTax, WageInTax from Expense where LineNo=5').fetchone()
        # 費用行の税と、Total のレッカー専用欄の税（hy_Wrecker1Tax）が同じ端数処理で一致すること。
        # レッカー代は工賃側ではなくこの欄に入る（2026-09-28 コグニ実機。引き継ぎ書 §13-13）
        _tot = _cur.execute('select hy_Wrecker1Tax from Total').fetchone()[0]
        chk(tuple(_row) == (105, _want, 105 + _want) and _tot == _want,
            f'PX: {_mode} のテンプレートでレッカー 105 円の税が 費用行 {tuple(_row)}／Total {_tot}（コグニ実機は 税 {_want}・税込 {105 + _want}）')

import shutil  # noqa: E402
for _d in _TMP_DIRS:
    shutil.rmtree(_d, ignore_errors=True)
print('REG_BEHAVIOR:', 'ALL PASS' if not FAIL else f'FAIL {len(FAIL)}件')
for _f in FAIL:
    print('  -', _f)
sys.exit(1 if FAIL else 0)
