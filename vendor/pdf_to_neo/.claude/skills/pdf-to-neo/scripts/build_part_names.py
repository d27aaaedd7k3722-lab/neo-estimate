# -*- coding: utf-8 -*-
"""build_part_names.py — ADDATA 全車種の 12.DB を走査し、「部品コード（4 桁）→ 名称の集合」の逆引き辞書を作る。

コグニには部品名の別名テーブルは無い（2026-09-08 調査）。代わりに 12.DB の 4 桁部品コードがメーカー・車種をまたいで同じ部品を指し、
同じコードに車種ごとの呼び名（Fﾌｴﾝﾀﾞﾗｲﾅ / Fｲﾝﾅﾌｴﾝﾀﾞ / Fﾌｴﾝﾀﾞｽﾌﾟﾗﾂｼﾕｼｰﾙﾄﾞ …）が付いている。これを集計すると事実上の同義語辞書になる。
draft_estimate は、品番も部品コードも無い行の名称を、この辞書で「コード候補」に変換してから、その車種の 12.DB にあるコードに絞って照合する。

使い方（files ディレクトリで。1〜2 分。--min-count の既定は 3）:
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/build_part_names.py [--out <json>] [--min-count 2]
出力: reference/part_code_names.json  {"names": {"<正規化名>": {"<code>": 件数, ...}}, "codes": {"<code>": ["名称", ...]}, "meta": {...}}
  - 正規化: 半角カナに統一、小書きカナ→大書き、'-'/'ｰ' 除去、空白除去、左右記号（L/R/左/右）は名称から外す（左右はコードのペアで扱う）
  - 12.DB 行の '〃' は直前行の名称の前半を継承する省略記号なので展開する
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402
skill_env.apply()
FILES = skill_env.FILES
sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))
from addata_vehicle_resolver import find_addata_root, _xor_text  # noqa: E402

OUT_DEFAULT = os.path.join(HERE, '..', 'reference', 'part_code_names.json')
SMALL = str.maketrans('ｧｨｩｪｫｬｭｮｯ', 'ｱｲｳｴｵﾔﾕﾖﾂ')


def norm_name(s: str, strip_side: bool = False) -> str:
    """名称の正規化（照合キー）: NFKC → 半角カナ → 小書き→大書き → 括弧内除去 → 記号・空白・長音除去 → 前後語を F/R に。
    strip_side=True（見積の名称）のときだけ左右記号（RH/LH/右/左/R./L.）を外す。12.DB の名称に左右は無く、先頭の R は「リヤ」なので外さない"""
    t = unicodedata.normalize('NFKC', s or '')
    out = []
    for ch in t:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6 or ch in 'ー・':
            out.append(_FW2HW.get(ch, ch))
        else:
            out.append(ch)
    t = ''.join(out)
    t = t.translate(SMALL)
    t = re.sub(r'[(（][^)）]*[)）]', '', t)  # 括弧内（(4個) (修理) (ﾄｿｳｽﾞﾐ)）は照合キーに含めない
    t = t.replace('$', '')
    if strip_side:
        t = re.sub(r'^\s*(RH|LH|R/H|L/H|右|左|R\.|L\.|R\s|L\s)\s*', '', t)
        t = re.sub(r'\s*(RH|LH|R/H|L/H|右|左|\(右\)|\(左\))\s*$', '', t)  # 末尾の左右（'ﾘﾔｺﾝﾋﾞﾈｰｼｮﾝﾗﾝﾌﾟ LH'）
    t = re.sub(r'[\s\-‐−ｰー･・()（）]', '', t)
    t = re.sub(r'^(ﾌﾛﾝﾄ|FR|Fr)(?=[ｦ-ﾟA-Z])', 'F', t)  # 前後の語は 12.DB の 1 文字表記に寄せる
    t = re.sub(r'^(ﾘﾔ|ﾘｱ|RR|Rr)(?=[ｦ-ﾟA-Z])', 'R', t)
    t = re.sub(r'ﾊﾞﾝﾊﾟｰ', 'ﾊﾞﾝﾊﾟ', t)
    return t.upper()


_FW = 'ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロワヲンヴー・'
_HW = ['ｧ', 'ｱ', 'ｨ', 'ｲ', 'ｩ', 'ｳ', 'ｪ', 'ｴ', 'ｫ', 'ｵ', 'ｶ', 'ｶﾞ', 'ｷ', 'ｷﾞ', 'ｸ', 'ｸﾞ', 'ｹ', 'ｹﾞ', 'ｺ', 'ｺﾞ', 'ｻ', 'ｻﾞ', 'ｼ', 'ｼﾞ', 'ｽ', 'ｽﾞ', 'ｾ', 'ｾﾞ', 'ｿ', 'ｿﾞ',
       'ﾀ', 'ﾀﾞ', 'ﾁ', 'ﾁﾞ', 'ｯ', 'ﾂ', 'ﾂﾞ', 'ﾃ', 'ﾃﾞ', 'ﾄ', 'ﾄﾞ', 'ﾅ', 'ﾆ', 'ﾇ', 'ﾈ', 'ﾉ', 'ﾊ', 'ﾊﾞ', 'ﾊﾟ', 'ﾋ', 'ﾋﾞ', 'ﾋﾟ', 'ﾌ', 'ﾌﾞ', 'ﾌﾟ', 'ﾍ', 'ﾍﾞ', 'ﾍﾟ', 'ﾎ', 'ﾎﾞ', 'ﾎﾟ',
       'ﾏ', 'ﾐ', 'ﾑ', 'ﾒ', 'ﾓ', 'ｬ', 'ﾔ', 'ｭ', 'ﾕ', 'ｮ', 'ﾖ', 'ﾗ', 'ﾘ', 'ﾙ', 'ﾚ', 'ﾛ', 'ﾜ', 'ｦ', 'ﾝ', 'ｳﾞ', 'ｰ', '･']
_FW2HW = dict(zip(_FW, _HW))


def parse_12(path: str):
    """12.DB の行 → (名称, 左コード, 右コード)。名称欄は 13 桁目から、直後の「4 桁数字」を境界にする（幅がファイルで 30/31 桁とずれるため）"""
    try:
        text = _xor_text(path)
    except Exception:  # noqa: BLE001
        return
    prev = ''
    for line in text.splitlines():
        if not line.startswith('12') or len(line) < 50:
            continue
        body = line[11:]
        m = re.search(r'(\d{4})(\d{4}|\s{4})', body[20:])  # 名称の後の 部品コード 4 桁（+ 右コード 4 桁 or 空白）
        if not m:
            continue
        name = body[2:20 + m.start()].strip()  # [11:13] は行種別（AA/01/81…）
        if not name or name[:2] in ('81', '82', '01', '02', '03', '04', '05'):
            continue  # 行種別コードを名称として拾わない（pass だと素通りしていた）
        left, right = m.group(1), m.group(2).strip()
        if name.startswith('〃'):
            name = prev[:max(0, len(prev) - len(name.lstrip('〃').strip()))].rstrip() + name.lstrip('〃').strip() if prev else name.lstrip('〃').strip()
        if not re.search(r'[ｦ-ﾟA-Za-z]', name):
            continue
        prev = name
        yield name, left, right


def build(root: str, min_count: int) -> dict:
    names: dict[str, dict[str, int]] = {}
    codes: dict[str, dict[str, int]] = {}
    files = glob.glob(os.path.join(root, '?', '*', '*12.DB'))
    t0 = time.time()
    for i, p in enumerate(files):
        car = os.path.basename(p)[:3]
        for name, left, right in parse_12(p) or ():
            key = norm_name(name)
            if len(key) < 2:
                continue
            for code in (left, right):
                if not code:
                    continue
                names.setdefault(key, {}).setdefault(code, 0)
                names[key][code] += 1
                nm = name.replace('$', '').strip()
                codes.setdefault(code, {}).setdefault(nm, 0)
                codes[code][nm] += 1
    # 件数の少ない組は落とす（誤読・特殊車）
    names = {k: {c: n for c, n in v.items() if n >= min_count} for k, v in names.items()}
    names = {k: v for k, v in names.items() if v}
    codes_out = {c: [n for n, _ in sorted(v.items(), key=lambda kv: -kv[1])[:6]] for c, v in codes.items()}
    return {'names': names, 'codes': codes_out, 'meta': {'files': len(files), 'built': time.strftime('%Y-%m-%d'), 'seconds': round(time.time() - t0, 1), 'min_count': min_count}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=OUT_DEFAULT)
    ap.add_argument('--min-count', type=int, default=3)
    a = ap.parse_args()
    root = os.environ.get('ADDATA_ROOT') or find_addata_root()
    d = build(root, a.min_count)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as fh:
        json.dump(d, fh, ensure_ascii=False, separators=(',', ':'))
    print(f"{a.out}: 名称 {len(d['names'])} / コード {len(d['codes'])} / 12.DB {d['meta']['files']} 本 / {d['meta']['seconds']} 秒")
    return 0


if __name__ == '__main__':
    sys.exit(main())
