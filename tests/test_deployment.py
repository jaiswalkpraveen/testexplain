from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_requirements_include_python_multipart_for_form_uploads():
    requirements = (ROOT / "requirements.txt").read_text()

    assert "python-multipart>=0.0.32" in requirements


def test_render_configures_a_free_python_web_service():
    render_config = (ROOT / "render.yaml").read_text()

    assert "type: web" in render_config
    assert "runtime: python" in render_config
    assert "plan: free" in render_config
    assert "buildCommand: pip install -r requirements.txt" in render_config
    assert "key: LLM_BASE_URL" in render_config
    assert "key: LLM_API_KEY" in render_config
    assert "key: LLM_MODEL" in render_config
    assert (
        "startCommand: PYTHONPATH=src uvicorn testexplain.api:app "
        "--host 0.0.0.0 --port $PORT"
    ) in render_config
