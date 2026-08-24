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


def requirements_lock() -> str:
    return (ROOT / "requirements.lock").read_text()


def test_requirements_include_python_multipart_for_form_uploads():
    requirements = (ROOT / "requirements.txt").read_text()

    assert "python-multipart>=0.0.32" in requirements


def test_requirements_lock_pins_runtime_dependencies_with_hashes():
    lock = requirements_lock()

    assert "python-multipart==" in lock
    assert "fastapi==" in lock
    assert "uvicorn==" in lock
    assert "--hash=sha256:" in lock
    assert ">=" not in lock


def test_requirements_lock_excludes_dev_dependencies_and_the_project():
    lock = requirements_lock()

    assert "pytest==" not in lock
    assert "pyyaml==" not in lock
    assert "-e ." not in lock
    assert "testexplain==" not in lock


def test_render_configures_a_free_python_web_service():
    service = render_service()
    start_command = service["startCommand"]

    assert service["type"] == "web"
    assert service["name"] == "testexplain"
    assert service["runtime"] == "python"
    assert service["plan"] == "free"
    assert service["buildCommand"] == (
        "pip install --require-hashes -r requirements.lock"
    )
    assert "PYTHONPATH=src" in start_command
    assert "uvicorn testexplain.api:app" in start_command
    assert "--host 0.0.0.0" in start_command
    assert "--port $PORT" in start_command


def test_render_requires_explicit_trusted_proxy_configuration():
    service = render_service()

    env_vars = {variable["key"]: variable for variable in service["envVars"]}

    assert "--forwarded-allow-ips=*" not in service["startCommand"]
    assert env_vars["TRUSTED_PROXY_IPS"]["sync"] is False


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
