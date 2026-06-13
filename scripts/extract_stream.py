"""
IPTV Stream Extractor — Playwright Edition (v3)
================================================
Extracts HLS .m3u8 stream URLs from stream.codecloud.bd with
proper channel names and logos for every entry.

Name extraction order (per channel):
  1. og:title meta tag on the watch page
  2. <title> tag (cleaned)
  3. <h1>/<h2> heading on the watch page
  4. Card img alt text / card heading from listing page
  5. URL slug → prettified  (e.g. /watch/bein-sports-1 → "Bein Sports 1")

Logo extraction order:
  1. og:image meta tag on the watch page
  2. .channel-logo / .logo / [class*=logo] img
  3. Card img src from listing page
"""

import re
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright, Page

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL   = "https://stream.codecloud.bd/"
OUTPUT_PATH = Path(__file__).parent.parent / "playlist" / "fifa_tv.m3u"
M3U8_RE    = re.compile(r'https?://[^\s\'"<>]+\.m3u8(?:[^\s\'"<>]*)?', re.I)

# Cap how many channel pages we visit per run (to stay within Action timeout)
MAX_CHANNELS = 50

# ── Helpers ───────────────────────────────────────────────────────────────────

def slug_to_name(url: str) -> str:
    """Turn a URL slug into a readable channel name.
    e.g. /watch/bein-sports-hd-1  →  'Bein Sports HD 1'
    """
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1]
    # Replace hyphens/underscores with space, title-case each word
    return " ".join(
        w.upper() if w.lower() in ("hd", "sd", "tv", "bd", "uk", "us")
        else w.capitalize()
        for w in re.split(r"[-_]", slug)
        if w
    )


def abs_url(src: str, base: str) -> str:
    if not src:
        return ""
    if src.startswith("http"):
        return src
    return urljoin(base, src)


def first_nonempty(*values) -> str:
    for v in values:
        v = (v or "").strip()
        if v and v.lower() not in ("channel", "live", "stream", "tv", ""):
            return v
    return ""


# ── Browser setup ─────────────────────────────────────────────────────────────

def make_context(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Referer": BASE_URL,
            "Origin":  BASE_URL.rstrip("/"),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    return browser, ctx


# ── Stream interception ───────────────────────────────────────────────────────

def collect_streams(page: Page, url: str, wait_ms: int = 20_000) -> list[str]:
    """Navigate to url, intercept network traffic, return all .m3u8 URLs found."""
    found: list[str] = []

    def on_request(req):
        if ".m3u8" in req.url and req.url not in found:
            found.append(req.url)

    def on_response(resp):
        if ".m3u8" in resp.url and resp.url not in found:
            found.append(resp.url)

    page.on("request",  on_request)
    page.on("response", on_response)

    try:
        page.goto(url, wait_until="networkidle", timeout=wait_ms)
    except Exception as exc:
        log.warning("  ⚠ page load: %s", exc)

    try:
        page.wait_for_timeout(5000)   # wait for lazy-loaded player
    except Exception:
        pass

    # Also scan rendered DOM
    try:
        for m in M3U8_RE.findall(page.content()):
            if m not in found:
                found.append(m)
    except Exception:
        pass

    page.remove_listener("request",  on_request)
    page.remove_listener("response", on_response)
    return list(dict.fromkeys(found))


# ── Channel info from watch page ──────────────────────────────────────────────

def extract_page_info(page: Page, fallback_url: str) -> dict:
    """
    After navigating to a watch page, pull:
      - name  : channel name
      - logo  : logo image URL
    """
    name = ""
    logo = ""
    page_url = page.url or fallback_url

    # ── Name ──────────────────────────────────────────────────────────────────

    # 1. og:title
    try:
        v = page.get_attribute('meta[property="og:title"]', "content") or ""
        name = first_nonempty(name, v)
    except Exception:
        pass

    # 2. twitter:title
    try:
        v = page.get_attribute('meta[name="twitter:title"]', "content") or ""
        name = first_nonempty(name, v)
    except Exception:
        pass

    # 3. <title> tag — strip site suffix like " | CodeCloudBD"
    if not name:
        try:
            t = page.title() or ""
            for sep in [" | ", " - ", " :: ", " — "]:
                if sep in t:
                    t = t.split(sep)[0]
            name = first_nonempty(name, t)
        except Exception:
            pass

    # 4. First <h1> or <h2>
    if not name:
        try:
            for sel in ["h1", "h2", ".channel-title", ".stream-title",
                        "[class*='title']", "[class*='channel-name']"]:
                el = page.query_selector(sel)
                if el:
                    t = el.inner_text().strip().split("\n")[0]
                    name = first_nonempty(name, t)
                    if name:
                        break
        except Exception:
            pass

    # 5. URL slug fallback
    if not name:
        name = slug_to_name(fallback_url)

    # ── Logo ──────────────────────────────────────────────────────────────────

    # 1. og:image
    try:
        v = page.get_attribute('meta[property="og:image"]', "content") or ""
        logo = logo or abs_url(v, page_url)
    except Exception:
        pass

    # 2. twitter:image
    if not logo:
        try:
            v = page.get_attribute('meta[name="twitter:image"]', "content") or ""
            logo = logo or abs_url(v, page_url)
        except Exception:
            pass

    # 3. Dedicated logo img elements
    if not logo:
        try:
            for sel in [
                ".channel-logo img", ".logo img", ".stream-logo img",
                "[class*='logo'] img", "[class*='channel'] img",
                ".player-overlay img", ".watermark img",
            ]:
                el = page.query_selector(sel)
                if el:
                    src = el.get_attribute("src") or el.get_attribute("data-src") or ""
                    if src and "logo" in src.lower():
                        logo = abs_url(src, page_url)
                        break
        except Exception:
            pass

    log.info("  📛 name='%s'  🖼 logo='%s'", name, logo[:60] if logo else "")
    return {"name": name, "logo": logo}


# ── Channel discovery from listing page ───────────────────────────────────────

def discover_channels(page: Page) -> list[dict]:
    """
    Scrape the main listing page.
    For every channel link found, extract: url, name, logo.
    """
    channels: list[dict] = []
    seen_urls: set[str] = set()

    LINK_SELECTORS = [
        "a[href*='/watch/']",
        "a[href*='/live/']",
        "a[href*='/channel/']",
    ]

    for sel in LINK_SELECTORS:
        try:
            elements = page.query_selector_all(sel)
        except Exception:
            continue

        for el in elements:
            href = (el.get_attribute("href") or "").strip()
            if not href:
                continue
            full_url = urljoin(BASE_URL, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            name = ""
            logo = ""

            # ── Name from the <a> element ──────────────────────────────────
            try:
                name = first_nonempty(
                    name,
                    el.get_attribute("title"),
                    el.get_attribute("aria-label"),
                    el.get_attribute("data-name"),
                    el.get_attribute("data-title"),
                )
            except Exception:
                pass

            # Inner img alt text
            try:
                img = el.query_selector("img")
                if img:
                    alt = img.get_attribute("alt") or ""
                    src = (img.get_attribute("src")
                           or img.get_attribute("data-src")
                           or img.get_attribute("data-lazy-src") or "")
                    name = first_nonempty(name, alt)
                    if src:
                        logo = abs_url(src, BASE_URL)
            except Exception:
                pass

            # Inner text (skip if purely icon)
            if not name:
                try:
                    text = el.inner_text().strip().split("\n")[0]
                    if text and len(text) > 1:
                        name = first_nonempty(name, text)
                except Exception:
                    pass

            # ── Walk up to card parent for richer info ─────────────────────
            try:
                parent = el.evaluate_handle(
                    """el => el.closest(
                        '[class*="card"],[class*="channel"],[class*="item"],
                        article, li'
                    )"""
                )
                if parent:
                    # Heading inside card
                    for h_sel in ["h1","h2","h3","h4",".title",".name",
                                  "[class*='title']","[class*='name']"]:
                        try:
                            h = parent.query_selector(h_sel)
                            if h:
                                t = h.inner_text().strip().split("\n")[0]
                                name = first_nonempty(name, t)
                                if name:
                                    break
                        except Exception:
                            pass

                    # Logo inside card
                    if not logo:
                        try:
                            ci = parent.query_selector("img")
                            if ci:
                                src = (ci.get_attribute("src")
                                       or ci.get_attribute("data-src") or "")
                                alt = ci.get_attribute("alt") or ""
                                name = first_nonempty(name, alt)
                                if src:
                                    logo = abs_url(src, BASE_URL)
                        except Exception:
                            pass
            except Exception:
                pass

            # ── Final fallback: pretty-print slug ─────────────────────────
            if not name:
                name = slug_to_name(full_url)

            channels.append({"name": name, "logo": logo, "url": full_url})
            log.info("  🔗 discovered: %-30s  %s", name, full_url)

    return channels


# ── M3U builder ───────────────────────────────────────────────────────────────

def build_m3u(entries: list[dict]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "#EXTM3U",
        f"# Auto-updated : {ts}",
        f"# Total channels: {len(entries)}",
        f"# Source        : {BASE_URL}",
        "",
    ]
    for e in entries:
        stream = e.get("stream_url", "")
        if not stream:
            continue
        name  = e.get("name",  "Unknown")
        logo  = e.get("logo",  "")
        group = e.get("group", "BD TV")
        slug  = e.get("url", "").rstrip("/").split("/")[-1]

        logo_part  = ' tvg-logo="'  + logo  + '"' if logo  else ""
        id_part    = ' tvg-id="'    + slug  + '"' if slug  else ""
        name_part  = ' tvg-name="'  + name  + '"'

        lines.append(
            f'#EXTINF:-1{id_part}{name_part}{logo_part}'
            f' group-title="{group}",{name}'
        )
        lines.append(stream)
        lines.append("")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    results: list[dict] = []

    with sync_playwright() as p:
        browser, ctx = make_context(p)

        # ── Load main / listing page ──────────────────────────────────────────
        log.info("🌐 Loading listing page: %s", BASE_URL)
        main_page = ctx.new_page()

        try:
            main_page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)
        except Exception as exc:
            log.warning("Main page load issue: %s", exc)

        try:
            main_page.wait_for_timeout(4000)
        except Exception:
            pass

        channels = discover_channels(main_page)
        main_page.close()

        log.info("🔎 Discovered %d channel(s) to visit", len(channels))

        if not channels:
            log.warning("No channel links found — trying BASE_URL directly")
            channels = [{"name": "CodeCloudBD", "logo": "", "url": BASE_URL}]

        # ── Visit each channel page ───────────────────────────────────────────
        for ch in channels[:MAX_CHANNELS]:
            log.info("📺 Visiting: %s  [%s]", ch.get("name","?"), ch["url"])
            pg = ctx.new_page()

            streams = collect_streams(pg, ch["url"], wait_ms=22_000)

            # Extract proper name + logo from the rendered page
            info = extract_page_info(pg, ch["url"])
            pg.close()

            # Merge: page info wins over card info, but card logo can fill gap
            final_name = first_nonempty(info["name"], ch.get("name","")) or slug_to_name(ch["url"])
            final_logo = info["logo"] or ch.get("logo", "")

            if streams:
                log.info("  ✅ stream captured for '%s'", final_name)
                results.append({
                    "name":       final_name,
                    "logo":       final_logo,
                    "group":      "FIfa WC Special",
                    "url":        ch["url"],
                    "stream_url": streams[0],
                })
            else:
                log.warning("  ⚠ no stream found for '%s'", final_name)

        browser.close()

    # ── Deduplicate by stream URL ─────────────────────────────────────────────
    seen: set[str] = set()
    unique = []
    for r in results:
        s = r.get("stream_url","")
        if s and s not in seen:
            seen.add(s)
            unique.append(r)

    if not unique:
        log.error("❌ No streams extracted. Playlist NOT updated.")
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(build_m3u(unique), encoding="utf-8")
    log.info("✅ Saved %d channel(s) → %s", len(unique), OUTPUT_PATH)

    # Print summary table
    log.info("\n%s", "─"*60)
    log.info("%-30s  %s", "CHANNEL", "STREAM URL (truncated)")
    log.info("%s", "─"*60)
    for r in unique:
        log.info("%-30s  %s", r["name"][:30], r["stream_url"][:50])
    log.info("%s", "─"*60)


if __name__ == "__main__":
    main()
