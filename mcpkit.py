"""mcpkit — the small toolkit shared by the Spiritus site MCP servers
(djinni-mcp, dou-mcp). Python 3 standard library only, any OS.

Each server repo carries its own identical copy of this file, so every
module stays self-contained. Change it in one → copy it to the others.

  McpServer     — MCP over stdio (newline-delimited JSON-RPC): tools only
  SiteSession   — one cookie session per site, persisted between runs,
                  paced and budgeted against bans, stops on anti-bot pages
  load_env      — read KEY=VALUE pairs from the nearest `.env` (env wins)
  html helpers  — html_to_text, clean, inner_html, page_links, page_title
"""

from __future__ import annotations

import html
import http.cookiejar
import json
import mimetypes
import os
import random
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

KIT_VERSION = "1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
BLOCK_MARKERS = (
    "cf-chl-", "challenge-platform", "Just a moment...", "Attention Required!",
    "g-recaptcha", "hcaptcha", "captcha-delivery",
)


class Stop(Exception):
    """The request must not go on (blocked, budget used up, not allowed).
    Shown to the caller as a tool error; never retried automatically."""


def log(msg: str) -> None:
    """stdout is the MCP channel — diagnostics go to stderr only."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------- env ---

def load_env(start: Path, explicit: str | None = None) -> Path | None:
    """Fill os.environ from a `.env` file without overriding what is set.
    `explicit` path wins; otherwise the nearest `.env` walking up from `start`."""
    path = Path(explicit) if explicit else None
    if path is None:
        for folder in [start, *start.parents]:
            if (folder / ".env").is_file():
                path = folder / ".env"
                break
    if not path or not path.is_file():
        return None
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return path


# --------------------------------------------------------------- session ---

class SiteSession:
    """Cookie session for one site, kept in `<state>/cookies.txt` between runs.

    Every request: daily budget check (`<state>/ledger.json`), a jittered pause
    since the previous request (also across tool calls and restarts), anti-bot
    detection. 403 / 429 / captcha → Stop, no retry through a block."""

    def __init__(self, name: str, state_dir: Path, hosts: tuple[str, ...], *,
                 delay: tuple[float, float] = (3.0, 7.0), daily: int = 150):
        self.name, self.hosts, self.delay, self.daily = name, hosts, delay, daily
        self.state = state_dir
        self.state.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.state / "ledger.json"
        self.jar = http.cookiejar.LWPCookieJar(str(self.state / "cookies.txt"))
        if Path(self.jar.filename).exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except (http.cookiejar.LoadError, OSError) as e:
                log(f"cookies unreadable, starting clean: {e}")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        }

    # -- budget ------------------------------------------------------------
    def _ledger(self) -> dict:
        try:
            return json.loads(self.ledger_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def budget(self) -> dict:
        used = self._ledger().get("days", {}).get(date.today().isoformat(), 0)
        return {"daily_limit": self.daily, "used_today": used, "left_today": max(0, self.daily - used)}

    def _spend_and_wait(self) -> None:
        data = self._ledger()
        days = data.setdefault("days", {})
        today = date.today().isoformat()
        if days.get(today, 0) >= self.daily:
            raise Stop(f"daily {self.name} budget ({self.daily} requests) is used up — try tomorrow")
        last = data.get("last_request")
        if last:
            since = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
            pause = random.uniform(*self.delay) - since
            if pause > 0:
                time.sleep(pause)
        days[today] = days.get(today, 0) + 1
        for d in sorted(days)[:-14]:
            del days[d]
        data["last_request"] = datetime.now().isoformat(timespec="seconds")
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        tmp.replace(self.ledger_path)

    # -- cookies -----------------------------------------------------------
    def save_cookies(self) -> None:
        self.jar.save(ignore_discard=True, ignore_expires=True)

    def clear_cookies(self) -> None:
        self.jar.clear()
        self.save_cookies()

    def import_cookie_header(self, header: str, domain: str) -> int:
        """`name=value; name2=value2` (copied from the browser) → the jar."""
        n = 0
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if not name or not value:
                continue
            self.jar.set_cookie(http.cookiejar.Cookie(
                0, name, value, None, False, domain, True, domain.startswith("."), "/", False,
                True, None, False, None, None, {}))
            n += 1
        self.save_cookies()
        return n

    def cookie(self, name: str) -> str | None:
        return next((c.value for c in self.jar if c.name == name), None)

    # -- http --------------------------------------------------------------
    def check_url(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self.hosts:
            raise Stop(f"{url}: only https pages of {', '.join(self.hosts)} are allowed")
        return url

    def request(self, url: str, data: dict | list | None = None, headers: dict | None = None,
                files: list[tuple[str, Path]] | None = None) -> tuple[str, int, str]:
        """→ (final url after redirects, status, body text).

        `data`: dict or list of (name, value) pairs (repeated names allowed);
        with `files` [(field, path)] the body is sent as multipart/form-data."""
        self.check_url(url)
        headers = dict(headers or {})
        if files:
            body, ctype = encode_multipart(data or [], files)
            headers["Content-Type"] = ctype
        else:
            body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
        self._spend_and_wait()
        req = urllib.request.Request(url, data=body, headers={**self.headers, **headers})
        try:
            with self.opener.open(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
                status, final = resp.status, resp.geturl()
        except urllib.error.HTTPError as e:
            log(f"HTTP {e.code} {url}")
            if e.code in (403, 429, 503):
                raise Stop(f"HTTP {e.code} on {url} — rate limiting or a block; stopped, try later")
            text = e.read().decode("utf-8", "replace") if e.fp else ""
            status, final = e.code, url
        except (urllib.error.URLError, TimeoutError) as e:
            raise Stop(f"network error on {url}: {e}")
        finally:
            self.save_cookies()
        log(f"{'POST' if body else 'GET'} {status} {url}" + (f" → {final}" if final != url else ""))
        if any(m in text for m in BLOCK_MARKERS):
            raise Stop(f"anti-bot challenge on {url} — stopped; log in once in a normal browser and try later")
        return final, status, text


def encode_multipart(fields: dict | list, files: list[tuple[str, Path]]) -> tuple[bytes, str]:
    boundary = f"----mcpkit{random.getrandbits(64):016x}"
    pairs = list(fields.items()) if isinstance(fields, dict) else list(fields)
    out = bytearray()
    for name, value in pairs:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{'' if value is None else value}\r\n").encode()
    for name, path in files:
        path = Path(path)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode()
        out += path.read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def check_upload(path: str, max_mb: float = 10) -> Path:
    """A local file to attach: must exist and be a document of sane size."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise Stop(f"file not found: {path}")
    if p.stat().st_size > max_mb * 1024 * 1024:
        raise Stop(f"{p.name} is larger than {max_mb} MB")
    if p.suffix.lower() not in (".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".png", ".jpg", ".jpeg"):
        raise Stop(f"{p.name}: only pdf / doc / docx / odt / rtf / txt / png / jpg can be attached")
    return p


# ------------------------------------------------------------------ forms ---

def parse_form(form_html: str) -> dict:
    """Current state of an HTML form, as a browser would submit it, plus the
    choices of every select / radio / checkbox group.

    → {"pairs": [(name, value), …], "fields": {name: {"type", "value", "options"?}}}"""
    pairs: list[tuple[str, str]] = []
    fields: dict[str, dict] = {}
    tags = [(t.start(), "input", t.group(1), "") for t in re.finditer(r"<input\b([^>]*)>", form_html, re.I)]
    tags += [(t.start(), t.group(1).lower(), t.group(2), t.group(3))
             for t in re.finditer(r"<(textarea|select)\b([^>]*)>(.*?)</\1>", form_html, re.S | re.I)]

    def attr(attrs: str, name: str) -> str | None:
        m = re.search(rf'\b{name}\s*=\s*(["\'])(.*?)\1', attrs, re.S)
        return html.unescape(m.group(2)) if m else None

    for _, kind, attrs, inner in sorted(tags, key=lambda t: t[0]):
        name = attr(attrs, "name")
        if not name or re.search(r"\bdisabled\b", attrs):
            continue
        typ = (attr(attrs, "type") or "text").lower() if kind == "input" else kind
        if typ in ("submit", "button", "image", "reset"):
            continue
        if typ == "file":
            fields.setdefault(name, {"type": "file"})
            continue
        if typ in ("checkbox", "radio"):
            value = attr(attrs, "value") or "on"
            f = fields.setdefault(name, {"type": typ, "value": [] if typ == "checkbox" else None, "options": []})
            f["options"].append(value)
            if re.search(r"\bchecked\b", attrs):
                pairs.append((name, value))
                if typ == "checkbox":
                    f["value"].append(value)
                else:
                    f["value"] = value
            continue
        if kind == "select":
            opts = re.findall(r"<option\b([^>]*)>(.*?)</option>", inner, re.S | re.I)
            options = [{"value": attr(a, "value") if attr(a, "value") is not None else clean(t), "label": clean(t)}
                       for a, t in opts]
            chosen = [o["value"] for (a, _), o in zip(opts, options) if re.search(r"\bselected\b", a)]
            multiple = bool(re.search(r"\bmultiple\b", attrs))
            if not chosen and options and not multiple:
                chosen = [options[0]["value"]]
            pairs += [(name, v) for v in chosen]
            fields[name] = {"type": "select-multiple" if multiple else "select",
                            "value": chosen if multiple else (chosen[0] if chosen else None), "options": options}
            continue
        value = html.unescape(inner) if kind == "textarea" else (attr(attrs, "value") or "")
        if kind == "textarea" and value.startswith("\n"):
            value = value[1:]
        pairs.append((name, value))
        fields[name] = {"type": typ, "value": value}
    return {"pairs": pairs, "fields": fields}


def override_form(pairs: list[tuple[str, str]], fields: dict, changes: dict) -> list[tuple[str, str]]:
    """Replace the values of `changes` keys in the submitted pairs. A list value
    sets a multi-value field (checkbox group / multiple select); bool for one checkbox."""
    out = [(n, v) for n, v in pairs if n not in changes]
    for name, value in changes.items():
        f = fields.get(name)
        if f is None:
            raise Stop(f"unknown field '{name}'; known: {', '.join(sorted(fields))}")
        if f.get("type") == "file":
            raise Stop(f"'{name}' is a file field — not editable here")
        if f["type"] == "checkbox" and isinstance(value, bool):
            if value:
                out.append((name, f["options"][0] if len(f["options"]) == 1 else "on"))
            continue
        values = value if isinstance(value, list) else [value]
        allowed = {o["value"] if isinstance(o, dict) else o for o in f.get("options", [])}
        for v in values:
            v = "" if v is None else str(v)
            if allowed and v not in allowed and v != "":
                raise Stop(f"'{v}' is not an option of '{name}'; options: {sorted(allowed)[:60]}")
            out.append((name, v))
    return out


# ------------------------------------------------------------------ html ---

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section", "article", "header"}
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(fragment: str) -> str:
    p = _TextExtractor()
    p.feed(fragment or "")
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean(fragment: str | None) -> str:
    """Inline HTML fragment → one line of plain text."""
    if not fragment:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def inner_html(page: str, tag: str, class_token: str) -> str | None:
    """innerHTML of the first <tag> whose class contains `class_token` (depth-aware)."""
    m = re.search(rf'<{tag}\b[^>]*class="[^"]*\b{re.escape(class_token)}\b[^"]*"[^>]*>', page)
    if not m:
        return None
    depth, pos = 1, m.end()
    for t in re.finditer(rf"<(/?){tag}\b[^>]*>", page[pos:], re.I):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return page[pos:pos + t.start()]
    return page[pos:]


def page_title(page: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return clean(m.group(1)) if m else ""


def page_links(page: str, base: str, hosts: tuple[str, ...], limit: int = 150) -> list[dict]:
    """Unique same-site links with their text — lets the caller navigate."""
    out, seen = [], set()
    for href, text in re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        url = urllib.parse.urljoin(base, html.unescape(href))
        host = urllib.parse.urlsplit(url).hostname
        if host not in hosts or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "text": clean(text)[:120]})
        if len(out) >= limit:
            break
    return out


def page_main(page: str) -> str:
    """The page body without head / nav noise where the markup allows."""
    m = re.search(r"<main\b[^>]*>(.*)</main>", page, re.S | re.I)
    if m:
        return m.group(1)
    m = re.search(r"<body\b[^>]*>(.*)</body>", page, re.S | re.I)
    return m.group(1) if m else page


# ------------------------------------------------------------------- mcp ---

class McpServer:
    """Minimal MCP server over stdio: initialize, ping, tools/list, tools/call.

    Every tool carries MCP annotations (read-only / destructive / idempotent)
    so clients can apply per-tool permissions. Server-side switches, read from
    the environment at start (`<PREFIX>` = server name upper-cased):
      <PREFIX>_MCP_READ_ONLY=1           hide every tool that changes something
      <PREFIX>_MCP_DISABLED_TOOLS=a,b    hide these tools
    """

    PROTOCOL = "2025-06-18"

    def __init__(self, name: str, version: str, instructions: str = ""):
        self.name, self.version, self.instructions = name, version, instructions
        self.tools: dict[str, dict] = {}
        self.hidden: set[str] = set()

    def tool(self, name: str, description: str, properties: dict | None = None,
             required: list[str] | None = None, *, title: str | None = None, read_only: bool = True,
             destructive: bool = False, idempotent: bool | None = None):
        annotations = {"title": title or name.replace("_", " ").capitalize(), "readOnlyHint": read_only,
                       "openWorldHint": True}
        if not read_only:
            annotations["destructiveHint"] = destructive
            annotations["idempotentHint"] = bool(idempotent)
        def register(fn):
            self.tools[name] = {
                "fn": fn, "write": not read_only,
                "spec": {
                    "name": name, "title": annotations["title"],
                    "description": description + ("" if read_only else
                                                  " CHANGES THE ACCOUNT / CONTACTS PEOPLE — needs the user's explicit OK;"
                                                  " call with dry_run=true first to show exactly what will be sent."),
                    "inputSchema": {"type": "object", "properties": properties or {},
                                    "required": required or [], "additionalProperties": False},
                    "annotations": annotations,
                },
            }
            return fn
        return register

    def configure_from_env(self) -> None:
        prefix = re.sub(r"\W", "_", self.name).upper()
        if os.environ.get(f"{prefix}_MCP_READ_ONLY", "").lower() in ("1", "true", "yes", "on"):
            self.hidden |= {n for n, t in self.tools.items() if t["write"]}
        self.hidden |= {t.strip() for t in os.environ.get(f"{prefix}_MCP_DISABLED_TOOLS", "").split(",") if t.strip()}
        if self.hidden:
            log(f"hidden tools: {', '.join(sorted(self.hidden))}")

    def _result(self, value) -> dict:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def _handle(self, msg: dict) -> dict | None:
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            return {"protocolVersion": params.get("protocolVersion") or self.PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                    "instructions": self.instructions}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t["spec"] for n, t in self.tools.items() if n not in self.hidden]}
        if method == "tools/call":
            tool = self.tools.get(params.get("name"))
            if not tool or params.get("name") in self.hidden:
                raise LookupError(f"unknown tool {params.get('name')}")
            try:
                return self._result(tool["fn"](**(params.get("arguments") or {})))
            except Stop as e:
                return {"content": [{"type": "text", "text": f"stopped: {e}"}], "isError": True}
            except TypeError as e:
                return {"content": [{"type": "text", "text": f"bad arguments: {e}"}], "isError": True}
            except Exception as e:  # report, keep the server alive
                log(traceback.format_exc())
                return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        raise LookupError(f"method not found: {method}")

    def run(self) -> None:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        self.configure_from_env()
        log(f"{self.name} {self.version} ready ({len(self.tools) - len(self.hidden)} tools)")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" not in msg:  # notification
                continue
            try:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": self._handle(msg)}
            except LookupError as e:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": str(e)}}
            except Exception as e:
                log(traceback.format_exc())
                reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
