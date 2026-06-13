"""
IPTV Stream Extractor — Playwright Edition (v4)
================================================
Visits stream.codecloud.bd, finds all .m3u8 URLs,
and names each channel directly from the stream URL path.

  e.g.  https://cdn.example.com/live/bein-sports-hd-1/index.m3u8
        → "Bein Sports HD 1"
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

BASE_URL    = "https://stream.codecloud.bd/"
OUTPUT_PATH = Path(__file__).parent.parent / "playlist" / "fifa_tv.m3u"
M3U8_RE     = re.compile(r'https?://[^\s\'"<>]+\.m3u8(?:[^\s\'"<>]*)?', re.I)
MAX_CHANNELS = 50

# Generic path segments that don't carry channel name info
SKIP_SEGMENTS = {
    "live", "hls", "stream", "streams", "channel", "channels",
    "watch", "index", "playlist", "video", "media", "cdn",
    "content", "tv", "output", "chunks", "seg", "master", "mono",
}

# Words to keep fully uppercase in channel names
UPPERCASE_WORDS = {"hd", "sd", "fhd", "uhd", "4k", "tv", "bd",
                   "uk", "us", "eu", "hls", "ip", "iptv"}

# ── Name from stream URL ───────────────────────────────────────────────────────

def name_from_m3u8(url: str) -> str:
    """
    Derive a readable channel name purely from the .m3u8 stream URL.

    Strategy:
      1. Take all path segments, strip the filename (*.m3u8)
      2. Drop generic/noise segments (live, hls, stream, index, …)
      3. Pick the most descriptive remaining segment
      4. Convert hyphens/underscores → spaces, title-case
      5. Keep known abbreviations uppercase (HD, SD, TV, …)

    Examples:
      .../live/bein-sports-hd-1/index.m3u8    → "Bein Sports HD 1"
      .../hls/star_sports_1/playlist.m3u8     → "Star Sports 1"
      .../channels/fifa-tv/stream.m3u8        → "Fifa TV"
      .../sony-ten-2.m3u8                     → "Sony Ten 2"
    """
    parsed = urlparse(url)
    path   = parsed.path  # e.g.  /live/bein-sports-hd-1/index.m3u8

    # Split path; strip .m3u8 but keep filename as name candidate
    raw_parts = [p for p in path.split("/") if p]
    parts = []
    for seg in raw_parts:
        if seg.lower().endswith(".m3u8"):
            clean = re.sub(r'\.m3u8.*$', '', seg, flags=re.I)
            if clean:
                parts.append(clean)   # "sony-ten-2.m3u8" -> "sony-ten-2"
        else:
            parts.append(seg)

    # Remove noise segments
    meaningful = [p for p in parts if p.lower() not in SKIP_SEGMENTS]

    # Fall back to all parts if nothing meaningful
    slug = meaningful[-1] if meaningful else (parts[-1] if parts else "")

    # Remove query/token noise after common delimiters
    slug = re.split(r'[?&=]', slug)[0]

    # Replace separators with space
    slug = re.sub(r'[-_]+', ' ', slug).strip()

    # Title-case each word, keep abbreviations uppercase
    words = []
    for word in slug.split():
        words.append(word.upper() if word.lower() in UPPERCASE_WORDS
                     else word.capitalize())

    return " ".join(words) or "Channel"


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
            "Referer":       BASE_URL,
            "Origin":        BASE_URL.rstrip("/"),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    return browser, ctx


# ── Stream interception ───────────────────────────────────────────────────────

def collect_streams(page: Page, url: str, wait_ms: int = 20_000) -> list[str]:
    """Navigate to url, intercept ALL network traffic, return .m3u8 URLs."""
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
        page.wait_for_timeout(5000)   # extra wait for lazy player init
    except Exception:
        pass

    # Also scan fully-rendered DOM source
    try:
        for m in M3U8_RE.findall(page.content()):
            if m not in found:
                found.append(m)
    except Exception:
        pass

    page.remove_listener("request",  on_request)
    page.remove_listener("response", on_response)
    return list(dict.fromkeys(found))


# ── Channel link discovery ────────────────────────────────────────────────────

def discover_channels(page: Page) -> list[str]:
    """Return all unique watch/live/channel URLs found on the listing page."""
    urls: list[str] = []
    seen: set[str]  = set()

    for sel in ["a[href*='/watch/']", "a[href*='/live/']", "a[href*='/channel/']"]:
        try:
            for el in page.query_selector_all(sel):
                href = (el.get_attribute("href") or "").strip()
                if not href:
                    continue
                full = urljoin(BASE_URL, href)
                if full not in seen:
                    seen.add(full)
                    urls.append(full)
        except Exception:
            continue

    return urls


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
        stream = e["stream_url"]
        name   = e["name"]
        group  = "FWC 2026 Special"
        lines.append(f'#EXTINF:-1 tvg-name="{name}" group-title="{group}",{name}')
        lines.append(stream)
        lines.append("")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    results: list[dict] = []

    with sync_playwright() as p:
        browser, ctx = make_context(p)

        # Step 1 — load listing page and discover channel links
        log.info("🌐 Loading: %s", BASE_URL)
        main_page = ctx.new_page()
        try:
            main_page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)
            main_page.wait_for_timeout(4000)
        except Exception as exc:
            log.warning("Main page: %s", exc)

        channel_urls = discover_channels(main_page)
        main_page.close()
        log.info("🔎 Found %d channel link(s)", len(channel_urls))

        # If no links found, try the base URL directly
        if not channel_urls:
            channel_urls = [BASE_URL]

        # Step 2 — visit each channel page, capture stream, name from URL
        for ch_url in channel_urls[:MAX_CHANNELS]:
            log.info("📺 %s", ch_url)
            pg = ctx.new_page()
            streams = collect_streams(pg, ch_url, wait_ms=22_000)
            pg.close()

            if streams:
                stream_url = streams[0]
                name       = name_from_m3u8(stream_url)
                log.info("  ✅ %-30s  %s", name, stream_url)
                results.append({"name": name, "stream_url": stream_url})
            else:
                log.warning("  ⚠ no stream at %s", ch_url)

        browser.close()

    # Deduplicate by stream URL
    seen: set[str] = set()
    unique = []
    for r in results:
        if r["stream_url"] not in seen:
            seen.add(r["stream_url"])
            unique.append(r)

    if not unique:
        log.error("❌ No streams found. Playlist NOT updated.")
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(build_m3u(unique), encoding="utf-8")

    # Summary table
    log.info("\n%s", "─" * 60)
    log.info("%-28s  %s", "CHANNEL NAME", "STREAM URL")
    log.info("%s", "─" * 60)
    for r in unique:
        log.info("%-28s  %.50s", r["name"], r["stream_url"])
    log.info("%s", "─" * 60)
    log.info("✅ %d channel(s) saved → %s", len(unique), OUTPUT_PATH)


if __name__ == "__main__":
    main()
