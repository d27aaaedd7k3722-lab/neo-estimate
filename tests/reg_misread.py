# -*- coding: utf-8 -*-
"""印字小計の誤読・値引きの読み落とし・少額行の読み落としで、
元見積と違う .neo を無言で出さないことを確かめる回帰テスト。

いずれも「合計を合わせるために原本に無い行を作る」「行が1本消えたのに
警告が出ない」という、協定見積として致命的な症状を作り込んだ実績がある。
"""
import sys, os, io, contextlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('XROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zz_h import run

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m)

ROWS3 = [{"work_or_part_name": "フロントバンパー", "category": "取替", "labor_fee": 0,
          "quantity": 1, "part_price": 300000, "part_number": "A"},
         {"work_or_part_name": "フロントフェンダ", "category": "取替", "labor_fee": 0,
          "quantity": 1, "part_price": 200000, "part_number": "B"},
         {"work_or_part_name": "バンパー脱着", "category": "脱着", "labor_fee": 100000,
          "quantity": 1, "part_price": 0, "part_number": ""}]

def go(rows, hdr, tax_incl=False):
    B = io.StringIO()
    with contextlib.redirect_stdout(B):
        return run('reg', rows, hdr, tax_incl=tax_incl, show=False)

# 1. 小計を誤読しても、明細が正しければ原本どおりの行数・総額で出す。
#    （印字小計が「総合計÷1.1」を1%超えた瞬間から、原本に無い調整行が
#      1本入り総額が約10%増えていた）
for label, dp, dw in (('工賃計+30,000誤読', 0, 30000), ('部品計+30,000誤読', 30000, 0),
                      ('小計+10,000誤読', 5000, 5000), ('小計+40,000誤読', 20000, 20000)):
    r = go(ROWS3, {"pdf_parts_total": 500000 + dp, "pdf_wage_total": 100000 + dw,
                   "pdf_grand_total": 660000, "discount_amount": 0})
    chk(len(r['rows']) == 3 and r['tot'][3] == 660000,
        f"1: {label} -> {len(r['rows'])}行/{r['tot'][3]:,}（期待 3行/660,000）")

# 2. 値引きをヘッダで拾えなくても、明細の値引き行から補って原本どおりにする
ROWS_D = ROWS3 + [{"work_or_part_name": "値引き", "category": "", "labor_fee": 0,
                   "quantity": 1, "part_price": -20000, "part_number": ""}]
for label, disc in (('値引きを拾えなかった', 0), ('値引きを拾えた', 20000)):
    r = go(ROWS_D, {"pdf_parts_total": 500000, "pdf_wage_total": 100000,
                    "pdf_grand_total": 638000, "discount_amount": disc})
    chk(len(r['rows']) == 4 and r['tot'][3] == 638000,
        f"2: {label} -> {len(r['rows'])}行/{r['tot'][3]:,}（期待 4行/638,000）")

# 3. 少額の行を1行読み落としたら必ず知らせる（調整行が作られない額でも）
#    総額の0.1%を警告の下限にしていたため、60万円の見積で600円の
#    クリップ1行が無警告で消えていた。
for miss in (300, 500, 600, 1000, 2000):
    r = go(ROWS3, {"pdf_parts_total": 500000 + miss, "pdf_wage_total": 100000,
                   "pdf_grand_total": int(round((600000 + miss) * 1.1)),
                   "discount_amount": 0})
    chk(bool(r['warn']), f"3: {miss}円の読み落としが無警告")

# 4. 正しく読めている見積では誤警告を出さない
r = go(ROWS3, {"pdf_parts_total": 500000, "pdf_wage_total": 100000,
               "pdf_grand_total": 660000, "discount_amount": 0})
chk(len(r['rows']) == 3 and r['tot'][3] == 660000 and not r['warn'],
    f"4: 完全一致なのに {len(r['rows'])}行/{r['tot'][3]:,}/警告{len(r['warn'])}件")

# 5. 印字小計が値引き「後」の見積で、二重に値引きを引かないこと。
#    引くと値引きが総額の8〜9%のとき税抜の総合計を税込と取り違え、
#    総額が9.09%減って原本に無い調整行が1本入る。
for _pct in (0.05, 0.076, 0.08, 0.085, 0.09, 0.095, 0.10, 0.12):
    _sub = 600000
    _d = int(round(_sub * _pct))
    rows = ROWS3 + [{"work_or_part_name": "値引き", "category": "", "labor_fee": 0,
                     "quantity": 1, "part_price": -_d, "part_number": ""}]
    _net = _sub - _d
    r = go(rows, {"pdf_parts_total": 500000 - _d, "pdf_wage_total": 100000,
                  "pdf_grand_total": _net, "discount_amount": 0})
    _want = int(round(_net * 1.1))
    chk(len(r['rows']) == 4 and r['tot'][3] == _want,
        f"5: 値引き{_pct:.1%}(印字小計は値引き後) -> {len(r['rows'])}行/{r['tot'][3]:,}"
        f"（期待 4行/{_want:,}）")

print('REG_MISREAD:', 'ALL PASS' if not FAIL else f'FAIL {len(FAIL)}件')
for f in FAIL: print('  -', f)
sys.exit(1 if FAIL else 0)
