# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a web scraping project designed to systematically crawl and extract content from the Clark County School District (CCSD) website (https://ccsd.net). The project consists of multiple specialized scrapers that collect different types of content and organize them for storage in a MySQL database.

## Common Commands

### Run All Scrapers
```bash
python all-scrape.py
```
This executes all scraper scripts in sequence: main scraper, HTML content scraper, PDF downloader, Google Drive links scraper, Google Sites scraper, and database insertion.

### Run Individual Scrapers
```bash
# Main website scraper - extracts links and categorizes them
python scraper.py

# HTML content scraper - downloads full HTML content from pages
python scraper-html.py

# PDF downloader - downloads all PDF files found
python scraper_pdf.py

# Google Drive links extractor
python scraper_drive_links.py

# Google Sites links extractor
python scraper_google_sites.py

# Database insertion script
python add_to_database.py
```

### Install Dependencies
```bash
pip install -r requirements.txt
```

## Architecture

### Core Components

**Main Scraper (`scraper.py`)**
- Crawls the CCSD website starting from https://ccsd.net
- Categorizes links into departments, divisions, PDFs, and general pages
- Uses breadth-first search with a queue-based approach
- Implements request throttling and random user agents
- Outputs: `scraped_links.txt`, `scraped_pdf.txt`, `scraper_departments.txt`, `scraper_divisions.txt`

**HTML Content Scraper (`scraper-html.py`)**
- Downloads full HTML content from all discovered pages
- Extracts content-wrap class elements specifically
- Implements content deduplication using MD5 hashing
- Saves content to individual text files in the `html/` directory
- Sanitizes URLs for valid filenames

**PDF Downloader (`scraper_pdf.py`)**
- Downloads all PDF files found during scraping
- Streams downloads for memory efficiency
- Saves files to the `pdf/` directory with original filenames

**Specialized Link Extractors**
- `scraper_drive_links.py`: Finds Google Drive links embedded in pages
- `scraper_google_sites.py`: Discovers Google Sites references

**Database Integration (`add_to_database.py`)**
- Creates MySQL database and tables if they don't exist
- Inserts scraped data into categorized columns
- Database: `scrape`, Table: `pages` with columns for each content type
- Requires MySQL server running locally with user 'root' and password 'advanced'

### Data Organization

**Output Files Structure:**
- `scraped_links.txt` - General website links
- `scraped_pdf.txt` - PDF file URLs
- `scraper_departments.txt` - Department-specific pages
- `scraper_divisions.txt` - Division-specific pages
- `scraper_drive_links.txt` - Google Drive links
- `scraped_google_sites.txt` - Google Sites links
- `html/` - Directory containing extracted HTML content
- `pdf/` - Directory containing downloaded PDF files
- `error_log.txt` - Failed requests log

### Key Features

**Request Management:**
- Randomized user agents to avoid detection
- Request timeouts and error handling
- Deduplication to prevent re-crawling
- Domain-specific crawling (stays within ccsd.net)

**Content Processing:**
- Fragment removal from URLs (#anchors)
- Query parameter filtering
- Content-specific extraction (content-wrap elements)
- File sanitization for cross-platform compatibility

## Database Schema

The MySQL database uses a single table `pages` with the following structure:
- `id` (auto-increment primary key)
- `departments` (text) - Department page links
- `drive` (text) - Google Drive links  
- `divisions` (text) - Division page links
- `general` (text) - General website links
- `googleSites` (text) - Google Sites links

## Prerequisites

- Python 3.x
- MySQL server running locally
- Dependencies: requests, beautifulsoup4, mysql-connector-python