# -*- coding: utf-8 -*-
"""Gemini クライアントのモック。app._get_genai_client を差し替えて使う。"""
import sys, types as _t

class FakeResponse:
    def __init__(self, text):
        self.text = text

class FakeModels:
    def __init__(self, owner):
        self.owner = owner
    def list(self):
        return self.owner.list_result()
    def generate_content(self, model=None, contents=None, config=None):
        self.owner.calls.append({'model': model, 'config': config,
                                 'contents': contents})
        return self.owner.respond(model, contents, config)

class FakeClient:
    def __init__(self, api_key, responder=None, lister=None):
        self.api_key = api_key
        self.calls = []
        self._responder = responder
        self._lister = lister
        self.models = FakeModels(self)
    def list_result(self):
        if self._lister is None:
            return []
        r = self._lister()
        return r
    def respond(self, model, contents, config):
        if self._responder is None:
            return FakeResponse('{}')
        r = self._responder(model, contents, config, len(self.calls))
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, FakeResponse):
            return r
        return FakeResponse(r)

class FakeModelInfo:
    def __init__(self, name, supported_actions=('generateContent',)):
        self.name = name
        self.supported_actions = list(supported_actions) if supported_actions is not None else None

def install(app, responder=None, lister=None):
    """app._get_genai_client を差し替え、生成した FakeClient を返す"""
    holder = {}
    def _get(api_key):
        c = holder.get(api_key)
        if c is None:
            c = FakeClient(api_key, responder, lister)
            holder[api_key] = c
        return c
    app._get_genai_client = _get
    return holder
