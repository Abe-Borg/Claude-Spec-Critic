"""Shared configuration for verification API calls."""

VERIFICATION_MODEL = "claude-sonnet-4-6"
VERIFICATION_MAX_TOKENS = 12_000

WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
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
    "max_uses": 3,
    "user_location": {
        "type": "approximate",
        "country": "US",
        "region": "California",
    },
}
