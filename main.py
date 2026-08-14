import os
import logging
import urllib.parse
from bs4 import BeautifulSoup
from curl_cffi import requests  # استفاده از curl_cffi برای دور زدن کامل کلودفلر
import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# تنظیمات لاگ
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# دریافت متغیرهای محیطی
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
    """استخراج لینک پست‌ها از صفحات اصلی با شبیه‌سازی مرورگر واقعی"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        # شبیه‌سازی کامل مرورگر کروم جهت عبور از سیستم‌های ضد ربات
        response = requests.get(url, headers=headers, impersonate="chrome", timeout=20)
        if response.status_code != 200:
            logging.error(f"خطا در بارگذاری صفحه {url} با کد وضعیت: {response.status_code}")
            return set()
            
        soup = BeautifulSoup(response.text, 'html.parser')
        links = set()
        
        for a in soup.find_all('a', href=True):
            href = a['href']
            full_url = urllib.parse.urljoin(url, href)
            
            # فیلتر کردن بخش‌های غیر ویدیو
            if any(x in full_url for x in ['/category/', '/tag/', '/page/', '?', '/about', '/contact', '/feed/', '/search/']):
                continue
                
            # اطمینان از اینکه لینک داخلی است و مربوط به صفحه اصلی نیست
            if urllib.parse.urlparse(full_url).netloc == urllib.parse.urlparse(url).netloc:
                path = urllib.parse.urlparse(full_url).path.strip('/')
                path_parts = [p for p in path.split('/') if p]
                if len(path_parts) >= 1:
                    if full_url.rstrip('/') != url.rstrip('/'):
                        links.add(full_url)
                        
        return links
    except Exception as e:
        logging.error(f"خطا در استخراج لینک از {url}: {e}")
        return set()

def extract_video_sources_from_post(post_url):
    """کاویدن کدهای خام صفحه پست جهت پیدا کردن پلیر اصلی و عبور از تبلیغات کلیکی"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(post_url, headers=headers, impersonate="chrome", timeout=20)
        if response.status_code != 200:
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        video_urls = []
        
        # ۱. پیدا کردن تمام آی‌فریم‌های پلیر پنهان (تگ Iframe)
        for iframe in soup.find_all('iframe', src=True):
            src = iframe['src']
            full_src = urllib.parse.urljoin(post_url, src)
            # رد کردن آی‌فریم‌های تبلیغاتی معروف یا شبکه‌های اجتماعی
            if any(x in full_src for x in ['doubleclick', 'google', 'facebook', 'twitter', 'disqus', 'ads', 'exoclick']):
                continue
            video_urls.append(full_src)
            
        # ۲. پیدا کردن تگ‌های مستقیم ویدیو در صورت وجود
        for video in soup.find_all('video'):
            if video.get('src'):
                video_urls.append(urllib.parse.urljoin(post_url, video['src']))
            for source in video.find_all('source', src=True):
                video_urls.append(urllib.parse.urljoin(post_url, source['src']))
                
        # ۳. پیدا کردن لینک‌های ارجاع به سایت‌های میزبان ویدیو معروف
        for a in soup.find_all('a', href=True):
            href = a['href']
            full_href = urllib.parse.urljoin(post_url, href)
            if any(ext in full_href.lower() for ext in ['.mp4', '.m3u8', '.webm']):
                video_urls.append(full_href)
            elif any(tube in full_href.lower() for tube in ['pornhub.com', 'spankbang.com', 'xhamster.com', 'eporner.com', 'redtube.com', 'youporn.com', 'xvvideos.com']):
                video_urls.append(full_href)
                
        return list(set(video_urls))
    except Exception as e:
        logging.error(f"خطا در استخراج منابع ویدیو از پست {post_url}: {e}")
        return []

def process_site(base_url, history, drive_service):
    logging.info(f"در حال بررسی سایت: {base_url}")
    all_links = set()
    
    # اسکن صفحات اول برای یافتن ویدیوهای جدید
    for page in range(1, 11):
        if "fyptt" in base_url:
            page_url = f"{base_url.rstrip('/')}/page/{page}/?0"
        else:
            page_url = f"{base_url.rstrip('/')}/page/{page}/"
            
        logging.info(f"در حال بررسی صفحه {page}: {page_url}")
        links = extract_links_from_page(page_url)
        all_links.update(links)
        logging.info(f"{len(links)} لینک معتبر در صفحه {page} پیدا شد.")

    logging.info(f"تعداد کل {len(all_links)} لینک برای پردازش آماده است.")
    os.makedirs('downloads', exist_ok=True)

    for link in all_links:
        # استفاده از پسوند آدرس به عنوان شناسه تاریخچه برای جلوگیری از تکرار
        video_id = urllib.parse.urlparse(link).path.strip('/')
        if not video_id or video_id in history:
            continue
            
        logging.info(f"شروع پردازش ویدیوی جدید: {link}")
        
        # استخراج منابع اصلی ویدیو از پست
        video_sources = extract_video_sources_from_post(link)
        if not video_sources:
            # اگر هیچ منبع خاصی یافت نشد، خود لینک پست را به عنوان بکاپ به yt-dlp می‌دهیم
            video_sources = [link]
            
        for source in video_sources:
            logging.info(f"در حال دانلود از منبع استخراج‌شده: {source}")
            
            # تنظیمات دانلود برای دریافت بالاترین کیفیت تصویر و صدا
            ydl_opts = {
                'format': 'bestvideo+bestaudio/best', # تضمین دانلود بالاترین کیفیت موجود
                'outtmpl': 'downloads/%(title)s [%(id)s].%(ext)s',
                'ignoreerrors': True,
                'quiet': False,
                'no_warnings': True,
                'restrictfilenames': True,
                'impersonate': 'chrome' # دور زدن کلودفلر پلتفرم‌های میزبان
            }
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(source, download=True)
                    if not info:
                        continue
                        
                    filename = ydl.prepare_filename(info)
                    
                    # مدیریت تغییر احتمالی فرمت خروجی (مانند mkv یا webm)
                    if not os.path.exists(filename):
                        base, _ = os.path.splitext(filename)
                        for ext in ['.mp4', '.mkv', '.webm']:
                            if os.path.exists(base + ext):
                                filename = base + ext
                                break
                                
                    if os.path.exists(filename):
                        logging.info(f"در حال آپلود به گوگل درایو: {os.path.basename(filename)}")
                        file_metadata = {'name': os.path.basename(filename), 'parents': [GDRIVE_FOLDER_ID]}
                        media = MediaFileUpload(filename, resumable=True)
                        uploaded_file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                        logging.info(f"آپلود موفقیت‌آمیز بود. شناسه فایل: {uploaded_file.get('id')}")
                        
                        os.remove(filename)
                        logging.info("فایل موقت محلی حذف شد.")
                        
                        save_history(video_id)
                        history.add(video_id)
                        break # با موفقیت دانلود شد، به سراغ لینک بعدی می‌رویم
            except Exception as e:
                logging.error(f"خطا در دانلود یا آپلود منبع {source}: {e}")

def main():
    history = load_history()
    logging.info(f"تعداد {len(history)} ویدیو از تاریخچه دانلودهای قبلی بارگذاری شد.")
    
    drive_service = get_drive_service()
    
    for site_url in TARGET_SITE_URLS:
        site_url = site_url.strip()
        if site_url:
            process_site(site_url, history, drive_service)

if __name__ == "__main__":
    main()
