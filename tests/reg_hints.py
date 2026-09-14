# -*- coding: utf-8 -*-
"""reg_hints.py — 添付した車検証・事故/保険の書類の OCR 結果を、生成経路の hint / vehicle_info / サイドバーの欄に写す
neo_skill.doc_hints の単体試験。Streamlit・Gemini・ADDATA は要らない。値は架空。

    python tests/reg_hints.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from neo_skill import doc_hints as dh  # noqa: E402

FAILS = []


def chk(cond, msg):
    if not cond:
        FAILS.append(msg)


# 車検証 OCR（app.analyze_vehicle_registration）の形。全角数字・同上・排ガス記号付きの型式を混ぜる
SHAKEN = {
    'customer_name': '同上', 'owner_name': 'テスト自動車販売株式会社',
    'postal_no': '', 'prefecture': '福岡県', 'municipality': '北九州市小倉北区', 'address_other': '試験町1-2-3',
    'car_reg_department': '北九州', 'car_reg_division': '５００', 'car_reg_business': 'あ', 'car_reg_serial': '１２３４',
    'car_serial_no': 'KSP210-9999999', 'car_name': 'トヨタ', 'car_model': '５ＢＡ－ＫＳＰ２１０',
    'car_model_designation': '19548', 'car_category_number': '2', 'engine_model': '1KR-FE',
    'body_color': '', 'color_code': '', 'trim_code': '', 'car_weight': '1000', 'engine_displacement': '996',
    'kilometer': '15345', 'term_date': '20261129', 'car_reg_date': '20211100', 'confidence': '0.9',
}
DOC = {
    'company': 'テスト共済', 'branch': '福岡テストサービスセンター', 'staff': '担当 花子', 'accept_no': '2500000000-501',
    'report_date': '20260323', 'accident_date': '令和7年10月26日', 'accident_place': '福岡県北九州市', 'contractor': 'テスト 太郎',
    'counterpart': '相手 次郎', 'policy_no': '', 'coverage': '対物 無制限 免責0', 'market_value': '1146000',
    'reg_no': '北九州 500 あ 1234', 'car_name': 'ヤリス KSP210 G 1000', 'model': '5BA-KSP210', 'grade': 'G', 'first_reg': '令和3年11月',
    'serial_no': 'KSP210-9999999', 'reg_date': '20211130', 'engine_model': '1KR-FE型', 'desig': '19548', 'category': '0002',
    'term_date': '20261129', 'owner': 'テスト自動車販売株式会社', 'user': '同上', 'mileage': '15345', 'color_code': '3T3',
    'color_name': 'センシュアルレッドマイカ', 'equipment': 'AT,ナビ', 'confidence': '0.9',
}


def test_model_prefix():
    for src, want in (('5BA-KSP210', 'KSP210'), ('６ＡＡ－ＡＹＨ３０Ｗ', 'AYH30W'), ('DBA-ZRR80G', 'ZRR80G'), ('KSP210', 'KSP210'),
                      ('3BA-GR3P', 'GR3P'), ('', ''), ('CBA-DBA10', 'DBA10')):
        chk(dh.strip_emission_prefix(src) == want, f'排ガス記号: {src!r} → {dh.strip_emission_prefix(src)!r}（期待 {want!r}）')


def test_dates():
    for src, want in (('20251026', '20251026'), ('2025-10-26', '20251026'), ('2025/1/6', '20250106'), ('令和7年10月26日', '20251026'),
                      ('R7.10.26', '20251026'), ('令和3年11月', '20211100'), ('平成31年4月', '20190400'), ('', ''), ('不明', ''),
                      ('20251340', ''), ('令和7年2月31日', ''), ('2025/13/01', ''), ('20250229', ''), ('20240229', '20240229')):
        chk(dh.date8(src) == want, f'date8: {src!r} → {dh.date8(src)!r}（期待 {want!r}）')
    for src, want in (('20211100', 'R3.11'), ('20190400', 'H31.4'), ('20190500', 'R1.5'), ('2015-05', 'H27.5'), ('令和3年11月', 'R3.11'),
                      ('R4.3', 'R4.3'), ('19880700', 'S63.7'), ('', '')):
        chk(dh.reg_date_wareki(src) == want, f'初度登録: {src!r} → {dh.reg_date_wareki(src)!r}（期待 {want!r}）')


def test_reg_no():
    chk(dh.reg_no_text(SHAKEN) == '北九州 500 あ 1234', f'登録番号（車検証）: {dh.reg_no_text(SHAKEN)!r}')
    chk(dh.reg_no_text({}, DOC) == '北九州 500 あ 1234', f'登録番号（書類）: {dh.reg_no_text({}, DOC)!r}')
    chk(dh.parse_reg_no('北九州 500 あ 12-34') == ('北九州', '500', 'あ', '1234'), f'parse_reg_no: {dh.parse_reg_no("北九州 500 あ 12-34")!r}')
    # Codex 64: 一連番号の掃除はベタ打ち側（app._reg_no_part）と同じ規則。U+2011 のハイフン・空白・「・・12」でも生成器の形になる
    chk(dh.parse_reg_no('北九州 500 あ ・・12') == ('北九州', '500', 'あ', '12'), f'parse_reg_no ・・12: {dh.parse_reg_no("北九州 500 あ ・・12")!r}')
    chk(dh.parse_reg_no('北九州500あ１２‑３４') == ('北九州', '500', 'あ', '1234'), f'parse_reg_no U+2011: {dh.parse_reg_no("北九州500あ１２‑３４")!r}')
    chk(dh.parse_reg_no('北九州 500 あ') == ('', '', '', '') and dh.parse_reg_no('') == ('', '', '', ''), 'parse_reg_no 一連番号なし')
    chk(dh.parse_reg_no('北九州 500 あ 12345') == ('', '', '', ''), 'parse_reg_no 一連番号 5 桁は分けない')
    # Codex 65: 分類番号のアルファベット（希望番号）も分ける。全角で来ても半角大文字に
    chk(dh.parse_reg_no('北九州 30A あ ・・12') == ('北九州', '30A', 'あ', '12'), f'parse_reg_no 30A: {dh.parse_reg_no("北九州 30A あ ・・12")!r}')
    chk(dh.parse_reg_no('北九州 ３ＡＣ あ 1234') == ('北九州', '3AC', 'あ', '1234'), f'parse_reg_no 3AC 全角: {dh.parse_reg_no("北九州 ３ＡＣ あ 1234")!r}')
    chk(dh.reg_no_text({}, {'reg_no': '北九州 30A あ 12'}) == '北九州 30A あ 12', f'reg_no_text 30A: {dh.reg_no_text({}, {"reg_no": "北九州 30A あ 12"})!r}')
    v5 = dh.vehicle_info_for_legacy({}, dict(DOC, reg_no='北九州 30A あ ・・12'))
    chk(v5.get('car_reg_division') == '30A' and v5.get('car_reg_serial') == '12', f'書類だけ・30A: {v5.get("car_reg_division")!r} {v5.get("car_reg_serial")!r}')
    _r = dict(SHAKEN, car_reg_serial='１２‑３４')
    chk(dh.reg_no_text(_r) == '北九州 500 あ 1234', f'reg_no_text 分割入力の U+2011: {dh.reg_no_text(_r)!r}')
    chk(dh.reg_no_text({'registration_number': '北九州 500 あ ・・12'}) == '北九州 500 あ 12', f'reg_no_text 全体入力の ・・12: {dh.reg_no_text({"registration_number": "北九州 500 あ ・・12"})!r}')
    chk(dh.reg_no_text({}, {'reg_no': '北九州 500 あ 12 34'}) == '北九州 500 あ 1234', f'reg_no_text 書類の空白入り: {dh.reg_no_text({}, {"reg_no": "北九州 500 あ 12 34"})!r}')


def test_vehicle_hint():
    h = dh.vehicle_hint(SHAKEN, DOC)
    chk(h == {'model_code': 'KSP210', 'serial_no': 'KSP210-9999999', 'desig': '19548', 'category': '0002', 'reg_date': 'R3.11', 'color_code': '3T3'},
        f'vehicle_hint（車検証＋書類）: {h!r}')
    h2 = dh.vehicle_hint({}, DOC)
    chk(h2['model_code'] == 'KSP210' and h2['reg_date'] == 'R3.11' and h2['category'] == '0002', f'vehicle_hint（書類だけ）: {h2!r}')
    chk(dh.vehicle_hint({}, {}) == {} and dh.vehicle_hint(None, None) == {}, '空の hint が空でない')


def test_customer_hint():
    c = dh.customer_hint(SHAKEN, DOC)
    chk(c.get('name') == 'テスト自動車販売株式会社', f'使用者が同上なら所有者を顧客名に: {c.get("name")!r}')
    chk(c.get('owner') == 'テスト自動車販売株式会社', 'owner')
    chk(c.get('reg_no') == '北九州 500 あ 1234', f'reg_no: {c.get("reg_no")!r}')
    chk(c.get('address') == '福岡県北九州市小倉北区試験町1-2-3', f'address: {c.get("address")!r}')
    chk(c.get('term_date') == '20261129' and c.get('kilometer') == '15345', f'term/km: {c.get("term_date")!r} {c.get("kilometer")!r}')
    c2 = dh.customer_hint({'customer_name': '***', 'owner_name': '所有 一郎'}, None)
    chk(c2.get('name') == '所有 一郎', f'*** なら所有者: {c2!r}')
    chk('owner_name' not in c and 'user_name' not in c, f'使用者=所有者なら owner_name/user_name は渡さない: {c!r}')
    c3 = dh.customer_hint({'customer_name': '使用 花子', 'owner_name': 'リース株式会社'}, None)
    chk(c3.get('name') == '使用 花子' and c3.get('owner_name') == 'リース株式会社' and c3.get('user_name') == '使用 花子', f'使用者≠所有者: {c3!r}')


def test_insurance():
    i = dh.insurance_hint_from_doc(DOC)
    chk(i == {'company': 'テスト共済', 'agency': 'テスト共済', 'contractor': 'テスト 太郎', 'accident_date': '20251026',
              'accept_no': '2500000000-501', 'adjuster': '担当 花子', 'adjuster_post': '福岡テストサービスセンター'}, f'insurance_hint: {i!r}')
    s = dh.sidebar_insurance_from_doc(DOC)
    chk(s == {'accept_no': '2500000000-501', 'accident_date': '20251026', 'contractor_name': 'テスト 太郎', 'agency_name': 'テスト共済',
              'adjuster_name': '担当 花子', 'adjuster_post': '福岡テストサービスセンター'}, f'sidebar: {s!r}')
    chk(dh.sidebar_insurance_from_doc({}) == {} and dh.insurance_hint_from_doc(None) == {}, '空の書類')
    # 日が読めない事故日・有効期限は入れない（年月だけを 8 桁にして書かない）
    chk('accident_date' not in dh.insurance_hint_from_doc(dict(DOC, accident_date='令和7年10月')), '日の無い事故日を渡している')
    chk('term_date' not in dh.customer_hint({}, dict(DOC, term_date='2026-11')), '日の無い有効期限を渡している')
    chk(dh.date8_full('20251026') == '20251026' and dh.date8_full('20251000') == '' and dh.date8_full('') == '', 'date8_full')


def test_legacy_vehicle_info():
    v = dh.vehicle_info_for_legacy(SHAKEN, DOC)
    chk(v['customer_name'] == 'テスト自動車販売株式会社' and v['owner_name'] == 'テスト自動車販売株式会社', f'使用者が同上なら所有者を顧客名に（旧経路は customer_name をそのまま Name1 に書く）: {v.get("customer_name")!r}')
    chk(v['color_code'] == '3T3' and v['body_color'] == 'センシュアルレッドマイカ', f'書類から色を補う: {v.get("color_code")!r} {v.get("body_color")!r}')
    chk(v['car_model'] == '５ＢＡ－ＫＳＰ２１０', '車検証の型式は書き換えない')
    # 登録番号は実機 NEO と同じ半角数字・ハイフン無し（車検証 OCR は全角 '５００' '１２３４' で返す）
    chk(v['car_reg_division'] == '500' and v['car_reg_serial'] == '1234' and v['car_reg_business'] == 'あ' and v['car_reg_department'] == '北九州',
        f'登録番号を半角に: {[v.get(k) for k in ("car_reg_department", "car_reg_division", "car_reg_business", "car_reg_serial")]!r}')
    v4 = dh.vehicle_info_for_legacy(dict(SHAKEN, car_reg_serial='１２-３４', car_reg_division='３０Ａ'), None)
    chk(v4['car_reg_serial'] == '1234' and v4['car_reg_division'] == '30A', f'一連番号のハイフンを外し英字の分類番号は残す: {v4.get("car_reg_serial")!r} {v4.get("car_reg_division")!r}')
    chk(v['car_name'] == 'ヤリス KSP210 G 1000', f'車検証の車名がメーカー名だけなら書類の車名: {v.get("car_name")!r}')
    v3 = dh.vehicle_info_for_legacy(dict(SHAKEN, car_name='ヤリスクロス MXPJ10'), DOC)
    chk(v3['car_name'] == 'ヤリスクロス MXPJ10', '車検証に車名があればそのまま')
    v2 = dh.vehicle_info_for_legacy({}, DOC)
    chk(v2.get('customer_name') == 'テスト自動車販売株式会社' and v2.get('car_reg_serial') == '1234' and v2.get('car_model') == '5BA-KSP210'
        and v2.get('car_reg_date') == '20211100' and v2.get('kilometer') == 15345 and v2.get('car_category_number') == '0002',
        f'書類だけから組み立て: {v2!r}')
    chk(dh.vehicle_info_for_legacy({}, {}) == {} and dh.vehicle_info_for_legacy(None, None) == {}, '空')
    chk('_error' not in dh.vehicle_info_for_legacy({'_error': 'x', 'car_name': 'A'}, {}), '_ で始まるキーは落とす')


def test_hunt_2026_09_15():
    """バグハント（codex hunt B）の採用分"""
    # B4 走行距離
    chk(dh.parse_km('15,345km') == '15345' and dh.parse_km('1.5万km') == '15000' and dh.parse_km('1万5000km') == '15000'
        and dh.parse_km(15345) == '15345' and dh.parse_km('約 2.3 万 km') == '23000' and dh.parse_km('') == '' and dh.parse_km('不明') == '',
        f'parse_km: {[dh.parse_km(x) for x in ("15,345km", "1.5万km", "1万5000km", 15345, "約 2.3 万 km", "", "不明")]!r}')
    chk(dh.customer_hint({}, dict(DOC, mileage='1.5万km')).get('kilometer') == '15000', 'customer_hint の走行距離 1.5万km')
    chk(dh.vehicle_info_for_legacy({}, dict(DOC, mileage='1.5万km')).get('kilometer') == 15000, '旧経路の走行距離 1.5万km')
    # B3 2 桁年
    chk(dh.date8('25.10.26', allow_yy=True) == '20251026' and dh.date8('25/10/26', allow_yy=True) == '20251026'
        and dh.date8('7.10.26', allow_yy=True) == '' and dh.date8('25.13.01', allow_yy=True) == '',
        f'date8 2 桁年（保険書類）: {[dh.date8(x, allow_yy=True) for x in ("25.10.26", "25/10/26", "7.10.26", "25.13.01")]!r}')
    # Codex 74: 2 桁年は保険書類の事故日だけ。車検証の初度登録（元号抜けの平成 25 年かもしれない）では受けない
    chk(dh.date8('25.10.26') == '' and dh.reg_date_wareki('25.10.26') == '' and dh.date8_full('25.10.26') == '', '2 桁年を既定で受けている')
    chk(dh.insurance_hint_from_doc(dict(DOC, accident_date='25.10.26')).get('accident_date') == '20251026', '保険書類の事故日 25.10.26')
    chk(dh.vehicle_hint({}, dict(DOC, first_reg='25.10.26')).get('reg_date', '') == '', '書類の初度登録 25.10.26 は受けない')
    # B5 かなはひらがな
    chk(dh.parse_reg_no('北九州 580 ｱ 1234') == ('北九州', '580', 'あ', '1234') and dh.parse_reg_no('北九州 580 ア 1234')[2] == 'あ',
        f'かな→ひらがな: {dh.parse_reg_no("北九州 580 ｱ 1234")!r}')
    chk(dh.reg_no_text(dict(SHAKEN, car_reg_business='ｱ')) == '北九州 500 あ 1234', f'reg_no_text のかな: {dh.reg_no_text(dict(SHAKEN, car_reg_business="ｱ"))!r}')
    chk(dh.vehicle_info_for_legacy(dict(SHAKEN, car_reg_business='ア'), None)['car_reg_business'] == 'あ', '旧経路のかな')
    chk(dh.kana_hira('ｱ') == 'あ' and dh.kana_hira('あ') == 'あ' and dh.kana_hira('E') == 'E', 'kana_hira')
    # B2 住所分割（実機: 市区郡は '〜市' まで、政令市の区は以降側）
    chk(dh.split_address('福岡県', '北九州市小倉北区', '試験町1-2-3') == ('福岡県', '北九州市', '小倉北区試験町1-2-3'), f'split_address 政令市: {dh.split_address("福岡県", "北九州市小倉北区", "試験町1-2-3")!r}')
    chk(dh.split_address('', '', '東京都渋谷区神宮前1-2-3') == ('東京都', '渋谷区', '神宮前1-2-3'), f'split_address 東京: {dh.split_address("", "", "東京都渋谷区神宮前1-2-3")!r}')
    chk(dh.split_address('', '', '') == ('', '', ''), 'split_address 空')
    chk(dh.split_address('福岡県', '北九州市', '') == ('福岡県', '北九州市', ''), f'split_address 市だけ: {dh.split_address("福岡県", "北九州市", "")!r}')
    # Codex 75: 構造化された市区郡は名前の中の 市・郡 で切らない（四日市市・余市郡余市町・市川市）。政令市の区だけ以降側へ
    chk(dh.split_address('三重県', '四日市市', '諏訪町1-1') == ('三重県', '四日市市', '諏訪町1-1'), f'四日市市: {dh.split_address("三重県", "四日市市", "諏訪町1-1")!r}')
    chk(dh.split_address('北海道', '余市郡余市町', '黒川町1') == ('北海道', '余市郡余市町', '黒川町1'), f'余市郡: {dh.split_address("北海道", "余市郡余市町", "黒川町1")!r}')
    chk(dh.split_address('千葉県', '市川市', '八幡1-1') == ('千葉県', '市川市', '八幡1-1'), '市川市')
    chk(dh.split_address('福岡県', '北九州市小倉北区', '') == ('福岡県', '北九州市', '小倉北区'), f'政令市の区だけ: {dh.split_address("福岡県", "北九州市小倉北区", "")!r}')
    chk(dh.split_address('', '北九州市小倉北区', '試験町1-2-3') == ('', '北九州市', '小倉北区試験町1-2-3'), '都道府県なし')
    chk(dh.split_address('福岡県', '志免町', '') == ('福岡県', '志免町', '') and dh.split_address('', '', '福岡県遠賀郡岡垣町') == ('福岡県', '遠賀郡', '岡垣町'),
        f'split_address 町だけ／郡町: {dh.split_address("福岡県", "志免町", "")!r} {dh.split_address("", "", "福岡県遠賀郡岡垣町")!r}')
    chk(dh.split_address('', '', '福岡県試験町1-2-3') == ('福岡県', '試験町', '1-2-3'), f'split_address 町だけ（生成器と同じ規則）: {dh.split_address("", "", "福岡県試験町1-2-3")!r}')
    # Codex 69 [2]: 車検証に名前が無く書類に使用者≠所有者があれば、書類の使用者が user_name
    v6 = dh.vehicle_info_for_legacy({'car_serial_no': 'KSP210-9999999'}, dict(DOC, user='使用 花子', owner='所有 太郎'))
    chk(v6.get('user_name') == '使用 花子' and v6.get('customer_name') == '使用 花子' and v6.get('owner_name') == '所有 太郎' and '_raw_user' not in v6,
        f'書類の使用者≠所有者: {[v6.get(k) for k in ("user_name", "customer_name", "owner_name")]!r}')
    chk('_raw_user' not in dh.vehicle_info_for_legacy(SHAKEN, None) and '_raw_user' not in dh.vehicle_info_for_legacy(SHAKEN, DOC), '_raw_user が残っている')
    chk(dh._get({'a': 0}, 'a') == '0' and dh.parse_km(0) == '0', 'Codex 69 [4]: 数値 0 を落とさない')
    chk(dh.vehicle_info_for_legacy({}, dict(DOC, desig='1234')).get('car_model_designation') == '01234', '書類だけの型式指定も 5 桁')
    chk(dh.vehicle_hint({}, dict(DOC, desig='1234')).get('desig') == '01234' and dh.vehicle_hint(SHAKEN, DOC).get('desig') == '19548', 'スキル経路の型式指定も 5 桁（Codex 70）')
    v = dh.vehicle_info_for_legacy(SHAKEN, DOC)
    chk(v['prefecture'] == '福岡県' and v['municipality'] == '北九州市' and v['address_other'] == '小倉北区試験町1-2-3', f'旧経路の住所分割: {[v.get(k) for k in ("prefecture", "municipality", "address_other")]!r}')
    # B7 使用者欄
    chk(v['user_name'] == '同上', f'使用者が同上なら user_name は 同上: {v.get("user_name")!r}')
    v2 = dh.vehicle_info_for_legacy(dict(SHAKEN, customer_name='使用 花子'), None)
    chk(v2['user_name'] == '使用 花子' and v2['customer_name'] == '使用 花子', f'使用者≠所有者なら user_name は使用者名: {v2.get("user_name")!r}')
    v3 = dh.vehicle_info_for_legacy({}, DOC)
    chk(v3.get('user_name') == '同上', f'書類だけ（使用者=所有者）: {v3.get("user_name")!r}')
    # B8 類別 4 桁・型式指定 5 桁
    chk(v['car_category_number'] == '0002' and v['car_model_designation'] == '19548', f'類別/型式指定の桁: {v.get("car_category_number")!r} {v.get("car_model_designation")!r}')
    chk(dh.vehicle_info_for_legacy(dict(SHAKEN, car_model_designation='１２３４'), None)['car_model_designation'] == '01234', '型式指定の 0 埋め')
    # C1 値の掃除
    chk(dh._get({'a': 'x\ny\x00z'}, 'a') == 'x y z' and len(dh._get({'a': 'あ' * 500}, 'a')) == dh._MAX_VALUE_LEN
        and dh._get({'a': ['x']}, 'a') == '' and dh._get({'a': True}, 'a') == '', '_get の掃除（制御文字・長さ・型）')


def test_reader_merge():
    """スキル経路: reader._normalise_header は 見積書に印字が無い項目にだけ hint を補う（印字があればそちらが残る）"""
    from neo_skill import reader
    vh, ch, ih = dh.vehicle_hint(SHAKEN, DOC), dh.customer_hint(SHAKEN, DOC), dh.insurance_hint_from_doc(DOC)
    h = reader._normalise_header({'source': 'x', 'totals': {'total': 1}}, vh, ih, ch)
    chk(h.get('vehicle', {}).get('serial_no') == 'KSP210-9999999' and h.get('vehicle', {}).get('model_code') == 'KSP210', f'印字なし → 車検証で補う: {h.get("vehicle")!r}')
    chk(h.get('customer', {}).get('name') == 'テスト自動車販売株式会社' and h.get('customer', {}).get('reg_no') == '北九州 500 あ 1234', f'顧客: {h.get("customer")!r}')
    chk(h.get('insurance', {}).get('accept_no') == '2500000000-501' and h.get('insurance', {}).get('adjuster_post') == '福岡テストサービスセンター', f'保険: {h.get("insurance")!r}')
    h2 = reader._normalise_header({'source': 'x', 'totals': {'total': 1}, 'vehicle': {'serial_no': 'AAA-0000001'}, 'customer': {'name': '印字 太郎'}}, vh, ih, ch)
    chk(h2['vehicle']['serial_no'] == 'AAA-0000001' and h2['vehicle'].get('model_code') == 'KSP210', f'印字あり → 印字が残り、無い項目だけ補う: {h2["vehicle"]!r}')
    chk(h2['customer']['name'] == '印字 太郎' and h2['customer'].get('reg_no') == '北九州 500 あ 1234', f'顧客の印字優先: {h2["customer"]!r}')
    # B1: 印字の「同上」「***」は穴 → 車検証で埋める。使用者欄の '同上' は正しい値なので残す
    h3 = reader._normalise_header({'source': 'x', 'totals': {'total': 1}, 'customer': {'name': '同上', 'owner_name': '***', 'user_name': '同上'}}, vh, ih,
                                  {'name': 'テスト自動車販売株式会社', 'owner_name': '所有 太郎', 'user_name': '使用 花子'})
    chk(h3['customer']['name'] == 'テスト自動車販売株式会社' and h3['customer']['owner_name'] == '所有 太郎' and h3['customer']['user_name'] == '同上',
        f'同上/*** を hint で埋める: {h3["customer"]!r}')


def test_summary():
    s = dh.summary(SHAKEN, DOC)
    chk('車検証:' in s and '書類:' in s and 'テスト' not in s, f'summary に個人名が出ている／見出しが無い: {s!r}')
    chk(dh.summary({}, {}) == '', 'summary 空')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
    for f in FAILS:
        print('*** FAILED:', f)
    print('reg_hints:', 'all ok' if not FAILS else f'{len(FAILS)} 件が不合格')
    sys.exit(1 if FAILS else 0)
