import sys; sys.path.insert(0,'.')
import harness, h
p = h.parse_csv_to_items
fails=[]
def chk(tag, csv, want_sum=None, want_n=None, check=None):
    try: it = p(csv)
    except Exception as e:
        fails.append((tag,'EXC',repr(e))); return
    s = sum(i.get('parts_amount',0)+i.get('wage',0) for i in it)
    ok = True
    if want_sum is not None and s != want_sum: ok=False
    if want_n is not None and len(it) != want_n: ok=False
    if check and not check(it): ok=False
    if not ok:
        fails.append((tag, len(it), s, [(i.get('name'),i.get('work_code'),i.get('parts_amount'),i.get('wage'),i.get('part_no')) for i in it]))

chk('D1 集計語', """品名,区分,数量,部品金額,工賃,部品コード
フロントバンパー,交換,1,45000,8000,52119-XXX
ヘッドランプASSY,交換,1,60000,5000,81130-YYY
塗装,塗装,1,0,32000,
部品計,,,105000,,
工賃計,,,,45000,
諸費用計,,,,,
小計,,,150000,,
消費税,,,,,
御請求額,,,203170,,
以上,,,,,""", want_sum=150000, want_n=3)
chk('D2 列優先', """品名,部品 コード,数量,部品金額,工賃
バンパー,52119-A,1,113000,0""", want_sum=113000, check=lambda it: it[0]['part_no']=='52119-A')
chk('D3 ¥先頭行', """バンパー,交換,1,¥45000,¥8000""", want_n=1, want_sum=53000)
chk('D4 位置補完', """品名,部品金額,工賃
バンパー,45000,8000""", want_sum=53000, check=lambda it: not it[0].get('work_code'))
chk('D5 途中見出し', """バンパー,交換,1,83000,0
ヘッドランプ,交換,1,60000,0
品名,区分,数量,部品金額,工賃
ドア,交換,1,50000,0""", want_sum=193000, want_n=3)
chk('D6 フェンス', """```csv
品名,区分,数量,部品金額,工賃
バンパー,交換,1,45000,8000
```
上記のとおりです。""", want_n=1, want_sum=53000)
chk('D7 相違メモ', """品名,区分,数量,部品金額,工賃
バンパー,交換,1,45000,8000
"部品相違 1,200円",,,1200,""", want_n=1, want_sum=53000)
chk('No列ずれ', """No,品名,区分,数量,部品金額,工賃,部品コード
1,バンパー,交換,1,45000,8000,52119-A""", want_sum=53000, check=lambda it: it[0]['part_no']=='52119-A')
chk('見出し無し', """バンパー,交換,1,45000,8000,52119-A""", want_sum=53000, check=lambda it: it[0]['part_no']=='52119-A' and it[0]['work_code']=='交換')
chk('括弧見出し', """品名,区分,数量,部品金額（税抜）,工賃（税抜）
バンパー,交換,1,45000,8000""", want_sum=53000, want_n=1)
chk('正当品名', """品名,区分,数量,部品金額,工賃
合計表示灯,交換,1,3000,0
特別値引,,1,-5000,0
税金,,1,12000,0""", want_n=3, want_sum=10000)
chk('見出し前のタイトル行', """お見積書
株式会社サンプル自動車
品名,区分,数量,部品金額,工賃
バンパー,交換,1,45000,8000""", want_n=1, want_sum=53000)
chk('空行入り', """品名,区分,数量,部品金額,工賃

バンパー,交換,1,45000,8000

ドア,交換,1,50000,0""", want_n=2, want_sum=103000)
chk('数量複数', """品名,区分,数量,部品金額,工賃
ボルト,交換,4,500,0""", want_n=1, check=lambda it: it[0]['quantity']==4)
chk('マイナス金額', """品名,区分,数量,部品金額,工賃
値引,,1,-5000,0""", want_n=1, want_sum=-5000)
chk('工数列', """品名,区分,数量,部品金額,工賃,工数
塗装,塗装,1,0,32000,2.5""", want_n=1, check=lambda it: str(it[0].get('index_value'))=='2.5')
for f in fails: print('FAIL', f)
print('RESULT:', 'ALL PASS' if not fails else f'{len(fails)} FAIL')

# ---- 4周目で見つかった回帰の再発防止 ----
fails2=[]
def chk2(tag, csv, want_sum=None, want_n=None, check=None):
    try: it = p(csv)
    except Exception as e:
        fails2.append((tag,'EXC',repr(e))); return
    s = sum(i.get('parts_amount',0)+i.get('wage',0) for i in it)
    ok = True
    if want_sum is not None and s != want_sum: ok=False
    if want_n is not None and len(it) != want_n: ok=False
    if check and not check(it): ok=False
    if not ok:
        fails2.append((tag, len(it), s, [(i.get('name'),i.get('work_code'),i.get('parts_amount'),i.get('wage'),i.get('part_no')) for i in it]))

chk2('A1 表上の合計欄', """御見積金額,,,203170,
品名,区分,数量,部品金額,工賃,部品コード
フロントバンパー,取替,1,45000,8000,52119-A
ヘッドランプASSY,取替,1,60000,5000,81130-B
塗装,塗装,1,0,32000,""", want_n=3, want_sum=150000)
chk2('A1 メタ情報行', """見積番号,12345
作成日,2026-09-01
品名,区分,数量,部品金額,工賃
バンパー,取替,1,45000,8000""", want_n=1, want_sum=53000)
chk2('A2 タイトル+No列', """お見積書
No,品名,区分,数量,部品金額,工賃,部品コード
1,フロントバンパー,取替,1,45000,8000,52119-A
2,ヘッドランプASSY,取替,1,60000,5000,81130-B""",
    want_n=2, want_sum=118000,
    check=lambda it: it[0]['part_no']=='52119-A' and it[0]['parts_amount']==45000)
chk2('A3 部品、油脂', """品名,区分,数量,部品、油脂,技術料
フロントバンパー,取替,1,45000,8000
ヘッドランプASSY,取替,1,60000,5000""", want_n=2, want_sum=118000)
chk2('A3 部品(単独)', """品名,区分,数量,部品,技術料
フロントバンパー,取替,1,45000,8000""", want_n=1, want_sum=53000)
chk2('A3 金額（部品）', """品名,区分,数量,金額（部品）,金額（工賃）
フロントバンパー,取替,1,45000,8000""", want_n=1, want_sum=53000)
chk2('A4 2ページ目見出し', """品名,区分,数量,部品金額,工賃
バンパー,取替,1,83000,0
品名,区分,数量,部品金額,工賃
ドア,取替,1,50000,0""", want_n=2, want_sum=133000)
for f in fails2: print('FAIL', f)
print('ROUND4:', 'ALL PASS' if not fails2 else f'{len(fails2)} FAIL')
