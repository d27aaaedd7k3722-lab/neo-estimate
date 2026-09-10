# -*- coding: utf-8 -*-
import sys, logging, json, sqlite3, tempfile, os, io, contextlib
SB=os.path.dirname(os.path.abspath(__file__))
ROOT=os.environ.get('XROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0,ROOT); sys.path.insert(0, SB+'/gm'); sys.path.insert(0,SB)
os.chdir(ROOT)
logging.getLogger('streamlit').setLevel(logging.CRITICAL)
import app, mockclient as M, pdf_to_neo_pipeline as P, neogen as N
assert os.path.dirname(app.__file__)==ROOT, app.__file__
KEY='K'*20
PDFB=open(SB+'/fixtures/a4_1p.pdf','rb').read()

def run(label, details, totals, tax_incl=False, mode='A', show=True, keep_cache=False):
    # keep_cache=True は「同じPDFを2回目に通したときに何が起きるか」を
    # 見るためのもの。既定でキャッシュを消すと、キャッシュ経路の不具合
    # （1回目の出力が2回目の入力になる）をテストが素通りしてしまう。
    if not keep_cache:
        P.clear_pipeline_cache(); app._analyze_result_cache.clear()
    TOTALS=json.dumps(dict(totals, vehicle_info={}), ensure_ascii=False)
    DETAIL=json.dumps({"details":details}, ensure_ascii=False)
    def resp(model, contents, config, n):
        p=contents[0] if contents else ''
        if 'shaken_ocr' in p: return M.FakeResponse('{}')
        if 'header_total_extraction' in p: return M.FakeResponse(TOTALS)
        if '金額検算エンジン' in p: return M.FakeResponse(json.dumps({"status":"success","details":details}, ensure_ascii=False))
        return M.FakeResponse(DETAIL)
    M.install(app, responder=resp, lister=lambda: [])
    tf=tempfile.NamedTemporaryFile(suffix='.pdf',delete=False); tf.write(PDFB); tf.close()
    buf=io.StringIO()
    with contextlib.redirect_stdout(buf):
        res=P.process_pdf_to_neo(tf.name, addata_root='', template_path=ROOT+'/template_toyota.neo',
                             mode_override=mode, api_key=KEY, model_name='g', is_tax_inclusive=tax_incl)
    f=N.unpack(res['neo_bytes']); t=tempfile.NamedTemporaryFile(suffix='.db',delete=False); t.write(f['AnSMB.txt']); t.close()
    c=sqlite3.connect(t.name)
    rows=c.execute("SELECT PartsName,PartsPriceOutTax,PartsPriceInTax,WageOutTax,WageInTax FROM ERParts").fetchall()
    tot=c.execute("SELECT ms_PartsTotalOutTax, ms_WageTotalOutTax, SubTotal, Total FROM Total").fetchone()
    v=res['verify']
    ws=[w for w in res.get('warnings',[]) if '車検証' not in w]
    if show:
        print(f"### {label}")
        print(f"  入力明細 {len(details)}行 / header={totals} / 税込表記={tax_incl}")
        print(f"  NEO {len(rows)}行:")
        for r in rows: print("    ", r)
        print(f"  Total: 部品計(税抜)={tot[0]:,} 工賃計(税抜)={tot[1]:,} 小計={tot[2]:,} 総合計(税込)={tot[3]:,}")
        print(f"  verify: ok={v.get('ok')} count={v.get('count_match')}({v.get('neo_count')}/{v.get('pdf_count')}) total={v.get('total_match')}")
        for w in ws: print("  ⚠", w)
        for l in res.get('log',[]):
            if 'total_match' in l or 'grand' in l: print("  LOG:", l)
    return {'rows':rows,'tot':list(tot),'warn':ws,'verify':{k:v.get(k) for k in ('ok','count_match','total_match','neo_count')},
            'log':[l for l in res.get('log',[]) if 'total_match' in l or 'grand' in l]}
