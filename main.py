import re
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# -------------------------
# CONFIG / LIMITS
# -------------------------

MAX_PAGES_PER_DOMAIN = 20   # hard cap for how many pages we fetch per domain
MAX_DEPTH = 1               # 0 = base only, 1 = follow one link hop
REQUEST_TIMEOUT = 6         # seconds per request

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# URL patterns that look like contact/staff/directory
URL_PATTERNS_CONTACT = [
    "contact", "contact-us", "contactus"
]

URL_PATTERNS_DIRECTORY = [
    "staff", "team", "people", "directory", "personnel",
    "faculty", "our-people", "our-team", "administration",
    "management", "executives", "leadership"
]

# -------------------------
# HELPER FUNCTIONS
# -------------------------

def clean_domain_to_url(domain: str) -> str:
    if not isinstance(domain, str):
        return ""
    d = domain.strip()
    if not d:
        return ""
    # Remove protocol
    d = re.sub(r"^https?://", "", d, flags=re.IGNORECASE)
    d = d.strip("/")
    return "https://" + d

def same_domain(base_url: str, candidate_url: str) -> bool:
    try:
        base_netloc = urlparse(base_url).netloc
        cand_netloc = urlparse(candidate_url).netloc
        return base_netloc and cand_netloc and base_netloc == cand_netloc
    except Exception:
        return False

def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "text" in ctype:
            return resp.text
    except Exception:
        return None
    return None

def get_visible_text_blocks(html: str):
    """
    Return a list of text blocks (lines) from the HTML,
    stripped of scripts/styles etc.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.split("\n")]
    return [l for l in lines if l]

def extract_emails(text: str):
    # basic email pattern
    pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    return re.findall(pattern, text)

def extract_phone_numbers(text: str):
    # very loose phone pattern; you'll get some false positives
    pattern = r"(\+?\d[\d\s\-\(\)]{6,}\d)"
    return re.findall(pattern, text)

def url_matches_any(url: str, patterns):
    u = url.lower()
    return any(p in u for p in patterns)

def get_links_from_html(base_url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(base_url, href)
        full = full.split("#")[0]
        links.add(full)
    return links

def extract_contacts_from_page(base_url: str, url: str, html: str):
    """
    Extract emails/phones and some surrounding context from a single page.

    For each text block, we:
    - grab all emails and phones
    - create a row for each email, attaching the phones seen in that same block
    - if there are phones but no emails, create phone-only rows

    No filtering by job title – Title_guess is just a hint.
    """
    blocks = get_visible_text_blocks(html)
    contacts = []

    for block in blocks:
        emails = list(set(extract_emails(block)))
        phones = list(set(extract_phone_numbers(block)))

        # Nothing interesting in this block
        if not emails and not phones:
            continue

        # crude attempt to find "name-ish" text: a couple of capitalised words
        name_match = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b", block)
        name_guess = name_match.group(1).strip() if name_match else ""

        # some role words we care about (just for Title_guess, NOT filtering)
        role_words = [
            "director", "manager", "head", "chief",
            "officer", "coordinator", "administrator",
            "lecturer", "professor", "dean"
        ]
        title_guess = ""
        lower_block = block.lower()
        for rw in role_words:
            if rw in lower_block:
                title_guess = rw
                break

        phones_joined = "; ".join(p.strip() for p in phones) if phones else ""

        # If there are emails, create one row per email, with any phones in the same block
        if emails:
            for e in emails:
                contacts.append({
                    "Email": e.strip(),
                    "Phone": phones_joined,
                    "Name_guess": name_guess,
                    "Title_guess": title_guess,
                    "Page_URL": url,
                    "Text_snippet": block[:300]
                })
        else:
            # No emails, but phones present → phone-only contact row(s)
            for p in phones:
                contacts.append({
                    "Email": "",
                    "Phone": p.strip(),
                    "Name_guess": name_guess,
                    "Title_guess": title_guess,
                    "Page_URL": url,
                    "Text_snippet": block[:300]
                })

    return contacts

def crawl_domain(domain: str):
    """
    Crawl a single domain in a limited, targeted way,
    and return a list of contact dicts.
    """
    base_url = clean_domain_to_url(domain)
    if not base_url:
        return []

    # Seed URLs
    seed_paths = [
        "/", "",
        "/contact",
        "/contact-us",
        "/about",
        "/about-us",
    ]
    queue = []
    visited = set()
    pages_processed = 0

    for path in seed_paths:
        full = urljoin(base_url, path)
        queue.append((full, 0))

    contacts = []

    while queue and pages_processed < MAX_PAGES_PER_DOMAIN:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        if not same_domain(base_url, url):
            continue

        html = fetch_url(url)
        if not html:
            continue

        pages_processed += 1

        # Extract contacts from this page
        page_contacts = extract_contacts_from_page(base_url, url, html)
        contacts.extend(page_contacts)

        # Stop further crawling if at depth limit
        if depth >= MAX_DEPTH:
            continue

        # Get links and selectively add those that look like staff/directory/contact
        links = get_links_from_html(base_url, html)
        for link in links:
            if link in visited:
                continue
            if not same_domain(base_url, link):
                continue
            if url_matches_any(link, URL_PATTERNS_CONTACT) or url_matches_any(link, URL_PATTERNS_DIRECTORY):
                queue.append((link, depth + 1))

    return contacts


# -------------------------
# STREAMLIT APP
# -------------------------

st.set_page_config(page_title="Private Contact Scraper", layout="wide")

st.title("🔎 Private Contact Scraper (Domains → Contacts)")
st.write(
    """
    This tool is for **your private use only**.  
    It takes a list of domains, visits a limited set of pages (contact, staff, directory, about),
    and pulls **publicly visible** contact information (emails, phones + some context).
    
    ⚠️ Please ensure you use this in line with applicable laws, GDPR, and each site's terms.
    """
)

upload = st.file_uploader("Upload a CSV with at least a Domain/Website column", type=["csv"])

domain_col_name = None
company_col_name = None

if upload is not None:
    df = pd.read_csv(upload)
    st.write("Preview of uploaded data:")
    st.dataframe(df.head())

    cols = list(df.columns)

    # Auto-detect domain column
    def detect_domain_col(cols):
        dom_keywords = ["domain", "website", "url", "site"]
        best = None
        best_score = 0
        for c in cols:
            h = c.lower().strip()
            score = sum(1 for kw in dom_keywords if kw in h)
            if score > best_score:
                best_score = score
                best = c
        return best

    # Auto-detect company/organisation column
    def detect_company_col(cols):
        org_keywords = [
            "company", "organisation", "organization",
            "school", "university", "college", "name"
        ]
        best = None
        best_score = 0
        for c in cols:
            h = c.lower().strip()
            score = sum(1 for kw in org_keywords if kw in h)
            if score > best_score:
                best_score = score
                best = c
        return best

    suggested_domain_col = detect_domain_col(cols)
    suggested_company_col = detect_company_col(cols)

    st.write("Detected columns (you can override):")
    domain_col_name = st.selectbox(
        "Domain / Website column",
        options=cols,
        index=cols.index(suggested_domain_col) if suggested_domain_col in cols else 0
    )
    company_col_name = st.selectbox(
        "Company / Organisation column (optional)",
        options=["<none>"] + cols,
        index=(cols.index(suggested_company_col) + 1) if suggested_company_col in cols else 0
    )

    max_domains = st.number_input(
        "Max domains to process (for safety)",
        min_value=1, max_value=500, value=50, step=1
    )

    if st.button("Run scraper"):
        if domain_col_name is None:
            st.error("Please select a domain/website column.")
        else:
            run_df = df.copy()
            # Trim to max_domains
            run_df = run_df.iloc[:max_domains].copy()

            all_results = []

            for idx, row in run_df.iterrows():
                domain = str(row[domain_col_name]).strip()
                if not domain:
                    continue
                company = ""
                if company_col_name and company_col_name != "<none>":
                    company = str(row[company_col_name])

                st.write(f"🔄 [{idx}] Processing domain: {domain} ({company})")
                st.write("… crawling (limited)…")

                contacts = crawl_domain(domain)

                if not contacts:
                    all_results.append({
                        "Input_Company": company,
                        "Input_Domain": domain,
                        "Name_guess": "",
                        "Title_guess": "",
                        "Email": "",
                        "Phone": "",
                        "Page_URL": "",
                        "Text_snippet": ""
                    })
                    continue

                for c in contacts:
                    all_results.append({
                        "Input_Company": company,
                        "Input_Domain": domain,
                        "Name_guess": c.get("Name_guess", ""),
                        "Title_guess": c.get("Title_guess", ""),
                        "Email": c.get("Email", ""),
                        "Phone": c.get("Phone", ""),
                        "Page_URL": c.get("Page_URL", ""),
                        "Text_snippet": c.get("Text_snippet", "")
                    })

            if not all_results:
                st.warning("No contacts found for the processed domains.")
            else:
                result_df = pd.DataFrame(all_results)
                st.success(f"Found {len(result_df)} contact rows.")
                st.dataframe(result_df.head(50))

                csv = result_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "📥 Download results as CSV",
                    data=csv,
                    file_name="scraped_contacts.csv",
                    mime="text/csv"
                )

else:
    st.info("Upload a CSV to begin.")
