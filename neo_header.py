#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NEO ファイル先頭 424 バイトの管理領域（コグニセブンの「既存見積」一覧が読むサマリ）

コグニセブンの見積一覧は、内包ファイル（AnSvMail.ini や AnSvEm0001Ex.db）ではなく
**ファイル先頭 424 バイト**から 登録番号・顧客名・車名 を読む。
ここを書かないと、一覧に並んでも「どの車の誰の見積か」が空欄のまま出る。

2026-09-10 に実機コグニセブンで確認したこと:

- 実機が作った `.neo` と `claude_neo_pipeline` の出力は一覧に正しく出る
- このアプリの出力だけ登録番号・顧客名・車名が空欄になる
- 内包ファイルを1つずつ実機のものと差し替えても直らない
  （AnSvMail.ini / AnSvImge.ini / AnSvEm0001Ex.db を差し替えても一覧の表示は変わらない）
- 先頭 424B を実機のものにすると直る
  → 一覧はこの管理領域だけを読んでいる

このアプリは出荷テンプレート（顧客情報が空）の管理領域をそのまま複写していたため、
生成した見積がすべて空欄で並んでいた。

構造（実機の .neo で確認）:

    raw[0:9]    'NEO300101'
    raw[9:424]  2つのビット列ブロックを XOR 0xFF した上でビットシフトして格納

    ブロック A: decodeA = ((raw[9:424] ^ FF) as big-int) << 3
        A[ 74:114]  協定工場名（40B）
        A[114:144]  顧客名（30B）
        A[144:206]  車名（62B）
        A[206:210]  作成日（u16 年, u8 月, u8 日）
        A[210:250]  double×5 = 部品計 / 工賃計 / 塗装計 / 費用計 / 合計（税込）
        A[274:282]  陸運支局(8B)  A[282:288] 分類番号(6B)
        A[288:292]  かな(4B)      A[292:302] 一連番号(10B)

    ブロック B: decodeB = ((raw[330:424] ^ FF) as big-int) << 7
        B[0:4]   保存日（u16 年, u8 月, u8 日）
        B[4:7]   保存時刻（u8 時, u8 分, u8 秒）
        B[7:15]  ライセンスID（8B）

ブロック A の項目はすべて raw[315] より前で終わり、ブロック B は raw[320] より後ろ。
その間は 0 埋めなので、A 側は raw[9:315]、B 側は raw[330:424] だけ書き換える。

移植元: claude_neo_pipeline/neo_header.py（同じ解析結果）。
"""
from __future__ import annotations

import codecs as _codecs
import datetime
import struct
from typing import Optional

MAGIC = b'NEO300101'
A_SHIFT = 3
B_SHIFT = 7
A_END = 315       # raw オフセット。ブロック A の項目はここより前で終わる
B_DATE_OFF = 330  # raw 330 から shift 7 で復号すると先頭が保存日


# ── cp932w: Windows が書く IBM 拡張漢字に合わせる ──────────────────────
# Python の cp932 は「德・髙・﨑」等を NEC 選定（ED/EE 行）で符号化するが、
# Windows（コグニセブン）は IBM 拡張（FA〜FC 行）で書く。氏名に普通に出る字なので、
# ここを合わせないと同じ名前が別のバイト列になる。
_IBM_MAP = None


def _build_ibm_map():
    """cp932 が NEC選定IBM拡張（0xED/0xEE 行）に符号化する字だけを IBM 行に移す。

    「cp932 と違うバイトになる字を全部 IBM 行にする」としてはいけない。
    「㈱」は cp932 の 87 8A（NEC特殊文字）と IBM の FA 58 の両方を持つが、
    実機は 87 8A で書いていた（実機 04011406.neo の協定工場名で確認）。
    置き換えるべきなのは「德・髙・﨑」のように cp932 が ED/EE 行に落とす字だけ。
    """
    m = {}
    for hi in range(0xFA, 0xFD):
        for lo in list(range(0x40, 0x7F)) + list(range(0x80, 0xFD)):
            b = bytes((hi, lo))
            try:
                ch = b.decode('cp932')
            except UnicodeDecodeError:
                continue
            if len(ch) != 1:
                continue
            try:
                cur = ch.encode('cp932')
            except UnicodeEncodeError:
                continue
            if cur != b and cur[:1] in (b'\xed', b'\xee'):
                m[ch] = b
    return m


def encode_cp932w(s: str, errors: str = 'replace') -> bytes:
    global _IBM_MAP
    if _IBM_MAP is None:
        _IBM_MAP = _build_ibm_map()
    out = bytearray()
    for ch in (s or ''):
        b = _IBM_MAP.get(ch)
        out += b if b else ch.encode('cp932', errors)
    return bytes(out)


def _cp932w_search(name):
    if name.lower() != 'cp932w':
        return None
    base = _codecs.lookup('cp932')
    return _codecs.CodecInfo(
        name='cp932w',
        encode=lambda s, errors='strict': (encode_cp932w(s, errors), len(s)),
        decode=base.decode)


_codecs.register(_cp932w_search)


# ── 管理領域の復号・再生成 ─────────────────────────────────────────────
def _dec(raw: bytes, shift: int) -> bytes:
    x = bytes(v ^ 0xFF for v in raw)
    n = len(x) * 8
    v = (int.from_bytes(x, 'big') << shift) & ((1 << n) - 1)
    return v.to_bytes(len(x), 'big')


def _enc(dec: bytes, shift: int, orig_raw: bytes) -> bytes:
    """復号バイト列 → raw。左シフトで落ちた上位 shift ビットは元の値を残す"""
    n = len(dec) * 8
    v = int.from_bytes(dec, 'big') >> shift
    top = (int.from_bytes(bytes(b ^ 0xFF for b in orig_raw), 'big') >> (n - shift)) << (n - shift)
    x = (v | top).to_bytes(len(dec), 'big')
    return bytes(b ^ 0xFF for b in x)


def _fit(sv: str, n: int) -> bytes:
    """cp932w で n バイトに収める。2バイト文字の途中で切らない"""
    bb = encode_cp932w(sv or '')[:n]
    while bb:
        try:
            bb.decode('cp932')
            break
        except UnicodeDecodeError:
            bb = bb[:-1]
    return bb.ljust(n, b'\0')


def decode(raw: bytes) -> dict:
    """管理領域を読む（検査・テスト用）"""
    h = raw[:424]
    a = _dec(h[9:424], A_SHIFT)
    b = _dec(h[B_DATE_OFF:424], B_SHIFT)

    def s(bs):
        return bs.split(b'\0')[0].decode('cp932', 'replace')

    return {
        'agreed': s(a[74:114]),
        'name1': s(a[114:144]),
        'car_name': s(a[144:206]),
        'created': struct.unpack('<HBB', a[206:210]),
        'totals': [struct.unpack('<d', a[210 + 8 * j:218 + 8 * j])[0] for j in range(5)],
        'carno': (s(a[274:282]), s(a[282:288]), s(a[288:292]), s(a[292:302])),
        'saved': struct.unpack('<HBB', b[0:4]) + struct.unpack('<BBB', b[4:7]),
        'license': b[7:15].decode('latin1'),
    }


def build(template_raw: bytes, *, agreed: str = '', name1: str = '', car_name: str = '',
          created: Optional[datetime.date] = None, totals: Optional[list] = None,
          carno: Optional[tuple] = None, saved: Optional[datetime.datetime] = None,
          license_id: Optional[str] = None) -> bytes:
    """テンプレートの管理領域をもとに、サマリ項目を差し替えた 424B を返す"""
    h = bytearray(template_raw[:424])
    if len(h) < 424 or bytes(h[:9]) != MAGIC:
        # 管理領域が想定の形でない。触らずに返す（壊すより空欄のほうがまし）
        return bytes(template_raw[:424])

    a = bytearray(_dec(bytes(h[9:424]), A_SHIFT))
    a[74:114] = _fit(agreed, 40)
    a[114:144] = _fit(name1, 30)
    a[144:206] = _fit(car_name, 62)
    if created:
        a[206:210] = struct.pack('<HBB', created.year, created.month, created.day)
    if totals:
        for j, v in enumerate(list(totals)[:5]):
            a[210 + 8 * j:218 + 8 * j] = struct.pack('<d', float(v))
    if carno:
        dep, div, biz, ser = (list(carno) + ['', '', '', ''])[:4]
        a[274:282] = _fit(dep, 8)
        a[282:288] = _fit(div, 6)
        a[288:292] = _fit(biz, 4)
        a[292:302] = _fit(ser, 10)
    enc_a = _enc(bytes(a), A_SHIFT, bytes(h[9:424]))
    h[9:A_END] = enc_a[:A_END - 9]

    b = bytearray(_dec(bytes(h[B_DATE_OFF:424]), B_SHIFT))
    if saved:
        b[0:4] = struct.pack('<HBB', saved.year, saved.month, saved.day)
        b[4:7] = struct.pack('<BBB', saved.hour, saved.minute, saved.second)
    if license_id:
        b[7:15] = license_id.encode('latin1', 'replace')[:8].ljust(8, b'\0')
    h[B_DATE_OFF:424] = _enc(bytes(b), B_SHIFT, bytes(h[B_DATE_OFF:424]))
    return bytes(h)


def apply(neo: bytes, **fields) -> bytes:
    """生成済みの .neo の管理領域だけ差し替える"""
    if len(neo) < 424:
        return neo
    return build(neo, **fields) + neo[424:]


if __name__ == '__main__':
    import json
    import sys
    for p in sys.argv[1:]:
        raw = open(p, 'rb').read()
        d = decode(raw)
        print(p, json.dumps(d, ensure_ascii=False, default=str))
        rt = build(raw, agreed=d['agreed'], name1=d['name1'], car_name=d['car_name'],
                   created=datetime.date(*d['created']), totals=d['totals'],
                   carno=d['carno'], saved=datetime.datetime(*d['saved']),
                   license_id=d['license'])
        print('  往復で一致:', rt == raw[:424],
              '/ 最初の相違位置', next((i for i in range(424) if rt[i] != raw[i]), None))
