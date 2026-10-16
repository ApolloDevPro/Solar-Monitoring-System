"""Create a local .env from .env.example when one is not present."""
from __future__ import annotations

from pathlib import Path


def ensure_env(env_path: Path, example_path: Path) -> bool:
    """Copy the example env file into place if .env is missing.

    Returns True when the file was created.
    """
    if env_path.exists():
        return False
    env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def main() -> int:
    root = Path(__file__).resolve().parent
    env_path = root / ".env"
    example_path = root / ".env.example"
    if not example_path.is_file():
        print(f"ERROR: Environment template not found: {example_path}")
        return 1
    try:
        created = ensure_env(env_path, example_path)
    except OSError as exc:
        print(f"ERROR: Could not prepare {env_path}: {exc}")
        return 1
    print("Created .env from .env.example." if created else ".env already exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
