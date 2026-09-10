# -*- coding: utf-8 -*-
"""sys.modules に偽の google.genai を差し込む。app.py は無改変のまま動かす。"""
import sys, types as _pytypes

STATE = {'responder': None, 'lister': None, 'calls': [], 'keys': []}

class _Resp:
    def __init__(self, text): self.text = text

class _Part:
    def __init__(self, data, mime_type): self.data, self.mime_type = data, mime_type
    @classmethod
    def from_bytes(cls, data=None, mime_type=None): return cls(data, mime_type)

class _GenerateContentConfig(dict):
    def __init__(self, **kw): super().__init__(**kw); self.__dict__.update(kw)

class _ModelInfo:
    def __init__(self, name, supported_actions=('generateContent',)):
        self.name = name
        self.supported_actions = list(supported_actions) if supported_actions is not None else None

class _Models:
    def __init__(self, c): self.c = c
    def list(self):
        f = STATE['lister']
        return f() if f else []
    def generate_content(self, model=None, contents=None, config=None):
        STATE['calls'].append({'model': model, 'config': config, 'key': self.c.api_key,
                               'prompt': contents[0] if isinstance(contents, list) and contents else contents})
        f = STATE['responder']
        r = f(model, contents, config, len(STATE['calls'])) if f else _Resp('{}')
        if isinstance(r, BaseException): raise r
        return r if isinstance(r, _Resp) else _Resp(r)

class Client:
    def __init__(self, api_key=None, **kw):
        self.api_key = api_key
        STATE['keys'].append(api_key)
        self.models = _Models(self)

def install():
    google = sys.modules.get('google')
    if google is None:
        google = _pytypes.ModuleType('google'); google.__path__ = []
        sys.modules['google'] = google
    genai = _pytypes.ModuleType('google.genai')
    genai.Client = Client
    types_mod = _pytypes.ModuleType('google.genai.types')
    types_mod.Part = _Part
    types_mod.GenerateContentConfig = _GenerateContentConfig
    genai.types = types_mod
    errors_mod = _pytypes.ModuleType('google.genai.errors')
    class ClientError(Exception): pass
    class ServerError(Exception): pass
    errors_mod.ClientError = ClientError; errors_mod.ServerError = ServerError
    genai.errors = errors_mod
    sys.modules['google.genai'] = genai
    sys.modules['google.genai.types'] = types_mod
    sys.modules['google.genai.errors'] = errors_mod
    google.genai = genai
    return STATE
