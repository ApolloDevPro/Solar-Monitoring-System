from ensure_env import ensure_env


def test_creates_env_from_example_when_missing(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text("SOLAR_DEVICE_ID=tracker\n", encoding="utf-8")
    env = tmp_path / ".env"
    assert ensure_env(env, example) is True
    assert env.read_text(encoding="utf-8") == "SOLAR_DEVICE_ID=tracker\n"


def test_existing_env_is_left_alone(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text("SOLAR_DEVICE_ID=from-example\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("SOLAR_DEVICE_ID=already-set\n", encoding="utf-8")
    assert ensure_env(env, example) is False
    assert env.read_text(encoding="utf-8") == "SOLAR_DEVICE_ID=already-set\n"
