import os
import requests
from urllib.parse import urlparse

def download_pdfs_from_file(filename="scraped_pdf.txt", download_folder="pdf"):
    """
    Reads PDF links from a text file and downloads them into a specified folder.

    Args:
        filename (str): The name of the file containing PDF URLs.
        download_folder (str): The folder where PDFs will be saved.
    """
    # Ensure the download folder exists
    if not os.path.exists(download_folder):
        os.makedirs(download_folder)

    # Read the URLs from the file
    try:
        with open(filename, 'r') as f:
            urls = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Error: The file '{filename}' was not found.")
        return

    for url in urls:
        try:
            # Parse the URL to get the path and extract the filename
            parsed_url = urlparse(url)
            pdf_name = os.path.basename(parsed_url.path)

            # If the name is empty (e.g., root URL), skip it
            if not pdf_name:
                print(f"Could not determine filename for URL: {url}")
                continue

            # Create the full path to save the file
            file_path = os.path.join(download_folder, pdf_name)

            # Download the PDF
            print(f"Downloading {pdf_name} from {url}...")
            response = requests.get(url, stream=True)
            response.raise_for_status()  # Raise an exception for bad status codes

            # Save the PDF to the specified path
            with open(file_path, 'wb') as pdf_file:
                for chunk in response.iter_content(chunk_size=8192):
                    pdf_file.write(chunk)
            
            print(f"Successfully saved {pdf_name} to {file_path}")

        except requests.exceptions.RequestException as e:
            print(f"Failed to download {url}. Error: {e}")
        except Exception as e:
            print(f"An unexpected error occurred for URL {url}: {e}")

if __name__ == "__main__":
    download_pdfs_from_file()