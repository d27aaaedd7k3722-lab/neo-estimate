# -*- coding: utf-8 -*-
"""inspect_estimate.py — estimate.json（下書き）を ADDATA と突き合わせて、NEO 生成前に人（Claude）が判断すべき点を一覧にする。

使い方（files ディレクトリで）:
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/inspect_estimate.py <estimate.json> [--json <out.json>]

出力（標準出力・日本語）:
  1. 車両特定の結果と候補（confidence / CarCode / 年式 / ボディ / グレード / FVA / KA81 期間）
  2. 明細 1 行ごとの照合結果: 選ばれた ref・12.DB 名称・部位ブロック・11.DB の品番一覧・理由。
     名称近似で決まった行、他ブロックの ref に飛んだ行、左側 ref に数量 2 以上が付いた行に「要確認」を付ける
  3. 取替/脱着行の標準指数（cogni_standard）と見積指数の比較（一致 = 標準行、不一致 = '#' 手入力になる）
  4. 板金行: 損傷面積と指数から BANKIN.DB を逆引きしてランク A/B/C を提案
  5. 装備（EVA）の提案: 見積の品番が 11.DB / 13.DB のどの条件行に一致するか（flags の [5:7] = 装備レター）
  6. 塗装: パネル別の標準指数（塗り数値）・加算基礎・バンパの標準と見積値の比較、材料代の丸め方（コグニ一括計算 vs 行ごと）
  7. 合計の検算（items / paint / expenses から再計算 → totals と比較）

生成器（estimate_to_neo.py）の関数だけを呼ぶ。NEO も estimate.json も書かない（書くのは --json の報告だけ）。
ただし塗装の標準指数を引くとき、生成器と同じく CHM の展開キャッシュ（%LOCALAPPDATA%\claude_neo_pipeline\chm）が無ければ作られる。
区分の写像（DISPOSAL 表）・FVA の正規化（末尾 1 文字）・工賃/材料代/消費税の丸め・present_rows（連動加算）は生成器 build と同じ規則にしてある。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from typing import Optional

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import skill_env  # noqa: E402
skill_env.apply()  # ADDATA / NEO_check / 雛形 の場所を環境変数に（PC ごとの設定ファイルと自動検出）
FILES = skill_env.FILES  # files/
flag = skill_env.flag  # 人が書いた真偽値欄の厳密な読み取り（文字列 "false" を真にしない）
sys.path.insert(0, os.path.join(FILES, 'claude_neo_pipeline'))

from estimate_to_neo import (AddataParts, NeoBuilder, BUMPER_DISPOSAL, BUMPER_DRAFT_ADD, COAT_CODES, DISPOSAL, HF_CODE,  # noqa: E402
                             _xor_lines_ref, bankin_time, default_material_rate, material_default, r10, r10_even)
from paint_index import PaintIndex  # noqa: E402



def _dcode(method: str, price, wage) -> int:
    """生成器 build_rows と同じ区分写像（DISPOSAL 表 + NFKC 正規化・鈑→板・空白除去。未知語は部品代あり→取替、工賃あり→脱着）"""
    m = (method or '').strip()
    m2 = _nfkc(m).replace('鈑', '板').replace(' ', '')
    default = 0 if (price or 0) > 0 else (1 if (wage or 0) > 0 else 0)
    return DISPOSAL.get(m, DISPOSAL.get(m2, default))


def _nfkc(s: str) -> str:
    import unicodedata
    return unicodedata.normalize('NFKC', str(s or ''))


def _coat_code(v) -> int:
    if isinstance(v, int):
        return v
    s = _nfkc(v).replace(' ', '')
    for k, c in COAT_CODES.items():
        if _nfkc(k).replace(' ', '') == s:
            return c
    return int(s) if s.isdigit() else 0


def _hf_code(v) -> int:
    if isinstance(v, int):
        return v
    import unicodedata as _u
    m = {_u.normalize('NFKC', k): c for k, c in HF_CODE.items()}
    k = _u.normalize('NFKC', str(v or 'しない')).strip()
    if k not in m:  # 生成器と同じく、知らない高機能塗装を「しない」として検査しない（Codex 指摘）
        raise ValueError(f"paint.hf は {sorted(set(HF_CODE) - {'ｽｸﾗｯﾁ'})} のいずれか（{v!r}）")
    return m[k]


def _given(v):
    """生成器は塗装の index を `float(x or 0)` で読み、0 を未指定扱いにする。None/0/''/'0' → None、それ以外は float"""
    try:
        f = float(v) if v not in (None, '') else 0.0
    except (TypeError, ValueError):
        return None
    return f if f else None


def _paint_code(v) -> int:
    s = _nfkc(v).replace(' ', '').upper()
    if s in ('1', '速乾', '速乾ウレタン'):
        return 1
    if s in ('4', '水性'):
        return 4
    return 3  # 2K


def main(path: str, out_json: str = '') -> int:
    """工賃丸め単位（estimate.wage_round）を生成器と同じ ContextVar に設定して検査し、終了時に戻す"""
    import estimate_to_neo as _e
    est0 = json.load(open(path, encoding='utf-8-sig'))
    token = _e.set_wage_unit(est0.get('wage_round') or 10)
    try:
        return _inspect(path, out_json)
    finally:
        _e.reset_wage_unit(token)


def _inspect(path: str, out_json: str = '') -> int:
    est = json.load(open(path, encoding='utf-8-sig'))
    skill_env.normalise_flags(est.get('items'), where='items[]')  # 真偽値欄は入口で 1 回だけ正規化（Codex 指摘）
    nb = NeoBuilder()
    rep: dict = {'vehicle': None, 'items': [], 'paint': {}, 'eva': {}, 'totals': {}}
    warn: list[str] = []

    # ---- 1. 車両 --------------------------------------------------------------------------------
    v = est['vehicle']
    if flag(v.get('generic'), 'vehicle.generic'):
        veh = nb.generic_vehicle(v)
    else:
        veh = nb.resolve_vehicle(v, est.get('hints'))
    car = veh['neo_car']
    print('=' * 100)
    print('1. 車両特定:', veh.get('confidence'), '|', car.get('CarNameByUser'), '/', car.get('CarCode'),
          'Year', car.get('YearCode'), 'Body', car.get('BodyCode'), 'Grade', car.get('GradeCode'), car.get('grade_name', ''),
          'FVA', car.get('FVACode'), '色', car.get('ColorCode'), car.get('ColorName', ''), '車形', car.get('CarFormCode'))
    for e in veh.get('evidence', []):
        print('   根拠:', e)
    for c in veh.get('candidates', [])[:5]:
        print('   候補:', c.get('car_code'), c.get('year_code'), c.get('body_code'), c.get('grade_code'), c.get('grade_name'), c.get('fva_code'),
              'score', c.get('score'), c.get('reasons'))
    if veh.get('confidence') not in ('confirmed', 'high', 'generic'):
        warn.append(f"車両特定の確信度が {veh.get('confidence')}。車検証の型式指定/類別/初度登録/車台番号を見直すか、vehicle.hints を足す")
    rep['vehicle'] = {k: car.get(k) for k in ('CarCode', 'YearCode', 'BodyCode', 'GradeCode', 'FVACode', 'ColorCode', 'CarFormCode')}
    rep['vehicle']['confidence'] = veh.get('confidence')
    if flag(v.get('generic'), 'vehicle.generic'):
        print('   汎用車種のため以下の ADDATA 照合はスキップ（items は manual=true で名称をそのまま使う）')
        _totals(est, warn, rep, str(car.get('CarFormCode', '') or ''), int(est.get('labor_rate') or 0))
        _finish(warn, rep, out_json)
        return 0

    if not car.get('CarCode'):
        warn.append('車両が特定できない（CarCode 空）。車検証の型式・車台番号・型式指定/類別を見直すか、コグニ非収録車なら vehicle.generic=true にする')
        _totals(est, warn, rep, '', int(est.get('labor_rate') or 0))
        _finish(warn, rep, out_json)
        return 0
    car_code = car['CarCode']
    grade, year, body = car.get('GradeCode', ''), str(car.get('YearCode', '')), str(car.get('BodyCode', '') or '')
    fva = (car.get('FVACode', '') or '')[-1:]  # 4WD は 'ZA' のような 2 文字。11/83.DB の条件判定は末尾 1 文字（生成器 _row_ctx と同じ）
    eva_hint = set(str(x) for x in ((est.get('hints') or {}).get('eva_codes') or []))
    if car.get('four_wd'):
        eva_hint.add('Z')
    reg_ym = re.sub(r'\D', '', str(car.get('ps_CarRegDate', '')))[:6]
    parts = AddataParts(nb.engine, car_code)
    parts.vehicle_body = body
    raw11 = parts._load_11_raw()
    try:
        raw83 = parts._load_83_raw()
    except Exception:
        raw83 = {}
    labor = int(est.get('labor_rate') or 0)
    if not labor:  # 生成器はレート未指定のとき明細から工賃単価を推定する。ここでも wage/index が揃う行から推定（10 円丸め）
        rates = [r10(float(it['wage']) / float(it['index'])) for it in est['items'] if it.get('wage') and it.get('index') and float(it['index']) > 0]
        if rates:
            labor = max(set(rates), key=rates.count)
            print(f'   labor_rate 未指定 → 明細から推定 {labor}（生成器も同様に推定する。estimate.json に明記するのが望ましい）')
        else:
            warn.append('labor_rate 未指定で明細から推定もできない: 指数の標準比較は run_case の結果で確認する')

    # ---- 2. 明細 --------------------------------------------------------------------------------
    print('=' * 100)
    print('2. 明細の照合（ref / 12.DB 名称 / ブロック / 理由）。★ = 要確認')
    ctx_block = ''
    eva_votes: dict[str, list[str]] = {}
    # 先に全行の (ref, DisposalCode) を決める。cogni_standard の present_rows（連動加算・骨格の集約先）に使う = 生成器 build と同じ
    pre_refs: list[tuple[int, int]] = []
    _blk = ''
    for it in est['items']:
        if it.get('manual') or it.get('reserve'):
            continue
        _q = max(1, int(it.get('qty') or 1))
        _pr = it.get('parts_price') if it.get('parts_price') is not None else it.get('price')
        _ref, _ = parts.find_ref(it.get('code', ''), it.get('parts_no', '') or '', it.get('name', ''), context_block=_blk,
                                 price=(int(_pr) // _q if _pr else None), qty=(_q if _q > 1 else None), year=year)
        if _ref is not None:
            _blk = parts.block_of(_ref) or _blk
            pre_refs.append((int(_ref), _dcode(str(it.get('method', '取替')), _pr, it.get('wage'))))
    for i, it in enumerate(est['items'], 1):
        name = it.get('name', '')
        pn_in = it.get('parts_no', '') or ''
        qty = max(1, int(it.get('qty') or 1))
        price = it.get('parts_price') if it.get('parts_price') is not None else it.get('price')  # 生成器と同じ優先順（parts_price → price）
        method = str(it.get('method', '取替'))
        dcode = _dcode(method, price, it.get('wage'))
        row = {'no': i, 'name': name, 'parts_no': pn_in, 'method': method, 'dcode': dcode, 'qty': qty}
        if it.get('manual'):
            row['_line'] = f"{i:>3} 手入力  {name} {pn_in} ×{qty} ¥{price}"
            row['ref'] = None
            rep['items'].append(row)
            continue
        ref, why = parts.find_ref(it.get('code', ''), pn_in, name, context_block=ctx_block,
                                 price=(int(price) // qty if price else None), qty=(qty if qty > 1 else None), year=year)
        flags = []
        if ref is None:
            row['_line'] = f"{i:>3}   --- {name:<26} {pn_in:<16} ×{qty:<2} | {why}"
            flags.append('★未照合（manual: true にするか、名称/品番を見直す）')
        else:
            ctx_block = parts.block_of(ref) or ctx_block
            n20 = sorted(parts.name20_by_ref.get(ref, [])) if hasattr(parts, 'name20_by_ref') else []
            blk = parts.block_of(ref) or ''
            code_in = str(it.get('code') or '').strip()
            if code_in and code_in.isdigit() and int(code_in) != ref:
                flags.append(f'★指定コード {code_in} と選ばれた ref {ref} が違う')
            if '名称近似' in why:
                flags.append('★名称近似で決定。同ブロックの 12.DB 名称一覧と照らして正しい ref か確認')
            if qty >= 2 and n20 and all(str(s)[:1] == 'L' for s in n20) and not re.match(r'^\s*(左|LH|L/H|L[.\s/]|L[FR](?![a-z]))', unicodedata.normalize('NFKC', name)):  # 見積名に左指定があれば左 n 個で正しい
                flags.append('★左側 ref に数量 2 以上。右側 ref と 1 個ずつに分けることを検討（コグニは左右別行）')
            pns = [(r['pn'], r.get('disp'), r.get('flags', '').rstrip(), r.get('body')) for r in raw11.get(ref, []) if r.get('disp') == 'K'][:8]
            row.update({'ref': ref, 'why': why, 'name20': n20, 'block': blk, 'pn11': pns})
            row['_line'] = f"{i:>3} {ref:>5} {blk:<4} {name:<26} {pn_in:<16} ×{qty:<2} | {'/'.join(n20)[:40]:<40} | {why}"
            # 装備の投票（見積の品番が 11.DB / 13.DB の条件付き行に一致するとき、その flags[5:7] を提案）
            pn_norm = parts.norm_pn(pn_in)
            if pn_norm:
                for r in raw11.get(ref, []):
                    if r.get('disp') == 'K' and parts.norm_pn(r['pn']) == pn_norm:
                        for ch in r.get('flags', '')[5:7]:
                            if ch.strip():
                                eva_votes.setdefault(ch, []).append(f"{ref} {pn_in}(11.DB)")
                for r in raw83.get(ref, []):
                    if parts.norm_pn(r.get('pn', '')) == pn_norm:
                        for ch in r.get('flags', '')[5:7]:
                            if ch.strip():
                                eva_votes.setdefault(ch, []).append(f"{ref} {pn_in}(13/83.DB {r.get('color', '')})")
            # 標準指数の比較
            if dcode in (0, 1, 2, 3) and (it.get('wage') or it.get('index')):
                try:
                    std = parts.cogni_standard(ref, dcode, grade, fva, eva_hint, year, pre_refs, body=body)
                except Exception as e:  # noqa: BLE001
                    std = None
                    flags.append(f'標準指数の取得で例外: {e}')
                t_in = _given(it.get('index'))
                if t_in is None and it.get('wage') and labor:
                    t_in = round(float(it['wage']) / labor, 2)
                if t_in is None:
                    flags.append('指数もレートも無く標準比較できない（run_case の結果で確認）')
                elif std and std.get('time', 0) > 0:
                    row['std_time'] = std['time']
                    if t_in is not None and abs(float(t_in) - float(std['time'])) < 0.005:
                        flags.append(f"標準一致 {std['time']}")
                    else:
                        flags.append(f"見積 {t_in} ≠ 標準 {std['time']} → '#' 手入力になる")
                elif t_in:
                    flags.append(f"標準指数なし（見積 {t_in} は '#' 手入力）")
            # 板金ランクの逆引き
            if dcode == 6:
                bk = it.get('bankin') or {}
                area = bk.get('area')
                t_in = it.get('index') or (round(float(it['wage']) / labor, 2) if it.get('wage') and labor else None)
                if area and t_in is not None:
                    hits = [rk for rk in 'ABC' if bankin_time(int(area), rk) is not None and abs(bankin_time(int(area), rk) - float(t_in)) < 0.005]
                    yes = {'A': [1, 1, 1], 'B': [1, 0, 0], 'C': [0, 0, 0]}
                    if hits:
                        flags.append(f"板金 {area}d㎡ 指数 {t_in} → ランク {hits[0]}（bankin.yes = {yes[hits[0]]}）")
                        row['bankin_rank'] = hits[0]
                    else:
                        flags.append(f"板金 {area}d㎡ 指数 {t_in} は BANKIN.DB のどのランクとも一致しない（A {bankin_time(int(area), 'A')} / B {bankin_time(int(area), 'B')} / C {bankin_time(int(area), 'C')}）→ '#' 手入力")
                else:
                    flags.append('板金行: bankin.area（損傷面積 d㎡）と index を入れるとランクを逆引きできる')
            # 色別部品
            if dcode == 0 and pn_in:
                try:
                    cp = parts.colored_part_by_pn(ref, pn_norm, grade, fva, eva_hint)
                    if cp:
                        flags.append(f"13/83.DB 一致 {cp.get('pn')} ¥{cp.get('price')} 色 {cp.get('color') or '-'} 期間 {cp.get('from', '')}〜{cp.get('to', '') or '現行'} {cp.get('note', '')}".rstrip())
                        if price and cp.get('price') and int(price) // qty != int(cp['price']):
                            flags.append(f"★価格差: 見積 {int(price) // qty} / ADDATA {cp['price']}")
                except Exception as _ecp:  # noqa: BLE001  13/83.DB が無い車種はある。黙って飛ばすと価格差の警告が消える
                    flags.append(f'★色別部品の照合ができなかった（{type(_ecp).__name__}: {_ecp}）。品番・価格は目で確かめる')
        row['flags'] = flags
        rep['items'].append(row)

    # 左側 ref の数量 2 以上: 同じ品番の右側行が別にあれば左右とも 2 個ずつの正当な行なので警告を外す
    pn_right = {parts.norm_pn(r.get('parts_no') or '') for r in rep['items']
                if (r.get('parts_no') or '').strip() and r.get('name20') and any(str(s)[:1] == 'R' for s in r['name20'])}  # 20 文字名の 1 文字目だけが左右（2 文字目 F/R は前後）
    suppressed = set()
    for r in rep['items']:
        pn_ = parts.norm_pn(r.get('parts_no') or '') if (r.get('parts_no') or '').strip() else ''
        if pn_ and pn_ in pn_right and any(f.startswith('★左側 ref に数量') for f in r.get('flags', [])):
            r['flags'] = [f for f in r['flags'] if not f.startswith('★左側 ref に数量')]
            suppressed.add(r['no'])
    # 明細の表示と要確認は抑止後の flags から（同じ明細の他の ★ を消さず、抑止した警告は本文にも出さない）
    for r in rep['items']:
        print(r.pop('_line', f"{r['no']:>3} {r['name']}"))
        for f in r.get('flags', []):
            print(f"        - {f}")
        stars = [f for f in r.get('flags', []) if f.startswith('★')]
        if stars:
            warn.append(f"明細 {r['no']} {r['name']}: " + ' / '.join(stars))

    # ---- 5. 装備 --------------------------------------------------------------------------------
    print('=' * 100)
    print('5. 装備（EVA）の提案。hints.eva_codes に入れる候補（10.DB の名称と照らして採否を決める）')
    opts = nb.resolver.options(car_code) if hasattr(nb, 'resolver') else {}
    for ch, ev in sorted(eva_votes.items()):
        print(f"   {ch} {opts.get(ch, '(10.DB に無い)')}: {len(ev)} 件 ← {', '.join(ev[:4])}")
    if eva_hint:
        print('   現在の hints.eva_codes:', sorted(eva_hint))
    missing = [ch for ch in eva_votes if ch not in eva_hint and ch != fva and ch in opts]  # flags[5:7] は FVA でも満たされる。10.DB に無いレターは装備ではない
    if missing:
        warn.append(f"品番から装備 {missing} が示唆されるが hints.eva_codes に無い（採用すると標準品番・標準指数が変わることがある）")
    rep['eva'] = {ch: ev for ch, ev in eva_votes.items()}

    # ---- 6. 塗装 --------------------------------------------------------------------------------
    p = est.get('paint') or {}
    print('=' * 100)
    if p.get('panels'):
        paint_c, hf_c = _paint_code(p.get('paint', '２Ｋ')), _hf_code(p.get('hf', 'しない'))
        try:  # 生成器 build と同じ: 車両色の 66.DB 先頭桁 → 見積の塗膜名 → 既定 2（メタリック）
            fc = nb.resolver.finish_code(car_code, car.get('ColorCode', '') or '')
        except Exception:
            fc = None
        coat_c = fc if fc in (1, 2, 3, 4) else (_coat_code(p.get('coat', '')) or 2)
        if p.get('coat') and fc in (1, 2, 3, 4) and _coat_code(p.get('coat', '')) not in (0, fc):
            warn.append(f"塗装 ★見積の塗膜 {p.get('coat')}（={_coat_code(p.get('coat', ''))}）と車両色 {car.get('ColorCode')} の 66.DB 塗膜 {fc} が違う（生成器は 66.DB を優先する）")
        pi = PaintIndex(nb.engine.root, car_code, body=car.get('BodyCode', ''))  # 20.DB はボディで面積が違う行を持つ
        from estimate_to_neo import is_manual_panel
        _man = [x for x in p['panels'] if is_manual_panel(x)]
        n_p = len(p['panels']) - len(_man)  # 手入力の塗装行（部品コード無し）は枚数に数えない
        print(f"6. 塗装: 塗料 {paint_c} 塗膜 {coat_c} 高機能 {hf_c} 枚数 {n_p} 車形 {car.get('CarFormCode')}")
        for _pn in p['panels']:            # 先にボディ別の行を引いて、選べなかったパネルを拾う
            if not is_manual_panel(_pn):
                pi.panel(str(_pn.get('code', '')))
        for _u in getattr(pi, 'body_unresolved', []):
            if _u.get('areas'):   # 同じコードに面積の違う行が複数（ADDATA 全車種で 258 組）
                warn.append(f"塗装 ★パネル {_u['code']}: 20.DB に面積の違う行が複数ある"
                            f"（{' / '.join(str(a) for a in _u['areas'])} d㎡）。先頭の {_u['areas'][0]} を採った。"
                            '見積書の dm² と突き合わせる（違えば paint.panels[].index に見積書の指数を書く）')
                continue
            _c = ('/'.join(_u['candidates']) + ' のどれか') if _u['candidates'] else 'このボディ用の行が無い'
            warn.append(f"塗装 ★パネル {_u['code']}: ボディ {_u['body']} 用の行を選べなかった（{_c}）。"
                        '面積＝塗装指数が実機とずれることがあるので、見積書の dm² と突き合わせる')
        for pnl in p['panels']:
            if is_manual_panel(pnl):
                _mk = "'#'" if _given(pnl.get('index')) else "'*'"  # 生成器: 指数あり '#'（Manual 1）/ 工賃だけ '*'（Manual 2）
                print(f"   （手入力の塗装行）{pnl.get('name', '')}: 指数 {pnl.get('index', '-')} 工賃 {pnl.get('wage', '-')}（標準なし・{_mk}。材料代の対象）")
                _w, _t = int(pnl.get('wage') or 0), _given(pnl.get('index'))
                if _w and _t and labor and _w != r10(int(round(float(_t) * 10)) * labor / 10):
                    warn.append(f"塗装 ★手入力の塗装行 {pnl.get('name', '')}: 工賃 {_w} が 指数 {_t} × 単価 {labor} と違う（実案件 NEO の指数付き手入力行は 指数 × 単価。読み取りを確かめる）")
                if _given(pnl.get('index')) is None and not int(pnl.get('wage') or 0):
                    warn.append(f"塗装 ★手入力の塗装行 {pnl.get('name', '')}: 指数も工賃も無い")
                continue
            code = str(pnl.get('code', ''))
            try:
                st = pi.standard_times(code, hf_c, n_p, paint_c)
            except Exception as e:  # noqa: BLE001
                st = None
                print(f"   {code} {pnl.get('name', '')}: 標準指数の取得で例外 {e}")
            m = str(pnl.get('method', ''))
            ratio = str(pnl.get('ratio', '') or '')
            key = 'new' if m in ('取替', '新品', '交換') else {'1/1': 's1', '1/2': 's2', '1/3': 's3'}.get(ratio, 's1')  # 生成器と同じ新品系の判定
            std_t = st.get(key) if st else None
            given = _given(pnl.get('index'))
            if std_t is None:
                msg = f"{code} {pnl.get('name', '')}: 20.DB/CHM に {m} {ratio} の塗り数値が無い → index を必ず指定（'#' 手入力）"
                print('   ' + msg)
                if _given(pnl.get('index')) is None:
                    warn.append('塗装 ★' + msg)
                continue
            if given is None:
                mark = f'index 省略 → 標準 {std_t} を採用'
            elif std_t is not None and abs(float(given) - float(std_t)) < 0.005:
                mark = '一致'
            else:
                mark = "不一致 → Manual/'#'"
            print(f"   {code} {pnl.get('name', ''):<14} {m} {ratio:<4} 面積 {(st.get('panel') or {}).get('area', '?'):>3} | 標準 new {st.get('new')} 1/1 {st.get('s1')} 1/2 {st.get('s2')} 1/3 {st.get('s3')} | 見積 {given} → {mark}")
            t_eff = given if given is not None else std_t  # index 省略時は標準指数が採用される
            if t_eff is not None and labor:
                w_std = r10_even(float(t_eff) * labor)  # 生成器と同じ 10 円四捨五入
                if int(pnl.get('wage') or 0) and int(pnl['wage']) != w_std:
                    msg = f"塗装 {code} {pnl.get('name', '')}: ★工賃 {pnl['wage']} ≠ 指数×レート（10 円四捨五入）{w_std}"
                    print('      ' + msg)
                    warn.append(msg)
        try:
            base_std = pi.base_time(str(car.get('CarFormCode')), paint_c, coat_c, hf_c, n_p) if n_p else None  # 部品コードのあるパネルが 0 枚なら加算基礎の標準は無い
        except Exception:
            base_std = None
        b = p.get('base') or {}
        if _given(b.get('index')) is None:
            base_mark = f'index 省略 → 標準 {base_std} を採用' if base_std is not None else '標準が取れない → index を指定'
        elif base_std is not None and abs(_given(b['index']) - base_std) < 0.005:
            base_mark = '一致'
        else:
            base_mark = '不一致（見積値が手入力で保持される）'
        print(f"   加算基礎数値: 標準 {base_std} / 見積 {b.get('index')} → {base_mark}")
        for key, front in (('bumper_front', True), ('bumper_rear', False)):
            bp = p.get(key)
            if not bp:
                continue
            method_b = _nfkc(bp.get('method') or '新品')
            if method_b not in BUMPER_DISPOSAL:
                msg = f"{key}: method {bp.get('method')!r} は {sorted(set(v[1] for v in BUMPER_DISPOSAL.values()))} のいずれかにする（生成器は例外）"
                print('   ' + msg); warn.append('塗装 ★' + msg)
                continue
            disp_code, disp_name = BUMPER_DISPOSAL[method_b]
            color_b = _nfkc(bp.get('color') or '一色')
            col_code = {'一色': 0, '黒ライン': 1, '二色': 2}.get(color_b, 0)
            form_code = {'大型': 0, '標準': 1}.get(_nfkc(bp.get('form') or ''), 0)
            draft = flag(bp.get('draft'), 'paint.bumper_*.draft') and disp_code != 1
            bt = None; has_tbl_b = False
            try:
                has_tbl = has_tbl_b = bool(pi.has_bumper_table(paint_c))
                bt = pi.bumper_time(front, coat_c, disp_name, two_tone=(col_code == 2), paint=paint_c)
                src_b = '<car>23/93.DB'
                if bt is None and not has_tbl:  # 車種別表が無い車種は COM/FBANPA.DB（外傷修正は小/大の区別なし）
                    bt = pi.bumper_time_generic(coat_c, disp_name, form_code, col_code)
                    src_b = 'COM/FBANPA.DB（汎用表）'
                if bt is not None and draft:
                    bt = round(bt + BUMPER_DRAFT_ADD, 1)
            except Exception as e:  # noqa: BLE001
                src_b = f'取得失敗 {e}'
            given_b = _given(bp.get('index'))
            if has_tbl_b and disp_code == 3:  # 車種別 23/93.DB がある車で 外傷修正（小/大なし）は生成器が例外
                msg = f"{key}: この車種は <car>23/93.DB を持つので 外傷修正 は 外傷修正小 / 外傷修正大 を指定する（生成器は例外）"
                print('   ' + msg); warn.append('塗装 ★' + msg)
                continue
            if bt is None:
                mark_b = '標準なし → index 必須' + ('' if given_b is not None else '（★未指定。生成器は例外）')
                if given_b is None:
                    warn.append(f'塗装 ★{key} {disp_name}: 標準指数が無く index も無い')
            elif given_b is None:
                mark_b = f'index 省略 → 標準 {bt} を採用'
            elif abs(float(given_b) - float(bt)) < 0.005:
                mark_b = '一致'
            else:
                mark_b = '不一致（見積値が手入力で保持される）'
            print(f"   {key}: {disp_name} {color_b}{'（絞模様 +0.4）' if draft else ''} 標準 {bt}（{src_b}） / 見積 {given_b} → {mark_b}")
        # 材料代
        wages = [int(x.get('wage') or 0) for x in p['panels']] + [int((p.get(k) or {}).get('wage') or 0) for k in PAINT_WAGE_KEYS]
        mat_base, other_w = _paint_wages(p)
        wage_total_p = (int(p.get('total') or 0) - other_w) if int(p.get('total') or 0) else mat_base  # 材料率の対象 = 塗装工賃計 − 追加項目（生成器と同じ）
        rate = float(p.get('material_rate') or 0)
        if rate:
            lump = material_default(wage_total_p, rate)  # コグニ既定値（10 円四捨五入）= 生成器と同じ
            per_line = sum(int(w * rate / 100.0 + 0.5) for w in wages) + int((wage_total_p - sum(wages)) * rate / 100.0 + 0.5)  # 工場書式によくある行ごと 1 円四捨五入（内訳が無い残りは 1 行扱い）
            print(f"   材料代: 工賃計 {wage_total_p} × {rate}% = コグニ既定(10円四捨五入) {lump} / 行ごと1円四捨五入 {per_line} / 見積 {p.get('material')}"
                  + ('' if (int(p.get('material') or 0) or None) in (None, lump, per_line) else ' ★どちらとも違う（読み取りか割合を確認）'))
            if (int(p.get('material') or 0) or None) not in (None, lump, per_line) and not flag(p.get('auto_panels'), 'paint.auto_panels'):  # auto_panels は材料代で工場の一式に合わせる設計（10-15）
                warn.append(f"塗装 ★材料代 {p.get('material')} がコグニ既定 {lump} とも行ごと丸め {per_line} とも違う（割合 {rate}% か読み取りを確認）")
            if int(p.get('material') or 0) and int(p['material']) != lump:
                print("      → コグニ既定値と違う。paint.material に見積値を入れると PaintingTotal は '*'（手入力）で保持される")
    else:
        print(f"6. 塗装: パネル明細なし → 一括 {p.get('total')}（塗装費用(工場見積) 1 行）")

    _totals(est, warn, rep, str(car.get('CarFormCode', '') or ''), labor, coat_c if p.get('panels') else None)
    _finish(warn, rep, out_json)
    return 0


PAINT_WAGE_KEYS = ('base', 'bumper_front', 'bumper_rear', 'booth', 'wax', 'sealing', 'door_sash', 'stripe', 'low_cover', 'two_coat_solid', 'two_tone', 'frame')


def _paint_wages(p: dict) -> tuple[int, int]:
    """詳細塗装の (材料率の対象になる工賃計, 追加項目 paint.other の工賃) を明細から合算する（生成器 write_ansvem と同じ範囲。other は塗装計に入るが材料率は掛けない）"""
    base = sum(int(x.get('wage') or 0) for x in (p.get('panels') or [])) + sum(int((p.get(k) or {}).get('wage') or 0) for k in PAINT_WAGE_KEYS)
    other = sum(int((x or {}).get('wage') or 0) for x in (p.get('other') or []))
    return base, other


def _frame_wage(fr: dict, form_x: str, labor: int, warn: list[str]) -> int:
    """内板骨格修正の工賃合計を生成器 write_ansvem と同じ規則で再計算する（COM/N_KIHON.DB の基礎修正、N_KEI.DB の部位×ランク標準、rp2 = r10(指数×レート)）"""
    def rp2(x):
        return r10(int(round((x or 0) * 10)) * labor / 10) if labor and x else 0
    kihon = 3.5
    keis = {}
    try:
        for l in _xor_lines_ref('N_KIHON.DB'):
            f = [x.strip() for x in l.split(',')]
            if f and f[0] == form_x and len(f) >= 3:
                kihon = int(f[2]) / 100.0
        for l in _xor_lines_ref('N_KEI.DB'):
            f = [x.strip() for x in l.split(',')]
            if len(f) >= 7 and f[0] == form_x:
                keis[f[2]] = (int(f[3]) / 100.0, int(f[4]) / 100.0, int(f[5]) / 100.0)
    except Exception as e:  # noqa: BLE001
        warn.append(f'内板骨格: N_KIHON/N_KEI の読取に失敗（{e}）。検算の内骨工賃は明示値だけ')
    total = 0
    if flag(fr.get('basic', True), 'frame.basic', default=True):
        t = float(fr.get('basic_index') or kihon)
        total += int(fr.get('basic_wage') or rp2(t))
    for itf in fr.get('items') or []:
        ri = {'A': 0, 'B': 1, 'C': 2}.get(str(itf.get('rank', 'A')).upper(), 0)
        std = keis.get(str(itf.get('code', '')))
        t = float(itf.get('index') or (std[ri] if std else 0))
        total += int(itf.get('wage') or rp2(t))
    return total


def _totals(est: dict, warn: list[str], rep: dict, form_x: str = '', labor: int = 0, coat_c: Optional[int] = None) -> None:
    items = est.get('items', [])
    parts_sum = sum(int(it.get('parts_price') if it.get('parts_price') is not None else (it.get('price') or 0)) for it in items if not it.get('reserve'))
    wage_sum = sum(int(it.get('wage') or 0) for it in items if not it.get('reserve'))
    p = est.get('paint') or {}
    if int(p.get('total') or 0) or not p.get('panels'):
        paint_w = int(p.get('total') or 0)
    else:  # パネル別塗装で total が 0/省略: 生成器はパネル・加算基礎・バンパ・ブース・付加塗装の工賃を合算する（index だけの項目は工賃を出せないので参考値）
        mat_base_t, other_t = _paint_wages(p)
        paint_w = mat_base_t + other_t  # 付加塗装・内板骨格塗装・追加項目も生成器は塗装工賃計に含める
        if any((x.get('wage') is None and x.get('index') is not None) for x in (p.get('panels') or [])):
            warn.append('塗装 paint.total 省略かつ wage 無しのパネルがある: 検算の塗装工賃は参考値（run_case の検算を正とする）')
    if int(p.get('material') or 0):
        material = int(p['material'])
    elif p.get('panels'):  # 生成器: 詳細塗装で material が 0/未指定なら material_rate → AnUsrTblPnt の既定率 → 26% で自動計算
        cc = coat_c if coat_c in (1, 2, 3, 4) else (_coat_code(p.get('coat', '')) or 2)
        mr = float(p.get('material_rate') or default_material_rate(_paint_code(p.get('paint', '２Ｋ')), cc, _hf_code(p.get('hf', 'しない'))) or 26)
        other_t2 = sum(int((x or {}).get('wage') or 0) for x in (p.get('other') or []))
        material = material_default(paint_w - other_t2, mr)  # 追加項目 paint.other は材料率の対象外
        warn.append(f'塗装 material 未指定: 生成器は {mr}% で {material} 円を自動計算する（見積書に材料代があるなら paint.material に入れる）')
    else:  # 一括塗装で material 無しは 0
        material = 0
    ex = est.get('expenses') or []
    ex_p = sum(int(e.get('amount') or 0) for e in ex if e.get('kind') == 'parts')  # 費用区分の合計は非課税分を含む（生成器 expense_parts = 課税 + 非課税）
    ex_w = sum(int(e.get('amount') or 0) for e in ex if e.get('kind') != 'parts')
    ex_nt = sum(int(e.get('amount') or 0) for e in ex if flag(e.get('taxfree'), 'expenses[].taxfree'))  # 非課税費用（課税小計から外し、税の外で合計に加算 = 生成器 totals['total']）
    fr = est.get('frame') or {}
    frame_w = _frame_wage(fr, form_x, labor, warn) if fr else 0
    disc = est.get('discount') or {}
    disc_sum = int(disc.get('parts') or 0) + int(disc.get('wage') or 0)  # + 割増 / − 値引（生成器 write_ansvem が課税小計に加える）
    sub = parts_sum + wage_sum + paint_w + material + (ex_p + ex_w - ex_nt) + frame_w + disc_sum
    tax = (sub * 10 + 50) // 100  # 消費税 10% 四捨五入（生成器と同じ整数演算）
    t = est.get('totals') or {}
    print('=' * 100)
    print('7. 検算（estimate.json 内の再計算 / totals）')
    rows = [('部品（明細）', parts_sum, t.get('parts')), ('工賃（明細）', wage_sum, t.get('wage')), ('塗装工賃', paint_w, t.get('paint')),
            ('材料代', material, t.get('material')), ('費用部品', ex_p, t.get('expense_parts')), ('費用工賃', ex_w, t.get('expense_wage')),
            ('内板骨格', frame_w, t.get('frame')), ('値引/割増', disc_sum, t.get('discount')), ('課税小計', sub, t.get('taxable')), ('消費税(四捨五入)', tax, t.get('tax')), ('非課税費用', ex_nt, None), ('合計', sub + tax + ex_nt, t.get('total'))]
    for label, calc, given in rows:
        note = ''
        if given is not None and int(given) != calc:
            if label == '部品（明細）' and int(given) == calc + ex_p:
                note = '（見積の部品計は費用部品込み）'
            elif label == '工賃（明細）' and int(given) in _wage_alts(calc, paint_w, material, frame_w, ex_w):
                note = '（見積の工賃計は塗装/材料/内骨/費用工賃のいずれかを含む集計）'
            else:
                note = ' ★不一致'
                warn.append(f"検算 {label}: 再計算 {calc} / totals {given}")
        print(f"   {label:<12} 再計算 {calc:>10,}  totals {'' if given is None else f'{int(given):>10,}'} {note}")
    rep['totals'] = {'parts': parts_sum, 'wage': wage_sum, 'paint': paint_w, 'material': material, 'expense_parts': ex_p, 'expense_wage': ex_w,
                     'frame': frame_w, 'discount': disc_sum, 'taxfree': ex_nt, 'taxable': sub, 'tax': tax, 'total': sub + tax + ex_nt}


def _wage_alts(base: int, paint: int, material: int, frame: int, ex_w: int) -> set[int]:
    """見積書の「工賃計」「作業計」が含みうる組合せ（塗装工賃・材料代・内板骨格・費用工賃の部分集合）"""
    from itertools import combinations
    extras = [paint, material, frame, ex_w]
    out = set()
    for n in range(0, 5):
        for comb in combinations(range(4), n):
            out.add(base + sum(extras[i] for i in comb))
    return out


def _finish(warn: list[str], rep: dict, out_json: str) -> None:
    print('=' * 100)
    if warn:
        print(f"要確認 {len(warn)} 件:")
        for w in warn:
            print('  -', w)
    else:
        print('要確認なし。run_case.py で生成してよい')
    rep['warnings'] = warn
    if out_json:
        json.dump(rep, open(out_json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
        print('JSON:', out_json)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('estimate')
    ap.add_argument('--json', default='')
    a = ap.parse_args()
    sys.exit(main(a.estimate, a.json))
