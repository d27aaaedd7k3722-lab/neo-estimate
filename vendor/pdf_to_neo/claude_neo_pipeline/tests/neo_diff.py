# -*- coding: utf-8 -*-
"""2 つの NEO の AnSvEm0001.sld / AnSvIf0001.sld 全テーブルを行単位で差分表示
usage: python neo_diff.py a.neo b.neo [table_filter]"""
import sys, os, sqlite3, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # claude_neo_pipeline（配布先でも動くように相対で解決）
import neo_container as nc


def load(p):
    neo = open(p, 'rb').read(); ck = nc.find_real_cks(neo); raw = nc.decompress_neo(neo, ck)
    mgmt, entries = nc.parse_entries(neo, ck[0]); fs = nc.extract_files(raw, entries)
    out = {}
    for k in ('AnSvEm0001.sld', 'AnSvIf0001.sld'):
        q = os.path.join(tempfile.gettempdir(), os.path.basename(p) + k + '.db'); open(q, 'wb').write(fs[k])
        c = sqlite3.connect(q); c.row_factory = sqlite3.Row; out[k] = c
    out['files'] = fs
    return out


def main(a, b, flt=''):
    A = load(a); B = load(b)
    for k in ('AnSvEm0001.sld', 'AnSvIf0001.sld'):
        ca, cb = A[k], B[k]
        for (t,) in ca.execute("select name from sqlite_master where type='table'"):
            if flt and flt.lower() not in t.lower():
                continue
            ra = ca.execute(f'select * from {t}').fetchall(); rb = cb.execute(f'select * from {t}').fetchall()
            if len(ra) != len(rb):
                print(f'## {t}: rows {len(ra)} -> {len(rb)}')
            for i, (x, y) in enumerate(zip(ra, rb)):
                d = {kk: (x[kk], y[kk]) for kk in x.keys() if x[kk] != y[kk]}
                if d:
                    print(f'{t}[{i}] {d}')
            for i in range(min(len(ra), len(rb)), max(len(ra), len(rb))):
                r = (rb if len(rb) > len(ra) else ra)[i]
                print(f'{t}[{i}] {"+" if len(rb) > len(ra) else "-"} {dict(r)}')
    for fn in ('AnSMB.txt', 'AnSvEm0001Ex.db', 'AnNote.ini'):
        fa = A['files'].get(fn, b''); fb = B['files'].get(fn, b'')
        if fa != fb:
            la = fa.decode('cp932', 'replace').splitlines(); lb = fb.decode('cp932', 'replace').splitlines()
            print(f'## {fn}: {len(la)} -> {len(lb)} lines')
            for i, (x, y) in enumerate(zip(la, lb)):
                if x != y:
                    print(f'  [{i}] {x!r}\n      {y!r}')
            for i in range(min(len(la), len(lb)), max(len(la), len(lb))):
                print(f'  [{i}] {"+" if len(lb) > len(la) else "-"} {(lb if len(lb) > len(la) else la)[i]!r}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else '')
