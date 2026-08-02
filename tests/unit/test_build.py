"""Focused grammar and integrity tests for src/palimpsest_local/build.py."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from palimpsest_local import state
from palimpsest_local.build import (
    CLEAN_TARGETS_V1,
    PALIMPSEST_CLEAN_V1,
    Palimpsestfile,
    build_layer,
    create_build_record,
    generate_cleaning_command,
    parse_palimpsestfile,
    parse_palimpsestfile_text,
    verify_build_integrity,
)
from palimpsest_local.errors import ArtifactValidationError, BuildError, InvalidDigestError
from palimpsest_local.refs import BuildSpec, ImageRef

D_BASE = "sha256:" + "a" * 64
D_LAYER1 = "sha256:" + "b" * 64
D_LAYER2 = "sha256:" + "c" * 64
D_MISMATCH = "sha256:" + "f" * 64


def test_parse_valid_palimpsestfile_text():
    content = f"""# Sample Palimpsestfile
FROM {D_BASE}
LAYER {D_LAYER1}
LAYER {D_LAYER2}

ENV PORT=8080 APP_ENV=production
WORKDIR /app
RUN echo "building app" \\
    && make build

ENV SPECIAL_FLAG "value with spaces"
WORKDIR /app/src
RUN gcc -o main main.c
"""
    recipe = parse_palimpsestfile_text(content)
    assert isinstance(recipe, Palimpsestfile)
    assert recipe.base_digest == D_BASE
    assert recipe.layers == (D_LAYER1, D_LAYER2)
    assert len(recipe.runs) == 2

    run1 = recipe.runs[0]
    assert run1.line == 8
    assert run1.command == 'echo "building app" && make build'
    assert run1.env == {"PORT": "8080", "APP_ENV": "production"}
    assert run1.workdir == "/app"

    run2 = recipe.runs[1]
    assert run2.line == 13
    assert run2.command == "gcc -o main main.c"
    assert run2.env == {"PORT": "8080", "APP_ENV": "production", "SPECIAL_FLAG": "value with spaces"}
    assert run2.workdir == "/app/src"

    assert recipe.final_env == {"PORT": "8080", "APP_ENV": "production", "SPECIAL_FLAG": "value with spaces"}
    assert recipe.final_workdir == "/app/src"
    assert recipe.recipe_sha256.startswith("sha256:")


def test_parse_palimpsestfile_from_path(tmp_path: Path):
    path = tmp_path / "Palimpsestfile"
    content = f"FROM {D_BASE}\nRUN echo test\n"
    path.write_text(content, encoding="utf-8")

    recipe = parse_palimpsestfile(path)
    assert recipe.base_digest == D_BASE
    assert len(recipe.runs) == 1
    assert recipe.runs[0].command == "echo test"


def test_parse_palimpsestfile_missing_path(tmp_path: Path):
    missing = tmp_path / "NonExistent"
    with pytest.raises(BuildError, match="recipe file not found"):
        parse_palimpsestfile(missing)


def test_exceeds_1mib_limit():
    content = f"FROM {D_BASE}\n" + "RUN echo " + ("x" * (1024 * 1024))
    with pytest.raises(BuildError, match="exceeds 1 MiB limit"):
        parse_palimpsestfile_text(content)


def test_reject_first_instruction_not_from():
    content = "RUN echo hello\n"
    with pytest.raises(BuildError, match="first instruction must be FROM"):
        parse_palimpsestfile_text(content)


def test_reject_bare_ubuntu_and_scratch_from():
    with pytest.raises(BuildError, match="FROM scratch or bare ubuntu image tags are not supported"):
        parse_palimpsestfile_text("FROM ubuntu:22.04\nRUN echo hello")

    with pytest.raises(BuildError, match="FROM scratch or bare ubuntu image tags are not supported"):
        parse_palimpsestfile_text("FROM scratch\nRUN echo hello")


def test_reject_from_aliases_or_flags():
    with pytest.raises(BuildError, match="FROM flags or 'AS' aliases are not supported"):
        parse_palimpsestfile_text(f"FROM {D_BASE} AS builder\nRUN echo hello")

    with pytest.raises(BuildError, match="FROM flags or 'AS' aliases are not supported"):
        parse_palimpsestfile_text(f"FROM --platform=linux/amd64 {D_BASE}\nRUN echo hello")


def test_reject_multi_stage_from():
    content = f"FROM {D_BASE}\nRUN echo 1\nFROM {D_BASE}\nRUN echo 2"
    with pytest.raises(BuildError, match="multi-stage builds are not supported"):
        parse_palimpsestfile_text(content)


def test_reject_layer_after_execution_instruction():
    content = f"FROM {D_BASE}\nENV FOO=bar\nLAYER {D_LAYER1}\nRUN echo hello"
    with pytest.raises(BuildError, match="LAYER instruction cannot appear after ENV, WORKDIR, or RUN"):
        parse_palimpsestfile_text(content)

    content2 = f"FROM {D_BASE}\nWORKDIR /app\nLAYER {D_LAYER1}\nRUN echo hello"
    with pytest.raises(BuildError, match="LAYER instruction cannot appear after ENV, WORKDIR, or RUN"):
        parse_palimpsestfile_text(content2)

    content3 = f"FROM {D_BASE}\nRUN echo hello\nLAYER {D_LAYER1}"
    with pytest.raises(BuildError, match="LAYER instruction cannot appear after ENV, WORKDIR, or RUN"):
        parse_palimpsestfile_text(content3)


def test_reject_layer_options_and_invalid_digests():
    with pytest.raises(BuildError, match="LAYER options or flags are not supported"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nLAYER --flag {D_LAYER1}\nRUN echo hello")

    with pytest.raises(BuildError, match="LAYER digest must be sha256:<64hex>"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nLAYER invalid_digest\nRUN echo hello")


def test_reject_more_than_25_layers():
    layers = "\n".join(f"LAYER sha256:{i:064x}" for i in range(1, 27))
    content = f"FROM {D_BASE}\n{layers}\nRUN echo hello"
    with pytest.raises(BuildError, match="supports at most 25 LAYER instructions"):
        parse_palimpsestfile_text(content)


def test_reject_heredocs_and_unclosed_continuations():
    with pytest.raises(BuildError, match="heredocs \\('<<'\\) are not supported"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nRUN <<EOF\nhello\nEOF")

    with pytest.raises(BuildError, match="unclosed line continuation at end of file"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nRUN echo hello \\")


def test_reject_unsupported_instructions():
    for inst in ["COPY", "ADD", "ARG", "USER", "SHELL", "EXPOSE", "VOLUME", "ENTRYPOINT", "CMD", "LABEL"]:
        content = f"FROM {D_BASE}\n{inst} something\nRUN echo hello"
        with pytest.raises(BuildError, match=f"instruction {inst} is not supported"):
            parse_palimpsestfile_text(content)

    with pytest.raises(BuildError, match="unknown instruction: UNKNOWNINST"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nUNKNOWNINST foo\nRUN echo hello")


def test_reject_invalid_run_options():
    with pytest.raises(BuildError, match="unsupported RUN flag or option"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nRUN --mount=type=cache,target=/var/cache apt update")

    with pytest.raises(BuildError, match="unsupported RUN flag or option"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nRUN --network=host ping localhost")

    with pytest.raises(BuildError, match="RUN command cannot be empty"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nRUN   ")


def test_reject_recipe_without_run():
    with pytest.raises(BuildError, match="Palimpsestfile must contain at least one RUN instruction"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nENV FOO=bar")


def test_reject_invalid_workdir():
    with pytest.raises(BuildError, match="WORKDIR must be an absolute path starting with '/'"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nWORKDIR relative/path\nRUN echo hello")

    with pytest.raises(BuildError, match="WORKDIR path cannot contain '\\.' or '\\.\\.' traversal segments"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nWORKDIR /app/../etc\nRUN echo hello")

    with pytest.raises(BuildError, match="WORKDIR path cannot contain '\\.' or '\\.\\.' traversal segments"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nWORKDIR /app/./sub\nRUN echo hello")


def test_reject_invalid_env():
    with pytest.raises(BuildError, match="invalid ENV key"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nENV 123INVALID=val\nRUN echo hello")

    with pytest.raises(BuildError, match="control characters not allowed"):
        parse_palimpsestfile_text(f"FROM {D_BASE}\nENV FOO=bar\x00\nRUN echo hello")


def test_verify_build_integrity_success():
    content = f"FROM {D_BASE}\nLAYER {D_LAYER1}\nLAYER {D_LAYER2}\nRUN echo hello"
    recipe = parse_palimpsestfile_text(content)

    base, layers = verify_build_integrity(recipe, D_BASE, [D_LAYER1, D_LAYER2])
    assert base == D_BASE
    assert layers == (D_LAYER1, D_LAYER2)

    # Omitted CLI layers defaults to recipe layers
    base2, layers2 = verify_build_integrity(recipe, D_BASE, None)
    assert base2 == D_BASE
    assert layers2 == (D_LAYER1, D_LAYER2)


def test_verify_build_integrity_mismatches():
    content = f"FROM {D_BASE}\nLAYER {D_LAYER1}\nRUN echo hello"
    recipe = parse_palimpsestfile_text(content)

    # Base mismatch
    with pytest.raises(BuildError, match="base digest mismatch"):
        verify_build_integrity(recipe, D_MISMATCH, [D_LAYER1])

    # Layer chain mismatch
    with pytest.raises(BuildError, match="layer chain mismatch"):
        verify_build_integrity(recipe, D_BASE, [D_LAYER2])

    # Extra CLI layer mismatch
    with pytest.raises(BuildError, match="layer chain mismatch"):
        verify_build_integrity(recipe, D_BASE, [D_LAYER1, D_LAYER2])

    # Invalid digest format on CLI
    with pytest.raises(InvalidDigestError):
        verify_build_integrity(recipe, "not_a_digest", [D_LAYER1])


def test_cleaning_and_record_helpers():
    cmd = generate_cleaning_command("/mnt/palimpsest/capture/upper")
    assert "/mnt/palimpsest/capture/upper/tmp" in cmd
    assert "etc/hosts" in cmd

    record = create_build_record(
        build_id="b-12345",
        base_digest=D_BASE,
        parent_digests=[D_LAYER1],
        recipe_sha256="sha256:" + "d" * 64,
        network="none",
        output_tag="my-tag",
        output_digest=D_LAYER2,
        status="success",
    )

    assert record["schema_version"] == 1
    assert record["build_id"] == "b-12345"
    assert record["base_digest"] == D_BASE
    assert record["parent_digests"] == [D_LAYER1]
    assert record["cleaning_policy"] == PALIMPSEST_CLEAN_V1
    assert record["network"] == "none"
    assert record["output_tag"] == "my-tag"
    assert record["output_digest"] == D_LAYER2
    assert record["status"] == "success"


def test_serial_builder_cleaning_targets_match_commit_policy():
    from palimpsest_local.cloudinit import _BUILD_WORKER_SOURCE

    assert all(f'"{target}"' in _BUILD_WORKER_SOURCE for target in CLEAN_TARGETS_V1)


def test_create_build_record_invalid():
    with pytest.raises(ArtifactValidationError, match="invalid build network"):
        create_build_record(
            build_id="b-1",
            base_digest=D_BASE,
            parent_digests=[],
            recipe_sha256="sha256:" + "d" * 64,
            network="invalid_net",  # type: ignore
        )


def _build_spec(tmp_path: Path, *, tag: str = "test-layer", command: str = "echo hello") -> BuildSpec:
    base_image = tmp_path / "base.qcow2"
    base_image.write_bytes(b"base-image")
    base_digest = f"sha256:{hashlib.sha256(base_image.read_bytes()).hexdigest()}"
    recipe = tmp_path / "Palimpsestfile"
    recipe.write_text(f"FROM {base_digest}\nRUN {command}\n", encoding="utf-8")
    return BuildSpec(
        base=ImageRef(base_digest, "qcow2", "x86_64", None, base_image),
        parent_layers=(),
        recipe=recipe,
        network="default",
        output_name=tag,
    )


def _successful_output_receiver(_socket_path: Path, output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"hsqs" + b"\0" * 128)
    return f"sha256:{hashlib.sha256(output.read_bytes()).hexdigest()}"


def test_build_layer_promotes_verified_output_and_cleans_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config")})
    spec = _build_spec(tmp_path)
    removed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "palimpsest_local.runtime.start_serial_builder",
        lambda run_spec, **_kwargs: {"status": "running", "name": run_spec.name},
    )
    monkeypatch.setattr(
        "palimpsest_local.runtime.rm",
        lambda name, *, volumes, **_kwargs: removed.append((name, volumes)),
    )

    record = build_layer(spec, roots=roots, output_receiver=_successful_output_receiver)

    assert record["status"] == "success"
    assert record["output_tag"] == spec.output_name
    tag = state.read_tag_record(roots, spec.output_name)
    assert tag.digest == record["output_digest"]
    assert tag.source == "build"
    assert removed == [(f"builder-{record['build_id']}", True)]


def test_build_layer_rejects_conflicting_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config")})
    spec = _build_spec(tmp_path, tag="conflict")
    state.write_tag_record(
        roots,
        state.TagRecord(
            1,
            "conflict",
            "sha256:" + "f" * 64,
            "application/vnd.afterglow.palimpsest.layer.squashfs.v1",
            1,
            None,
            spec.base.digest,
            "build",
            state.utc_now_iso(),
        ),
    )
    monkeypatch.setattr(
        "palimpsest_local.runtime.start_serial_builder",
        lambda run_spec, **_kwargs: {"status": "running", "name": run_spec.name},
    )
    monkeypatch.setattr("palimpsest_local.runtime.rm", lambda *_args, **_kwargs: None)

    with pytest.raises(BuildError, match="conflicting with built digest"):
        build_layer(spec, roots=roots, output_receiver=_successful_output_receiver)


def test_network_none_build_uses_serial_builder_without_an_interface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config")})
    spec = _build_spec(tmp_path)
    spec = BuildSpec(
        base=spec.base,
        parent_layers=spec.parent_layers,
        recipe=spec.recipe,
        network="none",
        output_name=spec.output_name,
    )
    started: list[tuple[object, str]] = []
    monkeypatch.setattr(
        "palimpsest_local.runtime.start_serial_builder",
        lambda run_spec, *, user_data, **_kwargs: started.append((run_spec, user_data)) or {"status": "running"},
    )
    monkeypatch.setattr("palimpsest_local.runtime.rm", lambda *_args, **_kwargs: None)

    build_layer(spec, roots=roots, output_receiver=_successful_output_receiver)

    assert started and started[0][0].network == "none"
    assert "ssh_authorized_keys:" not in started[0][1]
    assert "ed25519_private:" not in started[0][1]


def test_build_layer_records_serial_run_failure_and_cleans_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config")})
    spec = _build_spec(tmp_path, command="false")
    removed: list[str] = []
    monkeypatch.setattr(
        "palimpsest_local.runtime.start_serial_builder",
        lambda run_spec, **_kwargs: {"status": "running", "name": run_spec.name},
    )
    monkeypatch.setattr(
        "palimpsest_local.runtime.rm",
        lambda name, **_kwargs: removed.append(name),
    )

    def failed_output_receiver(_socket_path: Path, _output: Path) -> str:
        raise BuildError("serial builder reported a build failure during run at Palimpsestfile line 2")

    with pytest.raises(BuildError, match="Palimpsestfile line 2"):
        build_layer(spec, roots=roots, output_receiver=failed_output_receiver)
    records = list(roots.builds.glob("*/record.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["status"] == "failed"
    assert removed and removed[0].startswith("builder-")
