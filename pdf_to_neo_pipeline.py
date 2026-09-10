#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_to_neo_pipeline.py  (v2-Iter2)

工場見積PDF → コグニセブン互換 NEO ファイル変換パイプライン。

Iter2 追加要件:
  - classify_pdf_source: 正規表現 (Audatex|アウダテックス|コグニ|Cogni\\s*7?|AUDADAMS|車両見積システム|自動車補修見積システム|Cognitive\\s*Seven) に強化
  - identify_vehicle_in_addata: AddataSearchEngine + difflib fuzzy
  - build_neo_mode_b/c: マッチ→マーカー→generate_neo_file で bytes 生成
  - verify_neo_against_pdf: ERParts SELECT し件数・総額比較
  - process_pdf_to_neo: vehicle_info/items 未提供かつ skip_ocr=False かつ GEMINI_API_KEY あり → OCR
  - 後方互換: Iter1 のシグネチャ維持
"""
from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import sqlite3
import tempfile
from typing import Any, Dict, List, Literal, Optional, TypedDict

logger = logging.getLogger(__name__)


def _pdfium_lock():
    """pdfium はスレッドセーフでないため、app 側と同じロックで直列化する。

    複数スレッドから同時に触るとCヒープが壊れてプロセスごと落ちる。
    `streamlit run app.py` では app.py は __main__ として動くため
    `from app import ...` は app.py を二重読み込みして別のロックを返す。
    ロックだけを置いた専用モジュールを介して確実に共有する。
    """
    try:
        from _pdfium_lock_mod import PDFIUM_LOCK
        return PDFIUM_LOCK
    except Exception:
        global _LOCAL_PDFIUM_LOCK
        if _LOCAL_PDFIUM_LOCK is None:
            import threading
            _LOCAL_PDFIUM_LOCK = threading.Lock()
        return _LOCAL_PDFIUM_LOCK


_LOCAL_PDFIUM_LOCK = None
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[pdf_to_neo_pipeline] %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# ============================================================
# 型定義
# ============================================================
PdfSource = Literal["cogni", "other", "unknown"]
GenerationMode = Literal["A", "B", "C"]

# Iter9: パイプライン全体結果キャッシュ (md5 → process_pdf_to_neo result)
_PIPELINE_CACHE: Dict[str, Dict[str, Any]] = {}
_PIPELINE_CACHE_MAX = 32


# ============================================================
# v11.0 Phase A-1/A-2: 安全な数値変換ヘルパー
# OCR 由来の括弧書き「(02)」「△500」「￥1,200」「2台」等を吸収して
# float / int クラッシュを根絶する（BUG-1 (02) 問題の対策）
# ============================================================
def _clean_numeric_token(val) -> Optional[str]:
    """数値トークンを正規化する。数値として解釈できない場合は None を返す。

    以前は記号を無条件に削除していたため、'￥1,200-' が 0 に、
    '1,000～2,000' が 10002000 に化けていた。金額が静かに壊れるより、
    解釈できないものは呼び出し側の default に倒す方が安全。
    """
    s = str(val).strip()
    if not s:
        return None
    s = s.translate(str.maketrans('０１２３４５６７８９．−，、～', '0123456789.-,,~'))
    # 会計表記の括弧はマイナス
    paren_negative = bool(re.fullmatch(r'\(\s*[^()]+\s*\)', s))
    if paren_negative:
        s = s[1:-1].strip()
    is_negative = paren_negative or bool(re.match(r'^[△▲\-]', s))
    s = re.sub(r'^[△▲\-]+', '', s)
    # 単位・通貨・区切りを除去
    s = re.sub(r'[個本枚セット台式時間円¥￥,\s]', '', s)
    # 「¥1,200-」のような末尾のハイフン/長音は円マークの慣用表記
    s = re.sub(r'[\-ー―–—]+$', '', s)
    if not s:
        return None
    # ここで数値そのものになっていなければ解釈しない（範囲表記・分数など）
    if not re.fullmatch(r'\d+(\.\d+)?', s):
        return None
    return ('-' + s) if is_negative else s


def _to_int(val, default: int = 0) -> int:
    """安全な int 変換。括弧・通貨記号・全角・単位・会計表記を吸収。"""
    if val is None or val == '' or val == '*' or val == '**':
        return default
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        try:
            return int(round(val))
        except (ValueError, OverflowError):
            return default
    cleaned = _clean_numeric_token(val)
    if cleaned is None:
        return default
    try:
        return int(round(float(cleaned)))
    except (ValueError, OverflowError):
        return default


def _to_float(val, default: float = 0.0) -> float:
    """安全な float 変換。括弧・通貨記号・全角・単位を吸収。"""
    if val is None or val == '' or val == '*' or val == '**':
        return default
    if isinstance(val, bool):
        return float(val)
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = _clean_numeric_token(val)
    if cleaned is None:
        return default
    try:
        return float(cleaned)
    except (ValueError, OverflowError):
        return default


def _pdf_md5(b: bytes) -> str:
    import hashlib
    return hashlib.md5(b).hexdigest() if b else ""


def clear_pipeline_cache() -> None:
    _PIPELINE_CACHE.clear()


class VehicleInfo(TypedDict, total=False):
    maker: str
    model_code: str
    model_designation: str
    category_number: str
    color_code: str
    first_reg: str
    type_no: str
    classification: str


class IdentifyResult(TypedDict, total=False):
    found: bool
    vehicle_code: Optional[str]
    confidence: float
    method: str
    notes: str


# 判定キーワード（cogni/Audatex 系） - 後方互換用
_COGNI_KEYWORDS = (
    "Audatex", "AUDATEX", "audatex",
    "Cogni7", "Cogni 7", "COGNI7", "コグニセブン", "コグニ7",
    "アウダテックス", "アウダテッスク",
)

# Iter2: 正規表現（大文字小文字無視）
# ベンダーを特定できる語だけを「コグニ系」の判定に使う。
# 「見積番号」「見積書No」はどの見積書にも載る一般語なので、これを入れると
# 他社の見積までコグニ扱い（モードB=完全複製）になってしまう。
_COGNI_REGEX = re.compile(
    r"(Audatex|アウダテックス|コグニ|Cogni\s*7?|AUDADAMS|"
    r"車両見積システム|自動車補修見積システム|Cognitive\s*Seven|"
    r"データ№|データ番号|DCS|FUJITSU|富士通)",
    re.IGNORECASE,
)

# 「そもそも見積書らしいか」の判定用（モード選択には使わない）
_ESTIMATE_FORM_REGEX = re.compile(
    r"(見積\s*書\s*No|見積番号|見積書|御見積|部品\s*計|工賃\s*計|合計)",
    re.IGNORECASE,
)


# ============================================================
# 1. PDFソース分類
# ============================================================
def _extract_pdf_text_layer(pdf_path: str) -> str:
    """PyMuPDF→pypdfium2 順で全ページのテキスト層を連結。失敗時は空文字 (Iter R10)"""
    # PyMuPDF を試す
    try:
        import fitz  # type: ignore
        doc = fitz.open(pdf_path)
        chunks = []
        for page in doc:
            try:
                chunks.append(page.get_text() or "")
            except Exception:
                continue
        doc.close()
        return "\n".join(chunks)
    except Exception as e_fitz:
        logger.debug("fitz失敗→pypdfium2 fallback: %s", e_fitz)
    # pypdfium2 fallback
    try:
        import pypdfium2 as pdfium  # type: ignore
        with _pdfium_lock():
            pdf = pdfium.PdfDocument(pdf_path)
            try:
                chunks = []
                for page in pdf:
                    try:
                        tp = page.get_textpage()
                        chunks.append(tp.get_text_range() or "")
                    except Exception:
                        continue
            finally:
                pdf.close()
        return "\n".join(chunks)
    except Exception as e:
        logger.warning("PDFテキスト抽出失敗 %s: %s", pdf_path, e)
        return ""


def _extract_pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    """bytes から PyMuPDF→pypdfium2 fallback でテキスト抽出 (Iter R10)"""
    try:
        import fitz  # type: ignore
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        chunks = []
        for page in doc:
            try:
                chunks.append(page.get_text() or "")
            except Exception:
                continue
        doc.close()
        return "\n".join(chunks)
    except Exception:
        pass
    try:
        import pypdfium2 as pdfium  # type: ignore
        import io
        with _pdfium_lock():
            pdf = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
            try:
                chunks = []
                for page in pdf:
                    try:
                        tp = page.get_textpage()
                        chunks.append(tp.get_text_range() or "")
                    except Exception:
                        continue
            finally:
                pdf.close()
        return "\n".join(chunks)
    except Exception as e:
        logger.warning("PDFテキスト抽出(bytes)失敗: %s", e)
        return ""


def classify_pdf_source(pdf_path_or_bytes, ocr_text: Optional[str] = "") -> PdfSource:
    """PDFソース判定 (cogni/other/unknown)。

    pdf_path_or_bytes: ファイルパス(str) または bytes（Iter2 仕様兼用）。
    ocr_text: OCR後テキスト（任意）。
    """
    try:
        embedded = ""
        if isinstance(pdf_path_or_bytes, (bytes, bytearray)):
            embedded = _extract_pdf_text_from_bytes(bytes(pdf_path_or_bytes))
        elif isinstance(pdf_path_or_bytes, str):
            if pdf_path_or_bytes and os.path.exists(pdf_path_or_bytes):
                embedded = _extract_pdf_text_layer(pdf_path_or_bytes)
        # 正規表現判定
        for txt in (embedded, ocr_text or ""):
            if txt and _COGNI_REGEX.search(txt):
                return "cogni"
        # キーワード文字列フォールバック
        for txt in (embedded, ocr_text or ""):
            if not txt:
                continue
            for kw in _COGNI_KEYWORDS:
                if kw in txt:
                    return "cogni"
        if (embedded and embedded.strip()) or (ocr_text and ocr_text.strip()):
            return "other"
        return "unknown"
    except Exception as e:
        logger.warning("classify_pdf_source 失敗: %s", e)
        return "unknown"


# ============================================================
# 2. 車種特定
# ============================================================
def identify_vehicle_in_addata(vehicle_info: Dict[str, Any],
                               addata_root: str = r"C:\Addata") -> IdentifyResult:
    """ADDATA で車両を特定する (v12 Phase A: identify_vehicle_wrapper 経由)。

    auto_matching.identify_vehicle_wrapper を呼び、3 段階フォールバック結果を
    IdentifyResult 形式に変換して返す。

    match_layer 1: 型式指定+類別+初度登録 (confidence=1.0, method='layer1_full')
    match_layer 2: 型式コード/Katashiki/fuzzy 複合 (confidence=0.7, method='layer2_attr')
    match_layer 3: TOYOTA_GENERIC template (found=True, confidence=0.0, method='layer3_template')
    match_layer 4: engine error (found=False, method='no_db')
    """
    result: IdentifyResult = {
        "found": False,
        "vehicle_code": None,
        "confidence": 0.0,
        "method": "none",
        "notes": "",
    }
    try:
        if not vehicle_info:
            result["notes"] = "vehicle_info empty"
            return result

        try:
            from auto_matching import identify_vehicle_wrapper  # type: ignore
        except Exception as e:
            result["method"] = "no_engine"
            result["notes"] = f"auto_matching import失敗: {e}"
            return result

        wrap = identify_vehicle_wrapper(addata_root or "", vehicle_info)
        layer = wrap.get("match_layer", 4)
        is_supported = bool(wrap.get("is_supported"))
        is_template = bool(wrap.get("is_template"))
        vc = wrap.get("vehicle_code") or None
        folder = wrap.get("folder") or ""
        reason = wrap.get("reason") or ""

        result["match_layer"] = layer  # type: ignore[typeddict-item]
        result["is_template"] = is_template  # type: ignore[typeddict-item]

        if layer == 1 and is_supported:
            result["found"] = True
            result["vehicle_code"] = vc
            result["confidence"] = 1.0
            result["method"] = "layer1_full"
            result["notes"] = f"folder={folder}"
        elif layer == 2 and is_supported:
            result["found"] = True
            result["vehicle_code"] = vc
            result["confidence"] = 0.7
            result["method"] = "layer2_attr"
            result["notes"] = f"folder={folder}"
            # 候補が割れたまま先頭を採ったケース。reason を捨てると
            # 画面には「連携成功」としか出ず、別型式のマスタで照合した
            # ことが利用者に伝わらない。
            _amb = wrap.get("ambiguous") or []
            if _amb:
                result["ambiguous"] = list(_amb)
                result["confidence"] = 0.4
                result["notes"] = (f"候補が{len(_amb)}件に割れています"
                                   f"({'/'.join(map(str, _amb))}): "
                                   f"{reason}")
        elif layer == 3:
            # TOYOTA_GENERIC template fallback: NEO は template ベースで生成可能
            result["found"] = True
            result["vehicle_code"] = vc or "TOYOTA_GENERIC"
            result["confidence"] = 0.0
            result["method"] = "layer3_template"
            result["notes"] = f"template fallback: {reason}"
        else:
            # layer 4 (engine error / no vehicle_data) → not found
            result["method"] = "no_db"
            result["notes"] = reason or "not found"
        return result
    except Exception as e:
        logger.warning("identify_vehicle_in_addata 失敗: %s", e)
        result["notes"] = f"error: {e}"
        return result


_GRADE_CACHE: Dict[str, Dict[str, Any]] = {}


def identify_grade_from_items(vehicle_code: str,
                              items: List[Dict[str, Any]],
                              addata_root: str = r"C:\Addata",
                              body_code: int = 0) -> Dict[str, Any]:
    """Iter15: PDFGradeIdentifier で部品名/価格からグレードを推定。
    戻り値: {grade_code, grade_name, confidence, option_codes, source}
    """
    out = {"grade_code": None, "grade_name": None, "confidence": 0.0,
           "option_codes": [], "source": "none"}
    if not vehicle_code or not items or not addata_root or not os.path.isdir(addata_root):
        return out
    # Iter R5: grade キャッシュ (vcode + body_code + items件数で簡易キー)
    # プロセスは全利用者で共有される。ルートも明細の中身もキーに
    # 入れないと、車種コードと行数が同じというだけで、別の利用者が
    # アップロードした別の Addata で推定したグレードが再利用される。
    try:
        _root_sig = os.path.realpath(addata_root) if addata_root else ''
    except Exception:
        _root_sig = str(addata_root or '')
    _items_sig = hashlib.sha1(
        "\x1f".join(
            f"{(i.get('parts_name') or i.get('name') or '')}|{i.get('unit_price') or ''}"
            for i in (items or [])
        ).encode('utf-8', 'replace')).hexdigest()[:16]
    cache_key = f"{_root_sig}:{vehicle_code}:{body_code}:{len(items)}:{_items_sig}"
    if cache_key in _GRADE_CACHE:
        return dict(_GRADE_CACHE[cache_key])
    try:
        from _addata_db_search import AddataSearchEngine  # type: ignore
        from _grade_identifier import PDFGradeIdentifier  # type: ignore
        engine = AddataSearchEngine(addata_root)
        ident = PDFGradeIdentifier(engine, vehicle_code, body_code=body_code)
        # PDFGradeIdentifier.identify() は内部で items を受け取る形を期待 → 呼び出し方を確認
        try:
            r = ident.identify(items)  # type: ignore
        except TypeError:
            # 古い API: 引数なし → instance に items を渡す
            try:
                ident.items = items  # type: ignore
                r = ident.identify()
            except Exception:
                r = {}
        if isinstance(r, dict):
            out["grade_code"] = r.get("grade_code") or r.get("grade")
            out["grade_name"] = r.get("grade_name")
            out["confidence"] = float(r.get("confidence") or r.get("score") or 0)
            out["option_codes"] = r.get("option_codes") or []
            out["source"] = "PDFGradeIdentifier"
    except Exception as e:
        logger.debug("identify_grade_from_items skip: %s", e)
        out["source"] = f"err:{e}"
    # Iter R5: 結果キャッシュ
    if len(_GRADE_CACHE) > 64:
        _GRADE_CACHE.pop(next(iter(_GRADE_CACHE)))
    _GRADE_CACHE[cache_key] = dict(out)
    return out


# ============================================================
# 3. モード判定
# ============================================================
def decide_mode(found_or_source, source_or_addata=None) -> GenerationMode:
    """found / source からモード A/B/C を決定。

    後方互換: decide_mode(found: bool, source: str)
    Iter2 仕様: decide_mode(source_kind: str, addata_hit: dict)
    """
    # 引数パターンを判別
    if isinstance(found_or_source, str) and isinstance(source_or_addata, dict):
        # Iter2: (source_kind, addata_hit)
        source = found_or_source
        found = bool(source_or_addata.get("found"))
    else:
        # Iter1: (found, source)
        found = bool(found_or_source)
        source = source_or_addata if isinstance(source_or_addata, str) else "unknown"

    if not found:
        return "A"
    if source == "cogni":
        return "B"
    return "C"


def decide_mode_from_identify(ident: Dict[str, Any], source: str) -> GenerationMode:
    """identify結果からモードを決める。

    Addataが無い時 identify は「TOYOTA_GENERIC のテンプレで代用した」という
    意味で found=True を返す（match_layer=3 / is_template=True）。これは
    車種DBに当たったわけではないので、モードA（ベタ打ち）として扱う。
    """
    if not isinstance(ident, dict):
        return decide_mode(False, source)
    if ident.get("is_template") or ident.get("match_layer") == 3:
        return "A"
    return decide_mode(bool(ident.get("found")), source)


# ============================================================
# 4. 品番マーカー
# ============================================================
_PRICE_MISMATCH_MARK = "※部品価格不一致"
_DB_MISS_MARK = "※DB不一致"


def _decorate_part_no(orig: Any, marker: str) -> str:
    base = str(orig or "").strip()[:25]
    if not base:
        return marker
    return f"{base}　{marker}"  # 全角スペース区切り


def _append_remark(item: Dict[str, Any], marker: str):
    """remarks 末尾にマーカー追記（既存 remarks 保持）。"""
    cur = str(item.get("remarks", "") or "")
    if marker in cur:
        return
    item["remarks"] = (cur + " " + marker).strip()


PRICE_MISMATCH_TOL_RATIO = 0.02  # ±2% 以内なら一致とみなす (Iter3)
DB_MISS_SCORE_THRESHOLD = 0.6    # マッチスコア >= 0.6 なら DB不一致マーカー付けない (Iter3)


def _apply_price_mismatch_marker(items: List[Dict[str, Any]],
                                 tol_ratio: float = PRICE_MISMATCH_TOL_RATIO) -> List[Dict[str, Any]]:
    """db_price と unit_price の差を検出して品番にマーカー付与（Iter3: ±tol_ratio 許容）"""
    if not items:
        return items
    out = []
    for it in items:
        nit = dict(it)
        try:
            db_price = nit.get("db_price")
            unit_price = nit.get("unit_price") or nit.get("part_price") or 0
            try:
                db_p = float(db_price) if db_price not in (None, "") else 0.0
            except (TypeError, ValueError):
                db_p = 0.0
            try:
                up = float(unit_price) if unit_price not in (None, "") else 0.0
            except (TypeError, ValueError):
                up = 0.0
            tol_abs = max(db_p, up) * tol_ratio
            if db_p > 0 and up > 0 and abs(db_p - up) > max(tol_abs, 1):
                nit["parts_no"] = _decorate_part_no(nit.get("parts_no"), _PRICE_MISMATCH_MARK)
                nit["price_mismatch"] = True
                _append_remark(nit, _PRICE_MISMATCH_MARK)
        except Exception as e:
            logger.debug("price marker skip: %s", e)
        out.append(nit)
    return out


def _apply_db_miss_marker(items: List[Dict[str, Any]],
                          score_threshold: float = DB_MISS_SCORE_THRESHOLD) -> List[Dict[str, Any]]:
    """match_level == 'L4' か match_score < threshold の行に DB不一致マーカー付与（Iter3）"""
    if not items:
        return items
    out = []
    for it in items:
        nit = dict(it)
        try:
            ml = str(nit.get("match_level", "")).upper()
            score = nit.get("match_score")
            try:
                score_f = float(score) if score not in (None, "") else None
            except (TypeError, ValueError):
                score_f = None
            should_mark = (ml == "L4") or (score_f is not None and score_f < score_threshold)
            if should_mark:
                nit["parts_no"] = _decorate_part_no(nit.get("parts_no"), _DB_MISS_MARK)
                nit["db_miss"] = True
                _append_remark(nit, _DB_MISS_MARK)
        except Exception as e:
            logger.debug("db_miss marker skip: %s", e)
        out.append(nit)
    return out


# ============================================================
# 5. 内部ユーティリティ: テンプレートNEO読込 / generate_neo_file 呼出
# ============================================================
def _load_template_bytes(template_path: Optional[str]) -> bytes:
    if not template_path:
        here = os.path.dirname(os.path.abspath(__file__))
        cand = [
            os.path.join(here, "template_toyota.neo"),
            os.path.join(here, "テンプレート_トヨタ汎用_.neo"),
        ]
        for c in cand:
            if os.path.exists(c):
                template_path = c
                break
    if not template_path or not os.path.exists(template_path):
        raise FileNotFoundError(f"テンプレートNEOが見つかりません: {template_path}")
    with open(template_path, "rb") as f:
        return f.read()


def _fallback_parts_no_from_db(items: List[Dict[str, Any]],
                               vehicle_code: Optional[str],
                               addata_root: str) -> List[Dict[str, Any]]:
    """Iter13: 品番が空の行で、部品名から ADDATA を逆引きして補完。
    高コストなのでvehicle_code が確定している場合のみ実行。
    """
    if not items or not vehicle_code or not addata_root:
        return items
    if not os.path.exists(addata_root):
        return items
    try:
        from _addata_db_search import AddataSearchEngine  # type: ignore
        engine = AddataSearchEngine(addata_root)
        parts_master = engine.get_all_parts(vehicle_code)
        if not parts_master:
            return items
    except Exception as e:
        logger.debug("fallback_parts_no init失敗: %s", e)
        return items

    import difflib
    name_to_pno = {}
    for p in parts_master:
        nm = (p.get("name") or "").strip()
        pno = (p.get("parts_no") or "").strip()
        if nm and pno and nm not in name_to_pno:
            name_to_pno[nm] = pno
    names_list = list(name_to_pno.keys())

    out = []
    hit = 0
    for it in items:
        nit = dict(it) if isinstance(it, dict) else it
        if isinstance(nit, dict) and not (nit.get("parts_no") or nit.get("part_no")):
            nm = str(nit.get("name") or nit.get("parts_name") or "").strip()
            if nm:
                m = difflib.get_close_matches(nm, names_list, n=1, cutoff=0.7)
                if m:
                    nit["parts_no"] = name_to_pno[m[0]]
                    nit["parts_no_source"] = "db_fallback"
                    hit += 1
        out.append(nit)
    if hit:
        logger.info("fallback_parts_no: %d 件補完", hit)
    return out


def _final_dedup_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """ページ跨ぎで二重に読まれた明細だけを統合する。

    見積書には同じ部品が同じ金額で複数行並ぶこと（クリップ2個など）が
    普通にあるため、以前の「同名同金額なら全部まとめる」実装は正当な明細を
    削っていた。本アプリの方針は「同一ページ内の重複行は原本通り全て保持」
    なので、ここで落とすのは
      - 直前の行と完全に一致（品名・品番・部品金額・工賃）し、かつ
      - ページ番号が異なる（＝ページ境界の二重読み取り）
    ものだけに限定する。
    """
    if not items or len(items) <= 1:
        return items

    def _sig(it):
        return (
            str(it.get("name") or it.get("parts_name") or "").strip().lower(),
            str(it.get("part_no") or it.get("parts_no") or "").strip().lower(),
            _to_int(it.get("parts_amount") or it.get("part_price")),
            _to_int(it.get("wage") or it.get("labor_fee")),
            str(it.get("index_value") or "").strip(),
        )

    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            out.append(it)
            continue
        try:
            if out and isinstance(out[-1], dict):
                prev = out[-1]
                sig = _sig(it)
                if sig[0] and sig == _sig(prev):
                    prev_page = prev.get("page")
                    cur_page = it.get("page")
                    # page が無い明細（CSV取り込み等）は、どのページ由来か
                    # 判断できないので統合しない。誤って正当な明細を消すより、
                    # 重複が残る方が実害が小さい。
                    if prev_page is not None and cur_page is not None and prev_page != cur_page:
                        logger.info("[dedup] ページ境界の重複行を統合: %s (p%s/p%s)",
                                    sig[0], prev_page, cur_page)
                        continue
        except Exception:
            pass
        out.append(it)
    return out


def _is_discount_row(it: Dict[str, Any]) -> bool:
    """値引き・割引の行か。app.py の _has_disc_row と同じ規則を使う。

    見積書に印字された「部品計」「工賃計」は値引き前の小計なので、
    値引き行を含んだ明細合算と直接くらべると、必ず値引き額ぶんの差が出る。
    その差を「読み落とし」と解釈して調整行を足すと、値引きを打ち消す行が
    原本に無いまま増えてしまう。
    """
    name = str(it.get("name", "") or it.get("parts_name", "") or "")
    if re.search(r"(値引|割引)", name):
        return True
    return _to_int(it.get("wage", 0)) < 0 or _to_int(it.get("parts_amount", 0)) < 0


def _sum_items_outtax(items: List[Dict[str, Any]], skip_discount: bool = False) -> int:
    """明細の合算（部品＋工賃）。skip_discount で値引き・調整行を除く。"""
    total = 0
    for it in items or []:
        if skip_discount and (it.get("is_adjustment_row") or _is_discount_row(it)):
            continue
        try:
            pa = it.get("parts_amount") or it.get("amount") or 0
            if pa:
                total += _to_int(pa)
            else:
                up = _to_float(it.get("unit_price") or it.get("part_price"))
                qty = max(_to_int(it.get("quantity"), 1), 1)
                if up > 0:
                    total += int(up * qty)
        except Exception:
            pass
        try:
            total += _to_int(it.get("wage", 0) or it.get("labor_fee", 0) or 0)
        except Exception:
            pass
    return total


def _enforce_grand_total_match(items: List[Dict[str, Any]],
                               pdf_grand_total: int,
                               is_tax_inclusive: bool = True,
                               tax_rate: float = 0.10,
                               tolerance: int = 0) -> List[Dict[str, Any]]:
    """v7.1: pdf_grand_total を真理値として、明細合算が一致するか最終チェック。
    乖離があれば末尾の調整行(なければ新規)に差分を加える。

    pdf_grand_total が税込なら、税抜合計 = grand_total / 1.1 で逆算。
    """
    if not items or pdf_grand_total <= 0:
        return items
    out = list(items)
    target_outtax = pdf_grand_total / (1 + tax_rate) if is_tax_inclusive else pdf_grand_total
    target_outtax = int(round(target_outtax))

    sum_outtax = 0
    for it in out:
        try:
            pa = it.get("parts_amount") or it.get("amount") or 0
            if pa:
                sum_outtax += _to_int(pa)
            else:
                up = _to_float(it.get("unit_price") or it.get("part_price"))
                qty = max(_to_int(it.get("quantity"), 1), 1)
                if up > 0:
                    sum_outtax += int(up * qty)
        except Exception:
            pass
        try:
            wg = it.get("wage") or it.get("labor_fee") or 0
            if wg:
                sum_outtax += _to_int(wg)
        except Exception:
            pass

    diff = target_outtax - sum_outtax
    if abs(diff) <= tolerance:
        return out

    # 既存の調整行があれば加算、なければ新規作成
    adj_idx = next((i for i, it in enumerate(out) if it.get("is_adjustment_row")), None)
    if adj_idx is not None:
        existing = out[adj_idx]
        cur_pa = _to_int(existing.get("parts_amount"))
        existing["parts_amount"] = cur_pa + diff
        existing["amount"] = cur_pa + diff
        existing["unit_price"] = cur_pa + diff
        logger.info("[grand_total_match] 調整行更新 +%d (target=%d sum=%d)", diff, target_outtax, sum_outtax)
    else:
        out.append({
            "name": "※金額調整(PDF総額差)",
            "parts_name": "※金額調整",
            "parts_no": "※金額調整",
            "part_no": "※金額調整",
            "quantity": 1,
            "parts_amount": int(diff),
            "amount": int(diff),
            "unit_price": int(diff),
            "wage": 0,
            "labor_fee": 0,
            "category": "",
            "work_code": "",
            "index_value": 0.0,
            "is_adjustment_row": True,
        })
        logger.info("[grand_total_match] 調整行新規 %+d (target=%d sum=%d)", diff, target_outtax, sum_outtax)
    return out


def _enforce_total_match(items: List[Dict[str, Any]],
                         pdf_parts_total: int,
                         pdf_wage_total: int,
                         tolerance: int = 0) -> List[Dict[str, Any]]:
    """v7: PDF表示の部品計/工賃計と明細合算の差分を「※金額調整」行で吸収。
    ADDATA配備時もベタ打ち時も最終合計を PDF表示金額に完全一致させる。

    入力:
      pdf_parts_total: PDF表示の部品計 (税抜)
      pdf_wage_total: PDF表示の工賃計 (税抜)
      tolerance: ±これ以下の差は無視

    動作:
      - sum(parts_amount or unit_price*qty) と pdf_parts_total を比較
      - 差分 > tolerance → 末尾に調整行追加
        * parts_amount = 部品差分
        * wage = 工賃差分
        * parts_no = "※金額調整"
        * name = "※金額調整 (PDF原本との差分吸収)"
    """
    if not items:
        return items
    if pdf_parts_total <= 0 and pdf_wage_total <= 0:
        return items  # PDF総額未取得 → 調整しない
    out = list(items)
    # 明細合算。pdf_parts_total / pdf_wage_total は見積書に印字された
    # 「値引き前」の小計なので、値引き行・既に足した調整行を混ぜて比べると
    # 必ず値引き額ぶんの差が出て、それを埋める行を1本捏造してしまう。
    sum_parts = 0
    sum_wage = 0
    for it in out:
        if it.get("is_adjustment_row") or _is_discount_row(it):
            continue
        try:
            pa = it.get("parts_amount") or it.get("amount") or 0
            if pa:
                sum_parts += _to_int(pa)
            else:
                up = _to_float(it.get("unit_price") or it.get("part_price"))
                qty = max(_to_int(it.get("quantity"), 1), 1)
                if up > 0:
                    sum_parts += int(up * qty)
        except Exception:
            pass
        try:
            wg = it.get("wage") or it.get("labor_fee") or 0
            if wg:
                sum_wage += _to_int(wg)
        except Exception:
            pass

    diff_parts = pdf_parts_total - sum_parts if pdf_parts_total else 0
    diff_wage = pdf_wage_total - sum_wage if pdf_wage_total else 0

    if abs(diff_parts) <= tolerance and abs(diff_wage) <= tolerance:
        return out  # 既に一致

    if diff_parts == 0 and diff_wage == 0:
        return out

    # 調整行を追加
    adj = {
        "name": "※金額調整(部品/工賃差)",
        "parts_name": "※金額調整",
        "parts_no": "※金額調整",
        "part_no": "※金額調整",
        "quantity": 1,
        "parts_amount": int(diff_parts),
        "amount": int(diff_parts),
        "unit_price": int(diff_parts),
        "wage": int(diff_wage),
        "labor_fee": int(diff_wage),
        "category": "",
        "work_code": "",
        "index_value": 0.0,
        "is_adjustment_row": True,
    }
    out.append(adj)
    logger.info("[total_match] adj parts=%+d wage=%+d (PDF parts=%d wage=%d / 明細 parts=%d wage=%d)",
                diff_parts, diff_wage, pdf_parts_total, pdf_wage_total, sum_parts, sum_wage)
    return out


def _normalize_items_for_neo(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """OCR items を generate_neo_file 期待スキーマに正規化 (Iter6)
    OCR出力キー: name, part_no, quantity, parts_amount, wage, line_total, work_code, index_value
    NEO期待キー: name, parts_no, quantity, unit_price, wage, category
    """
    if not items:
        return items
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        nit = dict(it)
        # 品番キー統一
        if not nit.get("parts_no"):
            nit["parts_no"] = nit.get("part_no") or nit.get("part_number") or ""
        # v4: app.generate_neo_file は part_no を読むため、parts_no を part_no にもコピー
        if nit.get("parts_no") and not nit.get("part_no"):
            nit["part_no"] = nit["parts_no"]
        # 数量 (v11.0: _to_int で括弧書き対策)
        qty_i = max(_to_int(nit.get("quantity"), 1), 1)
        nit["quantity"] = qty_i
        # 単価: unit_price 未設定なら parts_amount/qty で算出
        if not nit.get("unit_price"):
            pa = _to_float(nit.get("parts_amount"))
            if pa > 0 and qty_i > 0:
                nit["unit_price"] = int(round(pa / qty_i))
            else:
                nit["unit_price"] = _to_int(nit.get("part_price"))
        # 工賃
        nit["wage"] = _to_int(nit.get("wage") or nit.get("labor_fee"))
        # parts_amount / index_value も安全変換
        if "parts_amount" in nit:
            nit["parts_amount"] = _to_int(nit.get("parts_amount"))
        if "index_value" in nit:
            iv = _to_float(nit.get("index_value"))
            nit["index_value"] = iv
        # カテゴリ（取替/脱着/修理）
        if not nit.get("category"):
            nit["category"] = nit.get("work_code") or ""
        # 部品名
        if not nit.get("name"):
            nit["name"] = nit.get("parts_name") or nit.get("work_or_part_name") or ""
        out.append(nit)
    return out


def _call_generate_neo(template_bytes: bytes,
                       customer_info: Dict[str, Any],
                       items: List[Dict[str, Any]],
                       is_beta_mode: bool = False,
                       is_tax_inclusive: bool = False,
                       merge_mode: bool = False,
                       expenses: Optional[Dict[str, Any]] = None) -> bytes:
    """app.generate_neo_file の薄ラッパ。lazy import + items正規化(Iter6)"""
    try:
        from app import generate_neo_file  # type: ignore
    except Exception as e:
        raise RuntimeError(f"app.generate_neo_file の import 失敗: {e}") from e
    deduped = _final_dedup_items(items or [])
    norm_items = _normalize_items_for_neo(deduped)
    neo_bytes, _tp, _tw, _gt = generate_neo_file(
        template_data=template_bytes,
        customer_info=customer_info or {},
        items=norm_items,
        short_parts_wage=0,
        insurance_info={},
        # 画面のサイドバーで入れたレッカー代・代車費用・非課税費用。
        # None 固定だったため、この経路で作った .neo には費用が1円も
        # 入らず、警告も出なかった（入力した費用の全額とその消費税が消える）。
        expenses=expenses or None,
        # 見積書の明細金額が税込表記かどうか。税込なら generate_neo_file 側で
        # 税抜に逆算される。ここを決め打ちにすると、税込表記の見積を
        # 取り込んだときに総額が消費税ぶん膨らむ。
        is_tax_inclusive=is_tax_inclusive,
        # モードA(ベタ打ち)ではDB照合していないので、未マッチを表す ※ を
        # 品名に付けてはいけない（付けると全品名が ※ 付きで出荷される）
        is_beta_mode=is_beta_mode,
        # 利用者が過去の .neo をテンプレートに指定したときは、画面の主経路と
        # 同じくマージモードにする。決め打ちで False にしていたため、この経路
        # だけ前案件の車の色・カラーコード・受付番号・アジャスター名が
        # DB側に残り、しかも同じ .neo のヘッダXML側は空という食い違いが出ていた。
        merge_mode=merge_mode,
    )
    return neo_bytes


def _normalize_jp_date(s: str) -> str:
    """v5: 和暦表記 ('R 2. 12. 16' / '令和6年9月' / 'H30.5.1') → YYYYMMDD 8桁文字列。
    変換不能なら "00000000"。既に8桁なら そのまま。"""
    if not s:
        return ""
    s = str(s).strip()
    if not s or s == "不明":
        return ""
    # 既に8桁数字
    if len(s) == 8 and s.isdigit():
        return s
    import re as _re
    # 和暦判定
    era_map = {"R": 2018, "令和": 2018, "H": 1988, "平成": 1988,
               "S": 1925, "昭和": 1925, "T": 1911, "大正": 1911}
    base = None
    rest = s
    for prefix, yr_off in era_map.items():
        if s.startswith(prefix):
            base = yr_off
            rest = s[len(prefix):]
            break
    if base is None:
        # 西暦4桁
        m = _re.search(r"(20\d{2})[\.\-/年]?\s*(\d{1,2})[\.\-/月]?\s*(\d{1,2})?", s)
        if m:
            yr = int(m.group(1))
            mo = int(m.group(2))
            da = int(m.group(3) or 1)
            return f"{yr:04d}{mo:02d}{da:02d}"
        return ""
    # 和暦数字抽出
    m = _re.search(r"(\d+)\D+(\d+)\D+(\d+)", rest)
    if m:
        wy, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yr = base + wy
        return f"{yr:04d}{mo:02d}{da:02d}"
    m = _re.search(r"(\d+)\D+(\d+)", rest)
    if m:
        wy, mo = int(m.group(1)), int(m.group(2))
        yr = base + wy
        return f"{yr:04d}{mo:02d}00"
    return ""


def _merge_vehicle_into_customer(vehicle_info: Dict[str, Any],
                                 customer_info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cust = dict(customer_info or {})
    # Iter2 (v3): generate_neo_file の Car テーブル書込で使う全キーを伝搬
    # v13 Step C: ADDATA Car テーブル UPDATE 用キーも追加（maker_code/car_code/etc）
    keys = (
        "car_serial_no", "car_model_designation", "car_category_number",
        "car_reg_date", "term_date", "car_name", "car_model",
        "color_code", "body_color", "engine_model", "trim_code",
        "grade", "model_year", "mileage", "model_code",
        "customer_name", "owner_name", "postal_no",
        "prefecture", "municipality", "address_other",
        "car_reg_department", "car_reg_division", "car_reg_business", "car_reg_serial",
        "car_weight", "engine_displacement", "kilometer",
        "repair_shop_name",
        # v13 Step C: NEO Car テーブル拡張列
        "maker_code", "car_code", "car_form_code", "form_code_1", "form_code_2",
        "fva_name", "body_code", "grade_code", "year_code", "finish_code", "color_name",
    )
    for k in keys:
        if vehicle_info.get(k) and not cust.get(k):
            cust[k] = vehicle_info[k]
    # v5: 日付フィールドを YYYYMMDD に正規化
    for date_k in ("car_reg_date", "term_date", "first_reg_date"):
        v = cust.get(date_k)
        if v and not (str(v).isdigit() and len(str(v)) == 8):
            try:
                norm = _normalize_jp_date(str(v))
                if norm:
                    cust[date_k] = norm
                else:
                    # 変換不能なら空文字に倒して app.py の int 変換死を防ぐ
                    cust[date_k] = ""
            except Exception:
                cust[date_k] = ""
    return cust


# ============================================================
# 6. NEO生成 3系統
# ============================================================
def build_neo_mode_a(items: List[Dict[str, Any]],
                     vehicle_info: Dict[str, Any],
                     template_path: Optional[str] = None,
                     customer_info: Optional[Dict[str, Any]] = None,
                     is_tax_inclusive: bool = False,
                     merge_mode: bool = False,
                     expenses: Optional[Dict[str, Any]] = None) -> bytes:
    """モードA: ベタ打ち (収録外)。OCR項目をそのまま転写。"""
    tpl = _load_template_bytes(template_path)
    cust = _merge_vehicle_into_customer(vehicle_info or {}, customer_info)
    # ベタ打ち: DB照合していないため ※（DB未マッチ印）を付けない
    return _call_generate_neo(tpl, cust, items or [], is_beta_mode=True,
                              is_tax_inclusive=is_tax_inclusive,
                              merge_mode=merge_mode,
                              expenses=expenses)


def build_neo_mode_b(items: List[Dict[str, Any]],
                     vehicle_info: Dict[str, Any],
                     template_path: Optional[str] = None,
                     addata_root: str = r"C:\Addata",
                     customer_info: Optional[Dict[str, Any]] = None,
                     is_tax_inclusive: bool = False,
                     merge_mode: bool = False,
                     expenses: Optional[Dict[str, Any]] = None) -> bytes:
    """モードB: 完全複製 (cogni判定)。価格不一致マーカー付与。"""
    matched = items or []
    # Iter13: 品番空 → DB逆引き補完
    vcode = (vehicle_info or {}).get("model_code") or (vehicle_info or {}).get("vehicle_code")
    matched = _fallback_parts_no_from_db(matched, vcode, addata_root)
    # v4: フルADDATAマッチング (parts_no_marked, db_price, db_work_index 付与)
    v4_done = False
    try:
        from auto_matching import _full_addata_match  # type: ignore
        matched = _full_addata_match(matched, vehicle_info or {}, addata_root)
        # parts_no を marked で上書き (NEOに反映)
        for it in matched:
            mk = it.get("parts_no_marked")
            if mk is not None:
                it["parts_no"] = mk
                it["part_no"] = mk  # v4: app.generate_neo_file 互換
            # ADDATAの指数で見積の工数を上書きしない。工賃は見積の値のまま
            # 書かれるので、上書きすると「工数はADDATA・工賃は見積」という
            # 行ができ、工数×レバーレートが工賃と合わなくなる。協定の場で
            # 説明できない見積になるうえ、元見積と同じ内容という条件も崩れる。
            # ADDATA側の値は db_work_index として保持し、食い違う行は知らせる。
            db_wi = it.get("db_work_index")
            if db_wi and isinstance(db_wi, (int, float)) and db_wi > 0:
                try:
                    _pdf_idx = _to_float(it.get("index_value"))
                    _db_idx = round(float(db_wi) / 100.0, 2)
                    if _pdf_idx <= 0:
                        # 見積側に工数が無い行だけ、ADDATAの指数で補う
                        it["index_value"] = _db_idx
                    elif abs(_pdf_idx - _db_idx) > 0.005:
                        it["index_mismatch"] = (_pdf_idx, _db_idx)
                except Exception:
                    pass
        v4_done = True
    except Exception as e:
        logger.warning("modeB v4 full_match失敗: %s (既存ロジック継続)", e)
    if not v4_done:
        try:
            from auto_matching import match_pdf_items_to_addata  # type: ignore
            matched = match_pdf_items_to_addata(matched, vehicle_info or {}, addata_root)
            for it in matched:
                db_pno = it.get("db_parts_no")
                if db_pno and str(it.get("match_level", "")).upper() in ("L1", "L2"):
                    it.setdefault("orig_parts_no", it.get("parts_no", ""))
                    it["parts_no"] = db_pno
                try:
                    db_p = _to_float(it.get("db_price"))
                    up = _to_float(it.get("unit_price"))
                    if db_p > 0 and up > 0 and abs(db_p - up) >= 1:
                        it["parts_no"] = _decorate_part_no(it.get("parts_no"), _PRICE_MISMATCH_MARK)
                        it["price_mismatch"] = True
                        _append_remark(it, _PRICE_MISMATCH_MARK)
                except (TypeError, ValueError):
                    pass
        except Exception as e:
            logger.warning("modeB マッチング失敗: %s (素通し+価格マーカーのみ)", e)
            matched = _apply_price_mismatch_marker(items or [])

    tpl = _load_template_bytes(template_path)
    cust = _merge_vehicle_into_customer(vehicle_info or {}, customer_info)
    return _call_generate_neo(tpl, cust, matched,
                              is_tax_inclusive=is_tax_inclusive,
                              merge_mode=merge_mode,
                              expenses=expenses)


def build_neo_mode_c(items: List[Dict[str, Any]],
                     vehicle_info: Dict[str, Any],
                     template_path: Optional[str] = None,
                     addata_root: str = r"C:\Addata",
                     customer_info: Optional[Dict[str, Any]] = None,
                     is_tax_inclusive: bool = False,
                     merge_mode: bool = False,
                     expenses: Optional[Dict[str, Any]] = None) -> bytes:
    """モードC: あいまい複製。L4 or db_parts_no 空 → ※ADDATA該当なし マーカー。"""
    matched = items or []
    vcode = (vehicle_info or {}).get("model_code") or (vehicle_info or {}).get("vehicle_code")
    matched = _fallback_parts_no_from_db(matched, vcode, addata_root)
    # v4: フルADDATAマッチング (parts_no_marked 付与) — 成功なら旧ロジックスキップ
    v4_done = False
    try:
        from auto_matching import _full_addata_match  # type: ignore
        matched = _full_addata_match(matched, vehicle_info or {}, addata_root)
        for it in matched:
            mk = it.get("parts_no_marked")
            if mk is not None:
                it["parts_no"] = mk
                it["part_no"] = mk  # v4: app.generate_neo_file 互換
            # ADDATAの指数で見積の工数を上書きしない。工賃は見積の値のまま
            # 書かれるので、上書きすると「工数はADDATA・工賃は見積」という
            # 行ができ、工数×レバーレートが工賃と合わなくなる。協定の場で
            # 説明できない見積になるうえ、元見積と同じ内容という条件も崩れる。
            # ADDATA側の値は db_work_index として保持し、食い違う行は知らせる。
            db_wi = it.get("db_work_index")
            if db_wi and isinstance(db_wi, (int, float)) and db_wi > 0:
                try:
                    _pdf_idx = _to_float(it.get("index_value"))
                    _db_idx = round(float(db_wi) / 100.0, 2)
                    if _pdf_idx <= 0:
                        # 見積側に工数が無い行だけ、ADDATAの指数で補う
                        it["index_value"] = _db_idx
                    elif abs(_pdf_idx - _db_idx) > 0.005:
                        it["index_mismatch"] = (_pdf_idx, _db_idx)
                except Exception:
                    pass
        v4_done = True
    except Exception as e:
        logger.warning("modeC v4 full_match失敗: %s", e)
    if v4_done:
        # v4成功 → 旧ロジックはスキップして即返却
        tpl = _load_template_bytes(template_path)
        cust = _merge_vehicle_into_customer(vehicle_info or {}, customer_info)
        return _call_generate_neo(tpl, cust, matched,
                              is_tax_inclusive=is_tax_inclusive,
                              merge_mode=merge_mode,
                              expenses=expenses)
    try:
        from auto_matching import match_pdf_items_to_addata  # type: ignore
        matched = match_pdf_items_to_addata(matched, vehicle_info or {}, addata_root)
        # 価格・部品番号は PDF 原文を採用 → parts_no は触らない
        # db_parts_no 空 の行も L4 と同等に扱う（mode_c 仕様）
        for it in matched:
            if not str(it.get("db_parts_no", "") or "").strip():
                it["match_level"] = "L4"
        matched = _apply_db_miss_marker(matched)
    except Exception as e:
        logger.warning("modeC マッチング失敗: %s (全行 L4 とみなしマーク)", e)
        for it in (matched or []):
            it["match_level"] = "L4"
        matched = _apply_db_miss_marker(matched or [])

    tpl = _load_template_bytes(template_path)
    cust = _merge_vehicle_into_customer(vehicle_info or {}, customer_info)
    return _call_generate_neo(tpl, cust, matched,
                              is_tax_inclusive=is_tax_inclusive,
                              merge_mode=merge_mode,
                              expenses=expenses)


# ============================================================
# 7. verify_neo_against_pdf
# ============================================================
def _find_erparts_blob(files: Dict[str, bytes]) -> Optional[bytes]:
    """NEO内部ファイルから ERParts テーブルを持つ SQLite を探して返す。

    本アプリのテンプレートでは AnSMB.txt が明細DB。旧実装は AnSvEm0001.sld を
    決め打ちしていたが、そちらは INI ファイルのため "file is not a database" で
    検証が必ず失敗していた。
    """
    if not files:
        return None
    _SQLITE_MAGIC = b"SQLite format 3"

    def _has_erparts(blob: bytes) -> bool:
        if not blob or blob[:15] != _SQLITE_MAGIC:
            return False
        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        try:
            tf.write(blob)
            tf.close()
            conn = sqlite3.connect(tf.name)
            try:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
            return "ERParts" in names
        except sqlite3.Error:
            return False
        finally:
            try:
                os.unlink(tf.name)
            except OSError:
                pass

    # 既知の名前を優先し、その後で全走査
    for name in ("AnSMB.txt", "AnSvEm0001.sld"):
        blob = files.get(name)
        if _has_erparts(blob):
            return blob
    for name, blob in files.items():
        if name in ("AnSMB.txt", "AnSvEm0001.sld"):
            continue
        if _has_erparts(blob):
            return blob
    return None


def verify_neo_against_pdf(neo_bytes: bytes, items: List[Dict[str, Any]],
                           pdf_parts_total: Optional[int] = None,
                           pdf_wage_total: Optional[int] = None,
                           is_tax_inclusive: bool = False,
                           tax_rate: float = 0.10) -> Dict[str, Any]:
    """生成したNEOの明細を、見積書PDFの金額と突き合わせる。

    pdf_parts_total / pdf_wage_total には、見積書に「印字されている」合計
    （OCRのヘッダ解析結果）を渡す。渡された場合はそちらを正とする。
    渡さない場合は items の合計と比べるが、それは「入れたものが入っている」
    ことを確認するだけの自明な検証にしかならず、明細の取りこぼしを見逃す。
    """
    pdf_parts_total_arg = pdf_parts_total
    pdf_wage_total_arg = pdf_wage_total
    res = {
        "ok": False,
        "count_match": False,
        "total_match": False,
        "neo_count": 0,
        "pdf_count": len(items or []),
        "neo_total": 0,
        "pdf_total": 0,
        "mismatches": [],
    }
    try:
        # PDF total 計算 (v11.0 Phase A-3: parts_amount + wage を直接合計。
        # 旧 unit_price*qty 方式は本アプリ items 仕様と不整合だった BUG-2 修正)
        # NEO の ERParts は「部品(税抜)」だけを持ち、工賃は別テーブルに入る。
        # そのため総額一致判定は部品(税抜)どうしで行い、工賃・総額は表示用に別途保持する。
        pdf_parts_total = 0
        pdf_wage_total = 0
        for it in (items or []):
            try:
                pa = _to_int(it.get("parts_amount") or it.get("amount") or it.get("part_price"))
                wg = _to_int(it.get("wage") or it.get("labor_fee"))
                if pa or wg:
                    pdf_parts_total += pa
                    pdf_wage_total += wg
                else:
                    # フォールバック: 旧来の unit_price * quantity
                    up = _to_float(it.get("unit_price"))
                    qty = max(_to_int(it.get("quantity"), 1), 1)
                    pdf_parts_total += int(up * qty)
            except (TypeError, ValueError):
                pass
        items_parts_total = pdf_parts_total
        items_wage_total = pdf_wage_total
        # 見積書に印字された合計が渡されていればそちらを正とする
        if pdf_parts_total_arg is not None and _to_int(pdf_parts_total_arg) > 0:
            pdf_parts_total = _to_int(pdf_parts_total_arg)
            res["total_source"] = "pdf_header"
        else:
            res["total_source"] = "items_sum"
        if pdf_wage_total_arg is not None and _to_int(pdf_wage_total_arg) > 0:
            pdf_wage_total = _to_int(pdf_wage_total_arg)
        # NEO の ERParts は常に税抜。見積書が税込表記なら、比較する前に
        # PDF 側を税抜へ換算する。換算しないと税込を選ぶたびに必ず
        # 「差異あり」と警告が出て、正しい .neo を疑わせてしまう。
        if is_tax_inclusive:
            pdf_parts_total = int(round(pdf_parts_total / (1 + tax_rate)))
            pdf_wage_total  = int(round(pdf_wage_total / (1 + tax_rate)))
        pdf_total = pdf_parts_total + pdf_wage_total
        res["pdf_parts_total"] = pdf_parts_total
        res["pdf_wage_total"] = pdf_wage_total
        res["pdf_total"] = pdf_total
        res["items_parts_total"] = items_parts_total
        res["items_wage_total"] = items_wage_total

        if not neo_bytes:
            res["error"] = "neo_bytes empty"
            return res

        # NEO 展開 → AnSvEm0001.sld 取得
        try:
            from app import find_real_cks, decompress_neo, parse_entries, extract_files  # type: ignore
        except Exception as e:
            res["error"] = f"app helper import失敗: {e}"
            return res

        try:
            real_ck = find_real_cks(neo_bytes)
            full_raw = decompress_neo(neo_bytes, real_ck)
            _mgmt, entries = parse_entries(neo_bytes, real_ck[0])
            files = extract_files(full_raw, entries)
        except Exception as e:
            res["error"] = f"NEO展開失敗: {e}"
            return res

        # 本アプリのNEOレイアウトでは ERParts は AnSMB.txt (SQLite) に入っている。
        # AnSvEm0001.sld は 140B の INI で SQLite ではないため、まず AnSMB.txt を見て、
        # 見つからなければ内部ファイルを走査して ERParts を持つ SQLite を探す。
        sld_bytes = _find_erparts_blob(files)
        if not sld_bytes:
            res["error"] = "NEO内にERPartsテーブルを持つファイルが見つかりません"
            return res

        # sqlite3 で ERParts SELECT
        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tf_name = tf.name
        try:
            tf.write(sld_bytes)
            tf.close()
            conn = sqlite3.connect(tf_name)
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT PartsNo, PartsUnitPriceOutTax, PartsUnitPriceInTax, "
                "PartsPriceOutTax, PartsPriceInTax, PartsCount FROM ERParts"
            ).fetchall()
            conn.close()
            neo_count = len(rows)
            neo_total = 0
            for r in rows:
                # PartsPriceInTax/OutTax のうち利用可能な値で総額算出 (v11.0: _to_float で安全化)
                try:
                    in_tax = _to_float(r[4])
                    out_tax = _to_float(r[3])
                    # 税抜どうしで比較するため PartsPriceOutTax を優先する
                    val = out_tax if out_tax > 0 else in_tax
                    if val <= 0:
                        # 単価×数量で代替
                        up = _to_float(r[2]) or _to_float(r[1])
                        qty = max(_to_int(r[5], 1), 1)
                        val = up * qty
                    # コグニセブンが「空欄」を表すのは -1 だけ。それ以外の
                    # 負値（マイナスの調整行・値引き行）まで 0 に潰すと、
                    # 総額を減らす方向の異常が検証をすり抜けて
                    # 「PDFと一致」と報告されてしまう。
                    if val == -1:
                        val = 0
                    neo_total += val
                except (TypeError, ValueError):
                    pass
            # name_match_pct (Iter4改良): PartsNo+PartsName両方で総合一致率
            try:
                import difflib
                pdf_keys = []
                for it in (items or []):
                    pno = str(it.get("parts_no") or "").strip()
                    pnm = str(it.get("parts_name") or "").strip()
                    if pno or pnm:
                        pdf_keys.append((pno, pnm))
                # NEO の PartsNo も読み出し
                try:
                    conn2 = sqlite3.connect(tf_name)
                    rows2 = conn2.execute("SELECT PartsNo, PartsName FROM ERParts").fetchall()
                    conn2.close()
                except Exception:
                    rows2 = [(r[0], "") for r in rows]
                neo_pnos = [str(r[0] or "").strip() for r in rows2]
                neo_pnms = [str(r[1] or "").strip() for r in rows2]
                if pdf_keys:
                    matched = 0
                    for pno, pnm in pdf_keys:
                        ok = False
                        if pno and (pno in neo_pnos or
                                    difflib.get_close_matches(pno, [n for n in neo_pnos if n], n=1, cutoff=0.7)):
                            ok = True
                        elif pnm and difflib.get_close_matches(pnm, [n for n in neo_pnms if n], n=1, cutoff=0.6):
                            ok = True
                        if ok:
                            matched += 1
                    res["name_match_pct"] = round(100.0 * matched / len(pdf_keys), 1)
                else:
                    res["name_match_pct"] = 0.0
            except Exception as e:
                logger.debug("name_match calc skip: %s", e)
                res["name_match_pct"] = None
            res["neo_count"] = neo_count
            res["neo_total"] = neo_total
            # 調整行は「読み取れなかった差額」なので、これを含めて数えると
            # 件数も金額も必ず一致してしまい、検証が意味をなさない。
            _adj_rows = [it for it in (items or []) if it.get("is_adjustment_row")]
            if _adj_rows:
                res["has_adjustment_row"] = True
                res["pdf_count"] = max(0, res["pdf_count"] - len(_adj_rows))
            res["count_match"] = (neo_count == res["pdf_count"])
            # 税込は行ごとに税抜を逆算するため数円ずれる。行数ぶんの
            # 許容を持たせないと、正しい .neo でも不一致と判定される。
            _tol = max(1.0, float(len(items or []))) if is_tax_inclusive else 1.0
            res["total_match"] = abs(neo_total - pdf_parts_total) < _tol
            if res.get("has_adjustment_row"):
                # 差額を埋めた結果として一致しているだけなので、
                # 「一致」とは報告しない。
                res["total_match"] = False
            if not res["count_match"]:
                res["mismatches"].append(
                    {"type": "count", "neo": neo_count, "pdf": res["pdf_count"]}
                )
            if not res["total_match"]:
                res["mismatches"].append(
                    {"type": "total", "neo": neo_total, "pdf": pdf_parts_total,
                     "note": "部品(税抜)どうしの比較"}
                )
            # 明細が1行も無いのに「一致」と言ってはいけない。
            # OCRがクォータ超過等で失敗すると 0件 対 0件 で一致してしまい、
            # 空のNEOに緑の「検証OK」が付いてしまう。
            res["ok"] = bool(res["count_match"] and res["total_match"] and neo_count > 0)
            return res
        finally:
            try:
                os.unlink(tf_name)
            except OSError:
                pass
    except Exception as e:
        logger.warning("verify_neo_against_pdf 例外: %s", e)
        res["error"] = str(e)
        return res


# ============================================================
# 8. ディスパッチャ
# ============================================================
def auto_find_addata() -> Optional[str]:
    """v6: ADDATA を OneDrive 含めて自動検索"""
    try:
        from addata_locator import find_addata
        return find_addata()
    except Exception:
        return None


def process_pdf_to_neo(pdf_path,
                       addata_root: str = r"C:\Addata",
                       template_path: Optional[str] = None,
                       vehicle_info: Optional[Dict[str, Any]] = None,
                       items: Optional[List[Dict[str, Any]]] = None,
                       ocr_text: str = "",
                       customer_info: Optional[Dict[str, Any]] = None,
                       skip_ocr: bool = False,
                       mode_override: Optional[str] = None,
                       model_name: Optional[str] = None,
                       api_key: Optional[str] = None,
                       cache_scope: str = "",
                       is_tax_inclusive: bool = False,
                       merge_mode: bool = False,
                       expenses: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """E2E ディスパッチャ。

    - vehicle_info/items 未提供かつ skip_ocr=False かつ GEMINI_API_KEY あり → OCR
    - skip_ocr=True または API キー無し → 既存の挙動維持
    - 戻り値に verify 結果も含める
    """
    log: List[str] = []
    warnings: List[str] = []
    out: Dict[str, Any] = {
        "ok": False,
        "mode": None,
        "is_tax_inclusive": bool(is_tax_inclusive),
        "source": "unknown",
        "source_kind": "unknown",
        "addata": {"found": False, "vehicle_code": None, "confidence": 0.0,
                   "method": "none"},
        "identify": {"found": False, "vehicle_code": None, "confidence": 0.0,
                     "method": "none", "notes": ""},
        "ocr_used": False,
        "neo_bytes": None,
        "vehicle_info": vehicle_info or {},
        "items": items or [],
        "verify": {"ok": False, "count_match": False, "total_match": False,
                   "mismatches": [], "neo_count": 0, "pdf_count": 0},
        "warnings": warnings,
        "fallback": None,
        "log": log,
    }

    # PDF bytes 取得
    pdf_bytes = b""
    try:
        if isinstance(pdf_path, (bytes, bytearray)):
            pdf_bytes = bytes(pdf_path)
        elif isinstance(pdf_path, str) and os.path.exists(pdf_path):
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
    except Exception as e:
        warnings.append(f"PDF読込失敗: {e}")

    # Iter9: パイプライン結果キャッシュ確認
    cache_key = ""
    if pdf_bytes and not vehicle_info and not items and not skip_ocr:
        # テンプレートやモデルが変わったのに前回結果を返さないよう、
        # 結果に影響する引数を全てキーに含める
        # cache_scope には呼び出し側のセッション識別子を渡す。
        # 同じPDFを別の利用者が処理したときに、前の利用者の解析結果や
        # 生成済みNEO（車台番号などを含む）が返るのを防ぐ。
        cache_key = "|".join([
            str(cache_scope),
            _pdf_md5(pdf_bytes),
            str(mode_override), str(addata_root),
            str(template_path), str(model_name),
            # 税区分は出力を変えるのでキーに含める。含めないと、税区分を
            # 選び直して生成し直しても前回の .neo がそのまま返る。
            str(bool(is_tax_inclusive)),
            # マージモードは生成物を変える。キーに入れないと、同じPDFを
            # テンプレート指定あり／なしで通したとき前の結果が返る。
            str(bool(merge_mode)),
            # 費用は生成物を変える。キーに入れないと費用を変えても前の結果が返る。
            repr(sorted((expenses or {}).items())),
            _pdf_md5((ocr_text or "").encode("utf-8", "ignore")),
        ])
        if cache_key in _PIPELINE_CACHE:
            cached = copy.deepcopy(_PIPELINE_CACHE[cache_key])
            cached["from_cache"] = True
            return cached

    # 1) OCR (必要時のみ)
    # APIキーは引数で受け取る。環境変数はCLI実行時のフォールバックに限る。
    # プロセスを全利用者で共有するホスト（Streamlit Community Cloud等）では
    # os.environ に書くと他の利用者のセッションからも読めてしまう。
    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY", "")
    need_vi = vehicle_info is None
    need_items = items is None
    if (need_vi or need_items) and (not skip_ocr) and api_key and pdf_bytes:
        try:
            # Iter11: 車検証OCRと見積書OCRを並列実行（pdf_bytes同一でも別関数なので重複しない）
            from concurrent.futures import ThreadPoolExecutor as _TPE
            _vi_future = None
            _ex = None
            if need_vi:
                # shutdown しないとワーカースレッドが残り続けるため、
                # 結果取得後に必ず片付ける（下の finally）
                _ex = _TPE(max_workers=2)
                try:
                    from app import analyze_vehicle_registration  # type: ignore
                    _vi_future = _ex.submit(analyze_vehicle_registration, api_key, pdf_bytes, "application/pdf")
                except Exception as e:
                    vehicle_info = {}
                    warnings.append(f"vehicle OCR submit失敗: {e}")
            if need_vi and _vi_future is not None:
                try:
                    vi = _vi_future.result()
                    if isinstance(vi, dict) and vi.get('_error'):
                        # 失敗を空の車両情報として扱うと、中身の無いNEOが
                        # 「生成成功」としてキャッシュまでされてしまう
                        vehicle_info = {}
                        out["ocr_incomplete"] = True
                        warnings.append(f"車検証OCR失敗: {vi['_error']}")
                        log.append(f"OCR vehicle_info 失敗: {vi['_error']}")
                    elif isinstance(vi, dict):
                        vehicle_info = vi
                        out["ocr_used"] = True
                        log.append("OCR vehicle_info OK")
                    else:
                        vehicle_info = {}
                        warnings.append("vehicle OCR 戻り値不正")
                except Exception as e:
                    vehicle_info = {}
                    warnings.append(f"vehicle OCR 失敗: {e}")
            if _ex is not None:
                # 結果は受け取り済み。放置するとワーカースレッドが
                # 実行ごとに1本ずつ残り続ける。
                _ex.shutdown(wait=False)
                _ex = None
            if need_items:
                try:
                    from app import analyze_estimate  # type: ignore
                    _ocr_model = model_name or os.environ.get('GEMINI_MODEL', '')
                    res = analyze_estimate(api_key, pdf_bytes, "application/pdf",
                                           model_name=_ocr_model or None,
                                           # 税込表記であることをモデルに伝える
                                           tax_inclusive=bool(is_tax_inclusive))
                    if isinstance(res, dict) and "items" in res:
                        items = res.get("items", [])
                        ocr_meta_first = res
                    elif isinstance(res, list):
                        items = res
                        ocr_meta_first = {"items": res}
                    else:
                        items = []
                        ocr_meta_first = {}
                    out["ocr_used"] = True
                    out["ocr_meta"] = ocr_meta_first
                    log.append(f"OCR estimate OK ({len(items)} items)")

                    # Iter5 (v3): car_name 正規化ヘルパ
                    def _clean_car_name(name: str, model: str = "") -> str:
                        if not name: return ""
                        name = str(name).strip()
                        # メーカー名のみは除去 (model から本当の車名を取る)
                        makers = {"トヨタ", "ホンダ", "日産", "ニッサン", "マツダ", "スバル",
                                  "スズキ", "ダイハツ", "三菱", "ミツビシ", "レクサス",
                                  "TOYOTA", "HONDA", "NISSAN", "MAZDA", "SUBARU",
                                  "SUZUKI", "DAIHATSU", "MITSUBISHI", "LEXUS"}
                        if name.upper() in {m.upper() for m in makers}:
                            # model から取り出し試行
                            if model:
                                # 例: "3DHB RPS13 TYPER 2000" → "TYPER" など
                                tokens = [t for t in str(model).split()
                                          if not t.isdigit() and len(t) >= 2
                                          and t.upper() not in {m.upper() for m in makers}
                                          and not (len(t) == 4 and t[0].isalpha() and t[1:].isdigit())]
                                if tokens:
                                    return tokens[0][:20]
                            return name  # フォールバック
                        # "不明" 含む等は除去
                        if "不明" in name or "C-IIR" in name:
                            return ""
                        return name
                    # Iter1 (v3): _vehicle_info を vehicle_info に統合
                    # analyze_estimate は見積書PDF内の車名・型式・車台番号を _vehicle_info に格納する
                    est_vi = ocr_meta_first.get("_vehicle_info") if isinstance(ocr_meta_first, dict) else None
                    if isinstance(est_vi, dict) and est_vi:
                        if not isinstance(vehicle_info, dict):
                            vehicle_info = {}
                        # キー名マッピング: OCR出力 → generate_neo_file 期待キー
                        _key_map = {
                            "car_name": "car_name",
                            "car_model": "car_model",  # 補助情報
                            "chassis_no": "car_serial_no",  # 車台番号
                            "color_code": "color_code",
                            "color_name": "body_color",
                            "engine_model": "engine_model",
                            "trim_code": "trim_code",
                            "grade": "grade",
                            "grade_code": "grade_code",   # v10.4: ADDATAマッチング補助
                            "body_code": "body_code",     # v10.4: ADDATAマッチング補助
                            "model_year": "model_year",
                            "mileage": "mileage",
                        }
                        merged = 0
                        for src_k, dst_k in _key_map.items():
                            v = est_vi.get(src_k)
                            if v and v != "不明" and not vehicle_info.get(dst_k):
                                vehicle_info[dst_k] = v
                                merged += 1
                        # car_name 正規化 (Iter5)
                        cn = vehicle_info.get("car_name") or ""
                        cm = vehicle_info.get("car_model") or ""
                        cn_clean = _clean_car_name(cn, cm)
                        if cn_clean and cn_clean != cn:
                            vehicle_info["car_name"] = cn_clean
                            log.append(f"car_name 正規化: {cn!r} → {cn_clean!r}")
                        # car_model から型式コード抽出 (例: "3DHB RPS13 TYPER 2000" → "RPS13")
                        if est_vi.get("car_model") and not vehicle_info.get("model_code"):
                            import re as _re
                            cm = str(est_vi.get("car_model"))
                            m = _re.search(r"\b([A-Z]{1,4}\d{1,4}[A-Z]?)\b", cm)
                            if m:
                                vehicle_info["model_code"] = m.group(1)
                                merged += 1
                        # 修理工場名・受付番号も入れておく
                        rs = ocr_meta_first.get("_repair_shop_name")
                        if rs and not vehicle_info.get("repair_shop_name"):
                            vehicle_info["repair_shop_name"] = rs
                            merged += 1
                        # Iter22-23: vehicle_info の追加フィールド
                        for ext_k in ("model_designation", "category_number",
                                      "first_reg_date", "term_date", "car_reg_no"):
                            ev = est_vi.get(ext_k)
                            if ev and ev != "不明":
                                _dst = {"model_designation": "car_model_designation",
                                        "category_number": "car_category_number",
                                        "first_reg_date": "car_reg_date",
                                        "term_date": "term_date",
                                        "car_reg_no": "car_reg_no"}.get(ext_k, ext_k)
                                if not vehicle_info.get(_dst):
                                    vehicle_info[_dst] = ev
                                    merged += 1
                        # Iter22: customer_info を vehicle_info にマージ
                        cust_info = ocr_meta_first.get("customer_info") or {}
                        if isinstance(cust_info, dict):
                            for c_k, c_v in cust_info.items():
                                if c_v and c_v != "不明" and not vehicle_info.get(c_k):
                                    vehicle_info[c_k] = c_v
                                    merged += 1
                        log.append(f"_vehicle_info マージ: +{merged} keys → {list(vehicle_info.keys())}")

                    # Iter13 v3 (Iter14で停止): リトライは Gemini非決定性で精度悪化リスクあり、
                    # かつ Iter6 thinking_budget=0 単独で十分な速度向上が得られたためコメントアウト

                    # 明細の合算と、見積書に印字された総額のずれを検知する。
                    #
                    # 以前は存在しないキー（grand_total / pdf_total / line_total）を
                    # 見ていたため判定値が常に 0 になり、この安全網は一度も
                    # 発火していなかった。実際のキーは pdf_grand_total と
                    # parts_amount / wage。
                    #
                    # 検知後に rasterize=ON で再OCRしていたが、明細抽出は
                    # rasterize の有無で送信内容が変わらないため、結果は必ず
                    # 同一になる。課金と待ち時間だけが増えるので再OCRはやめ、
                    # 利用者に差分を知らせて確認を促す。
                    try:
                        pdf_total = _to_float(ocr_meta_first.get("pdf_grand_total")
                                              or ocr_meta_first.get("grand_total")
                                              or ocr_meta_first.get("pdf_total"))
                        if pdf_total <= 0:
                            # 総合計を読めなかった見積書では、部品計＋工賃計で
                            # 比べる。これをしないと、明細を大半読み落としても
                            # 比率が0になり警告が一切出ない。
                            pdf_total = (_to_float(ocr_meta_first.get("pdf_parts_total"))
                                         + _to_float(ocr_meta_first.get("pdf_wage_total")))
                        items_sum = sum(
                            _to_float(it.get("line_total"))
                            or (_to_float(it.get("parts_amount")) + _to_float(it.get("wage")))
                            for it in items)
                        # 印字された総額は税込のことも税抜のこともあり、
                        # 明細合算とは基準が違う。基準を揃えずに比べると、
                        # 税抜表記の見積では明細が完全に正しくても必ず
                        # 9.1%(=1-1/1.1)ずれて警告が出る。恒常的に出る警告は
                        # 本物の読み落としを埋もれさせるので、近いほうで比べる。
                        _diff_abs = min(abs(items_sum - pdf_total),
                                        abs(items_sum - pdf_total / 1.10))
                        diff_ratio = (_diff_abs / pdf_total if pdf_total > 0 else 0)
                        out["items_total_diff_ratio"] = diff_ratio
                        if diff_ratio > 0.05:
                            log.append(f"⚠ 明細合算と総額の差 {diff_ratio:.1%}")
                            warnings.append(
                                f"読み取った明細の合算（{int(items_sum):,}円）が、"
                                f"見積書に印字された総額（{int(pdf_total):,}円）と"
                                f"{diff_ratio:.1%} ずれています。"
                                "明細の取りこぼしや誤読の可能性があるため、"
                                "生成前にプレビューで内容をご確認ください。")
                    except Exception as e:
                        log.append(f"金額差の判定に失敗: {e}")
                except Exception as e:
                    items = []
                    warnings.append(f"estimate OCR 失敗: {e}")
        except Exception as e:
            warnings.append(f"OCR 全体例外: {e}")
    else:
        if skip_ocr:
            log.append("skip_ocr=True (OCRバイパス)")
        elif not api_key:
            log.append("GEMINI_API_KEY 未設定 (OCRバイパス)")

    vehicle_info = vehicle_info or {}
    items = items or []

    # v7: PDF表示総額と明細合算の差分を「※金額調整」行で吸収 (完全一致保証)
    hdr_parts_total = 0
    hdr_wage_total = 0
    if items and out.get("ocr_meta"):
        try:
            _meta = out["ocr_meta"]
            pdf_p = _to_int(_meta.get("pdf_parts_total"))
            pdf_w = _to_int(_meta.get("pdf_wage_total"))
            pdf_g = _to_int(_meta.get("pdf_grand_total"))
            # v11.0 Phase A-4: header OCR で pdf_wage_total=0 だが items に工賃 > 0 がある場合、
            # items 側の合計を採用 (BUG-3: 工賃計 0 OCR ミス対策)
            try:
                items_wage_sum = sum(_to_int(it.get("wage") or it.get("labor_fee")) for it in items)
                if pdf_w == 0 and items_wage_sum > 1000:
                    log.append(f"[A-4] header工賃計=0 だが items合計={items_wage_sum} → items側採用")
                    pdf_w = items_wage_sum
            except Exception:
                pass
            # 総合計が明細合算と一致しているなら、明細は取りこぼしていない。
            # そのとき部品計・工賃計と合わないのは、小計に含まれない行
            # （レッカー代・諸経費・値引き）が明細にあるからで、行を足す理由にならない。
            # 足すと部品計と工賃計の間で金額が動き、原本と違う小計になる。
            #
            # 総合計の税区分は、まず「明細合算とは独立した証拠」で決める。
            # 明細合算に近いほうを採る方式にすると、明細を総合計の約9%
            # 読み落としたときに、その不足額が消費税額に化けて
            # 「総合計は税込・明細は正しい」と誤判定される。すると
            # 部品計/工賃計の調整・総合計の調整・ずれ警告が同時に外れ、
            # 1行足りない見積が「検証OK・警告なし」で出てしまう。
            # 印字された 部品計＋工賃計−値引 は明細の読み落としに影響されない
            # ので、それが取れているときはそちらで決める。
            # 小計行が無い単列金額形式のときだけ明細合算に頼る。
            _grand_is_intax = not is_tax_inclusive
            _grand_ok = False
            if pdf_g > 0:
                _s0 = _sum_items_outtax(items)
                _tol0 = max(int(round(pdf_g * 0.02)), 1000)
                # 値引きは、ヘッダの値と明細の値引き行の合計の大きいほうを採る。
                # ヘッダ側を読み落としたときに 0 のまま使うと、下の残差判定が
                # 「小計のほうが総合計より大きい」と見て税区分を取り違える。
                # ただし明細の値引き行で補ってよいのは、印字された小計が
                # 値引き「前」（グロス）のときだけ。値引き後の小計を印字する
                # 帳票で二重に引くと _pw_net が値引き額ぶん小さくなり、
                # 値引きが総額の8〜9%のとき _pw_net×1.1 が税抜の総合計に
                # 重なって、税抜を税込と取り違える（総額が9.09%減り、
                # 原本に無い調整行が1本入る）。
                _disc_rows = sum(abs(_to_int(it.get("wage", 0)))
                                 + abs(_to_int(it.get("parts_amount", 0)))
                                 for it in (items or []) if _is_discount_row(it))
                _disc0 = _to_int(_meta.get("discount_amount"))
                if _disc_rows > 0:
                    _pw_raw = pdf_p + pdf_w
                    _sub_tol = max(int(_pw_raw * 0.01), 100)
                    _gross = _sum_items_outtax(items, skip_discount=True)
                    _net = _sum_items_outtax(items)
                    # 小計が値引き前の合算と一致し、値引き後の合算とは
                    # 一致しないときだけ「グロス」と判断する。
                    if (abs(_pw_raw - _gross) <= _sub_tol
                            and abs(_pw_raw - _net) > _sub_tol):
                        _disc0 = max(_disc0, _disc_rows)
                _pw_net = pdf_p + pdf_w - _disc0
                _decided = False
                if _pw_net > 0:
                    _e = max(int(_pw_net * 0.01), 100)
                    if abs(pdf_g - _pw_net) <= _e:
                        _grand_is_intax, _decided = False, True
                    elif abs(pdf_g - int(round(_pw_net * 1.10))) <= _e:
                        _grand_is_intax, _decided = True, True
                # 部品計・工賃計が両方そろっているときだけ使える判定。片方しか
                # 無い形式では、A-4 が欠けた側を明細合算で補うため、証拠が
                # 明細合算に汚染されていて使えない。
                if not _decided and pdf_p > 0 and pdf_w > 0:
                    # 小計と総合計がぴったり合わないのは、レッカー代・諸経費など
                    # 小計に入らない行が総合計にだけ乗っているとき。これらは
                    # 「加算」なので、正しい解釈のほうは残差が 0 以上になる。
                    # 明細合算に頼る前にこれで決める。読み落としは小計に
                    # 影響しないので、読み落としがあっても判定が狂わない。
                    _r_ex = pdf_g - _pw_net
                    _r_in = int(round(pdf_g / 1.10)) - _pw_net
                    # 小計対象外の行は諸経費なので、総額に対して小さいはず。
                    # 残差が大きいときは小計自体が信用できないので採用しない。
                    _r_cap = int(pdf_g * 0.30)
                    _cand = [(abs(_r), _in) for _r, _in in
                             ((_r_ex, False), (_r_in, True))
                             if -_e <= _r <= _r_cap]
                    if _cand:
                        _grand_is_intax = min(_cand)[1]
                        _decided = True
                        log.append(f"[grand_total_match] 小計対象外分で判定: "
                                   f"税抜なら{_r_ex} / 税込なら{_r_in} → "
                                   f"{'税込' if _grand_is_intax else '税抜'}")
                        # この判定は「小計に入らない行が総合計にだけ乗っている」
                        # 前提で書いているが、値引きを読み落とした場合や小計を
                        # 過大に誤読した場合も残差の符号は同じ形になり、
                        # 税込の総合計を税抜と取り違える（総額が約10%増え、
                        # 原本に無い調整行が1本入る）。
                        # 明細合算が反対側の解釈と行ごとの丸め差の範囲で
                        # ぴったり一致し、選んだ側とは一致しないときだけ覆す。
                        # 読み落としのある見積では合算がぴったりにならないので、
                        # 「読み落としが消費税に化ける」防御はそのまま残る。
                        _flip_tol = max(len(items or []) * 2, 100)
                        _d_pick = abs((int(round(pdf_g / 1.10)) if _grand_is_intax
                                       else pdf_g) - _s0)
                        _d_other = abs((pdf_g if _grand_is_intax
                                        else int(round(pdf_g / 1.10))) - _s0)
                        if _d_other <= _flip_tol < _d_pick:
                            _grand_is_intax = not _grand_is_intax
                            log.append(f"[grand_total_match] 明細合算{_s0}が反対の"
                                       f"解釈とぴったり一致するため覆す → "
                                       f"{'税込' if _grand_is_intax else '税抜'}")
                if not _decided:
                    _grand_is_intax = (abs(int(round(pdf_g / 1.10)) - _s0)
                                       < abs(pdf_g - _s0))
                log.append(f"[grand_total_match] 総合計{pdf_g}の税区分: "
                           f"{'税込' if _grand_is_intax else '税抜'}"
                           f"（{'小計から判定' if _decided else '明細合算から判定'}"
                           f" 部品計+工賃計-値引={_pw_net}）")
                # 一致判定は、選んだ1つの解釈だけで行う。両方の解釈のどちらかが
                # 当たれば一致、とすると上記の読み落としを見逃す。
                _target0 = int(round(pdf_g / 1.10)) if _grand_is_intax else pdf_g
                _grand_ok = abs(_target0 - _s0) <= _tol0
                if _grand_ok:
                    log.append(f"[total_match] 総合計{pdf_g}と明細合算{_s0}が一致 → "
                               f"部品計/工賃計との差は小計対象外の行によるものとみなし調整しない")
            if (pdf_p > 0 or pdf_w > 0) and not _grand_ok:
                # v12 iter_006: tolerance を 2% / 1000円 に再拡大
                # （iter_005 でも 2.3% 差で M6=1 残ったため許容差を広げる）
                _tol = max(int(round((pdf_p + pdf_w) * 0.02)), 1000)
                items = _enforce_total_match(items, pdf_p, pdf_w, tolerance=_tol)
                log.append(f"[total_match] parts={pdf_p} wage={pdf_w} tol={_tol} 適用")
            # v7.1: grand_total で最終保証 (parts+wage の調整で足りない場合の差分)
            if pdf_g > 0:
                # v12 iter_006: grand_total も 2% / 1000円
                _tol_g = max(int(round(pdf_g * 0.02)), 1000)
                # この引数は「PDFの総額を明細の基準に換算するか」を意味する。
                # 明細が税抜なら総額(税込)を1.1で割って合わせる。
                # 明細が税込なら総額と同じ基準なので換算しない。
                # 税区分は上（_enforce_total_match の手前）で決めた _grand_is_intax
                # を使う。ここで明細合算から決め直すと、読み落としが消費税に
                # 化けて調整行の目標値が読み落とし後の合算そのものになる。
                _sum_now = _sum_items_outtax(items)
                # 小計から決められなかったときの明細合算による判定は、
                # _enforce_total_match が部品計・工賃計の不足を埋めた「後」の
                # 合算でやり直す。埋める前の合算で決めると、読み落としを含んだ
                # 数字で税区分が「税込」に反転し、直前に埋めた不足額を
                # ここで削り直してしまう（総額が9.09%減る）。
                if not _decided:
                    _grand_is_intax = (abs(int(round(pdf_g / 1.10)) - _sum_now)
                                       < abs(pdf_g - _sum_now))
                    log.append(f"[grand_total_match] 小計調整後の合算{_sum_now}で"
                               f"税区分を再判定: {'税込' if _grand_is_intax else '税抜'}")
                _target_now = (int(round(pdf_g / 1.10)) if _grand_is_intax else pdf_g)
                _d_now = abs(_target_now - _sum_now)
                # 許容差に収まらない差は、税区分の問題ではなく本当の読み落としか
                # 誤読。ここで行を捏造すると、原本に無い行が入った見積を
                # 「合計は合っている」という理由で出してしまう。協定見積は行と
                # 金額が原本と一致していることが条件なので、差が大きいときは
                # 調整行を作らず警告だけにする。
                if _d_now > max(_tol_g * 5, int(pdf_g * 0.10)):
                    log.append(f"[grand_total_match] 差が大きすぎるため調整行は作らない "
                               f"(差={_d_now})")
                    warnings.append(
                        f"見積書に印字された総額（{int(pdf_g):,}円）と、読み取った明細の"
                        f"合算（{int(_sum_now):,}円）の差が大きすぎます。"
                        "明細の読み落としが疑われるため、差額を埋める行は追加していません。"
                        "生成前にプレビューで原本と1行ずつ突き合わせてください。")
                else:
                    items = _enforce_grand_total_match(
                        items, pdf_g,
                        is_tax_inclusive=_grand_is_intax,
                        tolerance=_tol_g)
                    log.append(f"[grand_total_match] grand={pdf_g} tol={_tol_g} 適用")
            # v11.0 Phase A-4 v2: pdf_grand_total すら 0 のとき、items 合計を grand とみなして調整
            elif pdf_g == 0 and items:
                try:
                    items_total = sum(
                        _to_int(it.get("parts_amount") or it.get("amount") or it.get("part_price"))
                        + _to_int(it.get("wage") or it.get("labor_fee"))
                        for it in items
                    )
                    if items_total > 0:
                        log.append(f"[A-4] header総額未取得 → items合計={items_total} を grand_total として登録")
                        # _meta は analyze_estimate の戻り値そのもので、
                        # そのキャッシュはプロセス全体で共有されている。ここに
                        # 書き戻すと「調整後の合算」が次回の入力になり、同じPDFから
                        # 内訳の違う .neo が出る（2回目は調整処理が丸ごと飛ぶ）。
                        # 自分のコピーにだけ書く。
                        if isinstance(_meta, dict):
                            _meta = dict(_meta)
                            _meta["pdf_grand_total"] = items_total
                            out["ocr_meta"] = _meta
                except Exception:
                    pass
            hdr_parts_total = pdf_p
            hdr_wage_total = pdf_w
            # 調整行を作ったこと自体を必ず伝える。差額が大きいほど
            # 明細の読み落としが疑われるのに、以前は警告も上限も無く、
            # 総額だけ合った .neo が「検証OK」の表示で出荷されていた。
            try:
                _adj = next((it for it in items if it.get("is_adjustment_row")), None)
                if _adj:
                    # 部品と工賃を足した純額で知らせると、部品不足と工賃過剰が
                    # 打ち消し合ったときに「+0円のずれ」と表示しながら、
                    # 実際には ±1万円の行が1本入る。利用者は実害なしと読んで
                    # 突き合わせを省いてしまう。部品と工賃を別々に出す。
                    _adj_p = _to_int(_adj.get("parts_amount"))
                    _adj_w = _to_int(_adj.get("wage"))
                    _amt = _adj_p + _adj_w
                    _base = (pdf_p + pdf_w) or pdf_g or 1
                    # 比率も純額ではなく、大きいほうの絶対値で見る。
                    # 純額だと打ち消し合ったときに ocr_incomplete の判定も外れる。
                    _pct = max(abs(_adj_p), abs(_adj_w)) / _base if _base else 0
                    out["adjustment_amount"] = _amt
                    out["adjustment_parts"] = _adj_p
                    out["adjustment_wage"] = _adj_w
                    out["adjustment_ratio"] = _pct
                    warnings.append(
                        f"見積書の合計に合わせるため、原本に無い「※金額調整」の行を"
                        f"1本追加しました（部品 {_adj_p:+,}円 / 工賃 {_adj_w:+,}円"
                        f"、合計の {_pct:.1%}）。"
                        "明細の読み落としや誤読の可能性が高いので、"
                        "生成前にプレビューで原本と1行ずつ突き合わせてください。")
                    if _pct > 0.05:
                        # 読み落としが大きい結果はキャッシュに残さない
                        out["ocr_incomplete"] = True
            except Exception:
                pass
            # ADDATAの指数と見積の工数が食い違う行は、指数を上書きせずに
            # 見積の値を残している。黙って通すと利用者が食い違いに
            # 気づけないので知らせる。
            try:
                _im = [it for it in (items or []) if it.get("index_mismatch")]
                if _im:
                    _ex = _im[0]
                    _p, _d = _ex["index_mismatch"]
                    warnings.append(
                        f"ADDATAの指数と見積の工数が食い違う行が{len(_im)}件あります"
                        f"（例:「{_ex.get('name') or _ex.get('parts_name') or ''}」"
                        f" 見積 {_p} / ADDATA {_d}）。"
                        "見積の工数をそのまま採用しています。")
            except Exception:
                pass
            # 許容差(2%または1000円)の範囲内は調整行を作らないため、
            # 差が残ったまま出荷されうる。黙って通さず警告に残す。
            try:
                # pdf_p / pdf_w は値引き前の小計なので、値引き行・調整行を
                # 含めて比べると値引き額がそのまま「残差」として警告に出る。
                # 説明のつく差で毎回警告を出すと、本物の読み落としが埋もれる。
                _resid_src = [it for it in items
                              if not (it.get("is_adjustment_row") or _is_discount_row(it))]
                _sum_p = sum(_to_int(it.get("parts_amount") or it.get("part_price"))
                             for it in _resid_src)
                _sum_w = sum(_to_int(it.get("wage") or it.get("labor_fee"))
                             for it in _resid_src)
                _resid_p = pdf_p - _sum_p if pdf_p > 0 else 0
                _resid_w = pdf_w - _sum_w if pdf_w > 0 else 0
                out["total_residual"] = {"parts": _resid_p, "wage": _resid_w}
                # 残差の向きで意味が変わる。
                #   負（明細のほうが多い）… レッカー代・諸経費など小計に
                #     含まれない行が明細にあるだけで、説明がつく。総合計が
                #     合っているならこれは正常なので警告しない。毎回出すと
                #     本物の読み落としの警告が埋もれる。
                #   正（明細のほうが少ない）… 行が足りない。許容差内で調整行が
                #     作られなかった場合、これが唯一の手がかりになるので、
                #     総合計が合っていても必ず知らせる。
                # 行ごとの丸めで印字小計が明細合算より数円大きくなるのは普通に
                # 起きる。下限を置かないと、1行も落としていない見積で毎回
                # 警告が出て、本物の読み落としの警告が埋もれる。
                # 丸め差は行あたり高々1円。総額の0.1%を下限にすると
                # 60万円の見積で600円のクリップ1行が無警告で消え、
                # 行数を下限にすると250行の見積で210円の1行が消える。
                # 控えめなほうを採る。
                _resid_floor = max(min(len(items or []),
                                       int((pdf_p + pdf_w) * 0.001)), 100)
                _resid_shortfall = (_resid_p > _resid_floor or _resid_w > _resid_floor)
                if (_resid_p or _resid_w) and (_resid_shortfall or not _grand_ok):
                    warnings.append(
                        f"見積書の合計と明細の合計に差が残っています"
                        f"（部品 {_resid_p:+,}円 / 工賃 {_resid_w:+,}円）。明細を確認してください。"
                    )
            except Exception:
                pass
        except Exception as e:
            log.append(f"total_match 失敗: {e}")

    out["vehicle_info"] = vehicle_info
    out["items"] = items

    # 2) ソース判定 (Iter3: OCR完了後は raw_text 構築して再判定)
    try:
        ocr_combined = ocr_text or ""
        if items:
            try:
                parts = []
                for it in items[:200]:
                    for k in ("parts_name", "parts_no", "category", "remark"):
                        v = it.get(k)
                        if v:
                            parts.append(str(v))
                ocr_combined = (ocr_combined + "\n" + " ".join(parts)).strip()
            except Exception:
                pass
        if vehicle_info:
            try:
                ocr_combined += "\n" + " ".join(str(v) for v in vehicle_info.values() if v)
            except Exception:
                pass
        src = classify_pdf_source(pdf_path if isinstance(pdf_path, str) else pdf_bytes, ocr_combined)
        out["source"] = src
        out["source_kind"] = src
        log.append(f"classify_pdf_source={src} (ocr_len={len(ocr_combined)})")
    except Exception as e:
        warnings.append(f"classify失敗: {e}")
        out["source"] = "unknown"
        out["source_kind"] = "unknown"

    # 3) 車種特定 (v5-Iter2: identify 失敗 → 部品番号逆引き)
    ident = {"found": False, "vehicle_code": None, "confidence": 0.0, "method": "none"}
    try:
        ident = identify_vehicle_in_addata(vehicle_info, addata_root)
        if not ident.get("found") and items and addata_root and os.path.isdir(addata_root):
            try:
                from auto_matching import reverse_lookup_vehicle  # type: ignore
                rev = reverse_lookup_vehicle(items, addata_root, min_hits=2)
                if rev.get("found"):
                    ident["found"] = True
                    ident["vehicle_code"] = rev["vehicle_code"]
                    ident["confidence"] = rev["confidence"]
                    ident["method"] = "reverse_pno"
                    ident["notes"] = f"hits={rev['hits']}/{rev['total']}"
                    if isinstance(vehicle_info, dict) and not vehicle_info.get("model_code"):
                        vehicle_info["model_code"] = rev["vehicle_code"]
                    log.append(f"[reverse_pno] vcode={rev['vehicle_code']} conf={rev['confidence']}")
            except Exception as e:
                log.append(f"reverse_pno失敗: {e}")
        out["identify"] = ident
        out["addata"] = {
            "found": ident.get("found", False),
            "vehicle_code": ident.get("vehicle_code"),
            "confidence": ident.get("confidence", 0.0),
            "method": ident.get("method", "none"),
        }
        log.append(f"identify found={ident.get('found')} method={ident.get('method')}")

        # v12 Phase A-5: layer 1/2 の vehicle_code を vehicle_info["model_code"] に伝搬
        # （_full_addata_match は vehicle_info["model_code"] からフォルダを引くため）
        ident_layer = ident.get("match_layer")
        ident_vc = ident.get("vehicle_code")
        if ident_layer in (1, 2) and ident_vc and isinstance(vehicle_info, dict):
            old_mc = vehicle_info.get("model_code") or ""
            if old_mc != ident_vc:
                log.append(f"[Phase A-5] model_code 伝搬: {old_mc!r} → {ident_vc!r} (layer={ident_layer})")
                vehicle_info["model_code"] = ident_vc

        # v13 Step C: ADDATA folder 構造から MakerCode/CarCode を抽出して vehicle_info に流す
        # (path 分解で確実に取れる Audatex 内部コード Mapping → NEO Car テーブル UPDATE 用)
        # v12 修正: Layer 3 (TOYOTA_GENERIC) フォールバック時は伝搬しない。
        # 合成コード "TOYOTA_GENERIC" が NEO 固定長フィールドで "TOY" に切り詰められ、
        # コグニ7 が「該当の車種が収録されておりません [車種コード: TOY]」と拒否するため。
        _is_template_fallback = (ident_layer == 3) or (ident.get("is_template") is True) \
                                 or (str(ident_vc or '').upper().startswith("TOYOTA_GENERIC"))
        if ident_vc and isinstance(vehicle_info, dict) and not _is_template_fallback:
            vc = str(ident_vc).strip()
            if len(vc) >= 1:
                vehicle_info.setdefault("maker_code", vc[0])
            if len(vc) >= 2:
                vehicle_info.setdefault("car_code", vc)
            log.append(f"[Step C] MakerCode={vehicle_info.get('maker_code')} CarCode={vehicle_info.get('car_code')}")
        elif _is_template_fallback:
            log.append(f"[Step C skip] Layer3 template fallback ({ident_vc!r}) → maker_code/car_code は template の値を維持")

        # v12 Phase B/C 連動: NEO 生成前に _full_addata_match を呼んで items に
        # match_level / db_parts_no / addata_matched を付与する（メトリクス & CSV 用）。
        # build_neo_mode_b/c でも同じ関数が呼ばれるが冪等なので問題なし。
        if items and addata_root and ident_layer in (1, 2):
            try:
                from auto_matching import _full_addata_match as _fam  # type: ignore
                items = _fam(items, vehicle_info or {}, addata_root)
                out["items"] = items
                _matched_cnt = sum(1 for it in items if it.get('addata_matched'))
                _db_pno_cnt = sum(1 for it in items if str(it.get('db_parts_no') or '').strip())
                log.append(f"[Phase B/C 早期マッチ] addata_matched={_matched_cnt}/{len(items)} "
                           f"db_pno={_db_pno_cnt}")
            except Exception as e:
                log.append(f"[Phase B/C 早期マッチ] 失敗: {e}")
        # Iter15: グレード特定（任意・収録ありの時のみ）
        if ident.get("found") and ident.get("vehicle_code"):
            try:
                grade = identify_grade_from_items(
                    ident["vehicle_code"], items, addata_root,
                    body_code=int(vehicle_info.get("body_code") or 0)
                )
                out["grade"] = grade
                if grade.get("grade_code"):
                    if not vehicle_info.get("grade_code"):
                        vehicle_info["grade_code"] = grade["grade_code"]
                    log.append(f"grade={grade.get('grade_code')} conf={grade.get('confidence'):.2f}")
            except Exception as e:
                log.append(f"grade特定スキップ: {e}")
    except Exception as e:
        warnings.append(f"identify失敗: {e}")
        ident = {"found": False, "vehicle_code": None, "confidence": 0.0,
                 "method": "none", "notes": str(e)}
        out["identify"] = ident

    # 4) モード判定
    if mode_override in ("A", "B", "C"):
        mode = mode_override
        log.append(f"mode_override={mode}")
    else:
        mode = decide_mode_from_identify(ident, out["source"])
    out["mode"] = mode
    log.append(f"mode={mode}")

    # ページ境界の二重読み取りの統合は、これまで NEO 生成の直前
    # （_call_generate_neo の中）で黙って行われていた。画面のプレビューは
    # 統合前の行数を出すので、画面の行数と .neo の行数が食い違ったまま
    # 利用者に何も伝わらない。ここで先に済ませて、統合したことを知らせる。
    _before = len(items or [])
    items = _final_dedup_items(items or [])
    _merged = _before - len(items)
    if _merged > 0:
        out["items"] = items
        out["dedup_merged"] = _merged
        warnings.append(
            f"ページの境目で二重に読み取られた明細を{_merged}行統合しました"
            f"（{_before}行 → {len(items)}行）。同じ部品が原本にも複数行ある場合は"
            "統合していませんが、生成前にプレビューで行数をご確認ください。")

    # 5) NEO生成
    if not (vehicle_info or items):
        log.append("vehicle_info/items 共に空のため NEO生成スキップ")
        out["ok"] = True
        return out

    try:
        if mode == "A":
            neo = build_neo_mode_a(items, vehicle_info, template_path, customer_info,
                                   is_tax_inclusive=is_tax_inclusive,
                                   merge_mode=merge_mode,
                              expenses=expenses)
        elif mode == "B":
            neo = build_neo_mode_b(items, vehicle_info, template_path, addata_root, customer_info,
                                   is_tax_inclusive=is_tax_inclusive,
                                   merge_mode=merge_mode,
                              expenses=expenses)
        else:
            neo = build_neo_mode_c(items, vehicle_info, template_path, addata_root, customer_info,
                                   is_tax_inclusive=is_tax_inclusive,
                                   merge_mode=merge_mode,
                              expenses=expenses)
        out["neo_bytes"] = neo
        log.append(f"NEO生成成功 size={len(neo) if neo else 0}")
        # verify
        try:
            v = verify_neo_against_pdf(neo, items,
                                       pdf_parts_total=hdr_parts_total or None,
                                       pdf_wage_total=hdr_wage_total or None,
                                       is_tax_inclusive=is_tax_inclusive)
            out["verify"] = v
            log.append(f"verify ok={v.get('ok')} count={v.get('count_match')} total={v.get('total_match')}")
        except Exception as e:
            warnings.append(f"verify失敗: {e}")
        out["ok"] = True
    except Exception as e:
        warnings.append(f"NEO生成失敗: {e}")
        out["neo_bytes"] = None
        out["ok"] = False

    # Iter9: 成功結果をキャッシュ
    # OCRが途中で失敗した結果をキャッシュすると、クォータ回復後に
    # 同じPDFを処理しても中身の欠けたNEOが返り続ける。
    if cache_key and out.get("ok") and out.get("neo_bytes") and not out.get("ocr_incomplete"):
        if len(_PIPELINE_CACHE) >= _PIPELINE_CACHE_MAX:
            _PIPELINE_CACHE.pop(next(iter(_PIPELINE_CACHE)))
        _PIPELINE_CACHE[cache_key] = copy.deepcopy(out)

    # v10.4: items を CSV bytes 化（NEO 生成に成功している場合のみ。失敗時は付けない）
    # v13+: SECTION:VERIFY / SECTION:SUMMARY を OCR メタから抽出して同梱
    try:
        if out.get("ok") and out.get("items"):
            _ocr_m = out.get("ocr_meta") or {}
            _verify_summary: Dict[str, Any] = {}
            if isinstance(_ocr_m, dict):
                for _src_k, _dst_k in (
                    ("grand_total", "total_incl_tax"),
                    ("pdf_total", "total_incl_tax"),
                    ("subtotal_excl_tax", "total_excl_tax"),
                    ("tax_amount", "tax"),
                    ("paint_subtotal", "subtotal_paint"),
                    ("parts_subtotal", "subtotal_parts"),
                    ("labor_subtotal", "subtotal_labor"),
                ):
                    _v = _ocr_m.get(_src_k)
                    if _v not in (None, "", 0) and _dst_k not in _verify_summary:
                        _verify_summary[_dst_k] = _v
            _verify_rows = _ocr_m.get("verify_rows") if isinstance(_ocr_m, dict) else None
            out["csv_bytes"] = items_to_csv_bytes(
                out.get("items") or [],
                out.get("vehicle_info") or {},
                (out.get("vehicle_info") or {}).get("customer_info")
                if isinstance((out.get("vehicle_info") or {}).get("customer_info"), dict)
                else None,
                verify_rows=_verify_rows if isinstance(_verify_rows, list) else None,
                verify_summary=_verify_summary or None,
            )
    except Exception as _csv_e:
        logger.debug("items_to_csv_bytes failed: %s", _csv_e)

    return out

# 後方互換 alias
run_pipeline = process_pdf_to_neo


# v10.4: CSV 中間表現エクスポート
def items_to_csv_bytes(items: List[Dict[str, Any]],
                       vehicle_info: Optional[Dict[str, Any]] = None,
                       customer_info: Optional[Dict[str, Any]] = None,
                       verify_rows: Optional[List[Dict[str, Any]]] = None,
                       verify_summary: Optional[Dict[str, Any]] = None) -> bytes:
    """items list を CSV bytes（UTF-8 BOM 付き、Excel 対応）にエクスポート。

    v13+ 拡張で 4 セクション対応:
      - SECTION:VEHICLE  (車検証 3 キー含む)
      - SECTION:DETAIL   (明細行)
      - SECTION:VERIFY   (小計・塗装計など検証行)
      - SECTION:SUMMARY  (PDF 総合計・税抜・税額)

    列構成（NEO 生成前の可視チェック用）:
      行 / 部品名 / 部品番号(OCR) / DB部品番号 / 数量 / 単価 / DB単価 /
      部品計 / 工賃 / 作業区分 / 指数 / マッチレベル / マーカー / 備考
    """
    import io
    import csv
    buf = io.StringIO()
    buf.write('﻿')  # Excel が UTF-8 を判定する BOM
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    if vehicle_info:
        writer.writerow(["# === SECTION:VEHICLE ==="])
        writer.writerow(["# 車名", vehicle_info.get("car_name", "")])
        writer.writerow(["# 車台番号", vehicle_info.get("car_serial_no", "")])
        writer.writerow(["# 型式", vehicle_info.get("car_model", "")])
        writer.writerow(["# 色", str(vehicle_info.get("color_code", "")) + " " + str(vehicle_info.get("body_color", ""))])
        writer.writerow(["# グレード", vehicle_info.get("grade", "") or vehicle_info.get("grade_code", "")])
        writer.writerow(["# ボディコード", str(vehicle_info.get("body_code", 0))])
        # v13+: 車種特定 Layer 1 用 3 キー（OCR 抽出済値があれば書く）
        writer.writerow(["# メーカー", vehicle_info.get("maker") or vehicle_info.get("car_maker", "")])
        writer.writerow(["# 型式指定番号", vehicle_info.get("car_model_designation") or vehicle_info.get("model_designation", "")])
        writer.writerow(["# 類別区分番号", vehicle_info.get("car_category_number") or vehicle_info.get("category_number", "")])
        writer.writerow(["# 初度登録", vehicle_info.get("car_reg_date") or vehicle_info.get("first_reg_date", "")])
    if customer_info:
        writer.writerow(["# 顧客名", customer_info.get("customer_name", "")])
        writer.writerow(["# 受付番号", customer_info.get("receipt_no", "")])
        writer.writerow(["# 修理工場", customer_info.get("repair_shop_name", "")])
    writer.writerow([])
    writer.writerow(["# === SECTION:DETAIL ==="])
    writer.writerow([
        "行", "部品名", "部品番号(OCR)", "DB部品番号", "数量",
        "単価", "DB単価", "部品計", "工賃", "作業区分",
        "指数", "マッチ", "マーカー付", "備考",
    ])
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        writer.writerow([
            i,
            it.get("parts_name") or it.get("name", "") or it.get("work_or_part_name", ""),
            it.get("parts_no") or it.get("part_no", "") or it.get("part_number", ""),
            it.get("db_parts_no", ""),
            it.get("quantity", 1),
            it.get("unit_price") or it.get("part_price", 0),
            it.get("db_price") or "",
            it.get("parts_amount") or it.get("amount", 0),
            it.get("wage") or it.get("labor_fee", 0),
            it.get("category") or it.get("work_code", ""),
            it.get("index_value", 0),
            it.get("match_level", ""),
            it.get("parts_no_marked", "") or it.get("pno_marker", ""),
            it.get("match_note", "") or ("※金額調整" if it.get("is_adjustment_row") else ""),
        ])
    # v13+: SECTION:VERIFY (検証行: 小計/塗装計/部品計など)
    if verify_rows:
        writer.writerow([])
        writer.writerow(["# === SECTION:VERIFY ==="])
        writer.writerow(["ラベル", "金額"])
        for vr in verify_rows:
            if not isinstance(vr, dict):
                continue
            writer.writerow([vr.get("label", ""), vr.get("amount", 0)])
    # v13+: SECTION:SUMMARY (PDF 総合計検証用)
    if verify_summary:
        writer.writerow([])
        writer.writerow(["# === SECTION:SUMMARY ==="])
        writer.writerow(["key", "value"])
        for k in ("total_excl_tax", "tax", "total_incl_tax",
                  "subtotal_parts", "subtotal_paint", "subtotal_labor",
                  "extra_charges"):
            if k in verify_summary:
                writer.writerow([k, verify_summary.get(k, "")])
    return buf.getvalue().encode("utf-8")


if __name__ == "__main__":
    import sys
    print("pdf_to_neo_pipeline v2-Iter2")
    if len(sys.argv) > 1:
        r = process_pdf_to_neo(sys.argv[1], skip_ocr=True)
        print({k: v for k, v in r.items() if k != "neo_bytes"})
