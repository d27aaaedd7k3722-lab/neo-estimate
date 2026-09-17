# -*- coding: utf-8 -*-
"""reading_check.py の単体テスト（架空の見積。ADDATA 不要）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_reading_check.py
"""
from __future__ import annotations

import copy
import os
import re
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
    rd['totals'] = {'parts': 92000, 'wage': 36000, 'paint': 40000, 'material': 22000, 'expense': 10000,
                    'taxable': 192000, 'tax': 19200, 'total': 211200}  # 工賃計が明細より多い = 空欄の行に工賃がある書式
    r = run(rd)  # ここで NameError で落ちないこと
    assert has(r['warn'], '未検算'), r['warn']
    assert not r['fail'], r['fail']


def test_wage_unknown_row_is_zero_when_printed_total_matches():
    """工賃も指数も無い行があっても、印字の工賃計が明細の工賃の合計と一致するなら空欄は 0 円（下書きと同じ判断）。
    未検算にせず課税小計まで検算する（2026-09-16 シエンタ: 空欄に標準指数が入って +106,400 円になっていた）"""
    rd = copy.deepcopy(BASE)
    rd['blocks'][1]['rows'][1] = '|RH ﾌｪﾝﾀﾞ ﾗｲﾅ|脱着|53875-11111||1||||'
    rd['pages']['2'] = {'rows': 2, 'parts': 30000, 'wage': 16000}
    rd['totals'] = {'parts': 92000, 'wage': 28000, 'paint': 40000, 'material': 22000, 'expense': 10000,
                    'taxable': 192000, 'tax': 19200, 'total': 211200}
    r = run(rd)
    assert not has(r['warn'], '未検算') and not r['warn'], r['warn']
    assert has(r['note'], '空欄は 0 円') and has(r['note'], '合計欄 課税小計: 192,000 一致'), r['note']
    assert not r['fail'], r['fail']


def test_paint_lump_in_rows_and_paint_is_counted_once():
    """塗装の一式が明細の手入力行と paint の両方にある reading: 下書きと同じ寄せ方（印字の工賃計で決める）で検算する。
    ここで寄せないと、下書きが直せる案件を紙上検算で止めてしまう（2026-09-16 シエンタ）"""
    rd = copy.deepcopy(BASE)
    rd['paint'] = {'total': 40000}                      # 塗装計の印字は無い
    rd['blocks'][1]['rows'].append('|塗装費用||||||40000|M|')
    rd['pages']['2'] = {'rows': 3, 'parts': 33000, 'wage': 56000}
    rd['totals'] = {'parts': 95000, 'wage': 68000, 'expense': 10000,   # 工賃計は手入力行を含む（28,000 + 40,000）
                    'taxable': 173000, 'tax': 17300, 'total': 190300}
    r = run(rd)
    assert has(r['note'], '塗装計は明細の行で数える'), r['note']
    assert has(r['note'], '合計欄 課税小計: 173,000 一致'), r['note']
    assert not r['fail'], r['fail']
    rd['paint'] = {'total': 40000}                      # 逆: 工賃計が手入力行を含まない = 塗装は paint 側
    rd['totals'].update({'wage': 28000, 'paint': 40000})
    r2 = run(rd)
    assert has(r2['note'], '塗装計は paint で数える'), r2['note']
    assert not r2['fail'], r2['fail']


def test_unit_price_fraction_is_not_a_fail():
    """単価に円未満の端数がある見積（金額が数量で割り切れない行がある）の数円差は、読み取りの誤りではないので止めない。
    2026-09-16 フリード: 部品計 印字 234,627 / 明細 234,629（クリップの単価 154.5 円）"""
    rd = copy.deepcopy(BASE)
    rd['blocks'][0]['rows'][1] = '|ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ|取替|90467-11111||7|5001|||'   # 7 個 5,001 円（単価 714.43…）
    rd['blocks'][0]['subtotal'] = {'rows': 3, 'parts': 60001, 'wage': 12000}
    rd['pages']['1'] = {'rows': 3, 'parts': 60001, 'wage': 12000}
    r = run(rd)   # 印字の部品計 95,000 に対し明細 + 費用は 95,001（差 +1、端数のある行が 1 行）
    assert not r['fail'], r['fail']
    assert has(r['warn'], '単価に円未満の端数'), r['warn']
    # 消費税の丸め判定は**印字の課税小計**で行う（見逃した差でコグニ側の合計を使うと、四捨五入の工場を「切り捨て」と
    # 誤判定して Setting.tx_ArrangeFlag の違う NEO を作ってしまう。レビュー指摘 2026-09-17）
    assert not any('切り捨て' in w for w in r['warn']), r['warn']
    assert r['settings'].get('tax_round') in (None, '四捨五入'), r['settings']
    rd['blocks'][0]['rows'][1] = '|ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ|取替|90467-11111||7|5100|||'   # 差 +100 は端数（1 行なら最大 1 円）では説明できない
    rd['blocks'][0]['subtotal'] = {'rows': 3, 'parts': 60100, 'wage': 12000}
    rd['pages']['1'] = {'rows': 3, 'parts': 60100, 'wage': 12000}
    r2 = run(rd)
    assert has(r2['fail'], '部品計'), r2['fail']
    # 方向が逆（印字のほうが多い）は端数では説明できないので止める
    rd3 = copy.deepcopy(BASE)
    rd3['blocks'][0]['rows'][1] = '|ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ|取替|90467-11111||7|4999|||'
    rd3['blocks'][0]['subtotal'] = {'rows': 3, 'parts': 59999, 'wage': 12000}
    rd3['pages']['1'] = {'rows': 3, 'parts': 59999, 'wage': 12000}
    assert has(run(rd3)['fail'], '部品計'), '印字のほうが多い差を端数として見逃した'
    # 端数のある行が 1 行も無ければ、1 円でも止める
    rd4 = copy.deepcopy(BASE)
    rd4['blocks'][0]['rows'][1] = '|ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ|取替|90467-11111||10|5010|||'   # 10 個 5,010 円（単価 501 円・割り切れる）
    rd4['blocks'][0]['subtotal'] = {'rows': 3, 'parts': 60010, 'wage': 12000}
    rd4['pages']['1'] = {'rows': 3, 'parts': 60010, 'wage': 12000}
    assert has(run(rd4)['fail'], '部品計'), '端数のある行が無いのに見逃した'


def test_material_counts_lines_with_only_material():
    """材料の列だけに金額のある塗装行も材料計に数える（印字と一致するときだけ）。
    2026-09-16 アクセラ: 材料計が 2,000 円足りないまま不合格になり NEO を作れなかった"""
    rd = copy.deepcopy(BASE)
    rd['paint'] = {'total': 40000, 'material': 22000,
                   'lines': [{'name': '塗装一式', 'wage': 40000, 'material': 22000},
                             {'name': 'ｼｮｰﾄﾊﾟｰﾂ', 'material': 1000}, {'name': '写真代', 'material': 1000}]}
    rd['totals'] = dict(BASE['totals'], material=24000, taxable=197000, tax=19700, total=216700)
    r = run(rd)
    assert has(r['note'], '塗装行の材料の合計'), r['note']
    assert has(r['note'], '合計欄 課税小計: 197,000 一致'), r['note']
    assert not r['fail'], r['fail']
    rd['totals'] = dict(BASE['totals'], material=22000)   # 印字の材料計が塗装一式のぶんだけなら触らない
    r2 = run(rd)
    assert not has(r2['note'], '塗装行の材料の合計'), r2['note']


def test_wage_unknown_row_catches_total_mismatch():
    """未検算でも、合計欄の御見積額が課税小計 + 消費税 + 非課税と合わなければ FAIL にする"""
    rd = copy.deepcopy(BASE)
    rd['blocks'][1]['rows'][1] = '|RH ﾌｪﾝﾀﾞ ﾗｲﾅ|脱着|53875-11111||1||||'
    rd['totals'] = {'parts': 92000, 'wage': 28000, 'paint': 40000, 'material': 22000, 'expense': 10000,
                    'taxable': 192000, 'tax': 19200, 'total': 999999}
    r = run(rd)
    assert has(r['fail'], '御見積額'), r['fail']



def test_paint_total_transcribed_with_other():
    """塗装工賃計を追加項目込みで写した形（2026-09-15 N-BOX: パネル・加算基礎・ブース等の内訳 94,210 + アンダーコート 3,680 = 97,890）。
    内訳 + 追加項目とちょうど一致するときは追加項目を足し直さない（足すと塗装計（材料込）・課税小計が 3,680 多く出て、正しい転記を不合格にしていた）。
    C-HR の形（塗装工賃計が追加項目を含まない）は従来どおり足す"""
    rd = copy.deepcopy(BASE)
    rd['paint'] = {'total': 40000, 'material': 12400, 'panels': [{'code': '2300', 'name': 'x', 'method': '取替', 'wage': 30000}],
                   'base': {'index': 0.25, 'wage': 2000}, 'other': [{'name': 'アンダーコート', 'index': 1.0, 'wage': 8000}]}   # 30,000 + 2,000 + 8,000 = 40,000
    rd['totals'] = {'parts': 95000, 'wage': 28000, 'paint': 40000, 'paint_total': 52400, 'material': 12400, 'expense': 10000,
                    'taxable': 185400, 'tax': 18540, 'total': 203940}
    r = run(rd)
    assert not r['fail'], r['fail']
    assert has(r.get('info') or r.get('notes') or r.get('note') or [], '追加項目は足し直さない'), r.keys()
    rd2 = copy.deepcopy(rd)   # 塗装工賃計が追加項目を含まない（C-HR の形）: 32,000 と写し、塗装計（材料込）は 32,000 + 8,000 + 12,400
    rd2['paint']['total'] = 32000
    rd2['totals'] = dict(rd['totals'], paint=32000)
    assert not run(rd2)['fail'], run(rd2)['fail']


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


def _to_tax_included(rd: dict, rate: int = 10) -> dict:
    """税抜の読み取りを『各行の金額まで税込』の印字に戻す（to_tax_excluded の逆。テスト用）。
    非課税の費用は税が乗らないのでそのまま（呼び出し側で戻す）"""
    def up(v):
        return int((int(v) * (100 + rate) + 50) // 100)

    def d(o, keys):
        for k in keys:
            if isinstance((o or {}).get(k), (int, float)) and not isinstance(o[k], bool):
                o[k] = up(o[k])
    for b in rd['blocks']:
        rows = []
        for r in b['rows']:
            if isinstance(r, dict):
                d(r, ('price', 'wage', 'unit'))
                if r.get('comment'):
                    r['comment'] = re.sub(r'(unit\s*[=:]\s*)([\d,]+)', lambda m: m.group(1) + str(up(m.group(2).replace(',', ''))), r['comment'])
            else:
                r = '|'.join(str(up(float(c))) if i in (6, 7) and c.strip() else c for i, c in enumerate(r.split('|')))
            rows.append(r)
        b['rows'] = rows
        d(b.get('subtotal'), ('parts', 'wage'))
    for sub in (rd.get('pages') or {}).values():
        d(sub, ('parts', 'wage'))
    d(rd.get('paint'), ('total', 'material'))
    for k in rc.PAINT_WAGE_KEYS:
        v = (rd.get('paint') or {}).get(k)
        if isinstance(v, dict):
            d(v, ('wage', 'material'))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            rd['paint'][k] = up(v)
    d(rd.get('frame'), ('basic_wage',))
    for it in (rd.get('frame') or {}).get('items') or []:
        d(it, ('wage',))
    d(rd.get('discount'), ('parts', 'wage'))
    for e in rd.get('expenses') or []:
        d(e, ('amount',))
    d(rd, ('labor_rate',))
    for k in list(rd['totals']):
        if k in rc.TAX_TOTALS_KEYS:
            d(rd['totals'], (k,))
    return rd


def test_tax_included_is_found_and_divided():
    """『各行の金額まで税込』で刷られた見積書（judgment_rules 10-4）を、印字の合計欄と明細の積み上げだけで見分けて税抜に直す
    （2026-09-17 トヨタ系 BP の概算見積書。同じ見積が税抜で読めた日と、印字のまま税込で読めた日があった）"""
    printed = _to_tax_included(copy.deepcopy(BASE))
    assert printed['totals']['taxable'] == printed['totals']['total'] == 214500, printed['totals']
    assert printed['totals']['tax'] == 19500 and printed['labor_rate'] == 8800, printed['totals']
    res = run(printed)
    assert any('税込で印字された見積書' in f for f in res['fail']), res['fail']
    assert rc.tax_included_rate(printed) == 10
    n = rc.to_tax_excluded(printed, 10)
    assert n == 24, n   # 明細 7 + ブロック小計 2 + ページ小計 4 + 塗装 2 + 費用 2 + 合計欄 6 + レバーレート 1
    assert printed['totals'] == BASE['totals'], printed['totals']          # tax / total は印字どおり、ほかは税抜に戻る
    assert printed['blocks'] == BASE['blocks'] and printed['pages'] == BASE['pages']
    assert printed['paint'] == BASE['paint'] and printed['expenses'] == BASE['expenses']
    assert printed['labor_rate'] == 8000
    assert not run(printed)['fail'], run(printed)['fail']                  # 直せば紙上検算に通る


def test_tax_excluded_estimate_is_not_touched():
    """ふつうの（税抜で刷られた）見積書を税込と間違えない。積み上げは 御見積額 − 消費税 になる"""
    assert rc.tax_included_rate(copy.deepcopy(BASE)) is None
    assert not run(BASE)['fail'], run(BASE)['fail']
    rd = copy.deepcopy(BASE)   # 合計欄に消費税が無い見積（内税か外税か決められない）は触らない
    rd['totals'] = {k: v for k, v in rd['totals'].items() if k != 'tax'}
    assert rc.tax_included_rate(rd) is None
    rd2 = _to_tax_included(copy.deepcopy(BASE))   # 協定額（税込）は totals の外にあるので割らない
    rd2['target_total'] = 214500
    rc.to_tax_excluded(rd2, 10)
    assert rd2['target_total'] == 214500


def test_tax_included_with_taxfree_expense():
    """非課税の費用（リサイクル料など）は税が乗っていないので、税込の見積でも割らない（割ると御見積額が合わなくなる）"""
    rd = copy.deepcopy(BASE)
    rd['expenses'] = rd['expenses'] + [{'name': 'リサイクル料金', 'amount': 5000, 'in': '非課税'}]
    rd['totals'] = dict(rd['totals'], total=219500)
    assert not run(rd)['fail'], run(rd)['fail']
    printed = _to_tax_included(copy.deepcopy(rd))
    printed['expenses'][-1]['amount'] = 5000      # 非課税の費用は税が乗らない（印字も 5,000 のまま）
    printed['totals']['total'] = 219500
    assert rc.tax_included_rate(printed) == 10
    rc.to_tax_excluded(printed, 10)
    assert printed['expenses'][-1]['amount'] == 5000, printed['expenses'][-1]   # 割ってはいけない
    assert not run(printed)['fail'], run(printed)['fail']


def test_tax_included_divides_discount_frame_unit_and_bumper_base():
    """値引き・内板骨格・単価・バンパ加算基礎も税抜に直す（どれか 1 つでも残ると課税小計がずれるか、
    バンパ加算基礎のように**黙って 1.1 倍の工賃で NEO に載る**）"""
    rd = copy.deepcopy(BASE)
    rd['discount'] = {'parts': -10000, 'wage': 0}
    rd['frame'] = {'basic_wage': 12000, 'items': [{'code': '1400', 'rank': 'A', 'wage': 8000}]}
    rd['blocks'][0]['rows'][1] = {'name': 'ﾊﾞﾝﾊﾟ ｸﾘｯﾌﾟ', 'method': '取替', 'parts_no': '90467-11111',
                                  'qty': 10, 'price': 5000, 'unit': 500, 'comment': 'unit=500'}
    rd['paint'] = dict(rd['paint'], bumper_front={'index': 2.0, 'wage': 16000}, bumper_base={'index': 1.0, 'wage': 8000})
    rd['totals'] = dict(rd['totals'], taxable=205000, tax=20500, total=225500)
    assert not run(rd)['fail'], run(rd)['fail']
    printed = _to_tax_included(copy.deepcopy(rd))
    assert rc.tax_included_rate(printed) == 10, run(printed)['fail']
    rc.to_tax_excluded(printed, 10)
    assert printed['discount'] == {'parts': -10000, 'wage': 0}, printed['discount']
    assert printed['frame'] == rd['frame'], printed['frame']
    assert printed['blocks'][0]['rows'][1]['unit'] == 500, printed['blocks'][0]['rows'][1]
    assert printed['blocks'][0]['rows'][1]['comment'] == 'unit=500', printed['blocks'][0]['rows'][1]
    assert printed['paint']['bumper_base'] == {'index': 1.0, 'wage': 8000}, printed['paint']
    assert not run(printed)['fail'], run(printed)['fail']


def test_tax_included_needs_a_clean_reading():
    """転記の誤りがある読み取りは、積み上げがたまたま御見積額と揃っても直さない
    （黙って全金額を 1/1.1 にすると、印字と 9% 違う NEO を作ってしまう）"""
    rd = _to_tax_included(copy.deepcopy(BASE))
    rd['blocks'][1]['rows'].append('|ﾌｪﾝﾀﾞ ﾓｰﾙ|取替|75301-11111||1|19500|||')  # 二重に写した行
    assert rc.tax_included_rate(rd) is None, run(rd)['fail']                   # 積み上げが御見積額と合わないので判定自体しない
    rd1 = _to_tax_included(copy.deepcopy(BASE))       # 判定は当たるが、ほかに FAIL がある読み取りは直さない
    rd1['pages']['1']['parts'] += 1000
    assert any('税込で印字された見積書' in f for f in run(rd1)['fail']), run(rd1)['fail']
    why: list = []
    assert rc.tax_included_rate(rd1, why) is None
    assert why and 'ほかにも紙上検算の FAIL' in why[0], why
    rd2 = _to_tax_included(copy.deepcopy(BASE))          # 合計欄に突き合わせる印字が無い読み取りも直さない
    rd2['totals'] = {'tax': 19500, 'total': 214500}
    del rd2['pages']
    for b in rd2['blocks']:
        b.pop('subtotal', None)
    assert rc.tax_included_rate(rd2) is None
    rd3 = _to_tax_included(copy.deepcopy(BASE), 8)       # 8% は自動で直さない（生成器の消費税は 10% 固定）
    rd3['totals'].update({'tax_rate': 8, 'tax': 15889, 'total': 214500})
    why = []
    assert rc.tax_included_rate(rd3, why) is None
    assert not why or '10% 固定' in why[0], why


def test_tax_included_rounding_slack():
    """割り戻しの丸めで合計欄と積み上げが数円ずれても止めない（読み取りは 1 円も間違っていない）"""
    net = [3105 + i * 7 for i in range(30)]
    up = (lambda v: int((v * 110 + 50) // 100))
    P = sum(up(v) for v in net)
    rd = {'issuer': 'テスト鈑金', 'format': 'B', 'labor_rate': 8800, 'vehicle': {'model_code': 'X'},
          'blocks': [{'title': '明細', 'rows': [f'|部品{i:02d}|取替|9000-{i:04d}||1|{up(v)}|||' for i, v in enumerate(net)]}],
          'paint': {}, 'expenses': [],
          'totals': {'parts': P, 'wage': 0, 'taxable': P, 'tax': (P * 10 + 55) // 110, 'total': P}}
    assert rc.tax_included_rate(rd) == 10, run(rd)['fail']
    rc.to_tax_excluded(rd, 10)
    rd['tax_included'] = 10
    res = run(rd)
    assert not res['fail'], res['fail']
    assert any('割り戻した丸めの差' in w for w in res['warn']), res['warn']


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
