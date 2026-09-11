# -*- coding: utf-8 -*-
"""見積書（原本）→ .neo の「総額が1円も違わないか」を端から端まで固定する。

協定見積は、行も金額も原本と同じであることが条件。総額だけ合っていても
中の行が動いていたら使えないので、**行ごとの金額**まで見る。

OCR は呼ばない（課金しない）。app.analyze_estimate を差し替えて、
中身の分かっている見積書を読み取ったことにして pipeline を通す。

固定している事故:
  - ページ境界の二重読み取りの統合が、総額の突き合わせより「後」だったため、
    重複ぶんを打ち消すマイナスの調整行を作ってから重複行を消していた。
    同じ金額が2回引かれ、38,600円の行が二重に読まれた見積で
    **総額が42,460円不足した**（2026-09-12）。

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
import pdf_to_neo_pipeline as P  # noqa: E402

FAIL = []
TPL = os.path.join(R, 'template_toyota.neo')
jr = app.jpy_round
_N = [0]


def chk(cond, msg):
    if not cond:
        FAIL.append(msg)


def neo_read(nb):
    """.neo から 明細行と合計を読む。"""
    ck = app.find_real_cks(nb)
    full = app.decompress_neo(nb, ck)
    _m, ent = app.parse_entries(nb, ck[0])
    fs = app.extract_files(full, ent)
    tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    try:
        tf.write(fs['AnSMB.txt'])
        tf.close()
        con = sqlite3.connect(tf.name)
        try:
            rows = list(con.execute(
                'select PartsName, PartsPriceOutTax, PartsPriceInTax,'
                ' WageOutTax, WageInTax from ERParts'
                ' where PartsName is not null and PartsName<>""'
                ' order by LineNo'))
            tot = con.execute('select Total from Total').fetchone()[0]
        finally:
            con.close()
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass
    return rows, tot


def run(items, meta, intax, expenses=None):
    """OCR を差し替えて pipeline を通す。"""
    _N[0] += 1
    m = dict(meta)
    m['items'] = items
    app.analyze_estimate = lambda *a, **kw: {
        k: ([dict(x) for x in v] if k == 'items' else v) for k, v in m.items()}
    app.analyze_vehicle_registration = lambda *a, **kw: {}
    return P.process_pdf_to_neo(
        b'%PDF-1.4 reg_e2etotal ' + str(_N[0]).encode(),
        template_path=TPL, mode_override='A', api_key='X',
        customer_info={'customer_name': 'ｹﾝｼｮｳ', 'car_name': 'ﾃｽﾄ'},
        is_tax_inclusive=intax, expenses=expenses,
        cache_scope='reg%d' % _N[0])


def case(title, rows, intax, meta_over=None, want=None):
    """rows = [(品名, 作業, 部品額, 工賃, 数量)]。金額は表示どおりの基準。

    原本の総額と1円も違わず、原本に無い行が増えず、行ごとの金額も
    原本のままであることを見る。
    """
    items = [{'name': a, 'method': b, 'parts_amount': c, 'wage': d,
              'quantity': e} for a, b, c, d, e in rows]
    pt = sum(r[2] for r in rows if r[2] > 0)
    wt = sum(r[3] for r in rows if r[3] > 0)
    disc = -(sum(r[2] for r in rows if r[2] < 0)
             + sum(r[3] for r in rows if r[3] < 0))
    grand = (pt + wt - disc) if intax else jr((pt + wt - disc) * 1.10)
    meta = {'pdf_parts_total': pt, 'pdf_wage_total': wt,
            'discount_amount': disc, 'pdf_grand_total': grand}
    if meta_over:
        meta.update(meta_over)
        grand = meta['pdf_grand_total']
    if want is not None:
        # 総額が印字されていない帳票では、明細の合算がそのまま総額になる。
        grand = want
    res = run(items, meta, intax)
    nb = res.get('neo_bytes')
    if not nb:
        chk(False, '%s: .neo が作られなかった %s' % (title, res.get('warnings')))
        return
    er, tot = neo_read(nb)
    chk(tot == grand,
        '%s: 合計 %s（原本 %s／差 %+d）'
        % (title, format(tot, ','), format(grand, ','), tot - grand))
    adj = [r[0] for r in er if '調整' in (r[0] or '')]
    chk(not adj, '%s: 原本に無い調整行が入った %s' % (title, adj))
    chk(len(er) == len(rows),
        '%s: 行数 %d（原本 %d）' % (title, len(er), len(rows)))
    by = {}
    for r in er:
        by.setdefault((r[0] or '').strip(), []).append(r)
    for nm, _mt, pa, wg, _q in rows:
        got = by.get(nm.strip())
        if not got:
            chk(False, '%s: 行「%s」が .neo に無い' % (title, nm))
            continue
        r = got[0]
        p_get = r[2] if intax else r[1]
        w_get = r[4] if intax else r[3]
        if pa:
            chk(p_get == pa,
                '%s: %s の部品 %s（原本 %s）' % (title, nm, p_get, pa))
        if wg:
            chk(w_get == wg,
                '%s: %s の工賃 %s（原本 %s）' % (title, nm, w_get, wg))


# ── 1. 素直な見積 ─────────────────────────────────────────
case('税抜・部品と工賃', [('Rrﾊﾞﾝﾊﾟ', '取替', 38600, 0, 1),
                          ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 9800, 1),
                          ('ﾊﾞｯｸﾄﾞｱ', '板金', 0, 24500, 1)], False)
case('税込・部品と工賃', [('Rrﾊﾞﾝﾊﾟ', '取替', 42460, 0, 1),
                          ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 10780, 1)], True)
# ── 2. 数量2以上（単価で丸めて数量倍になるか） ────────────────
case('税抜・数量10', [('ｸﾘﾂﾌﾟ', '取替', 1550, 0, 10),
                      ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 9800, 1)], False)
case('税込・数量10（割り切れない）', [('ｸﾘﾂﾌﾟ', '取替', 1710, 0, 10),
                                      ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 99999, 1)], True)
case('税込・数量3', [('ﾅｯﾄ', '取替', 1000, 0, 3),
                     ('ｺｳｺﾞｳ', '脱着', 0, 7777, 1)], True)
case('税込・数量100', [('ﾜｯｼｬｰ', '取替', 12300, 0, 100),
                       ('ｺｳｺﾞｳ', '脱着', 0, 9800, 1)], True)
# ── 3. 値引き ───────────────────────────────────────────
case('税抜・値引きあり', [('Rrﾊﾞﾝﾊﾟ', '取替', 38600, 0, 1),
                          ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 9800, 1),
                          ('お値引き', '', -4000, 0, 1)], False)
case('税込・値引きあり', [('Rrﾊﾞﾝﾊﾟ', '取替', 42460, 0, 1),
                          ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 10780, 1),
                          ('お値引き', '', -5000, 0, 1)], True)
# 値引きがヘッダに印字されず、明細の行にだけある
case('値引きがヘッダに無い', [('Rrﾊﾞﾝﾊﾟ', '取替', 38600, 0, 1),
                              ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 9800, 1),
                              ('お値引き', '', -4400, 0, 1)], False,
     {'pdf_parts_total': 38600, 'pdf_wage_total': 9800,
      'discount_amount': 0, 'pdf_grand_total': jr(44000 * 1.10)})
# 小計が値引き「後」で印字されている
case('小計が値引き後', [('Rrﾊﾞﾝﾊﾟ', '取替', 38600, 0, 1),
                        ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 9800, 1),
                        ('お値引き', '', -4400, 0, 1)], False,
     {'pdf_parts_total': 34200, 'pdf_wage_total': 9800,
      'discount_amount': 4400, 'pdf_grand_total': jr(44000 * 1.10)})
# ── 4. 端数 ────────────────────────────────────────────
case('税抜・1円単位', [('ﾌﾞﾋﾝA', '取替', 1, 0, 1), ('ﾌﾞﾋﾝB', '取替', 3, 0, 1),
                       ('ｺｳﾁﾝ', '脱着', 0, 5, 1)], False)
case('税込・税抜に戻すと.5', [('ﾌﾞﾋﾝA', '取替', 116, 0, 1),
                              ('ｺｳﾁﾝ', '脱着', 0, 6215, 1)], True)
# ── 5. 行数が多い ───────────────────────────────────────
case('税抜・20行',
     [('ﾌﾞﾋﾝ%02d' % i, '取替', 1000 + i * 137, 0, 1) for i in range(1, 11)]
     + [('ｺｳﾁﾝ%02d' % i, '脱着', 0, 800 + i * 91, 1) for i in range(1, 11)],
     False)
case('税込・20行',
     [('ﾌﾞﾋﾝ%02d' % i, '取替', 1100 + i * 151, 0, 1) for i in range(1, 11)]
     + [('ｺｳﾁﾝ%02d' % i, '脱着', 0, 880 + i * 101, 1) for i in range(1, 11)],
     True)
# ── 6. 小計・総額が印字されない帳票 ─────────────────────────
case('税抜・小計の印字なし', [('Rrﾊﾞﾝﾊﾟ', '取替', 38600, 0, 1),
                              ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 9800, 1)], False,
     {'pdf_parts_total': 0, 'pdf_wage_total': 0,
      'pdf_grand_total': jr(48400 * 1.10)})
case('税抜・総額の印字なし', [('Rrﾊﾞﾝﾊﾟ', '取替', 38600, 0, 1),
                              ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 9800, 1)], False,
     {'pdf_parts_total': 0, 'pdf_wage_total': 0, 'pdf_grand_total': 0},
     want=jr(48400 * 1.10))
# ── 7. 小計に入らない行が総額にだけ乗っている（レッカー代） ──────
case('小計に入らないレッカー代', [('Rrﾊﾞﾝﾊﾟ', '取替', 38600, 0, 1),
                                  ('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 9800, 1),
                                  ('ﾚｯｶｰ代', '', 15000, 0, 1)], False,
     {'pdf_parts_total': 38600, 'pdf_wage_total': 9800,
      'discount_amount': 0, 'pdf_grand_total': jr(63400 * 1.10)})
# ── 8. 片方しかない ────────────────────────────────────
case('税抜・工賃だけ', [('ﾊﾞﾝﾊﾟ脱着', '脱着', 0, 9800, 1),
                        ('ﾊﾞｯｸﾄﾞｱ板金', '板金', 0, 24500, 1)], False)
case('税込・部品だけ', [('Rrﾊﾞﾝﾊﾟ', '取替', 42460, 0, 1),
                        ('ｸﾘﾂﾌﾟ', '取替', 1710, 0, 10)], True)


# ── 9. ページ境界で二重に読まれた行（今回の事故） ───────────────
# 重複行を先に消さないと、重複ぶんを打ち消すマイナスの調整行を作ってから
# 重複行を消すことになり、同じ金額が2回引かれる。
_GRAND = jr(48400 * 1.10)
_ITEMS = [
    {'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600, 'wage': 0,
     'quantity': 1, 'page': 1},
    {'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600, 'wage': 0,
     'quantity': 1, 'page': 2},
    {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0, 'wage': 9800,
     'quantity': 1, 'page': 2},
]
_res = run(_ITEMS, {'pdf_parts_total': 38600, 'pdf_wage_total': 9800,
                    'discount_amount': 0, 'pdf_grand_total': _GRAND}, False)
_nb = _res.get('neo_bytes')
if not _nb:
    chk(False, '9: 二重読み取り — .neo が作られなかった')
else:
    _er, _tot = neo_read(_nb)
    chk(_tot == _GRAND,
        '9: 二重読み取りで合計 %s（原本 %s／差 %+d）'
        '— 重複ぶんが2回引かれている'
        % (format(_tot, ','), format(_GRAND, ','), _tot - _GRAND))
    chk(len(_er) == 2, '9: 二重読み取り後の行数 %d（原本 2）' % len(_er))
    chk(not [r for r in _er if '調整' in (r[0] or '')],
        '9: 二重読み取りで原本に無い調整行が入った')
    chk((_res.get('verify') or {}).get('total_match'),
        '9: 二重読み取りで verify の総額一致が外れている')

# 統合は突き合わせより前に書かれていること（並べ替えで戻らないよう縛る）
import inspect  # noqa: E402
_src = inspect.getsource(P.process_pdf_to_neo)
_i_dedup = _src.find('items = _final_dedup_items(items or [])')
_i_match = _src.find('# v7: PDF表示総額と明細合算の差分')
chk(_i_dedup > 0 and _i_match > 0, '9b: 目印の行が見つからない')
chk(0 < _i_dedup < _i_match,
    '9c: 重複行の統合が総額の突き合わせより後ろにある'
    '（同じ金額が2回引かれる）')

# ── 10. 総額を1桁誤読したら、行を捏造せず警告だけ ─────────────────
_ITEMS = [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600,
           'wage': 0, 'quantity': 1},
          {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0,
           'wage': 9800, 'quantity': 1}]
_res = run(_ITEMS, {'pdf_parts_total': 38600, 'pdf_wage_total': 9800,
                    'discount_amount': 0, 'pdf_grand_total': 803190}, False)
_nb = _res.get('neo_bytes')
if not _nb:
    chk(False, '10: .neo が作られなかった')
else:
    _er, _tot = neo_read(_nb)
    chk(not [r for r in _er if '調整' in (r[0] or '')],
        '10: 総額の誤読で 75万円の調整行を捏造している')
    chk(any('差が大きすぎ' in w for w in (_res.get('warnings') or [])),
        '10: 総額が大きく食い違うのに警告が出ていない')

# ── 11. 読み落としがあるときは調整行と警告が出る ──────────────────
_res = run(_ITEMS, {'pdf_parts_total': 38600, 'pdf_wage_total': 34300,
                    'discount_amount': 0,
                    'pdf_grand_total': jr(72900 * 1.10)}, False)
_nb = _res.get('neo_bytes')
if not _nb:
    chk(False, '11: .neo が作られなかった')
else:
    _er, _tot = neo_read(_nb)
    chk([r for r in _er if '調整' in (r[0] or '')],
        '11: 読み落としの差を埋める調整行が入っていない')
    chk(any('※金額調整' in w for w in (_res.get('warnings') or [])),
        '11: 調整行を足したことを知らせていない')

print('REG_E2ETOTAL:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL:
    print('  -', f)
sys.exit(1 if FAIL else 0)
