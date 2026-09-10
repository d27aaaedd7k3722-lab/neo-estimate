# ROOT は zz_h.py と同じ決め方（XROOT があればそのツリー）。
# 開発環境の絶対パスを直書きすると、他のPCでは 1 本もテストが動かない。
import ast, os, re, sys, types
ROOT = os.environ.get("XROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
tree = ast.parse(src)
lines = src.split('\n')
want_fn = {'_normalize_number_text','safe_int','parse_csv_to_items','_is_total_row_name',
           '_build_column_map','cp932_trim','jpy_round','_normalize_date8','to_halfwidth_katakana','normalize_name','_strip_control_chars'}
pieces = ["import re, csv, io, unicodedata, math\nfrom decimal import Decimal, ROUND_HALF_UP\n"]
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in want_fn:
        pieces.append('\n'.join(lines[node.lineno-1:node.end_lineno]))
    elif isinstance(node, ast.Assign):
        tgt = node.targets[0]
        nm = getattr(tgt,'id',None)
        if nm and (nm.startswith('_TOTAL') or nm.startswith('_COLUMN') or nm in ('TAX_RATE','FULL_TO_HALF_KANA','HALF_TO_FULL_KANA')):
            pieces.append('\n'.join(lines[node.lineno-1:node.end_lineno]))
mod = types.ModuleType('h')
code = '\n\n'.join(pieces)
exec(compile(code, 'harness', 'exec'), mod.__dict__)
sys.modules['h'] = mod
