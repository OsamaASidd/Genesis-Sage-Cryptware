"""
Genesis - Locate the "Customer Enquiries -> Transactions" source table
======================================================================
The Sage screen "Customer Enquiries / Transactions" for branch IC shows rows
with Code = JC (journal) and CASH, References like 'FR 76700', 'ICARBR1654',
'OR 1027', 'PV 3653', descriptions like 'PURCHASE OF CRAB'.

These look like DEBTOR / AR LEDGER transactions (journals + cash), NOT sales
invoices in dbo.InvNum. This script finds WHICH table they live in by:

  1. Searching every text column of every table for the literal reference
     strings visible on screen.
  2. Reporting the table + column where each reference is found.
  3. For the strongest hit, dumping that table's columns and a few sample rows.

Run against GGNL-LIVE.
"""

import sys
import pyodbc
from config import GENESIS_FOOD_DB, _conn_str

DIVIDER = "=" * 80

# Exact strings visible on the screenshot. Add/trim as needed.
NEEDLES = [
    "FR 76700",
    "ICARBR1654",
    "FR 76688",
    "OR 1027",
    "PV 3653",
    "PURCHASE OF CRAB AND TRANSPORT",
]

# Only scan reasonably-sized text columns (skip huge blob/binary types).
TEXT_TYPES = ("char", "varchar", "nchar", "nvarchar", "text", "ntext")


def connect():
    try:
        return pyodbc.connect(_conn_str(GENESIS_FOOD_DB), timeout=20)
    except pyodbc.Error as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


def list_text_columns(cur):
    """Return [(table, column)] for all character columns in dbo."""
    cur.execute(f"""
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE,
               COALESCE(CHARACTER_MAXIMUM_LENGTH, 0) AS maxlen
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dbo'
          AND DATA_TYPE IN ({",".join("'"+t+"'" for t in TEXT_TYPES)})
        ORDER BY TABLE_NAME, COLUMN_NAME
    """)
    cols = []
    for tbl, col, dtype, maxlen in cur.fetchall():
        # Skip -1 (MAX) giant columns to keep the scan fast; keep <= 256-char cols.
        if maxlen == -1 or maxlen > 256:
            continue
        cols.append((tbl, col))
    return cols


def search_for_needles(cur, columns):
    """For each needle, find tables/columns containing it. Returns hit dict."""
    hits = {}  # needle -> list of (table, column, sample_count)
    total = len(columns)
    print(f"Scanning {total} text columns for {len(NEEDLES)} reference strings...\n")

    for i, (tbl, col) in enumerate(columns, 1):
        if i % 200 == 0:
            print(f"   ...{i}/{total} columns scanned")
        # Build one query per column that tests all needles at once (OR).
        likeclauses = " OR ".join([f"[{col}] LIKE ?" for _ in NEEDLES])
        params = [f"%{n}%" for n in NEEDLES]
        try:
            cur.execute(f"SELECT TOP 1 1 FROM dbo.[{tbl}] WHERE {likeclauses}", params)
            if cur.fetchone():
                # Column has at least one needle. Now find which needle(s).
                for n in NEEDLES:
                    cur.execute(f"SELECT COUNT(*) FROM dbo.[{tbl}] WHERE [{col}] LIKE ?", (f"%{n}%",))
                    c = cur.fetchone()[0]
                    if c:
                        hits.setdefault(n, []).append((tbl, col, c))
        except Exception:
            # Some tables/columns may be unreadable; skip silently.
            continue
    return hits


def dump_table(cur, tbl, n=5):
    print(f"\n{DIVIDER}")
    print(f"TABLE: dbo.[{tbl}]")
    print(DIVIDER)
    try:
        cur.execute(f"SELECT COUNT(*) FROM dbo.[{tbl}]")
        print(f"  row count: {cur.fetchone()[0]}")
        cur.execute(f"SELECT TOP 0 * FROM dbo.[{tbl}]")
        cols = [d[0] for d in cur.description]
        print(f"  columns ({len(cols)}): {', '.join(cols)}")
        cur.execute(f"SELECT TOP {n} * FROM dbo.[{tbl}]")
        print(f"\n  sample rows:")
        for row in cur.fetchall():
            vals = []
            for cv in zip(cols, row):
                c, v = cv
                if v not in (None, "", 0, 0.0, False):
                    sv = str(v)
                    if len(sv) > 30:
                        sv = sv[:30] + "..."
                    vals.append(f"{c}={sv}")
            print("   " + " | ".join(vals[:12]))
    except Exception as e:
        print(f"  (could not dump: {e})")


def main():
    conn = connect()
    cur = conn.cursor()
    print(f"Connected to {GENESIS_FOOD_DB}.\n")

    columns = list_text_columns(cur)
    hits = search_for_needles(cur, columns)

    print(f"\n{DIVIDER}")
    print("RESULTS - where the on-screen references were found")
    print(DIVIDER)

    if not hits:
        print("  No matches. The references may be in numeric columns, a different")
        print("  database, or computed at display time. Tell me and we widen the scan.")
        conn.close()
        return

    tables_seen = {}
    for needle, locs in hits.items():
        print(f"\n  '{needle}':")
        for tbl, col, cnt in locs:
            print(f"      dbo.{tbl}.{col}   ({cnt} rows)")
            tables_seen[tbl] = tables_seen.get(tbl, 0) + 1

    # Dump the most-hit table(s) for inspection.
    print(f"\n{DIVIDER}")
    print("MOST LIKELY SOURCE TABLE(S) - dumping schema + samples")
    print(DIVIDER)
    for tbl, _ in sorted(tables_seen.items(), key=lambda x: -x[1])[:3]:
        dump_table(cur, tbl)

    conn.close()
    print(f"\n{DIVIDER}")
    print("NEXT: that table is the customer/AR transaction ledger. We then check")
    print("whether these JC/CASH rows have any link to dbo.InvNum (real sales) or")
    print("are journal/expense entries that must NOT be filed to FIRS.")
    print(DIVIDER)


if __name__ == "__main__":
    main()