"""i18n framework: catalog + t(key) with fallback.

Language resolution: in-process set_language > OASET_LANG env > config
ui.language > "zh". First launch asks 中文 / English before the TUI paints.
Catalog covers the core UI surface (welcome, status hints, working/ready,
common notices); English is the source of truth, zh-CN provided.
"""

from __future__ import annotations

import os

CATALOG: dict[str, dict[str, str]] = {
    # I18N-01 closeout: update center strings (render + errors)
    "upd_url_scheme": {
        "en": "update source must be http/https: {url!r}",
        "zh": "更新来源只允许 http/https：{url!r}",
    },
    "upd_url_host": {
        "en": "update source is missing a hostname: {url!r}",
        "zh": "更新来源缺少主机名：{url!r}",
    },
    "upd_rollback_file_gone": {
        "en": "{path}: file no longer exists; cannot roll back by key",
        "zh": "{path}: 文件已不存在，无法按键回滚",
    },
    "upd_rollback_read_failed": {
        "en": "{path}: failed to read; cannot roll back by key: {exc}",
        "zh": "{path}: 读取失败，无法按键回滚: {exc}",
    },
    "upd_rollback_not_object": {
        "en": "{path}: root is not an object; cannot roll back by key",
        "zh": "{path}: 根节点不是对象，无法按键回滚",
    },
    "upd_rollback_write_failed": {
        "en": "{path}: by-key rollback write failed: {exc}",
        "zh": "{path}: 按键回滚写入失败: {exc}",
    },
    "upd_rollback_key_failed": {
        "en": "{path}: by-key rollback failed: {kind}: {exc}",
        "zh": "{path}: 按键回滚失败: {kind}: {exc}",
    },
    "upd_session_closed": {
        "en": "HTTP session already closed; refusing new requests",
        "zh": "HTTP 会话已关闭，拒绝在关闭后发起请求",
    },
    "upd_no_dir_restorer": {
        "en": "{dest}: missing directory-level restorer; core tree not restored",
        "zh": "{dest}: 缺少目录级恢复器，未恢复 core 树",
    },
    "upd_backup_missing": {
        "en": "{dest}: core backup directory missing; cannot restore",
        "zh": "{dest}: core 备份目录缺失，无法恢复",
    },
    "upd_restore_failed": {
        "en": "reverse-order restore failed {dest}: {kind}: {exc}",
        "zh": "逆序恢复失败 {dest}: {kind}: {exc}",
    },
    "upd_plan_title": {
        "en": "[b]oAset update plan[/b]",
        "zh": "[b]oAset 更新计划[/b]",
    },
    "upd_plan_empty": {
        "en": "(nothing installable)",
        "zh": "（无可安装目标）",
    },
    "upd_plan_hint": {
        "en": "[dim]apply: oaset update apply <name> (hash check + backup + rollback)[/dim]",
        "zh": "[dim]apply: oaset update apply <name>（hash 校验 + 备份 + 可回滚）[/dim]",
    },
    "upd_check_title": {
        "en": "[b]oAset update check[/b]",
        "zh": "[b]oAset 更新检查[/b]",
    },
    "upd_refresh_title": {
        "en": "[b]oAset catalog refresh[/b]",
        "zh": "[b]oAset 目录刷新[/b]",
    },
    "upd_list_title": {
        "en": "[b]oAset update catalog[/b]",
        "zh": "[b]oAset 更新目录[/b]",
    },
    "upd_plan_action_title": {
        "en": "[b]oAset update {action}[/b]",
        "zh": "[b]oAset update {action}[/b]",
    },
    "upd_result_when": {
        "en": "catalog at: {when}{cached} · current version {version}",
        "zh": "目录时间: {when}{cached} · 当前版本 {version}",
    },
    "upd_from_cache": {
        "en": " (cached)",
        "zh": " (缓存)",
    },
    "upd_entries_empty": {
        "en": "(catalog is empty)",
        "zh": "（目录为空）",
    },
    "upd_detail_source": {
        "en": "    source: {src}  sha256: {digest}",
        "zh": "    来源: {src}  sha256: {digest}",
    },
    "upd_detail_compat": {
        "en": "    compat: requires_oaset {req}",
        "zh": "    兼容: requires_oaset {req}",
    },
    "upd_detail_perms": {
        "en": "    permissions: {perms}",
        "zh": "    权限: {perms}",
    },
    "upd_detail_published": {
        "en": "    published: {published}",
        "zh": "    发布: {published}",
    },
    "upd_cache_write_failed": {
        "en": "failed to write the catalog cache: {exc}",
        "zh": "无法写入目录缓存: {exc}",
    },
    "upd_supported_actions": {
        "en": "supported actions: check / refresh / list / plan",
        "zh": "支持的动作: check / refresh / list / plan",
    },
    "upd_fetch_failed": {
        "en": "failed to fetch the catalog: {kind}: {exc}",
        "zh": "目录获取失败: {kind}: {exc}",
    },
    "upd_cat_sig_failed": {
        "en": "catalog signature verification failed: {exc}",
        "zh": "目录签名校验失败: {exc}",
    },
    "upd_cat_sig_hint": {
        "en": "confirm the catalog source matches a publisher key in [update] trusted_keys",
        "zh": "确认目录来源与 [update] trusted_keys 中的发布者公钥一致",
    },
    "upd_cat_signed": {
        "en": "catalog signature verified (key: {key_id})",
        "zh": "目录签名已验证（key: {key_id}）",
    },
    "upd_cat_unsigned": {
        "en": "catalog is unsigned — entries are protected by per-entry sha256 only",
        "zh": "目录未签名——条目仅由逐条目 sha256 保护",
    },
    "upd_art_sig_failed": {
        "en": "{name}: artifact signature verification failed: {exc}",
        "zh": "{name}: 工件签名校验失败: {exc}",
    },
    "upd_art_sig_hint": {
        "en": "confirm the publisher key is in [update] trusted_keys, or re-fetch the catalog from a trusted source",
        "zh": "确认发布者公钥在 [update] trusted_keys 中，或从可信源重新获取目录",
    },
    "upd_download_failed": {
        "en": "{label}: download failed: {kind}: {exc}",
        "zh": "{label}: 下载失败: {kind}: {exc}",
    },
    "upd_download_empty": {
        "en": "{label}: downloaded payload is empty",
        "zh": "{label}: 下载内容为空",
    },
    "upd_too_large": {
        "en": "{label}: payload size {size} exceeds the limit {limit}",
        "zh": "{label}: 包体积 {size} 超过上限 {limit}",
    },
    "upd_already_current": {
        "en": "{current} is already {version} or newer",
        "zh": "当前 {current} 已是 {version} 或更新",
    },
    "upd_busy": {
        "en": "another update transaction is running; concurrent apply refused.",
        "zh": "另一个更新事务正在进行，已拒绝并发 apply。",
    },
    "upd_busy_hint": {
        "en": "retry after it finishes; delete {lock} if no process is running",
        "zh": "等待其结束后重试；确认无进程在跑时可删除 {lock}",
    },
    "upd_targets_required": {
        "en": "apply needs explicit target names; implicitly installing the whole catalog is refused.",
        "zh": "apply 必须显式给出要安装的目标名，拒绝隐式安装目录中的全部条目。",
    },
    "upd_targets_hint": {
        "en": "example: oaset update apply demo-plugin",
        "zh": "示例: oaset update apply demo-plugin",
    },
    "upd_no_target": {
        "en": "{name}: no installable target in the catalog",
        "zh": "{name}: 目录中没有可安装的目标",
    },
    "upd_kind_not_implemented": {
        "en": "{name}: kind={kind} has no install transaction implemented",
        "zh": "{name}: kind={kind} 的安装事务未实现",
    },
    "upd_txn_rolled": {
        "en": "transaction {txn} rolled back {count} committed targets in reverse order",
        "zh": "事务 {txn} 已逆序回滚 {count} 个已提交目标",
    },
    "upd_txn_clean": {
        "en": "({names}): the apply left no partial install.",
        "zh": "（{names}）：本次 apply 未留下部分安装。",
    },
    "upd_none": {
        "en": "none",
        "zh": "无",
    },
    "upd_txn_interrupted": {
        "en": "transaction {txn} was interrupted; committed steps were rolled back in reverse order.",
        "zh": "事务 {txn} 被中断，已逆序回滚已提交的步骤。",
    },
    "upd_source_checkout": {
        "en": "source/editable install detected ({pkg_dir}). Transactional core upgrades refuse to overwrite a dev tree;\nuse pip install oaset-cli==<version> instead.",
        "zh": "检测到源码/可编辑安装（{pkg_dir}）。core 事务化升级拒绝覆盖开发树；\n请改用 pip install oaset-cli==<version>。",
    },
    "upd_missing_sha": {
        "en": "{name}: core release has no sha256; installing unverifiable packages is refused",
        "zh": "{name}: core 发布物缺少 sha256，拒绝安装不可校验的包",
    },
    "upd_entry_missing_sha": {
        "en": "{name}: catalog entry has no sha256; installing unverifiable packages is refused",
        "zh": "{name}: 目录条目缺少 sha256，拒绝安装不可校验的包",
    },
    "upd_sha_mismatch": {
        "en": "{name}: sha256 mismatch (expected {want}…, got {got}…)",
        "zh": "{name}: sha256 不匹配（期望 {want}…，实际 {got}…）",
    },
    "upd_unsafe_path": {
        "en": "{name}: release contains an unsafe path {member!r}",
        "zh": "{name}: 发布物包含不安全路径 {member!r}",
    },
    "upd_bad_core_zip": {
        "en": "{name}: release lacks oaset/__init__.py; not a valid core package",
        "zh": "{name}: 发布物缺少 oaset/__init__.py，不是合法的 core 包",
    },
    "upd_invalid_zip": {
        "en": "{name}: release is not a valid zip: {exc}",
        "zh": "{name}: 发布物不是有效 zip: {exc}",
    },
    "upd_dryrun_core": {
        "en": "{name} v{version}: [dry-run] verified; no files replaced.",
        "zh": "{name} v{version}: [dry-run] 校验通过，未替换任何文件。",
    },
    "upd_core_replace_failed": {
        "en": "{name}: core replacement failed; restored from backup: {exc}",
        "zh": "{name}: core 替换失败，已从备份恢复: {exc}",
    },
    "upd_core_done": {
        "en": "{name} v{version} upgraded transactionally (backup {backup}).\nRestart oaset to load the new version.",
        "zh": "{name} v{version} 已事务化升级（备份 {backup}）。\n重启 oaset 以加载新版本。",
    },
    "upd_dryrun_plugin": {
        "en": "{name} v{version}: [dry-run] verified; nothing written to {dest}.",
        "zh": "{name} v{version}: [dry-run] 校验通过，未写入 {dest}。",
    },
    "upd_plugin_write_failed": {
        "en": "{name}: write failed; old version restored: {exc}",
        "zh": "{name}: 写入失败，已恢复旧版本: {exc}",
    },
    "upd_plugin_done": {
        "en": "{name} v{version} installed. Run /reload-plugins in the TUI or restart to load it.",
        "zh": "{name} v{version} 已安装。在 TUI 中运行 /reload-plugins 或重启以加载。",
    },
    "upd_mcp_missing_install": {
        "en": "{name}: mcp entry lacks install.url or install.command",
        "zh": "{name}: mcp 条目缺少 install.url 或 install.command",
    },
    "upd_mcp_bad_servers": {
        "en": "{name}: mcp.json mcpServers is not an object; refusing to rewrite",
        "zh": "{name}: mcp.json 的 mcpServers 不是对象，拒绝改写",
    },
    "upd_dryrun_mcp": {
        "en": "{name} v{version}: [dry-run] would write mcp.json (unchanged).",
        "zh": "{name} v{version}: [dry-run] 将写入 mcp.json（未改动）。",
    },
    "upd_mcp_write_failed": {
        "en": "{name}: mcp.json write failed; restored: {exc}",
        "zh": "{name}: mcp.json 写入失败，已恢复: {exc}",
    },
    "upd_mcp_done": {
        "en": "{name} v{version} written to mcp.json. Run /reload-mcp to load.",
        "zh": "{name} v{version} 已写入 mcp.json。运行 /reload-mcp 加载。",
    },
    "upd_provider_missing_url": {
        "en": "{name}: provider entry lacks install.base_url",
        "zh": "{name}: provider 条目缺少 install.base_url",
    },
    "upd_model_missing_provider": {
        "en": "{name}: model entry lacks install.provider",
        "zh": "{name}: model 条目缺少 install.provider",
    },
    "upd_provider_added": {
        "en": "provider {name} added to config (default unchanged)",
        "zh": "provider {name} 已加入配置（未切换默认）",
    },
    "upd_model_added": {
        "en": "model {mid} added to config (default unchanged; verify connectivity before use)",
        "zh": "model {mid} 已加入配置（未切换默认；使用前请自行验证连通性）",
    },
    "models_add_title": {
        "en": "oAset models add — connect a new provider/model",
        "zh": "oAset models add — 接入新的 provider/model",
    },
    "models_add_configured": {
        "en": "configured providers: {list}",
        "zh": "已配置 providers: {list}",
    },
    "models_add_provider_prompt": {
        "en": "Provider name (new or existing)",
        "zh": "Provider 名称（新名字或已存在的名字）",
    },
    "models_add_provider_empty": {
        "en": "provider name cannot be empty",
        "zh": "provider 名称不能为空",
    },
    "models_add_reusing": {
        "en": "reusing configured {provider}: {url}",
        "zh": "复用已配置的 {provider}: {url}",
    },
    "models_add_base_url": {
        "en": "base_url for {provider}",
        "zh": "{provider} 的 base_url",
    },
    "models_add_base_url_bad": {
        "en": "base_url must start with http(s): {url}",
        "zh": "base_url 必须以 http(s) 开头: {url}",
    },
    "models_add_added": {
        "en": "provider {provider} added (credentials: use `oaset login` or an env:VAR reference later)",
        "zh": "已添加 provider {provider}（凭据稍后用 login 或 env:VAR 引用）",
    },
    "models_add_model_id": {
        "en": "model id (provider/model)",
        "zh": "模型 ID (provider/model)",
    },
    "models_add_model_empty": {
        "en": "model id cannot be empty",
        "zh": "模型 ID 不能为空",
    },
    "models_add_context": {
        "en": "context window tokens",
        "zh": "上下文窗口 tokens",
    },
    "models_add_output": {
        "en": "max output tokens",
        "zh": "最大输出 tokens",
    },
    "models_add_caps": {
        "en": "capability tags (comma separated)",
        "zh": "能力标签 (逗号分隔)",
    },
    "models_add_saved": {
        "en": "saved to config. Verify now: oaset models verify {model}",
        "zh": "已保存到配置。建议立即验证: oaset models verify {model}",
    },
    "models_add_discarded": {
        "en": "cancelled, nothing written to config.",
        "zh": "已取消，未写入配置。",
    },
    "models_verify_needs_id": {
        "en": "error: models verify needs MODEL_ID, e.g. oaset models verify provider/model",
        "zh": "error: models verify 需要 MODEL_ID，例如: oaset models verify provider/model",
    },
    "outline_header": {
        "en": "outline of {files} file(s) [{modes}]",
        "zh": "结构索引：{files} 个文件 [{modes}]",
    },
    "cmd_evidence": {
        "en": "Evidence gate: done claims must carry receipts",
        "zh": "证据门闩：完成声明必须附带收据",
    },
    "evidence_status_on": {
        "en": "evidence gate ON — done claims need receipts",
        "zh": "证据门闩已开启——完成声明必须附带收据",
    },
    "evidence_status_off": {
        "en": "evidence gate OFF",
        "zh": "证据门闩已关闭",
    },
    "evidence_count": {
        "en": "receipts in this session: {count}",
        "zh": "本会话已有收据：{count} 条",
    },
    "mcp_add_url_bad": {
        "en": "error: --url must start with http(s):// — got {url!r}",
        "zh": "error: --url 必须以 http(s):// 开头——当前为 {url!r}",
    },
    "mcp_add_searching": {
        "en": "searching the MCP registry for '{name}'…",
        "zh": "正在 MCP 注册表搜索 '{name}'…",
    },
    "mcp_add_not_found": {
        "en": "no MCP registry entry for '{name}' (use --url to add an http server directly)",
        "zh": "MCP 注册表没有 '{name}'（可用 --url 直接添加 http 服务）",
    },
    "mcp_add_ambiguous": {
        "en": "'{name}' matches several registry entries — be more specific:",
        "zh": "'{name}' 匹配到多个注册表条目——请用更精确的名字：",
    },
    "mcp_add_duplicate": {
        "en": ("error: an MCP server named '{name}' already exists in "
               "~/.oaset/mcp.json — remove it first or choose another name"),
        "zh": "error: ~/.oaset/mcp.json 中已存在名为 '{name}' 的 MCP 服务器——请先移除或换名",
    },
    "mcp_add_thirdparty_warn": {
        "en": ("WARNING: this entry runs third-party code; the package "
               "downloads on first connect: {package}"),
        "zh": ("警告：此条目运行第三方代码；首次连接时会下载包：{package}"),
    },
    "mcp_add_done": {
        "en": "added '{name}' to {path} (user-level: trusted). Restart oaset or /mcp reload.",
        "zh": "已将 '{name}' 写入 {path}（用户级：自动信任）。重启 oaset 或 /mcp reload 生效。",
    },
    "mcp_add_blocked": {
        "en": "error: network.mode='{mode}' blocks the registry lookup ({err})",
        "zh": "error: network.mode='{mode}' 禁止访问注册表（{err}）",
    },
    "mcp_add_name_required": {
        "en": "error: `oaset mcp add` requires a server name",
        "zh": "error: `oaset mcp add` 需要一个服务器名",
    },
    "offline_no_server": {
        "en": "none detected (Ollama :11434 / LM Studio :1234 are auto-discovered)",
        "zh": "未检测到（Ollama :11434 / LM Studio :1234 会被自动发现）",
    },
    "offline_context_headroom": {
        "en": "up to {tok} tok on-device",
        "zh": "本机最大上下文 {tok} tok",
    },
    "evidence_export_header": {
        "en": "# Evidence audit — session {sid}",
        "zh": "# 证据审计 — 会话 {sid}",
    },
    "panel_timeout_help": {
        "en": "seconds before an unanswered panel approval denies",
        "zh": "面板审批未应答的拒绝对话秒数",
    },
    "gateway_panel_help": {
        "en": ("panel mode: tool approvals become pending requests resolved "
               "via POST /panel/approve (timeout denies)"),
        "zh": ("面板模式：工具审批变为挂起请求，经 POST /panel/approve 解析"
               "（超时视为拒绝）"),
    },
    "evidence_flag_help": {
        "en": "replay/export the evidence audit chain of a session",
        "zh": "重放/导出一个会话的证据审计链",
    },
    "evidence_none": {
        "en": "no evidence recorded for session '{sid}' (enable /evidence on in the TUI)",
        "zh": "会话 '{sid}' 没有证据记录（在 TUI 里 /evidence on 开启）",
    },
    "evidence_replay_title": {
        "en": "evidence audit chain — session {sid} ({count} receipts)",
        "zh": "证据审计链 — 会话 {sid}（{count} 条收据）",
    },
    "evidence_exported": {
        "en": "exported: {path}",
        "zh": "已导出：{path}",
    },
    "offline_flag_help": {
        "en": "air-gap readiness: what works with the network gone",
        "zh": "断网就绪体检：网络消失后还有什么能用",
    },
    "offline_title": {
        "en": "offline-check — air-gap readiness",
        "zh": "offline-check — 断网就绪体检",
    },
    "offline_no_models": {
        "en": "none ready (run oaset setup, or start Ollama/LM Studio)",
        "zh": "暂无就绪模型（运行 oaset setup，或启动 Ollama/LM Studio）",
    },
    "offline_no_tools": {
        "en": "no network-free tools registered (unexpected)",
        "zh": "没有登记任何免网工具（异常）",
    },
    "offline_verdict": {
        "en": "OFFLINE-READY: {ready} — coding, editing, verification all work on-device",
        "zh": "断网就绪：{ready} —— 编码/修改/验证全流程可本机完成",
    },
    "context_cache": {
        "en": "cache: {read} tok read ({pct}% of prompt), {write} tok written",
        "zh": "缓存：命中 {read} tok（占提示词 {pct}%），新写 {write} tok",
    },
    "think_title": {
        "en": "reasoning depth",
        "zh": "思考深度",
    },
    "think_current": {
        "en": "current level: {level}",
        "zh": "当前档位：{level}",
    },
    "think_provider_default": {
        "en": "provider default (no parameter sent)",
        "zh": "模型默认（不发送参数）",
    },
    "think_knob": {
        "en": "this model's knob: {param}",
        "zh": "该模型的实际旋钮：{param}",
    },
    "think_no_knob": {
        "en": "none — this model picks thinking by its name, not by request",
        "zh": "无——该模型按型号名决定是否思考，请求参数改不了",
    },
    "think_hint": {
        "en": "/think <{levels}> — takes effect from the next model call",
        "zh": "/think <{levels}>——下一次模型调用生效",
    },
    "think_set": {
        "en": "thinking level set: {level}",
        "zh": "思考档位已设为 {level}",
    },
    "think_unknown": {
        "en": "unknown level; use one of: {levels}",
        "zh": "无法识别的档位；可选：{levels}",
    },
    "think_toggle_only": {
        "en": "This provider only supports on/off — light/medium/heavy all enable thinking.",
        "zh": "该服务只支持开/关——light/medium/heavy 都等于开。",
    },
    "think_model_only": {
        "en": "No request-level control for this provider; the level will not be sent.",
        "zh": "该服务没有请求级开关，档位不会随请求发送。",
    },
    "think_budget_note": {
        "en": "Budgets: light 2048 / medium 8192 / heavy 24576 tokens (off removes thinking).",
        "zh": "预算：light 2048 / medium 8192 / heavy 24576 token（off 关闭思考）。",
    },
    "cmd_plans": {
        "en": "List confirmed plans from .oaset/plans (idea-forge)",
        "zh": "列出 .oaset/plans 里的方案文档（需求锻造产出）",
    },
    "effect_plans": {
        "en": "read-only listing of plan documents; /view <path> to read one",
        "zh": "只读列出方案文档；/view <path> 查看内容",
    },
    "persist_failed": {
        "en": "session transcript write failed ({error}); lines may be missing from the saved session",
        "zh": "会话转录写入失败（{error}）；存档会话可能缺行",
    },
    "plans_title": {
        "en": "plans in this workspace (newest first)",
        "zh": "本工作区的方案文档（新→旧）",
    },
    "plans_empty": {
        "en": "no plans yet — say 先做方案 / idea-forge to forge one",
        "zh": "还没有方案——说「先做方案 / idea-forge」锻造一个",
    },
    "plans_row": {
        "en": "{name}  {when}  {title}",
        "zh": "{name}  {when}  {title}",
    },
    "context_cache_low": {
        "en": "low cache reuse ({pct}%): recent /compact or /model switch resets the prefix; idle gaps over 5 min expire entries (try prompt_cache_ttl = \"1h\" on anthropic)",
        "zh": "缓存复用率低（{pct}%）：刚 /compact 或 /model 换模型会重置前缀；回合间隔超 5 分钟会使条目过期（anthropic 可试 prompt_cache_ttl = \"1h\"）",
    },
    "context_cache_session": {
        "en": "session cache: {read} tok reused across {turns} turns ({pct}% of all prompt tokens)",
        "zh": "会话缓存：{turns} 个回合累计复用 {read} tok（占全部提示词 {pct}%）",
    },
    "api_format_azure": {
        "en": "Azure OpenAI (deployment URL)",
        "zh": "Azure OpenAI（部署式 URL）",
    },
    "models_local_detected": {
        "en": "local server detected: {vendor} @ {url} ({count} models) — /models add -> {vendor}",
        "zh": "检测到本地模型服务：{vendor} @ {url}（{count} 个模型）——/models add 选择 {vendor} 即可使用",
    },
    "models_verify_all_flag_help": {
        "en": "verify EVERY configured model (one real probe each)",
        "zh": "逐一验证所有已配置模型（每个一次真实调用）",
    },
    "verify_all_title": {
        "en": "verify-all: {count} model(s) configured",
        "zh": "verify-all：共配置 {count} 个模型",
    },
    "verify_all_skipped": {
        "en": "skip  (offline mock)",
        "zh": "跳过（离线 mock）",
    },
    "verify_all_summary": {
        "en": "summary: {ok} verified, {failed} failed, {skipped} skipped",
        "zh": "汇总：{ok} 个通过，{failed} 个失败，{skipped} 个跳过",
    },
    "verify_all_nothing": {
        "en": "nothing to verify (only the offline mock is configured)",
        "zh": "没有可验证的模型（只配置了离线 mock）",
    },
    "models_verify_flag_help": {
        "en": "after saving, probe the new model once (real API call)",
        "zh": "保存后立即探测一次新模型（真实 API 调用）",
    },
    "setup_confirm_write": {
        "en": "Write to config? [y/N]: ",
        "zh": "写入配置? [y/N]: ",
    },
    "cli_no_input_cancelled": {
        "en": "cancelled: no input available (stdin closed or piped dry).",
        "zh": "已取消：没有可用输入（stdin 已关闭或管道无数据）。",
    },
    "setup_provider_choice": {
        "en": "Provider [1-{count}]: ",
        "zh": "Provider [1-{count}]: ",
    },
    "setup_model_choice": {
        "en": "Default model [1-{count}]: ",
        "zh": "默认模型 [1-{count}]: ",
    },
    "setup_no_models": {
        "en": "no models configured for {provider}; add one in {path}",
        "zh": "{provider} 还没有配置模型；请在 {path} 中添加一个",
    },
    "setup_cancelled": {
        "en": "cancelled",
        "zh": "已取消",
    },
    "gateway_recv_only": {
        "en": ("receive-only: the default policy never sends replies from a transport. "
               "Add --send-replies or set [network] mode=\"full\" to enable sending."),
        "zh": ("只收不发：默认策略下传输仅接收、不发送回复。"
               "加 --send-replies 或设 [network] mode=\"full\" 启用发送。"),
    },
    "gateway_local_only": {
        "en": "error: network mode local_only forbids every transport",
        "zh": "error: local_only 网络策略禁止任何传输",
    },
    "net_blocked_push_pull_only": {
        "en": ("Receive-only: the default policy never sends content to third parties. "
               "Set [network] mode = \"full\" in config.toml, or pass --send-replies "
               "when starting the transport."),
        "zh": ("只收不发：默认策略禁止向第三方发送内容。"
               "在 config.toml 设 [network] mode = \"full\" 或启动传输时加 --send-replies。"),
    },
    "net_blocked_remote_exec_pull_only": {
        "en": ("Receive-only: remote-execution backends (ssh/docker/singularity/"
               "modal/daytona) are disabled by default. "
               "Set [network] mode = \"full\" to enable them."),
        "zh": ("只收不发：远程执行后端（ssh/docker/singularity/modal/daytona）默认停用。"
               "设 [network] mode = \"full\" 启用。"),
    },
    "net_blocked_model_call_local_only": {
        "en": ("local_only never reaches remote models. Use Ollama / LM Studio on "
               "127.0.0.1, or mock/mock-echo; for remote APIs set "
               "[network] mode = \"pull_only\"."),
        "zh": ("local_only 模式不访问远端模型。使用 127.0.0.1 上的 Ollama / "
               "LM Studio，或 mock/mock-echo；远程 API 请改 [network] mode = \"pull_only\"。"),
    },
    "net_blocked_generic": {
        "en": "network.mode='{mode}' blocks '{kind}' egress",
        "zh": "network.mode='{mode}' 禁止 '{kind}' 类外联",
    },
    "hook_blocked_by_policy": {
        "en": ("[hook] skipped {url}: network.mode='{mode}' is receive-only — "
               "hooks POST conversation content, which needs [network] mode=\"full\""),
        "zh": ("[hook] 已跳过 {url}: network.mode='{mode}' 为只收不发——"
               "钩子会上传会话内容，需要 [network] mode=\"full\""),
    },
    "upd_core_rollback_done": {
        "en": "rolled back the core upgrade {target} (previous version {version}). Restart oaset to load it.",
        "zh": "已回滚 core 升级 {target}（此前版本 {version}）。重启 oaset 以加载旧版本。",
    },
    "upd_rollback_done": {
        "en": "rolled back {target} (previous version {version})",
        "zh": "已回滚 {target}（此前版本 {version}）",
    },
    "sessions_none": {
        "en": "(no sessions yet)",
        "zh": "(还没有会话)",
    },
    "sessions_untitled": {
        "en": "(untitled)",
        "zh": "(未命名)",
    },
    "sessions_other_dir": {
        "en": "· dir: {dir}",
        "zh": "· 目录: {dir}",
    },
    "upd_install_invalid": {
        "en": "{name}: invalid install field: {exc}",
        "zh": "{name}: install 字段无效: {exc}",
    },
    "upd_dryrun_cfg": {
        "en": "{name} v{version}: [dry-run] {note} (nothing written to config).",
        "zh": "{name} v{version}: [dry-run] {note}（未写入配置）。",
    },
    "upd_cfg_write_failed": {
        "en": "{name}: config write failed; restored: {exc}",
        "zh": "{name}: 配置写入失败，已恢复: {exc}",
    },
    "upd_rollback_missing_name": {
        "en": "rollback record is missing a filename",
        "zh": "回滚记录缺少文件名",
    },
    "upd_partial_rollback": {
        "en": "some steps could not be restored; review the diagnostics above and retry.",
        "zh": "部分步骤未能恢复，请检查上面的诊断后再重试。",
    },
    "upd_rollback_done_a": {
        "en": "rolled back transaction {txn} ({count} steps, reverse order)",
        "zh": "已回滚事务 {txn}（{count} 步，逆序）",
    },
    "upd_rollback_done_targets": {
        "en": ", targets: {targets}",
        "zh": "，目标：{targets}",
    },
    "upd_rollback_done_tail": {
        "en": ". Restart oAset to load the old version.",
        "zh": "。重启 oaset 以加载旧版本。",
    },
    "upd_no_rollback_history": {
        "en": "no apply record to roll back",
        "zh": "没有可回滚的 apply 记录",
    },
    "upd_no_txn_record": {
        "en": "no apply record for transaction {txn}",
        "zh": "没有找到事务 {txn} 的 apply 记录",
    },
    "upd_no_target_record": {
        "en": "no apply record for {target}",
        "zh": "没有找到 {target} 的 apply 记录",
    },
    "upd_key_only_note": {
        "en": "by-key undo only; sibling targets in the same file are kept)",
        "zh": "仅按键撤销，同文件的其他目标保留）",
    },
    "upd_record_missing_dest": {
        "en": "apply record lacks dest; cannot roll back",
        "zh": "apply 记录缺少 dest，无法回滚",
    },
    "upd_core_backup_missing": {
        "en": "core backup directory missing; cannot roll back",
        "zh": "core 备份目录缺失，无法回滚",
    },
    "upd_core_rollback_failed": {
        "en": "core rollback failed: {exc}",
        "zh": "core 回滚失败: {exc}",
    },
    "upd_rollback_failed_generic": {
        "en": "rollback failed: {exc}",
        "zh": "回滚失败: {exc}",
    },
    "upd_history_empty": {
        "en": "(no update history yet)",
        "zh": "（暂无更新历史）",
    },
    "upd_plan_entry": {
        "en": "· {kind}/{name} → {version}  source {source}",
        "zh": "· {kind}/{name} → {version}  来源 {source}",
    },
    "upd_rollback_keys_head": {
        "en": "Rolled back {target} (previous version {version}, ",
        "zh": "已回滚 {target}（此前版本 {version}，",
    },
    "goal_active_line": {
        "en": "⌖ goal: {text}",
        "zh": "⌖ 目标: {text}",
    },
    "state_enabled": {
        "en": "enabled",
        "zh": "已启用",
    },
    "state_disabled": {
        "en": "disabled",
        "zh": "已停用",
    },
    "skills_header": {
        "en": "skills available",
        "zh": "可用 skills",
    },
    # TUI clipboard / input
    "clipboard_copied": {
        "en": "copied the input text to the clipboard",
        "zh": "已把输入内容复制到剪贴板",
    },
    "clipboard_empty": {
        "en": "clipboard has no text",
        "zh": "剪贴板里没有文本",
    },
    "clipboard_unavailable": {
        "en": "clipboard is unavailable in this environment",
        "zh": "当前环境不支持剪贴板",
    },
    "clipboard_failed": {
        "en": "clipboard error: {reason}",
        "zh": "剪贴板错误：{reason}",
    },
    "draft_cleared": {
        "en": "input cleared",
        "zh": "输入框已清空",
    },
    # TUI model/provider guided setup
    "models_menu_title": {
        "en": "Model & provider setup",
        "zh": "模型与 provider 设置",
    },
    "models_menu_switch": {
        "en": "Switch the active model",
        "zh": "切换当前模型",
    },
    "models_menu_add": {
        "en": "Add a provider / model (guided setup)",
        "zh": "新增 provider / 模型（向导）",
    },
    "models_menu_verify": {
        "en": "Verify a model's connectivity",
        "zh": "验证模型连通性",
    },
    "models_menu_list": {
        "en": "Show configured providers and models",
        "zh": "查看已配置的 provider 与模型",
    },
    "wizard_vendor_title": {
        "en": "Choose a provider (vendor)",
        "zh": "选择 provider（厂商）",
    },
    "wizard_vendor_custom": {
        "en": "Custom provider…",
        "zh": "自定义 provider…",
    },
    "wizard_vendor_configured": {
        "en": "configured",
        "zh": "已配置",
    },
    "wizard_provider_name": {
        "en": "Provider name (key used in config)",
        "zh": "Provider 名称（写入配置的键名）",
    },
    "wizard_provider_name_invalid": {
        "en": "provider name is required",
        "zh": "provider 名称不能为空",
    },
    "wizard_base_url_title": {
        "en": "Base URL for {provider}",
        "zh": "{provider} 的 Base URL",
    },
    "wizard_base_url_use_default": {
        "en": "Use default: {url}",
        "zh": "使用默认：{url}",
    },
    "wizard_base_url_custom": {
        "en": "Enter a custom Base URL…",
        "zh": "输入自定义 Base URL…",
    },
    "wizard_base_url_invalid": {
        "en": "Base URL must start with http(s):// — got {url}",
        "zh": "Base URL 必须以 http(s):// 开头——收到 {url}",
    },
    "wizard_api_format_title": {
        "en": "API format for {provider}",
        "zh": "{provider} 的 API 格式",
    },
    "api_format_openai": {
        "en": "OpenAI-compatible (/chat/completions)",
        "zh": "OpenAI 兼容（/chat/completions）",
    },
    "api_format_anthropic": {
        "en": "Anthropic Messages API",
        "zh": "Anthropic Messages API",
    },
    "api_format_gemini": {
        "en": "Google Generative Language API",
        "zh": "Google Generative Language API",
    },
    "api_format_bedrock": {
        "en": "AWS Bedrock Converse",
        "zh": "AWS Bedrock Converse",
    },
    "wizard_model_id": {
        "en": "Model ID (provider/model or bare name)",
        "zh": "模型 ID（provider/model 或裸名称）",
    },
    "wizard_model_id_invalid": {
        "en": "model ID is required",
        "zh": "模型 ID 不能为空",
    },
    "wizard_context": {
        "en": "Context window tokens",
        "zh": "上下文窗口 tokens",
    },
    "wizard_max_output": {
        "en": "Max output tokens",
        "zh": "最大输出 tokens",
    },
    "wizard_capabilities": {
        "en": "Capabilities (comma separated: tool_use, thinking, image_in)",
        "zh": "能力标签（逗号分隔：tool_use, thinking, image_in）",
    },
    "wizard_display_name": {
        "en": "Display name (optional, Enter to skip)",
        "zh": "显示名称（可选，回车跳过）",
    },
    "wizard_reasoning_key": {
        "en": "Reasoning key (optional, e.g. reasoning_content)",
        "zh": "推理字段名（可选，如 reasoning_content）",
    },
    "wizard_number_invalid": {
        "en": "{value!r} is not a positive integer",
        "zh": "{value!r} 不是正整数",
    },
    "wizard_api_key": {
        "en": "API key for {provider} (Enter to keep the stored key)",
        "zh": "{provider} 的 API Key（回车保留已存密钥）",
    },
    "wizard_api_key_saved": {
        "en": "API key stored in the credentials file (never in config.toml)",
        "zh": "API Key 已存入凭据文件（绝不写入 config.toml）",
    },
    "wizard_api_key_kept": {
        "en": "existing stored key kept",
        "zh": "保留原有已存密钥",
    },
    "wizard_preview_title": {
        "en": "Review before saving",
        "zh": "保存前确认",
    },
    "wizard_save": {
        "en": "Save configuration",
        "zh": "保存配置",
    },
    "wizard_save_verify": {
        "en": "Save, then verify connectivity",
        "zh": "保存并验证连通性",
    },
    "wizard_cancel": {
        "en": "Cancel",
        "zh": "取消",
    },
    "wizard_saved": {
        "en": "saved: provider {provider}, model {model}",
        "zh": "已保存：provider {provider}，模型 {model}",
    },
    "wizard_cancelled": {
        "en": "setup cancelled — nothing was saved",
        "zh": "已取消设置——未写入任何配置",
    },
    "wizard_verify_ok": {
        "en": "{model} is reachable and ready",
        "zh": "{model} 连通正常，可用",
    },
    "wizard_verify_failed": {
        "en": "{model} verification failed: {error}",
        "zh": "{model} 验证失败：{error}",
    },
    "wizard_verify_hint": {
        "en": "check the API key, Base URL and API format, then re-run /model",
        "zh": "请检查 API Key、Base URL 与 API 格式，然后重新运行 /model",
    },
    "model_switch_verifying": {
        "en": "verifying {model} before switching…",
        "zh": "切换前正在验证 {model}…",
    },
    "model_switch_failed": {
        "en": "{model} failed verification — keeping {current}",
        "zh": "{model} 验证失败——继续使用 {current}",
    },
    "model_switch_ok": {
        "en": "switched to {model}",
        "zh": "已切换到 {model}",
    },
    "model_switch_ok_persisted": {
        "en": "switched to {model} (saved as the default)",
        "zh": "已切换到 {model}（已保存为默认）",
    },
    "model_switch_ok_session_only": {
        "en": "switched to {model} for this session - the default could NOT be saved",
        "zh": "已切换到 {model}（仅本会话；默认值写入失败）",
    },
    "config_write_failed_hint": {
        "en": "Check that ~/.oaset/config.toml is writable; the change is live now but will not survive a restart.",
        "zh": "请检查 ~/.oaset/config.toml 是否可写；本次改动已生效，但重启后会丢失。",
    },
    "models_key_stored": {
        "en": "key: stored",
        "zh": "密钥：已存",
    },
    "models_key_missing": {
        "en": "key: MISSING (run /model → add, or /login)",
        "zh": "密钥：缺失（运行 /model → 新增，或 /login）",
    },
    "models_mock_builtin": {
        "en": "(built-in offline mock · no network, no base URL)",
        "zh": "（内置离线 mock · 无需网络、无 Base URL）",
    },
    "probe_mock_ok": {
        "en": "built-in offline mock (no network needed)",
        "zh": "内置离线 mock（无需网络）",
    },
    # TUI MCP + skills management
    "mcp_menu_title": {
        "en": "MCP servers",
        "zh": "MCP 服务器",
    },
    "mcp_untrusted": {
        "en": "untrusted project server(s), not started: {names}",
        "zh": "未信任的项目级服务器未启动：{names}",
    },
    # community plugin market
    "mkt_title": {
        "en": "Community plugin market",
        "zh": "社区插件市场",
    },
    "mkt_subtitle": {
        "en": "Discover plugins from your selected source",
        "zh": "从你选择的来源发现 oAset 插件",
    },
    "mkt_tab_discover": {"en": "Discover", "zh": "发现"},
    "mkt_tab_installable": {"en": "Installable", "zh": "可安装"},
    "mkt_tab_installed": {"en": "Installed", "zh": "已安装"},
    "mkt_tab_sources": {"en": "Sources", "zh": "来源"},
    "mkt_active_source": {"en": "Active source", "zh": "活动来源"},
    "mkt_pick_tab": {"en": "Choose a tab", "zh": "选择一个标签"},
    "mkt_fetching": {"en": "Fetching market source…", "zh": "正在获取市场来源…"},
    "mkt_empty_discover": {
        "en": "No plugins in this source yet",
        "zh": "该来源暂无插件",
    },
    "mkt_empty_installable": {
        "en": "Everything in this source is already installed",
        "zh": "该来源的插件已全部安装",
    },
    "mkt_nothing_installed": {
        "en": "No plugins installed yet (browse: /market)",
        "zh": "尚未安装插件（浏览：/market）",
    },
    "mkt_pick_install": {"en": "Install a plugin", "zh": "安装插件"},
    "mkt_pick_installable": {"en": "Install (from installable)", "zh": "安装（可安装列表）"},
    "mkt_source_name": {"en": "Source name", "zh": "来源名称"},
    "tool_group_calls": {
        "en": "{n} tool calls",
        "zh": "{n} 次工具调用",
    },
    "tool_used": {
        "en": "Used {name}",
        "zh": "Used {name}",
    },
    "tool_denied_msg": {
        "en": "denied by user",
        "zh": "用户拒绝了这次调用",
    },
    "tool_extra_lines": {
        "en": " (+{n} lines)",
        "zh": " (+{n} 行)",
    },
    "event_monitor_dead": {
        "en": "Console input monitor stopped: keyboard/mouse may no longer respond. Restart oaset.",
        "zh": "控制台输入监控已停止：键盘/鼠标可能不再响应。请重启 oaset。",
    },
    "event_monitor_dead_hint": {
        "en": "Usually caused by a console host change (terminal resize, quick-edit, remote session).",
        "zh": "通常由终端宿主变化引起（窗口尺寸变化/QuickEdit/远程会话）。",
    },
    "elicit_timeout": {
        "en": "MCP input request timed out after {seconds}s — reported as cancelled",
        "zh": "MCP 输入请求 {seconds}s 未应答——已按取消上报",
    },
    "io_blocked_hint": {
        "en": "A file write is stuck (antivirus lock / sync client / network drive?). "
              "Close the locking program and retry.",
        "zh": "一次文件写入被阻塞（杀毒/同步盘/网络盘占用？）。解除占用后重试。",
    },
    "cmd_market": {
        "en": "Community plugin market: discover, install, manage sources",
        "zh": "社区插件市场：发现/安装插件、管理来源",
    },
    "mkt_confirm_install": {
        "en": "Install {name} v{version} from {source}?",
        "zh": "从 {source} 安装 {name} v{version}？",
    },
    "mkt_installed_done": {
        "en": "Installed {name}; run /reload-plugins to load it",
        "zh": "已安装 {name}；运行 /reload-plugins 加载",
    },
    "mkt_pick_manage": {"en": "Manage an installed plugin", "zh": "管理已安装插件"},
    "mkt_confirm_uninstall": {"en": "Uninstall {name}?", "zh": "卸载 {name}？"},
    "mkt_uninstalled": {
        "en": "Uninstalled {name}; run /reload-plugins to apply",
        "zh": "已卸载 {name}；运行 /reload-plugins 生效",
    },
    "mkt_pick_source_action": {"en": "Sources (one active at a time)", "zh": "来源（每次只用一个）"},
    "mkt_source_use": {"en": "Set as active", "zh": "设为活动来源"},
    "mkt_source_add": {"en": "Add a source (https URL)", "zh": "新增来源（https 地址）"},
    "mkt_source_remove": {"en": "Remove a source", "zh": "移除来源"},
    "mkt_source_added": {"en": "Source added: {name}", "zh": "来源已添加：{name}"},
    "mkt_source_removed": {"en": "Source removed: {name}", "zh": "来源已移除：{name}"},
    "mkt_source_now_active": {"en": "Active source: {name}", "zh": "活动来源：{name}"},
    "mkt_bad_name": {
        "en": "Invalid source name: {name} (must be non-empty, not 'official', no '::')",
        "zh": "来源名称无效：{name}（不能为空、不能叫 official、不能含 ::）",
    },
    "mkt_duplicate": {"en": "Source already exists: {name}", "zh": "来源已存在：{name}"},
    "mkt_unknown": {"en": "Unknown source: {name}", "zh": "未知来源：{name}"},
    "mkt_not_in_source": {
        "en": "Plugin {name} not found in source {source}",
        "zh": "来源 {source} 中没有插件 {name}",
    },
    "mkt_not_installed": {"en": "Plugin not installed: {name}", "zh": "插件未安装：{name}"},
    "mkt_install_failed": {"en": "Install failed", "zh": "安装失败"},
    "mcp_menu_list": {
        "en": "List servers (status, transport, tools)",
        "zh": "列出服务器（状态/传输/工具）",
    },
    "mcp_menu_add": {
        "en": "Add a server (stdio or HTTP)",
        "zh": "新增服务器（stdio 或 HTTP）",
    },
    "mcp_menu_toggle": {
        "en": "Enable / disable a server",
        "zh": "启用 / 停用服务器",
    },
    "mcp_menu_remove": {
        "en": "Remove a server",
        "zh": "删除服务器",
    },
    "mcp_menu_reload": {
        "en": "Restart servers and re-register tools",
        "zh": "重启服务器并重新注册工具",
    },
    "mcp_menu_roots": {
        "en": "Extra workspace roots (roots/list)",
        "zh": "额外工作区根目录（roots/list）",
    },
    "mcp_scope_title": {
        "en": "Where should this change be written?",
        "zh": "这次改动写到哪个配置？",
    },
    "mcp_scope_project": {
        "en": "Project (.oaset/mcp.json) — this workspace only",
        "zh": "项目级（.oaset/mcp.json）——仅本工作区",
    },
    "mcp_scope_user": {
        "en": "User (~/.oaset/mcp.json) — all workspaces",
        "zh": "用户级（~/.oaset/mcp.json）——所有工作区",
    },
    "mcp_add_name": {
        "en": "Server name",
        "zh": "服务器名称",
    },
    "mcp_add_transport": {
        "en": "Transport",
        "zh": "传输方式",
    },
    "mcp_transport_stdio": {
        "en": "stdio — run a local command",
        "zh": "stdio——运行本地命令",
    },
    "mcp_transport_http": {
        "en": "HTTP — connect to a URL",
        "zh": "HTTP——连接远端 URL",
    },
    "mcp_add_command": {
        "en": "Command (e.g. npx, python, uvx)",
        "zh": "命令（如 npx、python、uvx）",
    },
    "mcp_add_args": {
        "en": "Arguments (space separated, optional)",
        "zh": "参数（空格分隔，可选）",
    },
    "mcp_add_url": {
        "en": "Server URL (http(s)://…)",
        "zh": "服务器 URL（http(s)://…）",
    },
    "mcp_add_tools": {
        "en": "Tool filter (optional: +only_a,+only_b or -skip_a)",
        "zh": "工具过滤（可选：+仅保留 或 -排除）",
    },
    "mcp_saved": {
        "en": "saved {name} in the {scope} config",
        "zh": "已在 {scope} 配置中保存 {name}",
    },
    "mcp_removed": {
        "en": "removed {name} from the {scope} config",
        "zh": "已从 {scope} 配置删除 {name}",
    },
    "mcp_enabled_msg": {
        "en": "{name} enabled",
        "zh": "{name} 已启用",
    },
    "mcp_disabled_msg": {
        "en": "{name} disabled",
        "zh": "{name} 已停用",
    },
    "mcp_empty": {
        "en": "no MCP servers configured — use /mcp to add one",
        "zh": "尚未配置 MCP 服务器——用 /mcp 新增",
    },
    "mcp_confirm_reload": {
        "en": "Apply now? Restart servers and re-register tools",
        "zh": "现在应用？重启服务器并重新注册工具",
    },
    "mcp_apply_yes": {
        "en": "Restart now",
        "zh": "立即重启",
    },
    "mcp_apply_later": {
        "en": "Later (run /reload-mcp)",
        "zh": "稍后（运行 /reload-mcp）",
    },
    "mcp_unknown_server": {
        "en": "no such server: {name}",
        "zh": "没有这个服务器：{name}",
    },
    "mcp_added_hint": {
        "en": "saved — restart to load it: /reload-mcp",
        "zh": "已保存——重启后加载：/reload-mcp",
    },
    "skills_menu_title": {
        "en": "Skills",
        "zh": "技能（skills）",
    },
    "skills_menu_new": {
        "en": "Create a new skill (writes SKILL.md)",
        "zh": "新建技能（写入 SKILL.md）",
    },
    "skills_menu_reload": {
        "en": "Reload the skill list and system prompt",
        "zh": "重新加载技能列表与系统提示",
    },
    "skills_new_name": {
        "en": "Skill name (used as the directory name)",
        "zh": "技能名称（用作目录名）",
    },
    "skills_new_description": {
        "en": "One-line description (optional)",
        "zh": "一句话描述（可选）",
    },
    "skills_create_summary": {
        "en": "Create skill {name} in the {scope} scope",
        "zh": "在 {scope} 作用域创建技能 {name}",
    },
    "skills_delete_summary": {
        "en": "Delete skill {name} (removes its files)",
        "zh": "删除技能 {name}（移除其文件）",
    },
    "skills_denied": {
        "en": "skill change denied — nothing was written",
        "zh": "技能变更被拒绝——未写入任何内容",
    },
    "skills_created": {
        "en": "created skill {name} at {path}",
        "zh": "已创建技能 {name}：{path}",
    },
    "skills_deleted": {
        "en": "deleted skill {name} ({path})",
        "zh": "已删除技能 {name}（{path}）",
    },
    # TUI output formatting
    "meta_tokens": {
        "en": "{total} tok",
        "zh": "{total} tok",
    },
    "meta_tokens_io": {
        "en": "↑{prompt} ↓{completion} tok",
        "zh": "↑{prompt} ↓{completion} tok",
    },
    "copy_what": {
        "en": "What to copy?",
        "zh": "复制什么？",
    },
    "copy_input": {
        "en": "Current input draft",
        "zh": "当前输入草稿",
    },
    "copy_last": {
        "en": "Last assistant reply",
        "zh": "最后一条助手回复",
    },
    "copy_selection": {
        "en": "Selection in the conversation (drag to select)",
        "zh": "对话中选中的内容（拖动选择）",
    },
    "copy_tools": {
        "en": "All tool outputs",
        "zh": "全部工具输出",
    },
    "copy_all": {
        "en": "The whole conversation",
        "zh": "整段对话",
    },
    "copy_no_selection": {
        "en": "nothing selected - drag over the conversation to select blocks",
        "zh": "尚未选中内容——在对话上拖动即可选中区块",
    },
    "copy_unknown_target": {
        "en": "unknown copy target '{target}' - try: selection / last / tools / all / input",
        "zh": "未知的复制目标 '{target}'——可用：selection / last / tools / all / input",
    },
    "copy_nothing": {
        "en": "nothing to copy yet",
        "zh": "暂无可复制内容",
    },
    "copied_chars": {
        "en": "copied {n} characters",
        "zh": "已复制 {n} 个字符",
    },
    # TUI sub-agent management
    "agents_menu_title": {
        "en": "Sub-agents",
        "zh": "子代理",
    },
    "agents_menu_new": {
        "en": "Create a sub-agent (writes .md)",
        "zh": "新建子代理（写入 .md）",
    },
    "agents_menu_show": {
        "en": "Show a sub-agent's definition",
        "zh": "查看子代理定义",
    },
    "agents_menu_delete": {
        "en": "Delete a sub-agent",
        "zh": "删除子代理",
    },
    "agents_menu_reload": {
        "en": "Reload sub-agent definitions",
        "zh": "重新加载子代理定义",
    },
    "agents_new_name": {
        "en": "Sub-agent name",
        "zh": "子代理名称",
    },
    "agents_new_description": {
        "en": "One-line description",
        "zh": "一句话描述",
    },
    "agents_new_model": {
        "en": "Model id (optional, empty = inherit parent)",
        "zh": "模型 ID（可选，留空继承主模型）",
    },
    "agents_create_summary": {
        "en": "Create sub-agent {name} in the {scope} scope",
        "zh": "在 {scope} 作用域创建子代理 {name}",
    },
    "agents_delete_summary": {
        "en": "Delete sub-agent {name}",
        "zh": "删除子代理 {name}",
    },
    "agents_created": {
        "en": "created sub-agent {name} at {path}",
        "zh": "已创建子代理 {name}：{path}",
    },
    "agents_deleted": {
        "en": "deleted sub-agent {name} ({path})",
        "zh": "已删除子代理 {name}（{path}）",
    },
    "agents_reloaded": {
        "en": "sub-agent definitions reloaded",
        "zh": "子代理定义已重新加载",
    },
    "agents_denied": {
        "en": "sub-agent change denied — nothing was written",
        "zh": "子代理变更被拒绝——未写入任何内容",
    },
    "agents_usage": {
        "en": "usage by sub-agent this session:",
        "zh": "本次会话子代理用量：",
    },
    "agents_unknown": {
        "en": "unknown sub-agent: {name}",
        "zh": "未知子代理：{name}",
    },
    # TUI help hierarchy
    "cat_interface": {
        "en": "Interface",
        "zh": "界面",
    },
    "cat_session": {
        "en": "Sessions",
        "zh": "会话",
    },
    "cat_history": {
        "en": "History",
        "zh": "历史",
    },
    "cat_input": {
        "en": "Input",
        "zh": "输入",
    },
    "cat_config": {
        "en": "Configuration",
        "zh": "配置",
    },
    "cat_system": {
        "en": "System & updates",
        "zh": "系统与更新",
    },
    "cat_computer": {
        "en": "Computer use",
        "zh": "计算机使用",
    },
    "cat_plugin": {
        "en": "Plugins",
        "zh": "插件",
    },
    "cat_model": {
        "en": "Models & usage",
        "zh": "模型与用量",
    },
    "cat_tools": {
        "en": "Tools",
        "zh": "工具",
    },
    "cat_mcp": {
        "en": "MCP",
        "zh": "MCP",
    },
    "cat_agents": {
        "en": "Agents & skills",
        "zh": "子代理与技能",
    },
    "cat_general": {
        "en": "General",
        "zh": "通用",
    },
    "help_shortcuts_title": {
        "en": "Keyboard shortcuts",
        "zh": "快捷键",
    },
    "help_commands_title": {
        "en": "Commands by category",
        "zh": "按类别分组的命令",
    },
    "help_risk_legend": {
        "en": "✎ writes · ⚠ destructive",
        "zh": "✎ 写入 · ⚠ 危险",
    },
    "shortcut_palette": {
        "en": "command palette",
        "zh": "命令面板",
    },
    "shortcut_fold": {
        "en": "fold tool output / thinking",
        "zh": "折叠工具输出 / 思考",
    },
    "shortcut_editor": {
        "en": "edit the draft in $EDITOR",
        "zh": "在 $EDITOR 中编辑草稿",
    },
    "shortcut_sidebar": {
        "en": "toggle the plan strip above the input",
        "zh": "切换输入框上方的计划条",
    },
    "shortcut_plan": {
        "en": "toggle plan (read-only) mode",
        "zh": "切换计划（只读）模式",
    },
    "shortcut_steer": {
        "en": "inject an instruction mid-turn",
        "zh": "回合中注入指令",
    },
    "shortcut_interrupt": {
        "en": "interrupt the running turn",
        "zh": "中断正在运行的回合",
    },
    "shortcut_clipboard": {
        "en": "copy (chat selection first, else the draft) / paste",
        "zh": "复制（先选区，否则草稿）/ 粘贴",
    },
    "shortcut_clear": {
        "en": "clear the input draft",
        "zh": "清空输入草稿",
    },
    "shortcut_exit": {
        "en": "exit oAset (with a confirm hint)",
        "zh": "退出 oAset（带确认提示）",
    },
    "shortcut_send": {
        "en": "send (queued automatically while the agent runs; Ctrl+J = newline)",
        "zh": "发送（agent 运行中自动排队；Ctrl+J 换行）",
    },
    "shortcut_cut": {
        "en": "cut in the input box (Windows convention)",
        "zh": "输入框内剪切（Windows 惯例）",
    },
    "shortcut_history": {
        "en": "history & suggestion navigation",
        "zh": "历史与建议导航",
    },
    "shortcut_rightclick": {
        "en": "chat: copy selection / paste (cmd.exe style); input: paste",
        "zh": "聊天区：复制选区/粘贴（cmd.exe 惯例）；输入框：粘贴",
    },
    "shortcut_toggle_block": {
        "en": "toggle the focused block/card (also click)",
        "zh": "展开/折叠当前聚焦的块或工具卡（也可点击）",
    },
    # P1-6 message queue
    "queued_at": {
        "en": "Queued #{n} — sends automatically when the current turn ends.",
        "zh": "已排队第 {n} 条——当前回合结束后自动发送。",
    },
    "queue_sending": {
        "en": "Queue: sending the next message ({n} still queued).",
        "zh": "队列：发送下一条（还剩 {n} 条）。",
    },
    "queue_kept": {
        "en": "Interrupted: {n} queued message(s) kept — the next Enter sends the first.",
        "zh": "已中断：保留 {n} 条排队消息——下次发送时先发第一条。",
    },
    "queue_dropped": {
        "en": "Dropped {n} queued message(s) — they belonged to the previous session.",
        "zh": "已丢弃 {n} 条排队消息——它们属于上一个会话。",
    },
    "session_switch_busy": {
        "en": "The running turn did not stop in time; session switch aborted. Try again.",
        "zh": "运行中的回合未能及时停止，已取消切换会话。请重试。",
    },
    # density / liveliness (2026-09-14 screenshot pass)
    "stalled_hint": {
        "en": "no output {n}s",
        "zh": "无输出 {n}s",
    },
    "fold_chip": {
        "en": "⋯ {n} tool calls folded (click to expand)",
        "zh": "⋯ 已折叠 {n} 个工具调用（点击展开）",
    },
    "notice_clamped": {
        "en": "… +{n} more lines (copy keeps the full text)",
        "zh": "…（还有 {n} 行，复制可得全文）",
    },
    "boot_pending": {
        "en": "Warming up the model connection — your message will send automatically.",
        "zh": "模型连接初始化中——就绪后自动发送。",
    },
    "boot_pending_cmd": {
        "en": "Still warming up; try the command again in a moment.",
        "zh": "正在初始化，稍等片刻再执行命令。",
    },
    # plain REPL (accessibility surface)
    "repl_banner": {
        "en": "oAset plain mode · {model} · {cwd}",
        "zh": "oAset 行式模式 · {model} · {cwd}",
    },
    "repl_hint": {
        "en": "Plain append-only output (no ANSI/animation, screen-reader friendly). /exit quits; slash commands live in the TUI.",
        "zh": "逐行纯文本输出（无 ANSI/动画，读屏友好）。/exit 退出；斜杠命令在 TUI 模式。",
    },
    "repl_permission": {
        "en": "Permission required [{level}] {tool}: {summary}",
        "zh": "需要确认 [{level}] {tool}: {summary}",
    },
    "repl_permission_answer": {
        "en": "allow? [y=once a=this session n=deny]",
        "zh": "允许? [y=一次 a=本会话 n=拒绝]",
    },
    "repl_unknown_cmd": {
        "en": "{cmd} is not available in plain mode (full commands live in the TUI; /exit quits).",
        "zh": "行式模式不支持 {cmd}（完整命令在 TUI 中；/exit 退出）。",
    },
    "repl_tool": {
        "en": "[tool] {name} {summary}",
        "zh": "[工具] {name} {summary}",
    },
    "repl_tool_done": {
        "en": "{mark} {name}",
        "zh": "{mark} {name}",
    },
    "repl_tool_denied": {
        "en": "denied",
        "zh": "已拒绝",
    },
    "repl_notice": {
        "en": "[notice] {text}",
        "zh": "[提示] {text}",
    },
    "repl_error": {
        "en": "[error] {error}",
        "zh": "[错误] {error}",
    },
    "repl_interrupted": {
        "en": "[interrupted]",
        "zh": "[已中断]",
    },
    "repl_bye": {
        "en": "bye.",
        "zh": "再见。",
    },
    "repl_turn_done": {
        "en": "— {tokens} tokens",
        "zh": "— {tokens} tokens",
    },
    # cost transparency
    "usage_cost": {
        "en": "Session cost ≈{cost} (estimated at the CURRENT model's prices)",
        "zh": "本会话累计 ≈{cost}（按当前模型单价估算）",
    },
    # turn-end notifications
    "notify_turn_done": {
        "en": "oAset: turn finished ({model})",
        "zh": "oAset：回合完成（{model}）",
    },
    "notify_turn_failed": {
        "en": "oAset: turn FAILED ({model})",
        "zh": "oAset：回合失败（{model}）",
    },
    # /context
    "context_title": {
        "en": "Context window — what is using it",
        "zh": "上下文窗口——谁在占用",
    },
    "context_prompt": {
        "en": "system prompt (incl. skills/memory/project blocks)",
        "zh": "系统提示（含技能/记忆/项目上下文块）",
    },
    "context_tools": {
        "en": "tool schemas ({n} tools)",
        "zh": "工具定义（{n} 个工具）",
    },
    "context_messages": {
        "en": "messages ({user} user · {assistant} assistant · {tool} tool)",
        "zh": "对话消息（用户 {user} · 回答 {assistant} · 工具 {tool}）",
    },
    "context_total": {
        "en": "total ≈ {tokens} / {max} tokens ({pct:.0f}%)",
        "zh": "合计 ≈ {tokens} / {max} tokens（{pct:.0f}%）",
    },
    "context_compact_hint": {
        "en": "past 75%: consider /compact or /rewind",
        "zh": "已过 75%：可考虑 /compact 或 /rewind",
    },
    "dequeue_cleared": {
        "en": "Cleared {n} queued message(s).",
        "zh": "已清除 {n} 条排队消息。",
    },
    "dequeue_empty": {
        "en": "No queued messages.",
        "zh": "当前没有排队消息。",
    },
    # P1-8 clipboard resilience
    "copied_osc52": {
        "en": "sent via OSC52 (terminal clipboard — support varies; verify with a paste)",
        "zh": "已通过 OSC52 发送（终端剪贴板，支持度因终端而异，可粘贴验证）",
    },
    # P1-2 input position indicator
    "input_pos": {
        "en": "ln {line}/{total}",
        "zh": "行 {line}/{total}",
    },
    # P1-5 welcome panel fields
    "welcome_dir": {"en": "Directory", "zh": "目录"},
    "welcome_session": {"en": "Session", "zh": "会话"},
    "welcome_model": {"en": "Model", "zh": "模型"},
    "welcome_provider": {"en": "Provider", "zh": "Provider"},
    # P1-5 permission badges
    "badge_write": {"en": "WRITE", "zh": "写入"},
    "badge_exec": {"en": "EXECUTE", "zh": "执行"},
    "badge_read": {"en": "READ", "zh": "读取"},
    # P1-5 agent-loop notices (UI-directed only; wire content stays English)
    "steer_injected": {
        "en": "Message injected into the running task.",
        "zh": "已注入正在运行的任务。",
    },
    "retry_preserved": {
        "en": "Holding {n} characters from the interrupted attempt; if the retry completes, its full answer replaces them.",
        "zh": "已保住中断前生成的 {n} 字符；若重试成功，以重试的完整回答为准。",
    },
    "provider_retry": {
        "en": "{category} error, retrying in {seconds}s ({attempt}/{total})…",
        "zh": "{category} 错误，{seconds} 秒后重试（{attempt}/{total}）…",
    },
    "auto_compact_failed": {
        "en": "Auto-compaction failed: {err}",
        "zh": "自动压缩失败：{err}",
    },
    # P2 output system
    "archive_hint": {
        "en": "↑ {n} earlier block(s) archived — scroll to the top to load them",
        "zh": "↑ 上方 {n} 条较早内容已归档——滚动到顶部自动加载",
    },
    "view_hint": {
        "en": "Terminal selection is active here: drag to select, then copy.",
        "zh": "此视图可用终端原生选择：拖选后复制。",
    },
    "view_empty": {
        "en": "Nothing to view yet ({which}).",
        "zh": "暂无可查看的内容（{which}）。",
    },
    "view_kind_user": {"en": "user message", "zh": "用户消息"},
    "view_kind_answer": {"en": "assistant answer", "zh": "助手回答"},
    "view_kind_tool": {"en": "tool output", "zh": "工具输出"},
    "shortcut_view": {
        "en": "view the last message full-screen (native character selection)",
        "zh": "全屏查看上一条消息（终端原生字符级选择）",
    },
    "cmd_view": {
        "en": "Open the last message full-screen to select & copy any part",
        "zh": "全屏打开上一条消息，可任意选择复制",
    },
    "cmd_login": {"en": "Store an API key for a provider locally", "zh": "本地保存 provider 的 API 密钥"},
    "login_pick_provider": {"en": "Which provider?", "zh": "选择要登录的 provider"},
    "login_key_title": {"en": "API key for {provider}", "zh": "{provider} 的 API 密钥"},
    "login_key_placeholder": {"en": "paste the key (input hidden)", "zh": "粘贴密钥（输入隐藏）"},
    "login_stored": {
        "en": "Stored the API key for {provider} in ~/.oaset/credentials/ (0600).",
        "zh": "已保存 {provider} 的密钥到 ~/.oaset/credentials/（0600）。",
    },
    "login_next_step": {
        "en": "Next: /model to pick a {provider} model, then type a question.",
        "zh": "下一步：用 /model 选择一个 {provider} 模型，然后直接提问。",
    },
    "login_ready": {
        "en": "This session can send now — type a question and press Enter.",
        "zh": "当前会话已可用——直接输入问题并回车。",
    },
    "login_unknown_provider": {
        "en": "Unknown provider '{provider}'. Known: {known}",
        "zh": "未知 provider：{provider}。可选：{known}",
    },
    "login_failed_hint": {
        "en": "Check that ~/.oaset/credentials/ is writable.",
        "zh": "请检查 ~/.oaset/credentials/ 是否可写。",
    },
    # P1-5 command descriptions (/help & palette) — cmd_<name>, '-'/' ' -> '_'
    "cmd_help": {"en": "Show shortcuts and daily commands", "zh": "显示快捷键与常用命令"},
    "cmd_palette": {"en": "Search commands", "zh": "搜索命令"},
    "cmd_dequeue": {"en": "Drop every queued message", "zh": "清除全部排队消息"},
    "cmd_update": {"en": "Update center (check/list/plan/refresh)", "zh": "更新中心（检查/列表/计划/刷新）"},
    "cmd_new": {"en": "Start a fresh session and clear context", "zh": "新建会话并清空上下文"},
    "cmd_sessions": {"en": "Browse and restore past sessions", "zh": "浏览并恢复历史会话"},
    "cmd_session_delete": {"en": "Delete a session by ID suffix", "zh": "按 ID 后缀删除会话"},
    "cmd_fork": {"en": "Fork the current session with full history", "zh": "复制当前会话为新会话"},
    "cmd_compact": {"en": "Compress older history into a summary", "zh": "把较早历史压缩为摘要"},
    "cmd_goal": {"en": "Set a persistent goal", "zh": "设定持续目标"},
    "cmd_history": {"en": "Show a compact history of this conversation", "zh": "查看本会话的简要历史"},
    "cmd_undo": {"en": "Undo the last file change via checkpoint", "zh": "用检查点回滚最近一次文件修改"},
    "cmd_rewind": {"en": "Drop the last N turns", "zh": "删除最近 N 轮对话"},
    "cmd_export": {"en": "Export current session to Markdown", "zh": "把当前会话导出为 Markdown"},
    "cmd_copy": {"en": "Copy a chat selection, tool output or the draft", "zh": "复制聊天选区、工具输出或草稿"},
    "cmd_paste": {"en": "Paste clipboard text into the input", "zh": "把剪贴板文本粘贴到输入框"},
    "cmd_image": {"en": "Attach an image to the next message", "zh": "为下一条消息附加图片"},
    "cmd_worktree": {"en": "Git worktree: new / list / remove", "zh": "Git worktree：新建/列表/删除"},
    "worktree_usage": {
        "en": "/worktree new <name> · /worktree list · /worktree remove <name>",
        "zh": "/worktree new <名称> · /worktree list · /worktree remove <名称>",
    },
    "worktree_created": {"en": "worktree created: {path}", "zh": "已创建 worktree：{path}"},
    "worktree_removed": {"en": "worktree removed: {path}", "zh": "已删除 worktree：{path}"},
    "worktree_empty": {"en": "No git worktrees (not a repo, or none added).", "zh": "没有 git worktree（不是仓库，或尚未创建）。"},
    "worktree_header": {"en": "Git worktrees", "zh": "Git worktrees"},
    "cmd_steer": {"en": "Inject an instruction into the running turn", "zh": "向运行中的回合注入指令"},
    "cmd_retry": {"en": "Resend the previous prompt", "zh": "重发上一条提示词"},
    "cmd_think": {
        "en": "Reasoning depth: off / light / medium / heavy (per-provider mapping)",
        "zh": "思考档位：off / light / medium / heavy（按各家协议映射）",
    },
    "cmd_model": {"en": "Switch, add or inspect models and providers", "zh": "切换、新增或查看模型与 provider"},
    "cmd_models": {"en": "Model & provider management", "zh": "模型与 provider 管理"},
    "cmd_usage": {"en": "Token usage for this session", "zh": "本会话的 token 用量"},
    "cmd_context": {"en": "What is using the context window", "zh": "上下文窗口占用明细"},
    "cmd_insights": {"en": "Token usage by day", "zh": "按天查看 token 用量"},
    "cmd_tools": {"en": "List tools", "zh": "列出工具"},
    "cmd_tools_toggle": {"en": "Enable/disable a tool for this session", "zh": "为本会话启停工具"},
    "cmd_mcp": {"en": "MCP servers: status / add / remove / reload", "zh": "MCP 服务器：状态/新增/移除/重载"},
    "cmd_roots": {"en": "MCP workspace roots: add / remove / clear", "zh": "MCP 工作区根目录：添加/移除/清空"},
    "cmd_reload_mcp": {"en": "Restart MCP servers", "zh": "重启 MCP 服务器并重新注册工具"},
    "cmd_agents": {"en": "Sub-agents: list / show / new / delete / reload", "zh": "子代理：列表/查看/新建/删除/重载"},
    "cmd_skills": {"en": "Skills: list / load / show / new / delete", "zh": "技能：列表/加载/查看/新建/删除"},
    "cmd_reload_skills": {"en": "Rebuild the skill list and system prompt", "zh": "重建技能列表与系统提示"},
    "cmd_init": {"en": "Analyze the codebase and generate AGENTS.md", "zh": "分析代码库并生成 AGENTS.md"},
    "cmd_memory": {"en": "Show persisted agent memory", "zh": "查看已持久化的代理记忆"},
    "cmd_config": {"en": "Show config path, providers and models", "zh": "显示配置路径、provider 与模型"},
    "cmd_settings": {"en": "Settings: network / tools / hooks / rules", "zh": "设置：网络/工具/hooks/权限规则"},
    "cmd_editor": {"en": "Set the external editor for Ctrl+G", "zh": "设置 Ctrl+G 使用的编辑器"},
    "cmd_mode": {"en": "Permission mode: default / plan / auto", "zh": "权限模式：default / plan / auto"},
    "cmd_yolo": {"en": "Toggle auto-approve mode", "zh": "切换全自动确认模式"},
    "cmd_reload": {"en": "Reload config.toml from disk", "zh": "从磁盘重载 config.toml"},
    "cmd_status": {"en": "Session and runtime status", "zh": "会话与运行时状态"},
    "cmd_doctor": {"en": "Environment and configuration self-check", "zh": "环境与配置自检"},
    "cmd_version": {"en": "Show version information", "zh": "显示版本信息"},
    "cmd_exit": {"en": "Exit oAset", "zh": "退出 oAset"},
    "cmd_cron": {"en": "List scheduled automations", "zh": "列出定时任务"},
    "cmd_tasks": {"en": "Browse background tasks", "zh": "查看后台任务"},
    "cmd_plugins": {"en": "List loaded plugins", "zh": "列出已加载插件"},
    "cmd_reload_plugins": {"en": "Hot reload plugins", "zh": "热重载插件"},
    "cmd_desktop": {"en": "List desktop windows (UIA, gated)", "zh": "枚举桌面窗口（UIA，经权限门控）"},
    "cmd_selfmaint": {"en": "Token-budgeted self-maintenance run", "zh": "按 token 预算运行自维护"},
    "cmd_search": {"en": "Full-text search over all past sessions", "zh": "全文检索全部历史会话"},
    "cmd_theme": {"en": "Switch theme", "zh": "切换主题"},
    "cmd_todo": {"en": "Toggle the plan strip above the input", "zh": "切换输入框上方的计划条"},
    "cmd_clear": {"en": "Clear the chat view (history is kept)", "zh": "清空聊天视图（保留历史）"},
    "cmd_title": {"en": "Set or show the session title", "zh": "设置或查看会话标题"},
    # TUI session / checkpoint panels
    "sessions_action_title": {
        "en": "What should happen with this session?",
        "zh": "对这个会话做什么？",
    },
    "sessions_action_switch": {
        "en": "Switch to it",
        "zh": "切换到它",
    },
    "sessions_action_show": {
        "en": "Show details (messages, files)",
        "zh": "查看详情（消息数、文件）",
    },
    "sessions_action_delete": {
        "en": "Delete it permanently",
        "zh": "永久删除",
    },
    "session_details": {
        "en": "session {sid} · {model} · {count} messages · updated {at}",
        "zh": "会话 {sid} · {model} · {count} 条消息 · 更新于 {at}",
    },
    "session_deleted_ok": {
        "en": "deleted session {sid}",
        "zh": "已删除会话 {sid}",
    },
    "sessions_empty": {
        "en": "no saved sessions for this workspace",
        "zh": "本工作区没有已保存的会话",
    },
    "undo_pick": {
        "en": "Restore which checkpoint?",
        "zh": "恢复哪个检查点？",
    },
    "undo_entry": {
        "en": "#{n} · {at} · {count} file(s): {files}",
        "zh": "#{n} · {at} · {count} 个文件：{files}",
    },
    "undo_none": {
        "en": "no checkpoints for this session",
        "zh": "本会话没有检查点",
    },
    "undo_done": {
        "en": "restored checkpoint #{n} ({count} file(s))",
        "zh": "已恢复检查点 #{n}（{count} 个文件）",
    },
    "fork_pick": {
        "en": "Fork which session?",
        "zh": "从哪个会话派生？",
    },
    "fork_current": {
        "en": "current session",
        "zh": "当前会话",
    },
    # TUI settings surface
    "settings_menu_title": {
        "en": "Settings",
        "zh": "设置",
    },
    "settings_network": {
        "en": "Network policy (egress)",
        "zh": "网络策略（出站）",
    },
    "settings_tools": {
        "en": "Tool allowlist (enable / disable)",
        "zh": "工具白名单（启用 / 停用）",
    },
    "settings_hooks": {
        "en": "Lifecycle hooks (shell / HTTP)",
        "zh": "生命周期 hooks（命令 / HTTP）",
    },
    "settings_rules": {
        "en": "Permission rules (allow / deny / ask)",
        "zh": "权限规则（allow / deny / ask）",
    },
    "settings_show": {
        "en": "Show the effective configuration",
        "zh": "查看当前生效配置",
    },
    "network_title": {
        "en": "Network mode — controls what may leave this machine",
        "zh": "网络模式——控制什么可以离开本机",
    },
    "network_local_only": {
        "en": "local_only — no remote I/O (loopback models and mock only)",
        "zh": "local_only——禁止远端 I/O（仅本机模型与 mock）",
    },
    "network_pull_only": {
        "en": "pull_only — receive only: model calls allowed, no push (default)",
        "zh": "pull_only——只收不发：允许模型调用，不推送（默认）",
    },
    "network_full": {
        "en": "full — allow push-class egress (web fetch, uploads, remote exec)",
        "zh": "full——允许推送类出站（网页抓取、上传、远程执行）",
    },
    "network_set": {
        "en": "network mode is now {mode}",
        "zh": "网络模式已设为 {mode}",
    },
    "settings_tools_title": {
        "en": "Toggle a tool for this session and config",
        "zh": "启停工具（本会话并写回配置）",
    },
    "settings_tools_current": {
        "en": "enabled: {enabled}",
        "zh": "已启用：{enabled}",
    },
    "settings_hooks_title": {
        "en": "Pick a lifecycle event to edit",
        "zh": "选择要编辑的生命周期事件",
    },
    "settings_hook_edit": {
        "en": "Action for {event} (shell command or http(s) URL; empty clears)",
        "zh": "{event} 的动作（命令或 http(s) URL；留空清除）",
    },
    "settings_hook_set": {
        "en": "hook {event} updated",
        "zh": "hook {event} 已更新",
    },
    "settings_hook_cleared": {
        "en": "hook {event} cleared",
        "zh": "hook {event} 已清除",
    },
    "settings_rules_title": {
        "en": "Permission rules",
        "zh": "权限规则",
    },
    "settings_rules_empty": {
        "en": "no permission rules configured",
        "zh": "尚未配置权限规则",
    },
    "settings_saved": {
        "en": "saved to config.toml",
        "zh": "已保存到 config.toml",
    },
    # TUI provider availability + relay onboarding
    "model_needs_key": {
        "en": "needs an API key for {provider}",
        "zh": "缺少 {provider} 的密钥",
    },
    "model_needs_key_msg": {
        "en": "{model} has no credential for provider {provider}",
        "zh": "{model} 所属 provider {provider} 没有可用凭据",
    },
    "send_needs_key": {
        "en": "Model {model} has no usable credential — nothing was sent.",
        "zh": "模型 {model} 没有可用凭据——这条消息没有发出。",
    },
    "send_needs_key_hint_cli": {
        "en": "Store a key with `oaset login {provider}` or run `oaset setup`.",
        "zh": "用 `oaset login {provider}` 保存密钥，或运行 `oaset setup`。",
    },
    "send_needs_key_hint": {
        "en": "Store a key with /login {provider}, then send again.",
        "zh": "用 /login {provider} 保存密钥，然后重新发送。",
    },
    "welcome_needs_key_badge": {
        "en": "needs a key — /login {provider}",
        "zh": "缺少密钥——/login {provider}",
    },
    "welcome_next_step": {
        "en": "Next: /login to add a key, then just ask.",
        "zh": "下一步：/login 配置密钥，然后直接提问。",
    },
    "help_get_started_title": {
        "en": "Getting started (5 things)",
        "zh": "入门五件事",
    },
    "help_get_started": {
        "en": "/login <provider> store a key · /model switch model · Esc interrupt · /undo rollback files · /sessions resume",
        "zh": "/login <provider> 存密钥 · /model 换模型 · Esc 随时中断 · /undo 回滚文件 · /sessions 恢复会话",
    },
    "help_primary_title": {
        "en": "Daily commands",
        "zh": "常用命令",
    },
    "help_more_hint": {
        "en": "Ctrl+K search · /help all for every command · /skills to load a playbook",
        "zh": "Ctrl+K 搜索 · /help all 看全部命令 · /skills 载入技能",
    },
    "model_needs_key_hint": {
        "en": "store a key with /login <provider>, then send again",
        "zh": "用 /login <provider> 保存密钥，然后重新发送",
    },
    "wizard_vendor_ready": {
        "en": "ready",
        "zh": "可用",
    },
    "wizard_vendor_needs_key": {
        "en": "needs key",
        "zh": "缺密钥",
    },
    "wizard_vendor_custom_hint": {
        "en": "for a vendor API, a relay/proxy (中转站) or a self-hosted gateway",
        "zh": "适用于厂商 API、中转站/代理或自建网关",
    },
    # TUI model limits resolution
    "wizard_limits_resolved": {
        "en": "context {context} / max output {output} — resolved from {source}; no need to type them",
        "zh": "上下文 {context} / 最大输出 {output} —— 来自{source}，无需手输",
    },
    "wizard_limits_from_provider": {
        "en": "the provider's model list",
        "zh": "provider 的模型列表",
    },
    "wizard_limits_from_vendor": {
        "en": "built-in vendor defaults",
        "zh": "内置厂商默认值",
    },
    "wizard_limits_from_default": {
        "en": "general defaults",
        "zh": "通用默认值",
    },
    "wizard_limits_from_manual": {
        "en": "your manual entry",
        "zh": "你手动填写",
    },
    "wizard_edit_limits": {
        "en": "Change context / output (advanced)",
        "zh": "修改上下文 / 输出（高级）",
    },
    # TUI per-turn meta
    "meta_tool_calls": {
        "en": "{n} tool calls",
        "zh": "{n} 次工具调用",
    },
    "upd_skipped": {
        "en": "✗ {name}: {reason}",
        "zh": "✗ {name}: {reason}",
    },
    "upd_source_local": {
        "en": "(local)",
        "zh": "(local)",
    },
    "welcome_title": {
        "en": "oAset",
        "zh": "oAset",
    },
    "welcome_greet_morning": {"en": "Good morning", "zh": "早上好"},
    "welcome_greet_noon": {"en": "Good afternoon", "zh": "中午好"},
    "welcome_greet_afternoon": {"en": "Good afternoon", "zh": "下午好"},
    "welcome_greet_evening": {"en": "Good evening", "zh": "晚上好"},
    "welcome_greet_late": {"en": "Late night build session?", "zh": "夜深了，改哪行？"},
    "welcome_session_ordinal": {
        "en": "session #{n} in this workspace",
        "zh": "本工作区第 {n} 次会话",
    },
    "welcome_streak": {
        "en": "{n}-day streak",
        "zh": "连用 {n} 天",
    },
    "welcome_tagline": {
        "en": "local-first personal coding terminal",
        "zh": "本地优先的个人编码终端",
    },
    "welcome_model_unset": {
        "en": "not configured yet — /login to add a key, /model to pick one",
        "zh": "暂无配置——/login 添加密钥，/model 选择模型",
    },
    "welcome_hint": {
        "en": "Type a question · /help",
        "zh": "直接提问 · /help",
    },
    "working": {
        "en": "working...",
        "zh": "工作中…",
    },
    "ready": {
        "en": "ready",
        "zh": "就绪",
    },
    "hint_idle": {
        "en": "/login store a key | /help commands | esc interrupt",
        "zh": "/login 存密钥 | /help 命令 | esc 中断",
    },
    "hint_busy": {
        "en": "esc: interrupt | /help: show commands",
        "zh": "esc: 中断 | /help: 显示命令",
    },
    "context_line": {
        "en": "context: {pct:.1f}% ({used}/{total})",
        "zh": "上下文：{pct:.1f}%（{used}/{total}）",
    },
    "thinking": {
        "en": "thinking",
        "zh": "思考中",
    },
    "thinking_waiting": {
        "en": "waiting for the first tokens…",
        "zh": "等待模型返回…",
    },
    "thought_folded": {
        "en": "thought {secs}s · {n} chars",
        "zh": "已思考 {secs}s · {n} 字",
    },
    "thought_folded_no_time": {
        "en": "thought · {n} chars",
        "zh": "已思考 · {n} 字",
    },
    "chars_short": {"en": "chars", "zh": "字"},
    "interrupted": {
        "en": "interrupted by user",
        "zh": "已被用户中断",
    },
    "no_plugins": {
        "en": "No plugins. Drop .py files into ~/.oaset/plugins/ or ./.oaset/plugins/ "
        "- each defines register(api) to add tools/commands.",
        "zh": "暂无插件。将 .py 文件放入 ~/.oaset/plugins/ 或 ./.oaset/plugins/，"
        "每个文件定义 register(api) 即可注册工具/命令。",
    },
    "no_sessions": {
        "en": "No sessions recorded for this directory yet.",
        "zh": "该目录还没有会话记录。",
    },
    "context_label": {
        "en": "context",
        "zh": "上下文",
    },
    "allow_persisted": {
        "en": "{tool} added to the durable allowlist (config.toml).",
        "zh": "{tool} 已写入持久允许清单（config.toml）。",
    },
    "approval_timeout": {
        "en": "Approval for {tool} timed out (90s) — denied by default.",
        "zh": "{tool} 的授权超时（90 秒），默认拒绝。",
    },
    "title_set": {
        "en": "Session title: {title}",
        "zh": "会话标题：{title}",
    },
    "reloaded": {
        "en": "Configuration reloaded from disk.",
        "zh": "配置已从磁盘重新加载。",
    },
    "mcp_reloaded": {
        "en": "MCP servers reloaded.",
        "zh": "MCP 服务器已重新加载。",
    },
    "steer_hint": {
        "en": "Nothing is running — /steer injects into an active turn.",
        "zh": "当前没有运行中的任务——/steer 用于向进行中的回合注入指令。",
    },
    "retry_none": {
        "en": "No previous prompt to retry.",
        "zh": "没有可重发的上一条消息。",
    },
    "history_empty": {
        "en": "Conversation is empty.",
        "zh": "当前会话还没有消息。",
    },
    "denied": {
        "en": "The user declined this operation.",
        "zh": "用户拒绝了该操作。",
    },

    "busy_queued": {
        "en": "Busy — message queued for injection (Ctrl-S).",
        "zh": "忙碌 — 消息已排队，将在回合中注入（Ctrl-S）。",
    },
    "interrupted_notice": {
        "en": "Interrupted by user.",
        "zh": "已被用户中断。",
    },
    "no_sessions_dir": {
        "en": "No sessions recorded for this directory yet.",
        "zh": "当前目录还没有历史会话。",
    },
    "title_failed": {
        "en": "Failed to set title.",
        "zh": "设置标题失败。",
    },
    "turn_running": {
        "en": "Wait for the current turn to finish.",
        "zh": "请等待当前回合结束。",
    },
    "compact_empty": {
        "en": "Nothing to compact yet.",
        "zh": "还没有可压缩的内容。",
    },
    "goal_none": {
        "en": "No active goal. /goal <objective> · /goal budget <tokens> · /goal stop",
        "zh": "没有进行中的目标。/goal <目标> · /goal budget <千token> · /goal stop",
    },
    "tasks_none": {
        "en": "No background tasks. The agent can start one with run_shell run_in_background.",
        "zh": "没有后台任务。agent 可用 run_shell run_in_background 启动一个。",
    },
    "goal_cleared": {
        "en": "Goal cleared.",
        "zh": "目标已清除。",
    },
    "steer_usage": {
        "en": "Usage: /steer <instruction>",
        "zh": "用法：/steer <指令>",
    },
    "steered": {
        "en": "Steered the running turn.",
        "zh": "已注入运行中的回合。",
    },
    "login_cancelled": {
        "en": "Login cancelled.",
        "zh": "登录已取消。",
    },
    "login_cancelled_provider": {
        "en": "Login for {provider} cancelled.",
        "zh": "{provider} 的登录已取消。",
    },
    "no_credentials": {
        "en": "No stored credentials.",
        "zh": "没有已保存的凭据。",
    },
    "unknown_command": {
        "en": "Unknown command /{name}. Try /help.",
        "zh": "未知命令 /{name}。试试 /help。",
    },
    "unknown_command_suggest": {
        "en": "Unknown command /{name}. Did you mean /{suggestion}? (or /help, /palette)",
        "zh": "未知命令 /{name}。是否想输入 /{suggestion}？（或 /help、/palette）",
    },
    "command_cancelled": {
        "en": "Cancelled /{name} - nothing was changed.",
        "zh": "已取消 /{name}，未做任何更改。",
    },
    "command_crashed_hint": {
        "en": "The command raised before finishing. Nothing was applied; details in ~/.oaset/logs/oaset.log (run with --debug).",
        "zh": "命令在完成前抛错，未产生任何更改；详情见 ~/.oaset/logs/oaset.log（以 --debug 运行）。",
    },
    "session_new_notice": {
        "en": "Started a new session; the previous one is kept in history (/sessions).",
        "zh": "已开始新会话；原会话已保存在历史中（/sessions 可找回）。",
    },
    "cleared_notice": {
        "en": "View cleared; the transcript is still on disk (/sessions).",
        "zh": "界面已清空；会话记录仍保存在磁盘（/sessions）。",
    },
    "paste_empty_warn": {
        "en": "Clipboard is empty or unavailable - nothing pasted.",
        "zh": "剪贴板为空或不可用——没有内容可粘贴。",
    },
    "paste_ok_notice": {
        "en": "Pasted {chars} characters into the input.",
        "zh": "已粘贴 {chars} 个字符到输入框。",
    },
    "todo_empty_notice": {
        "en": "No todos yet - the model adds them with the todo_write tool.",
        "zh": "暂无待办——模型会通过 todo_write 工具添加。",
    },
    "save_failed": {
        "en": "Session save failed: {err}",
        "zh": "会话保存失败：{err}",
    },
    "model_switched": {
        "en": "Switched to {model}",
        "zh": "已切换到 {model}",
    },
    "model_switch_verify": {
        "en": "Model {model} connectivity check",
        "zh": "模型 {model} 连通性检测",
    },
    "session_load_failed": {
        "en": "Session {sid} could not be loaded.",
        "zh": "会话 {sid} 加载失败。",
    },
    "restored_messages": {
        "en": "Restored {count} messages.",
        "zh": "已恢复 {count} 条消息。",
    },
    "theme_set": {
        "en": "Theme: {theme}",
        "zh": "主题：{theme}",
    },
    "compacting": {
        "en": "Compacting context…",
        "zh": "正在压缩上下文…",
    },
    "compact_failed": {
        "en": "Compact failed: {err}",
        "zh": "压缩失败：{err}",
    },
    "compacted": {
        "en": "Compacted: ≈{before} → ≈{after} tokens.",
        "zh": "已压缩：≈{before} → ≈{after} tokens。",
    },
    "skills_reloaded": {
        "en": "Skills reloaded: {count} available; system prompt rebuilt.",
        "zh": "技能已重载：可用 {count} 个；系统提示词已重建。",
    },
    "reload_failed": {
        "en": "Reload failed: {err}",
        "zh": "重载失败：{err}",
    },
    "init_analyzing": {
        "en": "/init: analyzing the codebase …",
        "zh": "/init：正在分析代码库……",
    },
    "init_prompt": {
        "en": (
            "Analyse this codebase and generate an AGENTS.md file (write it to the "
            "workspace root). First inspect the directory structure and key files "
            "(README, config, entry points), then write a concise AGENTS.md covering: "
            "project overview, tech stack, directory layout, build/test commands, "
            "code conventions and caveats. Everything must come from files you "
            "actually read — never invent."
        ),
        "zh": (
            "请分析当前代码库并生成 AGENTS.md 文件（写入工作区根目录）。"
            "先查看目录结构与关键文件（README、配置、入口），然后写出一个精炼的 AGENTS.md："
            "项目简介、技术栈、目录结构、构建/测试命令、代码约定与注意事项。"
            "内容必须来自你实际读到的文件，不要编造。"
        ),
    },
    # ---- ARG_PROMPTS labels: the table stores keys, pickers resolve via t() --
    "arg_selfmaint_title": {
        "en": "Self-maintenance plan", "zh": "体验计划",
    },
    "arg_selfmaint_run": {
        "en": "run - run one self-maintenance cycle", "zh": "run - 运行一次自维护",
    },
    "arg_selfmaint_enable": {
        "en": "enable - turn on the plan (read-only by default)", "zh": "enable - 开启体验计划（默认只读）",
    },
    "arg_market_discover": {
        "en": "Discover", "zh": "发现",
    },
    "arg_market_installable": {
        "en": "Installable", "zh": "可安装",
    },
    "arg_market_installed": {
        "en": "Installed", "zh": "已安装",
    },
    "arg_market_sources": {
        "en": "Sources", "zh": "来源",
    },
    "arg_worktree_list": {
        "en": "list - list existing worktrees", "zh": "list - 列出现有 worktree",
    },
    "arg_worktree_new": {
        "en": "new <name> - create a linked checkout", "zh": "new <name> - 新建链接检出",
    },
    "arg_worktree_remove": {
        "en": "remove <name> - delete worktree and branch", "zh": "remove <name> - 删除 worktree 与分支",
    },
    "arg_goal_status": {
        "en": "status - show the current goal", "zh": "status - 显示当前目标",
    },
    "arg_goal_stop": {
        "en": "stop - clear the goal", "zh": "stop - 清除目标",
    },
    "arg_goal_budget": {
        "en": "budget <k> - set a k-thousand-token budget", "zh": "budget <k> - 设置 k 千 token 预算",
    },
    "arg_density_cozy": {
        "en": "cozy - default line spacing", "zh": "cozy - 默认行距",
    },
    "arg_density_compact": {
        "en": "compact - tighter spacing", "zh": "compact - 更紧凑",
    },
    "arg_settings_network": {
        "en": "network - egress network policy", "zh": "network - 出站网络策略",
    },
    "arg_settings_tools": {
        "en": "tools - enable or disable tools", "zh": "tools - 启停工具",
    },
    "arg_settings_hooks": {
        "en": "hooks - lifecycle hooks", "zh": "hooks - 生命周期钩子",
    },
    "arg_settings_rules": {
        "en": "rules - permission rules (view)", "zh": "rules - 权限规则（查看）",
    },
    "arg_settings_show": {
        "en": "show - show current configuration", "zh": "show - 查看当前配置",
    },
    "arg_agents_show": {
        "en": "show <name> - view definition/model/tools", "zh": "show <name> - 查看定义/模型/工具",
    },
    "arg_agents_new": {
        "en": "new <name> - create a sub-agent (approval required)", "zh": "new <name> - 新建子代理（需审批）",
    },
    "arg_agents_delete": {
        "en": "delete <name> - delete a sub-agent (approval required)", "zh": "delete <name> - 删除子代理（需审批）",
    },
    "arg_agents_reload": {
        "en": "reload - reload definitions", "zh": "reload - 重新加载定义",
    },
    "arg_skills_show": {
        "en": "show <name> - view path and summary", "zh": "show <name> - 查看路径与摘要",
    },
    "arg_skills_new": {
        "en": "new <name> - create a skill skeleton (approval required)", "zh": "new <name> - 新建技能骨架（需审批）",
    },
    "arg_skills_delete": {
        "en": "delete <name> - delete a skill (approval required)", "zh": "delete <name> - 删除技能（需审批）",
    },
    "arg_mcp_list": {
        "en": "list - list servers and status", "zh": "list - 列出服务器与状态",
    },
    "arg_mcp_add": {
        "en": "add - add a server (stdio/HTTP)", "zh": "add - 新增服务器（stdio/HTTP）",
    },
    "arg_mcp_enable": {
        "en": "enable - enable a server", "zh": "enable - 启用服务器",
    },
    "arg_mcp_disable": {
        "en": "disable - disable a server", "zh": "disable - 停用服务器",
    },
    "arg_mcp_remove": {
        "en": "remove - remove a server", "zh": "remove - 删除服务器",
    },
    "arg_mcp_reload": {
        "en": "reload - restart servers and re-register tools", "zh": "reload - 重启服务器并重注册工具",
    },
    "arg_mcp_roots": {
        "en": "roots - extra workspace roots", "zh": "roots - 额外工作区根目录",
    },
    "arg_model_add": {
        "en": "add - add a provider/model (wizard)", "zh": "add - 新增 provider/模型（向导）",
    },
    "arg_model_list": {
        "en": "list - view configured providers and models", "zh": "list - 查看已配置 provider 与模型",
    },
    "arg_model_verify": {
        "en": "verify - check connectivity", "zh": "verify - 验证连通性",
    },
    "title_current": {
        "en": "{title}  —  /title <name>",
        "zh": "{title}  —  /title <名称>",
    },
    "history_header": {
        "en": "History (last {count}):",
        "zh": "历史（最近 {count} 条）：",
    },
    "goal_active": {
        "en": "⌖ {text}",
        "zh": "⌖ {text}",
    },
    "goal_set": {
        "en": "⌖ Goal set: {text}\nUse /goal budget <k> to cap tokens; /goal stop to clear.",
        "zh": "⌖ 目标已设定：{text}\n用 /goal budget <千token> 限制预算；/goal stop 清除。",
    },
    "goal_budget": {
        "en": "Goal budget set: {tokens} tokens.",
        "zh": "目标预算已设为 {tokens} tokens。",
    },
    "goal_first": {
        "en": "Set a goal first: /goal <objective>",
        "zh": "请先设定目标：/goal <目标>",
    },
    "goal_budget_usage": {
        "en": "Usage: /goal budget <k-tokens>",
        "zh": "用法：/goal budget <千token>",
    },
    "mode_default": {
        "en": "Permission mode: writes and shell require confirmation.",
        "zh": "权限模式：写文件与 shell 需确认。",
    },
    "mode_plan": {
        "en": "PLAN mode — read-only until you switch back (Shift-Tab).",
        "zh": "PLAN 模式——只读，直到切回（Shift+Tab）。",
    },
    "mode_auto": {
        "en": "AUTO mode — everything runs without confirmation. Careful!",
        "zh": "AUTO 模式——全部自动执行，注意风险！",
    },
    "picker_hints": {
        "en": "up/down select · enter confirm · q cancel",
        "zh": "↑/↓ 选择 · enter 确认 · q 取消",
    },
    "perm_title": {
        "en": "Permission required — {badge}",
        "zh": "需要授权 — {badge}",
    },
    "perm_allow_once": {
        "en": "y allow once",
        "zh": "y 允许一次",
    },
    "perm_allow_dir": {
        "en": "a allow this directory for session",
        "zh": "a 本目录本会话内放行",
    },
    "perm_allow_session": {
        "en": "a allow for session",
        "zh": "a 本会话内放行",
    },
    "perm_deny": {
        "en": "n deny",
        "zh": "n 拒绝",
    },
    "login_banner_text": {
        "en": "Login {provider} — paste the API key into the input below and press enter (stored locally) · esc cancel",
        "zh": "登录 {provider} —— 在下方输入框粘贴 API key 后回车保存（仅存本地） · esc 取消",
    },
    "verify_gate_notice": {
        "en": "verify-gate: evidence required before done",
        "zh": "verify-gate：收工前需要证据（测试/构建/真机画面）",
    },
    "skills_none": {
        "en": "No extra skills. Built-in playbooks ship with oAset; add more under ~/.oaset/skills/<name>/SKILL.md or ./.oaset/skills/.",
        "zh": "没有额外技能。内置手册随 oAset 提供；更多技能放在 ~/.oaset/skills/<名称>/SKILL.md 或 ./.oaset/skills/。",
    },
    "skill_not_found": {
        "en": "Skill '{name}' not found.",
        "zh": "未找到技能“{name}”。",
    },
    "skill_loaded": {
        "en": "Skill '{name}' loaded into context.",
        "zh": "技能“{name}”已载入上下文。",
    },
    "tools_header": {
        "en": "Tools:",
        "zh": "工具：",
    },
    "cron_header": {
        "en": "Scheduled jobs:",
        "zh": "定时任务：",
    },
    "task_started": {
        "en": "Started {task_id} in the background: {desc}\nRead output with task_output; stop it with task_stop.",
        "zh": "已在后台启动 {task_id}：{desc}\n用 task_output 读取输出；用 task_stop 停止。",
    },
    "shell_fallback": {
        "en": "Shell probe: using {shell}. {reason}",
        "zh": "shell 探测：使用 {shell}。{reason}",
    },
    "task_unknown": {
        "en": "Unknown task: {task_id}",
        "zh": "未知任务：{task_id}",
    },
    "task_already": {
        "en": "{task_id} already {status}",
        "zh": "{task_id} 已是 {status} 状态",
    },
    "task_stopped": {
        "en": "{task_id} stopped (ran {secs:.0f}s)",
        "zh": "{task_id} 已停止（运行 {secs:.0f} 秒）",
    },
    "bg_done": {
        "en": "Background {task_id} {status}: {desc}",
        "zh": "后台任务 {task_id} {status}：{desc}",
    },
    "credential_stored": {
        "en": "Stored API key for {provider} ({chars} chars).",
        "zh": "已保存 {provider} 的 API key（{chars} 字符）。",
    },
    "no_checkpoints": {
        "en": "No checkpoints to undo.",
        "zh": "没有可撤销的 checkpoint。",
    },
    "plugins_list": {
        "en": "Plugins: {names}",
        "zh": "插件：{names}",
    },
    "plugins_reloaded": {
        "en": "Plugins reloaded: {count} loaded.",
        "zh": "插件已重载：当前加载 {count} 个。",
    },
    "forked_to": {
        "en": "Forked to session {sid}.",
        "zh": "已派生到会话 {sid}。",
    },
    "subagents_header": {
        "en": "Sub-agents (used by the task tool):",
        "zh": "子代理（供 task 工具使用）：",
    },
    "unknown_provider": {
        "en": "Unknown provider '{name}'.",
        "zh": "未知 provider“{name}”。",
    },
    "credential_removed": {
        "en": "Removed stored credential for {provider}.",
        "zh": "已删除 {provider} 的凭据。",
    },
    "search_no_match": {
        "en": "No past conversation matches '{query}'.",
        "zh": "历史会话中没有匹配“{query}”的内容。",
    },
    "search_header": {
        "en": "Search '{query}':",
        "zh": "搜索“{query}”：",
    },
    "tool_toggled": {
        "en": "{name} {state} (saved).",
        "zh": "{name} 已{state}（已保存）。",
    },
    "export_ok": {
        "en": "Session exported: {path}",
        "zh": "会话已导出：{path}",
    },
    "export_failed": {
        "en": "Export failed (session missing).",
        "zh": "导出失败（会话不存在）。",
    },
    "session_delete_usage": {
        "en": "Usage: /session-delete <id-suffix>",
        "zh": "用法：/session-delete <会话ID后缀>",
    },
    "session_delete_current": {
        "en": "Cannot delete the current session.",
        "zh": "不能删除当前会话。",
    },
    "session_deleted": {
        "en": "Session {sid} deleted.",
        "zh": "会话 {sid} 已删除。",
    },
    "session_not_found": {
        "en": "Session {sid} not found.",
        "zh": "未找到会话 {sid}。",
    },
    "goal_budget_set": {
        "en": "Goal budget set: {tokens} tokens.",
        "zh": "目标预算已设为 {tokens} tokens。",
    },
    "lsp_connected": {
        "en": "LSP: {servers} server(s) connected",
        "zh": "LSP：已连接 {servers} 个服务器",
    },
    "roots_header": {
        "en": "MCP workspace roots (roots/list)",
        "zh": "MCP 工作区根目录（roots/list）",
    },
    "roots_workspace": {
        "en": "workspace (fixed)",
        "zh": "工作区（固定）",
    },
    "roots_added": {
        "en": "Root added: {path} — roots handlers refreshed",
        "zh": "已添加根目录：{path}（roots 处理器已刷新）",
    },
    "roots_removed": {
        "en": "Root removed: {path}",
        "zh": "已移除根目录：{path}",
    },
    "roots_cleared": {
        "en": "All extra roots removed.",
        "zh": "已移除全部额外根目录。",
    },
    "roots_not_dir": {
        "en": "{path} is not an existing directory",
        "zh": "{path} 不是已存在的目录",
    },
    "roots_duplicate": {
        "en": "Already exposed: {path}",
        "zh": "该路径已在根目录列表中：{path}",
    },
    "roots_bad_index": {
        "en": "No extra root #{n} (valid: 1..{max})",
        "zh": "不存在第 {n} 个额外根目录（有效范围：1..{max}）",
    },
    "roots_remove_usage": {
        "en": "usage: /roots remove <n>",
        "zh": "用法：/roots remove <序号>",
    },
    # I18N-01 closeout: command effect lines (palette confirm pages)
    "effect_update": {
        "en": "Refresh the catalog over the network / build install plans (apply and rollback go through the CLI only)",
        "zh": "联网刷新目录缓存 / 生成安装计划（apply 与 rollback 只经 CLI）",
    },
    # These two are the highest-risk actions in the whole command surface
    # (download-and-replace, restore-over-files). They used to fall through
    # effect_display()'s catalog lookup and show a hardcoded Chinese line even
    # in the English UI — on the very confirmation page that is supposed to
    # explain what is about to happen.
    "effect_update_apply": {
        "en": "Download and replace plugins/config/core packages (rollback can undo it)",
        "zh": "下载并替换插件/配置/核心包（可用 rollback 撤销）",
    },
    "effect_update_rollback": {
        "en": "Restore the file state from before the last apply, per the recorded transaction",
        "zh": "按历史记录恢复上一次 apply 之前的文件状态",
    },
    "effect_market": {
        "en": "Browse and install plugins from the active source (network policy + sha256 + transactional rollback; http(s) sources only, one at a time)",
        "zh": "从活动来源浏览并安装插件（复用更新中心：网络策略+sha256+事务回滚；来源仅 http(s) 且一次只用一个）",
    },
    "effect_worktree": {
        "en": "Add or remove a git worktree under .oaset/worktrees; your dirty workspace is untouched",
        "zh": "在 .oaset/worktrees 下加/删 git worktree，不碰当前脏工作区",
    },
    "effect_exit": {
        "en": "Exit oAset (ends the running turn and background tasks)",
        "zh": "退出 oAset（结束运行中的回合与后台任务）",
    },
    "effect_density": {
        "en": "Write the chat spacing back to [ui] density in config.toml",
        "zh": "写回 config.toml 的 [ui] density",
    },
    "effect_login": {
        "en": "Store the API key locally in ~/.oaset/credentials/ (0600); it never goes into config.toml",
        "zh": "密钥只写入本机 ~/.oaset/credentials/（0600），不会进 config.toml",
    },
    "effect_new": {
        "en": "Start a fresh session: the old one stays in history, the view clears",
        "zh": "新建会话：当前会话保留在历史中，界面清空",
    },
    "effect_sessions": {
        "en": "Switch the current session (unsaved drafts are lost)",
        "zh": "切换当前会话（未保存的草稿会丢失）",
    },
    "effect_think": {
        "en": "adapt reasoning depth per provider (budget / reasoning_effort / thinkingBudget / on-off)",
        "zh": "适配各家思考档位（Anthropic 预算 / OpenAI reasoning_effort / Gemini thinkingBudget / GLM-Qwen 开关）",
    },
    "effect_model": {
        "en": "Switch the model and write the default back to config.toml",
        "zh": "切换模型并写回 config.toml 的默认模型",
    },
    "effect_theme": {
        "en": "Switch the theme and write it back to config.toml",
        "zh": "切换主题并写回 config.toml",
    },
    "effect_mode": {
        "en": "Change the approval policy for subsequent tool calls",
        "zh": "改变后续工具调用的确认策略",
    },
    "effect_yolo": {
        "en": "While on, every tool call runs without approval",
        "zh": "开启后所有工具调用免确认执行",
    },
    "effect_reload_plugins": {
        "en": "Retract old plugin tools/commands and reload the plugin directory",
        "zh": "撤销旧插件工具/命令并重新加载插件目录",
    },
    "effect_desktop": {
        "en": "Enumerate desktop windows through the permission-gated UIA provider",
        "zh": "通过受权限门控的 UIA provider 枚举桌面窗口",
    },
    "effect_selfmaint": {
        "en": "Run one token-budgeted self-maintenance cycle; installing patches needs allow_apply plus approval",
        "zh": "按固定 token 预算运行一次自维护；安装补丁需 allow_apply 与审批",
    },
    "effect_compact": {
        "en": "Ask the model to compress history (irreversible; rewrites session records)",
        "zh": "调用模型压缩历史（不可撤销，会改写会话记录）",
    },
    "effect_fork": {
        "en": "Fork the current session into a new one and switch to it",
        "zh": "复制当前会话为新会话并切换过去",
    },
    "effect_undo": {
        "en": "Restore files from checkpoints (overwrites current file contents)",
        "zh": "按 checkpoint 还原文件（会覆盖当前文件内容）",
    },
    "effect_rewind": {
        "en": "Drop the last N turns from the session record (irreversible)",
        "zh": "从会话记录中删除最近 N 轮对话（不可撤销）",
    },
    "effect_tools_toggle": {
        "en": "Enable/disable a tool and write it back to config.toml disabled_tools",
        "zh": "启停工具并写回 config.toml 的 disabled_tools",
    },
    "effect_models": {
        "en": "Add a provider/model through the guided wizard, or inspect the configured list; nothing is written before confirmation",
        "zh": "通过向导新增 provider/模型，或查看已配置列表；确认前不写入任何内容",
    },
    "effect_mcp": {
        "en": "Edit mcp.json server entries (project/user scope) and optionally restart them",
        "zh": "编辑 mcp.json 的服务器条目（项目级/用户级）并可选重启",
    },
    "effect_settings": {
        "en": "Writes the network mode / tool toggles / hooks back to config.toml (reversible)",
        "zh": "写回 config.toml 的网络模式/工具开关/hooks（均可撤销）",
    },
    "effect_agents": {
        "en": "Creating/deleting sub-agents writes files and needs approval; each sub-agent can have its own model and tool allowlist",
        "zh": "新建/删除子代理会写盘并需审批；子代理可用独立模型与工具白名单",
    },
    "effect_skills": {
        "en": "Load a skill into the system prompt (takes effect this session)",
        "zh": "把某个 skill 载入系统提示（本会话内生效）",
    },
    "effect_clear": {
        "en": "Clears the view only; history stays on disk",
        "zh": "只清空界面，历史仍保存在磁盘",
    },
    "effect_export": {
        "en": "Writes a Markdown file into the current directory",
        "zh": "在当前目录写出 Markdown 文件",
    },
    "effect_session_delete": {
        "en": "Permanently deletes that session file from disk",
        "zh": "从磁盘永久删除该会话文件",
    },
    "effect_title": {
        "en": "Writes the session metadata",
        "zh": "写入会话元数据",
    },
    "effect_goal": {
        "en": "Rewrites the goal block in the system prompt (takes effect this session)",
        "zh": "改写系统提示中的目标块（本会话内生效）",
    },
    "effect_editor": {
        "en": "Writes the editor command back to config.toml",
        "zh": "写回 config.toml 的编辑器命令",
    },
    "effect_image": {
        "en": "Sends the image with your next message (needs a model with image_in)",
        "zh": "把该图片随下一条消息发送给模型（需模型支持 image_in）",
    },
    "effect_steer": {
        "en": "Injects text into the running turn; the model reacts immediately",
        "zh": "把文本注入正在运行的回合，会立即影响模型行为",
    },
    "effect_retry": {
        "en": "Calls the model again (consumes tokens)",
        "zh": "再次调用模型（会消耗 token）",
    },
    "effect_init": {
        "en": "Analyzes the codebase in a model turn and writes AGENTS.md",
        "zh": "以模型回合分析代码库并写入 AGENTS.md",
    },
    "effect_reload": {
        "en": "Rebuilds provider/fallback/hooks/shell configuration",
        "zh": "重建 provider/fallback/hooks/shell 配置",
    },
    "effect_reload_mcp": {
        "en": "Stops and restarts MCP servers, replacing registered tools",
        "zh": "关闭并重启 MCP 服务器，替换已注册工具",
    },
    "effect_roots": {
        "en": "roots/list discloses the extra root paths to MCP servers (adding/removing refreshes handlers immediately)",
        "zh": "roots/list 会把额外根目录路径披露给 MCP server（新增/移除立即刷新处理器）",
    },
    "effect_reload_skills": {
        "en": "Rebuilds the skill list and rewrites the system prompt",
        "zh": "重建 skill 列表并改写系统提示",
    },
    "effect_plugin": {
        "en": "Plugin command: behavior is defined by the plugin itself",
        "zh": "插件命令：行为由插件自身决定",
    },

    "blocked_unapprovable": {
        "en": "This command matched an unapprovable dangerous pattern (disk wipe / firmware / system damage) and is blocked by policy.",
        "zh": "此命令命中不可审批的危险模式（磁盘抹除/固件/系统级破坏），安全策略无条件拦截。",
    },
    "danger_marker": {
        "en": "  ⚠ DANGEROUS (no always-allow)",
        "zh": "  ⚠ 危险命令（不可选择“本会话始终允许”）",
    },
    "denied_msg": {
        "en": "The user declined this operation.",
        "zh": "用户拒绝了该操作。",
    },
    "shell_blocked_dangerous": {
        "en": ("This command hits an unapprovable danger pattern (disk wipe / "
               "firmware / system-level destruction); the safety policy blocks "
               "it unconditionally."),
        "zh": "此命令命中不可审批的危险模式（磁盘抹除/固件/系统级破坏），安全策略无条件拦截。",
    },
    "shell_dangerous_note": {
        "en": "⚠ DANGEROUS (per-session \"always allow\" is unavailable)",
        "zh": "⚠ DANGEROUS（不可选择“本会话始终允许”）",
    },
    "export_title": {
        "en": "# oAset session {sid}",
        "zh": "# oAset 会话 {sid}",
    },
    "export_dir": {
        "en": "- directory: {cwd}",
        "zh": "- 目录: {cwd}",
    },
    "export_model": {
        "en": "- model: {model}",
        "zh": "- 模型: {model}",
    },
    "export_time": {
        "en": "- created: {created}",
        "zh": "- 时间: {created}",
    },
    "export_user": {
        "en": "## User",
        "zh": "## 用户",
    },
    "export_tool": {
        "en": "### Tool result",
        "zh": "### 工具结果",
    },
    "spill_saved_note": {
        "en": "\n[full output saved to: {path}]",
        "zh": "\n[完整输出已保存: {path}]",
    },
    "tool_native_suffix": {
        "en": " (native)",
        "zh": "·原生",
    },
    "manifest_invalid": {
        "en": "[invalid manifest: {err}]",
        "zh": "[manifest 无效: {err}]",
    },
    "doctor_card_version": {
        "en": "oaset      {version}",
        "zh": "oaset      {version}",
    },
    "doctor_card_python": {
        "en": "python     {python} on {system}",
        "zh": "python     {python}（{system}）",
    },
    "doctor_card_config": {
        "en": "config     {path}",
        "zh": "config     {path}",
    },
    "doctor_card_credentials": {
        "en": "credentials {count} stored",
        "zh": "credentials 已存 {count} 条",
    },
    "doctor_card_fts5": {
        "en": "fts5       {state}",
        "zh": "fts5       {state}",
    },
    "doctor_fts_ok": {
        "en": "available",
        "zh": "可用",
    },
    "doctor_fts_missing": {
        "en": "unavailable (search degraded)",
        "zh": "不可用（搜索降级）",
    },
    "doctor_card_tools": {
        "en": "tools      {active} active / {allowed} always-allowed",
        "zh": "tools      {active} 个启用 / {allowed} 个始终允许",
    },
    "doctor_card_mcp": {
        "en": "mcp        {count} adapters (config: {path})",
        "zh": "mcp        {count} 个适配器（config: {path}）",
    },
    "doctor_card_network": {
        "en": "network    {mode}",
        "zh": "network    {mode}",
    },
    "mcp_serve_all_tools_warning": {
        "en": ("warning: --all-tools exposes write/exec tools to the MCP client "
               "without oAset-side approval; the client is the only gate."),
        "zh": ("warning: --all-tools 会把写/执行类工具暴露给 MCP 客户端，"
               "oAset 侧不再审批；客户端是唯一的把关者。"),
    },
    "ask_user_confirm": {
        "en": "Confirm",
        "zh": "确认",
    },
    "ask_user_cancel": {
        "en": "Cancel",
        "zh": "取消",
    },
    "ask_user_timeout": {
        "en": ("No answer within {seconds}s (auto-cancelled on timeout). Answer "
               "from the available information and state clearly that it is an "
               "assumption."),
        "zh": ("用户超过 {seconds} 秒未回答（超时自动取消）。"
               "请基于已有信息给出你的最佳假设并明确说明这是假设。"),
    },
    "ask_user_no_answer": {
        "en": "The user did not answer (cancelled).",
        "zh": "用户没有回答（已取消）。",
    },
    "ask_user_answered": {
        "en": "The user chose: {answer}",
        "zh": "用户选择了：{answer}",
    },
    "denied_headless_msg": {
        "en": ("This session has no interactive approver (headless run "
               "without --yolo), so the operation was denied."),
        "zh": ("本会话没有交互式审批人（headless 运行且未加 --yolo），操作被拒绝。"),
    },
    "goal_set_notice": {
        "en": "⌖ Goal set: {text}\nUse /goal budget <k> to cap tokens; /goal stop to clear.",
        "zh": "⌖ 目标已设定：{text}\n用 /goal budget <千token> 限制预算；/goal stop 清除。",
    },
    "fallback_switch": {
        "en": "Primary model failed — switching to fallback: {model}",
        "zh": "主模型失败——已切换到备用模型：{model}",
    },
    "rewound": {
        "en": "Rewound {turns} turn(s) — {count} messages remain.",
        "zh": "已回退 {turns} 轮对话——剩余 {count} 条消息。",
    },
    "editor_none": {
        "en": "No editor configured. /editor <command> or set $EDITOR.",
        "zh": "未配置编辑器。/editor <命令> 或设置 $EDITOR。",
    },
    "editor_set": {
        "en": "Editor set: {editor}",
        "zh": "编辑器已设为：{editor}",
    },
    "no_usage": {
        "en": "No usage recorded yet.",
        "zh": "还没有用量记录。",
    },
    "insights_days": {
        "en": "last {days} days",
        "zh": "最近 {days} 天",
    },
    "input_placeholder": {
        "en": "Type a message, / for commands...",
        "zh": "输入消息，/ 唤起命令…",
    },
    "welcome_tips": {
        "en": "/login · /model · Esc interrupt · /undo files · ↑ history",
        "zh": "/login 密钥 · /model 换模型 · Esc 中断 · /undo 回滚文件 · ↑ 历史",
    },
    "paste_collapsed": {
        "en": "Large paste saved to {path} ({n} chars) — the draft now references that file.",
        "zh": "大段粘贴已保存到 {path}（{n} 字），草稿里只保留文件引用。",
    },
    "paste_expand_failed": {
        "en": "A pasted block could not be re-read, so the message still carries its file "
              "reference instead of the content (the file may have been deleted).",
        "zh": "有一段粘贴内容无法读回，消息里仍保留文件引用而非正文（文件可能已被删除）。",
    },
    "rewind_usage": {
        "en": "Rewind needs a positive number of turns, e.g. /rewind 1 (got: {value}). "
              "Nothing was dropped.",
        "zh": "/rewind 需要一个正整数轮数，例如 /rewind 1（收到：{value}）。未删除任何内容。",
    },
    "tool_interrupted": {
        "en": "(interrupted)",
        "zh": "（已中断）",
    },
    "settings_hook_summary": {
        "en": "Persist a lifecycle hook for {event} (runs a shell command on every "
              "matching event)",
        "zh": "为 {event} 持久化生命周期钩子（每次匹配事件都会执行该 shell 命令）",
    },
    "settings_hook_preview_clear": {
        "en": "(clear this hook)",
        "zh": "（清除该钩子）",
    },
    "settings_hook_denied": {
        "en": "Hook for {event} was not stored.",
        "zh": "{event} 的钩子未写入。",
    },
    "settings_hook_unchanged": {
        "en": "Hook for {event} is unchanged.",
        "zh": "{event} 的钩子没有变化。",
    },
    "settings_network_summary": {
        "en": "Change the outbound network policy from {old} to {new}",
        "zh": "把出站网络策略从 {old} 改为 {new}",
    },
    "settings_network_denied": {
        "en": "Network policy unchanged.",
        "zh": "网络策略未改变。",
    },
    "search_running": {
        "en": "Searching history for {query}… (indexing new sessions)",
        "zh": "正在搜索历史：{query}…（会先索引新会话）",
    },
    "search_recent": {
        "en": "(recent)",
        "zh": "（最近）",
    },
    "search_open_title": {
        "en": "Open which session? (Esc keeps the list above)",
        "zh": "打开哪个会话？（Esc 保留上方列表）",
    },
    "search_already_open": {
        "en": "That session is already open.",
        "zh": "该会话已经打开。",
    },
    "density_current": {
        "en": "Chat spacing is {mode}. Use /density cozy or /density compact to change it.",
        "zh": "当前行距为 {mode}。用 /density cozy 或 /density compact 修改。",
    },
    "config_write_failed": {
        "en": "Could not write config.toml ({what}); the change is active for this "
              "session only and will be lost on restart.",
        "zh": "无法写入 config.toml（{what}）；本次改动只在当前会话生效，重启后会丢失。",
    },
    "tool_toggle_not_persisted": {
        "en": "The tool toggle applies to this session but was not saved to config.toml.",
        "zh": "工具开关在本次会话生效，但未能写入 config.toml。",
    },
    "compact_persist_failed": {
        "en": "Compaction summary could not be written; the history was NOT trimmed.",
        "zh": "压缩摘要未能落盘，历史记录并未被裁剪。",
    },
    "image_dropped_on_switch": {
        "en": "Dropped {n} attachment(s) staged for the previous session.",
        "zh": "已丢弃为上一个会话准备的 {n} 个附件。",
    },
    "picker_palette": {
        "en": "Command palette",
        "zh": "命令面板",
    },
    "picker_model": {
        "en": "Switch model",
        "zh": "切换模型",
    },
    "picker_sessions": {
        "en": "Restore session",
        "zh": "恢复会话",
    },
    "sessions_confirm_yes": {
        "en": "Yes, delete permanently",
        "zh": "确认永久删除",
    },
    "model_unknown": {
        "en": "unknown model: {model}\n  run `oaset models list` (or /model inside the TUI) for configured ids",
        "zh": "未知模型：{model}\n  运行 `oaset models list`（或 TUI 内 /model）查看已配置的模型 ID",
    },
    "config_parse_failed": {
        "en": "error: failed to parse {path}: {error}",
        "zh": "error: 配置解析失败 {path}: {error}",
    },
    "viewer_saved_to": {
        "en": "Full content written to: {path}",
        "zh": "完整内容已写入: {path}",
    },
    "mcp_not_started": {
        "en": "MCP: not started",
        "zh": "MCP: 未启动",
    },
    "agent_parent_model": {
        "en": "(parent model)",
        "zh": "（继承主模型）",
    },
    "none_marker": {
        "en": "(none)",
        "zh": "（无）",
    },
    "picker_theme": {
        "en": "Theme",
        "zh": "主题",
    },
    "picker_mode": {
        "en": "Permission mode",
        "zh": "权限模式",
    },
    "picker_tool": {
        "en": "Toggle tool",
        "zh": "启停工具",
    },
    "picker_ask_user": {
        "en": "AskUserQuestion",
        "zh": "向用户提问",
    },
    "update_usage": {
        "en": "Usage: /update [check|refresh|list|plan]",
        "zh": "用法: /update [check|refresh|list|plan]",
    },
    "picker_update": {
        "en": "Update center",
        "zh": "更新中心",
    },
    "arg_no_files": {
        "en": "No matching files in the workspace ({suffixes}).",
        "zh": "工作区内没有匹配的文件（{suffixes}）。",
    },
    "desktop_no_windows": {
        "en": "No desktop windows reported by the backend.",
        "zh": "后端未报告任何桌面窗口。",
    },
    "desktop_windows_header": {
        "en": "Desktop windows",
        "zh": "桌面窗口",
    },

    "update_missing": {
        "en": "Update center is not installed. Run `oaset update doctor` first.",
        "zh": "更新中心尚未安装。请先运行 oaset update doctor。",
    },
    "update_failed": {
        "en": "Update check failed: {err}",
        "zh": "更新检查失败：{err}",
    },
    "budget_exhausted_msg": {
        "en": "Token budget exhausted",
        "zh": "预算已用尽",
    },
    "budget_exhausted_detail": {
        "en": " (used {used} / {budget} tokens)",
        "zh": "（已用 {used} / {budget} tokens）",
    },
    "budget_exhausted_hint": {
        "en": " — further turns are blocked. /new for a fresh session, or raise the budget.",
        "zh": " —— 后续回合被拦截。/new 新会话，或调高预算。",
    },
    "image_usage": {
        "en": "Usage: /image <path> (or just /image to pick)",
        "zh": "用法: /image <图片路径>（或直接 /image 选择）",
    },
    "image_unsupported": {
        "en": "Model {model} does not support image input.",
        "zh": "当前模型 {model} 不支持图片输入。",
    },
    "image_attached": {
        "en": "Attached image {name} (sent with the next message).",
        "zh": "已附加图片 {name}（将随下一条消息发送）。",
    },
    "image_ignored_unsupported": {
        "en": "Model {model} does not support image input; attachment ignored.",
        "zh": "当前模型 {model} 不支持图片输入，已忽略附加图片。",
    },
    "no_bg_tasks": {
        "en": "No background tasks. The agent can start one with run_shell run_in_background.",
        "zh": "没有后台任务。Agent 可通过 run_shell 的 run_in_background 启动。",
    },
    "bg_tasks_header": {
        "en": "Background tasks:",
        "zh": "后台任务：",
    },
    "memory_empty": {
        "en": "Memory is empty. The agent persists knowledge with its memory tool (MEMORY.md / USER.md under ~/.oaset/memory/).",
        "zh": "记忆为空。Agent 会用 memory 工具持久化知识（~/.oaset/memory/ 下的 MEMORY.md / USER.md）。",
    },
    "memory_truncated": {
        "en": "… ({n} more characters — /memory all opens the full text)",
        "zh": "…（还有 {n} 个字符 —— /memory all 可查看全文）",
    },
    "memory_view_hint": {
        "en": "The full memory is long: /memory all pages through it with native selection.",
        "zh": "记忆内容较长：/memory all 可全屏翻阅并使用终端原生选择。",
    },
    "memory_viewer_title": {
        "en": "oAset memory (MEMORY.md / USER.md)",
        "zh": "oAset 记忆（MEMORY.md / USER.md）",
    },
    "no_cron_jobs": {
        "en": "No scheduled jobs. The agent can add them with its cron tool; fire them with 'oaset cron daemon'.",
        "zh": "没有定时任务。Agent 可用 cron 工具添加；用 oaset cron daemon 触发。",
    },
    "cron_header_line": {
        "en": "Scheduled jobs:",
        "zh": "定时任务：",
    },
    "arg_prompt": {
        "en": "{label}",
        "zh": "{label}",
    },
    "event_unknown_skipped": {
        "en": "Skipped an unknown event (missing type).",
        "zh": "已跳过未知事件（缺少 type 字段）。",
    },
    "event_type_unhandled": {
        "en": "Unhandled event type: {kind} (agent protocol drift?).",
        "zh": "未处理的事件类型：{kind}（可能存在协议漂移）。",
    },
    "iterations_exhausted_msg": {
        "en": "Iteration budget ({max}) reached — send 'continue' to keep going.",
        "zh": "迭代预算（{max}）已用尽 —— 发送 continue 可继续。",
    },
    "auto_compact_notice": {
        "en": "Context auto-compacted: ≈{before} → ≈{after} tokens.",
        "zh": "上下文自动压缩：≈{before} → ≈{after} tokens。",
    },
    "mode_notice_default": {
        "en": "Permission mode: writes and shell require confirmation.",
        "zh": "默认模式：写入与 shell 需要确认。",
    },
    "mode_notice_plan": {
        "en": "PLAN mode — read-only until you switch back (Shift-Tab).",
        "zh": "计划模式 —— 只读，Shift-Tab 切回。",
    },
    "mode_notice_auto": {
        "en": "AUTO mode — everything runs without confirmation. Careful!",
        "zh": "自动模式 —— 全部免确认执行，请谨慎！",
    },
    "ask_user_prefix": {
        "en": "❓ {question}",
        "zh": "❓ {question}",
    },
    # ------------------------------------------------------------ generic verbs
    "cancel": {"en": "Cancel", "zh": "取消"},
    "back": {"en": "Back", "zh": "返回"},
    "confirm": {"en": "Confirm", "zh": "确认"},
    "unknown_error": {"en": "unknown error", "zh": "未知错误"},
    "agent_failed_hint": {
        "en": "The turn was aborted. Check /status and the provider settings, then send your prompt again.",
        "zh": "该回合已中断。可用 /status 检查运行时与 provider 设置，然后重新发送。",
    },
    "model_unknown_hint": {
        "en": "Run /config to list configured models, then /model <id>.",
        "zh": "用 /config 查看已配置模型，再用 /model <id> 切换。",
    },
    "session_load_hint": {
        "en": "Run /sessions to browse available sessions.",
        "zh": "用 /sessions 浏览可恢复的会话。",
    },
    "reload_failed_hint": {
        "en": "Fix config.toml and run /reload again; the previous runtime keeps running.",
        "zh": "修正 config.toml 后再次 /reload；当前运行时保持可用。",
    },
    "export_failed_hint": {
        "en": "Check the output path is writable, or pass an explicit path: /export out.md",
        "zh": "确认输出路径可写，或显式指定路径：/export out.md",
    },
    "plan_mode_badge": {"en": "plan mode (read-only)", "zh": "plan 模式（只读）"},
    "auto_mode_badge": {"en": "⚡ auto-approve", "zh": "⚡ 自动放行"},
    # Widget BINDINGS labels: Textual needs a literal string, but the label is
    # user-visible (key hints / footer). Hardcoding Chinese in these put one
    # language into a table whose sibling entries were English.
    "binding_toggle": {"en": "Toggle", "zh": "展开/折叠"},    "sidebar_todos": {"en": "Todos", "zh": "计划"},
    "sidebar_no_todos": {
        "en": "(no todos — the agent maintains them for multi-step work)",
        "zh": "（暂无计划 —— 多步任务时由 agent 维护）",
    },
    "plan_meta": {
        "en": "plan {done}/{total}",
        "zh": "计划 {done}/{total}",
    },
    "thinking_tail": {
        "en": "…({n} earlier line(s) folded while streaming)",
        "zh": "…（流式期间已折叠前 {n} 行）",
    },
    "queue_badge": {"en": "queued {n}", "zh": "排队 {n}"},
    "bg_working": {"en": "bg:{n}", "zh": "后台:{n}"},
    "subagent_started": {"en": "[{agent}] started", "zh": "[{agent}] 已启动"},
    "subagent_tool": {"en": "[{agent}] {name}", "zh": "[{agent}] {name}"},
    "subagent_tool_mark": {"en": "[{agent}] {name} {mark}", "zh": "[{agent}] {name} {mark}"},
    "subagent_timeout": {"en": "[{agent}] timed out", "zh": "[{agent}] 已超时"},
    "subagent_error": {
        "en": "[{agent}] failed: {error}",
        "zh": "[{agent}] 失败：{error}",
    },
    "subagent_done": {"en": "[{agent}] done", "zh": "[{agent}] 已完成"},
    "pager_hint": {
        "en": "space next · b prev · g/G first/last · q quit",
        "zh": "空格 下页 · b 上页 · g/G 首/末 · q 退出",
    },
    "pager_hint_single": {
        "en": "Enter/q return", "zh": "Enter/q 返回",
    },
    "paste_native_hint": {
        "en": "System clipboard unavailable in this console — use the terminal's native paste (usually Ctrl+V / Cmd+V); text arrives directly",
        "zh": "当前终端未提供系统剪贴板——请用终端原生粘贴（通常 Ctrl+V / Cmd+V），文本会直接送达",
    },
    "agents_builtin_undeletable": {
        "en": "cannot delete built-in agent: {name}",
        "zh": "内置子代理不可删除：{name}",
    },
    "skills_delete_scope_dir": {
        "en": "(the ENTIRE skill directory is removed, not just SKILL.md)",
        "zh": "（将删除整个技能目录，不只 SKILL.md）",
    },
    "market_stale_note": {
        "en": "catalog refresh fell back to the local cache ({note}) — versions may be stale",
        "zh": "目录刷新失败已回退本地缓存（{note}）——版本信息可能过期",
    },
    "tool_duplicate_id": {
        "en": "duplicate tool-call id from provider — this card was superseded",
        "zh": "provider 重复下发了同一调用 id——此卡片已被取代",
    },
    "queue_dropped_switch": {
        "en": "a queued message was for the previous session — not sent",
        "zh": "有一条排队消息属于上一个会话——未发送",
    },
    "queue_dropped_exit": {
        "en": "exiting: dropped {n} queued message(s)",
        "zh": "退出：已丢弃 {n} 条排队消息",
    },
    "steer_not_delivered": {
        "en": "{n} mid-turn steer(s) were never consumed by the turn — dropped (send again if still relevant)",
        "zh": "有 {n} 条中途注入未被本轮消费——已丢弃（如仍需要请重新发送）",
    },
    "esc_again_hint": {
        "en": "panel closed — press Esc again to interrupt the turn",
        "zh": "面板已关闭——再按 Esc 可中断当前回合",
    },
    "image_cleared": {
        "en": "dropped {n} pending image attachment(s)",
        "zh": "已取消 {n} 个待发送图片",
    },
    "image_pending": {
        "en": "{n} image(s) waiting for the next message — /image clear to drop them",
        "zh": "有 {n} 张图片等待随下一条消息发送——/image clear 可取消",
    },
    "image_pending_now": {
        "en": "{n} pending — /image list|clear",
        "zh": "共 {n} 张待发——/image list|clear",
    },
    "plug_reserved_command": {
        "en": "plugin {name}: command /{command} is reserved by a built-in and was refused",
        "zh": "插件 {name}：命令 /{command} 与内建命令冲突，已拒绝注册",
    },
    "plug_reserved_tool": {
        "en": "plugin {name}: tool '{tool}' would override a built-in tool and was refused",
        "zh": "插件 {name}：工具 '{tool}' 会覆盖内建工具，已拒绝注册",
    },
    "plug_limit_reached": {
        "en": "plugin limit reached ({limit}); remaining plugins were not loaded",
        "zh": "插件数量已达上限（{limit}），其余插件未加载",
    },
    "plugins_failed_header": {
        "en": "failed to load:", "zh": "加载失败：",
    },
    "worktree_io_failed": {
        "en": "git worktree failed: {err}",
        "zh": "git worktree 执行失败：{err}",
    },
    "export_failed_reason": {
        "en": "export failed: {err}",
        "zh": "导出失败：{err}",
    },
    "welcome_quick_start": {"en": "Getting started", "zh": "快速上手"},
    "welcome_stats_7d": {"en": "Last 7 days", "zh": "近 7 天"},
    "welcome_stats_line": {"en": "{tokens} tok · {turns} turns",
                           "zh": "累计 {tokens} tok · {turns} 轮"},
    "welcome_branch": {"en": "Branch", "zh": "分支"},
    "welcome_fun_1": {"en": "Ctrl+K is the command palette — /help is just the poster.",
                      "zh": "Ctrl+K 才是命令面板——/help 只是海报。"},
    "welcome_fun_2": {"en": "F2 opens the whole message; drag inside it to copy a stack trace.",
                      "zh": "F2 放大整条消息，在里面拖选就能复制堆栈。"},
    "welcome_fun_3": {"en": "Esc interrupts; Ctrl+S steers mid-turn.",
                      "zh": "Esc 中断；Ctrl+S 可中途改道。"},
    "welcome_fun_4": {"en": "Every change is checkpointed — /undo files brings them back.",
                      "zh": "每次改文件都有检查点——/undo files 可回滚。"},
    "welcome_fun_5": {"en": "Click a row above: commands fill the composer, sessions restore.",
                      "zh": "点上面任一行：命令填入输入框，会话直接恢复。"},
    "welcome_fun_6": {"en": "Ask it to /worktree new and it edits in an isolated checkout.",
                      "zh": "试试 /worktree new：在隔离检出里改代码，主目录不动。"},
    "welcome_fun_7": {"en": "Everything here stays on this machine — the network is pull-only by default.",
                      "zh": "这里的一切都留在本机——网络默认只收不发。"},
    "welcome_fun_8": {"en": "/evidence on makes 'done' mean receipts, not vibes.",
                      "zh": "/evidence on 之后，「做完了」要交回执，不靠感觉。"},
    "welcome_fun_9": {"en": "It runs your tests before it claims victory. Every time.",
                      "zh": "它宣称胜利前会先跑你的测试。每次都会。"},
    "welcome_fun_10": {"en": "Try 'every 20m' with /cron — it waters your builds while you're away.",
                       "zh": "用 /cron 配 'every 20m'——你不在时它替你守着构建。"},
    "welcome_fun_11": {"en": "Paste a screenshot — it reads pixels, not just text.",
                       "zh": "直接贴截图——它看的是像素，不只是文字。"},
    "welcome_fun_12": {"en": "oaset offline-check tells you what survives a plane.",
                       "zh": "oaset offline-check 告诉你飞机上还能用什么。"},
    "welcome_click_hint": {
        "en": "click a command to fill it · click a session to restore",
        "zh": "点击命令行直接填入 · 点击会话行恢复",
    },
    "welcome_restoring": {
        "en": "restoring session {sid} …",
        "zh": "正在恢复会话 {sid} …",
    },
    "welcome_recent": {"en": "Recent sessions", "zh": "最近会话"},
    "welcome_untitled": {"en": "(untitled)", "zh": "（未命名）"},
    "welcome_qs_ask": {"en": "ask a question — Enter sends",
                       "zh": "直接提问——Enter 发送"},
    "welcome_qs_help": {"en": "every command and shortcut", "zh": "全部命令与快捷键"},
    "welcome_qs_model": {"en": "pick or add a model", "zh": "选择或添加模型"},
    "welcome_qs_login": {"en": "store an API key", "zh": "配置 API 密钥"},
    "welcome_qs_sessions": {"en": "restore an earlier session", "zh": "恢复历史会话"},
    "welcome_qs_undo": {"en": "roll back file changes", "zh": "回滚文件改动"},
    "welcome_qs_keys": {"en": "command palette · Ctrl+O folds tool output",
                        "zh": "命令面板 · Ctrl+O 折叠工具输出"},
    "welcome_qs_esc": {"en": "interrupt the running turn", "zh": "中断当前回合"},
    "first_setup_title": {
        "en": "No model configured yet — set one up now?",
        "zh": "还没有配置模型——现在配置吗？",
    },
    "first_setup_now": {
        "en": "Set up now (about a minute)",
        "zh": "立即配置（约 1 分钟）",
    },
    "first_setup_later": {
        "en": "Later — /login or /model any time",
        "zh": "稍后——随时可用 /login 或 /model",
    },
    "doctor_config_created": {
        "en": "  (created with defaults — first launch)",
        "zh": "  （首次运行，已生成默认配置）",
    },
    "doctor_key_hint": {
        "en": "run `oaset setup` (CLI wizard) or /login in the TUI",
        "zh": "运行 `oaset setup`（CLI 向导）或在 TUI 内 /login",
    },
    "goal_budget_negative": {
        "en": "budget must be a positive number of k-tokens (e.g. /goal budget 50)",
        "zh": "预算必须是正数（千 token 计，例如 /goal budget 50）",
    },
    "tool_body_clamped": {
        "en": "… {n} more characters — full text kept for copy (Ctrl+Shift+C / /copy tools)",
        "zh": "…… 另有 {n} 字符未显示——全文保留在复制中（Ctrl+Shift+C / /copy tools）",
    },
    "paste_inline_fallback": {
        "en": "Could not write the paste file ({n} chars) — inserted into the draft instead",
        "zh": "粘贴文件写入失败（{n} 字符）——已改为直接插入草稿",
    },
    "error_more": {"en": "/errors for detail", "zh": "/errors 看详情"},
    "error_detail_title": {"en": "error detail", "zh": "错误详情"},
    "no_pinned_error": {
        "en": "No pinned error — nothing to expand.",
        "zh": "当前没有钉住的错误。",
    },
    "cmd_errors": {
        "en": "Show the pinned error's full detail in the viewer",
        "zh": "在查看器中展开钉住错误的完整详情",
    },
    "cmd_density": {"en": "Chat spacing: compact | cozy", "zh": "聊天密度：compact | cozy"},
    "density_usage": {
        "en": "Usage: /density compact|cozy",
        "zh": "用法：/density compact|cozy",
    },
    "density_set": {
        "en": "Chat density: {mode} (saved).",
        "zh": "聊天密度：{mode}（已保存）。",
    },
    "plan_complete": {
        "en": "Plan complete — all {n} step(s) done.",
        "zh": "计划完成——全部 {n} 项已完成。",
    },
    "quit_confirm": {"en": "Press Ctrl-C again to exit", "zh": "再次按 Ctrl-C 退出"},
    "inject_nothing": {"en": "Nothing to inject", "zh": "没有可注入的内容"},
    "suggest_more": {
        "en": "↑/↓ browse all {total} commands",
        "zh": "↑/↓ 浏览全部 {total} 条命令",
    },
    "status_header": {"en": "Session and runtime status", "zh": "会话与运行时状态"},
    "status_source": {"en": "source", "zh": "运行来源"},
    "status_session": {"en": "session", "zh": "会话"},
    "status_model": {"en": "model", "zh": "模型"},
    "status_mode": {"en": "mode", "zh": "模式"},
    "status_context": {"en": "context", "zh": "上下文"},
    "status_tools": {"en": "tools", "zh": "工具"},
    "status_mcp": {"en": "mcp", "zh": "MCP"},
    "status_cwd": {"en": "cwd", "zh": "工作目录"},
    "status_untitled": {"en": "(untitled)", "zh": "（无标题）"},
    "status_network_language": {
        "en": "network: {network}   language: {language}",
        "zh": "网络：{network}   语言：{language}",
    },
    "lang_pick_title": {
        "en": "Language / 语言",
        "zh": "语言 / Language",
    },
    "lang_pick_zh": {
        "en": "中文 (Chinese)",
        "zh": "中文",
    },
    "lang_pick_en": {
        "en": "English",
        "zh": "English",
    },
    "lang_set": {
        "en": "Language: English. Switch anytime with /language zh",
        "zh": "界面语言：中文。随时可用 /language en 切换",
    },
    "lang_usage": {
        "en": "Usage: /language zh|en",
        "zh": "用法：/language zh|en",
    },
    "cmd_language": {
        "en": "Switch UI language (zh / en)",
        "zh": "切换界面语言（中文 / English）",
    },
    "effect_language": {
        "en": "Write [ui] language to config.toml and refresh the chrome",
        "zh": "写回 config.toml 的 [ui] language 并刷新界面文案",
    },
    "status_tools_detail": {
        "en": "{active} active, {disabled} disabled, {allowed} always-allowed",
        "zh": "{active} 启用，{disabled} 停用，{allowed} 永久放行",
    },
    "status_mcp_detail": {"en": "{count} adapters", "zh": "{count} 个适配器"},
    "undo_ok": {
        "en": "Undid checkpoint #{n}: {count} file(s) restored.",
        "zh": "已撤销检查点 #{n}：恢复 {count} 个文件。",
    },
    "usage_line": {
        "en": "Usage this session: {entries} billed turn(s) · prompt {prompt} tok · completion {completion} tok · total {total} tok",
        "zh": "本会话用量：{entries} 个计费回合 · 输入 {prompt} tok · 输出 {completion} tok · 合计 {total} tok",
    },
    "no_subagents": {
        "en": "No sub-agents defined. Add Markdown files to ~/.oaset/agents/<name>.md (optional header: name/description/tools). The task tool falls back to the built-in 'general' agent.",
        "zh": "没有定义子 agent。可在 ~/.oaset/agents/<name>.md 添加 Markdown 文件（可选头部：name/description/tools）。task 工具会退回到内置的 general agent。",
    },
    "search_pull_hint": {
        "en": "The agent can pull any of these into context with its search_history tool.",
        "zh": "agent 可以用 search_history 工具把其中任意结果拉入上下文。",
    },
    "no_skills": {
        "en": "No extra skills. Built-in playbooks ship with oAset; add more under ~/.oaset/skills/<name>/SKILL.md or ./.oaset/skills/.",
        "zh": "没有额外技能。内置手册随 oAset 提供；可在 ~/.oaset/skills/<name>/SKILL.md 或 ./.oaset/skills/ 添加。",
    },
    "editor_current": {"en": "{editor}  —  /editor <command>", "zh": "{editor}  ——  /editor <command>"},
    "config_header": {"en": "config: {path}", "zh": "配置：{path}"},
    "config_cwd": {"en": "cwd: {cwd}", "zh": "工作目录：{cwd}"},
    "config_providers": {"en": "providers:", "zh": "providers："},
    "config_models": {"en": "models: {models}", "zh": "models：{models}"},
    "login_banner": {
        "en": "[b]Log in to {provider}[/b] — paste the API key below and press enter (stored locally only) · [b]esc[/b] cancels",
        "zh": "[b]登录 {provider}[/b] —— 在下方输入框粘贴 API key 后回车保存（仅存本地） · [b]esc[/b] 取消",
    },
    "permission_always": {
        "en": "allow for this session",
        "zh": "本会话内放行",
    },
    "permission_always_dir": {
        "en": "allow this directory for this session",
        "zh": "该目录本会话内放行",
    },
    # ------------------------------------------------------- self-maintenance
    "selfmaint_disabled_title": {
        "en": "Experience Program is off",
        "zh": "体验计划未开启",
    },
    "selfmaint_enable_option": {
        "en": "Enable: let the TUI self-maintain with a fixed token budget",
        "zh": "开启：允许 TUI 用固定 token 预算做自维护",
    },
    "selfmaint_enabled_readonly": {
        "en": "Experience Program enabled (read-only by default; allow_apply must be turned on in config).",
        "zh": "体验计划已开启（默认只读；allow_apply 需在配置中显式打开）。",
    },
    "selfmaint_enabled": {
        "en": "Experience Program enabled.",
        "zh": "体验计划已开启。",
    },
    "selfmaint_running": {
        "en": "Experience Program running: check → plan → compat probe → budget-capped turn…",
        "zh": "体验计划运行中：check → plan → 兼容探测 → 受限模型回合…",
    },
    "selfmaint_refused": {
        "en": "Self-maintenance refused [{code}]: {err}",
        "zh": "自维护被拒绝 [{code}]：{err}",
    },
    "selfmaint_cancelled": {
        "en": "Self-maintenance cancelled — nothing was installed.",
        "zh": "自维护已取消 —— 未安装任何补丁。",
    },
    "selfmaint_failed": {
        "en": "Self-maintenance failed: {err}",
        "zh": "自维护失败：{err}",
    },
    # --------------------------------------------------------- file/image forms
    "picker_custom_path": {
        "en": "✎ Enter a path manually…",
        "zh": "✎ 手动输入路径…",
    },
    "custom_path_title": {
        "en": "Path to use",
        "zh": "请输入路径",
    },
    "image_not_found": {
        "en": "{path} is not an existing file — pick again.",
        "zh": "{path} 不是已存在的文件 —— 请重新选择。",
    },
    "image_bad_suffix": {
        "en": "{path} is not a supported image ({suffixes}) — pick again.",
        "zh": "{path} 不是受支持的图片格式（{suffixes}）—— 请重新选择。",
    },
    "image_too_large": {
        "en": "{path} is {size}, over the {limit} limit — pick another image.",
        "zh": "{path} 为 {size}，超过 {limit} 上限 —— 请另选图片。",
    },
    "image_size_unknown": {
        "en": "(size unavailable)",
        "zh": "（无法读取大小）",
    },
    # ------------------------------------------------------ pickers & approval
    "picker_position": {
        "en": "item {index} of {total}",
        "zh": "第 {index} / {total} 项",
    },
    "picker_empty": {
        "en": "nothing to choose here",
        "zh": "此处没有可选项",
    },
    "picker_footer": {
        "en": "↑/↓ move · enter choose · q or esc cancel (nothing runs)",
        "zh": "↑/↓ 移动 · enter 确认 · q 或 esc 取消（不会执行）",
    },
    "permission_title": {
        "en": "[b][yellow]Permission required — {badge}[/][/]",
        "zh": "[b][yellow]需要确认 —— {badge}[/][/]",
    },
    "permission_keys": {
        "en": "[b]y[/b] allow once · [b]a[/b] {always} · [b]n[/b] deny (esc/timeout = deny)",
        "zh": "[b]y[/b] 允许一次 · [b]a[/b] {always} · [b]n[/b] 拒绝（esc/超时即拒绝）",
    },
    # ------------------------------------------------------- command palette
    "palette_title": {
        "en": "Command palette",
        "zh": "命令面板",
    },
    "palette_recent": {
        "en": "Recently used",
        "zh": "最近使用",
    },
    "palette_filter_hint": {
        "en": "(type to filter by name, description, category or alias)",
        "zh": "（输入以按名称/描述/分类/别名过滤）",
    },
    "palette_empty": {
        "en": "No command matches “{query}” — Backspace to widen, esc to clear.",
        "zh": "没有匹配“{query}”的命令 —— Backspace 放宽，esc 清空。",
    },
    "palette_position": {
        "en": "{index}/{total}",
        "zh": "{index}/{total}",
    },
    "palette_details": {
        "en": "category: {category} · source: {source} · risk: {risk} · aliases: {aliases}",
        "zh": "分类：{category} · 来源：{source} · 风险：{risk} · 别名：{aliases}",
    },
    "palette_footer": {
        "en": "type filter · ↑/↓ move · enter choose · esc clear/back · q cancel",
        "zh": "输入过滤 · ↑/↓ 移动 · enter 选择 · esc 清空/返回 · q 取消",
    },
    "confirm_title": {
        "en": "[b][yellow]Confirm — {badge}[/][/]",
        "zh": "[b][yellow]请确认 —— {badge}[/][/]",
    },
    "confirm_footer": {
        "en": "[b]enter[/b] or [b]y[/b] run · [b]esc[/b]/[b]q[/b]/[b]n[/b] back (nothing runs)",
        "zh": "[b]enter[/b] 或 [b]y[/b] 执行 · [b]esc[/b]/[b]q[/b]/[b]n[/b] 返回（不会执行）",
    },
    "palette_unknown_command": {
        "en": "/{name} has no handler here (plugin not loaded?) — back to the palette.",
        "zh": "/{name} 在当前会话没有处理函数（插件未加载？）—— 返回命令面板。",
    },
    "palette_choice_custom": {
        "en": "✎ Type the value myself…",
        "zh": "✎ 手动输入…",
    },
    # ------------------------------------------------------------ capability ui
    "update_action_apply": {
        "en": "apply is a separate, approved action — run `oaset update apply <name>`.",
        "zh": "apply 是单独的审批动作 —— 请运行 `oaset update apply <name>`。",
    },
    "update_rollback_prompt": {
        "en": "Roll back which apply? Enter a target name, or leave empty for the last one.",
        "zh": "回滚哪次 apply？输入目标名，留空表示最近一次。",
    },
    "update_history_empty": {
        "en": "No update history yet.",
        "zh": "暂无更新历史。",
    },
    "picker_update_apply": {
        "en": "Install which catalog entry?",
        "zh": "要安装哪个目录条目？",
    },
    "update_nothing_to_apply": {
        "en": "Nothing to apply — the catalog plan has no compatible target.",
        "zh": "没有可安装的目标 —— 目录计划里没有兼容条目。",
    },
    "update_check_first": {
        "en": "Run /update refresh (or `oaset update refresh`) to fetch the catalog first.",
        "zh": "先运行 /update refresh（或 `oaset update refresh`）刷新目录。",
    },
    "update_apply_summary": {
        "en": "Update apply will install: {targets}",
        "zh": "更新 apply 将安装：{targets}",
    },
    "update_apply_preview": {
        "en": "来源为目录中带 sha256 校验的条目；事务化安装，可用 /update rollback 撤销",
        "zh": "来源为目录中带 sha256 校验的条目；事务化安装，可用 /update rollback 撤销",
    },
    "update_apply_denied": {
        "en": "Apply denied — nothing was installed.",
        "zh": "apply 被拒绝 —— 未安装任何内容。",
    },
    "update_rollback_denied": {
        "en": "Rollback denied — nothing was restored.",
        "zh": "rollback 被拒绝 —— 未恢复任何内容。",
    },
    "update_rollback_last": {
        "en": "leave empty for the most recent apply",
        "zh": "留空表示最近一次 apply",
    },
    "update_rollback_summary": {
        "en": "Roll back {target}",
        "zh": "回滚 {target}",
    },
    "reload_provider_failed": {
        "en": "reload: provider rebuild failed ({err}); keeping the previous provider chain.",
        "zh": "reload：provider 重建失败（{err}）；继续使用原有 provider 链。",
    },
    "reload_deferred_close": {
        "en": "A turn is streaming: the old provider client closes as soon as it finishes.",
        "zh": "有回合正在流式输出：旧 provider 客户端会在该回合结束后关闭。",
    },
    "mcp_reload_draining": {
        "en": "A turn was running: it has been cancelled before restarting MCP servers.",
        "zh": "有回合正在运行：已先取消该回合，再重启 MCP 服务器。",
    },
    "mcp_reload_draining_hint": {
        "en": "The prompt is not lost — the transcript keeps the partial reply; send it again if you need it.",
        "zh": "输入不会丢失 —— 记录里保留部分回复；需要的话可以重新发送。",
    },
    "mcp_reload_busy": {
        "en": "A turn is still running — MCP servers were not restarted.",
        "zh": "仍有回合在运行 —— 未重启 MCP 服务器。",
    },
    "mcp_reload_busy_hint": {
        "en": "Press esc (or ctrl+c) to cancel the turn, then run /reload-mcp again.",
        "zh": "按 esc（或 ctrl+c）取消该回合，再执行 /reload-mcp。",
    },
    "update_history_title": {
        "en": "Update transactions (newest last)",
        "zh": "更新事务记录（最新在最后）",
    },
    "update_error_hint": {
        "en": "Run /update history to inspect the recorded transactions, then retry or roll back.",
        "zh": "用 /update history 查看已记录的事务，再重试或回滚。",
    },
    "update_rollback_ok": {
        "en": "Rollback finished. Restart oaset to load the restored version.",
        "zh": "回滚完成。重启 oaset 以加载恢复后的版本。",
    },
    "maint_report_title": {
        "en": "[b]Experience program · self-maintenance report[/b]",
        "zh": "[b]体验计划 · 自维护报告[/b]",
    },
    "maint_plan_line": {
        "en": "plan: {plan}",
        "zh": "计划: {plan}",
    },
    "maint_applied_line": {
        "en": "patches applied: {names}",
        "zh": "已并入补丁: {names}",
    },
    "maint_core_applied": {
        "en": "core self-update applied (transactional; undo with oaset update rollback)",
        "zh": "core 自更新已并入（事务化安装，可 oaset update rollback 撤销）",
    },
    "maint_restart_line": {
        "en": "✦ restart oAset for the new version to take effect (never restarts automatically)",
        "zh": "✦ 需要重启 oAset 后新版本才生效（不会自动重启）",
    },
    "maint_core_reported": {
        "en": "core self-update recommendation: reported only, not executed (allow_apply off or refused)",
        "zh": "core 自更新建议: 已报告，未执行（allow_apply 未开启或被拒绝）",
    },
    "maint_blocked_line": {
        "en": "apply not run: {reasons}",
        "zh": "未执行 apply: {reasons}",
    },
    "maint_usage_line": {
        "en": "token usage: {used} / {budget}",
        "zh": "token 用量: {used} / {budget}",
    },
    "maint_budget_float": {
        "en": "token budget must be an integer; got {raw!r}",
        "zh": "token 预算必须是整数，收到 {raw!r}",
    },
    "maint_budget_float_hint": {
        "en": "decimals would be silently truncated; round it explicitly first",
        "zh": "小数会被静默截断，请显式取整后再传入",
    },
    "maint_budget_int_hint": {
        "en": "set [maintenance] budget_tokens to an integer between 1 and {max}",
        "zh": "请在 [maintenance] budget_tokens 设置 1 到 {max} 之间的整数",
    },
    "maint_budget_positive": {
        "en": "token budget must be positive; got {budget}",
        "zh": "token 预算必须是正数，收到 {budget}",
    },
    "maint_budget_positive_hint": {
        "en": "a zero/negative budget removes the turn's cap; refusing to run",
        "zh": "预算为 0 或负数会让回合失去上限，已拒绝运行",
    },
    "maint_budget_too_large": {
        "en": "token budget {budget} exceeds the cap {max}",
        "zh": "token 预算 {budget} 超过上限 {max}",
    },
    "maint_budget_too_large_hint": {
        "en": "self-maintenance is a fixed-budget feature; lower budget_tokens",
        "zh": "自维护是固定预算的体验功能，请调低 budget_tokens",
    },
    "maint_blocked_phases": {
        "en": "earlier phases failed: {failed}",
        "zh": "前置阶段失败: {failed}",
    },
    "maint_blocked_budget": {
        "en": "token budget exhausted; the plan is not trustworthy",
        "zh": "token 预算耗尽，计划不可信",
    },
    "maint_blocked_no_plan": {
        "en": "no parseable maintenance plan",
        "zh": "没有可解析的维护计划",
    },
    "maint_blocked_compat": {
        "en": "compatibility probe failed",
        "zh": "兼容性探测未通过",
    },
    "maint_disabled": {
        "en": "the experience program is disabled (maintenance.enabled=false); refusing to run.",
        "zh": "体验计划未开启（maintenance.enabled=false），已拒绝运行。",
    },
    "maint_disabled_hint": {
        "en": "run /selfmaint in the TUI to enable it, or set [maintenance] enabled = true",
        "zh": "在 TUI 中运行 /selfmaint 选择开启，或设置 [maintenance] enabled = true",
    },
    "maint_cancelled_error": {
        "en": "cancelled: the maintenance run was aborted; no patches were installed.",
        "zh": "已取消：维护运行被中止，未安装任何补丁。",
    },
    "maint_cancelled_phase": {
        "en": "run cancelled",
        "zh": "运行被取消",
    },
    "maint_check_detail": {
        "en": "{count} entries (cached={cached})",
        "zh": "{count} 条目（缓存={cached}）",
    },
    "maint_check_errors": {
        "en": "; errors {errors}",
        "zh": "；错误 {errors}",
    },
    "maint_plan_detail": {
        "en": "{count} compatible targets",
        "zh": "{count} 个兼容目标",
    },
    "maint_plan_skipped": {
        "en": "; skipped {count}",
        "zh": "；跳过 {count}",
    },
    "maint_no_plan_json": {
        "en": "the model produced no parseable maintenance-plan JSON",
        "zh": "模型未产出可解析的维护计划 JSON",
    },
    "maint_apply_blocked": {
        "en": "not run: {reasons}",
        "zh": "未执行：{reasons}",
    },
    "maint_readonly": {
        "en": "read-only mode: plan reported, nothing installed",
        "zh": "只读模式：计划已报告，未安装任何补丁",
    },
    "maint_no_allow_apply": {
        "en": "allow_apply not enabled: plan reported, nothing installed",
        "zh": "配置未开启 allow_apply：计划已报告，未安装任何补丁",
    },
    "maint_apply_no_plan": {
        "en": "apply phase is missing the maintenance plan",
        "zh": "apply 阶段缺少维护计划",
    },
    "maint_unknown_targets": {
        "en": "unknown targets in the plan rejected: {names}",
        "zh": "计划中的未知目标已拒绝: {names}",
    },
    "maint_core_missing_entry": {
        "en": "plan requests self_update=core but the catalog has no core entry; skipped",
        "zh": "计划要求 self_update=core，但更新目录中没有 core 条目，已跳过",
    },
    "maint_apply_empty": {
        "en": "no installable targets in the plan",
        "zh": "计划中没有可安装的目标",
    },
    "maint_apply_detail": {
        "en": "{count} patches applied",
        "zh": "并入 {count} 个补丁",
    },
    "maint_apply_core_note": {
        "en": "(includes the core self-update; effective after restart)",
        "zh": "（含 core 自更新，重启后生效）",
    },
    "maint_approve_cli": {
        "en": "explicit CLI allow_apply ({targets})",
        "zh": "CLI 显式 allow_apply（{targets}）",
    },
    "maint_approve_summary": {
        "en": "the experience program will install {count} patches: {targets}",
        "zh": "体验计划将安装 {count} 个补丁: {targets}",
    },
    "maint_approve_preview": {
        "en": "source: hash-checked catalog entries; undo with oaset update rollback",
        "zh": "来源: 更新目录的 hash 校验条目；可用 oaset update rollback 撤销",
    },
    "maint_approve_failed": {
        "en": "approval failed; installation refused: {kind}: {exc}",
        "zh": "审批失败，已拒绝安装: {kind}: {exc}",
    },
    "maint_approve_broken": {
        "en": "approval channel broken; treating as denial",
        "zh": "审批通道异常，按拒绝处理",
    },
    "maint_approve_ok": {
        "en": "approved installing {targets}",
        "zh": "已授权安装 {targets}",
    },
    "maint_approve_denied": {
        "en": "approval denied: no patches installed",
        "zh": "审批被拒绝：未安装任何补丁",
    },
    "maint_approve_denied_detail": {
        "en": "approval denied ({decision})",
        "zh": "审批被拒绝（{decision}）",
    },
    "maint_model_missing": {
        "en": "the default model is not configured",
        "zh": "默认模型不在配置中",
    },
    "maint_compat_ok": {
        "en": "{model} connectivity OK",
        "zh": "{model} 连通正常",
    },
    "maint_budget_exhausted": {
        "en": "budget exhausted ({used}/{budget} tokens)",
        "zh": "预算耗尽（{used}/{budget} tokens）",
    },
    "maint_turn_truncated": {
        "en": "— the turn was truncated; no reliable plan this run",
        "zh": "—— 回合被截断，本次不产生可靠计划",
    },
    "maint_turn_cancelled": {
        "en": "turn cancelled: no maintenance plan produced",
        "zh": "回合被取消：未产出维护计划",
    },
    "maint_cancelled_tag": {
        "en": "cancelled",
        "zh": "已取消",
    },
    "br_download_dest_missing": {
        "en": "download destination directory does not exist: {dest}",
        "zh": "下载目标目录不存在: {dest}",
    },
    "br_upload_missing": {
        "en": "file to upload does not exist: {path}",
        "zh": "要上传的文件不存在: {path}",
    },
    "br_upload_empty": {
        "en": "no files to upload",
        "zh": "未提供要上传的文件",
    },
    "br_upload_too_many": {
        "en": "at most {max} files per upload",
        "zh": "一次最多上传 {max} 个文件",
    },
    "br_exe_missing": {
        "en": "no Edge/Chrome executable found (the CDP backend needs one)",
        "zh": "未找到 Edge/Chrome 浏览器可执行文件（CDP 后端需要其一）",
    },
    "br_ws_failed": {
        "en": "CDP WebSocket connection failed: {exc}",
        "zh": "CDP WebSocket 连接失败: {exc}",
    },
    "br_no_devtools": {
        "en": "the browser printed no DevTools address (launch failed or timed out)",
        "zh": "浏览器未输出 DevTools 调试地址（启动失败或超时）",
    },
    "br_no_page_target": {
        "en": "no usable page debug target: {err}",
        "zh": "未找到可用的页面调试目标: {err}",
    },
    "br_download_not_complete": {
        "en": "download did not complete: {url}",
        "zh": "下载未完成: {url}",
    },
    "br_ws_closed": {
        "en": "browser connection dropped: {exc}",
        "zh": "浏览器连接中断: {exc}",
    },
    "br_download_no_file": {
        "en": "download event completed but the file is missing on disk",
        "zh": "下载事件完成但磁盘上找不到文件",
    },
    "br_download_cancelled": {
        "en": "download was cancelled by the browser or the site",
        "zh": "下载被浏览器或站点取消",
    },
    "br_download_timeout": {
        "en": "download did not finish within {timeout}s: {name}",
        "zh": "下载在 {timeout}s 内未完成: {name}",
    },
    "br_no_file_input": {
        "en": "no file input element on the page: {handle}",
        "zh": "页面上找不到文件输入元素: {handle}",
    },
    "desk_no_backend": {
        "en": "no Windows UIA backend registered: this build ships only the protocol and approval layer;\ndesktop actions stay unavailable until a real backend registers",
        "zh": "未注册 Windows UIA 后端：当前构建仅提供协议与审批层，\n真实后端接入前桌面操作不可用",
    },
    "uia_windows_only": {
        "en": "the UIA backend only exists on Windows",
        "zh": "UIA 后端仅存在于 Windows",
    },
    "vis_screenshot": {
        "en": "⚠ VISION screenshot (coordinate-fallback observation)",
        "zh": "⚠ VISION 截图（坐标回退观测）",
    },
    "vis_click": {
        "en": "⚠ VISION click ({x},{y}) display={display_id} @{scale}x",
        "zh": "⚠ VISION 坐标点击 ({x},{y}) display={display_id} @{scale}x",
    },
    "vis_click_window": {
        "en": " window {handle}",
        "zh": " 窗口 {handle}",
    },
    "vis_type": {
        "en": "⚠ VISION typed {count} chars into the focused control",
        "zh": "⚠ VISION 键入 {count} 字符到当前焦点控件",
    },
    "plug_cap_denied": {
        "en": "plugin {name}: permission '{capability}' not declared; call refused (declared: {declared})",
        "zh": "plugin {name}: 权限未声明 '{capability}'，调用已拒绝（声明的权限: {declared}）",
    },
    "plug_none": {
        "en": "none",
        "zh": "无",
    },
    "plug_untrusted": {
        "en": "plugin {name}: project plugin not trusted (or changed); skipped. To confirm: oaset trust-plugin {name}",
        "zh": "plugin {name}: 项目级插件未信任（或已变更），已跳过。确认可信后运行: oaset trust-plugin {name}",
    },
    "plug_bad_manifest": {
        "en": "plugin {name}: invalid OASET_MANIFEST ({exc}) — manifest ignored, load continues",
        "zh": "plugin {path}: OASET_MANIFEST 无效（{exc}）— 已忽略 manifest，继续加载",
    },
    "plug_incompatible": {
        "en": "plugin {name}: requires oaset {requires}; current version does not satisfy it — skipped",
        "zh": "plugin {name}: requires oaset {requires}，当前版本不满足 — 已跳过",
    },
    "plug_bad_constraint": {
        "en": "plugin {name}: requires_oaset constraint unparseable ({requires}) — skipped",
        "zh": "plugin {path}: requires_oaset 约束无法解析（{requires}）— 已跳过",
    },
    "plug_restricted": {
        "en": "plugin {name}: {exc} — the plugin keeps loading with its declared surface; undeclared registrations were refused",
        "zh": "plugin {name}: {exc} — 插件以受限表面继续加载，未声明的注册动作已拒绝",
    },
    "wiz_title": {
        "en": "oAset models add — onboard a new provider/model",
        "zh": "oAset models add — 接入新的 provider/model",
    },
    "wiz_providers": {
        "en": "configured providers:",
        "zh": "已配置 providers:",
    },
    "wiz_provider_prompt": {
        "en": "Provider name (new or existing)",
        "zh": "Provider 名称 (新名字或已存在的名字)",
    },
    "wiz_provider_empty": {
        "en": "provider name cannot be empty",
        "zh": "provider 名称不能为空",
    },
    "wiz_reuse_provider": {
        "en": "reusing configured {name}: {url}",
        "zh": "复用已配置的 {name}: {url}",
    },
    "wiz_base_url_prompt": {
        "en": "base_url for {name}",
        "zh": "{name} 的 base_url",
    },
    "wiz_base_url_invalid": {
        "en": "base_url must start with http(s): {url}",
        "zh": "base_url 必须以 http(s) 开头: {url}",
    },
    "wiz_provider_added": {
        "en": "provider {name} added (reference the credential later via login or env:VAR)",
        "zh": "已添加 provider {name}（凭据稍后用 login 或 env:VAR 引用）",
    },
    "wiz_model_prompt": {
        "en": "model id (provider/model)",
        "zh": "模型 ID (provider/model)",
    },
    "wiz_model_empty": {
        "en": "model id cannot be empty",
        "zh": "模型 ID 不能为空",
    },
    "wiz_context_prompt": {
        "en": "context-window tokens",
        "zh": "上下文窗口 tokens",
    },
    "wiz_maxout_prompt": {
        "en": "max output tokens",
        "zh": "最大输出 tokens",
    },
    "wiz_caps_prompt": {
        "en": "capability tags (comma-separated)",
        "zh": "能力标签 (逗号分隔)",
    },
}
LANGS = ("en", "zh")
_current: dict[str, str | None] = {"lang": None}


def resolve_language() -> str:
    current = _current.get("lang")
    if current:
        return current
    lang = (os.environ.get("OASET_LANG") or "").lower()
    return lang if lang in LANGS else "zh"


def set_language(lang: str) -> None:
    if lang in LANGS:
        _current["lang"] = lang


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Translate `key`; kwargs format the string; falls back to English."""
    lang = (lang or resolve_language()).lower()
    entry = CATALOG.get(key)
    if entry is None:
        return key
    text = entry.get(lang) or entry.get("en", key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text
