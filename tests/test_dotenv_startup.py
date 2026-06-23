import os
from pathlib import Path
from dotenv import load_dotenv

def test_dotenv_loads_openrouter_api_key(tmp_path: Path) -> None:
    # 1. Create a fake .env
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=test-sk-from-env-file")
    
    # 2. Simulate daemon/terminal startup logic (clear env to simulate empty session)
    if "OPENROUTER_API_KEY" in os.environ:
        del os.environ["OPENROUTER_API_KEY"]
    load_dotenv(dotenv_path=env_file)
    
    # 3. Assert os.environ now has it
    assert os.getenv("OPENROUTER_API_KEY") == "test-sk-from-env-file"
    
    # 4. Prove SecretManager now sees it
    from jarvis.config.secrets import SecretManager
    # SecretManager uses os.environ by default
    manager = SecretManager()
    assert manager.get_openrouter_api_key() == "test-sk-from-env-file"

    # Clean up so we don't pollute other tests
    del os.environ["OPENROUTER_API_KEY"]
