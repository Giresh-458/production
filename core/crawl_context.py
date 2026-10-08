import time
from dataclasses import dataclass, field
from typing import Optional
import contextvars

# Global Safety Limits
GLOBAL_MAX_PAGES = 30
GLOBAL_MAX_DOCUMENTS = 30
GLOBAL_MAX_DEPTH = 3
GLOBAL_MAX_SECONDS = 300
GLOBAL_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

@dataclass
class CrawlContext:
    deadline: Optional[float] = None
    
    # Global tracking
    pages_fetched: int = 0
    documents_collected: int = 0
    bytes_downloaded: int = 0
    start_time: float = field(default_factory=time.time)
    stop_reasons: set[str] = field(default_factory=set)
    playwright_used: int = 0
    playwright_fallback_failed: int = 0
    browser_attempts: int = 0
    browser_successes: int = 0
    browser_failures: int = 0
    js_shells_detected: int = 0
    http_requests: int = 0
    browser_escalations: int = 0
    
    urls_discovered: int = 0
    urls_scheduled: int = 0
    urls_fetched: int = 0
    urls_rejected_after_deadline: int = 0

    exceptions: list[str] = field(default_factory=list)
    source_limits_telemetry: list[dict] = field(default_factory=list)
    
    # Per-source tracking (reset for each source)
    source_pages_fetched: int = 0
    source_documents_collected: int = 0
    source_start_time: float = field(default_factory=time.time)
    
    lock: __import__('threading').Lock = field(default_factory=__import__('threading').Lock, init=False, repr=False)
    
    def begin_source(self):
        with self.lock:
            self.source_pages_fetched = 0
            self.source_documents_collected = 0
            self.source_start_time = time.time()
        
    def check_limits(self, 
                     source_max_pages: Optional[int] = None, 
                     source_max_documents: Optional[int] = None, 
                     source_max_depth: Optional[int] = None, 
                     source_max_seconds: Optional[int] = None,
                     current_depth: int = 0) -> bool:
        """Returns True if limits are reached."""
        
        now = time.time()
        with self.lock:
            if self.deadline and now >= self.deadline:
                self.stop_reasons.add("deadline")
                return True
                
            # Global limits
            if now - self.start_time >= GLOBAL_MAX_SECONDS:
                self.stop_reasons.add("global_max_seconds")
                return True
            if self.pages_fetched >= GLOBAL_MAX_PAGES:
                self.stop_reasons.add("global_max_pages")
                return True
            if self.documents_collected >= GLOBAL_MAX_DOCUMENTS:
                self.stop_reasons.add("global_max_documents")
                return True
            if self.bytes_downloaded >= GLOBAL_MAX_BYTES:
                self.stop_reasons.add("global_max_bytes")
                return True
                
            # Source limits (min of source config and global default)
            effective_pages = min(source_max_pages or GLOBAL_MAX_PAGES, GLOBAL_MAX_PAGES)
            if self.source_pages_fetched >= effective_pages:
                self.stop_reasons.add("source_max_pages")
                return True
                
            effective_documents = min(source_max_documents or GLOBAL_MAX_DOCUMENTS, GLOBAL_MAX_DOCUMENTS)
            if self.source_documents_collected >= effective_documents:
                self.stop_reasons.add("source_max_documents")
                return True
                
            effective_seconds = min(source_max_seconds or GLOBAL_MAX_SECONDS, GLOBAL_MAX_SECONDS)
            if now - self.source_start_time >= effective_seconds:
                self.stop_reasons.add("source_max_seconds")
                return True
                
            effective_depth = min(source_max_depth or GLOBAL_MAX_DEPTH, GLOBAL_MAX_DEPTH)
            if current_depth >= effective_depth:
                # We don't add a stop reason for depth, we just skip that branch
                pass 
                
            return False
        
    def add_page(self, size_bytes: int = 0):
        with self.lock:
            self.pages_fetched += 1
            self.source_pages_fetched += 1
            self.bytes_downloaded += size_bytes
        
    def add_document(self):
        with self.lock:
            self.documents_collected += 1
            self.source_documents_collected += 1

current_crawl_context = contextvars.ContextVar("current_crawl_context", default=None)
