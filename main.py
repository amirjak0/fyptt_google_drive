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

# تنظیمات لاگ
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

# کلمات کلیدی سربرگ‌ها و صفحات لیست که باید داخل آن‌ها اسکن شود
TAB_KEYWORDS = [
    'trending', 'hot', 'top', 'upcoming', 'porn-update', 
    'pornstars', 'new', 'studios', 'niche'
]

# دامنه‌ها و لینک‌های نامربوط (پرداخت، قوانین، شبکه‌های اجتماعی)
IGNORED_DOMAINS = [
    'segpay.com', 'epoch.com', 'psmhelp.com', 'mlfhelp.com',
    'paperstreetcash.com', 'auth.reptyle.com', 'ccbill.com',
    'verotel.com', 'probiller.com', 'google.com', 'twitter.com', 'facebook.com'
]

IGNORED_KEYWORDS = [
    'billingsupport', 'section2257', 'tos', 'privacy', 'refund',
    'faq', 'technicalsupport', 'content-removal', 'complaints',
    'dmca', 'anti-trafficking', 'cookie-policy', 'login', 'oauth',
    'join', 'signup', 'affiliate', 'amember'
]

def load_history():
    """خواندن لیست لینک‌هایی که قبلاً دانلود شده‌اند"""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_to_history(url):
    """ذخیره لینک در تاریخچه برای جلوگیری از دانلود تکراری"""
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{url}\n")

def get_gdrive_service(client_id, client_secret, refresh_token):
    """اتصال به Google Drive API"""
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
        media = MediaFileUpload(file_path, resumable=True)
        uploaded = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name'
        ).execute()
        logging.info(f"فایل در گوگل درایو ذخیره شد: {uploaded.get('name')}")
        return True
    except Exception as e:
        logging.error(f"خطا در آپلود به گوگل درایو: {e}")
        return False

def is_ignored_url(url):
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    if any(ig_dom in domain for ig_dom in IGNORED_DOMAINS):
        return True
    if any(ig_kw in path for ig_kw in IGNORED_KEYWORDS):
        return True
    return False

def is_tab_or_listing(url):
    """تشخیص اینکه آیا یک لینک، صفحه سربرگ یا دسته‌بندی است یا خیر"""
    clean_url = url.lower().split('?')[0].rstrip('/')
    return any(keyword in clean_url for keyword in TAB_KEYWORDS) or '/category/' in clean_url

def find_all_video_srcs(soup, base_url):
    """استخراج تمام سورس‌های ویدیو از یک صفحه HTML"""
    found_urls = set()
    
    # 1. تگ‌های ویدیویی
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

    # 2. تگ‌های Source مستقل
    for src_tag in soup.find_all('source'):
        src = src_tag.get('src') or src_tag.get('data-src')
        if src and not src.startswith('blob:'):
            found_urls.add(urljoin(base_url, src))

    # 3. متاتگ‌ها
    for meta in soup.find_all('meta'):
        prop = meta.get('property', '') or meta.get('name', '')
        if any(x in prop.lower() for x in ['og:video', 'twitter:player:stream', 'video:url']):
            content = meta.get('content')
            if content and not content.startswith('blob:'):
                found_urls.add(urljoin(base_url, content))

    # 4. لینک‌های مستقیم به پسوند ویدیو
    for a in soup.find_all('a', href=True):
        href = a['href']
        clean_href = href.split('?')[0].lower()
        if any(clean_href.endswith(ext) for ext in ['.mp4', '.m3u8', '.webm', '.mov', '.mkv']):
            found_urls.add(urljoin(base_url, href))

    # 5. استخراج از متغیرهای جاوااسکریپت و پلیرها
    for script in soup.find_all('script'):
        if script.string:
            matches = re.findall(r'https?://[^\s"\'<>]+\.(?:mp4|m3u8)(?:\?[^\s"\'<>]*)?', script.string)
            for m in matches:
                found_urls.add(m)
            rel_matches = re.findall(r'["\'](/[^"\']+\.(?:mp4|m3u8)(?:\?[^"\']*)?)["\']', script.string)
            for rm in rel_matches:
                found_urls.add(urljoin(base_url, rm))

    return list(found_urls)

def extract_media_from_post(post_url, headers):
    """استخراج ویدیوها از صفحه پست، آی‌فریم‌ها و لینک‌های عکس‌دار واسط"""
    all_videos = set()
    try:
        response = requests_cffi.get(post_url, headers=headers, impersonate="chrome", timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # ویدیوهای مستقیم صفحه
        all_videos.update(find_all_video_srcs(soup, post_url))
        
        # آی‌فریم‌ها
        for iframe in soup.find_all('iframe'):
            src = iframe.get('src') or iframe.get('data-src')
            if src:
                iframe_url = urljoin(post_url, src)
                if not is_ignored_url(iframe_url):
                    try:
                        iframe_resp = requests_cffi.get(iframe_url, headers=headers, impersonate="chrome", timeout=15)
                        if iframe_resp.status_code == 200:
                            iframe_soup = BeautifulSoup(iframe_resp.text, 'html.parser')
                            all_videos.update(find_all_video_srcs(iframe_soup, iframe_url))
                    except Exception:
                        pass

        # عکس‌های لینک‌دار به تریلرها یا صفحات واسط
        for a_tag in soup.find_all('a', href=True):
            if a_tag.find('img'):
                href = a_tag.get('href')
                full_href = urljoin(post_url, href)
                
                if is_ignored_url(full_href):
                    continue
                
                parsed_href = urlparse(full_href)
                parsed_post = urlparse(post_url)
                
                if parsed_href.netloc != parsed_post.netloc or 'trailer' in full_href.lower():
                    try:
                        ext_resp = requests_cffi.get(full_href, headers=headers, impersonate="chrome", timeout=15)
                        if ext_resp.status_code == 200:
                            ext_soup = BeautifulSoup(ext_resp.text, 'html.parser')
                            ext_videos = find_all_video_srcs(ext_soup, full_href)
                            if ext_videos:
                                all_videos.update(ext_videos)
                    except Exception:
                        pass

    except Exception as e:
        logging.error(f"خطا در خواندن پست {post_url}: {e}")
        
    return list(all_videos)

def scrape_all_tabs_and_posts(target_site_url, headers):
    """
    پیمایش تمام سربرگ‌ها (Trending, Hot, Top, Upcoming, New, Pornstars)
    و استخراج لیست کامل پست‌های ویدیویی از همه آن‌ها
    """
    base_domain = urlparse(target_site_url).netloc
    tabs_to_crawl = set([target_site_url])
    collected_post_links = []
    seen_posts = set()
    
    logging.info(f"در حال پیدا کردن سربرگ‌ها از آدرس {target_site_url}...")
    try:
        response = requests_cffi.get(target_site_url, headers=headers, impersonate="chrome", timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # استخراج تمام سربرگ‌های منو
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            full_url = urljoin(target_site_url, href)
            parsed = urlparse(full_url)
            
            if parsed.netloc == base_domain and not is_ignored_url(full_url):
                if is_tab_or_listing(full_url):
                    tabs_to_crawl.add(full_url)

    except Exception as e:
        logging.error(f"خطا در اسکن سربرگ‌ها: {e}")

    logging.info(f"تعداد {len(tabs_to_crawl)} سربرگ و بخش اصلی شناسایی شد:")
    for tab in tabs_to_crawl:
        logging.info(f"  📁 سربرگ: {tab}")

    # پیمایش درون تک‌تک سربرگ‌ها برای استخراج ویدیوهای آن‌ها
    for tab_url in tabs_to_crawl:
        logging.info(f"در حال اسکن پست‌های داخل سربرگ: {tab_url}")
        try:
            tab_resp = requests_cffi.get(tab_url, headers=headers, impersonate="chrome", timeout=15)
            if tab_resp.status_code != 200:
                continue
            tab_soup = BeautifulSoup(tab_resp.text, 'html.parser')
            
            # بررسی اگر خود این صفحه حاوی ویدیو مستقیم باشد
            if find_all_video_srcs(tab_soup, tab_url):
                if tab_url not in seen_posts:
                    seen_posts.add(tab_url)
                    collected_post_links.append(tab_url)
            
            tab_posts_count = 0
            for a in tab_soup.find_all('a', href=True):
                href = a['href'].strip()
                full_post_url = urljoin(tab_url, href)
                parsed_post = urlparse(full_post_url)
                
                # فیلتر کردن: فقط پست‌های ویدیویی (نه منوها یا صفحات تکراری)
                if parsed_post.netloc == base_domain and not is_ignored_url(full_post_url):
                    if not is_tab_or_listing(full_post_url) and full_post_url != tab_url and full_post_url != target_site_url:
                        if full_post_url not in seen_posts:
                            seen_posts.add(full_post_url)
                            collected_post_links.append(full_post_url)
                            tab_posts_count += 1

            logging.info(f"  ✅ تعداد {tab_posts_count} پست جدید از این سربرگ استخراج شد.")
        except Exception as e:
            logging.error(f"خطا در خواندن سربرگ {tab_url}: {e}")

    return collected_post_links

def download_video(video_url, download_dir):
    """دانلود با yt-dlp"""
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
            base = os.path.splitext(downloaded_filename)[0]
            for f in os.listdir(download_dir):
                if os.path.join(download_dir, f).startswith(base):
                    return os.path.join(download_dir, f)
    except Exception as e:
        logging.error(f"خطا در دانلود با yt-dlp برای {video_url}: {e}")
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
        logging.error("اتصال به گوگل درایو ناموفق بود.")
        sys.exit(1)

    history = load_history()
    logging.info(f"تعداد {len(history)} آیتم در تاریخچه قبلی یافت شد.")

    # استخراج تمام پست‌ها از تمامی سربرگ‌ها
    post_links = scrape_all_tabs_and_posts(target_site_url, DEFAULT_HEADERS)
    logging.info(f"🚀 مجموع کل پست‌های پیدا شده از تمام سربرگ‌ها: {len(post_links)}")

    if reverse_order:
        post_links.reverse()
        logging.info("ترتیب بررسی لینک‌ها معکوس شد.")

    for idx, post_url in enumerate(post_links, 1):
        if post_url in history:
            logging.info(f"[{idx}/{len(post_links)}] قبلاً کامل دانلود شده، رد شد: {post_url}")
            continue

        logging.info(f"[{idx}/{len(post_links)}] در حال پردازش پست: {post_url}")
        video_urls = extract_media_from_post(post_url, DEFAULT_HEADERS)

        if not video_urls:
            logging.warning(f"هیچ ویدیویی در {post_url} پیدا نشد.")
            save_to_history(post_url)
            continue

        logging.info(f"تعداد {len(video_urls)} ویدیو پیدا شد.")
        all_downloaded = True
        
        for v_idx, video_url in enumerate(video_urls, 1):
            if video_url in history:
                logging.info(f"  ({v_idx}/{len(video_urls)}) ویدیو قبلاً دانلود شده: {video_url}")
                continue

            logging.info(f"  ({v_idx}/{len(video_urls)}) شروع دانلود: {video_url}")
            downloaded_file = download_video(video_url, DOWNLOAD_DIR)

            if downloaded_file and os.path.exists(downloaded_file):
                logging.info(f"  فایل دانلود شد: {downloaded_file} -> شروع آپلود به گوگل درایو...")
                success = upload_to_gdrive(drive_service, downloaded_file, gdrive_folder_id)
                
                try:
                    os.remove(downloaded_file)
                except Exception:
                    pass

                if success:
                    save_to_history(video_url)
                    logging.info(f"  ویدیو با موفقیت ذخیره شد.")
                else:
                    all_downloaded = False
            else:
                logging.error(f"  دانلود ویدیو شکست خورد: {video_url}")
                all_downloaded = False

        if all_downloaded:
            save_to_history(post_url)

if __name__ == '__main__':
    main()
