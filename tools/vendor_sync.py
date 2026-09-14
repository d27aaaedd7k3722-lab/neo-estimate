# -*- coding: utf-8 -*-
"""vendor_sync.py — files リポジトリ（pdf-to-neo スキル＋生成器）の特定コミットを vendor/pdf_to_neo に取り込む。

アプリ（neo-estimate）は「見積 PDF を読む」段だけを自前で持ち、判断・生成・検算は
files のコードをそのまま呼ぶ（docs/pdf-to-neo_アプリ移植ガイド.md §1〜2）。
アプリ側で規則を書き直さないために、取り込みは **git のコミット単位** で固定し、
作業ツリーの未コミット変更は拾わない。

使い方（neo-estimate で）:
    python tools/vendor_sync.py --source "<files リポジトリ>" --commit <コミット ID>
    python tools/vendor_sync.py --check          # vendor が VENDOR_COMMIT.json・neo_skill/vendor.py の想定と食い違っていないか

取り込みは一時フォルダ（vendor/pdf_to_neo.tmp）に展開して検証し、揃っていたときだけ既存の vendor と入れ替える
（途中で失敗しても今使えている vendor は消えない）。取り込むと neo_skill/vendor.py の EXPECTED_COMMIT も
同じコミットに書き換える（アプリは起動時にこれと VENDOR_COMMIT.json・内容ハッシュを照合し、違えば経路を止める）。

取り込む範囲は files の make_bundle.py（配布 zip）と同じ:
  - claude_neo_pipeline/（tests/ は自己診断に要る数本だけ）
  - .claude/skills/pdf-to-neo/（scripts / reference / HANDOFF / SKILL。tests も入る）
  - _addata_db_search.py
  - 仕様書・案内（NEO_FILE_SPEC_COMPLETE.md ほか）
入れないもの: 実 NEO 由来の雛形・ダンプ、工場プロファイル（取引先名）、__pycache__、out/、STATUS_*
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
from neo_skill import vendor  # noqa: E402  sha256_tree / パスの定義を共有する

VENDOR = vendor.VENDOR_ROOT
STAMP_NAME = os.path.basename(vendor.STAMP_PATH)
VENDOR_PY = os.path.join(APP, 'neo_skill', 'vendor.py')

# make_bundle.py と同じ範囲（files 側を直したらここも合わせる）
INCLUDE_DIRS = ['.claude/skills/pdf-to-neo', 'claude_neo_pipeline']
INCLUDE_FILES = ['_addata_db_search.py', 'NEO_FILE_SPEC_COMPLETE.md', 'ADDATA_REVERSE_LOOKUP_SPEC.md',
                 'ADDATA_FULL_STRUCTURE_SPEC.md', 'README.md', 'docs/pdf-to-neo_ロードマップ.md',
                 'docs/pdf-to-neo_アプリ移植ガイド.md']
EXCLUDE_DIR_NAMES = {'__pycache__', 'out', 'evidence'}
SELFTEST_TESTS = {'unit_types.py', 'unit_consistency.py', 'unit_guards.py', 'unit_manual_rows.py',
                  'unit_settings.py', 'unit_eva_slot.py', 'unit_link_absorb.py', 'unit_frame.py',
                  'unit_insurance.py',  # files 2026-09-14〜（受付番号・代理店・アジャスター・入出庫日・修理日数）。古いコミットには無いので wanted() は「あれば入れる」
                  'unit_era.py',  # files 2026-09-14〜（元号は改元日で分ける。受付番号・代理店・アジャスター・入出庫日・修理日数）。古いコミットには無いので wanted() は「あれば入れる」
                  'unit_handoff.py', 'neo_diff.py'}
PIPELINE_TESTS = 'claude_neo_pipeline/tests/'
EXCLUDE_FILE_PREFIX = ('STATUS_',)
EXCLUDE_EXT = {'.pyc', '.log', '.neo', '.pdf'}
ALLOW_NEO = {'claude_neo_pipeline/reference/template.neo'}
EXCLUDE_FILES = {'claude_neo_pipeline/reference/neo_04011103_reference.json',
                 'claude_neo_pipeline/reference/template_04011103.neo',
                 '.claude/skills/pdf-to-neo/reference/factory_profiles.json'}
# 取り込み後に必ずあるべきもの（vendor.REQUIRED は VENDOR 基準の絶対パスなので相対に直して使う）
MUST_HAVE = [os.path.relpath(p, VENDOR).replace(os.sep, '/') for p in vendor.REQUIRED] + [
    '.claude/skills/pdf-to-neo/reference/part_code_names.json', '_addata_db_search.py',
    'claude_neo_pipeline/tests/neo_diff.py']


def wanted(rel: str) -> bool:
    """配布 zip と同じ判定。rel は '/' 区切り"""
    parts = rel.split('/')
    if any(p in EXCLUDE_DIR_NAMES for p in parts[:-1]):
        return False
    base = parts[-1]
    ext = os.path.splitext(base)[1].lower()
    if ext in EXCLUDE_EXT and rel not in ALLOW_NEO:
        return False
    if base.startswith(EXCLUDE_FILE_PREFIX) or rel in EXCLUDE_FILES:
        return False
    if rel.startswith(PIPELINE_TESTS) and base not in SELFTEST_TESTS:
        return False
    if any(rel == d or rel.startswith(d + '/') for d in INCLUDE_DIRS):
        return True
    return rel in INCLUDE_FILES


def git(source: str, *args: str) -> bytes:
    p = subprocess.run(['git', '-C', source] + list(args), capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f'git {" ".join(args)} に失敗: {p.stderr.decode("utf-8", "replace")[:400]}')
    return p.stdout


def set_expected_commit(full: str) -> None:
    """neo_skill/vendor.py の EXPECTED_COMMIT を取り込んだコミットに書き換える（改行コードは保つ）"""
    src = io.open(VENDOR_PY, encoding='utf-8', newline='').read()
    new, n = re.subn(r"^EXPECTED_COMMIT = '[0-9a-f]*'", f"EXPECTED_COMMIT = '{full}'", src, flags=re.M)
    if n != 1:
        raise RuntimeError('neo_skill/vendor.py の EXPECTED_COMMIT 行が見つからない（1 行のはず）')
    io.open(VENDOR_PY, 'w', encoding='utf-8', newline='').write(new)
    # コミット ID は長さが同じなので、同じ秒に書き換えると Python が古い .pyc を「有効」と見て古い定数を読む
    # （実際に往復テストで起きた）。vendor.py の .pyc は消して、次の import をソースから読ませる
    pyc_dir = os.path.join(os.path.dirname(VENDOR_PY), '__pycache__')
    if os.path.isdir(pyc_dir):
        for f in os.listdir(pyc_dir):
            if f.startswith('vendor.') and f.endswith('.pyc'):
                try:
                    os.remove(os.path.join(pyc_dir, f))
                except OSError:
                    pass


def _replace_retry(src: str, dst: str, attempts: int = 6, wait: float = 0.5) -> None:
    """フォルダの入れ替え。Windows では rename した直後に同じ場所へ別のフォルダを rename すると
    一時的に「アクセスが拒否されました」（WinError 5）になることがある（ハンドルの解放待ち）。少し待って繰り返す"""
    last: Exception = OSError('replace failed')
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            last = e
            time.sleep(wait * (i + 1))
    raise last


def extract(tar_bytes: bytes, dest: str) -> int:
    n = 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode='r:') as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            rel = m.name.replace(os.sep, '/')
            if not wanted(rel):
                continue
            dst = os.path.join(dest, *rel.split('/'))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with tf.extractfile(m) as src, open(dst, 'wb') as out:
                out.write(src.read())
            n += 1
    return n


def sync(source: str, commit: str) -> int:
    source = os.path.abspath(source)
    full = git(source, 'rev-parse', '--verify', commit + '^{commit}').decode().strip()
    branch = git(source, 'rev-parse', '--abbrev-ref', 'HEAD').decode().strip()
    subject = git(source, 'log', '-1', '--format=%s', full).decode('utf-8', 'replace').strip()
    # コミットの内容だけを tar で取り出す（作業ツリーの未コミット変更を混ぜない）
    tar_bytes = git(source, 'archive', '--format=tar', full)
    tmp = VENDOR + '.tmp'
    old = VENDOR + '.old'
    for d in (tmp, old):
        if os.path.isdir(d):
            shutil.rmtree(d)
    os.makedirs(tmp)
    try:
        n = extract(tar_bytes, tmp)
        missing = [m for m in MUST_HAVE if not os.path.isfile(os.path.join(tmp, *m.split('/')))]
        if missing:
            raise SystemExit('取り込みに欠けがある（コミットに入っていない）ので、今の vendor はそのまま: ' + ', '.join(missing))
        stamp = {
            'source': source, 'branch_at_sync': branch, 'commit': full, 'subject': subject,
            'synced_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'files': n, 'tree_sha256': vendor.sha256_tree(tmp),
            'note': 'アプリ側でこの配下を書き換えない。規則の修正は files（pdf-to-neo ブランチ）で行い、tools/vendor_sync.py で取り直す',
        }
        with open(os.path.join(tmp, STAMP_NAME), 'w', encoding='utf-8') as fh:
            json.dump(stamp, fh, ensure_ascii=False, indent=1)
        # 検証が済んでから入れ替える: 既存 → .old、.tmp → 本番、EXPECTED_COMMIT の書き換え、取り込み後の照合。
        # どこかで失敗したら .old と EXPECTED_COMMIT を元に戻す。.old を消すのは全部済んでから
        prev_commit = vendor.EXPECTED_COMMIT
        if os.path.isdir(VENDOR):
            _replace_retry(VENDOR, old)
        try:
            _replace_retry(tmp, VENDOR)
            set_expected_commit(full)
            vendor.EXPECTED_COMMIT = full   # このプロセスの照合にも新しい値を使う（import 時の定数は古いまま）
            vendor.readiness_error.cache_clear()
            why = vendor.readiness_error()
            if why:
                raise RuntimeError('取り込み後の照合に失敗: ' + why)
        except BaseException as exc:
            # 新しい tree をどかして .old を戻す。どかせないときは .failed に退避（消せないまま残すと
            # EXPECTED_COMMIT だけ戻って照合 NG になり、「元のまま」と嘘をつくことになる）
            restored = not os.path.isdir(old)   # .old が無い（初回取り込み）なら戻すものは無い
            if os.path.isdir(old):
                if os.path.isdir(VENDOR):
                    shutil.rmtree(VENDOR, ignore_errors=True)
                    if os.path.isdir(VENDOR):
                        failed = VENDOR + '.failed'
                        if os.path.isdir(failed):
                            shutil.rmtree(failed, ignore_errors=True)
                        try:
                            _replace_retry(VENDOR, failed)
                        except OSError:
                            pass
                if not os.path.isdir(VENDOR):
                    try:
                        _replace_retry(old, VENDOR)
                        restored = True
                    except OSError:
                        restored = False
            try:
                set_expected_commit(prev_commit)
            except (RuntimeError, OSError):
                pass
            vendor.EXPECTED_COMMIT = prev_commit
            vendor.readiness_error.cache_clear()
            if not restored:
                raise RuntimeError('取り込みに失敗し、しかも元の vendor を戻せなかった。'
                                   f'vendor/pdf_to_neo.old を手で vendor/pdf_to_neo に戻すこと（原因: {exc}）') from exc
            raise
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    if os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
    print(f'vendor/pdf_to_neo ← {source} @ {full[:12]}（{branch}）「{subject}」 {n} files')
    print(f'neo_skill/vendor.py EXPECTED_COMMIT = {full[:12]}')
    return 0


def ignored_vendor_files() -> list:
    """git が .gitignore で無視していて追跡されない vendor のファイル（__pycache__ を除く）。
    これがあると `git add vendor/` で入らず、clone 先でハッシュ照合に落ちる（*.db や test_*.py のパターンが効いてしまう）"""
    p = subprocess.run(['git', '-C', APP, 'ls-files', '--others', '--ignored', '--exclude-standard', 'vendor/pdf_to_neo'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode != 0:
        return []  # git が使えない環境では検査しない
    return [l.strip() for l in p.stdout.splitlines() if l.strip() and '__pycache__' not in l]


def index_tree_hash() -> str:
    """git の index（ステージ済み／追跡済み）の vendor を .gitattributes を効かせて一時フォルダに展開し、
    その内容ハッシュを返す（= clone 先の checkout で readiness_error が計算する値）。
    vendor が git に入っていなければ ''（検査できない）。git が無い環境も ''"""
    p = subprocess.run(['git', '-C', APP, 'ls-files', '-z', 'vendor/pdf_to_neo'], capture_output=True)
    if p.returncode != 0 or not p.stdout.strip(b'\0'):
        return ''
    tmp = tempfile.mkdtemp(prefix='vendor_index_')
    try:
        q = subprocess.run(['git', '-C', APP, 'checkout-index', f'--prefix={tmp}/', '-z', '--stdin'],
                           input=p.stdout, capture_output=True)
        if q.returncode != 0:
            return ''
        return vendor.sha256_tree(os.path.join(tmp, 'vendor', 'pdf_to_neo'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check() -> int:
    vendor.readiness_error.cache_clear()
    why = vendor.readiness_error()
    if why:
        print('NG:', why); return 1
    ign = ignored_vendor_files()
    if ign:
        print(f'NG: vendor のファイル {len(ign)} 件が .gitignore で無視されていて git に入らない（例 {ign[0]}）。'
              '.gitignore の `!vendor/pdf_to_neo/**` を確かめる'); return 1
    st = vendor.stamp()
    ih = index_tree_hash()
    if ih and ih != st.get('tree_sha256'):
        print('NG: git に入っている vendor（index を .gitattributes 付きで展開した内容）のハッシュが VENDOR_COMMIT.json と違う。'
              'clone 先で照合に落ちる。.gitattributes の `vendor/pdf_to_neo/** -text` と、git add し直しを確かめる'); return 1
    print(f"vendor は commit {st['commit'][:12]}（{st.get('synced_at')}）のまま、EXPECTED_COMMIT と一致・git に全部入る"
          + ('・index の内容もハッシュ一致（clone 先でも通る）' if ih else '・（git 未登録なので index の検査は省略）') + ': OK')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default='')
    ap.add_argument('--commit', default='')
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()
    if a.check:
        return check()
    if not (a.source and a.commit):
        ap.error('--source と --commit の両方が要る（--check 以外）')
    try:
        return sync(a.source, a.commit)
    except (RuntimeError, OSError) as e:
        print('NG: 取り込みに失敗したので、今の vendor と EXPECTED_COMMIT はそのまま:', e)
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
