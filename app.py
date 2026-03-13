from flask import Flask, render_template, request, jsonify
import mysql.connector
from mysql.connector import errorcode
import os
import re

app = Flask(__name__)

# Database configuration
DB_CONFIG = {
    'user': 'root',
    'password': 'advanced',
    'host': '127.0.0.1',
    'database': 'scrape'
}

def get_db_connection():
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        return connection
    except mysql.connector.Error as err:
        print(f"Database connection error: {err}")
        return None

def search_txt_files(search_term, category=None):
    """Search through .txt files for matching URLs"""
    results = []
    
    # Define file mappings
    file_mappings = {
        'departments': 'scraper_departments.txt',
        'divisions': 'scraper_divisions.txt',
        'general': 'scraped_links.txt',
        'drive': 'scraper_drive_links.txt',
        'googleSites': 'scraped_google_sites.txt',
        'pdf': 'scraped_pdf.txt'
    }
    
    # Determine which files to search
    files_to_search = {}
    if category and category != 'all' and category in file_mappings:
        files_to_search[category] = file_mappings[category]
    else:
        files_to_search = file_mappings
    
    for cat, filename in files_to_search.items():
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        raw = line.strip()
                        if not raw:
                            continue
                        if cat == 'pdf' and '|' in raw:
                            url, source_page = raw.split('|', 1)
                        else:
                            url = raw
                            source_page = None
                        if not search_term or search_term.lower() in url.lower():
                            result = {
                                'category': cat,
                                'url': url,
                                'source': 'txt_file',
                                'file': filename,
                                'line': line_num
                            }
                            if source_page:
                                result['source_page'] = source_page
                            results.append(result)
            except Exception as e:
                print(f"Error reading {filepath}: {e}")
    
    return results

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def get_data():
    category = request.args.get('category', 'all')
    search = request.args.get('search', '')
    
    all_results = []
    
    # Search database (pdf has no DB column — handled via txt file only)
    connection = get_db_connection() if category != 'pdf' else None
    if connection:
        cursor = connection.cursor()
        try:
            if category == 'all':
                query = """
                SELECT 'departments' as category, departments as url, 'database' as source 
                FROM pages WHERE departments IS NOT NULL AND departments != ''
                UNION ALL
                SELECT 'drive' as category, drive as url, 'database' as source 
                FROM pages WHERE drive IS NOT NULL AND drive != ''
                UNION ALL  
                SELECT 'divisions' as category, divisions as url, 'database' as source 
                FROM pages WHERE divisions IS NOT NULL AND divisions != ''
                UNION ALL
                SELECT 'general' as category, general as url, 'database' as source 
                FROM pages WHERE general IS NOT NULL AND general != ''
                UNION ALL
                SELECT 'googleSites' as category, googleSites as url, 'database' as source 
                FROM pages WHERE googleSites IS NOT NULL AND googleSites != ''
                """
            else:
                query = f"SELECT '{category}' as category, {category} as url, 'database' as source FROM pages WHERE {category} IS NOT NULL AND {category} != ''"
            
            if search:
                if category == 'all':
                    query += f" HAVING url LIKE %s"
                else:
                    query += f" AND {category} LIKE %s"
                
                cursor.execute(query, (f'%{search}%',))
            else:
                cursor.execute(query)
            
            db_results = cursor.fetchall()
            all_results.extend([{'category': row[0], 'url': row[1], 'source': row[2]} for row in db_results])
            
        except mysql.connector.Error as err:
            print(f"Database query error: {err}")
        finally:
            cursor.close()
            connection.close()
    
    # Search txt files
    txt_results = search_txt_files(search, category)
    all_results.extend(txt_results)
    
    # Remove duplicates while preserving order (database results first)
    seen_urls = set()
    unique_results = []
    for result in all_results:
        url = result['url']
        if url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(result)
    
    return jsonify({'data': unique_results, 'count': len(unique_results)})

@app.route('/api/stats')
def get_stats():
    stats = {'departments': 0, 'drive': 0, 'divisions': 0, 'general': 0, 'googleSites': 0, 'pdf': 0, 'total': 0}
    
    # Count from database
    connection = get_db_connection()
    if connection:
        cursor = connection.cursor()
        try:
            categories = ['departments', 'drive', 'divisions', 'general', 'googleSites']
            # Note: 'pdf' has no DB column — counted from txt file only
            
            for category in categories:
                cursor.execute(f"SELECT COUNT(*) FROM pages WHERE {category} IS NOT NULL AND {category} != ''")
                count = cursor.fetchone()[0]
                stats[category] += count
        except mysql.connector.Error as err:
            print(f"Database stats error: {err}")
        finally:
            cursor.close()
            connection.close()
    
    # Count from txt files
    file_mappings = {
        'departments': 'scraper_departments.txt',
        'divisions': 'scraper_divisions.txt',
        'general': 'scraped_links.txt',
        'drive': 'scraper_drive_links.txt',
        'googleSites': 'scraped_google_sites.txt',
        'pdf': 'scraped_pdf.txt'
    }

    for category, filename in file_mappings.items():
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    file_count = sum(1 for line in f if line.strip())
                stats[category] += file_count
            except Exception as e:
                print(f"Error counting {filepath}: {e}")
    
    # Calculate total unique URLs
    all_urls = set()
    
    # Add database URLs
    connection = get_db_connection()
    if connection:
        cursor = connection.cursor()
        try:
            cursor.execute("""
                SELECT departments FROM pages WHERE departments IS NOT NULL AND departments != ''
                UNION ALL
                SELECT drive FROM pages WHERE drive IS NOT NULL AND drive != ''
                UNION ALL
                SELECT divisions FROM pages WHERE divisions IS NOT NULL AND divisions != ''
                UNION ALL
                SELECT general FROM pages WHERE general IS NOT NULL AND general != ''
                UNION ALL
                SELECT googleSites FROM pages WHERE googleSites IS NOT NULL AND googleSites != ''
            """)
            for (url,) in cursor.fetchall():
                if url:
                    all_urls.add(url.strip())
        except mysql.connector.Error as err:
            print(f"Database total count error: {err}")
        finally:
            cursor.close()
            connection.close()
    
    # Add txt file URLs (file_mappings already includes pdf)
    for cat, filename in file_mappings.items():
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        raw = line.strip()
                        if raw:
                            url = raw.split('|', 1)[0] if cat == 'pdf' else raw
                            all_urls.add(url)
            except Exception as e:
                print(f"Error reading {filepath} for total: {e}")
    
    stats['total'] = len(all_urls)
    return jsonify(stats)

@app.route('/api/autocomplete')
def get_autocomplete():
    query = request.args.get('q', '').lower()
    suggestions = set()
    
    # Get suggestions from txt files
    file_mappings = {
        'departments': 'scraper_departments.txt',
        'divisions': 'scraper_divisions.txt',
        'general': 'scraped_links.txt',
        'drive': 'scraper_drive_links.txt',
        'googleSites': 'scraped_google_sites.txt',
        'pdf': 'scraped_pdf.txt'
    }
    
    for filename in file_mappings.values():
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        url = line.strip()
                        if url:
                            # Extract meaningful parts for autocomplete
                            parts = re.split(r'[/\-_.]', url)
                            for part in parts:
                                if len(part) > 2 and not part.isdigit():
                                    suggestions.add(part.lower())
            except Exception as e:
                print(f"Error reading {filepath}: {e}")
    
    # Filter and return suggestions
    all_suggestions = sorted(list(suggestions))
    if query:
        filtered = [s for s in all_suggestions if query in s]
        return jsonify(filtered[:10])
    else:
        return jsonify(all_suggestions[:10])

if __name__ == '__main__':
    app.run(debug=True, port=5002)