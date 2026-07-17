from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context
import mysql.connector
from mysql.connector import errorcode
import os
import re
import io
import base64
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

# PDF-to-image cache — stores last conversion batch's rendered pages keyed by PDF URL.
# Each entry: {'source': str, 'pages': [png_bytes, ...], 'error': str|None, 'basename': str}
_pdf_image_cache = {}

# Tracking/analytics script detection patterns — Google + Facebook Pixel only.
TRACKER_PATTERNS = [
    ('Google Analytics 4',         ['gtag/js?id=G-', "gtag('config", "gtag('event"]),
    ('Google Universal Analytics', ['google-analytics.com/analytics.js', "ga('create"]),
    ('Google Tag Manager',         ['googletagmanager.com/gtm.js']),
    ('Facebook Pixel',             ['connect.facebook.net/en_US/fbevents.js', "fbq('init"]),
]

# Lazily-initialized spell checker singleton — loading the dictionary is too
# slow to redo on every page of an SEO crawl, so build it once and reuse it.
_spell_checker = None

def get_spell_checker():
    global _spell_checker
    if _spell_checker is None:
        from spellchecker import SpellChecker
        _spell_checker = SpellChecker()
    return _spell_checker


def get_ollama_url():
    return os.getenv('OLLAMA_URL', 'http://localhost:11434').rstrip('/')


def list_ollama_models(base_url):
    """Returns (running_model_names, installed_model_names) — best-effort, never raises."""
    import requests as req

    running = []
    try:
        r = req.get(f"{base_url}/api/ps", timeout=5)
        if r.ok:
            running = [m.get('name') for m in r.json().get('models', []) if m.get('name')]
    except Exception:
        pass

    installed = []
    try:
        r = req.get(f"{base_url}/api/tags", timeout=5)
        if r.ok:
            installed = [m.get('name') for m in r.json().get('models', []) if m.get('name')]
    except Exception:
        pass

    return running, installed


def url_to_filename(url, ext='.txt'):
    """Turn a page URL into a safe filename like 'subdomain--path--to--page.txt'."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    if host.endswith('.ccsd.net'):
        host = host[:-len('.ccsd.net')]
    elif host == 'ccsd.net':
        host = 'ccsd'

    path = parsed.path.strip('/')
    name = f"{host}--{path.replace('/', '--')}" if path else host
    name = re.sub(r'[^a-zA-Z0-9\-_.]', '_', name)
    return f"{name}{ext}"


def pdf_url_to_basename(url):
    """Turn a PDF URL into a safe filename stem (no extension), e.g. for image output names."""
    from urllib.parse import urlparse, unquote

    name = unquote(urlparse(url).path.rsplit('/', 1)[-1]) or 'document'
    if name.lower().endswith('.pdf'):
        name = name[:-4]
    name = re.sub(r'[^a-zA-Z0-9\-_.]', '_', name)
    return name or 'document'


def stack_pages_into_one_image(page_pngs):
    """Stack a PDF's rendered pages top-to-bottom into a single tall PNG."""
    if len(page_pngs) == 1:
        return page_pngs[0]

    from PIL import Image

    pages  = [Image.open(io.BytesIO(png)).convert('RGB') for png in page_pngs]
    width  = max(p.width for p in pages)
    height = sum(p.height for p in pages)
    combined = Image.new('RGB', (width, height), 'white')

    y = 0
    for p in pages:
        combined.paste(p, ((width - p.width) // 2, y))
        y += p.height

    out = io.BytesIO()
    combined.save(out, format='PNG')
    return out.getvalue()


def resolve_img_src(img, base_url=None):
    """Get the real image URL for an <img> tag — a plain src/data-src/data-lazy-src,
    or (Finalsite CMS) the largest size baked into a data-image-sizes JSON blob."""
    from urllib.parse import urljoin

    src = (img.get('src') or img.get('data-src') or img.get('data-lazy-src') or '').strip()
    if src:
        return urljoin(base_url, src) if base_url else src

    raw_sizes = img.get('data-image-sizes')
    if raw_sizes:
        import json
        from urllib.parse import unquote
        try:
            sizes = json.loads(unquote(raw_sizes))
            best = max(sizes, key=lambda s: s.get('width', 0))
            if best.get('url'):
                return best['url']
        except Exception:
            pass

    return None


def extract_content_html(html_text, base_url=None):
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

        # Resolve each <img> to a real, absolute src (handling lazy-load
        # attributes and Finalsite's data-image-sizes CDN blobs) — drop any
        # image we can't resolve a URL for.
        for img in target.find_all('img'):
            resolved = resolve_img_src(img, base_url)
            if resolved:
                img['src'] = resolved
            else:
                img.decompose()

        return str(target)
    except Exception:
        # If anything goes wrong, fall back to returning the raw HTML
        return html_text


def clean_export_html(html_text):
    """Strip content HTML down to headings, paragraphs, links, lists, and tables only,
    then reformat it with each block tag on its own line and no stray whitespace."""
    from bs4 import BeautifulSoup, NavigableString

    ALLOWED_TAGS = {
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'p', 'a', 'img',
        'ul', 'ol', 'li',
        'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th',
    }
    ALLOWED_ATTRS = {'a': {'href'}, 'img': {'src', 'alt'}}

    # Structural tags whose direct children are themselves tags (never meaningful
    # text) — each child gets its own output line. Everything else (headings,
    # paragraphs, list items, cells) renders its inline content on one line.
    CONTAINER_TAGS = {'ul', 'ol', 'table', 'thead', 'tbody', 'tfoot', 'tr'}
    LEAF_TAGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'td', 'th'}

    # UI chrome that carries no page content of its own (form controls, decorative
    # media, icons) — delete these outright rather than unwrapping, or their
    # screen-reader-only label text (e.g. "pause slideshow") leaks into the output.
    # Note: <figure> is deliberately NOT here — it's just a wrapper and often
    # holds the <img> we want to keep, so it gets unwrapped instead, below.
    CHROME_TAGS = {
        'button', 'input', 'select', 'option', 'textarea', 'label', 'form',
        'svg', 'fieldset', 'legend', 'figcaption',
        'video', 'audio', 'canvas',
    }
    SR_ONLY_HINTS = ('sr-only', 'sronly', 'visually-hidden', 'visuallyhidden', 'screen-reader')

    try:
        soup = BeautifulSoup(html_text, 'html.parser')

        for elem in soup.find_all(True):
            # A tag whose ancestor was already decomposed earlier in this same
            # loop (e.g. a <button><svg>...) is a dead object — .attrs is None
            # and .get() raises, which was silently swallowed by the outer
            # except and made this whole function a no-op on real pages.
            if elem.decomposed:
                continue
            if elem.name in CHROME_TAGS or elem.get('aria-hidden') == 'true':
                elem.decompose()
                continue
            attrs = ' '.join(filter(None, [
                elem.get('id') or '',
                ' '.join(elem.get('class') or []),
            ])).lower()
            if any(hint in attrs for hint in SR_ONLY_HINTS):
                elem.decompose()

        for elem in soup.find_all(True):
            if elem.name not in ALLOWED_TAGS:
                elem.unwrap()

        for elem in soup.find_all(True):
            keep = ALLOWED_ATTRS.get(elem.name, set())
            elem.attrs = {k: v for k, v in elem.attrs.items() if k in keep}

        # Drop links that lost their href, and images that lost their src,
        # and any tags left empty after unwrapping.
        for a in soup.find_all('a'):
            if not a.get('href'):
                a.unwrap()

        for img in soup.find_all('img'):
            if not img.get('src'):
                img.decompose()

        for tag_name in ('li', 'p', 'td', 'th'):
            for elem in soup.find_all(tag_name):
                if not elem.get_text(strip=True):
                    elem.decompose()

        # Collapse all internal whitespace runs (newlines, tabs, repeated spaces
        # from the original page's indentation) down to single spaces.
        for node in soup.find_all(string=True):
            collapsed = re.sub(r'\s+', ' ', str(node))
            node.replace_with(NavigableString(collapsed))

        # Container tags (ul/ol/table/thead/tbody/tfoot/tr) only ever hold other
        # tags — any text between their children is leftover indentation, so drop it.
        for elem in list(soup.find_all(CONTAINER_TAGS)) + [soup]:
            for child in list(elem.contents):
                if isinstance(child, NavigableString) and not child.strip():
                    child.extract()

        # Leaf/inline tags keep their text, but trim the whitespace that used to
        # separate them from sibling tags in the original markup.
        for elem in soup.find_all(list(LEAF_TAGS) + ['a']):
            if elem.contents and isinstance(elem.contents[0], NavigableString):
                elem.contents[0].replace_with(elem.contents[0].lstrip())
            if elem.contents and isinstance(elem.contents[-1], NavigableString):
                elem.contents[-1].replace_with(elem.contents[-1].rstrip())

        # Serialize with each container/leaf tag on its own line and no added
        # indentation — inline tags (links) stay inline within their parent line.
        def render(node):
            lines = []
            for child in node.contents:
                if isinstance(child, NavigableString):
                    if child.strip():
                        lines.append(str(child))
                    continue
                if child.name in CONTAINER_TAGS:
                    lines.append(f'<{child.name}>')
                    lines.extend(render(child))
                    lines.append(f'</{child.name}>')
                else:
                    lines.append(str(child))
            return lines

        return '\n'.join(render(soup))
    except Exception:
        return html_text


def get_keyword_trends(phrases):
    """Return {phrase: avg_interest_0_to_100_or_None} via pytrends (best-effort, never raises)."""
    if not phrases:
        return {}
    try:
        from pytrends.request import TrendReq
        import time
        scores = {}
        batches = [phrases[i:i + 5] for i in range(0, len(phrases), 5)]
        for idx, batch in enumerate(batches):
            try:
                pt = TrendReq(hl='en-US', tz=360, timeout=(10, 30), retries=1, backoff_factor=0.5)
                pt.build_payload(batch, timeframe='today 12-m', geo='US')
                df = pt.interest_over_time()
                for phrase in batch:
                    if df is not None and not df.empty and phrase in df.columns:
                        scores[phrase] = int(round(float(df[phrase].mean())))
                    else:
                        scores[phrase] = None
            except Exception:
                for phrase in batch:
                    scores[phrase] = None
            if idx < len(batches) - 1:
                time.sleep(2.5)
        return scores
    except Exception:
        return {p: None for p in phrases}


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
    data          = request.get_json(force=True)
    dl_name       = data.get('download_name', 'scrape_pdfs.xlsx')
    wide_headers  = data.get('wide_headers')   # present for SEO wide-pivot export
    wide_rows     = data.get('wide_rows')

    header_font = Font(bold=True, color='FFFFFF', size=11)
    header_fill = PatternFill(fill_type='solid', fgColor='1F3864')
    link_font   = Font(color='1155CC', size=10)
    easy_font   = Font(color='1E7E34', size=10, bold=True)
    hard_font   = Font(color='B8430A', size=10, bold=True)
    center      = Alignment(horizontal='center', vertical='center')
    left        = Alignment(horizontal='left', vertical='center', wrap_text=True)
    thin        = Side(style='thin', color='BFBFBF')
    border      = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'PDFs'

    if wide_headers and wide_rows:
        # ── Wide pivot format (SEO checker): one row per source URL ──────────
        # Column layout: Source URL (col 1), then groups of [PDF URL, Pages, Difficulty]
        num_cols = len(wide_headers)
        ws.column_dimensions['A'].width = 70  # Source URL
        for i in range(1, num_cols):
            col_letter = chr(ord('A') + i)
            header_lc  = wide_headers[i].lower()
            if 'url' in header_lc:
                ws.column_dimensions[col_letter].width = 70
            elif 'pages' in header_lc:
                ws.column_dimensions[col_letter].width = 10
            else:  # Difficulty
                ws.column_dimensions[col_letter].width = 14

        # Header row
        for col_idx, hdr in enumerate(wide_headers, 1):
            cell           = ws.cell(row=1, column=col_idx, value=hdr)
            cell.font      = header_font
            cell.fill      = header_fill
            cell.alignment = center
            cell.border    = border
        ws.row_dimensions[1].height = 20

        for row_idx, row_data in enumerate(wide_rows, 2):
            for col_idx, val in enumerate(row_data, 1):
                cell        = ws.cell(row=row_idx, column=col_idx, value=val if val != '' else None)
                cell.border = border
                header_lc   = wide_headers[col_idx - 1].lower()
                if col_idx == 1:                        # Source URL
                    cell.font      = Font(size=10)
                    cell.alignment = left
                elif 'url' in header_lc:               # PDF URL columns
                    cell.font      = link_font
                    cell.alignment = left
                elif 'difficulty' in header_lc:         # Difficulty columns
                    vl = str(val).lower() if val else ''
                    cell.font      = easy_font if vl == 'easy' else (hard_font if vl == 'hard' else Font(size=10))
                    cell.alignment = center
                else:                                   # Pages columns
                    cell.font      = Font(size=10)
                    cell.alignment = center
            ws.row_dimensions[row_idx].height = 16

    else:
        # ── Original tall format (single-scrape) ─────────────────────────────
        pdfs      = data.get('pdfs', [])
        col_label = data.get('col_label', 'Source / File Name')
        extra_cols = data.get('extra_cols', [])

        col_widths = [6, 60, 80] + [16] * len(extra_cols)
        for i, w in enumerate(col_widths):
            ws.column_dimensions[chr(ord('A') + i)].width = w

        headers = ['#', col_label, 'PDF URL'] + extra_cols
        ws.append(headers)
        for col in range(1, len(headers) + 1):
            cell           = ws.cell(row=1, column=col)
            cell.font      = header_font
            cell.fill      = header_fill
            cell.alignment = center
            cell.border    = border
        ws.row_dimensions[1].height = 20

        for i, pdf in enumerate(pdfs, 1):
            url   = pdf.get('url', '')
            label = pdf.get('label', '')

            ws.cell(row=i + 1, column=1, value=i).alignment = center
            ws.cell(row=i + 1, column=1).border             = border
            ws.cell(row=i + 1, column=1).font               = Font(size=10)

            c2 = ws.cell(row=i + 1, column=2, value=label)
            c2.font = Font(size=10); c2.alignment = left; c2.border = border

            c3 = ws.cell(row=i + 1, column=3, value=url)
            c3.font = link_font; c3.alignment = left; c3.border = border

            for j, col_name in enumerate(extra_cols):
                val  = pdf.get(col_name.lower(), '') or ''
                cell = ws.cell(row=i + 1, column=4 + j, value=val)
                cell.alignment = center
                cell.border    = border
                if col_name.lower() == 'difficulty':
                    vl = str(val).lower()
                    cell.font = easy_font if vl == 'easy' else (hard_font if vl == 'hard' else Font(size=10))
                else:
                    cell.font = Font(size=10)
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


@app.route('/seo-checker')
def seo_checker_page():
    return render_template('seo_checker.html')


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
        queued         = {url}
        queue          = deque([url])
        found_pdfs     = set()
        found_sites    = set()
        found_images   = set()
        found_trackers = set()  # "tracker_name|code" keys — unique script code, site-wide
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
                headers = {
                    'User-Agent': random.choice(USER_AGENTS),
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                }
                resp = req.get(current, headers=headers, timeout=10, allow_redirects=True)
                if resp.status_code != 200:
                    yield f"data: Skipped (HTTP {resp.status_code}): {current}\n\n"
                    continue

                content_type = resp.headers.get('Content-Type', '')
                if 'html' not in content_type:
                    continue

                raw_html = resp.text
                _html_cache[current] = extract_content_html(raw_html, current)
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
                        if full not in visited and full not in queued:
                            queued.add(full)
                            queue.append(full)
                    elif find_external and full not in found_externals:
                        found_externals.add(full)
                        rel = a.get('rel') or []
                        if isinstance(rel, str):
                            rel = rel.split()
                        nofollow = 'nofollow' in [r.lower() for r in rel]
                        is_ccsd = link_domain == 'ccsd.net' or link_domain.endswith('.ccsd.net')
                        yield f"data: EXTERNAL: {full}|{current}|{1 if nofollow else 0}|{1 if is_ccsd else 0}\n\n"

                # Collect images from <img> tags only (no external CDNs/other
                # sites) — except a page-embedded resource CDN (e.g. Finalsite's
                # data-image-sizes-hosted photos) still counts as first-party
                # since it's this site's own asset host, not a foreign reference.
                if find_images:
                    for img in soup.find_all('img'):
                        plain_src = (img.get('src') or img.get('data-src') or
                                     img.get('data-lazy-src') or '').strip()
                        is_first_party_cdn = not plain_src and img.get('data-image-sizes')
                        full_img = resolve_img_src(img, current)
                        if not full_img or not full_img.startswith('http') or full_img in found_images:
                            continue
                        if not is_first_party_cdn:
                            img_domain = urlparse(full_img).netloc.lower().removeprefix('www.')
                            if img_domain != base_domain:
                                continue
                        found_images.add(full_img)
                        yield f"data: IMG: {full_img}|{current}\n\n"

                # Detect tracking/analytics scripts — capture each script's actual
                # code (src URL for external scripts, full body for inline ones)
                # and dedupe site-wide so an identical snippet on every page only
                # shows up once.
                if find_tracking:
                    for script in soup.find_all('script'):
                        src  = (script.get('src') or '').strip()
                        text = (script.get_text() or '').strip()
                        haystack = f"{src} {text}".lower()
                        code = src or text
                        if not code:
                            continue
                        for name, patterns in TRACKER_PATTERNS:
                            if any(p.lower() in haystack for p in patterns):
                                key = f"{name}|{code}"
                                if key not in found_trackers:
                                    found_trackers.add(key)
                                    kind = 'src' if src else 'inline'
                                    code_b64 = base64.b64encode(code.encode('utf-8')).decode('ascii')
                                    yield f"data: TRACKER: {name}|{current}|{kind}|{code_b64}\n\n"
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
            headers = {
                'User-Agent': random.choice(USER_AGENTS),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
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
            _html_cache[url] = extract_content_html(raw_html, url)
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
            queued_pages  = {url}
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
                    resp = req.get(current, headers=make_headers(), timeout=12, allow_redirects=True)
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
                            if urlparse(full).netloc.removeprefix('www.') == base_domain_norm and full not in visited_pages and full not in queued_pages:
                                queued_pages.add(full)
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
                                item = future_map[fut]
                                total_checked += 1
                                yield f"data: CHECKING: {total_checked}|{item[1]}\n\n"
                                check_results.append((item, status, status_text))
                    except Exception as te:
                        yield f"data: Warning: thread pool error — {te}\n\n"

                    for res, status, status_text in check_results:
                        res_type, res_url, element, source_page, is_social = res
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


def _ensure_spell_ignore_table(conn):
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS spell_ignore (
                id       INT AUTO_INCREMENT PRIMARY KEY,
                word     VARCHAR(200) NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_word (word)
            )
        """)
        conn.commit()
        cur.close()
    except Exception:
        pass


@app.route('/api/spell-ignore', methods=['GET'])
def get_spell_ignore():
    conn = get_db_connection()
    if not conn:
        return jsonify({'words': []})
    try:
        _ensure_spell_ignore_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT word, added_at FROM spell_ignore ORDER BY word ASC")
        words = [{'word': row[0], 'added_at': str(row[1])} for row in cur.fetchall()]
        cur.close()
        return jsonify({'words': words})
    except Exception as e:
        return jsonify({'words': [], 'error': str(e)})
    finally:
        conn.close()


@app.route('/api/spell-ignore', methods=['POST'])
def add_spell_ignore():
    word = ((request.get_json(force=True) or {}).get('word') or '').strip().lower()
    if not word:
        return jsonify({'ok': False, 'error': 'No word provided'}), 400
    conn = get_db_connection()
    if not conn:
        return jsonify({'ok': False, 'error': 'DB unavailable'}), 500
    try:
        _ensure_spell_ignore_table(conn)
        cur = conn.cursor()
        cur.execute("INSERT IGNORE INTO spell_ignore (word) VALUES (%s)", (word,))
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'word': word})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/spell-ignore/<path:word>', methods=['DELETE'])
def delete_spell_ignore(word):
    word = word.strip().lower()
    conn = get_db_connection()
    if not conn:
        return jsonify({'ok': False, 'error': 'DB unavailable'}), 500
    try:
        _ensure_spell_ignore_table(conn)
        cur = conn.cursor()
        cur.execute("DELETE FROM spell_ignore WHERE word = %s", (word,))
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'word': word})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/ollama-models')
def get_ollama_models_route():
    base_url = get_ollama_url()
    running, installed = list_ollama_models(base_url)
    default = running[0] if running else (installed[0] if installed else None)
    return jsonify({
        'ok': bool(installed or running),
        'running': running,
        'installed': installed,
        'default': default,
    })


@app.route('/api/seo-check')
def run_seo_check():
    import requests as req
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, urljoin
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time, random, json

    url            = request.args.get('url', '').strip()
    mode           = request.args.get('mode', 'site')   # 'page' | 'site'
    find_pdf       = request.args.get('pdf', 'true').lower() == 'true'
    find_sites     = request.args.get('sites', 'true').lower() == 'true'
    find_external  = request.args.get('external', 'true').lower() == 'true'
    find_tracking  = request.args.get('tracking', 'true').lower() == 'true'
    find_alt       = request.args.get('alt', 'true').lower() == 'true'
    find_spell     = request.args.get('spell', 'true').lower() == 'true'
    find_ai        = request.args.get('ai', 'true').lower() == 'true'
    find_keywords  = request.args.get('kw', 'false').lower() == 'true'
    requested_model = (request.args.get('model') or '').strip()
    ollama_url     = get_ollama_url()
    OLLAMA_TIMEOUT = int(os.getenv('OLLAMA_TIMEOUT', '300'))
    check_ext_stat = request.args.get('check_external', 'false').lower() == 'true'
    check_social   = request.args.get('social', 'false').lower() == 'true'

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
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
        'Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0',
    ]

    def make_headers():
        return {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }

    def check_url_status(check_u):
        def _get(hdrs):
            r = req.get(check_u, headers=hdrs, timeout=7, allow_redirects=True, stream=True)
            r.close()
            return r
        try:
            r = _get(make_headers())
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

    def resolve_ollama_model():
        if requested_model:
            return requested_model
        running, installed = list_ollama_models(ollama_url)
        if running:
            return running[0]
        if installed:
            return installed[0]
        return None

    def get_ollama_suggestion(model, current_title, current_description, content_text):
        prompt = (
            "You are an SEO specialist. Based on the webpage content below, write an "
            "improved, accurate SEO title tag (max 60 characters) and meta description "
            "(max 155 characters) for this exact page. Respond with exactly these two "
            "lines and nothing else:\n"
            "TITLE: <suggested title>\n"
            "DESCRIPTION: <suggested description>\n\n"
            f"Current title: {current_title or '(none)'}\n"
            f"Current meta description: {current_description or '(none)'}\n\n"
            f"Page content:\n{content_text[:3000]}"
        )
        r = req.post(
            f"{ollama_url}/api/generate",
            json={'model': model, 'prompt': prompt, 'stream': False},
            timeout=OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
        text = r.json().get('response', '')
        title_m = re.search(r'TITLE:\s*(.+)', text)
        desc_m  = re.search(r'DESCRIPTION:\s*(.+)', text, re.S)
        suggested_title = title_m.group(1).strip().strip('"') if title_m else ''
        suggested_desc  = desc_m.group(1).strip().strip('"').split('\n')[0] if desc_m else ''
        return suggested_title, suggested_desc

    def get_ollama_keywords(model, content_text, page_url):
        prompt = (
            "You are an expert SEO keyword strategist. Analyze the webpage content below and generate "
            "high-traffic, brand-specific keyword phrases for this site. This tool is used on ALL types "
            "of websites — agencies, retailers, restaurants, law firms, schools, nonprofits, SaaS products, "
            "medical practices, contractors, blogs, etc. Adapt your output to whatever industry this site is in.\n\n"
            f"Page URL: {page_url}\n\n"
            "--- INSTRUCTIONS ---\n\n"
            "STEP 1: Read the content and URL. Identify:\n"
            "  - The brand/organization name as it is commonly known (org_name)\n"
            "  - Any short version or abbreviation (org_short) — use the full name if no abbreviation exists\n"
            "  - The industry or business type (e.g., digital marketing agency, Italian restaurant, personal injury law firm)\n\n"
            "STEP 2: List the top 3 topics, products, or services this specific page focuses on.\n\n"
            "STEP 3: Generate 4–5 short branded keyword phrases (2–5 words each) this page should rank for.\n"
            "  - Every phrase must contain the brand name or abbreviation from Step 1\n"
            "  - Examples of the FORMAT (not the content — use the actual brand you found):\n"
            "      [Brand] [service], [Brand] [city], [Brand] [product] reviews, [Brand] vs competitors\n"
            "  - Add a location only if the site serves a specific geographic area\n"
            "  - No generic phrases — each phrase must only make sense for this specific brand\n\n"
            "STEP 4: Generate 6–9 long-tail phrases a real visitor would type into Google to reach this page.\n"
            "  - Every phrase must contain the brand name or abbreviation\n"
            "  - Match the phrasing to what someone in this industry's audience would search\n"
            "  - Cover a mix of: finding the brand (Navigational), learning something (Informational), "
            "taking action (Action-oriented)\n"
            "  - No phrase should be so generic it could apply to any company\n\n"
            "STEP 5: Label each long-tail phrase with its user intent: Navigational, Informational, or Action-oriented.\n\n"
            "--- OUTPUT FORMAT ---\n\n"
            "Return ONLY a valid JSON object. No markdown, no explanation, no code fences.\n\n"
            "{\n"
            "  \"org_name\": \"<full brand name detected from content>\",\n"
            "  \"org_short\": \"<short name or same as org_name>\",\n"
            "  \"current_topics\": [\"<topic 1>\", \"<topic 2>\", \"<topic 3>\"],\n"
            "  \"target_keywords\": [\n"
            "    {\"phrase\": \"<brand short keyword 1>\"},\n"
            "    {\"phrase\": \"<brand short keyword 2>\"},\n"
            "    {\"phrase\": \"<brand short keyword 3>\"},\n"
            "    {\"phrase\": \"<brand short keyword 4>\"},\n"
            "    {\"phrase\": \"<brand short keyword 5>\"}\n"
            "  ],\n"
            "  \"community_search_suggestions\": [\n"
            "    {\"phrase\": \"<long-tail phrase 1>\", \"user_intent\": \"<intent>\"},\n"
            "    {\"phrase\": \"<long-tail phrase 2>\", \"user_intent\": \"<intent>\"},\n"
            "    {\"phrase\": \"<long-tail phrase 3>\", \"user_intent\": \"<intent>\"},\n"
            "    {\"phrase\": \"<long-tail phrase 4>\", \"user_intent\": \"<intent>\"},\n"
            "    {\"phrase\": \"<long-tail phrase 5>\", \"user_intent\": \"<intent>\"},\n"
            "    {\"phrase\": \"<long-tail phrase 6>\", \"user_intent\": \"<intent>\"}\n"
            "  ]\n"
            "}\n\n"
            "--- WEBPAGE CONTENT ---\n"
            f"{content_text[:4000]}"
        )
        r = req.post(
            f"{ollama_url}/api/generate",
            json={'model': model, 'prompt': prompt, 'stream': False},
            timeout=OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
        text = r.json().get('response', '').strip()
        # Strip markdown code fences that some models emit
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text).strip()
        # Extract the outermost JSON object
        m = re.search(r'\{[\s\S]*\}', text)
        raw = m.group() if m else text
        # Remove trailing commas before ] or } which some models produce
        raw = re.sub(r',\s*([}\]])', r'\1', raw)
        return json.loads(raw)

    spell = get_spell_checker() if find_spell else None

    def generate():
        global _html_cache
        _html_cache = {}  # clear previous scrape

        parsed_start  = urlparse(url)
        base_domain   = parsed_start.netloc.lower().removeprefix('www.')
        is_ccsd_domain = 'ccsd.net' in base_domain

        visited         = set()
        queued          = {url}
        checked_urls    = set()
        queue           = deque([url])

        found_pdfs      = set()
        found_sites     = set()
        found_externals = set()
        found_trackers  = set()
        found_images    = set()
        crawled_pages   = []  # [{'url':..., 'title':..., 'description':...}] — used for the spellcheck/AI passes that run after the crawl

        pages_crawled = 0
        total_checked = 0
        broken_count  = 0
        spell_count   = 0
        ai_count      = 0
        keyword_count = 0

        yield f"data: Starting SEO check — {url}\n\n"
        yield f"data: Domain: {base_domain}\n\n"

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)

            try:
                resp = req.get(current, headers=make_headers(), timeout=12, allow_redirects=True)
                if resp.status_code != 200:
                    yield f"data: Skipped (HTTP {resp.status_code}): {current}\n\n"
                    continue
                if 'html' not in resp.headers.get('Content-Type', ''):
                    continue

                # /fs/pages/ shortlink dedup — ccsd.net only.
                # These CMS shortlinks redirect to canonical URLs; if the destination
                # was already crawled (or will be), skip the /fs/pages/ duplicate.
                effective = current
                if is_ccsd_domain and re.search(r'/fs/pages/\d+', current):
                    canonical = resp.url.rstrip('/')
                    if (canonical != current and
                            urlparse(canonical).netloc.lower().removeprefix('www.') == base_domain):
                        if canonical in visited:
                            yield f"data: Deduped: {current} → {canonical}\n\n"
                            continue
                        visited.add(canonical)
                        queued.add(canonical)
                        effective = canonical

                pages_crawled += 1
                yield f"data: PAGE: {pages_crawled}\n\n"
                yield f"data: [{pages_crawled}] {effective}\n\n"

                raw_html     = resp.text
                content_html = extract_content_html(raw_html, effective)
                _html_cache[effective] = content_html
                soup = BeautifulSoup(raw_html, 'html.parser')

                # Title & meta description
                title_tag   = soup.find('title')
                page_title  = title_tag.get_text().strip()[:160] if title_tag else ''
                desc_tag    = soup.find('meta', attrs={'name': re.compile(r'^description$', re.I)})
                description = (desc_tag.get('content') or '').strip()[:300] if desc_tag else ''
                yield f"data: PAGEINFO: {json.dumps({'url': effective, 'title': page_title, 'description': description}, ensure_ascii=True)}\n\n"
                crawled_pages.append({'url': effective, 'title': page_title, 'description': description})

                pending_checks = []  # (type, url, element, source_page, is_social)

                # Walk <a> tags: PDFs / Google Sites / External links / internal queueing / broken-check collection
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if not href or href.startswith(('mailto:', 'javascript:', '#', 'tel:')):
                        continue
                    full = urljoin(effective, href).split('#')[0].rstrip('/')
                    if not full.startswith('http'):
                        continue

                    data_file = (a.get('data-file-name') or '').strip()
                    url_path  = full.lower().split('?')[0]
                    is_pdf    = url_path.endswith('.pdf') or data_file.lower().endswith('.pdf')

                    if find_pdf and is_pdf and full not in found_pdfs:
                        found_pdfs.add(full)
                        yield f"data: PDF: {full}|{effective}\n\n"

                    if find_sites and 'sites.google.com' in full and full not in found_sites:
                        found_sites.add(full)
                        yield f"data: SITE: {full}|{effective}\n\n"

                    link_domain = urlparse(full).netloc.lower().removeprefix('www.')
                    is_internal = link_domain == base_domain

                    if is_internal:
                        if mode == 'site' and full not in visited and full not in queued:
                            queued.add(full)
                            queue.append(full)
                    elif find_external and full not in found_externals:
                        found_externals.add(full)
                        rel = a.get('rel') or []
                        if isinstance(rel, str):
                            rel = rel.split()
                        nofollow = 'nofollow' in [r.lower() for r in rel]
                        is_ccsd  = link_domain == 'ccsd.net' or link_domain.endswith('.ccsd.net')
                        yield f"data: EXTERNAL: {full}|{effective}|{1 if nofollow else 0}|{1 if is_ccsd else 0}\n\n"

                    if full not in checked_urls:
                        social = is_social_url(full)
                        if is_pdf:
                            if find_pdf:
                                pending_checks.append(('pdf', full, a, effective, social))
                        elif is_internal:
                            pending_checks.append(('link', full, a, effective, social))
                        elif check_ext_stat and not (social and not check_social):
                            pending_checks.append(('link', full, a, effective, social))

                # Images: alt-tag analysis + broken-check collection
                if find_alt:
                    for img in soup.find_all('img'):
                        src = (img.get('src') or img.get('data-src') or img.get('data-lazy-src') or '').strip()
                        if not src:
                            continue
                        full_img = urljoin(effective, src)
                        if not full_img.startswith('http'):
                            continue
                        alt     = img.get('alt')
                        has_alt = alt is not None and alt.strip() != ''
                        if full_img not in found_images:
                            found_images.add(full_img)
                            payload = json.dumps({
                                'src': full_img, 'alt': alt or '', 'hasAlt': has_alt, 'source': effective,
                            }, ensure_ascii=True)
                            yield f"data: IMGALT: {payload}\n\n"
                        if full_img not in checked_urls:
                            pending_checks.append(('image', full_img, img, effective, False))

                # Tracking/analytics scripts
                if find_tracking:
                    script_text = ''
                    for script in soup.find_all('script'):
                        script_text += ' ' + (script.get('src') or '')
                        script_text += ' ' + (script.get_text() or '')
                    for name, patterns in TRACKER_PATTERNS:
                        for pattern in patterns:
                            if pattern.lower() in script_text.lower():
                                key = f"{name}|{effective}"
                                if key not in found_trackers:
                                    found_trackers.add(key)
                                    yield f"data: TRACKER: {name}|{effective}\n\n"
                                break

                # Broken-link/image/pdf status checks
                new_checks = []
                for item in pending_checks:
                    if item[1] not in checked_urls:
                        checked_urls.add(item[1])
                        new_checks.append(item)

                if new_checks:
                    yield f"data: Checking {len(new_checks)} resource(s)…\n\n"

                check_results = []
                try:
                    with ThreadPoolExecutor(max_workers=3) as executor:
                        future_map = {executor.submit(check_url_status, r[1]): r for r in new_checks}
                        for fut in as_completed(future_map):
                            try:
                                status, status_text = fut.result()
                            except Exception as fe:
                                status, status_text = 0, type(fe).__name__
                            item = future_map[fut]
                            total_checked += 1
                            yield f"data: CHECKING: {total_checked}|{item[1]}\n\n"
                            check_results.append((item, status, status_text))
                except Exception as te:
                    yield f"data: Warning: thread pool error — {te}\n\n"

                for res, status, status_text in check_results:
                    res_type, res_url, element, source_page, social = res
                    if status == 0 or status >= 400:
                        if social and status != 404:
                            continue
                        broken_count += 1
                        try:
                            payload = json.dumps({
                                'type': res_type, 'url': res_url, 'status': status,
                                'statusText': status_text, 'source': source_page,
                                'snippet': get_snippet(element),
                            }, ensure_ascii=True)
                        except Exception:
                            payload = json.dumps({
                                'type': res_type, 'url': res_url, 'status': status,
                                'statusText': status_text, 'source': source_page, 'snippet': '',
                            })
                        yield f"data: BROKEN: {payload}\n\n"
                        yield f"data: BROKEN_COUNT: {broken_count}\n\n"

            except Exception as e:
                yield f"data: Error ({current}): {type(e).__name__}: {e}\n\n"

            time.sleep(0.15)

        # ---- Phase 1.5: PDF page count ----
        if find_pdf and found_pdfs:
            yield f"data: Counting PDF pages ({len(found_pdfs)} files)…\n\n"

            def count_pdf_pages(pdf_url):
                try:
                    r = req.get(pdf_url, headers={'User-Agent': random.choice(USER_AGENTS)},
                                timeout=20, stream=True)
                    if r.status_code != 200:
                        r.close()
                        return None
                    data = b''
                    for chunk in r.iter_content(8192):
                        data += chunk
                        if len(data) >= 524288:  # 512 KB is enough for page-tree metadata
                            break
                    r.close()
                    counts = re.findall(rb'/Count\s+(\d+)', data)
                    if counts:
                        return max(int(c) for c in counts)
                    pages = len(re.findall(rb'/Type\s*/Page\b', data))
                    return pages if pages > 0 else None
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=3) as pdf_ex:
                pdf_futures = {pdf_ex.submit(count_pdf_pages, u): u for u in found_pdfs}
                for fut in as_completed(pdf_futures):
                    pdf_url = pdf_futures[fut]
                    try:
                        pages = fut.result()
                    except Exception:
                        pages = None
                    if pages is not None:
                        difficulty = 'easy' if pages == 1 else 'hard'
                    else:
                        difficulty = 'unknown'
                    yield f"data: PDF_INFO: {json.dumps({'url': pdf_url, 'pages': pages, 'difficulty': difficulty}, ensure_ascii=True)}\n\n"

        # ---- Phase 2: spell-check every crawled page, now that discovery/link
        # checking is fully done (keeps slow per-page work from stalling the crawl) ----
        if find_spell and crawled_pages:
            # Load ignore list from DB once before the loop
            ignore_words = set()
            try:
                ig_conn = get_db_connection()
                if ig_conn:
                    _ensure_spell_ignore_table(ig_conn)
                    ig_cur = ig_conn.cursor()
                    ig_cur.execute("SELECT word FROM spell_ignore")
                    ignore_words = {row[0].lower() for row in ig_cur.fetchall()}
                    ig_cur.close()
                    ig_conn.close()
            except Exception:
                pass

            yield f"data: \n\n"
            yield f"data: Spell-checking {len(crawled_pages)} page(s)... ({len(ignore_words)} word(s) in ignore list)\n\n"
            for pdata in crawled_pages:
                page_url = pdata['url']
                content_html = _html_cache.get(page_url, '')
                if not content_html:
                    continue
                content_soup = BeautifulSoup(content_html, 'html.parser')
                candidates = []
                for node in content_soup.find_all(string=True):
                    if node.parent and node.parent.name in ('script', 'style'):
                        continue
                    for w in re.findall(r"[A-Za-z']+", str(node)):
                        if len(w) < 3 or w[:1].isupper():
                            continue
                        candidates.append(w.strip("'").lower())

                unknown = spell.unknown(candidates) if candidates else set()
                unknown -= ignore_words  # filter out ignored words
                if not unknown:
                    continue
                reported = set()
                for node in content_soup.find_all(string=True):
                    if node.parent and node.parent.name in ('script', 'style'):
                        continue
                    for w in re.findall(r"[A-Za-z']+", str(node)):
                        if len(w) < 3 or w[:1].isupper():
                            continue
                        wl = w.strip("'").lower()
                        if wl in unknown and wl not in reported:
                            reported.add(wl)
                            suggestion = spell.correction(wl) or ''
                            snippet = get_snippet(node.parent) if node.parent else str(node).strip()[:300]
                            spell_count += 1
                            payload = json.dumps({
                                'word': w, 'suggestion': suggestion,
                                'snippet': snippet, 'source': page_url,
                            }, ensure_ascii=True)
                            yield f"data: SPELL: {payload}\n\n"
                            yield f"data: SPELL_COUNT: {spell_count}\n\n"

        # ---- Phase 3: ask Ollama for title/description suggestions for every
        # crawled page, now that everything else has finished ----
        if find_ai and crawled_pages:
            resolved_model = resolve_ollama_model()
            if resolved_model:
                yield f"data: \n\n"
                yield f"data: Generating AI suggestions for {len(crawled_pages)} page(s) using {resolved_model}...\n\n"
                for pdata in crawled_pages:
                    page_url = pdata['url']
                    content_html = _html_cache.get(page_url, '')
                    if not content_html:
                        continue
                    try:
                        content_text = BeautifulSoup(content_html, 'html.parser').get_text(separator=' ', strip=True)
                        content_text = re.sub(r'\s+', ' ', content_text).strip()
                        if not content_text:
                            continue
                        yield f"data: Asking Ollama ({resolved_model}) for suggestions — {page_url}\n\n"
                        sugg_title, sugg_desc = get_ollama_suggestion(
                            resolved_model, pdata['title'], pdata['description'], content_text,
                        )
                        if sugg_title or sugg_desc:
                            ai_count += 1
                            payload = json.dumps({
                                'url': page_url,
                                'currentTitle': pdata['title'],
                                'currentDescription': pdata['description'],
                                'suggestedTitle': sugg_title,
                                'suggestedDescription': sugg_desc,
                            }, ensure_ascii=True)
                            yield f"data: AISEO: {payload}\n\n"
                    except req.exceptions.ReadTimeout:
                        yield f"data: AI suggestion timed out for {page_url} (>{OLLAMA_TIMEOUT}s) — skipping.\n\n"
                    except req.exceptions.ConnectionError:
                        yield f"data: AI suggestion skipped — could not connect to Ollama at {ollama_url}\n\n"
                    except Exception as e:
                        yield f"data: AI suggestion failed for {page_url}: {type(e).__name__}: {e}\n\n"
            else:
                yield f"data: AI suggestions disabled — no Ollama model found at {ollama_url}\n\n"

        # ---- Phase 4: keyword analysis (Ollama) + Google Trends interest scores ----
        if find_keywords and crawled_pages:
            resolved_model = resolve_ollama_model()
            if resolved_model:
                yield f"data: \n\n"
                yield f"data: Analyzing keywords for {len(crawled_pages)} page(s) using {resolved_model}...\n\n"
                trends_cache = {}
                for pdata in crawled_pages:
                    page_url     = pdata['url']
                    content_html = _html_cache.get(page_url, '')
                    if not content_html:
                        continue
                    try:
                        content_text = BeautifulSoup(content_html, 'html.parser').get_text(separator=' ', strip=True)
                        content_text = re.sub(r'\s+', ' ', content_text).strip()
                        if not content_text:
                            continue
                        yield f"data: Analyzing keywords — {page_url}\n\n"
                        kw_data        = get_ollama_keywords(resolved_model, content_text, page_url)
                        suggestions    = kw_data.get('community_search_suggestions', [])
                        target_kws     = kw_data.get('target_keywords', [])
                        all_phrases    = (
                            [t.get('phrase', '') for t in target_kws if t.get('phrase')] +
                            [s.get('phrase', '') for s in suggestions if s.get('phrase')]
                        )
                        new_phrases = [p for p in all_phrases if p not in trends_cache]
                        if new_phrases:
                            yield f"data: Checking Google Trends for {len(new_phrases)} phrase(s)...\n\n"
                            trends_cache.update(get_keyword_trends(new_phrases))
                        trends = {p: trends_cache.get(p) for p in all_phrases}
                        keyword_count += 1
                        payload = json.dumps({
                            'url':             page_url,
                            'org_name':        kw_data.get('org_name', ''),
                            'org_short':       kw_data.get('org_short', ''),
                            'topics':          kw_data.get('current_topics', []),
                            'target_keywords': target_kws,
                            'suggestions':     suggestions,
                            'trends':          trends,
                        }, ensure_ascii=True)
                        yield f"data: KEYWORDS: {payload}\n\n"
                    except req.exceptions.ReadTimeout:
                        yield f"data: Keyword analysis timed out for {page_url} — skipping.\n\n"
                    except req.exceptions.ConnectionError:
                        yield f"data: Keyword analysis skipped — could not connect to Ollama at {ollama_url}\n\n"
                    except Exception as e:
                        yield f"data: Keyword analysis failed for {page_url}: {type(e).__name__}: {e}\n\n"
            else:
                yield f"data: Keyword analysis disabled — no Ollama model found at {ollama_url}\n\n"

        yield f"data: \n\n"
        yield (
            f"data: Done. {pages_crawled} page(s) crawled, {total_checked} resource(s) checked. "
            f"{len(found_pdfs)} PDF(s), {len(found_sites)} Google Sites, {len(found_externals)} external link(s), "
            f"{len(found_trackers)} tracker event(s), {len(found_images)} image(s), "
            f"{spell_count} spelling issue(s), {ai_count} AI suggestion(s), "
            f"{keyword_count} keyword analysis page(s), "
            f"{broken_count} broken resource(s) found.\n\n"
        )
        yield "data: __SUCCESS__\n\n"

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/api/page-html')
def get_page_html():
    page_url = request.args.get('url', '').strip()
    html = _html_cache.get(page_url, '')
    return jsonify({'html': html, 'found': bool(html)})


@app.route('/api/export/html-zip')
def export_html_zip():
    import zipfile
    from urllib.parse import urlparse

    if not _html_cache:
        return 'No HTML cached — run a scrape first', 400

    buf = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for page_url, content in _html_cache.items():
            name = url_to_filename(page_url)
            base, ext = name[:-4], name[-4:]
            final, n = name, 2
            while final in used_names:
                final = f"{base}_{n}{ext}"
                n += 1
            used_names.add(final)
            zf.writestr(final, clean_export_html(content) if content else '')
    buf.seek(0)

    first_host = urlparse(next(iter(_html_cache))).netloc.lower().removeprefix('www.') or 'site'
    zip_name = f"{first_host}_html.zip"

    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)


@app.route('/api/export/images-zip', methods=['POST'])
def export_images_zip():
    import zipfile
    import requests as req
    from urllib.parse import urlparse

    data       = request.get_json(silent=True) or {}
    image_urls = data.get('images', [])
    if not image_urls:
        return 'No images to export', 400

    buf = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for img_url in image_urls:
            try:
                resp = req.get(img_url, timeout=15)
                if resp.status_code != 200:
                    continue
                filename = img_url.split('/')[-1].split('?')[0] or 'image'
                if '.' not in filename:
                    ct  = resp.headers.get('Content-Type', 'image/jpeg')
                    ext = ct.split('/')[-1].split(';')[0].strip() or 'jpg'
                    filename = f'{filename}.{ext}'
                filename = re.sub(r'[^a-zA-Z0-9\-_.]', '_', filename)

                final, n = filename, 2
                while final in used_names:
                    stem, dot, ext = filename.rpartition('.')
                    final = f"{stem}_{n}{dot}{ext}" if dot else f"{filename}_{n}"
                    n += 1
                used_names.add(final)
                zf.writestr(final, resp.content)
            except Exception:
                continue

    if not used_names:
        return 'Could not download any images', 502

    buf.seek(0)

    first_host = urlparse(image_urls[0]).netloc.lower().removeprefix('www.') or 'site'
    zip_name = f"{first_host}_images.zip"

    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)


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


# ── PDF to Image ─────────────────────────────────────────────────────────────

@app.route('/pdf-to-image')
def pdf_to_image_page():
    return render_template('pdf_to_image.html')


@app.route('/api/pdf-scrape')
def run_pdf_scrape():
    """SSE: crawl a single page or an entire domain, reporting only PDF links found."""
    import requests as req
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, urljoin
    from collections import deque
    import time
    import random

    url  = request.args.get('url', '').strip()
    mode = request.args.get('mode', 'page')  # 'page' or 'site'

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

    def fetch_html(target):
        headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        return req.get(target, headers=headers, timeout=10, allow_redirects=True)

    def pdfs_on_page(soup, current):
        found = []
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
            found.append((full, is_pdf))
        return found

    def generate():
        found_pdfs = set()

        if mode == 'site':
            parsed_start = urlparse(url)
            base_domain  = parsed_start.netloc.lower().removeprefix('www.')
            visited = set()
            queued  = {url}
            queue   = deque([url])
            pages_crawled = 0

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
                    resp = fetch_html(current)
                    if resp.status_code != 200:
                        yield f"data: Skipped (HTTP {resp.status_code}): {current}\n\n"
                        continue
                    content_type = resp.headers.get('Content-Type', '')
                    if 'html' not in content_type:
                        continue

                    soup = BeautifulSoup(resp.text, 'html.parser')

                    for full, is_pdf in pdfs_on_page(soup, current):
                        if is_pdf:
                            if full not in found_pdfs:
                                found_pdfs.add(full)
                                yield f"data: PDF: {full}|{current}\n\n"
                            continue

                        link_domain = urlparse(full).netloc.lower().removeprefix('www.')
                        if link_domain == base_domain and full not in visited and full not in queued:
                            queued.add(full)
                            queue.append(full)

                except Exception as e:
                    yield f"data: Error ({current}): {e}\n\n"

                time.sleep(0.15)

            yield f"data: \n\n"
            yield f"data: Done. {pages_crawled} page(s) crawled, {len(found_pdfs)} PDF(s) found.\n\n"
            yield "data: __SUCCESS__\n\n"

        else:
            yield f"data: Fetching {url}\n\n"
            try:
                resp = fetch_html(url)
                if resp.status_code != 200:
                    yield f"data: ERROR: HTTP {resp.status_code} — {url}\n\n"
                    yield "data: __FAILURE__\n\n"
                    return
                content_type = resp.headers.get('Content-Type', '')
                if 'html' not in content_type:
                    yield f"data: ERROR: Not an HTML page (Content-Type: {content_type})\n\n"
                    yield "data: __FAILURE__\n\n"
                    return

                soup = BeautifulSoup(resp.text, 'html.parser')
                for full, is_pdf in pdfs_on_page(soup, url):
                    if is_pdf and full not in found_pdfs:
                        found_pdfs.add(full)
                        yield f"data: PDF: {full}|{url}\n\n"

                yield f"data: \n\n"
                yield f"data: Done. {len(found_pdfs)} PDF(s) found.\n\n"
                yield "data: __SUCCESS__\n\n"
            except Exception as e:
                yield f"data: Error: {e}\n\n"
                yield "data: __FAILURE__\n\n"

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/api/pdf-to-image/convert', methods=['POST'])
def convert_pdfs_to_images():
    """SSE: download each given PDF, render every page to PNG, then stack those pages into
    a single tall PNG per PDF (one image per PDF, not one image per page)."""
    import requests as req
    import fitz  # PyMuPDF

    data = request.get_json(force=True)
    pdfs = data.get('pdfs', [])  # [{'url':..., 'source':...}, ...]
    try:
        dpi = int(data.get('dpi', 150))
    except (TypeError, ValueError):
        dpi = 150
    dpi = max(72, min(dpi, 400))

    def generate():
        global _pdf_image_cache
        _pdf_image_cache = {}

        total       = len(pdfs)
        done        = 0
        total_pages = 0

        yield f"data: Converting {total} PDF(s) at {dpi} DPI...\n\n"

        zoom   = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        for entry in pdfs:
            pdf_url = (entry.get('url') or '').strip()
            source  = entry.get('source') or ''
            if not pdf_url:
                continue

            basename = pdf_url_to_basename(pdf_url)
            yield f"data: PDF_START: {pdf_url}\n\n"

            try:
                resp = req.get(pdf_url, timeout=30, stream=True)
                if resp.status_code != 200:
                    raise Exception(f"HTTP {resp.status_code}")
                pdf_bytes = resp.content

                doc = fitz.open(stream=pdf_bytes, filetype='pdf')
                if doc.is_encrypted and not doc.authenticate(''):
                    raise Exception('Encrypted / password-protected PDF')

                page_count = doc.page_count
                yield f"data: PDF_INFO: {pdf_url}|{page_count}\n\n"

                page_pngs = []
                for i in range(page_count):
                    pix = doc.load_page(i).get_pixmap(matrix=matrix)
                    page_pngs.append(pix.tobytes('png'))
                    yield f"data: PAGE_DONE: {pdf_url}|{i + 1}|{page_count}\n\n"
                doc.close()

                if page_count > 1:
                    yield f"data: PDF_MERGING: {pdf_url}|{page_count}\n\n"
                combined = stack_pages_into_one_image(page_pngs)

                _pdf_image_cache[pdf_url] = {
                    'source': source, 'image': combined, 'page_count': page_count,
                    'error': None, 'basename': basename,
                }
                total_pages += page_count
                done += 1
                yield f"data: PDF_DONE: {pdf_url}|{page_count}\n\n"

            except Exception as e:
                _pdf_image_cache[pdf_url] = {
                    'source': source, 'image': None, 'page_count': 0,
                    'error': str(e), 'basename': basename,
                }
                done += 1
                yield f"data: PDF_ERROR: {pdf_url}|{e}\n\n"

            yield f"data: PROGRESS: {done}|{total}\n\n"

        yield f"data: \n\n"
        yield f"data: Done. Converted {done} of {total} PDF(s) into one image each ({total_pages} page(s) merged total).\n\n"
        yield "data: __SUCCESS__\n\n"

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/api/pdf-to-image/thumbnail')
def pdf_to_image_thumbnail():
    pdf_url = request.args.get('url', '').strip()
    entry = _pdf_image_cache.get(pdf_url)
    if not entry or not entry.get('image'):
        return 'No image available', 404
    return Response(entry['image'], mimetype='image/png')


@app.route('/api/pdf-to-image/download')
def pdf_to_image_download():
    """Download one PDF's single merged image."""
    pdf_url = request.args.get('url', '').strip()
    entry = _pdf_image_cache.get(pdf_url)
    if not entry or not entry.get('image'):
        return 'No image available', 404

    return Response(
        entry['image'],
        mimetype='image/png',
        headers={'Content-Disposition': f'attachment; filename="{entry["basename"]}.png"'}
    )


@app.route('/api/pdf-to-image/export-zip', methods=['POST'])
def pdf_to_image_export_zip():
    """Zip every cached converted image — one image per PDF — (or a given subset of PDF URLs)."""
    import zipfile

    data = request.get_json(silent=True) or {}
    urls = data.get('urls') or list(_pdf_image_cache.keys())

    buf = io.BytesIO()
    used_names = set()
    any_written = False
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for pdf_url in urls:
            entry = _pdf_image_cache.get(pdf_url)
            if not entry or not entry.get('image'):
                continue
            name = f"{entry['basename']}.png"
            final, n = name, 2
            while final in used_names:
                stem, dot, ext = name.rpartition('.')
                final = f"{stem}_{n}{dot}{ext}" if dot else f"{name}_{n}"
                n += 1
            used_names.add(final)
            zf.writestr(final, entry['image'])
            any_written = True

    if not any_written:
        return 'No converted images to export', 400

    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name='pdf_images.zip')


if __name__ == '__main__':
    app.run(debug=True, port=5002)