"""
Genesis - InvNum vs PostAR coverage check
==========================================
Question: PostAR shows 2026 rows, but InvNum "seems" to stop at 2021.
This checks BOTH tables directly and explains the gap.

It reports:
  1. InvNum: latest date per branch, and per-year row counts overall.
     (Proves InvNum DOES contain 2026 - just maybe not in the branch you sampled.)
  2. InvNum: 2026 rows by branch + invoice-number prefix (what doc types exist).
  3. PostAR: column list + a few 2026 sample rows, and its transaction-code
     breakdown (JC / CASH / INV ...), to show what PostAR holds that InvNum
     does NOT (journals & cash never create an InvNum document).
"""

import sys
import pyodbc
from config import GENESIS_FOOD_DB, _conn_str

DIVIDER = "=" * 80


def connect():
    try:
        return pyodbc.connect(_conn_str(GENESIS_FOOD_DB), timeout=20)
    except pyodbc.Error as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


def invnum_year_counts(cur):
    print(DIVIDER); print("1a. InvNum - rows per YEAR (all DocTypes)"); print(DIVIDER)
    cur.execute("""
        SELECT YEAR(InvDate) AS yr, COUNT(*) AS cnt
        FROM dbo.InvNum
        GROUP BY YEAR(InvDate)
        ORDER BY yr
    """)
    for yr, cnt in cur.fetchall():
        mark = "  <== current" if yr in (2025, 2026) else ""
        print(f"   {yr}: {cnt:>8}{mark}")


def invnum_latest_per_branch(cur):
    print(f"\n{DIVIDER}"); print("1b. InvNum - latest InvDate per branch"); print(DIVIDER)
    cur.execute("""
        SELECT InvNum_iBranchID AS br,
               COUNT(*) AS cnt,
               MAX(CONVERT(varchar,InvDate,23)) AS latest
        FROM dbo.InvNum
        GROUP BY InvNum_iBranchID
        ORDER BY latest DESC
    """)
    for br, cnt, latest in cur.fetchall():
        print(f"   branch {str(br):<4} latest={latest}  rows={cnt}")


def invnum_2026_by_branch_prefix(cur):
    print(f"\n{DIVIDER}"); print("2. InvNum - 2026 rows by branch + invoice prefix + DocType"); print(DIVIDER)
    cur.execute("""
        SELECT InvNum_iBranchID AS br,
               LEFT(InvNumber,3) AS pfx,
               DocType,
               COUNT(*) AS cnt
        FROM dbo.InvNum
        WHERE InvDate >= '2026-01-01'
        GROUP BY InvNum_iBranchID, LEFT(InvNumber,3), DocType
        ORDER BY cnt DESC
    """)
    rows = cur.fetchall()
    if not rows:
        print("   (no 2026 rows in InvNum at all)")
    else:
        print(f"   {'branch':<8}{'prefix':<8}{'DocType':<8}{'count':>8}")
        for br, pfx, dt, cnt in rows[:40]:
            print(f"   {str(br):<8}{str(pfx):<8}{str(dt):<8}{cnt:>8}")


def postar_info(cur):
    print(f"\n{DIVIDER}"); print("3a. PostAR - columns"); print(DIVIDER)
    cur.execute("SELECT TOP 0 * FROM dbo.PostAR")
    cols = [d[0] for d in cur.description]
    print("   " + ", ".join(cols))

    # Try to find a transaction-code / type column and a date column.
    lc = [c.lower() for c in cols]
    def pick(*cands):
        for cand in cands:
            if cand.lower() in lc:
                return cols[lc.index(cand.lower())]
        return None

    code_col = pick("TrCode", "cTrCode", "Code", "TxType", "cType", "Reference")
    date_col = pick("TxDate", "Date", "dDate", "PostDate", "TransactionDate", "InvDate")

    print(f"\n   detected code column: {code_col}")
    print(f"   detected date column: {date_col}")

    if code_col:
        print(f"\n{DIVIDER}"); print(f"3b. PostAR - breakdown by [{code_col}]"); print(DIVIDER)
        try:
            cur.execute(f"""
                SELECT TOP 25 [{code_col}] AS c, COUNT(*) AS cnt
                FROM dbo.PostAR
                GROUP BY [{code_col}]
                ORDER BY cnt DESC
            """)
            for c, cnt in cur.fetchall():
                print(f"   {str(c):<20} {cnt:>8}")
        except Exception as e:
            print(f"   (breakdown failed: {e})")

    if date_col:
        print(f"\n{DIVIDER}"); print(f"3c. PostAR - rows per year by [{date_col}]"); print(DIVIDER)
        try:
            cur.execute(f"""
                SELECT YEAR([{date_col}]) AS yr, COUNT(*) AS cnt
                FROM dbo.PostAR
                GROUP BY YEAR([{date_col}])
                ORDER BY yr
            """)
            for yr, cnt in cur.fetchall():
                print(f"   {yr}: {cnt:>8}")
        except Exception as e:
            print(f"   (year breakdown failed: {e})")

    # A few 2026 sample rows so we can see what they actually are.
    if date_col:
        print(f"\n{DIVIDER}"); print("3d. PostAR - sample 2026 rows"); print(DIVIDER)
        try:
            cur.execute(f"SELECT TOP 8 * FROM dbo.PostAR WHERE YEAR([{date_col}]) = 2026")
            scols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                vals = []
                for c, v in zip(scols, row):
                    if v not in (None, "", 0, 0.0, False):
                        sv = str(v)
                        if len(sv) > 26: sv = sv[:26] + "..."
                        vals.append(f"{c}={sv}")
                print("   " + " | ".join(vals[:12]))
        except Exception as e:
            print(f"   (sample failed: {e})")


def main():
    conn = connect()
    cur = conn.cursor()
    print(f"Connected to {GENESIS_FOOD_DB}.\n")

    invnum_year_counts(cur)
    invnum_latest_per_branch(cur)
    invnum_2026_by_branch_prefix(cur)
    postar_info(cur)

    conn.close()
    print(f"\n{DIVIDER}")
    print("INTERPRETATION")
    print(DIVIDER)
    print("  - If InvNum HAS 2026 rows (section 1a/2): your earlier 'stops at 2021'")
    print("    was just sampling a retired branch. Use the live branches.")
    print("  - PostAR's JC/CASH codes (section 3b) are journals & cash receipts that")
    print("    NEVER create an InvNum document - that's why PostAR has rows InvNum")
    print("    lacks. Those are NOT sales and must NOT be filed to FIRS.")
    print("  - FIRS sales come from InvNum sales documents only.")
    print(DIVIDER)


if __name__ == "__main__":
    main()