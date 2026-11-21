# Finance Agent

A personal expense tracker you use by chatting with Claude.

You send a receipt photo or write "bought coffee 3.40". Claude shows a draft, you confirm, and the expense is saved to your own PostgreSQL database. You can also ask questions like "how much did I spend on groceries this month?" or "will my balance last until the end of the month?".

**How it works:** a small remote [MCP](https://modelcontextprotocol.io) server (Python, official `mcp` SDK, Streamable HTTP) that Claude connects to as a custom connector. The code does all the math and the checks; the model only understands your text and explains the results.

**Status:** work in progress.

## Design principles
- **Code calculates, the model talks.** Sums, receipt checks and the forecast are plain Python with tests.
- **Safe by design.** A bearer token on every request, a read-only database role for free-form queries, a preview → confirm → save flow, and no delete tool (only "void").
- **Self-hosted.** Everyone deploys their own copy with their own database. No shared server, no shared data.

## Development
Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync          # create .venv and install dependencies
uv run pytest    # run the tests
```

Demo data in `examples/` is made up.
