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
# 読み込みを始めたときのコードの指紋（ファイルの最後で読み直し、同じ中身のときだけ __app_src_digest__ に控える。読み込みの途中で
# push されたら控えず、古い扱いにして読み直させる。レビュー 3 周目）
try:
    import hashlib as _stamp_hashlib0
    with open(__file__, 'rb') as _stamp_f0:
        _stamp_digest_at_start = _stamp_hashlib0.sha256(_stamp_f0.read()).hexdigest()
    del _stamp_hashlib0, _stamp_f0
except Exception:  # noqa: BLE001
    _stamp_digest_at_start = None

import io
import json
import os
import sys
import re
import threading
from typing import List, Optional

# ファイル探索の壁時計の上限（秒）。応答しない共有・未同期の OneDrive では
# os.listdir / open がそのまま返らないことがあり、件数の上限では止まらない。
# pdf-to-neo スキルの skill_env.py と同じ考え方。
try:
    _SEARCH_SECONDS = float(os.environ.get('ADDATA_SCAN_SECONDS') or 20)
except ValueError:
    _SEARCH_SECONDS = 20.0

# env_check.py --save が書く、この PC の設定
_SKILL_CONFIG = os.path.join(os.path.expanduser('~'), '.claude',
                             'pdf-to-neo.local.json')


def _bounded(fn, seconds: float, default):
    """fn() を壁時計で打ち切る。返らなければ default。

    応答しない共有フォルダでは os.listdir が返らないので、時間の上限は
    スレッド境界でしか作れない（中のスレッドは daemon なので置き去りでよい）。
    """
    box = {}

    def _run():
        try:
            box['v'] = fn()
        except BaseException:      # noqa: BLE001  探索の失敗で呼び出し側を止めない
            pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(max(0.1, seconds))
    return box.get('v', default)


def config_addata_root() -> Optional[str]:
    """pdf-to-neo スキルの env_check --save が決めた ADDATA。

    同じ PC で 2 つの実装が別々の ADDATA を掴むと、標準品番・標準指数が
    変わって協定見積の中身が変わる。env_check を通した PC では、
    そこで決めた場所をアプリでも使う。
    Streamlit Cloud のように設定ファイルが無い環境では単に None。
    """
    try:
        with io.open(_SKILL_CONFIG, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    root = cfg.get('ADDATA_ROOT') if isinstance(cfg, dict) else None
    return root if isinstance(root, str) and root else None

CACHE_KEY = "_ADDATA_LOCATOR_CACHE"
ALL_CACHE_KEY = "_ADDATA_LOCATOR_ALL_CACHE"
# 版（COM/AnVer.DB）を読めなかった候補。読めないまま順位を付けると
# 「並び順で選ぶ」ことになり、古い ADDATA を静かに掴む。画面で知らせる。
RANK_KEY = "_ADDATA_LOCATOR_RANK_FAILED"
# 締切で見送った候補。「無効と分かった」のではなく「時間が足りなくて
# 見ていない」。未検出と区別しないと、応答の遅い PC で Addata があるのに
# ベタ打ちモードに落ちる。
SKIPPED_KEY = "_ADDATA_LOCATOR_SKIPPED"
# 時間切れの未検出を、いつまで信じるか。毎回の画面更新で全部を探し直すと
# 遅い PC では操作のたびに固まる。かといって永久に未検出のままにすると、
# Addata が手元にあるのにベタ打ちモードから戻れない。
RETRY_KEY = "_ADDATA_LOCATOR_RETRY_AFTER"
_RETRY_SECONDS = 60.0
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
                    stats: Optional[dict] = None,
                    collect: Optional[List[str]] = None) -> Optional[str]:
    """root から再帰検索して 'Addata' フォルダかつ妥当判定で見つける。
    stats に dict を渡すと {visited:N, hit_path:str|None} が書き戻される。

    collect に list を渡すと、最初の1件で止めずに**見つかったもの全部**を
    そこに入れて走査を続ける。1つのルートの下に版の違う Addata が並んで
    いることがあり、最初に当たったものを採ると古い版を掴む。
    """
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
            return None if collect is None else (collect[0] if collect else None)
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
                    if collect is not None:
                        if cand not in collect:
                            collect.append(cand)
                        continue
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
                                    if collect is not None:
                                        if cand not in collect:
                                            collect.append(cand)
                                        continue
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


def addata_version(path: Optional[str]) -> str:
    """ADDATA のデータ版（例 '2026/08'）。取れなければ ''。

    COM/AnVer.DB は XOR 0xFF の INI で、'Number=2026/08' の行を持つ。
    PC ごとに版が違うと標準品番・標準指数が変わる。つまり古い版で照合すると、
    協定見積に載る部品コードや指数が実機と食い違う。
    （pdf-to-neo 配布パッケージ skill_env.py の考え方を採用）
    """
    if not path:
        return ''
    fp = os.path.join(path, 'COM', 'AnVer.DB')
    try:
        with open(fp, 'rb') as f:
            text = bytes(x ^ 0xFF for x in f.read()).decode('cp932', 'replace')
    except OSError:
        return ''
    for line in text.splitlines():
        if line.lower().startswith('number='):
            v = line.split('=', 1)[1].strip()
            # 版は「2026/08」の形だけ受ける。?addata_url= の ZIP などで外から入る値なので、
            # 形の違うもの（HTML を仕込んだもの等）は「不明」にする（バグハント第 3 弾 D1）
            return v if re.fullmatch(r'\d{4}/\d{1,2}', v) else ''
    return ''


def addata_version_key(path: Optional[str]) -> tuple:
    """候補が複数あるときの新しさ。データ版を優先し、無ければ更新日時。

    フォルダごとコピーすると更新日時が当てにならないので、版そのものを先に見る。
    """
    v = addata_version(path)
    num = 0.0
    if v:
        digits = ''.join(ch for ch in v if ch.isdigit())
        if len(digits) >= 6:
            num = float(digits[:6])          # 202608 のように年月で比べる
    for name in ('AnVer.DB', 'COM.CAB'):
        fp = os.path.join(path or '', 'COM', name)
        try:
            if os.path.isfile(fp):
                return (num, os.path.getmtime(fp))
        except OSError:
            break
    return (num, 0.0)


def find_addata(force_refresh: bool = False) -> Optional[str]:
    """ADDATA ルートを自動検索して返す。見つからなければ None。
    結果はモジュールキャッシュ。force_refresh=True で再検索。"""
    import time as _time
    if not force_refresh and CACHE_KEY in _cache:
        _hit = _cache[CACHE_KEY]
        if _hit is not None:
            return _hit
        # 時間切れの未検出はしばらくしたら探し直す。毎回の画面更新で
        # 全部を探し直すと、遅い PC では操作のたびに固まる。
        if _time.time() < (_cache.get(RETRY_KEY) or 0):
            return None

    _deadline = _time.time() + _SEARCH_SECONDS
    _rank_failed: List[str] = []      # 版を読めなかった候補
    _skipped: List[str] = []          # 締切で見ていない候補

    def _done(value):
        """どの返り道でも、今回の探索の状態を残す。

        前は順位を付けた2か所でしか控えを書いていなかったので、
        環境変数で決まったときや未検出のときに**前回の探索の控え**が
        そのまま画面に出ていた。
        """
        _cache[RANK_KEY] = list(_rank_failed)
        _cache[SKIPPED_KEY] = list(_skipped)
        # 締切で見送った候補が残っているときの未検出は「無い」ではなく
        # 「見ていない」。キャッシュに残すと、以後ずっと未検出になる。
        _cache[CACHE_KEY] = value
        if value is None and _skipped:
            # 「無い」と決まったわけではないので、しばらくしたら探し直す
            _cache[RETRY_KEY] = _time.time() + _RETRY_SECONDS
        else:
            _cache.pop(RETRY_KEY, None)
        return value

    def _left(cap: float) -> float:
        # 残り時間。0 以下なら「もう触らない」。下限を置くと、締切を過ぎて
        # からも候補の数だけ待ち直すことになり、約束した上限を超える。
        return min(cap, _deadline - _time.time())

    def _valid(p, cap=3.0) -> bool:
        # 応答しない共有を指していると os.listdir が返らない。
        # 呼び出し側（画面）を止めないよう、1 候補ずつ上限をかける。
        # 時間切れは「無効」ではなく「見ていない」なので、控えておく。
        t = _left(cap)
        if t <= 0:
            _skipped.append(p)
            return False
        got = _bounded(lambda: _is_valid_addata(p), t, None)
        if got is None:
            _skipped.append(p)
            return False
        return bool(got)

    def _vkey(p):
        """版の新しさ。

        読むのは COM/AnVer.DB（小さい INI）1つだけなので、フォルダ探索の
        締切とは**別枠**にする。ここで締切を理由に諦めると、全部の候補が
        同じ点数になって「並び順で選ぶ」ことになり、古い ADDATA を静かに
        掴む。読めなかった候補は控えておき、画面で知らせる。
        """
        def _probe():
            # 版そのものと、順位に使う組を1回で取る
            return (addata_version(p), addata_version_key(p))

        got = _bounded(_probe, 3.0, None)
        if got is None or not got[0]:
            # 読めない、または AnVer.DB に Number= が無い。
            # 更新日時で代用した順位は「版で比べた」ことにならないので、
            # 黙って順位を付けず、読めなかった候補として控える。
            if p not in _rank_failed:
                _rank_failed.append(p)
            return got[1] if got else (0.0, 0.0)
        return got[1]

    # 1. 環境変数
    env_root = os.environ.get("ADDATA_ROOT")
    if env_root and _valid(env_root):
        return _done(env_root)

    # 1b. pdf-to-neo スキルの設定（env_check.py --save の結果）
    #     同じ PC で 2 つの実装が別々の ADDATA を掴むと、標準品番・
    #     標準指数が変わって協定見積の中身が変わる。
    cfg_root = config_addata_root()
    if cfg_root and _valid(cfg_root):
        return _done(cfg_root)

    # 2. 標準位置と OneDrive の典型サブパスを「全部」見て、データ版が新しいものを選ぶ。
    #    以前は最初に見つかったものをそのまま返していたため、古い C:\Addata を
    #    残したまま新しい版を別の場所に置いている PC では、古い版で照合していた。
    #    版が違うと標準品番・標準指数が変わり、協定見積の中身が変わる。
    #    候補はどれも数個で、読むのは COM/AnVer.DB（小さい INI）だけなので速い。
    _shallow = []
    for std in _STANDARD_PATHS:
        if _valid(std):
            _shallow.append(std)
    for od in _candidate_onedrive_roots():
        for sub in _ONEDRIVE_SUBPATHS:
            cand = os.path.join(od, sub)
            if _valid(cand):
                _shallow.append(cand)
    if _shallow:
        return _done(max(_shallow, key=_vkey))

    # （以前ここに、同じ OneDrive のサブパスをもう一度「締切なしで」見る
    #   2b の段があった。上の 2 とまったく同じ候補を同じ関数で見ており、
    #   違いは時間の上限が効かないことだけ。応答しない共有を指していると
    #   画面が止まり、しかも版を比べずに最初に当たったものを返していた。）

    # 3. OneDrive配下を並列検索（複数候補を同時走査で最大3倍速）
    #    見つかった順ではなく**データ版の新しい順**で選ぶ。最初に返ったものを
    #    採ると、古い ADDATA が置いてある OneDrive を先に読み終えただけで
    #    古い版を掴み、標準品番・標準指数が実機と食い違う。
    #    全体の残り時間で打ち切る（walk は件数の上限では止まらない）。
    def _deep_all() -> List[str]:
        out: List[str] = []
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            roots = _candidate_onedrive_roots()
            if not roots:
                return out
            bags = {r: [] for r in roots}
            with ThreadPoolExecutor(max_workers=min(len(roots), 4)) as ex:
                futures = [ex.submit(_walk_for_addata, r, 5, 30000, None,
                                     bags[r]) for r in roots]
                for fu in as_completed(futures):
                    try:
                        fu.result()
                    except Exception:      # noqa: BLE001
                        pass
            for r in roots:
                out.extend(bags[r])
        except Exception:      # noqa: BLE001  フォールバック: 直列
            for od in _candidate_onedrive_roots():
                bag = []
                try:
                    _walk_for_addata(od, max_depth=5, max_dirs=30000,
                                     collect=bag)
                except Exception:      # noqa: BLE001
                    pass
                out.extend(bag)
        return out

    _deep_left = _left(_SEARCH_SECONDS)
    if _deep_left > 0:
        deep = _bounded(_deep_all, _deep_left, None)
        if deep is None:          # 時間切れ。走査しきっていない
            _skipped.append('(OneDrive 配下の探索)')
            deep = []
    else:
        _skipped.append('(OneDrive 配下の探索)')
        deep = []
    if deep:
        return _done(max(deep, key=_vkey))

    return _done(None)


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
            bags2 = {r: [] for r in roots}
            with ThreadPoolExecutor(max_workers=min(len(roots), 4)) as ex:
                futures = [ex.submit(_walk_for_addata, r, 5, 30000, None,
                                     bags2[r]) for r in roots]
                for fu in as_completed(futures):
                    try:
                        fu.result()
                    except Exception:
                        pass
            for r in roots:
                for q in bags2[r]:
                    _add(q)
    except Exception:
        for od in _candidate_onedrive_roots():
            bag2 = []
            try:
                _walk_for_addata(od, max_depth=5, max_dirs=30000, collect=bag2)
            except Exception:
                pass
            for q in bag2:
                _add(q)

    # 版の新しい順に並べる。優先度順のまま返すと、画面に出したときに
    # 「いちばん上が最新」と読めてしまい、古い版を選ばせることになる。
    found.sort(key=addata_version_key, reverse=True)
    _cache[ALL_CACHE_KEY] = list(found)
    return found


def rank_incomplete() -> List[str]:
    """直前の find_addata で、データ版を読めなかった候補。

    空でなければ「いちばん新しい版を選べていないかもしれない」という意味。
    """
    return list(_cache.get(RANK_KEY) or [])


def search_skipped() -> List[str]:
    """直前の find_addata で、締切のため見ていない候補。

    空でなければ「無いと分かった」のではなく「探しきれていない」。
    未検出と同じ顔で扱うと、応答の遅い PC で Addata があるのに
    ベタ打ちモードに落ちたまま気づけない。
    """
    return list(_cache.get(SKIPPED_KEY) or [])


def newer_addata_candidates(current: Optional[str],
                            budget: float = 8.0) -> List[tuple]:
    """いま使っている ADDATA より新しい版の候補を [(場所, 版), ...] で返す。

    古い C:\\Addata を残したまま新しい版を別の場所に置いている PC では、
    気づかないまま古い版で照合してしまう。版が違うと標準品番・標準指数が
    変わるので、協定見積に載る部品コードや指数が実機と食い違う。
    （pdf-to-neo スキルの env_check.py がしている確認と同じ）

    画面から呼ぶので、探索は budget 秒で打ち切る。
    """
    cur_v = addata_version(current) if current else ''
    if not cur_v:
        return []
    cur_n = os.path.normcase(os.path.abspath(current))
    others = _bounded(lambda: find_all_addata(force_refresh=False), budget, [])
    out = []
    for q in (others or []):
        if os.path.normcase(os.path.abspath(q)) == cur_n:
            continue
        v = addata_version(q)
        if v and addata_version_key(q) > addata_version_key(current):
            out.append((q, v))
    return out


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
