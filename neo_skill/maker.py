# -*- coding: utf-8 -*-
"""maker.py — vendor の make_neo.py をそのまま呼び、NEO と確認箇所シートを組で受け取る。

移植ガイド §2-2 A（一括実行をそのまま呼ぶ）。合否は make_neo.py の main() が決める
（検算差 / 未照合 / 前後左右の食い違い / 低照合率 / 確認箇所シートが作れない → 不合格）。
ここでは終了コードと成果物の有無を見るだけで、合格条件を別に書かない。

不合格の NEO は make_neo が <name>.ng.neo に隔離し <name>.neo は作らない。
アプリは ok のときだけダウンロードを出し、NEO と確認箇所シートを必ず組で渡す。
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

import dataclasses
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Optional

from . import vendor


@dataclasses.dataclass
class MakeResult:
    ok: bool
    returncode: int
    stdout: str
    case_dir: str
    name: str
    neo_path: Optional[str] = None       # 合格したときだけ
    ng_neo_path: Optional[str] = None    # 不合格のときの隔離先
    review_path: Optional[str] = None    # 確認箇所シート（xlsx。openpyxl が無い PC は csv）
    report_path: Optional[str] = None    # report.md（不合格でも書かれる）
    estimate_path: Optional[str] = None
    reasons: list = dataclasses.field(default_factory=list)   # 不合格の理由（make_neo の「不合格: …」行）
    match_line: str = ''                 # 「見積書合計との一致: …」の行
    error: str = ''                      # 例外・実行できなかったとき

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def new_case_dir(prefix: str = 'neo_case_') -> str:
    """要求ごとの作業フォルダ（顧客情報を含むので、使い終わったら remove_case_dir で消す）"""
    return tempfile.mkdtemp(prefix=prefix)


def _make_writable(func, path, _exc):
    """rmtree の onerror: 読み取り専用で消せないファイルは書込可にしてやり直す"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_case_dir(case_dir: Optional[str], attempts: int = 4, wait: float = 0.5) -> Optional[str]:
    """作業フォルダを消す。消せなければ残ったパスを返す（呼び出し側が利用者に知らせる。黙って残さない）。
    Windows では生成直後のファイルを別プロセスが掴んでいて消せないことがあるので、少し待って繰り返す"""
    if not case_dir or not os.path.exists(case_dir):
        return None
    for i in range(max(1, attempts)):
        try:
            shutil.rmtree(case_dir, onerror=_make_writable)
        except OSError:
            pass
        if not os.path.exists(case_dir):
            return None
        time.sleep(wait * (i + 1))
    return case_dir


CASE_TTL_SEC = 2 * 3600.0


def sweep_case_dirs(max_age_sec: float = CASE_TTL_SEC, prefix: str = 'neo_case_') -> int:
    """放置された作業フォルダ（車種フォルダ待ちのまま画面を閉じた等。reading.json など顧客情報を含む）を消す。
    生成中のものは make_neo の timeout（15 分）で終わるので、2 時間より古いものだけ消す。消した数を返す"""
    base = tempfile.gettempdir()
    n = 0
    now = time.time()
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(prefix):
            continue
        p = os.path.join(base, name)
        try:
            if os.path.isdir(p) and now - os.path.getmtime(p) > max_age_sec and remove_case_dir(p, attempts=1) is None:
                n += 1
        except OSError:
            pass
    return n


def write_reading(case_dir: str, reading: dict) -> str:
    path = os.path.join(case_dir, 'reading.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(reading, fh, ensure_ascii=False, indent=1)
    return path


def write_pages(case_dir: str, header: dict, pages: list) -> None:
    """ページ単位の転記（pages/header.json + page_N.json）。make_neo が merge を呼ぶ。
    前回書いた page_*.json（読み直しでページが減った・番号が詰まったときの残り）と検算結果 status.json は先に消す。
    残すと vendor の merge が古い行まで束ねる"""
    d = os.path.join(case_dir, 'pages')
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        if re.fullmatch(r'page_\d+\.json', f) or f == 'status.json':
            os.remove(os.path.join(d, f))
    with open(os.path.join(d, 'header.json'), 'w', encoding='utf-8') as fh:
        json.dump(header, fh, ensure_ascii=False, indent=1)
    for pg in pages:
        n = int(pg.get('page') or 0)
        with open(os.path.join(d, f'page_{n}.json'), 'w', encoding='utf-8') as fh:
            json.dump(pg, fh, ensure_ascii=False, indent=1)


def _safe_name(name: str) -> str:
    """ファイル名に使えない文字を落とす（make_neo は --name をそのままファイル名にする）"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', str(name or '')).strip(' ._')
    return s or 'estimate'


def _kill_tree(proc) -> None:
    """subprocess のプロセス木を止める（timeout 時）。失敗しても例外にしない"""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def make_neo(case_dir: str, name: str, *, no_profile: bool = False, allow_neo_total: bool = False,
             force_draft: bool = False, skip_check: bool = False, timeout: float = 900.0,
             addata_root: Optional[str] = None, neo_check_root: Optional[str] = None) -> MakeResult:
    """vendor の make_neo.py を subprocess で実行する。戻り値の ok は make_neo の終了コード 0 と同じ意味"""
    name = _safe_name(name)
    case_dir = os.path.abspath(case_dir)
    why = vendor.readiness_error()
    if why:
        return MakeResult(False, -1, '', case_dir, name, error=why)
    args = [vendor.python_exe(), vendor.script('make_neo.py'), case_dir, '--name', name]
    if no_profile:
        args.append('--no-profile')
    if allow_neo_total:
        args.append('--allow-neo-total')
    if force_draft:
        args.append('--force-draft')
    if skip_check:
        args.append('--skip-check')
    # 前の回の**出来上がり**を消してから走らせる（同じ作業フォルダで 2 回目を回すことがある:
    # 工賃 0 円での作り直し・検算を外しての作り直し）。残っていると、2 回目が書かなかったファイルを
    # 1 回目のものと知らずに拾い、NEO と確認箇所シートが食い違う組で渡りうる（2026-09-21 バグハント）。
    # reading.json / estimate.json（下書き）は入力なので消さない。
    # 消せなかったもの（Excel で開いている 等）は「前の回のまま」と分かるように印を控え、
    # **中身が変わっていなければ今回の出来上がりとして採らない**（握りつぶすと古いシートを渡す。Codex 指摘 2026-09-21）
    stale = {}
    for _old in (f'{name}.neo', f'{name}.ng.neo', f'{name}_確認箇所.xlsx', f'{name}_確認箇所.csv', 'report.md'):
        _p = os.path.join(case_dir, _old)
        try:
            os.remove(_p)
        except OSError:
            try:
                if os.path.exists(_p):
                    stale[_p] = (os.path.getmtime(_p), os.path.getsize(_p))
            except OSError:
                stale[_p] = None      # 見ることもできない = 触らせない（今回の出来上がりとしては採らない）
    # 子（make_neo.py）は孫（run_case.py 等）を起動する。timeout で子だけ殺すと孫が生き残って作業フォルダを掴む／書き戻すので、
    # プロセスグループごと止める（Windows: taskkill /T、POSIX: killpg。バグハント G6）
    _grp = ({'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)} if os.name == 'nt' else {'start_new_session': True})
    try:
        proc = subprocess.Popen(args, cwd=vendor.VENDOR_ROOT, env=vendor.subprocess_env(addata_root, neo_check_root),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', **_grp)
    except OSError as e:
        return MakeResult(False, -3, '', case_dir, name, error=f'make_neo.py を起動できない: {e}')
    try:
        so, se = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=15)
        except Exception:  # noqa: BLE001
            pass
        return MakeResult(False, -2, '', case_dir, name, error=f'make_neo.py が {int(timeout)} 秒で終わらなかった')
    rc = proc.returncode
    out = (so or '') + (('\n[stderr]\n' + se) if se and rc != 0 else '')
    res = MakeResult(rc == 0, rc, out, case_dir, name)

    def _made(p: str) -> bool:
        """この回が書いたファイルか。消せなかった前の回のものは、中身が変わっていなければ採らない"""
        if not os.path.isfile(p):
            return False
        if p not in stale:
            return True
        was = stale[p]
        if was is None:
            return False          # 触れなかったもの（掴まれている）は採らない
        try:
            return (os.path.getmtime(p), os.path.getsize(p)) != was
        except OSError:
            return False

    neo = os.path.join(case_dir, f'{name}.neo')
    ng = os.path.join(case_dir, f'{name}.ng.neo')
    res.neo_path = neo if (res.ok and _made(neo)) else None
    res.ng_neo_path = ng if _made(ng) else None
    for ext in ('.xlsx', '.csv'):
        r = os.path.join(case_dir, f'{name}_確認箇所{ext}')
        if _made(r):
            res.review_path = r
            break
    rp = os.path.join(case_dir, 'report.md')
    res.report_path = rp if _made(rp) else None
    ep = os.path.join(case_dir, 'estimate.json')
    res.estimate_path = ep if os.path.isfile(ep) else None
    res.match_line = next((l.strip() for l in out.splitlines() if '見積書合計との一致' in l), '')
    res.reasons = [l.strip() for l in out.splitlines()
                   if l.startswith('不合格:') or l.startswith('紙上検算に FAIL') or l.startswith('ページ単位の検算に不合格')
                   or l.startswith('ページの束ね') or l.startswith('下書き生成に失敗') or l.startswith('突合せに失敗')
                   or l.startswith('案件フォルダが無い') or l.startswith('reading.json も estimate.json も無い')]
    if res.ok and not res.neo_path:
        # 終了コード 0 なのに NEO が無いのは想定外。合格扱いにしない（NEO の無い合格を出さない）
        res.ok = False
        res.reasons.append('make_neo は合格を返したが NEO ファイルが無い')
    if res.ok and not res.review_path:
        res.ok = False
        res.reasons.append('確認箇所シートが無い（NEO はシートと組で渡す）')
    return res


def repair_bundle(case_dir: str, reading: Optional[dict] = None) -> Optional[bytes]:
    """人が直して続きをするための一式を zip にする（読み取り・生成が不合格のとき、作業フォルダを消す前に呼ぶ）。
    入れるもの: pages/*.json、reading.json（無ければ merge 結果 reading を書き出す）、report.md / estimate.json /
    reading_check.json / inspect.json（あれば）。NEO は入れない。
    NEO_check の案件フォルダに展開して page_N.json を直し、make_neo.py を回せば続きができる"""
    import io
    import zipfile
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        pd = os.path.join(case_dir, 'pages')
        if os.path.isdir(pd):
            for f in sorted(os.listdir(pd)):
                if f.endswith('.json'):
                    z.write(os.path.join(pd, f), 'pages/' + f)
                    n += 1
        for f in ('reading.json', 'report.md', 'estimate.json', 'reading_check.json', 'inspect.json'):
            p = os.path.join(case_dir, f)
            if os.path.isfile(p):
                z.write(p, f)
                n += 1
        if reading is not None and not os.path.isfile(os.path.join(case_dir, 'reading.json')):
            z.writestr('reading.json', json.dumps(reading, ensure_ascii=False, indent=1))
            n += 1
    return buf.getvalue() if n else None


def read_bytes(path: Optional[str]) -> Optional[bytes]:
    if not path or not os.path.isfile(path):
        return None
    with open(path, 'rb') as fh:
        return fh.read()


def read_text(path: Optional[str]) -> str:
    if not path or not os.path.isfile(path):
        return ''
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read()

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
