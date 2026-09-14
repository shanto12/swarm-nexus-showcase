# Authenticated runtime setup

Requires Python 3.11+ and `uv`. Use your own credentials and persistent storage. Copy [.env.example](../.env.example) to an untracked environment file and configure the variables; the example contains placeholders only. The application does not automatically load an env file: inject variables with your process manager or shell.

```sh
cd runtime
uv sync --frozen
PYTHONPATH=backend uv run uvicorn swarm.api:app --host 127.0.0.1 --port 8001
```

Set `SWARM_OWNER_EMAIL`, `SWARM_PASSWORD_HASH`, `SWARM_PUBLIC_ORIGIN`, `SWARM_DB_PATH` and `DEEPSEEK_API_KEY`. The password-hash format is `scrypt$16384$8$1$salt_hex$key_hex`, with at least 16 salt bytes and a 32-byte derived key; [`backend/tests/test_api.py`](../runtime/backend/tests/test_api.py) demonstrates the format with a test-only password. For local HTTP only, set `SWARM_COOKIE_SECURE=false`. Keep secure cookies enabled in production. Serve the runtime behind HTTPS.

To connect the full interface, build with `VITE_SHOWCASE=false`; copy `integrations/api.mts` into your Netlify functions folder, adjusting its import for that location, enable that folder in your own Netlify configuration, remove the static `/api/*` denial rule, and set `NEXUS_RUNTIME_ORIGIN` to your HTTPS runtime. Set `SWARM_PUBLIC_ORIGIN` to the same frontend origin. Neither backend tokens nor owner passwords belong in frontend environment variables. The retained relay tests show the origin/cookie/redirect contract. The public showcase intentionally does not make these changes.

The provider model in this captured deployment reports `deepseek-v4-flash`. Provider aliases and availability can change; confirm your account's supported model before deploying your own runtime. Optional LangSmith settings are server-side only. Do not treat a configured tracing flag as evidence that traces arrived.


The public walkthrough requires no credentials and starts with `npm ci && npm run dev` at the repository root.
