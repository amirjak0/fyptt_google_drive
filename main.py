import os
import re
import glob
import mimetypes
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from curl_cffi import requests
import yt_dlp

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- Google Drive Helpers ---

def get_gdrive_service():
    """Initializes and returns the Google Drive API service using OAuth2 refresh token secrets."""
    client_id = os.environ.get("GDRIVE_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET")
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")
    
    if not (client_id and client_secret and refresh_token):
        print("[-] Google Drive credentials missing from environment.")
        return None
        
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret
    )
    
    try:
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception as e:
        print(f"[-] Failed to build Google Drive service: {e}")
        return None

def upload_to_gdrive(file_path, folder_id=None):
    """Uploads a local file to Google Drive under an optional folder."""
    service = get_gdrive_service()
    if not service:
        print("[-] Skipping Google Drive upload due to initialization failure.")
        return False
        
    file_name = os.path.basename(file_path)
    file_metadata = {'name': file_name}
    if folder_id:
        file_metadata['parents'] = [folder_id]
        
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = 'video/mp4'
        
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    
    try:
        print(f"[+] Uploading '{file_name}' to Google Drive...")
        uploaded_file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        print(f"[+] Successfully uploaded to Google Drive. File ID: {uploaded_file.get('id')}")
        return True
    except Exception as e:
        print(f"[-] Failed to upload to Google Drive: {e}")
        return False

# --- Video Extraction & Download ---

def extract_video_url(html_content, page_url):
    """
    Parses a single post HTML to find direct video source URLs (.mp4, .m3u8, etc.)
    or iframe-embedded players.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # 1. Search in HTML5 tags
    for tag in soup.find_all(['source', 'video']):
        src = tag.get('src') or tag.get('data-src')
        if src:
            resolved = urljoin(page_url, src)
            if any(ext in resolved.lower() for ext in ['.mp4', '.m3u8', '.ts']):
                return resolved
                
    # 2. Search in iframe elements (often embedded players)
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src') or iframe.get('data-src')
        if src:
            resolved = urljoin(page_url, src)
            if any(k in resolved.lower() for k in ['player', 'embed', 'video', 'stream', 'jwplayer']):
                return resolved

    # 3. Safe regex search in scripts or unstructured text (e.g., config objects)
    video_regexes = [
        r'["\']?file["\']?\s*:\s*["\'](https?://[^"\']+\.(?:mp4|m3u8|ts)[^"\']*)["\']',
        r'["\']?url["\']?\s*:\s*["\'](https?://[^"\']+\.(?:mp4|m3u8|ts)[^"\']*)["\']',
        r'["\']?src["\']?\s*:\s*["\'](https?://[^"\']+\.(?:mp4|m3u8|ts)[^"\']*)["\']',
        r'["\'](https?://[^"\']+\.(?:mp4|m3u8|ts)[^"\']*)["\']'
    ]
    
    for regex in video_regexes:
        matches = re.findall(regex, html_content)
        for match in matches:
            # Prevent catching irrelevant assets like tracking scripts or CDN components
            if not any(k in match.lower() for k in ['analytics', 'google', 'tracker', 'facebook', 'webpack']):
                return match
                
    return None

def download_video(post_url, out_dir="downloads"):
    """
    Downloads the video. It first attempts native yt-dlp resolution on the post.
    If that fails, it fetches the page with curl_cffi, extracts the direct source URL, 
    and passes that to yt-dlp.
    """
    slug = post_url.rstrip('/').split('/')[-1]
    ydl_opts = {
        'outtmpl': f'{out_dir}/{slug}.%(ext)s',
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
    }
    
    # Method A: Try direct page parsing with yt-dlp
    try:
        print(f"[~] Attempting native yt-dlp download on: {post_url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([post_url])
            
        # Search for any file corresponding to our slug prefix to handle merged extension changes
        downloaded_files = glob.glob(os.path.join(out_dir, f"{slug}.*"))
        if downloaded_files:
            return downloaded_files[0]
    except Exception as e:
        print(f"[!] Direct yt-dlp download did not succeed: {e}. Trying page extraction...")
        
    # Method B: Fetch page HTML and extract the direct video link
    try:
        resp = requests.get(post_url, impersonate="chrome")
        if resp.status_code == 200:
            direct_url = extract_video_url(resp.text, post_url)
            if direct_url:
                print(f"[+] Found direct source URL: {direct_url}")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([direct_url])
                downloaded_files = glob.glob(os.path.join(out_dir, f"{slug}.*"))
                if downloaded_files:
                    return downloaded_files[0]
            else:
                print("[-] Unable to extract direct video source from page HTML.")
        else:
            print(f"[-] Failed to fetch post page: HTTP {resp.status_code}")
    except Exception as ex:
        print(f"[-] Custom extraction download failed: {ex}")
        
    return None

# --- Page Index Parsing ---

def parse_listing_page(page_number, base_url):
    """Fetches a listing page and extracts all matching video post URLs."""
    url = base_url if page_number == 1 else f"{base_url}/page/{page_number}/"
    print(f"[~] Scanning page {page_number}: {url}")
    
    try:
        resp = requests.get(url, impersonate="chrome")
        if resp.status_code == 404:
            print(f"[*] Page {page_number} returned 404. End of site archives reached.")
            return None
        if resp.status_code != 200:
            print(f"[-] Failed to scan page {page_number}: HTTP {resp.status_code}")
            return []
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        discovered_urls = []
        seen = set()
        
        # Clean target base for regex checks
        norm_base_url = base_url.replace("http://", "https://").rstrip("/")
        escaped_base = re.escape(norm_base_url)
        # Matches format: https://domain/digits/slug/
        post_pattern = rf'^{escaped_base}/\d+/[^/\s"\']+$'
        
        # Look for structured anchor tags
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            full_url = urljoin(base_url, href)
            norm_url = full_url.replace("http://", "https://").rstrip("/")
            if re.match(post_pattern, norm_url):
                if norm_url not in seen:
                    seen.add(norm_url)
                    discovered_urls.append(norm_url)
                    
        # Regex fallback for embedded script blocks
        if not discovered_urls:
            raw_pattern = rf'{escaped_base}/\d+/[^/\s"\']+'
            matches = re.findall(raw_pattern, resp.text)
            for match in matches:
                norm_url = match.strip().replace("http://", "https://").rstrip("/")
                if norm_url not in seen:
                    seen.add(norm_url)
                    discovered_urls.append(norm_url)
                    
        return discovered_urls
    except Exception as e:
        print(f"[-] Error while parsing page {page_number}: {e}")
        return []

# --- Main Flow ---

def main():
    print("==================================================")
    print("      Web Video Downloader to Google Drive")
    print("==================================================")
    
    target_site_url = os.environ.get("TARGET_SITE_URL", "https://fyptt.to").rstrip('/')
    newest_pages_to_scan = int(os.environ.get("NEWEST_PAGES_TO_SCAN", "2"))
    pages_per_run = int(os.environ.get("PAGES_PER_RUN", "2"))
    max_downloads = int(os.environ.get("MAX_DOWNLOADS_PER_RUN", "100"))
    reverse_order = os.environ.get("REVERSE_VIDEO_ORDER", "False").lower() in ("true", "1", "yes")
    gdrive_folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    
    history_file = "download_history.txt"
    current_page_file = "current_page.txt"
    
    # 1. Load download history
    downloaded_urls = set()
    if os.path.exists(history_file):
        with open(history_file, "r") as f:
            for line in f:
                u = line.strip().replace("http://", "https://").rstrip("/")
                if u:
                    downloaded_urls.add(u)
    print(f"[+] Loaded {len(downloaded_urls)} historical entries from download history.")
    
    # 2. Load current archive page pointer
    start_archive_page = 5
    if os.path.exists(current_page_file):
        with open(current_page_file, "r") as f:
            try:
                start_archive_page = int(f.read().strip())
            except ValueError:
                pass
    print(f"[+] Archive indexing starting at page: {start_archive_page}")
    
    # 3. Identify list of pages to scan
    pages_to_scan = list(range(1, newest_pages_to_scan + 1))
    archive_pages = list(range(start_archive_page, start_archive_page + pages_per_run))
    for ap in archive_pages:
        if ap not in pages_to_scan:
            pages_to_scan.append(ap)
            
    print(f"[+] Total pages queued for scanning this run: {pages_to_scan}")
    
    # 4. Scan pages and extract unarchived video posts
    video_queue = []
    seen_in_scan = set()
    archive_ended = False
    
    for page in pages_to_scan:
        urls = parse_listing_page(page, target_site_url)
        
        # Check if an archive page hit a 404 (indicating the absolute end of the site structure)
        if urls is None:
            if page in archive_pages:
                archive_ended = True
            continue
            
        if reverse_order:
            urls.reverse()
            
        for u in urls:
            if u not in downloaded_urls and u not in seen_in_scan:
                seen_in_scan.add(u)
                video_queue.append(u)
                
    print(f"[+] Found {len(video_queue)} new video(s) available for processing.")
    
    # 5. Process and download queue
    os.makedirs("downloads", exist_ok=True)
    downloaded_count = 0
    
    for video_url in video_queue:
        if downloaded_count >= max_downloads:
            print("[*] Processing limit reached (MAX_DOWNLOADS_PER_RUN). Stopping.")
            break
            
        print(f"\n[~] Processing ({downloaded_count + 1}/{len(video_queue)}): {video_url}")
        local_path = download_video(video_url, "downloads")
        
        if local_path and os.path.exists(local_path):
            # Upload to GDrive
            upload_success = upload_to_gdrive(local_path, gdrive_folder_id)
            if upload_success:
                # Add to history
                with open(history_file, "a") as hf:
                    hf.write(video_url + "\n")
                downloaded_urls.add(video_url)
                downloaded_count += 1
                
            # Free up workspace storage instantly
            try:
                os.remove(local_path)
                print(f"[+] Cleaned up local file: {local_path}")
            except Exception as e:
                print(f"[-] Could not delete local file: {e}")
        else:
            print(f"[-] Skip/Failed downloading: {video_url}")
            
    # 6. Update the archive page pointer
    next_archive_page = start_archive_page + pages_per_run
    if archive_ended:
        # Loop around back to the page following the newest pages range if the archive ended
        next_archive_page = newest_pages_to_scan + 1
        print(f"[*] Site end reached. Resetting starting page index in next run to: {next_archive_page}")
        
    with open(current_page_file, "w") as f:
        f.write(str(next_archive_page))
    print(f"[+] Index progress updated in {current_page_file} (Page pointer set to: {next_archive_page}).")
    print("[+] Done.")

if __name__ == "__main__":
    main()
