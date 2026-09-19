"""
run_case.py — 構造化した見積 JSON（Claude が PDF を読み取って起こしたもの）から NEO を生成する

使い方:
    python claude_neo_pipeline/run_case.py <estimate.json> [<out.neo>]

estimate.json の形:
    {"vehicle": {"model_code", "serial_no", "desig", "category", "reg_date", "color_code"},
     "customer": {...}, "insurance": {...}, "labor_rate": 8750, "hints": {...},
     "items": [{"code","name","parts_no","method","qty","price","wage","index"}...],
     "paint": {"total","material","material_rate","paint","coat","hf","panels":[...],"booth","base","bumper_front","wax"},
     "expenses": [{"name","amount","kind"}...], "totals": {...}}
"""
import json
import os
import re
import unicodedata
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _st in (sys.stdout, sys.stderr):  # 検算結果の日本語を PC の既定（cp932）に依存させない
    try:
        if getattr(_st, 'encoding', '').lower().replace('-', '') not in ('utf8', 'utf8sig'):
            _st.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError, OSError):
        pass
from estimate_to_neo import NeoBuilder, _flag  # noqa: E402  _flag = 人が書いた真偽値欄の厳密な読み取り


def _side_tokens(name: str) -> tuple:
    """名称から (左右, 前後) を読む。分からない側は空。

    ADDATA の書き方（12.DB の 20 文字名称）:
      `LRドアパネル` = 左＋後 / `RFドアパネル` = 右＋前（英字 2 文字が続く）
      `L クリツプ` `R クリツプ` = 左右だけ（英字 1 文字のあと空白）
      `Rバンパピース` = 後（空白なしの R は「リヤ」）
    見積の書き方: `左Rrドアパネル` `右Frバンパ` `フロントバンパ` `リヤバンパ`
    **空白の有無が「右」と「リヤ」を分ける**ので、空白を消す前に見る"""
    n = unicodedata.normalize('NFKC', name or '').strip()
    # ADDATA の書式は大文字固定。見積の 'Rr' を拾わないよう re.I にしない。
    # 直後が英字の語（RRC・RFID など略語）は左右前後ではないので除く
    m = re.match(r'^(LF|LR|RF|RR)(?![A-Za-z])', n)
    if m:
        return m.group(1)[0], m.group(1)[1]
    # 左右だけを表す書き方（下書きの `_side_of` と同じ規則）:
    #   `L ｸﾘﾂﾌﾟ` `R ｸﾘﾂﾌﾟ`（英字 1 文字＋空白）/ `LH…` `RH…` / `L/H` `R/H` / `L.` `R.`
    m = re.match(r'^([LR])(?:H|/H|[.\s/])', n)
    if m:
        return m.group(1), ''
    side = ''
    m = re.match(r'^(左|右)', n)
    if m:
        side, n = ('L' if m.group(1) == '左' else 'R'), n[1:].lstrip()
    # 英字が続く語（FRP・RRC など材料名や型番）を前後と読まないよう、直後が英字でないことを条件にする
    if re.match(r'^(Fr(?![A-Za-z])|フロント|F(?![A-Za-z]))', n, re.I):
        return side, 'F'
    if re.match(r'^(Rr(?![A-Za-z])|リヤ|リア|R(?![A-Za-z]))', n, re.I):
        return side, 'R'
    return side, ''


def _rows_in_source_order(rows: list) -> list:
    """生成行を「見積に書かれていた順」に戻す。
    リサイクル置換行は生成器が末尾へ動かすが、元の RecordNo を `_orig_recno` に残しているので、
    それ（無ければ現在の RecordNo）で並べ直せば見積の行と 1 対 1 で対応できる"""
    def key(i):
        r = rows[i]
        try:
            return int(r.get('_orig_recno') or r.get('RecordNo') or (i + 1))
        except (TypeError, ValueError):
            return i + 1
    return [rows[i] for i in sorted(range(len(rows)), key=key)]


def _report_weak_matches(est: dict, rep: dict) -> list:
    """名称の「近似」で ref を決めた行のうち、標準価格も合わない行を挙げる。
    どちらか一方だけなら普通に起きる（近似でも正しい / 純正の価格改定で価格だけ違う）が、
    **両方そろった行は取り違えの疑いが濃い**。実案件 10 件・約 500 行で 4 行しか出ない絞り込み"""
    out = []
    # 通常の流れ（reading → draft_estimate）では ref が `code` に確定して渡ってくるので、
    # 生成器から見た根拠は「部品コード」になる。下書きが item に残した本来の根拠を優先して読む
    items = est.get('items') or []
    rows = rep.get('rows') or []
    # リサイクル置換行が末尾へ動いていても、元の並びに戻せば見積の行と対応が取れる
    ordered = _rows_in_source_order(rows)
    aligned = len(items) == len(ordered)
    if not aligned and items:
        # 生成の途中で行が増減した案件。下書きが残した根拠を使えないので、その旨を出してから
        # 生成器側の根拠だけで見る（黙って検査が甘くなるのを避ける）
        print(f'  （要確認の絞り込みは生成器側の根拠だけで行う: 見積 {len(items)} 行 / 生成 {len(ordered)} 行）')
    whys = [str((items[i] if aligned else {}).get('_ref_why') or '') for i in range(len(ordered))]
    for i, r in enumerate(ordered):
        why = whys[i] or str(r.get('_ref_why') or '')
        # 下書きは「名称近似(0.80)」と「ブロック内名称照合(0.82)」の 2 通りの書き方をする
        if not any(k in why for k in ('名称近似', 'ブロック内名称照合')):
            continue
        try:
            std, pr = int(r.get('PartsPriceStandardOutTax') or -1), int(r.get('PartsPriceOutTax') or -1)
            qty = max(1, int(r.get('PartsCount') or 1))
        except (TypeError, ValueError):
            continue
        if std > 0 and pr > 0 and pr != std * qty:
            out.append((str(r.get('PartsName') or '').strip(), pr, std * qty, why))
    if out:
        print(f'  要確認: 名称の近似で決めた行のうち標準価格も合わないもの {len(out)} 行'
              '（12.DB の名称一覧と照らして ref を確かめ、違えば code を書くか manual にする）')
        for nm, pr, std, why in out[:6]:
            print(f'     {nm[:22]:<24} 見積 {pr:>8,} / 標準 {std:>8,}  [{why[:26]}]')
    return out


def _check_side_front_rear(est: dict, rep: dict) -> list:
    """見積の名称と照合先（ADDATA 標準名称）で 前後・左右 が食い違う行を返す。
    2026-09-10 のヴァンガードで、リヤの部品がフロントの ref に付く取り違えが 7 行あった。
    価格の一致率と違い、正当な価格差（純正の改定・色別）で誤検知しないのが利点"""
    out = []
    items, rows = est.get('items') or [], rep.get('rows') or []
    rows = _rows_in_source_order(rows)  # リサイクル置換で末尾へ動いた行も元の並びに戻す
    if len(items) != len(rows):
        # 行が増減する加工（リサイクル置換など）が入ると、見積の行と生成行が 1 対 1 でなくなる。
        # 取り違えた組で判定してしまうより、判定しないで知らせるほうが安全
        print(f'  （前後・左右の検査は省略: 見積 {len(items)} 行 / 生成 {len(rows)} 行で対応が取れない）')
        return []
    for it, r in zip(items, rows):
        if not str(r.get('PartsCode') or '').strip():
            continue  # 手入力行は照合していないので対象外
        src = str(it.get('name') or '')
        if not src:
            continue
        std = str(r.get('PartsNameStandard') or r.get('PartsName') or '')
        (s1, f1), (s2, f2) = _side_tokens(src), _side_tokens(std)
        if s1 and s2 and s1 != s2:
            out.append((src.strip(), std.strip(), f'左右 {s1}≠{s2}'))
        elif f1 and f2 and f1 != f2:
            out.append((src.strip(), std.strip(), f'前後 {f1}≠{f2}'))
    if out:
        print(f'  ★ 見積の名称と照合先で前後・左右が食い違う行が {len(out)} 行ある（ref の取り違え）:')
        for a, b, why in out[:6]:
            print(f'     {a[:22]} → {b[:22]}（{why}）')
    return out


def _report_pn_typo(rep: dict) -> None:
    """印字（読み取り）の品番が、同じ部品コードの標準品番と 1 文字だけ違う行を知らせる。
    読み違い（0 と 1・8 と 3 など）は金額が同じなので検算では出ない（2026-09-19 本番検証: 84840-58010 を 84840-58011 と読んだ）。
    メーカーの品番改定（新旧品番）でも 1 文字違いになるので、止めずに確かめてもらう"""
    for r in rep.get('rows') or []:
        a = re.sub(r'[\s\-‐−]', '', str(r.get('PartsNo') or '')).upper()
        b = re.sub(r'[\s\-‐−]', '', str(r.get('PartsNoStandard') or '')).upper()
        if not a or not b or a == b or len(a) != len(b) or not re.search(r'\d', a):
            continue
        if sum(1 for x, y in zip(a, b) if x != y) == 1:
            print(f"  ★ 品番の 1 文字違い: {r.get('PartsCode')} {str(r.get('PartsName') or '').strip()} 印字 {r.get('PartsNo')} / 標準 {r.get('PartsNoStandard')}。"
                  '読み違いか、品番の改定（新旧品番）か見積書で確かめる')


def _report_standard_price(rep: dict) -> None:
    """見積の部品金額が ADDATA の標準価格と合っている割合を出す。
    純正定価で出す工場なら、照合が正しければほぼ全部一致する。
    低いときは ref の取り違え（前後・左右）か車両（グレード・年式）の誤りを疑う
    ——実際 2026-09-09 のヴァンガードは 64% で、リヤドアパネルがフロントの ref に付いていた"""
    rows = rep.get('rows') or []
    pairs = []
    for r in rows:
        try:
            std, pr = int(r.get('PartsPriceStandardOutTax') or -1), int(r.get('PartsPriceOutTax') or -1)
            qty = max(1, int(r.get('PartsCount') or 1))
        except (TypeError, ValueError):
            continue
        if std > 0 and pr > 0:
            pairs.append((r, pr == std * qty))
    if not pairs:
        return
    same = sum(1 for _r, ok in pairs if ok)
    rate = same * 100 // len(pairs)
    # 安い小物（1,000 円未満のクリップ・グロメット等）は工場の価格が ADDATA と食い違うことが普通にあり、
    # ref を取り違えていても損害が小さい。取り違えを見つけたいのは金額の大きい行なので、そちらで判定する
    big = [(r, ok) for r, ok in pairs if int(r.get('PartsPriceOutTax') or 0) >= 1000]
    big_same = sum(1 for _r, ok in big if ok)
    tail = f' / うち 1,000 円以上 {big_same}/{len(big)}' if big else ''
    print(f'標準価格との一致 {same}/{len(pairs)} 行（{rate}%）{tail}')
    # 1 行しか無くてもその 1 行が高額なら見逃さない（0/1 は 0%）
    if big and big_same * 100 // len(big) < 90:
        ng = [f"{str(r.get('PartsName') or '').strip()}({int(r.get('PartsPriceOutTax') or 0):,})"
              for r, ok in big if not ok][:6]
        print(f'  ★ 標準価格と合わない行がある（1,000 円以上で {len(big) - big_same} 行）。'
              f'ref の取り違え（前後・左右）か車両の年式・グレード違いを疑う: {ng}')


def _warn_too_many_manual(est: dict, rep: dict, st: dict) -> None:
    """ADDATA に載っている車なのに明細をほとんど手入力にしていないか見る。
    手入力（manual）は「ADDATA に無い品目」のための逃げ道であって、
    工場書式に品番や指数の印字が無いことは手入力にする理由にならない。
    全行を手入力にすると、部品コード・標準品番・標準価格・部位ブロックが入らない NEO になる
    （2026-09-09 アクアで実際にやってしまった）"""
    rows = rep.get('rows') or []
    if len(rows) < 5 or _flag((est.get('vehicle') or {}).get('generic'), 'vehicle.generic') or (rep.get('vehicle') or {}).get('auto_generic'):
        return  # 汎用車種（二輪・輸入車）は照合先が無いので対象外
    matched = int(st.get('matched') or 0)
    if matched * 2 >= len(rows):
        return
    print(f'  ★ ADDATA に載っている車なのに照合できたのは {matched}/{len(rows)} 行だけ。'
          'items の manual を外して名称照合させ、本当に ADDATA に無い品目だけ manual に戻すこと'
          '（inspect_estimate.py で ref と 12.DB 名称を確認できる）')


def _warn_known_unresolved(est: dict) -> list:
    """実機との差が残っていて**金額に影響する**既知の組合せ（HANDOFF §8）。該当したら ★ でコグニ実機確認へ誘導する。
    - 4600（ロワバック系）取替 と クオータパネル（4800/4801/5000/5001）取替 が同時: W90 ハイエース（ボディ 20）で
      4600 の指数が 4.0 → 11.7 になる連動加算を生成器が再現できていない（cogni_W90b、2026-09-12）"""
    out = []
    items = [it for it in (est.get('items') or []) if str(it.get('method') or '').strip() == '取替' and not it.get('manual')]
    codes = {str(it.get('code') or '').strip().zfill(4) for it in items if str(it.get('code') or '').strip()}
    if '4600' in codes and codes & {'4800', '4801', '5000', '5001'}:
        out.append('4600 取替 と クオータパネル取替 が同時にある。実機（W90 ボディ 20）では 4600 の指数が連動加算で 4.0 → 11.7 になったが生成器は再現できていない（HANDOFF §8）。'
                   'この案件はコグニ実機で 4600 の指数を確認し、違えば index を手入力（#）で書く')
    return out


def _warn_tax_included(pt: dict) -> None:
    """合計欄が「税込で印字された見積書」の値のままでないか見る。
    ディーラー・二輪の見積は各行の金額が税込のことがあり、そのまま写すと課税小計が 1.1 倍になる"""
    sub, tax = pt.get('taxable'), pt.get('tax')
    if not sub or not tax:
        return
    sub, tax = int(sub), int(tax)
    try:  # 税率は estimate の tax_rate（省略時 10%。生成器も Setting.TaxRate=10 を書く）
        rate = int(pt.get('tax_rate') or 10)
    except (TypeError, ValueError):
        rate = 10
    if rate <= 0:
        return
    if abs(sub * rate - tax * 100) <= 100:  # 税抜 × 税率 = 消費税 → 正しい写し方
        return
    if abs(sub * rate - tax * (100 + rate)) <= (100 + rate):  # 小計が税込のときだけ成り立つ関係
        net = sub * 100 // (100 + rate)
        print(f'  ★ 見積書の金額が税込で印字されている可能性: 課税小計 {sub:,} × {rate}% は'
              f' {sub * rate // 100:,} なのに消費税は {tax:,}。税抜は {net:,}。各行の金額も税抜に直すこと'
              f'（totals の tax と total は印字どおりのままでよい）')


def main(path: str, out: str = ''):
    est = json.load(open(path, encoding='utf-8-sig'))  # メモ帳等で保存した BOM 付き JSON も読めるように
    nb = NeoBuilder()
    neo, rep = nb.build(est, est['vehicle'], hints=est.get('hints'), labor_rate=est.get('labor_rate'),
                        est_date=est.get('est_date'), insurance=est.get('insurance'))
    out = out or os.path.splitext(path)[0] + '.neo'
    v = rep['vehicle']; car = rep['car']; t = rep['totals']; st = rep['stats']
    print(f"車両: {car.get('CarNameByUser')} / {car.get('CarCode')} Year {car.get('YearCode')} Body {car.get('BodyCode')} Grade {car.get('GradeCode')} FVA {car.get('FVACode')} 色 {car.get('ColorCode')} 車形 {car.get('CarFormCode')} ({v.get('confidence')})")
    print(f"装備 {rep.get('eva_write', rep['eva'])} | レバーレート {st.get('labor_rate')} | 照合 {st.get('matched')}/{len(rep['rows'])}")
    if v.get('auto_generic'):
        print(f"  ★ 車両: {v['auto_generic']}で、コグニの車種マスタに候補が無い → 輸入車として汎用車種 {car.get('CarCode')} で作った"
              '（コグニ実機と同じ「メーカー→車名 汎用」。部品コード・標準指数は入らない）。国産車なら車検証の型式指定・類別を見直す')
    _warn_too_many_manual(est, rep, st)
    for _ku in _warn_known_unresolved(est):
        print(f'  ★ {_ku}')
    if st.get('labor_rate_assumed'):
        print('  ★ レバーレートが見積に無く、工賃÷指数からも決められないので 7,280 円を仮定した。estimate.json に labor_rate を書く（標準工賃・塗装工賃がこの単価で計算されている）')
    for _an in getattr(nb, '_adas_notes', None) or []:
        print(f'  ★ {_an}')
    if str(v.get('confidence') or '') == 'low':
        print('  ★ 車両特定の確度が low。グレードが決まっていない可能性がある。'
              'scripts/pick_grade.py <estimate.json> で部品金額から絞り、hints.grade_name に書く（判断規則 10-9-3）')
    _report_standard_price(rep)
    _report_pn_typo(rep)
    # 前後・左右の食い違いは ref の取り違えそのもの。検算自身を不合格にする
    side_ng = _check_side_front_rear(est, rep)
    _report_weak_matches(est, rep)
    for _cs in rep.get('com_stale') or []:
        print(f'  ★ {_cs} を使っている ADDATA の COM.CAB から読めず、同梱の予備かばら置きの古い表を使った。WorkCodeUpdateDate・型式の照合が ADDATA の月と合わない可能性がある')
    for _se in rep.get('silent_errors') or []:   # 「失敗しても続ける」箇所が実際に失敗した（黙って別の結果になっている）
        print(f'  ★ {_se}')
    for _u in rep.get('paint_body_unresolved') or []:   # 20.DB はボディごとに面積の違う行を持つ
        if _u.get('areas'):   # 同じコードに面積の違う行が複数（ADDATA 全車種で 258 組）
            print(f"  ★ 塗装パネル {_u['code']}: 20.DB に面積の違う行が複数ある"
                  f"（{' / '.join(str(a) for a in _u['areas'])} d㎡）。先頭の {_u['areas'][0]} を採った。"
                  '面積＝塗装指数なので、見積書の dm² と突き合わせる（違えば paint.panels[].index に見積書の指数を書く）')
            continue
        _c = ('/'.join(_u['candidates']) + ' のどれか') if _u.get('candidates') else 'このボディ用の行が無い'
        print(f"  ★ 塗装パネル {_u['code']}: ボディ {_u['body']} 用の行を選べなかった（{_c}）。"
              '面積＝塗装指数が実機とずれることがあるので、見積書の dm² と突き合わせる')
    print(f"部品 {t.get('parts')} 工賃 {t.get('wage')} 塗装 {t.get('paint')} 内骨 {t.get('frame')} 費用部品 {t.get('expense_parts')} 費用工賃 {t.get('expense_wage')} 値引 {t.get('discount')} 小計 {t.get('subtotal')} 税 {t.get('tax')} 合計 {t.get('total')}")
    if st.get('dup_refs'):
        print(f"同じ部品コード・同じ修理方法の行が複数: {', '.join(st['dup_refs'])} —— コグニはこの形を保持するが、左右・前後の取り違えがないか見直す")
    pt = est.get('totals', {})
    # 項目別の検算: 見積書に印字された部品計/工賃計/塗装計/費用計と生成値を突合（読み取りミスの検出用）
    checks = [('部品計', 'parts', t.get('parts')), ('工賃計', 'wage', t.get('wage')),
              ('塗装工賃計', 'paint', (t.get('paint') or 0) - (t.get('paint_material') or 0)),  # estimate.json の paint.total/ totals.paint は塗装工賃（材料別）
              ('材料代', 'material', t.get('paint_material')), ('塗装計(材料込)', 'paint_total', t.get('paint')), ('内板骨格', 'frame', t.get('frame')),
              ('費用部品', 'expense_parts', t.get('expense_parts')), ('費用工賃', 'expense_wage', t.get('expense_wage')),
              ('費用計', 'expense', (t.get('expense_parts') or 0) + (t.get('expense_wage') or 0)),
              ('課税小計', 'taxable', t.get('subtotal')), ('消費税', 'tax', t.get('tax'))]
    # 見積書の「部品計」「工賃計」は書式により費用部品・塗装・内骨・費用工賃を含むことがあるので、組合せ候補のどれかが一致すれば OK
    alt = {'parts': [t.get('parts'), (t.get('parts') or 0) + (t.get('expense_parts') or 0)],
           'wage': [t.get('wage'), (t.get('wage') or 0) + (t.get('frame') or 0), (t.get('wage') or 0) + (t.get('paint') or 0) + (t.get('frame') or 0),
                    (t.get('wage') or 0) + (t.get('paint') or 0) + (t.get('frame') or 0) + (t.get('expense_wage') or 0),
                    (t.get('wage') or 0) + (t.get('paint') or 0) - (t.get('paint_material') or 0) + (t.get('frame') or 0) + (t.get('expense_wage') or 0)]}
    # 費用工賃の一部だけが「作業計」に入り、残りが印字の「諸費用計」（下書きが totals.expense_printed に残す）に入る書式（トヨタ系ディーラー:
    # センサーエーミングは作業計、レッカー・産廃は諸費用計。2026-09-14 JPN タクシー 作業計 378,938 = 明細 296,378 + 内骨 62,560 + エーミング 20,000）
    # 印字の塗装工賃計は追加項目（paint.other）を含まない（生成器の塗装計は含む）。追加項目を除いた額も一致の候補にする（Codex 指摘。2026-09-14 C-HR）
    _oth = sum(int((o or {}).get('wage') or 0) for o in ((est.get('paint') or {}).get('other') or []) if isinstance(o, dict))
    if _oth:
        alt['paint'] = [(t.get('paint') or 0) - (t.get('paint_material') or 0) - _oth]
    _ep = pt.get('expense_printed')
    if _ep is not None and 0 <= int(_ep) <= int(t.get('expense_wage') or 0):
        _ew_in = int(t.get('expense_wage') or 0) - int(_ep)
        alt['wage'] += [(t.get('wage') or 0) + (t.get('frame') or 0) + _ew_in,
                        (t.get('wage') or 0) + (t.get('paint') or 0) - (t.get('paint_material') or 0) + (t.get('frame') or 0) + _ew_in,
                        (t.get('wage') or 0) + (t.get('paint') or 0) + (t.get('frame') or 0) + _ew_in]
    cat_ok = True
    # 差を許してよいのは、工場側の丸めを 3 点（neo_total / tolerance / 理由）で説明したときだけ。
    # 揃っていなければ tolerance は 0 として扱い、項目別の差も見逃さない
    _why = str(pt.get('tolerance_reason') or pt.get('neo_total_reason') or pt.get('note') or '').strip()
    _tol3 = (pt.get('neo_total') is not None and int(pt.get('tolerance') or 0) > 0 and bool(_why))
    eff_tol = int(pt.get('tolerance') or 0) if _tol3 else 0
    if int(pt.get('tolerance') or 0) > 0 and not _tol3:
        print('  ★ totals.tolerance が書かれているが neo_total か理由が無いので許容しない。'
              'totals.neo_total（コグニ計算の合計）と totals.tolerance_reason を揃えること')
    # 許容差（tolerance）を効かせてよいのは、工場の丸めで実際に差が出る項目だけ。
    # 部品計・材料代・費用は工場が円単位で出すので 0 円一致を求める（合計用の幅を流用しない。Codex 指摘）
    TOL_KEYS = {'wage', 'paint', 'paint_total', 'frame', 'taxable', 'tax'}
    # 単価に円未満の端数がある見積（クリップ 単価 154.5 円 …）は部品計にも丸め差が出る。
    # そのときだけ totals.tolerance_keys で対象を名指しする（下書きが証拠付きで書く。手書きでも 3 点セットは必要）。
    # 広げてよいのは **部品計だけ**（材料代・費用は工場が円単位で出すので 0 円一致のまま。レビュー指摘 2026-09-17）
    TOL_KEYS |= {str(k) for k in (pt.get('tolerance_keys') or []) if str(k) == 'parts'}
    for label, key, gen in checks:
        if pt.get(key) is None or gen is None:
            continue
        d = int(gen) - int(pt[key])
        if d != 0 and key in alt and any(v is not None and int(v) == int(pt[key]) for v in alt[key]):
            print(f"  検算 {label:<10} 見積 {int(pt[key]):>10,}  OK（費用/塗装/内骨を含む集計として一致）")
            continue
        print(f"  検算 {label:<10} 見積 {int(pt[key]):>10,} / 生成 {int(gen):>10,}  {'OK' if d == 0 else f'差 {d:+,}'}")
        _lim = eff_tol if key in TOL_KEYS else 0  # 丸めの出ない項目に合計用の幅を流用しない
        if abs(d) > _lim:  # 説明できない項目差分は不合格。tolerance が効くのは 3 点セットが揃っているときだけ
            cat_ok = False
            if key not in TOL_KEYS and eff_tol and abs(d) <= eff_tol:
                print(f'  ★ {label} は工場が円単位で出す項目なので totals.tolerance では許容しない。読み取りを見直す')
        elif d:
            print(f'  ★ {label} の差 {d:+,} を totals.tolerance（{eff_tol} 円）で許容した。'
                  '工場書式の丸めで説明できることを確かめる')
    unmatched = [r for r in rep['rows'] if not r.get('PartsCode') and not r.get('_manual')]
    if unmatched:
        print('  未照合行:', [r.get('PartsName', '').strip() for r in unmatched])
        if len(unmatched) > int(pt.get('allow_unmatched') or 0):  # 照合漏れも不合格（totals.allow_unmatched で想定件数を許可）
            cat_ok = False
    ok = cat_ok and not side_ng
    if pt.get('total') is not None:
        d = int(t.get('total', -1)) - int(pt['total'])
        # 工場が円未満・10 円単位で丸める書式は、コグニでは再現できない差が残る。
        # 許容するのは 3 つ揃ったときだけ: neo_total（コグニ計算の合計）/ tolerance（許容幅・円）/ 理由。
        # どれか欠けたら「合計を合わせるための逃げ」になるので不合格にする
        tol, why = int(pt.get('tolerance') or 0), _why  # 理由の正式名は tolerance_reason（古い案件の note / neo_total_reason も読む）
        if d != 0 and pt.get('neo_total') is not None and int(t.get('total', -1)) == int(pt['neo_total']):
            print(f"見積書合計との一致: 工場の印字 {int(pt['total']):,} / コグニ計算 {int(t.get('total', -1)):,}（差 {d:+,}）")
            if tol <= 0 or abs(d) > tol:
                print(f'  ★ totals.tolerance が {tol} 円なので、この差 {d:+,} は許容できない。'
                      '工場書式の丸めで説明できる幅を totals.tolerance に書くこと（説明できないなら読み取りを直す）')
                ok = False
            elif not why:
                print('  ★ 差を許容しているのに理由が書かれていない。'
                      'totals.tolerance_reason（または totals.note）に、どの書式のどんな丸めで差が出るのかを書くこと')
                ok = False
            else:
                print(f'  許容幅 {tol} 円 / 理由:', why[:150])
        else:
            print('見積書合計との一致:', 'OK' if d == 0 else f"NG (見積 {pt['total']}, 差 {d:+,})")
            if d != 0:
                _warn_tax_included(pt)
        # totals.neo_total = コグニ形式に直したときの期待合計（工場書式の丸め差が既知の案件。N-ONE 693,732）。無ければ見積書合計と一致が合格
        expect = pt.get('neo_total', pt['total'])
        if int(t.get('total', -1)) != int(expect):  # 合計が合わない（理由なしの neo_total は上で既に不合格にしている）
            ok = False
            print(f'合格条件 NG: 期待 {int(expect):,} / 生成 {int(t.get("total", -1)):,}')
    # 検算に通ってから納品先へ置く（不合格の NEO を納品物の名前で残さない）。
    # 不合格でも中身は見たいので、.ng.neo として隔離する
    _dst = out if ok else os.path.splitext(out)[0] + '.ng.neo'
    _tmp = f'{_dst}.{os.getpid()}.tmp'  # 書きかけで落ちても前回の NEO を壊さない（並行実行よけに PID を入れる）
    with open(_tmp, 'wb') as _f:
        _f.write(neo)
    os.replace(_tmp, _dst)
    if ok:
        print('出力:', _dst)
    else:
        print('不合格なので納品先には置かない。中身の確認用:', _dst)
    return ok


if __name__ == '__main__':
    sys.exit(0 if main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else '') else 1)
