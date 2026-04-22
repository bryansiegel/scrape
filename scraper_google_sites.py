import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin, urlparse
import random

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Safari/605.1.15',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/109.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:108.0) Gecko/20100101 Firefox/108.0',
    'Mozilla/5.0 (X11; Linux i686; rv:108.0) Gecko/20100101 Firefox/108.0'
]

def crawl_website(start_url):
    domain_name = urlparse(start_url).netloc
    urls_to_visit = {start_url}
    visited_urls = set()
    google_sites_links = set()

    while urls_to_visit:
        url = urls_to_visit.pop()
        if url in visited_urls:
            continue

        print(f"Scraping: {url}")
        visited_urls.add(url)

        try:
            headers = {'User-Agent': random.choice(USER_AGENTS)}
            response = requests.get(url, headers=headers, timeout=5)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error making request to {url}: {e}")
            continue

        soup = BeautifulSoup(response.text, 'html.parser')
        links = soup.find_all('a', href=True)

        for link in links:
            href = link['href']
            absolute_link = urljoin(url, href)
            parsed_link = urlparse(absolute_link)

            if re.search(r'sites.google.com', parsed_link.netloc):
                google_sites_links.add(absolute_link)

            if parsed_link.netloc == domain_name and parsed_link.scheme in ['http', 'https']:
                if absolute_link not in visited_urls:
                    urls_to_visit.add(absolute_link)

    with open('scraped_google_sites.txt', 'w') as f:
        for site_link in sorted(list(google_sites_links)):
            f.write(f"{site_link}\n")

    print(f"\nFinished crawling. Found {len(google_sites_links)} Google Sites links.")
    print(f"Saved links to scraped_google_sites.txt")

if __name__ == "__main__":
    crawl_website('https://ccsd.net')
