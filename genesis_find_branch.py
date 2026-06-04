"""
Genesis GGNL-LIVE - Branch / Entity Column Finder
==================================================
Both Genesis Food and Genesis Cinemas live in ONE database (dbo.InvNum).
This script finds the column that separates them.

Strategy:
  1. List every column in dbo.InvNum.
  2. For each "low-cardinality" column (few distinct values), show the
     distinct values + counts. A branch/division/store column will have a
     handful of values, not thousands.
  3. For the top candidates, print sample customer names per value so you
     can tell which value belongs to Food vs Cinemas.

After running:
  - Set ENTITY_FILTER_COLUMN  in config.py to the winning column name.
  - Set GENESIS_FOOD["filter_value"]    to the value that = Food.
  - Set GENESIS_CINEMAS["filter_value"] to the value that = Cinemas.
"""

import sys
import pyodbc
from config import GENESIS_DB_CONN_STR

DIVIDER = "=" * 80

# Columns whose NAME hints at branch/entity separation — checked first.
NAME_HINTS = (
    "branch", "store", "division", "site", "location", "company",
    "entity", "warehouse", "depot", "outlet", "region", "unit",
    "project", "dept", "department", "cc", "costcentre", "costcenter",
)

# How many distinct values still counts as "a separator" (not a free-text field).
MAX_DISTINCT = 60


def connect():
    try:
        return pyodbc.connect(GENESIS_DB_CONN_STR, timeout=15)
    except pyodbc.Error as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


def get_columns(cur):
    cur.execute("SELECT TOP 0 * FROM dbo.InvNum")
    return [(d[0], d[1]) for d in cur.description]  # (name, type_code)


def distinct_count(cur, col):
    try:
        cur.execute(f"SELECT COUNT(DISTINCT [{col}]) FROM dbo.InvNum")
        return cur.fetchone()[0]
    except Exception:
        return None


def value_breakdown(cur, col, limit=MAX_DISTINCT):
    cur.execute(f"""
        SELECT TOP {limit} [{col}] AS val, COUNT(*) AS cnt
        FROM dbo.InvNum
        GROUP BY [{col}]
        ORDER BY cnt DESC
    """)
    return cur.fetchall()


def sample_customers(cur, col, val, n=6):
    """Show a few customer names for a given column value."""
    try:
        if val is None:
            cur.execute(f"""
                SELECT TOP {n} cAccountName
                FROM dbo.InvNum
                WHERE [{col}] IS NULL AND cAccountName IS NOT NULL
                ORDER BY InvDate DESC
            """)
        else:
            cur.execute(f"""
                SELECT TOP {n} cAccountName
                FROM dbo.InvNum
                WHERE [{col}] = ? AND cAccountName IS NOT NULL
                ORDER BY InvDate DESC
            """, (val,))
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    except Exception as e:
        return [f"(sample failed: {e})"]


def main():
    conn = connect()
    cur = conn.cursor()
    print("Connected to GGNL-LIVE.\n")

    # Total rows for context
    try:
        cur.execute("SELECT COUNT(*) FROM dbo.InvNum")
        total_rows = cur.fetchone()[0]
    except Exception:
        total_rows = "?"

    columns = get_columns(cur)
    print(DIVIDER)
    print(f"dbo.InvNum has {len(columns)} columns, {total_rows} rows")
    print(DIVIDER)
    for name, _ in columns:
        print(f"  {name}")
    print()

    # --- Score every column by distinct-value count -------------------------
    candidates = []   # (name, distinct_count, name_hint_bool)
    print(DIVIDER)
    print("Scanning columns for low-cardinality separators...")
    print(DIVIDER)
    for name, _ in columns:
        dc = distinct_count(cur, name)
        if dc is None:
            continue
        is_hint = any(h in name.lower() for h in NAME_HINTS)
        # A separator: between 2 and MAX_DISTINCT distinct values.
        if 2 <= dc <= MAX_DISTINCT or is_hint:
            candidates.append((name, dc, is_hint))

    # Rank: name-hint columns first, then by fewest distinct values.
    candidates.sort(key=lambda c: (not c[2], c[1] if c[1] is not None else 9999))

    if not candidates:
        print("  No obvious separator columns found.")
        print("  The two entities may not be split inside dbo.InvNum at all —")
        print("  separation might live in the customer/account or a linked table.")
        conn.close()
        return

    print(f"\n  {'Column':<28} {'Distinct':>9}  Name-hint?")
    print(f"  {'-'*28} {'-'*9}  ---------")
    for name, dc, is_hint in candidates:
        print(f"  {name:<28} {str(dc):>9}  {'YES' if is_hint else ''}")
    print()

    # --- Deep dive on the top candidates ------------------------------------
    TOP_N = 8
    print(DIVIDER)
    print(f"VALUE BREAKDOWN + SAMPLE CUSTOMERS (top {TOP_N} candidates)")
    print("Look for a column where one value's customers are clearly FOOD/")
    print("catering/restaurant and another's are CINEMA/entertainment.")
    print(DIVIDER)

    for name, dc, is_hint in candidates[:TOP_N]:
        print(f"\n>>> Column: [{name}]  ({dc} distinct values){'  <-- name hint' if is_hint else ''}")
        print("-" * 70)
        try:
            rows = value_breakdown(cur, name)
        except Exception as e:
            print(f"    (breakdown failed: {e})")
            continue
        for val, cnt in rows:
            shown = "NULL" if val is None else repr(val)
            print(f"    value={shown:<25} rows={cnt}")
            samples = sample_customers(cur, name, val)
            for s in samples:
                print(f"        - {s}")
        print()

    conn.close()
    print(DIVIDER)
    print("NEXT STEPS")
    print(DIVIDER)
    print("1. Pick the column whose values cleanly separate Food vs Cinemas.")
    print("2. In config.py set:")
    print('     ENTITY_FILTER_COLUMN = "<that column name>"')
    print('     GENESIS_FOOD["filter_value"]    = "<value that = Food>"')
    print('     GENESIS_CINEMAS["filter_value"] = "<value that = Cinemas>"')
    print("3. Delete einvoice_genesis.db*, restart, and re-sync each login.")
    print()


if __name__ == "__main__":
    main()