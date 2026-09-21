# Contributing

oAset is a local-first terminal coding agent. Before sending a change,
read the two paragraphs below — they encode the project's non-negotiables
and will save us both a review round.

## The gates

```bash
python -m pytest -q                # hermetic suite (no live network — enforced)
python -m ruff check src/ tests/
python -m mypy src/oaset --ignore-missing-imports
```

All three green in **your exact tree** before you write the commit
message. A commit message that claims a verification must have run that
verification against that tree. Every behavior fix lands with a test
that fails before the fix and passes after.

## Non-negotiables (design constraints, not style)

1. **Local-first defaults** — approval silence/timeout denies; the
   gateway binds loopback; zero telemetry. Anything that phones home by
   default will not merge.
2. **Credentials never leave `~/.oaset/credentials/`** — not config.toml,
   not transcripts, not logs, not exceptions.
3. **One door** — every surface (TUI, CLI, ACP, gateway, SDK) goes
   through SessionHost/AgentKernel. Do not construct AgentLoop directly.
4. **Honest UI** — unknown model capabilities, unsupported knobs, dropped
   events: say so, never fake success. A wrong claim in a commit message
   or a UI string is a bug.
5. **CJK-aware** — any width/truncation math must be cell-aware
   (`oaset.utils.cell_width`), not character-counting.
6. **Windows is a first-class platform** — the maintainer develops on
   Windows; line-ending/encoding/frame assumptions get caught in CI and
   in review.

## Pull requests

- Small, described in terms of observable behavior.
- New user-facing strings go through `src/oaset/i18n.py` (zh + en).
- New commands need registry entries (`src/oaset/tui/commands.py`) with
  `cmd_*` and `effect_*` catalog keys — CI tests fail without them.
- Docs live in README.md (the single trusted source); deep research notes
  go under `docs/research/`.

## Reporting bugs

Open an issue with: the `oaset --version`, OS, what you ran, what you
saw, what you expected. Transcripts under `~/.oaset/sessions/` help —
check them for secrets first (they shouldn't contain any, but look).
