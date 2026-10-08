import pytest
import time
import tracemalloc
import concurrent.futures
from core.semantic_retrieval import _get_embedding, _EMBEDDING_CACHE, get_embedding_model

def test_embedding_cache_behavior():
    # Clear cache
    _EMBEDDING_CACHE.clear()
    
    # 1 init, 1 compute
    t0 = time.time()
    _get_embedding("Test content same", "hash-same")
    t1 = time.time()
    
    assert len(_EMBEDDING_CACHE) == 1
    
    # 99 hits
    for _ in range(99):
        _get_embedding("Test content same", "hash-same")
    
    assert len(_EMBEDDING_CACHE) == 1
    
    # Different content
    _get_embedding("Test content diff", "hash-diff")
    assert len(_EMBEDDING_CACHE) == 2

def test_concurrent_embedding_calls():
    _EMBEDDING_CACHE.clear()
    
    def worker(i):
        _get_embedding(f"Test content {i%5}", f"hash-{i%5}")
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(worker, range(100)))
        
    assert len(_EMBEDDING_CACHE) == 5

def test_memory_stress_behavior(capsys):
    _EMBEDDING_CACHE.clear()
    model = get_embedding_model() # ensure initialized
    
    tracemalloc.start()
    t0 = time.time()
    
    # Realistically sized fixture: 1000 records
    record_count = 1000
    for i in range(record_count):
        _get_embedding(f"This is a reasonably long problem statement {i} to test memory usage of the cache.", f"stress-hash-{i}")
        
    t1 = time.time()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    emb_dim = len(next(iter(_EMBEDDING_CACHE.values())))
    
    with capsys.disabled():
        print(f"\n--- STRESS TEST REPORT ---")
        print(f"record_count: {record_count}")
        print(f"embedding_dimensions: {emb_dim}")
        print(f"peak memory if measurable: {peak / 1024 / 1024:.2f} MB")
        print(f"execution time: {t1 - t0:.2f} seconds")
        print(f"--------------------------\n")
