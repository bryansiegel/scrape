import os
import random
import re
import requests
import hashlib
from collections import deque
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:53.0) Gecko/20100101 Firefox/53.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_5) AppleWebKit/603.2.4 (KHTML, like Gecko) Version/10.1.1 Safari/603.2.4',
    'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36',
    'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:53.0) Gecko/20100101 Firefox/53.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.11; rv:46.0) Gecko/20100101 Firefox/46.0',
    'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:53.0) Gecko/20100101 Firefox/53.0',
    'Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; Trident/5.0)'
]

def sanitize_filename(url):
    """Sanitizes a URL to be used as a valid filename."""
    path = urlparse(url).path
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', path)
    return sanitized.strip('./_ ').replace(" ", "_") + ".txt"

def scrape_all_html_content(base_url):
    output_dir = "html"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    urls_to_visit = deque([base_url])
    visited_urls = set([base_url])
    error_urls = []
    content_hashes = set()

    while urls_to_visit:
        current_url = urls_to_visit.popleft()
        print(f"Scraping: {current_url}")

        try:
            headers = {"User-Agent": random.choice(USER_AGENTS)}
            response = requests.get(current_url, headers=headers, timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")
            base_netloc = urlparse(base_url).netloc

            content_wraps = soup.find_all(class_="content-wrap")
            if content_wraps:
                content_str = "".join(str(c) for c in content_wraps)
                content_hash = hashlib.md5(content_str.encode('utf-8')).hexdigest()

                if content_hash not in content_hashes:
                    content_hashes.add(content_hash)
                    filename = sanitize_filename(current_url)
                    filepath = os.path.join(output_dir, filename)
                    with open(filepath, "w", encoding="utf-8") as html_file:
                        html_file.write(content_str)
                    print(f"  -> Saved HTML from {current_url} to {filepath}")
                else:
                    print(f"  -> Duplicate content found at {current_url}. Skipping.")

            for link in soup.find_all("a", href=True):
                absolute_link = urljoin(current_url, link["href"])
                if '?' in absolute_link:
                    continue
                parsed_link = urlparse(absolute_link)

                if parsed_link.netloc == base_netloc and absolute_link not in visited_urls:
                    visited_urls.add(absolute_link)
                    urls_to_visit.append(absolute_link)

        except requests.RequestException as e:
            print(f"Error scraping {current_url}: {e}")
            error_urls.append(current_url)

    if error_urls:
        with open("error_log.txt", "w", encoding="utf-8") as error_file:
            error_file.write("Failed to scrape the following URLs:\n")
            for url in error_urls:
                error_file.write(f"{url}\n")

    print(f"Scraping complete. HTML content saved to the '{output_dir}' directory.")
    if error_urls:
        print(f"A log of failed URLs has been saved to error_log.txt")

if __name__ == "__main__":
    scrape_all_html_content("https://ccsd.net")
