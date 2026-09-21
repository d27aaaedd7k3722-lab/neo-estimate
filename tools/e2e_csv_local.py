# -*- coding: utf-8 -*-
"""CSV 取り込み経路の画面の通し確認（ローカルのアプリに Playwright で入れる。架空のデータだけ・API の課金なし）。

  1. CSV 取り込み: 区分が空欄の行の知らせ・AI の「部品相違」申告の知らせ（③で確認）
  2. ③: AI の申告の警告＋確認チェック（入れるまで生成ボタンが押せない）・数量 × 単価の参考警告（表の No で呼ぶ）
  3. ④: ファイル名の「（部品相違）」・金額検証の文・落とした NEO の区分（CSV の空欄は DisposalCode -1 のまま）
  4. ①に戻って**同じ申告の別の CSV** を入れると、③の確認が外れている（前の確認を持ち越さない）
  5. ③で確認したあとに明細を直す（行を足す）と、確認が外れる（確認した明細と違う NEO を出さない）
  6. バグハント第 3 弾（2026-09-21 夜）: 「部品代相違」の申告（B1）・品番だけ違う別の CSV に前の使用者名が戻らない（X2）・
     サイドバーの費用は③でチェックを入れたときだけ入る（B2）・※金額調整の確認は行のコピーで外れる（B4）・
     ③で型式指定・類別を入れても部品コード・照合の品番が入らない（E1〜E5。Addata がつながっているときに意味がある）

使い方（先にローカルのアプリを立てておく。Claude なら preview_start の neo-estimate-local1 = port 8511）:
    C:/Users/R-T/.venvs/shouchiku8/Scripts/python.exe tools/e2e_csv_local.py [--url http://localhost:8511/] [--out <フォルダ>] [--allow-skip]
    本番（Addata がつながっていない）に当てるときは --allow-skip を付ける（E の場面は SKIP と出る）。E の前提に使う Addata は
    環境変数 NEO_E2E_ADDATA（既定 C:/Addata）
終了コード: 0 全部 OK / 1 どれかが NG（画面の写しを --out に残す）。SKIP（確かめられなかった場面。E は Addata が
つながっていないと直す前の版でも同じ結果になる）は合格に数えず、終わりに SKIP として出す（--allow-skip が無ければ 1 で終わる）
2026-09-21 に CSV 経路の残件（区分の推し量り・AI の相違申告・数量 × 単価）を直したときに作った"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LBL_AI = '見積書の合計と突き合わせました（このまま NEO に進む）'
NOTE = '"部品相違1,200円 工賃相違0円"\n'
CSV_MAIN = ('品名,区分,数量,部品金額,工賃,部品コード\n'
            'ｸﾘｯﾌﾟ,,1,300,0,\n'                       # 区分が空欄（品名・金額からは取替に見える）→ -1 のまま
            'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,0,52119-XXXXX\n'
            'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ脱着,脱着,1,0,8000,\n'
            'ﾎﾞﾙﾄ,取替,3,500,0,\n'                     # 数量 3 × 単価 166.7 ≠ 500 → 参考警告（No 4）
            + NOTE)
CSV_OTHER = ('品名,区分,数量,部品金額,工賃,部品コード\n'
             'ﾘﾔﾊﾞﾝﾊﾟ,取替,1,38000,0,\nﾘﾔﾊﾞﾝﾊﾟ脱着,脱着,1,0,6000,\n' + NOTE)


def _wait_text(page, s, t=120000):
    page.locator(f'text={s} >> visible=true').first.wait_for(timeout=t)


def _import_csv(page, csv_text):
    exp = page.get_by_text('PDF でうまく読み取れないとき', exact=False).first
    exp.wait_for(state='visible', timeout=120000)
    time.sleep(1.5)
    ta = page.get_by_label('Geminiの解析結果CSVを貼り付け（ヘッダー行必須）')
    if not ta.is_visible():
        exp.click()
        time.sleep(1.5)
    ta.fill(csv_text)
    ta.press('Control+Enter')
    _wait_text(page, '読み込み完了', 60000)
    time.sleep(2)


def _to_step3(page):
    page.get_by_role('button', name='NEO生成を開始').click()
    _wait_text(page, '合計・費用')
    time.sleep(3)
    page.get_by_role('tab', name='💰 合計・費用').click()
    time.sleep(2)


def _wait_idle(page, t=90):
    """Streamlit の実行が終わるまで待つ（stApp の data-test-script-state が running でなく、古い表示も無い状態が続くまで）"""
    t0 = time.time()
    time.sleep(0.5)
    calm = 0
    while time.time() - t0 < t:
        st_ = page.evaluate("""() => { const a = document.querySelector('[data-testid="stApp"]');
            return a ? a.getAttribute('data-test-script-state') : 'none'; }""")
        stale = page.evaluate("""() => document.querySelectorAll('[data-stale="true"]').length""")
        calm = calm + 1 if (st_ != 'running' and not stale) else 0
        if calm >= 3:
            break
        time.sleep(0.3)
    time.sleep(0.5)


def _to_step4(page):
    page.get_by_role('button', name='NEOファイルを生成する').click()
    _wait_text(page, 'NEOファイルの生成が完了しました')
    time.sleep(2)


CSV_PLAIN = ('品名,区分,数量,部品金額,工賃,部品コード\n'
             'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ,取替,1,45000,0,AAA-111\n'
             'ﾌﾛﾝﾄﾊﾞﾝﾊﾟ脱着,脱着,1,0,8000,\n')


def _h3_b1(page, out, res):
    """B1: 「部品代相違1,200円 工賃相違0円」の部品側が消えない"""
    _import_csv(page, CSV_PLAIN + '"部品代相違1,200円 工賃相違0円"\n')
    body = page.inner_text('body')
    res['B1 ①「部品代相違1,200円」が画面に出る（③で確認）'] = '部品代相違1,200円' in body and 'ステップ③で原本と突き合わせ' in body
    _to_step3(page)
    res['B1 ③ 確認するまで生成ボタンが押せない'] = page.get_by_role('button', name='NEOファイルを生成する').is_disabled()


def _h3_x2(page, out, res):
    """X2: 品番だけ違う別の CSV に、前の案件の使用者名が戻らない"""
    _import_csv(page, CSV_PLAIN)
    _to_step3(page)
    page.get_by_role('tab', name='🚗 車両情報').click()
    time.sleep(1)
    inp = page.get_by_role('textbox', name='使用者名')
    inp.fill('架空一郎')
    inp.press('Tab')
    _wait_idle(page)
    page.get_by_role('button', name='← ステップ①に戻る').first.click()
    time.sleep(4)
    _import_csv(page, CSV_PLAIN.replace('AAA-111', 'BBB-222'))
    page.get_by_role('button', name='NEO生成を開始').click()
    _wait_text(page, '合計・費用')
    time.sleep(3)
    res['X2 別の CSV に前の使用者名が戻らない'] = page.get_by_role('textbox', name='使用者名').input_value() == ''


def _h3_b2(page, out, res):
    """B2: サイドバーの費用は③でチェックを入れたときだけ NEO に入る"""
    time.sleep(3)
    tow = page.get_by_label('レッカー費用（税抜）')
    tow.fill('20000')
    tow.press('Enter')
    _wait_idle(page)
    _import_csv(page, CSV_PLAIN)
    _to_step3(page)
    cb = page.get_by_role('checkbox', name='サイドバーの費用（レッカー ¥20,000', exact=False)
    res['B2 ③に費用のチェックがあり、入っていない'] = cb.count() == 1 and not cb.is_checked()
    _to_step4(page)
    res['B2 ④ チェックなしなら費用を入れない（¥58,300）'] = '¥58,300（税込）' in page.inner_text('body')
    page.get_by_role('button', name='← ステップ③に戻って直す').first.click()
    time.sleep(3)
    page.get_by_role('tab', name='💰 合計・費用').click()
    time.sleep(1.5)
    page.get_by_text('サイドバーの費用（レッカー ¥20,000', exact=False).first.click(timeout=15000)   # チェックの本体は文字に隠れている
    _wait_idle(page)
    _to_step4(page)
    res['B2 ④ チェックを入れると費用が入る（¥80,300）'] = '¥80,300（税込）' in page.inner_text('body')


def _h3_b4(page, out, res):
    """B4: 「※金額調整」の確認は、行をコピーしたら外れる（ほかの欄を触っても戻らない）"""
    lbl_adj = '「※金額調整」の行を確認しました（このまま NEO に入れる）'
    _import_csv(page, CSV_PLAIN + '※金額調整（部品）,,1,500,0,\n')
    _to_step3(page)
    page.get_by_text(lbl_adj).click()
    _wait_idle(page)
    ok_before = not page.get_by_role('button', name='NEOファイルを生成する').is_disabled()
    page.get_by_label('コピーNo').fill('3')
    page.keyboard.press('Enter')
    _wait_idle(page)
    page.get_by_role('button', name='📋 コピー').click()
    _wait_idle(page)
    time.sleep(2)
    page.get_by_role('tab', name='🚗 車両情報').click()
    time.sleep(1)
    cn = page.get_by_label('車名', exact=True)
    cn.fill('ﾃｽﾄ')
    cn.press('Enter')
    _wait_idle(page)
    page.get_by_role('tab', name='💰 合計・費用').click()
    time.sleep(1.5)
    res['B4 行をコピーしたら「※金額調整」の確認が外れる'] = (ok_before and not page.get_by_role('checkbox', name=lbl_adj).is_checked()
                                                     and page.get_by_role('button', name='NEOファイルを生成する').is_disabled())


SKIPS: list = []   # 確かめられなかった場面（合格とは数えない。終わりに SKIP として出す）


def _h3_e(page, out, res):
    """E1〜E5: ③で型式指定・類別を入れても、NEO に部品コード・照合の品番を入れない（Addata がつながっているときに意味がある）"""
    # つながっていることを**上の帯の緑の札（🗂 Addata 2026/09）で**確かめる。文言の有無（「未接続」）で見ると、画面の文言が
    # 変わったときに未接続のまま合格になる（Codex 講評 第 3 弾 2 周目）。札（緑か黄）が出るまで待ってから決める
    # （描画の遅い run で札より先に判定して SKIP にしていた。4 周目）
    page.locator('span.chip-ok, span.chip-warn').filter(has_text='Addata').first.wait_for(timeout=120000)
    if page.locator('span.chip-ok').filter(has_text='Addata').count() == 0:
        # Addata が無いと直す前の版でも部品コードは空になるので、この場面では何も確かめられない。合格とは数えない
        SKIPS.append('E ③で型式を入れても部品コード・照合の品番が入らない（Addata 未接続なので確かめられない。'
                     'Addata をつないだアプリで回す）')
        return
    # 前提: この Addata なら、直す前の照合器（auto_matching。いまはアプリから呼ばない）は右ドアの行に部品コードを入れる。
    # 入れない Addata（車種 W69 が無い・版が違う）では、直す前の版でも部品コードは空になるので確かめられない（Codex 3 周目）。
    # アプリが使っている Addata と同じ版かも、上の帯の札で見る
    _root = os.environ.get('NEO_E2E_ADDATA', r'C:/Addata')
    sys.path.insert(0, ROOT)
    try:
        import addata_locator as _al
        import auto_matching as _am
        _ver = _al.addata_version(_root)
        _probe = _am.match_pdf_items_to_addata(
            [{'name': 'ﾌﾛﾝﾄﾄﾞｱﾊﾟﾈﾙ 右', 'category': '取替', 'quantity': 1, 'parts_amount': 54100, 'wage': 12000}],
            {'vehicle_code': 'W69'}, _root)
        _would = bool(_probe and str(_probe[0].get('_master_ref_no') or '').strip())
    except Exception as e:  # noqa: BLE001
        _ver, _would = '', False
        print('E の前提を調べられない:', type(e).__name__)
    _chip = page.locator('span.chip-ok').filter(has_text='Addata').first.inner_text()
    if not (_would and _ver and _ver in _chip):
        SKIPS.append(f'E ③で型式を入れても部品コード・照合の品番が入らない（{_root} の版 {_ver or "不明"} では直す前の照合器も'
                     f'部品コードを入れない、またはアプリの Addata〔{_chip.strip()}〕と版が違うので確かめられない）')
        return
    _import_csv(page, '品名,区分,数量,部品金額,工賃,部品コード\nﾌﾛﾝﾄﾄﾞｱﾊﾟﾈﾙ 右,取替,1,54100,12000,\n'
                      'ﾘﾔﾊﾞﾝﾊﾟｰｶﾊﾞｰ,取替,1,43900,0,52159-58941-C0\n')
    page.get_by_role('button', name='NEO生成を開始').click()
    _wait_text(page, '合計・費用')
    time.sleep(3)
    for lbl, v in (('型式指定番号', '19417'), ('類別区分番号', '0001')):
        x = page.get_by_label(lbl, exact=True)
        x.fill(v)
        x.press('Tab')
        _wait_idle(page)
    page.get_by_role('tab', name='💰 合計・費用').click()
    time.sleep(1.5)
    _to_step4(page)
    p_e = os.path.join(out, 'e2e_hunt3_E.neo')
    with page.expect_download() as dl:
        page.get_by_role('button', name='NEOファイルをダウンロード').first.click()
    dl.value.save_as(p_e)
    rows = _erparts(p_e, 'PartsName, PartsCode, PartsNo')
    res['E ③で型式を入れても部品コード・照合の品番が入らない（Addata 接続）'] = (
        [(r[1], r[2]) for r in rows] == [('', ''), ('', '52159-58941-C0')])


def _hunt3(browser, url, out, res):
    """バグハント第 3 弾の場面（1 つずつ新しい画面で。1 つが途中で止まっても残りは回し、止まった場面は NG にする）"""
    for fn in (_h3_b1, _h3_x2, _h3_b2, _h3_b4, _h3_e):
        ctx = browser.new_context(viewport={'width': 1500, 'height': 1100}, accept_downloads=True)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until='domcontentloaded')
            fn(page, out, res)
        except Exception as e:  # noqa: BLE001  画面が想定と違う（直す前の版など）: その場面を NG として続ける
            res[f'{fn.__doc__.split(":")[0]} の場面が最後まで進まない（{type(e).__name__}）'] = False
            try:
                page.screenshot(path=os.path.join(out, f'e2e_{fn.__name__}.png'), full_page=True)
            except Exception:  # noqa: BLE001
                pass
        finally:
            ctx.close()


def _erparts(neo_path, cols='PartsName, DisposalCode, DisposalName'):
    sys.path.insert(0, ROOT)
    import app  # noqa: E402  （ローカルのアプリと同じ app.py で NEO を開く）
    neo = open(neo_path, 'rb').read()
    ck = app.find_real_cks(neo)
    files = app.extract_files(app.decompress_neo(neo, ck), app.parse_entries(neo, ck[0])[1])
    c = sqlite3.connect(':memory:')
    c.deserialize(files['AnSMB.txt'])
    try:
        return c.execute(f'SELECT {cols} FROM ERParts ORDER BY LineNo').fetchall()
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='http://localhost:8511/')
    ap.add_argument('--out', default=os.path.join(tempfile.gettempdir(), 'neo_e2e_csv'))
    ap.add_argument('--allow-skip', action='store_true',
                    help='確かめられなかった場面（SKIP）があっても 0 で終わる（本番など Addata がつながっていないアプリに当てるとき）')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    from playwright.sync_api import sync_playwright
    res = {}
    neo_path = os.path.join(a.out, 'e2e_csv.neo')
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context(viewport={'width': 1400, 'height': 1000}, accept_downloads=True).new_page()
        try:
            page.goto(a.url, wait_until='domcontentloaded')
            # ── 1〜3: 取り込み → ③ → ④
            _import_csv(page, CSV_MAIN)
            body = page.inner_text('body')
            res['①区分が空欄の行の知らせ'] = '区分が空欄の行を、区分なし' in body and '「ｸﾘｯﾌﾟ」' in body
            res['①AI の申告の知らせ（③で確認）'] = '見積書との差異が記録されています' in body and 'ステップ③で原本と突き合わせ' in body
            res['①相違のメモを明細にしない（4 行）'] = '4行 読み込み完了' in body
            _to_step3(page)
            body = page.inner_text('body')
            res['③AI の申告の警告'] = 'この CSV を作った AI が、見積書の合計と明細の合算が合わないと書き残していました' in body
            res['③未確認なら生成ボタンが押せない'] = page.get_by_role('button', name='NEOファイルを生成する').is_disabled()
            try:
                page.get_by_text('参考警告', exact=False).first.click()
                time.sleep(1)
            except Exception:  # noqa: BLE001  たたみが無ければ下の判定が NG になる
                pass
            body = page.inner_text('body')
            res['③数量 × 単価の参考警告（No 4）'] = '行4「ﾎﾞﾙﾄ」' in body and '166.7' in body
            page.get_by_text(LBL_AI).click()
            time.sleep(2.5)
            res['③確認すると生成ボタンが押せる'] = not page.get_by_role('button', name='NEOファイルを生成する').is_disabled()
            page.get_by_role('button', name='NEOファイルを生成する').click()
            _wait_text(page, 'NEOファイルの生成が完了しました')
            time.sleep(2)
            body = page.inner_text('body')
            res['④ファイル名に（部品相違）'] = '_見積（部品相違）.neo' in body
            res['④金額検証の文'] = 'CSV を作った AI が申告した「部品相違」を確かめて生成' in body
            with page.expect_download() as dl:
                page.get_by_role('button', name='NEOファイルをダウンロード').first.click()
            dl.value.save_as(neo_path)
            res['④落とした名前'] = dl.value.suggested_filename.endswith('（部品相違）.neo')
            # ── 4: ①に戻って同じ申告の別の CSV → 確認が外れている
            page.get_by_role('button', name='← ステップ③に戻って直す').first.click()
            time.sleep(3)
            page.get_by_role('button', name='← ステップ①に戻る').first.click()
            time.sleep(4)
            _import_csv(page, CSV_OTHER)
            _to_step3(page)
            body = page.inner_text('body')
            res['別の CSV: 明細が替わっている'] = '38,000' in body or '38000' in body
            res['別の CSV: 確認が外れている'] = not page.get_by_role('checkbox', name=LBL_AI).is_checked()
            res['別の CSV: 生成ボタンが押せない'] = page.get_by_role('button', name='NEOファイルを生成する').is_disabled()
            # ── 5: 確認のあとに明細を直したら（ここでは行を 1 つ足す）確認が外れる（確認した明細と違う NEO を出さない）
            page.get_by_text(LBL_AI).click()
            time.sleep(2.5)
            res['行を直す前: 確認すると押せる'] = not page.get_by_role('button', name='NEOファイルを生成する').is_disabled()
            page.get_by_role('button', name='➕ 行挿入').click()
            time.sleep(3.5)
            res['行を直した後: 確認が外れている'] = not page.get_by_role('checkbox', name=LBL_AI).is_checked()
            res['行を直した後: 生成ボタンが押せない'] = page.get_by_role('button', name='NEOファイルを生成する').is_disabled()
            _hunt3(b, a.url, a.out, res)
        finally:
            if not all(res.values()) or not res:
                page.screenshot(path=os.path.join(a.out, 'e2e_last.png'), full_page=True)
            b.close()
    if os.path.isfile(neo_path):
        rows = _erparts(neo_path)
        res['④NEO の区分（CSV の空欄は -1 のまま）'] = [(r[1], r[2]) for r in rows] == [(-1, ''), (0, '取替'), (1, '脱着'), (0, '取替')]
    for k, v in res.items():
        print(('OK ' if v else 'NG ') + k)
    for k in SKIPS:
        print('SKIP ' + k)
    # SKIP は合格に数えない（--allow-skip を付けたときだけ許す。Codex 講評 第 3 弾 3 周目）
    return 0 if res and all(res.values()) and (a.allow_skip or not SKIPS) else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
