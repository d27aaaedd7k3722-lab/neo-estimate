# -*- coding: utf-8 -*-
"""同じPDFを2回通しても同じ .neo が出ることを確かめる。

解析結果のキャッシュはプロセス全体で共有（セッション跨ぎ・利用者跨ぎ）
なので、生成側がその dict を書き換えると1回目の結果が2回目の入力になり、
同じ見積から内訳の違う .neo が出る。
"""
import sys, os, io, contextlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('XROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zz_h import run

# 総合計が印字されておらず、部品/工賃の振り分けだけ誤読された見積
ROWS = [{"work_or_part_name": "フロントバンパー", "category": "取替", "labor_fee": 0,
         "quantity": 1, "part_price": 90000, "part_number": "X"},
        {"work_or_part_name": "バンパー脱着", "category": "脱着", "labor_fee": 60000,
         "quantity": 1, "part_price": 0, "part_number": ""}]
HDR = {"pdf_parts_total": 100000, "pdf_wage_total": 50000,
       "pdf_grand_total": 0, "discount_amount": 0}

# 1回目だけキャッシュを消し、2・3回目はキャッシュを残したまま通す。
# 毎回消すと、まさに検証したい「1回目の出力が2回目の入力になる」経路を
# テストが素通りしてしまう。
res = []
for i in range(3):
    B = io.StringIO()
    with contextlib.redirect_stdout(B):
        r = run(f'cache{i}', ROWS, HDR, tax_incl=False, show=False,
                keep_cache=(i > 0))
    res.append((len(r['rows']), tuple(r['tot'])))

ok = all(x == res[0] for x in res)
print('1回目:', res[0])
print('2回目:', res[1])
print('3回目:', res[2])
print('REG_CACHE:', 'ALL PASS' if ok else 'FAIL 同じ入力から違う .neo が出る')
sys.exit(0 if ok else 1)
