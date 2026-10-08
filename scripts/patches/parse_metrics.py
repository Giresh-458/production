import json
import glob
import os
from pathlib import Path

# Find the most recent pipeline run
run_dirs = sorted(glob.glob('outputs/pipeline_run/*'), key=os.path.getmtime)
if not run_dirs:
    print('No pipeline run found.')
    exit(1)

latest_run = run_dirs[-1]
summary_file = Path(latest_run) / 'workflow' / 'pipeline_summary.json'

if not summary_file.exists():
    print(f'Summary file not found: {summary_file}')
    exit(1)

with open(summary_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

# Extract metrics
run_id = data.get('run_id')
valid_calls = data.get('valid_calls_found', 0)
shared_stats = data.get('shared_crawl_stats', {})

unique_urls = shared_stats.get('unique_urls_processed', 0)
network_fetches = shared_stats.get('actual_fetches', 0)
cache_hits = shared_stats.get('cache_hits', 0)
llm_calls = shared_stats.get('llm_calls', 'Not explicitly tracked')

total_runtime = shared_stats.get('uptime_seconds', 0)

reports = data.get('reports', [])
successful_calls = sum(1 for r in reports if r.get('status') == 'completed')
failed_calls = sum(1 for r in reports if r.get('status') != 'completed')
final_proposals = data.get('total_proposals', 0)

print(f'--- LIVE RUN METRICS ({run_id}) ---')
print(f'Funding calls discovered: {valid_calls}')
print(f'Unique URLs processed: {unique_urls}')
print(f'Actual network fetches: {network_fetches}')
print(f'Cache hits: {cache_hits}')
print(f'Successful calls processed downstream: {successful_calls}')
print(f'Failed/partial calls downstream: {failed_calls}')
print(f'Final proposals generated: {final_proposals}')
