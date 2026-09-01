"""Canonical OCI process configuration for the future guest PID1."""

from __future__ import annotations

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_process import OCIProcessSpec, OCIUserSpec


def test_process_config_preserves_literal_argv_environment_and_identity() -> None:
    process = OCIProcessSpec.from_config(
        {
            "Entrypoint": ["/usr/bin/demo", "$(not-a-shell)"],
            "Cmd": ["line\nwith-newline", ""],
            "Env": ["PATH=/usr/bin:/bin", "MESSAGE=$HOME; still literal"],
            "WorkingDir": "/srv/../work",
            "User": "1000:staff",
            "StopSignal": "TERM",
        }
    )

    assert process.argv == ("/usr/bin/demo", "$(not-a-shell)", "line\nwith-newline", "")
    assert process.environment == (("PATH", "/usr/bin:/bin"), ("MESSAGE", "$HOME; still literal"))
    assert process.cwd == "/work"
    assert process.user == OCIUserSpec("1000", "staff")
    assert process.stop_signal == 15
    assert process.bootable
    assert OCIProcessSpec.from_dict(process.to_dict()) == process


def test_empty_process_is_source_valid_but_not_bootable() -> None:
    process = OCIProcessSpec.from_config(None)

    assert process == OCIProcessSpec.empty()
    with pytest.raises(ArtifactValidationError, match="no Entrypoint or Cmd"):
        process.require_bootable()

    with pytest.raises(ArtifactValidationError, match="no Entrypoint or Cmd"):
        OCIProcessSpec(("", "argument"), (), "/", OCIUserSpec("0", "0"), 15).require_bootable()


@pytest.mark.parametrize(
    "config,match",
    [
        ({"Entrypoint": "/bin/sh"}, "Entrypoint must be an array"),
        ({"Cmd": ["bad\0arg"]}, r"Cmd\[0\]"),
        ({"Env": ["NO_EQUALS"]}, "must contain"),
        ({"Env": ["A=1", "A=2"]}, "duplicated"),
        ({"WorkingDir": "relative"}, "canonical absolute"),
        ({"User": "01"}, "canonical"),
        ({"User": "root:"}, "user is invalid"),
        ({"StopSignal": "SIGPWR"}, "unsupported"),
        ({"ArgsEscaped": True}, "unsupported"),
        ({"ArgsEscaped": []}, "unsupported"),
        ({"WorkingDir": False}, "WorkingDir is invalid"),
    ],
)
def test_process_config_rejects_ambiguous_or_unsupported_values(config: dict[str, object], match: str) -> None:
    with pytest.raises(ArtifactValidationError, match=match):
        OCIProcessSpec.from_config(config)


def test_process_wire_contract_rejects_noncanonical_or_unknown_fields() -> None:
    process = OCIProcessSpec(("/init",), (), "/", OCIUserSpec("0", "0"), 15)
    value = process.to_dict()
    value["cwd"] = "/tmp/../run"
    with pytest.raises(ArtifactValidationError, match="canonical absolute"):
        OCIProcessSpec.from_dict(value)

    value = process.to_dict()
    value["shell"] = True
    with pytest.raises(ArtifactValidationError, match="fields"):
        OCIProcessSpec.from_dict(value)
