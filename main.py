import os
import logging
import mimetypes
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.auth.transport.requests import Request
import io

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

# تنظیم ترتیب دانلود: اگر True باشد، ویدیوها از آخر به اول (پایین صفحه به بالا) دانلود می‌شوند.
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

def get_history_file_id(service, folder_id):
    try:
        query = f"'{folder_id}' in parents and name = 'gdrive_download_history.txt' and trashed=false"
        results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
        items = results.get('files', [])
        if items:
            return items[0]['id']
    except Exception as e:
        logging.error(f"خطا در جستجوی فایل تاریخچه: {e}")
    return None

def load_history(service, folder_id):
    history = set()
    file_id = get_history_file_id(service, folder_id)
    if not file_id:
        logging.info("فایل تاریخچه (gdrive_download_history.txt) یافت نشد. یک فایل جدید ایجاد خواهد شد.")
        return history, None
    
    try:
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)
        content = fh.read().decode('utf-8', errors='ignore')
        for line in content.splitlines():
            line = line.strip()
            if line:
                history.add(line)
        logging.info(f"تعداد {len(history)} ویدیو از تاریخچه قبلی بارگذاری شد.")
        return history, file_id
    except Exception as e:
        logging.error(f"خطا در بارگذاری فایل تاریخچه: {e}")
        return history, file_id

def save_history(service, folder_id, file_id, history_set):
    content = "\n".join(sorted(list(history_set)))
    temp_file = "temp_history.txt"
    with open(temp_file, "w", encoding="utf-8") as f:
        f.write(content)
    
    try:
        media = MediaFileUpload(temp_file, mimetype='text/plain', resumable=True)
        if file_id:
            service.files().update(fileId=file_id, media_body=media).execute()
            logging.info("فایل تاریخچه در گوگل درایو با موفقیت بروزرسانی شد.")
        else:
            file_metadata = {'name': 'gdrive_download_history.txt', 'parents': [folder_id]}
            new_file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
            logging.info(f"فایل تاریخچه جدید در گوگل درایو ساخته شد. شناسه: {new_file.get('id')}")
            file_id = new_file.get('id')
    except Exception as e:
        logging.error(f"خطا در ذخیره فایل تاریخچه: {e}")
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)
    return file_id

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
    """
    دریافت تنظیمات شبیه‌سازی هویت مرورگر برای ساختار جدید yt-dlp
    """
    opts = {}
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts['impersonate'] = ImpersonateTarget.from_str('chrome')
    except Exception:
        # در صورتی که کلاس بالا موجود نباشد، رشته خام را ارسال می‌کنیم
        opts['impersonate'] = 'chrome'
    return opts

def scrape_video_links(target_url):
    video_links = []
    
    # روش اول: استفاده از curl_cffi برای دور زدن کلودفلر و گرفتن تگ‌های HTML
    try:
        logging.info("در حال ارسال درخواست وب‌سایت با تکنولوژی curl_cffi (تقلید هویت کروم)...")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        # استفاده از impersonate="chrome" برای عبور از سد امنیتی کلودفلر
        response = requests_cffi.get(target_url, headers=headers, impersonate="chrome", timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # اسکن تگ‌های <video> و <source>
        for video_tag in soup.find_all('video'):
            src = video_tag.get('src')
            if src:
                video_links.append(urljoin(target_url, src))
            for source in video_tag.find_all('source'):
                source_src = source.get('src')
                if source_src:
                    video_links.append(urljoin(target_url, source_src))
                    
        # اسکن تگ‌های <a> که مستقیماً به فرمت‌های ویدیویی لینک شده‌اند
        video_extensions = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.3gp', '.m3u8')
        for a_tag in soup.find_all('a'):
            href = a_tag.get('href')
            if href:
                parsed = urlparse(href)
                path = parsed.path.lower()
                if any(path.endswith(ext) for ext in video_extensions):
                    video_links.append(urljoin(target_url, href))
                    
    except Exception as e:
        logging.error(f"خطا در اسکن اولیه صفحه با BeautifulSoup: {e}")
        
    # روش دوم کمکی: استفاده از اسکنر عمومی yt-dlp به همراه ویژگی شبیه‌ساز هویت مرورگر
    if not video_links:
        logging.info("تگ مستقیمی یافت نشد. در حال تلاش با اسکنر عمومی yt-dlp...")
        ydl_opts = {
            'extract_flat': 'in_playlist',
            'quiet': True,
            'no_warnings': True,
            'extractor_args': {'generic': ['impersonate']},
        }
        ydl_opts.update(get_ydl_impersonate_opts())
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                playlist_dict = ydl.extract_info(target_url, download=False)
                if playlist_dict and 'entries' in playlist_dict:
                    for entry in playlist_dict['entries']:
                        if entry and entry.get('url'):
                            video_links.append(entry.get('url'))
        except Exception as e:
            logging.error(f"خطا در استخراج با yt-dlp: {e}")

    seen = set()
    unique_links = []
    for link in video_links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
            
    return unique_links

def find_downloaded_file(info, expected_path):
    if os.path.exists(expected_path):
        return expected_path
    
    base_path = os.path.splitext(expected_path)[0]
    for ext in ['mp4', 'mkv', 'webm', 'avi']:
        test_path = f"{base_path}.{ext}"
        if os.path.exists(test_path):
            return test_path
            
    try:
        title = info.get('title', '')
        video_id = info.get('id', '')
        files = [os.path.join(DOWNLOAD_FOLDER, f) for f in os.listdir(DOWNLOAD_FOLDER)]
        files.sort(key=os.path.getmtime, reverse=True)
        for f in files:
            if (video_id and video_id in f) or (title and title[:15] in f):
                return f
    except Exception as e:
        logging.error(f"خطا در جستجوی فایل دانلود شده در پوشه محلی: {e}")
        
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

    history, history_file_id = load_history(service, folder_id)

    logging.info(f"در حال بررسی صفحه هدف: {target_url}")
    scraped_urls = scrape_video_links(target_url)
    
    if not scraped_urls:
        logging.warning("هیچ ویدیویی در صفحه مورد نظر یافت نشد.")
        return

    # معکوس کردن لیست اگر کاربر در تنظیمات آن را فعال کرده باشد
    if REVERSE_VIDEO_ORDER:
        logging.info("ترتیب دانلود طبق تنظیمات کاربر معکوس شد تا انتهای صفحه (پایین صفحه) زودتر دانلود شود.")
        scraped_urls.reverse()

    logging.info(f"تعداد {len(scraped_urls)} لینک ویدیوی منحصر‌به‌فرد یافت شد.")

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

        logging.info(f"شروع پردازش ویدیوی جدید: {url}")
        
        ydl_opts_info = {
            'quiet': True,
            'no_warnings': True,
            'extractor_args': {'generic': ['impersonate']},
        }
        ydl_opts_info.update(get_ydl_impersonate_opts())
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    duration = info.get('duration')
                    if duration is not None and duration > 600:
                        logging.info(f"مدت زمان ویدیو ({duration} ثانیه) از ۱۰ دقیقه بیشتر است. رد شد.")
                        history.add(clean_key)
                        history_changed = True
                        continue
        except Exception as e:
            logging.warning(f"امکان دریافت اطلاعات زمانی پیش از دانلود وجود نداشت ({e}). دانلود شروع می‌شود تا مستقیماً تست شود.")

        download_opts = {
            'format': 'bestvideo+bestaudio/best',
            'outtmpl': f'{DOWNLOAD_FOLDER}/%(title)s [%(id)s].%(ext)s',
            'merge_output_format': 'mp4',
            'ignoreerrors': True,
            'extractor_args': {'generic': ['impersonate']},
        }
        download_opts.update(get_ydl_impersonate_opts())

        try:
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    logging.warning(f"دانلود ویدیو از آدرس {url} ناموفق بود.")
                    continue
                
                duration = info.get('duration')
                expected_path = ydl.prepare_filename(info)
                file_path = find_downloaded_file(info, expected_path)

                if duration is not None and duration > 600:
                    logging.info(f"مدت زمان ویدیو پس از دانلود ({duration} ثانیه) بیش از حد مجاز تشخیص داده شد. حذف فایل...")
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

    if history_changed:
        save_history(service, folder_id, history_file_id, history)

if __name__ == "__main__":
    setup_environment()
    download_and_process()
