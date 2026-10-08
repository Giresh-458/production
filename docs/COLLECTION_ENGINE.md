# Intelligence Collection Engine

*Note: This document describes the scraping strategy for the 11 downstream intelligence agents (Company, Labs, Literature, etc.). The Funding Agent uses a separate deep-spidering engine described in `FUNDING_CRAWL4AI_ENGINE.md`.*

The intelligence data collection layer in RIF is designed to be highly resilient, adaptive, and respectful of target domains. It is located primarily in `core/web_collection.py` and `core/shared_crawl_manager.py`.

## Dual-Engine Scraping Strategy

The web is hostile to automated scrapers. To ensure reliable intelligence extraction, RIF implements a dual-engine fallback system:

1. **Primary Engine (Fast HTTP/Text Extraction)**
   - The system initiates fetching via a fast HTTP client (`requests`).
   - The raw HTML is parsed using `Trafilatura` and `BeautifulSoup4` to strip away boilerplate (navbars, footers, cookie banners) and extract the core article text.

2. **Secondary Engine (Headless Browser Fallback)**
   - If the primary engine fails (due to a 403 Forbidden anti-bot block, or if the extracted text is suspiciously short indicating a React/JS shell), the system seamlessly routes the URL to a headless `Playwright` Chromium instance.
   - Playwright renders the JavaScript, executes the page, and extracts the DOM just like a real human visiting the site.

## Rate Limiting & Ethics

- **Concurrency Limits**: The `SharedCrawlManager` enforces strict limits on concurrent connections per domain to prevent overwhelming target servers (enforcing a hard limit of `threading.Semaphore(2)` — maximum 2 simultaneous connections per domain).
- **Timeouts**: Every HTTP request and browser navigation is strictly bounded by a hard deadline (default 20 seconds). If a site hangs, the collector aborts and moves to the next source to prevent pipeline stalls.
- **Deduplication**: URLs are hashed and tracked in `outputs/crawl_cache.db`. If a URL was successfully scraped in a previous run, the orchestrator retrieves it from the local SQLite cache instead of hitting the remote server again.

## Integration with Agents

Each of the 11 downstream Collection Agents defines a list of target URLs or search queries in `sources/*.yaml`. They pass these targets to the central `SharedCrawlManager`, which handles the locks, caching, fallbacks, and text extraction automatically. The agent only receives the cleaned Markdown text to analyze against the active funding context.
