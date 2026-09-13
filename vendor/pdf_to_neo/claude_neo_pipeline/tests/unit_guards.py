# -*- coding: utf-8 -*-
"""入力ガードの単体テスト（境界条件）: 誤った estimate.json が「静かに通る」ことがないかを確かめる。
期待: 数量 0 以下 / 未知の修理方法 / 負のレバーレート / wage_round 不正 / 20.DB に無い塗装パネル / 費用の負値・行あふれ は ValueError で止まる。
      数量 999・明細 200 行・金額の文字列（'1,000'）・費用 28 件までは通る。
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_guards.py
"""
import copy
import json
import os
import sys
import traceback

B = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # <repo>/files（配布先でも動くよう __file__ から求める）
sys.path.insert(0, os.path.join(B, 'claude_neo_pipeline')); sys.path.insert(0, os.path.join(B, 'claude_neo_pipeline', 'tests'))
import neo_diff  # noqa: E402
from estimate_to_neo import NeoBuilder  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': 'YR586P'}
BASE = {'source': 'edge', 'issuer': '', 'est_date': '20260908', 'vehicle': VEH, 'customer': {}, 'insurance': {}, 'labor_rate': 8000,
        'items': [{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'qty': 1}], 'paint': {}, 'expenses': [], 'totals': {}, 'hints': {}}
nb = NeoBuilder()


def run(tag, mutate, expect_error=False):
    est = copy.deepcopy(BASE)
    try:
        mutate(est)
    except Exception as e:  # noqa: BLE001
        print(f'  {tag}: mutate 失敗 {e}'); return
    try:
        neo, rep = nb.build(est, est['vehicle'], hints=est.get('hints'), labor_rate=est.get('labor_rate'), est_date=est.get('est_date'), insurance={})
    except Exception as e:  # noqa: BLE001
        kind = 'ok(明確に停止)' if expect_error else '**例外**'
        print(f'  {tag}: {kind} {type(e).__name__}: {str(e)[:110]}')
        return
    tmp = os.path.join(os.environ.get('TEMP', '.'), 'edge.neo')
    open(tmp, 'wb').write(neo)
    d = neo_diff.load(tmp); em = d['AnSvEm0001.sld']
    cols = [c[1] for c in em.execute('pragma table_info(ERParts)')]
    rows = [dict(zip(cols, r)) for r in em.execute('select * from ERParts order by RecordNo')]
    tc = [c[1] for c in em.execute('pragma table_info(Total)')]
    tot = dict(zip(tc, em.execute('select * from Total').fetchone()))
    smb = None
    try:
        import neo_container as nc
        raw = open(tmp, 'rb').read(); ck = nc.find_real_cks(raw); dec = nc.decompress_neo(raw, ck)
        mgmt, entries = nc.parse_entries(raw, ck[0]); files = nc.extract_files(dec, entries)
        smb = files.get('AnSMB.txt')
    except Exception:
        pass
    smb_lines = len([l for l in (smb or b'').split(b'\r\n') if l.strip()]) if smb else None
    note = f"rows={len(rows)} total={tot.get('Total')} smb={smb_lines}"
    flag = ''
    if expect_error:
        flag = ' ← **止まるべきなのに通った**'
    neg = [r['PartsCode'] for r in rows if int(r.get('PartsPriceOutTax') or 0) < -1 or int(r.get('WageOutTax') or 0) < -1]
    if neg:
        flag += f' ← 負値 {neg[:3]}'
    if tot.get('Total') is not None and int(tot['Total']) < 0:
        flag += ' ← 合計が負'
    print(f'  {tag}: {note}{flag}')


FAILS = []


def expect_error(tag, mutate):
    est = copy.deepcopy(BASE); mutate(est)
    try:
        NeoBuilder().build(est, est['vehicle'], hints=est.get('hints'), labor_rate=est.get('labor_rate'), est_date=est.get('est_date'), insurance={})
    except ValueError:
        print('ok  ', tag, '→ 明確に停止'); return
    except Exception as e:  # noqa: BLE001
        FAILS.append(f'{tag}: ValueError 以外 {type(e).__name__}'); print('FAIL', tag, type(e).__name__); return
    FAILS.append(f'{tag}: 止まらずに通った'); print('FAIL', tag, '止まらずに通った')


def expect_ok(tag, mutate):
    est = copy.deepcopy(BASE); mutate(est)
    try:
        NeoBuilder().build(est, est['vehicle'], hints=est.get('hints'), labor_rate=est.get('labor_rate'), est_date=est.get('est_date'), insurance={})
        print('ok  ', tag, '→ 通る')
    except Exception as e:  # noqa: BLE001
        FAILS.append(f'{tag}: 通るべきだが {type(e).__name__}: {e}'); print('FAIL', tag, type(e).__name__, str(e)[:80])


expect_error('数量 0', lambda e: e['items'][0].update({'qty': 0}))
expect_error('数量 -1', lambda e: e['items'][0].update({'qty': -1}))
expect_error('未知の修理方法', lambda e: e['items'][0].update({'method': 'ぐるぐる'}))
expect_error('負のレバーレート', lambda e: e.update({'labor_rate': -8000}))
expect_error('wage_round 不正', lambda e: e.update({'wage_round': 7}))
expect_error('費用の負値', lambda e: e.update({'expenses': [{'name': 'x', 'amount': -100, 'kind': 'wage'}]}))
expect_error('費用 40 件（行あふれ）', lambda e: e.update({'expenses': [{'name': f'費用{i}', 'amount': 100, 'kind': 'wage'} for i in range(40)]}))
expect_error('20.DB に無い塗装パネル', lambda e: e.update({'paint': {'paint': '２Ｋ', 'coat': 'ソリッド', 'hf': 'しない', 'panels': [{'code': '9999', 'name': 'x', 'method': '取替', 'index': 1.0}], 'material_rate': 55}}))
expect_ok('数量 999', lambda e: e['items'][0].update({'qty': 999}))
expect_ok('明細 200 行', lambda e: e.update({'items': [dict(e['items'][0], name=f'x{i}') for i in range(200)]}))
expect_ok('費用の金額が文字列', lambda e: e.update({'expenses': [{'name': 'x', 'amount': '1,000', 'kind': 'wage'}]}))
expect_ok('費用 28 件', lambda e: e.update({'expenses': [{'name': f'費用{i}', 'amount': 100, 'kind': 'wage'} for i in range(28)]}))
print('unit_guards:', 'all ok' if not FAILS else f'{len(FAILS)} failed')
sys.exit(1 if FAILS else 0)

_OLD = '''
print('=== 明細の境界')
run('数量 0', lambda e: e['items'][0].update({'qty': 0}))
run('数量 -1', lambda e: e['items'][0].update({'qty': -1}))
run('数量 999', lambda e: e['items'][0].update({'qty': 999}))
run('金額 負', lambda e: e['items'][0].update({'parts_price': -5000}))
run('明細ゼロ件', lambda e: e.update({'items': []}))
run('同じ部品コード・同じ修理方法 2 行', lambda e: e['items'].append(dict(e['items'][0])))
run('同じ部品コード・別修理方法 2 行', lambda e: e['items'].append({'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '脱着', 'qty': 1}))
run('存在しない部品コード', lambda e: e['items'][0].update({'code': '9999'}))
run('部品コードが文字', lambda e: e['items'][0].update({'code': 'ABCD'}))
run('修理方法が不明語', lambda e: e['items'][0].update({'method': 'ぐるぐる'}))
run('名称が空', lambda e: e['items'][0].update({'name': '', 'code': None}))
run('指数 100 時間', lambda e: e['items'][0].update({'index': 100.0}))
run('指数 負', lambda e: e['items'][0].update({'index': -1.5}))
run('明細 200 行', lambda e: e.update({'items': [dict(e['items'][0], name=f'x{i}') for i in range(200)]}))
print('=== 車両の境界')
run('型式なし', lambda e: e['vehicle'].update({'model_code': ''}), expect_error=False)
run('型式指定・類別なし', lambda e: e['vehicle'].update({'desig': '', 'category': ''}))
run('初度登録が未来', lambda e: e['vehicle'].update({'reg_date': 'R30.1'}))
run('カラーコード不正', lambda e: e['vehicle'].update({'color_code': 'ZZZZZ'}))
run('vehicle 空', lambda e: e.update({'vehicle': {}}), expect_error=True)
print('=== 設定の境界')
run('レート 0', lambda e: e.update({'labor_rate': 0}))
run('レート 負', lambda e: e.update({'labor_rate': -8000}))
run('wage_round 不正', lambda e: e.update({'wage_round': 7}))
run('tax_round 不正語', lambda e: e.update({'tax_round': 'まるめる'}))
run('hints.eva_codes 不正', lambda e: e.update({'hints': {'eva_codes': ['あ', 1, None]}}))
print('=== 塗装の境界')
run('paint.total だけ', lambda e: e.update({'paint': {'total': 50000}}))
run('panels 空 + base', lambda e: e.update({'paint': {'panels': [], 'base': {'index': 1.0}}}))
run('パネルコードが不正', lambda e: e.update({'paint': {'paint': '２Ｋ', 'coat': 'ソリッド', 'hf': 'しない', 'panels': [{'code': '9999', 'name': 'x', 'method': '取替'}], 'material_rate': 55}}))
run('material_rate 200%', lambda e: e.update({'paint': {'paint': '２Ｋ', 'coat': 'ソリッド', 'hf': 'しない', 'panels': [{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟ', 'method': '取替'}], 'material_rate': 200}}))
print('=== 費用・合計の境界')
run('費用 kind 不正', lambda e: e.update({'expenses': [{'name': 'x', 'amount': 1000, 'kind': 'ぐ'}]}))
run('費用 金額が文字', lambda e: e.update({'expenses': [{'name': 'x', 'amount': '1,000', 'kind': 'wage'}]}))
run('totals が文字', lambda e: e.update({'totals': {'total': '715,000'}}))
run('target_total 不可能', lambda e: e.update({'totals': {'target_total': 1}}))

'''
