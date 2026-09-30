# wikicore

An MCP server for a wiki that your AI assistants keep for you: a git repo of Markdown notes
(Karpathy's LLM-wiki pattern) that Claude, ChatGPT and coding agents all read and write, so
what one of them learns the others know.

- **Your assistant does the reasoning; wikicore does lookup and filing.** No LLM calls, no
  embeddings, no database on the server.
- **The vault is the instance.** Page types (`_schema/*.yaml`), the rules every client receives
  (`_instructions.md`), tool descriptions (`_tools.yaml`) and optional modules (`repos.yaml`)
  all live in the vault. You customise by editing files, never by forking.
- **Plain files.** Markdown with YAML frontmatter, one folder per type, `[[wiki-links]]`,
  generated `index.md` and append-only `log.md`. Opens as an Obsidian vault. Every change is a
  git commit authored by the assistant that made it.

## Tools

Read: `search`, `get`, `related`, `list_notes`, `recent`, `schema`, `status`.
Write: `save_source` (keep verbatim input: meeting notes, transcripts, paper text), `file`
(create a page; refuses duplicates by title or alias), `update` (append to or replace a section,
merge frontmatter), `add_type`.
Setup: `bootstrap` (per assistant: file what it already knows from its memory and chat history).
Repos (only when `repos.yaml` exists): `repos`, `register_repo`, `repo_status`, `repo_read`,
`repo_log`, `repo_tree`.

Web: `/` connect guide, `/login` (OAuth, paste your key), `/upload` (files and PDFs into
unfiled sources, up to 25 MB), `/health`.

## Run locally

```bash
uv sync
uv run wikicore init ./vault && git -C vault init -q
WIKICORE_VAULT=./vault WIKICORE_DEV_CLIENT=me uv run wikicore serve   # no auth, local only
claude mcp add --transport http wiki http://127.0.0.1:8050/mcp
uv run pytest
```

With auth: `uv run wikicore token add owner` prints a `wk_` key. CLI agents send it as
`Authorization: Bearer wk_...`; claude.ai and ChatGPT connect with OAuth and you paste the key once.

## Commands

- `wikicore init <dir>`: create a vault from the defaults. On an existing vault it only adds
  missing default files and never overwrites your edits.
- `wikicore serve`: run the server (a single process; OAuth state is per process).
- `wikicore check`: report unreadable pages, schema warnings and dangling links. Exit code 1 if any.
- `wikicore token add|list|revoke <client>`: manage client keys. Revoking a key also ends the
  OAuth sessions it created.
- `wikicore oauth revoke-all`: end every OAuth session (client keys stay valid).

## Customise

`_tools.yaml`:

```yaml
search:
  description_append: "Use for anything about my research, the people in it and its funders."
  parameters:
    query: "Names, project names or topic words."
bootstrap:
  disabled: true
```

`_instructions.md` frontmatter `name` sets the name clients show; its body is sent as the MCP
server instructions. Add a type with the `add_type` tool or by dropping a file in `_schema/`.

Server name and instructions are read at startup, so changes to `_instructions.md` need a restart
(a deploy does that). Tool descriptions refresh when the vault syncs.

### Environment

`wikicore serve` reads these variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `WIKICORE_VAULT` | `./vault` | Path to the vault (a git repo). |
| `WIKICORE_STATE` | `<vault parent>/state` | Where keys, OAuth state and repo clones live. Keep it outside the vault. |
| `WIKICORE_PUBLIC_URL` | `http://127.0.0.1:8050` | The URL clients reach the server at; used for OAuth. |
| `WIKICORE_HOST` | `127.0.0.1` | Address to bind. |
| `WIKICORE_PORT` | `8050` | Port to bind. |
| `WIKICORE_GIT_PUSH` | `1` | Push each commit to `origin`. `0`, `false` or `no` keeps commits local. |
| `WIKICORE_PULL_INTERVAL` | `300` | Seconds between background pulls of the vault. |
| `WIKICORE_REPO_TTL` | `300` | Seconds before a registered code repo is fetched again. |
| `WIKICORE_GITHUB_TOKEN` | unset | Token for cloning private GitHub repos (sent to github.com only). |
| `WIKICORE_DEV_CLIENT` | unset | Turns auth off; every call acts as this client. Local development only. |
| `WIKICORE_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Comma-separated Host headers the server accepts. Add your domain. |

### Code repos

Put a `repos.yaml` in the vault to let assistants read your code repos:

```yaml
repos:
  - name: analytical-engine
    url: https://github.com/example/analytical-engine
    project: analytical-engine   # optional: slug of a project page
    branch: main                 # optional
```

The repo tools are registered only when `repos.yaml` exists, and `repos: []` enables them with no
repos. Assistants can then add entries with `register_repo`. It refuses to run while any entry in
the file is unreadable, so fix those by hand first. Adding the file needs a restart.

## Deploy

Copy `.deploy.env.example` to `.deploy.env`, fill it in, run `./deploy.sh`. It sets up a system
user, `/opt/<app>`, a systemd service, an nginx vhost (uploads up to 26 MB) and a certificate, and
prints the deploy key to add (with write access) to your private vault repo. Run one server process
only.

## License

MIT
