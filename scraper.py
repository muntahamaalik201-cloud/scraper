# Combined Google Ads Transparency scraper
# Video-ad detection logic is kept from the original scrapper.txt.
# Non-video ads use text/image extraction + package matching from the uploaded non-video files.

from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote, quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
import difflib
import re
import unicodedata

def get_best_matching_package_for_text_ad(headline, description, package_list, min_score=0.70):
    """Matches package names with headline + description using character-level comparison."""
    import difflib
    def clean_text_for_comparison(text):
        if not text or text == "N/A":
            return ""
        return re.sub(r"[^a-z0-9]", "", text.lower())

    ad_text = clean_text_for_comparison(str(headline) + str(description))

    best_pkg = None
    best_score = 0.0

    for pkg in package_list:
        pkg_clean = clean_text_for_comparison(pkg)
        if not pkg_clean:
            continue
        ratio = difflib.SequenceMatcher(None, ad_text, pkg_clean).ratio()
        if ratio > best_score:
            best_score = ratio
            best_pkg = pkg

    if best_score >= min_score:
        return best_pkg, best_score
    return None, best_score

import time
import threading
import sheets


MAX_WORKERS = 2
SHEET_LOCK = threading.Lock()
PLAY_STORE_CACHE_LOCK = threading.Lock()
PLAY_STORE_PACKAGE_CACHE = {}
DIRECT_APP_PACKAGE_CACHE_LOCK = threading.Lock()
DIRECT_APP_PACKAGE_CACHE = {}
PENDING_APP_PACKAGE_ROWS_LOCK = threading.Lock()
PENDING_APP_PACKAGE_ROWS = []


def _app_identity_tokens(value):
    text = str(value or "").casefold()
    if "all-in-one" in text or "tout-en-un" in text or "tudo-em-um" in text or "todo-en-uno" in text:
        text += " allinone"
    if re.search(r"\u0628\u0633\u064a\u0637", text):
        text += " simple"
    if "\u0648\u0627\u0644\u0639\u0644\u0645" in text or "\u0639\u0644\u0645\u064a" in text:
        text += " scientific"
    if "\u0622\u0644\u0629" in text or "\u062d\u0627\u0633\u0628" in text:
        text += " calculator"
    if "\u0e07\u0e48\u0e32\u0e22" in text:
        text += " simple"
    if "\u0e04\u0e34\u0e14\u0e40\u0e25\u0e02" in text:
        text += " calculator"

    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    tokens = re.findall(r"[a-z0-9]+|[\u0600-\u06ff]+|[\u0e00-\u0e7f]+", text)
    aliases = {
        "calculadora": "calculator", "calculatrice": "calculator", "rechner": "calculator",
        "simples": "simple", "simpel": "simple", "sencillo": "simple", "sencilla": "simple",
        "basico": "simple", "basica": "simple", "basic": "simple",
        "cientifica": "scientific", "cientificas": "scientific",
        "cientifico": "scientific", "cientificos": "scientific", "wissenschaftlich": "scientific",
        "scientifique": "scientific", "scientifiques": "scientific",
        "resolva": "solve", "resolver": "solve", "resuelve": "solve", "solving": "solve",
        "equacoes": "equation", "ecuaciones": "equation", "ecuacion": "equation",
        "ecuaciones": "equation", "inteligente": "smart",
    }
    generic = {
        "calculator", "calculators", "app", "apps", "play", "google", "store",
        "the", "and", "for", "with", "in", "on", "a", "an", "one", "um",
        "de", "la", "el", "del", "un", "una", "y", "en", "das", "der",
    }
    meaningful = set()
    for token in tokens:
        canonical = aliases.get(token, token)
        if canonical in generic or len(canonical) < 3:
            continue
        meaningful.add(canonical)
    return meaningful


def _package_cache_key(advertiser, app_name, creative_text=""):
    advertiser_key = re.sub(r"\s+", " ", clean_text(advertiser)).strip().casefold()
    identity_tokens = _app_identity_tokens(f"{app_name} {creative_text}")
    if advertiser_key in {"", "n/a"} or not identity_tokens:
        return None
    return advertiser_key, frozenset(identity_tokens)


def remember_direct_app_package(advertiser, app_name, package_name, creative_text=""):
    """Remember a package only when it came from a creative's actual destination."""
    key = _package_cache_key(advertiser, app_name, creative_text)
    package = (
        package_name.strip()
        if isinstance(package_name, str) and _is_valid_pkg(package_name.strip())
        else extract_package_name(package_name)
    )
    if not key or package == "N/A":
        return
    with DIRECT_APP_PACKAGE_CACHE_LOCK:
        DIRECT_APP_PACKAGE_CACHE.setdefault(key, set()).add(package)


def get_direct_app_package(advertiser, app_name, creative_text=""):
    lookup_key = _package_cache_key(advertiser, app_name, creative_text)
    if not lookup_key:
        return None
    advertiser_key, lookup_tokens = lookup_key
    matched_packages = set()
    with DIRECT_APP_PACKAGE_CACHE_LOCK:
        for (cached_advertiser, cached_tokens), packages in DIRECT_APP_PACKAGE_CACHE.items():
            if cached_advertiser == advertiser_key and lookup_tokens.intersection(cached_tokens):
                matched_packages.update(packages)
    return next(iter(matched_packages)) if len(matched_packages) == 1 else None

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".m3u8")

INSTALL_SELECTORS = [
    "a.install-button-anchor.svg-anchor",
    "a.install-button-anchor",
    'a[data-asoch-targets-ad-objective-type]',
    'a:has-text("Install")',
    'a:has-text("Get")',
    'a:has-text("Download")',
    "a[href]",
    "a[data-href]",
    "[data-url]",
    "[data-destination-url]",
    "[data-click-url]",
    '[role="link"]',
]


def safe_update_combined_row(row_num, data):
    """
    Thread-safe Google Sheet row update.
    Browser scraping runs parallel, but sheet writing is protected.
    """
    with SHEET_LOCK:
        sheets.update_combined_row(row_num, data)


def safe_update_headline_desc(row_num, headline, description):
    """
    Thread-safe Google Sheet row update for Headline and Description in cols M and N.
    """
    with SHEET_LOCK:
        sheets.update_headline_and_description(row_num, headline, description)


def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    """
    Thread-safe log writing.
    """
    with SHEET_LOCK:
        sheets.add_log(
            row_number=row_number,
            status=status,
            log_type=log_type,
            url=url,
            video_id=video_id,
            app_link=app_link,
            message=message
        )


def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")


def clean_text(value):
    if not value:
        return "N/A"
    text = re.sub(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(value))
    return re.sub(r"\s+", " ", text).strip() or "N/A"


def looks_like_code_or_css(value):
    """Reject source/style text exposed by empty Google ad-rendering iframes."""
    text = str(value or "").strip()
    if not text:
        return False
    return bool(
        re.search(
            r"(?:function\s+[A-Za-z_$][\w$]*\s*\(|(?:window|document)\.[A-Za-z_$]|"
            r"Array\.prototype|Object\.prototype|SPDX-License-Identifier|"
            r"Copyright\s+The\s+Closure\s+Library\s+Authors|"
            r"(?:^|[;{}])\s*(?:html|body|:root|#[\w-]+|\.[\w-]+)\s*\{)",
            text,
            re.IGNORECASE,
        )
        or (text.count("{") > 0 and text.count("}") > 0)
        or text.count(";") >= 2
    )


def extract_package_name(app_link):
    """
    Extracts package name from app store link.
    For Google Play: extracts the 'id' parameter
    For App Store: extracts app ID from URL
    """
    if not app_link or app_link == "N/A":
        return "N/A"
    
    try:
        for candidate in decoded_url_variants(app_link):
            lowered = candidate.lower()

            # Google Play URL, including nested/escaped ad click destinations.
            if "play.google.com" in lowered or "market://" in lowered:
                parsed = urlparse(candidate)
                package_name = parse_qs(parsed.query).get("id", [None])[0]
                if package_name and _is_valid_pkg(package_name):
                    return package_name

            # A nested store URL is sometimes present only as an escaped string.
            package_match = re.search(
                r"(?:[?&]id=|[?&]package=)([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)",
                candidate,
                re.IGNORECASE,
            )
            if package_match and _is_valid_pkg(package_match.group(1)):
                return package_match.group(1)

            if "apps.apple.com" in lowered or "itunes.apple.com" in lowered:
                match = re.search(r"/id(\d+)", candidate)
                if match:
                    return f"id{match.group(1)}"

        return "N/A"
    except Exception:
        return "N/A"


def decoded_url_variants(value, max_items=40):
    """Return nested URL encodings found in Google ad click-through links."""
    if not value:
        return []

    found = []
    pending = [str(value)]
    seen = set()

    while pending and len(seen) < max_items:
        current = pending.pop(0).strip().strip("\"'")
        if not current or current in seen:
            continue
        seen.add(current)
        found.append(current)

        normalized = unescape(current)
        normalized = (normalized.replace("\\u0026", "&")
                                .replace("\\u003d", "=")
                                .replace("\\/", "/")
                                .replace("\\x26", "&")
                                .replace("\\x3d", "="))
        decoded = unquote(normalized)

        for variant in (normalized, decoded):
            if variant and variant not in seen:
                pending.append(variant)

        # Google commonly nests its destination in adurl/url/q/ds_dest_url.
        try:
            for values in parse_qs(urlparse(decoded).query, keep_blank_values=False).values():
                pending.extend(values)
        except Exception:
            pass

    return found


# =========================
# VIDEO ID LOGIC (REVERTED TO YOUR ORIGINAL WORKING LOGIC)
# =========================

def is_real_video_response(response):
    try:
        url = response.url.lower()
        headers = response.headers
        content_type = headers.get("content-type", "").lower()

        if content_type.startswith("video/"):
            return True

        if "application/vnd.apple.mpegurl" in content_type:
            return True

        if "application/x-mpegurl" in content_type:
            return True

        if "videoplayback" in url:
            return True

        if any(ext in url for ext in VIDEO_EXTENSIONS):
            return True

    except Exception:
        pass

    return False


def extract_video_id_from_url(req_url):
    """
    Extracts only clean video IDs or filenames.
    Does NOT return full video links.
    """
    try:
        url_lower = req_url.lower()
        parsed = urlparse(req_url)
        query = parse_qs(parsed.query)

        if "videoplayback" in url_lower:
            video_id = query.get("id", [None])[0]

            if video_id:
                return video_id

            for key in ["itag", "ei", "source"]:
                value = query.get(key, [None])[0]
                if value:
                    return value

            return None

        for ext in VIDEO_EXTENSIONS:
            if ext in url_lower:
                filename = parsed.path.split("/")[-1]
                filename = filename.split("?")[0].strip()

                if filename:
                    return filename

        for player_host in ("youtube.com/embed/", "youtube-nocookie.com/embed/"):
            if player_host in url_lower:
                start = url_lower.index(player_host) + len(player_host)
                return req_url[start:].split("?")[0].split("&")[0]

        if "youtube.com/watch" in url_lower:
            return query.get("v", [None])[0]

        if "youtu.be/" in url_lower:
            return req_url.split("youtu.be/")[1].split("?")[0].split("&")[0]

        for key in ("video_id", "docid"):
            value = query.get(key, [None])[0]
            if value:
                return value

    except Exception:
        return None

    return None


def extract_video_from_dom(page):
    """
    Checks actual video elements on page and inside frames.
    """
    try:
        video_sources = page.evaluate("""
            () => Array.from(document.querySelectorAll('video'))
                .map(v => v.currentSrc || v.src || '')
                .filter(Boolean)
        """)

        for src in video_sources:
            video_id = extract_video_id_from_url(src)
            if video_id:
                return video_id

    except Exception:
        pass

    for frame in page.frames:
        try:
            video_sources = frame.evaluate("""
                () => Array.from(document.querySelectorAll('video'))
                    .map(v => v.currentSrc || v.src || '')
                    .filter(Boolean)
            """)

            for src in video_sources:
                video_id = extract_video_id_from_url(src)
                if video_id:
                    return video_id

        except Exception:
            continue

    return "N/A"


def scan_browser_performance_for_video(page):
    """
    Scans performance entries for real video URLs only.
    """
    try:
        urls = page.evaluate("""
            () => performance.getEntriesByType('resource').map(r => r.name)
        """)

        for u in urls:
            u_lower = u.lower()

            if (
                "videoplayback" in u_lower
                or ".mp4" in u_lower
                or ".webm" in u_lower
                or ".mov" in u_lower
                or ".m4v" in u_lower
                or ".m3u8" in u_lower
                or "youtube.com/embed/" in u_lower
                or "youtube.com/watch" in u_lower
                or "youtu.be/" in u_lower
            ):
                video_id = extract_video_id_from_url(u)

                if video_id:
                    return video_id

    except Exception:
        pass

    return "N/A"


def click_possible_video_targets(page):
    """
    Clicks possible video preview areas.
    Avoids install buttons/app links.
    """
    selectors = [
        "video",
        "iframe",
        "creative-preview",
        'button[aria-label*="Play"]',
        'button[title*="Play"]',
        'div[aria-label*="Play"]',
        'img[src*="play"]'
    ]

    for sel in selectors:
        try:
            elements = page.locator(sel)
            count = elements.count()

            for i in range(count):
                el = elements.nth(i)

                if not el.is_visible():
                    continue

                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                    box = el.bounding_box()

                    if not box:
                        continue

                    if box["width"] < 120 or box["height"] < 80:
                        continue

                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2

                    page.mouse.click(x, y)
                    page.wait_for_timeout(1500)
                    return True

                except Exception:
                    continue

        except Exception:
            continue

    return False


def wait_for_video_id(page, captured, max_seconds=20):
    waited = 0

    while waited < max_seconds:
        if captured.get("video_id") and captured["video_id"] != "N/A":
            return captured["video_id"]

        dom_video_id = extract_video_from_dom(page)
        if dom_video_id != "N/A":
            return dom_video_id

        page.wait_for_timeout(500)
        waited += 0.5

    return "N/A"


def detect_video_id(page, captured):
    """
    Main video detection flow.
    """
    video_id = extract_video_from_dom(page)

    if video_id == "N/A":
        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=15)

    if video_id == "N/A":
        video_id = scan_browser_performance_for_video(page)

    if video_id == "N/A":
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(1500)

        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=10)

    return video_id


def has_video_creative(page):
    """Detect visible video media/player content, independent of Google's format label."""
    js = r"""
    () => {
        const visibleSized = (el, minWidth = 120, minHeight = 80) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width >= minWidth && r.height >= minHeight && r.bottom > 0 && r.right > 0 &&
                   r.top < innerHeight && r.left < innerWidth &&
                   s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        };

        if (Array.from(document.querySelectorAll('video')).some(el => visibleSized(el))) return true;

        const playerFrame = Array.from(document.querySelectorAll('iframe[src]')).some(el => {
            if (!visibleSized(el)) return false;
            let src = '';
            try { src = new URL(el.getAttribute('src') || '', location.href).href.toLowerCase(); }
            catch (_) { src = (el.getAttribute('src') || '').toLowerCase(); }
            return /youtube\.com\/embed|youtube-nocookie\.com\/embed|youtu\.be\/|player\.vimeo\.com\/video|vimeo\.com\/video|googlevideo\.com|(?:^|\/)video(?:\/|[?#])|(?:^|\/)player(?:\/|[?#])/.test(src);
        });
        if (playerFrame) return true;

        const markedPlayer = Array.from(document.querySelectorAll('[class*="video-player"]'))
            .some(el => visibleSized(el) &&
            !!el.querySelector('video, canvas, [aria-label*="play" i], [title*="play" i]'));
        if (markedPlayer) return true;

        return Array.from(document.querySelectorAll(
            'button[aria-label], [role="button"][aria-label], button[title], [role="button"][title]'
        )).some(el => {
            if (!visibleSized(el, 20, 20)) return false;
            const label = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).toLowerCase();
            return /play video|watch video|play ad/.test(label);
        });
    }
    """

    for target in [page] + [frame for frame in page.frames if frame != page.main_frame]:
        try:
            if target.evaluate(js):
                return True
        except Exception:
            continue
    return False


# =========================
# APP LINK LOGIC
# =========================

def clean_googleadservices_link(href):
    if not href:
        return "N/A"

    href = href.strip()

    if href.startswith("//"):
        href = "https:" + href

    try:
        for candidate in decoded_url_variants(href):
            parsed = urlparse(candidate)
            host = parsed.netloc.lower()
            if any(domain in host for domain in (
                "play.google.com", "apps.apple.com", "itunes.apple.com"
            )) or parsed.scheme.lower() == "market":
                return candidate

            query = parse_qs(parsed.query)
            for key in ("adurl", "url", "q", "u", "ds_dest_url", "destination"):
                value = query.get(key, [None])[0]
                if value:
                    for nested in decoded_url_variants(value):
                        nested_url = urlparse(nested)
                        nested_host = nested_url.netloc.lower()
                        if any(domain in nested_host for domain in (
                            "play.google.com", "apps.apple.com", "itunes.apple.com"
                        )) or nested_url.scheme.lower() == "market":
                            return nested
    except Exception:
        pass

    return href


def is_good_app_link(href):
    if not href:
        return False

    return any(
        any(domain in candidate.lower() for domain in (
            "googleadservices.com/pagead/aclk",
            "play.google.com",
            "apps.apple.com",
            "itunes.apple.com",
            "market://",
        )) or extract_package_name(candidate) != "N/A"
        for candidate in decoded_url_variants(href)
    )


def get_visible_install_candidates_from_target(target, headline="", description=""):
    candidates = []

    for selector in INSTALL_SELECTORS:
        try:
            loc = target.locator(selector)
            count = loc.count()

            for i in range(count):
                try:
                    el = loc.nth(i)

                    possible_hrefs = []
                    for attribute in (
                        "href", "data-href", "data-url", "data-destination-url",
                        "data-click-url", "data-redirect-url", "data-ad-url"
                    ):
                        try:
                            value = el.get_attribute(attribute, timeout=1000)
                            if value:
                                possible_hrefs.append(value)
                        except Exception:
                            continue

                    try:
                        onclick = el.get_attribute("onclick", timeout=1000) or ""
                        possible_hrefs.extend(re.findall(r"https?://[^\s\"'<>]+", onclick))
                    except Exception:
                        pass

                    final_href = next((value for value in possible_hrefs if is_good_app_link(value)), None)
                    if not final_href:
                        continue

                    resolved_href = clean_googleadservices_link(final_href)
                    resolved_package = extract_package_name(resolved_href)

                    box = el.bounding_box(timeout=1500)

                    if not box:
                        continue

                    if box["width"] < 20 or box["height"] < 10:
                        continue

                    text = ""
                    try:
                        text = el.inner_text(timeout=1000).strip().lower()
                    except Exception:
                        pass

                    score = 0

                    if resolved_package != "N/A":
                        score += 120
                        score += int(score_package_against_text(
                            resolved_package, headline, description
                        ) * 100)
                    elif any(domain in resolved_href.lower() for domain in (
                        "play.google.com", "apps.apple.com", "itunes.apple.com", "market://"
                    )):
                        score += 60

                    try:
                        class_name = el.get_attribute("class", timeout=1000) or ""
                        if "install-button-anchor" in class_name:
                            score += 100
                    except Exception:
                        pass

                    if "install" in text:
                        score += 80
                    elif "get" in text or "download" in text:
                        score += 40

                    center_x = box["x"] + box["width"] / 2
                    center_y = box["y"] + box["height"] / 2

                    if 350 <= center_x <= 850:
                        score += 40

                    if 50 <= center_y <= 700:
                        score += 40

                    if center_y > 700:
                        score -= 100

                    candidates.append({
                        "href": resolved_href,
                        "score": score,
                        "box": box,
                        "text": text,
                    })

                except Exception:
                    continue

        except Exception:
            continue

    return candidates


def extract_visible_install_link(page, headline="", description=""):
    """
    Extracts only the visible install button from the active creative.
    Does not scan random adservice links.
    """
    all_candidates = []

    try:
        all_candidates.extend(get_visible_install_candidates_from_target(page, headline, description))
    except Exception:
        pass

    for frame in page.frames:
        try:
            all_candidates.extend(get_visible_install_candidates_from_target(frame, headline, description))
        except Exception:
            continue

    if not all_candidates:
        return "N/A"

    all_candidates.sort(key=lambda x: x["score"], reverse=True)

    best = all_candidates[0]

    if best["score"] <= 0:
        return "N/A"

    return clean_googleadservices_link(best["href"])


def extract_install_link_by_precise_js(page):
    """
    Strict JS fallback:
    only install-button-anchor / Install text links,
    not every googleadservices link.
    """
    js = r"""
    () => {
        const anchors = Array.from(document.querySelectorAll('a[href], a[data-href]'));
        const candidates = anchors.map(a => {
            const href = a.href || a.getAttribute('href') || a.getAttribute('data-href') || '';
            const text = (a.innerText || a.textContent || '').trim().toLowerCase();
            const cls = String(a.className || '').toLowerCase();
            const aria = String(a.getAttribute('aria-label') || '').toLowerCase();
            const rect = a.getBoundingClientRect();

            const goodLink =
                href.includes('googleadservices.com/pagead/aclk') ||
                href.includes('play.google.com') ||
                href.includes('apps.apple.com') ||
                href.includes('itunes.apple.com');

            const looksInstall =
                cls.includes('install-button-anchor') ||
                text.includes('install') ||
                text.includes('get') ||
                text.includes('download') ||
                aria.includes('install');

            const visible =
                rect.width > 20 &&
                rect.height > 10 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth;

            if (!goodLink || !looksInstall || !visible) {
                return null;
            }

            let score = 0;
            if (cls.includes('install-button-anchor')) score += 100;
            if (text.includes('install')) score += 80;
            if (text.includes('get') || text.includes('download')) score += 40;
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            if (cx >= 350 && cx <= 850) score += 40;
            if (cy >= 50 && cy <= 700) score += 40;
            if (cy > 700) score -= 100;
            return {
                href,
                score
            };
        }).filter(Boolean);

        candidates.sort((a, b) => b.score - a.score);

        return candidates.length ? candidates[0].href : null;
    }
    """

    try:
        href = page.evaluate(js)
        if href and is_good_app_link(href):
            return clean_googleadservices_link(href)
    except Exception:
        pass

    for frame in page.frames:
        try:
            href = frame.evaluate(js)
            if href and is_good_app_link(href):
                return clean_googleadservices_link(href)
        except Exception:
            continue

    return "N/A"


def wait_and_extract_install_link(page, max_wait_seconds=35, headline="", description=""):
    start = time.time()

    while time.time() - start < max_wait_seconds:
        app_link = extract_visible_install_link(page, headline, description)

        if app_link != "N/A":
            return app_link

        app_link = extract_install_link_by_precise_js(page)

        if app_link != "N/A":
            return app_link

        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        page.wait_for_timeout(1500)

    return "N/A"


# =========================
# HEADLINE AND DESCRIPTION LOGIC
# =========================

def wait_and_extract_headline_description(page, max_wait_seconds=15):
    """
    Polls for Headline and Description inside iframes ONLY.
    Uses structural class patterns (-e-15, -e-67) and visibility checks 
    to avoid grabbing hidden template text.
    """
    js = r"""
    () => {
        let headText = "N/A";
        let descText = "N/A";

        // Helper to ensure we don't grab hidden/template elements
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
        };

        // SEARCH HEADLINE: Matches any class containing '-e-15' OR 'headline'
        const headNodes = document.querySelectorAll('[class*="-e-15"], [class*="headline"]');
        for (let el of headNodes) {
            if (isVisible(el)) {
                let text = (el.innerText || el.textContent || "").replace(/\n/g, ' ').trim();
                // Ensure it's not a template placeholder like {{headline}}
                if (text.length > 1 && !text.includes('{{')) { 
                    headText = text; 
                    break; 
                }
            }
        }

        // SEARCH DESCRIPTION: Matches any class containing '-e-67' OR 'long-description'
        const descNodes = document.querySelectorAll('[class*="-e-67"], [class*="long-description"]');
        for (let el of descNodes) {
            if (isVisible(el)) {
                let text = (el.innerText || el.textContent || "").replace(/\n/g, ' ').trim();
                if (text.length > 1 && text !== headText && !text.includes('{{')) { 
                    descText = text; 
                    break; 
                }
            }
        }

        // If we found either one, return it
        if (headText !== "N/A" || descText !== "N/A") {
            return { headline: headText, description: descText };
        }

        return null;
    }
    """

    start = time.time()
    
    # Retry loop: Keeps trying for up to max_wait_seconds (15s)
    while time.time() - start < max_wait_seconds:
        
        # STRICTLY CHECK IFRAMES ONLY.
        for frame in page.frames:
            try:
                result = frame.evaluate(js)
                if result and (result.get("headline", "N/A") != "N/A" or result.get("description", "N/A") != "N/A"):
                    return result.get("headline", "N/A"), result.get("description", "N/A")
            except Exception:
                continue
        
        # Wait 1 second and loop again to let the ad iframe fully load
        page.wait_for_timeout(1000)

    # If the timer runs out, return N/A
    return "N/A", "N/A"

# =========================
# STRICT TEXT-AD PACKAGE MATCHER
# =========================

MIN_PACKAGE_MATCH_SCORE = 0.76

_GENERIC_PACKAGE_TOKENS = {
    "com", "net", "org", "co", "io", "app", "apps", "android", "mobile",
    "google", "play", "store", "free", "pro", "lite", "online", "official",
    "inc", "ltd", "llc", "studio", "studios", "company", "group", "digital",
    "ai", "all", "new", "best", "easy", "fast", "calculator", "calculators"
}


def clean_text_for_comparison(text):
    """Lowercase and remove punctuation/spaces for ad text vs package comparison."""
    if not text or text == "N/A":
        return ""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def split_words_for_comparison(text):
    if not text or text == "N/A":
        return []
    return re.findall(r"[a-z0-9]+", str(text).lower())


def package_tokens_for_matching(pkg):
    """Turn com.example.musicplayer into useful tokens like example/musicplayer."""
    if not pkg:
        return []

    raw_tokens = re.split(r"[._-]+", pkg.lower())
    tokens = []

    for token in raw_tokens:
        token = re.sub(r"[^a-z0-9]", "", token)
        if not token or token in _GENERIC_PACKAGE_TOKENS:
            continue
        if len(token) < 3 or token.isdigit():
            continue
        tokens.append(token)

    return tokens


def score_package_against_text(pkg, headline, description, app_name=""):
    """
    Score a package against the visible headline, description, and optional app label.
    This prevents image ads from using random hidden package names from the page HTML.
    """
    visible_parts = [
        str(value).strip()
        for value in (headline, description, app_name)
        if value and str(value).strip().lower() not in {"n/a", "none"}
    ]
    visible_raw = " ".join(visible_parts)
    visible_clean = clean_text_for_comparison(visible_raw)
    visible_words = split_words_for_comparison(visible_raw)
    visible_word_set = set(visible_words)

    if not visible_clean or not visible_words:
        return 0.0

    tokens = package_tokens_for_matching(pkg)
    if not tokens:
        return 0.0

    package_core = "".join(tokens)
    score = 0.0

    # Very strong signal: useful package core appears directly in visible ad text.
    if package_core and len(package_core) >= 6 and package_core in visible_clean:
        score = max(score, 0.98)

    # Direct token hits only. Generic tokens were already removed by package_tokens_for_matching().
    exact_hits = []
    partial_hits = []

    for token in tokens:
        if token in visible_word_set:
            exact_hits.append(token)
            continue

        # Allow long tokens like musicplayer/pdfreader to match joined visible text.
        if len(token) >= 6 and token in visible_clean:
            exact_hits.append(token)
            continue

        for word in visible_words:
            if (
                len(token) >= 4
                and len(word) >= 6
                and (word.startswith(token) or (len(token) >= 6 and token in word))
            ):
                partial_hits.append(token)
                break

    exact_hits = list(dict.fromkeys(exact_hits))
    partial_hits = list(dict.fromkeys(partial_hits))
    total_hits = len(set(exact_hits + partial_hits))

    # One exact distinctive package segment often matches the app's displayed brand.
    if len(exact_hits) >= 2:
        score = max(score, 0.92)
    elif len(exact_hits) == 1 and len(exact_hits[0]) >= 5:
        score = max(score, 0.78)
    elif len(partial_hits) == 1 and len(partial_hits[0]) >= 4:
        score = max(score, 0.76)
    elif total_hits >= 2:
        score = max(score, 0.76)

    # Fuzzy matching can only boost when the whole package core is extremely close.
    # It cannot pass alone on one random similar word.
    if package_core and len(package_core) >= 8:
        core_ratio = difflib.SequenceMatcher(None, visible_clean, package_core).ratio()
        if core_ratio >= 0.88:
            score = max(score, 0.82)

    return round(score, 4)


def get_best_matching_package(headline, description, package_list, min_score=MIN_PACKAGE_MATCH_SCORE):
    """
    Compare headline + description with every found package.
    Returns (package, score). If no package score is at least 0.76, returns (None, best_score).
    """
    if not package_list:
        return None, 0.0

    best_pkg = None
    best_score = 0.0

    for pkg in sorted(package_list):
        score = score_package_against_text(pkg, headline, description)
        if score > best_score:
            best_score = score
            best_pkg = pkg

    if best_pkg and best_score >= min_score:
        return best_pkg, best_score

    return None, best_score


def _play_store_locale_for_text(text):
    """Choose a Play Store locale from the creative's visible language."""
    value = str(text or "")
    if re.search(r"[\u0600-\u06ff]", value):
        return "ar", "SA"
    if re.search(r"[\u0e00-\u0e7f]", value):
        return "th", "TH"

    lower = value.casefold()
    if re.search(r"\b(calculadora|resolva|cient[ií]fic[ao]s|inteligente|aplicativo)\b", lower):
        return "pt-BR", "BR"
    if re.search(r"\b(calculatrice|l'appli|scientifique|tout-en-un|qu'il)\b", lower):
        return "fr", "FR"
    if re.search(r"\b(rechner|wissenschaftlich|einfach|berechnen)\b", lower):
        return "de", "DE"
    if re.search(r"\b(calculadora|resuelve|cient[ií]fica|aplicaci[oó]n)\b", lower):
        return "es", "ES"
    return "en", "US"


def lookup_package_from_play_store(page, headline, description, app_name="", advertiser=""):
    """Fallback for text ads whose rendered DOM hides the app destination/package ID."""
    if (not headline or headline == "N/A") and not app_name.strip():
        return None, 0.0

    title_parts = re.split(r"\s*(?:\||:|\s[-–—]\s)\s*", headline, maxsplit=1)
    title_query = app_name.strip() or (title_parts[0].strip() if title_parts else "")
    title_words = re.findall(r"[\w]+", title_query, flags=re.UNICODE)
    query_parts = [
        title_query if len(title_words) >= 2 else "",
        headline or "",
        description or "",
    ]
    query = re.sub(r"\s+", " ", " ".join(query_parts)).strip()[:160]
    if not query:
        return None, 0.0

    search_page = None
    best_package = None
    best_score = 0.0
    generic_title_words = {"the", "app", "free", "best", "fast", "secure", "official"}
    creative_words = set(re.findall(
        r"[\w]+", f"{headline} {description or ''}".lower(), flags=re.UNICODE
    )) - generic_title_words
    creative_normalized = re.sub(
        r"[^\w]", "", f"{headline} {description or ''}".lower(), flags=re.UNICODE
    )
    app_name_normalized = re.sub(r"[^\w]", "", app_name.lower(), flags=re.UNICODE)
    advertiser_normalized = re.sub(r"[^a-z0-9]", "", advertiser.lower())
    locale, country = _play_store_locale_for_text(f"{headline} {description or ''}")
    cache_key = "|".join((
        advertiser_normalized,
        app_name_normalized,
        locale,
        country,
        query.casefold(),
    ))
    with PLAY_STORE_CACHE_LOCK:
        cached = PLAY_STORE_PACKAGE_CACHE.get(cache_key)
    if cached:
        return cached

    vendor_packages = {
        "com.google.android.calculator": "google",
        "com.miui.calculator": "xiaomi",
        "com.sec.android.app.popupcalculator": "samsung",
    }

    try:
        search_page = page.context.new_page()
        search_url = (
            "https://play.google.com/store/search?" +
            f"q={quote_plus(query)}&c=apps&hl={quote_plus(locale)}&gl={quote_plus(country)}"
        )
        search_page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

        app_links = search_page.locator('a[href*="/store/apps/details?id="]')
        try:
            app_links.first.wait_for(state="visible", timeout=7000)
        except Exception:
            pass

        for index in range(app_links.count()):
            link = app_links.nth(index)
            href = link.get_attribute("href", timeout=1000) or ""
            package = extract_package_name(href)
            if package == "N/A":
                continue

            try:
                result_title = link.inner_text(timeout=1000).strip().splitlines()[0]
            except Exception:
                result_title = link.get_attribute("aria-label", timeout=1000) or ""
            result_title = result_title.strip()
            result_normalized = re.sub(r"[^\w]", "", result_title.lower(), flags=re.UNICODE)
            result_words = set(re.findall(r"[\w]+", result_title.lower(), flags=re.UNICODE)) - generic_title_words

            if not result_normalized or not result_words:
                continue

            expected_vendor = vendor_packages.get(package.lower())
            if expected_vendor and expected_vendor not in advertiser_normalized:
                # Do not assign a device/vendor calculator merely because its
                # generic title matches a third-party advertiser's copy.
                continue

            package_fit = score_package_against_text(
                package, headline, description, app_name=app_name
            )
            if package_fit < 0.70:
                continue

            if result_normalized in creative_normalized and (
                len(result_words) >= 2 or len(result_normalized) >= 14
            ):
                score = 0.70 + min(package_fit * 0.30, 0.28)
            else:
                overlap = len(result_words & creative_words) / max(len(result_words), 1)
                score = min(overlap, 0.88) if len(result_words) >= 2 else 0.0

            # The app card often shows a short display name (for example,
            # "Calculator") separately from the longer ad headline. Accept an
            # exact display-name result only when the package also matches the
            # visible headline/description.
            if app_name_normalized and result_normalized == app_name_normalized:
                score = max(score, 0.70 + min(package_fit * 0.30, 0.28))

            if score > best_score:
                best_package = package
                best_score = score

        if best_package and best_score >= 0.80:
            with PLAY_STORE_CACHE_LOCK:
                PLAY_STORE_PACKAGE_CACHE[cache_key] = (best_package, best_score)
            return best_package, best_score
    except Exception as error:
        print(f"Play Store package lookup skipped for '{query}': {error}")
    finally:
        if search_page:
            try:
                search_page.close()
            except Exception:
                pass

    return None, best_score

def decode_all(text):
    """Decode every encoding variant so no package name is missed."""
    for _ in range(4):
        previous = text
        text = re.sub(r'\\x3[Dd]', '=', text)
        text = re.sub(r'\\x26',    '&', text)
        text = re.sub(r'\\x3[Ff]', '?', text)
        text = re.sub(r'\\x2[Ff]', '/', text)
        text = re.sub(r'\\u003[Dd]', '=', text)
        text = re.sub(r'\\u0026',    '&', text)
        text = re.sub(r'\\u003[Ff]', '?', text)
        text = re.sub(r'%25', '%', text, flags=re.I)
        text = re.sub(r'%3[Dd]', '=', text, flags=re.I)
        text = re.sub(r'%26',    '&', text, flags=re.I)
        text = re.sub(r'%3[Ff]', '?', text, flags=re.I)
        text = re.sub(r'%2[Ff]', '/', text, flags=re.I)
        text = re.sub(r'%3[Aa]', ':', text, flags=re.I)
        text = re.sub(r'%2[Ee]', '.', text, flags=re.I)
        text = unquote(text)
        text = unescape(text)
        if text == previous:
            break
    return text


_SKIP_EXT = re.compile(
    r'\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|json|xml|html|htm|'
    r'woff|woff2|ttf|otf|eot|pdf|zip|apk|mp4|mp3|ogg|m3u8)$', re.I)
_SKIP_PFX = re.compile(
    r'^(com\.google\.android\.(gms|vending|inputmethod|tts|webview)|'
    r'com\.android\.|android\.|androidx\.|kotlin\.|kotlinx\.|'
    r'com\.squareup\.|io\.reactivex\.|okhttp3\.|javax\.|java\.|'
    r'org\.json\.|org\.apache\.)', re.I)

def _is_valid_pkg(pkg):
    parts = pkg.split('.')
    # Android application IDs need at least two dot-separated identifiers.
    if len(parts) < 2 or len(pkg) < 5:  return False
    if _SKIP_EXT.search(pkg):            return False
    if _SKIP_PFX.match(pkg):             return False
    for p in parts:
        if not p or not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', p):
            return False
    return True

def extract_packages_from_text(raw_text):
    """Returns a SET of all unique, valid package names found in the text."""
    text = decode_all(raw_text)
    candidates = set()   

    patterns = [
        r"""['"]appId['"]\s*:\s*['"]([A-Za-z][\w.]+)['"]""",
        r"""['"](?:packageName|package_name|appPackage|androidPackageName)['"]\s*:\s*['"]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)['"]""",
        r"""data-(?:package-name|app-id|android-package)=['"]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)['"]""",
        r"""play\.google\.com/store/apps/details[^\s'"<>]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)""",
        r"""market://[^\s'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)""",
        r"""(?:destination_url|final_url|click_url|destUrl|clickUrl|landingUrl)['"\s]*:['"\s]*['"][^'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)""",
        r"""[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)""",
        r"""[?&]package=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)"""
    ]

    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            pkg = m.group(1).rstrip('.,;\'"\\ ')
            if _is_valid_pkg(pkg):
                candidates.add(pkg)

    return candidates

def extract_package_from_page(page, active_only=False):
    """
    Extract package IDs from rendered creative data and links. When requested,
    inspect the highest-ranked active creative frames first to reduce matches
    against neighboring ads and page chrome.
    """
    collected_texts = []

    targets = []
    if active_only:
        try:
            targets = [item[1] for item in get_ranked_non_video_targets(page)[:4]]
        except Exception:
            targets = []

    if not targets:
        targets = list(page.frames)
        targets.append(page)

    for target in targets:
        try:
            frame_html = target.evaluate("() => document.documentElement.outerHTML")
            if frame_html and len(frame_html) > 200:
                collected_texts.append(frame_html)

            hrefs = target.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                           .map(a => a.href).filter(Boolean)
            """)
            if hrefs:
                collected_texts.append('\n'.join(hrefs))

            visible = target.evaluate("() => document.body ? document.body.innerText : ''")
            if visible:
                collected_texts.append(visible)

        except Exception:
            continue

    combined = '\n'.join(collected_texts)
    packages = extract_packages_from_text(combined)

    return packages

def extract_advertiser_from_page(page):
    try:
        loc = page.locator('.advertiser-title, [data-test-id="advertiser-name"]').first
        loc.wait_for(timeout=4000)
        text = loc.inner_text().strip()
        if text and len(text) > 1 and "Sign in" not in text:
            return text
    except Exception:
        pass

    js = r"""
    () => {
        const badWords = ['sign in', 'log in', 'home', 'menu', 'search', 'help', 'privacy', 'terms', 'ad details', 'see more ads', 'ads transparency'];
        let maxFont = 0;
        let advertiserName = "N/A";

        for (let el of document.querySelectorAll('body *')) {
            if (el.childElementCount > 0) continue;
            let txt = (el.innerText || "").trim();
            let lower = txt.toLowerCase();
            if (txt.length < 2 || txt.length > 60 || badWords.some(b => lower.includes(b))) continue;

            let rect = el.getBoundingClientRect();
            // Strict visual bounds check
            if (rect.width === 0 || rect.height === 0 || rect.y < 0 || rect.y > 350 || rect.width < 10) continue;

            let style = window.getComputedStyle(el);
            if (style.opacity === '0' || style.display === 'none' || style.visibility === 'hidden') continue;

            let font = parseFloat(style.fontSize || '0');
            if (font > maxFont) {
                maxFont = font;
                advertiserName = txt;
            }
        }
        return advertiserName;
    }
    """
    try:
        if advertiser := page.evaluate(js): return advertiser
    except Exception:
        pass
    return "N/A"

def _frame_parent_box(frame):
    """
    Returns the iframe element box in the parent page.
    This is important because a hidden/stale iframe can still return text from inside itself.
    """
    try:
        iframe_el = frame.frame_element()
        box = iframe_el.bounding_box()
        if not box:
            return None
        return box
    except Exception:
        return None


def _score_non_video_target(target):
    """
    Scores a page/frame by checking whether it looks like the active ad creative.
    Higher score = more likely to be the current transparency URL preview.
    """
    js = r"""
    () => {
        const cleanText = (txt) => (txt || '').replace(/\n/g, ' ').replace(/\s+/g, ' ').trim();

        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                style.opacity !== '0'
            );
        };

        const visibleText = cleanText(document.body ? document.body.innerText : '');
        const lowerText = visibleText.toLowerCase();

        const headlineNodes = Array.from(document.querySelectorAll(
            '[class*="-e-15"], [class*="headline"], [aria-label*="Headline"], [aria-label*="headline"]'
        )).filter(el => {
            const txt = cleanText(el.innerText || el.textContent || '');
            return txt.length >= 4 && txt.length <= 180 && isVisible(el) && !txt.includes('{{');
        });

        const descNodes = Array.from(document.querySelectorAll(
            '[class*="-e-67"], [class*="long-description"], [class*="description"], [aria-label*="Description"], [aria-label*="description"]'
        )).filter(el => {
            const txt = cleanText(el.innerText || el.textContent || '');
            return txt.length >= 8 && txt.length <= 260 && isVisible(el) && !txt.includes('{{');
        });

        const installNodes = Array.from(document.querySelectorAll('a[href], a[data-href], button')).filter(el => {
            const txt = cleanText(el.innerText || el.textContent || '').toLowerCase();
            const cls = String(el.className || '').toLowerCase();
            const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
            const href = String(el.href || el.getAttribute('href') || el.getAttribute('data-href') || '').toLowerCase();
            const looksInstall = cls.includes('install-button-anchor') || txt.includes('install') || txt === 'get' || txt.includes('download') || aria.includes('install');
            const goodHref = href.includes('googleadservices.com/pagead/aclk') || href.includes('play.google.com') || href.includes('apps.apple.com') || href.includes('itunes.apple.com');
            return isVisible(el) && (looksInstall || goodHref);
        });

        const imageNodes = Array.from(document.querySelectorAll('img, picture, canvas, svg')).filter(el => {
            const src = String(el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google')) return false;
            const rect = el.getBoundingClientRect();
            return isVisible(el) && rect.width >= 80 && rect.height >= 50;
        });

        const leafTextNodes = Array.from(document.querySelectorAll('*')).filter(el => {
            if (el.childElementCount > 0) return false;
            const txt = cleanText(el.innerText || el.textContent || '');
            if (txt.length < 4 || txt.length > 220) return false;
            if (txt.includes('{{') || txt.includes('}}')) return false;
            return isVisible(el);
        });

        let score = 0;
        score += Math.min(headlineNodes.length, 2) * 120;
        score += Math.min(descNodes.length, 2) * 100;
        score += Math.min(installNodes.length, 2) * 80;
        score += Math.min(imageNodes.length, 3) * 25;
        score += Math.min(leafTextNodes.length, 8) * 8;

        // The Google transparency shell/page chrome should not beat the actual creative iframe.
        if (lowerText.includes('ads transparency center') || lowerText.includes('ads transparency centre')) score -= 180;
        if (lowerText.includes('see more ads') || lowerText.includes('report this ad')) score -= 90;
        if (lowerText.includes('last shown') || lowerText.includes('shown in')) score -= 50;

        return {
            score,
            headlineCount: headlineNodes.length,
            descriptionCount: descNodes.length,
            installCount: installNodes.length,
            imageCount: imageNodes.length,
            leafTextCount: leafTextNodes.length,
            visibleTextLength: visibleText.length
        };
    }
    """
    try:
        return target.evaluate(js) or {"score": 0}
    except Exception:
        return {"score": 0}


def get_ranked_non_video_targets(page):
    """
    Returns frames/page ordered by the most likely active creative.
    Old logic checked page.frames in browser order, which can be wrong for repeated ads.
    """
    ranked = []

    for frame in page.frames:
        if frame == page.main_frame:
            continue
        # Google embeds unrelated safeframe ads and many inert adframe variation
        # templates. Only score frames that contain actual visible content.
        if "googlesyndication.com" in (frame.url or "").lower():
            continue

        parent_bonus = 0
        box = _frame_parent_box(frame)

        if box:
            width = box.get("width", 0) or 0
            height = box.get("height", 0) or 0
            y = box.get("y", 99999) or 99999
            area = width * height

            # Active ad preview iframe is normally visible and reasonably large.
            if width >= 120 and height >= 70:
                parent_bonus += min(area / 8000, 80)
            else:
                parent_bonus -= 120

            # Prefer currently visible/near-top preview, not repeated ads farther down the page.
            if -50 <= y <= 900:
                parent_bonus += 80
            elif 900 < y <= 1400:
                parent_bonus += 20
            else:
                parent_bonus -= 80
        else:
            parent_bonus -= 40

        inner = _score_non_video_target(frame)
        inner_score = float(inner.get("score", 0) or 0)
        if inner_score <= 0:
            continue
        final_score = inner_score + parent_bonus

        if final_score > 0:
            ranked.append((final_score, frame, "iframe", inner))

    # Main page is only a fallback. It contains Google page chrome, so keep it below real creative frames.
    main_inner = _score_non_video_target(page)
    main_score = float(main_inner.get("score", 0) or 0) - 60
    if main_score > 0:
        ranked.append((main_score, page, "main_page", main_inner))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def wait_and_extract_text_ad_details(page, max_wait_seconds=15):
    """
    Extract visible copy from the active creative, using both Google selectors and
    the text-card layout (app label, store badge, headline, description).
    """
    js = r"""
    () => {
        const cleanText = txt => (txt || '')
            .replace(/[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g, '')
            .replace(/\s+/g, ' ').trim();
        const looksLikeCode = text =>
            /function\s+[A-Za-z_$][\w$]*\s*\(|(?:window|document)\.[A-Za-z_$]|Array\.prototype|Object\.prototype|(?:^|[;{}])\s*(?:html|body|:root|#[\w-]+|\.[\w-]+)\s*\{/i.test(text) ||
            ((text.match(/\{/g) || []).length > 0 && (text.match(/\}/g) || []).length > 0) ||
            (text.match(/;/g) || []).length >= 2;
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
                   style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
        };
        const ignored = /^(install|get|download|learn more|sign in|google play|google play store|app store|apple app store|sponsored|ad)$/i;
        const collect = (selectors, minLength, maxLength) => {
            const result = [];
            for (const selector of selectors) {
                for (const el of document.querySelectorAll(selector)) {
                    if (!isVisible(el)) continue;
                    const text = cleanText(el.innerText || el.textContent);
                    if (text.length < minLength || text.length > maxLength ||
                        text.includes('{{') || ignored.test(text) || looksLikeCode(text)) continue;
                    if (!result.includes(text)) result.push(text);
                }
            }
            return result;
        };

        const headlines = collect([
            '[class*="-e-15"]', '[class*="headline"]',
            '[aria-label*="Headline" i]', 'div[role="link"] span',
            'div.HFTpmd-WsjYwc-hgDUwe', 'div.cS4Vcb-vnv8ic'
        ], 3, 180);
        const descriptions = collect([
            '[class*="-e-67"]', '[class*="long-description"]',
            '[class*="description"]', '[aria-label*="Description" i]',
            'div.HFTpmd-WsjYwc-hgDUwe', 'div.cS4Vcb-vnv8ic'
        ], 8, 260);

        // In text ads, the copy is often plain text without stable class names.
        // Read visible text lines around the app-store badge as a structural fallback.
        const lines = (document.body ? document.body.innerText : '')
            .split(/\n+/).map(cleanText).filter(Boolean);
        const storeIndex = lines.findIndex(line => /google play|app store/i.test(line));
        let appName = '';
        let cardHeadline = '';
        let cardDescription = '';
        let textCard = false;

        if (storeIndex >= 0) {
            const storeLine = lines[storeIndex];
            const storeMatch = storeLine.match(/google play(?: store)?|apple app store|app store/i);
            if (storeMatch && storeMatch.index > 0) {
                appName = cleanText(storeLine.slice(0, storeMatch.index));
            }
            if (looksLikeCode(appName)) appName = '';
            if (!appName && storeIndex > 0 && lines[storeIndex - 1].length <= 80) {
                appName = lines[storeIndex - 1];
            }

            const afterStore = lines.slice(storeIndex + 1).filter(line =>
                line.length >= 3 && line.length <= 260 && !ignored.test(line) &&
                !/ads transparency|see more ads|report this ad|last shown/i.test(line) &&
                !looksLikeCode(line)
            );
            if (afterStore.length) {
                cardHeadline = afterStore[0].slice(0, 180);
                cardDescription = afterStore.find((line, index) => index > 0 && line !== cardHeadline) || '';
                textCard = true;
            }
        }

        const headline = cardHeadline || headlines[0] || 'N/A';
        const description = cardDescription || descriptions.find(text => text !== headline) || 'N/A';
        const validCopy = value => value !== 'N/A' && !looksLikeCode(value);
        const cleanHeadline = validCopy(headline) ? headline : 'N/A';
        const cleanDescription = validCopy(description) ? description : 'N/A';
        textCard = textCard && (validCopy(cleanHeadline) || validCopy(cleanDescription));
        return {
            headline: cleanHeadline,
            description: cleanDescription,
            app_name: appName,
            text_card: textCard
        };
    }
    """

    def read_target(target):
        try:
            data = target.evaluate(js)
            if data:
                return data
        except Exception:
            return None
        return None

    start_time = time.time()
    best = {"headline": "N/A", "description": "N/A", "app_name": "", "text_card": False}

    while time.time() - start_time < max_wait_seconds:
        try:
            ranked = get_ranked_non_video_targets(page)
            targets = [item[1] for item in ranked]
        except Exception:
            targets = []
        if not targets:
            targets = [frame for frame in page.frames if frame != page.main_frame] + [page]

        for target in targets:
            data = read_target(target)
            if not data:
                continue
            for field in ("headline", "description"):
                candidate = clean_text(data.get(field, "N/A"))
                if (
                    best[field] == "N/A"
                    and candidate != "N/A"
                    and not looks_like_code_or_css(candidate)
                ):
                    best[field] = candidate
            app_name = clean_text(data.get("app_name"))
            if not best["app_name"] and app_name != "N/A" and not looks_like_code_or_css(app_name):
                best["app_name"] = app_name
            best["text_card"] = best["text_card"] or (
                bool(data.get("text_card")) and
                any(best[field] != "N/A" for field in ("headline", "description"))
            )

        if best["headline"] != "N/A" and best["description"] != "N/A":
            return best

        page.wait_for_timeout(1000)

    return best


def extract_visible_app_name(page):
    """Read the app label immediately paired with a Google Play/App Store badge."""
    js = r"""
    () => {
        const clean = value => (value || '').replace(/\s+/g, ' ').trim();
        const visible = el => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' &&
                   s.visibility !== 'hidden' && s.opacity !== '0';
        };
        const storeLabel = /^(google play|google play store|app store|apple app store)$/i;
        for (const label of document.querySelectorAll('body *')) {
            if (label.children.length || !visible(label)) continue;
            if (!storeLabel.test(clean(label.innerText || label.textContent))) continue;
            let parent = label.parentElement;
            for (let depth = 0; parent && depth < 3; depth++, parent = parent.parentElement) {
                const lines = (parent.innerText || '').split(/\n+/).map(clean).filter(Boolean);
                const index = lines.findIndex(line => storeLabel.test(line));
                if (index > 0 && lines[index - 1].length <= 80) return lines[index - 1];
            }
        }
        for (const selector of ['[class*="app-name"]', '[class*="app-title"]', '[data-testid*="app-name"]']) {
            for (const el of document.querySelectorAll(selector)) {
                if (!visible(el)) continue;
                const value = clean(el.innerText || el.textContent);
                if (value && value.length <= 80) return value;
            }
        }
        return '';
    }
    """
    try:
        ranked = get_ranked_non_video_targets(page)
        targets = [ranked[0][1]] if ranked else []
    except Exception:
        targets = []
    if not targets:
        targets = [frame for frame in page.frames if frame != page.main_frame] + [page]

    for target in targets:
        try:
            value = target.evaluate(js)
            if value:
                return clean_text(value)
        except Exception:
            continue
    return ""
# =========================
# MAIN COMBINED SCRAPER: VIDEO ADS + TEXT ADS
# =========================

def is_valid_text_ad(headline, description):
    if (
        headline and headline != "N/A" and len(clean_text(headline)) >= 3
        and not looks_like_code_or_css(headline)
    ):
        return True
    if (
        description and description != "N/A" and len(clean_text(description)) >= 15
        and not looks_like_code_or_css(description)
    ):
        return True
    return False

def has_visible_image_creative(page, has_text=False):
    """
    Detect a substantial image in the active creative, excluding small app icons
    and images belonging to surrounding Google page chrome.
    """
    js = r"""
    (hasText) => {
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width >= 120 &&
                rect.height >= 80 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                style.opacity !== '0'
            );
        };

        const minImageWidth = hasText ? 180 : 120;
        const minImageHeight = hasText ? 145 : 80;
        const minImageArea = hasText ? 26000 : 9000;
        const substantialAsset = (el, minArea = minImageArea) => {
            if (!isVisible(el)) return false;
            const rect = el.getBoundingClientRect();
            const width = Math.max(rect.width, el.naturalWidth || el.width || 0);
            const height = Math.max(rect.height, el.naturalHeight || el.height || 0);
            const area = rect.width * rect.height;
            const ratio = rect.width / Math.max(rect.height, 1);
            if (area < minArea || rect.width < minImageWidth || rect.height < minImageHeight ||
                width < minImageWidth || height < minImageHeight) return false;
            // Ignore square app icons and logos; keep large square image creatives.
            if (ratio >= 0.82 && ratio <= 1.22 && rect.width < 200) return false;
            return true;
        };

        const imageLike = Array.from(document.querySelectorAll('img')).some(el => {
            const src = String(el.currentSrc || el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google') ||
                /icon|avatar|logo|favicon/.test(alt + ' ' + src)) return false;
            return substantialAsset(el);
        });
        if (imageLike) return true;

        const canvasCreative = Array.from(document.querySelectorAll('canvas'))
            .some(el => substantialAsset(el, Math.max(minImageArea, 40000)));
        if (canvasCreative) return true;

        const svgCreative = Array.from(document.querySelectorAll('svg')).some(el =>
            substantialAsset(el, Math.max(minImageArea, 40000)) && !!el.querySelector('image')
        );
        if (svgCreative) return true;

        const backgroundTargets = hasText
            ? document.querySelectorAll('[role="img"], [class*="creative-image"], [class*="banner-image"], [class*="ad-image"]')
            : document.querySelectorAll('*');
        return Array.from(backgroundTargets).some(el => {
            if (!isVisible(el)) return false;
            const bg = window.getComputedStyle(el).backgroundImage || '';
            return bg !== 'none' && bg.includes('url(') && substantialAsset(el);
        });
    }
    """

    try:
        ranked = get_ranked_non_video_targets(page)
        targets = [ranked[0][1]] if ranked else []
    except Exception:
        targets = []
    if not targets:
        targets = [frame for frame in page.frames if frame != page.main_frame] + [page]

    for target in targets:
        try:
            if target.evaluate(js, has_text):
                return True
        except Exception:
            continue

    return False


def scrape_single_url(url_row):
    row_num, url = url_row

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            service_workers="block",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        page = context.new_page()
        captured = {"video_id": "N/A"}

        # ORIGINAL VIDEO RESPONSE HANDLER - kept unchanged.
        def handle_response(response):
            try:
                if not is_real_video_response(response):
                    return

                video_id = extract_video_id_from_url(response.url)

                if video_id and captured["video_id"] == "N/A":
                    captured["video_id"] = video_id

            except Exception:
                pass

        page.on("response", handle_response)

        try:
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"🔍 Row {row_num}: opening transparency URL")

            safe_add_log(
                row_number=row_num,
                status="STARTED",
                log_type="COMBINED",
                url=url,
                message="Started combined video/text/image ad extraction"
            )

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            advertiser = extract_advertiser_from_page(page)

            # Ignore Google's "Format: Video" label. A rendered player is required
            # before we treat a creative as video or spend time probing for its ID.
            video_creative = has_video_creative(page)
            video_id = detect_video_id(page, captured) if video_creative else "N/A"
            video_time = get_exact_time()

            text_data = wait_and_extract_text_ad_details(page, max_wait_seconds=15)
            headline = clean_text(text_data.get("headline"))
            description = clean_text(text_data.get("description"))
            if headline == "N/A" or description == "N/A":
                fallback_headline, fallback_description = wait_and_extract_headline_description(
                    page, max_wait_seconds=3
                )
                if (
                    headline == "N/A" and fallback_headline != "N/A"
                    and not looks_like_code_or_css(fallback_headline)
                ):
                    headline = clean_text(fallback_headline)
                if (
                    description == "N/A" and fallback_description != "N/A"
                    and not looks_like_code_or_css(fallback_description)
                ):
                    description = clean_text(fallback_description)
            has_text = is_valid_text_ad(headline, description)
            app_name = clean_text(text_data.get("app_name"))
            if not app_name or app_name == "N/A":
                app_name = extract_visible_app_name(page)

            # =========================
            # VIDEO AD PATH
            # =========================
            if video_creative:
                print(f"🎬 Row {row_num}: video creative found (video ID: {video_id})")

                app_link = wait_and_extract_install_link(
                    page, max_wait_seconds=35, headline=headline, description=description
                )
                app_link_time = get_exact_time()

                if headline == "N/A" and description == "N/A":
                    fallback_headline, fallback_description = wait_and_extract_headline_description(
                        page, max_wait_seconds=3
                    )
                    if not looks_like_code_or_css(fallback_headline):
                        headline = clean_text(fallback_headline)
                    if not looks_like_code_or_css(fallback_description):
                        description = clean_text(fallback_description)
                    app_name = app_name or extract_visible_app_name(page)

                package_name = extract_package_name(app_link)
                if package_name != "N/A":
                    remember_direct_app_package(
                        advertiser, app_name, package_name, f"{headline} {description}"
                    )
                if package_name == "N/A":
                    # Some video previews expose the app destination in page data
                    # rather than as a clickable install button.
                    page_packages = extract_package_from_page(page, active_only=True)
                    matched_package, match_score = get_best_matching_package(
                        headline, description, page_packages
                    )
                    if matched_package:
                        remember_direct_app_package(
                            advertiser, app_name, matched_package, f"{headline} {description}"
                        )
                    if not matched_package:
                        broader_packages = extract_package_from_page(page, active_only=False)
                        matched_package, match_score = get_best_matching_package(
                            headline, description, broader_packages, min_score=0.90
                        )
                    if not matched_package:
                        matched_package, match_score = lookup_package_from_play_store(
                            page, headline, description, app_name=app_name,
                            advertiser=advertiser
                        )
                    if matched_package:
                        package_name = matched_package
                        app_link = f"https://play.google.com/store/apps/details?id={matched_package}"
                        print(f"📦 Row {row_num}: video package matched from creative text ({match_score})")

                if app_link == "N/A":
                    status = "VIDEO_FOUND_APP_LINK_NOT_FOUND"
                    message = "Video creative found, but no app install destination was resolved"
                else:
                    status = "SUCCESS"
                    message = "Video creative and app destination saved"

                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    app_link_time,
                    video_id if video_id != "N/A" else "VIDEO",
                    video_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, headline, description)

                safe_add_log(
                    row_number=row_num,
                    status=status,
                    log_type="VIDEO_AD",
                    url=url,
                    video_id=video_id,
                    app_link=app_link,
                    message=message
                )

                print(f"✅ Row {row_num}: saved VIDEO ad advertiser + package + media marker + text")
                return

            # =========================
            # NON-VIDEO PATH: TEXT + IMAGE ADS
            # =========================
            print(f"📄 Row {row_num}: no video found, checking text/image ad")

            process_time = get_exact_time()

            # First try visible install/app link from the active creative.
            visible_app_link = wait_and_extract_install_link(
                page, max_wait_seconds=8, headline=headline, description=description
            )
            visible_package = extract_package_name(visible_app_link)
            package_from_direct_source = visible_package != "N/A"
            if package_from_direct_source:
                remember_direct_app_package(
                    advertiser, app_name, visible_package, f"{headline} {description}"
                )

            is_image_like = (
                has_visible_image_creative(page, has_text=has_text)
                if not has_text else False
            )
            ad_type = (
                "text" if has_text
                else "image" if is_image_like
                else "N/A"
            )

            if not has_text and visible_package == "N/A" and not is_image_like:
                data = [
                    advertiser,
                    "N/A",
                    url,
                    "N/A",
                    process_time,
                    "N/A",
                    process_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")

                safe_add_log(
                    row_number=row_num,
                    status="NO_VIDEO_NO_TEXT_IMAGE",
                    log_type="COMBINED",
                    url=url,
                    video_id="N/A",
                    app_link="N/A",
                    message="No video ID and no valid text/image creative found"
                )

                print(f"⏭ Row {row_num}: no video and no valid text/image ad found")
                return

            if has_text:
                print(f"🔎 Row {row_num}: text/image headline -> {headline}")
            else:
                print(f"🖼 Row {row_num}: likely image ad, headline/description not found")

            print(f"📦 Row {row_num}: resolving package from visible install link first")

            if visible_package != "N/A":
                package_name = visible_package
                app_link = visible_app_link
                match_score = 1.0
                status = "SUCCESS"
                message = f"Non-video {ad_type} ad package extracted from visible install link"
                print(f"✅ Row {row_num}: package from visible install link -> {package_name}")
            else:
                package_name = None
                match_score = 0.0

                if has_text:
                    print(f"📦 Row {row_num}: visible install link not found, strict matching with headline + description")
                    all_found_packages = extract_package_from_page(page, active_only=True)
                    package_name, match_score = get_best_matching_package(headline, description, all_found_packages)
                    if package_name:
                        package_from_direct_source = True
                        remember_direct_app_package(
                            advertiser, app_name, package_name, f"{headline} {description}"
                        )
                    if not package_name:
                        cached_package = get_direct_app_package(
                            advertiser, app_name, f"{headline} {description}"
                        )
                        if cached_package:
                            package_name = cached_package
                            match_score = 1.0
                            package_from_direct_source = True
                    if not package_name:
                        broader_packages = extract_package_from_page(page, active_only=False)
                        package_name, match_score = get_best_matching_package(
                            headline, description, broader_packages, min_score=0.90
                        )
                    if not package_name:
                        print(f"🔎 Row {row_num}: no package ID in the creative; matching the app title in Google Play")
                        package_name, match_score = lookup_package_from_play_store(
                            page, headline, description, app_name=app_name,
                            advertiser=advertiser
                        )

                if package_name:
                    app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                    status = "SUCCESS"
                    message = f"Non-video {ad_type} ad package strictly matched with score {match_score}"
                    print(f"✅ Row {row_num}: strict matched package -> {package_name} | score={match_score}")
                else:
                    package_name = "N/A"
                    app_link = "N/A"
                    status = "NON_VIDEO_PACKAGE_NOT_FOUND"
                    message = f"Non-video {ad_type} ad found, but package score below 0.76. Best score={match_score}"
                    print(f"⚠️ Row {row_num}: package score below 0.76, writing N/A | best score={match_score}")

            data = [
                advertiser,
                package_name,
                url,
                app_link,
                process_time,
                ad_type,      # Column F: text/image for non-video ads
                process_time
            ]

            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline if has_text else "N/A", description if has_text else "N/A")

            if has_text and app_name and app_name != "N/A" and not package_from_direct_source:
                # The batch may contain another creative for the same advertised
                # app whose install destination exposes the package directly.
                with PENDING_APP_PACKAGE_ROWS_LOCK:
                    PENDING_APP_PACKAGE_ROWS.append({
                        "row": row_num,
                        "advertiser": advertiser,
                        "app_name": app_name,
                        "headline": headline,
                        "description": description,
                        "url": url,
                        "ad_type": ad_type,
                        "time": process_time,
                    })

            safe_add_log(
                row_number=row_num,
                status=status,
                log_type="NON_VIDEO_AD",
                url=url,
                video_id=ad_type,
                app_link=app_link,
                message=message
            )

            print(f"✅ Row {row_num}: saved NON-VIDEO {ad_type} ad advertiser + package + headline + description")

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error at {error_time}: {e}")

            try:
                data = [
                    "",
                    "N/A",
                    url,
                    "ERROR",
                    error_time,
                    "ERROR",
                    error_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
            except Exception:
                pass

            try:
                safe_add_log(
                    row_number=row_num,
                    status="ERROR",
                    log_type="COMBINED",
                    url=url,
                    message=str(e)
                )
            except Exception:
                pass

        finally:
            page.close()
            context.close()
            browser.close()


def apply_direct_package_matches_from_batch():
    """Backfill text ads from a unique, directly observed matching app package."""
    with PENDING_APP_PACKAGE_ROWS_LOCK:
        pending_rows = list(PENDING_APP_PACKAGE_ROWS)

    for pending in pending_rows:
        package = get_direct_app_package(
            pending["advertiser"],
            pending["app_name"],
            f"{pending['headline']} {pending['description']}",
        )
        if not package:
            continue

        app_link = f"https://play.google.com/store/apps/details?id={package}"
        update_time = get_exact_time()
        data = [
            pending["advertiser"],
            package,
            pending["url"],
            app_link,
            update_time,
            pending["ad_type"],
            update_time,
        ]
        safe_update_combined_row(pending["row"], data)
        safe_add_log(
            row_number=pending["row"],
            status="SUCCESS",
            log_type="NON_VIDEO_AD",
            url=pending["url"],
            video_id=pending["ad_type"],
            app_link=app_link,
            message=(
                "Package backfilled from the unique direct destination found "
                "for the same advertiser and app label"
            ),
        )
        print(
            f"📦 Row {pending['row']}: package backfilled from matching direct "
            f"creative -> {package}"
        )

def run_parallel_combined_scraper(max_workers=2):
    with PLAY_STORE_CACHE_LOCK:
        PLAY_STORE_PACKAGE_CACHE.clear()
    with DIRECT_APP_PACKAGE_CACHE_LOCK:
        DIRECT_APP_PACKAGE_CACHE.clear()
    with PENDING_APP_PACKAGE_ROWS_LOCK:
        PENDING_APP_PACKAGE_ROWS.clear()

    urls = sheets.get_urls_with_retry()

    url_rows = [
        (i + 2, u.strip())
        for i, u in enumerate(urls)
        if u and u.strip()
    ]

    if not url_rows:
        print("No transparency URLs found in column H.")
        return

    print(f"🚀 Starting combined VIDEO + TEXT scraper for {len(url_rows)} rows")
    print(f"⚡ Running parallel with max_workers={max_workers}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(scrape_single_url, url_row): url_row
            for url_row in url_rows
        }

        for future in as_completed(futures):
            row_num, _ = futures[future]

            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {e}")

                try:
                    safe_add_log(
                        row_number=row_num,
                        status="WORKER_ERROR",
                        log_type="COMBINED",
                        message=str(e)
                    )
                except Exception:
                    pass

    apply_direct_package_matches_from_batch()

    print("✅ Finished combined video + text scraping")


if __name__ == "__main__":
    run_parallel_combined_scraper(max_workers=MAX_WORKERS)
