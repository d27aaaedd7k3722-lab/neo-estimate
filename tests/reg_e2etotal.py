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
_i_dedup = _src.find('items = _final_dedup_items(items or [], freeze=True)')
_i_match = _src.find('# v7: PDF表示総額と明細合算の差分')
chk(_i_dedup > 0 and _i_match > 0, '9b: 目印の行が見つからない')
chk(0 < _i_dedup < _i_match,
    '9c: 重複行の統合が総額の突き合わせより後ろにある'
    '（同じ金額が2回引かれる）')

# ── 9d. 突き合わせの「後」に行が消えないこと（Codex 指摘） ──────────
# パイプラインは 統合 → 総額の突き合わせ → ADDATA照合 → NEO生成 の順で、
# NEO生成の中（_call_generate_neo）でもう1回統合が走る。照合が品番を
# 埋めて2行の見分けが付かなくなると、**突き合わせの後で**行が消えて、
# 差を埋めた調整行だけが残る（また二重に引かれる）。
_a = {'name': 'Rrﾊﾞﾝﾊﾟ', 'part_no': '', 'parts_amount': 38600, 'wage': 0,
      'page': 1}
_b = {'name': 'Rrﾊﾞﾝﾊﾟ', 'part_no': '52159-52250', 'parts_amount': 38600,
      'wage': 0, 'page': 2}
# 品番が違うので、この時点では統合されない（残す、と判断する）
_kept = P._final_dedup_items([_a, _b], freeze=True)
chk(len(_kept) == 2, '9d: 品番の違う2行を統合してしまった')
# このあと ADDATA 照合が品番を埋めて、2行が見分け付かなくなる
_a['part_no'] = '52159-52250'
chk(len(P._final_dedup_items(_kept)) == 2,
    '9d: 突き合わせの後で行が消えた（調整行だけが残り二重に引かれる）')
# 印の付いていない行は、これまでどおり統合される
_c = {'name': 'ｸﾘﾂﾌﾟ', 'part_no': 'X', 'parts_amount': 100, 'wage': 0,
      'page': 1}
_d = {'name': 'ｸﾘﾂﾌﾟ', 'part_no': 'X', 'parts_amount': 100, 'wage': 0,
      'page': 2}
chk(len(P._final_dedup_items([_c, _d])) == 1,
    '9e: ページ境界の重複行が統合されなくなった')
# 内部用の印が生成側に漏れないこと
chk(all('_dedup_frozen' not in x
        for x in P._normalize_items_for_neo(_kept)),
    '9f: 内部用の印 _dedup_frozen が生成側に漏れている')

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
    # 調整行が入った .neo は「原本どおり」ではない。読み落としの疑いとして
    # 印を付け、キャッシュにも残さないこと（次に同じPDFを出したときに
    # 捏造行入りの .neo がそのまま返ると、確認の機会そのものが無くなる）。
    chk(_res.get('ocr_incomplete'),
        '11b: 調整行が入ったのに ocr_incomplete が立っていない'
        '（捏造行入りの .neo がキャッシュに残る）')
    chk(_res.get('adjustment_parts') or _res.get('adjustment_wage'),
        '11c: 調整額が結果に入っていない（画面が大きさを出せない）')

# ── 12. 検証がマイナスの行を見落とさないこと ─────────────────────
# 「金額が0以下なら単価×数量で代替」という分岐が、値引き行(-4,000)を
# 手入力行の単価欄(-1)で置き換え、そこから 0 に潰していた。総額を
# 減らす方向の異常がまるごと検証をすり抜け、「PDFと一致」と出ていた。
_TPLB = open(TPL, 'rb').read()
_base = [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600,
          'wage': 0, 'quantity': 1},
         {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0,
          'wage': 9800, 'quantity': 1}]
_disc = _base + [{'name': 'お値引き', 'method': '', 'parts_amount': -4000,
                  'wage': 0, 'quantity': 1}]

_nb_disc = app.generate_neo_file(_TPLB, {'customer_name': 'ｹﾝｼｮｳ'},
                                 [dict(i) for i in _disc], 0, {}, {},
                                 False, False, False)[0]
# (a) 値引き行が .neo にも明細にもある → 一致
_v = P.verify_neo_against_pdf(_nb_disc, [dict(i) for i in _disc],
                              pdf_parts_total=38600, pdf_wage_total=9800)
chk(_v.get('total_match'),
    '12a: 値引きのある正しい .neo で「差異あり」と出る（誤報）')
chk(_v.get('neo_minus_total') == -4000,
    '12b: 値引き行が %s として数えられている（-4000 のはず）'
    % _v.get('neo_minus_total'))
chk(_v.get('neo_total') == 38600 - 4000,
    '12c: .neo の部品計 %s（38,600-4,000 のはず）' % _v.get('neo_total'))

# (b) .neo にだけマイナスの行がある（原本に無い行が紛れた） → 見つかること
_v2 = P.verify_neo_against_pdf(_nb_disc, [dict(i) for i in _base],
                               pdf_parts_total=38600, pdf_wage_total=9800)
chk(not _v2.get('total_match'),
    '12d: .neo にだけ -4,000 の行があるのに「一致」と報告している'
    '（総額を減らす方向の異常が検証をすり抜ける）')
chk(any(m.get('type') == 'minus' for m in (_v2.get('mismatches') or [])),
    '12e: マイナスの行の食い違いが mismatches に出ていない')

# (c) 税込でも同じこと
_nb_in = app.generate_neo_file(
    _TPLB, {'customer_name': 'ｹﾝｼｮｳ'},
    [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 42460, 'wage': 0,
      'quantity': 1},
     {'name': 'お値引き', 'method': '', 'parts_amount': -5000, 'wage': 0,
      'quantity': 1}], 0, {}, {}, True, False, False)[0]
_v3 = P.verify_neo_against_pdf(
    _nb_in,
    [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 42460, 'wage': 0,
      'quantity': 1},
     {'name': 'お値引き', 'method': '', 'parts_amount': -5000, 'wage': 0,
      'quantity': 1}],
    pdf_parts_total=42460, is_tax_inclusive=True)
chk(_v3.get('total_match'),
    '12f: 税込の値引きのある .neo で「差異あり」と出る（誤報）')
chk(_v3.get('neo_minus_total') < 0,
    '12g: 税込で値引き行が 0 に潰れている')

# ── 13. 工賃も検証すること ─────────────────────────────────
# 以前は ERParts の部品欄しか読んでおらず、工賃がいくら違っていても
# 行数と部品計さえ合えば「検証OK」と出ていた。工賃は見積の半分の金額。
_I = [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600,
       'wage': 0, 'quantity': 1},
      {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0,
       'wage': 9800, 'quantity': 1}]
_nb = app.generate_neo_file(_TPLB, {'customer_name': 'ｹﾝｼｮｳ'},
                            [dict(i) for i in _I], 0, {}, {},
                            False, False, False)[0]
# 原本の工賃計は 24,300（14,500 の行を読み落とした）
_v = P.verify_neo_against_pdf(_nb, _I, pdf_parts_total=38600,
                              pdf_wage_total=24300)
chk(_v.get('wage_match') is False,
    '13a: 工賃が 9,800 しか入っていないのに工賃の検証が通っている')
chk(not _v.get('total_match'),
    '13b: 工賃が 14,500 足りないのに「一致」と報告している')
chk(any(m.get('type') == 'wage' for m in (_v.get('mismatches') or [])),
    '13c: 工賃の食い違いが mismatches に出ていない')
# 工賃が合っているときは通ること（誤報を出さない）
_v = P.verify_neo_against_pdf(_nb, _I, pdf_parts_total=38600,
                              pdf_wage_total=9800)
chk(_v.get('ok'), '13d: 工賃が合っている正しい .neo で誤報が出る')

# ── 14. 値引き後の小計を出す帳票でも穴が開かないこと ─────────────
# 「値引き前・値引き後のどちらかに合えばよい」にすると、値引きと同額の
# 正の行が丸ごと消えても一致と出る。基準を先に決めて1つだけで比べる。
_good = [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600,
          'wage': 0, 'quantity': 1},
         {'name': 'お値引き', 'method': '', 'parts_amount': -4400,
          'wage': 0, 'quantity': 1}]
_bad = [dict(_good[0], parts_amount=34200), dict(_good[1])]


def _mk(its):
    return app.generate_neo_file(_TPLB, {'customer_name': 'ｹﾝｼｮｳ'},
                                 [dict(i) for i in its], 0, {}, {},
                                 False, False, False)[0]


# 印字された部品計が値引き「後」の 34,200 の帳票
_v = P.verify_neo_against_pdf(_mk(_good), _good, pdf_parts_total=34200)
chk(_v.get('total_match'), '14a: 値引き後の小計の帳票で誤報が出る')
chk(_v.get('parts_basis') == '値引き後',
    '14b: 基準の判定が「%s」になっている' % _v.get('parts_basis'))
_v = P.verify_neo_against_pdf(_mk(_bad), _good, pdf_parts_total=34200)
chk(not _v.get('total_match'),
    '14c: 値引きと同額(4,400円)の正の行が消えた .neo を「一致」と報告している')
# 印字された部品計が値引き「前」の 38,600 の帳票（従来どおり通ること）
_v = P.verify_neo_against_pdf(_mk(_good), _good, pdf_parts_total=38600)
chk(_v.get('total_match'), '14d: 値引き前の小計の帳票で誤報が出る')
chk(_v.get('parts_basis') == '値引き前',
    '14e: 基準の判定が「%s」になっている' % _v.get('parts_basis'))

# ── 15. 統合の印が画面まで漏れないこと ──────────────────────────
_ITEMS = [
    {'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600, 'wage': 0,
     'quantity': 1, 'page': 1},
    {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0, 'wage': 9800,
     'quantity': 1, 'page': 1},
]
_res = run(_ITEMS, {'pdf_parts_total': 38600, 'pdf_wage_total': 9800,
                    'discount_amount': 0,
                    'pdf_grand_total': jr(48400 * 1.10)}, False)
chk(all('_dedup_frozen' not in it for it in (_res.get('items') or [])),
    '15: 内部用の印 _dedup_frozen が画面に渡る items に載っている'
    '（取り込んだ先で二度と統合されず、見慣れない列が編集画面に出る）')

# ── 16. 「比べていない」を「一致」と言わないこと（Codex 3周目） ────────
# 印字された小計が読めていないと、突き合わせる相手が自分の読み取り結果に
# なる。それで一致しても何も確かめたことにならないのに、画面は
# 「検証OK」と出していた。
_I2 = [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600,
        'wage': 0, 'quantity': 1},
       {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0,
        'wage': 9800, 'quantity': 1}]
_GT = jr(48400 * 1.10)
# 小計も総額も印字されていない見積。突き合わせる相手が1つも無い。
_res = run(_I2, {'pdf_parts_total': 0, 'pdf_wage_total': 0,
                 'discount_amount': 0, 'pdf_grand_total': 0}, False)
_v = _res.get('verify') or {}
chk(_v.get('verified_against_pdf') is False,
    '16a: 何も読めていないのに「原本と突き合わせた」ことになっている')
chk(not _v.get('ok'),
    '16b: 突き合わせていないのに検証が「一致」になっている')
# 総額だけは印字されている見積は、総額と突き合わせられる
_res = run(_I2, {'pdf_parts_total': 0, 'pdf_wage_total': 0,
                 'discount_amount': 0, 'pdf_grand_total': _GT}, False)
_v = _res.get('verify') or {}
chk(_v.get('grand_match') is True and _v.get('ok'),
    '16a2: 総額が印字されているのに突き合わせていない')

# 工賃計が印字されていない見積で、工賃の行を読み落とした
# （A-4 が明細合算で工賃計を埋めるが、それは印字された値ではない）
_res = run(_I2, {'pdf_parts_total': 38600, 'pdf_wage_total': 0,
                 'discount_amount': 0,
                 'pdf_grand_total': jr((38600 + 24300) * 1.10)}, False)
_v = _res.get('verify') or {}
chk(_v.get('wage_match') is None,
    '16c: 明細から作った工賃計を「印字された値」として検証に使っている'
    '（工賃を丸ごと読み落としても一致してしまう）')
chk(not _v.get('ok'), '16d: 工賃が 14,500 足りないのに検証が通っている')
# 印字と同じ基準で比べるので、丸めのための許容は要らない
chk(P.verify_neo_against_pdf.__defaults__ is not None, '16d2: 署名が読めない')
chk(_v.get('grand_match') is False,
    '16e: 印字された総額との突き合わせで捕まっていない')

# ── 17. 印字された総額と .neo の合計を直接くらべること ─────────────
# 部品計・工賃計が値引き前か後かに左右されない、いちばん強い検査。
_res = run(_I2, {'pdf_parts_total': 38600, 'pdf_wage_total': 9800,
                 'discount_amount': 0, 'pdf_grand_total': _GT}, False)
_v = _res.get('verify') or {}
chk(_v.get('grand_match') is True,
    '17a: 正しい .neo で総額の突き合わせが通らない（誤報）')
chk(_v.get('neo_grand_total') == _GT,
    '17b: .neo の合計 %s（原本 %s）' % (_v.get('neo_grand_total'), _GT))
chk(_v.get('ok'), '17c: 正しい .neo で検証が通らない')
# 費用を足した .neo は総額が増えるのが正しいので、突き合わせを外す
_res = run(_I2, {'pdf_parts_total': 38600, 'pdf_wage_total': 9800,
                 'discount_amount': 0, 'pdf_grand_total': _GT}, False,
           expenses={'towing': 12000})
_v = _res.get('verify') or {}
chk(_v.get('grand_match') is None,
    '17d: 費用を足した .neo で総額の突き合わせをして誤報を出している')
chk(_v.get('ok'), '17e: 費用を足しただけで検証が落ちている')

# ── 18. 税込は税込どうしで比べること ─────────────────────────────
# 以前は税込表記でも .neo の税抜どうしで比べていた。数量2以上の行は
# 「単価で丸めて数量倍」（実機と同じ数え方）なので、印字1,710÷1.1=1,555 と
# .neo の1,550 が5円ずれ、正しい .neo が「差異あり」と報告されていた。
_res = run([{'name': 'ｸﾘﾂﾌﾟ', 'method': '取替', 'parts_amount': 1710,
             'wage': 0, 'quantity': 10},
            {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0,
             'wage': 99999, 'quantity': 1}],
           {'pdf_parts_total': 1710, 'pdf_wage_total': 99999,
            'discount_amount': 0, 'pdf_grand_total': 101709}, True)
_v = _res.get('verify') or {}
chk(_v.get('total_match'),
    '18a: 税込・数量10 の正しい .neo で「差異あり」と出る'
    '（税抜に割り戻して比べているため）')
chk(_v.get('ok'), '18b: 税込・数量10 の正しい .neo で検証が通らない')
chk(_v.get('neo_total') == 1710,
    '18c: .neo の部品計を税込で %s と数えている（1,710 のはず）'
    % _v.get('neo_total'))

# ── 19. 工賃だけの見積（印字された部品計 0 は正しい値） ────────────
_res = run([{'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0,
             'wage': 9800, 'quantity': 1},
            {'name': 'ﾊﾞｯｸﾄﾞｱ板金', 'method': '板金', 'parts_amount': 0,
             'wage': 24500, 'quantity': 1}],
           {'pdf_parts_total': 0, 'pdf_wage_total': 34300,
            'discount_amount': 0, 'pdf_grand_total': jr(34300 * 1.10)}, False)
_v = _res.get('verify') or {}
chk(_v.get('ok'),
    '19: 工賃だけの見積が「検証できていません」になる'
    '（印字された部品計 0 を「読めなかった」と取り違えている）')

# ── 20. 金額の入っている側を比べ残さないこと（Codex 5周目） ──────────
# 部品計だけ印字され、工賃計も総額も印字されていない見積では、
# 工賃を1円も比べないまま「一致」と出ていた。
_res = run(_I2, {'pdf_parts_total': 38600, 'pdf_wage_total': 0,
                 'discount_amount': 0, 'pdf_grand_total': 0}, False)
_v = _res.get('verify') or {}
chk(not _v.get('ok'),
    '20a: 工賃を一度も突き合わせていないのに検証が通っている')
chk(any(m.get('type') == 'no_pdf_wage'
        for m in (_v.get('mismatches') or [])),
    '20b: 工賃を比べていないことが mismatches に出ていない')

# ── 21. 総額の丸めは生成側と同じ規則を使うこと ──────────────────
# Python の round() は偶数丸め。生成側は四捨五入（.5 切り上げ）なので、
# .5 になる見積で正しい .neo が「差異あり」になる。
_src = inspect.getsource(P.verify_neo_against_pdf)
chk('int(round(_g * (1 + tax_rate)))' not in _src,
    '21: 総額の丸めに Python の round()（偶数丸め）を使っている')

# ── 22. 工賃が差引0になる見積で素通りしないこと（Codex 6周目） ────────
# 工賃の行とマイナスの行が打ち消し合って 0 になると、差引後の合計では
# 「工賃が入っていない」ように見え、突き合わせの網羅チェックを
# すり抜けていた。部品側と同じく「行があるかどうか」で見る。
_IZ = [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600,
        'wage': 0, 'quantity': 1},
       {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0,
        'wage': 9800, 'quantity': 1},
       {'name': '工賃サービス', 'method': '', 'parts_amount': 0,
        'wage': -9800, 'quantity': 1}]
_nb = app.generate_neo_file(_TPLB, {'customer_name': 'ｹﾝｼｮｳ'},
                            [dict(i) for i in _IZ], 0, {}, {},
                            False, False, False)[0]
# 工賃計も総額も印字されていない見積
_v = P.verify_neo_against_pdf(_nb, _IZ, pdf_parts_total=38600)
chk(not _v.get('ok'),
    '22a: 工賃が差引0の見積で、工賃を一度も比べずに検証が通っている')
chk(any(m.get('type') == 'no_pdf_wage'
        for m in (_v.get('mismatches') or [])),
    '22b: 工賃を比べていないことが mismatches に出ていない'
    '（差引後の合計で見ているため打ち消し合うと素通りする）')

# ── 23. 行ごとの金額を比べること（Codex 7周目） ────────────────────
# 合計と行数しか見ていなかったので、2行のあいだで金額が入れ替わって
# いても「一致」と出ていた。協定見積は「同じ明細行・同じ金額」が条件。
_good = [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600,
          'wage': 0, 'quantity': 1},
         {'name': 'Fﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 20000,
          'wage': 0, 'quantity': 1}]
_swap = [dict(_good[0], parts_amount=39600), dict(_good[1], parts_amount=19000)]
_GT2 = jr(58600 * 1.10)
_v = P.verify_neo_against_pdf(
    app.generate_neo_file(_TPLB, {'customer_name': 'ｹﾝｼｮｳ'},
                          [dict(i) for i in _swap], 0, {}, {},
                          False, False, False)[0],
    _good, pdf_parts_total=58600, pdf_grand_total=_GT2, grand_is_intax=True)
chk(_v.get('line_match') is False,
    '23a: 2行のあいだで金額が入れ替わっているのに行ごとの検証が通っている')
chk(not _v.get('ok'),
    '23b: 合計と行数が合っていれば、行の金額が違っても「一致」と出る')
chk(any(m.get('type') == 'line' for m in (_v.get('mismatches') or [])),
    '23c: どの行が違うのかが mismatches に出ていない')
# 正しい .neo では行ごとの検証も通ること
_v = P.verify_neo_against_pdf(
    app.generate_neo_file(_TPLB, {'customer_name': 'ｹﾝｼｮｳ'},
                          [dict(i) for i in _good], 0, {}, {},
                          False, False, False)[0],
    _good, pdf_parts_total=58600, pdf_grand_total=_GT2, grand_is_intax=True)
chk(_v.get('line_match') is True and _v.get('ok'),
    '23d: 正しい .neo で行ごとの検証が落ちる（誤報）')

# ── 24. 総額を渡されたのに読めなかったら素通りさせないこと ──────────
# .neo の Total が読めなかったときに grand_match が None のままだと、
# 「いちばん強い検査を外した」まま合格になる。
_v = P.verify_neo_against_pdf(b'not a neo', _good, pdf_parts_total=58600,
                              pdf_grand_total=_GT2)
chk(not _v.get('ok'), '24: 壊れた .neo で検証が通っている')

# ── 25. 画面が verify の ok を見ていること（Codex 8周目） ─────────────
# 行ごとの検証を足しても、画面が count_match と total_match しか見て
# いなければ「検証OK」と出てしまい、足した意味が無くなる。
with open(os.path.join(R, 'app.py'), encoding='utf-8') as _f:
    _appsrc = _f.read()
chk("elif _p2n_v.get('ok'):" in _appsrc,
    '25a: 画面が verify の ok を見ていない'
    '（行ごとの検証が落ちても「検証OK」と出る）')
chk("_p2n_v.get('count_match') and _p2n_v.get('total_match')" not in _appsrc,
    '25b: 画面がまだ個別の項目だけで「検証OK」を出している')

# ── 26. 単価×数量しか持たない明細で誤報を出さないこと ────────────────
_UP = [{'name': 'ｸﾘﾂﾌﾟ', 'method': '取替', 'unit_price': 155,
        'quantity': 10, 'wage': 0},
       {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'unit_price': 0,
        'quantity': 1, 'wage': 9800}]
_nb = app.generate_neo_file(_TPLB, {'customer_name': 'ｹﾝｼｮｳ'},
                            [dict(i) for i in _UP], 0, {}, {},
                            False, False, False)[0]
_v = P.verify_neo_against_pdf(_nb, _UP, pdf_parts_total=1550,
                              pdf_wage_total=9800,
                              pdf_grand_total=jr(11350 * 1.10),
                              grand_is_intax=True)
chk(_v.get('line_match') is not False,
    '26a: 単価×数量しか持たない明細で行ごとの検証が誤報を出す')
chk(_v.get('ok'), '26b: 単価×数量しか持たない正しい .neo で検証が落ちる')

# ── 27. 明細に工賃があるのに工賃計も総額も無いとき合格にしないこと ──────
_IW = [{'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'parts_amount': 38600,
        'wage': 0, 'quantity': 1},
       {'name': 'ﾊﾞﾝﾊﾟ脱着', 'method': '脱着', 'parts_amount': 0,
        'wage': 9800, 'quantity': 1}]
_nb = app.generate_neo_file(_TPLB, {'customer_name': 'ｹﾝｼｮｳ'},
                            [dict(i) for i in _IW], 0, {}, {},
                            False, False, False)[0]
_v = P.verify_neo_against_pdf(_nb, _IW, pdf_parts_total=38600)
chk(not _v.get('ok'),
    '27: 工賃があるのに工賃計も総額も無い見積で、部品だけ見て合格にしている')

# ── 28. ERParts を並び順の指定なしで読まないこと ────────────────────
# SQLite は ORDER BY 無しの順番を約束しない。行ごとの突き合わせがずれる。
_src = inspect.getsource(P.verify_neo_against_pdf)
chk('ORDER BY LineNo' in _src,
    '28: ERParts を並び順の指定なしで読んでいる（行の突き合わせがずれる）')

print('REG_E2ETOTAL:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL:
    print('  -', f)
sys.exit(1 if FAIL else 0)
