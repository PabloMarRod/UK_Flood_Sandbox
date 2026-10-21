"""Network setup shared by all downloads.

- Identifies the app clearly (OpenStreetMap and others may refuse anonymous or generic clients).
- Uses the operating system's certificate store when the 'truststore' package is installed, so downloads also work
  on networks that inspect secure connections (common on university and corporate networks).
"""
import requests

USER_AGENT = "UKFloodSandbox/1.0 (local flood modelling app; https://github.com/)"

try:
    import truststore
    truststore.inject_into_ssl()
    TRUSTSTORE = True
except Exception:
    TRUSTSTORE = False

_orig = requests.Session.request


def _request(self, method, url, **kw):
    headers = kw.pop("headers", None) or {}
    headers.setdefault("User-Agent", USER_AGENT)
    return _orig(self, method, url, headers=headers, **kw)


requests.Session.request = _request       # applies to requests.get/post/head and sessions
