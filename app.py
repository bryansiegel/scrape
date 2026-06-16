from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context
import mysql.connector
from mysql.connector import errorcode
import os
import re
import io
import subprocess
import openpyxl

# Load .env file if present (no external dependency needed)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

app = Flask(__name__)

# Single-scrape HTML cache — stores last scrape's page HTML keyed by URL
_html_cache = {}

# Tracking/analytics script detection patterns
TRACKER_PATTERNS = [
    ('Google Analytics 4',         ['gtag/js?id=G-', "gtag('config", "gtag('event"]),
    ('Google Universal Analytics', ['google-analytics.com/analytics.js', "ga('create"]),
    ('Google Tag Manager',         ['googletagmanager.com/gtm.js']),
    ('Facebook Pixel',             ['connect.facebook.net/en_US/fbevents.js', "fbq('init"]),
    ('LinkedIn Insight Tag',       ['snap.licdn.com/li.lms-analytics', '_linkedin_partner_id']),
    ('Twitter / X Pixel',          ['static.ads-twitter.com/uwt.js', "twq('init"]),
    ('HotJar',                     ['static.hotjar.com/c/hotjar']),
    ('Microsoft Clarity',          ['clarity.ms/tag']),
    ('Mixpanel',                   ['cdn.mxpnl.com', "mixpanel.init("]),
    ('Segment',                    ['cdn.segment.com/analytics.js', "analytics.load("]),
    ('Heap Analytics',             ['cdn.heapanalytics.com', "heap.load("]),
    ('HubSpot',                    ['js.hs-scripts.com', 'js.hsforms.net']),
    ('Matomo / Piwik',             ['matomo.js', 'piwik.js', '_paq.push']),
    ('Optimizely',                 ['cdn.optimizely.com']),
    ('FullStory',                  ['fullstory.com/s/fs.js', '_fs_debug']),
    ('Intercom',                   ['widget.intercom.io', 'js.intercomcdn.com']),
    ('Amplitude',                  ['cdn.amplitude.com', "amplitude.getInstance"]),
    ('Crazy Egg',                  ['script.crazyegg.com']),
    ('TikTok Pixel',               ['analytics.tiktok.com/i18n/pixel', "ttq.load("]),
    ('Pinterest Tag',              ['ct.pinterest.com/v3/', "pintrk('load"]),
    ('Snapchat Pixel',             ['sc-static.net/s/snapchat.js']),
    ('Adobe Analytics',            ['omtrdc.net', 's_code.js']),
    ('Cloudflare Web Analytics',   ['static.cloudflareinsights.com/beacon.min.js']),
    ('Yandex.Metrica',             ['mc.yandex.ru/metrika']),
]


def extract_content_html(html_text):
    """Return only the main content HTML, stripping nav, header, footer, sidebars, and scripts."""
    from bs4 import BeautifulSoup, Tag

    try:
        soup = BeautifulSoup(html_text, 'html.parser')

        STRIP_TAGS = ['nav', 'header', 'footer', 'aside', 'script', 'style', 'noscript', 'iframe']
        STRIP_ATTRS = [
            'nav', 'navigation', 'navbar', 'menu', 'sidebar', 'side-bar',
            'site-header', 'site-footer', 'page-header', 'page-footer',
            'breadcrumb', 'pagination', 'widget', 'advertisement', 'banner',
            'cookie', 'popup', 'modal', 'social', 'share', 'related',
            'comment', 'toolbar', 'skip-link',
        ]
        CONTENT_SELECTORS = [
            'main', '[role="main"]', 'article',
            '#content', '#main-content', '#page-content', '#main', '#primary', '#body-content',
            '.content-wrap', '.content-area', '.main-content', '.page-content',
            '.entry-content', '.post-content', '.article-content', '.site-content',
            '.content', '.main',
        ]

        target = None
        for sel in CONTENT_SELECTORS:
            target = soup.select_one(sel)
            if target:
                break
        if target is None:
            target = soup.find('body') or soup

        # Collect then decompose — avoids accessing already-destroyed children
        # when iterating a pre-built list that includes their descendants.
        for tag in STRIP_TAGS:
            for elem in target.find_all(tag):
                elem.decompose()

        to_remove = []
        for elem in target.find_all(True):
            if not isinstance(elem, Tag):
                continue
            try:
                attrs = ' '.join(filter(None, [
                    elem.get('id') or '',
                    ' '.join(elem.get('class') or []),
                ])).lower()
                if any(p in attrs for p in STRIP_ATTRS):
                    to_remove.append(elem)
            except Exception:
                pass
        for elem in to_remove:
            try:
                elem.decompose()
            except Exception:
                pass

        # Strip class and style attributes from every remaining element
        for elem in target.find_all(True):
            if isinstance(elem, Tag):
                elem.attrs.pop('class', None)
                elem.attrs.pop('style', None)

        return str(target)
    except Exception:
        # If anything goes wrong, fall back to returning the raw HTML
        return html_text


# Database configuration — values loaded from .env
DB_CONFIG = {
    'host':     os.getenv('DB_HOST', '127.0.0.1'),
    'user':     os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', ''),
    'database': os.getenv('DB_NAME', 'scrape'),
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


@app.route('/api/export/scrape-pdfs', methods=['POST'])
def export_scrape_pdfs():
    data        = request.get_json(force=True)
    pdfs        = data.get('pdfs', [])          # [{url, label}, ...]
    col_label   = data.get('col_label', 'Source / File Name')
    dl_name     = data.get('download_name', 'scrape_pdfs.xlsx')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'PDFs'

    header_font = Font(bold=True, color='FFFFFF', size=11)
    header_fill = PatternFill(fill_type='solid', fgColor='1F3864')
    link_font   = Font(color='1155CC', size=10)
    center      = Alignment(horizontal='center', vertical='center')
    left        = Alignment(horizontal='left', vertical='center', wrap_text=True)
    thin        = Side(style='thin', color='BFBFBF')
    border      = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.column_dimensions['A'].width = 6
    ws.column_dimensions['B'].width = 60
    ws.column_dimensions['C'].width = 80

    ws.append(['#', col_label, 'PDF URL'])
    for col in range(1, 4):
        cell = ws.cell(row=1, column=col)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = center
        cell.border    = border
    ws.row_dimensions[1].height = 20

    for i, pdf in enumerate(pdfs, 1):
        url   = pdf.get('url', '')
        label = pdf.get('label', '')
        ws.cell(row=i + 1, column=1, value=i).alignment  = center
        ws.cell(row=i + 1, column=1).border              = border
        ws.cell(row=i + 1, column=2, value=label).font   = Font(size=10)
        ws.cell(row=i + 1, column=2).alignment           = left
        ws.cell(row=i + 1, column=2).border              = border
        pdf_cell             = ws.cell(row=i + 1, column=3, value=url)
        pdf_cell.font        = link_font
        pdf_cell.alignment   = left
        pdf_cell.border      = border
        ws.row_dimensions[i + 1].height = 16

    ws.freeze_panes = 'A2'

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=dl_name,
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


@app.route('/page-scrape')
def page_scrape_page():
    return render_template('page_scrape.html')


@app.route('/broken-link-checker')
def broken_link_checker_page():
    return render_template('broken_link_checker.html')


@app.route('/api/single-scrape')
def run_single_scrape():
    import requests as req
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, urljoin
    from collections import deque
    import time
    import random

    url           = request.args.get('url', '').strip()
    find_pdf      = request.args.get('pdf', 'true').lower() == 'true'
    find_sites    = request.args.get('sites', 'true').lower() == 'true'
    find_images   = request.args.get('images', 'true').lower() == 'true'
    find_tracking = request.args.get('tracking', 'true').lower() == 'true'
    find_external = request.args.get('external', 'true').lower() == 'true'

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
        global _html_cache
        _html_cache = {}  # clear previous scrape

        parsed_start = urlparse(url)
        base_domain  = parsed_start.netloc.lower().removeprefix('www.')

        visited        = set()
        queue          = deque([url])
        found_pdfs     = set()
        found_sites    = set()
        found_images   = set()
        found_trackers = set()  # "tracker_name|page_url" keys to dedupe per-page
        found_externals = set()
        pages_crawled  = 0

        yield f"data: Starting crawl of {url}\n\n"
        yield f"data: Domain: {base_domain}\n\n"

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            pages_crawled += 1

            yield f"data: PAGE: {pages_crawled}\n\n"
            yield f"data: [{pages_crawled}] {current}\n\n"

            try:
                headers = {'User-Agent': random.choice(USER_AGENTS)}
                resp = req.get(current, headers=headers, timeout=10, allow_redirects=True)
                if resp.status_code != 200:
                    yield f"data: Skipped (HTTP {resp.status_code}): {current}\n\n"
                    continue

                content_type = resp.headers.get('Content-Type', '')
                if 'html' not in content_type:
                    continue

                raw_html = resp.text
                _html_cache[current] = extract_content_html(raw_html)
                soup = BeautifulSoup(raw_html, 'html.parser')

                # Emit LINK with page title so the All Links tab can label it
                title_tag  = soup.find('title')
                page_title = title_tag.get_text().strip()[:100] if title_tag else ''
                yield f"data: LINK: {current}|{page_title}\n\n"

                # Walk all <a> tags
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if not href or href.startswith('mailto:') or href.startswith('javascript:'):
                        continue
                    full = urljoin(current, href).split('#')[0].rstrip('/')
                    if not full.startswith('http'):
                        continue

                    data_file = (a.get('data-file-name') or '').strip()
                    url_path  = full.lower().split('?')[0]
                    is_pdf    = url_path.endswith('.pdf') or data_file.lower().endswith('.pdf')
                    if find_pdf and is_pdf and full not in found_pdfs:
                        found_pdfs.add(full)
                        yield f"data: PDF: {full}|{current}\n\n"

                    if find_sites and 'sites.google.com' in full and full not in found_sites:
                        found_sites.add(full)
                        yield f"data: SITE: {full}|{current}\n\n"

                    link_domain = urlparse(full).netloc.lower().removeprefix('www.')
                    if link_domain == base_domain:
                        if full not in visited:
                            queue.append(full)
                    elif find_external and full not in found_externals:
                        found_externals.add(full)
                        rel = a.get('rel') or []
                        if isinstance(rel, str):
                            rel = rel.split()
                        nofollow = 'nofollow' in [r.lower() for r in rel]
                        is_ccsd = link_domain == 'ccsd.net' or link_domain.endswith('.ccsd.net')
                        yield f"data: EXTERNAL: {full}|{current}|{1 if nofollow else 0}|{1 if is_ccsd else 0}\n\n"

                # Collect images
                if find_images:
                    for img in soup.find_all('img'):
                        src = (img.get('src') or img.get('data-src') or
                               img.get('data-lazy-src') or '').strip()
                        if src:
                            full_img = urljoin(current, src)
                            if full_img.startswith('http') and full_img not in found_images:
                                found_images.add(full_img)
                                yield f"data: IMG: {full_img}|{current}\n\n"
                    for source in soup.find_all('source'):
                        for part in (source.get('srcset') or '').split(','):
                            src = part.strip().split()[0] if part.strip() else ''
                            if src:
                                full_img = urljoin(current, src)
                                if full_img.startswith('http') and full_img not in found_images:
                                    found_images.add(full_img)
                                    yield f"data: IMG: {full_img}|{current}\n\n"

                # Detect tracking/analytics scripts
                if find_tracking:
                    script_text = ''
                    for script in soup.find_all('script'):
                        script_text += ' ' + (script.get('src') or '')
                        script_text += ' ' + (script.get_text() or '')
                    for name, patterns in TRACKER_PATTERNS:
                        for pattern in patterns:
                            if pattern.lower() in script_text.lower():
                                key = f"{name}|{current}"
                                if key not in found_trackers:
                                    found_trackers.add(key)
                                    yield f"data: TRACKER: {name}|{current}\n\n"
                                break

            except Exception as e:
                yield f"data: Error ({current}): {e}\n\n"

            time.sleep(0.15)

        yield f"data: \n\n"
        yield (
            f"data: Done. {pages_crawled} page(s) crawled. "
            f"{len(found_pdfs)} PDF(s), {len(found_sites)} Google Sites, "
            f"{len(found_images)} image(s), {len(found_trackers)} tracker(s), "
            f"{len(found_externals)} external link(s) found.\n\n"
        )
        yield "data: __SUCCESS__\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/api/page-scrape')
def run_page_scrape():
    import requests as req
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    import random

    url           = request.args.get('url', '').strip()
    find_pdf      = request.args.get('pdf', 'true').lower() == 'true'
    find_sites    = request.args.get('sites', 'true').lower() == 'true'
    find_images   = request.args.get('images', 'true').lower() == 'true'
    find_tracking = request.args.get('tracking', 'true').lower() == 'true'

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
        global _html_cache

        yield f"data: Fetching {url}\n\n"

        try:
            headers = {'User-Agent': random.choice(USER_AGENTS)}
            resp = req.get(url, headers=headers, timeout=10, allow_redirects=True)

            if resp.status_code != 200:
                yield f"data: ERROR: HTTP {resp.status_code} — {url}\n\n"
                yield "data: __FAILURE__\n\n"
                return

            content_type = resp.headers.get('Content-Type', '')
            if 'html' not in content_type:
                yield f"data: ERROR: Not an HTML page (Content-Type: {content_type})\n\n"
                yield "data: __FAILURE__\n\n"
                return

            raw_html = resp.text
            _html_cache[url] = extract_content_html(raw_html)
            soup = BeautifulSoup(raw_html, 'html.parser')

            title_tag  = soup.find('title')
            page_title = title_tag.get_text().strip()[:120] if title_tag else ''
            yield f"data: PAGEINFO: {url}|{page_title}\n\n"
            yield f"data: Page: {page_title or url}\n\n"

            found_pdfs   = set()
            found_sites  = set()
            found_links  = set()
            found_images = set()
            found_trackers = set()

            for a in soup.find_all('a', href=True):
                href = a['href'].strip()
                if not href or href.startswith('mailto:') or href.startswith('javascript:'):
                    continue
                full = urljoin(url, href).split('#')[0].rstrip('/')
                if not full.startswith('http'):
                    continue

                # PDF: check URL path extension AND data-file-name attribute (Finalsite CMS)
                data_file = (a.get('data-file-name') or '').strip()
                url_path  = full.lower().split('?')[0]
                is_pdf    = url_path.endswith('.pdf') or data_file.lower().endswith('.pdf')
                if find_pdf and is_pdf and full not in found_pdfs:
                    found_pdfs.add(full)
                    display = data_file or url_path.split('/')[-1] or full
                    yield f"data: PDF: {full}|{display}\n\n"
                    continue

                if find_sites and 'sites.google.com' in full and full not in found_sites:
                    found_sites.add(full)
                    yield f"data: SITE: {full}|{url}\n\n"
                    continue

                if full not in found_links:
                    found_links.add(full)
                    link_text = a.get_text().strip()[:80]
                    yield f"data: LINK: {full}|{link_text}\n\n"

            if find_images:
                for img in soup.find_all('img'):
                    src = (img.get('src') or img.get('data-src') or
                           img.get('data-lazy-src') or '').strip()
                    if src:
                        full_img = urljoin(url, src)
                        if full_img.startswith('http') and full_img not in found_images:
                            found_images.add(full_img)
                            yield f"data: IMG: {full_img}|{url}\n\n"
                for source in soup.find_all('source'):
                    for part in (source.get('srcset') or '').split(','):
                        src = part.strip().split()[0] if part.strip() else ''
                        if src:
                            full_img = urljoin(url, src)
                            if full_img.startswith('http') and full_img not in found_images:
                                found_images.add(full_img)
                                yield f"data: IMG: {full_img}|{url}\n\n"

            if find_tracking:
                script_text = ''
                for script in soup.find_all('script'):
                    script_text += ' ' + (script.get('src') or '')
                    script_text += ' ' + (script.get_text() or '')
                for name, patterns in TRACKER_PATTERNS:
                    for pattern in patterns:
                        if pattern.lower() in script_text.lower():
                            if name not in found_trackers:
                                found_trackers.add(name)
                                yield f"data: TRACKER: {name}|{url}\n\n"
                            break

            yield f"data: \n\n"
            yield (
                f"data: Done. {len(found_links)} link(s), {len(found_pdfs)} PDF(s), "
                f"{len(found_sites)} Google Sites, {len(found_images)} image(s), "
                f"{len(found_trackers)} tracker(s) found.\n\n"
            )
            yield "data: __SUCCESS__\n\n"

        except Exception as e:
            yield f"data: Error: {e}\n\n"
            yield "data: __FAILURE__\n\n"

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/api/broken-link-check')
def run_broken_link_check():
    import requests as req
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, urljoin
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time, random, json

    url          = request.args.get('url', '').strip()
    mode         = request.args.get('mode', 'page')   # 'page' | 'site'
    check_imgs   = request.args.get('images',   'true').lower()  == 'true'
    check_pdfs   = request.args.get('pdfs',     'true').lower()  == 'true'
    check_ext    = request.args.get('external', 'false').lower() == 'true'
    check_social = request.args.get('social',   'false').lower() == 'true'

    SOCIAL_DOMAINS = [
        'facebook.com', 'fb.com', 'fbcdn.net',
        'twitter.com', 'x.com', 't.co',
        'instagram.com', 'instagr.am',
        'linkedin.com', 'lnkd.in',
        'youtube.com', 'youtu.be', 'yt.be',
        'tiktok.com', 'vm.tiktok.com',
        'pinterest.com', 'pin.it',
        'snapchat.com', 'snap.com',
        'reddit.com', 'redd.it',
        'tumblr.com', 'vimeo.com',
        'flickr.com', 'threads.net',
    ]

    def is_social_url(u):
        netloc = urlparse(u).netloc.lower().removeprefix('www.')
        return any(netloc == s or netloc.endswith('.' + s) for s in SOCIAL_DOMAINS)

    def err(msg):
        yield f"data: {msg}\n\n"
        yield "data: __FAILURE__\n\n"

    if not url or not url.startswith('http'):
        return Response(stream_with_context(err('ERROR: Invalid or missing URL')), mimetype='text/event-stream')

    USER_AGENTS = [
        # Chrome — Windows
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        # Chrome — Mac
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        # Chrome — Linux
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        # Firefox — Windows
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
        # Firefox — Mac / Linux
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0',
        'Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0',
        # Safari — Mac
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
        # Edge — Windows
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
        # Mobile
        'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1',
    ]

    def make_headers():
        ua = random.choice(USER_AGENTS)
        return {
            'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }

    def check_url_status(check_u):
        # Use GET with stream=True (never reads body, just headers) rather than HEAD.
        # HEAD is unreliable — many servers return 403/404 for HEAD but serve fine on GET.
        def _get(hdrs):
            r = req.get(check_u, headers=hdrs, timeout=7, allow_redirects=True, stream=True)
            r.close()
            return r

        try:
            r = _get(make_headers())

            # Retry once on 429, honouring Retry-After if present
            if r.status_code == 429:
                wait = min(int(r.headers.get('Retry-After', 3)), 15)
                time.sleep(wait + random.uniform(0.5, 2.0))
                r = _get(make_headers())

            return r.status_code, r.reason or str(r.status_code)
        except req.exceptions.Timeout:
            return 0, 'Timeout'
        except req.exceptions.SSLError:
            return 0, 'SSL Error'
        except req.exceptions.TooManyRedirects:
            return 0, 'Too Many Redirects'
        except req.exceptions.InvalidURL:
            return 0, 'Invalid URL'
        except req.exceptions.ConnectionError:
            return 0, 'Connection Error'
        except Exception as e:
            return 0, type(e).__name__

    def get_snippet(element):
        try:
            html = str(element)
            return html[:600] + ('…' if len(html) > 600 else '')
        except Exception:
            return ''

    def generate():
        try:
            parsed_start     = urlparse(url)
            base_domain      = parsed_start.netloc
            base_domain_norm = base_domain.removeprefix('www.')

            visited_pages = set()
            checked_urls  = set()
            queue = deque([url])

            pages_crawled = 0
            total_checked = 0
            broken_count  = 0

            yield f"data: Starting broken link check — {url}\n\n"

            while queue:
                current = queue.popleft()
                if current in visited_pages:
                    continue
                visited_pages.add(current)
                pages_crawled += 1

                yield f"data: PAGE: {pages_crawled}\n\n"
                yield f"data: Scanning [{pages_crawled}]: {current}\n\n"

                try:
                    headers = {'User-Agent': random.choice(USER_AGENTS)}
                    resp = req.get(current, headers=headers, timeout=12, allow_redirects=True)
                    if resp.status_code != 200:
                        yield f"data: Skipped (HTTP {resp.status_code}): {current}\n\n"
                        continue
                    if 'html' not in resp.headers.get('Content-Type', ''):
                        continue

                    soup = BeautifulSoup(resp.text, 'html.parser')

                    # Queue internal pages for crawling (site mode)
                    if mode == 'site':
                        for a in soup.find_all('a', href=True):
                            href = a['href'].strip()
                            if not href or href.startswith(('mailto:', 'javascript:', '#', 'tel:')):
                                continue
                            full = urljoin(current, href).split('#')[0].rstrip('/')
                            if not full.startswith('http'):
                                continue
                            if urlparse(full).netloc.removeprefix('www.') == base_domain_norm and full not in visited_pages:
                                queue.append(full)

                    # Collect resources to check from this page
                    resources = []

                    for a in soup.find_all('a', href=True):
                        href = a['href'].strip()
                        if not href or href.startswith(('mailto:', 'javascript:', '#', 'tel:')):
                            continue
                        full = urljoin(current, href).split('#')[0].rstrip('/')
                        if not full.startswith('http'):
                            continue
                        if full in checked_urls:
                            continue

                        is_internal = urlparse(full).netloc.removeprefix('www.') == base_domain_norm
                        social      = is_social_url(full)
                        url_path    = full.lower().split('?')[0]
                        is_pdf      = url_path.endswith('.pdf')

                        if not is_internal:
                            if social and not check_social:
                                continue
                            # PDFs obey check_pdfs regardless of check_ext
                            if is_pdf and not check_pdfs:
                                continue
                            if not is_pdf and not check_ext:
                                continue

                        if is_pdf:
                            if check_pdfs:
                                resources.append(('pdf', full, a, current, social))
                        else:
                            resources.append(('link', full, a, current, social))

                    if check_imgs:
                        for img in soup.find_all('img'):
                            src = (img.get('src') or img.get('data-src') or img.get('data-lazy-src') or '').strip()
                            if not src:
                                continue
                            full_img = urljoin(current, src)
                            if not full_img.startswith('http') or full_img in checked_urls:
                                continue
                            resources.append(('image', full_img, img, current, False))

                    # Dedupe before checking (tuple index 1 is the URL)
                    new_resources = []
                    for item in resources:
                        if item[1] not in checked_urls:
                            checked_urls.add(item[1])
                            new_resources.append(item)

                    if new_resources:
                        yield f"data: Checking {len(new_resources)} resource(s)…\n\n"

                    # Run concurrent checks and collect ALL results before yielding
                    # (avoids generator suspension inside the executor context)
                    check_results = []
                    try:
                        with ThreadPoolExecutor(max_workers=3) as executor:
                            future_map = {
                                executor.submit(check_url_status, r[1]): r
                                for r in new_resources
                            }
                            for fut in as_completed(future_map):
                                try:
                                    status, status_text = fut.result()
                                except Exception as fe:
                                    status, status_text = 0, type(fe).__name__
                                check_results.append((future_map[fut], status, status_text))
                    except Exception as te:
                        yield f"data: Warning: thread pool error — {te}\n\n"

                    # Yield results now that the executor is fully closed
                    for res, status, status_text in check_results:
                        res_type, res_url, element, source_page, is_social = res
                        total_checked += 1
                        yield f"data: CHECKING: {total_checked}|{res_url}\n\n"
                        if status == 0 or status >= 400:
                            # Social media: only 404 is a genuine broken link;
                            # 400/403/429 etc. are normal bot-blocking responses.
                            if is_social and status != 404:
                                continue
                            broken_count += 1
                            try:
                                payload = json.dumps({
                                    'type':       res_type,
                                    'url':        res_url,
                                    'status':     status,
                                    'statusText': status_text,
                                    'source':     source_page,
                                    'snippet':    get_snippet(element),
                                }, ensure_ascii=True)
                            except Exception:
                                payload = json.dumps({
                                    'type': res_type, 'url': res_url,
                                    'status': status, 'statusText': status_text,
                                    'source': source_page, 'snippet': '',
                                })
                            yield f"data: BROKEN: {payload}\n\n"
                            yield f"data: BROKEN_COUNT: {broken_count}\n\n"

                except Exception as e:
                    yield f"data: Error scanning {current}: {type(e).__name__}: {e}\n\n"

                time.sleep(0.1)

            yield f"data: \n\n"
            yield (
                f"data: Done. {pages_crawled} page(s) scanned, "
                f"{total_checked} resource(s) checked, "
                f"{broken_count} broken found.\n\n"
            )
            yield "data: __SUCCESS__\n\n"

        except GeneratorExit:
            return
        except Exception as e:
            yield f"data: Fatal error: {type(e).__name__}: {e}\n\n"
            yield "data: __FAILURE__\n\n"

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/api/page-html')
def get_page_html():
    page_url = request.args.get('url', '').strip()
    html = _html_cache.get(page_url, '')
    return jsonify({'html': html, 'found': bool(html)})


@app.route('/api/download-image')
def download_image():
    import requests as req
    img_url = request.args.get('url', '').strip()
    if not img_url.startswith('http'):
        return 'Invalid URL', 400
    try:
        resp = req.get(img_url, timeout=15, stream=True)
        filename = img_url.split('/')[-1].split('?')[0] or 'image'
        if not filename or '.' not in filename:
            ct = resp.headers.get('Content-Type', 'image/jpeg')
            ext = ct.split('/')[-1].split(';')[0].strip() or 'jpg'
            filename = f'image.{ext}'
        return Response(
            resp.iter_content(chunk_size=8192),
            content_type=resp.headers.get('Content-Type', 'image/jpeg'),
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return str(e), 500


if __name__ == '__main__':
    app.run(debug=True, port=5002)