<p align="center"><img src="docs/assets/logo.svg" width="112" alt="oAset logo"></p>

# oAset CLI

Local-first personal coding terminal. OpenAI-compatible providers, streaming tool calls, a Textual TUI, and session persistence on disk.

Python 3.11+ · Windows-first (also Linux / macOS) · MIT · **GitHub only** — not on PyPI.

> **独立声明**：oAset CLI 是独立开源项目，与它可连接的模型厂商（DeepSeek、智谱、月之暗面、Anthropic、Google、OpenAI 等）**无隶属、无赞助、无背书关系**。文中提及的厂商与产品名称仅用于说明接口对接关系（指示性使用），相关商标归各自所有者。调用各厂商 API 时使用你自己的密钥，并受该厂商的服务条款与使用政策约束。


**在中国大陆开箱即用** —— 不需要代理、不需要海外 API key：

- **国产模型一等公民**：默认配置内置 DeepSeek（开箱默认）、智谱 GLM、Kimi（月之暗面），端点全部国内直连（`api.deepseek.com` / `open.bigmodel.cn` / `api.moonshot.cn`），`oaset login glm` 存 key 即用。
- **自运行能力引擎（原生技能优先）**：主流模型的服务端技能自动启用、本地等价工具自动退位（省 token、结果质量更高）。覆盖搜索、抓取、服务端代码执行、扩展思考；Kimi `$web_search` 已真机验证，其余按官方规范接线。`[search] native = "off"` 一键全关。
- **搜索优先走合规通道**：有 GLM key 时 `web_search` 默认走智谱 web-search-pro 官方 API（复用现有凭据）；两者都无时回落免配置的 Bing RSS（非官方接口，仅限个人非商业用途）。
- **镜像源友好**：依赖安装可走国内 PyPI 镜像（见下方安装命令）；更新目录支持 `[update] catalog_url` 指向任意镜像，`oaset update` 不依赖 GitHub Pages。
- **断网可用的完整代理**：`--mock` 是硬离线保证；`oaset offline-check` 体检"网络消失后还有什么能用"——本机 Ollama/LM Studio 自动发现、免网工具清单、会话/检查点/证据全部落在本机。
- **证据门闩（/evidence on）**：完成声明必须附带收据——验证命令输出、诊断结果——写入会话 JSONL；`oaset evidence <session>` 本地重放/导出审计链。计划里未完成的场景会被门闩点名。
- **code_outline 工具**：按需的结构索引（类/函数 + 行号，无正文）——Aider 式省 token 与 Claude Code 式按需探索的折中，Python 走 ast 精确解析。

这三条不是宣传语——每一条都有对应测试锁定（`tests/test_china_ready.py`）。

Current version: **0.10.0** (`src/oaset/__init__.py`).

```
oaset                 interactive TUI
oaset -p "prompt"     one-shot (final answer on stdout)
oaset -c              resume the latest session in this directory
oaset --resume ID     resume a specific session (a unique short id works)
```

---

## 它是什么

oAset 在当前工作区里读代码、改文件、跑命令。模型调用走你配置的 endpoint；会话、记忆、密钥都在本机 `~/.oaset/`（可用 `OASET_HOME` 改路径）。默认网络策略是 **只收不发**（`pull_only`）：可以拉网页、可以打模型，不会把会话上传到任何地方。

TUI、一次性 `-p`、HTTP gateway、cron、ACP 都走同一套运行时（`SessionHost` / `AgentKernel`）。TUI 会话会写成 JSONL；`oaset -p` 只打印结果，**不**写会话文件（`-c`/`--resume` 恢复的会话例外：这次交互会追加回原会话，含用量）。

**多进程共存**（桌面端核心场景）：TUI、ACP、gateway、一次性 CLI 可同时运行——会话正文、索引、配置、凭据的全部写入走跨进程文件锁 + 原子替换（无第三方依赖，msvcrt/flock 实现），崩溃不会留下半写的密钥或索引；所有 serve 面（acp / gateway / mcp-serve）挂 Windows Job Object，进程被杀时 MCP 子进程树一并退出。

---

## 安装

需要 Python 3.11 或 3.12。**分发只通过 GitHub**（本仓库）；不要 `pip install oaset-cli`：那不是本仓库的发布渠道。

**从 GitHub 拉取后，三步即可用**（本地配置会自动生成，只有模型与密钥需要你选一次）：

```bat
pip install "git+https://github.com/ydds2/oAset-CLI"   :: 1) 安装（正式仓库）
oaset setup                                                         :: 2) 选 provider、存密钥、设默认模型
oaset                                                               :: 3) 启动（首次进入只问一次语言，并可选立即配置模型）
```

配置都在本机 `~/.oaset/`（`config.toml` 首次运行自动生成；密钥只进 `~/.oaset/credentials/`）。`oaset doctor` 会自愈缺失的默认配置并指出下一步。

### 从源码（开发 / 日常）

```bat
git clone https://github.com/ydds2/oAset-CLI.git
cd oAset-CLI
python -m venv .venv
.venv\Scripts\activate
pip install -e .
oaset --version
```

依赖下载慢或超时（国内常见）时走镜像：

```bat
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

更新目录同样可指镜像（GitHub Pages 国内不稳）：`config.toml` 里设

```toml
[update]
catalog_url = "https://你的镜像/oaset/catalog/index.json"
```

`oaset --version` 会写明你在跑 **dev source**（工作树 + commit）还是 **release**（带 fingerprint 的打包 exe）。源码安装永远不是 release。

Linux / macOS 把 activate 换成 `source .venv/bin/activate`。

### GitHub Releases（Windows exe）

打 `v*` tag 时，CI（`.github/workflows/release.yml`）在 `windows-latest` 上打包 **单文件** `oaset.exe`，连同 `oaset.exe.sha256` 发到 GitHub Releases。Actions 的 artifact 不是下载渠道。

1. 从 [Releases](https://github.com/ydds2/oAset-CLI/releases) 下载 `oaset.exe` 和校验文件。
2. 核对 sha256。
3. 放到 PATH。配置仍在 `%USERPROFILE%\.oaset\`。

本地 `pyinstaller` 只适合调试，不要当发布物。打包必须先 `python scripts/stamp_build.py`，否则 `oaset.spec` 会拒绝继续。

---

## 第一次使用

```bat
oaset setup
oaset
```

`oaset setup` 选 provider、用 `getpass` 存密钥、设默认模型。密钥写入 `~/.oaset/credentials/<provider>.json`（尽量 `0600`），**不进** `config.toml`。

也可以分开做：

```bat
oaset login deepseek
oaset models list
oaset
```

进 TUI 后：直接提问。没有密钥时欢迎页会提示 `/login <provider>`，发送会被拦住。`/help` 是日常快捷键和命令；`/help all` 是完整表；`Ctrl+K` 打开命令面板。

内置 provider（`config.py` 默认表）：`deepseek`、`moonshot`、`openai`、`anthropic`、`bedrock`、`gemini`、`glm`，以及本机 `ollama` / `lmstudio`。启动时会向 loopback 的 `/models` 探测真实 tag，失败则静默（本机服务没开不能挡住启动）。

环境变量密钥也可以：config 里 `api_key = "env:DEEPSEEK_API_KEY"` 这种形式。`oaset login` 存的文件优先于空 env。

语言：第一次启动会问 **中文 / English**，写入 `[ui] language`（`language_chosen = true`）。之后用 `/language zh` 或 `/language en` 切换；环境变量 `OASET_LANG=en` 可覆盖且跳过首次询问。

---

## TUI

启动后是一条流式对话：用户行用 `›`，输入栏常驻 `❯`。Markdown 按块增量提交（完整块不再整篇重排）；shell 输出会刷到正在跑的工具卡片上。

| 按键 | 作用 |
| --- | --- |
| Enter | 发送。agent 运行中会排队 |
| Ctrl+J | 输入换行 |
| Esc | 中断流式输出 / 关面板 / 清选择 |
| Ctrl+C ×2 | 退出（有确认提示） |
| Shift+Tab | PLAN 模式（只读） |
| Ctrl+K | 命令面板 |
| Ctrl+Shift+C / V | 复制（先选区，否则上一条回答，再否则草稿） / 粘贴 |
| Ctrl+X | 剪切（输入框，Windows 习惯） |
| ↑ / ↓ | 历史与补全 |
| 右键 | 对话区：复制选区或粘贴；输入框：粘贴 |
| F2 | 全屏看上一条消息（终端原生字符选择） |
| Ctrl+G | 用 `$EDITOR` / `$VISUAL` / `/editor` 编辑草稿 |
| Ctrl+T | 输入栏上方的计划条 |
| Ctrl+O | 折叠工具输出与思考 |
| Ctrl+S | 向正在跑的回合注入一句指令 |

聊天区可选中消息块；在同一块内拖行可按行复制。**Shift+拖拽直接使用终端原生字符级选择**（oAset 不拦截）；F2 / `/view` 全屏查看亦可。

### 日常斜杠命令

`/help` 和空的 `Ctrl+K` 展示这组（其余仍可搜索、可输入）：

`/login` `/model` `/think` `/new` `/sessions` `/undo` `/rewind` `/compact` `/skills` `/mcp` `/mode` `/status` `/doctor` `/exit` `/clear` `/tools` `/memory` `/plans` `/init` `/retry` `/todo`

完整注册表在 `src/oaset/tui/commands.py`（含 `/worktree`、`/image`、`/steer`、`/copy`、`/settings`、`/agents`、`/goal`、`/density` 等）。写盘或危险命令在面板里会先确认。

权限弹出在输入栏上方，不是遮罩：

- `y` 允许一次
- `a` 本会话放行（读操作是「这个目录本会话放行」）
- `n` / Esc / 超时 = 拒绝

`/mode plan` 只读；`/mode auto` 或 `--yolo` 跳过确认（危险，仅在你明确要时用）。

`/think [off|light|medium|heavy]` 适配各家思考档位（下一次模型调用生效，`/think` 无参数看当前档与该模型的真实旋钮）；设定后状态栏模型名旁显示 `✦档位` 徽标，无头模式用 `--think` 旗标：

- Anthropic / Bedrock：`thinking.budget_tokens`（light 2048 / medium 8192 / heavy 24576，自动抬高 `max_tokens` 并强制 `temperature=1`；off 删除参数）
- OpenAI / Azure：`reasoning_effort` low/medium/high；gpt-5 的 off 映射 `minimal`，o 系无 off 则不发送
- Gemini：`thinkingConfig.thinkingBudget`（Flash 可 0 关闭，2.5 Pro 最低 128）；gemini-3 走 `thinkingLevel` 且不可关
- GLM `thinking.type` / Qwen `enable_thinking`：只有开/关，分档全部等价于开（界面明说）
- DeepSeek / Kimi：请求级无旋钮（思考与否由型号名决定），oAset 如实显示「无」而不是装样子

模型级默认值写在 `[models.<id>] thinking = "..."`；`/think` 一旦手动设置，本进程内换模型不再重置。

---

## 权限、沙箱、网络

工作区内只读工具自动跑。工作区外读取按目录确认一次。写文件和 shell 默认要确认。

`run_shell` 默认开沙箱（`[shell] sandbox_default = true`，内存上限 2048 MB）：

- Windows：**Job Object（资源上限）+ 受限令牌（文件系统写保护）**。命令经 `oaset.sandboxexec` 垫片启动：`Authenticated Users` / `Administrators` 变为 deny-only，经组授权的写位置（C:\ 根建文件、部分 ProgramData）由 OS 拒绝；任何一步失败即拒绝执行（fail-closed）。已知边界：用户 SID 直接授权的位置（如 `~/.ssh`）不受组 deny 影响，由预检启发式拦截——`oaset doctor` 如实报告
- Linux：有 `bwrap` 时包一层（工作区可写，其余只读）
- macOS：有 `sandbox-exec` 时用 Seatbelt 限制写路径
- 找不到这些二进制就退回 rlimit / Job Object，命令仍会跑；POSIX 的包装 argv 有跨平台单元测试锁定

网络（`[network] mode`）：

| 模式 | 行为 |
| --- | --- |
| `local_only` | 禁止远程。loopback 模型（Ollama / LM Studio / 127.0.0.1）仍可用 |
| `pull_only`（默认） | 允许 GET 拉取和模型调用；禁止向第三方推内容 |
| `full` | 含 gateway 回发、远程 shell 后端（ssh / docker / …） |

Telegram / Discord / Slack 传输默认只收。要回消息必须 `--send-replies` 或把网络模式设成 `full`。

---

## 工具

内置工具包括：

- 文件：`read_file` `write_file` `edit_file` `apply_patch` `glob` `grep` `list_dir` `repo_map`
- 执行：`run_shell`（可后台），`task_list` / `task_output` / `task_stop`
- 搜索：`web_search`（引擎优先级：智谱 web-search-pro（复用 glm key）→ Bing RSS 免配置 → SearXNG 自建；支持原生搜索的模型自动改用服务端通道，见能力引擎），`web_fetch`

**原生能力矩阵**（`oaset doctor` 实时显示当前模型的状态）：

| 供应商 | 技能 | 机制 | 状态 |
| --- | --- | --- | --- |
| Moonshot Kimi | web_search | `client_tool`（`$web_search` 空回复契约） | 真机验证 |
| 智谱 GLM | web_search | zhipu 引擎通道（web-search-pro API） | 真机验证 |
| OpenAI | web_search | `param`（`web_search_options`） | 按规范接线 |
| 通义 Qwen | web_search | `param`（`enable_search`） | 按规范接线 |
| Anthropic | web_search / web_fetch / code_execution / thinking | `server_tool` + `param` | 按规范接线 |
| Gemini | google_search / url_context / code_execution / thinking | `server_tool` + `generationConfig` | 按规范接线 |
| DeepSeek | （无原生搜索；reasoner 走 `reasoning_content`） | — | 搜索走本地引擎链 |

未接线（有意）：OpenAI `code_interpreter` / `file_search` / computer use 属于 Responses/Assistants API，不在 chat.completions。

**本机 computer-use**（不套 UI-TARS / CUA，执行层是 oAset 自己的）：动作名沿用主流 agent 的约定（`screenshot` / `left_click` / `type` / `key` / `scroll` / `drag` / `zoom`），oAset 在 Windows 上用 UIA 截图 + SendInput 执行，截图 PNG 回传到工具结果。配套 `bash`（走 `run_shell` 沙箱）和 `str_replace_based_edit_tool`（走本机读写编辑）。这些一律以**普通函数工具**形式提供给支持工具调用的模型——不声明任何厂商的专有 computer-use 协议、不携带 beta 头。权限门每次确认桌面输入；`--yolo` 才免确认。
- 代理：`task`（串行子代理），`parallel_task`
- 会话：`todo_write` `memory` `search_history` `cron` `skill_create` `git_status`
- 计划：`ask_user` `enter_plan_mode` `exit_plan_mode`
- 校验：`lsp_diagnostics`（本机要有 language server）
- 浏览器（CDP / Edge / Chrome）：`browser_open` `browser_observe` `browser_click` `browser_type` `browser_screenshot` …
- 桌面（Windows UIA 控件）：`desktop_windows` `desktop_find` `desktop_click` `desktop_type` `desktop_screenshot`
- 本机 computer-use：`computer` `bash` `str_replace_based_edit_tool`（截图坐标协议，见上）

改了源码后缀（`.py` `.ts` `.css` 等，不含 `.md` / `.txt`）之后，若本回合没跑校验工具，内核会 **nudges 一次**：先跑 `run_shell` / `lsp_diagnostics`，视觉工作还要看真实屏幕。这是机械门闩，不是装饰。内置技能 `ship-complete` 说的是同一件事：编译过 ≠ 做完。

内置技能 `idea-forge`（需求锻造）管另一端：**动态需求 → 确认方案 → 制作**。你随口一句需求，主模型先自己优化提示词——重写成结构化规格（背景/用户/核心场景/验收标准），并按**同类型产品**补齐你没说但用户会当标配的隐含需求（四桶分类：用户原话 / 同类标配 / 本项目特有 / 明确排除，补齐项在确认前给你过目）；再基于开源项目与市场主流路线绘制技术图谱（分层架构 + mermaid 构建顺序 + 阶段里程碑，OSS 逐层点名，没有可信选项就写「自研」）；方案落盘 `.oaset/plans/<slug>.md` 后是**硬确认门**——`ask_user` 确认/调整/放弃，没有明确确认不写一行产品代码；确认后按图制作，ship-complete 纪律全程生效。说「需求锻造 / idea-forge / 先做方案」即可触发。

项目约定从工作区的 `OASET.md` / `AGENTS.md` 注入系统提示。`/init` 会让模型根据实际读到的文件生成 `AGENTS.md`。

子代理定义在 `.oaset/agents/*.md`（`/agents` 管理），头部字段：`name` `description` `tools`（留空=全部）`model`（留空=继承主模型）`max_iterations` `timeout_seconds`，以及 `thinking: off|light|medium|heavy`——给该代理**固定思考档位**（留空=继承当前 `/think` 档）。机械型代理（搜索、格式化）设 `thinking: off` 可以不再为用不上的推理付费。`/agents show` 会展示全部生效字段。

---

## 本机数据（`~/.oaset` 或 `$OASET_HOME`）

| 路径 | 内容 |
| --- | --- |
| `config.toml` | provider、模型、权限、网络、UI |
| `credentials/<provider>.json` | API key |
| `sessions/wd_<name>_<hash8>/session_<uuid>.jsonl` | 会话正文 |
| `session_index.jsonl` | 会话索引 |
| `memory/MEMORY.md`、`USER.md` | 跨会话记忆 |
| `skills/` | 用户技能（agentskills.io：`<name>/SKILL.md`） |
| `plugins/` | 用户插件 |
| `mcp.json` | 用户 MCP 服务器 |
| `cron.json` | 定时任务 |
| `logs/oaset.log` | `--debug` 日志 |
| `audit/YYYYMMDD.jsonl` | 工具调用审计（每次调用的工具/参数/耗时/会话，纯本地） |
| `input_history.json` | 输入历史 |

工作区还可以有 `.oaset/skills`、`.oaset/plugins`、`.oaset/mcp.json`、`.oaset/worktrees/`。

**信任边界：** 用户目录下的插件和 MCP 默认加载。仓库里的 `.oaset/plugins/*.py` 和 `.oaset/mcp.json` **默认不跑**，直到：

```bat
oaset trust-plugin <stem>
oaset mcp trust <name>
```

信任记录钉 sha256；文件一改就失效，需要重新信任。

技能加载顺序：内置 → `~/.oaset/skills` → `.oaset/skills`（同名后者覆盖）。内置不能删。匹配到技能时模型应先 `read_file` 那份 `SKILL.md`，不要猜步骤。

---

## CLI

```
oaset login PROVIDER
oaset logout PROVIDER
oaset setup
oaset doctor
oaset models [list|add|verify] [MODEL_ID]   # add --verify 保存后立即探测
                                            # verify --all 逐一真机验证全部模型（阶段 0 诚实矩阵）
                                            # list 会自动标注本机在跑的 Ollama/LM Studio（零配置发现）
oaset sessions            # 人读列表（跨目录的会话会标出目录）；--json 输出机器可读契约
oaset offline-check       # 断网就绪体检（本地模型 / 免网工具 / 本机数据）
oaset evidence <SESSION> [--export OUT.md]   # 重放/导出证据审计链
oaset config [list|set KEY VALUE]
oaset mcp [list|add|trust|revoke] [NAME]   # add: 从 MCP 注册表一键接入（stdio 条目会安装第三方包，已标注）；--url 直接加 http 服务
oaset mcp-serve [--cwd DIR] [--all-tools] [--token TOKEN]
oaset acp [--cwd DIR] [--yolo] [--lsp]
oaset gateway [--host 127.0.0.1] [--port 8790] [--panel]   # --panel: 工具审批变为面板挂起请求（SSE /events 实时推送，POST /panel/approve 解析，超时拒绝）
oaset cron list|add|remove|run|daemon
oaset matrix --tasks tasks.jsonl [--max-parallel 3]
oaset batch --tasks prompts.jsonl [--out runs]
oaset market list|add|use|remove|install|installed
oaset update check|refresh|list|plan|apply|rollback|history|doctor
oaset trust-plugin NAME
```

一次性模式：

```bat
oaset -p "summarise this repo" --model deepseek/deepseek-chat
type notes.txt | oaset -p "extract action items"
oaset -p "..." --output-format json
oaset -p "..." --output-format stream-json   # 每行带 schema_version/sequence/timestamp 信封
oaset -p "deep refactor..." --think heavy     # 思考档位（off|light|medium|heavy，别名 low/high 亦可）
```

`-p` 支持管道/重定向文件（真正的 FIFO 或普通文件；不会去读 TTY）。进度在 stderr，答案在 stdout。`--quiet` 只留结果；`--verbose` 多打 provider/model/耗时。

`oaset acp`：stdio 上的 JSON-RPC 2.0（ACP-lite）。方法：`initialize`、`session/new`、`session/load`、`session/list`、`session/prompt`、`session/cancel`、`ping`。**实时事件**：`session/update` 在事件发生时即刻推送（不等回合结束）；`session/cancel` 可在回合中途打断；权限审批走 `session/request_permission` 请求——桌面端据此弹权限框，客户端不答或取消一律 **deny**（本地默认拒绝）。图片 prompt（base64 块）透传给模型；会话默认启用 MCP 工具（`session/new` 传 `"mcp": false` 关闭）。`--lsp` 改用 Content-Length 分帧。这不是完整的 agent-client-protocol Python SDK。

`oaset mcp-serve`：把 oAset 的工具暴露给其他 MCP 客户端。默认只读；`--all-tools` 才带上写/执行。

`oaset gateway`：`POST /message` `{"text": "...", "session_id": "可选"}`，默认 `127.0.0.1:8790`，**需要本地令牌**：首次启动自动生成 `~/.oaset/gateway_token`，请求带 `authorization: Bearer <token>`（`OASET_GATEWAY_TOKEN` 环境变量可覆盖）；同一会话跨请求复用上下文，不刷会话索引。`/health` 免令牌。可选 `--telegram` / `--discord` / `--slack` 传输。

`oaset cron`：`every 20m`、`daily 09:00`、或 5 字段 cron。`oaset cron daemon` 每 30 秒扫到期任务。

`oaset matrix`：并行多任务——每个任务在自己的 git worktree 里跑（用户当前工作区绝不动），最多 N 个并发，结束输出审阅表；worktree 保留供 diff 审查与合并（TUI 里 `/worktree list` 查看）。

`oaset upgrade` 是遗留的 `pip install --upgrade oaset-cli`，**冻结 exe 上不可用**。源码用户请 `git pull` 再 `pip install -e .`；二进制用户请下新的 Release，或走 `oaset update`。

---

## 配置摘要

`~/.oaset/config.toml` 首次启动会按默认表创建。常用键：

```toml
default_provider = "deepseek"
default_model = "deepseek/deepseek-chat"
permission_mode = "default"          # default | auto
max_iterations = 0                   # 0 = 不限制
max_tool_calls = 0                   # 0 = 不限制（相同调用熔断仍在）

[ui]
theme = "oaset-dark"
language = "zh"                      # zh | en
# editor = "notepad"
# density = "compact"                # cozy（默认）| compact
# notify = "desktop"                 # 回合结束提醒: off | bell | desktop(OSC 9/777)

# 成本透明：给模型定价（USD / 百万 token，0=不计价不显示）。价格自己填、自己拥有。
[models."deepseek/deepseek-chat"]
price_in = 0.27
price_out = 1.10

[network]
mode = "pull_only"                   # local_only | pull_only | full

[shell]
backend = "local"                    # local | ssh | docker | …
sandbox_default = true
sandbox_memory_mb = 2048

[search]
backend = "auto"                     # auto | 显式引擎名（不会偷偷改路由）
# searxng_url = "http://127.0.0.1:8080"
```

Provider 段：`type`（`openai` / `azure` / `anthropic` / `gemini` / `bedrock`）、`api_key`（`env:NAME` 或留空靠 `oaset login`）、`base_url`。模型 id 形如 `provider/model`（Azure 的 model 字段填部署名）。`prompt_cache`（anthropic 与 bedrock 端点，默认开启，可设 `false` 关闭）在工具表/系统提示/最近**两个**用户回合边界打 `cache_control` 断点（4/4 断点预算：空闲超过 5 分钟 TTL 仍可命中上一边界；`prompt_cache_ttl = "1h"` 可选延长 TTL——写入价 2×，慢节奏会话才划算）；OpenAI 兼容端点（含 DeepSeek 的 `prompt_cache_hit_tokens`）与 Gemini 的隐式缓存自动记账。`/context` 显示最近一次调用的缓存命中/写入与会话累计复用率。

```bat
oaset config list
oaset config set default_model glm/glm-5.3-flash
```

TUI 里 `/reload` 会从磁盘重读 `config.toml`。模型段支持 `thinking = "off|light|medium|heavy"`（默认空 = 不发参数），作为该模型的思考档位默认值，运行时 `/think` 覆盖。

---

## Python API

```python
from oaset.sdk import Oaset
from oaset.host import SessionHost

agent = Oaset(model="glm/glm-5.3-flash", cwd=".")
async for event in agent.chat("read demo.txt"):
    if event.type == "assistant_delta":
        print(event.data["text"], end="")

answer = await agent.ask("now summarize")
```

桌面或其他外壳应直接用 `SessionHost`（一个会话、一份 JSONL、一条事件流）。`Oaset.create_session()` 在**同一个** host 上开新会话，不是第二份独立运行时。

---

## 开发

```bat
pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src/ tests/
python -m mypy src/oaset --ignore-missing-imports
```

CI（Ubuntu + Windows，3.11 / 3.12）：`uv lock --check`、ruff、pytest（覆盖率门闩 70%）、mypy。

插件：`~/.oaset/plugins/*.py` 里定义 `register(api)`，可 `api.add_command` / `api.add_tool`。项目插件必须先 `oaset trust-plugin`。

Worktree：`/worktree new <name>` 在 `.oaset/worktrees/<name>` 下 `git worktree add`，不碰你当前的脏工作区。

提示词工程参考库：`docs/research/agent-prompt-patterns.md` —— 各大终端智能体系统提示词的蒸馏式样库（含缓存友好纪律），改 `src/oaset/agent/prompts.py` 前先读它。

---

## 做完的标准（给用这个 CLI 的人，也给模型）

源码能编译不等于产品做完。oAset 的系统提示（v5）和机械门闩一起强制这几条：

1. **验收清单先行**：动手前把需求重述成可核验的编号清单，最终回复逐条给出证据——"写了点相关的东西"不等于"达到要求"。
2. **全栈不静默砍层**：请求暗示一个系统（数据存储 / 后端 API / 前端 / 鉴权 / 测试 / 部署）时，每一层要么实现，要么在写代码**之前**明说不做；verify-gate 会在改完源码后追加栈清单追问。
3. **标准流程、既有技术栈**：先读再改、小批量实现、每批跑 lint/测试/构建；在仓库现有框架内工作，不另起炉灶。
4. **视觉工作看真实屏幕**（`browser_observe`/截图/合成器输出），只读源码不算证据。

内核的 verify-gate 和内置技能 `ship-complete` 强制的是同一条规则。界面侧：连续 5 个以上工具调用自动折叠为一行 `⋯`（点击展开）、长通知钳制为 12 行（复制保留全文）、回合无输出超过 8 秒状态栏显示 `无输出 Ns`、输入栏带边框常驻可见。

---

## 无障碍

- **行式模式（读屏/哑终端友好）**：`oaset --plain`。与 TUI 共用同一运行时（SessionHost、会话持久化、权限确认），但输出是**逐行追加的纯文本**——零 ANSI 光标移动、零动画，屏幕阅读器可以像读普通终端程序一样朗读；权限确认也是一行式 `y/a/n` 问答。慢速 SSH、管道处理同样适用。
- **减少动画**：`config.toml` 设 `[ui] reduce_motion = true` 冻结所有动态元素——状态栏/工具卡/活动行 spinner 改为静态字形、输入光标停止闪烁、命令面板光标停止闪烁。语义与操作系统的"减少动态效果"设置一致。
- **颜色不承载唯一信息**：成功/失败等状态始终有 `✓`/`✗` 字形兜底，色盲用户不依赖颜色也能分辨。

---

## 许可

MIT。见 [LICENSE](LICENSE)。
