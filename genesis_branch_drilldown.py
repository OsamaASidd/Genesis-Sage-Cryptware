"""
Genesis GGNL-LIVE - Branch ID Drill-Down
=========================================
InvNum_iBranchID (17 distinct values) is the prime separator candidate.
This script shows, per branch ID:
  - row count
  - date range
  - sample customer names
  - top ISIC-ish hints from item descriptions (food vs cinema tells)
so you can map each branch ID -> Food or Cinemas.
"""

import sys
import pyodbc
from config import GENESIS_DB_CONN_STR

DIVIDER = "=" * 80
PRIMARY = "InvNum_iBranchID"
BACKUPS = ["DocRepID", "TillID", "iINVNUMAgentID"]  # other plausible splitters


def connect():
    try:
        return pyodbc.connect(GENESIS_DB_CONN_STR, timeout=15)
    except pyodbc.Error as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


def breakdown(cur, col):
    """Per-value: count, earliest/latest date."""
    cur.execute(f"""
        SELECT [{col}] AS val,
               COUNT(*) AS cnt,
               MIN(CONVERT(varchar,InvDate,23)) AS earliest,
               MAX(CONVERT(varchar,InvDate,23)) AS latest
        FROM dbo.InvNum
        GROUP BY [{col}]
        ORDER BY cnt DESC
    """)
    return cur.fetchall()


def sample_customers(cur, col, val, n=10):
    cur.execute(f"""
        SELECT TOP {n} cAccountName, COUNT(*) AS cnt
        FROM dbo.InvNum
        WHERE [{col}] = ? AND cAccountName IS NOT NULL AND LTRIM(RTRIM(cAccountName)) <> ''
        GROUP BY cAccountName
        ORDER BY cnt DESC
    """, (val,))
    return [(str(r[0]).strip(), r[1]) for r in cur.fetchall()]


def sample_descriptions(cur, col, val, n=8):
    """Invoice-header Description gives a feel for food vs cinema."""
    try:
        cur.execute(f"""
            SELECT TOP {n} Description, COUNT(*) AS cnt
            FROM dbo.InvNum
            WHERE [{col}] = ? AND Description IS NOT NULL AND LTRIM(RTRIM(Description)) <> ''
            GROUP BY Description
            ORDER BY cnt DESC
        """, (val,))
        return [(str(r[0]).strip()[:60], r[1]) for r in cur.fetchall()]
    except Exception:
        return []


def drill(cur, col):
    print(f"\n{DIVIDER}")
    print(f"COLUMN: [{col}]")
    print(DIVIDER)
    try:
        rows = breakdown(cur, col)
    except Exception as e:
        print(f"  (breakdown failed: {e})")
        return

    for val, cnt, earliest, latest in rows:
        shown = "NULL" if val is None else repr(val)
        print(f"\n  ── value={shown}   rows={cnt}   {earliest} → {latest}")
        custs = sample_customers(cur, col, val)
        if custs:
            print("     top customers:")
            for name, c in custs:
                print(f"        {c:>6}x  {name}")
        descs = sample_descriptions(cur, col, val)
        if descs:
            print("     common descriptions:")
            for d, c in descs:
                print(f"        {c:>6}x  {d}")


def main():
    conn = connect()
    cur = conn.cursor()
    print("Connected to GGNL-LIVE.")

    drill(cur, PRIMARY)

    print(f"\n\n{DIVIDER}")
    print("BACKUP CANDIDATES (only if InvNum_iBranchID doesn't separate cleanly)")
    print(DIVIDER)
    for col in BACKUPS:
        try:
            cur.execute(f"SELECT COUNT(DISTINCT [{col}]) FROM dbo.InvNum")
            dc = cur.fetchone()[0]
        except Exception:
            continue
        if 2 <= dc <= 60:
            drill(cur, col)

    conn.close()
    print(f"\n{DIVIDER}")
    print("Map each branch ID to Food or Cinemas using the customer/description")
    print("hints, then set in config.py:")
    print('   ENTITY_FILTER_COLUMN = "InvNum_iBranchID"')
    print('   GENESIS_FOOD["filter_value"]    = <food branch id>')
    print('   GENESIS_CINEMAS["filter_value"] = <cinema branch id>')
    print("NOTE: branch IDs are integers — if Food/Cinemas each span MULTIPLE")
    print("branch IDs, tell me and I'll switch the filter to an IN (...) list.")
    print(DIVIDER)


if __name__ == "__main__":
    main()