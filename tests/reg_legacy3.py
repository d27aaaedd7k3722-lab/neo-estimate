# -*- coding: utf-8 -*-
"""reg_legacy3.py — 旧経路（ベタ打ち・CSV → ステップ④）の NEO 書き込み（2026-09-15 バグハント 3 回目 L/O）。
前の案件の値を入れたテンプレートから作り、前の案件の値が残らないこと・同じ .neo の中で値が食い違わないことを見る。
データはすべて架空。

    python tests/reg_legacy3.py
終了コード: 0 全部 OK / 1 失敗
"""
from __future__ import annotations

import datetime
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import logging  # noqa: E402

logging.getLogger('streamlit').setLevel(logging.CRITICAL)
import app  # noqa: E402

TPL = open(os.path.join(ROOT, 'template_toyota.neo'), 'rb').read()
FAILS: list = []


def chk(cond, msg):
    if not cond:
        FAILS.append(msg)


def unpack(neo):
    ck = app.find_real_cks(neo)
    raw = app.decompress_neo(neo, ck)
    mgmt, ent = app.parse_entries(neo, ck[0])
    return app.extract_files(raw, ent), mgmt, ent


def mem(blob):
    """一時ファイルを残さずに SQLite を開く"""
    c = sqlite3.connect(':memory:')
    c.deserialize(blob)
    return c


def dump(conn):
    return conn.serialize()


def one(blob, sql):
    c = mem(blob)
    try:
        return c.execute(sql).fetchone()
    finally:
        c.close()


def dirty_template(search_method=3):
    """前の案件の値を入れたテンプレート（架空の値）"""
    files, mgmt, ent = unpack(TPL)
    c = mem(files['AnSvEm0001Ex.db'])
    c.execute("UPDATE Customer SET Name1='前案件 太郎', Name2='前案件 二', Name3='前案件 三', Phone='099-000-0000', Fax='099-000-0001', "
              "AddressOther2='前案件ﾋﾞﾙ', AddressCode='401010001', CarSerialNo='ZZZ99-9999999', Kilometer=12345")
    c.execute("UPDATE Insurance SET PolicyNo='OLD-POLICY', ContractorName='前案件 契約者', RepairDays=12, TimelyPriceOutTax=800000, "
              "TimelyPriceInTax=880000, TimelyPriceTax=80000, ConsultantFactory='前案件工場', AdjusterPost='前の支店'")
    c.execute("UPDATE FileInfo SET AcceptNo='OLD-ACCEPT', Note2='前の備考2', Note3='前の備考3', GroupKey='G-OLD', "
              "GarageInDate='20250101', GarageInEraYear='0007', GarageOutDate='20250110', GarageOutEraYear='0007'")
    c.execute("UPDATE CarSearch SET SearchMethod=?, ps_CarSerialNo='ZZZ99-9999999', ps_CarSerialNoHead='ZZZ99', ps_CarSerialNoTail='9999999', "
              "ps_CarMouldNo='99999', ps_CarKindNo='9999', ps_CarRegDate='20200100', ev_CarName='前の車', nm_CarNameByUser='前の車', "
              "ms_CarSerialNoHead='ZZZ', ms_CarSerialNoTail='999'", (search_method,))
    c.execute("UPDATE Car SET ColorCode='OLD', ColorCodeFlag=1, CarName='前の車', CarNameByUser='前の車'")
    c.execute("UPDATE Statistics SET ProjectCompletedFlag='1', DisasterFlag='1'")
    c.execute('UPDATE Setting SET TaxRate=8, tx_ArrangeFlag=2')
    c.commit()
    files['AnSvEm0001Ex.db'] = dump(c)
    c.close()
    # 画像 DB に 1 枚
    c = mem(files['AnSvIf0001.sld'])
    cols = [r[1] for r in c.execute('PRAGMA table_info(Image)')]
    c.execute(f"INSERT INTO Image ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
              [b'OLDPHOTO' * 64 if 'Image' in col or 'Data' in col else (1 if i == 0 else '') for i, col in enumerate(cols)])
    c.commit()
    files['AnSvIf0001.sld'] = dump(c)
    c.close()
    files['AnSvIg0001.sld'] = files['AnSvIg0001.sld'].replace(b'[ImageSections]\r\n', b'[ImageSections]\r\nSection1=Image1\r\n') \
        .replace(b'[Image1]\r\n', b'[Image1]\r\nFile=old.jpg\r\n')
    files['AnFlInfo'] = files['AnFlInfo'].replace(b'[Reserve]\r\nFlag=0', b'[Reserve]\r\nFlag=1').replace(b'[Comment]\r\nFlag=0', b'[Comment]\r\nFlag=1')
    files['AnSvEm0001.sld'] = files['AnSvEm0001.sld'].replace(b'Idx1.ItemName=``', b'Idx1.ItemName=`OLD ADAS`')
    ini = files['AnSvImge.ini'].decode('cp932')
    ini = re.sub(r'TicketNo=[^\r\n]*', 'TicketNo=OLD-POLICY', ini)
    ini = re.sub(r'AgreedName=[^\r\n]*', 'AgreedName=' + '前案件工場', ini)
    files['AnSvImge.ini'] = ini.encode('cp932')
    return app.repack_neo(TPL, files, mgmt, ent)


CUST = {'customer_name': '新案件 花子', 'car_name': 'テストカー', 'car_serial_no': 'AB12-3456789', 'car_model_designation': '12345',
        'car_category_number': '0001', 'car_reg_date': '20230400', 'color_code': 'W99', 'car_reg_department': '品川',
        'car_reg_division': '300', 'car_reg_business': 'あ', 'car_reg_serial': '1234'}
INS = {'policy_no': 'NEW-POLICY', 'contractor_name': '新 契約者', 'factory_name': '新工場', 'accident_date': '20260901',
       'accept_no': 'NEW-ACCEPT'}
ITEMS = [{'name': 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ', 'method': '取替', 'quantity': 1, 'parts_amount': 45000, 'wage': 8000}]


def test_nonmerge_resets_previous_case():
    neo, *_ = app.generate_neo_file(dirty_template(3), CUST, ITEMS, 0, INS, merge_mode=False)
    f, _m, _e = unpack(neo)
    db = f['AnSvEm0001Ex.db']
    cu = one(db, "SELECT Name2, Name3, Phone, Fax, AddressOther2, AddressCode FROM Customer")
    chk(cu == ('', '', '', '', '', ''), f'L1: 顧客の入力欄の無い欄が前の案件のまま: {cu}')
    ins = one(db, 'SELECT RepairDays, TimelyPriceOutTax, TimelyPriceInTax, TimelyPriceTax, AdjusterPost FROM Insurance')
    chk(ins[:4] == (-1, -1, -1, -1), f'L1: 修理日数・時価額が前の案件のまま: {ins}')
    fi = one(db, 'SELECT Note2, Note3, GroupKey, GarageInDate, GarageOutDate FROM FileInfo')
    chk(fi == ('', '', '', '00000000', '00000000'), f'L1: 備考・グループキー・入出庫日が前の案件のまま: {fi}')
    cs = one(db, 'SELECT ps_CarSerialNo, ps_CarSerialNoHead, ps_CarSerialNoTail, ps_CarMouldNo, ps_CarKindNo, ps_CarRegDate, '
                 'nm_CarNameByUser, ev_CarName, ms_CarSerialNoHead, ms_CarSerialNoTail FROM CarSearch')
    chk(cs == ('AB12-3456789', 'AB12', '3456789', '12345', '0001', '20230400', 'テストカー', 'テストカー', '', ''),
        f'L1: CarSearch が新しい車にそろわない: {cs}')
    chk(one(db, 'SELECT ColorCodeFlag, ColorCode FROM Car') == (1, 'W99'), 'L18: カラーコードの旗')
    chk(one(db, 'SELECT ProjectCompletedFlag, DisasterFlag FROM Statistics') == ('0', '0'), 'L17: Statistics の旗が 0 でない')
    # 税率・計算単位は計算に合わせて 10%・請求書単位・1 円。端数処理（tx_ArrangeFlag）はテンプレート（工場の設定）を残し、計算もそれに従う
    chk(one(db, 'SELECT TaxRate, tx_CalculateFlag, tx_Unit, tx_ArrangeFlag FROM Setting') == (10, 1, 1, 2),
        f"O3: 消費税の設定: {one(db, 'SELECT TaxRate, tx_CalculateFlag, tx_Unit, tx_ArrangeFlag FROM Setting')}")
    # 画像・目録・AnNote・ADASWork
    chk(one(f['AnSvIf0001.sld'], 'SELECT COUNT(*) FROM Image') == (0,), 'L1: 前の案件の画像が残る')
    chk(b'OLDPHOTO' not in f['AnSvIf0001.sld'], 'L1: 消した画像がファイルの空き領域に残る')
    chk(b'old.jpg' not in f['AnSvIg0001.sld'] and f['AnSvIg0001.sld'].endswith(b'[ImageSections]\r\n\r\n[Image1]\r\n'), 'L1: 画像の目録が前の案件のまま')
    chk(b'Flag=1' not in f['AnFlInfo'], 'L1: AnNote の Flag=1 が残る')
    chk(b'OLD ADAS' not in f['AnSvEm0001.sld'], 'L1: [ADASWork] が前の案件のまま')
    # 前の案件の氏名・車台番号が DB のどこにも残らない（空き領域も）
    for old in ('前案件'.encode('utf-8'), b'ZZZ99-9999999', b'OLD-POLICY'):
        chk(old not in db, f'L1: DB の中に前の案件の値が残る: {old!r}')


def test_ansvmail_from_db():
    for merge in (False, True):
        neo, *_ = app.generate_neo_file(dirty_template(3), CUST, ITEMS, 0, INS, merge_mode=merge)
        f, _m, _e = unpack(neo)
        ini = f['AnSvImge.ini'].decode('cp932')
        kv = dict(re.findall(r'^(\w+)=([^\r\n]*)', ini, re.M))
        db = f['AnSvEm0001Ex.db']
        ins = one(db, 'SELECT ContractorName, PolicyNo, AccidentDate, ConsultantFactory FROM Insurance')
        cu = one(db, 'SELECT CarRegNoDepartment, CarRegNoDivision, CarRegNoBusiness, CarRegNoSerial FROM Customer')
        want = {'Signature': 'NEOMAIL2', 'CustomerName': ins[0], 'TicketNo': ins[1], 'AccidentDate': ins[2], 'AgreedName': ins[3],
                'AcceptNo': one(db, 'SELECT AcceptNo FROM FileInfo')[0], 'CarName': one(db, 'SELECT CarNameByUser FROM Car')[0],
                'CarNoDepartment': cu[0], 'CarNoDivision': cu[1], 'CarNoBusiness': cu[2], 'CarNoSerial': cu[3]}
        for k, v in want.items():
            chk(kv.get(k) == v, f'L4: AnSvMail.ini の {k} が DB と違う（merge={merge}）: {kv.get(k)!r} / {v!r}')
        chk(kv.get('CustomerName') == '新 契約者' and kv.get('TicketNo') == 'NEW-POLICY' and kv.get('AgreedName') == '新工場',
            f'L4: 契約者・証券番号・相手工場（merge={merge}）: {kv}')
        chk(list(kv)[:3] == ['Signature', 'CustomerName', 'CarNoDepartment'], f'L4: キーの並び: {list(kv)}')
        chk(ini.endswith('\r\n') and '\n' not in ini.replace('\r\n', ''), 'L4: 改行が CRLF でない')
    # マージ: 画像はテンプレート（同じ案件）のまま、CarSearch の ps_* は新しい値
    neo, *_ = app.generate_neo_file(dirty_template(3), CUST, ITEMS, 0, INS, merge_mode=True)
    f, _m, _e = unpack(neo)
    chk(one(f['AnSvIf0001.sld'], 'SELECT COUNT(*) FROM Image') == (1,), 'マージ: テンプレートの画像を消している')
    chk(one(f['AnSvEm0001Ex.db'], 'SELECT ps_CarSerialNo FROM CarSearch') == ('AB12-3456789',), 'L1: マージで CarSearch の車台番号が前の車のまま')


def test_generic_template_clears_ps():
    neo, *_ = app.generate_neo_file(dirty_template(1), CUST, ITEMS, 0, INS, merge_mode=False)
    f, _m, _e = unpack(neo)
    cs = one(f['AnSvEm0001Ex.db'], 'SELECT ps_CarSerialNo, ps_CarMouldNo, ps_CarRegDate FROM CarSearch')
    chk(cs == ('', '', ''), f'L1: 汎用車種（SearchMethod=1）の ps_* が空にならない: {cs}')


def test_xml_dates_and_total():
    neo, *_ = app.generate_neo_file(TPL, CUST, ITEMS, 0, dict(INS, garage_in_date='20260903'), merge_mode=False)
    f, _m, _e = unpack(neo)
    x = f['AnSvMail.ini'].decode('cp932')
    tag = lambda t: (re.search(r'<%s>([^<]*)</%s>' % (t, t), x) or [None, None])[1]  # noqa: E731
    chk(tag('AccidentDate') == '2026/09/01', f"L14: XML の事故日: {tag('AccidentDate')!r}")
    chk(tag('GarageInDate') == '' and tag('GarageOutDate') == '', 'L14: XML の入出庫日は空')
    chk(one(f['AnSvEm0001Ex.db'], 'SELECT GarageInDate FROM FileInfo') == ('20260903',), 'L14: DB の入庫日')
    # マージで総額 0 → XML の Total も 0（テンプレートの総額を残さない）
    tpl2, *_ = app.generate_neo_file(TPL, CUST, ITEMS, 0, INS, merge_mode=False)
    neo2, *_ = app.generate_neo_file(tpl2, {}, [], 0, {}, merge_mode=True)
    x2 = unpack(neo2)[0]['AnSvMail.ini'].decode('cp932')
    chk(re.search(r'<Total>0</Total>', x2) is not None, f"L15: マージで総額 0 のとき XML の Total が前の値: {re.search(r'<Total>[^<]*</Total>', x2)}")


def test_minus_one_yen_refused():
    items = ITEMS + [{'name': '端数値引', 'quantity': 1, 'parts_amount': 0, 'wage': -1}]
    try:
        app.generate_neo_file(TPL, CUST, items, 0, INS)
        FAILS.append('L2: −1 円の行で止まらない')
    except ValueError as e:
        chk('−1' in str(e) and '端数値引' in str(e), f'L2: 止める理由: {e}')
    # −2 円・−10 円は書ける
    items = ITEMS + [{'name': '端数値引', 'quantity': 1, 'parts_amount': 0, 'wage': -10}]
    neo, *_ = app.generate_neo_file(TPL, CUST, items, 0, INS)
    chk(bool(neo), 'L2: −10 円の行が書けない')


def test_tax_inclusive_distribution_bounded():
    items = [{'name': f'ｸﾘｯﾌﾟ{i}', 'method': '取替', 'quantity': 10, 'parts_amount': 1050, 'wage': 0} for i in range(10)]
    items += [{'name': 'ｽﾃｯｶｰ', 'method': '取替', 'quantity': 1, 'parts_amount': 110, 'wage': 0},
              {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'quantity': 1, 'parts_amount': 0, 'wage': 5500}]
    neo, tp, tw, gt = app.generate_neo_file(TPL, CUST, items, 0, INS, is_tax_inclusive=True)
    f, _m, _e = unpack(neo)
    c = mem(f['AnSMB.txt'])
    try:
        rows = c.execute('SELECT PartsName, PartsPriceOutTax, PartsPriceInTax, PartsPriceTax, WageOutTax, WageInTax, WageTax FROM ERParts ORDER BY LineNo').fetchall()
        total = c.execute('SELECT Total FROM Total').fetchone()[0]
    finally:
        c.close()
    st = [r for r in rows if r[0] == 'ｽﾃｯｶｰ'][0]
    chk(abs(st[1] - 100) <= 1 and st[3] >= 0, f'O4: ステッカー行の税抜が自然な逆算（100）から離れる・税が負: {st}')
    chk(all((r[3] >= 0 or r[1] < 0) for r in rows if r[1] != -1), 'O4: 税額が負の行がある')
    chk(total == 10 * 1050 + 110 + 5500, f'O4: 総額が税込の合計と違う: {total}')


def test_control_chars_same_in_erparts_and_ansmb():
    items = [{'name': 'ﾌﾛﾝﾄ\tﾊﾞﾝﾊﾟ\n', 'part_no': '52119-\n08000', 'method': '取替', 'quantity': 1, 'parts_amount': 1000, 'wage': 0}]
    neo, *_ = app.generate_neo_file(TPL, CUST, items, 0, INS)
    f, _m, _e = unpack(neo)
    nm, pn = one(f['AnSMB.txt'], 'SELECT PartsName, PartsNo FROM ERParts')
    chk(not re.search(r'[\x00-\x1f]', nm + pn), f'L5: ERParts に制御文字が残る: {nm!r} {pn!r}')
    rec = f['AnNote.ini'][:142]
    chk(rec[14:38].decode('cp932').rstrip() == nm.rstrip() and rec[62:80].decode('cp932').rstrip() == pn.rstrip(),
        f'L5: AnSMB と ERParts の品名・品番が違う: {rec[14:38]!r} {rec[62:80]!r} / {nm!r} {pn!r}')


def test_fractional_quantity():
    items = [{'name': 'ｵｲﾙ', 'method': '取替', 'quantity': '2.5', 'parts_amount': 2750, 'wage': 0}]
    neo, *_ = app.generate_neo_file(TPL, CUST, items, 0, INS)
    f, _m, _e = unpack(neo)
    r = one(f['AnSMB.txt'], 'SELECT PartsCount, PartsPriceOutTax, PartsPriceTax FROM ERParts')
    chk(r == (1, 2750, 275), f'L7: 小数の数量の行: {r}')
    chk(app.safe_int('1234.5') == 1235 and app.safe_int(2.5) == 3 and app.safe_int('-2.5') == -3, 'O12: safe_int の .5 が四捨五入でない')
    items, notes = app.parse_csv_to_items('品名,区分,数量,部品金額,工賃\nｵｲﾙ,取替,2.5,2750,0\n', return_notes=True)
    chk(items and items[0]['quantity'] == 1 and any('2.5' in n for n in notes), f'L7: CSV の小数の数量: {items} {notes}')


def test_fake_ck_in_table():
    real_now = app.now_jst
    try:
        app.now_jst = lambda: datetime.datetime(2026, 9, 15, 9, 26, 6)   # DOS 時刻 0x4B43 = 'CK'
        neo, *_ = app.generate_neo_file(TPL, CUST, ITEMS, 0, INS)
    finally:
        app.now_jst = real_now
    first = neo.find(b'CK', 424)
    ck = app.find_real_cks(neo)
    chk(ck and ck[0] != first, f'L9: 表の中の偽の CK を拾った（最初の CK {first}, 採用 {ck[:1]}）')
    try:
        f, _m, _e = unpack(neo)
        chk('AnSvEm0001Ex.db' in f, 'L9: 展開できない')
    except Exception as e:  # noqa: BLE001
        FAILS.append(f'L9: 09:26:06 に作った .neo が展開できない: {e}')


def test_unencodable_names():
    neo, *_ = app.generate_neo_file(TPL, dict(CUST, customer_name='𠮷田 一郎'), ITEMS, 0, INS)
    f, _m, _e = unpack(neo)
    chk(one(f['AnSvEm0001Ex.db'], 'SELECT Name1 FROM Customer') == ('吉田 一郎',), 'L12: 𠮷 が吉にならない')
    import neo_header
    chk(neo_header.unencodable_chars('A😀B') == ['😀'] and not neo_header.unencodable_chars('〜𠮷—'), 'L12: 書けない字の判定')


def test_address_keeps_room_number():
    t = app._trimmed_cust_values({'address_other': '1-2-3 ｸﾞﾗﾝﾄﾞﾒｿﾞﾝﾊﾟｰｸｻｲﾄﾞ 1015'})
    chk(t['address_other'].endswith('1015'), f"L6: 部屋番号が消える: {t['address_other']!r}")
    # 全角数字の住所は半角に畳む（全角のままだと 30 バイトで部屋番号が欠ける。レビュー）
    t = app._trimmed_cust_values({'prefecture': '大阪府', 'municipality': '大阪市北区', 'address_other': '梅田３－４－５　梅田ビル２０１'})
    chk(t['address_other'] == '北区梅田3-4-5梅田ビル201', f"L6: 全角数字の住所: {t['address_other']!r}")


def _neo_rows(neo, sql):
    f, _m, _e = unpack(neo)
    c = mem(f['AnSMB.txt'])
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_details_db_secure_delete():
    """前の案件の明細（品名・品番）が明細 DB の空き領域に残らない（レビュー）"""
    old = [{'name': f'MARKERPART{i:03d}', 'part_no': f'OLDPNO{i:03d}', 'method': '取替', 'quantity': 1, 'parts_amount': 1000 + i, 'wage': 0}
           for i in range(60)]
    tpl, *_ = app.generate_neo_file(TPL, CUST, old, 0, INS)
    neo, *_ = app.generate_neo_file(tpl, CUST, ITEMS, 0, INS, merge_mode=False)
    f, _m, _e = unpack(neo)
    chk(b'MARKERPART' not in f['AnSMB.txt'] and b'OLDPNO' not in f['AnSMB.txt'], 'レビュー: 前の案件の明細が明細 DB の空き領域に残る')


def test_tax_inclusive_keeps_10_percent():
    """税込表記: 数量 2 以上の行を含んでも、ふつうの見積では 消費税 = 課税額計の 10%（旧来どおり）。1 行の上限で O4 は防ぐ（レビュー）"""
    for items, sub_want, tax_want in (
        ([{'name': 'ｸﾘｯﾌﾟ', 'method': '取替', 'quantity': 10, 'parts_amount': 26430, 'wage': 0},
          {'name': 'ﾊﾞﾝﾊﾟ', 'method': '取替', 'quantity': 1, 'parts_amount': 5080, 'wage': 0},
          {'name': '脱着', 'method': '脱着', 'quantity': 1, 'parts_amount': 0, 'wage': 27736}], 53860, 5386),
        ([{'name': 'ｸﾘｯﾌﾟ', 'method': '取替', 'quantity': 10, 'parts_amount': 1050, 'wage': 0},
          {'name': 'ﾊﾞﾝﾊﾟ', 'method': '取替', 'quantity': 1, 'parts_amount': 33000, 'wage': 0},
          {'name': '脱着', 'method': '脱着', 'quantity': 1, 'parts_amount': 0, 'wage': 11000}], None, 4095)):
        neo, *_ = app.generate_neo_file(TPL, CUST, items, 0, INS, is_tax_inclusive=True)
        t = _neo_rows(neo, 'SELECT SubTotal, tx_TotalOutTax, Total FROM Total')[0]
        chk(t[2] == sum(i['parts_amount'] + i['wage'] for i in items), f'税込: 総額が変わる: {t}')
        chk(t[1] == tax_want and (sub_want is None or t[0] == sub_want), f'税込: 課税額計・消費税が旧来と違う（10% が崩れる）: {t}')
    # 数量行の単価欄は税込表記では空欄（税込の単価が原本と合わなくなる）
    neo, *_ = app.generate_neo_file(TPL, CUST, [{'name': 'ｸﾘｯﾌﾟ', 'method': '取替', 'quantity': 10, 'parts_amount': 1050, 'wage': 0}], 0, INS,
                                    is_tax_inclusive=True)
    r = _neo_rows(neo, 'SELECT PartsUnitPriceOutTax, PartsUnitPriceInTax FROM ERParts')[0]
    chk(r == (-1, -1), f'税込表記の数量行に単価欄を書いている: {r}')
    # 5 円の行と数量 10 の行 3 本: 税が負・−1 の行を作らない
    items = [{'name': 'ﾜｯｼｬｰ', 'method': '取替', 'quantity': 1, 'parts_amount': 5, 'wage': 0}] + \
            [{'name': f'ﾎﾞﾙﾄ{i}', 'method': '取替', 'quantity': 10, 'parts_amount': 1050, 'wage': 0} for i in range(3)]
    neo, *_ = app.generate_neo_file(TPL, CUST, items, 0, INS, is_tax_inclusive=True)
    rows = _neo_rows(neo, 'SELECT PartsName, PartsPriceOutTax, PartsPriceInTax, PartsPriceTax FROM ERParts ORDER BY LineNo')
    chk(all(r[3] >= 0 for r in rows), f'税込: 税が負の行がある: {rows}')


def test_minus_ten_yen_tax_not_blank():
    neo, *_ = app.generate_neo_file(TPL, CUST, ITEMS + [{'name': '端数値引', 'quantity': 1, 'parts_amount': 0, 'wage': -10}], 0, INS)
    r = _neo_rows(neo, "SELECT WageOutTax, WageInTax, WageTax FROM ERParts WHERE PartsName='端数値引'")[0]
    chk(r[2] != -1 and r[0] == -10, f'−10 円の値引の税の欄が −1（空欄の印）: {r}')


def test_template_tax_round_kept():
    files, mgmt, ent = unpack(TPL)
    c = mem(files['AnSvEm0001Ex.db'])
    c.execute('UPDATE Setting SET tx_ArrangeFlag=2')
    c.commit()
    files['AnSvEm0001Ex.db'] = dump(c)
    c.close()
    tpl = app.repack_neo(TPL, files, mgmt, ent)
    items = [{'name': 'ﾊﾞﾝﾊﾟ', 'method': '取替', 'quantity': 1, 'parts_amount': 60005, 'wage': 40000}]
    neo, *_ = app.generate_neo_file(tpl, CUST, items, 0, INS, merge_mode=False)
    t = _neo_rows(neo, 'SELECT SubTotal, tx_TotalOutTax, Total FROM Total')[0]
    f, _m, _e = unpack(neo)
    chk(t == (100005, 10000, 110005), f'切り捨ての工場のテンプレートで四捨五入している: {t}')
    chk(one(f['AnSvEm0001Ex.db'], 'SELECT tx_ArrangeFlag FROM Setting') == (2,), '切り捨ての設定を上書きしている')


def _round_down_template():
    files, mgmt, ent = unpack(TPL)
    c = mem(files['AnSvEm0001Ex.db'])
    c.execute('UPDATE Setting SET tx_ArrangeFlag=2')
    c.commit()
    files['AnSvEm0001Ex.db'] = dump(c)
    c.close()
    return app.repack_neo(TPL, files, mgmt, ent)


def test_verify_and_screen_use_template_rounding():
    """verify と画面も、NEO と同じ消費税の端数処理（テンプレートの設定）で総額を出す（レビュー 2 周目: 切り捨てのテンプレートで
    1 円少ない NEO が合格し、正しい NEO が不一致になっていた）"""
    import pdf_to_neo_pipeline as P
    tpl = _round_down_template()
    chk(app._neo_tax_round(tpl) == ('切り捨て', 2), f'テンプレートの端数処理を読めない: {app._neo_tax_round(tpl)}')
    chk(app._neo_tax_round(TPL) == ('四捨五入', 1), '標準テンプレートの端数処理')
    items = [{'name': 'ﾊﾞﾝﾊﾟ', 'method': '取替', 'quantity': 1, 'parts_amount': 60005, 'wage': 40000}]
    neo, *_ = app.generate_neo_file(tpl, CUST, items, 0, INS, merge_mode=False)
    v_ok = P.verify_neo_against_pdf(neo, items, pdf_parts_total=60005, pdf_wage_total=40000, pdf_grand_total=110005)
    v_ng = P.verify_neo_against_pdf(neo, items, pdf_parts_total=60005, pdf_wage_total=40000, pdf_grand_total=110006)
    chk(v_ok.get('grand_match') is True, f'切り捨ての正しい NEO を総額不一致にしている: {v_ok.get("neo_grand_total")}')
    chk(v_ng.get('grand_match') is False, f'1 円違う総額を合格にしている: {v_ng.get("neo_grand_total")}')
    v_ex = P.verify_neo_against_pdf(neo, items, pdf_parts_total=60005, pdf_wage_total=40000, pdf_grand_total=100005,
                                    grand_is_intax=False)
    chk(v_ex.get('grand_match') is True and v_ex.get('grand_is_intax') is False, f'税抜で印字された総額: {v_ex}')


def test_tax_inclusive_round_down_template():
    """税込表記でも、課税額計＋テンプレートの端数処理の税 ＝ 総額（切り捨てのテンプレートで 1 円ずれていた。レビュー 2 周目）"""
    tpl = _round_down_template()
    bad = []
    for v in (69379, 11000, 10999, 54321, 123457):
        items = [{'name': 'ﾊﾞﾝﾊﾟ', 'method': '取替', 'quantity': 1, 'parts_amount': v, 'wage': 0}]
        neo, *_ = app.generate_neo_file(tpl, CUST, items, 0, INS, is_tax_inclusive=True, merge_mode=False)
        sub, tax, tot = _neo_rows(neo, 'SELECT SubTotal, tx_TotalOutTax, Total FROM Total')[0]
        if tot != v or (v % 11 != 10 and (sub + (sub * 10) // 100 != tot)):
            bad.append((v, sub, tax, tot))
    chk(not bad, f'税込表記で課税額計と切り捨ての税が総額と合わない: {bad}')


def test_expense_rows_and_template_without_row10():
    files, mgmt, ent = unpack(TPL)
    c = mem(files['AnSMB.txt'])
    c.execute('DELETE FROM Expense WHERE LineNo>=10')
    c.commit()
    files['AnSMB.txt'] = dump(c)
    c.close()
    tpl = app.repack_neo(TPL, files, mgmt, ent)
    neo, *_ = app.generate_neo_file(tpl, CUST, ITEMS, 0, INS, expenses={'tax_exempt': 3000})
    rows = _neo_rows(neo, 'SELECT LineNo, Name, OutTaxFlag, WageOutTax FROM Expense WHERE WageEnabled=1')
    chk(rows == [(9, '非課税費用', 1, 3000)], f'10 行目の無いテンプレートで非課税が行に入らない: {rows}')
    try:
        app.generate_neo_file(tpl, CUST, ITEMS, 0, INS, expenses={'tax_exempt': 3000, 'rental_car': 5000})
        FAILS.append('空いた費用行が足りないのに止まらない（合計にだけ入る）')
    except ValueError as e:
        chk('空いた行' in str(e), f'費用行が足りないときの理由: {e}')


def test_adas_work_bracket_in_value():
    ini = ('[General]\r\nSignature=SVEM\r\nVersion=0001\r\n[PaintingPlan]\r\nIdx1.FormShow=0\r\n[ADASWork]\r\n'
           'Idx1.PartsCode=`A010`\r\nIdx1.ItemName=`前方ｶﾒﾗ[単眼]ｴｰﾐﾝｸﾞ`\r\nIdx2.Comment=`GTS使用 前案件`\r\n[After]\r\nX=1\r\n').encode('cp932')
    out = app._reset_adas_work(ini).decode('cp932')
    chk('前案件' not in out and '単眼' not in out and '[After]\r\nX=1' in out and out.count('[ADASWork]') == 1,
        f'[ADASWork] の値の中の [ で止まる: {out!r}')


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
    print('reg_legacy3:', 'all ok' if not FAILS else f'{len(FAILS)} 件が不合格')
    sys.exit(1 if FAILS else 0)
