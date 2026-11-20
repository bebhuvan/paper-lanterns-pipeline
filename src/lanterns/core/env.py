"""Dependency-free .env loader with read-only donor fallback and zero secret logging."""

from __future__ import annotations

import os
from pathlib import Path

from lanterns.core.paths import repo_root

DONOR_FALLBACK_ENV = Path("/home/bebhuvan/Documents/Akshara wokspace/akshara_new_portable/akshara_new/.env")

ALLOWED_FALLBACK_KEYS: frozenset[str] = frozenset(
    {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
        "DASHSCOPE_API_KEY",
        "QWEN_CLOUD_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
    }
)


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        clean_key = key.strip()
        clean_val = value.strip().strip("'").strip('"')
        if clean_key and clean_val:
            parsed[clean_key] = clean_val
    return parsed


def _bridge_provider_key_aliases() -> None:
    if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
        os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]
    if not os.environ.get("DASHSCOPE_API_KEY") and os.environ.get("QWEN_CLOUD_API_KEY"):
        os.environ["DASHSCOPE_API_KEY"] = os.environ["QWEN_CLOUD_API_KEY"]
    if not os.environ.get("QWEN_CLOUD_API_KEY") and os.environ.get("DASHSCOPE_API_KEY"):
        os.environ["QWEN_CLOUD_API_KEY"] = os.environ["DASHSCOPE_API_KEY"]


def load_env(dotenv_path: str | Path | None = None, *, include_donor_fallback: bool = True) -> dict[str, str]:
    """Load `<repo>/.env`, `LANTERNS_ENV_FILE`, and read-only donor `.env` fallback for unset API keys."""
    applied: dict[str, str] = {}
    candidates: list[tuple[Path, bool]] = []

    if dotenv_path is not None:
        p = Path(dotenv_path)
        candidates.append((p / ".env" if p.is_dir() else p, False))
    else:
        env_override = os.environ.get("LANTERNS_ENV_FILE", "").strip()
        if env_override:
            candidates.append((Path(env_override), False))
        local_env = repo_root() / ".env"
        candidates.append((local_env, False))
        # Check if `<repo>/.env` declares LANTERNS_ENV_FILE
        local_vals = _parse_dotenv_file(local_env)
        if local_vals.get("LANTERNS_ENV_FILE") and not env_override:
            candidates.append((Path(local_vals["LANTERNS_ENV_FILE"]), False))

    if include_donor_fallback:
        candidates.append((DONOR_FALLBACK_ENV, True))

    for candidate, restrict_keys in candidates:
        for key, value in _parse_dotenv_file(candidate).items():
            if restrict_keys and key not in ALLOWED_FALLBACK_KEYS:
                continue
            if not os.environ.get(key):
                os.environ[key] = value
                applied[key] = str(candidate)

    _bridge_provider_key_aliases()
    return applied
