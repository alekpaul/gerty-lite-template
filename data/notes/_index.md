# Notes

Free-form structured notes. The bot reads/writes here via `read_file` /
`write_file`. Daily notes go under `Progress/YYYY-MM-DD.md`.

This is the bot's default `NOTES_ROOT`. If you have an existing Obsidian vault
or another notes system, point `NOTES_ROOT` at it in `config/.paths` and the
bot will read/write there instead.
