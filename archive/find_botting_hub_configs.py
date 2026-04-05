import sqlite3

db_path = r"C:\Users\nubonix\Botting Hub\Manager\Data\database.db"
tables = ["accounts", "proxies", "configurations"]

conn = sqlite3.connect(db_path)
cur = conn.cursor()

for table in tables:
    print(f"\n=== {table.upper()} : COLUMNS ===")
    cur.execute(f"PRAGMA table_info({table})")
    for row in cur.fetchall():
        print(row)

    print(f"\n=== {table.upper()} : SAMPLE ROWS ===")
    cur.execute(f"SELECT * FROM {table} LIMIT 5")
    rows = cur.fetchall()
    for row in rows:
        print(row)

conn.close()