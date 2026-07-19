"""
IPTV Stream Extractor — Playwright Edition (multi-source)
===========================================================
Reads an arbitrary list of source URLs from `source.txt` (repo root),
scans each one for .m3u8 streams, and merges everything into a single
playlist. You can add as many source sites / channel pages / YouTube
links as you want — just add a line to source.txt, no code changes.

For each source line, the extractor:
  1. Detects whether it's a YouTube URL -> resolves via yt-dlp.
  2. Otherwise treats it as a website:
       - Scans the page itself (network traffic + rendered HTML) for
         .m3u8 URLs directly.
       - Auto-discovers channel/watch links on the page and visits
         each one (capped per-source) to pull its stream too.

See source.txt for the exact line format.
"""

import re
import sys
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
import subprocess

from playwright.sync_api import sync_playwright, Page

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

REPO_ROOT     = Path(__file__).parent.parent
SOURCE_FILE   = REPO_ROOT / "source.txt"
OUTPUT_PATH   = REPO_ROOT / "playlist" / "fifa_tv.m3u"

# Safety caps so one bad/huge site can't blow the whole run
MAX_CHANNELS_PER_SOURCE = 40      # auto-discovered sub-pages visited per website
PAGE_WAIT_MS            = 20_000  # per-page network-idle timeout
MAIN_PAGE_WAIT_MS       = 30_000  # listing-page timeout (usually heavier)
YOUTUBE_WORKERS         = 6       # parallel yt-dlp resolutions

# Regex to find m3u8 URLs in rendered HTML / JS
M3U8_RE = re.compile(r'https?://[^\s\'"<>]+\.m3u8(?:[^\s\'"<>]*)?', re.I)

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

# Selectors to find channel card links on a listing page
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


# ── source.txt parsing ───────────────────────────────────────────────────────

def parse_sources(path: Path) -> list[dict]:
    """
    Parse source.txt into a list of {"name", "url", "group"} dicts.
    Supports:
        Name | URL | Group
        Name | URL
        URL                (bare, auto-named)
    Blank lines and lines starting with # are ignored.
    """
    if not path.exists():
        log.error("❌ source.txt not found at %s", path)
        return []

    sources: list[dict] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split("|")]

        if len(parts) == 1:
            url, name, group = parts[0], None, None
        elif len(parts) == 2:
            name, url, group = parts[0], parts[1], None
        else:
            name, url, group = parts[0], parts[1], parts[2]

        if not url or not url.lower().startswith("http"):
            log.warning("  ⚠ skipping malformed source line: %r", raw_line)
            continue

        if not name:
            name = urlparse(url).netloc.replace("www.", "") or "Source"
        if not group:
            group = "IPTV"

        sources.append({"name": name, "url": url, "group": group})

    return sources


def is_youtube(url: str) -> bool:
    return urlparse(url).netloc.lower() in YOUTUBE_HOSTS


# ── Browser helpers ───────────────────────────────────────────────────────────

def make_context(playwright, referer: str):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    parsed = urlparse(referer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Referer": origin,
            "Origin": origin,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    return browser, context


def collect_streams(page: Page, url: str, wait_ms: int = PAGE_WAIT_MS) -> list[str]:
    """
    Navigate to `url`, intercept every network request/response, and
    return deduplicated .m3u8 URLs found in network traffic AND in the
    rendered HTML source.
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

    page.on("request", on_request)
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

    page.remove_listener("request", on_request)
    page.remove_listener("response", on_response)
    return list(dict.fromkeys(found))  # preserve order, remove dups


def discover_channels(page: Page, base_url: str) -> list[dict]:
    """
    Scrape a listing page for channel card links.
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
                full_url = urljoin(base_url, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

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


# ── Website source processing ────────────────────────────────────────────────

def process_website_source(ctx, source: dict) -> list[dict]:
    """
    Handle one non-YouTube source: scan the page itself, then
    auto-discover + visit its sub-channels.
    """
    name, url, group = source["name"], source["url"], source["group"]
    entries: list[dict] = []

    log.info("🌐 [%s] Loading: %s", name, url)
    main_page = ctx.new_page()
    direct_streams = collect_streams(main_page, url, wait_ms=MAIN_PAGE_WAIT_MS)

    if direct_streams:
        log.info("   ✅ %d direct stream(s) on page", len(direct_streams))
        for i, s in enumerate(direct_streams, 1):
            entry_name = name if len(direct_streams) == 1 else f"{name} #{i}"
            entries.append({"name": entry_name, "group": group, "stream_url": s})

    sub_channels = discover_channels(main_page, url)
    main_page.close()

    if sub_channels:
        log.info("   🔎 %d sub-channel link(s) discovered", len(sub_channels))

    for ch in sub_channels[:MAX_CHANNELS_PER_SOURCE]:
        log.info("   📺 [%s] → %s", ch["name"], ch["url"])
        pg = ctx.new_page()
        streams = collect_streams(pg, ch["url"])
        pg.close()

        if streams:
            log.info("      ✅ stream: %s", streams[0])
            entries.append({"name": ch["name"], "group": group, "stream_url": streams[0]})
        else:
            log.warning("      ⚠ no stream found")

    if not entries:
        log.warning("   ⚠ no streams found for source %s", name)

    return entries


# ── YouTube source processing ────────────────────────────────────────────────

def resolve_youtube_stream(url: str, timeout_s: int = 30) -> str | None:
    """
    Use yt-dlp to resolve a YouTube URL (specific video, /live, or
    channel handle) to its current best HLS (.m3u8) playback URL.
    Returns None if the video/channel isn't live or yt-dlp fails.
    """
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "-f", "best", "-g", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        log.error("  ⚠ yt-dlp not found — install it with `pip install yt-dlp`")
        return None
    except subprocess.TimeoutExpired:
        log.warning("  ⚠ yt-dlp timed out for %s", url)
        return None

    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else ""
        log.warning("  ⚠ yt-dlp could not resolve %s (%s)", url, err)
        return None

    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    for l in lines:
        if ".m3u8" in l:
            return l
    return lines[0] if lines else None


def process_youtube_source(source: dict) -> dict | None:
    name, url, group = source["name"], source["url"], source["group"]
    log.info("▶️  [YouTube] %s → %s", name, url)
    stream = resolve_youtube_stream(url)
    if stream:
        log.info("   ✅ stream: %s", stream)
        return {"name": name, "group": group, "stream_url": stream}
    log.warning("   ⚠ no live stream found (offline or unavailable)")
    return None


# ── M3U builder ───────────────────────────────────────────────────────────────

def build_m3u(entries: list[dict]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "#EXTM3U",
        f"# Auto-updated : {ts}",
        f"# Total channels: {len(entries)}",
        "",
    ]
    for e in entries:
        stream = e.get("stream_url", "")
        if not stream:
            continue
        name = e.get("name", "Channel")
        logo = e.get("logo", "")
        group = e.get("group", "IPTV")
        logo_attr = f' tvg-logo="{logo}"' if logo else ""
        lines.append(f'#EXTINF:-1{logo_attr} group-title="{group}",{name}')
        lines.append(stream)
        lines.append("")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    sources = parse_sources(SOURCE_FILE)
    if not sources:
        log.error("❌ No valid sources found in %s — nothing to do.", SOURCE_FILE)
        sys.exit(1)

    website_sources = [s for s in sources if not is_youtube(s["url"])]
    youtube_sources = [s for s in sources if is_youtube(s["url"])]

    log.info(
        "📋 Loaded %d source(s): %d website(s), %d YouTube",
        len(sources), len(website_sources), len(youtube_sources),
    )

    results: list[dict] = []

    # ── Website sources: sequential browser scan (each opens its own pages) ──
    if website_sources:
        with sync_playwright() as p:
            # one shared browser/context is fine across sources; referer is
            # only used for default headers so pick the first source's host
            browser, ctx = make_context(p, website_sources[0]["url"])
            for source in website_sources:
                try:
                    results.extend(process_website_source(ctx, source))
                except Exception as exc:
                    log.error("   ❌ failed processing source %s: %s", source["name"], exc)
            browser.close()

    # ── YouTube sources: resolved in parallel via yt-dlp ──────────────────────
    if youtube_sources:
        log.info("▶️  Resolving %d YouTube source(s) in parallel…", len(youtube_sources))
        with ThreadPoolExecutor(max_workers=YOUTUBE_WORKERS) as pool:
            futures = {pool.submit(process_youtube_source, s): s for s in youtube_sources}
            for fut in as_completed(futures):
                entry = fut.result()
                if entry:
                    results.append(entry)

    # ── Deduplicate by stream URL ──────────────────────────────────────────────
    seen_streams: set[str] = set()
    unique: list[dict] = []
    for r in results:
        s = r.get("stream_url", "")
        if s and s not in seen_streams:
            seen_streams.add(s)
            unique.append(r)

    if not unique:
        log.error("❌ No streams found across any source. Playlist NOT updated.")
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(build_m3u(unique), encoding="utf-8")
    log.info("✅ Playlist saved → %s  (%d channel(s))", OUTPUT_PATH, len(unique))


if __name__ == "__main__":
    main()
