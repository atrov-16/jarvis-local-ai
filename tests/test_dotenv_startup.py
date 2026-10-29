import os
from pathlib import Path
from dotenv import load_dotenv

def test_dotenv_loads_openrouter_api_key(tmp_path: Path, monkeypatch) -> None:
    # conftest disables python-dotenv for the session; this test exercises it explicitly.
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED")

    # 1. Create a fake .env
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=test-sk-from-env-file")
    
    # 2. Simulate daemon/terminal startup logic. The conftest fixture starts every test
    # without OPENROUTER_API_KEY and removes the value load_dotenv sets afterwards.
    assert "OPENROUTER_API_KEY" not in os.environ
    load_dotenv(dotenv_path=env_file)
    
    # 3. Assert os.environ now has it
    assert os.getenv("OPENROUTER_API_KEY") == "test-sk-from-env-file"
    
    # 4. Prove SecretManager now sees it
    from jarvis.config.secrets import SecretManager
    # SecretManager uses os.environ by default
    manager = SecretManager()
    assert manager.get_openrouter_api_key() == "test-sk-from-env-file"
