import os
import sys
import time
import logging
import requests
from urllib.parse import urljoin, urlparse
from DrissionPage import ChromiumOptions, ChromiumPage
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# تنظیمات لاگ‌گیری
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

HISTORY_FILE = 'image_download_history.txt'
DOWNLOAD_DIR = 'image_downloads'

# تنظیمات سایت
TARGET_URL = "https://www.pornpics.com/recent/"
MAX_ALBUMS_PER_RUN = 10

def setup_browser():
    co = ChromiumOptions()
    co.set_browser_path('/usr/bin/google-chrome')
    co.headless(False) 
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-gpu')
    co.set_argument('--window-size=1920,1080')
    co.set_argument('--disable-blink-features=AutomationControlled')
    co.set_user_agent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
    page = ChromiumPage(co)
    return page

def bypass_cloudflare(page):
    """بررسی و دور زدن سیستم امنیتی کلودفلر"""
    if "Just a moment..." in page.html or "Cloudflare" in page.title or "Attention Required" in page.html:
        logging.info("سیستم امنیتی کلودفلر تشخیص داده شد. در حال عبور...")
        try:
            cf_iframe = page.get_frame('@src^https://challenges.cloudflare.com')
            if cf_iframe:
                cf_iframe.ele('xpath://input[@type="checkbox"] | //*[@id="challenge-stage"]', timeout=3).click()
        except Exception: pass
        time.sleep(12)

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
    try:
        with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{url}\n")
    except Exception: pass

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
        media = MediaFileUpload(file_path, resumable=True)
        uploaded = service.files().create(body=file_metadata, media_body=media, fields='id, name').execute()
        logging.info(f"آپلود موفق عکس: {file_name}")
        return True
    except Exception as e:
        logging.error(f"خطا در آپلود عکس: {e}")
        return False

def get_album_name(album_url):
    """اسم آلبوم را از انتهای مسیر URL می‌گیرد."""
    try:
        parts = [p for p in urlparse(album_url).path.split('/') if p]
        if parts:
            return parts[-1]
    except Exception:
        pass
    return f"album_{int(time.time())}"

def download_image(img_url, download_dir, session, custom_name=None):
    try:
        response = session.get(img_url, stream=True, timeout=15)
        if response.status_code == 200:
            if custom_name:
                file_name = custom_name
            else:
                file_name = img_url.split('/')[-1].split('?')[0]
                if not file_name or not file_name.lower().endswith(('.jpg', '.jpeg', '.png')):
                    ext = '.png' if img_url.split('?')[0].lower().endswith('.png') else '.jpg'
                    file_name = f"image_{int(time.time())}{ext}"
            
            file_path = os.path.join(download_dir, file_name)
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(1024):
                    f.write(chunk)
            return file_path
    except Exception as e:
        logging.error(f"خطا در دانلود عکس {img_url}: {e}")
    return None

def main():
    gdrive_client_id = os.environ.get('GDRIVE_CLIENT_ID')
    gdrive_client_secret = os.environ.get('GDRIVE_CLIENT_SECRET')
    gdrive_refresh_token = os.environ.get('GDRIVE_REFRESH_TOKEN')
    gdrive_folder_id = os.environ.get('GDRIVE_FOLDER_ID')

    drive_service = get_gdrive_service(gdrive_client_id, gdrive_client_secret, gdrive_refresh_token)
    if not drive_service:
        logging.error("شکست در اتصال به گوگل درایو.")
        sys.exit(1)

    history = load_history()
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    logging.info("در حال اجرای مرورگر...")
    page = setup_browser()
    
    try:
        page.get(TARGET_URL)
        time.sleep(8) # زمان بیشتر برای لود کامل صفحه
        bypass_cloudflare(page)
        
        # روش جدید و مطمئن‌تر برای پیدا کردن آلبوم‌ها
        all_links = page.eles('tag:a')
        new_albums = []
        
        for link in all_links:
            href = link.attr('href')
            if href and '/galleries/' in href:
                full_link = urljoin(TARGET_URL, href)
                if full_link not in history and full_link not in new_albums:
                    new_albums.append(full_link)
                    
        logging.info(f"تعداد {len(new_albums)} آلبوم جدید پیدا شد.")
        
        albums_to_process = new_albums[:MAX_ALBUMS_PER_RUN]
        
        session = requests.Session()
        session.headers.update({'User-Agent': page.user_agent})
        for cookie in page.cookies():
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))

        for idx, album_url in enumerate(albums_to_process, 1):
            logging.info(f"[{idx}/{len(albums_to_process)}] در حال پردازش آلبوم: {album_url}")
            page.get(album_url)
            time.sleep(5)
            bypass_cloudflare(page)
            
            page.scroll.to_bottom()
            time.sleep(3)
            
            # روش جدید برای پیدا کردن عکس‌های باکیفیت
            all_img_links = page.eles('tag:a')
            image_urls = []
            
            for link in all_img_links:
                href = link.attr('href')
                if href and (href.lower().endswith('.jpg') or href.lower().endswith('.png')):
                    full_img_url = urljoin(album_url, href)
                    if full_img_url not in image_urls:
                        image_urls.append(full_img_url)
                    
            logging.info(f"تعداد {len(image_urls)} عکس در این آلبوم پیدا شد.")
            
            album_name = get_album_name(album_url)
            all_success = True
            for i, img_url in enumerate(image_urls, 1):
                ext = '.png' if img_url.split('?')[0].lower().endswith('.png') else '.jpg'
                new_name = f"{album_name}_{i:03d}{ext}"
                downloaded_file = download_image(img_url, DOWNLOAD_DIR, session, custom_name=new_name)
                if downloaded_file:
                    success = upload_to_gdrive(drive_service, downloaded_file, gdrive_folder_id)
                    if not success:
                        all_success = False
                    os.remove(downloaded_file)
                else:
                    all_success = False
                    
            if all_success and len(image_urls) > 0:
                save_to_history(album_url)
                history.add(album_url)
                logging.info(f"آلبوم با موفقیت کامل شد و در تاریخچه ثبت شد.")

    except Exception as e:
        logging.error(f"خطای کلی در اجرای اسکریپت: {e}")
    finally:
        page.quit()

if __name__ == '__main__':
    main()
