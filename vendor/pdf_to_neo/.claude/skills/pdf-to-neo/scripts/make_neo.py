# -*- coding: utf-8 -*-
"""make_neo.py — 案件フォルダを渡すと reading.json → estimate.json → 突合せ → NEO 生成 → 検算 → 印の照合 → 報告文 → 納品コピー まで 1 コマンドで行う。

使い方（files ディレクトリで）:
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/make_neo.py <案件フォルダ(NEO_check 配下)> [--deliver <案件フォルダ(Z:)>] [--name <NEO 名>]
        [--force-draft] [--force-pages] [--allow-neo-total] [--skip-check] [--skip-inspect] [--no-report] [--no-profile] [--open]

  - <案件フォルダ>/pages/（ページ単位の転記: header.json + page_N.json）があり reading.json より新しければ、reading_pages.py merge で reading.json を作る（ページ不合格なら止める）
  - <案件フォルダ>/reading.json があれば、まず reading_check.py で紙の上の検算（小計・数量×単価・費用の集計先・左右・工場設定）を行い、FAIL があれば止める（--skip-check で無視）
  - 続けて draft_estimate.py で estimate.json を作る（estimate.json が既にあり reading.json より新しければ再生成しない。--force-draft で強制）
  - inspect_estimate.py を実行して要確認を表示（報告 JSON は <案件フォルダ>/inspect.json）
  - run_case.py で NEO を生成し検算。run_case が合格（exit 0 = 項目差分が totals.tolerance 内・合計一致）かつ「見積書合計との一致: OK」かつ未照合行なし のときだけ合格。
    totals.neo_total で通す案件（工場書式の円未満計上）は --allow-neo-total
  - 装備監査: 生成器が選んだ装備で決まる標準品番と印字品番を突き合わせ、別の装備なら一致する行があれば ★ で警告（option_audit.py）
  - 印の照合: reading の行に印字の印（$ # * / 短縮記法の flags 列）があれば、生成 NEO の WageByManual と突き合わせて違いを表示（コグニ印刷の再現度）
  - 報告文: <案件フォルダ>/report.md に 車両・合計表・手入力行・判断点・要確認 をまとめる（亮平さんへの報告の下書き）
  - 確認箇所シート: <案件フォルダ>/<name>_確認箇所.xlsx（openpyxl が無ければ .csv）（転記メモ・数量や部品コードを直した行・標準価格と違う行・手入力の行・★）。
    NEO の明細コメントには人向けのメモを書かず、このシートで渡す（review_sheet.py。openpyxl が無ければ CSV）
  - 合格し --deliver があれば <deliver>/<name>_claude.neo と <name>_claude_確認箇所.xlsx（または .csv）にコピー（既存ファイルは上書きせず _2, _3 … を付ける）
  - 合格したら工場の設定（レート・丸め・書式・費用の集計先）を <NEO_CHECK_ROOT>/_profiles/factory_profiles.json に記録（PC ごと・git に入れない。--no-profile で記録しない）
終了コード: 0 合格 / 1 不合格（検算差・未照合・例外）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Optional

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402
ENV = skill_env.apply()  # ADDATA / コグニ / NEO_check / 雛形（PC ごとの設定ファイルと自動検出）
FILES = skill_env.FILES
PY = sys.executable


def run(args: list[str], cwd: str) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.run([PY] + args, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = (p.stdout or '') + (('\n[stderr]\n' + p.stderr) if p.stderr and p.returncode != 0 else '')
    return p.returncode, out


def build_rows(est: dict) -> tuple[dict, list[dict]]:
    """生成器をこのプロセスで呼んで rep（rows / totals / eva）を得る（NEO は書かない。run_case と同じ build）"""
    sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))
    from estimate_to_neo import NeoBuilder  # noqa: E402
    nb = NeoBuilder()
    _, rep = nb.build(est, est['vehicle'], hints=est.get('hints'), labor_rate=est.get('labor_rate'), est_date=est.get('est_date'), insurance=est.get('insurance'))
    return rep, rep['rows']


def mark_check(est: dict, rows: list[dict]) -> list[str]:
    """印字の印（items[]._mark）と生成行の WageByManual / PartsPriceByManual を照合。違いを文で返す
    印の意味: '$' 暫定指数（標準行、Provisional）、'#' 手入力指数、'*' 手入力工賃 または 手入力金額、'@' 板金ランク"""
    out = []
    items = est.get('items') or []
    if len(items) != len(rows):
        return [f'印の照合: items {len(items)} 行と生成行 {len(rows)} 行の数が違うので照合しない']
    for it, r in zip(items, rows):
        m = ''.join(ch for ch in str(it.get('_mark') or '') if ch in '$#*@')
        if not m:
            continue
        got = set()
        wbm = str(r.get('WageByManual') or '')
        if wbm:
            got.add(wbm)
        if str(r.get('PartsPriceByManual') or '') == '*':
            got.add('*')
        exp = set(m)
        if exp != got:
            out.append(f"{r.get('PartsCode') or '----'} {str(r.get('PartsName') or '').strip()[:18]}: 印字 {''.join(sorted(exp))} / 生成 {''.join(sorted(got)) or '(なし)'}")
    return out


def write_report(case: str, est: dict, rep: dict, rows: list[dict], inspect_json: str, run_out: str, marks: list[str], neo: str, delivered: str, check: Optional[dict] = None, audit_lines: Optional[list] = None) -> str:
    car = rep.get('car') or {}
    t = rep.get('totals') or {}
    pt = est.get('totals') or {}
    L = [f"# NEO 化報告: {os.path.basename(case)}", '']
    L.append(f"- 出力: `{neo}`" + (f"  納品: `{delivered}`" if delivered else ''))
    L.append(f"- 元資料: {est.get('source', '')}")
    L.append(f"- 車両: {str(car.get('CarNameByUser', '')).strip()} / {car.get('CarCode')} 年式 {car.get('YearCode')} ボディ {car.get('BodyCode')} グレード {car.get('GradeCode')} FVA {car.get('FVACode')} 色 {car.get('ColorCode')}（{(rep.get('vehicle') or {}).get('confidence')}）装備 {rep.get('eva_write', rep.get('eva'))}")
    L.append(f"- レバーレート {est.get('labor_rate')} 円" + (f"、工賃丸め {est.get('wage_round')} 円" if est.get('wage_round') else '') + (f"、index_policy {est.get('index_policy')}" if est.get('index_policy') else ''))
    L += ['', '| 項目 | 見積書 | 生成 |', '|---|---|---|']
    for label, k_pt, k_t in (('部品計', 'parts', 'parts'), ('工賃計', 'wage', 'wage'), ('塗装計（材料込）', 'paint_total', 'paint'), ('材料代', 'material', 'paint_material'),
                            ('内板骨格', 'frame', 'frame'), ('費用部品', 'expense_parts', 'expense_parts'), ('費用工賃', 'expense_wage', 'expense_wage'),
                            ('課税小計', 'taxable', 'subtotal'), ('消費税', 'tax', 'tax'), ('合計（税込）', 'total', 'total')):
        a = pt.get(k_pt); b = t.get(k_t)
        if a is None and not b:
            continue
        L.append(f"| {label} | {'' if a is None else f'{int(a):,}'} | {'' if b is None else f'{int(b):,}'} |")
    ok_line = next((l for l in run_out.splitlines() if '見積書合計との一致' in l), '')
    L += ['', f"- 検算: {ok_line.strip() or '（run_case の出力なし）'}", f"- 明細 {len(rows)} 行（手入力 {sum(1 for r in rows if r.get('_manual'))} 行）"]
    marks_rows = [r for r in rows if str(r.get('WageByManual') or '') in ('#', '*', '@') or str(r.get('PartsPriceByManual') or '') == '*']
    if marks_rows:
        L += ['', '## 手入力の行（工賃: `#` 手入力指数 / `*` 手入力工賃 / `@` 板金ランク、金額: `*` 手入力金額）']
        for r in marks_rows:
            tm, ts = r.get('Time'), r.get('TimeStandard')
            wm = str(r.get('WageByManual') or ''); pm = str(r.get('PartsPriceByManual') or '')
            parts_s = f" 金額 {int(r.get('PartsPriceOutTax') or 0):,}{'(*)' if pm == '*' else ''}" if (r.get('PartsPriceOutTax') or 0) > 0 else ''
            wage_s = f" 工賃 {int(r.get('WageOutTax') or 0):,}({wm}) 指数 {tm if (tm or -1) > 0 else '-'}（標準 {ts if (ts or 0) > 0 else 'なし'}）" if wm else ''
            note_s = f" ← {r['_std_note']}" if r.get('_std_note') else ''  # 生成器が付ける説明（連動・吸収込みの標準との差 / 吸収行）
            L.append(f"- {r.get('PartsCode') or '----'} {str(r.get('PartsName') or '').strip()}:{parts_s}{wage_s}{note_s}")
    notes = est.get('_draft_notes') or []
    if notes:
        L += ['', '## 下書きで判断した点（draft_estimate）'] + [f'- {n}' for n in notes]
    warn: list = []
    try:
        if inspect_json and os.path.exists(inspect_json):
            warn = json.load(open(inspect_json, encoding='utf-8-sig')).get('warnings') or []
    except Exception as e:  # noqa: BLE001  inspect.json が壊れていても報告文は作る（要確認は空になる）
        warn = [f'inspect.json を読めなかったので要確認を載せられない: {type(e).__name__}: {e}']
    if check:
        alts = [n for n in (check.get('note') or []) if '合計欄' in n and 'として一致' in n]  # 代替解釈で通した合計欄は必ず報告に出す（監査 14）
        L += ['', '## 紙上検算（reading_check）'] + ([f'- FAIL: {w}' for w in check.get('fail') or []] + [f'- {w}' for w in check.get('warn') or []] + [f'- 解釈: {n}' for n in alts] or ['- FAIL / WARN なし'])
        st = check.get('settings') or {}
        if st:
            L.append(f"- 推定した設定: レート {st.get('labor_rate')} / 工賃丸め {st.get('wage_round')} 円 / 消費税 {st.get('tax_round', '-')} / 書式 {st.get('format')}")
    L += ['', '## 要確認（inspect_estimate）'] + ([f'- {w}' for w in warn] or ['- なし'])
    L += ['', '## 印字の印との照合'] + ([f'- {m}' for m in marks] or ['- 差なし（印の指定が無い行は照合していない）'])
    if audit_lines:
        L += ['', '## 装備監査（option_audit: 印字品番 vs 装備で決まる標準品番）'] + [f'- {x.strip()}' for x in audit_lines]
    path = os.path.join(case, 'report.md')
    tmp = f'{path}.{os.getpid()}.tmp'  # 途中で落ちても前の報告を壊さない（並行実行よけに PID を入れる）
    open(tmp, 'w', encoding='utf-8').write('\n'.join(L) + '\n')
    os.replace(tmp, path)
    return path


def write_review(case: str, name: str, est: dict, rows: list[dict], rep: dict, inspect_json: str, check: Optional[dict], audit_lines: Optional[list], run_out: str,
                 extra: Optional[list] = None) -> str:
    """確認箇所シート（xlsx）を案件フォルダに書く。書いたパスを返す（review_sheet.py）"""
    import review_sheet
    warn: list = []
    if inspect_json and os.path.exists(inspect_json):
        try:
            warn = json.load(open(inspect_json, encoding='utf-8-sig')).get('warnings') or []
        except Exception as e:  # noqa: BLE001  壊れていてもシートは作る
            warn = [f'inspect.json を読めなかった: {type(e).__name__}: {e}']
    sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))
    import run_case  # noqa: E402  失敗したらシート生成の失敗として不合格にする（明細 No がずれたシートを渡さない。Codex 指摘）
    ordered = run_case._rows_in_source_order(rows)  # リサイクル置換で末尾へ動いた行も見積の並びに戻す
    entries = review_sheet.collect(est, ordered, warn, check, audit_lines, run_out, extra=extra)
    return review_sheet.write(os.path.join(case, f'{name}_確認箇所.xlsx'), entries, est, ordered, rep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('case_dir')
    ap.add_argument('--deliver', default='')
    ap.add_argument('--name', default='')
    ap.add_argument('--force-draft', action='store_true')
    ap.add_argument('--skip-inspect', action='store_true')
    ap.add_argument('--allow-neo-total', action='store_true', help='totals.neo_total/tolerance で合格させる案件（工場書式の円未満計上など）')
    ap.add_argument('--no-report', action='store_true')
    ap.add_argument('--skip-check', action='store_true', help='reading_check とページ単位検算の FAIL で止めない（理由が説明できるときだけ）')
    ap.add_argument('--no-profile', action='store_true', help='合格しても factory_profiles.json に記録しない')
    ap.add_argument('--force-pages', action='store_true', help='pages/ に不合格ページがあっても merge する（reading_pages.py merge --force と同じ。理由が説明できるときだけ）')
    ap.add_argument('--open', action='store_true', help='合格した NEO をコグニセブン（COGNI_BIN / 自動検出）で開く')
    a = ap.parse_args()
    case = os.path.abspath(a.case_dir)
    reading = os.path.join(case, 'reading.json')
    est_path = os.path.join(case, 'estimate.json')
    if not os.path.isdir(case):
        print('案件フォルダが無い:', case); return 1

    # 0a) ページ単位の転記（pages/）があれば束ねる
    import reading_pages
    if reading_pages.pages_newer_than_reading(case):
        rc, out = run([os.path.join(HERE, 'reading_pages.py'), 'merge', case] + (['--force'] if a.force_pages else []), FILES)
        print('== reading_pages merge ==')
        print(out.rstrip())
        if not os.path.exists(reading) or reading_pages.pages_newer_than_reading(case):
            print('ページの束ね（merge）ができなかった。不合格ページ・欠番・ページ番号の不一致を直して再実行（--skip-check では無視しない。理由があれば --force-pages）'); return 1
        if rc != 0 and not a.skip_check:
            print('ページ単位の検算に不合格がある。該当ページ（pages/page_N.json）を読み直して再実行'); return 1

    # 0b) 紙の上の検算（ADDATA 不要）: 小計・数量×単価・費用の集計先・左右・工場設定
    check: dict = {}
    check_json = os.path.join(case, 'reading_check.json')
    if os.path.exists(reading):
        rc, out = run([os.path.join(HERE, 'reading_check.py'), reading, '--json', check_json, '--quiet'], FILES)
        print('== reading_check ==')
        print(out.rstrip())
        try:
            check = json.load(open(check_json, encoding='utf-8-sig'))
        except (OSError, ValueError):
            check = {}
        if rc != 0 and not a.skip_check:
            print('紙上検算に FAIL がある。reading.json を直して再実行（理由が説明できるときだけ --skip-check）'); return 1

    # 1) reading.json → estimate.json
    # reading.json が estimate.json と同時刻でも下書きし直す（同秒に書かれた古い estimate.json を使わない）
    if os.path.exists(reading) and (a.force_draft or not os.path.exists(est_path) or os.path.getmtime(reading) >= os.path.getmtime(est_path)):
        rc, out = run([os.path.join(HERE, 'draft_estimate.py'), reading, est_path], FILES)
        print('== draft_estimate ==')
        print(out.rstrip())
        if rc != 0:
            print('下書き生成に失敗'); return 1
    elif not os.path.exists(est_path):
        print('reading.json も estimate.json も無い:', case); return 1
    else:
        print('estimate.json を使用（reading.json より新しいか、reading.json 無し）')

    # 2) 突合せ
    inspect_json = os.path.join(case, 'inspect.json')
    if a.skip_inspect:
        inspect_json = ''  # 今回の estimate に対する検査結果ではない古い inspect.json を報告に混ぜない
    else:
        rc, out = run([os.path.join(HERE, 'inspect_estimate.py'), est_path, '--json', inspect_json], FILES)
        print('== inspect_estimate ==')
        print(out.rstrip())
        if rc != 0:
            print('突合せに失敗'); return 1

    # 3) 生成・検算
    name = a.name or os.path.basename(case)
    neo = os.path.join(case, f'{name}.neo')
    ng_path = os.path.splitext(neo)[0] + '.ng.neo'
    # run_case はまず仮の名前に書き、make_neo の関門（照合率・意図との突き合わせ・確認箇所シート）を全部通ったときだけ <name>.neo に置き換える。
    # 前回の合格 <name>.neo は、今回が不合格なら一切触らない（Codex 指摘: run_case が直接 <name>.neo を上書きすると前回分を失う）
    stage = os.path.join(case, f'.{name}.staging.neo')
    stage_ng = os.path.splitext(stage)[0] + '.ng.neo'   # run_case の検算で落ちたときに run_case が置く名前
    for p_ in (stage, stage_ng):
        if os.path.exists(p_):
            os.remove(p_)   # 前回の実行が途中で落ちた残り（自分の仮ファイルだけ）
    rc, out = run([os.path.join(FILES, 'claude_neo_pipeline', 'run_case.py'), est_path, stage], FILES)
    print('== run_case ==')
    print(out.rstrip())
    est = json.load(open(est_path, encoding='utf-8-sig'))
    neo_total_ok = False
    if a.allow_neo_total:
        # 差を許すのは run_case と同じ 3 点セット（neo_total / tolerance / 理由）が揃い、
        # かつ run_case 自身が合格（rc==0）しているときだけ。ここを緩めると run_case の関門を素通りできてしまう
        tt = est.get('totals') or {}
        why = str(tt.get('tolerance_reason') or tt.get('neo_total_reason') or tt.get('note') or '').strip()
        try:
            tol = int(tt.get('tolerance') or 0)
        except (TypeError, ValueError):
            tol = 0
        neo_total_ok = (rc == 0 and tt.get('total') is not None and tt.get('neo_total') is not None
                        and tol > 0 and bool(why) and '見積書合計との一致' in out)
        if not neo_total_ok:
            print('--allow-neo-total: 無効。totals に total / neo_total / tolerance（正の値）/ tolerance_reason が'
                  ' 揃っていて、run_case 自身が合格している案件でだけ使える')
    unmatched_n = 0
    for ln in out.splitlines():
        if '未照合行:' in ln:
            unmatched_n = max(1, len([x for x in ln.split('未照合行:')[1].split(',') if x.strip()]))
    allow_un = int((est.get('totals') or {}).get('allow_unmatched') or 0)  # reading で明示した許容数までは run_case と同じく合格にする（監査 4）
    if unmatched_n and unmatched_n <= allow_un:
        print(f'未照合 {unmatched_n} 行を totals.allow_unmatched {allow_un} で見逃している。スキルの合格条件は未照合ゼロなので、納品コピーはしない'
              ' —— 12.DB に無い品目は manual: true にする')
    dup_line = next((l for l in out.splitlines() if '同じ部品コード' in l), '')
    if dup_line:  # コグニもこの形を保持する（実機 H24）。左右・前後の取り違えを見つけるための情報
        print(dup_line.strip())
    # ADDATA に載っている車なのに照合率が低い（明細をほとんど手入力にした）NEO は納品しない。
    # 部品コード・標準品番・部位ブロックの入らない「工場見積を書き写しただけ」の NEO になるため（判断規則 10-7）
    low_match = next((l.strip() for l in out.splitlines() if '照合できたのは' in l and '★' in l), '')
    if low_match:
        print(low_match)
    # 標準価格と合わない高額行がある NEO も納品しない（ref の前後・左右の取り違えを配ってしまうため）。
    # 品番改定などで説明が付くときだけ totals.allow_price_mismatch に件数を書いて通す
    # 前後・左右の食い違いは ref の取り違えそのもの。正当な価格差と違って言い訳が効かないので必ず止める
    side_ng = next((l.strip() for l in out.splitlines() if '前後・左右が食い違う' in l), '')
    if side_ng:
        print(side_ng)
    # 標準価格の差は **納品の可否には使わない**。純正の価格改定・色別価格・工場独自価格で
    # 正当に食い違うことが実案件で確かめられている（C-HR のディスクホイール 65,600 円 ×2、
    # N-ONE の Fr バンパフェイス 50,700 円、手入力の実験ケースのヘッドライト 45,000 円）。
    # 見るべき合図として出すだけにし、止めるのは前後・左右の食い違い（言い訳が効かない誤り）に絞る
    price_ng = next((l.strip() for l in out.splitlines() if '標準価格と合わない行がある' in l), '')
    if price_ng:
        print(price_ng + '  → 納品は止めないが、ref と車両を一度確かめること')
    ok = (rc == 0 and ('見積書合計との一致: OK' in out or neo_total_ok)
          and not unmatched_n and not low_match and not side_ng)  # 未照合が 1 行でもあれば合格にしない（allow_unmatched は生成を続けるためだけの逃げ道）

    # 4) 印の照合と報告文（生成器をこのプロセスで呼ぶ。NEO は書かない。失敗しても合否には影響しない）
    marks: list[str] = []
    rep: dict = {}
    rows: list[dict] = []
    audit_lines: list[str] = []
    try:
        rep, rows = build_rows(est)
        marks = mark_check(est, rows)
        if marks:
            print('== 印字の印との照合（違いのある行）==')
            for m in marks:
                print('  -', m)
    except Exception as e:  # noqa: BLE001
        print('印の照合/報告文の生成で例外（合否には影響しない）:', e)
    if rep:
        try:  # 装備監査（合否には影響しない。★ が出たら reading の hints.eva_codes を検討する）
            import option_audit
            res = option_audit.audit(est, rep)
            audit_lines = option_audit.format_lines(res)
            print('== 装備監査 ==')
            for line in audit_lines:
                print(line)
        except Exception as e:  # noqa: BLE001
            print('装備監査で例外（合否には影響しない）:', e)

    # 5) できあがった NEO を読み戻して、下書きの意図（estimate.json）と 1 行ずつ突き合わせる（intent_check.py）。
    #    行数・部品コード・数量・金額・工賃・明細コメントの有無が違えば不合格。名称や顧客名が欄で切れたものは確認箇所シートの 要確認
    intent: dict = {'hard': [], 'soft': []}
    if ok and os.path.exists(stage):
        try:
            import intent_check
            intent = intent_check.check(est, stage)
            print(f"== 意図との突き合わせ == {intent['rows']} 行 / 食い違い {len(intent['hard'])} / 要確認 {len(intent['soft'])}")
            for e in (intent['hard'] + intent['soft'])[:30]:
                print(f"  [{e['kind']}] 明細 {e['row']} {e['name']} {e['text']}")
        except Exception as e:  # noqa: BLE001  読み戻せない NEO は納品しない
            intent = {'hard': [{'level': '要確認', 'kind': '意図との突き合わせ', 'page': '', 'row': '', 'name': '', 'code': '',
                                'text': f'生成した NEO を読み戻せなかった: {type(e).__name__}: {e}'}], 'soft': []}
            print('意図との突き合わせで例外:', type(e).__name__, e)
    intent_ng = bool(intent['hard'])
    ok = ok and not intent_ng
    # 6) 過去 NEO の索引（corpus_lookup.py build で作る。無ければ何もしない）: 工場名の書き方が過去と違う・同じ案件らしい NEO がある → 確認箇所シートへ
    corpus_rev: list = []
    if ok and rep:
        try:
            import corpus_lookup
            corpus_rev = corpus_lookup.hints(est, stage if os.path.exists(stage) else '')
            for _e in corpus_rev:
                print(f"  [過去 NEO] {_e['text']}")
        except Exception as e:  # noqa: BLE001  手掛かりなので合否には影響させない
            print('過去 NEO の索引を引けなかった（合否には影響しない）:', type(e).__name__, e)
    review_path = ''
    if rep:
        try:
            review_path = write_review(case, name, est, rows, rep, inspect_json, check, audit_lines, out, intent['hard'] + intent['soft'] + corpus_rev)
            print('確認箇所シート:', review_path)
        except Exception as e:  # noqa: BLE001
            print('確認箇所シートの生成で例外:', type(e).__name__, e)
    review_ng = not review_path  # 確認点は NEO ではなくシートで渡す（判断規則 10-22）。シートが作れなければ不合格（Codex 指摘）
    ok = ok and not review_ng
    if not ok:
        # run_case が合格して <name>.neo を置いたあとに make_neo 側の関門（低照合率・確認箇所シート…）で落ちたときも、
        # 納品物の名前の NEO を残さない（run_case と同じく .ng.neo に隔離。アプリが組でない NEO を拾わないように。Codex 指摘）
        neo_for_report = ''
        src_ng = stage if os.path.exists(stage) else (stage_ng if os.path.exists(stage_ng) else '')
        if src_ng:
            try:
                os.replace(src_ng, ng_path)
                neo_for_report = ng_path
                print('不合格なので NEO は', ng_path, 'に置いた（中身の確認用。前回の', os.path.basename(neo), 'はそのまま）')
            except OSError as e:
                neo_for_report = src_ng
                print('不合格の NEO を .ng.neo に置けなかった:', src_ng, e)
        if not a.no_report and rep:
            try:
                write_report(case, est, rep, rows, inspect_json, out, marks, neo_for_report, '', check, audit_lines)  # 不合格の報告は実際に残った NEO（.ng.neo）を指す（Codex 指摘）
            except Exception as e:  # noqa: BLE001
                print('報告文の生成で例外:', e)
        _why = []
        if rc != 0 or not ('見積書合計との一致: OK' in out or neo_total_ok):
            _why.append('検算に差がある')
        if unmatched_n:
            _why.append(f'未照合行が {unmatched_n} 行ある')
        if low_match:
            _why.append('ADDATA 照合率が低い（明細を手入力にしすぎ。判断規則 10-7）')
        if side_ng:
            _why.append('見積の名称と照合先で前後・左右が食い違う（ref の取り違え。判断規則 10-9）')
        if review_ng:
            _why.append('確認箇所シートが作れなかった（上の例外を直す。判断規則 10-22）')
        if intent_ng:
            _why.append('できあがった NEO が下書きの意図と違う（行数・部品コード・数量・金額・工賃・コメント。上の「意図との突き合わせ」）')
        print('不合格: ' + ' / '.join(_why or ['理由不明']) + '。reading/estimate を直して再実行'); return 1

    # 全部の関門を通った: 仮の NEO を <name>.neo に置き換える（ここで初めて納品物の名前になる）
    os.replace(stage, neo)
    if os.path.exists(ng_path):
        print('（前回の不合格分', os.path.basename(ng_path), 'が残っている。今回は合格なので使わない）')

    # 5) 納品コピー
    delivered = ''
    if a.deliver and unmatched_n:  # 未照合行を許容した案件は納品しない（スキルの合格条件は未照合ゼロ）
        print(f'未照合 {unmatched_n} 行が残っているので納品コピーはしない。ref を決めるか manual: true にしてから再実行する')
    elif a.deliver:
        dst_dir = os.path.abspath(a.deliver)
        if not os.path.isdir(dst_dir):
            print('納品先が無い:', dst_dir); return 1
        if not review_path or not os.path.exists(review_path):
            # 確認点は NEO の明細コメントではなくシートで渡す（判断規則 10-22）。シートが無いまま NEO だけ納品すると確認点が届かない
            print('確認箇所シートが作れなかったので納品しない（上の例外を直して再実行）'); return 1
        base = f'{name}_claude'
        ext_r = os.path.splitext(review_path)[1] if review_path else ''
        stem, k = base, 2
        # NEO と確認箇所シートは同じ番号で揃える（どちらかが既にあれば次の番号）。既存ファイルは上書きしない
        while os.path.exists(os.path.join(dst_dir, stem + '.neo')) or (review_path and os.path.exists(os.path.join(dst_dir, f'{stem}_確認箇所{ext_r}'))):
            stem = f'{base}_{k}'; k += 1
        dst = os.path.join(dst_dir, stem + '.neo')
        dst_r = os.path.join(dst_dir, f'{stem}_確認箇所{ext_r}')
        # 両方を納品先の一時名にコピーしてから名前を付ける。途中で失敗したら自分が作った一時ファイルとシートだけ片付ける
        # （NEO だけ・シートだけが納品先に残らないように。既存ファイルは触らない。Codex 指摘）
        tmp_n = os.path.join(dst_dir, f'.{stem}.{os.getpid()}.neo.tmp')
        tmp_r = os.path.join(dst_dir, f'.{stem}.{os.getpid()}.review.tmp')
        placed: list = []
        try:
            shutil.copy2(neo, tmp_n)
            shutil.copy2(review_path, tmp_r)
            os.rename(tmp_r, dst_r); placed.append(dst_r)
            os.rename(tmp_n, dst); placed.append(dst)
        except Exception:
            for p_ in [tmp_n, tmp_r] + placed:
                try:
                    if os.path.exists(p_):
                        os.remove(p_)
                except OSError:
                    pass
            raise
        delivered = dst
        print('納品:', dst)
        print('納品（確認箇所シート）:', dst_r)
    if check and not a.no_profile and os.path.exists(reading):  # 合格した案件の工場設定を記録（reading_check が FAIL なしのときだけ書く）
        rc, out2 = run([os.path.join(HERE, 'reading_check.py'), reading, '--save-profile', '--quiet'], FILES)
        line = next((l for l in out2.splitlines() if '工場プロファイル' in l), '')
        if line:
            print(line)
    if not a.no_report and rep:
        try:
            print('報告文:', write_report(case, est, rep, rows, inspect_json, out, marks, neo, delivered, check, audit_lines))
        except Exception as e:  # noqa: BLE001
            print('報告文の生成で例外（合否には影響しない）:', e)
    if a.open:
        cb = ENV.get('COGNI_BIN') or ''
        if cb and os.path.isfile(cb):
            subprocess.Popen([cb, neo])
            print('コグニセブンで開いた:', cb)
        else:
            print('コグニセブン本体が見つからない（env_check.py --cogni で指定）。--open は無視')
    print('合格:', neo)
    return 0


if __name__ == '__main__':
    sys.exit(main())
