# -*- coding: utf-8 -*-
"""pdf_pages.py — 見積 PDF を「読みやすい画像」に切り分ける（転記の下ごしらえ。時間短縮用）。

FAX・スキャンの見積 PDF はページ全体を 1 枚で読むと細かい数字（品番・単価）を読み違えやすい。
ページごとに埋め込み画像を取り出し、明細の帯を上下に分けて拡大した画像を作る。文字層がある PDF は文字も書き出す。

    python .claude/skills/pdf-to-neo/scripts/pdf_pages.py <見積.pdf> <出力フォルダ> [--parts 2] [--width 1650]

出力: page_N.png（ページ全体）/ page_N_1.png, page_N_2.png …（上から順に分けて拡大）/ page_N.txt（文字層があるときだけ）
その後 Read ツールで page_N_k.png を順に開いて写す（2026-09-13 ランクル・ベンツで手作業していた切り出しを 1 コマンドにした）。
要 pypdf と Pillow（ocr_prefill.py と同じ）。埋め込み画像の無い（文字だけの）PDF は page_N.txt だけ書く。
"""
from __future__ import annotations

import argparse
import io
import os
import sys


def split_pages(pdf: str, out_dir: str, parts: int = 2, width: int = 1650, top: float = 0.0, bottom: float = 1.0, overlap: float = 0.03) -> list[dict]:
    """戻り値: ページごとの {'page', 'full', 'parts': [...], 'text': 文字数}"""
    try:
        import pypdf
        from PIL import Image
    except ImportError as e:  # noqa: F841
        raise SystemExit('pypdf と Pillow が要る（pip install pypdf pillow）。無ければ PDF を Read ツールで直接読む')
    os.makedirs(out_dir, exist_ok=True)
    rd = pypdf.PdfReader(pdf)
    res = []
    for i, pg in enumerate(rd.pages, start=1):
        info = {'page': i, 'full': '', 'parts': [], 'text': 0}
        try:
            txt = pg.extract_text() or ''
        except Exception:  # noqa: BLE001  壊れた文字層は無いものとして扱う
            txt = ''
        if txt.strip():
            p = os.path.join(out_dir, f'page_{i}.txt')
            open(p, 'w', encoding='utf-8').write(txt)
            info['text'] = len(txt)
        imgs = list(pg.images)
        if imgs:
            img = max((Image.open(io.BytesIO(x.data)) for x in imgs), key=lambda im: im.size[0] * im.size[1]).convert('RGB')
            full = os.path.join(out_dir, f'page_{i}.png')
            img.save(full)
            info['full'] = full
            w, h = img.size
            y0, y1 = int(h * top), int(h * bottom)
            step = (y1 - y0) / max(1, parts)
            for k in range(parts):
                a = max(y0, int(y0 + step * k - h * overlap)); b = min(y1, int(y0 + step * (k + 1) + h * overlap))  # 境目の行が切れないよう少し重ねる
                crop = img.crop((0, a, w, b))
                if w > width:
                    crop = crop.resize((width, max(1, int(crop.size[1] * width / w))))
                p = os.path.join(out_dir, f'page_{i}_{k + 1}.png')
                crop.save(p)
                info['parts'].append(p)
        res.append(info)
    return res


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description='見積 PDF をページごとの拡大画像に切り分ける')
    ap.add_argument('pdf')
    ap.add_argument('out_dir')
    ap.add_argument('--parts', type=int, default=2, help='1 ページを上下いくつに分けるか（既定 2。明細が細かいときは 3）')
    ap.add_argument('--width', type=int, default=1650, help='切り出した画像の幅（px）')
    ap.add_argument('--top', type=float, default=0.0, help='明細の帯の上端（ページの高さに対する割合。ヘッダを飛ばすなら 0.2 など）')
    ap.add_argument('--bottom', type=float, default=1.0, help='明細の帯の下端（割合）')
    a = ap.parse_args(argv)
    res = split_pages(a.pdf, a.out_dir, a.parts, a.width, a.top, a.bottom)
    for r in res:
        print(f"page {r['page']}: 画像 {'あり' if r['full'] else 'なし'} / 切り出し {len(r['parts'])} 枚 / 文字層 {r['text']} 字")
    print(f'{len(res)} ページ → {a.out_dir}（page_N_k.png を順に Read で開いて写す）')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
