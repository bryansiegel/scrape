import mysql.connector
from mysql.connector import errorcode
import os

DB_NAME = 'scrape'
TABLES = {}
TABLES['pages'] = (
    "CREATE TABLE `pages` ("
    "  `id` int(11) NOT NULL AUTO_INCREMENT,"
    "  `departments` text,"
    "  `drive` text,"
    "  `divisions` text,"
    "  `general` text,"
    "  `googleSites` text,"
    "  PRIMARY KEY (`id`)"
    ") ENGINE=InnoDB")

def create_database(cursor):
    try:
        cursor.execute(
            "CREATE DATABASE {} DEFAULT CHARACTER SET 'utf8'".format(DB_NAME))
        print(f"Database '{DB_NAME}' created successfully.")
    except mysql.connector.Error as err:
        print(f"Failed creating database: {err}")
        exit(1)

def create_tables(cursor):
    cursor.execute("USE {}".format(DB_NAME))
    for table_name in TABLES:
        table_description = TABLES[table_name]
        try:
            print(f"Creating table `{table_name}`: ", end='')
            cursor.execute(table_description)
            print("OK")
        except mysql.connector.Error as err:
            if err.errno == errorcode.ER_TABLE_EXISTS_ERROR:
                print("already exists.")
            else:
                print(err.msg)

def insert_data(cursor, cnx):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    files_to_columns = {
        'scraper_departments.txt': 'departments',
        'scraper_drive_links.txt': 'drive',
        'scraper_divisions.txt': 'divisions',
        'scraped_links.txt': 'general',
        'scraped_google_sites.txt': 'googleSites'
    }

    for file_name, column_name in files_to_columns.items():
        file_path = os.path.join(base_dir, file_name)
        try:
            with open(file_path, 'r') as f:
                links = f.readlines()
                if not links:
                    print(f"File {file_name} is empty. Nothing to insert.")
                    continue
                
                print(f"Inserting data from {file_name} into {column_name}...")
                for link in links:
                    link = link.strip()
                    if link:
                        try:
                            query = f"INSERT INTO pages ({column_name}) VALUES (%s)"
                            cursor.execute(query, (link,))
                        except mysql.connector.Error as err:
                            print(f"Error inserting link '{link}': {err}")
                cnx.commit()
                print(f"Data from {file_name} inserted successfully.")

        except FileNotFoundError:
            print(f"Error: {file_path} not found.")
        except Exception as e:
            print(f"An error occurred with {file_path}: {e}")

if __name__ == "__main__":
    try:
        cnx = mysql.connector.connect(user='root', password='advanced', host='127.0.0.1')
        cursor = cnx.cursor()
        print("Successfully connected to MySQL.")
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
            print("Something is wrong with your user name or password")
        elif err.errno == errorcode.ER_BAD_DB_ERROR:
            print("Database does not exist")
        else:
            print(f"Error connecting to MySQL: {err}")
        exit(1)

    try:
        cursor.execute(f"USE {DB_NAME}")
        print(f"Database '{DB_NAME}' selected.")
    except mysql.connector.Error as err:
        print(f"Database '{DB_NAME}' does not exist.")
        if err.errno == errorcode.ER_BAD_DB_ERROR:
            create_database(cursor)
            cnx.database = DB_NAME
        else:
            print(err)
            exit(1)

    create_tables(cursor)
    insert_data(cursor, cnx)

    cursor.close()
    cnx.close()
    print("MySQL connection is closed.")