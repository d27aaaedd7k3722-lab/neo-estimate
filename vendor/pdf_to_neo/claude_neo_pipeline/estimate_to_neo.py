# -*- coding: utf-8 -*-
"""
工場見積（構造化データ）＋ 車検証 → コグニセブン同等 NEO 生成
=================================================================
1. 車両特定: addata_vehicle_resolver（型式＋車台番号→KA06、型式指定＋類別→KA81、01/05/07/08/09/10/26.DB）
2. 部品照合: ADDATA <車種>11.DB（品番・価格・装備フラグ）/ 12.DB（名称）/ 15.DB（作業指数）/ 17.DB（部位ブロック）
3. 装備推定: 見積の品番が一致した 11.DB 変種の装備フラグ（U/V/Z…）→ CarEVA
4. NEO 生成: 実 NEO（コグニセブン作成）をテンプレートに、AnSvEm（明細・塗装・費用・合計）/ AnSvIf（車両・顧客）/ AnSMB / XML を書換

実装の根拠: claude_neo_pipeline/reference/neo_04011103_reference.json（実 NEO の全テーブル実値）
DisposalCode は AnDefine.ini [WorkSheet] の真値: 0=取替 1=脱着 2=修理 3=脱着修理/脱着板金 4=点検/調整/点検調整 5=分解調整 6=板金
"""
from __future__ import annotations
import os, re, io, json, struct, sqlite3, tempfile, datetime, unicodedata, sys, math
from collections import Counter
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE); sys.path.insert(0, ROOT)
import neo_container as nc
from addata_vehicle_resolver import AddataVehicleResolver, to_halfwidth, parse_reg_date, _xor_text
from _addata_db_search import AddataSearchEngine
from paint_index import PaintIndex, PAINT_CODE, COAT_CODE, HF_CODE, HF_NAME, floor10
import com_tables  # noqa: E402  ADDATA の COM.CAB を版ごとに展開して読む（DATAUP・Katashiki は毎月変わる）
import neo_header as nh
LICENSE_ID = os.environ.get('COGNI_LICENSE_ID', 'G0141016')  # 管理領域に書くライセンス ID（このPCのコグニ）

TAX = 0.10
DISPOSAL = {  # AnDefine.ini [WorkSheet] Repair0-9 の真値
    '取替': 0, '交換': 0, '取換': 0, '部品': 0,
    '脱着': 1, '取外': 1, '取付': 1, '脱付': 1,
    '修理': 2, '補修': 2, '修正': 2, '鈑金修正': 2,
    '脱着修理': 3, '脱着板金': 3, '脱着鈑金': 3,
    '点検': 4, '調整': 4, '点検調整': 4, '診断': 4,
    '分解調整': 5, '分解': 5, 'オーバーホール': 5, 'O/H': 5, 'OH': 5,
    '板金': 6, '鈑金': 6,
}
DISPOSAL_NAME = {0: '取替', 1: '脱着', 2: '修理', 3: '脱着修理', 4: '点検調整', 5: '分解調整', 6: '板金'}  # 4 はコグニ保存版で '点検調整'（NONE_dc.neo 2026-09-05）


# 登録番号「地名 分類番号 かな 一連番号」の分割。分類番号は 2〜3 桁のほか、英字入り（30A・3ZX。2018 年〜の希望番号）と
# 旧式の 1 桁（'5'）も受ける（実機 NEO の CarRegNoDivision は半角数字 + 英字。アプリ側 doc_hints._REG_SPLIT と同じ。2026-09-15）
# 一連番号はプレートの表記（「10-31」「・・12」「・123」）でも受け、split_reg_no が数字だけにする（2026-09-15 アプリのバグハント:
# ハイフン区切りの見積だと登録番号 4 欄が全部空のまま合格していた）
REG_NO_RE = re.compile(r'^\s*(\S+?)\s*(\d[0-9A-Z]{0,2})\s*([ぁ-んア-ン])\s*[\-‐]?\s*([・･.．\s]*\d[\d\s\-‐－]*)\s*$')


def split_reg_no(text) -> Optional[tuple]:
    """登録番号「地名 分類番号 かな 一連番号」を (地名, 分類番号, かな, 一連番号) に分ける。一連番号は数字だけ（1〜4 桁）。分けられなければ None"""
    m = REG_NO_RE.match(unicodedata.normalize('NFKC', str(text or '')))
    if not m:
        return None
    ser = re.sub(r'\D', '', m.group(4))
    if not 1 <= len(ser) <= 4:
        return None
    return (m.group(1), m.group(2), m.group(3), ser)


def _date8_str(v) -> str:
    """YYYYMMDD の 8 桁（区切り付きでも数字が 8 桁なら受ける）。それ以外・番兵 '00000000' は ''"""
    d = re.sub(r'\D', '', str(v or ''))
    return d if len(d) == 8 and d != '00000000' else ''


def split_address_text(addr: str) -> tuple:
    """1 本の住所文字列を コグニと同じ 都道府県 / 市区郡 / 以降 に分ける（AddressOther1 は 30 バイト）。
    市区郡は最短一致なので「四日市市」「蒲郡市」のような名前は切り違える — 車検証 OCR など構造化された住所は
    customer.prefecture / municipality / address_other で渡せば、この分割を使わない（write_ansvem）"""
    addr = unicodedata.normalize('NFKC', addr or '')
    m_ = re.match(r'^(.{2,3}?[都道府県])(.*)$', addr); pref, rest = (m_.group(1), m_.group(2)) if m_ else ('', addr)
    m_ = re.match(r'^((?:.{1,8}?(?:市|区|郡|町|村))+?)(.*)$', rest)
    muni, other = (m_.group(1), m_.group(2)) if m_ else ('', rest)
    if len(muni.encode('cp932w', 'replace')) > 30 or not other:
        muni, other = '', rest
    return pref, muni, other




def r10(x: float) -> int:
    """工賃の丸め（Setting.wb_Round の単位で四捨五入。既定 10 円。塗装・内板骨格・付加塗装の工賃に使う）
    1.9×8750=16,625 → 16,630（Python の round は偶数丸めなので使わない）。wage_round=100 のときは塗装側も 100 円丸め（コグニの wb_Round は工賃全体の設定。
    100 円設定の工場で塗装明細を持つ実例は未取得なので、明細側と同じ扱いにしている）"""
    u = wage_unit()
    return int((x + u / 2.0) // u) * u


import contextvars

# 工賃の丸め単位（Setting.wb_Round / wi_Round = 1 / 10 / 100、既定 10）。build() が estimate['wage_round'] で build の間だけ設定し、終了時に戻す。
# ContextVar なので同一プロセス内の別スレッド（Streamlit 等）の build と混ざらない。
# 工場のコグニ設定が 100 円のときは 0.25h×11,000 = 2,750 → 2,800 と印字される（工場 J 2026-09-07 オデッセイ）
_WAGE_UNIT: contextvars.ContextVar = contextvars.ContextVar('neo_wage_unit', default=10)


def wage_unit() -> int:
    return int(_WAGE_UNIT.get())


def set_wage_unit(unit):
    """工賃の丸め単位を現在のコンテキストに設定し、_WAGE_UNIT.reset() 用のトークンを返す（1 / 10 / 100 以外は ValueError）"""
    u = int(unit or 10)
    if u not in (1, 10, 100):
        raise ValueError(f'wage_round は 1 / 10 / 100 のいずれか（{unit!r}）')
    return _WAGE_UNIT.set(u)


def reset_wage_unit(token) -> None:
    _WAGE_UNIT.reset(token)


def r10_even(x: float) -> int:
    """ERParts の標準工賃: 指数×レバーレートを WAGE_UNIT（既定 10 円）単位で**四捨五入**（本 PC のコグニ 2.1.1.3 実機: 工賃単価を 8,750 に変えて再計算した ROUND_8750.neo で
    0.3h → 2,625 → 2,630、1.9h → 16,625 → 16,630、0.5h×8,610 → 4,310）。工場のコグニ生成 NEO（C-HR、2,620/27,120）は旧バージョンの偶数丸めなので、
    工場見積の工賃がこれと違うときは WageByManual '*' になる。関数名は互換のため据え置き"""
    x = round(float(x), 2)  # 2.3×6950 = 15984.999… の浮動小数ノイズを除く（コグニは 230×6950/100 = 15,985 → 15,990）
    u = wage_unit()
    return int(math.floor(x / float(u) + 0.5)) * u


def tax_of(out: int) -> tuple[int, int]:
    """(税込, 税) 10% 四捨五入（コグニ生成 NEO の末尾 5 円の行 89 件: PartsPrice 245 → 25、PartsPriceStandard 195 → 20、ChangeTotal 295 → 30。
    例外は PartsUnitPriceTax（切捨 155 → 15）と数量行の PartsPriceTax（四捨五入(単価×0.1)×数量）。旧生成器の切捨行（PartsCode '' の 195 → 19）はコグニに温存される。総合計の消費税も四捨五入 §6）"""
    t = int(math.floor(out * TAX + 0.5))
    return out + t, t


def hw(s: str) -> str:
    """NEO の名称欄に書く形（半角カナ・半角英数）。長音は 'ｰ'（to_halfwidth が 'ー' を潰すのでハイフンにはならない。
    ADDATA 由来の名称は '-' を使うので同じ NEO に両方が出るが、金額にも照合にも効かない）"""
    return to_halfwidth(unicodedata.normalize('NFKC', s or ''))


def _xor_lines_ref(name: str) -> list:
    p = os.path.join(HERE, 'reference', name)
    if not os.path.exists(p):
        return []
    return [l for l in bytes(x ^ 0xFF for x in open(p, 'rb').read()).decode('cp932', 'replace').splitlines() if l.strip()]


def _fit(s: str, n: int) -> str:
    """TEXT(n) 列は cp932 で n バイト（コグニ保存時の切詰めと同じ）。文字の途中では切らない"""
    b = (s or '').encode('cp932w', 'replace')
    if len(b) <= n:
        return s or ''
    try:
        return b[:n].decode('cp932')
    except UnicodeDecodeError:
        return b[:n - 1].decode('cp932', 'ignore')


def _guideline_path() -> str:
    """SHOUCHIKU 見積ガイドライン（社内の参考値。git・配布 zip の外）: 環境変数 PDF_TO_NEO_GUIDELINE → <NEO_CHECK_ROOT>/_reference/shouchiku_guideline.json"""
    p = os.environ.get('PDF_TO_NEO_GUIDELINE') or ''
    if p:
        return p
    root = os.environ.get('NEO_CHECK_ROOT') or ''
    if not root:
        try:
            with open(os.path.join(os.path.expanduser('~'), '.claude', 'pdf-to-neo.local.json'), encoding='utf-8-sig') as fh:
                root = str((json.load(fh) or {}).get('NEO_CHECK_ROOT') or '')
        except (OSError, ValueError):
            root = ''
    root = root or os.path.join(os.path.expanduser('~'), 'Documents', 'NEO_check')
    return os.path.join(root, '_reference', 'shouchiku_guideline.json')


def guideline_material_rate(paint: int, coat: int, hf: int) -> Optional[float]:
    """ガイドラインの塗装材料代割合表（塗料 × クリヤー × 塗膜 × 対応単価）の既定列（default_band、既定 6500〜）の値。
    実案件 NEO 4,933 本（Z:\ドキュメント、2025-12〜2026-09）の 83% がこの列の値だった（工賃単価によらず 6500〜 列。表のオレンジ枠）。
    表が無い・該当しない組合せ（速乾・フッ素）は None"""
    p = _guideline_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding='utf-8-sig') as fh:
            g = json.load(fh).get('material_rate') or {}
        bands = [int(x) for x in g.get('bands') or []]
        band = int(g.get('default_band') or 6500)
        idx = max(i for i, b in enumerate(bands) if b <= band)
        pk = {3: '2K', 4: '水性'}.get(int(paint)); hk = {0: '標準', 2: '耐擦傷性', 3: 'スクラッチシールド'}.get(int(hf))
        ck = {1: 'ソリッド', 2: 'メタリック', 3: '2P', 4: '3P'}.get(int(coat))
        v = ((g.get(pk) or {}).get(hk) or {}).get(ck) if (pk and hk and ck) else None
        val = float(v[idx]) if isinstance(v, list) and len(v) > idx else None
        return val if (val is not None and 0 < val <= 100) else None  # 表の値が壊れていたら使わない（コグニ既定へ。Codex 指摘）
    except Exception:  # noqa: BLE001  表の形が違う → コグニ既定へ
        return None


def is_manual_panel(pnl: dict) -> bool:
    """塗装パネルの「手入力の塗装行」（部品コードの無い行。コグニの外板パネル画面で「行追加」して名称・指数を手で入れた行 = PaintingPanel.DisposalCode 9）。
    `manual: true` か、部品コード（code）が空の行"""
    return bool(_flag(pnl.get('manual'), 'paint.panels[].manual')) or not str(pnl.get('code') or '').strip()


def default_material_rate(paint: int, coat: int, hf: int) -> Optional[float]:
    """材料代割合の既定値（見積書に材料代も割合も無いとき）。
    1) SHOUCHIKU ガイドライン表の 6500〜 列（guideline_material_rate。この PC に表があるとき）
    2) コグニ環境 DB AudaData/AnUsrTblPnt.sld MaterialRate(Paint 0 速乾/2 ２Ｋ/3 水性 = PaintingPlan.Paint−1, Coat = PaintingPlan.Coat−1, HFPainting 0-3) / 100
       （N-ONE 実験 2026-09-04: ２Ｋ×２コートパール で しない 15% / フッ素 25% / 耐スリ傷 18%。コグニで何も変えないときの値）"""
    g = guideline_material_rate(paint, coat, hf)
    if g is not None:
        return g
    return cogni_default_material_rate(paint, coat, hf)


def cogni_default_material_rate(paint: int, coat: int, hf: int) -> Optional[float]:
    """コグニで何も変えないときの材料代割合（この PC のコグニ設定の写し reference/AnUsrTblPnt.sld）"""
    p = os.path.join(HERE, 'reference', 'AnUsrTblPnt.sld')
    if not os.path.exists(p):
        return None
    try:
        c = sqlite3.connect(p)
        r = c.execute('SELECT MaterialRate FROM MaterialRate WHERE Paint=? AND Coat=? AND HFPainting=?', (int(paint) - 1, int(coat) - 1, int(hf))).fetchone()
        c.close()
        return (r[0] / 100.0) if r and r[0] else None
    except Exception:
        return None


def bankin_time(area: int, rank: str) -> Optional[float]:
    """COM/BANKIN.DB: 損傷面積 1〜40 dm² × ランク A/B/C → 指数（例 10 dm²: A 1.3 / B 1.9 / C 2.5。コグニ板金ダイアログと一致）"""
    for l in _xor_lines_ref('BANKIN.DB'):
        f = [x.strip() for x in l.split(',')]
        if len(f) >= 4 and f[0].isdigit() and int(f[0]) == int(area):
            return int(f[{'A': 1, 'B': 2, 'C': 3}[rank]]) / 100.0
    return None


def bankin_fuka(parts_code: str) -> list[tuple[str, float]]:
    """COM/BAN_FUKA.DB: 部品コード → 板金付加作業（作業名, 時間）。時間 0 の '*' 付き作業は手加算用の案内"""
    out = []
    for l in _xor_lines_ref('BAN_FUKA.DB'):
        f = l.split(',')
        if len(f) >= 4 and f[1].strip() == parts_code:
            out.append((f[3].strip(), int(f[2].strip() or 0) / 100.0))
    return out


BUMPER_DISPOSAL = {'新品': (1, '新品'), '取替': (1, '新品'), '外傷修正': (3, '外傷修正'), '変形修正': (2, '変形修正'), '変形': (2, '変形修正'),
                   '外傷修正小': (4, '外傷修正小'), '外傷小': (4, '外傷修正小'), '外傷修正大': (5, '外傷修正大'), '外傷大': (5, '外傷修正大')}
BUMPER_DRAFT_ADD = 0.4  # 絞模様有り の加算（コグニ実機 N-ONE 2026-09-05: 修理 3 方式・塗膜によらず +0.4）
COAT_CODES = {'ソリッド': 1, 'メタリック': 2, '2コートパール': 3, '3コートパール': 4}  # NFKC 正規化後の照合用
COAT_DISPLAY = ['', 'ソリッド', 'メタリック', '２コートパール', '３コートパール']  # NEO に書く表記（コグニ CoatName と同じ全角数字）
BUMPER_ONLY_KEYS = ('paint', 'coat', 'hf', 'panels', 'bumper_front', 'bumper_rear', 'bumper_base', 'material', 'material_rate', 'material_round',
                    'input_type', 'actual', 'total', 'note', 'auto_panels', '_note')  # パネル無しでバンパだけ塗る見積に許す paint のキー（許可リスト。sealing / frame / other / 付加塗装が混じる組合せは実機未確認なので通さない）
PAINT_DETAIL_KEYS = ('bumper_front', 'bumper_rear', 'wax', 'door_sash', 'stripe', 'low_cover', 'two_coat_solid', 'two_tone')  # パネル別指数（paint.panels）のときだけ書ける項目。frame / sealing / other は一括計上でも可


def is_bumper_only_paint(p: dict) -> bool:
    """外板パネルが無くバンパだけ塗る詳細塗装（panels: [] + bumper_*、キーは許可リストだけ）か。NeoBuilder の判定と同じ（検算側が同じ関数を使う）"""
    return (isinstance(p.get('panels'), list) and not p.get('panels') and any(p.get(k) for k in ('bumper_front', 'bumper_rear'))
            and all(k in BUMPER_ONLY_KEYS or str(k).startswith('_') for k in p))


def _code4(v) -> str:
    """部品コードを 4 桁の文字列にする。10 / '10' / '0010' / 10.0 のどれで書かれても同じに扱う"""
    if v is None or v == '':
        return ''
    if isinstance(v, bool):
        raise ValueError(f'部品コードが真偽値になっている（{v}）')
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError(f'部品コードが整数でない（{v}）')
        v = int(v)
    if isinstance(v, int):
        if not 0 <= v <= 9999:  # 負数や 5 桁以上は下流で別のコードに化ける
            raise ValueError(f'部品コードが 0〜9999 の範囲外（{v}）')
        return f'{v:04d}'
    t = unicodedata.normalize('NFKC', str(v)).strip()
    if t == '':  # 空白だけの欄は「指定なし」
        return ''
    if t.isdigit():
        if not 0 <= int(t) <= 9999:  # '10000' のような 5 桁も範囲外（数値で渡したときと同じ扱い）
            raise ValueError(f'部品コードが 0〜9999 の範囲外（{v!r}）')
        return f'{int(t):04d}'  # '00010' のような先頭ゼロ付きも 4 桁に正規化（呼び出し側は 4 桁前提）
    # 数字にならないコード（'10.5' / '12OOO' / '-1' / '12,9'）は、下流が非数字を落として別の部品コードに化けるので止める
    raise ValueError(f'部品コードが 4 桁の数字でない（{v!r}）。写し間違いか、code ではなく品番・名称を入れている')


def _money(v, name: str) -> int:
    """金額を整数にする。'1,000' や '¥1,000' も受ける（estimate.json を手書きしたとき用）。数値にできなければどの項目かを示して止める"""
    if v is None or v == '':
        return 0
    if isinstance(v, bool):
        raise ValueError(f'{name}: 真偽値ではなく金額を入れる（{v}）')
    if isinstance(v, float) and not v.is_integer():
        raise ValueError(f'{name}: 円未満の端数がある（{v}）。見積書の印字どおりの整数にする')
    if isinstance(v, (int, float)):
        return int(v)
    t = unicodedata.normalize('NFKC', str(v)).replace(',', '').replace('¥', '').replace('円', '').strip()
    if re.fullmatch(r'[+-]?\d+(\.0*)?', t):  # '2' / '2.00' は受ける。'12.9' や '1e3' は写し間違いとして止める
        return int(float(t))
    raise ValueError(f'{name}: 整数にならない（{v!r}）。カンマ・通貨記号は付いていてもよいが、小数や指数表記は写し間違い')


def _int_strict(v, name: str) -> int:
    """JSON の数値/数値文字列を整数として受ける。bool・小数・非数値は ValueError（枚数やコードを黙って丸めない）"""
    if isinstance(v, bool):
        raise ValueError(f'{name}: 真偽値ではなく整数で指定する（{v}）')
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != int(v):
            raise ValueError(f'{name}: 整数で指定する（{v}）')
        return int(v)
    t = unicodedata.normalize('NFKC', str(v)).strip()
    if not re.fullmatch(r'-?\d+', t):
        raise ValueError(f'{name}: 整数で指定する（{v!r}）')
    return int(t)


def _truthy(v) -> bool:
    """JSON/OCR 由来の真偽値を正規化（True/1/'有り'/'あり'/'true'/'yes' のみ真。'無し'/'false'/'0'/'' は偽）"""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    t = unicodedata.normalize('NFKC', str(v)).strip().lower()
    return t in ('1', 'true', 'yes', 'y', 'on', '有り', 'あり', '有')


def _blank_hint(v) -> bool:
    """ヒントが「書かれていない」か（None・空・空白だけ）。False にすると 2WD 指定になってしまうので、
    未指定はそのまま残す"""
    return v is None or (isinstance(v, str) and not v.strip())


def _norm_hint_flags(hints):
    """車種特定のヒントに入る真偽値欄（four_wd / hybrid）を厳密に読み直す。
    文字列 "false" が真になると 2WD の車を 4WD 側へ寄せてしまう"""
    if not hints:
        return hints
    out = dict(hints)
    # 空（None・空文字・空白だけ）は **キーごと落とす**。後段は「キーがある = 指定あり」と見るので、
    # 残すと 2WD 指定として採点に効いてしまう（Codex 指摘）
    for k in ('four_wd', 'hybrid'):
        if k in out:
            if _blank_hint(out[k]):
                out.pop(k)
            else:
                out[k] = _flag(out[k], f'hints.{k}')
    c = out.get('candidate')
    if isinstance(c, dict) and 'four_wd' in c:
        c = dict(c)
        if _blank_hint(c['four_wd']):
            c.pop('four_wd')
        else:
            c['four_wd'] = _flag(c['four_wd'], 'hints.candidate.four_wd')
        out['candidate'] = c
    return out


_VIN_RE = re.compile(r'[A-HJ-NPR-Z0-9]{17}')   # 国際 VIN（I・O・Q を使わない 17 桁）。国産車の車台番号は「型式-番号」で 17 桁の VIN にならない
# 車名欄に出ていれば輸入車とみなすメーカー名。国産車の車名と紛れる短い語（ミニ・MINI 等。ミニキャブと紛れる）は入れない
_IMPORT_BRANDS = ('ボルボ', 'VOLVO', 'BMW', 'ベンツ', 'メルセデス', 'MERCEDES', 'アウディ', 'AUDI', 'フォルクスワーゲン', 'VOLKSWAGEN',
                  'ポルシェ', 'PORSCHE', 'プジョー', 'PEUGEOT', 'ルノー', 'RENAULT', 'シトロエン', 'CITROEN', 'フィアット', 'FIAT',
                  'アルファロメオ', 'ジープ', 'JEEP', 'ランドローバー', 'ジャガー', 'JAGUAR', 'テスラ', 'TESLA')


def imported_signal(v: dict) -> str:
    """車両の読み取りに輸入車のしるしがあれば、その説明を返す（無ければ ''）。
    車種マスタに候補が無いときに汎用車種へ切り替えてよいかの判断にだけ使う（しるしの無い候補ゼロは止める）"""
    s = re.sub(r'[\s\-‐−]', '', unicodedata.normalize('NFKC', str(v.get('serial_no') or ''))).upper()
    if _VIN_RE.fullmatch(s):
        return f'車台番号が 17 桁の国際 VIN（{s[:3]}…）'
    txt = unicodedata.normalize('NFKC', ' '.join(str(v.get(k) or '') for k in ('car_name', 'maker', 'maker_name', 'name'))).upper()
    for b in _IMPORT_BRANDS:
        if unicodedata.normalize('NFKC', b).upper() in txt:
            return f'車名に輸入車のメーカー名（{b}）'
    return ''


def _flag(v, name: str, default: bool = False) -> bool:
    """人が書いた JSON の真偽値欄を厳密に読む。判断できない値は ValueError。
    `if d.get('generic'):` だと文字列 "false" が真になり、実在車種を汎用車種で作ってしまう"""
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


def _fukaetc_row(idx: int, name: str) -> list[float]:
    # 行番号に意味がある表なので空行を落とさずに読む（_xor_lines_ref は空行を除くため使わない）
    fp = os.path.join(HERE, 'reference', 'fukaetc.DB')
    if not os.path.exists(fp):
        raise ValueError(f'付加塗装 {name}: reference/fukaetc.DB が無い')
    raw = bytes(b ^ 0xFF for b in open(fp, 'rb').read()).decode('cp932', 'replace')
    lines = raw.replace('\r\n', '\n').split('\n')
    if len(lines) <= idx or not lines[idx].strip():
        raise ValueError(f'付加塗装 {name}: reference/fukaetc.DB の {idx + 1} 行目が無い')
    cells = [x.strip() for x in lines[idx].split(',')]
    if not cells or not all(c.isdigit() for c in cells):  # 位置で意味が決まる表なので空セル・非数値は詰めずに異常扱い
        raise ValueError(f'付加塗装 {name}: reference/fukaetc.DB の {idx + 1} 行目に空または非数値のセルがある: {lines[idx]!r}')
    return [int(c) / 100.0 for c in cells]


def fukaetc_time(kind: str, count: int, paint: int = 2) -> Optional[float]:
    """COM/fukaetc.DB（XOR 0xff CSV）: 2 行目 ドアサッシュ黒塗り [枚数1..4]、3 行目 ボデーストライプ [枚数1..4]（実機 0.4/0.6/0.7/0.9・0.5/0.5/0.6/0.7）。
    塗料が水性(4)のときドアサッシュ黒塗りは指数なし（コグニ実機 J52 2026-09-06 夕: 枚数を入れても指数・工賃が空欄）。ストライプ・防錆ワックスは塗料によらない"""
    idx = {'door_sash': 1, 'stripe': 2}.get(kind)
    if idx is None:
        return None
    vals = _fukaetc_row(idx, {'door_sash': 'ドアサッシュ黒塗り', 'stripe': 'ボデーストライプ'}[kind])  # 行欠落・空セルは ValueError
    if count < 1 or count > len(vals):  # コグニの枚数スピンは 1〜4（表の列数）で止まる
        raise ValueError(f'付加塗装 {kind}: 枚数は 1〜{len(vals)}（{count}）')
    if kind == 'door_sash' and int(paint or 0) == 4:
        return None  # 水性: 標準指数なし（枚数の範囲は上で検証済み）
    return vals[count - 1]


def low_cover_time(roof: int, n_change: int, n_repair: int, paint: int = 2) -> float:
    """低隠蔽性塗色（付加塗装）: COM/fukaetc.DB 6 行目（溶剤）`050,020,030,020,030` / 8 行目（水性）`070,040,060,030,030`
    = [ルーフ取替, ルーフ以外取替 1 枚あたり, ルーフ修理, ルーフ以外修理 1 枚あたり, 基本加算（何か選べば 1 回）]。
    コグニ実機 J52 2026-09-06 夕（2K）: ルーフ取替のみ 0.8、ルーフ修理のみ 0.6、取替 1 のみ 0.5、修理 1 のみ 0.5、ルーフ取替+取替1+修理1 = 1.2、取替 2/3 で +0.2 ずつ、修理 3 で 2.0。
    水性: ルーフ取替のみ 1.0、ルーフ修理のみ 0.9、取替 1 のみ 0.7、取替 2 で 1.1、修理 1 のみ 0.6。2026-09-05 の観測（1.6 / 1.0 / 0.5 / 0.8）もこの式で一致"""
    v = _fukaetc_row(7 if int(paint or 0) == 4 else 5, '低隠蔽性塗色')
    if len(v) < 5:
        raise ValueError('付加塗装 低隠蔽性塗色: fukaetc.DB の列数が不足')
    if roof == 0 and n_change <= 0 and n_repair <= 0:
        return 0.0
    t = v[4] + {1: v[0], 2: v[2]}.get(roof, 0.0)
    if n_change > 0:
        t += v[1] * n_change
    if n_repair > 0:
        t += v[3] * n_repair
    return round(t, 1)


def two_coat_solid_time(roof: int, n_other: int, paint: int = 2) -> float:
    """2コートソリッド（付加塗装、塗膜ソリッド時のみ）: COM/fukaetc.DB 1 行目（溶剤）`040,010` = [ルーフ, ルーフ以外 1 枚あたり]、
    7 行目（水性）`030,010,010` = [ルーフ, ルーフ以外 1 枚あたり, 基本加算]。
    コグニ実機 N-ONE 2026-09-05（2K）: ルーフのみ 0.4、+1 枚 0.5、+5 枚 0.9、+7 枚 1.1。2026-09-06 夕（水性）: 1 枚 0.2、2 枚 0.3、ルーフ+2 枚 0.6"""
    water = int(paint or 0) == 4
    v = _fukaetc_row(6 if water else 0, '2コートソリッド')
    if len(v) < (3 if water else 2):
        raise ValueError('付加塗装 2コートソリッド: fukaetc.DB の列数が不足')
    if not roof and n_other <= 0:
        return 0.0
    t = (v[2] if water else 0.0) + (v[0] if roof else 0.0)
    if n_other > 0:
        t += v[1] * n_other
    return round(t, 1)


def two_tone_time(paint: int, coat_up: int, coat_low: int, count: int) -> Optional[float]:
    """2トーン加算: COM/2TONE.DB（水性は W2TONE.DB）[上部塗膜, 下部塗膜, 枚数1..5]（実機 N-ONE 2コートパール/ソリッド: 1〜3 枚 0.9・4〜5 枚 1.0、ソリッド/ソリッド 4 枚 1.2）"""
    if count < 1 or count > 5:
        return None
    name = 'W2TONE.DB' if paint == 4 else '2TONE.DB'
    for l in _xor_lines_ref(name):
        f = [x.strip() for x in l.split(',')]
        if len(f) >= 7 and f[0] == str(coat_up) and f[1] == str(coat_low):
            return int(f[2 + count - 1]) / 100.0
    return None


MATERIAL_ROUND_MODES = ('四捨五入', '切り上げ', '切り捨て')
MATERIAL_ROUND_UNITS = (10, 1, 100)


def material_round_of(spec) -> tuple[int, str]:
    """`paint.material_round` → (単位, 丸め方)。既定は 10 円四捨五入（コグニ実機 2026-09-05 NEW3/NEW4）。
    書き方は `{"unit": 10, "mode": "切り上げ"}` か、文字列 `"10円切り上げ"` / `"1円四捨五入"`。
    工場のコグニの設定で違う（実案件 NEO 700 本: 10 円四捨五入が大半だが、10 円切り上げでしか説明できない案件が 22 本、
    1 円単位の案件もある。NEO 仕様 §塗装「材料代の端数処理」）"""
    unit, mode = 10, '四捨五入'
    if spec in (None, ''):
        return unit, mode
    if isinstance(spec, dict):
        u, m = spec.get('unit'), spec.get('mode')
    else:
        s = unicodedata.normalize('NFKC', str(spec))
        mm = re.search(r'(\d+)\s*円?', s)
        u = mm.group(1) if mm else None
        m = next((k for k in MATERIAL_ROUND_MODES if k in s), None)
        rest = re.sub(r'\d+', '', s).replace('円', '').replace(' ', '').replace('単位', '')
        if m:
            rest = rest.replace(m, '')
        if rest.strip():   # 「10円切上げ」「foo」のような読めない値を既定に落とさない（Codex 指摘）
            raise ValueError(f'paint.material_round が読めない（例 "10円切り上げ" / {{"unit": 10, "mode": "切り上げ"}}）: {spec!r}')
    if u not in (None, ''):
        try:
            u = int(float(u))
        except (TypeError, ValueError):
            raise ValueError(f'paint.material_round の単位が数でない: {spec!r}')
        if u not in MATERIAL_ROUND_UNITS:
            raise ValueError(f'paint.material_round の単位は {" / ".join(str(x) for x in MATERIAL_ROUND_UNITS)} 円のどれか: {spec!r}')
        unit = u
    if m not in (None, ''):
        if m not in MATERIAL_ROUND_MODES:
            raise ValueError(f'paint.material_round の丸め方は {" / ".join(MATERIAL_ROUND_MODES)} のどれか: {spec!r}')
        mode = m
    return unit, mode


def material_default(total_wage: int, rate, round_=None) -> int:
    """材料代の既定値 = 塗装工賃計 × 割合 を、工場の端数処理で丸めた額（既定 10 円単位で四捨五入。
    コグニ実機 2026-09-05 NEW3/NEW4: 15,500×15%=2,325→2,330、18,950×15%=2,842.5→2,840、46,490×15%=6,973.5→6,970、46,490×14%=6,508.6→6,510。
    コグニは double で計算するので 18,950×0.15 は 2842.4999… → 2,840。Python でも同じ double 計算にする）"""
    unit, mode = material_round_of(round_)
    x = (total_wage or 0) * (float(rate) / 100.0) / unit
    if mode == '切り上げ':
        n = math.ceil(x - 1e-9)
    elif mode == '切り捨て':
        n = math.floor(x + 1e-9)
    else:
        n = math.floor(x + 0.5)
    return int(n) * unit


def cogni_parts_names(name20: str) -> tuple[str, str]:
    """11.DB / 83.DB の名称欄 20 文字 → (PartsName, PartsNameStandard)。コグニ実機（NEW2 / NONE_pnl 2026-09-05）:
    PartsNameStandard = 名称欄を右トリム（' Fﾊﾞﾝﾊﾟﾋﾞ-ﾑ' / 'LFﾊﾞﾝﾊﾟｽﾍﾟ-ｻ' / 'L ﾍﾂﾄﾞﾗｲﾄﾕﾆﾂﾄ' / '  ﾊﾞﾝﾊﾟｸﾘﾂﾌﾟ'）
    PartsName = [0] L→'左' R→'右' ' '→'  '  ＋ [1] F→'Fr' R→'Rr' ' '→'  ' ＋ 残り（'  Frﾊﾞﾝﾊﾟﾋﾞ-ﾑ' / '左Frﾊﾞﾝﾊﾟｽﾍﾟ-ｻ' / '左  ﾍﾂﾄﾞﾗｲﾄﾕﾆﾂﾄ' / '    ﾊﾞﾝﾊﾟｸﾘﾂﾌﾟ'）"""
    n = (name20 or '').rstrip()
    if len(n) < 2:
        return n, n
    side = {'L': '左', 'R': '右'}.get(n[0], '  ')
    fr = {'F': 'Fr', 'R': 'Rr'}.get(n[1], '  ')
    return side + fr + n[2:], n


def _same_part_name(a: str, b: str) -> str:
    """11.DB と 12.DB の名称が同じ品目を指しているか。
    長音とハイフン（ADDATA はどちらの表記も使う）・空白の違いは同じものとみなす"""
    def norm(s: str) -> str:
        # NFKC は半角長音 'ｰ' を全角 'ー' にするだけでハイフンにはしないので、正規化の後にも揃える
        t = unicodedata.normalize('NFKC', (s or ''))
        for ch in ('ー', 'ｰ', '—', '−', '‐'):
            t = t.replace(ch, '-')
        return t.replace(' ', '').replace('　', '')
    return norm(a) == norm(b)


def side_letter(name: str) -> str:
    """見積の名称が示す左右を 1 文字で返す（'L' / 'R' / 不明なら ''）。
    'RRC'（Rear Cross Traffic）のように直後が英字の略語は左右ではないので拾わない"""
    n = unicodedata.normalize('NFKC', name or '').strip()
    if n[:1] in ('左', '右'):
        return 'L' if n[0] == '左' else 'R'
    m = re.match(r'^([LR])(?:[FR](?![A-Za-z])|H|/H|[.\s/])', n)
    return m.group(1) if m else ''


def display_name(std: str) -> str:
    """ADDATA 標準名 (F/R/LF/RF…) → コグニ表示名 (Fr/Rr/左Fr/右Fr…)"""
    s = std.strip()
    for pat, rep in [(r'^LF', '左Fr'), (r'^RF', '右Fr'), (r'^LR', '左Rr'), (r'^RR', '右Rr'),
                     (r'^L ', '左 '), (r'^R ', '右 '), (r'^F(?=[^r])', 'Fr'), (r'^R(?=[^r])', 'Rr')]:
        s2 = re.sub(pat, rep, s)
        if s2 != s:
            return s2
    return s


# ======================================================================
class AddataParts:
    """車種フォルダの 11/12/15/17.DB を使った部品照合"""

    def __init__(self, engine: AddataSearchEngine, car_code: str):
        self.e = engine
        self.car = car_code
        self.p11 = engine.load_11db(car_code)
        self.p12 = engine.load_12db(car_code)
        self.p15 = engine.load_15db(car_code)
        self.blocks = self._load_17(car_code)
        self.tail_by_ref: dict[int, str] = {}
        self.disp_by_ref: dict[int, str] = {}  # 12.DB [52:55] 可能作業
        self.block_by_ref = self._load_12_blocks(car_code)
        self.by_ref: dict[int, list] = {}
        self.by_pn: dict[str, list] = {}
        for r in self.p11:
            self.by_ref.setdefault(r['ref_no'], []).append(r)
            pn = self.norm_pn(r['parts_no'])
            if pn and '-' in str(r['parts_no']):
                self.by_pn.setdefault(pn, []).append(r)
        self.pair_left = {v: k for k, v in self.pair_right.items()}  # 右 ref → 左 ref
        self.qty_by_ref = {ref: int(rec.get('quantity') or 1) for ref, rec in self.p12.items()}
        # 11.DB 名称欄（20 文字: [0] L/R [1] F/R + 名称 + 括弧内の修飾）→ コグニの PartsName 形。工場見積・コグニ書式の見積の名称と完全一致させる索引
        self.name20_by_ref: dict[int, set] = {}
        self._n20_index: dict[str, list] = {}
        try:
            for ref, recs in self._load_11_raw().items():
                for r in recs:
                    n20 = (r.get('name20') or '').rstrip()
                    if len(n20) < 2 or ref in self.name20_by_ref and n20 in self.name20_by_ref[ref]:
                        continue
                    self.name20_by_ref.setdefault(ref, set()).add(n20)
                    key = self.norm_name(cogni_parts_names(n20)[0])
                    if key:
                        lst = self._n20_index.setdefault(key, [])
                        if ref not in lst:
                            lst.append(ref)
        except Exception:
            pass

    @staticmethod
    def norm_pn(s) -> str:
        return re.sub(r'[^0-9A-Z]', '', unicodedata.normalize('NFKC', str(s or '')).upper())

    def _load_17(self, car: str) -> list[tuple[str, int, int]]:
        p = os.path.join(self.e.root, car[0], car, f'{car}17.DB')
        out = []
        if not os.path.exists(p):
            return out
        b = open(p, 'rb').read()
        n = struct.unpack('<I', b[:4])[0]
        o = 16
        while o + 7 <= len(b) and len(out) < n:
            code = b[o:o + 3].decode('latin1')
            f, t = struct.unpack('<HH', b[o + 3:o + 7])
            if re.match(r'^[A-Z][0-9]{2}$', code):
                out.append((code, f, t))
            o += 7
        return out

    def duplicate_groups(self) -> dict[int, list[int]]:
        """12.DB のレベル欄による「重複部品コードチェック」の親子（コグニ実機 J52 2026-09-06 夕: 1400 ⊃ 1410/1430/1434/1441、1500 ⊃ 1503/1505、1600 ⊃ 1603、1611 と 1612）。
        行の修理方法欄（7 文字）の直後 2 文字 = [G フラグ][レベル]。レベル '1' = 親（COMP）、'2' = 同じ部位ブロック内で次の親が現れるまでの子。
        同じ行番号に枝番 02/03… で並ぶ行（Fサイドフレーム 1511 と Fサイドフレーム(後部除く) 1512）は互いに排他。レベル無し（1442、1508 = レベル 1 同士）は重複にならない。
        戻り値 {親 ref: [子 ref...]}（左右 ref とも登録）"""
        if getattr(self, '_dup_groups', None) is not None:
            return self._dup_groups
        out: dict[int, list[int]] = {}
        p = os.path.join(self.e.root, self.car[0], self.car, f'{self.car}12.DB')
        try:
            raw = bytes(b ^ 0xff for b in open(p, 'rb').read())
        except OSError:
            self._dup_groups = out; return out
        parent: Optional[tuple] = None; variants: dict[str, list[tuple]] = {}
        lines = [l for l in raw.split(b'\r\n') if len(l) >= 20 and l[8:11].isdigit()]
        low: dict[bytes, bytes] = {}  # ブロックごとに最小の群（行番号の百の位 = 12.DB の群。1xx/2xx… は同じ行の再収録）
        for l in lines:
            blk_ = l[5:8]; g_ = l[8:9]
            if blk_ not in low or g_ < low[blk_]:
                low[blk_] = g_
        for l in lines:
            if l[8:9] != low.get(l[5:8]):
                continue
            m = re.search(rb'(\d{4})(\d{4}| {4})([A-Z ]{7})(..)( {4}|\d{4})', l[13:])
            if not m:
                continue
            blk = l[5:8]; no = l[8:11]; sub = l[11:13]
            left = int(m.group(1)); right = int(m.group(2)) if m.group(2).strip() else None
            lvl = m.group(4)[1:2]
            if lvl == b'1':
                parent = (blk, left, right)
            elif lvl == b'2' and parent and parent[0] == blk:
                out.setdefault(parent[1], []).append(left)
                if parent[2] and right:
                    out.setdefault(parent[2], []).append(right)
            sub_s = sub.decode('latin1').strip()
            if sub_s.isdigit() and sub_s != '01':  # 02/03… = 同じ行番号の排他バリエーション（01 は説明行・修理限定行で対象外）
                variants.setdefault(blk.decode('latin1') + no.decode('latin1'), []).append((left, right))
        for vs in variants.values():
            for i, (l1, r1) in enumerate(vs):
                for l2, r2 in vs[i + 1:]:
                    out.setdefault(l1, []).append(l2); out.setdefault(l2, []).append(l1)
                    if r1 and r2:
                        out.setdefault(r1, []).append(r2); out.setdefault(r2, []).append(r1)
        self._dup_groups = out
        return out

    def _load_12_blocks(self, car: str) -> dict[int, str]:
        """12.DB 行 '12L10A01001AAFﾊﾞﾝﾊﾟﾌｴｲｽ … 0010 KDS G 0010': [5:8] = 部位ブロックコード（ERParts.BlockCode / DamageParts.BlockCode）"""
        p = os.path.join(self.e.root, car[0], car, f'{car}12.DB')
        out: dict[int, str] = {}
        self.tail_by_ref = {}; self.disp_by_ref = {}
        self.pair_right = {}  # 左 ref → 右 ref（12.DB の 8 桁ペア '00300050'）
        self.order_by_ref = {}  # ref → 12.DB の行番号（W/S の表示順。同名同品番の候補の並び順に使う）
        self.ws_versions = {}  # ref → その部品が載っている W/S 版の集合（行番号 [8:11] の百の位。'0' = 基本版）
        self.disp_by_ver = {}  # ref → {W/S 版: 可能作業}。同じ部品でも版で 'OH ' / 'OHD' のように変わることがある（C88 8700/8850）
        if not os.path.exists(p):  # 12.DB の無い車種（汎用車 Z10 等）でも属性は空で用意する
            return out
        raw = bytes(x ^ 0xFF for x in open(p, 'rb').read())
        for i, l in enumerate(raw.split(b'\n')):
            if len(l) < 20 or not l.startswith(b'12'):
                continue
            tail = l[65:76].decode('cp932', 'replace').rstrip('\r') if len(l) > 65 else ''
            disp3 = l[52:55].decode('latin1') if len(l) > 55 else ''  # 可能作業（K/D/S/C…）。AnSMB 104 桁目の板金可否はここの 'S'
            # 名称の後ろに 左ref(4)+右ref(4 or 空白4) の 8 桁ペア（例 '00300050KS' / '0010    KDS'）
            ver = l[8:9].decode('latin1') if len(l) > 9 else ''  # 行番号の百の位 = W/S 版（0 基本 / 1・2 装備違い）
            m = re.search(rb'(\d{4})(\d{4}|\s{4})[A-Z ]{0,3}', l[13:])
            if m:
                self.ws_versions.setdefault(int(m.group(1)), set()).add(ver)
                if m.group(2).strip():
                    self.ws_versions.setdefault(int(m.group(2)), set()).add(ver)
                out.setdefault(int(m.group(1)), l[5:8].decode('latin1'))
                self.tail_by_ref.setdefault(int(m.group(1)), tail)
                self.disp_by_ref.setdefault(int(m.group(1)), disp3)
                self.disp_by_ver.setdefault(int(m.group(1)), {}).setdefault(ver, disp3)
                self.order_by_ref.setdefault(int(m.group(1)), i)
                if m.group(2).strip():
                    self.pair_right.setdefault(int(m.group(1)), int(m.group(2)))
                    out.setdefault(int(m.group(2)), l[5:8].decode('latin1'))
                    self.tail_by_ref.setdefault(int(m.group(2)), tail)
                    self.disp_by_ref.setdefault(int(m.group(2)), disp3)
                    self.disp_by_ver.setdefault(int(m.group(2)), {}).setdefault(ver, disp3)
                    self.order_by_ref.setdefault(int(m.group(2)), i)
        return out

    def block_of(self, ref_no: int) -> str:
        return self.block_by_ref.get(ref_no, '')

    def no_kd(self, ref_no: int) -> bool:
        """12.DB に行があり、可能作業（[52:55]）に 取替 K も 脱着 D も無い品目か。
        行が無い部品（登録部品 9065 等）は False。欄が空白だけの行も『K も D も無い』として True にする
        （現行 ADDATA では空白欄の部品は 1,192 車種中 0 件だが、行の有無と欄の中身は別物として扱う）。
        同じ部品が版で違う可能作業を持つときは **いちばん小さい W/S 版（通常は基本版）** の行で決まる。
        実機 2026-09-09 cogni_CXE: C88 8700 は 版 0 が 'OH '・版 1 が 'OHD' で、脱着で入れても PartsType 1 / BlockCode 空。
        （現行 ADDATA 300 車種の走査で K/D の有無まで割れるのは C88 8700/8850 の 2 件だけ）"""
        vers = self.disp_by_ver.get(ref_no) or {}
        if not vers:
            return False
        d = vers[min(vers)]  # いちばん小さい W/S 版（通常は基本版 '0'）の行で決まる
        return 'K' not in d and 'D' not in d

    def damage_block(self, ref_no: int) -> str:
        """DamageParts.BlockCode（損傷部品の部位）。ERParts.BlockCode とは別で、12.DB の全 W/S 版から引く。
        ただし基本版(0)に無く複数の版に載っている部品は、どの版の部位か決まらないのでコグニは空にする
        （実機 2026-09-09: J87 0070/0140 = 版 1+2 → 空、J87 0652 = 版 1 のみ → A15、0185 = 版 2 のみ → A01、
        D98 0071/0076/0012 = それぞれ版 1/2/3 のみ → A01）"""
        vers = self.ws_versions.get(ref_no) or set()
        if vers and '0' not in vers and len(vers) > 1:
            return ''
        return self.block_by_ref.get(ref_no, '')

    # 工場見積の表記 → ADDATA 12.DB 標準表記の言い換え（正規化後の半角カナで比較。左が見積側、右が候補の別表記）
    ALIASES = [
        ('ﾏｳﾝﾃｲﾝｸﾞ', 'ﾏｳﾝﾄ'), ('ﾛｱ', 'ﾛﾜ'), ('ｱﾂﾊﾟｰ', 'ｱﾂﾊﾟ'), ('ﾓｰﾙﾃﾞｲﾝｸﾞ', 'ﾓｰﾙ'),
        ('ﾄﾞｱｰ', 'ﾄﾞｱ'), ('ﾄﾘﾑ', 'ﾗｲﾆﾝｸﾞ'), ('ﾍﾂﾄﾞﾗﾝﾌﾟ', 'ﾍﾂﾄﾞﾗｲﾄ'), ('ﾌｵｸﾞﾗﾝﾌﾟ', 'ﾌｵｸﾞﾗｲﾄ'), ('ﾃｰﾙﾗﾝﾌﾟ', 'ﾘﾔｺﾝﾋﾞﾈｰｼﾖﾝﾗﾝﾌﾟ'),
        ('ﾌｴﾝﾀﾞｰﾗｲﾅｰ', 'ｲﾝﾅﾌｴﾝﾀﾞ'), ('ﾌｴﾝﾀﾞﾗｲﾅ', 'ｲﾝﾅﾌｴﾝﾀﾞ'), ('ﾎｲｰﾙﾊｳｽﾗｲﾅ', 'ﾌｴﾝﾀﾞﾗｲﾅ'), ('ｺｱｻﾎﾟｰﾄ', 'ﾊﾞﾙｸﾍﾂﾄﾞ'), ('ｺｱｻﾎﾟｰﾄ', 'ﾗｼﾞｴｰﾀｻﾎﾟｰﾄ'),
        ('ﾗｼﾞｴｰﾀ', 'ﾗｼﾞｴﾀ'), ('ｻｲﾄﾞﾒﾝﾊﾞ', 'ｻｲﾄﾞﾌﾚｰﾑ'), ('ｻｲﾄﾞﾌﾚｰﾑ', 'ｻｲﾄﾞﾒﾝﾊﾞ'), ('ｴﾌﾟﾛﾝ', 'ﾀﾞﾝﾊﾟﾊｳｼﾞﾝｸﾞ'), ('ﾀﾞﾝﾊﾟﾊｳｼﾞﾝｸﾞ', 'ﾌｴﾝﾀﾞｴﾌﾟﾛﾝ'),
        ('ｳｲﾝﾄﾞｼｰﾙﾄﾞ', 'ｳｲﾝﾄﾞｼｰﾙﾄﾞｶﾞﾗｽ'), ('ﾌﾛﾝﾄｶﾞﾗｽ', 'ｳｲﾝﾄﾞｼｰﾙﾄﾞｶﾞﾗｽ'), ('ﾎﾞﾝﾈﾂﾄ', 'ﾌｰﾄﾞ'), ('ﾌｰﾄﾞ', 'ﾎﾞﾝﾈﾂﾄ'), ('ﾊﾞﾂｸﾄﾞｱ', 'ﾃｰﾙｹﾞｰﾄ'), ('ﾃｰﾙｹﾞｰﾄ', 'ﾊﾞﾂｸﾄﾞｱ'),
        ('ﾘﾔｹﾞｰﾄ', 'ﾊﾞﾂｸﾄﾞｱ'), ('ｸｵｰﾀｰ', 'ｸｵｰﾀ'), ('ﾘﾔﾌｴﾝﾀﾞ', 'ｸｵｰﾀﾊﾟﾈﾙ'), ('ｻｲﾄﾞｼﾙ', 'ﾛﾂｶﾊﾟﾈﾙ'), ('ﾛﾂｶﾊﾟﾈﾙ', 'ｻｲﾄﾞｼﾙﾊﾟﾈﾙ'),
        ('ﾊﾞﾝﾊﾟｰﾌｴｲｽ', 'ﾊﾞﾝﾊﾟｶﾊﾞｰ'), ('ﾊﾞﾝﾊﾟﾌｴｲｽ', 'ﾊﾞﾝﾊﾟｶﾊﾞｰ'), ('ﾊﾞﾝﾊﾟｶﾊﾞｰ', 'ﾊﾞﾝﾊﾟﾌｴｲｽ'), ('ﾚｲﾝﾌｵｰｽﾒﾝﾄ', 'ﾘｲﾝﾎｰｽ'), ('ﾚｲﾝﾌｵｰｽ', 'ﾘｲﾝﾎｰｽ'), ('ﾋﾞｰﾑ', 'ﾘｲﾝﾎｰｽ'),
        ('ｴﾝﾌﾞﾚﾑ', 'ｴﾝﾌﾞﾚﾑ(H)'), ('ｽﾃｱﾘﾝｸﾞ', 'ｽﾃｱﾘﾝｸﾞﾎｲｰﾙ'), ('ｺﾝﾃﾞﾝｻｰ', 'ｺﾝﾃﾞﾝｻ'), ('ﾚｼｰﾊﾞｰ', 'ﾚｼｰﾊﾞ'), ('ｾﾂﾄ', ''), ('補機一式', ''), ('一式', ''),
        ('ｸｰﾗｰｶﾞｽ', 'ﾌﾛﾝｶﾞｽ'), ('ｴｱｺﾝｶﾞｽ', 'ﾌﾛﾝｶﾞｽ'),   # エアコンの冷媒（ADDATA は ﾌﾛﾝｶﾞｽ。2026-09-14 スペーシア）
        ('ｴﾑﾌﾞﾚﾑ', 'ｴﾝﾌﾞﾚﾑ'),   # エムブレム（別表記・読み取りの揺れ）→ ADDATA は ｴﾝﾌﾞﾚﾑ（2026-09-15 スペーシア FAX の読み取りで未照合）
    ]

    @staticmethod
    def _prep(s: str) -> str:
        """norm_name と共通の前処理: 半角化・括弧内除去・先頭の連番除去・ホンダ系のカンマ書式の並べ替え（'ﾌｴｲｽ, ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ' → 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰﾌｴｲｽ'）"""
        t = hw(s)
        t = re.sub(r'\(.*?\)|（.*?）|\[.*?\]', '', t)
        t = re.sub(r'^\s*\d+\s+', '', t)  # 先頭の連番 '1 ', '23 '
        if ',' in t:  # ホンダ系ディーラー形式 'ﾌｴｲｽ, ﾌﾛﾝﾄﾊﾞﾝﾊﾟｰ' / 'ﾊﾟﾈﾙCOMP., L. ﾌﾛﾝﾄﾌｴﾝﾀﾞｰ' → 'L ﾌﾛﾝﾄﾌｴﾝﾀﾞｰ ﾊﾟﾈﾙ'
            head, tail = t.split(',', 1)
            side = ''
            m = re.match(r'^\s*([LR])\.\s*', tail)
            if m:
                side = m.group(1); tail = tail[m.end():]
            t = f'{side}{tail.strip()}{head.strip()}'
        return t

    @classmethod
    def _light(cls, s: str) -> str:
        """言い換えを当てるための軽い正規化: 前処理（_prep）・小書きを大書きに・空白・長音・記号を除く。
        norm_name と違い ﾌﾛﾝﾄ/ﾘﾔ/左/右 は残す（norm_name の後だと 右ﾌｪﾝﾀﾞ と ﾘﾔﾌｪﾝﾀﾞ がどちらも Rﾌｴﾝﾀﾞ になり、言い換えを当て分けられない）"""
        t = cls._prep(s or '')
        t = t.translate(str.maketrans('ｧｨｩｪｫｬｭｮｯ', 'ｱｲｳｴｵﾔﾕﾖﾂ'))
        return re.sub(r'[\s\-ｰ‐･・、,.:]', '', t).upper()

    _ALIASES_LIGHT: Optional[list] = None

    @classmethod
    def _aliases_light(cls) -> list:
        """ALIASES を _light で正規化した組"""
        if cls._ALIASES_LIGHT is None:
            out = []
            for a, b in cls.ALIASES:
                al, bl = cls._light(a), cls._light(b)
                if al and al != bl and (al, bl) not in out:
                    out.append((al, bl))
            cls._ALIASES_LIGHT = out
        return cls._ALIASES_LIGHT

    def _name_variants(self, name: str, normalized: bool = False) -> list[str]:
        """見積の名称（正規化済みでも可）に言い換えを 1 段ずつ適用した候補集合（元の名称を先頭に）。これまでどおり norm_name 後の名前に当てる
        （長音入り・ﾌﾛﾝﾄ 入りの言い換えはここでは当たらない。それらは _strict_variants で完全一致のときだけ使う）"""
        n = name if normalized else self.norm_name(name)   # 正規化済み（find_ref の n0）はそのまま（norm_name を 2 回かけない）
        out = [n]
        for a, b in self.ALIASES:
            for base in list(out):
                if a in base:
                    v = base.replace(a, b)
                    if v and v not in out:
                        out.append(v)
        return out[:24]

    def _strict_variants(self, name: str) -> list[str]:
        """_name_variants では当たらなかった言い換え（ﾃｰﾙｹﾞｰﾄ → ﾊﾞｯｸﾄﾞｱ、ﾘﾔﾌｪﾝﾀﾞ → ｸｵｰﾀﾊﾟﾈﾙ、ｸｰﾗｰｶﾞｽ → ﾌﾛﾝｶﾞｽ など）を、
        前後・左右の語を残した形（_light）に当てて norm_name した候補。**12.DB 名と完全一致したときだけ**使う（近似に使うと ｺｱｻﾎﾟｰﾄ → ﾗｼﾞｴｰﾀ本体、
        ﾌﾛﾝﾄｶﾞﾗｽﾓｰﾙ → ｳｲﾝﾄﾞｼｰﾙﾄﾞｶﾞﾗｽ のような別部品に当たる。2026-09-15 検証 520 件）。先頭の左右（左/右/LH/RH）は外して作る"""
        base_old = set(self._name_variants(name))
        # 左右の書き方（左/右/LH/RH/R/H/R./R/F の R/、RF・LR の先頭の左右、'R ' など）は全部外す。前後（ﾘﾔ・Rr・RF の F）は残す
        # （R. ﾊﾞﾝﾊﾟｰﾌｪｲｽ の R を残すと Rﾊﾞﾝﾊﾟｶﾊﾞ = リヤに完全一致する。Codex 指摘）
        t = re.sub(r'^\s*(?:左|右|LH|RH|[LR]/H|[LR]\.|[LR]/(?=[FR])|[LR](?=[FR](?:\s|[ｦ-ﾟ]))|[LR](?=\s))\s*', '', hw(name or ''))
        lights = [self._light(t)]
        for a, b in self._aliases_light():
            for base in list(lights):
                if a in base:
                    v = base.replace(a, b)
                    if v and v not in lights:
                        lights.append(v)
        out = []
        for v in lights[1:]:
            nv = self.norm_name(v)
            if nv and nv not in base_old and nv not in out:
                out.append(nv)
        return out[:12]

    _ROT_SIDE = re.compile(r'^(左|右|LH|RH|[LR]/H|[LR][FR]?|[LR]/[FR]|[LR]\.|Fr|Rr|FR|RR)$')   # 左右・コグニ式の前後（Fr/Rr）は先頭のまま
    _ROT_TAIL = re.compile(r'^(NO\.?\d+|#\d+|\d+|ASSY\.?|ｱｯｼ|ｱﾂｼ|ｱｯｾﾝﾌﾞﾘ|SUB-?ASSY|SUB|COMP\.?|ｺﾝﾌﾟ|付属品|[×xX*]\d+|M\d+)$', re.I)

    @classmethod
    def reordered_names(cls, name: str) -> list[str]:
        """「名詞 修飾」の順（スズキ系の部品表: 'ｸﾞﾘﾙ ﾗｼﾞｴｰﾀ' / 'ﾊﾟﾈﾙ ﾌﾛﾝﾄﾌｰﾄﾞ' / 'ｶﾞｰﾆｯｼｭ ｶｳﾘﾝｸﾞﾄｯﾌﾟ ｾﾝﾀ'）を ADDATA の順（ﾗｼﾞｴｰﾀｸﾞﾘﾙ）に並べ替えた候補。
        先頭の左右（左/右/LH/RH/RF/R. …。末尾にあれば先頭へ）・先頭の連番・括弧（末尾へ）・NO.2・ASSY・付属品・×10 などは並べ替えない。
        空白で 2 語以上に分かれない名前は []（2026-09-14 スペーシア）"""
        t = hw(name or '').strip()
        parens = re.findall(r'\([^()]*\)|（[^（）]*）', t)            # 括弧は空白入りでも 1 つのまま末尾へ
        t = re.sub(r'\([^()]*\)|（[^（）]*）', ' ', t)
        t = re.sub(r'^\s*\d+\s+', '', t)                            # 先頭の連番
        toks = t.split()
        if toks:   # 左右が語にくっついている（LHﾊﾟﾈﾙ ﾌｪﾝﾀﾞ）ときも先頭の左右として切り出す
            m_ = (re.match(r'^(左|右|LH|RH|Fr|Rr|FR|RR|[LR][FR]|[LR]/[FR]|[FLR])(?=[ｦ-ﾟァ-ヶ])', toks[0])
                  or re.match(r'^(左|右|LH|RH)(?=[A-Za-z])', toks[0]))   # Rrｶﾊﾞｰ・RFﾓｰﾙ・Fﾓｰﾙ・Lﾊﾟﾈﾙ も（Codex 指摘）
            if m_:
                toks = [m_.group(1), toks[0][m_.end():]] + toks[1:]
        glued_one = bool(toks) and toks[0] in ('F', 'L', 'R') and not re.match(r'^\s*[FLR]\s', t)   # くっついた 1 文字（Rﾊﾞﾝﾊﾟ の R はリヤかもしれない）は置き換えない
        pre = ''
        if glued_one:
            pre = toks.pop(0)
        elif toks and cls._ROT_SIDE.match(toks[0]):
            pre = toks.pop(0)
        elif len(toks) > 1 and cls._ROT_SIDE.match(toks[-1]):
            pre = toks.pop()
        pre = pre if glued_one else {'LH': '左', 'RH': '右', 'L/H': '左', 'R/H': '右', 'L': '左', 'R': '右'}.get(pre, pre)
        tail = [x for x in toks if cls._ROT_TAIL.match(x)]             # NO.2・ASSY などは途中にあっても末尾へ（並べ替えない）
        toks = [x for x in toks if not cls._ROT_TAIL.match(x)]
        tail += parens
        if len(toks) < 2:
            return []
        if glued_one:   # くっついていた 1 文字は元どおり次の語に付けて返す（Fﾓｰﾙ ﾄﾞｱ → Fﾄﾞｱ ﾓｰﾙ）
            joiner = ''
        else:
            joiner = None
        cands = [toks[1:] + toks[:1]]            # 先頭の名詞を末尾へ（ﾊﾟﾈﾙ ﾌﾛﾝﾄﾌｰﾄﾞ → ﾌﾛﾝﾄﾌｰﾄﾞ ﾊﾟﾈﾙ）
        if len(toks) >= 3:
            cands.append(list(reversed(toks)))    # 全部逆順（ﾗﾍﾞﾙ ｴｱﾊﾞｯｸﾞ ｻｲﾄﾞ → ｻｲﾄﾞ ｴｱﾊﾞｯｸﾞ ﾗﾍﾞﾙ）
        out = []
        for c in cands:
            sep_ = joiner if joiner is not None else (' ' if pre and not re.match(r'^(左|右)$', pre) else '')
            s_ = (pre + sep_ + ' '.join(c + tail)).strip()
            if s_ not in out and c != toks:
                out.append(s_)
        return out

    @staticmethod
    def _fr_word(name: str) -> str:
        """名前（括弧の中も含む）に前後を表す語があれば 'F' / 'R'。両方・無しは ''（左右の R/L とは区別して ﾌﾛﾝﾄ/ﾘﾔ/Fr/Rr だけ見る）"""
        t = hw(name or '')
        f = bool(re.search(r'ﾌﾛﾝﾄ|(^|[^A-Za-z])(Fr|FR)(?![a-z])', t))
        r = bool(re.search(r'ﾘﾔ|ﾘｱ|(^|[^A-Za-z])(Rr|RR)(?![a-z])', t))
        return 'F' if f and not r else ('R' if r and not f else '')

    @staticmethod
    def _why_score(why: str) -> float:
        """find_ref の根拠文 → 名称の確からしさ（名称一致 = 1.0、名称近似(0.76) = 0.76、それ以外（未一致など）= 0）"""
        if (why or '').startswith('名称一致'):
            return 1.0
        m = re.match(r'名称近似\(([\d.]+)\)', why or '')
        return float(m.group(1)) if m else 0.0

    def find_ref(self, code: str, parts_no: str, name: str, context_block: str = '', price: Optional[int] = None, qty: Optional[int] = None, year: str = '') -> tuple[Optional[int], str]:
        """PDF の部品コード → 品番 → 名称 の順で ref_no を決める（_find_ref_core）。名称で決まらない・近似止まりのときは、
        「名詞 修飾」の逆順の名前を並べ替えて引き直す（2026-09-14 スペーシア）。並べ替えた名前は、完全一致するか、元の名前で決まらず
        名前が 0.75 以上近く**標準単価が印字の単価とぴったり合う**ときだけ採る（名前が近いだけでは採らない: 普通の語順の名前を並べ替えた候補は
        12 件中 12 件が別部品だった。2026-09-15 検証）。括弧の修飾（ﾚｶﾛ 等）まで合っている元の近似は動かさない"""
        ref, why = self._find_ref_core(code, parts_no, name, context_block, price, qty, year)
        s0 = self._why_score(why) if ref is not None else 0.0
        if ref is not None and (s0 >= 1.0 or not (why or '').startswith('名称')):
            return ref, why   # 部品コード・品番・名称の完全一致は動かさない
        q = self._qual(name)
        if ref is not None and q and q in {self._qual(n) for n in (self.name20_by_ref.get(ref) or ())}:
            return ref, why   # 括弧の修飾まで合った近似（11.DB の名称欄は 20 字で切れていて、並べ替えた完全一致は修飾を見分けられない）
        est_fr = self._fr_word(name)
        for rn in self.reordered_names(name):
            r2, w2 = self._find_ref_core('', parts_no, rn, context_block, price, qty, year)
            if r2 is None or (w2 or '').startswith(('部品コード', '品番')):
                continue
            # 候補の前後は 11.DB 名称欄の 2 文字目（1 文字目は左右 L/R/空白。_rank_refs と同じ読み方。RF = 右前・LR = 左後）。
            # 名前（括弧の中も）の前後と候補の前後が違う（ﾓｰﾙ ﾄﾞｱ (ﾌﾛﾝﾄ ﾛｱ) → Rﾄﾞｱﾓｰﾙ）は採らない（Codex 指摘）
            c_frs = {n[1] for n in (self.name20_by_ref.get(r2) or ()) if len(n) > 1 and n[1] in ('F', 'R')}
            if est_fr and c_frs and est_fr not in c_frs:
                continue
            s2 = self._why_score(w2)
            unit_ok = bool(price) and any(int(v.get('price') or 0) == int(price) for v in (self.by_ref.get(r2) or []))
            if (s2 >= 1.0 and s2 > s0) or (ref is None and s2 >= 0.75 and unit_ok):
                return r2, f'語順入替「{rn}」 {w2}'
        return ref, why

    def _pick_ref_by_context(self, refs: list[int], name: str, context_block: str) -> int:
        """品番が同じ複数 ref から 1 つ選ぶ: 直前行と同じ部位ブロック → 見積名称に最も近い 12.DB 名 → 先頭"""
        if len(refs) == 1:
            return refs[0]
        pool = [r for r in refs if context_block and self.block_of(r) == context_block] or refs
        if len(pool) > 1 and name:
            import difflib
            n0 = re.sub(r'^(LH|RH|[LR])(?=[^A-Z])', '', self.norm_name(name))

            def _score(r: int) -> float:
                rec = self.p12.get(r)
                if not rec:
                    return 0.0
                cand = re.sub(r'^(LH|RH|[LR])(?=[^A-Z])', '', self.norm_name(rec['name']))
                cand2 = cand[1:] if cand[:1] in ('F', 'R') else cand
                return max(difflib.SequenceMatcher(None, cand, n0).ratio(), difflib.SequenceMatcher(None, cand2, n0).ratio())
            pool = sorted(pool, key=lambda r: -_score(r))
        return pool[0]

    def infer_from_parts(self, items: list) -> dict:
        """見積の品番から車両条件を逆引きする（車検証だけではグレードが絞れないときのヒント）。
        11.DB の K 変種行は品番ごとにフラグ（[0:5] グレード記号のいずれか、[5:7] EVA/FVA 記号）と年式群を持つ。同じ ref に品番違いの行が複数あり、
        見積の品番がフラグ付きの行にだけ一致するとき、その行が許すグレード／群／装備が証拠になる。証拠が矛盾する（別グレード用の部品を敢えて選んだ行がある）
        ときはグレードを返さない。コグニ生成 NEO 10 本の検証（2026-09-06）: J97 → A、W64 → B が正解、D88 は矛盾（ドアガラスだけ D 用）で None、
        年式群は S64 2 / U52 1 / D98 1 が正解。戻り値 {'grade_codes': set|None, 'year_group': str|None, 'eva_codes': set, 'evidence': [str]}"""
        raw = self._load_11_raw()
        grade_sets = []; grp_sets = []; eva_votes = {}; ev = []
        for it in items or []:
            pn = self.norm_pn(re.sub(r'\s*\(\d+\)\s*$', '', str(it.get('parts_no') or '')))
            if not pn:
                continue
            refs = sorted({r['ref_no'] for r in self.by_pn.get(pn, [])})
            if not refs:
                continue
            per_ref = []  # 同じ品番を持つ全 ref の条件（ref ごとに (g_allow|None, grps, eva共通)）
            for ref in refs:
                krows = [x for x in raw.get(ref, []) if x['disp'] == 'K']
                mine = [x for x in krows if self.norm_pn(x['pn']) == pn]
                if not mine or len({self.norm_pn(x['pn']) for x in krows}) < 2:
                    continue  # 品番違いの変種が無い部品は証拠にならない
                g_allow = set(); restrictive = True
                for x in mine:
                    g = [ch for ch in x['flags'][0:5] if ch.strip()]
                    if g:
                        g_allow |= set(g)
                    else:
                        restrictive = False  # 無条件行が混じる = 全グレード可
                evs = [set(ch for ch in x['flags'][5:7] if ch.strip()) for x in mine]
                per_ref.append((frozenset(g_allow) if (restrictive and g_allow) else None, frozenset(x['grp'] for x in mine), frozenset(set.intersection(*evs) if evs else set())))
            if not per_ref or len({p_[0] for p_ in per_ref}) > 1 or len({p_[1] for p_ in per_ref}) > 1:
                continue  # 同一品番の ref 間で条件が食い違う（別部位の同名クリップ等）ときは証拠にしない
            g_allow, grps, eva_common = per_ref[0]
            if g_allow:
                grade_sets.append(set(g_allow)); ev.append(f'{refs[0]:04d} {pn} → グレード {"".join(sorted(g_allow))}')
            if '' not in grps:
                grp_sets.append(set(grps)); ev.append(f'{refs[0]:04d} {pn} → 年式群 {"/".join(sorted(grps))}')
            for ch in eva_common:
                eva_votes[ch] = eva_votes.get(ch, 0) + 1
        grade = None
        if grade_sets:
            inter = set.intersection(*grade_sets)
            if inter:  # 証拠が矛盾しなければヒントにする（1 行でも。車検証で確定できるときは使われない）
                grade = inter
            else:
                ev.append('グレード証拠が矛盾（別グレード用の部品が混在）→ グレードは車検証のみで決める')
        grp = None
        if grp_sets:
            gi = set.intersection(*grp_sets)
            if len(gi) == 1:
                grp = next(iter(gi))
        return {'grade_codes': grade, 'year_group': grp, 'eva_codes': {k for k, v in eva_votes.items() if v >= 2}, 'evidence': ev}

    @staticmethod
    def _qual(name: str) -> str:
        """名称の括弧内（(ｺｳﾁﾝ) / (6個) / (ﾄｿｳｽﾞﾐ) 等）を正規化して返す。無ければ ''"""
        t = hw(name or '')
        m = re.findall(r'\((.*?)\)', t)
        return re.sub(r'[\s\-ｰ‐･・、,.:]', '', ''.join(m)).translate(str.maketrans('ｧｨｩｪｫｬｭｮｯ', 'ｱｲｳｴｵﾔﾕﾖﾂ')) if m else ''

    @staticmethod
    def _fr_of(name: str) -> str:
        """名称の前後指定: 'F'（Fr/ﾌﾛﾝﾄ/F）/ 'R'（Rr/ﾘﾔ/ﾘｱ）/ ''"""
        t = re.sub(r'^\s*(左|右|LH|RH|[LR])\.?\s*', '', hw(name or ''))
        if re.match(r'^(Fr|FR|ﾌﾛﾝﾄ|F(?=[\sｦ-ﾟ]))', t):
            return 'F'
        if re.match(r'^(Rr|RR|ﾘﾔ|ﾘｱ|R(?=[\sｦ-ﾟ]))', t):
            return 'R'
        return ''

    def _rank_refs(self, refs: list, name: str, context_block: str, qty: Optional[int] = None, price: Optional[int] = None, year: str = '') -> list:
        """同一品番・同名の複数 ref を、見積の名称（左右・前後・括弧内の修飾）・数量・直前行の部位・12.DB の行順で並べる。
        コグニ生成 NEO 11 本のラウンドトリップ検証（2026-09-05）: 0171 (6個)/0172 (3個) は数量、0532/0582 は左右、5946/5996 は左右ペア、2960 は Rr で決まる"""
        side = self._side_of(name); fr = self._fr_of(name); q = self._qual(name)
        n0 = re.sub(r'^(LH|RH|[LR])(?=[^A-Z])', '', self.norm_name(name))
        raw_est = re.sub(r'\s+', '', hw(name or ''))
        grp = str(year).strip()[-1] if str(year or '').strip().isdigit() and int(year) else ''  # 年式群 = YearCode の下 1 桁
        import difflib

        def key(ref: int):
            n20s = self.name20_by_ref.get(ref) or set()
            side20 = {n[0] for n in n20s if len(n) > 1}
            fr20 = {n[1] for n in n20s if len(n) > 1}
            side_ok = 1 if (not side or side in side20 or (side == 'R' and (ref in self.pair_left or ref in self.pair_right)) or (side == 'L' and ref in self.pair_right)) else 0  # 右指定は右 ref か、右へ変換できる左 ref（品番が左にしか無いペア）
            side_bad = 1 if (not side and side20 and side20 <= {'L', 'R'} and (ref in self.pair_right or ref in self.pair_left)) else 0  # 左右指定の無い見積名に左右付き部品
            qty_ok = 1 if (qty and qty > 1 and (self.qty_by_ref.get(ref) == qty or any(f'({qty}個)' in n for n in n20s))) else 0
            qty_bad = 1 if (qty and self.qty_by_ref.get(ref, 1) > 1 and self.qty_by_ref.get(ref) != qty) else 0
            n20_exact = 1 if any(self.norm_name(cogni_parts_names(n)[0]) == self.norm_name(name) for n in n20s) else 0
            q20 = {self._qual(n) for n in n20s}
            qual_ok = 1 if (q and q in q20) else 0
            qual_bad = 1 if (not q and q20 and q20 != {''}) else 0
            fr_ok = 1 if (not fr or fr in fr20 or ' ' in fr20) else 0
            ctx_ok = 1 if (context_block and self.block_of(ref) == context_block) else 0
            rec = self.p12.get(ref) or self.p12.get(self.pair_left.get(ref, -1))
            cand = re.sub(r'^(LH|RH|[LR])(?=[^A-Z])', '', self.norm_name(rec['name'])) if rec else ''
            sim = max(difflib.SequenceMatcher(None, cand, n0).ratio(), difflib.SequenceMatcher(None, cand[1:] if cand[:1] in ('F', 'R') else cand, n0).ratio()) if cand else 0.0
            rows11 = self.by_ref.get(ref) or []
            price_ok = 1 if (price and any(int(r.get('price') or 0) == price for r in rows11)) else 0  # 同名同品番でも単価が違う変種（ﾌｴﾝﾀﾞﾌﾞﾗｹﾂﾄ 450/510 円）
            price_bad = 1 if (price and rows11 and all(int(r.get('price') or 0) > 0 and int(r.get('price') or 0) != price for r in rows11)) else 0
            raw11 = [re.sub(r'\s+', '', cogni_parts_names(n)[0]) for n in n20s]
            raw_sim = max([difflib.SequenceMatcher(None, x, raw_est).ratio() for x in raw11] or [0.0])  # 年式注記 'H30.5-' と '-H30.5' の向きまで含めた生文字列の近さ
            rows_y = self._load_11_raw().get(ref) or []
            year_exact = 1 if (grp and any((r.get('grp') or '') == grp for r in rows_y)) else 0  # 車両の年式群そのものの変種行がある
            year_bad = 1 if (grp and rows_y and all((r.get('grp') or '') not in ('', grp) for r in rows_y)) else 0  # 別の年式群しか無い
            return (-side_ok, side_bad, -n20_exact, -qual_ok, qual_bad, year_bad, qty_bad, -qty_ok, -fr_ok, price_bad, -price_ok, -ctx_ok, -round(raw_sim, 2), -round(sim, 2), -year_exact, self.order_by_ref.get(ref, 1 << 30), ref)  # 年式群の一致は最後のタイブレーク（先に置くと共通行（群 ''）の正しい候補を落とし 714→713・693→687 に悪化: 2026-09-06 実測）
        return sorted(refs, key=key)

    @staticmethod
    def _side_of(name: str) -> str:
        t = hw(name or '')
        if re.search(r'(^|[\s,])(RH|R/H|R\.|R(?=[\sｦ-ﾟ])|R/?[FR](?=[\s,ｦ-ﾟ]|$))|右', t):  # RH / R/H / R. / R ﾌｪﾝﾀﾞ / RF / RR / R/F（R/H は 2026-09-15 検証で追加）
            return 'R'
        if re.search(r'(^|[\s,])(LH|L/H|L\.|L(?=[\sｦ-ﾟ])|L/?[FR](?=[\s,ｦ-ﾟ]|$))|左', t):
            return 'L'
        return ''

    def _find_ref_core(self, code: str, parts_no: str, name: str, context_block: str = '', price: Optional[int] = None, qty: Optional[int] = None, year: str = '') -> tuple[Optional[int], str]:
        """PDF の部品コード → 品番 → 名称 の順で ref_no を決める。戻り値 (ref_no, 根拠)
        名称照合は 表記ゆれ辞書（ALIASES）・左右（12.DB の左右ペア）・部位文脈（直前行の部位ブロック）・価格整合で絞る。
        同一品番の複数 ref は `_rank_refs`（左右・前後・括弧内修飾・数量・部位・12.DB 行順）で選ぶ。'〜付属品' 行は本体部品の ref（脱着行）"""
        c = re.sub(r'\D', '', code or '')
        if c and (int(c) in self.by_ref or int(c) in self.p12):
            return int(c), f'部品コード {c}'
        fuzoku = ''
        if re.search(r'付属品\s*$', hw(name or '')):
            name = re.sub(r'\s*付属品\s*$', '', hw(name)); fuzoku = ' 付属品'
        pn = self.norm_pn(re.sub(r'\s*\(\d+\)\s*$', '', parts_no or ''))
        pn_known = False
        if pn:
            exact = self.by_pn.get(pn, [])
            if not exact:  # 11.DB に無く 83.DB（色別部品）にだけある品番（N-ONE FAX の 71101-T4G-N00ZG 等）も ref の根拠にする
                exact = [{'ref_no': ref, 'parts_no': r['pn'], 'price': r['price']} for ref, rows in self._load_83_raw().items() for r in rows if self.norm_pn(r['pn']) == pn]
            near = [] if exact else [r for k, rows in self.by_pn.items() if k[:-2] == pn[:-2] and len(k) == len(pn) for r in rows]
            cands = exact or near
            if cands and not exact and price and price > 0:  # 枝番違い（OCR 誤読含む）の候補は単価も照合し、大きく違う部品は除く
                cands = [r for r in cands if int(r.get('price') or 0) <= 0 or (price <= int(r['price']) * 2.2 and price * 2.2 >= int(r['price']))]
            pn_known = bool(exact or near)
            if cands:
                refs = []
                for r in cands:  # 同一品番が複数 ref（クリップ 0170/0171/0372…、左右・別部位）に出るので 左右/数量/修飾/部位/行順 で絞る
                    if r['ref_no'] not in refs:
                        refs.append(r['ref_no'])
                ref = self._rank_refs(refs, name, context_block, qty, price, year)[0]
                if self._side_of(name) == 'R' and ref in self.pair_right:  # 右指定で左 ref が選ばれたら右 ref へ（品番が左にしか無いペアも、両方ある場合も）
                    ref = self.pair_right[ref]
                tag = '品番一致' if exact else '品番近似'
                return ref, f'{tag} {parts_no}' + ('' if exact else f'≈{cands[0]["parts_no"]}') + (f'（{len(refs)} 候補から左右/数量/部位で選択）' if len(refs) > 1 else '') + fuzoku
        n0 = self.norm_name(name)
        if not n0:
            return None, '未一致'
        # 11.DB 名称欄の完全一致（コグニ書式の見積・コグニ生成 NEO の PartsName はこの形。括弧内の修飾も比較）
        exact20 = self._n20_index.get(n0) or []
        if exact20 and price and price > 0:  # 名称完全一致でも単価が標準の 2.2 倍超／半分未満なら別部品（12.DB 近似と同じ価格整合）
            def _price_ok(ref_: int) -> bool:
                stds = [int(r['price'] or 0) for r in self.by_ref.get(ref_, []) if int(r['price'] or 0) > 0]
                return not stds or not all((price > st * 2.2 or price * 2.2 < st) for st in stds)
            exact20 = [r_ for r_ in exact20 if _price_ok(r_)]
        if fuzoku and not exact20:  # '左Frﾄﾞｱ付属品' → 本体パネル 'LFﾄﾞｱﾊﾟﾈﾙ'（同名＋ﾊﾟﾈﾙ だけ。ｽﾃｰ 等の別部品は拾わない）
            if (n0 + 'ﾊﾟﾈﾙ') in self._n20_index:
                exact20 = list(self._n20_index[n0 + 'ﾊﾟﾈﾙ'])
            elif (n0 + 'ﾊﾟﾈﾙ') in self._n20_index or any(k == n0 for k in self._n20_index):
                pass
            else:
                return None, f'未一致（付属品の本体 {n0} が 11.DB に無い）'
        if exact20 and pn and pn_known is False:  # 見積の品番が ADDATA に無い: 品番の先頭 5 桁（部品群）が違う候補は別部品として除く
            def _pn_family_ok(ref_: int) -> bool:
                cand_pns = [self.norm_pn(r['parts_no']) for r in self.by_ref.get(ref_, []) if '-' in str(r.get('parts_no', ''))]
                return not cand_pns or any(k[:5] == pn[:5] for k in cand_pns)
            exact20 = [r_ for r_ in exact20 if _pn_family_ok(r_)]
        if fuzoku and not exact20:
            return None, f'未一致（付属品の本体 {n0} が候補に残らない）'
        if exact20:
            ranked = self._rank_refs(list(exact20), name, context_block, qty, price, year)
            ref = ranked[0]
            if exact20:
                return ref, f'名称一致(11.DB) {sorted(self.name20_by_ref.get(ref) or [""])[0].strip()}' + (f'（{len(exact20)} 候補）' if len(exact20) > 1 else '') + fuzoku
        import difflib
        side = self._side_of(name)
        strip_side = lambda t: re.sub(r'^(LH|RH|[LR])(?=[^A-Z])', '', t)  # 12.DB の名称は左右を持たない
        variants = [strip_side(v) for v in self._name_variants(n0, normalized=True)]
        strict = self._strict_variants(name)
        scored = []  # (score, ref, 12.DB 名, variant index)
        for ref, rec in self.p12.items():
            cand = strip_side(self.norm_name(rec['name']))
            if not cand:
                continue
            best_v = 0.0; best_i = 0
            cand_nofr = cand[1:] if (cand[:1] in ('F', 'R') and len(cand) > 2) else ''  # 12.DB の F/R 接頭辞（見積側に前後の指定が無い）
            for vi, v in enumerate(variants):
                if cand == v:
                    sc = 1.0
                else:
                    sc = difflib.SequenceMatcher(None, cand, v).ratio()
                    if cand in v or v in cand:
                        sc = max(sc, 0.8)
                    if cand_nofr and v[:1] not in ('F', 'R'):
                        sc2 = 1.0 if cand_nofr == v else difflib.SequenceMatcher(None, cand_nofr, v).ratio()
                        sc = max(sc, sc2 - 0.01)  # 接頭辞を無視した一致は僅かに劣後（部位文脈で F/R を決める）
                if sc > best_v:
                    best_v, best_i = sc, vi
            if best_v < 1.0 and strict:   # 新しく当たるようになった言い換えは、12.DB 名そのもの（R = リヤを外さない形）と完全一致したときだけ
                cand_raw = self.norm_name(rec['name'])
                for si, v in enumerate(strict):
                    if cand_raw == v or (cand_raw[:1] == 'F' and cand_raw[1:] == v):
                        best_v, best_i = 1.0, len(variants) + si
                        break
            if best_v >= 0.75:
                q_est = self._qual(name); q20 = {self._qual(n) for n in (self.name20_by_ref.get(ref) or set())}
                if q_est and q_est in q20:
                    best_v = min(1.0, best_v) + 0.03
                elif not q_est and q20 and q20 != {''}:
                    best_v -= 0.01
                scored.append((best_v, ref, rec['name'], best_i))
        if not scored:
            return None, '未一致'
        scored.sort(key=lambda x: (-x[0], x[3], x[1]))
        top = scored[0][0]
        ties = [x for x in scored if x[0] >= top - 0.02]
        pick = ties[0]
        if side and len(ties) > 1:  # 左右指定があれば左右ペアを持つ部品（0065/0066）を、中央の単独部品（0045）より優先
            paired = [x for x in ties if x[1] in self.pair_right]
            if paired:
                ties = paired; pick = ties[0]
        if context_block and len(ties) > 1:  # 部位文脈: 同点候補は直前行と同じ部位ブロックを優先（バンパクリップ 0170 / 0372 等）
            same = [x for x in ties if self.block_of(x[1]) == context_block]
            if same:
                pick = same[0]
        # 選んだ候補から順に 品番整合・価格整合 を確認し、通らなければ同点圏内の次候補を試す（クリップ・モール類の同名部品）
        ordered = [pick] + [x for x in ties if x is not pick]
        reject = ''
        for sc, ref, std, vi in ordered:
            if side == 'R' and ref in self.pair_right:  # 右指定なら 12.DB ペアの右 ref（品番・価格は右 ref で検証する）
                ref = self.pair_right[ref]
            if pn:  # 見積に品番があり、候補の 11.DB 品番と一致しないなら別部品（O リング 80872-ST7-000 vs 46134-SNC-A01）。ADDATA に無い品番（年式違い・色別 52119-B5100-A1 等）は先頭 5 桁（部品群）が同じなら同じ部品
                cand_pns = [self.norm_pn(r['parts_no']) for r in self.by_ref.get(ref, []) if '-' in str(r.get('parts_no', ''))]
                if cand_pns and not any(k == pn or (len(k) == len(pn) and k[:-2] == pn[:-2]) or (not pn_known and k[:5] == pn[:5]) for k in cand_pns):
                    reject = reject or f'名称候補 {std} は品番不一致（見積 {parts_no} / 標準 {cand_pns[0]}）'
                    continue
            if price and price > 0:  # 価格整合: 名称だけの照合で単価が標準の 2.2 倍超／半分未満なら別部品
                stds = [int(r['price'] or 0) for r in self.by_ref.get(ref, []) if int(r['price'] or 0) > 0]
                if stds and all((price > st * 2.2 or price * 2.2 < st) for st in stds):
                    reject = reject or f'名称候補 {std} は価格不整合（見積 {price} / 標準 {min(stds)}〜{max(stds)}）'
                    continue
            break
        else:
            return None, reject or '未一致'
        why = ('名称一致' if sc >= 1.0 else f'名称近似({sc:.2f})') + (f' 言換{vi}' if vi else '') + (' 右' if side == 'R' else '') + f' {std}' + fuzoku
        return ref, why

    @staticmethod
    def norm_name(s: str) -> str:
        """部品名の正規化: 半角化・Fr→F/Rr→R/左→L/右→R・括弧内除去・小書き仮名を大書きに・長音/記号除去"""
        t = AddataParts._prep(s)
        t = t.replace('ﾌﾛﾝﾄ', 'F').replace('ﾘﾔ', 'R').replace('ﾘｱ', 'R').replace('Fr', 'F').replace('Rr', 'R').replace('FR', 'F').replace('RR', 'R')
        t = t.replace('左', 'L').replace('右', 'R')
        t = t.translate(str.maketrans('ｧｨｩｪｫｬｭｮｯ', 'ｱｲｳｴｵﾔﾕﾖﾂ'))
        t = re.sub(r'[\s\-ｰ‐･・、,.:S]$', '', t)
        t = re.sub(r'[\s\-ｰ‐･・、,.:]', '', t)
        t = re.sub(r'(ｱｯｼｭ|ASSY|Assy|ASSY\.|ｱｯｾﾝﾌﾞﾘ|COMP\.?|ｺﾝﾌﾟ)$', '', t, flags=re.I)
        return t.upper()

    def variant(self, ref_no: int, parts_no: str, ctx: Optional[dict] = None, dcode: int = 0) -> tuple[Optional[dict], list[dict]]:
        """ref の 11.DB 変種のうち PDF 品番と一致するものと、それ以外。品番が無い／一致しないときは車両条件（年式群→フラグ）で
        コグニの部品検索と同じ変種を選ぶ（ctx = {'grade','fva','eva','year'}。コグニ実機 FRAME_p8 1904: 年式 01 → 群 1 の 61160-T4G-J00ZZ 14,100）"""
        rows = [r for r in self.by_ref.get(ref_no, []) if '-' in str(r['parts_no'])]
        b = str((ctx or {}).get('body') or getattr(self, 'vehicle_body', '') or '').strip()
        b_ = int(b) if b.isdigit() else 0  # 未確定・非数値のボディは 0（共通行だけ）= _std_row と同じ扱い（Codex 99）
        raw11 = self._load_11_raw().get(ref_no, [])
        if rows and raw11:  # 他ボディ専用の 11.DB 行は候補から外す（Codex 98/100: 許される行が無ければ変種なし。11.DB 生行が読めない車種だけ従来どおり）
            ok_pn = {self.norm_pn(r['pn']) for r in raw11 if r.get('disp') == 'K' and int(r.get('body') or 0) in (0, b_)}
            rows = [r for r in rows if self.norm_pn(r['parts_no']) in ok_pn]
        pn = self.norm_pn(re.sub(r'\s*\(\d+\)\s*$', '', parts_no or ''))
        hit = [r for r in rows if self.norm_pn(r['parts_no']) == pn] if pn else []
        if not hit and pn:
            hit = [r for r in rows if self.norm_pn(r['parts_no'])[:-2] == pn[:-2]]
        if not hit and rows and ctx:
            y = str(ctx.get('year') or '').strip()
            grp = y[-1] if y.isdigit() and int(y) else ''
            srow = self._std_row(ref_no, dcode, ctx.get('grade', ''), ctx.get('fva', ''), set(ctx.get('eva') or ()), grp, ctx.get('body'))
            if srow and srow.get('pn'):
                hit = [r for r in rows if self.norm_pn(r['parts_no']) == self.norm_pn(srow['pn'])]
                return (hit[0] if hit else rows[0]), [r for r in rows if r not in hit]
        return (hit[0] if hit else (rows[0] if rows else None)), [r for r in rows if r not in hit]

    def wage_entries(self, ref_no: int) -> list[dict]:
        return self.p15.get(ref_no, []) or []

    # ------------------------------------------------------------ コグニの標準指数（11.DB 変種行 × 15.DB 区分）
    def _load_11_raw(self) -> dict[int, list[dict]]:
        """11.DB を復号し、変種行の 修理方法/群/フラグ/区分レター/暫定 を返す（engine.load_11db は品番・価格のみ）"""
        if getattr(self, '_r11', None) is not None:
            return self._r11
        out: dict[int, list[dict]] = {}
        try:
            from _addata_db_search import LCG
            H, B = self.e.HEADER_11, self.e.RECORD_11
            p = os.path.join(self.e.root, self.car[0], self.car, f'{self.car}11.DB')
            raw = open(p, 'rb').read(); ks = LCG(self.e._read_seed(self.car)).keystream(B)
            for ri in range((len(raw) - H) // B):
                off = H + ri * B; rec = bytes(a ^ b for a, b in zip(raw[off:off + B], ks)); ref = struct.unpack_from('<H', rec, 0)[0]
                if not ref:
                    continue
                wc = rec[4:7].decode('latin1'); fl = rec[8:25].decode('latin1')
                # rec[7] = ボディコード条件（0 = 共通、0x14 = ボディ 20、0x1e = 30 …。KA81 の id と同じ 1 バイト整数）。コグニ実機 COLOR_D98.neo（D98 ボディ 20・年式 01・W25、2026-09-06 夜）:
                #   0010 → 群 1 の共通行 52101-B2B90-C0 ではなくボディ 20 行 52119-B2G40-C0（→ 13.DB W25 の -A0 51,900）、0073 → ボディ 30 行 52713-B2480-C0 を除外して共通行 52713-B2470、
                #   0124 → 共通行（色別フラグ 1）よりボディ 20 行（色別フラグ 0）が優先され -C0 のまま。ボディ固有行 > 共通行、他ボディの行は除外
                out.setdefault(ref, []).append({'disp': wc[0:2].strip(), 'grp': wc[2].strip(), 'body': rec[7], 'flags': fl[0:7], 'secs': fl[7:].strip(),  # disp = 'K'/'D'/'DS'/'S'/'C'
                                                'pn': rec[45:62].decode('latin1').strip(), 'pn_raw': rec[45:62].decode('latin1').rstrip(), 'prov': rec[71:72] == b'$',  # pn_raw = 位置を保った生値（'-' 部品は '     -'）
                                                # [62:68] 価格、[68:70] ConstructGroup（ERParts.ConstructGroup にそのまま入る: 'P6'/'G4'/'R0'/'MM'）、[70] 色別部品フラグ（1 = 83.DB に色別品番あり）— コグニ新規見積 NEW2 で確認 2026-09-05
                                                'cgroup': rec[68:70].decode('latin1'), 'color_flag': rec[70], 'name20': rec[25:45].decode('cp932', 'replace')})
        except Exception as _e11:  # 解析に失敗したら空にするが、黙って標準指数が全滅するので理由を残す（監査 4）
            out = {}
            self._r11_error = str(_e11)  # 控えは NeoBuilder.build_rows が拾って silent_errors に載せる（このクラスに _note_silent は無い）
        self._r11 = out
        return out

    def _load_83_raw(self) -> dict[int, list[dict]]:
        """<car>83.DB = 色別部品価格（LCG。u16 件数 + 8B 索引 (ref, sub=色群×100, from, to) + 201B ブロック、ブロックごとにキーストリーム先頭から）。
        ブロック: [ref u16][sub u16][32 u16][flags 8: グレード/FVA/EVA 文字][名称 20][品番 17][価格 6][ConstructGroup 2][' '][カラーコード]。
        コグニは Car.ColorCode に一致するブロックの品番・価格・名称（'(ﾄｿｳｽﾞﾐ)' 付き）を 11.DB の変種より優先して明細に入れる（NEW2: 0010 → 71101-T4G-N00ZG 50,700）"""
        if getattr(self, '_r83', None) is not None:
            return self._r83
        out: dict[int, list[dict]] = {}
        # 83.DB（188 車種、201B ブロック）か 13.DB（672 車種、189B ブロック。両者は排他）。13.DB は 83.DB と同じ列に「適用開始/終了 YYYYMM（t[71:77]/t[77:83]）・備考（t[83:]）」が付く
        # （COLOR_D98.neo 2026-09-06 夜: D98 W25 で 0010 → 13.DB の 52119-B2G40-A0 51,900、0094 → 52722-B2121-A0、期間違いの 0182 はコグニがダイアログで選ばせ 52561-B2030 150 を PartsNo/PartsNoStandard/PartsPriceStandard に書く）
        p83 = os.path.join(self.e.root, self.car[0], self.car, f'{self.car}83.DB')
        p13 = os.path.join(self.e.root, self.car[0], self.car, f'{self.car}13.DB')
        p, blen, src = (p83, 201, '83') if os.path.exists(p83) else (p13, 189, '13')
        if os.path.exists(p):  # ファイル欠落（色別部品の無い車種）だけ空扱い。解析失敗は例外にして黙って 11.DB へ落ちないようにする
            from _addata_db_search import LCG
            try:
                raw = open(p, 'rb').read(); n = struct.unpack_from('<H', raw, 0)[0]; base = 2 + n * 8
                if len(raw) < 2 or base > len(raw) or (len(raw) - base) % blen != 0:
                    raise ValueError(f'{self.car}{src}.DB の長さが不正（件数 {n}、本体 {len(raw) - base} バイトは {blen} の倍数でない）')
                nblocks = (len(raw) - base) // blen
                idx_to = [struct.unpack_from('<HHHH', raw, 2 + i * 8)[3] for i in range(n)]
                if idx_to and max(idx_to) >= nblocks:  # 索引が指す範囲が本体に無い（末尾欠損・レイアウト誤認）
                    raise ValueError(f'{self.car}{src}.DB の索引が本体の範囲外（to={max(idx_to)}, ブロック数 {nblocks}）')
                ks = LCG(self.e._read_seed(self.car)).keystream(blen)
                body = raw[base:]
                for k in range(len(body) // blen):
                    dec = bytes(a ^ c for a, c in zip(body[k * blen:(k + 1) * blen], ks))
                    ref = struct.unpack_from('<H', dec, 0)[0]
                    if not ref:
                        continue
                    t = dec[6:].decode('cp932', 'replace')
                    fl = t[0:7]; name20 = t[7:27]; name = name20.strip(); pn = t[27:44].strip(); price = t[44:50].strip(); cg = t[50:52]; color = t[53:64].strip()  # フラグ 7、名称 20（[0] L/R/' ' [1] F/R/' '）、品番 17、価格 6、ConstructGroup 2、' '、カラーコード 11（以降は未使用領域なので固定幅で切る）
                    frm = to = note = ''; bad_period = False
                    if src == '13':
                        frm, to, note = t[71:77].strip(), t[77:83].strip(), t[83:].strip()
                        for v in (frm, to):
                            if v and not (v.isdigit() and len(v) == 6):
                                raise ValueError(f'13.DB ブロック {k}: 適用年月が YYYYMM でない {v!r}')
                        # 月が 1〜12 でない／開始 > 終了 の期間は「期間不明」= 空欄として扱う（ADDATA 実データ T99 に '199340' が 2 行。全 672 車種を走査して他は正常。Codex 99）
                        if (frm and not 1 <= int(frm[4:6]) <= 12) or (to and not 1 <= int(to[4:6]) <= 12) or (frm and to and frm > to):
                            frm = to = ''; bad_period = True  # 期間不明の印（初度登録での絞り込み候補にしない = レビュー 104）
                    if price and not price.isdigit():  # 空欄（価格未設定）は 0、非空の不正値だけ例外
                        raise ValueError(f'{src}.DB ブロック {k}: 価格欄が数値でない {price!r}')
                    out.setdefault(ref, []).append({'flags': fl, 'name': name, 'name20': name20, 'pn': pn, 'price': int(price) if price else 0, 'cgroup': cg, 'color': color,
                                                    'src': src, 'from': frm, 'to': to, 'note': note, 'period_invalid': bad_period})
            except ValueError:
                raise
            except Exception as ex:
                raise ValueError(f'{self.car}{src}.DB の解析に失敗: {ex}') from ex
        self._r83 = out
        return out

    @staticmethod
    def _same_stem(a: str, b: str) -> bool:
        """色サフィックスだけが違う品番か（末尾 3 文字以内の違い）。トヨタ/ダイハツ '52119-B2G40-C0' ↔ '-A0'、ホンダ '71101-TTA-000ZG' ↔ '000ZF'、
        スズキ '3F116-53UA0-ZYW' ↔ 自身。'52101-B2B90-A1' と '52119-B2G40-C0'（別部品）は不一致。13.DB/83.DB の色別行は 11.DB 変種と同じ語幹で並ぶ"""
        a = (a or '').strip().upper(); b = (b or '').strip().upper()
        if not a or not b:
            return False
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n >= max(len(a), len(b)) - 3

    def variant_color_flag(self, ref: int, std_pn: str, body: Optional[str] = None, ctx: Optional[dict] = None) -> int:
        """選んだ 11.DB 変種の色別フラグ [70]。ctx（grade/fva/eva/year）があれば _std_row と同じ順位（群 → フラグ → ボディ固有 > 共通）で選んだ行が
        std_pn と同じ品番のときその行の値、そうでなければ同じ品番の行（ボディ固有行優先）の値
        （D98 0124: 共通行 89348-B2050-C0 は 1、ボディ 20 行は 0 → コグニは -C0 のまま。COLOR_D98.neo。Codex 100）"""
        b = str(body if body is not None else getattr(self, 'vehicle_body', '') or '').strip()
        b_ = int(b) if b.isdigit() else 0
        pn = self.norm_pn(std_pn or '')
        if ctx:
            y = str(ctx.get('year') or '').strip(); grp = y[-1] if y.isdigit() and int(y) else ''
            srow = self._std_row(ref, 0, ctx.get('grade', ''), ctx.get('fva', ''), set(ctx.get('eva') or ()), grp, b)
            if srow and self.norm_pn(srow.get('pn', '')) == pn:
                return int(srow.get('color_flag') or 0) & 1
        rows = [r for r in self._load_11_raw().get(ref, []) if r.get('disp') == 'K' and self.norm_pn(r['pn']) == pn and int(r.get('body') or 0) in (0, b_)]
        if not rows:
            return 0
        rows.sort(key=lambda r: 1 if (b_ and int(r.get('body') or 0) == b_) else 0, reverse=True)
        return int(rows[0].get('color_flag') or 0) & 1

    def colored_part(self, ref: int, color: str, grade: str, fva: str, eva: set, std_pn: str = '', reg_ym: str = '') -> Optional[dict]:
        """色別・期間別部品（83.DB / 13.DB）。フラグ（グレード/FVA/EVA 文字）が車両条件に合う行を優先、無条件行を次点。
        規則（コグニ実機 COLOR_D98.neo 2026-09-06 夜 と工場 NEO 6 件の突合せ tests/test_color13.py）:
          1. 車両カラーの行。無ければ色なし行（生産期間・仕様違いの品番: W66 4525 69350-52391/52401、W64 2450 87940-B1B60/B1502）
          2. 選んだ 11.DB 変種 std_pn と同じ語幹（色サフィックスを除く）の行だけ。語幹が合う行が無ければ色別品番は使わない = 11.DB のまま
             （W66 3870: 11.DB 52151-52010 に対し 13.DB 070 は 52151-52030-E1 → コグニは 52010。D82 1350 87940-B5110 も同じ）
          3. 同じ語幹で適用期間（YYYYMM）違いの行が複数残るとき、コグニは初度登録年月で絞らずダイアログで選ばせる（COLOR_D98 0182: 初度登録 2025.01 でも
             52561-B2030 (202209-202305) / B2031 (202305-) の 2 行を表示、既定選択は先頭行）。生成器は「初度登録年月 reg_ym を含む行が 1 行だけならそれ、
             それ以外はダイアログ既定の先頭行（ファイル順）」（工場 NEO 4 件中 3 件と一致: W66 4570 -C3 = 期間、D98 0182 B2031 = 期間、W66 4525 52391 = 境界月で
             2 行とも含むので先頭。W64 2450 87940-B1B60 は期間外の先頭行を担当者が選んだ例で再現しない）。仕様違い（ﾒﾂｷ・ﾊﾞﾂｸｶﾒﾗ付車 等の備考行）も同じ扱い"""
        allrows = self._load_83_raw().get(ref, [])
        if not allrows:
            return None
        rows = [r for r in allrows if color and r['color'] == color.strip()] or [r for r in allrows if not r['color']]
        if not rows:
            return None
        if std_pn:  # 83.DB でも同じ色に変種ごとの行が並ぶ（S64 3906 ZYW: 3F116-54S00-ZYW / 3F116-53UA0-ZYW → 11.DB 変種 53UA0 と同じ語幹の行、12081431.neo）
            rows = [r for r in rows if self._same_stem(r['pn'], std_pn)]
            if not rows:
                return None
        # フラグ（グレード/FVA/EVA）で車両に合う行に絞ってから期間規則を当てる（Codex 98: 期間で先に 1 行に潰すとフラグ不適合行だけが残ることがある）
        cond = [r for r in rows if r['flags'].strip() and self._flags_ok(r['flags'], grade, fva, eva, True)]
        plain = [r for r in rows if not r['flags'].strip()]
        if cond:  # 条件付き行が複数あれば _std_row と同じ優先順位（グレード > FVA > EVA）で最も具体的な行（同順位は複数残す）
            top = max(self._flags_rank(r['flags'], grade, fva) for r in cond)
            cands = [r for r in cond if self._flags_rank(r['flags'], grade, fva) == top]
        elif plain:
            cands = plain
        else:
            return None  # 条件付き行が車両条件に合わず無条件行も無ければ色別部品は使わない（11.DB 側へフォールバック）
        if len(cands) > 1 and any(r.get('from') or r.get('to') for r in cands):
            ym = str(reg_ym or '').strip()
            inside = [r for r in cands if not r.get('period_invalid') and (not r.get('from') or r['from'] <= ym) and (not r.get('to') or ym <= r['to'])] if (ym.isdigit() and len(ym) == 6) else []  # 期間不明の行は初度登録で絞る候補にしない
            cands = inside if len(inside) == 1 else cands[:1]  # 初度登録で 1 行に絞れればそれ、無理ならダイアログ既定の先頭行
        return cands[0]

    def colored_part_by_pn(self, ref: int, pn_norm: str, grade: str, fva: str, eva: set) -> Optional[dict]:
        """品番で 83.DB を引く（カラー未確定時）。同じ品番が条件違いで複数あれば colored_part と同じ条件フィルタ・具体度順位"""
        rows = [r for r in self._load_83_raw().get(ref, []) if self.norm_pn(r['pn']) == pn_norm]
        if not rows:
            return None
        cond = [r for r in rows if r['flags'].strip() and self._flags_ok(r['flags'], grade, fva, eva, True)]
        if cond:
            return max(cond, key=lambda r: self._flags_rank(r['flags'], grade, fva))
        plain = [r for r in rows if not r['flags'].strip()]
        return plain[0] if plain else None

    def _load_15_raw(self) -> dict[int, list[dict]]:
        if getattr(self, '_r15', None) is not None:
            return self._r15
        out: dict[int, list[dict]] = {}
        p = os.path.join(self.e.root, self.car[0], self.car, f'{self.car}15.DB')
        if os.path.exists(p):
            raw = open(p, 'rb').read(); H, B = 104, 19
            for bi in range((len(raw) - H) // B):
                blk = raw[H + bi * B:H + (bi + 1) * B]; ref, sub, wi = struct.unpack_from('<HHH', blk, 0)
                out.setdefault(ref, []).append({'letter': chr(blk[6]), 'cyc': chr(blk[7]).strip(), 'grp': chr(blk[8]).strip(), 'wi': wi, 'body': blk[9],
                                                'grade': blk[10:17].decode('latin1').ljust(7), 'link': blk[17:19].decode('latin1').strip(), 'sub': sub})  # [9] = ボディコード条件（0 = 共通。D98 ハイゼットカーゴ BodyCode 20: 0800 取替 B0 50 → 80）
        self._r15 = out
        return out

    @staticmethod
    def _flags_ok(fl: str, grade: str, fva: str, eva: set, allow_eva: bool) -> bool:
        g = [ch for ch in fl[0:5] if ch.strip()]; e = [ch for ch in fl[5:7] if ch.strip()]
        return (not g or grade in g) and all((ch == fva or (allow_eva and ch in eva)) for ch in e)

    @staticmethod
    def _flags_rank(fl: str, grade: str, fva: str) -> tuple:
        """一致の具体性: グレード一致 > FVA 一致 > EVA 一致 > 無条件（W66 3810: B31 80 'C'(グレード) が 110 'S'(EVA) より優先）"""
        g = [ch for ch in fl[0:5] if ch.strip()]; e = [ch for ch in fl[5:7] if ch.strip()]
        return (1 if (g and grade in g) else 0, 1 if (e and fva in e) else 0, 1 if e else 0)

    DISP_LETTER = {0: 'K', 1: 'D', 2: 'S', 6: 'S', 3: 'DS', 5: 'OH'}  # 脱着修理(3) は DS 変種行だけを見る。分解調整(5) = 11.DB の 'OH'（オーバーホール）行（J97 7600 'I7O7' 3.0h、D88 7900 'D6J6' 3.2h = 04011103/04011141）。'C' 行は点検調整(4) 用で区分レター無し = 標準なし（監査 42）

    def _std_row(self, ref: int, dcode: int, grade: str, fva: str, eva: set, grp: str, body: Optional[str] = None) -> Optional[dict]:
        """11.DB の標準行（変種）: 群 → フラグ → ボディ固有（rec[7] == 車両ボディ）> 共通（0）の順。他ボディの行は候補にしない。
        body 未指定なら vehicle_body（build 時に車両の BodyCode を入れる）。'' なら共通行だけ"""
        tok = self.DISP_LETTER.get(dcode)
        if not tok:  # 点検調整(4) など標準指数の対象外
            return None
        b = str(body if body is not None else getattr(self, 'vehicle_body', '') or '').strip()
        b_ = int(b) if b.isdigit() else 0
        rows = [r for r in self._load_11_raw().get(ref, []) if r['disp'] == tok and int(r.get('body') or 0) in (0, b_)]
        for g_ in ([grp, ''] if grp else ['']):
            cand = [r for r in rows if r['grp'] == g_ and self._flags_ok(r['flags'], grade, fva, eva, True)]
            if cand:
                cand.sort(key=lambda r: (self._flags_rank(r['flags'], grade, fva), 1 if (b_ and int(r.get('body') or 0) == b_) else 0), reverse=True)
                return cand[0]
        return None

    def _std_pick15(self, ref: int, sc: str, grade: str, fva: str, eva: set, grp: str, present: set, body: str = '', host_fallback: bool = False) -> Optional[dict]:
        r15 = self._load_15_raw()
        b_ = int(body) if str(body or '').strip().isdigit() else 0

        def _match(x: dict) -> bool:
            return x['letter'] == sc[0] and x['cyc'] == sc[1:] and (x.get('body', 0) in (0, b_))  # ボディコード条件（0 = 共通、他ボディの行は除外）

        taker_box: list = []  # 枠を取った行（_slot_taken の戻り値）。host_fallback の ChangeTotal 用にそのまま使う（Codex e21）

        def _select(es: list, present_: set, slot_check: bool) -> Optional[dict]:
            for g_ in ([grp, ''] if grp else ['']):
                eg = [x for x in es if x['grp'] == g_]
                # **この車のボディ専用行が sub_ref 付きで、その相手が見積に居ないなら、この区分は標準なし**。
                # 下の「全候補 sub 付きならそのまま採る」救済に入れてはいけない（入れると sub=4801 の行を採って 8.1 を作る）。
                # コグニ実機 2026-09-12 W90 ハイエース（ボディ 20）4800 取替: 15.DB の J3/Q4 はホスト 4600 の下に
                # 共通行 sub=4800（wi 740/70）とボディ 20 行 sub=4801（wi 900/70）。ホスト経由の候補（下の sub==ref 分岐）は
                # ボディ 20 行だけになり、明細に 4801 は無い → コグニは標準なし（Time -1）。この分岐を外すと 8.1（785,700 円）に戻る
                # （fixture cogni_W90 の ChangeTotal 830,600 ≠ 実機 985,800 で検知される）。
                # 一方、ボディ専用行が **グレード/装備フラグ** で外れるだけなら共通行へ逃げてよい
                # （同じ実機で 2700 取替: N1 のボディ 20 行は装備 P 付き、見積は P 無し → 共通行 1.9 が採られた）
                if b_:
                    # 押しのける側のボディ専用行は、車両条件（グレード/装備）に合うものだけ（Codex 指摘: 不一致行で過剰/過小除外しない）
                    _bs = [x for x in eg if x.get('body') == b_ and x['sub'] and self._flags_ok(x['grade'], grade, fva, eva, True)]
                    if _bs and all(x['sub'] not in present_ for x in _bs):
                        continue   # 次の年式群へ（無ければ None = 標準なし）
                eg2 = [x for x in eg if not x['sub'] or x['sub'] in present_]
                # sub_ref 付きしか無い区分（全候補が sub 付き: J52 0402 F の T/BDFG/C 変種。J52 6500 の V5/W5 は sub 0・link 'B4' で該当しない）はそのまま標準にする。sub 有無が混在する区分では相手不在の組合せを選ばない
                eg = eg2 or (eg if eg and all(x['sub'] for x in eg) else [])
                egf = [x for x in eg if self._flags_ok(x['grade'], grade, fva, eva, True)]
                if egf:  # 群・sub・フラグで有効な行の中で、フラグの具体性 → 車両のボディ固有行（D98 0800 B0: 共通 0.5 / ボディ 20 は 0.8）→ sub 付き の順
                    egf.sort(key=lambda x: (self._flags_rank(x['grade'], grade, fva), 1 if (b_ and x.get('body') == b_) else 0, 1 if x['sub'] else 0), reverse=True)
                    if slot_check:
                        taker = self._slot_taken(ref, sc, egf[0], grade, fva, eva, grp, b_, present_)
                        if taker:
                            taker_box.append(taker)
                            return None  # 同じ枠（同ブロック・同区分・同リンク符号）を、車両条件に一致する条件付き行を持つ別 ref が取る（コグニ実機 J87 0400 vs 0402、装備 U）
                    return egf[0]
            return None

        def _host_es() -> list:
            # 別 ref（ホスト）の下にあるその区分の行: 同じ部位ブロックで一意 → 無ければ車種ファイル全体で一意（複数なら誤認の恐れがあるので使わない）。
            # 0010 の 'C' 10 = バンパビーム 0020 の取替指数（J52 NONE_dc: 0020 ChangeTotal 2,750 + 0.1h）。cogni_H5: 0800 'TN1' の N1 は H01 ドア 2300 の下（41,800 = 37,000 + 0.6h）
            blk = self.block_of(ref)
            pres_ = set(present) | {ref}

            def _ok(x: dict) -> bool:  # 選べる行だけでホストを数える（sub 無し、sub が見積に居る、または全候補 sub 付きの区分 = _select と同じ規則。Codex e23）
                return _match(x) and x['grp'] in ('', grp)

            def _rows(h: int) -> list:
                es_ = [x for x in r15[h] if _ok(x)]
                es2 = [x for x in es_ if not x['sub'] or x['sub'] in pres_]
                return es2 or (es_ if es_ and all(x['sub'] for x in es_) else [])
            hosts = {h for h in r15 if h != ref and blk and self.block_of(h) == blk and _rows(h)}
            if not hosts:
                hosts = {h for h in r15 if h != ref and _rows(h)}
            if len(hosts) != 1:
                return []
            return [x for x in r15[next(iter(hosts))] if _match(x)]  # sub 付き行も候補（0400 の D 行は sub 450: 全候補 sub 付きなら _select がそのまま採る）

        es = [x for x in r15.get(ref, []) if _match(x)]
        if not es:  # 指数が別の ref（ホスト）の下に sub=この ref で置かれている部品（D88 センタピラー 2670 の O1/R1 は 2601 の下）
            # **ホストの下に、この車のボディ専用行があるなら、その行の sub がこの ref でなければ候補にしない**
            # （共通行の sub=この ref へ逃げない）。コグニ実機 2026-09-12 W90 ハイエース（ボディ 20）4800 取替:
            # ホスト 4600 の J3 は 共通行 sub=4800（wi 740）と ボディ 20 行 sub=4801（wi 900）。
            # コグニは標準なし（Time -1）だった = ボディ 20 行（sub 4801 ≠ 4800）が優先され、共通行は使われない。
            # 生成器は共通行を拾って J3 7.4 + Q4 0.7 = 8.1（785,700 円）を作っていた
            # ボディ専用行が共通行を押しのけるのは **同じ年式群で、車両条件（グレード/装備）にも合う** 行だけ
            # （別の年式群や装備不一致のボディ行があるだけで、今の年式群の有効な共通行まで消してはいけない。Codex 指摘）
            es = []; _alias_rows = []
            for hs in r15.values():
                by_sec: dict = {}
                for x in hs:
                    if _match(x):
                        by_sec.setdefault((x['letter'], x['cyc'], x['grp']), []).append(x)
                for grp_rows in by_sec.values():
                    bodyrows = [x for x in grp_rows if b_ and x.get('body') == b_ and self._flags_ok(x['grade'], grade, fva, eva, True)]
                    cands = bodyrows if bodyrows else grp_rows
                    es += [x for x in cands if x['sub'] == ref]
                    # ボディ専用行の sub が枝番違い（4801 ≠ 4800）で押しのけた区分。Time は -1 だが取替合計（ChangeTotal）にはこの行の指数が入る
                    # 条件: その区分に sub=この ref の共通行が実在し（= 押しのけられた）、ボディ行の sub が 4 桁ゼロ埋めで上 3 桁一致（枝番違い）。
                    # 先頭 3 桁がたまたま同じ無関係な部品や、共通行が元々無い区分は採らない（Codex 指摘）
                    if (any(int(y['sub'] or 0) == int(ref) and int(y.get('body') or 0) == 0 and self._flags_ok(y['grade'], grade, fva, eva, True) for y in grp_rows)  # 車両条件に合う共通行（body 0）が実在
                            and not any(int(y['sub'] or 0) == int(ref) for y in bodyrows)):  # かつボディ行には sub=ref が無い（= 共通行が押しのけられた。Codex 指摘 2 周目）
                        _alias_rows += [x for x in bodyrows if int(x['sub'] or 0) != int(ref) and f"{int(x['sub'] or 0):04d}"[:3] == f"{int(ref):04d}"[:3]]
            present = set(present) | {ref}
            if not es and host_fallback and _alias_rows:
                # 実機 2026-09-12 W90b（ボディ 20）4800 取替: 表示は Time -1 なのに ChangeTotal = 44,900 + 9.7h（ホスト 4600 の J3 9.0 + Q4 0.7 = ボディ 20 行 sub 4801）
                es = _alias_rows; present = set(present) | {x['sub'] for x in _alias_rows}
        if not es:
            return _select(_host_es(), present, False) if host_fallback else None  # ChangeTotal の取替標準を引くときだけホストの行
        pick = _select(es, present, True)
        if pick is None and host_fallback and taker_box:
            return taker_box[0]  # 枠を取った行の指数が取替合計の標準工賃（実機 pair_U 0400 = 43,000 + 0402 の U 行 0.4h）
        if pick is None and host_fallback:
            # 自分の行はあるが車両条件に合わない／枠を取られた区分: ChangeTotal の標準工賃は同ブロックのホスト（別 ref）の行で（コグニ実機 2026-09-08 cogni_pair_none 0402 'DE' = 57,600 + 0400 の 0.3h、cogni_pair_U 0400 = 43,000 + 0402 の U 行 0.4h）
            pick = _select(_host_es(), present, False)
        return pick

    def _slot_taken(self, ref: int, sc: str, own: dict, grade: str, fva: str, eva: set, g_: str, b_: int, present: set) -> Optional[dict]:  # g_ = 車両の年式群（grp）。戻り値 = 枠を取った行（無ければ None）
        """15.DB の枠の取り合い（コグニ実機 2026-09-08 J87、cogni_pair_U.neo / cogni_TU.neo）: 自分の行が無条件（グレード/EVA フラグ空・共通ボディ 0）で、
        同じ部位ブロックの別 ref が 同じ区分（レター+サイクル）・同じリンク符号の行を **車両条件に一致する条件付き**（グレード一致 or EVA 一致、ボディ 0 か車両ボディ）で
        持てば、標準指数はその ref のもの（自分は −1）。部品番号・価格はそのまま。
        競合行の sub は _std_pick15 と同じ規則（sub 無し or sub が見積に居る行を優先。その区分の候補が全部 sub 付きならそのまま候補 = 実機: 0402 の U 行は sub 452 が不在でも枠を取った）。
        骨格ブロック（_frame_combination）・リンク符号の無い行・ボディ固有の自行には適用しない"""
        link = own.get('link') or ''
        if not link or own.get('grade', '').strip() or int(own.get('body') or 0) != 0 or self.block_of(ref) in self.FRAME_BLOCKS:
            return None
        blk = self.block_of(ref)
        if not blk:
            return None
        for other, rows in self._load_15_raw().items():
            if other == ref or self.block_of(other) != blk:
                continue
            sec_all = [x for x in rows if x['letter'] == sc[0] and x['cyc'] == sc[1:] and x.get('body', 0) in (0, b_)]
            if not sec_all:
                continue
            for cg in ([g_, ''] if g_ else ['']):  # 競合 ref も _std_pick15 と同じ順（年式群 → 共通）で「その ref が選ぶ 1 行」を決める
                sec = [x for x in sec_all if x['grp'] == cg]
                sec2 = [x for x in sec if not x['sub'] or x['sub'] in present]
                sec = sec2 or (sec if sec and all(x['sub'] for x in sec) else [])
                egf = [x for x in sec if self._flags_ok(x['grade'], grade, fva, eva, True)]
                if not egf:
                    continue
                egf.sort(key=lambda x: (self._flags_rank(x['grade'], grade, fva), 1 if (b_ and x.get('body') == b_) else 0, 1 if x['sub'] else 0), reverse=True)
                pick = egf[0]
                if (pick.get('link') or '') == link and pick.get('grade', '').strip():
                    return pick  # 競合 ref が選ぶ行が同じ枠の条件付き行 → 枠はその ref のもの
                break  # 競合 ref の選択は決まった（無条件行や別リンク）: この ref は枠を取らない
        return None

    def _active_links(self, present_rows, ref: int, grade: str, fva: str, eva: set, grp: str, body: str = '') -> frozenset:
        """見積中の他部品（非骨格ブロック、取替/脱着/脱着板金）が実際に使う 15.DB 行のリンク符号の集合（吸収の偶奇ペア判定用。コグニ実機 2026-09-08 H31）"""
        key = ('al', tuple((int(p), int(d)) for p, d in present_rows), ref, grade, fva, frozenset(eva or ()), grp, str(body or ''))
        cache = self.__dict__.setdefault('_al_cache', {})
        if key in cache:
            return cache[key]
        r15 = self._load_15_raw(); links: set = set()
        present_all = set(int(p) for p, _ in present_rows)
        for pref, pd in present_rows:
            if int(pref) == ref or pd not in (0, 1, 3) or self.block_of(int(pref)) in self.FRAME_BLOCKS:
                continue
            prow = self._std_row(int(pref), 1 if pd == 3 else pd, grade, fva, eva, grp, body)
            if not prow or not prow['secs']:
                continue
            own = {x['letter'] + x['cyc'] for x in r15.get(int(pref), [])} | {x['letter'] + x['cyc'] for hs in r15.values() for x in hs if x['sub'] == int(pref)}
            for sc in re.findall(r'[A-Z]\d?', prow['secs']):
                if sc in own:
                    pk = self._std_pick15(int(pref), sc, grade, fva, eva, grp, present_all, body, False)
                    if pk and pk.get('link'):
                        links.add(pk['link'])
        if len(cache) > 512:
            cache.clear()
        cache[key] = frozenset(links)
        return cache[key]

    @staticmethod
    def _even_partner(link: str) -> str:
        """奇数リンク X(2k+1) のペア X(2k)。偶数・無効なら ''"""
        return (link[0] + str(int(link[1]) - 1)) if (len(link) == 2 and link[1].isdigit() and int(link[1]) % 2 == 1) else ''

    def _frame_combination(self, members, grade: str, fva: str, eva: set, grp: str, body: str = '', touched: Optional[set] = None, own_all: bool = False, ext_links: frozenset = frozenset()) -> dict[int, int]:
        """骨格ブロックの組合せ指数（コグニ実機 J52 2026-09-06: 1400/1410/1420/1430/1434/1500/1512/1600/1603/1611/1612/1902/1904/1950 の 23 通りで一致。値は W/S 頁明細の画面読取、保存 NEO は FRAME_p7/p8）。
        members = 見積中の (ref, DisposalCode)。戻り値 {ref: 指数×100}（行が無い／全て相手に集約された部品は 0 = 空欄）。touched を渡すと 15.DB 行を 1 つでも指した部品の ref を追加する
        規則:
          1. 各部品 P の 11.DB 行の区分（レター+サイクル）に一致する 15.DB 行を探す。同じ部位ブロックの host（または自部品を host/sub に持つ行）があればその中から、
             無ければ車種ファイル全体から（D88 1910 の Y9 は H10 ピラー 2200 の下）。候補が複数なら 群→フラグ→ボディ固有→自部品の host/sub→同ブロック→見積中の host/sub の順に 1 行
          2. 行の集約先 = ホスト（行が置かれている ref）が見積に居ればホスト、居なければ sub が見積に居れば sub、どちらも居なければ捨てる（自部品に戻ることは無い:
             コグニ実機 FRAME_p9 2026-09-06 夕: 1500 単独・1503 単独・1511 単独は表示空欄。1400+1500 = 1400 に I0 400 のみ（J0 はホスト 1410 不在で消える）、1410+1500 = 1410 に 4.5）。
             own_all=True のときは「自区分の全行を自部品に」（ChangeTotal の単独値: 1500 = I0+J0 = 4.5h、1503 = K0+L0 = 2.7h、1511 = M0+N0 = 3.4h）
          3. 同じ行を複数の部品が指しても 1 回だけ（1410 と 1420 の共有行 B0）
          4. リンク記号 Xn の奇数行は、同じ系列の偶数 X(n-1) の行が有効なら 0（A0→A1、A2→A3、A6→A7、B0→B1、B2→B3、C8→C9。連鎖ではなくペア: A7 が有効でも A8 は生きる）
        """
        key = (tuple((int(p), int(d)) for p, d in members), grade, fva, frozenset(eva or ()), grp, str(body or ''), own_all, ext_links)  # 帰属先の決定が members 順に依存するので順序を保ったキー
        cache = self.__dict__.setdefault('_frame_cache', {})
        if key in cache and touched is None:
            return dict(cache[key])  # 同じ見積の全行から同じ members で呼ばれるので結果を再利用（250 行の見積で 500 回呼ばれる）
        r15 = self._load_15_raw()
        by_sec = self.__dict__.get('_r15_by_sec')
        if by_sec is None:  # 区分（レター+サイクル）→ [(host, 行)] の索引
            by_sec = {}
            for h, xs in r15.items():
                for x in xs:
                    by_sec.setdefault(x['letter'] + x['cyc'], []).append((h, x))
            self._r15_by_sec = by_sec
        present = {int(p) for p, _ in members}
        b_ = int(body) if str(body or '').strip().isdigit() else 0
        # 区分（レター+サイクル）は車種ファイル全体でほぼ一意（全 1,230 車種 22.3 万キーのうち複数 host にまたがるのは 0.8%）。
        # 行は別ブロックの host の下にも置かれる（D88 1910 カウルトップサイドパネルの Y9 は H10 ピラー 2200 の下、sub 0）ので全体から探し、同ブロック・自部品を優先する
        activ: dict[tuple, dict] = {}
        for p, d in members:
            prow = self._std_row(int(p), int(d), grade, fva, eva, grp, body)  # ボディ条件（他ボディの 11.DB 行を除外）
            if not prow:
                continue
            for sc in re.findall(r'[A-Z]\d?', prow['secs']):
                blk_p = self.block_of(int(p))
                cands_all = [(h, x) for h, x in by_sec.get(sc, []) if x.get('body', 0) in (0, b_)]
                # 同じ部位ブロックの host（または自部品を host/sub に持つ行）を優先し、無いときだけ全体から探す（同じ区分が別ブロックにもある 0.8% で誤選択しないため）
                cands = [(h, x) for h, x in cands_all if (blk_p and self.block_of(h) == blk_p) or h == int(p) or x['sub'] == int(p)] or cands_all  # 12.DB ブロック不明（''）の部品は自部品の行だけを同ブロック扱いにする
                pick = None
                for g_ in ([grp, ''] if grp else ['']):
                    eg = [(h, x) for h, x in cands if x['grp'] == g_ and self._flags_ok(x['grade'], grade, fva, eva, True)]
                    if eg:
                        eg.sort(key=lambda hx: (self._flags_rank(hx[1]['grade'], grade, fva), 1 if (b_ and hx[1].get('body') == b_) else 0,
                                                1 if (hx[0] == p or hx[1]['sub'] == p) else 0, 1 if (blk_p and self.block_of(hx[0]) == blk_p) else 0,
                                                1 if hx[0] in present else 0, 1 if hx[1]['sub'] in present else 0), reverse=True)
                        pick = eg[0]
                        break
                if not pick:
                    continue
                h, x = pick
                if touched is not None:
                    touched.add(int(p))
                a_ = activ.setdefault((h, sc, id(x)), {'host': h, 'row': x, 'sc': sc, 'by': []})
                if int(p) not in a_['by']:
                    a_['by'].append(int(p))  # 見積の行順（members 順）を保つ = 帰属先の決定を決定的にする
        for a in activ.values():
            h, x = a['host'], a['row']
            if own_all:
                a['target'] = a['by'][0]  # 単独値: 自区分の行を全部自分に
            else:
                a['target'] = h if h in present else (x['sub'] if (x['sub'] and x['sub'] in present) else None)
        applied = {(self.block_of(a['host']), a['row']['link']) for a in activ.values() if a['target'] is not None}  # リンク記号は部位ブロック内で閉じる（W66 X30 の C7 を他ブロックの C6 で消さない）

        def _suppressed(blk: str, link: str) -> bool:
            ev = self._even_partner(link)
            return bool(ev) and ((blk, ev) in applied or ev in ext_links)  # 同ブロックの偶数行、または見積中の非骨格部品が使う偶数リンク（H31: 2700 の T1/F0 → 4810 の U3/F1）
        tot: dict[int, int] = {}
        for a in activ.values():
            if a['target'] is None or _suppressed(self.block_of(a['host']), a['row']['link']):
                continue
            tot[a['target']] = tot.get(a['target'], 0) + a['row']['wi']
        if len(cache) > 512:
            cache.clear()
        cache[key] = dict(tot)
        return tot

    FRAME_BLOCKS = ('A25', 'A35', 'P05', 'X20', 'X30')  # 骨格系ブロックの部品自身には連動加算を行わない（組合せ規則が別）。相手側が骨格でも加算はする（D88 ピラー 2200 = A1 + A25 相手の Y9 がコグニ実値）

    def cogni_standard(self, ref: int, dcode: int, grade: str, fva: str, eva: set, year: str, present_rows, body: str = '') -> Optional[dict]:
        """コグニが車両条件から選ぶ標準指数。present_rows = 見積中の (ref, DisposalCode) 一覧（連動加算に使う）
        単一部品 58/75 ＋ 連動加算（相手部品の区分がこの ref の 15.DB にある: バンパビーム C・ドアロワガーニッシュ E1 ／ sub 付き区分でリンク記号の系列と偶奇が同じ: 両側 H）で 67/75
        戻り値 {'time', 'secs', 'prov', 'pn'} または None"""
        present_rows = list(present_rows or []); present = set(p[0] for p in present_rows)
        present_same = set(p[0] for p in present_rows if p[1] == dcode)  # 同じ修理方法で見積に居る相手（sub_ref 付き区分・両側加算の判定用）
        grp = str(year).strip()[-1] if str(year).strip().isdigit() and int(year) else ''  # 年式群 = YearCode の下 1 桁（'01' → '1'、'10' → '0'。'00' は群なし）
        row = self._std_row(ref, dcode, grade, fva, eva, grp, body)  # ボディ条件（Codex 96: cogni_standard の body を _std_row にも渡す）
        if not row or not row['secs']:
            return None
        if self.block_of(ref) in self.FRAME_BLOCKS:
            # 骨格ブロック: 15.DB 行の置き場所（host）と集約先（sub）で指数が相手部品に集約される（§11-7c）。単独値（base）は自部品だけの組合せ
            touched: set = set()
            tot_one = self._frame_combination([(ref, dcode)], grade, fva, eva, grp, body, touched, own_all=True)  # 単独値 = 自区分の全行（ChangeTotal 用。FRAME_p9 1503 = K0 230 + L0 40 = 2.7h）
            if ref not in touched:
                return None  # 区分に一致する 15.DB 行が 1 つも無い（標準なし。相手に集約された「吸収」とは区別する）
            tot_all = self._frame_combination([(p, d) for p, d in present_rows if d == dcode] or [(ref, dcode)], grade, fva, eva, grp, body, ext_links=self._active_links(present_rows, ref, grade, fva, eva, grp, body))
            t = tot_all.get(ref, 0); b = tot_one.get(ref, 0)
            return {'time': t / 100.0, 'base': b / 100.0, 'secs': row['secs'], 'prov': row['prov'], 'pn': row['pn'], 'absorbed': t == 0}
        r15_all = self._load_15_raw()

        def _own_secs(rf: int) -> set:  # その部品の 15.DB にある区分（sub=自分 で他 ref の下に置かれた行も含む）
            return {x['letter'] + x['cyc'] for x in r15_all.get(rf, [])} | {x['letter'] + x['cyc'] for hs in r15_all.values() for x in hs if x['sub'] == rf}
        secs_list = re.findall(r'[A-Z]\d?', row['secs'])
        own_secs = _own_secs(ref)
        tot = 0; used: list[str] = []; used_hidden: list[str] = []; links = set(); own_picks: list = []; foreign = 0; resolved = False
        for sc in secs_list:
            if sc in own_secs:
                pick = self._std_pick15(ref, sc, grade, fva, eva, grp, present_same, body, False)
                if pick:
                    tot += pick['wi']; used.append(sc); links.add(pick['link']); own_picks.append((sc, pick)); resolved = True
                else:
                    pb = self._std_pick15(ref, sc, grade, fva, eva, grp, present_same, body, True)  # 車両条件に合わない／枠を取られた区分: 表示指数は無いが取替合計の標準工賃は同ブロックのホストの行（実機 pair_U 0400 = 43,000 + 0.4h、pair_none 0402 = 57,600 + 0.3h）
                    if pb:
                        foreign += pb['wi']; resolved = True
            else:  # 自分の 15.DB に無い区分（他部品の下にある: 0800 'TN1' の N1 = 2300、0020 'C' = 0010）。表示指数には入れず、取替合計（ChangeTotal）の標準工賃にだけ入れる（実機 H5/H6）
                pick = self._std_pick15(ref, sc, grade, fva, eva, grp, present_same, body, True)
                if pick:
                    foreign += pick['wi']; resolved = True
        if not resolved:
            return None  # どこにも指数の無い区分だけ（工場 NEO 6001 'C5': WorkCode 空）
        base = tot + foreign  # ChangeTotal の標準工賃 = 連動加算前・吸収前の自区分の全区分（NEW2 0010 = 50,700 + 1.3h、H5 0800 = 37,000 + 0.6h、H3 吸収された 2450 = 58,500 + 0.2h）
        if self.block_of(ref) not in self.FRAME_BLOCKS and own_picks:
            # 吸収: 自分の行のリンク符号が同ブロックの見積中の他部品（取替/脱着）の区分に一致 → その作業は相手に含まれる（実機 H3: 2300 取替 E1 が居ると 2310 G1(link E1)・2450 I1(link E1) は指数なし。H7: 2300 脱着 D1 では吸収されない）
            blk = self.block_of(ref); other_secs: set = set()
            for pref, pd in present_rows:
                if pref == ref or pd not in (0, 1) or self.block_of(pref) != blk or self.block_of(pref) in self.FRAME_BLOCKS:
                    continue
                prow_ = self._std_row(pref, pd, grade, fva, eva, grp, body)
                if prow_:
                    other_secs |= set(re.findall(r'[A-Z]\d?', prow_['secs']))
            ext = self._active_links(present_rows, ref, grade, fva, eva, grp, body)
            for sc, pick in own_picks:
                ev = self._even_partner(pick['link'] or '')
                if pick['link'] and sc in used and (pick['link'] in other_secs or (ev and ev in ext)):
                    tot -= pick['wi']; used.remove(sc); links.discard(pick['link'])  # 偶奇ペア: 見積中の他部品が使う行のリンク X(2k) が有効なら X(2k+1) の行は吸収（ブロックをまたぐ。H31）
        appended: list[str] = []
        if self.block_of(ref) not in self.FRAME_BLOCKS and dcode in (0, 1) and used:
            # 連動 1: 見積中の他部品 P の区分のうち、P 自身の 15.DB に無く この ref の 15.DB にある区分を加算（実機 2026-09-05 NEW2 0010+0020 脱着板金 → 1.4、2026-09-08 H1 0010 取替+0020 脱着 → 0.9、H6 0010 脱着+0020 取替 → 0.5、H2 2300 取替+0800 取替 → 2.1）。
            # P が自分の 15.DB を持つ部品（0800 の T/S）なら、この ref が取替のときだけ（H7: 2300 脱着 + 0800 脱着 は 0.6 のまま）。相手が骨格ブロックでも加算する（D88 ピラー 2200 = A1 620 + A25 相手の Y9 70）
            for pref, pd in present_rows:
                if pref == ref or pd not in (0, 1, 3):
                    continue
                p_has15 = bool(r15_all.get(pref))
                if p_has15 and dcode != 0:
                    continue
                prow = self._std_row(pref, 1 if pd == 3 else pd, grade, fva, eva, grp, body)  # 脱着板金の相手は脱着(D) 行の区分で加算
                p_own = _own_secs(pref) if p_has15 else set()
                for sc in re.findall(r'[A-Z]\d?', prow['secs']) if prow else []:
                    if sc in p_own or sc in secs_list or sc in used or sc in used_hidden or sc in appended:
                        continue  # 自分の 11.DB 区分（吸収で used から外れた区分も含む）は WorkCode に重複させない（Codex e22）
                    pick = self._std_pick15(ref, sc, grade, fva, eva, grp, present_same | {pref}, body)  # 連動相手は修理方法によらず sub 判定で「居る」扱い（Codex e20）
                    if pick and sc in own_secs:
                        tot += pick['wi']; used_hidden.append(sc)  # 連動加算は WorkCode に出ない（実機 2026-09-08 H15: 2300 取替 + 2344 取替 → 'E1'、H16: 0010 取替 + 0020 取替 → 'B'。以前の 'BC' 説は撤回）
            # 連動 2: sub が見積に居るエントリのうち、リンク記号の系列と偶奇が自区分と同じもの（両側の加算）
            def _par(link: str):
                return (link[0], int(link[1]) % 2) if len(link) == 2 and link[1].isdigit() else None
            own_par = set(p for p in (_par(l) for l in links) if p)
            cand_secs: list[str] = []
            for e in r15_all.get(ref, []):
                sc = e['letter'] + e['cyc']
                if e['sub'] and e['sub'] in present_same and sc not in secs_list and sc not in used and sc not in used_hidden and sc not in appended and sc not in cand_secs and e['grp'] in (grp, '') and _par(e['link']) in own_par:
                    cand_secs.append(sc)
            for sc in cand_secs:  # 区分ごとに候補を集め、群優先・グレード/FVA/EVA の優先順位で 1 件に絞ってから加算
                pick = self._std_pick15(ref, sc, grade, fva, eva, grp, present_same, body)
                if pick and pick['sub'] and sc not in used and _par(pick['link']) in own_par:  # 絞り込み後の行でもリンク系列・偶奇を再確認
                    tot += pick['wi']; appended.append(sc)
        secs_out = row['secs'] + ''.join(appended)  # WorkCode は 11.DB の区分文字列そのまま（自分の 15.DB に無い区分も出る: 実機 H5 'TN1'、H6 'C'、工場 NEO 'T3V3'）
        if not used:
            return {'time': 0.0, 'base': base / 100.0, 'secs': secs_out, 'prov': row['prov'], 'pn': row['pn'], 'absorbed': True}  # 表示指数なし（相手に吸収 / 区分が他部品の下 / 枠を取られた）。WorkCode は残る
        return {'time': tot / 100.0, 'base': base / 100.0, 'secs': secs_out, 'prov': row['prov'], 'pn': row['pn']}


# ======================================================================
class NeoBuilder:
    def __init__(self, addata_root: Optional[str] = None, template_path: Optional[str] = None):
        self.resolver = AddataVehicleResolver(addata_root)
        self.engine = AddataSearchEngine(self.resolver.root)
        env_tpl = os.environ.get('NEO_TEMPLATE') or ''
        self.silent_errors: list[str] = []  # 「失敗しても続ける」箇所の理由。report['silent_errors'] に載せて run_case が ★ で出す
        self._sil_call: Optional[list[str]] = None  # build_rows 1 回分の控え（直呼びでも、2 回目の同じ失敗を落とさない）
        self.template_path = template_path or (env_tpl if os.path.isfile(env_tpl) else '') or next(
            (p_ for p_ in (os.path.join(HERE, 'reference', 'template.neo'), os.path.join(HERE, 'reference', 'template_04011103.neo'), os.path.join(ROOT, 'サンプル見積PDF', 'sample.csv')) if os.path.isfile(p_)),
            os.path.join(HERE, 'reference', 'template.neo'))  # 雛形 NEO: 環境変数 → reference/template.neo（生成器で作った顧客情報の無い雛形）→ 旧雛形 → サンプル

    def _note_silent(self, where: str, ex, what: str) -> None:
        """失敗しても続ける箇所の理由を控える。控えるだけだと誰も読まないので report に載せる"""
        msg = f'{where}: {type(ex).__name__}: {ex} —— {what}'
        for buf in (self.silent_errors, self._sil_call):
            if buf is not None and msg not in buf:
                buf.append(msg)

    # ------------------------------------------------------------ 車両
    def resolve_vehicle(self, v: dict, hints: Optional[dict] = None) -> dict:
        return self.resolver.resolve(model_code=v.get('model_code', ''), serial_no=v.get('serial_no', ''),
                                     desig=v.get('desig', ''), category=v.get('category', ''),
                                     reg_date=v.get('reg_date', ''), color_code=v.get('color_code', ''),
                                     hints=hints or {})

    # ------------------------------------------------------------ 明細
    GENERIC_MODELS = {'Z10': ('乗用車', '10', '03'), 'Z20': ('１ＢＯＸ', '10', '10'), 'Z30': ('トラック', '10', '21')}
    MAKER_NAMES = {'A': '三菱', 'B': 'ダイハツ', 'C': 'スバル', 'D': 'ホンダ', 'E': 'いすゞ', 'F': 'マツダ', 'G': '日産', 'H': 'スズキ', 'I': 'トヨタ', 'J': 'フォルクスワーゲン'}

    def dataup_date(self, car_code: str) -> str:
        """COM.CAB の DATAUP.DB（XOR 0xff の CSV 'C10,201305'）から車種データ更新年月を引く。無ければ ''（コグニも '' を書く）"""
        if not car_code:
            return ''
        _ver = com_tables.version(self.resolver.root)
        if not hasattr(self, '_dataup') or getattr(self, '_dataup_ver', None) != _ver or getattr(self, '_dataup_src', None) != 'cache':  # ADDATA（COM.CAB）が更新されたら読み直す。CAB の展開から読めていないときも毎回やり直す（予備で固定しない。Codex 指摘）
            self._dataup = {}; self._dataup_ver = _ver
            for cand in (com_tables.com_path(self.resolver.root, 'DATAUP.DB'),):  # 使用中の ADDATA の COM.CAB（展開キャッシュ）→ 同梱の予備。毎月の ADDATA 更新で変わる表（2026-09-12）
                self._dataup_src = com_tables._src().get('DATAUP.DB')
                if os.path.exists(cand):
                    try:
                        t = bytes(x ^ 0xFF for x in open(cand, 'rb').read()).decode('cp932', 'replace')
                        for l in t.splitlines():
                            if ',' in l:
                                k, v = l.split(',', 1); self._dataup[k.strip()] = v.strip()
                    except Exception:
                        pass
                    break
        if getattr(self, '_dataup_src', None):  # 2 回目以降のビルドでも、どこから読んだ表かを記録し直す（予備なら ★）
            com_tables._src()['DATAUP.DB'] = self._dataup_src
        return self._dataup.get(str(car_code).upper(), '')

    def generic_vehicle(self, v: dict) -> dict:
        """コグニ非収録車（輸入車等）: メーカー→車名「汎用」→モデル（乗用車/1BOX/トラック）で作成した見積と同形の Car/CarSearch。
        コグニ実機で作成した VOLVO_gen.neo（2026-09-04）を正とする: Extension=1, YearCode 00, BodyCode 10, GradeCode A, FVACode T,
        SearchMethod=1（メーカーから検索）, 車名・エンジン名・カラーコードは手入力"""
        code = (v.get('car_code') or 'Z10').upper(); maker = (v.get('maker_code') or 'I').upper()
        model, body, img = self.GENERIC_MODELS.get(code, ('乗用車', '10', '03'))
        reg = v.get('reg_date', '')
        try:
            ym = parse_reg_date(reg) if reg else None
            reg_ymd = f'{ym[0]:04d}{ym[1]:02d}00' if ym else ''
        except Exception:
            reg_ymd = ''
        car = {'MakerCode': maker, 'CarCode': code, 'YearCode': '00', 'BodyCode': body, 'GradeCode': 'A', 'FVACode': 'T', 'BodyImageCode': img,
               'LBaseCode': '00', 'SBaseCode': '00', 'CarName': '', 'CarNameByUser': v.get('car_name', ''), 'FVAName': '', 'FVANameByUser': v.get('engine', ''),
               'ColorCodeFlag': 1 if v.get('color_code') else 0, 'ColorCode': v.get('color_code', ''), 'ColorName': '', 'ColorRGB1': '',
               'CarFormCode': '', 'FormCode1': '', 'FormCode2': '', 'FinishCode': '', 'Extension': 1, 'PartsPriceDate': '',
               'MakerName': self.MAKER_NAMES.get(maker, ''), 'CarNameName': '汎用', 'ModelName': model, '_generic': True,
               'ps_CarMouldNo': v.get('desig', ''), 'ps_CarKindNo': v.get('category', ''), 'ps_CarSerialNo': v.get('serial_no', ''), 'ps_CarRegDate': reg_ymd,
               'options_available': {}, 'four_wd': False}
        return {'confidence': 'generic', 'best': None, 'neo_car': car, 'candidates': [], 'evidence': [f'コグニ非収録車 → 汎用車種 {code}（{self.MAKER_NAMES.get(maker, maker)} 汎用 {model}）'], 'inputs': v}

    def build_rows(self, items: list[dict], car_code: str, labor_rate: Optional[int] = None, index_policy: str = 'auto') -> tuple[list[dict], dict]:
        if labor_rate is not None and int(labor_rate) < 0:  # 負のレートは工賃を負にする（境界条件テスト）
            raise ValueError(f'レバーレートが負（{labor_rate}）。estimate.labor_rate を確認する')
        self._sil_call = []  # この呼び出し分だけを stats に載せる（直呼びで前回分を引き継がず、2 回目の同じ失敗も落とさない。Codex 指摘）
        parts = AddataParts(self.engine, car_code)
        parts.vehicle_body = str((getattr(self, '_row_ctx', None) or {}).get('body', '') or '')  # 11.DB 変種のボディ条件（_std_row / variant）
        self._blocks17 = parts.blocks
        self._last_parts = parts
        try:  # 20.DB 塗装パネルの集合（板金行の DamageRank 既定値 'A' の判定に使う）
            paint_codes = set(str(d.get('code')) for d in PaintIndex(self.engine.root, car_code)._load_20())
        except Exception as _e20c:  # noqa: BLE001  20.DB が無い車種はある。黙って空にすると板金行の既定ランクが変わる
            paint_codes = set()
            self._note_silent('20.DB（塗装パネル一覧）の読み込み', _e20c, "板金行の DamageRank 既定値が 'A' にならない")
        # 1) 全行 ref 決定
        work = []
        ctx_block = ''
        for it in items:
            # 真偽値欄はここで 1 回だけ正規化する。以降は正規化済みの値を見るので、
            # 後段に生の `it.get('manual')` が残っていても文字列 "false" で挙動が変わらない（Codex 指摘）
            it = dict(it)
            for _bk in ('manual', 'reserve'):  # recycle は真偽値ではなくリサイクル部品の情報（dict）
                if _bk in it:
                    it[_bk] = _flag(it[_bk], f'items[].{_bk}')
            if it.get('manual'):  # 見積書の名称・品番をそのまま使う（照合しない）
                ref, why = None, '手入力指定'
            else:
                _pr = _money(it.get('parts_price') if it.get('parts_price') is not None else it.get('price'),
                             f"明細 '{str(it.get('name') or it.get('code') or '')[:20]}' の部品代（price）")
                _q = max(1, _money(it.get('qty'), f"明細 '{str(it.get('name') or it.get('code') or '')[:20]}' の数量（qty）") or 1)
                ref, why = parts.find_ref(_code4(it.get('code')), str(it.get('parts_no') or ''), str(it.get('name') or ''), context_block=ctx_block, price=(int(_pr) // _q if _pr else None), qty=(_q if _q > 1 else None), year=(getattr(self, '_row_ctx', None) or {}).get('year', ''))
            if ref is not None:
                ctx_block = parts.block_of(ref) or ctx_block
            work.append({'item': it, 'ref': ref, 'why': why})
        # 2) レバーレート推定（工賃 ÷ 指数 の最頻値）
        rate_votes = Counter()
        idx_pairs = []  # (指数, 印字工賃): 見積書に指数が印字された行。候補レートごとに「指数×レートを丸め単位で丸めると印字工賃になる行数」で選ぶ（100 円丸めの工場でも 11,200 に誤らない）
        for w in work:
            wage = int(w['item'].get('wage') or 0)
            if wage > 0 and w['item'].get('index'):  # 見積書に指数が印字されていれば 工賃÷指数 を優先（非コグニ書式で標準指数とずれる行が多いため）
                idx_pairs.append((float(w['item']['index']), wage))
                continue
            if w['ref'] is None or wage <= 0:
                continue
            for ent in parts.wage_entries(w['ref']):
                wi = ent.get('wi') or 0
                if wi > 0:
                    r = round(wage / (wi / 100.0) / 100) * 100
                    if 4000 <= r <= 20000:
                        rate_votes[r] += 1
        if idx_pairs:
            cands = {int(round(w_ / t_ / 10)) * 10 for t_, w_ in idx_pairs if t_ > 0 and 4000 <= w_ / t_ <= 20000}
            for t_, w_ in idx_pairs:  # 丸め後の印字工賃から逆算できるレート範囲（10 円 / 100 円丸め）も候補に（100 円丸めの行だけだと実単価が w/t に現れない）
                if t_ <= 0:
                    continue
                for u_ in (10, 100):
                    lo_, hi_ = (w_ - u_ / 2.0) / t_, (w_ + u_ / 2.0) / t_
                    c_ = int(lo_ // 10) * 10; n_ = 0
                    while c_ <= hi_ and n_ < 400:
                        if c_ >= lo_ and 4000 <= c_ <= 20000:
                            cands.add(c_)
                        c_ += 10; n_ += 1
            cands = sorted(cands)
            if cands:
                best = max(cands, key=lambda c: (sum(1 for t_, w_ in idx_pairs if r10_even(t_ * c) == w_), -abs(c - 10000)))
                rate_votes[best] += 2 * len(idx_pairs)

        rate = labor_rate; rate_assumed = False
        if not rate:
            top = rate_votes.most_common(4)
            rate = top[0][0] if top else 7280
            rate_assumed = not top  # 見積に labor_rate が無く、工賃÷指数からも決められない: 7,280 円は仮定（run_case が ★ で知らせる。黙って通さない）
            # 指数に「脱着/取替」2 系統がある部品では 2 倍のレートが誤投票されやすい → 半分のレートに十分な票があればそちら
            for r_, n_ in top[1:]:
                if abs(r_ * 2 - rate) <= 200 and n_ >= max(2, top[0][1] * 0.4):
                    rate = r_; break
        # 3) 行組み立て
        rows, evidence, opt_pos, opt_neg = [], [], Counter(), Counter()
        for i, w in enumerate(work):
            it, ref = w['item'], w['ref']
            method = (it.get('method') or '').strip()
            _qraw = it.get('qty')
            qty = int(_qraw) if (_qraw is not None and str(_qraw).strip() != '') else 1
            if qty <= 0:  # 数量 0 / 負を黙って 1 にしない（転記ミスがそのまま通ると金額が合わない）
                raise ValueError(f"数量は 1 以上（{it.get('name') or it.get('code')}: qty={it.get('qty')!r}）。行を消したいなら items から外す")
            _nm = str(it.get('name') or it.get('code') or '(名称なし)')[:20]
            pprice = _money(it.get('parts_price') if it.get('parts_price') is not None else it.get('price'), f"明細 '{_nm}' の部品代（price）")
            wage = _money(it.get('wage'), f"明細 '{_nm}' の工賃（wage）")
            _m = unicodedata.normalize('NFKC', method).replace('鈑', '板').replace(' ', '')
            free_method = ''   # 部品コードも品番も指数も無い手入力の作業行だけは、印字の修理方法をそのまま書ける
            if method and method not in DISPOSAL and _m not in DISPOSAL:  # 未知の修理方法を黙って取替にしない（'脱着修正' 等の表記ゆれ・転記ミスを止める）
                # 実機 NEO の作業区分は自由文字列（DisposalCode -1 で '施工' '作業' '再発行' '充填' '一部脱着' '再封印'。
                # 2026-09-19 に実案件 NEO 300 本で確認）。手入力の作業行に限り、印字の語を写す（judgment_rules 10-32）
                if not str(it.get('code') or '').strip() and not str(it.get('parts_no') or '').strip() and not it.get('index'):
                    free_method = _fit(hw(_m), 8)
                else:
                    raise ValueError(f"修理方法 '{method}' が不明（{it.get('name') or it.get('code')}）。{sorted(set(DISPOSAL))} のいずれかにするか、手入力行なら method を空にする")
            dcode = DISPOSAL.get(method, DISPOSAL.get(_m, 0 if pprice > 0 else (1 if wage > 0 else 0)))  # 正規化した名称（'脱着　板金' 等）でも引く
            if method == '部品':
                dcode = 0
            disp_name = _m if (_m in ('取替', '脱着', '修理', '脱着修理', '脱着板金', '点検', '調整', '点検調整', '分解調整', '板金') and DISPOSAL.get(_m, dcode) == dcode) else DISPOSAL_NAME.get(dcode, method)  # W/S の名称をそのまま（NEW2: 脱着板金/脱着修理、点検/調整）。別名（部品・交換・取付 等）は既定名
            if free_method:   # コグニに無い修理方法（再封印 など）は手入力の作業行として印字の語を写す
                dcode = -1; disp_name = free_method
            if it.get('manual') and not _m.strip():  # 修理方法が空欄の手入力行（塗装費用・産廃・材料代を明細に手入力する工場）: コグニ実機 2026-09-08 exp_manual.neo = DisposalCode -1・名称欄空
                dcode = -1; disp_name = ''
            std_pn, std_price, std_name, block, work_code, wi_used, sec_used = '', -1, '', '', '', -1, ''
            cgroup = ''; color_name = ''; cp = None; no_std_price = False
            if ref is not None:
                var, others = parts.variant(ref, it.get('parts_no', ''), getattr(self, '_row_ctx', None), dcode)
                if var:
                    std_pn, std_price = str(var['parts_no']), int(var['price'] or 0)
                _ctx = getattr(self, '_row_ctx', None) or {}
                if dcode == 1 and parts._std_row(ref, 1, _ctx.get('grade', ''), _ctx.get('fva', ''), set(_ctx.get('eva') or ()), (str(_ctx.get('year') or '').strip()[-1:] if str(_ctx.get('year') or '').strip().isdigit() and int(_ctx.get('year') or 0) else ''), _ctx.get('body')) is None:
                    std_pn, std_price, no_std_price = '', -1, True  # 現在車両に適用できる脱着(D) 変種の無い部品の脱着行（Codex e15: 別ボディ/別条件の D 行に引っ張られない）: コグニは標準品番・標準価格を空にする（実機 2026-09-08 cogni_frame_F2 1410）。取替(K) 行の品番を流用しない
                ctx = getattr(self, '_row_ctx', None) or {}
                # 色別部品（<car>83.DB）: カラーコードが確定していて 11.DB の変種に色別フラグが立つ部品は、コグニは色に一致する品番・価格・名称 '(ﾄｿｳｽﾞﾐ)' を使う（NEW2 実機 2026-09-05）。
                # 見積書の品番が 83.DB の色別品番に一致するときもその行を正とする（N-ONE FAX の 71101-T4G-N00ZG）
                pn_in = parts.norm_pn(it.get('parts_no', '') or '')
                if pn_in and dcode == 0:  # 見積書の品番が 83.DB の色別品番なら、その行（＝見積を作った側が選んだ色）を正にする
                    cp = parts.colored_part_by_pn(ref, pn_in, ctx.get('grade', ''), ctx.get('fva', ''), set(ctx.get('eva') or ()))
                    if cp and ctx.get('color') and cp['color'] != ctx['color']:
                        w['why'] = (w.get('why') or '') + f" 色別品番は見積のまま（{cp['color']}。車両カラー {ctx['color']} で再検索すると変わる）"
                if cp is None and dcode == 0:
                    # [70] は生バイト 0x00/0x01（W66 に 0x03 が 1 件）。ASCII ではない（J52 全 2,927 行で確認）。判定は「選んだ変種」の値（ボディ固有行優先）: D98 0073 は共通行 52713-B2470（0）が選ばれ 13.DB の B2480-A0 は使わない
                    if std_pn and parts.variant_color_flag(ref, std_pn, ctx.get('body'), ctx):  # 初度登録年月（reg_ym）で 13.DB の生産期間を絞る
                        # 品番が無い／83.DB に無い品番のときは車両のカラーコードに合う色別品番（コグニの部品検索と同じ）
                        # カラー未設定の車両でも色なし（期間・仕様別）行は対象（コグニの「複数部品選択」はカラーに依らず出る）。色付き行は colored_part 側でカラー一致のときだけ使う
                        cp = parts.colored_part(ref, ctx.get('color', '') or '', ctx.get('grade', ''), ctx.get('fva', ''), set(ctx.get('eva') or ()), std_pn, ctx.get('reg_ym', ''))
                        if cp and pn_in:
                            w['why'] = (w.get('why') or '') + f" 色別品番 {cp['pn']}（見積 {it.get('parts_no')} は {cp.get('src', '83')}.DB に無い）"
                if cp:
                    std_pn, std_price, color_name = cp['pn'], int(cp['price'] or 0), cp['name']
                    cgroup = cp['cgroup']
                elif std_pn:
                    cgroup = next((r.get('cgroup', '') for r in parts._load_11_raw().get(ref, []) if parts.norm_pn(r['pn']) == parts.norm_pn(std_pn)), '')
                if dcode == 2:
                    # 修理(2) の ConstructGroup は 11.DB の修理(S) 行のもの（取替(K) 行ではない）: W66 4802 は S 行 'Z0' → 'Z0'、W66 4600 は K 行 'M8' でも S 行 '  ' → '  '（実機 2026-09-12 w66_real / w66b_real）。
                    # S 行が複数ある部品は板金(6) と同じく車両条件（年式群・グレード/FVA/EVA・ボディ）で選ぶ（Codex 指摘）。最終 EVA では標準化パスで作り直す
                    try:
                        _g2 = (str(ctx.get('year', '')).strip()[-1] if str(ctx.get('year', '')).strip().isdigit() and int(ctx.get('year') or 0) else '')
                        _sr2 = parts._std_row(ref, 2, ctx.get('grade', ''), ctx.get('fva', ''), set(ctx.get('eva') or ()), _g2, str(ctx.get('body', '') or ''))
                    except Exception:
                        _sr2 = None
                    cgroup = (_sr2.get('cgroup') or '') if _sr2 else ''
                if dcode == 0 and it.get('parts_price') is None and it.get('price') is None and std_price > 0 and not it.get('reserve'):
                    # 価格キー自体が無い取替行（部品コードだけ指定した estimate.json）: コグニは部品コード入力時に標準価格（色別部品ならその価格）を入れる（FRAME_p7/p8）。
                    # 見積書の価格欄が空欄の行（PDF パーサは parts_price 0）は「部品代なし」なので補完しない（合計が見積書と一致しなくなる）。脱着・修理行も -1 のまま
                    pprice = std_price * max(1, qty)
                if var:
                    # 装備推定（一致変種だけが持つフラグ = 装備あり、他変種だけが持つ = 装備なし）。色別部品を使う行でも 11.DB 変種の証拠は残す
                    mf = set(ch for ch in str(var.get('grade_flags', '')) if ch.isalpha())
                    of = set(ch for o in others for ch in str(o.get('grade_flags', '')) if ch.isalpha())
                    if it.get('parts_no'):
                        for ch in mf - of:
                            opt_pos[ch] += 1
                        for ch in of - mf:
                            opt_neg[ch] += 1
                rec12 = parts.p12.get(ref)
                std_name = (color_name or (rec12['name'] if rec12 else (var['name'] if var else ''))).strip()
                block = parts.block_of(ref)
                # 工賃指数: 見積工賃に最も近い指数
                best = None
                for ent in parts.wage_entries(ref):
                    wi = ent.get('wi') or 0
                    if wi <= 0:
                        continue
                    est = r10_even(wi * rate / 100)
                    d = abs(est - wage) if wage > 0 else 10 ** 9
                    if best is None or d < best[0]:
                        best = (d, wi, ent.get('section', ''))
                if best and wage > 0 and best[0] <= max(300, wage * 0.05):
                    wi_used, sec_used = best[1], best[2]
                    work_code = (sec_used or '').ljust(10)[:10]
                # 見積書に指数が印字されている場合はそれを正とする（コグニ印字の工場見積など）
                if it.get('index'):
                    wi_pdf = int(round(float(it['index']) * 100))
                    if wi_used != wi_pdf:
                        wi_used = wi_pdf
                        sec_pdf = next((ent.get('section', '') for ent in parts.wage_entries(ref) if (ent.get('wi') or 0) == wi_pdf), None)
                        if sec_pdf is not None:
                            sec_used = sec_pdf; work_code = (sec_used or '').ljust(10)[:10]
            if dcode in (2, 6):
                work_code = ''  # 板金(6)/修理(2) 行の WorkCode は空欄（工場 NEO の板金 7 行・修理 13 行、コグニ再検索 C-HR 3500 板金 '#'。取替(K) 行の区分を流用しない）
            name_disp = display_name(std_name) if std_name else hw(re.sub(r'(?<=[ｦ-ﾟ])S$', 'ｽ', it.get('name', '')))  # 半角カナの末尾の 'S' だけ 'ｽ' に（OCR の読み違い）。英字の品名 'SPL. S' は直さない（2026-09-13 ベンツ）
            if it.get('name') and ('左' in it['name'] or '右' in it['name']) and name_disp and name_disp[0] not in '左右':
                name_disp = ('左' if '左' in it['name'] else '右') + name_disp
            is_sub = pprice > 0 and wage == 0 and dcode == 0 and ref is not None  # 手入力行（部品コード無し）には付属部品の 2 スペース接頭辞を付けない（コグニ実機 2026-09-08）
            no_price_part = dcode == 0 and std_pn.strip() == '-' and pprice <= 0 and not it.get('reserve')  # 価格なし部品（11.DB 品番 '-'）の取替で部品代 0/空欄: PartsPrice 0・PartsNo '-'（合計は変わらない）
            std_pn_raw = next((r.get('pn_raw') for r in (parts._load_11_raw().get(ref, []) if ref is not None else []) if r.get('pn') == '-'), std_pn) if std_pn.strip() == '-' else std_pn
            # コグニは明細名称を 11.DB（色別なら 83.DB）の名称欄から作る（再評価で見積書の表記に関係なく置き換わる）。名称欄が取れた行はそれを正にする
            name20 = None
            if cp:
                name20 = cp.get('name20')
            elif ref is not None and std_pn:
                # 11.DB は修理方法ごとに名称欄が違うことがある（0010 は取替 'Fﾊﾞﾝﾊﾟﾌｴｲｽ(ﾄｿｳｽﾞﾐ)' / 脱着 'Fﾊﾞﾝﾊﾟﾌｴｲｽ'）。
                # ただし実機は同じ部品・同じ修理方法でも両方の形で保存されている（cogni_M1/R1 は取替の名称のまま、
                # cogni_H6/H11 は脱着の名称）。コグニが部品を入れた時点の修理方法で決まり、後から変えても名称は残るため。
                # estimate からは操作履歴が分からないので、品番の取れた行（＝取替）の名称を正とする
                name20 = next((r.get('name20') for r in parts._load_11_raw().get(ref, []) if parts.norm_pn(r['pn']) == parts.norm_pn(std_pn) and r.get('disp') == parts.DISP_LETTER.get(dcode, 'K')), None) \
                    or next((r.get('name20') for r in parts._load_11_raw().get(ref, []) if parts.norm_pn(r['pn']) == parts.norm_pn(std_pn)), None)
            if name20 is None and ref is not None and 'K' in (parts.disp_by_ref.get(ref) or ''):
                # 品番の出ない行（脱着・板金など）でも、取替できる部品はコグニが 11.DB の名称欄を使う。
                # 12.DB の名称は左右を持たないので、ここで補わないと
                # 「左Frﾊﾞﾙｸﾍﾂﾄﾞｻｲﾄﾞｽﾃ-」が「Frﾊﾞﾙｸﾍﾂﾄﾞｻｲﾄﾞｽﾃｰ」になり左右が消える（実機 cogni_T1 1410 / H24 2599）。
                # 取替できない作業項目（12.DB の「…(修理)」「…(片側)」など）は 12.DB の名称が正
                # （実機 cogni_CX1 0005 ﾗｲｾﾝｽﾌﾟﾚｰﾄ(修理) / cogni_H24 7600 ｻｽﾍﾟﾝｼﾖﾝ(片側)）。
                # 11.DB の [0] は左右・[1] は前後なので、12.DB 名（左右なし）と比べるのは [1:]。
                # 補いたいのは 12.DB に無い左右の別だけなので、左右記号を持つ候補に限る
                # （左右のない品目は 12.DB の名称がそのまま出る。実機 cogni_R1 ｸﾞﾘﾙﾍﾞｰｽ）
                n20s = [n for n in sorted(parts.name20_by_ref.get(ref) or [])
                        if n[:1] in ('L', 'R') and _same_part_name(n[1:], std_name)]
                if len(n20s) == 1:
                    name20 = n20s[0]
                elif n20s:
                    want = side_letter(it.get('name', ''))   # 見積の名称が示す左右（L/R。無ければ ''）
                    hit = [n for n in n20s if n[:1] == (want or ' ')]
                    if len(hit) == 1:
                        name20 = hit[0]
            if name20 and not it.get('manual'):
                pn_disp, pn_std = cogni_parts_names(name20)
            else:
                pn_disp, pn_std = None, None
            if ref is None and it.get('index'):
                wi_used = int(round(float(it['index']) * 100))
            time_h = (wi_used / 100.0) if wi_used > 0 else -1  # ref 無し・index 無しの行は指数を推定せず手入力工賃（Time=-1）
            # 指数の出所: 12.DB の標準指数に一致すれば標準、見積書の指数を直接入れた（標準に無い）なら手入力指数 '#'
            std_wis = [int(e.get('wi') or 0) for e in parts.wage_entries(ref)] if ref is not None else []
            # index_policy 'manual'（非コグニ書式の工場見積）: 見積書の指数は標準に一致しても '#' にする。
            # 理由: コグニは再評価時に装備条件から標準区分（例 U→U1、O5 100→90）を選び直し、標準扱いの行は指数が置き換わる／消える（N-ONE 実機 2026-09-04）
            wi_manual = wi_used > 0 and (wi_used not in std_wis or (index_policy == 'manual' and bool(it.get('index'))))
            wbm_ = ('#' if (wi_manual and wage > 0) else ('' if wi_used > 0 and r10_even(wi_used * rate / 100) == wage else ('*' if wage > 0 else '')))
            if wbm_ == '*' and not it.get('index'):
                time_h = -1  # 工賃だけ手入力で指数の無い行: コグニは指数欄空欄（印刷でも空欄）。標準欄（TimeStandard/WageStandard）は残す
            row = {
                'RecordNo': i + 1, 'LineNo': (i + 1) * 10,
                'PartsCode': f'{ref:04d}' if ref is not None else '', 'PartsCodeSub': -1,
                # 標準欄は手入力行（DisposalCode -1）では空（実案件 NEO 300 本・-1 の行 1,400 件すべて空。2026-09-19）
                'DisposalCode': dcode, 'DisposalName': disp_name, 'DisposalNameStandard': ('' if dcode == -1 else disp_name),  # 同じコードでも W/S で選んだ名称を保存（3: 脱着修理/脱着板金、4: 点検/調整/点検調整。NEW2）
                'PartsName': _fit(pn_disp if pn_disp is not None else (('  ' if is_sub else '') + name_disp), 24), 'PartsNameStandard': ('' if dcode == -1 else _fit(pn_std if pn_std is not None else ((' ' + std_name) if std_name else name_disp), 24)),  # 修理方法空欄の手入力行は標準名称欄も空（実機）
                '_smb_tail': (parts.tail_by_ref.get(ref, '') if ref is not None else ''),
                '_smb_disp': (parts.disp_by_ref.get(ref, '') if ref is not None else ''),
                '_dmg_block': (parts.damage_block(ref) if ref is not None else ''),
                '_dmg_nokd': (parts.no_kd(ref) if ref is not None else False),  # 12.DB の可能作業に K も D も無い品目（DamageParts.PartsType 1・BlockCode 空）  # DamageParts.BlockCode 用。ERParts.BlockCode は W/S 版で空になることがあるが、損傷部品の部位は 12.DB の全版から引く（実機 cogni_CD98 0012）
                'CommentFlag': 1 if it.get('comment') else 0, 'Comment1': _fit(it.get('comment', ''), 40),  # TEXT(40): コグニ保存時に 40 バイトで切詰（N-ONE 案件で確認）
                '_recycle': it.get('recycle'), '_reserve': _flag(it.get('reserve'), 'items[].reserve'),
                'PartsNo': ((cp['pn'] if cp else (re.sub(r'\s*\(\d+\)\s*$', '', it.get('parts_no', '') or '') or (std_pn if dcode == 0 else ''))) if (pprice > 0 or it.get('reserve')) else (std_pn_raw if no_price_part else '')),  # 品番欄が無い取替行はコグニが標準品番を入れる（FRAME_p7。'-' 部品は生値 '     -'）
                'PartsNoStandard': (std_pn_raw if std_pn.strip() == '-' else std_pn),  # '-' 部品は 11.DB 生値の右トリム（J52 '     -'、D88 '-'。他工場 NEO・FRAME_p7 1511）
                '_sub_prefix': (pn_disp is None and is_sub),
                'PartsPriceOutTax': pprice if pprice > 0 else (0 if no_price_part else -1),  # 価格なし部品（11.DB 品番 '-'）の取替で価格未指定: コグニは 0（FRAME_p7 1511）
                'PartsUnitPriceOutTax': (pprice // qty if qty > 1 and pprice > 0 else -1),
                'PartsPriceStandardOutTax': (0 if std_pn.strip() == '-' else (std_price if std_price > 0 else (-1 if (pprice <= 0 or ref is None or no_std_price) else (pprice // qty if qty > 1 else pprice)))),  # 手入力行（部品コード無し）の標準価格は -1（コグニ実機 2026-09-08。工場 NEO の '*' 手入力行も -1）  # 標準価格は単価（数量倍しない。コグニ実機 NONE_dc Rec8: 単価 155×10 で標準 155）。品番 '-' の部品は入力価格に関わらず 0（他工場 NEO 17 行）
                'PartsPriceByManual': ('*' if (pprice > 0 and (std_price <= 0 or pprice != std_price * qty)) else ''),  # 標準価格が無い部品（手入力行・ADDATA に価格の無い品番・ディーラーオプション）に金額を入れた行も '*'（他工場 NEO 23 行: 6029 ｶﾞﾗｽｾﾂﾁﾔｸｻﾞｲ 等、std 0 → '*'。コグニ印刷 2026-09-07 オデッセイの手入力行・08R04 バイザも '*'）
                # コグニ生成 NEO（04011103 等）の板金/修理行: 指数を手入力すると WageByManual '#', TimeStandard 0, WageStandardOutTax 0
                'Time': time_h, 'TimeStandard': (0 if wi_manual else ((wi_used / 100.0) if wi_used > 0 else 0)),  # 標準の無い行はコグニは 0（-1 は書かない: 他工場 NEO 613 行すべて 0、FRAME_p9 1442/1517）
                'WageOutTax': wage if wage > 0 else -1,
                'WageStandardOutTax': (0 if wi_manual else (r10_even(wi_used * rate / 100) if wi_used > 0 else 0)),
                'WageByManual': wbm_,
                'PartsCount': qty if pprice > 0 else -1,
                'WorkCode': work_code if work_code else ' ' * 10, 'BlockCode': block,
                'ConstructGroup': ((cgroup or '  ') if dcode in (0, 2) else ('  ' if (dcode in (1, 3) and not wi_manual) else '')),  # コグニ自身の書式（NEW2/NEW4）: 標準指数の脱着/脱着板金は '  '、修理（d2）は 11.DB の ConstructGroup（W66 4802 は 'Z0'、他の実機 修理 11 行はすべて '  '。実機 2026-09-12 w66_real / 2026-09-08 cogni_H11 0010）、それ以外の実額入力行は NULL
                'WageFileTime': (f'{wi_used / 100.0:g}' if (wi_used > 0 and not wi_manual and wage > 0 and r10_even(wi_used * rate / 100) == wage) else ''),  # WageByManual '' と同じ条件（標準工賃に一致）のときだけ  # ADDATA から引いた標準指数の文字列。手入力指数 '#' や手入力工賃 '*' の行は ''（全 NEO 調査 2026-09-06: '' 行 432/432 が Time と同値、'*' 行は ''、'#' 行は標準があれば TimeStandard）
                'PartsPriceFlag': 1 if pprice <= 0 and (std_price > 0 or (dcode == 2 and ref is not None and bool(parts._load_11_raw().get(ref)))) else 0,  # 修理(2) は標準価格が '-'（0）でも部品コードが 11.DB にあれば 1（実機 2026-09-12 W66 4802。実機の修理行 12/12 が 1）
                '_ref_why': w['why'], '_std_name': std_name, '_manual': bool(it.get('manual')), '_wage_given': (it.get('wage') is not None and str(it.get('wage')).strip() != ''), '_index_given': bool(it.get('index')), '_no_d': no_std_price, '_pn_in': str(it.get('parts_no') or ''),
                # コグニ生成 NEO: 外板パネル（20.DB）の板金(6)行はランクダイアログ既定の DamageRank 'A'・Btn 1/1/1、骨格の板金行は空
                'DamageRank': ('A' if (dcode == 6 and ref is not None and f'{ref:04d}' in paint_codes) else ''),
                'DamageRankBtn1': (1 if (dcode == 6 and ref is not None and f'{ref:04d}' in paint_codes) else 0),
                'DamageRankBtn2': (1 if (dcode == 6 and ref is not None and f'{ref:04d}' in paint_codes) else 0),
                'DamageRankBtn3': (1 if (dcode == 6 and ref is not None and f'{ref:04d}' in paint_codes) else 0),
            }
            if dcode in (4, 5):
                # コグニ実機（N-ONE 2026-09-05）: 点検調整(4)/分解調整(5) は標準指数を持たない → 標準側は常にクリア（TimeStandard 0 / WageStandard 0 / WorkCode 空）。
                # 見積書に指数があれば手入力指数 '#'（Time はその値）、無ければ Time -1 で工賃のみ手入力 '*'（工賃近似で拾った 15.DB 指数は使わない）
                if not it.get('index'):
                    row['Time'] = -1
                row.update({'TimeStandard': 0, 'WageStandardOutTax': 0, 'WageFileTime': '', 'WorkCode': ' ' * 10,
                            'WageByManual': ('#' if (it.get('index') and wage > 0) else ('*' if wage > 0 else ''))})
            bk = it.get('bankin')
            if bk and dcode == 6:  # 板金ランク入力（コグニ板金ダイアログ相当）
                area = int(bk.get('area') or 10)
                yes = [1 if y else 0 for y in (bk.get('yes') or [1, 1, 1])][:3] + [1] * (3 - len(bk.get('yes') or [1, 1, 1]))
                rank = 'A' if sum(yes) == 3 else ('C' if sum(yes) == 0 else 'B')
                t_bk = bankin_time(area, rank)
                if t_bk is None:
                    raise ValueError(f'板金ランク: BANKIN.DB に面積 {area} dm² の行が無い（1〜40 のみ）か reference/BANKIN.DB が欠落: {it.get("name")}')
                fuka_sel = bk.get('fuka') or []
                fuka_all = bankin_fuka(f'{ref:04d}') if ref is not None else []
                t_fk = 0.0
                if fuka_sel is True:
                    t_fk = sum(t for _, t in fuka_all if t > 0)
                else:
                    for nm_ in fuka_sel:
                        t_fk += next((t for n_, t in fuka_all if hw(nm_).replace(' ', '') in hw(n_).replace(' ', '') or hw(n_).replace(' ', '') in hw(nm_).replace(' ', '')), 0.0)
                t_all = round(t_bk + t_fk, 2)
                w_bk = r10_even(t_all * rate)
                row.update({'Time': t_all, 'TimeStandard': t_all, 'WageOutTax': w_bk, 'WageStandardOutTax': w_bk, 'WageByManual': '@', 'WageFileTime': '0',  # 板金ランクの指数は標準欄にも入る（コグニ実機 2026-09-08 cogni_H11 0800: TimeStandard 1.5 / WageStandard 12,000 / WageFileTime '0'）
                            'DamageArea': str(area), 'DamageRank': rank, 'DamageRankBtn1': 1 if yes[0] else 2, 'DamageRankBtn2': 1 if yes[1] else 2, 'DamageRankBtn3': 1 if yes[2] else 2,
                            'SATime1Flag': 1 if t_fk > 0 else 0,
                            'PartsNo': f'{area}d㎡ {rank}' + (f' 付加 {t_fk:.2f}' if t_fk > 0 else '')})
                if ref is not None and f'{ref:04d}' in paint_codes:
                    try:
                        # 20.DB はボディごとに面積・名称の違う行を持つので、この車のボディで引く。
                        # 使い回して「選べなかった」控えを 1 か所に集める（report に載せるため）
                        _key = (car_code, parts.vehicle_body)   # 車種・ボディが変わったら作り直す
                        if getattr(self, '_pi_rows_key', None) != _key:
                            self._pi_rows = PaintIndex(self.engine.root, car_code, body=parts.vehicle_body)
                            self._pi_rows_key = _key
                        pn20 = self._pi_rows.panel(f'{ref:04d}')
                        if pn20 and pn20.get('name'):
                            row['PartsNameStandard'] = _fit(pn20['name'].strip(), 24)
                    except Exception as _e20:  # noqa: BLE001  20.DB が無い車種はある。握り潰すと退行が静かに起きるので理由を残す
                        self._paint20_error = str(_e20)
                        self._note_silent('20.DB（塗装パネル）の名称引き', _e20, '板金行の名称が 12.DB のままになる')
                wage = w_bk
            # 取替合計 = 標準部品代（数量で割った単価）+ 標準工賃（コグニ実験保存版: 2個 260 円 → 130）
            q_ = max(1, int(row.get('PartsCount') or 1))
            row['ChangeTotalOutTax'] = (row['PartsPriceStandardOutTax'] if row['PartsPriceStandardOutTax'] > 0 else 0) + (row['WageStandardOutTax'] if row['WageStandardOutTax'] > 0 else 0)  # 標準価格は単価なので数量で割らない
            if row['ChangeTotalOutTax'] == 0:
                row['ChangeTotalOutTax'] = -1
            rows.append(row)
            evidence.append(f"{row['PartsCode'] or '----'} {name_disp}: {w['why']} / 指数{wi_used if wi_used > 0 else '-'} {sec_used or ''}")
        if getattr(parts, '_r11_error', ''):
            self._note_silent('11.DB（品番・価格）の解析', RuntimeError(parts._r11_error), '標準品番・標準価格が全行で空になる')
        stats = {'labor_rate': rate, 'rate_votes': dict(rate_votes.most_common(3)), 'labor_rate_assumed': rate_assumed,
                 'matched': sum(1 for r in rows if r['PartsCode']), 'total': len(rows),
                 'option_pos': dict(opt_pos), 'option_neg': dict(opt_neg), 'evidence': evidence,
                 # build_rows を直に呼ぶ検査・スクリプトからも握り潰しに気づけるようにする（build は report['silent_errors'] にも載せる）
                 'silent_errors': list(self._sil_call or [])}
        return rows, stats

    # ------------------------------------------------------------ SQLite helpers
    @staticmethod
    def _open(db_bytes: bytes):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.sld')
        tmp.write(db_bytes); tmp.close()
        con = sqlite3.connect(tmp.name)
        con.text_factory = lambda b: b.decode('utf-8', 'replace')
        return con, tmp.name

    @staticmethod
    def _close(con, path) -> bytes:
        con.commit(); con.close()
        b = open(path, 'rb').read(); os.unlink(path)
        return b

    # ------------------------------------------------------------ AnSvEm
    def write_ansvem(self, db: bytes, rows: list[dict], paint_total: int, expenses: list[dict], paint_material: int = 0, estimate: Optional[dict] = None) -> tuple[bytes, dict]:
        mat_auto_rate = None  # 材料代を割合から自動計算したときの率（追加要素の加算後に再計算するため）
        notes: list[str] = []  # 塗装の注記（パネル別ブロックが無い見積でも追加項目で使う）
        con, p = self._open(db)
        cur = con.cursor()
        def t3i(v):
            v = int(v or 0); i_, t_ = tax_of(v); return (v, i_, t_)
        cols = [r[1] for r in cur.execute('PRAGMA table_info(ERParts)')]
        # 保留部品（見積書に「保留」として印字される行）は ERParts から外し ReserveERParts に入れる
        reserve_rows = [r for r in rows if r.get('_reserve')]
        self._has_reserve = bool(reserve_rows)  # 仮置き。最終値は ERParts を書き終えてから数え直す
        self._has_comment = any(r.get('CommentFlag') for r in rows)  # 仮置き。最終値は ERParts を書き終えてから数え直す
        for n_, r_ in enumerate(reserve_rows, 1):
            r_['ReserveFlag'] = 1; r_['ReserveRecordNo'] = 0  # コグニ自身の保留登録（W/S の保留チェック → SIENTA_hold.neo）は ReserveFlag=1, ReserveRecordNo=0 で ERParts に残すだけ。ReserveERParts には入れない
            r_['_reserve_price'] = r_.get('PartsPriceOutTax', -1)
            r_['PartsPriceOutTax'] = -1; r_['PartsUnitPriceOutTax'] = -1; r_['WageOutTax'] = -1; r_['Time'] = -1; r_['ChangeTotalOutTax'] = -1
        tmpl = dict(cur.execute('SELECT * FROM ERParts LIMIT 1').fetchone() and zip(cols, cur.execute('SELECT * FROM ERParts LIMIT 1').fetchone()) or [])
        cur.execute('DELETE FROM ERParts')
        for t in ['PaintingPanel', 'PaintingLinkParts', 'RCParts', 'RCLinkParts', 'RWLinkParts', 'EPCLinkParts', 'ReserveERParts', 'DamageParts', 'DamageComment', 'DamageImage', 'PartsPlan']:
            try:
                cur.execute(f'DELETE FROM {t}')
            except sqlite3.Error:
                pass
        try:
            cur.execute('INSERT INTO PartsPlan (NameShift, SortMode) VALUES (0, 0)')
        except sqlite3.Error:
            pass
        try:
            rcols = [r[1] for r in cur.execute('PRAGMA table_info(ReserveERParts)')]
            def _rdef(c):
                if c in ('RecordNo', 'LineNo'): return 1
                if c == 'DisposalCode': return 3
                if c == 'WageByManual': return '*'
                if c.startswith('WageStandard') or c == 'TimeStandard': return 0
                if c.endswith(('OutTax', 'InTax')) or (c.endswith('Tax') and c != 'Tax'): return -1
                if c in ('PartsCodeSub', 'PartsCount', 'Time') or re.fullmatch(r'SATime\d', c): return -1
                if c in ('OrderFlag', 'Provisional'): return ''
                if c.endswith(('Flag', 'RecordNo', 'Btn1', 'Btn2', 'Btn3')): return 0
                return ''
            adas_rows = self._adas_rows(estimate, 0)
            for n_, r_ in enumerate([], 1):  # 保留行は ReserveERParts に書かない（コグニ実機で確定。ReserveERParts は ADAS 作業専用）
                rec_ = {c: _rdef(c) for c in rcols}
                rec_['ERPartsRecordNo'] = 0  # コグニ自身が保留登録した保存版（SIENTA_exp）は 0
                for k_, v_ in r_.items():
                    if not k_.startswith('_') and k_ in rec_:
                        rec_[k_] = v_
                rec_['PartsPriceOutTax'] = r_.get('_reserve_price', -1); rec_['ConstructGroup'] = ''
                rec_.update({'RecordNo': n_, 'LineNo': n_, 'PartsPriceInTax': (tax_of(int(rec_['PartsPriceOutTax']))[0] if isinstance(rec_['PartsPriceOutTax'], (int, float)) and rec_['PartsPriceOutTax'] > 0 else -1),
                             'PartsPriceTax': (tax_of(int(rec_['PartsPriceOutTax']))[1] if isinstance(rec_['PartsPriceOutTax'], (int, float)) and rec_['PartsPriceOutTax'] > 0 else -1)})
                for c in ('WageOutTax', 'WageInTax', 'WageTax', 'Time'):
                    rec_[c] = -1
                cur.execute(f"INSERT INTO ReserveERParts ({','.join(rcols)}) VALUES ({','.join('?' * len(rcols))})", [rec_[c] for c in rcols])
            if adas_rows:
                # コグニの ADAS 画面（運転支援システム再設定・調整）: 空の入力行（DisposalCode 3, WageByManual '*'）が先に RecordNo を取り、作業行が続く。LineNo は表示順（作業行 → 空行が最後）
                n0 = 0
                blank = {c: _rdef(c) for c in rcols}; blank.update({'RecordNo': n0 + 1, 'LineNo': n0 + len(adas_rows) + 1})
                cur.execute(f"INSERT INTO ReserveERParts ({','.join(rcols)}) VALUES ({','.join('?' * len(rcols))})", [blank[c] for c in rcols])
                for i_, a_ in enumerate(adas_rows, 1):
                    rec_ = {c: _rdef(c) for c in rcols}
                    rec_.update({'RecordNo': n0 + 1 + i_, 'LineNo': n0 + i_, 'PartsCode': a_['code'], 'DisposalCode': a_['dcode'], 'PartsNo': a_['item'], 'PartsNoStandard': a_['sub'],
                                 'Time': a_['time'], 'TimeStandard': a_['time'], 'WageOutTax': a_['wage'], 'WageInTax': tax_of(a_['wage'])[0], 'WageTax': tax_of(a_['wage'])[1],
                                 'WageStandardOutTax': a_['wage_std'], 'WageStandardInTax': tax_of(a_['wage_std'])[0], 'WageStandardTax': tax_of(a_['wage_std'])[1],
                                 'WageByManual': ('*' if a_['wage'] != a_['wage_std'] else ''), 'WorkCode': '   ', 'ConstructGroup': a_.get('combi') or '   ', 'Provisional': ' ',
                                 'DamageArea': ' ', 'DamageRank': ' ', 'DamageRankBtn1': a_.get('order') or 0, 'PartsPriceFlag': 0})
                    cur.execute(f"INSERT INTO ReserveERParts ({','.join(rcols)}) VALUES ({','.join('?' * len(rcols))})", [rec_[c] for c in rcols])
            else:
                cur.execute(f"INSERT INTO ReserveERParts ({','.join(rcols)}) VALUES ({','.join('?' * len(rcols))})", [_rdef(c) for c in rcols])  # 既定 1 行（空の入力行）
            self._adas_rows_written = adas_rows
        except sqlite3.Error as ex_:
            if (estimate or {}).get('adas'):
                raise ValueError(f'ReserveERParts の書込に失敗（ADAS 行が欠落する）: {ex_}') from ex_
            print('ReserveERParts skip', ex_)
        # リサイクル部品に置換する行はコグニと同じく末尾へ（LineNo は 10 刻みで振り直し）
        if any(r.get('_recycle') for r in rows):
            # コグニは元行を削除して末尾に追加する: 他行の RecordNo は元のまま、LineNo だけ位置順に振り直し、追加行は RecordNo=最大+1
            nmax = max(r['RecordNo'] for r in rows)
            for r_ in rows:
                if r_.get('_recycle'):
                    r_['_orig_recno'] = r_['RecordNo']; r_['_orig_lineno'] = r_['LineNo']
                    nmax += 1; r_['RecordNo'] = nmax
            rows[:] = [r for r in rows if not r.get('_recycle')] + [r for r in rows if r.get('_recycle')]
            for i_, r_ in enumerate(rows):
                r_['LineNo'] = (i_ + 1) * 10
        parts_total = wage_total = 0
        parts_tax_sum = wage_tax_sum = 0  # コグニは合計の税額を「行ごとの税額（切捨）の合計」で持つ
        for r in rows:
            rec = {c: tmpl.get(c, '') for c in cols}
            # 既定値（実 NEO 準拠）
            rec.update({'PartsUnitPriceInTax': -1, 'PartsUnitPriceTax': -1, 'PartsFileTime': ('' if (not str(r.get('PartsCode') or '').strip() and int(r.get('DisposalCode') or 0) < 0) else '0'), 'ConstructGroup': r.get('ConstructGroup', '  '), 'OrderFlag': '',  # 手入力行（部品コード無し）の PartsFileTime は ''（コグニ実機 2026-09-08）  # PartsFileTime: 部品コード無しでも修理方法のある手入力行は '0'（コグニ再検索 C04）。'' は修理方法空欄の手入力行だけ（exp_manual）
                        'Provisional': '', 'DuplicateFlag': 0, 'ShapeModifyTime': '', 'DamageArea': '', 'DamageRank': '',
                        'DamageRankBtn1': 0, 'DamageRankBtn2': 0, 'DamageRankBtn3': 0, 'BlockListFlag': 0, 'RecycleFlag': 0,
                        'RCRecordNo': 0, 'ReserveFlag': 0, 'ReserveRecordNo': 0, 'CommentFlag': 0, 'Comment1': '', 'Comment2': '', 'Comment3': '', 'RWLinkFlag': 0})
            for k in range(1, 6):
                rec[f'SATime{k}'] = -1; rec[f'SATime{k}ByManual'] = ''; rec[f'SATime{k}Flag'] = 0
            for k, v in r.items():
                if not k.startswith('_') and k in rec:
                    rec[k] = v
            r['OrderFlag'] = rec['OrderFlag']  # AnSMB 100 桁と report が同じ値を見るように rows にも戻す
            for base in ['PartsPrice', 'PartsUnitPrice', 'PartsPriceStandard', 'Wage', 'WageStandard', 'ChangeTotal']:
                out = rec.get(base + 'OutTax', -1)
                if isinstance(out, (int, float)) and out >= 0:
                    it_, tx = tax_of(int(out))
                    if base == 'PartsUnitPrice':
                        tx = int(int(out) * TAX); it_ = int(out) + tx  # 単価欄の税だけ切捨（NONE_dc 155 → 15）
                    if base == 'PartsPrice' and int(rec.get('PartsCount') or 0) > 1 and int(rec.get('PartsUnitPriceOutTax') or 0) > 0 and int(rec['PartsUnitPriceOutTax']) * int(rec['PartsCount']) == int(out):
                        tx = int(math.floor(int(rec['PartsUnitPriceOutTax']) * TAX + 0.5)) * int(rec['PartsCount']); it_ = int(out) + tx  # 数量行の税 = 単価の税（四捨五入）×数量（コグニ実機: 155×10 → 税 160、185×9 → 171。単価欄 PartsUnitPriceTax は切捨 15 のまま）
                    rec[base + 'InTax'] = it_; rec[base + 'Tax'] = tx
                else:
                    rec[base + 'InTax'] = -1; rec[base + 'Tax'] = -1
            if rec['PartsPriceOutTax'] > 0:
                parts_total += rec['PartsPriceOutTax']; parts_tax_sum += rec['PartsPriceTax']
            if rec['WageOutTax'] > 0:
                wage_total += rec['WageOutTax']; wage_tax_sum += rec['WageTax']
            cur.execute(f"INSERT INTO ERParts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [rec[c] for c in cols])
        # リサイクル部品（コグニ「リサイクル部品登録」と同形）: 元行を RCLinkParts に退避し、ERParts 行をリサイクル部品に置換（工賃は消える）
        rc_total = 0
        for n_rc, (i, r) in enumerate([(i, r) for i, r in enumerate(rows) if r.get('_recycle')], 1):
            rc = r['_recycle']
            if not isinstance(rc, dict):  # recycle: true だけでは値段が決まらない（リサイクル部品の仕入額・売価は見積書から取る）
                raise ValueError(f"リサイクル部品 {str(r.get('PartsName') or '').strip()}: recycle は "
                                 '{"name": 名称, "price": 売価, "stock_price": 仕入値} の形で書く（true だけでは金額が決まらない）')
            price = _money(rc.get('price'), f"リサイクル部品 {rc.get('name', '')} の価格")
            stock = _money(rc.get('stock_price'), f"リサイクル部品 {rc.get('name', '')} の仕入値") or price
            name = _fit(hw(str(rc.get('name') or '')).strip() or r['PartsName'].strip(), 20)  # 部品の名称欄は半角カナ（明細の name と同じ扱い）
            cur.execute('INSERT INTO RCParts (RecordNo, PartsName, StockingPriceOutTax, StockingPriceInTax, StockingPriceTax, PartsPriceOutTax, PartsPriceInTax, PartsPriceTax, '
                        'PartsPriceCoefficient, Comment, ContactFlag) VALUES (?,?,?,?,?,?,?,?,1,"",1)', (n_rc, name, *t3i(stock), *t3i(price)))
            src = dict(zip(cols, cur.execute('SELECT * FROM ERParts WHERE RecordNo=?', (r['RecordNo'],)).fetchone()))
            lcols = [c[1] for c in cur.execute('PRAGMA table_info(RCLinkParts)')]
            link = {c: src.get(c, '') for c in lcols}
            link.update({'RecordNo': n_rc, 'PartsRecordNo': n_rc, 'ERPartsRecordNo': r.get('_orig_recno', r['RecordNo']), 'LineNo': r.get('_orig_lineno', r['LineNo'])})
            cur.execute(f"INSERT INTO RCLinkParts ({','.join(lcols)}) VALUES ({','.join('?' * len(lcols))})", [link[c] for c in lcols])
            wstd = src.get('WageStandardOutTax') or 0
            cur.execute('UPDATE ERParts SET PartsCodeSub=1, PartsName=?, PartsNo=?, PartsPriceOutTax=?, PartsPriceInTax=?, PartsPriceTax=?, PartsPriceByManual="R", PartsCount=1, '
                        'PartsNameStandard="", PartsNoStandard="", PartsPriceStandardOutTax=-1, PartsPriceStandardInTax=-1, PartsPriceStandardTax=-1, TimeStandard=0, '
                        'WageStandardOutTax=0, WageStandardInTax=0, WageStandardTax=0, PartsFileTime="", WorkCode="", ConstructGroup="", CommentFlag=0, Comment1="", '
                        'Time=-1, WageOutTax=-1, WageInTax=-1, WageTax=-1, WageFileTime="", ChangeTotalOutTax=?, ChangeTotalInTax=?, ChangeTotalTax=?, RecycleFlag=1, RCRecordNo=? WHERE RecordNo=?',
                        (name, 'リサイクル部品', *t3i(price), *t3i(price + (wstd if wstd > 0 else 0)), n_rc, r['RecordNo']))
            if src['PartsPriceOutTax'] > 0:
                parts_total -= src['PartsPriceOutTax']; parts_tax_sum -= src['PartsPriceTax']
            parts_total += price; parts_tax_sum += tax_of(price)[1]
            if src['WageOutTax'] > 0:
                wage_total -= src['WageOutTax']; wage_tax_sum -= src['WageTax']
            rc_total += price
            r['_smb_recycle'] = (name, price)
            r.update({  # 報告用の行も置換後の姿にそろえる（report.md の明細・印の照合が ERParts と食い違わないように）
                'PartsName': _fit(name, 24), 'PartsNo': _fit('リサイクル部品', 17), 'PartsNameStandard': '', 'PartsNoStandard': '',
                'PartsPriceOutTax': price, 'PartsPriceInTax': tax_of(price)[0], 'PartsPriceTax': tax_of(price)[1],
                'PartsCodeSub': 1, 'PartsPriceStandardOutTax': -1, 'PartsPriceStandardInTax': -1, 'PartsPriceStandardTax': -1,
                'PartsPriceByManual': 'R', 'PartsCount': 1, 'PartsFileTime': '', 'CommentFlag': 0, 'Comment1': '',
                'WageStandardInTax': 0, 'WageStandardTax': 0,
                'Time': -1, 'TimeStandard': 0, 'WageOutTax': -1, 'WageInTax': -1, 'WageTax': -1,
                'WageStandardOutTax': 0, 'WageByManual': '', 'WageFileTime': '', 'WorkCode': '', 'ConstructGroup': '',
                'ChangeTotalOutTax': price + (wstd if wstd > 0 else 0),
                'ChangeTotalInTax': tax_of(price + (wstd if wstd > 0 else 0))[0], 'ChangeTotalTax': tax_of(price + (wstd if wstd > 0 else 0))[1],
                'RecycleFlag': 1, 'RCRecordNo': n_rc})
        # 塗装連動部品（PaintingLinkParts）: 20.DB に載る部品（外板パネル・バンパ・4802 のような工賃行）の取替(0)/修理(2)/板金(6) 行。コグニは部位・W/S 画面を通ると生成する
        # （他工場 NEO 10 本: 取替 23・修理 14・板金 8 行がすべて 20.DB 収録部品。バンパ 0010/3810 は PaintingPanel には出ず PaintingBumper 側だが PaintingLinkParts には出る）
        pi_ = getattr(self, '_paint_index', None)
        if pi_ is not None:
            try:
                cur.execute('DELETE FROM PaintingLinkParts')
                for r in rows:
                    if r.get('DisposalCode') in (0, 2, 6) and r.get('PartsCode') and not r.get('_recycle'):
                        pn_ = pi_.panel_for_body(r['PartsCode'])  # 紐付けは明細のコードそのままで、**この車のボディに載っている行だけ**（枝番へ飛ばさない・他ボディ行も拾わない。コグニ実機 2026-09-12 W90: ボディ 10 専用の 4800 は連動しなかった）
                        if pn_:
                            cur.execute('INSERT OR IGNORE INTO PaintingLinkParts (RecordNo, PartsCode, DisposalCode, PanelName, PaintingFlag) VALUES (?,?,?,?,0)', (r['RecordNo'], r['PartsCode'], r['DisposalCode'], pn_['name']))
            except sqlite3.Error as ex:
                print('PaintingLinkParts skip', ex)
        # 損傷部位（コグニが保存時に生成するものと同形）: DamageBlock = 出現ブロック、DamageParts = 行→ブロック
        try:
            cur.execute('DELETE FROM DamageBlock'); cur.execute('DELETE FROM DamageParts')
            # コグニ保存版は 17.DB のブロック一覧（表示順 from≠0 のもの）を全件書く
            codes = []
            for c, f, t in (getattr(self, '_blocks17', None) or []):
                if f < 4096 and c not in codes:
                    codes.append(c)
            if not codes:
                codes = sorted({r['BlockCode'] for r in rows if r.get('BlockCode')})
            for bc in codes:
                cur.execute('INSERT OR IGNORE INTO DamageBlock (BlockCode) VALUES (?)', (bc,))
            for i, r in enumerate(rows):  # 部位コードが空の行（12.DB 基本版に無い部品・手入力行）も入れる（実機 cogni_K1: 5 行の見積で 5 行）
                # PartsType 1 = リサイクル置換行、または 12.DB の可能作業に取替(K)も脱着(D)も無い品目（修理のみ 'S' / オーバーホールのみ 'OH'）。
                # 後者は部位図の部品ではなく作業項目なので BlockCode も空になる（実機 2026-09-09 cogni_CX2: 0005 'S'・2000 'S'・8705 'OH' が 1、0003 'D'・6305 'DS'・7600 'OHD'・0045 'K' は 0）。
                # 保留（ReserveFlag=1）は 0（実機 cogni_CX4 6800。旧実装が 1 としていたのは SIENTA_hold 0005 が 'S' 品目だったための取り違え）
                _nokd = bool(r.get('_dmg_nokd'))
                _pt = 1 if (r.get('_recycle') or _nokd or not str(r.get('PartsCode') or '').strip()) else 0  # 部品コードの無い手入力行も 1（実機 cogni_M1 ｼｮｰﾄﾊﾟｰﾂ）
                # _dmg_block は 12.DB から引いた損傷部品の部位。空なら意図した空（版があいまい・12.DB に無い）なので
                # ERParts.BlockCode で埋め戻してはいけない（ERParts と DamageParts は別々に決まる。§10-19）
                _blk = '' if _nokd else (r.get('_dmg_block') or '')
                cur.execute('INSERT INTO DamageParts (RecordNo, ERPartsRecordNo, BlockCode, PartsType) VALUES (?,?,?,?)',
                            (r['RecordNo'], r['RecordNo'], _blk, _pt))
            cur.execute("UPDATE DamageBlockPlan SET DamageCode='0102030405060708', FrontArea=1, RearArea=1, AllArea=1")
        except sqlite3.Error as ex:
            print('DamageBlock skip', ex)
        # 塗装: 工場見積の塗装費用を「その他」行に実額計上。テンプレート由来のブース加算・基礎数値・骨格・シーリング等は全てクリア
        cur.execute('UPDATE PaintingOther SET Time=-1, WageOutTax=-1, WageInTax=-1, WageTax=-1, WageByManual=""')
        cur.execute('UPDATE PaintingPlan SET BoothFlag=0, BoothTime=-1, BoothTimeStandard=-1, BoothWageOutTax=-1, BoothWageInTax=-1, BoothWageTax=-1, '
                    'BoothWageStandardOutTax=-1, BoothWageStandardInTax=-1, BoothWageStandardTax=-1, BaseTime=-1, BaseTimeStandard=-1, '
                    'BaseWageOutTax=-1, BaseWageInTax=-1, BaseWageTax=-1, BaseWageStandardOutTax=-1, BaseWageStandardInTax=-1, BaseWageStandardTax=-1, '
                    'CalculateLevel_Panel=1, CalculateLevel_Booth=0, CalculateLevel_Base=1, CalculateLevel_Bumper=1, CalculateLevel_Frame=0, CalculateLevel_Etcetera=0')  # CalculateLevel は塗装明細の有無によらず Panel/Base/Bumper=1（コグニ保存版・工場 NEO 57 本すべて 1/0/1/1/0/0/0。内板骨格塗装がある NEO でも Frame は 0）
        if self._coat:
            cur.execute('UPDATE PaintingPlan SET Coat=?, CoatName=?', self._coat)
        cur.execute('UPDATE PaintingFrame SET er_Disposal=0, er_Time=-1, er_TimeStandard=-1, er_WageOutTax=-1, er_WageInTax=-1, er_WageTax=-1, '
                    'er_WageStandardOutTax=-1, er_WageStandardInTax=-1, er_WageStandardTax=-1')
        cur.execute('UPDATE PaintingEtcetera SET BSealing=-1, BSealingTime=-1, BSealingTimeStandard=-1, BSealingWageOutTax=-1, BSealingWageInTax=-1, BSealingWageTax=-1, '
                    'BSealingWageStandardOutTax=-1, BSealingWageStandardInTax=-1, BSealingWageStandardTax=-1, ARWax=-1, ARWaxTime=-1, ARWaxTimeStandard=-1, '
                    'ARWaxWageOutTax=-1, ARWaxWageInTax=-1, ARWaxWageTax=-1, ARWaxWageStandardOutTax=-1, ARWaxWageStandardInTax=-1, ARWaxWageStandardTax=-1')
        # 新規対応した付加塗装（ドアサッシュ/ストライプ/低隠蔽性/2コートソリッド/2トーン加算）もテンプレート値を引き継がないよう中立値へ（コグニ新規見積の初期値: 枚数 -1、Flag 0、Roof 0、Other -1）
        for col in ('DSBlack', 'BStripe', 'TwoTone'):
            cur.execute(f'UPDATE PaintingEtcetera SET {col}=-1, {col}Time=-1, {col}TimeStandard=-1, {col}WageOutTax=-1, {col}WageInTax=-1, {col}WageTax=-1, '
                        f'{col}WageStandardOutTax=-1, {col}WageStandardInTax=-1, {col}WageStandardTax=-1, {col}WageByManual="", {col}MaterialOutTax=-1, {col}MaterialInTax=-1, {col}MaterialTax=-1, {col}MaterialByManual=""')
        cur.execute('UPDATE PaintingEtcetera SET LCColorFlag=0, LCColorRoof=0, LCColorOtherChange=-1, LCColorOtherRepair=-1, LCColorTime=-1, LCColorTimeStandard=-1, '
                    'LCColorWageOutTax=-1, LCColorWageInTax=-1, LCColorWageTax=-1, LCColorWageStandardOutTax=-1, LCColorWageStandardInTax=-1, LCColorWageStandardTax=-1, '
                    'LCColorWageByManual="", LCColorMaterialOutTax=-1, LCColorMaterialInTax=-1, LCColorMaterialTax=-1, LCColorMaterialByManual="", LCColorByManual="", '
                    'TwoCSolidFlag=0, TwoCSolidRoof=0, TwoCSolidOther=-1, TwoCSolidTime=-1, TwoCSolidTimeStandard=-1, '
                    'TwoCSolidWageOutTax=-1, TwoCSolidWageInTax=-1, TwoCSolidWageTax=-1, TwoCSolidWageStandardOutTax=-1, TwoCSolidWageStandardInTax=-1, TwoCSolidWageStandardTax=-1, '
                    'TwoCSolidWageByManual="", TwoCSolidMaterialOutTax=-1, TwoCSolidMaterialInTax=-1, TwoCSolidMaterialTax=-1, TwoCSolidMaterialByManual="", TwoCSolidByManual=""')
        cur.execute("UPDATE PaintingPlan SET TwoToneFlag=0, TwoToneCoat=-1, TwoToneCoatName='', TwoToneMaterialRate=-1, CalculateLevel_TwoTone=0")  # コグニ保存版は 2トーン有りでも CalculateLevel_TwoTone 0
        for pfx in ('fb', 'rb'):
            cur.execute(f"UPDATE PaintingBumper SET {pfx}_Disposal=0, {pfx}_Name='なし', {pfx}_Form=-1, {pfx}_FormName='', {pfx}_Color=0, {pfx}_ColorName='', {pfx}_Draft=0, {pfx}_DraftName='', "
                        f"{pfx}_Time=-1, {pfx}_TimeStandard=-1, {pfx}_WageOutTax=-1, {pfx}_WageInTax=-1, {pfx}_WageTax=-1, "
                        f"{pfx}_WageStandardOutTax=-1, {pfx}_WageStandardInTax=-1, {pfx}_WageStandardTax=-1, {pfx}_WageByManual='', "
                        f"{pfx}_MaterialOutTax=-1, {pfx}_MaterialInTax=-1, {pfx}_MaterialTax=-1, {pfx}_MaterialByManual=''")
        cur.execute('UPDATE PaintingTotal SET TimeTotalPanel=0,TimeTotalBumper=0,TimeTotalFrame=0,TimeTotalEtcetera=0,TimeTotalOther=0,TimeTotal=0,'
                    'WageTotalPanelOutTax=0,WageTotalPanelInTax=0,WageTotalPanelTax=0,WageTotalBumperOutTax=0,WageTotalBumperInTax=0,WageTotalBumperTax=0,'
                    'WageTotalFrameOutTax=0,WageTotalFrameInTax=0,WageTotalFrameTax=0,WageTotalEtceteraOutTax=0,WageTotalEtceteraInTax=0,WageTotalEtceteraTax=0,'
                    'MaterialTotalOutTax=0,MaterialTotalInTax=0,MaterialTotalTax=0')
        pd = getattr(self, '_paint_detail', None)
        _jitsu = False   # この build で塗装を実額の形で書いたか（下の枝で立てる。あとで指定どおりに書き直すかの判定に使う）
        if pd:  # パネル別塗装（`panels` あり）か、パネル無しでバンパだけ塗る見積（_paint_detail 側で判定）
            # パネル別塗装: 20.DB（パネルマスタ）+ CHM 塗り数値 + T_KEI_3/BOOTH（加算基礎・ブース）+ 23.DB（バンパ）で
            # コグニが「パネル追加」で生成する PaintingPanel/PaintingPlan/PaintingBumper/PaintingEtcetera/PaintingTotal と同形に書く
            pi = getattr(self, '_paint_index', None)
            form = str(getattr(self, '_car_form', '') or '')
            rate = int(getattr(self, '_labor_rate', 0) or 0)
            pv = pd.get('paint')
            if (isinstance(pv, (int, float)) and not isinstance(pv, bool)) or (isinstance(pv, str) and pv.strip().isdigit()):  # 1 速乾 / 3 ２Ｋ / 4 水性 をコードで渡された場合
                paint_c = _int_strict(pv, 'paint.paint')
                if paint_c not in PAINT_CODE.values():
                    raise ValueError(f'paint.paint のコードは {sorted(set(PAINT_CODE.values()))} のいずれか（{pv}）')
            else:
                pn_ = unicodedata.normalize('NFKC', pv or '２Ｋ').replace('2K', '２Ｋ')
                if pn_ not in PAINT_CODE:
                    raise ValueError(f'paint.paint は {list(PAINT_CODE)} のいずれか（{pv}）')
                paint_c = PAINT_CODE[pn_]
            coat_c = self._coat[0] if self._coat else COAT_CODE.get(unicodedata.normalize('NFKC', pd.get('coat') or ''), 2)
            _hf_in = unicodedata.normalize('NFKC', str(pd.get('hf') or 'しない')).strip()
            _hf_map = {unicodedata.normalize('NFKC', k): v for k, v in HF_CODE.items()}
            if _hf_in not in _hf_map:  # 知らない高機能塗装を黙って「しない」にしない（加算基礎・材料代割合・パネル加算が変わる）
                raise ValueError(f"paint.hf は {sorted(set(HF_CODE) - {'ｽｸﾗｯﾁ'})} のいずれか（{pd.get('hf')!r}）")
            hf = _hf_map[_hf_in]
            panels = [p_ for p_ in pd['panels'] if not is_manual_panel(p_)]   # 部品コードのある外板パネル（20.DB）
            man_panels = [p_ for p_ in pd['panels'] if is_manual_panel(p_)]  # 手入力の塗装行（DisposalCode 9）
            # 加算基礎の枚数・単体塗/複数塗の判定は部品コードのあるパネルだけで数える（手入力の塗装行は数えない。実案件 NEO 60/60・16/16。2026-09-13）
            n_p = len(panels); n_rows = n_p + len(man_panels)
            pcols = [r[1] for r in cur.execute('PRAGMA table_info(PaintingPanel)')]
            notes = []
            if unicodedata.normalize('NFKC', str(((estimate or {}).get('paint') or {}).get('input_type') or '')).strip() == '実額'                     or _truthy(((estimate or {}).get('paint') or {}).get('actual')):
                # 実額は総額 1 つだけの欄で、パネル・加算基礎・材料計を持てない。パネルがあるのに指定されたら黙って捨てない
                notes.append('paint.input_type「実額」は塗装パネルのある見積では使えないので、指数（パネル別）のままにした'
                             '（実額は外板パネル・加算基礎・材料計を持てない。判断規則 10-15）')
            def t3(v):
                v = int(v or 0); return (v, *tax_of(v))
            def rp(x):
                return r10(int(round((x or 0) * 10)) * rate / 10) if rate and x else 0
            def wstd(x):
                return rp(x)
            # **枝番補正はしない**（コグニ実機 2026-09-11 で確かめた。判断規則 10-17）。
            # ボディで塗装パネルのコードが変わる車（W90 ハイエース: 明細 4800 ｸｵ-ﾀﾊﾟﾈﾙ → ボディ 20 の塗装パネルは 4801）で
            # 4801 を連動（AddedFrom 0）にすると、コグニは塗装ページを開いたときに連動行を明細から作り直すため、
            # 明細に 4801 が無い 4801 の行は**消える**（塗装計 176,560 → 142,610）。
            # AddedFrom 1・工賃 '*' の「パネル追加」なら実機でも残るので、枝番パネルはパネル追加のままにする。
            bankin_codes = {r.get('PartsCode') for r in rows if r.get('DisposalCode') == 6 and r.get('PartsCode')}  # 明細の板金行（塗装パネルの DisposalCode 6 判定）
            # 明細の取替/修理/板金行を「部品コード → 修理方法の集合」で持つ（PaintingLinkParts と同じ条件。
            # 修理(2) も連動: 12081431 0600・11261526 4800 の PaintingPanel は AddedFrom 0 / WageByManual ''。監査 42）。
            # **修理方法まで見る**: 実機 NEO 29 本の連動パネル 68 行は、明細に同じ部品コードかつ同じ
            # DisposalCode の行が必ずある（食い違い 0）。部品コードだけで連動と決めると、
            # 明細が板金(6) だけの部品を「取替塗装」で計上したときに連動扱いになり、実機と形が変わる
            linked_disp: dict = {}
            for r in rows:
                d_ = r.get('DisposalCode')
                if d_ in (0, 2, 6) and r.get('PartsCode') and not r.get('_reserve'):
                    linked_disp.setdefault(r['PartsCode'], set()).add(int(d_))
            linked_codes = set(linked_disp)
            panel_wage = 0; panel_time = 0.0
            for i, pnl in enumerate(panels):
                w = _money(pnl.get('wage'), f"塗装パネル {pnl.get('name', pnl.get('code', ''))} の工賃"); t = float(pnl.get('index') or 0)
                new = pnl.get('method') in ('取替', '新品', '交換')
                ratio = '' if new else (pnl.get('ratio') or '1/1').strip()
                std = pi.standard_times(_code4(pnl.get('code')), hf, n_p, paint_c) if pi else None
                mp = (std or {}).get('panel') or {}
                _chm = (std or {}).get('chm') or {}
                if ratio in ('1/2', '1/3') and _chm and _chm.get('r12') is None and _chm.get('r13') is None:
                    # CHM に 1/2・1/3 の列が無いパネル（ロッカパネルアウタ・サイドシルなど）は、コグニの塗装面積が 1/1 固定で選べない
                    # （実機 2026-09-12 W66 2602: 修理で連動しても '1/1'、指数 1.3。cogni_W66z）。1/2 を書いても実機で開けば 1/1 に戻るので 1/1 で書く
                    notes.append(f"{pnl.get('code')} {hw(pnl.get('name', '')).strip()}: このパネルは塗装面積 1/1 しか選べない（CHM に 1/2・1/3 の列が無い）ので {ratio} → 1/1")
                    ratio = '1/1'
                area = int(mp.get('area') or pnl.get('area') or 0)
                name = mp.get('name') or hw(pnl.get('name', ''))[:20].ljust(20)
                s = {k: (std or {}).get(k) for k in ('new', 's1', 's2', 's3', 'hf')}
                expect = {'': s['new'], '1/1': s['s1'], '1/2': s['s2'], '1/3': s['s3']}.get(ratio)
                if expect is None and not new and pi is not None and getattr(pi, 'chm_unavailable', False):
                    # 修正塗装の標準指数は CHM（車種別補修塗装指数）から取る。展開できない環境で黙って 0 を書くと、
                    # Windows で作った NEO と TimeStandard*/WageStandard* が変わる（実測: 9 案件中 1 件）。止めて理由を出す
                    raise ValueError(f"塗装パネル {pnl.get('code')} {pnl.get('name', '')}: 修正塗装の標準指数が取れない。この環境では塗装指数表（CHM）を展開できない"
                                     "（Windows の hh.exe か 7-Zip（Linux は p7zip-full）が必要）。同じ NEO にならないので作らない")
                if not t:
                    if expect is None:
                        raise ValueError(f"塗装パネル {pnl.get('code')} {pnl.get('name', '')}: 標準指数が取れず（CHM/係数表に無い）見積の index も無い。paint.panels[].index を指定してください")
                    t = expect
                if not w and rate:
                    w = rp(t)
                if expect is not None and abs(expect - t) > 0.05:
                    notes.append(f"{mp.get('code', pnl.get('code'))} {name.strip()} 指数 見積 {t} / 標準 {expect}")
                # 手入力指数: 標準値と異なる、または標準値が無く見積の index で決めた（コグニの再計算で消えないよう Manual=1 / '#'）
                pnl_manual = (expect is not None and abs(expect - t) > 0.05) or (expect is None and bool(pnl.get('index')))
                rec = {c: '' for c in pcols}
                pcode_ins = mp.get('code') or re.sub(r'\D', '', pnl.get('code', ''))[:4].zfill(4)  # 4 桁ゼロ埋め（明細行の PartsCode と突き合わせるため。監査 5）
                if not mp.get('code') and pi and pi.panel(pcode_ins) is None and not pi.panel_exact(pcode_ins):
                    # 20.DB にそのパネルが無い（3 桁前方一致も一意でない）: コグニが扱えない部品コードを書かないよう止める（Codex）
                    raise ValueError(f"塗装パネル {pnl.get('code')} {pnl.get('name', '')}: 20.DB に無い部品コード。塗装明細のパネルコードを 20.DB の 4 桁に直す（外板パネルの部品コードと同じ）")
                is_bankin = (not new) and bool(bankin_codes) and pcode_ins in bankin_codes  # 明細が板金(6) のパネルはコグニが自動追加する行と同形: DisposalCode 6・名称 '修理'・AddedFrom 0・Manual 0・WageByManual ''（NONE_pnl 2300）
                disp_pnl = 0 if new else (6 if is_bankin else 2)  # この塗装パネルの修理方法
                linked = disp_pnl in linked_disp.get(pcode_ins, set())  # 明細に同じ部品・同じ修理方法の行があるパネルはコグニが W/S 連動で自動生成（AddedFrom 0・WageByManual ''。工場のコグニ生成 NEO 04011103）。無いパネルは「パネル追加」相当（AddedFrom 1・'*'）
                rec.update({'RecordNo': i + 1, 'LineNo': i, 'PartsCode': pcode_ins,
                            'DisposalCode': disp_pnl, 'DisposalName': '取替' if new else '修理', 'PanelName': name,
                            'PrepareArea': -1 if new else (pi.prepare_area(area, ratio) if pi else -1), 'PanelArea': area,
                            'PaintingArea': -1 if new else {'1/1': 1, '1/2': 2, '1/3': 3}.get(ratio, 1), 'PaintingAreaName': ratio,
                            'Time': t, 'TimeStandardNew': s['new'] or (t if new else 0), 'TimeStandard1': s['s1'] or (t if ratio == '1/1' else 0),
                            'TimeStandard2': s['s2'] or (t if ratio == '1/2' else 0), 'TimeStandard3': s['s3'] or (t if ratio == '1/3' else 0), 'TimeStandardHF': s['hf'] or 0,
                            'WageOutTax': w, 'WageInTax': tax_of(w)[0], 'WageTax': tax_of(w)[1],
                            'WageByManual': ('#' if pnl_manual else ('' if (is_bankin or linked) else '*')), 'MaterialOutTax': -1, 'MaterialInTax': -1, 'MaterialTax': -1, 'MaterialByManual': '',
                            'PanelDivision': mp.get('div', 1), 'PanelTypeDivision': mp.get('type', 1), 'PanelCode': mp.get('pcode', 0),
                            'SortNo': 1, 'ButtonNo': mp.get('btn', i + 1), 'Provisional': 0, 'AddedFrom': 0 if (is_bankin or linked) else 1,  # 明細にその部品があるかだけで決める（実機 71 行で例外なし）。指数が標準と違う（pnl_manual）ことは関係ない —— それで 0 にすると明細に無いパネルが連動扱いになり、コグニが塗装ページを開いたときに消える
                            # 指数が標準値と異なる（工場見積の独自指数）ときは Manual=1: 0 だとコグニが塗装タブ表示時に標準値へ再計算する（N-ONE 案件 2026-09-04）
                            'Manual': 1 if pnl_manual else 0})  # SortNo はコグニ内部の操作回数カウンタ（表示に影響なし）
                for key, tv in (('New', rec['TimeStandardNew']), ('1', rec['TimeStandard1']), ('2', rec['TimeStandard2']), ('3', rec['TimeStandard3']), ('HF', rec['TimeStandardHF'])):
                    rec[f'WageStandard{key}OutTax'], rec[f'WageStandard{key}InTax'], rec[f'WageStandard{key}Tax'] = t3(wstd(tv))
                cur.execute(f"INSERT INTO PaintingPanel ({','.join(pcols)}) VALUES ({','.join('?' * len(pcols))})", [rec[c] for c in pcols])
                panel_wage += w; panel_time += t
            # 手入力の塗装行（外板パネル画面の「行追加」。部品コード無し・名称と指数を手で入れた行 = DisposalCode 9）。
            # 実案件 NEO 354 本・631 行（2026-09-13）: 部品コード空・DisposalName 空（修理方法を選んだ行は '修理'/'取替'）・面積と標準欄はすべて -1・
            # 指数あり → 工賃 = 指数 × 単価、工賃印 '#'・Manual 1 / 工賃だけ → Time -1・工賃印 '*'・Manual 2。AddedFrom 1・SortNo 15・名称は入力のまま（詰めない）。
            # 部品コードのあるパネルの後ろに入力順で並ぶ。塗装工賃計（材料代の対象）に入り、加算基礎の枚数には数えない
            for j, pnl in enumerate(man_panels):
                # 観測: コグニで行追加する人の入力は半角カナが大半（実案件 NEO 209 行のうち 全角カナ 3・前後空白 4、長音は 'ｰ' と '-' の両方。
                #       2026-09-17 に 400 本で数え直すと外板パネル 851 行中 849 行が半角）。
                # 方針: 少数の全角も半角に寄せる（判断規則 10-22b。亮平さん指示 2026-09-17）。20 バイトで切るのは同じ
                nm = _fit(hw(str(pnl.get('name') or '')), 20)
                t = float(pnl.get('index') or 0)
                w = _money(pnl.get('wage'), f"手入力の塗装行 {nm} の工賃")
                if t > 0 and not w and rate:
                    w = rp(t)
                elif t > 0 and w and rate and w != rp(t):
                    # 実案件 NEO の指数付き手入力行は 工賃 = r10(指数 × 単価)。見積の工賃は動かさない（行の金額を合計合わせで動かさない原則）が、知らせる
                    notes.append(f"手入力の塗装行 {nm}: 工賃 {w:,} が 指数 {t:g} × 単価 {rate:,} = {rp(t):,} と違う（見積の工賃のまま '#' で書く。読み取りを確かめる）")
                if t <= 0 and not w:
                    raise ValueError(f'手入力の塗装行 {nm or "(名称なし)"}: 指数（index）か工賃（wage）が要る')
                if not nm.strip():
                    raise ValueError('手入力の塗装行に名称（name）が無い（コグニの外板パネル画面に出る名前）')
                m_ = unicodedata.normalize('NFKC', str(pnl.get('method') or '')).strip()
                rec = {c: '' for c in pcols}
                rec.update({'RecordNo': n_p + j + 1, 'LineNo': n_p + j, 'PartsCode': '', 'DisposalCode': 9,
                            'DisposalName': ('取替' if m_ in ('取替', '新品', '交換') else ('修理' if m_ in ('修理', '修正') else '')),
                            'PanelName': nm, 'PrepareArea': -1, 'PanelArea': -1, 'PaintingArea': -1, 'PaintingAreaName': '',
                            'Time': t if t > 0 else -1, 'TimeStandardNew': -1, 'TimeStandard1': -1, 'TimeStandard2': -1, 'TimeStandard3': -1, 'TimeStandardHF': -1,
                            'WageOutTax': w, 'WageInTax': tax_of(w)[0], 'WageTax': tax_of(w)[1],
                            'WageByManual': '#' if t > 0 else '*', 'MaterialOutTax': -1, 'MaterialInTax': -1, 'MaterialTax': -1, 'MaterialByManual': '',
                            'PanelDivision': -1, 'PanelTypeDivision': -1, 'PanelCode': -1, 'SortNo': 15, 'ButtonNo': -1, 'Provisional': 0,
                            'AddedFrom': 1, 'Manual': 1 if t > 0 else 2})
                for key in ('New', '1', '2', '3', 'HF'):
                    rec[f'WageStandard{key}OutTax'] = rec[f'WageStandard{key}InTax'] = rec[f'WageStandard{key}Tax'] = -1
                cur.execute(f"INSERT INTO PaintingPanel ({','.join(pcols)}) VALUES ({','.join('?' * len(pcols))})", [rec[c] for c in pcols])
                panel_wage += w; panel_time += (t if t > 0 else 0)
            _pp = [dict(zip(pcols, r)) for r in cur.execute('SELECT * FROM PaintingPanel ORDER BY RecordNo')]
            # 「パネル追加」（AddedFrom 1）した行が 1 枚でもあると、コグニは加算基礎数値の工賃印を '*' にして保存する
            # （実機 NEO: cogni_P1 = 2601 パネル追加 → BaseWageByManual '*'、w90_real 2026-09-12 = 4801 パネル追加 → '*'。
            #   パネル追加の無い実機 27 本はすべて ''。値は標準のままでも印だけ '*'）
            _any_added = any(int(x.get('AddedFrom') or 0) == 1 and int(x.get('DisposalCode') or 0) != 9 for x in _pp)  # 手入力の塗装行だけなら付かない（実案件 NEO 241/245）
            # 関門: AddedFrom 0（W/S 連動）の塗装パネルは、必ず明細（ERParts）に同じ部品コードの
            # 取替/修理/板金行があること。コグニは塗装ページを開くと連動行を明細から作り直すので、
            # 明細に無い連動行は **開いた瞬間に消える**（2026-09-11 実機 W90 ハイエース: 4801 が消え、
            # 枚数 1 で再計算されて塗装計が 176,560 → 142,610 になった）。
            # 実機 NEO 29 本・塗装パネル 71 行で「明細にある → AddedFrom 0 / 明細に無い → AddedFrom 1・工賃印 '*'」は例外なし
            _linked_ng = [x for x in _pp if not int(x.get('AddedFrom') or 0) == 1
                          and int(x.get('DisposalCode') or 0) not in linked_disp.get(str(x.get('PartsCode') or '').strip(), set())]
            if _linked_ng:
                raise ValueError(
                    '塗装パネル ' + ', '.join(f"{x['PartsCode']} {str(x.get('PanelName') or '').strip()}" for x in _linked_ng)
                    + ' を W/S 連動（AddedFrom 0）で書こうとしている。明細に同じ部品コードの取替/修理/板金行が無いので、'
                      'コグニで塗装ページを開くとこの行は消え、塗装計がその分不足する。'
                      'パネル追加（AddedFrom 1・工賃印 \'*\'）で書くか、明細の部品コードと塗装パネルのコードを揃える（判断規則 10-17）')
            _order = lambda x: (int(x.get('DisposalCode') or 0) == 9, '' if int(x.get('DisposalCode') or 0) == 9 else str(x['PartsCode']))  # noqa: E731  手入力の塗装行は最後（入力順のまま。sorted は安定）
            if _pp and [_order(x) for x in _pp] != sorted(_order(x) for x in _pp):
                cur.execute('DELETE FROM PaintingPanel')
                for n_, rec_ in enumerate(sorted(_pp, key=_order), start=1):  # コグニは塗装パネル行を部品コード昇順で保存する（工場 NEO 5 本・再検索 C06）
                    rec_['RecordNo'] = n_; rec_['LineNo'] = n_ - 1  # LineNo は 0 始まり（再検索 C06: 0,1,2,3,4）
                    cur.execute(f"INSERT INTO PaintingPanel ({','.join(pcols)}) VALUES ({','.join('?' * len(pcols))})", [rec_[c] for c in pcols])
            booth = pd.get('booth') or {}; base = pd.get('base') or {}; bb = sb = None  # 標準値は取れないことがある（汎用車種・CHM の無い車種）
            bt = float(booth.get('index') or 0); st = float(base.get('index') or 0)
            if pi and form:
                if n_p > 0:  # 部品コードのあるパネルが 0 枚（手入力の塗装行だけ・バンパだけ）なら加算基礎の標準は無い（ブースは枚数と無関係なので下で引く）
                    sb = pi.base_time(form, paint_c, coat_c, hf, n_p)
                    if base.get('index') is None and sb is not None:
                        st = sb
                    elif sb is not None and abs(sb - st) > 0.05:
                        notes.append(f'加算基礎数値 見積 {st} / 標準 {sb}')
                bb = pi.booth_time(form, paint_c, coat_c, hf)  # None = 表に無い、0.0 = 高機能塗装でブース加算なし
                if booth.get('index') is None and bb:
                    bt = bb
            bw = int(booth.get('wage') or rp(bt)); sw = int(base.get('wage') or rp(st))
            booth_given = isinstance(booth, dict) and (_flag(booth.get('use'), 'paint.booth.use') or float(booth.get('index') or 0) > 0 or int(booth.get('wage') or 0) > 0)  # 文字列 "false" を真にしない（Codex 指摘）  # {index 0, wage 0} の置き場だけの booth は「無し」扱い
            booth_on = booth_given or (bb is not None and bool(hf))  # ブース使用は見積に booth（指数か工賃か use）があるときだけ（コグニ実機 2026-09-08 exp_paint_A: 既定は「ブース使用」オフ = BoothFlag 0 / 時間・工賃 -1）。高機能塗装時は BOOTH.DB が 0 でもフラグは立つ
            if not booth_on:
                bt = 0.0; bw = 0
            # 標準値欄には ADDATA の標準値を入れる（見積値ではない）。塗装パネルの Time / TimeStandard* と同じ流儀。
            # 標準が取れない車種では見積値をそのまま標準としておく（従来どおり）
            bt_std = bb if (bb is not None and booth_on) else bt
            st_std = sb if sb is not None else st
            bw_std = rp(bt_std) if bt_std is not None else bw   # 標準が 0.0 でも見積工賃で埋めない（0 は「加算なし」という有効値）
            sw_std = rp(st_std) if st_std is not None else sw
            _bumper_only = (n_rows == 0)  # 外板パネルの行が 1 行も無い（手入力の塗装行があればバンパ加算基礎は付かない。実案件 NEO）
            _base_manual_only = (n_p == 0 and n_rows > 0)  # 手入力の塗装行だけ: 加算基礎の標準は無い（BaseTimeStandard -1）。見積に加算基礎があれば手入力 '#' で書く
            if _base_manual_only and not (base.get('index') or base.get('wage')):
                st = st_std = 0.0; sw = sw_std = 0
            if _bumper_only:
                # 外板パネルが無い（バンパだけ塗装）: 加算基礎数値は無し（PaintingPlan.Base* = -1・印 ''）。代わりにバンパ加算基礎（BAN.DB）を BumperBase* に書く
                # （実機 2026-09-12 w66d_real: 0010 新品 2.0 + バンパ加算 0.5、TimeTotalBumper 2.5、Base* -1）。合計には 0 として扱う
                st = st_std = 0.0; sw = sw_std = 0
            if _bumper_only or (_base_manual_only and not st and not sw):
                _base_vals = (-1, -1, -1, -1, -1, -1, -1, -1, '')
            elif _base_manual_only:  # 手入力の塗装行だけで、見積に加算基礎がある（実案件 NEO: 標準 -1・印 '#'）
                _base_vals = (st if st else -1, -1, *t3(sw), -1, -1, -1, '#')
            else:
                _base_vals = (st, st_std, *t3(sw), *t3(sw_std), ('*' if ((sw and sw != sw_std) or _any_added) else ''))
            paint_name = {1: '速乾', 3: '２Ｋ', 4: '水性'}.get(paint_c, '２Ｋ')
            cur.execute('UPDATE PaintingPlan SET Paint=?, PaintName=?, HFPainting=?, HFPaintingName=?, BoothFlag=?, BoothTime=?, BoothTimeStandard=?, '
                        'BoothWageOutTax=?, BoothWageInTax=?, BoothWageTax=?, BoothWageStandardOutTax=?, BoothWageStandardInTax=?, BoothWageStandardTax=?, BoothWageByManual=?, '
                        'BaseTime=?, BaseTimeStandard=?, BaseWageOutTax=?, BaseWageInTax=?, BaseWageTax=?, BaseWageStandardOutTax=?, BaseWageStandardInTax=?, BaseWageStandardTax=?, BaseWageByManual=?, '
                        'MaterialRateType=1, MaterialRate=?, CalculateLevel_Panel=1, CalculateLevel_Base=1, CalculateLevel_Bumper=1',
                        (paint_c, paint_name, hf, HF_NAME.get(hf, 'しない'), 1 if booth_on else 0, bt if booth_on else -1, bt_std if booth_on else -1,
                         *(t3(bw) if booth_on else (-1, -1, -1)), *(t3(bw_std) if booth_on else (-1, -1, -1)), ('*' if (bw and bw != bw_std) else ''),
                         *_base_vals, float(pd.get('material_rate') or default_material_rate(paint_c, coat_c, hf) or 26)))
            bumper_w = 0; bumper_t = 0.0; bumper_ws = {'fb': 0, 'rb': 0}
            # バンパ部品（0010 / 3810）の明細行が暫定指数 '$'（標準工賃のまま）なら、バンパ塗装の工賃印も '$'（実機 2026-09-12 w66d_real: 0010 '$' → fb_WageByManual '$'、バンパ加算基礎も '$'。
            # cogni_R2: 0010 は Provisional '$' でも工賃手入力 '#' → fb は ''）。1 例からの推定
            _bumper_prov = {pfx: any(r.get('PartsCode') == code and r.get('WageByManual') == '$' for r in rows) for pfx, code in (('fb', '0010'), ('rb', '3810'))}
            for key, pfx in (('bumper_front', 'fb'), ('bumper_rear', 'rb')):
                fb = pd.get(key)
                if not fb:
                    continue
                w = int(fb.get('wage') or 0); t = float(fb.get('index') or 0)
                color = unicodedata.normalize('NFKC', fb.get('color') or '一色')
                col_code = {'一色': 0, '黒ライン': 1, '二色': 2}.get(color, 0); two = col_code == 2
                form = unicodedata.normalize('NFKC', fb.get('form') or ''); form_code = {'大型': 0, '標準': 1}.get(form, -1)
                method = unicodedata.normalize('NFKC', fb.get('method') or '新品')
                # コグニ バンパ画面 fb_Disposal: 0 なし / 1 新品 / 2 変形修正 / 4 外傷修正小 / 5 外傷修正大（N-ONE 保存版 NONE_bp*.neo 2026-09-05）
                if method not in BUMPER_DISPOSAL:
                    raise ValueError(f'バンパ塗装 method は {sorted(set(v[1] for v in BUMPER_DISPOSAL.values()))} のいずれか（{method}）')
                disp_code, disp_name = BUMPER_DISPOSAL[method]
                draft = 1 if (_flag(fb.get('draft'), 'paint.bumper_*.draft') and disp_code != 1) else 0  # 絞模様: 新品では選択不可。判断できない値は止める（検査側 inspect_estimate と同じ語彙）
                has_tbl = bool(pi and pi.has_bumper_table(paint_c))
                if has_tbl and disp_code == 3:
                    raise ValueError(f"{'F' if pfx == 'fb' else 'R'}バンパ塗装: この車種は <car>23.DB を持つので 外傷修正 は 外傷修正小 / 外傷修正大 を指定する")
                sbt = pi.bumper_time(pfx == 'fb', coat_c, disp_name, two, paint=paint_c) if pi else None  # 水性(4)は <car>93.DB
                if sbt is None and pi is not None and disp_code != 0 and not has_tbl:  # 車種別の表が無い車種だけ汎用表（表はあるが値が無い行は標準なしのまま）
                    # 23.DB の無い車種は COM/FBANPA.DB（形状 大型/標準 × 一色/黒ライン/二色、外傷修正は 小/大 の区別なし = fb_Disposal 3。コグニ実機 J69 2026-09-06 夕）
                    g_ = pi.bumper_time_generic(coat_c, disp_name, form_code if form_code >= 0 else 0, col_code)
                    if g_ is not None:
                        sbt = g_
                        if disp_code in (4, 5):
                            disp_code, disp_name = 3, '外傷修正'
                        if form_code < 0:
                            form_code, form = 0, '大型'  # 汎用表は形状が必須（未指定は大型）
                if sbt is not None and draft:
                    sbt = round(sbt + BUMPER_DRAFT_ADD, 1)  # 絞模様有り +0.4（実機: 変形/外傷小/外傷大・ソリッド/パールとも一定）
                if not t:
                    if sbt is None:
                        raise ValueError(f"{'F' if pfx == 'fb' else 'R'}バンパ塗装 {disp_name}: 標準指数が <car>23.DB に無い（index を明示するか車種データを確認）")
                    t = sbt
                if not w and rate:
                    w = rp(t)
                if sbt is not None and abs(sbt - t) > 0.05:
                    notes.append(f"{'F' if pfx == 'fb' else 'R'}バンパ 指数 見積 {t} / 標準 {sbt}")
                bumper_w += w; bumper_t += t; bumper_ws[pfx] = w
                # コグニのバンパ画面: 形状 0=大型/1=標準（未選択 -1）、カラー 0=一色/1=黒ライン/2=二色、絞模様 0=無し/1=有り（コグニ保存版で確認）
                t_std = sbt if sbt is not None else t
                w_std = rp(sbt) if (sbt is not None and rate) else w
                cur.execute(f"UPDATE PaintingBumper SET {pfx}_Disposal=?, {pfx}_Name=?, {pfx}_Form=?, {pfx}_FormName=?, {pfx}_Color=?, {pfx}_ColorName=?, {pfx}_Draft=?, {pfx}_DraftName=?, {pfx}_Time=?, {pfx}_TimeStandard=?, "
                            f"{pfx}_WageOutTax=?, {pfx}_WageInTax=?, {pfx}_WageTax=?, {pfx}_WageStandardOutTax=?, {pfx}_WageStandardInTax=?, {pfx}_WageStandardTax=?, {pfx}_WageByManual=?",
                            (disp_code, disp_name, form_code, form if form_code >= 0 else '', col_code, color, (-1 if disp_code == 1 else draft), ('' if disp_code == 1 else ('有り' if draft else '無し')), t, t_std, *t3(w), *t3(w_std),  # 新品は 絞模様 -1（コグニ実機 2026-09-08 exp_paint_B/C2）
                             ('*' if w != w_std else ('$' if (_bumper_only and _bumper_prov.get(pfx)) else ''))))  # '$' の伝播は外板パネル 0 枚のときだけ（panels ありの既存出力は変えない。Codex 指摘）
            if _bumper_only and (bumper_ws['fb'] or bumper_ws['rb']):
                # バンパ加算基礎（COM/BAN.DB: 車形 × 塗膜クラス）。外板パネルが無いときだけ。合計（TimeTotalBumper / WageTotalBumper）に含める
                _form_car = str(getattr(self, '_car_form', '') or '')  # `form` はバンパのループで形状（大型/標準）に上書きされているので車形は取り直す
                bbt = pi.bumper_base_time(_form_car, coat_c) if (pi and _form_car) else None
                bb_ = pd.get('bumper_base') or {}
                t_ = float(bb_.get('index') or 0) or bbt
                if t_ is None:
                    raise ValueError('バンパ加算基礎（COM/BAN.DB）が取れない車種。paint.bumper_base.index を指定してください')
                w_ = int(bb_.get('wage') or 0) or rp(t_)
                t_std = bbt if bbt is not None else t_; w_std = rp(t_std) if rate else w_
                flag = '*' if w_ != w_std else ('$' if any(_bumper_prov.get(p_) for p_ in ('fb', 'rb') if bumper_ws.get(p_)) else '')  # 今回塗ったバンパの明細行だけを見る（塗らない側の暫定 '$' は拾わない。Codex 指摘）
                cur.execute('UPDATE PaintingPlan SET BumperBaseTime=?, BumperBaseTimeStandard=?, BumperBaseWageOutTax=?, BumperBaseWageInTax=?, BumperBaseWageTax=?, '
                            'BumperBaseWageStandardOutTax=?, BumperBaseWageStandardInTax=?, BumperBaseWageStandardTax=?, BumperBaseWageByManual=?',
                            (t_, t_std, *t3(w_), *t3(w_std), flag))
                if bbt is not None and abs(bbt - t_) > 0.05:
                    notes.append(f'バンパ加算基礎 見積 {t_} / 標準 {bbt}')
                bumper_w += w_; bumper_t += t_
            etc_w = 0; etc_t = 0.0
            wax = pd.get('wax')
            if wax:
                w = int(wax.get('wage') or 0); t = float(wax.get('index') or 0)
                cnt = int(wax.get('count') or 1)
                if not t:
                    t = round(0.1 * cnt, 1)
                if not w and rate:
                    w = rp(t)
                etc_w += w; etc_t += t
                cur.execute('UPDATE PaintingEtcetera SET ARWax=?, ARWaxTime=?, ARWaxTimeStandard=?, ARWaxWageOutTax=?, ARWaxWageInTax=?, ARWaxWageTax=?, '
                            'ARWaxWageStandardOutTax=?, ARWaxWageStandardInTax=?, ARWaxWageStandardTax=?', (cnt, t, t, *t3(w), *t3(w)))
            # --- 付加塗装（コグニ 塗装→付加塗装 タブ、COM/fukaetc.DB・2TONE.DB。N-ONE 実機 2026-09-05 で確認）
            def _cnt(v, key, name, default=1):
                c = v.get(key)
                c = default if c is None or c == '' else _int_strict(c, f'付加塗装 {name}: {key}')
                if c < 1:
                    raise ValueError(f'付加塗装 {name}: {key} は 1 以上で指定する（{c}）')
                return c

            def _std(val, name):
                if val is None:
                    raise ValueError(f'付加塗装 {name}: 標準指数が COM 表に無い（index を明示するか reference/ の DB を確認）')
                return val
            def _etc_values(v, t_std, name):
                """付加塗装 1 項目の (Time, TimeStandard, Wage, WageStandard, WageByManual)。index/wage を明示すれば手入力、無ければ標準"""
                t = float(v.get('index') or 0) or t_std
                w_std = rp(t_std) if rate else 0
                w = int(v.get('wage') or 0) or (rp(t) if rate else 0)  # 手入力指数なら実指数から工賃を計算（パネル・バンパと同じ）
                if abs(t - t_std) > 0.05:
                    notes.append(f'付加塗装 {name}: 指数 見積 {t} / 標準 {t_std}')
                return t, t_std, w, w_std, ('*' if w != w_std else '')

            def _etc_update(col, cnt, vals):
                t, t_std, w, w_std, man = vals
                cur.execute(f'UPDATE PaintingEtcetera SET {col}=?, {col}Time=?, {col}TimeStandard=?, {col}WageOutTax=?, {col}WageInTax=?, {col}WageTax=?, '
                            f'{col}WageStandardOutTax=?, {col}WageStandardInTax=?, {col}WageStandardTax=?, {col}WageByManual=?', (cnt, t, t_std, *t3(w), *t3(w_std), man))
                return t, w
            for key, col, label in (('door_sash', 'DSBlack', 'ドアサッシュ黒塗り'), ('stripe', 'BStripe', 'ボデーストライプ')):
                v = pd.get(key)
                if not v:
                    continue
                cnt = _cnt(v, 'count', label)
                ft_ = fukaetc_time(key, cnt, paint_c)
                if ft_ is None and key == 'door_sash' and paint_c == 4:
                    # 水性のドアサッシュ黒塗りは標準指数が無い（コグニ実機 J52 2026-09-06 夕: 枚数を入れても指数・工賃が空欄）。見積に index/wage があればそれを手入力として書く
                    if v.get('index') or v.get('wage'):
                        t = float(v.get('index') or 0); w = int(v.get('wage') or 0) or (rp(t) if (rate and t) else 0)
                        cur.execute(f'UPDATE PaintingEtcetera SET {col}=?, {col}Time=?, {col}TimeStandard=-1, {col}WageOutTax=?, {col}WageInTax=?, {col}WageTax=?, '
                                    f'{col}WageStandardOutTax=-1, {col}WageStandardInTax=-1, {col}WageStandardTax=-1, {col}WageByManual=?', (cnt, t if t else -1, *t3(w), '*'))  # 標準は無い（-1）まま手入力
                        etc_w += w; etc_t += t
                        notes.append(f'付加塗装 {label}: 水性は標準指数なし → 見積の指数/工賃を手入力（*）')
                    else:
                        cur.execute(f'UPDATE PaintingEtcetera SET {col}=?, {col}Time=-1, {col}TimeStandard=-1, {col}WageOutTax=-1, {col}WageInTax=-1, {col}WageTax=-1, {col}WageStandardOutTax=-1, {col}WageStandardInTax=-1, {col}WageStandardTax=-1, {col}WageByManual=\'\'', (cnt,))
                        notes.append(f'付加塗装 {label}: 水性は標準指数なし（枚数 {cnt} のみ）')
                    continue
                t, w = _etc_update(col, cnt, _etc_values(v, _std(ft_, label), label))
                etc_w += w; etc_t += t
            lc = pd.get('low_cover')
            if lc:
                roof_name = unicodedata.normalize('NFKC', str(lc.get('roof') or 'なし'))
                if roof_name not in ('なし', '取替', '修理'):
                    raise ValueError(f'付加塗装 低隠蔽性塗色: roof は なし/取替/修理 のいずれか（{roof_name}）')
                roof = {'なし': 0, '取替': 1, '修理': 2}[roof_name]
                n_ch = _int_strict(lc.get('change') or 0, '付加塗装 低隠蔽性塗色: change'); n_rp = _int_strict(lc.get('repair') or 0, '付加塗装 低隠蔽性塗色: repair')
                if n_ch < 0 or n_rp < 0 or (roof == 0 and n_ch == 0 and n_rp == 0):
                    raise ValueError(f'付加塗装 低隠蔽性塗色: roof/change/repair の指定が不正（{lc}）')
                t, t_std, w, w_std, man = _etc_values(lc, low_cover_time(roof, n_ch, n_rp, paint_c), '低隠蔽性塗色')
                etc_w += w; etc_t += t
                cur.execute('UPDATE PaintingEtcetera SET LCColorFlag=1, LCColorRoof=?, LCColorOtherChange=?, LCColorOtherRepair=?, LCColorTime=?, LCColorTimeStandard=?, '
                            'LCColorWageOutTax=?, LCColorWageInTax=?, LCColorWageTax=?, LCColorWageStandardOutTax=?, LCColorWageStandardInTax=?, LCColorWageStandardTax=?, LCColorWageByManual=?',
                            (roof, n_ch if n_ch > 0 else -1, n_rp if n_rp > 0 else -1, t, t_std, *t3(w), *t3(w_std), man))
            tcs = pd.get('two_coat_solid')
            if tcs:
                if coat_c != 1:
                    raise ValueError(f'付加塗装 2コートソリッド: 塗膜がソリッド(1)のときだけ指定できる（coat={coat_c}）')
                roof_f = 1 if _truthy(tcs.get('roof')) else 0
                n_o = _int_strict(tcs.get('count') or 0, '付加塗装 2コートソリッド: count')
                if n_o < 0 or (roof_f == 0 and n_o == 0):
                    raise ValueError(f'付加塗装 2コートソリッド: roof か count(1 以上) を指定する（{tcs}）')
                t, t_std, w, w_std, man = _etc_values(tcs, two_coat_solid_time(roof_f, n_o, paint_c), '2コートソリッド')
                etc_w += w; etc_t += t
                cur.execute('UPDATE PaintingEtcetera SET TwoCSolidFlag=1, TwoCSolidRoof=?, TwoCSolidOther=?, TwoCSolidTime=?, TwoCSolidTimeStandard=?, '
                            'TwoCSolidWageOutTax=?, TwoCSolidWageInTax=?, TwoCSolidWageTax=?, TwoCSolidWageStandardOutTax=?, TwoCSolidWageStandardInTax=?, TwoCSolidWageStandardTax=?, TwoCSolidWageByManual=?',
                            (roof_f, n_o if n_o > 0 else -1, t, t_std, *t3(w), *t3(w_std), man))
            tt = pd.get('two_tone')
            if tt:
                low_name = unicodedata.normalize('NFKC', str(tt.get('coat') or 'ソリッド'))
                if low_name not in COAT_CODES:
                    raise ValueError(f'付加塗装 2トーン加算: 下部塗膜 coat は {list(COAT_CODES)} のいずれか（{low_name}）')
                low_c = COAT_CODES[low_name]
                low_name = COAT_DISPLAY[low_c]  # 保存はコグニ表記（全角数字）
                cnt = _cnt(tt, 'count', '2トーン加算')
                if cnt > 5:
                    raise ValueError(f'付加塗装 2トーン加算: 枚数は 1〜5（{cnt}）')
                cur.execute('UPDATE PaintingPlan SET TwoToneFlag=1, TwoToneCoat=?, TwoToneCoatName=?, TwoToneMaterialRate=?', (low_c, low_name, float(tt.get('material_rate') or 12)))
                t, w = _etc_update('TwoTone', cnt, _etc_values(tt, _std(two_tone_time(paint_c, coat_c, low_c, cnt), '2トーン加算'), '2トーン加算'))
                etc_w += w; etc_t += t
            other_w = bw + sw; other_t = round(bt + st, 1)
            wage_total_p = int(panel_wage + bumper_w + etc_w + other_w)
            if paint_total and wage_total_p != paint_total:
                notes.append(f'塗装工賃計 明細 {wage_total_p} / 見積 {paint_total}')
            mr = float(pd.get('material_rate') or default_material_rate(paint_c, coat_c, hf) or 26)
            if not paint_material:
                paint_material = material_default(wage_total_p, mr, pd.get('material_round'))
                mat_auto_rate = mr
                if pd.get('material_rate') in (None, ''):  # 見積に材料代も割合も無い → 既定の割合で計算した（どの表の値かを残す）
                    _src = ('ガイドライン表の 6500〜 列' if guideline_material_rate(paint_c, coat_c, hf) is not None
                            else 'この PC のコグニ既定（ガイドライン表が無い）')
                    notes.append(f'材料代割合 {mr:g}%（見積に材料代も割合も無い → {_src}）')
            mt_in, mt_tax = tax_of(paint_material)
            pt_all = wage_total_p + paint_material; pt_in, pt_tax = tax_of(pt_all)
            cur.execute('UPDATE PaintingTotal SET TimeTotalPanel=?, TimeTotalBumper=?, TimeTotalFrame=0, TimeTotalEtcetera=?, TimeTotalOther=0, TimeTotal=?, '
                        'WageTotalPanelOutTax=?,WageTotalPanelInTax=?,WageTotalPanelTax=?, WageTotalBumperOutTax=?,WageTotalBumperInTax=?,WageTotalBumperTax=?, '
                        'WageTotalFrameOutTax=0,WageTotalFrameInTax=0,WageTotalFrameTax=0, WageTotalEtceteraOutTax=?,WageTotalEtceteraInTax=?,WageTotalEtceteraTax=?, '
                        'WageTotalOtherOutTax=0,WageTotalOtherInTax=0,WageTotalOtherTax=0, WageTotalOutTax=?,WageTotalInTax=?,WageTotalTax=?, WageTotalByManual="", '
                        'MaterialTotalOutTax=?,MaterialTotalInTax=?,MaterialTotalTax=?, MaterialTotalbyManual=?, TotalOutTax=?,TotalInTax=?,TotalTax=?',
                        (round(panel_time, 1), round(bumper_t, 1), round(etc_t, 1), round(panel_time + bumper_t + etc_t + other_t - bt, 1),  # ブース指数は指数計に含めない（コグニ保存版で確認）
                         *t3(panel_wage), *t3(bumper_w), *t3(etc_w), *t3(wage_total_p),
                         paint_material, mt_in, mt_tax, ('*' if pd.get('material') else ''), pt_all, pt_in, pt_tax))
            self._paint_notes = notes
            for n_ in notes:
                print('塗装:', n_)
            paint_total = pt_all
        else:
            pn_in, pn_tax = tax_of(paint_total)
            mt_in, mt_tax = tax_of(paint_material)
            pt_all = paint_total + paint_material
            pt_in, pt_tax = tax_of(pt_all)
            _pdx0 = (estimate or {}).get('paint') or {}
            _adds = [k for k in PAINT_DETAIL_KEYS + ('frame', 'sealing', 'other') if _pdx0.get(k)]
            _jitsu = (_truthy(_pdx0.get('actual')) or unicodedata.normalize('NFKC', str(_pdx0.get('input_type') or '')).strip() == '実額') and not _adds
            if _jitsu:
                # コグニの塗装「実額」入力（その他 → 塗装 → 入力方式）。総額を 1 つの金額で入れる方式で、
                # 外板パネル・加算基礎・ブース・材料代の内訳を持たない（実案件 NEO 400 本中 91 本がこれ。NEO 仕様 §塗装）。
                # 工場見積が「塗装費用 一式」しか出していない案件を、印字どおりに 1 行で書くために使う
                cur.execute('UPDATE PaintingPlan SET InputType=0, InputTypeName=?, BoothFlag=0, '
                            'BoothTime=-1, BoothTimeStandard=-1, BoothWageOutTax=-1, BoothWageInTax=-1, BoothWageTax=-1, '
                            'BoothWageStandardOutTax=-1, BoothWageStandardInTax=-1, BoothWageStandardTax=-1, BoothWageByManual="", '
                            'BaseTime=-1, BaseTimeStandard=-1, BaseWageOutTax=-1, BaseWageInTax=-1, BaseWageTax=-1, '
                            'BaseWageStandardOutTax=-1, BaseWageStandardInTax=-1, BaseWageStandardTax=-1, BaseWageByManual=""', ('実額',))
                cur.execute('UPDATE PaintingTotal SET TimeTotalPanel=0,TimeTotalBumper=0,TimeTotalFrame=0,TimeTotalEtcetera=0,TimeTotalOther=0,TimeTotal=0,'
                            'WageTotalPanelOutTax=0,WageTotalPanelInTax=0,WageTotalPanelTax=0, WageTotalBumperOutTax=0,WageTotalBumperInTax=0,WageTotalBumperTax=0,'
                            'WageTotalFrameOutTax=0,WageTotalFrameInTax=0,WageTotalFrameTax=0, WageTotalEtceteraOutTax=0,WageTotalEtceteraInTax=0,WageTotalEtceteraTax=0,'
                            'WageTotalOtherOutTax=0,WageTotalOtherInTax=0,WageTotalOtherTax=0, WageTotalOutTax=0,WageTotalInTax=0,WageTotalTax=0, WageTotalByManual="",'
                            'MaterialTotalOutTax=0,MaterialTotalInTax=0,MaterialTotalTax=0,MaterialTotalbyManual="", TotalOutTax=?,TotalInTax=?,TotalTax=?',
                            (pt_all, pt_in, pt_tax))
                if paint_material:   # 実額は内訳を持たない欄なので、材料代は総額に含めて 1 つにする
                    self._paint_notes = (getattr(self, '_paint_notes', None) or []) + [
                        f'塗装は実額入力（総額 {pt_all:,} 円）。材料代 {paint_material:,} 円は内訳を持てないので総額に含めた']
                    print(f'塗装: 実額入力（総額 {pt_all:,} 円）。材料代 {paint_material:,} 円は総額に含めた')
                paint_total = pt_all
                paint_material = 0
            else:
                cur.execute('UPDATE PaintingOther SET Name=?, Time=-1, WageOutTax=?, WageInTax=?, WageTax=?, WageByManual="*" WHERE LineNo=0',
                            ('塗装費用(工場見積)', paint_total, pn_in, pn_tax))
                cur.execute('UPDATE PaintingTotal SET WageTotalOtherOutTax=?,WageTotalOtherInTax=?,WageTotalOtherTax=?,WageTotalOutTax=?,WageTotalInTax=?,WageTotalTax=?,'
                            'MaterialTotalOutTax=?,MaterialTotalInTax=?,MaterialTotalTax=?,MaterialTotalbyManual=?,TotalOutTax=?,TotalInTax=?,TotalTax=?',
                            (paint_total, pn_in, pn_tax, paint_total, pn_in, pn_tax, paint_material, mt_in, mt_tax, ('*' if paint_material else ''), pt_all, pt_in, pt_tax))
                paint_total = pt_all
        # --- 塗装の追加要素（内板骨格塗装 / ボデーシーリング / 追加項目）: コグニ実験保存版（CHR_ETO_exp）と同形
        est = estimate or {}; pdx = est.get('paint') or {}
        rate_x = int(getattr(self, '_labor_rate', 0) or 0)
        form_x = str(getattr(self, '_car_form', '') or '')
        def rp2(x):
            return r10(int(round((x or 0) * 10)) * rate_x / 10) if rate_x and x else 0
        add_frame_t = add_etc_t = add_other_t = 0.0; add_frame_w = add_etc_w = add_other_w = 0
        pf = pdx.get('frame') or {}
        if pf:
            nk = {}
            for l in _xor_lines_ref('NAIKOKUA.DB'):
                f = [x.strip() for x in l.split(',')]
                if len(f) >= 5 and f[1] == form_x:
                    nk[f[2]] = int(f[3]) / 100.0
            for key, pfx, nos in (('engine_room', 'er', {1: '01', 2: '02', 3: '03'}), ('front_pillar', 'fp', {1: '04', 2: '05'}),
                                  ('center_pillar', 'cp', {1: '06', 2: '07'}), ('rear_floor', 'rp', {1: '08', 2: '09'})):
                v = pf.get(key)
                if not v:
                    continue
                sel = int(v.get('option', 1) if isinstance(v, dict) else v)
                t = float((v.get('index') if isinstance(v, dict) else 0) or nk.get(nos.get(sel, ''), 0))
                w = int((v.get('wage') if isinstance(v, dict) else 0) or rp2(t))
                cur.execute(f'UPDATE PaintingFrame SET {pfx}_Disposal=?, {pfx}_Time=?, {pfx}_TimeStandard=?, {pfx}_WageOutTax=?, {pfx}_WageInTax=?, {pfx}_WageTax=?, '
                            f'{pfx}_WageStandardOutTax=?, {pfx}_WageStandardInTax=?, {pfx}_WageStandardTax=?', (sel, t, t, *t3i(w), *t3i(w)))
                add_frame_t += t; add_frame_w += w
        seal = pdx.get('sealing')
        if seal:
            cnt = float(seal.get('m') or 1); t = float(seal.get('index') or round(0.1 * cnt, 1)); w = int(seal.get('wage') or rp2(t))
            cur.execute('UPDATE PaintingEtcetera SET BSealing=?, BSealingTime=?, BSealingTimeStandard=?, BSealingWageOutTax=?, BSealingWageInTax=?, BSealingWageTax=?, '
                        'BSealingWageStandardOutTax=?, BSealingWageStandardInTax=?, BSealingWageStandardTax=?', (int(cnt), t, t, *t3i(w), *t3i(w)))
            add_etc_t += t; add_etc_w += w
        for o in pdx.get('other') or []:
            nm = unicodedata.normalize('NFKC', o.get('name', '')).replace('ー', '-')
            line = None
            for ln, n_ in cur.execute('SELECT LineNo, Name FROM PaintingOther').fetchall():
                tn = unicodedata.normalize('NFKC', n_).replace('ー', '-')
                if tn and (tn in nm or nm in tn):
                    line = ln; break
            if line is None:  # テンプレートに無い追加項目は空き行（Time=-1）へ名称ごと入れる
                free = [ln for ln, tm in cur.execute('SELECT LineNo, Time FROM PaintingOther ORDER BY LineNo').fetchall() if (tm is None or tm < 0) and ln > 0]
                if not free:
                    raise ValueError(f'塗装追加項目の空き行が無い: {o.get("name")}')
                line = free[-1]
                cur.execute('UPDATE PaintingOther SET Name=? WHERE LineNo=?', (_fit(hw(str(o.get('name') or '')), 20), line))  # 追加塗装の工程名も半角カナ（実機 2,915 行中 2,864 行）
                notes.append(f'塗装追加項目 {o.get("name")} はテンプレートに無いため行 {line} に名称を入れて計上')
            t = float(o.get('index') or 0); w = int(o.get('wage') or rp2(t))
            cur.execute('UPDATE PaintingOther SET Time=?, WageOutTax=?, WageInTax=?, WageTax=?, WageByManual="#" WHERE LineNo=?', (t, *t3i(w), line))
            add_other_t += t; add_other_w += w
        if add_frame_w or add_etc_w or add_other_w:
            cur_pt = cur.execute('SELECT * FROM PaintingTotal'); ptc = [d[0] for d in cur_pt.description]; ptr = dict(zip(ptc, cur_pt.fetchone()))
            fr_w = int(ptr['WageTotalFrameOutTax'] or 0) + add_frame_w; et_w = int(ptr['WageTotalEtceteraOutTax'] or 0) + add_etc_w; ot_w = int(ptr['WageTotalOtherOutTax'] or 0) + add_other_w
            wt = int(ptr['WageTotalOutTax'] or 0) + add_frame_w + add_etc_w  # 追加項目(Other) は WageTotal に含めず Total にだけ加算（実 NEO と同じ）
            mat = int(ptr['MaterialTotalOutTax'] or 0); mat_before = mat
            if mat_auto_rate is not None:  # 材料代 = 区分ごとの round10(工賃×割合) の合計。見積書に材料代が無いときは追加した骨格・付加塗装分も含めて再計算
                new_mat = material_default(wt, mat_auto_rate, pdx.get('material_round'))
                if new_mat != mat:
                    cur.execute('UPDATE PaintingTotal SET MaterialTotalOutTax=?,MaterialTotalInTax=?,MaterialTotalTax=?', t3i(new_mat))
                    paint_total += new_mat - mat
                    mat = new_mat; paint_material = new_mat
            cur.execute('UPDATE PaintingTotal SET TimeTotalFrame=?, TimeTotalEtcetera=?, TimeTotalOther=?, TimeTotal=?, '
                        'WageTotalFrameOutTax=?,WageTotalFrameInTax=?,WageTotalFrameTax=?, WageTotalEtceteraOutTax=?,WageTotalEtceteraInTax=?,WageTotalEtceteraTax=?, '
                        'WageTotalOtherOutTax=?,WageTotalOtherInTax=?,WageTotalOtherTax=?, WageTotalOutTax=?,WageTotalInTax=?,WageTotalTax=?, TotalOutTax=?,TotalInTax=?,TotalTax=?',
                        (round((ptr['TimeTotalFrame'] or 0) + add_frame_t, 1), round((ptr['TimeTotalEtcetera'] or 0) + add_etc_t, 1), round((ptr['TimeTotalOther'] or 0) + add_other_t, 1),
                         round((ptr['TimeTotal'] or 0) + add_frame_t + add_etc_t, 1),
                         *t3i(fr_w), *t3i(et_w), *t3i(ot_w), *t3i(wt),
                         *t3i(int(ptr['TotalOutTax'] or 0) + add_frame_w + add_etc_w + add_other_w + (mat - mat_before))))  # 既存 Total（一括塗装費は WageTotal と Other の両方に入っている）＋追加分
            paint_total += add_frame_w + add_etc_w + add_other_w
        _itype = unicodedata.normalize('NFKC', str((pdx or {}).get('input_type') or '')).strip()
        if (_itype == '実額' or _truthy((pdx or {}).get('actual'))) and not _jitsu:
            # 塗装の入力方式「実額」を**指定された**のに、内訳（パネル・バンパ・加算基礎・内板骨格塗装・追加項目 等）が
            # 残っていて上の一括計上の枝を通らなかったとき: ここで実額の形に書き直す。
            # 総額は**計算し終えた塗装計（材料込）**そのものなので、金額は動かない（アプリの方針。2026-09-20 亮平さん指示）
            _row = cur.execute('SELECT TotalOutTax FROM PaintingTotal').fetchone()
            _tot_j = int((_row[0] if _row else 0) or 0)
            cur.execute('UPDATE PaintingPlan SET InputType=0, InputTypeName=?, BoothFlag=0, '
                        'BoothTime=-1, BoothTimeStandard=-1, BoothWageOutTax=-1, BoothWageInTax=-1, BoothWageTax=-1, '
                        'BoothWageStandardOutTax=-1, BoothWageStandardInTax=-1, BoothWageStandardTax=-1, BoothWageByManual="", '
                        'BaseTime=-1, BaseTimeStandard=-1, BaseWageOutTax=-1, BaseWageInTax=-1, BaseWageTax=-1, '
                        'BaseWageStandardOutTax=-1, BaseWageStandardInTax=-1, BaseWageStandardTax=-1, BaseWageByManual=""', ('実額',))
            cur.execute('UPDATE PaintingTotal SET TimeTotalPanel=0,TimeTotalBumper=0,TimeTotalFrame=0,TimeTotalEtcetera=0,TimeTotalOther=0,TimeTotal=0,'
                        'WageTotalPanelOutTax=0,WageTotalPanelInTax=0,WageTotalPanelTax=0, WageTotalBumperOutTax=0,WageTotalBumperInTax=0,WageTotalBumperTax=0,'
                        'WageTotalFrameOutTax=0,WageTotalFrameInTax=0,WageTotalFrameTax=0, WageTotalEtceteraOutTax=0,WageTotalEtceteraInTax=0,WageTotalEtceteraTax=0,'
                        'WageTotalOtherOutTax=0,WageTotalOtherInTax=0,WageTotalOtherTax=0, WageTotalOutTax=0,WageTotalInTax=0,WageTotalTax=0, WageTotalByManual="",'
                        'MaterialTotalOutTax=0,MaterialTotalInTax=0,MaterialTotalTax=0,MaterialTotalbyManual="", TotalOutTax=?,TotalInTax=?,TotalTax=?',
                        (_tot_j, *tax_of(_tot_j)))
            self._paint_notes = (getattr(self, '_paint_notes', None) or []) + [
                f'塗装は実額の指定があるので、計算した塗装計（材料込）{_tot_j:,} 円を総額 1 つで入れた（内訳の欄は 0）']
            print(f'塗装: 実額の指定 → 総額 {_tot_j:,} 円 1 つにした（内訳の欄は 0）')
            paint_total, paint_material = _tot_j, 0
        if _itype == '参考':
            # 塗装の入力方式「参考」（コグニ: その他 → 塗装 → 入力方式）。計算は指数と同じで、見積の位置づけだけが違う
            # （実案件 NEO 700 本に 1 本。パネル・バンパ・追加項目はそのまま、材料代は手入力の印が付く）
            cur.execute("UPDATE PaintingPlan SET InputType=2, InputTypeName='参考'")
        elif _itype and _itype not in ('指数', '実額'):
            raise ValueError(f"paint.input_type は 指数 / 実額 / 参考 のどれか: {(pdx or {}).get('input_type')!r}")
        # --- 内板骨格修正（内骨画面）: FramePlan = 基本修正作業（N_KIHON）, Frame = 部位区分×損傷ランク（N_KEI: A/B/C）
        nk_total = 0
        fr = est.get('frame') or {}
        if fr:
            kihon = 3.5
            for l in _xor_lines_ref('N_KIHON.DB'):
                f = [x.strip() for x in l.split(',')]
                if f and f[0] == form_x and len(f) >= 3:
                    kihon = int(f[2]) / 100.0
            keis = {}
            for l in _xor_lines_ref('N_KEI.DB'):
                f = [x.strip() for x in l.split(',')]
                if len(f) >= 7 and f[0] == form_x:
                    keis[f[2]] = (len(keis), int(f[3]) / 100.0, int(f[4]) / 100.0, int(f[5]) / 100.0, f[6])
            if _flag(fr.get('basic', True), 'frame.basic', default=True):  # 文字列 "false" を真にしない（Codex 指摘）
                t = float(fr.get('basic_index') or kihon); w = int(fr.get('basic_wage') or rp2(t))
                cur.execute('UPDATE FramePlan SET FrameFlag=1, PartsCode="1371", Time=?, TimeStandard=?, WageOutTax=?, WageInTax=?, WageTax=?, WageStandardOutTax=?, WageStandardInTax=?, WageStandardTax=?, WageByManual=""',
                            (t, t, *t3i(w), *t3i(w)))
                nk_total += w
            cur.execute('DELETE FROM Frame')
            for itf in fr.get('items') or []:
                code = str(itf.get('code', '')); rank = str(itf.get('rank', 'A')).upper(); ri = {'A': 0, 'B': 1, 'C': 2}.get(rank, 0)
                std = keis.get(code)
                t = float(itf.get('index') or (std[1 + ri] if std else 0)); w = int(itf.get('wage') or rp2(t))
                ln = std[0] if std else len(fr.get('items') or [])
                cur.execute('INSERT OR REPLACE INTO Frame (LineNo, PartsCode, PartsName, DamageRank, Time, TimeStandard, WageOutTax, WageInTax, WageTax, WageStandardOutTax, WageStandardInTax, WageStandardTax, WageByManual) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"")', (ln, code, itf.get('name') or (std[4] if std else ''), ri + 2, t, t, *t3i(w), *t3i(w)))
                nk_total += w
        # 費用: コグニ既定行（1-8 固定名, 9-36 任意名）へ割付。キーワードで既定行に寄せ、無ければ後方の行を名前ごと書き換える
        cur.execute('UPDATE Expense SET PartsEnabled=0,PartsPriceOutTax=0,PartsPriceInTax=0,PartsPriceTax=0,WageEnabled=0,WageOutTax=0,WageInTax=0,WageTax=0')
        fixed = {r[0]: r[1] for r in cur.execute('SELECT LineNo, Name FROM Expense')}
        # 費用名は **見積書の印字どおり**（全角なら全角）で reading に写す。draft が半角カナに直すので
        # 既定行に載る。ここで表記ゆれを一般に吸収してはいけない——実機は半角カナの費用名を既定行へ寄せず
        # 自由行に置くため（cogni_CXA の 'ﾚｯｶｰ' は自由行 36。2026-09-10 に確認）
        KEYMAP = [('文字書き', 1), ('内張', 2), ('配線', 3), ('ショートパーツ', 4), ('ｼｮｰﾄﾊﾟｰﾂ', 4), ('レッカー', 5), ('写真', 7),
                  ('エーミング', 13), ('ｴｰﾐﾝｸﾞ', 13), ('DTC', 26), ('診断', 26), ('スキャン', 26), ('光軸', 18), ('再設定', 18), ('リセット', 18),
                  ('アライメント', 10), ('ｱﾗｲﾒﾝﾄ', 10), ('清掃', 9), ('コーティング', 12), ('ｺｰﾃｨﾝｸﾞ', 12), ('防錆', 24), ('ソナー', 11)]
        used = set(); free_line = 36
        hy_parts = hy_wage = 0
        hy_parts_nt = hy_wage_nt = 0  # 非課税（OutTaxFlag=1）
        for ex in expenses:
            amt = _money(ex.get('amount'), f"費用 '{ex.get('name', '')}' の金額")
            if amt < 0:
                raise ValueError(f"費用 '{ex.get('name', '')}' の金額が負（{amt}）。値引きは discount に書く")
            if amt == 0:
                continue
            nm = ex.get('name', '')
            kind_ = 'parts' if ex.get('kind') == 'parts' else 'wage'
            line = next((ln for kw, ln in KEYMAP if kw in nm and (ln, kind_) not in used), None)
            if line is None:  # 既に別種別で使った任意行と同名なら同じ行に載せる
                line = next((ln for (ln, k_) in used if k_ != kind_ and ln > 8 and unicodedata.normalize('NFKC', fixed.get(ln, '')) == unicodedata.normalize('NFKC', nm)), None)
            if line is None:
                while any(u[0] == free_line for u in used) and free_line > 9:
                    free_line -= 1
                if free_line < 9 or any(u[0] == free_line for u in used):  # 空き行が尽きた: 黙って同じ行に重ねると Expense 表と合計欄が食い違う（監査 8）
                    raise ValueError(f"費用の行が足りない（自由に使える行は 9〜36 の 28 行）。'{nm}' を入れられない。費用をまとめるか、明細の手入力行にする")
                line = free_line; free_line -= 1
                # Name TEXT(20) はバイト長。自由行の名前は半角カナ（判断規則 10-26。既定行 1〜8 の照合（上の KEYMAP）は
                # 印字どおりの全角で当てるので、**行を決めたあとに**半角へ直す
                nm20 = _fit(hw(nm), 20)
                cur.execute('UPDATE Expense SET Name=? WHERE LineNo=?', (nm20, line)); fixed[line] = nm20
            used.add((line, kind_))
            it_, tx = tax_of(amt)
            tf = 1 if _flag(ex.get('taxfree'), 'expenses[].taxfree') else 0  # 文字列 "false" を非課税にしない（Codex 指摘）
            if tf:
                it_, tx = amt, 0
            if ex.get('kind') == 'parts':
                cur.execute('UPDATE Expense SET PartsEnabled=1,PartsPriceOutTax=?,PartsPriceInTax=?,PartsPriceTax=?,OutTaxFlag=? WHERE LineNo=?', (amt, it_, tx, tf, line))
                if tf: hy_parts_nt += amt
                else: hy_parts += amt
            else:
                cur.execute('UPDATE Expense SET WageEnabled=1,WageOutTax=?,WageInTax=?,WageTax=?,OutTaxFlag=? WHERE LineNo=?', (amt, it_, tx, tf, line))
                if tf: hy_wage_nt += amt
                else: hy_wage += amt
        # 値引・割増（総合計画面）: pt_/wg_Extra。Flag 1=割増(+) 0=値引(−)、金額は絶対値
        disc = est.get('discount') or {}
        pt_x = int(disc.get('parts') or 0); wg_x = int(disc.get('wage') or 0)
        if pt_x or wg_x:
            cur.execute('UPDATE Total SET pt_ExtraTotalOutTax=?,pt_ExtraTotalInTax=?,pt_ExtraTotalTax=?, pt_ExtraFlag=?, pt_ExtraRate=-1, pt_ExtraUnit=1, pt_ExtraArrangeFlag=1, pt_IncludeRecycle=1, '
                        'wg_ExtraTotalOutTax=?,wg_ExtraTotalInTax=?,wg_ExtraTotalTax=?, wg_ExtraFlag=?, wg_ExtraRate=-1, wg_ExtraUnit=1, wg_ExtraArrangeFlag=1, wg_IncludeMaterial=1',
                        (*t3i(abs(pt_x)), 1 if pt_x > 0 else 0, *t3i(abs(wg_x)), 1 if wg_x > 0 else 0))
        # 合計
        for a_ in (getattr(self, '_adas_rows_written', None) or []):  # ADAS 作業の工賃は工賃計（ms_WageTotal）と小計に入る（実機: 8,000 円分 SubTotal も増加）。保留行は入らない
            wage_total += a_['wage']; wage_tax_sum += tax_of(a_['wage'])[1]
        sub = parts_total + wage_total + paint_total + nk_total + hy_parts + hy_wage + pt_x + wg_x
        _tr = str(getattr(self, '_tax_round', None) or '四捨五入')
        if abs(TAX - 0.1) < 1e-9:
            tx = (sub * 10) // 100 if _tr == '切り捨て' else (-((-sub * 10) // 100) if _tr == '切り上げ' else (sub * 10 + 50) // 100)  # 消費税の計算単位（既定 四捨五入。コグニ保存版 630,665→63,067。切り捨て/切り上げは消費税設定ダイアログ = Setting.tx_ArrangeFlag）
        else:
            tx = int(math.floor(sub * TAX + 0.5))  # 税率が 10% 以外のときの経路。round() は偶数丸め（125 → 12）なので使わない
        def t3(v): i, t = tax_of(v); return (v, i, t)
        cur.execute('UPDATE Total SET ms_RecyclePartsTotalOutTax=?,ms_RecyclePartsTotalInTax=?,ms_RecyclePartsTotalTax=?, nk_TotalOutTax=?,nk_TotalInTax=?,nk_TotalTax=?, '
                    'hy_PartsNoTaxTotalOutTax=?,hy_PartsNoTaxTotalInTax=?,hy_PartsNoTaxTotalTax=0, hy_WageNoTaxTotalOutTax=?,hy_WageNoTaxTotalInTax=?,hy_WageNoTaxTotalTax=0',
                    (*t3i(rc_total), *t3i(nk_total), hy_parts_nt, hy_parts_nt, hy_wage_nt, hy_wage_nt))
        cur.execute('UPDATE Total SET pt_IncludeRecycle=1, wg_IncludeMaterial=1')  # 総合計画面の既定（値引にリサイクル部品代・塗装材料代を含む）。コグニ新規見積 NEW3 と同じ
        cur.execute('UPDATE Total SET ms_PartsTotalOutTax=?,ms_PartsTotalInTax=?,ms_PartsTotalTax=?, ms_WageTotalOutTax=?,ms_WageTotalInTax=?,ms_WageTotalTax=?,'
                    'pn_TotalOutTax=?,pn_TotalInTax=?,pn_TotalTax=?, pn_MaterialTotalOutTax=?,pn_MaterialTotalInTax=?,pn_MaterialTotalTax=?,'
                    'hy_PartsTaxTotalOutTax=?,hy_PartsTaxTotalInTax=?,hy_PartsTaxTotalTax=?, hy_WageTaxTotalOutTax=?,hy_WageTaxTotalInTax=?,hy_WageTaxTotalTax=?,'
                    'tx_TotalOutTax=?, tx_TotalInTax=?, SubTotal=?, Total=?',
                    (parts_total, parts_total + parts_tax_sum, parts_tax_sum, wage_total, wage_total + wage_tax_sum, wage_tax_sum,
                     *t3(paint_total), *t3(paint_material), *t3(hy_parts), *t3(hy_wage), tx, tx, sub, sub + tx + hy_parts_nt + hy_wage_nt))
        totals = {'parts': parts_total, 'wage': wage_total, 'paint': paint_total, 'paint_material': paint_material, 'frame': nk_total, 'recycle': rc_total,
                  'expense_parts': hy_parts + hy_parts_nt, 'expense_wage': hy_wage + hy_wage_nt, 'discount': pt_x + wg_x, 'subtotal': sub, 'tax': tx, 'total': sub + tx + hy_parts_nt + hy_wage_nt}
        # AnNote.ini の [Reserve] / [Comment] Flag は、書き終わった ERParts から数える
        # （リサイクル置換行は CommentFlag を 0 にするので、置換前の rows で数えると 1 過大になる）
        self._has_reserve = cur.execute('SELECT COUNT(*) FROM ERParts WHERE ReserveFlag=1').fetchone()[0] > 0
        self._has_comment = cur.execute('SELECT COUNT(*) FROM ERParts WHERE CommentFlag=1').fetchone()[0] > 0
        return self._close(con, p), totals

    def _adas_rows(self, estimate: Optional[dict], n_reserve: int) -> list[dict]:
        """estimate['adas'] = [{code: '9900' | item: 'A010', sub: 1, index, wage, comment}] → ReserveERParts 行。
        指数・名称は <car>55.DB（基本 A010 → DisposalCode 1）/ 56.DB（センサ A1xx → DisposalCode 2）から。工賃 = 指数×単価（偶数丸め 10 円）。
        コグニ実機（W66 SIENTA_adas.neo 2026-09-05）: PartsNo=ItemNo, PartsNoStandard=ItemNoSub, WorkCode 3 スペース, ConstructGroup = 56.DB ItemNoSubCombiCode（無ければ 3 スペース）, Provisional/DamageArea/DamageRank 1 スペース, DamageRankBtn1 = 55/56.DB OrderNo（ADAS_D98c/D98d 2026-09-06）"""
        self._adas_notes = []  # 同じ NeoBuilder を使い回しても前の案件の通知を残さない（Codex 指摘）
        items = (estimate or {}).get('adas') or []
        if not items:
            return []
        car = str(getattr(self, '_car_code', '') or '')
        rate = int(getattr(self, '_labor_rate', 0) or 0)
        body = str(getattr(self, '_body_code', '') or '')
        try:
            from adas_db import AdasDB
            allrows = AdasDB(self.resolver.root).work_rows(car)
        except FileNotFoundError:
            allrows = []
        # 55/56.DB は BodyCode ごとに同じ PartsCode で指数が違うことがある: 車両の BodyCode の行を優先し、無ければ '00'（共通）の行
        table: dict = {}
        for r in allrows:
            if r['BodyCode'] == body or (r['BodyCode'] == '00' and r['PartsCode'] not in table):
                if r['BodyCode'] == body or r['PartsCode'] not in table:
                    table[r['PartsCode']] = r
        out = []
        for a in items:
            code = str(a.get('code') or '').strip()
            row = table.get(code)
            if code and row is None and a.get('item') and not a.get('index'):
                raise ValueError(f'ADAS 作業 {a}: code {code} が <car>55/56.DB（BodyCode {body!r}/00）に無い（item/sub で別コードに置き換えない）')
            if row is None and a.get('item') and not code:
                cand = [r for r in table.values() if r['ItemNo'] == str(a['item']) and str(r['ItemNoSub']) == str(a.get('sub') or '1')]
                if len(cand) > 1:  # 同じ項目番号/枝番に複数の PartsCode（例: 9931/9932 で指数違い）がある車種は code で指定させる
                    raise ValueError(f"ADAS 作業 {a}: item/sub に一致する作業が複数（{[r['PartsCode'] for r in cand]}）。code で指定する")
                row = cand[0] if cand else None
                code = row['PartsCode'] if row else code
            if row is None:
                if not (code and a.get('index') and a.get('item')):
                    raise ValueError(f'ADAS 作業 {a}: <car>55/56.DB（BodyCode {body!r}/00）に無い（code・item/sub・index をすべて明示すれば手入力で書ける）')
            if rate <= 0 and not a.get('wage'):
                raise ValueError(f'ADAS 作業 {a}: 工賃単価が無く wage も未指定')
            if row is not None and ((a.get('item') and str(a['item']) != row['ItemNo']) or (a.get('sub') is not None and str(a['sub']) != str(row['ItemNoSub']))):
                raise ValueError(f"ADAS 作業 {a}: code {code} は {row['ItemNo']}({row['ItemNoSub']}) であり item/sub と矛盾")
            if row is None and a.get('sub') is None:
                raise ValueError(f'ADAS 作業 {a}: 手入力の ADAS 行は sub（枝番）も必須')
            if a.get('index') is not None and str(a.get('index')).strip() != '' and float(a['index']) <= 0:
                raise ValueError(f'ADAS 作業 {a}: index は正の数で指定する')
            t = float(a.get('index') or (row['TimeHours'] if row else 0))
            if t <= 0:
                raise ValueError(f'ADAS 作業 {a}: 指数が無い')
            w_std = r10_even(t * rate) if rate else 0
            w = int(a.get('wage') or w_std)
            item = str(a.get('item') or (row['ItemNo'] if row else '')); sub = str(a.get('sub') or (row['ItemNoSub'] if row else '1'))
            # ADAS_D98c/D98d.neo（2026-09-06 夜、コグニ実機）: ReserveERParts.DamageRankBtn1 = 55/56.DB の OrderNo（作業選択ダイアログの行番号。9942 → 4、9904 → 3）、
            # ConstructGroup = 56.DB の ItemNoSubCombiCode（9940/9942 → 'A13'。無ければ 3 スペース）。手入力行（55/56.DB に無い）は OrderNo を持たないので 0
            order = 0
            if row is not None:
                try:
                    order = int(str(row.get('OrderNo') or '').strip() or 0)
                except ValueError:
                    raise ValueError(f"ADAS 作業 {a}: 55/56.DB の OrderNo が数値でない {row.get('OrderNo')!r}")
            combi = str(row.get('ItemNoSubCombiCode') or '').strip() if row is not None else ''
            if len(combi) > 3:
                raise ValueError(f'ADAS 作業 {a}: 56.DB の ItemNoSubCombiCode が 3 文字を超える {combi!r}')
            out.append({'code': code, 'item': item, 'sub': sub, 'dcode': 1 if item.startswith('A0') else 2, 'time': t, 'wage': w, 'wage_std': w_std,
                        'name': str(a.get('name') or (row['ItemName'] if row else '')), 'comment': str(a.get('comment') or ''),
                        'order': order, 'combi': combi.ljust(3)})
        # 表示順: 基本作業（A0xx）→ センサ別（A1xx）、項目番号順
        out.sort(key=lambda r: (r['dcode'], r['item'], int(r['sub']) if str(r['sub']).isdigit() else 99, str(r['sub'])))  # 枝番は数値順（'10' を '2' の前に置かない）
        # センサ別作業には 56.DB の前提作業（BasePartsCodes = 基本作業 A010 の PartsCode）がある。コグニの作業選択ダイアログはセンサ別作業を選ぶと
        # 基本作業を同時に付ける（実機 ADAS_D98c/D98d 2026-09-06）。生成器は見積書に書かれた作業だけを書くので、前提作業が無ければ知らせる（足しはしない）
        codes_out = {r_['code'] for r_ in out}
        for r_ in out:
            base = list((table.get(r_['code']) or {}).get('BasePartsCodes') or [])
            if r_['dcode'] == 2 and base and not (set(base) & codes_out):
                self._adas_notes.append(f"ADAS: センサ別作業 {r_['item']}({r_['sub']}) {r_['name'].strip()} の前提作業（56.DB BasePartsCodes {base}）が adas に無い。"
                                        "コグニは作業選択で基本作業（A010）を同時に付ける。見積書に基本作業が無ければそのままでよい")
        return out

    # ------------------------------------------------------------ AnSvIf
    def write_ansvif(self, db: bytes, car: dict, cust: dict, ins: dict, eva_codes: list[str], labor_rate: int, est_date: str) -> bytes:
        con, p = self._open(db)
        cur = con.cursor()
        car_cols = [r[1] for r in cur.execute('PRAGMA table_info(Car)')]
        upd = {k: car[k] for k in car_cols if k in car}
        tc = str(car.get('TrimCode') or '').strip()
        upd['TrimCodeFlag'] = 1 if tc else 0; upd['TrimCode'] = tc; upd['TrimName'] = ''; upd['TrimRGB'] = ''  # TRIM_W66b.neo: 29.DB から '20' を選ぶと Flag 1 / '20'、名称・RGB は空
        upd['ColorRGB2'] = ''; upd['Extension'] = int(car.get('Extension', 0) or 0)
        for k in ['UColorCode', 'UColorName', 'UColorRGB1', 'UColorRGB2', 'LColorCode', 'LColorName', 'LColorRGB1', 'LColorRGB2']:
            upd[k] = ''
        upd['PartsPriceDate'] = car.get('PartsPriceDate', upd.get('PartsPriceDate', ''))
        cur.execute('UPDATE Car SET ' + ','.join(f'{k}=?' for k in upd), list(upd.values()))
        # CarSearch: 車検証検索 (SearchMethod=3) の痕跡を実 NEO と同じ形で
        cs = {'SearchChangedFlag': 0, 'SearchMethod': 3, 'ms_YearSearchFlag': 0,
              'ps_CarMouldNo': car.get('ps_CarMouldNo', ''), 'ps_CarKindNo': car.get('ps_CarKindNo', ''),
              'ps_CarRegDate': car.get('ps_CarRegDate', ''), 'ev_CarCode': car['CarCode'], 'ev_YearCode': car['YearCode'],
              'ev_BodyCode': car['BodyCode'], 'ev_FVACode': car['FVACode'], 'ev_GradeCode': car['GradeCode'], 'ev_CarName': car['CarName'],
              'ev_TwoColorCodeFlag': 1, 'ev_ColorCode': car.get('ColorCode', ''), 'ev_ColorCodeByManual': (0 if car.get('ColorName') else 1), 'ev_ColorName': car.get('ColorName', ''),  # 26.DB の一覧から選んだ色は 0、一覧に無い手入力は 1（NEW1）
              'ev_ColorRGB1': car.get('ColorRGB1', ''), 'ev_ColorRGB2': '', 'ev_UColorCodeByManual': 0, 'ev_LColorCodeByManual': 0, 'ev_TrimCodeByManual': 0, 'ev_TrimCode': str(car.get('TrimCode') or '').strip(),
              'nm_PartsPriceDate': upd['PartsPriceDate'], 'nm_CarNameByUser': car['CarNameByUser'], 'nm_FVANameByUser': car['FVANameByUser'],
              'ps_YearSearchFlag': car.get('ps_YearSearchFlag', 0), 'ps_CarSerialNoHead': car.get('ps_CarSerialNoHead', ''),
              'ps_CarSerialNoTail': car.get('ps_CarSerialNoTail', ''), 'ps_CarSerialNo': car.get('ps_CarSerialNo', ''), 'ps_YearName': car.get('ps_YearName', ''), 'ws_YearName': ''}
        if cs['ps_CarRegDate']:
            era, ey = nc.get_era_info(cs['ps_CarRegDate']); cs['ps_CarRegEra'] = era; cs['ps_CarRegEraYear'] = ey
        for k in ['ms_MakerCode', 'ms_MakerName', 'ms_CarFormGroupCode', 'ms_CarNameCode', 'ms_CarNameName', 'ms_CarCode', 'ms_ModelName', 'ms_YearCode', 'ms_YearName',
                  'ms_CarSerialNoHead', 'ms_CarSerialNoTail', 'ms_BodyCode', 'ms_BodyName', 'ms_FVACode', 'ms_FVAName', 'ms_GradeCode', 'ms_GradeName', 'ms_GradeDivision',
                  'ms_GradeExHead', 'ms_GradeExDivision', 'ms_GradeExTail', 'ws_TapeCode', 'ws_A_CarMouldNo', 'ws_A_CarKindNo', 'ws_A_CarRegDate', 'ws_A_CarRegEra',
                  'ws_A_CarRegEraYear', 'ws_A_YearCode', 'ws_B_YearCode', 'ws_B_BodyCode', 'ws_B_GradeNo', 'ws_B_FVANoHead', 'ws_B_FVANoTail',
                  'ev_EVACodeNeed1', 'ev_EVACodeNeed2', 'ev_EVACodeNeed3', 'ev_EVACodeNeed4', 'ev_UColorCode', 'ev_UColorName', 'ev_UColorRGB1', 'ev_UColorRGB2',
                  'ev_LColorCode', 'ev_LColorName', 'ev_LColorRGB1', 'ev_LColorRGB2', 'ev_TrimCode', 'ev_TrimName', 'ev_TrimRGB']:
            cs.setdefault(k, '')
        if car.get('_generic'):  # メーカーから検索（汎用）: ms_* を持ち ps_* は空
            cs.update({'SearchMethod': 1, 'ms_MakerCode': car['MakerCode'], 'ms_MakerName': car.get('MakerName', ''), 'ms_CarFormGroupCode': '02', 'ms_CarNameCode': '010',
                       'ms_CarNameName': '汎用', 'ms_CarCode': car['CarCode'], 'ms_ModelName': car.get('ModelName', ''), 'ms_YearCode': '00', 'ms_YearSearchFlag': 0,
                       'ms_BodyCode': car['BodyCode'], 'ms_FVACode': car['FVACode'], 'ms_GradeCode': car['GradeCode'],
                       'ev_TwoColorCodeFlag': 0, 'ev_ColorCodeByManual': 1, 'ev_UColorCodeByManual': 0, 'ev_LColorCodeByManual': 0, 'ev_TrimCodeByManual': 0, 'ev_TrimCode': str(car.get('TrimCode') or '').strip(),
                       'ev_ColorName': '', 'ev_ColorRGB1': '', 'nm_PartsPriceDate': '', 'ps_YearSearchFlag': 0,
                       'ps_CarMouldNo': '', 'ps_CarKindNo': '', 'ps_CarRegDate': '', 'ps_CarRegEra': '', 'ps_CarRegEraYear': '', 'ps_CarSerialNoHead': '', 'ps_CarSerialNoTail': '', 'ps_CarSerialNo': '', 'ps_YearName': ''})
        cs_cols = [r[1] for r in cur.execute('PRAGMA table_info(CarSearch)')]
        cs = {k: v for k, v in cs.items() if k in cs_cols}
        cur.execute('UPDATE CarSearch SET ' + ','.join(f'{k}=?' for k in cs), list(cs.values()))
        # CarEVA / CarSearchEVA
        opts = car.get('options_available', {})
        cur.execute("UPDATE CarEVA SET EVACode='', EVAName=''")
        cur.execute("UPDATE CarSearchEVA SET EVariationNo='', EVariationName='', EVariationOrder=''")
        if len(eva_codes) > 28:
            raise ValueError(f'装備が 28 件を超えている（{len(eva_codes)} 件）。CarEVA は 28 行しかないので hints.eva_codes を絞る')
        for i, code in enumerate(eva_codes[:28]):
            cur.execute('UPDATE CarEVA SET EVACode=?, EVAName=? WHERE RecordNo=?', (code, opts.get(code, ''), i + 1))
            cur.execute('UPDATE CarSearchEVA SET EVariationName=?, EVariationOrder=? WHERE RecordNo=?', (opts.get(code, ''), f'{i + 1:02d}', i + 1))
        # Customer
        reg = cust.get('reg_no', '')
        dep, div, biz, ser = split_reg_no(reg) or ('', '', '', '')
        reg_date = car.get('ps_CarRegDate', '')
        era, ey = nc.get_era_info(reg_date) if reg_date else ('令和', '')
        term = cust.get('term_date', '') or '00000000'
        tera, tey = nc.get_era_info(term) if term != '00000000' else ('令和', '')
        # 住所はコグニと同じく 都道府県 / 市区郡 / 以降 に分割（AddressOther1 は 30 バイト）
        _st = {k: unicodedata.normalize('NFKC', str(cust.get(k) or '')).strip() for k in ('prefecture', 'municipality', 'address_other')}
        if any(_st.values()):   # 車検証 OCR などの構造化された住所はそのまま（市区郡の名前に 市/郡 を含む住所を切り違えない。2026-09-15）
            pref, muni, other = _st['prefecture'], _st['municipality'], _st['address_other']
        else:
            pref, muni, other = split_address_text(cust.get('address', '') or '')
        cur.execute('''UPDATE Customer SET Name1=?,Name2='',Name3='',Owner='様',PostalNo=?,Prefecture=?,Municipality=?,AddressOther1=?,AddressOther2='',Phone=?,Fax='',
                       CarRegNoDepartment=?,CarRegNoDivision=?,CarRegNoBusiness=?,CarRegNoSerial=?,CarSerialNo=?,CarMouldNo=?,CarKindNo=?,UserName=?,OwnerName=?,
                       TermDate=?,TermEra=?,TermEraYear=?,CarRegDate=?,CarRegEra=?,CarRegEraYear=?,Kilometer=?''',
                    (_fit(cust.get('name', ''), 30), cust.get('postal', ''), _fit(pref, 8), _fit(muni, 30), _fit(other, 30), cust.get('phone', ''),
                     dep, div, biz, ser, car.get('ps_CarSerialNo', ''), car.get('ps_CarMouldNo', ''), car.get('ps_CarKindNo', ''),
        # 所有者・使用者欄に入るのは owner_name / user_name だけ。customer.owner は車検証の所有者を控えるメモで、
        # コグニ運用では所有者欄に顧客名を入れることが多い（実機 cogni_R1/R2）ため自動では採用しない
                     _fit(cust.get('user_name', '同上'), 20), _fit(cust.get('owner_name', cust.get('name', '')), 20), term, tera, tey, reg_date, era, ey, int(cust.get('kilometer') or 0)))
        # Insurance / FileInfo / Setting
        def _date8(v):
            """YYYYMMDD 以外（空・区切り付きで 8 桁にならないもの）は ''。区切り付きでも数字が 8 桁なら受ける。
            日付なしの番兵 '00000000' も ''（元号の年が '0000' にならないように。Codex 指摘 2026-09-14）"""
            s = re.sub(r'\D', '', str(v or ''))
            return s if len(s) == 8 and s != '00000000' else ''

        def _int_or(v, default):
            try:
                return int(str(v).strip()) if str(v or '').strip() else default
            except ValueError:
                return default
        acc = _date8(ins.get('accident_date', '')) or '00000000'; pre = _date8(ins.get('presence_date', '')) or '00000000'
        aera, aey = nc.get_era_info(acc) if acc != '00000000' else ('令和', ''); pera, pey = nc.get_era_info(pre) if pre != '00000000' else ('令和', '')
        # 受付番号・代理店・アジャスター・入出庫日・修理日数（2026-09-14）: estimate の insurance にあれば書く。無ければ従来どおり
        # 空 / -1 / '00000000'（雛形と同じ）。置き場所はコグニと同じ列（Insurance.AgencyName / AdjusterName / RepairDays、
        # FileInfo.AcceptNo / GarageIn* / GarageOut*）。XML の GarageIn/OutDate は実機 NEO 178 本で日付があっても空なので書かない
        gin = _date8(ins.get('garage_in', '')); gout = _date8(ins.get('garage_out', ''))
        giera, giey = nc.get_era_info(gin) if gin else ('令和', ''); goera, goey = nc.get_era_info(gout) if gout else ('令和', '')
        cur.execute('''UPDATE Insurance SET PolicyNo=?,ContractorName=?,AgencyName=?,AccidentDate=?,AccidentEra=?,AccidentEraYear=?,PresenceDate=?,PresenceEra=?,PresenceEraYear=?,
                       AgreedDate='00000000',AgreedEra='令和',AgreedEraYear='',RepairDays=?,TimelyPriceOutTax=-1,TimelyPriceInTax=-1,TimelyPriceTax=-1,AdjusterName=?,AdjusterPost=?,ConsultantName='',ConsultantFactory=?''',
                    (_fit(ins.get('policy_no', ''), 20), _fit(ins.get('contractor', ''), 20), _fit(ins.get('agency', ''), 20), acc, aera, aey, pre, pera, pey,
                     _int_or(ins.get('repair_days'), -1), _fit(ins.get('adjuster', ''), 20),
                     _fit(ins.get('adjuster_post', ''), 40),   # アジャスターの支店・所属（速報報告書の「支店」。2026-09-14）。列は TEXT(40)（アプリの旧経路と同じ幅。2026-09-15）
                     _fit(ins.get('factory', ''), 30)))
        eera, eey = nc.get_era_info(est_date)
        cur.execute("UPDATE FileInfo SET EstimatedDate=?,EstimatedEra=?,EstimatedEraYear=?,AcceptNo=?,GarageInDate=?,GarageInEra=?,GarageInEraYear=?,"
                    "GarageOutDate=?,GarageOutEra=?,GarageOutEraYear=?,Note1='',Note2='',Note3=''",
                    (est_date, eera, eey, _fit(ins.get('accept_no', ''), 37), gin or '00000000', giera, giey, gout or '00000000', goera, goey))
        tax_flag = {'四捨五入': 1, '切り捨て': 2, '切り上げ': 3}.get(str(getattr(self, '_tax_round', None) or '四捨五入'), 1)
        # TaxKindFlag はコグニの「消費税設定」の表示方法（0 外税 / 1 内税）。金額が税込で印字された見積書（judgment_rules 10-4）は
        # 内税にすると、コグニの画面・帳票が見積書と同じ税込の金額で並ぶ（明細に入れる額は外税・内税どちらでも税抜。実機 NEO 12 本で確認 2026-09-17）
        tax_kind = 1 if getattr(self, '_tax_included', False) else 0
        cur.execute('UPDATE Setting SET wb_PriceBase=?, wb_Round=?, wi_Round=10, TaxKindFlag=?, TaxRate=10, tx_ArrangeFlag=?', (labor_rate, wage_unit(), tax_kind, tax_flag))  # 工賃単位（1/10/100 円）は wb_Round だけ（コグニ実機 2026-09-08 cogni_frame_F2_r100: wi_Round は 10 のまま）。消費税の計算単位 tx_ArrangeFlag 1=四捨五入/2=切り捨て/3=切り上げ（cogni_frame_F2_taxfloor）
        try:  # 帳票タイトルの並びはコグニ保存版と同じ ReportID（Unicode）順
            rt = cur.execute('SELECT ReportID, ReportTitle FROM ReportTitle').fetchall()
            cur.execute('DELETE FROM ReportTitle')
            for rid, ttl in sorted(rt, key=lambda x: x[0]):
                cur.execute('INSERT INTO ReportTitle (ReportID, ReportTitle) VALUES (?,?)', (rid, ttl))
        except sqlite3.Error:
            pass
        return self._close(con, p)

    # ------------------------------------------------------------ AnSMB.txt (142B 固定長)
    @staticmethod
    def build_ansmb(rows: list[dict]) -> bytes:
        """AnSMB.txt: 142B 固定長 × 明細行（ERParts の全行）。60 行で打ち切る実装だったが、実 NEO は 1 行も欠けない
        （コグニ標準サンプル 04011141 は 267 行 / 267 行、工場 NEO 12051345 は 97/97、再検索保存版 C06 は 66/66。2026-09-08 の総当たり検証で修正）"""
        out = []
        for r in rows:
            line = bytearray(b' ' * 142)
            def put(pos, s, width):
                b = str(s).encode('cp932w', 'replace')[:width]
                line[pos:pos + len(b)] = b
            put(0, f"{r['LineNo']:08d}", 8)
            put(8, (r['PartsCode'] or '    '), 4)  # 部品コードの無い行はコード欄も空白（コグニ実機 2026-09-08 cogni_K1 / C06 再検索: '0000' ではない）
            put(12, ('  ' if int(r['DisposalCode']) < 0 else f" {r['DisposalCode']}"), 2)  # 修理方法空欄の手入力行: コード欄・区分欄とも空白（コグニ実機 2026-09-08 exp_manual.neo の AnSMB）
            put(14, r['PartsName'], 25); put(38, r['PartsNameStandard'], 24)  # 標準名称は 38 桁目から、11.DB の名称欄そのまま（先頭スペースを落とさない。実 NEO 299 行で開始桁 38/39/40 の分布が名称の先頭スペース数と一致）
            put(62, r['PartsNo'], 18)
            put(80, r['PartsNoStandard'], 18)  # 標準品番は修理方法によらず入る（実機 cogni_M1。再検索を通した保存版では消えるが、それは再検索の副作用）
            if r.get('_smb_recycle'):  # リサイクル部品: 12 桁目 PartsCodeSub=1、名称と品番欄を差替、101 桁目 RecycleFlag
                put(12, '1', 1); put(14, ' ' * 25, 25); put(14, r['_smb_recycle'][0], 25); put(39, ' ' * 23, 23); put(62, ' ' * 18, 18); put(62, 'リサイクル部品', 18); put(80, ' ' * 18, 18)
            put(98, f"{max(1, r['PartsCount'] if r['PartsCount'] > 0 else 1):02d}", 2)
            _of = r.get('OrderFlag')
            put(100, (' ' if _of in (None, '') else str(_of)), 1)  # 数値の 0 も '0' として書く（ERParts と同じ値にする）  # 100 桁 = ERParts.OrderFlag（部品発注の状態）。触っていない見積は全行 空白（実機 cogni_M1、実 NEO 199 本 6,477 行で恒等）
            put(101, ('1' if r.get('_smb_recycle') else '0') + ('1' if r.get('ReserveFlag') else '0') + '00', 4); put(104, (('1' + str(r.get('_smb_tail') or '')) if re.search(r'[A-Z]', str(r.get('_smb_tail') or '')) else '0'), 12); put(127, 'F99999', 6)  # 101 桁 = リサイクル置換、102 桁 = 保留（実機 2026-09-09 cogni_CX4 6800）。 104〜115 桁: 12.DB の CutWork 欄（[65:76] = 部分切断作業）に **英字（可能作業 KS 等）があれば** '1' + その欄、無ければ '0' と空白（実 NEO 801 行で完全一致。数字だけの '0      0   ' 等は 0）
            out.append(bytes(line) + b'\r\n')
        return b''.join(out)

    # ------------------------------------------------------------ XML
    @staticmethod
    def build_xml(xml: bytes, car: dict, cust: dict, ins: dict, total: int, est_date: str) -> bytes:
        t = xml.decode('cp932', 'replace')
        reg = car.get('ps_CarRegDate', '')
        dep, div, biz, ser = split_reg_no(cust.get('reg_no', '')) or ('', '', '', '')
        _acc8 = _date8_str(ins.get('accident_date', ''))   # 区切り付き・和暦で来ても 8 桁から作る（'2026/09/01' が '2026//0/9/' になっていた）
        vals = {'CustomerName1': _fit(cust.get('name', ''), 30), 'CustomerName2': '', 'AdjusterName': _fit(ins.get('adjuster', ''), 20), 'AcceptNo': _fit(ins.get('accept_no', ''), 37), 'TicketNo': _fit(ins.get('policy_no', ''), 20),   # DB と同じ欄幅
                'AccidentDate': (f'{_acc8[:4]}/{_acc8[4:6]}/{_acc8[6:8]}' if _acc8 else ''),
                'CarNo': f'{dep}{div}{biz}{ser}', 'CarName': car.get('CarNameByUser', ''), 'CarMouldNo': car.get('ps_CarMouldNo', ''), 'CarKindNo': car.get('ps_CarKindNo', ''),
                'ColorCode': car.get('ColorCode', ''), 'OwnerName': _fit(cust.get('owner_name', cust.get('name', '')), 20), 'UserName': _fit(cust.get('user_name', '同上'), 20),   # DB と同じ欄幅（XML だけ切っていなかった。2026-09-15 アプリのレビュー）
                'CreatedDate': f'{est_date[:4]}/{est_date[4:6]}/{est_date[6:8]}', 'GarageInDate': '', 'GarageOutDate': '', 'CarSerialNo': car.get('ps_CarSerialNo', ''),
                'CarTermEraDate': '', 'Kilometrage': str(cust.get('kilometer') or ''), 'CarRegistedDate': (f'{reg[:4]}/{reg[4:6]}' if reg else ''),
                'ii_CustomerName': _fit(ins.get('contractor', ''), 20), 'ii_PresenceDate': '', 'ii_AgreedDate': '', 'ii_RepairDays': '', 'ii_TimePrice': '',
                'MakerName': car.get('MakerName', ''), 'CarNameName': car.get('CarNameName', ''), 'ModelName': car.get('ModelName', ''), 'CarYearName': '', 'BodyName': '', 'FVariationNameByUser': car.get('FVANameByUser', ''), 'GradeName': '',
                'Total': str(total), 'CarNoArea': dep, 'CarNoClass': div, 'CarNoKana': biz, 'CarNoSeries': ser}
        if reg:
            era, ey = nc.get_era_info(reg)
            vals['CarRegistedDateEra'] = {'令和': '4', '平成': '3', '昭和': '2'}.get(era, '4'); vals['CarRegistedDateYear'] = str(int(ey)); vals['CarRegistedDateMonth'] = str(int(reg[4:6]))
        for k, v in vals.items():
            t = nc.replace_xml_tag(t, k, v)
        return t.encode('cp932w', 'replace')

    # ------------------------------------------------------------ 総合
    def build(self, estimate: dict, vehicle_inputs: dict, hints: Optional[dict] = None, labor_rate: Optional[int] = None,  # noqa: D401
              est_date: Optional[str] = None, insurance: Optional[dict] = None) -> tuple[bytes, dict]:
        labor_rate = _money(labor_rate, 'labor_rate（レバーレート）') or None  # '8,000' のような写し方でも受ける
        if insurance is None:  # estimate_schema.md は estimate['insurance'] が正。引数で渡さない呼び出し元でも保険・案件欄を落とさない（Codex 指摘 2026-09-14）
            insurance = estimate.get('insurance') if isinstance(estimate.get('insurance'), dict) else None
        token = set_wage_unit(_money(estimate.get('wage_round'), 'wage_round（工賃の丸め単位）') or 10)  # 工賃丸め単位（工場のコグニ設定。100 円丸めの工場あり）。この build の間だけ有効
        com_tables.reset_sources()  # このビルドで COM の表をどこから読んだか（予備を使ったら run_case が ★）
        try:
            return self._build_inner(estimate, vehicle_inputs, hints, labor_rate, est_date, insurance)
        finally:
            reset_wage_unit(token)

    def _build_inner(self, estimate: dict, vehicle_inputs: dict, hints: Optional[dict], labor_rate: Optional[int],
                     est_date: Optional[str], insurance: Optional[dict]) -> tuple[bytes, dict]:
        est_date = est_date or datetime.datetime.now().strftime('%Y%m%d')
        self.silent_errors = []  # build ごとに初期化（前の案件の失敗が次の報告に残ると、直したのに ★ が出続ける）
        _generic = _flag(vehicle_inputs.get('generic'), 'vehicle.generic')  # 文字列 "false" を真にしない
        hints = _norm_hint_flags(hints)  # hints の真偽値欄も同じく厳密に読む
        veh = self.generic_vehicle(vehicle_inputs) if _generic else self.resolve_vehicle(vehicle_inputs, hints)
        car = veh['neo_car']
        if not car and not _generic and not veh.get('candidates'):
            # 車種マスタに候補が 1 つも無く、輸入車のしるし（17 桁の国際 VIN・輸入車メーカー名）があるときだけ、
            # コグニ実機と同じ「メーカー→車名 汎用」の汎用車種で作る（VOLVO_gen.neo が正。部品コード・標準指数は入らない）。
            # 国産車の読み違いで候補ゼロになったときは今までどおり止める（しるしが無いので）。2026-09-19 本番検証のボルボで止まっていた
            _why = imported_signal(vehicle_inputs)
            if _why:
                veh = self.generic_vehicle(vehicle_inputs)
                veh['auto_generic'] = _why
                veh['evidence'] = [f'{_why}で、コグニの車種マスタに候補が無い → 輸入車として汎用車種 {veh["neo_car"]["CarCode"]} で作った'] + list(veh.get('evidence') or [])
                car = veh['neo_car']
        if not car:
            raise RuntimeError(f"車両特定失敗: {veh['evidence']}")
        if not _generic and veh.get('confidence') not in ('confirmed', 'high') and car.get('CarCode'):
            # 車検証だけでグレード等が絞れない（medium/low）ときは、見積の品番から 11.DB 変種行の条件を逆引きしてヒントに加え、もう一度特定する
            try:
                inf = AddataParts(self.engine, car['CarCode']).infer_from_parts(estimate.get('items') or [])
            except Exception:
                inf = None
            if inf and (inf.get('grade_codes') or inf.get('year_group')):
                h2 = dict(hints or {})
                # 呼び出し側（車検証・見積の記載）のヒントを優先し、品番からの推定（確率的）は「無いキーを補う」か「矛盾しない範囲で絞る」だけに使う
                if inf.get('grade_codes'):
                    if h2.get('grade_codes'):
                        inter = set(h2['grade_codes']) & set(inf['grade_codes'])
                        h2['grade_codes'] = inter or set(h2['grade_codes'])
                    else:
                        h2['grade_codes'] = set(inf['grade_codes'])
                if inf.get('year_group') and not h2.get('year_group'):
                    h2['year_group'] = inf['year_group']
                veh2 = self.resolve_vehicle(vehicle_inputs, h2)
                rank = {'unknown': 0, 'low': 1, 'medium': 2, 'high': 3, 'confirmed': 4}
                c2 = veh2.get('neo_car') or {}
                if c2 and rank.get(veh2.get('confidence'), 0) > rank.get(veh.get('confidence'), 0) and c2.get('CarCode') == car.get('CarCode') and c2.get('YearCode') == car.get('YearCode'):
                    # 品番ヒントで候補が一意になり確度が上がったときだけ採用（同点のままなら車検証だけの結果を保つ）
                    veh2['evidence'] = list(veh2.get('evidence') or []) + ['品番からの逆引き: ' + '; '.join(inf['evidence'][:6])]
                    veh, car = veh2, c2
        tc = str((vehicle_inputs or {}).get('trim_code') or '').strip()  # トリムコード（トヨタ。<car>29.DB の候補。TRIM_W66b.neo）
        if tc:
            codes = [x[11:].decode('cp932', 'replace').strip() for x in self.resolver._vdb_bytes(car['CarCode'], '29')]
            if codes and tc not in codes:
                raise ValueError(f"trim_code {tc!r} は {car['CarCode']}29.DB の候補 {codes} に無い")
            if not codes:
                raise ValueError(f"{car['CarCode']} には 29.DB（トリムコード一覧）が無い（trim_code {tc!r} は指定できない）")
            car['TrimCode'] = tc; car['TrimCodeFlag'] = 1
        self._row_ctx = {'year': car.get('YearCode', ''), 'body': str(car.get('BodyCode', '') or ''), 'reg_ym': str(car.get('ps_CarRegDate', '') or '')[:6], 'color': car.get('ColorCode', '') if car.get('ColorCodeFlag') else '', 'grade': car.get('GradeCode', ''), 'fva': (car.get('FVACode', '') or '')[-1:],  # 4WD は 'ZA' なので照合は末尾 1 文字
                         'eva': (set(str(x) for x in ((hints or {}).get('eva_codes') or []) if x) | ({'Z'} if car.get('four_wd') else set()))
                                - set(str(x).strip() for x in ((hints or {}).get('eva_exclude') or []) if str(x).strip())}  # 色別部品の装備条件は build 前に分かる EVA（hints と 4WD の 'Z'）で判定。**eva_exclude はここにも効かせる** —— 行生成（11/13/83.DB の変種選択）に使うので、最終 CarEVA だけ直しても品番・価格がずれる（Codex 指摘 2026-09-12）
        self._tax_round = estimate.get('tax_round')  # 消費税の計算単位（Setting.tx_ArrangeFlag と消費税額。write_ansvif / write_ansvem が参照）
        self._tax_included = bool(estimate.get('tax_included'))  # 見積書が税込で印字されている（Setting.TaxKindFlag = 内税。コグニの「消費税設定」の表示方法）
        rows, stats = self.build_rows(estimate['items'], car['CarCode'], labor_rate, index_policy=(estimate.get('index_policy') or 'auto'))
        try:  # 「重複部品コードチェック」（12.DB のレベル欄）: コグニで開くとダイアログが出る組合せを警告する（行は変えない）
            ap_dup = getattr(self, '_last_parts', None)  # build_rows() が今回の車種で作った AddataParts（_parts_for_std はこの後で更新される）
            if ap_dup is not None:
                groups = ap_dup.duplicate_groups(); codes = {int(r['PartsCode']) for r in rows if str(r.get('PartsCode') or '').isdigit()}
                dups = sorted({tuple(sorted((p_, c_))) for p_, kids in groups.items() if p_ in codes for c_ in kids if c_ in codes})  # 枝の相互登録を 1 組に正規化
                if dups:
                    stats['duplicate_parts'] = [f'{p_:04d} と {c_:04d}' for p_, c_ in dups]
        except Exception as _edup:  # noqa: BLE001  検査できない車種はある。黙って飛ばすと二重計上の警告が消える
            self._note_silent('重複部品（枝の相互登録）の検査', _edup, '同じ部品を親子で二重計上していても警告が出ない')
        self._parts_for_std = getattr(self, '_last_parts', None)
        self._adas_rows_written = []  # build ごとに初期化（前回の ADAS 行が残ると合計に二重計上される。監査 7）
        if car.get('_generic'):
            for r_, it_ in zip(rows, [i for i in estimate['items']]):
                if not it_.get('index') and r_.get('WageOutTax', -1) > 0:
                    r_['Time'] = -1; r_['TimeStandard'] = 0; r_['WageStandardOutTax'] = 0; r_['WageByManual'] = '*'; r_['WageFileTime'] = ''
        # 装備: 部品証拠 + ヒント
        opts = car.get('options_available', {})
        # 装備コード: 肯定証拠（一致変種だけが持つ）が否定証拠（他変種だけが持つ）を上回るものだけ。A-E はグレード/エンジン文字
        eva = [c for c, n in stats['option_pos'].items() if c in opts and c not in ('A', 'B', 'C', 'D', 'E') and n > stats['option_neg'].get(c, 0)]
        _excl = set(str(x).strip() for x in ((hints or {}).get('eva_exclude') or []) if str(x).strip())  # hints.eva_exclude: 部品証拠から拾った装備を明示的に外す（品番が別の理由で一致するとき）
        if _excl:
            eva = [c for c in eva if c not in _excl]
        for c in (hints or {}).get('eva_codes', []):
            if c in opts and c not in eva and c not in _excl:
                eva.append(c)
        # 4WD の Z は自動で付くが、**明示的に eva_exclude に書かれていれば尊重する**
        # （車種特定が 4WD 側に寄っていて品番が合わないときの逃げ道。これが無いと eva_exclude が no-op になる。Codex 指摘 2026-09-12）
        # 内部の装備集合（標準指数・変種の照合に使う）には 4WD の Z を **必ず残す**。
        # 11/15.DB の Z 付き行は FVA 'ZA' の車でも Z フラグで選ばれる（Codex 3 周目: W90 ボディ 20 で 7600 脱着 3.6h/3.1h、8370 取替 1.4h/1.1h が Z の有無で変わる）。
        # ファイルに書く CarEVA から Z を落とすのは write_ansvif に渡す直前で行う（下の eva_write）
        if car.get('four_wd') and 'Z' in opts and 'Z' not in eva and 'Z' not in _excl:
            eva.append('Z')
        # コグニの標準指数（11.DB × 15.DB の選択規則）: 工賃も指数も無い取替/脱着行は標準で埋め、標準どおりの行は WorkCode/暫定 '$' を揃える
        if not car.get('_generic'):
            ap_std = getattr(self, '_parts_for_std', None)
            if ap_std is not None:
                present = [(int(r['PartsCode']), int(r['DisposalCode'])) for r in rows if r.get('PartsCode') and not r.get('_reserve')]
                rate_ = int(stats.get('labor_rate') or labor_rate or 0)
                for r_ in rows:
                    if r_.get('_reserve'):
                        continue
                    # 工賃未指定で指数だけ見積にある行（修理・未照合も含む）: 指数×単価で工賃を補完（手入力指数 '#'）
                    if not r_.get('_wage_given') and float(r_.get('Time') or -1) > 0 and (r_.get('WageOutTax', -1) or -1) < 0 and rate_:
                        r_.update({'WageOutTax': r10_even(float(r_['Time']) * rate_), 'TimeStandard': 0, 'WageStandardOutTax': 0, 'WageByManual': '#'})
                        q_ = max(1, int(r_.get('PartsCount') or 1)); ps_ = int(r_.get('PartsPriceStandardOutTax') or 0)
                        r_['ChangeTotalOutTax'] = (ps_ if ps_ > 0 else 0) or -1  # 手入力指数行の取替合計 = 標準部品代のみ（標準工賃 0。コグニ生成 NEO の板金 '#' 行と同形）
                    if not r_.get('PartsCode') or int(r_['DisposalCode']) == 4:
                        continue  # 点検調整(4) は 11.DB に修理方法トークンが無く標準指数を持たない（実機確認）。分解調整(5) は 'C' 変種行がある部品だけ cogni_standard が値を返す
                    std = ap_std.cogni_standard(int(r_['PartsCode']), int(r_['DisposalCode']), car.get('GradeCode', ''), (car.get('FVACode', '') or '')[-1:], set(eva), car.get('YearCode', ''), present, car.get('BodyCode', ''))
                    r_['_cogni_std'] = std
                    # 部位コード: 12.DB の基本版（行番号の百の位 0）に無い部品で、車両条件の標準も引けない行はコグニが空にする
                    # （実機 2026-09-08 cogni_K1/K2/K3 と pair_P/Q/U/none: J87 0140 フォグライト = 版 1/2 のみ → 常に空。0402 ヘッドライトユニットも版 1/2 のみで、装備 U で表示指数 0.4 を持つときだけ A05。相手に吸収されて指数が無いときは空）
                    _vers = getattr(ap_std, 'ws_versions', {}).get(int(r_['PartsCode']), set())
                    if _vers and '0' not in _vers and (not std or std.get('absorbed')):
                        r_['BlockCode'] = ''
                    if not std:
                        # 標準指数が引けなくても、11.DB 変種行に区分レターがあれば WorkCode には入る
                        # （コグニ実機 2026-09-12 W90 4800 取替: Time -1 のまま WorkCode 'J3Q4'。実機 NEO 25 本でも
                        #   Time<0 で WorkCode の入った取替行が 47 行: cogni_G1 2450 'I1'、H16 0020 'C' など）
                        if r_['DisposalCode'] in (0, 1, 3, 5) and not str(r_.get('WorkCode') or '').strip():
                            try:
                                _yr = str(car.get('YearCode', '') or '').strip()
                                _grp = _yr[-1] if _yr.isdigit() and int(_yr) else ''
                                _row11 = ap_std._std_row(int(r_['PartsCode']), int(r_['DisposalCode']), car.get('GradeCode', ''), (car.get('FVACode', '') or '')[-1:], set(eva), _grp, car.get('BodyCode', ''))
                            except Exception as _e11w:  # noqa: BLE001
                                _row11 = None
                                self._note_silent(f"WorkCode の補完（部品 {r_['PartsCode']}）", _e11w, '標準の無い行の WorkCode が空のままになる')
                            if _row11 and str(_row11.get('secs') or '').strip():
                                r_['WorkCode'] = str(_row11['secs']).ljust(10)[:10]
                        continue
                    if r_['DisposalCode'] in (0, 1, 3, 5) and not str(r_.get('WorkCode') or '').strip() and std.get('secs'):
                        r_['WorkCode'] = std['secs'].ljust(10)[:10]  # 工賃 0 指定（付属部品）などで下の分岐に入らない行も WorkCode は自区分（コグニ再検索 C-HR 3430 ロッカモール脱着 wage 0: Time -1 / WorkCode 'R3'）
                    if std.get('absorbed'):
                        # 骨格部品で指数が相手部品（バルクヘッド等）に集約された行: コグニは Time -1 / TimeStandard 0 のまま WorkCode に自区分を残す（FRAME_p7 1500 'I0J0'）
                        r_['WorkCode'] = std['secs'].ljust(10)[:10]
                        r_['TimeStandard'] = 0; r_['WageStandardOutTax'] = 0  # 標準欄は 0（見積書の工賃欄が空欄＝wage 0 の行も同じ。FRAME_p7 1430/1434/1500/1511/1600）
                        if r_['DisposalCode'] in (0, 1) and (r_.get('WageOutTax', -1) or -1) <= 0 and float(r_.get('Time') or -1) <= 0:
                            r_.update({'Time': -1, 'WageOutTax': -1, 'WageByManual': '', 'WageFileTime': ''})
                        elif r_.get('WageByManual') == '' and (r_.get('WageOutTax', -1) or -1) > 0:  # '$' はこの後の暫定指数パスで付くのでここには来ない（監査 12）
                            r_.update({'WageByManual': ('#' if r_.get('_index_given') else '*'), 'WageFileTime': '', '_std_note': '相手部品の作業に吸収される行（コグニの標準は指数なし）'})  # 標準が無くなった行の印字工賃は手入力の印（指数印字あり '#'、工賃だけ '*'。Codex e23）
                            if not r_.get('_index_given'):
                                r_['Time'] = -1  # 工賃だけ印字の吸収行: 単価から推定した指数は出さない（'*' 行は Time -1 = exp_manual.neo。Codex e25）
                        # 見積書に工賃/指数が印字されている吸収行はそのまま（工場が手入力した '*' / '#' 行として残す。コグニも吸収行への手入力を消さない = 再検索の「手入力を残す」）
                        continue
                    # 見積書に工賃欄が無い（wage 未指定）取替/脱着行だけ標準で埋める。wage 0 の付属部品（工賃は主部品に含む）は空欄のまま
                    if r_['DisposalCode'] in (0, 1) and not r_.get('_wage_given') and (r_.get('WageOutTax', -1) or -1) < 0 and rate_:
                        t_given = float(r_.get('Time') or -1)
                        if t_given > 0 and abs(t_given - std['time']) >= 0.01:  # 指数だけ見積にあり標準と違う: 手入力指数として工賃を補完
                            w_ = r10_even(t_given * rate_)
                            r_.update({'WageOutTax': w_, 'TimeStandard': 0, 'WageStandardOutTax': 0, 'WageByManual': '#'})
                        else:
                            w_ = r10_even(std['time'] * rate_)
                            r_.update({'Time': std['time'], 'TimeStandard': std['time'], 'WageOutTax': w_, 'WageStandardOutTax': w_, 'WageByManual': '',
                                       'WorkCode': std['secs'].ljust(10)[:10], 'WageFileTime': f"{std['time']:g}"})
                        q_ = max(1, int(r_.get('PartsCount') or 1)); ps_ = int(r_.get('PartsPriceStandardOutTax') or 0)
                        r_['ChangeTotalOutTax'] = (ps_ if ps_ > 0 else 0) + (r10_even(std['base'] * rate_) if r_.get('WageByManual') == '' else w_)  # 取替合計 = 標準部品代（単価）+ 連動加算前の標準工賃（NEW2 0010: 61,890 = 50,700 + 1.3h×8,610）
                        if r_.get('_sub_prefix') and str(r_.get('PartsName', '')).startswith('  '):  # 工賃付きになった主部品は生成器が付けた付属部品の 2 スペース接頭辞を外す（11.DB 名称欄由来の先頭スペースはコグニも残す: NEW2 '  Frﾊﾞﾝﾊﾟﾌｴｲｽ(ﾄｿｳｽﾞﾐ)'、FRAME_p8 '    ｳｲﾝﾄﾞｼ-ﾙﾄﾞﾛﾜﾌﾚ-ﾑ'）
                            r_['PartsName'] = _fit(str(r_['PartsName']).lstrip(' '), 24)
                    elif r_.get('WageByManual') == '' and r_.get('Time', -1) > 0 and abs(float(r_['Time']) - std['time']) < 0.01:
                        r_['WorkCode'] = std['secs'].ljust(10)[:10]
                        if abs(std['base'] - std['time']) >= 0.01:  # 連動加算を含む標準行: 取替合計は加算前の指数で（NEW2 0010）
                            ps_ = int(r_.get('PartsPriceStandardOutTax') or 0)
                            r_['ChangeTotalOutTax'] = ((ps_ if ps_ > 0 else 0) + r10_even(std['base'] * rate_)) or -1  # '$'（暫定指数）はコグニ生成 NEO と一致しない例があるため付けない
                    elif r_.get('WageByManual') == '' and r_['DisposalCode'] in (0, 1) and rate_ and std['time'] > 0 and float(r_.get('Time') or -1) > 0 and abs(float(r_['Time']) - std['time']) >= 0.01:
                        # 見積の指数が 15.DB の単独値とは一致するが、組合せ後の標準（連動・吸収込み）と違う行: コグニは再検索で標準値に置き換えるので、見積値を守るには手入力指数 '#'（再検索 C06 2300 2.0 vs 標準 2.3、2700 2.8 vs 2.9）
                        ws_ = r10_even(std['time'] * rate_)
                        r_.update({'TimeStandard': std['time'], 'WageStandardOutTax': ws_, 'WageByManual': '#', 'WorkCode': std['secs'].ljust(10)[:10], 'WageFileTime': f"{std['time']:g}", '_std_note': '連動・吸収込みの組合せ標準と差（工場のコグニは入力順で計算）'})
                    elif r_.get('WageByManual') == '#' and rate_ and std and not (r_.get('Time', -1) > 0 and abs(float(r_['Time']) - std['time']) < 0.01):
                        # 手入力指数のまま標準欄には標準値を残す（他工場のコグニ生成 NEO: '#' 行でも TimeStandard 0.7 / WageStandard 4,870 = neo_REAL_12051345）
                        ws_ = r10_even(std['time'] * rate_)
                        r_.update({'TimeStandard': std['time'], 'WageStandardOutTax': ws_, 'WorkCode': std['secs'].ljust(10)[:10], 'WageFileTime': f"{std['time']:g}"})  # '#' 行の WageFileTime は標準指数（04011103 2300: Time 0.7 / TimeStandard 0.5 / WageFileTime '0.5'）
                    elif r_.get('WageByManual') == '*' and rate_ and std['time'] > 0 and int(r_.get('WageOutTax') or 0) != r10_even(std['time'] * rate_):
                        # 工賃だけ手入力（標準と違う額）で標準指数のある行: コグニ実機 2026-09-08 exp_manual.neo 2700 = Time -1 / TimeStandard 2.8 / WageStandard 22,400 / WorkCode 'Q1T1' / WageFileTime '2.8'。取替合計は標準部品代のみ
                        r_.update({'TimeStandard': std['time'], 'WageStandardOutTax': r10_even(std['time'] * rate_), 'WorkCode': std['secs'].ljust(10)[:10], 'WageFileTime': f"{std['time']:g}"})
                        if not r_.get('_index_given'):
                            r_['Time'] = -1  # 取替合計は後段の一括計算（標準部品代 + 標準工賃 = exp_manual.neo 2700 の 92,000）
                    elif r_.get('WageByManual') == '*' and (estimate.get('index_policy') or 'auto') != 'manual' and rate_ and std['time'] > 0 and int(r_.get('WageOutTax') or 0) == r10_even(std['time'] * rate_) and float(r_.get('Time') or -1) <= 0:
                        # 工賃だけ印字され指数の無い行で、工賃が組合せ標準（15.DB の個別 wi には無い合算値）× 単価と一致: コグニ標準の行として書く（骨格 1410 の 9.2 等）
                        r_.update({'Time': std['time'], 'TimeStandard': std['time'], 'WageStandardOutTax': int(r_['WageOutTax']), 'WageByManual': '', 'WorkCode': std['secs'].ljust(10)[:10], 'WageFileTime': f"{std['time']:g}"})
                        ps_ = int(r_.get('PartsPriceStandardOutTax') or 0)
                        r_['ChangeTotalOutTax'] = (ps_ if ps_ > 0 else 0) + r10_even(std['base'] * rate_)
                    elif r_.get('WageByManual') == '#' and (estimate.get('index_policy') or 'auto') != 'manual' and r_.get('Time', -1) > 0 and abs(float(r_['Time']) - std['time']) < 0.01 and rate_:
                        # 合算標準（'FG' 等）は 15.DB の個別 wi に無いので前段で '#' になる。見積の指数が合算標準と一致するなら標準扱いに戻す
                        ws_ = r10_even(std['time'] * rate_)
                        _is_std = int(r_.get('WageOutTax') or 0) == ws_
                        r_.update({'TimeStandard': std['time'], 'WageStandardOutTax': ws_, 'WageByManual': ('' if _is_std else '*'), 'WorkCode': std['secs'].ljust(10)[:10], 'WageFileTime': (f"{std['time']:g}" if _is_std else '')})  # '*' になる行は WageFileTime ''
                        q_ = max(1, int(r_.get('PartsCount') or 1)); ps_ = int(r_.get('PartsPriceStandardOutTax') or 0)
                        r_['ChangeTotalOutTax'] = (ps_ if ps_ > 0 else 0) + r10_even(std['base'] * rate_)  # 連動加算前の標準工賃
                    elif r_.get('WageByManual') == '#' and rate_ and std and std['time'] > 0:
                        # index_policy manual で見積の指数が標準と一致する '#' 行: 手入力指数のまま標準欄には標準値（コグニの '#' 行と同じ。分解調整 7600 'I7O7' 3.0h = 04011103。監査 42）
                        ws_ = r10_even(std['time'] * rate_)
                        r_.update({'TimeStandard': std['time'], 'WageStandardOutTax': ws_, 'WorkCode': std['secs'].ljust(10)[:10], 'WageFileTime': f"{std['time']:g}"})
        for r_ in rows:  # 板金(6)/修理(2) 行の WorkCode は標準化パスの後でも空欄（Codex e32: 標準が引けて指数一致の行で復活しないように）
            if not r_.get('_reserve') and int(r_.get('DisposalCode') or 0) in (2, 6):
                r_['WorkCode'] = ' ' * 10
        # 暫定指数（11.DB [71] '$'）: Provisional '$'。標準工賃のまま（WageByManual ''）で工賃がある行は WageByManual も '$'（他工場のコグニ生成 NEO 10 本の prov 行 30 件で例外なし。空欄行は ''、手入力指数は '#' のまま）
        for r_ in rows:
            std_ = r_.get('_cogni_std')
            if r_.get('_reserve') or not r_.get('PartsCode') or not (std_ and std_.get('prov')):
                continue
            r_['Provisional'] = '$'
            if r_.get('WageByManual') == '' and float(r_.get('Time') or -1) > 0 and (r_.get('WageOutTax', -1) or -1) > 0:
                r_['WageByManual'] = '$'  # 工賃のある行だけ（工賃欄が空欄の付属部品行は '' のまま。監査 10）
        for r_ in rows:  # 脱着(1) の '#' 行で標準が無いもの: WorkCode は空文字（10 スペースではない）、価格未入力なら PartsPriceFlag 1（コグニ再検索 C-HR 3147 / C04 1002、工場 NEO の脱着 '#' 2 行）
            if not r_.get('_reserve'):
                if r_.get('PartsCode') and int(r_.get('DisposalCode') or 0) == 1 and r_.get('WageByManual') == '#' and not r_.get('_cogni_std') and not str(r_.get('WorkCode') or '').strip():
                    r_['WorkCode'] = ''
                    if int(r_.get('PartsPriceOutTax') or 0) <= 0:
                        r_['PartsPriceFlag'] = 1
        # ConstructGroup は最終的な WageByManual で決める（標準化パスで '#' ⇔ ''/'*' が変わるため）: 取替 = 11.DB、標準指数の脱着/脱着板金 = '  '、実額入力（'#'）や修理系 = NULL（コグニ NEW2/NEW4）
        for r_ in rows:
            if r_.get('_reserve') or not r_.get('PartsCode'):
                continue
            d_ = int(r_.get('DisposalCode') or 0)
            if d_ in (1, 3):
                # 脱着(1)/脱着板金(3): 11.DB の D 変種行の [68:70]（通常 '  '。D88 8200 ステアリングコラムチューブ 'QQ'、W66 6350 'N8'、S64 0402/0452 'A1'/'A3' = 他工場 NEO 55 行すべて D 行の値と一致）。
                # 標準の無い実額入力（'#'）行は ''（空文字。NULL ではない = 監査 39 / レビュー 102）
                std_ = r_.get('_cogni_std')
                if r_.get('WageByManual') == '#' and not std_:
                    r_['ConstructGroup'] = ''
                else:
                    try:
                        _g = (str(car.get('YearCode', '')).strip()[-1] if str(car.get('YearCode', '')).strip().isdigit() and int(car.get('YearCode') or 0) else '')
                        _args = (car.get('GradeCode', ''), (car.get('FVACode', '') or '')[-1:], set(eva), _g)
                        # 脱着(1) は D 行、脱着板金(3) は標準指数と同じ DS 行（無ければ D 行）
                        drow = (ap_std._std_row(int(r_['PartsCode']), d_, *_args) or (ap_std._std_row(int(r_['PartsCode']), 1, *_args) if d_ == 3 else None)) if ap_std is not None else None
                    except Exception:
                        drow = None
                    r_['ConstructGroup'] = '' if (d_ == 1 and r_.get('_no_d')) else ((drow.get('cgroup') or '  ') if drow else '  ')  # 現在車両に D 変種の無い脱着行は空文字（cogni_frame_F2 1410）
            elif d_ in (2, 6):
                # 板金(6): 11.DB の S 変種行の [68:70]（通常 '  '。W66 4802 'Z0'、5320 'N1'、D98 1502 'G2' = 他工場 NEO の板金 27 行すべて S 行の値と一致。'#' でも同じ）
                # 修理(2) も同じ S 行の値（実機 2026-09-12 w66_real: 4802 'Z0'、w66b_real: 4600 は取替行 'M8' でも S 行の '  '。他の実機の修理行 11 行は '  '）
                try:
                    _g = (str(car.get('YearCode', '')).strip()[-1] if str(car.get('YearCode', '')).strip().isdigit() and int(car.get('YearCode') or 0) else '')
                    srow = ap_std._std_row(int(r_['PartsCode']), d_, car.get('GradeCode', ''), (car.get('FVACode', '') or '')[-1:], set(eva), _g) if ap_std is not None else None
                except Exception:
                    srow = None
                r_['ConstructGroup'] = (srow.get('cgroup') or '  ') if srow else '  '
            elif d_ != 0:
                # 修理(2)・点検調整(4)・分解調整(5): '  '（NONE_dc 点検調整、SIENTA 修理、他工場 修理 13 行・分解調整 2 行）。標準の無い実額入力 '#' と標準の無い分解調整（NONE_dc 6500/6600、NEW4 0030/0140）は ''（空文字。
                # NULL は全 NEO に 1 件も無い = 監査 39）。明細画面から直接入力した '#' 行（PartsType 1）の点検調整には ' '（半角 1 文字: 12151249 7600、12241720 6305）もあるが生成器は '' に統一
                r_['ConstructGroup'] = '' if ((r_.get('WageByManual') == '#' and not r_.get('_cogni_std') and d_ != 2) or (d_ == 5 and not r_.get('_cogni_std'))) else '  '  # 標準が引ける '#' 行は '  '（04011103 2300）。修理（d2）は標準が無い '#' 行でも '  '（実機 2026-09-08 cogni_H11 0010）
        # ChangeTotal（取替合計）= 標準部品代（単価）+ その部品を取替したときの標準工賃（連動加算前）。修理方法に関わらず同じ
        # （コグニ生成 NEO: 点検調整 0450 = 64,300 + 0.4h、分解調整 6500 = 部品 + 取替工賃、板金 4802 = 0 + 6.6h、板金 '#' 1500 = 17,700 + 4.4h）
        ap_ct = getattr(self, '_parts_for_std', None)
        if ap_ct is not None:
            rate_ = int(stats.get('labor_rate') or labor_rate or 0)
            present = [(int(r['PartsCode']), int(r['DisposalCode'])) for r in rows if r.get('PartsCode') and not r.get('_reserve')]
            for r_ in rows:
                if r_.get('_reserve') or not r_.get('PartsCode'):
                    continue
                if int(r_.get('DisposalCode') or 0) == 1 and r_.get('_no_d') and not str(r_.get('PartsNoStandard') or '').strip():
                    # build 時の EVA では脱着(D) 行が無かった行: 最終 EVA（部品証拠で確定）で D 行が有効なら標準品番・標準価格を復元（Codex e20）
                    try:
                        _g = (str(car.get('YearCode', '')).strip()[-1] if str(car.get('YearCode', '')).strip().isdigit() and int(car.get('YearCode') or 0) else '')
                        if ap_ct._std_row(int(r_['PartsCode']), 1, car.get('GradeCode', ''), (car.get('FVACode', '') or '')[-1:], set(eva), _g, str(car.get('BodyCode', '') or '')) is not None:
                            ctx_ = dict(getattr(self, '_row_ctx', None) or {}); ctx_['eva'] = set(eva)
                            var_, _ = ap_ct.variant(int(r_['PartsCode']), r_.get('_pn_in', ''), ctx_, 1)
                            if var_:
                                r_['PartsNoStandard'] = str(var_['parts_no']); r_['PartsPriceStandardOutTax'] = int(var_['price'] or 0) or -1; r_['_no_d'] = False
                                r_['PartsPriceFlag'] = 1 if int(r_.get('PartsPriceOutTax') or 0) <= 0 and int(var_['price'] or 0) > 0 else 0  # 標準価格に依存する印を作り直す（Codex e23）
                                _pp = int(r_.get('PartsPriceOutTax') or 0); _sp = int(var_['price'] or 0); _q = max(1, int(r_.get('PartsCount') or 1))
                                r_['PartsPriceByManual'] = '*' if (_pp > 0 and (_sp <= 0 or _pp != _sp * _q)) else ''  # 価格手入力の印も復元後の標準価格で再評価（Codex e24）
                                try:  # 復元した行の ConstructGroup も D 行の値で作り直す（監査 2: 復元前に '' にしたまま残っていた）
                                    _g2 = (str(car.get('YearCode', '')).strip()[-1] if str(car.get('YearCode', '')).strip().isdigit() and int(car.get('YearCode') or 0) else '')
                                    _drow = ap_ct._std_row(int(r_['PartsCode']), 1, car.get('GradeCode', ''), (car.get('FVACode', '') or '')[-1:], set(eva), _g2, str(car.get('BodyCode', '') or ''))
                                    r_['ConstructGroup'] = (_drow.get('cgroup') or '  ') if _drow else '  '
                                except Exception:
                                    r_['ConstructGroup'] = '  '
                    except Exception:
                        pass
                if int(r_.get('DisposalCode') or 0) == 1 and r_.get('_no_d'):
                    # 最終 EVA でも D 変種が無いと確定した脱着行: WorkCode・ConstructGroup は空文字（スペースではない）、価格未入力なら PartsPriceFlag 1（実機 cogni_frame_F2 1410。D 変種のある W66 0003 は WorkCode 'A' / CG '  ' / flag 1）
                    r_['WorkCode'] = ''; r_['ConstructGroup'] = ''
                    if int(r_.get('PartsPriceOutTax') or 0) <= 0:
                        r_['PartsPriceFlag'] = 1
                if int(r_.get('DisposalCode') or 0) == 1 and r_.get('_no_d') and not str(r_.get('PartsNoStandard') or '').strip():
                    r_['ChangeTotalOutTax'] = -1  # 現在車両に脱着(D) 変種の無い部品の脱着行だけ: 取替合計も -1（コグニ実機 2026-09-08 cogni_frame_F2 1410）。標準品番が空なだけの脱着行は従来どおり取替標準工賃（Codex e21）
                    continue
                ps_ = int(r_.get('PartsPriceStandardOutTax') or 0)
                try:
                    stdK = ap_ct.cogni_standard(int(r_['PartsCode']), 0, car.get('GradeCode', ''), (car.get('FVACode', '') or '')[-1:], set(eva), car.get('YearCode', ''), present, car.get('BodyCode', ''))
                except Exception:
                    stdK = None
                wk = r10_even(stdK['base'] * rate_) if (stdK and rate_) else 0
                if r_.get('_cogni_std') is None and int(r_.get('DisposalCode') or 0) == 0 and r_.get('WageStandardOutTax', 0) and r_['WageStandardOutTax'] > 0 and not stdK:
                    wk = int(r_['WageStandardOutTax'])  # 取替の標準が見つからないが行に標準工賃があるときはそれ
                r_['ChangeTotalOutTax'] = ((ps_ if ps_ > 0 else 0) + wk) or (0 if str(r_.get('PartsNoStandard') or '').strip() == '-' else -1)  # 品番 '-'（価格なし部品）の取替合計は 0（工場 NEO 2 行、コグニ再検索 C-HR タイヤ 8070/8979）
        # テンプレート展開
        tpl = open(self.template_path, 'rb').read()
        ck = nc.find_real_cks(tpl); raw = nc.decompress_neo(tpl, ck); mgmt, entries = nc.parse_entries(tpl, ck[0]); files = nc.extract_files(raw, entries)
        if car.get('_generic'):
            car['PartsPriceDate'] = ''
        else:
            hdr = self.resolver.header_db(car['CarCode'])
            car['PartsPriceDate'] = hdr['title'].get('price_date', '') or '080701'
        # WorkCodeUpdateDate = COM.CAB/DATAUP.DB（CarCode, YYYYMM = 車種データ更新年月）。他工場のコグニ生成 NEO 9 本で 9/9 一致（J52 201706、W66 202011、J97 202201、D88 202312、D98 202303…）。
        # DATAUP.DB に無い車種（S64 ワゴンR スマイル等）はコグニも '' を書く。vehicle.workcode_date で上書き可
        car['WorkCodeUpdateDate'] = str((vehicle_inputs or {}).get('workcode_date') or self.dataup_date(car.get('CarCode', '')))
        # 塗膜（66.DB 先頭桁: 1=ソリッド 2=メタリック 3=２コートパール 4=３コートパール）
        self._coat = None
        fc = None if car.get('_generic') else self.resolver.finish_code(car['CarCode'], car.get('ColorCode', ''))
        if fc in (1, 2, 3, 4):
            self._coat = (fc, ['', 'ソリッド', 'メタリック', '２コートパール', '３コートパール'][fc])
        _pd0 = estimate.get('paint') or {}
        # パネルが 1 枚も無くバンパだけ塗る見積（`panels: []` + bumper_front/rear）も塗装詳細として扱う（実機 2026-09-12 w66d_real: 加算基礎数値 -1、BAN.DB のバンパ加算基礎）
        _bumper_only0 = (isinstance(_pd0.get('panels'), list) and not _pd0.get('panels') and any(_pd0.get(k) for k in ('bumper_front', 'bumper_rear'))
                        and all(k in BUMPER_ONLY_KEYS or str(k).startswith('_') for k in _pd0))  # _ で始まる下書きの内部キー（_total_from_lines 等）は読まない  # 許可リスト外のキー（sealing / frame / other / 付加塗装 / base / booth …）が混じる組合せは実機未確認なので従来どおり止める（Codex 指摘）
        self._paint_detail = _pd0 if (_pd0.get('panels') or _bumper_only0) else None
        if not self._paint_detail:
            extra = [k for k in PAINT_DETAIL_KEYS if (estimate.get('paint') or {}).get(k)]
            if extra:
                raise ValueError(f'paint.{extra[0]} などの塗装詳細は paint.panels（パネル別指数）がある見積でだけ書ける（一括計上の塗装費と併用不可）: {extra}')
        if (estimate.get('paint') or {}).get('coat'):  # 見積書に塗膜が印字されていればそれを正とする（カラーコードの 66.DB 塗膜と違えば工場がコグニの塗装条件で変えたもの。実機 P1: 塗膜を 3コートパールに変えると加算基礎数値と材料代割合の既定が変わる）
            names = ['', 'ソリッド', 'メタリック', '２コートパール', '３コートパール']
            cn = unicodedata.normalize('NFKC', estimate['paint']['coat'])
            for i, nm in enumerate(names):
                if nm and unicodedata.normalize('NFKC', nm) == cn:
                    self._coat = (i, nm)
        self._labor_rate = stats.get('labor_rate') or labor_rate or 0
        self._car_code = car['CarCode']; self._body_code = str(car.get('BodyCode', '') or '')
        self._car_form = car.get('CarFormCode', '')
        self._paint_index = None
        try:  # 塗装明細が無くても 20.DB のパネル一覧は要る（PaintingLinkParts は塗装をしない見積にも入る。実機 cogni_K1）
            self._paint_index = PaintIndex(self.resolver.root, car['CarCode'], body=car.get('BodyCode', ''))
        except Exception as ex:
            if self._paint_detail:
                print('PaintIndex skip', ex)
        files['AnSvEm0001.sld'], totals = self.write_ansvem(
            files['AnSvEm0001.sld'], rows, _money(estimate.get('paint', {}).get('total'), 'paint.total（塗装工賃計）'),
            estimate.get('expenses', []), _money(estimate.get('paint', {}).get('material'), 'paint.material（材料代）'), estimate=estimate)
        # **FVA コードが 'Z' 始まり（4WD がエンジン・駆動の区分に含まれる車）では、ファイルの CarEVA に Z を書かない**。
        # コグニ実機 2026-09-12 W90 ハイエース TRH229: FVA 'ZA' で装備バリエーション一覧に「４ＷＤ」は出ず
        # （10.DB には Z=４ＷＤ があるが FVA で表す装備は一覧から外れる）、保存版の CarEVA は空だった。
        # 内部の照合用 eva は Z を残したまま（上）。書き出し用だけ落とす
        eva_write = [c for c in eva if not (c == 'Z' and str(car.get('FVACode') or '').startswith('Z'))]
        files['AnSvIf0001.sld'] = self.write_ansvif(files['AnSvIf0001.sld'], car, estimate.get('customer', {}), insurance or {}, eva_write, stats['labor_rate'], est_date)
        files['AnSMB.txt'] = self.build_ansmb(rows)
        xml_name = next((n for n in files if n.lower().endswith('.xml')), None)
        if xml_name:
            files[xml_name] = self.build_xml(files[xml_name], car, estimate.get('customer', {}), insurance or {}, totals['total'], est_date)
        if 'AnSvEm0001Ex.db' in files:
            adas_w = getattr(self, '_adas_rows_written', None) or []
            if adas_w:  # ADAS 作業行の索引（コグニ保存版と同形: RecordNo 順に Idx、空の入力行は PartsCode 空）。保留行は入れない（コグニ実機 SIENTA_hold）
                CRLF = chr(13) + chr(10)
                idx = ''
                n1 = 1
                for i, a in enumerate(adas_w, 1):
                    k = n1 + i
                    idx += 'Idx%d.PartsCode=`%s`%sIdx%d.ItemName=`%s`%sIdx%d.Comment=`%s`%s' % (k, a['code'], CRLF, k, a['name'], CRLF, k, a.get('comment', ''), CRLF)
                # 空の入力行（Idx1）は最後に置く（コグニ実機 ADAS_D98c と 2026-09-09 cogni_CX5。番号は 1 のままで、本文の並びだけ末尾）
                idx += 'Idx%d.PartsCode=``%sIdx%d.ItemName=``%sIdx%d.Comment=``%s' % (n1, CRLF, n1, CRLF, n1, CRLF)
                txt = files['AnSvEm0001Ex.db'].decode('cp932', 'replace')
                pat = re.compile(r'\[ADASWork\]\r?\n(?:Idx\d+\.[A-Za-z]+=`[^`]*`\r?\n)*')
                txt, n_sub = pat.subn(lambda m: '[ADASWork]' + CRLF + idx, txt)
                if n_sub != 1:
                    raise ValueError('AnSvEm0001Ex.db に [ADASWork] セクションが無く、保留/ADAS の索引を書けない')
                files['AnSvEm0001Ex.db'] = txt.encode('cp932w', 'replace')
        if 'AnNote.ini' in files:
            _note = nc.replace_ini_value(files['AnNote.ini'].decode('cp932', 'replace'), 'Note', '')
            # [Reserve] Flag = 保留行があれば 1（実機 2026-09-09 cogni_CX4）。キー名 'Flag' は [Comment] にもあるのでセクション内だけを置換する
            _note = re.sub(r'(\[Reserve\]\s*\r?\n(?:[^\[]*?\r?\n)??Flag\s*=)[^\r\n]*',
                           lambda m: m.group(1) + ('1' if getattr(self, '_has_reserve', False) else '0'), _note, count=1)
            _note = re.sub(r'(\[Comment\]\s*\r?\n(?:[^\[]*?\r?\n)??Flag\s*=)[^\r\n]*',
                           lambda m: m.group(1) + ('1' if getattr(self, '_has_comment', False) else '0'), _note, count=1)
            files['AnNote.ini'] = _note.encode('cp932w', 'replace')
        # AnSvMail.ini（ネオメール連携）: テンプレートの顧客・証券情報を必ず置換（実 NEO 保存版と同形）
        if 'AnSvMail.ini' in files:
            cust = estimate.get('customer', {}); ins = insurance or {}
            dep, div, biz, ser = split_reg_no(cust.get('reg_no', '')) or ('', '', '', '')
            def _ini1(v):   # INI の 1 行に収める（改行で行が増えない）
                return re.sub(r'[\r\n]+', ' ', str(v or ''))
            mail = '\r\n'.join(['[General]', 'Signature=NEOMAIL2', '[Audaneo2]', f"CustomerName={_ini1(_fit(ins.get('contractor', ''), 20))}",
                                f'CarNoDepartment={dep}', f'CarNoDivision={div}', f'CarNoBusiness={biz}', f'CarNoSerial={ser}',
                                f"TicketNo={_ini1(_fit(ins.get('policy_no', ''), 20))}", f"AcceptNo={_ini1(_fit(ins.get('accept_no', ''), 37))}", f"AccidentDate={_date8_str(ins.get('accident_date', '')) or '00000000'}",
                                f"AgreedName={_ini1(_fit(ins.get('factory', ''), 30))}", f"CarName={_ini1(car['CarNameByUser'])}", ''])
            files['AnSvMail.ini'] = mail.encode('cp932w', 'replace')
        # AnFlInfo: 車種データ版数を ADDATA の AnVer.DB に合わせる（コグニ保存時と同形）
        if 'AnFlInfo' in files:
            t = files['AnFlInfo'].decode('cp932', 'replace')
            ver = ''
            vp = os.path.join(self.resolver.root, 'COM', 'AnVer.DB')
            if os.path.exists(vp):
                mm = re.search(r'Number=(\S+)', _xor_text(vp))
                ver = mm.group(1) if mm else ''
            old = re.search(r'^AnVer\.db=(.*)$', t, re.M)
            if ver and old and old.group(1).strip() != ver:
                t = re.sub(r'^AnVer\.db=.*$', f'AnVer.db={ver}\r\nAnVer.db_Back1={old.group(1).strip()}', t, flags=re.M)
            files['AnFlInfo'] = t.encode('cp932w', 'replace')
        neo = nc.repack_neo(tpl, files, mgmt, entries)
        # 先頭 424B の管理領域（既存見積一覧のサマリ）を今回の見積の値で書き直す
        cust = estimate.get('customer', {}); ins = insurance or {}
        carno = split_reg_no(cust.get('reg_no', '')) or ('', '', '', '')
        neo = nh.apply(neo, agreed=_fit(ins.get('factory', ''), 30), name1=_fit(cust.get('name', ''), 30), car_name=car['CarNameByUser'],
                       created=datetime.date(int(est_date[:4]), int(est_date[4:6]), int(est_date[6:8])),
                       totals=[totals['parts'], totals['wage'], totals['paint'], totals['expense_parts'] + totals['expense_wage'], totals['total']],
                       carno=carno, saved=datetime.datetime.now(), license_id=LICENSE_ID)
        _dup: dict = {}
        for _r in rows:  # 同じ部品コードでも修理方法が違えば工場の実 NEO にもある（12081431 の脱着+修理）。修理方法まで同じ行が重なったときだけ数える
            _c = str(_r.get('PartsCode') or '').strip()
            if _c:
                _k = (_c, int(_r.get('DisposalCode') or 0))
                _dup[_k] = _dup.get(_k, 0) + 1
        stats['dup_refs'] = sorted(f'{c}({d})' for (c, d), n in _dup.items() if n > 1)  # 実機はこの形を保持する（H24 で確認）。左右の取り違えを見つけるための情報
        report = {'vehicle': veh, 'car': car, 'eva': eva, 'rows': rows, 'stats': stats, 'totals': totals, 'pdf_totals': estimate.get('totals', {})}
        # この車のボディ用の塗装パネル行を選べなかったもの（面積＝塗装指数が実機とずれる可能性）。
        # 生成器を直接呼ぶ経路でも気づけるよう report に載せる
        _bu = list(getattr(self._paint_index, 'body_unresolved', []) or [])
        for _u in (getattr(self, '_pi_rows', None).body_unresolved if getattr(self, '_pi_rows', None) else []):
            if not any(x['code'] == _u['code'] for x in _bu):
                _bu.append(_u)   # 明細の名称引きで使った一時インスタンスの控えも集める
        report['paint_body_unresolved'] = _bu
        report['silent_errors'] = list(self.silent_errors)
        if getattr(self.resolver, '_kata_src', None):
            com_tables._src()['Katashiki.DB'] = self.resolver._kata_src
        report['com_stale'] = com_tables.stale_reference_used()  # 毎月変わる COM 表を同梱の予備から読んだ（ADDATA の版とずれている可能性）
        # ファイルの CarEVA に実際に書いた装備（FVA が Z 始まりの車は Z を落としてある）。
        # report['eva'] は内部の照合用（Z を含む）なので、人に見せる「装備」はこちらを使う（Codex 4 周目）
        report['eva_write'] = list(eva_write)
        return neo, report


# ======================================================================
def load_human_csv(path: str) -> dict:
    """サンプル見積PDF/*.csv（Claude 手起こし 4 セクション形式）→ estimate dict"""
    import csv
    rows = list(csv.reader(open(path, encoding='utf-8-sig')))
    sec, hdr = None, {}
    items, paint_total, paint_material, expenses, totals = [], 0, 0, [], {}
    header = None
    EXPENSE_KW = ('諸経費', 'DTC', 'エーミング', '光軸', '写真', '保管', '間接', 'ショートパーツ', '廃棄', 'リセット', '再設定', '材料費', 'シーラー', 'アンダーコート', '防錆', 'ガス')

    def method_from_name(nm: str) -> str:
        for kw, mth in (('脱着', '脱着'), ('板金', '板金'), ('鈑金', '板金'), ('取替', '取替'), ('交換', '取替'), ('分解', '分解調整'),
                        ('測定', '調整'), ('調整', '調整'), ('点検', '調整'), ('修正', '修理'), ('修理', '修理')):
            if kw in nm:
                return mth
        return ''

    def num(v) -> int:
        try:
            return int(float(str(v).replace(',', '') or 0))
        except ValueError:
            return 0

    last_part_row = None
    for r in rows:
        if not r or not any(r):
            continue
        if r[0].startswith('## '):
            sec = r[0][3:]; header = None; continue
        if sec == '車両・顧客情報' and len(r) >= 2:
            hdr[r[0]] = r[1]; continue
        if header is None:
            header = r; continue
        d = dict(zip(header, r))
        if sec in ('修理明細', '整備明細'):
            name = (d.get('修理項目・部品名称') or d.get('整備内容（部品名/作業名）') or d.get('作業/部品名称') or d.get('作業内容・使用部品名') or '').strip()
            code = (d.get('コード') or '').strip()
            method = (d.get('作業区分') or '').strip()
            pn = (d.get('部品番号（数量）') or d.get('部品番号') or d.get('品番') or '').strip()
            if pn in ('※', '-', '－'):
                pn = ''
            qty = 1
            m = re.search(r'\((\d+)\)', pn)
            if m:
                qty = int(m.group(1))
            elif d.get('数量') or d.get('部品数量'):
                try: qty = int(float(d.get('数量') or d.get('部品数量')))
                except ValueError: qty = 1
            price = num(d.get('部品価格合計（円）') or d.get('部品代（円）') or d.get('部品・油脂代') or d.get('部品金額') or 0)
            wage = num(d.get('工賃（円）') or d.get('技術料（円）') or d.get('技術料') or 0)
            # 塗装（作業区分 or 名称）
            if (method == '塗装' or ('塗装' in name and not code and not pn)) and price == 0 and wage > 0:
                paint_total += wage; continue
            if method == '塗装' and price > 0 and wage > 0:  # 工場 M 形式: 塗装費用 技術料+材料
                paint_total += wage; paint_material += price; continue
            # 費用（部品でも工賃でもない行）
            if not code and not pn and price == 0 and wage > 0 and any(k in name for k in EXPENSE_KW):
                expenses.append({'name': name, 'amount': wage, 'kind': 'wage'}); continue
            if not code and not pn and price > 0 and wage == 0 and any(k in name for k in ('ショートパーツ', '鈑金パテ', '溶接材料', '写真')):
                expenses.append({'name': name, 'amount': price, 'kind': 'parts'}); continue
            if price == 0 and wage == 0:
                continue
            # トヨタ系ディーラー形式: 作業行（名称末尾に 取替/脱着 等, 工賃のみ）と部品行（先頭連番）が分かれている
            section_fmt = '部位' in d
            if section_fmt:
                mth = method_from_name(name)
                if price == 0 and wage > 0 and mth:
                    if mth == '取替' and '費増' not in name and '片側' not in name:
                        if last_part_row and last_part_row.get('pending_wage'):  # 前の保留を工賃のみ行として確定
                            items.append({'code': '', 'name': last_part_row['name'], 'method': '取替', 'parts_no': '', 'qty': 1, 'parts_price': 0, 'wage': last_part_row.pop('pending_wage')})
                        last_part_row = {'pending_wage': wage, 'group': d.get('部位', ''), 'name': re.sub(r'取替$', '', name).strip()}
                        continue  # 直後の主部品行に工賃を合流
                    items.append({'code': '', 'name': re.sub(r'(取替|脱着|測定|調整|点検)$', '', name).strip(), 'method': mth, 'parts_no': '', 'qty': 1, 'parts_price': 0, 'wage': wage})
                    continue
                name = re.sub(r'^\d+\s+', '', name)
                method = method or '取替'
                if last_part_row and last_part_row.get('pending_wage') and last_part_row.get('group') == d.get('部位', ''):
                    wage = last_part_row.pop('pending_wage')
            if not method:
                method = method_from_name(name) or ('取替' if price > 0 else '調整')
            items.append({'code': code, 'name': name, 'method': method, 'parts_no': pn, 'qty': qty, 'parts_price': price, 'wage': wage})
        elif sec == '塗装明細':
            paint_material += num(d.get('材料費（円）') or 0)
            paint_total += num(d.get('技術料（円）') or 0)
        elif sec == 'その他（課税）':
            amt = num(d.get('技術料（円）') or d.get('料金（円）') or 0)
            if amt:
                expenses.append({'name': d.get('項目名', ''), 'amount': amt, 'kind': 'wage'})
        elif sec == '金額集計' and len(r) >= 2:
            totals[r[0]] = num(r[1])
    if last_part_row and last_part_row.get('pending_wage'):
        items.append({'code': '', 'name': last_part_row['name'], 'method': '取替', 'parts_no': '', 'qty': 1, 'parts_price': 0, 'wage': last_part_row.pop('pending_wage')})
    # 税込単価の見積（ホンダ系ディーラー等）: 明細合計が「税抜合計×1.1」に一致するなら税込入力と判定し税抜へ換算
    tax_inclusive = False
    sum_items = sum(i['parts_price'] + i['wage'] for i in items) + paint_total + paint_material + sum(e['amount'] for e in expenses)
    ex_total = next((v for k, v in totals.items() if '税抜' in k), 0)
    if ex_total and abs(sum_items - round(ex_total * 1.1)) <= 5 and abs(sum_items - ex_total) > 5:
        tax_inclusive = True
        for i in items:
            i['parts_price'] = round(i['parts_price'] / 1.1); i['wage'] = round(i['wage'] / 1.1)
        paint_total = round(paint_total / 1.1); paint_material = round(paint_material / 1.1)
        for e in expenses:
            e['amount'] = round(e['amount'] / 1.1)
    reg = hdr.get('登録番号', '')
    desig, cat = hdr.get('型式指定', ''), hdr.get('類別区分', '')
    for key in ('型式別', '類別番号', '型式指定・類別'):
        m = re.match(r'^\s*(\d{4,5})\s*[-－ ]\s*(\d{1,4})', hdr.get(key, ''))
        if m and not desig:
            desig, cat = m.group(1), m.group(2)
    model = re.sub(r'^\S+-', '', hdr.get('型式', '')) or re.sub(r'-.*$', '', hdr.get('車台番号', ''))
    serial = hdr.get('車台番号', '') or hdr.get('フレーム番号', '')
    if serial and '-' not in serial and model:
        serial = f'{model}-{serial}'
    veh = {'model_code': model, 'serial_no': serial, 'desig': desig, 'category': cat,
           'reg_date': hdr.get('初度登録年月', hdr.get('初度登録', hdr.get('登録年月', ''))), 'color_code': hdr.get('カラーコード', '')}
    if not veh['model_code'] and hdr.get('車名・型式'):
        m = re.search(r'\b([A-Z]{1,4}\d{1,3}[A-Z]?)\b', hdr['車名・型式'])
        veh['model_code'] = m.group(1) if m else ''
    cust = {'name': hdr.get('お客様名', ''), 'reg_no': reg, 'postal': hdr.get('お客様郵便番号', ''), 'address': hdr.get('お客様住所', ''),
            'kilometer': int(re.sub(r'\D', '', hdr.get('走行キロ_km', hdr.get('走行距離_km', '0')) or '0') or 0)}
    return {'header': hdr, 'vehicle': veh, 'customer': cust, 'items': items, 'paint': {'total': paint_total, 'material': paint_material}, 'expenses': expenses, 'totals': totals,
            'issuer': hdr.get('発行元', ''), 'tax_inclusive': tax_inclusive}


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'サンプル見積PDF', '修理明細_スマートカーレンタル_NBOX_5936.csv')
    est = load_human_csv(src)
    b = NeoBuilder()
    neo, rep = b.build(est, est['vehicle'], hints={'car_name': est['header'].get('車名・型式', '')})
    out = os.path.join(HERE, 'out'); os.makedirs(out, exist_ok=True)
    name = datetime.datetime.now().strftime('%m%d%H%M') + '.neo'
    open(os.path.join(out, name), 'wb').write(neo)
    print('生成:', os.path.join(out, name), len(neo), 'bytes')
    print('車両:', {k: rep['car'].get(k) for k in ['CarCode', 'YearCode', 'BodyCode', 'GradeCode', 'FVACode', 'CarName', 'ColorName']}, '装備:', rep['eva'])
    print('照合:', rep['stats']['matched'], '/', rep['stats']['total'], 'レバーレート', rep['stats']['labor_rate'], rep['stats']['rate_votes'])
    print('NEO合計:', rep['totals']); print('PDF合計:', rep['pdf_totals'])
