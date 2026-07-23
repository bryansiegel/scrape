# CCSD Website Scraper

A web scraping toolkit and browser-based dashboard for crawling the Clark County School District (CCSD) website (`ccsd.net`). It collects, categorizes, and stores links, PDFs, Google Sites references, Google Drive links, and full HTML content, then exposes everything — plus a set of on-demand auditing tools (SEO, broken links, PDF conversion, accessibility) — through a Flask web interface.

---

## What It Does

### Full-site scrapers (`all-scrape.py`)
Runs the following scripts in sequence against `ccsd.net`:

| Script | Output |
|---|---|
| `scraper.py` | Categorizes all links into departments, divisions, PDFs, and general pages |
| `scraper-html.py` | Downloads the content HTML of every discovered page into `html/` |
| `scraper_pdf.py` | Downloads every PDF into `pdf/` |
| `scraper_drive_links.py` | Extracts Google Drive links |
| `scraper_google_sites.py` | Extracts Google Sites references |
| `add_to_database.py` | Inserts all collected links into MySQL |

### Web Dashboard (`/`)
- Search and filter all collected URLs by category
- Paginated results with autocomplete
- Export PDFs to formatted Excel (`.xlsx`)
- Trigger any scraper from the **Scrape** menu, with live streamed output

### Single Site Scraper (`/single-scrape`)
Point-and-click scraper for any URL. Crawls within the target domain and collects:
- **PDFs** — all `.pdf` links found
- **Google Sites** — any `sites.google.com` references
- **All Links** — every page crawled, with a **Copy HTML** button (returns clean content-only HTML — no nav, footer, sidebars, CSS classes, or inline styles)
- **Images** — with thumbnail previews and per-image download
- **Scripts & Tracking** — detects 24+ analytics and pixel tools (Google Analytics, Facebook Pixel, HotJar, Microsoft Clarity, LinkedIn Insight Tag, TikTok Pixel, etc.)

### Page Scrape (`/page-scrape`)
Same extraction as Single Site Scraper (links, PDFs, Google Sites, images, tracking scripts) but scoped to one page only — no crawling.

### Broken Link Checker (`/broken-link-checker`)
Crawls a page or an entire site to find broken links, missing images, and inaccessible PDFs. Each result includes the source HTML snapshot showing exactly where the broken reference appears.

### SEO Checker (`/seo-checker`)
Crawls a page or an entire site to review titles, meta descriptions, image alt tags, spelling, tracking scripts, and broken links/images/PDFs all in one pass. Can use a local [Ollama](https://ollama.com) instance to suggest improved titles/descriptions and generate keyword/traffic analysis.

### PDF to Image (`/pdf-to-image`)
Finds PDFs on a page or an entire site, converts each one to a single stacked PNG image, and exports the results — so you can swap PDF links for images on your pages.

### Accessibility Checker (`/accessibility-checker`)
Crawls a page or an entire site and audits it for accessibility in one pass:
- **WCAG page scan** — runs [axe-core](https://github.com/dequelabs/axe-core) (the same rule engine WAVE is built on) against every crawled page in a headless browser, producing a 0–100 score per page. An **Open in WAVE** button pops open WebAIM's free hosted report for a human cross-check.
- **Alt tags** — flags every image missing (or with) alt text, with the URL and the image tag's raw source markup, filterable and exportable to Excel.
- **PDF accessibility** — after the page crawl finishes, checks every PDF found for PDF/UA structural accessibility (tagged, language, title, figure alt-text coverage, heading structure) — the same category of checks [check.axes4.com](https://check.axes4.com/en) applies, with a link to test there manually. Local Ollama can flag non-descriptive alt text.
- **Accessibility Score** — a 0–100 composite averaged across every page and PDF scanned, shown at the top of the page.

No paid API keys are required for any of this — everything runs locally.

---

## Prerequisites

- Python 3.9+
- MySQL 8.x running locally
- (Optional) [Ollama](https://ollama.com) running locally, for AI-assisted SEO suggestions and PDF alt-text review
- ~300 MB free disk space for the Playwright Chromium browser used by the Accessibility Checker

---

## Setup

### 1. Clone the repo

```bash
git clone <repo-url>
cd scrape
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

This installs Flask, mysql-connector-python, requests, beautifulsoup4, openpyxl, playwright, pikepdf, and related packages. The `playwright install chromium` step is a one-time download (~150–300 MB) of the headless browser used by the Accessibility Checker's WCAG scan — it only needs to be run once per machine.

### 4. Configure environment variables

Copy the example and fill in your credentials:

```bash
cp .env.example .env
```

`.env`:
```
DB_HOST=127.0.0.1
DB_USER=root
DB_PASSWORD=your_password
DB_NAME=scrape
```

`.env` is listed in `.gitignore` and will never be committed.

### 5. Set up the database

Start MySQL, then import the included dump:

```bash
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS scrape;"
mysql -u root -p scrape < _db/scrape.sql
```

### 6. Run the web app

```bash
python app.py
```

Open `http://localhost:5002` in your browser.

---

## Running the Scrapers

### All at once

```bash
python all-scrape.py
```

### Individually

```bash
python scraper.py              # crawl ccsd.net, categorize links
python scraper-html.py         # download HTML content
python scraper_pdf.py          # download PDFs
python scraper_drive_links.py  # extract Google Drive links
python scraper_google_sites.py # extract Google Sites links
python add_to_database.py      # push results to MySQL
```

You can also trigger any scraper from the **Scrape** menu in the web dashboard, which streams live output back to the browser.

---

## Project Structure

```
scrape/
├── app.py                          # Flask web app and API for every tool below
├── all-scrape.py                   # Runs all scrapers in sequence
├── scraper.py                      # Main CCSD link crawler
├── scraper-html.py                 # HTML content downloader
├── scraper_pdf.py                  # PDF downloader
├── scraper_drive_links.py          # Google Drive link extractor
├── scraper_google_sites.py         # Google Sites link extractor
├── add_to_database.py              # MySQL insertion
├── templates/
│   ├── index.html                  # Main dashboard (/)
│   ├── single_scrape.html          # Single Site Scraper UI (/single-scrape)
│   ├── page_scrape.html            # Page Scrape UI (/page-scrape)
│   ├── broken_link_checker.html    # Broken Link Checker UI (/broken-link-checker)
│   ├── seo_checker.html            # SEO Checker UI (/seo-checker)
│   ├── pdf_to_image.html           # PDF to Image UI (/pdf-to-image)
│   └── accessibility_checker.html  # Accessibility Checker UI (/accessibility-checker)
├── static/
│   └── vendor/axe.min.js           # Vendored axe-core build used by the Accessibility Checker
├── _db/
│   └── scrape.sql                  # MySQL dump for import
├── requirements.txt
└── .gitignore
```

---

## Database Schema

Table: `pages` (database: `scrape`)

| Column | Type | Description |
|---|---|---|
| `id` | INT (PK) | Auto-increment |
| `departments` | TEXT | Department page URLs |
| `divisions` | TEXT | Division page URLs |
| `general` | TEXT | General page URLs |
| `drive` | TEXT | Google Drive links |
| `googleSites` | TEXT | Google Sites links |

PDFs are stored in `scraped_pdf.txt` (format: `pdf_url|source_page`) and read directly by the app — they do not have a database column.
