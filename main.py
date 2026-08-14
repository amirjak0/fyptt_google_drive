import os
import time
import yt_dlp
from bs4 import BeautifulSoup
from curl_cffi import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==========================================
# تنظیمات و متغیرهای محیطی
# ==========================================
TARGET_URLS_RAW = os.environ.get('TARGET_SITE_URL', '')
GDRIVE_CLIENT_ID = os.environ.get('GDRIVE_CLIENT_ID')
GDRIVE_CLIENT_SECRET = os.environ.get('GDRIVE_CLIENT_SECRET')
GDRIVE_REFRESH_TOKEN = os.environ.get('GDRIVE_REFRESH_TOKEN')
GDRIVE_FOLDER_ID = os.environ.get('GDRIVE_FOLDER_ID')
HISTORY_FILE = 'download_history.txt'

# تبدیل رشته لینک‌ها به یک لیست (جدا شده با کاما یا خط جدید)
TARGET_URLS = [url.strip() for url in TARGET_URLS_RAW.replace('\n', ',').split(',') if url.strip()]

# ==========================================
# توابع مدیریت تاریخچه (برای جلوگیری از دانلود تکراری)
# ==========================================
def load_history():
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())

def save_to_history(url):
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{url}\n")

# ==========================================
# توابع گوگل درایو
# ==========================================
def get_gdrive_service():
    creds = Credentials(
        None,
        refresh_token=GDRIVE_REFRESH_TOKEN,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=GDRIVE_CLIENT_ID,
        client_secret=GDRIVE_CLIENT_SECRET
    )
    return build('drive', 'v3', credentials=creds)

def upload_to_gdrive(service, file_path, folder_id):
    file_name = os.path.basename(file_path)
    file_metadata = {
        'name': file_name,
        'parents': [folder_id]
    }
    media = MediaFileUpload(file_path, resumable=True)
    print(f"Uploading {file_name} to Google Drive...")
    
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    print(f"Upload successful! File ID: {file.get('id')}")
    return file.get('id')

# ==========================================
# توابع استخراج و دانلود ویدیو
# ==========================================
def extract_direct_video_url(page_url):
    """
    اگر yt-dlp نتوانست ویدیو را پیدا کند، این تابع با دور زدن کلودفلر
    سورس صفحه را می‌خواند و لینک مستقیم ویدیو را پیدا می‌کند.
    """
    print(f"Attempting manual extraction for: {page_url}")
    try:
        # استفاده از curl_cffi برای دور زدن Cloudflare
        session = requests.Session(impersonate="chrome110")
        response = session.get(page_url, timeout=30)
        soup = BeautifulSoup(response.text, 'html.parser')

        # جستجو برای تگ‌های ویدیو در هر دو سایت
        video_tags = soup.find_all(['video', 'source'])
        for tag in video_tags:
            src = tag.get('src')
            if src and ('.mp4' in src or '.m3u8' in src or 'http' in src):
                print(f"Direct video link found: {src}")
                # اگر لینک نسبی بود، آن را کامل می‌کنیم
                if src.startswith('/'):
                    from urllib.parse import urlparse
                    parsed_uri = urlparse(page_url)
                    base = '{uri.scheme}://{uri.netloc}'.format(uri=parsed_uri)
                    src = base + src
                return src
                
        print("No direct video tag found in HTML.")
        return page_url
    except Exception as e:
        print(f"Manual extraction failed: {e}")
        return page_url

def download_video(url):
    """
    دانلود ویدیو با بالاترین کیفیت ممکن با استفاده از yt-dlp
    """
    # تنظیمات yt-dlp برای بالاترین کیفیت و دور زدن تحریم/کلودفلر
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best', # بالاترین کیفیت
        'outtmpl': 'downloads/%(title)s_%(id)s.%(ext)s', # مسیر و نام فایل
        'impersonate': 'chrome', # دور زدن کلودفلر
        'merge_output_format': 'mp4',
        'quiet': False,
        'no_warnings': True,
        'restrictfilenames': True,
    }

    # ایجاد پوشه دانلود اگر وجود نداشت
    if not os.path.exists('downloads'):
        os.makedirs('downloads')

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"\nStarting download process for: {url}")
            # ابتدا سعی می‌کنیم مستقیم با yt-dlp دانلود کنیم
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # اگر فرمت تغییر کرده بود (مثلا mkv شده بود) نام فایل را اصلاح می‌کنیم
            if not os.path.exists(filename):
                base, _ = os.path.splitext(filename)
                for ext in ['.mp4', '.mkv', '.webm']:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break
                        
            return filename
            
    except Exception as e:
        print(f"yt-dlp direct download failed: {e}")
        print("Trying manual extraction fallback...")
        
        # اگر yt-dlp شکست خورد، لینک مستقیم را استخراج می‌کنیم
        direct_url = extract_direct_video_url(url)
        if direct_url != url:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(direct_url, download=True)
                    filename = ydl.prepare_filename(info)
                    return filename
            except Exception as e2:
                print(f"Fallback download failed: {e2}")
                return None
        return None

# ==========================================
# بدنه اصلی برنامه
# ==========================================
def main():
    if not TARGET_URLS:
        print("Error: TARGET_SITE_URL is empty. Please add URLs in GitHub Secrets.")
        return

    history = load_history()
    gdrive_service = get_gdrive_service()

    for url in TARGET_URLS:
        if url in history:
            print(f"Skipping already downloaded URL: {url}")
            continue

        print(f"\n--- Processing: {url} ---")
        downloaded_file = download_video(url)

        if downloaded_file and os.path.exists(downloaded_file):
            try:
                # آپلود در گوگل درایو
                upload_to_gdrive(gdrive_service, downloaded_file, GDRIVE_FOLDER_ID)
                
                # ثبت در تاریخچه
                save_to_history(url)
                
                # حذف فایل از سرور گیت‌هاب برای خالی شدن فضا
                os.remove(downloaded_file)
                print(f"Deleted local file: {downloaded_file}")
                
            except Exception as e:
                print(f"Failed to upload {downloaded_file} to Google Drive: {e}")
        else:
            print(f"Failed to download video from: {url}")

if __name__ == "__main__":
    main()
