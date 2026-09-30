# wikicore design

wikicore is a single-user MCP server over a git repo of Markdown notes, following the LLM-wiki
pattern: the owner's assistants (Claude, ChatGPT, coding agents) read and write one shared wiki, so
what one learns the others know. This document describes how it is built.

## 1. Architecture

Two things, deliberately separate:

- **wikicore** (this repo): a generic, instance-agnostic server. It knows nothing about any
  particular domain. It never contains instance data.
- **The vault**: the instance. A separate git repo of Markdown with YAML frontmatter
  (Obsidian-compatible) plus the files that define its behaviour: types, instructions, tool
  descriptions and the repo registry (section 4).

Stack: Python, Starlette, the official MCP Python SDK (streamable HTTP at `/mcp`), uv. No database,
no embeddings, no LLM calls on the server: the caller's assistant does all reasoning and writing.
The server does lookup, filing, git and auth.

The server is a single process. OAuth pending state (authorisation requests awaiting login) is held
in memory, so running more than one process would break logins. Deployment is generic: a system
user, a systemd service, a reverse proxy and a certificate; `deploy.sh` reads its settings from
`.deploy.env`. Configuration is through `WIKICORE_*` environment variables (see the README).

Modules in `src/wikicore/`: `vault` (parse, write, slugs, sections, index), `schema` (types),
`search`, `wiki` (all tool logic and the write path), `gitops`, `repos`, `custom` (instructions and
tool descriptions), `bootstrap`, `auth` (client keys), `oauth`, `web` (connect page, login,
upload, health), `server` (tool wrappers), `cli`.

## 2. The vault

### 2.1 Layout

```
people/        one page per person
orgs/          organisations
meetings/      one page per conversation
projects/      one page per project; projects are also the scopes
concepts/      vocabulary and ideas
decisions/     standing decisions with their reasons
papers/        one page per paper
learnings/     free-standing things learned
sources/       verbatim inputs (meeting notes, pasted text, uploads, bootstrap dumps); never edited
_files/        uploaded binaries (PDFs), referenced from sources/
_schema/       <type>.yaml, one per type; adding a type = adding a file
_instructions.md   usage rules served to every client as MCP instructions
_tools.yaml    tool description overrides
_meta/         server bookkeeping (bootstrap.yaml)
repos.yaml     registered code repos (optional)
index.md       generated on every write: every page, grouped by type, one line each
log.md         append-only, one line per change
```

`wikicore init <dir>` writes neutral defaults for all of this. On an existing vault it only adds
missing files and never overwrites edits.

### 2.2 Page shapes

All pages: frontmatter `type`, `title`, `aliases` (other names, for dedupe and search), `projects`
(list of `[[project]]` links, the scoping), `sources` (links to `sources/`), `created`, `updated`
and `added_by`. `type`, `created`, `updated` and `added_by` are set by the server. Relationships
are `[[slug]]` wiki-links.

- **person / org**: sections *Who*, *How I know them*, *What I want from them*, *What they want
  from me*, *Open threads*, *Timeline*. Fields `org`, `role`, `contact`.
- **meeting**: `date`, `with`, `projects`; *Summary*, *Decisions*, *Follow-ups*, *Notes*.
- **project**: `status`, `aliases`, `repos`; *Goal*, *Status*, *Open*, *Decisions*, *Log*.
- **concept**: *Definition*, *How we use it*, *Don't confuse with*, links.
- **decision**: `date`, `projects`, `status` (standing or superseded); *Decision*, *Why*,
  *Consequences*.
- **paper**: `authors`, `year`, `venue`, `doi`/`url`, `file`; *Gist*, *Why it matters*, *Key
  claims*, *Links*.
- **learning**: *What*, *Where from*, links.
- **source**: `kind`, `url`, `file`, `filed`, `filed_into`. Created by `save_source` and uploads.

Types are extensible: `add_type` writes a new `_schema/<type>.yaml` (description, fields, required
fields, section headings) and creates the folder. The server validates frontmatter against the
schema and returns warnings, never rejections, so filing never fails on a schema detail.

Slugs are lowercase ASCII, letters, digits and dashes. A title with no Latin letters gets a
`<type>-<hash>` slug so distinct titles stay distinct.

Sources are stored byte-for-byte: the text the caller sent is the body, and `update` refuses to
edit a source. Only its `filed` and `filed_into` fields change, when a page is filed from it.

A page whose frontmatter cannot be parsed still loads: it can be read (and appears in `wikicore
check`), but writes to it are refused with a message until the owner fixes it by hand.

### 2.3 `_instructions.md`

The single lever that makes every client behave the same, because chat products have no
CLAUDE.md. Its body is served verbatim as the MCP server `instructions`; its frontmatter `name` is
the server name clients show. The defaults tell assistants to:

- search before answering about anything the wiki may hold;
- write silently: file or update whatever durable comes up, then end the reply with a one-line
  `filed:` summary;
- `save_source` pasted meeting notes verbatim first, then file the meeting and update every person,
  org and project it touches;
- `save_source` a paper's text (or metadata plus the assistant's reading), then file a `paper` page;
- for coding agents: `get` the project page at session start and record decisions and todos there;
- dedupe: search names and aliases before creating; prefer `update` to a new page.

## 3. Customisation surface: the vault, not a fork

Clients decide when to call a tool mostly from its description and weight server instructions less
reliably, so the vault shapes tool descriptions as well as pages. wikicore is never forked per
instance.

| Vault file | Controls |
|---|---|
| `_instructions.md` | MCP server `instructions`; frontmatter `name` sets the server name clients show |
| `_tools.yaml` | per tool: `description` (replace) or `description_append`; per-parameter descriptions; `disabled: true` to hide a tool |
| `_schema/*.yaml` | each type's description, fields and sections; injected into the `search`, `file`, `update`, `list_notes` and `schema` descriptions so the client sees what every type is for |
| `repos.yaml` | presence enables the repo tools (an optional module); absent, they are not registered |

The server name and instructions are read at startup, so changing `_instructions.md` needs a
restart. Tool descriptions are rebuilt whenever the vault changes: after `add_type`, and after a
background sync pulls new commits. The tool set itself (including the repo tools) is fixed at
startup. An unreadable `_tools.yaml` or `_instructions.md` frontmatter falls back to defaults with
a log warning.

The only reason to change wikicore itself is a new kind of tool, which goes in as a generic
optional module enabled by a vault file, like repos.

## 4. MCP tools

Read:
- `search(query, type?, project?, limit?)`: keyword search over titles, aliases, frontmatter and
  bodies; returns slugs, titles, snippets.
- `get(slug)`: full page plus backlinks; unknown slugs return `did_you_mean`.
- `related(slug)`: outgoing links and backlinks, one hop.
- `list_notes(type, filters?, limit?)`: enumerate a type with frontmatter filters (exact match, or
  membership for list fields).
- `recent(n?, type?)`: latest changes, from `log.md`.
- `schema()`: all types with fields and sections, and the fields every page shares.
- `status()`: page counts per type, unfiled sources, bootstrap runs per client, problems.

Write:
- `save_source(text, kind?, title?, url?)`: saves verbatim to `sources/`, commits, and returns a
  `source_id`, existing pages the text mentions, and what to do next.
- `file(type, title, frontmatter?, body?, source_id?)`: creates a page. If a page of that type with
  the same title or alias exists it does not create; it returns the existing page and says to use
  `update`. `similar` lists near-matches. An empty body yields the type's section skeleton.
- `update(slug, section?, content?, mode=append|replace, frontmatter?, source_id?)`: edits one
  section (created if missing) and/or merges frontmatter (lists are unioned, null removes a field).
- `add_type(name, description, fields?, sections?, required?)`: extends the schema.

Setup:
- `bootstrap(done?, summary?, as_client?, created?, updated?)`: see section 6.

Every write appends a line to `log.md`, regenerates `index.md`, commits (author: the client name
from the auth token, for example `chatgpt` or `claude-code`) and pushes.

## 5. Code repos (optional module)

The registry is `repos.yaml` in the vault: name, URL, project, branch. Its presence enables the
tools; `repos: []` enables them with no entries. Adding the file needs a restart.

- `repos()`: registered repos with project and freshness.
- `register_repo(url, project?, name?)`: adds an entry to `repos.yaml` and clones. It refuses to
  run while any entry in the file is unreadable. URLs must be `https://`, `ssh://` or `git@`;
  anything git could read as an option or transport helper is refused.
- `repo_status(name)`: branch, recent commits, README head, freshness. It does not report open
  pull requests yet.
- `repo_read(name, path)` (text files, up to 200 kB), `repo_log(name, n?, path?)`,
  `repo_tree(name, path?)`. Paths are confined to the clone.

Clones live in the state directory, outside the vault, and are fetched on demand when older than
`WIKICORE_REPO_TTL`. A GitHub token, if set, is sent to github.com only. Fetch failures return the
stale state with its age, never an empty answer.

## 6. Bootstrap (per client, via a tool)

Each client has different context, so each is bootstrapped separately by calling `bootstrap` from
inside it. The tool returns a client-specific guide; the client's assistant does the work through
the normal write tools. The client is taken from the auth token and can be overridden with
`as_client`. Runs are idempotent: dedupe by title and alias means a second run updates rather than
duplicates.

- **claude-code**: list memory files (`~/.claude/projects/*/memory/*.md`) and notes-to-self files in
  registered repos; sort into in scope and out of scope by the rules in the instructions; show the
  owner both lists and wait for a yes; `save_source(kind=memory)` each, then file pages; add a
  "Moved to the wiki" pointer line to each moved memory file.
- **claude.ai**: use its memory and past-chat search, several searches per topic; summarise each
  conversation into `save_source(kind=chat-export)` and file.
- **chatgpt**: use saved memories and whatever chat history it can recall. It cannot search all past
  chats on demand, so the guide tells it to say the run is partial; a data export can be uploaded
  later through the upload page.
- **other**: gather what the assistant can reach and file it the same way.

When finished, the client calls `bootstrap(done=true, summary=..., created?, updated?)`. The server
records each run in `_meta/bootstrap.yaml` (client, date, summary, and the optional created and
updated page counts); `status()` shows which clients are done.

## 7. Clients

- **claude.ai and ChatGPT**: add as a custom connector (URL plus OAuth); ChatGPT via developer mode.
- **Claude Code and Codex**: HTTP MCP with a bearer key, for example
  `claude mcp add --scope user --transport http ...`.
- **Coding-agent conventions**: one line in the global agent instructions pointing at the wiki, and
  per repo "read the project page at session start; write decisions and todos there".

## 8. Auth

Single user. Keys are `wk_...` secrets created with `wikicore token add <client>`; each client name
is a label. Keys are stored hashed. CLI agents send a key as a bearer token.

claude.ai and ChatGPT cannot send a bearer header, so the server is also an OAuth 2.1 authorisation
server: dynamic client registration, PKCE, refresh tokens. Its login page asks the owner to paste a
key once per client. The key's client name becomes the commit author and the bootstrap label.

- Dynamic client registration is capped (200 clients) and unused registrations are pruned after an
  hour; pending authorisations are capped and expire.
- Redirect URIs must be `https`, or `http` on loopback, with no fragment or embedded credentials.
- Access and refresh tokens are stored as SHA-256 hashes, as are client keys.
- `wikicore token revoke <client>` also ends the OAuth sessions minted by that key.
  `wikicore oauth revoke-all` ends every OAuth session and leaves keys valid.
- Requests are checked against an allowed-hosts list (DNS-rebinding protection).
- `WIKICORE_DEV_CLIENT` turns auth off for local development.

## 9. Upload page

`/upload` is a small page for PDFs and text from a phone or browser, for when a chat product cannot
hand a file's full text to the MCP. The form takes a key, a file and an optional note. The file is
stored in `_files/`, text is extracted (pypdf for PDFs), and a `sources/` entry is saved marked
unfiled. `status()` reports unfiled sources and the instructions tell assistants to offer to file
them.

The request must carry a Content-Length header (else 411). The cap is 25 MB (413); the body is
also read through a bounded wrapper so a lying header cannot exceed it. A bad key gets 403 and a
malformed body a plain 400 message.

## 10. Errors and git

- The file on disk is the source of truth. Each write pulls with rebase, applies the change, logs,
  regenerates the index, commits and pushes, all under one lock.
- Git failures never lose a write. If the pull, commit or push fails, the change stays in the
  vault files and the tool result carries a `sync_warning` saying what happened. A failed push is
  retried by the background sync (every `WIKICORE_PULL_INTERVAL` seconds), which also pulls
  changes made elsewhere and reloads the vault. A rebase conflict is aborted and nothing is pushed
  until it is resolved by hand.
- Schema problems are warnings in the tool result, not failures.
- Tool errors are plain sentences the assistant can relay: a `VaultError` message is returned as
  `{"error": ...}`, and unexpected exceptions are logged and returned without a stack trace.

## 11. Testing

- pytest covers: vault parse and write, slug and alias dedupe, `update` section merge, frontmatter
  list union, `add_type`, index and log regeneration, schema loading, search, repo registry and
  status against local git repos, the OAuth flow and its limits, the web routes and upload, the
  CLI, bootstrap bookkeeping, tool-description refresh, and an end-to-end run over the MCP
  transport.
- `wikicore check` parses every page and reports unreadable pages, schema warnings and dangling
  links; it exits 1 if any are found.
- After a deploy, smoke test in each client: paste a sample meeting note, confirm the pages were
  filed and reported; ask a question the wiki can answer; call `repo_status` if repos are enabled.

## 12. Out of scope

Embeddings or a search index (add SQLite FTS behind `search` if it ever gets slow); server-side
LLM calls; multiple users; syncing from third-party CRMs or notetakers (meeting notes, for example
from a notetaker, arrive as pasted text through `save_source`); open-PR counts in `repo_status`;
running more than one server process.
