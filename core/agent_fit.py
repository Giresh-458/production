"""Which collection agent does a web page suit, and which page on a site suits an agent?

Transparent scoring (no LLM): each agent has strong phrases, weak words and URL-path
hints. A page's score for an agent combines distinct matches in its title (counted
twice), its text, and its URL. Results always carry the matched evidence so the UI
can show *why* a page fits.

Used by the "Add source" page to (1) check that a URL fits the chosen agent,
(2) trace links on the same site to the page that fits better, and
(3) suggest an agent when the user lets the app decide.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urldefrag, urljoin, urlsplit

AGENT_SIGNALS: dict[str, dict[str, tuple[str, ...]]] = {
    "funding": {
        "strong": ("call for proposals", "calls for proposals", "funding opportunit", "grant program", "grants program",
                   "grant programme", "apply for funding", "request for proposals", "rfp", "open call", "funding call",
                   "grant application", "application deadline", "research grant", "ecosystem grant", "fellowship programme"),
        "weak": ("grant", "grants", "funding", "fellowship", "award", "apply", "deadline", "eligibility", "budget", "proposal"),
        "path": ("grant", "funding", "fund", "call", "rfp", "opportunit", "apply", "fellowship", "propgf"),
    },
    "hackathon": {
        "strong": ("hackathon", "hackathons", "bounty", "bounties", "prize pool", "buildathon", "hack week",
                   "submission deadline", "judging criteria", "hacker house"),
        "weak": ("prize", "prizes", "tracks", "builders", "teams", "challenge", "demo day", "submissions", "judges"),
        "path": ("hackathon", "hack", "bount", "challenge", "earn", "events"),
    },
    "regulation": {
        "strong": ("regulation (eu)", "directive (eu)", "regulatory framework", "compliance requirements", "supervisory",
                   "rulemaking", "consultation paper", "regulatory guidance", "standardisation", "standardization",
                   "technical committee", "w3c recommendation", "legislation", "reporting standards",
                   "disclosure framework", "reporting framework", "assessment framework"),
        "weak": ("regulation", "regulator", "regulatory", "law", "legal", "policy", "compliance", "obligation",
                 "shall", "standard", "standards", "directive", "specification", "guidelines", "framework",
                 "principles", "disclosure", "guidance", "methodology", "criteria", "recommendations"),
        "path": ("regulation", "legal", "law", "standard", "policy", "eli", "celex", "committee", "tr/", "rules"),
    },
    "company": {
        "strong": ("book a demo", "request a demo", "contact sales", "our customers", "case studies", "trusted by",
                   "get started", "our platform", "for enterprises", "pricing"),
        "weak": ("product", "products", "solution", "solutions", "platform", "customers", "partners", "enterprise",
                 "careers", "use cases"),
        "path": ("product", "solution", "pricing", "platform", "customers", "use-case", "enterprise"),
    },
    "investment": {
        "strong": ("portfolio companies", "our portfolio", "venture capital", "we invest", "led the round", "series a",
                   "seed round", "investment thesis", "limited partners", "fund i", "backed by"),
        "weak": ("portfolio", "invest", "investing", "investors", "venture", "fund", "raised", "round", "capital", "thesis"),
        "path": ("portfolio", "invest", "venture", "thesis", "insights", "research"),
    },
    "failure": {
        "strong": ("post-mortem", "postmortem", "incident report", "root cause", "exploit", "funds stolen",
                   "attack vector", "vulnerability disclosure", "hacks database", "security incident", "rekt",
                   "security research", "audit report", "smart contract security", "exploit analysis"),
        "weak": ("incident", "incidents", "hack", "hacked", "vulnerability", "attack", "outage", "stolen", "drained",
                 "loss", "audit", "fraud", "double counting", "security", "risk"),
        "path": ("incident", "postmortem", "post-mortem", "hacks", "exploit", "security", "rekt", "audit"),
    },
    "data_availability": {
        "strong": ("dataset", "datasets", "benchmark", "open data", "data portal", "download data", "api documentation",
                   "data explorer", "registry", "test suite", "csv", "transparency report", "attestation",
                   "proof of reserve", "reserve report", "simulation scenario"),
        "weak": ("data", "dashboard", "analytics", "metrics", "download", "api", "explorer", "statistics", "records"),
        "path": ("data", "dataset", "benchmark", "explorer", "registry", "api", "dashboard", "stats", "transparency"),
    },
    "expert": {
        "strong": ("associate professor", "assistant professor", "professor", "faculty", "phd students", "google scholar",
                   "orcid", "curriculum vitae", "research interests", "h-index", "selected publications"),
        "weak": ("publications", "researcher", "department", "university", "lecturer", "cv", "students", "author"),
        "path": ("people", "faculty", "~", "profile", "author", "staff", "person"),
    },
    "lab": {
        "strong": ("research group", "research lab", "research laboratory", "our research", "research projects",
                   "lab members", "principal investigator", "research areas", "research centre", "research center"),
        "weak": ("lab", "laboratory", "research", "projects", "publications", "group", "centre", "center", "institute"),
        "path": ("lab", "research", "group", "projects", "centre", "center", "institute"),
    },
    "opensource": {
        "strong": ("pull requests", "open source", "contributing", "repository", "issues", "releases", "license"),
        "weak": ("github", "code", "commit", "sdk", "library", "fork", "stars"),
        "path": (),
    },
    "practitioner": {
        "strong": ("community forum", "developer forum", "engineering blog", "developer blog", "discussion", "tutorial",
                   "stack overflow", "replies", "latest topics", "q&a", "job board", "jobs", "we are hiring"),
        "weak": ("developer", "developers", "blog", "community", "forum", "thread", "guide", "docs", "posts", "questions"),
        "path": ("forum", "blog", "discuss", "community", "docs", "writings", "t/", "questions", "posts", "job"),
    },
}
AGENTS = tuple(AGENT_SIGNALS)
_CODE_HOSTS = ("github.com", "gitlab.com", "codeberg.org")
_PROGRAMME_AGENTS = {"funding", "hackathon"}
_ARTICLE_PATH = re.compile(r"/(?:blog|news|research|insights|posts?|articles?|stories|press|media|writing)/|/20\d\d/\d\d/", re.I)
_SKIP_LINK = re.compile(r"^(?:mailto:|tel:|javascript:)|\.(?:png|jpe?g|gif|svg|webp|ico|css|js|zip|gz|mp4|mp3|woff2?)(?:[?#].*)?$", re.I)
_LOGIN_PATH = re.compile(r"/(?:login|signin|sign-in|signup|sign-up|register|cart|account|privacy|cookie|terms)\b", re.I)


@dataclass
class AgentFit:
    agent: str
    score: float
    evidence: list[str] = field(default_factory=list)


def _phrase_present(text: str, phrase: str) -> bool:
    if len(phrase) <= 4 or phrase in {"grant", "grants", "fund", "hack", "lab", "data", "law", "loss"}:
        return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None
    return phrase in text


def score_agents(url: str, title: str = "", text: str = "") -> list[AgentFit]:
    """Score every agent for a page; highest first. Distinct matches only (no keyword stuffing)."""
    title_l = (title or "").lower()
    text_l = re.sub(r"\s+", " ", (text or "").lower())[:60000]
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    path = (parts.path or "").lower()
    fits = []
    for agent, signals in AGENT_SIGNALS.items():
        score, evidence = 0.0, []
        for phrase in signals["strong"]:
            in_title, in_text = _phrase_present(title_l, phrase), _phrase_present(text_l, phrase)
            if in_title or in_text:
                score += 3 + (3 if in_title else 0)
                evidence.append(f'"{phrase}"' + (" (title)" if in_title else ""))
        for word in signals["weak"]:
            in_title, in_text = _phrase_present(title_l, word), _phrase_present(text_l, word)
            if in_title or in_text:
                score += 1 + (1 if in_title else 0)
                evidence.append(word)
        hits = [hint for hint in signals["path"] if hint and hint in path]
        if hits:
            score += 4
            evidence.append(f"URL path: {hits[0]}")
        if agent in _PROGRAMME_AGENTS and _ARTICLE_PATH.search(path) and score > 0:
            # Calls and hackathons are programme pages; articles only discuss them.
            score = max(0.0, score - 6)
            evidence.append("article page (penalised)")
        if agent == "opensource":
            if any(host == h or host.endswith("." + h) for h in _CODE_HOSTS):
                score += 10
                evidence.append(f"code host: {host}")
            else:
                # Words like "open source"/"issues"/"license" sit in footers of any site;
                # the open-source agent only collects code-host repositories.
                score, evidence = 0.0, ["not a code-host repository"]
        fits.append(AgentFit(agent, score, evidence))
    return sorted(fits, key=lambda fit: -fit.score)


UNDETERMINED_BELOW = 4.0


def best_agents(fits: list[AgentFit], top: int = 3) -> list[AgentFit]:
    """Top agents, or [] when the page carries too little evidence to tell (e.g. JS-only apps)."""
    if not fits or fits[0].score < UNDETERMINED_BELOW:
        return []
    return [fit for fit in fits[:top] if fit.score >= UNDETERMINED_BELOW]


def fits_agent(fits: list[AgentFit], agent: str, *, min_score: float = 8.0, min_share: float = 0.6) -> bool:
    """True when `agent` scores well in absolute terms and is close to the best agent."""
    by_agent = {fit.agent: fit for fit in fits}
    best = max((fit.score for fit in fits), default=0.0)
    own = by_agent.get(agent).score if agent in by_agent else 0.0
    return own >= min_score and best > 0 and own >= min_share * best


@dataclass
class PageCandidate:
    url: str
    title: str
    score: float
    evidence: list[str]
    note: str = ""


def _same_site(host: str, base: str) -> bool:
    host, base = host.lower().removeprefix("www."), base.lower().removeprefix("www.")
    return host == base or host.endswith("." + base) or base.endswith("." + host)


def candidate_links(html: str, base_url: str, agent: str, limit: int = 6) -> list[tuple[str, str, float]]:
    """Same-site links whose anchor text or URL suggest a page for `agent`, best first."""
    from bs4 import BeautifulSoup

    signals = AGENT_SIGNALS[agent]
    base_host = urlsplit(base_url).hostname or ""
    seen, ranked = set(), []
    for anchor in BeautifulSoup(html or "", "html.parser").find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith("#") or _SKIP_LINK.search(href):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"} or not _same_site(parts.hostname or "", base_host):
            continue
        if _LOGIN_PATH.search(parts.path or "") or absolute.rstrip("/") == base_url.rstrip("/"):
            continue
        key = absolute.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).lower()[:120]
        path = (parts.path or "").lower()
        score = 0.0
        score += sum(3 for p in signals["strong"] if _phrase_present(label, p))
        score += sum(1 for w in signals["weak"] if _phrase_present(label, w))
        score += sum(2 for h in signals["path"] if h and h in path)
        if score > 0:
            ranked.append((absolute, label, score))
    ranked.sort(key=lambda item: -item[2])
    return ranked[:limit]


def trace_pages(url: str, agent: str, fetch: Callable[[str], Any] | None = None, max_fetch: int = 5) -> list[PageCandidate]:
    """Fetch the page, follow its most promising same-site links, and rank pages for `agent`."""
    if fetch is None:
        from core.shared_crawl_manager import SharedCrawlManager

        manager = SharedCrawlManager.get()
        fetch = lambda target: manager.fetch(target)  # noqa: E731
    candidates: list[PageCandidate] = []
    start = fetch(url)
    if getattr(start, "error", None):
        return [PageCandidate(url, "", 0.0, [], note=f"could not fetch: {start.error}")]
    start_url = getattr(start, "final_url", "") or url
    own = next(f for f in score_agents(start_url, start.title, start.clean_text) if f.agent == agent)
    candidates.append(PageCandidate(start_url, start.title, own.score, own.evidence, note="the page you gave"))
    for link, label, _link_score in candidate_links(start.html, start_url, agent, limit=max_fetch):
        page = fetch(link)
        if getattr(page, "error", None):
            continue
        fit = next(f for f in score_agents(getattr(page, "final_url", "") or link, page.title, page.clean_text) if f.agent == agent)
        candidates.append(PageCandidate(getattr(page, "final_url", "") or link, page.title, fit.score, fit.evidence,
                                        note=f'linked as "{label[:60]}"'))
    unique: dict[str, PageCandidate] = {}
    for candidate in candidates:
        key = candidate.url.rstrip("/").lower()
        if key not in unique or candidate.score > unique[key].score:
            unique[key] = candidate
    return sorted(unique.values(), key=lambda c: -c.score)
