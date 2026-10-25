# MCP server: connect Claude Desktop

The core service is also an MCP server, so an agent such as Claude Desktop can work with
care gaps directly. It has four tools:

| Tool | What it does | Writes? |
|---|---|---|
| `get_member_gaps(member_id, year=2025)` | The member's plan, contact language, the fields the plan allows (`allowed_member_fields`) and the plan's care gap measures | no |
| `search_benefits(customer, query, k=5)` | Lines from the plan's Summary of Benefits, as written, nearest first, with ids to cite | no |
| `log_interaction(member_id, channel, outcome, summary)` | Records a contact (`member_interactions` table) | yes |
| `escalate_to_human(member_id, reason, campaign_id=None)` | Asks a person to follow up. The team is notified with IDs only. Asking again with the same reason returns the same request while it is open | yes |

All data is synthetic. The server is a thin layer over the core service's HTTP API; the
rules are in `caregap.members` ([ADR 0011](adr/0011-mcp-integration-surface.md)).

## 1. Run the stack on your computer

Claude Desktop starts the MCP server as a local process, which then calls the core
service at `CORE_URL` (default `http://localhost:8000`). So the stack runs on the same
machine as Claude Desktop:

```bash
git clone https://github.com/ameyabhamare/caregap-voice && cd caregap-voice
make install                  # uv sync (Python deps, including the MCP SDK)
make up                       # containers, including the core service on :8000
make seed && make pipeline && make sync-gaps   # members and gaps (about 25 min)
make prompts-register && make ingest-benefits  # prompts and the benefits index
```

On an existing install, `make up` rebuilds the core image and `make db-schema` adds the
Phase 9 tables. `curl localhost:8000/health` should answer `"status": "ok"`.

## 2. Add the server to Claude Desktop

```bash
make mcp-config
```

prints an entry with absolute paths (Claude Desktop does not use your shell's `PATH`):

```json
{
  "mcpServers": {
    "caregap": {
      "command": "/Users/you/.local/bin/uv",
      "args": ["--directory", "/Users/you/caregap-voice", "run", "python", "-m", "caregap.mcp"],
      "env": {"CORE_URL": "http://localhost:8000"}
    }
  }
}
```

In Claude Desktop, open Settings > Developer > Edit Config. Merge the `caregap` entry into
`mcpServers` in `claude_desktop_config.json`, then quit and restart Claude Desktop. The
file lives at:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

The tools then appear under the tools (hammer/slider) icon in a new chat.

## 3. Try it (the Phase 9 acceptance check)

Pick a member with an open gap:

```bash
docker compose exec -T postgres psql -U caregap -tAc \
  "SELECT member_id FROM care_gaps WHERE status = 'gap' AND measurement_year = 2025 LIMIT 3"
```

Then ask Claude Desktop, for example:

1. "Look up member `<id>`. Which care gaps are open, and what does their plan charge for
   that visit?" (uses `get_member_gaps`, then `search_benefits`)
2. "The member wants to talk to a nurse about their medication. Escalate it and log the
   call." (uses `escalate_to_human` and `log_interaction`; Claude Desktop asks before
   running each tool)

Check the result:

```bash
curl -s localhost:8000/escalations | python -m json.tool   # kind "agent_request", your reason in "note"
tail -1 data/notifications.log                             # delivered by n8n, IDs only
```

## Troubleshooting

- **Tools missing in Claude Desktop**: run the `command` and `args` from the config in a
  terminal. It should print `[caregap.mcp] core service at ...` and then wait for input.
  Claude Desktop logs are in `~/Library/Logs/Claude/mcp*.log` (macOS).
- **"core service unreachable"**: the stack is not up, or `CORE_URL` is wrong.
- **"core service refused (404)"**: unknown member or plan.
  **(409)**: the member belongs to no configured plan in that year.
- **Debugging without Claude Desktop**: `make mcp` runs the server on stdio. Any MCP
  client works, for example the MCP Inspector (`npx @modelcontextprotocol/inspector`,
  needs Node).
