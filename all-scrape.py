
import subprocess
import sys


def run_script(script_name):
    """Runs a python script, streaming output in real time."""
    print(f"--- Running {script_name} ---", flush=True)
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", script_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in process.stdout:
            print(line, end="", flush=True)
        process.wait()
        if process.returncode != 0:
            print(f"!!! {script_name} exited with code {process.returncode} !!!", flush=True)
            print(f"--- Halting execution due to error in {script_name} ---", flush=True)
            sys.exit(1)
        print(f"--- Finished {script_name} ---\n", flush=True)
    except FileNotFoundError:
        print(f"!!! Error: {script_name} not found. !!!", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    run_script("scraper.py")
    run_script("scraper-html.py")
    run_script("scraper_pdf.py")
    run_script("scraper_drive_links.py")
    run_script("scraper_google_sites.py")
    run_script("add_to_database.py")
    print("--- All scraping scripts completed. ---", flush=True)
