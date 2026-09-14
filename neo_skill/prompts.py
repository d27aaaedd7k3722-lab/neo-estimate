# -*- coding: utf-8 -*-
"""prompts.py — 「読む」段の指示文。規則の文書を要約せず、そのまま入れる（移植ガイド §3-1）。

指示文は vendor の文書から毎回組み立てる（アプリ側に写しを持たない。§6-4）:
  - reference/reading_schema.md（出力の形。短縮記法）
  - reference/format_catalog.md（書式ごとの写し方）
  - SKILL.md 手順 4 の「ページを写す順番と自己チェック」8 項目
LLM にさせるのは「紙に書いてあるとおりに写す」だけ。部品コードの推測・左右の分割・数量の読み替え・区分の言い換え・
レバーレートの決定・塗装パネルの組み立て・費用の分類は draft_estimate.py がする（§3-2）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from . import vendor

HEADER_KEYS = ('source', 'issuer', 'est_date', 'format', 'vehicle', 'customer', 'insurance', 'labor_rate', 'wage_round',
               'index_policy', 'hints', 'paint', 'expenses', 'totals', 'target_total', 'discount', 'frame', 'adas', 'tax_round', 'note')


def _read(path: str) -> str:
    with open(path, encoding='utf-8-sig') as fh:
        return fh.read()


def reading_schema_md() -> str:
    return _read(vendor.reference('reading_schema.md'))


def format_catalog_md() -> str:
    return _read(vendor.reference('format_catalog.md'))


def skill_step4_checklist() -> str:
    """SKILL.md 手順 4 の「ページを写す順番と自己チェック」（番号付き 8 項目）を切り出す。
    見出しの行から、次の段落（「全ページ合格 →」）の手前まで。切り出せなければ止める（黙って空にしない）"""
    text = _read(os.path.join(vendor.SKILL_DIR, 'SKILL.md'))
    m = re.search(r'^ページを写す順番と自己チェック.*?$', text, re.M)
    if not m:
        raise ValueError('SKILL.md に「ページを写す順番と自己チェック」の節が無い（版が変わった。prompts.py を合わせる）')
    rest = text[m.start():]
    end = re.search(r'\n\n全ページ合格', rest)
    block = rest[:end.start()] if end else rest.split('\n\n', 1)[0]
    if not re.search(r'^8\. ', block, re.M):
        raise ValueError('SKILL.md 手順 4 の自己チェックが 8 項目に見えない（版が変わった。prompts.py を合わせる）')
    return block.strip()


ROLE = """あなたは自動車の修理工場が出した見積書（PDF）を、印字どおりに JSON へ写す係です。
この JSON は pdf-to-neo スキルの reading.json（ページ単位の pages/header.json と pages/page_N.json）として、
判断規則を持つプログラム（draft_estimate.py）に渡されます。

## あなたがすること
- 紙に書いてあるとおりに写す。名称（RH/LH・全角半角・スペース）・区分語・指数・工賃・数量・数量分の金額・品番・注記行・塗装行・費用・合計欄。
- 読めない数値は推測せず、その行の comment に `?` と読めた範囲を書く。
- 人に確かめてほしい点は comment を `要確認:` で始める。見積書に印字された明細コメントだけ `NEO:` を付ける。

## あなたがしないこと（プログラムが決める。先回りすると再現性が落ちる）
- 部品コードの推測、左右の分割（数量 2 の行を 2 行にしない）、数量の読み替え、区分の言い換え（「部品」を「取替」にしない）、
  レバーレートの決定、塗装パネルの組み立て、費用の分類、合計を合わせるための金額の調整。
- 見積書に無い行を足さない。合計に合わせるために行を消したり金額を動かしたりしない。

## 出力
- 指示された JSON だけを返す。前置き・説明・コードフェンスは付けない。
- 数値は JSON の数値で（カンマ・¥・円を付けない）。空欄は短縮記法では空のまま、dict では書かない。
"""


def build_system_prompt() -> str:
    """毎回同じ文（キャッシュの対象）。規則の文書はそのまま入れる"""
    return '\n\n'.join([
        ROLE,
        '# 参照文書 1: reading.json の形（reference/reading_schema.md）\n\n' + reading_schema_md(),
        '# 参照文書 2: 書式カタログと写像規則（reference/format_catalog.md）\n\n' + format_catalog_md(),
        '# 参照文書 3: ページを写す順番と自己チェック（SKILL.md 手順 4）\n\n' + skill_step4_checklist(),
    ])


HEADER_TEMPLATE = {
    'source': '', 'issuer': '', 'est_date': '', 'format': '',
    'vehicle': {'model_code': '', 'serial_no': '', 'desig': '', 'category': '', 'reg_date': '', 'color_code': ''},
    'customer': {'name': '', 'reg_no': ''}, 'insurance': {'company': ''},
    'labor_rate': None,
    'paint': {}, 'expenses': [],
    'totals': {'parts': None, 'wage': None, 'paint': None, 'material': None, 'expense': None, 'taxable': None, 'tax': None, 'total': None},
}

PAGE_TEMPLATE = {'page': 1, 'rows_printed': None, 'subtotal': {}, 'marks': {}, 'blocks': [{'title': '', 'rows': []}],
                 'paint_lines': [], 'expenses': []}


def header_task(n_pages: int, vehicle_hint: Optional[dict] = None, source_name: str = '') -> str:
    hint = ''
    if vehicle_hint:
        hint = ('\n\n車検証・速報から分かっている車両情報（見積書と食い違うときは見積書の印字を写し、comment ではなく '
                '`note` に食い違いを書く）:\n' + json.dumps(vehicle_hint, ensure_ascii=False))
    return f"""この見積書 PDF は全 {n_pages} ページです。**明細以外**を pages/header.json の形で書いてください（明細の行はここには書かない）。

書くキー（無いものは省く。値が読めないキーは空文字か null）: {', '.join(HEADER_KEYS)}
- source: "{source_name or 'estimate.pdf'} 書式X"（書式は format_catalog.md の A〜G）
- vehicle: 登録番号・車台番号・型式・型式指定/類別・初度登録（reg_date は "R4.3" のような印字どおり）・カラーNo・グレード名・エンジン・排気量のうち印字されているもの
- customer / insurance: 印字されているもの（氏名・登録番号・保険会社・証券番号など）
- labor_rate: 印字されていればその値。無ければ書かない（プログラムが工賃÷指数で逆算する）
- paint: 塗料・塗膜・高機能塗装・材料代・材料代割合・塗装工賃計（lines はページ側の paint_lines に書くので、ここでは lines を書かない）
- expenses: **ここには書かない**（費用・諸費用は、印字されたページ側の expenses に書く。header とページの両方に書くと「費用の同じ行が 2 回ある」で不合格）
- totals: 合計欄そのまま（parts / wage / paint / material / expense / taxable / tax / total。印字されている項目だけ。税込印字の見積書は reading_schema.md の規則どおり）
- index_policy: 非コグニ書式（指数の列が無い・区分語彙が違う）なら "manual"、それ以外は "auto"
- format: A〜G

雛形:
{json.dumps(HEADER_TEMPLATE, ensure_ascii=False, indent=1)}{hint}

JSON だけを返してください。"""


def page_task(page_no: int, n_pages: int, header: dict) -> str:
    fmt = header.get('format') or ''
    rate = header.get('labor_rate')
    return f"""添付は見積書の {page_no} ページ目（全 {n_pages} ページ）です。このページの**明細**を pages/page_{page_no}.json の形で書いてください。
書式は {fmt or '未分類（format_catalog.md で判断）'}{f'、レバーレート {rate} 円' if rate else ''}。

手順（参照文書 3 の 8 項目どおり）:
1. まず rows_printed（このページに印字された明細行数。注記行・小計行・繰越行は数えない）、印字されていればページ小計 subtotal（parts / wage）、印の数 marks（$ # * @ の個数）を書く
2. 明細を短縮記法 `code|name|method|parts_no|index|qty|price|wage|flags|comment`（`|` は 9 個）で 1 行 1 行写す。ブロック見出しがあれば blocks[].title に
3. 金額は数量分。単価しか印字が無ければ price に 単価×数量、comment に `unit=単価`。数量 1 のまま複数個分の金額は印字どおり
4. 左右・Fr/Rr・上下は印字どおり
5. 小計・消費税・繰越・合計の行は明細に入れない
6. このページに塗装の区画（外板パネル・バンパ・加算基礎数値・ブース・付加塗装・材料代 など）や費用の区画（ショートパーツ・廃棄費用・
   アライメント・診断料・写真代 など）が印字されていれば、その行は rows に入れず paint_lines / expenses（in = 印字の集計先: 部品計 / 作業計 /
   諸費用計 / 非課税）に写す（rows_printed にも数えない）。コグニ印刷ではこれらが明細の表の続きに同じ形で印字される。
   ページ小計 subtotal は印字どおり書く（塗装・費用を含んだ小計でも検算が受ける）。合計欄はここに書かない（header にある）。
   費用・塗装行を header に書かない（両方に書くと二重計上で不合格）
7. 読めない数値は推測せず comment に `?`。確かめてほしい点は `要確認:`。印字された明細コメントだけ `NEO:`
8. 明細の無いページ（表紙・計算書だけ）は rows_printed: 0、blocks: [{{"title": "", "rows": []}}] にする（blocks を空リストにしない）

雛形:
{json.dumps(dict(PAGE_TEMPLATE, page=page_no), ensure_ascii=False, indent=1)}

JSON だけを返してください。"""


def retry_task(page_no: int, fails: list, warns: list, previous: dict) -> str:
    """ページ検算に落ちたときの読み直し。FAIL の文言と前回の出力を渡す（他のページは渡さない）"""
    return f"""{page_no} ページ目の写しを機械検算したところ、次の点で不合格でした。添付の同じページをもう一度見て、写しを直してください。
直すのは検算に落ちた箇所とその原因になった行だけです。**合計を合わせるために行を消したり金額を動かしたりしない**でください
（見積書に印字されている数字がすべてです。行数が合わないなら数え直し、金額が合わないなら読み違えた行を探します）。

不合格（FAIL）:
{chr(10).join('- ' + f for f in fails) or '- （なし）'}
{('注意（WARN）:' + chr(10) + chr(10).join('- ' + w for w in warns)) if warns else ''}

前回の写し（これを直す）:
{json.dumps(previous, ensure_ascii=False, indent=1)}

直した page_{page_no}.json 全体を JSON だけで返してください。"""


def header_shape_retry_task(problem: str, previous) -> str:
    """header.json の返事の形が違う／合計欄が無いときの読み直し（値ではなく形・欠落の問題。Codex 指摘 2026-09-14）"""
    return f"""header.json の写しに問題があります: {problem}
添付の見積書（全ページ）をもう一度見て、header.json を直してください。各項目は決められた形で書きます
（totals / vehicle / customer / insurance / paint はオブジェクト、expenses / adas はオブジェクトの配列。配列や文字列に置き換えない）。
totals（見積書の合計欄）は必ず写します — 検算の拠り所です。値は印字どおりに写します（計算して埋めない）。

前回の header.json（これを直す）:
{json.dumps(previous, ensure_ascii=False, indent=1)[:6000]}

直した header.json 全体を JSON だけで返してください。"""


def header_retry_task(fails: list, warns: list, previous: dict) -> str:
    """合計欄の検算（全体）に落ちたときの header の読み直し"""
    return f"""全ページを束ねて合計欄と突き合わせたところ、次の点で不合格でした。添付の見積書（全ページ）をもう一度見て、
header.json（合計欄・塗装・レバーレート）の写しを直してください。明細の行はここでは直しません。
差額と同じ額の行や費用が手掛かりです。合計欄の数字は印字どおりに写します（計算して埋めない）。
費用（expenses）と塗装行（paint.lines）は header には書きません（印字されたページ側の expenses / paint_lines に写してあります。
「費用の同じ行が 2 回ある」は header に書いたのが原因なので、header からは消します）。

不合格（FAIL）:
{chr(10).join('- ' + f for f in fails) or '- （なし）'}
{('注意（WARN）:' + chr(10) + chr(10).join('- ' + w for w in warns)) if warns else ''}

前回の header.json（これを直す）:
{json.dumps(previous, ensure_ascii=False, indent=1)}

直した header.json 全体を JSON だけで返してください。"""


def page_totals_retry_task(page_no: int, fails: list, previous: dict) -> str:
    """合計欄の検算に落ち、header を直しても合わないとき、各ページを差額のヒント付きで読み直す"""
    return f"""全ページを束ねて合計欄と突き合わせたところ不合格でした（下の FAIL）。header（合計欄）は読み直しても同じでした。
添付の {page_no} ページ目に、写し漏れ・二重写し・金額の読み違いが無いか確かめてください。
差額と同じ額の行が手掛かりです。**合計を合わせるために行を消したり金額を動かしたりしない**でください。
このページに直すところが無ければ、前回の写しをそのまま返してください。

不合格（FAIL）:
{chr(10).join('- ' + f for f in fails) or '- （なし）'}

前回の写し:
{json.dumps(previous, ensure_ascii=False, indent=1)}

page_{page_no}.json 全体を JSON だけで返してください。"""
