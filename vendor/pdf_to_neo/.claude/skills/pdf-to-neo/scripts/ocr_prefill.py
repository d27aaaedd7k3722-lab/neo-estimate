# -*- coding: utf-8 -*-
"""ocr_prefill.py — 見積 PDF の各ページを Windows 標準 OCR（Windows.Media.Ocr、pip 不要）で先読みし、
数値列（部品コード・品番・指数・数量・金額・工賃・印）を page_N.json の下書きに流し込む。

FAX 品質の画像では半角カナの名称はほぼ読めないが、縦横比を補正して拡大すれば 品番・指数・金額・車両欄は 9 割方読める（2026-09-07 実測）。
なので役割分担は「数字と品番は OCR が先に埋める、名称の確認と数値の検証は Claude が画像を見て行う」。
下書きの行は comment が `OCR未確認` で始まり、reading_pages validate はその印が残っている行を FAIL にする（見ずに通せない）。

使い方（files ディレクトリで）:
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/ocr_prefill.py <見積PDF> <案件フォルダ> [--pages 1,2] [--scale 2] [--overwrite]
出力:
    <案件>/pages/ocr/page_N.png      OCR にかけた画像（拡大・縦横比補正後）
    <案件>/pages/ocr/page_N.words    OCR の生出力（語ごとの座標）
    <案件>/pages/page_N.ocr.json     行の推定（rows / header_guess / subtotal_guess）。人が見るための中間物
    <案件>/pages/page_N.json         無ければ作る（推定行を rows に、comment に `OCR未確認`）。--overwrite で上書き
    <案件>/pages/header.json         無ければ作る（車両欄の推定を入れる）
必要なもの: Windows 10/11 の日本語 OCR（設定 > 言語 に日本語があれば入っている）、Python パッケージ pypdf と Pillow（無ければ理由を表示して終了。手で写す）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import unicodedata
from typing import Optional

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import reading_pages  # noqa: E402

WINOCR = os.path.join(HERE, 'winocr.ps1')
DASH = '[-‐‑‒–—―ー－~〜ｰ]'
PN_RE = re.compile(r'^\d{5}' + DASH + r'[A-Z0-9]{2,5}(' + DASH + r'[A-Z0-9]{1,6})?$')
AMOUNT_RE = re.compile(r'^\d{1,3}(,\d{3})+$|^[1-9]\d{1,6}$|^0?[1-9]$|^0$')  # '00' '000' は桁の断片。'04' は数量 4
TOTAL_LINE_RE = re.compile(r'小計|合計|課税|消費税|御見積|見積額|部品価格適応|頁計|ページ計')
INDEX_RE = re.compile(r'^\d{1,2}\.\d{1,2}$')
CODE_RE = re.compile(r'^\d{4}$')
MARK_RE = re.compile(r'^[$#*@]$')
HEADER_WORDS = {'code': ('コード', 'ｺｰﾄﾞ'), 'name': ('部品名', '品名', '名称', '項目'), 'method': ('修理方法', '区分', '作業'), 'parts_no': ('品番', '部品番号'),
                'index': ('指数',), 'qty': ('数量', '個数'), 'price': ('部品価格', '部品金額', '金額', '価格', '部品代'), 'wage': ('工賃', '技術料', '作業料', '工資', '工貸', '賃(円)')}


def nfkc(s: str) -> str:
    return unicodedata.normalize('NFKC', s or '')


# ---------------------------------------------------------------------- 画像
def extract_pages(pdf: str, out_dir: str, pages: Optional[set[int]], scale: float) -> list[tuple[int, str, str]]:
    """PDF の各ページを画像にして (page, png, text) を返す。埋め込み画像（FAX）はそのまま取り出し、文字層があれば text にも入れる"""
    try:
        import pypdf  # type: ignore
    except ImportError:
        print('pypdf が無い（pip install pypdf）。OCR 先読みは使えないので手で写す'); return []
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        print('Pillow が無い（pip install pillow）。OCR 先読みは使えないので手で写す'); return []
    os.makedirs(out_dir, exist_ok=True)
    r = pypdf.PdfReader(pdf)
    out = []
    for i, page in enumerate(r.pages, start=1):
        if pages and i not in pages:
            continue
        text = ''
        try:
            text = page.extract_text() or ''
        except Exception:  # noqa: BLE001
            text = ''
        imgs = []
        try:
            imgs = list(page.images)
        except Exception as e:  # noqa: BLE001
            print(f'ページ {i}: 画像の取り出しに失敗（{e}）')
        if not imgs:
            print(f'ページ {i}: 埋め込み画像なし（文字層 {len(text)} 文字）。OCR は行わない')
            out.append((i, '', text))
            continue
        # 最大の画像をページ画像とみなす
        best = None
        for im in imgs:
            try:
                pil = im.image
            except Exception:  # noqa: BLE001
                continue
            if best is None or pil.size[0] * pil.size[1] > best.size[0] * best.size[1]:
                best = pil
        if best is None:
            out.append((i, '', text)); continue
        w, h = best.size
        asp = 2.0 if h < w * 0.95 else 1.0  # FAX（横 204dpi × 縦 98dpi）は縦が半分に潰れている
        k = scale if w < 2600 else 1.0
        big = best.convert('L').resize((int(w * k), int(h * k * asp)), Image.LANCZOS)
        png = os.path.join(out_dir, f'page_{i}.png')
        big.save(png)
        out.append((i, png, text))
        print(f'ページ {i}: 画像 {w}x{h} → {big.size[0]}x{big.size[1]}（拡大 {k} 倍 / 縦補正 {asp}）')
    return out


def run_ocr(png: str) -> list[dict]:
    """winocr.ps1 を呼び、語ごとの {line, x, y, w, h, text} を返す"""
    if not os.path.exists(WINOCR):
        print('winocr.ps1 が無い'); return []
    cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', WINOCR, '-Path', png]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        print('OCR の実行に失敗:', e); return []
    words = []
    for line in (p.stdout or '').splitlines():
        m = re.match(r'^L(\d+) (\d+) (\d+) (\d+) (\d+) \| (.*)$', line)
        if m:
            words.append({'line': int(m.group(1)), 'x': int(m.group(2)), 'y': int(m.group(3)), 'w': int(m.group(4)), 'h': int(m.group(5)), 'text': m.group(6)})
        elif line.startswith('NO ENGINE'):
            print('Windows の日本語 OCR が無い（設定 > 時刻と言語 > 言語 で日本語を追加）'); return []
    if not words and p.returncode != 0:
        print('OCR エラー:', (p.stderr or '')[-400:])
    return words


# ---------------------------------------------------------------------- 行と列
def group_rows(words: list[dict]) -> list[list[dict]]:
    """語を y でまとめて行にする（行の高さの中央値の 0.6 倍以内を同じ行）"""
    if not words:
        return []
    hs = [w['h'] for w in words if w['h'] > 0]
    tol = max(6, int(statistics.median(hs) * 0.6)) if hs else 12
    ws = sorted(words, key=lambda w: (w['y'] + w['h'] / 2, w['x']))
    rows: list[list[dict]] = []
    for w in ws:
        cy = w['y'] + w['h'] / 2
        if rows and abs(statistics.mean(v['y'] + v['h'] / 2 for v in rows[-1]) - cy) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    for r in rows:
        r.sort(key=lambda w: w['x'])
    return rows


def join_tokens(row: list[dict]) -> list[dict]:
    """語を意味のある塊に繋ぐ: '83 , 400' → '83,400'、'52119 ー 58988 ー AO' → '52119-58988-A0'、'2. 00' → '2.00'。カナは隣接語を繋いで名称にする"""
    toks: list[dict] = []
    for w in row:
        t = nfkc(w['text']).strip()
        if not t:
            continue
        t = re.sub(r'^[|｜]+|[|｜]+$', '', t)
        if not t:
            continue
        if toks:
            prev = toks[-1]
            gap = w['x'] - (prev['x'] + prev['w'])
            near = gap < max(prev['h'], w['h']) * 1.2
            pt = prev['text']
            if near and (re.match(r'^[\d,.]+$', t) and re.match(r'^[\d,.]+[,.]?$', pt) or t in (',', '.') or pt.endswith((',', '.')) and re.match(r'^\d', t)):
                prev['text'] = pt + t; prev['w'] = w['x'] + w['w'] - prev['x']; continue
            if near and (re.match(r'^' + DASH + r'?[A-Z0-9]+' + DASH + r'?$', t) or re.match(r'^' + DASH + r'$', t)) and re.match(r'^\d{5}(' + DASH + r'[A-Z0-9]*)*' + DASH + r'?$', pt):
                prev['text'] = pt + t; prev['w'] = w['x'] + w['w'] - prev['x']; continue
            if near and _is_kana(t) and _is_kana(pt):
                prev['text'] = pt + t; prev['w'] = w['x'] + w['w'] - prev['x']; continue
        toks.append(dict(w, text=t))
    for tk in toks:
        s = tk['text']
        s = re.sub(DASH, '-', s) if re.match(r'^\d{5}', s) else s
        if re.match(r'^\d{5}-', s):  # 品番: 国産車の品番は I と O を使わない（1 と 0 の誤読）
            s = s.replace('O', '0').replace('I', '1').replace('l', '1')
        tk['text'] = s
    return toks


def _is_kana(t: str) -> bool:
    return bool(re.match(r'^[ｦ-ﾟァ-ヶー・a-zA-Z一-龥()（）/／]+$', t))


def find_header(rows: list[list[dict]]) -> Optional[dict]:
    """列見出し行（部品価格 / 工賃 / 指数 / 数量 / 品番 …）を探し、列名 → x 中心 を返す。
    OCR は語を細切れにするので、行の語を連結した文字列で見出し語を探し、見つかった文字位置から元の語の x を引く"""
    best, best_n = None, 0
    for r in rows[:80]:
        chars: list[tuple[str, float]] = []  # (文字, その文字のおよその x 中心。語内の位置から按分)
        for w in r:
            t = nfkc(w['text']).replace(' ', '')
            n = max(1, len(t))
            chars += [(ch, w['x'] + w['w'] * (i + 0.5) / n) for i, ch in enumerate(t)]
        text = ''.join(ch for ch, _ in chars)
        cols: dict[str, float] = {}
        for key, names in HEADER_WORDS.items():
            for nm in names:
                i = text.find(nm)
                if i >= 0:
                    cols[key] = (chars[i][1] + chars[i + len(nm) - 1][1]) / 2  # 見出し語の中心
                    break
        if len(cols) > best_n and ('price' in cols or 'wage' in cols):
            best, best_n = {'cols': cols, 'y': r[0]['y']}, len(cols)
    return best


def classify_row(toks: list[dict], header: Optional[dict], width: int) -> Optional[dict]:
    """1 行の塊から明細行を推定。数値が 1 つも無い行は None"""
    nums = [t for t in toks if AMOUNT_RE.match(t['text']) or INDEX_RE.match(t['text']) or CODE_RE.match(t['text'])]
    pn = next((t for t in toks if PN_RE.match(t['text'])), None)
    if not nums and not pn:
        return None
    out = {'code': '', 'name': '', 'method': '', 'parts_no': pn['text'] if pn else '', 'index': '', 'qty': '', 'price': '', 'wage': '', 'flags': '', 'comment': ''}
    marks = ''.join(t['text'] for t in toks if MARK_RE.match(t['text']))
    out['flags'] = marks
    for t in toks:
        s = t['text']
        if s in ('取替', '脱着', '修理', '鈑金', '板金', '修正', '調整', '交換', '部品', '塗装', '脱着修理', '分解調整', '点検調整'):
            out['method'] = s
    kana = [t for t in toks if _is_kana(t['text']) and len(t['text']) >= 2 and t['text'] not in ('取替', '脱着', '修理', '鈑金', '板金')]
    if kana:
        out['name'] = ''.join(k['text'] for k in kana)
    # コード: 左端の 4 桁
    for t in nums:
        if CODE_RE.match(t['text']) and t['x'] < width * 0.12:
            out['code'] = t['text']; nums = [n for n in nums if n is not t]; break
    idx = [t for t in nums if INDEX_RE.match(t['text'])]
    if idx:
        out['index'] = idx[0]['text']; nums = [n for n in nums if n is not idx[0]]
    amounts = [t for t in nums if AMOUNT_RE.match(t['text'])]
    if header and header.get('cols'):
        cols = dict(header['cols'])
        synthetic_wage = 'price' in cols and 'wage' not in cols  # 「工賃」が読めなかった見出し: 部品価格の見出し中心から右 12% より右の金額は工賃（FAX 実測: 見出し中心 2488、部品価格 2580、工賃 2960 / 幅 3100）
        for t in amounts:
            cx = t['x'] + t['w'] / 2
            if synthetic_wage and cx > cols['price'] + width * 0.12:
                key = 'wage'
            else:
                key = min(((abs(cx - x), k) for k, x in cols.items() if k in ('qty', 'price', 'wage', 'index')), default=(None, None))[1]
            if key == 'qty' and re.match(r'^\d{1,2}$', t['text']):
                out['qty'] = str(int(t['text']))
            elif key in ('price', 'wage') and not out[key]:
                out[key] = t['text'].replace(',', '')
            elif key == 'price' and out['price'] and not out['wage'] and cx > cols['price']:
                out['wage'] = t['text'].replace(',', '')  # 部品価格の右にもう 1 つ金額 = 工賃
            elif key == 'index' and re.match(r'^\d{1,2}$', t['text']) and not out['qty']:
                out['qty'] = str(int(t['text']))
    else:
        small = [t for t in amounts if re.match(r'^\d{1,2}$', t['text'])]
        big = [t for t in amounts if t not in small]
        if small:
            out['qty'] = str(int(small[0]['text']))
        if len(big) >= 2:
            out['price'], out['wage'] = big[-2]['text'].replace(',', ''), big[-1]['text'].replace(',', '')
        elif len(big) == 1:
            key = 'price' if (pn or not out['index']) else 'wage'
            out[key] = big[0]['text'].replace(',', '')
    if not out['qty'] and (out['price'] or pn):
        out['qty'] = '1'
    return out


def header_guess(all_text: str) -> dict:
    """車両欄の推定（車台番号・型式・型式指定/類別・カラー・初度登録・御見積額）。見出し語の近くにある値だけを採る"""
    t = nfkc(all_text).replace('\n', ' ')
    t = re.sub(r'(?<=[A-Z0-9])\s*' + DASH + r'\s*(?=[A-Z0-9])', '-', t)  # 'RC4 ー 1000001' → 'RC4-1000001'
    g: dict = {}
    m = re.search(r'(?:車台番号|車台|車体番号)[^A-Z0-9]{0,8}([A-Z]{1,4}\d{0,3}[A-Z]?\d{0,2}W?-\d{6,7})', t) or re.search(r'\b([A-Z]{2,4}\d{1,3}[A-Z]?W?-\d{7})\b', t)
    if m:
        g['serial_no'] = m.group(1)
    m = re.search(r'(?:型式指定|指定番号|指定)\D{0,6}(\d{5})', t)
    if m:
        g['desig'] = m.group(1)
    m = re.search(r'(?:類別|類別区分|区分番号)\D{0,6}(\d{4})', t)
    if m:
        g['category'] = m.group(1)
    if 'desig' not in g:
        m = re.search(r'(?:型式指定|類別)\D{0,20}?(\d{5})\s*[-/]\s*(\d{4})', t) or re.search(r'\b(\d{5})\s*[-/]\s*(\d{4})\b', t)
        if m:
            g['desig'], g['category'] = m.group(1), m.group(2)
    m = re.search(r'(?:型式|型 式)\s*[:：]?\s*((?:[0-9A-Z]{1,4}-)?[A-Z]{2,4}\d{1,3}[A-Z]{0,3}W?)\b', t)
    if m:
        g['model_code_raw'] = m.group(1)
    m = re.search(r'(?:カラー|ｶﾗｰ|色)\s*(?:No|NO|№)?\.?\s*[:：]?\s*([0-9A-Z]{3})\b', t)
    if m:
        g['color_code'] = m.group(1)
    m = re.search(r'(令和|平成|R|H)\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月', t)
    if m:
        era = 'R' if m.group(1) in ('令和', 'R') else 'H'
        g['reg_date'] = f'{era}{int(m.group(2))}.{int(m.group(3))}'
    m = re.search(r'(?:御見積額|見積額|合計金額|御見積金額)\D{0,8}([\d,]{5,})', t)
    if m:
        g['total'] = int(m.group(1).replace(',', ''))
    return g


def subtotal_guess(rows: list[list[dict]]) -> dict:
    """ページ小計らしい行（小計 / 部品計 / 工賃計 / 合計）の金額"""
    g: dict = {}
    for r in rows:
        text = ''.join(nfkc(w['text']) for w in r).replace(' ', '')
        nums = [int(x.replace(',', '')) for x in re.findall(r'\d{1,3}(?:,\d{3})+|\d{4,}', text)]
        if not nums:
            continue
        if re.search(r'小計|ページ計|頁計', text):
            g.setdefault('subtotal_line', []).append(nums)
        if re.search(r'部品計|部品合計', text):
            g['parts'] = nums[-1]
        if re.search(r'工賃計|作業計|技術料計', text):
            g['wage'] = nums[-1]
    return g


# ---------------------------------------------------------------------- メイン
def prefill(pdf: str, case: str, pages: Optional[set[int]], scale: float, overwrite: bool) -> int:
    pdir = reading_pages.pages_dir(case)
    odir = os.path.join(pdir, 'ocr')
    imgs = extract_pages(pdf, odir, pages, scale)
    if not imgs:
        return 1
    all_text = []
    made = 0
    for pg, png, text in imgs:
        words = run_ocr(png) if png else []
        if png:
            with open(os.path.join(odir, f'page_{pg}.words'), 'w', encoding='utf-8') as fh:
                for w in words:
                    fh.write(f"L{w['line']} {w['x']} {w['y']} {w['w']} {w['h']} | {w['text']}\n")
        width = max((w['x'] + w['w'] for w in words), default=1)
        rows = group_rows(words)
        header = find_header(rows)
        guessed = []
        for r in rows:
            if header and r[0]['y'] <= header['y']:
                continue
            line_text = ''.join(nfkc(w['text']) for w in r).replace(' ', '')
            if TOTAL_LINE_RE.search(line_text):
                break  # 小計・合計の行から下は明細ではない（小計は subtotal_guess が拾う）
            toks = join_tokens(r)
            c = classify_row(toks, header, width)
            if c and (c['price'] or c['wage'] or c['parts_no'] or c['index']):
                guessed.append(c)
        page_text = ' '.join(w['text'] for w in words) + ' ' + text
        all_text.append(page_text)
        sg = subtotal_guess(rows)
        ocr_json = {'page': pg, 'image': png, 'header_found': bool(header), 'columns': (header or {}).get('cols'), 'rows': guessed, 'subtotal_guess': sg,
                    'text_layer': bool(text.strip())}
        reading_pages.save_json(os.path.join(pdir, f'page_{pg}.ocr.json'), ocr_json)
        print(f"ページ {pg}: OCR 語 {len(words)} / 行 {len(rows)} / 明細らしい行 {len(guessed)} / 列見出し {'あり' if header else 'なし'} / 小計候補 {sg or 'なし'}")
        dst = os.path.join(pdir, f'page_{pg}.json')
        if overwrite or not os.path.exists(dst):
            rows_short = ['|'.join([c['code'], c['name'], c['method'], c['parts_no'], c['index'], c['qty'], c['price'], c['wage'], c['flags'], 'OCR未確認']) for c in guessed]
            page_json = {'page': pg, 'rows_printed': None, 'subtotal': {k: v for k, v in sg.items() if k in ('parts', 'wage')}, 'marks': {},
                         'blocks': [{'title': '', 'rows': rows_short}]}
            reading_pages.save_json(dst, page_json)
            made += 1
            print(f'  → {dst}（{len(rows_short)} 行の下書き。画像を見て名称と数値を確認し、確認した行の comment「OCR未確認」を消す）')
        else:
            print(f'  → {dst} は既にある（--overwrite で上書き）。推定は page_{pg}.ocr.json に')
    hg = header_guess('\n'.join(all_text))
    hdr = os.path.join(pdir, 'header.json')
    if not os.path.exists(hdr):
        os.makedirs(pdir, exist_ok=True)
        reading_pages.cmd_init(case, 0)
    try:
        h = reading_pages.load_json(hdr)
    except (OSError, ValueError):
        h = {}
    if hg:
        h['_ocr_guess'] = hg  # 車両欄の推定（人が vehicle に写す。validate/merge は使わない）
        reading_pages.save_json(hdr, h)
        print('車両欄の推定（header.json の _ocr_guess。確認して vehicle に写す）:', json.dumps(hg, ensure_ascii=False))
    print(f'下書きしたページ {made} / {len(imgs)}。次: 各ページの画像（pages/ocr/page_N.png か元 PDF）を見て page_N.json を確認 → reading_pages.py validate --page N')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf')
    ap.add_argument('case_dir')
    ap.add_argument('--pages', default='')
    ap.add_argument('--scale', type=float, default=2.0)
    ap.add_argument('--overwrite', action='store_true')
    a = ap.parse_args()
    if sys.platform != 'win32':
        print('Windows 専用（Windows.Media.Ocr）'); return 1
    pages = {int(x) for x in a.pages.split(',') if x.strip()} if a.pages else None
    return prefill(os.path.abspath(a.pdf), os.path.abspath(a.case_dir), pages, a.scale, a.overwrite)


if __name__ == '__main__':
    sys.exit(main())
