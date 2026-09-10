#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ADDATA フォルダ自動検索 (各PCのOneDrive内対応)

検索戦略:
  1. 環境変数 ADDATA_ROOT
  2. C:\\Addata (従来の標準位置)
  3. OneDriveパス候補配下を再帰検索 (深さ4まで)
     - %USERPROFILE%\\OneDrive*
     - C:\\Users\\<user>\\OneDrive - <org>
     - D:\\OneDrive*
  4. レジストリ HKCU\\Software\\Microsoft\\OneDrive
  5. ADDATA フォルダの判定: 子フォルダに 'A'-'Z' 1文字フォルダがあり、
     さらにその下に *01.DB / *11.DB / *12.DB が存在する。
"""
from __future__ import annotations
import os
import sys
import re
from typing import List, Optional

CACHE_KEY = "_ADDATA_LOCATOR_CACHE"
ALL_CACHE_KEY = "_ADDATA_LOCATOR_ALL_CACHE"
_cache: dict = {}

# v6.1: 標準位置リスト (優先度順)
_STANDARD_PATHS = (
    r"C:\Addata",
    r"D:\Addata",
    r"C:\AdSeven\Addata",                              # コグニセブン同梱 (最優先)
    r"D:\AdSeven\Addata",
    r"E:\AdSeven\Addata",
    r"C:\Program Files (x86)\Audatex\Auda7\Addata",   # インストーラ版
    r"C:\Program Files\Audatex\Auda7\Addata",
    r"C:\cogni車種データ\Addata",
    r"D:\cogni車種データ\Addata",
    r"E:\cogni車種データ\Addata",
    # Linux / コンテナ配置（Streamlit Cloud・Docker・Cloud Run）。
    # Windows パスしか見ていないと、本番では必ず未検出になり
    # 常にモードA（ベタ打ち）へ落ちてしまう。
    "/mnt/addata",
    "/mnt/Addata",
    "/data/addata",
    "/data/Addata",
    "/opt/addata",
    "/opt/Addata",
    "/app/Addata",
    os.path.join(os.path.expanduser("~"), "Addata"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "Addata"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "addata"),
)

# v10.4: OneDrive ルート配下の業務典型サブパス（再帰検索より高速で確実な O(1) パス確認）
# 各 OneDrive ルートに対してこれらを直積展開して _is_valid_addata で検証する。
_ONEDRIVE_SUBPATHS = (
    r"【※写真※】 2025\コグニ、アセス　データベース\Addata",
    r"【※写真※】2025\コグニ、アセス　データベース\Addata",   # スペース無し版
    r"【※写真※】 2025\コグニ、アセス データベース\Addata",   # 半角スペース版
    r"コグニ、アセス　データベース\Addata",
    r"コグニ、アセス データベース\Addata",
    r"コグニアセスデータベース\Addata",
    r"Addata",                                                  # OneDrive 直下版
    r"AdSeven\Addata",
)


def _is_valid_addata(path: str, max_check: int = 3) -> bool:
    """path が ADDATA ルートとして妥当か検証（軽量チェック）"""
    if not path or not os.path.isdir(path):
        return False
    try:
        # A-Z 1文字フォルダがあるか（最大3つチェックで早期終了）
        letter_dirs = []
        for entry in os.listdir(path):
            if len(entry) == 1 and entry.isalpha() and os.path.isdir(os.path.join(path, entry)):
                letter_dirs.append(entry)
                # max_check 個そろうまで集める。1個で打ち切ると、最初に
                # 見つかった文字フォルダが空だっただけで有効なAddataを
                # 「無効」と判定してしまう。
                if len(letter_dirs) >= max_check:
                    break
        if not letter_dirs:
            return False
        # 配下に vehicle_code フォルダ があり、その中に *.DB があるか
        for ld in letter_dirs[:max_check]:
            ld_path = os.path.join(path, ld)
            try:
                for sub in os.listdir(ld_path):
                    sub_path = os.path.join(ld_path, sub)
                    if os.path.isdir(sub_path):
                        try:
                            files = os.listdir(sub_path)
                            if any(f.upper().endswith('.DB') for f in files):
                                return True
                        except OSError:
                            continue
            except OSError:
                continue
        return False
    except OSError:
        return False


def _candidate_onedrive_roots() -> List[str]:
    """OneDriveルート候補を列挙"""
    roots: List[str] = []

    # 1. 環境変数
    for env_key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        v = os.environ.get(env_key)
        if v and os.path.isdir(v):
            roots.append(v)

    # 2. ユーザーホーム配下の OneDrive*
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if home and os.path.isdir(home):
        try:
            for entry in os.listdir(home):
                if entry.startswith("OneDrive"):
                    p = os.path.join(home, entry)
                    if os.path.isdir(p):
                        roots.append(p)
        except OSError:
            pass

    # 3. ドライブ直下の OneDrive*
    for drive in ("C:", "D:", "E:"):
        try:
            if not os.path.isdir(drive + "\\"):
                continue
            for entry in os.listdir(drive + "\\"):
                if entry.startswith("OneDrive"):
                    p = os.path.join(drive + "\\", entry)
                    if os.path.isdir(p):
                        roots.append(p)
        except OSError:
            continue

    # 重複除去・存在確認
    seen = set()
    out = []
    for r in roots:
        rn = os.path.normpath(r)
        if rn not in seen and os.path.isdir(rn):
            seen.add(rn)
            out.append(rn)
    return out


def _walk_for_addata(root: str, max_depth: int = 5,
                    max_dirs: int = 5000,
                    stats: Optional[dict] = None) -> Optional[str]:
    """root から再帰検索して 'Addata' フォルダかつ妥当判定で見つける。
    stats に dict を渡すと {visited:N, hit_path:str|None} が書き戻される。"""
    if not os.path.isdir(root):
        return None
    visited = 0
    # v10.4: 業務名ヒント。コグニ/アセス/データベース を含むフォルダを優先深掘り
    _HINT_KEYWORDS = ("コグニ", "アセス", "データベース", "AdSeven", "Adseven", "adseven")
    for cur, dirs, _files in os.walk(root):
        visited += 1
        if visited > max_dirs:
            if stats is not None:
                stats["visited"] = visited
                stats["hit_path"] = None
                stats["truncated"] = True
            return None
        # 深さ判定
        rel = os.path.relpath(cur, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirs[:] = []
            continue
        # 大文字小文字を問わず "Addata" マッチ
        for d in dirs:
            if d.lower() == "addata":
                cand = os.path.join(cur, d)
                if _is_valid_addata(cand):
                    if stats is not None:
                        stats["visited"] = visited
                        stats["hit_path"] = cand
                    return cand
        # v10.4: 業務名ヒント検索（直近サブフォルダに「コグニ」「アセス」等を含む場合は
        # その配下を即座に深掘りして Addata を探す。子→孫程度の浅い階層想定で計算量小）
        for d in dirs:
            if any(kw in d for kw in _HINT_KEYWORDS):
                hint_root = os.path.join(cur, d)
                try:
                    for h_cur, h_dirs, _ in os.walk(hint_root):
                        # ヒント配下は最大3階層まで
                        rel_h = os.path.relpath(h_cur, hint_root)
                        hd = 0 if rel_h == "." else rel_h.count(os.sep) + 1
                        if hd > 3:
                            h_dirs[:] = []
                            continue
                        for hd_name in h_dirs:
                            if hd_name.lower() == "addata":
                                cand = os.path.join(h_cur, hd_name)
                                if _is_valid_addata(cand):
                                    if stats is not None:
                                        stats["visited"] = visited
                                        stats["hit_path"] = cand
                                        stats["via_hint"] = d
                                    return cand
                except OSError:
                    continue
        # 不要な深掘り抑制（OneDrive にはユーザーフォルダが多い）
        # node_modules / .git / __pycache__ / cache / temp 等を除外
        dirs[:] = [d for d in dirs
                   if not d.startswith(".")
                   and d.lower() not in ("node_modules", "__pycache__",
                                          "cache", "temp", "tmp", "logs",
                                          "videos", "music", "ピクチャ",
                                          "pictures")]
    if stats is not None:
        stats["visited"] = visited
        stats["hit_path"] = None
    return None


def find_addata(force_refresh: bool = False) -> Optional[str]:
    """ADDATA ルートを自動検索して返す。見つからなければ None。
    結果はモジュールキャッシュ。force_refresh=True で再検索。"""
    if not force_refresh and CACHE_KEY in _cache:
        return _cache[CACHE_KEY]

    # 1. 環境変数
    env_root = os.environ.get("ADDATA_ROOT")
    if env_root and _is_valid_addata(env_root):
        _cache[CACHE_KEY] = env_root
        return env_root

    # 2. 標準位置 (v6.1: AdSeven含め拡張)
    for std in _STANDARD_PATHS:
        if _is_valid_addata(std):
            _cache[CACHE_KEY] = std
            return std

    # 2b. OneDrive ルート × 業務典型サブパス の直積展開を O(1) 確認 (v10.4)
    # 再帰検索より速く、ユーザー指定パス「【※写真※】 2025\コグニ、アセス　データベース\Addata」を確実に当てる
    for od in _candidate_onedrive_roots():
        for sub in _ONEDRIVE_SUBPATHS:
            cand = os.path.join(od, sub)
            if _is_valid_addata(cand):
                _cache[CACHE_KEY] = cand
                return cand

    # 3. OneDrive配下を並列検索（複数候補を同時走査で最大3倍速）
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        roots = _candidate_onedrive_roots()
        if roots:
            with ThreadPoolExecutor(max_workers=min(len(roots), 4)) as ex:
                futures = {ex.submit(_walk_for_addata, r, 5, 30000): r for r in roots}
                for fu in as_completed(futures):
                    try:
                        found = fu.result()
                    except Exception:
                        found = None
                    if found:
                        # 残りのfutureはcancel
                        for f in futures:
                            if f is not fu:
                                f.cancel()
                        _cache[CACHE_KEY] = found
                        return found
    except Exception:
        # フォールバック: 直列
        for od in _candidate_onedrive_roots():
            found = _walk_for_addata(od, max_depth=5, max_dirs=30000)
            if found:
                _cache[CACHE_KEY] = found
                return found

    _cache[CACHE_KEY] = None
    return None


def find_all_addata(force_refresh: bool = False) -> List[str]:
    """全ADDATA候補を返す (優先度順、重複除去済)。
    UI でユーザーに選択肢を提示するための補助API。
    """
    if not force_refresh and ALL_CACHE_KEY in _cache:
        return list(_cache[ALL_CACHE_KEY])

    found: List[str] = []
    seen: set = set()

    def _add(p: Optional[str]):
        if not p:
            return
        n = os.path.normpath(p)
        if n not in seen and _is_valid_addata(n):
            seen.add(n)
            found.append(n)

    # 1. 環境変数
    _add(os.environ.get("ADDATA_ROOT"))

    # 2. 標準位置 (優先度順)
    for std in _STANDARD_PATHS:
        _add(std)

    # 2b. OneDrive ルート × 業務典型サブパス (v10.4)
    for od in _candidate_onedrive_roots():
        for sub in _ONEDRIVE_SUBPATHS:
            _add(os.path.join(od, sub))

    # 3. OneDrive配下 (並列で全候補回収)
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        roots = _candidate_onedrive_roots()
        if roots:
            with ThreadPoolExecutor(max_workers=min(len(roots), 4)) as ex:
                futures = {ex.submit(_walk_for_addata, r, 5, 30000): r for r in roots}
                for fu in as_completed(futures):
                    try:
                        _add(fu.result())
                    except Exception:
                        pass
    except Exception:
        for od in _candidate_onedrive_roots():
            try:
                _add(_walk_for_addata(od, max_depth=5, max_dirs=30000))
            except Exception:
                pass

    _cache[ALL_CACHE_KEY] = list(found)
    return found


def list_candidate_paths() -> List[str]:
    """検索候補を返す（デバッグ用）"""
    out = []
    for env_key in ("ADDATA_ROOT",):
        v = os.environ.get(env_key)
        if v:
            out.append(f"env:{env_key}={v}")
    for std in (r"C:\Addata", r"D:\Addata"):
        out.append(f"std:{std} (exists={os.path.isdir(std)})")
    for od in _candidate_onedrive_roots():
        out.append(f"onedrive:{od}")
    return out


# v10.4: AcesData 並列検出（NEO 生成には不要だが UI 上で「並列にあり」を表示する用途）
def find_acesdata(force_refresh: bool = False) -> Optional[str]:
    """ADDATA と並んで「AcesData」フォルダがある場合のパスを返す。なければ None。
    `find_addata()` で見つけた ADDATA の親フォルダ配下を最初に当たり、
    なければ OneDrive サブパス候補（_ONEDRIVE_SUBPATHS 由来の親 + AcesData）を確認する。
    """
    addata = find_addata(force_refresh=force_refresh)
    if addata:
        sibling = os.path.join(os.path.dirname(addata), "AcesData")
        if os.path.isdir(sibling):
            return sibling
    # OneDrive サブパス候補の親に AcesData が並んでいる構造もチェック
    for od in _candidate_onedrive_roots():
        for sub in _ONEDRIVE_SUBPATHS:
            parent = os.path.dirname(os.path.join(od, sub))
            cand = os.path.join(parent, "AcesData")
            if os.path.isdir(cand):
                return cand
    return None


if __name__ == "__main__":
    import time as _time
    sys.stdout.reconfigure(encoding="utf-8")
    print("=== ADDATA 自動検出診断 ===")
    print("候補一覧:")
    for p in list_candidate_paths():
        print(f"  {p}")
    print()
    t0 = _time.perf_counter()
    found = find_addata()
    elapsed = _time.perf_counter() - t0
    print(f"検出結果: {found!r}")
    print(f"検出時間: {elapsed:.2f} 秒")
    if found:
        print(f"_is_valid_addata: {_is_valid_addata(found)}")
        aces = find_acesdata()
        print(f"並列 AcesData: {aces!r}")
    print()
    print("全候補（find_all_addata）:")
    for c in find_all_addata():
        print(f"  - {c}")
