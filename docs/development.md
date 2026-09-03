# Development and validation

## Environment

The project requires Python 3.14 or newer and uses uv for locking, environments,
commands, and builds.

```bash
git clone https://github.com/jason-r-becker/fantasyfootball.git
cd fantasyfootball
./setup.sh
```

`setup.sh` runs `uv sync --extra dev`.

## Project map

```text
src/fantasyfootball/draft_app.py       CLI and local HTTP server
src/fantasyfootball/draft_state.py     validation, state, CSV persistence
src/fantasyfootball/draft_sources.py   read-only Sleeper/ESPN adapters
src/fantasyfootball/draft_analysis.py  charts and draft optimization
src/fantasyfootball/web/               packaged browser assets
tests/                                 draft-room tests
docs/                                  user and contributor documentation
examples/                              placeholder-only configuration
```

Several older analysis modules are script-oriented. Keep draft-room changes
focused on the four modules above and their tests unless the task explicitly
includes the legacy workflows.

## Validation suite

Run these commands from the repository root after code or documentation work:

```bash
uv run pytest
uv run ruff check src/fantasyfootball/draft_app.py \
  src/fantasyfootball/draft_state.py \
  src/fantasyfootball/draft_sources.py \
  src/fantasyfootball/draft_analysis.py \
  tests/test_draft_app.py \
  tests/test_draft_sources.py \
  tests/test_draft_state.py \
  tests/test_projection_script.py
uv run ruff format --check src/fantasyfootball/draft_app.py \
  src/fantasyfootball/draft_state.py \
  src/fantasyfootball/draft_sources.py \
  src/fantasyfootball/draft_analysis.py \
  tests/test_draft_app.py \
  tests/test_draft_sources.py \
  tests/test_draft_state.py \
  tests/test_projection_script.py
node --check src/fantasyfootball/web/draft.js
bash -n setup.sh
uv build
```

The targeted Ruff scope covers the maintained draft-room code. Expanding it to
all historical scripts may reveal unrelated legacy formatting or lint debt.

Check the built artifacts without installing private data:

```bash
unzip -l dist/*.whl
tar -tf dist/*.tar.gz
```

The wheel must include `fantasyfootball/web/draft.html`, `draft.css`, and
`draft.js`. The source distribution must include `.env.example`, `readme.md`,
`docs/`, `examples/`, `scripts/`, and `setup.sh`.

## Documentation checks

Before merging documentation:

1. Resolve every relative Markdown link from the file containing it.
2. Search for the obsolete weekly path:

   ```bash
   rg -n 'weekly[/].*(week|[0-9]+)' readme.md docs examples
   ```

3. Confirm the example parses and deliberately has no assigned slot:

   ```bash
   uv run python -m json.tool examples/sleeper-config.example.json >/dev/null
   uv run python -m json.tool examples/espn-config.example.json >/dev/null
   ```

4. Compare `uv run fantasy-draft --help` with every documented flag and
   default.

## Clean-snapshot rehearsal

Tests in the normal checkout can accidentally benefit from ignored private
files. Rehearse from a temporary copy containing only paths known to Git, but
copy their current worktree contents so uncommitted documentation is included:

```bash
SNAPSHOT_DIR="$(mktemp -d)"
git ls-files --cached --others --exclude-standard -z \
  | tar --null -T - -cf - \
  | tar -xf - -C "$SNAPSHOT_DIR"
cd "$SNAPSHOT_DIR"
uv sync --extra dev
uv run pytest
node --check src/fantasyfootball/web/draft.js
bash -n setup.sh
uv build
```

Use synthetic fixture data for an automated launch test. A real private
rehearsal should copy only `config.json`, `clean.csv`, optional `adp.csv`, and
optional `source_player_map.json` into this temporary snapshot, run practice
mode, and remove the entire temporary directory afterward. Never print or add
those files to Git.
