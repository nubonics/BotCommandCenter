import sqlite3

db_path = r"C:\Users\nubonix\Botting Hub\Manager\Data\database.db"

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
for row in cur.fetchall():
    print(row[0])

conn.close()