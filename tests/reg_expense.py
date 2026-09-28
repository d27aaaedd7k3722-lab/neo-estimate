# -*- coding: utf-8 -*-
"""諸経費（レッカー代・代車費用・非課税費用）が .neo に正しく届き、
内訳と総額が食い違わないことを確かめる回帰テスト。"""
import sys, os, random
S = os.path.dirname(os.path.abspath(__file__))
ROOT=os.environ.get('XROOT',os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, S); sys.path.insert(0, ROOT)
os.chdir(ROOT)
import app, neogen, pdf_to_neo_pipeline as P

TPL = open('template_toyota.neo', 'rb').read()
FAIL = []
def chk(c, m):
    if not c: FAIL.append(m)

def totals(neo):
    cur = neogen.opendb(neogen.unpack(neo)['AnSMB.txt']).cursor()
    return cur, cur.execute(
        'select ms_PartsTotalInTax, ms_WageTotalInTax, hy_WageTaxTotalInTax,'
        ' ms_PartsTotalTax, ms_WageTotalTax, hy_WageTaxTotalTax,'
        ' tx_TotalOutTax, hy_WageNoTaxTotalInTax, SubTotal, Total,'
        # レッカー代は工賃側ではなくレッカー専用欄に入る（2026-09-28 コグニ実機。§13-13）。
        # 内訳の和を見る式にこの欄を足さないと、レッカー代のぶん必ず不一致になる
        ' hy_Wrecker1InTax, hy_Wrecker1Tax, hy_Wrecker1OutTax,'
        ' hy_WageTaxTotalOutTax, hy_PartsTaxTotalOutTax,'
        ' hy_PartsTaxTotalInTax, hy_PartsTaxTotalTax from Total').fetchone()

# 1. 内訳の税込欄の合計と Total、税額欄の合計と tx_Total が一致すること
#    （まとめて1回で丸めていたため、同じ .neo の中で1円食い違っていた）
random.seed(20260909)
for _ in range(120):
    p = random.randint(0, 300000); w = random.randint(0, 200000)
    sp = random.choice([0, 0, 517, 3000, 20000])   # ショートパーツも振る（3 欄に割る道を通す）
    exp = {'towing': random.choice([0, 12345, 13636, 20000, 7777]),
           'rental_car': random.choice([0, 15000, 9999]),
           'tax_exempt': random.choice([0, 11000, 3333])}
    for ti in (False, True):
        items = [{'name': 'A', 'method': '取替', 'parts_amount': p, 'wage': 0, 'quantity': 1},
                 {'name': 'B', 'method': '脱着', 'parts_amount': 0, 'wage': w, 'quantity': 1}]
        neo = app.generate_neo_file(TPL, {}, items, sp, {}, exp, ti, False, False)[0]
        _c, t = totals(neo)
        chk(t[0] + t[1] + t[2] + t[7] + t[10] + t[15] == t[9],
            f'1: 内訳(税込)={t[0]+t[1]+t[2]+t[7]+t[10]+t[15]:,} と Total={t[9]:,} が不一致 '
            f'(p={p} w={w} 短ﾊﾟ={sp} {exp} 税込表記={ti})')
        chk(t[3] + t[4] + t[5] + t[11] + t[16] == t[6],
            f'1b: 税額内訳={t[3]+t[4]+t[5]+t[11]+t[16]:,} と tx_Total={t[6]:,} が不一致 '
            f'(p={p} w={w} 短ﾊﾟ={sp} {exp} 税込表記={ti})')
        # レッカー代の置き場。工賃側（代車ほか）に混ぜると、一覧から出した帳票の
        # 小計（工賃列）がレッカー代込みで印字され、その下の「レッカー代１」と
        # 二重に見える（総額は合う）。2026-09-28 コグニ実機で確かめた置き方。
        chk(t[12] == exp['towing'],
            f'1d: レッカー専用欄が {t[12]:,}（期待 {exp["towing"]:,}） '
            f'(p={p} w={w} 短ﾊﾟ={sp} {exp} 税込表記={ti})')
        chk(t[13] == exp['rental_car'],
            f'1e: 工賃側の費用欄が {t[13]:,}（期待 代車だけの {exp["rental_car"]:,}） '
            f'(p={p} w={w} 短ﾊﾟ={sp} {exp} 税込表記={ti})')
        # レッカー専用欄の税は費用行の税と同じでなければならない（実機がそう作る）。
        # 端数のやり場をレッカー欄に寄せると、同じレッカー代の税が行と欄で 1 円違う .neo になる
        _row_tow = _c.execute('select WageTax from Expense where LineNo=5').fetchone()[0]
        # この道はショートパーツ 0 なので、代車があるときだけ「ほかの置き場」がある。
        # レッカーだけのときは端数を置く欄がレッカー欄しかない（3 欄の和＝諸経費の税は崩せない）
        chk(t[11] == _row_tow or not (exp['rental_car'] or sp),
            f'1g: レッカー専用欄の税 {t[11]} と費用行の税 {_row_tow} が違う '
            f'（ほかに費用の欄があるのにレッカー欄で端数を吸っている） '
            f'(p={p} w={w} 短ﾊﾟ={sp} {exp} 税込表記={ti})')
        # どの欄の税も「その欄の金額の 10%」から大きく離れない。離れるのは請求書単位の
        # まとめ丸めの端数（数円）だけのはず。ここが緩いと、レッカー代の税を工賃側に
        # 足したままでも和さえ合えば通ってしまう
        for _lbl_c, _amt_c, _tax_c in (('部品側', t[14], t[16]),
                                       ('レッカー', t[12], t[11]),
                                       ('工賃側', t[13], t[5])):
            chk(abs(_tax_c - app._round_tax10(_amt_c, '四捨五入')) <= 5,
                f'1i: {_lbl_c}の費用の税 {_tax_c} が金額 {_amt_c} の 10% から離れている '
                f'(p={p} w={w} 短ﾊﾟ={sp} {exp} 税込表記={ti})')
        chk((t[11] > 0) == (exp['towing'] > 0),
            f'1f: レッカー代 {exp["towing"]:,} に対して専用欄の税が {t[11]:,} '
            f'（金額が無いのに税だけある欄・税の無い欄は帳票にできない） '
            f'(p={p} w={w} 短ﾊﾟ={sp} {exp} 税込表記={ti})')
        # 内部の整合だけ見ていると、「内訳も総額もそろって原本から1円ずれる」
        # という壊れ方を素通りする。原本の総額そのものと突き合わせる。
        _exp_out = exp['towing'] + exp['rental_car'] + sp
        if ti:
            # 税込表記: 明細ぶんは**原本に書かれた税込額そのもの**。
            # 以前はここで `best_intax_for`（税抜＋消費税で表せる額に寄せた値）を
            # 期待値にしていた。つまり「11件に1件は原本から1円ずれてよい」と
            # いう作りを、テスト側が追認していた。協定見積として保険会社に
            # 出すファイルなので、1円でも違えば使えない。
            # いまは明細ぶんの税額を「原本の税込 − 逆算した税抜」で書くので、
            # 表せない額でも総額は原本と一致する（税抜額は従来どおり自然な
            # 逆算値のまま。変わるのは税額の1円だけ）。
            _want = (p + w
                     + _exp_out + app.jpy_round(_exp_out * 0.10)
                     + exp['tax_exempt'])
        else:
            # 税抜表記: 消費税は請求書単位で1回だけ丸める（インボイスの原則で、
            # 見積書に印字された税込総額の作り方でもある）。
            _sub = p + w + _exp_out
            _want = _sub + app.jpy_round(_sub * 0.10) + exp['tax_exempt']
        # 1円の許容を置くと、まさに検出したい ±1円のずれを見逃す。完全一致で見る。
        chk(t[9] == _want,
            f'1c: 原本の税込総額 {_want:,} と .neo の Total {t[9]:,} が'
            f'{t[9]-_want:+,}円ずれた (p={p} w={w} {exp} 税込表記={ti})')

# 1h. ショートパーツとレッカーだけ（代車なし）で、行ごとの税の和が費用計の税を 1 円超える形。
#     端数をレッカー欄に先に寄せると、同じレッカー代の税が費用行 11・Total 10 の .neo になる
#     （Codex レビュー 4 周目）。端数は金額の大きい欄（ここではショートパーツ）が持つ
# (105, 517) はレッカーがいちばん大きい欄になる形。端数をここから引くと行と欄が 1 円ずれる
for _sp5, _tw5 in ((517, 105), (105, 517), (5, 5), (1234, 567), (7, 1234)):
    _n5 = app.generate_neo_file(TPL, {}, [{'name': 'A', 'method': '取替', 'parts_amount': 1000, 'wage': 0, 'quantity': 1}],
                                _sp5, {}, {'towing': _tw5, 'rental_car': 0, 'tax_exempt': 0}, False, False, False)[0]
    _c5 = neogen.opendb(neogen.unpack(_n5)['AnSMB.txt']).cursor()
    _row5 = _c5.execute('select WageTax from Expense where LineNo=5').fetchone()[0]
    _t5 = _c5.execute('select hy_Wrecker1Tax, hy_PartsTaxTotalTax, hy_WageTaxTotalTax,'
                      ' tx_TotalOutTax, hy_Wrecker1OutTax from Total').fetchone()
    chk(_t5[0] == _row5,
        f'1h: 代車なしでレッカー専用欄の税 {_t5[0]} と費用行の税 {_row5} が違う'
        f'（ショートパーツ {_sp5} ／ レッカー {_tw5}）')
    chk(_t5[4] == _tw5, f'1h: 代車なしでレッカー専用欄の額 {_t5[4]}（期待 {_tw5}）')

# 2. 「PDFからNEOを生成」経路でも費用が .neo に入ること
items = [{'name': 'フロントバンパー', 'method': '取替', 'parts_amount': 45000, 'wage': 0, 'quantity': 1},
         {'name': 'バンパー脱着', 'method': '脱着', 'parts_amount': 0, 'wage': 12000, 'quantity': 1}]
exp = {'towing': 20000, 'rental_car': 15000, 'tax_exempt': 11000}
neo = P._call_generate_neo(TPL, {}, items, is_beta_mode=True, expenses=exp)
cur, t = totals(neo)
rows = [r for r in cur.execute('select LineNo, WageOutTax from Expense') if r[1]]
# レッカー=LineNo5「レッカー代１」は固定費目名。
# 代車・非課税は固定費目に無いので LineNo9・10 の自由行に費目名ごと入れる（実機と同じ。非課税を LineNo8
# 「その他控除」に入れると帳票に「その他控除」と印字される。バグハント 3 回目 L10）。
chk(sorted(rows) == [(5, 20000), (9, 15000), (10, 11000)],
    f'2: PDF経路の Expense が {rows}（期待 LineNo5=20000/9=15000/10=11000）')
_nm = dict(cur.execute('select LineNo, Name from Expense').fetchall())
chk((_nm.get(9) or '').strip() == '代車費用',
    f'2c: 代車の費目名が {_nm.get(9)!r}（期待 代車費用）')
chk((_nm.get(10) or '').strip() == '非課税費用' and
    cur.execute('select OutTaxFlag from Expense where LineNo=10').fetchone()[0] == 1,
    f'2d: 非課税の費目名・非課税の旗（LineNo 10）: {_nm.get(10)!r}')
chk(not cur.execute('select WageEnabled from Expense where LineNo=8').fetchone()[0],
    '2e: 固定費目「その他控除」（LineNo 8）に金額を入れている')
chk(t[9] == 112200, f'2b: PDF経路の Total={t[9]:,}（期待 112,200）')

# ── 2026-09-21: コグニセブン**実機**で確かめた費用行の消費税 ─────────────────────
# トヨタ アイシス 5ドアワゴン ZNM10 L 1800 の新規見積に「レッカー代1」= 105 円（部品代の列）を入れ、
# 消費税設定の計算単位だけを変えて 2 本保存し、NEO の中身を読んだ実測値:
#   四捨五入(tx_ArrangeFlag=1): Expense.PartsPriceTax=11 / PartsPriceInTax=116 / Total.hy_Wrecker1Tax=11
#   切り捨て(tx_ArrangeFlag=2): Expense.PartsPriceTax=10 / PartsPriceInTax=115 / Total.hy_Wrecker1Tax=10
# → **費用行の税も請求書単位と同じ端数処理に従う**。以前アプリは常に四捨五入していて、
#   切り捨て・切り上げのテンプレートでコグニと 1 円ずれていた（Codex 深掘りの指摘を実機で裏取り）
_COGNI_REAL = [('四捨五入', 105, 11, 116), ('切り捨て', 105, 10, 115)]
for _mode, _amt, _want_tax, _want_in in _COGNI_REAL:
    _got = app._round_tax10(_amt, _mode)
    chk(_got == _want_tax and _amt + _got == _want_in,
        f'実機一致: {_mode} の {_amt} 円 → 税 {_got}（コグニ実機は {_want_tax}・税込 {_want_in}）')
# ── 諸経費の税を 3 欄に割る道（app._split_expense_tax）を総当たりで ─────────────────
# 和・非負・「金額ゼロの欄に税だけ」を破らないこと。負の額を欄のあいだで受け渡す書き方だと
# 同じ額が行き来して負が残った（Codex レビュー 3 周目。[(5,1),(5,1),(5,-2)] が実例）
import itertools as _it_sx
_SX_FAIL = 0
for _a in _it_sx.product((0, 1, 5, 20000), repeat=3):
    for _t in _it_sx.product((-2, -1, 0, 1, 2), repeat=3):
        for _tot in (-1, 0, 1, 2, 3, 2000):
          for _pin in (None, 1):   # 本番は pinned=1（レッカー欄）で呼ぶ
            _out = app._split_expense_tax(list(zip(_a, _t)), _tot, pinned=_pin)
            _bad = (sum(_out) != _tot
                    or any(_o < 0 for _o in _out if _tot >= 0)
                    # 金額が 3 欄とも 0 なら税の置き場が無い（実際には諸経費 0 なので税も 0）
                    or (any(_a) and any(_o and not _amt for _amt, _o in zip(_a, _out)) and _tot >= 0))
            if _bad:
                _SX_FAIL += 1
                if _SX_FAIL <= 3:
                    chk(False, f'税の割り方: 金額{_a} 自然な税{_t} 合計{_tot} pinned={_pin} → {_out}')
chk(_SX_FAIL == 0, f'税の割り方が破れた組み合わせ {_SX_FAIL} 件')
# Codex が挙げた形をそのまま
chk(app._split_expense_tax([(5, 1), (5, 1), (5, -2)], 1) == [0, 1, 0],
    f'税の割り方（Codex 3 周目の形）: {app._split_expense_tax([(5, 1), (5, 1), (5, -2)], 1)}')
# レッカー欄（pinned=1）は、ほかに引ける欄があるかぎり動かさない（Codex 5 周目の形）
chk(app._split_expense_tax([(105, 11), (517, 52), (0, -1)], 62, pinned=1) == [10, 52, 0],
    f'税の割り方（レッカー欄を動かさない）: {app._split_expense_tax([(105, 11), (517, 52), (0, -1)], 62, pinned=1)}')

# ── 2026-09-28: コグニセブン**実機**で確かめた費用の置き場（引き継ぎ書 §13-13）────────
# アプリが作った .neo をコグニセブンで開いて上書き保存させ、Total を読み直した実測値。
# コグニは開いた時点で Total を作り直すので、保存後の値が「コグニの正解」。
#   ・レッカー代（費用 LineNo=5）→ hy_Wrecker1*。hy_WageTaxTotal* からは外れる
#   ・ショートパーツ（LineNo=4）→ hy_PartsTaxTotal*（動かない）
#   ・代車（自由行）→ hy_WageTaxTotal*（動かない）
#   ・非課税（自由行）→ hy_WageNoTaxTotal*（動かない）
# 工賃側に入れたまま一覧から帳票を出すと、小計の工賃列がレッカー代込みで印字され、
# その下に「レッカー代１」がもう一度出る（合計は合うが紙の上で検算が合わない）。
_COGNI_PLACE = [
    # (明細, ショートパーツ, 費用, 期待する Total の欄)
    ([(45000, 8000), (0, 12000)], 0, {'towing': 20000, 'rental_car': 0, 'tax_exempt': 0},
     {'hy_Wrecker1OutTax': 20000, 'hy_Wrecker1Tax': 2000, 'hy_Wrecker1InTax': 22000,
      'hy_WageTaxTotalOutTax': 0, 'hy_WageTaxTotalTax': 0,
      'hy_PartsTaxTotalOutTax': 0, 'SubTotal': 85000, 'Total': 93500}),
    ([(45001, 8003), (0, 12005)], 517, {'towing': 105, 'rental_car': 1033, 'tax_exempt': 1500},
     {'hy_PartsTaxTotalOutTax': 517, 'hy_PartsTaxTotalTax': 52,
      'hy_Wrecker1OutTax': 105, 'hy_Wrecker1Tax': 11,
      'hy_WageTaxTotalOutTax': 1033, 'hy_WageTaxTotalTax': 103,
      'hy_WageNoTaxTotalOutTax': 1500, 'SubTotal': 66664, 'Total': 74830}),
]
for _rows, _sp, _exp, _want_cols in _COGNI_PLACE:
    _items = [{'name': f'X{_i}', 'method': '取替' if _i == 0 else '脱着',
               'parts_amount': _p, 'wage': _w, 'quantity': 1}
              for _i, (_p, _w) in enumerate(_rows)]
    _neo = app.generate_neo_file(TPL, {}, _items, _sp, {}, _exp, False, False, False)[0]
    _cur = neogen.opendb(neogen.unpack(_neo)['AnSMB.txt']).cursor()
    _got = _cur.execute('select %s from Total' % ', '.join(_want_cols)).fetchone()
    for _k, _v, _g in zip(_want_cols, _want_cols.values(), _got):
        chk(_g == _v, f'実機の置き場: {_k} が {_g:,}（コグニ実機は {_v:,}。費用 {_exp}・ショートパーツ {_sp}）')
    # 一覧に出る「諸経費計」はレッカー代も含んだ額のまま（実機の保存後も同じだった）
    _sum = app._summary_totals(neogen.unpack(_neo)['AnSMB.txt'])
    _want_exp = _sp + _exp['towing'] + _exp['rental_car'] + _exp['tax_exempt']
    chk(_sum and _sum[3] == _want_exp,
        f'実機の置き場: 一覧の諸経費計が {_sum and _sum[3]}（期待 {_want_exp:,}。レッカー代を落としていないか）')

# _calc_tax が tax_round を見ていること（常に四捨五入に戻っていないか）
_src_upd = __import__('re').sub(r'[ 	]+', ' ', __import__('inspect').getsource(app._update_ansmb_body))
chk('tax = _round_tax10(amount, tax_round)' in _src_upd,
    '費用行の税が端数処理の設定を見ていない（常に四捨五入に戻っている）')
chk('tax = jpy_round(amount * TAX_RATE)' not in _src_upd,
    '費用行の税に四捨五入の決め打ちが残っている')

print('REG_EXPENSE:', 'ALL PASS' if not FAIL else f'FAIL {len(FAIL)}件')
for f in FAIL[:10]: print('  -', f)
sys.exit(1 if FAIL else 0)
