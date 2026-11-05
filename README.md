# dou-mcp

> Personal project. I built it for myself and I'm still testing it — expect
> rough edges and breaking changes. Not affiliated with DOU.

An MCP server that lets an AI assistant (Claude Code, Codex, any MCP client)
work with **your own** [DOU](https://dou.ua) account: profile and its settings,
job applications with cover letters, messages with recruiters, stats — and
search vacancies and companies on jobs.dou.ua.

- Python 3 standard library only — no dependencies, Windows / macOS / Linux.
- 13 read tools, 4 write tools (`update_profile`, `set_looking_for_work`,
  `send_message`, `apply_to_job`). Every write tool has `dry_run` and is
  annotated `readOnlyHint: false`, so the client can ask before each call.
- Paced requests (3–7 s), daily cap, stops on rate limits / captchas.

## Run

```sh
python server.py            # stdio MCP server
```

Credentials — environment or a `.env` file in this folder or above:

```
DOU_COOKIES=sessionid=...; csrftoken=...   # from your logged-in browser (DevTools → Cookies)
# or DOU_EMAIL=... and DOU_PASSWORD=...     # DOU's own e-mail login
DOU_MCP_READ_ONLY=1                          # optional: hide the write tools
```

Claude Code (`.mcp.json`):

```json
{ "mcpServers": { "dou": { "type": "stdio", "command": "python", "args": ["path/to/dou-mcp/server.py"] } } }
```

Allow the read tools and keep the write tools on "ask" — see the permissions
example and the full tool list in [AGENTS.md](AGENTS.md).

Use it only with your own account and at a human pace.
