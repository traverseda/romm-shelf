# Rules for coding agents

- Read [docs/design.md](docs/design.md) first. Every change has to fit the governing rule there: romm-shelf keeps no state of its own.
- Put decisions that need the owner's judgement in [todo.md](todo.md). Don't make those decisions yourself.
- Never commit ROMs, BIOS/firmware files, real save files, or API tokens. Fixtures use dummy bytes.
- Against a live server or Deck, reads are fine. Writes (uploading saves, deleting saves or ROMs) go only to the designated test ROM unless the owner says otherwise.
- Keep building a plan separate from executing it. Anything that changes files or the server has to be printable as a plan first.
- A UI feature without a CLI equivalent is a bug.
- Controllers come from Steam Input, in Steam's order: its first controller is player 1. No emulator setting may bypass or reorder that. See design.md § Controller rule.

## Commands

- `uv run pytest` — run all tests. `uv run pytest -k test_name` for a subset.
- `uv run pytest -m live` — run live tests against a real RomM (`ROMM_SHELF_TEST_URL` required; most tests skip).
- `uv run romm-shelf <command>` — CLI entry point.
- `nix build .#appimage` — the AppImage for the Deck (`result/romm-shelf-<version>-x86_64.AppImage`); runs the tests first. Nix only sees files git tracks, so use `nix build path:.#appimage` while new files are uncommitted.
- `uv run python _make_screenshot.py` — start mock server + Playwright screenshot for README.
- No lint, typecheck, or formatter config. Code ships as-is.

## Structure

- **Python 3.12+ monorepo, 4 packages** under `src/`:
  - `romm_shelf` — core (RomM client, RetroDECK layout, sync engine, adapters, CLI, daemon, UI templates)
  - `romm_shelf_retroarch` — RetroArch save adapter
  - `romm_shelf_dolphin` — Dolphin GC/Wii adapter
  - `romm_shelf_eden` — Eden (Switch) emulator, bundled and external, and its adapter
- Packages are registered via `romm_shelf.adapters` and `romm_shelf.externals` entry-point groups.
- RomM typed models are generated from `docs/reference/romm-openapi-5.4.1.json`.
- UI is a static Quasar SPA in `src/romm_shelf/web/` (no build step; served as static files by the daemon).
- No CI, no Makefile, no pre-commit.

## Testing

- Fixtures live in `tests/conftest.py`: `deck` creates a fake RetroDECK filesystem under `tmp_path`; `rom()` creates a test RomM object; `remote` provides sample ROM lists.
- All tests are contract tests — no server or Deck required — unless marked `live`.
- Live tests use the designated test ROMs from [todo.md](todo.md).
