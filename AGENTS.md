# Rules for coding agents

- Read [docs/design.md](docs/design.md) first. Every change has to fit the governing rule there: romm-shelf keeps no state of its own.
- Put decisions that need the owner's judgement in [todo.md](todo.md). Don't make those decisions yourself.
- Never commit ROMs, BIOS/firmware files, real save files, or API tokens. Fixtures use dummy bytes.
- Never fetch games or firmware from anywhere except the user's own RomM server.
- Against a live server or Deck, reads are fine. Writes (uploading saves, deleting saves or ROMs) go only to the designated test ROM unless the owner says otherwise.
- Keep building a plan separate from executing it. Anything that changes files or the server has to be printable as a plan first.
- A UI feature without a CLI equivalent is a bug.
