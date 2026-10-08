# Future Development Roadmap

The following phases outline the planned enhancements to the Research Intelligence Framework (RIF) beyond the currently implemented v1.0 pipeline.

## Phase 1: Scheduled Source Operations
- [ ] **Scheduled Source Refresh**: Implement a cron-like runner to periodically update configured YAML sources.
- [ ] **Automated Source Discovery**: Add a background workflow to search for new grants, companies, and literature, and propose them for addition to the YAML configs.
- [ ] **Change Detection**: Add stable fingerprints (hashes) for collected pages to detect content modifications and avoid unnecessary downstream processing if a page hasn't changed.

## Phase 2: Recurring Batch Orchestration
- [ ] **Profile-Based Runs**: Support execution profiles (e.g., `daily_quick_scan`, `weekly_deep_dive`).
- [ ] **Incremental Synthesis**: Add a recurring synthesis runner that only processes newly discovered evidence or changed problem clusters.
- [ ] **Automated Alerting**: Add a notification layer for important new findings (e.g., a newly announced grant that perfectly matches a high-value cluster).

## Phase 3: External Validation Integration
- [ ] **Novelty Checks**: Implement API calls to Google Scholar or arXiv to automatically verify if a proposed research idea has already been published.
- [ ] **Feasibility Checks**: Automatically cross-reference generated prototypes against GitHub to see if similar repositories already exist.

## Phase 4: UI Enhancements
- [ ] **Operations Dashboard**: Extend the Streamlit UI to monitor running batch operations, view crawler statistics, and inspect cache hit rates.
- [ ] **Human-in-the-Loop Refinement**: Add a chat interface in the UI to allow researchers to manually edit and refine the synthesized grant proposals before export.
