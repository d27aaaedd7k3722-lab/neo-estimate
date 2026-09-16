# -*- coding: utf-8 -*-
"""reg_reader.py — 「読む」段（neo_skill.reader）の検算・読み直しループを、LLM を差し替えて確かめる（API 不要・課金なし）。

  1. 1 回目のページ写しがわざと検算に落ちる（rows_printed が違う）→ FAIL の文言を渡して読み直す → 2 回目で合格
  2. 全ページ合格 → merge → 合計欄の検算 OK（reading.json は reader が書かず、make_neo が pages/ から作る）
  3. その pages/ で vendor の make_neo が合格し、NEO と確認箇所シートが組で出る
  4. 合計欄が合わないまま読み直しても直らないときは ok=False で止まり、NEO を作らない（合計合わせをしない）
  5〜9. 壊れた JSON の言い直し / 読み直し上限 / 保険情報の補い / 作業フォルダの後始末 / プロセスを汚さないこと
  10. 明細の無いページ（表紙・計算書）は blocks を空ブロックにして検算を通す（vendor の validate_page は空リストを FAIL にする）
  11. 不合格のとき、人が直すための一式（pages/・reading.json）を zip にできる
  13. 読み手が index_policy=manual と書いても区分がコグニ語彙（または区分の印字なし）なら auto に戻す（「部品」があれば残す）

    python tests/reg_reader.py
終了コード: 0 全部 OK / 1 失敗
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
from neo_skill import llm as llm_mod  # noqa: E402
from neo_skill import maker, reader  # noqa: E402

# 検証用の見積（実在の人物・車両ではない）。N BOX JF1 は ADDATA に載っている車なので照合まで通る
HEADER = {
    'source': 'test.pdf 書式B', 'issuer': 'テスト工場', 'est_date': '20260913', 'format': 'B',
    'vehicle': {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': 'YR586P'},
    'customer': {'name': 'ｹﾝｼｮｳ ﾀﾛｳ'}, 'insurance': {'company': 'テスト損保'},
    'labor_rate': 8000, 'index_policy': 'auto',
    'totals': {'parts': 45000, 'wage': 16000, 'taxable': 61000, 'tax': 6100, 'total': 67100},
}
ROWS = ['|Rrﾊﾞﾝﾊﾟ|取替|71501-TY0-000ZZ|1.00|1|45000|8000||',
        '|Rrﾊﾞﾝﾊﾟ|脱着||1.00|1||8000||']
PAGE_OK = {'page': 1, 'rows_printed': 2, 'subtotal': {'parts': 45000, 'wage': 16000}, 'marks': {},
           'blocks': [{'title': 'リヤバンパ', 'rows': ROWS}]}
PAGE_BAD = dict(PAGE_OK, rows_printed=3)   # 行数を数え間違えた写し（validate が落とす）
PAGE_BLANK = {'page': 2, 'rows_printed': 0, 'subtotal': {}, 'marks': {}, 'blocks': []}   # 計算書だけのページ（LLM が空リストで返した想定）


class FakeReader:
    """ClaudeReader の代わり。用意した返事を順に返し、受け取った指示文を記録する"""
    def __init__(self, answers):
        self.answers = [json.dumps(a, ensure_ascii=False) if isinstance(a, dict) else a for a in answers]
        self.prompts = []
        self.calls = 0

    def ask(self, system, blocks, **_):
        self.calls += 1
        text = [b['text'] for b in blocks if b.get('type') == 'text'][-1]
        self.prompts.append(text)
        if not self.answers:
            raise llm_mod.LLMError('用意した返事を使い切った')
        return llm_mod.LLMReply(text=self.answers.pop(0), stop_reason='end_turn', input_tokens=10, output_tokens=5)


def blank_pdf(n_pages: int) -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(n_pages):
        c.drawString(72, 720, f'test page {i + 1}')
        c.showPage()
    c.save()
    return buf.getvalue()


def rows_of(rd: dict) -> list:
    return [r for b in (rd or {}).get('blocks', []) for r in b.get('rows', [])]


def neo_if_tables(neo_path: str) -> dict:
    """生成した NEO の AnSvIf の Insurance / FileInfo を読む。vendor の neo_container を使うが、
    このプロセスには import しない（別プロセス。項目 9 の非汚染検査と両立させる）"""
    import subprocess
    from neo_skill import vendor as _v
    code = (
        'import sys, os, json, sqlite3, tempfile\n'
        'sys.path.insert(0, sys.argv[1]); import neo_container as nc\n'
        'neo = open(sys.argv[2], "rb").read(); ck = nc.find_real_cks(neo); raw = nc.decompress_neo(neo, ck)\n'
        'mgmt, entries = nc.parse_entries(neo, ck[0]); fs = nc.extract_files(raw, entries)\n'
        'p = os.path.join(tempfile.gettempdir(), "reg_reader_if_%d.db" % os.getpid()); open(p, "wb").write(fs["AnSvIf0001.sld"])\n'
        'c = sqlite3.connect(p); out = {}\n'
        'for t in ("Insurance", "FileInfo"):\n'
        '    cols = [r[1] for r in c.execute("pragma table_info(%s)" % t)]; out[t] = dict(zip(cols, c.execute("select * from %s" % t).fetchone()))\n'
        'c.close(); os.unlink(p); print("@@" + json.dumps(out, ensure_ascii=False, default=str))\n')
    p = subprocess.run([sys.executable, '-c', code, _v.PIPELINE_DIR, neo_path], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    line = next((l for l in (p.stdout or '').splitlines() if l.startswith('@@')), '')
    return json.loads(line[2:]) if line else {}


def main() -> int:
    fails = []
    pdf = blank_pdf(1)

    # ── 1〜3: 1 回目 FAIL → 読み直しで合格 → merge → 合計欄 OK → make_neo 合格
    case = tempfile.mkdtemp(prefix='reg_reader_')
    fake = FakeReader([HEADER, PAGE_BAD, PAGE_OK])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if not res.ok:
        fails.append(f'読み取りが ok にならない: error={res.error} fails={res.fails()}')
    if fake.calls != 3:
        fails.append(f'LLM の呼び出し回数が 3 でない: {fake.calls}')
    if res.traces and (res.traces[0].attempts != 2 or res.traces[0].first_try_ok):
        fails.append(f'1 ページ目が「1 回目 FAIL → 2 回目 OK」になっていない: {res.traces[0]}')
    if len(fake.prompts) >= 3 and ('不合格' not in fake.prompts[2] or 'rows_printed' not in fake.prompts[2]):
        fails.append('読み直しの指示文に FAIL の文言（rows_printed）が入っていない')
    if len(fake.prompts) >= 3 and '行を消したり金額を動かしたりしない' not in fake.prompts[2]:
        fails.append('読み直しの指示文に「合計合わせをしない」が入っていない')
    # header の指示文: お客様の郵便番号・住所を雛形に持ち、工賃の丸め・消費税の端数処理は推測で書かせない（2026-09-15 スペーシア FAX 見積:
    # 住所が NEO に入らず、tax_round の推測で Setting.tx_ArrangeFlag が変わっていた）
    if fake.prompts:
        _h = fake.prompts[0]
        if '"postal"' not in _h or '"address"' not in _h:
            fails.append('header の雛形に customer.postal / customer.address が無い')
        if 'wage_round / tax_round: 書かない' not in _h:
            fails.append('header の指示文に「wage_round / tax_round は書かない」が無い')
        # 塗装の一式（「塗装費用 ○○円」1 行）を明細と paint の両方に書かせない（2026-09-16: 塗装計が二重に乗って不合格になった）
        if 'paint には**何も書かない**' not in _h:
            fails.append('header の指示文に「明細に 1 行の塗装は paint に書かない」が無い')
    # ページの指示文: 区分の欄には区分語だけ・金額欄の空欄は空のまま（2026-09-16 Gemini: 「修正 基本内」で区分が取替に化け、
    # 金額の印字が無い行に ADDATA の標準価格が入って部品計が +9,400 円になった）
    if len(fake.prompts) >= 2:
        _p = fake.prompts[1]
        if 'method には**区分の語だけ**' not in _p:
            fails.append('ページの指示文に「method には区分の語だけ」が無い')
        if '空のまま' not in _p or '0 や推測値を書かない' not in _p:
            fails.append('ページの指示文に「金額欄の空欄は空のまま」が無い')
        if '同じ金額を header の paint.total にも書かない' not in _p:
            fails.append('ページの指示文に「塗装の一式を paint にも書かない」が無い')
        if '工場（発行元）の住所は issuer' not in _h:
            fails.append('header の指示文に「工場の住所は customer に入れない」が無い')
    if not os.path.isfile(os.path.join(case, 'pages', 'page_1.json')) or not os.path.isfile(os.path.join(case, 'pages', 'header.json')):
        fails.append('pages/ が書かれていない')
    if os.path.isfile(os.path.join(case, 'reading.json')):
        fails.append('reader が reading.json を書いている（make_neo の merge に任せる設計）')
    if rows_of(res.reading) != ROWS:
        fails.append('merge 結果（res.reading）の明細が写しと違う')
    if res.stats.get('first_try_ok_rate') != 0.0 or res.stats.get('retries') != 1:
        fails.append(f'指標がおかしい: {res.stats}')
    if res.ok:
        mk = maker.make_neo(case, 'estimate', no_profile=True)
        if not mk.ok:
            fails.append(f'make_neo が不合格: {mk.error or mk.reasons} / {mk.match_line}\n' + '\n'.join(mk.stdout.splitlines()[-12:]))
        elif not (mk.neo_path and mk.review_path):
            fails.append('NEO と確認箇所シートが組で出ていない')
        if not os.path.isfile(os.path.join(case, 'reading.json')):
            fails.append('make_neo が pages/ から reading.json を作っていない')
    shutil.rmtree(case, ignore_errors=True)

    # ── 13: 読み手が index_policy=manual と書いても、区分がコグニ語彙（取替/脱着 …）か区分の印字が無ければ auto に戻し、注意に出す。
    #        書式 C の目印「部品」があれば読み手の判断を残す（2026-09-15 スペーシア FAX 見積）
    case = tempfile.mkdtemp(prefix='reg_reader_')
    h_manual = json.loads(json.dumps(HEADER))
    h_manual['index_policy'] = 'manual'
    fake = FakeReader([h_manual, PAGE_OK])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if res.header.get('index_policy'):
        fails.append(f"コグニ語彙なのに index_policy=manual が残っている: {res.header.get('index_policy')}")
    if not any('index_policy=manual を外して' in w for w in (res.check.get('warn') or [])):
        fails.append(f'index_policy を外した注意が check.warn に無い: {res.check.get("warn")}')
    try:
        with io.open(os.path.join(case, 'pages', 'header.json'), encoding='utf-8') as fh:
            hj = json.load(fh)
        if hj.get('index_policy'):
            fails.append('pages/header.json に index_policy=manual が残っている')
    except OSError as e:
        fails.append(f'pages/header.json が読めない: {e}')
    if not res.ok:
        fails.append(f'index_policy を外した後に読み取りが ok にならない: {res.fails()}')
    shutil.rmtree(case, ignore_errors=True)
    case = tempfile.mkdtemp(prefix='reg_reader_')
    page_b = json.loads(json.dumps(PAGE_OK))
    page_b['blocks'][0]['rows'] = ['|Rrﾊﾞﾝﾊﾟ||71501-TY0-000ZZ|1.00|1|45000|8000||', '|Rrﾊﾞﾝﾊﾟ|||1.00|1||8000||']   # 区分の列が無い書式
    fake = FakeReader([h_manual, page_b])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if res.header.get('index_policy'):
        fails.append('区分が全行空欄なのに index_policy=manual が残っている')
    if not any('区分の印字が無い' in w for w in (res.check.get('warn') or [])):
        fails.append(f'区分空欄で index_policy を外した注意が無い: {res.check.get("warn")}')
    shutil.rmtree(case, ignore_errors=True)
    case = tempfile.mkdtemp(prefix='reg_reader_')
    page_c = json.loads(json.dumps(PAGE_OK))
    page_c['blocks'][0]['rows'] = ['|Rrﾊﾞﾝﾊﾟ|部品|71501-TY0-000ZZ|1.00|1|45000|8000||', ROWS[1]]
    fake = FakeReader([h_manual, page_c])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if res.header.get('index_policy') != 'manual':
        fails.append('区分「部品」（書式 C）なのに index_policy=manual を外した')
    if any('index_policy=manual を外して' in w for w in (res.check.get('warn') or [])):
        fails.append('書式 C で index_policy を外した注意が出ている')
    shutil.rmtree(case, ignore_errors=True)

    # ── 14: header の est_date / labor_rate / wage_round / tax_round と、page の rows_printed / marks / subtotal の型（バグハント G1/G2/G3/G5）
    h14 = json.loads(json.dumps(HEADER))
    h14['est_date'] = '令和8年9月13日'
    h14['labor_rate'] = '8,000円'
    h14['issuer'] = {'name': 'テスト工場', 'tel': '000-0000-0000'}
    h14['wage_round'] = 100
    h14['tax_round'] = '切り捨て'
    h14['target_total'] = 700000
    h14['discount'] = {'amount': 5000}
    _notes14 = []
    out14 = reader._normalise_header(h14, None, None, None, notes=_notes14)
    if out14.get('est_date') != '20260913':
        fails.append(f"14a: est_date の和暦が YYYYMMDD にならない: {out14.get('est_date')!r}")
    if out14.get('labor_rate') != 8000:
        fails.append(f"14b: labor_rate '8,000円' が 8000 にならない: {out14.get('labor_rate')!r}")
    if not isinstance(out14.get('issuer'), str) or 'テスト工場' not in out14['issuer']:
        fails.append(f"14c: issuer の dict が文字列にならない: {out14.get('issuer')!r}")
    if 'wage_round' in out14 or 'tax_round' in out14 or 'target_total' in out14 or out14.get('discount') != {'amount': 5000} or len(_notes14) != 3:
        fails.append(f"14d: wage_round / tax_round が落ちない・注意が出ない: {out14.keys()} {_notes14}")
    try:
        reader._normalise_header(dict(HEADER, est_date='2026年'), None, None, None)
        fails.append('14e: 8 桁にならない est_date が形の FAIL にならない')
    except reader.PageShapeError:
        pass
    try:   # 年月だけ（日 00）は生成器が date(…, 0) で落ちるので形の FAIL（レビュー 2026-09-15）
        reader._normalise_header(dict(HEADER, est_date='令和8年9月'), None, None, None)
        fails.append('14e2: 年月だけの est_date が形の FAIL にならない')
    except reader.PageShapeError:
        pass
    _ch = {'name': 'ｹﾝｼｮｳ', 'address': '三重県四日市市日永1-1', 'prefecture': '三重県', 'municipality': '四日市市', 'address_other': '日永1-1'}
    _o1 = reader._normalise_header(dict(HEADER, customer={'name': 'ｹﾝｼｮｳ ﾀﾛｳ'}), None, None, _ch)
    if (_o1['customer'].get('municipality'), _o1['customer'].get('address_other')) != ('四日市市', '日永1-1'):
        fails.append(f"14j: 印字に住所が無いのに車検証の構造化住所が渡らない: {_o1['customer']}")
    _o2 = reader._normalise_header(dict(HEADER, customer={'name': 'ｹﾝｼｮｳ ﾀﾛｳ', 'address': '福岡県北九州市小倉北区1-1'}), None, None, _ch)
    if _o2['customer'].get('municipality') or _o2['customer'].get('address') != '福岡県北九州市小倉北区1-1':
        fails.append(f"14k: 印字の住所があるのに車検証の構造化住所を足している: {_o2['customer']}")
    if reader._normalise_header(dict(HEADER, est_date=20260913), None, None, None).get('est_date') != '20260913':
        fails.append('14f: 数値の est_date を 8 桁の文字列にしない')
    for bad_page in ({'page': 1, 'rows_printed': '二', 'blocks': [{'title': '', 'rows': ROWS}]},
                     {'page': 1, 'rows_printed': 2, 'marks': {'$': 'one'}, 'blocks': [{'title': '', 'rows': ROWS}]},
                     {'page': 1, 'rows_printed': 2, 'subtotal': {'parts': [45000]}, 'blocks': [{'title': '', 'rows': ROWS}]}):
        try:
            reader._normalise_page(bad_page, 1)
            fails.append(f'14g: 数値でない rows_printed / marks / subtotal が形の FAIL にならない: {bad_page}')
        except reader.PageShapeError:
            pass
    ok_page = reader._normalise_page({'page': 1, 'rows_printed': '2', 'marks': {'$': '1', '#': None}, 'subtotal': {'parts': '45,000', 'wage': 16000.0},
                                      'blocks': [{'title': '', 'rows': ROWS}]}, 1)
    if ok_page.get('rows_printed') != 2 or ok_page.get('marks') != {'$': 1} or ok_page.get('subtotal') != {'parts': 45000, 'wage': 16000}:
        fails.append(f"14h: 数字の文字列・float・None の正規化が違う: {ok_page.get('rows_printed')!r} {ok_page.get('marks')!r} {ok_page.get('subtotal')!r}")
    if ok_page['blocks'][0]['rows'] is not ok_page['blocks'][0]['rows'] or reader._normalise_page({'page': 2, 'blocks': []}, 2)['blocks'][0]['rows'] is reader.EMPTY_BLOCKS[0]['rows']:
        fails.append('14i: 空ブロックの rows が module 定数を共有している')

    # ── 15: 注記行だけのページは pages/ に残さず、注記は直前の明細ページの末尾へ（バグハント G4）
    note_page = {'page': 2, 'rows_printed': 0, 'subtotal': {}, 'marks': {}, 'blocks': [{'title': '', 'rows': ['|インテリジェントクリアランスソナー|||||||N|']}]}
    hdr15, pages15 = reader._pages_for_merge(HEADER, [PAGE_OK, note_page])
    if len(pages15) != 1 or pages15[0]['page'] != 1:
        fails.append(f'15a: 注記行だけのページが pages/ に残る: {[p.get("page") for p in pages15]}')
    elif not any(reader._is_note_row(r) for r in pages15[0]['blocks'][-1]['rows']):
        fails.append('15b: 注記行が直前の明細ページの末尾に繋がれていない')
    if reader._is_note_row({'name': 'Rrﾊﾞﾝﾊﾟ', 'method': '取替', 'price': 45000, 'note': '装備: ソナー付'}) \
            or not reader._is_note_row({'note': 'ソナー付'}) or not reader._is_note_row('|ソナー|||||||Ｎ|') \
            or not reader._is_note_row({'name': 'ｿﾅｰ', 'flags': 'ｎ'}):
        fails.append('15f: 注記行の判定が vendor と違う（note 付きの明細 dict 行／全角 Ｎ）')
    if reader._has_rows(note_page) or not reader._has_rows(PAGE_OK):
        fails.append('15c: _has_rows が注記行を明細に数えている')
    case = tempfile.mkdtemp(prefix='reg_reader_')
    fake = FakeReader([HEADER, PAGE_OK, note_page])
    res = reader.read_estimate(blank_pdf(2), reader=fake, case_dir=case, source_name='test.pdf')
    if not res.ok:
        fails.append(f'15d: 注記行だけの 2 ページ目があると読み取りが ok にならない: {res.fails()}')
    if fake.calls != 3:
        fails.append(f'15e: 注記行だけのページで読み直しが走った（呼び出し {fake.calls} 回）')
    shutil.rmtree(case, ignore_errors=True)

    # ── 4: 合計欄が合わない（header の totals が違う）。header 読み直し・ページ読み直しでも同じ → ok=False で止まる
    case = tempfile.mkdtemp(prefix='reg_reader_')
    bad_header = json.loads(json.dumps(HEADER))
    bad_header['totals']['parts'] = 40000   # 明細 45,000 と合わない
    bad_header['totals']['taxable'] = 56000
    fake = FakeReader([bad_header, PAGE_OK, bad_header, PAGE_OK])   # header / page1 / header 読み直し / page1 読み直し
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if res.ok:
        fails.append('合計欄が合わないのに ok になった（合計合わせをしていないか）')
    if not (res.check.get('fail')):
        fails.append(f'合計欄の FAIL が返っていない: {res.check} / {res.error}')
    if fake.calls != 4:
        fails.append(f'合計欄 FAIL 時の呼び出し回数が 4（header, page, header 再, page 再）でない: {fake.calls}')
    if len(fake.prompts) >= 3 and '合計欄' not in fake.prompts[2]:
        fails.append('header 読み直しの指示文に合計欄の話が無い')
    if rows_of(res.reading) != ROWS:   # 明細を変えて合わせていないこと
        fails.append('合計欄 FAIL の後に明細が書き換わっている（合計合わせの疑い）')
    # ── 11: 不合格のとき、人が直すための一式を zip にできる（pages/ と merge 結果の reading.json）
    zb = maker.repair_bundle(case, res.reading)
    if not zb:
        fails.append('repair_bundle が空')
    else:
        names = set(zipfile.ZipFile(io.BytesIO(zb)).namelist())
        if not {'pages/header.json', 'pages/page_1.json', 'reading.json'} <= names:
            fails.append(f'repair_bundle の中身が足りない: {sorted(names)}')
        if any(n.endswith('.neo') for n in names):
            fails.append('repair_bundle に NEO が入っている')
    shutil.rmtree(case, ignore_errors=True)

    # ── 5: LLM が壊れた JSON を返したら 1 回だけ言い直させる
    case = tempfile.mkdtemp(prefix='reg_reader_')
    fake = FakeReader(['{"source": ', HEADER, PAGE_OK])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if not res.ok or fake.calls != 3:
        fails.append(f'壊れた JSON の言い直しが効いていない: ok={res.ok} calls={fake.calls} error={res.error}')
    shutil.rmtree(case, ignore_errors=True)

    # ── 6: 読み直し上限（max_retries）を超えたら ok=False（無限に読み直さない）
    case = tempfile.mkdtemp(prefix='reg_reader_')
    fake = FakeReader([HEADER, PAGE_BAD, PAGE_BAD, PAGE_BAD, PAGE_BAD, PAGE_BAD])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf', max_retries=2)
    if res.ok or fake.calls != 4:   # header + 1 回目 + 読み直し 2 回
        fails.append(f'読み直し上限が効いていない: ok={res.ok} calls={fake.calls}')
    if res.ok is False and not res.fails():
        fails.append('不合格なのに理由（fails）が空')
    if not maker.repair_bundle(case, res.reading):   # ページ不合格でも pages/ は zip にできる
        fails.append('ページ不合格のときの repair_bundle が空')
    shutil.rmtree(case, ignore_errors=True)

    # ── 7: サイドバーの保険情報（insurance_hint）は見積書の印字より優先し（利用者が画面で確かめた値。2026-09-15 バグハント 3 回目 Q13）、
    #        サイドバーに無い項目は印字のまま残る
    case = tempfile.mkdtemp(prefix='reg_reader_')
    fake = FakeReader([HEADER, PAGE_OK])
    hint = {'policy_no': 'P-0001', 'contractor': 'ｹﾝｼｮｳ ﾊﾅｺ', 'accident_date': '20260901', 'company': 'サイドバー損保',
            'accept_no': 'A-2026-0001', 'agency': 'テスト代理店', 'adjuster': 'テスト査定', 'garage_in': '20260903', 'garage_out': '20260910', 'repair_days': '7'}
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf', insurance_hint=hint)
    ins = ((res.reading or {}).get('insurance') or {})
    if not res.ok or ins.get('policy_no') != 'P-0001' or ins.get('contractor') != 'ｹﾝｼｮｳ ﾊﾅｺ' or ins.get('accident_date') != '20260901':
        fails.append(f'insurance_hint が reading に補われていない: ok={res.ok} insurance={ins} error={res.error}')
    if ins.get('company') != 'サイドバー損保':
        fails.append(f'サイドバーの値（company）が見積書の印字より優先されていない: {ins.get("company")}')
    _h2 = reader._normalise_header(json.loads(json.dumps(HEADER)), None, {'policy_no': 'P-0002'}, None)
    if (_h2.get('insurance') or {}).get('company') != 'テスト損保' or (_h2.get('insurance') or {}).get('policy_no') != 'P-0002':
        fails.append(f'サイドバーに無い項目の印字が消えた／サイドバーの値が入らない: {_h2.get("insurance")}')
    # 生成まで通し、受付番号・代理店・アジャスター・入出庫日・修理日数が NEO の Insurance / FileInfo に本当に入ること
    # （ソース文字列の検査だけだと、vendor の生成器が古くて無視していても通ってしまう。Codex 指摘 2026-09-14）
    if res.ok:
        mk = maker.make_neo(case, 'estimate', no_profile=True)
        t = neo_if_tables(mk.neo_path) if (mk.ok and mk.neo_path) else {}
        ins_t, fi_t = (t.get('Insurance') or {}), (t.get('FileInfo') or {})
        if (not mk.ok or ins_t.get('AgencyName') != 'テスト代理店' or ins_t.get('AdjusterName') != 'テスト査定'
                or str(ins_t.get('RepairDays')) != '7' or fi_t.get('AcceptNo') != 'A-2026-0001'
                or fi_t.get('GarageInDate') != '20260903' or fi_t.get('GarageOutDate') != '20260910'):
            fails.append('保険情報が NEO の Insurance/FileInfo に入っていない（vendor の生成器が古い可能性。files 2026-09-14 以降を取り直す）: '
                         f'ok={mk.ok} AgencyName={ins_t.get("AgencyName")!r} AdjusterName={ins_t.get("AdjusterName")!r} RepairDays={ins_t.get("RepairDays")!r} '
                         f'AcceptNo={fi_t.get("AcceptNo")!r} GarageIn={fi_t.get("GarageInDate")!r} GarageOut={fi_t.get("GarageOutDate")!r}')
    shutil.rmtree(case, ignore_errors=True)

    # ── 8: 作業フォルダの後始末。消せたら None、無いパスも None
    case = tempfile.mkdtemp(prefix='reg_reader_')
    open(os.path.join(case, 'reading.json'), 'w').write('{}')
    if maker.remove_case_dir(case) is not None or os.path.exists(case):
        fails.append('remove_case_dir が作業フォルダを消せない')
    if maker.remove_case_dir(case) is not None or maker.remove_case_dir(None) is not None:
        fails.append('remove_case_dir が存在しないパスで None を返さない')

    # ── 9: このプロセスの環境変数・sys.path を vendor が汚していない（Streamlit は 1 プロセスを全利用者で共有）
    import sys as _sys
    from neo_skill import vendor as _v
    if os.environ.get('REPO_ROOT') == _v.VENDOR_ROOT:
        fails.append('reader/maker がこのプロセスの REPO_ROOT を vendor に書き換えている')
    if any(p.startswith(_v.VENDOR_ROOT) for p in _sys.path):
        fails.append('reader/maker がこのプロセスの sys.path に vendor を入れている')
    if any(m in _sys.modules for m in ('reading_pages', 'reading_check', 'draft_estimate', 'skill_env')):
        fails.append('vendor のスクリプトがこのプロセスに import されている')

    # ── 10: 明細の無いページ（計算書だけ）は空ブロックに正規化して検算を通す。2 ページとも 1 回で合格
    case = tempfile.mkdtemp(prefix='reg_reader_')
    fake = FakeReader([HEADER, PAGE_OK, PAGE_BLANK])
    res = reader.read_estimate(blank_pdf(2), reader=fake, case_dir=case, source_name='test.pdf')
    if not res.ok or fake.calls != 3:
        fails.append(f'明細の無いページが検算を通らない: ok={res.ok} calls={fake.calls} fails={res.fails()} error={res.error}')
    if len(res.traces) == 2 and not (res.traces[1].ok and res.traces[1].attempts == 1):
        fails.append(f'2 ページ目（明細なし）が 1 回で合格していない: {res.traces[1]}')
    if rows_of(res.reading) != ROWS:
        fails.append('明細の無いページを足すと merge 結果の明細が変わる')
    if os.path.isfile(os.path.join(case, 'pages', 'page_2.json')):
        fails.append('明細の無いページが pages/ に書かれている（vendor の Checker が FAIL にする）')
    if res.ok:
        mk = maker.make_neo(case, 'estimate', no_profile=True)
        if not mk.ok:
            fails.append(f'明細の無いページを含む案件で make_neo が不合格: {mk.error or mk.reasons} / {mk.match_line}')
    shutil.rmtree(case, ignore_errors=True)

    # ── 12: 塗装行・費用だけのページは、その行を header に移してから pages/ から除く（merge が繋ぐ先と同じ）
    case = tempfile.mkdtemp(prefix='reg_reader_')
    page_paint = {'page': 2, 'rows_printed': 0, 'blocks': [],
                  'expenses': [{'name': 'ショートパーツ', 'amount': 1000, 'in': '部品計'}]}
    hdr2 = json.loads(json.dumps(HEADER))
    hdr2['totals'] = {'parts': 46000, 'wage': 16000, 'taxable': 62000, 'tax': 6200, 'total': 68200}   # 費用 1,000 を部品計に含む
    fake = FakeReader([hdr2, PAGE_OK, page_paint])
    res = reader.read_estimate(blank_pdf(2), reader=fake, case_dir=case, source_name='test.pdf')
    exp = (res.reading or {}).get('expenses') or []
    if not res.ok or len(exp) != 1 or exp[0].get('amount') != 1000:
        fails.append(f'費用だけのページの費用が header に移っていない: ok={res.ok} expenses={exp} fails={res.fails()} error={res.error}')
    shutil.rmtree(case, ignore_errors=True)

    # ── 13: write_pages は前回の page_*.json を消してから書く（読み直しでページが減っても古い行が merge に混ざらない）
    case = tempfile.mkdtemp(prefix='reg_reader_')
    maker.write_pages(case, HEADER, [dict(PAGE_OK, page=1), dict(PAGE_OK, page=2), dict(PAGE_OK, page=3)])
    open(os.path.join(case, 'pages', 'status.json'), 'w').write('{}')
    maker.write_pages(case, HEADER, [dict(PAGE_OK, page=1), dict(PAGE_OK, page=2)])
    left = sorted(os.listdir(os.path.join(case, 'pages')))
    if left != ['header.json', 'page_1.json', 'page_2.json']:
        fails.append(f'write_pages が前回の page_*.json / status.json を残している: {left}')
    shutil.rmtree(case, ignore_errors=True)

    # ── 14: アプリが決めた ADDATA は subprocess の環境変数 ADDATA_ROOT として vendor に渡る（渡さなければ触らない）
    from neo_skill import vendor as _v2
    env_a = _v2.subprocess_env(addata_root=r'X:\Addata_test')
    env_b = _v2.subprocess_env()
    if env_a.get('ADDATA_ROOT') != r'X:\Addata_test' or env_a.get('REPO_ROOT') != _v2.VENDOR_ROOT:
        fails.append(f'subprocess_env が ADDATA_ROOT / REPO_ROOT を渡していない: {env_a.get("ADDATA_ROOT")} / {env_a.get("REPO_ROOT")}')
    if env_b.get('ADDATA_ROOT') != os.environ.get('ADDATA_ROOT'):
        fails.append('subprocess_env が addata_root 未指定のときに ADDATA_ROOT を勝手に変えている')

    # ── 15: 開けない画像（HEIC を pillow-heif 無しで開いた場合など）は分かりやすい LLMError になる（黙って落ちない）
    try:
        llm_mod.image_to_pdf(b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic')   # HEIC の先頭だけ（中身なし）
        fails.append('開けない画像で例外が出ない')
    except llm_mod.LLMError as e:
        if 'HEIC' not in str(e):
            fails.append(f'開けない画像のエラー文に HEIC の案内が無い: {e}')
    except Exception as e:  # noqa: BLE001
        fails.append(f'開けない画像で LLMError 以外の例外: {type(e).__name__}: {e}')

    # ── 16: vendor は取り込みのまま（コミット・ハッシュ）で、git にも全部入る（.gitignore で落ちるファイルが無い）
    import subprocess as _sp
    p = _sp.run([sys.executable, os.path.join(APP, 'tools', 'vendor_sync.py'), '--check'],
                capture_output=True, text=True, encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    if p.returncode != 0:
        fails.append('tools/vendor_sync.py --check が NG: ' + (p.stdout + p.stderr).strip()[-300:])

    # ── 17: Gemini 版の読み手は、同じブロック（document / text）を Gemini の形に写し、finish_reason を同じ規則で扱う（API は呼ばない）
    try:
        from google.genai import types as _gt
        parts = llm_mod.GeminiReader.to_parts([llm_mod.document_block(pdf), {'type': 'text', 'text': 'こんにちは'}], _gt)
        if len(parts) != 2 or not isinstance(parts[1], str) or getattr(getattr(parts[0], 'inline_data', None), 'mime_type', '') != 'application/pdf':
            fails.append(f'GeminiReader.to_parts の写しが違う: {parts}')
        if llm_mod.GeminiReader._fatal('404 NOT_FOUND model') is not True or llm_mod.GeminiReader._fatal('503 Service Unavailable') is not False:
            fails.append('GeminiReader._fatal の判定が違う（404 は即諦め、503 は再試行）')

        class _Stub:  # generate_content の返事を差し替えて、切れた返事・空の返事を拒む規則を確かめる
            def __init__(self, text, fin):
                self.text = text
                self.candidates = [type('C', (), {'finish_reason': type('F', (), {'name': fin})()})()]
                self.usage_metadata = type('U', (), {'prompt_token_count': 5, 'candidates_token_count': 2, 'cached_content_token_count': 0})()
                self.response_id = 'r1'

        gr = llm_mod.GeminiReader.__new__(llm_mod.GeminiReader)
        gr._types = _gt; gr.model = 'stub'; gr.max_tokens = 10; gr.calls = 0
        gr.client = type('Cl', (), {'models': type('M', (), {'generate_content': staticmethod(lambda **kw: _Stub('{"a":1}', 'STOP'))})()})()
        rp = gr.ask('sys', [{'type': 'text', 'text': 'x'}])
        if rp.text != '{"a":1}' or rp.stop_reason != 'STOP' or rp.input_tokens != 5:
            fails.append(f'GeminiReader.ask の戻りが違う: {rp}')
        for bad_fin in ('MAX_TOKENS', 'OTHER', 'LANGUAGE', 'SAFETY', 'FINISH_REASON_UNSPECIFIED', ''):
            gr.client = type('Cl', (), {'models': type('M', (), {'generate_content': staticmethod(lambda _f=bad_fin, **kw: _Stub('{"a":1}', _f))})()})()
            try:
                gr.ask('sys', [{'type': 'text', 'text': 'x'}])
                fails.append(f'GeminiReader が finish_reason={bad_fin!r} の返事を通した（STOP 以外は拒む）')
            except llm_mod.LLMError:
                pass
        if llm_mod.make_reader('gemini', api_key='dummy', model='gemini-x').model != 'gemini-x':
            fails.append('make_reader(gemini) がモデル名を通していない')
    except ImportError:
        print('（google-genai が無いので項目 17 は省略）')

    # ── 18: LLM が subtotal / marks を配列・文字列で返しても読み取り全体は止まらず、形の FAIL 文言で読み直して合格する
    case = tempfile.mkdtemp(prefix='reg_reader_')
    bad_shape = dict(PAGE_OK, subtotal=[45000, 16000], marks='$')
    fake = FakeReader([HEADER, bad_shape, PAGE_OK])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if not res.ok or fake.calls != 3:
        fails.append(f'形の崩れたページが読み直しに回らない: ok={res.ok} calls={fake.calls} error={res.error[:120]}')
    if len(fake.prompts) >= 3 and ('subtotal は' not in fake.prompts[2] or 'marks は' not in fake.prompts[2]):
        fails.append('形の崩れの読み直し指示文に、どのキーの形が違うかが書かれていない')
    shutil.rmtree(case, ignore_errors=True)
    # header 側の型崩れ（vehicle が配列、expenses が文字列、要素がオブジェクトでない）は落とさず PageShapeError（読み直しの理由）
    for label, bad_h in (('vehicle が配列', {'vehicle': ['JF1'], 'totals': {'parts': 1}}), ('expenses が文字列', {'totals': {'parts': 1}, 'expenses': 'x'}),
                         ('expenses の要素が文字列', {'totals': {'parts': 1}, 'expenses': ['x', {'name': 'y', 'amount': 1}]}), ('adas の要素が null', {'totals': {'parts': 1}, 'adas': [None]}),
                         ('paint.lines の要素が文字列', {'totals': {'parts': 1}, 'paint': {'lines': ['x']}}), ('paint.lines が文字列', {'totals': {'parts': 1}, 'paint': {'lines': 'x'}}),
                         ('paint.panels の要素が文字列', {'totals': {'parts': 1}, 'paint': {'panels': ['x']}}), ('paint.other の要素が文字列', {'totals': {'parts': 1}, 'paint': {'other': ['x']}}),
                         ('frame.items の要素が文字列', {'totals': {'parts': 1}, 'frame': {'items': ['x']}}), ('hints.eva_codes の要素が数値', {'totals': {'parts': 1}, 'hints': {'eva_codes': [1]}}),
                         ('paint.sealing が文字列', {'totals': {'parts': 1}, 'paint': {'sealing': 'x'}}), ('paint.wax が配列', {'totals': {'parts': 1}, 'paint': {'wax': [1]}}),
                         ('paint.booth が数値', {'totals': {'parts': 1}, 'paint': {'booth': 2550}})):
        try:
            h = reader._normalise_header(bad_h, {'model_code': 'JF1'}, None)
            fails.append(f'_normalise_header が型崩れ（{label}）を黙って落とした: {h}')
        except reader.PageShapeError:
            pass
    # schema どおりの入れ子（frame.items のオブジェクト、hints.eva_codes の文字列）は通る
    try:
        h = reader._normalise_header({'totals': {'parts': 1}, 'frame': {'basic': True, 'items': [{'code': '1400', 'rank': 'A'}]}, 'hints': {'eva_codes': ['U'], 'eva_exclude': ['T']},
                                      'paint': {'lines': [{'name': 'a', 'index': 1.0, 'wage': 1}], 'panels': [{'code': '1400', 'wage': 1}], 'other': [{'name': 'b', 'wage': 1}],
                                                'booth': {'index': 0.3, 'wage': 2550}, 'sealing': {'m': 2, 'wage': 1000}, 'material': 12000, 'total': 34850}}, None, None)
        if not (h.get('frame', {}).get('items') and h.get('hints', {}).get('eva_codes') == ['U'] and len(h.get('paint', {})) == 7):
            fails.append(f'_normalise_header が schema どおりの入れ子を落とした: {h}')
    except reader.PageShapeError as e:
        fails.append(f'_normalise_header が schema どおりの入れ子を形の誤りにした: {e}')
    # null / 空の項目は「書かなかった」として落とすだけ（形の誤りにしない）
    h = reader._normalise_header({'vehicle': None, 'totals': {'parts': 1, 'wage': None}, 'expenses': [], 'paint': {}}, {'model_code': 'JF1'}, None)
    if h.get('vehicle') != {'model_code': 'JF1'} or h.get('totals') != {'parts': 1} or 'expenses' in h or 'paint' in h:
        fails.append(f'_normalise_header が null / 空の扱いを変えた: {h}')

    # ── 20: 要素の形崩れ（expenses が文字列の配列、rows が文字列）でも検算プロセスが落ちず、形の FAIL 文言で読み直して合格する（Codex 17）
    for label, bad_page in (('expenses の要素が文字列', dict(PAGE_OK, expenses=['short parts 1000'])),
                            ('paint_lines の要素が文字列', dict(PAGE_OK, paint_lines=['x'])),
                            ('rows の要素が配列', dict(PAGE_OK, blocks=[{'title': '', 'rows': [['Rrﾊﾞﾝﾊﾟ', '取替', 45000]]}])),
                            ('blocks の要素が文字列', dict(PAGE_OK, blocks=['|Rrﾊﾞﾝﾊﾟ|取替|71501-TY0-000ZZ|1.00|1|45000|8000||']))):
        case = tempfile.mkdtemp(prefix='reg_reader_')
        fake = FakeReader([HEADER, bad_page, PAGE_OK])
        res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
        if not res.ok or fake.calls != 3:
            fails.append(f'{label}: 読み直しに回らない: ok={res.ok} calls={fake.calls} error={res.error[:120]}')
        if len(fake.prompts) >= 3 and ('オブジェクト' not in fake.prompts[2] and '文字列' not in fake.prompts[2]):
            fails.append(f'{label}: 読み直し指示文に要素の形が書かれていない')
        if len(fake.prompts) >= 3 and label == 'rows の要素が配列' and '"取替"' not in fake.prompts[2]:
            fails.append(f'{label}: 読み直し指示文の「前回の写し」から形の崩れた行が消えている（元の返事を書き換えている。Codex 23）')
        if len(fake.prompts) >= 3 and label == 'blocks の要素が文字列' and '取替' not in fake.prompts[2]:
            fails.append(f'{label}: 読み直し指示文の「前回の写し」から文字列の block が消えている（Codex 24）')
        shutil.rmtree(case, ignore_errors=True)
    # ── 21: header.json が配列で返っても読み取り全体は止まらない — parse_json_reply が非オブジェクトを拒み、_ask_json が
    #        1 回言い直させる（Codex 19 の「AttributeError で止まる」は到達しない。_normalise_header にも明確なガードを置いた）
    case = tempfile.mkdtemp(prefix='reg_reader_')
    fake = FakeReader(['[1, 2]', HEADER, PAGE_OK])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if not res.ok or fake.calls != 3:
        fails.append(f'header が配列のとき言い直しに回らない: ok={res.ok} calls={fake.calls} error={res.error[:120]}')
    if len(fake.prompts) >= 2 and 'オブジェクト' not in fake.prompts[1]:
        fails.append('header の言い直し指示文に、オブジェクトで返す旨が書かれていない')
    shutil.rmtree(case, ignore_errors=True)
    # 2 回続けて配列なら、理由付きの error で終わる（例外の型名だけにしない・AttributeError で落ちない）
    case = tempfile.mkdtemp(prefix='reg_reader_')
    fake = FakeReader(['[1]', '[2]', HEADER, PAGE_OK])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if res.ok or fake.calls != 2 or 'オブジェクト' not in (res.error or '') or 'AttributeError' in (res.error or ''):
        fails.append(f'header が配列のまま直らないときの終わり方: ok={res.ok} calls={fake.calls} error={(res.error or "")[:120]}')
    shutil.rmtree(case, ignore_errors=True)
    try:  # _normalise_header 自体も配列・文字列・null を AttributeError でなく理由付きで拒む
        reader._normalise_header([1, 2], None, None); fails.append('_normalise_header が配列を通した')
    except reader.PageShapeError:
        pass

    # ── 22: header の totals が配列で返る／無い → 落として合計欄の検算なしで合格にせず、理由を返して読み直して合格する（Codex 21）
    for label, bad_h in (('totals が配列', dict(HEADER, totals=[45000, 16000])), ('totals が無い', {k: v for k, v in HEADER.items() if k != 'totals'}),
                         ('paint が配列', dict(HEADER, paint=['x'])), ('paint.lines の要素が文字列', dict(HEADER, paint={'lines': ['x']})),
                         ('frame.items の要素が文字列', dict(HEADER, frame={'items': ['x']})), ('paint.sealing が文字列', dict(HEADER, paint={'sealing': 'x'}))):
        case = tempfile.mkdtemp(prefix='reg_reader_')
        fake = FakeReader([bad_h, HEADER, PAGE_OK])
        res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
        if not res.ok or fake.calls != 3:
            fails.append(f'{label}: header の読み直しに回らない: ok={res.ok} calls={fake.calls} error={res.error[:120]}')
        if len(fake.prompts) >= 2 and ('totals' not in fake.prompts[1] and 'paint' not in fake.prompts[1] and 'frame' not in fake.prompts[1]):
            fails.append(f'{label}: header の読み直し指示文に問題の項目が書かれていない')
        shutil.rmtree(case, ignore_errors=True)
    # 上限まで直らなければ不合格（合計欄なしで NEO を作らせない）。理由付きの error
    case = tempfile.mkdtemp(prefix='reg_reader_')
    bad_h = dict(HEADER, totals=[45000, 16000])
    fake = FakeReader([bad_h, bad_h, bad_h, bad_h, PAGE_OK])
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf')
    if res.ok or fake.calls != 4 or 'totals' not in (res.error or ''):
        fails.append(f'totals が直らないときの終わり方: ok={res.ok} calls={fake.calls} error={(res.error or "")[:120]}')
    shutil.rmtree(case, ignore_errors=True)

    # 文字列の行（| 区切り）と dict 行はどちらも正しい形（reading_schema.md）。形の誤りにしない
    try:
        reader._normalise_page({'page': 1, 'rows_printed': 2, 'blocks': [{'title': '', 'rows': ['|a|取替||1.00|1|100|||', {'name': 'b', 'method': '脱着', 'qty': 1}]}], 'subtotal': {}, 'marks': {}}, 1)
    except reader.PageShapeError as e:
        fails.append(f'文字列の行・dict 行が形の誤りにされた: {e}')

    # ── 19: ページ数の上限。上限を超える PDF は AI を 1 回も呼ばずに止まる。環境変数が読めなくても import が落ちない（Codex 22）
    old_env = os.environ.get('NEO_READER_MAX_PAGES')
    try:
        for val, want in (('abc', 30), ('0', 30), ('-5', 30), (' 12 ', 12), ('', 30)):
            os.environ['NEO_READER_MAX_PAGES'] = val
            if reader._env_int('NEO_READER_MAX_PAGES', 30) != want:
                fails.append(f'NEO_READER_MAX_PAGES={val!r} → {reader._env_int("NEO_READER_MAX_PAGES", 30)}（期待 {want}）')
    finally:
        if old_env is None:
            os.environ.pop('NEO_READER_MAX_PAGES', None)
        else:
            os.environ['NEO_READER_MAX_PAGES'] = old_env
    fake = FakeReader([])
    old = reader.MAX_PAGES
    reader.MAX_PAGES = 2
    try:
        case = tempfile.mkdtemp(prefix='reg_reader_')
        res = reader.read_estimate(blank_pdf(3), reader=fake, case_dir=case, source_name='big.pdf')
        if res.ok or fake.calls != 0 or '上限' not in res.error:
            fails.append(f'ページ数の上限が効いていない: ok={res.ok} calls={fake.calls} error={res.error[:80]}')
        shutil.rmtree(case, ignore_errors=True)
    finally:
        reader.MAX_PAGES = old

    for f in fails:
        print('*** FAILED:', f)
    print('reg_reader:', 'all ok' if not fails else f'{len(fails)} 件が不合格')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
