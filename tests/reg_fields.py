# -*- coding: utf-8 -*-
""".neo の欄の値が、実機（コグニセブン）が書くものと同じかの回帰テスト。

pdf-to-neo スキル（claude_neo_pipeline）とアプリで、同じ欄に別の値を
書いている箇所があった。実機が作った .neo を数えて白黒つけた結果を固定する。

実測（2026-09-12、実機 150 件 6,024 行）:

  ERParts.OrderFlag と AnSMB の [100] バイトは **同じ欄**。
  120 件 4,875 行で1行も食い違わなかった。値の分布は
      '0'（マスタ由来）87.2% ／ ' '（手入力）9.2% ／ '9' 2.3%
  アプリは ERParts に '9' を固定で書き、AnSMB には '0'/' ' を書いていた。
  **同じ .neo の中で矛盾した値**を持っていたことになる。

  部品代の無い行（工賃だけの行）の ERParts.PartsCount は -1 が 85.1%。
  アプリは常に数量を書いていた。なお AnSMB 側の数量欄は実機でも '01' で、
  PartsCount=-1 の行の 100% が '01' だった（そちらは変えない）。

**検証用のダミー値だけを使う。実在の人物の情報は扱わない。**
"""
import os
import sqlite3
import sys
import tempfile

R = os.environ.get('XROOT',
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, R)
os.chdir(R)
sys.stdout.reconfigure(encoding='utf-8')

import addata_locator  # noqa: E402
addata_locator.find_addata = lambda *a, **kw: None
import app  # noqa: E402

FAIL = []


def chk(cond, msg):
    if not cond:
        FAIL.append(msg)


def dec(v):
    if isinstance(v, bytes):
        return v.decode('cp932', 'replace')
    return '' if v is None else str(v)


def build(items, intax=False):
    tpl = open(os.path.join(R, 'template_toyota.neo'), 'rb').read()
    nb = app.generate_neo_file(tpl, {'customer_name': 'ｹﾝｼｮｳ'},
                               [dict(i) for i in items], 0, {}, {},
                               intax, False, False)[0]
    ck = app.find_real_cks(nb)
    fs = app.extract_files(app.decompress_neo(nb, ck),
                           app.parse_entries(nb, ck[0])[1])
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    try:
        tf.write(fs['AnSMB.txt'])
        tf.close()
        con = sqlite3.connect(tf.name)
        con.text_factory = bytes
        try:
            rows = list(con.execute(
                'select LineNo, PartsName, PartsCode, OrderFlag, PartsCount,'
                ' PartsPriceOutTax, WageOutTax from ERParts'
                ' where PartsName is not null and PartsName<>""'
                ' order by LineNo'))
        finally:
            con.close()
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass
    note = fs.get('AnNote.ini', b'')       # 実体は 144B 固定長の明細
    by = {}
    for i in range(0, len(note) - 143, 144):
        rec = note[i:i + 144]
        try:
            ln = int(rec[0:8].decode('ascii').strip() or 0)
        except ValueError:
            continue
        by[ln] = (rec[100:101].decode('cp932', 'replace'),
                  rec[98:100].decode('cp932', 'replace'))
    return rows, by


ITEMS = [
    # マスタ由来（部品コードあり）
    {'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600, 'wage': 0,
     'quantity': 1, '_master_ref_no': '1234'},
    # 手入力・工賃だけの行
    {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0, 'wage': 9800,
     'quantity': 1},
    # 手入力・数量10
    {'name': 'ｸﾘﾂﾌﾟ', 'method': '取替', 'parts_amount': 1550, 'wage': 0,
     'quantity': 10},
]

rows, by = build(ITEMS)
chk(len(rows) == 3, '0: 行数 %d（3のはず）' % len(rows))

for r in rows:
    ln = app.safe_int(r[0])
    name = dec(r[1])
    code = dec(r[2]).strip()
    of = dec(r[3])
    pc = app.safe_int(r[4])
    pp = app.safe_int(r[5])
    b100, qcol = by.get(ln, ('?', '?'))

    # ── 1. OrderFlag と AnSMB[100] は同じ欄。値がそろっていること ──
    chk(of == b100,
        '1: %s の OrderFlag が %r、AnSMB[100] が %r。'
        '実機ではこの2つは同じ欄で、1行も食い違わない'
        '（同じ .neo の中で矛盾した値を持っている）' % (name, of, b100))
    chk(of != '9',
        '1b: %s の OrderFlag が %r。実機 6,024 行のうち 2.3%% しか無い'
        '少数派で、アプリが固定で書いてよい値ではない' % (name, of))

    # ── 2. 由来の規則: 部品コードがあれば '0'、無ければ ' ' ──────
    want = '0' if code else ' '
    chk(of == want,
        '2: %s（コード %r）の由来が %r（%r のはず）' % (name, code, of, want))

    # ── 3. 部品代の無い行の PartsCount は -1（空欄） ──────────────
    if pp <= 0:
        chk(pc == -1,
            '3: %s は部品代が無い行なのに PartsCount が %d。'
            '実機は -1（空欄）が 85%%。部品が無いのに「1個」と読める'
            % (name, pc))
    else:
        chk(pc >= 1,
            '3b: %s は部品代のある行なのに PartsCount が %d' % (name, pc))

    # ── 4. AnSMB の数量欄は実機どおり数量のまま ─────────────────
    #     PartsCount=-1 の行でも、実機は数量欄 '01' を書いていた（100%）。
    if pp <= 0:
        chk(qcol == '01',
            '4: %s の AnSMB 数量欄が %r（実機は -1 の行でも "01"）'
            % (name, qcol))

# ── 5. 税込表記でも同じであること ────────────────────────────
rows2, by2 = build([
    {'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 42460, 'wage': 0,
     'quantity': 1, '_master_ref_no': '5678'},
    {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0, 'wage': 10780,
     'quantity': 1},
], intax=True)
for r in rows2:
    ln = app.safe_int(r[0])
    of = dec(r[3])
    b100 = by2.get(ln, ('?', '?'))[0]
    chk(of == b100,
        '5: 税込でも OrderFlag %r と AnSMB[100] %r が食い違う' % (of, b100))
    chk(of != '9', '5b: 税込で OrderFlag に %r を書いている' % of)

# ── 6. 固定値に戻されないようソースでも縛る ─────────────────────
import inspect  # noqa: E402
_src = inspect.getsource(app.generate_neo_file)
chk("'9', '', 0, 0," not in _src,
    "6: ERParts の OrderFlag に '9' を固定で書く記述が戻っている")

# ── 7. 品名だけで部品代を消さないこと ─────────────────────────
# ADDATA の正式な部品名 49,432 語のうち 182 語が「取付/組付/板金/塗装/
# 修理/研磨」を含む（Rﾊﾞﾝﾊﾟ(塗装済) / ｸﾛｽﾒﾝﾊﾞ(修理) など）。
# 「Rﾊﾞﾝﾊﾟ(塗装済)」は取替の定番部品で数万円。品名で判断していたため、
# 区分に「取替」以外の語（日産系の「部品」など）が入ると黙って消えていた。
_KEEP = [
    ('Rﾊﾞﾝﾊﾟ(塗装済)', '部品', 38600),
    ('Fﾊﾞﾝﾊﾟ(未塗装)', '', 42000),
    ('ｸﾛｽﾒﾝﾊﾞ(修理)', '取替', 15000),
    ('ﾊﾞﾝﾊﾟ取付ｸﾘﾂﾌﾟ', '部品', 1550),
    ('Rｼｰﾄ(脱着･修理)', '部品', 88000),
]
_items = [{'name': n, 'method': m, 'parts_amount': p, 'wage': 0,
           'quantity': 1} for n, m, p in _KEEP]
_out, _notes = app.validate_and_correct_items(_items)
for (n, m, p), o in zip(_KEEP, _out):
    chk(o['parts_amount'] == p,
        '7: 「%s」（区分 %s）の部品代 %s円 が %s円 にされた。'
        'ADDATA の正式な部品名に「塗装」「修理」等が含まれるのは普通で、'
        '品名で作業か部品かを決めてはいけない'
        % (n, m or '空欄', format(p, ','), format(o['parts_amount'], ',')))
chk(not _notes, '7b: 何も動かしていないのに「動かした」と記録している')

# 作業区分がはっきり作業の行は、これまでどおり動かす。ただし**黙ってやらない**
_work = [{'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 5000,
          'wage': 9800, 'quantity': 1},
         {'name': 'ﾊﾞﾝﾊﾟ板金', 'method': '板金', 'parts_amount': 12000,
          'wage': 0, 'quantity': 1}]
_out2, _notes2 = app.validate_and_correct_items(_work)
chk(_out2[0]['parts_amount'] == 0, '7c: 脱着の行の部品代が残っている')
chk(_out2[1]['parts_amount'] == 0 and _out2[1]['wage'] == 12000,
    '7d: 板金の行の金額が工賃へ移っていない')
chk(len(_notes2) == 2,
    '7e: 原本の金額を動かしたのに知らせていない（%d件）' % len(_notes2))
# 画面に出る道があること
with open(os.path.join(R, 'app.py'), encoding='utf-8') as _f:
    _appsrc = _f.read()
chk("_amount_changes" in _appsrc and "原本の金額を動かしました" in _appsrc,
    '7f: 金額を動かしたことが画面に出ない')

# ── 8. 部品代の無い行の数量 ────────────────────────────────
# 実機 150 件では -1 が 90.3% ／ 1 が 9.7% ／ 2以上は 1 行も無い。
# 数量1なら -1（空欄）、2以上はそのまま（画面・AnSMB と食い違わせない）。
_rows, _by = build([
    {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0, 'wage': 9800,
     'quantity': 1},
    {'name': 'ｸﾘﾂﾌﾟ脱着', 'method': '脱着', 'parts_amount': 0, 'wage': 3000,
     'quantity': 3},
])
_pc = [app.safe_int(r[4]) for r in _rows]
chk(_pc[0] == -1,
    '8a: 部品代の無い行（数量1）の PartsCount が %s（実機は -1）' % _pc[0])
chk(_pc[1] == 3,
    '8b: 部品代の無い行（数量3）の PartsCount が %s。消すと ERParts=-1 /'
    ' AnSMB=03 / 画面=3 で数量が3通りになる' % _pc[1])

# ── 9. 部品計・工賃計の税額欄は行ごとの税の合計 ────────────────
# 仕様書 §6・§4 がそう書いており、実機 200 件でも部品計は行ごとの合計と
# 100% 一致（合計×10% の一括丸めは 82% しか合わない）。
_tpl = open(os.path.join(R, 'template_toyota.neo'), 'rb').read()
_nb = app.generate_neo_file(
    _tpl, {'customer_name': 'ｹﾝｼｮｳ'},
    [{'name': 'A', 'method': '取替', 'parts_amount': 245, 'wage': 0,
      'quantity': 1},
     {'name': 'B', 'method': '取替', 'parts_amount': 195, 'wage': 0,
      'quantity': 1},
     {'name': 'C', 'method': '取替', 'parts_amount': 295, 'wage': 0,
      'quantity': 1}], 0, {}, {}, False, False, False)[0]
_ck = app.find_real_cks(_nb)
_fs = app.extract_files(app.decompress_neo(_nb, _ck),
                        app.parse_entries(_nb, _ck[0])[1])
_tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
try:
    _tf.write(_fs['AnSMB.txt'])
    _tf.close()
    _con = sqlite3.connect(_tf.name)
    try:
        _rt = sum(app.safe_int(x[0]) for x in _con.execute(
            'select PartsPriceTax from ERParts where PartsName is not null'
            ' and PartsName<>""') if app.safe_int(x[0]) > 0)
        _mt, _tx, _sub, _gt = _con.execute(
            'select ms_PartsTotalTax, tx_TotalOutTax, SubTotal, Total'
            ' from Total').fetchone()
    finally:
        _con.close()
finally:
    try:
        os.unlink(_tf.name)
    except OSError:
        pass
chk(app.safe_int(_mt) == _rt == 75,
    '9a: 部品計の税額欄が %s（行ごとの合計 %s／25+20+30=75 のはず）'
    % (_mt, _rt))
chk(app.safe_int(_tx) == 74,
    '9b: 総額の税が %s（課税額計 735 の一括丸め 74 のはず）' % _tx)
chk(app.safe_int(_gt) == 809, '9c: 合計が %s（809 のはず）' % _gt)

print('REG_FIELDS:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL:
    print('  -', f)
sys.exit(1 if FAIL else 0)
