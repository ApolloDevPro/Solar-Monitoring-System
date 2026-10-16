from __future__ import annotations

from dotenv import dotenv_values

from ensure_env import ensure_env


def test_creates_local_env_from_example_with_secure_key(tmp_path):
    example = tmp_path / ".env.example"
    env = tmp_path / ".env"
    example.write_text(
        "SOLAR_DEVICE_ID=tracker\nSOLAR_API_KEY=replace-this-with-a-long-random-secret\n",
        encoding="utf-8",
    )

    key, created = ensure_env(env, example)

    assert created
    assert len(key) >= 40
    assert dotenv_values(env)["SOLAR_API_KEY"] == key
    assert dotenv_values(env)["SOLAR_DEVICE_ID"] == "tracker"


def test_preserves_an_existing_configured_key(tmp_path):
    example = tmp_path / ".env.example"
    env = tmp_path / ".env"
    example.write_text("SOLAR_API_KEY=placeholder\n", encoding="utf-8")
    env.write_text("SOLAR_API_KEY=my-existing-key\nOTHER=value\n", encoding="utf-8")

    key, created = ensure_env(env, example)

    assert key == "my-existing-key"
    assert not created
    assert dotenv_values(env)["OTHER"] == "value"


def test_replaces_blank_key_without_discarding_other_settings(tmp_path):
    example = tmp_path / ".env.example"
    env = tmp_path / ".env"
    example.write_text("SOLAR_API_KEY=placeholder\n", encoding="utf-8")
    env.write_text("SOLAR_DEVICE_ID=my-tracker\nSOLAR_API_KEY=\n", encoding="utf-8")

    key, created = ensure_env(env, example)

    assert created
    assert dotenv_values(env)["SOLAR_API_KEY"] == key
    assert dotenv_values(env)["SOLAR_DEVICE_ID"] == "my-tracker"
