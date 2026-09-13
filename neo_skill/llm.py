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
            raise LLMError('返事に JSON が無い: ' + t[:200])
        try:
            v = json.loads(t[i:j + 1])
        except ValueError as e:
            raise LLMError(f'返事の JSON が壊れている（{e}）: ' + t[:200])
    if not isinstance(v, dict):
        raise LLMError('返事の JSON がオブジェクトでない')
    return v


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
