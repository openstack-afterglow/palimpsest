from __future__ import annotations

import hashlib
import io
import tarfile
from contextlib import contextmanager

import pytest

from palimpsest_local.oci_capture import (
    CaptureError,
    IgnoreMatcher,
    UpperEntry,
    UpperKind,
    convert_upper_to_layer,
    normalize_capture_path,
)


@contextmanager
def _open(payload):
    yield io.BytesIO(payload)


def _file(path: str, data: bytes = b"x", **kwargs) -> UpperEntry:
    return UpperEntry(path, UpperKind.FILE, size=len(data), payload=data, **kwargs)


def _layer(entries, **kwargs):
    sink = io.BytesIO()
    receipt = convert_upper_to_layer(entries, sink, _open, **kwargs)
    members = {}
    with tarfile.open(fileobj=io.BytesIO(sink.getvalue())) as archive:
        for member in archive:
            members[member.name] = (member, archive.extractfile(member).read() if member.isreg() else None)
    return receipt, sink.getvalue(), members


def test_overlay_semantics_become_oci_markers_and_placeholders_are_dropped():
    entries = [
        _file("opt/app/main.py", b"print(1)", mode=0o755, uid=1000, gid=1000),
        UpperEntry("usr/share/doc/old", UpperKind.WHITEOUT),
        UpperEntry("var/cache/app", UpperKind.DIRECTORY, mode=0o755, xattrs=(("trusted.overlay.opaque", b"y"),)),
        _file("etc/machine-id", b""),
        _file(".palimpsest/state/x", b"secret"),
    ]
    receipt, raw, members = _layer(entries, generated_placeholders=["etc/machine-id"])
    assert set(members) == {
        "opt/app/main.py",
        "usr/share/doc/.wh.old",
        "var/cache/app",
        "var/cache/app/.wh..wh..opq",
    }
    assert members["opt/app/main.py"][1] == b"print(1)"
    assert members["opt/app/main.py"][0].uid == 1000
    assert set(receipt.excluded) == {"etc/machine-id", ".palimpsest/state/x"}
    assert receipt.diff_id == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_user_content_at_platform_bound_identity_path_is_kept_unless_recorded_as_placeholder():
    _, _, members = _layer([_file("etc/hostname", b"mybox\n")])
    assert members["etc/hostname"][1] == b"mybox\n"


def test_reinclusion_cannot_defeat_mandatory_exclusion_and_parent_identity_is_hidden():
    entries = [
        _file("etc/ssh/ssh_host_ed25519_key", b"PRIVATE"),
        _file("etc/ssh/sshd_config", b"Port 22"),
        _file("var/lib/cloud/instance/x", b"state"),
    ]
    receipt, _, members = _layer(
        entries,
        plmignore="!etc/ssh/ssh_host_ed25519_key\n!var/lib/cloud\n",
        parent_paths=[
            "etc/ssh/ssh_host_ed25519_key",
            "etc/ssh/ssh_host_rsa_key.pub",
            "var/lib/cloud",
            "var/lib/cloud/instance",
            "etc/passwd",
        ],
    )
    assert "etc/ssh/ssh_host_ed25519_key" not in members
    assert "var/lib/cloud/instance/x" not in members
    assert members["etc/ssh/sshd_config"][1] == b"Port 22"
    assert set(receipt.hidden) == {"etc/ssh/ssh_host_ed25519_key", "etc/ssh/ssh_host_rsa_key.pub", "var/lib/cloud"}
    assert {"etc/ssh/.wh.ssh_host_ed25519_key", "etc/ssh/.wh.ssh_host_rsa_key.pub", "var/lib/.wh.cloud"} <= set(members)
    assert "etc/.wh.passwd" not in members


def test_plmignore_excludes_only_new_upper_files_and_never_whiteouts_parent_files():
    entries = [_file("srv/data/big.bin", b"1"), _file("srv/data/keep.txt", b"2"), _file("home/u/note", b"3")]
    receipt, _, members = _layer(
        entries,
        plmignore="# comment\nsrv/data/*\n!srv/data/keep.txt\n",
        parent_paths=["srv/data/old.bin"],
    )
    assert set(members) == {"srv/data/keep.txt", "home/u/note"}
    assert receipt.ignored == ("srv/data/big.bin",)
    assert not receipt.hidden
    assert not any(".wh." in name for name in members)


def test_directory_rule_covers_descendants_and_reinclusion_wins_when_later():
    matcher = IgnoreMatcher("build\n**/*.log\n!build/keep/**\n")
    assert matcher.ignored("build/out/a.o")
    assert not matcher.ignored("build/keep/a.o")
    assert matcher.ignored("var/app/x.log")
    assert not matcher.ignored("var/app/x.txt")


def test_plmignore_capture_root_patterns_and_reinclusion():
    receipt, _, members = _layer(
        [_file("build"), _file("tempa"), _file("subdir/tempa"), _file("subdir/app.log")],
        plmignore="build/\ntemp?\n**/*.log\n",
    )
    assert set(members) == {"subdir/tempa"}
    assert set(receipt.ignored) == {"build", "tempa", "subdir/app.log"}

    receipt, _, members = _layer(
        [
            UpperEntry("build", UpperKind.DIRECTORY),
            _file("build/out/a.o"),
            _file("build/keep/a.o"),
            _file("build/keep/drop.log"),
            _file("foo/bar/nested"),
            _file("subdir/build"),
        ],
        plmignore="build/\n!build/keep/\n**/*.log\n/foo/bar/\n",
    )
    assert set(members) == {"build/keep/a.o", "subdir/build"}
    assert set(receipt.ignored) == {"build", "build/out/a.o", "build/keep/drop.log", "foo/bar/nested"}


@pytest.mark.parametrize("pattern", ["foo/bar", "/foo/bar/", "foo/bar/"])
def test_plmignore_slashes_do_not_change_file_or_directory_matching(pattern):
    receipt, _, members = _layer(
        [_file("foo/bar"), _file("other/foo/bar")],
        plmignore=pattern,
    )
    assert set(members) == {"other/foo/bar"}
    assert receipt.ignored == ("foo/bar",)

    receipt, _, members = _layer(
        [UpperEntry("foo/bar", UpperKind.DIRECTORY), _file("foo/bar/child")],
        plmignore=pattern,
    )
    assert not members
    assert set(receipt.ignored) == {"foo/bar", "foo/bar/child"}


@pytest.mark.parametrize("name", ["trusted.overlay.redirect", "trusted.overlay.metacopy", "trusted.overlay.nlink"])
def test_overlay_redirect_and_metacopy_are_rejected(name):
    with pytest.raises(CaptureError) as error:
        _layer([UpperEntry("a", UpperKind.DIRECTORY, xattrs=((name, b"/b"),))])
    assert error.value.code == "capture-overlay-metadata"


@pytest.mark.parametrize("name", ["trusted.overlay.impure", "trusted.overlay.origin", "user.overlay.impure"])
def test_overlay_copy_up_bookkeeping_is_dropped_and_never_written_to_the_layer(name):
    entries = [
        UpperEntry("opt", UpperKind.DIRECTORY, mode=0o755, xattrs=((name, b"y"), ("user.keep", b"1"))),
        _file("opt/a.txt", b"x"),
    ]
    _, _, members = _layer(entries)
    assert set(members) == {"opt", "opt/a.txt"}
    headers = members["opt"][0].pax_headers
    assert not any("overlay" in key for key in headers)
    assert any("user.keep" in key for key in headers)


def test_special_files_and_bad_paths_are_rejected():
    with pytest.raises(CaptureError):
        _layer([UpperEntry("dev/x", UpperKind.OTHER)])
    for bad in ["/abs", "../up", "a/../b", "a//b", "", "a\0b", "./a"]:
        with pytest.raises(CaptureError):
            normalize_capture_path(bad)


def test_hardlink_requires_emitted_earlier_target_and_output_is_deterministic():
    entries = [_file("a/one", b"d"), UpperEntry("b/two", UpperKind.HARDLINK, link_target="a/one")]
    first = _layer(entries)
    second = _layer(list(reversed(entries)))
    assert first[1] == second[1]
    assert first[2]["b/two"][0].islnk()
    with pytest.raises(CaptureError) as error:
        _layer([_file("z/one", b"d"), UpperEntry("a/two", UpperKind.HARDLINK, link_target="z/one")])
    assert error.value.code == "capture-hardlink"
    with pytest.raises(CaptureError):
        _layer(
            [_file("a/one", b"d"), UpperEntry("b/two", UpperKind.HARDLINK, link_target="a/one")],
            plmignore="a/one",
        )


def test_byte_limit_is_enforced_without_partial_success():
    with pytest.raises(CaptureError) as error:
        _layer([_file("big", b"x" * 4096)], max_bytes=1024)
    assert error.value.code == "capture-limit"


def test_user_overlay_and_opaque_metadata_validation():
    # user.overlay.opaque on directory accepted and converts to .wh..wh..opq
    _, _, members = _layer([UpperEntry("app/cache", UpperKind.DIRECTORY, xattrs=(("user.overlay.opaque", b"y"),))])
    assert "app/cache/.wh..wh..opq" in members

    # Opaque on regular file rejected
    with pytest.raises(CaptureError) as error:
        _layer([_file("app/data.txt", b"secret", xattrs=(("trusted.overlay.opaque", b"y"),))])
    assert error.value.code == "capture-overlay-metadata"

    # Invalid opaque value rejected
    with pytest.raises(CaptureError) as error:
        _layer([UpperEntry("app/cache", UpperKind.DIRECTORY, xattrs=(("trusted.overlay.opaque", b"x"),))])
    assert error.value.code == "capture-overlay-metadata"

    # user.overlay.redirect rejected
    with pytest.raises(CaptureError) as error:
        _layer([UpperEntry("app/cache", UpperKind.DIRECTORY, xattrs=(("user.overlay.redirect", b"/other"),))])
    assert error.value.code == "capture-overlay-metadata"


def test_tar_member_ordering_is_strictly_canonical():
    # .wh.old must sort before new.txt in the same directory because '.' < 'n' in ASCII
    entries = [
        _file("opt/app/new.txt", b"new"),
        UpperEntry("opt/app/old", UpperKind.WHITEOUT),
    ]
    _, raw, _ = _layer(entries)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        names = archive.getnames()
    assert names == ["opt/app/.wh.old", "opt/app/new.txt"]


def test_plmignore_resolution_from_upper_and_parent():
    # Upper /.plmignore file is read automatically when plmignore=None
    entries = [
        _file(".plmignore", b"ignored.txt\n"),
        _file("ignored.txt", b"drop"),
        _file("kept.txt", b"keep"),
    ]
    receipt, _, members = _layer(entries)
    assert set(members) == {".plmignore", "kept.txt"}
    assert receipt.ignored == ("ignored.txt",)

    # When .plmignore is absent from upper, parent_plmignore is consulted
    receipt, _, members = _layer(
        [_file("temp.bin", b"x"), _file("stay.txt", b"y")],
        parent_plmignore="temp.bin\n",
    )
    assert set(members) == {"stay.txt"}
    assert receipt.ignored == ("temp.bin",)


def test_payload_size_mismatch_fails_closed():
    # Reported size 10 but stream yields 4 bytes -> fail closed
    bad_entry = UpperEntry("corrupt.bin", UpperKind.FILE, size=10, payload=b"1234")
    with pytest.raises(CaptureError):
        _layer([bad_entry])


def test_all_mandatory_identity_paths_are_hidden_when_in_parent():
    parent_paths = [
        "etc/ssh/ssh_host_rsa_key",
        "var/lib/cloud/instance/boot.log",
        "var/lib/dhcp/dhclient.leases",
        "root/.ssh/authorized_keys",
        "home/ubuntu/.ssh/authorized_keys",
        "etc/machine-id",  # platform bound: no whiteout
        "etc/hostname",  # platform bound: no whiteout
    ]
    receipt, _, members = _layer(
        [_file("app/run", b"1")],
        parent_paths=parent_paths,
        injected_placeholders=["root/.ssh/authorized_keys", "home/ubuntu/.ssh/authorized_keys"],
    )
    assert "etc/ssh/.wh.ssh_host_rsa_key" in members
    assert "var/lib/.wh.cloud" in members
    assert "var/lib/dhcp/.wh.dhclient.leases" in members
    assert "root/.ssh/.wh.authorized_keys" in members
    assert "home/ubuntu/.ssh/.wh.authorized_keys" in members
    # Platform-bound files need no whiteout (spec.md:639)
    assert "etc/.wh.machine-id" not in members
    assert "etc/.wh.hostname" not in members


def test_unclassified_authorized_keys_in_upper_fails_closed():
    # An unclassified authorized_keys in upper fails closed to prevent leaking credentials
    with pytest.raises(CaptureError) as error:
        _layer([_file("root/.ssh/authorized_keys", b"ssh-ed25519 AAA...")])
    assert error.value.code == "capture-provenance"


def test_unclassified_authorized_keys_in_parent_fails_closed():
    # An unclassified authorized_keys in parent fails closed to prevent inheriting injected credentials
    with pytest.raises(CaptureError) as error:
        _layer([_file("app.py", b"1")], parent_paths=["home/ubuntu/.ssh/authorized_keys"])
    assert error.value.code == "capture-provenance"


def test_user_authored_authorized_keys_are_preserved_with_explicit_classification():
    # Explicitly classified user_authored_keys are preserved in upper and not whiteouted in parent
    user_key = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIUserKey user@box\n"
    entries = [
        _file("root/.ssh/authorized_keys", user_key),
        _file("home/developer/.ssh/authorized_keys", user_key),
        _file("app/main.py", b"pass"),
    ]
    parent_paths = [
        "home/operator/.ssh/authorized_keys",
        "etc/ssh/ssh_host_rsa_key",
    ]
    receipt, _, members = _layer(
        entries,
        user_authored_keys=[
            "root/.ssh/authorized_keys",
            "home/developer/.ssh/authorized_keys",
            "home/operator/.ssh/authorized_keys",
        ],
        parent_paths=parent_paths,
    )
    assert "root/.ssh/authorized_keys" in members
    assert members["root/.ssh/authorized_keys"][1] == user_key
    assert "home/developer/.ssh/authorized_keys" in members
    assert members["home/developer/.ssh/authorized_keys"][1] == user_key
    assert "root/.ssh/authorized_keys" not in receipt.excluded
    assert "home/developer/.ssh/authorized_keys" not in receipt.excluded

    # Parent user-authored authorized_keys has no whiteout
    assert "home/operator/.ssh/.wh.authorized_keys" not in members
    assert "home/operator/.ssh/authorized_keys" not in receipt.hidden
    # Parent SSH host key is still hidden
    assert "etc/ssh/.wh.ssh_host_rsa_key" in members
    assert "etc/ssh/ssh_host_rsa_key" in receipt.hidden


def test_injected_authorized_keys_are_excluded_and_hidden_with_explicit_classification():
    # Explicitly classified injected_placeholders are excluded from upper and whiteouted in parent
    entries = [
        _file("root/.ssh/authorized_keys", b"priv"),
        _file("home/ubuntu/.ssh/authorized_keys", b"priv"),
        _file("app/code.py", b"ok"),
    ]
    receipt, _, members = _layer(
        entries,
        injected_placeholders=["root/.ssh/authorized_keys", "home/ubuntu/.ssh/authorized_keys"],
        parent_paths=["root/.ssh/authorized_keys"],
    )
    assert "root/.ssh/authorized_keys" not in members
    assert "home/ubuntu/.ssh/authorized_keys" not in members
    assert "app/code.py" in members
    assert set(receipt.excluded) == {"root/.ssh/authorized_keys", "home/ubuntu/.ssh/authorized_keys"}
    # Parent path with injected classification is hidden
    assert "root/.ssh/.wh.authorized_keys" in members
    assert "root/.ssh/authorized_keys" in receipt.hidden


def test_guest_claim_alone_cannot_classify_authorized_keys():
    # Guest-provided provenance claim alone without caller classification must fail closed
    entry = UpperEntry("root/.ssh/authorized_keys", UpperKind.FILE, size=4, payload=b"priv", provenance="user")
    with pytest.raises(CaptureError) as error:
        _layer([entry])
    assert error.value.code == "capture-provenance"


def test_nonstandard_home_root_authorized_keys_requires_classification():
    # Conservative matching catches nonstandard home roots
    with pytest.raises(CaptureError) as error:
        _layer([_file("export/home/alice/.ssh/authorized_keys", b"key")])
    assert error.value.code == "capture-provenance"

    with pytest.raises(CaptureError) as error:
        _layer([_file("app.py", b"1")], parent_paths=["var/lib/service/.ssh/authorized_keys"])
    assert error.value.code == "capture-provenance"

    # With explicit user classification, nonstandard root is preserved
    _, _, members = _layer(
        [_file("export/home/alice/.ssh/authorized_keys", b"key")],
        user_authored_keys=["export/home/alice/.ssh/authorized_keys"],
    )
    assert "export/home/alice/.ssh/authorized_keys" in members


def test_conflicting_classification_fails_closed():
    with pytest.raises(CaptureError) as error:
        _layer(
            [_file("root/.ssh/authorized_keys", b"key")],
            injected_placeholders=["root/.ssh/authorized_keys"],
            user_authored_keys=["root/.ssh/authorized_keys"],
        )
    assert error.value.code == "capture-provenance"


def test_mandatory_precedence_prevents_reinclusion_of_injected_keys():
    entries = [_file("root/.ssh/authorized_keys", b"priv")]
    receipt, _, members = _layer(
        entries,
        injected_placeholders=["root/.ssh/authorized_keys"],
        plmignore="!root/.ssh/authorized_keys\n",
    )
    assert "root/.ssh/authorized_keys" not in members
    assert receipt.excluded == ("root/.ssh/authorized_keys",)


def test_unrecognized_entry_provenance_fails_closed():
    entry = UpperEntry("etc/hostname", UpperKind.FILE, size=1, payload=b"a", provenance="unverified-guess")
    with pytest.raises(CaptureError) as error:
        _layer([entry])
    assert error.value.code == "capture-provenance"


def test_cloud_descendants_in_parent_collapse_to_ancestor_whiteout():
    # Multiple descendants under var/lib/cloud in parent must produce exactly
    # one whiteout at var/lib/.wh.cloud and not individual child whiteouts
    parent_paths = [
        "var/lib/cloud/instance/boot.log",
        "var/lib/cloud/data/status.json",
        "var/lib/cloud/handlers",
    ]
    receipt, _, members = _layer([_file("app.txt", b"x")], parent_paths=parent_paths)
    assert "var/lib/.wh.cloud" in members
    assert "var/lib/cloud/instance/.wh.boot.log" not in members
    assert receipt.hidden == ("var/lib/cloud",)


def test_plmignore_size_limits_fail_closed():
    oversized = b"a\n" * (600 * 1024)  # ~1.2 MiB > 1 MiB limit
    # Upper .plmignore
    entries = [_file(".plmignore", oversized)]
    with pytest.raises(CaptureError) as error:
        _layer(entries)
    assert error.value.code == "plmignore"

    # Parent .plmignore
    with pytest.raises(CaptureError) as error:
        _layer([_file("app.py", b"1")], parent_plmignore=oversized)
    assert error.value.code == "plmignore"

    # Direct IgnoreMatcher
    with pytest.raises(CaptureError) as error:
        IgnoreMatcher(oversized)
    assert error.value.code == "plmignore"


def test_streamed_payload_size_excess_fails_closed():
    # Declared size 4 but stream yields 8 bytes -> fail closed
    entry = UpperEntry("overflow.bin", UpperKind.FILE, size=4, payload=b"12345678")
    with pytest.raises(CaptureError) as error:
        _layer([entry])
    assert error.value.code == "capture-payload"


def test_logs_caches_tmp_and_package_database_are_included_by_default():
    entries = [
        _file("var/log/app.log", b"log"),
        _file("var/cache/app/index", b"cache"),
        _file("tmp/work.bin", b"tmp"),
        _file("var/lib/dpkg/status", b"Package: gcc\n"),
    ]
    receipt, _, members = _layer(entries)
    assert set(members) == {
        name for name in ("var/log/app.log", "var/cache/app/index", "tmp/work.bin", "var/lib/dpkg/status")
    }
    assert not receipt.ignored
    assert not receipt.retained


def test_plmignore_may_drop_logs_and_caches_but_never_the_package_database():
    entries = [
        _file("var/log/app.log", b"log"),
        _file("var/cache/app/index", b"cache"),
        _file("var/lib/dpkg/status", b"Package: gcc\n"),
        _file("var/lib/dpkg/info/gcc.list", b"/usr/bin/gcc\n"),
        _file("var/lib/other/data", b"keep"),
    ]
    receipt, _, members = _layer(entries, plmignore="var/log\nvar/cache\nvar/lib/dpkg\nvar/lib/dpkg/**\n")
    assert set(members) == {"var/lib/dpkg/status", "var/lib/dpkg/info/gcc.list", "var/lib/other/data"}
    assert set(receipt.ignored) == {"var/log/app.log", "var/cache/app/index"}
    assert set(receipt.retained) == {"var/lib/dpkg/status", "var/lib/dpkg/info/gcc.list"}
