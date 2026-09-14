# -*- coding: utf-8 -*-
"""draft_estimate.py — 見積書を「印字のまま」写した reading.json から、判断規則を適用した estimate.json を作る。

使い方（files ディレクトリで）:
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/draft_estimate.py <reading.json> [<estimate.json>]

reading.json の形は reference/reading_schema.md。Claude（人）は PDF を見たまま写すだけでよく、次の判断はこのスクリプトが行う:
  - 明細の ref 決定（品番一致 → 部位ブロック内の名称照合 → 全体の名称照合）。ブロック見出し（【フロントバンパー】等）を部位文脈に使う
  - 左右ペア部品に左右指定の無い数量 2n 行 → 左 n・右 n の 2 行に分割（コグニは左右別行）
  - 板金行: 損傷面積（名称の "(5dm²)" か area）と指数から BANKIN.DB を逆引きしてランク（bankin.yes）を決める
  - 装備（hints.eva_codes）: 見積の品番が、この車のグレードでその装備レターの行にしか無いとき採用（11.DB / 13.DB / 83.DB）
  - 塗装行の解釈（パネル名 → 20.DB コード、修正 1/3 → ratio、加算基礎数値 → base、バンパ 取替 → bumper_front 新品 …）
  - 費用の kind（見積書のどの合計に入っているか）、レバーレートの逆算、totals の補完
  - 数量 1 のまま複数個分の金額が印字された小物（クリップ 1,300 円 = 標準 100 円 × 13 個）は数量を直す（金額はそのまま。判断規則 10-21）
  - 品番の無い行で標準単価が合わないとき、名称の近い部品のうち単価が合うものへ部品コードを直す（判断規則 10-21）
  - 「〃 交換工賃」の行（技術料だけの続き行）は直前の部品の行にまとめる。技術料だけの書式でレートが決まらないときは ADDATA の標準指数で絞る
決めきれなかった点は `_draft_notes` と標準出力に出す（inspect_estimate.py が同じ点を ★ で再掲する）。
人が確かめる点（転記メモ・数量や部品コードを直した行・価格の食い違い）は `_review` に集め、make_neo.py が確認箇所シート（xlsx）にする。
reading の `comment` は転記メモなので NEO には書かない（NEO の明細コメントに出すのは `neo_comment` だけ）。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import unicodedata
from typing import Optional

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402
skill_env.apply()  # ADDATA / NEO_check / 雛形 の場所を環境変数に（PC ごとの設定ファイルと自動検出）
FILES = skill_env.FILES  # リポジトリ root（ジャンクション経由・別フォルダからの実行でも解決）
sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))

from estimate_to_neo import _code4, _fit, AddataParts, BUMPER_DISPOSAL, DISPOSAL, NeoBuilder, bankin_time, hw, material_default, r10, BUMPER_ONLY_KEYS, is_manual_panel  # noqa: E402
from paint_index import PaintIndex  # noqa: E402

BANKIN_YES = {'A': [1, 1, 1], 'B': [1, 0, 0], 'C': [0, 0, 0]}
PART_NAMES_JSON = os.path.join(HERE, '..', 'reference', 'part_code_names.json')  # build_part_names.py が作る 部品名 → 部品コード 辞書（全車種 12.DB の集計）
_PART_NAMES: Optional[dict] = None


def part_names() -> dict:
    """辞書 {'names': {正規化名: {code: 件数}}, 'codes': {code: [名称…]}}。無ければ空（build_part_names.py で作る）"""
    global _PART_NAMES
    if _PART_NAMES is None:
        try:
            _PART_NAMES = json.load(open(PART_NAMES_JSON, encoding='utf-8-sig'))
        except (OSError, ValueError):
            _PART_NAMES = {}
    return _PART_NAMES
EXTRA_ALIASES = [('ﾌｴﾝﾀﾞｰﾗｲﾅｰ', 'ﾌｴﾝﾀﾞｽﾌﾟﾗﾂｼﾕｼｰﾙﾄﾞ'), ('ﾌｴﾝﾀﾞﾗｲﾅ', 'ﾌｴﾝﾀﾞｽﾌﾟﾗﾂｼﾕｼｰﾙﾄﾞ'), ('ﾌｴﾝﾀﾞﾗｲﾅ', 'ﾌｴﾝﾀﾞﾌﾟﾛﾃｸﾀ'),
                 ('ﾌﾛﾝﾄｶﾞﾗｽ', 'ｳｲﾝﾄﾞｼｰﾙﾄﾞｶﾞﾗｽ'), ('ﾃｰﾙﾗﾝﾌﾟ', 'ﾘﾔｺﾝﾋﾞﾈｰｼﾖﾝﾗﾝﾌﾟ'), ('ﾊﾞﾝﾊﾟｰ', 'ﾊﾞﾝﾊﾟ')]  # トヨタ: フェンダライナ = フェンダスプラッシュシールド
SMALL_WORDS = ('ｸﾘﾂﾌﾟ', 'ﾘﾃｰﾅ', 'ｸﾞﾛﾒﾂﾄ', 'ｽｸﾘﾕ', 'ﾎﾞﾙﾄ', 'ﾅﾂﾄ', 'ﾋﾟｰｽ', 'ｼｰﾙ', 'ｶﾊﾞｰ', 'ﾌﾞﾗｹﾂﾄ', 'ｽﾃｰ', 'ｸﾂｼﾖﾝ', 'ﾊﾟﾂﾄﾞ', 'ﾓｰﾙ', 'ｼｰﾙﾄﾞ', 'ｶﾞｰﾄﾞ', 'ﾌﾟﾛﾃｸﾀ')


def _nfkc(s) -> str:
    return unicodedata.normalize('NFKC', str(s or ''))


def _clean_panel_line(n: str) -> str:
    """コグニ印刷の塗装明細の書き方（'Rrﾊﾟﾈﾙ修理20d㎡(1/2)' — NFKC 後は ㎡ が m2）を「名前 修理 1/2」の形に寄せる。
    面積（塗装面積 dm²。20.DB のパネル面積とは別物なので捨てる）と括弧付きの比率を外す（2026-09-14）"""
    s = re.sub(r'[（(]\s*(1/[123])\s*[）)]', r'\1', n)                 # (1/2) → 1/2
    s = re.sub(r'[0-9]+(?:\.[0-9]+)?\s*d?m[2²]', '', s)                 # 20d㎡ / 121dm² / 20dm2
    s = re.sub(r'[0-9]+(?:\.[0-9]+)?\s*d㎡', '', s)
    return s.strip()


def _hw_kana(s: str) -> str:
    """全角カナ → 半角カナ（NFKC は半角→全角なので逆変換を自前で）"""
    t = _nfkc(s)
    out = []
    for ch in t:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6 or ch in 'ー・':
            out.append(_FW2HW.get(ch, ch))
        else:
            out.append(ch)
    return ''.join(out)


_FW = 'ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロワヲンヴー・'
_HW = ['ｧ', 'ｱ', 'ｨ', 'ｲ', 'ｩ', 'ｳ', 'ｪ', 'ｴ', 'ｫ', 'ｵ', 'ｶ', 'ｶﾞ', 'ｷ', 'ｷﾞ', 'ｸ', 'ｸﾞ', 'ｹ', 'ｹﾞ', 'ｺ', 'ｺﾞ', 'ｻ', 'ｻﾞ', 'ｼ', 'ｼﾞ', 'ｽ', 'ｽﾞ', 'ｾ', 'ｾﾞ', 'ｿ', 'ｿﾞ',
       'ﾀ', 'ﾀﾞ', 'ﾁ', 'ﾁﾞ', 'ｯ', 'ﾂ', 'ﾂﾞ', 'ﾃ', 'ﾃﾞ', 'ﾄ', 'ﾄﾞ', 'ﾅ', 'ﾆ', 'ﾇ', 'ﾈ', 'ﾉ', 'ﾊ', 'ﾊﾞ', 'ﾊﾟ', 'ﾋ', 'ﾋﾞ', 'ﾋﾟ', 'ﾌ', 'ﾌﾞ', 'ﾌﾟ', 'ﾍ', 'ﾍﾞ', 'ﾍﾟ', 'ﾎ', 'ﾎﾞ', 'ﾎﾟ',
       'ﾏ', 'ﾐ', 'ﾑ', 'ﾒ', 'ﾓ', 'ｬ', 'ﾔ', 'ｭ', 'ﾕ', 'ｮ', 'ﾖ', 'ﾗ', 'ﾘ', 'ﾙ', 'ﾚ', 'ﾛ', 'ﾜ', 'ｦ', 'ﾝ', 'ｳﾞ', 'ｰ', '･']
_FW2HW = dict(zip(_FW, _HW))


def _num(v):
    """印字を写した数値: '83,400' / '２．００' / '¥1,000' / 83400 → 数字だけの文字列（空欄・'-' は ''）"""
    if v in (None, ''):
        return ''
    t = _nfkc(str(v)).replace(',', '').replace(' ', '').replace('¥', '').replace('円', '').strip()
    return '' if t in ('-', '−', '—', '―', '‐') else t


def _flag(v, name, default=False):
    """人が書いた JSON の真偽値欄を厳密に読む。判断できない値は ValueError。
    文字列 "false" は空でないので `if v:` だと真になってしまう"""
    if v is None or v == '':
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v in (0, 1):
            return bool(v)
        raise ValueError(f'{name}: true / false で指定する（{v!r}）')
    t = unicodedata.normalize('NFKC', str(v)).strip().lower()
    if not t:          # 空白だけは「書いていない」と同じ
        return default
    if t in ('1', 'true', 'yes', 'y', 'on', '有り', 'あり', '有', 'はい', 'する', '要', '○', '◯', '●'):
        return True
    if t in ('0', 'false', 'no', 'n', 'off', '無し', 'なし', '無', 'いいえ', 'しない', '不要', '×', 'x', '✕'):
        return False
    raise ValueError(f'{name}: true / false で指定する（{v!r}）')


def _money_or(v, default=0, name='金額'):
    """金額・数値を寛容に読む（カンマ・全角・通貨記号）。空欄は default、数字にならない文字列は ValueError（'12OOO' のような写し間違いを 0 円にしない）"""
    t = _num(v)
    if t == '':
        return default
    try:
        return int(float(t))
    except ValueError:
        raise ValueError(f'{name}が数値でない: {v!r}（写し間違いか、単位や記号が混ざっている）')


def _side20(name20: str) -> str:
    """12.DB / 11.DB の 20 文字名の左右: 1 文字目が L/R のときだけ（2 文字目の F/R は前後。' Rﾊﾞﾝﾊﾟｸﾘﾂﾌﾟ' はリヤの無印部品）"""
    s = str(name20 or '')
    return s[:1] if s[:1] in ('L', 'R') else ''


def _fr20(name20: str) -> str:
    """11.DB の 20 文字名の前後: 2 文字目が F/R（'LFﾄﾞｱﾊﾟﾈﾙ' の F、' Rﾊﾞﾝﾊﾟﾌｴｲｽ' の R）"""
    s = str(name20 or '')
    return s[1:2] if s[1:2] in ('F', 'R') else ''


def _fr_of(name: str) -> str:
    """見積名称の前後: 'Fﾊﾞﾝﾊﾟ' 'ﾌﾛﾝﾄﾄﾞｱ' 'LFﾄﾞｱ' → 'F'、'Rrﾊﾞﾝﾊﾟ' 'ﾘﾔﾄﾞｱ' 'RRﾄﾞｱ' → 'R'。
    先頭の左右記号（RH / LH / 右 / 左）は前後ではないので先に外す。単独の 'R'（Rﾄﾞｱ）は右かリヤか決められないので '' """
    t = _nfkc(_hw_kana(name)).strip()
    t = re.sub(r'^(RH|LH|R/H|L/H|右|左)[\s.]*', '', t)
    if re.search(r'ﾌﾛﾝﾄ|フロント|前', t) or re.match(r'^[LR]?F(?![A-Za-z])', t) or re.match(r'^[LR]?Fr(?![a-z])', t):
        return 'F'
    if re.search(r'ﾘﾔ|ﾘｱ|リヤ|リア|後', t) or re.match(r'^[LR]?Rr(?![a-z])', t) or re.match(r'^[LR]R(?![A-Za-z])', t):
        return 'R'
    return ''


def _side_of(name: str) -> str:
    """名称・見出しの先頭の左右記号: RH / R/H / R. / R / R/F / 右 → 'R'、LH / L/H / L. / L / L/F / 左 → 'L'（'Rr'（リヤ）は左右ではない）"""
    t = re.sub(r'^[\s【\[（(]+', '', _nfkc(name)).strip()
    if re.match(r'^(RH|R/H|右)', t) or re.match(r'^R(?![a-z])[.\s/]', t) or re.match(r'^R[FR](?![a-z])', t):  # RF/RR = 右前/右後（Rr は後ろ）
        return 'R'
    if re.match(r'^(LH|L/H|左)', t) or re.match(r'^L(?![a-z])[.\s/]', t) or re.match(r'^L[FR](?![a-z])', t):
        return 'L'
    m = re.search(r'[\s(（](RH|LH|右|左)[)）]?\s*$', t)  # 末尾の左右（'ﾘﾔｺﾝﾋﾞﾈｰｼｮﾝﾗﾝﾌﾟ LH'、'ﾄﾞｱﾐﾗｰ(右)'）
    if m:
        return 'R' if m.group(1) in ('RH', '右') else 'L'
    return ''


def _clean_name(name: str) -> str:
    """'RH ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ ﾎｰﾙｶﾊﾞｰ' → '右 ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ ﾎｰﾙｶﾊﾞｰ'、面積 '(5dm²)' を除く。半角カナに統一"""
    t = _hw_kana(name).strip()
    t = re.sub(r'^(RH|R/H|R(?![a-z])[.\s/])\s*', '右', t)
    t = re.sub(r'^(LH|L/H|L(?![a-z])[.\s/])\s*', '左', t)
    t = re.sub(r'\(\s*[\d.]+\s*d?m?[²2]?\s*\)', '', t)
    return t.strip()


def _round_half_up(x: float) -> int:
    """四捨五入（Python の round は 4.5 → 4 の偶数丸めなので使わない）"""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _area_of(name: str, row: dict) -> Optional[int]:
    """板金面積 d㎡（整数）。'4.5' のような小数は四捨五入する（切り捨てるとランク判定が 1 段軽くなる）"""
    if row.get('area') is not None:
        try:
            return _round_half_up(float(row['area']))
        except (TypeError, ValueError):
            return None
    m = re.search(r'\(\s*([\d.]+)\s*d?m?[²2]?\s*\)', _nfkc(name))
    return _round_half_up(float(m.group(1))) if m else None


def _dcode(method: str, price, wage) -> int:
    m = (method or '').strip()
    m2 = _nfkc(m).replace('鈑', '板').replace(' ', '')
    default = 0 if (price or 0) > 0 else (1 if (wage or 0) > 0 else 0)
    return DISPOSAL.get(m, DISPOSAL.get(m2, default))


METHOD_NAME = {0: '取替', 1: '脱着', 2: '修理', 3: '脱着修理', 4: '点検調整', 5: '分解調整', 6: '板金'}


ROW_FIELDS = ('code', 'name', 'method', 'parts_no', 'index', 'qty', 'price', 'wage', 'flags', 'comment')


PARTS_NAME_BYTES = 24   # ERParts.PartsName TEXT(24)（コグニの明細の名称欄。cp932 のバイト数）
TEXT30_BYTES = 30       # 顧客名・工場名など（Customer.Name1 / Insurance.ConsultantFactory / 管理領域）
COMPANY_ABBR = (('株式会社', '(株)'), ('有限会社', '(有)'), ('合同会社', '(同)'), ('合資会社', '(資)'), ('合名会社', '(名)'),
                ('一般社団法人', '(一社)'), ('一般財団法人', '(一財)'), ('医療法人', '(医)'), ('社会福祉法人', '(福)'))


def _cp932_len(s) -> int:
    return len(str(s or '').encode('cp932', 'replace'))


def shorten_name(name: str, max_bytes: int = PARTS_NAME_BYTES) -> str:
    """NEO の名称欄（cp932 で max_bytes バイト）に入らない手入力行の名称を、意味をなるべく残して短くする。
    入るならそのまま。句読点・括弧を半角 → 空白を詰める → 末尾の括弧書きを外す → それでも長ければバイト数で切る（2026-09-13 ベンツ: 作業名 13 行が途中で切れていた）"""
    t = hw(str(name or '')).strip()
    if _cp932_len(t) <= max_bytes:
        return t
    t = t.translate(str.maketrans({'、': ',', '。': '.', '（': '(', '）': ')', '　': ' ', '・': '･', '，': ','}))
    if _cp932_len(t) > max_bytes:
        t = re.sub(r'\s+', '', t)
    while _cp932_len(t) > max_bytes and re.search(r'\([^()]*\)\s*$', t):
        t = re.sub(r'\([^()]*\)\s*$', '', t).rstrip()
    return _fit(t, max_bytes)


def abbr_company(s: str, max_bytes: int = TEXT30_BYTES) -> str:
    """会社名が欄（cp932 で max_bytes バイト）に入らないとき、株式会社 → (株) のように略す（コグニで人が打つ形）。入るならそのまま"""
    t = str(s or '')
    if _cp932_len(t) <= max_bytes:
        return t
    for a, b in COMPANY_ABBR:
        t = t.replace(a, b)
    if _cp932_len(t) > max_bytes:
        t = t.translate(str.maketrans({'（': '(', '）': ')', '　': ' '}))
    m = re.match(r'^(.*?)\s*((?:TEL\s*)?[0-9０-９][0-9０-９\-－]{8,})$', t)
    if _cp932_len(t) > max_bytes and m:  # 「工場名 電話番号」: 電話番号は残し、名前の側を詰める（電話番号が途中で切れると使えない）
        name_, tel = m.group(1).strip(), m.group(2)
        room = max_bytes - _cp932_len(tel) - 1
        if room >= 8:
            t = f'{_fit(name_, room).rstrip()} {tel}'
    return t


NEO_COMMENT_RE = re.compile(r'^\s*(NEO|ＮＥＯ)\s*[:：]\s*', re.I)


def _split_neo_comment(d: dict) -> dict:
    """comment 欄の先頭が 'NEO:' なら、それは見積書に印字された明細コメント（コグニの明細コメントとして NEO に書く）→ neo_comment に移す。
    それ以外の comment は転記メモ（NEO には書かず確認箇所シートへ。2026-09-13 亮平さん指示）"""
    c = str(d.get('comment') or '')
    m = NEO_COMMENT_RE.match(c)  # 全角の ＮＥＯ： も受ける。本文は写したまま（NFKC を掛けると半角カナが全角に変わる）
    if m:
        d['neo_comment'] = c[m.end():].strip()
        d.pop('comment', None)
    return d


def expand_row(row) -> dict:
    """reading.json の行は dict か、'code|name|method|parts_no|index|qty|price|wage|flags|comment' の文字列（転記の手間を減らす短縮記法）。
    空欄は空文字。flags: M=manual、R=reserve、N=注記行（name を note にする）。数値は int/float に変換。
    comment は転記メモ（NEO に書かない）。見積書に印字された明細コメントは 'NEO:※JAS在庫使用' のように先頭に NEO: を付ける（dict 行は neo_comment でも可）"""
    return _split_neo_comment(_expand_row(row))


def _expand_row(row) -> dict:
    if isinstance(row, dict):
        d_ = dict(row)
        fl = _nfkc(str(d_.pop('flags', '') or '')).upper()
        if fl:  # dict 行でも短縮記法と同じ意味・同じ検証（M 手入力 / R 保留 / N 注記 / 印）
            bad_ = ''.join(ch for ch in fl if ch not in 'MRN$#*@ ')
            if bad_:
                raise ValueError(f"reading の行 {row!r}: flags に未知の文字 {bad_!r}（使えるのは M 手入力 / R 保留 / N 注記 / $ # * @）")
            if 'M' in fl:
                d_['manual'] = True
            if 'R' in fl:
                d_['reserve'] = True
            mk = ''.join(ch for ch in fl if ch in '$#*@')
            if mk:
                d_['mark'] = mk
            if 'N' in fl:  # 注記行は文字列行と同じく {'note': 名称} だけにする（明細として計上しない）
                return {'note': d_.get('name', '') or d_.get('note', ''), 'comment': d_.get('comment', '')} if d_.get('comment') else {'note': d_.get('name', '') or d_.get('note', '')}
        return d_
    parts = [x.strip() for x in str(row).split('|')]
    extra = [x for x in parts[len(ROW_FIELDS):] if x]
    if extra:  # 11 列目以降に中身がある = 列がずれている（comment が消える）ので黙って捨てない
        raise ValueError(f"reading の行 {row!r}: 列が {len(ROW_FIELDS)} 個を超えている（余分: {extra}）。'|' の数を数え直す（{len(ROW_FIELDS) - 1} 個まで）")
    parts += [''] * (len(ROW_FIELDS) - len(parts))
    d = dict(zip(ROW_FIELDS, parts[:len(ROW_FIELDS)]))
    flags = _nfkc(d.pop('flags', '') or '').upper()  # ＊＃＄＠ や全角英字も半角に（印字の写し）
    unknown = ''.join(ch for ch in flags if ch not in 'MRN$#*@ ')
    if unknown:  # 打ち間違いを黙って捨てない（reading_check の WARN は expand 後の行を見るので届かない）
        raise ValueError(f"reading の行 {row!r}: flags に未知の文字 {unknown!r}（使えるのは M 手入力 / R 保留 / N 注記 / $ # * @）")
    out: dict = {}  # flags 自体は estimate に残さない（未知文字は上で弾く。M/R/N と印は下でキーに展開する）
    marks = ''.join(ch for ch in flags if ch in '$#*@')
    if marks:
        out['mark'] = marks
    for k in ('code', 'name', 'method', 'parts_no', 'comment'):
        if d.get(k):
            out[k] = d[k]
    for k, conv in (('index', float), ('qty', int), ('price', int), ('wage', int)):
        v = _nfkc(str(d.get(k, ''))).replace(',', '').replace('¥', '').replace('円', '').strip()  # 全角数字・カンマ・通貨記号を受ける（印字の写し）
        if v in ('-', '−', '—', '―', '‐'):  # 印字の '-'（金額・指数なし）は空欄と同じ
            v = ''
        if v != '':
            try:
                out[k] = conv(float(v)) if conv is int else conv(v)
            except ValueError:
                raise ValueError(f'reading の行 {row!r}: {k} が数値でない: {v!r}')
    if 'M' in flags:
        out['manual'] = True
    if 'R' in flags:
        out['reserve'] = True
    if 'N' in flags:  # 注記行。dict 行と同じく comment は残す（明細としては計上しない）
        out = {'note': out.get('name', ''), 'comment': out['comment']} if out.get('comment') else {'note': out.get('name', '')}
    return out


def detect_wage_round(rows: list[dict], labor: int) -> int:
    """工賃の丸め単位を推定: 指数×レートが 10 円丸めと 100 円丸めで違う行の印字工賃がどちらに一致するか（既定 10）"""
    if not labor:
        return 10
    hit100 = hit10 = 0
    for r in rows:
        if r.get('manual') or r.get('reserve') or r.get('note'):
            continue  # 手入力行・保留行は工場コグニの丸め設定の証拠にならない
        try:
            if r.get('wage') in (None, '') or r.get('index') in (None, ''):
                continue
            t = float(_num(r['index'])); w = int(float(_num(r['wage'])))
        except (TypeError, ValueError):
            continue
        if t <= 0:
            continue
        x = round(t * labor, 2)
        w10 = int(x // 10 + (1 if (x % 10) >= 5 else 0)) * 10
        w100 = int(x // 100 + (1 if (x % 100) >= 50 else 0)) * 100
        if w10 == w100:
            continue
        if w == w100:
            hit100 += 1
        elif w == w10:
            hit10 += 1
    return 100 if hit100 and not hit10 else 10


def labor_pairs(rows: list[dict]) -> list[tuple[float, int]]:
    """レート推定の材料: 手入力・保留・注記を除く行の (指数, 印字工賃)"""
    pairs = []
    for r in rows:
        if r.get('manual') or r.get('reserve') or r.get('note'):
            continue
        try:
            if _num(r.get('wage')) != '' and _num(r.get('index')) != '' and float(_num(r['index'])) > 0:
                pairs.append((float(_num(r['index'])), int(float(_num(r['wage'])))))
        except (TypeError, ValueError):
            pass
    return pairs


def rate_score(pairs: list[tuple[float, int]], rate: int) -> int:
    """レート候補が説明できる行数（指数×レートを 10 円または 100 円で丸めて印字工賃に一致）"""
    def _round(x, u):
        return int(x // u + (1 if (x % u) >= u / 2 else 0)) * u
    return sum(1 for t, w in pairs if w in (_round(round(t * rate, 2), 10), _round(round(t * rate, 2), 100)))


def infer_labor_rate(rows: list[dict]) -> int:
    """レバーレートの推定: 各行の wage/index を候補にし、候補ごとに「指数×候補を 10 円または 100 円で丸めると印字工賃に一致する行数」を数えて最多の候補を選ぶ
    （100 円丸めの工場では 0.25h → 2,800 なので単純な wage/index = 11,200 が混ざる。全行を説明できる 11,000 を選ぶ）
    同点は 100 円の倍数 → 50 円の倍数 → 1 万円に近い順（8,000 と 8,020 が同点なら 8,000）"""
    pairs = labor_pairs(rows)
    if not pairs:
        return 0
    cands = {r10(w / t) for t, w in pairs}
    for t, w in pairs:  # 丸め後の印字工賃から逆算できるレート範囲（10 円 / 100 円丸めの両方）も候補にする（100 円丸めの行だけだと実単価が w/t に現れない）
        for u in (10, 100):
            lo, hi = (w - u / 2.0) / t, (w + u / 2.0) / t
            c = int(lo // 10) * 10
            n = 0
            while c <= hi and n < 400:
                if c >= lo and 4000 <= c <= 20000:
                    cands.add(c)
                c += 10; n += 1
    cands = sorted(cands)
    best = max(cands, key=lambda c: (rate_score(pairs, c), c % 100 == 0, c % 50 == 0, -abs(c - 10000)))
    return best


# 塗装の「追加項目」（PaintingOther。材料代の対象外）に入れる工程の名前（NFKC 後の全角カナで照合）。
# 実案件 NEO 7,002 本の追加項目の名前: アンダーコート 149・内板調色 63・チッピング 25・ヒンジ 30・レールカバー 14・フューエルリッド 12・ホースメント 10・ホイルハウス 5 …（2026-09-13）。
# 「シーリング材料費」「…材」「…剤」「…費用」のような材料・費用の行も部位ではないのでこちら（ボデーシーリングの作業は先に paint.sealing へ分けてある）
_ADD_ITEM_RE = re.compile(r'アンダ[ー\-]?コ|内板|調色|チッピング|ヒンジ|ホ[ー\-]?スメント|インナ|レ[ー\-]?ルカバ|フ[ュユ][ー\-]?エルリ[ッツ]ド|ホイ[ー\-]?ルハウス|加算|下地|シ[ー\-]リング|材|剤|費')


class Drafter:
    def __init__(self, reading: dict):
        self.rd = reading
        self.notes: list[str] = []
        self.nb = NeoBuilder()
        v = dict(reading['vehicle'])
        self.vehicle = v
        if _flag(v.get('generic'), 'vehicle.generic'):  # 文字列 "false" を真にしない（Codex 指摘）
            self.veh = self.nb.generic_vehicle(v)
        else:
            self.veh = self.nb.resolve_vehicle(v, reading.get('hints'))
        self.car = self.veh['neo_car']
        self.generic = _flag(v.get('generic'), 'vehicle.generic') or not self.car.get('CarCode')
        if not self.generic:
            self.parts = AddataParts(self.nb.engine, self.car['CarCode'])
            self.parts.vehicle_body = str(self.car.get('BodyCode', '') or '')
            self.raw11 = self.parts._load_11_raw()
            try:
                self.raw83 = self.parts._load_83_raw()
            except Exception as e:  # noqa: BLE001  83.DB（色別部品）が無い車種はある。色別の照合だけ諦めて続ける
                self.raw83 = {}
                self.notes.append(f'色別部品（83.DB）を読めないので色別の品番照合は省略した: {type(e).__name__}: {e}')
            try:
                self.pi: Optional[PaintIndex] = PaintIndex(self.nb.engine.root, self.car['CarCode'],
                                                          body=self.car.get('BodyCode', ''))
            except Exception as e:  # noqa: BLE001  20.DB（塗装パネル）が無い車種はある。塗装は一括計上に落ちる
                self.pi = None
                self.notes.append(f'塗装パネル表（20.DB）を読めないので塗装はパネル別にできない: {type(e).__name__}: {e}')
            self.opts = self.nb.resolver.options(self.car['CarCode'])
        else:
            self.parts = None; self.raw11 = {}; self.raw83 = {}; self.pi = None; self.opts = {}
        self.grade = self.car.get('GradeCode', '')
        self.body = str(self.car.get('BodyCode', '') or '')
        self.reg_ym = re.sub(r'\D', '', str(self.car.get('ps_CarRegDate', '') or ''))[:6]  # 初度登録 YYYYMM（13.DB の適用期間判定）
        self.fva = (self.car.get('FVACode', '') or '')[-1:]
        self.year = str(self.car.get('YearCode', ''))
        self.eva_votes: dict[str, set[str]] = {}
        self.eva_veto: set[str] = set()
        self.review: list[dict] = []   # 確認箇所（make_neo.py が xlsx にする）。{'level', 'kind', 'page', 'name', 'code', 'text', '_item'}
        self._price_index: Optional[dict] = None

    # ------------------------------------------------------------------ 確認箇所
    def _rev(self, level: str, kind: str, text: str, row: Optional[dict] = None, item: Optional[dict] = None, code: str = '') -> None:
        """確認箇所を 1 件足す。level は 要確認（人が見て決める）/ 判断（下書きが決めた。根拠を残す）/ 参考"""
        row = row or {}
        self.review.append({'level': level, 'kind': kind, 'page': row.get('_page') or '', 'name': str(row.get('name') or (item or {}).get('name') or ''),
                            'code': code or str((item or {}).get('code') or ''), 'text': text, '_item': item})

    # ------------------------------------------------------------------ 価格で数量・部品コードを合わせる（判断規則 10-21）
    def _std_unit(self, ref: int, pn: str = '') -> int:
        """この車の条件（グレード・FVA・装備・年式・ボディ）で生成器が選ぶ 11.DB 変種の標準単価。無ければ 0"""
        ctx = {'grade': self.grade, 'fva': self.fva, 'eva': set((self.rd.get('hints') or {}).get('eva_codes') or ()), 'year': self.year, 'body': self.body}
        try:
            v, _ = self.parts.variant(ref, pn, ctx)
            return int((v or {}).get('price') or 0)
        except Exception:  # noqa: BLE001  変種が読めない ref は価格では判断しない
            return 0

    def _refs_by_price(self) -> dict:
        if self._price_index is None:
            idx: dict = {}
            for ref, vs in self.parts.by_ref.items():
                for v in vs:
                    try:
                        pr = int(v.get('price') or 0)
                    except (TypeError, ValueError):
                        continue
                    if pr > 0 and '-' in str(v.get('parts_no') or ''):
                        idx.setdefault(pr, set()).add(ref)
            self._price_index = idx
        return self._price_index

    def _name_sim(self, name: str, ref: int, side: str) -> float:
        """見積の名称と ref の 12.DB 名称の近さ（左右を外して比べる）。左右が食い違う・左右の無い見積名に左右付き部品は -1"""
        n0 = re.sub(r'^[LR](?=[^A-Z])', '', self.parts.norm_name(name))
        best = -1.0
        for n20 in self.parts.name20_by_ref.get(ref, ()):
            s20 = _side20(n20)
            if (side and s20 and s20 != side) or (not side and s20):
                continue
            c = self.parts.norm_name(n20)
            c1 = re.sub(r'^[LR](?=[^A-Z])', '', c) if s20 else c
            s = difflib.SequenceMatcher(None, c1, n0).ratio()
            s -= 0.3 * sum(1 for w in SMALL_WORDS if w in c1 and w not in n0)  # 候補にだけある小物語（クリップ等）は別部品
            best = max(best, s)
        return best

    QTY_FROM_PRICE_MAX = 99   # 数量として読み替える上限（これより多い倍数は偶然の一致とみなす）
    QTY_FROM_PRICE_UNIT_MAX = 3000  # 数量を自動で読み替えるのは標準単価がこれ以下の小物だけ（それより高い部品の倍数一致は 要確認 に挙げるだけ。Codex 指摘）

    def _price_fit(self, ref: int, why: str, name: str, side: str, price: int, qty: int, pn: str, may_switch: bool, ctx_block: str) -> tuple[int, str, int, str]:
        """印字の金額が標準単価 × 数量と合わない行を、(1) 単価の合う別の ref（名称が近いもの）へ直すか、
        (2) 数量 1 のまま複数個分の金額なら数量を直す。戻り値 (ref, why, qty, 何をしたか)。何もしなければ最後は ''"""
        self._price_check = ''
        if price <= 0 or qty <= 0:
            return ref, why, qty, ''
        u0 = self._std_unit(ref, pn)
        if not u0 or price == u0 * qty:
            return ref, why, qty, ''
        s0 = self._name_sim(name, ref, side)
        colored = ref in (self.raw83 or {})  # 色別部品は色で単価が変わるので数量の読み替えはしない
        n0 = price // u0 if (qty == 1 and price % u0 == 0) else 0
        div_ok = (not colored) and 2 <= n0 <= self.QTY_FROM_PRICE_MAX and u0 <= self.QTY_FROM_PRICE_UNIT_MAX
        self._price_check = (f'金額 {price:,} 円が標準単価 {u0:,} 円のちょうど {n0} 倍。数量 {n0} の可能性がある（標準単価 {self.QTY_FROM_PRICE_UNIT_MAX:,} 円を超える部品なので自動では直さない）'
                             if (not colored and 2 <= n0 <= self.QTY_FROM_PRICE_MAX and u0 > self.QTY_FROM_PRICE_UNIT_MAX) else '')
        blk0 = self.parts.block_of(ref)
        exact = []
        if may_switch and price % qty == 0:
            unit_in = price // qty
            for r in self._refs_by_price().get(unit_in, ()):
                if r == ref:
                    continue
                s = self._name_sim(name, r, side)
                if s >= 0.6 and self._std_unit(r) == unit_in:
                    exact.append((round(s, 3), 1 if self.parts.block_of(r) in (blk0, ctx_block) else 0, -r, r))
        exact.sort(reverse=True)
        # 採ってよい候補だけに絞ってから最良を選ぶ（先頭 1 件だけ見ると、別ブロックの先頭に隠れた同じブロックの候補を取り逃がす。Codex 指摘）
        #   数量の読み替えもできる行: 同じ部位で名称が同等以上の候補だけ / 読み替えできない行: 名称が近くない採用（s0 < 0.9）なら全候補、そうでなければ同じ名前（0.95 以上）の候補だけ
        ok_exact = [x for x in exact if ((x[1] and x[0] >= s0) if div_ok else (s0 < 0.9 or x[0] >= 0.95))]
        if ok_exact:
            s_e, _same_blk, _, r_e = ok_exact[0]
            return r_e, f'単価一致({price // qty:,} 円・名称 {s_e:.2f}) ← {why}', qty, f'部品コード {ref:04d} → {r_e:04d}（単価 {price // qty:,} 円が一致）'
        if div_ok:
            return ref, why, n0, f'数量 1 → {n0}（金額 {price:,} ÷ 標準単価 {u0:,}）'
        if may_switch and qty == 1 and s0 < 0.9:  # 名称の弱い採用で単価も合わない: 名称がほぼ同じで単価が金額を割り切る部品（ドアトリムボードクリップ 720 = 90 × 8）
            best = None
            for r, n20s in self.parts.name20_by_ref.items():
                if r == ref or r in (self.raw83 or {}):
                    continue
                s = self._name_sim(name, r, side)
                if s < 0.85:
                    continue
                u = self._std_unit(r)
                if u and price % u == 0 and (price // u == 1 or (2 <= price // u <= self.QTY_FROM_PRICE_MAX and u <= self.QTY_FROM_PRICE_UNIT_MAX)):  # 数量を変えるのは小物だけ（Codex 指摘）
                    key = (round(s, 3), 1 if self.parts.block_of(r) in (blk0, ctx_block) else 0, -r)
                    if best is None or key > best[0]:
                        best = (key, r, u)
            if best is not None:
                _k, r_b, u_b = best
                n_b = price // u_b
                return r_b, f'単価の倍数一致({u_b:,} 円 × {n_b}・名称 {_k[0]:.2f}) ← {why}', n_b, (
                    f'部品コード {ref:04d} → {r_b:04d}' + (f'、数量 1 → {n_b}' if n_b > 1 else '') + f'（金額 {price:,} = 標準単価 {u_b:,} × {n_b}）')
        return ref, why, qty, ''

    # ------------------------------------------------------------------ 明細
    def _refs_in_block(self, block: str) -> list[int]:
        return [r for r, b in self.parts.block_by_ref.items() if b == block]

    def _match_in_block(self, name: str, block: str, side: str) -> tuple[Optional[int], float]:
        """ブロック内の 12.DB 名称と照合（言い換え辞書込み）。左右は名称の先頭 L/R と一致するものだけ"""
        if not block:
            return None, 0.0
        n0 = self.parts.norm_name(name)
        variants = list(self.parts._name_variants(n0))
        for a, b in EXTRA_ALIASES:
            for base in list(variants):
                if a in base and base.replace(a, b) not in variants:
                    variants.append(base.replace(a, b))
        best, best_s = None, 0.0
        for ref in self._refs_in_block(block):
            for n20 in self.parts.name20_by_ref.get(ref, ()):
                c = self.parts.norm_name(n20)
                side20 = _side20(n20)
                if side and side20 and side != side20:
                    continue
                if not side and side20:
                    continue
                c1 = re.sub(r'^[LR](?=[^A-Z])', '', c) if side20 else c
                for v in variants:
                    v1 = re.sub(r'^[LR](?=[^A-Z])', '', v)
                    s = difflib.SequenceMatcher(None, c1, v1).ratio()
                    # 候補にだけ含まれる小物語（クリップ・リテーナ・グロメット…）は別部品の可能性が高いので減点
                    s -= 0.3 * sum(1 for w in SMALL_WORDS if w in c1 and w not in v1)
                    if s > best_s:
                        best, best_s = ref, s
        return best, best_s

    def _block_candidates(self, pn: str, name: str, block: str, side: str) -> list[int]:
        """品番一致の ref のうち、指定ブロックに属し、左右が矛盾せず、名称が最も近いもの（同点は ref 順）"""
        n0 = re.sub(r'^[LR](?=[^A-Z])', '', self.parts.norm_name(name))
        scored = []
        for c in self._pn_candidates(pn):
            if self.parts.block_of(c) != block:
                continue
            raw_names = list(self.parts.name20_by_ref.get(c, ()))
            names = [self.parts.norm_name(x) for x in raw_names]
            sides = {_side20(x) for x in raw_names if _side20(x)}
            target = c
            if side and sides and side not in sides:
                if side == 'R' and sides == {'L'} and c in self.parts.pair_right:  # 品番が左 ref にしか無いペア → 右 ref を候補にする（find_ref と同じ変換）
                    target = self.parts.pair_right[c]
                else:
                    continue
            if not side and sides:
                continue
            sim = max((difflib.SequenceMatcher(None, re.sub(r'^[LR](?=[^A-Z])', '', x) if _side20(r_) else x, n0).ratio() for x, r_ in zip(names, raw_names)), default=0.0)
            scored.append((sim, target))
        if not scored:
            return []
        top = max(s_ for s_, _ in scored)
        return [c for s_, c in sorted(scored, key=lambda t: (-t[0], t[1])) if s_ >= top - 0.1]

    def _alias_ref(self, name_raw: str, side: str, block: str) -> tuple[Optional[int], str]:
        """別名辞書（全車種 12.DB の 部品名 → 部品コード）でこの車種の ref を引く。
        見積名称を正規化（左右を外す）→ 辞書のコード候補（件数順）→ この車種の 12.DB にあるコード → 左右（右なら pair_right の右側 ref）→ 部位ブロックが合うものを優先"""
        d = part_names()
        if not d or self.generic:
            return None, ''
        from build_part_names import norm_name as _nn
        key = _nn(name_raw, strip_side=True)
        cands = d.get('names', {}).get(key) or {}
        if not cands:
            return None, ''
        best = None
        for code, cnt in sorted(cands.items(), key=lambda kv: -kv[1]):
            ref = int(code)
            if ref not in self.parts.name20_by_ref:
                continue
            n20 = sorted(self.parts.name20_by_ref.get(ref, ()))
            s20 = {_side20(x) for x in n20 if _side20(x)}
            if side == 'R' and s20 == {'L'}:
                ref = self.parts.pair_right.get(ref, ref)
            elif side == 'L' and s20 == {'R'}:
                continue
            score = (1 if (block and self.parts.block_of(ref) == block) else 0, cnt)
            if best is None or score > best[0]:
                best = (score, ref, cnt)
        if best is None:
            return None, ''
        return best[1], f'別名辞書({key} → {best[1]:04d}、全車種 {best[2]} 件)'

    def _title_block(self, title: str) -> str:
        """ブロック見出し（フロントバンパー / 右 フロントフェンダー …）→ 12.DB の部位ブロック。見出しを部品名として照合し、十分近い ref の block"""
        t = re.sub(r'[【】\[\]（）()]', '', _hw_kana(title)).strip()
        if not t:
            return ''
        t = re.sub(r'^(RH|LH)\s*', lambda m: '右' if m.group(1) == 'RH' else '左', t)
        ref, why = self.parts.find_ref('', '', t, context_block='', price=None, qty=None, year=self.year)
        m = re.search(r'名称近似\(([\d.]+)\)', why or '')
        if ref is not None and (m is None or float(m.group(1)) >= 0.6):
            return self.parts.block_of(ref) or ''
        return ''

    def _pn_candidates(self, pn: str) -> list[int]:
        """11.DB の取替行にこの品番を持つ ref（12.DB 順）"""
        pn_norm = self.parts.norm_pn(pn)
        if not pn_norm:
            return []
        refs = sorted(ref for ref, rows in self.raw11.items() if any(r.get('disp') == 'K' and self.parts.norm_pn(r['pn']) == pn_norm for r in rows))
        if not refs:  # 11.DB に無い品番（色付き・期間別・仕様別）は 13/83.DB の行から（find_ref と同じ解決範囲）
            refs = sorted(ref for ref, rows in self.raw83.items() if any(self.parts.norm_pn(r.get('pn', '')) == pn_norm for r in rows))
        return refs

    def _vote_eva(self, ref: int, pn: str) -> None:
        """見積の品番が、この車のグレードで装備レター付きの行にしか無いとき、そのレターを採用候補にする"""
        pn_norm = self.parts.norm_pn(pn)
        if not pn_norm or not self.grade or self.veh.get('confidence') not in ('confirmed', 'high'):  # 車両特定が confirmed/high でなければ装備を推定しない（暫定グレードで条件付き行を自車扱いにしない）
            return
        rows11 = [r for rows in self.raw11.values() for r in rows if r.get('disp') == 'K' and self.parts.norm_pn(r['pn']) == pn_norm]  # 同品番の全 ref
        rows83 = [r for rows in self.raw83.values() for r in rows if self.parts.norm_pn(r.get('pn', '')) == pn_norm]

        body_ok = {0, int(self.body) if str(self.body).isdigit() else 0}
        grp = self.year[-1] if self.year.isdigit() and int(self.year) else ''

        def mine(rows):  # この車のグレード・ボディ・年式群に当てはまる行（条件なし or 一致）
            return [r for r in rows
                    if (not r.get('flags', '')[0:5].strip() or self.grade in r.get('flags', '')[0:5])
                    and int(r.get('body') or 0) in body_ok
                    and (not r.get('grp') or r.get('grp') == grp)]

        def letters_of(r):
            return set(ch for ch in r.get('flags', '')[5:7] if ch.strip() and ch != self.fva and ch in self.opts)
        def in_period(r):  # 13.DB 行は from/to（YYYYMM）で適用期間を持つ。初度登録がその中に無い行・期間不明行は証拠にしない
            if r.get('period_invalid'):
                return False
            f, t = str(r.get('from') or ''), str(r.get('to') or '')
            if not self.reg_ym:
                return not f and not t
            return (not f or f <= self.reg_ym) and (not t or self.reg_ym <= t)
        m11 = mine(rows11)
        src = m11 if rows11 else [r for r in mine(rows83) if in_period(r)]  # 11.DB に品番がある限り 11.DB を正とする。11.DB に無い品番だけ 13/83.DB（適用期間内の行のみ）
        if not src:
            return
        letters = [letters_of(r) for r in src]
        if all(letters) and all(l == letters[0] for l in letters):  # 自グレードの全行がその装備を要求 → 採用候補
            for ch in letters[0]:
                self.eva_votes.setdefault(ch, set()).add(f'{ref} {pn}')
        elif m11 and any(not l for l in letters):  # 11.DB に装備条件の無い行でも同じ品番 → 装備の証拠にはならない
            for l in letters:
                for ch in l:
                    self.eva_veto.add(ch)

    DITTO_RE = re.compile(r'^[〃″”"]\s*|^同上\s*')
    DITTO_WAGE_RE = re.compile(r'^(交換|取替|取付|脱着|組替)?\s*(工賃|技術料)$')

    def _merge_ditto(self, rows: list[dict]) -> list[dict]:
        """「〃 交換工賃」（直前の部品の工賃だけを次の行に印字する書式）を直前の行にまとめる。
        「〃（モデリスタ）」のように金額のある続き行は、直前の名称を補った別の行にする"""
        out: list[dict] = []
        for r in rows:
            nm = _nfkc(str(r.get('name') or '')).strip()
            if out and self.DITTO_RE.match(nm) and out[-1].get('_block_title') == r.get('_block_title'):
                rest = self.DITTO_RE.sub('', nm).strip()
                prev = out[-1]
                price = _num(r.get('price'))
                if self.DITTO_WAGE_RE.match(rest) and price in ('', '0'):
                    w = _num(r.get('wage'))
                    if w not in ('', '0'):
                        pw = _num(prev.get('wage'))
                        prev['wage'] = int(float(w)) + (int(float(pw)) if pw not in ('', '0') else 0)
                        self._rev('判断', '行のまとめ', f'「{r.get("name")}」の技術料 {int(float(w)):,} 円を直前の「{prev.get("name")}」の工賃にまとめた'
                                  + (f'（この行にも工賃 {int(float(pw)):,} 円があったので合算）' if pw not in ('', '0') else ''), row=prev)
                        prev.setdefault('_revs', []).append(self.review[-1])  # 明細 No を付けるため、この行から作る item に後で結び付ける
                    for _k in ('comment', 'neo_comment'):  # 続き行の転記メモ・印字コメントも落とさない（Codex 指摘）
                        if r.get(_k):
                            prev[_k] = '。'.join(x for x in (str(prev.get(_k) or ''), str(r[_k])) if x)
                    continue
                if rest:
                    r = dict(r, name=re.sub(r'\s+', '', _nfkc(str(prev.get('name') or ''))) + rest)
            out.append(r)
        return out

    def items(self) -> list[dict]:
        out: list[dict] = []
        rows_flat: list[dict] = []
        for blk in self.rd.get('blocks') or []:
            title = _hw_kana(blk.get('title') or '')
            for row in blk.get('rows') or []:
                row = expand_row(row)
                if row.get('note') and not row.get('name'):
                    self.notes.append(f"注記【{title}】{row['note']}")
                    continue
                rows_flat.append(dict(row, _block_title=title, _page=blk.get('page') or ''))
        rows_flat = self._merge_ditto(rows_flat)
        # 真偽値欄はここで 1 回だけ正規化する。レート推定・丸め判定も生値を見るので、
        # 行ループの中で直すだけでは届かない（Codex 指摘）
        for _r in rows_flat:
            for _bk in ('manual', 'reserve'):   # recycle は真偽値ではなくリサイクル部品の情報（dict）
                if _bk in _r:
                    _r[_bk] = _flag(_r[_bk], f'rows[].{_bk}')
        labor = _money_or(self.rd.get('labor_rate'), name='labor_rate') or infer_labor_rate(rows_flat + list((self.rd.get('paint') or {}).get('lines') or []))
        self.labor = labor
        self.wage_round = _money_or(self.rd.get('wage_round'), name='wage_round') or detect_wage_round(rows_flat + list((self.rd.get('paint') or {}).get('lines') or []), labor)  # 塗装行も証拠に（レート推定と同じ集合）
        if self.wage_round != 10:
            self.notes.append(f'工賃の丸め単位 {self.wage_round} 円（印字の工賃が指数×レートの {self.wage_round} 円丸めと一致）→ estimate.wage_round')
        has_wage_col = any(r.get('wage') is not None or r.get('index') is not None for r in rows_flat)  # 工賃列のある書式か
        ctx_block = ''
        used: set[int] = set()
        cur_title = None
        title_blocks: dict[str, str] = {}
        for row in rows_flat:
            if row.get('_block_title') != cur_title:  # 新しいブロック: 見出しから部位文脈を作り直す（前ブロックの ref を引きずらない）
                cur_title = row.get('_block_title')
                if cur_title not in title_blocks:
                    title_blocks[cur_title] = self._title_block(cur_title) if (cur_title and not self.generic) else ''
                ctx_block = title_blocks[cur_title]
                if cur_title and not ctx_block and not self.generic:
                    self.notes.append(f'見出し【{cur_title}】は部位ブロックに対応付けできず、行の品番から文脈を決める')
            # 行の真偽値欄はここで 1 回だけ正規化する（"false" を真にしない。Codex 指摘）
            row = dict(row)
            for _bk in ('manual', 'reserve'):  # recycle は真偽値ではなくリサイクル部品の情報（dict）
                if _bk in row:
                    row[_bk] = _flag(row[_bk], f'rows[].{_bk}')
            name_raw = str(row.get('name') or '')
            name = _clean_name(name_raw)
            method = str(row.get('method') or ('' if row.get('manual') else '取替'))  # 手入力行で修理方法が空欄なら空のまま（コグニは DisposalCode -1 で保存。実機 2026-09-08）
            qty = int(float(_num(row.get('qty')) or 1))
            if qty <= 0:  # 数量 0 や負を 1 に化けさせない（写し間違い。行ごと消えている見積なら reading から行を削る）
                raise ValueError(f'reading の行 {name_raw!r}: 数量が {qty}。印字を写し直すか、行そのものを削る')
            price_raw = _num(row.get('price'))
            price = int(float(price_raw)) if price_raw != '' else 0
            wage = int(float(_num(row['wage']))) if _num(row.get('wage')) != '' else None  # 空欄セルは '' で写されることがある → 未指定。'12,750' も可
            index = float(_num(row['index'])) if _num(row.get('index')) != '' else None
            pn = str(row.get('parts_no') or '').strip()
            dcode = _dcode(method, price, wage)
            if price > 0 and dcode in (1, 2, 3, 4, 5, 6) and not row.get('manual'):
                # 部品代が付くのは取替（と手入力行）。実 NEO 211 本 5,400 行のうち、修理・板金・点検調整・分解調整は 1 例も無く、脱着も 2 行だけ
                self.notes.append(f'{name_raw}: {METHOD_NAME.get(dcode, dcode)} の行に部品代 {price:,} 円がある。'
                                  '取替の行と取り違えていないか、部品代が別行のものでないか確かめる（合計が合っていても区分が変わる）')
            if method.strip() and method.strip() not in DISPOSAL and _nfkc(method).replace(' ', '') not in DISPOSAL:
                self.notes.append(f'{name_raw}: 修理方法「{method}」はコグニの区分に無いので {METHOD_NAME.get(dcode, dcode)} として扱った。違うなら reading の method を直す')
            item: dict = {'code': '', 'name': name, 'method': ('' if (row.get('manual') and not method.strip()) else METHOD_NAME.get(dcode, '取替')), 'parts_no': pn, 'qty': qty}
            if price_raw != '':
                item['price'] = price  # 印字された金額（0 も含む）。欄が無い行は省略 → 生成器が標準価格で補完（取替）/ 0（脱着等）
            if wage is not None:
                item['wage'] = int(wage)
            elif index is not None and float(index) > 0 and labor:
                x = round(float(index) * labor, 2); u = self.wage_round
                item['wage'] = int(x // u + (1 if (x % u) >= u / 2 else 0)) * u  # 指数だけ印字された行: コグニ丸め（丸め単位で四捨五入）で工賃化
            elif has_wage_col and dcode == 0 and price > 0 and index is None:
                item['wage'] = 0  # 工賃列のある書式で工賃も指数も空の部品行 = 付属部品（工賃なし）
            # それ以外（工賃列の無い書式、脱着/修理/板金で工賃が読めない行）は wage を省略し、生成器の標準指数に任せる
            if index is not None and float(index) > 0:
                item['index'] = float(index)
            if row.get('comment'):  # 転記メモ（人が確かめる点）。NEO の明細コメントには書かず確認箇所シートへ
                item['_memo'] = str(row['comment'])
                _cm = str(row['comment'])
                _need = bool(re.match(r'^\s*(要確認|★|確認)', _cm)) or '?' in _cm or '？' in _cm  # 「要確認: …」「★…」や読めない印（?）は人が決める点
                self._rev('要確認' if _need else '参考', '転記メモ', re.sub(r'^\s*(要確認|★)\s*[:：]?\s*', '', _cm) if _need else _cm, row=row, item=item)
            if row.get('neo_comment'):  # NEO の明細コメント（見積書に印字された備考など、コグニの画面に出したいものだけ）
                item['comment'] = str(row['neo_comment'])
            item['_page'] = row.get('_page') or ''
            if row.get('neo_name'):  # NEO の名称欄（24 バイト）に入れる短い名前（手入力行のみ効く。見積の印字は確認箇所シートに残す）
                item['_neo_name'] = str(row['neo_name'])
            for _e in row.get('_revs') or []:  # 「〃 交換工賃」をまとめた記録をこの行の明細に結び付ける（確認箇所シートの明細 No）
                _e['_item'] = item
            if row.get('bankin'):  # 板金ランクを reading で明示したとき（下の自動判定より優先。judgment_rules 5）
                item['bankin'] = row['bankin']
            if row.get('recycle'):  # リサイクル部品（estimate_schema items.recycle）
                item['recycle'] = row['recycle']
            if row.get('mark'):
                item['_mark'] = ''.join(ch for ch in str(row['mark']) if ch in '$#*@')  # 印字の印（$ 暫定指数 / # 手入力指数 / * 手入力工賃・金額）。make_neo が生成結果と照合する
            if row.get('reserve'):
                item['reserve'] = True
            if row.get('manual') or self.generic:
                item['manual'] = True
                out.append(item)
                continue
            # ref 決定
            side = _side_of(name_raw) or _side_of(row.get('_block_title') or '')  # 行に左右が無ければブロック見出し（【右 フロントフェンダー】）の左右
            if not side and re.match(r'^[LR](?![A-Za-z])[ァ-ヶｦ-ﾟ]', _nfkc(name_raw).strip()):  # 'Rドア' は右かリヤか決められない（監査 15）
                self.notes.append(f'{name_raw}: 先頭の L/R が左右か前後（Rr = リヤ）か読めないので、左右指定なしで名称照合した。左右が必要なら reading の name を「右…」「左…」に直す')
            _c_raw = row.get('code')
            if _c_raw is None or (isinstance(_c_raw, str) and _nfkc(_c_raw).strip() == ''):  # 空白だけの欄は「指定なし」
                code_in = ''
            else:  # 非数字を落として先頭 4 桁、では '12OOO' が '12' に化ける。生成器と同じ検証を通す
                try:
                    code_in = _code4(_c_raw)
                except ValueError as _e:
                    raise ValueError(f'reading の行 {name_raw!r}: {_e}') from None
            # コグニ印刷（書式 A）の部品コードは find_ref の code 引数で解決する（12.DB を正に検証。無効なら品番/名称に落ちる）
            ref, why = self.parts.find_ref(code_in, pn, name, context_block=ctx_block, price=(price // max(1, qty) if price else None),
                                           qty=(qty if qty > 1 else None), year=self.year)
            if code_in and (ref is None or ref != int(code_in)):
                self.notes.append(f'印字の部品コード {code_in} をそのまま使えない（{why}）→ {ref}: {name}')
                code_in = ''
            if pn and ctx_block and not code_in:  # 同じ品番が複数ブロックにある小物: 見出しのブロックに属し、左右と名称が合う候補を未使用の順に使う（1179 → 1183）
                cands = self._block_candidates(pn, name, ctx_block, side)
                if cands and (ref not in cands or ref in used):
                    pick = next((c for c in cands if c not in used), None)
                    if pick is not None and pick != ref:
                        why = f'品番一致（ブロック {ctx_block} 内の未使用候補 {pick} ← 全体照合は {ref}）'
                        ref = pick
            if ref is not None and not pn and not code_in and ctx_block and self.parts.block_of(ref) != ctx_block:
                # 品番の無い行が他ブロックに飛んだ → 同ブロック内の名称照合を優先
                ref2, s2 = self._match_in_block(name, ctx_block, side)
                if ref2 is not None and s2 >= 0.55:
                    why = f'ブロック内名称照合({s2:.2f}) ← 全体照合は {ref}'
                    ref = ref2
            if ref is None and ctx_block:
                ref2, s2 = self._match_in_block(name, ctx_block, side)
                if ref2 is not None and s2 >= 0.55:
                    ref, why = ref2, f'ブロック内名称照合({s2:.2f})'
            if not pn and not code_in:  # 品番も部品コードも無い行: 別名辞書（全車種の 12.DB 名称）でコードを引き、名称近似の結果と突き合わせる
                aref, awhy = self._alias_ref(name_raw, side, ctx_block)
                m_sim = re.search(r'名称近似\(([\d.]+)\)|ブロック内名称照合\(([\d.]+)\)', why or '')
                sim = float(m_sim.group(1) or m_sim.group(2)) if m_sim else 1.0  # 類似度の無い理由（名称一致・品番一致）は確定扱い = 辞書で置き換えない
                same_block = (not ctx_block) or self.parts.block_of(aref) == ctx_block if aref is not None else False
                if aref is not None and (ref is None or (ref != aref and sim < 0.9 and same_block)):  # 既に候補がある行は、辞書のコードが同じ部位ブロックのときだけ置き換える
                    self.notes.append(f'{awhy}: {name}' + (f'（名称近似 {ref} {"/".join(sorted(self.parts.name20_by_ref.get(ref, ())))} より辞書を優先）' if ref is not None else ''))
                    ref, why = aref, awhy
            if ref is None and not code_in and price > 0 and qty == 1 and dcode == 0:
                # 数量 1 のまま複数個分の金額（クリップ 1,300 円 = 100 円 × 13）は、find_ref の価格整合（標準の 2.2 倍超は別部品）で落ちる。
                # 価格を外して名称で引き直し、標準単価の整数倍（小物）なら採る（数量は下の _price_fit が直す。Codex 指摘）
                ref_np, why_np = self.parts.find_ref('', pn, name, context_block=ctx_block, price=None, qty=None, year=self.year)
                if ref_np is not None and ref_np not in (self.raw83 or {}):
                    u_np = self._std_unit(ref_np, pn)
                    if u_np and u_np <= self.QTY_FROM_PRICE_UNIT_MAX and price % u_np == 0 and 2 <= price // u_np <= self.QTY_FROM_PRICE_MAX:
                        ref, why = ref_np, f'{why_np}（金額が標準単価 {u_np:,} 円の {price // u_np} 倍）'
            if ref is None:
                self.notes.append(f'未照合: {name} {pn}（manual にした。ADDATA にある品目なら code を指定）')
                item['manual'] = True
                out.append(item)
                continue
            if 0 < price <= 1000 and dcode == 0 and wage is not None and wage >= 10000:
                # 1,000 円以下の小物に 1 万円以上の技術料（2026-09-13 ランクル: エンジンサービスラベル 200 円に 19,200 円 = 次の行の工賃と同額）
                self._rev('要確認', '小物に大きな工賃', f'部品代 {price:,} 円の行に技術料 {wage:,} 円。別の行の工賃を写し間違えていないか、工場の書き間違いでないか確かめる（印字どおりに入れた）', row=row, item=item)
            if price > 0 and dcode == 0 and not row.get('reserve') and not row.get('recycle'):
                ref0 = ref
                ref, why, qty2, did = self._price_fit(ref, why, name_raw, side, price, qty, pn, may_switch=not (code_in or pn), ctx_block=ctx_block)
                if getattr(self, '_price_check', '') and not did:
                    self._rev('要確認', '数量の可能性', self._price_check, row=row, item=item)
                if did:
                    if qty2 != qty:
                        item['qty'] = qty = qty2
                        item['_qty_from_price'] = True
                    self.notes.append(f'{name_raw}: {did}')
                    self._rev('判断', '数量' if ref == ref0 else '部品コード', did, row=row, item=item, code=f'{ref:04d}')
            if not code_in and not pn and '辞書' not in (why or ''):  # 品番も部品コードも無い行（汎用小物など）は名称だけが根拠なので必ず見せる
                self.notes.append(f'名称だけで決めた: {name} → {ref} '
                                  + '/'.join(sorted(self.parts.name20_by_ref.get(ref, ()))) + f'（{why}）。品番が無いので別の部品を選んでいないか確かめる')
            elif '名称近似' in why or 'ブロック内' in why:
                self.notes.append(f"名称照合で決定: {name} → {ref} {'/'.join(sorted(self.parts.name20_by_ref.get(ref, ())))}（{why}）")
            _moved_by_price = '単価' in (why or '').split(' ← ')[0]
            if not _moved_by_price or not ctx_block or self.parts.block_of(ref) == ctx_block:
                # 単価で**別の部位ブロック**の ref に直した行（同じ品番の小物が別ブロックにある）だけは、後続行の部位文脈を動かさない（同じブロック・文脈が空なら通常どおり更新。Codex 指摘）
                ctx_block = self.parts.block_of(ref) or ctx_block
            used.add(ref)
            item['code'] = f'{ref:04d}'
            item['_ref_why'] = why  # どうやって ref を決めたか。run_case が「名称近似 × 価格不一致」を絞るのに使う
            if ref:  # 名称と部品コードの左右・前後が食い違っていないか（印字のコードを写し間違えると静かに反対側の部品ができる）
                _n20s = list(self.parts.name20_by_ref.get(ref, ()))
                _s20 = {_side20(x) for x in _n20s if _side20(x)}
                _f20 = {_fr20(x) for x in _n20s if _fr20(x)}
                _s = side or _side_of(name_raw)
                _f = _fr_of(name_raw) or _fr_of(row.get('_block_title') or '')  # 行名に前後が無ければ見出し（【フロントバンパー】）から
                if _s and _s20 and _s not in _s20:
                    self.notes.append(f'{name_raw}: 部品コード {ref} は {"/".join(sorted(_s20))} 側の部品（12.DB 名称 {_n20s[0].strip()!r}）。'
                                      '見積の名称と左右が食い違う。印字のコードか名称の写し間違いを確かめる')
                elif _f and _f20 and _f not in _f20:
                    self.notes.append(f'{name_raw}: 部品コード {ref} は {"/".join(sorted(_f20))} 側の部品（12.DB 名称 {_n20s[0].strip()!r}）。'
                                      '見積の名称と前後が食い違う。印字のコードか名称の写し間違いを確かめる')
            if pn:
                self._vote_eva(ref, pn)
            # 板金ランク
            if dcode == 6 and not item.get('bankin'):  # reading で bankin を明示した行は自動判定しない
                area = _area_of(name_raw, row)
                t = float(index) if index else (round(float(wage) / labor, 2) if wage and labor else None)
                if area and t is not None:
                    hit = next((rk for rk in 'ABC' if bankin_time(area, rk) is not None and abs(bankin_time(area, rk) - t) < 0.005), None)
                    if hit:
                        item['bankin'] = {'area': area, 'yes': BANKIN_YES[hit]}
                        self.notes.append(f'板金 {name} {area}d㎡ 指数 {t} → ランク {hit}')
                    else:
                        self.notes.append(f"板金 {name} {area}d㎡ 指数 {t} は BANKIN.DB のどのランクとも違う → '#' 手入力")
                elif area and t is None and wage:
                    item['_bankin_area'] = area  # レートがまだ決まっていない（技術料だけの書式）。_labor_from_wages でレートが決まったらランクを引き直す
                elif area is None:
                    self.notes.append(f"板金 {name}: 損傷面積が読めない → '#' 手入力（面積が分かれば bankin を付ける）")
            # 左右分割: 左右指定の無い名称で左 ref に数量 2n
            n20 = sorted(self.parts.name20_by_ref.get(ref, ()))
            left_only = bool(n20) and all(_side20(s) == 'L' for s in n20)
            has_labor = bool(item.get('wage')) or bool(item.get('index'))
            if not side and left_only and qty >= 2 and qty % 2 == 0 and ref in self.parts.pair_right and has_labor:
                self.notes.append(f'左右分割せず: {name} ×{qty} は工賃/指数があるので 1 行のまま（左右に分けるなら reading で 2 行に）')
            if not side and left_only and qty >= 2 and qty % 2 == 0 and ref in self.parts.pair_right and not has_labor and not item.get('_qty_from_price'):
                rref = self.parts.pair_right[ref]
                half = qty // 2
                unit = price // qty if qty else 0
                left = dict(item, code=f'{ref:04d}', name=name, qty=half)
                right = dict(item, code=f'{rref:04d}', name=name, qty=half)
                if 'price' in item:
                    left['price'] = unit * half; right['price'] = price - unit * half
                rn = sorted(self.parts.name20_by_ref.get(rref, ()))  # 右 ref の 20 文字名（注記用）
                left['name'] = '左' + re.sub(r'^(右|左)\s*', '', name)
                right['name'] = '右' + re.sub(r'^(右|左)\s*', '', name)
                out.extend([left, right])
                for _e in self.review:  # この行に付けた確認箇所は分割後の左の行に結び付ける（明細 No が消えないように。Codex 指摘）
                    if _e.get('_item') is item:
                        _e['_item'] = left
                self.notes.append(f'左右分割: {name} ×{qty} → {ref}（左）×{half} + {rref}（右 {"/".join(rn)}）×{half}')
                continue
            out.append(item)
        if not labor and not self.generic and not _money_or(self.rd.get('labor_rate'), name='labor_rate'):
            self._labor_from_wages(out)
        for it in out:
            it.pop('_bankin_area', None)
            self._fit_manual_name(it)
        return out

    def _fit_texts(self, d: dict, keys: tuple, label: str) -> dict:
        """顧客名・工場名など 30 バイトの欄に入らない会社名を (株) 等に略す（入らないまま渡すと生成器が途中で切る）。略しても入らなければ要確認"""
        out = dict(d)
        for k in keys:
            v = str(out.get(k) or '')
            if not v or _cp932_len(v) <= TEXT30_BYTES:
                continue
            a = abbr_company(v)
            if a != v:
                out[k] = a
                self._rev('判断', f'{label}の欄', f'{k}「{v}」は {TEXT30_BYTES} バイトの欄に入らないので「{a}」に略した')
            if _cp932_len(a) > TEXT30_BYTES:
                self._rev('要確認', f'{label}の欄', f'{k}「{a}」は {TEXT30_BYTES} バイトの欄に入らず、NEO では「{_fit(a, TEXT30_BYTES)}」まで。短い表記を reading に書く')
        return out

    def _fit_manual_name(self, it: dict) -> None:
        """手入力行（部品コード無し）の名称は見積の印字がそのまま NEO の名称欄（24 バイト）に入る。入らないときは
        reading の neo_name（人が付けた短い名前）か、shorten_name の自動短縮にして、印字の全文を確認箇所シートに残す"""
        short = it.pop('_neo_name', None)
        if it.get('code') and not it.get('manual'):
            return  # 部品コードのある行の名称は ADDATA の標準名称になる
        full = str(it.get('name') or '')
        if short:
            s_hw = hw(short).strip()
            if _cp932_len(s_hw) > PARTS_NAME_BYTES:
                raise ValueError(f'reading の neo_name {short!r} が NEO の名称欄 {PARTS_NAME_BYTES} バイトに入らない（{_cp932_len(s_hw)} バイト）。もっと短くする')
            if s_hw != hw(full).strip():
                it['name'] = s_hw; it['_full_name'] = full
                self._rev('判断', '名称の短縮', f'NEO の名称欄は {PARTS_NAME_BYTES} バイトなので reading の neo_name「{s_hw}」にした（見積の印字「{full}」）', item=it)
            return
        if _cp932_len(hw(full).strip()) > PARTS_NAME_BYTES:
            s_auto = shorten_name(full)
            it['name'] = s_auto; it['_full_name'] = full
            self._rev('要確認', '名称の短縮', f'見積の印字「{full}」は NEO の名称欄 {PARTS_NAME_BYTES} バイトに入らないので「{s_auto}」に自動で短くした。'
                      '意味が通らなければ reading の行に neo_name（24 バイト以内）を書く', item=it)

    def _labor_from_wages(self, items: list[dict]) -> None:
        """指数の列が無く技術料だけの書式（書式 F）でレートが決まらないとき: 技術料を 0.1 刻みの指数で説明できるレート（guess_labor_rate）を挙げ、
        複数あれば「技術料 ÷ レート が ADDATA の標準指数と一致する行」の多いレートを採る（2026-09-13 ランクル: 6,000 / 12,000 → 標準一致 12,000）"""
        from guess_labor_rate import guess
        rows = [it for it in items if it.get('wage') and not it.get('manual') and it.get('code')]
        wages = sorted({int(it['wage']) for it in rows if int(it['wage']) > 0})
        if not wages:
            return
        hits, unit = [], self.wage_round or 10
        for unit in ((self.wage_round,) if (self.wage_round or 10) != 10 else (10, 100)):  # 工賃の丸め単位も分からないので 10 円 → 100 円の順に試す（Codex 指摘）
            hits = [r for r, _ in guess(wages, unit=unit)]
            if hits:
                break
        if hits and unit != (self.wage_round or 10):
            self.wage_round = unit
            self.notes.append(f'工賃の丸め単位 {unit} 円（技術料が 0.1 刻みの指数 × レートの {unit} 円丸めでだけ説明できる）→ estimate.wage_round')
        if not hits:
            self._rev('要確認', 'レバーレート', f'技術料 {len(wages)} 種類を 0.1 刻みの指数で説明できるレートが無い。速報の工賃単価か工場に確かめて reading の labor_rate に書く')
            return
        pick, why = hits[0], ''
        if len(hits) > 1:
            eva = set((self.rd.get('hints') or {}).get('eva_codes') or ())
            present = [(int(it['code']), _dcode(it.get('method') or '', it.get('price') or 0, it.get('wage'))) for it in items if it.get('code') and not it.get('manual')]
            score = {}
            for rate in hits:
                n = 0
                for it in rows:
                    try:
                        st = self.parts.cogni_standard(int(it['code']), _dcode(it.get('method') or '', it.get('price') or 0, it.get('wage')), self.grade, self.fva, eva, self.year, present, self.body)
                    except Exception:  # noqa: BLE001  標準が引けない行は数えない
                        st = None
                    if st and st.get('time') and abs(float(st['time']) - int(it['wage']) / rate) < 0.005:
                        n += 1
                score[rate] = n
            ranked = sorted(hits, key=lambda r_: -score[r_])
            if score[ranked[0]] >= 2 and (len(ranked) < 2 or score[ranked[0]] > score[ranked[1]]):
                pick = ranked[0]
                why = '（候補 ' + ' / '.join(f'{r_:,} 円: 標準指数と一致 {score[r_]} 行' for r_ in ranked[:4]) + '）'
            else:
                self._rev('要確認', 'レバーレート', '技術料からのレートの候補が複数あり、ADDATA の標準指数でも決まらない（候補 ' + ' / '.join(f'{r_:,}' for r_ in hits[:6])
                          + ' 円）。速報の工賃単価か工場に確かめて reading の labor_rate に書く')
                return
        self.labor = pick
        self.notes.append(f'レバーレート {pick:,} 円: 技術料を 0.1 刻みの指数で説明できるレート{why}')
        self._rev('判断', 'レバーレート', f'{pick:,} 円（技術料だけの書式。技術料 ÷ レートが 0.1 刻みの指数になるレート{why}）')
        for it in items:  # レートが決まる前に面積だけ分かっていた板金行のランクを引き直す（Codex 指摘）
            area = it.pop('_bankin_area', None)
            if not area or it.get('bankin') or not it.get('wage'):
                continue
            t = round(float(it['wage']) / pick, 2)
            hit = next((rk for rk in 'ABC' if bankin_time(area, rk) is not None and abs(bankin_time(area, rk) - t) < 0.005), None)
            if hit:
                it['bankin'] = {'area': area, 'yes': BANKIN_YES[hit]}
                self.notes.append(f"板金 {it.get('name')} {area}d㎡ 指数 {t} → ランク {hit}（レート決定後）")
            else:
                self.notes.append(f"板金 {it.get('name')} {area}d㎡ 指数 {t} は BANKIN.DB のどのランクとも違う → '#' 手入力")

    # ------------------------------------------------------------------ 塗装
    def _panel_by_code(self, code) -> Optional[dict]:
        """塗装行に部品コード（4 桁）が写されていれば、明細行と同じくそれで 20.DB のパネルを引く（名前照合より確実。コグニ印刷の塗装明細）"""
        if not self.pi:
            return None
        c = re.sub(r'\D', '', str(code or ''))
        if len(c) != 4:  # ちょうど 4 桁のときだけ（5 桁以上の品番や OCR の連結値を先頭 4 桁で別パネルにしない。Codex 指摘）
            return None
        try:
            return self.pi.panel(c)  # 明細行と同じボディ対応の引き方（panel_exact だと別ボディの同コード行を掴む。Codex 指摘）
        except Exception:  # noqa: BLE001  20.DB を引けない車種
            return None

    def _panel_code(self, text: str) -> Optional[dict]:
        if not self.pi:
            return None
        n0 = self.parts.norm_name(text)
        n0 = re.sub(r'ﾊﾟﾈﾙ$', '', n0)
        scored = []
        for pnl in self.pi.panels:
            c = self.parts.norm_name(pnl['name'])
            c = re.sub(r'ﾊﾟﾈﾙ$', '', c)
            s = difflib.SequenceMatcher(None, c, n0).ratio()
            if c[:1] in ('L', 'R') and n0[:1] in ('L', 'R') and c[:1] != n0[:1]:
                s -= 0.5
            scored.append((s, pnl))
        if not scored:
            return None
        best_s = max(s for s, _ in scored)
        if best_s < 0.6:
            return None  # 採用するかは名称の近さだけで決める（ボディで下駄を履かせない）
        top = [p for s, p in scored if s >= best_s - 1e-9]
        # 20.DB は同じ名前のパネルをボディごとに別コードで持つことがある
        # （W90 ハイエース: 4800 L ｸｵ-ﾀﾊﾟﾈﾙ 面積 150 = ボディ 10 / 4801 同名 面積 236 = ボディ 20）。
        # 名称が同点のときだけ、この車のボディ専用 → 全ボディ共通 の順で選ぶ
        bc = getattr(self.pi, 'body_code', 0)
        if len(top) > 1 and bc:
            top = [p for p in top if p.get('body') == bc] or [p for p in top if not p.get('body')] or top
        if bc and top[0].get('body') not in (bc, 0):
            # この車のボディ用の行も全ボディ共通の行も無い。他ボディの面積＝別の塗装指数になる
            self.notes.append(
                f"★ 塗装パネル「{text}」→ {top[0]['code']}: この車のボディ {bc} 用の行が無く、"
                f"ボディ {top[0]['body']} の面積 {top[0]['area']} を使う。見積書の dm² と突き合わせる")
        if len({p['code'] for p in top}) > 1:
            # 名前も点数も同じパネルが複数。先頭を採るが、面積＝塗装指数が変わるので知らせる
            self.notes.append(
                f'★ 塗装パネル「{text}」: 同じ名前の候補が複数ある（'
                + ' / '.join(f"{p['code']} 面積 {p['area']}" for p in top[:4])
                + f"）。{top[0]['code']} を採った。見積書の dm² と違うなら reading の name をコグニのパネル名に直す")
        if best_s < 0.8:
            # 名前が近いだけ（例 'ﾙｰﾌｻｲﾄﾞ' → 'L ｻｲﾄﾞｼﾙﾊﾟﾈﾙ' が 0.6 台）。20.DB に無い部位なら手入力の塗装行にするのが正しい（判断規則 10-20）
            self.notes.append(f"★ 塗装パネル「{text}」→ {top[0]['code']} {top[0]['name'].strip()}: 名前が近いだけ（一致度 {best_s:.2f}）。"
                              "違う部位なら reading の name を直すか、estimate.json で手入力の塗装行（manual: true）にする")
        return top[0]

    def _put_special(self, out: dict, key: str, rec: dict, name: str) -> None:
        """加算基礎・ブース・ワックス・バンパは 1 案件 1 つ。2 行目が来たら黙って上書きせず注記する（監査 8）"""
        if key in out and out[key] != rec:
            self.notes.append(f'塗装 {name}: {key} の行が 2 回あるので後の行を採った（前 {out[key]} / 後 {rec}）。別の項目なら reading の name を直す')
        out[key] = rec

    def paint(self) -> Optional[dict]:
        p = self.rd.get('paint')
        if not p:
            return None
        out: dict = {}
        for k, v in p.items():  # 塗料/塗膜/割合/材料/総額に加え、estimate.json の塗装キー（panels/base/booth/bumper_*/wax/sealing/frame/other/低隠蔽性 …）をそのまま通す
            if k in ('lines', 'note') or v in (None, ''):
                continue
            out[k] = v
        lines = p.get('lines') or []
        if not lines or self.generic:
            if 'total' not in out and lines:
                out['total'] = sum(int(float(_num(l.get('wage')) or 0)) for l in lines)
            return out
        panels, other = list(out.get('panels') or []), list(out.get('other') or [])
        auto_manual: list = []  # 下書きが作った手入力の塗装行（rec, 見積の名前）
        for ln in lines:
            name = _hw_kana(ln.get('name') or '')
            t = float(_num(ln['index'])) if _num(ln.get('index')) != '' else None
            w = int(float(_num(ln['wage']))) if _num(ln.get('wage')) != '' else None
            n = _nfkc(name).replace(' ', '')
            if re.search(r'加算基礎', n):
                self._put_special(out, 'base', {k: v for k, v in (('index', t), ('wage', w)) if v is not None}, name)
                continue
            if re.search(r'ブース|ﾌﾞｰｽ', n):
                self._put_special(out, 'booth', {'index': t or 0.0, 'wage': w or 0}, name)
                continue
            if re.search(r'ﾜｯｸｽ|ワックス|防錆', n):
                cnt = int(ln.get('count') or (round(t * 10) if t else 1))
                self._put_special(out, 'wax', {k: v for k, v in (('count', cnt), ('index', t), ('wage', w)) if v is not None}, name)
                continue
            mm = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*m(?![a-z])', n, re.I)  # 長さ（'ボデーシーリング 10.00m'）
            if re.search(r'シ[ー\-]リング', n) and not re.search(r'材|剤|費用', n)                     and (re.search(r'ボデ[ー\-ィ]', n) or mm):
                # ボデーシーリング（塗装のシーリング作業）。'シーリング材料費' のような費用行は拾わない
                val = {'m': float(mm.group(1))} if mm else {}
                if t is not None:
                    val['index'] = t
                if w is not None:
                    val['wage'] = w
                self._put_special(out, 'sealing', val, name)
                continue
            if re.search(r'ﾊﾞﾝﾊﾟ|バンパ', n):
                # 前後: ﾘﾔ/ﾘｱ/Rr/後、または「R ﾊﾞﾝﾊﾟ」「RRﾊﾞﾝﾊﾟ」の略記（部品側の norm_name と同じ前後略記）はリヤ。それ以外（F/Fr/ﾌﾛﾝﾄ/無印）はフロント
                is_rear = bool(re.search(r'ﾘﾔ|ﾘｱ|リヤ|リア|Rr|後', n)) or bool(re.match(r'^R{1,2}(ﾊﾞﾝﾊﾟ|バンパ)', n))
                key = 'bumper_rear' if is_rear else 'bumper_front'
                mth = None
                for cand in sorted(BUMPER_DISPOSAL, key=len, reverse=True):  # 長い別名から（外傷修正小 > 外傷修正 > 変形）
                    if cand in n:
                        mth = BUMPER_DISPOSAL[cand][1]
                        break
                if mth is None:
                    if re.search(r'修正|修理', n):
                        mth = '変形修正'
                        self.notes.append(f'バンパ塗装「{name}」: 修正の種類が読めないので 変形修正 にした（外傷修正小/大なら reading の name に書く）')
                    else:
                        mth = '新品'
                color = '二色' if '二色' in n or '2色' in n else '一色'
                self._put_special(out, key, {k: v for k, v in (('method', mth), ('color', color), ('index', t), ('wage', w)) if v is not None}, name)
                if _flag(ln.get('draft'), 'paint.lines[].draft'):  # 文字列 "false" を真に潰さない（Codex 指摘）
                    out[key]['draft'] = True
                continue
            m = re.match(r'^(.*?)(取替|新品|交換|修正|修理)\s*(1/[123])?$', _clean_panel_line(n))
            if m:
                pname, mth, ratio = m.group(1), m.group(2), m.group(3) or ''
                pnl = self._panel_by_code(ln.get('code')) or self._panel_code(pname)
                if pnl:
                    method = '取替' if mth in ('取替', '新品', '交換') else '修理'
                    if method == '修理' and not ratio:
                        ratio = '1/1'
                        self.notes.append(f'塗装 {name}: 修正の塗装面積が読めないので 1/1 にした')
                    # 面積は 20.DB の「パネル面積」。見積書に印字される dm²（'196dm²'）は塗装面積で別物なので入れない
                    # （実機コグニも外板パネル画面でパネル面積と塗装面積を別の列に持つ。2026-09-10 確認）
                    rec = {'code': pnl['code'], 'name': pnl['name'].strip(), 'method': method, 'area': pnl['area'], 'ratio': ratio if method == '修理' else ''}
                    if t is not None:
                        rec['index'] = t
                    if w is not None:
                        rec['wage'] = w
                    panels.append(rec)
                    continue
            if _ADD_ITEM_RE.search(n) or (t is None and w is None):
                # 工程の名前（アンダーコート・内板調色・チッピング・ヒンジ …）は「追加項目」（材料代の対象外）。実案件 NEO の追加項目の名前から
                other.append({k: v for k, v in (('name', name), ('index', t), ('wage', w)) if v is not None})
                self.notes.append(f'塗装 {name}: 工程の名前なので 追加項目（paint.other。材料代の対象外）にした')
                continue
            # 部位の名前なのに 20.DB のパネルに無い（ルーフサイド・リヤボデーフロア・テールゲート …）: 人はコグニの外板パネル画面で
            # 「行追加」して手入力する（PaintingPanel の手入力の塗装行。材料代の対象）。実案件 NEO 354 本・631 行（2026-09-13）
            # 名称は半角カナ（_hw_kana 済みの name）で渡す。コグニで行追加する人の入力は半角カナが大半（実案件 NEO 209 行中 約 95%、長音は 'ｰ' が多い）。
            # 工場の印字（全角のことがある）そのままより、人が打つ形に寄せる。生成器は渡された名称を直さずに書く
            mm2 = re.match(r'^(.*?)\s*(取替|新品|交換|修正|修理)\s*(?:1/[123])?$', name)
            rec_m = {'manual': True, 'name': (mm2.group(1).strip() if mm2 and mm2.group(1).strip() else name)}
            if mm2:
                rec_m['method'] = '取替' if mm2.group(2) in ('取替', '新品', '交換') else '修理'
            if t is not None:
                rec_m['index'] = t
            if w is not None:
                rec_m['wage'] = w
            panels.append(rec_m)
            auto_manual.append((rec_m, name, bool(m)))
        if auto_manual and all(is_manual_panel(x) for x in panels):
            # 20.DB のパネルに 1 行も対応付けできなかった: 名前の書き方がコグニと違う書式の可能性が高いので、全部を手入力の塗装行にはせず
            # 従来どおり一括計上（paint.total）に戻す（下の else 節。行は追加項目の扱いで落とす）
            for rec_m, name, _hm in auto_manual:
                panels.remove(rec_m)
                other.append({'name': name, **{k: rec_m[k] for k in ('index', 'wage') if k in rec_m}})
        else:
            for _rec, name, had_method in auto_manual:
                self.notes.append(f'塗装 {name}: ' + ('20.DB のパネルに無い部位なので' if had_method else
                                                      '修理方法（取替/修理）の印字が無く 20.DB のパネルに対応付けていないので')
                                  + ' 手入力の塗装行（外板パネルの行追加。材料代の対象）にした。工程（追加項目）なら reading の name に工程名を、'
                                  '20.DB のパネルなら 取替/修理 を書く')
        if panels:
            out['panels'] = panels
            if other:
                out['other'] = other
        elif any(k in out for k in ('bumper_front', 'bumper_rear')) and not other and all(k in BUMPER_ONLY_KEYS for k in out):
            # 外板パネルが無くバンパだけ塗る見積: 生成器は `panels: []` + bumper_* を詳細塗装として書く
            # （加算基礎数値なし・バンパ加算基礎は COM/BAN.DB。実機 2026-09-12 W66 cogni_W66w で全列一致）。
            # 付加塗装（wax 等）や対応付けできない行（other）が混じるときは従来どおり一括計上に戻す（実機未確認のため）
            out['panels'] = []
            self.notes.append('塗装: 外板パネルは無くバンパだけなので、パネル無しのバンパ塗装（加算基礎数値なし・バンパ加算基礎は BAN.DB の標準）で書く')
        else:  # 生成器は panels があるときだけ詳細塗装。無ければ詳細キーをすべて落として一括計上（paint.total）
            # sealing も落とす。残すと一括計上（paint.total に全塗装行の工賃が入る）と二重に乗る
            dropped = [k for k in ('base', 'booth', 'wax', 'bumper_front', 'bumper_rear', 'sealing') if k in out]
            for k in dropped:
                out.pop(k, None)
            if dropped or other:
                self.notes.append('塗装: 20.DB のパネルに対応付けできた行が無いので一括計上（paint.total）にした。パネル別にするなら reading の name をコグニのパネル名に直す')
        s_lines = sum(int(float(_num(l.get('wage')) or 0)) for l in lines)
        if 'total' not in out:
            out['total'] = s_lines
        else:  # 印字の塗装計と塗装行の合計が違う = 行の写し漏れか、total に骨格塗装・付加塗装が入っている（監査 9）
            try:
                t_in = int(float(_num(out['total']) or 0))
            except ValueError:
                t_in = None
            if t_in is not None and t_in != s_lines:
                self.notes.append(f'塗装計: 印字 {t_in:,} と塗装行の工賃合計 {s_lines:,} が違う（差 {t_in - s_lines:+,}）。行の写し漏れか、内板骨格・付加塗装が含まれていないか確かめる')
        # 材料代が「塗装工賃計 × 割合」の一括四捨五入と一致するなら割合だけ渡す（コグニは費用割合モード = MaterialTotalbyManual ''。額を渡すと '*' 手入力扱いになる。実機 2026-09-08 exp_paint_B）
        try:
            mat = int(float(_num(out.get('material')) or 0)); rate = float(_num(out.get('material_rate')) or 0); tot = int(float(_num(out.get('total')) or 0))
        except ValueError:
            mat = rate = tot = 0
        if mat and rate and tot and mat in (material_default(tot, rate), int(tot * rate / 100 + 0.5)):  # 生成器と同じ 10 円丸め（material_default）か 1 円四捨五入のどちらかに一致
            out.pop('material')
            self.notes.append(f'材料代 {mat:,} = 塗装工賃計 {tot:,} × {rate:g}%（一括四捨五入）と一致 → 費用割合モード（material を渡さない）')
        return out

    # ------------------------------------------------------------------ 費用・合計
    def expenses(self) -> list[dict]:
        out = []
        for e in self.rd.get('expenses') or []:
            kind = 'parts' if '部品' in _nfkc(e.get('in') or e.get('kind') or '') or e.get('kind') == 'parts' else 'wage'
            rec = {'name': _hw_kana(e.get('name') or ''), 'amount': _money_or(e.get('amount'), name=f"費用「{e.get('name')}」の金額"), 'kind': kind}
            if _flag(e.get('taxfree'), 'expenses[].taxfree') or '非課税' in _nfkc(e.get('in') or ''):
                rec['taxfree'] = True
            out.append(rec)
        return out

    # 一括計上（paint.total だけ）に戻すとき落とす塗装の詳細キー。
    # 生成器は panels があるときだけ詳細塗装を書くので、panels だけ消して他を残すと生成が落ちる
    MATERIAL_RATE_MIN, MATERIAL_RATE_MAX = 10.0, 90.0  # 材料代割合の常識的な幅（コグニの費用割合は 40〜60% が多い）
    # 一括計上に戻したときに残してよい塗装のキー。**これ以外は全部落とす**（列挙式にすると
    # 塗装キーが増えたときに落とし忘れ、工場見積の一式に上乗せされる）。
    # auto_panels の paint.total は工場の一式（材料も付加塗装も内板骨格塗装も込み）なので、内訳は 1 つも残せない
    PAINT_KEEP_ON_LUMP = ('total', 'paint', 'coat', 'hf', 'auto_panels', 'note')

    def _back_to_lump(self, paint: dict, why: str) -> None:
        """詳細塗装をやめて一括計上（paint.total だけ）に戻す"""
        dropped = [k for k in list(paint) if k not in self.PAINT_KEEP_ON_LUMP]
        for k in dropped:
            paint.pop(k, None)
        self._paint_split = None
        self.notes.append(f'paint.auto_panels: {why}。一括計上に戻す'
                          + (f'（落とした塗装キー: {", ".join(dropped)}）' if dropped else ''))

    def auto_paint_panels(self, est: dict) -> None:
        """`paint.auto_panels: true` のとき、**明細の取替・板金・修理行**から塗装パネルを起こす。
        コグニは部品を入れて塗装ページを開いた時点で 20.DB にあるパネルを自動で計上し、加算基礎数値も入れる。
        工場見積が「塗装費用 一式」しか出していない案件でも、同じ形（パネル別 + 加算基礎）に組み直すためのもの。
        工賃は標準（CHM）に任せ、工場の一式との差は**材料代**で吸収する（工場の数字は動かさない）"""
        paint = est.get('paint') or {}
        if not _flag(paint.get('auto_panels'), 'paint.auto_panels') or paint.get('panels'):
            return
        if not self.pi:
            self._back_to_lump(paint, '20.DB を読めないのでパネルを起こせない')
            return
        # まず塗装パネルごとに候補を集める（同じパネルに取替行と板金行が両方あることがある）。
        # 行を見た順に 1 枚ずつ足すと、先に出た行の修理方法で決まってしまう（Codex 指摘）
        cands: dict = {}
        qty_over = []
        for it in est.get('items') or []:
            code = str(it.get('code') or '').strip()
            mth = str(it.get('method') or '').strip()
            if not code or it.get('manual') or it.get('reserve'):
                continue
            if mth not in ('取替', '板金', '鈑金', '修理'):
                continue  # 脱着・点検などは塗らない
            # panel()（枝番まで見る）で引く。同じパネルがボディごとに別コードになっている車があるため
            # （W90 ハイエース: 明細は 4800 だが、ボディ 20 の塗装パネルは 4801）。
            # ただし前方一致で無関係なパネルを拾わないよう、行き先が同じ 3 桁であることを確かめる
            pnl = self.pi.panel(code)
            if not pnl or pnl['code'][:3] != code[:3]:
                continue
            area = int(pnl.get('area') or 0)
            if area in (0, 9999):
                continue  # 9999 はバンパ（23.DB 側。paint.bumper_* で扱う）
            try:
                qty = int(float(it.get('qty') or 1))
            except (TypeError, ValueError):
                qty = 1
            if qty > 1:
                # 左右をまとめた行（コグニは左右別コードなので、数量 2 は塗装パネル 2 枚になる）か、
                # 同じパネルを複数枚。どちらも自動では起こせない。黙って 1 枚にすると塗装工賃が過少になり、
                # その差を fit_paint_total が材料代へ移してしまう
                qty_over.append(f"{code} {it.get('name', '')}（数量 {qty}）")
            e = cands.get(pnl['code'])
            if e is None:
                cands[pnl['code']] = {'pnl': pnl, 'area': area, 'methods': [mth], 'codes': [code]}
            else:
                e['methods'].append(mth)
                if code not in e['codes']:
                    e['codes'].append(code)
        if qty_over:
            self._back_to_lump(paint, '数量 2 以上の塗装対象行があるので自動では起こせない（'
                                      + ' / '.join(qty_over[:4]) + '）。左右は 1 行ずつに分ける（判断規則 10-3）')
            return
        panels = []
        for pcode, e in cands.items():
            # 同じパネルに取替行があれば塗装も「取替」（コグニは取替パネルを新品塗装で計上する）
            method = '取替' if '取替' in e['methods'] else '修理'
            ratio = ''
            if method == '修理':
                # コグニの自動連動は修理パネルを 1/2 で起こす（実機 2026-09-12 W66 修理 5 枚すべて 1/2）。
                # CHM に 1/2・1/3 の列が無いパネル（ロッカパネルアウタ 2602 など）は 1/1 固定（同日 W66 2602: '1/1'・1.3）
                ratio = '1/2'
                try:
                    row = self.pi.chm_row_for(e['pnl'], 3)
                except Exception:
                    row = None
                if row and row.get('r12') is None and row.get('r13') is None:
                    ratio = '1/1'
            panels.append({'code': pcode, 'name': e['pnl']['name'].strip(), 'method': method,
                           'area': e['area'], 'ratio': ratio})
            if len(e['codes']) > 1 or len(e['methods']) > 1:
                self.notes.append(f"paint.auto_panels: 塗装パネル {pcode} {e['pnl']['name'].strip()} に"
                                  f"明細 {' / '.join(e['codes'])}（{' / '.join(e['methods'])}）が行き着いた。"
                                  f'1 枚にまとめて「{method}」で起こした')
        if not panels:
            self._back_to_lump(paint, '明細に 20.DB のパネルが無いのでパネルを起こせない')
            return
        paint['panels'] = panels
        names = ' / '.join(f"{x['name']}({x['method']})" for x in panels)
        self.notes.append(f'paint.auto_panels: 明細から塗装パネル {len(panels)} 枚を起こした —— {names}。'
                          '加算基礎数値は枚数から標準で入る')

    def fit_paint_total(self, est: dict) -> None:
        """`paint.auto_panels` で起こしたパネルの塗装工賃計と、工場見積の塗装一式（paint.total）の差を
        **材料代**で埋める。生成器を 1 回試走させて塗装工賃計を実測する（標準指数・加算基礎・付加塗装込み）"""
        paint = est.get('paint') or {}
        if not _flag(paint.get('auto_panels'), 'paint.auto_panels') or not paint.get('panels'):
            return
        want = _num(paint.get('total'))
        if want == '' or not float(want):
            self._back_to_lump(paint, '工場見積の塗装費用（paint.total）が無いので材料代を決められない')
            return
        want = int(float(want))
        _mat_printed = _num((self.rd.get('paint') or {}).get('material'))
        if _mat_printed != '' and float(_mat_printed) > 0:
            # 工場見積に材料代の内訳が印字されているのに auto_panels が付いている。材料代で差を吸収すると
            # 工場の材料代を黙って書き換えることになる（合計は合うのに中身が違う）。Codex 指摘 2026-09-12
            self._back_to_lump(paint, f'★ 工場見積に材料代 {int(float(_mat_printed)):,} の内訳が印字されている。'
                                      'auto_panels は「一式しか無い」見積のためのもので、材料代を置き換えてはいけない。'
                                      'reading の paint.auto_panels を外すか、塗装行を印字どおり写す')
            return
        import copy
        probe = copy.deepcopy(est)
        probe['paint']['material'] = 1  # 0/未指定だと既定率で自動計算されるので 1 円を仮置き
        probe['paint'].pop('material_rate', None)
        probe.pop('totals', None)
        try:
            _neo, rep_ = self.nb.build(probe, probe['vehicle'], hints=probe.get('hints'),
                                       labor_rate=probe.get('labor_rate'), est_date=probe.get('est_date'),
                                       insurance=probe.get('insurance'))
        except Exception as e:  # noqa: BLE001
            self._back_to_lump(paint, f'生成器の試算に失敗（{e}）')
            return
        wage = int(rep_['totals'].get('paint') or 0) - 1  # 塗装計（材料込）から仮置きの 1 円を引く = 塗装工賃計
        material = want - wage
        rate = round(material * 100.0 / wage, 1) if wage else 0
        if material <= 0:
            self._back_to_lump(paint, f'自動計上の塗装工賃 {wage:,} が工場の一式 {want:,} を超える（材料代 {material:,}）'
                                      '。パネルの起こしすぎか一式の読み違い')
            return
        if not (self.MATERIAL_RATE_MIN <= rate <= self.MATERIAL_RATE_MAX):
            self._back_to_lump(paint, f'★ 材料代 {material:,} が塗装工賃 {wage:,} の {rate}% になる'
                                      f'（常識の幅は {self.MATERIAL_RATE_MIN}〜{self.MATERIAL_RATE_MAX}%）'
                                      '。明細に塗装対象の部品が足りない（手入力の板金作業など）疑いがある')
            return
        paint['material'] = material
        paint['total'] = wage
        paint['material_rate'] = rate  # 画面の「材料代割合」を実態に合わせる（金額は手入力 material が優先）。
        # 入力に割合が書いてあっても上書きする —— 一式しか無い見積で割合だけ残すと画面の表示と金額が食い違う
        # 見積書の合計欄は「塗装費用 一式」しか無いので、内訳（塗装工賃計・材料代）はこちらで決めた値に置き換える。
        # 置き換えないと run_case の項目別検算が『塗装工賃計 = 一式』のままになって落ちる。
        # 入力の reading（self.rd）は書き換えない —— 呼び出し元の dict を汚さないよう、totals() で被せる
        self._paint_split = {'paint': wage, 'material': material, 'paint_total': want}
        self.notes.append(f'paint.auto_panels: 塗装工賃計 {wage:,}（標準指数）＋ 材料代 {material:,}（{rate}%）'
                          f' = 工場見積の塗装費用 {want:,} に一致させた（合計欄の内訳もこの配分に直した）')

    def apply_target_total(self, est: dict) -> Optional[dict]:
        """reading.target_total（税込の指定合計。協定額の NEO）があれば、塗装材料代でその額に合わせる。
        生成器（NeoBuilder.build）を材料代 1 円で 1 回走らせて課税小計を実測し（標準補完される工賃・内板骨格・付加塗装をすべて含む）、
        材料代 = 目標の課税小計 − (実測課税小計 − 1) とする。課税小計 S は S + 消費税(10% 四捨五入) + 非課税費用 = 指定額 を満たす整数。
        条件: 詳細塗装（paint.panels）であること。戻り値 = 見積書合計欄相当の totals（無理なら None で注記）"""
        target_raw = _num(self.rd.get('target_total'))  # '715,000' のように印字どおりでも可
        if target_raw == '' or not float(target_raw):
            return None
        target = int(float(target_raw))
        paint = est.get('paint') or {}
        _mat_printed = _num((self.rd.get('paint') or {}).get('material'))
        if _mat_printed != '' and float(_mat_printed) > 0 and not _flag(self.rd.get('target_total_replaces_material'), 'target_total_replaces_material'):
            # 工場見積に材料代が印字されているなら、協定額合わせで材料代を動かすのは「工場の数字を動かす」ことになる。
            # 損保が「材料代で調整」と明示した案件だけ reading に target_total_replaces_material: true を書いて通す（Codex 指摘 2026-09-12）
            self.notes.append(f'★ target_total {target:,}: 工場見積に材料代 {int(float(_mat_printed)):,} が印字されているので材料代では調整しない。'
                              '損保の指示で材料代を動かすなら reading に "target_total_replaces_material": true を書く')
            return None
        if not paint.get('panels'):
            self.notes.append(f'target_total {target:,}: 塗装の詳細（panels）が無いので材料代で調整できない。paint を詳細にするか手で合わせる')
            return None
        unknown = [it.get('name') for it in (est.get('items') or []) if not it.get('manual') and not it.get('reserve')
                   and it.get('wage') is None and it.get('index') is None and it.get('method') in ('取替', '脱着', '脱着修理')]
        if unknown:  # 工賃未確定（生成器が標準で補完する）行があると、読み取り漏れの工賃を材料代で隠してしまうので調整しない
            self.notes.append(f'target_total {target:,}: 工賃未確定（wage/index とも無い取替・脱着）の行 {len(unknown)} 件があるので調整しない: {unknown[:5]}')
            return None
        import copy
        probe = copy.deepcopy(est)
        probe['paint']['material'] = 1  # 0/未指定だと生成器が既定率で材料代を自動計算するので 1 円を仮置き
        probe.pop('totals', None)
        try:
            _, rep = self.nb.build(probe, probe['vehicle'], hints=probe.get('hints'), labor_rate=probe.get('labor_rate'), est_date=probe.get('est_date'), insurance=probe.get('insurance'))
        except Exception as e:  # noqa: BLE001
            self.notes.append(f'target_total {target:,}: 生成器の試算に失敗（{e}）。調整しない')
            return None
        tt = rep['totals']
        ex_nt = sum(_money_or(e.get('amount'), name='非課税費用の金額') for e in (est.get('expenses') or []) if _flag(e.get('taxfree'), 'expenses[].taxfree'))
        base = int((target - ex_nt) / 1.1)
        _tr = str(est.get('tax_round') or '四捨五入')  # 生成器と同じ消費税の計算単位で解く（Setting.tx_ArrangeFlag）
        _tax = (lambda s_: (s_ * 10) // 100) if _tr == '切り捨て' else ((lambda s_: -((-s_ * 10) // 100)) if _tr == '切り上げ' else (lambda s_: (s_ * 10 + 50) // 100))
        S = next((s_ for s_ in range(base - 3, base + 4) if s_ + _tax(s_) + ex_nt == target), None)
        if S is None:
            self.notes.append(f'target_total {target:,}: 消費税 10% {_tr}で合計がその額になる課税小計が無い（1 円ずらした額を指定する）')
            return None
        sub_wo = int(tt.get('subtotal') or 0) - 1  # 材料代を除いた課税小計（生成器の実測）
        material = S - sub_wo
        if material <= 0:  # 0 も不可: 生成器は material 0 を未指定扱いにして既定率で再計算する
            self.notes.append(f'target_total {target:,}: 材料代が {material:,} 円（0 以下）になる。工賃か塗装の指数を下げる必要がある')
            return None
        pw = int(tt.get('paint') or 0) - 1  # 生成器の塗装計（材料込）から仮置きの 1 円を除いた塗装工賃計（other・内板骨格塗装込み）
        rate = round(material * 100.0 / pw, 1) if pw else 0
        # 断るときは paint に何も書かずに戻る（書いてから戻ると、調整しないと言いながら値だけ残る）
        if _flag(paint.get('auto_panels'), 'paint.auto_panels') and not (self.MATERIAL_RATE_MIN <= rate <= self.MATERIAL_RATE_MAX):
            # 起こしたパネルは妥当でも、協定額が離れていると材料代が非常識な割合になる
            self.notes.append(f'target_total {target:,}: auto_panels で起こした塗装だと材料代が塗装工賃の {rate}% になる'
                              f'（常識の幅は {self.MATERIAL_RATE_MIN}〜{self.MATERIAL_RATE_MAX}%）。調整しない')
            return None
        paint['material'] = material
        paint['total'] = pw
        if _flag(paint.get('auto_panels'), 'paint.auto_panels'):
            paint['material_rate'] = rate  # 画面の割合も実態に合わせる（fit_paint_total と同じ扱い）
        self.notes.append(f'target_total {target:,}: 課税小計 {S:,} = 生成器実測 {sub_wo:,}（部品 {int(tt.get("parts") or 0):,} + 工賃 {int(tt.get("wage") or 0):,} + 塗装工賃 {pw:,} + 内骨 {int(tt.get("frame") or 0):,} + 費用/値引）+ 材料 {material:,}（塗装工賃の {rate}%）')
        return {'parts': int(tt.get('parts') or 0), 'wage': int(tt.get('wage') or 0), 'paint': pw, 'material': material, 'paint_total': pw + material,
                'frame': int(tt.get('frame') or 0), 'expense_parts': int(tt.get('expense_parts') or 0), 'expense_wage': int(tt.get('expense_wage') or 0),
                'expense': int(tt.get('expense_parts') or 0) + int(tt.get('expense_wage') or 0), 'discount': int(tt.get('discount') or 0),
                'taxable': S, 'tax': _tax(S), 'total': target}  # 消費税は tax_round と同じ計算単位（Codex e18）

    def totals(self, items: list[dict], paint: Optional[dict], expenses: list[dict]) -> dict:
        t = {k: (_money_or(v, None, name=f'合計欄の {k}') if (isinstance(v, str) and k not in ('neo_total_reason', 'tolerance_reason', 'note')) else v) for k, v in (self.rd.get('totals') or {}).items()}  # 印字どおりの '95,000' を数値に（下流の int() が落ちない）
        t = {k: v for k, v in t.items() if v is not None}
        # 生成器の totals.expense_parts / expense_wage は非課税分も含む（estimate_to_neo.py の hy_parts + hy_parts_nt）ので、
        # 検算がずれないよう同じ定義で書く。非課税だけを別に見たいときは expense_taxfree を使う
        ex_p = sum(e['amount'] for e in expenses if e['kind'] == 'parts')
        ex_w = sum(e['amount'] for e in expenses if e['kind'] != 'parts')
        ex_free = sum(e['amount'] for e in expenses if _flag(e.get('taxfree'), 'expenses[].taxfree'))
        if ex_free:
            t['expense_taxfree'] = ex_free
        if t.get('expense') is not None and int(t['expense']) != ex_p + ex_w:
            t['expense_printed'] = t['expense']  # 見積書の「諸費用計」（費用工賃だけ等）。検算は費用部品＋費用工賃で行う
        t['expense'] = ex_p + ex_w
        t['expense_parts'] = ex_p
        t['expense_wage'] = ex_w
        if paint and paint.get('total') is not None and paint.get('material') is not None:
            t.setdefault('paint', paint['total']); t.setdefault('material', paint['material'])
            t.setdefault('paint_total', int(paint['total']) + int(paint['material']))
        t.update(getattr(self, '_paint_split', None) or {})  # auto_panels で決めた塗装の内訳は印字より優先する
        return t

    def build(self) -> dict:
        items = self.items()
        paint = self.paint()
        expenses = self.expenses()
        est: dict = {
            'source': self.rd.get('source', ''), 'issuer': self.rd.get('issuer', ''), 'est_date': self.rd.get('est_date', ''),
            'vehicle': self.vehicle, 'customer': self._fit_texts(self.rd.get('customer') or {}, ('name', 'owner_name', 'user_name'), '顧客'),
            'insurance': self._fit_texts(self.rd.get('insurance') or {}, ('factory',), '保険'),
            'labor_rate': self.labor,
        }
        if getattr(self, 'wage_round', 10) != 10:
            est['wage_round'] = self.wage_round
        tr = self.rd.get('tax_round')
        if not tr:  # 合計欄の消費税が 10% の切り捨て/切り上げにだけ一致するなら、その計算単位（コグニの消費税設定）を渡す
            t = self.rd.get('totals') or {}
            try:
                sub_ = int(float(_num(t.get('taxable')) or 0)); tax_ = int(float(_num(t.get('tax')) or 0))
            except ValueError:
                sub_ = tax_ = 0
            if sub_ and tax_ and tax_ != (sub_ * 10 + 50) // 100:
                if tax_ == (sub_ * 10) // 100:
                    tr = '切り捨て'
                elif tax_ == -((-sub_ * 10) // 100):
                    tr = '切り上げ'
        if tr and tr != '四捨五入':
            est['tax_round'] = tr
            self.notes.append(f'消費税の計算単位 {tr}（合計欄の消費税がそれにだけ一致）→ estimate.tax_round（Setting.tx_ArrangeFlag）')
        if self.rd.get('index_policy'):
            est['index_policy'] = self.rd['index_policy']
        hints = dict(self.rd.get('hints') or {})
        excl = set(str(x).strip() for x in (hints.get('eva_exclude') or []) if str(x).strip())  # 生成器にも渡す（部品証拠からの自動採用を外すのは生成器側の判定）  # reading の hints.eva_exclude で自動採用を外す（option_audit の「除外」提案を実行する入口）
        if excl:
            self.notes.append(f"装備を除外: {sorted(excl)}（reading の hints.eva_exclude）")
        adopted = sorted(ch for ch in self.eva_votes if ch not in self.eva_veto and ch not in excl)
        if adopted:
            hints['eva_codes'] = sorted((set(hints.get('eva_codes') or []) | set(adopted)) - excl)
            hints['note'] = (hints.get('note', '') + ' ' if hints.get('note') else '') + '; '.join(
                f"{ch}={self.opts.get(ch, '?')} ← {', '.join(sorted(self.eva_votes[ch]))}" for ch in adopted)
            self.notes.append('装備を採用: ' + hints['note'])
        if excl:
            hints['eva_exclude'] = sorted(excl)  # 生成器にも渡す（部品証拠から拾ったレターを外す）
        vetoed = sorted(ch for ch in self.eva_votes if ch in self.eva_veto)
        if vetoed:
            self.notes.append(f'装備 {vetoed} は同じ品番が装備条件なしの行にもあるので採用しない')
        if hints:
            est['hints'] = hints
        est['items'] = items
        _seen: dict = {}
        for _i in items:
            _c = str(_i.get('code') or '').strip()
            if _c and not _i.get('manual'):  # 修理方法が違えば別行でよい（工場の実 NEO にもある）
                _seen.setdefault((_c, str(_i.get('method') or '')), []).append(_i.get('name') or '')
        for (_c, _m), _names in _seen.items():
            if len(_names) > 1:  # コグニは同じ部品コードの 2 行目を捨てる（実機 H24）。合計が合っていても NEO を開いた時点で変わる
                self.notes.append(f'部品コード {_c} の {_m} の行が {len(_names)} 行ある（{" / ".join(str(n)[:14] for n in _names)}）。'
                                  'コグニはこの形を保持する（実機確認済み）が、左右や前後を取り違えて同じ ref に寄せていないか見直す')
        if paint is not None:
            est['paint'] = paint
        est['expenses'] = expenses
        if self.rd.get('discount'):
            est['discount'] = self.rd['discount']
        if self.rd.get('frame'):
            est['frame'] = self.rd['frame']
        if self.rd.get('adas'):  # ADAS のエーミング作業（生成器が estimate['adas'] を読む）
            est['adas'] = self.rd['adas']
        self.auto_paint_panels(est)   # 明細から塗装パネルを起こす（paint.auto_panels）
        self.fit_paint_total(est)     # 起こしたパネルの工賃と工場の一式の差を材料代で埋める
        tt = self.apply_target_total(est)  # 生成器の実計算で材料代を決める（discount/frame を含めた後）
        est['totals'] = tt if tt else self.totals(items, paint, expenses)
        est['_draft_notes'] = self.notes
        pos = {id(it): i + 1 for i, it in enumerate(items)}
        est['_review'] = [dict({k: v for k, v in r.items() if k != '_item'}, row=pos.get(id(r.get('_item')), '')) for r in self.review]
        return est


def main(src: str, dst: str = '') -> int:
    rd = json.load(open(src, encoding='utf-8-sig'))
    d = Drafter(rd)
    est = d.build()
    dst = dst or os.path.join(os.path.dirname(os.path.abspath(src)), 'estimate.json')
    tmp = f'{dst}.{os.getpid()}.tmp'  # 途中で落ちても既存の estimate.json を壊さない。同じ案件を並行で回しても互いの一時ファイルを踏まないよう PID を入れる
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(est, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, dst)
    car = d.car
    print(f"車両: {car.get('CarNameByUser')} / {car.get('CarCode')} Year {car.get('YearCode')} Grade {car.get('GradeCode')} FVA {car.get('FVACode')} ({d.veh.get('confidence')})")
    print(f"レバーレート {d.labor} | 明細 {len(est['items'])} 行（手入力 {sum(1 for i in est['items'] if i.get('manual'))}）| 装備 {est.get('hints', {}).get('eva_codes', [])}")
    for n in d.notes:
        print('  -', n)
    print('出力:', dst)
    return 0


if __name__ == '__main__':
    _a = sys.argv[1:]
    if not _a or _a[0] in ('-h', '--help'):  # 引数無し・--help で例外を出さず使い方を見せる
        print(__doc__ or '')
        print('使い方: python draft_estimate.py <reading.json> [<出力する estimate.json>]')
        sys.exit(0 if _a else 1)
    sys.exit(main(_a[0], _a[1] if len(_a) > 1 else ''))
