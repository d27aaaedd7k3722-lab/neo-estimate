# -*- coding: utf-8 -*-
"""CSV 取り込み経路の画面の通し確認（ローカルのアプリに Playwright で入れる。架空のデータだけ・API の課金なし）。

  1. CSV 取り込み: 区分が空欄の行の知らせ・AI の「部品相違」申告の知らせ（③で確認）
  2. ③: AI の申告の警告＋確認チェック（入れるまで生成ボタンが押せない）・数量 × 単価の参考警告（表の No で呼ぶ）
  3. ④: ファイル名の「（部品相違）」・金額検証の文・落とした NEO の区分（CSV の空欄は DisposalCode -1 のまま）
  4. ①に戻って**同じ申告の別の CSV** を入れると、③の確認が外れている（前の確認を持ち越さない）
  5. ③で確認したあとに明細を直す（行を足す）と、確認が外れる（確認した明細と違う NEO を出さない）

使い方（先にローカルのアプリを立てておく。Claude なら preview_start の neo-estimate-local1 = port 8511）:
    C:/Users/R-T/.venvs/shouchiku8/Scripts/python.exe tools/e2e_csv_local.py [--url http://localhost:8511/] [--out <フォルダ>]
終了コード: 0 全部 OK / 1 どれかが NG（画面の写しを --out に残す）
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


def _erparts(neo_path):
    sys.path.insert(0, ROOT)
    import app  # noqa: E402  （ローカルのアプリと同じ app.py で NEO を開く）
    neo = open(neo_path, 'rb').read()
    ck = app.find_real_cks(neo)
    files = app.extract_files(app.decompress_neo(neo, ck), app.parse_entries(neo, ck[0])[1])
    c = sqlite3.connect(':memory:')
    c.deserialize(files['AnSMB.txt'])
    try:
        return c.execute('SELECT PartsName, DisposalCode, DisposalName FROM ERParts ORDER BY LineNo').fetchall()
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='http://localhost:8511/')
    ap.add_argument('--out', default=os.path.join(tempfile.gettempdir(), 'neo_e2e_csv'))
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
        finally:
            if not all(res.values()) or not res:
                page.screenshot(path=os.path.join(a.out, 'e2e_last.png'), full_page=True)
            b.close()
    if os.path.isfile(neo_path):
        rows = _erparts(neo_path)
        res['④NEO の区分（CSV の空欄は -1 のまま）'] = [(r[1], r[2]) for r in rows] == [(-1, ''), (0, '取替'), (1, '脱着'), (0, '取替')]
    for k, v in res.items():
        print(('OK ' if v else 'NG ') + k)
    return 0 if res and all(res.values()) else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
