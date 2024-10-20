from flask import Flask, request, render_template, redirect, url_for
import threading
import os
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from pymongo import MongoClient
import logging
from selenium.webdriver.firefox.service import Service as FirefoxService
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.firefox.options import Options as FirefoxOptions

app = Flask(__name__)

load_dotenv()

mongo_client = MongoClient(os.getenv('MONGO_URI'))
db = mongo_client['house_listings']
collection = db['house_images']
blocked_collection = db['blocked_urls']  

driver = None
scraper_thread = None
scraper_active = False

blocked_urls = {doc['url'] for doc in blocked_collection.find()}

logging.basicConfig(filename='scraper.log', level=logging.INFO, 
                    format='%(asctime)s - %(message)s')

buy_urls = [
    "https://home.ss.ge/en/real-estate/l/Flat/For-Sale?cityIdList=95&currencyId=1&advancedSearch=%7B%22individualEntityOnly%22%3Atrue%7D",
    "https://home.ss.ge/en/real-estate/l/Private-House/For-Sale?cityIdList=95&currencyId=1&advancedSearch=%7B%22individualEntityOnly%22%3Atrue%7D",
    "https://home.ss.ge/en/real-estate/l/Hotel/For-Sale?cityIdList=95&currencyId=1&advancedSearch=%7B%22individualEntityOnly%22%3Atrue%7D",
    "https://home.ss.ge/en/real-estate/l/Commercial-Real-Estate/For-Sale?cityIdList=95&currencyId=1&advancedSearch=%7B%22individualEntityOnly%22%3Atrue%7D"
]

rent_urls = [
    "https://home.ss.ge/en/real-estate/l/Flat/For-Rent?cityIdList=95&currencyId=1&advancedSearch=%7B%22individualEntityOnly%22%3Atrue%7D",
    "https://home.ss.ge/en/real-estate/l/Private-House/For-Rent?cityIdList=95&currencyId=1&advancedSearch=%7B%22individualEntityOnly%22%3Atrue%7D",
    "https://home.ss.ge/en/real-estate/l/Hotel/For-Rent?cityIdList=95&currencyId=1&advancedSearch=%7B%22individualEntityOnly%22%3Atrue%7D",
    "https://home.ss.ge/en/real-estate/l/Commercial-Real-Estate/For-Rent?cityIdList=95&currencyId=1&advancedSearch=%7B%22individualEntityOnly%22%3Atrue%7D"
]

def fetch_house_images_selenium_sync(house_link):
    global driver
    try:
        if driver is None: 
            firefox_options = FirefoxOptions()
            firefox_options.add_argument("--window-size=1280x720")
            firefox_options.add_argument("--no-remote") 
            firefox_options.add_argument("--no-sandbox")
            firefox_options.add_argument("--disable-dev-shm-usage")
            firefox_options.add_argument("--disable-software-rasterizer")

            service = FirefoxService(GeckoDriverManager().install())
            driver = webdriver.Firefox(service=service, options=firefox_options)
        
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

        try:
            total_images_text = driver.find_element(By.CLASS_NAME, 'sc-1acce1b7-11').text 
            total_images = int(total_images_text.split('/')[1])  
        except Exception:
            total_images = 10  

        images = set()

        for _ in range(total_images):
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            images_divs = soup.find_all('div', class_='lg-item')

            for div in images_divs:
                img_tag = div.find('img', class_='lg-object lg-image')
                if img_tag:
                    image_src = img_tag.get('src') or img_tag.get('data-src')
                    if image_src and image_src not in blocked_urls:
                        images.add(image_src)
            
            try:
                next_button = driver.find_element(By.CLASS_NAME, 'lg-next')
                if next_button:
                    next_button.click()
                    time.sleep(0.5)
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

def fetch_houses_from_url(url, page=1):
    url_with_page = f"{url}&page={page}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'
    }
    
    try:
        response = requests.get(url_with_page, headers=headers)
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

        print(f"Fetched {len(fetched_houses)} houses from page {page} for URL: {url}.")

        return fetched_houses

    except Exception as e:
        print(f"Error fetching houses on page {page}: {e}")
        return []
def fetch_houses_from_url(url, page=1):
    url_with_page = f"{url}&page={page}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'
    }
    
    try:
        response = requests.get(url_with_page, headers=headers)
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

        print(f"Fetched {len(fetched_houses)} houses from page {page} for URL: {url}.")

        return fetched_houses

    except Exception as e:
        print(f"Error fetching houses on page {page}: {e}")
        return []

@app.route('/')
def index():
    with open('scraper.log', 'r') as f:
        log_data = f.read()
    return render_template('index.html', blocked_urls=blocked_urls, log_data=log_data)

@app.route('/add_url', methods=['POST'])
def add_url():
    url = request.form.get('url')
    if url and url not in blocked_urls:  
        blocked_urls.add(url)
        blocked_collection.insert_one({'url': url})
    return redirect(url_for('index'))

@app.route('/fetch_new_houses', methods=['POST'])
def fetch_new_houses():
    global scraper_thread, scraper_active
    if not scraper_active:
        scraper_active = True
        
        all_urls = buy_urls + rent_urls
        
        def scrape_all_urls():
            for url in all_urls:
                fetch_houses_from_url(url, 1)  
            scraper_active = False 
        
        scraper_thread = threading.Thread(target=scrape_all_urls, daemon=True)
        scraper_thread.start()
    
    return redirect(url_for('index'))

@app.route('/start_selenium', methods=['POST'])
def start_selenium():
    global scraper_thread, scraper_active
    if not scraper_active:
        scraper_active = True
        
        all_urls = buy_urls + rent_urls
        
        def scrape_all_pages():
            for url in all_urls:
                page = 1
                while True:
                    fetched_houses = fetch_houses_from_url(url, page)
                    if not fetched_houses:  
                        break
                    page += 1 
            scraper_active = False 
        
        scraper_thread = threading.Thread(target=scrape_all_pages, daemon=True)
        scraper_thread.start()
    
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True)