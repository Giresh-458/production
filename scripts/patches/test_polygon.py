from agents.funding_agent import run_agent
import json
import logging
logging.basicConfig(level=logging.ERROR)
res = run_agent(mode="manual_url", input_data={"url": "https://polygon.technology/village/startups"})
print(json.dumps(res, indent=2))
