# Vault

Free-form content the bot helps you create:

- `inbox/` — raw captures, things the bot saves on your behalf
- `drafts/` — work-in-progress writing
- `published/` — posted / finalized content
- `resources/` — curated reference material
- `templates/` — reusable templates the bot can pull from

The bot reads/writes here via `read_file` / `write_file` with paths like
`inbox/note.md` (no prefix means vault root).
