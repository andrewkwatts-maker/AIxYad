"""
pytest test: SprintRunner can be instantiated without exceptions.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sprint_runner import SprintRunner


def test_sprint_runner_instantiates(tmp_path):
    """SprintRunner should init without error given a temp project dir and Ollama config."""
    provider_config = {
        "endpoint": "http://localhost:11434",
        "model": "gemma3:4b",
        "num_predict": 2048,
        "temperature": 0.1,
    }

    runner = SprintRunner(
        project_dir=tmp_path,
        provider="ollama",
        provider_config=provider_config,
    )

    assert runner is not None


def test_sprint_runner_stores_project_dir(tmp_path):
    runner = SprintRunner(
        project_dir=tmp_path,
        provider="http_local_chat",
        provider_config={"endpoint": "http://localhost:11434", "model": "gemma3:4b"},
    )

    assert runner.project_dir == tmp_path


def test_sprint_runner_stores_provider(tmp_path):
    runner = SprintRunner(
        project_dir=tmp_path,
        provider="ollama",
        provider_config={"endpoint": "http://localhost:11434", "model": "gemma3:4b"},
    )

    assert runner.provider == "ollama"
