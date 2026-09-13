# -*- coding: utf-8 -*-
"""_runner.py — vendor の検算関数（reading_pages.validate_page / merge、reading_check.Checker）を別プロセスで呼ぶ。

reader.py がページごとの検算・束ね・合計欄の検算に使う。vendor のスクリプトは import 時に
skill_env.apply() で os.environ と sys.path を書き換えるので、アプリのプロセス（Streamlit は 1 プロセスを
全利用者で共有）では import せず、この小さなプロセスの中だけで済ませる。

  stdin : JSON {"op": "validate", "vendor_root": ..., "header": {...}, "page": {...}}
          JSON {"op": "merge",    "vendor_root": ..., "case_dir": ..., "force": false}
  stdout: 最後の行に  @@RESULT@@<JSON>   （vendor 側の print が混ざっても拾えるように目印を付ける）
"""
from __future__ import annotations

import json
import os
import sys

MARK = '@@RESULT@@'


def main() -> int:
    raw = sys.stdin.read()
    req = json.loads(raw)
    root = req['vendor_root']
    os.environ['REPO_ROOT'] = root  # このプロセスだけ
    for p in (os.path.join(root, '.claude', 'skills', 'pdf-to-neo', 'scripts'),
              os.path.join(root, 'claude_neo_pipeline'), root):
        sys.path.insert(0, p)
    import reading_pages  # noqa: E402  vendor
    from reading_check import Checker  # noqa: E402  vendor
    op = req.get('op')
    if op == 'validate':
        out = reading_pages.validate_page(req['header'], req['page'])
    elif op == 'merge':
        rd, msgs = reading_pages.merge(req['case_dir'], bool(req.get('force')))
        check = Checker(rd).run() if rd else {}
        out = {'reading': rd, 'messages': list(msgs), 'check': check}
    else:
        raise SystemExit(f'unknown op: {op!r}')
    sys.stdout.write('\n' + MARK + json.dumps(out, ensure_ascii=False, default=str) + '\n')
    sys.stdout.flush()
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
