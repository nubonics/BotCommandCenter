import sqlite3

db = "osrs_money_makers.old.db"
con = sqlite3.connect(db)

tables = [r[0] for r in con.execute(
    "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
).fetchall()]
print("TABLES:", tables)

print("\n--- CREATE TABLE STATEMENTS ---\n")
for name, sql in con.execute(
    "select name, sql from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
).fetchall():
    if sql:
        print(f"-- {name}\n{sql};\n")

con.close()
