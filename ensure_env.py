"""Create a local .env with a strong API key when one is not configured."""
from __future__ import annotations

from io import StringIO
import re
import secrets
from pathlib import Path

from dotenv import dotenv_values

PLACEHOLDER = "replace-this-with-a-long-random-secret"


def ensure_env(env_path: Path, example_path: Path) -> tuple[str, bool]:
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else (
        example_path.read_text(encoding="utf-8")
    )
    configured_key = dotenv_values(stream=StringIO(content)).get("SOLAR_API_KEY")
    if configured_key and configured_key != PLACEHOLDER:
        if not env_path.exists():
            env_path.write_text(content, encoding="utf-8")
        return configured_key, False

    generated_key = secrets.token_urlsafe(32)
    replacement = f"SOLAR_API_KEY={generated_key}"
    if re.search(r"(?m)^SOLAR_API_KEY=.*$", content):
        content = re.sub(r"(?m)^SOLAR_API_KEY=.*$", replacement, content, count=1)
    else:
        content = content.rstrip() + f"\n\n{replacement}\n"
    env_path.write_text(content, encoding="utf-8")
    return generated_key, True


def main() -> int:
    root = Path(__file__).resolve().parent
    env_path = root / ".env"
    example_path = root / ".env.example"
    if not example_path.is_file():
        print(f"ERROR: Environment template not found: {example_path}")
        return 1
    try:
        _, created = ensure_env(env_path, example_path)
    except OSError as exc:
        print(f"ERROR: Could not prepare {env_path}: {exc}")
        return 1
    if created:
        print("Created .env and generated a strong local API key.")
        print("Open .env and paste SOLAR_API_KEY into the dashboard's API access key field.")
    else:
        print(".env already contains a configured SOLAR_API_KEY.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
