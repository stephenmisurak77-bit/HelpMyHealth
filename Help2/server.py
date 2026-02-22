from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests
import re
from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import Query
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, unquote
from nhs_slugs import NHS_SLUG_MAP

app = FastAPI(title="Help My Health")

# Serve frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return FileResponse("static/index.html")


# -----------------------------
# Models
# -----------------------------
class ChatRequest(BaseModel):
    message: str


class EvidenceSource(BaseModel):
    id: str
    title: str
    publisher: str
    year: int
    type: str
    url: str
    reliability: str
    rationale: str
    sample_size: Optional[int] = None
    snippet: Optional[str] = None


class AssistantResponse(BaseModel):
    triage: Optional[Dict[str, Any]] = None
    steps: List[Dict[str, Any]]
    seekCareNow: List[str]
    prevention: List[str] = []
    related: List[str] = []
    sources: List[EvidenceSource]


# -----------------------------
# PubMed / E-utilities helpers
# -----------------------------
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL = "med-research-chat"
EMAIL = None  # optionally set to your contact email string
API_KEY = None  # optionally set your NCBI api key string


def infer_sample_size(abstract_text: str) -> Optional[int]:
    """
    Heuristic extraction of sample size from abstract text.
    Looks for:
    - n=240 / N = 1,234
    - 240 participants/patients/subjects
    - enrolled 300 / randomized 150
    """
    if not abstract_text:
        return None
    text = re.sub(r"\s+", " ", abstract_text)

    patterns = [
        r"\b[nN]\s*=\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\b",
        r"\b([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\s+(participants|patients|subjects|adults|children)\b",
        r"\b(enrolled|included|randomized)\s+([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\b",
    ]

    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if not m:
            continue
        # pattern 3 has number in group 2
        num_str = m.group(1) if m.lastindex and m.lastindex >= 1 else None
        if p.endswith(r")\b") and "enrolled" in p:
            num_str = m.group(2)

        if not num_str:
            continue
        num_str = num_str.replace(",", "")
        try:
            n = int(num_str)
            if 0 < n < 10_000_000:
                return n
        except ValueError:
            pass
    return None


def reliability_from_year_and_n(year: Optional[int], n: Optional[int]) -> (str, str):
    """
    Reliability mainly based on:
    - recency (year)
    - sample size (n, inferred)

    Score:
      recency: <=5y:3, 6-10:2, 11-20:1, >20:0
      size:    >=1000:3, 200-999:2, 50-199:1, <50/None:0
    """
    now = datetime.now().year
    age = 999 if not year else max(0, now - year)

    # recency points
    if age <= 5:
        recency = 3
    elif age <= 10:
        recency = 2
    elif age <= 20:
        recency = 1
    else:
        recency = 0

    # sample size points
    if not n:
        size = 0
    elif n >= 1000:
        size = 3
    elif n >= 200:
        size = 2
    elif n >= 50:
        size = 1
    else:
        size = 0

    total = recency + size

    if total >= 5:
        rel = "High"
    elif total >= 3:
        rel = "Moderate"
    else:
        rel = "Low"

    rationale = f"Scored mainly by year ({year if year else 'unknown'}) and sample size (n={n if n else 'unknown'})."
    return rel, rationale


def pubmed_esearch(term: str, retmax: int = 6) -> List[str]:
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": str(retmax),
        "sort": "relevance",
        "tool": TOOL,
    }
    if EMAIL:
        params["email"] = EMAIL
    if API_KEY:
        params["api_key"] = API_KEY

    r = requests.get(EUTILS + "esearch.fcgi", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("esearchresult", {}).get("idlist", []) or []


def pubmed_efetch(pmids: List[str]) -> List[Dict[str, Any]]:
    if not pmids:
        return []
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": TOOL,
    }
    if EMAIL:
        params["email"] = EMAIL
    if API_KEY:
        params["api_key"] = API_KEY

    r = requests.get(EUTILS + "efetch.fcgi", params=params, timeout=25)
    r.raise_for_status()
    xml = r.text

    # Minimal XML parsing without extra deps:
    # We’ll extract PMIDs, titles, journal titles, year, and abstract using regex.
    # For hackathons this is fine; for production, use an XML parser.
    articles = re.split(r"<PubmedArticle>", xml)[1:]
    out = []

    for chunk in articles:
        # PMID
        pmid_m = re.search(r"<PMID[^>]*>(\d+)</PMID>", chunk)
        pmid = pmid_m.group(1) if pmid_m else ""

        # Title (very rough)
        title_m = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", chunk, flags=re.DOTALL)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else f"PubMed article {pmid}"

        # Journal
        journal_m = re.search(r"<Title>(.*?)</Title>", chunk, flags=re.DOTALL)
        journal = re.sub(r"<[^>]+>", "", journal_m.group(1)).strip() if journal_m else "PubMed"

        # Year
        year_m = re.search(r"<PubDate>.*?<Year>(\d{4})</Year>.*?</PubDate>", chunk, flags=re.DOTALL)
        if not year_m:
            year_m = re.search(r"<MedlineDate>.*?((19|20)\d{2}).*?</MedlineDate>", chunk, flags=re.DOTALL)
        year = int(year_m.group(1)) if year_m else datetime.now().year

        # Abstract
        abs_chunks = re.findall(r"<AbstractText[^>]*>(.*?)</AbstractText>", chunk, flags=re.DOTALL)
        abstract = " ".join(re.sub(r"<[^>]+>", "", a).strip() for a in abs_chunks).strip()

        out.append({
            "pmid": pmid,
            "title": title,
            "journal": journal,
            "year": year,
            "abstract": abstract,
        })

    return out

def symptom_steps(user_text: str, sources: List[EvidenceSource] = None):
    t = user_text.lower()

    # Bloody nose / epistaxis
    if (
        "bloody nose" in t
        or "nosebleed" in t
        or "nose bleed" in t
        or "bleeding from my nose" in t
        or "bleeding from the nose" in t
        or "epistaxis" in t
        ):
        return [
            {
                "title": "Stop the bleeding (first aid)",
                "actions": [
                    "Sit upright and lean forward slightly (don’t lean back).",
                    "Pinch the soft part of your nose (just below the bony bridge) for 10 minutes continuously.",
                    "Breathe through your mouth; avoid talking/checking the bleeding during the 10 minutes."
                ],
                "why": "Leaning forward prevents blood from going down your throat and steady pressure allows clotting."
            },
            {
                "title": "After it stops",
                "actions": [
                    "Avoid blowing your nose, heavy lifting, or vigorous exercise for 24 hours.",
                    "If your nose feels dry, consider gentle saline spray or humidification.",
                    "If bleeding restarts, repeat 10 minutes of pressure (up to 2–3 rounds)."
                ],
                "why": "Clots can re-open easily; dryness and irritation increase re-bleeding risk."
            },
        ], [
            "Bleeding lasts longer than 20 minutes despite pressure",
            "Heavy bleeding, dizziness, fainting, or trouble breathing",
            "Nosebleed after significant injury or you suspect a broken nose",
            "You take blood thinners (warfarin, apixaban, rivaroxaban, etc.) and bleeding is hard to stop",
            "Frequent recurrent nosebleeds"
        ]

    # Add more symptoms as you expand:
    if "burn" in t:
        return [
            {
                "title": "Cool the burn",
                "actions": [
                    "Cool under cool running water for 20 minutes (not ice).",
                    "Remove rings/jewelry near the area if possible.",
                    "Cover loosely with a clean non-stick dressing."
                ],
                "why": "Cooling reduces tissue damage; ice can worsen injury."
            }
        ], [
            "Large burn, facial/genital burn, chemical/electrical burn",
            "Blistering with severe pain, or signs of infection"
        ]

    # Dynamic fallback: if we have sources, use the top one
    if sources and len(sources) > 0:
        top = sources[0]
        return [
            {
                "title": f"Information from {top.publisher}",
                "actions": [top.snippet[:200] + "..."] if top.snippet else ["Review the linked source for guidance."],
                "why": f"Based on top search result: {top.title}"
            }
        ], ["If symptoms worsen", "High fever or severe pain"]

    # Default fallback
    return [
        {
            "title": "Basic safe steps",
            "actions": [
                "Rest and hydrate.",
                "Track symptoms (timing, fever, severity 1–10).",
                "Seek care if worsening or not improving."
            ],
            "why": "Safe defaults until more details are known."
        }
    ], [
        "Severe or worsening symptoms",
        "Trouble breathing, chest pain, confusion, fainting"
    ]

MEDLINEPLUS_WS = "https://wsearch.nlm.nih.gov/ws/query"

TRUSTED_GUIDANCE_DOMAINS = {
    "medlineplus.gov",
    "nhs.uk",
    "nhsinform.scot",
    "redcross.org",
    "cdc.gov",
    "mayoclinic.org",
    "clevelandclinic.org",
    "hopkinsmedicine.org",
    "health.harvard.edu",
    "redcross.org",
}

def medlineplus_search(query: str, max_hits: int = 12) -> list[dict]:
    """Search MedlinePlus Health Topics (official NLM/NIH) and return a few topic URLs."""
    # Clean query to improve topic matching (remove "help", "treatment", etc.)
    clean = re.sub(r"(?i)\b(help|treatment|symptoms|cure|for|steps|guide|what to do)\b", "", query).strip()
    if not clean:
        clean = query

    params = {"db": "healthTopics", "term": clean, "retmax": str(max_hits)}
    r = requests.get(MEDLINEPLUS_WS, params=params, timeout=15)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    hits = []
    for doc in root.findall(".//document"):
        title = (doc.findtext(".//content[@name='title']") or "").strip()
        url = (doc.findtext(".//content[@name='url']") or "").strip()
        snippet = (doc.findtext(".//content[@name='full-summary']") or doc.findtext(".//content[@name='snippet']") or "").strip()
        snippet = re.sub(r"<[^>]+>", "", snippet) # clean html tags

        if title and url:
            hits.append({"title": title, "url": url, "snippet": snippet})
    return hits

def is_trusted_url(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower().replace("www.", "")
        return any(host == d or host.endswith("." + d) for d in TRUSTED_GUIDANCE_DOMAINS)
    except Exception:
        return False

def looks_like_emergency_red_flags(items: list[str]) -> bool:
    text = " ".join(items).lower()

    # Phrases that strongly indicate "call 999 / emergency symptoms" lists
    strong = [
        "call 999", "go to a&e", "immediate action required", "emergency",
        "stiff neck", "glass test", "does not fade when you press",
        "difficulty breathing", "breathlessness", "breathing very fast",
        "pale, blue, grey", "pale blue", "blue lips", "grey lips",
        "confused", "not responding", "throat feels tight", "struggling to swallow",
        "sudden swelling of", "tongue look", "lips or tongue"
    ]

    hits = sum(1 for s in strong if s in text)
    # Threshold: if we match several of these, it's almost certainly a red-flag list
    return hits >= 2

def extract_steps_from_html(html: str, max_steps: int = 8) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    scope = soup.find("main") or soup.find("article") or soup

    def clean_items(items: list[str]) -> list[str]:
        out = []
        for x in items:
            x = re.sub(r"\s+", " ", x).strip()
            if 3 <= len(x) <= 220:
                out.append(x)
        return out

    def looks_like_action_list(items: list[str]) -> bool:
        # crude but works well: action lists have lots of verb-y starts
        starters = (
            "try", "do", "avoid", "keep", "get", "talk", "speak", "tell", "contact",
            "call", "go", "make", "write", "practice", "reduce", "cut", "limit",
            "rest", "drink", "eat", "use", "take", "stay", "plan", "book"
        )
        hits = 0
        for it in items:
            first = it.lower().split(" ", 1)[0]
            if first in starters:
                hits += 1
        return hits >= 2  # at least 2 action-like bullets

    # Prioritize "things you can do" / self-help headings first
    positive = [
        "things you can do",
        "things you can do to help",
        "self-help",
        "help yourself",
        "what you can do",
        "what to do",
        "help and support",
        "tips",
        "tips and support",
        "coping",
        "cope with",
        "how to cope",
        "support",
    ]
    negative = [
        "symptoms", "signs", "causes", "check if", "diagnosis", "complications"
    ]

    best_items = []
    best_score = -10

    for h in scope.find_all(["h2", "h3"]):
        ht = h.get_text(" ", strip=True).lower()

        # skip clearly wrong sections
        if any(n in ht for n in negative):
            continue

        score = 0
        for p in positive:
            if p in ht:
                # longer/more specific phrases get more weight
                score += 5 if "things you can do" in p else 3

        if score <= 0:
            continue

        # find first list after this heading
        sibling = h.find_next_sibling()
        while sibling and sibling.name not in ["h2", "h3"]:
            if sibling.name in ["ul", "ol"]:
                items = clean_items([li.get_text(" ", strip=True) for li in sibling.find_all("li")])
                if len(items) >= 3 and looks_like_action_list(items):
                    if score > best_score:
                        best_score = score
                        best_items = items[:max_steps]
                break
            sibling = sibling.find_next_sibling()

    if best_items:
        return best_items[:max_steps]

    # Fallback: find any ul/ol that looks like actions (NOT symptoms)
    for ul in scope.find_all(["ul", "ol"]):
        if len(ul.find_all("a")) >= (len(ul.find_all("li")) / 2):
            continue  # likely nav/menu

        items = clean_items([li.get_text(" ", strip=True) for li in ul.find_all("li")])
        if len(items) >= 3 and looks_like_action_list(items):
            return items[:max_steps]

    return []

def extract_do_dont_from_html(html: str, max_items_each: int = 6):
    soup = BeautifulSoup(html, "lxml")
    scope = soup.find("main") or soup.find("article") or soup

    def clean(x: str) -> str:
        return re.sub(r"\s+", " ", (x or "")).strip()

    do_items, dont_items = [], []

    for h in scope.find_all(["h2", "h3", "h4"]):
        ht = clean(h.get_text(" ", strip=True)).lower()

        if ht in ["do", "dos"]:
            ul = h.find_next(["ul", "ol"])
            if ul:
                do_items = [clean(li.get_text(" ", strip=True)) for li in ul.find_all("li")]
                do_items = [x for x in do_items if x][:max_items_each]

        if ht in ["don't", "dont", "do not", "don'ts", "donts"]:
            ul = h.find_next(["ul", "ol"])
            if ul:
                dont_items = [clean(li.get_text(" ", strip=True)) for li in ul.find_all("li")]
                dont_items = [x for x in dont_items if x][:max_items_each]

    return do_items, dont_items

def extract_steps_from_nhs_selfhelp_sections(html: str, max_steps: int = 8) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    scope = soup.find("main") or soup.find("article") or soup

    def clean(x: str) -> str:
        x = re.sub(r"\s+", " ", x).strip()
        return x

    negative = ["audio", "more in", "page last reviewed", "next review due"]

    steps = []
    for h in scope.find_all(["h2", "h3"]):
        title = clean(h.get_text(" ", strip=True))
        if not title:
            continue
        lt = title.lower()
        if any(n in lt for n in negative):
            continue

        # grab the first paragraph after the heading (if it exists)
        p = h.find_next(["p", "ul", "ol"])
        desc = ""
        if p and p.name == "p":
            desc = clean(p.get_text(" ", strip=True))

        # build a step string
        if desc:
            steps.append(f"{title} — {desc}")
        else:
            steps.append(title)

        if len(steps) >= max_steps:
            break

    return steps

def extract_prevention_from_html(html: str, max_items: int = 6) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    scope = soup.find("main") or soup.find("article") or soup
    
    # Look for "How to prevent", "Avoid", or "Stopping it coming back"
    prevention_headings = ["prevent", "avoid", "stop", "reduce risk"]
    for h in scope.find_all(["h2", "h3"]):
        text = h.get_text().lower()
        if any(t in text for t in prevention_headings):
            sibling = h.find_next_sibling()
            while sibling and sibling.name not in ["h2", "h3"]:
                if sibling.name in ["ul", "ol"]:
                    return [li.get_text(strip=True) for li in sibling.find_all("li")][:max_items]
                sibling = sibling.find_next_sibling()
    return []

def extract_emergency_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()

    scope = soup.find("main") or soup.find("article") or soup
    out = []

    # 1. Look for NHS Care Cards (Red/Orange)
    # They usually have a heading inside.
    care_cards = scope.find_all("div", class_=lambda c: c and "nhsuk-card--care" in c)
    for card in care_cards:
        # Check if it's an emergency/urgent card
        heading = card.find(["h2", "h3", "h4"])
        if heading:
            ht = heading.get_text(" ", strip=True).lower()
            if any(x in ht for x in ["999", "a&e", "emergency", "urgent", "111", "call", "doctor", "gp"]):
                # Extract list items
                for li in card.find_all("li"):
                    out.append(li.get_text(" ", strip=True))
    
    if out: 
        return out[:8]

    # 2. Fallback: Look for headings in plain text
    target_headings = ["call 999", "ask for an urgent gp appointment", "call 111", "urgent advice", "seek medical help"]
    for h in scope.find_all(["h2", "h3"]):
        ht = h.get_text(" ", strip=True).lower()
        if any(t in ht for t in target_headings):
            node = h.find_next_sibling()
            while node:
                if node.name in ["h2", "h3", "div"]: # Stop at next section
                    break
                if node.name in ["ul", "ol"]:
                    for li in node.find_all("li"):
                        out.append(li.get_text(" ", strip=True))
                    break # Usually just one list
                node = node.find_next_sibling()
    
    return out[:8]

def extract_causes_from_html(html: str, max_items: int = 10) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()

    scope = soup.find("main") or soup.find("article") or soup
    
    # 1. Try NHS tables first (common for "Check if you have")
    tables = scope.find_all("table")
    for table in tables:
        # Check headers and caption
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        caption = table.find("caption")
        caption_text = caption.get_text(" ", strip=True).lower() if caption else ""

        # Expanded keywords to catch "Type of stomach ache" | "Possible condition"
        keywords = ["cause", "condition", "symptom", "type of", "check if you have"]
        if any(k in h for h in headers for k in keywords) or any(k in caption_text for k in keywords):
            rows = []
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) >= 2:
                    c1 = cells[0].get_text(" ", strip=True)
                    c2 = cells[1].get_text(" ", strip=True)
                    if c1 and c2:
                        rows.append(f"{c1} — {c2}")
            if rows:
                return rows[:max_items]

    def normalize(items: list[str]) -> list[str]:
        out = []
        for x in items:
            x = re.sub(r"\s+", " ", x).strip()
            if 3 <= len(x) <= 150:
                out.append(x)
        return out

    target_headings = ["causes", "check if you have", "possible causes", "common causes"]

    for h in scope.find_all(["h2", "h3"]):
        ht = h.get_text(" ", strip=True).lower()
        if any(t in ht for t in target_headings):
            node = h.find_next_sibling()
            while node:
                if node.name in ["h2", "h3"]:
                    break
                if node.name in ["ul", "ol"]:
                    items = normalize([li.get_text(" ", strip=True) for li in node.find_all("li")])
                    if items:
                        return items[:max_items]
                node = node.find_next_sibling()
    return []

def nhs_site_search(query: str, max_results: int = 6) -> list[dict]:
    """
    Uses NHS search results page but ONLY extracts actual result links.
    Avoids header/footer navigation links like 'Mental health'.
    """
    try:
        url = "https://www.nhs.uk/search/results"
        params = {"q": query}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        r = requests.get(url, params=params, headers=headers, timeout=12)
        if not r.ok:
            return []

        soup = BeautifulSoup(r.text, "lxml")
        main = soup.find("main") or soup

        results = []
        stop = False

        # Walk through main content until "Support links" section
        for node in main.descendants:
            if getattr(node, "name", None) in ["h2", "h3"]:
                if "support links" in node.get_text(" ", strip=True).lower():
                    stop = True
                    break

            if getattr(node, "name", None) == "a":
                href = node.get("href") or ""
                text = node.get_text(" ", strip=True)
                if not href or not text:
                    continue

                # NHS results are usually relative paths
                if href.startswith("/"):
                    full = "https://www.nhs.uk" + href
                elif href.startswith("http") and "nhs.uk" in href:
                    full = href
                else:
                    continue

                path = urlparse(full).path.lower()

                # Skip known hub pages (the exact ones causing your issue)
                if path in ["/mental-health/", "/healthy-living/", "/care-and-support/", "/nhs-services/", "/health-a-to-z/"]:
                    continue

                # Prefer real condition pages
                if any(p in path for p in ["/conditions/", "/symptoms/", "/mental-health/"]) and len(path) > 14:
                    results.append({"title": text, "url": full})
                elif "/medicines/" in path:
                    results.append({"title": text, "url": full})

                if len(results) >= max_results:
                    break

        # Deduplicate
        out, seen = [], set()
        for x in results:
            if x["url"] not in seen:
                out.append(x)
                seen.add(x["url"])
        return out[:max_results]

    except Exception as e:
        print(f"NHS site search failed: {e}")
        return []

def duckduckgo_search_nhs(query: str, max_results: int = 5) -> list[dict]:
    """
    Search site:nhs.uk via DuckDuckGo HTML to find relevant pages dynamically.
    """
    url = "https://html.duckduckgo.com/html/"
    params = {"q": f"site:nhs.uk {query}"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://html.duckduckgo.com/"
    }
    
    out = []
    try:
        r = requests.post(url, data=params, headers=headers, timeout=10)
        if r.status_code != 200:
            return []
            
        soup = BeautifulSoup(r.text, "html.parser")
        results = soup.find_all("div", class_="result")
        
        for res in results:
            if len(out) >= max_results:
                break
                
            a = res.find("a", class_="result__a")
            if not a:
                continue
                
            title = a.get_text(strip=True)
            raw_href = a.get("href", "")
            
            # Extract real URL from DDG redirect
            link = raw_href
            if "uddg=" in raw_href:
                try:
                    link = unquote(raw_href.split("uddg=")[1].split("&")[0])
                except:
                    pass
            
            # Filter for actual NHS content pages
            if "nhs.uk" in link:
                out.append({"title": title, "url": link})
    except Exception as e:
        print(f"DDG search failed: {e}")
    return out

def nhs_candidate_urls(query: str) -> list[dict]:
    """
    Very lightweight NHS lookup: tries likely condition slugs based on keywords.
    This avoids needing Google/Bing.
    """
    t = query.lower()
    # --- Priority overrides (these must win even with thousands of sitemap slugs) ---
    if "anxiety" in t or "panic" in t or "panic attack" in t or "fear" in t:
        return [{
            "title": "NHS help: anxiety, fear and panic",
            "url": "https://www.nhs.uk/mental-health/feelings-symptoms-behaviours/feelings-and-symptoms/anxiety-fear-panic/"
        }]

    if "depression" in t or "depressed" in t or "low mood" in t:
        return [{
            "title": "NHS self-help: cope with depression",
            "url": "https://www.nhs.uk/mental-health/self-help/tips-and-support/cope-with-depression/"
        }]
    if "rash" in t or "skin rash" in t or "itchy rash" in t:
        return [{
            "title": "NHS guidance: hives",
            "url": "https://www.nhs.uk/conditions/hives/"
    }]
    candidates = []

    # Map common phrases to NHS condition slugs
    slug_map = dict(NHS_SLUG_MAP)

    for k, path in slug_map.items():
        if k in t:
            candidates.append({
                "title": f"NHS guidance: {k}",
                "url": f"https://www.nhs.uk/{path}/"
            })

    return candidates

def is_nhs_hub_page(url: str, html: str) -> bool:
    """
    Returns True for NHS hub/landing pages that don't contain actionable guidance
    (e.g., 'Healthy living' top tasks pages).
    Keep this STRICT so it doesn't skip real condition pages.
    """
    if "nhs.uk" not in (url or ""):
        return False

    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    title = (h1.get_text(" ", strip=True) if h1 else "").lower()

    # Very specific: only skip the 'Healthy living' hub style pages
    if "healthy living" in title:
        if "top tasks" in html.lower():
            return True

    return False

def fetch_guidance_steps(query: str):
    """
    Returns:
      steps_blocks: list[dict] (your existing UI format)
      seek_care_now: list[str]
      prevention: list[str]
      guidance_sources: list[EvidenceSource]
      related: list[str]
    """
    seek_care_now = None
    guidance_sources: list[EvidenceSource] = []
    steps_blocks = []
    prevention = []
    related = []

    try:
        # 1. Get MedlinePlus hits and add ALL to sources
        mp_hits = medlineplus_search(query, max_hits=10)
        for h in mp_hits:
            guidance_sources.append(EvidenceSource(
                id=f"mp-{abs(hash(h['url']))}",
                title=h["title"],
                publisher="MedlinePlus",
                year=datetime.now().year,
                type="Guidance",
                url=h["url"],
                reliability="High",
                rationale="Official NIH MedlinePlus topic.",
                sample_size=None,
                snippet=h.get("snippet") or "Official health guidance."
            ))

        # 2. Try NHS candidates + MedlinePlus hits for step extraction
        # We prioritize NHS for the steps text if available
        # Combine static map (fast) + dynamic search (comprehensive) + MedlinePlus
        candidates = nhs_candidate_urls(query) + nhs_site_search(query) + duckduckgo_search_nhs(query) + mp_hits
        
        # Deduplicate by URL
        seen_urls = set()
        unique_candidates = []
        for c in candidates:
            if c["url"] not in seen_urls:
                unique_candidates.append(c)
                seen_urls.add(c["url"])

        for h in unique_candidates:
            if steps_blocks: 
                break # Stop if we already found steps

            url = h["url"]
            if not is_trusted_url(url):
                continue

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            try:
                page = requests.get(url, headers=headers, timeout=10)
                if not page.ok:
                    continue
                if is_nhs_hub_page(url, page.text):
                    continue

                # If NHS works, add it to sources (at top)
                if "nhs.uk" in url:
                    guidance_sources.insert(0, EvidenceSource(
                        id=f"nhs-{abs(hash(url))}",
                        title=h["title"],
                        publisher="NHS",
                        year=datetime.now().year,
                        type="Guidance",
                        url=url,
                        reliability="High",
                        rationale="NHS Condition Page",
                        sample_size=None,
                        snippet="Official NHS guidance."
                    ))

                # Extract emergency info first
                emergency_info = extract_emergency_from_html(page.text)
                if emergency_info:
                    seek_care_now = emergency_info

               # 1) Injury pages: try Do/Don't blocks first
                do_items, dont_items = extract_do_dont_from_html(page.text)

                if do_items or dont_items:
                    # Use Do's as steps, and Don'ts as prevention
                    steps = do_items
                    if dont_items:
                        prevention = dont_items
                else:
                    # 2) General pages: try bullet/ordered lists under good headings
                    steps = extract_steps_from_html(page.text)

                    # 3) NHS self-help pages: headings + paragraphs (no lists)
                    if not steps and "nhs.uk" in url:
                        steps = extract_steps_from_nhs_selfhelp_sections(page.text)

                related = extract_causes_from_html(page.text)

                if steps and not looks_like_emergency_red_flags(steps):
                    steps_blocks = [{
                        "title": "Recommended steps (trusted guidance)",
                        "actions": steps,
                        "why": f"Extracted from: {h['title']}"
                    }]
                    if not prevention:
                        prevention = extract_prevention_from_html(page.text)
                    return steps_blocks, seek_care_now, prevention, related, guidance_sources
            except Exception as e:
                print(f"Error fetching {url}: {e}")
                continue
        
        return steps_blocks, seek_care_now, prevention, related, guidance_sources

    except Exception as e:
        # If anything fails, just fall back to your current symptom_steps()
        print("Guidance fetch failed:", e)

    # If we couldn't extract step-by-step lists, still provide a useful guidance fallback
    if not steps_blocks and mp_hits:
        top = mp_hits[0]
        snippet = (top.get("snippet") or "").strip()
        if snippet:
            steps_blocks = [{
                "title": "Trusted guidance (summary)",
                "actions": [snippet] if len(snippet) <= 240 else [snippet[:240] + "..."],
                "why": f"From: {top.get('title')}"
            }]

    return steps_blocks, seek_care_now, prevention, related, guidance_sources

def prevention_tips(query_text: str, sources: List[EvidenceSource] = None) -> list[str]:
    tips = []

    # 1. Try to extract from PubMed sources if available
    if sources:
        for s in sources:
            # Only use Guidance sources (NHS/MedlinePlus) for prevention tips
            if s.type != "Guidance":
                continue

            text = s.snippet or ""
            # Split roughly into sentences
            sentences = re.split(r'(?<=[.!?])\s+', text)
            for sent in sentences:
                s_lower = sent.lower()
                # Look for prevention keywords
                if any(x in s_lower for x in ["prevent", "avoid", "reduce risk", "prophylaxis"]):
                    clean = sent.strip()
                    # Basic quality filter
                    if 20 <= len(clean) <= 200 and "?" not in clean:
                        tips.append(clean)

    # Deduplicate preserving order
    tips = list(dict.fromkeys(tips))
    
    if tips:
        return tips[:5]

    # 2. Generic fallback
    return [
        "Consult a healthcare provider for specific prevention advice.",
        "Keep a record of your symptoms to identify triggers.",
        "Maintain general hygiene and healthy habits."
    ]

def build_response(user_text: str, sources: List[EvidenceSource]) -> AssistantResponse:
    lower = user_text.lower()
    lower = re.sub(r"\s+", " ", lower).strip()
    lower = lower.replace("nose bleed", "nosebleed")

    urgent_terms = [
        "chest pain", "trouble breathing", "shortness of breath",
        "faint", "passed out", "worst headache", "confusion",
        "stroke", "face droop", "severe allergic"
    ]
    urgent = any(t in lower for t in urgent_terms)

    # Start with symptom templates as a fallback
    steps, seek_care_now = symptom_steps(lower, sources)

    triage = None
    if urgent:
        triage = {
            "level": "Urgent",
            "headline": "This may be urgent based on what you wrote.",
            "redFlags": [
                "Trouble breathing or chest pain",
                "Fainting, confusion, or severe weakness",
                "Severe allergic reaction (swelling/wheeze)",
                "Sudden severe headache or stroke-like symptoms",
            ],
            "suggestedAction": "Seek urgent medical care now.",
        }

    return AssistantResponse(
        triage=triage,
        steps=steps,
        seekCareNow=seek_care_now,
        prevention = prevention_tips(lower, sources),
        related=[],
        sources=sources
    )


@app.post("/api/chat")
def chat(req: ChatRequest):
    query = req.message.strip()
    if not query:
        return {"error": "Missing message"}

    # 1. Try Trusted Guidance (NHS / MedlinePlus) FIRST
    guidance_steps, seek_care_now, guidance_prevention, guidance_related, guidance_sources = fetch_guidance_steps(query)

    sources: List[EvidenceSource] = []
    if guidance_sources:
        sources.extend(guidance_sources)

    # 2. Always fetch PubMed so it appears in sources list
    try:
        pmids = pubmed_esearch(query, retmax=10)
        fetched = pubmed_efetch(pmids)

        for item in fetched:
            abstract = item.get("abstract") or ""
            n = infer_sample_size(abstract)
            reliability, rationale = reliability_from_year_and_n(item.get("year"), n)
            snippet = (abstract[:600] + "...") if abstract else "No abstract available."

            sources.append(EvidenceSource(
                id=f"pubmed-{item.get('pmid')}",
                title=item.get("title") or f"PubMed article {item.get('pmid')}",
                publisher=item.get("journal") or "PubMed",
                year=item.get("year") or datetime.now().year,
                type="PubMed study",
                url=f"https://pubmed.ncbi.nlm.nih.gov/{item.get('pmid')}/" if item.get("pmid") else "https://pubmed.ncbi.nlm.nih.gov/",
                reliability=reliability,
                rationale=rationale + " Sample size inferred from abstract when available.",
                sample_size=n,
                snippet=snippet
            ))
    except Exception as e:
        print(f"PubMed search failed: {e}")

    payload = build_response(query, sources)

    # If we found trusted step-by-step guidance, use it
    if guidance_steps:
        payload.steps = guidance_steps
        if seek_care_now:
            payload.seekCareNow = seek_care_now
    
    if guidance_prevention:
        payload.prevention = guidance_prevention

    if guidance_related:
        payload.related = guidance_related

    return payload.model_dump()
# --- Emergency number mapping (partial, expand as needed) ---
# Many countries use 112; US/Canada use 911; UK/Ireland 999/112; Australia 000/112; NZ 111; etc.
EMERGENCY_BY_COUNTRY = {
    "US": "911",
    "CA": "911",
    "MX": "911",
    "GB": "999 or 112",
    "IE": "999 or 112",
    "AU": "000 or 112",
    "NZ": "111",
    "IN": "112",
    "ZA": "10111 or 112",
    "FR": "112",
    "DE": "112",
    "ES": "112",
    "IT": "112",
    "NL": "112",
    "SE": "112",
    "NO": "112",
    "DK": "112",
    "FI": "112",
    "BR": "190 (Police) / 192 (Ambulance) / 193 (Fire)",
    "JP": "110 (Police) / 119 (Ambulance/Fire)",
    "KR": "112 (Police) / 119 (Ambulance/Fire)",
}

def reverse_geocode_country(lat: float, lon: float):
    """
    Reverse geocode via OpenStreetMap Nominatim.
    Returns (country_code, country_name) or (None, None) on failure.
    """
    try:
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            "format": "jsonv2",
            "lat": str(lat),
            "lon": str(lon),
            "zoom": "3",
            "addressdetails": "1",
        }
        # Nominatim requires a User-Agent identifying your app
        headers = {"User-Agent": "HelpMyHealthHackathon/1.0 (contact: demo@example.com)"}
        r = requests.get(url, params=params, headers=headers, timeout=12)
        r.raise_for_status()
        data = r.json()
        addr = data.get("address", {}) or {}
        cc = (addr.get("country_code") or "").upper() or None
        cn = addr.get("country") or None
        return cc, cn
    except Exception:
        return None, None


@app.get("/api/emergency")
def emergency(lat: float = Query(...), lon: float = Query(...)):
    cc, country = reverse_geocode_country(lat, lon)

    # Default fallback: 112 works in many regions; if unknown, show guidance.
    if cc and cc in EMERGENCY_BY_COUNTRY:
        number = EMERGENCY_BY_COUNTRY[cc]
        return {
            "country_code": cc,
            "country": country,
            "number": number,
            "note": "If you are in immediate danger, call your local emergency number now.",
        }

    return {
    "country_code": cc,
    "country": country,
    "number": "911 (US) or 112 (international)",
    "note": "Could not confidently determine location — showing common emergency numbers.",
}