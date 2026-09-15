# -*- coding: utf-8 -*-
"""部品名の照合の単体テスト（2026-09-14 スペーシアの見積で外れた形）: 「名詞 修飾」の語順入替・言い換えの正規化・小物の判定・
金額から数量を推す規則の歯止め・左右の無い小物が直前の主作業の左右を引き継ぐこと・未照合行の候補。ADDATA に S89 が無い PC では車種を使うテストを飛ばす
    PYTHONIOENCODING=utf-8 python .claude/skills/pdf-to-neo/scripts/tests/test_name_order.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import skill_env  # noqa: E402

skill_env.apply()
import draft_estimate as de  # noqa: E402
from estimate_to_neo import AddataParts  # noqa: E402

# 型式指定・類別は車種の識別（個人情報ではない）。車台番号は架空
S89 = {'model_code': '5AA-MK94S', 'serial_no': 'MK94S-300001', 'desig': '20824', 'category': '0001', 'reg_date': 'R7.12', 'color_code': 'WBW'}
READING = {'source': 't', 'issuer': 't', 'est_date': '20260823', 'format': 'B', 'vehicle': dict(S89), 'customer': {}, 'insurance': {}, 'labor_rate': 7620,
           'paint': {}, 'expenses': [], 'totals': {}}


class _Names(AddataParts):   # ADDATA を読まずに名前の処理だけ使う
    def __init__(self):  # noqa: D107
        pass


def _s89_available() -> bool:
    try:
        return de.Drafter(dict(READING, blocks=[])).car.get('CarCode') == 'S89'
    except Exception:  # noqa: BLE001  ADDATA に車種が無い・読めない
        return False


def _draft(rows: list) -> dict:
    return de.Drafter(dict(READING, blocks=[{'title': '鈑金・塗装', 'rows': rows}])).build()


def test_reordered_names():
    rn = AddataParts.reordered_names
    assert rn('ｸﾞﾘﾙ ﾗｼﾞｴｰﾀ') == ['ﾗｼﾞｴｰﾀ ｸﾞﾘﾙ'], rn('ｸﾞﾘﾙ ﾗｼﾞｴｰﾀ')
    assert rn('グリル ラジエータ') == ['ﾗｼﾞｴｰﾀ ｸﾞﾘﾙ']                      # 全角も半角にしてから
    assert rn('ｶﾞｰﾆｯｼｭ ｶｳﾘﾝｸﾞﾄｯﾌﾟ ｾﾝﾀ')[0] == 'ｶｳﾘﾝｸﾞﾄｯﾌﾟ ｾﾝﾀ ｶﾞｰﾆｯｼｭ'
    assert rn('LH ﾊﾟﾈﾙ ﾌﾛﾝﾄﾌｪﾝﾀﾞ') == ['左ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ']                 # 先頭の左右は動かさない
    assert rn('ｸﾘｯﾌﾟ') == [] and rn('左 ｸﾘｯﾌﾟ') == []                      # 1 語なら並べ替えない
    assert rn('ｸﾘｯﾌﾟ ﾓｰﾙﾃﾞｨﾝｸﾞ NO.2')[0] == 'ﾓｰﾙﾃﾞｨﾝｸﾞ ｸﾘｯﾌﾟ NO.2'        # 末尾の NO.2 は末尾のまま
    assert rn('ｸﾘｯﾌﾟ NO.2') == [] and rn('ﾎｰｽ ASSY') == []                  # 2 語で末尾が NO.2・ASSY なら並べ替えない
    assert rn('ﾓｰﾙ ﾙｰﾌ 左') == ['左ﾙｰﾌ ﾓｰﾙ']                                   # 末尾の左右は先頭へ
    assert rn('ﾓｰﾙ ﾄﾞｱ (ﾌﾛﾝﾄ ﾛｱ)') == ['ﾄﾞｱ ﾓｰﾙ (ﾌﾛﾝﾄ ﾛｱ)']                     # 括弧は空白入りでも 1 つのまま末尾へ
    assert rn('ﾌﾞﾗｹｯﾄ NO.2 ﾊﾞﾝﾊﾟ') == ['ﾊﾞﾝﾊﾟ ﾌﾞﾗｹｯﾄ NO.2'] and rn('1 ｸﾞﾘﾙ ﾗｼﾞｴｰﾀ') == ['ﾗｼﾞｴｰﾀ ｸﾞﾘﾙ']
    assert rn('ｸﾘｯﾌﾟ ×10') == [] and rn('ﾎｰｽ SUB ASSY') == [] and rn('RF ﾓｰﾙ ﾄﾞｱ') == ['RF ﾄﾞｱ ﾓｰﾙ']
    assert rn('Rr ｶﾊﾞｰ ﾊﾞﾝﾊﾟ') == ['Rr ﾊﾞﾝﾊﾟ ｶﾊﾞｰ'] and rn('LHﾊﾟﾈﾙ ﾌｪﾝﾀﾞ') == ['左ﾌｪﾝﾀﾞ ﾊﾟﾈﾙ']
    assert rn('Rrｶﾊﾞｰ ﾊﾞﾝﾊﾟ') == ['Rr ﾊﾞﾝﾊﾟ ｶﾊﾞｰ'] and rn('RFﾓｰﾙ ﾄﾞｱ') == ['RF ﾄﾞｱ ﾓｰﾙ'] and rn('LED ﾗﾝﾌﾟ') == ['ﾗﾝﾌﾟ LED']
    assert rn('Fﾓｰﾙ ﾄﾞｱ') == ['Fﾄﾞｱ ﾓｰﾙ'] and rn('Lﾊﾟﾈﾙ ﾌｪﾝﾀﾞ') == ['Lﾌｪﾝﾀﾞ ﾊﾟﾈﾙ'] and rn('Rﾊﾟﾈﾙ ﾊﾞﾝﾊﾟ') == ['Rﾊﾞﾝﾊﾟ ﾊﾟﾈﾙ']   # くっついた 1 文字は置き換えずに先頭へ
    assert rn('R/H ﾊﾟﾈﾙ ﾌｪﾝﾀﾞ') == ['右ﾌｪﾝﾀﾞ ﾊﾟﾈﾙ'] and AddataParts._side_of('R/H ﾘﾔﾌｪﾝﾀﾞｰ') == 'R' and AddataParts._side_of('L/H ﾄﾞｱ') == 'L'
    assert AddataParts._fr_word('ﾓｰﾙ ﾄﾞｱ (ﾌﾛﾝﾄ ﾛｱ)') == 'F' and AddataParts._fr_word('Rr ｶﾊﾞｰ') == 'R' and AddataParts._fr_word('右ﾄﾞｱ') == ''


def test_aliases_are_applied_before_normalising():
    """長音入り・ﾌﾛﾝﾄ 入りの言い換え（norm_name の後では当たらなかったもの）は、前後・左右の語を残した形に当て、完全一致専用の候補にする
    （近似に使うと ｺｱｻﾎﾟｰﾄ → ﾗｼﾞｴｰﾀ本体 のような別部品に当たる）。右ﾌｪﾝﾀﾞ と ﾘﾔﾌｪﾝﾀﾞ を取り違えない"""
    p = _Names()
    assert 'ﾊﾞﾂｸﾄﾞｱ' in p._strict_variants('ﾃｰﾙｹﾞｰﾄ'), p._strict_variants('ﾃｰﾙｹﾞｰﾄ')
    assert 'ｸｵﾀﾊﾟﾈﾙ' in p._strict_variants('ﾘﾔﾌｪﾝﾀﾞ') and not p._strict_variants('右ﾌｪﾝﾀﾞ')
    assert 'Rﾊﾞﾝﾊﾟｶﾊﾞ' in p._strict_variants('ﾘﾔﾊﾞﾝﾊﾟｰﾌｪｲｽ')                       # リヤの指定は R を保つ（R 付きの 12.DB 名と完全一致できる）
    for right in ('R. ﾊﾞﾝﾊﾟｰﾌｪｲｽ', 'R/H ﾊﾞﾝﾊﾟｰﾌｪｲｽ', 'RH ﾊﾞﾝﾊﾟｰﾌｪｲｽ', 'R ﾊﾞﾝﾊﾟｰﾌｪｲｽ', '右ﾊﾞﾝﾊﾟｰﾌｪｲｽ'):   # 左右の R はリヤにしない
        assert not any(v.startswith('R') for v in p._strict_variants(right)), (right, p._strict_variants(right))
    assert any(v.startswith('F') for v in p._strict_variants('RF ﾊﾞﾝﾊﾟｰﾌｪｲｽ'))   # RF = 右フロント → 前は残す
    assert 'ﾌﾛﾝｶﾞｽ' in p._strict_variants('ｸｰﾗｰｶﾞｽ') and 'ﾌﾛﾝｶﾞｽ' in p._name_variants('エアコンガス') + p._strict_variants('エアコンガス')
    assert p._name_variants('ﾌﾛﾝﾄﾊﾞﾝﾊﾟ')[0] == AddataParts.norm_name('ﾌﾛﾝﾄﾊﾞﾝﾊﾟ')   # 先頭は元の名前
    assert 'ﾛﾜ' in ''.join(p._name_variants('ﾌﾛﾝﾄﾊﾞﾝﾊﾟﾛｱ'))                       # 以前から効いていた言い換えは従来どおり近似にも使う
    assert not set(p._strict_variants('ﾌﾛﾝﾄﾊﾞﾝﾊﾟﾛｱ')) & set(p._name_variants('ﾌﾛﾝﾄﾊﾞﾝﾊﾟﾛｱ'))


def test_small_name():
    assert de.is_small_name('ｸﾘｯﾌﾟ') and de.is_small_name('クリップ') and de.is_small_name('ﾘﾃｰﾅ') and de.is_small_name('ｽｸﾘｭ')
    assert not de.is_small_name('ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ')
    for small in ('ﾘﾃｰﾅｰ', 'ﾜｯｼｬｰ', 'ﾘﾃ-ﾅ', 'ｸﾘｯﾌﾟ類', 'クリップ等', 'ｸﾘｯﾌﾟ×12', '左 ｸﾘｯﾌﾟ ﾓｰﾙ'):   # 語末の長音・類/等・×N・先頭の左右
        assert de.is_small_name(small), small
    assert not de.is_small_name('ｴﾝｼﾞﾝﾏｳﾝﾃｨﾝｸﾞｸｯｼｮﾝ', 8000)                    # 1 個 3,000 円を超えれば小物にしない
    assert de.is_small_name('ｸﾘｯﾌﾟ ﾌﾛﾝﾄﾊﾞﾝﾊﾟ') and de.is_small_name('ｱｳﾄｻｲﾄﾞﾓｰﾙﾃﾞｨﾝｸﾞ ｸﾘｯﾌﾟ NO.2') and de.is_small_name('ﾄﾞｱｼｰﾙ')
    for big in ('ｻｲﾄﾞｼﾙ ｽｶｯﾌﾟ', 'ｽﾃｯﾌﾟ', 'ｽﾃｱﾘﾝｸﾞ', 'ﾋﾟｽﾄﾝ', 'ｴｸｽﾃﾝｼｮﾝ', 'ﾌﾛﾝﾄﾊﾞﾝﾊﾟｶﾊﾞｰ', 'ｳｲﾝﾄﾞｼｰﾙﾄﾞｶﾞﾗｽ', 'ﾊﾟﾈﾙ ﾌﾛﾝﾄﾌｪﾝﾀﾞ', 'ﾎﾞﾙﾄｶﾊﾞｰ ﾌﾛﾝﾄ'):   # 長音を除くと ｼｰﾙ → ｼﾙ・ｽﾃｰ → ｽﾃ・ﾋﾟｰｽ → ﾋﾟｽ に当たる大物
        assert not de.is_small_name(big), big


def test_main_job_moves_block_context():
    """2026-09-15 アプリの実機テスト（スペーシア FAX 見積）: 読み手が HYBRID ｴﾝﾌﾞﾚﾑ を ｴﾑﾌﾞﾚﾑ と誤記した行が未照合になり、部位の文脈が
    フードのまま残って、左ﾌﾛﾝﾄｻｲﾄﾞﾒﾝﾊﾞ（全体で名称一致 1510）が同じ部位の左ﾌｰﾄﾞﾋﾝｼﾞ（0.63）に、ｸﾘｯﾌﾟ ×12 がフードのｸﾘｯﾌﾟになっていた
    （合計は合うので合格）。原因は、工賃のある主作業（左ﾌﾛﾝﾄﾌｪﾝﾀﾞﾊﾟﾈﾙ 取替）が単価で決まった行として部位の文脈を動かさなかったこと。
    主作業は単価で決めても文脈を動かす／ｴﾑﾌﾞﾚﾑ は ｴﾝﾌﾞﾚﾑ の言い換え。
    （「名前がただ 1 つ完全に一致した部品は同じ部位の候補で置き換えない」規則も試したが、JPN タクシーの 2 行（点検調整・脱着）で実機と違う
    コードになったので採らない。同じ部位の中の候補の方が正しい行が実在する）"""
    if not _s89_available():
        print('   skip test_main_job_moves_block_context（この ADDATA に S89 が無い）')
        return
    e = _draft(['|ﾊﾟﾈﾙ ﾌﾛﾝﾄﾌｰﾄﾞ|取替|||1|30700|3048||', '|RH ﾌﾛﾝﾄﾌｰﾄﾞ ﾋﾝｼﾞ|取替|||1|2950|762||', '|ｸﾘｯﾌﾟ|取替|||7|630|||',
                '|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||', '|ｻｲﾄﾞ HYBRID ｴﾑﾌﾞﾚﾑ|取替|||1|1400|||', '|ｸﾘｯﾌﾟ|取替|||12|1080|||',
                '|LH ﾌﾛﾝﾄｻｲﾄﾞ ﾒﾝﾊﾞ|修理|||||15240||'])
    codes = [it.get('code') for it in e['items']]
    assert codes[-1] == '1510', f'サイドメンバが 1510 にならない: {[(it.get("code"), it.get("name")) for it in e["items"]]}'
    assert codes[4] == '0850', f'ｴﾑﾌﾞﾚﾑ（別表記）が ｴﾝﾌﾞﾚﾑ(HYBRID) 0850 にならない: {codes}'
    assert codes[5] == '0970', f'左フェンダの後のｸﾘｯﾌﾟ ×12 がフェンダのｸﾘｯﾌﾟ 0970 にならない: {codes}'
    # 辞書に無い誤記（語末欠け）でも、主作業が文脈を動かすのでサイドメンバとｸﾘｯﾌﾟは正しい（エンブレムの行だけ未照合 = 要確認）
    e2 = _draft(['|ﾊﾟﾈﾙ ﾌﾛﾝﾄﾌｰﾄﾞ|取替|||1|30700|3048||', '|RH ﾌﾛﾝﾄﾌｰﾄﾞ ﾋﾝｼﾞ|取替|||1|2950|762||', '|ｸﾘｯﾌﾟ|取替|||7|630|||',
                 '|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||', '|ｻｲﾄﾞ HYBRID ｴﾝﾌﾞﾚ|取替|||1|1400|||', '|ｸﾘｯﾌﾟ|取替|||12|1080|||',
                 '|LH ﾌﾛﾝﾄｻｲﾄﾞ ﾒﾝﾊﾞ|修理|||||15240||'])
    codes2 = [it.get('code') for it in e2['items']]
    assert codes2[-1] == '1510' and codes2[5] == '0970', f'語末欠けの誤記でサイドメンバ／ｸﾘｯﾌﾟがずれる: {codes2}'


def test_reversed_names_find_the_part():
    """「名詞 修飾」の順の名前でも ADDATA の部品に当たる。言い換え（クーラーガス → フロンガス）も"""
    if not _s89_available():
        print('   skip test_reversed_names_find_the_part（この ADDATA に S89 が無い）')
        return
    e = _draft(['|ﾊﾟﾈﾙ ﾌﾛﾝﾄﾌｰﾄﾞ|取替|||1|30700|3048||', '|ｸﾞﾘﾙ ﾗｼﾞｴｰﾀ|取替|||1|19800|||', '|ｷｬｯﾌﾟ ﾌﾛﾝﾄﾊﾞﾝﾊﾟ|取替|||1|1600|||',
                '|ｶﾞｰﾆｯｼｭ ｶｳﾘﾝｸﾞﾄｯﾌﾟ ｾﾝﾀ|取替|||1|11600|3048||', '|ｸｰﾗｰｶﾞｽ|取替|||1|3000|||'])
    assert [it.get('code') for it in e['items']] == ['0600', '0200', '0045', '0750', '6599'], [(it.get('code'), it.get('name')) for it in e['items']]
    assert all(not it.get('manual') for it in e['items'])


def test_quantity_from_price_needs_the_same_name():
    """名前が近いだけの別部品（TV アンテナフィルム 6,000 円 → リヤドアのフィルム 100 円）を数量 60 にしない。手入力にして候補を探す"""
    if not _s89_available():
        print('   skip test_quantity_from_price_needs_the_same_name（この ADDATA に S89 が無い）')
        return
    e = _draft(['|ｶﾞﾗｽ ｳｨﾝﾄﾞｼｰﾙﾄﾞ|取替|||1|187600|23622||', '|TVｱﾝﾃﾅﾌｨﾙﾑ|取替|||1|6000|||'])
    it = e['items'][1]
    assert it.get('manual') and it['qty'] == 1 and it['price'] == 6000, it


def test_small_part_follows_one_sided_main_job():
    """左フェンダの取替の組の「クリップ 12 個」は左のクリップ 1 行（左右 6 個ずつに分けない）。左右の書かれていない主作業のあとは従来どおり"""
    if not _s89_available():
        print('   skip test_small_part_follows_one_sided_main_job（この ADDATA に S89 が無い）')
        return
    e = _draft(['|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||', '|ｻｲﾄﾞ HYBRID ｴﾝﾌﾞﾚﾑ|取替|||1|1400|||', '|ｸﾘｯﾌﾟ|取替|||12|1080|||'])
    assert len(e['items']) == 3, [(it.get('code'), it.get('qty')) for it in e['items']]
    clip = e['items'][2]
    assert clip['code'] == '0970' and clip['qty'] == 12, clip
    assert any('左右分割せず' in n for n in e.get('_draft_notes') or []), e.get('_draft_notes')


def test_side_is_not_inherited_after_both_sides_or_printed_code():
    """左右両方の主作業のあとの小物は片側にまとめない。部品コードが印字された小物は印字どおり（引き継いだ左右で反対側に書き換えない）"""
    if not _s89_available():
        print('   skip test_side_is_not_inherited_after_both_sides_or_printed_code（この ADDATA に S89 が無い）')
        return
    e = _draft(['|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||', '|RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||', '|ｸﾘｯﾌﾟ|取替|||12|1080|||'])
    assert not any('左右分割せず' in n for n in e.get('_draft_notes') or []), e.get('_draft_notes')
    assert not (len(e['items']) == 3 and e['items'][2]['qty'] == 12 and e['items'][2]['code'] in ('0970', '1170')), [(it.get('code'), it.get('qty')) for it in e['items']]
    e2 = _draft(['|RH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||', '0970|ｸﾘﾂﾌﾟ|取替|||2|180|||'])
    assert e2['items'][1]['code'] == '0970', e2['items'][1]


def test_manual_sided_main_job_blocks_inheritance():
    """部位の分からない手入力の主作業（左右あり）のあとは、別の片側の主作業があっても小物に片側を引き継がない"""
    if not _s89_available():
        print('   skip test_manual_sided_main_job_blocks_inheritance（この ADDATA に S89 が無い）')
        return
    e = _draft([{'name': '右 ﾌｪﾝﾀﾞ 下処理', 'method': '', 'wage': 7620, 'manual': True}, '|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||', '|ｸﾘｯﾌﾟ|取替|||12|1080|||'])
    assert not any('左右分割せず' in n for n in e.get('_draft_notes') or []), e.get('_draft_notes')


def test_alias_dictionary_needs_votes_or_same_name():
    """別名辞書（全車種の 部品名 → 部品コード）は、票の 30% 以上がそのコードか、この車の名称と包含関係のときだけ採る
    （ﾎﾞﾝﾈｯﾄ → ﾌｰﾄﾞﾊﾟﾈﾙ は採る。ｸﾘｯﾌﾟ を ｽｸﾘﾕ にしない）"""
    if not _s89_available():
        print('   skip test_alias_dictionary_needs_votes_or_same_name（この ADDATA に S89 が無い）')
        return
    d = de.Drafter(dict(READING, blocks=[]))
    if not de.part_names():
        print('   skip（別名辞書が無い）')
        return
    assert d._alias_ref('ﾎﾞﾝﾈｯﾄ', '', '')[0] == 600 and d._alias_ref('ﾃｰﾙｹﾞｰﾄ', '', '')[0] == 4300
    r, _ = d._alias_ref('ｸﾘｯﾌﾟ', '', '')
    assert r is None or 'ｸﾘﾂﾌﾟ' in ''.join(d.parts.name20_by_ref.get(r, ())), (r, d.parts.name20_by_ref.get(r))


def test_quantity_for_unsided_clip_on_one_sided_part():
    """左右の無い「クリップ」1 行の金額が、左側だけの部品の標準単価の倍数なら数量で合わせる（名前の近さが -1 になって別の部位のクリップに替わらない）"""
    if not _s89_available():
        print('   skip test_quantity_for_unsided_clip_on_one_sided_part（この ADDATA に S89 が無い）')
        return
    e = _draft(['|LH ﾌﾛﾝﾄﾌｪﾝﾀﾞ ﾊﾟﾈﾙ|取替|||1|23000|3810||', '|ｸﾘｯﾌﾟ|取替|||1|1080|||'])
    clip = e['items'][1]
    assert clip['code'] in ('0970', '0973') and clip['qty'] == 12, clip


def test_unmatched_row_gets_price_candidates():
    """名前で決まらなかった行には、この車の標準単価が同じ部品を候補として出す（find_ref_by_price を手で回さなくてよい）"""
    if not _s89_available():
        print('   skip test_unmatched_row_gets_price_candidates（この ADDATA に S89 が無い）')
        return
    e = _draft(['|ﾎﾞﾝﾈｯﾄ ｱｳﾀｰ ﾐｶｸﾆﾝ|取替|||1|30700|||'])
    it = e['items'][0]
    rv = [r for r in e['_review'] if r['kind'] == '未照合の候補']
    if it.get('manual'):
        assert rv and '0600' in rv[0]['text'], e['_review']
    else:
        assert it['code'] == '0600', it   # 言い換え（ﾎﾞﾝﾈﾂﾄ → ﾌｰﾄﾞ）で当たったならそれでよい


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
    print('name_order tests:', 'all ok' if not fails else f'{fails} failed')
    sys.exit(1 if fails else 0)
