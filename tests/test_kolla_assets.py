"""Tests for root-wheel Kolla-Ansible role data and lifecycle assets."""

from __future__ import annotations

import subprocess
import tomllib
import zipfile
from pathlib import Path

import jinja2
import yaml

REPO_ROOT = Path(__file__).parent.parent
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"
KOLLA_DIR = REPO_ROOT / "deploy" / "kolla"
ROLE_DIR = KOLLA_DIR / "ansible" / "roles" / "palimpsest"


def _get_hub_version() -> str:
    init_py = REPO_ROOT / "hub" / "src" / "palimpsest_hub" / "__init__.py"
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    raise RuntimeError("Could not determine hub_version from __init__.py")


root_pyproject = tomllib.loads(ROOT_PYPROJECT.read_text(encoding="utf-8"))
root_version = root_pyproject["project"]["version"]
hub_version = _get_hub_version()


def test_kolla_required_assets_exist_without_standalone_package():
    assert KOLLA_DIR.exists()
    assert not (KOLLA_DIR / "pyproject.toml").exists()
    assert not (KOLLA_DIR / "src").exists()

    required_role_files = [
        "defaults/main.yml",
        "files/validate_image_ref.py",
        "handlers/main.yml",
        "meta/main.yml",
        "templates/palimpsest.conf.j2",
        "vars/main.yml",
        "tasks/main.yml",
        "tasks/deploy.yml",
        "tasks/reconfigure.yml",
        "tasks/upgrade.yml",
        "tasks/precheck.yml",
        "tasks/pull.yml",
        "tasks/config.yml",
        "tasks/bootstrap_service.yml",
        "tasks/start.yml",
        "tasks/destroy.yml",
        "tasks/loadbalancer.yml",
        "tasks/source_build.yml",
        "tasks/image_precheck.yml",
        "tasks/preconditions.yml",
        "tasks/preconditions_keystone.yml",
        "tasks/preconditions_db.yml",
    ]

    for relative_path in required_role_files:
        path = ROLE_DIR / relative_path
        assert path.exists(), f"Missing required role asset: {relative_path}"


def test_root_metadata_owns_kolla_shared_data_without_runtime_dependencies():
    project = root_pyproject["project"]
    assert project["name"] == "palimpsest-local"
    assert project["dependencies"] == []
    assert "service" not in project["optional-dependencies"]
    assert all(
        "kolla-ansible" not in requirement
        for requirements in project["optional-dependencies"].values()
        for requirement in requirements
    )
    assert "palimpsest-hub" not in project["dependencies"]
    assert root_pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["shared-data"] == {
        "deploy/kolla/ansible/roles/palimpsest": "share/kolla-ansible/ansible/roles/palimpsest"
    }

    defaults_yaml = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert defaults_yaml["palimpsest_image_tag"] == hub_version


def test_all_yaml_files_parse():
    for yml_file in ROLE_DIR.rglob("*.yml"):
        content = yml_file.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        assert parsed is not None or yml_file.name == "main.yml", f"YAML file parsed to None or empty: {yml_file}"


def test_jinja_templates_compile():
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    for template_file in (ROLE_DIR / "templates").glob("*.j2"):
        content = template_file.read_text(encoding="utf-8")
        assert env.parse(content) is not None


def test_root_wheel_builds_and_installs_kolla_shared_data(tmp_path: Path):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    res = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"uv build failed: {res.stderr}"

    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1
    wheel_path = wheels[0]
    assert f"palimpsest_local-{root_version}" in wheel_path.name

    with zipfile.ZipFile(wheel_path, "r") as zf:
        namelist = zf.namelist()
        data_prefix = f"palimpsest_local-{root_version}.data/data/share/kolla-ansible/ansible/roles/palimpsest/"
        role_files = [name for name in namelist if name.startswith(data_prefix)]
        assert role_files, "No shared-data role files found in wheel"

    venv_dir = tmp_path / "venv"
    res_venv = subprocess.run(["uv", "venv", str(venv_dir)], capture_output=True, text=True)
    assert res_venv.returncode == 0, f"uv venv failed: {res_venv.stderr}"

    venv_python = venv_dir / "bin" / "python"
    res_inst = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_python), "--no-deps", str(wheel_path)],
        capture_output=True,
        text=True,
    )
    assert res_inst.returncode == 0, f"uv pip install failed: {res_inst.stderr}"

    installed_role = venv_dir / "share" / "kolla-ansible" / "ansible" / "roles" / "palimpsest"
    assert (installed_role / "defaults" / "main.yml").is_file()
    assert (installed_role / "tasks" / "main.yml").is_file()
    assert (installed_role / "templates" / "palimpsest.conf.j2").is_file()

    res_uninst = subprocess.run(
        ["uv", "pip", "uninstall", "--python", str(venv_python), "palimpsest-local"],
        capture_output=True,
        text=True,
    )
    assert res_uninst.returncode == 0, f"uv pip uninstall failed: {res_uninst.stderr}"
    remaining_files = list(installed_role.glob("**/*")) if installed_role.exists() else []
    assert not [path for path in remaining_files if path.is_file()], "Uninstall left behind role files"
