# VidYour

Requires Python 3.14 with [uv](https://docs.astral.sh/uv/) (`brew install uv`), Node 20+, and FFmpeg on `PATH`.

Copy `.env.example` to `.env` at the repository root and fill in whichever providers you use; everything falls back to local providers when empty.

## Backend

```sh
cd backend
uv sync --frozen --extra dev
DATA_DIR=../data .venv/bin/uvicorn --factory app.main:create_app --port 8000
```

## Frontend

```sh
cd frontend
npm install
npm run dev   # http://localhost:5173, proxies /api to :8000
```
