# -*- coding: utf-8 -*-
"""app.py と各モジュールの版が食い違ったときの回帰テスト。

Streamlit はメインスクリプト（app.py）を実行のたびに読み直すが、
`import` したモジュールは `sys.modules` に残ったままで、**プロセスを
再起動しない限り古い版がメモリに居座る**。そのため本番で
「app.py は新しいのに pdf_to_neo_pipeline は古い」という食い違いが起き、
`process_pdf_to_neo() got an unexpected keyword argument 'source_mime'`
で PDF→NEO 変換が丸ごと失敗した（2026-09-11）。

引数が増えた場合は例外で止まるので気づけるが、**中身だけが変わった場合は
黙って古い規則で .neo が出る**（たとえば neo_rules の作業区分の対応表）。
協定見積として保険会社に出すファイルなので、こちらのほうが危ない。

ここで守るのは3つ。
  1. ディスクと揃えられること（利用者が何もしなくても直る）
  2. 揃えられないときは、**古いまま走らせずに断る**こと
  3. 変換中に足元のモジュールを差し替えないこと
"""
import inspect
import os
import re
import sys
import threading
import types

R = os.environ.get('XROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, R)
os.chdir(R)
sys.stdout.reconfigure(encoding='utf-8')

import app  # noqa: E402

FAIL = []


def chk(cond, msg):
    if not cond:
        FAIL.append(msg)


# ── 1. 受けられない引数を見つけられるか ─────────────────────────────
def _old_sig(pdf_path, addata_root='', template_path=None, mode_override=None,
             model_name=None, api_key=None, cache_scope='',
             is_tax_inclusive=False, merge_mode=False, expenses=None):
    return {'ok': True}


def _new_sig(pdf_path, addata_root='', template_path=None, mode_override=None,
             model_name=None, api_key=None, cache_scope='',
             is_tax_inclusive=False, merge_mode=False, expenses=None,
             source_mime='application/pdf'):
    return {'ok': True, 'source_mime': source_mime,
            'is_tax_inclusive': is_tax_inclusive, 'expenses': expenses}


def _very_old(pdf_path, addata_root='', template_path=None):
    return {'ok': True}


chk(app._pipeline_accepts(_old_sig, ('source_mime',)) == ('source_mime',),
    '1: 古い版が受けられない引数を見つけられていない')
chk(app._pipeline_accepts(_new_sig, ('source_mime',)) == (),
    '1b: 新しい版なのに受けられないと言っている')
chk(app._pipeline_accepts(lambda p, **kw: None, ('source_mime', 'x')) == (),
    '1c: **kwargs を受ける相手にまで文句を言っている')
chk(app._pipeline_accepts('文字列', ('source_mime',)) == (),
    '1d: 署名を調べられない相手で落ちている')


class _Pipe:
    """process_pdf_to_neo を1つだけ持つ、pipeline のふり。"""

    def __init__(self, fn):
        self.calls = []
        self._fn = fn

        def _wrapped(path, **kw):
            self.calls.append(kw)
            return fn(path, **kw)

        _wrapped.__signature__ = inspect.signature(fn)
        self.process_pdf_to_neo = _wrapped


# ── 2. 受けられない引数が1つでもあれば断ること ──────────────────────
# **黙って落としてはいけない。** is_tax_inclusive が落ちれば税込の見積が
# 消費税ぶん膨らみ、expenses が落ちればレッカー代が1円も入らない。
# source_mime が落ちれば写真を PDF として読もうとして中身が拾えない。
BASE = dict(addata_root='', template_path=None, mode_override='A',
            model_name=None, api_key=None, cache_scope='',
            is_tax_inclusive=True, merge_mode=False,
            expenses={'towing': 15000})

for _mime, _what in (('application/pdf', 'PDF'), ('image/jpeg', '写真')):
    _p = _Pipe(_old_sig)
    try:
        app._call_pipeline(_p, 'x', source_mime=_mime, **BASE)
        chk(False, f'2: {_what} なのに古い版へ渡してしまっている')
    except RuntimeError as e:
        chk('再起動' in str(e), f'2b: 直し方が伝わらない文面: {e}')
        chk('source_mime' in str(e), f'2c: 何が渡せないのか書いていない: {e}')
    except Exception as e:      # noqa: BLE001
        chk(False, f'2d: 想定外の例外 {type(e).__name__}: {e}')
    chk(not _p.calls, f'2e: {_what} で断ったのに呼んでしまっている')

_p = _Pipe(_very_old)
try:
    app._call_pipeline(_p, 'x', source_mime='application/pdf', **BASE)
    chk(False, '2f: 金額に関わる引数を受けられない相手に渡している')
except RuntimeError as e:
    for _bad in ('is_tax_inclusive', 'expenses', 'merge_mode', 'cache_scope',
                 'mode_override', 'model_name', 'api_key'):
        chk(_bad in str(e), f'2g: 断る理由に {_bad} が挙がっていない: {e}')
except Exception as e:      # noqa: BLE001
    chk(False, f'2h: 想定外の例外 {type(e).__name__}: {e}')
chk(not _p.calls, '2i: 断ったのに呼んでしまっている')

# ── 3. 新しい版なら、すべてそのまま渡ること ─────────────────────────
_p = _Pipe(_new_sig)
r = app._call_pipeline(_p, 'x.jpg', source_mime='image/jpeg', **BASE)
chk(r.get('ok') is True, '3: 新しい版で通らない')
chk(r.get('source_mime') == 'image/jpeg', '3b: 写真の種類が渡っていない')
chk(r.get('is_tax_inclusive') is True, '3c: 税込の指定が渡っていない')
chk(r.get('expenses') == {'towing': 15000}, '3d: 諸経費が渡っていない')

# ── 4. 中身だけ変わった場合（引数は同じ）も見つけること ──────────────
# ここが今回の肝。例外にならないので、黙って古い規則で .neo が出る。
_probe_path = os.path.join(R, 'tests', '_ver_probe.py')
with open(_probe_path, 'w', encoding='utf-8') as f:
    f.write('X = 1\n')
_fake = types.ModuleType('_ver_probe')
_fake.__file__ = _probe_path
app._stamp_module(_fake)
chk(bool(getattr(_fake, '__app_src_digest__', None)), '4: 指紋を付けられていない')
_loaded, _disk = app._module_src_state(_fake)
chk(_loaded == _disk, '4b: 付けた直後なのに食い違っている')
with open(_probe_path, 'w', encoding='utf-8') as f:
    f.write('X = 2\n')          # 中身だけ差し替え（引数は関係ない）
_loaded, _disk = app._module_src_state(_fake)
chk(_loaded != _disk, '4c: 中身が変わったのに気づいていない（黙って古い規則で出る）')

# いま動いているモジュールを揃えたら、ずれ扱いされないこと
for _n in app._APP_MODULES:
    _m = sys.modules.get(_n)
    if _m is not None:
        app._stamp_module(_m)
chk(app._stale_modules() == [], f'4d: 揃えた直後なのにずれ扱い {app._stale_modules()}')

# ── 5. 揃えられないときは断ること（古いまま走らせない） ──────────────
sys.modules['_ver_probe'] = _fake
_bak_list = app._APP_MODULES
app._APP_MODULES = ('_ver_probe',)
try:
    chk(app._stale_modules() == ['_ver_probe'], '5: ずれを見つけられていない')
    # 変換中は差し替えず、ずれたままだと知らせること
    with app._conversion_guard():
        chk(app.sync_app_modules() == ['_ver_probe'],
            '5b: 変換中にモジュールを差し替えている（足元で規則が変わる）')
    # 変換中でなければ揃うこと
    chk(app.sync_app_modules() == [], '5c: 揃えられていない')
    chk(getattr(sys.modules['_ver_probe'], 'X', None) == 2,
        '5d: 読み直したのに中身が古いまま')
    # 揃わないときは pipeline を渡さず断ること
    sys.modules['_ver_probe'].__app_src_digest__ = 'ずれたまま'
    with app._conversion_guard():
        try:
            app._load_pipeline()
            chk(False, '5e: 揃っていないのに pipeline を返している')
        except RuntimeError as e:
            chk('再起動' in str(e), f'5f: 直し方が伝わらない文面: {e}')
        except Exception as e:      # noqa: BLE001
            chk(False, f'5g: 想定外の例外 {type(e).__name__}: {e}')
finally:
    app._APP_MODULES = _bak_list
    sys.modules.pop('_ver_probe', None)
    try:
        os.remove(_probe_path)
    except OSError:
        pass

# ── 6. 変換中の数え方（後始末が漏れると以後ずっと差し替え不能になる） ──
chk(app._active_conversions == 0, '6: 変換の数え方がずれている')
_seen = []


def _worker():
    with app._conversion_guard():
        _seen.append(app._active_conversions)


_t = threading.Thread(target=_worker)
_t.start()
_t.join()
chk(_seen == [1], f'6b: 変換中の数え方がおかしい {_seen}')
chk(app._active_conversions == 0, '6c: 変換のあと数が戻っていない')
try:
    with app._conversion_guard():
        raise ValueError('ためし')
except ValueError:
    pass
chk(app._active_conversions == 0,
    '6d: 例外のあと数が戻っていない（以後ずっと差し替えできなくなる）')

# ── 7. 実際の呼び出しと、渡すつもりの一覧が合っていること ────────────
# ここがずれると、古い版の検出が効かなくなる（今回の不具合の再来）。
with open(os.path.join(R, 'app.py'), encoding='utf-8') as f:
    _all = f.read()
_call = 'result = _call_pipeline('
_i = _all.find(_call)
_src = _all[_i:_all.find(chr(10) + '        )', _i)] if _i >= 0 else ''
chk(_all.count(_call) == 1, '7: pipeline の呼び出しが1か所に特定できない')
chk(bool(_src), '7a: 呼び出し箇所が見つからない')
if _src:
    _missing = {k for k in app._PIPE_ARGS_EXPECTED if (k + '=') not in _src}
    chk(not _missing,
        f'7b: _PIPE_ARGS_EXPECTED に実際は渡していない引数がある {sorted(_missing)}')
    _actual = set(re.findall(r'^\s+([a-z_]+)=', _src, re.M))
    _unlisted = _actual - set(app._PIPE_ARGS_EXPECTED)
    chk(not _unlisted,
        f'7c: 渡しているのに _PIPE_ARGS_EXPECTED に無い引数がある {sorted(_unlisted)}')

# ── 8. 揃える対象に、生成に関わるモジュールが入っていること ───────────
# 抜けると、そのモジュールだけ古いまま黙って動く。
for _need in ('pdf_to_neo_pipeline', 'neo_rules', 'neo_header',
              'auto_matching', 'addata_settings', 'addata_locator',
              '_addata_db_search', '_grade_identifier',
              'addata_vehicle_resolver',
              # pipeline は `from app import generate_neo_file` と
              # **生成の本体を app から取っている**。画面側は __main__ で
              # 動くので、sys.modules['app'] は別の二重読み込みであり
              # 独立に古くなる。ここが抜けると、古い生成本体で .neo が出る。
              'app'):
    chk(_need in app._APP_MODULES, f'8: 揃える対象に {_need} が入っていない')
# 読み直してはいけないものが入っていないこと。
# _pdfium_lock_mod は pdfium のロックをプロセスで1つにするためだけの
# モジュールで、読み直すと別のロックができて共有の意味が消える
# （Cヒープが壊れてプロセスごと落ちる）。
for _never in app._APP_MODULES_NEVER_RELOAD:
    chk(_never not in app._APP_MODULES,
        f'8a: 読み直してはいけない {_never} が対象に入っている')
chk('_pdfium_lock_mod' in app._APP_MODULES_NEVER_RELOAD,
    '8a2: _pdfium_lock_mod が「読み直さない」一覧から外れている')
# 依存の浅い順（先に読み直したものを、あとのものが取り込む）


def _order(first, second, msg):
    """first が second より先に並んでいること。片方が無い場合も理由を出す。"""
    ms = app._APP_MODULES
    if first not in ms:
        FAIL.append(f'{msg}（{first} が一覧に無い）')
    elif second not in ms:
        FAIL.append(f'{msg}（{second} が一覧に無い）')
    elif ms.index(first) >= ms.index(second):
        FAIL.append(msg)


_order('neo_rules', 'auto_matching',
       '8b: neo_rules より先に auto_matching を読み直している')
_order('auto_matching', 'pdf_to_neo_pipeline',
       '8c: auto_matching より先に pipeline を読み直している')
_order('neo_rules', 'pdf_to_neo_pipeline',
       '8d: neo_rules より先に pipeline を読み直している')
_order('app', 'pdf_to_neo_pipeline',
       '8e: app より先に pipeline を読み直している'
       '（pipeline は app から生成の本体を取る）')

# ── 9. 変換は必ず「変換中」の印の中で行うこと ───────────────────────
_cp = inspect.getsource(app._call_pipeline)
chk('_conversion_guard()' in _cp,
    '9: 変換を「変換中」の印で囲んでいない（途中でモジュールを差し替えられる）')
_fu = inspect.getsource(app._addata_from_url)
chk('_conversion_guard()' in _fu,
    '9a: Addata の取得を「変換中」の印で囲んでいない'
    '（最大120秒の取得中に addata_settings を差し替えられる）')
_lp = inspect.getsource(app._load_pipeline)
chk('sync_app_modules' in _lp, '9b: 版を揃えずに pipeline を返している')
chk('raise RuntimeError' in _lp, '9c: 揃わないのに断っていない')

print('REG_PIPEVER:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL:
    print('  -', f)
sys.exit(1 if FAIL else 0)
