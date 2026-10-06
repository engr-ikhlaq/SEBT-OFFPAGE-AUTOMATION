import re
import time
import random
import sys
import signal
import sqlite3
from urllib.parse import urlparse, urljoin
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# =====================================================
# SHEETS
# =====================================================
SPREADSHEET_ID = "1FV2RumpsbUQ8ypoGl-77nzNYyXKeaJckKlB99TPzDFA"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_file("Credentials.json", scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_key(SPREADSHEET_ID).sheet1
service = build("sheets", "v4", credentials=creds)

today = datetime.now().strftime("%d-%b-%Y")

if not any(sheet.row_values(1)):
    sheet.update(
        range_name="A1:I1",
        values=[[
            "Date",
            "Keyword",
            "Query",
            "Domain",
            "URL",
            "Email",
            "Contact Page",
            "Snippet",
            "Score"
        ]]
    )
# =====================================================
# DATABASE (PERSISTENT DEDUP)
# =====================================================
conn = sqlite3.connect("seen_domains.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS seen (
    domain TEXT PRIMARY KEY
)
""")
conn.commit()


def is_seen(domain):
    cursor.execute(
        "SELECT 1 FROM seen WHERE domain=?",
        (domain,)
    )
    return cursor.fetchone() is not None


def mark_seen(domain):
    cursor.execute(
        "INSERT OR IGNORE INTO seen(domain) VALUES (?)",
        (domain,)
    )
    conn.commit()


# =====================================================
# CONFIG
# =====================================================
MAX_PAGES = 5

KEYWORDS = [
    "pept",
    "snapchat cloaking agency",
    "pinterest cloaking agency",
    "ads cloaking pinterest",
    "pinterest cloaking ads",
    "youtube cloaking agency",
    "ads cloaking snapchat",
]

QUERIES = [

    # ==============================
    # CORE GUEST POST INTENT
    # ==============================
    '[KEYWORD] "write for us"',
    '[KEYWORD] "guest post"',
    '[KEYWORD] "guest posting"',
    '[KEYWORD] "guest article"',
    '[KEYWORD] "submit article"',
    '[KEYWORD] "submit a guest post"',
    '[KEYWORD] "become a contributor"',
    '[KEYWORD] "contribute to our site"',
    '[KEYWORD] "submit post"',
    '[KEYWORD] "write for me"',

    # ==============================
    # EDITORIAL / GUIDELINES PAGES
    # ==============================
    '[KEYWORD] "contributor guidelines"',
    '[KEYWORD] "editorial guidelines"',
    '[KEYWORD] "submission guidelines"',
    '[KEYWORD] "writing guidelines"',
    '[KEYWORD] "guest post guidelines"',

    # ==============================
    # HIGH INTENT ACTION QUERIES
    # ==============================
    '[KEYWORD] "pitch us"',
    '[KEYWORD] "send your pitch"',
    '[KEYWORD] "submit your pitch"',
    '[KEYWORD] "article submission"',
    '[KEYWORD] "send article idea"',
    '[KEYWORD] "suggest a post"',

    # ==============================
    # GOOGLE OPERATORS (HIGH VALUE)
    # ==============================
    'inurl:write-for-us [KEYWORD]',
    'inurl:guest-post [KEYWORD]',
    'inurl:contribute [KEYWORD]',
    'inurl:submit-article [KEYWORD]',
    'inurl:blog [KEYWORD] "write for us"',
    'intitle:"write for us" [KEYWORD]',
    'intitle:"guest post" [KEYWORD]',

    # ==============================
    # UK + GEO TARGETING
    # ==============================
    '[KEYWORD] "write for us" UK',
    '[KEYWORD] "guest post" UK',
    '[KEYWORD] "submit article" UK',
    '[KEYWORD] "contribute" UK',

    # ==============================
    # BUSINESS / MONETIZATION SITES
    # ==============================
    '[KEYWORD] "sponsored post"',
    '[KEYWORD] "advertise with us"',
    '[KEYWORD] "partnership opportunities"',
    '[KEYWORD] "collaborate with us"',
    '[KEYWORD] "brand collaboration"',

    # ==============================
    # BLOG DISCOVERY (HIDDEN GEMS)
    # ==============================
    '[KEYWORD] blog "write for us"',
    '[KEYWORD] blog "guest post"',
    '[KEYWORD] blog "contribute"',
    '[KEYWORD] blog "submit article"',

]

INTENT_PATTERNS = {
    "guest_post_core": [
        "write for us",
        "guest post",
        "guest posting",
        "guest article",
        "submit article",
        "submit post",
        "submit guest post"
    ],

    "contributor": [
        "become a contributor",
        "contribute to our site",
        "contributor guidelines",
        "writing guidelines"
    ],

    "editorial": [
        "editorial guidelines",
        "submission guidelines",
        "guest post guidelines"
    ],

    "pitch": [
        "pitch us",
        "send your pitch",
        "submit your pitch",
        "send article idea",
        "suggest a post"
    ],

    "monetization": [
        "sponsored post",
        "advertise with us",
        "brand collaboration",
        "partnership opportunities"
    ]
}
# =====================================================
# DRIVER
# =====================================================
options = Options()
options.add_argument("--start-maximized")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument("--lang=en-GB")

driver = webdriver.Chrome(options=options)


# =====================================================
# UTILITIES
# =====================================================

def is_valid_url(url):
    url = url.lower()

    # =========================
    # 1. BLOCK SOCIAL PLATFORMS
    # =========================
    blocked_domains = [
        "facebook.com", "instagram.com", "twitter.com", "x.com",
        "linkedin.com", "youtube.com", "pinterest.com",
        "reddit.com", "tiktok.com", "quora.com",
        "medium.com", "github.com", "stackoverflow.com",
        "play.google.com", "apps.apple.com", "upwork.com", "fiver.com","freelancer.com"
    ]

    # =========================
    # 2. BLOCK WEBSITE BUILDERS / SAAS
    # =========================
    blocked_builders = [
        "wix.com", "squarespace.com", "wordpress.com",
        "blogspot.com", "weebly.com", "shopify.com",
        "webflow.io"
    ]

    # =========================
    # 3. BLOCK FILE TYPES (PDF/DOC/etc)
    # =========================
    blocked_extensions = [
        ".pdf", ".doc", ".docx", ".ppt", ".pptx",
        ".xls", ".xlsx", ".zip", ".rar", ".csv", ".app"
    ]

    # =========================
    # 4. BLOCK LOGIN / DASHBOARD PAGES
    # =========================
    blocked_keywords = [
        "login", "signup", "register", "account",
        "dashboard", "admin", "wp-admin"
    ]

    # =========================
    # APPLY CHECKS
    # =========================
    if any(d in url for d in blocked_domains):
        return False

    if any(b in url for b in blocked_builders):
        return False

    if any(url.endswith(ext) for ext in blocked_extensions):
        return False

    if any(k in url for k in blocked_keywords):
        return False

    # must be real http(s)
    if not url.startswith("http"):
        return False

    return True



def go_next_page():
    try:
        btn = driver.find_element(By.ID, "pnnext")
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(random.uniform(3, 6))
        return True
    except:
        return False


def delay(a=2, b=5):
    time.sleep(random.uniform(a, b))


def human_scroll(driver):
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
        delay(1, 2)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        delay(2, 4)
    except:
        pass


def sanitize(t):
    return str(t).replace("\n", " ").replace("\r", " ") if t else ""


def get_domain(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except:
        return ""


def human_type(element, text):
    for i, char in enumerate(text):

        element.send_keys(char)

        if i % random.randint(3, 7) == 0:
            time.sleep(random.uniform(0.2, 0.6))  # thinking pause
        else:
            time.sleep(random.uniform(0.05, 0.15))


# =====================================================
# EMAIL EXTRACTION (FIXED)
# =====================================================
EMAIL_REGEX = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"


def extract_emails(text):
    if not text:
        return []
    return list(set(re.findall(EMAIL_REGEX, text)))


def extract_mailto(driver):
    emails = []
    try:
        for m in driver.find_elements(By.CSS_SELECTOR, "a[href^='mailto:']"):
            href = m.get_attribute("href")
            if href:
                emails.append(href.replace("mailto:", "").split("?")[0])
    except:
        pass
    return emails


def get_page_emails(driver):
    human_scroll(driver)

    emails = set()
    emails.update(extract_emails(driver.page_source))
    try:
        emails.update(extract_emails(driver.find_element(By.TAG_NAME, "body").text))
    except:
        pass
    emails.update(extract_mailto(driver))

    return list(emails)

def intent_score(text):
    text = text.lower()
    score = 0

    for group, words in INTENT_PATTERNS.items():

        for w in words:
            if w in text:
                if group == "guest_post_core":
                    score += 6
                elif group == "contributor":
                    score += 5
                elif group == "editorial":
                    score += 4
                elif group == "pitch":
                    score += 3
                elif group == "monetization":
                    score += 2

    return min(score, 25)


# =====================================================
# INTELLIGENCE CRAWLER (LIGHTWEIGHT)
# =====================================================
def crawl_site(base_url):
    session = requests.Session()

    pages_to_check = [
        "/contact", "/contact-us", "/about", "/team",
        "/editor", "/write-for-us", "/guest-post",
        "/contributors", "/advertise", "/contact - us.html",
        " /contact/","index.php/contact", "/company/contact",
        "/get-in-touch","/support/contact","/help/contact",
        "/contact-us/"
    ]

    found_emails = []
    contact_link = ""

    def fetch(url):
        try:
            r = session.get(url, timeout=8)
            if r.status_code == 200:
                return BeautifulSoup(r.text, "html.parser")
        except:
            pass
        return None

    # 1. homepage check
    soup = fetch(base_url)
    if soup:
        found_emails += extract_emails(soup.get_text(" "))

    # 2. smart predefined paths (CODE 2 STRENGTH)
    for path in pages_to_check:
        full_url = urljoin(base_url, path)
        soup = fetch(full_url)

        if not soup:
            continue

        emails = extract_emails(soup.get_text(" "))

        if emails:
            return emails, full_url

        if soup.find("form"):
            contact_link = full_url

    if found_emails:
        return found_emails, base_url

    return [], contact_link


# =====================================================
# SCORE ENGINE
# =====================================================
def score(keyword, title, h1, meta, body, domain, emails, contact):

    total = 0

    keyword = keyword.lower()
    title = title.lower()
    h1 = h1.lower()
    meta = meta.lower()
    body = body.lower()

    # ===========================
    # 1. RELEVANCY (40)
    # ===========================

    # keyword match (basic)
    if keyword in title:
        total += 12

    if keyword in h1:
        total += 10

    if keyword in meta:
        total += 6

    # partial relevance (more realistic)
    words = keyword.split()
    for w in words:
        if w in body:
            total += 2

    # frequency boost (capped)
    total += min(body.count(keyword), 3) * 3

    total = min(total, 40)

    # ===========================
    # 2. INTENT SCORE (25)  🔥 NEW UPGRADE
    # ===========================

    total += intent_score(body)

    total = min(total, 65)

    # ===========================
    # 3. DOMAIN QUALITY (15)
    # ===========================

    if domain.endswith(".edu"):
        total += 15
    elif domain.endswith(".gov"):
        total += 15
    elif domain.endswith(".org"):
        total += 10
    elif domain.endswith(".co.uk"):
        total += 8
    elif domain.endswith(".com"):
        total += 5

    total = min(total, 80)

    # ===========================
    # 4. CONTACT QUALITY (10)
    # ===========================

    if emails:
        total += 6
    if contact:
        total += 4

    total = min(total, 90)

    # ===========================
    # 5. SEO QUALITY (10)
    # ===========================

    if len(title) > 10:
        total += 2
    if len(meta) > 20:
        total += 2
    if len(h1) > 5:
        total += 2
    if body.count("http") > 2:
        total += 2
    if "https" in body:
        total += 2

    return min(total, 100)


# =====================================================
# GOOGLE
# =====================================================
def search_google(query):
    driver.get("https://www.google.co.uk/?hl=en-GB&gl=uk&pws=0")
    delay()

    box = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.NAME, "q"))
    )

    delay(1, 2)
    human_type(box, query)
    delay(1, 2)
    box.send_keys(Keys.RETURN)
    time.sleep(random.uniform(1.5, 3.5))


# =====================================================
# PARSER
# =====================================================
def parse_serp():
    time.sleep(random.uniform(2, 5))
    results = []

    blocks = driver.find_elements(By.XPATH,
                                  "//div[contains(@class,'MjjYud')] | //div[contains(@class,'g')]"
                                  )

    for b in blocks:
        try:
            a = b.find_element(By.CSS_SELECTOR,"a[href]")
            url = a.get_attribute("href")
            snippet = b.text

            if url and "google" not in url:
                results.append({"url": url, "snippet": snippet})
        except:
            continue

    return results

# =====================================================
# COLLECT URLS FROM GOOGLE (PAGES 1-5)
# =====================================================
def collect_serp_urls():
    all_results = []
    seen_domains = set()

    page = 1

    while page <= MAX_PAGES:

        print(f"\n📄 Collecting Google Page {page}")

        time.sleep(random.uniform(2, 4))

        results = parse_serp()

        if not results:
            print("No results found on this page.")
        else:
            for r in results:

                url = r.get("url")
                snippet = r.get("snippet", "")

                if not url:
                    continue

                if not is_valid_url(url):
                    continue

                domain = get_domain(url)

                if domain in seen_domains:
                    continue

                seen_domains.add(domain)

                all_results.append({
                    "url": url,
                    "domain": domain,
                    "snippet": snippet
                })

        if page == MAX_PAGES:
            break

        if not go_next_page():
            print("No more Google pages.")
            break

        page += 1

    print(f"\nCollected {len(all_results)} unique domains.")

    return all_results

# =====================================================
# BUFFER SYSTEM (CRASH SAFE)
# =====================================================
buffer = []
last_flush = time.time()


def flush():
    global buffer
    if buffer:
        sheet.append_rows(buffer)
        print(f"🔥 Flushed {len(buffer)} rows")
        buffer = []

def safe_add(row):
    global last_flush

    buffer.append(row)

    if len(buffer) >= 8 or time.time() - last_flush > 60:
        flush()
        last_flush = time.time()


def shutdown(*args):
    print("\n🛑 SAFE STOP - flushing buffer")

    try:
        flush()
    except Exception as e:
        print(f"Flush error: {e}")

    try:
        conn.close()
    except:
        pass

    try:
        driver.quit()
    except:
        pass

    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# =====================================================
# MAIN ENGINE
# =====================================================

for keyword in KEYWORDS:

    for q in QUERIES:

        query = q.replace("[KEYWORD]", keyword)

        print("\nSEARCH:", query)

        search_google(query)

        # STEP 1
        serp_results = collect_serp_urls()

        # STEP 2
        for item in serp_results:

            url = item["url"]
            domain = item["domain"]
            snippet = item["snippet"]

            # Already processed?
            if is_seen(domain):
                print(f"⏭ Skipping existing domain: {domain}")
                continue

            # =========================
            # DEFAULT VALUES (IMPORTANT)
            # =========================
            title = ""
            body = ""
            h1 = ""
            meta = ""
            emails = []
            contact = ""

            try:
                driver.get(url)

                title = driver.title or ""

                try:
                    body = driver.find_element(By.TAG_NAME, "body").text
                except:
                    body = ""

                try:
                    h1 = driver.find_element(By.TAG_NAME, "h1").text
                except:
                    h1 = ""

                try:
                    meta = driver.find_element(
                        By.CSS_SELECTOR,
                        'meta[name="description"]'
                    ).get_attribute("content") or ""
                except:
                    meta = ""

                # email extraction
                emails = get_page_emails(driver)

                # fallback crawling if no emails
                if not emails:
                    emails, contact = crawl_site(url)

            except Exception as e:
                # full fallback if page fails completely
                emails, contact = crawl_site(url)

            # =========================
            # SCORE
            # =========================
            sc = score(
                keyword,
                title,
                h1,
                meta,
                body,
                domain,
                emails,
                contact
            )

            # =========================
            # ROW BUILD
            # =========================
            row = [
                today,
                keyword,
                query,
                domain,
                sanitize(url),
                sanitize(", ".join(emails)),
                sanitize(contact),
                sanitize(snippet),
                sc,
            ]

            safe_add(row)
            mark_seen(domain)

            print("✔", domain)

shutdown()
