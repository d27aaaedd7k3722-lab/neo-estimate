# -*- coding: utf-8 -*-
"""reg_hunt4.py — 2026-09-22 のバグハント第 4 弾で直したもの。LLM の API は呼ばない。

  F1   ③の表の数の欄に小数を打つと 10 倍になる（45000.5 → 450005・2.5 → 25）→ 小数も受け、④へ進ませない
  Q2   車種フォルダ待ちが Addata の同一性を覚えていない → 取り置きに控え、再開の前に比べる
  P3   検算に通らなかった「確認用の NEO」の報告文が合格のように読める → 先頭に確認用である旨と不合格の理由
  P4   確認箇所シートが CSV のときの数式の無害化が、先頭の空白・全角の「＝」を見逃す
  Q1   橋渡しの部品が送り先・受け取り元の origin を確かめない（攻撃ページが部品を直接開くと乗っ取れる）
  Q4   橋渡しの 1 回の上限 128MB は実需（最大 11.1MB）の 11 倍 → 元の大きさで 24MB（送る知らせは base64 で約 32MB）

実画面での確かめ（このテストとは別に回した。scratchpad/hunt4/）:
  decimal_probe.py … F1。直す前 45000.5→450005・2.5→25、直した後は値のまま受けて④で止まる。整数は通る
  bridge_attack.py … Q1。直す前は別 origin のページが部品を乗っ取れて知らせを 4 通受け取る、直した後は 0 通。正規の親は前後とも反応する

    python tests/reg_hunt4.py
終了コード: 0 全部 OK / 1 失敗
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get('XROOT') or os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)
os.environ.setdefault('NEO_ADDATA_NO_AUTODETECT', '1')
sys.stdout.reconfigure(encoding='utf-8')
import logging  # noqa: E402

logging.getLogger('streamlit').setLevel(logging.CRITICAL)
import app  # noqa: E402
from neo_skill import bridge  # noqa: E402

FAILS: list = []
SRC = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
HTML = open(os.path.join(ROOT, 'neo_skill', 'addata_bridge', 'index.html'), encoding='utf-8').read()


def chk(cond, msg):
    if not cond:
        FAILS.append(msg)


# ── F1: 小数の見つけ方（振る舞い） ─────────────────────────────────────────
try:
    f = app._is_fractional_cell
    for v, want in ((45000.5, True), (2.5, True), ('2.5', True), (0.01, True),
                    (45000, False), (45000.0, False), (0, False), (None, False), (float('nan'), False),
                    ('', False), ('abc', False), (-3.5, True), (-3.0, False)):
        chk(f(v) is want, f'F1: _is_fractional_cell({v!r}) が {want} でない')
except Exception as e:  # noqa: BLE001  例外で落ちるのは「狙いの文言で落ちた」に数えない（9/22 の教訓）
    chk(False, f'F1: _is_fractional_cell を呼べない（{type(e).__name__}）')

# F1: 表の数の欄は小数を受ける（step=1 だと画面の部品が「.」を捨てて 10 倍にする）。
# 「'部品金額':」は表のデータを作る行にも出るので、**欄の設定（NumberColumn('部品金額', …)）の行そのもの**を見る
# （最初の出現で見ていたら、step=1 に戻しても通ってしまった。ミューテーションで発覚）
for col in ('数量', '部品金額', '工賃'):
    key = f"st.column_config.NumberColumn('{col}',"
    lines = [ln for ln in SRC.splitlines() if key in ln]
    chk(len(lines) == 1, f'F1: 表の「{col}」の欄の設定が見つからない（{len(lines)} 件）')
    args = lines[0].split(key, 1)[1].replace(' ', '') if lines else ''
    chk('step=0.01' in args and 'step=1,' not in args and 'step=1)' not in args,
        f'F1: 表の「{col}」がまだ step=1（小数を打つと 10 倍になる）')
chk("_df_edit[_numc] = pd.to_numeric(_df_edit[_numc], errors='coerce').astype(float)" in SRC,
    'F1: 表の数の列を小数型で渡していない（整数型だと打った小数が黙って切り捨てられる）')
# F1: 読み戻しで小数を控え、④の手前で止める
chk("'_frac_cols': [f'{_fl} {_fmt_frac(_fv)}'" in SRC and '_is_fractional_cell(_fv)' in SRC,
    'F1: 読み戻しで小数の欄を控えていない')
_gate = SRC.split('elif _qty_blank_nos:', 1)[1][:600] if 'elif _qty_blank_nos:' in SRC else ''
chk('elif _frac_nos:' in _gate and '小数が入っている欄があります' in _gate,
    'F1: 小数が入った行で④に進ませない止めが無い')
# F1: 打った小数はそのまま見せる（'{:g}' は有効 6 桁で 1234567.25 → 1.23457e+06 になっていた）
try:
    chk(app._fmt_frac(2.5) == '2.5' and app._fmt_frac(123456.5) == '123456.5' and app._fmt_frac(1234567.25) == '1234567.25'
        and app._fmt_frac(45000) == '45000',
        f'F1: 小数の見せ方が違う: {app._fmt_frac(1234567.25)!r}')
except Exception as e:  # noqa: BLE001
    chk(False, f'F1: _fmt_frac を呼べない（{type(e).__name__}）')
# F1: 行挿入・コピーで表を組み直すとき、打った小数を戻す（丸めた値で組み直すと止めをすり抜けた）
chk("'_frac_raw': {_fl: float(_fv) for _fl, _fv in" in SRC and "_fraw.get('数量', qty_int(_item.get('quantity', 1), 1))" in SRC
    and "_fraw.get('部品金額', safe_int(_item.get('parts_amount', 0)))" in SRC and "_fraw.get('工賃', safe_int(_item.get('wage', 0)))" in SRC,
    'F1: 表を組み直すときに、打った小数を戻していない（行挿入・コピーのあとに丸めた値で④へ進める）')

# ── P3: 確認用の NEO の報告文（振る舞い） ───────────────────────────────────
try:
    u = app._unverified_report
    md = '# 報告' + chr(10) + '- 検算: 見積書合計との一致: OK' + chr(10)
    r = u(md, ['ページ 2: 小計が合わない', '合計欄: 総額が読めない'])
    chk(r.startswith(app._UNVERIFIED_HEAD), 'P3: 報告文の先頭に「確認用の NEO」の見出しが無い')
    chk('ページ 2: 小計が合わない' in r and '合計欄: 総額が読めない' in r, 'P3: 不合格の理由が報告文に入らない')
    chk(r.index('- 検算: 見積書合計との一致: OK') > r.index(app._UNVERIFIED_HEAD), 'P3: 元の報告文より前に置かれていない')
    chk(u(r, ['x']).count(app._UNVERIFIED_HEAD) == 1, 'P3: 二度足している')
    chk(u('', ['x']) == '' and u(None, ['x']) is None, 'P3: 報告文が無いのに作っている')
    chk('画面の一覧' in u(md, []), 'P3: 理由が空のとき何も書かない（確認用である旨は必ず出す）')
    many = u(md, [f'ページ {i}: x' for i in range(45)])
    chk('ページ 29: x' in many and 'ページ 30: x' not in many and 'ほか 15 件' in many, 'P3: 理由の数の上限（30）が効いていない')
except Exception as e:  # noqa: BLE001
    chk(False, f'P3: _unverified_report を呼べない（{type(e).__name__}）')
# P3: 確認用の NEO を作る枝で使っている
_fu = SRC.split("if out.pop('force_unverified', False):", 1)
chk(len(_fu) > 1 and "out['report_md'] = _unverified_report(" in _fu[1][:2500],
    'P3: 確認用の NEO の報告文に見出しを付けていない')

# ── P4: CSV の確認箇所シートの無害化（振る舞い・両方向） ─────────────────────
try:
    TAB, NL, CRLF = chr(9), chr(10), chr(13) + chr(10)
    bad = ['=1+1', TAB + '=cmd', NL + '=HYPERLINK("x")', chr(0xFF1D) + '1+1', chr(0xFF20) + 'SUM(A1)', '-cmd', ' =1']
    good = ['+1', '-5', 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ', '45,000', '-1,200', '']
    rows = bad + good
    csv = ('col' + CRLF + CRLF.join('"' + x.replace('"', '""') + '"' for x in rows) + CRLF).encode('utf-8-sig')
    out = app._defuse_review_sheet(csv, '.csv').decode('utf-8-sig').split(CRLF)[1:-1]
    for x, o in zip(rows, out):
        defused = o.lstrip('"').startswith("'")
        chk(defused is (x in bad), f'P4: {x!r} の無害化が {"されない" if x in bad else "されてしまう"}')
except Exception as e:  # noqa: BLE001
    chk(False, f'P4: _defuse_review_sheet（CSV）を呼べない（{type(e).__name__}）')

# ── Q2: 車種フォルダ待ちに Addata の同一性（振る舞い） ─────────────────────────
try:
    import time as _tq
    _qa = tempfile.mkdtemp(prefix='reg_hunt4_q2a_')
    _qb = tempfile.mkdtemp(prefix='reg_hunt4_q2b_')
    try:
        _pend = {'file_key': 'F1', 'parked_at': _tq.time(), 'addata_id': app._p2n_addata_identity(_qa)}
        chk(app._p2n_pending_stale_reason(_pend, 'F1', _qa) == '', 'Q2: 同じ見積書・同じ Addata の取り置きを捨てている')
        chk('Addata' in app._p2n_pending_stale_reason(_pend, 'F1', _qb), 'Q2: 待っている間に Addata が替わっても取り置きで作る')
        chk('見積書' in app._p2n_pending_stale_reason(_pend, 'F2', _qa), 'Q2: 別の見積書でも取り置きで作る')
        chk('2 時間' in app._p2n_pending_stale_reason(_pend, 'F1', _qa, now=_tq.time() + 3 * 3600), 'Q2: 2 時間を過ぎた取り置きで作る')
        # 取り置きに Addata の同一性を控えている（控えが無いと比べられない）
        chk("_p2n_state['addata_id'] = _p2n_addata_identity(_p2n_addata)" in SRC, 'Q2: 取り置きに Addata の同一性を控えていない')
        # 画面は取り置きで NEO を作る（p2n_make を呼ぶ）前にこの関数で比べ、捨てる理由があれば取り置きを捨てて None にする
        _i_cmp = SRC.find('_p2n_stale_why = _p2n_pending_stale_reason(_p2n_pending, _p2n_file_key, _p2n_addata)')
        _i_dis = SRC.find('_p2n_pending = _p2n_discard_pending(st.session_state)')
        _i_use = SRC.find('_p2n_out = _guarded_call(p2n_make, _p2n_pending')
        chk(0 < _i_cmp < _i_dis < _i_use and SRC.count('_p2n_discard_pending(st.session_state)') == 1,
            'Q2: 取り置きで NEO を作る前に、捨てる理由を確かめて取り置きを捨てていない')
        # 捨てるのは**捨てる理由があるときだけ**（いつも捨てると、正しい取り置きでも NEO を作れない）。直前の行が理由の判定
        _prev = SRC[:_i_dis].rstrip().splitlines()[-1].strip() if _i_dis > 0 else ''
        chk(_prev == 'if _p2n_stale_why:', f'Q2: 取り置きを捨てる処理が、捨てる理由の判定の中に無い（直前の行: {_prev!r}）')
        # 捨てる処理（振る舞い）: 取り置き・作業フォルダ（見積書・顧客情報）が消え、待ちも解ける
        _qc = tempfile.mkdtemp(prefix='reg_hunt4_q2c_')
        open(os.path.join(_qc, 'reading.json'), 'w').close()
        _ss = {'_bridge_pending': {'case_dir': _qc, 'file_key': 'F1'}, '_bridge_want': 'T10'}
        chk(app._p2n_discard_pending(_ss) is None and '_bridge_pending' not in _ss and _ss['_bridge_want'] == ''
            and not os.path.isdir(_qc), f'Q2: 取り置きを捨てても作業フォルダ・待ちが残る: {sorted(_ss)} {os.path.isdir(_qc)}')
        import shutil as _shq
        _shq.rmtree(_qc, ignore_errors=True)
    finally:
        import shutil as _shq
        _shq.rmtree(_qa, ignore_errors=True)
        _shq.rmtree(_qb, ignore_errors=True)
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'Q2: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-300:])

# ── Q1: 橋渡しの部品の origin（postMessage の呼び出しを全部見る） ──────────────────
import re as _req
_js = _req.sub(r'/\*.*?\*/', '', HTML, flags=_req.S)
_js = '\n'.join(_req.sub(r'(?<![:\'"])//.*$', '', _ln) for _ln in _js.splitlines())   # コメントの中の文字列で通らないように


def _call_args(src, at):
    """src[at] が '(' の呼び出しの引数の文字列（括弧・文字列の中の括弧を数える）"""
    depth, i, q = 0, at, None
    while i < len(src):
        c = src[i]
        if q:
            if c == '\\':
                i += 2
                continue
            if c == q:
                q = None
        elif c in '\'"`':
            q = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return src[at + 1:i]
        i += 1
    return ''


def _last_arg(args):
    depth, q, cut = 0, None, 0
    for i, c in enumerate(args):
        if q:
            if c == q:
                q = None
        elif c in '\'"`':
            q = c
        elif c in '({[':
            depth += 1
        elif c in ')}]':
            depth -= 1
        elif c == ',' and depth == 0:
            cut = i + 1
    return args[cut:].strip()


_pm = [m.end() - 1 for m in _req.finditer(r'\.postMessage\s*\(', _js)]
_targets = [_last_arg(_call_args(_js, i)) for i in _pm]
chk(_pm and all(t == 'APP_ORIGIN' for t in _targets), f"Q1: postMessage の送り先が APP_ORIGIN でない呼び出しがある: {_targets}")
chk(_req.findall(r'\bAPP_ORIGIN\s*=(?!=)', _js) == ['APP_ORIGIN ='] and 'const APP_ORIGIN = window.location.origin;' in _js,
    'Q1: APP_ORIGIN がアプリ自身の origin（window.location.origin）だけから決まっていない')
_lst = _req.search(r"addEventListener\(\s*['\"]message['\"]", _js)
_body = _call_args(_js, _js.find('(', _lst.start())) if _lst else ''
chk(bool(_body) and _req.search(r'if\s*\(\s*ev\.origin\s*!==\s*APP_ORIGIN\s*\)\s*return\s*;', _body)
    and _body.find('ev.origin') < _body.find('ev.data'),
    'Q1: 受け取るとき、中身を読む前に origin を確かめていない')

# ── Q4: 橋渡しの上限（振る舞い） ────────────────────────────────────────────
chk(bridge.MAX_MESSAGE_BYTES == 24 * 1024 * 1024 and bridge.MAX_FILE_BYTES <= bridge.MAX_MESSAGE_BYTES,
    f'Q4: サーバ側の上限が元の大きさで 24MB でない（{bridge.MAX_MESSAGE_BYTES}。送る知らせは base64 で 4/3 倍）')
chk('MAX_MESSAGE_BYTES = 24 * 1024 * 1024' in HTML and 'MAX_FILE_BYTES = 24 * 1024 * 1024' in HTML,
    'Q4: 部品側の上限がアプリ側（元の大きさで 24MB）と違う')
d = d2 = None
try:
    d = tempfile.mkdtemp(prefix='reg_hunt4_')
    one_mb = base64.b64encode(os.urandom(1024 * 1024)).decode('ascii')
    # 同じ車種フォルダに 1MB を 40 本（合計 40MB > 24MB）→ そのフォルダは書かず、完了印も付けない
    files = {f'T/T10/T10{i:02d}.DB': one_mb for i in range(40)}
    n, nbytes, dropped = bridge.store(d, files)
    chk(dropped and not bridge.has_car(d, 'T10'), f'Q4: 24MB を超える車種フォルダを受け付けている（書いた {n} 本）')
    # 実需の最大（11.1MB 相当）は通る。has_car は <車種>01.DB と <車種>11.DB の両方を要るので 01〜11 の 11 本にする
    # （否定側の 40 本も 01・11 を含むので、「書かれていれば has_car が真になる」形で確かめている）
    d2 = tempfile.mkdtemp(prefix='reg_hunt4_')
    files2 = {f'T/T11/T11{i:02d}.DB': one_mb for i in range(1, 12)}
    n2, _b2, dropped2 = bridge.store(d2, files2)
    chk(n2 == 11 and not dropped2 and bridge.has_car(d2, 'T11'), f'Q4: 実需の大きさ（11MB）まで捨てている（書いた {n2} 本・捨てた {len(dropped2)}）')
except Exception as e:  # noqa: BLE001
    chk(False, f'Q4: bridge.store を呼べない（{type(e).__name__}: {e}）')
finally:
    import shutil as _shq4
    for _dq in (d, d2):
        if _dq:
            _shq4.rmtree(_dq, ignore_errors=True)

# ── C（添付書類 → NEO の欄）─────────────────────────────────────────────
try:
    from neo_skill import doc_hints as dh
    # C1: 読めなかったことを表す語は空（証券番号が空なら事故番号・受付番号を入れる決めが効く）
    for w in ('不明', 'なし', '記載なし', '－', '-', '―', 'N/A', '読取不可', '***'):
        chk(dh._get({'k': w}, 'k') == '', f'C1: 「{w}」を値として通している')
    for w in ('ABC-123', '2500000000-501', '品川', '0', '同上'):
        chk(dh._get({'k': w}, 'k') == w, f'C1: 値の「{w}」まで空にしている（逆向きの穴）')
    chk(app.policy_no_or_accept({'policy_no': '－', 'accept_no': '2500-501'}) == '2500-501',
        'C1: 証券番号が「－」のとき事故番号を入れていない')
    chk(app.policy_no_or_accept({'policy_no': 'P-123', 'accept_no': '2500-501'}) == 'P-123', 'C1: 本物の証券番号を捨てている')
    # C2: ベタ打ちの車検証の日付（初度登録は YYYYMM00、有効期限は日まであるときだけ）
    for raw, want in (('令和元年5月1日', '20190500'), ('平成元年1月8日', '19890100'), ('202111', '20211100'),
                      ('2021/11', '20211100'), ('2021/11/15', '20211100'), ('20211100', '20211100')):
        got = dh.vehicle_info_for_legacy({'car_reg_date': raw, 'customer_name': 'x'}).get('car_reg_date')
        chk(got == want, f'C2: 初度登録「{raw}」→ {got!r}（{want} のはず）')
    for raw, want in (('2026年11月', ''), ('2026/11/29', '20261129'), ('平成32年3月31日', '20200331')):
        got = dh.vehicle_info_for_legacy({'term_date': raw, 'customer_name': 'x'}).get('term_date')
        chk(got == want, f'C2: 有効期限「{raw}」→ {got!r}（{want!r} のはず）')
    for raw, want in (('2025/9/1(月)', '20250901'), ('2025年09月01日（月）', '20250901'), ('令和7年9月1日 10:30', '20250901'),
                      ('2025-09-01T10:30:00', '20250901'), ('令1/5/1', '20190501'), ('r1.5.1', '20190501'),
                      ('令和0年5月1日', ''), ('平成32年3月31日', '20200331')):
        chk(dh.date8(raw) == want, f'C2: date8「{raw}」→ {dh.date8(raw)!r}（{want!r} のはず）')
    chk(dh.reg_date_wareki('S0.1') == '' and dh.reg_date_wareki('R0.5') == '' and dh.reg_date_wareki('R3.11') == 'R3.11',
        'C14: 初度登録の和暦の 0 年を素通ししている（または正しい値を捨てている）')
    # C3: 車台番号・型式のダッシュの仲間・全角をそろえる（スキル経路の hint もベタ打ちも）
    for sep in ('-', 'ー', '―', '−', '–', '—', '‐', 'ｰ', '－'):
        h = dh.vehicle_hint({'car_serial_no': f'U61V{sep}1400002', 'car_model': f'5BA{sep}U61V'})
        chk(h.get('serial_no') == 'U61V-1400002' and h.get('model_code') == 'U61V',
            f'C3: 区切り「{sep}」をそろえていない: {h.get("serial_no")!r} / {h.get("model_code")!r}')
    chk(dh.vehicle_info_for_legacy({'car_serial_no': 'ＫＳＰ２１０－００５７６６２', 'customer_name': 'x'})['car_serial_no'] == 'KSP210-0057662',
        'C3: ベタ打ちの車台番号が全角のまま')
    # C8・C9: 登録番号は 4 欄を 1 つとして（ありえない番号・混ぜた番号を作らない）
    def _reg(vd, doc=None):
        v = dh.vehicle_info_for_legacy(dict(vd, customer_name='x'), doc)
        return tuple(v.get(k, '') for k in ('car_reg_department', 'car_reg_division', 'car_reg_business', 'car_reg_serial'))
    chk(_reg({'car_reg_department': '品川', 'car_reg_division': '300', 'car_reg_business': 'あ', 'car_reg_serial': '12−34'})
        == ('品川', '300', 'あ', '1234'), 'C8: 一連番号の「−」を掃除していない')
    for bad in ({'car_reg_serial': '123456789'}, {'car_reg_serial': 'l234'}, {'car_reg_division': '300.0'}, {'car_reg_business': 'あい'}):
        vd = dict({'car_reg_department': '品川', 'car_reg_division': '300', 'car_reg_business': 'あ', 'car_reg_serial': '1234'}, **bad)
        chk(_reg(vd) == ('', '', '', ''), f'C8: ありえない登録番号を書いている: {bad} → {_reg(vd)}')
    chk(_reg({'car_reg_department': '品川', 'car_reg_business': 'あ', 'car_reg_serial': '1234'}, {'reg_no': '横浜 500 さ 5678'})
        == ('横浜', '500', 'さ', '5678'), 'C9: 車検証と書類の登録番号を欄ごとに混ぜている')
    chk(_reg({'car_reg_division': '300', 'car_reg_business': 'あ', 'car_reg_serial': '1234'}) == ('', '', '', ''),
        'C8: 地名が空の「300」を「3」「00」と割って読んでいる')
    chk(dh.vehicle_info_for_legacy({}, {}) == {} and dh.vehicle_info_for_legacy(None, None) == {}, 'C8: 空の入力に空の欄を足している')
    # C13: 走行距離
    chk(dh.parse_km('1万5千km') == '15000' and dh.parse_km('9' * 25) == '' and dh.parse_km('15,345km') == '15345',
        f"C13: 走行距離の読み方が違う: {dh.parse_km('1万5千km')!r} / {dh.parse_km('9' * 25)!r}")
    # 上限（199 万 9999 km）: 桁は収まっていても、ありえない走行距離は入れない（25 桁は別の「10 桁以上は読まない」で止まる。
    # 上限そのものを守るため、桁の内側で上限を超える値で確かめる。ミューテーションで発覚）
    chk(dh.parse_km('5000000') == '' and dh.parse_km('300万km') == '' and dh.parse_km('150万km') == '1500000',
        f"C13: 走行距離の上限が効いていない: {dh.parse_km('5000000')!r} / {dh.parse_km('300万km')!r} / {dh.parse_km('150万km')!r}")
    # C10: 空白入りの印
    for p in ('同　上', '同 上', '＊ ＊ ＊'):
        chk(dh.is_placeholder(p), f'C10: 「{p}」を印と見分けていない')
    chk(not dh.is_placeholder('山田'), 'C10: 名前を印と見なしている')
    # C6: 会社名は入りきらないときだけ略す
    chk(dh.fit_corp_name('トヨタファイナンス株式会社', 20) == 'トヨタファイナンス㈱' and dh.fit_corp_name('株式会社サンプル', 20) == '株式会社サンプル',
        'C6: 会社名の略し方が違う（入りきらないときだけ ㈱）')
    # NEO に書く道（update_em_db）で、契約者欄（Insurance.ContractorName）が 20 バイトに収まり、㈱ に略してから切られる
    import sqlite3 as _sq6
    _tpl6 = open(os.path.join(ROOT, 'template_toyota.neo'), 'rb').read()
    _ck6 = app.find_real_cks(_tpl6)
    _db6 = app.extract_files(app.decompress_neo(_tpl6, _ck6), app.parse_entries(_tpl6, _ck6[0])[1])['AnSvEm0001Ex.db']
    _new6 = app.update_em_db(_db6, {}, {'contractor_name': '株式会社ケンショウロジスティクス九州支店'}, '20260922')
    _new6 = _new6[0] if isinstance(_new6, tuple) else _new6
    _c6 = _sq6.connect(':memory:')
    _c6.deserialize(_new6)
    _cn6 = _c6.execute('SELECT ContractorName FROM Insurance').fetchone()[0]
    _c6.close()
    chk(_cn6.startswith('㈱ケンショウ') and len(app.neo_header.encode_cp932w(_cn6)) <= 20,
        f'C6: NEO の契約者欄が 20 バイトに収まっていない／㈱ に略していない: {_cn6!r}（{len(app.neo_header.encode_cp932w(_cn6))} バイト）')
    # 使用者欄・所有者欄も同じ（略さずに切ると「…株」で切れる。レビュー 5 周目）
    _tc6 = app._trimmed_cust_values({'user_name': '株式会社ケンショウロジスティクス九州支店', 'owner_name': '株式会社ケンショウロジスティクス九州支店'})
    chk(_tc6['user_name'].startswith('㈱ケンショウ') and _tc6['owner_name'].startswith('㈱ケンショウ')
        and not _tc6['user_name'].endswith('株'),
        f"C6: NEO の使用者欄・所有者欄を略さずに切っている: {_tc6['user_name']!r} / {_tc6['owner_name']!r}")
    chk("_val = _doc_hints.fit_corp_name(_val, _w) if _lbl in ('使用者名', '所有者名') else _val" in SRC,
        'C6: ③の列幅の警告が、略す前の会社名で出ている')
    # C4: 要約は実際に使う形で数える
    chk(dh.summary({'car_reg_serial': '1234'}, None) == '' and '有効期限' not in dh.summary({'term_date': '令和8年11月', 'car_serial_no': 'A-1'}, None),
        'C4: NEO に入らない項目まで「読めた」と要約している')
    # C5: CP932 に無い字は、書類の値を読む入口で代わりの字に（DB と XML が食い違わないように）。代わりが無い字は残す
    chk(dh._clean('\U00020BB7田') == '吉田' and dh._clean('a—b') == 'a―b',
        f"C5: CP932 に無い字を代わりの字にしていない: {dh._clean(chr(0x20BB7) + '田')!r}")
    chk(dh._clean('𩸽') == '𩸽', 'C5: 代わりの字が無い字まで消している')
    # C11: 全角空白は全角のまま（実機の氏名は全角空白が 9 割）。半角の並びは半角 1 つ。所有者と使用者の比べは空白を見ない
    chk(dh._clean('山田　太郎') == '山田　太郎' and dh._clean('山田 　 太郎') == '山田　太郎' and dh._clean('A   B') == 'A B',
        f"C11: 氏名の全角空白を半角にしている: {dh._clean('山田　太郎')!r}")
    _ch = dh.customer_hint({'customer_name': '山田　太郎', 'owner_name': '山田 太郎'})
    chk(_ch.get('name') == '山田　太郎' and not _ch.get('owner_name') and not _ch.get('user_name'),
        f'C11: 空白の違いだけの所有者と使用者を別の人にしている: {_ch}')
    _lg = dh.vehicle_info_for_legacy({'customer_name': '山田　太郎', 'owner_name': '山田 太郎'})
    chk(_lg.get('user_name') == '同上', f"C11: ベタ打ちの使用者欄が「同上」にならない: {_lg.get('user_name')!r}")
    # C12: 事故日の 2 桁の年は、西暦の下 2 桁と令和の両方で読み、ありうる範囲に入るほう（無ければ空）
    import datetime as _dtm
    _T = _dtm.date(2026, 9, 22)
    for _s, _w in (('25.10.26', '20251026'), ('07.10.26', '20251026'), ('99.10.26', ''), ('08.01.15', '20260115'),
                   ('25.10.26 10:30', '20251026'), ('2025/10/26', '20251026')):
        chk(dh.accident_date8(_s, today=_T) == _w, f'C12: 事故日「{_s}」→ {dh.accident_date8(_s, today=_T)!r}（{_w!r} のはず）')
    # 添付書類・見積書（reader）の両方で同じ関数を使っている（今日の日付で決まるので、今日から見て 2 年前の令和の年で確かめる）
    _y = _dtm.date.today().year - 2
    _rw = f'{_y - 2018:02d}.03.15'
    chk(dh.insurance_hint_from_doc({'accident_date': _rw}).get('accident_date') == f'{_y}0315',
        f"C12: 添付書類の事故日「{_rw}」を令和で読んでいない: {dh.insurance_hint_from_doc({'accident_date': _rw})}")
    _src = open(os.path.join(ROOT, 'neo_skill', 'reader.py'), encoding='utf-8').read()
    chk('_dh.accident_date8(' in _src, 'C12: 見積書の事故日（reader）が 2 桁の年を西暦と令和の両方で読んでいない')
    # E9: 排ガス記号は最後の字が英字。「GB8-WHCHS6A」（型式＋類別記号）の「GB8」は外さない（外すと resolver が引けない）
    chk(dh.strip_emission_prefix('GB8-WHCHS6A') == 'GB8-WHCHS6A' and dh.strip_emission_prefix('GH-GD1') == 'GD1'
        and dh.strip_emission_prefix('5BA-KSP210') == 'KSP210',
        f"E9: 型式の頭の外し方が違う: {dh.strip_emission_prefix('GB8-WHCHS6A')!r} / {dh.strip_emission_prefix('GH-GD1')!r}")
    # 2010 年の常用漢字で CP932 に無い字（剝・𠮟・塡・頰）は JIS の字に（ベタ打ちで「?離」、スキル経路で DB に Unicode のままだった）
    import neo_header as _nh
    chk(dh._clean('剝離 太郎') == '剥離 太郎' and _nh.encode_cp932w('剝離') == '剥離'.encode('cp932')
        and dh._clean('\U00020B9F責') == '叱責' and dh._clean('頰') == '頬' and dh._clean('補塡') == '補填',
        f"C5: 常用漢字の CP932 に無い字を置き換えていない: {dh._clean('剝離 太郎')!r}")
    # 車台番号の後ろの注記（打刻）は外す（車台番号に括弧は入らない）
    chk(dh.serial_clean('KSP210-0057662（打刻）') == 'KSP210-0057662'
        and dh.vehicle_hint({'car_serial_no': 'KSP210-0057662(打刻)'}).get('serial_no') == 'KSP210-0057662'
        and dh.vehicle_info_for_legacy({'car_serial_no': 'KSP210-0057662（打刻）'}).get('car_serial_no') == 'KSP210-0057662',
        f"C3: 車台番号の注記の括弧を外していない: {dh.serial_clean('KSP210-0057662（打刻）')!r}")
    # すべて 0 の型式指定・類別は空（スキル経路は '00000'、ベタ打ちは空で、経路によって違った）
    _vh0 = dh.vehicle_hint({'car_model_designation': 0, 'car_category_number': '0000', 'car_model': 'KSP210'})
    _lg0 = dh.vehicle_info_for_legacy({'car_model_designation': '0', 'car_category_number': '0000'})
    chk(not _vh0.get('desig') and not _vh0.get('category') and not _lg0.get('car_model_designation') and not _lg0.get('car_category_number'),
        f"C17: すべて 0 の型式指定・類別を番号として書いている: {_vh0} / {_lg0.get('car_model_designation')!r}")
    chk(dh.vehicle_hint({'car_model_designation': '2', 'car_category_number': '15'}).get('desig') == '00002',
        'C17: 0 以外の短い型式指定まで空にしている')
    # C17: 車検証の数値の欄が小数・真偽値で来ても、桁の欄を壊さない
    _v17 = dh.vehicle_info_for_legacy({'car_model_designation': 2.0, 'car_category_number': 15.0, 'kilometer': 12345.0,
                                       'car_serial_no': True})
    chk(_v17.get('car_model_designation') == '00002' and _v17.get('car_category_number') == '0015' and not _v17.get('car_serial_no'),
        f"C17: 小数の型式指定・類別を壊している: {_v17.get('car_model_designation')!r} / {_v17.get('car_category_number')!r}")
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'C: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-300:])

# ── A1: CSV の「部品差額」「工賃差額」行（振る舞い・両方向） ───────────────────
try:
    _H = '品名,区分,数量,部品金額,工賃,部品コード\n'
    # 金額のある見出し語だけの行は、明細なのか申告なのか決められないので止める（以前は申告として黙って捨て、3,300 円少ない NEO）
    for _nm, _row in (('部品差額', '部品差額,,1,3000,0,'), ('工賃差額', '工賃差額,,1,0,-2000,'), ('金額差異', '金額差異,,1,500,0,')):
        _it, _nt = app.parse_csv_to_items(_H + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,8000,52119-X\n' + _row + '\n', return_notes=True)
        chk(not _it and any(str(n).startswith('❌') and _nm in str(n) for n in _nt),
            f'A1: 金額のある「{_nm}」の行を黙って捨てている／明細にしている: {len(_it)} 行 {[str(n)[:40] for n in _nt]}')
    # 従来の申告（品名に同じ金額「部品相違 1,200円」＋部品金額 1,200）は申告のまま（suite の D7）
    _it, _nt = app.parse_csv_to_items(_H + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,8000,52119-X\n"部品相違 1,200円",,,1200,\n', return_notes=True)
    chk(len(_it) == 1 and any(app._is_ai_diff_text(n) for n in _nt), f'A1: 従来の申告「部品相違 1,200円」を申告として扱っていない: {len(_it)} {_nt}')
    # 申告として扱うのは、金額の欄がちょうど 1 つ・見出しの側のときだけ（両方に同じ額・逆の欄は止める。Codex 3 周目）
    for _row, _want_decl in (('"部品相違 1,200円",,,1200,1200', False), ('"部品相違 1,200円",,,,1200', False),
                             ('"工賃相違 800円",,,,800', True), ('"合計相違 500円",,,500,', True)):
        _it, _nt = app.parse_csv_to_items(_H + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,8000,52119-X\n' + _row + '\n', return_notes=True)
        _is_decl = len(_it) == 1 and any(app._is_ai_diff_text(n) for n in _nt)
        _stopped = not _it and any(str(n).startswith('❌') for n in _nt)
        chk(_is_decl if _want_decl else _stopped,
            f'A1: 「{_row}」を{"申告として扱っていない" if _want_decl else "申告として捨てている（止めるはず）"}: {len(_it)} 行 {[str(n)[:30] for n in _nt]}')
    # 符号付きの申告（「部品相違 -1,200円」＋部品金額 -1200）も申告（明細にすると金額が変わる）。符号付きの「差額」は止める
    _it, _nt = app.parse_csv_to_items(_H + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,8000,52119-X\n"部品相違 -1,200円",,,-1200,\n', return_notes=True)
    chk(len(_it) == 1 and any(app._is_ai_diff_text(n) for n in _nt), f'A1: 符号付きの申告を明細として取り込んでいる: {len(_it)} {_nt}')
    _it, _nt = app.parse_csv_to_items(_H + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,8000,52119-X\n"部品差額 ▲3,000",,1,-3000,0,\n', return_notes=True)
    chk(not _it and any(str(n).startswith('❌') for n in _nt), f'A1: 符号付きの「部品差額」を止めていない: {len(_it)} {[str(n)[:30] for n in _nt]}')
    # 金額の無い申告は申告のまま・品名に一言足した明細は明細として取り込む
    _it, _nt = app.parse_csv_to_items(_H + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,8000,52119-X\n部品差額,,1,0,0,\n', return_notes=True)
    chk(len(_it) == 1 and any(app._is_ai_diff_text(n) for n in _nt), f'A1: 金額の無い「部品差額」を申告として扱っていない: {len(_it)} {_nt}')
    _it, _nt = app.parse_csv_to_items(_H + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,8000,52119-X\n部品差額 調整分,,1,3000,0,\n', return_notes=True)
    chk(len(_it) == 2, f'A1: 品名に一言足した「部品差額 調整分」を明細として取り込んでいない: {len(_it)} {_nt}')
    # 「差額・差異」は品名と金額の欄が同じ金額でも止める（申告として扱うのは指示文が書かせる「〜相違」の形だけ。Codex 2 周目 P1）
    for _row in ('"部品差額 3,000",,1,3000,0,', '"工賃差異 2,000円",,1,0,2000,'):
        _it, _nt = app.parse_csv_to_items(_H + 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,8000,52119-X\n' + _row + '\n', return_notes=True)
        chk(not _it and any(str(n).startswith('❌') for n in _nt),
            f'A1: 品名と同じ金額の「{_row.split(",")[0]}」を申告として捨てている: {len(_it)} 行 {[str(n)[:30] for n in _nt]}')
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'A1: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-300:])

# ── E1・E2・E7: 車種の手がかりの突き合わせ（vendor を呼ぶ口を作り物にして、アプリの判定だけを見る） ─────────
try:
    from neo_skill import reader as _rdr
    _orig_runner = _rdr._runner
    _NM = {'W69': 'トヨタ C-HRハイブリッド', 'Y15': 'トヨタ クラウンエステート', 'A11': 'ダイハツ ハイゼットカーゴ', 'A12': 'ダイハツ アトレー'}
    _calls = []

    def _fake(results):
        def _r(op, **kw):
            _calls.append((op, kw.get('variants')))
            if op != 'vehicle_crosscheck':   # 呼ぶ操作の名前を間違えたら、本物の口と同じく落とす（Codex 4 周目）
                raise RuntimeError(f'unknown op: {op!r}')
            return {'results': results[:len(kw.get('variants') or [])], 'names': _NM}
        return _r
    _RD = {'vehicle': {'model_code': '6AA-ZYX11', 'serial_no': 'ZYX11-1', 'desig': '10417', 'category': '0005'}}
    try:
        # 型式指定・類別の車と型式・車台番号の車が別（1 桁の読み違い）→ 合格にしない理由
        _rdr._runner = _fake([{'by_desig': ['Y15'], 'by_serial': ['W69']}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {}, 'X')
        chk(_vx['conflict'] and 'クラウンエステート' in _vx['conflict'] and 'C-HR' in _vx['conflict'] and _vx['reading'] is _RD,
            f"E1: 型式指定・類別と型式・車台番号が別の車を指すのに止めていない: {_vx['conflict'][:80]!r}")
        # 車検証の値に替えれば 2 つが同じ車を指す → そちらを使い、止めない（値そのものは注意に出さない）
        _rdr._runner = _fake([{'by_desig': ['Y15'], 'by_serial': ['W69']}, {'by_desig': ['W69'], 'by_serial': ['W69']}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {'desig': '19417'}, 'X')
        chk(not _vx['conflict'] and _vx['reading'] is not _RD and _vx['reading']['vehicle']['desig'] == '19417'
            and _RD['vehicle']['desig'] == '10417' and '19417' not in _vx['note'] and _vx['cands'] == ['W69'],
            f"E1: 車検証の値で車種が決まるのに使っていない／元の reading を書き換えた: {_vx['note'][:80]!r}")
        # 車検証の値でも決まらない → 止める
        _rdr._runner = _fake([{'by_desig': ['Y15'], 'by_serial': ['W69']}, {'by_desig': ['A11'], 'by_serial': ['W69']}])
        chk(app._p2n_vehicle_crosscheck(_RD, {'desig': '12345'}, 'X')['conflict'], 'E1: 車検証の値でも決まらないのに止めていない')
        # 型式指定・類別が無く、型式・車台番号だけでは 2 車種 → 止める（E2）
        _rdr._runner = _fake([{'by_desig': [], 'by_serial': ['A11', 'A12']}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {}, 'X')
        chk(_vx['conflict'] and 'アトレー' in _vx['conflict'], f"E2: 候補が 2 車種なのに止めていない: {_vx['conflict'][:80]!r}")
        # 2 つが同じ車 → 止めない。書き方の違い（先頭の 0・全角）は食い違いにしない
        _calls.clear()
        _rdr._runner = _fake([{'by_desig': ['W69'], 'by_serial': ['W69']}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {'desig': '１０４１７', 'model_code': 'ZYX11'}, 'X')
        chk(not _vx['conflict'] and not _vx['note'] and len(_calls[-1][1]) == 1,
            f"E1: 書き方の違いだけで食い違いにしている／同じ車で止めている: {_vx}")
        # 見積書の値が読み違いでも 1 つの車にきれいに当たる（片方だけ）とき、車検証の値が 2 つとも同じ車なら車検証を使う（Codex P1）
        _rdr._runner = _fake([{'by_desig': ['Y15'], 'by_serial': []}, {'by_desig': ['W69'], 'by_serial': ['W69']}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {'desig': '19417'}, 'X')
        chk(not _vx['conflict'] and _vx['cands'] == ['W69'] and _vx['reading']['vehicle']['desig'] == '19417',
            f"E1: 見積書の値が片方だけで別の車に当たると、車検証の値と比べずに通している: {_vx['cands']} {_vx['conflict'][:60]!r}")
        # 見積書も車検証もそれぞれ 2 つの手がかりが揃って別の車 → 添付が別の車の疑いで止める
        _rdr._runner = _fake([{'by_desig': ['Y15'], 'by_serial': ['Y15']}, {'by_desig': ['W69'], 'by_serial': ['W69']}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {'desig': '19417', 'serial_no': 'ZYX11-2'}, 'X')
        chk(_vx['conflict'] and '添付した車検証' in _vx['conflict'] and 'ZYX11-2' not in _vx['conflict'],
            f"E1: 見積書と車検証がそれぞれ別の車で揃っているのに止めていない: {_vx['conflict'][:60]!r}")
        # 見積書が 2 つ揃い、車検証は片方だけ別の車 → 見積書のまま・注意を残す
        _rdr._runner = _fake([{'by_desig': ['W69'], 'by_serial': ['W69']}, {'by_desig': ['Y15'], 'by_serial': []}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {'desig': '10418'}, 'X')
        chk(not _vx['conflict'] and _vx['cands'] == ['W69'] and _vx['reading'] is _RD and '見積書の値' in _vx['note'],
            f"E1: 見積書の値が 2 つ揃っているのに車検証の片方の値で止めた／注意が無い: {_vx}")
        # 両方とも片方だけで別の車 → 止める
        _rdr._runner = _fake([{'by_desig': ['Y15'], 'by_serial': []}, {'by_desig': ['W69'], 'by_serial': []}])
        chk(app._p2n_vehicle_crosscheck(_RD, {'desig': '19417'}, 'X')['conflict'],
            'E1: 見積書と車検証が片方ずつ別の車を指すのに止めていない')
        # 同じ車でも、型式指定・類別の違いでグレード・ボディ・駆動が変わる → 止める（車検証があるときだけ見分けられる）
        _rdr._runner = _fake([{'by_desig': ['S89'], 'by_serial': ['S89'], 'desig_recs': ['S89|02|20|A|B|False']},
                              {'by_desig': ['S89'], 'by_serial': ['S89'], 'desig_recs': ['S89|02|10|A|B|False']}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {'category': '0001'}, 'X')
        chk(_vx['conflict'] and 'グレード・ボディ・駆動' in _vx['conflict'],
            f"E1: 見積書と車検証で類別が違い同じ車の別のボディ・駆動になるのに止めていない: {_vx['conflict'][:60]!r}")
        _rdr._runner = _fake([{'by_desig': ['S89'], 'by_serial': ['S89'], 'desig_recs': ['S89|02|10|A|B|False']},
                              {'by_desig': ['S89'], 'by_serial': ['S89'], 'desig_recs': ['S89|02|10|A|B|False']}])
        chk(not app._p2n_vehicle_crosscheck(_RD, {'model_code': 'ZYX10'}, 'X')['conflict'],
            'E1: 型式指定・類別の指す行が同じなのに止めた')
        # 同じ車が年式で 2 つの車種コードに分かれている車（N-BOX SLASH の J84／J89 など）は、正しい入力でも 2 コード返る → 止めない
        _rdr._runner = _fake([{'by_desig': ['J84', 'J89'], 'by_serial': ['J84', 'J89']}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {}, 'X')
        chk(not _vx['conflict'] and sorted(_vx['cands']) == ['J84', 'J89'],
            f"E2: 型式指定・類別で引けているのに候補 2 車種で止めている（年式で分かれた同じ車）: {_vx['conflict'][:60]!r}")
        # 見積書の型式指定が読み違いで KA81 に当たらず、車検証なら両方当たる（候補の車は同じ）→ 車検証の値を使う
        _rdr._runner = _fake([{'by_desig': [], 'by_serial': ['W69']}, {'by_desig': ['W69'], 'by_serial': ['W69']}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {'desig': '19417'}, 'X')
        chk(not _vx['conflict'] and _vx['reading'] is not _RD and _vx['reading']['vehicle']['desig'] == '19417' and _vx['note'],
            f'E1: 車検証だけが 2 つの手がかりで同じ車を指すのに、値を使っていない: {_vx}')
        # 両方とも片方だけで同じ車 → そのまま（注意も出さない）
        _rdr._runner = _fake([{'by_desig': ['W69'], 'by_serial': []}, {'by_desig': ['W69'], 'by_serial': []}])
        _vx = app._p2n_vehicle_crosscheck(_RD, {'category': '0006'}, 'X')
        chk(not _vx['conflict'] and not _vx['note'] and _vx['cands'] == ['W69'], f'E1: 同じ車を指すのに止めた・注意を出した: {_vx}')
        # 引けなかった（vendor の失敗）→ 止めずに注意だけ
        def _boom(op, **kw):
            raise RuntimeError('x')
        _rdr._runner = _boom
        _vx = app._p2n_vehicle_crosscheck(_RD, {}, 'X')
        chk(not _vx['conflict'] and _vx['note'], f'E1: 突き合わせができないときの扱いが違う: {_vx}')
    finally:
        _rdr._runner = _orig_runner
    # 生成のあと: 報告文の車種が手がかりの車に入っていなければ合格にしない（汎用車種も。E7）
    _line = '# x\n- 車両: ﾄﾖﾀ C-HR / {} 年式 01 ボディ 10 グレード A FVA B 色 070（{}）装備 []\n'
    _vx = {'cands': ['W69'], 'names': _NM, 'conflict': ''}
    chk(app._p2n_vehicle_verdict(_vx, _line.format('W69', 'confirmed')) == '', 'E1: 手がかりどおりの車種で合格にしていない')
    chk('Y15' in app._p2n_vehicle_verdict(_vx, _line.format('Y15', 'confirmed')), 'E1: 手がかりと違う車種の NEO を合格にしている')
    chk('汎用' in app._p2n_vehicle_verdict(_vx, _line.format('Z10', 'generic')), 'E7: ADDATA に載っている車を汎用車種で作った NEO を合格にしている')
    chk(app._p2n_vehicle_verdict({'conflict': 'X'}, '') == 'X', 'E1: 作る前に見つけた食い違いを生成のあとで落としている')
    # p2n_make に組み込んである（作る前に突き合わせ・作ったあとに車種を見る）
    # 画面: 合格の枝で車検証の注意を出す・読み取りが不合格の枝でも車種の食い違いを出す（報告文の中だけだと気づけない）
    chk('''if _p2n_res.get('vehicle_note'):''' in SRC and '''st.warning("🚗 " + _md_literal(_p2n_res['vehicle_note']))''' in SRC,
        'E1: 合格の画面に車検証の注意（vehicle_note）を出していない')
    _rdsec = SRC.split("elif _p2n_res.get('stage') == 'read' and not _p2n_rd.get('ok'):", 1)
    # 条件の行そのものを見る（本文に文字列が残っていても、条件を False にすれば出なくなるため）
    chk(len(_rdsec) > 1 and "if _p2n_res.get('vehicle_conflict'):" in _rdsec[1][:400]
        and "st.error(\"🚗 \" + _md_literal(_p2n_res['vehicle_conflict']))" in _rdsec[1][:600],
        'E1: 読み取りが不合格の枝で、車種の食い違いを画面に出していない')
    chk('_vx = _p2n_vehicle_crosscheck(reading, out.get(\'vehicle_hint\'), addata_root)' in SRC
        and SRC.count('_p2n_vehicle_verdict(_vx, ') >= 2 and "out['vehicle_hint'] = dict(vehicle_hint or {})" in SRC,
        'E1: p2n_read / p2n_make に車種の突き合わせが組み込まれていない')
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'E1: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-300:])

# ── E1: 本物の vendor の口（_runner.py の vehicle_crosscheck）。NEO_TEST_ADDATA=<ADDATA> を指定したときだけ
#    （ADDATA の版に左右されないよう、特定の車種コードではなく「車種コード・行・車名がそろって返る」形で見る。Codex 4 周目） ───
_AD_REAL = os.environ.get('NEO_TEST_ADDATA') or ''
if _AD_REAL and os.path.isfile(os.path.join(_AD_REAL, 'COM', 'KA81.DB')) and os.path.isfile(os.path.join(_AD_REAL, 'COM', 'KA06_ALL.DB')):
    try:
        from neo_skill import reader as _rdr6
        # 型式指定・類別は車種の番号（個人の情報ではない）。車台番号は架空
        _r6 = _rdr6._runner('vehicle_crosscheck', addata_root=_AD_REAL, timeout=180,
                            variants=[{'model_code': '6AA-ZYX11', 'serial_no': 'ZYX11-0000001', 'desig': '19417', 'category': '0005'},
                                      {'model_code': '', 'serial_no': '', 'desig': '10417', 'category': '0005'}])
        _o6 = (_r6.get('results') or [{}])[0]
        _codes6 = list(_o6.get('by_desig') or [])
        _recs6 = list(_o6.get('desig_recs') or [])
        chk(len(_r6.get('results') or []) == 2 and _codes6 and all(len(c) == 3 for c in _codes6)
            and _recs6 and all(str(x).split('|')[0] in _codes6 and len(str(x).split('|')) == 6 for x in _recs6)
            and all((_r6.get('names') or {}).get(c) for c in _codes6) and isinstance(_o6.get('by_serial'), list),
            f"E1: 本物の vendor の口が型式指定・類別の車・行・車名を返さない: {str(_r6)[:160]}")
    except Exception as e:  # noqa: BLE001
        chk(False, f'E1: 本物の vendor の口を呼べない（{type(e).__name__}: {str(e)[:120]}）')
else:
    print('reg_hunt4: NEO_TEST_ADDATA が無いので、本物の vendor の口の確かめは飛ばしました（合格には数えない）')

# ── E1（p2n_make の中で効いているか。make_neo と vendor の口を偽物にして p2n_make を実際に通す。Codex テスト 2 周目） ─────
try:
    import json as _jsn
    from neo_skill import maker as _mk5, reader as _rdr5
    _om, _orr = _mk5.make_neo, _rdr5._runner
    _NM5 = {'W69': 'トヨタ C-HRハイブリッド', 'Y15': 'トヨタ クラウンエステート'}
    _seen5 = {}

    def _fake_make(car_code):
        def _mkf(case_dir, name, **kw):
            _seen5['reading'] = _jsn.load(open(os.path.join(case_dir, 'reading.json'), encoding='utf-8'))
            neo = os.path.join(case_dir, f'{name}.neo')
            open(neo, 'wb').write(b'NEO-BYTES')
            rv = os.path.join(case_dir, f'{name}_確認箇所.csv')
            open(rv, 'w', encoding='utf-8-sig').write('col\r\nx\r\n')
            rp = os.path.join(case_dir, 'report.md')
            open(rp, 'w', encoding='utf-8').write('# 報告\n- 車両: ﾃｽﾄ / ' + car_code + ' 年式 01 ボディ 10 グレード A FVA B 色 070（confirmed）装備 []\n')
            return _mk5.MakeResult(True, 0, '見積書合計との一致: OK', case_dir, name, neo_path=neo, review_path=rv, report_path=rp,
                                   match_line='見積書合計との一致: OK')
        return _mkf

    def _run5(results, car_code, hint):
        def _r5(op, **kw):
            if op != 'vehicle_crosscheck':
                raise RuntimeError(f'unknown op: {op!r}')
            return {'results': results[:len(kw.get('variants') or [])], 'names': _NM5}
        _rdr5._runner = _r5
        _mk5.make_neo = _fake_make(car_code)
        _cd = tempfile.mkdtemp(prefix='reg_hunt4_e1m_')
        _ad = tempfile.mkdtemp(prefix='reg_hunt4_e1a_')
        _rd = {'vehicle': {'model_code': '6AA-ZYX11', 'serial_no': 'ZYX11-1', 'desig': '10417', 'category': '0005'}}
        _mk5.write_reading(_cd, _rd)
        try:
            return app.p2n_make({'case_dir': _cd, 'reading': _rd, 'vehicle_hint': hint, 'app_notes': [], 'read': {'ok': True, 'fails': []}},
                                addata_root=_ad), os.path.isdir(_cd)
        finally:
            import shutil as _sh5
            _sh5.rmtree(_cd, ignore_errors=True)
            _sh5.rmtree(_ad, ignore_errors=True)
    try:
        # 別の車（1 桁の読み違い）: vendor が合格にしても、確認用の NEO にして理由を先頭に
        _o, _left = _run5([{'by_desig': ['Y15'], 'by_serial': ['W69']}], 'Y15', {})
        chk(not _o.get('ok') and _o.get('vehicle_conflict') and _o.get('unverified_neo') == b'NEO-BYTES'
            and not _o.get('neo_bytes') and str(_o.get('report_md') or '').startswith(app._UNVERIFIED_HEAD) and not _left,
            f"E1: p2n_make が別の車の NEO を合格にした／確認用にしていない: ok={_o.get('ok')} conflict={bool(_o.get('vehicle_conflict'))}")
        # 車検証の値で決まる: 書き戻した reading で作り、合格
        _o, _left = _run5([{'by_desig': ['Y15'], 'by_serial': ['W69']}, {'by_desig': ['W69'], 'by_serial': ['W69']}], 'W69', {'desig': '19417'})
        chk(_o.get('vehicle_note') and '車検証' in str(_o.get('vehicle_note')),
            f"E1: 車検証の値を使ったことを画面に出せていない（vehicle_note）: {_o.get('vehicle_note')!r}")
        chk(_o.get('ok') and _seen5['reading']['vehicle']['desig'] == '19417' and any('車検証' in n for n in (_o.get('app_notes') or [])),
            f"E1: p2n_make が車検証の値を reading.json に書き戻して作っていない: ok={_o.get('ok')} desig={_seen5.get('reading', {}).get('vehicle', {}).get('desig')}")
        # 手がかりどおりの車 → 合格のまま
        _o, _left = _run5([{'by_desig': ['W69'], 'by_serial': ['W69']}], 'W69', {})
        chk(_o.get('ok') and _o.get('neo_bytes') == b'NEO-BYTES' and not _o.get('vehicle_conflict'),
            f"E1: 手がかりどおりの車種で p2n_make が合格にしない: {_o.get('error')}")
        # make_neo が検算に通らなかった通常の道でも、渡す報告文の先頭に確認用の見出し（Codex 4 周目）
        _mk_ok = _fake_make

        def _fake_ng(car_code):
            _f = _mk_ok(car_code)

            def _mkf(case_dir, name, **kw):
                r = _f(case_dir, name, **kw)
                ng = os.path.join(case_dir, f'{name}.ng.neo')
                os.replace(r.neo_path, ng)
                return _mk5.MakeResult(False, 1, '不合格: 見積書合計と合わない', case_dir, name, ng_neo_path=ng,
                                       review_path=r.review_path, report_path=r.report_path, reasons=['不合格: 見積書合計と合わない'],
                                       match_line='見積書合計との一致: NG')
            return _mkf
        _fake_make_saved = _fake_make
        try:
            _fake_make = _fake_ng   # _run5 が使う
            _o, _left = _run5([{'by_desig': ['W69'], 'by_serial': ['W69']}], 'W69', {})
        finally:
            _fake_make = _fake_make_saved
        chk(not _o.get('ok') and _o.get('unverified_neo') == b'NEO-BYTES' and str(_o.get('report_md') or '').startswith(app._UNVERIFIED_HEAD)
            and '見積書合計と合わない' in str(_o.get('report_md') or ''),
            f"P3: 検算に通らなかった通常の道で渡す報告文に確認用の見出しが無い: {str(_o.get('report_md') or '')[:60]!r}")
        # 生成した車種が手がかりと違う（汎用車種で作られた）→ 合格にしない（E7）
        _o, _left = _run5([{'by_desig': ['W69'], 'by_serial': ['W69']}], 'Z10', {})
        chk(not _o.get('ok') and '汎用' in str(_o.get('vehicle_conflict') or ''), f"E7: 汎用車種で作られた NEO を p2n_make が合格にした: {_o.get('ok')}")
    finally:
        _mk5.make_neo, _rdr5._runner = _om, _orr
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'E1: p2n_make の確かめが途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-400:])

# ── E6: 見積書から読んだ初度登録 ───────────────────────────────────────────────
try:
    from neo_skill import reader as _rdr2
    for _raw, _want in (('令和元年5月', 'R1.5'), ('R元.5', 'R1.5'), ('R1/5', 'R1.5'), ('r2.5', 'R2.5'), ('H27.5', 'H27.5'),
                        ('2020年5月登録', '2020年5月登録'), ('不明', None),
                        # vendor が読む形でも、ありえない年・月は捨てる（vendor は R0 を 2018 年と読む。Codex 2 周目）
                        ('R0.5', None), ('令和0年5月', None), ('2020年13月登録', None), ('R2.13', None)):
        _o = {'vehicle': {'reg_date': _raw}}
        _rdr2._normalise_values(_o, [])
        chk(_o['vehicle'].get('reg_date') == _want, f"E6: 初度登録「{_raw}」→ {_o['vehicle'].get('reg_date')!r}（{_want!r} のはず）")
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'E6: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-300:])

# ── B4: 部品コードの印字が無い見積の作業の見出し行 ─────────────────────────────
try:
    from neo_skill import reader as _rdr3
    # 短縮記法の列: code|name|method|parts_no|index|qty|price|wage|flags|comment
    _pg = [{'blocks': [{'rows': ['|左Frｻｽﾍﾟﾝｼｮﾝ脱着分解|||5.5|||||', '|アライメント調整||||||15000|M|',
                                  {'name': 'パネル計測', 'method': '', 'index': 0.8, 'manual': True},
                                  '|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ|取替||||45000||M|', '|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ|脱着||0.5||||M|']}]}]
    _o, _why = _rdr3._manual_rows_guard({'format': 'F', 'vehicle': {'model_code': 'GB8', 'serial_no': 'GB8-0000001'}}, _pg)
    _rs = _o[0]['blocks'][0]['rows']
    chk('M' in _rs[0].split('|')[8] and 'M' in _rs[1].split('|')[8] and _rs[2].get('manual') is True,
        f'B4: 区分が空欄で工賃・指数だけの行（作業の見出し）を手入力にそろえていない: {_rs[:3]}')
    chk('M' not in _rs[3].split('|')[8] and 'M' not in _rs[4].split('|')[8],
        f'B4: 部品代のある行・区分のある行の M を外していない: {_rs[3:]}')
    chk(_why and '作業の見出し' in _why, f'B4: 手入力にそろえた旨の注意が無い: {_why}')
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'B4: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-300:])

# ── A3: ベタ打ちの一発生成も、品名を半角にそろえて NEO に書く ───────────────────
try:
    import sqlite3 as _sq
    import pdf_to_neo_pipeline as _pipe
    _neo = _pipe.build_neo_mode_a([{'page': 1, 'name': 'フロントバンパリインフォースメント', 'work_code': '取替', 'quantity': 1,
                                    'parts_amount': 23000, 'wage': 0, 'part_no': '', 'index_value': ''}], {}, customer_info={})
    _ck = app.find_real_cks(_neo)
    _fs = app.extract_files(app.decompress_neo(_neo, _ck), app.parse_entries(_neo, _ck[0])[1])
    _c = _sq.connect(':memory:')
    _c.deserialize(_fs['AnSMB.txt'])
    _nm = _c.execute('SELECT PartsName FROM ERParts ORDER BY LineNo').fetchone()[0]
    _c.close()
    chk(_nm == 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟﾘｲﾝﾌｫｰｽﾒﾝﾄ', f'A3: ベタ打ちの一発生成が品名を全角のまま書いている（24 バイトで切れる）: {_nm!r}')
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'A3: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-300:])

# ── 塗装材料費の注意: 合計欄の数が 2 つある文字列をつながない・桁あふれで落ちない ─────────────
try:
    from neo_skill import reader as _rdr4

    def _pmn(h):
        try:
            return _rdr4._paint_material_note(h)
        except Exception as e:  # noqa: BLE001  例外も「狙いの文言」で落とす
            return f'（例外 {type(e).__name__}）'
    _pc = _pmn({'totals': {'paint': '76,000（税込 83,600）', 'material': '5,000'}})
    chk(_pc is None, f'塗装材料費: 「76,000（税込 83,600）」の数字をつないでいる: {_pc}')
    chk(_pmn({'paint': {'total': 10 ** 400, 'material': 5000}}) is None and _pmn({'paint': {'total': float('inf'), 'material': 5000}}) is None
        and _pmn({'totals': {'paint': None, 'material': ''}}) is None,
        '塗装材料費: 桁あふれ・inf・空で注意の組み立てが落ちる')
    _pn = _pmn({'totals': {'paint': '¥76,000', 'material': '76,000円'}})
    chk(_pn and '76,000 円' in _pn and '100.0%' in _pn, f'塗装材料費: 数が 1 つの合計欄を読めていない: {_pn}')
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'塗装材料費: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-300:])

# ── A2: 区分確認は NFKC でそろえた品名で探す ──────────────────────────────────
try:
    _al = app.check_parts_labor_classification([{'name': 'ｳﾚﾀﾝﾊﾞﾝﾊﾟ補修材', 'method': '', 'parts_amount': 0, 'wage': 3000},
                                                {'name': 'ﾘﾔﾊﾞﾝﾊﾟ', 'method': 'ﾀﾞｯﾁｬｸ', 'parts_amount': 5000, 'wage': 2000}])
    chk(any(a['flag'] == 'labor_in_parts' and 'ｳﾚﾀﾝ' in a['name'] for a in _al),
        f'A2: 半角カナの材料系品名（ｳﾚﾀﾝ）の区分確認が出ない: {[a["flag"] for a in _al]}')
except Exception as e:  # noqa: BLE001
    import traceback
    chk(False, f'A2: 途中で例外（{type(e).__name__}: {e}）' + traceback.format_exc()[-300:])

print('reg_hunt4:', 'all ok' if not FAILS else f'{len(FAILS)} 件が不合格')
for m in FAILS:
    print('  -', m)
sys.exit(1 if FAILS else 0)
