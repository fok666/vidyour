# VidYour

## Backend

Install and launch the backend from the repository root:

```sh
cd backend
uv sync --frozen --extra dev
DATA_DIR=../data .venv/bin/uvicorn --factory app.main:create_app --port 8000
```
