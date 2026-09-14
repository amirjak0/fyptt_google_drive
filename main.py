import os
import sys
import re
import time
import logging
import mimetypes
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from bs4 import BeautifulSoup

try:
    from DrissionPage import ChromiumOptions, ChromiumPage
    from DrissionPage.common import By
except ImportError:
    logging.error("کتابخانه DrissionPage نصب نیست. لطفا در فایل اکشن گیت‌هاب آن را نصب کنید.")
    sys.exit(1)

import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# تنظیمات لاگ‌گیری
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

HISTORY_FILE = 'download_history.txt'
DOWNLOAD_DIR = 'downloads'
COOKIE_FILE = 'cookies.txt'

TAB_KEYWORDS = [
    'trending', 'hot', 'top', 'upcoming', 'porn-update', 
    'pornstars', 'new', 'studios', 'niche', 'category', 'tag'
]

# دامنه‌های تبلیغاتی پاپ‌آپ و سیستم‌های ریدایرکت (لیست کامل‌تر)
IGNORED_DOMAINS = [
    'segpay.com', 'epoch.com', 'psmhelp.com', 'mlfhelp.com', 'paperstreetcash.com', 
    'auth.reptyle.com', 'ccbill.com', 'verotel.com', 'probiller.com', 'google.com', 
    'twitter.com', 'facebook.com', 'chaturbate.com', 'bongacams.com', 'camsoda.com',
    'stripchat.com', 'livejasmin.com', 'jerkmate.com', 'myfreecams.com',
    'adultfriendfinder.com', 'cams.com', 'awempire.com', 'clickdealer.com',
    'adtng.com', 'awptg.com', 'trafficjunky.com', 'exoclick.com', 'realsrv.com',
    'porntraffic.com', 'eroadvertising.com', 'juicyads.com', 'plugrush.com',
    'onlyfans.com', 'fansly.com', 'sttrck.com', 'ads.sttrck.com', 'ads.namethatpornad.com',
    'tsyndicate.com', 'pxl-us.tsyndicate.com', 'cdn.tsyndicate.com', 'jssdk.tsyndicate.com',
    'out.php', 'ad.php', 'redirect'
]

IGNORED_KEYWORDS = [
    'billingsupport', 'section2257', 'tos', 'privacy', 'refund', 'faq', 
    'technicalsupport', 'content-removal', 'complaints', 'dmca', 'anti-trafficking', 
    'cookie-policy', 'login', 'oauth', 'join', 'signup', 'affiliate', 'amember', 
    'iamgettingoutnow', 'ad.php', 'out.php', 'adx-dir-d', 'link?aid='
]

def setup_browser():
    co = ChromiumOptions()
    co.set_browser_path('/usr/bin/google-chrome')
    co.headless(False) 
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-gpu')
    co.set_argument('--window-size=1920,1080')
    # افزونه‌های پیش‌فرض بلاک‌کننده‌ی پاپ‌آپ فعال می‌شوند
    co.set_argument('--disable-popup-blocking=false')
    co.set_argument('--disable-blink-features=AutomationControlled')
    co.set_user_agent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
    page = ChromiumPage(co)
    return page

def export_cookies_for_ytdlp(page, filename=COOKIE_FILE):
    try:
        cookies = page.cookies() 
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            for cookie in cookies:
                domain = cookie.get('domain', '')
                initial_dot = 'TRUE' if domain.startswith('.') else 'FALSE'
                path = cookie.get('path', '/')
                secure = 'TRUE' if cookie.get('secure') else 'FALSE'
                expiry = cookie.get('expiry') or cookie.get('expires')
                expires = str(int(float(expiry))) if expiry else '0'
                name = cookie.get('name', '')
                value = cookie.get('value', '')
                f.write(f"{domain}\t{initial_dot}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
    except Exception:
        pass

def get_page_and_sniff(page, url):
    """باز کردن صفحه، حل کلودفلر، کلیک روی دکمه‌های Play و شنود شبکه"""
    found_videos = set()
    try:
        # بستن پاپ‌آپ‌های تبلیغاتی که قبلا باز شده‌اند
        if len(page.tabs) > 1:
            page.close_tabs(page.tabs[1:])
            
        # فعال‌سازی رادار شبکه برای استخراج لینک‌های واقعی ویدیو (فقط مدیا)
        page.listen.start(['.mp4', '.m3u8', '.webm', '.ts'])
        page.get(url)
        time.sleep(3)
        
        if "Just a moment..." in page.html or "Cloudflare" in page.title or "Attention Required" in page.html:
            logging.info(f"🛡️ دیوار کلودفلر شناسایی شد. در حال حل چالش جاوااسکریپت...")
            try:
                cf_iframe = page.get_frame('@src^https://challenges.cloudflare.com')
                if cf_iframe:
                    cf_iframe.ele('xpath://input[@type="checkbox"] | //*[@id="challenge-stage"]', timeout=3).click()
            except Exception: pass
            time.sleep(12)
            
        page.scroll.to_bottom()
        time.sleep(2)
        
        # اسکرول به بالا و پیدا کردن دکمه‌های Play واقعی
        page.scroll.to_top()
        try:
            # دکمه‌های Play در پلیرها یا دکمه Unlock در سایت namethatpornad
            btns = page.eles('xpath://button[contains(@class, "play") or contains(@class, "vjs")] | //div[contains(@class, "play")] | //a[contains(text(), "UNLOCK")] | //a[contains(text(), "WATCH")]')
            for btn in btns[:2]:
                btn.click(by_js=True)
                time.sleep(3)
        except Exception: pass
        
        # اگر پاپ‌آپ تبلیغاتی باز شد (سایت‌های tSyndicate و...)، آنها را ببند و به تب اصلی برگرد
        if len(page.tabs) > 1:
            page.close_tabs(page.tabs[1:])
            page.to_tab(page.tabs[0])
            
        # استخراج لینک‌های ویدیو که در شبکه دزدیده شده‌اند
        for packet in page.listen.steps(timeout=4):
            req_url = getattr(packet, 'url', None) or getattr(packet.request, 'url', None)
            if req_url and not is_ignored_url(req_url):
                found_videos.add(req_url)
                
    except Exception as e:
        logging.error(f"خطا در پردازش صفحه {url}: {e}")
    finally:
        page.listen.stop()
        export_cookies_for_ytdlp(page)
        
    return page.html, list(found_videos)

def load_history():
    history = set()
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip(): history.add(line.strip())
        except Exception: pass
    return history

def save_to_history(url):
    clean_url = get_clean_url_key(url)
    try:
        with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{clean_url}\n")
    except Exception: pass

def get_clean_url_key(url):
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
    if not all([client_id, client_secret, refresh_token]): return None
    try:
        creds = Credentials(None, refresh_token=refresh_token, token_uri="https://oauth2.googleapis.com/token", client_id=client_id, client_secret=client_secret)
        if creds.expired or not creds.valid: creds.refresh(Request())
        return build('drive', 'v3', credentials=creds)
    except Exception: return None

def upload_to_gdrive(service, file_path, folder_id):
    try:
        file_name = os.path.basename(file_path)
        file_metadata = {'name': file_name, 'parents': [folder_id] if folder_id else []}
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type: mime_type = 'video/mp4' if file_path.endswith('.mp4') else 'application/octet-stream'
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        uploaded = service.files().create(body=file_metadata, media_body=media, fields='id, name').execute()
        logging.info(f"✅ آپلود موفق در درایو (ID: {uploaded.get('id')})")
        return True
    except Exception as e:
        logging.error(f"❌ خطا در آپلود: {e}")
        return False

def is_ignored_url(url):
    parsed = urlparse(url)
    domain, path = parsed.netloc.lower(), parsed.path.lower()
    if any(ig_dom in domain for ig_dom in IGNORED_DOMAINS): return True
    if any(ig_kw in path for ig_kw in IGNORED_KEYWORDS): return True
    return False

def is_tab_or_listing(url):
    clean_url = url.lower().split('?')[0].rstrip('/')
    return any(keyword in clean_url for keyword in TAB_KEYWORDS) or '/page/' in clean_url

def find_all_video_srcs(html, base_url):
    found_urls = set()
    soup = BeautifulSoup(html, 'html.parser')
    
    for v in soup.find_all(['video', 'audio']):
        for attr in ['src', 'data-src', 'data-video', 'data-url', 'data-orig']:
            val = v.get(attr)
            if val and not val.startswith('blob:'): 
                found_urls.add(urljoin(base_url, val))
        for src_tag in v.find_all('source'):
            val = src_tag.get('src')
            if val and not val.startswith('blob:'): 
                found_urls.add(urljoin(base_url, val))
                
    for a in soup.find_all('a', href=True):
        href = a['href']
        if any(ext in href.lower() for ext in ['.mp4', '.m3u8', '.webm']):
            found_urls.add(urljoin(base_url, href))
            
    pattern = r'(https?://[^\s"\'<>\[\]]+\.(?:mp4|m3u8|webm)(?:\?[^\s"\'<>\[\]]*)?)'
    for match in re.findall(pattern, html, re.IGNORECASE):
        found_urls.add(match.replace('\\/', '/'))

    return list(found_urls)


def extract_media_from_post(page, post_url):
    all_videos = set()
    try:
        html, sniffed_videos = get_page_and_sniff(page, post_url)
        all_videos.update(sniffed_videos)
        all_videos.update(find_all_video_srcs(html, post_url))
        
        soup = BeautifulSoup(html, 'html.parser')
        
        # استخراج دکمه‌ها و لینک‌های خارجی که به سایت اصلی ویدیو (مثل backroomcastingcouch) می‌روند
        external_links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True).lower()
            classes = " ".join(a.get('class', [])).lower()
            
            parsed_href = urlparse(href)
            if parsed_href.netloc and parsed_href.netloc != urlparse(post_url).netloc:
                if not is_ignored_url(href):
                    # اگر لینک دکمه، یا حاوی کلمات کلیدی تماشای ویدیو بود و به استودیوهای اصلی می‌رفت
                    if "backroomcastingcouch" in href.lower() or "watch" in text or "full" in text or "video" in text or "scene" in text or "unlock" in text or "play" in classes or "btn" in classes or "button" in classes:
                        external_links.append(href)
                        
        # بررسی سایت‌های مقصد اصلی (فقط آنهایی که مسدود نیستند)
        for ext_url in external_links[:2]:
            logging.info(f"🔍 دنبال کردن لینک سایت خارجی سازنده ویدیو: {ext_url}")
            ext_html, ext_sniffed = get_page_and_sniff(page, ext_url)
            all_videos.update(ext_sniffed)
            all_videos.update(find_all_video_srcs(ext_html, ext_url))
            
        # بررسی آی‌فریم‌ها
        for iframe in soup.find_all('iframe'):
            src = iframe.get('src') or iframe.get('data-src')
            if src and not src.startswith('javascript:') and not is_ignored_url(src):
                iframe_url = urljoin(post_url, src)
                iframe_html, i_sniffed = get_page_and_sniff(page, iframe_url)
                all_videos.update(i_sniffed)
                all_videos.update(find_all_video_srcs(iframe_html, iframe_url))

    except Exception as e:
        logging.error(f"خطا در اسکرپ مدیا: {e}")

    if not all_videos:
        if not (post_url.endswith('/?0') or '#' in post_url):
            all_videos.add(post_url)
            
    return [v for v in all_videos if not is_ignored_url(v)]


def scrape_all_tabs_and_posts(page, target_site_url, history):
    base_domain = urlparse(target_site_url).netloc
    tabs_to_crawl = {target_site_url}
    collected_post_links = []
    seen_posts = set()
    
    try:
        html, _ = get_page_and_sniff(page, target_site_url)
        soup = BeautifulSoup(html, 'html.parser')
        for a in soup.find_all('a', href=True):
            full_url = urljoin(target_site_url, a['href'].strip())
            if urlparse(full_url).netloc == base_domain and not is_ignored_url(full_url) and is_tab_or_listing(full_url):
                tabs_to_crawl.add(full_url)
    except Exception as e:
        logging.error(f"خطا در اسکن اولیه: {e}")
        return []

    for tab_url in list(tabs_to_crawl)[:15]:
        try:
            tab_html, _ = get_page_and_sniff(page, tab_url)
            tab_soup = BeautifulSoup(tab_html, 'html.parser')
            for a in tab_soup.find_all('a', href=True):
                full_post_url = urljoin(tab_url, a['href'].strip())
                
                if is_ignored_url(full_post_url): continue
                    
                parsed_post = urlparse(full_post_url)
                is_internal = parsed_post.netloc == base_domain
                
                if not is_tab_or_listing(full_post_url):
                    if not is_internal and parsed_post.path in ['', '/']:
                        continue
                        
                    clean_key = get_clean_url_key(full_post_url)
                    if clean_key not in history and full_post_url not in seen_posts:
                        seen_posts.add(full_post_url)
                        collected_post_links.append(full_post_url)
        except Exception: pass

    return collected_post_links

def download_video(video_url, download_dir, referer, user_agent):
    os.makedirs(download_dir, exist_ok=True)
    out_template = os.path.join(download_dir, '%(title).100s [%(id)s].%(ext)s')
    
    ydl_opts = {
        'outtmpl': out_template,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'quiet': False,
        'no_warnings': True,
        'noplaylist': True,
        'ignoreerrors': True,
        'cookiefile': COOKIE_FILE, 
        'http_headers': {
            'User-Agent': user_agent,
            'Referer': referer if referer else video_url
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            if not info: 
                logging.warning(f"ویدیویی در لینک پیدا نشد یا دسترسی مسدود است: {video_url}")
                return None
            downloaded_filename = ydl.prepare_filename(info)
            if os.path.exists(downloaded_filename): return downloaded_filename
            base = os.path.splitext(downloaded_filename)[0]
            for f in os.listdir(download_dir):
                if f.startswith(os.path.basename(base)): return os.path.join(download_dir, f)
    except Exception as e:
        err_msg = str(e)
        if '403' in err_msg or 'Forbidden' in err_msg:
             logging.warning(f"خطای دسترسی 403: اجازه دانلود از سرور صادر نشد -> {video_url}")
        else:
             logging.error(f"خطای دانلود: {e}")
    return None


def clean_downloads_folder():
    try:
        if os.path.exists(DOWNLOAD_DIR):
            for file in os.listdir(DOWNLOAD_DIR):
                path = os.path.join(DOWNLOAD_DIR, file)
                if os.path.isfile(path):
                    os.remove(path)
    except Exception as e:
        pass


def main():
    target_site_env = os.environ.get('TARGET_SITE_URL', '')
    target_sites = [u.strip() for u in re.split(r'[,\n]', target_site_env) if u.strip()]

    gdrive_client_id = os.environ.get('GDRIVE_CLIENT_ID')
    gdrive_client_secret = os.environ.get('GDRIVE_CLIENT_SECRET')
    gdrive_refresh_token = os.environ.get('GDRIVE_REFRESH_TOKEN')
    gdrive_folder_id = os.environ.get('GDRIVE_FOLDER_ID')

    if not target_sites: 
        logging.error("هیچ سایتی برای دانلود تنظیم نشده است.")
        sys.exit(1)

    drive_service = get_gdrive_service(gdrive_client_id, gdrive_client_secret, gdrive_refresh_token)
    if not drive_service:
        logging.error("شکست در اتصال به حساب گوگل درایو.")
        sys.exit(1)
        
    history = load_history()

    logging.info("در حال اجرای موتور مرورگر کروم مخفی برای دور زدن فایروال‌ها...")
    try:
        browser_page = setup_browser()
    except Exception as e:
        logging.error(f"اجرای مرورگر با مشکل مواجه شد: {e}")
        sys.exit(1)
        
    user_agent = browser_page.user_agent

    clean_downloads_folder()

    for target_site_url in target_sites:
        logging.info(f"\nشروع بررسی سایت: {target_site_url}")
        
        post_links = scrape_all_tabs_and_posts(browser_page, target_site_url, history)
        
        for idx, post_url in enumerate(post_links, 1):
            if get_clean_url_key(post_url) in history: continue

            logging.info(f"[{idx}/{len(post_links)}] پردازش: {post_url}")
            video_urls = extract_media_from_post(browser_page, post_url)

            all_downloaded = True
            dl_count = 0
            
            for video_url in video_urls:
                if get_clean_url_key(video_url) in history: continue

                downloaded_file = download_video(video_url, DOWNLOAD_DIR, post_url, user_agent)
                if downloaded_file and os.path.exists(downloaded_file):
                    if upload_to_gdrive(drive_service, downloaded_file, gdrive_folder_id):
                        save_to_history(video_url)
                        history.add(get_clean_url_key(video_url))
                        dl_count += 1
                    else: all_downloaded = False
                    
                    try: os.remove(downloaded_file)
                    except: pass
                else: all_downloaded = False

            if all_downloaded and dl_count > 0:
                save_to_history(post_url)
                history.add(get_clean_url_key(post_url))
                
            clean_downloads_folder()

    try:
        browser_page.quit()
    except: pass

if __name__ == '__main__':
    main()
