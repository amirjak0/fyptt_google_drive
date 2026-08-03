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
HISTORY_FILE = 'download_history.txt'
STATE_FILE = 'current_page.txt'

# خواندن تنظیمات از متغیرهای محیطی
REVERSE_VIDEO_ORDER = os.environ.get("REVERSE_VIDEO_ORDER", "False").lower() in ("true", "1", "yes")
MAX_DOWNLOADS_PER_RUN = int(os.environ.get("MAX_DOWNLOADS_PER_RUN", "100"))
PAGES_PER_RUN = int(os.environ.get("PAGES_PER_RUN", "2"))
NEWEST_PAGES_TO_SCAN = int(os.environ.get("NEWEST_PAGES_TO_SCAN", "2"))

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
    history = set()
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        history.add(line)
            logging.info(f"تعداد {len(history)} ویدیو از تاریخچه محلی بارگذاری شد.")
        except Exception as e:
            logging.error(f"خطا در بارگذاری تاریخچه محلی: {e}")
    return history

def save_history(history_set):
    try:
        content = "\n".join(sorted(list(history_set)))
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        logging.info("فایل تاریخچه محلی با موفقیت ذخیره شد.")
    except Exception as e:
        logging.error(f"خطا در ذخیره تاریخچه محلی: {e}")

def load_current_page():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val.isdigit():
                    page = int(val)
                    # اگر مقدار کمتر از ۳ باشد، آن را به ۳ تغییر می‌دهیم، زیرا صفحات ۱ و ۲ همیشه اسکن می‌شوند.
                    if page < 3:
                        page = 3
                    logging.info(f"صفحه شروع قبلی آرشیو یافت شد: {page}")
                    return page
        except Exception as e:
            logging.error(f"خطا در خواندن فایل وضعیت صفحه: {e}")
    logging.info("فایل وضعیت آرشیو پیدا نشد. شروع آرشیو از صفحه ۳.")
    return 3

def save_current_page(page):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(page))
        logging.info(f"صفحه شروع بعدی آرشیو ذخیره شد: {page}")
    except Exception as e:
        logging.error(f"خطا در ذخیره فایل وضعیت صفحه: {e}")

def get_page_url(base_url, page_num):
    if page_num <= 1:
        return base_url
    parsed = urlparse(base_url)
    path = parsed.path.rstrip('/')
    if re.search(r'/page/\d+$', path):
        path = re.sub(r'/page/\d+$', f'/page/{page_num}', path)
    else:
        path = f"{path}/page/{page_num}/"
    return urlunparse(parsed._replace(path=path))

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

def scrape_video_links(target_url, history_set, start_page=1, pages_to_scan=2):
    """
    استخراج لینک‌ها از محدوده مشخصی از صفحات سایت بدون ارسال درخواست مسدودکننده HEAD
    """
    video_links = []
    target_domain = urlparse(target_url).netloc
    current_url = get_page_url(target_url, start_page)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    reached_end = False
    successful_pages = 0

    for i in range(pages_to_scan):
        if not current_url:
            reached_end = True
            break
            
        page_num = start_page + i
        logging.info(f"در حال بررسی صفحه {page_num}: {current_url}")
        
        try:
            response = requests_cffi.get(current_url, headers=headers, impersonate="chrome", timeout=20)
            if response.status_code == 404:
                logging.info(f"صفحه {page_num} وجود ندارد (خطای 404). به پایان سایت رسیدیم.")
                reached_end = True
                break
                
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            page_links = []
            for a_tag in soup.find_all('a'):
                href = a_tag.get('href')
                if href:
                    full_url = urljoin(current_url, href)
                    parsed = urlparse(full_url)
                    path = parsed.path.lower()
                    
                    if parsed.netloc == target_domain:
                        path_parts = path.strip('/').split('/')
                        if path_parts and path_parts[0].isdigit():
                            page_links.append(full_url)
            
            if not page_links:
                logging.info(f"هیچ لینک ویدیویی در صفحه {page_num} یافت نشد.")
                reached_end = True
                break

            video_links.extend(page_links)
            logging.info(f"{len(page_links)} لینک در صفحه {page_num} پیدا شد.")
            successful_pages += 1

            # پیدا کردن آدرس صفحه بعدی
            next_page = None
            next_link_tag = soup.find('a', rel='next') or \
                            soup.find('a', class_=re.compile(r'next|pagination', re.I)) or \
                            soup.find('a', string=re.compile(r'next|بعدی|›|»|older', re.I))
            
            if next_link_tag and next_link_tag.get('href'):
                next_page = urljoin(current_url, next_link_tag['href'])
            else:
                parsed_current = urlparse(current_url)
                path = parsed_current.path.rstrip('/')
                match = re.search(r'/page/(\d+)/?$', path)
                if match:
                    current_p_num = int(match.group(1))
                    next_path = re.sub(r'/page/\d+/?$', f'/page/{current_p_num + 1}/', path)
                    next_page = urlunparse(parsed_current._replace(path=next_path))
                else:
                    if path.endswith('/'):
                        next_page = urlunparse(parsed_current._replace(path=path + 'page/2/'))
                    else:
                        next_page = urlunparse(parsed_current._replace(path=path + '/page/2/'))

            current_url = next_page
                
        except Exception as e:
            logging.error(f"خطا در اسکن صفحه {current_url}: {e}")
            break

    seen = set()
    unique_links = []
    for link in video_links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
            
    return unique_links, reached_end, successful_pages

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
        logging.error("آدرس سایت هدف یا شناسه پوشه گوگل درایو موجود نیست.")
        return

    service = get_gdrive_service()
    if not service:
        return

    # بارگذاری فایل تاریخچه ویدیوها
    history = load_history()

    # ۱. اسکن مداوم جدیدترین ویدیوها (همیشه صفحات ۱ و ۲)
    logging.info(f"شروع اسکن صفحات جدید روزانه (صفحات ۱ تا {NEWEST_PAGES_TO_SCAN})...")
    newest_urls, _, _ = scrape_video_links(
        target_url, 
        history, 
        start_page=1, 
        pages_to_scan=NEWEST_PAGES_TO_SCAN
    )

    # ۲. اسکن پله‌پله آرشیو قدیمی (بر اساس وضعیت ذخیره شده قبلی)
    start_page = load_current_page()
    logging.info(f"شروع اسکن پله‌پله آرشیو قدیمی (شروع از صفحه {start_page} به مدت {PAGES_PER_RUN} صفحه)...")
    
    archive_urls, reached_end, successful_pages = scrape_video_links(
        target_url, 
        history, 
        start_page=start_page, 
        pages_to_scan=PAGES_PER_RUN
    )
    
    # تعیین و ذخیره صفحه شروع بعدی برای اجرای نوبت بعدی
    if reached_end:
        logging.info("به انتهای صفحات سایت یا یک صفحه نامعتبر در بخش آرشیو رسیدیم. وضعیت صفحه به ۳ بازنشانی می‌شود.")
        # بازنشانی به صفحه ۳ (چون صفحات ۱ و ۲ همیشه توسط بخش جدیدترین‌ها لود می‌شوند)
        save_current_page(3)
    else:
        save_current_page(start_page + successful_pages)

    # ادغام لینک‌های یافت شده در هر دو بخش و حذف لینک‌های تکراری مشترک
    combined_urls = []
    seen = set()
    for url in newest_urls + archive_urls:
        if url not in seen:
            seen.add(url)
            combined_urls.append(url)

    if not combined_urls:
        logging.warning("هیچ ویدیوی جدیدی در صفحات اسکن شده این نوبت یافت نشد.")
        return

    if REVERSE_VIDEO_ORDER:
        logging.info("ترتیب دانلود طبق تنظیمات کاربر معکوس شد.")
        combined_urls.reverse()

    logging.info(f"تعداد {len(combined_urls)} لینک ویدیوی منحصر‌به‌فرد برای پردازش آماده است.")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    downloaded_count = 0
    history_changed = False

    for url in combined_urls:
        if downloaded_count >= MAX_DOWNLOADS_PER_RUN:
            logging.info(f"به محدودیت دانلود {MAX_DOWNLOADS_PER_RUN} ویدیو در این نوبت رسیدیم. متوقف شد.")
            break
            
        clean_key = get_clean_url_key(url)
        if clean_key in history:
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

    # ذخیره فایل دانلودها
    if history_changed:
        save_history(history)

if __name__ == "__main__":
    setup_environment()
    download_and_process()
