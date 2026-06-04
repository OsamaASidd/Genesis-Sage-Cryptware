"""
Genesis Group - E-Invoicing Dashboard (multi-entity with login)
================================================================
Syncs from Sage Evolution SQL Server (dbo.InvNum, dbo.Client, _btblInvoiceLines)
DocType 0 = Tax Invoice | DocType 1 = Credit Note

Login:
  "Food 123"   -> Genesis Food Nigeria Limited
  "Cenima 456" -> Genesis Deluxe Cinemas Limited

Each logged-in user only sees and posts invoices for their own entity, using
that entity's API key. Rows are isolated per entity via the `entity` column.
"""

import os, io, re, sqlite3, threading, functools, pyodbc, requests
from datetime import datetime, date
from decimal import Decimal
from flask import (
    Flask, render_template, jsonify, send_file, request,
    session, redirect, url_for,
)

from config import (
    SECRET_KEY,
    GENESIS_DB_CONN_STR,
    GENESIS_DOCTYPE_INVOICE, GENESIS_DOCTYPE_CREDIT_NOTE,
    ENTITY_FILTER_COLUMN,
    LOGIN_USERS, ENTITY_LABELS, get_entity,
)

TAX_CAT_STANDARD = "STANDARD_VAT"
TAX_CAT_EXEMPT   = "ZERO_VAT"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "einvoice_genesis.db")
PDF_DIR  = os.path.join(BASE_DIR, "invoices_genesis")
os.makedirs(PDF_DIR, exist_ok=True)

PER_PAGE  = 25
app       = Flask(__name__)
app.secret_key = SECRET_KEY
_db_lock  = threading.Lock()


# ─── ENTITY RESOLUTION (per request) ───────────────────────────────────────────

def current_entity_key():
    return session.get("entity_key")

def current_entity():
    """Return the active entity config dict for the logged-in session."""
    return get_entity(session.get("entity_key"))

def entity_api(entity):
    """Build API url + headers for a given entity dict."""
    url     = entity["api_base_url"].rstrip("/")
    headers = {"Content-Type": "application/json", "x-api-key": entity["api_key"]}
    return url, headers

def entity_supplier(entity):
    cfg = entity["supplier"]
    return {
        "name":        cfg["party_name"],
        "address":     cfg["postal_address"].get("street_name", ""),
        "tin":         cfg["tin"],
        "email":       cfg["email"],
        "telephone":   cfg["telephone"],
        "street_name": cfg["postal_address"].get("street_name", ""),
        "city_name":   cfg["postal_address"].get("city_name", ""),
        "postal_zone": cfg["postal_address"].get("postal_zone", ""),
        "country":     cfg["postal_address"].get("country", "NG"),
    }


# ─── AUTH ───────────────────────────────────────────────────────────────────

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not current_entity():
            # API calls get JSON 401; page navigations get redirected.
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Not authenticated"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def to_float(val):
    if val is None: return 0.0
    if isinstance(val, Decimal): return float(val)
    try: return float(val)
    except: return 0.0

def to_str(val):
    if val is None: return ""
    return str(val).strip()

def find_col(columns, *candidates):
    cl = [c.lower() for c in columns]
    for cand in candidates:
        if cand.lower() in cl:
            return columns[cl.index(cand.lower())]
    return None

def to_e164(phone):
    p = re.sub(r'\D', '', phone or '')
    if not p: return '+234'
    if p.startswith('234'): return f'+{p}'
    if p.startswith('0') and len(p) == 11: return f'+234{p[1:]}'
    if len(p) == 10: return f'+234{p}'
    return f'+{p}'


# ─── SQLITE ───────────────────────────────────────────────────────────────────

def _open_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn

def db_read(sql, params=()):
    with _db_lock:
        conn = _open_db()
        try: return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally: conn.close()

def db_read_one(sql, params=()):
    with _db_lock:
        conn = _open_db()
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally: conn.close()

def db_write(sql, params=()):
    with _db_lock:
        conn = _open_db()
        try: conn.execute(sql, params); conn.commit()
        finally: conn.close()

def db_write_many(operations):
    with _db_lock:
        conn = _open_db()
        try:
            for sql, params in operations: conn.execute(sql, params)
            conn.commit()
        finally: conn.close()

def init_db():
    with _db_lock:
        conn = _open_db()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS invoices (
                post_order INTEGER PRIMARY KEY,
                entity TEXT,
                trx_number INTEGER,
                invoice_num TEXT, customer_name TEXT, customer_id TEXT,
                customer_tin TEXT, customer_email TEXT, customer_phone TEXT,
                customer_address TEXT, customer_city TEXT, invoice_date TEXT,
                amount REAL DEFAULT 0, vat_amount REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                irn TEXT, qr_code TEXT, posted_at TEXT,
                error_message TEXT, api_response TEXT,
                invoice_description TEXT,
                invoice_type TEXT DEFAULT 'Invoice',
                last_synced TEXT)""")

            conn.execute("""CREATE TABLE IF NOT EXISTS invoice_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_order INTEGER,
                trx_number INTEGER,
                line_num INTEGER, item_code TEXT, description TEXT,
                quantity REAL DEFAULT 1, unit_price REAL DEFAULT 0,
                amount REAL DEFAULT 0, tax_rate REAL DEFAULT 0)""")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_status   ON invoices(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_customer ON invoices(customer_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_entity   ON invoices(entity)")
            for col_sql in (
                "ALTER TABLE invoices ADD COLUMN cancel_ref TEXT",
                "ALTER TABLE invoices ADD COLUMN entity TEXT",
            ):
                try:
                    conn.execute(col_sql)
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()

init_db()


# ─── CLIENT MAP ───────────────────────────────────────────────────────────────

def _build_client_map(cursor):
    """Read dbo.Client with dynamic column discovery. Returns {AccountID: {...}}."""
    try:
        cursor.execute("SELECT TOP 0 * FROM dbo.Client")
        cols = [d[0] for d in cursor.description]
    except Exception as e:
        print(f"[WARN] dbo.Client not readable: {e}")
        return {}

    c_pk    = find_col(cols, "DCLink", "AutoIndex", "iClientID", "ClientID")
    c_acct  = find_col(cols, "Account", "cAccount", "AccountCode", "Code")
    c_name  = find_col(cols, "Name", "cName", "ClientName", "cAccount")
    c_tin   = find_col(cols, "TaxRef", "cTaxRefNo", "cTaxNo", "VATNo", "TaxNumber")
    c_email = find_col(cols, "EMail", "cEmail", "Email", "EmailAddress")
    c_phone = find_col(cols, "Tel", "cTel", "Cell", "cCell", "Phone", "Telephone")
    c_addr1 = find_col(cols, "Addr1", "cAddr1", "Address1", "cAddress1")
    c_addr2 = find_col(cols, "Addr2", "cAddr2", "Address2", "cAddress2")
    c_city  = find_col(cols, "Addr3", "cAddr3", "City", "cCity", "Address3")

    if not c_pk:
        print("[WARN] Cannot find PK column in dbo.Client")
        return {}

    sel = [f"[{c}]" for c in [c_pk, c_acct, c_name, c_tin, c_email, c_phone, c_addr1, c_addr2, c_city] if c]
    key_order = [c for c in [c_pk, c_acct, c_name, c_tin, c_email, c_phone, c_addr1, c_addr2, c_city] if c]

    client_map = {}
    try:
        cursor.execute(f"SELECT {', '.join(sel)} FROM dbo.Client")
        for row in cursor.fetchall():
            rec  = dict(zip(key_order, row))
            pk   = rec.get(c_pk)
            if pk is None:
                continue
            addr = ", ".join(p for p in [to_str(rec.get(c_addr1,"")), to_str(rec.get(c_addr2,""))] if p)
            client_map[pk] = {
                "id":      to_str(rec.get(c_acct, "")),
                "name":    to_str(rec.get(c_name, "")),
                "tin":     to_str(rec.get(c_tin, "")),
                "email":   to_str(rec.get(c_email, "")),
                "phone":   to_str(rec.get(c_phone, "")),
                "address": addr,
                "city":    to_str(rec.get(c_city, "")),
            }
    except Exception as e:
        print(f"[WARN] Client map query failed: {e}")

    print(f"[SYNC] Client map loaded: {len(client_map)} entries")
    return client_map


# ─── SAGE EVOLUTION SYNC ──────────────────────────────────────────────────────

def sync_from_sage(entity_key, entity, date_from=None, date_to=None):
    if not date_from:
        date_from = "2020-01-01"
    if not date_to:
        date_to = date.today().strftime("%Y-%m-%d")

    # Entity separation filter. Only applied if you've configured both
    # ENTITY_FILTER_COLUMN (config) and this entity's filter_value.
    filter_col   = ENTITY_FILTER_COLUMN
    filter_value = entity.get("filter_value", "")
    use_filter   = bool(filter_col and filter_value)

    try:
        sage = pyodbc.connect(GENESIS_DB_CONN_STR, timeout=15)
    except Exception as e:
        return {"ok": False, "error": f"DB connection: {e}"}

    try:
        cursor     = sage.cursor()
        client_map = _build_client_map(cursor)

        base_cols = """AutoIndex, InvNumber, AccountID, cAccountName,
                       InvDate, InvTotExcl, InvTotTax, InvTotIncl,
                       Description, DocType,
                       Address1, Address2, Address3,
                       cTaxNumber, cTelephone, cEmail"""

        extra_filter = f" AND [{filter_col}] = ?" if use_filter else ""
        params_tail  = [filter_value] if use_filter else []

        try:
            sql = f"""
                SELECT {base_cols}, iLinkNum
                FROM dbo.InvNum
                WHERE DocType IN (?, ?)
                  AND InvDate >= ? AND InvDate < DATEADD(day, 1, CAST(? AS date))
                  {extra_filter}
                ORDER BY InvDate DESC
            """
            cursor.execute(sql, [GENESIS_DOCTYPE_INVOICE, GENESIS_DOCTYPE_CREDIT_NOTE,
                                 date_from, date_to] + params_tail)
            has_link_col = True
        except Exception:
            sql = f"""
                SELECT {base_cols}
                FROM dbo.InvNum
                WHERE DocType IN (?, ?)
                  AND InvDate >= ? AND InvDate < DATEADD(day, 1, CAST(? AS date))
                  {extra_filter}
                ORDER BY InvDate DESC
            """
            cursor.execute(sql, [GENESIS_DOCTYPE_INVOICE, GENESIS_DOCTYPE_CREDIT_NOTE,
                                 date_from, date_to] + params_tail)
            has_link_col = False
        headers = cursor.fetchall()
        print(f"[SYNC:{entity_key}] {len(headers)} rows for {date_from} → {date_to} "
              f"(filter={'on' if use_filter else 'OFF'}, iLinkNum={'yes' if has_link_col else 'no'})")

        link_ids = set()
        for hdr in headers:
            if hdr[9] == GENESIS_DOCTYPE_CREDIT_NOTE and has_link_col and len(hdr) > 16 and hdr[16]:
                link_ids.add(int(hdr[16]))
        link_map = {}
        if link_ids:
            placeholders = ",".join("?" * len(link_ids))
            cursor.execute(f"SELECT AutoIndex, InvNumber FROM dbo.InvNum WHERE AutoIndex IN ({placeholders})",
                           list(link_ids))
            for r in cursor.fetchall():
                link_map[r[0]] = to_str(r[1])

    except Exception as e:
        sage.close()
        return {"ok": False, "error": str(e)}
    finally:
        sage.close()

    # Only consider this entity's existing rows.
    existing  = {r["post_order"]: r["status"]
                 for r in db_read("SELECT post_order, status FROM invoices WHERE entity=?", (entity_key,))}
    now       = datetime.now().isoformat()
    ops       = []
    new_count = 0

    for hdr in headers:
        auto_idx = hdr[0]
        inv_num  = to_str(hdr[1])
        acct_id  = hdr[2]
        acct_nm  = to_str(hdr[3])
        inv_date = hdr[4]
        excl     = to_float(hdr[5])
        tax      = to_float(hdr[6])
        desc     = to_str(hdr[8])
        doc_type = hdr[9]
        addr1    = to_str(hdr[10]) if len(hdr) > 10 else ""
        addr2    = to_str(hdr[11]) if len(hdr) > 11 else ""
        addr3    = to_str(hdr[12]) if len(hdr) > 12 else ""
        cust_tin_raw   = to_str(hdr[13]) if len(hdr) > 13 else ""
        cust_tel_raw   = to_str(hdr[14]) if len(hdr) > 14 else ""
        cust_email_raw = to_str(hdr[15]) if len(hdr) > 15 else ""
        link_num = hdr[16] if has_link_col and len(hdr) > 16 else None
        cancel_ref = link_map.get(int(link_num), "") if link_num else ""

        inv_date_str = (
            inv_date.strftime("%Y-%m-%d") if isinstance(inv_date, (datetime, date))
            else str(inv_date)[:10]
        )

        inv_type  = "Credit Note" if doc_type == GENESIS_DOCTYPE_CREDIT_NOTE else "Invoice"
        cust      = client_map.get(acct_id, {})
        cust_name = cust.get("name") or acct_nm or f"Account {acct_id}"
        cust_id   = cust.get("id")   or to_str(acct_id)

        street = ", ".join(p for p in [addr1, addr2] if p) or cust.get("address", "")
        city   = addr3 or cust.get("city", "")
        tin    = cust_tin_raw   or cust.get("tin",   "")
        phone  = cust_tel_raw   or cust.get("phone", "")
        email  = cust_email_raw or cust.get("email", "")

        if auto_idx in existing:
            if existing[auto_idx] != "posted":
                ops.append((
                    "UPDATE invoices SET invoice_num=?,customer_name=?,customer_id=?,"
                    "customer_tin=?,customer_email=?,customer_phone=?,customer_address=?,"
                    "customer_city=?,invoice_date=?,amount=?,vat_amount=?,"
                    "invoice_description=?,invoice_type=?,cancel_ref=?,last_synced=? "
                    "WHERE post_order=? AND entity=?",
                    (inv_num, cust_name, cust_id, tin, email, phone,
                     street, city, inv_date_str, excl, tax, desc, inv_type, cancel_ref, now,
                     auto_idx, entity_key),
                ))
        else:
            new_count += 1
            ops.append((
                "INSERT INTO invoices "
                "(post_order,entity,trx_number,invoice_num,customer_name,customer_id,"
                "customer_tin,customer_email,customer_phone,customer_address,customer_city,"
                "invoice_date,amount,vat_amount,status,invoice_description,invoice_type,cancel_ref,last_synced) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (auto_idx, entity_key, auto_idx, inv_num, cust_name, cust_id, tin, email, phone,
                 street, city, inv_date_str, excl, tax, desc, inv_type, cancel_ref, now),
            ))

    if ops:
        db_write_many(ops)

    return {
        "ok":        True,
        "synced":    len(headers),
        "new":       new_count,
        "date_from": date_from,
        "date_to":   date_to,
        "filtered":  use_filter,
    }


# ─── FETCH LINE ITEMS ─────────────────────────────────────────────────────────

def fetch_line_items(auto_index):
    try:
        sage = pyodbc.connect(GENESIS_DB_CONN_STR, timeout=15)
    except Exception as e:
        return [], 0, f"DB: {e}"

    try:
        cursor = sage.cursor()

        LINES_TABLE = "_btblInvoiceLines"

        item_lookup = {}
        try:
            cursor.execute(
                "SELECT StockLink, Code, Description_1 FROM dbo.StkItem WHERE Code IS NOT NULL"
            )
            for r in cursor.fetchall():
                item_lookup[r[0]] = {"code": to_str(r[1]), "desc": to_str(r[2])}
        except Exception as e:
            print(f"[WARN] StkItem lookup failed: {e}")

        cursor.execute(f"""
            SELECT
                il.iLineID,
                il.iStockCodeID,
                il.cDescription,
                il.fQuantity,
                il.fUnitPriceExcl,
                il.fQuantityLineTotExcl,
                il.fQuantityLineTaxAmount,
                il.fTaxRate,
                il.iTaxTypeID
            FROM dbo.[{LINES_TABLE}] il
            WHERE il.iInvoiceID = ?
            ORDER BY il.iLineID
        """, (auto_index,))

        rows       = cursor.fetchall()
        lines      = []
        vat_amount = 0.0

        for row in rows:
            line_id    = row[0]
            stock_id   = row[1]
            line_desc  = to_str(row[2])
            qty        = to_float(row[3])
            unit_price = to_float(row[4])
            excl_amt   = to_float(row[5])
            line_tax   = to_float(row[6])
            tax_rate_f = to_float(row[7])
            tax_type   = row[8]

            item_info = item_lookup.get(stock_id, {})
            item_code = item_info.get("code", "") or (str(stock_id) if stock_id else "")
            if not line_desc:
                line_desc = item_info.get("desc", "") or item_code or "Food / Catering"

            if unit_price == 0 and qty and excl_amt:
                unit_price = abs(excl_amt / qty)
            elif unit_price == 0:
                unit_price = abs(excl_amt)

            if tax_rate_f > 0:
                tax_rate = tax_rate_f
            elif line_tax > 0 and excl_amt:
                tax_rate = round((line_tax / excl_amt) * 100, 2)
            elif tax_type == 1:
                tax_rate = 7.5
            else:
                tax_rate = 0.0

            vat_amount += abs(line_tax)

            if excl_amt != 0 or unit_price != 0:
                lines.append({
                    "item_code":   item_code or "ITEM",
                    "description": line_desc or "Food / Catering",
                    "quantity":    abs(qty) if qty else 1,
                    "unit_price":  abs(unit_price),
                    "amount":      abs(excl_amt) if excl_amt else abs(unit_price),
                    "tax_rate":    tax_rate,
                })

        print(f"[LINES] AutoIndex={auto_index} → {len(lines)} lines, VAT={vat_amount}")
        return lines, vat_amount, None

    except Exception as e:
        return [], 0, str(e)
    finally:
        sage.close()


# ─── BUILD PAYLOAD ────────────────────────────────────────────────────────────

def build_payload(auto_index, entity_key, entity):
    inv = db_read_one("SELECT * FROM invoices WHERE post_order=? AND entity=?", (auto_index, entity_key))
    if not inv:
        return None, [], 0, "Invoice not found"

    isic_code    = entity["isic_code"]
    product_cat  = entity["product_category"]
    default_city = entity["supplier"]["postal_address"].get("city_name", "Port Harcourt")

    lines, vat_amount, line_error = fetch_line_items(inv["post_order"])

    if not lines:
        amt = abs(to_float(inv["amount"]))
        if amt > 0:
            lines = [{
                "item_code":   inv["invoice_num"] or f"INV-{auto_index}",
                "description": to_str(inv.get("invoice_description")) or "Services",
                "quantity":    1,
                "unit_price":  amt,
                "amount":      amt,
                "tax_rate":    7.5,
            }]
            if vat_amount == 0:
                vat_amount = to_float(inv.get("vat_amount", 0)) or round(amt * 0.075, 2)
        else:
            return None, [], 0, line_error or "No line items"

    cust_tin   = to_str(inv["customer_tin"])   or "00000000-0001"
    cust_email = to_str(inv["customer_email"]) or "noemail@placeholder.com"
    cust_phone = to_e164(inv["customer_phone"])
    if not cust_phone or cust_phone == "+234":
        cust_phone = "+2340000000000"
    cust_name    = to_str(inv["customer_name"])    or "Customer"
    cust_address = to_str(inv["customer_address"]) or "N/A"
    cust_city    = to_str(inv["customer_city"])    or default_city

    subtotal = sum(l["amount"] for l in lines)

    inv_type = inv.get("invoice_type") or "Invoice"
    if inv_type == "Credit Note":  type_code = "380"
    elif inv_type == "Debit Note": type_code = "384"
    else:                          type_code = "381"

    inv_num      = inv["invoice_num"] or f"INV-{auto_index}"
    inv_num_safe = re.sub(r'[^A-Za-z0-9\-_]', '-', inv_num)

    api_lines = []
    for line in lines:
        if line["unit_price"] <= 0:
            continue
        lr   = line.get("tax_rate", 0)
        desc = to_str(line["description"]) or "Item"
        api_lines.append({
            "description":       desc,
            "invoiced_quantity": line["quantity"],
            "price_amount":      line["unit_price"],
            "isic_code":         isic_code,
            "price_unit":        "EA",
            "product_category":  product_cat,
            "tax_rate":          lr,
            "tax_category_id":   TAX_CAT_STANDARD if lr > 0 else TAX_CAT_EXEMPT,
            "discount_rate":     0,
        })

    if not api_lines:
        return None, lines, vat_amount, "No valid line items"

    payload = {
        "document_identifier":    inv_num_safe,
        "invoice_type":           "STANDARD",
        "issue_date":             inv["invoice_date"],
        "due_date":               inv["invoice_date"],
        "invoice_type_code":      type_code,
        "document_currency_code": "NGN",
        "transaction_category":   "B2B",
        "accounting_customer_party": {
            "party_name":           cust_name,
            "tin":                  cust_tin,
            "email":                cust_email,
            "telephone":            cust_phone,
            "business_description": "Customer",
            "postal_address": {
                "street_name": cust_address,
                "city_name":   cust_city,
                "postal_zone": "000001",
                "country":     "NG",
            },
        },
        "invoice_lines": api_lines,
    }

    if inv_type == "Credit Note":
        cancel_ref = to_str(inv.get("cancel_ref")) or ""
        if cancel_ref:
            orig = db_read_one(
                "SELECT irn, invoice_date FROM invoices WHERE invoice_num=? AND entity=?",
                (cancel_ref, entity_key))
            ref_id   = (orig["irn"] if orig and orig.get("irn") else None) \
                       or re.sub(r'[^A-Za-z0-9\-_]', '-', cancel_ref)
            ref_date = orig["invoice_date"] if orig else inv["invoice_date"]
        else:
            ref_id   = inv_num_safe
            ref_date = inv["invoice_date"]
        payload["cancel_references"] = [{"original_irn": ref_id, "original_issue_date": ref_date}]

    return payload, lines, vat_amount, None


# ─── POST TO FIRS ─────────────────────────────────────────────────────────────

def post_to_firs(auto_index, entity_key, entity):
    inv = db_read_one("SELECT * FROM invoices WHERE post_order=? AND entity=?", (auto_index, entity_key))
    if not inv:
        return {"ok": False, "error": "Not found"}
    if inv["status"] == "posted":
        return {"ok": False, "error": "Already posted", "irn": inv["irn"]}

    api_url, api_headers = entity_api(entity)

    payload, lines, vat_amount, build_error = build_payload(auto_index, entity_key, entity)
    if not payload:
        db_write("UPDATE invoices SET status='failed', error_message=? WHERE post_order=? AND entity=?",
                 (build_error[:500], auto_index, entity_key))
        return {"ok": False, "error": build_error}

    ops = [
        ("DELETE FROM invoice_lines WHERE post_order=?", (auto_index,)),
        ("UPDATE invoices SET vat_amount=? WHERE post_order=? AND entity=?", (vat_amount, auto_index, entity_key)),
    ]
    for i, line in enumerate(lines):
        ops.append((
            "INSERT INTO invoice_lines "
            "(post_order,trx_number,line_num,item_code,description,quantity,unit_price,amount,tax_rate) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (auto_index, auto_index, i+1, line["item_code"], line["description"],
             line["quantity"], line["unit_price"], line["amount"], line.get("tax_rate", 0)),
        ))
    db_write_many(ops)

    try:
        resp      = requests.post(f"{api_url}/invoice/generate", headers=api_headers, json=payload, timeout=30)
        resp_text = resp.text
        resp_json = {}
        try:
            resp_json = resp.json()
        except:
            pass

        if resp.status_code in (200, 201):
            data    = resp_json.get("data", resp_json)
            irn     = data.get("irn", "N/A")
            qr_code = data.get("qr_code_url", "") or data.get("qr_code", "")
            db_write(
                "UPDATE invoices SET status='posted', irn=?, qr_code=?, posted_at=?, "
                "error_message=NULL, api_response=? WHERE post_order=? AND entity=?",
                (irn, qr_code, datetime.now().isoformat(), resp_text[:5000], auto_index, entity_key),
            )
            generate_pdf(auto_index, entity_key, entity)
            return {"ok": True, "irn": irn, "status": "posted"}

        elif resp.status_code == 409:
            errors  = resp_json.get("errors", {})
            irn     = errors.get("irn",     resp_json.get("irn", ""))
            qr_code = errors.get("qr_code", resp_json.get("qr_code_url", "") or resp_json.get("qr_code", ""))
            if irn:
                db_write(
                    "UPDATE invoices SET status='posted', irn=?, qr_code=?, posted_at=?, "
                    "error_message=NULL, api_response=? WHERE post_order=? AND entity=?",
                    (irn, qr_code, datetime.now().isoformat(), resp_text[:5000], auto_index, entity_key),
                )
                generate_pdf(auto_index, entity_key, entity)
                return {"ok": True, "irn": irn, "status": "posted", "note": "Already on FIRS"}
            error_msg = resp_json.get("message", "409 conflict")
            db_write(
                "UPDATE invoices SET status='failed', error_message=?, api_response=? WHERE post_order=? AND entity=?",
                (error_msg[:500], resp_text[:5000], auto_index, entity_key),
            )
            return {"ok": False, "error": error_msg, "status_code": 409, "api_response": resp_json}

        else:
            error_msg = resp_json.get("message", resp_text[:300])
            db_write(
                "UPDATE invoices SET status='failed', error_message=?, api_response=? WHERE post_order=? AND entity=?",
                (error_msg[:500], resp_text[:5000], auto_index, entity_key),
            )
            return {"ok": False, "error": error_msg, "status_code": resp.status_code, "api_response": resp_json}

    except requests.exceptions.ConnectionError as e:
        db_write("UPDATE invoices SET status='failed', error_message=? WHERE post_order=? AND entity=?",
                 (f"Connection: {str(e)[:200]}", auto_index, entity_key))
        return {"ok": False, "error": f"Connection failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── PDF GENERATION ───────────────────────────────────────────────────────────

def generate_pdf(auto_index, entity_key, entity):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib.utils import ImageReader

    supplier = entity_supplier(entity)

    inv   = db_read_one("SELECT * FROM invoices WHERE post_order=? AND entity=?", (auto_index, entity_key))
    lines = db_read("SELECT * FROM invoice_lines WHERE post_order=? ORDER BY line_num", (auto_index,))
    if not inv:
        return None

    qr_img_reader = None
    if inv["qr_code"]:
        try:
            import qrcode
            qr = qrcode.QRCode(version=1, box_size=4, border=2)
            qr.add_data(inv["qr_code"]); qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
            qr_img_reader = ImageReader(buf)
        except:
            pass

    safe_name = (inv["invoice_num"] or f"INV-{auto_index}").replace("/","_").replace("\\","_").replace(" ","_")
    pdf_path  = os.path.join(PDF_DIR, f"{entity_key}_{safe_name}.pdf")
    w, h      = A4
    c         = rl_canvas.Canvas(pdf_path, pagesize=A4)

    navy     = colors.HexColor("#0f172a")
    blue     = colors.HexColor("#2563eb")
    slate50  = colors.HexColor("#f8fafc")
    slate200 = colors.HexColor("#e2e8f0")
    slate500 = colors.HexColor("#64748b")
    slate800 = colors.HexColor("#1e293b")
    green    = colors.HexColor("#16a34a")

    y = h - 30
    c.setFillColor(navy);         c.rect(0, y-60, w, 70, fill=True, stroke=False)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 16); c.drawString(30, y-25, supplier["name"])
    c.setFont("Helvetica", 9);    c.drawString(30, y-42, supplier["address"])
    c.setFillColor(green);        c.roundRect(w-145, y-47, 115, 30, 4, fill=True, stroke=False)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 11); c.drawCentredString(w-87, y-37, "E-INVOICE")
    y -= 85

    doc_label = "CREDIT NOTE" if inv.get("invoice_type") == "Credit Note" else "INVOICE"
    c.setFillColor(slate800); c.setFont("Helvetica-Bold", 22); c.drawString(30, y, doc_label); y -= 25

    for label, val in [
        ("Invoice No:", inv["invoice_num"]),
        ("Date:",       inv["invoice_date"]),
        ("IRN:",        inv["irn"] or "Pending"),
        ("Currency:",   "NGN"),
    ]:
        c.setFont("Helvetica-Bold", 9); c.setFillColor(slate500); c.drawString(30,  y, label)
        c.setFont("Helvetica",      9); c.setFillColor(slate800); c.drawString(115, y, str(val))
        y -= 15

    if qr_img_reader:
        c.drawImage(qr_img_reader, w-140, y+5, 105, 105)
    y -= 15

    c.setFillColor(slate50);     c.rect(25, y-55, w-50, 60, fill=True,  stroke=False)
    c.setStrokeColor(slate200);  c.rect(25, y-55, w-50, 60, fill=False, stroke=True)
    c.setFillColor(blue);        c.setFont("Helvetica-Bold", 9);  c.drawString(35, y-5,  "BILL TO")
    c.setFillColor(slate800);    c.setFont("Helvetica-Bold", 11); c.drawString(35, y-20, inv["customer_name"] or "")
    c.setFont("Helvetica", 8);   c.setFillColor(slate500)
    addr = f"{inv['customer_address'] or ''}, {inv['customer_city'] or ''}".strip(", ")
    c.drawString(35, y-34, addr[:80])
    if inv["customer_tin"]:
        c.drawString(35, y-46, f"TIN: {inv['customer_tin']}")
    c.drawRightString(w-35, y-20, inv["customer_email"] or "")
    c.drawRightString(w-35, y-34, inv["customer_phone"] or "")
    y -= 75

    c.setFillColor(slate800); c.setFont("Helvetica-Bold", 10); c.drawString(30, y, "Line Items"); y -= 5
    table_data = [["#", "Description", "Qty", "Unit Price (N)", "Tax", "Amount (N)"]]
    total = 0.0
    for line in lines:
        qty   = line["quantity"]; price = line["unit_price"]; amt = qty * price; total += amt
        lr    = to_float(line.get("tax_rate", 0))
        table_data.append([
            str(line["line_num"]),
            (line["description"] or "Item")[:40],
            f"{qty:g}", f"{price:,.2f}",
            f"{lr:g}%" if lr > 0 else "0%",
            f"{amt:,.2f}",
        ])

    col_widths = [25, 220, 35, 85, 40, 85]
    max_rows   = int((y - 120) / 16)
    header_row = table_data[0]
    data_rows  = table_data[1:]
    page_num   = 1

    while data_rows:
        chunk     = data_rows[:max_rows]; data_rows = data_rows[max_rows:]
        page_data = [header_row] + chunk
        t = Table(page_data, colWidths=col_widths)
        t.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0),  navy),
            ("TEXTCOLOR",    (0,0), (-1,0),  colors.white),
            ("FONTNAME",     (0,0), (-1,0),  "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,0),  8),
            ("FONTNAME",     (0,1), (-1,-1), "Helvetica"),
            ("FONTSIZE",     (0,1), (-1,-1), 7.5),
            ("TEXTCOLOR",    (0,1), (-1,-1), slate800),
            ("ALIGN",        (0,0), (0,-1),  "CENTER"),
            ("ALIGN",        (2,0), (-1,-1), "RIGHT"),
            *[("BACKGROUND", (0,i), (-1,i),  slate50) for i in range(2, len(page_data), 2)],
            ("LINEBELOW",    (0,0), (-1,0),  1,   navy),
            ("LINEBELOW",    (0,-1),(-1,-1), 0.5, slate200),
            ("TOPPADDING",   (0,0), (-1,-1), 3),
            ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ]))
        tw, th = t.wrap(0, 0); t.drawOn(c, 30, y-th); y -= th + 10
        if data_rows:
            c.setFont("Helvetica", 7); c.setFillColor(slate500)
            c.drawRightString(w-30, 25, f"Page {page_num}")
            c.showPage(); page_num += 1; y = h - 50
            c.setFillColor(slate800); c.setFont("Helvetica-Bold", 10)
            c.drawString(30, y, "Line Items (continued)"); y -= 5
            max_rows = int((y - 120) / 16)

    y -= 10
    stored_vat = to_float(inv.get("vat_amount", 0))
    tax_amt    = stored_vat if stored_vat > 0 else round(
        sum(l["quantity"] * l["unit_price"] * (to_float(l.get("tax_rate", 0)) / 100)
            for l in lines if to_float(l.get("tax_rate", 0)) > 0), 2)
    grand  = total + tax_amt
    tx, bw = w - 230, 200

    c.setFillColor(slate50);    c.rect(tx, y-65, bw, 70, fill=True,  stroke=False)
    c.setStrokeColor(slate200); c.rect(tx, y-65, bw, 70, fill=False, stroke=True)
    c.setFont("Helvetica", 9);  c.setFillColor(slate500)
    c.drawString(tx+10, y-8,  "Subtotal:"); c.drawString(tx+10, y-23, "VAT:")
    c.setFillColor(slate800)
    c.drawRightString(tx+bw-10, y-8,  f"N{total:,.2f}")
    c.drawRightString(tx+bw-10, y-23, f"N{tax_amt:,.2f}")
    c.setStrokeColor(navy); c.line(tx+10, y-33, tx+bw-10, y-33)
    c.setFont("Helvetica-Bold", 11); c.setFillColor(navy)
    c.drawString(tx+10, y-50, "TOTAL:")
    c.drawRightString(tx+bw-10, y-50, f"N{grand:,.2f}")

    c.setFillColor(navy); c.rect(0, 0, w, 45, fill=True, stroke=False)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 8); c.drawString(30, 28, f"IRN: {inv['irn'] or 'Pending'}")
    c.setFont("Helvetica",      7); c.drawString(30, 15, "System-generated e-invoice. Validated by FIRS.")
    c.drawRightString(w-30, 15, f"Page {page_num}")
    c.save()
    return pdf_path


# ─── AUTH ROUTES ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    # Already logged in -> go to dashboard.
    if current_entity() and request.method == "GET":
        return redirect(url_for("index"))

    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        entity_key = LOGIN_USERS.get(password)
        if entity_key and get_entity(entity_key):
            session["entity_key"] = entity_key
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid password. Please try again.")

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    entity_key = current_entity_key()
    entity     = current_entity()
    entity_label = ENTITY_LABELS.get(entity_key, entity["supplier"]["party_name"])

    page          = request.args.get("page",   1,  type=int)
    q             = request.args.get("q",      "").strip()
    status_filter = request.args.get("status", "").strip()
    today         = date.today()
    default_from  = "2020-01-01"
    default_to    = today.strftime("%Y-%m-%d")
    date_from     = request.args.get("date_from", default_from).strip()
    date_to       = request.args.get("date_to",   default_to).strip()

    try:
        rp        = (entity_key, date_from, date_to)
        all_stats = db_read(
            "SELECT status, COUNT(*) as cnt FROM invoices "
            "WHERE entity=? AND invoice_date >= ? AND invoice_date <= ? GROUP BY status", rp)
        stats = {"total": 0, "posted": 0, "pending": 0, "failed": 0, "credit_notes": 0, "invoices_count": 0}
        for s in all_stats:
            stats[s["status"]] = s["cnt"]; stats["total"] += s["cnt"]

        type_stats = db_read(
            "SELECT invoice_type, COUNT(*) as cnt FROM invoices "
            "WHERE entity=? AND invoice_date >= ? AND invoice_date <= ? GROUP BY invoice_type", rp)
        for t in type_stats:
            if t["invoice_type"] == "Credit Note": stats["credit_notes"]   = t["cnt"]
            elif t["invoice_type"] == "Invoice":   stats["invoices_count"] = t["cnt"]

        where_parts = ["entity = ?", "invoice_date >= ?", "invoice_date <= ?"]
        params      = [entity_key, date_from, date_to]
        if q:
            where_parts.append(
                "(LOWER(customer_name) LIKE ? OR LOWER(customer_id) LIKE ? OR LOWER(invoice_num) LIKE ?)")
            like = f"%{q.lower()}%"
            params += [like, like, like]
        if status_filter in ("pending", "posted", "failed"):
            where_parts.append("status = ?"); params.append(status_filter)

        where_sql   = "WHERE " + " AND ".join(where_parts)
        count_row   = db_read_one(f"SELECT COUNT(*) as cnt FROM invoices {where_sql}", tuple(params))
        total       = count_row["cnt"] if count_row else 0
        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page        = max(1, min(page, total_pages))
        offset      = (page - 1) * PER_PAGE
        invoices    = db_read(
            f"SELECT * FROM invoices {where_sql} ORDER BY post_order DESC LIMIT ? OFFSET ?",
            tuple(params) + (PER_PAGE, offset))
    except Exception as e:
        print(f"[INDEX] error: {e}")
        invoices = []; stats = {"total":0,"posted":0,"pending":0,"failed":0,"credit_notes":0,"invoices_count":0}
        total = 0; total_pages = 1; page = 1

    return render_template(
        "index.html",
        invoices=invoices, stats=stats,
        page=page, total_pages=total_pages, total=total,
        q=q, status_filter=status_filter,
        date_from=date_from, date_to=date_to,
        entity_label=entity_label,
    )


@app.route("/api/sync", methods=["POST"])
@login_required
def api_sync():
    entity_key = current_entity_key()
    entity     = current_entity()
    data = request.get_json(silent=True) or {}
    return jsonify(sync_from_sage(entity_key, entity,
                                  date_from=data.get("date_from"), date_to=data.get("date_to")))


@app.route("/api/post/<int:auto_index>", methods=["POST"])
@login_required
def api_post(auto_index):
    return jsonify(post_to_firs(auto_index, current_entity_key(), current_entity()))


@app.route("/api/post-bulk", methods=["POST"])
@login_required
def api_post_bulk():
    entity_key = current_entity_key()
    entity     = current_entity()
    pending = db_read("SELECT post_order FROM invoices WHERE status='pending' AND entity=?", (entity_key,))
    results = []
    for row in pending:
        results.append({"id": row["post_order"], **post_to_firs(row["post_order"], entity_key, entity)})
    posted = sum(1 for r in results if r.get("ok"))
    return jsonify({"ok": True, "posted": posted, "failed": len(results)-posted, "details": results})


@app.route("/api/preview-payload/<int:auto_index>")
@login_required
def api_preview_payload(auto_index):
    entity_key = current_entity_key()
    entity     = current_entity()
    inv = db_read_one("SELECT * FROM invoices WHERE post_order=? AND entity=?", (auto_index, entity_key))
    if not inv: return jsonify({"ok": False, "error": "Invoice not found"})
    payload, lines, vat_amount, error = build_payload(auto_index, entity_key, entity)
    if not payload: return jsonify({"ok": False, "error": error or "Failed to build payload"})
    subtotal = sum(l["amount"] for l in lines)
    api_url, _ = entity_api(entity)
    return jsonify({
        "ok": True, "invoice_num": inv["invoice_num"],
        "customer_name": inv["customer_name"], "post_order": auto_index,
        "subtotal": subtotal, "vat_amount": vat_amount,
        "grand_total": subtotal + vat_amount, "lines_count": len(lines),
        "api_url": f"{api_url}/invoice/generate",
        "payload": payload,
    })


@app.route("/api/error-details/<int:auto_index>")
@login_required
def api_error_details(auto_index):
    entity_key = current_entity_key()
    inv = db_read_one(
        "SELECT post_order, invoice_num, customer_name, status, error_message, api_response "
        "FROM invoices WHERE post_order=? AND entity=?", (auto_index, entity_key))
    if not inv: return jsonify({"ok": False, "error": "Invoice not found"})
    try:
        import json as _j; parsed = _j.loads(inv.get("api_response") or "")
    except:
        parsed = inv.get("api_response", "")
    return jsonify({
        "ok": True, "post_order": auto_index,
        "invoice_num": inv["invoice_num"], "customer_name": inv["customer_name"],
        "status": inv["status"], "error_message": inv["error_message"] or "",
        "api_response": parsed,
    })


@app.route("/api/tax-categories", methods=["GET", "POST"])
@login_required
def api_tax_categories():
    global TAX_CAT_STANDARD, TAX_CAT_EXEMPT
    if request.method == "POST":
        data = request.json or {}
        if "standard" in data: TAX_CAT_STANDARD = data["standard"]
        if "exempt"   in data: TAX_CAT_EXEMPT   = data["exempt"]
        return jsonify({"ok": True, "standard": TAX_CAT_STANDARD, "exempt": TAX_CAT_EXEMPT})
    return jsonify({"standard": TAX_CAT_STANDARD, "exempt": TAX_CAT_EXEMPT})


@app.route("/api/stats")
@login_required
def api_stats():
    entity_key = current_entity_key()
    try:
        rows  = db_read("SELECT status, COUNT(*) as cnt FROM invoices WHERE entity=? GROUP BY status", (entity_key,))
        stats = {"total": 0, "posted": 0, "pending": 0, "failed": 0}
        for r in rows:
            stats[r["status"]] = r["cnt"]; stats["total"] += r["cnt"]
        return jsonify({"ok": True, **stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/debug-lines/<int:auto_index>")
@login_required
def api_debug_lines(auto_index):
    entity_key       = current_entity_key()
    inv              = db_read_one("SELECT * FROM invoices WHERE post_order=? AND entity=?", (auto_index, entity_key))
    lines, vat, err  = fetch_line_items(auto_index)
    subtotal         = sum(l["amount"] for l in lines)
    return jsonify({
        "post_order":  auto_index,
        "invoice":     {"invoice_num": inv["invoice_num"], "customer_name": inv["customer_name"],
                        "amount": inv["amount"]} if inv else None,
        "lines_found": len(lines), "lines": lines[:20],
        "vat_amount":  vat, "subtotal": subtotal, "grand_total": subtotal + vat, "error": err,
    })


@app.route("/api/debug-schema")
@login_required
def api_debug_schema():
    """Lists all tables + probes invoice-related ones for column names."""
    try:
        sage   = pyodbc.connect(GENESIS_DB_CONN_STR, timeout=10)
        cursor = sage.cursor()

        cursor.execute("""
            SELECT TABLE_NAME, (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS c
                                WHERE c.TABLE_NAME = t.TABLE_NAME) AS col_count
            FROM INFORMATION_SCHEMA.TABLES t
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
        """)
        all_tables = {r[0]: r[1] for r in cursor.fetchall()}

        candidates = [t for t in all_tables if any(k in t.lower() for k in
                      ("inv", "line", "detail", "row", "item", "doc", "stk", "stock"))]
        table_info = {}
        for tbl in candidates:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM dbo.[{tbl}]")
                cnt = cursor.fetchone()[0]
                cursor.execute(f"SELECT TOP 0 * FROM dbo.[{tbl}]")
                cols = [d[0] for d in cursor.description]
                table_info[tbl] = {"rows": cnt, "columns": cols}
            except Exception as e:
                table_info[tbl] = {"error": str(e)}

        sample_invnum_id = None
        try:
            cursor.execute("SELECT TOP 1 AutoIndex FROM dbo.InvNum WHERE DocType = 0 ORDER BY InvDate DESC")
            row = cursor.fetchone()
            if row:
                sample_invnum_id = row[0]
        except:
            pass

        sage.close()
        return jsonify({
            "ok": True,
            "all_tables": list(all_tables.keys()),
            "invoice_related_tables": table_info,
            "sample_AutoIndex_for_testing": sample_invnum_id,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/debug-sage")
@login_required
def api_debug_sage():
    """Shows what's actually in dbo.InvNum — use this to verify the sync query."""
    try:
        sage   = pyodbc.connect(GENESIS_DB_CONN_STR, timeout=10)
        cursor = sage.cursor()

        cursor.execute("""
            SELECT DocType, COUNT(*) as cnt,
                   MIN(CONVERT(varchar,InvDate,23)) as earliest,
                   MAX(CONVERT(varchar,InvDate,23)) as latest
            FROM dbo.InvNum
            GROUP BY DocType
            ORDER BY DocType
        """)
        doctype_summary = [
            {"DocType": r[0], "count": r[1], "earliest": r[2], "latest": r[3]}
            for r in cursor.fetchall()
        ]

        cursor.execute("""
            SELECT TOP 5 AutoIndex, InvNumber, cAccountName, InvDate,
                         InvTotExcl, InvTotTax, DocType
            FROM dbo.InvNum WHERE DocType = 0 ORDER BY InvDate DESC
        """)
        inv_samples = [
            {"AutoIndex": r[0], "InvNumber": r[1], "Customer": r[2],
             "Date": str(r[3])[:10], "Excl": float(r[4] or 0), "Tax": float(r[5] or 0)}
            for r in cursor.fetchall()
        ]

        cursor.execute("""
            SELECT TOP 5 AutoIndex, InvNumber, cAccountName, InvDate,
                         InvTotExcl, InvTotTax, DocType
            FROM dbo.InvNum WHERE DocType = 1 ORDER BY InvDate DESC
        """)
        cn_samples = [
            {"AutoIndex": r[0], "InvNumber": r[1], "Customer": r[2],
             "Date": str(r[3])[:10], "Excl": float(r[4] or 0), "Tax": float(r[5] or 0)}
            for r in cursor.fetchall()
        ]

        sage.close()
        return jsonify({
            "ok": True,
            "doctype_summary": doctype_summary,
            "invoice_samples (DocType 0)": inv_samples,
            "credit_note_samples (DocType 1)": cn_samples,
            "note": "DocType 0=Invoice, 1=Credit Note. Check 'earliest'/'latest' dates match your sync range.",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/download/<int:auto_index>")
@login_required
def download_pdf(auto_index):
    entity_key = current_entity_key()
    entity     = current_entity()
    inv = db_read_one("SELECT * FROM invoices WHERE post_order=? AND entity=?", (auto_index, entity_key))
    if not inv or inv["status"] != "posted":
        return "Not posted yet", 404
    safe_name = (inv["invoice_num"] or f"INV-{auto_index}").replace("/","_").replace("\\","_").replace(" ","_")
    pdf_path  = os.path.join(PDF_DIR, f"{entity_key}_{safe_name}.pdf")
    if not os.path.exists(pdf_path):
        generate_pdf(auto_index, entity_key, entity)
    if os.path.exists(pdf_path):
        return send_file(pdf_path, as_attachment=True, download_name=f"{safe_name}.pdf")
    return "PDF generation failed", 500


if __name__ == "__main__":
    print("\n  Genesis Group E-Invoicing Dashboard (multi-entity)")
    print("  ===================================================")
    print("  http://localhost:5002")
    print("  Login: 'Food 123' (Food) or 'Cenima 456' (Cinemas)\n")
    app.run(debug=False, host="0.0.0.0", port=5002)