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

# 読み込みを始めたときのコードの指紋（ファイルの最後で読み直し、同じ中身のときだけ __app_src_digest__ に控える。読み込みの途中で
# push されたら控えず、古い扱いにして読み直させる。レビュー 3 周目）
try:
    import hashlib as _stamp_hashlib0
    with open(__file__, 'rb') as _stamp_f0:
        _stamp_digest_at_start = _stamp_hashlib0.sha256(_stamp_f0.read()).hexdigest()
    del _stamp_hashlib0, _stamp_f0
except Exception:  # noqa: BLE001
    _stamp_digest_at_start = None

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


def render(want: str, have: list, com: bool, key: str = 'addata_bridge',
           bid: str = '', forget: bool = False, keep: Optional[dict] = None):
    """部品を描く。戻り値は部品が最後に送った値（{seq, phase, car, files, bytes, error}）か None。
    bid … いまの送り先のフォルダ名（部品がブラウザに覚える）／forget … 覚えを消させる／
    keep … ブラウザに覚えさせる設定（フォルダのパス・取得URL）"""
    return component()(want=want or '', have=list(have or []), com=bool(com), key=key, default=None,
                       bid=str(bid or ''), forget=bool(forget), keep=dict(keep or {}))


_BID_RE = re.compile(r'[0-9a-f]{12}|[0-9a-f]{32}')   # 古い 12 桁（48 bit）も受ける。新しく作るのは 32 桁（128 bit）


def adopt(session_state, bridge_id, anver: Optional[bytes] = None) -> bool:
    """ブラウザが覚えていた送り先のフォルダ名を引き継ぐ（画面を読み直しても送り直さなくて済むように）。
    引き継ぐのは次のすべてを満たすときだけ:
      ・名前の形が合っている（当てられない長さ）
      ・そのフォルダに COM が丸ごと届いている
      ・いまのセッションが使える接続を持っていない（別のタブのフォルダに黙って乗り換えない）
      ・PC 側の COM/AnVer.DB（データ版）と、置いてあるものが同じ（PC で Addata を入れ替えたら引き継がない。
        引き継がなければ部品が COM から送り直すので、古い版で照合し続けることがない）"""
    bid = str(bridge_id or '').strip()
    if not _BID_RE.fullmatch(bid) or session_state.get('_bridge_id') == bid:
        return False
    cur = session_state.get('_bridge_path')
    if cur and has_com(cur):
        return False        # いま使えている接続はそのまま
    p = os.path.join(tempfile.gettempdir(), PREFIX + bid)
    if not has_com(p):
        return False
    if anver is not None:
        try:
            with open(os.path.join(p, 'COM', 'AnVer.DB'), 'rb') as fh:
                same = fh.read() == anver
        except OSError:
            same = False
        if not same:
            # PC の Addata が入れ替わっている: 置いてあるものは捨て、COM から送り直させる
            _clear_all(p)
            return False
    old = session_state.get('_bridge_id')
    session_state['_bridge_id'] = bid
    session_state['_bridge_path'] = p
    touch(p)
    if old and old != bid:
        # この run で作ったばかりの空のフォルダを片づける（読み直すたびに増える）
        try:
            os.rmdir(os.path.join(tempfile.gettempdir(), PREFIX + str(old)))
        except OSError:
            pass
    return True


def resend(session_state) -> None:
    """いま送ってある車種マスタ・車種フォルダを消して、部品に送り直させる（PC で Addata を入れ替えたとき）。
    接続そのものは切らない（フォルダの選択は残るので、部品が自動で COM から送り直す）"""
    p = session_state.get('_bridge_path') or ''
    if p and is_bridge(p):
        _clear_all(p)
    session_state.pop('_bridge_path', None)
    session_state['_bridge_msg'] = 'PC の Addata を送り直します（車種マスタから取り直します）'


def sent_at(path: Optional[str]) -> float:
    """その橋渡しフォルダに COM が届いた日時（完了印の更新時刻）。分からなければ 0"""
    try:
        return os.path.getmtime(os.path.join(str(path), MARKER_DIR, 'COM.ok'))
    except (OSError, ValueError, TypeError):
        return 0.0


def root(session_state) -> str:
    """このセッションの橋渡し Addata フォルダ（無ければ作る）"""
    bid = session_state.get('_bridge_id')
    if not bid:
        bid = secrets.token_hex(16)   # 名前が分かれば引き継げる（ブラウザに覚えさせる）ので、当てられない長さにする
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


# 車種フォルダに必ず要る DB の番号。実在の C:\Addata 1,307 車種で 01・11 は全部にあるが、12.DB は 108 車種・15.DB は 69 車種に無い
# （輸入車・汎用車・旧型）。生成器はその欠落を許容する（12 は属性空、15 は無ければ空）ので、必須は 01（車種）と 11（部品表）だけ
ESSENTIAL_CAR_DB = ('01', '11')


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
    # 生成器が読む DB の本体が揃っているか（01 車種・11 部品表）。01 だけでは「届いた」と見なさない
    # （欠けたまま作ると生成器が黙って汎用値で NEO を作る。Codex hunt D2 2026-09-15）
    return all((car.lower() + n + '.db') in names for n in ESSENTIAL_CAR_DB)


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
    forget_vendor_cache(path)   # 前の COM の展開キャッシュも（新しい COM は mtime が違うので別の鍵になる）


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
        written = 0
        broken = False
        try:   # 書けない（権限・ディスク満杯）ときはそのグループを壊れた扱いにし、完了印を付けない（欠けた Addata で作らない。J5）
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp, exist_ok=True)
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
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            dropped.append('/'.join(dparts)[:40] + f'（書き込み失敗 {type(e).__name__}）')
            continue
        n += written
    _cap_car_dirs(path)
    return n, total, dropped


MAX_CAR_DIRS = 6   # 1 セッションに残す車種フォルダの数（古いものから消す。溜め続けると一時領域が尽きる。J7）


def _cap_car_dirs(path: str) -> None:
    """文字/車種 のフォルダが MAX_CAR_DIRS を超えたら、古い（mtime の小さい）ものと完了印を消す"""
    try:
        cars = []
        for letter in os.listdir(path):
            d = os.path.join(path, letter)
            if len(letter) == 1 and letter.isalpha() and os.path.isdir(d):
                for car in os.listdir(d):
                    cd = os.path.join(d, car)
                    if os.path.isdir(cd) and not cd.endswith('.part'):
                        cars.append((os.path.getmtime(cd), cd, car))
        cars.sort()
        for _m, cd, car in cars[:max(0, len(cars) - MAX_CAR_DIRS)]:
            shutil.rmtree(cd, ignore_errors=True)
            mk = os.path.join(path, MARKER_DIR, car + '.ok')
            if os.path.exists(mk):
                os.remove(mk)
    except OSError:
        pass


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
    phase = value.get('phase')
    if phase == 'hello':
        session_state['_bridge_hello_done'] = True
        # 部品が起動時に送る「前に覚えた送り先」。引き継げたら、送り直さずに続きができる。
        # 覚えていた設定（フォルダのパス・取得URL）も、いまの画面が空のときだけ引き継ぐ（URL のクエリが優先）
        keep = value.get('keep')
        if isinstance(keep, dict):
            for _k in ('addata_dir', 'addata_url'):
                _v = str(keep.get(_k) or '').strip()[:500]
                if _v and not str(session_state.get('_setting_' + _k) or '').strip():
                    session_state['_setting_' + _k] = _v
        _anver = None
        _av = value.get('anver')
        if isinstance(_av, str) and _av:
            try:
                _anver = base64.b64decode(_av, validate=True)[:4096]
            except Exception:  # noqa: BLE001
                _anver = None
        if adopt(session_state, value.get('bridge_id'), _anver):
            session_state['_bridge_root_name'] = str(value.get('root_name') or session_state.get('_bridge_root_name') or '')[:64]
            session_state['_bridge_msg'] = 'このブラウザに覚えていた PC の Addata につなぎ直しました'
            return session_state['_bridge_msg']
        return None
    path = root(session_state)
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
    if phase not in ('com', 'car'):   # 部品からの値の形が違う（型・長さ）ものは扱わない（J6）
        session_state['_bridge_msg'] = f'取り込みを無視しました（phase={str(phase)[:20]!r}）'
        return session_state['_bridge_msg']
    if not isinstance(value.get('files'), dict):
        value = dict(value, files={})
    if phase == 'com':
        _clear_all(path)   # 前の COM・車種フォルダ・完了印を消してから受ける（別のフォルダ・別の版と混ぜない。失敗したら古い COM を使わない）
    n, total, dropped = store(path, value.get('files') or {})
    why = f"（{len(dropped)} 件が上限超え・壊れている・Addata の形でない: {', '.join(dropped[:3])}）" if dropped else ''
    if phase == 'com':
        session_state['_bridge_root_name'] = str(value.get('root_name') or '')[:64]
        if has_com(path):
            msg = f"COM を取り込みました（{n} ファイル・{total / 1048576:.1f}MB）"
        else:
            msg = f"COM を取り込めませんでした{why}"
    elif phase == 'car':
        car = str(value.get('car') or '')[:32]
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
        # 消せなかったフォルダ（別プロセスが掴んでいる等）は黙って忘れない。顧客情報を含むので、
        # 印を残して画面の定期掃除がもう一度消しに行く（2026-09-21 バグハント。以前は例外ごと握りつぶしていた）
        left = pending.get('case_dir')
        try:
            from . import maker as _maker
            left = _maker.remove_case_dir(left)
        except Exception:  # noqa: BLE001
            pass
        if left:
            _rest = [p for p in (session_state.get('_case_dirs_left') or []) if p != left]
            session_state['_case_dirs_left'] = (_rest + [left])[-10:]
    bid = session_state.get('_bridge_id')
    if bid:
        _p = os.path.join(tempfile.gettempdir(), PREFIX + str(bid))
        shutil.rmtree(_p, ignore_errors=True)
        forget_vendor_cache(_p)   # 解除後は _bridge_id も消え sweep() が二度と見ないので、ここで消す（レビュー 2026-09-15）
    # _bridge_seen は残す: 部品は次の rerun でも最後に送った値（同じ seq・nonce）を返すので、消すとその古い COM を新しい値と
    # 見なして黙って再接続してしまう（Codex 47）。選び直せば seq が進み、iframe を読み直せば nonce が変わるので処理される
    for k in ('_bridge_path', '_bridge_id', '_bridge_want', '_bridge_msg', '_bridge_root_name'):
        session_state.pop(k, None)
    session_state['_bridge_forget'] = True   # 部品にも覚え（localStorage・IndexedDB）を消させる


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


def _vendor_cache_bases() -> list:
    """vendor が COM.CAB を展開するキャッシュの置き場の候補（subprocess の LOCALAPPDATA = neo_skill.vendor.subprocess_env と同じ決め方＋予備）"""
    out = []
    for b in (os.environ.get('LOCALAPPDATA'), os.path.join(tempfile.gettempdir(), 'neo_skill_cache'), tempfile.gettempdir()):
        if b and b not in out:
            out.append(b)
    return out


def forget_vendor_cache(path: str) -> int:
    """この橋渡しフォルダ（root）用に vendor が作った COM.CAB の展開キャッシュを消す。消した数を返す。
    橋渡しは root が毎回 addata_bridge_<乱数> なので、root を消すときに一緒に消さないと展開が溜まり続ける
    （この PC で 123 個・約 700MB。バグハント J2）。鍵は vendor の com_tables と同じ sha1(normcase(abspath(root)))[:8]"""
    import hashlib
    try:
        rid = hashlib.sha1(os.path.normcase(os.path.abspath(str(path))).encode('utf-8')).hexdigest()[:8]
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for base in _vendor_cache_bases():
        d = os.path.join(base, 'claude_neo_pipeline', 'com')
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name.startswith(rid + '_'):
                shutil.rmtree(os.path.join(d, name), ignore_errors=True)
                n += 1
    return n


def sweep(max_age_sec: float = 6 * 3600.0) -> int:
    """古い橋渡しフォルダを消す（セッションが終わっても残るため）。消した数を返す。そのフォルダ用の vendor の展開キャッシュも消す"""
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
                forget_vendor_cache(p)
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
