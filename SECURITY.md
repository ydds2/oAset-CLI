# Security Policy

## Reporting

Report vulnerabilities by opening a **private security advisory** on this
repository (Security → Advisories), or contact the maintainers through the
repo's contact channel. Please do not open public issues for
vulnerabilities. We aim to respond within 72 hours.

## Scope

- The oAset CLI application and its bundled tools
- The gateway / ACP / MCP-serve surfaces
- The update catalog client (`oaset update`)

Out of scope: third-party MCP servers you connect to, model providers,
and anything reachable only by an attacker who already runs code as you.

## Design guarantees worth knowing when reporting

- **Local-first defaults**: the gateway binds `127.0.0.1` and requires a
  local bearer token; the default network policy is pull-only; approval
  silence/timeout always denies.
- **SSRF floor**: `web_fetch` blocks cloud metadata endpoints and private
  addresses (opt-out exists, metadata stays blocked), with DNS pinning
  against rebinding.
- **Trust boundaries**: plugins must be trusted (`oaset trust-plugin`,
  sha256-pinned) before loading; MCP stdio installs from the registry are
  flagged as third-party; web/tool output is framed as untrusted content
  in the model context.
- **Credentials** live only under `~/.oaset/credentials/` — never in
  `config.toml`, transcripts, or git.

## Disclosure

Once a fix ships we publish the advisory with credits. Coordinated
disclosure preferred; no bounties (community project).
