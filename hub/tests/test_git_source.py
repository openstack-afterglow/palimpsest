from __future__ import annotations

import subprocess

import pytest
from pydantic import ValidationError

from palimpsest_hub.services import git_source
from palimpsest_hub.services.git_source import GitBuildRequest, GitSourceError, PinnedSource

SHA = "a" * 40
ALLOWED = ["github.com"]


def request(url="https://github.com/o/r.git", revision=SHA, **extra):
    return GitBuildRequest(repo_url=url, revision=revision, **extra)


def public(host, port):
    return ["140.82.112.3"]


def test_valid_request_is_pinned_to_the_first_resolved_public_address():
    pinned = git_source.validate(request(), ALLOWED, resolver=lambda h, p: ["140.82.112.3", "140.82.112.4"])
    assert (pinned.host, pinned.port, pinned.address, pinned.revision) == ("github.com", 443, "140.82.112.3", SHA)


@pytest.mark.parametrize("revision", ["main", "v1.0", SHA[:7], "g" * 40, SHA + "0", ""])
def test_revision_must_be_a_full_commit_sha(revision):
    with pytest.raises(ValidationError):
        request(revision=revision)


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../x", "a\\b", "a\x00b"])
def test_paths_must_stay_inside_the_repository(path):
    with pytest.raises(ValidationError):
        request(dockerfile_path=path)
    with pytest.raises(ValidationError):
        request(context_subdir=path)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://github.com/o/r.git", "scheme"),
        ("git://github.com/o/r.git", "scheme"),
        ("file:///srv/repo", "scheme"),
        ("ssh://github.com/o/r.git", "scheme"),
        ("https://user:token@github.com/o/r.git", "userinfo"),
        ("https://token@github.com/o/r.git", "userinfo"),
        ("https://github.com/o/r.git?x=1", "url"),
        ("https://github.com/o/r.git#frag", "url"),
        ("https://10.0.0.5/o/r.git", "host"),
        ("https://[::1]/o/r.git", "host"),
        ("https://evil.example/o/r.git", "host-not-allowed"),
    ],
)
def test_url_is_rejected_before_any_dns_lookup(url, code):
    def resolver(host, port):
        raise AssertionError("DNS must not be consulted for a rejected URL")

    with pytest.raises(GitSourceError) as raised:
        git_source.validate(request(url=url), ALLOWED, resolver=resolver)
    assert raised.value.code == code


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:169.254.169.254",
        "224.0.0.1",
    ],
)
def test_any_non_public_resolution_is_rejected_even_when_a_public_address_is_also_returned(address):
    with pytest.raises(GitSourceError) as raised:
        git_source.validate(request(), ALLOWED, resolver=lambda h, p: ["140.82.112.3", address])
    assert raised.value.code == "address"


def test_unresolvable_host_is_rejected():
    with pytest.raises(GitSourceError) as raised:
        git_source.validate(request(), ALLOWED, resolver=lambda h, p: [])
    assert raised.value.code == "dns"


def test_allow_list_comparison_ignores_case_and_trailing_dot():
    pinned = git_source.validate(request(url="https://GitHub.com./o/r.git"), ["GITHUB.COM"], resolver=public)
    assert pinned.host == "github.com"


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "origin"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "Dockerfile").write_text("FROM scratch\n")
    (root / "app.txt").write_text("one\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    first = _git(root, "rev-parse", "HEAD")
    (root / "app.txt").write_text("two\n")
    _git(root, "commit", "-q", "-am", "two")
    return root, first, _git(root, "rev-parse", "HEAD")


def pinned(root, sha):
    return PinnedSource(root.as_uri(), "localhost", 443, "127.0.0.1", sha)


def test_fetch_checks_out_exactly_the_pinned_revision_without_git_metadata(repo, tmp_path):
    root, first, _ = repo
    out = git_source.fetch(pinned(root, first), tmp_path / "ctx", allow_schemes="file")
    assert (out / "app.txt").read_text() == "one\n"
    assert (out / "Dockerfile").exists()
    assert not (out / ".git").exists()


def test_fetch_refuses_a_missing_revision_and_an_existing_destination(repo, tmp_path):
    root, _, _ = repo
    with pytest.raises(GitSourceError) as raised:
        git_source.fetch(pinned(root, "b" * 40), tmp_path / "ctx", allow_schemes="file")
    assert raised.value.code == "fetch"
    (tmp_path / "taken").mkdir()
    with pytest.raises(GitSourceError) as raised:
        git_source.fetch(pinned(root, "b" * 40), tmp_path / "taken", allow_schemes="file")
    assert raised.value.code == "dest"


def test_fetch_is_refused_when_the_protocol_is_not_allowed(repo, tmp_path):
    root, first, _ = repo
    with pytest.raises(GitSourceError) as raised:
        git_source.fetch(pinned(root, first), tmp_path / "ctx", allow_schemes="https")
    assert raised.value.code == "fetch"


def test_tree_caps_are_enforced_and_escaping_symlinks_are_refused(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "a").write_bytes(b"x" * 10)
    (tree / "b").write_bytes(b"x" * 10)
    assert git_source.check_tree(tree) == (20, 2)
    with pytest.raises(GitSourceError) as raised:
        git_source.check_tree(tree, max_bytes=19)
    assert raised.value.code == "size"
    with pytest.raises(GitSourceError) as raised:
        git_source.check_tree(tree, max_files=1)
    assert raised.value.code == "size"
    (tree / "link").symlink_to("/etc/passwd")
    with pytest.raises(GitSourceError) as raised:
        git_source.check_tree(tree)
    assert raised.value.code == "symlink"
    (tree / "link").unlink()
    (tree / "inside").symlink_to("a")
    assert git_source.check_tree(tree) == (20, 2)


def test_credential_is_a_reference_never_a_value():
    assert request(credential_ref="deploy-key.1").credential_ref == "deploy-key.1"
    with pytest.raises(ValidationError):
        request(credential_ref="tok en/with space")
