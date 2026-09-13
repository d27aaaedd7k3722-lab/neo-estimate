# -*- coding: utf-8 -*-
"""ADDATA の共通表（COM/COM.CAB）を ADDATA の版ごとに展開してキャッシュし、表のパスを返す。

ADDATA はコグニセブンから毎月更新が届く。COM.CAB の中の表のうち
  - DATAUP.DB（車種ごとのデータ更新年月 → Car.WorkCodeUpdateDate）
  - Katashiki.DB（型式 → 車種・年式・ボディ・グレード）
は毎月変わる（2026-09-12 に確認: 同梱の reference/ は 2026-09-06 の写しで、2026/08 版と比べて W87 の更新年月と
新型式の行が違っていた。実案件 NEO 638 本の突き合わせで WorkCodeUpdateDate が 16 本ずれた原因）。
そこで、使っている ADDATA の COM.CAB を展開して読む。reference/ の同梱コピーは展開できない PC 用の予備。

キャッシュ: %LOCALAPPDATA%\\claude_neo_pipeline\\com\\<ADDATA の場所のハッシュ_COM.CAB の大きさ_更新時刻>\\
展開は一時フォルダで行い、必須の表が揃っていることを確かめてから rename で公開する（同時に動いても壊れない）
表を読んで覚えておく側（NeoBuilder.dataup_date・AddataVehicleResolver.katashiki）は version() を一緒に覚え、版が変わったら読み直す。
（車種ごとの DB の lru_cache は対象外。ADDATA を更新したら、長く動いているプロセスは作り直す）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REFERENCE = os.path.join(HERE, 'reference')
# 展開が揃ったとみなす表（どれか 1 つでも欠けた展開は公開しない。部分展開を正常扱いしない。Codex 指摘）
REQUIRED = ('DATAUP.DB', 'Katashiki.DB', 'T_KEI_1.DB', 'T_KEI_3.DB', 'BOOTH.DB', 'F_S.DB')
MONTHLY = ('DATAUP.DB', 'Katashiki.DB')   # 毎月変わる表（予備から読んだら報告する）
_lock = threading.Lock()
_memo: dict = {}          # (root, COM.CAB の大きさ, 更新時刻) → (展開先 or None, 記録した時刻)
_RETRY = 60.0             # 展開に失敗したら、この秒数たってから展開をやり直す（一時的な失敗で固定しない。Codex 指摘）
_source = threading.local()  # 表名 → 実際に読んだ場所。スレッドごと（並行ビルドで混ざらない。Codex 指摘）


def _cab(root: str) -> Optional[str]:
    p = os.path.join(root or '', 'COM', 'COM.CAB')
    return p if root and os.path.isfile(p) else None


def _ok(d: str) -> bool:
    return all(os.path.isfile(os.path.join(d, n)) and os.path.getsize(os.path.join(d, n)) > 0 for n in REQUIRED)


def com_dir(root: str) -> Optional[str]:
    """ADDATA（root）の COM.CAB を展開したフォルダ。展開できなければ None。
    COM.CAB の大きさと更新時刻ごとにキャッシュするので、ADDATA が更新されれば（同じプロセスの中でも）新しく展開し直す"""
    root = str(root or '')
    cab = _cab(root)
    if not cab:
        return None
    try:
        st = os.stat(cab)
    except OSError:
        return None
    key = (root, st.st_size, int(st.st_mtime))
    base = os.path.join(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(), 'claude_neo_pipeline', 'com')
    import hashlib
    rid = hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode('utf-8')).hexdigest()[:8]  # ADDATA の置き場ごとに分ける（別の ADDATA の展開を使い回さない。Codex 指摘）
    cache = os.path.join(base, f'{rid}_{st.st_size}_{int(st.st_mtime)}')
    with _lock:
        hit = _memo.get(key)
    if hit is not None:
        if hit[0] is not None and _ok(hit[0]):
            return hit[0]
        if _ok(cache):  # 前回は失敗したが、その後に別プロセスが揃えて公開した
            with _lock:
                _memo[key] = (cache, time.time())
            return cache
        if time.time() - hit[1] < _RETRY:
            return None
    out = None
    try:
        if _ok(cache):
            out = cache
        else:
            os.makedirs(base, exist_ok=True)
            tmp = tempfile.mkdtemp(dir=base, prefix='x.', suffix='.tmp')
            try:
                exe = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'System32', 'expand.exe')
                r = subprocess.run([exe if os.path.exists(exe) else 'expand', cab, '-F:*', tmp],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                if r.returncode == 0 and _ok(tmp):   # 失敗・部分展開は公開しない
                    try:
                        os.rename(tmp, cache)   # 置き場所が空なら成功。先に別プロセスが置いていたら失敗する（相手のものを使う）
                    except OSError:
                        pass
                out = cache if _ok(cache) else None
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    except Exception:  # noqa: BLE001  expand.exe が無い・権限・時間切れ → 予備（reference）で続ける
        out = None
    with _lock:
        _memo[key] = (out, time.time())
    return out


def version(root: str) -> Optional[tuple]:
    """COM.CAB の版（大きさ, 更新時刻）。表を読んだ側のキャッシュが古くなったかを見るのに使う。COM.CAB が無ければ None"""
    cab = _cab(str(root or ''))
    if not cab:
        return None
    try:
        st = os.stat(cab)
        return (st.st_size, int(st.st_mtime))
    except OSError:
        return None


def _src() -> dict:
    d = getattr(_source, 'd', None)
    if d is None:
        d = _source.d = {}
    return d


def reset_sources() -> None:
    """ビルドの始めに呼ぶ（このスレッドでどの表をどこから読んだかの記録を空にする）"""
    _source.d = {}


def com_path(root: str, name: str) -> str:
    """表 name（例 'DATAUP.DB'）を読む場所。**COM.CAB の展開キャッシュ**（いま使っている ADDATA の版）→ ADDATA の COM/ に
    ばらで置かれたもの → 同梱の reference/ の順。ばらのファイルは前の月の展開が残っていることがあるので CAB を優先する（Codex 指摘）。
    どれも無ければ reference/ のパス（存在しない）を返す"""
    d = com_dir(root)
    if d and os.path.isfile(os.path.join(d, name)):
        _src()[name] = 'cache'
        return os.path.join(d, name)
    direct = os.path.join(str(root or ''), 'COM', name)
    if root and os.path.isfile(direct):
        # COM.CAB を展開できず、ばら置きの表を使った。毎月変わる表は前の月の展開残りかもしれないので「予備を使った」扱いで報告する（Codex 指摘）
        _src()[name] = 'loose' if (name in MONTHLY and _cab(str(root or ''))) else 'addata'
        return direct
    ref = os.path.join(REFERENCE, name)
    _src()[name] = 'reference' if os.path.isfile(ref) else ''
    return ref


def stale_reference_used() -> list:
    """このスレッドで、毎月変わる表を COM.CAB の展開以外（同梱の予備 reference/、または CAB を展開できずばら置きの表）から読んだもの。
    ADDATA の版とずれている可能性がある"""
    return sorted(n for n, s in _src().items() if s in ('reference', 'loose') and n in MONTHLY)
