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
        ' tx_TotalOutTax, hy_WageNoTaxTotalInTax, SubTotal, Total from Total').fetchone()

# 1. 内訳の税込欄の合計と Total、税額欄の合計と tx_Total が一致すること
#    （まとめて1回で丸めていたため、同じ .neo の中で1円食い違っていた）
random.seed(20260909)
for _ in range(120):
    p = random.randint(0, 300000); w = random.randint(0, 200000)
    exp = {'towing': random.choice([0, 12345, 13636, 20000, 7777]),
           'rental_car': random.choice([0, 15000, 9999]),
           'tax_exempt': random.choice([0, 11000, 3333])}
    for ti in (False, True):
        items = [{'name': 'A', 'method': '取替', 'parts_amount': p, 'wage': 0, 'quantity': 1},
                 {'name': 'B', 'method': '脱着', 'parts_amount': 0, 'wage': w, 'quantity': 1}]
        neo = app.generate_neo_file(TPL, {}, items, 0, {}, exp, ti, False, False)[0]
        _c, t = totals(neo)
        chk(t[0] + t[1] + t[2] + t[7] == t[9],
            f'1: 内訳(税込)={t[0]+t[1]+t[2]+t[7]:,} と Total={t[9]:,} が不一致 '
            f'(p={p} w={w} {exp} 税込表記={ti})')
        chk(t[3] + t[4] + t[5] == t[6],
            f'1b: 税額内訳={t[3]+t[4]+t[5]:,} と tx_Total={t[6]:,} が不一致 '
            f'(p={p} w={w} {exp} 税込表記={ti})')
        # 内部の整合だけ見ていると、「内訳も総額もそろって原本から1円ずれる」
        # という壊れ方を素通りする。原本の総額そのものと突き合わせる。
        _exp_out = exp['towing'] + exp['rental_car']
        if ti:
            # 税込表記: 明細ぶんは「税を足すと原本の税込額に戻る」税抜額 S。
            # 消費税は S と費用を別々に1回ずつ丸める。
            _want = (app.best_intax_for(p + w)
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

# 2. 「PDFからNEOを生成」経路でも費用が .neo に入ること
items = [{'name': 'フロントバンパー', 'method': '取替', 'parts_amount': 45000, 'wage': 0, 'quantity': 1},
         {'name': 'バンパー脱着', 'method': '脱着', 'parts_amount': 0, 'wage': 12000, 'quantity': 1}]
exp = {'towing': 20000, 'rental_car': 15000, 'tax_exempt': 11000}
neo = P._call_generate_neo(TPL, {}, items, is_beta_mode=True, expenses=exp)
cur, t = totals(neo)
rows = [r for r in cur.execute('select LineNo, WageOutTax from Expense') if r[1]]
chk(sorted(rows) == [(5, 20000), (7, 15000), (8, 11000)],
    f'2: PDF経路の Expense が {rows}（期待 LineNo5=20000/7=15000/8=11000）')
chk(t[9] == 112200, f'2b: PDF経路の Total={t[9]:,}（期待 112,200）')

print('REG_EXPENSE:', 'ALL PASS' if not FAIL else f'FAIL {len(FAIL)}件')
for f in FAIL[:10]: print('  -', f)
sys.exit(1 if FAIL else 0)
