"""Per-client bootstrap: each assistant has different context (memory files, chat history, saved
memories), so each is walked through filing what it knows, once, using the normal write tools."""

from __future__ import annotations

COMMON_END = """
When done, call `bootstrap` again with done=true and a one-paragraph summary (how many sources
saved, pages created and updated, what you could not reach). Then tell the owner the same in a
few lines. Re-running is safe: `file` refuses duplicates, so a second run updates pages."""

GUIDES: dict[str, str] = {
    "claude-code": """You are in Claude Code on the owner's machine, which holds the most context.
1. List memory files: ~/.claude/projects/*/memory/*.md (skip MEMORY.md indexes). Also list
   notes-to-self files (e.g. WORKING-NOTES.md) in the repos returned by `repos`, if any.
2. Using the scope in `rules`, sort them into in scope / out of scope. Show the owner both lists
   and wait for their yes before filing anything.
3. For each file in scope: `save_source` (kind=memory, title=the file path, text=its full
   content), then `update` existing pages and `file` new ones from it (people, projects,
   decisions, concepts, open todos into the project's Open section).
4. After filing, add one line at the top of each moved memory file:
   "Moved to the wiki: [[slug]], [[slug]]." Do not delete memory files.""" + COMMON_END,
    "claude-ai": """You are in claude.ai.
1. Use your memory and your past-chat tools (conversation_search, recent_chats) if you have
   them. Run several searches for the topics in `rules`, not just one.
2. For each relevant conversation or memory item: `save_source` (kind=chat-export, title=chat
   title and date, text=a faithful summary keeping names, decisions, follow-ups and open
   questions), then `update` / `file` pages from it.
3. If past-chat search is off, say so and file what your memory holds.""" + COMMON_END,
    "chatgpt": """You are in ChatGPT.
1. Use your saved memories and whatever you can recall from chat history about the topics in
   `rules`. You cannot search all past chats on demand: tell the owner this bootstrap is partial
   and that a ChatGPT data export can be uploaded later through the wiki's upload page.
2. For each relevant item: `save_source` (kind=chat-export, text=a faithful summary keeping
   names, decisions, follow-ups), then `update` / `file` pages from it.""" + COMMON_END,
    "other": """1. Gather what you know about the owner's work that falls within `rules`: your memory,
   past conversations you can reach, files you can read.
2. For each item: `save_source` (kind=chat-export or memory), then `update` / `file` pages.""" + COMMON_END,
}


def guide_for(client: str) -> tuple[str, str]:
    c = client.lower().replace(" ", "-").replace(".", "-")
    if "claude-code" in c:
        key = "claude-code"
    elif "chatgpt" in c or "openai" in c:
        key = "chatgpt"
    elif c.startswith("claude"):
        key = "claude-ai"
    else:
        key = "other"
    return key, GUIDES[key]
