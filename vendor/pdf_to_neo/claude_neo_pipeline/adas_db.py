# -*- coding: utf-8 -*-
"""
コグニセブン ADDATA の ADAS（運転支援システム再設定・調整）指数テーブル
  <車種>55.DB … 基本作業指数        (ItemNo A010 のみ。DLL: TRC_AnAdasBaseWorkList)
  <車種>56.DB … センサ別再設定・調整作業指数 (ItemNo A100〜A140。DLL: TRC_AnAdasSensorWorkList)
の読み取りパーサ。ADDATA 本体は一切書き換えない（読み取り専用）。

復号: 全バイト XOR 0xFF → cp932。1 行 = 1 レコード、カンマ区切り・固定幅（空白詰め）。
列は DLL（AxDBAcsCarDat.dll / AxDBAcs.bpl の TRC_XXX55 / TRC_XXX56 RTTI）の順に対応するが、
55.DB だけは 13〜15 列目が「OrderNo(2桁), Time(4桁), Provisional(1桁)」の順で格納されている
（56.DB は RTTI 通り「Time, Provisional, OrderNo」）。全 81 車種で列数 18 / 28 固定を確認済み。

使い方:
    from adas_db import AdasDB
    db = AdasDB()                       # ADDATA ルート既定 C:\\Addata
    base   = db.load_55('D18')          # list[dict]
    sensor = db.load_56('D18')          # list[dict]  (BasePartsCodes を list で持つ)
    for r in db.work_rows('D18'):       # 55+56 を統合、注記行(Note 行)を除いた「指数行」のみ
        print(r['ItemNo'], r['ItemNoSub'], r['ItemName'], r['TimeHours'])
    notes = db.notes('D18')             # ItemNo ごとの注記(Note)一覧
    for car in db.cars(): ...           # 55.DB を持つ全車種コード
"""
from __future__ import annotations

import glob
import os
from typing import Iterator, Optional

def _default_root() -> str:
    """ADDATA の場所。PC ごとに違うので addata_vehicle_resolver の解決（環境変数 → 設定ファイル → 既定 → 自動検出）に任せる。
    パッケージとして読まれた場合（from claude_neo_pipeline.adas_db import …）と、
    claude_neo_pipeline を sys.path に足した場合（run_case.py）のどちらでも動くように 3 通りで探す"""
    try:
        from .addata_vehicle_resolver import find_addata_root  # パッケージ内の相対 import
    except ImportError:
        try:
            from addata_vehicle_resolver import find_addata_root  # sys.path に claude_neo_pipeline がある
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from addata_vehicle_resolver import find_addata_root
    return find_addata_root()

# 列定義（55.DB / 56.DB）。先頭列 YearCode は 1 桁（全件空白）。
COLS_55 = [
    'YearCode',            # 0  1桁  常に空白
    'BodyCode',            # 1  2桁  車種内の body 系 ('00' 基本。'10','20','30','40' は KATB021 の body 系と同じ)
    'GradeCode',           # 2  5桁  常に空白
    'FEVACode',            # 3  2桁  ほぼ空白（56.DB に 'B' が 2 行だけ）
    'PartsCode',           # 4  4桁  作業コード 99xx（ERParts/ReserveERParts.PartsCode に入る）
    'ItemNoSubCombiCode',  # 5  3桁  構成(組合せ)コード 大文字 例 AAA/A00/A12/B13
    'ItemNoCombiCode',     # 6  3桁  構成(組合せ)コード 小文字 例 aaa/a00/a12/a13
    'ExtraFlag',           # 7  1桁  '1' = 割増項目
    'ExtraDivision',       # 8  1桁  割増区分 A/B…（同一区分の割増は重複不可 = ExtraDivisionDuplicateCheck）
    'ItemNo',              # 9  4桁  A010 / A100 / A110 / A120 / A130 / A135 / A140
    'ItemNoSub',           # 10 4桁  '1','2',… または '割増'（注記行は空）
    'ItemName',            # 11 200桁 作業名称
    'CommentScanTool',     # 12 100桁 条件コメント（DS-Ⅲ使用 / ﾚｰｻﾞ墨出し器使用 など）
    'OrderNo',             # 13 2桁  表示順（55.DB のみこの位置）
    'Time',                # 14 4桁  指数×100（'20' = 0.20h）
    'Provisional',         # 15 1桁  '$' = 暫定指数
    'Note',                # 16 200桁 注記（注記行のみ）
    'NoteSub',             # 17 2桁  注記の連番
]
COLS_56 = [
    'YearCode', 'BodyCode', 'GradeCode', 'FEVACode', 'PartsCode',
    'ItemNoSubCombiCode', 'ItemNoCombiCode', 'ExtraFlag', 'ExtraDivision',
    'ItemNo', 'ItemNoSub', 'ItemName',
    'CommentTool',         # 12 100桁
    'Time',                # 13 4桁
    'Provisional',         # 14 1桁
    'OrderNo',             # 15 2桁
    'Note',                # 16
    'NoteSub',             # 17
] + [f'BasePartsCode{i}' for i in range(1, 11)]   # 18..27 前提作業の PartsCode（55.DB の 9900/9902/9904/9906/9901）

ITEMNO_NAMES = {
    'A010': '運転支援システム再設定・調整基本作業（基本作業指数）',
    'A100': 'センサ別再設定・調整作業指数（周囲カメラ／モニタ系）',
    'A110': 'センサ別再設定・調整作業指数（ソナー・パーキング系）',
    'A120': 'センサ別再設定・調整作業指数（前方カメラ／ステレオカメラ系）',
    'A130': 'センサ別再設定・調整作業指数（前方ミリ波レーダ系）',
    'A135': 'センサ別再設定・調整作業指数（前側方レーダ系）',
    'A140': 'センサ別再設定・調整作業指数（後側方レーダ系）',
}


def decode_db(path: str) -> str:
    with open(path, 'rb') as f:
        raw = f.read()
    return bytes(x ^ 0xFF for x in raw).decode('cp932', errors='replace')


def _split(line: str, cols: list[str]) -> Optional[dict]:
    fields = line.split(',')
    if len(fields) != len(cols):
        return None
    return {c: v.strip() for c, v in zip(cols, fields)}


def _time_hours(v: str) -> Optional[float]:
    v = v.strip()
    if not v:
        return None
    try:
        return int(v) / 100.0
    except ValueError:
        return None


class AdasDB:
    def __init__(self, root: Optional[str] = None):
        self.root = root or _default_root()

    # ------------------------------------------------------------ paths
    def path(self, car: str, table: str) -> str:
        return os.path.join(self.root, car[0], car, f'{car}{table}.DB')

    def cars(self, table: str = '55') -> list[str]:
        """指定テーブル(55/56)を持つ車種コード一覧"""
        return sorted(os.path.basename(p)[:3] for p in glob.glob(os.path.join(self.root, '*', '*', f'*{table}.DB')))

    # ------------------------------------------------------------ raw load
    def _load(self, car: str, table: str, cols: list[str]) -> list[dict]:
        p = self.path(car, table)
        if not os.path.exists(p):
            return []
        out = []
        for i, line in enumerate(decode_db(p).splitlines()):
            if not line.strip():
                continue
            rec = _split(line, cols)
            if rec is None:
                raise ValueError(f'{p}: line {i + 1} has {len(line.split(","))} fields, expected {len(cols)}')
            rec['_car'] = car
            rec['_table'] = table
            rec['_line'] = i + 1
            rec['IsNote'] = not rec['PartsCode'] and bool(rec['Note'])
            rec['IsExtra'] = rec['ExtraFlag'] == '1'
            rec['TimeHours'] = _time_hours(rec['Time'])
            if table == '56':
                rec['BasePartsCodes'] = [rec[f'BasePartsCode{k}'] for k in range(1, 11) if rec[f'BasePartsCode{k}']]
            else:
                rec['BasePartsCodes'] = []
            out.append(rec)
        return out

    def load_55(self, car: str) -> list[dict]:
        """基本作業指数（A010）。注記行(IsNote=True)も含む"""
        return self._load(car, '55', COLS_55)

    def load_56(self, car: str) -> list[dict]:
        """センサ別再設定・調整作業指数（A100〜）。注記行も含む"""
        return self._load(car, '56', COLS_56)

    # ------------------------------------------------------------ views
    def work_rows(self, car: str, body_code: Optional[str] = None) -> list[dict]:
        """指数行（PartsCode を持つ行）だけを 55→56 の順で返す。body_code 指定で body 系を絞る"""
        rows = [r for r in self.load_55(car) + self.load_56(car) if not r['IsNote'] and r['PartsCode']]
        if body_code is not None:
            rows = [r for r in rows if r['BodyCode'] == body_code]
        return rows

    def notes(self, car: str) -> dict[str, list[str]]:
        """ItemNo → 注記一覧"""
        d: dict[str, list[str]] = {}
        for r in self.load_55(car) + self.load_56(car):
            if r['IsNote']:
                d.setdefault(r['ItemNo'], []).append(r['Note'])
        return d

    def by_parts_code(self, car: str) -> dict[str, dict]:
        """PartsCode(99xx) → 指数行。前提作業(BasePartsCodes)の解決に使う"""
        return {r['PartsCode']: r for r in self.work_rows(car)}

    def resolve_base(self, car: str, row: dict) -> list[dict]:
        """56.DB 行の前提作業 (BasePartsCode1..10) を 55.DB の行に解決"""
        idx = self.by_parts_code(car)
        return [idx[c] for c in row.get('BasePartsCodes', []) if c in idx]

    def iter_all(self, table: str = '56') -> Iterator[dict]:
        for car in self.cars(table):
            yield from (self.load_56(car) if table == '56' else self.load_55(car))

    # ------------------------------------------------------------ 24.DB（参考: 構成コードの同形式テーブル。ADAS 専用ではない）
    def load_24(self, car: str) -> list[dict]:
        """<車種>24.DB: CarCode,Generation4Code,PartsCode,DisposalCode,CarYearCode,CarTypeCode,GradeCode,FVACode,ConstructCode
        ヘッドランプ等の Assy 構成（AAA=Assy, A00=ユニット, A12.. 構成部品）を表す部品側のテーブル。
        全 ADDATA 160 車種にあり ADAS 車種 81 のうち 39 のみ存在 → 56.DB の組合せコードとは別物（形式のみ同じ）"""
        p = self.path(car, '24')
        if not os.path.exists(p):
            return []
        cols = ['CarCode', 'Generation4Code', 'PartsCode', 'DisposalCode', 'CarYearCode', 'CarTypeCode', 'GradeCode', 'FVACode', 'ConstructCode']
        out = []
        for line in decode_db(p).splitlines():
            if line.strip():
                rec = _split(line, cols)
                if rec:
                    out.append(rec)
        return out

    # ------------------------------------------------------------ 車名（COM/KATB021.DB: CarCode, body系, 車名コード, 車名）
    def car_names(self) -> dict[str, list[tuple[str, str, str]]]:
        p = os.path.join(self.root, 'COM', 'KATB021.DB')
        d: dict[str, list] = {}
        if not os.path.exists(p):
            return d
        for l in decode_db(p).splitlines():
            if len(l) >= 13 and l[:3].strip():
                d.setdefault(l[:3], []).append((l[5:7], l[10:13], l[13:].strip()))
        return d

    def car_name(self, car: str, body_code: str = '00') -> str:
        lst = self.car_names().get(car, [])
        for body, _code, name in lst:
            if body == body_code:
                return name
        return lst[0][2] if lst else ''


if __name__ == '__main__':
    import sys
    db = AdasDB()
    car = sys.argv[1] if len(sys.argv) > 1 else 'D18'
    print(car, db.car_name(car))
    for r in db.work_rows(car):
        base = ','.join(r['BasePartsCodes'])
        print(f"{r['_table']} {r['BodyCode']} {r['PartsCode']} {r['ItemNo']}-{r['ItemNoSub']:<3} "
              f"[{r['ItemNoSubCombiCode']:3}/{r['ItemNoCombiCode']:3}] EF={r['ExtraFlag']:1} ED={r['ExtraDivision']:1} "
              f"T={r['TimeHours']} P={r['Provisional']:1} base={base:<15} {r['ItemName']} | {r.get('CommentScanTool') or r.get('CommentTool')}")
    for k, v in db.notes(car).items():
        for n in v:
            print('  NOTE', k, n)
