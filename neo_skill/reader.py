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

# 読み込みを始めたときのコードの指紋（ファイルの最後で読み直し、同じ中身のときだけ __app_src_digest__ に控える。読み込みの途中で
# push されたら控えず、古い扱いにして読み直させる。レビュー 3 周目）
try:
    import hashlib as _stamp_hashlib0
    with open(__file__, 'rb') as _stamp_f0:
        _stamp_digest_at_start = _stamp_hashlib0.sha256(_stamp_f0.read()).hexdigest()
    del _stamp_hashlib0, _stamp_f0
except Exception:  # noqa: BLE001
    _stamp_digest_at_start = None

import copy
import dataclasses
import json
import os
import subprocess
import time
import re
import unicodedata
from typing import Callable, Optional

from . import doc_hints as _dh
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
    app_notes: list = dataclasses.field(default_factory=list)  # アプリ側が写しに書いた指定（塗装の実額・M を外した 等）。報告文にも残す

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


# 画面に出してよい行だけを拾う型（例外の名前 / File "...", line N）。ほかの行は見積書の中身が混じりうるので出さない
_SAFE_EXC_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning))\b')
_SAFE_FILE_RE = re.compile(r'^File "([^"]*)", line (\d+)')


def _safe_tail(lines: list) -> str:
    """別プロセスの出力から、**例外の名前とファイル・行番号だけ**を取り出す。
    当てはまる行が無ければ何も出さない —— 元の文には見積書の中身（氏名・登録番号・車台番号）が混じりうるので、
    「分からなかったから生のまま出す」はしない（Codex 第28周）"""
    keep = []
    for raw in (lines or []):
        t = str(raw).strip()
        m = _SAFE_EXC_RE.match(t)
        if m:
            keep.append(m.group(1))
            continue
        m = _SAFE_FILE_RE.match(t)
        if m:
            keep.append(f'{os.path.basename(m.group(1))}:{m.group(2)}')
    if not keep:
        return '（理由は画面に出せません。見積書の中身が混じるため）'
    # 同じものは 1 回だけ・最大 4 つ（順序は出てきた順）
    seen, out = set(), []
    for k in keep:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return ' / '.join(out[:4])[:200]


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
        # 別プロセスの出力には見積書の中身（顧客名・登録番号・車台番号）が混じりうるので、
        # 画面に出す文は**例外の名前と場所だけ**に絞り、数字の並びは伏せる（バグハント 2026-09-20）
        tail = (p.stderr or p.stdout or '').strip().splitlines()[-6:]
        raise RunnerError(f'検算（{op}）が失敗（終了コード {p.returncode}）: ' + _safe_tail(tail))
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


def _ask_once(reader, system: str, blocks: list, usage: _Usage):
    """1 回尋ねる。使えない返事（途中で切れた等）でも、課金された呼び出しは数えてから投げ直す（P15）"""
    try:
        r = reader.ask(system, blocks)
    except llm_mod.LLMReplyError as e:
        if getattr(e, 'reply', None) is not None:
            usage.add(e.reply)
        raise
    usage.add(r)
    return r


def _ask_json(reader, system: str, blocks: list, usage: _Usage, what: str) -> dict:
    """JSON を返させる。壊れた JSON・途中で切れた返事は 1 回だけ言い直させる。断られた（refusal・安全性で止まった）返事は
    言い直しても同じなので、そのまま上げる（header が断られても 2 回呼んでいた。レビュー 2 周目）"""
    try:
        r = _ask_once(reader, system, blocks, usage)
        return llm_mod.parse_json_reply(r.text)
    except llm_mod.LLMReplyError as e:
        if not getattr(e, 'retryable', True):
            raise
        r2 = _ask_once(reader, system, blocks + [{'type': 'text', 'text': f'前回の返事は JSON として読めませんでした（{e}）。{what}を JSON だけで返してください。'}], usage)
        return llm_mod.parse_json_reply(r2.text)


def _ask_page_json(reader, system: str, blocks: list, usage: _Usage, what: str):
    """ページの写しを JSON で返させる。使えない返事なら (None, 理由, やり直せるか) を返し、そのページの読み直し（上限あり）に回す。
    以前は 1 ページの返事が max_tokens で切れたり 2 回続けて JSON でなかったりすると、読み直しの上限を使わずに
    読み取り全体が止まっていた（バグハント 3 回目 P15）。1 回の呼び出しを読み直しの 1 回と数える（内側で言い直させると
    1 ページ最大 8 回・出力 128k トークンを課金していた。レビュー）"""
    try:
        r = _ask_once(reader, system, blocks, usage)
        return llm_mod.parse_json_reply(r.text), None, True
    except llm_mod.LLMReplyError as e:
        return None, f'返事が使えなかった（{str(e)[:120]}）。{what} を JSON だけで、省略せずに返す', bool(getattr(e, 'retryable', True))


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


def _as_int(v) -> Optional[int]:
    """数値・数字の文字列（'2', '45,000', '４５０００', '2.0'）を int に。bool・配列・数字の無い文字列は None"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    if isinstance(v, str):
        t = unicodedata.normalize('NFKC', v).replace(',', '').replace('円', '').strip()
        if re.fullmatch(r'-?\d+(\.0+)?', t):
            return int(float(t))
    return None


_BLANK_MARKS = ('-', '－', '―', '—', 'ー', '―', '―')


def _is_blank_print(cur) -> bool:
    """印字が空白だけ・ハイフンだけ（'－'）は「印字なし」（ヒントで補ってよい。バグハント G9）"""
    if cur in (None, ''):
        return True
    if isinstance(cur, str):
        t = cur.strip()
        return (not t) or t in _BLANK_MARKS
    return False


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
        if b.get('rows') is None:   # "rows": null は空の配列に（注記行の付け替えで None に足さない。レビュー 2026-09-15）
            b['rows'] = []
        if b.get('rows') is not None and not isinstance(b.get('rows'), list):
            bad.append('blocks[].rows は配列')
            b['rows'] = []
        elif b.get('rows') and not all(isinstance(r, (str, dict)) for r in b['rows']):  # 行は "code|name|method|…" の文字列か dict 行（reading_schema.md）
            bad.append('blocks[].rows の各要素は "code|name|method|parts_no|index|qty|price|wage|flags|comment" の文字列（または {"name": …} のオブジェクト）。配列や数値で書かない')
            b['rows'] = [r for r in b['rows'] if isinstance(r, (str, dict))]
    for b in p['blocks']:   # ブロックの小計（blocks[].subtotal）も数値に（Q14）
        if isinstance(b.get('subtotal'), dict):
            _bs = {}
            for kk, vv in b['subtotal'].items():
                if vv in (None, '') or _is_blank_print(vv):
                    continue
                n_ = _as_int(str(vv).replace('¥', '').replace('￥', '').replace(' ', '')) if not isinstance(vv, (int, float)) else _as_int(vv)
                if n_ is None:
                    bad.append(f'blocks[].subtotal.{kk} は数値')
                    continue
                _bs[str(kk)] = n_
            b['subtotal'] = _bs
        elif b.get('subtotal') not in (None, '', {}):
            bad.append('blocks[].subtotal は {"parts": …} のオブジェクト')
            b.pop('subtotal', None)
    p['blocks'] = p['blocks'] or [{'title': '', 'rows': []}]   # 毎回作る（module 定数のリストを共有しない。バグハント G10）
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
    # rows_printed・subtotal・marks の値は数値でないと、vendor の検算が黙って飛ぶ（rows_printed / subtotal）か
    # プロセスごと落ちて読み直しに回らない（marks の int()）。形の FAIL にして読み直させる（バグハント G2/G3）
    rp = p.get('rows_printed')
    if rp not in (None, ''):
        n_ = _as_int(rp)
        if n_ is None:
            bad.append('rows_printed は整数（このページに印字された明細行数）')
        else:
            p['rows_printed'] = n_
    for k in ('subtotal', 'marks'):
        fixed = {}
        for kk, vv in (p.get(k) or {}).items():
            if k == 'marks':
                kk = unicodedata.normalize('NFKC', str(kk)).strip()   # 全角の ＄ ＃ ＊ ＠ を半角に（vendor の flags と同じ。Q7）
            if vv in (None, '') or (k == 'subtotal' and _is_blank_print(vv)):   # 小計の '-'・'－' は「印字なし」（vendor の _num と同じ）
                continue
            if k == 'subtotal' and isinstance(vv, str):
                vv = vv.replace('¥', '').replace('￥', '').replace(' ', '').replace('　', '')   # '¥45,000'・'45 000' は数値（vendor と同じ）
            n_ = _as_int(vv)
            if n_ is None:
                bad.append(f'{k}.{kk} は数値（文字列や配列で書かない）')
                continue
            fixed[str(kk)] = fixed.get(str(kk), 0) + n_ if k == 'marks' else n_
        p[k] = fixed
    if bad:
        raise PageShapeError('page_' + str(page_no) + '.json の形が違う: ' + ' / '.join(bad))
    return p


def _apply_hint(header: dict, key: str, hint: Optional[dict], override: bool = False) -> None:
    """車検証・サイドバーで分かっている値を、見積書に印字が無い項目にだけ補う（印字があればそちらを残す）。
    override=True（サイドバーの事故・保険欄 = 利用者が画面で確かめた値）は印字より優先する（立会工場「写真鑑定」などを読み手の値で
    潰さない。Q13）"""
    if not hint:
        return
    v = dict(header.get(key) or {})
    printed_addr = not _is_blank_print(v.get('address'))   # 見積書に住所の印字があったか（hint を当てる前に見る。レビュー 2026-09-15）
    for k, val in hint.items():
        cur = v.get(k)
        # 顧客名・所有者欄の「同上」「***」は印字ではなく穴（車検証の値で埋める。使用者欄の '同上' は正しい値。Codex hunt B1）
        if key == 'customer' and k in ('name', 'owner', 'owner_name') and isinstance(cur, str) \
                and (cur.strip() in ('同上', '***', '＊＊＊') or (cur.strip() and set(cur.strip()) <= set('*＊'))):
            cur = ''
        if _is_blank_print(cur):
            cur = ''
        if key == 'customer' and k in ('prefecture', 'municipality', 'address_other') and printed_addr:
            continue   # 見積書に住所の印字がある: 車検証の構造化住所（都道府県/市区郡/以降）で上書きしない（印字が正。バグハント I2）
        if val not in (None, '') and (override or not cur):
            v[k] = val
    if v:
        header[key] = v


HEADER_LISTS = ((('expenses',), 'object'), (('adas',), 'object'), (('paint', 'lines'), 'object'), (('paint', 'panels'), 'object'),
                (('paint', 'other'), 'object'), (('frame', 'items'), 'object'), (('hints', 'eva_codes'), 'string'), (('hints', 'eva_exclude'), 'string'))
# 塗装の詳細キーはオブジェクト（estimate_schema.md: booth {"index", "wage"}、bumper_front {"method", …}、wax / sealing / … README）。
# 生成器が .get() で読むので、文字列や配列で来たら読み直し（Codex 26）
HEADER_OBJECTS = tuple(('paint', k) for k in ('base', 'booth', 'bumper_front', 'bumper_rear', 'wax', 'sealing', 'door_sash', 'stripe',
                                               'low_cover', 'two_coat_solid', 'two_tone', 'frame'))


def _strip_weekday(v):
    """日付の後ろの曜日（'2026年09月01日(火)'・'2026/9/1（火）'）を落とす"""
    return re.sub(r'\s*[（(][日月火水木金土祝][）)]\s*$', '', str(v)) if isinstance(v, str) else v


def _normalise_values(out: dict, notes: Optional[list]) -> None:
    """読み手が印字どおりに写した値を、生成器（vendor）が期待する形に揃える（2026-09-15 バグハント 3 回目 Q2/Q3/Q4/Q10）。
    揃えられない値は落とし（車検証・サイドバーの値で補われる）、注意に出す。形そのものが違う値（dict・配列）は形の FAIL で読み直し"""
    def note(msg):
        if notes is not None and msg not in notes:
            notes.append(msg)
    bad = []
    for sec in ('customer', 'insurance', 'vehicle'):
        d = out.get(sec)
        if not isinstance(d, dict):
            continue
        for k in list(d):
            v = d[k]
            if isinstance(v, (dict, list, tuple, set)):
                bad.append(f'{sec}.{k} は文字列か数値（オブジェクトや配列で書かない）')
                continue
            if isinstance(v, str):
                d[k] = _dh._clean(v)   # 改行・制御文字は空白に（AnSvMail.ini の行が増えない）・長さの上限
    if bad:
        raise PageShapeError('header.json の形が違う: ' + ' / '.join(bad))
    cu = out.get('customer') if isinstance(out.get('customer'), dict) else None
    if cu is not None:
        rn = cu.get('reg_no')
        if rn not in (None, ''):   # 登録番号は「地名 分類 かな 一連」（一連はハイフン・「・」を外す。生成器は区切り付きを受けず 4 欄が空になっていた。Q2）
            parts = _dh._split_reg(str(rn))
            if all(parts):
                cu['reg_no'] = ' '.join(parts)
            else:
                cu.pop('reg_no', None)
                # 登録番号の値そのものは画面・報告文に載るので出さない（顧客情報。バグハント 2026-09-20）
                note('見積書の登録番号を地名・分類番号・かな・一連番号に分けられないので、車検証の値（あれば）を使う')
        if cu.get('kilometer') not in (None, ''):
            km = _dh.parse_km(cu['kilometer'])
            if km.strip('0'):
                cu['kilometer'] = km.lstrip('0')
            else:
                cu.pop('kilometer', None)   # 0・読めない走行距離は車検証の値（あれば）で補う（以前は 0 を空とみなしていた）
        if cu.get('term_date') not in (None, ''):
            d8 = _dh.date8_full(_strip_weekday(cu['term_date']))
            if d8:
                cu['term_date'] = d8
            else:
                cu.pop('term_date', None)
        if cu.get('postal') not in (None, ''):
            pc = _dh.postal_text(cu['postal'])
            if pc:
                cu['postal'] = pc
            else:
                cu.pop('postal', None)
    ins = out.get('insurance') if isinstance(out.get('insurance'), dict) else None
    if ins is not None:   # 日付は 8 桁（'2026/09/01' のまま渡すと XML の事故日が '2026//0/9/' になっていた。Q3）
        for k, yy in (('accident_date', True), ('presence_date', False), ('garage_in', False), ('garage_out', False)):
            if ins.get(k) not in (None, ''):
                _raw_d = ins[k]
                d8 = _dh.date8_full(_strip_weekday(_raw_d), allow_yy=yy)
                if d8:
                    ins[k] = d8
                else:
                    ins.pop(k, None)
                    note(f'insurance.{k}「{str(_raw_d)[:16]}」を日付として読めないので使わない')
        if ins.get('repair_days') not in (None, ''):
            # 日数だけ受ける（'7'・'7日'・'約7日間'）。'2週間'・'1ヶ月' を 2・1 日にしない（レビュー）
            _m = re.fullmatch(r'\s*(?:約)?\s*(\d+)\s*(?:日|日間)?\s*', unicodedata.normalize('NFKC', str(ins['repair_days'])))
            rd_ = int(_m.group(1)) if _m else None
            if rd_ is None or rd_ < 0:
                note(f'insurance.repair_days「{str(ins.get("repair_days"))[:16]}」を日数として読めないので使わない')
                ins.pop('repair_days', None)
            else:
                ins['repair_days'] = rd_
    pt = out.get('paint') if isinstance(out.get('paint'), dict) else None
    if pt is not None:   # 塗装の金額・割合は数値（'55%'・'61,245円' のまま渡すと生成器が float() で落ちていた。Q4）
        for k in ('total', 'material'):
            if pt.get(k) not in (None, ''):
                n_ = _as_int(str(pt[k]).replace('¥', '').replace('￥', '').replace(' ', '')) if not isinstance(pt[k], bool) else None
                if n_ is None:
                    raise PageShapeError(f'header.json の形が違う: paint.{k} は数値（「{str(pt[k])[:16]}」は読めない）')
                pt[k] = n_
        if pt.get('material_rate') not in (None, ''):
            m_ = re.search(r'\d+(?:\.\d+)?', unicodedata.normalize('NFKC', str(pt['material_rate'])))
            if m_:
                pt['material_rate'] = float(m_.group(0))
            else:
                pt.pop('material_rate', None)
    tt = out.get('totals') if isinstance(out.get('totals'), dict) else None
    if tt is not None:   # 工場書式の円未満計上などの許容（neo_total / tolerance）は人が決めるもの。読み手からは受けない（O10）
        for k in ('neo_total', 'tolerance', 'tolerance_reason'):
            if k in tt:
                tt.pop(k, None)
                note(f'読み手が書いた totals.{k} は使わない（合計の許容は人が reading に書くもの）')


def _normalise_header(h: dict, vehicle_hint: Optional[dict], insurance_hint: Optional[dict],
                      customer_hint: Optional[dict] = None, notes: Optional[list] = None) -> dict:
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
    # 文字列の欄が dict / 配列で来たら文字列に潰す（issuer が {"name": …} で来ると vendor が落ちる。バグハント G1）
    for k in ('source', 'issuer', 'format', 'note'):
        v = out.get(k)
        if isinstance(v, dict):
            out[k] = ' '.join(str(x) for x in v.values() if x not in (None, ''))
        elif isinstance(v, list):
            out[k] = ' '.join(str(x) for x in v if x not in (None, ''))
        elif v is not None and not isinstance(v, str):
            out[k] = str(v)
    # est_date は YYYYMMDD の 8 桁（生成器は est_date[:4] を年として使うので、数値や '2026/9/13'・和暦のままだと落ちる。バグハント G1）
    if out.get('est_date') not in (None, ''):
        d8 = _dh.date8_full(str(out['est_date']))   # 年月だけ（日 00）は '' → 形の FAIL（レビュー 2026-09-15）
        if len(d8) == 8:
            out['est_date'] = d8
        else:
            bad.append('est_date は YYYYMMDD の 8 桁（例 20260913。和暦や区切り付きの印字は変換して書く。無ければ空）')
    # labor_rate は数値（'8,000円' は 8000 に。数字が無ければ書かなかった扱い = 生成器が逆算）
    if out.get('labor_rate') not in (None, ''):
        lr = _as_int(out['labor_rate'])
        if lr is None:
            m_ = re.search(r'\d[\d,]*', unicodedata.normalize('NFKC', str(out['labor_rate'])))
            lr = int(m_.group(0).replace(',', '')) if m_ else None
        if lr and lr > 0:
            out['labor_rate'] = lr
        else:
            out.pop('labor_rate', None)
    # wage_round / tax_round は読み手が推測で書いても使わない（印字の工賃と合計欄から vendor が判定する。index_policy と同じ歯止め。G5）
    for k in ('wage_round', 'tax_round'):
        if out.get(k) not in (None, ''):
            if notes is not None:
                notes.append(f'読み手が書いた {k}={out[k]!r} は使わず、印字の工賃・合計欄から判定した（推測で書くとコグニの設定が変わる）')
            out.pop(k, None)
    # target_total（協定額）は人が reading に書く指示。読み手が写すと生成器が協定調整を始め、合わなければ「協定額に合っていない」で
    # 不合格になる（files f66558f の関門）。アプリの読み手からは受けない。discount は印字された値引き・割増の欄なので残す（レビュー 2026-09-15）
    if out.get('target_total') not in (None, '', {}, []):
        if notes is not None:
            notes.append('読み手が書いた target_total は使わない（協定額は人が reading に書くもの。見積書どおりに作る）')
        out.pop('target_total', None)
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
    _normalise_values(out, notes)
    for k in list(out):
        if out[k] in (None, '', [], {}):
            out.pop(k)
    _apply_hint(out, 'vehicle', vehicle_hint)
    _apply_hint(out, 'insurance', insurance_hint, override=True)   # サイドバーの値（利用者が確かめた値）を優先
    _apply_hint(out, 'customer', customer_hint)   # 車検証（使用者・登録番号・住所・有効期限・走行距離）。印字が無い項目にだけ
    return out


def _is_note_row(r) -> bool:
    """注記行か（vendor の draft_estimate._expand_row・reading_check と同じ基準）。文字列行は 9 番目の欄（flags）を NFKC・大文字にして N、
    dict 行は flags に N（NFKC）か「note があって name が無い」。note 付きでも name のある dict 行は明細（レビュー 2026-09-15: 明細を注記と
    取り違えるとページごと外れて merge が落ちていた）"""
    if isinstance(r, str):
        parts = [x.strip() for x in r.split('|')]
        return len(parts) > 8 and 'N' in unicodedata.normalize('NFKC', parts[8]).upper()
    if isinstance(r, dict):
        fl = unicodedata.normalize('NFKC', str(r.get('flags') or '')).upper()
        return 'N' in fl or (bool(r.get('note')) and not r.get('name'))
    return False


def _has_rows(page: dict) -> bool:
    """明細（注記行以外）が 1 行でもあるか。注記行だけのページを pages/ に残すと vendor の Checker が
    「小計があるのに行が無い」で必ず不合格にする（バグハント G4）"""
    return any(not _is_note_row(r) for b in (page.get('blocks') or []) if isinstance(b, dict) for r in (b.get('rows') or []))


def _note_rows(page: dict) -> list:
    return [r for b in (page.get('blocks') or []) if isinstance(b, dict) for r in (b.get('rows') or []) if _is_note_row(r)]


def _pages_for_merge(header: dict, pages: list) -> tuple:
    """pages/ に書く形にする。明細の無いページ（表紙・計算書）は pages/ に置かない —
    vendor の reading_check.Checker は `pages` に載っていて行の無いページを FAIL にするため
    （validate_page の案内「rows_printed: 0 と空の blocks」とは食い違う。人が写した実案件も明細ページだけを 1 から数えている）。
    そのページに塗装行・費用だけがあれば header の paint.lines / expenses に移す（merge が繋ぐ先と同じ）。
    残ったページは 1 から番号を付け直す（ファイル名 page_N.json と中身の page を merge が突き合わせる）。
    検算・読み直し・画面表示は PDF の実ページ番号のまま行い、ここでは書き出す形だけを変える"""
    hdr = copy.deepcopy(header)
    out = []
    pending_lists: dict = {}   # 明細の無いページの塗装行・費用（最初の明細ページより前にあったもの）
    pending_notes: list = []   # 明細の無いページの注記行（装備注記）: 直前の明細ページの末尾に繋ぐ（無ければ次の明細ページの先頭）
    for p in pages:
        if _has_rows(p):
            q = copy.deepcopy(p)
            if pending_notes and q.get('blocks'):
                q['blocks'][0].setdefault('rows', [])[0:0] = pending_notes
                pending_notes = []
            out.append(q)
            continue
        notes_ = copy.deepcopy(_note_rows(p))
        if notes_:
            if out and out[-1].get('blocks'):
                out[-1]['blocks'][-1].setdefault('rows', []).extend(notes_)
            else:
                pending_notes.extend(notes_)
        for _lk in ('paint_lines', 'expenses'):   # 直前の明細ページの後ろへ（無ければ次の明細ページの前へ）。header に移すと紙と逆順になっていた（Q6）
            if p.get(_lk):
                if out:
                    out[-1].setdefault(_lk, []).extend(copy.deepcopy(p[_lk]))
                else:
                    pending_lists.setdefault(_lk, []).extend(copy.deepcopy(p[_lk]))
    if pending_lists and out:
        for _lk, _vals in pending_lists.items():
            out[0][_lk] = _vals + list(out[0].get(_lk) or [])
    elif pending_lists:   # 明細のページが 1 つも無い（塗装・費用だけの見積）: 従来どおり header へ
        if pending_lists.get('paint_lines'):
            paint = hdr.get('paint') if isinstance(hdr.get('paint'), dict) else {}
            paint.setdefault('lines', []).extend(pending_lists['paint_lines'])
            hdr['paint'] = paint
        if pending_lists.get('expenses'):
            hdr.setdefault('expenses', []).extend(pending_lists['expenses'])
    for i, p in enumerate(out, 1):
        p['page'] = i
    return hdr, out


# コグニの区分語彙（estimate_to_neo.DISPOSAL のうちコグニ帳票で使われる語 ＋ 書式 B の「塗装」）。「部品」は日産系 FAX（書式 C）の目印なので入れない
_COGNI_METHODS = {'取替', '脱着', '修理', '脱着修理', '脱着板金', '脱着鈑金', '板金', '鈑金', '点検', '調整', '点検調整', '分解調整', '塗装'}


def _row_methods(pages: list) -> set:
    """pages の明細行（"code|name|method|…" の文字列か dict）から区分の集合（NFKC・空白除去。空欄は数えない）"""
    out: set = set()
    for p in pages or []:
        if not isinstance(p, dict):
            continue
        for b in p.get('blocks') or []:
            if not isinstance(b, dict):
                continue
            for r in b.get('rows') or []:
                if isinstance(r, str):
                    parts = r.split('|')
                    m = parts[2] if len(parts) > 2 else ''
                elif isinstance(r, dict):
                    m = r.get('method') or ''
                else:
                    continue
                m = unicodedata.normalize('NFKC', str(m)).replace(' ', '').strip()
                if m:
                    out.add(m)
    return out


def _index_policy_guard(header: dict, pages: list) -> Optional[str]:
    """読み手が index_policy=manual と書いたが、区分の語彙がコグニのものなら auto に戻す（戻す理由の文を返す。戻さないなら None）。
    manual は書式 C（日産系 FAX。区分に「部品」等、コグニと違う語彙）だけ（reading_schema.md / format_catalog.md）。指数欄が空欄・
    技術料だけの見積で manual にすると、連動・吸収の標準が 0 になる行が手入力工賃（標準なし）で書かれ、スキル（Claude）の読みと
    NEO が変わる（2026-09-15 スペーシア FAX 見積: ヘッドランプ・フードヒンジの標準指数が消えた）。区分にコグニ以外の語（部品・交換・
    取付・修正 など）が 1 つでもあれば読み手の判断を残す。区分が全行空欄（作業区分の列が無い書式 F など）は「語彙が違う」証拠ではないので外す"""
    if str(header.get('index_policy') or '').strip().lower() != 'manual':
        return None
    if str(header.get('format') or '').strip().upper()[:1] == 'C':   # 書式 C（日産系 FAX）は manual が正（format_catalog）。「部品」行が無くても外さない（Q12）
        return None
    methods = _row_methods(pages)
    if not methods <= _COGNI_METHODS:
        return None
    vocab = ('区分が ' + '・'.join(sorted(methods)) + ' のコグニ語彙') if methods else '区分の印字が無い'
    return ('index_policy=manual を外して auto にした（' + vocab + '。manual は日産系 FAX のように'
            '区分の語彙が違う書式だけ。指数欄が空欄でも技術料だけでも、コグニ語彙なら標準指数と突き合わせて写す）')


_VIN_RE = re.compile(r'[A-HJ-NPR-Z0-9]{17}')
# 輸入車のしるし（vendor の estimate_to_neo.imported_signal と同じ語。このプロセスでは vendor を import しないので写しを持つ）
_IMPORT_BRANDS = ('ボルボ', 'VOLVO', 'BMW', 'ベンツ', 'メルセデス', 'MERCEDES', 'アウディ', 'AUDI', 'フォルクスワーゲン', 'VOLKSWAGEN',
                  'ポルシェ', 'PORSCHE', 'プジョー', 'PEUGEOT', 'ルノー', 'RENAULT', 'シトロエン', 'CITROEN', 'フィアット', 'FIAT',
                  'アルファロメオ', 'ジープ', 'JEEP', 'ランドローバー', 'ジャガー', 'JAGUAR', 'テスラ', 'TESLA')


def _is_true(v) -> bool:
    return v is True or (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 1) \
        or str(v).strip().lower() in ('true', '1', 'yes', 'y', 'on')


def _looks_imported(veh: dict) -> bool:
    s = re.sub(r'[\s\-‐−]', '', unicodedata.normalize('NFKC', str(veh.get('serial_no') or ''))).upper()
    if _VIN_RE.fullmatch(s):
        return True
    txt = unicodedata.normalize('NFKC', ' '.join(str(veh.get(k) or '') for k in ('car_name', 'maker', 'maker_name', 'name'))).upper()
    return any(unicodedata.normalize('NFKC', b).upper() in txt for b in _IMPORT_BRANDS)


def _row_code(r) -> str:
    if isinstance(r, str):
        return unicodedata.normalize('NFKC', r.split('|')[0]).strip()
    if isinstance(r, dict):
        return unicodedata.normalize('NFKC', str(r.get('code') or '')).strip()
    return ''


# 塗装の「内訳」（これがあるときはアプリから実額を勧めない）。**lines（塗装行）は入れない** —
# 塗装行はパネルに当てられれば下書きがパネルを作り（その場合は生成器が実額を無視して指数のままにする）、
# 当てられなければ一括計上になる。当てられなかった一括計上こそ実額にしたいので、行の有無では決めない（2026-09-20）
_PAINT_DETAIL_KEYS = ('panels', 'bumper_front', 'bumper_rear', 'bumper_base', 'frame', 'sealing', 'other',
                      'base', 'booth', 'wax', 'door_sash', 'stripe', 'low_cover', 'two_coat_solid', 'two_tone', 'auto_panels')


# スキル（vendor draft_estimate._flag）が真として読む語。人が書いた読み取りの真偽値欄をここでも同じに読む
_VENDOR_TRUE = ('1', 'true', 'yes', 'y', 'on', '有り', 'あり', '有', 'はい', 'する', '要', '○', '◯', '●')


def _vendor_true(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    return unicodedata.normalize('NFKC', str(v or '')).strip().lower() in _VENDOR_TRUE


def _paint_actual_guard(header: dict, pages: list) -> tuple:
    """塗装は**常に**コグニの入力方式「実額」で入れる（このアプリの方針。2026-09-20 亮平さん指示）。
    戻り値は (header, 付けた理由の文 or None)。header は直したときだけ複製を返す（読み手の写しは書き換えない）。

    実額は総額を 1 つの金額で入れる方式で、**塗装費用 ＋ 材料代**をまとめて書く（下書きが内訳を畳み、
    生成器が材料代を総額に足す）。実額にしないと NEO に「塗装費用(工場見積)」という**印字に無い行**ができる。
    金額の合計は動かない（印字の塗装計と同じ額が 1 つの欄に入るだけ）。

    付けない: 読み取りに `input_type`（指数・参考）や `actual` の指定があるとき（人の指定が優先）／
    `auto_panels`（協定でパネル別の内訳を求められる案件）のとき／塗装がどこにも無いとき。
    なお**ベタ打ち（Addata なし）は別の経路**なので、ここは効かない（ベタ打ちは印字どおりそのまま）"""
    def _int(v) -> int:
        # totals は paint.total のようには正規化されていないので、「104,660円」「¥104,660」「全角」も読む
        # （読めずに 0 になると、印字の総額があるのに使わない道に落ちる。Codex 第38周）
        try:
            s = re.sub(r'[^0-9.\-]', '', unicodedata.normalize('NFKC', str(v if v is not None else 0)))
            return int(float(s or 0))
        except (TypeError, ValueError):
            return 0

    paint = header.get('paint') if isinstance(header.get('paint'), dict) else {}
    totals = header.get('totals') if isinstance(header.get('totals'), dict) else {}
    # ここで見ているのは**読み手（AI）が書いた写し**なので、`input_type` や `auto_panels` が入っていても
    # それは人の指定ではない。AI の気まぐれで実額になったりならなかったりすると、同じ見積書から違う NEO が出る。
    # このアプリは**常に実額**なので、読み手が何を書いていても上書きする（バグハント 2026-09-20）。
    # 人が指定する道（スキルで reading.json を直に書く）は vendor 側で今までどおり尊重される
    _said = str(paint.get('input_type') or '').strip()
    _said_auto = _vendor_true(paint.get('auto_panels'))
    _over = (f'読み手が書いた入力方式「{_said}」' if _said else '') + ('・auto_panels' if _said_auto else '')
    n_lines = len(paint.get('lines') or []) + sum(len((p or {}).get('paint_lines') or [])
                                                  for p in (pages or []) if isinstance(p, dict))
    has_detail = any(paint.get(k) for k in _PAINT_DETAIL_KEYS)
    total = _int(paint.get('total')) or _int(totals.get('paint')) or _int(totals.get('paint_total'))
    if total <= 0 and not n_lines and not has_detail:
        return header, None            # 塗装がどこにも無い
    hdr = copy.deepcopy(header)
    _p = dict(hdr.get('paint') or {}, input_type='実額')
    _p.pop('actual', None)
    _p.pop('auto_panels', None)          # 読み手が書いていても、このアプリでは実額に寄せる
    hdr['paint'] = _p
    mat = _int(paint.get('material')) or _int(totals.get('material'))
    # 【ここで内訳を畳まない】2026-09-21 に「印字の塗装費用計から実額 1 本を作る」のをここでやってみたが、
    # **下書きより手前で塗装の内訳を消すと、下書きが明細から塗装パネルを作り直して足す**（明細 → 塗装パネルの
    # 自動連動）。本番と同じ読み取りで +10,549 円（＝ 明細 4600 の塗装 9,590 × 1.1）ずれた。
    # 畳むのは内訳を組み立て終えた**下書きの中**（スキルの `_force_actual_paint`）が正しい場所。
    # このアプリがここで決めるのは**入力方式を必ず実額にすること**までで、総額の決め方は下書きに任せる。
    # 印字の「塗装費用計」は `totals.paint_total` として読ませ（prompts.py）、下書きがその額から畳む。
    # NEO に実際に入った塗装計は、報告文の注記（app.py `_paint_note_with_real_total`）で必ず示す
    amt = f'（塗装費用 {total:,} 円' + (f' ＋ 材料代 {mat:,} 円' if mat else '') + '）' if total > 0 else ''
    return hdr, (f'塗装はコグニの入力方式を**実額**にする{amt}。'
                 '塗装費用と材料代をまとめて総額 1 つで入れる（印字に無い「塗装費用(工場見積)」の行を作らない）'
                 + (f'。{_over}は使わない（このアプリは常に実額）' if _over else ''))


def _manual_rows_guard(header: dict, pages: list) -> tuple:
    """読み手が明細に付けた M（手入力）を外す。戻り値は (pages, 外した理由の文 or None)。
    M を付けてよいのは ADDATA に無い品目だけで（判断規則 10-7）、ADDATA を見られない読み手には決められない。
    部品コードの印字が 1 つも無い見積（協定見積書・他システムの見積）に「コード欄が空 = 手入力」（書式 A の規則）を当てはめ、
    2 ページ目の 16 行が部品コード無しの手入力行になった（2026-09-20 本番 フリード協定見積。前の回は同じ行に部品コードが付いた
    = 読み取りの揺れで NEO が変わっていた）。外しても金額は印字のまま。ADDATA で照合できない行は下書きが手入力にする。
    外さない: 汎用車種・輸入車（全行手入力）/ 部品コードの印字がある見積（コードの無い行は工場のコグニでも手入力行）/
    書式 B（素材欄の * = 手入力品目が印字されている）"""
    veh = header.get('vehicle') if isinstance(header.get('vehicle'), dict) else {}
    if _is_true(veh.get('generic')) or _looks_imported(veh):
        return pages, None
    if str(header.get('format') or '').strip().upper()[:1] == 'B':
        return pages, None
    rows = [r for p in (pages or []) if isinstance(p, dict) for b in (p.get('blocks') or []) if isinstance(b, dict)
            for r in (b.get('rows') or [])]
    # 部品コードの印字 = 4 桁（枝番付き 0010-02 も）。行番号や OCR のかけらの数字 1 つでは「コードのある見積」にしない（Codex 指摘）
    if any(re.fullmatch(r'\d{4}(?:\s*-\s*\d{1,2})?', _row_code(r)) for r in rows if not _is_note_row(r)):
        return pages, None
    out = copy.deepcopy(pages)
    n = 0
    for p in out:
        if not isinstance(p, dict):
            continue
        for b in p.get('blocks') or []:
            if not isinstance(b, dict):
                continue
            rs = b.get('rows') or []
            for i, r in enumerate(rs):
                if _is_note_row(r):
                    continue
                if isinstance(r, str):
                    parts = r.split('|')
                    if len(parts) > 8 and 'M' in unicodedata.normalize('NFKC', parts[8]).upper():
                        parts[8] = re.sub('[Mm]', '', unicodedata.normalize('NFKC', parts[8]))
                        rs[i] = '|'.join(parts)
                        n += 1
                elif isinstance(r, dict):
                    hit = False
                    fl = unicodedata.normalize('NFKC', str(r.get('flags') or ''))
                    if 'M' in fl.upper():
                        r['flags'] = re.sub('[Mm]', '', fl)
                        hit = True
                    if 'manual' in r:
                        hit = hit or _is_true(r.get('manual'))
                        r.pop('manual')
                    n += 1 if hit else 0
    if not n:
        return pages, None
    return out, (f'読み手が手入力（M）にした明細 {n} 行を部品の照合に戻した（部品コードの印字が無い見積では「コード欄が空 = 手入力」は'
                 '当てはまらない。ADDATA に有るかは下書きが決め、照合できない行は手入力にする。金額は印字のまま）')


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
    guard_notes: list = []   # index_policy を戻した理由（check.warn に載せて画面の「読み取りの注意」に出す）
    res = ReadResult(False, case_dir)
    t0 = time.time()
    try:
        pdf_bytes = llm_mod.pdf_prepare(pdf_bytes)   # 権限パスワードだけの暗号化 PDF は暗号を外す（P6）
        n = llm_mod.pdf_page_count(pdf_bytes)
    except llm_mod.LLMError as e:
        res.error = str(e)
        return res
    except Exception as e:  # noqa: BLE001
        # 画面にそのまま出る文なので、何をすればよいかが分かる日本語にする（2026-09-20 実画面のバグハント）
        res.error = f'PDF を開けません（ファイルが壊れているか、PDF ではないようです。{type(e).__name__}）'
        return res
    _lim = int(getattr(reader, 'max_pdf_bytes', 0) or 0)
    if _lim and len(pdf_bytes) > _lim:   # 上限を超える PDF は必ず失敗するのに、全ページ分をメモリに展開して送っていた（P8）
        res.error = (f'PDF が大きすぎます（{len(pdf_bytes) / 1048576:.1f}MB。この読み手の上限は {_lim / 1048576:.0f}MB）。'
                     'スキャンの解像度を下げるか、見積書のページだけの PDF にして入れてください')
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
            h = _normalise_header(raw, vehicle_hint, insurance_hint, customer_hint, notes=guard_notes)
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
        _refused = False   # 読み直せない理由（断られた等）で不合格のページがある
        page_pdfs = [llm_mod.pdf_single_page(pdf_bytes, i) for i in range(n)]
        for i in range(n):
            pg = i + 1
            tr = PageTrace(page=pg)
            blocks = [llm_mod.document_block(page_pdfs[i]), {'type': 'text', 'text': prompts.page_task(pg, n, header)}]
            _progress(progress, f'{pg}/{n} ページ目を写しています')
            raw, reply_err, can_retry = _ask_page_json(reader, system, blocks, usage, f'page_{pg}.json')
            page, shape_err = normalise_or_fail(raw, pg) if raw is not None else (None, reply_err)
            raw = raw if raw is not None else {}
            tr.attempts = 1
            v = run('validate', header=header, page=page) if page else {'ok': False, 'fail': [shape_err], 'warn': [], 'rows': 0}
            tr.first_try_ok = bool(v.get('ok'))
            while not v.get('ok') and tr.attempts <= max_retries and can_retry:
                _progress(progress, f'{pg}/{n} ページ目の検算に落ちたので読み直しています（{tr.attempts} 回目）: {(v.get("fail") or [""])[0][:60]}')
                blocks = [llm_mod.document_block(page_pdfs[i]),
                          {'type': 'text', 'text': prompts.retry_task(pg, v.get('fail') or [], v.get('warn') or [], page if page else raw)}]
                raw, reply_err, can_retry = _ask_page_json(reader, system, blocks, usage, f'page_{pg}.json')
                page, shape_err = normalise_or_fail(raw, pg) if raw is not None else (None, reply_err)
                raw = raw if raw is not None else {}
                tr.attempts += 1
                v = run('validate', header=header, page=page) if page else {'ok': False, 'fail': [shape_err], 'warn': [], 'rows': 0}
            if not v.get('ok') and not can_retry:
                _refused = True
            if not page:  # 上限まで形が直らなかった。空ページとして持ち、不合格の理由に残す
                page = _normalise_page({'page': pg, 'rows_printed': _as_int(raw.get('rows_printed')) if isinstance(raw, dict) else None, 'blocks': []}, pg)
            tr.ok, tr.rows, tr.fail, tr.warn = bool(v.get('ok')), int(v.get('rows') or 0), list(v.get('fail') or []), list(v.get('warn') or [])
            pages.append(page)
            res.traces.append(tr)
        res.pages = pages
        _g = _index_policy_guard(header, pages)
        if _g:
            header = {k: v for k, v in header.items() if k != 'index_policy'}; res.header = header
            if _g not in guard_notes:
                guard_notes.append(_g)
        # 読み手の M を外すのは書き出す写しだけ（pages は読み手の写しのまま残す。あとで header を読み直して書式 B・輸入車に
        # 変わったら、外した M を戻せるように毎回元から掛け直す。Codex 指摘）
        _pages_w, m_note = _manual_rows_guard(header, pages)
        _hdr_w, _pages_m = _pages_for_merge(header, _pages_w)
        _hdr_w, p_note = _paint_actual_guard(_hdr_w, _pages_m)   # 塗装が一式だけなら実額で入れる（2026-09-20）
        maker.write_pages(case_dir, _hdr_w, _pages_m)
        # 3) 束ねて合計欄を検算
        m = run('merge', case_dir=case_dir)
        rd, check = m.get('reading'), (m.get('check') or {})
        res.merge_messages = list(m.get('messages') or [])
        rounds = 0
        # 断られたページがあると読み取りは必ず不合格なので、合計欄の読み直し（API の呼び出し）はしない（header・全ページを
        # もう一度呼び、最後も断られていた。レビュー 2 周目）
        while rd and check.get('fail') and rounds < 2 and not _refused:
            rounds += 1
            if rounds == 1:
                _progress(progress, '合計欄と合わないので、合計欄・費用・塗装の写しを読み直しています')
                header = ask_header(prompts.header_retry_task(check['fail'], check.get('warn') or [], header, (rd or {}).get('tax_included')), system, whole)
                res.header = header
            else:
                _progress(progress, '合計欄と合わないので、各ページの写し漏れ・二重写しを確かめています')
                new_pages = []
                for i, page in enumerate(pages):
                    pg = i + 1
                    blocks = [llm_mod.document_block(page_pdfs[i]),
                              {'type': 'text', 'text': prompts.page_totals_retry_task(pg, check['fail'], page, (rd or {}).get('tax_included'))}]
                    p2, shape_err = normalise_or_fail(_ask_json(reader, system, blocks, usage, f'page_{pg}.json'), pg)
                    v = run('validate', header=header, page=p2) if p2 else {'ok': False}
                    if v.get('ok'):
                        new_pages.append(p2)
                        res.traces[i].attempts += 1
                    else:  # 読み直しで検算が落ちるなら前回の（合格していた）写しを残す
                        new_pages.append(page)
                pages = new_pages
                res.pages = pages
            _g = _index_policy_guard(header, pages)
            if _g:
                header = {k: v for k, v in header.items() if k != 'index_policy'}; res.header = header
                if _g not in guard_notes:
                    guard_notes.append(_g)
            _pages_w, m_note = _manual_rows_guard(header, pages)
            _hdr_w, _pages_m = _pages_for_merge(header, _pages_w)
            _hdr_w, p_note = _paint_actual_guard(_hdr_w, _pages_m)
            maker.write_pages(case_dir, _hdr_w, _pages_m)
            m = run('merge', case_dir=case_dir)
            rd, check = m.get('reading'), (m.get('check') or {})
            res.merge_messages = list(m.get('messages') or [])
        res.app_notes = [n for n in (list(guard_notes) + ([m_note] if m_note else []) + ([p_note] if p_note else [])) if n]   # 納品する報告文にも残す（画面だけに出して消えないように。2026-09-20 本番のバグハント）
        _extra = list(guard_notes) + ([m_note] if m_note else []) + ([p_note] if p_note else []) + [m_ for m_ in (res.merge_messages or []) if m_]   # merge の注意（重複行など）も合格時に見える所へ（G11）。M の注意は最後に書き出した写しの分だけ
        if _extra:   # 戻した理由は合計欄の検算の注意と同じ列に（app は check.warn を「読み取りの注意」に出す）
            check = dict(check or {})
            _w0 = list(check.get('warn') or [])
            check['warn'] = _w0 + [x for i_, x in enumerate(_extra) if x not in _w0 and x not in _extra[:i_]]   # 重複を出さない
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

# 読み込んだときのコードの指紋（app.sync_app_modules が「メモリのコードがディスクと同じか」を見る。読み込みの時点で
# 控えないと、あとから初めて import したモジュールが「古い」と見なされ、偽の版ずれで変換を断っていた。バグハント 3 回目 N2）。
# ファイルの最後に置く: 読み直しが途中で例外になったときは古い指紋のまま残り、版ずれとして断れる（先頭に置くと
# 途中までしか新しくないモジュールを「揃った」と見ていた。レビュー 2026-09-15）
try:
    import hashlib as _stamp_hashlib
    with open(__file__, 'rb') as _stamp_f:
        _stamp_now = _stamp_hashlib.sha256(_stamp_f.read()).hexdigest()
    if _stamp_now == globals().get('_stamp_digest_at_start'):
        __app_src_digest__ = _stamp_now
    del _stamp_hashlib, _stamp_f, _stamp_now
except Exception:  # noqa: BLE001
    pass
