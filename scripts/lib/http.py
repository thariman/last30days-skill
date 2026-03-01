"""HTTP utilities for last30days skill (stdlib only)."""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse

DEFAULT_TIMEOUT = 30
DEBUG = os.environ.get("LAST30DAYS_DEBUG", "").lower() in ("1", "true", "yes")


def log(msg: str):
    """Log debug message to stderr."""
    if DEBUG:
        sys.stderr.write(f"[DEBUG] {msg}\n")
        sys.stderr.flush()
MAX_RETRIES = 5
RETRY_DELAY = 2.0
USER_AGENT = "last30days-skill/2.1 (Assistant Skill)"


def _parse_proxy_url(proxy_url: str) -> Optional[Tuple[int, str, int, str, str]]:
    """Parse REDDIT_PROXY env var into (type, host, port, username, password).

    Supports: socks5://user:pass@host:port
    """
    try:
        import socks as _socks_module  # noqa: F811
    except ImportError:
        log("PySocks not installed – REDDIT_PROXY ignored")
        return None

    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    proxy_types = {
        "socks5": _socks_module.SOCKS5,
        "socks4": _socks_module.SOCKS4,
    }
    ptype = proxy_types.get(scheme)
    if ptype is None:
        log(f"Unsupported proxy scheme: {scheme}")
        return None

    host = parsed.hostname or ""
    port = parsed.port or 1080
    username = parsed.username or ""
    password = parsed.password or ""

    if not host:
        log("Proxy URL missing host")
        return None

    return (ptype, host, port, username, password)


def _is_reddit_url(url: str) -> bool:
    """Check if a URL points to reddit.com."""
    try:
        host = urlparse(url).hostname or ""
        return host == "reddit.com" or host.endswith(".reddit.com")
    except Exception:
        return False


def _build_proxy_opener(proxy_info: Tuple[int, str, int, str, str]):
    """Build a urllib opener that routes through a SOCKS proxy."""
    import socks
    from sockshandler import SocksiPyHandler

    ptype, host, port, username, password = proxy_info
    handler = SocksiPyHandler(ptype, host, port, True, username, password)
    return urllib.request.build_opener(handler)


class HTTPError(Exception):
    """HTTP request error with status code."""
    def __init__(self, message: str, status_code: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = MAX_RETRIES,
) -> Dict[str, Any]:
    """Make an HTTP request and return JSON response.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        headers: Optional headers dict
        json_data: Optional JSON body (for POST)
        timeout: Request timeout in seconds
        retries: Number of retries on failure

    Returns:
        Parsed JSON response

    Raises:
        HTTPError: On request failure
    """
    headers = headers or {}
    headers.setdefault("User-Agent", USER_AGENT)

    data = None
    if json_data is not None:
        data = json.dumps(json_data).encode('utf-8')
        headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    # Use SOCKS proxy for Reddit URLs when REDDIT_PROXY is set
    proxy_env = os.environ.get("REDDIT_PROXY", "")
    proxy_opener = None
    if proxy_env and _is_reddit_url(url):
        proxy_info = _parse_proxy_url(proxy_env)
        if proxy_info:
            proxy_opener = _build_proxy_opener(proxy_info)
            log(f"Routing through SOCKS proxy: {proxy_info[1]}:{proxy_info[2]}")

    url_open = proxy_opener.open if proxy_opener else urllib.request.urlopen

    log(f"{method} {url}")
    if json_data:
        log(f"Payload keys: {list(json_data.keys())}")

    last_error = None
    for attempt in range(retries):
        try:
            with url_open(req, timeout=timeout) as response:
                body = response.read().decode('utf-8')
                log(f"Response: {response.status} ({len(body)} bytes)")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = None
            try:
                body = e.read().decode('utf-8')
            except:
                pass
            log(f"HTTP Error {e.code}: {e.reason}")
            if body:
                log(f"Error body: {body[:500]}")
            last_error = HTTPError(f"HTTP {e.code}: {e.reason}", e.code, body)

            # Don't retry client errors (4xx) except rate limits
            if 400 <= e.code < 500 and e.code != 429:
                raise last_error

            if attempt < retries - 1:
                if e.code == 429:
                    # Respect Retry-After header, fall back to exponential backoff
                    retry_after = e.headers.get("Retry-After") if hasattr(e, 'headers') else None
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            delay = RETRY_DELAY * (2 ** attempt) + 1
                    else:
                        delay = RETRY_DELAY * (2 ** attempt) + 1  # 2s, 5s, 9s...
                    log(f"Rate limited (429). Waiting {delay:.1f}s before retry {attempt + 2}/{retries}")
                else:
                    delay = RETRY_DELAY * (2 ** attempt)
                time.sleep(delay)
        except urllib.error.URLError as e:
            log(f"URL Error: {e.reason}")
            last_error = HTTPError(f"URL Error: {e.reason}")
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
        except json.JSONDecodeError as e:
            log(f"JSON decode error: {e}")
            last_error = HTTPError(f"Invalid JSON response: {e}")
            raise last_error
        except (OSError, TimeoutError, ConnectionResetError) as e:
            # Handle socket-level errors (connection reset, timeout, etc.)
            log(f"Connection error: {type(e).__name__}: {e}")
            last_error = HTTPError(f"Connection error: {type(e).__name__}: {e}")
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))

    if last_error:
        raise last_error
    raise HTTPError("Request failed with no error details")


def get(url: str, headers: Optional[Dict[str, str]] = None, **kwargs) -> Dict[str, Any]:
    """Make a GET request."""
    return request("GET", url, headers=headers, **kwargs)


def post(url: str, json_data: Dict[str, Any], headers: Optional[Dict[str, str]] = None, **kwargs) -> Dict[str, Any]:
    """Make a POST request with JSON body."""
    return request("POST", url, headers=headers, json_data=json_data, **kwargs)


def get_reddit_json(path: str, timeout: int = DEFAULT_TIMEOUT, retries: int = MAX_RETRIES) -> Dict[str, Any]:
    """Fetch Reddit thread JSON.

    Args:
        path: Reddit path (e.g., /r/subreddit/comments/id/title)
        timeout: HTTP timeout per attempt in seconds
        retries: Number of retries on failure

    Returns:
        Parsed JSON response
    """
    # Ensure path starts with /
    if not path.startswith('/'):
        path = '/' + path

    # Remove trailing slash and add .json
    path = path.rstrip('/')
    if not path.endswith('.json'):
        path = path + '.json'

    url = f"https://www.reddit.com{path}?raw_json=1"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    return get(url, headers=headers, timeout=timeout, retries=retries)
