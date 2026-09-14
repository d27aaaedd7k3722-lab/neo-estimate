# -*- coding: utf-8 -*-
"""bridge.py — PC の Addata を、ブラウザ経由でクラウドのアプリに渡す（2026-09-14）。

クラウド（Linux サーバ）は利用者の PC のフォルダを読めない。そこで、ブラウザ側の部品（addata_bridge/index.html、
File System Access API）で利用者が PC の Addata フォルダを一度選び、必要なファイルだけを base64 で送ってもらう:
  1. COM/（車種マスタ KA06_ALL.DB・COM.CAB など、約 9MB）   → 車種の特定に使う
  2. 見積の車種フォルダ <A-Z>/<車種コード>/（*.DB と *LTB.CHM、数 MB。部品画像 *IMG.CAB は送らない）
サーバ側は、そのセッション専用の一時フォルダ（addata_bridge_<id>）に Addata の形（COM/ と 文字/車種/）で置き、
アプリはそれを ADDATA として vendor（生成器・検算）に渡す。

流れ（app.py STEP 1-A）: 見積を読む → resolve_car()（COM だけで車種コードを決める。別プロセス）→ その車種フォルダが
まだ無ければ、部品に「この車種を送って」と頼んで rerun（読み取り結果は session に取り置き）→ 届いたら生成（make_neo）。
セッションが終わったフォルダは sweep() が消す（顧客情報は入らないが、放置しない）。
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import tempfile
import time
from typing import Optional

from . import reader as _reader

COMPONENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'addata_bridge')
PREFIX = 'addata_bridge_'
_component = None
_SAFE_PART = re.compile(r'^[A-Za-z0-9_.\-]+$')
# 部品から受け取る内容の上限（Addata の実測: COM 69 ファイル・最大 3.4MB、車種フォルダ 30 ファイル前後・最大 7MB）。
# 画面の ZIP アップロード（200MB）や取得URL（300MB）の上限をこの経路で素通りさせない
MAX_FILES_PER_MESSAGE = 400
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_MESSAGE_BYTES = 128 * 1024 * 1024
ALLOWED_EXT = frozenset({'.db', '.cab', '.chm', '.txt', ''})   # '' は COM/AdVer（拡張子なし）
MARKER_DIR = '.bridge'   # フォルダ単位の完了印（COM.ok / W69.ok）。vendor は A〜Z と COM しか見ない


def component():
    """Streamlit のカスタムコンポーネント（index.html）。初回だけ登録する"""
    global _component
    if _component is None:
        import streamlit.components.v1 as components
        _component = components.declare_component('addata_bridge', path=COMPONENT_DIR)
    return _component


def render(want: str, have: list, com: bool, key: str = 'addata_bridge'):
    """部品を描く。戻り値は部品が最後に送った値（{seq, phase, car, files, bytes, error}）か None"""
    return component()(want=want or '', have=list(have or []), com=bool(com), key=key, default=None)


def root(session_state) -> str:
    """このセッションの橋渡し Addata フォルダ（無ければ作る）"""
    bid = session_state.get('_bridge_id')
    if not bid:
        bid = secrets.token_hex(6)
        session_state['_bridge_id'] = bid
    p = os.path.join(tempfile.gettempdir(), PREFIX + bid)
    os.makedirs(p, exist_ok=True)
    return p


def is_bridge(path: Optional[str]) -> bool:
    return bool(path) and os.path.basename(os.path.normpath(str(path))).startswith(PREFIX)


def _complete(path: str, name: str) -> bool:
    """そのフォルダ（COM か 車種コード）が丸ごと届いて差し替え済みか（store() が最後に付ける完了印）"""
    return os.path.isfile(os.path.join(str(path), MARKER_DIR, name + '.ok'))


def has_com(path: Optional[str]) -> bool:
    """COM が丸ごと届いているか。vendor（skill_env.is_addata_partial）が部分 Addata と認める条件と同じ:
    KA06_ALL.DB（車種マスタ）と、AnVer.DB（データ版）か COM.CAB。ここが緩いと、アプリは使えると思い vendor は弾く、が起きる（Codex 44）"""
    if not path or not _complete(str(path), 'COM'):
        return False
    com = os.path.join(str(path), 'COM')
    return os.path.isfile(os.path.join(com, 'KA06_ALL.DB')) and (os.path.isfile(os.path.join(com, 'AnVer.DB')) or os.path.isfile(os.path.join(com, 'COM.CAB')))


def has_car(path: Optional[str], car: str) -> bool:
    """車種フォルダが丸ごと届いているか: 完了印 ＋ <車種>01.DB（部品表の本体。vendor の is_addata が目印にするのと同じ）。
    PC 側のフォルダが壊れていて 01.DB が無ければ「届いた」と見なさず、待ちを解いて理由付きで不合格にする（Codex 45）"""
    car = str(car or '').strip()
    if not path or not car or not _complete(str(path), car):
        return False
    d = os.path.join(str(path), car[0], car)
    if not os.path.isdir(d):
        return False
    names = {n.lower() for n in os.listdir(d)}
    return (car.lower() + '01.db') in names


def cars(path: Optional[str]) -> list:
    """取り込み済みの車種コード（<A-Z>/<車種>）"""
    out = []
    if not path or not os.path.isdir(str(path)):
        return out
    for letter in sorted(os.listdir(str(path))):
        d = os.path.join(str(path), letter)
        if len(letter) == 1 and letter.isalpha() and os.path.isdir(d):
            for car in sorted(os.listdir(d)):
                if os.path.isdir(os.path.join(d, car)) and has_car(path, car):
                    out.append(car)
    return out


def _safe_rel(rel: str) -> Optional[list]:
    """部品から来た相対パスを COM/x か 文字/車種/x に限る（'..' や絶対パスは捨てる）"""
    parts = [q for q in str(rel or '').replace('\\', '/').split('/') if q not in ('', '.')]
    if not parts or '..' in parts or any(not _SAFE_PART.match(q) for q in parts):
        return None
    if parts[0] == 'COM' and len(parts) == 2:
        return parts
    if len(parts) == 3 and len(parts[0]) == 1 and parts[0].isalpha() and parts[1][:1] == parts[0]:
        return parts
    return None


def _clear_all(path: str) -> None:
    """COM を取り直すとき（別のフォルダ／別の版かもしれない）、前の COM・車種フォルダ・完了印を全部消してから受ける。
    新しい COM の取り込みに失敗しても古い COM のまま「使用中」にならない（Codex 43・44）"""
    try:
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        p = os.path.join(path, name)
        if os.path.isdir(p) and (name == 'COM' or name == MARKER_DIR or name.endswith('.part') or (len(name) == 1 and name.isalpha())):
            shutil.rmtree(p, ignore_errors=True)


def store(path: str, files: dict) -> tuple:
    """部品から来た {相対パス: base64} を橋渡しフォルダに書く。(書いたファイル数, バイト数, 捨てた名前) を返す。

    フォルダ（COM／文字/車種）ごとに '.part' の隣に書いてから差し替え、最後に完了印（.bridge/<名前>.ok）を付ける。
    途中で切れたものは完了印が無いので has_com / has_car が偽のまま ＝ 欠けた Addata で NEO を作らない。
    上限（件数・1 ファイル・合計・拡張子）を超えた分は捨てて名前を返す（画面に「捨てた」と出る）。
    """
    n = 0
    total = 0
    dropped = []
    groups: dict = {}
    tainted = set()   # 上限超え・壊れたファイルのあるフォルダ: 何も書かず完了印も付けない（欠けた Addata で作らない。Codex 42）
    for i, (rel, b64) in enumerate((files or {}).items()):
        parts = _safe_rel(rel)
        if not parts:
            dropped.append(str(rel)[:60])
            continue
        g = tuple(parts[:-1])
        if os.path.splitext(parts[-1])[1].lower() not in ALLOWED_EXT:
            dropped.append(str(rel)[:60])   # Addata に無い種類のファイル（desktop.ini 等）は無視。揃っているかには関係ない
            continue
        approx = len(b64) * 3 // 4 if isinstance(b64, str) else MAX_FILE_BYTES + 1
        if i >= MAX_FILES_PER_MESSAGE or approx > MAX_FILE_BYTES or total + approx > MAX_MESSAGE_BYTES:
            dropped.append(str(rel)[:60])
            tainted.add(g)
            continue
        total += approx
        groups.setdefault(g, []).append((parts[-1], b64))
    total = 0
    for dparts, items in groups.items():
        if dparts in tainted:
            continue
        final = os.path.join(path, *dparts)
        tmp = final + '.part'
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        written = 0
        broken = False
        for name, b64 in items:
            try:
                data = base64.b64decode(b64, validate=True)   # 文字の混入は壊れているとみなす（validate=False は黙って捨てる）
                if not data:
                    raise ValueError('empty')
            except Exception:  # noqa: BLE001
                dropped.append('/'.join(dparts + (name,))[:60])
                broken = True
                break
            with open(os.path.join(tmp, name), 'wb') as f:
                f.write(data)
            written += 1
            total += len(data)
        if broken or not written:
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        marker_dir = os.path.join(path, MARKER_DIR)
        os.makedirs(marker_dir, exist_ok=True)
        marker = os.path.join(marker_dir, dparts[-1] + '.ok')
        if os.path.exists(marker):
            os.remove(marker)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
        with open(marker, 'w', encoding='utf-8') as f:
            f.write(str(written))
        n += written
    return n, total, dropped


def ingest(session_state, value) -> Optional[str]:
    """部品の戻り値を 1 回だけ処理して、画面に出す短い文を返す（同じ seq は二度処理しない）"""
    if not isinstance(value, dict) or not value.get('seq'):
        return None
    # 二重処理防止: nonce（iframe ごとの印。読み直すと変わる）ごとに、処理した最大の seq を覚える。
    # 同じ値の再送だけでなく、遅れて届いた古い値（seq が小さい）も受けない（古い COM で新しい選択を上書きしない。Codex 51）
    nonce = str(value.get('nonce') or '')
    try:
        seq = int(value.get('seq'))
    except (TypeError, ValueError):
        return None
    seen = session_state.get('_bridge_seen')
    if not isinstance(seen, dict):
        seen = {}
    active = seen.get('_active')
    if active is not None and nonce != active and nonce in seen:
        return None   # 前の iframe（読み直す前）の遅れた値: いまの iframe に切り替わった後は受けない（Codex 52）
    if seq <= int(seen.get(nonce) or 0):
        return None
    seen[nonce] = seq
    seen['_active'] = nonce
    session_state['_bridge_seen'] = seen
    path = root(session_state)
    phase = value.get('phase')
    if value.get('error'):
        if phase == 'com':
            _clear_all(path)   # COM の無いフォルダを選び直した: 前の Addata を消して「未接続」に戻す（古い Addata で黙って作らない）
            # 車種フォルダ待ち（_bridge_want）はそのまま残す: 正しいフォルダを選び直せば COM → 車種フォルダ と続き、
            # 取り置いた読み取りから再開できる（消すと app が「届いたのに無い」と見て取り置きを捨て、読み直しになる。Codex 48）
        elif has_com(path) and str(value.get('car') or '') == str(session_state.get('_bridge_want') or ''):
            # いま待っている車種のフォルダが PC に無い: 待ちを解いて app が理由付きで不合格にする。
            # 別の（前の）車種の遅れた error や、COM を受け取っていない間の error では待ちを解かない（Codex 49）
            session_state['_bridge_want'] = ''
        session_state['_bridge_msg'] = str(value['error'])[:200]
        return session_state['_bridge_msg']
    if phase == 'com':
        _clear_all(path)   # 前の COM・車種フォルダ・完了印を消してから受ける（別のフォルダ・別の版と混ぜない。失敗したら古い COM を使わない）
    n, total, dropped = store(path, value.get('files') or {})
    why = f"（{len(dropped)} 件が上限超え・壊れている・Addata の形でない: {', '.join(dropped[:3])}）" if dropped else ''
    if phase == 'com':
        session_state['_bridge_root_name'] = str(value.get('root_name') or '')
        if has_com(path):
            msg = f"COM を取り込みました（{n} ファイル・{total / 1048576:.1f}MB）"
        else:
            msg = f"COM を取り込めませんでした{why}"
    elif phase == 'car':
        car = str(value.get('car') or '')
        if has_car(path, car):
            msg = f"車種 {car} のフォルダを取り込みました（{n} ファイル・{total / 1048576:.1f}MB）"
        else:
            msg = f"車種 {car} のフォルダを取り込めませんでした{why}"   # 待ち続けない: STEP 1-A が理由付きで不合格にする
        if session_state.get('_bridge_want') == car:
            session_state['_bridge_want'] = ''
    else:
        msg = f"取り込み（{phase}）: {n} ファイル"
    if dropped and 'ませんでした' not in msg:
        msg += f"。Addata の形でない {len(dropped)} 件は捨てました"
    session_state['_bridge_msg'] = msg
    return msg


def disconnect(session_state) -> None:
    """PC の Addata との接続を解除: 一時フォルダ・取り置き（作業フォルダ）・待ち・状態を全部消す。
    以後 find_addata_dir() は他の手段（ZIP・パス・取得URL・環境変数・自動検出）に戻る（Codex 45）"""
    pending = session_state.pop('_bridge_pending', None) or {}
    if pending.get('case_dir'):
        try:
            from . import maker as _maker
            _maker.remove_case_dir(pending.get('case_dir'))
        except Exception:  # noqa: BLE001
            pass
    bid = session_state.get('_bridge_id')
    if bid:
        shutil.rmtree(os.path.join(tempfile.gettempdir(), PREFIX + str(bid)), ignore_errors=True)
    # _bridge_seen は残す: 部品は次の rerun でも最後に送った値（同じ seq・nonce）を返すので、消すとその古い COM を新しい値と
    # 見なして黙って再接続してしまう（Codex 47）。選び直せば seq が進み、iframe を読み直せば nonce が変わるので処理される
    for k in ('_bridge_path', '_bridge_id', '_bridge_want', '_bridge_msg', '_bridge_root_name'):
        session_state.pop(k, None)


def resolve_car(path: str, reading: dict) -> dict:
    """reading の vehicle / hints から車種コードを決める（vendor の AddataVehicleResolver を別プロセスで。COM だけで足りる）"""
    try:
        return _reader._runner('resolve_vehicle', addata_root=path, vehicle=(reading or {}).get('vehicle') or {},
                               hints=(reading or {}).get('hints') or {})
    except Exception as e:  # noqa: BLE001
        return {'car_code': '', 'error': f'{type(e).__name__}: {e}'}


def version(path: Optional[str]) -> str:
    """COM/AnVer.DB の版（addata_locator と同じ読み方）。読めなければ ''"""
    try:
        import addata_locator
        return addata_locator.addata_version(path) or ''
    except Exception:  # noqa: BLE001
        return ''


def sweep(max_age_sec: float = 6 * 3600.0) -> int:
    """古い橋渡しフォルダを消す（セッションが終わっても残るため）。消した数を返す"""
    base = tempfile.gettempdir()
    n = 0
    now = time.time()
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(PREFIX):
            continue
        p = os.path.join(base, name)
        try:
            if now - os.path.getmtime(p) > max_age_sec:
                shutil.rmtree(p, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


def touch(path: str) -> None:
    """使用中の印（sweep に消されないよう更新時刻を進める）"""
    try:
        os.utime(path, None)
    except OSError:
        pass
