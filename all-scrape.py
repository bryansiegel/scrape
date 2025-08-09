
import subprocess
import sys

def run_script(script_name):
    """Runs a python script and checks for errors."""
    print(f"--- Running {script_name} ---")
    try:
        # Using sys.executable to ensure we use the same python interpreter
        result = subprocess.run([sys.executable, script_name], check=True, text=True, capture_output=True)
        print(result.stdout)
        if result.stderr:
            print("--- Errors ---")
            print(result.stderr)
        print(f"--- Finished {script_name} ---\
")
    except subprocess.CalledProcessError as e:
        print(f"!!! Error running {script_name} !!!")
        print(e.stdout)
        print(e.stderr)
        print(f"--- Halting execution due to error in {script_name} ---")
        sys.exit(1) # Exit the script if a scraper fails
    except FileNotFoundError:
        print(f"!!! Error: {script_name} not found. !!!")
        sys.exit(1)

if __name__ == "__main__":
    run_script("scraper.py")
    run_script("scraper-html.py")
    run_script("scraper_pdf.py")
    run_script("scraper_drive_links.py")
    run_script("scraper_google_sites.py")
    run_script("add_to_database.py")
    print("--- All scraping scripts completed. ---")
