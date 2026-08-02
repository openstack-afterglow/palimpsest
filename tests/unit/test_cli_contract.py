"""Compatibility and safety contract for the standalone argparse CLI."""

from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path

import pytest

from palimpsest_local import cli, digest
from palimpsest_local.errors import PalimpsestError
from palimpsest_local.oci_layout import ContentStore


def test_cli_uses_only_stdlib_and_package_imports():
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    top_level = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            top_level.add(node.module.split(".")[0])
    assert top_level <= {
        "__future__",
        "argparse",
        "collections",
        "datetime",
        "json",
        "os",
        "pathlib",
        "shutil",
        "subprocess",
        "sys",
        "tempfile",
        "tomllib",
        "typing",
    }


def test_cli_never_invokes_a_host_shell():
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source


def test_digest_file_streams_hash(tmp_path: Path):
    payload = b"palimpsest layer bytes" * 100
    path = tmp_path / "layer.squashfs"
    path.write_bytes(payload)
    assert cli.digest_file(path) == f"sha256:{hashlib.sha256(payload).hexdigest()}"
    assert "read_bytes()" not in inspect.getsource(digest.digest_file)


def test_run_image_resolution_pulls_missing_verified_cloud_image_from_selected_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    store = ContentStore(roots.store)
    payload = b"ubuntu-arm64-cloud-image"
    digest_value = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    calls: list[object] = []

    class FakeHub:
        def __init__(self, url: str, token: str):
            calls.append((url, token))

        def get_layer(self, requested: str):
            assert requested == digest_value
            return {"kind": "cloud-image", "disk_format": "raw", "arch": "aarch64", "os_variant": "ubuntu"}

        def pull_blob(self, requested: str, target: Path):
            assert requested == digest_value
            target.write_bytes(payload)
            return target

    monkeypatch.setattr(cli, "HubClient", FakeHub)
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")

    image = cli._resolve_image_ref(store, digest_value, "https://hub.example.test")

    assert image.arch == "aarch64"
    assert image.local_path.read_bytes() == payload
    assert calls == [("https://hub.example.test", "test-token")]


def test_pack_command_matches_fixed_mksquashfs_argv(tmp_path: Path):
    source = tmp_path / "rootfs"
    source.mkdir()
    assert cli.build_mksquashfs_command(source, tmp_path / "out.squashfs") == [
        "mksquashfs",
        str(source),
        str(tmp_path / "out.squashfs"),
        "-comp",
        "zstd",
        "-Xcompression-level",
        "3",
        "-noappend",
        "-no-exports",
    ]
    with pytest.raises(PalimpsestError):
        cli.build_mksquashfs_command(tmp_path / "missing", tmp_path / "out.squashfs")


def test_hub_environment_errors_are_actionable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("PALIMPSEST_URL", raising=False)
    monkeypatch.delenv("PALIMPSEST_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    with pytest.raises(PalimpsestError, match="Hub URL is required"):
        cli.resolve_url(None)
    with pytest.raises(PalimpsestError, match="PALIMPSEST_TOKEN"):
        cli.resolve_token()


def test_explicit_then_environment_then_config_url_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = tmp_path / "config" / "palimpsest" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[hub]\nurl = 'https://configured.example/'\n", encoding="utf-8")
    assert cli.resolve_url(None) == "https://configured.example"
    monkeypatch.setenv("PALIMPSEST_URL", "https://environment.example/")
    assert cli.resolve_url(None) == "https://environment.example"
    assert cli.resolve_url("https://explicit.example/") == "https://explicit.example"


def test_exact_nested_command_tree_and_defaults():
    parser = cli.build_parser()
    top = next(action for action in parser._actions if isinstance(action, __import__("argparse")._SubParsersAction))
    assert set(top.choices) == {
        "image",
        "layer",
        "bundle",
        "build",
        "run",
        "ps",
        "inspect",
        "logs",
        "shell",
        "exec",
        "stop",
        "rm",
        "commit",
    }
    assert parser.parse_args(["image", "ls"]).limit == 50
    assert parser.parse_args(["layer", "ls"]).limit == 50
    assert parser.parse_args(["run", "sha256:" + "a" * 64, "--name", "demo"]).memory == 4096
    assert parser.parse_args(["build", "--base", "sha256:" + "a" * 64, "--tag", "layer"]).network == "none"
    assert parser.parse_args(["bundle", "pull", "sha256:" + "a" * 64, "--output", "out"]).include_base is False


@pytest.mark.parametrize(
    "argv",
    [
        [
            "image",
            "ls",
            "--ubuntu-base",
            "24.04",
            "--arch",
            "aarch64",
            "--os-variant",
            "ubuntu24.04",
            "--disk-format",
            "raw",
            "--limit",
            "200",
        ],
        ["image", "pull", "sha256:" + "a" * 64, "--output", "out"],
        ["image", "verify", "base.raw", "--digest", "sha256:" + "a" * 64],
        [
            "image",
            "push",
            "base.raw",
            "--name",
            "base",
            "--disk-format",
            "raw",
            "--arch",
            "aarch64",
            "--os-variant",
            "ubuntu24.04",
            "--ubuntu-base",
            "24.04",
            "--publish",
        ],
        ["layer", "ls", "--name", "layer", "--kind", "squashfs", "--parent", "sha256:" + "a" * 64, "--limit", "1"],
        ["layer", "pull", "sha256:" + "a" * 64, "--output", "out"],
        ["layer", "pack", "rootfs", "--tag", "layer"],
        [
            "layer",
            "push",
            "layer",
            "--name",
            "layer",
            "--parent",
            "sha256:" + "a" * 64,
            "--base-image",
            "sha256:" + "b" * 64,
            "--ubuntu-base",
            "24.04",
            "--publish",
        ],
        ["bundle", "pull", "sha256:" + "a" * 64, "--output", "out", "--include-base"],
        ["bundle", "verify", "layout"],
        [
            "build",
            "--base",
            "sha256:" + "a" * 64,
            "--tag",
            "layer",
            "-f",
            "Recipe",
            "--layer",
            "sha256:" + "b" * 64,
            "--network",
            "default",
        ],
        [
            "run",
            "stack",
            "--name",
            "demo",
            "--layer",
            "sha256:" + "a" * 64,
            "--memory",
            "512",
            "--vcpus",
            "3",
            "--network",
            "isolated",
        ],
        ["ps"],
        ["inspect", "demo"],
        ["logs", "demo", "--follow"],
        ["shell", "demo"],
        ["exec", "demo", "--", "printf", "%s", "hello"],
        ["stop", "demo"],
        ["rm", "demo", "--volumes"],
        ["commit", "demo", "--tag", "layer"],
    ],
)
def test_parser_accepts_every_v1_command_form(argv: list[str]):
    cli.build_parser().parse_args(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["images"],
        ["--token", "secret", "ps"],
        ["build", "--base", "sha256:" + "a" * 64, "--tag", "layer", "--file", "Recipe"],
        ["image", "ls", "--publish"],
    ],
)
def test_parser_rejects_prohibited_legacy_aliases_and_flags(argv: list[str], capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as rejected:
        cli.build_parser().parse_args(argv)
    assert rejected.value.code == 2
    assert capsys.readouterr().err


def test_exec_requires_nonempty_remainder(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["exec", "demo", "--"]) == 2
    assert "command" in capsys.readouterr().err


def test_cli_dispatch_image_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    img_file = tmp_path / "test.qcow2"
    img_file.write_bytes(b"qcow2 header content")
    d = digest.digest_file(img_file)
    ret = cli.main(["image", "verify", str(img_file), "--digest", d])
    assert ret == 0
    assert "ok" in capsys.readouterr().out


def test_cli_dispatch_ps(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(
        "palimpsest_local.cli.ps",
        lambda roots=None: [
            {
                "name": "demo-vm",
                "status": "running",
                "base_digest": "sha256:" + "a" * 64,
                "layers_count": 2,
                "guest_ip": "192.168.122.50",
                "created_at": "2026-07-30T00:00:00Z",
            }
        ],
    )
    ret = cli.main(["ps"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "demo-vm" in out
    assert "running" in out
    assert "192.168.122.50" in out


def test_cli_dispatch_image_ls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("PALIMPSEST_URL", "http://localhost:8080")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")
    monkeypatch.setattr(
        "palimpsest_local.cli.HubClient.list_images",
        lambda self, **kwargs: [
            {"digest": "sha256:" + "a" * 64, "name": "ubuntu-24.04", "disk_format": "qcow2", "arch": "x86_64"}
        ],
    )
    ret = cli.main(["image", "ls"])
    assert ret == 0
    assert "ubuntu-24.04" in capsys.readouterr().out


def test_cli_dispatch_layer_ls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("PALIMPSEST_URL", "http://localhost:8080")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")
    monkeypatch.setattr(
        "palimpsest_local.cli.HubClient.list_layers",
        lambda self, **kwargs: [{"digest": "sha256:" + "b" * 64, "name": "python-layer", "kind": "squashfs"}],
    )
    ret = cli.main(["layer", "ls"])
    assert ret == 0
    assert "python-layer" in capsys.readouterr().out


def test_cli_dispatch_stop_and_rm(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    stopped = []
    removed = []
    monkeypatch.setattr("palimpsest_local.cli.stop", lambda name, roots=None: stopped.append(name))
    monkeypatch.setattr(
        "palimpsest_local.cli.rm", lambda name, roots=None, volumes=False: removed.append((name, volumes))
    )

    assert cli.main(["stop", "demo-vm"]) == 0
    assert stopped == ["demo-vm"]
    assert "stopped demo-vm" in capsys.readouterr().out

    assert cli.main(["rm", "demo-vm", "--volumes"]) == 0
    assert removed == [("demo-vm", True)]
    assert "removed demo-vm" in capsys.readouterr().out


def test_cli_dispatch_build_routes_verified_spec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    base_bytes = b"base"
    base_digest = f"sha256:{hashlib.sha256(base_bytes).hexdigest()}"
    store = ContentStore(tmp_path / "state" / "palimpsest" / "store")
    store.blobs_dir.mkdir(parents=True)
    store.blob_path(base_digest).write_bytes(base_bytes)
    store.write_metadata(
        base_digest,
        {"kind": "cloud-image", "disk_format": "qcow2", "arch": "x86_64", "os_variant": None},
    )
    recipe = tmp_path / "Palimpsestfile"
    recipe.write_text(f"FROM {base_digest}\nRUN true\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        "palimpsest_local.cli.build_layer",
        lambda spec, *, roots: (captured.append((spec, roots)), {"output_digest": "sha256:" + "a" * 64})[1],
    )

    assert cli.main(["build", "--base", base_digest, "--tag", "result", "--network", "default", "-f", str(recipe)]) == 0
    assert captured[0][0].base.digest == base_digest
    assert captured[0][0].network == "default"
    assert "sha256:" in capsys.readouterr().out


def test_cli_dispatch_commit_routes_runtime(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(
        "palimpsest_local.cli.commit",
        lambda name, tag, *, roots: {"name": name, "tag": tag, "digest": "sha256:" + "b" * 64},
    )
    assert cli.main(["commit", "run-one", "--tag", "delta"]) == 0
    assert "sha256:" in capsys.readouterr().out


def test_cli_dispatch_build_integrity_guard(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    recipe = tmp_path / "Palimpsestfile"
    recipe.write_text(f"FROM sha256:{'a' * 64}\nRUN echo hi\n")
    ret = cli.main(["build", "--base", "sha256:" + "b" * 64, "--tag", "my-layer", "-f", str(recipe)])
    assert ret == 1
    err = capsys.readouterr().err
    assert "base digest mismatch" in err


def test_cli_dispatch_image_push_reaches_hub_and_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    img = tmp_path / "ubuntu.qcow2"
    img.write_bytes(b"qcow2 fake data")
    monkeypatch.setenv("PALIMPSEST_URL", "http://localhost:8080")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")

    pushed_args = []
    ingested_paths = []

    monkeypatch.setattr(
        "palimpsest_local.cli.HubClient.push_blob",
        lambda self, path, metadata, resume=True: (
            pushed_args.append((path, metadata)),
            {"blob_digest": "sha256:" + "c" * 64},
        )[1],
    )
    monkeypatch.setattr(
        "palimpsest_local.cli.ContentStore.ingest_file",
        lambda self, source, expected_digest=None: (
            ingested_paths.append(source),
            tmp_path / "store" / "blob",
        )[1],
    )
    monkeypatch.setattr("palimpsest_local.cli.ContentStore.write_metadata", lambda self, digest, metadata: None)

    ret = cli.main(["image", "push", str(img), "--name", "my-img", "--disk-format", "qcow2"])
    assert ret == 0
    assert len(pushed_args) == 1
    assert pushed_args[0][1]["name"] == "my-img"
    assert len(ingested_paths) == 1
    assert ingested_paths[0] == img


def test_cli_dispatch_run_passes_cli_layers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import hashlib

    base_content = b"base qcow2 content"
    layer_content = b"layer squashfs content"
    base_hex = hashlib.sha256(base_content).hexdigest()
    layer_hex = hashlib.sha256(layer_content).hexdigest()
    base_digest = f"sha256:{base_hex}"
    layer_digest = f"sha256:{layer_hex}"

    store_dir = tmp_path / "state" / "palimpsest" / "store" / "blobs" / "sha256"
    store_dir.mkdir(parents=True)
    (store_dir / base_hex).write_bytes(base_content)
    (store_dir / layer_hex).write_bytes(layer_content)
    store = ContentStore(tmp_path / "state" / "palimpsest" / "store")
    store.write_metadata(
        base_digest,
        {
            "kind": "cloud-image",
            "disk_format": "qcow2",
            "arch": "x86_64",
            "os_variant": None,
        },
    )
    store.write_metadata(
        layer_digest,
        {
            "kind": "squashfs",
            "media_type": "application/vnd.afterglow.palimpsest.layer.squashfs.v1",
            "parent_digest": None,
            "base_image_digest": base_digest,
        },
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    recorded_specs = []
    monkeypatch.setattr(
        "palimpsest_local.cli.run",
        lambda spec, roots=None: (recorded_specs.append(spec), {"guest_ip": "10.0.0.5"})[1],
    )

    ret = cli.main(["run", base_digest, "--name", "test-vm", "--layer", layer_digest])
    assert ret == 0
    assert len(recorded_specs) == 1
    spec = recorded_specs[0]
    assert spec.name == "test-vm"
    assert len(spec.stack.layers) == 1
    assert spec.stack.layers[0].digest == layer_digest


def test_cli_dispatch_image_pull_fresh_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    d = "sha256:" + "d" * 64
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PALIMPSEST_URL", "http://localhost:8080")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")

    monkeypatch.setattr(
        "palimpsest_local.cli.HubClient.get_layer",
        lambda self, digest: {"digest": d, "kind": "cloud-image", "disk_format": "qcow2", "arch": "x86_64"},
    )

    pulled_destinations = []

    def mock_pull(self, digest, destination, resume=True):
        pulled_destinations.append(destination)
        destination.write_bytes(b"downloaded blob content")
        return destination

    monkeypatch.setattr("palimpsest_local.cli.HubClient.pull_blob", mock_pull)

    ret = cli.main(["image", "pull", d])
    assert ret == 0
    assert len(pulled_destinations) == 1
    assert pulled_destinations[0].exists()


def test_cli_image_import_ingests_verified_cloud_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    source = tmp_path / "ubuntu-arm64.img"
    payload = b"ubuntu-arm64-cloud-image"
    source.write_bytes(payload)
    expected_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    monkeypatch.setattr(cli, "init_roots", lambda: roots)

    assert cli.main(["image", "import", str(source), "--disk-format", "qcow2", "--arch", "aarch64"]) == 0

    store = ContentStore(roots.store)
    assert store.read_metadata(expected_digest)["arch"] == "aarch64"


def test_cli_rejects_commit_for_lima_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    monkeypatch.setattr(cli, "init_roots", lambda: roots)
    monkeypatch.setattr(cli.lima, "is_lima_run", lambda _rpaths: True)

    assert cli.main(["commit", "mac-vm", "--tag", "layer"]) == 1
    assert "use palimpsest build" in capsys.readouterr().err
