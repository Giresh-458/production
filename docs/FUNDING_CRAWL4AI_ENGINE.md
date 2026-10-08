# Funding Acquisition Engine (Crawl4AI)

While the 11 downstream intelligence agents use a standard concurrency-limited HTTP/Playwright fetcher, the **Funding Agent** requires a much more aggressive approach. 

Grant opportunities are rarely found on a single static page. Government grant portals, foundation websites, and university boards hide their funding calls behind dynamic JavaScript tables, multi-page pagination, and nested PDF guidelines.

To solve this, RIF implements a dedicated deep-traversal engine using **Crawl4AI** located in `core/crawl4ai_web.py` and invoked by `agents/funding_collector.py`.

## Core Features

### 1. BFS Queue-Based Traversal
Instead of pulling a single URL, the Funding Agent initiates a Breadth-First Search (BFS) spider. It starts at a root funding directory URL (defined in `sources/funding_sources.yaml`) and systematically crawls through sub-links and pagination layers up to a configured `max_depth` to discover buried grant announcements.

### 2. Two-Stage Discovery
To prevent the crawler from wandering off-topic, it uses a two-stage filter:
- **High-Recall Link Extraction:** It maps the portal and extracts all links.
- **High-Precision Classification:** It filters the discovered URLs to ensure it only crawls paths that look like actual funding opportunities, discarding contact pages or unrelated news.

### 3. Full PDF Processing
A large percentage of official grant guidelines are published directly as `.pdf` files. The Crawl4AI implementation in RIF intercepts `.pdf` URLs natively, downloads the byte stream, and extracts the text using `PyPDF`, ensuring no critical eligibility requirements are silently skipped.

### 4. LLM Schema Extraction
Once the recursively discovered pages (and PDFs) are extracted into text, they are passed to the central `llm_provider.py`. The LLM extracts the unstructured text into a strict `FundingOpportunity` JSON schema, validating fields like `Funding Amount`, `Deadline`, and `Eligibility` before writing to the SQLite database.
