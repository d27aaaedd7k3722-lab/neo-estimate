# -*- coding: utf-8 -*-
"""亮平さんが過去にコグニで作った NEO の山（既定 Z:\\ドキュメント、約 7,000 本）の索引を作り、案件ごとの手掛かりを一瞬で引く。

    python .claude/skills/pdf-to-neo/scripts/corpus_lookup.py build                   # 索引を作る / 足す（新しい・変わった NEO だけ読む。初回は数分）
    python .claude/skills/pdf-to-neo/scripts/corpus_lookup.py factory 09XXXXXXXX       # 工場名の過去の書き方（電話番号の数字 6 桁以上か、工場名の一部）
    python .claude/skills/pdf-to-neo/scripts/corpus_lookup.py same --car S89 --total 671653 [--compare <生成.neo>]
                                                                                        # 同じ車種・同じ合計の過去 NEO（あれば neo_compare で突き合わせ）

make_neo.py は索引があれば自動で引き、確認箇所シートに「過去 NEO」の行を足す（工場名の書き方が過去と違う・同じ案件らしい NEO がある）。
2026-09-14 に工場名の書き方と同じ案件の NEO を探すため、案件ごとに 7,000 本を数分かけて走査していた手間をなくすためのもの。

索引（<NEO_CHECK_ROOT>/_zdocs/corpus_index.json。git の外）に残すのは 車種コード・合計・課税小計・レート・工賃丸め・見積日・明細行数・工場名（業者名）だけ。
顧客名・契約者・住所・登録番号・車台番号は読まない。NEO の場所は id（パスのハッシュ）で持ち、id → パスはファイルに残さず `same` のたびに走査し直す
（ファイル名に顧客名が入っていることがある。claude_neo_pipeline/tests/corpus_scan.py と同じ扱い）。NEO の山は読むだけ。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from typing import Optional

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402

skill_env.apply()
FILES = skill_env.FILES
sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))
import neo_container as nc  # noqa: E402

VERSION = 1


def corpus_root() -> str:
    """NEO の山の場所（環境変数 NEO_CORPUS_ROOT、既定 Z:\\ドキュメント。corpus_scan.py と同じ）"""
    return os.environ.get('NEO_CORPUS_ROOT') or r'Z:\ドキュメント'


def neo_check_root() -> str:
    """案件置き場（skill_env.apply() が環境変数 NEO_CHECK_ROOT に載せる。配布版には claude_neo_pipeline/tests が無いので case_dirs は使わない）"""
    return os.environ.get('NEO_CHECK_ROOT') or os.path.join(os.path.expanduser('~'), 'Documents', 'NEO_check')


def index_path() -> str:
    return os.path.join(neo_check_root(), '_zdocs', 'corpus_index.json')


def _id(rel: str) -> str:
    return hashlib.sha1(rel.encode('utf-8')).hexdigest()[:12]


def _digits(s: str) -> str:
    return re.sub(r'\D', '', unicodedata.normalize('NFKC', str(s or '')))


def phones(text: str) -> list[str]:
    """文字列の中の電話番号（数字だけ、10〜11 桁）。'TEL 012-345-6789 / FAX 012-345-6780' → ['0123456789', '0123456780']"""
    t = unicodedata.normalize('NFKC', str(text or ''))
    out = []
    sep = r'[\s\-‐ー−–—―.(（)）]*'
    # 数字や区切りの途中から始めない（〒812-0011 の 0011 から読まない）。前後に数字が続くものも取らない
    for m in re.finditer(r'(?<!\d)(?<!\d[\-‐ー−–—―.])0\d{1,4}' + sep + r'\d{1,4}' + sep + r'\d{3,4}(?!\d)', t):   # TEL.092… は取る（検証で指摘）
        d = _digits(m.group(0))
        if 10 <= len(d) <= 11 and d not in out:
            out.append(d)
    return out


def read_one(path: str) -> dict:
    """NEO 1 本から索引の項目だけを読む（顧客・契約者・登録番号は読まない）"""
    neo = open(path, 'rb').read()
    ck = nc.find_real_cks(neo)
    dec = nc.decompress_neo(neo, ck)
    _m, entries = nc.parse_entries(neo, ck[0])
    files = nc.extract_files(dec, entries)
    cons = {}
    try:
        for k in ('AnSvIf0001.sld', 'AnSvEm0001.sld'):
            con = sqlite3.connect(':memory:')
            con.deserialize(files[k])
            cons[k] = con
        cif, em = cons['AnSvIf0001.sld'], cons['AnSvEm0001.sld']

        def one(db, sql, default=None):
            try:
                r = db.execute(sql).fetchone()
                return r[0] if r else default
            except sqlite3.Error:
                return default
        tot = em.execute('SELECT Total, SubTotal FROM Total').fetchone() or (None, None)
        st = cif.execute('SELECT wb_PriceBase, wb_Round FROM Setting').fetchone() or (None, None)
        return {'car': str(one(cif, 'SELECT CarCode FROM Car') or ''), 'total': tot[0], 'subtotal': tot[1], 'rate': st[0], 'round': st[1],
                'est_date': str(one(cif, 'SELECT EstimatedDate FROM FileInfo') or ''), 'rows': one(em, 'SELECT COUNT(*) FROM ERParts', 0),
                'factory': str(one(cif, 'SELECT ConsultantFactory FROM Insurance') or '').strip()}
    finally:
        for con in cons.values():
            con.close()


def _read_job(item):
    rid, path, mt, sz = item
    try:
        r = read_one(path)
    except Exception as e:  # noqa: BLE001  壊れた・読めない NEO は種類の名前だけ（本文にパスが入ることがある）
        r = {'error': type(e).__name__}
    r.update({'mtime': mt, 'size': sz})
    return rid, r


def load_index(path: str = '') -> dict:
    p = path or index_path()
    try:
        d = json.load(open(p, encoding='utf-8'))
        if isinstance(d, dict) and d.get('version') == VERSION and isinstance(d.get('entries'), dict):
            d['entries'] = {k: v for k, v in d['entries'].items() if isinstance(v, dict)}   # 壊れた行は捨てる
            return d
    except (OSError, ValueError):
        pass
    return {'version': VERSION, 'root': '', 'built': '', 'entries': {}}


def _scan(src: str):
    """NEO の山を走査して (id, パス, 更新日時, 大きさ) を返す。Windows では os.scandir の一覧に大きさ・更新日時が入っているので、
    ファイルごとに os.stat を呼ばない（共有ドライブ Z: で 7,000 本を 1 本ずつ問い合わせると 8 分かかった。2026-09-15）"""
    stack = [src]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.name.lower().endswith('.neo'):
                        st = e.stat(follow_symlinks=False)
                        yield _id(os.path.relpath(e.path, src)), e.path, int(st.st_mtime), st.st_size
                except OSError:
                    continue


def _walk(src: str):
    for rid, p, _mt, _sz in _scan(src):
        yield rid, p


def build(src: str = '', workers: int = 6, path: str = '') -> dict:
    """索引を作る / 足す。新しい・大きさか更新日時の変わった NEO だけ読む。消えた NEO は索引から外す"""
    from concurrent.futures import ProcessPoolExecutor
    src = src or corpus_root()
    if not os.path.isdir(src):
        raise SystemExit(f'NEO の山が見つからない: {src}（環境変数 NEO_CORPUS_ROOT で場所を指定）')
    idx = load_index(path)
    if idx.get('root') and idx['root'] != _id(os.path.abspath(src)):
        if not path:   # 既定の索引を別の山で丸ごと作り直さない（--index で別の置き場所を指定する）
            raise SystemExit(f'既定の索引は別の NEO の山のもの。{src} の索引は --index <置き場所> を付けて作る')
        idx = {'version': VERSION, 'root': '', 'built': '', 'entries': {}}   # 別の山の索引は使い回さない
    ent = idx['entries']
    t0 = time.time()
    seen, todo = set(), []
    for rid, p, mt, sz in _scan(src):
        seen.add(rid)
        old = ent.get(rid)
        if old and old.get('mtime') == mt and old.get('size') == sz:
            continue
        todo.append((rid, p, mt, sz))
    gone = [k for k in ent if k not in seen]
    for k in gone:
        ent.pop(k, None)
    print(f'NEO {len(seen)} 本 / 読む {len(todo)} 本 / 消えた {len(gone)} 本', flush=True)
    if todo:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for n, (rid, r) in enumerate(pool.map(_read_job, todo, chunksize=8), 1):
                ent[rid] = r
                if n % 500 == 0:
                    print(f'  {n}/{len(todo)} {time.time() - t0:.0f}s', flush=True)
    idx.update({'root': _id(os.path.abspath(src)), 'built': time.strftime('%Y-%m-%d %H:%M'), 'entries': ent})
    p = path or index_path()
    if os.path.dirname(p):   # ファイル名だけの --index でも書ける（Codex 指摘）
        os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    json.dump(idx, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, p)
    print(f'索引: {p}（{len(ent)} 本、{time.time() - t0:.0f} 秒）', flush=True)
    return idx


def _norm_name(s: str) -> str:
    return re.sub(r'[\s　]', '', unicodedata.normalize('NFKC', str(s or ''))).upper()


def factory_forms(query: str, idx: Optional[dict] = None) -> list[dict]:
    """工場名の過去の書き方。query に 6 桁以上の数字があれば電話番号で、無ければ工場名の一部で探す。
    戻り値 [{'factory': 書き方, 'count': 本数, 'rates': {レート: 本数}, 'last': 最新の見積日}]（本数の多い順）"""
    idx = idx if idx is not None else load_index()
    q_d = _digits(query)
    q_n = _norm_name(query)
    if (len(q_d) < 6 and len(q_n) < 2) or (len(q_d) < 6 and re.fullmatch(r'[\d\-‐ー−–—―.()（）\s]+', q_n or '')):   # 数字だけで 6 桁未満は電話番号にならない
        return []
    agg: dict = {}
    for r in idx.get('entries', {}).values():
        f = r.get('factory') or ''
        if not f:
            continue
        hit = (q_d in _digits(f)) if len(q_d) >= 6 else (q_n in _norm_name(f))
        if not hit:
            continue
        a = agg.setdefault(f, {'factory': f, 'count': 0, 'rates': {}, 'last': ''})
        a['count'] += 1
        if r.get('rate'):
            a['rates'][str(r['rate'])] = a['rates'].get(str(r['rate']), 0) + 1
        a['last'] = max(a['last'], str(r.get('est_date') or ''))
    return sorted(agg.values(), key=lambda a: (-a['count'], a['factory']))


def same_case(car: str, total: int, idx: Optional[dict] = None) -> list[str]:
    """同じ車種コード・同じ合計（税込）の過去 NEO の id"""
    idx = idx if idx is not None else load_index()
    return sorted(k for k, r in idx.get('entries', {}).items() if r.get('car') == car and r.get('total') == total)


def paths_of(ids: list[str], src: str = '') -> dict:
    """id → パス（走査し直してメモリ上でだけ作る。ファイルには残さない）"""
    want = set(ids)
    return {rid: p for rid, p in _walk(src or corpus_root()) if rid in want}


def hints(est: dict, neo_path: str = '', idx: Optional[dict] = None) -> list[dict]:
    """make_neo 用: 確認箇所シートに足す「過去 NEO」の行。索引が無ければ []（手掛かりなし。合否には関わらない）"""
    idx = idx if idx is not None else load_index()
    if not idx.get('entries'):
        return []
    out = []
    ins = est.get('insurance') or {}
    fac = str(ins.get('factory') or '').strip()
    tels = []
    for t in phones(fac) + phones(est.get('issuer') or ''):
        if t not in tels:
            tels.append(t)
    for t in tels[:2]:
        forms = factory_forms(t, idx)
        if not forms:
            continue
        top = forms[0]
        rates = '・'.join(f'{k}×{v}' for k, v in sorted(top['rates'].items(), key=lambda kv: -kv[1])[:2])
        if fac and _norm_name(fac) in {_norm_name(f['factory']) for f in forms}:
            out.append({'level': '参考', 'kind': '過去 NEO', 'text': f'工場名は過去 NEO と同じ書き方（「{fac}」、電話 {t} の NEO {sum(f["count"] for f in forms)} 本）'})
        else:
            others = ' / '.join(f'「{f["factory"]}」{f["count"]} 本' for f in forms[:3])
            out.append({'level': '要確認', 'kind': '過去 NEO',
                        'text': f'電話 {t} の工場は過去 NEO で {others}（レート {rates}）。今回の工場名「{fac}」を同じ書き方にするか確かめる'})
        break
    car = total = None
    if neo_path and os.path.exists(neo_path):
        try:
            r = read_one(neo_path)
            car, total = r.get('car'), r.get('total')
        except Exception:  # noqa: BLE001  読めなければ同じ案件の検索はしない
            car = None
    if car and total:
        ids = same_case(car, int(total), idx)
        if ids:
            out.append({'level': '要確認', 'kind': '過去 NEO',
                        'text': f'過去 NEO に同じ車種（{car}）・同じ合計（{int(total):,} 円）のものが {len(ids)} 本ある（同じ案件を人が作った NEO かもしれない）。'
                                f'corpus_lookup.py same --car {car} --total {int(total)} --compare <この NEO> で突き合わせる'})
    for e in out:
        e.update({k: '' for k in ('page', 'row', 'name', 'code')})
    return out


def main(argv: list) -> int:
    import argparse
    skill_env.use_utf8_io()
    ap = argparse.ArgumentParser(description='過去 NEO の索引（工場名の書き方・同じ案件の NEO）')
    sp = ap.add_subparsers(dest='cmd', required=True)
    b = sp.add_parser('build', help='索引を作る / 足す')
    b.add_argument('--src', default='', help='NEO の山の場所（既定 NEO_CORPUS_ROOT か Z:\\ドキュメント）')
    b.add_argument('--workers', type=int, default=6)
    b.add_argument('--index', default='', help='索引の置き場所（既定 <NEO_CHECK_ROOT>/_zdocs/corpus_index.json）')
    f = sp.add_parser('factory', help='工場名の過去の書き方')
    f.add_argument('query', help='電話番号（数字 6 桁以上）か工場名の一部')
    f.add_argument('--index', default='', help='索引の置き場所（build --index で作ったもの）')
    s = sp.add_parser('same', help='同じ車種・同じ合計の過去 NEO')
    s.add_argument('--car', required=True)
    s.add_argument('--total', required=True, type=int)
    s.add_argument('--compare', default='', help='見つかった NEO と neo_compare で突き合わせる NEO（生成した NEO）')
    s.add_argument('--src', default='')
    s.add_argument('--index', default='', help='索引の置き場所（build --index で作ったもの）')
    a = ap.parse_args(argv)
    if a.cmd == 'build':
        build(a.src, a.workers, a.index)
        return 0
    idx = load_index(a.index)
    if not idx.get('entries'):
        print('索引が無い。先に corpus_lookup.py build を実行する')
        return 1
    print(f"索引 {len(idx['entries'])} 本（{idx.get('built')} 作成）")
    if a.cmd == 'factory':
        forms = factory_forms(a.query, idx)
        if not forms:
            print('見つからない')
            return 1
        for fm in forms[:10]:
            print(f"  {fm['count']:4} 本  「{fm['factory']}」  レート {fm['rates']}  最新 {fm['last']}")
        return 0
    ids = same_case(a.car, a.total, idx)
    if not ids:
        print('同じ車種・同じ合計の NEO は無い')
        return 1
    found = paths_of(ids, a.src)
    for rid in ids:
        p = found.get(rid)
        print(f'  {p or "(索引のあとで消えた・動いた)"}')
        if p and a.compare:
            import neo_compare
            print(neo_compare.report(neo_compare.compare(neo_compare.load(a.compare), neo_compare.load(p))))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
