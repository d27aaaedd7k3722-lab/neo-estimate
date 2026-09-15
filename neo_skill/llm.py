# -*- coding: utf-8 -*-
"""llm.py — 見積 PDF を読む LLM（Claude API）の薄い層。

移植ガイド §3-4: このスキルで PDF を読んでいるのは Claude（PDF・画像入力）なので、同じ精度を狙って Claude API に PDF を渡す。
1 ページずつ切り出した PDF を document ブロックで送る（FAX は 1 ページずつ。§3-1）。
指示文（規則の文書 3 本）は毎回同じなので system に置き、prompt caching で 2 回目以降を安くする。

モデルは既定 claude-opus-5。環境変数 NEO_READER_MODEL で変えられる。
"""
from __future__ import annotations

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


def pdf_page_count(pdf_bytes: bytes) -> int:
    from pypdf import PdfReader
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def pdf_single_page(pdf_bytes: bytes, page_index: int) -> bytes:
    """0 起点の 1 ページだけを含む PDF を作る（文字層があればそのまま残る）"""
    from pypdf import PdfReader, PdfWriter
    r = PdfReader(io.BytesIO(pdf_bytes))
    w = PdfWriter()
    w.add_page(r.pages[page_index])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def image_to_pdf(image_bytes: bytes) -> bytes:
    """写真（JPG/PNG/WebP、pillow-heif があれば HEIC/HEIF も）で来た見積書を 1 ページの PDF にする。
    Claude API の画像入力は JPEG/PNG/GIF/WebP だけなので、iPhone の HEIC は Pillow で開ける必要がある"""
    from PIL import Image, UnidentifiedImageError
    try:
        import pillow_heif  # 任意（HEIC/HEIF を開くための追加パッケージ。無ければ JPG/PNG/PDF だけ）
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    try:
        im = Image.open(io.BytesIO(image_bytes))
        im.load()
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise LLMError('この画像を開けません（HEIC/HEIF は pillow-heif を入れると使えます。'
                       f'JPG・PNG・PDF に変換してから入れてください）: {type(e).__name__}')
    if im.mode not in ('RGB', 'L'):
        im = im.convert('RGB')
    buf = io.BytesIO()
    im.save(buf, format='PDF', resolution=200.0)
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
            raise LLMError('返事に JSON が無い: ' + t[:80])   # 返事の先頭（顧客名を含み得る）は短く（G14）
        try:
            v = json.loads(t[i:j + 1])
        except ValueError as e:
            raise LLMError(f'返事の JSON が壊れている（{e}）: ' + t[:80])
    if not isinstance(v, dict):
        raise LLMError('返事の JSON がオブジェクトでない')
    return v


DEFAULT_GEMINI_MODEL = os.environ.get('NEO_READER_GEMINI_MODEL') or 'gemini-3.5-flash'


class GeminiReader:
    """Gemini API 版の読み手。ClaudeReader と同じ ask(system, blocks) を持ち、reader.py からは区別されない。
    指示文・PDF の渡し方（1 ページずつ）・出力（JSON だけ）・検算と読み直しのループは Claude 版と同じ。
    違うのは API の呼び方だけ: system は system_instruction、document ブロックは Part.from_bytes、JSON モードで返させる"""

    def __init__(self, api_key: Optional[str] = None, model: str = '', max_tokens: int = 16000,
                 timeout: float = 600.0):
        from google import genai  # 遅延 import
        from google.genai import types
        self._types = types
        kw = {'api_key': api_key} if api_key else {}
        self.client = genai.Client(http_options=types.HttpOptions(timeout=int(timeout * 1000)), **kw)
        self.model = model or DEFAULT_GEMINI_MODEL
        self.max_tokens = max_tokens
        self.calls = 0

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
                                    '401', '403', 'PERMISSION', 'UNAUTHENTICATED', 'API KEY', 'API_KEY'))

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
                elif (code in (400, 401, 403, 404)) or (code is None and self._fatal(msg)):
                    raise LLMError(f'Gemini API エラー（{self.model}）: {msg[:300]}')
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
            raise LLMError(f'返事が max_output_tokens（{self.max_tokens}）で切れた。ページを分けるか max_tokens を増やす')
        if fin != 'STOP':
            # STOP 以外（SAFETY / RECITATION / BLOCKLIST / LANGUAGE / OTHER / UNSPECIFIED / 読めない …）は、本文が付いていても
            # 正常に終わった返事ではないので検算に回さない（Claude 版の refusal と同じ扱い）
            raise LLMError(f'Gemini が正常に終わらなかった（finish_reason={fin or "不明"}）')
        if not text.strip():
            raise LLMError('Gemini から空の返事（finish_reason=STOP）')
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
            kw['api_key'] = api_key
        self.client = anthropic.Anthropic(**kw)
        self.model = model or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.effort = effort
        self.calls = 0

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
            raise LLMError(f'返事が max_tokens（{self.max_tokens}）で切れた。ページを分けるか max_tokens を増やす')
        if rep.stop_reason == 'refusal':
            raise LLMError('Claude が読み取りを断った（refusal）')
        return rep
