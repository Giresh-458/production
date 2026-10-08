import logging
import json
from agents.funding_agent import run_agent

logging.basicConfig(level=logging.ERROR)
res = run_agent(mode="configured_scan", area="RWA")
print(json.dumps(res, indent=2))
