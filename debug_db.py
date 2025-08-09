import mysql.connector

cnx = mysql.connector.connect(user='root', password='advanced', host='127.0.0.1', database='scrape')
cursor = cnx.cursor()

print("Checking database content for filtered categories:")

cursor.execute("SELECT COUNT(*) FROM pages WHERE drive IS NOT NULL AND drive != ''")
print(f'Drive count in DB: {cursor.fetchone()[0]}')

cursor.execute("SELECT COUNT(*) FROM pages WHERE googleSites IS NOT NULL AND googleSites != ''")
print(f'Google Sites count in DB: {cursor.fetchone()[0]}')

cursor.execute("SELECT COUNT(*) FROM pages WHERE divisions IS NOT NULL AND divisions != ''")
print(f'Divisions count in DB: {cursor.fetchone()[0]}')

cursor.execute("SELECT drive FROM pages WHERE drive IS NOT NULL AND drive != '' LIMIT 3")
drive_samples = cursor.fetchall()
print(f'Sample drive URLs: {drive_samples}')

cursor.execute("SELECT googleSites FROM pages WHERE googleSites IS NOT NULL AND googleSites != '' LIMIT 3")
sites_samples = cursor.fetchall()
print(f'Sample Google Sites URLs: {sites_samples}')

cursor.execute("SELECT divisions FROM pages WHERE divisions IS NOT NULL AND divisions != '' LIMIT 3")
divisions_samples = cursor.fetchall()
print(f'Sample divisions URLs: {divisions_samples}')

cursor.close()
cnx.close()