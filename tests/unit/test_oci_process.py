"""Canonical OCI process configuration for the future guest PID1."""

from __future__ import annotations

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_process import (
    OCI_DEFAULT_PATH,
    OCIProcessSpec,
    OCIUserSpec,
    oci_path_candidates,
    resolve_oci_user,
)


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


def test_process_inserts_container_default_path_without_inheriting_host_environment() -> None:
    process = OCIProcessSpec(("demo",), (), "/", OCIUserSpec("0", "0"), 15)

    assert process.environment == (("PATH", OCI_DEFAULT_PATH),)
    assert oci_path_candidates(process) == tuple(f"{directory}/demo" for directory in OCI_DEFAULT_PATH.split(":"))


def test_path_search_preserves_order_empty_entries_and_direct_slash_commands() -> None:
    searched = OCIProcessSpec(("demo",), (("PATH", "/missing::relative"),), "/work", OCIUserSpec("0", "0"), 15)
    direct = OCIProcessSpec(("./demo",), (), "/work", OCIUserSpec("0", "0"), 15)

    assert oci_path_candidates(searched) == ("/missing/demo", "demo", "relative/demo")
    assert oci_path_candidates(direct) == ("./demo",)


def test_path_candidates_preserve_lazy_guest_order_for_late_oversized_component() -> None:
    oversized = "x" * 4096
    process = OCIProcessSpec(("demo",), (("PATH", f"/first:{oversized}"),), "/", OCIUserSpec("0", "0"), 15)

    assert oci_path_candidates(process) == ("/first/demo", f"{oversized}/demo")


def test_image_root_identity_resolution_covers_names_omitted_group_and_numeric_fallback() -> None:
    passwd = b"root:x:0:0:root:/:/bin/false\npalimpsest:x:65534:1234:proof:/:/bin/false\n"
    group = b"root:x:0:\nworkers:x:2345:\n"

    assert resolve_oci_user(OCIUserSpec("palimpsest", None), passwd=passwd, group=None) == (65534, 1234)
    assert resolve_oci_user(OCIUserSpec("palimpsest", "workers"), passwd=passwd, group=group) == (65534, 2345)
    assert resolve_oci_user(OCIUserSpec("9876", None), passwd=passwd, group=None) == (9876, 0)
    assert resolve_oci_user(OCIUserSpec("65534", "3456"), passwd=None, group=None) == (65534, 3456)


def test_numeric_user_with_explicit_group_does_not_consult_passwd() -> None:
    malformed_passwd = b"this-is-not-a-passwd-record\n"

    assert resolve_oci_user(OCIUserSpec("1234", "5678"), passwd=malformed_passwd, group=None) == (1234, 5678)
    assert resolve_oci_user(
        OCIUserSpec("1234", "workers"),
        passwd=malformed_passwd,
        group=b"workers:x:5678:\n",
    ) == (1234, 5678)


def test_account_database_uses_lf_only_record_boundaries_like_the_guest() -> None:
    passwd = b"palimpsest:x:65534:1234:proof\vaccount:/:/bin/false\n"

    assert resolve_oci_user(OCIUserSpec("palimpsest", None), passwd=passwd, group=None) == (65534, 1234)


@pytest.mark.parametrize(
    "user,passwd,group,match",
    [
        (OCIUserSpec("missing", None), b"root:x:0:0:root:/:/bin/false\n", None, "absent"),
        (OCIUserSpec("root", None), b"root:x:0:0:a:/:/x\nroot:x:1:1:b:/:/x\n", None, "ambiguous"),
        (OCIUserSpec("0", "missing"), None, b"root:x:0:\n", "absent"),
        (OCIUserSpec("0", "root"), None, b"root:x:0:\nroot:x:1:\n", "ambiguous"),
        (OCIUserSpec("0", "root"), None, b"root:x:00:\n", "canonical"),
    ],
)
def test_image_root_identity_resolution_rejects_missing_ambiguous_or_malformed_databases(
    user: OCIUserSpec, passwd: bytes | None, group: bytes | None, match: str
) -> None:
    with pytest.raises(ArtifactValidationError, match=match):
        resolve_oci_user(user, passwd=passwd, group=group)


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
