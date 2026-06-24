"""
IPTV Stream Extractor — Playwright Edition
==========================================
Uses headless Chromium to render JavaScript SPAs and intercepts
ALL network requests/responses to capture .m3u8 HLS stream URLs.

Target: https://blocinoapi.com/watch/japan-vs-netherlands-12446
No environment variables needed — URL is hardcoded below.
"""

import re
import sys
import json
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, Page

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL = "https://blocinoapi.com"

# Add individual channel watch pages here if you know them.
# The script will ALSO auto-discover channels from the main page.
MANUAL_CHANNELS = [
    # {"name": "FIFA TV",   "url": "https://stream.codecloud.bd/watch/fifa-tv"},
    # {"name": "beIN 1",    "url": "https://stream.codecloud.bd/watch/bein-1"},
]

# YouTube live streams / channels to resolve via yt-dlp.
# Works with either:
#   - a specific video/live URL, e.g. "https://www.youtube.com/watch?v=XXXXXXXXXXX"
#   - a channel's live URL,        e.g. "https://www.youtube.com/@SomeChannel/live"
#   - a channel handle/URL,        e.g. "https://www.youtube.com/@SomeChannel"
#     (yt-dlp will resolve this to whatever is currently live, if anything)
YOUTUBE_CHANNELS = [
    # {"name": "Somoy TV YT",       "url": "https://www.youtube.com/live/ITx_k7uNFP4?si=3r7EGJ61A2DZmVFH", "group": "FWC Special"},
    # {"name": "Ekattor TV YT","url": "https://www.youtube.com/live/9L9ymmaPIS0?si=S62X-5-YFRjXmPLE", "group": "FWC Special"},
    # {"name": "DBC News YT",       "url": "https://www.youtube.com/live/W5ANJGgnjxg?si=GAqod5skeKdezbyz", "group": "FWC Special"},
]

OUTPUT_PATH = Path(__file__).parent.parent / "playlist" / "fifa_tv.m3u"

# Regex to find m3u8 URLs in rendered HTML / JS
M3U8_RE = re.compile(r'https?://[^\s\'"<>]+\.m3u8(?:[^\s\'"<>]*)?', re.I)

# Selectors to find channel card links on the listing page
CHANNEL_LINK_SELECTORS = [
    "a[href*='/watch/']",
    "a[href*='/live/']",
    "a[href*='/channel/']",
    ".channel-card a",
    ".channel-item a",
    ".stream-card a",
    "[class*='channel'] a",
    "[class*='stream'] a",
    "[class*='card'] a",
]

# ── Browser helpers ───────────────────────────────────────────────────────────

def make_context(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Referer":  BASE_URL,
            "Origin":   BASE_URL.rstrip("/"),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    return browser, context


def collect_streams(page: Page, url: str, wait_ms: int = 20_000) -> list[str]:
    """
    Navigate to `url`, intercept every network request/response,
    and return deduplicated .m3u8 URLs found in the network traffic
    AND in the rendered HTML source.
    """
    found: list[str] = []

    def on_request(req):
        if ".m3u8" in req.url:
            log.info("  → [REQ ] %s", req.url)
            found.append(req.url)

    def on_response(resp):
        if ".m3u8" in resp.url:
            log.info("  → [RESP] %s", resp.url)
            found.append(resp.url)

    page.on("request",  on_request)
    page.on("response", on_response)

    try:
        page.goto(url, wait_until="networkidle", timeout=wait_ms)
    except Exception as exc:
        log.warning("  ⚠ goto timeout/error (continuing anyway): %s", exc)

    # Extra wait — some players fire HLS requests 2–3 s after page load
    try:
        page.wait_for_timeout(4000)
    except Exception:
        pass

    # Also scan the fully-rendered DOM for m3u8 URLs
    try:
        html = page.content()
        for m in M3U8_RE.findall(html):
            if m not in found:
                log.info("  → [HTML] %s", m)
                found.append(m)
    except Exception:
        pass

    page.remove_listener("request",  on_request)
    page.remove_listener("response", on_response)
    return list(dict.fromkeys(found))   # preserve order, remove dups


def discover_channels(page: Page) -> list[dict]:
    """
    Scrape the main listing page for channel card links.
    Returns a list of {"name": ..., "url": ...} dicts.
    """
    channels = []
    seen_urls: set[str] = set()

    for sel in CHANNEL_LINK_SELECTORS:
        try:
            for el in page.query_selector_all(sel):
                href = el.get_attribute("href") or ""
                if not href:
                    continue
                full_url = urljoin(BASE_URL, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # Best-effort name extraction
                try:
                    name = (
                        el.inner_text().strip().split("\n")[0]
                        or el.get_attribute("title")
                        or el.get_attribute("aria-label")
                        or "Channel"
                    )
                except Exception:
                    name = "Channel"

                channels.append({"name": name.strip() or "Channel", "url": full_url})
        except Exception:
            continue

    return channels


def resolve_youtube_stream(url: str, timeout_s: int = 30) -> str | None:
    """
    Use yt-dlp to resolve a YouTube URL (specific video, /live, or channel
    handle) to its current best HLS (.m3u8) playback URL.

    Returns None if the video/channel isn't live or yt-dlp fails.
    Requires `yt-dlp` to be installed and on PATH.
    """
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "-f", "best",
        "-g",                # print final media URL only
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        log.error("  ⚠ yt-dlp not found — install it with `pip install yt-dlp`")
        return None
    except subprocess.TimeoutExpired:
        log.warning("  ⚠ yt-dlp timed out for %s", url)
        return None

    if proc.returncode != 0:
        # Common cases: channel not currently live, video unavailable, etc.
        err = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else ""
        log.warning("  ⚠ yt-dlp could not resolve %s (%s)", url, err)
        return None

    # -g can print multiple lines (e.g. separate video/audio streams).
    # We want the one that actually looks like an HLS manifest.
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    for l in lines:
        if ".m3u8" in l:
            return l
    # Fall back to the first URL printed if no explicit .m3u8 found
    return lines[0] if lines else None


def collect_youtube_streams(channels: list[dict]) -> list[dict]:
    """
    Resolve a list of {"name", "url", "group"} YouTube entries to playable
    stream URLs via yt-dlp. Entries that aren't currently live are skipped.
    """
    results: list[dict] = []
    for ch in channels:
        log.info("📺 [YouTube] %s → %s", ch["name"], ch["url"])
        stream = resolve_youtube_stream(ch["url"])
        if stream:
            log.info("   ✅ stream: %s", stream)
            results.append({
                "name": ch["name"],
                "group": ch.get("group", "FWC Special"),
                "stream_url": stream,
            })
        else:
            log.warning("   ⚠ no live stream found (channel may be offline)")
    return results


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
        name   = e.get("name",  "Channel")
        logo   = e.get("logo",  "")
        group  = e.get("group", "FWC Special")
        logo_attr = ' tvg-logo="' + logo + '"' if logo else ""
        lines.append(f'#EXTINF:-1{logo_attr} group-title="{group}",{name}')
        lines.append(stream)
        lines.append("")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    results: list[dict] = []

    with sync_playwright() as p:
        browser, ctx = make_context(p)

        # ── Step 1: Load main page ────────────────────────────────────────────
        log.info("🌐 Loading main page: %s", BASE_URL)
        main_page = ctx.new_page()
        main_streams = collect_streams(main_page, BASE_URL, wait_ms=30_000)

        if main_streams:
            log.info("✅ Found %d stream(s) on main page", len(main_streams))
            for s in main_streams:
                results.append({"name": "CodeCloudBD Live", "group": "BD TV", "stream_url": s})

        # ── Step 2: Discover channel links ────────────────────────────────────
        auto_channels = discover_channels(main_page)
        log.info("🔎 Auto-discovered %d channel link(s)", len(auto_channels))
        main_page.close()

        # Merge manual + auto channels (deduplicate by URL)
        all_channels: list[dict] = list(MANUAL_CHANNELS)
        seen_ch_urls  = {c["url"] for c in all_channels}
        for ch in auto_channels:
            if ch["url"] not in seen_ch_urls:
                all_channels.append(ch)
                seen_ch_urls.add(ch["url"])

        # ── Step 3: Visit each channel page ───────────────────────────────────
        for ch in all_channels[:30]:   # cap at 30 channels per run
            log.info("📺 [%s] → %s", ch["name"], ch["url"])
            pg = ctx.new_page()
            streams = collect_streams(pg, ch["url"], wait_ms=20_000)
            pg.close()

            if streams:
                log.info("   ✅ stream: %s", streams[0])
                results.append({**ch, "stream_url": streams[0]})
            else:
                log.warning("   ⚠ no stream found")

        browser.close()

    # ── Step 4: Resolve YouTube live streams (via yt-dlp) ──────────────────────
    if YOUTUBE_CHANNELS:
        log.info("▶️  Resolving %d YouTube source(s)…", len(YOUTUBE_CHANNELS))
        yt_results = collect_youtube_streams(YOUTUBE_CHANNELS)
        results.extend(yt_results)

    # ── Deduplicate results by stream URL ─────────────────────────────────────
    seen_streams: set[str] = set()
    unique: list[dict] = []
    for r in results:
        s = r.get("stream_url", "")
        if s and s not in seen_streams:
            seen_streams.add(s)
            unique.append(r)

    if not unique:
        log.error("❌ No streams found. Playlist NOT updated.")
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(build_m3u(unique), encoding="utf-8")
    log.info("✅ Playlist saved → %s  (%d channel(s))", OUTPUT_PATH, len(unique))


if __name__ == "__main__":
    main()
    
