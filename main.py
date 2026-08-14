import os
import re
import sys
import logging
from bs4 import BeautifulSoup
import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import urllib.parse

# تنظیمات لاگ مشابه خروجی گیت‌هاب شما
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
    try:
        # استفاده از curl_cffi برای عبور از کلودفلر و دریافت HTML واقعی صفحه
        from curl_cffi import requests as curl_requests
        response = curl_requests.get(url, impersonate="chrome", timeout=20)
        
        if response.status_code != 200:
            logging.error(f"خطا در دریافت صفحه {url} با کد وضعیت: {response.status_code}")
            return set()
            
        soup = BeautifulSoup(response.text, 'html.parser')
        links = set()
        
        domain = urllib.parse.urlparse(url).netloc
        
        # ۱. منطق استخراج لینک برای سایت fyptt
        if "fyptt" in domain:
            for a in soup.find_all('a', href=True):
                href = a['href']
                full_url = urllib.parse.urljoin(url, href)
                path_parts = [p for p in urllib.parse.urlparse(full_url).path.split('/') if p]
                # در fyptt لینک‌های ویدیو با ساختار عددی شروع می‌شوند مانند /23614/title
                if len(path_parts) >= 2 and path_parts[0].isdigit():
                    links.add(full_url)
                    
        # ۲. منطق استخراج لینک برای سایت namethatpornad (وردپرسی)
        else:
            # استخراج پست‌ها از تگ‌های <article> برای نادیده گرفتن سایدبارها و تبلیغات
            articles = soup.find_all('article')
            for article in articles:
                # روش اول: استخراج از تگ h2 عنوان پست
                h2 = article.find('h2')
                if h2:
                    a = h2.find('a', href=True)
                    if a:
                        full_url = urllib.parse.urljoin(url, a['href'])
                        links.add(full_url)
                
                # روش دوم: استخراج از تصویر شاخص
                thumbnail = article.find('a', class_='featured-thumbnail')
                if thumbnail and 'href' in thumbnail.attrs:
                    full_url = urllib.parse.urljoin(url, thumbnail['href'])
                    # حذف صفحات نامربوط
                    if not any(x in full_url for x in ['/category/', '/tag/', '/page/', 'wp-', '?']):
                        links.add(full_url)
            
            # در صورتی که ساختار تغییر کرده باشد (به عنوان زاپاس)
            if not links:
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    full_url = urllib.parse.urljoin(url, href)
                    if urllib.parse.urlparse(full_url).netloc == domain:
                        if not any(x in full_url for x in ['/category/', '/tag/', '/page/', 'wp-', '?', '#']):
                            path_parts = [p for p in urllib.parse.urlparse(full_url).path.split('/') if p]
                            if len(path_parts) >= 1:
                                links.add(full_url)
                                
        return links
    except Exception as e:
        logging.error(f"خطا در استخراج لینک از {url}: {e}")
        return set()

def get_direct_video_url_and_id(link):
    # این تابع آدرس ویدیو را بررسی کرده و در صورت نیاز لینک آی‌فریم پلیر را برای دانلود تمیز می‌کند
    try:
        from curl_cffi import requests as curl_requests
        video_id = urllib.parse.urlparse(link).path.strip('/')
        
        if "fyptt" in link:
            res = curl_requests.get(link, impersonate="chrome", timeout=15)
            soup = BeautifulSoup(res.text, 'html.parser')
            iframe = soup.find('iframe', src=True)
            if iframe and "fypttstr.php" in iframe['src']:
                iframe_url = urllib.parse.urljoin(link, iframe['src'])
                logging.info(f"یافتن فریم ویدیو پلیر: {iframe_url}")
                
                # دریافت صفحه فریم مستقیم ویدیو
                res_iframe = curl_requests.get(iframe_url, impersonate="chrome", timeout=15)
                # جستجوی آدرس مستقیم فایل mp4 در سورس فریم
                mp4_match = re.search(r'https?://[^\s"\']+\.mp4\?[^\s"\']+', res_iframe.text)
                if mp4_match:
                    direct_url = mp4_match.group(0)
                    logging.info(f"آدرس مستقیم فایل ویدیو با موفقیت استخراج شد: {direct_url}")
                    return direct_url, video_id
                return iframe_url, video_id
                
        return link, video_id
    except Exception as e:
        logging.error(f"خطا در استخراج پلیر: {e}")
        return link, urllib.parse.urlparse(link).path.strip('/')

def process_site(base_url, history, drive_service):
    logging.info(f"در حال بررسی سایت: {base_url}")
    all_links = set()
    
    # بررسی ۱۰ صفحه اول
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
        direct_url, video_id = get_direct_video_url_and_id(link)
        
        if not video_id or video_id in history:
            continue
            
        logging.info(f"شروع پردازش ویدیوی جدید: {video_id}")
        
        # تنظیمات کیفیت بالا برای دانلود ویدیو از طریق yt-dlp
        ydl_opts = {
            'format': 'bestvideo+bestaudio/best', # بالاترین کیفیت موجود ویدیو و صدا
            'outtmpl': 'downloads/%(title)s [%(id)s].%(ext)s',
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            'restrictfilenames': True,
            'impersonate': 'chrome' # استفاده از شبیه‌ساز کروم برای دانلود از هاست ویدیوها
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(direct_url, download=True)
                if not info:
                    logging.warning(f"امکان دانلود ویدیو وجود نداشت. آدرس ویدیو رد شد.")
                    continue
                    
                filename = ydl.prepare_filename(info)
                
                # بررسی تغییر پسوند احتمالی (مثلاً mkv یا webm)
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
