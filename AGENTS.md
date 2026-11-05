---
kind: tool
about: DOU (dou.ua, jobs.dou.ua) as an MCP server — my account (profile + settings, job applications, recruiter messages, stats) with write tools behind dry_run, and public vacancy / company search
when: reading or acting on my DOU account (applications, messages, profile), or looking up DOU vacancies and companies
---

# dou-mcp — DOU as an MCP server

Stdio MCP server, Python 3 standard library only (Windows / macOS / Linux).
Acts as the logged-in DOU user. A Spiritus **module** (`kind: tool`):
installed into `arsenal/dou-mcp/` with `spiritus run modules:install dou-mcp`;
nothing here depends on the rest of Spiritus, so it also runs standalone.
For people: `README.md`.

| File | What |
|---|---|
| `server.py` | the tools below, DOU login and parsing |
| `mcpkit.py` | shared kit: MCP stdio + tool annotations + read-only switches, paced cookie session, forms, `.env` loader, HTML → text. Identical copy in `djinni-mcp` — change one, copy to the other |
| `.state/` | cookies + request ledger (gitignored; `--state-dir` / `DOU_MCP_STATE_DIR`) |

## Tools

Read — annotated `readOnlyHint: true`:

| Tool | Gives | Requests |
|---|---|---|
| `status` | logged in? user id, profile, budget, hidden tools | 1 |
| `login` | re-login from the configured credentials | 1–3 |
| `get_profile` | a user's public profile (mine by default) | 1–2 |
| `get_profile_settings` | every field of my profile form with value + options | 1 |
| `list_applications` | my responses: date, vacancy, company, cover letter, CV (`period`) | 1 |
| `list_conversations` | message threads: user, name, date, excerpt | 1 |
| `get_conversation` | one thread: contact, messages (id, author, date, text, from_me) | 1 |
| `get_stats` | my articles / topics / comments stats | 1 |
| `list_favorites` | saved articles, topics, events | 1 |
| `search_jobs` | vacancies: category, experience, remote, relocation, keyword, city, ≤ 5 pages | 1 / page |
| `get_job` | full vacancy text + `applied` + `dou_form` | 1 |
| `get_company` | company info + open vacancies | 1–3 |
| `get_page` | any dou.ua / jobs.dou.ua page as text (+ links); action links refused | 1 |

Write — `readOnlyHint: false`, each with `dry_run` (returns the exact payload, sends nothing):

| Tool | Does | Hints |
|---|---|---|
| `update_profile` | change profile fields by name; others keep their values; validates options | destructive, idempotent |
| `set_looking_for_work` | the "шукаю роботу" flag | idempotent |
| `send_message` | message a user (reply / quote / attach a file), verifies it appeared | not idempotent |
| `apply_to_job` | respond to a vacancy with a cover letter (+ CV file); refuses a repeat | not idempotent |

## Config

| Env (or the nearest `.env` above the folder; env wins) | |
|---|---|
| `DOU_COOKIES` | `sessionid=…; csrftoken=…` from a logged-in browser (DevTools → Application → Cookies → dou.ua) — works with any sign-in method; preferred |
| `DOU_EMAIL`, `DOU_PASSWORD` | DOU's own e-mail login (DOU closed e-mail sign-up; most accounts use Google / LinkedIn / GitHub → cookies) |
| `DOU_MCP_READ_ONLY=1` | the write tools are not even listed |
| `DOU_MCP_DISABLED_TOOLS=a,b` | hide these tools |
| `DOU_MCP_DAILY` | daily request cap, default 150 |

Claude Code `.mcp.json`: `"dou": {"type": "stdio", "command": "python", "args": ["arsenal/dou-mcp/server.py"]}`.
Codex `config.toml`: `[mcp_servers.dou]`, `command = "python"`, `args = ["<repo>/arsenal/dou-mcp/server.py"]`.

Per-tool permissions in Claude Code (`.claude/settings.json` or `settings.local.json`):

```json
{ "permissions": {
    "allow": ["mcp__dou__status", "mcp__dou__get_profile", "mcp__dou__get_profile_settings",
              "mcp__dou__list_applications", "mcp__dou__list_conversations", "mcp__dou__get_conversation",
              "mcp__dou__get_stats", "mcp__dou__list_favorites", "mcp__dou__search_jobs",
              "mcp__dou__get_job", "mcp__dou__get_company", "mcp__dou__get_page", "mcp__dou__login"],
    "ask":   ["mcp__dou__update_profile", "mcp__dou__set_looking_for_work",
              "mcp__dou__send_message", "mcp__dou__apply_to_job"] } }
```

`deny` instead of `ask` blocks a tool; other clients get the same split from
the tool annotations or from `DOU_MCP_READ_ONLY`.

## Rules

- Write tools act as me: show me the `dry_run` payload, wait for an explicit
  OK, then call once. Never retry a send / apply blindly — check
  `get_conversation` / `list_applications` first.
- Anti-ban: one request at a time, 3–7 s jittered pause (kept across calls and
  restarts), daily cap; 403 / 429 / captcha stops the call — don't loop.
  Bulk collection belongs to a collector with its own budget, not here.
- My own account only, low volume. Never print, log or return credentials or cookies.
