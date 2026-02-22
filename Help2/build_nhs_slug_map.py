import re
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

SITEMAP_INDEX = "https://www.nhs.uk/sitemap.xml"

ALLOW_PREFIXES = (
    "/conditions/",
    "/symptoms/",
    "/mental-health/",
)

HEADERS = {"User-Agent": "Mozilla/5.0"}

def fetch_xml(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text

def extract_locs(xml_text: str):
    root = ET.fromstring(xml_text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [loc.text.strip() for loc in root.findall(".//sm:loc", ns) if loc.text]

def crawl_sitemaps(start_url: str):
    queue = [start_url]
    visited = set()
    pages = []

    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            xml = fetch_xml(url)
            locs = extract_locs(xml)
        except Exception:
            continue

        for loc in locs:
            if loc.endswith(".xml"):
                queue.append(loc)
            else:
                pages.append(loc)

    return pages

def slug_to_key(path: str):
    last = path.strip("/").split("/")[-1]
    last = last.replace("-", " ")
    last = re.sub(r"\s+", " ", last).lower().strip()
    return last

def main():
    print("Downloading NHS sitemap tree...")
    all_pages = crawl_sitemaps(SITEMAP_INDEX)

    slug_map = {}

    for url in all_pages:
        path = urlparse(url).path

        if not any(path.startswith(prefix) for prefix in ALLOW_PREFIXES):
            continue

        key = slug_to_key(path)
        norm_path = path.strip("/")

        if key:
            slug_map.setdefault(key, norm_path)

    with open("nhs_slugs.py", "w", encoding="utf-8") as f:
        f.write("NHS_SLUG_MAP = {\n")
        for k in sorted(slug_map):
            f.write(f"    {k!r}: {slug_map[k]!r},\n")
        f.write("}\n")

    print(f"\nGenerated {len(slug_map)} NHS entries → nhs_slugs.py")

if __name__ == "__main__":
    main()