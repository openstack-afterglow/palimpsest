"""Strict palimpsest.yml parsing, normalization, and safety contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from palimpsest_local.project import (
    DEFAULT_PROJECT_FILE,
    ProjectError,
    canonical_project_json,
    canonical_project_payload,
    deterministic_project_name,
    deterministic_service_name,
    interpolate,
    load_interpolation_environment,
    load_project,
    parse_yaml_subset,
    project_config_digest,
    resolve_cloud_init,
    resolve_service_environment,
    service_start_order,
)

BASE = "sha256:" + "a" * 64
LAYER = "sha256:" + "b" * 64
DB_BASE = "sha256:" + "c" * 64


def _write_project(tmp_path: Path, text: str) -> Path:
    path = tmp_path / DEFAULT_PROJECT_FILE
    path.write_text(text, encoding="utf-8")
    return path


def _complete_project(tmp_path: Path) -> Path:
    (tmp_path / ".env.runtime").write_text("LOG_LEVEL=info\n", encoding="utf-8")
    bundle = tmp_path / "database-bundle"
    bundle.mkdir()
    (bundle / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")
    return _write_project(
        tmp_path,
        f"""version: "1"
name: demo
networks:
  frontend:
    driver: bridge
  backend:
    driver: isolated
volumes:
  database:
    driver: block
    size: 20GiB
  assets: {{}}
services:
  db:
    bundle: database-bundle
    networks: [backend]
    volumes:
      - source: database
        target: /var/lib/database
        read_only: false
  api:
    image: {BASE}
    layers:
      - {LAYER}
    memory: 2GiB
    vcpus: 4
    depends_on: [db]
    networks: [frontend]
    volumes:
      - database:/mnt/database:ro
      - type: volume
        source: assets
        target: /srv/data
        read_only: true
    ports:
      - "8080:80"
      - target: 53
        published: 5353
        protocol: tcp
        host_ip: 127.0.0.2
    environment:
      LOG_LEVEL: ${{LOG_LEVEL:-info}}
      API_TOKEN: ${{API_TOKEN:?API_TOKEN is required}}
    env_file: .env.runtime
    cloud_init:
      inline:
        packages: [curl, ca-certificates]
        write_files:
          - path: /etc/demo.conf
            permissions: "0640"
            content: |-
              log_level=${{LOG_LEVEL:-info}}
        runcmd:
          - [systemctl, enable, demo.service]
""",
    )


def test_loads_and_normalizes_complete_project(tmp_path: Path) -> None:
    path = _complete_project(tmp_path)

    project = load_project(path, {"API_TOKEN": "runtime-only-token", "LOG_LEVEL": "debug"})

    assert project.version == "1"
    assert project.name == "demo"
    assert tuple(project.services) == ("api", "db")
    assert project.services["api"].image == BASE
    assert project.services["api"].layers == (LAYER,)
    assert project.services["api"].memory_mib == 2048
    assert project.services["api"].vcpus == 4
    assert project.services["api"].networks == ("frontend",)
    assert project.services["api"].ports[0].host_ip == "127.0.0.1"
    assert project.services["api"].ports[0].host_port == 8080
    assert project.services["api"].ports[1].protocol == "tcp"
    assert project.services["api"].volumes[0].type == "volume"
    assert project.services["api"].volumes[1].source == "assets"
    assert project.services["db"].bundle is not None
    assert project.services["db"].bundle.path == (tmp_path / "database-bundle").resolve()
    assert project.volumes["database"].size_mib == 20 * 1024
    assert project.networks["default"].driver == "nat"
    assert project.services["api"].cloud_init is not None
    assert project.services["api"].cloud_init.write_files[0].content == "log_level=${LOG_LEVEL:-info}"
    assert project.services["api"].cloud_init.runcmd == (("systemctl", "enable", "demo.service"),)
    assert service_start_order(project) == ("db", "api")
    assert service_start_order(project, ["api"]) == ("db", "api")
    assert resolve_service_environment(project.services["api"], {"API_TOKEN": "secret", "LOG_LEVEL": "trace"}) == {
        "API_TOKEN": "secret",
        "LOG_LEVEL": "info",
    }


def test_canonical_payload_is_deterministic_relative_and_never_resolves_secrets(tmp_path: Path) -> None:
    project = load_project(_complete_project(tmp_path), {"API_TOKEN": "super-secret-runtime-value"})

    payload = canonical_project_payload(project)
    encoded = canonical_project_json(project)

    assert json.loads(encoded) == payload
    assert encoded.endswith("\n")
    assert str(tmp_path) not in encoded
    assert "super-secret-runtime-value" not in encoded
    assert payload["services"]["api"]["environment"]["API_TOKEN"] == "${API_TOKEN:?API_TOKEN is required}"
    assert payload["services"]["api"]["env_file"] == [".env.runtime"]
    assert payload["services"]["db"]["bundle"] == "database-bundle"
    assert project_config_digest(project) == project_config_digest(project)


def test_cloud_init_file_is_parsed_as_typed_subset(tmp_path: Path) -> None:
    (tmp_path / "cloud-init.yml").write_text(
        """packages: [jq]
write_files:
  - path: /etc/app.conf
    content: "mode=${MODE:-safe}"
runcmd:
  - [systemctl, restart, app]
""",
        encoding="utf-8",
    )
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    cloud_init:
      file: cloud-init.yml
""",
    )

    project = load_project(path, {})
    cloud = project.services["app"].cloud_init

    assert cloud is not None
    assert cloud.packages == ("jq",)
    assert cloud.source is not None and cloud.source.reference == "cloud-init.yml"
    assert cloud.runcmd == (("systemctl", "restart", "app"),)
    assert cloud.write_files[0].content == "mode=${MODE:-safe}"


@pytest.mark.parametrize(
    ("expression", "environment", "expected"),
    [
        ("${VALUE}", {"VALUE": "set"}, "set"),
        ("${VALUE:-fallback}", {}, "fallback"),
        ("${VALUE:-fallback}", {"VALUE": "set"}, "set"),
        ("cost=$$5", {}, "cost=$5"),
    ],
)
def test_interpolation_supported_subset(expression: str, environment: dict[str, str], expected: str) -> None:
    assert interpolate(expression, environment) == expected


@pytest.mark.parametrize("expression", ["$VALUE", "${VALUE-default}", "${VALUE:+x}", "${VALUE:-${OTHER}}"])
def test_interpolation_rejects_unsupported_or_nested_forms(expression: str) -> None:
    with pytest.raises(ProjectError, match="interpolation"):
        interpolate(expression, {"VALUE": "set", "OTHER": "x"})


def test_all_structural_string_scalars_interpolate_before_validation(tmp_path: Path) -> None:
    (tmp_path / "runtime.env").write_text("PUBLIC=value\n", encoding="utf-8")
    path = _write_project(
        tmp_path,
        """version: "${VERSION:-1}"
name: "${PROJECT_NAME}"
networks:
  appnet:
    driver: "${NETWORK_DRIVER:-nat}"
volumes:
  data:
    driver: "${VOLUME_DRIVER:-block}"
    size: "${VOLUME_SIZE:-1GiB}"
services:
  db:
    image: "${DB_IMAGE}"
    networks: [appnet]
  app:
    image: "${APP_IMAGE}"
    layers: ["${APP_LAYER}"]
    memory: "${MEMORY:-2GiB}"
    vcpus: "${VCPUS:-3}"
    networks: ["${NETWORK_NAME:-appnet}"]
    volumes: ["${VOLUME_NAME:-data}:${MOUNT_TARGET:-/data}:ro"]
    ports: ["${HOST_PORT:-8080}:${GUEST_PORT:-80}"]
    env_file: "${ENV_FILE:-runtime.env}"
    depends_on: ["${DEPENDENCY:-db}"]
""",
    )
    environment = {
        "PROJECT_NAME": "interpolated",
        "DB_IMAGE": DB_BASE,
        "APP_IMAGE": BASE,
        "APP_LAYER": LAYER,
    }

    project = load_project(path, environment)

    assert project.name == "interpolated"
    assert project.services["app"].memory_mib == 2048
    assert project.services["app"].vcpus == 3
    assert project.services["app"].volumes[0].target == "/data"
    assert project.services["app"].ports[0].host_port == 8080
    assert project.services["app"].env_files[0].reference == "runtime.env"
    assert project.services["app"].depends_on == ("db",)
    assert project.volumes["data"].size_mib == 1024
    assert "${APP_IMAGE}" not in canonical_project_json(project)


def test_single_quoted_yaml_scalar_disables_interpolation(tmp_path: Path) -> None:
    path = _write_project(
        tmp_path,
        """services:
  app:
    image: '${APP_IMAGE}'
""",
    )
    with pytest.raises(ProjectError, match="sha256 cloud-image digest"):
        load_project(path, {"APP_IMAGE": BASE})

    path.write_text(
        f"""services:
  app:
    image: {BASE}
    environment:
      LITERAL: '${{NOT_EXPANDED}}'
""",
        encoding="utf-8",
    )
    project = load_project(path, {})
    assert resolve_service_environment(project.services["app"], {})["LITERAL"] == "${NOT_EXPANDED}"


def test_required_execution_templates_are_deferred_until_runtime_resolution(tmp_path: Path) -> None:
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    environment:
      REQUIRED: ${{MISSING:?set MISSING}}
    cloud_init:
      inline:
        write_files:
          - path: /etc/app.conf
            content: "required=${{MISSING:?set MISSING}}"
""",
    )

    project = load_project(path, {})

    with pytest.raises(ProjectError, match="set MISSING"):
        resolve_service_environment(project.services["app"], {})
    cloud = project.services["app"].cloud_init
    assert cloud is not None
    with pytest.raises(ProjectError, match="set MISSING"):
        resolve_cloud_init(cloud, {})


def test_required_structural_interpolation_still_fails_during_load(tmp_path: Path) -> None:
    path = _write_project(
        tmp_path,
        """services:
  app:
    image: "${IMAGE:?set IMAGE}"
""",
    )

    with pytest.raises(ProjectError, match="set IMAGE"):
        load_project(path, {})


def test_env_files_merge_in_order_then_inline_environment_wins(tmp_path: Path) -> None:
    (tmp_path / "first.env").write_text(
        """# a comment
BASE=first
QUOTED="two words"
SINGLE='literal value'
""",
        encoding="utf-8",
    )
    (tmp_path / "second.env").write_text(
        """BASE=second
DERIVED=${BASE}
WITH_COMMENT=value # ignored
""",
        encoding="utf-8",
    )
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    env_file: [first.env, second.env]
    environment:
      BASE: inline
      FINAL: ${{DERIVED}}
""",
    )
    project = load_project(path, {"BASE": "host"})

    assert resolve_service_environment(project.services["app"], {"BASE": "host"}) == {
        "BASE": "inline",
        "QUOTED": "two words",
        "SINGLE": "literal value",
        "DERIVED": "second",
        "WITH_COMMENT": "value",
        "FINAL": "second",
    }
    encoded = canonical_project_json(project)
    assert "two words" not in encoded
    assert '"env_file":["first.env","second.env"]' in encoded


def test_env_file_duplicate_and_secret_literal_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "bad.env").write_text("DUP=one\nDUP=two\n", encoding="utf-8")
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    env_file: bad.env
""",
    )
    with pytest.raises(ProjectError, match="duplicate environment key"):
        load_project(path, {})

    (tmp_path / "bad.env").write_text("DATABASE_PASSWORD=literal\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="secret-shaped"):
        load_project(path, {})

    (tmp_path / "bad.env").write_text("DATABASE_PASSWORD=${DATABASE_PASSWORD:?required}\n", encoding="utf-8")
    project = load_project(path, {"DATABASE_PASSWORD": "runtime"})
    assert resolve_service_environment(project.services["app"], {"DATABASE_PASSWORD": "runtime"}) == {
        "DATABASE_PASSWORD": "runtime"
    }


def test_global_interpolation_env_files_merge_then_host_wins(tmp_path: Path) -> None:
    (tmp_path / "one.env").write_text("FIRST=one\nSHARED=one\n", encoding="utf-8")
    (tmp_path / "two.env").write_text("SECOND=${FIRST}\nSHARED=two\nHOST_COPY=${HOST_VALUE}\n", encoding="utf-8")

    environment = load_interpolation_environment(
        tmp_path,
        ["one.env", Path("two.env")],
        {"HOST_VALUE": "host", "SHARED": "host-wins"},
    )

    assert environment == {
        "FIRST": "one",
        "SECOND": "one",
        "SHARED": "host-wins",
        "HOST_COPY": "host",
        "HOST_VALUE": "host",
    }


def test_global_interpolation_env_file_is_project_contained_and_symlink_free(tmp_path: Path) -> None:
    outside = tmp_path.parent / "global.env"
    outside.write_text("X=value\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="project-relative"):
        load_interpolation_environment(tmp_path, ["../global.env"], {})
    (tmp_path / "linked.env").symlink_to(outside)
    with pytest.raises(ProjectError, match="symlink"):
        load_interpolation_environment(tmp_path, ["linked.env"], {})


def test_secret_shaped_environment_must_be_external_only(tmp_path: Path) -> None:
    literal = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    environment:
      DATABASE_PASSWORD: literal-password
""",
    )
    with pytest.raises(ProjectError, match="secret-shaped"):
        load_project(literal, {})

    reference = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    environment:
      DATABASE_PASSWORD: ${{DATABASE_PASSWORD:?required}}
""",
    )
    project = load_project(reference, {"DATABASE_PASSWORD": "kept-out-of-model"})
    assert "kept-out-of-model" not in canonical_project_json(project)

    reference.write_text(
        f"""services:
  app:
    image: {BASE}
    environment:
      DATABASE_PASSWORD: '${{DATABASE_PASSWORD:?required}}'
""",
        encoding="utf-8",
    )
    with pytest.raises(ProjectError, match="secret-shaped"):
        load_project(reference, {"DATABASE_PASSWORD": "runtime"})

    dotenv = tmp_path / "literal.env"
    dotenv.write_text("DATABASE_PASSWORD='${DATABASE_PASSWORD:?required}'\n", encoding="utf-8")
    reference.write_text(
        f"""services:
  app:
    image: {BASE}
    env_file: literal.env
""",
        encoding="utf-8",
    )
    with pytest.raises(ProjectError, match="secret-shaped"):
        load_project(reference, {"DATABASE_PASSWORD": "runtime"})


def test_literal_private_key_is_rejected_from_cloud_init_state(tmp_path: Path) -> None:
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    cloud_init:
      inline:
        write_files:
          - path: /root/id
            content: |-
              -----BEGIN PRIVATE KEY-----
              material
              -----END PRIVATE KEY-----
""",
    )
    with pytest.raises(ProjectError, match="secret-shaped material"):
        load_project(path, {})


def test_resolve_cloud_init_returns_execution_copy_and_preserves_templates(tmp_path: Path) -> None:
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    cloud_init:
      inline:
        packages: ["${{PACKAGE:-curl}}"]
        write_files:
          - path: /etc/app.conf
            content: "mode=${{MODE:?required}}"
        runcmd:
          - [systemctl, "${{ACTION:-restart}}", app]
""",
    )
    project = load_project(path, {"MODE": "fast"})
    original = project.services["app"].cloud_init
    assert original is not None

    resolved = resolve_cloud_init(original, {"MODE": "fast"})

    assert resolved.packages == ("curl",)
    assert resolved.write_files[0].content == "mode=fast"
    assert resolved.runcmd == (("systemctl", "restart", "app"),)
    assert original.write_files[0].content == "mode=${MODE:?required}"


def test_cloud_init_templates_can_reference_resolved_service_environment(tmp_path: Path) -> None:
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    environment:
      MODE: fast
    cloud_init:
      inline:
        write_files:
          - path: /etc/app.conf
            content: "mode=${{MODE:?MODE required}}"
""",
    )
    project = load_project(path, {})
    cloud = project.services["app"].cloud_init
    assert cloud is not None
    execution = resolve_cloud_init(cloud, resolve_service_environment(project.services["app"], {}))
    assert execution.write_files[0].content == "mode=fast"


@pytest.mark.parametrize(
    "target",
    [
        "/dev/data",
        "/proc",
        "/sys/kernel",
        "/etc/palimpsest/config",
        "/opt/layers",
        "/mnt/palimpsest/work",
        "/usr/local/libexec/palimpsest-exec",
        "/etc/systemd/system/palimpsest-agent.service",
    ],
)
def test_reserved_guest_paths_rejected_for_mounts_and_cloud_init(tmp_path: Path, target: str) -> None:
    mount = _write_project(
        tmp_path,
        f"""volumes:
  data: {{}}
services:
  app:
    image: {BASE}
    volumes: ["data:{target}"]
""",
    )
    with pytest.raises(ProjectError, match="owned guest path"):
        load_project(mount, {})

    write_file = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    cloud_init:
      inline:
        write_files:
          - path: {target}
            content: test
""",
    )
    with pytest.raises(ProjectError, match="owned guest path"):
        load_project(write_file, {})


def test_paths_cannot_escape_or_traverse_symlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.env"
    outside.write_text("X=1", encoding="utf-8")
    escaping = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    env_file: ../outside.env
""",
    )
    with pytest.raises(ProjectError, match="project-relative"):
        load_project(escaping, {})

    inside_link = tmp_path / "linked.env"
    inside_link.symlink_to(outside)
    symlinked = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    env_file: linked.env
""",
    )
    with pytest.raises(ProjectError, match="symlink"):
        load_project(symlinked, {})


def test_explicit_project_root_controls_relative_inputs_and_default_name(tmp_path: Path) -> None:
    source_directory = tmp_path / "invocation" / "config"
    project_root = tmp_path / "chosen-root"
    source_directory.mkdir(parents=True)
    (project_root / "bundle").mkdir(parents=True)
    (project_root / "runtime.env").write_text("PUBLIC=from-project-root\n", encoding="utf-8")
    source = source_directory / DEFAULT_PROJECT_FILE
    source.write_text(
        """services:
  app:
    bundle: bundle
    env_file: runtime.env
""",
        encoding="utf-8",
    )

    project = load_project(source, {}, project_root=project_root)

    assert project.source == source.resolve()
    assert project.root == project_root.resolve()
    assert project.name == "chosen-root"
    assert project.services["app"].bundle is not None
    assert project.services["app"].bundle.path == (project_root / "bundle").resolve()
    assert project.services["app"].env_files[0].path == (project_root / "runtime.env").resolve()


def test_project_file_itself_cannot_be_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.yml"
    real.write_text(f"services:\n  app:\n    image: {BASE}\n", encoding="utf-8")
    linked = tmp_path / DEFAULT_PROJECT_FILE
    linked.symlink_to(real)
    with pytest.raises(ProjectError, match="not a symlink"):
        load_project(linked, {})


@pytest.mark.parametrize(
    "body",
    [
        f"services:\n  app:\n    image: {BASE}\n    surprise: true\n",
        f"services:\n  app:\n    image: {BASE}\n    image: {DB_BASE}\n",
        f"services:\n  app:\n    image: {BASE}\n    bundle: bundle\n",
        "services:\n  app:\n    image: not-a-digest\n",
        f"services:\n  app:\n    image: {BASE}\n    layers: [{LAYER}, {LAYER}]\n",
        f"services:\n  app:\n    image: {BASE}\n    networks: [missing]\n",
    ],
)
def test_schema_unknown_duplicate_and_invalid_values_fail_closed(tmp_path: Path, body: str) -> None:
    (tmp_path / "bundle").mkdir()
    path = _write_project(tmp_path, body)
    with pytest.raises(ProjectError):
        load_project(path, {})


def test_dependency_cycles_and_unknown_dependencies_are_rejected(tmp_path: Path) -> None:
    cycle = _write_project(
        tmp_path,
        f"""services:
  a:
    image: {BASE}
    depends_on: [b]
  b:
    image: {DB_BASE}
    depends_on: [a]
""",
    )
    with pytest.raises(ProjectError, match="cycle.*a -> b -> a"):
        load_project(cycle, {})

    unknown = _write_project(
        tmp_path,
        f"""services:
  a:
    image: {BASE}
    depends_on: [missing]
""",
    )
    with pytest.raises(ProjectError, match="unknown service"):
        load_project(unknown, {})


def test_duplicate_mount_targets_and_ports_are_rejected(tmp_path: Path) -> None:
    duplicate_mount = _write_project(
        tmp_path,
        f"""volumes:
  first: {{}}
  second: {{}}
services:
  app:
    image: {BASE}
    volumes:
      - first:/data
      - second:/data
""",
    )
    with pytest.raises(ProjectError, match="duplicate guest mount"):
        load_project(duplicate_mount, {})

    duplicate_port = _write_project(
        tmp_path,
        f"""services:
  one:
    image: {BASE}
    ports: ["8080:80"]
  two:
    image: {DB_BASE}
    ports: ["8080:8080"]
""",
    )
    with pytest.raises(ProjectError, match="claimed by both"):
        load_project(duplicate_port, {})


def test_ports_default_to_loopback_and_validate_ip(tmp_path: Path) -> None:
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    ports:
      - "9090:90/tcp"
""",
    )
    assert load_project(path, {}).services["app"].ports[0].host_ip == "127.0.0.1"

    path.write_text(
        f"""services:
  app:
    image: {BASE}
    ports:
      - target: 80
        published: 8080
        host_ip: localhost
""",
        encoding="utf-8",
    )
    with pytest.raises(ProjectError, match="literal IPv4 or IPv6"):
        load_project(path, {})


def test_v1_rejects_bind_mounts_udp_and_multiple_network_attachments(tmp_path: Path) -> None:
    (tmp_path / "host-data").mkdir()
    bind = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    volumes:
      - type: bind
        source: host-data
        target: /srv/data
""",
    )
    with pytest.raises(ProjectError, match="bind is unsupported"):
        load_project(bind, {})

    udp = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    ports: ["5353:53/udp"]
""",
    )
    with pytest.raises(ProjectError, match="must be tcp"):
        load_project(udp, {})

    multiple_networks = _write_project(
        tmp_path,
        f"""networks:
  one: {{}}
  two: {{}}
services:
  app:
    image: {BASE}
    networks: [one, two]
""",
    )
    with pytest.raises(ProjectError, match="exactly one attachment"):
        load_project(multiple_networks, {})


def test_cloud_init_forbids_raw_text_shell_commands_and_unknown_keys(tmp_path: Path) -> None:
    raw = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    cloud_init:
      inline: |
        #cloud-config
        packages: [curl]
""",
    )
    with pytest.raises(ProjectError, match="inline must be a mapping"):
        load_project(raw, {})

    shell = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    cloud_init:
      inline:
        runcmd:
          - "curl example.test | sh"
""",
    )
    with pytest.raises(ProjectError, match="argv sequence"):
        load_project(shell, {})


def test_yaml_subset_rejects_aliases_tags_merge_directives_tabs_and_duplicate_keys() -> None:
    for payload in (
        "x: &anchor value\n",
        "x: *anchor\n",
        "x: !Thing value\n",
        "<<: value\n",
        "%YAML 1.2\nx: value\n",
        "x:\n\tchild: value\n",
        "x: one\nx: two\n",
    ):
        with pytest.raises(ProjectError):
            parse_yaml_subset(payload)


def test_yaml_subset_bounds_flow_depth_node_count_and_integer_size() -> None:
    too_deep = "x: " + "[" * 34 + "value" + "]" * 34
    too_many = "x: [" + ",".join("0" for _ in range(10_001)) + "]"
    huge_integer = "x: " + "9" * 5_000
    for payload in (too_deep, too_many, huge_integer, "x: [" + "9" * 5_000 + "]"):
        with pytest.raises(ProjectError):
            parse_yaml_subset(payload)
    two_flow_scalars = (
        "a: [" + ",".join("0" for _ in range(6_000)) + "]\nb: [" + ",".join("0" for _ in range(6_000)) + "]"
    )
    with pytest.raises(ProjectError, match="YAML nodes"):
        parse_yaml_subset(two_flow_scalars)


def test_guest_double_slash_and_huge_numeric_fields_fail_with_project_error(tmp_path: Path) -> None:
    for field in (
        'ports: ["' + "9" * 5_000 + ':80"]',
        'memory: "' + "9" * 5_000 + 'GiB"',
        'volumes: ["data://dev"]',
    ):
        volume = "volumes:\n  data: {}\n" if field.startswith("volumes") else ""
        path = _write_project(
            tmp_path,
            f"""{volume}services:
  app:
    image: {BASE}
    {field}
""",
        )
        with pytest.raises(ProjectError):
            load_project(path, {})

    path = _write_project(
        tmp_path,
        f"""volumes:
  data: {{}}
services:
  app:
    image: {BASE}
    volumes:
      - source: data
        target: //dev
""",
    )
    with pytest.raises(ProjectError, match="normalized absolute guest path"):
        load_project(path, {})


def test_dotenv_decoded_controls_surrogates_and_overlong_paths_are_wrapped(tmp_path: Path) -> None:
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    env_file: bad.env
""",
    )
    for content in ('X="\\u0000"\n', 'X="\\r"\n', 'X="\\ud800"\n'):
        (tmp_path / "bad.env").write_text(content, encoding="utf-8")
        with pytest.raises(ProjectError):
            load_project(path, {})

    path.write_text(
        f"""services:
  app:
    image: {BASE}
    env_file: {"x" * 300}
""",
        encoding="utf-8",
    )
    with pytest.raises(ProjectError):
        load_project(path, {})


def test_env_file_cannot_change_between_model_validation_and_execution(tmp_path: Path) -> None:
    env_path = tmp_path / "runtime.env"
    env_path.write_text("PUBLIC=first\n", encoding="utf-8")
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    env_file: runtime.env
""",
    )
    project = load_project(path, {})
    env_path.write_text("PUBLIC=second\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="changed after project validation"):
        resolve_service_environment(project.services["app"], {})


def test_env_and_cloud_files_are_size_checked_before_content_hashing(tmp_path: Path) -> None:
    oversized = "X" * (256 * 1024 + 1)
    (tmp_path / "large.env").write_text(oversized, encoding="utf-8")
    path = _write_project(
        tmp_path,
        f"""services:
  app:
    image: {BASE}
    env_file: large.env
""",
    )
    with pytest.raises(ProjectError, match="262144-byte limit"):
        load_project(path, {})

    (tmp_path / "cloud.yml").write_text(oversized, encoding="utf-8")
    path.write_text(
        f"""services:
  app:
    image: {BASE}
    cloud_init:
      file: cloud.yml
""",
        encoding="utf-8",
    )
    with pytest.raises(ProjectError, match="262144-byte limit"):
        load_project(path, {})


def test_naming_helpers_are_deterministic_safe_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / "My Project With Spaces" / DEFAULT_PROJECT_FILE
    first = deterministic_project_name(path)
    second = deterministic_project_name(path)
    assert first == second
    assert len(first) <= 63
    assert " " not in first
    assert deterministic_service_name("demo", "api") == "demo-api-1"
    assert len(deterministic_service_name("p" * 60, "service", 12)) <= 63
    with pytest.raises(ProjectError):
        deterministic_service_name("demo", "api", 0)
