# -*- coding: utf-8 -*-
"""cbaa9f8 で直した NEO 生成精度の回帰テスト。
同じバグが二度出ないようにするのと、後の周で自分が壊したときに気づくため。"""
import sys, os, sqlite3, tempfile
R=os.environ.get('XROOT',os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.path.insert(0,R); os.chdir(R)
import app, neogen

TPL = open('template_toyota.neo', 'rb').read()
FAIL = []

def chk(cond, msg):
    if not cond: FAIL.append(msg)

def gen(items, **kw):
    kw.setdefault('short_parts_wage', 0)
    out = app.generate_neo_file(TPL, kw.pop('cust', {}), items, kw.pop('short_parts_wage'),
                                {}, kw.pop('expenses', {}), kw.pop('tax_in', False),
                                kw.pop('beta', False), False)
    return neogen.opendb(neogen.unpack(out[0])['AnSMB.txt']).cursor()

# 1. DisposalCode は 取替=0 / 脱着=1 / 修理・鈑金・塗装=2（実機NEO244件で確定）
cur = gen([{'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1},
           {'name': 'B', 'method': '脱着', 'parts_amount': 0, 'wage': 1000, 'quantity': 1},
           {'name': 'C', 'method': '鈑金', 'parts_amount': 0, 'wage': 1000, 'quantity': 1},
           {'name': 'D', 'method': '塗装', 'parts_amount': 0, 'wage': 1000, 'quantity': 1},
           {'name': 'E', 'method': '修理', 'parts_amount': 0, 'wage': 1000, 'quantity': 1}])
got = [r[0] for r in cur.execute('select DisposalCode from ERParts order by RecordNo')]
chk(got == [0, 1, 2, 2, 2], f'1: DisposalCode {got} != [0,1,2,2,2]')

# 括弧書き・空白付きでも同じコードに落ちること
cur = gen([{'name': 'A', 'method': '脱着（左）', 'parts_amount': 0, 'wage': 1, 'quantity': 1},
           {'name': 'B', 'method': ' 取替 ', 'parts_amount': 1, 'wage': 0, 'quantity': 1}])
got = [r[0] for r in cur.execute('select DisposalCode from ERParts order by RecordNo')]
chk(got == [1, 0], f'1b: 正規化後の DisposalCode {got} != [1,0]')

# 2. 指数が ERParts.Time に入ること／未入力は -1（空欄）のまま
cur = gen([{'name': 'A', 'method': '取替', 'parts_amount': 1, 'wage': 0, 'quantity': 1, 'index_value': '2.5'},
           {'name': 'B', 'method': '取替', 'parts_amount': 1, 'wage': 0, 'quantity': 1, 'index_value': ''},
           {'name': 'C', 'method': '取替', 'parts_amount': 1, 'wage': 0, 'quantity': 1}])
got = [r[0] for r in cur.execute('select Time from ERParts order by RecordNo')]
chk(got == [2.5, -1, -1], f'2: Time {got} != [2.5,-1,-1]')

# 金額列がバインドずれで壊れていないこと（Time追加でずれた場合ここが落ちる）
cur = gen([{'name': 'A', 'method': '取替', 'parts_amount': 48000, 'wage': 7700,
            'quantity': 3, 'index_value': '1.2'}])
row = cur.execute('select PartsPriceOutTax, WageOutTax, PartsCount, Time from ERParts').fetchone()
chk(row == (48000, 7700, 3, 1.2), f'2b: 列ずれ {row} != (48000,7700,3,1.2)')

# 3. 未マッチ部品の ※ が明細タブを通っても消えないこと
for label, extra in (('照合直後', {'match_level': 'L4'}),
                     ('明細タブ後', {'match_level': 'L4', '_match_level': 0})):
    cur = gen([dict({'name': '未マッチ', 'method': '取替', 'parts_amount': 1,
                     'wage': 0, 'quantity': 1}, **extra)])
    nm = cur.execute('select PartsName from ERParts').fetchone()[0]
    chk(nm.startswith('※'), f'3: {label} で ※ が付かない: {nm!r}')
# ベタ打ちモードでは ※ を付けない
cur = gen([{'name': '未マッチ', 'method': '取替', 'parts_amount': 1, 'wage': 0,
            'quantity': 1, 'match_level': 'L4'}], beta=True)
nm = cur.execute('select PartsName from ERParts').fetchone()[0]
chk(not nm.startswith('※'), f'3b: ベタ打ちなのに ※ が付いた: {nm!r}')

# 4. Addata の部品コード大区分が ERParts.PartsCode に届くこと
cur = gen([{'name': 'A', 'method': '取替', 'parts_amount': 1, 'wage': 0, 'quantity': 1,
            '_master_section_code': '01', '_master_branch_code': '00101'},
           {'name': 'B', 'method': '取替', 'parts_amount': 1, 'wage': 0, 'quantity': 1}])
got = cur.execute('select PartsCode, PartsCodeSub from ERParts order by RecordNo').fetchall()
chk(got == [('01', 101), ('', -1)], f'4: PartsCode {got}')

# 5. 前案件の .neo をテンプレートにしても予備明細が残らないこと
prev = app.generate_neo_file(TPL, {}, [
    {'name': '前案件A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1},
    {'name': '前案件B', 'method': '取替', 'parts_amount': 2000, 'wage': 0, 'quantity': 1}],
    0, {}, {}, False, False, False)[0]
ck = app.find_real_cks(prev); raw = app.decompress_neo(prev, ck)
mgmt, ent = app.parse_entries(prev, ck[0]); files = app.extract_files(raw, ent)
tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False); tf.write(files['AnSMB.txt']); tf.close()
cn = sqlite3.connect(tf.name)
cn.execute("UPDATE ReserveERParts SET PartsName='前案件の予備', ERPartsRecordNo=2, PartsPriceOutTax=9999")
cn.execute("UPDATE PaintingOther SET Name='前案件のスポイラー塗装', WageOutTax=12000")
cn.commit(); cn.close()
files['AnSMB.txt'] = open(tf.name, 'rb').read(); os.unlink(tf.name)
prev = app.repack_neo(prev, files, mgmt, ent)

new = app.generate_neo_file(prev, {}, [{'name': '新X', 'method': '取替',
                                        'parts_amount': 500, 'wage': 0, 'quantity': 1}],
                            0, {}, {}, False, False, False)[0]
c2 = neogen.opendb(neogen.unpack(new)['AnSMB.txt']).cursor()
res = c2.execute('select RecordNo, PartsName, ERPartsRecordNo, PartsPriceOutTax,'
                 ' DisposalCode, WageByManual from ReserveERParts').fetchall()
chk(res == [(1, '', 0, -1, 3, '*')], f'5: ReserveERParts が空行1件に戻っていない: {res}')
# 6. PaintingOther の作業名も残らないこと
po = c2.execute('select Name, WageOutTax from PaintingOther').fetchone()
chk(po == ('', -1), f'6: PaintingOther が残留: {po}')

# 7. 塗装テーブルの ByManual は '' （-1 を書くと値域外の2文字になる）
for t in ('PaintingBumper', 'PaintingFrame', 'PaintingEtcetera'):
    cols = [x[1] for x in c2.execute(f'PRAGMA table_info({t})')]
    row = c2.execute(f'select * from {t}').fetchone()
    if not row: continue
    bad = {c: v for c, v in zip(cols, row) if 'ByManual' in c and v != ''}
    chk(not bad, f'7: {t} の ByManual が空でない: {bad}')

# ── 内包ファイル側の前案件残留（round 6） ──
import re as _re

# 8. 出荷テンプレートの自由入力費目名が全生成物に付いて回らないこと。
#    固定費目名(NameFix=1)の LineNo1〜8 は消してはいけない。
cur = gen([{'name': 'A', 'method': '取替', 'parts_amount': 100, 'wage': 0, 'quantity': 1}])
names = [(r[0], r[1]) for r in cur.execute('select LineNo, Name from Expense') if r[1]]
chk([n for _, n in names] == ['文字書き費用', '内張り費用', '配線・配管費用', 'ショートパーツ',
                              'レッカー代１', 'レッカー代２', '写真代他', 'その他控除'],
    f'8: Expense.Name {names}')

# 9. 前案件の Fixer(金額付き調整行) / Expense自由行 / Statistics が残らないこと
prev = app.generate_neo_file(TPL, {}, [{'name': '前A', 'method': '取替',
                                        'parts_amount': 1000, 'wage': 0, 'quantity': 1}],
                             0, {}, {}, False, False, False)[0]
ck = app.find_real_cks(prev); raw = app.decompress_neo(prev, ck)
mgmt, ent = app.parse_entries(prev, ck[0]); files = app.extract_files(raw, ent)
tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False); tf.write(files['AnSMB.txt']); tf.close()
cn = sqlite3.connect(tf.name)
cn.execute("UPDATE Fixer SET Name='前案件の調整', Enabled=1, Price=66666 WHERE LineNo=1")
cn.execute("UPDATE Expense SET Name='前案件の特別費用' WHERE LineNo=9")
cn.commit(); cn.close()
files['AnSMB.txt'] = open(tf.name, 'rb').read(); os.unlink(tf.name)
tf2 = tempfile.NamedTemporaryFile(suffix='.db', delete=False); tf2.write(files['AnSvEm0001Ex.db']); tf2.close()
cn2 = sqlite3.connect(tf2.name)
cn2.execute("UPDATE Statistics SET EstimationId='EST-PREV', ProjectNo='PRJ-PREV', DefiniteOutTax=555555")
cn2.commit(); cn2.close()
files['AnSvEm0001Ex.db'] = open(tf2.name, 'rb').read(); os.unlink(tf2.name)
prev = app.repack_neo(prev, files, mgmt, ent)

new = app.generate_neo_file(prev, {}, [{'name': '新X', 'method': '取替',
                                        'parts_amount': 100, 'wage': 0, 'quantity': 1}],
                            0, {}, {}, False, False, False)[0]
c3 = neogen.opendb(neogen.unpack(new)['AnSMB.txt']).cursor()
chk(c3.execute('select Name, Enabled, Price from Fixer where LineNo=1').fetchone() == ('', 0, 0),
    '9a: 前案件の Fixer が残った')
chk(c3.execute('select Name from Expense where LineNo=9').fetchone()[0] == '',
    '9b: 前案件の Expense 費目名が残った')
e3 = neogen.opendb(neogen.unpack(new)['AnSvEm0001Ex.db']).cursor()
st = e3.execute('select EstimationId, ProjectNo, DefiniteOutTax from Statistics').fetchone()
chk(st == ('', '', -1), f'9c: 前案件の Statistics が残った: {st}')

# 10. ヘッダXMLの初度登録が、合成文字列と構造化タグでDBと矛盾しないこと
out10 = app.generate_neo_file(TPL, {'car_reg_date': '20200100'},
                              [{'name': 'X', 'method': '取替', 'parts_amount': 100,
                                'wage': 0, 'quantity': 1}], 0, {}, {}, False, False, False)[0]
fs10 = neogen.unpack(out10)
xml10 = [v for v in fs10.values() if b'AudaNeo2Data' in v[:300]][0].decode('cp932', 'replace')
tag = lambda t: (_re.search(r'<%s>(.*?)</%s>' % (t, t), xml10) or [None, None])[1]
db10 = neogen.opendb(fs10['AnSvEm0001Ex.db']).cursor().execute(
    'select CarRegDate, CarRegEraYear from Customer').fetchone()
chk(tag('CarRegistedDateYear') == db10[1] and tag('CarRegistedDateMonth') == db10[0][4:6],
    f"10: XML({tag('CarRegistedDateYear')}/{tag('CarRegistedDateMonth')}) と DB{db10} が食い違う")

# 11. 前案件の立会者名・伝票番号・備考がヘッダXMLに残らないこと
xml11 = [v for v in neogen.unpack(new).values() if b'AudaNeo2Data' in v[:300]][0].decode('cp932', 'replace')
for t in ('TicketNo', 'Note2', 'Note3', 'ii_CustomerName', 'GradeName', 'CustomerName2'):
    m = _re.search(r'<%s>(.*?)</%s>' % (t, t), xml11)
    chk(m is None or m.group(1) == '', f'11: {t} が残留: {m.group(1) if m else ""!r}')

# ── 自分の修正が入れた回帰（round 7） ──

# 12. 指数は金額と同じ正規化を通す。全角・単位付きが落ちず、
#     丸めて0になる値や inf が Time に入らないこと。
_cases = [('1.5', 1.5), ('１．５', 1.5), ('２．５', 2.5), ('1.5h', 1.5),
          ('2.0時間', 2.0), ('(0.8)', -1), ('0.001', -1), ('-1.0', -1),
          ('inf', -1), ('nan', -1), ('', -1)]
cur = gen([{'name': f'P{i}', 'method': '取替', 'parts_amount': 1000, 'wage': 0,
            'quantity': 1, 'index_value': c} for i, (c, _) in enumerate(_cases)])
got = [r[0] for r in cur.execute('select Time from ERParts order by RecordNo')]
for (raw, want), have in zip(_cases, got):
    chk(have == want, f'12: index_value {raw!r} -> Time={have!r} (期待 {want!r})')

# 13. 品名が空の行に ※ だけを書かない。金額は落とさない。
cur = gen([{'name': '', 'method': '取替', 'parts_amount': 500, 'wage': 0,
            'quantity': 1, 'match_level': 'L4', '_match_level': 0}])
row = cur.execute('select PartsName, PartsPriceOutTax from ERParts').fetchone()
chk(row == ('', 500), f'13: 品名空の行 {row} (期待 ("",500))')
# 品名がある未マッチ行には引き続き ※ が付く
cur = gen([{'name': '未マッチ品', 'method': '取替', 'parts_amount': 500, 'wage': 0,
            'quantity': 1, 'match_level': 'L4', '_match_level': 0}])
chk(cur.execute('select PartsName from ERParts').fetchone()[0] == '※未マッチ品',
    '13b: 名前のある未マッチ行に ※ が付かない')

# 14. 未マッチ行に ※ を付けても、末尾の左右が消えないこと。
#     列幅ちょうどの品名だと「…カバー左」と「…カバー右」が
#     両方「※…カバー」になり、左右の部品が同じ文字列で並んでいた。
_seen = {}
for _n in ('フロントバンパーカバー左', 'フロントバンパーカバー右',
           'フロントドアパネルアウタ左側', 'フロントドアパネルアウタ右側',
           'リヤコンビネーションランプ左', 'リヤコンビネーションランプ右',
           # 左右の直前に空白がある形。空白を「左右表記」として温存すると
           # 22バイトの持ち分を空白が食い、識別に必要な語尾から先に消えて
           # 「…アウタ R」と「…インナ R」が同じ文字列になっていた。
           'フロントドアパネル アウタ R', 'フロントドアパネル インナ R',
           'フロントドアパネル　アウタ　Ｒ', 'フロントドアパネル　インナ　Ｒ'):
    cur = gen([{'name': _n, 'method': '取替', 'parts_amount': 1000, 'wage': 0,
                'quantity': 1, 'match_level': 'L4'}])
    _got = cur.execute('select PartsName from ERParts').fetchone()[0]
    chk(len(_got.encode('cp932', 'replace')) <= 24, f'14: {_n} -> {_got!r} が24バイト超')
    for _side in ('左', '右'):
        if _side in _n:
            chk(_side in _got, f'14: {_n} -> {_got!r} で「{_side}」が消えた')
    _seen.setdefault(_got, []).append(_n)
_dup = {k: v for k, v in _seen.items() if len(v) > 1}
chk(not _dup, f'14: 別部品が同じ文字列になった: {_dup}')

# 15. 税込表記で、丸めの差額を1行に寄せないこと。
#     寄せると明細が増えるほどその1行だけ税抜額が原本から離れる
#     （1,000行で1行が455円ずれていた）。同じ税込額の行なのに
#     1行だけ単価が違う見積は協定の場で説明できない。
import collections as _co
for _n in (100, 300, 1000):
    _items = [{'name': 'ｸﾘｯﾌﾟ', 'method': '取替', 'parts_amount': 18023,
               'wage': 0, 'quantity': 1} for _ in range(_n)]
    _o = app.generate_neo_file(TPL, {}, _items, 0, {}, {}, True, False, False)
    _c = neogen.opendb(neogen.unpack(_o[0])['AnSMB.txt']).cursor()
    _outs = [r[0] for r in _c.execute('select PartsPriceOutTax from ERParts')]
    chk(max(_outs) - min(_outs) <= 1,
        f'15: {_n}行で税抜額の幅が {max(_outs)-min(_outs)}円'
        f'（{min(_outs)}〜{max(_outs)}）。差額が1行に寄っている')
    chk(len(_outs) == _n, f'15b: {_n}行入れて {len(_outs)}行しか出ていない')

# 16. 品名が空で金額のある行は、黙って落とさず知らせること
_csv = ('品名,区分,数量,部品金額,工賃\n'
        'フロントバンパー,取替,1,45000,0\n'
        ',取替,1,12345,0\n')
_it, _notes = app.parse_csv_to_items(_csv, return_notes=True)
chk(len(_it) == 1, f'16: 取り込み行数 {len(_it)}（期待 1）')
chk(any('読み飛ばし' in n for n in _notes),
    f'16b: 落とした行の注記が出ていない: {_notes}')

print('REG_NEOACC:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL: print('  -', f)
sys.exit(1 if FAIL else 0)
