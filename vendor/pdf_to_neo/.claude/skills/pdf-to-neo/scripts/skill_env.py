# -*- coding: utf-8 -*-
"""skill_env.py — この PC の環境（ADDATA / コグニセブン / NEO_check / テンプレート NEO）を解決して環境変数に載せる。
各スクリプトは pipeline を import する前に `import skill_env; skill_env.apply()` を呼ぶ。

優先順: 環境変数 → 設定ファイル（%USERPROFILE%\\.claude\\pdf-to-neo.local.json、PC ごと・git 外）→ 自動検出
  ADDATA_ROOT     コグニのデータ（<root>\\COM がある。C:\\Addata が標準。他ドライブや コグニ本体の隣も探す）
  COGNI_BIN       AudaMenu.exe のフルパス（Program Files (x86)\\Audatex\\Auda7\\Bin が標準。レジストリのアンインストール情報も見る）
  NEO_CHECK_ROOT  案件フォルダの置き場（既定 %USERPROFILE%\\Documents\\NEO_check）
  NEO_TEMPLATE    生成の雛形 NEO（既定 claude_neo_pipeline/reference/template.neo。無ければ旧雛形 template_04011103.neo → サンプル見積PDF/04011103.neo）
"""
from __future__ import annotations

import collections
import glob
import json
import os
import stat
import string
import sys
import threading
import time
from typing import Optional

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath: 個人スキル領域のジャンクション経由でも実体の場所
CONFIG = os.path.join(os.path.expanduser('~'), '.claude', 'pdf-to-neo.local.json')
KEYS = ('ADDATA_ROOT', 'COGNI_BIN', 'NEO_CHECK_ROOT', 'NEO_TEMPLATE', 'REPO_ROOT')


def _is_repo(p: str) -> bool:
    return bool(p) and os.path.isfile(os.path.join(p, 'claude_neo_pipeline', 'estimate_to_neo.py'))


def find_repo_root() -> str:
    """リポジトリ root（claude_neo_pipeline/estimate_to_neo.py がある files）。
    環境変数 REPO_ROOT → このファイルの実体から 4 階層上（<repo>/.claude/skills/pdf-to-neo/scripts）→ 設定ファイルの REPO_ROOT → cwd とその親 → __file__ の親を順に遡る"""
    env = os.environ.get('REPO_ROOT') or ''
    if _is_repo(env):
        return os.path.abspath(env)
    cand = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..'))
    if _is_repo(cand):
        return cand
    for start in (os.getcwd(), HERE):  # 今いる場所（新しい展開先）を設定ファイルより優先: 古い展開先が残っていても新しい方を使う
        p = os.path.abspath(start)
        for _ in range(8):
            if _is_repo(p):
                return p
            q = os.path.dirname(p)
            if q == p:
                break
            p = q
    try:
        cfg = json.load(open(CONFIG, encoding='utf-8-sig')) if os.path.exists(CONFIG) else {}
    except Exception:
        cfg = {}
    if _is_repo(cfg.get('REPO_ROOT', '')):
        return os.path.abspath(cfg['REPO_ROOT'])  # 最後の手段（個人スキル領域がコピーで、cwd もリポジトリ外のとき）
    return cand  # 見つからなくても従来の計算値を返す（env_check が NG を出す）


FILES = find_repo_root()


def load_config() -> dict:
    try:
        return json.load(open(CONFIG, encoding='utf-8-sig')) if os.path.exists(CONFIG) else {}
    except Exception:
        return {}


def save_config(cfg: dict) -> str:
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    json.dump(cfg, open(CONFIG, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return CONFIG


def _drives() -> list[str]:
    """走査するドライブ: 固定ローカルドライブ（DRIVE_FIXED）だけ。ネットワーク/リムーバブルは切断中や低速で起動を止めるので、環境変数 ADDATA_SCAN_ALL=1 のときだけ含める"""
    out = []
    scan_all = os.environ.get('ADDATA_SCAN_ALL') == '1'
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        get_type = k32.GetDriveTypeW
    except Exception:
        mask, get_type = None, None
    for i, d in enumerate(string.ascii_uppercase):
        root = f'{d}:\\'
        if mask is not None and not (mask >> i) & 1:
            continue  # 存在しないドライブ文字（isdir で触らない）
        if get_type is not None and not scan_all and get_type(root) != 3:  # 3 = DRIVE_FIXED。ネットワーク/リムーバブルは触らない（切断中はハングし得る）
            continue
        if mask is None and not os.path.isdir(root):
            continue
        out.append(root)
    return out


def is_addata(p: str) -> bool:
    """ADDATA として使える root か: COM に AnVer.DB（データ版）か COM.CAB があり、メーカー別フォルダ（1 文字）が 5 つ以上、そのうち 1 つ以上に <car>01.DB がある"""
    try:
        if not p or not os.path.isdir(os.path.join(p, 'COM')):
            return False
        com = os.path.join(p, 'COM')
        if not (os.path.isfile(os.path.join(com, 'AnVer.DB')) or os.path.isfile(os.path.join(com, 'COM.CAB'))):
            return False
        makers = sorted(d for d in os.listdir(p) if len(d) == 1 and os.path.isdir(os.path.join(p, d)))
        if len(makers) < 5:
            return False
        for m in makers:  # 全メーカーフォルダを走査（順序に依存しない）
            if glob.glob(os.path.join(p, m, '*', '*01.DB')):
                return True
        return False
    except OSError:  # 権限不足・壊れたジャンクション・部分コピーなど: この候補は不採用にして次へ
        return False


def is_addata_partial(p: str) -> bool:
    """「COM ＋ 必要な車種フォルダだけ」の部分コピーとして使えるか: COM に KA06_ALL.DB（車種マスタ）と AnVer.DB か COM.CAB がある。
    is_addata() より緩い（メーカーフォルダ 5 つを要らない）。アプリ（neo-estimate）がブラウザ経由で利用者の PC から
    COM と見積の車種フォルダだけを取り寄せた一時フォルダ（addata_bridge_<id>）がこれ。自動検出では決して採らない"""
    try:
        if not p or not os.path.isdir(os.path.join(p, 'COM')):
            return False
        com = os.path.join(p, 'COM')
        if not os.path.isfile(os.path.join(com, 'KA06_ALL.DB')):
            return False
        return os.path.isfile(os.path.join(com, 'AnVer.DB')) or os.path.isfile(os.path.join(com, 'COM.CAB'))
    except OSError:
        return False


def partial_flag() -> bool:
    """ADDATA_ROOT_PARTIAL=1: 環境変数 ADDATA_ROOT に「COM ＋ 必要な車種フォルダだけ」の部分コピーを渡していると宣言している。
    このときは環境変数以外（設定ファイル・既定パス・自動検出）に決して落とさない（別の Addata で黙って作らない = fail closed）"""
    return (os.environ.get('ADDATA_ROOT_PARTIAL') or '').strip().lower() in ('1', 'true', 'yes')


def is_addata_or_partial(p: str) -> bool:
    """環境変数 ADDATA_ROOT の検証。ADDATA_ROOT_PARTIAL=1 のときは、環境変数 ADDATA_ROOT そのものに限って部分コピーも採る。
    設定ファイルや自動検出の候補は is_addata() のまま（部分コピーを拾って別の版で作らない）。
    旗が無いと部分コピーは弾かれて自動検出に落ちる ＝ 開発機では C:\Addata に化け、Cloud では見つからず失敗する（2026-09-14 Codex 43）"""
    if is_addata(p):
        return True
    env = os.environ.get('ADDATA_ROOT') or ''
    return partial_flag() and bool(env) and os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(env)) and is_addata_partial(p)


def addata_version(p: str) -> str:
    """ADDATA のデータ版（COM\AnVer.DB は XOR 0xff の INI で 'Number=2026/08'）。取れなければ ''。
    PC ごとに版が違うと標準品番・標準指数が変わるので、社内で揃っているかの確認に使う"""
    fp = os.path.join(p, 'COM', 'AnVer.DB')
    try:
        t = bytes(x ^ 0xFF for x in open(fp, 'rb').read()).decode('cp932', 'replace')
    except OSError:
        return ''
    for line in t.splitlines():
        if line.lower().startswith('number='):
            return line.split('=', 1)[1].strip()
    return ''


def addata_version_key(p: str) -> tuple:
    """候補が複数あるときの新しさ: データ版（AnVer.DB の Number）を優先し、無ければ更新日時。
    フォルダごとコピーすると更新日時が当てにならないので、版そのものを先に見る"""
    v = addata_version(p)
    num = 0.0
    if v:
        d = ''.join(ch for ch in v if ch.isdigit())
        if len(d) >= 6:
            num = float(d[:6])  # 202608 のように年月で比較
    for f in ('AnVer.DB', 'COM.CAB'):
        fp = os.path.join(p, 'COM', f)
        if os.path.isfile(fp):
            return (num, os.path.getmtime(fp))
    return (num, 0.0)


def _drives_network() -> list[str]:
    """割り当て済みネットワークドライブ（DRIVE_REMOTE）。固定ドライブで見つからなかったときだけ使う"""
    out = []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        for i, d in enumerate(string.ascii_uppercase):
            if (mask >> i) & 1 and k32.GetDriveTypeW(f'{d}:' + chr(92)) == 4:  # 4 = DRIVE_REMOTE
                out.append(f'{d}:' + chr(92))
    except Exception:  # noqa: BLE001  取れない環境では諦める（固定ドライブの結果を使う）
        pass
    return out


_SKIP_TOP = {'windows', '$recycle.bin', 'system volume information', 'recovery', 'perflogs', 'config.msi', 'msocache'}


def _is_reparse(entry) -> bool:
    """ジャンクション・シンボリックリンク（リパースポイント）か。
    Windows のジャンクションは is_dir(follow_symlinks=False) が True を返すので、属性で判定しないと除外できない"""
    try:
        st = entry.stat(follow_symlinks=False)
        return bool(getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True  # 判定できないものは辿らない（安全側）


def _scan_one_drive(drive: str, deadline: float, out: list) -> None:
    """1 ドライブを幅優先で 4 階層目まで見て Addata で始まるフォルダを out に足す。deadline を過ぎたら打ち切る"""
    queue = collections.deque([(drive, 0)])
    while queue:
        if time.time() > deadline:
            return
        cur, depth = queue.popleft()
        try:
            entries = list(os.scandir(cur))
        except OSError:  # 権限不足・切断されたネットワークドライブ
            continue
        if time.time() > deadline:  # scandir 自体が遅いことがあるので前後で見る
            return
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False) or _is_reparse(e):
                    continue
            except OSError:
                continue
            low = e.name.lower()
            if depth == 0 and low in _SKIP_TOP:
                continue
            if low.startswith('addata'):
                out.append(e.path)
            if depth < 3:  # ドライブ直下から 4 階層目まで（例 C:\Program Files (x86)\Audatex\Auda7\Addata）
                queue.append((e.path, depth + 1))


def _scan_addata_deep(drives: list[str], budget: float) -> list[str]:
    r"""各ドライブを 4 階層目まで走査して `Addata*` を集める。
    ドライブごとに daemon スレッドを立てて**同時に**走らせ、全体の締切まで待つ。
    返らない共有があっても待ち時間は budget +1 秒で頭打ちになる（スレッドは daemon なのでプロセス終了を妨げない）"""
    out: list[str] = []
    if not drives:
        return out
    deadline = time.time() + budget
    jobs = []
    for d in drives:
        got: list[str] = []
        th = threading.Thread(target=_scan_one_drive, args=(d, deadline, got), daemon=True)
        th.start()
        jobs.append((th, got))
    grace = deadline + 1.0  # 猶予は全体で 1 回だけ（ドライブ数ぶん伸びない）
    for th, got in jobs:
        th.join(max(0.0, grace - time.time()))
        out += list(got)
    return out


def _addata_candidates(drives: list[str], shallow: bool = False) -> list[str]:
    """よくある置き方（ドライブ直下・Audatex 配下・2 階層まで）。速い方の探索。
    shallow=True では glob を使わない（ネットワークドライブ用: glob は共有全体を舐めるので遅い PC で待たされる）"""
    cands = []
    for d in drives:
        cands += [os.path.join(d, 'Addata'), os.path.join(d, 'ADDATA'), os.path.join(d, 'Audatex', 'Addata')]
    if shallow:
        return cands
    for d in drives:
        cands += sorted(glob.glob(os.path.join(d, '*', 'Addata'))) + sorted(glob.glob(os.path.join(d, '*', '*', 'Addata')))
    return cands


def _bounded(fn, seconds: float, default):
    """fn() を daemon スレッドで動かし、seconds で打ち切って結果（無ければ default）を返す。
    応答しない共有では os.scandir / open がそのまま返らないので、時間制限はスレッド境界でしか作れない"""
    box = [default]

    def work():
        try:
            box[0] = fn()
        except Exception:  # noqa: BLE001  検出は補助。失敗しても default を返す
            pass
    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(seconds)
    return box[0]


def _valid_addata_bounded(drives: list[str], seconds: float) -> list[str]:
    """ネットワークドライブ用: 「よくある置き方」の候補を **1 候補 1 スレッド** で判定し、全体を指定秒で打ち切る。
    glob も is_addata も内部で scandir するので、切断気味の共有ではスレッドを跨がないと時間制限が効かない。
    候補ごとに分けるのは、同じドライブの先頭候補が応答しなくても後続の候補（例 Z:\Audatex\Addata）を見逃さないため"""
    if not drives:
        return []
    end = time.time() + seconds
    # 候補を並べる処理自体も時間制限の中で行う（将来 glob を使う形に変えても、ここで固まらないように）
    cands = _bounded(lambda: _addata_candidates(drives, shallow=True), max(0.0, end - time.time()), [])
    jobs = []
    for p in cands:
        box: list[str] = []

        def work(q=p, out=box):
            try:
                if is_addata(q):
                    out.append(q)
            except OSError:
                pass
        th = threading.Thread(target=work, daemon=True)
        th.start()
        jobs.append((th, box))
    got: list[str] = []
    for th, box in jobs:
        th.join(max(0.0, end - time.time()))
        got += list(box)
    return got


def find_addata(cogni_bin: str = '', budget: Optional[float] = None) -> str:
    r"""ADDATA の root。コグニ本体の隣・固定ドライブのよくある置き方・（あれば）ネットワークドライブの
    よくある置き方を **同じ土俵に集めて**、is_addata() を通った候補のうち
    **データ版が最新（COM\AnVer.DB の Number、無ければ更新日時）** を選ぶ。
    古い C:\Addata が残る PC で、共有サーバの新しいデータを取り違えないため。
    それでも 1 つも無いときだけ、4 階層・`Addata*` 名まで広げて探し直す（時間制限つき。ADDATA_SCAN_SECONDS）"""
    head = []
    if cogni_bin:  # コグニ本体の隣（同じフォルダ・親・同じドライブ直下）
        base = os.path.dirname(os.path.dirname(cogni_bin))  # ...\Auda7
        drive = os.path.splitdrive(cogni_bin)[0] + os.sep
        head = [os.path.join(base, 'Addata'), os.path.join(os.path.dirname(base), 'Addata'), os.path.join(drive, 'Addata'), os.path.join(drive, 'ADDATA')]

    def pick(cands: list[str], prevalidated: Optional[set] = None, stop: float = 0.0) -> str:
        """候補から ADDATA を 1 つ選ぶ。判定も版の読み取りも**候補ごとに**時間制限をかける。
        応答しない候補（切断気味の共有）はその候補だけ諦め、他の有効な候補まで巻き添えにしない"""
        seen, valid = set(), []
        for p in cands:
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            if stop and time.time() > stop:
                break  # 時間切れ: ここまでで見つかった中から選ぶ（応答しない候補が並んでいても線形に伸びない）
            if (prevalidated and key in prevalidated) or _bounded(lambda q=p: is_addata(q), 3.0, False):
                valid.append(p)
        if not valid:
            return ''
        # 版の読み取りも締切の中で。締切を過ぎたぶんは読まずに最下位キーにする（候補数ぶん伸びないように）
        keys = {p: ((-1.0, 0.0) if (stop and time.time() > stop) else _bounded(lambda q=p: addata_version_key(q), 3.0, (-1.0, 0.0)))
                for p in valid}
        valid.sort(key=lambda p: (keys[p], -cands.index(p)), reverse=True)  # 最新のデータ版、同点なら候補順（コグニ隣接が先）
        return valid[0]
    if budget is None:  # 引数優先（同じプロセスで同時に呼んでも互いに影響しない）
        try:  # 社内配布で '30s' のような値が入っていても落ちない
            budget = float(os.environ.get('ADDATA_SCAN_SECONDS') or 20)
        except ValueError:
            budget = 20.0
    budget = max(1.0, float(budget))  # 0 や負でも「よくある置き方」は必ず見る
    deadline = time.time() + budget  # 全体で 1 本の締切（各段はここからの残り時間だけ使う）

    def left(cap: float) -> float:
        return max(0.0, min(cap, deadline - time.time()))
    fixed, net = _drives(), _drives_network()
    # ネットワークの「よくある置き方」は **固定ドライブの候補集めと並行に** 調べる。
    # 遅い共有がある PC でも、待ち時間は「固定ドライブぶん」と「ネットワークの上限 3 秒」の長い方で済む
    net_box: list = []
    net_th = None
    if net:
        def _probe_net():
            net_box.extend(_valid_addata_bounded(net, left(3.0)))
        net_th = threading.Thread(target=_probe_net, daemon=True)
        net_th.start()
    cands_fixed = head + _addata_candidates(fixed)
    if net_th is not None:
        net_th.join(max(0.0, left(3.0)) + 0.5)
    net_ok = list(net_box)
    # 候補にネットワークが混じると、is_addata だけでなく版の読み取り（AnVer.DB の open/getmtime）も返らないことがある。
    # 選定は候補ごとに時間制限をかける（下の pick）
    cands1 = cands_fixed + net_ok
    pre = {os.path.normcase(os.path.abspath(p)) for p in net_ok}
    # 締切を使い切っていても、固定ドライブの候補（すぐ返る）は必ず見る。ネットワーク探索に予算を取られて
    # ローカルの ADDATA を 1 つも確認しないまま終わるのを防ぐ
    got = pick(cands1, pre, max(deadline, time.time() + 3.0))
    # 「よくある置き方」で見つかったらそこで決める（全 PC が毎回 4 階層走査すると数十秒かかるため）。
    # 古い C:\Addata が残ったまま本物を深い場所に置いている PC では、それだと古い版を選んでしまう。
    # そのため ADDATA_SCAN_DEEP=1 で深い探索も必ず行い、データ版の新しい方を選べるようにしてある
    # （env_check はこの経路で全候補を調べ、選んだものより新しい ADDATA があれば警告する）
    if got and os.environ.get('ADDATA_SCAN_DEEP', '') != '1':
        return got
    if got:
        deep_all = _scan_addata_deep(fixed + net, left(budget))
        best = pick([got] + head + deep_all, None, time.time() + left(10.0) + 3.0)
        return best or got
    deep = _scan_addata_deep(fixed + net, left(budget))
    # 深い探索で拾った候補の検証（is_addata / 版の読み取り）もネットワーク越しだと返らないことがあるので、時間制限の中で行う
    return pick(head + deep, None, time.time() + left(10.0) + 3.0)


def find_addata_all(budget: float = 20.0) -> list:
    r"""この PC で見つかる ADDATA を「データ版が新しい順」に (パス, 版) で返す。
    設定の点検用（env_check）。深い探索まで行うので時間がかかる"""
    fixed, net = _drives(), _drives_network()
    deadline = time.time() + max(1.0, float(budget))
    out, seen, keys = [], set(), {}

    def _judge(cands: list, until: float) -> None:
        """候補を ADDATA かどうか見て out に足す（1 件ずつ時間制限つき）"""
        for c in cands:
            key = os.path.normcase(os.path.abspath(c))
            if key in seen:
                continue
            seen.add(key)
            if time.time() > until:
                break  # 締切を過ぎたら打ち切る（応答しない候補が並んでいても呼び出し全体が返らなくならない）
            # 1 回ごとに残り時間を計り直す。使い回すと 1 候補で 3 倍の時間を使い、
            # 応答しないネットワークドライブがあるときに予算を超える
            def _per():
                return min(3.0, max(0.5, until - time.time()))
            if not _bounded(lambda z=c: is_addata(z), _per(), False):
                continue
            if time.time() > until:
                break
            # 版は 1 度だけ読み、並べ替えにも使い回す（同じ候補を 2 度読まない）
            keys[key] = _bounded(lambda z=c: addata_version_key(z), _per(), (-1.0, 0.0))
            out.append((c, _bounded(lambda z=c: addata_version(z), _per(), '')))

    # よくある置き方は先に見る。深い探索を先に走らせると予算を使い切って、
    # 目の前の C:\Addata すら判定できずに空を返してしまう（2026-09-11 に実測）
    _judge(_addata_candidates(fixed) + _addata_candidates(net, shallow=True), deadline)
    left = max(0.0, deadline - time.time())
    deep = _scan_addata_deep(fixed + net, left * 0.7)  # 深い探索は残り予算の 7 割まで（判定の時間を残す）
    _judge(deep, min(deadline, time.time() + max(2.0, left * 0.3)))  # 期限は元の予算を超えない
    out.sort(key=lambda r: keys.get(os.path.normcase(os.path.abspath(r[0])), (-1.0, 0.0)), reverse=True)
    return out


def find_cogni() -> str:
    """コグニセブン本体（AudaMenu.exe）。既定パス → 全ドライブの Program Files → レジストリ（アンインストール情報の InstallLocation / DisplayIcon）"""
    cands = []
    for d in _drives():
        for pf in ('Program Files (x86)', 'Program Files'):
            cands.append(os.path.join(d, pf, 'Audatex', 'Auda7', 'Bin', 'AudaMenu.exe'))
        cands.append(os.path.join(d, 'Audatex', 'Auda7', 'Bin', 'AudaMenu.exe'))
    for p in cands:
        if os.path.isfile(p):
            return p
    try:
        import winreg
        for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
                          (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')):
            try:
                root = winreg.OpenKey(hive, key)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    sub = winreg.OpenKey(root, winreg.EnumKey(root, i))
                    name = ''
                    try:
                        name = str(winreg.QueryValueEx(sub, 'DisplayName')[0])
                    except OSError:
                        pass
                    if 'Audatex' in name or 'コグニ' in name or 'Auda' in name:
                        for val in ('InstallLocation', 'DisplayIcon'):
                            try:
                                v = os.path.expandvars(str(winreg.QueryValueEx(sub, val)[0]).strip('"'))  # REG_EXPAND_SZ の %ProgramFiles(x86)% 等を展開
                            except OSError:
                                continue
                            base = v if os.path.isdir(v) else os.path.dirname(v)
                            for cand in (os.path.join(base, 'Bin', 'AudaMenu.exe'), os.path.join(base, 'AudaMenu.exe'), os.path.join(base, 'Auda7', 'Bin', 'AudaMenu.exe')):
                                if os.path.isfile(cand):
                                    return cand
                except OSError:
                    continue
    except ImportError:
        pass
    for d in _drives():  # 最後の手段: Program Files 配下を 3 階層まで
        for p in glob.glob(os.path.join(d, 'Program Files*', '*', '*', 'Bin', 'AudaMenu.exe')):
            return p
    return ''


def find_template() -> str:
    """雛形 NEO: reference/template.neo（生成器で作った顧客情報の無い雛形。配布用）→ reference/template_04011103.neo → サンプル見積PDF/04011103.neo"""
    for p in (os.path.join(FILES, 'claude_neo_pipeline', 'reference', 'template.neo'), os.path.join(FILES, 'claude_neo_pipeline', 'reference', 'template_04011103.neo'),
              os.path.join(FILES, 'サンプル見積PDF', '04011103.neo')):
        if os.path.isfile(p):
            return p
    return ''


def resolve(save: bool = False) -> dict:
    """環境変数 → 設定ファイル → 自動検出 の順に 4 項目を解決して返す（見つからない項目は ''）。save=True で設定ファイルに保存"""
    cfg = load_config()
    out = {}

    def pick(key, valid, detect):
        """環境変数 → 設定ファイル → 自動検出 の順に、個別に検証して最初に有効なものを採る（無効な環境変数が有効な設定を潰さない）。
        検証は 3 秒で打ち切る: 古い設定が切断済みの共有を指していると os.path.isdir / is_addata が返らず、
        自動検出まで進めないため（打ち切ったら「無効」とみなして次の手段へ）"""
        for v in (os.environ.get(key) or '', cfg.get(key) or ''):
            if v and _bounded(lambda q=v: valid(q), 3.0, False):
                return v
        return detect() or ''
    out['COGNI_BIN'] = pick('COGNI_BIN', os.path.isfile, find_cogni)
    if partial_flag():
        # 部分 Addata を渡すと宣言している: 環境変数が無効なら空にする（設定ファイル・自動検出に落ちて別の Addata で作らない）
        env_root = os.environ.get('ADDATA_ROOT') or ''
        out['ADDATA_ROOT'] = env_root if (env_root and _bounded(lambda: is_addata_or_partial(env_root), 3.0, False)) else ''
    else:
        out['ADDATA_ROOT'] = pick('ADDATA_ROOT', is_addata, lambda: find_addata(out['COGNI_BIN']))  # COM だけ残った古いコピー等は不採用
    out['NEO_CHECK_ROOT'] = os.environ.get('NEO_CHECK_ROOT') or cfg.get('NEO_CHECK_ROOT') or os.path.join(os.path.expanduser('~'), 'Documents', 'NEO_check')
    # 雛形 NEO はリポジトリ同梱のものを毎回その場で解決する（設定ファイルには保存しない: 別 PC やフォルダ移動で古い絶対パスが残るのを防ぐ）。環境変数だけ上書き可
    out['NEO_TEMPLATE'] = os.environ.get('NEO_TEMPLATE') or ''
    if not (out['NEO_TEMPLATE'] and os.path.isfile(out['NEO_TEMPLATE'])):
        out['NEO_TEMPLATE'] = find_template()
    out['REPO_ROOT'] = FILES
    if save:
        save_config({k: v for k, v in out.items() if v and k != 'NEO_TEMPLATE'})  # REPO_ROOT も保存（ジャンクション経由の実行で root を引けるように）
    return out


def flag(v, name: str = '真偽値', default: bool = False) -> bool:
    """人が書いた JSON の真偽値欄を厳密に読む。判断できない値は ValueError。
    `if d.get('generic'):` だと文字列 "false" が真になり、実在車種を汎用車種扱いにしてしまう"""
    import unicodedata
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


def normalise_flags(rows, keys=('manual', 'reserve'), where: str = 'rows[]') -> None:
    """明細行の真偽値欄をその場で正規化する（入口で 1 回だけ呼ぶ）。
    箇所ごとに `flag()` を書くと必ず取りこぼすので、読み込んだ直後にこれを通す。
    `recycle` は真偽値ではなくリサイクル部品の情報（dict）なので既定では触らない"""
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        for k in keys:
            if k in r:
                r[k] = flag(r[k], f'{where}.{k}')


def use_utf8_io() -> None:
    """標準出力・標準エラーを UTF-8 にする。
    Windows の既定は cp932 で、出力をパイプ（Claude Code のツール出力・ファイル）に流すと日本語が cp932 になり、
    PC によって文字化けや UnicodeEncodeError の原因になる。PYTHONIOENCODING を付け忘れても同じ結果になるようにする"""
    for st in (sys.stdout, sys.stderr):
        try:
            if getattr(st, 'encoding', '').lower().replace('-', '') not in ('utf8', 'utf8sig'):
                st.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):  # 差し替えられた stdout など
            pass


def apply() -> dict:
    """解決した値を環境変数に載せる（既に環境変数があるものは触らない）。pipeline（resolver の ADDATA_ROOT、NeoBuilder の NEO_TEMPLATE、tests の NEO_CHECK_ROOT）が読む"""
    use_utf8_io()
    out = resolve()
    for k, v in out.items():
        if v:
            os.environ[k] = v  # resolve() は有効な環境変数を最優先しつつ、無効なパス（別 PC の古い値など）は検出結果に置き換える。検証済みの値を必ず載せる
        elif k in os.environ:
            os.environ.pop(k, None)  # 解決できなかったキーは無効な既存値を残さない（pipeline が分かりにくい欠落エラーで落ちるのを防ぐ）
    if FILES not in sys.path:
        sys.path.insert(0, FILES)
    return out


if __name__ == '__main__':
    for k, v in resolve().items():
        print(f'{k:<15} {v or "(見つからない)"}')
