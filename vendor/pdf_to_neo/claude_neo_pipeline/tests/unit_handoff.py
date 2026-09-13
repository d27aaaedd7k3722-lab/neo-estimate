# -*- coding: utf-8 -*-
"""引き継ぎ文書（HANDOFF.md）に書いた主張が、実装・実データと合っているかを機械で確かめる。

文書は実装より古くなる。2026-09-12 の監査では 21 件の食い違いが見つかった。
**同じことを繰り返さないよう、文書の主張を常設テストにする**。
HANDOFF.md を直したらこのテストも直す（直せない主張は、そもそも書かないほうがよい）。

実機 NEO（%USERPROFILE%\\Documents\\NEO_check\\_eva_exp）が無い PC では、その部分だけ飛ばす。
"""
from __future__ import annotations

import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
PIPE = os.path.dirname(HERE)
ROOT = os.path.dirname(PIPE)
sys.path.insert(0, PIPE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

HANDOFF = os.path.join(ROOT, '.claude', 'skills', 'pdf-to-neo', 'HANDOFF.md')


def _read(p: str) -> str:
    with open(p, encoding='utf-8') as f:
        return f.read()


_REAL_NEO_PREFIXES = ('cogni_', 'nbox_', 'exp_')  # コグニ実機で保存した NEO の接頭辞（fixture / N-BOX 実験 / 個別実験）


def _neo_files() -> list:
    """コグニ実機で保存した NEO だけ（NEO_check/_eva_exp の cogni_* / nbox_* / exp_*）。
    生成器の出力（gen*_*.neo）やリポジトリ内の雛形は数えない（生成物で規則を裏付けると循環する。2026-09-12 に訂正）"""
    check = os.environ.get('NEO_CHECK_ROOT') or os.path.join(os.path.expanduser('~'), 'Documents', 'NEO_check')
    return sorted(p for p in glob.glob(os.path.join(check, '_eva_exp', '*.neo'))
                  if os.path.basename(p).startswith(_REAL_NEO_PREFIXES))  # 許可リスト（gen* を除くだけだと退避物・別名の生成物が混じる。Codex 指摘）


def test_handoff_exists_and_points_to_real_files():
    """HANDOFF が参照する文書・スクリプトが実在すること（引き継ぎ先が辿れなくなるのを防ぐ）"""
    assert os.path.exists(HANDOFF), 'HANDOFF.md が無い'
    txt = _read(HANDOFF)
    skill = os.path.join(ROOT, '.claude', 'skills', 'pdf-to-neo')
    for rel in ('SKILL.md', 'reference/judgment_rules.md', 'reference/reading_schema.md',
                'reference/estimate_schema.md', 'reference/format_catalog.md', 'reference/checklist.md',
                'scripts/make_neo.py', 'scripts/env_check.py'):
        assert os.path.exists(os.path.join(skill, *rel.split('/'))), f'HANDOFF の参照先が無い: {rel}'
        assert os.path.basename(rel) in txt, f'HANDOFF が {rel} に触れていない'
    for rel in ('NEO_FILE_SPEC_COMPLETE.md', 'ADDATA_REVERSE_LOOKUP_SPEC.md',
                'claude_neo_pipeline/README.md', 'claude_neo_pipeline/tests/verify_all.sh'):
        if _dev_only_in_bundle(rel):
            continue
        assert os.path.exists(os.path.join(ROOT, *rel.split('/'))), f'HANDOFF の参照先が無い: {rel}'


# HANDOFF が触れるが「リポジトリに実在しなくて当然」のもの（案件ごとの生成物・各 PC の設定・zip 生成物）
_NOT_IN_REPO = {
    'reading.json', 'estimate.json', 'inspect.json', 'reading_check.json', 'report.md',
    'BUNDLE_README.md', 'pdf-to-neo.local.json',
    'shouchiku_guideline.json',  # 社内の見積ガイドライン（PC ごと・NEO_check/_reference。git に入れない）
}
# 配布 zip（BUNDLE_README.md がある展開先）には入れない開発機専用のもの: 実機 NEO・案件フォルダを前提にする検証一式
_BUNDLE = os.path.exists(os.path.join(ROOT, 'BUNDLE_README.md'))
_DEV_ONLY = {'claude_neo_pipeline/tests/verify_all.sh', 'claude_neo_pipeline/tests/audit_cogni_files.py', 'claude_neo_pipeline/tests/corpus_scan.py'}  # make_bundle が実際に落とす相対パスだけ


def _dev_only_in_bundle(path: str) -> bool:
    """配布物の展開先で、make_bundle が同梱しない開発機専用スクリプトへの参照か（相対パスで一致するものだけ。同名の別パスは除外しない。Codex 指摘）"""
    if not _BUNDLE:
        return False
    rel = str(path).replace(chr(92), '/')
    rel = rel[2:] if rel.startswith('./') else rel  # 先頭の './' だけ剥がす（lstrip だと '../' や '/' 始まりの壊れた参照まで除外に吸い込む。Codex 指摘）
    return rel in _DEV_ONLY or ('claude_neo_pipeline/' + rel) in _DEV_ONLY  # 'tests/verify_all.sh' のようなスキル側からの相対表記も同じ 2 本だけ


def test_handoff_every_path_reference_exists():
    """HANDOFF に書かれた**すべての**パス記述（`…` で囲んだ .md / .py / .sh / .json）が実在すること。
    固定リストだけを見ていると、新しく足した壊れた参照を素通りする（Codex 指摘 2026-09-12）"""
    import re
    txt = _read(HANDOFF)
    skill = os.path.join(ROOT, '.claude', 'skills', 'pdf-to-neo')
    missing = []
    for m in re.finditer(r'`([^`\n]+)`', txt):
        t = m.group(1).strip()
        if ' ' in t or not re.search(r'\.(md|py|sh|json)$', t):
            continue                      # ここではバッククォート内の単独パスだけ（コマンド行は下の別テスト）
        rel = t.replace('\\', '/')
        if os.path.basename(rel) in _NOT_IN_REPO or _dev_only_in_bundle(rel):
            continue                      # 案件ごとの生成物・各 PC の設定・配布物に入れない開発機専用の検証
        found = any(os.path.exists(os.path.normpath(os.path.join(root, rel))) for root in (skill, ROOT))
        if not found:                     # ファイル名だけの記述はリポジトリ内を探す
            name = os.path.basename(rel)
            for dirpath, dnames, fnames in os.walk(ROOT):
                dnames[:] = [d for d in dnames if d not in ('.git', '.venv', '__pycache__', 'node_modules')]
                if name in fnames:
                    found = True
                    break
        if not found:
            missing.append(t)
    assert not missing, 'HANDOFF が実在しないファイルを参照している: ' + ', '.join(missing)


def test_handoff_commands_run_in_powershell():
    """HANDOFF に載せたコマンドが、この環境の既定シェル（PowerShell）で動く書き方であること。
    `%USERPROFILE%` は PowerShell では展開されず、そのまま Python に渡って「フォルダが無い」で止まる
    （Codex 指摘 2026-09-12）"""
    import re
    for line in _read(HANDOFF).splitlines():
        t = line.strip()
        if not t.startswith('python ') and not t.startswith('bash '):
            continue
        assert '%USERPROFILE%' not in t, \
            f'コマンド行に %USERPROFILE% がある（PowerShell で展開されない。$env:USERPROFILE にする）: {t[:80]}'
        # 「利用者フォルダの絶対パス」を探す。パターンは分けて組み立てる
        # （そのまま書くと make_bundle の「開発機のパスが残っていないか」検査が自分自身を誤検知する）
        user_dir = 'C:' + chr(92) + 'Users' + chr(92)
        assert user_dir.lower() not in t.lower().replace('/', chr(92)), \
            f'コマンド行に開発機の絶対パスがある: {t[:80]}'


def test_handoff_command_paths_exist():
    """HANDOFF に載せたコマンド行が指すスクリプトが実在すること。
    バッククォート内の単独パスだけ見ていると、コマンド例の壊れたパスを素通りする（Codex 指摘）"""
    import shlex
    missing = []
    for line in _read(HANDOFF).splitlines():
        t = line.strip()
        if not (t.startswith('python ') or t.startswith('bash ')):
            continue
        try:
            parts = shlex.split(t.replace(chr(92), '/'), posix=True)
        except ValueError:
            parts = t.replace(chr(92), '/').split()
        for a in parts[1:]:
            if not re.search(r'\.(py|sh)$', a):
                continue
            if not os.path.exists(os.path.join(ROOT, a)) and not _dev_only_in_bundle(a):
                missing.append(a)
    assert not missing, 'HANDOFF のコマンド行が実在しないスクリプトを指している: ' + ', '.join(missing)


def test_handoff_addedfrom_table_matches_the_code():
    """HANDOFF §5-1 の表そのものが実装と合っていること。
    実装の固定文字列だけ見ていると、文書側の表が壊れてもテストが通る（Codex 指摘）"""
    txt = _read(HANDOFF)
    # AddedFrom の表: 明細にあれば 0（連動）/ 無ければ 1（パネル追加）
    assert re.search(r'\|\s*ある\s*\|\s*0（W/S 連動）', txt), \
        'HANDOFF に「明細にある → AddedFrom 0（W/S 連動）」の行が無い'
    assert re.search(r'\|\s*無い\s*\|\s*1（パネル追加）', txt), \
        'HANDOFF に「明細に無い → AddedFrom 1（パネル追加）」の行が無い'
    # 工賃印: 指数が標準と違えば '#'（AddedFrom とは独立）
    assert re.search(r'標準と違う.*`#`', txt), \
        'HANDOFF に「指数が標準と違えば工賃印 #」の記述が無い'
    assert 'AddedFrom` と無関係' in txt or 'AddedFrom` が 0 でも 1 でも' in txt, \
        'HANDOFF に「手入力は AddedFrom と独立」の断りが無い'
    # 修理方法まで一致させること
    assert '同じ修理方法' in txt, 'HANDOFF に「修理方法まで一致」の条件が無い'


def test_handoff_claims_about_the_generator():
    """HANDOFF §5 が生成器について書いていることが、実際のコードと合っていること"""
    src = _read(os.path.join(PIPE, 'estimate_to_neo.py'))
    rc = _read(os.path.join(PIPE, 'run_case.py'))
    pi = _read(os.path.join(PIPE, 'paint_index.py'))
    se = _read(os.path.join(ROOT, '.claude', 'skills', 'pdf-to-neo', 'scripts', 'skill_env.py'))
    claims = [
        ('消費税 10% 固定', re.search(r'^TAX\s*=\s*0\.10\b', src, re.M) is not None),
        ('Setting.TaxRate=10', 'TaxRate=10' in src),
        ('AddedFrom は明細にあるかだけで決まる', "'AddedFrom': 0 if (is_bankin or linked) else 1" in src),
        ('連動判定に修理方法を使う', 'linked = disp_pnl in linked_disp' in src),
        ('違反を止める関門 _linked_ng', '_linked_ng' in src),
        ('塗装パネルは部品コード昇順（手入力の塗装行は後ろ）', "sorted(_pp, key=_order)" in src and "'' if int(x.get('DisposalCode') or 0) == 9 else str(x['PartsCode'])" in src),
        ('加算基礎の標準値は ADDATA 値', 'st_std = sb if sb is not None else st' in src),
        ('真偽値の厳密読み取り _flag', 'def _flag(' in src),
        ('握り潰しの控え silent_errors', 'silent_errors' in src),
        ('不合格は .ng.neo に隔離', '.ng.neo' in rc),
        ('tolerance が効く項目の限定', 'TOL_KEYS' in rc),
        ('20.DB は 39 バイトレコード', 'range(400, len(b) - 38, 39)' in pi),
        ('20.DB の 6 バイト目がボディコード', "'body': r[5]" in pi),
        ('面積が割れていたら控える', 'def _note_areas(' in pi),
        # 工賃印は指数が標準どおりかが先に効く（AddedFrom とは独立）。HANDOFF §5-1 の表がこれに一致すること
        ("工賃印は '#' が優先", "'#' if pnl_manual else" in src),
        ('skill_env.flag', 'def flag(' in se),
        ('skill_env.normalise_flags', 'def normalise_flags(' in se),
    ]
    bad = [n for n, ok in claims if not ok]
    assert not bad, 'HANDOFF の主張と実装が食い違う: ' + ', '.join(bad)


def test_handoff_numbers_match_the_real_neo_files():
    """HANDOFF §5-1 の数値（実機 NEO 18 本 / パネル 44 行 / 連動 41 行 / 追加 3 行 /
    明細に無い連動 0 件 / 修理方法の食い違い 0 行。2026-09-12 時点）が実データと合っていること"""
    from neo_diff import load
    files = _neo_files()
    if not files:
        print('     （実機 NEO が無い PC なので飛ばす）')
        return
    n_neo = n_panel = n_linked = n_added = n_bad = n_diff = 0
    for p in files:
        try:
            con = load(p)['AnSvEm0001.sld']
            er: dict = {}
            for c, d in con.execute('SELECT PartsCode, DisposalCode FROM ERParts WHERE PartsCode IS NOT NULL'):
                er.setdefault(str(c).strip(), set()).add(int(d or 0))
            rows = list(con.execute('SELECT PartsCode, DisposalCode, AddedFrom FROM PaintingPanel'))
        except Exception:
            continue
        if not rows:
            continue
        n_neo += 1
        n_panel += len(rows)
        for c, d, af in rows:
            c, d, af = str(c).strip(), int(d or 0), int(af or 0)
            if af == 0:
                n_linked += 1
                if c not in er:
                    n_bad += 1
                elif d not in er[c]:
                    n_diff += 1
            else:
                n_added += 1
    txt = _read(HANDOFF)
    assert n_bad == 0, f'連動（AddedFrom 0）なのに明細に無い行が実機に {n_bad} 件ある（規則が崩れている）'
    assert n_diff == 0, f'連動パネルの修理方法が明細と食い違う行が実機に {n_diff} 行ある'
    # 数値は**ラベルとセットで**探し、**文書内のすべての出現**が実測と一致すること。
    # 1 か所でも一致すれば通る書き方だと、同じ数値が 2 か所にあるときに片方が古いまま素通りする
    # （2026-09-12 にわざと壊して確かめたら、実際に見逃した）
    for pat, got, label in ((r'実機 NEO (\d+) 本', n_neo, '実機 NEO の本数'),
                            (r'塗装パネル (\d+) 行', n_panel, '塗装パネルの行数'),
                            (r'連動 (\d+) 行', n_linked, '連動の行数'),
                            (r'パネル追加 (\d+) 行', n_added, 'パネル追加の行数')):
        found = [int(x) for x in re.findall(pat, txt)]
        assert found, f'HANDOFF に「{label}」の記述が無い（実測 {got}）'
        bad = sorted({x for x in found if x != got})
        assert not bad, f'HANDOFF の数値が古い: {label} は実測 {got} なのに {bad} と書いてある箇所がある'


def test_handoff_paint_area_counts():
    """HANDOFF §5-4 の「枝番 20 組 / 同一コード 258 組」が ADDATA の実データと合っていること。
    ADDATA の版が変わると数が変わるので、そのときは文書と一緒に直す"""
    from addata_vehicle_resolver import AddataVehicleResolver
    from paint_index import PaintIndex
    try:
        root = AddataVehicleResolver().root
    except Exception:
        print('     （ADDATA が無い PC なので飛ばす）')
        return
    cars = []
    for letter in sorted(os.listdir(root)):
        d = os.path.join(root, letter)
        if not os.path.isdir(d) or len(letter) != 1:
            continue
        cars += [c for c in sorted(os.listdir(d))
                 if len(c) == 3 and os.path.exists(os.path.join(d, c, f'{c}20.DB'))]
    if len(cars) < 100:
        print(f'     （20.DB を持つ車種が {len(cars)} 件しかないので飛ばす）')
        return
    dup_amb = br_amb = 0
    for car in cars:
        try:
            rows = PaintIndex(root, car)._load_20()
        except Exception:
            continue
        by: dict = {}
        for r in rows:
            by.setdefault((r['name'].replace(' ', '').strip(), r.get('body')), []).append(r)
        for v in by.values():
            if len(v) < 2 or len({x['area'] for x in v}) < 2:
                continue
            if len({x['code'] for x in v}) == 1:
                dup_amb += 1
            else:
                br_amb += 1
    txt = _read(HANDOFF)
    flat = txt.replace(',', '')
    # ここもラベルとセットで探す
    assert re.search(r'%d 組（\d+ 車種）' % br_amb, txt), \
        f'HANDOFF の「枝番で面積が違う組」が古い（実測 {br_amb} 組）'
    assert re.search(r'%d 組' % dup_amb, txt), \
        f'HANDOFF の「同一コードで面積が違う組」が古い（実測 {dup_amb} 組）'
    assert re.search(r'%d 車種を全走査' % len(cars), flat), \
        f'HANDOFF の車種数が古い（実測 {len(cars)}）'


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith('test_') and callable(f)):
        try:
            fn()
            print('ok   ' + name)
        except AssertionError as e:
            fails += 1
            print('FAIL ' + name + ': ' + str(e))
    print('unit_handoff: ' + ('all ok' if not fails else f'{fails} 件が不合格'))
    sys.exit(1 if fails else 0)
