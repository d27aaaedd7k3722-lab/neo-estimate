# -*- coding: utf-8 -*-
"""reg_reader.py — 「読む」段（neo_skill.reader）の検算・読み直しループを、LLM を差し替えて確かめる（API 不要・課金なし）。

  1. 1 回目のページ写しがわざと検算に落ちる（rows_printed が違う）→ FAIL の文言を渡して読み直す → 2 回目で合格
  2. 全ページ合格 → merge → 合計欄の検算 OK（reading.json は reader が書かず、make_neo が pages/ から作る）
  3. その pages/ で vendor の make_neo が合格し、NEO と確認箇所シートが組で出る
  4. 合計欄が合わないまま読み直しても直らないときは ok=False で止まり、NEO を作らない（合計合わせをしない）
  5〜9. 壊れた JSON の言い直し / 読み直し上限 / 保険情報の補い / 作業フォルダの後始末 / プロセスを汚さないこと
  10. 明細の無いページ（表紙・計算書）は blocks を空ブロックにして検算を通す（vendor の validate_page は空リストを FAIL にする）
  11. 不合格のとき、人が直すための一式（pages/・reading.json）を zip にできる

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

    # ── 7: サイドバーの保険情報（insurance_hint）は、見積書に印字が無い項目にだけ補われる
    case = tempfile.mkdtemp(prefix='reg_reader_')
    fake = FakeReader([HEADER, PAGE_OK])
    hint = {'policy_no': 'P-0001', 'contractor': 'ｹﾝｼｮｳ ﾊﾅｺ', 'accident_date': '20260901', 'company': '上書きされない'}
    res = reader.read_estimate(pdf, reader=fake, case_dir=case, source_name='test.pdf', insurance_hint=hint)
    ins = ((res.reading or {}).get('insurance') or {})
    if not res.ok or ins.get('policy_no') != 'P-0001' or ins.get('contractor') != 'ｹﾝｼｮｳ ﾊﾅｺ' or ins.get('accident_date') != '20260901':
        fails.append(f'insurance_hint が reading に補われていない: ok={res.ok} insurance={ins} error={res.error}')
    if ins.get('company') != 'テスト損保':
        fails.append(f'見積書に印字のある項目（company）がヒントで上書きされた: {ins.get("company")}')
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

    for f in fails:
        print('*** FAILED:', f)
    print('reg_reader:', 'all ok' if not fails else f'{len(fails)} 件が不合格')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
