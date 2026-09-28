# -*- coding: utf-8 -*-
"""_runner.py — vendor の検算関数（reading_pages.validate_page / merge、reading_check.Checker）を別プロセスで呼ぶ。

reader.py がページごとの検算・束ね・合計欄の検算に使う。vendor のスクリプトは import 時に
skill_env.apply() で os.environ と sys.path を書き換えるので、アプリのプロセス（Streamlit は 1 プロセスを
全利用者で共有）では import せず、この小さなプロセスの中だけで済ませる。

  stdin : JSON {"op": "validate", "vendor_root": ..., "header": {...}, "page": {...}}
          JSON {"op": "merge",    "vendor_root": ..., "case_dir": ..., "force": false}
          JSON {"op": "resolve_vehicle", "vendor_root": ..., "addata_root": ..., "vehicle": {...}, "hints": {...}}
          JSON {"op": "vehicle_crosscheck", "vendor_root": ..., "addata_root": ..., "variants": [{...}], "extra_codes": [...]}
  stdout: 最後の行に  @@RESULT@@<JSON>   （vendor 側の print が混ざっても拾えるように目印を付ける）
"""
from __future__ import annotations

# 読み込みを始めたときのコードの指紋（ファイルの最後で読み直し、同じ中身のときだけ __app_src_digest__ に控える。読み込みの途中で
# push されたら控えず、古い扱いにして読み直させる。レビュー 3 周目）
try:
    import hashlib as _stamp_hashlib0
    with open(__file__, 'rb') as _stamp_f0:
        _stamp_digest_at_start = _stamp_hashlib0.sha256(_stamp_f0.read()).hexdigest()
    del _stamp_hashlib0, _stamp_f0
except Exception:  # noqa: BLE001
    _stamp_digest_at_start = None

import json
import os
import sys

MARK = '@@RESULT@@'


def main() -> int:
    raw = sys.stdin.read()
    req = json.loads(raw)
    root = req['vendor_root']
    os.environ['REPO_ROOT'] = root  # このプロセスだけ
    for p in (os.path.join(root, '.claude', 'skills', 'pdf-to-neo', 'scripts'),
              os.path.join(root, 'claude_neo_pipeline'), root):
        sys.path.insert(0, p)
    import reading_pages  # noqa: E402  vendor
    from reading_check import Checker  # noqa: E402  vendor
    op = req.get('op')
    if op == 'validate':
        out = reading_pages.validate_page(req['header'], req['page'])
    elif op == 'merge':
        rd, msgs = reading_pages.merge(req['case_dir'], bool(req.get('force')))
        check = Checker(rd).run() if rd else {}
        out = {'reading': rd, 'messages': list(msgs), 'check': check}
    elif op == 'resolve_vehicle':
        # reading の vehicle / hints から車種コードを決める（PC の Addata を橋渡しする経路: COM だけで足りる。2026-09-14）
        from addata_vehicle_resolver import AddataVehicleResolver  # noqa: E402  vendor
        v = req.get('vehicle') or {}
        try:
            r = AddataVehicleResolver(req.get('addata_root') or os.environ.get('ADDATA_ROOT') or '').resolve(
                model_code=str(v.get('model_code') or ''), serial_no=str(v.get('serial_no') or ''), desig=str(v.get('desig') or ''),
                category=str(v.get('category') or ''), reg_date=str(v.get('reg_date') or ''), color_code=str(v.get('color_code') or ''),
                hints=req.get('hints') or {})
            car = r.get('neo_car') or {}
            out = {'car_code': str(car.get('CarCode') or ''), 'car_name': str(car.get('CarName') or ''),
                   'confidence': r.get('confidence'), 'evidence': r.get('evidence')}
        except Exception as e:  # noqa: BLE001  理由を返す（呼び出し側が画面に出す）
            out = {'car_code': '', 'error': f'{type(e).__name__}: {e}'}
    elif op == 'vehicle_crosscheck':
        # 車種を決める 2 つの手がかりが同じ車を指すか（第 4 弾 E1）。vendor の検索関数を読むだけで、車種は決めない。
        # variants: [{model_code, serial_no, desig, category}, …]（印字の値・車検証の値の組）→ 組ごとに
        # by_serial（型式＋車台番号 = KA06 で引けた車種コード）と by_desig（型式指定＋類別区分 = KA81 で引けた車種コード）。
        # names: 出てきた車種コード（と extra_codes）の「メーカー 車名」（KATB021。顧客情報ではない）
        from addata_vehicle_resolver import AddataVehicleResolver  # noqa: E402  vendor
        res = AddataVehicleResolver(req.get('addata_root') or os.environ.get('ADDATA_ROOT') or '')
        results, codes = [], set(str(c) for c in (req.get('extra_codes') or []) if c)
        for v in (req.get('variants') or []):
            v = v or {}
            one = {}
            try:
                one['by_serial'] = sorted({r['car_code'] for r in res.lookup_by_model_serial(
                    str(v.get('model_code') or ''), str(v.get('serial_no') or ''))})
            except Exception as e:  # noqa: BLE001  引けない理由を返す（呼び出し側は「確かめられなかった」と扱う）
                one['by_serial'], one['error'] = None, f'{type(e).__name__}: {e}'
            d, c = str(v.get('desig') or '').strip(), str(v.get('category') or '').strip()
            try:
                _recs = res.lookup_by_designation(d, c) if (d and c) else []
                one['by_desig'] = sorted({r['car_code'] for r in _recs})
                # 同じ車でもグレード・ボディ・駆動が変わるかを比べるための、型式指定＋類別の指す行（車種|年式|ボディ|グレード|FVA|4WD）
                one['desig_recs'] = sorted({'|'.join(str(r.get(k)) for k in ('car_code', 'year_code', 'body_code', 'grade_code',
                                                                               'fva_code', 'four_wd')) for r in _recs})
            except Exception as e:  # noqa: BLE001
                one['by_desig'], one['error'] = None, f'{type(e).__name__}: {e}'
            codes.update(one.get('by_serial') or [])
            codes.update(one.get('by_desig') or [])
            results.append(one)
        names = {}
        try:
            _cn, _mn, _mk = res.car_names(), res.maker_names(), res.maker_of_car()
            for code in sorted(codes):
                nm = (_cn.get(code) or [])
                names[code] = f"{_mn.get(_mk.get(code, ''), '')} {nm[0][2] if nm else ''}".strip()
        except Exception:  # noqa: BLE001  名前が引けなくてもコードで出す
            pass
        out = {'results': results, 'names': names}
    else:
        raise SystemExit(f'unknown op: {op!r}')
    sys.stdout.write('\n' + MARK + json.dumps(out, ensure_ascii=False, default=str) + '\n')
    sys.stdout.flush()
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    sys.exit(main())

# 読み込んだときのコードの指紋（app.sync_app_modules が「メモリのコードがディスクと同じか」を見る。読み込みの時点で
# 控えないと、あとから初めて import したモジュールが「古い」と見なされ、偽の版ずれで変換を断っていた。バグハント 3 回目 N2）。
# ファイルの最後に置く: 読み直しが途中で例外になったときは古い指紋のまま残り、版ずれとして断れる（先頭に置くと
# 途中までしか新しくないモジュールを「揃った」と見ていた。レビュー 2026-09-15）
try:
    import hashlib as _stamp_hashlib
    with open(__file__, 'rb') as _stamp_f:
        _stamp_now = _stamp_hashlib.sha256(_stamp_f.read()).hexdigest()
    if _stamp_now == globals().get('_stamp_digest_at_start'):
        __app_src_digest__ = _stamp_now
    del _stamp_hashlib, _stamp_f, _stamp_now
except Exception:  # noqa: BLE001
    pass
