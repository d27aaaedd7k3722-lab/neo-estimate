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
    # 住所は実機 NEO と同じく市区郡は '〜市' まで、政令市の区は以降側（Codex hunt B2 2026-09-15）
    'Customer.Municipality': '北九州市',
    'Customer.AddressOther1': '小倉北区検証町1-2-3',
    'Customer.CarRegNoDepartment': '北九州',
    'Customer.CarRegNoDivision': '300',
    'Customer.CarRegNoBusiness': 'あ',   # かなは全角ひらがな（実機 108 本すべて。Codex hunt B5）
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
chk(_tv2['car_div'] == '30A' and _tv2['car_serial'] == '12' and _tv2['car_biz'] == 'あ',
    f"6b: 英字入り分類番号と「・」付き一連番号、かなはひらがなに: {_tv2['car_div']!r} {_tv2['car_serial']!r} {_tv2['car_biz']!r}")
_tv3 = app._trimmed_cust_values({'customer_name': '顧客 太郎', 'user_name': '同上', 'car_category_number': '2', 'car_model_designation': '１２３４',
                                 'prefecture': '福岡県', 'municipality': '北九州市小倉北区', 'address_other': '検証町1-2-3'})
chk(_tv3['user_name'] == '同上' and app._trimmed_cust_values({'customer_name': '顧客 太郎'})['user_name'] == '顧客 太郎',
    f"6c: 使用者欄は user_name があればそれ、無ければ顧客名: {_tv3['user_name']!r}")
chk(_tv3['category_num'] == '0002' and _tv3['model_desig'] == '01234', f"6d: 類別 4 桁・型式指定 5 桁: {_tv3['category_num']!r} {_tv3['model_desig']!r}")
chk(_tv3['prefecture'] == '福岡県' and _tv3['municipality'] == '北九州市' and _tv3['address_other'] == '小倉北区検証町1-2-3',
    f"6e: 住所分割: {_tv3['prefecture']!r} {_tv3['municipality']!r} {_tv3['address_other']!r}")

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

# ── 9. 見積書が変わったら案件の入力を消す／結果に入力の指紋（Codex hunt A1/A2/A3 2026-09-15）──
_src9 = inspect.getsource(app._reset_case_inputs)
for _k in ('policy_no', 'contractor_name', 'accept_no', 'adjuster_post', 'exp_towing', '_doc_ocr_cache', '_insdoc_filled',
           'pdf2neo_result', '_beta_exp_file_key', 'pdf2neo_beta_use_exp'):
    chk(_k in inspect.getsource(app).split('_CASE_INPUT_KEYS = (')[1].split(')')[0], f'9: _CASE_INPUT_KEYS に {_k} が無い')
chk("'form_seq'" in _src9 and "'upload_seq'" in _src9 and '_bridge_pending' in _src9, '9b: _reset_case_inputs が入力欄・uploader・取り置きを作り直していない')
_wiz = inspect.getsource(app).split('"🔄 新しい見積を作成する"')[1][:3000]
chk("'_p2n_last_file_key'" in _wiz and "'_p2n_reset_msg'" in _wiz and "'_p2n_last_docs_sig'" in _wiz, '9l: 「新しい見積を作成する」が _p2n_last_file_key / _p2n_last_docs_sig を消していない（Codex 69/71）')
_blk = inspect.getsource(app).split('_p2n_prev_key and _p2n_prev_key != _p2n_early_key')[1][:1800]
chk("_docs_now == _docs_prev" in _blk and "_docs_prev = str(st.session_state.get('_p2n_last_docs_sig')" in _blk and '_reset_case_inputs()' in _blk and "pop('pdf2neo_result'" not in _blk.split('else:')[0],
    '9p: 見積書が変わったとき、添付が前の見積書のときと同じなら消し、変わっていれば残す分岐が無い（Codex 71）')
chk('"user_name"' in inspect.getsource(P._merge_vehicle_into_customer), '9q: 旧経路の _merge_vehicle_into_customer が user_name を落とす（Codex 71）')
_mv = P._merge_vehicle_into_customer({'customer_name': '顧客 太郎', 'user_name': '同上'}, {})
chk(_mv.get('user_name') == '同上', f'9r: user_name が旧経路の merge を通らない: {_mv!r}')
_app_src = inspect.getsource(app)
chk("_p2n_prev_key and _p2n_prev_key != _p2n_early_key" in _app_src and '_reset_case_inputs()' in _app_src.split('_p2n_prev_key and _p2n_prev_key != _p2n_early_key')[1][:700],
    '9c: 見積書が別のファイルに変わったときに _reset_case_inputs を呼んでいない')
_st9 = {'vehicle_upload': None, 'insurance_doc_upload': None, 'contractor_name': 'A', 'repair_days': 0, 'pdf_tax_radio': '税抜き（外税）'}
_sig1 = app._p2n_inputs_signature('f1', _st9)
chk(_sig1 == app._p2n_inputs_signature('f1', dict(_st9)), '9d: 同じ入力なら同じ指紋')
chk(_sig1 != app._p2n_inputs_signature('f2', _st9), '9e: 見積書が違えば指紋が違う')
chk(_sig1 != app._p2n_inputs_signature('f1', dict(_st9, contractor_name='B')), '9f: 事故・保険欄が違えば指紋が違う')
chk(_sig1 != app._p2n_inputs_signature('f1', dict(_st9, pdf2neo_beta_use_exp=True, exp_towing=1000), beta=True), '9g: ベタ打ちで費用のチェックが違えば指紋が違う')
chk(_sig1 == app._p2n_inputs_signature('f1', dict(_st9, pdf2neo_beta_use_exp=True, exp_towing=1000)), '9g4: スキル経路（beta=False）は費用のチェック・額を見ない（Codex 77）')
chk(app._p2n_inputs_signature('f1', _st9, beta=True) == app._p2n_inputs_signature('f1', dict(_st9, exp_towing=1000), beta=True), '9g2: チェックが無いときは費用の額を変えても指紋は同じ（Codex 75）')
chk(app._p2n_inputs_signature('f1', dict(_st9, accident_date='2026/09/01')) == app._p2n_inputs_signature('f1', dict(_st9, accident_date='20260901'))
    and app._p2n_inputs_signature('f1', dict(_st9, accident_date='20260901')) != app._p2n_inputs_signature('f1', dict(_st9, accident_date='20260902')),
    '9g3: 日付の書き方の違いで陳腐化させない／日付が違えば指紋が違う（Codex 76）')
class _FakeUp:
    def __init__(self, b): self._b = b
    def getvalue(self): return self._b
chk(_sig1 != app._p2n_inputs_signature('f1', dict(_st9, vehicle_upload=_FakeUp(b'x'))), '9h: 車検証の添付が違えば指紋が違う')
# Codex 72 [1]: 同じ添付でも OCR の控え（読めた中身）が変われば指紋が変わる（キーを直して読めるようになった等）
import hashlib as _hl
_ck = ('shaken', _hl.sha256(b'x').hexdigest(), 'm', _hl.sha256(b'k').hexdigest()[:12])
_s_no = dict(_st9, vehicle_upload=_FakeUp(b'x'))
_s_ok = dict(_s_no, _doc_ocr_cache={_ck: {'car_name': 'トヨタ'}})
_s_err = dict(_s_no, _doc_ocr_cache={_ck: {'_error': 'x'}})
chk(app._p2n_inputs_signature('f1', _s_no, api_key='k', model_name='m') != app._p2n_inputs_signature('f1', _s_ok, api_key='k', model_name='m'),
    '9t: OCR が読めるようになっても指紋が変わらない')
chk(app._p2n_inputs_signature('f1', _s_no, api_key='k', model_name='m') == app._p2n_inputs_signature('f1', _s_err, api_key='k', model_name='m'),
    '9u: OCR 失敗の控えは「読めていない」と同じ指紋')
_src_rc = inspect.getsource(app._reset_case_inputs)
chk('keep_docs' in _src_rc and '_DOC_STATE_KEYS' in _src_rc and '_insdoc_filled' in _src_rc, '9v: _reset_case_inputs(keep_docs) が書類と書類から入れた欄を残す形になっていない（Codex 72）')
chk('_reset_case_inputs(keep_docs=True)' in inspect.getsource(app).split('_p2n_prev_key and _p2n_prev_key != _p2n_early_key')[1][:1800],
    '9w: 書類を入れ替えて別の見積書を入れたとき、手入力の保険欄を消していない（Codex 72）')
chk("not any(_docs_now.split('|'))" in inspect.getsource(app).split('_p2n_prev_key and _p2n_prev_key != _p2n_early_key')[1][:1800],
    '9y: 添付が無いときに keep_docs の枝に入る（Codex 73）')
chk('_slot_same' in inspect.getsource(app).split('_p2n_prev_key and _p2n_prev_key != _p2n_early_key')[1][:1800],
    '9y2: 片方の書類だけ前のままのときに keep_docs の枝に入る（Codex 76）')
_kd = inspect.getsource(app).split('_reset_case_inputs(keep_docs=True)')[1][:900]
chk("_p2n_deferred_rerun'] = True" in _kd and 'st.rerun()' not in _kd.split("_p2n_deferred_rerun'] = True")[0],
    '9z: 書類を残す枝で uploader を描く前に st.rerun() している（書類が消える）')
chk("st.session_state.pop('_p2n_deferred_rerun', False)" in inspect.getsource(app), '9z2: 保留した描き直し（_p2n_deferred_rerun）が無い')
chk('fv.name' not in inspect.getsource(app._attached_docs_caption) and 'fd.name' not in inspect.getsource(app._attached_docs_caption), '9x: 添付の案内にファイル名を出している（Codex 72）')
_n9i = _app_src.count("['inputs_sig'] = _p2n_inputs_signature(_p2n_file_key, api_key=api_key, model_name=selected_model,")   # ベタ打ち 1 ＋ スキル経路 2（addata_id 付き）
chk(_n9i == 3, f'9i: 結果に指紋を添える箇所が 3 か所ではない: {_n9i}')
chk("_p2n_res = dict(_p2n_res, stale=True)" in _app_src and _app_src.count("disabled=bool(_p2n_res.get('stale'))") == 6,
    '9j: 指紋が違う結果のダウンロード・プレビュー取り込み・修正用 ZIP を止めていない（3 つのダウンロード＋プレビュー＋修正用 ZIP 2 つ）')
chk("custom_neo_bytes" in inspect.getsource(app._p2n_inputs_signature) and "custom_neo_upload" not in inspect.getsource(app._p2n_inputs_signature),
    '9n: 指紋のテンプレートは受け付けた custom_neo_bytes で取る（弾いたファイルで照合しない。Codex 73）')
chk("_tpl_sig_seen" in inspect.getsource(app) and inspect.getsource(app).count("st.session_state['_tpl_sig_seen'] = _tpl_now") == 1,
    '9n2: テンプレートの受け付けが変わったときに描き直していない（Codex 70/73）')
chk(app._p2n_inputs_signature('f1', _st9, beta=True) != app._p2n_inputs_signature('f1', dict(_st9, custom_neo_bytes=b'tpl'), beta=True), '9o: ベタ打ちはテンプレートが違えば指紋が違う')
chk(app._p2n_inputs_signature('f1', _st9) == app._p2n_inputs_signature('f1', dict(_st9, custom_neo_bytes=b'tpl', pdf_tax_radio='税込み（内税）')),
    '9o2: スキル経路はテンプレート・税区分の選択を見ない（Codex 78）')
chk(app._p2n_inputs_signature('f1', _st9, beta=True) != app._p2n_inputs_signature('f1', dict(_st9, pdf_tax_radio='税込み（内税）'), beta=True), '9o3: ベタ打ちは税区分が違えば指紋が違う')
chk(_app_src.count('st.caption(_attached_docs_caption(api_key, selected_model))') == 2, '9k: 添付の案内が読めたかで出ていない（2 か所）')

# ── 10. 実機テスト（2026-09-15）とバグハント D/E/F の採用分 ──────────────────
# R1: 工賃欄が空欄の行
_rd = {'blocks': [{'rows': [{'name': '左 ﾍｯﾄﾞﾗﾝﾌﾟ', 'method': '脱着'}, {'name': 'ﾊﾞﾝﾊﾟ', 'method': '取替', 'index': 1.2, 'wage': 10000, 'price': 30000},
                            {'name': 'ｸﾘｯﾌﾟ', 'price': 100}, {'note': 'メモ'}, {'name': '手入力', 'manual': True}]}]}
chk([n for _, _, n in app._blank_wage_rows(_rd)] == ['左 ﾍｯﾄﾞﾗﾝﾌﾟ'], f'10a: 工賃も指数も無い行の抽出（部品代のある取替行は生成器が 0 円にするので対象外）: {app._blank_wage_rows(_rd)!r}')
chk(app._blank_wage_rows({'blocks': [{'rows': [{'name': 'A', 'method': '取替', 'price': 1}, {'name': 'B', 'method': '脱着'}]}]}) == [],
    '10b: 工賃欄自体が無い書式では抽出しない（標準に任せる）')
chk(app._blank_wage_rows({}) == [] and app._blank_wage_rows(None) == [], '10c: 空')
# 短縮記法（merge 後の rows は文字列）: code|name|method|parts_no|index|qty|price|wage|flags|comment
_rd_s = {'blocks': [{'rows': ['|左 ﾍｯﾄﾞﾗﾝﾌﾟ|脱着|||||||', '0010|ﾊﾞﾝﾊﾟ|取替||1.20|1|30000|10000||', '|ｸﾘｯﾌﾟ|取替|||1|100|||',
                            '|注記です|||||||N|', '|手入力品|取替|||1|500||M|', '|保留|取替||||||R|']}]}
chk([n for _, _, n in app._blank_wage_rows(_rd_s)] == ['左 ﾍｯﾄﾞﾗﾝﾌﾟ'], f'10c2: 短縮記法の行の抽出: {app._blank_wage_rows(_rd_s)!r}')
_z = app._row_with_wage_zero('|左 ﾍｯﾄﾞﾗﾝﾌﾟ|脱着|||||||', '要確認: X')
chk(isinstance(_z, dict) and _z['wage'] == 0 and _z['comment'] == '要確認: X' and _z['name'] == '左 ﾍｯﾄﾞﾗﾝﾌﾟ' and _z['method'] == '脱着' and 'price' not in _z,
    f'10c3: 短縮記法の行に wage 0 とコメント（dict 行に）: {_z!r}')
_zn = app._row_with_wage_zero('|ﾌﾞﾁﾙﾃｰﾌﾟ|脱着|||||||NEO:※JAS在庫使用', '要確認: X')
chk(_zn.get('neo_comment') == '※JAS在庫使用' and _zn['comment'] == '要確認: X' and _zn['wage'] == 0, f'10c3b: NEO: 付きの印字コメントを neo_comment に分ける: {_zn!r}')
_zd2 = app._row_with_wage_zero({'name': 'A', 'method': '脱着', 'comment': 'ＮＥＯ： 印字'}, '要確認: X')
chk(_zd2.get('neo_comment') == '印字' and _zd2['comment'] == '要確認: X', f'10c3c: 全角 ＮＥＯ： も分ける: {_zd2!r}')
_zd = app._row_with_wage_zero({'name': 'A', 'method': '脱着', 'comment': 'c'}, '要確認: X')
chk(_zd['wage'] == 0 and _zd['comment'] == '要確認: X / c', f'10c4: dict に wage 0 とコメント: {_zd!r}')
chk(app._blank_wage_rows({'blocks': [{'rows': ['|A|取替|||1|100|||', '|B|脱着|||||||']}]}) == [], '10c5: 短縮記法でも工賃欄自体が無い書式は抽出しない')
# レビュー 2026-09-15: '-'・'**'（印字の印だけ）は空欄扱い、dict の保留行（R / reserve）は対象外
_rd_m = {'blocks': [{'rows': ['|左 ﾍｯﾄﾞﾗﾝﾌﾟ|脱着|||||**||', '|右 ﾍｯﾄﾞﾗﾝﾌﾟ|脱着|||||-||', '0010|ﾊﾞﾝﾊﾟ|取替||1.20|1|30000|10000||',
                            {'name': '保留 dict', 'method': '取替', 'flags': 'R'}, {'name': '保留2', 'method': '取替', 'reserve': True}]}]}
chk([n for _, _, n in app._blank_wage_rows(_rd_m)] == ['左 ﾍｯﾄﾞﾗﾝﾌﾟ', '右 ﾍｯﾄﾞﾗﾝﾌﾟ'], f'10c6: 印だけの工賃欄と保留行: {app._blank_wage_rows(_rd_m)!r}')
_src_mk0 = inspect.getsource(app.p2n_make)
chk('_first_report' in _src_mk0 and '_first_repair' in _src_mk0 and "out.pop('_first_report_md', None)" in _src_mk0, '10d2: 再試行に失敗したとき 1 回目の報告文・修正用 ZIP を返していない')
chk('_p2n_beta_ui_shown' in _app_src and "not locals().get('_p2n_beta_ui_shown')" in _app_src, '10e2: 同じ run で 2 回ベタ打ち UI を描く（Addata が外れた後）')
chk('blank_wage_retry' in _app_src.split('def _render_beta_result')[0] and "_p2n_res.get('blank_wage_retry')" in _app_src, '10d3: 再試行の失敗を画面に出していない')
# レビュー 2 周目: 「ベタ打ちで作る」は if/elif の連鎖（最後の else = 変換できませんでした）の後ろに置く（中に挟むと合格結果に誤エラー）
chk(_app_src.find('if _p2n_offer_beta:') > _app_src.find("st.error(f\"❌ {_p2n_res.get('error') or '変換できませんでした'}\")")
    and _app_src.find('if _p2n_offer_beta:') - _app_src.find("st.error(f\"❌ {_p2n_res.get('error') or '変換できませんでした'}\")") < 200,
    '10e3: ベタ打ちの逃げ道が結果の if/elif 連鎖の外（最後の else の直後）に無い')
chk('_p2n_beta_ui_shown = False' in _app_src, '10e4: _p2n_beta_ui_shown を初期化していない')
_src_mk = inspect.getsource(app.p2n_make)
chk('_blank_wage_rows(reading)' in _src_mk and 'force_draft=True' in _src_mk and "write_reading(case_dir, reading)" in _src_mk,
    '10d: p2n_make が工賃欄空欄の行を 0 円にして force_draft で作り直していない／失敗時に元へ戻していない')
# R2: ベタ打ちの逃げ道
chk(_app_src.count('_beta_generate_ui(') == 3 and 'fallback=True' in _app_src and '_p2n_offer_beta' in _app_src,
    f"10e: ベタ打ちの UI が共通化されて 2 か所（Addata なし／スキル経路の不合格）から呼ばれていない: {_app_src.count('_beta_generate_ui(')}")
# E1/E4: 差がある結果のダウンロードは確認してから
_src_rb = inspect.getsource(app._render_beta_result)
chk("key='pdf2neo_beta_ack'" in _src_rb and 'or not _p2n_ack' in _src_rb and 'amount_changes' in _src_rb, '10f: ベタ打ちの結果に確認チェックのゲートが無い')
chk("'pdf2neo_beta_ack'" in _app_src.split('_CASE_INPUT_KEYS = (')[1].split(')')[0] and "pop('pdf2neo_beta_ack'" in inspect.getsource(app._beta_generate_ui),
    '10g: 確認チェックが次の結果・次の案件に持ち越される')
# E2/E3
chk('_tax{int(bool(tax_inclusive))}' in inspect.getsource(app.analyze_estimate), '10h: OCR 結果キャッシュのキーに税区分が無い')
chk('(out.get("verify") or {}).get("ok") is True' in inspect.getsource(P.process_pdf_to_neo) and 'it.get("is_adjustment_row") for it in (out.get("items")' in inspect.getsource(P.process_pdf_to_neo),
    '10i: 検証に落ちた結果をパイプラインのキャッシュに残している')
chk('out["amount_changes"]' in inspect.getsource(P.process_pdf_to_neo), '10j: 金額列の補正を結果に載せていない')
# F1: 指紋に Addata
chk(app._p2n_inputs_signature('f1', _st9, addata_id='C:/Addata|2026/09') != app._p2n_inputs_signature('f1', _st9, addata_id=''), '10k: Addata が違えば指紋が違う')
chk(_app_src.count('addata_id=_p2n_addata_identity(_p2n_addata)') == 2 and "addata_id='' if _p2n_is_beta else _p2n_addata_identity(find_addata_dir())" in _app_src,
    '10l: スキル経路の結果と照合に Addata の同一性が入っていない')
# F2: vendor の subprocess に秘密情報を渡さない
from neo_skill import vendor as _vend
os.environ['NEO_TEST_API_KEY'] = 'x'; os.environ['NEO_TEST_PLAIN'] = 'y'
_env = _vend.subprocess_env()
chk('NEO_TEST_API_KEY' not in _env and _env.get('NEO_TEST_PLAIN') == 'y' and 'PATH' in _env and _env.get('REPO_ROOT'),
    '10m: subprocess_env が秘密情報を落としていない／必要な変数を落としている')
for _k in ('ANTHROPIC_API_KEY', 'GEMINI_API_KEY', 'OPENAI_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'GITHUB_TOKEN'):
    chk(bool(_vend._SECRET_ENV.search(_k)), f'10m2: {_k} が秘密情報として弾かれない')
os.environ.pop('NEO_TEST_API_KEY', None); os.environ.pop('NEO_TEST_PLAIN', None)
# D3: 部品側の上限
_html = open(os.path.join(R, 'neo_skill', 'addata_bridge', 'index.html'), encoding='utf-8').read()
chk('f.size > MAX_FILE_BYTES' in _html and 'MAX_MESSAGE_BYTES' in _html and 'ALLOWED_EXT.test(name)' in _html, '10n: 部品が読む前にファイルの大きさ・種類で弾いていない')

# ── 11. 本番でモジュールが古いまま残らないこと（2026-09-15 read_estimate() got an unexpected keyword argument 'customer_hint'）──
_mods = ('neo_skill.vendor', 'neo_skill.prompts', 'neo_skill.llm', 'neo_skill.doc_hints', 'neo_skill._runner', 'neo_skill.reader', 'neo_skill.maker', 'neo_skill.bridge')
chk(all(m in app._APP_MODULES for m in _mods), f'11: neo_skill のモジュールが _APP_MODULES に無い: {[m for m in _mods if m not in app._APP_MODULES]}')
chk(all(app._APP_MODULES.index(m) < app._APP_MODULES.index('app') for m in _mods), '11b: neo_skill は app より先に読み直す')
chk(all(app._APP_MODULES.index(_mods[i]) < app._APP_MODULES.index(_mods[i + 1]) for i in range(len(_mods) - 1)), '11c: neo_skill の読み直しの順が依存の順ではない')
import neo_skill.reader as _nr
chk(len(app._nsk_code_stamp()) == 8 and app._nsk_code_stamp() == app._file_digest(_nr.__file__)[:8], '11d: 画面に出す neo_skill の印が reader.py の指紋と違う')

print('REG_INSURANCE:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL:
    print('  -', f)
sys.exit(1 if FAIL else 0)
