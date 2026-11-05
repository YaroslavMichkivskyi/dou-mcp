"""dou-mcp — DOU (dou.ua, jobs.dou.ua) as an MCP server.

    python server.py [--env-file PATH] [--state-dir PATH]

Account: profile and its settings, job applications with cover letters,
messages with recruiters, stats, favorites — read, and (separate tools, each
with `dry_run`) edit the profile, send a message, apply to a vacancy.
Public: vacancy search, vacancy text, company pages.

Credentials (env or the nearest `.env`): DOU_COOKIES (`sessionid=…; csrftoken=…`
from a logged-in browser — works for any sign-in method), or DOU_EMAIL +
DOU_PASSWORD (DOU's own e-mail login). Switches: DOU_MCP_READ_ONLY=1,
DOU_MCP_DISABLED_TOOLS=a,b, DOU_MCP_DAILY=150.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mcpkit import (DRY_RUN_PROP, PAGING_PROPS, McpServer, SiteSession, Stop, check_upload, clean,  # noqa: E402
                    csrf_token, html_to_text, inner_html, is_action_link, load_env, log, override_form, page_links,
                    page_main, page_title, parse_form, slice_text)

VERSION = "0.2.2"
SITE = "https://dou.ua"
JOBS = "https://jobs.dou.ua"
HOSTS = ("dou.ua", "jobs.dou.ua")
# Path segments of links that change state on a plain GET (whole segments, so slugs are safe).
ACTION_SEGMENTS = re.compile(r"logout.*|delete.*|remove.*|deactivat.*|unsubscrib.*|apply|respond|reject|accept|"
                             r"withdraw|toggle.*|(?:un)?subscribe|vote|send|new|(?:un)?archive|hide|block|ban", re.I)
CATEGORIES = (
    ".NET", "AI/ML", "Analyst", "Android", "Architect", "Big Data", "Blockchain", "C++",
    "Data Engineer", "Data Science", "DBA", "DevOps", "Embedded", "Engineering Manager",
    "Erlang", "ERP/CRM", "Flutter", "Front End", "Golang", "iOS/macOS", "Java", "No-code",
    "Node.js", "Other", "PHP", "Python", "QA", "React Native", "Ruby", "Rust", "Salesforce",
    "SAP", "Scala", "Security", "SysAdmin", "Unity", "Unreal Engine",
)
EXPERIENCE = ("0-1", "1-3", "3-5", "5plus")
PERIODS = ("all", "year", "month", "week")
UA_MONTHS = {"січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
             "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12}
SLUG = re.compile(r"^[A-Za-z0-9_.-]+$")

server = McpServer("dou", VERSION, instructions=(
    "DOU (Ukrainian IT community and job board) for the logged-in user. Read tools are free to call but every "
    "request is paced and counted against a daily budget — call `status` first. Write tools (update_profile, "
    "set_looking_for_work, send_message, apply_to_job) act as the user: show the user the dry_run payload and "
    "get an explicit OK before the real call. Never send or apply on your own initiative."))
session: SiteSession  # set in main()

DRY_RUN, PAGING = DRY_RUN_PROP, PAGING_PROPS
_csrf, _slice = csrf_token, slice_text


# ---------------------------------------------------------------- helpers ---

def _user_id(page: str) -> str | None:
    """Anonymous pages say `window.USER_ID = 0` and carry the login link."""
    m = re.search(r"USER_ID\s*=\s*['\"]?(\d+)", page)
    return m.group(1) if m and m.group(1) != "0" and 'id="login-link"' not in page else None


def _profile_url(page: str) -> str | None:
    """My profile link sits in the site header (articles below link other users)."""
    head = re.search(r"<header\b.*?</header>", page, re.S | re.I)
    m = re.search(r'href="(https://dou\.ua/users/[^/"?]+/)', head.group(0) if head else page[:20000])
    return m.group(1) if m else None


def _content(page: str) -> str:
    """The page's own content column, without header, banners and sidebars."""
    for tag, cls in (("div", "l-content-wrap"), ("div", "l-content"), ("div", "b-content")):
        block = inner_html(page, tag, cls)
        if block:
            return block
    return page_main(page)


def _text(page: str) -> str:
    text = html_to_text(_content(page))
    return re.sub(r"^.*?Доступ до цієї сторінки є лише у Вашого акаунта\s*", "", text, flags=re.S)


def _ua_date(text: str) -> str | None:
    """'13 жовтня' / '25 вересня 2026' → ISO date; None for anything odd (never raises)."""
    m = re.search(r"(\d{1,2})\s+([а-яіїєґ']+)(?:\s+(\d{4}))?", (text or "").lower())
    if not m or m.group(2) not in UA_MONTHS:
        return None
    today = date.today()
    year, month, day = int(m.group(3)) if m.group(3) else today.year, UA_MONTHS[m.group(2)], int(m.group(1))
    for y in ((year,) if m.group(3) else (year, year - 1)):  # no year given: the latest date not in the future
        try:
            d = date(y, month, day)
        except ValueError:  # 31 квітня, 29 лютого in a non-leap year
            continue
        if m.group(3) or d <= today:
            return d.isoformat()
    return None


# ------------------------------------------------------------------ login ---

def _logged_in() -> tuple[bool, str]:
    _, _, page = session.request(f"{SITE}/")
    return bool(_user_id(page)), page


def _login() -> dict:
    cookies = os.environ.get("DOU_COOKIES")
    email, password = os.environ.get("DOU_EMAIL"), os.environ.get("DOU_PASSWORD")
    if cookies:
        session.clear_cookies()
        n = session.import_cookie_header(cookies, ".dou.ua")
        method = f"cookies ({n})"
    elif email and password:
        session.clear_cookies()
        _, _, page = session.request(f"{SITE}/login/")
        url = re.search(r"LOGIN_URL\s*=\s*['\"]([^'\"]+)", page)
        _, status, body = session.request(
            url.group(1) if url else f"{SITE}/ajax-login/",
            data={"username": email, "password": password, "csrfmiddlewaretoken": _csrf(page), "mode": "login"},
            headers={"Referer": f"{SITE}/login/", "Origin": SITE, "X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json, text/javascript, */*; q=0.01"})
        try:
            reply = json.loads(body)
        except ValueError:
            raise Stop(f"DOU login: unexpected answer (HTTP {status})")
        if not reply.get("userId"):
            raise Stop("DOU login failed: wrong e-mail / password, or the account signs in through "
                       "Google / LinkedIn / GitHub — then use DOU_COOKIES")
        method = "password"
    else:
        raise Stop("no credentials: set DOU_COOKIES (or DOU_EMAIL + DOU_PASSWORD) in .env / the MCP config")
    ok, page = _logged_in()
    if not ok:
        raise Stop(f"DOU login via {method} did not give an account session (cookies expired?)")
    return {"logged_in": True, "method": method, "user_id": _user_id(page), "profile": _profile_url(page)}


def _get(url: str, *, account: bool = True) -> tuple[str, str]:
    """GET a page; for account pages log in once if the session is gone."""
    url = url if url.startswith("https://") else urllib.parse.urljoin(SITE, url)
    parts = urllib.parse.urlsplit(url)
    if is_action_link(url, ACTION_SEGMENTS):
        raise Stop(f"{url} looks like an action link (changes the account) — not opened")
    final, status, page = session.request(url)
    if account and not _user_id(page):
        log("not logged in — logging in")
        _login()
        final, status, page = session.request(url)
        if not _user_id(page):
            raise Stop("still not logged in after login")
    if status >= 400:
        raise Stop(f"HTTP {status} on {url}")
    return final, page


def _me() -> str:
    _, page = _get(f"{SITE}/stats/")
    url = _profile_url(page)
    if not url:
        raise Stop("logged in, but no /users/<me>/ link found")
    return url.rstrip("/").rsplit("/", 1)[-1]


# -------------------------------------------------------------- account ---

@server.tool("status", "Is the DOU session logged in (user id, profile link); today's request budget. 1 request.",
             title="Session status")
def status() -> dict:
    ok, page = _logged_in()
    return {"logged_in": ok, "user_id": _user_id(page), "profile": _profile_url(page) if ok else None,
            "credentials": [k for k in ("DOU_COOKIES", "DOU_EMAIL", "DOU_PASSWORD") if os.environ.get(k)],
            "budget": session.budget(), "hidden_tools": sorted(server.hidden), "version": VERSION}


@server.tool("login", "Log in again with the configured credentials (cookies, else password). Never returns secrets.",
             title="Log in")
def login() -> dict:
    return _login()


@server.tool("get_profile", "A DOU user's public profile page as text — mine by default.",
             {"user": {"type": "string", "description": "profile slug from dou.ua/users/<slug>/; empty = me"}},
             title="Profile")
def get_profile(user: str | None = None) -> dict:
    slug = user or _me()
    if not SLUG.match(slug):
        raise Stop("user must be the slug from dou.ua/users/<slug>/")
    final, page = _get(f"{SITE}/users/{slug}/")
    return {"url": final, "user": slug, "title": page_title(page), "text": _text(page)[:20000]}


def _settings_form() -> tuple[str, dict, str]:
    final, page = _get(f"{SITE}/edit-profile/")
    form = next((m.group(1) for m in re.finditer(r"<form\b[^>]*method=\"post\"[^>]*>(.*?)</form>", page, re.S | re.I)
                 if 'name="display_name"' in m.group(1)), None)
    if not form:
        raise Stop("profile form not found on /edit-profile/ — markup changed?")
    parsed = parse_form(form)
    labels = {i: clean(t) for i, t in re.findall(r'<label[^>]*for="([^"]+)"[^>]*>(.*?)</label>', form, re.S)}
    for name, f in parsed["fields"].items():
        if f["type"] == "checkbox" and len(f.get("options", [])) > 1:
            f["options"] = [{"value": v, "label": labels.get(f"{name}_{v}", v)} for v in f["options"]]
    return final, parsed, page


@server.tool("get_profile_settings", "My profile settings exactly as the edit form holds them: every field "
             "(name, title, city, company, skills, about, looking for work, specialization, English, experience, "
             "industries, phone, telegram…) with its current value and allowed options. Use the field names with "
             "update_profile.", title="Profile settings")
def get_profile_settings() -> dict:
    final, parsed, _ = _settings_form()
    fields = {k: v for k, v in parsed["fields"].items() if k != "csrfmiddlewaretoken"}
    return {"url": final, "fields": fields}


@server.tool("list_applications", "My responses to vacancies: date, vacancy, company, cover letter, attached CV.",
             {"period": {"type": "string", "enum": list(PERIODS), "default": "all"},
              "letter_chars": {"type": "integer", "minimum": 0, "maximum": 20000, "default": 1500}},
             title="My job applications")
def list_applications(period: str = "all", letter_chars: int = 1500) -> dict:
    final, page = _get(f"{SITE}/applications/" + (f"?period={period}" if period != "all" else ""))
    block = inner_html(page, "ul", "applications-list") or ""
    items = []
    for li in re.finditer(r'<li id="(\d+)">(.*?)(?=<li id="\d+">|\Z)', block, re.S):
        body = li.group(2)
        title = re.search(r'<a class="title" href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
        company = re.search(r'<a class="company" href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
        letter = re.search(r'<div class="description">(.*?)</div>\s*</details>', body, re.S)
        cv = re.search(r'<a class="cv_link" href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
        items.append({
            "id": li.group(1),
            "date": clean((re.search(r'<div class="date">(.*?)</div>', body, re.S) or [None, ""])[1]) or None,
            "title": clean(title.group(2)) if title else None,
            "vacancy_url": title.group(1).split("?")[0] if title else None,
            "company": clean(company.group(2)) if company else None,
            "cover_letter": html_to_text(letter.group(1))[:letter_chars] if letter and letter_chars else None,
            "cv": {"file": clean(cv.group(2)), "url": cv.group(1)} if cv else None,
        })
    return {"url": final, "period": period, "count": len(items), "applications": items}


@server.tool("get_stats", "My DOU stats page (articles, topics, popular comments) as text.", title="My stats")
def get_stats() -> dict:
    final, page = _get(f"{SITE}/stats/")
    text = _text(page)
    return {"url": final, "text": text.split("Не пропустіть")[0].strip()[:15000]}


@server.tool("list_favorites", "Articles, topics and events I saved to favorites on DOU.", title="Favorites")
def list_favorites() -> dict:
    final, page = _get(f"{SITE}/favorites/")
    return {"url": final, "text": _text(page).split("Не пропустіть")[0].strip()[:15000]}


@server.tool("list_conversations", "My DOU message threads (recruiters write here): who, company, last date, excerpt.",
             title="Message threads")
def list_conversations() -> dict:
    final, page = _get(f"{SITE}/inbox/")
    threads = []
    for m in re.finditer(r'<div class="contact[^"]*"\s+data-username="([^"]+)"[^>]*>(.*?)</div>', page, re.S):
        body = m.group(2)
        threads.append({
            "user": m.group(1),
            "name": clean((re.search(r'class="contact-name">(.*?)</span>', body, re.S) or [None, ""])[1]),
            "date": clean((re.search(r'class="contact-date">(.*?)</span>', body, re.S) or [None, ""])[1]),
            "excerpt": clean((re.search(r'class="contact-excerpt">(.*?)</span>', body, re.S) or [None, ""])[1]),
            "unread": "unread" in m.group(0),
        })
    return {"url": final, "count": len(threads), "threads": threads}


def _messages(page: str) -> list[dict]:
    out = []
    # mine: class="item i_sent"; theirs: class="item recipient_sent"
    for m in re.finditer(r'<div class="item\b([^"]*)"\s+data-mid="([^"]+)"[^>]*>(.*?)<div class="date">(.*?)</div>',
                         page, re.S):
        date_block = m.group(4)
        author = re.search(r'class="message-author">(.*?)</span>', date_block, re.S)
        text = re.search(r'<div class="main-message">(.*?)</div>', m.group(3), re.S)
        out.append({
            "id": m.group(2),
            "from_me": "i_sent" in m.group(1).split(),
            "author": clean(author.group(1)) if author else None,
            "date": clean(re.sub(r'<span class="message-author">.*?</span>', "", date_block, flags=re.S)),
            "text": html_to_text(text.group(1)) if text else html_to_text(m.group(3)),
        })
    return out


@server.tool("get_conversation", "One message thread with a DOU user: their name, role, company and every message "
             "(id, author, date, text, from_me).",
             {"user": {"type": "string", "description": "username from list_conversations"},
              "last": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50,
                       "description": "only the newest N messages"}}, ["user"], title="Message thread")
def get_conversation(user: str, last: int = 50) -> dict:
    if not SLUG.match(user):
        raise Stop("user must be the username from list_conversations")
    final, page = _get(f"{SITE}/inbox/{user}/")
    top = inner_html(page, "div", "messages-top-stick") or ""
    msgs = _messages(page)
    return {"url": final, "user": user,
            "contact": clean(re.sub(r"<img[^>]*>", "", top)) or None,
            "count": len(msgs), "messages": msgs[-last:]}


@server.tool("get_page", "Read any dou.ua / jobs.dou.ua page in my session as text, e.g. a forum topic, an "
             "article, a company's reviews. Action links (logout, delete, subscribe…) are refused.",
             {"path": {"type": "string", "description": "a dou.ua path or a full https URL on dou.ua / jobs.dou.ua"},
              "links": {"type": "boolean", "default": False, "description": "also return the page's DOU links"},
              **PAGING}, ["path"], title="Read a DOU page")
def get_page(path: str, links: bool = False, offset: int = 0, max_chars: int = 15000) -> dict:
    final, page = _get(path, account=False)
    out = {"url": final, "title": page_title(page), "logged_in": bool(_user_id(page)),
           **_slice(_text(page), offset, max_chars)}
    if links:
        out["links"] = page_links(_content(page), final, HOSTS)
    return out


# ------------------------------------------------------------ write tools ---

@server.tool("update_profile", "Change fields of my DOU profile (names from get_profile_settings; lists for "
             "multi-choice fields like industries, true/false for checkboxes like looking_for_work). Every other "
             "field keeps its current value.",
             {"changes": {"type": "object", "description": 'e.g. {"job_title": "Full Stack Engineer", '
                                                           '"english_level": "Upper-Intermediate"}'},
              **DRY_RUN}, ["changes"], title="Edit my profile", read_only=False, destructive=True, idempotent=True)
def update_profile(changes: dict, dry_run: bool = False) -> dict:
    if not changes:
        raise Stop("nothing to change")
    final, parsed, page = _settings_form()
    pairs = override_form(parsed["pairs"], parsed["fields"], changes)
    before = {k: parsed["fields"][k].get("value") for k in changes}
    if dry_run:
        return {"dry_run": True, "url": final, "before": before, "after": changes}
    _, status, body = session.request(f"{SITE}/edit-profile/", data=pairs,
                                      headers={"Referer": f"{SITE}/edit-profile/", "Origin": SITE})
    if status >= 400:
        raise Stop(f"HTTP {status} saving the profile")
    errors = [clean(e) for e in re.findall(r'class="[^"]*(?:errorlist|error-msg|errors)[^"]*"[^>]*>(.*?)</', body, re.S)
              if clean(e)]
    _, after, _ = _settings_form()
    now = {k: after["fields"].get(k, {}).get("value") for k in changes}
    return {"saved": not errors, "errors": errors or None, "before": before, "now": now}


@server.tool("set_looking_for_work", "Turn DOU's 'шукаю роботу' (looking for work) flag on or off.",
             {"on": {"type": "boolean"}, **DRY_RUN}, ["on"], title="Looking for work",
             read_only=False, destructive=False, idempotent=True)
def set_looking_for_work(on: bool, dry_run: bool = False) -> dict:
    return update_profile({"looking_for_work": on}, dry_run)


@server.tool("send_message", "Send a DOU message to a user (e.g. reply to a recruiter), optionally quoting one of "
             "their messages and attaching a file (CV).",
             {"user": {"type": "string", "description": "username from list_conversations / a profile slug"},
              "text": {"type": "string"},
              "reply_to": {"type": "string", "description": "message id from get_conversation to quote"},
              "attachment": {"type": "string", "description": "local file path (pdf, docx…, ≤ 10 MB)"},
              **DRY_RUN}, ["user", "text"], title="Send a message", read_only=False, destructive=False,
             idempotent=False)
def send_message(user: str, text: str, reply_to: str | None = None, attachment: str | None = None,
                 dry_run: bool = False) -> dict:
    if not SLUG.match(user):
        raise Stop("user must be a DOU username")
    if not text.strip():
        raise Stop("empty message")
    file = check_upload(attachment) if attachment else None
    url = f"{SITE}/inbox/{user}/"
    final, page = _get(url)
    if 'name="message"' not in page:
        raise Stop(f"no message form on {final} — can't write to this user")
    if reply_to and f'data-mid="{reply_to}"' not in page:
        raise Stop(f"message {reply_to} is not in this thread")
    payload = {"to": user, "text": text, "reply_to": reply_to, "attachment": file.name if file else None}
    if dry_run:
        return {"dry_run": True, "url": final, "would_send": payload}
    before = {m["id"] for m in _messages(page)}
    _, status, _ = session.request(final, data=[("csrfmiddlewaretoken", _csrf(page)),
                                                ("reply_to", reply_to or ""), ("message", text)],
                                   files=[("file", file)] if file else None,
                                   headers={"Referer": final, "Origin": SITE})
    if status >= 400:
        raise Stop(f"HTTP {status} sending the message")
    _, page = _get(url)
    # compare message ids, not counts: a long thread may render only a window of messages
    new = [m for m in _messages(page) if m["id"] not in before and m["from_me"]]
    return {"sent": bool(new), "message": new[-1] if new else None,
            "note": None if new else "no new message of mine in the thread — check get_conversation before retrying"}


def _vacancy_page(url: str) -> tuple[str, str]:
    m = re.match(r"https://jobs\.dou\.ua/companies/([^/]+)/vacancies/(\d+)/?", url or "")
    if not m:
        raise Stop("give the vacancy URL: https://jobs.dou.ua/companies/<company>/vacancies/<id>/")
    return _get(f"{JOBS}/companies/{m.group(1)}/vacancies/{m.group(2)}/", account=False)


def _applied(page: str) -> str | None:
    i = page.find("Ви вже відгукувались на цю вакансію")
    if i >= 0:
        m = re.search(r"\d{2}\.\d{2}\.\d{4}", page[i:i + 400])
        return m.group(0) if m else "yes"
    return "yes" if "Ви відгукнулись на цю вакансію" in page and 'class="replied sent' in page else None


@server.tool("apply_to_job", "Respond to a DOU vacancy as me: cover letter and, optionally, a CV file. "
             "Refuses if I already applied.",
             {"url": {"type": "string", "description": "https://jobs.dou.ua/companies/<company>/vacancies/<id>/"},
              "cover_letter": {"type": "string"},
              "cv": {"type": "string", "description": "local CV file path (pdf, docx…, ≤ 10 MB)"},
              **DRY_RUN}, ["url", "cover_letter"], title="Apply to a vacancy", read_only=False,
             destructive=False, idempotent=False)
def apply_to_job(url: str, cover_letter: str, cv: str | None = None, dry_run: bool = False) -> dict:
    file = check_upload(cv) if cv else None
    final, page = _vacancy_page(url)
    if not _user_id(page):
        _login()
        final, page = _vacancy_page(url)
    if _applied(page):
        raise Stop(f"already applied to this vacancy ({_applied(page)})")
    form = re.search(r'<form id="replied-id"[^>]*>(.*?)</form>', page, re.S)
    if not form:
        raise Stop("this vacancy has no DOU response form (it may link to an external site) — apply there")
    parsed = parse_form(form.group(1))
    # build the real request before answering a dry run, so a dry run fails exactly where the send would
    pairs = override_form(parsed["pairs"], parsed["fields"], {"descr": cover_letter})
    if file and parsed["fields"].get("user_cv", {}).get("type") != "file":
        raise Stop("this vacancy's form has no CV upload field — send without `cv`")
    title = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    payload = {"vacancy": clean(title.group(1)) if title else final, "url": final,
               "cover_letter": cover_letter, "cv": file.name if file else None}
    if dry_run:
        return {"dry_run": True, "would_send": payload}
    _, status, _ = session.request(final, data=pairs, files=[("user_cv", file)] if file else None,
                                   headers={"Referer": final, "Origin": JOBS})
    if status >= 400:
        raise Stop(f"HTTP {status} sending the response")
    _, page = _vacancy_page(final)
    return {"applied": bool(_applied(page)), **payload,
            "note": None if _applied(page) else "the vacancy page doesn't show the response — check list_applications"}


# ----------------------------------------------------------- public jobs ---

def _items(fragment: str) -> list[dict]:
    items = []
    for block in fragment.split('<li class="l-vacancy')[1:]:
        link = re.search(r'<a class="vt" href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = link.group(1).split("?")[0]
        vid = re.search(r"/vacancies/(\d+)/", url)
        company = re.search(r'class="company"[^>]*>(.*?)</a>', block, re.S)
        items.append({
            "id": vid.group(1) if vid else url, "url": url, "title": clean(link.group(2)),
            "company": clean(company.group(1)) if company else None,
            "salary": clean((re.search(r'<span class="salary">(.*?)</span>', block, re.S) or [None, None])[1]) or None,
            "location": clean((re.search(r'<span class="cities[^"]*">(.*?)</span>', block, re.S) or [None, None])[1]) or None,
            "posted_at": _ua_date(clean((re.search(r'<div class="date">(.*?)</div>', block, re.S) or [None, ""])[1])),
            "hot": "__hot" in block[:200],
            "summary": clean((re.search(r'<div class="sh-info">(.*?)</div>', block, re.S) or [None, None])[1]),
        })
    return items


def _list(url: str, pages: int) -> dict:
    _, _, page = session.request(url)
    found = _items(page)
    m = re.search(r"<h1>\s*(\d+)\s", page)
    total = int(m.group(1)) if m else None
    if total and not found:
        raise Stop(f"DOU shows {total} vacancies but none were parsed — markup changed?")
    csrf = re.search(r"CSRF_TOKEN\s*=\s*['\"]([^'\"]+)", page)
    split = urllib.parse.urlsplit(url)
    more_url = urllib.parse.urlunsplit(split._replace(path=split.path.rstrip("/") + "/xhr-load/"))
    n = 1
    while n < pages and csrf and (total is None or len(found) < total):
        _, _, body = session.request(more_url, data={"csrfmiddlewaretoken": csrf.group(1), "count": len(found)},
                                     headers={"Referer": url, "X-Requested-With": "XMLHttpRequest"})
        try:
            chunk = json.loads(body)
        except ValueError:
            break
        more = _items(chunk.get("html", ""))
        if not more:
            break
        found += more
        n += 1
        if chunk.get("last"):
            break
    return {"url": url, "total_on_site": total, "count": len(found), "jobs": found}


@server.tool("search_jobs", "Search jobs.dou.ua vacancies (public): newest first, 20 per page, each with a "
             "one-paragraph summary; get_job gives the full text.",
             {"category": {"type": "string", "enum": list(CATEGORIES)},
              "experience": {"type": "string", "enum": list(EXPERIENCE)},
              "remote": {"type": "boolean", "default": False},
              "relocation": {"type": "boolean", "default": False},
              "search": {"type": "string", "description": "keyword (DOU search field)"},
              "city": {"type": "string", "description": "e.g. Київ"},
              "pages": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1,
                        "description": "listing pages of 20 (each page = 1 request)"}}, title="Search vacancies")
def search_jobs(category: str | None = None, experience: str | None = None, remote: bool = False,
                relocation: bool = False, search: str | None = None, city: str | None = None, pages: int = 1) -> dict:
    params = {k: v for k, v in (("category", category), ("exp", experience), ("search", search), ("city", city)) if v}
    flags = "".join(f"{f}&" for f, on in (("remote", remote), ("relocation", relocation)) if on)
    return _list(f"{JOBS}/vacancies/?{flags}{urllib.parse.urlencode(params)}", pages)


@server.tool("get_job", "Full text of one DOU vacancy, plus whether I already applied and whether it has a DOU "
             "response form.",
             {"url": {"type": "string", "description": "https://jobs.dou.ua/companies/<company>/vacancies/<id>/"}},
             ["url"], title="Vacancy")
def get_job(url: str) -> dict:
    final, page = _vacancy_page(url)
    body = inner_html(page, "div", "vacancy-section")
    info = inner_html(page, "div", "sh-info") or ""
    title = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    company = re.search(r'<div class="l-n">\s*<a[^>]*>(.*?)</a>', page, re.S)
    return {
        "url": final, "title": clean(title.group(1)) if title else page_title(page),
        "company": clean(company.group(1)) if company else None,
        "location": clean((re.search(r'<span class="place[^"]*">(.*?)</span>', info, re.S) or [None, None])[1]) or None,
        "salary": clean((re.search(r'<span class="salary">(.*?)</span>', info, re.S) or [None, None])[1]) or None,
        "posted_at": _ua_date(clean((re.search(r'<div class="date">(.*?)</div>', page, re.S) or [None, ""])[1])),
        "applied": _applied(page) if _user_id(page) else None,
        "dou_form": 'id="replied-id"' in page,
        "description": html_to_text(body) if body is not None else _text(page)[:20000],
    }


@server.tool("get_company", "A company on jobs.dou.ua: its description and (optionally) its open vacancies.",
             {"company": {"type": "string", "description": "slug from jobs.dou.ua/companies/<slug>/ or that URL"},
              "vacancies": {"type": "boolean", "default": True}}, ["company"], title="Company")
def get_company(company: str, vacancies: bool = True) -> dict:
    value = company.strip().split("?")[0]
    m = re.search(r"/companies/([^/]+)", value) if "/" in value else re.fullmatch(r"([^/]+)", value)
    slug = m.group(1) if m else ""
    if not SLUG.match(slug):
        raise Stop("give the company slug or its jobs.dou.ua/companies/<slug>/ URL")
    final, page = _get(f"{JOBS}/companies/{slug}/", account=False)
    parts = [inner_html(page, "div", c) for c in ("company-info", "b-company-about")]
    text = "\n\n".join(html_to_text(p) for p in parts if p) or _text(page)
    out = {"url": final, "company": slug, "title": page_title(page), "text": text[:10000]}
    if vacancies:
        jobs = _list(f"{JOBS}/companies/{slug}/vacancies/", 2)["jobs"]
        for j in jobs:
            j["company"] = j["company"] or slug
        out["vacancies"] = jobs
    return out


# ------------------------------------------------------------------ main ---

def main() -> None:
    global session
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", help="read credentials from this file (default: nearest .env upwards)")
    ap.add_argument("--state-dir", help="cookies + request ledger (default: DOU_MCP_STATE_DIR or <this folder>/.state)")
    args = ap.parse_args()
    env = load_env(HERE, args.env_file)
    log(f"env file: {env or 'none'}")
    state = Path(args.state_dir or os.environ.get("DOU_MCP_STATE_DIR") or HERE / ".state")
    session = SiteSession("dou", state, HOSTS, delay=(3.0, 7.0), daily=int(os.environ.get("DOU_MCP_DAILY", "150")))
    server.run()


if __name__ == "__main__":
    main()
