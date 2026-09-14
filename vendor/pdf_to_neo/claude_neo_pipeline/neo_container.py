# -*- coding: utf-8 -*-
"""NEO コンテナ読み書き（app.py から純粋関数を切り出し。Streamlit 依存なし）"""
import struct, zlib, re, os, datetime


def find_real_cks(data, start=424):
    """comp_len連鎖法でCK位置を特定（偽CK除外）"""
    all_ck = []
    for i in range(start, len(data) - 1):
        if data[i] == 0x43 and data[i + 1] == 0x4B:
            all_ck.append(i)
    if not all_ck:
        return []
    real_ck = []
    idx = 0
    while idx < len(all_ck):
        ck = all_ck[idx]
        real_ck.append(ck)
        cl = struct.unpack('<H', data[ck - 4:ck - 2])[0]
        exp = ck + cl + 8
        found = False
        for j in range(idx + 1, len(all_ck)):
            if all_ck[j] == exp:
                idx = j
                found = True
                break
        if not found:
            break
    return real_ck


def decompress_neo(data, real_ck):
    """辞書連鎖展開でrawデータを復元"""
    full_raw = b''
    for i, ck in enumerate(real_ck):
        start = ck + 2
        end   = real_ck[i + 1] - 8 if i + 1 < len(real_ck) else len(data)
        chunk = data[start:end]
        if i == 0:
            raw = zlib.decompress(chunk, -15)
        else:
            dobj = zlib.decompressobj(-15, zdict=full_raw[-32768:])
            raw  = dobj.decompress(chunk)
        full_raw += raw
    return full_raw


def parse_entries(data, first_ck):
    """管理領域とファイルテーブルを解析
    NEOファイルテーブルの正しい構造:
      Entry[0]: [DOS 4B][attr 2B][\\fn\\0]  ← 先頭ファイル(hidden), size/offsetは管理領域に格納
      Entry[1]: (Entry[0]の末尾に付く [size 4B][offset 4B][00 00]) + [DOS 4B][attr 2B][\\fn\\0]
      ...
      Entry[N-1]: (Entry[N-2]の末尾に付く [size 4B][offset 4B][00 00]) + [DOS 4B][attr 2B][\\fn\\0]
    つまり各エントリの\\0の直後の size/offset は「次のエントリ」のデータ位置を示す。
    先頭エントリ(raw offset=0)にはテーブル内にsize/offsetがなく、管理領域から導出する。
    """
    table       = data[424:first_ck]
    first_entry = None
    for i in range(len(table) - 7):
        if table[i + 6] == 0x5C and struct.unpack_from('<H', table, i + 4)[0] == 0x0020:
            first_entry = i
            break
    if first_entry is None:
        raise ValueError("ファイルテーブルのエントリが見つかりません")
    mgmt    = table[:first_entry]
    # まず全エントリをスキャンし、各エントリの名前と末尾データを収集
    raw_entries = []
    pos         = first_entry
    while pos < len(table):
        if pos + 6 >= len(table):
            break
        if table[pos + 6] != 0x5C:
            pos += 1
            continue
        dos_bytes = table[pos:pos + 4]
        nul       = table.find(b'\x00', pos + 7)
        if nul == -1:
            break
        fn        = table[pos + 7:nul].decode('cp932', errors='replace')
        remaining = len(table) - (nul + 1)
        if remaining >= 10:
            trailing_sz  = struct.unpack_from('<I', table, nul + 1)[0]
            trailing_off = struct.unpack_from('<I', table, nul + 5)[0]
            has_trailing  = trailing_sz < 10_000_000 and trailing_off < 10_000_000
        else:
            trailing_sz, trailing_off, has_trailing = None, None, False
        raw_entries.append({
            'name': fn, 'dos': dos_bytes,
            'trailing_sz': trailing_sz, 'trailing_off': trailing_off,
            'has_trailing': has_trailing,
        })
        if has_trailing:
            pos = nul + 11
        else:
            pos = len(table)
    # size/offsetの正しい割り当て:
    #   raw_entries[i]の末尾データ → entries[i+1] のsize/offset
    #   entries[0] は先頭ファイル(hidden): raw offset=0, sizeは他エントリの合計から逆算
    entries = []
    for i, raw in enumerate(raw_entries):
        if i == 0:
            entries.append({'name': raw['name'], 'size': None, 'offset': None,
                            'is_last': True, 'dos': raw['dos']})
        else:
            prev = raw_entries[i - 1]
            entries.append({'name': raw['name'],
                            'size': prev['trailing_sz'], 'offset': prev['trailing_off'],
                            'is_last': False, 'dos': raw['dos']})
    return mgmt, entries


def extract_files(full_raw, entries):
    """rawデータから12ファイルを切り出し"""
    files        = {}
    normal_total = 0
    for e in entries:
        if not e['is_last']:
            files[e['name']] = full_raw[e['offset']:e['offset'] + e['size']]
            normal_total    += e['size']
    last_entry = [e for e in entries if e['is_last']]
    if not last_entry:
        raise ValueError("最後エントリ（hidden先頭ファイル）が見つかりません")
    last_name         = last_entry[0]['name']
    files[last_name]  = full_raw[0:len(full_raw) - normal_total]
    return files


# ============================================================
# 内部ファイル更新: AnSMB.txt（見積本体SQLite）
# ============================================================


def update_mail_ini(orig_bytes, cust, grand_total, merge_mode=False):
    """Shift_JIS XMLの顧客・車両情報を更新
    merge_mode=True の場合、非空の値のみ上書きする。
    """
    text         = orig_bytes.decode('cp932', errors='replace')
    customer_name = safe_str(cust.get('customer_name', ''))
    owner_name    = safe_str(cust.get('owner_name', ''))
    car_dept      = safe_str(cust.get('car_reg_department', ''))
    car_div       = safe_str(cust.get('car_reg_division', ''))
    car_biz       = safe_str(cust.get('car_reg_business', ''))
    car_serial    = safe_str(cust.get('car_reg_serial', ''))
    car_no_full   = f'{car_dept}{car_div}{car_biz}{car_serial}'
    car_name      = safe_str(cust.get('car_name', ''))
    car_serial_no = safe_str(cust.get('car_serial_no', ''))
    kilometer     = safe_str(cust.get('kilometer', ''))
    car_reg_date  = safe_str(cust.get('car_reg_date', ''))
    term_date     = safe_str(cust.get('term_date', ''))
    tag_values = {
        'CustomerName1': customer_name,
        'OwnerName':     owner_name,
        'UserName':      customer_name,
        'CarNo':         car_no_full,
        'CarName':       car_name,
        'CarSerialNo':   car_serial_no,
        'Kilometrage':   kilometer,
        'CarNoArea':     car_dept,
        'CarNoClass':    car_div,
        'CarNoKana':     car_biz,
        'CarNoSeries':   car_serial,
        'Total':         grand_total,
    }
    if term_date and term_date != '00000000':
        term_era, term_era_year = get_era_info(term_date)
        era_year_int = int(term_era_year)
        term_month = term_date[4:6] if len(term_date) >= 6 else ''
        term_day   = term_date[6:8] if len(term_date) >= 8 else ''
        tag_values['CarTermEraDate'] = (
            f'{term_era}{era_year_int}年{int(term_month)}月{int(term_day)}日'
            if term_month and term_day else ''
        )
    else:
        tag_values['CarTermEraDate'] = ''
    if car_reg_date and car_reg_date != '00000000':
        reg_era, reg_era_year = get_era_info(car_reg_date)
        reg_year_int = int(reg_era_year)
        reg_month    = car_reg_date[4:6] if len(car_reg_date) >= 6 else ''
        tag_values['CarRegistedDate'] = (
            f'{reg_era}{reg_year_int}年{int(reg_month)}月'
            if reg_month and reg_month != '00' else ''
        )
    else:
        tag_values['CarRegistedDate'] = ''
    for tag_name, value in tag_values.items():
        if merge_mode and not value:
            continue  # マージモード: 空値はスキップ（テンプレートの既存値を保持）
        text = replace_xml_tag(text, tag_name, value)
    return text.encode('cp932', errors='replace')


# ============================================================
# 内部ファイル更新: AnSvImge.ini（INI）
# ============================================================

def update_imge_ini(orig_bytes, cust, merge_mode=False):
    """INIファイルの顧客・車両情報を更新
    merge_mode=True の場合、非空の値のみ上書きする。
    """
    text      = orig_bytes.decode('cp932', errors='replace')
    ini_values = {
        'CustomerName':    safe_str(cust.get('customer_name', '')),
        'CarNoDepartment': safe_str(cust.get('car_reg_department', '')),
        'CarNoDivision':   safe_str(cust.get('car_reg_division', '')),
        'CarNoBusiness':   safe_str(cust.get('car_reg_business', '')),
        'CarNoSerial':     safe_str(cust.get('car_reg_serial', '')),
        'CarName':         safe_str(cust.get('car_name', '')),
    }
    for key, value in ini_values.items():
        if merge_mode and not value:
            continue  # マージモード: 空値はスキップ（テンプレートの既存値を保持）
        text = replace_ini_value(text, key, value)
    return text.encode('cp932', errors='replace')


# ============================================================
# 内部ファイル更新: AnNote.ini（明細簡易表現）
# ============================================================

def generate_annote(items):
    """142B固定長 × 行数 の AnSMB.txt を生成（ERParts概要行テキスト）"""
    if not items:
        return b''
    lines = []
    for i, item in enumerate(items):
        name    = item.get('name', '')
        qty     = safe_int(item.get('quantity', 1), 1)
        rec_no  = i + 1
        line_no = rec_no * 10
        line    = bytearray(142)
        for j in range(142):
            line[j] = 0x20
        ln_str = f'{line_no:08d}'
        for j, c in enumerate(ln_str):
            line[j] = ord(c)
        name_bytes = name.encode('cp932', errors='replace')[:30]
        for j, b in enumerate(name_bytes):
            line[14 + j] = b
        qty_str = f'{min(qty, 99):02d}'
        line[98] = ord(qty_str[0])
        line[99] = ord(qty_str[1])
        for j, c in enumerate('90000'):
            line[100 + j] = ord(c)
        for j, c in enumerate('F99999'):
            line[127 + j] = ord(c)
        lines.append(bytes(line) + b'\r\n')
    return b''.join(lines)


# ============================================================
# NEO リパッカー
# ============================================================

def repack_neo(orig_data, files, mgmt, entries):
    """更新済みファイルをNEOバイナリに再パック
    NEOファイルテーブルの正しい書き込み構造:
      各エントリの\\0の直後に「次のエントリ」のsize/offset/padを配置する。
      先頭エントリ(hidden)のデータはraw offset=0に配置される。
      最終エントリの後にはsize/offset/padを付けない。
    """
    now     = datetime.datetime.now()
    now_dos = datetime_to_dos(now)
    entry_names  = [e['name'] for e in entries if e['name'] in files]
    missing_names = [name for name in files.keys() if name not in entry_names]
    ordered_names = entry_names + sorted(missing_names, key=lambda x: x.encode('cp932'))
    hidden_entries = [e for e in entries if e.get('is_last')]
    hidden_name    = hidden_entries[0]['name'] if hidden_entries else ordered_names[0]
    normal_names   = [name for name in ordered_names if name != hidden_name]
    raw     = files[hidden_name]
    offsets = {}
    sizes   = {}
    for name in normal_names:
        offsets[name] = len(raw)
        sizes[name]   = len(files[name])
        raw += files[name]
    table_bytes = b''
    for i, name in enumerate(ordered_names):
        if name == 'AnDBVersion.ini':
            dos = DOS_DBVER
        elif name == 'AnSvImge.ini':
            dos = DOS_IMGE
        else:
            dos = now_dos
        attr      = struct.pack('<H', 0x0020)
        name_enc  = ('\\' + name).encode('cp932') + b'\x00'
        table_bytes += dos + attr + name_enc
        # 次のエントリのsize/offsetを末尾に付加（最終エントリ以外）
        if i + 1 < len(ordered_names):
            next_name = ordered_names[i + 1]
            if next_name in sizes:
                table_bytes += struct.pack('<I', sizes[next_name])
                table_bytes += struct.pack('<I', offsets[next_name])
                table_bytes += b'\x00\x00'
    CHUNK_SIZE      = 32768
    raw_chunks      = [raw[i:i + CHUNK_SIZE] for i in range(0, len(raw), CHUNK_SIZE)]
    num_chunks      = len(raw_chunks)
    compressed_chunks = []
    prev_raw        = b''
    for i, raw_chunk in enumerate(raw_chunks):
        if i == 0:
            c = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15)
        else:
            dict_data = prev_raw[-32768:]
            c = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15, zdict=dict_data)
        compressed = c.compress(raw_chunk) + c.flush()
        compressed_chunks.append(compressed)
        prev_raw += raw_chunk
    new_mgmt  = bytearray(mgmt)
    last_size = len(files[hidden_name])
    struct.pack_into('<H', new_mgmt, len(new_mgmt) - 14, num_chunks)
    struct.pack_into('<H', new_mgmt, len(new_mgmt) - 10, last_size)
    header  = orig_data[:424]
    ck_data = b''
    for i, comp in enumerate(compressed_chunks):
        comp_len  = len(comp) + 2
        decomp_len = len(raw_chunks[i])
        ck_data   += b'\x00\x00\x00\x00' + struct.pack('<HH', comp_len, decomp_len) + b'CK' + comp
    return header + bytes(new_mgmt) + table_bytes + ck_data



# ---- app.py から追加切り出し（定数・ヘルパ）
TAX_RATE          = 0.10
DOS_DBVER = bytes.fromhex('334cc198')   # AnDBVersion.ini 固定値
DOS_IMGE  = bytes.fromhex('2c365a67')   # AnSvImge.ini 固定値
def datetime_to_dos(dt):
    """Python datetime → DOS日時バイト列(4B)"""
    dos_date = ((dt.year - 1980) << 9) | (dt.month << 5) | dt.day
    dos_time = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
    return struct.pack('<HH', dos_date, dos_time)


def get_era_info(date_str):
    """YYYYMMDD文字列（日の無い YYYYMM00 / YYYYMM も可）→ (和暦名, 和暦年4桁ゼロ埋め)

    年だけで分けると改元の年が狂う: 2019-01〜04 は平成 31（令和 1 ではない）、1989-01-01〜07 は昭和 64、
    1926-12-25〜31 は昭和元年。平成 31 年 3 月登録の車は実際に多い。日が無いときは月初とみなす（Codex 指摘 2026-09-14）"""
    s = str(date_str or '').strip()
    if s == '00000000' or not re.fullmatch(r'[0-9]{4}([0-9]{2}([0-9]{2})?)?', s):  # 4 / 6 / 8 桁の数字だけ。区切り付き（2019-05-01）や文字混じりは番兵（1 月 1 日扱いにしない。Codex 指摘）
        return '令和', '0000'
    year = int(s[:4])
    month = int(s[4:6]) if len(s) >= 6 else 0
    day = int(s[6:8]) if len(s) >= 8 else 0
    ymd = (year, month or 1, day or 1)
    if ymd >= (2019, 5, 1):
        return '令和', f'{year - 2018:04d}'
    if ymd >= (1989, 1, 8):
        return '平成', f'{year - 1988:04d}'
    if ymd >= (1926, 12, 25):
        return '昭和', f'{year - 1925:04d}'
    return '令和', '0000'

def safe_int(val, default=0):
    """OCR由来の「1個」「19,550円」「1.00」「8本」「**」「１２３」なども整数化"""
    if val is None or val == '' or val == '*' or val == '**':
        return default
    if isinstance(val, str) and val.strip().replace('*', '') == '':
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(round(val))
    s = str(val).strip()
    # 全角数字・全角マイナス・全角ピリオドを半角に変換（OCR/手入力対策）
    s = s.translate(str.maketrans('０１２３４５６７８９．−', '0123456789.-'))
    # 単位除去
    s = re.sub(r'[個本枚セット台式時間]$', '', s)
    s = re.sub(r'[円¥,，\s]', '', s)
    s = re.sub(r'[^\d.\-]', '', s)
    if not s or s == '-':
        return default
    try:
        return int(round(float(s)))
    except (ValueError, OverflowError):
        return default


def safe_float(val, default=0.0):
    """安全な浮動小数変換 (v11.0: 括弧・通貨・全角・単位を吸収)"""
def safe_str(val, default=''):
    """安全な文字列変換"""
    if val is None:
        return default
    return str(val)


def replace_xml_tag(text, tag_name, value):
    """XMLタグの中身を現在値に関係なく置換"""
    pattern = rf'<{re.escape(tag_name)}>[^<]*</{re.escape(tag_name)}>'
    replacement = f'<{tag_name}>{value}</{tag_name}>'
    result = re.sub(pattern, replacement, text)
    # 空タグ形式も対応
    empty_pattern = rf'<{re.escape(tag_name)}/>'
    result = result.replace(empty_pattern, replacement)
    return result


def replace_ini_value(text, key, value):
    """INIキー値を確実に更新"""
    pattern = rf'^({re.escape(key)}\s*=).*$'
    replacement = rf'\g<1>{value}'
    return re.sub(pattern, replacement, text, flags=re.MULTILINE)



# ---------------------------------------------------------------- Windows 互換 cp932（IBM 拡張漢字）
import codecs as _codecs

def _build_ibm_map():
    m = {}
    for hi in range(0xFA, 0xFD):
        for lo in list(range(0x40, 0x7F)) + list(range(0x80, 0xFD)):
            b = bytes((hi, lo))
            try:
                ch = b.decode('cp932')
            except UnicodeDecodeError:
                continue
            if len(ch) == 1 and ch.encode('cp932', 'replace') != b:
                m[ch] = b
    return m

_IBM_MAP = None

def encode_cp932w(s: str, errors: str = 'replace') -> bytes:
    """Python の cp932 は IBM 拡張漢字（德・髙・﨑 等）を NEC 選定（ED/EE 行）で符号化するが、
    Windows（コグニ）は IBM 拡張（FA-FC 行）で書く。実 NEO の xml/AnSMB/管理領域と一致させる"""
    global _IBM_MAP
    if _IBM_MAP is None:
        _IBM_MAP = _build_ibm_map()
    out = bytearray()
    for ch in s:
        b = _IBM_MAP.get(ch)
        out += b if b else ch.encode('cp932', errors)
    return bytes(out)


def _cp932w_search(name):
    if name.lower() != 'cp932w':
        return None
    base = _codecs.lookup('cp932')
    return _codecs.CodecInfo(name='cp932w', encode=lambda s, errors='strict': (encode_cp932w(s, errors), len(s)), decode=base.decode)

_codecs.register(_cp932w_search)
