import os
import sys
import subprocess
import logging
import shutil

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Add default downloads path in user's home directory
DEFAULT_DOWNLOADS_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "MediaDownloader")

DEPENDENCIES_FLAG = os.path.join(os.path.dirname(__file__), ".dependencies_installed")
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

def ensure_downloads_directory():
    """Create default downloads directory if it doesn't exist."""
    try:
        os.makedirs(DEFAULT_DOWNLOADS_DIR, exist_ok=True)
        logging.info(f"Using downloads directory: {DEFAULT_DOWNLOADS_DIR}")
        os.environ["MEDIA_DOWNLOADS_DIR"] = DEFAULT_DOWNLOADS_DIR
    except Exception as e:
        logging.error(f"Failed to create downloads directory: {str(e)}")
        sys.exit(1)

def install_missing_dependencies():
    """Install missing dependencies and mark them as installed."""
    if os.path.exists(DEPENDENCIES_FLAG):
        logging.info("Dependencies already installed, skipping installation.")
    else:
        try:
            subprocess.check_call([sys.executable, "-m", "ensurepip"])
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to ensure pip: {str(e)}")
            sys.exit(1)

        required = {
            "flask": "Flask",
            "yt_dlp": "yt-dlp",
            "spotipy": "spotipy",
            "mutagen": "mutagen",
            "requests": "requests",
            "urllib3": "urllib3",
            "concurrent.futures": "concurrent.futures"
        }
        
        for module_name, package_name in required.items():
            try:
                __import__(module_name)
                logging.info(f"{package_name} is already installed.")
            except ImportError:
                if module_name == "concurrent.futures":
                    logging.error("concurrent.futures should be built-in but is missing. Please check your Python installation.")
                    sys.exit(1)
                logging.info(f"Installing {package_name}...")
                try:
                    subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to install {package_name}: {str(e)}")
                    sys.exit(1)

        if not shutil.which("ffmpeg"):
            logging.info("Installing ffmpeg...")
            try:
                subprocess.check_call(["winget", "install", "ffmpeg"])
            except (subprocess.CalledProcessError, FileNotFoundError):
                try:
                    subprocess.check_call([sys.executable, "-m", "pip", "install", "ffmpeg-python"])
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to install ffmpeg: {str(e)}")
                    sys.exit(1)
        
        try:
            with open(DEPENDENCIES_FLAG, "w") as f:
                f.write("Dependencies installed on " + sys.version)
            logging.info("All dependencies installed successfully.")
        except IOError as e:
            logging.error(f"Failed to create dependencies flag file: {str(e)}")
            sys.exit(1)

def check_cookies_file():
    """Check if cookies.txt exists and set the environment variable."""
    if os.path.exists(COOKIES_FILE):
        os.environ["YTDLP_COOKIES_FILE"] = COOKIES_FILE
        logging.info(f"Set YTDLP_COOKIES_FILE to {COOKIES_FILE}")
    else:
        logging.warning(f"cookies.txt not found in {os.path.dirname(__file__)}. Proceeding without cookies.")

if __name__ == "__main__":
    ensure_downloads_directory()
    install_missing_dependencies()
    check_cookies_file()
    from main import launch_app
    launch_app()

