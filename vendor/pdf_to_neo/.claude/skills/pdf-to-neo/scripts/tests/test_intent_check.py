# -*- coding: utf-8 -*-
"""できあがった NEO と下書きの意図の突き合わせ（intent_check.py）・名称の短縮・会社名の略しの単体テスト
（NEO を作るので ADDATA が要る。無い PC では NEO を作るテストだけ飛ばす）
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_intent_check.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import skill_env  # noqa: E402

skill_env.apply()
sys.path.insert(0, os.path.join(skill_env.FILES, 'claude_neo_pipeline'))
import draft_estimate as de  # noqa: E402
import intent_check as ic  # noqa: E402

VEH = {'model_code': 'JF1', 'serial_no': 'JF1-0000001', 'desig': '17075', 'category': '0061', 'reg_date': 'H28.10', 'color_code': 'YR586P'}


def test_shorten_name_and_company():
    """名称欄 24 バイト・顧客名 30 バイトに入らないときの短縮（2026-09-13 ベンツの作業名・ランクルの顧客名）"""
    assert de.shorten_name('ﾌｭｰｴﾙﾀﾝｸ脱着') == 'ﾌｭｰｴﾙﾀﾝｸ脱着'                         # 入るならそのまま
    s = de.shorten_name('低温回路クーラントの排出、注入、調整（システムの作業時）')
    assert de._cp932_len(s) <= 24 and s.startswith('低温回路ｸｰﾗﾝﾄ') and '(' not in s, s  # 括弧書きから外す
    assert de.shorten_name('ｼｰﾘﾝｸﾞﾘﾝｸﾞ, SPL. S') == 'ｼｰﾘﾝｸﾞﾘﾝｸﾞ, SPL. S'
    assert de.abbr_company('サンプルオートサービス東京株式会社') == 'サンプルオートサービス東京(株)'
    assert de.abbr_company('山田 太郎') == '山田 太郎'
    f = de.abbr_company('サンプル自動車販売（株）東京中央店 03-1234-5678')
    assert de._cp932_len(f) <= 30 and f.endswith('03-1234-5678'), f       # 電話番号は残して名前の側を詰める


def _reading(rows, **kw):
    rd = {'source': 't', 'issuer': 't', 'est_date': '20260913', 'format': 'F', 'vehicle': dict(VEH), 'customer': {}, 'insurance': {},
          'labor_rate': 8000, 'blocks': [{'title': '', 'page': 1, 'rows': rows}], 'paint': {}, 'expenses': [], 'totals': {}}
    rd.update(kw)
    return rd


def test_long_manual_names_are_shortened_with_review():
    """手入力行の長い名称: neo_name があればそれ、無ければ自動で短くして、印字の全文を確認箇所に残す"""
    if not _addata_ok():
        print('   skip test_long_manual_names_are_shortened_with_review（この PC の ADDATA で J87 が引けない）')
        return
    e = de.Drafter(_reading([{'name': '高圧燃料ポンプの高圧ラインの交換（点検後の作業）', 'method': '', 'wage': 8000, 'manual': True},
                             {'name': '12V車両電源回路のバッテリアースラインの取外し、接続', 'neo_name': '12Vﾊﾞｯﾃﾘｱｰｽ脱着', 'method': '', 'wage': 4000, 'manual': True}],
                            customer={'name': 'サンプルオートサービス東京株式会社'})).build()
    a, b = e['items']
    assert de._cp932_len(a['name']) <= 24 and a.get('_full_name'), a
    assert b['name'] == '12Vﾊﾞｯﾃﾘｱｰｽ脱着' and b.get('_full_name'), b
    kinds = [(r['kind'], r['level']) for r in e['_review']]
    assert ('名称の短縮', '要確認') in kinds and ('名称の短縮', '判断') in kinds, kinds
    assert e['customer']['name'] == 'サンプルオートサービス東京(株)', e['customer']
    try:
        de.Drafter(_reading([{'name': 'x', 'neo_name': 'とても長い名前でNEOの名称欄に入らない', 'method': '', 'wage': 1000, 'manual': True}])).build()
    except ValueError as ex:
        assert 'neo_name' in str(ex), ex
    else:
        raise AssertionError('24 バイトを超える neo_name を通してしまった')


def _addata_ok() -> bool:
    """J87（N BOX）の車両が引ける ADDATA があるか。無い PC だけ NEO を作るテストを飛ばす（それ以外の失敗は隠さない。Codex 指摘）"""
    root = os.environ.get('ADDATA_ROOT') or ''   # skill_env.apply() が設定する
    return bool(root) and os.path.isdir(os.path.join(root, 'J', 'J87'))   # 例外は捕まえない（Drafter の不具合を skip で隠さない）


def _build(est):
    from estimate_to_neo import NeoBuilder
    neo, _rep = NeoBuilder().build(est, est['vehicle'], hints=est.get('hints'), labor_rate=est.get('labor_rate'), est_date=est.get('est_date'), insurance=est.get('insurance') or {})
    t = tempfile.NamedTemporaryFile(delete=False, suffix='.neo'); t.write(neo); t.close()
    return t.name


def test_intent_check_finds_hard_and_soft_differences():
    """できあがった NEO が意図どおりなら差 0。数量・金額を変えた意図と比べると hard、欄で切れた名称は soft。英字の品名の末尾 S は ｽ に化けない"""
    if not _addata_ok():
        print('   skip test_intent_check_finds_hard_and_soft_differences（この PC の ADDATA で J87 が引けない）')
        return
    e = de.Drafter(_reading(['0178|Fｲﾝﾅﾌｪﾝﾀﾞｸﾘｯﾌﾟ|取替|||1|1850|||',
                             {'name': 'ｼｰﾘﾝｸﾞﾘﾝｸﾞ, SPL. S', 'method': '取替', 'qty': 1, 'price': 240, 'manual': True},
                             {'name': 'ﾎｰS', 'method': '取替', 'qty': 1, 'price': 100, 'manual': True}])).build()
    neo = _build(e)
    try:
        r = ic.check(e, neo)
        soft_names = [x for x in r['soft'] if '名称' in x['text']]
        assert not r['hard'], r['hard']
        assert len(soft_names) == 1 and 'ﾎｰS' in soft_names[0]['text'] and 'ﾎｰｽ' in soft_names[0]['text'], soft_names  # OCR の読み違い（半角カナの末尾 S）だけ ｽ に直る
        e2 = copy.deepcopy(e)
        e2['items'][0]['qty'] = 3
        e2['items'][1]['price'] = 999
        r2 = ic.check(e2, neo)
        what = ' '.join(x['text'] for x in r2['hard'])
        assert '数量' in what and '部品金額' in what, r2['hard']
        e7 = copy.deepcopy(e); e7['items'][1]['price'] = '240円'; e7['items'][2]['price'] = '¥1,000'   # 金額の文字列も生成器と同じく読む（'¥1,000' ≠ NEO の 100 → hard）
        what7 = ' '.join(x['text'] for x in ic.check(e7, neo)['hard'])
        assert '部品金額' in what7 and "1000" in what7 and '240' not in what7, what7
        e9 = copy.deepcopy(e); e9['items'][0]['code'] = 178        # 数値で書いた部品コードも生成器と同じく 4 桁にそろえて比べる（'0178'）
        assert not ic.check(e9, neo)['hard'], ic.check(e9, neo)['hard']
        e10 = copy.deepcopy(e); e10['items'][1]['price'] = 240.9; e10['items'][2]['price'] = '1e2'   # 円未満・指数表記は読めない値（切り捨てて一致にしない）
        assert sum('読めない' in x['text'] for x in ic.check(e10, neo)['hard']) == 2, ic.check(e10, neo)['hard']
        e6 = copy.deepcopy(e); e6['items'][0]['reserve'] = True             # 保留のつもりの行が NEO で保留になっていなければ hard
        assert any('保留' in x['text'] for x in ic.check(e6, neo)['hard'])
        e4 = copy.deepcopy(e)
        e4['items'][1]['manual'] = 'true'; e4['items'][0]['manual'] = 'false'   # 文字列の真偽値も正しく読む
        e4['items'][2]['parts_price'] = 555                                     # parts_price は price より優先
        what4 = ' '.join(x['text'] for x in ic.check(e4, neo)['hard'])
        assert '部品金額' in what4 and '部品コード' not in what4, what4
        e3 = copy.deepcopy(e)
        e3['items'][1]['name'] = 'ｼｰﾘﾝｸﾞﾘﾝｸﾞ, SPL. S とても長い名前の続き'
        r3 = ic.check(e3, neo)
        assert any(x['kind'] in ('欄で切れた', '名称が変わった') for x in r3['soft']), r3['soft']
        e11 = copy.deepcopy(e3); e11['vehicle']['generic'] = True; e11['items'][1].pop('manual', None)   # 汎用車種は manual の印が無くても名称を見る
        assert any('とても長い' in x['text'] for x in ic.check(e11, neo)['soft']), ic.check(e11, neo)['soft']
    finally:
        os.unlink(neo)



def test_intent_check_with_recycle_rows():
    """リサイクル部品に置き換えた行は生成器が末尾へ動かす。それ以外の行は並び順で突き合わせ、取り違えは hard で見つける"""
    est = {'source': 't', 'est_date': '20260913', 'vehicle': dict(VEH), 'customer': {}, 'insurance': {}, 'labor_rate': 8000, 'paint': {}, 'expenses': [], 'totals': {},
           'items': [{'code': '0010', 'name': 'Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'method': '取替', 'qty': 1, 'price': 50000, 'wage': 8000,
                      'recycle': {'name': 'ﾘｻｲｸﾙ Fﾊﾞﾝﾊﾟﾌｪｲｽ', 'price': 20000, 'stock_price': 20000}},
                     {'code': '0600', 'name': 'ﾌｰﾄﾞ', 'method': '取替', 'qty': 1, 'price': 30000},
                     {'name': '手入力', 'method': '', 'qty': 1, 'price': 1000, 'manual': True}]}
    if not _addata_ok():
        print('   skip test_intent_check_with_recycle_rows（この PC の ADDATA で J87 が引けない）')
        return
    neo = _build(est)
    try:
        r = ic.check(est, neo)
        assert not r['hard'] and r['rows'] == 2, r
        e2 = copy.deepcopy(est); e2['items'][1]['price'] = 31000
        assert any('部品金額' in x['text'] for x in ic.check(e2, neo)['hard'])
        e3 = copy.deepcopy(est); e3['items'][0]['recycle']['price'] = 25000   # リサイクル行の金額も突き合わせる
        assert any('リサイクル部品の金額' in x['text'] for x in ic.check(e3, neo)['hard'])
        e8 = copy.deepcopy(est); e8['items'][0]['recycle']['price'] = '二万円'   # 読めない金額は hard（黙って飛ばさない）
        assert any('読めない' in x['text'] for x in ic.check(e8, neo)['hard'])
        e5 = copy.deepcopy(est); e5['items'][0]['code'] = '0600'; e5['items'][0]['price'] = 51000  # リサイクル行の部品コード・退避した元の金額も見る
        what5 = ' '.join(x['text'] for x in ic.check(e5, neo)['hard'])
        assert 'リサイクル部品の部品コード' in what5 and '退避した元の部品' in what5, what5
        e4 = copy.deepcopy(est); e4['items'][1]['wage'] = 0                  # 工賃 0 のつもりの行に工賃が入っていれば hard
        assert any('工賃' in x['text'] for x in ic.check(e4, neo)['hard'])
    finally:
        os.unlink(neo)


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
    print('intent_check tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
