import os
import sys
import re
import logging
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from curl_cffi import requests as requests_cffi
import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# تنظیمات لاگینگ
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)

HISTORY_FILE = 'download_history.txt'
DOWNLOAD_DIR = 'downloads'

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

def load_history():
    """خواندن لیست لینک‌هایی که قبلاً پردازش شده‌اند"""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_to_history(url):
    """ذخیره لینک پردازش شده در فایل تاریخچه"""
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{url}\n")

def get_gdrive_service(client_id, client_secret, refresh_token):
    """اتصال به سرویس Google Drive با توکن‌ها"""
    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret
        )
        if creds.expired or not creds.valid:
            creds.refresh(Request())
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        logging.error(f"خطا در احراز هویت Google Drive: {e}")
        return None

def upload_to_gdrive(service, file_path, folder_id):
    """آپلود فایل دانلود شده به گوگل درایو"""
    try:
        file_name = os.path.basename(file_path)
        file_metadata = {
            'name': file_name,
            'parents': [folder_id] if folder_id else []
        }
        media = MediaFileUpload(file_path, resumable=True)
        uploaded = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name'
        ).execute()
        logging.info(f"فایل با موفقیت در گوگل درایو ذخیره شد: {uploaded.get('name')} (ID: {uploaded.get('id')})")
        return True
    except Exception as e:
        logging.error(f"خطا در آپلود فایل {file_path} به گوگل درایو: {e}")
        return False

def extract_media_from_post(post_url, headers):
    """
    استخراج مستقیم ویدیو یا ورود به صفحات واسط (لینک‌های روی عکس‌ها)
    برای پیدا کردن فایل نهایی ویدیو
    """
    video_url = None
    animated_images = []
    
    try:
        response = requests_cffi.get(post_url, headers=headers, impersonate="chrome", timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # تابع کمکی برای جستجوی انواع فرمت‌های ویدیو در کدهای HTML
        def find_video_src(s):
            # 1. تگ‌های ویدیویی استاندارد
            for v in s.find_all('video'):
                src = v.get('src') or v.get('data-src')
                if src: return src
            for src_tag in s.find_all('source'):
                src = src_tag.get('src') or src_tag.get('data-src')
                if src: return src
                
            # 2. متاتگ‌های OpenGraph
            for meta_prop in ['og:video', 'og:video:secure_url', 'og:video:url', 'twitter:player:stream']:
                meta = s.find('meta', property=meta_prop) or s.find('meta', attrs={'name': meta_prop})
                if meta and meta.get('content'):
                    return meta.get('content')
                    
            # 3. بررسی اسکریپت‌ها و متغیرهای جاوااسکریپت پلیرها
            for script in s.find_all('script'):
                if script.string:
                    match = re.search(r'file\s*:\s*["\'](https?://[^"\']+\.(?:mp4|m3u8)(?:\?[^"\']*)?)["\']', script.string)
                    if match: return match.group(1)
                    match_generic = re.search(r'["\'](https?://[^"\']+\.(?:mp4|m3u8)(?:\?[^"\']*)?)["\']', script.string)
                    if match_generic: return match_generic.group(1)
            return None

        # گام 1: بررسی مستقیم صفحه جاری
        direct_url = find_video_src(soup)
        if direct_url:
            video_url = urljoin(post_url, direct_url)
        
        # گام 2: بررسی تمام تگ‌های iframe درون صفحه
        if not video_url:
            for iframe in soup.find_all('iframe'):
                src = iframe.get('src') or iframe.get('data-src')
                if src:
                    iframe_url = urljoin(post_url, src)
                    try:
                        iframe_resp = requests_cffi.get(iframe_url, headers=headers, impersonate="chrome", timeout=15)
                        if iframe_resp.status_code == 200:
                            iframe_soup = BeautifulSoup(iframe_resp.text, 'html.parser')
                            iframe_video = find_video_src(iframe_soup)
                            if iframe_video:
                                video_url = urljoin(iframe_url, iframe_video)
                                break
                    except Exception as e:
                        logging.debug(f"بررسی iframe ناموفق بود: {iframe_url} -> {e}")

        # گام 3: بررسی عکس‌های کلیک‌دار (Clickable Images) که به صفحات تریلر/اسپانسر می‌روند
        if not video_url:
            for a_tag in soup.find_all('a', href=True):
                if a_tag.find('img'):
                    href = a_tag.get('href')
                    full_href = urljoin(post_url, href)
                    
                    # اگر لینک عکس مستقیماً فایل ویدیویی بود
                    if any(full_href.lower().split('?')[0].endswith(ext) for ext in ['.mp4', '.webm', '.mkv']):
                        video_url = full_href
                        break
                    
                    # اگر لینک به یک صفحه دیگر (سایت خارجی، صفحه تریلر و ...) اشاره می‌کند
                    parsed_href = urlparse(full_href)
                    parsed_post = urlparse(post_url)
                    
                    if parsed_href.netloc != parsed_post.netloc or 'trailer' in full_href.lower() or 'track' in full_href.lower():
                        try:
                            ext_resp = requests_cffi.get(full_href, headers=headers, impersonate="chrome", timeout=15)
                            if ext_resp.status_code == 200:
                                ext_soup = BeautifulSoup(ext_resp.text, 'html.parser')
                                ext_video = find_video_src(ext_soup)
                                if ext_video:
                                    video_url = urljoin(full_href, ext_video)
                                    logging.info(f"ویدیو از صفحه متصل به تصویر پیدا شد: {video_url}")
                                    break
                        except Exception as e:
                            logging.debug(f"خطا در بررسی لینک تریلر {full_href}: {e}")

        # استخراج گیف‌ها و تصاویر متحرک در صورت تمایل
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if src:
                src_lower = src.lower()
                if '.gif' in src_lower or '.webp' in src_lower:
                    animated_images.append(urljoin(post_url, src))
                    
    except Exception as e:
        logging.error(f"خطا در پردازش آدرس {post_url}: {e}")
        
    return video_url, list(set(animated_images))

def scrape_video_links(target_site_url, headers):
    """یافتن تمام لینک‌های پست‌های داخل صفحه هدف"""
    post_links = []
    try:
        response = requests_cffi.get(target_site_url, headers=headers, impersonate="chrome", timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        excluded_patterns = [
            '/category/', '/tag/', '/author/', '/page/', '/wp-content/',
            '/feed/', '/comments/', '#', 'javascript:', 'mailto:',
            '/privacy', '/terms', '/contact', '/about', '/dmca'
        ]
        
        base_domain = urlparse(target_site_url).netloc

        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            full_url = urljoin(target_site_url, href)
            parsed_url = urlparse(full_url)
            
            # فقط لینک‌های داخلی همان سایت
            if parsed_url.netloc == base_domain and full_url != target_site_url:
                if not any(pattern in full_url.lower() for pattern in excluded_patterns):
                    if full_url not in post_links:
                        post_links.append(full_url)
                        
    except Exception as e:
        logging.error(f"خطا در استخراج لیست پست‌ها از {target_site_url}: {e}")
        
    return post_links

def download_video(video_url, download_dir):
    """دانلود فایل ویدیویی با استفاده از yt-dlp"""
    os.makedirs(download_dir, exist_ok=True)
    out_template = os.path.join(download_dir, '%(title).100s-%(id)s.%(ext)s')
    
    ydl_opts = {
        'outtmpl': out_template,
        'format': 'bestvideo+bestaudio/best',
        'quiet': False,
        'no_warnings': True,
        'noplaylist': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            downloaded_filename = ydl.prepare_filename(info)
            if os.path.exists(downloaded_filename):
                return downloaded_filename
            # در صورتی که پسوند فایل تغییر کرده باشد (مثلاً ادغام شده باشد)
            base = os.path.splitext(downloaded_filename)[0]
            for f in os.listdir(download_dir):
                if os.path.join(download_dir, f).startswith(base):
                    return os.path.join(download_dir, f)
    except Exception as e:
        logging.error(f"خطا در دانلود با yt-dlp برای آدرس {video_url}: {e}")
    return None

def main():
    target_site_url = os.environ.get('TARGET_SITE_URL')
    gdrive_client_id = os.environ.get('GDRIVE_CLIENT_ID')
    gdrive_client_secret = os.environ.get('GDRIVE_CLIENT_SECRET')
    gdrive_refresh_token = os.environ.get('GDRIVE_REFRESH_TOKEN')
    gdrive_folder_id = os.environ.get('GDRIVE_FOLDER_ID')
    reverse_order = os.environ.get('REVERSE_VIDEO_ORDER', 'False').lower() in ('true', '1', 'yes')

    if not target_site_url:
        logging.error("متغیر TARGET_SITE_URL تنظیم نشده است!")
        sys.exit(1)

    logging.info("در حال اتصال به Google Drive...")
    drive_service = get_gdrive_service(gdrive_client_id, gdrive_client_secret, gdrive_refresh_token)
    if not drive_service:
        logging.error("امکان اتصال به Google Drive وجود ندارد.")
        sys.exit(1)

    history = load_history()
    logging.info(f"تعداد {len(history)} لینک در تاریخچه قبلی یافت شد.")

    logging.info(f"در حال جمع‌آوری لینک‌های پست‌ها از {target_site_url}...")
    post_links = scrape_video_links(target_site_url, DEFAULT_HEADERS)
    logging.info(f"تعداد {len(post_links)} پست در صفحه اصلی پیدا شد.")

    if reverse_order:
        post_links.reverse()
        logging.info("ترتیب بررسی لینک‌ها معکوس شد.")

    for idx, post_url in enumerate(post_links, 1):
        if post_url in history:
            logging.info(f"[{idx}/{len(post_links)}] قبلاً دانلود شده، رد شد: {post_url}")
            continue

        logging.info(f"[{idx}/{len(post_links)}] در حال بررسی پست: {post_url}")
        video_url, _ = extract_media_from_post(post_url, DEFAULT_HEADERS)

        if not video_url:
            logging.warning(f"هیچ ویدیویی در {post_url} پیدا نشد.")
            save_to_history(post_url)
            continue

        logging.info(f"لینک ویدیو پیدا شد: {video_url}")
        logging.info("شروع فرآیند دانلود...")
        downloaded_file = download_video(video_url, DOWNLOAD_DIR)

        if downloaded_file and os.path.exists(downloaded_file):
            logging.info(f"فایل روی دیسک ذخیره شد: {downloaded_file} - شروع آپلود به گوگل درایو...")
            success = upload_to_gdrive(drive_service, downloaded_file, gdrive_folder_id)
            
            # حذف فایل محلی جهت آزادسازی حافظه رانر گیت‌هاب
            try:
                os.remove(downloaded_file)
            except Exception:
                pass

            if success:
                save_to_history(post_url)
                logging.info(f"فرآیند برای {post_url} با موفقیت به پایان رسید.")
        else:
            logging.error(f"دانلود ویدیو از {video_url} شکست خورد.")

if __name__ == '__main__':
    main()
