from flask import Flask, jsonify
import mysql.connector
import os

app = Flask(__name__)

DB_CONFIG = {
    'user': 'root',
    'password': 'advanced',
    'host': '127.0.0.1',
    'database': 'scrape'
}

@app.route('/api/test')
def test():
    try:
        # Test database connection
        connection = mysql.connector.connect(**DB_CONFIG)
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM pages WHERE departments IS NOT NULL AND departments != ''")
        dept_count = cursor.fetchone()[0]
        cursor.close()
        connection.close()
        
        # Test file reading
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scraped_links.txt')
        file_exists = os.path.exists(filepath)
        
        if file_exists:
            with open(filepath, 'r', encoding='utf-8') as f:
                file_lines = sum(1 for line in f if line.strip())
        else:
            file_lines = 0
            
        return jsonify({
            'database_departments': dept_count,
            'scraped_links_file_exists': file_exists,
            'scraped_links_lines': file_lines,
            'working_directory': os.getcwd()
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5001)