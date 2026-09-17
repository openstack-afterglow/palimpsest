"""Tests for Palimpsest Kolla-Ansible packaging and lifecycle assets."""

from __future__ import annotations

import subprocess
import tomllib
import zipfile
from pathlib import Path

import jinja2
import yaml

REPO_ROOT = Path(__file__).parent.parent
KOLLA_DIR = REPO_ROOT / "deploy" / "kolla"
ROLE_DIR = KOLLA_DIR / "ansible" / "roles" / "palimpsest"


def _get_hub_version() -> str:
    init_py = REPO_ROOT / "hub" / "src" / "palimpsest_hub" / "__init__.py"
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    raise RuntimeError("Could not determine hub_version from __init__.py")


hub_version = _get_hub_version()


def test_kolla_required_assets_exist():
    assert KOLLA_DIR.exists()
    assert (KOLLA_DIR / "pyproject.toml").exists()
    assert (KOLLA_DIR / "src" / "palimpsest_kolla" / "__init__.py").exists()

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


def test_version_lockstep_and_metadata():
    pyproject_data = tomllib.loads((KOLLA_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject_data["project"]["name"] == "palimpsest-kolla"
    assert pyproject_data["project"]["requires-python"] == ">=3.11"
    assert pyproject_data["tool"]["hatch"]["version"]["path"] == "../../hub/src/palimpsest_hub/__init__.py"

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


def test_palimpsest_kolla_wheel_build_and_install(tmp_path: Path):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    res = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=KOLLA_DIR,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"uv build failed: {res.stderr}"

    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1
    wheel_path = wheels[0]
    assert f"palimpsest_kolla-{hub_version}" in wheel_path.name

    with zipfile.ZipFile(wheel_path, "r") as zf:
        namelist = zf.namelist()
        data_prefix = f"palimpsest_kolla-{hub_version}.data/data/share/kolla-ansible/ansible/roles/palimpsest/"
        role_files = [n for n in namelist if n.startswith(data_prefix)]
        assert len(role_files) > 0, "No shared-data role files found in wheel"

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

    # Clean uninstall check
    res_uninst = subprocess.run(
        ["uv", "pip", "uninstall", "--python", str(venv_python), "palimpsest-kolla"],
        capture_output=True,
        text=True,
    )
    assert res_uninst.returncode == 0, f"uv pip uninstall failed: {res_uninst.stderr}"
    remaining_files = list(installed_role.glob("**/*")) if installed_role.exists() else []
    assert not [path for path in remaining_files if path.is_file()], "Uninstall left behind role files"
