# Easy PRD2

Easy PRD2 is a local web app for safely creating fresh copies of Rossum demo
workspaces and queues in another organization. It uses Rossum's PRD2 deployment
engine for dependency remapping while keeping credentials in memory.

## Install and run (macOS)

Python 3.12+ and Git are required.

```bash
pipx install .
easy-prd2
```

The app binds to `127.0.0.1` and opens the browser. Use `easy-prd2 --no-browser`
or `easy-prd2 --port 9000` when needed.

## Safety model

- Every deployment is create-only. Target IDs in generated PRD2 manifests are
  always empty, so existing target configuration is never updated.
- API tokens stay in process memory and are never written to history, logs,
  manifests, browser storage, or credentials files.
- Hook secrets are not copied.
- A deployment must be previewed and explicitly confirmed.
- Rollback is a separate, explicit operation and is blocked when a created
  object has changed since deployment.

Non-secret run history is stored in
`~/Library/Application Support/Easy PRD2`. Override this location for tests or
portable use with `EASY_PRD2_DATA_DIR`.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
pytest
easy-prd2 --no-browser
```

