# -*- coding: utf-8 -*-
"""llm.py — 見積 PDF を読む LLM（Claude API）の薄い層。

移植ガイド §3-4: このスキルで PDF を読んでいるのは Claude（PDF・画像入力）なので、同じ精度を狙って Claude API に PDF を渡す。
1 ページずつ切り出した PDF を document ブロックで送る（FAX は 1 ページずつ。§3-1）。
指示文（規則の文書 3 本）は毎回同じなので system に置き、prompt caching で 2 回目以降を安くする。

モデルは既定 claude-opus-5。環境変数 NEO_READER_MODEL で変えられる。
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
import dataclasses
import io
import json
import os
import re
import time
from typing import Optional

DEFAULT_MODEL = os.environ.get('NEO_READER_MODEL') or 'claude-opus-5'


@dataclasses.dataclass
class LLMReply:
    text: str
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    request_id: str = ''
    model: str = ''


class LLMError(RuntimeError):
    pass


class LLMReplyError(LLMError):
    """API の呼び出しは通った（課金された）が、返事が使えない（max_tokens で切れた・正常に終わらなかった・空・JSON でない・
    断られた）。読み手はそのページの読み直しの上限の中でやり直す（バグハント 3 回目 P15）。reply は使った量を数えるため。
    retryable=False（断られた・安全性で止まった）は、読み直しても同じなのでやり直さない（レビュー）"""
    def __init__(self, msg: str, reply=None, retryable: bool = True):
        super().__init__(msg)
        self.reply = reply
        self.retryable = retryable


def clean_api_key(key) -> str:
    """API キーの前後の空白・改行と、コピーで紛れ込む見えない文字（ゼロ幅空白・BOM など）を落とす。以前はゼロ幅空白の
    入ったキーが「接続できない」と誤って案内されていた（P14）"""
    return re.sub(r'[\s\u200b-\u200f\u2028-\u202f\u2060\ufeff]', '', str(key or ''))


MAX_DECLARED_PAGES = 500   # ページ木が申告するページ数がこれを超える PDF は、ページを数える（木を展開する）前に断る


def _open_pdf(pdf_bytes: bytes):
    """PdfReader を開き、権限パスワード（空のユーザーパスワード）だけの PDF は復号する。ユーザーパスワード付きは日本語で断る"""
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(pdf_bytes))
    if r.is_encrypted:
        try:
            ok = r.decrypt('')
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == 'DependencyError':   # AES の暗号を外す部品（cryptography）が無い。パスワードのせいにしない
                raise LLMError('この PDF の暗号を外せません（サーバーに暗号の部品がありません）。印刷した PDF にしてから入れてください')
            ok = 0
        if not ok:
            raise LLMError('パスワード付きの PDF は読めません。パスワードを外した PDF（または印刷した PDF）を入れてください')
    return r


def _declared_pages(r) -> int:
    """/Root/Pages/Count（ページ木が申告するページ数。木を展開しないので速く、爆弾でも膨らまない）。読めなければ -1"""
    try:
        return int(r.trailer['/Root']['/Pages'].get('/Count'))
    except Exception:  # noqa: BLE001
        return -1


def pdf_prepare(pdf_bytes: bytes) -> bytes:
    """読み手に送る前の PDF: 権限パスワードだけの暗号化 PDF は暗号を外した PDF に書き直す（Claude・Gemini は暗号化 PDF を受けない。
    1 ページずつの PDF は暗号なしになるのに、合計欄を読む全ページの PDF だけ暗号化のまま送っていた。2026-09-15 バグハント 3 回目 P6）"""
    from pypdf import PdfWriter
    r = _open_pdf(pdf_bytes)
    if not r.is_encrypted:
        return pdf_bytes
    if _declared_pages(r) > MAX_DECLARED_PAGES:
        return pdf_bytes   # 数える側（pdf_page_count）が断る
    w = PdfWriter()
    for pg in r.pages:
        w.add_page(pg)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def pdf_page_count(pdf_bytes: bytes) -> int:
    """ページ数。申告が MAX_DECLARED_PAGES を超えるならその申告値を返す（呼び出し側がページ数の上限で断る。
    1KB ほどの PDF に「100 万ページ」と書かれていると、木を展開するだけで数百 MB 使っていた。P10）"""
    r = _open_pdf(pdf_bytes)
    d = _declared_pages(r)
    if d > MAX_DECLARED_PAGES:
        return d
    return len(r.pages)


def pdf_single_page(pdf_bytes: bytes, page_index: int) -> bytes:
    """0 起点の 1 ページだけを含む PDF を作る（文字層があればそのまま残る）"""
    from pypdf import PdfWriter
    r = _open_pdf(pdf_bytes)
    w = PdfWriter()
    w.add_page(r.pages[page_index])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


MAX_IMAGE_PIXELS = 40_000_000        # これを超える画像は縮小して PDF にする（app の MAX_RASTER_PIXELS と同じ）
MAX_IMAGE_PIXELS_HARD = 100_000_000  # 縮小できない形式でこれを超えたら断る（展開爆弾。13000×13000 の PNG が 641KB で 1.3GB 使っていた）
# 展開したときの大きさの上限。Pillow は RGB を 1 画素 4 バイト、白黒・グレーを 1 バイトで持つ（画素数だけで見ると、上限の手前の
# 色つき画像 1 枚で 1.1GB 使っていた。レビュー 2 周目）。スマホの JPEG は縮小して読むのでここまで来ない
MAX_IMAGE_BYTES_HARD = 256 * 1024 * 1024
MAX_IMAGE_FRAMES = 30               # 複数ページの TIFF のページ数の上限（読み手のページ数の上限と同じ）


def image_to_pdf(image_bytes: bytes) -> bytes:
    """写真（JPG/PNG/WebP、pillow-heif があれば HEIC/HEIF も）・FAX の TIFF で来た見積書を PDF にする（TIFF は全ページ）。
    Claude API の画像入力は JPEG/PNG/GIF/WebP だけなので、iPhone の HEIC は Pillow で開ける必要がある。
    1 ページずつ PDF にしてから束ねる（全ページを RGB でメモリに溜めると、600dpi の白黒 10 ページで 1.4GB 使っていた。
    共有プロセスが落ちる。レビュー 2026-09-15）。白黒・グレーは RGB にしない"""
    from PIL import Image, UnidentifiedImageError
    try:
        import pillow_heif  # 任意（HEIC/HEIF を開くための追加パッケージ。無ければ JPG/PNG/PDF だけ）
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    import math
    try:
        im = Image.open(io.BytesIO(image_bytes))
    except Image.DecompressionBombError:
        raise LLMError('画像が大きすぎます（画素数が多すぎる）。スマホの写真なら縮小してから入れてください')
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise LLMError('この画像を開けません（HEIC/HEIF は pillow-heif を入れると使えます。'
                       f'JPG・PNG・PDF に変換してから入れてください）: {type(e).__name__}')
    w, h = im.size
    _draft_scale = 1.0
    if im.format in ('JPEG', 'MPO') and w * h > MAX_IMAGE_PIXELS:   # MPO（プレビュー画像入りの JPEG）も縮小して読める（レビュー 4 周目）
        # JPEG は縮小して読む（全画素を展開しない）。draft の倍率は 1/2・1/4・1/8 で、目標の大きさ以上に収まる倍率を選ぶので
        # 目標は 1/k0 にする（以前の目標だと 1.6 億画素未満は縮まらず、4,800 万画素で 192MB 使っていた。レビュー 3 周目）
        k0 = int(math.ceil(math.sqrt(w * h / float(MAX_IMAGE_PIXELS))))
        im.draft(im.mode if im.mode in ('RGB', 'L', 'CMYK') else 'RGB', (max(1, w // k0), max(1, h // k0)))
        _draft_scale = w / float(max(1, im.size[0]))
        w, h = im.size
    # 複数ページとして扱うのは TIFF（FAX）だけ。JPEG の MPO（カメラのプレビュー画像入り）などは 1 枚目だけ（2 ページになり
    # 同じ明細を 2 回写していた。レビュー）
    n_frames = int(getattr(im, 'n_frames', 1) or 1) if im.format == 'TIFF' else 1
    if n_frames > MAX_IMAGE_FRAMES:
        raise LLMError(f'画像のページが多すぎます（{n_frames} ページ。上限 {MAX_IMAGE_FRAMES}）。見積書のページだけにしてください')
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    for i_ in range(n_frames):   # 複数ページの TIFF（FAX）は全ページを PDF のページに（2 ページ目以降を黙って捨てていた。P4）
        try:
            im.seek(i_)
        except (OSError, ValueError, EOFError) as e:
            raise LLMError(f'この画像を開けません（{i_ + 1} ページ目）: {type(e).__name__}')
        _px = im.size[0] * im.size[1]
        if _px > MAX_IMAGE_PIXELS_HARD or _px * (1 if im.mode in ('1', 'L') else 4) > MAX_IMAGE_BYTES_HARD:
            # ページごとに、展開する前に確かめる（P9）
            raise LLMError(f'画像が大きすぎます（{im.size[0]:,}×{im.size[1]:,} 画素）。縮小してから入れてください')
        try:
            # TIFF は Pillow が読み込むとき（load）に向きを直して印を消すので、向きの印は load の前に読む
            _o0 = int((im.getexif() or {}).get(0x0112) or 1) if im.format == 'TIFF' else 1
        except Exception:  # noqa: BLE001
            _o0 = 1
        try:
            im.load()
        except (OSError, ValueError, EOFError) as e:
            raise LLMError(f'この画像を開けません（{i_ + 1} ページ目）: {type(e).__name__}')
        try:
            orient = int((im.getexif() or {}).get(0x0112) or 1)   # スマホの縦撮り（EXIF の向き。P3）
        except Exception:  # noqa: BLE001
            orient = 1
        # 写し（copy）を作らずに 1 ページずつ変換して書き出し、向きは縮小してから直す（写しと向き直しで同じ画素を 3〜4 枚
        # 持ち、上限の手前の画像 1 枚で 1.1GB 使っていた。レビュー 2 周目）
        fr = im
        res = 200.0 / _draft_scale   # dpi の無い画像も、縮小して読んだぶんページの大きさは保つ（半分になっていた。レビュー 4 周目）
        dpi = im.info.get('dpi')
        try:
            xd, yd = (float(dpi[0]), float(dpi[1])) if dpi else (0.0, 0.0)
        except (TypeError, ValueError, IndexError):
            xd = yd = 0.0
        if _o0 in (5, 6, 7, 8):
            # 画素は 90 度回したのに dpi は保存したときの軸のまま（FAX の縦横比を逆の軸に直し、4 倍に歪めていた。レビュー 3 周目）
            xd, yd = yd, xd
        if _draft_scale > 1.0:
            xd, yd = xd / _draft_scale, yd / _draft_scale   # 縮小して読んだぶん、ページの大きさは保つ
        if fr.mode == '1' and ((xd > 0 and yd > 0 and abs(xd - yd) > 1.0) or fr.width * fr.height > MAX_IMAGE_PIXELS):
            fr = fr.convert('L')   # 縮小・伸長は白黒のままではできないのでグレーに（RGB にはしない）
        if xd > 0 and yd > 0 and abs(xd - yd) > 1.0:   # FAX の標準モード（204×98dpi）は縦が半分に潰れるので正方画素に直す
            if xd > yd:
                fr = fr.resize((fr.width, max(1, round(fr.height * xd / yd))))
            else:
                fr = fr.resize((max(1, round(fr.width * yd / xd)), fr.height))
            res = max(xd, yd)
        elif xd > 0:
            res = xd
        if fr.width * fr.height > MAX_IMAGE_PIXELS:
            # 整数分の 1 に縮める（reduce は縮めた先の画像の分しかメモリを使わない。resize は途中の画像も持ち、4,900 万画素の
            # 画像 1 枚で 500MB 使っていた。レビュー 2 周目）。reduce が扱えない形式（パレット・CMYK など）は先に RGB(A) に
            k_ = int(math.ceil(math.sqrt(fr.width * fr.height / float(MAX_IMAGE_PIXELS))))
            if fr.mode not in ('L', 'LA', 'RGB', 'RGBA'):
                fr = fr.convert('RGBA' if (fr.mode in ('P', 'PA') and ('transparency' in fr.info or fr.mode == 'PA')) else 'RGB')
            fr = fr.reduce(k_)
            res = res / k_
        if n_frames == 1 and fr is not im:
            im.close()   # 1 枚の画像は、縮小などで作り直したら元の画素をすぐ手放す
        _tp = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
               5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
               8: Image.Transpose.ROTATE_90}.get(orient)
        if _tp is not None:
            fr = fr.transpose(_tp)   # ImageOps.exif_transpose と同じ向き直し（縮小のあとで。横倒しのまま AI に渡していた。P3）
            if n_frames == 1:
                im.close()   # 透過と向きが両方ある画像で、元・回した写し・合成で 3 枚持っていた（レビュー 4 周目）
        if fr.mode in ('RGBA', 'LA') or (fr.mode == 'P' and 'transparency' in fr.info):   # 透過は白地に（黒地で文字が消えていた。P16）
            if fr.mode != 'RGBA':
                fr = fr.convert('RGBA')
                if n_frames == 1:
                    im.close()
            bg = Image.new('RGB', fr.size, (255, 255, 255))
            bg.paste(fr, (0, 0), fr)   # 帯に分けずに透過のまま合成（写しと帯で同じ画素を 4 枚持ち、HEAD の 2 倍使っていた。レビュー 3 周目）
            fr = bg
            if n_frames == 1:
                im.close()
        elif fr.mode not in ('RGB', 'L', '1'):
            fr = fr.convert('RGB')
        pbuf = io.BytesIO()
        fr.save(pbuf, format='PDF', resolution=max(72.0, float(res or 200.0)))
        del fr
        writer.append(PdfReader(io.BytesIO(pbuf.getvalue())))
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def document_block(pdf_bytes: bytes) -> dict:
    return {'type': 'document',
            'source': {'type': 'base64', 'media_type': 'application/pdf',
                       'data': base64.standard_b64encode(pdf_bytes).decode('ascii')}}


def parse_json_reply(text: str) -> dict:
    """LLM の返事から JSON を取り出す。コードフェンスや前置きが付いていても最初の { から最後の } を読む"""
    t = (text or '').strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        v = json.loads(t)
    except ValueError:
        i, j = t.find('{'), t.rfind('}')
        if i < 0 or j <= i:
            raise LLMReplyError('返事に JSON が無い: ' + t[:80])   # 返事の先頭（顧客名を含み得る）は短く（G14）
        try:
            v = json.loads(t[i:j + 1])
        except ValueError as e:
            raise LLMReplyError(f'返事の JSON が壊れている（{e}）: ' + t[:80])
    if not isinstance(v, dict):
        raise LLMReplyError('返事の JSON がオブジェクトでない')
    return v


DEFAULT_GEMINI_MODEL = os.environ.get('NEO_READER_GEMINI_MODEL') or 'gemini-3.5-flash'


def is_timeout_error(e) -> bool:
    """締め切り切れか（クライアント側の httpx.TimeoutException、サーバー側の 504 DEADLINE_EXCEEDED）。送り直しても
    また締め切りまで待つだけなので送り直さない。接続の失敗（ConnectError「Connection timed out」など）は含めない
    （送り直せば通ることがある。文言の「timed out」で見分けると取り違える。レビュー 3 周目）"""
    try:
        import httpx
        if isinstance(e, httpx.TimeoutException):
            return True
    except ImportError:  # pragma: no cover
        pass
    return getattr(e, 'code', None) == 504 or 'DEADLINE_EXCEEDED' in str(e).upper()


class GeminiReader:
    """Gemini API 版の読み手。ClaudeReader と同じ ask(system, blocks) を持ち、reader.py からは区別されない。
    指示文・PDF の渡し方（1 ページずつ）・出力（JSON だけ）・検算と読み直しのループは Claude 版と同じ。
    違うのは API の呼び方だけ: system は system_instruction、document ブロックは Part.from_bytes、JSON モードで返させる"""

    def __init__(self, api_key: Optional[str] = None, model: str = '', max_tokens: int = 16000,
                 timeout: float = 600.0):
        from google import genai  # 遅延 import
        from google.genai import types
        self._types = types
        kw = {'api_key': clean_api_key(api_key)} if api_key else {}
        self.client = genai.Client(http_options=types.HttpOptions(timeout=int(timeout * 1000)), **kw)
        self.model = model or DEFAULT_GEMINI_MODEL
        self.max_tokens = max_tokens
        self.calls = 0
        self.max_pdf_bytes = 45 * 1024 * 1024   # 公式は PDF 50MB・要求全体 100MB（2026-09 に確認）。超える PDF は送る前に断る（P8）

    @staticmethod
    def to_parts(blocks: list, types_mod) -> list:
        """Claude 形式のブロック（document / text）を Gemini の contents に写す（テストしやすいよう純関数）"""
        out = []
        for b in blocks:
            t = b.get('type')
            if t == 'document':
                src = b.get('source') or {}
                if src.get('type') != 'base64':
                    raise LLMError('Gemini 版は base64 の document ブロックだけ受ける')
                out.append(types_mod.Part.from_bytes(data=base64.standard_b64decode(src['data']),
                                                     mime_type=src.get('media_type') or 'application/pdf'))
            elif t == 'image':
                src = b.get('source') or {}
                out.append(types_mod.Part.from_bytes(data=base64.standard_b64decode(src['data']),
                                                     mime_type=src.get('media_type') or 'image/png'))
            elif t == 'text':
                out.append(str(b.get('text') or ''))
            else:
                raise LLMError(f'未知のブロック種別: {t!r}')
        return out

    @staticmethod
    def _status_code(e) -> Optional[int]:
        """SDK の例外から HTTP 状態コードを取る（google.genai.errors.APIError.code。無ければ None）"""
        for attr in ('code', 'status_code'):
            v = getattr(e, attr, None)
            if isinstance(v, int) and 100 <= v <= 599:
                return v
        resp = getattr(e, 'response', None)
        v = getattr(resp, 'status_code', None)
        return v if isinstance(v, int) else None

    @staticmethod
    def _fatal(msg: str) -> bool:
        """待っても通らないエラー（モデル無し・上限・不正な要求・認証）は即座に諦める（app.py の call_gemini と同じ流儀）"""
        m = msg.upper()
        return any(k in m for k in ('404', 'NOT_FOUND', '429', 'RESOURCE_EXHAUSTED', '400', 'INVALID_ARGUMENT',
                                    '401', '403', 'PERMISSION', 'UNAUTHENTICATED', 'API KEY', 'API_KEY',
                                    '413', 'PAYLOAD', 'TOO LARGE', 'EXCEEDS THE LIMIT'))

    def ask(self, system: str, blocks: list, *, cache_system: bool = True) -> LLMReply:
        """system（固定の指示文）＋ user（PDF と作業の指示）を送り、テキストを返す。
        max_tokens で切れた返事・安全性で止まった返事は使わない（壊れた JSON を検算に回さない）。
        Gemini の指示文キャッシュは対応モデルで自動（implicit caching）なので、ここでは何もしない"""
        t = self._types
        parts = self.to_parts(blocks, t)
        config = t.GenerateContentConfig(system_instruction=system, temperature=0.0,
                                         max_output_tokens=self.max_tokens, response_mime_type='application/json')
        last: Optional[Exception] = None
        r = None
        waits = (1, 2, 4, 8, 15)   # 429（分あたり上限）はページ数ぶん連続で呼ぶ経路で起きやすい: 待って続ける（G12）
        for attempt in range(len(waits) + 1):
            try:
                r = self.client.models.generate_content(model=self.model, contents=parts, config=config)
                break
            except Exception as e:  # noqa: BLE001  SDK の例外型は版で変わるので code があればそれ、無ければ文言で判断
                msg = str(e)
                code = self._status_code(e)
                busy = code == 429 or 'RESOURCE_EXHAUSTED' in msg.upper()
                if busy:
                    if attempt >= len(waits):
                        raise LLMError(f'Gemini API の利用上限（429）が続いています（{self.model}）。しばらく待ってからやり直してください: {msg[:200]}')
                elif (isinstance(code, int) and 400 <= code < 500 and code != 408) or (code is None and self._fatal(msg)):
                    # 4xx（不正な要求・大きすぎる・キー・モデル無し）は待っても通らない。6 回・30 秒待っていた（P14）
                    raise LLMError(f'Gemini API エラー（{self.model}）: {msg[:300]}')
                elif is_timeout_error(e):
                    # 締め切り切れは送り直さない（締め切り 10 分を最大 6 回・60 分待たせていた。レビュー 3 周目）
                    raise LLMError(f'Gemini の応答が時間内に返りませんでした（{self.model}）。ページ数を減らすか、'
                                   'しばらくしてからもう一度お試しください')
                last = e
                if attempt < len(waits):
                    time.sleep(waits[attempt] * (2 if busy else 1))
        if r is None:
            raise LLMError(f'Gemini API に失敗（{self.model}）: {last}')
        self.calls += 1
        try:
            text = r.text or ''
        except Exception:  # noqa: BLE001  候補が無い・複数パートで .text が使えない
            text = ''
        fin = ''
        try:
            fr = r.candidates[0].finish_reason if r.candidates else None
            fin = str(getattr(fr, 'name', fr) or '')
        except Exception:  # noqa: BLE001
            fin = ''
        um = getattr(r, 'usage_metadata', None)
        rep = LLMReply(text=text, stop_reason=fin,
                       input_tokens=int(getattr(um, 'prompt_token_count', 0) or 0),
                       output_tokens=int(getattr(um, 'candidates_token_count', 0) or 0),
                       cache_read_tokens=int(getattr(um, 'cached_content_token_count', 0) or 0),
                       request_id=str(getattr(r, 'response_id', '') or ''), model=self.model)
        if fin == 'MAX_TOKENS':
            raise LLMReplyError(f'返事が max_output_tokens（{self.max_tokens}）で切れた。ページを分けるか max_tokens を増やす', rep)
        if fin != 'STOP':
            # STOP 以外（SAFETY / RECITATION / BLOCKLIST / LANGUAGE / OTHER / UNSPECIFIED / 読めない …）は、本文が付いていても
            # 正常に終わった返事ではないので検算に回さない（Claude 版の refusal と同じ扱い）
            raise LLMReplyError(f'Gemini が正常に終わらなかった（finish_reason={fin or "不明"}）', rep,
                                retryable=fin in ('', 'OTHER', 'FINISH_REASON_UNSPECIFIED', 'MALFORMED_FUNCTION_CALL'))
        if not text.strip():
            raise LLMReplyError('Gemini から空の返事（finish_reason=STOP）', rep)
        return rep


def make_reader(kind: str, api_key: Optional[str] = None, model: str = ''):
    """読み手を作る。kind は 'claude' か 'gemini'。どちらも ask(system, blocks) を持つ"""
    k = (kind or '').lower()
    if k == 'gemini':
        return GeminiReader(api_key=api_key, model=model)
    if k == 'claude':
        return ClaudeReader(api_key=api_key, model=model)
    raise LLMError(f'未知の読み手: {kind!r}（claude / gemini）')


class ClaudeReader:
    def __init__(self, api_key: Optional[str] = None, model: str = '', max_tokens: int = 16000,
                 timeout: float = 600.0, effort: str = 'high'):
        import anthropic  # 遅延 import（Gemini だけの環境でもアプリ本体は起動できるように）
        self._anthropic = anthropic
        kw = {'timeout': timeout, 'max_retries': 2}
        if api_key:
            kw['api_key'] = clean_api_key(api_key)
        self.client = anthropic.Anthropic(**kw)
        self.model = model or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.effort = effort
        self.calls = 0
        self.max_pdf_bytes = 22 * 1024 * 1024   # 要求は 32MB まで（base64 で 4/3 倍）。超える PDF は送る前に断る（P8）

    def ask(self, system: str, blocks: list, *, cache_system: bool = True) -> LLMReply:
        """system（固定の指示文）＋ user（PDF と作業の指示）を送り、テキストを返す。
        max_tokens で切れた返事は使わない（壊れた JSON を検算に回さない）"""
        a = self._anthropic
        sys_param = [{'type': 'text', 'text': system}]
        if cache_system:
            sys_param[0]['cache_control'] = {'type': 'ephemeral'}
        try:
            r = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens, system=sys_param,
                output_config={'effort': self.effort},
                messages=[{'role': 'user', 'content': blocks}],
            )
        except a.AuthenticationError as e:
            raise LLMError('Claude API キーが無効: ' + str(getattr(e, 'message', e)))
        except a.RateLimitError as e:
            raise LLMError('Claude API の利用上限（429）。しばらく待って再実行: ' + str(getattr(e, 'message', e)))
        except a.APIStatusError as e:
            raise LLMError(f'Claude API エラー {getattr(e, "status_code", "?")}: ' + str(getattr(e, "message", e)))
        except a.APIConnectionError as e:
            raise LLMError('Claude API に接続できない: ' + str(e))
        self.calls += 1
        text = ''.join(b.text for b in r.content if getattr(b, 'type', '') == 'text')
        u = r.usage
        rep = LLMReply(text=text, stop_reason=str(r.stop_reason or ''),
                       input_tokens=int(getattr(u, 'input_tokens', 0) or 0),
                       output_tokens=int(getattr(u, 'output_tokens', 0) or 0),
                       cache_read_tokens=int(getattr(u, 'cache_read_input_tokens', 0) or 0),
                       cache_write_tokens=int(getattr(u, 'cache_creation_input_tokens', 0) or 0),
                       request_id=str(getattr(r, '_request_id', '') or ''), model=str(r.model or self.model))
        if rep.stop_reason == 'max_tokens':
            raise LLMReplyError(f'返事が max_tokens（{self.max_tokens}）で切れた。ページを分けるか max_tokens を増やす', rep)
        if rep.stop_reason == 'refusal':
            raise LLMReplyError('Claude が読み取りを断った（refusal）', rep, retryable=False)
        return rep

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
