# -*- coding: utf-8 -*-
"""Addata の場所設定（画面から設定して URL に残す仕組み）の回帰テスト。

本番はクラウドで動いていて利用者のPCが見えないので、
「設定を URL に残して持ち歩ける」ことが実用上いちばん効く。
壊すと Addata が読めなくなり、部品コードが入らない見積が出る。
"""
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

R = os.environ.get('XROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, R)
os.chdir(R)
sys.stdout.reconfigure(encoding='utf-8')

import addata_settings as A  # noqa: E402
import app  # noqa: E402

FAIL = []


def chk(cond, msg):
    if not cond:
        FAIL.append(msg)


def chk_order(text, first, second, msg):
    """first が second より先に出てくること。片方が無い場合も理由を出す。"""
    if first not in text:
        FAIL.append(f'{msg}（{first} が見当たらない）')
    elif second not in text:
        FAIL.append(f'{msg}（{second} が見当たらない）')
    elif text.index(first) >= text.index(second):
        FAIL.append(msg)


# ── 1. 共有リンクを「中身が落ちてくる」形に直す ──────────────────────
chk(A.normalize_share_url('https://x.sharepoint.com/:u:/g/p/Ab?e=1').endswith('download=1'),
    '1: SharePoint の共有リンクに download=1 が付かない')
chk('download=1' in A.normalize_share_url('https://1drv.ms/u/s!AbC'),
    '1b: OneDrive の共有リンクに download=1 が付かない')
chk(A.normalize_share_url('https://drive.google.com/file/d/ABC123/view')
    == 'https://drive.google.com/uc?export=download&id=ABC123',
    '1c: Google ドライブの共有リンクがダウンロード用にならない')
chk(A.normalize_share_url('https://example.com/Addata.zip')
    == 'https://example.com/Addata.zip', '1d: 普通のURLを書き換えている')
# 共有ドライブのリンクは resourcekey が無いと中身が返らず、許可を求める
# HTML が返ってくる（＝ZIPでないとして弾かれ、設定したのに Addata が読めない）
_gd = A.normalize_share_url(
    'https://drive.google.com/file/d/ABC123/view?usp=sharing&resourcekey=0-xYz')
chk('id=ABC123' in _gd and 'resourcekey=0-xYz' in _gd,
    f'1g: 共有ドライブの resourcekey を落としている: {_gd}')
chk(A.normalize_share_url('') == '', '1e: 空のURLで落ちる')
chk(A.normalize_share_url(None) == '', '1f: None で落ちる')

# ── 2. 受け付けてよい URL / いけない URL ─────────────────────────────
for _u in ('', 'ftp://x/a.zip', 'file:///C:/Addata.zip', 'not a url', 'https://'):
    chk(not A.validate_url(_u)[0], f'2: {_u!r} を受け付けてしまう')
chk(A.validate_url('https://example.com/Addata.zip')[0],
    '2b: 普通の https URL を弾いている')
# 合言葉入りのURL。この設定はURLに残して共有できる形なので、
# 受け付けるとブックマーク・履歴・ログに合言葉がそのまま残る。
# `@` の前を本当の宛先だと読み違えさせる細工にも使われる。
for _u in ('https://user:pass@example.com/Addata.zip',
           'https://user@example.com/Addata.zip',
           'http://example.com@evil.example.net/a.zip',
           'https://:pw@example.com/a.zip'):
    _ok2, _why2 = A.validate_url(_u)
    chk(not _ok2, f'2c: 合言葉入り／宛先をまぎらわす {_u!r} を受け付けてしまう')
    chk(not _ok2 and ('ユーザー名' in _why2 or 'パスワード' in _why2),
        f'2d: {_u!r} を弾いた理由が合言葉の話になっていない: {_why2}')
# 転送先にも同じ基準がかかること
try:
    A._GuardedRedirect().redirect_request(
        urllib.request.Request('https://example.com/a.zip'),
        None, 302, 'Found', {}, 'https://user:pass@example.com/a.zip')
    chk(False, '2e: 合言葉入りのURLへの転送を通してしまう')
except urllib.error.HTTPError:
    pass
except Exception as _e:      # noqa: BLE001
    chk(False, f'2e: 想定外の例外 {type(_e).__name__}: {_e}')

# ── 3. 社内・自分自身を指すアドレスを弾く（踏み台にされない） ─────────
# このアプリの URL は誰でも開ける。?addata_url=… を仕込んだリンクを踏ませれば
# サーバに任意の場所を叩かせられるので、外から到達できない宛先は弾く。
_was = os.environ.pop('ADDATA_ALLOW_INTERNAL_URL', None)
try:
    for _u in ('http://127.0.0.1:8080/a.zip', 'http://localhost/a.zip',
               'http://10.0.0.5/a.zip', 'http://192.168.0.2/a.zip',
               'http://172.16.0.1/a.zip', 'http://169.254.169.254/latest/meta-data/',
               'http://[::1]/a.zip', 'http://0.0.0.0/a.zip',
               'http://api.localhost/a.zip',
               # 名前だけで社内と分かるもの（クラウドの内部メタデータ等）
               'http://metadata.google.internal/computeMetadata/v1/',
               'http://metadata.goog/x.zip', 'http://srv.internal/a.zip',
               'http://nas.local/a.zip', 'http://x.home.arpa/a.zip',
               'http://LOCALHOST./a.zip',
               # IPv6 に包んだ内側のアドレス
               'http://[::ffff:127.0.0.1]/a.zip', 'http://[::ffff:10.0.0.1]/a.zip',
               # インターネットから届かないが private でも reserved でもない範囲
               'http://100.64.0.1/a.zip', 'http://100.127.255.254/a.zip',
               'http://192.0.2.1/a.zip', 'http://198.18.0.1/a.zip',
               'http://203.0.113.9/a.zip', 'http://240.0.0.1/a.zip',
               'http://224.0.0.1/a.zip', 'http://255.255.255.255/a.zip',
               'http://[2001:db8::1]/a.zip', 'http://[fc00::1]/a.zip',
               # 6to4（2002::/16）に包んだ内側
               'http://[2002:7f00:0001::]/a.zip', 'http://[2002:0a00:0001::]/a.zip'):
        ok, why = A.validate_url(_u)
        chk(not ok, f'3: 社内向けの {_u!r} を受け付けてしまう')
    chk(A.validate_url('https://example.com/a.zip')[0],
        '3b: 外のURLまで弾いている')
    chk(A.validate_url('https://data.local-files.example.com/a.zip')[0],
        '3b2: 名前に local を含むだけの外部URLまで弾いている')
    # 本物の公開アドレスまで巻き添えにしていないこと
    for _u in ('http://8.8.8.8/a.zip', 'http://93.184.216.34/a.zip',
               'http://[2606:4700:4700::1111]/a.zip'):
        chk(A.validate_url(_u)[0], f'3b3: 公開アドレス {_u!r} を弾いている')
    # 環境変数で明示的に開けたときだけ通す（社内サーバ運用・検証用）
    os.environ['ADDATA_ALLOW_INTERNAL_URL'] = '1'
    chk(A.validate_url('http://127.0.0.1:8080/a.zip')[0],
        '3c: 明示的に開けても通らない')
finally:
    os.environ.pop('ADDATA_ALLOW_INTERNAL_URL', None)
    if _was is not None:
        os.environ['ADDATA_ALLOW_INTERNAL_URL'] = _was

# IPv6 に包まれた内側のアドレスについて。
#   6to4（2002::/16）は is_global が True になる ＝ **包みをほどかないと通る**。
#     上の 3 の `2002:7f00:0001::`（内側 127.0.0.1）がそれを見ている。
#   Teredo（2001::/32）は 2001::/23 に入っていて is_private が True なので、
#     ほどかなくても止まる。ほどく側は念のための二重化で、ここでは
#     「止まること」だけを固定しておく（Python 側の分類が変わっても気づける）。
import ipaddress as _ipa  # noqa: E402
_was_t = os.environ.pop('ADDATA_ALLOW_INTERNAL_URL', None)
try:
    for _t_addr in ('2001:0:4136:e378:8000:63bf:3fff:fdd2',
                    '2001:0:d5c7:b3f3:34f5:6cf2:effe:fffe'):
        chk(A._is_internal_ip(_t_addr), f'3f: Teredo {_t_addr} を通している')
    # 包みをほどく処理そのものも見ておく（6to4 / IPv4-mapped / Teredo）
    chk(A._is_internal_ip('2002:0a00:0001::'), '3f2: 6to4 の内側 10.0.0.1 を通している')
    chk(A._is_internal_ip('::ffff:169.254.169.254'),
        '3f3: IPv4-mapped の内側 169.254.169.254 を通している')
    chk(not A._is_internal_ip('2002:5db8:d822::'),
        '3f4: 6to4 の内側が公開アドレス（93.184.216.34）でも弾いている')
finally:
    if _was_t is not None:
        os.environ['ADDATA_ALLOW_INTERNAL_URL'] = _was_t

# ── 3b. 転送先も同じ基準で見る（リダイレクトを使った踏み台） ────────────
# urllib は 302 を黙って追う。入口が外のアドレスでも、そこから社内へ
# 飛ばされれば入口の検査を抜けてしまう。
_was2 = os.environ.pop('ADDATA_ALLOW_INTERNAL_URL', None)
try:
    _h = A._GuardedRedirect()
    _req = urllib.request.Request('https://example.com/Addata.zip')
    for _u in ('http://169.254.169.254/latest/meta-data/', 'http://127.0.0.1/a.zip',
               'http://10.1.2.3/a.zip', 'http://192.168.1.1/a.zip',
               'http://metadata.google.internal/x'):
        try:
            _h.redirect_request(_req, None, 302, 'Found', {}, _u)
            chk(False, f'3b: 社内アドレスへの転送 {_u!r} を通してしまう')
        except urllib.error.HTTPError:
            pass
        except Exception as _e:      # noqa: BLE001
            chk(False, f'3b: 転送の検査で想定外の例外 {type(_e).__name__}: {_e}')
    try:
        _r = _h.redirect_request(_req, None, 302, 'Found', {}, 'https://cdn.example.com/a.zip')
        chk(_r is not None, '3c2: 外のアドレスへの転送まで止めている')
    except Exception as _e:          # noqa: BLE001
        chk(False, f'3c2: 外への転送で例外 {type(_e).__name__}: {_e}')
finally:
    if _was2 is not None:
        os.environ['ADDATA_ALLOW_INTERNAL_URL'] = _was2

# ── 3d. 名前が社内アドレスに解決される場合（DNS を使った抜け道） ────────
# 名前の見た目だけでは足りない。evil.example.com が 127.0.0.1 を返すことが
# ある。**繋ぐ直前に、引いた結果そのものを見て**、そのアドレスに繋ぐ。
_was3 = os.environ.pop('ADDATA_ALLOW_INTERNAL_URL', None)
_orig_gai = A.socket.getaddrinfo
try:
    for _bad_ip in ('127.0.0.1', '169.254.169.254', '10.1.2.3'):
        def _fake(host, port, *a, _ip=_bad_ip, **kw):
            return [(A.socket.AF_INET, A.socket.SOCK_STREAM, 6, '', (_ip, port or 80))]
        A.socket.getaddrinfo = _fake
        try:
            A._safe_connect('evil.example.com', 80, 5, None)
            chk(False, f'3d: {_bad_ip} に解決される名前に繋いでしまう')
        except A.BlockedAddress:
            pass
        except Exception as _e:      # noqa: BLE001
            chk(False, f'3d: 想定外の例外 {type(_e).__name__}: {_e}')
        # 取得の入口（download_zip）まで通しても止まること＝検査が繋がっている
        _b, _w = A.download_zip('http://evil.example.com/Addata.zip')
        chk(_b is None and ('社内' in _w or '指定できません' in _w),
            f'3e: download_zip が {_bad_ip} に解決される名前を止めない: {_w}')
finally:
    A.socket.getaddrinfo = _orig_gai
    if _was3 is not None:
        os.environ['ADDATA_ALLOW_INTERNAL_URL'] = _was3

# ── 4. 実際に配って取り込む（取得 → 展開 → 使える形か） ───────────────
work = tempfile.mkdtemp(prefix='reg_addata_')
_made = []
_dl_made = []
import inspect as inspect0  # noqa: E402
try:
    src = os.path.join(work, 'Addata')
    for d in ('D', 'COM'):
        os.makedirs(os.path.join(src, d), exist_ok=True)
    # 「1文字フォルダ ／ 車種コード ／ *.DB」の形にする（展開側がこれを見る）
    os.makedirs(os.path.join(src, 'D', 'D89'), exist_ok=True)
    for _fn in ('D8912.DB', 'D8915.DB'):
        with open(os.path.join(src, 'D', 'D89', _fn), 'wb') as f:
            f.write(b'x' * 64)
    zp = os.path.join(work, 'Addata.zip')
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as zf:
        for base, _d, files in os.walk(src):
            for fn in files:
                full = os.path.join(base, fn)
                zf.write(full, os.path.relpath(full, work))
    with open(os.path.join(work, 'notzip.bin'), 'wb') as f:
        f.write(b'this is not a zip')

    served = {'stream_bytes': 0}

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        # HTTP/1.0 = 接続を閉じて本文の終わりを示す形。Content-Length を
        # 付けずに配れるので、「大きさを先に知れない相手」を再現できる。
        protocol_version = 'HTTP/1.0'

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=work, **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith('/redir/'):
                # 毎回 0.8 秒待ってから次へ飛ばす。1回ずつは timeout= に
                # 引っかからないので、全体の残り時間で切れるかを見る。
                try:
                    n = int(self.path.rsplit('/', 1)[1])
                except ValueError:
                    n = 0
                time.sleep(0.8)
                if n >= 9:
                    return super().do_GET()
                self.send_response(302)
                self.send_header('Location', '/redir/%d' % (n + 1))
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/drip-head':
                # 見出しを少しずつ送る。本文に入らないので、こちらの
                # 時間の検査は回らない。口を閉じる見張り役だけが頼り。
                try:
                    self.wfile.write(bytes.fromhex('485454502f312e3020323030204f4b0d0a'))
                    self.wfile.flush()
                    for i in range(200):
                        self.wfile.write(('X-Pad-%d: y' % i).encode() + bytes.fromhex('0d0a'))
                        self.wfile.flush()
                        time.sleep(0.1)
                    self.wfile.write(bytes.fromhex('0d0a'))
                except OSError:
                    pass
                self.close_connection = True
                return
            if self.path == '/drip-zip':
                # PK を送ってから、少しずつ垂れ流す。届き続けるので
                # timeout= では切れない。全体の時間で切れるかを見る。
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.end_headers()
                try:
                    self.wfile.write(bytes.fromhex('504b0304'))
                    for _ in range(400):
                        self.wfile.write(b'x' * 16)
                        time.sleep(0.05)
                except OSError:
                    pass
                return
            if self.path != '/stream-notzip':
                return super().do_GET()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()          # Content-Length を付けない
            served['stream_bytes'] = 0
            try:
                self.wfile.write(b'<html>')
                served['stream_bytes'] += 6
                for _ in range(80):     # 80 x 64KB = 5MB
                    self.wfile.write(b'A' * (64 * 1024))
                    served['stream_bytes'] += 64 * 1024
            except OSError:
                pass                    # 相手が途中で切った（＝早く切れている）

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(('127.0.0.1', 0), _Quiet)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    os.environ['ADDATA_ALLOW_INTERNAL_URL'] = '1'   # 検証中だけ自分自身を許す
    try:
        base_url = 'http://127.0.0.1:%d/' % port
        url = base_url + 'Addata.zip'
        blob, why = A.download_zip(url)
        chk(blob is not None, f'4: 取得できない: {why}')
        # 中身はメモリに抱えず一時ファイルへ流すこと（上限300MB × 同時利用で
        # プロセスごと落ちる）。返ってくるのは「置き場所」。
        chk(isinstance(blob, str) and os.path.isfile(blob),
            f'4p: 取得の結果が一時ファイルの場所になっていない: {type(blob).__name__}')
        if isinstance(blob, str):
            _dl_made.append(blob)       # このテストで作ったぶんは後で消す
        # 途中で諦めたぶんは残さない
        _before = len([f for f in os.listdir(tempfile.gettempdir())
                       if f.startswith('addata_dl_')])
        A.download_zip(base_url + 'notzip.bin')
        A.download_zip(base_url + 'nothere.zip')
        _after = len([f for f in os.listdir(tempfile.gettempdir())
                      if f.startswith('addata_dl_')])
        chk(_after <= _before, f'4q: 失敗した取得の一時ファイルが残っている（{_before}→{_after}）')
        # 落ちかけの取り残しも掃除で拾えること（ふだんは自分で消している）
        _fsw = inspect0.getsource(app._sweep_stale_addata_dirs)
        chk('addata_dl_' in _fsw, '4s: 取り残しの ZIP が掃除の対象に入っていない')
        if blob:
            shutil.rmtree(A.cache_dir_for(url), ignore_errors=True)
            # ── 1回目の取り込み
            d1 = A.new_payload_dir(url)
            _made.append(d1)
            os.makedirs(d1, exist_ok=True)
            r1, why2 = app.extract_addata_zip(blob, d1)
            chk(bool(r1), f'4b: 展開できない: {why2}')
            chk(bool(r1) and app._addata_is_valid(r1),
                '4c: 展開したものが Addata として妥当でない')
            if r1:
                A.remember_root(url, d1, r1)
                chk(A.cached_root(url, app._addata_is_valid) == r1,
                    '4d: 一度取り込んだものを使い回せない（毎回落とし直す）')

                # ── 2回目の取り込み（別の利用者が同じURLを開いた想定）
                # **1回目の展開先を消してはいけない。**消すと、生成の最中の
                # セッションから Addata が消えて静かにモードAに落ちる。
                d2 = A.new_payload_dir(url)
                _made.append(d2)
                chk(d1 != d2, '4h: 取り込みごとに別の場所を使っていない')
                os.makedirs(d2, exist_ok=True)
                r2, _w = app.extract_addata_zip(blob, d2)
                A.remember_root(url, d2, r2)
                chk(os.path.isdir(r1),
                    '4i: 2回目の取り込みが1回目の展開先を消している（使用中でも消える）')
                chk(A.cached_root(url, app._addata_is_valid) == r2,
                    '4j: 目印が新しい取り込みを指していない')

                # ── 古さの判定は「取り込んだ時刻」で行う。
                # 「最後に使った時刻」（フォルダの更新時刻）と混ぜると、
                # 使い続けているだけで永久に取り直さなくなる。
                _mk = os.path.join(A.cache_dir_for(url), A.MARKER)
                _m = A.read_marker(url)
                _m['fetched'] = time.time() - A.DOWNLOAD_CACHE_SEC - 10
                with open(_mk, 'w', encoding='utf-8') as f:
                    json.dump(_m, f)
                A._touch(A.cache_dir_for(url))      # 使用中の印は新しい
                A._touch(d2)
                chk(A.cached_root(url, app._addata_is_valid) == '',
                    '4k: 取り込んでから古くなったのに落とし直さない')

        # ZIP でないもの・存在しないもの・大きすぎるものを弾く
        chk(A.download_zip(base_url + 'notzip.bin')[0] is None,
            '4e: ZIP でないものを受け付けてしまう')
        chk(A.download_zip(base_url + 'nothere.zip')[0] is None,
            '4f: 存在しない URL を受け付けてしまう')
        _lim = A.DOWNLOAD_MAX_BYTES
        A.DOWNLOAD_MAX_BYTES = 10
        try:
            b, w = A.download_zip(url)
            chk(b is None and '大き' in w, '4g: 上限を超えるものを弾いていない')
        finally:
            A.DOWNLOAD_MAX_BYTES = _lim
        # ZIP でないものは**最初の一口**で切ること。
        # Content-Length を付けない相手だと大きさを先に知れないので、
        # 最後まで読んでから捨てていると、仕込まれたURLひとつで
        # 上限いっぱい（300MB）を読まされる。
        b, w = A.download_zip(base_url + 'stream-notzip')
        chk(b is None and 'ZIP ではありません' in w,
            f'4l: Content-Length の無い非ZIPを ZIP 判定で切っていない（{w[:60]}）')
        chk(served['stream_bytes'] <= 2 * 1024 * 1024,
            '4m: ZIP でないと分かった後も読み続けている'
            f'（{served["stream_bytes"]:,} バイト配ってしまった / 全部で5MB）')
        # 少しずつ送り続ける相手を、全体の時間で切ること。
        # timeout= は「何も届かない時間」の上限なので、これでは切れない。
        _tmo = A.DOWNLOAD_TIMEOUT_SEC
        A.DOWNLOAD_TIMEOUT_SEC = 2
        _t0 = time.monotonic()
        try:
            b2, w2 = A.download_zip(base_url + 'drip-zip')
        finally:
            A.DOWNLOAD_TIMEOUT_SEC = _tmo
        _el = time.monotonic() - _t0
        chk(b2 is None and '時間' in (w2 or ''),
            f'4n: 少しずつ送り続ける相手を全体の時間で切っていない（{(w2 or "")[:50]}）')
        chk(_el < 8, f'4n2: 打ち切りが効かず {_el:.1f} 秒かかった（上限は2秒のはず）')
        # 転送を繰り返して時間を稼ぐ相手。1回ずつは timeout= に引っかからないので、
        # **繋ぐところから転送まで全体**を1つの残り時間で測っていないと伸びる。
        A.DOWNLOAD_TIMEOUT_SEC = 2
        _t1 = time.monotonic()
        try:
            b3, w3 = A.download_zip(base_url + 'redir/0')
        finally:
            A.DOWNLOAD_TIMEOUT_SEC = _tmo
        _el2 = time.monotonic() - _t1
        chk(_el2 < 5,
            f'4o: 転送を繰り返されて {_el2:.1f} 秒かかった（上限は2秒のはず）')
        chk(b3 is None, '4o2: 時間切れなのに取得できたことになっている')
        # 見出しを少しずつ送る相手。本文に入らないので、読み取りループの
        # 検査には辿り着かない。時間が来たら口を閉じて終わらせること。
        A.DOWNLOAD_TIMEOUT_SEC = 2
        _t3 = time.monotonic()
        try:
            b5, w5 = A.download_zip(base_url + 'drip-head')
        finally:
            A.DOWNLOAD_TIMEOUT_SEC = _tmo
        _el4 = time.monotonic() - _t3
        chk(b5 is None, '4r: 見出しだけの相手から取得できたことになっている')
        chk(_el4 < 8,
            f'4r2: 見出しを少しずつ送られて {_el4:.1f} 秒かかった（上限は2秒のはず）')
    finally:
        os.environ.pop('ADDATA_ALLOW_INTERNAL_URL', None)
        httpd.shutdown()
        httpd.server_close()
finally:
    shutil.rmtree(work, ignore_errors=True)
    for _d in _made:
        shutil.rmtree(_d, ignore_errors=True)
    for _f in _dl_made:
        try:
            os.remove(_f)
        except OSError:
            pass

# ── 4t. 目録に本数を盛った ZIP を、開く前に弾く ──────────────────────
# zipfile は組み立ての時点で目録を丸ごとメモリに載せる。
# 小さな入れ物を何百万本も並べられると、本数の検査に辿り着く前に落ちる。
_zwork = tempfile.mkdtemp(prefix='reg_zip_')
try:
    _zp = os.path.join(_zwork, 'many.zip')
    with zipfile.ZipFile(_zp, 'w') as _zf:
        for _i in range(5):
            _zf.writestr('Addata/D/D89/x%d.DB' % _i, b'x')
    chk(app._zip_declared_members(_zp) == 5,
        f'4t: 目録の本数を読めていない（{app._zip_declared_members(_zp)}）')
    with open(_zp, 'rb') as _f:
        chk(app._zip_declared_members(_f.read()) == 5,
            '4t2: バイト列からも本数を読めること')
    # 画面からのアップロード（バイト列）も壊れていないこと。
    # URL 経路のためにファイルの場所も受けるようにしたので、両方見る。
    with open(_zp, 'rb') as _f:
        _blob2 = _f.read()
    for _label, _src2 in (('バイト列', _blob2), ('ファイルの場所', _zp)):
        _out = os.path.join(_zwork, 'out_' + ('b' if _label == 'バイト列' else 'f'))
        os.makedirs(_out, exist_ok=True)
        _r6, _w6 = app.extract_addata_zip(_src2, _out)
        chk(bool(_r6) and app._addata_is_valid(_r6),
            f'4u: {_label} からの展開が壊れている: {_w6}')
    # 壊れた入力でも落ちないこと
    for _bad in (b'not a zip at all', os.path.join(_zwork, 'nope.zip')):
        try:
            chk(app._zip_declared_members(_bad) is None,
                f'4v: 壊れた入力から本数を読めたことになっている: {_bad!r:.30}')
        except Exception as _e:      # noqa: BLE001
            chk(False, f'4v2: 壊れた入力で例外 {type(_e).__name__}: {_e}')
    _lim2 = app.ADDATA_ZIP_MAX_MEMBERS
    app.ADDATA_ZIP_MAX_MEMBERS = 3
    try:
        _r5, _w5 = app.extract_addata_zip(_zp, os.path.join(_zwork, 'out'))
        chk(_r5 is None and '多すぎ' in (_w5 or ''),
            f'4t3: 本数の多い ZIP を弾いていない: {_w5}')
    finally:
        app.ADDATA_ZIP_MAX_MEMBERS = _lim2
    # 本数とバイト数の**両方**を見ること。
    # zipfile は目録を「本数」ではなく「バイト数」で読み進めるので、
    # 「本数は小さく、バイト数は巨大」と書かれると本数の検査をすり抜ける。
    _n, _sz = app._zip_declared_index(_zp)
    chk(_n == 5 and isinstance(_sz, int) and _sz > 0,
        f'4w: 目録の本数とバイト数を読めていない（{_n} / {_sz}）')
    with open(_zp, 'rb') as _f:
        _raw = bytearray(_f.read())
    _e = _raw.rfind(bytes.fromhex('504b0506'))
    chk(_e >= 0, '4w2: 目録の末尾が見つからない')
    if _e >= 0:
        # 本数はそのまま（5本）で、バイト数だけ 400MB と偽る
        _raw[_e + 12:_e + 16] = (400 * 1024 * 1024).to_bytes(4, 'little')
        _fz = os.path.join(_zwork, 'fake.zip')
        with open(_fz, 'wb') as _f:
            _f.write(bytes(_raw))
        _r7, _w7 = app.extract_addata_zip(_fz, os.path.join(_zwork, 'out_f2'))
        chk(_r7 is None and '目録' in (_w7 or ''),
            f'4w3: 本数を小さく偽り目録だけ巨大な ZIP を通している: {_w7}')
finally:
    shutil.rmtree(_zwork, ignore_errors=True)

# ── 5. 使い回しの入れ物は URL ごとに分かれる ─────────────────────────
chk(A.cache_dir_for('https://a/x.zip') != A.cache_dir_for('https://b/x.zip'),
    '5: 別の URL が同じ入れ物を使っている')
chk(A.cache_dir_for('https://a/x.zip') == A.cache_dir_for('https://a/x.zip'),
    '5b: 同じ URL なのに入れ物が変わる')
chk(A.cached_root('https://never-fetched.example/x.zip', app._addata_is_valid) == '',
    '5c: 取り込んでいない URL で何かを返している')
# 目印を置く場所と中身を置く場所は別。同じだと「目印を消さずに中身だけ
# 入れ替える」ができない。
chk(A.new_payload_dir('https://a/x.zip') != A.cache_dir_for('https://a/x.zip'),
    '5d: 目印と中身が同じ場所にある')
# 掃除（app._sweep_stale_addata_dirs）が拾えるよう addata_ で始めること
_tmp = os.path.realpath(tempfile.gettempdir())
for _p in (A.cache_dir_for('https://a/x.zip'), A.new_payload_dir('https://a/x.zip')):
    chk(os.path.dirname(os.path.realpath(_p)) == _tmp
        and os.path.basename(_p).startswith('addata_'),
        f'5e: 掃除の対象から外れる場所を使っている: {_p}')

# ── 6. 解決順序（アップロード > パス > URL > 環境変数 > 自動検出） ────
import inspect  # noqa: E402
with open(os.path.join(R, 'app.py'), encoding='utf-8') as _f0:
    _app_src0 = _f0.read()
_src = inspect.getsource(app.find_addata_dir)
_order = [_src.index(k) for k in ('_ADDATA_UPLOAD_KEY', '_QS_ADDATA_DIR',
                                  '_QS_ADDATA_URL', 'ADDATA_ROOT', 'addata_locator')]
chk(_order == sorted(_order),
    f'6: Addata の探し方の順番が入れ替わっている {_order}')

# ── 7. 取り込みは共有の入れ物を消さない（ソースで固定） ───────────────
# 「同じURLの入れ物を rmtree してから展開」に戻すと 4i が壊れるが、
# 気づきにくいのでソース側でも縛る。
_fsrc = inspect.getsource(app._addata_from_url)
chk('new_payload_dir' in _fsrc,
    '7: 取り込みが取り込みごとの展開先を使っていない')
chk('rmtree(dest' not in _fsrc.split('if not root:')[0],
    '7b: 展開前に展開先を消している（他セッションの使用中を巻き込む）')

# ── 7c. 取り込み済みでも、使ってよい URL かを先に見る ─────────────────
# 後回しにすると「前に取れているから」で、いま許されない宛先が通ってしまう。
_f7 = inspect.getsource(app._addata_from_url)
chk_order(_f7, 'validate_url', 'cached_root',
          '7c: 取り込み済みのものを使う前に URL を検査していない')

# ── 7d. 共有リンクの組み立て直しで、合言葉が消えた形にしない ──────────
# 正規化は URL を作り直す。Google ドライブの
# `https://利用者名:合言葉@drive.google.com/file/d/…` は合言葉の落ちた形に化けるので、
# 正規化の**あと**で検査すると合言葉入りのURLを受け付けてしまい、
# そのURLがブックマークに残り続ける。生のURLを先に見ること。
chk(A.validate_url(A.normalize_share_url(
    'https://u:p@drive.google.com/file/d/ABC/view'))[0],
    '7d: 前提が崩れている（正規化しても合言葉が残るなら、この検査は不要）')
_f7d = inspect.getsource(app._addata_from_url)
chk_order(_f7d, 'validate_url', 'cached_root',
          '7d2: 検査より先に取り込み済みを使っている')
chk('safe_str(url).strip(), _as.normalize_share_url(url)' in _f7d,
    '7d3: 生のURLと正規化後のURLの両方を検査していない')

# ── 7e. URL から取り込んだぶんは、合計でも頭打ちにする ───────────────
# URL は誰でも仕込めるので、違うURLを並べられると一時領域が尽きてアプリが止まる。
chk(hasattr(app, '_evict_addata_url_cache'), '7e: 量で捨てる仕組みが無い')
chk(app._ADDATA_URL_CACHE_MAX_BYTES > 0, '7e2: 合計の上限が設定されていない')
_f7e = inspect.getsource(app._addata_from_url)
chk_order(_f7e, '_evict_addata_url_cache', 'extract_addata_zip',
          '7e3: 展開する前に量で捨てていない')
# 実際に消える／残るを見る。ほかの一時ファイルに左右されないよう、
# この検査のあいだだけ一時領域を専用の場所に差し替える
# （実物を消してしまわないためでもある）。
_cap = app._ADDATA_URL_CACHE_MAX_BYTES
_tmp_bak = tempfile.tempdir
_sandbox = tempfile.mkdtemp(prefix='reg_evict_')
_ev_old = os.path.join(_sandbox, 'addata_url_old')
_ev_new = os.path.join(_sandbox, 'addata_url_new')


def _mk_ev(path, mtime_back, lease_age=None):
    """256KB 入りの展開先を作る。lease_age を渡すと「使っている」印も置く。"""
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'big.bin'), 'wb') as f:
        f.write(b'x' * (256 * 1024))
    if lease_age is not None:
        _ld = os.path.join(path, app._ADDATA_LEASE_DIR)
        os.makedirs(_ld, exist_ok=True)
        _lf = os.path.join(_ld, 'other')
        with open(_lf, 'w') as f:
            f.write('')
        _lt = time.time() - lease_age
        os.utime(_lf, (_lt, _lt))
    _t = time.time() - mtime_back
    os.utime(path, (_t, _t))


try:
    tempfile.tempdir = _sandbox
    app._ADDATA_URL_CACHE_MAX_BYTES = 300 * 1024     # 1つぶんだけ置ける
    _mk_ev(_ev_old, 3600)       # 最後に使ったのが1時間前
    _mk_ev(_ev_new, 0)          # たった今使った
    app._evict_addata_url_cache()
    chk(not os.path.isdir(_ev_old), '7e4: 上限を超えても古いものを捨てていない')
    chk(os.path.isdir(_ev_new), '7e5: 新しく使ったものまで捨てている')

    # いま使っているものは、古くても残す
    _mk_ev(_ev_old, 7200)
    _mk_ev(_ev_new, 0)
    app._evict_addata_url_cache(keep=_ev_old)
    chk(os.path.isdir(_ev_old), '7e6: keep したものまで捨てている')

    # ── 別のセッションが生成に使っている最中のものは後回しにする ──────
    # Streamlit Cloud は複数の利用者が同じプロセス・同じ一時領域を使う。
    # 生成の最中に展開先を消されると、その場で照合が崩れる。
    _mk_ev(_ev_old, 7200, lease_age=0)      # 古いが「使っている」印あり
    _mk_ev(_ev_new, 60)                     # 新しいが印なし
    app._evict_addata_url_cache()
    chk(os.path.isdir(_ev_old),
        '7e10: 別のセッションが使っている最中のものを先に捨てている')
    chk(not os.path.isdir(_ev_new), '7e11: 印の無い方を捨てていない')

    # 印が古くなったものは、使っていないものとして扱う
    _mk_ev(_ev_old, 7200, lease_age=app._ADDATA_LEASE_SEC + 60)
    _mk_ev(_ev_new, 0)
    app._evict_addata_url_cache()
    chk(not os.path.isdir(_ev_old), '7e12: 古い印をいつまでも尊重している')

    # 印のあるものしか無く、それでも上限を超えるなら捨てる
    # （一時領域が尽きるとアプリごと止まるので、最後は量を優先する）
    _mk_ev(_ev_old, 7200, lease_age=0)
    _mk_ev(_ev_new, 0, lease_age=0)
    app._evict_addata_url_cache()
    chk(not (os.path.isdir(_ev_old) and os.path.isdir(_ev_new)),
        '7e14: 全部に印があると、上限を超えたまま何も捨てない')
finally:
    tempfile.tempdir = _tmp_bak
    app._ADDATA_URL_CACHE_MAX_BYTES = _cap
    shutil.rmtree(_sandbox, ignore_errors=True)

# 展開したあとにも均すこと（展開前だけだと、いま入れたものが上限の外に残る）
_f7f = inspect.getsource(app._addata_from_url)
chk(_f7f.count('_evict_addata_url_cache') >= 2,
    '7e7: 展開したあとに均していない（入れたぶんが上限の外に残る）')
chk('max_total=min(_ADDATA_URL_CACHE_MAX_BYTES, _room)' in _f7f,
    '7e8: 1本の展開量を「合計の上限」と「いま空いているぶん」の小さい方に'
    '合わせていない（上限近くまで埋まっているときに新しいぶんが丸ごと乗る）')
chk('_addata_url_cache_size(exclude=_old_base)' in _f7f,
    '7e16: いま空いているぶんを数えていない（同じURLの古いぶんは除くこと）')
chk_order(_f7f, '_evict_addata_url_cache()', '_room =',
          '7e17: 空けてから空きを数えていない')
# 目録の本数は、zipfile に渡す前に見ること
chk(hasattr(app, '_zip_declared_index'), '7e18: 目録を先に見る仕組みが無い')
_fex = inspect.getsource(app.extract_addata_zip)
chk_order(_fex, '_zip_declared_index', 'zipfile.ZipFile',
          '7e19: 目録を zipfile に渡したあとに見ている')
chk('_cd_size' in _fex,
    '7e20: 目録のバイト数を見ていない（zipfile は本数ではなくバイト数で読み進める）')
# 合計は keep も使用中のぶんも数えること（数えないと上限を超えて居座る）
_f7g = inspect.getsource(app._evict_addata_url_cache)
chk('total += size' in _f7g and 'if rd == keep_real' in _f7g,
    '7e9: 合計にいま使っているぶんを数えていない')
chk(_f7f.count('_addata_mark_in_use') >= 3,
    '7e13: 取り込んだとき／使い回すときに「使っている」印を打っていない')
# 印は展開を**始める前**に打つこと。展開先は addata_url_* なので、
# 展開している最中に別のセッションの回収に消されうる。
chk_order(_f7f, '_addata_mark_in_use(dest)', 'extract_addata_zip',
          '7e15: 展開を始める前に「使っている」印を打っていない')

# 名前を引くのも残り時間の中で行うこと（getaddrinfo には時間の指定が無い）
_fres = inspect.getsource(A._safe_connect)
chk('_resolve(' in _fres, '7h: 名前を引くのに時間の歯止めが無い')
_fr = inspect.getsource(A._resolve)
chk('join(' in _fr and 'is_alive()' in _fr,
    '7h2: 名前を引くのを別の走りにして待つ時間を区切っていない')
# 返事が返らない名前で、決めた時間で諦めること
_orig_gai2 = A.socket.getaddrinfo


def _slow_gai(host, port, *a, **kw):
    time.sleep(30)
    return []


A.socket.getaddrinfo = _slow_gai
try:
    _t2 = time.monotonic()
    try:
        A._safe_connect('slow.example.com', 80, 1.0, None)
        chk(False, '7h3: 返事の返らない名前で繋いだことになっている')
    except (A.TookTooLong, A.BlockedAddress):
        pass
    except Exception as _e:      # noqa: BLE001
        chk(False, f'7h3: 想定外の例外 {type(_e).__name__}: {_e}')
    _el3 = time.monotonic() - _t2
    chk(_el3 < 6, f'7h4: 名前を引くのに {_el3:.1f} 秒待ってしまった（上限1秒のはず）')
finally:
    A.socket.getaddrinfo = _orig_gai2

# 打ち切りは **文字列ではなく中身の型**で見分けること。
# 文面は1種類ではないので（「取得に時間が…」と「名前の解決に時間が…」）、
# 文字列を切り出す作りだと ValueError が飛んで画面が落ちる。
_fdz = inspect.getsource(A.download_zip)
chk('.index(' not in _fdz.split('except')[-3] if _fdz.count('except') >= 3 else True,
    '7j: 打ち切りの判別で文字列を切り出している')
chk('isinstance(_r, (TookTooLong, BlockedAddress))' in _fdz,
    '7j2: 包まれた例外を型で見分けていない')
# 名前が引けない相手で、例外を投げずに (None, 理由) を返すこと
_orig_gai3 = A.socket.getaddrinfo


def _slow_gai3(host, port, *a, **kw):
    time.sleep(30)
    return []


A.socket.getaddrinfo = _slow_gai3
_tmo3 = A.DOWNLOAD_TIMEOUT_SEC
A.DOWNLOAD_TIMEOUT_SEC = 1
try:
    _b4, _w4 = A.download_zip('https://slow-dns.example.com/Addata.zip')
    chk(_b4 is None and bool(_w4),
        '7j3: 名前が引けないときに理由を返していない')
    chk('時間' in (_w4 or ''), f'7j4: 理由が時間の話になっていない: {(_w4 or "")[:60]}')
except Exception as _e:          # noqa: BLE001
    chk(False, f'7j5: 名前が引けないときに例外が飛んでいる {type(_e).__name__}: {_e}')
finally:
    A.socket.getaddrinfo = _orig_gai3
    A.DOWNLOAD_TIMEOUT_SEC = _tmo3

# 本文を読むときも、そのつど残り時間を渡し直すこと
_fdl = inspect.getsource(A.download_zip)
_body = _fdl.split('while True:')[1]
chk_order(_body, '_deadline_left()', 'chunk = _read(',
          '7i: 読み取りの前に残り時間を見ていない')
chk('settimeout' in _body.split('chunk = _read(')[0],
    '7i2: 読み取りのたびに時間を締め直していない')

# 行き先が複数返るとき、1件ごとに残り時間を測り直すこと
# （測り直さないと件数ぶん待たされ、全体の上限を軽く超える）
_fsc = inspect.getsource(A._safe_connect)
chk_order(_fsc, 'for fam, typ, proto', 'sock.settimeout',
          '7g: 接続の組み立てが想定と違う')
_loop = _fsc.split('for fam, typ, proto')[1]
chk('_deadline_left()' in _loop,
    '7g2: 行き先1件ごとに残り時間を測り直していない')

# ── 7k. 失敗した取得URLをしばらく叩き直さない ────────────────────────
# find_addata_dir() は1回の描き直しの中で何度も呼ばれ、描き直しのたびにまた
# 呼ばれる。失敗するURLをそのつど取りに行くと、上限120秒 × 呼ばれた回数だけ
# 画面が止まり、**設定を消すことすらできなくなる**。
chk(hasattr(app, '_addata_url_recent_failure'), '7k: 失敗を覚える仕組みが無い')
_f7k = inspect.getsource(app._addata_from_url)
chk_order(_f7k, '_addata_url_recent_failure', 'download_zip',
          '7k2: 取りに行く前に「さっき失敗したか」を見ていない')
chk('_addata_url_remember_failure' in _f7k, '7k3: 失敗を覚えていない')
import streamlit as _st2  # noqa: E402
_st2.session_state.pop('_addata_url_failed', None)
app._addata_url_remember_failure('https://x.example/a.zip', 'ためし')
chk(app._addata_url_recent_failure('https://x.example/a.zip') == 'ためし',
    '7k4: 覚えた失敗を思い出せない')
chk(app._addata_url_recent_failure('https://y.example/a.zip') == '',
    '7k5: 別のURLまで失敗扱いにしている')
_st2.session_state['_addata_url_failed']['at'] = time.time() - app._ADDATA_URL_FAIL_SEC - 5
chk(app._addata_url_recent_failure('https://x.example/a.zip') == '',
    '7k6: 古い失敗をいつまでも覚えている')
app._addata_url_forget_failure()
chk(app._addata_url_recent_failure('https://x.example/a.zip') == '',
    '7k7: 忘れる手が効いていない')

# ── 7l. 取り込んだ一時ファイルは使い終わったら消すこと ────────────────
chk('os.remove(zip_path)' in _f7k, '7l: 取り込んだ一時ファイルを消していない')
# 印は生成が終わるまで持つ長さにすること（OCR を挟むと数分かかる）
chk(app._ADDATA_LEASE_SEC >= 20 * 60,
    f'7l2: 使用中の印が短すぎる（{app._ADDATA_LEASE_SEC} 秒）。'
    '生成の最中に切れると、その最中に展開先を回収されうる')

# ── 7f. 「設定を消す」で入力欄の中身も消える ──────────────────────
# Streamlit は key を付けた入力の値を session_state で持っていて、
# 描き直しでは value= よりそちらが優先される。消さないと
# 「消したはずの設定が欄に残り、次の保存で書き戻される」。
# 確定したあとに session_state から消す手は Streamlit に拒まれる
# （例外が出て握り潰され、欄に前の値が残ったままになる）。
# 入力欄の名前に番号を付けて、消すときに番号を進めて作り直す。
_clear_seg = _app_src0.split('if _clear:')[1].split('if _save:')[0]
chk("st.session_state['_addata_widget_nonce'] = _w + 1" in _clear_seg,
    '7f: 設定を消すときに入力欄を作り直していない（前の値が欄に残る）')
for _wk in ("key='addata_dir_input_%d' % _w", "key='addata_url_input_%d' % _w"):
    chk(_wk in _app_src0, f'7f2: 入力欄の名前に番号が付いていない: {_wk}')
chk("st.session_state.pop('addata_dir_input'" not in _app_src0,
    '7f3: 拒まれる消し方（確定後の session_state からの削除）に戻っている')

# ── 8. 取得URLが設定されていて取れないときは、別の Addata に落ちない ──
# 「どのPCでも同じデータベース」が取得URLの目的。取りに行けなかったときに
# 黙ってそのマシンの Addata を使うと、**同じブックマークから開いたのに
# 違うデータベースで照合した見積**が出る。版が違えば標準品番も標準指数も
# 変わるのに、画面はいつもどおりで気づけない。
import streamlit as _st  # noqa: E402
_env_bak = os.environ.get('ADDATA_ROOT')
_fake_addata = tempfile.mkdtemp(prefix='reg_addata_env_')
try:
    os.makedirs(os.path.join(_fake_addata, 'D', 'D89'), exist_ok=True)
    with open(os.path.join(_fake_addata, 'D', 'D89', 'D8912.DB'), 'wb') as f:
        f.write(b'x' * 64)
    chk(app._addata_is_valid(_fake_addata), '8: 検証用の Addata が妥当でない')
    os.environ['ADDATA_ROOT'] = _fake_addata
    # 取得URLなし → 環境変数のものが使われる（落ちる先があること自体の確認）
    _st.session_state['_setting_' + app._QS_ADDATA_URL] = ''
    _st.session_state['_setting_' + app._QS_ADDATA_DIR] = ''
    chk(app.find_addata_dir() == _fake_addata,
        '8b: 環境変数の Addata が使われない（この後の検査が意味を持たない）')
    # 取得URLあり・取れない → None（環境変数のものに落ちない）
    _st.session_state['_setting_' + app._QS_ADDATA_URL] =         'https://this-host-does-not-exist.invalid/Addata.zip'
    chk(app.find_addata_dir() is None,
        '8c: 取得URLが取れないのに別の Addata（環境変数）を使ってしまう')
    chk(bool(_st.session_state.get('_addata_url_error')),
        '8d: 取れなかった理由が残っていない（画面に出せない）')
finally:
    _st.session_state['_setting_' + app._QS_ADDATA_URL] = ''
    if _env_bak is None:
        os.environ.pop('ADDATA_ROOT', None)
    else:
        os.environ['ADDATA_ROOT'] = _env_bak
    shutil.rmtree(_fake_addata, ignore_errors=True)

# ── 9. 見えないパスが取得URLの保存を邪魔しない ───────────────────────
# 「自分のPC用にパスを入れ、クラウド用に取得URLも入れる」は正しい使い方。
# パスが見えないことを保存の失敗にすると、クラウドでは**取得URLを1つも
# 保存できなくなる**（パス欄を手で空にするまで）。探す順番の側では
# 見えないパスは黙って飛ばして取得URLへ進むので、扱いを揃える。
with open(os.path.join(R, 'app.py'), encoding='utf-8') as _f:
    _app_src = _f.read()
chk(_app_src.count('if _save:') == 1, '9: 保存ボタンの処理が1か所に特定できない')
_seg = _app_src.split('if _save:')[1].split('if st.session_state.get(')[0]
_key = 'if _d and not _addata_is_valid(_d):'
chk(_key in _seg, '9b: 保存時のパス検査が見当たらない')
if _key in _seg:
    _dir_block = _seg.split(_key)[1].split('if _u:')[0]
    chk('_problems.append' not in _dir_block,
        '9c: 見えないフォルダのパスが保存を止めている（取得URLを保存できなくなる）')
    chk('_dir_warn' in _dir_block, '9d: 見えないパスであることを伝えていない')
# 注意書きは保存後の描き直しで消えてしまうので、セッションに置いて出すこと
chk("st.session_state['_addata_dir_warn'] = _dir_warn" in _seg,
    '9e: 注意書きが画面の描き直しで消える（保存直後しか出ない）')
chk("st.session_state.get('_addata_dir_warn')" in _app_src,
    '9f: 置いた注意書きを描き直したあとに出していない')

print('REG_SETTINGS:', 'ALL PASS' if not FAIL else 'FAIL')
for f in FAIL:
    print('  -', f)
sys.exit(1 if FAIL else 0)
