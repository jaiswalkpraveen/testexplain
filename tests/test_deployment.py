from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def test_requirements_include_python_multipart_for_form_uploads():
    requirements = (ROOT / "requirements.txt").read_text()

    assert "python-multipart>=0.0.32" in requirements


def test_render_configures_a_free_python_web_service():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    services = blueprint["services"]
    service = services[0]

    assert len(services) == 1
    assert service["type"] == "web"
    assert service["name"] == "testexplain"
    assert service["runtime"] == "python"
    assert service["plan"] == "free"
    assert service["buildCommand"] == "pip install -r requirements.txt"
    assert service["startCommand"] == (
        "PYTHONPATH=src uvicorn testexplain.api:app --host 0.0.0.0 --port $PORT"
    )


def test_render_prompts_for_credentials_and_defaults_the_model():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    env_vars = blueprint["services"][0]["envVars"]

    assert env_vars == [
        {"key": "LLM_BASE_URL", "sync": False},
        {"key": "LLM_API_KEY", "sync": False},
        {"key": "LLM_MODEL", "value": "gateframe/gemini-2.5-flash"},
    ]
