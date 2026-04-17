"""
Root launcher.

Running `python main.py` starts the FastAPI service under uvicorn. All real
code lives under `src/` — this file is deliberately tiny.

Environment overrides:
  HOST   - bind address (default 0.0.0.0)
  PORT   - port         (default 8000)
  RELOAD - 'true' to enable uvicorn auto-reload for local development

Note: the original minimal CLI script is preserved at `artifacts/main.py`.
"""

import os

import uvicorn


def _getenv_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var in a forgiving way."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host   = os.getenv("HOST", "0.0.0.0"),
        port   = int(os.getenv("PORT", "8000")),
        reload = _getenv_bool("RELOAD", False),
    )
