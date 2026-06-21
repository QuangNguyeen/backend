import json

from serpapi import SerpApiClient

# NOTE: this venv has the legacy `google-search-results` package installed,
# which exposes `SerpApiClient` (pass `engine` via params) rather than the
# newer `serpapi.Client(...).search(...)` API.
params = {
    "engine": "youtube_video_transcript",
    "v": "vT08R58R_nk",
    "language_code": "en",
    "api_key": "d87bef64dfb332fc261dc09abd110e5eb85e0855040fd37410fce9db585ba74a",
}

results = SerpApiClient(params).get_dict()

print("status:", results.get("search_metadata", {}).get("status"))
if "error" in results:
    print("error:", results["error"])

transcript = results.get("transcript", [])
print("transcript segments:", len(transcript))
print(json.dumps(transcript[:5], indent=2, ensure_ascii=False))