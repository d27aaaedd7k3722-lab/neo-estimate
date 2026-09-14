"""
paint_index.py — コグニセブン塗装指数の再現（ADDATA 車種別 20.DB / 23.DB / CHM ＋ COM T_KEI_3 / BOOTH）

補修塗装指数 = ①塗り数値（パネル別・CHM 表）＋ ②加算基礎数値（塗料×塗膜×高機能×枚数・T_KEI_3.DB）＋ ③付加数値
実 NEO（C-HR W69 / N-BOX J97）で以下を確認済み:
  PaintingPanel.TimeStandardNew/1/2/3 = CHM「塗り数値」(複数塗) + 高機能塗装加算（耐スリ傷: floor10(0.3 + 0.01×面積)）
  PaintingPanel.PanelArea / PanelName / PanelDivision / PanelTypeDivision / PanelCode / ButtonNo = 20.DB
  PaintingPlan.BaseTime = T_KEI_3.DB (車形, 塗料, 塗膜, B/F/S/T, 枚数)
  PaintingPlan.BoothTime = BOOTH.DB (車形, 塗料, 塗膜, B/F/S/T)
  PaintingBumper.fb_Time = <car>23.DB (F/R, 塗膜クラス, 取替一色/二色, 外傷大, 外傷小, 変形)
"""
from __future__ import annotations

import glob
import html
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PAINT_CODE = {'速乾': 1, '速乾ウレタン': 1, '２Ｋ': 3, '2K': 3, '水性': 4}
COAT_CODE = {'ソリッド': 1, 'メタリック': 2, '２コートパール': 3, '2コートパール': 3, '３コートパール': 4, '3コートパール': 4}
HF_CODE = {'しない': 0, 'フッ素': 1, '耐スリ傷': 2, 'スクラッチ': 3, 'スクラッチシールド': 3, 'ｽｸﾗｯﾁ': 3}
HF_NAME = {0: 'しない', 1: 'フッ素', 2: '耐スリ傷', 3: 'ｽｸﾗｯﾁ'}  # PaintingPlan.HFPaintingName（実 NEO の書き方。スクラッチは半角）
HF_KIND = {0: 'B', 1: 'F', 2: 'T', 3: 'S'}  # T_KEI_3 / BOOTH の種別列。S = スクラッチ（実案件 NEO 100 本で HFPainting 3 'ｽｸﾗｯﾁ'。2026-09-12）
COAT_CLASS_BUMPER = {1: 1, 2: 2, 3: 3, 4: 4}  # 23.DB 第 8 列 = PaintingPlan.Coat（1 ソリッド / 2 メタリック / 3 2コートパール / 4 3コートパール）。実機 J52: ソリッド 1.1=行1、2コートパール 1.3=行3、3コートパール 1.5=行4（2026-09-05）


def _panel_key(name: str) -> str:
    """20.DB のパネル名を突き合わせる用に揃える（前後の空白と中の空白を落とす）"""
    return (name or '').replace(' ', '').replace('　', '')


def floor10(x: float) -> float:
    return math.floor(x * 10 + 1e-9) / 10.0


def _xor_lines(path: str) -> list[str]:
    raw = bytes(b ^ 0xFF for b in open(path, 'rb').read())
    return [l for l in raw.decode('cp932', 'replace').splitlines() if l.strip()]


class PaintIndex:
    _warned_chm = False  # CHM を展開できない旨の警告は 1 プロセスに 1 回だけ

    def __init__(self, root: str, car: str, com_dir: Optional[str] = None, body: str = ''):
        # ボディコード（KA81 の 2 桁）。20.DB は同じパネルでもボディで面積が違う行を持つ
        self.body_code = int(str(body).strip()) if str(body).strip().isdigit() else 0
        self.body_unresolved: list = []  # このボディ用の行を選べなかったパネル（面積が実機とずれる可能性がある）
        self.root = root
        self.car = car
        self.car_dir = os.path.join(root, car[0], car)
        if com_dir is None:  # 使用中の ADDATA の COM.CAB を展開したもの（ADDATA の月次更新に追従）。展開できなければ同梱の予備
            try:
                import com_tables
                com_dir = com_tables.com_dir(root)
            except Exception:  # noqa: BLE001
                com_dir = None
        self.com_dir = com_dir or os.path.join(HERE, 'reference')
        self.panels = self._load_20()
        self.chm_rows, self.chm_base = self._load_chm()
        # CHM（車種別補修塗装指数）を **展開できなかった** PC では、修正塗装の標準指数が取れない。
        # 黙って別の値で通ると見積が静かにずれるので 1 回だけ知らせる（hh.exe が無い／ポリシーで使えない PC 対策）。
        # 展開はできたが補修塗装指数のページが無い車種（古い車種の CHM）は正常なので警告しない
        self.chm_unavailable = bool(getattr(self, '_chm_extract_failed', False))
        if self.chm_unavailable and PaintIndex._warned_chm is not True:
            PaintIndex._warned_chm = True
            hh_ = os.path.join(os.environ.get('WINDIR', ''), 'hh.exe')
            print(f'★ 塗装指数表（CHM）を展開できない: {self.car}。修正塗装の標準指数が取れないので '
                  f'paint.panels[].index を見積書の値で書くこと（{hh_} が使えるか確認）')
        self._chm_water: Optional[list[dict]] = None  # 水性ページ（CHM「車種別補修塗装指数（水性）」）は要求時に読む

    # ------------------------------------------------------------ 20.DB: 塗装パネルマスタ
    def _load_20(self) -> list[dict]:
        """400B ヘッダ（部品コード上2桁 → レコード範囲）+ 39B レコード×N
        [code u16][area u16][0x20][body u8][5B][flag 1][name 20B][PanelDivision 1][PanelTypeDivision 1][PanelCode 1|' '][ButtonNo 2][' ']

        body = ボディコード（0 = 全ボディ共通、10 / 20 = そのボディ専用。KA81 の id と同じ 1 バイト整数）。
        同じパネルコードでもボディで面積が変わる（W90 ハイエース: L ｽﾗｲﾄﾞﾄﾞｱﾊﾟﾈﾙ 178（共通/10）と 196（20））ので、
        車のボディに合う行を選ばないと塗装指数が実機と食い違う（2026-09-11 に 20.DB の生バイトで確認）"""
        p = os.path.join(self.car_dir, f'{self.car}20.DB')
        out = []
        if not os.path.exists(p):
            return out
        b = open(p, 'rb').read()
        for o in range(400, len(b) - 38, 39):
            r = b[o:o + 39]
            code, area = struct.unpack_from('<HH', r, 0)
            dv = r[33:36].decode('cp932', 'replace')
            btn = r[36:38].decode('cp932', 'replace')
            out.append({'code': f'{code:04d}', 'area': area, 'name': r[13:33].decode('cp932', 'replace'),
                        'body': r[5], 'flag': chr(r[11]) if 32 <= r[11] < 127 else ' ',
                        'div': int(dv[0]) if dv[0].strip() else 0, 'type': int(dv[1]) if dv[1].strip() else 0,
                        'pcode': int(dv[2]) if dv[2].strip() else 0, 'btn': int(btn) if btn.strip() else 0})
        return out

    def _note_areas(self, rows: list, note: bool = True) -> None:
        """同じコード・同じ条件の行が複数あって**面積が違う**ときは控える。
        面積 = 塗装指数なので、どちらを採るかで見積が変わる。黙って先頭を採ると誰も気づけない。
        ADDATA 全 1,204 車種で 258 組ある（C39 ﾌ-ﾄﾞ 0600 が 131 と 149、C48 LRﾄﾞｱ 2700 が 67 と 80 など。
        2026-09-12 に 20.DB を全走査して集計）"""
        if not note or len(rows) < 2:
            return
        areas = sorted({int(r.get('area') or 0) for r in rows})
        if len(areas) > 1:
            self._note_unresolved(rows[0]['code'], [], areas=areas)

    def _pick_body(self, rows: list, note: bool = True) -> Optional[dict]:
        """同じパネルコードの行から、この車のボディに合うものを選ぶ。
        ボディ専用行 → 全ボディ共通（body 0）→ 先頭 の順（ボディが分からない車は従来どおり先頭）。
        選んだ先に面積の違う行が残っていたら控える（黙って先頭を採らない）"""
        if not rows:
            return None
        b = self.body_code
        if b:
            hit = [r for r in rows if r.get('body') == b]
            if hit:
                self._note_areas(hit, note)
                return hit[0]
            common = [r for r in rows if not r.get('body')]
            if common:
                self._note_areas(common, note)
                return common[0]
            if note:
                # このボディ用も全ボディ共通も無い。他ボディの面積を使うことになるので控える
                self._note_unresolved(rows[0]['code'], [])
        else:
            self._note_areas(rows, note)   # ボディが分からない車でも、面積が割れていれば知らせる
        return rows[0]

    def panel_for_body(self, code: str) -> Optional[dict]:
        """20.DB に**この車のボディ用（またはボディ共通）の行がある**ときだけ、その行を返す。無ければ None。
        panel_exact と違って他ボディ専用行へ逃げない。PaintingLinkParts（W/S 連動の紐付け）用:
        コグニは、この車のボディに載っていないパネルは連動しない
        （コグニ実機 2026-09-12 W90 ハイエース ボディ 20: 明細 4800 取替は 20.DB にボディ 10 専用行しか無く、
        PaintingLinkParts に出なかった。生成器は panel_exact で他ボディ行を拾って書いていた）"""
        c = str(code).zfill(4)
        rows = [r for r in self.panels if r['code'] == c]
        b = self.body_code
        if not rows:
            return None
        if not b:
            return rows[0]
        hit = [r for r in rows if r.get('body') == b] or [r for r in rows if not r.get('body')]
        return hit[0] if hit else None

    def panel_exact(self, code: str):
        """20.DB に完全一致するパネル行（前方一致フォールバックを使わない）。この車のボディに合う行を選ぶ。
        同じコードにこのボディ用が無いときは他ボディ専用行を返す —— **枝番違い（4800 → 4801）の補正が要るなら
        `panel()` を使う**（ボディで別コードになっている車がある）"""
        c = str(code).zfill(4)
        return self._pick_body([r for r in self.panels if r['code'] == c], note=False)

    def _note_unresolved(self, code: str, cands: list, areas=None) -> None:
        """選べなかったパネルを控える（面積＝塗装指数が実機とずれる可能性）。
        cands … 枝番の行き先が複数（コードが違う） / areas … 同じコードに面積の違う行が複数 /
        どちらも空 … このボディ用の行が無く、他ボディの面積を使う"""
        if not any(u['code'] == code for u in self.body_unresolved):
            self.body_unresolved.append({'code': code, 'body': self.body_code,
                                         'candidates': cands, 'areas': list(areas or [])})

    def panel(self, code: str) -> Optional[dict]:
        code = re.sub(r'\D', '', code or '')[:4].zfill(4)
        same = [r for r in self.panels if r['code'] == code]
        b = self.body_code
        if same and not b:
            self._note_areas(same, True)   # 同じコードで面積が割れていたら知らせる（黙って先頭を採らない）
            return same[0]
        if same:
            hit = [r for r in same if r.get('body') == b] or [r for r in same if not r.get('body')]
            if hit:
                self._note_areas(hit, True)
                return hit[0]
            # 同じコードに、このボディ用も全ボディ共通も無い（他ボディ専用しか無い）。
            # そのときは枝番違いにこのボディ用の行があることがある
            # （W90 ハイエース: 4800 = ボディ 10 専用 / 4801 = ボディ 20 専用）
        pre = [r for r in self.panels if r['code'][:3] == code[:3]]
        if b and same:
            # 枝番へ飛ぶのは **同じパネル名** のときだけ。3 桁が同じでも別物のことがある
            # （4800 ｸｵ-ﾀﾊﾟﾈﾙ と 4801 ｸｵ-ﾀﾊﾟﾈﾙ(工賃) のような組）
            base = _panel_key(same[0]['name'])
            hit = [r for r in pre if r.get('body') == b and _panel_key(r['name']) == base]
            # 行き先が 1 コードに定まるときだけ飛ぶ。同じ名前・同じボディでも面積が違う枝番があるので
            # （ADDATA 120 車種で 2,539 組中 20 組。C39 ﾌ-ﾄﾞ 131/149 など）、複数あるなら選べない
            codes = {r['code'] for r in hit}
            if len(codes) == 1:
                return hit[0]
            if codes:
                # 候補が複数。黙って他ボディの面積を使うと塗装指数が実機とずれるので、選べなかったことを残す
                self._note_unresolved(code, sorted(codes))
        if same:
            if b and not any(r.get('body') in (b, 0) for r in same):
                self._note_unresolved(code, [])  # このボディ用も共通も無い（他ボディ専用しか無い）
            return same[0]  # 枝番でも見つからなければ同じコードの先頭（従来どおり）
        # 枝番違い（4801 → 4800 等）
        if len({r['code'] for r in pre}) == 1:  # 3 桁前方一致は行き先が 1 コードのときだけ（複数あると別パネルの面積・名称を黙って使ってしまう。監査 6）
            return self._pick_body(pre)
        return None

    # ------------------------------------------------------------ CHM: 補修塗装指数（溶剤系）
    def _chm_path(self) -> Optional[str]:
        p76 = os.path.join(self.car_dir, f'{self.car}76.DB')
        if os.path.exists(p76):
            for l in _xor_lines(p76):
                m = re.search(r'(\S+LTB\.CHM)', l, re.I)
                if m and '塗り数値' in l:
                    q = os.path.join(self.car_dir, m.group(1))
                    if os.path.exists(q):
                        return q
        c = glob.glob(os.path.join(self.car_dir, '*LTB.CHM')) + glob.glob(os.path.join(self.car_dir, '*LTB.chm'))
        return c[0] if c else None

    @staticmethod
    def _cache_ok(d: str) -> bool:
        """CHM を展開したフォルダとして使えるか（索引 .hhc と本文 html/*.html が揃っている）。
        hh.exe が途中で終わると .hhc だけ残ることがあり、それを使うと本文の読み込みで落ちる"""
        # 本文の拡張子は車種によって .html と .htm がある（C88 C9500LTB は .htm）ので両方を見る
        return bool(glob.glob(os.path.join(d, '*.hhc'))
                    and (glob.glob(os.path.join(d, 'html', '*.htm')) or glob.glob(os.path.join(d, 'html', '*.html'))))

    @staticmethod
    def _extract_chm_hh(hh: str, chm: str, tmp: str) -> bool:
        """hh.exe -decompile を起動する（Windows 付属。非同期に書くので、揃ったかは呼び出し側が _cache_ok で待つ）。起動できたら True"""
        if not os.path.isfile(hh):
            return False
        try:
            subprocess.run([hh, '-decompile', tmp, chm], timeout=60)
            return True
        except Exception:  # noqa: BLE001  失敗・時間切れ
            return False

    @staticmethod
    def _extract_chm_7z(chm: str, tmp: str) -> bool:
        """7z x で CHM を展開する（Linux / Streamlit Cloud の p7zip、または Windows の 7-Zip。同期）。
        7z の展開は hh.exe -decompile と同じ .hhc / html/*.htm を作る（内部ファイル #SYSTEM 等が余分に出るが害はない。
        2026-09-14 に J8200LTB.CHM で 155 ファイル全部一致を確認）"""
        import com_tables
        sz = com_tables.find_7z()
        if not sz:
            return False
        try:
            os.makedirs(tmp, exist_ok=True)
            r = subprocess.run([sz, 'x', '-y', '-o' + tmp, chm], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _decompile(self, chm: str) -> Optional[str]:
        """CHM を LOCALAPPDATA に展開してそのフォルダを返す。展開済みなら再利用する。
        途中で終わったキャッシュ（.hhc だけ等）はそのまま使わずに作り直す。
        展開は一時フォルダで行い、揃ってから本来の場所へ移すので、同時に 2 つ動いても壊れない"""
        base = os.path.join(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(), 'claude_neo_pipeline', 'chm')  # LOCALAPPDATA の無い Linux では一時フォルダ（配布物の中に書かない: アプリは vendor の内容ハッシュを照合する）
        try:  # キャッシュ名に CHM の内容ハッシュを入れる（別の ADDATA の版・置き場の同名 CHM を取り違えない。Linux の共有一時フォルダでも安全。Codex 指摘）
            import hashlib
            h = hashlib.sha1()
            with open(chm, 'rb') as f_:
                for chunk in iter(lambda: f_.read(1 << 20), b''):
                    h.update(chunk)
            ident = '_' + h.hexdigest()[:12]
        except OSError:  # 読めない CHM は「展開できない」扱い（名前だけのキャッシュに落として別物を掴まない。Codex 指摘）
            return None
        cache = os.path.join(base, os.path.splitext(os.path.basename(chm))[0] + ident)
        if self._cache_ok(cache):
            return cache
        hh = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'hh.exe')
        # 一時フォルダは呼び出しごとに一意にする（同じプロセスの別スレッドが同時に展開しても混ざらない）。
        # rename で公開するので、置き場所はキャッシュと同じドライブでなければならない
        try:
            os.makedirs(base, exist_ok=True)
            self._sweep_tmp(cache)  # 前回異常終了で残った一時フォルダを片付ける
            tmp = tempfile.mkdtemp(dir=base, prefix=os.path.basename(cache) + '.', suffix='.tmp')
        except OSError:
            return cache if self._cache_ok(cache) else None
        launched = self._extract_chm_hh(hh, chm, tmp)
        if launched:
            for _ in range(40):  # hh.exe は非同期で書き終わるので、揃うまで待つ
                if self._cache_ok(tmp):
                    time.sleep(0.5)
                    break
                time.sleep(0.25)
        if not self._cache_ok(tmp):
            # hh.exe が無い（Linux / Streamlit Cloud）か、起動はしたが揃わない（ポリシーで止められる等）→ 7z（p7zip / 7-Zip）で同期展開（Codex 指摘）
            shutil.rmtree(tmp, ignore_errors=True)
            used_7z = self._extract_chm_7z(chm, tmp)
            if not used_7z or not self._cache_ok(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
                if not launched and not used_7z:  # どの道具も無い: 待っても揃わないので待たない
                    return cache if self._cache_ok(cache) else None
                return cache if self._cache_ok(cache) else self._wait_cache(cache)
        if self._cache_ok(cache):  # 展開している間に別プロセスが完成させた → 相手のものを使う（消さない）
            shutil.rmtree(tmp, ignore_errors=True)
            return cache
        # 公開済みのフォルダは決して消さない（読んでいる別プロセスの足元を崩さないため）。
        # まず「置いてみる」。置き場所が空なら rename は成功し、誰のものも触らずに済む
        try:
            os.rename(tmp, cache)
            return cache if self._cache_ok(cache) else self._wait_cache(cache)
        except OSError:  # 既に何かある（Windows は中身のあるフォルダへは rename できない）
            pass
        if self._cache_ok(cache):  # 直前に別プロセスが完成させていた → 相手のものを使う（消さない）
            shutil.rmtree(tmp, ignore_errors=True)
            return cache
        # 途中で終わった不完全なキャッシュが居座っている → どけてから自分のものを置く
        if self._retire(cache):
            try:
                os.rename(tmp, cache)
            except OSError:  # その隙に別プロセスが置いた
                pass
        shutil.rmtree(tmp, ignore_errors=True)
        return cache if self._cache_ok(cache) else self._wait_cache(cache)

    @staticmethod
    def _sweep_tmp(cache: str, age: float = 3600.0) -> None:
        """異常終了で残った展開途中の一時フォルダを捨てる。動いている展開を巻き込まないよう、
        更新から age 秒より古いものだけを対象にする"""
        now = time.time()
        for d in glob.glob(f'{cache}.*.tmp'):
            try:
                if os.path.isdir(d) and now - os.path.getmtime(d) > age:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:  # 消えた / 触れない
                pass

    @staticmethod
    def _retire(d: str) -> bool:
        """公開済みのキャッシュを **消さずに** どける。読んでいる別プロセスがいれば rename が失敗するので何もしない。
        （公開フォルダを直接 rmtree すると、既にそれを使い始めた別プロセスの足元を崩す）"""
        stale = f'{d}.stale.{os.getpid()}.{time.time_ns()}'
        try:
            os.rename(d, stale)
        except OSError:  # 無い / 使用中 / 別プロセスが処理中
            return False
        shutil.rmtree(stale, ignore_errors=True)
        return True

    @classmethod
    def _wait_cache(cls, cache: str, seconds: float = 8.0) -> Optional[str]:
        """別のプロセスが同じ CHM を展開している最中かもしれないので、少しだけ完成を待つ。
        2 人が同時に見積を作ると hh.exe が競合し、自分の展開だけ間に合わないことがあるため"""
        end = time.time() + seconds
        while time.time() < end:
            if cls._cache_ok(cache):
                return cache
            time.sleep(0.25)
        return None

    def _chm_page(self, d: str, water: bool) -> tuple:
        """展開フォルダ d から「補修塗装指数（溶剤/水性）」のページ位置と本文を返す。
        (ページ位置, 本文) を返し、ページが索引に無ければ (None, None)、位置は分かるが読めなければ (位置, None)"""
        hhc_list = glob.glob(os.path.join(d, '*.hhc'))
        if not hhc_list:
            return None, None
        h = open(hhc_list[0], 'rb').read().decode('cp932', 'replace')
        items = re.findall(r'<param name="Name" value="([^"]*)">\s*<param name="Local" value="([^"]*)">', h)
        key = '水性' if water else '溶剤'
        page = next((l for n, l in items if n.startswith('補修塗装指数') and key in n and '#' not in l), None)
        if not page:
            return None, None
        try:
            return page, open(os.path.join(d, page.replace('/', os.sep)), 'rb').read().decode('cp932', 'replace')
        except OSError:
            return page, None

    def _load_chm(self, water: bool = False) -> tuple[list[dict], dict]:
        """water=True で CHM の「車種別補修塗装指数（水性）」ページ（76.DB 997600）を読む。コグニ実機 W66: 塗料を水性にするとクォータ 2.3→2.6、ロッカ 1.4→1.6（2026-09-05）"""
        chm = self._chm_path()
        if not chm:
            return [], {}
        d = self._decompile(chm)
        self._chm_extract_failed = not d  # 展開そのものに失敗したか（ページが無いだけの車種と区別する）
        if not d:
            return [], {}
        page, t = self._chm_page(d, water)
        if t is None and page is not None:
            # 本文が読めない = 展開が途中（hh.exe は非同期に書く）か壊れたキャッシュ。
            # その見積 1 回ぶんを指数なしで作ってしまわないよう、**この呼び出しの中で**作り直して 1 度だけやり直す。
            # 使っている別プロセスがいるかもしれないので、公開フォルダは消さずに「どける」
            # どけられなければ（別プロセスが展開・使用中）壊さない。代わりに相手が書き終わるのを待って読み直す
            if self._retire(d):
                d2 = self._decompile(chm)
                if d2:
                    page, t = self._chm_page(d2, water)
                    d = d2
            else:
                for _ in range(6):  # 3 秒まで待つ
                    time.sleep(0.5)
                    page, t = self._chm_page(d, water)
                    if t is not None:
                        break
        if t is None:
            if page is not None:  # 目的のページが見つかっているのに読めない = 展開に失敗している
                self._chm_extract_failed = True
                self._retire(d)  # 次回作り直せるようどける（使用中なら何もしない）
            return [], {}
        t = re.sub(r'<script.*?</script>|<style.*?</style>', '', t, flags=re.S)
        t = re.sub(r'<[^>]+>', ' ', t)
        t = html.unescape(t).replace('\xa0', ' ').replace('　', ' ')
        t = re.sub(r'\s+', ' ', t)
        rows = []
        seg = t.split('①塗り数値', 2)[-1] if '①塗り数値' in t else t
        seg = seg.split('②加算基礎数値')[0]
        num = r'(\d+\.\d|-)'
        for m in re.finditer(r'(?<![\d.])(\d{1,2}) (.+?) (\d{1,3}) ' + ' '.join([num] * 6), seg, flags=re.ASCII):
            no, name, area = int(m.group(1)), m.group(2).strip(), int(m.group(3))
            vals = [None if v == '-' else float(v) for v in m.groups()[3:9]]
            rows.append({'no': no, 'name': name, 'area': area, 'new_multi': vals[0], 'new_single': vals[1],
                         'r11': vals[2], 'r12': vals[3], 'r13': vals[4], 'hf': vals[5]})
        base = {}
        if '②加算基礎数値' in t:
            seg2 = t.split('②加算基礎数値', 1)[1]
            for m in re.finditer(r'(ソリッド|メタリック|２コートパール|３コートパール)?\s*(速乾|２Ｋ|水性)\s+' + r'\s+'.join([r'(\d\.\d)'] * 5), seg2):
                pass
        return rows, base

    def chm_rows_for_paint(self, paint: int) -> list[dict]:
        """PaintingPlan.Paint 4（水性）は CHM の水性ページ、それ以外は溶剤系ページ。水性ページが無い CHM は溶剤系で代用"""
        if int(paint or 0) == 4:
            if self._chm_water is None:
                self._chm_water = self._load_chm(water=True)[0]
            if self._chm_water:
                return self._chm_water
        return self.chm_rows

    def chm_row_for(self, pn: dict, paint: int = 3) -> Optional[dict]:
        """20.DB のパネル → CHM 表の行（面積一致を優先、次に種別語で近似）"""
        rows_ = self.chm_rows_for_paint(paint)
        if not rows_:
            return None
        cand = [r for r in rows_ if r['area'] == pn['area']]
        if len(cand) == 1:
            return cand[0]
        nm = pn['name']
        kw = {2: ('ボンネット', 'フード'), 5: ('フェンダ',), 6: ('クォータ', 'アウトサイド', 'クオータ'), 4: ('ロワーバック', 'ロワバック', 'リヤーパネル', 'リヤパネル'),
              3: ('ルーフ',) if pn['div'] == 1 else ('サイドシル', 'ロッカ')}
        if pn['type'] == 1:
            if 'F' in nm[:2]:
                keys = ('フロントドア',)
            elif nm[:2].strip() in ('LR', 'RR', 'L', 'R') or 'ｽﾗｲﾄﾞ' in nm:
                keys = ('リヤドア', 'リヤードア', 'スライドドア')
            else:
                keys = ('バックドア', 'テールゲート', 'トランク')
        else:
            keys = kw.get(pn['type'], ())
        pool = cand or rows_
        for r in pool:
            if any(k in r['name'] for k in keys):
                return r
        return cand[0] if cand else None

    # ------------------------------------------------------------ 標準値
    # ------------------------------------------------------------ 係数表による式（CHM が無いときの補完・高機能塗装）
    @staticmethod
    def _r1(x: float) -> float:
        return math.floor(x * 10 + 0.5 + 1e-9) / 10.0

    def form_codes(self) -> tuple[str, str, str, str]:
        """<car>25.DB [9:13] = CarFormCode, FormCode1, FormCode2, FinishCode"""
        p = os.path.join(self.car_dir, f'{self.car}25.DB')
        if not os.path.exists(p):
            return ('', '', '', '')
        c = open(p, 'rb').read()[9:13].decode('latin1')
        return (c[0], c[1], c[2], c[3]) if len(c) == 4 else ('', '', '', '')

    def _kei1(self, form: str, div: int, typ: int, disp: str, pa: int, area: int, paint: str = '1') -> Optional[tuple[int, int]]:
        for r in self._com_rows('T_KEI_1.DB'):
            if len(r) >= 10 and r[0] == form and r[1] == str(div) and r[2] == str(typ) and r[3] == disp and r[4] == str(pa) and r[5] == paint \
                    and r[6].isdigit() and r[7].isdigit() and int(r[6]) <= area <= int(r[7]):
                return int(r[8]), int(r[9])
        return None

    def _c12(self, form: str, div: int) -> Optional[tuple[int, int]]:
        for r in self._com_rows('T_KEI_2.DB'):
            if len(r) >= 4 and r[0] == form and r[1] == str(div):
                return int(r[2]), int(r[3])
        return None

    def _hn_prep(self, form: str, f1: str, f2: str, pcode: int, finish: str) -> float:
        """H_N_SSZ: 溶接パネル（PanelCode≠0）取替時の関連部下処理加算"""
        if not pcode:
            return 0.0
        for r in self._com_rows('H_N_SSZ.DB'):
            if len(r) >= 7 and r[0] == form and r[3].isdigit() and int(r[3]) == pcode and (r[6] == '' or r[6] == finish):
                if (r[1] == f1 and r[2] == f2) or (r[1] == f1 and r[2] == '9') or (r[1] == '9'):
                    return int(r[4]) / 100.0
        return 0.0

    def _hju(self, div: int, typ: int, idx: int) -> float:
        """HJU_SS: 修正塗装の下処理時間（PanelDivision, PanelTypeDivision 2/3/4 以外は 9, index=ceil(S'/3)、最大行で頭打ち）"""
        tbl = {}
        for r in self._com_rows('HJU_SS.DB'):
            if len(r) >= 4 and r[0].isdigit() and r[1].isdigit() and r[2].isdigit():
                tbl[(int(r[0]), int(r[1]), int(r[2]))] = int(r[3]) / 100.0
        t = typ if any(k[0] == div and k[1] == typ for k in tbl) else 9
        mx = max([k[2] for k in tbl if k[0] == div and k[1] == t] or [0])
        if not mx:
            return 0.0
        return tbl[(div, t, max(1, min(idx, mx)))]

    def formula_times(self, pn: dict, paint: str = '1') -> Optional[dict]:
        """係数表だけから CHM 相当の行を作る: {new_multi, new_single, r11, r12, r13}（CHM が無い車種・パネル用）"""
        form, f1, f2, fin = self.form_codes()
        if not form:
            return None
        div, typ, S, pcode = int(pn['div']), int(pn['type']), int(pn['area']), int(pn.get('pcode') or 0)
        ab = self._kei1(form, div, typ, 'K', 9, S, paint); c = self._c12(form, div)
        if not ab or ab == (0, 0) or not c:
            return None
        A, B = ab; C1, C2 = c; add = self._hn_prep(form, f1, f2, pcode, fin)
        out = {'name': pn['name'].strip(), 'area': S, 'hf': None,
               'new_multi': self._r1((B + A * S / 1000) * C2 / 100000 + add), 'new_single': self._r1((B + A * S / 1000) * C1 / 100000 + add)}
        for n, key in ((1, 'r11'), (2, 'r12'), (3, 'r13')):
            Sn = S if n == 1 else int(math.ceil(S / n - 1e-9))
            abn = (self._kei1(form, div, typ, 'S', n, Sn, paint) or self._kei1(form, div, 9, 'S', n, Sn, paint)
                   or self._kei1(form, div, typ, 'S', 9, Sn, paint) or self._kei1(form, div, 9, 'S', 9, Sn, paint))  # 汎用行（PaintingArea 9 / Type 9）へフォールバック
            out[key] = None if (not abn or abn == (0, 0)) else self._r1((abn[1] + abn[0] * Sn / 1000) * C2 / 100000 + self._hju(div, typ, int(math.ceil(Sn / 3 - 1e-9))))
        return out

    def hf_time(self, area: int, hf: int, pn: Optional[dict] = None, paint: int = 3) -> float:
        """高機能塗装（フッ素 1 / 耐スリ傷 2）のパネル別加算。COM/F_S.DB の式（実 NEO 4 パネル 31/58/79/92 d㎡ → 0.6/0.8/1.0/1.2 一致）。
        F_S に該当行が無いときは経験式 floor10(0.3 + 0.01×面積)"""
        if not hf:
            return 0.0
        if int(hf) == 3:
            # スクラッチの加算は COM/Scrach.DB（車形・塗料・区分ごとの係数）で決まるが式が未同定
            # （実案件 NEO 369 パネルで 耐スリ傷/フッ素の式・経験式のどれとも 2 割しか合わない。HANDOFF §8）。
            # 分からない値で埋めないので None を返し、呼び出し側は見積書の指数（index）を要求する
            return None
        form = self.form_codes()[0]
        if form and pn:
            kind = {1: 'F', 2: 'T'}.get(int(hf), 'T')
            c = self._c12(form, int(pn['div']))
            rows_fs = [r for r in self._com_rows('F_S.DB') if len(r) >= 7 and r[0] == form and r[1] == str(paint) and r[2] == kind and r[3] == str(pn['div'])]
            for typ_key in (str(pn['type']), '9'):  # 完全一致 → 汎用行（Type 9）
                for r in rows_fs:
                    if r[4] == typ_key and c:
                        return self._r1((int(r[6]) + int(r[5]) * area / 1000) * c[1] / 100000)
        return floor10(0.3 + 0.01 * area)

    @staticmethod
    def prepare_area(area: int, ratio: str) -> int:
        """修正パネルの下処理面積の近似: 塗装面積（面積 × 1/1・1/2・1/3 を切上で整数化）× 0.345 を四捨五入。
        実機で合う 13 例: J87 0600 45 1/1 → 16、0800 28 1/2 → 5、2300 94 1/2 → 16、2601 20 1/1 → 7、4600 22 1/3 → 3、4800 88 1/2 → 15、5800 285 1/3 → 33、
        W66 0600 81 1/2 → 14、0800 28 1/2 → 5、2300 88 1/1 → 30・1/2 → 15、4600 40 1/2 → 7、4802 68 1/2 → 12、W82 1000 47 1/3 → 6、ZYX11 3500 79 1/1 → 27、U52 1000 37 1/2 → 7。
        合わないのは **W66 のルーフ 287 だけ**（1/2 → 実機 48 / 式 50、1/3 → 実機 32 / 式 33。2026-09-12 w66b_real で再確認）。
        J87 のルーフ 285 1/3 → 33 は式どおりなので「大面積」ではなく車種（車形 6 と 7）か COM/T_KEI_4（2 行 × 6 定数・用途未同定）の
        係数差と推定。切上(面積×割合÷3) に置き換えると W66 ルーフは合うが J87 45 1/1（→16）と 285 1/3（→33）が外れるので採らない。
        金額に影響しない表示列（ロードマップ 1-10、判断規則 10-18）"""
        r = {'1/1': 1.0, '1/2': 0.5, '1/3': 1.0 / 3}.get(ratio)
        if not r:
            return -1
        part = int(math.ceil(area * r - 1e-9))
        return int(math.floor(part * 0.345 + 0.5))

    def standard_times(self, code: str, hf: int, n_panels: int, paint: int = 3) -> Optional[dict]:
        """paint: PaintingPlan.Paint（1 速乾 / 3 ２Ｋ / 4 水性）。係数表（T_KEI_1 / F_S）の塗料列に渡す"""
        pn = self.panel(code)
        if not pn:
            return None
        row = self.chm_row_for(pn, paint)
        if row is None or row.get('area') != pn['area']:  # CHM に無い／面積が合わないパネルは係数表の式で補う。補えなければ None（呼び出し側が index を要求）
            row = self.formula_times(pn, str(paint))
        add = self.hf_time(pn['area'], hf, pn, paint)
        single = n_panels <= 1
        res = {'panel': pn, 'chm': row, 'hf': add}
        if add is None:  # 高機能加算が求まらない（スクラッチ）: 標準指数は無しとして扱う（呼び出し側が index を要求し、手入力 # で書く）
            res.update({'new': None, 's1': None, 's2': None, 's3': None})
            return res
        if row:
            new = row['new_single'] if single and row['new_single'] is not None else row['new_multi']
            r11 = row['r11']
            r12 = row['r12'] if row['r12'] is not None else r11
            r13 = row['r13'] if row['r13'] is not None else r12
            bump = 0.4 if single else 0.0  # 修正パネルを単体塗装する場合は 0.4 加算（CHM 注2）
            def _v(base: Optional[float], extra: float) -> Optional[float]:  # CHM の '-'（None）は None のまま（呼び出し側が index を要求）
                return None if base is None else round(base + extra + add, 1)
            res.update({'new': _v(new, 0.0), 's1': _v(r11, bump), 's2': _v(r12, bump), 's3': _v(r13, bump)})
        return res

    def _com_rows(self, name: str) -> list[list[str]]:
        p = os.path.join(self.com_dir, name)
        if not os.path.exists(p):
            p = os.path.join(HERE, 'reference', name)  # 展開キャッシュに無い表は同梱の予備
        if not os.path.exists(p):
            return []
        return [[x.strip() for x in l.split(',')] for l in _xor_lines(p)]

    def base_time(self, form: str, paint: int, coat: int, hf: int, n_panels: int) -> Optional[float]:
        kind = HF_KIND.get(hf, 'B')
        for r in self._com_rows('T_KEI_3.DB'):
            if len(r) >= 9 and r[0] == str(form) and r[1] == str(paint) and r[2] == str(coat) and r[3] == kind:
                return int(r[4 + min(max(n_panels, 1), 5) - 1]) / 100.0
        return None

    def bumper_base_time(self, form: str, coat: int) -> Optional[float]:
        """バンパ加算基礎（COM/BAN.DB: 車形, 塗膜クラス 1-4, 値×1/100, 予備, 予備）。
        外板パネルが 1 枚も無くバンパだけ塗装するときに PaintingPlan.BumperBaseTime に入る
        （実機 2026-09-12 W66 車形 6・2コートパール: 0.5、cogni_W66w。パネルがあるときは -1 = 加算基礎数値の側で見る）"""
        for r in self._com_rows('BAN.DB'):
            if len(r) >= 3 and r[0] == str(form) and r[1] == str(coat):
                try:
                    return int(r[2]) / 100.0
                except ValueError:
                    return None
        return None

    def booth_time(self, form: str, paint: int, coat: int, hf: int) -> Optional[float]:
        kind = HF_KIND.get(hf, 'B')
        for r in self._com_rows('BOOTH.DB'):
            if len(r) >= 5 and r[0] == str(form) and r[1] == str(paint) and r[2] == str(coat) and r[3] == kind:
                return int(r[4]) / 100.0
        return None

    def bumper_time(self, front: bool, coat: int, kind: str = '取替', two_tone: bool = False, paint: int = 3) -> Optional[float]:
        """<car>23.DB（溶剤）/ <car>93.DB（水性 paint=4、同形）: F/R, 塗膜クラス → [取替一色, 取替二色, 外傷大一色, 外傷大二色, 外傷小一色, 外傷小二色, 変形一色, 変形二色]"""
        p = os.path.join(self.car_dir, f'{self.car}{93 if paint == 4 else 23}.DB')
        if not os.path.exists(p):
            return None
        cls = COAT_CLASS_BUMPER.get(coat, 2)
        base_idx = {'取替': 0, '新品': 0, '外傷大': 2, '外傷修正大': 2, '外傷小': 4, '外傷修正小': 4, '変形': 6, '変形修正': 6}.get(kind)
        if base_idx is None:
            return None  # '外傷修正'（小/大の区別なし）は 23.DB 車種では選べない（FBANPA 車種専用）
        idx = base_idx + (1 if two_tone else 0)
        for l in _xor_lines(p):
            f = [x.strip() for x in l.split(',')]
            if len(f) >= 16 and f[6] == ('F' if front else 'R') and f[7] == str(cls):
                v = int(f[8 + idx] or 0)
                return v / 100.0 if v > 0 else None  # 0 埋めは未収録扱い
        return None


    def has_bumper_table(self, paint: int = 3) -> bool:
        """車種別のバンパ表（<car>23.DB、水性は 93.DB）があるか。無い車種は COM/FBANPA.DB（bumper_time_generic）"""
        return os.path.exists(os.path.join(self.car_dir, f'{self.car}{93 if paint == 4 else 23}.DB'))

    def bumper_time_generic(self, coat: int, kind: str = '新品', form_code: int = 0, col_code: int = 0) -> Optional[float]:
        """<car>23.DB の無い車種（586 車種 = 01.DB を持つ 1,299 車種中、うち 20.DB あり 489。例: ミニキャブ・パートナー・アクティ・ダイナ等）の樹脂バンパ塗装 = COM/FBANPA.DB（コグニ実機 J69 2026-09-06 夕）。
        行 `車形 0, K/S, 塗膜クラス 1-4, 副区分(S: 1=変形修正 2=外傷修正), v1..v6`。v4/v5/v6 = 大型 の 一色/黒ライン/二色、標準 = 大型 − 0.1（3コートパール 新品: 大型 2.6/3.0/3.3、標準 2.5/2.9/3.2。
        メタリック 変形 大型黒ライン 4.6 = S,2,1 の v5、外傷 4.0 = S,2,2 の v5、標準一色 3.8/3.2 = v4 − 0.1）。v1..v3 の用途は未同定（小型と推定）。絞模様有りは呼び出し側で +0.4"""
        cls = COAT_CLASS_BUMPER.get(coat, 2)
        ks = 'K' if kind in ('取替', '新品') else 'S'
        sub = '0' if ks == 'K' else ('1' if kind.startswith('変形') else '2')
        form = (self.form_codes()[0] or '0').strip()  # 行の第 1 列 = 車形（25.DB の CarFormCode。0〜9 の 10 車形 × 12 行。車形 6（J69/W66）は 0 と同値、7（軽）は別値）
        rows = self._com_rows('FBANPA.DB')
        cand = [f for f in rows if len(f) >= 10 and f[0].strip() == form] or [f for f in rows if len(f) >= 10 and f[0].strip() == '0']
        for f in cand:
            if f[1].strip() == ks and f[2].strip() == str(cls) and f[3].strip() == sub:
                v = int(f[4 + 3 + min(max(col_code, 0), 2)] or 0)  # v4..v6
                if v <= 0:
                    return None
                t = v / 100.0 - (0.1 if form_code == 1 else 0.0)
                return round(t, 1)
        return None


if __name__ == '__main__':
    import sys
    pi = PaintIndex(r'C:\Addata', sys.argv[1] if len(sys.argv) > 1 else 'W69')
    for r in pi.panels:
        print(r)
    for r in pi.chm_rows:
        print(r)
    for c in ('1000', '3100', '3500', '5000'):
        print(c, {k: v for k, v in (pi.standard_times(c, 2, 4) or {}).items() if k not in ('panel', 'chm')})
    print('base', pi.base_time('6', 3, 3, 2, 4), 'booth', pi.booth_time('6', 3, 3, 2), 'bumper', pi.bumper_time(True, 3))
