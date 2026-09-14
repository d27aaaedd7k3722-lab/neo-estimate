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
def _env_int(name: str, default: int) -> int:
    """環境変数の整数（読めない・0 以下なら既定。'abc' で import ごと落ちてアプリが起動しなくならないように）"""
    try:
        v = int(str(os.environ.get(name) or '').strip() or default)
    except ValueError:
        return default
    return v if v > 0 else default


MAX_PAGES = _env_int('NEO_READER_MAX_PAGES', 30)   # 誤アップロード（写真集・全案件の束）で API を何百回も呼ばない


class PageShapeError(ValueError):
    """LLM の返事の形が page_N.json と違う（subtotal が配列、marks が文字列 …）。
    値の問題ではなく形の問題なので、読み直しの FAIL 文言として LLM に返す（読み取り全体は止めない）"""


def _normalise_page(page: dict, page_no: int) -> dict:
    """LLM の出力を page_N.json の形に揃える（値には触らない。欠けたキーを補い、page 番号を固定する）。
    明細の無いページは blocks を空リストにせず 1 つの空ブロックにする（vendor の validate_page は
    `not page.get('blocks')` を FAIL にするため。文言は「空の blocks を書く」だが空リストは通らない）。
    形が違うキーは PageShapeError（読み直しの FAIL 文言になる）"""
    if not isinstance(page, dict):
        raise PageShapeError('page_N.json 全体が JSON オブジェクトでない')
    p = copy.deepcopy(page)   # 浅いコピーだと blocks[].rows の書き換えが元の返事に及び、読み直しの指示文から形の崩れた行が消える（Codex 23）
    p['page'] = page_no
    bad = []
    blocks = p.get('blocks')
    if blocks is not None and not isinstance(blocks, list):
        bad.append('blocks は配列（[{"title": …, "rows": […]}]）')
    elif blocks and not all(isinstance(b, dict) for b in blocks):  # 要素が文字列（行を直に並べた等）なら黙って捨てず読み直し（Codex 24）
        bad.append('blocks の各要素は {"title": …, "rows": […]} のオブジェクト（行は rows の中に書く。文字列や配列を直に並べない）')
    p['blocks'] = [b for b in (blocks or []) if isinstance(b, dict)] if isinstance(blocks, list) else []
    for b in p['blocks']:
        if b.get('rows') is not None and not isinstance(b.get('rows'), list):
            bad.append('blocks[].rows は配列')
            b['rows'] = []
        elif b.get('rows') and not all(isinstance(r, (str, dict)) for r in b['rows']):  # 行は "code|name|method|…" の文字列か dict 行（reading_schema.md）
            bad.append('blocks[].rows の各要素は "code|name|method|parts_no|index|qty|price|wage|flags|comment" の文字列（または {"name": …} のオブジェクト）。配列や数値で書かない')
            b['rows'] = [r for r in b['rows'] if isinstance(r, (str, dict))]
    p['blocks'] = p['blocks'] or [dict(b) for b in EMPTY_BLOCKS]
    for k, shape in (('subtotal', '{"parts": …, "wage": …} のオブジェクト'), ('marks', '{"$": n, "#": n} のオブジェクト')):
        v = p.get(k)
        if v in (None, '', [], {}):
            p[k] = {}
        elif isinstance(v, dict):
            p[k] = dict(v)
        else:
            bad.append(f'{k} は {shape}')
            p[k] = {}
    for k in ('paint_lines', 'expenses'):
        v = p.get(k)
        if not v:
            p.pop(k, None)
        elif not isinstance(v, list):
            bad.append(f'{k} は配列')
            p.pop(k, None)
        elif not all(isinstance(x, dict) for x in v):  # 文字列の要素は vendor の検算が .get() で落ちて読み取り全体が止まる（Codex 指摘）
            bad.append(f'{k} の各要素はオブジェクト（{{"name": …, "amount": …}}。文字列で書かない）')
            p[k] = [x for x in v if isinstance(x, dict)]
    if bad:
        raise PageShapeError('page_' + str(page_no) + '.json の形が違う: ' + ' / '.join(bad))
    return p


def _apply_hint(header: dict, key: str, hint: Optional[dict]) -> None:
    """車検証・サイドバーで分かっている値を、見積書に印字が無い項目にだけ補う（印字があればそちらを残す）"""
    if not hint:
        return
    v = dict(header.get(key) or {})
    for k, val in hint.items():
        cur = v.get(k)
        # 顧客名・所有者欄の「同上」「***」は印字ではなく穴（車検証の値で埋める。使用者欄の '同上' は正しい値。Codex hunt B1）
        if key == 'customer' and k in ('name', 'owner', 'owner_name') and isinstance(cur, str) \
                and (cur.strip() in ('同上', '***', '＊＊＊') or (cur.strip() and set(cur.strip()) <= set('*＊'))):
            cur = ''
        if val not in (None, '') and not cur:
            v[k] = val
    if v:
        header[key] = v


HEADER_LISTS = ((('expenses',), 'object'), (('adas',), 'object'), (('paint', 'lines'), 'object'), (('paint', 'panels'), 'object'),
                (('paint', 'other'), 'object'), (('frame', 'items'), 'object'), (('hints', 'eva_codes'), 'string'), (('hints', 'eva_exclude'), 'string'))
# 塗装の詳細キーはオブジェクト（estimate_schema.md: booth {"index", "wage"}、bumper_front {"method", …}、wax / sealing / … README）。
# 生成器が .get() で読むので、文字列や配列で来たら読み直し（Codex 26）
HEADER_OBJECTS = tuple(('paint', k) for k in ('base', 'booth', 'bumper_front', 'bumper_rear', 'wax', 'sealing', 'door_sash', 'stripe',
                                               'low_cover', 'two_coat_solid', 'two_tone', 'frame'))


def _normalise_header(h: dict, vehicle_hint: Optional[dict], insurance_hint: Optional[dict],
                      customer_hint: Optional[dict] = None) -> dict:
    """header.json の形に揃える。値には触らない（null・空の項目は「書かなかった」として落とすだけ）。
    形が違うキー（配列で来た totals / vehicle / paint、expenses / adas のオブジェクトでない要素）は **落とさず** PageShapeError
    （ask_header が理由を返して読み直させる）。落として続けると、vendor の Checker は totals 無しを WARN にしかしないので
    合計欄の検算なしで合格してしまう（Codex 指摘・実行で再現 2026-09-14）。
    全体がオブジェクトでないものも PageShapeError（実際には llm.parse_json_reply が先に拒み _ask_json が 1 回言い直させる）"""
    if not isinstance(h, dict):
        raise PageShapeError('header.json 全体が JSON オブジェクトでない（{"source": …, "vehicle": {…}, "totals": {…}} の 1 つのオブジェクトで返す。配列や文字列で包まない）')
    out = {k: v for k, v in h.items() if k in prompts.HEADER_KEYS}
    bad = []
    for k in ('totals', 'vehicle', 'customer', 'insurance', 'paint', 'hints', 'discount', 'frame'):
        if k in out and out[k] not in (None, '', []) and not isinstance(out[k], dict):
            bad.append(f'{k} は {{…}} のオブジェクト（配列や文字列で書かない）')
    for k in ('expenses', 'adas'):
        if k in out and out[k] not in (None, '', {}) and not isinstance(out[k], list):
            bad.append(f'{k} は [{{"name": …}}, …] の配列')
    # 入れ子の配列（reading_schema.md）: オブジェクトの配列は vendor の Checker / draft が .get() で読む（文字列が混ざると別プロセスごと落ちる。Codex 22・25）。
    # hints.eva_codes / eva_exclude は文字列の配列
    for path, kind in HEADER_LISTS:
        v = out
        for key in path:
            v = v.get(key) if isinstance(v, dict) else None
        if v in (None, '', []):
            continue
        want = dict if kind == 'object' else str
        if not isinstance(v, list) or not all(isinstance(x, want) for x in v):
            bad.append('.'.join(path) + (' は [{"name": …}, …] のオブジェクトの配列（文字列で書かない）' if kind == 'object' else ' は文字列の配列'))
    for path in HEADER_OBJECTS:
        v = out
        for key in path:
            v = v.get(key) if isinstance(v, dict) else None
        if v not in (None, '', [], {}) and not isinstance(v, dict):
            bad.append('.'.join(path) + ' は {"index": …, "wage": …} のオブジェクト（文字列や配列で書かない）')
    if bad:
        raise PageShapeError('header.json の形が違う: ' + ' / '.join(bad))
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
    _apply_hint(out, 'customer', customer_hint)   # 車検証（使用者・登録番号・住所・有効期限・走行距離）。印字が無い項目にだけ
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
                  customer_hint: Optional[dict] = None,
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
    if n > MAX_PAGES:
        res.error = (f'PDF が {n} ページあります（上限 {MAX_PAGES}）。見積書だけの PDF にして入れてください'
                     f'（ページ数ぶん AI を呼ぶので、誤って大きな PDF を入れると時間と費用がかかります。上限は NEO_READER_MAX_PAGES で変更）')
        return res

    def normalise_or_fail(raw: dict, pg: int) -> tuple:
        """形が違えば (None, FAIL 文言) を返し、読み直しに回す（読み取り全体を止めない）"""
        try:
            return _normalise_page(raw, pg), None
        except PageShapeError as e:
            return None, str(e)

    def ask_header(task_text: str, system: str, whole: dict) -> dict:
        """header.json を読ませる。形が違う（配列で来た totals など）・合計欄が無いときは理由を返して読み直させる
        （ページの検算と同じ。上限 max_retries。直らなければ PageShapeError がそのまま上がり、読み取りは理由付きで不合格 —
        黙って値を落として合計欄の検算なしで進まない。Codex 指摘 2026-09-14）。
        JSON オブジェクトでない返事（配列・文字列）は _ask_json が 1 回言い直させる"""
        def normalise(raw):
            h = _normalise_header(raw, vehicle_hint, insurance_hint, customer_hint)
            if not isinstance(h.get('totals'), dict) or not h['totals']:
                raise PageShapeError('totals（見積書の合計欄）が無い。合計欄は必ず写す（検算の拠り所）')
            return h
        raw = _ask_json(reader, system, [whole, {'type': 'text', 'text': task_text}], usage, 'header.json')
        for attempt in range(max_retries):
            try:
                return normalise(raw)
            except PageShapeError as e:
                _progress(progress, f'合計欄・車両欄の写しに問題があるので読み直しています（{attempt + 1} 回目）: {str(e)[:60]}')
                raw = _ask_json(reader, system, [whole, {'type': 'text', 'text': prompts.header_shape_retry_task(str(e), raw)}], usage, 'header.json')
        return normalise(raw)

    try:
        system = prompts.build_system_prompt()
        whole = llm_mod.document_block(pdf_bytes)
        # 1) header（明細以外）
        _progress(progress, f'合計欄・車両欄を写しています（全 {n} ページ）')
        header = ask_header(prompts.header_task(n, vehicle_hint, source_name, customer_hint), system, whole)
        res.header = header
        # 2) ページごとに写して検算
        pages: list = []
        page_pdfs = [llm_mod.pdf_single_page(pdf_bytes, i) for i in range(n)]
        for i in range(n):
            pg = i + 1
            tr = PageTrace(page=pg)
            blocks = [llm_mod.document_block(page_pdfs[i]), {'type': 'text', 'text': prompts.page_task(pg, n, header)}]
            _progress(progress, f'{pg}/{n} ページ目を写しています')
            raw = _ask_json(reader, system, blocks, usage, f'page_{pg}.json')
            page, shape_err = normalise_or_fail(raw, pg)
            tr.attempts = 1
            v = run('validate', header=header, page=page) if page else {'ok': False, 'fail': [shape_err], 'warn': [], 'rows': 0}
            tr.first_try_ok = bool(v.get('ok'))
            while not v.get('ok') and tr.attempts <= max_retries:
                _progress(progress, f'{pg}/{n} ページ目の検算に落ちたので読み直しています（{tr.attempts} 回目）: {(v.get("fail") or [""])[0][:60]}')
                blocks = [llm_mod.document_block(page_pdfs[i]),
                          {'type': 'text', 'text': prompts.retry_task(pg, v.get('fail') or [], v.get('warn') or [], page if page else raw)}]
                raw = _ask_json(reader, system, blocks, usage, f'page_{pg}.json')
                page, shape_err = normalise_or_fail(raw, pg)
                tr.attempts += 1
                v = run('validate', header=header, page=page) if page else {'ok': False, 'fail': [shape_err], 'warn': [], 'rows': 0}
            if not page:  # 上限まで形が直らなかった。空ページとして持ち、不合格の理由に残す
                page = _normalise_page({'page': pg, 'rows_printed': raw.get('rows_printed') if isinstance(raw, dict) else None, 'blocks': []}, pg)
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
                header = ask_header(prompts.header_retry_task(check['fail'], check.get('warn') or [], header), system, whole)
                res.header = header
            else:
                _progress(progress, '合計欄と合わないので、各ページの写し漏れ・二重写しを確かめています')
                new_pages = []
                for i, page in enumerate(pages):
                    pg = i + 1
                    blocks = [llm_mod.document_block(page_pdfs[i]),
                              {'type': 'text', 'text': prompts.page_totals_retry_task(pg, check['fail'], page)}]
                    p2, shape_err = normalise_or_fail(_ask_json(reader, system, blocks, usage, f'page_{pg}.json'), pg)
                    v = run('validate', header=header, page=p2) if p2 else {'ok': False}
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
