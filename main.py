import os
import logging
import requests
from bs4 import BeautifulSoup
import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import urllib.parse

# تنظیمات لاگ مشابه خروجی گیت‌هاب شما
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# دریافت متغیرهای محیطی
# با استفاده از split پشتیبانی از چند سایت که با کاما جدا شده‌اند اضافه شد
TARGET_SITE_URLS = os.getenv('TARGET_SITE_URL', '').split(',')
GDRIVE_CLIENT_ID = os.getenv('GDRIVE_CLIENT_ID')
GDRIVE_CLIENT_SECRET = os.getenv('GDRIVE_CLIENT_SECRET')
GDRIVE_REFRESH_TOKEN = os.getenv('GDRIVE_REFRESH_TOKEN')
GDRIVE_FOLDER_ID = os.getenv('GDRIVE_FOLDER_ID')
HISTORY_FILE = 'download_history.txt'

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_history(video_id):
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{video_id}\n")

def get_drive_service():
    creds = Credentials(
        None,
        refresh_token=GDRIVE_REFRESH_TOKEN,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=GDRIVE_CLIENT_ID,
        client_secret=GDRIVE_CLIENT_SECRET
    )
    return build('drive', 'v3', credentials=creds)

def extract_links_from_page(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        links = set()
        
        for a in soup.find_all('a', href=True):
            href = a['href']
            full_url = urllib.parse.urljoin(url, href)
            
            # فیلتر کردن لینک‌های نامربوط (دسته‌بندی‌ها، صفحات، تگ‌ها و ...)
            if any(x in full_url for x in ['/category/', '/tag/', '/page/', '?', '/about', '/contact']):
                continue
                
            # بررسی اینکه لینک متعلق به همان دامنه باشد و ساختار یک پست/ویدیو را داشته باشد
            if urllib.parse.urlparse(full_url).netloc == urllib.parse.urlparse(url).netloc:
                path_parts = [p for p in full_url.split('/') if p]
                if len(path_parts) >= 2: # معمولا لینک ویدیوها طولانی‌تر از صفحه اصلی هستند
                    links.add(full_url)
        return links
    except Exception as e:
        logging.error(f"خطا در استخراج لینک از {url}: {e}")
        return set()

def process_site(base_url, history, drive_service):
    logging.info(f"در حال بررسی سایت: {base_url}")
    all_links = set()
    
    # بررسی 10 صفحه اول سایت
    for page in range(1, 11):
        # تنظیم ساختار URL صفحات بر اساس نوع سایت
        if "fyptt" in base_url:
            page_url = f"{base_url.rstrip('/')}/page/{page}/?0"
        else:
            page_url = f"{base_url.rstrip('/')}/page/{page}/"
            
        logging.info(f"در حال بررسی صفحه {page}: {page_url}")
        links = extract_links_from_page(page_url)
        all_links.update(links)
        logging.info(f"{len(links)} لینک معتبر در صفحه {page} پیدا شد.")

    logging.info(f"تعداد کل {len(all_links)} لینک برای پردازش از این سایت آماده است.")

    os.makedirs('downloads', exist_ok=True)

    for link in all_links:
        # استفاده از مسیر URL به عنوان شناسه یکتا برای جلوگیری از دانلود تکراری
        video_id = urllib.parse.urlparse(link).path.strip('/')
        if not video_id or video_id in history:
            continue
            
        logging.info(f"شروع پردازش ویدیوی جدید: {link}")
        
        # تنظیمات yt-dlp برای بالاترین کیفیت و دور زدن محدودیت‌ها
        ydl_opts = {
            'format': 'bestvideo+bestaudio/best', # دانلود بالاترین کیفیت ممکن
            'outtmpl': 'downloads/%(title)s [%(id)s].%(ext)s',
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            'restrictfilenames': True,
            'impersonate': 'chrome' # استفاده از curl_cffi برای دور زدن کلودفلر و آنتی‌بات‌ها
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(link, download=True)
                if not info:
                    logging.warning(f"امکان استخراج فایل ویدیو از آدرس {link} وجود نداشت. ویدیو رد شد.")
                    continue
                    
                filename = ydl.prepare_filename(info)
                
                # بررسی تغییر پسوند فایل توسط yt-dlp (مثلا ادغام صدا و تصویر در mkv)
                if not os.path.exists(filename):
                    base, _ = os.path.splitext(filename)
                    for ext in ['.mp4', '.mkv', '.webm']:
                        if os.path.exists(base + ext):
                            filename = base + ext
                            break
                            
                if os.path.exists(filename):
                    logging.info(f"در حال آپلود: {os.path.basename(filename)}")
                    file_metadata = {'name': os.path.basename(filename), 'parents': [GDRIVE_FOLDER_ID]}
                    media = MediaFileUpload(filename, resumable=True)
                    uploaded_file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                    logging.info(f"آپلود موفقیت‌آمیز بود. شناسه فایل: {uploaded_file.get('id')}")
                    
                    os.remove(filename)
                    logging.info("فایل محلی از روی سرور حذف شد.")
                    
                    save_history(video_id)
                    history.add(video_id)
        except Exception as e:
            logging.error(f"خطا در پردازش {link}: {e}")

def main():
    history = load_history()
    logging.info(f"تعداد {len(history)} ویدیو از تاریخچه محلی (گیت‌هاب) بارگذاری شد.")
    
    drive_service = get_drive_service()
    
    for site_url in TARGET_SITE_URLS:
        site_url = site_url.strip()
        if site_url:
            process_site(site_url, history, drive_service)

if __name__ == "__main__":
    main()
