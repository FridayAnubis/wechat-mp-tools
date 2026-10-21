import json
from urllib.request import ProxyHandler, build_opener

with build_opener(ProxyHandler({})).open("http://127.0.0.1:5200/api/health", timeout=3) as response:
    health = json.load(response)
assert health["status"] == "ok" and health["rss_scheduler"] is True
