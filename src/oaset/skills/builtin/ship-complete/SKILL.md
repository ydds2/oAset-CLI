# Ship complete

Never treat compiling code as done. The user sees the running system — pixels, tests, data — not your source.

## When to use

Any implementation, UI, TUI, feature, or "fix it" request. Especially when you are tempted to skip tests, skip a screenshot, stub a database, or omit the backend/frontend because a happy-path file would look finished.

## Steps

1. Map the stack the request actually needs (UI, API, data store, auth, jobs, migrations). List the layers. If you will skip one, say so *before* coding — never silently.
2. Read the existing files. Do not rewrite from a blank page. Do not invent a parallel stack next to the one already in the repo.
3. Implement the real path, not the demo path: no hardcoded happy-path only, no `TODO` left on the hot path, no mock the user will hit in normal use, no CSS that is "transparent" but still paints a fill.
4. After every batch of edits, gather evidence *this turn*:
   - `run_shell` the project's tests or build.
   - `lsp_diagnostics` on files you touched; fix what it reports.
   - If the work is visual (TUI, CSS, layout, web UI), look at the real screen (`browser_observe`, a screenshot, or a compositor dump). Reading the stylesheet is not visual proof.
5. If verification fails, fix the product. Do not redefine the task, weaken the assertion, or claim "the code is correct so the UI must be".
6. Only then write the answer. Lead with what the user can observe. If a layer is still missing, name it.

## Done means

- Tests or the build ran this turn, or you stated why they cannot.
- Visual work has a real-screen observation this turn.
- Every layer the user asked for exists, or was explicitly declined.
- You did not take the shortest path that only looks finished.
