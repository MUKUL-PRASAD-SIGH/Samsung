"""Deployment artifacts must stay consistent with the code and with each other (they are only exercised by CI/Docker,
so nothing else notices when they rot)."""

import re
import shutil
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest

from agent.settings import Settings

try:
    import tomllib
except ModuleNotFoundError:     # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
yaml = pytest.importorskip("yaml")


def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_ci_python_matrix_matches_requires_python():
    spec = _pyproject()["project"]["requires-python"]            # ">=3.10,<3.13"
    lo = tuple(int(x) for x in re.search(r">=(\d+)\.(\d+)", spec).groups())
    hi = tuple(int(x) for x in re.search(r"<(\d+)\.(\d+)", spec).groups())
    wanted = {f"{lo[0]}.{m}" for m in range(lo[1], hi[1])}
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert set(ci["jobs"]["test"]["strategy"]["matrix"]["python"]) == wanted


def test_workflows_are_valid_and_gate_what_they_claim():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert {"lint", "secrets", "test", "frontend", "android", "docker"} <= set(ci["jobs"])
    steps = " ".join(str(s.get("run", "")) for s in ci["jobs"]["test"]["steps"])
    assert "pytest" in steps and "--fail-under" in steps and "--virtual" in steps and "--set all" in steps
    nightly = yaml.safe_load((ROOT / ".github/workflows/nightly-live-eval.yml").read_text())
    assert "schedule" in nightly[True] or "schedule" in nightly.get("on", {})      # PyYAML reads the key `on` as True
    assert "GROQ_API_KEY" in (ROOT / ".github/workflows/nightly-live-eval.yml").read_text()


def test_every_setting_is_documented_in_env_example():
    assert fields(Settings)                                  # (the dataclass is what from_env() populates)
    text = (ROOT / ".env.example").read_text()
    names = {"AUTH_TOKEN", "ALLOWED_ORIGINS", "TRUST_PROXY", "MAX_CONNECTIONS_PER_IP", "MAX_SESSIONS", "MSG_RATE_PER_S", "MSG_BURST",
             "AUDIO_BYTES_PER_S", "AUDIO_BURST_BYTES", "MAX_TEXT_MESSAGE_BYTES", "MAX_USER_TEXT_CHARS", "WARMUP_MIN_INTERVAL_S",
             "LOG_FORMAT", "LOG_LEVEL", "METRICS_ENABLED"}
    source = (ROOT / "agent/settings.py").read_text()
    read_by_code = set(re.findall(r'(?:_bool|_int|_float|_list|getenv)\("([A-Z][A-Z_]+)"', source[source.index("def from_env"):]))
    assert names == read_by_code, f"settings.py reads {read_by_code ^ names} that this test / .env.example disagree on"
    assert not [n for n in names if n not in text], "undocumented in .env.example"


def test_lock_file_pins_direct_dependencies_and_lists_no_gpu_wheels_in_the_cpu_image():
    lock = (ROOT / "requirements.lock").read_text().lower()
    for dep in ("fastapi", "uvicorn", "websockets", "pydantic", "faster-whisper", "onnxruntime", "sentence-transformers", "piper-tts"):
        assert re.search(rf"^{dep}==", lock, re.M), f"{dep} is not pinned in requirements.lock"
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "nvidia" in dockerfile and "download.pytorch.org/whl/cpu" in dockerfile      # the CUDA pins are filtered out


def test_dockerfile_is_multi_stage_non_root_and_healthchecked():
    d = (ROOT / "Dockerfile").read_text()
    assert d.count("\nFROM ") + d.startswith("FROM ") >= 3
    assert "USER app" in d and "HEALTHCHECK" in d and "frontend/dist" in d and "scripts/fetch_models.py" in d
    ignore = (ROOT / ".dockerignore").read_text()
    assert ".env" in ignore.splitlines() and "frontend/node_modules" in ignore     # secrets and host node_modules never enter the image


def test_compose_is_valid_and_keeps_the_app_off_the_public_interface_by_default():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["app"]["ports"] == ["127.0.0.1:8000:8000"]
    assert compose["services"]["caddy"]["profiles"] == ["https"]
    assert compose["services"]["app"]["read_only"] is True
    assert "Caddyfile" in str(compose["services"]["caddy"]["volumes"])
    if shutil.which("docker"):
        r = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "config", "-q"], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin:/usr/local/bin"})      # no DOMAIN: plain `up` must work
        assert r.returncode == 0, r.stderr


def test_ruff_config_gates_correctness_rules_only():
    cfg = _pyproject()["tool"]["ruff"]["lint"]
    assert "F" in cfg["select"] and "E9" in cfg["select"]


def test_setuptools_only_packages_the_agent():
    """A fresh `pip install -e .` failed with 'Multiple top-level packages discovered' until this was set."""
    assert _pyproject()["tool"]["setuptools"]["packages"]["find"]["include"] == ["agent*"]


# ------------------------------------------------------------------------------------------ desktop packaging
def test_desktop_build_workflow_is_valid_and_builds_the_installer():
    wf = yaml.safe_load((ROOT / ".github/workflows/build-desktop.yml").read_text())
    job = wf["jobs"]["windows"]
    assert job["runs-on"] == "windows-latest" and wf["permissions"]["contents"] == "write"
    text = (ROOT / ".github/workflows/build-desktop.yml").read_text()
    for needle in ("pyinstaller packaging/kairos.spec", "installer.iss", "Kairos.exe", "/settings/keys", "gh release upload"):
        assert needle in text


def test_pyinstaller_spec_is_valid_python_and_keeps_torch_out():
    src = (ROOT / "packaging/kairos.spec").read_text()
    compile(src, "kairos.spec", "exec")
    assert '"torch"' in src and "frontend/dist" in src and "intent_weights.json" in src
    assert (ROOT / "packaging/kairos_app.py").exists() and (ROOT / "packaging/kairos.ico").stat().st_size > 1000


def test_installer_script_installs_the_pyinstaller_output():
    iss = (ROOT / "packaging/installer.iss").read_text()
    assert r"..\dist\Kairos\*" in iss and "Kairos.exe" in iss and "AppVersion" in iss
