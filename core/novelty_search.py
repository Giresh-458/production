import json
import urllib.request
import urllib.parse
import re
from abc import ABC, abstractmethod
from typing import List, Dict, Any

from core.research_schemas import ExistingWork

class ExistingSolutionSearcher(ABC):
    @abstractmethod
    def search(self, problem: str) -> Dict[str, Any]:
        """
        Returns a dict matching:
        {
            "similar_work": list[ExistingWork],
            "strongest_overlap": str,
            "remaining_gap": str,
            "search_coverage": float,
            "novelty_status": str,
            "novelty_confidence": str,
            "search_mode": str
        }
        """
        pass

class LocalKnowledgeBaseSearcher(ExistingSolutionSearcher):
    def search(self, problem: str) -> Dict[str, Any]:
        # A local store is not a novelty oracle. If no real indexed corpus is
        # configured, report that fact instead of manufacturing a search result.
        return {
            "similar_work": [],
            "strongest_overlap": "",
            "remaining_gap": "Novelty cannot be assessed from an unconfigured local corpus.",
            "search_coverage": 0.0,
            "novelty_status": "NO_RESULT",
            "novelty_confidence": "Unassessed",
            "search_mode": "local"
        }

class OpenAlexSearcher(ExistingSolutionSearcher):
    def search(self, problem: str) -> Dict[str, Any]:
        low_prob = (problem or "").strip().lower()
        if not low_prob or low_prob in ("insufficient problem signal", "unknown", "no explicit problem statement found."):
            return {
                "similar_work": [],
                "strongest_overlap": "",
                "remaining_gap": "Insufficient or unresolved technical problem provided; novelty search bypassed.",
                "search_coverage": 0.0,
                "query_executed": False,
                "result_count": 0,
                "novelty_status": "NO_RESULT",
                "novelty_confidence": "Unassessed",
                "search_mode": "external"
            }

        # Check if it's just raw funding metadata
        if low_prob.startswith("title:") and "agency:" in low_prob and "details:" in low_prob:
            return {
                "similar_work": [],
                "strongest_overlap": "",
                "remaining_gap": "Raw funding metadata is not a technical problem; novelty search bypassed.",
                "search_coverage": 0.0,
                "query_executed": False,
                "result_count": 0,
                "novelty_status": "NO_RESULT",
                "novelty_confidence": "Unassessed",
                "search_mode": "external"
            }

        stopwords = {
            "the", "and", "this", "that", "with", "from", "for", "are", "was", "were", 
            "problem", "project", "research", "technology", "system", "evaluation", 
            "development", "data", "analysis", "approach", "method", "results", "based", 
            "using", "study", "new", "proposed", "model", "performance", "design", 
            "application", "novel", "different", "important", "can", "has", "have", 
            "which", "these", "tribal", "colleges", "universities", "program", "agency", 
            "number", "details", "title", "grant", "funding", "opportunity", "award"
        }

        clean_text = re.sub(r'[^a-z0-9\s]', ' ', low_prob)
        words = [w for w in clean_text.split() if w not in stopwords and len(w) > 4]

        if len(words) < 2:
            return {
                "similar_work": [],
                "strongest_overlap": "",
                "remaining_gap": "Problem statement lacks specific technical terms for search.",
                "search_coverage": 0.0,
                "query_executed": False,
                "result_count": 0,
                "novelty_status": "NO_RESULT",
                "novelty_confidence": "Unassessed",
                "search_mode": "external"
            }

        query = " ".join(words[:4])
        
        url = f"https://api.openalex.org/works?search={urllib.parse.quote(query)}&per-page=3"
        try:
            from core.http_client import fetch_json
            data, _ = fetch_json(url, headers={"User-Agent": "RIF-Novelty-Search/3.1"}, timeout=(5.0, 12.0), retries=0)
                
            results = data.get("results", [])
            similar_work = []
            for r in results:
                similar_work.append(ExistingWork(
                    title=r.get("title", ""),
                    url=r.get("id", ""),
                    authors=[a.get("author", {}).get("display_name", "") for a in r.get("authorships", [])],
                    overlap_type="UNKNOWN",
                    limitations=[]
                ))
                
            if similar_work:
                def toks(value: str) -> set[str]:
                    raw_tokens = {x for x in re.findall(r"[a-z0-9]{4,}", (value or "").lower())}
                    return raw_tokens - stopwords
                    
                problem_tokens = toks(problem)
                close = []
                for work in similar_work:
                    work_tokens = toks(work.title)
                    overlap = problem_tokens & work_tokens
                    if len(overlap) >= 3:
                        close.append(work)
                        
                status = "OVERLAP_FOUND" if close else "NO_OVERLAP_IN_SEARCH"
                strongest = close[0].title if close else ""
                return {
                    "similar_work": similar_work,
                    "strongest_overlap": strongest,
                    "remaining_gap": "External search completed; returned works are related search results and are not treated as overlap unless title-level similarity is substantial.",
                    "search_coverage": 1.0,
                    "query_executed": True,
                    "result_count": len(similar_work),
                    "novelty_status": status,
                    "novelty_confidence": "Low" if close else "Unassessed",
                    "search_mode": "external"
                }
            else:
                return {
                    "similar_work": [],
                    "strongest_overlap": "None found externally",
                    "remaining_gap": "No immediate overlap found in external search",
                    "search_coverage": 1.0,
                    "query_executed": True,
                    "result_count": 0,
                    "novelty_status": "NO_RESULT", # DO NOT CONVERT TO NOVEL
                    "novelty_confidence": "Unassessed",
                    "search_mode": "external"
                }
        except Exception:
            return {
                "similar_work": [],
                "strongest_overlap": "Search failed",
                "remaining_gap": "Unknown due to search failure",
                "search_coverage": 0.0,
                "novelty_status": "UNKNOWN",
                "novelty_confidence": "Low",
                "search_mode": "unavailable"
            }

def perform_novelty_search(problem: str, use_external: bool = True) -> Dict[str, Any]:
    searcher = OpenAlexSearcher() if use_external else LocalKnowledgeBaseSearcher()
    return searcher.search(problem)
