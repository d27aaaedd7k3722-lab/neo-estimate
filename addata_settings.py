#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Addata（コグニセブンの車種データベース）の置き場所を、画面から設定して覚える。

## なぜ必要か

このアプリは本番では Streamlit Cloud（Linux サーバ）で動いている。
**サーバから利用者のPCの `C:\\Addata` は読めない。** パスを打っても届かない。
そのため、いままでは開くたびに ZIP をアップロードし直す必要があった。

そこで、設定を **URL のクエリに残せる**ようにした。
一度設定して出てきた URL をブックマークすれば、**どのPCでその URL を開いても
同じ設定で立ち上がる**。設定は次の3通りで、上から順に試す。

1. `dir`  … フォルダのパス。**アプリが動いているマシンから見える場所**のみ有効。
             そのPCでアプリを直接起動している場合、社内サーバ・Docker で
             ボリュームを渡している場合に使う。
2. `url`  … Addata の ZIP が置いてある URL（OneDrive/SharePoint の共有リンク、
             社内 HTTP など）。サーバが取りに行けるので、**クラウドでも効く**。
             一度設定すればどのPCでも使える、いちばん実用的な方法。
3. 画面からの ZIP アップロード（従来どおり。その場かぎり）

## 置き場所

クエリに残すのは短い文字列だけ。ZIP の中身は毎回サーバの一時領域に展開し、
使われなくなったものは掃除される（`app._sweep_stale_addata_dirs`）。

## 気をつけていること

**このアプリの URL は誰でも開ける。** `?addata_url=…` を仕込んだリンクを踏ませれば、
サーバに任意の宛先を叩かせられる（踏み台）。宛先の検査は
「URL の文字列」ではなく **実際に繋ぐアドレス**で行う（`_safe_connect`）。
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# URL から取り込むときの歯止め。悪意ある URL でサーバのディスクを埋めない。
DOWNLOAD_MAX_BYTES = 300 * 1024 * 1024      # 300MB
DOWNLOAD_TIMEOUT_SEC = 120
# 取り込んだものはこの時間だけ使い回す（毎回落とし直すと重い）
DOWNLOAD_CACHE_SEC = 6 * 60 * 60

QS_DIR = 'addata_dir'
QS_URL = 'addata_url'

MARKER = '.addata_root'

_ALLOWED_SCHEMES = ('http', 'https')

# 名前だけで社内と分かるもの。DNS を引くまでもなく弾く。
# `metadata.google.internal` はクラウドの内部メタデータ（169.254.169.254）。
_INTERNAL_NAMES = ('metadata.google.internal', 'metadata.goog', 'instance-data')
_INTERNAL_SUFFIXES = ('.internal', '.local', '.localdomain', '.home.arpa')


_NOT_ZIP = ('ZIP ではありませんでした。共有リンクの場合は'
            '「ダウンロード用のリンク」を指定してください')


class BlockedAddress(OSError):
    """行き先が社内・自分自身を指していたので繋がなかった。"""


class TookTooLong(OSError):
    """決めた時間で終わらなかったので打ち切った。"""


# 1回の取得に使ってよい残り時間。繋ぐ・TLS・見出し・本文・転送のすべてを
# これ1つで測る。socket の timeout= は「何も届かない時間」の上限でしかなく、
# 少しずつ送り続ける相手や、転送を繰り返す相手には効かない。
_budget = threading.local()


def _sock_box():
    """いま繋いでいる相手の入れ物。

    見張り役（別の走り）からも触るので、**走りごとの置き場ではなく
    受け渡せる入れ物**にしておく。
    """
    box = getattr(_budget, 'box', None)
    if box is None:
        box = {'sock': None, 'expired': False}
        _budget.box = box
    return box


def _cut_connection(box):
    """時間切れになったら、繋いでいる口を閉じて読み書きを終わらせる。

    socket の timeout は「何も届かない時間」の上限でしかない。
    **見出しを少しずつ送る**相手には効かず、本文を読み始めるまで
    こちらの時間の検査に辿り着けない。口を閉じれば必ず終わる。
    """
    box['expired'] = True
    sk = box.get('sock')
    if sk is None:
        return
    try:
        sk.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sk.close()
    except OSError:
        pass


def _deadline_set(seconds):
    _budget.until = time.monotonic() + seconds
    _budget.box = {'sock': None, 'expired': False}


def _deadline_clear():
    _budget.until = None


def _timeout_reason():
    """時間切れのときに画面へ出す文言（socket が先に切れた場合も同じ扱い）。"""
    return ('取得に時間がかかりすぎています（%d 秒で打ち切りました）。'
            '置き場所を見直してください' % DOWNLOAD_TIMEOUT_SEC)


def _deadline_left():
    """残り時間（秒）。決めていなければ None。無ければ例外。"""
    until = getattr(_budget, 'until', None)
    if until is None:
        return None
    left = until - time.monotonic()
    if left <= 0:
        raise TookTooLong(_timeout_reason())
    return left


def normalize_share_url(url: str) -> str:
    """共有リンクを「中身がそのまま落ちてくる」形に直す。

    OneDrive / SharePoint / Google ドライブの共有リンクは、そのまま取りに行くと
    HTML のプレビュー画面が返ってきて ZIP にならない。各サービスの
    ダウンロード用の形に寄せる。当てはまらない URL はそのまま返す。
    """
    u = (url or '').strip()
    if not u:
        return ''
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return u
    host = (p.netloc or '').lower()
    # OneDrive / SharePoint: 末尾に download=1 を足す
    if ('sharepoint.com' in host or '1drv.ms' in host
            or 'onedrive.live.com' in host):
        q = dict(urllib.parse.parse_qsl(p.query, keep_blank_values=True))
        q['download'] = '1'
        return urllib.parse.urlunsplit(
            (p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))
    # Google ドライブ: /file/d/<id>/view → uc?export=download&id=<id>
    if 'drive.google.com' in host:
        m = re.search(r'/file/d/([^/]+)', p.path)
        if m:
            q = [('export', 'download'), ('id', m.group(1))]
            # 共有ドライブのリンクは `resourcekey` が無いと中身が返らず、
            # 許可を求める HTML が返ってくる（＝ZIPでないとして弾かれ、
            # 設定したのに Addata が読めない）。落とさずに持っていく。
            for _k, _v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
                if _k.lower() in ('resourcekey', 'resourceKey'.lower()):
                    q.append(('resourcekey', _v))
            return 'https://drive.google.com/uc?' + urllib.parse.urlencode(q)
    return u


def _is_internal_ip(addr: str) -> bool:
    """実際に繋ぐアドレスが、インターネットから届かない範囲か。

    考え方は「**外から届くと言い切れないものは全部止める**」。
    個別の範囲を並べる形だと必ず穴が残る。実際、はじめは private / loopback /
    link-local / reserved / multicast / unspecified だけを見ていて、
    `100.64.0.0/10`（通信事業者の中で使う共有アドレス）が素通りしていた。
    Python はこれを private とも reserved とも呼ばないため。
    """
    try:
        ip = ipaddress.ip_address((addr or '').strip().strip('[]'))
    except ValueError:
        return False
    if ip.version == 6:
        # ::ffff:127.0.0.1 のように IPv6 に包んで内側を指せるので中身で判断する。
        # Teredo は `(中継サーバ, 相手)` の**両方**が包まれている。
        # 相手側だけを内側にした細工があるので、片方だけ見てはいけない。
        embedded = []
        if ip.ipv4_mapped:
            embedded.append(ip.ipv4_mapped)
        _s6 = getattr(ip, 'sixtofour', None)
        if _s6:
            embedded.append(_s6)
        _t = getattr(ip, 'teredo', None)
        if _t:
            embedded.extend([a for a in _t if a])
        for _e in embedded:
            if _is_internal_ip(str(_e)):
                return True
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        return True
    try:
        return not bool(ip.is_global)
    except (AttributeError, ValueError):
        return False


def _is_internal_host(host: str) -> bool:
    """社内・自分自身を指すアドレスか（**名前の見た目だけ**の判断）。

    このアプリの URL は誰でも開ける。`?addata_url=…` を仕込んだリンクを
    踏ませれば、**サーバに任意の場所を叩かせられる**（踏み台にされる）。
    クラウドの内部メタデータ（169.254.169.254）や社内の機器が狙われるので、
    外から到達できないアドレスは弾く。

    ただし名前の見た目だけでは足りない。`example.com` が 127.0.0.1 に
    解決されることもある。**最後の砦は繋ぐ直前の検査**（`_safe_connect`）で、
    ここはあくまで早い段階で分かりやすく断るためのもの。
    """
    h = (host or '').strip().strip('[]').rstrip('.').lower()
    if not h:
        return True
    if h in ('localhost', 'localhost.localdomain') or h.endswith('.localhost'):
        return True
    if h in _INTERNAL_NAMES or h.endswith(_INTERNAL_SUFFIXES):
        return True
    return _is_internal_ip(h)


def allow_internal_hosts() -> bool:
    """社内アドレスを許すか。検証や社内サーバ運用のときだけ環境変数で開ける。"""
    return str(os.environ.get('ADDATA_ALLOW_INTERNAL_URL', '')).strip().lower() \
        in ('1', 'true', 'yes', 'on')


def validate_url(url: str) -> tuple:
    """取り込み先として使ってよい URL か。(可否, 理由) を返す。

    ここで見るのは URL の文字列だけ。名前が実際にどこを指すかは
    繋ぐ直前に `_safe_connect` が見る。
    """
    u = (url or '').strip()
    if not u:
        return (False, 'URL が空です')
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return (False, 'URL の形式が正しくありません')
    if p.scheme.lower() not in _ALLOWED_SCHEMES:
        return (False, 'http / https の URL を指定してください')
    if not p.netloc:
        return (False, 'URL の形式が正しくありません')
    # この設定は URL に残して**ブックマーク・共有される**のが前提。
    # `https://利用者名:合言葉@example.com/…` の形は、そのまま
    # ブラウザの履歴・ブックマーク・共有リンク・サーバのログに残る。
    # 「@ の前を本当の宛先だと読み違える」細工にも使われるので受け付けない。
    if p.username is not None or p.password is not None or '@' in p.netloc:
        return (False, 'ユーザー名やパスワードを含む URL は使えません。'
                       'この設定はURLに残して共有できる形なので、'
                       '合言葉がそのまま他の人に渡ってしまいます')
    try:
        host = p.hostname or ''
    except ValueError:
        return (False, 'URL の形式が正しくありません')
    if _is_internal_host(host) and not allow_internal_hosts():
        return (False, 'このアドレスは指定できません（社内・自分自身を指す宛先）。'
                       'インターネットから取得できる URL を指定してください')
    return (True, '')


# ── 繋ぐ直前の検査 ───────────────────────────────────────────────────
# 「名前を引いて検査 → 繋ぐときにもう一度引く」だと、その隙に社内アドレスへ
# 差し替えられる（DNS リバインディング）。**引いた結果そのものに繋ぐ**ことで
# 隙をなくす。リダイレクトも同じ経路を通るので同じ検査がかかる。

def _resolve(host, port, timeout):
    """名前を引く。決めた時間で戻らなければ諦める。

    `socket.getaddrinfo` には時間の指定が無く、返事を遅らせる名前を
    仕込まれると**ここだけで何秒も止まる**。別の走りで引かせて、
    待つのは残り時間までにする（引きっぱなしの走りは置いていく）。
    """
    box = {}

    def _run():
        try:
            box['r'] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except BaseException as e:      # noqa: BLE001
            box['e'] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout if isinstance(timeout, (int, float)) else None)
    if th.is_alive():
        raise TookTooLong('名前の解決に時間がかかりすぎています（%s）。'
                          '置き場所を見直してください' % host)
    if 'e' in box:
        raise BlockedAddress('名前を解決できませんでした: %s' % host) from box['e']
    return box.get('r') or []


def _safe_connect(host, port, timeout, source_address):
    allow = allow_internal_hosts()
    # 残り時間があればそれより長くは待たない（転送を繰り返されても伸びない）
    _left = _deadline_left()
    if _left is not None:
        if isinstance(timeout, (int, float)):
            timeout = min(timeout, _left)
        else:
            timeout = _left
    infos = _resolve(host, port, timeout)
    if not infos:
        raise BlockedAddress('名前を解決できませんでした: %s' % host)
    if not allow:
        for _fam, _typ, _proto, _canon, sa in infos:
            if _is_internal_ip(sa[0] if sa else ''):
                raise BlockedAddress(
                    'この宛先は指定できません（%s は社内・自分自身を指すアドレス'
                    ' %s でした）' % (host, sa[0]))
    last = None
    for fam, typ, proto, _canon, sa in infos:
        # 行き先が複数返ることがある（どれも外向きだが繋がらない、など）。
        # 1件ごとに残り時間を測り直さないと、件数ぶん待たされて
        # 全体の上限を軽く超える。名前を引くのに時間がかかった場合も同じ。
        _left = _deadline_left()
        _tmo = timeout
        if _left is not None:
            _tmo = _left if not isinstance(_tmo, (int, float)) else min(_tmo, _left)
        sock = None
        try:
            sock = socket.socket(fam, typ, proto)
            if isinstance(_tmo, (int, float)):
                sock.settimeout(_tmo)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            # 本文を読むときに時間を締め直せるよう、繋いだ相手を控えておく
            _sock_box()['sock'] = sock
            return sock
        except OSError as e:
            last = e
            if sock is not None:
                sock.close()
    raise last if last is not None else BlockedAddress('接続できませんでした: %s' % host)


class _SafeHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _safe_connect(self.host, self.port, self.timeout,
                                  self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _SafeHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        self.sock = _safe_connect(self.host, self.port, self.timeout,
                                  self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._tunnel_host or self.host)
        # TLS で包むと元の口は使えなくなるので、包んだ方を控え直す
        _sock_box()['sock'] = self.sock


class _SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_SafeHTTPConnection, req)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_SafeHTTPSConnection, req, context=self._context)


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクト先も入口と同じ基準で検査する。

    urllib は 302 を黙って追う。入口が外のアドレスでも、
    そこから社内アドレスへ飛ばされれば検査を抜けて叩けてしまう
    （リダイレクトを使った踏み台）。飛び先ごとに見る。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _deadline_left()        # 時間切れならここで打ち切る
        ok, why = validate_url(newurl)
        if not ok:
            raise urllib.error.HTTPError(
                newurl, code, '転送先が指定できないアドレスです（%s）' % why,
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener():
    # ProxyHandler({}) で環境変数のプロキシを無効にする。プロキシを挟むと
    # 実際に繋ぐ相手がプロキシになり、宛先の検査が意味を持たなくなる。
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _SafeHTTPHandler, _SafeHTTPSHandler, _GuardedRedirect)


def download_zip(url: str) -> tuple:
    """URL から ZIP を落とす。(一時ファイルの場所, 理由) を返す。失敗なら (None, 理由)。

    **中身はメモリに抱えず、一時ファイルへ流す。** 上限は 300MB で、
    本番はクラウド。丸ごと持つと、それだけで数百MBを占める上に
    展開のときにもう一度写しが要る。同時に何人か開けばプロセスごと落ちる。
    受け取った側が使い終わったら消すこと（`os.remove`）。
    """
    ok, why = validate_url(url)
    if not ok:
        return (None, why)
    req = urllib.request.Request(
        url, headers={'User-Agent': 'neo-estimate/1.0 (Addata fetch)'})
    # timeout= は「何も届かない時間」の上限でしかない。少しずつ送り続けられても、
    # 転送を繰り返されても切れないので、**繋ぐところから本文まで全体**を
    # 1つの残り時間で測る（`_deadline_*`）。本番では利用者の操作を握ったまま
    # 待たせることになるため。
    _deadline_set(DOWNLOAD_TIMEOUT_SEC)
    box = _sock_box()
    # 見出しを読んでいる間はこちらの検査が回らないので、時間が来たら
    # 別の走りから口を閉じてもらう（これが最後の歯止め）。
    watchdog = threading.Timer(DOWNLOAD_TIMEOUT_SEC, _cut_connection, args=(box,))
    watchdog.daemon = True
    watchdog.start()
    tmp_path = None
    out = None
    result = None
    try:
        with _build_opener().open(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            # Content-Length があれば先に弾く
            try:
                declared = int(resp.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared and declared > DOWNLOAD_MAX_BYTES:
                return (None, 'ファイルが大きすぎます（%d MB）。上限は %d MB です'
                        % (declared // (1024 * 1024),
                           DOWNLOAD_MAX_BYTES // (1024 * 1024)))
            # read() は「頼んだ分が揃うまで」戻らない。少しずつ送り続ける
            # 相手だと1回の read で何分も待つことになり、下の時間の検査に
            # 辿り着けない。read1() は**届いているぶんで戻る**ので、
            # 短い間隔で打ち切りを見に行ける。
            _read1 = getattr(resp, 'read1', None)
            _read = _read1 if callable(_read1) else resp.read
            fd, tmp_path = tempfile.mkstemp(prefix='addata_dl_', suffix='.zip')
            os.close(fd)
            got = 0
            head = b''
            out = open(tmp_path, 'wb')
            while True:
                # 1回の読み取りも残り時間までにする。繋いだときの値のままだと、
                # 少しだけ送って黙る、を繰り返されて全体の上限を超える。
                _left = _deadline_left()
                _sk = box.get('sock')
                if _sk is not None and _left is not None:
                    try:
                        _sk.settimeout(max(0.05, min(DOWNLOAD_TIMEOUT_SEC, _left)))
                    except OSError:
                        pass
                chunk = _read(1024 * 256)
                if not chunk:
                    break
                if len(head) < 2:
                    head = (head + chunk)[:2]
                # ZIP かどうかは先頭2バイトで分かる。共有リンクが HTML の
                # プレビュー画面を返すことはよくあるので、**最初の一口で切る**。
                # 最後まで読んでから捨てると、仕込まれた URL ひとつで
                # 300MB を読ませられる（Content-Length が無ければ事前にも弾けない）。
                if len(head) >= 2 and head != b'PK':
                    return (None, _NOT_ZIP)
                out.write(chunk)
                got += len(chunk)
                if got > DOWNLOAD_MAX_BYTES:
                    return (None, 'ファイルが大きすぎます（上限 %d MB）'
                            % (DOWNLOAD_MAX_BYTES // (1024 * 1024)))
                _deadline_left()        # 時間切れならここで打ち切る
        # ここまで来たら読み切れている。持ち主を呼び出し側に渡すので、
        # 下の後片付けの対象から外す（外す前に return すると消えてしまう）。
        out.close()
        out = None
        if not got:
            return (None, '中身が空でした')
        if head != b'PK':
            return (None, _NOT_ZIP)
        result, tmp_path = tmp_path, None
    except TookTooLong as e:
        return (None, str(e))
    except (socket.timeout, TimeoutError):
        # （見張り役が閉じた場合も同じ文言になる）
        # 残り時間を socket に渡しているので、こちらが先に切れることがある。
        # 中身は同じ「時間切れ」なので、同じ文言で返す。
        return (None, _timeout_reason())
    except urllib.error.URLError as e:
        # urllib は繋ぐ途中の OSError を URLError で包む。包まれた中身を見る。
        # **文字列で判別しない**（打ち切りの文面は1種類ではないし、
        # 切り出しに失敗すると例外が飛んで画面が落ちる）。
        _r = getattr(e, 'reason', None)
        if isinstance(_r, (TookTooLong, BlockedAddress)):
            return (None, str(_r))
        if isinstance(_r, (socket.timeout, TimeoutError)) or box.get('expired'):
            return (None, _timeout_reason())
        return (None, '取得できませんでした: %s' % str(e)[:160])
    except Exception as e:      # noqa: BLE001  ネットワークは何が起きるか分からない
        if box.get('expired'):      # 見張り役が口を閉じた結果のエラー
            return (None, _timeout_reason())
        return (None, '取得できませんでした: %s' % str(e)[:160])
    finally:
        watchdog.cancel()
        _deadline_clear()
        box['sock'] = None
        if out is not None:
            try:
                out.close()
            except OSError:
                pass
        # 途中で諦めたぶんは残さない（成功したときは tmp_path を None にしてある）
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return (result, '')


# ── 取り込んだものの置き場所 ─────────────────────────────────────────
# 同じ URL をブックマークした利用者が同時に開くことがある。
# **使っている最中の展開先を消さない**ように、
#   ・目印（どこに展開したか）だけを URL ごとの場所に置く
#   ・中身は取り込みごとに別の場所へ展開する
#   ・古くなったものは掃除（`app._sweep_stale_addata_dirs`）が回収する
# という形にしてある。目印の差し替えは os.replace 一発で行う。

def _key_for(url: str) -> str:
    return hashlib.sha256((url or '').encode('utf-8')).hexdigest()[:16]


def cache_dir_for(url: str) -> str:
    """URL ごとの「目印を置く場所」。中身はここには置かない。"""
    return os.path.join(tempfile.gettempdir(), 'addata_url_' + _key_for(url))


def new_payload_dir(url: str) -> str:
    """今回の取り込み分を展開する、他と重ならない場所。

    使用中のものを消さずに済むよう、取り込むたびに新しい場所を使う。
    `addata_` で始めるのは掃除の対象に入れるため。
    """
    return os.path.join(tempfile.gettempdir(),
                        'addata_url_%s_%s' % (_key_for(url), uuid.uuid4().hex[:12]))


def _touch(path) -> None:
    """「今も使っている」ことを更新時刻で示す（掃除に消されないように）。"""
    try:
        if path and os.path.isdir(path):
            os.utime(path, None)
    except OSError:
        pass


def read_marker(url: str):
    """目印を読む。`{'base':…, 'root':…, 'fetched':…}` か None。"""
    try:
        with open(os.path.join(cache_dir_for(url), MARKER), encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def cached_root(url: str, is_valid) -> str:
    """URL から取り込み済みで、まだ使い回してよいものがあればそのルートを返す。

    is_valid は「Addata として妥当か」を判定する関数（app 側から渡す）。
    古さの判定に使うのは**取り込んだ時刻**（目印に書いてある）。
    フォルダの更新時刻は「最後に使った時刻」として掃除が見るので、
    ここでは別物として扱う。
    """
    d = read_marker(url)
    if not d:
        return ''
    try:
        fetched = float(d.get('fetched') or 0)
    except (TypeError, ValueError):
        return ''
    if time.time() - fetched > DOWNLOAD_CACHE_SEC:
        return ''
    root = str(d.get('root') or '')
    if not root or not is_valid(root):
        return ''
    _touch(cache_dir_for(url))
    _touch(str(d.get('base') or ''))
    return root


def remember_root(url: str, base: str, root: str) -> None:
    """URL から展開した場所を覚えておく（次回このURLなら落とし直さない）。

    書きかけを別のセッションに読ませないよう、別名で書いてから差し替える。
    """
    d = cache_dir_for(url)
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, MARKER + '.' + uuid.uuid4().hex[:8])
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'base': base or '', 'root': root or '',
                       'fetched': time.time()}, f)
        os.replace(tmp, os.path.join(d, MARKER))
    except OSError:
        return
    _touch(d)
    _touch(base)
