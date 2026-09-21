# Agent prompt patterns — cross-tool reference knowledge base

Why this file exists: oAset's system prompt should be engineered against what
the field has converged on, not reinvented per release. This is the distilled
pattern library from the major terminal coding agents' prompts (public leaks,
open-source repos and third-party analyses — patterns only, no verbatim text).
Revisit when a major tool ships a prompt rewrite.

Last reviewed: 2026-09-19 (oAset PROMPT_VERSION 7: volatile facts — date, git snapshot — moved to the prompt TAIL so the stable head matches across days/sessions for implicit-cache providers).

## Claude Code (Anthropic)

Structure: not one block — assembled from dozens of conditional parts chosen
per environment (OS, shell, workspace state, active tools). Core disciplines:

- **Identity as a pair-programmer**, tone direct, "ordinary technical
  colleague"; conciseness rules with explicit anti-padding ("do not say
  fluff like 'Great question'").
- **Tool-use budgeting**: prefer targeted reads/greps over whole-file reads;
  batch independent calls in parallel; one tool call per dependent step.
- **Verification before claiming done**: run the thing you changed; visual
  work needs looking at the rendered screen, not the source.
- **Task-list hygiene**: exactly one in-progress item, re-read the original
  request before declaring completion.
- **Persistence**: keep going until the user's goal is genuinely met — the
  loop, not one reply, is the unit of work.
- **Untrusted-content firewall**: text from tools/web results is data, never
  instructions.

## Gemini CLI (Google)

Open source; the prompt ships in the repo and can be overridden wholesale
(`GEMINI_SYSTEM_MD`) or layered (`GEMINI.md` hierarchical files).

- **A proactiveness dial** (0–4) as a first-class knob: from "answer only"
  to "propose then act" to "act without asking". One scalar where other
  tools hide the choice in scattered rules.
- **Environment block first** (cwd, OS, date), then rules, then tool
  policies — same layering oAset uses.
- **Planning discipline**: for non-trivial tasks, state a plan before
  executing; prefer `google_search` grounding for freshness-sensitive facts.

## OpenAI Codex CLI

The gpt-5-codex prompt (leaked, widely mirrored) leads with collaboration
framing: agent and user share one workspace, work continues **until the
goal is genuinely achieved** — persistence stated as the opening sentence,
not a buried rule.

- **AGENTS.md kept deliberately short**; everything not needed at all times
  moved out of the always-loaded context (progressive disclosure — the same
  reason oAset lists skills as one-line pointers with read-on-demand).
- **Shell-first verification loop**: run tests/lint after changes; treat
  failed commands as the primary feedback signal.
- **Safety by sandbox tiers**: read-only / workspace-write / full network,
  chosen before the run — the model is told which tier it is in.

## Cross-tool convergences (all three + oAset)

| Pattern | Why everyone converged on it |
|---|---|
| Identity + environment + rules layering | Stable prefix (cache), scannable, testable |
| Verify-then-done (run it, don't assert it) | "Looks finished" is the top failure mode |
| Untrusted tool/web text ≠ instructions | Prompt injection is the top remote risk |
| Todo list with one in-progress item | Long tasks derail without a ratchet |
| Conciseness with named anti-patterns | Models pad unless told exactly what to cut |
| Persistence until goal met | One reply is never the unit of work |

## Where oAset deliberately goes further

- **ACCEPTANCE FIRST + re-read the original request**: acceptance criteria
  are restated as observable checks, and the final pass is against the
  user's original words — omissions hide in the phrasing, not in the todo
  list the agent wrote itself (rule 2, PROMPT_VERSION 6).
- **COMPLETE THE REQUIREMENT, NOT THE SENTENCE**: one-sided user requests
  get their implied requirements enumerated (happy/failure path, edge
  cases, what gets checked), with a clarify-vs-default decision rule (rule
  3). Field prompts verify what was asked; they do not systematically
  complete what was meant.
- **FULL STACK, NO SILENT CUTS**: implied systems must enumerate layers and
  name every layer that will NOT ship, before coding (rule 4).
- **Evidence gate** (`/evidence`, replayable receipts) turns the
  verify-then-done convention into an auditable artifact rather than a
  promise.

## Prompt-cache engineering (the discipline the prompt itself must keep)

Prefix caching (Anthropic markers, OpenAI/DeepSeek implicit, Gemini implicit
`cachedContentTokenCount`) reuses the longest identical request prefix.
Prompt-side rules that protect it:

1. **Build the system prompt once per session** — never per turn. oAset's
   `build_system_prompt` runs at host init; the git status snapshot inside
   it is frozen by design (staleness is the accepted cost).
2. **Stable ordering**: identity/rules/policy first, repo-stable blocks
   (skills, agents, project context) next, volatile facts (per-day date,
   git snapshot) LAST — implicit-cache providers match the literal token
   prefix, so a date at the head broke it every midnight; oAset v7 moved
   date/git into a `# Now` tail section.
3. **Append-only mindset**: nothing already sent to the model is edited in
   place mid-session (rewind/compaction are explicit user-scale events).
4. **Tool table order is part of the prefix** — registration order stays
   deterministic; mid-session MCP additions append, never reorder.
5. **Rolling breakpoints on the last TWO user-turn boundaries** (Anthropic):
   markers only write cache entries, reads happen by prefix match — one
   marker carries the conversation across consecutive turns; the second
   (one turn back) survives idle gaps that outlive the newest entry's
   5-minute TTL. tools + system + 2 rolling = the 4-breakpoint budget.
   `ttl: "1h"` (extended-TTL beta, 2x write cost) is the opt-in for
   slow-paced sessions.
6. **Measure it**: `/context` shows last-turn hit rate and session-cumulative
   reuse — a cache regression is visible the same day, not at invoice time.

Boundary closed 2026-09-19: the Bedrock provider (Anthropic-on-Bedrock,
same Messages wire format) now attaches the same cache_control breakpoints
as anthropic-native — system block + tools[-1] + two rolling user-turn
boundaries, gated on the same `prompt_cache` flag (default on). The 1h
extended TTL stays anthropic-native-only (no Bedrock equivalent).

## Sources

- [Piebald-AI/claude-code-system-prompts (GitHub)](https://github.com/Piebald-AI/claude-code-system-prompts) — collected Claude Code prompts incl. internal agents
- [How Claude Code Builds a System Prompt — dbreunig.com](https://www.dbreunig.com/2026/04/04/how-claude-code-builds-a-system-prompt.html) — conditional-assembly analysis
- [Highlights from the Claude 4 system prompt — Simon Willison](https://simonwillison.net/2025/May/25/claude-4-system-prompt/) — tone/tool-use rules breakdown
- [google-gemini/gemini-cli (GitHub)](https://github.com/google-gemini/gemini-cli) — open-source prompt source
- [Proactiveness considered harmful?! — danicat.dev](https://danicat.dev) — Gemini CLI proactiveness dial deep-dive
- [Transparency in Prompt Construction for Codex — OpenAI Community](https://community.openai.com) — AGENTS.md short-by-design rationale
- [system_prompts_leaks / codex-full (GitHub)](https://github) — leaked Codex prompt mirror ("collaborate until the goal is genuinely achieved")
