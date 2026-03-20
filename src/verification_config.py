"""Shared configuration for verification API calls."""

VERIFICATION_MODEL = "claude-sonnet-4-6"
VERIFICATION_MAX_TOKENS = 20_000

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "allowed_domains": [
        "iccsafe.org",
        "nfpa.org",
        "ashrae.org",
        "dgs.ca.gov",
        "bsc.ca.gov",
        "energy.ca.gov",
        "up.codes",
        "archive.org",
    ],
    "blocked_domains": [
        "reddit.com",
        "quora.com",
        "medium.com",
        "chatgpt.com",
        "perplexity.ai",
    ],
    "max_uses": 5,
    "user_location": {
        "type": "approximate",
        "country": "US",
        "region": "California",
    },
}
