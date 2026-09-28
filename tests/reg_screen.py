# -*- coding: utf-8 -*-
"""画面の③④を Streamlit の AppTest で通す回帰テスト（API は呼ばない。2026-09-22 バグハント第 4 弾）。

③④の直しを「ソースにその文字列があるか」で見るテストは、挙動を元に戻しても通っていた（第 4 弾 A5: 8 通りの戻しを
どれも見逃した）。ここでは本物の画面の流れ（session_state を②の直後の形にして描く → ボタン → ④の NEO の中身）で確かめる。

  S1 車検証だけ（明細なし）の③が落ちない（estimate_data が None。2026-09-10 から AttributeError で落ちていた）
  S2 車検証だけの NEO にも、サイドバーの費用は③でチェックを入れたときだけ入る（B2 の取りこぼし）
  S3 CSV 取り込み: サイドバーの費用は③でチェックを入れたときだけ入る（B2）
  S4 プレビュー取り込み: ③で品名を半角カナにしても、材料系の品名（ｳﾚﾀﾝ…）の区分確認が出る（A2）

  XROOT=<別のツリー> で、そのツリーの app.py に対して回せる"""
from __future__ import annotations

import copy
import logging
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get('XROOT') or os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ['NEO_ADDATA_NO_AUTODETECT'] = '1'
# キーは**空文字を入れておく**（消すだけだと app の import の load_dotenv が .env から読み直す。load_dotenv は入っている値を上書きしない）
for _k in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'ANTHROPIC_API_KEY', 'APP_PASSCODE'):
    os.environ[_k] = ''
sys.dont_write_bytecode = True
logging.getLogger('streamlit').setLevel(logging.CRITICAL)

import app  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

if app.GEMINI_API_KEY or app.ANTHROPIC_API_KEY:   # st.secrets などから本物のキーが入った: 課金の道に入らないよう止める
    print('reg_screen: API キーが読み込まれたので止めます（.streamlit/secrets.toml などを外して回してください）')
    sys.exit(2)

FAILS: list = []


def chk(cond, msg):
    if not cond:
        FAILS.append(msg)
        print('*** FAILED:', msg)


def new_at():
    at = AppTest.from_file(os.path.join(ROOT, 'app.py'), default_timeout=240)
    for _k in ('GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'APP_PASSCODE'):
        at.secrets[_k] = ''   # secrets.toml があってもキーを入れない
    return at


def errors(at) -> list:
    return [str(getattr(e, 'value', e))[:200] for e in at.exception] + [str(e.value)[:200] for e in at.error]


def expense_rows(neo) -> list:
    ck = app.find_real_cks(neo)
    f = app.extract_files(app.decompress_neo(neo, ck), app.parse_entries(neo, ck[0])[1])
    c = sqlite3.connect(':memory:')
    c.deserialize(f['AnSMB.txt'])
    return c.execute('SELECT LineNo, WageOutTax FROM Expense WHERE WageOutTax > 0 ORDER BY LineNo').fetchall()


def neo_of(at):
    return at.session_state['neo_bytes'] if 'neo_bytes' in at.session_state else None


def click(at, text):
    b = [x for x in at.button if text in x.label]
    if not b:
        return False
    b[0].click().run()
    at.run()
    return True


# ── S1・S2: 車検証だけ（明細なし） ─────────────────────────────────────────────

def vehicle_only(extra):
    at = new_at()
    ss = at.session_state
    ss['step'] = 3
    ss['vehicle_data'] = {'car_name': 'ﾃｽﾄ'}
    ss['estimate_data'] = None
    ss['_estimate_token'] = app._make_estimate_token(None, b'veh', None)
    ss['selected_mode'] = 'beta'
    for k, v in extra.items():
        ss[k] = v
    at.run()
    return at


def test_vehicle_only():
    at = vehicle_only({'exp_towing': 20000})
    chk(not errors(at), f'S1: 車検証だけ（明細なし）の③で例外・エラー: {errors(at)}')
    cb = [c for c in at.checkbox if 'サイドバーの費用' in c.label]
    chk(len(cb) == 1, f'S2: 車検証だけの③にサイドバーの費用のチェックが無い: {[c.label for c in at.checkbox]}')
    chk(click(at, 'NEOファイルを生成する'), 'S1: 車検証だけの③に生成ボタンが無い')
    neo = neo_of(at)
    chk(neo is not None, f'S1: 車検証だけで NEO ができない: {errors(at)}')
    if neo:
        chk(expense_rows(neo) == [], f'S2: チェックを入れていないサイドバーの費用が NEO に入った: {expense_rows(neo)}')
    if cb:
        at = vehicle_only({'exp_towing': 20000})
        [c for c in at.checkbox if 'サイドバーの費用' in c.label][0].check().run()
        click(at, 'NEOファイルを生成する')
        neo = neo_of(at)
        rows = expense_rows(neo) if neo else None
        chk(bool(rows) and any(w == 20000 for _l, w in rows), f'S2: チェックを入れたサイドバーの費用が NEO に入らない: {rows}')


# ── S2b: 明細 0 行の見積（ベタ打ち）も「明細の無い NEO」として扱う（レビュー 5 周目 P1） ─────────────

def test_zero_item_estimate():
    ed = {'items': [], 'discount_amount': 0, 'short_parts_wage': 0, 'confidence': 1.0, 'pdf_parts_total': 0,
          'pdf_wage_total': 0, 'pdf_grand_total': 0, '_is_tax_inclusive': False, '_page_count': 1, '_vehicle_info': {},
          '_repair_shop_name': ''}
    at = new_at()
    ss = at.session_state
    ss['step'] = 3
    ss['vehicle_data'] = {'car_name': 'ﾃｽﾄ'}
    ss['estimate_data'] = ed
    ss['_estimate_token'] = app._make_estimate_token([], b'veh', None)
    ss['selected_mode'] = 'beta'
    ss['exp_towing'] = 20000
    at.run()
    chk(not errors(at), f'S2b: 明細 0 行の見積の③で例外・エラー: {errors(at)}')
    cb = [c for c in at.checkbox if 'サイドバーの費用' in c.label]
    chk(len(cb) == 1, f'S2b: 明細 0 行の③にサイドバーの費用のチェックが無い: {[c.label for c in at.checkbox]}')
    chk(click(at, 'NEOファイルを生成する'), 'S2b: 明細 0 行の③に生成ボタンが無い')
    neo = neo_of(at)
    chk(neo is not None, f'S2b: 明細 0 行で NEO ができない: {errors(at)}')
    if neo:
        chk(expense_rows(neo) == [], f'S2b: チェックを入れていないサイドバーの費用が NEO に入った: {expense_rows(neo)}')


# ── S3: CSV 取り込みの費用（B2） ─────────────────────────────────────────────

def csv_state(at, csv_text, extra=None):
    """①で CSV を取り込み、②を通った直後（③の最初の描画の前）の session_state（app の②の CSV 経路と同じ形）"""
    items, notes = app.parse_csv_to_items(csv_text, return_notes=True)
    ed = {
        'items': copy.deepcopy(items), 'discount_amount': 0, 'short_parts_wage': 0, 'confidence': 1.0,
        'pdf_parts_total': sum(app.safe_int(it.get('parts_amount', 0)) for it in items),
        'pdf_wage_total': sum(app.safe_int(it.get('wage', 0)) for it in items),
        'pdf_grand_total': 0, '_is_tax_inclusive': False, '_tax_basis': 'tax_exclusive', '_page_count': 1, '_vehicle_info': {},
        '_repair_shop_name': '', '_csv_import': True, '_reverse_match': True, '_csv_import_seq': 1,
    }
    app.apply_addata_matching(ed, {})
    ss = at.session_state
    ss['step'] = 3
    ss['csv_items'] = items
    ss['csv_mode'] = True
    ss['vehicle_data'] = {}
    ss['estimate_data'] = ed
    ss['_estimate_token'] = app._make_estimate_token(items, None, None)
    ss['_csv_import_seq'] = 1
    ss['selected_mode'] = 'beta'
    for k, v in (extra or {}).items():
        ss[k] = v


CSV = '品名,区分,数量,部品金額,工賃,部品コード\nﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,0,\nﾌﾛﾝﾄﾊﾞﾝﾊﾟ脱着,脱着,1,0,8000,\n'


def test_csv_expenses():
    at = new_at()
    csv_state(at, CSV, {'exp_towing': 20000})
    at.run()
    chk(not errors(at), f'S3: CSV の③で例外・エラー: {errors(at)}')
    cb = [c for c in at.checkbox if 'サイドバーの費用' in c.label]
    chk(len(cb) == 1, f'S3: CSV の③にサイドバーの費用のチェックが無い: {[c.label for c in at.checkbox]}')
    click(at, 'NEOファイルを生成する')
    neo = neo_of(at)
    chk(neo is not None, f'S3: CSV で NEO ができない: {errors(at)}')
    if neo:
        chk(expense_rows(neo) == [], f'S3: チェックを入れていないサイドバーの費用が NEO に入った: {expense_rows(neo)}')
    if cb:
        at = new_at()
        csv_state(at, CSV, {'exp_towing': 20000})
        at.run()
        [c for c in at.checkbox if 'サイドバーの費用' in c.label][0].check().run()
        click(at, 'NEOファイルを生成する')
        neo = neo_of(at)
        rows = expense_rows(neo) if neo else None
        chk(bool(rows) and any(w == 20000 for _l, w in rows), f'S3: チェックを入れたサイドバーの費用が NEO に入らない: {rows}')


# ── S4: プレビュー取り込みの区分確認（A2） ─────────────────────────────────────

def test_preview_classification():
    items = [
        {'page': 1, 'name': 'フロントバンパカバー', 'work_code': '取替', 'quantity': 1, 'parts_amount': 45000, 'wage': 8000, 'part_no': '52119-XX'},
        {'page': 1, 'name': 'ウレタンバンパ補修材', 'work_code': '', 'quantity': 1, 'parts_amount': 0, 'wage': 3000},
    ]
    pv = []
    for it in copy.deepcopy(items):
        if not str(it.get('method') or it.get('work_code') or '').strip():
            m = app._infer_method_from_name(it)
            if m:
                it['method'] = it['work_code'] = m
        pv.append(it)
    at = new_at()
    ss = at.session_state
    ss['csv_items'] = pv
    ss['csv_mode'] = True
    ss['pdf2neo_preview_meta'] = {'sig': app._items_sig(pv), 'src': 'file-key-A', 'pdf_parts_total': 45000, 'pdf_wage_total': 11000,
                                  'pdf_grand_total': 61600, 'grand_is_intax': True, 'needs_ack': False, 'exp_declined': False}
    ss['vehicle_file_bytes'] = None
    ss['estimate_file_bytes'] = None
    ss['selected_mode'] = 'beta'
    ss['step'] = 2
    at.run()
    at.run()
    chk(not errors(at), f'S4: プレビュー取り込みの③で例外・エラー: {errors(at)}')
    ed = at.session_state['estimate_data'] if 'estimate_data' in at.session_state else None
    chk(bool(ed) and ed.get('_preview_import'), 'S4: プレビュー取り込みの③に進んでいない')
    pan = [m.value for m in at.markdown if '材料系品名' in m.value]
    chk(bool(pan), 'S4: 品名を半角カナにした③で、材料系の品名（ｳﾚﾀﾝ…）なのに工賃だけの行の区分確認が出ない')


if __name__ == '__main__':
    for fn in (test_vehicle_only, test_zero_item_estimate, test_csv_expenses, test_preview_classification):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            chk(False, f'{fn.__name__}: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-400:])
    print('reg_screen:', 'all ok' if not FAILS else f'{len(FAILS)} 件が不合格')
    for m in FAILS:
        print('  -', m)
    sys.exit(1 if FAILS else 0)
