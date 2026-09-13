# -*- coding: utf-8 -*-
"""env_check.py — 新しい PC でこのスキルが動くかを確認し、設定ファイルを作る（最初に 1 回実行）。

使い方（リポジトリの files ディレクトリで）:
    python .claude/skills/pdf-to-neo/scripts/env_check.py                 # 確認だけ
    python .claude/skills/pdf-to-neo/scripts/env_check.py --save          # 検出結果を %USERPROFILE%\\.claude\\pdf-to-neo.local.json に保存
    python .claude/skills/pdf-to-neo/scripts/env_check.py --save --install-skill   # さらに %USERPROFILE%\\.claude\\skills\\pdf-to-neo をこのフォルダへのジャンクション（不可ならコピー）にする
    python .claude/skills/pdf-to-neo/scripts/env_check.py --addata "D:\\Addata" --cogni "D:\\Audatex\\Auda7\\Bin\\AudaMenu.exe" --save   # 手で指定

確認する項目: Python 3.11+ / ADDATA（COM と W）/ コグニ本体 / 生成器の依存ファイル（claude_neo_pipeline・_addata_db_search.py・reference の DB・雛形 NEO）/ NEO_check フォルダ / hh.exe（CHM 展開）/ 書き込み可否。
最後に生成器の自己テスト（雛形 NEO の読み込みと ADDATA の 1 車種検索）を行う。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402

FILES = skill_env.FILES
NEED_FILES = [
    'claude_neo_pipeline/estimate_to_neo.py', 'claude_neo_pipeline/addata_vehicle_resolver.py', 'claude_neo_pipeline/paint_index.py',
    'claude_neo_pipeline/neo_container.py', 'claude_neo_pipeline/neo_header.py', 'claude_neo_pipeline/adas_db.py', 'claude_neo_pipeline/run_case.py',
    '_addata_db_search.py',
    'claude_neo_pipeline/reference/BANKIN.DB', 'claude_neo_pipeline/reference/T_KEI_3.DB', 'claude_neo_pipeline/reference/BOOTH.DB', 'claude_neo_pipeline/reference/Katashiki.DB',
    'claude_neo_pipeline/reference/DATAUP.DB', 'claude_neo_pipeline/reference/AnUsrTblPnt.sld', 'claude_neo_pipeline/reference/fukaetc.DB', 'claude_neo_pipeline/reference/NAIKOKUA.DB',
    'claude_neo_pipeline/reference/N_KEI.DB', 'claude_neo_pipeline/reference/N_KIHON.DB', 'claude_neo_pipeline/reference/FBANPA.DB',
    '.claude/skills/pdf-to-neo/SKILL.md', '.claude/skills/pdf-to-neo/scripts/draft_estimate.py', '.claude/skills/pdf-to-neo/scripts/inspect_estimate.py', '.claude/skills/pdf-to-neo/scripts/make_neo.py',
]


def _material_table_check(env: dict):
    """材料代割合の既定値（塗料×塗膜×高機能）は生成器同梱の reference/AnUsrTblPnt.sld から引く。
    これはコグニのユーザー設定（AudaData/AnUsrTblPnt.sld）の写しなので、この PC のコグニで値を変えていると
    生成器の既定値（見積書に材料代が無いとき）と画面が食い違う。同じかどうかを見る（NEO 生成には必須ではない）"""
    import sqlite3
    ref = os.path.join(FILES, 'claude_neo_pipeline', 'reference', 'AnUsrTblPnt.sld')
    cog = env.get('COGNI_BIN') or ''
    live = ''
    for cand in ([os.path.join(os.path.dirname(os.path.dirname(cog)), 'AudaData', 'AnUsrTblPnt.sld')] if cog else []) + [
            r'C:\Program Files (x86)\Audatex\Auda7\AudaData\AnUsrTblPnt.sld', r'C:\Program Files\Audatex\Auda7\AudaData\AnUsrTblPnt.sld']:
        if cand and os.path.exists(cand):
            live = cand
            break
    if not os.path.exists(ref):
        return False, 'reference/AnUsrTblPnt.sld が無い（材料代割合の既定値が引けない）'
    if not live:
        return True, 'コグニのユーザー設定（AudaData/AnUsrTblPnt.sld）が見つからないので比較していない'
    def _rates(p):
        c = sqlite3.connect(p)
        try:
            return sorted(tuple(r) for r in c.execute('SELECT Paint, Coat, HFPainting, MaterialRate FROM MaterialRate'))
        finally:
            c.close()
    try:
        same = _rates(ref) == _rates(live)
    except Exception as e:
        return False, f'比較できない（{e}）'  # required=False なので全体判定は落ちないが、OK とは表示しない（Codex 指摘）
    return same, ('この PC のコグニと同じ' if same else f'この PC のコグニの設定と違う（{live}）。見積書に材料代が無い案件は画面の既定値と食い違うので、reference を写し直すか paint.material_rate を書く')


def main() -> int:
    skill_env.use_utf8_io()  # 出力を PC の既定（cp932）に依存させない
    ap = argparse.ArgumentParser()
    ap.add_argument('--save', action='store_true')
    ap.add_argument('--install-skill', action='store_true')
    ap.add_argument('--addata', default='')
    ap.add_argument('--cogni', default='')
    ap.add_argument('--neo-check', default='')
    ap.add_argument('--self-test', action='store_true', help='この PC の ADDATA で生成器の単体テストを通す（10 秒ほど。配布先の受け入れ確認用）')
    a = ap.parse_args()
    if a.addata:
        os.environ['ADDATA_ROOT'] = a.addata
    if a.cogni:
        os.environ['COGNI_BIN'] = a.cogni
    if a.neo_check:
        os.environ['NEO_CHECK_ROOT'] = a.neo_check
    ok = True

    def row(label, good, detail, required=True):
        """required=False の項目は NEO 生成に必須ではないので、欠けても保存・スキル登録は止めない（コグニ本体・hh.exe）"""
        nonlocal ok
        mark = 'OK' if good else ('NG' if required else '--')
        print(f"[{mark}] {label:<16} {detail}")
        if required:
            ok = ok and good

    row('Python', sys.version_info >= (3, 11), f'{sys.version.split()[0]}（3.11 以上が必要）')
    row('リポジトリ', os.path.isdir(os.path.join(FILES, 'claude_neo_pipeline')), FILES)
    missing = [f for f in NEED_FILES if not os.path.exists(os.path.join(FILES, f))]
    row('依存ファイル', not missing, '揃っている' if not missing else f'無い: {missing[:6]}{" …" if len(missing) > 6 else ""}')
    env = skill_env.resolve(save=False)
    _mat_good, _mat_detail = _material_table_check(env)
    row('材料代割合表', _mat_good, _mat_detail, required=False)
    _g = os.path.join(env.get('NEO_CHECK_ROOT') or '', '_reference', 'shouchiku_guideline.json')
    row('見積ガイドライン', os.path.isfile(os.environ.get('PDF_TO_NEO_GUIDELINE') or _g),
        ('ある（材料代割合の既定 = ガイドライン表の 6500〜 列）' if os.path.isfile(os.environ.get('PDF_TO_NEO_GUIDELINE') or _g)
         else f'無い（{_g}）。材料代割合の既定がコグニ既定になる。開発機の NEO_check/_reference からコピーする'), required=False)
    row('ADDATA', bool(env['ADDATA_ROOT']), env['ADDATA_ROOT'] or 'C:\\Addata 等が見つからない → --addata で指定')
    if env['ADDATA_ROOT']:
        def _count_makers():  # ネットワーク上の ADDATA だと listdir が返らないことがあるので時間制限つき
            root = env['ADDATA_ROOT']
            return (sum(1 for d in os.listdir(root) if len(d) == 1 and os.path.isdir(os.path.join(root, d))),
                    os.path.isdir(os.path.join(root, 'COM')))
        n, has_com = skill_env._bounded(_count_makers, 5.0, (-1, False))
        row('ADDATA 車種', n >= 10, (f'メーカー別フォルダ {n} 個（COM: {has_com}）' if n >= 0
                                   else '5 秒で読めない（ネットワーク上の ADDATA が応答していない可能性）'))
        # ネットワーク上の ADDATA だと AnVer.DB の読み取りが返らないことがあるので時間制限つきで読む
        ver = skill_env._bounded(lambda: skill_env.addata_version(env['ADDATA_ROOT']), 5.0, '')
        row('ADDATA データ版', bool(ver), (ver or '(AnVer.DB から読めない)') + '（PC ごとに版が違うと標準品番・標準指数が変わる。社内で揃えること）', required=False)
        # 普段の探索は「よくある置き方」で見つかった時点で決める（毎回 4 階層を走査すると遅いため）。
        # 古い C:\Addata が残ったまま本物を深い場所に置いている PC を見落とさないよう、
        # 設定を点検するこの場面だけは深い探索まで行い、選んだものより新しい ADDATA があれば知らせる
        others = skill_env._bounded(lambda: skill_env.find_addata_all(20.0), 30.0, [])
        cur = os.path.normcase(os.path.abspath(env['ADDATA_ROOT']))
        newer = [(q, v) for q, v in others
                 if os.path.normcase(os.path.abspath(q)) != cur and v and ver and v > ver]
        if newer:
            row('ADDATA 他の候補', False, ('もっと新しい版がある: ' + ', '.join(f'{q}（{v}）' for q, v in newer[:3])
                                       + ' → 使いたい方を --addata で指定して --save するか、古い方を消すこと'), required=False)
        elif len(others) > 1:
            row('ADDATA 他の候補', True, f'他に {len(others) - 1} 個あるが、選んだものが最新の版', required=False)
    row('コグニセブン', bool(env['COGNI_BIN']), env['COGNI_BIN'] or 'AudaMenu.exe が見つからない → --cogni で指定（実機確認をしないなら無くても NEO は作れる）', required=False)
    row('雛形 NEO', bool(env['NEO_TEMPLATE']), env['NEO_TEMPLATE'] or 'claude_neo_pipeline/reference/template.neo が無い')
    nc = env['NEO_CHECK_ROOT']
    if not os.path.isdir(nc):
        try:
            os.makedirs(nc, exist_ok=True)
        except OSError:
            pass
    row('NEO_check', os.path.isdir(nc), nc)
    hh = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'hh.exe')
    row('hh.exe', os.path.isfile(hh), f'{hh}（塗装指数の CHM 展開に使う。無いと塗装パネルの標準指数が取れないので paint.panels[].index を書く）', required=False)
    la = os.environ.get('LOCALAPPDATA', '')
    row('キャッシュ書込', bool(la) and os.access(la, os.W_OK), os.path.join(la, 'claude_neo_pipeline', 'chm'))
    # 自己テスト（保存・登録の前に行う: 壊れた環境の絶対パスを設定に残さない）
    if env['ADDATA_ROOT'] and env['NEO_TEMPLATE'] and not missing:
        try:
            skill_env.apply()
            sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))
            from addata_vehicle_resolver import AddataVehicleResolver  # noqa: E402
            from estimate_to_neo import NeoBuilder  # noqa: E402
            r = AddataVehicleResolver()
            res = r.resolve(model_code='JF1', serial_no='JF1-0000001', desig='17075', category='0061', reg_date='H28.10', color_code='YR586P')
            nb = NeoBuilder()
            tpl_ok = os.path.isfile(nb.template_path)
            row('自己テスト', res.get('confidence') in ('confirmed', 'high') and tpl_ok, f"車両特定 {res.get('confidence')}（N BOX JF1 → {res['neo_car'].get('CarCode')}）/ 雛形 {nb.template_path}")
        except Exception as e:  # noqa: BLE001
            row('自己テスト', False, f'例外: {e}')
    else:
        row('自己テスト', False, 'ADDATA・雛形・依存ファイルのどれかが無いので実行しない')
    if a.save and ok:
        skill_env.resolve(save=True)  # NEO_TEMPLATE は保存しない（skill_env の規約: 雛形はリポジトリ同梱を毎回解決）
        print('設定を保存:', skill_env.CONFIG)
    elif a.save:
        print('NG があるので設定は保存しない（直してから --save）')
    if a.install_skill and not ok:
        print('NG があるのでスキル登録はしない')
    if a.install_skill and ok:
        try:
            _install_skill()
        except Exception as e:  # noqa: BLE001  権限・使用中・壊れたリンク・copytree の shutil.Error など。手当ての仕方を出す
            ok = False  # --install-skill を頼まれて登録できていないので成功扱いにしない
            print(f'[NG] スキル登録に失敗（NEO 生成そのものには影響しない）: {type(e).__name__}: {e}')
            print('  手動で登録する: cmd /c mklink /J "%USERPROFILE%\\.claude\\skills\\pdf-to-neo" "この files のパス\\.claude\\skills\\pdf-to-neo"')
    if a.self_test and ok:
        ok = _self_test() and ok
    print('結果:', '使える' if ok else '不足あり（NG の項目を直す）')
    return 0 if ok else 1


def _install_skill() -> None:
    """個人スキル領域（%USERPROFILE% の .claude/skills/pdf-to-neo）をこのフォルダへのジャンクションにする（不可ならコピー）"""
    import shutil
    dst = os.path.join(os.path.expanduser('~'), '.claude', 'skills', 'pdf-to-neo')
    src = os.path.abspath(os.path.join(HERE, '..'))

    def make_link():
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        r = subprocess.run(['cmd', '/c', 'mklink', '/J', dst, src], capture_output=True, text=True)
        if r.returncode == 0:
            print('ジャンクション作成:', dst, '→', src)
        else:
            shutil.copytree(src, dst)
            print('ジャンクション不可のためコピー:', dst)
    if os.path.abspath(dst) == src:
        print('スキルはすでに個人スキル領域にある:', dst)
        return
    if not os.path.lexists(dst):
        make_link()
        return
    if os.path.normcase(os.path.realpath(dst)) == os.path.normcase(src):
        print('個人スキル領域のジャンクションはこのフォルダを指している:', dst)
        return
    # 別の場所を指すジャンクション、または古いコピー → 退避して作り直す（古いスクリプトを使い続けない）
    if os.path.islink(dst) or os.path.realpath(dst) != os.path.abspath(dst):
        os.rmdir(dst)  # ジャンクションは rmdir で外せる（中身は消えない）
        print('別の場所を指すジャンクションを外した:', dst)
    else:
        bak = dst + '.old'
        if os.path.exists(bak):
            shutil.rmtree(bak)
        os.rename(dst, bak)
        print('古いコピーを退避:', bak)
    make_link()


def _self_test() -> bool:
    """同梱の単体テスト（ADDATA だけで完結するもの）をこの PC の ADDATA で通す。配布先の受け入れ確認用"""
    tdir = os.path.join(FILES, 'claude_neo_pipeline', 'tests')
    # 対象は配布 zip の許可リストと同じ固定リスト（ADDATA だけで完結するもの）。
    # tests/ を丸ごと持つ開発機でも、実機フィクスチャが要る unit_struct 等を拾わないようにする
    import make_bundle
    want = [f for f in make_bundle.SELFTEST_TESTS if f.startswith('unit_')]
    missing = [f for f in want if not os.path.isfile(os.path.join(tdir, f))]
    if missing:  # 明示的に --self-test したのに検査できないのは「受け入れ確認が空回り」なので NG にする
        print(f'[NG] 自己診断           単体テストが同梱されていない: {missing}')
        return False
    names = want
    print('--- 自己診断（この PC の ADDATA で生成器の単体テスト） ---')
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    bad = []
    # 受け入れ確認は「その場で待てる時間」で終わらせる。テストごとに上限を持たせると
    # 応答しない共有・固まった CHM 展開のある PC で本数ぶん待たされるので、全体で 1 本の締切にする
    try:
        budget = float(os.environ.get('SELFTEST_SECONDS') or 180)
    except ValueError:
        budget = 180.0
    deadline = time.time() + max(10.0, budget)
    for n in names:
        left = deadline - time.time()
        if left <= 0:
            print(f'[NG] {n:<22} 全体の制限時間を使い切ったので実行していない')
            bad.append(n)
            continue
        try:  # 返らないテスト（ADDATA が応答しない・CHM 展開が固まる等）で自己診断ごと止まらないように
            r = subprocess.run([sys.executable, os.path.join(tdir, n)], cwd=FILES, env=env,
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=left)
        except subprocess.TimeoutExpired:
            print(f'[NG] {n:<22} 全体 {int(max(10.0, budget))} 秒の制限内に終わらない（打ち切り）')
            bad.append(n)
            continue
        last = (r.stdout or '').strip().splitlines()[-1:] or ['']
        print(f"[{'OK' if r.returncode == 0 else 'NG'}] {n:<22} {last[0][:70]}")
        if r.returncode != 0:
            bad.append(n)
    print('自己診断:', 'すべて合格' if not bad else f'不合格 {bad}')
    return not bad


if __name__ == '__main__':
    sys.exit(main())
