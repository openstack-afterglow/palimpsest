"""Bounded OCI registry and Docker acquisition into verified local archives."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import ArtifactValidationError, PalimpsestError
from .oci_image import ACQUISITION_PLATFORMS, OCIImageRef, validate_acquisition_platform
from .oci_source import LocalArchiveSource, SourceCAS
from .registry import (
    RegistryProfile,
    add_profile,
    credential_free_subprocess_environment,
    default_registry_config,
    docker_image_inspect_argv,
    docker_save_argv,
    load_registry_config,
    normalize_endpoint,
    resolve_docker_config_dir,
    resolve_image_reference,
    run_docker_command,
)
from .state import StatePaths
from .web_job_client import defer_web_cancellation

SUPPORTED_PLATFORM = "linux/amd64"
DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_POLL_SECONDS = 0.05


def _selected_architecture(platform: str) -> str:
    try:
        return validate_acquisition_platform(platform)
    except ArtifactValidationError as exc:
        raise RegistryIntakeError(str(exc)) from None


class RegistryIntakeError(PalimpsestError):
    """An anonymous TLS registry reference could not become a verified local archive."""


class RegistryImageNotFoundError(RegistryIntakeError):
    """The image was genuinely not found in the registry or daemon."""


class RegistryAuthenticationError(RegistryIntakeError):
    """Registry authentication or authorization failed."""


class RegistryTLSError(RegistryIntakeError):
    """Registry TLS verification or handshake failed."""


class RegistryNetworkError(RegistryIntakeError):
    """Network connection or DNS resolution to the registry failed."""


@dataclass(frozen=True, slots=True)
class RegistryPullReceipt:
    reference: str
    output: Path
    platform: str
    root_digest: str
    manifest_digest: str
    source_cas_id: str
    source_binding_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_digest": self.manifest_digest,
            "output": os.fspath(self.output),
            "platform": self.platform,
            "reference": self.reference,
            "root_digest": self.root_digest,
            "source_binding_digest": self.source_binding_digest,
            "source_cas_id": self.source_cas_id,
        }


def _has_explicit_registry(reference: str) -> bool:
    name = reference.partition("@")[0]
    if "/" not in name:
        return False
    authority = name.split("/", 1)[0]
    return "." in authority or ":" in authority or authority.lower() == "localhost" or authority.startswith("[")


def resolve_anonymous_reference(reference: str):
    """Resolve only explicit, scheme-free registry references."""
    if not isinstance(reference, str) or not _has_explicit_registry(reference):
        raise RegistryIntakeError("OCI registry pull requires a fully qualified registry/repository reference")
    try:
        # Anonymous intake is genuine OCI /v2 transport only; bind its explicit
        # authority as an ephemeral OCI profile rather than consulting native profiles.
        config = default_registry_config()
        authority = normalize_endpoint(reference.partition("@")[0].split("/", 1)[0])
        if all(profile.endpoint != authority for profile in config.registries.values()):
            config = add_profile(config, RegistryProfile(alias="anonymous", endpoint=authority))
        return resolve_image_reference(reference, config)
    except PalimpsestError as exc:
        raise RegistryIntakeError(str(exc)) from None


def skopeo_copy_argv(
    executable: Path,
    reference: str,
    destination: Path,
    *,
    platform: str = SUPPORTED_PLATFORM,
) -> tuple[str, ...]:
    architecture = _selected_architecture(platform)
    return (
        os.fspath(executable),
        "--override-os",
        "linux",
        "--override-arch",
        architecture,
        "copy",
        "--multi-arch=all",
        "--preserve-digests",
        "--src-no-creds",
        "--src-tls-verify=true",
        f"docker://{reference}",
        f"oci-archive:{destination}",
    )


def skopeo_pull_argv(
    executable: Path,
    reference: str,
    destination: Path,
    *,
    profile: RegistryProfile | None = None,
    docker_config_dir: Path | None = None,
    certificate_dir: Path | None = None,
    platform: str = SUPPORTED_PLATFORM,
) -> tuple[str, ...]:
    architecture = _selected_architecture(platform)
    argv: list[str] = [
        os.fspath(executable),
        "--override-os",
        "linux",
        "--override-arch",
        architecture,
        "copy",
        # Retain the index even for tags: system-copy would hide ambiguous
        # platform entries before our descriptor/config verifier sees them.
        "--multi-arch=all",
        "--preserve-digests",
    ]
    if profile is not None and (profile.tls_skip_verify or profile.plain_http):
        argv.append("--src-tls-verify=false")
    else:
        argv.append("--src-tls-verify=true")
    if certificate_dir is not None:
        argv.extend(["--src-cert-dir", os.fspath(certificate_dir)])
    elif profile is not None and profile.ca:
        for ca_path in profile.ca:
            argv.extend(["--src-cert-dir", ca_path])
    auth_file = None
    if docker_config_dir is not None:
        candidate = Path(docker_config_dir) / "config.json"
        if candidate.is_file():
            auth_file = candidate
    if auth_file is not None:
        argv.extend(["--authfile", os.fspath(auth_file)])
    else:
        argv.append("--src-no-creds")
    argv.extend(
        [
            f"docker://{reference}",
            f"oci-archive:{destination}",
        ]
    )
    return tuple(argv)


def skopeo_docker_archive_copy_argv(
    executable: Path,
    docker_archive: Path,
    destination: Path,
    *,
    platform: str = SUPPORTED_PLATFORM,
) -> tuple[str, ...]:
    return (
        os.fspath(executable),
        "--override-os",
        "linux",
        "--override-arch",
        _selected_architecture(platform),
        "copy",
        "--multi-arch=system",
        "--preserve-digests",
        f"docker-archive:{docker_archive}",
        f"oci-archive:{destination}",
    )


def _safe_output_parent(output: Path) -> tuple[Path, os.stat_result]:
    target = output.expanduser().absolute()
    if not target.name or any(component in {"", ".", ".."} for component in target.parts[1:]):
        raise RegistryIntakeError("OCI archive output path must be absolute and canonical")
    try:
        if os.path.lexists(target):
            raise RegistryIntakeError("OCI archive output already exists")
        parent = target.parent.resolve(strict=True)
        visible = parent.stat(follow_symlinks=False)
    except RegistryIntakeError:
        raise
    except OSError:
        raise RegistryIntakeError("OCI archive output parent is unavailable") from None
    if not stat.S_ISDIR(visible.st_mode) or visible.st_uid != os.geteuid() or visible.st_mode & 0o022:
        raise RegistryIntakeError("OCI archive output parent must be owner-bound and not group/world writable")
    if target.parent != parent:
        raise RegistryIntakeError("OCI archive output parent must not traverse symbolic links")
    return target, visible


def _remove_owned_tree(parent_fd: int, name: str) -> None:
    """Remove one generated directory through its held parent authority."""
    try:
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        for entry in os.scandir(directory_fd):
            try:
                if entry.is_dir(follow_symlinks=False):
                    _remove_owned_tree(directory_fd, entry.name)
                else:
                    os.unlink(entry.name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
    finally:
        os.close(directory_fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _isolated_environment(private_home: Path) -> dict[str, str]:
    private_directories = {
        "TMPDIR": private_home / "tmp",
        "XDG_CACHE_HOME": private_home / "cache",
        "XDG_CONFIG_HOME": private_home / "config",
        "XDG_RUNTIME_DIR": private_home / "runtime",
    }
    for directory in private_directories.values():
        directory.mkdir(mode=0o700)
    environment = {
        "HOME": os.fspath(private_home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        **{name: os.fspath(path) for name, path in private_directories.items()},
    }
    for key in ("LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _staged_bytes(staging: Path) -> int:
    total = 0
    try:
        for root, directories, files in os.walk(staging, followlinks=False):
            for name in directories:
                try:
                    metadata = os.lstat(Path(root) / name)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    raise RegistryIntakeError("registry client created an unsafe staging entry")
            for name in files:
                try:
                    metadata = os.lstat(Path(root) / name)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise RegistryIntakeError("registry client created an unsafe staging entry")
                total += metadata.st_size
    except RegistryIntakeError:
        raise
    except OSError:
        raise RegistryIntakeError("cannot inspect registry pull staging") from None
    return total


def _classify_skopeo_error(stderr_text: str, returncode: int) -> RegistryIntakeError:
    """Only protocol image misses advance search; never expose helper stderr."""
    lowered = stderr_text.lower()
    if re.search(
        r"authentication (?:required|failed)|(?:invalid|missing) credentials|"
        r"error (?:getting|looking up) credentials|(?:^|:\s+)(?:unauthorized|denied|forbidden)\b|"
        r"(?:^|:\s+)(?:401|403)\b|(?:status(?: code)?|response status)[:=]?\s+(?:401|403)\b",
        lowered,
    ):
        return RegistryAuthenticationError("registry authentication failed")
    if re.search(
        r"(?:^|:\s+)x509:\s|certificate (?:signed|has expired|is not|verify|verification)|tls handshake|"
        r"(?:^|:\s+)tls:\s|http response to https client",
        lowered,
    ):
        return RegistryTLSError("registry TLS verification failed")
    if any(
        marker in lowered
        for marker in (
            "connection refused",
            "connection reset",
            "connection timed out",
            "no route to host",
            "i/o timeout",
            "context deadline exceeded",
            "dial tcp ",
            "name resolution",
            "no such host",
            "network is unreachable",
        )
    ):
        return RegistryNetworkError("registry network connection failed")
    if any(
        marker in lowered for marker in ("manifest unknown", "name unknown", "repository name not known", "tag unknown")
    ):
        return RegistryImageNotFoundError("image manifest or repository was not found")
    return RegistryIntakeError(f"skopeo could not copy the registry image (exit code {returncode})")


def _run_bounded_copy(
    argv: tuple[str, ...],
    archive: Path,
    private_home: Path,
    *,
    timeout_seconds: float,
    maximum_bytes: int,
    environment: dict[str, str] | None = None,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
) -> None:
    if not 0 < timeout_seconds <= 3600:
        raise RegistryIntakeError("registry pull timeout must be between 0 and 3600 seconds")
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_ARCHIVE_BYTES:
        raise RegistryIntakeError("registry pull archive limit is invalid")
    stderr_path = private_home / "stderr.log"
    stderr_file = None
    process = None
    try:
        with defer_web_cancellation():
            stderr_file = open(stderr_path, "w+b")
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr_file,
                    env=_isolated_environment(private_home) if environment is None else environment,
                    close_fds=True,
                    start_new_session=True,
                )
            except OSError:
                raise RegistryIntakeError("cannot execute the required OCI acquisition tool") from None
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if _staged_bytes(private_home) > maximum_bytes:
                raise RegistryIntakeError("registry response exceeds the OCI archive size limit")
            if time.monotonic() >= deadline:
                raise RegistryIntakeError("registry pull timed out")
            time.sleep(_POLL_SECONDS)
        if process.returncode != 0:
            stderr_file.seek(0)
            captured = stderr_file.read(16384).decode("utf-8", errors="replace")
            raise _classify_skopeo_error(captured, process.returncode)
        if _staged_bytes(private_home) > maximum_bytes:
            raise RegistryIntakeError("registry response exceeds the OCI archive size limit")
    finally:
        with defer_web_cancellation():
            try:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            finally:
                if stderr_file is not None:
                    stderr_file.close()


def _stage_and_publish_oci_archive(
    output: Path,
    roots: StatePaths,
    image_ref: OCIImageRef,
    build_staging: Callable[[Path, Path], None],
    *,
    platform: str = SUPPORTED_PLATFORM,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    maximum_bytes: int = MAX_ARCHIVE_BYTES,
) -> RegistryPullReceipt:
    target, parent_before = _safe_output_parent(output)
    deadline = time.monotonic() + timeout_seconds
    parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    staging: Path | None = None
    try:
        held_parent = os.fstat(parent_fd)
        if (held_parent.st_dev, held_parent.st_ino) != (parent_before.st_dev, parent_before.st_ino):
            raise RegistryIntakeError("OCI archive output parent changed before registry pull")
        staging = Path(tempfile.mkdtemp(prefix=".palimpsest-oci-pull-", dir=target.parent))
        os.chmod(staging, 0o700)
        staged_visible = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
        if (staged_visible.st_dev, staged_visible.st_ino) != (staging.stat().st_dev, staging.stat().st_ino):
            raise RegistryIntakeError("OCI archive staging parent changed during creation")
        archive = staging / "image.oci.tar"
        build_staging(staging, archive)
        os.chmod(archive, 0o600, follow_symlinks=False)
        source_cas = SourceCAS(roots.oci_source_cas)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RegistryIntakeError("registry pull timed out before archive verification")
        snapshot = LocalArchiveSource(archive).snapshot_acquisition(
            image_ref,
            source_cas,
            platform=platform,
            timeout_seconds=remaining,
            maximum_bytes=maximum_bytes,
        )
        parent_after = target.parent.stat(follow_symlinks=False)
        if (parent_after.st_dev, parent_after.st_ino) != (parent_before.st_dev, parent_before.st_ino):
            raise RegistryIntakeError("OCI archive output parent changed during registry pull")
        staging_fd = os.open(staging.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            os.link(archive.name, target.name, src_dir_fd=staging_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            os.chmod(target.name, 0o600, dir_fd=parent_fd, follow_symlinks=False)
        finally:
            os.close(staging_fd)
        os.fsync(parent_fd)
        published_parent = target.parent.stat(follow_symlinks=False)
        if (published_parent.st_dev, published_parent.st_ino) != (parent_before.st_dev, parent_before.st_ino):
            os.unlink(target.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            raise RegistryIntakeError("OCI archive output parent changed during publication")
        return RegistryPullReceipt(
            reference=image_ref.requested_reference,
            output=target,
            platform=f"{snapshot.image.platform.os}/{snapshot.image.platform.architecture}",
            root_digest=snapshot.root.descriptor.digest,
            manifest_digest=snapshot.manifest.descriptor.digest,
            source_cas_id=snapshot.cas_id,
            source_binding_digest=snapshot.binding_digest,
        )
    except FileExistsError:
        raise RegistryIntakeError("OCI archive output already exists") from None
    except RegistryIntakeError:
        raise
    except OSError:
        raise RegistryIntakeError("registry pull filesystem boundary failed") from None
    finally:
        if staging is not None:
            _remove_owned_tree(parent_fd, staging.name)
        os.close(parent_fd)


def pull_anonymous_oci_archive(
    reference: str,
    output: Path,
    roots: StatePaths,
    *,
    platform: str = SUPPORTED_PLATFORM,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    maximum_bytes: int = MAX_ARCHIVE_BYTES,
    skopeo: Path | None = None,
) -> RegistryPullReceipt:
    """Acquire anonymously, verify through SourceCAS, then publish without overwrite."""
    _selected_architecture(platform)
    resolved = resolve_anonymous_reference(reference)
    executable_text = shutil.which("skopeo") if skopeo is None else os.fspath(skopeo)
    if executable_text is None:
        raise RegistryIntakeError("skopeo is required for OCI registry pull")
    try:
        executable = Path(executable_text).expanduser().resolve(strict=True)
    except OSError:
        raise RegistryIntakeError("skopeo executable is unavailable") from None
    if not executable.is_file():
        raise RegistryIntakeError("skopeo executable is not a regular file")
    image_ref = OCIImageRef(resolved.endpoint, resolved.repository, resolved.canonical)

    def _build(staging: Path, archive: Path) -> None:
        _run_bounded_copy(
            skopeo_copy_argv(executable, resolved.canonical, archive, platform=platform),
            archive,
            staging,
            timeout_seconds=timeout_seconds,
            maximum_bytes=maximum_bytes,
        )

    return _stage_and_publish_oci_archive(
        output,
        roots,
        image_ref,
        _build,
        platform=platform,
        timeout_seconds=timeout_seconds,
        maximum_bytes=maximum_bytes,
    )


def _stage_docker_auth(config_dir: Path, endpoint: str, staging: Path, *, timeout_seconds: float) -> Path | None:
    """Resolve only the Docker registry credential domain into private staging."""
    try:
        fd = os.open(config_dir / "config.json", os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise RegistryAuthenticationError("Docker credential configuration cannot be read") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise RegistryAuthenticationError("Docker credential configuration must be a bounded regular file")
            document = json.loads(stream.read(1024 * 1024 + 1))
    except (OSError, UnicodeError, ValueError):
        raise RegistryAuthenticationError("Docker credential configuration is invalid") from None
    if not isinstance(document, dict):
        raise RegistryAuthenticationError("Docker credential configuration is invalid")
    helpers = document.get("credHelpers", {})
    auths = document.get("auths", {})
    if not isinstance(helpers, dict) or not isinstance(auths, dict):
        raise RegistryAuthenticationError("Docker credential configuration is invalid")
    server = (
        "https://index.docker.io/v1/"
        if endpoint in {"docker.io", "index.docker.io", "registry-1.docker.io"}
        else endpoint
    )
    helper = helpers.get(server) or helpers.get(endpoint) or document.get("credsStore") or None
    auth = auths.get(server)
    if auth is None:
        for key in (
            endpoint,
            f"https://{endpoint}/v1/",
            f"http://{endpoint}/v1/",
            f"https://{endpoint}",
            f"http://{endpoint}",
        ):
            if key in auths:
                auth = auths[key]
                break
    if helper is None and auth is None:
        return None
    private = staging / "docker-auth"
    private.mkdir(mode=0o700)
    if helper:
        if not isinstance(helper, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", helper) is None:
            raise RegistryAuthenticationError("Docker credential helper configuration is invalid")
        environment = credential_free_subprocess_environment()
        environment["DOCKER_CONFIG"] = os.fspath(config_dir)
        executable = shutil.which("docker-credential-" + helper, path=environment.get("PATH", os.defpath))
        if executable is None:
            raise RegistryAuthenticationError("configured Docker credential helper is unavailable")
        input_path = private / "server-url"
        input_path.write_text(server + "\n", encoding="utf-8")
        output_path = private / "helper-output.json"
        try:
            with input_path.open("rb") as helper_input, output_path.open("x+b") as helper_output:
                os.fchmod(helper_output.fileno(), 0o600)
                _run_bounded_copy(
                    (executable, "get"),
                    output_path,
                    private,
                    timeout_seconds=min(timeout_seconds, 30),
                    maximum_bytes=1024 * 1024,
                    environment=environment,
                    stdin=helper_input,
                    stdout=helper_output,
                )
        except RegistryIntakeError:
            # Docker helpers use this exact protocol error for an anonymous caller.
            for path in (output_path, private / "stderr.log"):
                if path.is_file():
                    with path.open("rb") as stream:
                        if stream.read(16385).strip() == b"credentials not found in native keychain":
                            return None
            raise RegistryAuthenticationError("Docker credential helper failed") from None
        try:
            credentials = json.loads(output_path.read_bytes())
            username, secret = credentials["Username"], credentials["Secret"]
            if not isinstance(username, str) or not isinstance(secret, str):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise RegistryAuthenticationError("Docker credential helper returned invalid credentials") from None
        auth = (
            {"identitytoken": secret}
            if username == "<token>"
            else {
                "auth": base64.b64encode((username + ":" + secret).encode("utf-8")).decode("ascii"),
            }
        )
    if not isinstance(auth, dict):
        raise RegistryAuthenticationError("Docker registry credentials are invalid")
    auth_file = private / "config.json"
    with auth_file.open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({"auths": {endpoint: auth}}, stream)
    return private


def _stage_registry_ca(profile: RegistryProfile | None, staging: Path) -> Path | None:
    if profile is None or not profile.ca:
        return None
    certificates = staging / "certificates"
    certificates.mkdir(mode=0o700)
    for index, path in enumerate(profile.ca):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                    raise RegistryIntakeError("configured registry CA must be a bounded regular file")
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise RegistryIntakeError("configured registry CA exceeds its size bound")
            (certificates / f"ca-{index}.crt").write_bytes(content)
        except OSError:
            raise RegistryIntakeError("configured registry CA cannot be read") from None
    return certificates


def inspect_registry_reference(
    reference: str,
    roots: StatePaths,
    *,
    profile: RegistryProfile,
    tags: bool = False,
    raw: bool = False,
    timeout_seconds: float = 30,
) -> dict:
    """Bounded skopeo read with the same single-domain Docker helper as pulls."""
    if profile.protocol != "oci" or profile.plain_http or profile.tls_skip_verify:
        raise RegistryIntakeError("remote artifact inspection requires verified OCI HTTPS")
    resolved = resolve_image_reference(reference, load_registry_config(roots), registry_alias=profile.alias)
    if resolved.endpoint != profile.endpoint:
        raise RegistryIntakeError("reference is outside the selected registry connection")
    executable = shutil.which("skopeo")
    if executable is None:
        raise RegistryIntakeError("skopeo is required for OCI registry inspection")
    with tempfile.TemporaryDirectory(prefix="palimpsest-inspect-") as temporary:
        staging = Path(temporary)
        credentials = _stage_docker_auth(
            resolve_docker_config_dir(), resolved.endpoint, staging, timeout_seconds=timeout_seconds
        )
        certificates = _stage_registry_ca(profile, staging)
        command = [executable, "list-tags" if tags else "inspect", "--tls-verify=true"]
        if raw and not tags:
            command.append("--raw")
        if certificates is not None:
            command.extend(["--cert-dir", os.fspath(certificates)])
        if credentials is not None:
            command.extend(["--authfile", os.fspath(credentials / "config.json")])
        else:
            command.append("--no-creds")
        target = resolved.endpoint + "/" + resolved.repository if tags else resolved.canonical
        command.append("docker://" + target)
        output = staging / "response.json"
        with output.open("x+b") as stream:
            _run_bounded_copy(
                tuple(command),
                output,
                staging,
                timeout_seconds=timeout_seconds,
                maximum_bytes=4 * 1024 * 1024,
                stdout=stream,
            )
            stream.seek(0)
            payload = stream.read(4 * 1024 * 1024 + 1)
        try:
            document = json.loads(payload)
        except (ValueError, UnicodeError):
            raise RegistryIntakeError("registry inspection returned invalid JSON") from None
        if not isinstance(document, dict):
            raise RegistryIntakeError("registry inspection returned an invalid document")
        return document


def pull_registry_oci_archive(
    reference: str,
    output: Path,
    roots: StatePaths,
    *,
    profile: RegistryProfile | None = None,
    docker_config_dir: Path | None = None,
    platform: str = SUPPORTED_PLATFORM,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    maximum_bytes: int = MAX_ARCHIVE_BYTES,
    skopeo: Path | None = None,
) -> RegistryPullReceipt:
    """Acquire an OCI image from a registry using configured profile and credentials."""
    _selected_architecture(platform)
    config_dir = docker_config_dir or resolve_docker_config_dir()
    executable_text = shutil.which("skopeo") if skopeo is None else os.fspath(skopeo)
    if executable_text is None:
        raise RegistryIntakeError("skopeo is required for OCI registry pull")
    try:
        executable = Path(executable_text).expanduser().resolve(strict=True)
    except OSError:
        raise RegistryIntakeError("skopeo executable is unavailable") from None
    if not executable.is_file():
        raise RegistryIntakeError("skopeo executable is not a regular file")

    config = load_registry_config(roots)
    if profile is not None:
        if profile.protocol != "oci":
            raise RegistryIntakeError("native profiles cannot be acquired through Docker /v2")
        config = add_profile(config, profile, replace_existing=True)
    resolved = resolve_image_reference(reference, config, registry_alias=profile.alias if profile else None)
    selected_profile = config.registries.get(resolved.registry_alias)
    if selected_profile is not None and selected_profile.protocol != "oci":
        raise RegistryIntakeError("native profiles cannot be acquired through Docker /v2")
    image_ref = OCIImageRef(resolved.endpoint, resolved.repository, resolved.canonical)

    def _build(staging: Path, archive: Path) -> None:
        certificates = _stage_registry_ca(selected_profile, staging)
        auth_config = _stage_docker_auth(config_dir, resolved.endpoint, staging, timeout_seconds=timeout_seconds)
        argv = skopeo_pull_argv(
            executable,
            resolved.canonical,
            archive,
            profile=selected_profile,
            docker_config_dir=auth_config,
            certificate_dir=certificates,
            platform=platform,
        )
        _run_bounded_copy(
            argv,
            archive,
            staging,
            timeout_seconds=timeout_seconds,
            maximum_bytes=maximum_bytes,
        )

    return _stage_and_publish_oci_archive(
        output,
        roots,
        image_ref,
        _build,
        platform=platform,
        timeout_seconds=timeout_seconds,
        maximum_bytes=maximum_bytes,
    )


def export_docker_image_to_oci_archive(
    reference: str,
    output: Path,
    roots: StatePaths,
    *,
    docker_config_dir: Path | None = None,
    platform: str = SUPPORTED_PLATFORM,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    maximum_bytes: int = MAX_ARCHIVE_BYTES,
    skopeo: Path | None = None,
) -> RegistryPullReceipt:
    """Export and verify the Docker image config against the requested platform."""
    _selected_architecture(platform)
    config_dir = docker_config_dir or resolve_docker_config_dir()
    executable_text = shutil.which("skopeo") if skopeo is None else os.fspath(skopeo)
    if executable_text is None:
        raise RegistryIntakeError("skopeo is required for OCI export")
    try:
        executable = Path(executable_text).expanduser().resolve(strict=True)
    except OSError:
        raise RegistryIntakeError("skopeo executable is unavailable") from None
    if not executable.is_file():
        raise RegistryIntakeError("skopeo executable is not a regular file")

    inspect_argv = docker_image_inspect_argv(config_dir, [reference])
    try:
        inspect_res = run_docker_command(inspect_argv, timeout_seconds=30, check=False)
    except PalimpsestError:
        raise RegistryIntakeError("cannot inspect the configured local Docker daemon") from None
    if inspect_res.returncode != 0:
        detail = str(inspect_res.stderr or "").lower()
        if "no such image" in detail or "no such object" in detail:
            raise RegistryImageNotFoundError("image was not found in the configured local Docker daemon")
        raise RegistryIntakeError("local Docker image inspection failed; not an image miss")

    resolved = resolve_image_reference(
        reference, default_registry_config(), registry_alias=None if _has_explicit_registry(reference) else "docker"
    )
    image_ref = OCIImageRef(resolved.endpoint, resolved.repository, resolved.canonical)

    def _build(staging: Path, archive: Path) -> None:
        docker_tar = staging / "docker-export.tar"
        save_argv = docker_save_argv(config_dir, [reference], output=docker_tar)
        _run_bounded_copy(
            tuple(save_argv),
            docker_tar,
            staging,
            timeout_seconds=timeout_seconds,
            maximum_bytes=maximum_bytes,
            environment=credential_free_subprocess_environment(),
        )
        if not docker_tar.is_file():
            raise RegistryIntakeError("docker save did not produce an archive file")
        convert_argv = skopeo_docker_archive_copy_argv(executable, docker_tar, archive, platform=platform)
        _run_bounded_copy(
            convert_argv,
            archive,
            staging,
            timeout_seconds=timeout_seconds,
            maximum_bytes=maximum_bytes,
        )

    return _stage_and_publish_oci_archive(
        output,
        roots,
        image_ref,
        _build,
        platform=platform,
        timeout_seconds=timeout_seconds,
        maximum_bytes=maximum_bytes,
    )


__all__ = [
    "ACQUISITION_PLATFORMS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ARCHIVE_BYTES",
    "RegistryAuthenticationError",
    "RegistryImageNotFoundError",
    "RegistryIntakeError",
    "RegistryNetworkError",
    "RegistryPullReceipt",
    "RegistryTLSError",
    "SUPPORTED_PLATFORM",
    "export_docker_image_to_oci_archive",
    "pull_anonymous_oci_archive",
    "pull_registry_oci_archive",
    "inspect_registry_reference",
    "resolve_anonymous_reference",
    "skopeo_copy_argv",
    "skopeo_docker_archive_copy_argv",
    "skopeo_pull_argv",
]
