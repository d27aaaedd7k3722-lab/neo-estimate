# -*- coding: utf-8 -*-
"""コグニセブン実機が作った .neo を解析し、実機でないと確定できない項目を出す。

    python3 analyze_real_neo.py <実機が作った.neo>

何を作ってもらえばよいかは docs/引き継ぎ書.md §6 に書いてある。
出力は §6 の A〜K に対応しており、各項目に

    現状: このアプリが決め打ちしている値
    実機: 渡された .neo から読み取った値

を並べる。食い違っていれば、実機の値が正しい。
"""
import io
import os
import sqlite3
import sys
import tempfile

SB = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("XROOT", os.path.dirname(SB))

# app を import する前に os.chdir(ROOT) するので、引数のパスは
# それより先に絶対パスへ直す。でないと相対パスで渡したときに見失う。
_ARG = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None

sys.path.insert(0, ROOT)
os.chdir(ROOT)

import logging  # noqa: E402

logging.getLogger("streamlit").setLevel(logging.CRITICAL)

import app  # noqa: E402


# ── NEO の展開 ────────────────────────────────────────────────────────────

def unpack(neo_bytes):
    """.neo を展開して {内包ファイル名: bytes} を返す"""
    real_ck = app.find_real_cks(neo_bytes)
    if not real_ck:
        raise SystemExit("CKチャンクが見つかりません。コグニセブンの .neo ではないようです。")
    full_raw = app.decompress_neo(neo_bytes, real_ck)
    _mgmt, entries = app.parse_entries(neo_bytes, real_ck[0])
    return app.extract_files(full_raw, entries)


def opendb(blob):
    """SQLite の中身（bytes）を一時ファイルに落として開く"""
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tf.write(blob)
    tf.close()
    return sqlite3.connect(tf.name)


def table_names(conn):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r[0] for r in cur.fetchall()]


def columns(conn, table):
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(%s)" % table)
    return [r[1] for r in cur.fetchall()]


def fetch(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return cols, cur.fetchall()


def pick(cols, row, name, default="(列なし)"):
    return row[cols.index(name)] if name in cols else default


def head(title):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)


def item(label, now, real):
    print("  %-34s 現状=%-22s 実機=%s" % (label, now, real))


# ── A〜E: 明細（AnSMB.txt の ERParts） ────────────────────────────────────

def report_erparts(smb):
    head("A〜E  明細（ERParts）— 1行ずつそのまま出す")
    if smb is None:
        print("  AnSMB.txt が読めませんでした")
        return
    cols, rows = fetch(smb, "SELECT * FROM ERParts ORDER BY LineNo")
    if not rows:
        print("  ERParts が 0 行です")
        return

    show = ["LineNo", "PartsName", "DisposalCode", "WorkCode", "PartsCode", "PartsCodeSub",
            "Quantity", "PartsPriceOutTax", "WageOutTax", "Time", "TimeStandard",
            "PartsPriceByManual", "OrderFlag", "Provisional"]
    show = [c for c in show if c in cols]
    print("  列: " + " / ".join(show))
    print("  " + "-" * 74)
    for row in rows:
        vals = []
        for c in show:
            v = row[cols.index(c)]
            vals.append("''" if v == "" else repr(v) if v is None else str(v))
        print("  " + " | ".join(vals))

    print("")
    print("  ▼ 判定のしかた")
    print("    A 作業区分コード : 品名から入力した区分を思い出し、DisposalCode との対応を見る")
    print("                       （このアプリは 取替=0 / 脱着=1 / 修理=2 で書いている）")
    print("    B 区分を空欄にした行の DisposalCode（このアプリは -1）")
    print("    C PartsPriceByManual / OrderFlag（このアプリは '*' / '9'）")
    print("    D TimeStandard（このアプリは -1）")
    print("    E 枝番の先頭ゼロ: PartsCodeSub が 00101 → 101 になっていないか")

    # 空欄行（品名だけ入れて区分を空にした行）の候補を拾う
    if "DisposalCode" in cols:
        codes = sorted({row[cols.index("DisposalCode")] for row in rows})
        print("")
        print("  この .neo に出てくる DisposalCode: %s" % (codes,))


# ── F〜H: 合計（AnSMB.txt の Total） ─────────────────────────────────────

_TOTAL_KEYS = [
    "ms_PartsTotalOutTax", "ms_WageTotalOutTax",
    "hy_WageTaxTotalOutTax", "hy_PartsNoTaxTotalOutTax", "hy_WageNoTaxTotalOutTax",
    "hy_Wrecker1OutTax", "hy_Wrecker1Tax", "hy_Wrecker1TaxFlag",
    "hy_Wrecker2OutTax", "tx_Total",
]


def report_total(smb):
    head("F〜H  合計（Total）— 費用の置き場所・符号・二重計上")
    if smb is None:
        print("  AnSMB.txt が読めませんでした")
        return
    if "Total" not in table_names(smb):
        print("  Total テーブルがありません")
        return
    cols, rows = fetch(smb, "SELECT * FROM Total")
    if not rows:
        print("  Total が 0 行です")
        return
    row = rows[0]

    print("  ▼ 0 でない欄だけ（費目名の欄も含む）")
    for c, v in zip(cols, row):
        if v in (0, "", None):
            continue
        print("    %-30s = %r" % (c, v))

    print("")
    print("  ▼ §6 の確認項目")
    for k in _TOTAL_KEYS:
        if k in cols:
            print("    %-30s = %r" % (k, row[cols.index(k)]))
    print("")
    print("    F 代車費用   : どの欄に入っているか（このアプリは LineNo=7「写真代他」）")
    print("    G 非課税項目 : 正の値で加算か、実機は減算か")
    print("    H レッカー代 : hy_WageTaxTotalOutTax と hy_Wrecker1OutTax の両方に入っているか、")
    print("                   実機が内訳から再計算するなら二重計上になる")


# ── I: 初度登録の元号（ヘッダXML） ───────────────────────────────────────

def report_era(files):
    head("I  初度登録の元号コード（ヘッダXML）")
    xml = None
    for name, blob in files.items():
        if name.lower().endswith(".xml") or name == "AnSvMail.ini":
            try:
                text = blob.decode("cp932", errors="replace")
            except Exception:
                continue
            if "CarRegistedDate" in text:
                xml = (name, text)
                break
    if not xml:
        print("  CarRegistedDate を含むXMLが見つかりませんでした")
        return
    name, text = xml
    print("  ファイル: %s" % name)
    for tag in ("CarRegistedDateEra", "CarRegistedDateYear", "CarRegistedDateMonth",
                "CarRegistedDate"):
        s = text.find("<%s>" % tag)
        if s < 0:
            continue
        e = text.find("</%s>" % tag, s)
        print("    <%s> = %r" % (tag, text[s + len(tag) + 2:e]))
    print("")
    print("    令和の車なら Era=5 か（このアプリは 明治1〜令和5 で書いている）")


# ── J: AnNote.ini の1レコード長 ──────────────────────────────────────────

def report_annote(files, smb):
    head("J  AnNote.ini の1レコード長")
    blob = files.get("AnNote.ini")
    if blob is None:
        print("  AnNote.ini がありません")
        return
    size = len(blob)
    n_rows = None
    if smb is not None and "ERParts" in table_names(smb):
        _c, rows = fetch(smb, "SELECT COUNT(*) FROM ERParts")
        n_rows = rows[0][0]

    print("  サイズ = %d バイト" % size)
    if n_rows:
        print("  ERParts の行数 = %d" % n_rows)
        for cand in (142, 144):
            ok = "← 一致" if size == cand * n_rows else ""
            print("    %d バイト/行 なら %d バイト %s" % (cand, cand * n_rows, ok))
        if size % n_rows == 0:
            print("  実測 = %d バイト/行" % (size // n_rows))
    print("")
    print("    このアプリは 142 バイト固定長で書いている（仕様書も142）。")
    print("    実機が 144（142＋CRLF）なら、71行目以降が丸ごとずれる。")
    print("")
    print("  ▼ 先頭2レコードを 142 / 144 の両方で切って表示")
    for width in (142, 144):
        print("    --- %d バイト区切り ---" % width)
        for i in range(min(2, max(1, size // width))):
            chunk = blob[i * width:(i + 1) * width]
            print("      [%d] %r" % (i, chunk[:60]))
            tail = chunk[-4:]
            print("           末尾4バイト = %r" % tail)


# ── K: 内包ファイル ─────────────────────────────────────────────────────

def report_files(files):
    head("K  内包ファイルの名前と中身")
    print("  %d ファイル" % len(files))
    for i, (name, blob) in enumerate(files.items(), 1):
        kind = ""
        if blob[:16].startswith(b"SQLite format 3"):
            kind = "SQLite"
        elif blob[:5] in (b"<?xml", b"<Root"):
            kind = "XML"
        print("    %2d. %-24s %8d B  %s" % (i, name, len(blob), kind))


# ── 顧客・車両（AnSvEm0001Ex.db）────────────────────────────────────────

def report_em(files):
    head("参考  AnSvEm0001Ex.db（顧客・車両・保険）")
    blob = files.get("AnSvEm0001Ex.db")
    if blob is None:
        print("  AnSvEm0001Ex.db がありません")
        return
    conn = opendb(blob)
    for t in table_names(conn):
        try:
            cols, rows = fetch(conn, "SELECT * FROM %s" % t)
        except Exception as e:
            print("  %s: 読めません (%s)" % (t, e))
            continue
        print("  [%s] %d 行" % (t, len(rows)))
        for row in rows[:2]:
            shown = [(c, v) for c, v in zip(cols, row) if v not in (0, "", None, -1)]
            for c, v in shown[:24]:
                print("      %-28s = %r" % (c, v))
            if len(shown) > 24:
                print("      … 他 %d 列" % (len(shown) - 24))


def main():
    if _ARG is None:
        print(__doc__)
        return 1
    path = _ARG
    if not os.path.exists(path):
        print("ファイルがありません: %s" % path)
        return 1

    data = io.open(path, "rb").read()
    files = unpack(data)

    smb = None
    if "AnSMB.txt" in files:
        try:
            smb = opendb(files["AnSMB.txt"])
        except Exception as e:
            print("AnSMB.txt を開けません: %s" % e)

    print("解析対象: %s (%d バイト)" % (path, len(data)))
    report_files(files)
    report_erparts(smb)
    report_total(smb)
    report_era(files)
    report_annote(files, smb)
    report_em(files)
    print("")
    print("以上。食い違った項目は docs/引き継ぎ書.md §6 の表を実機の値で更新すること。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
