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
import struct
import subprocess
import tempfile
import threading
import time
import zlib
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


def find_7z() -> Optional[str]:
    """7-Zip の実行ファイル（Linux の p7zip: 7z / 7za / 7zz、Windows の 7-Zip）。無ければ None。
    環境変数 PDF_TO_NEO_7Z でフルパスを指定できる（Streamlit Cloud は packages.txt の p7zip-full で 7z が PATH に入る）"""
    p = os.environ.get('PDF_TO_NEO_7Z', '').strip()
    if p and os.path.isfile(p):
        return p
    for name in ('7z', '7za', '7zz'):
        w = shutil.which(name)
        if w:
            return w
    for cand in (os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), '7-Zip', '7z.exe'),
                 os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), '7-Zip', '7z.exe')):
        if os.path.isfile(cand):
            return cand
    return None


def _cab_checksum(data: bytes, seed: int) -> int:
    """CAB の検査和（MS-CAB 仕様 / cabextract と同じ: 4 バイトずつ XOR、端数は上位から詰める）"""
    csum = seed
    n = len(data) // 4
    for i in range(n):
        csum ^= int.from_bytes(data[4 * i:4 * i + 4], 'little')
    ul = 0
    for b in data[4 * n:]:
        ul = (ul << 8) | b
    return (csum ^ ul) & 0xFFFFFFFF


class UnsupportedCab(ValueError):
    """純 Python では展開できない圧縮方式（LZX / Quantum）。7z に回してよいのはこれだけ"""


def _safe_cab_parts(name: str) -> list:
    """CAB の中のファイル名を展開先の相対パスの部品に分ける。'..'・絶対パス・ドライブ文字・空は ValueError"""
    parts = [q for q in name.replace('\\', '/').split('/') if q not in ('', '.')]
    if not parts or '..' in parts or os.path.isabs(name) or any(':' in q for q in parts):
        raise ValueError(f'CAB の中の安全でない名前: {name!r}')
    return parts


def extract_cab(cab: str, dest: str) -> list:
    """CAB を純 Python で展開する（MSZIP・無圧縮。COM.CAB は 1 フォルダ MSZIP）。展開したファイル名を返す。
    CFDATA ブロックごとに 'CK' + raw deflate、LZ77 の窓（32KB）は前ブロックから引き継ぐ（zdict）。
    expand.exe（Windows 付属）が無い Linux / Streamlit Cloud 用。expand.exe の展開と全 53 ファイル一致を確認（2026-09-14）"""
    data = open(cab, 'rb').read()
    if data[:4] != b'MSCF':
        raise ValueError('CAB ではない: ' + cab)
    coffFiles, = struct.unpack_from('<I', data, 16)
    cFolders, cFiles, flags = struct.unpack_from('<HHH', data, 26)
    pos = 36
    cbCFFolder = cbCFData = 0
    if flags & 4:  # 予約領域
        cbCFHeader, cbCFFolder, cbCFData = struct.unpack_from('<HBB', data, pos)
        pos += 4 + cbCFHeader
    for _ in range((2 if flags & 1 else 0) + (2 if flags & 2 else 0)):  # 前後のキャビネット名（文字列）
        pos = data.index(b'\0', pos) + 1
    folders = []
    for _ in range(cFolders):
        coffCabStart, cCFData, typeCompress = struct.unpack_from('<IHH', data, pos)
        pos += 8 + cbCFFolder
        folders.append((coffCabStart, cCFData, typeCompress & 0x000f))
    files = []
    pos = coffFiles
    for _ in range(cFiles):
        cbFile, uoffFolderStart, iFolder = struct.unpack_from('<IIH', data, pos)
        pos += 16
        end = data.index(b'\0', pos)
        files.append((data[pos:end].decode('cp932', 'replace'), cbFile, uoffFolderStart, iFolder))
        pos = end + 1
    for name, _cb, _off, _fi in files:  # 名前の安全性は展開の前（対応外の圧縮方式で 7z に回す前）に見る（Codex 指摘）
        _safe_cab_parts(name)
    out = []
    for fi, (coffCabStart, cCFData, comp) in enumerate(folders):
        buf = bytearray()
        p = coffCabStart
        window = b''
        for _ in range(cCFData):
            csum, cbData, cbUncomp = struct.unpack_from('<IHH', data, p)
            hdr = data[p + 4:p + 8 + cbCFData]
            p += 8 + cbCFData
            blk = data[p:p + cbData]
            p += cbData
            if len(blk) != cbData:
                raise ValueError('CFDATA が途中で切れている')
            if csum and _cab_checksum(bytes(blk), _cab_checksum(bytes(hdr), 0)) != csum:  # csum 0 は「検査和なし」
                raise ValueError('CFDATA の検査和が合わない（壊れた CAB）')
            if comp == 1:  # MSZIP
                if blk[:2] != b'CK':
                    raise ValueError('MSZIP ブロックの印が違う')
                d = zlib.decompressobj(-15, zdict=window) if window else zlib.decompressobj(-15)
                dec = d.decompress(blk[2:]) + d.flush()
            elif comp == 0:
                dec = bytes(blk)
            else:
                raise UnsupportedCab(f'対応していない圧縮 {comp:#x}（LZX / Quantum）')
            if len(dec) != cbUncomp:
                raise ValueError(f'展開サイズが合わない {len(dec)} / {cbUncomp}')
            buf += dec
            window = bytes(buf[-32768:])
        for name, cbFile, off, iFolder in files:
            if iFolder == fi:
                parts = _safe_cab_parts(name)
                if off + cbFile > len(buf):
                    raise ValueError(f'CAB の中のファイルが展開データの範囲を超える: {name!r}')
                fp = os.path.join(dest, *parts)
                os.makedirs(os.path.dirname(fp) or dest, exist_ok=True)
                with open(fp, 'wb') as f:
                    f.write(bytes(buf[off:off + cbFile]))
                out.append(name)
    return out


def _extract_cab_any(cab: str, tmp: str) -> bool:
    """COM.CAB を tmp に展開する。expand.exe（Windows 付属）→ 純 Python → 7z の順。揃えば True"""
    exe = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'System32', 'expand.exe')
    if os.path.isfile(exe):
        try:
            r = subprocess.run([exe, cab, '-F:*', tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            if r.returncode == 0 and _ok(tmp):
                return True
        except Exception:  # noqa: BLE001  権限・時間切れ → 次の手段
            pass
    try:
        extract_cab(cab, tmp)
        if _ok(tmp):
            return True
    except UnsupportedCab:  # 対応外の圧縮方式だけ 7z に回す
        pass
    except Exception:  # noqa: BLE001  検査和・安全でない名前・範囲外・途中で切れた構造（struct.error）… 壊れた CAB は 7z にも渡さない（Codex 指摘）
        return False
    sz = find_7z()
    if sz:
        try:
            r = subprocess.run([sz, 'x', '-y', '-o' + tmp, cab], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            if r.returncode == 0 and _ok(tmp):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


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
                if _extract_cab_any(cab, tmp):   # 失敗・部分展開は公開しない（expand.exe が無い Linux でも純 Python / 7z で展開する）
                    try:
                        os.rename(tmp, cache)   # 置き場所が空なら成功。先に別プロセスが置いていたら失敗する（相手のものを使う）
                    except OSError:
                        pass
                out = cache if _ok(cache) else None
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    except Exception:  # noqa: BLE001  どの手段でも展開できない・権限・時間切れ → 予備（reference）で続ける
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
