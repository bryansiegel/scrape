import random
import requests
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

def scrape_website(base_url):
    output_drive_links_file = "scraper_drive_links.txt"
    urls_to_visit = deque([base_url])
    visited_urls = set([base_url])
    error_urls = []

    with open(output_drive_links_file, "w", encoding="utf-8") as drivefile:
        while urls_to_visit:
            current_url = urls_to_visit.popleft()
            print(f"Scraping: {current_url}")

            try:
                headers = {"User-Agent": random.choice(USER_AGENTS)}
                response = requests.get(current_url, headers=headers, timeout=5)
                response.raise_for_status()

                soup = BeautifulSoup(response.content, "html.parser")
                base_netloc = urlparse(base_url).netloc

                for link in soup.find_all("a", href=True):
                    absolute_link = urljoin(current_url, link["href"]).split('#')[0]
                    
                    if "drive.google.com" in absolute_link:
                        drivefile.write(f"{absolute_link}\n")

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

    print(f"Scraping complete. Results saved to {output_drive_links_file}")
    if error_urls:
        print(f"A log of failed URLs has been saved to error_log.txt")

if __name__ == "__main__":
    scrape_website("https://ccsd.net")
