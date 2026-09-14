# -*- coding: utf-8 -*-
"""保険・案件欄の単体テスト（2026-09-14）
  estimate['insurance'] の 受付番号・代理店・アジャスター・入出庫日・修理日数 が、コグニと同じ列に入ること:
    Insurance.AgencyName / AdjusterName / RepairDays、FileInfo.AcceptNo / GarageIn* / GarageOut*、XML の AcceptNo / AdjusterName、AnSvMail.ini の AcceptNo
  無ければ雛形と同じ既定（'' / -1 / '00000000' / 令和 / ''）のまま。XML の GarageIn/OutDate は書かない（実機 NEO 178 本で日付があっても空）
    cd files && PYTHONIOENCODING=utf-8 python claude_neo_pipeline/tests/unit_insurance.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
from estimate_to_neo import NeoBuilder  # noqa: E402
import neo_diff  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': ''}
ITEMS = [{'code': '1400', 'name': 'Fﾊﾞﾙｸﾍｯﾄﾞ', 'method': '取替', 'qty': 1}]
INS = {'company': 'テスト損保', 'policy_no': 'P-0001', 'contractor': 'ｹﾝｼｮｳ ﾀﾛｳ', 'accident_date': '20260901', 'accept_no': 'A-2026-0001',
       'agency': 'テスト代理店', 'adjuster': 'テスト査定', 'garage_in': '20260903', 'garage_out': '2026/09/10', 'repair_days': '7', 'factory': 'テスト工場 000-0000'}


def build(ins: dict, tag: str, explicit: dict | None = None):
    """estimate['insurance'] だけで build する（引数 insurance= は渡さない。estimate_schema.md の書き方どおりの呼び出し元を守る）。
    explicit を渡したときだけ insurance= 引数も付け、引数が estimate より優先されることを見る"""
    nb = NeoBuilder()
    est = {'source': 'unit_insurance', 'issuer': '', 'est_date': '20260908', 'vehicle': VEH, 'customer': {'name': 'ｹﾝｼｮｳ ﾀﾛｳ'}, 'insurance': ins,
           'labor_rate': 8000, 'items': ITEMS, 'paint': {}, 'expenses': [], 'totals': {}, 'hints': {}}
    kw = {'insurance': explicit} if explicit is not None else {}
    neo, rep = nb.build(est, VEH, hints=est.get('hints'), labor_rate=8000, est_date='20260908', **kw)
    tmp = os.path.join(os.environ.get('TEMP', HERE), f'unit_insurance_{tag}.neo')
    open(tmp, 'wb').write(neo)
    d = neo_diff.load(tmp)
    ifc = d['AnSvIf0001.sld']
    ins_row = dict(zip([c[1] for c in ifc.execute('pragma table_info(Insurance)')], ifc.execute('select * from Insurance').fetchone()))
    fi_row = dict(zip([c[1] for c in ifc.execute('pragma table_info(FileInfo)')], ifc.execute('select * from FileInfo').fetchone()))
    xml = next((v for k, v in d['files'].items() if k.lower().endswith('.xml')), b'').decode('cp932', 'replace')
    mail = d['files'].get('AnSvMail.ini', b'').decode('cp932', 'replace')
    return ins_row, fi_row, xml, mail


def tag(xml: str, name: str) -> str:
    m = re.search(r'<%s>(.*?)</%s>' % (name, name), xml)
    return m.group(1) if m else '(タグなし)'


def main() -> int:
    fails = 0

    def check(cond, msg):
        nonlocal fails
        if not cond:
            fails += 1; print('FAIL', msg)

    i, f, xml, mail = build(INS, 'full')
    check(i['AgencyName'] == 'テスト代理店' and i['AdjusterName'] == 'テスト査定' and i['RepairDays'] == 7,
          f"Insurance agency/adjuster/days {i['AgencyName']!r} {i['AdjusterName']!r} {i['RepairDays']!r}")
    check(i['PolicyNo'] == 'P-0001' and i['ContractorName'] == 'ｹﾝｼｮｳ ﾀﾛｳ' and i['AccidentDate'] == '20260901', f"Insurance 従来欄 {i['PolicyNo']!r} {i['AccidentDate']!r}")
    check(f['AcceptNo'] == 'A-2026-0001', f"FileInfo.AcceptNo {f['AcceptNo']!r}")
    check(f['GarageInDate'] == '20260903' and f['GarageInEra'] == '令和' and str(f['GarageInEraYear']).strip() not in ('', 'None'),
          f"FileInfo.GarageIn {f['GarageInDate']!r} {f['GarageInEra']!r} {f['GarageInEraYear']!r}")
    check(f['GarageOutDate'] == '20260910', f"FileInfo.GarageOutDate（区切り付き入力 2026/09/10 → 8 桁） {f['GarageOutDate']!r}")
    check(tag(xml, 'AcceptNo') == 'A-2026-0001' and tag(xml, 'AdjusterName') == 'テスト査定', f"XML AcceptNo/AdjusterName {tag(xml, 'AcceptNo')!r} {tag(xml, 'AdjusterName')!r}")
    check(tag(xml, 'GarageInDate') == '' and tag(xml, 'GarageOutDate') == '', f"XML GarageIn/OutDate は空のまま {tag(xml, 'GarageInDate')!r}")
    check('AcceptNo=A-2026-0001' in mail, 'AnSvMail.ini AcceptNo')

    i, f, xml, mail = build({}, 'empty')
    check(i['AgencyName'] == '' and i['AdjusterName'] == '' and i['RepairDays'] == -1, f"空: Insurance 既定 {i['AgencyName']!r} {i['AdjusterName']!r} {i['RepairDays']!r}")
    check(f['AcceptNo'] == '' and f['GarageInDate'] == '00000000' and f['GarageInEra'] == '令和' and f['GarageOutDate'] == '00000000',
          f"空: FileInfo 既定 {f['AcceptNo']!r} {f['GarageInDate']!r} {f['GarageInEra']!r}")
    check(tag(xml, 'AcceptNo') == '' and tag(xml, 'AdjusterName') == '', '空: XML 既定')
    check('AcceptNo=\r\n' in mail or 'AcceptNo=\n' in mail, '空: AnSvMail.ini AcceptNo=')

    i, f, xml, mail = build({'repair_days': 'abc', 'garage_in': '2026-9-3'}, 'bad')   # 読めない値は既定に落とす（黙って別の値にしない）
    check(i['RepairDays'] == -1 and f['GarageInDate'] == '00000000', f"読めない値 {i['RepairDays']!r} {f['GarageInDate']!r}")

    # 日付なしの番兵 '00000000' を渡されても雛形と同じ（EraYear が '0000' にならない。Codex 指摘 2026-09-14）
    i, f, xml, mail = build({'garage_in': '00000000', 'garage_out': '00000000', 'accident_date': '00000000'}, 'zero')
    check(f['GarageInDate'] == '00000000' and f['GarageInEra'] == '令和' and str(f['GarageInEraYear']) == '' and str(f['GarageOutEraYear']) == '',
          f"番兵 00000000 → 雛形どおり {f['GarageInEra']!r} {f['GarageInEraYear']!r} {f['GarageOutEraYear']!r}")
    check(i['AccidentDate'] == '00000000' and str(i['AccidentEraYear']) == '', f"番兵 00000000（事故日） {i['AccidentEraYear']!r}")

    # 改元の境界日: 2019-04-30 入庫は平成 31 年、2019-05-01 事故は令和元年（年だけで分けると狂う。Codex 指摘 2026-09-14）
    i, f, xml, mail = build({'garage_in': '20190430', 'garage_out': '20190501', 'accident_date': '20190501'}, 'era')
    check(f['GarageInEra'] == '平成' and str(f['GarageInEraYear']) == '0031' and f['GarageOutEra'] == '令和' and str(f['GarageOutEraYear']) == '0001',
          f"改元境界 GarageIn/Out {f['GarageInEra']!r} {f['GarageInEraYear']!r} / {f['GarageOutEra']!r} {f['GarageOutEraYear']!r}")
    check(i['AccidentEra'] == '令和' and str(i['AccidentEraYear']) == '0001', f"改元境界 Accident {i['AccidentEra']!r} {i['AccidentEraYear']!r}")

    # 引数 insurance= を渡したときはそちらが優先（make_neo.py など既存の呼び出し元の経路）
    i, f, xml, mail = build({'accept_no': 'IGNORED'}, 'explicit', explicit={'accept_no': 'B-0002', 'adjuster': '引数側'})
    check(f['AcceptNo'] == 'B-0002' and i['AdjusterName'] == '引数側', f"引数 insurance= が優先 {f['AcceptNo']!r} {i['AdjusterName']!r}")

    print('unit_insurance:', 'all ok' if not fails else f'{fails} 件が不合格')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
