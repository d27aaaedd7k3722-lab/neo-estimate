# -*- coding: utf-8 -*-
"""事故・保険情報と車検証情報が、**どちらの入口でも** .neo に入ることの回帰テスト。

本番で「車検証情報が全く反映していません」と指摘された（2026-09-11）。
調べると、**一発生成の経路（「見積書からNEOを生成」）だけ**
事故・保険情報が1つも入っていなかった。

  画面 → run_pdf_to_neo_pipeline → process_pdf_to_neo
       → build_neo_mode_a/b/c → _call_generate_neo → generate_neo_file

この道のどこかで落とすと、同じ見積なのに**入口によって中身の違う .neo**が出る。
コグニセブンの保険欄が空のまま保険会社に出ることになるので、静かに困る。

**検証用のダミー値だけを使う。実在の人物の情報は扱わない。**
"""
import inspect
import os
import sqlite3
import sys
import tempfile

R = os.environ.get('XROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, R)
os.chdir(R)
sys.stdout.reconfigure(encoding='utf-8')

import addata_locator  # noqa: E402
addata_locator.find_addata = lambda *a, **kw: None
import app  # noqa: E402
import pdf_to_neo_pipeline as P  # noqa: E402

FAIL = []


def chk(cond, msg):
    if not cond:
        FAIL.append(msg)


TPL = open(os.path.join(R, 'template_toyota.neo'), 'rb').read()

INS = {
    'policy_no': 'ZZPOLICY123',
    'contractor_name': 'ｹﾝｼｮｳｹｲﾔｸｼｬ',
    'accept_no': 'ZZ-2026-0001',
    'accident_date': '20260901',
    'agency_name': '検証火災海上',
    'adjuster_name': 'ｹﾝｼｮｳｱｼﾞｬｽﾀｰ',
    'garage_in_date': '20260902',
    'garage_out_date': '20260910',
    'repair_days': 8,
}
CUST = {
    'customer_name': 'ｹﾝｼｮｳﾀﾛｳ',
    'owner_name': 'ｹﾝｼｮｳﾊﾅｺ',
    'postal_no': '8000000',
    'prefecture': '福岡県',
    'municipality': '北九州市小倉北区',
    'address_other': '検証町1-2-3',
    'car_reg_department': '北九州',
    'car_reg_division': '300',
    'car_reg_business': 'ｱ',
    'car_reg_serial': '1234',
    'car_serial_no': 'MXPJ10-0001234',
    'car_name': 'ﾔﾘｽｸﾛｽ',
    'body_color': 'ｼﾛ',
    'color_code': '089',
    'trim_code': 'FA20',
    'car_model_designation': '18164',
    'car_category_number': '0010',
    'term_date': '20280315',
    'car_reg_date': '20190300',
}
ITEMS = [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600,
          'wage': 0, 'quantity': 1}]


def em_values(nb):
    """AnSvEm0001Ex.db の中身を {テーブル.列: 値} で返す。"""
    ck = app.find_real_cks(nb)
    full = app.decompress_neo(nb, ck)
    _m, ent = app.parse_entries(nb, ck[0])
    fs = app.extract_files(full, ent)
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    out = {}
    try:
        tf.write(fs['AnSvEm0001Ex.db'])
        tf.close()
        con = sqlite3.connect(tf.name)
        con.text_factory = bytes
        try:
            for table in ('Customer', 'Car', 'Insurance', 'FileInfo'):
                try:
                    cols = [c[1].decode('utf-8', 'replace')
                            if isinstance(c[1], bytes) else c[1]
                            for c in con.execute('PRAGMA table_info(%s)' % table)]
                    row = con.execute('select * from %s' % table).fetchone()
                except sqlite3.Error:
                    continue
                if not row:
                    continue
                for c, v in zip(cols, row):
                    if isinstance(v, bytes):
                        v = v.decode('utf-8', 'replace').rstrip('\x00').strip()
                    out['%s.%s' % (table, c)] = v
        finally:
            con.close()
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass
    return out


# 保険欄に入るべき値 → .neo の列
WANT_INS = {
    'Insurance.PolicyNo': 'ZZPOLICY123',
    'Insurance.ContractorName': 'ｹﾝｼｮｳｹｲﾔｸｼｬ',
    # 事故受付番号は Insurance ではなく FileInfo に入る
    'FileInfo.AcceptNo': 'ZZ-2026-0001',
    'FileInfo.GarageInDate': '20260902',
    'FileInfo.GarageOutDate': '20260910',
    'Insurance.AccidentDate': '20260901',
    'Insurance.AgencyName': '検証火災海上',
    'Insurance.AdjusterName': 'ｹﾝｼｮｳｱｼﾞｬｽﾀｰ',
}
WANT_CUST = {
    'Customer.Name1': 'ｹﾝｼｮｳﾀﾛｳ',
    'Customer.UserName': 'ｹﾝｼｮｳﾀﾛｳ',
    'Customer.OwnerName': 'ｹﾝｼｮｳﾊﾅｺ',
    'Customer.PostalNo': '8000000',
    'Customer.Prefecture': '福岡県',
    'Customer.Municipality': '北九州市小倉北区',
    'Customer.AddressOther1': '検証町1-2-3',
    'Customer.CarRegNoDepartment': '北九州',
    'Customer.CarRegNoDivision': '300',
    'Customer.CarRegNoBusiness': 'ｱ',
    'Customer.CarRegNoSerial': '1234',
    'Customer.CarSerialNo': 'MXPJ10-0001234',
    'Customer.CarMouldNo': '18164',
    'Customer.CarKindNo': '0010',
    'Car.CarName': 'ﾔﾘｽｸﾛｽ',
    'Car.ColorName': 'ｼﾛ',
    'Car.ColorCode': '089',
    'Car.TrimCode': 'FA20',
}

# ── 1. 画面（プレビュー）経由 = generate_neo_file を直接呼ぶ道 ────────
nb1 = app.generate_neo_file(TPL, CUST, [dict(i) for i in ITEMS], 0, INS, {},
                            False, False, False)[0]
v1 = em_values(nb1)
for col, want in list(WANT_INS.items()) + list(WANT_CUST.items()):
    got = v1.get(col)
    if got is None:
        chk(False, f'1: プレビュー経由で列 {col} が見当たらない')
    else:
        chk(got == want, f'1: プレビュー経由 {col} が {got!r}（期待 {want!r}）')

# ── 2. 一発生成の経路 = pipeline を通る道 ──────────────────────────
# ここが今回の不具合。process_pdf_to_neo が insurance_info を受けず、
# generate_neo_file には空の辞書が固定で渡されていた。
nb2 = P.build_neo_mode_a(
    [dict(i) for i in ITEMS],
    vehicle_info={},
    template_path=os.path.join(R, 'template_toyota.neo'),
    customer_info=dict(CUST),
    insurance_info=dict(INS))
v2 = em_values(nb2)
for col, want in WANT_INS.items():
    got = v2.get(col)
    if got is None:
        chk(False, f'2: 一発生成の経路で列 {col} が見当たらない')
    else:
        chk(got == want,
            f'2: 一発生成の経路で {col} が {got!r}（期待 {want!r}）'
            '— 保険欄が空のまま保険会社に出てしまう')
for col, want in WANT_CUST.items():
    got = v2.get(col)
    if got is not None:
        chk(got == want, f'2b: 一発生成の経路で {col} が {got!r}（期待 {want!r}）')

# ── 3. 両方の入口で同じ中身になること ──────────────────────────────
# 入口が違うだけで中身の違う .neo が出るのがいちばん困る。
diff = [c for c in WANT_INS if v1.get(c) != v2.get(c)]
chk(not diff, f'3: 入口によって保険欄の中身が違う {diff}')

# ── 4. 道の途中で落とさないこと（ソースでも縛る） ───────────────────
for fn in (P.process_pdf_to_neo, P.build_neo_mode_a, P.build_neo_mode_b,
           P.build_neo_mode_c, P._call_generate_neo):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        chk(False, f'4: {fn.__name__} の引数を調べられない')
        continue
    chk('insurance_info' in params,
        f'4: {fn.__name__} が insurance_info を受けない（途中で落ちる）')

_src = inspect.getsource(P._call_generate_neo)
chk('insurance_info={}' not in _src,
    '4b: _call_generate_neo が空の辞書を固定で渡している（今回の不具合そのもの）')
chk('insurance_info=insurance_info' in _src,
    '4c: _call_generate_neo が受け取った値を渡していない')

# 画面が pipeline に渡しているか
with open(os.path.join(R, 'app.py'), encoding='utf-8') as f:
    _app = f.read()
_call = _app.split('result = _call_pipeline(')[1].split('\n        )')[0]
chk('insurance_info=' in _call,
    '4d: app.py が pipeline に insurance_info を渡していない')
chk('insurance_info' in app._PIPE_ARGS_EXPECTED,
    '4e: _PIPE_ARGS_EXPECTED に insurance_info が無い（古い版の検出から漏れる）')
# 2026-09-13〜 見積 PDF→NEO は pdf-to-neo スキル経路（run_pdf_to_neo_skill）。画面はサイドバーの保険情報を
# _sidebar_insurance_hint() で reading の insurance に渡し、vendor の生成器が Insurance / FileInfo / XML に書く。
# 「定義」ではなく「呼び出し」を拾う（定義を拾うと既定値を引数と誤認する）
_marker = "_p2n_kw = dict("   # 一発生成と PC の Addata 橋渡し（p2n_read / p2n_make）が共有する引数（2026-09-14）
chk(_app.count(_marker) == 1, '4f0: 一発生成の呼び出しが1か所に特定できない')
_tail = chr(10) + ' ' * 24 + ')'
_ui = _app.split(_marker)[1].split(_tail)[0] if _marker in _app else ''
# 2026-09-14: 添付の書類（速報報告書）から読んだ保険情報とサイドバーの値を合わせた _p2n_ihint を渡す形も可
# （_p2n_ihint はブロックの直前で _sidebar_insurance_hint() から作られていること）
_pre = _app.split(_marker)[0][-1500:] if _marker in _app else ''
_now_src = inspect.getsource(app._insurance_hint_now) if hasattr(app, '_insurance_hint_now') else ''
chk(('insurance_hint=_sidebar_insurance_hint()' in _ui)
    or ('insurance_hint=_p2n_ihint' in _ui and '_p2n_ihint = ' in _pre
        and ('_sidebar_insurance_hint()' in _pre or ('_insurance_hint_now(' in _pre and '_sidebar_insurance_hint()' in _now_src))),
    '4f: 画面が run_pdf_to_neo_skill に事故・保険情報（insurance_hint）を渡していない')
_hint_src = inspect.getsource(app._sidebar_insurance_hint)
for _k in ('accept_no', 'policy_no', 'contractor_name', 'agency_name', 'adjuster_name',
           'garage_in_date', 'garage_out_date', 'repair_days', 'accident_date'):
    chk(_k in _hint_src, f'4g: 画面が渡す事故・保険情報に {_k} が含まれていない')
for _k in ('accept_no', 'agency', 'adjuster', 'garage_in', 'garage_out', 'repair_days'):
    chk(f"'{_k}'" in _hint_src, f'4h: reading.insurance のキー {_k}（生成器が読む名前）で渡していない')

# ── 5. 保険情報を変えたら、作り直した .neo も変わること ────────────────
# 同じ見積書の番号だけ直して出し直すのは普通にある。キャッシュのキーに
# 保険情報が入っていないと、**前の保険情報のままの .neo** が返り、
# 別案件の事故番号が入ったファイルをそのまま出してしまう。
_ck = inspect.getsource(P.process_pdf_to_neo)
_key = _ck.split('cache_key = "|".join([')[1].split('])')[0] if 'cache_key = "|".join([' in _ck else ''
chk(bool(_key), '5: キャッシュのキーの組み立てが見つからない')
chk('insurance_info' in _key,
    '5b: キャッシュのキーに保険情報が入っていない'
    '（番号を入れ直しても前の .neo が返る）')
chk('expenses' in _key, '5c: キャッシュのキーに費用が入っていない')
chk('vehicle_info' in _key, '5d: キャッシュのキーに車検証・書類の vehicle_info が入っていない（車検証を差し替えても前の .neo が返る）')
chk('if pdf_bytes and not vehicle_info and not items and not skip_ocr:' in _ck,
    '5e: vehicle_info があるときは旧経路のキャッシュを通らない、という守りが外れている')

# ── 6. 登録番号は実機 NEO と同じ半角数字・ハイフン無しで書くこと ─────────
# 実機 NEO 108 本（登録番号あり）は分類番号・一連番号がすべて半角数字、かなは全角ひらがな（2026-09-14 集計）。
# 旧来の車検証 OCR は全角数字で返していたので、DB・XML・INI に書く直前（_trimmed_cust_values）で揃える
_tv = app._trimmed_cust_values({'car_reg_department': '北九州', 'car_reg_division': '３４６', 'car_reg_business': 'の', 'car_reg_serial': '１２-２４'})
chk(_tv['car_div'] == '346' and _tv['car_serial'] == '1224' and _tv['car_biz'] == 'の' and _tv['car_dept'] == '北九州',
    f"6: 登録番号が半角・ハイフン無しになっていない: {_tv['car_dept']!r} {_tv['car_div']!r} {_tv['car_biz']!r} {_tv['car_serial']!r}")
_tv2 = app._trimmed_cust_values({'car_reg_division': '30A', 'car_reg_serial': '・・12', 'car_reg_business': 'ｱ'})
chk(_tv2['car_div'] == '30A' and _tv2['car_serial'] == '12' and _tv2['car_biz'] == 'ｱ',
    f"6b: 英字入り分類番号と「・」付き一連番号、かなは幅を変えない: {_tv2['car_div']!r} {_tv2['car_serial']!r} {_tv2['car_biz']!r}")

# ── 7. 車検証 OCR が confidence だけ返したら「読めなかった」こと（Codex 66）──────
# 車検証のページが無い画像を渡すと Gemini は全項目空＋confidence だけの JSON を返すことがある。
# これを成功として控えると、空の vehicle_info で顧客・車両欄の空いた NEO が黙って作られる
class _FakeResp:
    text = '{"customer_name": "", "car_name": "", "car_serial_no": "", "confidence": "0.2"}'
class _FakeModels:
    def generate_content(self, **kw):
        return _FakeResp()
class _FakeClient:
    models = _FakeModels()
_orig_client, _orig_call = app._get_genai_client, app.call_gemini
app._get_genai_client = lambda api_key: _FakeClient()
app.call_gemini = lambda *a, **kw: _FakeResp.text
try:
    _r7 = app.analyze_vehicle_registration('dummy-key', b'x', 'image/png', 'dummy-model')
finally:
    app._get_genai_client, app.call_gemini = _orig_client, _orig_call
chk(isinstance(_r7, dict) and bool(_r7.get('_error')), f'7: confidence だけの車検証 OCR が成功扱い: {_r7!r}')
chk(app._shaken_has_data({'car_name': 'トヨタ', 'confidence': '0.9'}) and not app._shaken_has_data({'confidence': '0.9', 'car_name': ''})
    and not app._shaken_has_data({'_error': 'x'}) and not app._shaken_has_data({}) and not app._shaken_has_data(None),
    '7b: _shaken_has_data の判定')

# ── 8. 書類の値をサイドバーに入れる計画（_doc_fill_plan。Codex 62/67）─────────
# 空欄か前に書類から入れたままの項目にだけ入れ、手で直した値は書き換えない。読み直しで無かった項目も、前に入れたままなら追跡を続ける
_u, _n = app._doc_fill_plan({'accept_no': 'A2'}, {'accept_no': 'A1', 'adjuster_name': 'B1'},
                            {'accept_no': 'A1', 'adjuster_name': 'B1', 'contractor_name': '手入力'})
chk(_u == {'accept_no': 'A2'} and _n == {'accept_no': 'A2', 'adjuster_name': 'B1'},
    f'8: 読み直しで無かった adjuster_name の追跡が外れた／入れる範囲が違う: {_u!r} {_n!r}')
_u, _n = app._doc_fill_plan({'accept_no': 'A2', 'adjuster_name': 'B2'}, {'accept_no': 'A1', 'adjuster_name': 'B1'},
                            {'accept_no': 'A1', 'adjuster_name': '直した'})
chk(_u == {'accept_no': 'A2'} and _n == {'accept_no': 'A2'}, f'8b: 手で直した値を書き換えた／追跡した: {_u!r} {_n!r}')
_u, _n = app._doc_fill_plan({'policy_no': 'P'}, {}, {'policy_no': ''})
chk(_u == {'policy_no': 'P'} and _n == {'policy_no': 'P'}, f'8c: 空欄に入らない: {_u!r} {_n!r}')
_u, _n = app._doc_fill_plan({}, {'accept_no': 'A1'}, {'accept_no': 'A1'})
chk(_u == {} and _n == {'accept_no': 'A1'}, f'8d: 読めなかったときも前の自動入力の追跡を続ける: {_u!r} {_n!r}')
_u, _n = app._doc_fill_plan({'accept_no': 'A2'}, {'accept_no': 'A1'}, {'accept_no': ''})
chk(_u == {'accept_no': 'A2'} and _n == {'accept_no': 'A2'}, f'8e: 利用者が消した欄には入れ直す（同じ書類の読み直し）: {_u!r} {_n!r}')

print('REG_INSURANCE:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL:
    print('  -', f)
sys.exit(1 if FAIL else 0)
