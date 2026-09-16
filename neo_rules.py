#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""見積の読み取り規則のうち、複数の入口で同じでなければならないもの。

このアプリには見積を読む入口が3つある（画面のCSV取り込み・見積書PDFの直接変換・
Addata 照合）。同じ規則をそれぞれの場所に書いていたため、

- 作業区分の対応表を app.py だけ直して auto_matching が古いまま（2026-09-10 と 09-11）
- 指数の括弧書きを app.py だけ直して pdf_to_neo_pipeline が古いまま（2026-09-11）

という取り残しが二度続けて起きた。片方だけ直しても気づけないので、
**規則そのものはここに1つだけ置き**、3つの入口はここを呼ぶ。

このモジュールは他のアプリ内モジュールを import しない（循環を作らないため）。
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

import re
import unicodedata

# ── 作業区分コード ────────────────────────────────────────────────────
# 実機コグニセブンの基本定義ファイル
# （Auda7/AudaData/Const/AnDefine.ini の [WorkSheet]）が定めている対応。
#   Repair<N>=<表示順>,<区分コード>,<名称>,<略号>,<有効>
#   0=取替 / 1=脱着 / 2=修理 / 6=板金 / 3=脱着修理・脱着板金
#   4=点検・調整・点検調整 / 5=分解調整
# 実機の .neo 202件・明細11,254行でもこの対応どおりだった
# （板金121行はすべて 6、分解調整206行はすべて 5）。
#
# **並び順に意味がある。** 完全一致で決まらないときは登録順に部分一致を見るので、
# 「脱着修理」を「脱着」より後ろに置くと 1（脱着）に取られる。
DISPOSAL_MAP = {
    # 複合区分（先に置く）
    '脱着修理': 3, '脱着板金': 3, '脱着鈑金': 3,
    '脱着取替': 1, '脱着清掃': 1,
    '点検調整': 4, '点検清掃': 4,
    '分解調整': 5, '分解清掃': 5,
    '磨き調整': 2,
    # 単独区分
    '取替': 0, '交換': 0, '取換': 0, '取り替え': 0, '取替え': 0,
    '脱着': 1, '取外': 1, '取付': 1, '組付': 1, '脱外': 1,
    '板金': 6, '鈑金': 6,
    # 点検・調整の同義語。CSV や手入力で「光軸」「コーディング」と
    # 直接書かれても、抽出プロンプトが「調整」に寄せる語と同じコード（4）になる。
    '点検': 4, '診断': 4,
    '調整': 4, '光軸': 4, 'フィッティング': 4, 'コーディング': 4,
    '設定': 4, '消去': 4,
    '分解': 5, '清掃': 5,
    # 区分として「磨き」が来たら 2。実機にも DisposalCode=2 の
    # 「磨き調整」10行・「磨き」1行があった。
    # 区分が空欄で品名に「磨き」が入っているだけの行（「ﾎｲｰﾙ研磨」など）は
    # 区分なしのままにする。そちらは品名からの推定側（app.py）で扱う。
    '修理': 2, '補修': 2, '修正': 2, '磨き': 2,
    '穴あけ': 2, 'シーリング': 2,
    # 塗装まわりは AnDefine.ini に区分が無い（実機は塗装テーブルに入れる）。
    # ERParts の1行として出す以上、いちばん近い 2（修理）に寄せる。
    '塗装': 2, 'ペイント': 2, 'ワックス': 2, '加算': 2, 'ブース': 2,
}

NO_DISPOSAL = -1   # 区分なし。実機の自由入力行 536行すべてがこの値だった


def normalize_method(method) -> str:
    """区分の文字列を照合用に揃える。

    完全一致だけで引くと、末尾に空白が付いただけ・「脱着（左）」のように
    補足が付いただけで区分不明になる。記号や括弧書きを落として正規化する。
    """
    t = unicodedata.normalize('NFKC', str(method if method is not None else ''))
    t = re.sub(r'[（(\[【][^）)\]】]*[）)\]】]?', '', t)      # 括弧書きを落とす
    t = re.sub(r'[\s　※*・/／,、]', '', t)
    return t.strip()


def disposal_code(method) -> int:
    """区分の文字列 → 作業区分コード。決められなければ -1（区分なし）。"""
    key = normalize_method(method)
    if not key:
        return NO_DISPOSAL
    code = DISPOSAL_MAP.get(key)
    if code is not None:
        return code
    # 完全一致で決まらなければ部分一致。登録順に見るので複合区分が先に当たる。
    for kw, cd in DISPOSAL_MAP.items():
        if kw in key:
            return cd
    return NO_DISPOSAL


# ── 指数（工数）の読み取り ────────────────────────────────────────────
# 欄まるごとが「(数字)」のときだけ外す。欄の一部だけを外すと、
# 「(1) 0.80」が「1 0.80」→ 数値化で空白が詰まって「10.80」になり、
# 別の指数に化ける（「1.50 (2)」→「1.502」も同じ）。
_INDEX_PARENS_WHOLE = re.compile(r'^\s*[（(]\s*([0-9０-９.．]+)\s*[)）]\s*$')


def strip_index_parens(value) -> str:
    """指数の括弧書きを外す。

    金額の「(5,000)」は会計表記のマイナス（値引き）だが、
    **指数の括弧はただの印字**。コグニセブンが印刷する見積書は
    「加算基礎数値 ( 1.50) 11,000」「ブース加算 ( 0.50) 3,670」のように
    指数を括弧付きで出す（実機の帳票 PDF で確認、2026-09-11）。

    金額と同じ数値変換をそのまま通していたため、コグニ印刷の見積書を
    読み込むと指数のある行が全部「指数なし」になっていた。

    **外すのは欄まるごとが「(数字)」の形のときだけ。**
    「(1) 0.80」のように他の数字が混ざっている欄は触らない
    （外すと空白が詰まって「10.80」という別の指数に化ける）。
    「(左)」のような文字の括弧も触らない。
    """
    t = str(value if value is not None else '')
    m = _INDEX_PARENS_WHOLE.match(t)
    return m.group(1) if m else t

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
