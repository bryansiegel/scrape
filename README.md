# CCSD Website Scraper

A web scraping toolkit and browser-based dashboard for crawling the Clark County School District (CCSD) website (`ccsd.net`). It collects, categorizes, and stores links, PDFs, Google Sites references, Google Drive links, and full HTML content, then exposes everything through a searchable Flask web interface.

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

### Single Site Scraper (`/single-scrape`)
Point-and-click scraper for any URL. Crawls within the target domain and collects:
- **PDFs** — all `.pdf` links found
- **Google Sites** — any `sites.google.com` references
- **All Links** — every page crawled, with a **Copy HTML** button (returns clean content-only HTML — no nav, footer, sidebars, CSS classes, or inline styles)
- **Images** — with thumbnail previews and per-image download
- **Scripts & Tracking** — detects 24+ analytics and pixel tools (Google Analytics, Facebook Pixel, HotJar, Microsoft Clarity, LinkedIn Insight Tag, TikTok Pixel, etc.)

### Web Dashboard (`/`)
- Search and filter all collected URLs by category
- Paginated results with autocomplete
- Export PDFs to formatted Excel (`.xlsx`)

---

## Prerequisites

- Python 3.9+
- MySQL 8.x running locally

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
```

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

### 6. Set up the database

Start MySQL, then import the included dump:

```bash
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS scrape;"
mysql -u root -p scrape < _db/scrape.sql
```

### 7. Run the web app

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
├── app.py                   # Flask web app and API
├── all-scrape.py            # Runs all scrapers in sequence
├── scraper.py               # Main CCSD link crawler
├── scraper-html.py          # HTML content downloader
├── scraper_pdf.py           # PDF downloader
├── scraper_drive_links.py   # Google Drive link extractor
├── scraper_google_sites.py  # Google Sites link extractor
├── add_to_database.py       # MySQL insertion
├── templates/
│   ├── index.html           # Main dashboard
│   └── single_scrape.html   # Single site scraper UI
├── _db/
│   └── scrape.sql           # MySQL dump for import
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
