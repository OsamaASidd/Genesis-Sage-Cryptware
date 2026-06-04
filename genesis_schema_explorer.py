"""
Genesis GGNL-LIVE - Schema Explorer
Finds all tables and dumps columns for invoice-relevant ones.
"""
import pyodbc
import sys
from config import GENESIS_DB_CONN_STR

def connect():
    try:
        return pyodbc.connect(GENESIS_DB_CONN_STR, timeout=10)
    except pyodbc.Error as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

def all_tables(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE='BASE TABLE'
        ORDER BY TABLE_NAME
    """)
    return [r[0] for r in cur.fetchall()]

def sample(conn, table, n=3):
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT TOP {n} * FROM dbo.[{table}] ORDER BY 1 DESC")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    except Exception as e:
        return [], []

TARGETS = [
    "InvLines", "InvLine", "InvDetail", "InvDetails",
    "DocLines", "DocLine", "StkItem", "Client",
    "TaxRate", "Rep", "InvNum", "_btblInvoiceLines",
]

def main():
    conn = connect()
    print("Connected.\n")

    tables = all_tables(conn)
    print(f"All tables ({len(tables)}):")
    for t in tables:
        print(f"  {t}")
    print()

    for target in TARGETS:
        match = next((t for t in tables if t.lower() == target.lower()), None)
        if not match:
            match = next((t for t in tables if target.lower() in t.lower()), None)
        if not match:
            continue

        print("=" * 70)
        print(f"TABLE: dbo.{match}")
        print("=" * 70)
        cols, rows = sample(conn, match, n=2)
        if not cols:
            print("  (could not read)\n")
            continue
        print(f"Columns: {', '.join(cols)}\n")
        for row in rows:
            for c, v in zip(cols, row):
                if v not in (None, "", b"", 0, 0.0, False):
                    print(f"  {c:<35} {v}")
            print()

    conn.close()

if __name__ == "__main__":
    main()