#!/usr/bin/env python3
"""
Local testing API for resolving embed pages into final stream URLs.

This file is intentionally dependency-light. It first tries ordinary Python HTTP
requests, then falls back to the saved capture files in this folder. It does not
try to bypass CAPTCHA, DRM, paywalls, or access controls.

Run:
    python web_dev_learing_testing.py --serve --port 8787

Use:
    http://127.0.0.1:8787/resolve?url=https%3A%2F%2Fmultiembed.mov%2F%3Fvideo_id%3D280%26tmdb%3D1
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


CAPTURE_DIR = Path(__file__).resolve().parent
DEFAULT_TIMEOUT = 20
STREAMINGNOW_BASE = "https://streamingnow.mov"
DEFAULT_INPUT_URL = "https://multiembed.mov/?video_id=45050&tmdb=1"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


STREAM_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mpd|mp4)(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)
PLAYER_FILE_RE = re.compile(
    r"""(?:file|src)\s*:\s*(['"])(?P<url>https?://.*?\.(?:m3u8|mpd|mp4)(?:\?.*?)?)\1""",
    re.IGNORECASE | re.DOTALL,
)
PLAY_TOKEN_RE = re.compile(r"""[?&]play=([^&"'<>]+)""", re.IGNORECASE)
LOAD_SOURCES_RE = re.compile(r"""load_sources\((['"])(?P<token>[^'"]+)\1\)""")
IFRAME_SRC_RE = re.compile(r"""<iframe\b[^>]*\bsrc=(['"])(?P<src>.*?)\1""", re.IGNORECASE | re.DOTALL)
SOURCE_LI_RE = re.compile(r"""<li\b(?P<attrs>[^>]*\bdata-id=[^>]*)>""", re.IGNORECASE | re.DOTALL)
ATTR_RE = re.compile(r"""([:\w-]+)\s*=\s*(['"])(.*?)\2""", re.DOTALL)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class SourceChoice:
    video_id: str
    server_id: str
    label: str = ""
    quality: str = ""


@dataclass
class ResolveResult:
    input_url: str
    ok: bool
    status: str
    play_url: Optional[str] = None
    play_token: Optional[str] = None
    sources: List[SourceChoice] = field(default_factory=list)
    embed_urls: List[str] = field(default_factory=list)
    stream_urls: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    used_capture: bool = False
    used_live_http: bool = False

    def to_jsonable(self) -> Dict[str, Any]:
        data = asdict(self)
        data["sources"] = [asdict(item) for item in self.sources]
        return data


def unique_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def request_headers(referer: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def http_get(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    referer: Optional[str] = None,
    allow_redirects: bool = True,
) -> Tuple[int, str, Dict[str, str], str]:
    opener = urllib.request.build_opener() if allow_redirects else urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers=request_headers(referer))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.geturl(), dict(resp.headers.items()), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, url, dict(exc.headers.items()), body


def http_post_form(
    url: str,
    form: Dict[str, str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    referer: Optional[str] = None,
) -> Tuple[int, str, Dict[str, str], str]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    headers = request_headers(referer)
    headers.update(
        {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": f"{urllib.parse.urlsplit(url).scheme}://{urllib.parse.urlsplit(url).netloc}",
        }
    )
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
        return resp.status, resp.geturl(), dict(resp.headers.items()), text


def attrs_to_dict(raw_attrs: str) -> Dict[str, str]:
    return {name.lower(): html.unescape(value) for name, _, value in ATTR_RE.findall(raw_attrs)}


def extract_play_token(url_or_html: str) -> Optional[str]:
    match = PLAY_TOKEN_RE.search(url_or_html)
    if match:
        return urllib.parse.unquote(match.group(1))
    match = LOAD_SOURCES_RE.search(url_or_html)
    if match:
        return match.group("token")
    return None


def extract_stream_urls(text: str) -> List[str]:
    urls = [html.unescape(m.group("url")) for m in PLAYER_FILE_RE.finditer(text)]
    urls.extend(html.unescape(m.group(0)) for m in STREAM_URL_RE.finditer(text))
    return unique_keep_order(urls)


def extract_iframe_urls(text: str, base_url: str) -> List[str]:
    urls = []
    for match in IFRAME_SRC_RE.finditer(text):
        src = html.unescape(match.group("src")).strip()
        if src:
            urls.append(urllib.parse.urljoin(base_url, src))
    return unique_keep_order(urls)


def clean_text(fragment: str) -> str:
    fragment = re.sub(r"<script\b.*?</script>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<style\b.*?</style>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(html.unescape(fragment).split())


def extract_source_choices(response_html: str) -> List[SourceChoice]:
    sources = []
    matches = list(SOURCE_LI_RE.finditer(response_html))
    for index, match in enumerate(matches):
        attrs = attrs_to_dict(match.group("attrs"))
        video_id = attrs.get("data-id")
        server_id = attrs.get("data-server")
        if not video_id or not server_id:
            continue

        end = matches[index + 1].start() if index + 1 < len(matches) else response_html.find("</ul>", match.end())
        if end < 0:
            end = min(len(response_html), match.end() + 500)
        fragment = response_html[match.end() : end]
        quality_match = re.search(r"""<span\b[^>]*class=(['"])[^'"]*\bquality\b[^'"]*\1[^>]*>(.*?)</span>""", fragment, re.I | re.S)
        quality = clean_text(quality_match.group(2)) if quality_match else ""
        label = clean_text(fragment)
        sources.append(SourceChoice(video_id=video_id, server_id=server_id, label=label, quality=quality))
    return sources


def choose_source(sources: List[SourceChoice], preferred_server: Optional[str] = None) -> Optional[SourceChoice]:
    if not sources:
        return None
    if preferred_server:
        for source in sources:
            if source.server_id == preferred_server:
                return source
    for wanted in ("89", "88", "90", "92", "91"):
        for source in sources:
            if source.server_id == wanted:
                return source
    return sources[0]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def capture_play_url_for_input(input_url: str, capture_dir: Path) -> Optional[str]:
    wanted = urllib.parse.urlsplit(input_url)
    wanted_query = urllib.parse.parse_qs(wanted.query)
    full_capture = capture_dir / "full_capture.jsonl"

    for event in iter_jsonl(full_capture):
        if event.get("type") != "response":
            continue
        if event.get("status") not in (301, 302, 303, 307, 308):
            continue
        event_url = event.get("url", "")
        parsed = urllib.parse.urlsplit(event_url)
        if parsed.netloc != wanted.netloc or parsed.path != wanted.path:
            continue
        event_query = urllib.parse.parse_qs(parsed.query)
        if wanted_query and event_query != wanted_query:
            continue
        location = event.get("headers", {}).get("location")
        if location:
            return location
    return None


def captured_play_tokens(capture_dir: Path) -> List[str]:
    tokens: List[str] = []
    full_capture = capture_dir / "full_capture.jsonl"
    for event in iter_jsonl(full_capture):
        if event.get("type") != "response":
            continue
        location = event.get("headers", {}).get("location")
        if location:
            token = extract_play_token(location)
            if token:
                tokens.append(token)

    index_html = load_capture_html(capture_dir, "streamingnow.mov_index.html")
    if index_html:
        token = extract_play_token(index_html)
        if token:
            tokens.append(token)
    return unique_keep_order(tokens)


def load_capture_html(capture_dir: Path, name: str) -> str:
    path = capture_dir / "html_pages" / name
    return read_text(path) if path.exists() else ""


def resolve_from_capture(input_url: str, capture_dir: Path, preferred_server: Optional[str] = None) -> ResolveResult:
    result = ResolveResult(input_url=input_url, ok=False, status="capture")
    result.used_capture = True

    result.play_url = capture_play_url_for_input(input_url, capture_dir)
    input_token = extract_play_token(input_url)
    known_tokens = captured_play_tokens(capture_dir)

    if input_token and known_tokens and input_token not in known_tokens:
        result.status = "not_in_capture"
        result.errors.append("The input play token is not present in this capture set.")
        return result

    if not result.play_url and input_token and "streamingnow.mov" in input_url:
        result.play_url = input_url

    if not result.play_url and not input_token:
        result.status = "not_in_capture"
        result.errors.append("This input URL was not found in full_capture.jsonl, so saved streams were not reused.")
        return result

    if result.play_url:
        result.play_token = extract_play_token(result.play_url)
        result.steps.append("capture: found streamingnow play URL from full_capture.jsonl")

    index_html = load_capture_html(capture_dir, "streamingnow.mov_index.html")
    if index_html:
        result.play_token = result.play_token or extract_play_token(index_html)
        result.steps.append("capture: parsed streamingnow index HTML")

    response_html = load_capture_html(capture_dir, "streamingnow.mov_response.php.html")
    if response_html:
        result.sources = extract_source_choices(response_html)
        result.steps.append(f"capture: parsed {len(result.sources)} source choice(s)")

    playvideo_html = load_capture_html(capture_dir, "streamingnow.mov_playvideo.php.html")
    if playvideo_html:
        result.embed_urls.extend(extract_iframe_urls(playvideo_html, STREAMINGNOW_BASE))
        result.stream_urls.extend(extract_stream_urls(playvideo_html))
        result.steps.append("capture: parsed playvideo iframe/stream URLs")

    vip_html = load_capture_html(capture_dir, "streamingnow.mov_vipstream_vfx.php.html")
    if vip_html:
        result.embed_urls.extend(extract_iframe_urls(vip_html, STREAMINGNOW_BASE))
        result.stream_urls.extend(extract_stream_urls(vip_html))
        result.steps.append("capture: parsed vipstream Playerjs stream URLs")

    media_json = capture_dir / "media_urls.json"
    if media_json.exists():
        try:
            media = json.loads(read_text(media_json))
            result.stream_urls.extend(media.get("m3u8_playlists", []))
            result.steps.append("capture: added m3u8_playlists from media_urls.json")
        except Exception as exc:
            result.errors.append(f"capture media_urls parse failed: {exc}")

    result.embed_urls = unique_keep_order(result.embed_urls)
    result.stream_urls = unique_keep_order(result.stream_urls)

    chosen = choose_source(result.sources, preferred_server)
    if chosen:
        result.steps.append(f"capture: preferred source server={chosen.server_id} video_id={chosen.video_id}")

    result.ok = bool(result.stream_urls or result.embed_urls or result.sources)
    result.status = "ok" if result.ok else "not_found"
    if not result.ok:
        result.errors.append("No stream, iframe, or source entries found in capture files.")
    return result


def resolve_live_raw(input_url: str, preferred_server: Optional[str] = None) -> ResolveResult:
    result = ResolveResult(input_url=input_url, ok=False, status="live_raw")
    result.used_live_http = True

    try:
        status, final_url, headers, body = http_get(input_url, allow_redirects=False)
        result.steps.append(f"live: initial GET returned HTTP {status}")

        location = headers.get("Location") or headers.get("location")
        if location:
            result.play_url = urllib.parse.urljoin(input_url, location)
            result.play_token = extract_play_token(result.play_url)
            result.steps.append("live: found redirect Location play URL")
        elif final_url != input_url:
            result.play_url = final_url
            result.play_token = extract_play_token(final_url)
            result.steps.append("live: final URL contains play token")
        else:
            result.play_url = input_url
            result.play_token = extract_play_token(input_url) or extract_play_token(body)

        result.stream_urls.extend(extract_stream_urls(body))
        result.embed_urls.extend(extract_iframe_urls(body, final_url))

        if not result.play_url:
            result.errors.append("Raw HTTP did not find a play URL.")
            return result

        status, final_url, headers, page = http_get(result.play_url, referer=input_url)
        result.steps.append(f"live: play page GET returned HTTP {status}")
        result.play_token = result.play_token or extract_play_token(page)
        result.stream_urls.extend(extract_stream_urls(page))
        result.embed_urls.extend(extract_iframe_urls(page, final_url))

        blocked_markers = ("turnstile", "cf_clearance", "challenge-platform", "captcha")
        if any(marker in page.lower() for marker in blocked_markers):
            result.errors.append("Raw HTTP reached a browser/CAPTCHA challenge; use capture fallback or optional browser mode.")

        if result.play_token:
            response_url = urllib.parse.urljoin(result.play_url, "/response.php")
            status, _, _, response_html = http_post_form(
                response_url,
                {"token": result.play_token},
                referer=result.play_url,
            )
            result.steps.append(f"live: response.php POST returned HTTP {status}")
            result.sources = extract_source_choices(response_html)

            source = choose_source(result.sources, preferred_server)
            if source:
                playvideo_url = urllib.parse.urljoin(
                    result.play_url,
                    f"/playvideo.php?video_id={urllib.parse.quote(source.video_id)}"
                    f"&server_id={urllib.parse.quote(source.server_id)}"
                    f"&token={urllib.parse.quote(result.play_token)}&init=1",
                )
                status, _, _, playvideo_html = http_get(playvideo_url, referer=result.play_url)
                result.steps.append(f"live: playvideo.php GET returned HTTP {status}")
                result.embed_urls.extend(extract_iframe_urls(playvideo_html, playvideo_url))
                result.stream_urls.extend(extract_stream_urls(playvideo_html))

                # If this is a vipstream response, follow its internal iframe/API page.
                for embed_url in list(result.embed_urls):
                    if "vipstream" in embed_url or "streamingnow.mov" in embed_url:
                        status, _, _, embed_html = http_get(embed_url, referer=playvideo_url)
                        result.steps.append(f"live: embed GET returned HTTP {status} for {embed_url}")
                        result.stream_urls.extend(extract_stream_urls(embed_html))

        result.embed_urls = unique_keep_order(result.embed_urls)
        result.stream_urls = unique_keep_order(result.stream_urls)
        result.ok = bool(result.stream_urls or result.embed_urls or result.sources)
        result.status = "ok" if result.ok else "blocked_or_not_found"
        return result
    except Exception as exc:
        result.status = "error"
        result.errors.append(f"{type(exc).__name__}: {exc}")
        return result


async def resolve_with_nodriver(input_url: str, timeout_ms: int = 45000) -> Dict[str, Any]:
    """
    Optional advanced mode using nodriver. Requires:
        pip install nodriver

    It records media URLs seen by a stealthy CDP-controlled browser session.
    """
    try:
        import nodriver as uc
    except ImportError as exc:
        return {"ok": False, "error": f"Required package is not installed: {exc}"}

    import asyncio

    stream_urls: List[str] = []
    page_urls: List[str] = []
    started = time.time()
    browser = None
    captured_error = None

    try:
        # Added browser_args to prevent crashes in Linux CI environments
        browser = await uc.start(
            headless=False,
            browser_args=[
                '--no-sandbox', 
                '--disable-setuid-sandbox', 
                '--disable-gpu',
                '--disable-dev-shm-usage'
            ]
        )
        page = await browser.get('about:blank')

        # Intercept network requests via Chrome DevTools Protocol
        async def request_handler(event: uc.cdp.network.RequestWillBeSent):
            url = event.request.url
            page_urls.append(url)
            if STREAM_URL_RE.search(url):
                stream_urls.append(url)

        page.add_handler(uc.cdp.network.RequestWillBeSent, request_handler)

        await page.get(input_url)

        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline and not stream_urls:
            await asyncio.sleep(1)

    except Exception as e:
        # Capture the error so we can see it in the GitHub Actions log
        captured_error = f"{type(e).__name__}: {str(e)}"
    finally:
        if browser:
            browser.stop()

    return {
        "ok": bool(stream_urls),
        "input_url": input_url,
        "error": captured_error,
        "stream_urls": unique_keep_order(stream_urls),
        "observed_urls": unique_keep_order(page_urls),
        "elapsed_seconds": round(time.time() - started, 2),
    }


def resolve(
    input_url: str,
    *,
    capture_dir: Path = CAPTURE_DIR,
    live: bool = True,
    capture: bool = True,
    preferred_server: Optional[str] = None,
) -> ResolveResult:
    merged = ResolveResult(input_url=input_url, ok=False, status="not_started")

    live_result: Optional[ResolveResult] = None
    if live:
        live_result = resolve_live_raw(input_url, preferred_server)
        merged = live_result
        if live_result.ok and live_result.stream_urls:
            return live_result

    if capture:
        capture_result = resolve_from_capture(input_url, capture_dir, preferred_server)
        if live_result:
            capture_result.used_live_http = live_result.used_live_http
            capture_result.steps = live_result.steps + capture_result.steps
            capture_result.errors = live_result.errors + capture_result.errors
            capture_result.embed_urls = unique_keep_order(live_result.embed_urls + capture_result.embed_urls)
            capture_result.stream_urls = unique_keep_order(live_result.stream_urls + capture_result.stream_urls)
            if not capture_result.play_url:
                capture_result.play_url = live_result.play_url
            if not capture_result.play_token:
                capture_result.play_token = live_result.play_token
            if not capture_result.sources:
                capture_result.sources = live_result.sources
            capture_result.ok = bool(capture_result.stream_urls or capture_result.embed_urls or capture_result.sources)
            capture_result.status = "ok" if capture_result.ok else live_result.status
        return capture_result

    return merged


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "StreamResolverTesting/1.0"

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if parsed.path == "/health":
                self.write_json({"ok": True, "service": "web_dev_learing_testing.py"})
                return

            if parsed.path == "/resolve":
                input_url = (params.get("url") or [""])[0]
                if not input_url:
                    self.write_json({"ok": False, "error": "Missing url query parameter."}, status=400)
                    return
                live = (params.get("live") or ["1"])[0] not in ("0", "false", "False")
                capture = (params.get("capture") or ["1"])[0] not in ("0", "false", "False")
                server_id = (params.get("server") or [None])[0]
                result = resolve(input_url, live=live, capture=capture, preferred_server=server_id)
                self.write_json(result.to_jsonable())
                return

            self.write_json(
                {
                    "ok": False,
                    "error": "Not found.",
                    "endpoints": ["/health", "/resolve?url=<embed-url>&live=1&capture=1&server=89"],
                },
                status=404,
            )
        except Exception as exc:
            self.write_json(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                },
                status=500,
            )

    def log_message(self, fmt: str, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def write_json(self, payload: Dict[str, Any], status: int = 200):
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def serve(host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"Serving on http://{host}:{port}")
    print("Example:")
    print(
        f"http://{host}:{port}/resolve?url="
        + urllib.parse.quote("https://multiembed.mov/?video_id=280&tmdb=1", safe="")
    )
    httpd.serve_forever()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Testing API/extractor for embed stream URLs.")
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_INPUT_URL,
        help=f"Embed URL to resolve, for CLI mode. Default: {DEFAULT_INPUT_URL}",
    )
    parser.add_argument("--serve", action="store_true", help="Start the local JSON API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    parser.add_argument("--capture-dir", default=str(CAPTURE_DIR), help="Folder containing capture JSON/HTML files.")
    parser.add_argument("--no-live", action="store_true", help="Skip live raw HTTP and only use capture files.")
    parser.add_argument("--no-capture", action="store_true", help="Skip capture fallback.")
    parser.add_argument("--server-id", default=None, help="Preferred server id, for example 89.")
    parser.add_argument("--browser", action="store_true", help="Use optional nodriver browser collector.")
    args = parser.parse_args(argv)

    if args.serve:
        serve(args.host, args.port)
        return 0

    if args.browser:
        import asyncio

        payload = asyncio.run(resolve_with_nodriver(args.url))
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if payload.get("ok") else 2

    result = resolve(
        args.url,
        capture_dir=Path(args.capture_dir),
        live=not args.no_live,
        capture=not args.no_capture,
        preferred_server=args.server_id,
    )
    print(json.dumps(result.to_jsonable(), indent=2, ensure_ascii=False))
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
