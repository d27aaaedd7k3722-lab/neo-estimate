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

# 1. DisposalCode は実機の基本定義ファイル AnDefine.ini [WorkSheet] のとおり
#    取替=0 / 脱着=1 / 修理=2 / 板金=6 / 脱着修理=3 / 点検・調整=4 / 分解調整=5。
#    塗装は定義に無いので、いちばん近い 2（修理）に寄せている。
#    実機の .neo 202件・明細11,254行でも同じ対応だった。
cur = gen([{'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1},
           {'name': 'B', 'method': '脱着', 'parts_amount': 0, 'wage': 1000, 'quantity': 1},
           {'name': 'C', 'method': '鈑金', 'parts_amount': 0, 'wage': 1000, 'quantity': 1},
           {'name': 'D', 'method': '塗装', 'parts_amount': 0, 'wage': 1000, 'quantity': 1},
           {'name': 'E', 'method': '修理', 'parts_amount': 0, 'wage': 1000, 'quantity': 1}])
got = [r[0] for r in cur.execute('select DisposalCode from ERParts order by RecordNo')]
chk(got == [0, 1, 6, 2, 2], f'1: DisposalCode {got} != [0,1,6,2,2]')

# 実機の定義表にある残りの区分も、定義どおりのコードに落ちること
cur = gen([{'name': 'F', 'method': '脱着修理', 'parts_amount': 0, 'wage': 1000, 'quantity': 1},
           {'name': 'G', 'method': '点検調整', 'parts_amount': 0, 'wage': 1000, 'quantity': 1},
           {'name': 'H', 'method': '分解調整', 'parts_amount': 0, 'wage': 1000, 'quantity': 1},
           {'name': 'I', 'method': '調整', 'parts_amount': 0, 'wage': 1000, 'quantity': 1}])
got = [r[0] for r in cur.execute('select DisposalCode from ERParts order by RecordNo')]
chk(got == [3, 4, 5, 4], f'1c: 複合区分の DisposalCode {got} != [3,4,5,4]')

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

# 4. Addata の参照番号が ERParts.PartsCode に4桁で届くこと。
#    実機の PartsCode は部品マスタの参照番号を4桁ゼロ埋めしたもので、
#    帳票のいちばん左「ｺｰﾄﾞ」列にそのまま印字される。
#    PartsCodeSub（枝番）は実機の自由入力行 536行すべてが -1 だった。
cur = gen([{'name': 'A', 'method': '取替', 'parts_amount': 1, 'wage': 0, 'quantity': 1,
            '_master_ref_no': '3810'},
           {'name': 'B', 'method': '取替', 'parts_amount': 1, 'wage': 0, 'quantity': 1}])
got = cur.execute('select PartsCode, PartsCodeSub from ERParts order by RecordNo').fetchall()
chk(got == [('3810', -1), ('', -1)], f'4: PartsCode {got}')

# 4b. 4桁でない参照番号は書かない（帳票の桁が崩れるため）
cur = gen([{'name': 'A', 'method': '取替', 'parts_amount': 1, 'wage': 0, 'quantity': 1,
            '_master_ref_no': '38100'},
           {'name': 'B', 'method': '取替', 'parts_amount': 1, 'wage': 0, 'quantity': 1,
            '_master_ref_no': 'ABC'}])
got = cur.execute('select PartsCode from ERParts order by RecordNo').fetchall()
chk(got == [('',), ('',)], f'4b: 4桁でない部品コードが書かれた {got}')

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
# XML はゼロ埋めしない（実機70件すべてが1〜2桁）、DB は4桁/2桁ゼロ埋め。
# 表記は違ってよいが、指している年月は一致していなければならない。
_i = lambda v: int(v) if (v or '').strip().isdigit() else None
chk(_i(tag('CarRegistedDateYear')) == _i(db10[1])
    and _i(tag('CarRegistedDateMonth')) == _i(db10[0][4:6]),
    f"10: XML({tag('CarRegistedDateYear')}/{tag('CarRegistedDateMonth')}) と DB{db10} が食い違う")

# 10b. 初度登録の書式そのものが実機と同じであること
#      実機70件: 元号コード（令和=4/平成=3/昭和=2）・年月はゼロ埋めなし・
#      合成文字列は西暦 'YYYY/MM'。
chk(tag('CarRegistedDateEra') == '4', f"10b: 元号コード {tag('CarRegistedDateEra')!r} != '4'")
chk(tag('CarRegistedDateYear') == '2', f"10b: 和暦年 {tag('CarRegistedDateYear')!r} != '2'")
chk(tag('CarRegistedDate') == '2020/01', f"10b: 初度登録 {tag('CarRegistedDate')!r} != '2020/01'")

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



# ============================================================
# 実機コグニセブンの .neo 202件（明細11,254行）から確定した形
# ------------------------------------------------------------
# 素材: Desktop\工場見積\*.neo（コグニが印刷した帳票PDFとの対）と
#       OneDrive の 〇玲央バックアップ/冠水 ほか。
# 定義: Auda7/AudaData/Const/AnDefine.ini [WorkSheet]（作業区分）
#       Auda7/AudaData/Const/AnEra.ini    [EraValue]（元号コード）
# ============================================================

# 20. 標準欄。実機の自由入力行（DisposalCode=-1）536行では
#     TimeStandard は 100% が 0、WageStandard* は 527行（98.3%）が 0。
#     PartsPriceStandard* だけが -1 のまま、という非対称な形をしている。
cur = gen([{'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 500,
            'quantity': 1, 'index_value': '1.5'}])
r20 = cur.execute('select Time, TimeStandard, WageStandardOutTax, WageStandardInTax,'
                  ' WageStandardTax, PartsPriceStandardOutTax from ERParts').fetchone()
chk(r20 == (1.5, 0, 0, 0, 0, -1), f'20: 標準欄 {r20} != (1.5, 0, 0, 0, 0, -1)')

# 21. AnNote.ini は 144バイト固定長（本体142B＋CRLF）× 行数。
#     実機202件すべてが 144 で割り切れた。
out21 = app.generate_neo_file(TPL, {}, [
    {'name': 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ', 'method': '取替', 'parts_amount': 45000, 'wage': 0,
     'quantity': 1, 'part_no': '52119-21921', '_master_ref_no': '0010'},
    {'name': 'ﾊﾞﾝﾊﾟｰ脱着', 'method': '脱着', 'parts_amount': 0, 'wage': 12000, 'quantity': 3},
], 0, {}, {}, False, False, False)[0]
note21 = neogen.unpack(out21)['AnNote.ini']
chk(len(note21) == 144 * 2, f'21: AnNote {len(note21)}B != {144 * 2}B')
chk(all(note21[i + 142:i + 144] == b'\r\n' for i in range(0, len(note21), 144)),
    '21: AnNote のレコード末尾が CRLF でない')

# 22. AnNote.ini のフィールド配置。実機レコード102件を割って確定した:
#     [0:8]行番号 [8:12]部品ｺｰﾄﾞ [12:13]枝番 [13:14]作業区分 [14:38]品名
#     [38:62]標準品名 [62:80]品番 [80:98]標準品番 [98:100]数量
#     [100:105]由来（マスタ由来='00000' / 手入力=' 0000'）[127:133]'F99999'
r0, r1 = note21[:144], note21[144:288]
chk(r0[0:8] == b'00000010', f'22: 行番号 {r0[0:8]!r}')
chk(r0[8:12] == b'0010', f'22: 部品コードが AnNote に届いていない {r0[8:12]!r}')
chk(r0[12:13] == b' ', f'22: 枝番は空欄のはず {r0[12:13]!r}')
chk(r0[13:14] == b'0', f'22: 作業区分が AnNote に届いていない {r0[13:14]!r}')
chk(r0[14:38].rstrip() == 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ'.encode('cp932'), f'22: 品名 {r0[14:38]!r}')
chk(r0[38:62].strip() == b'', f'22: 標準品名は空欄のはず {r0[38:62]!r}')
chk(r0[62:80].rstrip() == b'52119-21921', f'22: 品番が AnNote に届いていない {r0[62:80]!r}')
chk(r0[98:100] == b'01', f'22: 数量 {r0[98:100]!r}')
chk(r0[100:105] == b'00000', f'22: マスタ由来の行の由来欄 {r0[100:105]!r} != b"00000"')
chk(r0[127:133] == b'F99999', f'22: F欄 {r0[127:133]!r}')
# 2行目は照合できていない手入力行
chk(r1[8:12] == b'    ', f'22: 手入力行に部品コードが入った {r1[8:12]!r}')
chk(r1[13:14] == b'1', f'22: 脱着の作業区分 {r1[13:14]!r}')
chk(r1[98:100] == b'03', f'22: 数量3が反映されていない {r1[98:100]!r}')
chk(r1[100:105] == b' 0000', f'22: 手入力行の由来欄 {r1[100:105]!r} != b" 0000"')

# 23. 品名は [14:38] の24バイトに収まり、隣の標準品名欄へはみ出さないこと
out23 = app.generate_neo_file(TPL, {}, [
    {'name': 'ﾆﾎﾝｺﾞﾉﾄﾃﾓﾅｶﾞｲﾌﾞﾋﾝﾒｲｼｮｳﾃﾞｽ', 'method': '取替',
     'parts_amount': 1000, 'wage': 0, 'quantity': 1}], 0, {}, {}, False, False, False)[0]
r23 = neogen.unpack(out23)['AnNote.ini'][:144]
chk(r23[38:62].strip() == b'', f'23: 長い品名が標準品名欄へはみ出した {r23[38:62]!r}')



# 24. 初度登録の元号。実機の元号設定ファイル AnEra.ini [EraValue] が
#     西暦=1 令和=4 平成=3 昭和=2 と定めており、実機の .neo 70件でも
#     Era=4 が令和、Era=3 が平成だった。年・月はゼロ埋めしない。
#     合成文字列は西暦 'YYYY/MM'、車検満了日は西暦 'YYYY/MM/DD'。
#     元号を決められない日付（大正以前・不正値・月が00）は、
#     「令和0年0月」ではなく空欄にする。
def _regtags(cust):
    _nb = app.generate_neo_file(TPL, cust, [
        {'name': 'X', 'method': '取替', 'parts_amount': 100, 'wage': 0, 'quantity': 1}],
        0, {}, {}, False, False, False)[0]
    _x = [v for v in neogen.unpack(_nb).values()
          if b'AudaNeo2Data' in v[:300]][0].decode('cp932', 'replace')
    _g = lambda t: (_re.search(r'<%s>([^<]*)</%s>' % (t, t), _x) or [None, ''])[1]
    return (_g('CarRegistedDateEra'), _g('CarRegistedDateYear'),
            _g('CarRegistedDateMonth'), _g('CarRegistedDate'), _g('CarTermEraDate'))

for _ym, _want in (('202408', ('4', '6', '8', '2024/08')),
                   ('201904', ('3', '31', '4', '2019/04')),
                   ('201905', ('4', '1', '5', '2019/05')),
                   ('198801', ('2', '63', '1', '1988/01')),
                   # 元号を決められないもの。空欄になること
                   ('192601', ('', '', '', '')),
                   ('190001', ('', '', '', '')),
                   ('202400', ('', '', '', '')),
                   ('あいうえお', ('', '', '', '')),
                   ('', ('', '', '', ''))):
    _got = _regtags({'car_reg_date': _ym})[:4]
    chk(_got == _want, f'24: 初度登録 {_ym!r} → {_got}（期待 {_want}）')

# 25. 車検満了日は西暦 'YYYY/MM/DD'。日が 00 のときは空欄。
chk(_regtags({'car_reg_date': '202408', 'term_date': '20280225'})[4] == '2028/02/25',
    '25: 車検満了日が西暦 YYYY/MM/DD になっていない')
chk(_regtags({'car_reg_date': '202408', 'term_date': '20280200'})[4] == '',
    '25b: 日が00の車検満了日が空欄になっていない')



# 26. 諸経費の税額欄が負にならないこと。
#     諸経費を部品側（ショートパーツ）と工賃側（レッカー・代車）に割るとき、
#     請求書単位のまとめ丸めの端数がマイナスだと片方が負になりうる。
#     税額が負の見積書は帳票として成立しない。合計は変えずに吸収する。
import itertools as _it26
for _pa, _wg, _sp, _tw, _rc in _it26.product((0, 1, 3), (0, 1), (0, 1, 7, 13, 20000),
                                             (0, 1, 3), (0, 1, 3)):
    for _incl in (False, True):
        _nb26 = app.generate_neo_file(
            TPL, {}, [{'name': 'A', 'method': '取替', 'parts_amount': _pa,
                       'wage': _wg, 'quantity': 1}],
            _sp, {}, {'towing': _tw, 'rental_car': _rc}, _incl, False, False)[0]
        _c26 = neogen.opendb(neogen.unpack(_nb26)['AnSMB.txt']).cursor()
        (_hp, _hpt, _hw, _hwt, _mp, _mw, _pn, _sub, _tx, _tot) = _c26.execute(
            'select hy_PartsTaxTotalOutTax, hy_PartsTaxTotalTax,'
            ' hy_WageTaxTotalOutTax, hy_WageTaxTotalTax,'
            ' ms_PartsTotalOutTax, ms_WageTotalOutTax, pn_TotalOutTax,'
            ' SubTotal, tx_TotalOutTax, Total from Total').fetchone()
        _tag = f'(部品{_pa} 工賃{_wg} 短ﾊﾟ{_sp} ﾚｯｶｰ{_tw} 代車{_rc} 税込={_incl})'
        chk(_hpt >= 0 and _hwt >= 0, f'26: 諸経費の税額が負 {_hpt}/{_hwt} {_tag}')
        chk(_hp + _hw == _sp + _tw + _rc,
            f'26b: 諸経費の内訳計 {_hp}+{_hw} != {_sp + _tw + _rc} {_tag}')
        chk(_mp + _mw + _pn + _hp + _hw == _sub,
            f'26c: 課税額計が内訳と合わない {_tag}')
        chk(_sub + _tx == _tot, f'26d: 小計＋消費税が総額と合わない {_tag}')

# 27. マージモード（前案件の .neo をテンプレートにする）で、初度登録が
#     ヘッダXML と 見積本体DB で食い違わないこと。
#     元号を決められない日付を DB にだけ書いてしまうと、同じ .neo の中に
#     初度登録が2通り入る（協定見積として致命的）。
_tpl27 = app.generate_neo_file(
    TPL, {'car_reg_date': '202408'},
    [{'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1}],
    0, {}, {}, False, False, False)[0]
for _label, _cust27 in (('不正（大正以前）', {'car_reg_date': '192601'}),
                        ('未入力', {}),
                        ('月が00', {'car_reg_date': '202400'}),
                        ('有効（令和2年1月）', {'car_reg_date': '202001'})):
    _nb27 = app.generate_neo_file(
        _tpl27, _cust27,
        [{'name': 'B', 'method': '取替', 'parts_amount': 2000, 'wage': 0, 'quantity': 1}],
        0, {}, {}, False, False, True)[0]
    _f27 = neogen.unpack(_nb27)
    _x27 = [v for v in _f27.values() if b'AudaNeo2Data' in v[:300]][0].decode('cp932', 'replace')
    _g27 = lambda t: (_re.search(r'<%s>([^<]*)</%s>' % (t, t), _x27) or [None, ''])[1]
    _db27 = neogen.opendb(_f27['AnSvEm0001Ex.db']).cursor().execute(
        'select CarRegDate, CarRegEraYear from Customer').fetchone()
    _n27 = lambda v: int(v) if (v or '').strip().isdigit() else None
    chk(_n27(_g27('CarRegistedDateYear')) == _n27(_db27[1])
        and _n27(_g27('CarRegistedDateMonth')) == _n27((_db27[0] or '')[4:6]),
        f'27: マージ({_label}) XML({_g27("CarRegistedDateYear")}/'
        f'{_g27("CarRegistedDateMonth")}) と DB{_db27} が食い違う')

# 28. 元号を決められない初度登録は、見積本体DB にも書かないこと
#     （書くと「1926年1月」という車の見積になる）
_nb28 = app.generate_neo_file(
    TPL, {'car_reg_date': '192601'},
    [{'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1}],
    0, {}, {}, False, False, False)[0]
_d28 = neogen.opendb(neogen.unpack(_nb28)['AnSvEm0001Ex.db']).cursor().execute(
    'select CarRegDate from Customer').fetchone()[0]
chk(_d28 == '00000000', f'28: 元号不明の初度登録が DB に書かれた {_d28!r}')



# 29. 作業区分は「区分を直接もらったとき」と「品名から推し量ったとき」で
#     同じコードにならなければならない。区分は実機の基本定義ファイル
#     AnDefine.ini [WorkSheet] が定めるコードに揃える。
#     アプリには区分の判定が3か所（抽出プロンプト／品名からの推定／対応表）
#     あり、どれか1つだけ直すと経路によってコードが変わる。
_WANT29 = {
    '取替': 0, '交換': 0, '取換': 0,
    '脱着': 1, '取外': 1, '取付': 1, '組付': 1,
    '修理': 2, '補修': 2, '修正': 2, '穴あけ': 2, 'シーリング': 2, '磨き調整': 2,
    '脱着修理': 3, '脱着板金': 3, '脱着鈑金': 3,
    '点検': 4, '診断': 4, '調整': 4, '点検調整': 4,
    '光軸': 4, 'フィッティング': 4, 'コーディング': 4, '設定': 4, '消去': 4,
    '分解調整': 5, '分解': 5, '分解清掃': 5,
    '板金': 6, '鈑金': 6,
}
_w29 = sorted(_WANT29)
# 区分を直接もらう経路
_c29a = gen([{'name': f'P{_i:02d}', 'method': _v, 'parts_amount': 0, 'wage': 1000,
              'quantity': 1} for _i, _v in enumerate(_w29)])
_g29a = [r[0] for r in _c29a.execute(
    'select DisposalCode from ERParts where PartsName<>"" order by LineNo')]
# 区分が空で、品名から推し量る経路（工賃ありにして「部品のみ→取替」規則を避ける）
_c29b = gen([{'name': f'ﾄﾞｱ{_v}', 'method': '', 'parts_amount': 0, 'wage': 1000,
              'quantity': 1} for _v in _w29])
_g29b = [r[0] for r in _c29b.execute(
    'select DisposalCode from ERParts where PartsName<>"" order by LineNo')]
for _v, _a, _b in zip(_w29, _g29a, _g29b):
    chk(_a == _WANT29[_v], f'29: 区分「{_v}」を直接もらったとき {_a}（期待 {_WANT29[_v]}）')
    chk(_b == _WANT29[_v], f'29b: 区分「{_v}」を品名から推したとき {_b}（期待 {_WANT29[_v]}）')

# 30. 研磨・磨き・写真代・ショートパーツは区分なし（-1）のまま。
#     「磨き調整」だけは実機にある区分なので 2 になる（29 で検査済み）。
_c30 = gen([{'name': _n, 'method': '', 'parts_amount': 0, 'wage': 1000, 'quantity': 1}
            for _n in ('ﾎｲｰﾙ研磨', 'ﾎﾞﾃﾞｰ磨き', '写真代', 'ショートパーツ')])
_g30 = _c30.execute(
    'select DisposalCode, DisposalName from ERParts where PartsName<>"" order by LineNo').fetchall()
chk(all(_r[0] == -1 and not (_r[1] or '').strip() for _r in _g30),
    f'30: 研磨・磨き・写真代・ショートパーツに区分が付いた {_g30}')



# 31. コグニセブンの「既存見積」一覧が読む先頭424Bの管理領域。
#     ここを書かないと、一覧に並んでも登録番号・顧客名・車名が空欄で出る
#     （2026-09-10 に実機で確認。内包ファイルを実機のものと差し替えても
#      直らず、先頭424Bを差し替えたときだけ直った）。
import neo_header as _nh31
import datetime as _dt31
_cust31 = {'customer_name': 'テスト太郎', 'car_name': 'ﾉｱ',
           'car_reg_department': '品川', 'car_reg_division': '300',
           'car_reg_business': 'あ', 'car_reg_serial': '1234'}
_out31 = app.generate_neo_file(TPL, _cust31, [
    {'name': 'A', 'method': '取替', 'parts_amount': 45000, 'wage': 0, 'quantity': 1},
    {'name': 'B', 'method': '脱着', 'parts_amount': 0, 'wage': 12000, 'quantity': 1},
], 1000, {}, {'towing': 20000}, False, False, False)
_nb31, _p31, _w31, _g31 = _out31
_h31 = _nh31.decode(_nb31)
chk(_h31['name1'] == 'テスト太郎', f'31: 管理領域の顧客名 {_h31["name1"]!r}')
chk(_h31['car_name'] == 'ﾉｱ', f'31: 管理領域の車名 {_h31["car_name"]!r}')
chk(_h31['carno'] == ('品川', '300', 'あ', '1234'),
    f'31: 管理領域の登録番号 {_h31["carno"]}')
chk(_h31['created'][:3] == (_dt31.datetime.now(app.JST).year,
                            _dt31.datetime.now(app.JST).month,
                            _dt31.datetime.now(app.JST).day),
    f'31: 管理領域の作成日 {_h31["created"]}')
# 金額は [部品計, 工賃計, 塗装計, 諸経費計, 総額]。見積本体と一致すること。
# 諸経費計には非課税ぶんも入る（非課税のある実機12件すべてでそうだった）。
_SUM31 = ('select ms_PartsTotalOutTax, ms_WageTotalOutTax, pn_TotalOutTax,'
          ' hy_PartsTaxTotalOutTax, hy_WageTaxTotalOutTax,'
          ' hy_PartsNoTaxTotalOutTax, hy_WageNoTaxTotalOutTax, Total from Total')
_c31 = neogen.opendb(neogen.unpack(_nb31)['AnSMB.txt']).cursor()
_t31 = _c31.execute(_SUM31).fetchone()
chk([int(v) for v in _h31['totals']] ==
    [_t31[0], _t31[1], _t31[2], _t31[3] + _t31[4] + _t31[5] + _t31[6], _t31[7]],
    f'31b: 管理領域の金額 {_h31["totals"]} が見積本体 {_t31} と合わない')
chk(int(_h31['totals'][4]) == _g31, f'31c: 管理領域の総額 {_h31["totals"][4]} != {_g31}')

# 31e. 非課税があるときも、諸経費計と総額が本体と一致すること
for _exp31, _lab31 in (({'towing': 20000, 'rental_car': 15000, 'tax_exempt': 11000}, '非課税あり'),
                       ({'tax_exempt': 30000}, '非課税だけ'),
                       ({}, '費用なし')):
    _n31e, _, _, _g31e = app.generate_neo_file(TPL, _cust31, [
        {'name': 'A', 'method': '取替', 'parts_amount': 45000, 'wage': 0, 'quantity': 1}],
        1000, {}, dict(_exp31), False, False, False)
    _te = neogen.opendb(neogen.unpack(_n31e)['AnSMB.txt']).cursor().execute(_SUM31).fetchone()
    _he = _nh31.decode(_n31e)['totals']
    chk(int(_he[3]) == _te[3] + _te[4] + _te[5] + _te[6],
        f'31e: {_lab31} の諸経費計 {_he[3]} != {_te[3] + _te[4] + _te[5] + _te[6]}')
    chk(int(_he[4]) == _te[7] == _g31e,
        f'31e: {_lab31} の総額 {_he[4]} / 本体 {_te[7]} / 返り値 {_g31e}')

# 31d. 管理領域を書いても内包ファイルは壊れないこと（先頭424Bだけを触る）
_f31 = neogen.unpack(_nb31)
chk(len(_f31) == 12, f'31d: 内包ファイル数 {len(_f31)}（期待 12）')

# 32. マージモード（前案件の .neo をテンプレートにする）で、
#     今回の顧客情報が空なら前の値を残し、入っていれば上書きすること。
_prev32 = app.generate_neo_file(TPL, _cust31, [
    {'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1}],
    0, {}, {}, False, False, False)[0]
_keep32 = app.generate_neo_file(_prev32, {}, [
    {'name': 'B', 'method': '取替', 'parts_amount': 2000, 'wage': 0, 'quantity': 1}],
    0, {}, {}, False, False, True)[0]
chk(_nh31.decode(_keep32)['name1'] == 'テスト太郎',
    f'32: マージで顧客名が消えた {_nh31.decode(_keep32)["name1"]!r}')
_over32 = app.generate_neo_file(_prev32, {'customer_name': '別人二郎',
                                          'car_reg_department': '沖縄',
                                          'car_reg_division': '582',
                                          'car_reg_business': 'な',
                                          'car_reg_serial': '4313'}, [
    {'name': 'B', 'method': '取替', 'parts_amount': 2000, 'wage': 0, 'quantity': 1}],
    0, {}, {}, False, False, True)[0]
_d32 = _nh31.decode(_over32)
chk(_d32['name1'] == '別人二郎' and _d32['carno'] == ('沖縄', '582', 'な', '4313'),
    f'32b: マージで新しい顧客情報が入っていない {_d32["name1"]!r} {_d32["carno"]}')

# 32c. 非マージなら前案件の顧客名を残さないこと（個人情報が混ざる）
_new32 = app.generate_neo_file(_prev32, {}, [
    {'name': 'B', 'method': '取替', 'parts_amount': 2000, 'wage': 0, 'quantity': 1}],
    0, {}, {}, False, False, False)[0]
chk(_nh31.decode(_new32)['name1'] == '',
    f'32c: 非マージなのに前案件の顧客名が残った {_nh31.decode(_new32)["name1"]!r}')



# 33. 登録番号を1欄だけ直したとき、管理領域・見積本体DB・ヘッダXML の3つが
#     同じ登録番号を指していること。
#     本体は欄ごとに「空ならテンプレートの値を残す」ので、管理領域だけ
#     4欄まとめて入れ替えると、1欄直しただけで一覧と中身が食い違う。
_base33 = {'customer_name': 'テスト太郎', 'car_name': 'ﾉｱ', 'car_reg_department': '品川',
           'car_reg_division': '300', 'car_reg_business': 'あ', 'car_reg_serial': '1234'}
_it33 = [{'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1}]
_prev33 = app.generate_neo_file(TPL, _base33, _it33, 0, {}, {}, False, False, False)[0]

def _carno3(nb):
    _f = neogen.unpack(nb)
    _db = neogen.opendb(_f['AnSvEm0001Ex.db']).cursor().execute(
        'select CarRegNoDepartment, CarRegNoDivision, CarRegNoBusiness,'
        ' CarRegNoSerial from Customer').fetchone()
    _x = [v for v in _f.values() if b'AudaNeo2Data' in v[:300]][0].decode('cp932', 'replace')
    _g = lambda t: (_re.search(r'<%s>([^<]*)</%s>' % (t, t), _x) or [None, ''])[1]
    return (tuple(_nh31.decode(nb)['carno']), tuple(_db),
            (_g('CarNoArea'), _g('CarNoClass'), _g('CarNoKana'), _g('CarNoSeries')))

for _lab33, _c33, _want33 in (
        ('一連番号だけ', {'car_reg_serial': '9999'}, ('品川', '300', 'あ', '9999')),
        ('陸運支局だけ', {'car_reg_department': '沖縄'}, ('沖縄', '300', 'あ', '1234')),
        ('かなだけ', {'car_reg_business': 'な'}, ('品川', '300', 'な', '1234')),
        ('何も直さない', {}, ('品川', '300', 'あ', '1234')),
        ('4欄すべて', {'car_reg_department': '沖縄', 'car_reg_division': '582',
                       'car_reg_business': 'な', 'car_reg_serial': '4313'},
         ('沖縄', '582', 'な', '4313'))):
    _nb33 = app.generate_neo_file(_prev33, dict(_c33), _it33, 0, {}, {}, False, False, True)[0]
    _h33, _d33, _x33 = _carno3(_nb33)
    chk(_h33 == _d33 == _x33 == _want33,
        f'33: マージで{_lab33}を直したとき 管理領域={_h33} DB={_d33} XML={_x33}'
        f'（期待 {_want33}）')



# 34. 管理領域が空のまま本体に顧客が入っている「古いアプリ製の .neo」を
#     テンプレートにしても、一覧のサマリが本体と一致すること。
#     管理領域はテンプレート側を頼りにせず、生成し終わった見積本体から作る。
_good34 = app.generate_neo_file(TPL, _cust31, [
    {'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1}],
    0, {}, {}, False, False, False)[0]
# 管理領域だけ出荷テンプレート（空）に戻したもの＝直す前のアプリが作っていた形
_legacy34 = TPL[:424] + _good34[424:]
chk(_nh31.decode(_legacy34)['name1'] == '',
    '34: 検査の前提が崩れている（古い形の管理領域が空でない）')


def _body34(nb):
    return neogen.opendb(neogen.unpack(nb)['AnSvEm0001Ex.db']).cursor().execute(
        'select Name1, CarRegNoDepartment, CarRegNoDivision,'
        ' CarRegNoBusiness, CarRegNoSerial from Customer').fetchone()


for _lab34, _c34, _merge34 in (('何も直さない', {}, True),
                               ('一連番号だけ直す', {'car_reg_serial': '9999'}, True),
                               ('顧客名だけ直す', {'customer_name': '別人二郎'}, True),
                               ('非マージ', {}, False)):
    _nb34 = app.generate_neo_file(_legacy34, dict(_c34), [
        {'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1}],
        0, {}, {}, False, False, _merge34)[0]
    _h34 = _nh31.decode(_nb34)
    _b34 = _body34(_nb34)
    chk(_h34['name1'] == (_b34[0] or '')
        and tuple(_h34['carno']) == tuple(x or '' for x in _b34[1:5]),
        f'34: 古い形をテンプレートに（{_lab34}）管理領域={_h34["name1"]!r}'
        f'{_h34["carno"]} 本体={_b34}')



# 35. 品名から区分を推し量るときも、**原本に書かれていた区分語をそのまま**
#     区分名にすること。DisposalName は帳票の「修理方法」に印字されるので、
#     「脱着板金」と書かれた行を「脱着修理」に書き換えると原本と違う紙になる
#     （区分コードはどちらも 3 で同じ）。
_CASE35 = [
    ('ﾄﾞｱ脱着板金', '脱着板金', 3),
    ('ﾄﾞｱ脱着鈑金', '脱着鈑金', 3),
    ('ﾄﾞｱ脱着修理', '脱着修理', 3),
    ('ｾﾝｻｰ点検清掃', '点検清掃', 4),
    ('ｾﾝｻｰ点検調整', '点検調整', 4),
    ('ｴﾝｼﾞﾝ分解清掃', '分解清掃', 5),
    ('ｴﾝｼﾞﾝ分解調整', '分解調整', 5),
    ('ﾎｲｰﾙ磨き調整', '磨き調整', 2),
    ('ﾊﾟﾈﾙ板金', '板金', 6),
    ('ﾊﾟﾈﾙ鈑金', '鈑金', 6),
    ('ﾎｲｰﾙ研磨', '', -1),
    ('写真代', '', -1),
    ('ﾊﾞﾝﾊﾟｰ取替', '取替', 0),
    ('ﾗｲﾄ光軸調整', '調整', 4),
]
_c35 = gen([{'name': _n, 'method': '', 'parts_amount': 0, 'wage': 1000, 'quantity': 1}
            for _n, _, _ in _CASE35])
_g35 = _c35.execute(
    'select PartsName, DisposalName, DisposalCode from ERParts'
    ' where PartsName<>"" order by LineNo').fetchall()
for (_n35, _wn35, _wc35), _r35 in zip(_CASE35, _g35):
    chk((_r35[1] or '') == _wn35 and _r35[2] == _wc35,
        f'35: 「{_n35}」→ 区分名 {_r35[1]!r} コード {_r35[2]}'
        f'（期待 {_wn35!r} / {_wc35}）')

# 35b. 部品金額だけの行は取替。ただし「研磨」等が入っていたら区分なしのまま。
_c35b = gen([{'name': 'ﾊﾞﾝﾊﾟｰ', 'method': '', 'parts_amount': 1000, 'wage': 0, 'quantity': 1},
             {'name': 'ﾎｲｰﾙ研磨', 'method': '', 'parts_amount': 1000, 'wage': 0, 'quantity': 1},
             {'name': 'ｼｮｰﾄﾊﾟｰﾂ', 'method': '', 'parts_amount': 1000, 'wage': 0, 'quantity': 1}])
_g35b = [r[0] for r in _c35b.execute(
    'select DisposalCode from ERParts where PartsName<>"" order by LineNo')]
chk(_g35b == [0, -1, -1], f'35b: 部品だけの行の区分 {_g35b}（期待 [0, -1, -1]）')

# 35c. 見積書の品名は半角カナで書かれることが多い。全角で書いたキーワードに
#      当たらず区分なしの行になっていた（「ｼｮｰﾄﾊﾟｰﾂ」「ﾍﾟｲﾝﾄ」「ﾌｨｯﾃｨﾝｸﾞ」）。
_CASE35c = [('ｼｮｰﾄﾊﾟｰﾂ', '', -1), ('ﾍﾟｲﾝﾄ', '塗装', 2),
            ('ﾜｯｸｽ', '塗装', 2), ('ﾌｨｯﾃｨﾝｸﾞ', '調整', 4),
            ('ｺｰﾃﾞｨﾝｸﾞ', '調整', 4)]
_c35c = gen([{'name': _n, 'method': '', 'parts_amount': 0, 'wage': 1000, 'quantity': 1}
             for _n, _, _ in _CASE35c])
_g35c = _c35c.execute('select PartsName, DisposalName, DisposalCode from ERParts'
                      ' where PartsName<>"" order by LineNo').fetchall()
for (_n35c, _wn35c, _wc35c), _r35c in zip(_CASE35c, _g35c):
    chk((_r35c[1] or '') == _wn35c and _r35c[2] == _wc35c,
        f'35c: 半角カナ「{_n35c}」→ 区分名 {_r35c[1]!r} コード {_r35c[2]}'
        f'（期待 {_wn35c!r} / {_wc35c}）')


print('REG_NEOACC:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL: print('  -', f)
sys.exit(1 if FAIL else 0)
