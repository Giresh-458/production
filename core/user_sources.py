"""Add / list / remove user-supplied sources for the collection agents.

A user-added source is written into the agent's own YAML file, in the same shape
as the curated entries, so the agent collects it exactly like a configured source.
Each entry carries ``user_added: true`` and ``added_at``; curation tests and the
UI use that marker to tell user additions apart from curated sources.

Flow used by the UI: ``find_url_candidates`` (name -> official websites, from
Wikidata's "official website" property P856) -> the user confirms a URL ->
``inspect_url`` (reachability, robots.txt, duplicates, domain evidence) -> the
user confirms again -> ``add_user_source``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"
DOMAINS = ("RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins")

# agent key -> (file, root key, layout). Company sections are domains; labs is a list.
AGENT_FILES: dict[str, tuple[str, str, str]] = {
    "funding": ("funding_sources.yaml", "sources", "sections"),
    "company": ("company_sources.yaml", "companies", "domain_sections"),
    "data_availability": ("data_sources.yaml", "data_sources", "sections"),
    "expert": ("expert_sources.yaml", "expert_sources", "sections"),
    "failure": ("failure_sources.yaml", "failure_sources", "sections"),
    "hackathon": ("hackathon_sources.yaml", "hackathon_sources", "sections"),
    "investment": ("investment_sources.yaml", "investment_sources", "sections"),
    "lab": ("labs_sources.yaml", "labs", "list"),
    "opensource": ("opensource_sources.yaml", "opensource_sources", "sections"),
    "practitioner": ("practitioner_sources.yaml", "practitioner_sources", "sections"),
    "regulation": ("regulation_sources.yaml", "regulation_sources", "sections"),
}
AGENT_LABELS = {
    "funding": "Funding", "company": "Company", "data_availability": "Data availability", "expert": "Expert",
    "failure": "Failure / incidents", "hackathon": "Hackathon", "investment": "Investment", "lab": "Lab",
    "opensource": "Open source", "practitioner": "Practitioner", "regulation": "Regulation",
}
COMPANY_SECTION = {"RWA": "rwa", "ESG": "esg_carbon", "ZK-IoV": "zk_iov", "DID": "did", "DePIN": "depin",
                   "MEV": "mev", "Stablecoins": "stablecoins"}
USER_SECTION = "user_added"
_HEADERS = {"User-Agent": "RIF-source-finder/1.0 (research tool; python-requests)"}


class UserSourceError(ValueError):
    pass


# ---------------------------------------------------------------- name -> URL
def looks_like_url(text: str) -> bool:
    value = str(text or "").strip()
    return bool(re.match(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#].*)?$", value, re.IGNORECASE)) and " " not in value


def normalize_url(text: str) -> str:
    value = str(text or "").strip()
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://" + value
    return value


def find_url_candidates(name: str, limit: int = 5, session: Any = None) -> list[dict[str, str]]:
    """Official websites for a name, from Wikidata (property P856). Never guesses."""
    import requests

    client = session or requests
    query = str(name or "").strip()
    if not query:
        return []
    found = client.get("https://www.wikidata.org/w/api.php", params={
        "action": "wbsearchentities", "search": query, "language": "en", "limit": limit, "format": "json",
    }, headers=_HEADERS, timeout=20).json()
    ids = [item["id"] for item in found.get("search", []) if item.get("id")]
    if not ids:
        return []
    entities = client.get("https://www.wikidata.org/w/api.php", params={
        "action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels|descriptions",
        "languages": "en", "format": "json",
    }, headers=_HEADERS, timeout=20).json().get("entities", {})
    candidates = []
    for entity_id in ids:
        entity = entities.get(entity_id, {})
        for claim in entity.get("claims", {}).get("P856", [])[:1]:
            url = (claim.get("mainsnak", {}).get("datavalue") or {}).get("value")
            if url:
                candidates.append({
                    "label": entity.get("labels", {}).get("en", {}).get("value", entity_id),
                    "description": entity.get("descriptions", {}).get("en", {}).get("value", ""),
                    "url": url,
                    "wikidata": f"https://www.wikidata.org/wiki/{entity_id}",
                })
    return candidates


# ---------------------------------------------------------------- inspection
@dataclass
class UrlInspection:
    url: str
    final_url: str = ""
    status: int | None = None
    title: str = ""
    robots_allowed: bool | None = None
    robots_reason: str | None = None
    duplicate_of: list[str] = field(default_factory=list)
    domain_evidence: list[tuple[str, float]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # block adding
    agent_fits: list[Any] = field(default_factory=list)  # core.agent_fit.AgentFit, best first
    warnings: list[str] = field(default_factory=list)   # shown, do not block


def configured_urls() -> dict[str, list[str]]:
    """Normalized URL -> ["agent: name", ...] across all agent source files."""
    out: dict[str, list[str]] = {}
    for agent, (fname, _, _) in AGENT_FILES.items():
        for entry in _iter_entries(_load(SOURCES_DIR / fname)):
            out.setdefault(_url_key(entry.get("url")), []).append(f"{AGENT_LABELS[agent]}: {entry.get('name')}")
    return out


def inspect_url(url: str, agent: str | None, session: Any = None, domains: list[str] | None = None,
                crawler_check: bool = True) -> UrlInspection:
    """Reachability, robots.txt, duplicates, domains and agent fit for a URL.

    ``agent=None`` means "let the app decide": agent-specific checks are skipped and
    ``agent_fits`` ranks every agent for the page.
    """
    import html as html_lib

    import requests

    from core.schemas import rank_research_areas

    client = session or requests
    result = UrlInspection(url=normalize_url(url))
    if agent == "opensource" and not re.match(r"^https://github\.com/[^/]+/[^/#?]+/?$", result.url):
        result.problems.append("Open-source sources must be a GitHub repository URL (github.com/owner/repo).")
    result.duplicate_of = configured_urls().get(_url_key(result.url), [])
    same_agent = [d for d in result.duplicate_of if agent and d.startswith(AGENT_LABELS[agent] + ": ")]
    if same_agent:
        result.problems.append("Already configured for this agent: " + "; ".join(same_agent))
    elif result.duplicate_of:
        result.warnings.append("Also used by another agent: " + "; ".join(result.duplicate_of))
    try:
        response = client.get(result.url, headers={"User-Agent": "Mozilla/5.0 (compatible; RIF-source-check/1.0)"},
                              timeout=25, allow_redirects=True)
        result.status = int(response.status_code)
        result.final_url = str(response.url)
        text = response.text or ""
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        result.title = html_lib.unescape(re.sub(r"\s+", " ", title.group(1))).strip()[:120] if title else ""
        body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))[:60000]
        result.domain_evidence = [(d["domain"], d["raw_score"]) for d in rank_research_areas(body) if d["raw_score"] > 0][:3]
        from core.agent_fit import score_agents

        result.agent_fits = score_agents(result.final_url or result.url, result.title, body)
    except Exception as exc:  # network errors are reported, not raised
        result.problems.append(f"Could not load the page ({type(exc).__name__}).")
    if result.status is not None and result.status >= 400:
        if result.status in (401, 403, 429):
            result.warnings.append(f"The site returned HTTP {result.status} to automated access; collection may get nothing.")
        else:
            result.problems.append(f"The page returned HTTP {result.status}.")
    try:
        # The project's shared RFC 9309 robots policy (wildcard-aware).
        from core.robots_policy import check_robots

        allowed, reason, _delay = check_robots(result.final_url or result.url)
        result.robots_allowed, result.robots_reason = bool(allowed), reason
        if not allowed:
            result.problems.append(f"robots.txt does not allow crawling this page ({reason or 'disallowed'}).")
    except Exception as exc:
        result.warnings.append(f"robots.txt could not be checked ({type(exc).__name__}).")
    if crawler_check and result.status and result.status < 400:
        # A plain request can succeed while the agents' crawler is challenged (e.g. anti-bot
        # pages), so also fetch with the same crawler the agents use.
        try:
            from core.shared_crawl_manager import SharedCrawlManager

            crawled = SharedCrawlManager.get().fetch(result.final_url or result.url)
            if crawled.error or (crawled.status_code and crawled.status_code >= 400):
                reason = crawled.error or f"HTTP {crawled.status_code}"
                result.warnings.append(f"The agents' crawler could not fetch this page ({reason}); "
                                       "collection will most likely get nothing from it.")
            elif crawled.clean_text:
                # Judge agent fit on what the agents' crawler really sees.
                from core.agent_fit import score_agents

                result.agent_fits = score_agents(crawled.final_url or result.final_url or result.url,
                                                 crawled.title or result.title, crawled.clean_text)
        except Exception as exc:
            result.warnings.append(f"The agents' crawler could not be tried ({type(exc).__name__}).")
    if result.status and result.status < 400 and not result.domain_evidence:
        result.warnings.append("The page text shows no evidence for any of the 7 research domains.")
    elif domains and result.domain_evidence and not set(domains) & {d for d, _ in result.domain_evidence}:
        result.warnings.append("The domains you picked were not detected on this page "
                               f"(detected: {', '.join(d for d, _ in result.domain_evidence)}).")
    return result


# ---------------------------------------------------------------- write / list / remove
def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _iter_entries(data: Any):
    if isinstance(data, dict):
        if "url" in data and "name" in data:
            yield data
        for value in data.values():
            yield from _iter_entries(value)
    elif isinstance(data, list):
        for value in data:
            yield from _iter_entries(value)


def _url_key(url: Any) -> str:
    parts = urlsplit(normalize_url(str(url or "")))
    return f"{(parts.hostname or '').casefold().removeprefix('www.')}{parts.path.rstrip('/')}".casefold()


def _q(value: str) -> str:
    """YAML double-quoted scalar."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _entry_lines(agent: str, name: str, url: str, domains: list[str], focus: str, indent: str,
                 added_at: str | None = None) -> list[str]:
    today = added_at or datetime.now(UTC).date().isoformat()
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    lines = [f"{indent}- name: {_q(name)}"]
    inner = indent + "  "
    if agent == "funding":
        lines += [f"{inner}research_domains: [{', '.join(domains)}]", f"{inner}url: {_q(url)}",
                  f"{inner}direct_page: false", f"{inner}focus: {_q(focus)}",
                  f"{inner}user_added: true", f"{inner}added_at: {_q(today)}",
                  f"{inner}collection:", f"{inner}  mode: web", f"{inner}  engine: crawl4ai",
                  f"{inner}  allowed_domains:", f"{inner}  - {host}",
                  f"{inner}  discovery:", f"{inner}    start_urls:", f"{inner}    - {_q(url)}",
                  f"{inner}    currentness_verification_urls:", f"{inner}    - {_q(url)}",
                  f"{inner}    max_depth: 1", f"{inner}    max_pages: 20",
                  f"{inner}    follow_detail_pages: true", f"{inner}    follow_pdfs: false",
                  f"{inner}    pagination: false"]
        return lines
    if agent == "lab":
        lines += [f"{inner}url: {_q(url)}", f"{inner}focus_pages:", f"{inner}- /"]
    else:
        type_key = {"company": "source_type", "regulation": "source_type"}.get(agent, "type")
        type_value = {"opensource": "github_repo", "company": "User-added", "regulation": "User-added"}.get(agent, "website")
        lines += [f"{inner}{type_key}: {type_value}", f"{inner}url: {_q(url)}", f"{inner}focus: {_q(focus)}"]
        if agent == "regulation":
            lines.append(f"{inner}evidence_type: {_q('User-added source')}")
    lines += [f"{inner}user_added: true", f"{inner}added_at: {_q(today)}",
              f"{inner}limits:", f"{inner}  max_pages: 4", f"{inner}  max_depth: 1", f"{inner}  max_seconds: 60"]
    return lines


def _insert_into_section(text: str, section: str, entry_lines: list[str]) -> str:
    """Append entry lines at the end of `  section:` (created at EOF if missing)."""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if line.rstrip() == f"  {section}:"), None)
    if header is None:
        body = text.rstrip("\n") + "\n" + f"  {section}:\n" + "\n".join(entry_lines) + "\n"
        return body
    end = header + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith("    ") or lines[end].startswith("  -")):
        end += 1
    return "\n".join(lines[:end] + entry_lines + lines[end:]) + "\n"


def add_user_source(agent: str, name: str, url: str, domains: list[str], focus: str = "",
                    sources_dir: Path | None = None, *, _record: bool = True, _added_at: str | None = None) -> Path:
    if agent not in AGENT_FILES:
        raise UserSourceError(f"Unknown agent: {agent}")
    domains = [d for d in domains if d in DOMAINS]
    if not domains:
        raise UserSourceError("Choose at least one of the 7 research domains.")
    url = normalize_url(url)
    name = re.sub(r"\s+", " ", str(name or "")).strip() or (urlsplit(url).hostname or url)
    focus = re.sub(r"\s+", " ", str(focus or "")).strip() or f"User-added source ({', '.join(domains)})"
    fname, root_key, layout = AGENT_FILES[agent]
    path = (sources_dir or SOURCES_DIR) / fname
    original = path.read_text(encoding="utf-8")
    existing = list(_iter_entries(_load(path)))
    if any(_url_key(e.get("url")) == _url_key(url) for e in existing):
        raise UserSourceError("This URL is already configured for this agent.")
    if any(str(e.get("name", "")).casefold() == name.casefold() for e in existing):
        raise UserSourceError(f"A source named {name!r} already exists for this agent.")
    if layout == "list":
        updated = original.rstrip("\n") + "\n" + "\n".join(_entry_lines(agent, name, url, domains, focus, "", _added_at)) + "\n"
    else:
        section = COMPANY_SECTION[domains[0]] if layout == "domain_sections" else USER_SECTION
        updated = _insert_into_section(original, section, _entry_lines(agent, name, url, domains, focus, "  ", _added_at))
    path.write_text(updated, encoding="utf-8")
    try:
        reloaded = list(_iter_entries(_load(path)))
        if not any(_url_key(e.get("url")) == _url_key(url) and e.get("user_added") for e in reloaded):
            raise UserSourceError("The entry did not appear after saving.")
        _validate_with_agent_loader(agent, path)
    except Exception:
        path.write_text(original, encoding="utf-8")  # never leave a broken config behind
        raise
    if _record and sources_dir is None:
        _ledger_add({"agent": agent, "name": name, "url": url, "domains": domains, "focus": focus,
                     "added_at": _added_at or datetime.now(UTC).date().isoformat()})
    return path


def _validate_with_agent_loader(agent: str, path: Path) -> None:
    """Load the file with the agent's own loader so the agent can really read it."""
    import importlib

    module = importlib.import_module({"lab": "agents.lab_agent"}.get(agent, f"agents.{agent}_agent"))
    loader = getattr(module, "load_sources", None)
    if loader is not None:
        loader(path)


def list_user_sources(sources_dir: Path | None = None) -> list[dict[str, Any]]:
    rows = []
    for agent, (fname, _, _) in AGENT_FILES.items():
        path = (sources_dir or SOURCES_DIR) / fname
        for entry in _iter_entries(_load(path)):
            if entry.get("user_added"):
                rows.append({"agent": agent, "name": entry.get("name"), "url": entry.get("url"),
                             "added_at": entry.get("added_at"),
                             "domains": entry.get("research_domains") or []})
    return rows


def remove_user_source(agent: str, name: str, sources_dir: Path | None = None) -> None:
    fname, _, _ = AGENT_FILES[agent]
    path = (sources_dir or SOURCES_DIR) / fname
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)- name: (.+?)\s*$", line)
        if not match or yaml.safe_load(match.group(2)) != name:
            continue
        indent = len(match.group(1))
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
            j += 1
        block = "\n".join(lines[i:j])
        if "user_added: true" not in block:
            raise UserSourceError("Only user-added sources can be removed here.")
        remaining = lines[:i] + lines[j:]
        # drop an emptied user_added section header
        cleaned = []
        for k, item in enumerate(remaining):
            if item.rstrip() == f"  {USER_SECTION}:" and not any(
                    r.startswith("  -") or r.startswith("    ") for r in remaining[k + 1:k + 2]):
                continue
            cleaned.append(item)
        path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
        if sources_dir is None:
            import textwrap

            parsed = yaml.safe_load(textwrap.dedent(block)) or []
            removed_url = (parsed[0] if isinstance(parsed, list) and parsed else {}).get("url", "")
            _ledger_remove(agent, removed_url)
        return
    raise UserSourceError(f"No source named {name!r} for this agent.")


def forget_in_registry(agent: str, url: str, registry_path: Path | None = None) -> int:
    """Drop registry rows for a user-added URL.

    The registry otherwise keeps a removed source as ``deprecated``, and the agents'
    refresh policy skips deprecated sources, so re-adding the same URL later would be
    silently ignored. User-added sources have no curated history worth keeping.
    """
    from core.source_registry import SOURCE_REGISTRY_PATH, _load_registry, _save_registry

    path = registry_path or SOURCE_REGISTRY_PATH
    payload = _load_registry(path)
    entries = payload.get("entries") or []
    kept = [e for e in entries
            if not (e.get("agent_name") == agent and _url_key(e.get("url")) == _url_key(url))]
    if len(kept) != len(entries):
        payload["entries"] = kept
        _save_registry(payload, path)
    return len(entries) - len(kept)


# ---------------------------------------------------------------- permanence ledger
# User-added sources live in the agents' tracked YAML files so agents treat them
# exactly like curated sources. A git checkout/stash/pull can silently revert those
# files, so every approved addition is also recorded here (outputs/ is git-ignored)
# and restored before agents run. Only an explicit removal in the app deletes it.
def _ledger_path() -> Path:
    import os

    return Path(os.environ.get("RIF_USER_SOURCES_LEDGER", PROJECT_ROOT / "outputs" / "workflow" / "user_sources_ledger.json"))


def _read_ledger() -> list[dict[str, Any]]:
    import json

    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_ledger(rows: list[dict[str, Any]]) -> None:
    import json
    import os

    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _ledger_add(row: dict[str, Any]) -> None:
    rows = [r for r in _read_ledger() if not (r.get("agent") == row["agent"] and _url_key(r.get("url")) == _url_key(row["url"]))]
    _write_ledger(rows + [row])


def _ledger_remove(agent: str, url: str) -> None:
    rows = _read_ledger()
    kept = [r for r in rows if not (r.get("agent") == agent and _url_key(r.get("url")) == _url_key(url))]
    if len(kept) != len(rows):
        _write_ledger(kept)


def ensure_user_sources(sources_dir: Path | None = None) -> list[dict[str, Any]]:
    """Re-add ledger sources that are missing from their agent's file. Returns what was restored."""
    restored = []
    for row in _read_ledger():
        agent = row.get("agent")
        if agent not in AGENT_FILES:
            continue
        fname, _, _ = AGENT_FILES[agent]
        path = (sources_dir or SOURCES_DIR) / fname
        try:
            present = any(_url_key(e.get("url")) == _url_key(row.get("url")) for e in _iter_entries(_load(path)))
        except Exception:
            continue
        if present:
            continue
        try:
            add_user_source(agent, row["name"], row["url"], row.get("domains") or [], row.get("focus", ""),
                            sources_dir=sources_dir, _record=False, _added_at=row.get("added_at"))
            restored.append(row)
        except Exception:
            continue
    return restored


def sync_registry_for(agent: str, url: str | None = None) -> None:
    """Refresh the shared source registry for one agent (project tool).

    With ``url``, any stale row for it is dropped first so the sync creates a fresh,
    active entry (add) or none at all (remove).
    """
    from core.source_registry import sync_source_registry

    if url:
        forget_in_registry(agent, url)
    sync_source_registry(agent_filter=agent)


# ---------------------------------------------------------------- immediate test collection
def test_collect(agent: str, url: str, sources_dir: Path | None = None, timeout_seconds: int = 240) -> dict[str, Any]:
    """Run the agent's own collection code on one configured source, as a pipeline run would.

    The entry is copied from the agent's real source file into a temporary file with
    only that entry, so the agent uses the same loader, crawler, limits and filters
    as for its built-in sources. Returns what actually happened; nothing is saved.
    """
    import importlib
    import tempfile
    import time

    from core.crawl_context import CrawlContext, current_crawl_context

    fname, root_key, layout = AGENT_FILES[agent]
    data = _load((sources_dir or SOURCES_DIR) / fname)
    entry = next((e for e in _iter_entries(data) if _url_key(e.get("url")) == _url_key(url)), None)
    if entry is None:
        return {"ok": False, "error": "The source is not in the agent's file."}
    section = "user_added"
    if layout == "domain_sections":
        section = next((k for k, v in (data.get(root_key) or {}).items() if isinstance(v, list) and entry in v), "rwa")
    payload = {root_key: [entry]} if layout == "list" else {root_key: {section: [entry]}}
    tmp = Path(tempfile.mkdtemp()) / fname
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    ctx = CrawlContext(deadline=time.time() + timeout_seconds)
    token = current_crawl_context.set(ctx)
    warnings: list[str] = []
    started = time.time()
    try:
        if agent == "funding":
            from agents.funding_agent import load_sources
            from agents.funding_collector import collect_auto_sources

            results = []
            for item in collect_auto_sources(load_sources(tmp), None, warnings=warnings, is_test_mode=False):
                results.append(item[0] if isinstance(item, tuple) else item)
            res = results[0] if results else None
            titles = [getattr(o[0] if isinstance(o, tuple) else o, "title", "") for o in (getattr(res, "opportunities", None) or [])]
            return {"ok": True, "pages": getattr(res, "pages_fetched", ctx.pages_fetched), "documents": len(titles),
                    "titles": titles[:8], "status": getattr(res, "final_status", ""), "warnings": warnings[:5],
                    "errors": [str(e)[:200] for e in (getattr(res, "errors", None) or [])][:3],
                    "seconds": round(time.time() - started)}
        module = importlib.import_module({"lab": "agents.lab_agent"}.get(agent, f"agents.{agent}_agent"))
        if agent == "lab":
            docs = module.build_auto_documents(tmp, tmp.parent / "labs_memory.db", None, warnings=warnings)
        else:
            docs = module.build_auto_documents(tmp, None, warnings=warnings)
        titles = [str(getattr(d, "title", "") or getattr(d, "name", "") or getattr(d, "incident_name", "")) for d in docs]
        return {"ok": True, "pages": ctx.pages_fetched, "documents": len(docs), "titles": titles[:8], "status": "",
                "warnings": warnings[:5], "errors": [], "seconds": round(time.time() - started)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}", "pages": ctx.pages_fetched}
    finally:
        current_crawl_context.reset(token)
