# -*- coding: utf-8 -*-
"""reader.py — 見積 PDF を「ページごとに写し、ページごとに検算し、落ちたページだけ読み直す」（移植ガイド §3-3）。

  1. header（明細以外）を全ページから写す
  2. ページごとに明細を写す → vendor の reading_pages.validate_page で検算 → FAIL なら FAIL の文言を返して読み直し（最大 max_retries 回）
  3. 全ページ合格 → reading_pages.merge → 合計欄の検算（reading_check.Checker）。落ちたら header を読み直し、
     それでも合わなければ各ページを差額のヒント付きで読み直す
  4. それでも合わなければ人に回す（ok=False。差額と FAIL を返す）。合計合わせのために行を消したり金額を動かしたりしない

出力は case_dir/pages/（header.json + page_N.json）。**reading.json はここでは書かない** — make_neo.py が pages/ から
merge して作る（アプリが reading.json を組み立てない = 同じコードで同じ結果）。merge 結果は ReadResult.reading に持つ。

検算はすべて vendor のコードで行う（規則をここに書かない）。vendor のスクリプトは import 時に環境変数と sys.path を
書き換えるので、このプロセスでは import せず _runner.py（別プロセス）に任せる。
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import subprocess
import time
from typing import Callable, Optional

from . import llm as llm_mod
from . import maker, prompts, vendor

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_runner.py')
RUNNER_MARK = '@@RESULT@@'


@dataclasses.dataclass
class PageTrace:
    page: int
    attempts: int = 0
    ok: bool = False
    rows: int = 0
    fail: list = dataclasses.field(default_factory=list)
    warn: list = dataclasses.field(default_factory=list)
    first_try_ok: bool = False


@dataclasses.dataclass
class ReadResult:
    ok: bool
    case_dir: str
    n_pages: int = 0
    header: dict = dataclasses.field(default_factory=dict)
    pages: list = dataclasses.field(default_factory=list)
    reading: Optional[dict] = None                              # merge の結果（reading.json 相当。ファイルには書かない）
    traces: list = dataclasses.field(default_factory=list)
    merge_messages: list = dataclasses.field(default_factory=list)
    check: dict = dataclasses.field(default_factory=dict)      # 合計欄の検算（fail / warn / settings）
    error: str = ''
    usage: dict = dataclasses.field(default_factory=dict)      # トークン・呼び出し回数
    stats: dict = dataclasses.field(default_factory=dict)      # §5-3 の指標

    def fails(self) -> list:
        out = []
        for t in self.traces:
            if not t.ok:
                out += [f'ページ {t.page}: {f}' for f in t.fail]
        out += self.merge_messages
        out += [f'合計欄: {f}' for f in (self.check.get('fail') or [])]
        return out


class RunnerError(RuntimeError):
    pass


def _runner(op: str, timeout: float = 300.0, addata_root: Optional[str] = None, **kw) -> dict:
    """vendor の検算関数を別プロセスで呼ぶ（_runner.py）。戻りは JSON（dict）。
    addata_root はアプリが決めた ADDATA（無ければ vendor の skill_env が設定ファイル→自動検出で決める）"""
    req = dict(kw, op=op, vendor_root=vendor.VENDOR_ROOT)
    try:
        p = subprocess.run([vendor.python_exe(), RUNNER], input=json.dumps(req, ensure_ascii=False),
                           cwd=vendor.VENDOR_ROOT, env=vendor.subprocess_env(addata_root), capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RunnerError(f'検算（{op}）が {int(timeout)} 秒で終わらなかった')
    except OSError as e:
        raise RunnerError(f'検算（{op}）を起動できない: {e}')
    line = next((l for l in reversed((p.stdout or '').splitlines()) if l.startswith(RUNNER_MARK)), '')
    if p.returncode != 0 or not line:
        tail = (p.stderr or p.stdout or '').strip().splitlines()[-6:]
        raise RunnerError(f'検算（{op}）が失敗（終了コード {p.returncode}）: ' + ' / '.join(t.strip() for t in tail)[:600])
    try:
        return json.loads(line[len(RUNNER_MARK):])
    except ValueError as e:
        raise RunnerError(f'検算（{op}）の結果を読めない: {e}')


def _progress(cb: Optional[Callable], msg: str) -> None:
    if cb:
        try:
            cb(msg)
        except Exception:  # noqa: BLE001  進捗表示の失敗で読み取りを止めない
            pass


class _Usage:
    def __init__(self):
        self.calls = 0
        self.input = self.output = self.cache_read = self.cache_write = 0

    def add(self, r: llm_mod.LLMReply) -> None:
        self.calls += 1
        self.input += r.input_tokens
        self.output += r.output_tokens
        self.cache_read += r.cache_read_tokens
        self.cache_write += r.cache_write_tokens

    def as_dict(self) -> dict:
        return {'calls': self.calls, 'input_tokens': self.input, 'output_tokens': self.output,
                'cache_read_tokens': self.cache_read, 'cache_write_tokens': self.cache_write}


def _ask_json(reader, system: str, blocks: list, usage: _Usage, what: str) -> dict:
    """JSON を返させる。壊れた JSON は 1 回だけ言い直させる"""
    r = reader.ask(system, blocks)
    usage.add(r)
    try:
        return llm_mod.parse_json_reply(r.text)
    except llm_mod.LLMError as e:
        r2 = reader.ask(system, blocks + [{'type': 'text', 'text': f'前回の返事は JSON として読めませんでした（{e}）。{what}を JSON だけで返してください。'}])
        usage.add(r2)
        return llm_mod.parse_json_reply(r2.text)


EMPTY_BLOCKS = [{'title': '', 'rows': []}]


def _normalise_page(page: dict, page_no: int) -> dict:
    """LLM の出力を page_N.json の形に揃える（値には触らない。欠けたキーを補い、page 番号を固定する）。
    明細の無いページは blocks を空リストにせず 1 つの空ブロックにする（vendor の validate_page は
    `not page.get('blocks')` を FAIL にするため。文言は「空の blocks を書く」だが空リストは通らない）"""
    p = dict(page or {})
    p['page'] = page_no
    p['blocks'] = [b for b in (p.get('blocks') or []) if isinstance(b, dict)] or [dict(b) for b in EMPTY_BLOCKS]
    p['subtotal'] = dict(p.get('subtotal') or {})
    p['marks'] = dict(p.get('marks') or {})
    for k in ('paint_lines', 'expenses'):
        if not p.get(k):
            p.pop(k, None)
    return p


def _apply_hint(header: dict, key: str, hint: Optional[dict]) -> None:
    """車検証・サイドバーで分かっている値を、見積書に印字が無い項目にだけ補う（印字があればそちらを残す）"""
    if not hint:
        return
    v = dict(header.get(key) or {})
    for k, val in hint.items():
        if val not in (None, '') and not v.get(k):
            v[k] = val
    if v:
        header[key] = v


def _normalise_header(h: dict, vehicle_hint: Optional[dict], insurance_hint: Optional[dict]) -> dict:
    out = {k: v for k, v in (h or {}).items() if k in prompts.HEADER_KEYS}
    # 空の totals 項目（null）は落とす（reading_check は「書いた項目」だけを突き合わせる）
    tt = out.get('totals')
    if isinstance(tt, dict):
        out['totals'] = {k: v for k, v in tt.items() if v not in (None, '')}
    for k in ('vehicle', 'customer', 'insurance'):
        if isinstance(out.get(k), dict):
            out[k] = {kk: vv for kk, vv in out[k].items() if vv not in (None, '')}
    for k in list(out):
        if out[k] in (None, '', [], {}):
            out.pop(k)
    _apply_hint(out, 'vehicle', vehicle_hint)
    _apply_hint(out, 'insurance', insurance_hint)
    return out


def _has_rows(page: dict) -> bool:
    return any((b.get('rows') or []) for b in (page.get('blocks') or []) if isinstance(b, dict))


def _pages_for_merge(header: dict, pages: list) -> tuple:
    """pages/ に書く形にする。明細の無いページ（表紙・計算書）は pages/ に置かない —
    vendor の reading_check.Checker は `pages` に載っていて行の無いページを FAIL にするため
    （validate_page の案内「rows_printed: 0 と空の blocks」とは食い違う。人が写した実案件も明細ページだけを 1 から数えている）。
    そのページに塗装行・費用だけがあれば header の paint.lines / expenses に移す（merge が繋ぐ先と同じ）。
    残ったページは 1 から番号を付け直す（ファイル名 page_N.json と中身の page を merge が突き合わせる）。
    検算・読み直し・画面表示は PDF の実ページ番号のまま行い、ここでは書き出す形だけを変える"""
    hdr = copy.deepcopy(header)
    out = []
    for p in pages:
        if _has_rows(p):
            out.append(copy.deepcopy(p))
            continue
        if p.get('paint_lines'):
            paint = hdr.get('paint') if isinstance(hdr.get('paint'), dict) else {}
            paint.setdefault('lines', []).extend(copy.deepcopy(p['paint_lines']))
            hdr['paint'] = paint
        if p.get('expenses'):
            hdr.setdefault('expenses', []).extend(copy.deepcopy(p['expenses']))
    for i, p in enumerate(out, 1):
        p['page'] = i
    return hdr, out


def read_estimate(pdf_bytes: bytes, *, reader, case_dir: str, source_name: str = '',
                  vehicle_hint: Optional[dict] = None, insurance_hint: Optional[dict] = None, max_retries: int = 3,
                  progress: Optional[Callable[[str], None]] = None, addata_root: Optional[str] = None) -> ReadResult:
    """PDF → case_dir/pages/（header.json + page_N.json）→ 検算・読み直し。
    reader は neo_skill.llm.ClaudeReader（ask(system, blocks) を持つもの）。
    reading.json は書かない（make_neo.py が pages/ から merge する）。merge 結果は戻り値の reading に入る。
    addata_root: アプリが決めた ADDATA。検算ランナーにも同じものを渡す（make_neo と版を揃える）"""
    def run(op: str, **kw) -> dict:
        return _runner(op, addata_root=addata_root, **kw)

    usage = _Usage()
    res = ReadResult(False, case_dir)
    t0 = time.time()
    try:
        n = llm_mod.pdf_page_count(pdf_bytes)
    except Exception as e:  # noqa: BLE001
        res.error = f'PDF を開けない: {e}'
        return res
    res.n_pages = n
    if n <= 0:
        res.error = 'PDF にページが無い'
        return res
    try:
        system = prompts.build_system_prompt()
        whole = llm_mod.document_block(pdf_bytes)
        # 1) header（明細以外）
        _progress(progress, f'合計欄・車両欄を写しています（全 {n} ページ）')
        header = _normalise_header(_ask_json(reader, system, [whole, {'type': 'text', 'text': prompts.header_task(n, vehicle_hint, source_name)}], usage, 'header.json'),
                                   vehicle_hint, insurance_hint)
        res.header = header
        # 2) ページごとに写して検算
        pages: list = []
        page_pdfs = [llm_mod.pdf_single_page(pdf_bytes, i) for i in range(n)]
        for i in range(n):
            pg = i + 1
            tr = PageTrace(page=pg)
            blocks = [llm_mod.document_block(page_pdfs[i]), {'type': 'text', 'text': prompts.page_task(pg, n, header)}]
            _progress(progress, f'{pg}/{n} ページ目を写しています')
            page = _normalise_page(_ask_json(reader, system, blocks, usage, f'page_{pg}.json'), pg)
            tr.attempts = 1
            v = run('validate', header=header, page=page)
            tr.first_try_ok = bool(v.get('ok'))
            while not v.get('ok') and tr.attempts <= max_retries:
                _progress(progress, f'{pg}/{n} ページ目の検算に落ちたので読み直しています（{tr.attempts} 回目）: {(v.get("fail") or [""])[0][:60]}')
                blocks = [llm_mod.document_block(page_pdfs[i]),
                          {'type': 'text', 'text': prompts.retry_task(pg, v.get('fail') or [], v.get('warn') or [], page)}]
                page = _normalise_page(_ask_json(reader, system, blocks, usage, f'page_{pg}.json'), pg)
                tr.attempts += 1
                v = run('validate', header=header, page=page)
            tr.ok, tr.rows, tr.fail, tr.warn = bool(v.get('ok')), int(v.get('rows') or 0), list(v.get('fail') or []), list(v.get('warn') or [])
            pages.append(page)
            res.traces.append(tr)
        res.pages = pages
        maker.write_pages(case_dir, *_pages_for_merge(header, pages))
        # 3) 束ねて合計欄を検算
        m = run('merge', case_dir=case_dir)
        rd, check = m.get('reading'), (m.get('check') or {})
        res.merge_messages = list(m.get('messages') or [])
        rounds = 0
        while rd and check.get('fail') and rounds < 2:
            rounds += 1
            if rounds == 1:
                _progress(progress, '合計欄と合わないので、合計欄・費用・塗装の写しを読み直しています')
                header = _normalise_header(_ask_json(reader, system, [whole, {'type': 'text', 'text': prompts.header_retry_task(check['fail'], check.get('warn') or [], header)}], usage, 'header.json'),
                                           vehicle_hint, insurance_hint)
                res.header = header
            else:
                _progress(progress, '合計欄と合わないので、各ページの写し漏れ・二重写しを確かめています')
                new_pages = []
                for i, page in enumerate(pages):
                    pg = i + 1
                    blocks = [llm_mod.document_block(page_pdfs[i]),
                              {'type': 'text', 'text': prompts.page_totals_retry_task(pg, check['fail'], page)}]
                    p2 = _normalise_page(_ask_json(reader, system, blocks, usage, f'page_{pg}.json'), pg)
                    v = run('validate', header=header, page=p2)
                    if v.get('ok'):
                        new_pages.append(p2)
                        res.traces[i].attempts += 1
                    else:  # 読み直しで検算が落ちるなら前回の（合格していた）写しを残す
                        new_pages.append(page)
                pages = new_pages
                res.pages = pages
            maker.write_pages(case_dir, *_pages_for_merge(header, pages))
            m = run('merge', case_dir=case_dir)
            rd, check = m.get('reading'), (m.get('check') or {})
            res.merge_messages = list(m.get('messages') or [])
        res.check = check or {}
        res.reading = rd
        res.ok = bool(rd) and not (check or {}).get('fail') and all(t.ok for t in res.traces)
    except (llm_mod.LLMError, RunnerError) as e:
        res.error = str(e)
    except Exception as e:  # noqa: BLE001  理由を残して返す（握り潰さない）。顧客情報は含めない（型と文言だけ）
        res.error = f'{type(e).__name__}: {e}'
    res.usage = usage.as_dict()
    first = sum(1 for t in res.traces if t.first_try_ok)
    res.stats = {
        'pages': n, 'first_try_ok_rate': (first / len(res.traces)) if res.traces else 0.0,
        'retries': sum(max(0, t.attempts - 1) for t in res.traces),
        'calls': usage.calls, 'seconds': round(time.time() - t0, 1),
    }
    return res
