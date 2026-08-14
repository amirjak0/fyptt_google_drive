import os
import re
import logging
import mimetypes
import hashlib
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

# محدودیت دانلود: 2 گیگابایت (به بایت)
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024 

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
    history = set()
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        history.add(line)
            logging.info(f"تعداد {len(history)} آیتم از تاریخچه محلی بارگذاری شد.")
        except Exception as e:
            logging.error(f"خطا در بارگذاری تاریخچه محلی: {e}")
    else:
        logging.info("فایل تاریخچه محلی یافت نشد. یک فایل جدید ایجاد خواهد شد.")
    return history

def save_history(history_set):
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

def scrape_video_links(target_url, history_set, max_pages=20):
    video_links = []
    target_domain = urlparse(target_url).netloc
    current_url = target_url
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    stop_pagination = False
    
    excluded_paths = {
        'category', 'niche', 'studios', 'trending', 'hot', 'top', 
        'upcoming-xxx', 'porn-update-new-porn-videos-todays-scenes', 
        'pornstars-top', 'page', 'tag', 'dmca', 'iamgettingoutnow', 'amember'
    }

    for page_num in range(1, max_pages + 1):
        if not current_url or stop_pagination:
            break
            
        logging.info(f"در حال بررسی صفحه {page_num}: {current_url}")
        
        try:
            response = requests_cffi.get(current_url, headers=headers, impersonate="chrome", timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            page_links = []
            
            for a_tag in soup.find_all('a'):
                href = a_tag.get('href')
                if href:
                    full_url = urljoin(current_url, href)
                    parsed = urlparse(full_url)
                    path = parsed.path.strip('/').lower()
                    
                    if parsed.netloc == target_domain and path:
                        path_parts = path.split('/')
                        if len(path_parts) == 1 and path_parts[0] not in excluded_paths:
                            page_links.append(full_url)
            
            if not page_links:
                logging.info("هیچ لینک پستی در این صفحه یافت نشد. توقف.")
                break

            page_all_duplicates = True
            for link in page_links:
                clean_key = get_clean_url_key(link)
                if clean_key not in history_set:
                    page_all_duplicates = False
                    break
            
            if page_all_duplicates:
                logging.info(f"تمام پست‌های صفحه {page_num} قبلاً بررسی شده‌اند. اسکن متوقف شد.")
                stop_pagination = True
            else:
                video_links.extend(page_links)
                logging.info(f"{len(page_links)} لینک معتبر در صفحه {page_num} پیدا شد.")

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
                    next_page = urlunparse(parsed_current._replace(path=path + '/page/2/'))

            if next_page:
                try:
                    head_resp = requests_cffi.head(next_page, headers=headers, impersonate="chrome", timeout=10, allow_redirects=True)
                    if head_resp.status_code == 200:
                        current_url = next_page
                    else:
                        break
                except Exception:
                    break
            else:
                break
                
        except Exception as e:
            logging.error(f"خطا در اسکن صفحه {current_url}: {e}")
            break

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
        path = parsed.path.strip('/')
        if path:
            post_id = hashlib.md5(path.encode()).hexdigest()[:8]
            post_title = path.replace('-', ' ').title()
            return post_id, post_title
    except Exception:
        pass
    return "unknown", "post"

def extract_media_from_post(post_url, headers):
    video_url = None
    animated_images = []
    
    try:
        response = requests_cffi.get(post_url, headers=headers, impersonate="chrome", timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. پیدا کردن ویدیو
        def find_video_src(s):
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
                    if match: return match.group(1)
                    match_generic = re.search(r'["\'](https?://[^"\']+\.mp4(?:\?[^"\']*)?)["\']', script.string)
                    if match_generic: return match_generic.group(1)
            return None

        direct_url = find_video_src(soup)
        if direct_url:
            video_url = urljoin(post_url, direct_url)
        else:
            for iframe in soup.find_all('iframe'):
                src = iframe.get('src')
                if src and ('fypttstr.php' in src or 'player' in src or 'embed' in src):
                    iframe_url = urljoin(post_url, src)
                    iframe_resp = requests_cffi.get(iframe_url, headers=headers, impersonate="chrome", timeout=15)
                    if iframe_resp.status_code == 200:
                        iframe_soup = BeautifulSoup(iframe_resp.text, 'html.parser')
                        iframe_video = find_video_src(iframe_soup)
                        if iframe_video:
                            video_url = urljoin(iframe_url, iframe_video)
                            break

        # 2. پیدا کردن تصاویر متحرک (GIF و WebP)
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if src:
                src_lower = src.lower()
                if '.gif' in src_lower or '.webp' in src_lower:
                    animated_images.append(urljoin(post_url, src))
                    
    except Exception as e:
        logging.error(f"خطا در استخراج مدیا از {post_url}: {e}")
        
    return video_url, list(set(animated_images))

def download_image(url, headers, filepath):
    try:
        resp = requests_cffi.get(url, headers=headers, impersonate="chrome", timeout=30)
        resp.raise_for_status()
        with open(filepath, 'wb') as f:
            f.write(resp.content)
        return True
    except Exception as e:
        logging.error(f"خطا در دانلود تصویر {url}: {e}")
        return False

def download_and_process():
    target_url = os.environ.get("TARGET_SITE_URL", "https://namethatpornad.com/")
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    
    if not target_url or not folder_id:
        logging.error("آدرس سایت هدف یا شناسه پوشه گوگل درایو موجود نیست.")
        return

    service = get_gdrive_service()
    if not service:
        return

    history = load_history()
    total_downloaded_bytes = 0

    logging.info(f"در حال بررسی صفحه هدف: {target_url}")
    scraped_urls = scrape_video_links(target_url, history, max_pages=10)
    
    if not scraped_urls:
        logging.warning("هیچ پست جدیدی یافت نشد.")
        return

    if REVERSE_VIDEO_ORDER:
        scraped_urls.reverse()

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    history_changed = False

    for url in scraped_urls:
        # بررسی محدودیت 2 گیگابایت قبل از شروع دانلود فایل جدید
        if total_downloaded_bytes >= MAX_DOWNLOAD_BYTES:
            logging.info(f"حجم دانلود به سقف ۲ گیگابایت رسید (دانلود شده: {total_downloaded_bytes / (1024*1024):.2f} MB). پایان عملیات برای امروز.")
            break
            
        clean_post_key = get_clean_url_key(url)
        if clean_post_key in history:
            continue

        post_id, post_title = extract_post_info(url)
        logging.info(f"شروع پردازش پست: [{post_id}] {post_title}")
        
        video_url, animated_images = extract_media_from_post(url, headers)
        
        clean_title = "".join(c for c in post_title if c.isalnum() or c in (' ', '_', '-')).strip()[:80]

        # --- پردازش ویدیو ---
        if video_url:
            clean_video_key = get_clean_url_key(video_url)
            if clean_video_key not in history:
                logging.info(f"در حال دانلود ویدیو: {video_url}")
                
                # تنظیمات برای بالاترین کیفیت تا 4K
                download_opts = {
                    'format': 'bestvideo[height<=2160]+bestaudio/best[height<=2160]/best',
                    'outtmpl': f'{DOWNLOAD_FOLDER}/{clean_title} [{post_id}].%(ext)s',
                    'ignoreerrors': True,
                    'http_headers': {'Referer': url, 'User-Agent': headers['User-Agent']}
                }

                try:
                    with yt_dlp.YoutubeDL(download_opts) as ydl:
                        info = ydl.extract_info(video_url, download=True)
                        if info:
                            expected_path = ydl.prepare_filename(info)
                            file_path = expected_path
                            
                            # پیدا کردن فایل نهایی (گاهی فرمت بعد از ادغام تغییر میکند)
                            if not os.path.exists(file_path):
                                base_path = os.path.splitext(expected_path)[0]
                                for ext in ['mp4', 'mkv', 'webm', 'avi']:
                                    if os.path.exists(f"{base_path}.{ext}"):
                                        file_path = f"{base_path}.{ext}"
                                        break

                            if file_path and os.path.exists(file_path):
                                file_size = os.path.getsize(file_path)
                                total_downloaded_bytes += file_size
                                
                                if upload_to_gdrive(service, folder_id, file_path):
                                    os.remove(file_path)
                                    history.add(clean_video_key)
                                    history_changed = True
                except Exception as e:
                    logging.error(f"خطا در دانلود ویدیو {video_url}: {e}")

        # --- پردازش تصاویر متحرک ---
        for idx, img_url in enumerate(animated_images):
            # بررسی مجدد محدودیت حجم قبل از دانلود هر عکس
            if total_downloaded_bytes >= MAX_DOWNLOAD_BYTES:
                logging.info("حجم دانلود به سقف ۲ گیگابایت رسید. توقف دانلود تصاویر.")
                break
                
            clean_img_key = get_clean_url_key(img_url)
            if clean_img_key in history:
                continue
                
            ext = img_url.split('.')[-1].split('?')[0]
            if ext.lower() not in ['gif', 'webp']:
                ext = 'gif' # پیش‌فرض
                
            img_filename = f"{clean_title} [{post_id}]_anim_{idx}.{ext}"
            img_filepath = os.path.join(DOWNLOAD_FOLDER, img_filename)
            
            logging.info(f"در حال دانلود تصویر متحرک: {img_url}")
            if download_image(img_url, headers, img_filepath):
                file_size = os.path.getsize(img_filepath)
                total_downloaded_bytes += file_size
                
                if upload_to_gdrive(service, folder_id, img_filepath):
                    os.remove(img_filepath)
                    history.add(clean_img_key)
                    history_changed = True

        # ثبت خود پست در تاریخچه تا فردا دوباره بررسی نشود
        history.add(clean_post_key)
        history_changed = True

    if history_changed:
        save_history(history)
        
    logging.info(f"پایان عملیات. کل حجم دانلود شده در این نوبت: {total_downloaded_bytes / (1024*1024):.2f} مگابایت.")

if __name__ == "__main__":
    setup_environment()
    download_and_process()
