# GPTmax

Monorepo snapshot for the current production stack on the server.

Included components:

- `CLIProxyAPI/`: gateway and management backend
- `chatgpt_register_web/`: registration and pool maintenance web app
- `Clash/`: proxy subscription update script and runtime launcher

Sanitized before publishing:

- live account/auth data
- PostgreSQL local store and runtime caches
- active `.env` / runtime config files
- generated binaries, logs, `node_modules`, build artifacts
- subscription runtime database and Clash live config

Important runtime files are intentionally excluded:

- `CLIProxyAPI/config.yaml`
- `CLIProxyAPI/.env`
- `CLIProxyAPI/auths/`
- `CLIProxyAPI/pgstore/`
- `chatgpt_register_web/config.json`
- `chatgpt_register_web/ak.txt`
- `chatgpt_register_web/rk.txt`
- `chatgpt_register_web/registered_accounts.txt`
- `chatgpt_register_web/codex_tokens/`
- `Clash/config/config.yaml`

Bootstrap notes:

- For `CLIProxyAPI`, start from `CLIProxyAPI/config.example.yaml` and `.env.example`.
- For `chatgpt_register_web`, copy `chatgpt_register_web/config.example.json` to `config.json` and fill your own values.
- For `Clash`, place your own runtime config at `Clash/config/config.yaml`.

This repo was assembled from the live server snapshot on 2026-03-18.
