# -*- coding: utf-8 -*-
"""reg_inputs.py — 読み手に渡す前の画像・PDF の扱いと、読み手の返事の正規化（2026-09-15 バグハント 3 回目 P3/P4/P6/P9/P10/P16・Q2/Q3/Q4/Q6/Q7/Q10/Q12/Q14）。
LLM の API は呼ばない。画像・PDF は架空のものをその場で作る。

    python tests/reg_inputs.py
終了コード: 0 全部 OK / 1 失敗
"""
from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from neo_skill import llm, reader  # noqa: E402

FAILS: list = []


def chk(cond, msg):
    if not cond:
        FAILS.append(msg)


def _pdf_pages(b: bytes) -> int:
    from pypdf import PdfReader
    return len(PdfReader(io.BytesIO(b)).pages)


def _page_size(b: bytes, i: int = 0) -> tuple:
    from pypdf import PdfReader
    p = PdfReader(io.BytesIO(b)).pages[i]
    return float(p.mediabox.width), float(p.mediabox.height)


def test_images():
    from PIL import Image
    # EXIF の向き（Orientation=6 = 右に 90 度回して見る）: 横長の画素でも縦のページになる
    im = Image.new('RGB', (400, 200), (255, 255, 255))
    ex = im.getexif()
    ex[0x0112] = 6
    buf = io.BytesIO()
    im.save(buf, format='JPEG', exif=ex.tobytes())
    w, h = _page_size(llm.image_to_pdf(buf.getvalue()))
    chk(h > w, f'P3: EXIF の向きが効いていない（ページ {w:.0f}×{h:.0f}）')
    # 複数ページの TIFF は全ページ
    frames = [Image.new('L', (200, 280), c) for c in (255, 200, 150)]
    buf = io.BytesIO()
    frames[0].save(buf, format='TIFF', save_all=True, append_images=frames[1:])
    chk(_pdf_pages(llm.image_to_pdf(buf.getvalue())) == 3, 'P4: 複数ページの TIFF の 2 ページ目以降が落ちる')
    # FAX の標準モード（204×98dpi）は正方画素に直す（縦横比が本来に近い）
    fx = Image.new('1', (1728, 1100), 1)
    buf = io.BytesIO()
    fx.save(buf, format='TIFF', dpi=(204, 98))
    w, h = _page_size(llm.image_to_pdf(buf.getvalue()))
    chk(1.2 < h / w < 1.6, f'P4: FAX の縦横比が直っていない（{w:.0f}×{h:.0f}、比 {h / w:.2f}）')
    # 透過 PNG は白地（黒地で文字が消えない）
    tp = Image.new('RGBA', (50, 50), (0, 0, 0, 0))
    buf = io.BytesIO()
    tp.save(buf, format='PNG')
    from pypdf import PdfReader
    pdf = llm.image_to_pdf(buf.getvalue())
    chk(_pdf_pages(pdf) == 1, 'P16: 透過 PNG の PDF が作れない')
    im2 = Image.open(io.BytesIO(buf.getvalue()))
    chk(im2.mode == 'RGBA', '前提: 透過 PNG')
    # 大きすぎる画像（PNG 13000×13000）は展開せずに断る
    big = Image.new('1', (13000, 13000), 1)
    buf = io.BytesIO()
    big.save(buf, format='PNG')
    try:
        llm.image_to_pdf(buf.getvalue())
        FAILS.append('P9: 1.69 億画素の画像を断らない')
    except llm.LLMError as e:
        chk('大きすぎ' in str(e), f'P9: 断る理由が日本語でない: {e}')


def test_pdfs():
    from pypdf import PdfWriter
    # 権限パスワードだけ（空のユーザーパスワード）の PDF は暗号を外して読める
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    w.encrypt(user_password='', owner_password='owner-secret')
    buf = io.BytesIO()
    w.write(buf)
    enc = buf.getvalue()
    dec = llm.pdf_prepare(enc)
    from pypdf import PdfReader
    chk(not PdfReader(io.BytesIO(dec)).is_encrypted and llm.pdf_page_count(dec) == 1, 'P6: 権限パスワードだけの PDF の暗号が外れない')
    # ユーザーパスワード付きは日本語で断る
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    w.encrypt(user_password='user-secret', owner_password='owner-secret')
    buf = io.BytesIO()
    w.write(buf)
    try:
        llm.pdf_page_count(buf.getvalue())
        FAILS.append('P6: ユーザーパスワード付きの PDF を断らない')
    except llm.LLMError as e:
        chk('パスワード' in str(e), f'P6: 断る理由: {e}')
    # ページ木が「100 万ページ」と申告する PDF は、木を展開せず申告値を返す（呼び出し側がページ数の上限で断る）
    w = PdfWriter()
    w.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    w.write(buf)
    b = buf.getvalue().replace(b'/Count 1', b'/Count 1000000')
    try:
        n = llm.pdf_page_count(b)
        chk(n == 1000000, f'P10: 申告のページ数を返していない: {n}')
    except Exception as e:  # noqa: BLE001
        FAILS.append(f'P10: 申告の大きい PDF で例外: {type(e).__name__}: {e}')


def test_header_normalise():
    base = {'source': 't', 'issuer': 't', 'est_date': '20260915', 'format': 'B',
            'customer': {'name': 'ｹﾝｼｮｳ', 'reg_no': '北九州 539 な 10-31', 'kilometer': '15,345km', 'term_date': '2028/12/14', 'postal': '〒510-0001'},
            'insurance': {'accident_date': '2026/09/01', 'garage_in': '令和8年9月3日', 'repair_days': '7日', 'policy_no': 'X'},
            'paint': {'total': '61,245円', 'material': '12,000', 'material_rate': '55%'},
            'totals': {'total': 100, 'neo_total': 99, 'tolerance': 1, 'tolerance_reason': 'x'}}
    notes: list = []
    h = reader._normalise_header(base, None, None, None, notes=notes)
    cu, ins, pt = h.get('customer') or {}, h.get('insurance') or {}, h.get('paint') or {}
    chk(cu.get('reg_no') == '北九州 539 な 1031', f"Q2: 登録番号の一連番号がそろわない: {cu.get('reg_no')!r}")
    chk(cu.get('kilometer') == '15345', f"Q4: 走行距離がそろわない: {cu.get('kilometer')!r}")
    chk(cu.get('term_date') == '20281214' and cu.get('postal') == '510-0001', f'Q3/Q4: 有効期限・郵便番号: {cu}')
    chk(ins.get('accident_date') == '20260901' and ins.get('garage_in') == '20260903' and ins.get('repair_days') == 7, f'Q3: 保険の日付・日数: {ins}')
    chk(pt.get('total') == 61245 and pt.get('material') == 12000 and pt.get('material_rate') == 55.0, f'Q4: 塗装の金額・割合: {pt}')
    chk('neo_total' not in (h.get('totals') or {}) and 'tolerance' not in (h.get('totals') or {}), f"O10: 合計の許容のキーが残る: {h.get('totals')}")
    bad = dict(base, customer={'name': 'x', 'reg_no': '北九州 539'})
    h2 = reader._normalise_header(bad, None, None, {'reg_no': '北九州 539 な 1031'}, notes=[])
    chk((h2.get('customer') or {}).get('reg_no') == '北九州 539 な 1031', f"Q2: 分けられない登録番号を車検証の値で補わない: {h2.get('customer')}")
    try:
        reader._normalise_header(dict(base, customer={'name': {'a': 1}}), None, None, None)
        FAILS.append('Q4: customer の値が dict でも形の FAIL にならない')
    except reader.PageShapeError:
        pass
    try:
        reader._normalise_header(dict(base, paint={'total': '約6万'}), None, None, None)
        FAILS.append('Q4: 読めない塗装工賃計が形の FAIL にならない')
    except reader.PageShapeError:
        pass
    h3 = reader._normalise_header(dict(base, customer={'name': 'a\nb\x07'}), None, None, None)
    chk('\n' not in (h3.get('customer') or {}).get('name', '') and '\x07' not in (h3.get('customer') or {}).get('name', ''), 'Q4: 改行・制御文字が残る')


def test_page_normalise():
    rows = ['|a|取替|||1|100|||']
    p = reader._normalise_page({'page': 1, 'rows_printed': 1, 'marks': {'＄': 1, '$': 1}, 'blocks': [{'title': 'x', 'rows': rows, 'subtotal': {'parts': '¥100'}}]}, 1)
    chk(p['marks'] == {'$': 2}, f"Q7: marks のキーが半角にそろわない・合算されない: {p['marks']}")
    chk(p['blocks'][0].get('subtotal') == {'parts': 100}, f"Q14: ブロックの小計が数値にならない: {p['blocks'][0].get('subtotal')}")


def test_pages_for_merge_order():
    hdr = {'source': 't'}
    p1 = {'page': 1, 'blocks': [{'title': '', 'rows': ['|a|取替|||1|100|||']}], 'expenses': [{'name': '写真代', 'amount': 1000}],
          'paint_lines': [{'name': '加算基礎数値', 'wage': 1}]}
    p2 = {'page': 2, 'blocks': [{'title': '', 'rows': []}], 'expenses': [{'name': '廃棄処分費', 'amount': 2000}],
          'paint_lines': [{'name': 'ﾘﾔﾊﾞﾝﾊﾟ 取替', 'wage': 2}]}
    h, pages = reader._pages_for_merge(hdr, [p1, p2])
    chk(len(pages) == 1 and [e['name'] for e in pages[0].get('expenses') or []] == ['写真代', '廃棄処分費'], f"Q6: 費用の並びが紙と違う: {pages}")
    chk([x['name'] for x in pages[0].get('paint_lines') or []] == ['加算基礎数値', 'ﾘﾔﾊﾞﾝﾊﾟ 取替'], 'Q6: 塗装行の並びが紙と違う')
    chk(not h.get('expenses') and not (h.get('paint') or {}).get('lines'), 'Q6: header に移している')


def test_reply_errors_use_page_retry():
    """使えない返事（max_tokens で切れた等）は、そのページの読み直しの上限の中でやり直す。課金された失敗の呼び出しも数える（P15）"""
    import tempfile
    sys.path.insert(0, HERE)
    import reg_reader as rr

    class _Cut(rr.FakeReader):
        def __init__(self, answers, cut_at):
            super().__init__(answers)
            self.cut_at = set(cut_at)

        def ask(self, system, blocks, **_):
            if self.calls in self.cut_at:
                self.calls += 1
                raise llm.LLMReplyError('返事が max_tokens で切れた',
                                        llm.LLMReply(text='{"page":', stop_reason='max_tokens', input_tokens=7, output_tokens=9))
            return super().ask(system, blocks)

    # 1 ページ目の初回が途中で切れる → そのページの読み直し（1 回と数える）で合格（読み取りは止まらない）
    fake = _Cut([rr.HEADER, rr.PAGE_OK], cut_at={1})
    res = reader.read_estimate(rr.blank_pdf(1), reader=fake, case_dir=tempfile.mkdtemp(prefix='reg_inputs_'), source_name='t.pdf')
    chk(res.ok and not res.error and res.traces and res.traces[0].attempts == 2,
        f'P15: 1 回の途中切れで読み取りが止まる: error={res.error} traces={res.traces}')
    chk((res.usage or {}).get('calls') == 3, f"P15: 途中で切れた呼び出しを数えていない: {res.usage}")
    # 2 回続けて使えない → 読み直しの上限の中で 3 回目に合格（内側で言い直させない ＝ 1 呼び出し 1 回）
    fake = _Cut([rr.HEADER, rr.PAGE_OK], cut_at={1, 2})
    res = reader.read_estimate(rr.blank_pdf(1), reader=fake, case_dir=tempfile.mkdtemp(prefix='reg_inputs_'), source_name='t.pdf')
    chk(res.ok and res.traces and res.traces[0].attempts == 3 and fake.calls == 4,
        f'P15: 読み直しの数え方: error={res.error} traces={res.traces} calls={fake.calls}')
    # 毎回切れる → 1 ページの呼び出しは 1 + 読み直しの上限まで（以前は内側の言い直しで最大 8 回。レビュー）
    fake = _Cut([rr.HEADER], cut_at=set(range(1, 50)))
    res = reader.read_estimate(rr.blank_pdf(1), reader=fake, case_dir=tempfile.mkdtemp(prefix='reg_inputs_'), source_name='t.pdf',
                               max_retries=3)
    chk(not res.ok and fake.calls == 1 + 4, f'P15: 毎回切れる返事の呼び出し回数: {fake.calls}')

    class _Refuse(rr.FakeReader):
        def ask(self, system, blocks, **_):
            if self.calls >= 1:
                self.calls += 1
                raise llm.LLMReplyError('Claude が読み取りを断った（refusal）',
                                        llm.LLMReply(text='', stop_reason='refusal', input_tokens=7, output_tokens=0), retryable=False)
            return super().ask(system, blocks)
    fake = _Refuse([rr.HEADER])
    res = reader.read_estimate(rr.blank_pdf(1), reader=fake, case_dir=tempfile.mkdtemp(prefix='reg_inputs_'), source_name='t.pdf')
    chk(not res.ok and fake.calls == 2, f'断られた返事を読み直している: calls={fake.calls}')

    # header が断られたら言い直さない（以前は 2 回呼んでいた。レビュー 2 周目）
    class _RefuseAll(rr.FakeReader):
        def ask(self, system, blocks, **_):
            self.calls += 1
            raise llm.LLMReplyError('Claude が読み取りを断った（refusal）',
                                    llm.LLMReply(text='', stop_reason='refusal', input_tokens=7, output_tokens=0), retryable=False)
    fake = _RefuseAll([])
    res = reader.read_estimate(rr.blank_pdf(1), reader=fake, case_dir=tempfile.mkdtemp(prefix='reg_inputs_'), source_name='t.pdf')
    chk(not res.ok and fake.calls == 1, f'断られた header を言い直している: calls={fake.calls}')

    # 2 ページで 1 ページ目だけ断られた: header・1 ページ目・2 ページ目の 3 回だけ（合計欄の読み直しをしない。以前は 6 回）
    class _RefuseP1(rr.FakeReader):
        def ask(self, system, blocks, **_):
            if self.calls == 1:
                self.calls += 1
                raise llm.LLMReplyError('Claude が読み取りを断った（refusal）',
                                        llm.LLMReply(text='', stop_reason='refusal', input_tokens=7, output_tokens=0), retryable=False)
            return super().ask(system, blocks)
    fake = _RefuseP1([rr.HEADER, dict(rr.PAGE_OK, page=2)] + [rr.HEADER] * 3 + [rr.PAGE_OK] * 3)
    res = reader.read_estimate(rr.blank_pdf(2), reader=fake, case_dir=tempfile.mkdtemp(prefix='reg_inputs_'), source_name='t.pdf')
    chk(not res.ok and fake.calls == 3, f'断られたページがあるのに合計欄を読み直している: calls={fake.calls}')


def test_image_frames_and_memory():
    from PIL import Image
    # JPEG の MPO（カメラのプレビュー画像入り）は 1 ページ（2 ページにすると同じ明細を 2 回写す）
    a = Image.new('RGB', (300, 400), (255, 255, 255))
    b = Image.new('RGB', (160, 120), (0, 0, 0))
    buf = io.BytesIO()
    try:
        a.save(buf, format='MPO', save_all=True, append_images=[b])
        chk(_pdf_pages(llm.image_to_pdf(buf.getvalue())) == 1, 'MPO が 2 ページになる')
    except (KeyError, OSError, ValueError):
        pass   # この Pillow は MPO を書けない
    # メモリは子プロセスで測る（tracemalloc は Pillow の画素のメモリを数えない。レビュー 2 周目）
    # 600dpi・A4 の白黒 10 ページの TIFF: 1 ページずつ PDF にするので、メモリは 1 ページぶん（以前は 1.4GB）
    frames = [Image.new('1', (4960, 7016), 1) for _ in range(10)]
    buf = io.BytesIO()
    frames[0].save(buf, format='TIFF', save_all=True, append_images=frames[1:], compression='group4', dpi=(600, 600))
    del frames
    n, mb, _w, _h = _img_mem(buf.getvalue())
    chk(n == 10, f'多ページ TIFF のページ数: {n}')
    chk(mb < 300, f'多ページ TIFF の変換でメモリを使いすぎる: {mb:.0f}MB')
    # 色つきの大きな画像 1 枚（4,900 万画素）: 写しと向き直しで同じ画素を 3〜4 枚持たない（以前は 700MB 超）
    big = Image.new('RGB', (7000, 7000), (255, 255, 255))
    buf = io.BytesIO()
    big.save(buf, format='PNG')
    del big
    n, mb, _w, _h = _img_mem(buf.getvalue())
    chk(n == 1 and mb < 520, f'大きな画像 1 枚の変換でメモリを使いすぎる: {mb:.0f}MB')
    # 展開すると大きすぎる色つきの画像は断る（Pillow は RGB を 1 画素 4 バイトで持つ）
    try:
        llm.image_to_pdf(_png_header_only(9000, 9000))
        FAILS.append('展開すると 300MB を超える画像を断らない')
    except llm.LLMError as e:
        chk('大きすぎ' in str(e), f'大きすぎる画像の理由: {e}')
    # 透過のある大きな画像（3,970 万画素の RGBA）: 写しと帯で同じ画素を 4 枚持たない（以前は 600MB 超。レビュー 3 周目）
    rgba = Image.new('RGBA', (6300, 6300), (255, 255, 255, 0))
    buf = io.BytesIO()
    rgba.save(buf, format='PNG')
    del rgba
    n, mb, _w, _h = _img_mem(buf.getvalue())
    chk(n == 1 and mb < 450, f'透過のある大きな画像の変換でメモリを使いすぎる: {mb:.0f}MB')
    # 4,800 万画素の JPEG は縮小して読む（以前の目標だと縮まらず 230MB）
    big = Image.new('RGB', (8000, 6000), (255, 255, 255))
    buf = io.BytesIO()
    big.save(buf, format='JPEG', quality=80, dpi=(300, 300))
    del big
    n, mb, w_, h_ = _img_mem(buf.getvalue())
    chk(n == 1 and mb < 180, f'大きな JPEG を縮小して読んでいない: {mb:.0f}MB')
    chk(abs(w_ - 8000 / 300 * 72) < 5 and abs(h_ - 6000 / 300 * 72) < 5, f'縮小して読んだ JPEG のページの大きさが変わる: {w_}x{h_}')
    # 回転の印（6）付きの FAX 標準モード（204×98dpi）の TIFF: 縦横比は向きの印の無いものと同じ（Pillow が読み込みで回したのに
    # dpi を保存したときの軸で当て、4 倍に歪めていた。レビュー 3 周目）
    disp = Image.new('1', (1728, 1143), 1)
    sizes = {}
    for o_, tr in ((1, None), (6, Image.Transpose.ROTATE_90), (8, Image.Transpose.ROTATE_270)):
        st_ = disp.transpose(tr) if tr else disp.copy()
        buf = io.BytesIO()
        st_.save(buf, 'TIFF', compression='group4', dpi=((98, 204) if o_ in (6, 8) else (204, 98)), tiffinfo={0x0112: o_})
        sizes[o_] = _page_size(llm.image_to_pdf(buf.getvalue()))
    r1 = sizes[1][1] / sizes[1][0]
    for o_ in (6, 8):
        chk(abs(sizes[o_][1] / sizes[o_][0] - r1) < 0.03, f'回転の印 {o_} の FAX の縦横比: {sizes[o_]}（印なし {sizes[1]}）')
    # スマホの縦撮り（EXIF の向き 6）は縦向きのページに（縮小のあとで向きを直しても向きは同じ）
    ph = Image.new('RGB', (400, 300), (255, 255, 255))
    ex = ph.getexif()
    ex[0x0112] = 6
    buf = io.BytesIO()
    ph.save(buf, format='JPEG', exif=ex)
    w_, h_ = _page_size(llm.image_to_pdf(buf.getvalue()))
    chk(w_ < h_, f'EXIF の向きを直していない: {w_}x{h_}')


_MEM_CHILD = r'''
import io, os, sys
sys.path.insert(0, sys.argv[1])
from neo_skill import llm


def _mem():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        gpmi = ctypes.windll.psapi.GetProcessMemoryInfo
        gpmi.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        gpmi(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PagefileUsage, pmc.PeakPagefileUsage
    import resource
    with open('/proc/self/statm') as f:
        cur = int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    return cur, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


data = open(sys.argv[2], 'rb').read()
cur0, _p = _mem()
pdf = llm.image_to_pdf(data)
_c, peak = _mem()
from pypdf import PdfReader
r = PdfReader(io.BytesIO(pdf))
print(len(r.pages), int(peak - cur0), float(r.pages[0].mediabox.width), float(r.pages[0].mediabox.height))
'''


def _img_mem(data: bytes):
    """子プロセスで image_to_pdf を動かし、(ページ数, 変換で増えたメモリの山 MB, 1 ページ目の幅, 高さ) を返す"""
    import subprocess
    import tempfile
    fd, path = tempfile.mkstemp(suffix='.img')
    os.write(fd, data)
    os.close(fd)
    try:
        out = subprocess.run([sys.executable, '-c', _MEM_CHILD, os.path.dirname(HERE), path],
                             capture_output=True, text=True, timeout=600)
    finally:
        os.unlink(path)
    parts = (out.stdout or '').strip().split()
    if out.returncode != 0 or len(parts) < 4:
        raise RuntimeError(f'子プロセスが失敗: {(out.stderr or "")[-300:]}')
    return int(parts[0]), int(parts[1]) / 1024 / 1024, float(parts[2]), float(parts[3])


def _png_header_only(w: int, h: int) -> bytes:
    """大きさだけ申告する PNG（画素は展開前に断るので中身は要らない）"""
    import struct
    import zlib

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00' * 16)) + chunk(b'IEND', b''))


def test_gemini_reader_timeout_not_retried():
    '''GeminiReader は締め切り切れ（httpx の締め切り・504）を送り直さない（締め切り 10 分を最大 6 回待たせていた）。
    接続の失敗は送り直す（レビュー 3 周目）'''
    import httpx
    try:
        g = llm.GeminiReader(api_key='dummy-key-for-test', model='m')
    except Exception:  # noqa: BLE001  google-genai が無い環境
        return
    calls = {'n': 0}

    class _E504(Exception):
        code = 504

    def make(exc):
        class _Models:
            def generate_content(self, **kw):
                calls['n'] += 1
                raise exc

        class _Client:
            models = _Models()
        return _Client()
    import time as _t
    real_sleep = _t.sleep
    _t.sleep = lambda *_a, **_k: None
    try:
        for exc, want in ((httpx.ReadTimeout('read timed out'), 1), (_E504('504 DEADLINE_EXCEEDED'), 1),
                          (httpx.ConnectError('[Errno 110] Connection timed out'), 6)):
            calls['n'] = 0
            g.client = make(exc)
            try:
                g.ask('sys', [{'type': 'text', 'text': 'x'}])
                FAILS.append(f'GeminiReader が {type(exc).__name__} で例外にならない')
            except llm.LLMError:
                pass
            chk(calls['n'] == want, f'GeminiReader の {type(exc).__name__} の送り回数: {calls["n"]}（期待 {want}）')
    finally:
        _t.sleep = real_sleep


def test_reader_value_details():
    base = {'source': 't', 'issuer': 't', 'est_date': '20260915', 'format': 'B',
            'customer': {'name': 'x', 'kilometer': '0'},
            'insurance': {'repair_days': '2週間', 'accident_date': '2026年09月01日(火)'}}
    notes: list = []
    h = reader._normalise_header(base, None, None, {'kilometer': '15345'}, notes=notes)
    chk((h.get('customer') or {}).get('kilometer') in (None, '', '15345'), f"走行距離 0 が車検証の値を止める: {h.get('customer')}")
    chk('repair_days' not in (h.get('insurance') or {}), f"修理日数「2週間」を 2 日にしている: {h.get('insurance')}")
    chk((h.get('insurance') or {}).get('accident_date') == '20260901', f"曜日付きの日付を落としている: {h.get('insurance')}")


def test_index_policy_format_c():
    pages = [{'blocks': [{'rows': ['|a|取替|||1|100|||']}]}]
    chk(reader._index_policy_guard({'index_policy': 'manual', 'format': 'C'}, pages) is None, 'Q12: 書式 C の manual を外している')
    chk(reader._index_policy_guard({'index_policy': 'manual', 'format': 'B'}, pages) is not None, '既存: 書式 B の manual を外さない')


def test_manual_rows_guard():
    """読み手が付けた M（手入力）: 部品コードの印字が無い見積では外す（ADDATA に有るかは下書きが決める。2026-09-20 本番 フリード協定見積）。
    コードの印字がある見積・書式 B・汎用車種・輸入車では外さない"""
    veh = {'model_code': 'GB8', 'serial_no': 'GB8-0000001'}
    pages = [{'blocks': [{'rows': ['|左　テールゲートアツパ|取替||||1319||M*|', '|エンブレム（Ｈ）|取替||||1959||Ｍ|',
                                   {'name': 'ｶﾞﾗｽｾﾂﾁﾔｸｻﾞｲ', 'method': '取替', 'price': 7999, 'manual': True},
                                   {'name': 'ﾄﾗﾝｸﾄﾞﾚﾝﾌﾟﾗｸﾞ', 'price': 320, 'flags': 'M'},
                                   '|インテリジェントクリアランスソナー|||||||N|', '|Rrバンパフェイス|取替||||51399|7650|*|']}]}]
    out, why = reader._manual_rows_guard({'format': 'F', 'vehicle': veh}, pages)
    rows = out[0]['blocks'][0]['rows']
    chk(why is not None and '4 行' in why, f'M を外した行数の注意が無い: {why}')
    chk(rows[0].split('|')[8] == '*' and rows[1].split('|')[8] == '', f'文字列行の M（全角含む）を外していない: {rows[:2]}')
    chk('manual' not in rows[2] and rows[3].get('flags') == '', f'dict 行の manual / flags の M を外していない: {rows[2:4]}')
    chk(rows[4] == pages[0]['blocks'][0]['rows'][4] and rows[5] == pages[0]['blocks'][0]['rows'][5], '注記行・M の無い行を変えている')
    chk('M' in pages[0]['blocks'][0]['rows'][0].split('|')[8], '元の pages を書き換えている')
    coded = [{'blocks': [{'rows': ['3810|Rrﾊﾞﾝﾊﾟﾌｪｲｽ|取替||||51399|7650||', '|塗装費用||||||94664|M|']}]}]
    chk(reader._manual_rows_guard({'format': 'A', 'vehicle': veh}, coded)[1] is None, 'コードの印字がある見積の M を外している')
    coded2 = [{'blocks': [{'rows': [{'code': '０２３０-02', 'name': 'x', 'price': 1}, '|塗装費用||||||94664|M|']}]}]
    chk(reader._manual_rows_guard({'format': 'A', 'vehicle': veh}, coded2)[1] is None, '枝番付き・全角の部品コードを「コードの印字」とみなしていない')
    numbered = [{'blocks': [{'rows': ['1|左 ﾃｰﾙｹﾞｰﾄｱﾂﾊﾟ|取替||||1319||M|', '12|ｴﾝﾌﾞﾚﾑ(H)|取替||||1959||M|']}]}]
    chk(reader._manual_rows_guard({'format': 'F', 'vehicle': veh}, numbered)[1] is not None, '行番号（1・12）を部品コードとみなして M を残している')
    chk(reader._manual_rows_guard({'format': 'B', 'vehicle': veh}, pages)[1] is None, '書式 B（素材欄の *）の M を外している')
    chk(reader._manual_rows_guard({'format': 'D', 'vehicle': {'generic': 'true'}}, pages)[1] is None, '汎用車種の M を外している')
    chk(reader._manual_rows_guard({'format': 'D', 'vehicle': {'serial_no': 'YV1XXXXXXXX000001'}}, pages)[1] is None, '輸入車（VIN）の M を外している')
    chk(reader._manual_rows_guard({'format': 'D', 'vehicle': {'car_name': 'ボルボ V40'}}, pages)[1] is None, '輸入車（車名）の M を外している')



def test_paint_actual_guard():
    """塗装が一式だけの見積は、コグニの入力方式を実額にする（2026-09-20 亮平さん指示）。
    材料代の金額が印字されているときと、塗装の内訳があるときは実額にしない"""
    pages = [{'page': 1, 'blocks': [{'rows': ['0010|ﾊﾞﾝﾊﾟ|取替||||38600|8000||']}]}]
    lump = {'paint': {'total': 99080}}
    h, note = reader._paint_actual_guard(lump, pages)
    chk(h['paint'].get('input_type') == '実額' and note, '一式だけの塗装を実額にしていない')
    chk(lump['paint'].get('input_type') is None, '元の header を書き換えている（読み手の写しは触らない）')

    # 材料代の割合だけ（金額 0）でも実額にする ← 本番検証で実額にならなかった形
    h2, note2 = reader._paint_actual_guard({'paint': {'total': 200000, 'material': 0, 'material_rate': 26}}, pages)
    chk(h2['paint'].get('input_type') == '実額' and note2, '材料代 0 円（割合だけ）で実額にしていない')

    # 材料代の金額が印字されている → 実額にしない（実額は材料計の欄を持てない）
    h3, note3 = reader._paint_actual_guard({'paint': {'total': 85520, 'material': 19226}}, pages)
    chk(h3['paint'].get('input_type') is None and not note3, '印字の材料代があるのに実額にした')

    # 塗装の内訳（パネル・バンパ・内板骨格・追加項目 等）がある → 実額を勧めない
    for k, v in (('panels', [{'name': 'ﾄﾞｱ'}]), ('other', [{'name': 'ｱﾝﾀﾞｰｺｰﾄ'}]),
                 ('frame', {'wage': 1000}), ('bumper_front', {'wage': 1000}), ('auto_panels', True)):
        hx, nx = reader._paint_actual_guard({'paint': {'total': 99080, k: v}}, pages)
        chk(hx['paint'].get('input_type') is None and not nx, f'塗装の内訳（{k}）があるのに実額にした')

    # 塗装行だけのとき（「塗装費用 一式」が塗装行で読まれる書式）は実額を勧める。
    # パネルに当てられる見積なら生成器が実額を無視して指数のままにするので、ここでは行の有無で決めない
    h4, note4 = reader._paint_actual_guard({'paint': {}}, [{'page': 1, 'paint_lines': [{'name': '塗装費用', 'wage': 200000}], 'blocks': []}])
    chk(h4['paint'].get('input_type') == '実額' and note4, 'ページの塗装行だけのときに実額を勧めていない')
    h4b, note4b = reader._paint_actual_guard({'paint': {'lines': [{'name': '塗装費用', 'wage': 200000}]}}, pages)
    chk(h4b['paint'].get('input_type') == '実額' and note4b, 'header の塗装行だけのときに実額を勧めていない')

    # 合計欄の材料代が印字されている → 実額にしない
    h4c, note4c = reader._paint_actual_guard({'paint': {'total': 99080}, 'totals': {'material': 12000}}, pages)
    chk(h4c['paint'].get('input_type') is None and not note4c, '合計欄の材料代があるのに実額にした')

    # 読み取りの指定が先（実額にも指数にも勝手に変えない）
    h5, note5 = reader._paint_actual_guard({'paint': {'total': 99080, 'input_type': '指数'}}, pages)
    chk(h5['paint'].get('input_type') == '指数' and not note5, '読み取りの input_type を上書きした')

    # 塗装が無い・0 円なら何もしない
    for pa in ({}, {'paint': {}}, {'paint': {'total': 0}}):
        h6, n6 = reader._paint_actual_guard(pa, pages)
        chk((h6.get('paint') or {}).get('input_type') is None and not n6, f'塗装が無い（{pa}）のに実額を付けた')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                FAILS.append(f'{name}: 例外 {type(e).__name__}: {e}')
    for f in FAILS:
        print('*** FAILED:', f)
    print('reg_inputs:', 'all ok' if not FAILS else f'{len(FAILS)} 件が不合格')
    sys.exit(1 if FAILS else 0)
