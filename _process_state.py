#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""プロセスで1つだけ持つ状態の置き場。**このモジュールは読み直してはいけない。**

## なぜ別モジュールなのか

Streamlit は `app.py` を **rerun のたびに `__main__` として実行し直す**。
つまり `app.py` のモジュール変数は、セッションごと・実行ごとに作り直される。
一方 `sys.modules` の中身はプロセス全体で共有される。

そのため「いま誰かが変換中か」を `app.py` の変数で数えると、
**別のセッションからは 0 に見える**。片方が変換している最中に、
もう片方がモジュールを読み直してしまい、走っている変換の足元で
規則が変わる（`app._sync_app_modules` が防ぎたかったのはまさにこれ）。

数を数える場所は、読み直されず、かつプロセスで1つでなければならない。
`_pdfium_lock_mod` が pdfium のロックについて同じことをしている。

**`app._APP_MODULES`（読み直す対象）に入れてはいけない。**
入れると、読み直した瞬間に数が 0 に戻って同じ穴が開く。
"""
import threading

# 読み直しと変換の取り合いを避けるための錠
module_lock = threading.RLock()

# いま走っている変換・Addata 取得の数
_active = 0


def enter():
    """変換（または Addata の取得）を始めた。"""
    global _active
    with module_lock:
        _active += 1


def leave():
    """終わった。数が負に回り込まないようにする
    （回り込むと、以後ずっと「変換中ではない」と誤認される）。"""
    global _active
    with module_lock:
        _active = max(0, _active - 1)


def busy():
    """いま走っている数。0 なら読み直してよい。"""
    with module_lock:
        return _active
