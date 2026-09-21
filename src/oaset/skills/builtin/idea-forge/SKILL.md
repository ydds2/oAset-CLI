# Idea forge

Turn a raw, dynamic requirement into a confirmed plan, then build it. The main model refines the prompt itself (optimize + gap-fill from same-category products), maps the tech route from open source and mainstream market practice, and NEVER builds before the user confirms.

## When to use

The user brings a product or feature idea in any shape — one sentence, a wish, "做个像 X 的东西", a half-formed spec. Also when they say 需求锻造 / idea-forge / "先做方案". Not for bug fixes or concrete change requests (ship-complete owns those).

## Flow — five phases, in order, no skipping

### 1. Intake

Restate the raw request in one line. Identify the product category (e.g. 终端工具 / Web 面板 / 桌面应用 / CLI+服务). If the request is already a full spec, say so and go light on phase 2.

### 2. Refine the requirement (main model, directly)

- Rewrite the requirement as a structured spec: 背景 / 目标用户 / 核心场景（≤3）/ 验收标准（用户可观察的，逐条可验证）.
- **Gap-fill from same-category products**: `web_search` for 2–3 products in the same direction. From each, extract what its users treat as STANDARD (the table-stakes the user did not spell out — e.g. a paste target needs a clipboard story; a panel needs an empty state).
- Classify every requirement into one of four buckets and list them in a table: ①用户原话 ②同类产品标配（隐含，补齐） ③本项目特有 ④明确排除（with the reason the user can veto).
- If no search engine is configured, say so and fill bucket ② from your training knowledge, labelled "未联网核实" — never present it as researched.

### 3. Draw the tech map (grounded, not invented)

- Open-source grounding: `web_search` GitHub for 2–4 candidate OSS projects per layer the product needs. Name them (repo, license when visible, one-line why). When nothing credible exists for a layer, say "自研" — an invented dependency is worse than none.
- Market mainstream route: state the stack the market commonly uses for this category today, and the one oAset/the repo already has. Inside an existing repo, the existing stack wins unless the user says otherwise.
- Produce, in the plan doc:
  - a layered architecture outline (ASCII tree);
  - a mermaid `flowchart` of build order with dependencies;
  - phased milestones (每阶段有可观察产出), risks/unknowns, and what is deliberately deferred.
- Cite the URL for every factual claim taken from the web.

### 4. Confirmation gate — hard stop

- Write the whole plan to `.oaset/plans/<slug>.md`（slug from the request, kebab-case）: spec + bucket table + OSS table + both diagrams + phases + risks.
- Show the user: the plan path, the bucket-② additions (they must SEE what you added beyond their words), and the phase list.
- `ask_user` with explicit options: 确认方案 / 调整（说明改哪）/ 放弃. Record the decision and timestamp in the plan doc.
- **Do not write any product code before a 确认.** A silence, a vague "行吧", or a tangent is not 确认 — ask again or stop. Mid-build scope changes re-enter this gate for the changed part only.

### 5. Build the confirmed plan

- `todo_write` one task per phase, exactly one in_progress; implement in dependency order.
- ship-complete discipline applies from here: real path not demo path, verify every batch (`run_shell` tests/build, `lsp_diagnostics`, real-screen for visual work), name any layer you skip.
- Finish by mapping each acceptance criterion from phase 2 to its evidence.

## Done means

- The plan doc exists, carries the user's 确认 record, and every phase landed maps back to a bucket-①②③ requirement.
- Nothing in bucket ② was built silently — the user saw the additions at the gate.
- The diagrams in the doc match what was actually built (update them if reality diverged).
