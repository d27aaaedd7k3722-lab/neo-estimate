"""
neo_header.py — NEO 先頭 424B 管理領域（既存見積一覧が読むサマリ）の復号・再生成

構造（実 NEO 4 件で確認）:
  raw[0:9]   'NEO300101'
  raw[9:424] 2 つのビット列ブロックを XOR 0xFF した上でビットシフトして格納
    ブロック A（raw[9:~320]）: decodeA = ((raw ^ FF) as big-int) << 3
      A[0:7]     固定 'f7 ff fc c7 ff ff f8'
      A[74:114]  協定工場名（Insurance.ConsultantFactory、cp932 40B）
      A[114:144] 顧客名 Name1（30B）
      A[144:206] 車名 CarNameByUser（62B）
      A[206:210] 作成日 (u16 year, u8 month, u8 day)
      A[210:250] double×5 = 部品計, 工賃計, 塗装計(材料込), 費用計(部品+工賃), 合計(税込)
      A[274:282] 登録番号 陸運支局(8B) A[282:288] 分類番号(6B) A[288:292] かな(4B) A[292:302] 一連番号(10B)
    ブロック B（raw[~320:424]）: decodeB = ((raw ^ FF) as big-int) << 7
      B[..] 'ea 07 09 04' 保存日 (u16 year, u8 month, u8 day) + 保存時刻 (u8 h, u8 m, u8 s) + ライセンスID 8B 'G0141016'
            + double×6 = -1.0 + 0 埋め
    ブロック境界は raw[319] 付近（ブロック A の登録番号の後ろは 0 埋めなので、A 側は raw[9:315]、B 側は raw[315:424] を書き換え対象にする）
"""
from __future__ import annotations

import datetime
import os
import struct
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import neo_container as _nc  # cp932w コーデック登録
from typing import Optional

MAGIC = b'NEO300101'
A_SHIFT = 3
B_SHIFT = 7
A_END = 315      # raw オフセット。ブロック A のフィールドはすべてこれより前で終わる
B_START = 320    # raw オフセット。ブロック B（保存日時・ライセンス）はこれより後ろ
B_DATE_OFF = 330 # raw 330 から shift 7 で復号すると先頭が保存日 'ea 07 mm dd'


def _dec(raw: bytes, shift: int) -> bytes:
    x = bytes(v ^ 0xFF for v in raw)
    n = len(x) * 8
    v = (int.from_bytes(x, 'big') << shift) & ((1 << n) - 1)
    return v.to_bytes(len(x), 'big')


def _enc(dec: bytes, shift: int, orig_raw: bytes) -> bytes:
    """復号バイト列 → raw。左シフトで失われた上位 shift ビットは元 raw の値を保つ"""
    n = len(dec) * 8
    v = int.from_bytes(dec, 'big') >> shift
    top = (int.from_bytes(bytes(b ^ 0xFF for b in orig_raw), 'big') >> (n - shift)) << (n - shift)
    x = (v | top).to_bytes(len(dec), 'big')
    return bytes(b ^ 0xFF for b in x)


def decode(raw: bytes) -> dict:
    h = raw[:424]
    a = _dec(h[9:424], A_SHIFT)
    b = _dec(h[B_DATE_OFF:424], B_SHIFT)
    def s(bs):
        return bs.split(b'\0')[0].decode('cp932', 'replace')
    out = {
        'agreed': s(a[74:114]), 'name1': s(a[114:144]), 'car_name': s(a[144:206]),
        'created': struct.unpack('<HBB', a[206:210]),
        'totals': [struct.unpack('<d', a[210 + 8 * j:218 + 8 * j])[0] for j in range(5)],
        'carno': (s(a[274:282]), s(a[282:288]), s(a[288:292]), s(a[292:302])),
        'saved': struct.unpack('<HBB', b[0:4]) + struct.unpack('<BBB', b[4:7]),
        'license': b[7:15].decode('latin1'),
    }
    return out


def _fit(sv: str, n: int) -> bytes:
    bb = (sv or '').encode('cp932w', 'replace')[:n]
    # 文字の途中で切らない
    try:
        bb.decode('cp932')
    except UnicodeDecodeError:
        bb = bb[:-1]
    return bb.ljust(n, b'\0')


def build(template_raw: bytes, *, agreed: str = '', name1: str = '', car_name: str = '', created: Optional[datetime.date] = None,
          totals: Optional[list] = None, carno: Optional[tuple] = None, saved: Optional[datetime.datetime] = None,
          license_id: Optional[str] = None) -> bytes:
    """テンプレート NEO の管理領域をもとに、サマリ項目を差し替えた 424B を返す"""
    h = bytearray(template_raw[:424])
    # --- ブロック A
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
        a[274:282] = _fit(dep, 8); a[282:288] = _fit(div, 6); a[288:292] = _fit(biz, 4); a[292:302] = _fit(ser, 10)
    enc_a = _enc(bytes(a), A_SHIFT, bytes(h[9:424]))
    h[9:A_END] = enc_a[:A_END - 9]
    # --- ブロック B
    b = bytearray(_dec(bytes(h[B_DATE_OFF:424]), B_SHIFT))
    if saved:
        b[0:4] = struct.pack('<HBB', saved.year, saved.month, saved.day)
        b[4:7] = struct.pack('<BBB', saved.hour, saved.minute, saved.second)
    if license_id:
        b[7:15] = license_id.encode('latin1')[:8].ljust(8, b'\0')
    enc_b = _enc(bytes(b), B_SHIFT, bytes(h[B_DATE_OFF:424]))
    h[B_DATE_OFF:424] = enc_b
    return bytes(h)


def apply(neo: bytes, **fields) -> bytes:
    """生成済み NEO の管理領域だけ差し替える"""
    return build(neo, **fields) + neo[424:]


if __name__ == '__main__':
    import sys, json
    for p in sys.argv[1:]:
        raw = open(p, 'rb').read()
        d = decode(raw)
        print(p, json.dumps(d, ensure_ascii=False, default=str))
        # 往復テスト: 同じ値で build すると raw と一致するか
        rt = build(raw, agreed=d['agreed'], name1=d['name1'], car_name=d['car_name'],
                   created=datetime.date(*d['created']), totals=d['totals'], carno=d['carno'],
                   saved=datetime.datetime(*d['saved']), license_id=d['license'])
        print('  roundtrip identical:', rt == raw[:424], 'first diff at', next((i for i in range(424) if rt[i] != raw[i]), None))
