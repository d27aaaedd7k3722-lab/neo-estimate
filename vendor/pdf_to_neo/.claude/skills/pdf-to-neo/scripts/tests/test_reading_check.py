# -*- coding: utf-8 -*-
"""reading_check.py の単体テスト（架空の見積。ADDATA 不要）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_reading_check.py
"""
from __future__ import annotations

import copy
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import reading_check as rc  # noqa: E402

BASE = {
    'issuer': 'テスト鈑金（福岡県テスト市1-2-3 TEL 000-000-0000 担当 甲）',
    'format': 'B', 'labor_rate': 8000,
    'vehicle': {'model_code': 'X'},
    'blocks': [
        {'title': 'フロントバンパー', 'page': 1, 'subtotal': {'rows': 3, 'parts': 60000, 'wage': 12000}, 'rows': [
            '|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ|取替|52119-11111|1.50|1|50000|12000||',
            '|ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ|取替|90467-11111||10|5000|||',
            '|ﾊﾞﾝﾊﾟ ﾋﾟｰｽ|取替|52161-11111||1|5000|||',
        ]},
        {'title': '右 フロントフェンダー', 'page': 2, 'rows': [
            '|RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ|取替|53811-11111|2.00|1|30000|16000||',
            '|RH ﾌｪﾝﾀﾞ ﾗｲﾅ|取替|53875-11111||1|3000|||',
        ]},
    ],
    'pages': {'1': {'rows': 3, 'parts': 60000, 'wage': 12000}, '2': {'rows': 2, 'parts': 33000, 'wage': 16000}},
    'paint': {'total': 40000, 'material': 22000},
    'expenses': [{'name': 'ショートパーツ', 'amount': 2000, 'in': '部品計'}, {'name': 'コーティング', 'amount': 10000, 'in': '諸費用計'}],
    'totals': {'parts': 95000, 'wage': 28000, 'paint': 40000, 'material': 22000, 'expense': 10000, 'taxable': 195000, 'tax': 19500, 'total': 214500},
}


def run(rd: dict) -> dict:
    return rc.Checker(copy.deepcopy(rd)).run()


def has(msgs: list[str], word: str) -> bool:
    return any(word in m for m in msgs)


def test_clean():
    r = run(BASE)
    assert not r['fail'], r['fail']
    assert not r['warn'], r['warn']
    assert r['settings']['labor_rate'] == 8000 and r['settings']['wage_round'] == 10 and r['settings']['tax_round'] == '四捨五入'
    assert r['settings']['format'] == 'B'


def test_unit_vs_total():
    rd = copy.deepcopy(BASE)
    rd['blocks'][0]['rows'][1] = '|ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ|取替|90467-11111||10|500|||unit=500'  # 単価を金額に写した
    r = run(rd)
    assert has(r['fail'], '単価 500 × 数量 10'), r['fail']
    assert has(r['fail'], 'ブロック【フロントバンパー】: 部品計 印字 60,000 / 転記 55,500'), r['fail']


def test_expense_wrong_subtotal():
    rd = copy.deepcopy(BASE)
    rd['expenses'][0]['in'] = '諸費用計'  # ショートパーツを諸費用に入れてしまった
    r = run(rd)
    assert has(r['fail'], '部品計') and has(r['fail'], '費用 ショートパーツ'), r['fail']


def test_expense_missing_in():
    rd = copy.deepcopy(BASE)
    del rd['expenses'][1]['in']
    r = run(rd)
    assert has(r['fail'], 'コーティング: in'), r['fail']


def test_tax_floor():
    rd = copy.deepcopy(BASE)
    rd['totals'].update({'taxable': 195005, 'tax': 19500, 'total': 214505})
    rd['blocks'][1]['rows'][1] = '|RH ﾌｪﾝﾀﾞ ﾗｲﾅ|取替|53875-11111||1|3005|||'
    rd['pages']['2']['parts'] = 33005
    rd['totals']['parts'] = 95005
    r = run(rd)
    assert has(r['warn'], '切り捨て'), (r['fail'], r['warn'])
    assert r['settings']['tax_round'] == '切り捨て'


def test_extra_column():
    rd = copy.deepcopy(BASE)
    rd['blocks'][0]['rows'][2] = '|ﾊﾞﾝﾊﾟ ﾋﾟｰｽ|取替|52161-11111||1|5000||||メモ'  # '|' が 1 個多く comment が 11 列目に落ちた
    r = run(rd)
    assert has(r['fail'], '列が 10 個を超えている'), r['fail']


def test_side_conflict():
    rd = copy.deepcopy(BASE)
    rd['blocks'][1]['rows'][1] = '|LH ﾌｪﾝﾀﾞ ﾗｲﾅ|取替|53875-11111||1|3000|||'
    r = run(rd)
    assert has(r['warn'], '見出し【右 フロントフェンダー】は 右 なのに行は 左'), r['warn']


def test_same_pn_both_sides():
    rd = copy.deepcopy(BASE)
    rd['blocks'][1]['rows'].append('|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ|取替|53811-11111|2.00|1|30000|16000||')
    rd['pages']['2'].update({'rows': 3, 'parts': 63000, 'wage': 32000})
    rd['totals'].update({'parts': 125000, 'wage': 44000, 'taxable': 241000, 'tax': 24100, 'total': 265100})
    r = run(rd)
    assert has(r['warn'], '左右両方の行にある'), r['warn']
    assert not r['fail'], r['fail']
    rd2 = copy.deepcopy(BASE)  # クリップは左右で同じ品番でも警告しない
    rd2['blocks'][1]['rows'].append('|LH ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ|取替|90467-11111||2|1000|||')
    rd2['pages']['2'].update({'rows': 3, 'parts': 34000})
    rd2['totals'].update({'parts': 96000, 'taxable': 196000, 'tax': 19600, 'total': 215600})
    r2 = run(rd2)
    assert not has(r2['warn'], '左右両方'), r2['warn']


def test_wage_round_100():
    rd = copy.deepcopy(BASE)
    rd['blocks'][0]['rows'][0] = '|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ|取替|52119-11111|1.55|1|50000|12400||'  # 12,400 = 100 円丸め（10 円なら 12,400 でも同じ）→ 1.53 で差を出す
    rd['blocks'][0]['rows'][0] = '|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ|取替|52119-11111|1.53|1|50000|12200||'  # 1.53×8000=12,240 → 100 円丸め 12,200
    rd['blocks'][1]['rows'][0] = '|RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ|取替|53811-11111|2.03|1|30000|16200||'  # 16,240 → 16,200
    rd['blocks'][0]['subtotal']['wage'] = 12200; rd['pages']['1']['wage'] = 12200; rd['pages']['2']['wage'] = 16200
    rd['totals'].update({'wage': 28400, 'taxable': 195400, 'tax': 19540, 'total': 214940})
    r = run(rd)
    assert r['settings']['wage_round'] == 100, r['settings']
    assert not r['fail'], r['fail']


def test_wage_mismatch_row():
    rd = copy.deepcopy(BASE)
    rd['blocks'][1]['rows'][0] = '|RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ|取替|53811-11111|2.00|1|30000|15000||'  # 2.00×8000=16,000 なのに 15,000
    rd['pages']['2']['wage'] = 15000
    rd['totals'].update({'wage': 27000, 'taxable': 194000, 'tax': 19400, 'total': 213400})
    r = run(rd)
    assert has(r['warn'], '工賃 15,000 が 指数 2.0'), r['warn']
    rd['blocks'][1]['rows'][0] = '|RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ|取替|53811-11111|2.00|1|30000|15000|*|'  # 手入力工賃の印があれば警告しない
    r = run(rd)
    assert not has(r['warn'], '工賃 15,000'), r['warn']


def test_totals_as_printed_strings():
    rd = copy.deepcopy(BASE)
    rd['totals'] = {'parts': '95,000', 'wage': '28,000', 'paint': '', 'taxable': '195,000', 'tax': '19,500', 'total': '214,500'}
    r = run(rd)
    assert not r['fail'], r['fail']


def test_profile_corrupt_and_atomic(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp(prefix='prof_')
    old = rc.PROFILES
    rc.PROFILES = os.path.join(d, 'factory_profiles.json')
    try:
        assert rc.save_profile(BASE, {'labor_rate': 8000, 'wage_round': 10, 'format': 'B'}).endswith('factory_profiles.json')
        assert rc.load_profiles()['テスト鈑金']['count'] == 1
        rc.save_profile(BASE, {'labor_rate': 8000, 'wage_round': 10, 'format': 'B'})  # 同じ案件の再実行は件数を増やさない
        assert rc.load_profiles()['テスト鈑金']['count'] == 1
        rd2 = copy.deepcopy(BASE); rd2['est_date'] = '20260902'
        rc.save_profile(rd2, {'labor_rate': 8000, 'wage_round': 10, 'format': 'B'})
        assert rc.load_profiles()['テスト鈑金']['count'] == 2
        with open(rc.PROFILES, 'w', encoding='utf-8') as fh:
            fh.write('{broken')  # 壊れたファイルは退避される（消えない）
        assert rc.load_profiles() == {}
        rc.save_profile(BASE, {'labor_rate': 8000, 'wage_round': 10, 'format': 'B'})
        assert rc.load_profiles()['テスト鈑金']['count'] == 1
        assert any(f.startswith('factory_profiles.json.corrupt-') for f in os.listdir(d)), os.listdir(d)
        assert not os.path.exists(rc.PROFILES + '.lock')
    finally:
        rc.PROFILES = old
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_manual_rows_mode():
    rd = copy.deepcopy(BASE)
    rd['paint'] = {}
    rd['blocks'][1]['rows'].append('|塗装材料費用|取替||||40000||M|')
    rd['pages']['2'].update({'rows': 3, 'parts': 73000})
    rd['totals'] = {'parts': 135000}
    r = run(rd)
    assert r['settings'].get('manual_rows_mode') is True, r['settings']
    assert not r['fail'], r['fail']
    rd['paint'] = {'total': 40000}  # 明細にもあり paint にもある → 二重計上の疑い
    r = run(rd)
    assert has(r['warn'], '二重計上'), r['warn']
    rd2 = copy.deepcopy(BASE)  # 費用らしい手入力行 + expenses あり → 二重計上の疑い（paint は関係ない）
    rd2['blocks'][1]['rows'].append('|写真代|取替||||800||M|')
    rd2['pages']['2'].update({'rows': 3, 'parts': 33800}); rd2['totals'].update({'parts': 95800, 'taxable': 195800, 'tax': 19580, 'total': 215380})
    r2 = run(rd2)
    assert has(r2['warn'], '費用らしい手入力行') and not r2['settings'].get('manual_rows_mode'), (r2['warn'], r2['settings'])


def test_issuer_key():
    assert rc.issuer_key(BASE['issuer']) == 'テスト鈑金'
    assert rc.issuer_key('テスト鈑金（テスト市南区…）') == 'テスト鈑金'
    assert rc.issuer_key('株式会社テスト 狭山工場 埼玉県狭山市1-1 TEL 04-0000-0000') == '株式会社テスト 狭山工場'
    assert rc.issuer_key('') == ''


def test_format_detect():
    rd = copy.deepcopy(BASE)
    rd['blocks'][0]['rows'][0] = '0010|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ|取替|52119-11111|1.50|1|50000|12000|$|'
    r = run(rd)
    assert has(r['warn'], 'reading.format=B だが特徴からは書式 A'), r['warn']
    rd['format'] = 'A'
    assert not has(run(rd)['warn'], '書式'), run(rd)['warn']
    rd['blocks'][0]['rows'][0] = '0010|ﾌﾛﾝﾄﾊﾞﾝﾊﾟ ｶﾊﾞｰ|取替|52119-11111|1.50|1|50000|12000||'  # コード列だけなら A/B どちらでもよい
    rd['format'] = 'B'
    assert not has(run(rd)['warn'], '書式'), run(rd)['warn']


def test_page_missing_rows():
    rd = copy.deepcopy(BASE)
    rd['pages']['3'] = {'rows': 1, 'parts': 100}
    r = run(rd)
    assert has(r['fail'], 'ページ 3'), r['fail']


def test_target_total_skips_totals():
    rd = copy.deepcopy(BASE)
    rd['target_total'] = '999,999'
    rd['totals'] = {'parts': 95000, 'wage': 28000}
    r = run(rd)
    assert not r['fail'], r['fail']
    assert has(r['note'], 'target_total'), r['note']


def test_wage_unknown_row_still_checks_totals():
    """工賃も指数も無い行があっても、合計欄どうしの整合（課税小計 + 消費税 + 非課税 = 御見積額）は検算する"""
    rd = copy.deepcopy(BASE)
    rd['blocks'][1]['rows'][1] = '|RH ﾌｪﾝﾀﾞ ﾗｲﾅ|脱着|53875-11111||1||||'  # 取替 → 脱着（部品代も工賃も指数も無い）
    rd['pages']['2'] = {'rows': 2, 'parts': 30000, 'wage': 16000}
    rd['totals'] = {'parts': 92000, 'wage': 28000, 'paint': 40000, 'material': 22000, 'expense': 10000,
                    'taxable': 192000, 'tax': 19200, 'total': 211200}
    r = run(rd)  # ここで NameError で落ちないこと
    assert has(r['warn'], '未検算'), r['warn']
    assert not r['fail'], r['fail']


def test_wage_unknown_row_catches_total_mismatch():
    """未検算でも、合計欄の御見積額が課税小計 + 消費税 + 非課税と合わなければ FAIL にする"""
    rd = copy.deepcopy(BASE)
    rd['blocks'][1]['rows'][1] = '|RH ﾌｪﾝﾀﾞ ﾗｲﾅ|脱着|53875-11111||1||||'
    rd['totals'] = {'parts': 92000, 'wage': 28000, 'paint': 40000, 'material': 22000, 'expense': 10000,
                    'taxable': 192000, 'tax': 19200, 'total': 999999}
    r = run(rd)
    assert has(r['fail'], '御見積額'), r['fail']



def test_paint_other_outside_paint_total():
    """印字の塗装工賃計は追加項目（プライマー塗装など paint.other。コグニ印刷の「追加塗装費用計」）を含まない。
    塗装計（材料込）・課税小計は 塗装工賃計 + 追加項目 + 材料代で見て、材料代の割合は追加項目を除いて出す（2026-09-14 C-HR）"""
    rd = copy.deepcopy(BASE)
    rd['paint'] = {'total': 40000, 'material': 12400, 'other': [{'name': 'プライマー塗装', 'index': 1.0, 'wage': 8000}]}   # 12,400 = 40,000 × 31%
    rd['totals'] = {'parts': 95000, 'wage': 28000, 'paint': 40000, 'paint_total': 60400, 'material': 12400, 'expense': 10000,
                    'taxable': 193400, 'tax': 19340, 'total': 212740}
    r = run(rd)
    assert not r['fail'], r['fail']
    assert r['settings'].get('material_rate') == 31.0, r['settings']
    rd2 = copy.deepcopy(rd); rd2['paint']['other'] = []      # 追加項目を写し落とすと塗装計・課税小計で落ちる
    assert has(run(rd2)['fail'], '塗装計（材料込）'), run(rd2)['fail']
    rd3 = copy.deepcopy(rd)   # 塗装工賃計を書かず塗装行だけ写したときも、内板骨格塗装（paint.frame の位置ごとの工賃）と追加項目を 1 回ずつ足す
    rd3['paint'] = {'lines': [{'name': '加算基礎数値', 'index': 4.0, 'wage': 32000}], 'frame': {'engine_room': {'option': 2, 'index': 1.0, 'wage': 8000}},
                    'material': 12400, 'other': [{'name': 'プライマー塗装', 'index': 1.0, 'wage': 8000}]}
    r3 = run(rd3)
    assert not r3['fail'] and r3['settings'].get('material_rate') == 31.0, (r3['fail'], r3['settings'])
    rd5 = copy.deepcopy(rd)   # パネル別（total なし）でも付加塗装（ドアサッシュ等）を塗装工賃に入れる
    rd5['paint'] = {'panels': [{'code': '2300', 'name': 'x', 'method': '取替', 'wage': 30000}], 'door_sash': {'count': 2, 'wage': 2000},
                    'frame': {'engine_room': {'option': 2, 'wage': 8000}}, 'material': 12400, 'other': [{'name': 'プライマー塗装', 'wage': 8000}]}
    r5 = run(rd5)
    assert not r5['fail'] and r5['settings'].get('material_rate') == 31.0, (r5['fail'], r5['settings'])
    rd6 = copy.deepcopy(rd)   # バンパだけの詳細塗装（panels: [] + bumper_*。生成器と同じく許可されたキーだけの形）も工賃を数える
    rd6['paint'] = {'panels': [], 'bumper_front': {'method': '新品', 'wage': 40000}, 'material': 12400}
    rd6['totals'] = {'parts': 95000, 'wage': 28000, 'paint': 40000, 'paint_total': 52400, 'material': 12400, 'expense': 10000,
                     'taxable': 185400, 'tax': 18540, 'total': 203940}
    r6 = run(rd6)
    assert not r6['fail'], r6['fail']


def test_expense_on_both_columns_profile():
    """部品と工賃の両方に金額がある費用（同じ名前で 2 行に写す）は 'parts+wage' として工場プロファイルと比べる（2026-09-14 C-HR）"""
    ex = [{'name': 'モデリスタ サイドスカート', 'amount': 36000, 'in': '部品計'}, {'name': 'モデリスタ サイドスカート', 'amount': 5000, 'in': '作業計'},
          {'name': '内張り費用', 'amount': 5000, 'in': '作業計'}]
    k = rc._expense_kinds(ex)
    assert k == {'モデリスタサイドスカート': 'parts+wage', '内張り費用': 'wage'}, k
    import json, shutil, tempfile
    d = tempfile.mkdtemp(prefix='prof_')
    old = rc.PROFILES
    rc.PROFILES = os.path.join(d, 'factory_profiles.json')
    try:   # 旧形式のプロファイル（同名 2 行を 'wage' 1 つで保存）でも WARN にしない。区分が入れ替わったときは従来どおり WARN
        json.dump({'テスト鈑金': {'expense_in': {'モデリスタサイドスカート': 'wage', '内張り費用': 'parts'}}}, open(rc.PROFILES, 'w', encoding='utf-8'), ensure_ascii=False)
        rd = copy.deepcopy(BASE); rd['expenses'] = ex
        w = run(rd)['warn']
        assert not any('モデリスタサイドスカート' in x for x in w), w
        assert any('内張り費用' in x for x in w), w
        rd5 = copy.deepcopy(rd); rd5['expenses'] = [ex[1], dict(ex[0], **{'in': '諸費用計', 'taxfree': True})]   # 過去 wage → 今回 taxfree+wage は区分の変更なので WARN
        assert rc._expense_kinds(rd5['expenses'])['モデリスタサイドスカート'] == 'taxfree+wage'
        assert any('モデリスタサイドスカート' in x for x in run(rd5)['warn']), run(rd5)['warn']
        json.dump({'テスト鈑金': {'expense_in': {'モデリスタサイドスカート': 'parts+wage'}}}, open(rc.PROFILES, 'w', encoding='utf-8'), ensure_ascii=False)
        rd4 = copy.deepcopy(rd); rd4['expenses'] = [ex[0]]      # 過去 parts+wage → 今回 parts だけ（工賃の行の写し漏れの疑い）は WARN
        assert any('モデリスタサイドスカート' in x for x in run(rd4)['warn']), run(rd4)['warn']
        rc.save_profile(rd4, {'labor_rate': 8000, 'wage_round': 10, 'format': 'B'})     # 片方だけの今回で 'parts+wage' を上書きしない
        assert rc.load_profiles()['テスト鈑金']['expense_in']['モデリスタサイドスカート'] == 'parts+wage', rc.load_profiles()
    finally:
        rc.PROFILES = old
        shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                fails += 1
                print('FAIL', name, e)
    print('reading_check tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
