# -*- coding: utf-8 -*-
"""make_bundle.py — 別の PC に持っていくための配布 zip を作る（スキル＋生成器＋依存ファイル＋雛形 NEO＋仕様書）。

使い方（files ディレクトリで）:
    python .claude/skills/pdf-to-neo/scripts/make_bundle.py [--out <zip パス>]

入るもの: .claude/skills/pdf-to-neo/（全部）、claude_neo_pipeline/（.py・reference/・README）、_addata_db_search.py、README.md、docs/pdf-to-neo_ロードマップ.md、
          雛形 NEO（claude_neo_pipeline/reference/template.neo = 生成器で作った顧客情報の無い雛形）、NEO_FILE_SPEC_COMPLETE.md、ADDATA_REVERSE_LOOKUP_SPEC.md、
          展開先の案内 BUNDLE_README.md
入らないもの: NEO_check（顧客情報）、claude_neo_pipeline/tests（元 PC の案件参照・実 NEO を前提にした開発用テスト。スキル同梱の scripts/tests は入る）、STATUS_*.md（開発経緯）、__pycache__、out/、ADDATA（コグニのデータは各 PC のものを使う）
展開: 任意のフォルダ（例 C:\\SHOUCHIKU8\\files）に解凍 → その files で `python .claude/skills/pdf-to-neo/scripts/env_check.py --save --install-skill`
"""
from __future__ import annotations

import argparse
import datetime
import io
import re
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402
FILES = skill_env.FILES

MACHINE_PATH_RE = re.compile(r'[A-Za-z]:[\\\\/]+Users[\\\\/]+', re.IGNORECASE)  # 同梱物に開発機のユーザーフォルダが残っていないか


def _machine_paths(pairs) -> list:
    """zip に入れる予定のテキストに、その PC 固有の絶対パス（ユーザーフォルダ）が埋まっていないか調べる。
    残ったまま配ると、別 PC で動かない・別の checkout を見てしまうので、見つけたら zip を作らない"""
    bad = []
    for src, rel in pairs:
        if os.path.splitext(rel)[1].lower() not in ('.py', '.sh', '.md', '.json', '.ini', '.txt'):
            continue
        try:
            # newline='' を付けないと Python が単独の CR を改行に変換してしまい、
            # 行の途中に紛れた CR（README を壊したのと同じ事故）を見逃す
            txt = io.open(src, encoding='utf-8', errors='replace', newline='').read()
        except OSError:
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            if MACHINE_PATH_RE.search(line) and 'expanduser' not in line:
                bad.append(f'{rel}:{i}: {line.strip()[:90]}')
        bad += _ctrl_chars(txt, rel)
    return bad


CTRL = tuple(chr(c) for c in (13, 12, 11, 7, 8))  # CR / FF / VT / BEL / BS（TAB と LF は正常なので見ない）


def _missing_imports(pairs) -> list:
    """同梱する .py が、同梱しないリポジトリ内モジュールを import していないか。
    テストだけ入れて相棒のモジュールを外すと、配布先で ImportError になって全部落ちる"""
    import ast
    have = {os.path.splitext(os.path.basename(rel))[0] for _p, rel in pairs if rel.endswith('.py')}
    # リポジトリにあるがこの zip に入れない .py（＝配布先で import できないもの）
    repo = set()
    for base in ('claude_neo_pipeline', os.path.join('claude_neo_pipeline', 'tests'), '.'):
        d = os.path.join(FILES, base)
        if os.path.isdir(d):
            repo |= {os.path.splitext(f)[0] for f in os.listdir(d) if f.endswith('.py')}
    missing = repo - have
    bad = []
    for path, rel in pairs:
        if not rel.endswith('.py'):
            continue
        try:
            tree = ast.parse(io.open(path, encoding='utf-8', newline='').read())
        except SyntaxError as e:
            bad.append(f'{rel}: 構文が壊れている（{e}）')
            continue
        # 関数の中で try か「ファイルがあるか」を確かめてから import しているものは、
        # 無い環境で飛ばす作りなので許す。読み込んだ瞬間に落ちるトップレベルの import だけ止める
        guarded = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = list(ast.walk(fn))
            ok = any(isinstance(x, ast.Try) for x in body) or                 any(isinstance(x, ast.Attribute) and x.attr in ('exists', 'isfile', 'isdir') for x in body)
            if ok:
                guarded |= {id(x) for x in body if isinstance(x, (ast.Import, ast.ImportFrom))}
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split('.')[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split('.')[0]]
            if id(node) in guarded:
                continue
            for m in mods:
                if m in missing:
                    bad.append(f'{rel}:{getattr(node, "lineno", 0)}: 同梱しない {m} を import している'
                               '（無い環境で飛ばすなら try か存在確認で囲む）')
    return bad


def _ctrl_chars(txt: str, rel: str) -> list:
    """テキストに制御文字が紛れていないか調べる。Windows のパスを素の文字列に書くと
    `\r` `\f` などがエスケープとして解釈されて混入し、配った先で案内やコードが壊れる。
    改行は CRLF を LF に均してから見るので、CRLF のファイルは誤検知しない"""
    flat = txt.replace(chr(13) + chr(10), chr(10))
    out = []
    for i, line in enumerate(flat.split(chr(10)), 1):
        for ch in CTRL:
            if ch in line:
                out.append(f'{rel}:{i}: 制御文字 {ch!r} が混入している（\\ と 2 つ重ねる）: {line.strip()[:60]}')
                break
    return out


README = """# pdf-to-neo 配布パッケージ

工場見積 PDF → コグニセブン NEO 変換スキル（Claude Code 用）と生成器一式です。

## 展開と初期設定（新しい PC で 1 回）
1. この zip を任意のフォルダに解凍する（例 `C:\\SHOUCHIKU8\\files`。以後この `files` がリポジトリ root）
2. Python 3.11 以上を入れる（python.org のインストーラで可。追加パッケージは不要。3.14 まで動作確認済み）
   - インストール時に **「Add python.exe to PATH」にチェック**を入れる
   - `python` と打って Microsoft Store が開く PC は、Windows の「アプリ実行エイリアス」で python を切るか、以後 `python` の代わりに **`py -3`** を使う（例 `py -3 .claude\skills\pdf-to-neo\scripts\env_check.py --save --install-skill`）
3. Claude Code（デスクトップ or CLI）を入れておく
4. `files` で次を実行する（ADDATA とコグニセブンを自動検出し、設定を `%USERPROFILE%\\.claude\\pdf-to-neo.local.json` に保存、個人スキル領域にスキルを登録）
   ```
   python .claude\\skills\\pdf-to-neo\\scripts\\env_check.py --save --install-skill
   ```
   自動検出できないときはパスを指定する:
   ```
   python .claude\\skills\\pdf-to-neo\\scripts\\env_check.py --addata "D:\\Addata" --cogni "D:\\Audatex\\Auda7\\Bin\\AudaMenu.exe" --save --install-skill
   ```
5. Claude Code を `files` で起動し、「この PDF を NEO にして」と頼む（スキル pdf-to-neo が自動で使われる）

### その PC で正しく作れるかの確認（推奨・10 秒ほど）
```
python .claude\skills\pdf-to-neo\scripts\env_check.py --self-test
```
その PC の ADDATA を使って生成器の単体テスト 9 本（明細・塗装・骨格・装備・設定・型ゆれ・引き継ぎ文書との整合）を通す。`自己診断: すべて合格` なら、この PC が作る NEO は開発機と同じ結果になる。
`ADDATA データ版` の行も確認する（社内で版が違うと標準品番・標準指数が変わる）。

## 中身を理解するには
`CLAUDE.md`（Claude Code が起動時に自動で読む案内。2 系統の説明と環境確認）→ `README.md`（現況と使い方）→ `.claude\skills\pdf-to-neo\HANDOFF.md`（全体の地図・絶対ルール・実機で確かめた事実・これまでに踏んだ落とし穴）の順に読む。

## 案件の置き場
`%USERPROFILE%\\Documents\\NEO_check\\<案件名>\\`（顧客情報を含むので配布 zip には入れない）。変えたいときは env_check の `--neo-check` か環境変数 `NEO_CHECK_ROOT`。

## 必要なもの
- コグニセブンのデータ `Addata`（`COM` と メーカー別フォルダ `W`, `J`, `D` … があるフォルダ）。NEO 生成に必須
- コグニセブン本体（AudaMenu.exe）。生成した NEO を実機で開いて確認するときだけ必要
- Windows の `hh.exe`（標準で入っている。塗装指数の CHM 展開に使う）

## うまくいかないとき
- `ADDATA が見つかりません` → `env_check.py --addata "<Addata のパス>" --save`。`Addata` 以外の名前のフォルダや、ドライブ直下から 5 階層以上深い場所は自動検出が届かないので明示する。（自動検出の順序: コグニ本体の隣 → 固定ドライブの `Addata` / `ADDATA` / `Audatex\Addata` / 2 階層まで → 割り当て済みネットワークドライブの同じ 3 か所（5 秒で打ち切り）。ここまでで見つかった中から **データ版（COM\AnVer.DB の Number）が新しいもの**を選ぶ。1 つも無いときだけ 4 階層目まで・`Addata*` という名前まで広げる。全体の上限は 20 秒で、環境変数 `ADDATA_SCAN_SECONDS` で変えられる）
- `古い ADDATA を見てしまう` → 浅い場所（`C:\Addata` 等）で見つかった時点で決めるので、**古い `C:\Addata` を残したまま本物を深い場所に置いている PC** では古い方を選ぶ。`env_check.py` は毎回 深い場所まで調べ、選んだものより新しい ADDATA があれば `ADDATA 他の候補` の行で知らせるので、**配布後に一度は `env_check.py` を実行すること**。深い場所の新しい方を常に使いたい PC は `ADDATA_SCAN_DEEP=1` を設定する（毎回 数十秒かかる。基本は `--addata "<パス>" --save` で固定するのがおすすめ）
- 実行のたびに設定を探し直したくない → 一度 `--save` すれば `%USERPROFILE%\.claude\pdf-to-neo.local.json` に残り、以後は生成器を直接呼んでも（`python claude_neo_pipeline\\run_case.py …`）その設定が使われる
- 日本語が文字化けする → スクリプト側で UTF-8 出力に固定してあるので、ターミナルの文字コード設定を確認する
"""

INCLUDE_DIRS = ['.claude/skills/pdf-to-neo', 'claude_neo_pipeline']
INCLUDE_FILES = ['_addata_db_search.py', 'NEO_FILE_SPEC_COMPLETE.md', 'ADDATA_REVERSE_LOOKUP_SPEC.md', 'ADDATA_FULL_STRUCTURE_SPEC.md', 'README.md', 'CLAUDE.md', 'AGENTS.md', 'docs/pdf-to-neo_ロードマップ.md']  # README とロードマップ（現状と残課題）も同梱
EXCLUDE_DIR_NAMES = {'__pycache__', 'out', 'evidence'}
SELFTEST_TESTS = [  # 配布先でも動く自己診断（ADDATA だけで完結し、実機 NEO・案件フォルダを使わない）
    'unit_types.py', 'unit_consistency.py', 'unit_guards.py', 'unit_manual_rows.py',
    'unit_settings.py', 'unit_eva_slot.py', 'unit_link_absorb.py', 'unit_frame.py',
    'unit_handoff.py',  # 引き継ぎ文書（HANDOFF.md）の主張と実装・ADDATA の突き合わせ（3〜4 秒）
    'neo_diff.py',  # 上記テストが使う NEO 差分ツール
]
EXCLUDE_PIPELINE_TESTS = 'claude_neo_pipeline/tests/'  # 生成器の tests だけ除外（スキル同梱の scripts/tests は入れる）  # tests は元 PC の案件・実 NEO を前提にした開発用（スキルの回帰は scripts/regress_cases.py）
EXCLUDE_FILE_PREFIX = ('STATUS_',)
EXCLUDE_EXT = {'.pyc', '.log', '.neo', '.pdf'}  # *.neo は雛形（reference/template.neo）以外入れない（tests 配下の実 NEO は顧客情報を含む）
ALLOW_NEO = {'claude_neo_pipeline/reference/template.neo'}  # 生成器で作った顧客情報の無い雛形だけ同梱
EXCLUDE_FILES = {'claude_neo_pipeline/reference/neo_04011103_reference.json', 'claude_neo_pipeline/reference/template_04011103.neo',
                 '.claude/skills/pdf-to-neo/reference/factory_profiles.json'}  # 工場プロファイル（取引先名）は NEO_check/_profiles が置き場。reference に残っていても入れない  # 実 NEO 由来のダンプ・雛形は配布しない


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='')
    a = ap.parse_args()
    out = a.out or os.path.join(FILES, f"pdf-to-neo_bundle_{datetime.date.today().strftime('%Y%m%d')}.zip")
    tpl = os.path.join(FILES, 'claude_neo_pipeline', 'reference', 'template.neo')
    if not os.path.isfile(tpl):
        print('雛形 NEO が無い:', tpl); return 1
    pairs = []
    for d in INCLUDE_DIRS:
        for root, dirs, files in os.walk(os.path.join(FILES, d)):
            dirs[:] = [x for x in dirs if x not in EXCLUDE_DIR_NAMES]
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, FILES).replace(os.sep, '/')
                if (os.path.splitext(f)[1].lower() in EXCLUDE_EXT and rel not in ALLOW_NEO) or f.startswith(EXCLUDE_FILE_PREFIX) or rel in EXCLUDE_FILES or (rel.startswith(EXCLUDE_PIPELINE_TESTS) and os.path.basename(rel) not in SELFTEST_TESTS):
                    continue
                pairs.append((p, rel))
    leak = [rel for _p, rel in pairs if 'factory_profiles' in os.path.basename(rel) or rel.endswith('settings.local.json')]
    if leak:
        raise SystemExit(f'取引先名・ローカル設定を含むファイルが同梱対象に入っている: {leak}')  # .gitignore は見ない歩き方なので明示的に止める（Codex 監査 2026-09-12）
    for f in INCLUDE_FILES:
        p = os.path.join(FILES, f)
        if os.path.isfile(p):
            pairs.append((p, f))
        else:
            # 入口文書（CLAUDE.md / README.md / 仕様書）が欠けた zip は別 PC で読めないので作らない（Codex 監査 2026-09-12）
            raise SystemExit(f'同梱必須ファイルが無いので zip を作らない: {f}')
    bad = _machine_paths(pairs)
    bad += [f'BUNDLE_README.md:{i}: {l.strip()[:90]}' for i, l in enumerate(README.splitlines(), 1)
            if MACHINE_PATH_RE.search(l)]  # 生成する README 自体も見る
    # README も素の三重引用符なので同じ事故が起きる（実際に run_case.py の手前が壊れていた）
    bad += _ctrl_chars(README, 'BUNDLE_README.md')
    bad += _missing_imports(pairs)   # 同梱物の中で import が解決できるか（配布先での ImportError を防ぐ）
    if bad:  # 開発機のパスが残ったまま配ると別 PC で動かない・別の checkout を見てしまう
        print('同梱物に問題があるので zip を作らない:')
        for b_ in bad[:20]:
            print('   ', b_)
        return 1
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for p, rel in pairs:
            z.write(p, rel)
        z.writestr('BUNDLE_README.md', README)
    print(f'{out}  ({len(pairs)} files, {os.path.getsize(out) // 1024} KB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
