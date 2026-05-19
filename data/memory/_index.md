# Memory Index

This is the bot's memory. Each entry is a `.md` file inside `entries/` with
frontmatter (name, description, saved_at) and a body in Markdown.

The bot maintains this index automatically when it calls `save_memory(...)`.
You can also edit entries by hand — keep the frontmatter intact so the bot
can still find them.

## Entries

<!-- One line per memory, kept in sync by save_memory. Format:
- [name](name.md) — short description
-->
