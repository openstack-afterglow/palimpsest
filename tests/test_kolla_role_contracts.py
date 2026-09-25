"""Palimpsest Kolla role lifecycle contracts: source pin, ports, Redis/Valkey
wiring, Keystone registration, bootstrap, and HAProxy public routing.

Recovered from the Afterglow monorepo's removed scripts/kolla-contract.test.js
(git show 249dd697) and rewritten against files this repo owns directly, with
no dependency on an Afterglow checkout. Assertions that exercised Afterglow-
owned files (the afterglow role, deploy/kolla/site.yml, and
globals.afterglow.sample.yml) or the retired per-service "*-kolla" PyPI
distribution model are intentionally not reproduced here; they are either out
of this repo's ownership or describe a packaging mechanism this repo no
longer uses (see pyproject.toml's wheel shared-data target).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE_DIR = REPO_ROOT / "deploy" / "kolla" / "ansible" / "roles" / "palimpsest"

defaults_text = (ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8")
defaults_yaml = yaml.safe_load(defaults_text)
precheck_text = (ROLE_DIR / "tasks" / "precheck.yml").read_text(encoding="utf-8")
source_build_text = (ROLE_DIR / "tasks" / "source_build.yml").read_text(encoding="utf-8")
bootstrap_text = (ROLE_DIR / "tasks" / "bootstrap_service.yml").read_text(encoding="utf-8")
keystone_text = (ROLE_DIR / "tasks" / "preconditions_keystone.yml").read_text(encoding="utf-8")
loadbalancer_text = (ROLE_DIR / "tasks" / "loadbalancer.yml").read_text(encoding="utf-8")
validator_text = (ROLE_DIR / "files" / "validate_image_ref.py").read_text(encoding="utf-8")


def test_source_build_pins_immutable_commit_and_repo():
    assert re.search(r'^palimpsest_source_version: "[0-9a-f]{40}"$', defaults_text, re.MULTILINE)
    assert re.search(
        r'^palimpsest_source_repo: "https://github\.com/openstack-afterglow/palimpsest\.git"$',
        defaults_text,
        re.MULTILINE,
    )
    # Source-build fails closed on a dirty or wrong-SHA checkout rather than silently rebuilding.
    assert "Fail if existing Palimpsest checkout is dirty" in source_build_text
    assert "palimpsest_existing_git_status.stdout | trim | length == 0" in source_build_text
    assert "palimpsest_existing_head_sha.stdout | trim == palimpsest_source_version" in source_build_text
    assert "palimpsest_head_sha.stdout | trim == palimpsest_source_version" in source_build_text


def test_source_build_targets_both_hub_images_from_local_dockerfile():
    assert 'dockerfile: "docker/hub/Dockerfile"' in source_build_text
    assert "- palimpsest-hub-api" in source_build_text
    assert "- palimpsest-hub-worker" in source_build_text
    assert re.search(r'name: "afterglow-local/\{\{ item \}\}:', source_build_text)


def test_api_and_worker_ports_commands_and_health_path():
    services = defaults_yaml["palimpsest_services"]
    assert defaults_yaml["palimpsest_api_port"] == 8020
    assert defaults_yaml["palimpsest_api_listen_port"] == 18020
    api_command = services["palimpsest-hub-api"]["command"]
    assert api_command[:2] == ["uvicorn", "palimpsest_hub.main:app"]
    assert "--port" in api_command and "{{ palimpsest_api_listen_port | string }}" in api_command
    assert services["palimpsest-hub-worker"]["command"] == ["palimpsest-hub-worker"]
    assert "/v1/health" in services["palimpsest-hub-api"]["healthcheck"]["test"][3]


def test_container_env_values_are_explicitly_stringified():
    """community.docker.docker_container rejects non-string env values.

    Ansible renders Jinja with native types, so an env expression that yields a
    bool/int (``| bool``, ``not …``, an integer default) reaches the module as a
    non-string and deploy fails with "Non-string value found for env option".
    Every such expression in the service definitions and the bootstrap task must
    end in ``| string``. Plain variable references are passed through as-is.
    """
    bootstrap_env = next(
        task["community.docker.docker_container"]["env"]
        for task in yaml.safe_load(bootstrap_text)
        if "env" in task.get("community.docker.docker_container", {})
    )
    env_maps = {
        "bootstrap": bootstrap_env,
        **{name: svc["environment"] for name, svc in defaults_yaml["palimpsest_services"].items()},
    }
    non_string_expression = re.compile(r"\| *bool\b|\bnot\b|_bytes|_operations|_port\b")
    for owner, mapping in env_maps.items():
        for key, template in mapping.items():
            if non_string_expression.search(template):
                assert re.search(r"\| *string *\}\}\s*$", template), (
                    f"{owner}.{key} = {template!r} must end with '| string' for docker_container env"
                )


def test_redis_db_index_pinned_to_nine_and_hub_volume_paths():
    assert defaults_yaml["palimpsest_redis_db_index"] == 9
    assert defaults_yaml["palimpsest_hub_volume"] == "palimpsest_hub"
    assert defaults_yaml["palimpsest_hub_path"] == "/var/lib/palimpsest/hub"
    # Precheck independently guards the same invariant so a defaults edit cannot silently drift it.
    assert "palimpsest_redis_db_index == 9" in precheck_text


def test_keystone_registers_dedicated_service_project_and_user():
    assert "type: palimpsest" in keystone_text
    assert "name: palimpsest" in keystone_text
    assert defaults_yaml["palimpsest_service_project_name"] == "palimpsest-service"
    assert defaults_yaml["palimpsest_keystone_user"] == "palimpsest"
    assert 'OS_PROJECT_NAME: "{{ palimpsest_service_project_name }}"' in defaults_text
    assert defaults_yaml["palimpsest_database_url"].startswith("mysql+asyncmy:")


def test_bootstrap_runs_dedicated_command_without_automatic_data_migration():
    assert 'command: ["palimpsest-hub-bootstrap"]' in bootstrap_text
    assert 'OS_PROJECT_NAME: "{{ palimpsest_service_project_name }}"' in bootstrap_text
    assert "palimpsest-hub-migrate-data" not in bootstrap_text


def test_validator_recognizes_local_source_build_image_naming():
    assert "afterglow-local/palimpsest-hub-" in validator_text


def test_valkey_dependency_wiring_matches_stock_kolla_inventory():
    assert defaults_yaml["palimpsest_valkey_host"] == "{{ 'api' | kolla_address(groups['valkey'][0]) }}"
    assert defaults_yaml["palimpsest_valkey_port"] == "{{ valkey_server_port }}"
    assert "valkey_master_password" in defaults_yaml["palimpsest_valkey_password"]
    assert defaults_yaml["palimpsest_redis_url"] == (
        "redis://default:{{ palimpsest_valkey_password }}@{{ palimpsest_valkey_host }}:"
        "{{ palimpsest_valkey_port }}/{{ palimpsest_redis_db_index }}"
    )


def test_precheck_requires_stock_valkey_with_no_plugin_redis_fallback():
    assert "name: Precheck | Verify stock Kolla Valkey dependency" in precheck_text
    assert "enable_valkey | default(false) | bool" in precheck_text
    assert "groups.get('valkey', []) | length > 0" in precheck_text
    assert "valkey_master_password is defined and valkey_master_password | length > 0" in precheck_text
    assert "Deploy stock Kolla Valkey before enabling palimpsest; no plugin Redis fallback exists" in precheck_text
    assert "run_once: true" in precheck_text
    assert "tags: precheck" in precheck_text
    assert "when: enable_palimpsest | default(false) | bool" in precheck_text


def test_haproxy_public_route_defaults_are_disabled_and_combine_into_services():
    assert defaults_yaml["palimpsest_public_haproxy_enabled"] is False
    assert defaults_yaml["palimpsest_public_haproxy_fqdn"] == ""
    assert defaults_yaml["palimpsest_haproxy_services"] == (
        "{{ palimpsest_services | combine(palimpsest_public_haproxy_services, recursive=True) }}"
    )
    public_haproxy = defaults_yaml["palimpsest_public_haproxy_services"]["palimpsest-public"]["haproxy"][
        "palimpsest-public"
    ]
    assert public_haproxy["external_fqdn"] == "{{ palimpsest_public_haproxy_fqdn }}"


def test_loadbalancer_task_wires_combined_haproxy_services_and_prunes_when_disabled():
    assert 'project_services: "{{ palimpsest_haproxy_services }}"' in loadbalancer_text
    assert "not (palimpsest_public_haproxy_enabled | default(false) | bool)" in loadbalancer_text


def test_precheck_validates_public_hostname_matches_endpoint_url():
    assert "palimpsest_public_haproxy_fqdn is match(" in precheck_text
    assert (
        "(palimpsest_public_endpoint_url | regex_replace('/$', '')) == ('https://' ~ palimpsest_public_haproxy_fqdn)"
    ) in precheck_text
