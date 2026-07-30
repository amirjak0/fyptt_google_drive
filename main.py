import os
import re
import logging
import mimetypes
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# وارد کردن کتابخانه curl_cffi برای دور زدن سیستم کلودفلر
try:
    from curl_cffi import requests as requests_cffi
except ImportError:
    logging.warning("کتابخانه curl_cffi نصب نیست. ممکن است با خطای 403 مواجه شوید.")
    import requests as requests_cffi

# تنظیمات لاگ‌گیری
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DOWNLOAD_FOLDER = 'downloads'
MAX_DOWNLOADS_PER_RUN = 100
HISTORY_FILE = 'download_history.txt'

# تنظیم ترتیب دانلود: اگر True باشد، ویدیوها از آخر به اول دانلود می‌شوند.
REVERSE_VIDEO_ORDER = os.environ.get("REVERSE_VIDEO_ORDER", "False").lower() in ("true", "1", "yes")

def setup_environment():
    if not os.path.exists(DOWNLOAD_FOLDER):
        os.makedirs(DOWNLOAD_FOLDER)

def get_gdrive_service():
    client_id = os.environ.get("GDRIVE_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET")
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")
    
    if not all([client_id, client_secret, refresh_token]):
        logging.error("اطلاعات اتصال به گوگل درایو در متغیرهای محیطی یافت نشد.")
        return None

    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret
        )
        creds.refresh(Request())
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception as e:
        logging.error(f"خطا در اتصال به گوگل درایو: {e}")
        return None

def load_history():
    """
    بارگذاری تاریخچه به صورت محلی از گیت‌هاب
    """
    history = set()
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        history.add(line)
            logging.info(f"تعداد {len(history)} ویدیو از تاریخچه محلی (گیت‌هاب) بارگذاری شد.")
        except Exception as e:
            logging.error(f"خطا در بارگذاری تاریخچه محلی: {e}")
    else:
        logging.info("فایل تاریخچه محلی یافت نشد. یک فایل جدید ایجاد خواهد شد.")
    return history

def save_history(history_set):
    """
    ذخیره تاریخچه به صورت محلی برای کامیت شدن در گیت‌هاب
    """
    try:
        content = "\n".join(sorted(list(history_set)))
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        logging.info("فایل تاریخچه محلی با موفقیت ذخیره شد.")
    except Exception as e:
        logging.error(f"خطا در ذخیره تاریخچه محلی: {e}")

def upload_to_gdrive(service, folder_id, file_path):
    logging.info(f"در حال آپلود: {os.path.basename(file_path)}")
    try:
        file_metadata = {'name': os.path.basename(file_path), 'parents': [folder_id]}
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = 'application/octet-stream'
            
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        logging.info(f"آپلود موفقیت‌آمیز بود. شناسه فایل: {file.get('id')}")
        return True
    except Exception as e:
        logging.error(f"خطا در آپلود به گوگل درایو: {e}")
        return False

def get_clean_url_key(url):
    try:
        parsed = urlparse(url)
        query_params = parse_qsl(parsed.query)
        ignored_keys = {'token', 'expires', 'signature', 'sig', 'hash', 'auth', 'time', 't', 'session', 'session_id'}
        clean_params = [(k, v) for k, v in query_params if k.lower() not in ignored_keys]
        
        clean_query = urlencode(clean_params)
        clean_parsed = parsed._replace(query=clean_query, fragment='')
        return urlunparse(clean_parsed)
    except Exception:
        return url

def get_ydl_impersonate_opts():
    opts = {}
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts['impersonate'] = ImpersonateTarget.from_str('chrome')
    except Exception:
        opts['impersonate'] = 'chrome'
    return opts

def scrape_video_links(target_url):
    video_links = []
    target_domain = urlparse(target_url).netloc
    
    try:
        logging.info("در حال ارسال درخواست وب‌سایت با تکنولوژی curl_cffi (تقلید هویت کروم)...")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = requests_cffi.get(target_url, headers=headers, impersonate="chrome", timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for video_tag in soup.find_all('video'):
            src = video_tag.get('src')
            if src:
                video_links.append(urljoin(target_url, src))
            for source in video_tag.find_all('source'):
                source_src = source.get('src')
                if source_src:
                    video_links.append(urljoin(target_url, source_src))
                    
        for a_tag in soup.find_all('a'):
            href = a_tag.get('href')
            if href:
                full_url = urljoin(target_url, href)
                parsed = urlparse(full_url)
                path = parsed.path.lower()
                
                video_extensions = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.3gp', '.m3u8')
                if any(path.endswith(ext) for ext in video_extensions):
                    video_links.append(full_url)
                    continue
                
                if parsed.netloc == target_domain:
                    path_parts = path.strip('/').split('/')
                    if path_parts and path_parts[0].isdigit():
                        video_links.append(full_url)
                        
    except Exception as e:
        logging.error(f"خطا در اسکن اولیه صفحه با BeautifulSoup: {e}")
        
    seen = set()
    unique_links = []
    for link in video_links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
            
    return unique_links

def extract_post_info(url):
    try:
        parsed = urlparse(url)
        path_parts = parsed.path.strip('/').split('/')
        if path_parts and path_parts[0].isdigit():
            post_id = path_parts[0]
            post_title = "video"
            if len(path_parts) > 1:
                post_title = path_parts[1].replace('-', ' ').title()
            return post_id, post_title
    except Exception:
        pass
    return "unknown", "video"

def extract_direct_video_url(post_url, headers):
    try:
        response = requests_cffi.get(post_url, headers=headers, impersonate="chrome", timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        def find_src_in_soup(s):
            for v in s.find_all('video'):
                src = v.get('src')
                if src: return src
            for src_tag in s.find_all('source'):
                src = src_tag.get('src')
                if src: return src
            for meta_prop in ['og:video', 'og:video:secure_url', 'og:video:url']:
                meta = s.find('meta', property=meta_prop)
                if meta and meta.get('content'):
                    return meta.get('content')
            for script in s.find_all('script'):
                if script.string:
                    match = re.search(r'file\s*:\s*["\'](https?://[^"\']+\.mp4(?:\?[^"\']*)?)["\']', script.string)
                    if match:
                        return match.group(1)
                    match_generic = re.search(r'["\'](https?://[^"\']+\.mp4(?:\?[^"\']*)?)["\']', script.string)
                    if match_generic:
                        return match_generic.group(1)
            return None

        direct_url = find_src_in_soup(soup)
        if direct_url:
            return urljoin(post_url, direct_url)
            
        for iframe in soup.find_all('iframe'):
            src = iframe.get('src')
            if src and ('fypttstr.php' in src or 'player' in src or 'embed' in src):
                iframe_url = urljoin(post_url, src)
                logging.info(f"یافتن فریم ویدیو پلیر: {iframe_url}")
                
                iframe_resp = requests_cffi.get(iframe_url, headers=headers, impersonate="chrome", timeout=15)
                iframe_resp.raise_for_status()
                iframe_soup = BeautifulSoup(iframe_resp.text, 'html.parser')
                
                iframe_video = find_src_in_soup(iframe_soup)
                if iframe_video:
                    return urljoin(iframe_url, iframe_video)
                    
    except Exception as e:
        logging.error(f"خطا در استخراج آدرس مستقیم ویدیو از {post_url}: {e}")
    return None

def download_and_process():
    target_url = os.environ.get("TARGET_SITE_URL")
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    
    if not target_url or not folder_id:
        logging.error("آدرس سایت هدف (TARGET_SITE_URL) یا شناسه پوشه گوگل درایو موجود نیست.")
        return

    service = get_gdrive_service()
    if not service:
        return

    # بارگذاری تاریخچه محلی از مخزن گیت‌هاب
    history = load_history()

    logging.info(f"در حال بررسی صفحه هدف: {target_url}")
    scraped_urls = scrape_video_links(target_url)
    
    if not scraped_urls:
        logging.warning("هیچ ویدیویی در صفحه مورد نظر یافت نشد.")
        return

    if REVERSE_VIDEO_ORDER:
        logging.info("ترتیب دانلود طبق تنظیمات کاربر معکوس شد.")
        scraped_urls.reverse()

    logging.info(f"تعداد {len(scraped_urls)} لینک ویدیوی منحصر‌به‌فرد یافت شد.")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    downloaded_count = 0
    history_changed = False

    for url in scraped_urls:
        if downloaded_count >= MAX_DOWNLOADS_PER_RUN:
            logging.info(f"به محدودیت دانلود {MAX_DOWNLOADS_PER_RUN} ویدیو در این نوبت رسیدیم. متوقف شد.")
            break
            
        clean_key = get_clean_url_key(url)
        if clean_key in history:
            logging.info(f"این ویدیو قبلاً پردازش شده است و نادیده گرفته می‌شود: {clean_key}")
            continue

        post_id, post_title = extract_post_info(url)
        logging.info(f"شروع پردازش ویدیوی جدید: [{post_id}] {post_title}")
        
        direct_video_url = extract_direct_video_url(url, headers)
        if not direct_video_url:
            logging.warning(f"امکان استخراج فایل ویدیو از آدرس {url} وجود نداشت. ویدیو رد شد.")
            continue
            
        logging.info(f"آدرس مستقیم فایل ویدیو با موفقیت استخراج شد: {direct_video_url}")

        clean_title = "".join(c for c in post_title if c.isalnum() or c in (' ', '_', '-')).strip()
        clean_title = clean_title[:80]

        download_opts = {
            'format': 'best',
            'outtmpl': f'{DOWNLOAD_FOLDER}/{clean_title} [{post_id}].%(ext)s',
            'ignoreerrors': True,
            'http_headers': {
                'Referer': url,
                'User-Agent': headers['User-Agent']
            }
        }

        try:
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                info = ydl.extract_info(direct_video_url, download=True)
                if info is None:
                    logging.warning(f"دانلود ویدیو از آدرس مستقیم {direct_video_url} ناموفق بود.")
                    continue
                
                duration = info.get('duration')
                expected_path = ydl.prepare_filename(info)
                
                file_path = expected_path
                if not os.path.exists(file_path):
                    base_path = os.path.splitext(expected_path)[0]
                    for ext in ['mp4', 'mkv', 'webm', 'avi']:
                        test_path = f"{base_path}.{ext}"
                        if os.path.exists(test_path):
                            file_path = test_path
                            break

                if duration is not None and duration > 600:
                    logging.info(f"مدت زمان ویدیو پس از دانلود ({duration} ثانیه) بیش از ۱۰ دقیقه است. حذف فایل...")
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                    history.add(clean_key)
                    history_changed = True
                    continue

                if file_path and os.path.exists(file_path):
                    if upload_to_gdrive(service, folder_id, file_path):
                        os.remove(file_path)
                        logging.info("فایل محلی از روی سرور حذف شد.")
                        
                        history.add(clean_key)
                        history_changed = True
                        downloaded_count += 1
                else:
                    logging.error(f"فایل دانلود شده یافت نشد: {expected_path}")
                    
        except Exception as e:
            logging.error(f"خطا در پردازش ویدیو {url}: {e}")

    # در صورت تغییر تاریخچه، آن را به عنوان یک فایل محلی ذخیره کن
    if history_changed:
        save_history(history)

if __name__ == "__main__":
    setup_environment()
    download_and_process()
