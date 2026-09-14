# -*- coding: utf-8 -*-
"""reading_pages.py — 見積書を「ページ単位」で写し、ページごとに検算してから reading.json に束ねる。

読み取り（Claude の転記）が最も時間と誤りを生む工程なので、1 ページ写すたびに機械検算し、落ちたページだけ読み直す。

案件フォルダ（NEO_check/<案件>/）の構成:
    pages/header.json   明細以外のすべて（source / issuer / est_date / format / vehicle / customer / insurance / labor_rate / paint / expenses / totals / target_total / hints …）
    pages/page_1.json   1 ページ目の明細: {"page": 1, "rows_printed": 18, "subtotal": {"parts": 363370, "wage": 63800}, "marks": {"$": 1, "#": 3},
                                            "blocks": [{"title": "フロントバンパー", "rows": ["code|name|method|parts_no|index|qty|price|wage|flags|comment", ...]}],
                                            "paint_lines": [...], "expenses": [...]}   ← paint_lines / expenses はそのページに印字されていれば
    pages/page_2.json   …
    pages/status.json   検算結果（reading_pages が書く）
    reading.json        merge が作る（header + 全ページの blocks（page 付き）+ pages 小計）

使い方（files ディレクトリで）:
    python .claude/skills/pdf-to-neo/scripts/reading_pages.py init <案件フォルダ> --pages 3      # header.json と page_N.json の雛形を作る（既存は上書きしない）
    python .claude/skills/pdf-to-neo/scripts/reading_pages.py validate <案件フォルダ> [--page N] # ページ単位の検算（行数・小計・印の数・数量×単価・左右・工賃丸め）
    python .claude/skills/pdf-to-neo/scripts/reading_pages.py merge <案件フォルダ> [--force]     # 全ページ合格なら reading.json を作り、全体の検算（合計欄）も行う
    python .claude/skills/pdf-to-neo/scripts/reading_pages.py status <案件フォルダ>              # どのページが未検算 / 不合格か

ページの不変条件（validate が見るもの）:
  - rows_printed（そのページに印字された明細行数。注記行は数えない）= 転記した行数
  - subtotal.parts / subtotal.wage（ページ小計が印字されていれば）= 転記した金額 / 工賃の合計（保留行を除く）
  - marks（印字の $ # * @ の数）= 転記した flags の数
  - 行ごと: unit×qty=price、指数×レートの丸め、左右、短縮記法の列数
  小計の印字が無いページは rows_printed だけで検算し、合計欄との突合は merge（全体）で行う。
終了コード: 0 = 合格 / 1 = FAIL あり（validate は対象ページのいずれか、merge は全体）
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
from reading_check import Checker, _int  # noqa: E402

HEADER_KEYS = ('source', 'issuer', 'est_date', 'format', 'vehicle', 'customer', 'insurance', 'labor_rate', 'wage_round', 'index_policy', 'hints',
               'paint', 'expenses', 'totals', 'target_total', 'discount', 'frame', 'adas', 'tax_round', 'note')
PAGE_RE = re.compile(r'^page_(\d+)\.json$')


def pages_dir(case: str) -> str:
    return os.path.join(case, 'pages')


def page_files(case: str) -> list[tuple[int, str]]:
    d = pages_dir(case)
    if not os.path.isdir(d):
        return []
    out = []
    for f in os.listdir(d):
        m = PAGE_RE.match(f)
        if m:
            out.append((int(m.group(1)), os.path.join(d, f)))
    return sorted(out)


def load_json(path: str) -> dict:
    with open(path, encoding='utf-8-sig') as fh:  # BOM 付きでも読む
        return json.load(fh)


def save_json(path: str, data) -> None:
    tmp = f'{path}.{os.getpid()}.tmp'  # 並行実行で一時ファイルを共有しない
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------- init
def cmd_init(case: str, n_pages: int) -> int:
    d = pages_dir(case)
    os.makedirs(d, exist_ok=True)
    hdr = os.path.join(d, 'header.json')
    if not os.path.exists(hdr):
        save_json(hdr, {
            'source': '', 'issuer': '', 'est_date': '', 'format': '',
            'vehicle': {'model_code': '', 'serial_no': '', 'desig': '', 'category': '', 'reg_date': '', 'color_code': ''},
            'customer': {'name': '', 'reg_no': ''}, 'insurance': {'company': ''},
            'labor_rate': None,
            'paint': {}, 'expenses': [],
            'totals': {'parts': None, 'wage': None, 'paint': None, 'material': None, 'expense': None, 'taxable': None, 'tax': None, 'total': None},
        })
        print('作成:', hdr)
    for i in range(1, n_pages + 1):
        p = os.path.join(d, f'page_{i}.json')
        if os.path.exists(p):
            continue
        save_json(p, {'page': i, 'rows_printed': None, 'subtotal': {}, 'marks': {}, 'blocks': [{'title': '', 'rows': []}]})
        print('作成:', p)
    print('次: header.json を埋め、page_1.json から順に写す → 各ページを写すたびに validate --page N')
    return 0


# ---------------------------------------------------------------------- validate
def _page_reading(header: dict, page: dict) -> dict:
    """1 ページ分を reading.json の形にする（合計欄は付けない。ページ小計は pages に）"""
    rd = {k: copy.deepcopy(header[k]) for k in HEADER_KEYS if k in header and k not in ('totals', 'target_total', 'paint', 'expenses')}
    pg = int(page.get('page') or 0)
    rd['blocks'] = [dict(b, page=pg) for b in page.get('blocks') or []]
    sub = dict(page.get('subtotal') or {})
    if page.get('rows_printed') is not None:
        sub['rows'] = page['rows_printed']
    if page.get('marks'):
        sub['marks'] = page['marks']
    rd['pages'] = {str(pg): sub}
    # ページ側の塗装行・費用には page を付ける（Checker がページ小計の別解「明細 + 塗装行 + 費用」に使う）
    rd['paint'] = {'lines': [dict(l, page=pg) if isinstance(l, dict) else l for l in page.get('paint_lines') or []]} if page.get('paint_lines') else {}
    rd['expenses'] = [dict(e, page=pg) if isinstance(e, dict) else e for e in page.get('expenses') or []]
    if header.get('labor_rate'):
        rd['labor_rate'] = header['labor_rate']
    return rd


def validate_page(header: dict, page: dict) -> dict:
    """ページ単位の検算。戻り値 {'ok', 'fail', 'warn', 'rows'}"""
    res = {'ok': False, 'fail': [], 'warn': [], 'rows': 0}
    pg = page.get('page')
    if not isinstance(pg, int) or pg <= 0:
        res['fail'].append('page（ページ番号）が正の整数でない')
        return res
    if not page.get('blocks'):
        res['fail'].append('blocks が無い（明細の無いページは rows_printed: 0 と空の blocks を書く）')
    rows_printed = page.get('rows_printed')
    if rows_printed is None:
        res['fail'].append('rows_printed（このページに印字された明細行数。注記行は数えない）が無い。まず行数を数えて書く')
    rd = _page_reading(header, page)
    ck = Checker(rd)
    ck.load_rows()
    if not ck.rows and rows_printed:
        res['fail'].append(f'明細行が 0 行（rows_printed は {rows_printed}）')
    if ck.rows:
        labor = _int(rd.get('labor_rate')) or 0  # 印字どおり '8,000' でも落ちない
        from draft_estimate import detect_wage_round, infer_labor_rate  # noqa: E402
        lines = list((rd.get('paint') or {}).get('lines') or [])
        labor = labor or infer_labor_rate(ck.rows + lines)
        wage_round = _int(header.get('wage_round')) or detect_wage_round(ck.rows + lines, labor)
        ck.check_rows(labor, wage_round)
        ck.check_sides()
        ck.check_subtotals()
        ck.check_expenses()
    unverified = [r_ for r_ in ck.rows if str(r_.get('comment') or '').startswith('OCR未確認')]
    if unverified:
        res['fail'].append(f"OCR 下書きのまま未確認の行が {len(unverified)} 行（行{unverified[0]['_no']} ほか）。画像を見て名称と数値を確認し、comment の「OCR未確認」を消す")
    r = ck.result()
    res['fail'] += [t for t in r['fail'] if t != '明細行が 1 行も無い（blocks[].rows）' or rows_printed]
    res['warn'] += r['warn']
    res['rows'] = len(ck.rows)
    res['ok'] = not res['fail']
    return res


def cmd_validate(case: str, only: int | None) -> int:
    d = pages_dir(case)
    hdr_path = os.path.join(d, 'header.json')
    if not os.path.exists(hdr_path):
        print('pages/header.json が無い（reading_pages.py init で雛形を作る）'); return 1
    header = load_json(hdr_path)
    files = page_files(case)
    if not files:
        print('pages/page_N.json が無い'); return 1
    status_path = os.path.join(d, 'status.json')
    try:
        status = load_json(status_path) if os.path.exists(status_path) else {}
    except ValueError:
        status = {}
    bad = 0
    for pg, path in files:
        if only is not None and pg != only:
            continue
        try:
            page = load_json(path)
        except ValueError as e:
            res = {'ok': False, 'fail': [f'JSON として読めない: {e}'], 'warn': [], 'rows': 0}
        else:
            if page.get('page') != pg:
                page = dict(page, page=pg) if page.get('page') is None else page
            res = validate_page(header, page)
            if page.get('page') != pg:
                res['fail'].append(f"ファイル名のページ {pg} と中身の page {page.get('page')} が違う")
                res['ok'] = False
        status[str(pg)] = {'ok': res['ok'], 'fail': res['fail'], 'warn': res['warn'], 'rows': res['rows'],
                           'mtime': os.path.getmtime(path), 'checked': datetime.datetime.now().isoformat(timespec='seconds')}
        print(f"ページ {pg}: {'合格' if res['ok'] else '不合格'}（明細 {res['rows']} 行）")
        for t in res['fail']:
            print('  FAIL:', t)
        for t in res['warn']:
            print('  WARN:', t)
        bad += 0 if res['ok'] else 1
    save_json(status_path, status)
    return 1 if bad else 0


# ---------------------------------------------------------------------- merge
def merge(case: str, force: bool = False) -> tuple[dict | None, list[str]]:
    """全ページを検算し、合格なら reading.json の dict を返す。戻り値 (reading または None, メッセージ)"""
    msgs: list[str] = []
    d = pages_dir(case)
    hdr_path = os.path.join(d, 'header.json')
    if not os.path.exists(hdr_path):
        return None, ['pages/header.json が無い']
    header = load_json(hdr_path)
    files = page_files(case)
    if not files:
        return None, ['pages/page_N.json が無い']
    nums = [pg for pg, _ in files]
    if nums != list(range(1, len(nums) + 1)):  # 欠番は --force でも束ねない（ページの欠けた reading.json を作らない）
        msgs.append(f'ページ番号が連続していない: {nums}（欠けたページを写すか、番号を直す。--force でも束ねない）')
        return None, msgs
    rd: dict = {k: copy.deepcopy(header[k]) for k in HEADER_KEYS if k in header}
    rd['blocks'] = []
    rd['pages'] = {}
    paint = dict(rd.get('paint') or {})
    lines = list(paint.get('lines') or [])
    expenses = list(rd.get('expenses') or [])
    src_lines = ['header'] * len(lines)  # 各行がどこ（header / ページ番号）から来たか（重複検知用）
    src_exp = ['header'] * len(expenses)
    failed = []
    hard = []  # 構造エラー（壊れた JSON・ページ番号不一致）: --force でも束ねない（ページが欠けた reading.json を作らない）
    for pg, path in files:
        try:
            page = load_json(path)
        except ValueError as e:
            hard.append(pg)
            msgs.append(f'ページ {pg}: JSON として読めない: {e}')
            continue
        if page.get('page') is None:
            page['page'] = pg
        elif page.get('page') != pg:  # ファイル名と中身のページ番号が違う（コピーして直し忘れ）: そのまま束ねると順序と小計がずれる
            hard.append(pg)
            msgs.append(f"ページ {pg}: ファイル名のページ {pg} と中身の page {page.get('page')} が違う（中身を直す）")
            continue
        res = validate_page(header, page)
        if not res['ok']:
            failed.append(pg)
            msgs.append(f"ページ {pg}: 不合格 " + ' / '.join(res['fail']))
        for b in page.get('blocks') or []:
            rd['blocks'].append(dict(b, page=pg))
        sub = dict(page.get('subtotal') or {})
        if page.get('rows_printed') is not None:
            sub['rows'] = page['rows_printed']
        if page.get('marks'):
            sub['marks'] = page['marks']
        rd['pages'][str(pg)] = sub
        for _l in page.get('paint_lines') or []:
            lines.append(_l); src_lines.append(pg)
        for _e in page.get('expenses') or []:
            expenses.append(_e); src_exp.append(pg)
    if hard:
        msgs.append(f'ページ {hard} は読めないか番号が違う。--force でも束ねない（直してから再実行）')
        return None, msgs
    if failed and not force:
        msgs.append(f'不合格のページ {failed} を読み直す（reading_pages.py validate --page N で個別に検算）')
        return None, msgs
    # OCR の下書きのまま人が確認していない行は、--force でも束ねない（見ずに通す経路を塞ぐ。Codex 指摘 2026-09-12）
    _unseen = [(b.get('title', ''), (r.get('name', '') if isinstance(r, dict) else str(r)[:20]))
               for b in (rd.get('blocks') or []) for r in (b.get('rows') or [])
               if (isinstance(r, dict) and str(r.get('comment') or '').startswith('OCR未確認'))
               or (isinstance(r, str) and 'OCR未確認' in r)]  # 短縮記法 "code|name|...|OCR未確認" の行も（Codex 指摘）
    if _unseen:
        msgs.append(f'OCR 未確認の行が {len(_unseen)} 行ある（例 {_unseen[0]}）。画像を見て確認し comment の「OCR未確認」を消してから束ねる。--force でも通さない')
        return None, msgs
    for label, seq, src in (('費用', expenses, src_exp), ('塗装行', lines, src_lines)):
        seen: dict = {}
        for it, where in zip(seq, src):
            key = json.dumps(it, ensure_ascii=False, sort_keys=True)
            seen.setdefault(key, []).append(where)
        for key, wheres in seen.items():
            if len(wheres) > 1:
                msgs.append(f'{label}の同じ行が {len(wheres)} 回ある（出所 {wheres}）: {key[:80]}。各ページに繰り返し印字される合計欄を写していないか確かめる（二重計上になる）')
    # 重複検出（上）は page 無しのキーで済ませてから、ページ由来の行に page を付ける（Checker のページ小計の別解に使う。header 由来は付けない）
    lines = [dict(l, page=w) if (isinstance(l, dict) and w != 'header') else l for l, w in zip(lines, src_lines)]
    expenses = [dict(e, page=w) if (isinstance(e, dict) and w != 'header') else e for e, w in zip(expenses, src_exp)]
    if lines:
        paint['lines'] = lines
    if paint:
        rd['paint'] = paint
    rd['expenses'] = expenses
    rd['_merged_from'] = {'pages': len(files), 'at': datetime.datetime.now().isoformat(timespec='seconds')}
    return rd, msgs


def cmd_merge(case: str, force: bool) -> int:
    rd, msgs = merge(case, force)
    for m in msgs:
        print(m)
    if rd is None:
        return 1
    out = os.path.join(case, 'reading.json')
    save_json(out, rd)
    print(f"reading.json を作成: {out}（{len(rd['blocks'])} ブロック / {len(rd['pages'])} ページ）")
    res = Checker(rd).run()  # 全体（合計欄・費用・工場設定）の検算
    for t in res['fail']:
        print('FAIL:', t)
    for t in res['warn']:
        print('WARN:', t)
    print(f"reading_check: FAIL {len(res['fail'])} / WARN {len(res['warn'])} / 明細 {res['rows']} 行 / 推定 {res['settings']}")
    return 1 if res['fail'] else 0


def cmd_status(case: str) -> int:
    d = pages_dir(case)
    files = page_files(case)
    if not files:
        print('pages/ が無い'); return 1
    status_path = os.path.join(d, 'status.json')
    try:
        status = load_json(status_path) if os.path.exists(status_path) else {}
    except ValueError:
        status = {}
    bad = 0
    for pg, path in files:
        st = status.get(str(pg))
        if not st:
            print(f'ページ {pg}: 未検算'); bad += 1
        elif st.get('mtime') != os.path.getmtime(path):
            print(f'ページ {pg}: 検算後に変更あり（再 validate）'); bad += 1
        elif not st.get('ok'):
            print(f"ページ {pg}: 不合格 → {' / '.join(st.get('fail') or [])}"); bad += 1
        else:
            print(f"ページ {pg}: 合格（明細 {st.get('rows')} 行）")
    rj = os.path.join(case, 'reading.json')
    if os.path.exists(rj) and any(os.path.getmtime(p) > os.path.getmtime(rj) for _, p in files):
        print('reading.json はページより古い（merge が必要）')
    return 1 if bad else 0


def pages_newer_than_reading(case: str) -> bool:
    """pages/ があり、reading.json が無いかページ/ヘッダより古ければ True（make_neo が merge を呼ぶ判断）"""
    files = page_files(case)
    if not files:
        return False
    rj = os.path.join(case, 'reading.json')
    if not os.path.exists(rj):
        return True
    hdr = os.path.join(pages_dir(case), 'header.json')
    srcs = [p for _, p in files] + ([hdr] if os.path.exists(hdr) else [])
    return any(os.path.getmtime(p) > os.path.getmtime(rj) for p in srcs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['init', 'validate', 'merge', 'status'])
    ap.add_argument('case_dir')
    ap.add_argument('--pages', type=int, default=1)
    ap.add_argument('--page', type=int, default=None)
    ap.add_argument('--force', action='store_true', help='merge: 不合格ページがあっても reading.json を作る（理由が説明できるときだけ）')
    a = ap.parse_args()
    case = os.path.abspath(a.case_dir)
    if not os.path.isdir(case):
        if a.cmd == 'init' and os.path.isdir(os.path.dirname(case)):  # init は案件フォルダごと作る（親が無いときは打ち間違いとみて止める）
            os.makedirs(case, exist_ok=True)
            print('案件フォルダを作成:', case)
        else:
            print('案件フォルダが無い:', case
                  + ('（親フォルダも無い。パスを確かめる）' if a.cmd == 'init' else '。先に reading_pages.py init で作る'))
            return 1
    if a.cmd == 'init':
        return cmd_init(case, a.pages)
    if a.cmd == 'validate':
        return cmd_validate(case, a.page)
    if a.cmd == 'merge':
        return cmd_merge(case, a.force)
    return cmd_status(case)


if __name__ == '__main__':
    sys.exit(main())
