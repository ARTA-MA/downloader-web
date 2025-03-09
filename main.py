import os
import re
import sys
import logging
import threading
import requests
import concurrent.futures
import urllib3
from typing import Any, Dict, Tuple, List
from queue import Queue
from flask import Flask, request, render_template, jsonify
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC
from mutagen.mp3 import MP3
import webbrowser
from yt_dlp import YoutubeDL
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

download_status = {
    "status": "idle",
    "message": "No download in progress",
    "progress": 0,
    "speed": "N/A",
    "completed_tracks": [],
    "total_tracks": 0
}
download_status_lock = threading.Lock()

def sanitize_filename(filename: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", filename)[:200]

class DownloadManager:
    stop_flag: bool = False

    @staticmethod
    def reset_stop() -> None:
        DownloadManager.stop_flag = False

    @staticmethod
    def set_stop() -> None:
        DownloadManager.stop_flag = True

    @staticmethod
    def is_valid_youtube_url(url: str) -> bool:
        pattern = r'(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)'
        return bool(re.search(pattern, url))

    @staticmethod
    def is_valid_soundcloud_url(url: str) -> bool:
        return url.startswith("https://soundcloud.com/")

    @staticmethod
    def _clean_ansi_string(text: str) -> str:
        return re.sub(r'\x1b\[[0-9;]*[mK]', '', text).strip()

    @staticmethod
    def _clean_percent_string(percent_str: str) -> float:
        clean_str = DownloadManager._clean_ansi_string(percent_str).strip('%')
        try:
            return float(clean_str)
        except ValueError:
            logging.warning(f"Could not parse percent string '{percent_str}', defaulting to 0.0")
            return 0.0

    @staticmethod
    def _download_progress_hook(d: Dict[str, Any]) -> None:
        global download_status
        if DownloadManager.stop_flag:
            raise Exception("Download stopped by user.")
        if d.get("status") == "downloading":
            percent_str = d.get("_percent_str", "0%")
            percent = DownloadManager._clean_percent_string(percent_str)
            speed_raw = d.get("_speed_str", "N/A")
            speed = DownloadManager._clean_ansi_string(speed_raw)
            eta = d.get("_eta_str", "N/A")
            logging.info(f"Downloading: {percent}% at {speed} | ETA: {eta}")
            with download_status_lock:
                download_status.update({
                    "status": "running",
                    "progress": min(100, percent),
                    "speed": speed,
                    "message": f"Downloading at {speed}, ETA: {eta}"
                })
        elif d.get("status") == "finished":
            with download_status_lock:
                download_status.update({
                    "status": "running",
                    "message": "Download completed for current track",
                    "progress": 100,
                    "speed": "N/A"
                })

    @staticmethod
    def clean_track_info(original_title: str, uploader: str) -> Tuple[str, str]:
        original_title = original_title.strip() if original_title else "Unknown Title"
        uploader = uploader.strip() if uploader else "Unknown Artist"
        pattern = r"^(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
        m = re.match(pattern, original_title)
        if m:
            artist = m.group("artist").strip()
            title = m.group("title").strip()
        else:
            artist = uploader if uploader != "Unknown Artist" else "Unknown Artist"
            title = original_title
        title = re.sub(r"\s*[\[\(].*?[\]\)]$", "", title).strip()
        artist = re.sub(r"\s*[\[\(].*?[\]\)]$", "", artist).strip()
        return title or "Unknown Title", artist or "Unknown Artist"

    @staticmethod
    def download_youtube(query: str, output_path: str, is_audio: bool = True, quality: str = None, metadata: dict = None, max_retries: int = 3) -> str:
        try:
            if DownloadManager.stop_flag:
                raise Exception("Download stopped before start.")
            quality_setting = quality or "1080"  # Default to 1080p if not specified
            format_str = ("bestaudio/best" if is_audio 
                          else f'bestvideo[ext=mp4][height<={quality_setting}]+bestaudio[ext=m4a]/best[ext=mp4]')
            
            outtmpl = (f"{sanitize_filename(metadata['artist'])} - {sanitize_filename(metadata['name'])}.%(ext)s" 
                       if metadata else "%(title)s.%(ext)s")
            ydl_opts = {
                "outtmpl": os.path.join(output_path, outtmpl),
                "default_search": "ytsearch",
                "format": format_str,
                "quiet": True,
                "progress_hooks": [DownloadManager._download_progress_hook],
                "continue": True,
                "noplaylist": True,
                "ignoreerrors": False,
                "cookiefile": os.getenv("YTDLP_COOKIES_FILE"),
            }
            if is_audio:
                ydl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",  # Changed to 320 kbps for YouTube audio
                }]

            retry_count = 0
            while retry_count < max_retries:
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(query, download=False)
                        if not info:
                            raise Exception("No video found for query.")
                        if info.get("age_limit", 0) > 0 or "Sign in to confirm your age" in str(info.get("reason", "")):
                            raise Exception("Age-restricted content detected.")
                        if not metadata:
                            cleaned_title, cleaned_artist = DownloadManager.clean_track_info(info.get("title", ""), info.get("uploader", ""))
                            metadata = {
                                "name": cleaned_title,
                                "artist": cleaned_artist,
                                "album": info.get("album", ""),
                                "cover_url": info.get("thumbnail")
                            }
                        expected_filename = ydl.prepare_filename(info)
                        if is_audio:
                            base, _ = os.path.splitext(expected_filename)
                            expected_filename = f"{base}.mp3"
                        if os.path.exists(expected_filename):
                            logging.info(f"File exists, skipping: {expected_filename}")
                        else:
                            ydl.download([info["webpage_url"]])
                        DownloadManager.update_mp3_metadata(expected_filename, metadata)
                        return expected_filename
                except Exception as e:
                    retry_count += 1
                    if "age" in str(e).lower() and retry_count == max_retries:
                        logging.warning(f"Age restriction on YouTube, falling back to SoundCloud for: {query}")
                        return DownloadManager.fallback_download(query, output_path, "soundcloud", metadata)
                    elif retry_count < max_retries:
                        query += " alternative official audio -inurl:(lyrics topic)"
                        logging.warning(f"Retry {retry_count}/{max_retries}: {str(e)}")
                    else:
                        raise
            raise Exception("Failed after retries.")
        except Exception as e:
            logging.error(f"YouTube download failed: {str(e)}")
            raise

    @staticmethod
    def download_soundcloud(url: str, output_path: str, metadata: dict = None) -> str:
        try:
            outtmpl = (f"{sanitize_filename(metadata['artist'])} - {sanitize_filename(metadata['name'])}.%(ext)s" 
                       if metadata else "%(uploader)s - %(title)s.%(ext)s")
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(output_path, outtmpl),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",  # Set to 320 kbps
                }],
                "quiet": True,
                "progress_hooks": [DownloadManager._download_progress_hook],
                "continue": True
            }
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not metadata:
                    cleaned_title, cleaned_artist = DownloadManager.clean_track_info(info.get("title", ""), info.get("uploader", ""))
                    metadata = {
                        "name": cleaned_title,
                        "artist": cleaned_artist,
                        "album": "",
                        "cover_url": info.get("thumbnail")
                    }
                expected_filename = ydl.prepare_filename(info)
                base, _ = os.path.splitext(expected_filename)
                expected_filename = f"{base}.mp3"
                if os.path.exists(expected_filename):
                    logging.info(f"File exists, skipping: {expected_filename}")
                else:
                    ydl.download([url])
                DownloadManager.update_mp3_metadata(expected_filename, metadata)
                return expected_filename
        except Exception as e:
            logging.error(f"SoundCloud download failed: {str(e)}")
            raise

    @staticmethod
    def fallback_download(query: str, output_path: str, fallback_service: str, metadata: dict = None) -> str:
        try:
            if fallback_service == "soundcloud":
                search_query = f"{metadata['artist']} {metadata['name']}" if metadata else query
                tracks = DownloadManager.get_soundcloud_tracks(f"https://soundcloud.com/search?q={search_query}")
                if tracks and tracks[0]["url"]:
                    return DownloadManager.download_soundcloud(tracks[0]["url"], output_path, metadata)
                else:
                    logging.debug(f"No SoundCloud tracks found for query: {search_query}")
                    raise Exception("No tracks available on SoundCloud")
            elif fallback_service == "youtube":
                search_query = f"{metadata['artist']} {metadata['name']} official audio -inurl:(lyrics topic)" if metadata else query
                return DownloadManager.download_youtube(search_query, output_path, is_audio=True, metadata=metadata)
            raise Exception(f"No valid fallback for {fallback_service}")
        except Exception as e:
            logging.debug(f"Fallback to {fallback_service} failed: {str(e)}")
            raise

    @staticmethod
    def initialize_spotify() -> spotipy.Spotify:
        try:
            auth_manager = SpotifyClientCredentials(
                client_id=os.getenv("SPOTIFY_CLIENT_ID", "62c275d1bc1d44ac83c311056b42d55a"),
                client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "1a259f3ab52b42cfa5229abaeba31fd3")
            )
            return spotipy.Spotify(auth_manager=auth_manager)
        except Exception as e:
            logging.error(f"Spotify initialization failed: {str(e)}")
            return None

    @staticmethod
    def get_spotify_tracks(sp_client: spotipy.Spotify, url: str) -> List[Dict]:
        try:
            if "playlist" in url:
                playlist_id = url.split("/")[-1].split("?")[0]
                tracks = []
                offset = 0
                while True:
                    results = sp_client.playlist_tracks(playlist_id, offset=offset)
                    items = results.get("items", [])
                    if not items:
                        break
                    for item in items:
                        if track := item.get("track"):
                            tracks.append({
                                "name": track["name"],
                                "artist": track["artists"][0]["name"],
                                "album": track["album"]["name"],
                                "cover_url": track["album"]["images"][0]["url"] if track["album"].get("images") else None
                            })
                    if not results.get("next"):
                        break
                    offset += len(items)
                return tracks
            elif "track" in url:
                track_id = url.split("/")[-1].split("?")[0]
                track = sp_client.track(track_id)
                return [{
                    "name": track["name"],
                    "artist": track["artists"][0]["name"],
                    "album": track["album"]["name"],
                    "cover_url": track["album"]["images"][0]["url"] if track["album"].get("images") else None
                }]
            return None
        except Exception as e:
            logging.error(f"Spotify track retrieval failed: {str(e)}")
            return None

    @staticmethod
    def get_soundcloud_tracks(url: str) -> List[Dict]:
        try:
            ydl_opts = {
                "extract_flat": True,
                "quiet": True,
                "get_thumbnails": True,
                "playlist_items": "0-1000",
            }
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                tracks = []
                def create_track_dict(item, source_url):
                    title = item.get("title", "Unknown Title")
                    uploader = item.get("uploader", "Unknown Artist")
                    cleaned_title, cleaned_artist = DownloadManager.clean_track_info(title, uploader)
                    return {
                        "name": cleaned_title,
                        "artist": cleaned_artist,
                        "album": "",
                        "cover_url": item.get("thumbnail"),
                        "url": item.get("url", source_url)
                    }
                if "entries" in info:
                    tracks = [create_track_dict(entry, url) for entry in info["entries"] if entry]
                else:
                    tracks.append(create_track_dict(info, url))
                if not tracks:
                    logging.debug(f"No tracks found for SoundCloud URL: {url}")
                    return None
                return tracks
        except Exception as e:
            if "404" in str(e):
                logging.debug(f"SoundCloud returned 404 for URL: {url}")
            else:
                logging.error(f"SoundCloud track retrieval failed: {str(e)}")
            return None

    @staticmethod
    def get_youtube_tracks(url: str) -> List[Dict]:
        try:
            ydl_opts = {"extract_flat": True, "quiet": True}
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                tracks = []
                if "entries" in info:
                    for entry in info["entries"]:
                        tracks.append({
                            "name": entry.get("title", ""),
                            "artist": entry.get("uploader", ""),
                            "album": "",
                            "cover_url": entry.get("thumbnail")
                        })
                else:
                    tracks.append({
                        "name": info.get("title", ""),
                        "artist": info.get("uploader", ""),
                        "album": "",
                        "cover_url": info.get("thumbnail")
                    })
                return tracks
        except Exception as e:
            logging.error(f"YouTube track retrieval failed: {str(e)}")
            return None

    @staticmethod
    def update_mp3_metadata(file_path: str, metadata: dict) -> None:
        try:
            if not os.path.exists(file_path):
                logging.error(f"File not found for metadata update: {file_path}")
                return
            try:
                audio = EasyID3(file_path)
            except Exception:
                audio = MP3(file_path, ID3=ID3)
                audio.add_tags()
            audio["title"] = metadata.get("name", "Unknown Title")
            audio["artist"] = metadata.get("artist", "Unknown Artist")
            audio["album"] = metadata.get("album", "Unknown Album")
            audio.save()
            cover_url = metadata.get("cover_url")
            if cover_url:
                try:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    r = requests.get(cover_url, headers=headers, timeout=10, stream=True, verify=False)
                    r.raise_for_status()
                    id3 = ID3(file_path)
                    id3.delall("APIC")
                    id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=r.content))
                    id3.save(file_path, v2_version=3)
                    logging.info(f"Updated metadata with cover art for {file_path}")
                except Exception as e:
                    logging.error(f"Cover art download failed: {str(e)}")
        except Exception as e:
            logging.error(f"Failed to update metadata for {file_path}: {str(e)}")

def download_spotify_track(track: dict, output_path: str) -> None:
    global download_status
    if DownloadManager.stop_flag:
        raise Exception("Download stopped by user.")
    query = f"{track['artist']} {track['name']} official audio site:youtube.com -inurl:(lyrics topic)"
    track_id = f"{track['artist']}-{track['name']}"
    try:
        downloaded_file = DownloadManager.download_youtube(query, output_path, is_audio=True, metadata=track, max_retries=3)
        with download_status_lock:
            if track_id not in download_status["completed_tracks"]:
                download_status["completed_tracks"].append(track_id)
            completed = len(download_status["completed_tracks"])
            total = download_status["total_tracks"]
            download_status.update({
                "status": "running",
                "message": f"Downloaded {completed}/{total} tracks",
                "progress": (completed / total) * 100 if total > 0 else 0,
                "speed": download_status.get("speed", "N/A")
            })
    except Exception as e:
        logging.debug(f"Spotify track YouTube download failed: {str(e)}")
        try:
            downloaded_file = DownloadManager.download_soundcloud(f"https://soundcloud.com/search?q={track['artist']} {track['name']}", output_path, metadata=track)
            with download_status_lock:
                if track_id not in download_status["completed_tracks"]:
                    download_status["completed_tracks"].append(track_id)
                completed = len(download_status["completed_tracks"])
                total = download_status["total_tracks"]
                download_status.update({
                    "status": "running",
                    "message": f"Downloaded {completed}/{total} tracks",
                    "progress": (completed / total) * 100 if total > 0 else 0,
                    "speed": download_status.get("speed", "N/A")
                })
        except Exception as fb_e:
            logging.info(f"No suitable source found for {track['name']} by {track['artist']} after SoundCloud fallback.")
            try:
                broader_query = f"{track['artist']} {track['name']} audio"
                downloaded_file = DownloadManager.download_youtube(broader_query, output_path, is_audio=True, metadata=track)
                with download_status_lock:
                    if track_id not in download_status["completed_tracks"]:
                        download_status["completed_tracks"].append(track_id)
                    completed = len(download_status["completed_tracks"])
                    total = download_status["total_tracks"]
                    download_status.update({
                        "status": "running",
                        "message": f"Downloaded {completed}/{total} tracks",
                        "progress": (completed / total) * 100 if total > 0 else 0,
                        "speed": download_status.get("speed", "N/A")
                    })
            except Exception as final_e:
                logging.info(f"Skipping {track['name']} - all download attempts failed.")
                with download_status_lock:
                    completed = len(download_status["completed_tracks"])
                    total = download_status["total_tracks"]
                    download_status.update({
                        "status": "running",
                        "message": f"Downloaded {completed}/{total} tracks",
                        "progress": (completed / total) * 100 if total > 0 else 0
                    })

def download_soundcloud_track(track: dict, output_path: str) -> None:
    global download_status
    if DownloadManager.stop_flag:
        raise Exception("Download stopped by user.")
    track_id = f"{track['artist']}-{track['name']}"
    try:
        downloaded_file = DownloadManager.download_soundcloud(track["url"], output_path, metadata=track)
        with download_status_lock:
            if track_id not in download_status["completed_tracks"]:
                download_status["completed_tracks"].append(track_id)
            completed = len(download_status["completed_tracks"])
            total = download_status["total_tracks"]
            download_status.update({
                "status": "running",
                "message": f"Downloaded {completed}/{total} tracks",
                "progress": (completed / total) * 100 if total > 0 else 0,
                "speed": download_status.get("speed", "N/A")
            })
    except Exception as e:
        logging.error(f"SoundCloud track download failed: {str(e)}")
        try:
            logging.info(f"Falling back to YouTube for SoundCloud track: {track['name']}")
            query = f"{track['artist']} {track['name']} official audio -inurl:(lyrics topic)"
            downloaded_file = DownloadManager.download_youtube(query, output_path, is_audio=True, metadata=track)
            with download_status_lock:
                if track_id not in download_status["completed_tracks"]:
                    download_status["completed_tracks"].append(track_id)
                completed = len(download_status["completed_tracks"])
                total = download_status["total_tracks"]
                download_status.update({
                    "status": "running",
                    "message": f"Downloaded {completed}/{total} tracks",
                    "progress": (completed / total) * 100 if total > 0 else 0,
                    "speed": download_status.get("speed", "N/A")
                })
        except Exception as fb_e:
            logging.error(f"SoundCloud track fallback failed: {str(fb_e)}")
            with download_status_lock:
                completed = len(download_status["completed_tracks"])
                total = download_status["total_tracks"]
                download_status.update({
                    "status": "running",
                    "message": f"Downloaded {completed}/{total} tracks",
                    "progress": (completed / total) * 100 if total > 0 else 0
                })

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/download", methods=["POST"])
def download():
    global download_status
    service = request.form.get("service")
    url = request.form.get("url")
    output_path = request.form.get("output_path")

    if not service or not url:
        return jsonify({"status": "error", "message": "Service and URL are required"}), 400

    url = url.strip()
    output_path = output_path.strip() if output_path else os.getcwd()
    if os.path.splitext(output_path)[1]:
        output_path = os.path.dirname(output_path)
    output_path = os.path.abspath(output_path)
    if not os.path.exists(output_path):
        os.makedirs(output_path, exist_ok=True)

    quality = request.form.get("quality", "1080") if service == "youtube" else None
    is_audio = request.form.get("is_audio") == "true" if service == "youtube" else True

    DownloadManager.reset_stop()
    with download_status_lock:
        download_status = {
            "status": "running",
            "message": f"{service.capitalize()} download started",
            "progress": 0,
            "speed": "N/A",
            "completed_tracks": [],
            "total_tracks": 0
        }

    def run_download():
        global download_status
        try:
            if service == "youtube":
                if not DownloadManager.is_valid_youtube_url(url):
                    with download_status_lock:
                        download_status = {"status": "error", "message": "Invalid YouTube URL", "progress": 0, "completed_tracks": [], "total_tracks": 0}
                    return
                downloaded_file = DownloadManager.download_youtube(url, output_path, is_audio, quality)
                with download_status_lock:
                    tracks = DownloadManager.get_youtube_tracks(url)
                    if tracks:
                        track_id = f"{tracks[0]['artist']}-{tracks[0]['name']}"
                        if track_id not in download_status["completed_tracks"]:
                            download_status["completed_tracks"].append(track_id)
                        download_status.update({
                            "status": "idle",
                            "message": "YouTube download completed",
                            "progress": 100,
                            "total_tracks": 1
                        })
            elif service in ["spotify", "soundcloud"]:
                if service == "spotify":
                    if not url.startswith("https://open.spotify.com/"):
                        with download_status_lock:
                            download_status = {"status": "error", "message": "Invalid Spotify URL", "progress": 0, "completed_tracks": [], "total_tracks": 0}
                        return
                    sp_client = DownloadManager.initialize_spotify()
                    if not sp_client:
                        with download_status_lock:
                            download_status = {"status": "error", "message": "Spotify initialization failed", "progress": 0, "completed_tracks": [], "total_tracks": 0}
                        return
                    tracks = DownloadManager.get_spotify_tracks(sp_client, url)
                    download_func = download_spotify_track
                else:  # soundcloud
                    if not DownloadManager.is_valid_soundcloud_url(url):
                        with download_status_lock:
                            download_status = {"status": "error", "message": "Invalid SoundCloud URL", "progress": 0, "completed_tracks": [], "total_tracks": 0}
                        return
                    tracks = DownloadManager.get_soundcloud_tracks(url)
                    download_func = download_soundcloud_track

                if not tracks:
                    with download_status_lock:
                        download_status = {"status": "error", "message": "No tracks found", "progress": 0, "completed_tracks": [], "total_tracks": 0}
                    return

                total = len(tracks)
                with download_status_lock:
                    download_status["total_tracks"] = total

                track_queue = Queue()
                for track in tracks:
                    track_queue.put(track)

                MAX_WORKERS = 10  # Using your manual change to 10
                active_tasks = {}

                def process_track(executor):
                    while not track_queue.empty() and not DownloadManager.stop_flag:
                        try:
                            track = track_queue.get_nowait()
                            future = executor.submit(download_func, track, output_path)
                            active_tasks[future] = track
                        except Queue.Empty:
                            break

                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    process_track(executor)
                    while active_tasks and not DownloadManager.stop_flag:
                        for future in concurrent.futures.as_completed(list(active_tasks.keys())):
                            track = active_tasks.pop(future)
                            try:
                                future.result()
                            except Exception as e:
                                logging.error(f"Error downloading track {track['name']}: {str(e)}")
                            process_track(executor)

                    if DownloadManager.stop_flag:
                        with download_status_lock:
                            download_status.update({"status": "stopped", "message": "Download stopped"})
                        return

                    with download_status_lock:
                        download_status.update({
                            "status": "idle",
                            "message": f"{service.capitalize()} download completed",
                            "progress": 100,
                            "speed": "N/A"
                        })
            else:
                with download_status_lock:
                    download_status = {"status": "error", "message": "Unsupported service", "progress": 0, "completed_tracks": [], "total_tracks": 0}
        except Exception as e:
            logging.error(f"{service.capitalize()} download failed: {str(e)}")
            with download_status_lock:
                download_status.update({"status": "error", "message": str(e)})

    threading.Thread(target=run_download, daemon=True).start()
    return jsonify({"status": "started", "message": f"{service.capitalize()} download started", "progress": 0})

@app.route("/stop", methods=["POST"])
def stop_download():
    DownloadManager.set_stop()
    global download_status
    with download_status_lock:
        download_status.update({"status": "stopped", "message": "Download stopped"})
    return jsonify(download_status)

@app.route("/preview", methods=["POST"])
def preview():
    service = request.form.get("service")
    url = request.form.get("url")
    start_index = int(request.form.get("start_index", 0))
    batch_size = int(request.form.get("batch_size", 10))  # Load 10 tracks at a time
    
    if not service or not url:
        return jsonify({"status": "error", "message": "Service and URL are required"}), 400
    url = url.strip()

    try:
        if service == "youtube":
            if not DownloadManager.is_valid_youtube_url(url):
                return jsonify({"status": "error", "message": "Invalid YouTube URL"}), 400
            tracks = DownloadManager.get_youtube_tracks(url)
            if not tracks:
                return jsonify({"status": "error", "message": "No tracks found"}), 404
            return jsonify({
                "status": "success", 
                "tracks": tracks,
                "total_tracks": len(tracks),
                "has_more": False
            })
            
        elif service == "spotify":
            if not url.startswith("https://open.spotify.com/"):
                return jsonify({"status": "error", "message": "Invalid Spotify URL"}), 400
            sp_client = DownloadManager.initialize_spotify()
            if not sp_client:
                return jsonify({"status": "error", "message": "Failed to initialize Spotify client"}), 500
            
            all_tracks = DownloadManager.get_spotify_tracks(sp_client, url)
            if not all_tracks:
                return jsonify({"status": "error", "message": "No tracks found"}), 404
            
            total_tracks = len(all_tracks)
            batch_tracks = all_tracks[start_index:start_index + batch_size]
            
            track_list = [{
                "name": t.get("name", "Unknown Track"),
                "artist": t.get("artist", "Unknown Artist"),
                "album": t.get("album", ""),
                "cover_url": t.get("cover_url", "")
            } for t in batch_tracks]
            
            return jsonify({
                "status": "success", 
                "tracks": track_list,
                "total_tracks": total_tracks,
                "has_more": (start_index + batch_size) < total_tracks
            })
            
        elif service == "soundcloud":
            if not DownloadManager.is_valid_soundcloud_url(url):
                return jsonify({"status": "error", "message": "Invalid SoundCloud URL"}), 400
            
            all_tracks = DownloadManager.get_soundcloud_tracks(url)
            if not all_tracks:
                return jsonify({"status": "error", "message": "No tracks found"}), 404
            
            total_tracks = len(all_tracks)
            batch_tracks = all_tracks[start_index:start_index + batch_size]
            
            track_list = [{
                "name": t.get("name", "Unknown Track"),
                "artist": t.get("artist", "Unknown Artist"),
                "album": t.get("album", ""),
                "cover_url": t.get("cover_url", "")
            } for t in batch_tracks]
            
            return jsonify({
                "status": "success", 
                "tracks": track_list,
                "total_tracks": total_tracks,
                "has_more": (start_index + batch_size) < total_tracks
            })
        
        else:
            return jsonify({"status": "error", "message": "Unsupported service"}), 400
            
    except Exception as e:
        logging.error(f"Preview failed: {str(e)}")
        return jsonify({"status": "error", "message": f"Preview failed: {str(e)}"}), 500

@app.route("/status", methods=["GET"])
def status():
    global download_status
    with download_status_lock:
        return jsonify(download_status)

def launch_app():
    port = 5000
    url = f"http://localhost:{port}"
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        logging.info(f"Opening browser at {url}")
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    app.run(debug=True, host="0.0.0.0", port=port)

if __name__ == "__main__":
    launch_app()










