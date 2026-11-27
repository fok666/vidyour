import re
import tomllib
from pathlib import Path

from app.config import Settings


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_DIR.parent
DEPENDENCY_PATTERN = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[(?P<extras>[A-Za-z0-9._,-]+)\])?"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)"
    r"(?:\s*;\s*(?P<marker>.+))?"
)


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pinned_requirements(requirements: list[str]) -> dict[str, tuple[str, set[str]]]:
    result = {}
    for requirement in requirements:
        match = DEPENDENCY_PATTERN.fullmatch(requirement)
        assert match is not None, f"dependency is not exact-pinned: {requirement}"
        name = _normalized_name(match.group("name"))
        assert name not in result, f"duplicate direct dependency: {name}"
        result[name] = (
            match.group("version"),
            set((match.group("extras") or "").split(",")) - {""},
        )
    return result


def _lock_references(
    references: list[dict], expected: dict[str, tuple[str, set[str]]], lock_packages
) -> None:
    references_by_name = {
        _normalized_name(reference["name"]): reference for reference in references
    }
    assert set(references_by_name) == set(expected)

    packages_by_name = {}
    for package in lock_packages:
        packages_by_name.setdefault(_normalized_name(package["name"]), set()).add(
            package["version"]
        )

    for name, (version, extras) in expected.items():
        reference = references_by_name[name]
        assert set(reference.get("extra", [])) == extras
        assert packages_by_name.get(name) == {version}


def test_backend_direct_dependencies_are_exact_pinned_and_locked():
    pyproject = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text())
    lock = tomllib.loads((BACKEND_DIR / "uv.lock").read_text())

    build = _pinned_requirements(pyproject["build-system"]["requires"])
    runtime = _pinned_requirements(pyproject["project"]["dependencies"])
    development = _pinned_requirements(
        pyproject["project"]["optional-dependencies"]["dev"]
    )
    assert build

    project_name = _normalized_name(pyproject["project"]["name"])
    locked_project = [
        package
        for package in lock["package"]
        if _normalized_name(package["name"]) == project_name
        and package.get("source", {}).get("editable") == "."
    ]
    assert len(locked_project) == 1, "uv.lock must contain the editable backend"

    project_lock = locked_project[0]
    _lock_references(project_lock.get("dependencies", []), runtime, lock["package"])
    optional_dependencies = project_lock.get("optional-dependencies", {})
    assert set(optional_dependencies) == {"dev"}
    _lock_references(
        optional_dependencies["dev"], development, lock["package"]
    )


def test_readme_documents_frozen_backend_install_and_server_command():
    readme = (REPOSITORY_ROOT / "README.md").read_text()

    assert re.search(
        r"(?m)^cd backend\n"
        r"uv sync --frozen --extra dev\n"
        r"DATA_DIR=\.\./data \.venv/bin/uvicorn "
        r"--factory app\.main:create_app --port 8000$",
        readme,
    )


def test_real_settings_select_local_fallbacks_without_provider_credentials(
    monkeypatch,
):
    for name in (
        "GEMINI_API_KEY",
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REDIRECT_URI",
        "JAMENDO_CLIENT_ID",
        "FREESOUND_API_KEY",
        "S3_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_PROVIDER", "auto")

    settings = Settings(_env_file=None)
    capabilities = settings.capabilities()

    assert capabilities["active_ai_provider"] == "local"
    assert capabilities["storage_backend"] == "local"


def _release(version: str) -> tuple[int, ...]:
    release = re.match(r"\d+(?:\.\d+)*", version)
    assert release is not None, f"version has no numeric release: {version}"
    return tuple(int(part) for part in release.group(0).split("."))


def test_locked_numpy_and_boto3_meet_version_floors():
    floors = {"numpy": "2.4.6", "boto3": "1.44.10"}
    lock = tomllib.loads((BACKEND_DIR / "uv.lock").read_text())

    locked: dict[str, list[str]] = {}
    for package in lock["package"]:
        name = _normalized_name(package["name"])
        if name in floors:
            locked.setdefault(name, []).append(package["version"])
    assert set(floors) <= set(locked)

    below = sorted(
        f"{name}=={version}"
        for name, floor in floors.items()
        for version in locked[name]
        if _release(version) < _release(floor)
    )
    assert below == []


def test_build_requirement_setuptools_meets_version_floor():
    floors = {"setuptools": "84.2.1"}
    pyproject = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text())

    build = _pinned_requirements(pyproject["build-system"]["requires"])
    found = {name: version for name, (version, _) in build.items()}

    stale = sorted(
        f"{name}=={found[name]}"
        for name, floor in floors.items()
        if name in found and _release(found[name]) < _release(floor)
    )
    missing = sorted(name for name in floors if name not in found)
    assert stale + missing == []
