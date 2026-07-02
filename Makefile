# TestLens development shortcuts

.PHONY: test run-cli run-cli-fake run-api clean

# Run all tests
test:
	uv run pytest -v

# Run the CLI against the sample report (needs ANTHROPIC_API_KEY for real LLM)
run-cli:
	uv run testlens analyze tests/fixtures/sample_report.json

# Run the CLI with the fake gateway (no API key needed)
run-cli-fake:
	uv run testlens analyze tests/fixtures/sample_report.json --fake

# Run the FastAPI server; then open http://127.0.0.1:8000/docs
run-api:
	uv run uvicorn testlens.api:app --reload

# Remove caches and build artifacts
clean:
	rm -rf __pycache__ dist/ .pytest_cache/