"""pdfium(pypdfium2) 用の共有ロック。

`streamlit run app.py` では app.py は `__main__` として実行されるため、
`from app import _PDFIUM_LOCK` は app.py をもう一度読み込んで別の Lock を
返してしまう。両者が同じロックを確実に共有できるよう、ロックだけを
この小さなモジュールに置く。
"""
import threading

PDFIUM_LOCK = threading.Lock()
