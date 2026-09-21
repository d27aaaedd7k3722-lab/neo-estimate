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
import hashlib
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
    'factory_name': '写真鑑定',
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
    'Insurance.ConsultantFactory': '写真鑑定',   # 立会工場（画像鑑定は「写真鑑定」。2026-09-15）
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

# 証券番号の欄が空なら、事故番号・受付番号をそこにも入れる（2026-09-16 亮平さん指示。スキル経路は
# draft_estimate.Drafter._insurance が同じことをする）。受付番号の欄は消さない
_ins_acc = dict(INS); _ins_acc.pop('policy_no')
_v_acc = em_values(app.generate_neo_file(TPL, CUST, [dict(i) for i in ITEMS], 0, _ins_acc, {}, False, False, False)[0])
chk(_v_acc.get('Insurance.PolicyNo') == 'ZZ-2026-0001',
    f"1b: 証券番号が空のとき事故番号が証券番号に入らない: {_v_acc.get('Insurance.PolicyNo')!r}")
chk(_v_acc.get('FileInfo.AcceptNo') == 'ZZ-2026-0001',
    f"1b: 受付番号の欄が消えた: {_v_acc.get('FileInfo.AcceptNo')!r}")
_nb_pol = app.generate_neo_file(TPL, CUST, [dict(i) for i in ITEMS], 0, INS, {}, False, False, False)[0]
_v_pol = em_values(_nb_pol)
chk(_v_pol.get('Insurance.PolicyNo') == 'ZZPOLICY123',
    f"1b: 証券番号が読めているのに上書きした: {_v_pol.get('Insurance.PolicyNo')!r}")
# マージモード（カスタムのテンプレート NEO を使う経路）では、テンプレートに残っている本物の証券番号を
# 事故番号で塗り替えない（レビュー指摘 2026-09-16）。テンプレートが空なら今までどおり事故番号を入れる
_v_mg = em_values(app.generate_neo_file(_nb_pol, CUST, [dict(i) for i in ITEMS], 0, _ins_acc, {}, False, False, True)[0])
chk(_v_mg.get('Insurance.PolicyNo') == 'ZZPOLICY123',
    f"1c: マージモードでテンプレートの証券番号を事故番号で上書きした: {_v_mg.get('Insurance.PolicyNo')!r}")
chk(_v_mg.get('FileInfo.AcceptNo') == 'ZZ-2026-0001',
    f"1c: マージモードで受付番号が入っていない: {_v_mg.get('FileInfo.AcceptNo')!r}")

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
_vals_src = inspect.getsource(app._sidebar_insurance_values)
chk("'factory_name'" in _vals_src, '4i: ベタ打ち（旧経路）に渡す insurance_info に立会工場（factory_name）が無い')
_upd_src = inspect.getsource(app._update_em_db_impl) if hasattr(app, '_update_em_db_impl') else inspect.getsource(app.update_em_db)
chk('if factory_name:' in _upd_src and "('ConsultantFactory', factory_name)" not in _upd_src,
    '4j: 立会工場が空でも ConsultantFactory を書いてテンプレートの値を消している')
_hint_src = inspect.getsource(app._sidebar_insurance_hint)
for _k in ('accept_no', 'policy_no', 'contractor_name', 'agency_name', 'adjuster_name', 'factory_name',
           'garage_in_date', 'garage_out_date', 'repair_days', 'accident_date'):
    chk(_k in _hint_src, f'4g: 画面が渡す事故・保険情報に {_k} が含まれていない')
for _k in ('accept_no', 'agency', 'adjuster', 'factory', 'garage_in', 'garage_out', 'repair_days'):
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
for _k in ('policy_no', 'contractor_name', 'accept_no', 'adjuster_post', 'factory_name', 'exp_towing', '_doc_ocr_cache', '_insdoc_filled',
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
chk("_p2n_prev_key and _p2n_prev_key != _p2n_early_key" in _app_src and '_reset_case_inputs()' in _app_src.split('_p2n_prev_key and _p2n_prev_key != _p2n_early_key')[1][:1000],
    '9c: 見積書が別のファイルに変わったときに _reset_case_inputs を呼んでいない')
_st9 = {'docs_upload': None, 'contractor_name': 'A', 'repair_days': 0, 'pdf_tax_radio': '税抜き（外税）'}
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
chk(_sig1 != app._p2n_inputs_signature('f1', dict(_st9, docs_upload=[_FakeUp(b'x')])), '9h: 車検証の添付が違えば指紋が違う')
# Codex 72 [1]: 同じ添付でも OCR の控え（読めた中身）が変われば指紋が変わる（キーを直して読めるようになった等）
import hashlib as _hl
_ck = ('shaken', _hl.sha256(b'x').hexdigest(), 'm', _hl.sha256(b'k').hexdigest()[:12])
_s_no = dict(_st9, docs_upload=[_FakeUp(b'x')])
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
chk("not _docs_now_set" in inspect.getsource(app).split('_p2n_prev_key and _p2n_prev_key != _p2n_early_key')[1][:1800],
    '9y: 添付が無いときに keep_docs の枝に入る（Codex 73）')
chk('_slot_same' in inspect.getsource(app).split('_p2n_prev_key and _p2n_prev_key != _p2n_early_key')[1][:1800],
    '9y2: 前の案件の書類が 1 枚でも残っているときに keep_docs の枝に入る（Codex 76）')
# 9y3: 添付の入れ物は 1 つ。何の書類かは読み取りで見分ける（2026-09-21 亮平さん指示）
chk('accept_multiple_files=True, key=_docs_upload_key()' in _app_src and '_doc_upload_keys' not in _app_src,
    '9y3: 添付の入れ物が 1 つになっていない')
chk('_SHAKEN_NAME_RE' in _app_src and "for kind in order:" in _app_src,
    '9y4: 添付の種類を見分ける処理が無い（名前で当たりを付け、外れたらもう一方で読み直す）')
_att_src = inspect.getsource(app._attached_docs_ocr)
chk('st.expander' not in inspect.getsource(app).split('📎 添付（任意）')[0][-400:],
    '9y5: 添付の入れ物がたたみの中にある（常に開いておく）')
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
_n9k = _app_src.count('(_att_cap := _attached_docs_caption(api_key, selected_model))')
# 3 か所（ベタ打ちの単独 UI ／ Addata なしの横並び ／ Addata ありの横並び）。2026-09-21 にボタンを 2 つに分けて 1 か所増えた
chk(_n9k == 4 and _app_src.count('st.caption(_att_cap)') == 4, f'9k: 添付の案内が読めたかで出ていない（4 か所）: {_n9k}')
chk("return ''" in inspect.getsource(app._attached_docs_caption).split('if not _files:')[1][:160],
    '9k2: 何も添えていないときに添付の案内を出している（画面作り直し 2026-09-20）')
# 上の案内カードは 3 通り（要確認の NEO・合格・まだ）。要確認を「まだ生成していない」扱いにすると、帯（3 段目）と食い違う（Codex 第17周）
_9m_card = _app_src.split("_p2n_card_res = st.session_state.get('pdf2neo_result') or {}")[1][:1800]
chk("_p2n_card_res.get('unverified_neo')" in _9m_card and '_要確認' in _9m_card and "elif _p2n_give_now and _p2n_card_res.get('ok')" in _9m_card,
    '9m: 「_要確認」の NEO を渡すときの案内カードが無い（帯は「NEO を受け取る」なのに「入れて押すだけ」と出る）')
chk("out['total'] = cells[1] or cells[2]" in inspect.getsource(app._p2n_summary),
    '9m2: まとめの帯の合計に生成側の額を出している（印字＝見積書の額が正。Codex 第17周）')
_9m3 = app._p2n_summary('| 合計（税込） | 208,450 | 208,447 |')
chk(_9m3['total'] == '208,450' and _9m3['total_made'] == '208,447', f'9m3: 印字と生成が違うときの拾い方が違う: {_9m3}')
chk(app._p2n_summary('| 合計（税込） | 208,450 | 208,450 |')['total_made'] == '', '9m4: 印字と生成が同じなのに生成側も出している')
# 画面の CSS: 入れたファイルを外す ✕ は、ドロップ欄の小さな文字・span の一括非表示に巻き込まれないこと
# （2026-09-20 実画面のバグハントで、外せなくなっていた。Streamlit 1.63 では ✕ の中身は svg だが、版が上がって span になっても消えないように）
chk('[data-testid="stFileUploaderDropzone"] [data-testid="stFileChipDeleteBtn"] span' in _app_src
    and 'display:revert !important' in _app_src,
    '9r: ✕ の中身（span）を戻す指定が無い（一括非表示と同じ強さで書かないと勝てない。Codex 第19周）')
chk("_p2n_render_summary(_p2n_res.get('report_md'), stale=bool(_p2n_res.get('stale')))" in _app_src
    and 'total-strip-stale' in _app_src,
    '9s: 前の入力で作った結果の帯を、いまの見積書の数字と同じ見た目で出している（Codex 第19周）')
chk('small:not([data-testid="stFileChipDeleteBtn"]):not([data-testid="stFileUploaderDeleteBtn"])' in _app_src,
    '9r2: 小さな文字の一括非表示が ✕ を除いていない')
chk('[data-testid="stFileUploaderDropzone"] [data-testid="stBaseButton-borderlessIcon"] { display:none !important; }' in _app_src,
    '9r3: 見積書は 1 件だけなのに「＋（追加）」のボタンを出している（アイコンだけ消えて 4×4 の点になる）')
# たたみ（expander）の入れ子を作らない: 1.63 では動くが、中身が読みづらく、版によっては例外になる（Codex 第20/21周）
import ast as _ast  # noqa: E402


def _nested_expanders(src):
    def _is_exp(n):
        return isinstance(n, _ast.With) and any(
            isinstance(i.context_expr, _ast.Call) and getattr(i.context_expr.func, 'attr', '') == 'expander' for i in n.items)
    found = []

    def _walk(node, stack):
        for ch in _ast.iter_child_nodes(node):
            if _is_exp(ch):
                if stack:
                    found.append((ch.lineno, stack[-1]))
                _walk(ch, stack + [ch.lineno])
            else:
                _walk(ch, stack)
    _walk(_ast.parse(src), [])
    return found


# 生成のあとに「何を確かめればよいか」を出す（添付から読み取った内容・確認箇所シートの件数。2026-09-20 の流れ）
chk('_attached_docs_result_line(api_key, selected_model)' in _app_src and '_p2n_check_count(' in _app_src,
    '9u: 生成後の画面に「添付から読み取り」「要確認の件数」を出していない')
_u2a = '## 要確認（inspect_estimate）' + chr(10) + '- a' + chr(10) + '- b' + chr(10) + chr(10) + '## 次' + chr(10) + '- c'
_u2b = '## 要確認（inspect_estimate）' + chr(10) + '- なし' + chr(10)
chk(app._p2n_check_count(_u2a) == 2 and app._p2n_check_count(_u2b) == 0 and app._p2n_check_count('') == 0,
    '9u2: 要確認の件数の数え方が違う')
# 添付の読み取りは画面を描くときには走らせない（添付しただけで待たされない。2026-09-20 亮平さん指示）
chk('cached_only=True' in _app_src and _app_src.count('cached_only=True') >= 4,
    '9v2: 画面を描くときの添付の読み取りが API を呼ぶままになっている')
chk("_doc_fill_to_sidebar(_p2n_doc)" in _app_src and inspect.getsource(app).index("_p2n_ihint = _insurance_hint_now(_p2n_doc)") < inspect.getsource(app).index("_doc_fill_to_sidebar(_p2n_doc)"),
    '9v3: 生成時の保険 hint を作る前にサイドバーへ入れている（書類の値が hint から落ちる）')

# アプリ側が写しに書いた指定（塗装の実額・M を外した 等）は、画面だけでなく**納品する報告文**にも残す（2026-09-20 本番のバグハント）
chk(_app_src.count('_with_app_notes(') >= 4 and "out['app_notes']" in _app_src,
    '9w2: アプリ側の判断を報告文に残していない')
_w2 = app._with_app_notes('# 報告' + chr(10) + '本文', ['塗装は実額にする'])
chk('## アプリ側で判断した点' in _w2 and '- 塗装は実額にする' in _w2 and app._with_app_notes('x', []) == 'x',
    '9w3: 報告文への足し方が違う')
# 報告文が無いときは注記だけの report.md を作らない／同じ報告文に二度足さない（Codex 第24周）
chk(app._with_app_notes('', ['x']) == '' and app._with_app_notes(None, ['x']) is None and app._with_app_notes('   ', ['x']) == '   ',
    '9w4: 報告文が空なのにアプリ側の判断だけの報告文を作っている')
chk(app._with_app_notes(_w2, ['塗装は実額にする']).count('## アプリ側で判断した点') == 1,
    '9w5: 同じ報告文に二度足している')

# 9y: 添えた書類を読めなかったときは、**結果のところで**知らせる（読み取りは「生成」を押した後に走るので、
#     上の添付欄の知らせはこの run ではもう描き終わっていて出ない。2026-09-21 バグハント）
chk("st.session_state.pop('_doc_ocr_error', '')" in _app_src and '添えた書類を読み取れませんでした' in _app_src,
    '9y: 添付の読み取り失敗を結果のところで知らせていない')
# 知らせは共通の関数（_show_doc_ocr_error）に移した。結果を描き始める前に 1 回だけ呼ぶ（9zc で位置を固定）
chk('_show_doc_ocr_error()' in _app_src, '9y2: 読み取り失敗の知らせを共通の関数から出していない')

# 9x: 塗装の実額の注記は、**NEO に実際に入る塗装計**を添える（印字の塗装費用＋材料代と食い違う案件がある。
#     2026-09-20 本番のバグハント: 印字 76,000＋21,280 = 97,280 に対し NEO の塗装計は 104,660 だった）
_tbl = (chr(10).join(['# 報告', '', '| 項目 | 見積書 | 生成 |', '|---|---|---|',
                      '| 部品計 | 183,830 | 182,830 |', '| 塗装計（材料込） |  | 104,660 |',
                      '| 合計（税込） | 400,136 | 400,136 |']))
_pn = '塗装はコグニの入力方式を**実額**にする（塗装費用 76,000 円 ＋ 材料代 21,280 円）'
_w6 = app._with_app_notes(_tbl, [_pn])
chk('104,660 円' in _w6 and '7,380 円ちがいます' in _w6,
    f'9x: 実額の注記に NEO の塗装計を添えていない: {_w6[-220:]}')
# 印字の塗装費用＋材料代 と 塗装計 が同じなら何も足さない（ふつうの案件でうるさくしない）
_tbl2 = _tbl.replace('104,660', '97,280')
chk('ちがいます' not in app._with_app_notes(_tbl2, [_pn]),
    '9x2: 額が合っているのに差の注記を足している')
# 表が無い・数字でない・ほかの注記は素通り（落ちない）
for _b, _n in ((( '# 報告' + chr(10) + '本文'), _pn), (_tbl, 'M を外した'),
               (_tbl.replace('104,660', '—'), _pn)):
    _g = app._with_app_notes(_b, [_n])
    chk('ちがいます' not in _g and _n.split('（')[0] in _g, f'9x3: 素通りしていない: {_g[-160:]}')
# 画面の「読み取りの注意」にも同じ注記を通している（報告文だけ直っても画面が古いままになる）
chk('_paint_note_with_real_total(_w, _p2n_res.get(' in _app_src,
    '9x4: 画面の読み取りの注意に NEO の塗装計を添えていない')
# 9x5: 表がいくつもある報告文では**最後の検算表**を採る（Codex 第30周 P1）
_tbl3 = _tbl.replace('104,660', '97,280') + chr(10) * 2 + _tbl.split('# 報告' + chr(10))[1]
chk(app._paint_made_total(_tbl3) == 104660, f'9x5: 最後の検算表を見ていない: {app._paint_made_total(_tbl3)}')
# 表の外に同じ名前の行があっても拾わない
chk(app._paint_made_total('塗装計（材料込）は 999,999 円です' + chr(10) + _tbl) == 104660,
    '9x6: 検算表の外の行を拾っている')
# 9x7: 全角の数字・カンマ・＋ でも読める（Codex 第30周 P2）
_z = str.maketrans('0123456789,+', '０１２３４５６７８９，＋')
chk('104,660 円' in app._with_app_notes(_tbl.replace('104,660', '104,660'.translate(_z)),
                                        [_pn.translate(_z)]),
    '9x7: 全角の金額で注記が出ない')
chk(app._yen('１０４，６６０ 円') == 104660 and app._yen('') is None and app._yen('—') is None,
    '9x8: 金額の読み取りが違う')
# 9z3〜9z7: 画面の作り直しで Codex が挙げた 5 件（2026-09-21 第40周）
# (1) CSV だけの経路でも税区分を変えられる（描いていない run にだけ出す。キーは重複させない）
# 定義 + vendor/キーが無い枝 + Addata なしの横並び + Addata ありの横並び + CSV だけの経路（2026-09-21: 行き止まりの枝にも足した）
chk("if not st.session_state.get('_pdf_tax_row_shown'):" in _app_src and _app_src.count('_p2n_tax_row(') == 6,
    f"9z3: CSV だけの経路に税区分の切り替えが無い／二重に描いている: {_app_src.count('_p2n_tax_row(')}")
chk("st.session_state.pop('_pdf_tax_row_shown', None)" in _app_src.split('def _main_and_reset')[1][:400],
    '9z4: 税区分を描いた印を run の終わりで落としていない（次の run で描けなくなる）')
# (2) 添付を読む順番は内容のハッシュ順（入れた順番で採用される書類が変わらない＝指紋と食い違わない）
_ocr_src = inspect.getsource(app._attached_docs_ocr)
chk('_fh.sort(key=lambda x: x[0])' in _ocr_src, '9z5: 添付を読む順番を内容のハッシュ順に固定していない')
# (3) 読み取り失敗の文・進み具合の文にファイル名を出さない（顧客情報が入りやすい）
chk('f.name' not in _ocr_src.split('_doc_ocr_error')[1][:200] and '添付 {_idx} 件目' in _ocr_src,
    '9z6: 読み取り失敗の文にファイル名を出している')
chk('件目を読んでいます' in _ocr_src and 'f.name}）' not in _ocr_src,
    '9z7: 進み具合の文にファイル名を出している')
# (4) 生成後の run では、結果より先に読み取り失敗の知らせを消さない
chk("if st.session_state.get('_doc_ocr_error') and not st.session_state.get('pdf2neo_result'):" in _app_src,
    '9z8: 添付欄が結果より先に読み取り失敗の知らせを消している')
# 9zc: 読み取り失敗の知らせは**どの結果でも**出す（合格・ベタ打ち・要確認）。共通の関数 1 か所にまとめる
chk('def _show_doc_ocr_error' in _app_src and _app_src.count('            _show_doc_ocr_error()') == 1
    and _app_src.index('            _show_doc_ocr_error()') < _app_src.index("if _p2n_res and _p2n_res.get('legacy_beta'):"),
    '9zc: 読み取り失敗の知らせが合格の枝にしか無い（ベタ打ち・要確認で黙る）')
chk("err = st.session_state.pop('_doc_ocr_error', '')" in inspect.getsource(app._show_doc_ocr_error),
    '9zc2: 知らせを出したあとに消していない（次の案件まで残る）')

# 9z9 / 9za: Codex 第41周
# (1) 同じ中身でも**名前で読み方が変わる**ので、名前も指紋に入れる（名前を変えたのに古い結果を落とせない道を塞ぐ）
class _NUp:
    def __init__(self, b, name):
        self._b, self.name = b, name

    def getvalue(self):
        return self._b


_sd = {'docs_upload': None, 'contractor_name': 'A', 'repair_days': 0, 'pdf_tax_radio': '税抜き（外税）'}
chk(app._p2n_inputs_signature('f1', dict(_sd, docs_upload=[_NUp(b'x', 'report.pdf')]))
    != app._p2n_inputs_signature('f1', dict(_sd, docs_upload=[_NUp(b'x', '車検証_report.pdf')])),
    '9z9: 同じ中身でも名前で読み方が変わるのに指紋が同じ（古い結果を落とせる）')
chk(app._p2n_inputs_signature('f1', dict(_sd, docs_upload=[_NUp(b'x', 'a.pdf')]))
    == app._p2n_inputs_signature('f1', dict(_sd, docs_upload=[_NUp(b'x', 'b.pdf')])),
    '9z9b: どちらも車検証らしくない名前なのに指紋が違う（毎回作り直しになる）')
# (2) 事故・保険の書類を「車検証だけで作る」経路に渡さない（読み取りで車検証と分かったものか、名前が車検証らしいものだけ）
chk('vehicle_file = None' in _app_src and "_hit = _cache.get(('shaken'" in _app_src,
    '9za: 車検証と分からない添付を「車検証だけで作る」経路に渡している')
# 9zb: 添付の同一性は 1 か所（_docs_sig）で決める。保険欄の入れ直し・持ち越し判定・指紋が同じ見方をする
chk('_docs_sig(state)' in _app_src and _app_src.count("'#' + ('S' if _SHAKEN_NAME_RE.search") == 1,
    '9zb: 添付の同一性の決め方が 2 か所にある（片方だけ直すと食い違う）')
chk(app._docs_sig({'docs_upload': [_NUp(b'x', 'a.pdf')]}) != app._docs_sig({'docs_upload': [_NUp(b'x', '車検証.pdf')]}),
    '9zb2: 同じ中身でも役割（名前）が変われば別の添付として扱えていない（前の保険欄が残る）')
chk(app._docs_sig({'docs_upload': None}) == '', '9zb3: 添付が無いときの同一性が空でない')

# 9xa: 検算表の見出しは「3 列目が 生成」の表だけ。ほかの 3 列の表を取り違えない（Codex 第31周 P1）
_other = _tbl + chr(10) * 2 + chr(10).join(['| 項目 | 説明 | 金額 |', '|---|---|---|',
                                            '| 塗装計（材料込） | メモ | 97,280 |'])
chk(app._paint_made_total(_other) == 104660, f'9xa: 検算表でない 3 列の表を読んでいる: {app._paint_made_total(_other)}')
# 9xb: 材料代の印字が無い見積では注記も「塗装費用 X 円」だけになる。そのときも差を添える（Codex 第31周 P2）
_pn2 = '塗装はコグニの入力方式を**実額**にする（塗装費用 99,080 円）。塗装費用と材料代をまとめて総額 1 つで入れる'
_g2 = app._with_app_notes(_tbl.replace('104,660', '106,460'), [_pn2])
chk('106,460 円' in _g2 and '7,380 円ちがいます' in _g2 and '＋材料代' not in _g2.split('印字の塗装費用')[-1][:12],
    f'9xb: 材料代の無い注記で差が出ない／文面が変: {_g2[-200:]}')
chk('ちがいます' not in app._with_app_notes(_tbl.replace('104,660', '99,080'), [_pn2]),
    '9xb2: 材料代の無い注記で、合っているのに差を足している')
# 9xc: 「＋材料代」と書いてあるのに数字が読めない注記は触らない（塗装費用だけ拾って 0 円扱いにしない。Codex 第32周）
_pn3 = '塗装はコグニの入力方式を**実額**にする（塗装費用 76,000 円 ＋ 材料代 — 円）'
chk('ちがいます' not in app._with_app_notes(_tbl, [_pn3]),
    f'9xc: 材料代が読めない注記を 0 円として扱っている: {app._with_app_notes(_tbl, [_pn3])[-160:]}')
# 9xd: 生成が 0 円は「読めない」ではない。注記の額と食い違うなら出す（Codex 第32周）
chk('0 円' in app._with_app_notes(_tbl.replace('| 塗装計（材料込） |  | 104,660 |',
                                               '| 塗装計（材料込） | 97,280 | 0 |'), [_pn]),
    '9xd: 生成 0 円の食い違いを黙っている')
# 9x9: 塗装を実額に寄せるのは**見積書 PDF を読む経路だけ**。ベタ打ち（Addata なし）・CSV 取り込みは
#      印字どおりに打つ経路なので触らない（2026-09-20 亮平さん指示の後半）。app.py から呼んでいないことで担保する
chk('_paint_actual_guard' not in _app_src,
    '9x9: ベタ打ち側（app.py）から塗装の実額の上書きを呼んでいる')

_nest = _nested_expanders(_app_src)
chk(not _nest, f'9t: たたみの入れ子がある（中の行 → 外の行）: {_nest[:3]}')

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
chk('_p2n_beta_ui_shown = False' in _app_src and 'not _p2n_beta_ui_shown' in _app_src and "locals().get('_p2n_beta_ui_shown')" not in _app_src, '10e2: 同じ run で 2 回ベタ打ち UI を描く（Addata が外れた後）。印は locals() でなく変数で見る（バグハント H7）')
chk('blank_wage_retry' in _app_src.split('def _render_beta_result')[0] and "_p2n_res.get('blank_wage_retry')" in _app_src, '10d3: 再試行の失敗を画面に出していない')
# レビュー 2 周目: 「ベタ打ちで作る」は if/elif の連鎖（最後の else = 変換できませんでした）の後ろに置く（中に挟むと合格結果に誤エラー）
# 2026-09-15 午後: 逃げ道のベタ打ちで作った結果の下にも作り直しの入口を置いた（_render_beta_result の直後）。連鎖の後ろのものは最後の出現で見る
chk(_app_src.rfind('if _p2n_offer_beta:') > _app_src.find("st.error(f\"❌ {_p2n_res.get('error') or '変換できませんでした'}\")")
    and _app_src.rfind('if _p2n_offer_beta:') - _app_src.find("st.error(f\"❌ {_p2n_res.get('error') or '変換できませんでした'}\")") < 200,
    '10e3: ベタ打ちの逃げ道が結果の if/elif 連鎖の外（最後の else の直後）に無い')
_i_rb = _app_src.rfind('_render_beta_result(_p2n_res, selected_model)')   # 定義ではなく呼び出し（最後の出現）
chk(_i_rb > 0 and 0 < _app_src.find('if _p2n_offer_beta:', _i_rb) - _i_rb < 120, '10e5: 逃げ道のベタ打ちの結果の下に作り直しの入口が無い（H2/K4）')
chk('_p2n_beta_ui_shown = False' in _app_src, '10e4: _p2n_beta_ui_shown を初期化していない')
_src_mk = inspect.getsource(app.p2n_make)
chk('_blank_wage_rows(reading)' in _src_mk and 'force_draft=True' in _src_mk and "write_reading(case_dir, reading)" in _src_mk,
    '10d: p2n_make が工賃欄空欄の行を 0 円にして force_draft で作り直していない／失敗時に元へ戻していない')
# R2: ベタ打ちの逃げ道
# 定義 + Addata なしの横並び + Addata ありの横並び + 不合格の下 + 逃げ道の結果の下（2026-09-21 にボタンを 2 つに分けた）
chk(_app_src.count('_beta_generate_ui(') == 6 and 'fallback=True' in _app_src and '_p2n_offer_beta' in _app_src,
    f"10e: ベタ打ちの UI が共通化されて 5 か所から呼ばれていない: {_app_src.count('_beta_generate_ui(')}")
# 10e3: 2 つのモードを横並びで出す。Addata が無いときは部品コードつきが押せない（押せる／押せないで分かる）
chk("_p2n_c1, _p2n_c2 = st.columns(2)" in _app_src and "inline=True" in _app_src,
    '10e3: 生成ボタンを横並びにしていない')
chk("key='pdf2neo_run_disabled'" in _app_src and 'disabled=True' in _app_src,
    '10e4: Addata が無いときに「部品コードつき」を押せない形で見せていない')
# 同じ run でベタ打ちのボタンを 2 回描くとキーが重複して落ちるので、横並びを出したら印を立てる
chk(_app_src.count('_p2n_beta_ui_shown = True') == 3, '10e5: 横並びを描いたのに「描いた印」を立てていない')
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
_nd = os.path.dirname(_nr.__file__)
_nh = hashlib.sha256()
for _nn in sorted(x for x in os.listdir(_nd) if x.endswith('.py')):
    _nh.update(_nn.encode('utf-8') + b'\0' + app._file_digest(os.path.join(_nd, _nn)).encode('ascii'))
chk(len(app._nsk_code_stamp()) == 8 and app._nsk_code_stamp() == _nh.hexdigest()[:8], '11d: 画面に出す neo_skill の印が neo_skill 全モジュールの指紋と違う（2026-09-15: reader.py だけ → 全モジュール）')

# ── 12a〜12e: 2026-09-21 の並行バグハント（Codex ×2・Claude ×2）で出た穴をここで留める
# 12a: 未定義の名前を残さない。費用のチェックを関数に切り出したとき `_p2n_beta_exp` の参照だけが残り、
#      チェックを入れてベタ打ちを押すと必ず NameError で落ちていた（条件式の中なので既存のテストでは踏めない）
import glob as _glob            # noqa: E402
import subprocess as _sp        # noqa: E402
_pf = _sp.run([sys.executable, '-m', 'pyflakes', 'app.py'] + sorted(_glob.glob('neo_skill/*.py')),
              cwd=R, capture_output=True, text=True, encoding='utf-8', errors='replace')
_undef = [ln for ln in ((_pf.stdout or '') + (_pf.stderr or '')).splitlines() if 'undefined name' in ln]
chk(not _undef, '12a: 未定義の名前が残っている（実行時に NameError で落ちる）: ' + ' / '.join(_undef[:5]))
chk('_beta_exp_values()' in inspect.getsource(app._beta_generate_ui)
    and '_beta_exp_values()' in inspect.getsource(app._beta_expense_gate),
    '12a2: チェックを描く側と生成する側が同じ費用の値を見ていない')

# 12b: vendor が壊れている・APIキーが無いときも行き止まりにしない（ベタ打ちは vendor を使わない）
chk('if not _nsk_ready or not (claude_api_key or api_key):' in _app_src,
    '12b: vendor 不調とキー無しの枝でボタンも金額表記も出ず行き止まりになる')

# 12c: 添付は一度読めた役割を覚える（失敗の控えの 120 秒が切れた 2 回目で役割が入れ替わらない＝同じ見積書なら同じ NEO）
chk('_ok_kind' in _ocr_src and 'first = _ok_kind or (' in _ocr_src,
    '12c: 同じ添付の役割が run ごとに入れ替わりうる')

# 12d: テンプレート NEO を使っているときは、ベタ打ちの側にも「空欄は前の値が残る」注意を出す
#      （前は CSV プレビューの側にしか無く、立会工場が前の案件のまま黙って入った）
chk('テンプレートNEO を使用中です' in inspect.getsource(app._p2n_tax_row),
    '12d: ベタ打ちの側にテンプレート NEO の注意が無い')

# 12e: 入れたファイルは getvalue() で読む（read() は同じ run をまたいだ 2 回目で b'' になる）
chk('_csv_file.getvalue()' in _app_src and 'vehicle_file.getvalue()' in _app_src
    and '_p2n_file.getvalue()' in _app_src and 'custom_neo_file.getvalue()' in _app_src,
    '12e: 入れたファイルを read() で読んでいる（2 回目で空になる）')

# 12f: 画面に出る例外の文に、よそから来た本文（＝読んだ見積書・車検証の文字）を入れない
_llm_src = inspect.getsource(_llm_mod) if (_llm_mod := sys.modules.get('neo_skill.llm')) else ''
chk(_llm_src and 'msg[:200]' not in _llm_src and "str(getattr(e, 'message', e))" not in _llm_src,
    '12f: API の返事の本文が例外の文に入っている')

# 12g: 0 バイト・読み出せない添付を黙って捨てない（書類の情報が入っていない NEO が警告なしで出ていた。Codex hunt X）
chk('_skipped' in _ocr_src and "st.session_state['_doc_ocr_error'] = (f'添付 {_skipped} 件" in _ocr_src,
    '12g: 読めない添付が黙って無視される')

# 12h: 画面に出る文に、顧客名の入ったファイル名・見積書の文字が例外の本文として混ざらない
#      （自分で書いている例外＝LLMError/RunnerError/PageShapeError だけ本文を残す）
chk(_app_src.count("out['error'] = _safe_pipeline_err(e)") == 2
    and "f'{type(e).__name__}: {e}'" not in _app_src,
    '12h: パイプラインの例外の本文がそのまま画面に出る')

# 12i: 見積書を入れる前も、入れた後と同じ形にする（ボタン 2 つ横並び ＋ 金額表記はそのすぐ上）。
#      以前はここだけ無効ボタン 1 つで、金額表記は遥か下の「うまくいかないとき」の中にしか出なかった
#      （2026-09-21 亮平さん指摘「税込・税抜のボタンがありません」「NEO生成と、ベタ打ちNEO生成のボタンもできていません」）
_seg_wait = _app_src.split('if _p2n_file is None:')[1].split('_p2n_beta_ui_shown = False')[0]
chk('_p2n_tax_row(_saved_pdf_tax)' in _seg_wait and 'st.columns(2)' in _seg_wait
    and "key='pdf2neo_run_disabled'" in _seg_wait and "key='pdf2neo_run_beta_wait'" in _seg_wait,
    '12i: 見積書が無いときにボタン 2 つ・金額表記が出ていない')
# 待ち用のベタ打ちボタンは押せる方とキーが別（同じだと Addata 無しの枝と重複して落ちる）
chk("'pdf2neo_run_beta_wait'" in _app_src and _app_src.count("key='pdf2neo_run_beta'") == 1,
    '12i2: 待ち用と本物のベタ打ちボタンのキーが同じ')

print('REG_INSURANCE:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL:
    print('  -', f)
sys.exit(1 if FAIL else 0)
