# -*- coding: utf-8 -*-
"""ケース生成: 原本(true)と、Geminiが返す明細(read)を作る"""
def mk(M=0, disc=0, extra=0, basis='in', tax_incl=False, disc_row=True, drop=True):
    """M: 読み落とす部品行の金額 / disc: 値引 / extra: 小計対象外の工賃行(レッカー)
       basis: 'in'=総合計は税込, 'out'=総合計は税抜表記
       disc_row: 値引き行が明細に印字されている
    """
    k = 1.1 if tax_incl else 1.0
    def a(x): return int(round(x*k))
    # 金額がすべて1000円単位だと消費税に端数が出ず、丸め方の違いを
    # 構造的に検出できない（実際、税の丸めを変えた回帰を144ケースが素通りした）。
    # 端数の出る金額を混ぜる。
    parts=[("フロントバンパー",300000),("フロントフェンダ",199_837)]
    if M: parts.append(("追加部品",M))
    P=sum(p[1] for p in parts); W=100_163
    rows=[]
    for n,v in parts:
        rows.append({"work_or_part_name":n,"category":"取替","labor_fee":0,"quantity":1,
                     "part_price":a(v),"part_number":"X"+str(v)})
    rows.append({"work_or_part_name":"バンパー脱着","category":"脱着","labor_fee":a(W),
                 "quantity":1,"part_price":0,"part_number":""})
    if extra:
        rows.append({"work_or_part_name":"レッカー費用","category":"","labor_fee":a(extra),
                     "quantity":1,"part_price":0,"part_number":""})
    if disc and disc_row:
        rows.append({"work_or_part_name":"値引き","category":"","labor_fee":0,"quantity":1,
                     "part_price":-a(disc),"part_number":""})
    T=P+W+extra-disc
    grand=int(round(T*1.1)) if basis=='in' else T
    if tax_incl:
        grand=int(round(T*1.1))
    read=[r for r in rows if not (drop and M and r["work_or_part_name"]=="追加部品")]
    hdr={"pdf_parts_total":a(P),"pdf_wage_total":a(W),"pdf_grand_total":grand,"discount_amount":a(disc)}
    return {"read":read,"hdr":hdr,"tax_incl":tax_incl,
            "true_rows":len(rows),"true_total_outtax":T,
            "true_total_intax":int(round(T*1.1))}
