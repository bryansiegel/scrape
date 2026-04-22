from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context
import mysql.connector
from mysql.connector import errorcode
import os
import re
import io
import subprocess
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

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

@app.route('/api/export/pdf')
def export_pdf_excel():
    search = request.args.get('search', '')

    # Load all PDF entries from scraped_pdf.txt
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scraped_pdf.txt')
    groups = {}  # source_page -> [pdf_url, ...]
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                if '|' in raw:
                    pdf_url, source_page = raw.split('|', 1)
                else:
                    pdf_url, source_page = raw, '(unknown page)'
                if search and search.lower() not in pdf_url.lower() and search.lower() not in source_page.lower():
                    continue
                if source_page not in groups:
                    groups[source_page] = []
                groups[source_page].append(pdf_url)

    # Build workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'PDFs by Page'

    # Styles
    header_font = Font(bold=True, color='FFFFFF', size=11)
    header_fill = PatternFill(fill_type='solid', fgColor='1F3864')
    page_font = Font(bold=True, size=10)
    page_fill = PatternFill(fill_type='solid', fgColor='D9E1F2')
    link_font = Font(color='1155CC', size=10)
    center = Alignment(horizontal='center', vertical='center')
    left = Alignment(horizontal='left', vertical='center', wrap_text=True)
    thin = Side(style='thin', color='BFBFBF')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Header row
    ws.column_dimensions['A'].width = 6
    ws.column_dimensions['B'].width = 60
    ws.column_dimensions['C'].width = 80

    ws.append(['#', 'Source Page', 'PDF URL'])
    for col in range(1, 4):
        cell = ws.cell(row=1, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border
    ws.row_dimensions[1].height = 20

    row_num = 2
    entry_num = 1
    for source_page, pdfs in sorted(groups.items()):
        # Source page group header
        if len(ws.merged_cells.ranges):
            pass
        ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=3)
        cell = ws.cell(row=row_num, column=1)
        cell.value = f'Page: {source_page}'
        cell.font = page_font
        cell.fill = page_fill
        cell.alignment = left
        cell.border = border
        # Apply border to merged cells individually
        for col in range(2, 4):
            ws.cell(row=row_num, column=col).border = border
        ws.row_dimensions[row_num].height = 18
        row_num += 1

        for pdf_url in pdfs:
            ws.cell(row=row_num, column=1, value=entry_num).alignment = center
            ws.cell(row=row_num, column=1).border = border
            ws.cell(row=row_num, column=2, value=source_page).font = Font(size=10)
            ws.cell(row=row_num, column=2).alignment = left
            ws.cell(row=row_num, column=2).border = border
            pdf_cell = ws.cell(row=row_num, column=3, value=pdf_url)
            pdf_cell.font = link_font
            pdf_cell.alignment = left
            pdf_cell.border = border
            ws.row_dimensions[row_num].height = 16
            row_num += 1
            entry_num += 1

    # Freeze header row
    ws.freeze_panes = 'A2'

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='ccsd_pdfs.xlsx'
    )


SCRAPER_SCRIPTS = {
    'all': 'all-scrape.py',
    'main': 'scraper.py',
    'html': 'scraper-html.py',
    'pdf': 'scraper_pdf.py',
    'drive': 'scraper_drive_links.py',
    'sites': 'scraper_google_sites.py',
    'database': 'add_to_database.py',
}

@app.route('/api/scrape/<scraper_type>')
def run_scraper(scraper_type):
    if scraper_type not in SCRAPER_SCRIPTS:
        def err():
            yield f"data: ERROR: Unknown scraper '{scraper_type}'\n\n"
            yield "data: __FAILURE__\n\n"
        return Response(stream_with_context(err()), mimetype='text/event-stream')

    script = SCRAPER_SCRIPTS[scraper_type]
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)

    if not os.path.exists(script_path):
        def err():
            yield f"data: ERROR: Script not found: {script}\n\n"
            yield "data: __FAILURE__\n\n"
        return Response(stream_with_context(err()), mimetype='text/event-stream')

    def generate():
        process = subprocess.Popen(
            ['python', '-u', script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        for line in process.stdout:
            yield f"data: {line.rstrip()}\n\n"
        process.wait()
        if process.returncode == 0:
            yield "data: __SUCCESS__\n\n"
        else:
            yield f"data: __FAILURE__ (exit code {process.returncode})\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/single-scrape')
def single_scrape_page():
    return render_template('single_scrape.html')


@app.route('/api/single-scrape')
def run_single_scrape():
    import requests as req
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, urljoin
    from collections import deque
    import time
    import random

    url        = request.args.get('url', '').strip()
    find_pdf   = request.args.get('pdf', 'true').lower() == 'true'
    find_sites = request.args.get('sites', 'true').lower() == 'true'
    max_pages  = min(int(request.args.get('max_pages', 50)), 500)

    def err(msg):
        yield f"data: {msg}\n\n"
        yield "data: __FAILURE__\n\n"

    if not url or not url.startswith('http'):
        return Response(stream_with_context(err('ERROR: Invalid or missing URL')), mimetype='text/event-stream')

    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
        'Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0',
    ]

    def generate():
        parsed_start = urlparse(url)
        base_domain  = parsed_start.netloc

        visited     = set()
        queue       = deque([url])
        found_pdfs  = set()
        found_sites = set()
        pages_crawled = 0

        yield f"data: Starting crawl of {url}\n\n"
        yield f"data: Domain: {base_domain} | Max pages: {max_pages}\n\n"

        while queue and pages_crawled < max_pages:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            pages_crawled += 1

            yield f"data: PAGE: {pages_crawled}\n\n"
            yield f"data: [{pages_crawled}/{max_pages}] {current}\n\n"

            try:
                headers = {'User-Agent': random.choice(USER_AGENTS)}
                resp = req.get(current, headers=headers, timeout=10, allow_redirects=True)
                if resp.status_code != 200:
                    yield f"data: Skipped (HTTP {resp.status_code}): {current}\n\n"
                    continue

                content_type = resp.headers.get('Content-Type', '')
                if 'html' not in content_type:
                    continue

                soup = BeautifulSoup(resp.text, 'html.parser')

                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if not href or href.startswith('mailto:') or href.startswith('javascript:'):
                        continue
                    full = urljoin(current, href).split('#')[0].rstrip('/')
                    if not full.startswith('http'):
                        continue

                    # PDF detection
                    if find_pdf and full.lower().endswith('.pdf') and full not in found_pdfs:
                        found_pdfs.add(full)
                        yield f"data: PDF: {full}|{current}\n\n"

                    # Google Sites detection
                    if find_sites and 'sites.google.com' in full and full not in found_sites:
                        found_sites.add(full)
                        yield f"data: SITE: {full}|{current}\n\n"

                    # Queue same-domain pages
                    link_domain = urlparse(full).netloc
                    if link_domain == base_domain and full not in visited:
                        queue.append(full)

            except Exception as e:
                yield f"data: Error ({current}): {e}\n\n"

            time.sleep(0.15)

        yield f"data: \n\n"
        yield f"data: Done. Crawled {pages_crawled} page(s). Found {len(found_pdfs)} PDF(s) and {len(found_sites)} Google Sites link(s).\n\n"
        yield "data: __SUCCESS__\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


if __name__ == '__main__':
    app.run(debug=True, port=5002)