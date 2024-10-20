from flask import Flask, request, render_template, redirect, url_for
import threading
import os
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import time
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from pymongo import MongoClient
import logging

app = Flask(__name__)

load_dotenv()

mongo_client = MongoClient(os.getenv('MONGO_URI'))
db = mongo_client['house_listings']
collection = db['house_images']
blocked_collection = db['blocked_urls']  

driver = None
scraper_thread = None
scraper_active = False
autostart_thread = None
autostart_active = False

blocked_urls = {doc['url'] for doc in blocked_collection.find()}

logging.basicConfig(filename='autostart.log', level=logging.INFO, 
                    format='%(asctime)s - %(message)s')

def fetch_house_images_selenium_sync(house_link):
    global driver
    try:
        if driver is None: 
            chrome_options = Options()
            chrome_options.add_argument("--window-size=1280x720")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")

            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
        
        driver.execute_script("window.open('');")
        driver.switch_to.window(driver.window_handles[-1])
        driver.get(house_link)

        wait = WebDriverWait(driver, 10)

        try:
            image_gallery = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, 'sc-1acce1b7-10')))
            image_gallery.click()
            time.sleep(0.5)
        except Exception:
            pass

        images = set()
        prev_image_count = 0
        max_retries = 10
        retries = 0

        while retries < max_retries:
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            images_divs = soup.find_all('div', class_='lg-item')

            new_images = set()
            for div in images_divs:
                img_tag = div.find('img', class_='lg-object lg-image')
                if img_tag:
                    image_src = img_tag.get('src') or img_tag.get('data-src')
                    if image_src and image_src not in blocked_urls:
                        new_images.add(image_src)
            
            images.update(new_images)
            
            current_image_count = len(images_divs)

            if current_image_count == prev_image_count:
                retries += 1
            else:
                retries = 0

            prev_image_count = current_image_count

            try:
                next_button = driver.find_element(By.CLASS_NAME, 'lg-next')
                if next_button:
                    next_button.click()
                    time.sleep(0.5)
                else:
                    break
            except Exception:
                break
        time.sleep(5)
        driver.close()
        driver.switch_to.window(driver.window_handles[0])  

        return list(images)

    except Exception:
        return []

def close_driver():
    global driver
    if driver:
        driver.quit()
        driver = None

def fetch_houses(page=1, max_page=None):
    url = f"https://home.ss.ge/en/real-estate/l/Flat/For-Sale?cityIdList=95&currencyId=1&advancedSearch=%7B%22individualEntityOnly%22%3Atrue%7D&page={page}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'
    }

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 403:
            print(f"Access forbidden on page {page}")
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        house_list = soup.find_all('div', class_='sc-8fa2c16a-0')

        if not house_list:
            print(f"No more houses found on page {page}.")
            return []

        fetched_houses = []
        for house in house_list:
            link_tag = house.find('a', href=True)
            house_link = f"https://home.ss.ge{link_tag['href']}" if link_tag else None

            house_id = link_tag['href'].split('-')[-1] if link_tag and 'href' in link_tag.attrs else None

            if collection.find_one({'houseId': house_id}):
                print(f"House ID {house_id} already exists. Skipping...")
                continue

            photos = fetch_house_images_selenium_sync(house_link) if house_link else []

            if not photos:
                print(f"House ID {house_id} has no images. Skipping...")
                continue

            unique_photos = [photo for photo in photos if not collection.find_one({'photos': photo})]

            if not unique_photos:
                print(f"House ID {house_id} has no unique images. Skipping...")
                continue

            house_data = {
                'houseId': house_id,
                'photos': unique_photos,
            }

            collection.insert_one(house_data)
            fetched_houses.append(house_data)

        print(f"Fetched {len(fetched_houses)} houses from page {page}.")

        if not max_page and house_list:
            next_page_houses = fetch_houses(page=page + 1, max_page=max_page)
            fetched_houses.extend(next_page_houses)

        return fetched_houses

    except Exception as e:
        print(f"Error fetching houses on page {page}: {e}")
        return []




def autostart_fetch():
    global autostart_active
    while autostart_active:
        try:
            print("Autostart fetching first two pages...")

            fetched_houses_page1 = fetch_houses(page=1, max_page=1) 
            print(f"Fetched houses from page 1: {fetched_houses_page1}")

            fetched_houses_page2 = fetch_houses(page=2, max_page=2) 
            print(f"Fetched houses from page 2: {fetched_houses_page2}")
            
            fetched_houses = fetched_houses_page1 + fetched_houses_page2

            for house in fetched_houses:
                print(f"Fetched house ID: {house.get('houseId')}")
                for photo in house.get('photos', []):
                    print(f"Fetched image link: {photo}")

            logging.info(f"Autostart fetched {len(fetched_houses)} houses from the first 2 pages.")
        except Exception as e:
            print(f"Autostart error: {e}")
            logging.error(f"Autostart error: {e}")
        time.sleep(300)  



@app.route('/')
def index():
    with open('autostart.log', 'r') as f:
        log_data = f.read()
    return render_template('index.html', blocked_urls=blocked_urls, autostart_log=log_data)

@app.route('/add_url', methods=['POST'])
def add_url():
    url = request.form.get('url')
    if url and url not in blocked_urls:  
        blocked_urls.add(url)
        blocked_collection.insert_one({'url': url})
    return redirect(url_for('index'))

@app.route('/start_scraper', methods=['POST'])
def start_scraper():
    global scraper_thread, scraper_active
    if not scraper_active:
        scraper_active = True
        scraper_thread = threading.Thread(target=fetch_houses, args=(1, None), daemon=True)
        scraper_thread.start()
    return redirect(url_for('index'))


@app.route('/stop_scraper', methods=['POST'])
def stop_scraper():
    global scraper_active, autostart_active
    scraper_active = False
    close_driver() 
    return redirect(url_for('index'))

def start_autostart_thread():
    global autostart_thread, autostart_active
    autostart_active = True
    autostart_thread = threading.Thread(target=autostart_fetch, daemon=True)
    autostart_thread.start()

if __name__ == '__main__':
    start_autostart_thread() 
    app.run(debug=True)
