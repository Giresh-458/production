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

### 3. Directory Roots vs. Direct Programme Pages

A source URL is not automatically an individual funding call. Some configured URLs are catalogues (for example, an ecosystem grants directory, a public SBIR topic listing, or an EU calls portal). The crawler keeps these known roots as traversal hubs and classifies their individual child pages separately, so directory-wide words such as “apply”, “grant”, and “deadline” do not become a false call record.

The source configuration can explicitly designate a true programme page:
- `direct_page: true` marks the source's primary URL as a programme-level record, unless the page explicitly reports that no active funding is available.
- `collection.discovery.direct_page_urls` is a list of additional exact URLs that should be treated as direct programme pages. This is useful when the primary source URL is a directory but a separately seeded official programme page (such as a specific open-grants README) contains the actual acceptance and funding terms.
- Known directory URLs remain non-call roots even when their combined listings contain application language. A direct-page override is explicit and source-scoped; it does not weaken the per-opportunity status, eligibility, or research-domain checks.

Keep each source's `max_depth` and `max_pages` bounded. Portal entries are discovery aids, not evidence that every linked result is open or eligible.

### 4. Full PDF Processing
A large percentage of official grant guidelines are published directly as `.pdf` files. The Crawl4AI implementation in RIF intercepts `.pdf` URLs natively, downloads the byte stream, and extracts the text using `PyPDF`, ensuring no critical eligibility requirements are silently skipped.

### 4. LLM Schema Extraction
Once the recursively discovered pages (and PDFs) are extracted into text, they are passed to the central `llm_provider.py`. The LLM extracts the unstructured text into a strict `FundingOpportunity` JSON schema, validating fields like `Funding Amount`, `Deadline`, and `Eligibility` before writing to the SQLite database.


## Structured funding discovery and live completeness

Most sources use the bounded web collector. A source may instead use API mode when its publisher provides a supported public search API. API adapters must keep publisher-defined status filters, page and document limits, deduplication, and explicit partial/failure reporting; reaching a local cap must never be reported as complete coverage.

The EU Funding & Tenders source uses the European Commission's public Search API at https://api.tech.ec.europa.eu/search-api/prod/rest/search. Its query is restricted to call types 1, 2, and 8 and the open/forthcoming status codes 31094501 and 31094502. The official portal listing remains in the source record for provenance, but the static topic-list export is not used as a substitute for the status-filtered API results. Returned records must retain their official detail URL and only use status, dates, eligibility, or funding amounts when those values are present in source metadata.

The sources/funding_sources.yaml catalogue and sources/source_registry.json lifecycle state work together. A URL present in YAML can still be skipped when its matching registry entry is deprecated. When a source is intentionally restored to the active catalogue, restore its registry status in the same change and add a regression test so an apparently successful run cannot silently omit it.

For live quality checks, read the per-source collection status and inspect the generated call records after the crawler completes. VERIFIED describes source-collection completeness within the configured boundary; it does not by itself prove every extracted record is an open, eligible, domain-matched funding opportunity. A page-cap warning or failed robots-policy check remains a coverage gap and should be resolved or preserved as PARTIAL.


### EU live-call status and eligibility metadata

The EU public Search API can return legacy records whose source status is still Open despite a past deadline. The adapter requests deadline ordering, but live probes showed returned ordering is not dependable. The collector therefore enforces deadline consistency client-side before exposing a record as open. Where the API lacks a scalar eligibility field, extract the publisher's `topicConditions` HTML as eligibility evidence, strip markup and preserve field provenance. Use `descriptionByte` as a description fallback where the standard description field is absent. These fields should still be reviewed at call level; non-empty criteria do not by themselves prove that a call matches RIF's research domains.


### EU funding quality gates

The EU collector uses a bounded fan-out of RIF-domain text queries against the public Search API; these queries are discovery, not proof of eligibility. It uses the public document endpoint by reference only when search hits lack relevant publisher fields, extracts eligibility only from explicit applicant criteria (including relevant paragraphs in beneficiary-administration HTML), and filters expired records. Records without eligibility evidence or a strong canonical research-domain match are rejected from actionable outputs. Quality filters are counted separately from fetch/parse failures; source truncation remains PARTIAL. The gates prefer fewer defensible calls over a larger list of weak or expired opportunities.
