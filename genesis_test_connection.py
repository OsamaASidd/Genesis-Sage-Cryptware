"""
Genesis SQL Server - Connection & Table Test
============================================
Tests: dbo.Client | dbo.InvNum (Invoices + Credit Notes)
Remote server 10.10.98.23 over TCP with TLS encryption.
"""

import pyodbc
import sys
from config import (
    GENESIS_DB_HOST, GENESIS_DB_NAME,
    GENESIS_DB_USER, GENESIS_DB_PASSWORD, GENESIS_DB_PORT, GENESIS_DB_CONN_STR
)

DIVIDER = "-" * 80


def probe_drivers():
    """Print all ODBC drivers installed on this machine."""
    drivers = pyodbc.drivers()
    sql_drivers = [d for d in drivers if "sql" in d.lower()]
    print("All SQL-related ODBC drivers on this machine:")
    for d in sql_drivers:
        print(f"  {d}")
    if not sql_drivers:
        print("  (none found — install ODBC Driver 17/18 for SQL Server)")
    print()
    return sql_drivers


def try_variants():
    """Try every available SQL Server driver against the remote TCP host."""
    drivers = pyodbc.drivers()
    sql_drivers = [d for d in drivers if "sql server" in d.lower()]

    candidates = []

    # Encrypted TCP with explicit port (preferred for remote server)
    for drv in sql_drivers:
        candidates.append((
            f"TCP :{GENESIS_DB_PORT} encrypted  [{drv}]",
            f"Driver={{{drv}}};Server={GENESIS_DB_HOST},{GENESIS_DB_PORT};"
            f"Database={GENESIS_DB_NAME};UID={GENESIS_DB_USER};PWD={GENESIS_DB_PASSWORD};"
            "Encrypt=yes;TrustServerCertificate=yes;"
        ))

    # Unencrypted fallback (older drivers / if TLS negotiation fails)
    for drv in sql_drivers:
        candidates.append((
            f"TCP :{GENESIS_DB_PORT} no-encrypt [{drv}]",
            f"Driver={{{drv}}};Server={GENESIS_DB_HOST},{GENESIS_DB_PORT};"
            f"Database={GENESIS_DB_NAME};UID={GENESIS_DB_USER};PWD={GENESIS_DB_PASSWORD};"
            "TrustServerCertificate=yes;"
        ))

    print("Trying connection variants...\n")
    for label, conn_str in candidates:
        try:
            conn = pyodbc.connect(conn_str, timeout=8)
            print(f"  ✓ SUCCESS  — {label}")
            print(f"    Conn str: {conn_str}\n")
            return conn, conn_str
        except pyodbc.Error as e:
            short = str(e).splitlines()[0][:120]
            print(f"  ✗ FAILED   — {label}")
            print(f"    {short}\n")

    return None, None


# ── Table helpers ─────────────────────────────────────────────────────────────

def show_columns(cursor, table):
    cursor.execute(f"SELECT TOP 0 * FROM {table}")
    cols = [d[0] for d in cursor.description]
    print(f"  Columns ({len(cols)}): {', '.join(cols)}")
    return cols


def _count(cur, table, where=""):
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table} {('WHERE ' + where) if where else ''}")
        return cur.fetchone()[0]
    except Exception:
        return "?"


def show_customers(conn, limit=10):
    print(DIVIDER)
    print("CUSTOMERS  —  dbo.Client")
    print(DIVIDER)
    cur = conn.cursor()
    try:
        cols = show_columns(cur, "dbo.Client")
        print()
        cur.execute(f"SELECT TOP {limit} * FROM dbo.Client ORDER BY 1")
        rows = cur.fetchall()
        print(f"  Showing {len(rows)} of {_count(cur, 'dbo.Client')} total rows\n")
        for row in rows:
            for col, val in zip(cols, row):
                if val not in (None, "", b""):
                    print(f"    {col:<35} {val}")
            print()
    except pyodbc.Error as e:
        print(f"  ✗ Error: {e}")


def show_invoices(conn, limit=10):
    print(DIVIDER)
    print("INVOICES  —  dbo.InvNum")
    print(DIVIDER)
    cur = conn.cursor()
    try:
        cols = show_columns(cur, "dbo.InvNum")
        print()
        cur.execute(f"SELECT TOP {limit} * FROM dbo.InvNum ORDER BY 1 DESC")
        rows = cur.fetchall()
        print(f"  Showing {len(rows)} of {_count(cur, 'dbo.InvNum')} total rows\n")
        for row in rows:
            for col, val in zip(cols, row):
                if val not in (None, "", b""):
                    print(f"    {col:<35} {val}")
            print()
    except pyodbc.Error as e:
        print(f"  ✗ Error: {e}")


def show_doctype_breakdown(conn):
    print(DIVIDER)
    print("CREDIT NOTE DETECTION  —  dbo.InvNum DocType breakdown")
    print(DIVIDER)
    cur = conn.cursor()
    try:
        cur.execute("SELECT TOP 0 * FROM dbo.InvNum")
        col_names = [d[0].lower() for d in cur.description]
        real_cols  = [d[0] for d in cur.description]

        type_col = None
        for candidate in ("doctype", "typedoc", "invtype", "type", "documenttype", "doctype_id"):
            if candidate in col_names:
                type_col = real_cols[col_names.index(candidate)]
                break

        if type_col:
            print(f"\n  Type column found: [{type_col}]")
            cur.execute(
                f"SELECT [{type_col}], COUNT(*) AS cnt "
                f"FROM dbo.InvNum GROUP BY [{type_col}] ORDER BY cnt DESC"
            )
            print(f"\n  {'Value':<20} {'Count':>8}")
            print(f"  {'-'*20} {'-'*8}")
            for r in cur.fetchall():
                print(f"  {str(r[0]):<20} {r[1]:>8}")
            print()
        else:
            print("\n  No standard DocType column found.")
            print(f"  Columns available: {', '.join(real_cols)}\n")

    except pyodbc.Error as e:
        print(f"  ✗ Error: {e}")


def main():
    print(DIVIDER)
    probe_drivers()

    conn, working_conn_str = try_variants()

    if conn is None:
        print(DIVIDER)
        print("All connection attempts failed.")
        print("Check: Is 10.10.98.23 reachable (VPN/network)? Port 1433 open?")
        print("       ODBC Driver 17/18 installed? powerbi_reader credentials valid?")
        sys.exit(1)

    print(DIVIDER)
    show_customers(conn, limit=10)
    show_invoices(conn, limit=10)
    show_doctype_breakdown(conn)

    conn.close()
    print(DIVIDER)
    print(f"Working connection string (copy to config.py GENESIS_DB_CONN_STR):\n")
    print(f"  {working_conn_str}\n")


if __name__ == "__main__":
    main()