import os
import sys
import re
import logging
import mimetypes
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from bs4 import BeautifulSoup

# استفاده از curl_cffi برای دور زدن کلودفلر و فایروال‌ها
try:
    from curl_cffi import requests as requests_cffi
except ImportError:
    import requests as requests_cffi

import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# تنظیمات لاگ‌گیری
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

# کلمات کلیدی دسته‌بندی‌ها و سربرگ‌ها
TAB_KEYWORDS = [
    'trending', 'hot', 'top', 'upcoming', 'porn-update', 
    'pornstars', 'new', 'studios', 'niche', 'category', 'tag'
]

# دامنه‌ها و لینک‌های نامربوط که باید فیلتر شوند
IGNORED_DOMAINS = [
    'segpay.com', 'epoch.com', 'psmhelp.com', 'mlfhelp.com',
    'paperstreetcash.com', 'auth.reptyle.com', 'ccbill.com',
    'verotel.com', 'probiller.com', 'google.com', 'twitter.com', 'facebook.com'
]

IGNORED_KEYWORDS = [
    'billingsupport', 'section2257', 'tos', 'privacy', 'refund',
    'faq', 'technicalsupport', 'content-removal', 'complaints',
    'dmca', 'anti-trafficking', 'cookie-policy', 'login', 'oauth',
    'join', 'signup', 'affiliate', 'amember', 'iamgettingoutnow'
]

def load_history():
    """خواندن لیست لینک‌هایی که قبلاً دانلود شده‌اند"""
    history = set()
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        history.add(line)
            logging.info(f"تعداد {len(history)} آیتم در تاریخچه قبلی یافت شد.")
        except Exception as e:
            logging.error(f"خطا در خواندن تاریخچه: {e}")
    return history

def save_to_history(url):
    """ذخیره لینک تمیز در فایل تاریخچه"""
    clean_url = get_clean_url_key(url)
    try:
        with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{clean_url}\n")
    except Exception as e:
        logging.error(f"خطا در ذخیره تاریخچه برای {url}: {e}")

def get_clean_url_key(url):
    """حذف پارامترهای موقت مانند توکن، امضا و زمان انقضا برای جلوگیری از تکرار"""
    try:
        parsed = urlparse(url)
        query_params = parse_qsl(parsed.query)
        ignored_keys = {'token', 'expires', 'signature', 'sig', 'hash', 'auth', 'time', 't', 'session', 'session_id'}
        clean_params = [(k, v) for k, v in query_params if k.lower() not in ignored_keys]
        clean_query = urlencode(clean_params)
        return urlunparse(parsed._replace(query=clean_query, fragment=''))
    except Exception:
        return url

def get_gdrive_service(client_id, client_secret, refresh_token):
    """اتصال به Google Drive API"""
    if not all([client_id, client_secret, refresh_token]):
        logging.error("کلیدهای اتصال به گوگل درایو یافت نشدند.")
        return None
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
        logging.error(f"خطا در اتصال به Google Drive: {e}")
        return None

def upload_to_gdrive(service, file_path, folder_id):
    """آپلود فایل دانلود شده به گوگل درایو"""
    try:
        file_name = os.path.basename(file_path)
        file_metadata = {
            'name': file_name,
            'parents': [folder_id] if folder_id else []
        }
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = 'video/mp4' if file_path.endswith('.mp4') else 'application/octet-stream'

        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        uploaded = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name'
        ).execute()
        logging.info(f"✅ فایل با موفقیت در گوگل درایو ذخیره شد (ID: {uploaded.get('id')})")
        return True
    except Exception as e:
        logging.error(f"❌ خطا در آپلود به گوگل درایو: {e}")
        return False

def is_ignored_url(url):
    """بررسی لینک‌های نامربوط یا تبلیغاتی"""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    if any(ig_dom in domain for ig_dom in IGNORED_DOMAINS):
        return True
    if any(ig_kw in path for ig_kw in IGNORED_KEYWORDS):
        return True
    return False

def is_tab_or_listing(url):
    """تشخیص صفحات دسته‌بندی و لیست‌ها"""
    clean_url = url.lower().split('?')[0].rstrip('/')
    return any(keyword in clean_url for keyword in TAB_KEYWORDS) or '/page/' in clean_url

def find_all_video_srcs(soup, base_url):
    """استخراج تمام سورس‌های ویدیویی (MP4, M3U8, WEBM) از HTML"""
    found_urls = set()
    
    # 1. تگ‌های video و audio
    for v in soup.find_all(['video', 'audio']):
        for attr in ['src', 'data-src', 'data-video', 'data-url', 'data-orig']:
            src = v.get(attr)
            if src and not src.startswith('blob:'):
                found_urls.add(urljoin(base_url, src))
        for src_tag in v.find_all('source'):
            for attr in ['src', 'data-src']:
                src = src_tag.get(attr)
                if src and not src.startswith('blob:'):
                    found_urls.add(urljoin(base_url, src))

    # 2. تگ‌های source مستقل
    for src_tag in soup.find_all('source'):
        src = src_tag.get('src') or src_tag.get('data-src')
        if src and not src.startswith('blob:'):
            found_urls.add(urljoin(base_url, src))

    # 3. متاتگ‌ها
    for meta in soup.find_all('meta'):
        prop = meta.get('property', '') or meta.get('name', '')
        if any(x in prop.lower() for x in ['og:video', 'og:video:url', 'og:video:secure_url', 'twitter:player:stream', 'video:url']):
            content = meta.get('content')
            if content and not content.startswith('blob:'):
                found_urls.add(urljoin(base_url, content))

    # 4. لینک‌های مستقیم با پسوند ویدیو
    for a in soup.find_all('a', href=True):
        href = a['href']
        clean_href = href.split('?')[0].lower()
        if any(clean_href.endswith(ext) for ext in ['.mp4', '.m3u8', '.webm', '.mov', '.mkv']):
            found_urls.add(urljoin(base_url, href))

    # 5. استخراج از کدهای جاوااسکریپت و پلیرها
    for script in soup.find_all('script'):
        if script.string:
            matches = re.findall(r'https?://[^\s"\'<>]+\.(?:mp4|m3u8|webm)(?:\?[^\s"\'<>]*)?', script.string)
            for m in matches:
                found_urls.add(m)
            rel_matches = re.findall(r'["\'](/[^"\']+\.(?:mp4|m3u8|webm)(?:\?[^"\']*)?)["\']', script.string)
            for rm in rel_matches:
                found_urls.add(urljoin(base_url, rm))

    return list(found_urls)

def extract_media_from_post(post_url, headers):
    """استخراج ویدیو از پست، آی‌فریم‌ها یا برگرداندن خود لینک صفحه برای yt-dlp"""
    all_videos = set()
    try:
        response = requests_cffi.get(post_url, headers=headers, impersonate="chrome", timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # استخراج مستقیم از صفحه
            all_videos.update(find_all_video_srcs(soup, post_url))
            
            # بررسی آی‌فریم‌ها
            for iframe in soup.find_all('iframe'):
                src = iframe.get('src') or iframe.get('data-src')
                if src and not src.startswith('javascript:') and not is_ignored_url(src):
                    iframe_url = urljoin(post_url, src)
                    try:
                        iframe_resp = requests_cffi.get(iframe_url, headers=headers, impersonate="chrome", timeout=15)
                        if iframe_resp.status_code == 200:
                            iframe_soup = BeautifulSoup(iframe_resp.text, 'html.parser')
                            all_videos.update(find_all_video_srcs(iframe_soup, iframe_url))
                    except Exception:
                        pass
    except Exception as e:
        logging.error(f"خطا در اسکرپ مدیا از پست {post_url}: {e}")

    # Fallback حیاتی: اگر هیچ لینک مستقیمی پیدا نشد، خود لینک پست را ارسال می‌کنیم تا yt-dlp تست کند
    if not all_videos:
        all_videos.add(post_url)
        
    return list(all_videos)

def scrape_all_tabs_and_posts(target_site_url, headers, history):
    """پیمایش تمام سربرگ‌ها و صفحات برای جمع‌آوری لینک پست‌ها"""
    base_domain = urlparse(target_site_url).netloc
    tabs_to_crawl = {target_site_url}
    collected_post_links = []
    seen_posts = set()
    
    logging.info(f"در حال پیدا کردن سربرگ‌ها و پست‌ها از: {target_site_url}")
    try:
        response = requests_cffi.get(target_site_url, headers=headers, impersonate="chrome", timeout=20)
        if response.status_code != 200:
            logging.error(f"سایت هدف با کد وضعیت {response.status_code} پاسخ داد: {target_site_url}")
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            full_url = urljoin(target_site_url, href)
            parsed = urlparse(full_url)
            
            if parsed.netloc == base_domain and not is_ignored_url(full_url):
                if is_tab_or_listing(full_url):
                    tabs_to_crawl.add(full_url)

    except Exception as e:
        logging.error(f"خطا در اسکن اولیه سایت {target_site_url}: {e}")
        return []

    logging.info(f"تعداد {len(tabs_to_crawl)} سربرگ و بخش اصلی شناسایی شد.")

    for tab_url in tabs_to_crawl:
        logging.info(f"در حال اسکن پست‌های سربرگ: {tab_url}")
        try:
            tab_resp = requests_cffi.get(tab_url, headers=headers, impersonate="chrome", timeout=15)
            if tab_resp.status_code != 200:
                continue
            tab_soup = BeautifulSoup(tab_resp.text, 'html.parser')
            
            tab_posts_count = 0
            for a in tab_soup.find_all('a', href=True):
                href = a['href'].strip()
                full_post_url = urljoin(tab_url, href)
                parsed_post = urlparse(full_post_url)
                
                if parsed_post.netloc == base_domain and not is_ignored_url(full_post_url):
                    if not is_tab_or_listing(full_post_url) and full_post_url != tab_url and full_post_url != target_site_url:
                        clean_key = get_clean_url_key(full_post_url)
                        if clean_key not in history and full_post_url not in seen_posts:
                            seen_posts.add(full_post_url)
                            collected_post_links.append(full_post_url)
                            tab_posts_count += 1

            logging.info(f"  ✅ تعداد {tab_posts_count} پست جدید از سربرگ {tab_url} استخراج شد.")
        except Exception as e:
            logging.error(f"خطا در خواندن سربرگ {tab_url}: {e}")

    return collected_post_links

def download_video(video_url, download_dir, referer=None):
    """دانلود با yt-dlp به همراه هدرهای اختصاصی ضد مسدودسازی"""
    os.makedirs(download_dir, exist_ok=True)
    out_template = os.path.join(download_dir, '%(title).100s [%(id)s].%(ext)s')
    
    ydl_opts = {
        'outtmpl': out_template,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'quiet': False,
        'no_warnings': True,
        'noplaylist': True,
        'ignoreerrors': True,
        'http_headers': {
            'User-Agent': DEFAULT_HEADERS['User-Agent'],
            'Referer': referer if referer else video_url
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            if not info:
                return None
            downloaded_filename = ydl.prepare_filename(info)
            if os.path.exists(downloaded_filename):
                return downloaded_filename
            
            # جستجوی فایل در صورت تبدیل پسوند توسط ffmpeg
            base = os.path.splitext(downloaded_filename)[0]
            for f in os.listdir(download_dir):
                full_f = os.path.join(download_dir, f)
                if full_f.startswith(base) and os.path.isfile(full_f):
                    return full_f
    except Exception as e:
        logging.error(f"خطا در دانلود با yt-dlp برای {video_url}: {e}")
    return None

def main():
    target_site_env = os.environ.get('TARGET_SITE_URL', '')
    target_sites = [u.strip() for u in re.split(r'[,\n]', target_site_env) if u.strip()]

    gdrive_client_id = os.environ.get('GDRIVE_CLIENT_ID')
    gdrive_client_secret = os.environ.get('GDRIVE_CLIENT_SECRET')
    gdrive_refresh_token = os.environ.get('GDRIVE_REFRESH_TOKEN')
    gdrive_folder_id = os.environ.get('GDRIVE_FOLDER_ID')
    reverse_order = os.environ.get('REVERSE_VIDEO_ORDER', 'False').lower() in ('true', '1', 'yes')

    if not target_sites:
        logging.error("متغیر TARGET_SITE_URL تنظیم نشده است!")
        sys.exit(1)

    logging.info("در حال اتصال به Google Drive...")
    drive_service = get_gdrive_service(gdrive_client_id, gdrive_client_secret, gdrive_refresh_token)
    if not drive_service:
        logging.error("اتصال به گوگل درایو ناموفق بود.")
        sys.exit(1)

    history = load_history()

    for target_site_url in target_sites:
        logging.info(f"\n{'='*60}\nشروع پردازش سایت: {target_site_url}\n{'='*60}")
        
        post_links = scrape_all_tabs_and_posts(target_site_url, DEFAULT_HEADERS, history)
        logging.info(f"🚀 مجموع کل پست‌های واجد شرایط: {len(post_links)}")

        if reverse_order:
            post_links.reverse()
            logging.info("ترتیب بررسی لینک‌ها معکوس شد.")

        for idx, post_url in enumerate(post_links, 1):
            clean_post_key = get_clean_url_key(post_url)
            if clean_post_key in history:
                logging.info(f"[{idx}/{len(post_links)}] قبلاً کامل دانلود شده: {post_url}")
                continue

            logging.info(f"[{idx}/{len(post_links)}] در حال پردازش پست: {post_url}")
            video_urls = extract_media_from_post(post_url, DEFAULT_HEADERS)

            all_downloaded = True
            download_count = 0
            
            for v_idx, video_url in enumerate(video_urls, 1):
                clean_video_key = get_clean_url_key(video_url)
                if clean_video_key in history:
                    logging.info(f"  ({v_idx}/{len(video_urls)}) ویدیو قبلاً ثبت شده: {video_url}")
                    continue

                logging.info(f"  ({v_idx}/{len(video_urls)}) شروع دانلود: {video_url}")
                downloaded_file = download_video(video_url, DOWNLOAD_DIR, referer=post_url)

                if downloaded_file and os.path.exists(downloaded_file):
                    logging.info(f"  فایل دانلود شد: {downloaded_file} -> شروع آپلود به گوگل درایو...")
                    success = upload_to_gdrive(drive_service, downloaded_file, gdrive_folder_id)
                    
                    # پاک کردن فایل دانلود شده برای پر نشدن دیسک گیت‌هاب رانر
                    try:
                        os.remove(downloaded_file)
                    except Exception:
                        pass

                    if success:
                        save_to_history(video_url)
                        history.add(clean_video_key)
                        download_count += 1
                    else:
                        all_downloaded = False
                else:
                    all_downloaded = False

            if all_downloaded and download_count > 0:
                save_to_history(post_url)
                history.add(clean_post_key)
                logging.info(f"  پست و تمام ویدیوهای آن با موفقیت ذخیره شدند.")

if __name__ == '__main__':
    main()
