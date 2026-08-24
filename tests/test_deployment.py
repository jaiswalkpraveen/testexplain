from functools import cache
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


@cache
def render_service() -> dict:
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    services = blueprint["services"]

    assert len(services) == 1
    return services[0]


def test_requirements_include_python_multipart_for_form_uploads():
    requirements = (ROOT / "requirements.txt").read_text()

    assert "python-multipart>=0.0.32" in requirements


def test_render_configures_a_free_python_web_service():
    service = render_service()
    start_command = service["startCommand"]

    assert service["type"] == "web"
    assert service["name"] == "testexplain"
    assert service["runtime"] == "python"
    assert service["plan"] == "free"
    assert service["buildCommand"] == "pip install -r requirements.txt"
    assert "PYTHONPATH=src" in start_command
    assert "uvicorn testexplain.api:app" in start_command
    assert "--host 0.0.0.0" in start_command
    assert "--port $PORT" in start_command


def test_render_prompts_for_credentials_and_defaults_the_model():
    env_vars = {
        variable["key"]: variable for variable in render_service()["envVars"]
    }

    assert env_vars["LLM_BASE_URL"]["sync"] is False
    assert env_vars["LLM_API_KEY"]["sync"] is False
    assert env_vars["LLM_MODEL"]["value"] == "gateframe/gemini-2.5-flash"


def test_asgi_app_imports():
    from testexplain.api import app

    assert callable(app)
