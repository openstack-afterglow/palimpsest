"""Finite, journalled CephX consumers bound to one CephFS path AND RADOS namespace.

No Manila-rule fallback, admin fallback, auth get-or-create, or mutation retries.
The caller reserves the access_rules budget before entering this manager.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID

from .cloud_env import CEPHX_KEY_RE, CloudEnvError, _ceph_source_identity

_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_POOL = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_SCOPE = re.compile(r"public|(?:user|project)-[0-9a-f]{32}\Z")
_OUTPUT_LIMIT = 65536
_ERROR_LIMIT = 8192
_COMMAND_SECONDS = 30
_TERM_SECONDS = 5


class CephCommandError(CloudEnvError):
    def __init__(self, message, *, command_pending, started=True):
        super().__init__(message)
        self.command_pending, self.started = command_pending, started


def _access_denied(code, output, error):
    return not output.strip() and (code, error.strip()) in {
        (13, b"Error EACCES: access denied"),
        (1, b"Error EPERM: Operation not permitted"),
    }


def namespace_for(share_id, scope_key):
    try:
        valid_share = isinstance(share_id, str) and str(UUID(share_id)) == share_id
    except ValueError:
        valid_share = False
    if not valid_share or not isinstance(scope_key, str) or not _SCOPE.fullmatch(scope_key):
        raise CloudEnvError("invalid Ceph namespace scope; HOLD")
    return "pal-" + hashlib.sha256(share_id.encode()).hexdigest()[:24] + "-" + scope_key


def consumer_caps(binding):
    """Do not replace the OSD namespace cap with a broad CephFS data tag."""
    fs_name, pool, namespace = (binding.get(key) for key in ("fs_name", "data_pool", "namespace"))
    if (
        not isinstance(fs_name, str)
        or not _TOKEN.fullmatch(fs_name)
        or not isinstance(pool, str)
        or not _POOL.fullmatch(pool)
    ):
        raise CloudEnvError("invalid Ceph filesystem or data pool; HOLD")
    if namespace != namespace_for(binding.get("share_id"), binding.get("scope_key")):
        raise CloudEnvError("Ceph namespace does not bind the share and scope; HOLD")
    try:
        _, path = _ceph_source_identity(binding["export"])
        _, base = _ceph_source_identity(binding["base_export"])
        if _ceph_source_identity(binding["export"])[0] != _ceph_source_identity(binding["base_export"])[0]:
            raise ValueError
        if path != base.rstrip("/") + "/cas/" + binding["scope_key"] or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise CloudEnvError("Ceph export does not bind the exact published scope; HOLD") from None
    return {
        "mon": f"allow r fsname={fs_name}",
        "mds": f"allow r fsname={fs_name} path={path}",
        "osd": f"allow r pool={pool} namespace={namespace}",
    }


def _open_private(path, limit):
    """Walk without symlinks; reject foreign or group/world writable ancestors."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise CloudEnvError("Ceph issuer files must be absolute private paths; HOLD")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if info.st_uid not in (0, os.geteuid()) or (
                info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            ):
                raise CloudEnvError("Ceph issuer directory is not privately controlled; HOLD")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > limit
            ):
                raise CloudEnvError("Ceph issuer file is not owned, private and bounded; HOLD")
            data = os.read(fd, limit + 1)
            after = os.fstat(fd)

            def stable(item):
                return (
                    item.st_dev,
                    item.st_ino,
                    item.st_mode,
                    item.st_uid,
                    item.st_nlink,
                    item.st_size,
                    item.st_mtime_ns,
                    item.st_ctime_ns,
                )

            if len(data) > limit or len(data) != info.st_size or stable(after) != stable(info):
                raise CloudEnvError("Ceph issuer file changed; HOLD")
            return fd, data
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(directory)


def _terminate_and_reap(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=_TERM_SECONDS)
        return True
    except subprocess.TimeoutExpired:
        # Never SIGKILL an uncertain auth mutation. Keep a reaper until it exits;
        # the durable intent remains HOLD, and no later call replays the mutation.
        threading.Thread(target=process.wait, daemon=True, name="ceph-auth-reaper").start()
        return False


def _command(argv, *, pass_fds=(), before_spawn=None):
    """Bound both streams while reading; errors never include CLI output/keys."""
    if before_spawn is not None:
        try:
            before_spawn()
        except Exception:
            raise CephCommandError(
                "Ceph mutation boundary rejected before spawn; HOLD", command_pending=False, started=False
            ) from None
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
            start_new_session=True,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except OSError:
        raise CephCommandError(
            "Ceph issuer command could not start; HOLD", command_pending=False, started=False
        ) from None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + _COMMAND_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CloudEnvError("Ceph issuer command timed out; HOLD")
                for key, _ in selector.select(min(remaining, 0.25)):
                    data = os.read(key.fd, 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    target = buffers[key.data]
                    target.extend(data)
                    if len(target) > (_OUTPUT_LIMIT if key.data == "stdout" else _ERROR_LIMIT):
                        raise CloudEnvError("Ceph issuer output exceeds its private bound; HOLD")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloudEnvError("Ceph issuer command timed out; HOLD")
            code = process.wait(timeout=remaining)
        return code, bytes(buffers["stdout"]), bytes(buffers["stderr"])
    except (subprocess.TimeoutExpired, OSError, CloudEnvError):
        reaped = _terminate_and_reap(process)
        raise CephCommandError(
            "Ceph issuer outcome is unavailable; HOLD, no mutation replay", command_pending=not reaped
        ) from None
    finally:
        process.stdout.close()
        process.stderr.close()


class CephPathAccessManager:
    def __init__(self, env, configuration, published_binding):
        self.env, self.configuration, self.binding = env, dict(configuration), dict(published_binding)
        self.preflight()

    def preflight(self):
        """Local issuer/config qualification only; no filesystem/provider mutation."""
        required = ("fs_name", "data_pool", "issuer_client", "conf_file", "keyring_file")
        if not all(isinstance(self.configuration.get(key), str) and self.configuration[key] for key in required):
            raise CloudEnvError("dedicated Ceph issuer configuration is incomplete; HOLD")
        if self.binding.get("kind") != "manila-namespace-v1" or self.binding.get("root") != "blobs/sha256":
            raise CloudEnvError("native SDK requires a namespace-qualified publication; HOLD")
        if any(self.configuration[key] != self.binding.get(key) for key in ("fs_name", "data_pool")):
            raise CloudEnvError("Ceph issuer filesystem or pool differs from publication; HOLD")
        issuer = self.configuration["issuer_client"]
        defaults = {
            "client.admin",
            "client.guest",
            "client.manila",
            "client.cinder",
            "client.nova",
            "client.glance",
            "client.radosgw",
        }
        if (
            not re.fullmatch(r"client\.[a-zA-Z0-9_.-]{1,64}", issuer)
            or issuer in defaults
            or issuer.startswith("client.bootstrap-")
        ):
            raise CloudEnvError("a dedicated non-admin, non-service-default Ceph issuer is required; HOLD")
        publication_root = self.configuration.get("publication_root")
        if publication_root is not None:
            root = Path(publication_root)
            if not root.is_absolute() or ".." in root.parts:
                raise CloudEnvError("Ceph publication root is not absolute; HOLD")
            if any(
                path == root or root in path.parents
                for path in (Path(self.configuration["conf_file"]), Path(self.configuration["keyring_file"]))
            ):
                raise CloudEnvError("Ceph issuer private files must be outside the publication root; HOLD")
        consumer_caps(self.binding)
        fds = []
        try:
            for key in ("conf_file", "keyring_file"):
                fd, data = _open_private(self.configuration[key], _OUTPUT_LIMIT)
                fds.append(fd)
                if not data.strip():
                    raise CloudEnvError("Ceph issuer file is empty; HOLD")
                if key == "keyring_file":
                    text = data.decode("ascii")
                    sections = re.findall(r"^\s*\[([^\]]+)\]\s*$", text, re.MULTILINE)
                    if sections != [self.configuration["issuer_client"]] or not re.search(
                        r"^\s*key\s*=\s*[A-Za-z0-9+/]+=*\s*$", text, re.MULTILINE
                    ):
                        raise CloudEnvError("Ceph issuer keyring identity differs; HOLD")
                else:
                    # Prevent config-driven fallback/plugin execution and key includes.
                    text = data.decode("ascii")
                    forbidden = r"^\s*(?:!include\b|include\b|key\s*=|keyfile\s*=|keyring\s*=|plugin\s*=|log_to_file\s*=|log_file\s*=|admin_socket\s*=)"
                    if re.search(forbidden, text, re.MULTILINE | re.IGNORECASE):
                        raise CloudEnvError(
                            "Ceph issuer configuration contains implicit credentials or output paths; HOLD"
                        )
        except (OSError, UnicodeError):
            raise CloudEnvError("Ceph issuer private files cannot be qualified; HOLD") from None
        finally:
            for fd in fds:
                os.close(fd)

    def _run(self, *args):
        try:
            self.preflight()
        except (CloudEnvError, OSError):
            raise CephCommandError(
                "Ceph issuer configuration is unavailable before spawn; HOLD", command_pending=False, started=False
            ) from None
        fds = []
        try:
            try:
                for key in ("conf_file", "keyring_file"):
                    fd, _ = _open_private(self.configuration[key], _OUTPUT_LIMIT)
                    fds.append(fd)
            except (CloudEnvError, OSError):
                raise CephCommandError(
                    "Ceph issuer private files are unavailable before spawn; HOLD", command_pending=False, started=False
                ) from None
            argv = [
                "/usr/bin/ceph",
                "--conf",
                f"/proc/self/fd/{fds[0]}",
                "--name",
                self.configuration["issuer_client"],
                "--keyring",
                f"/proc/self/fd/{fds[1]}",
                "--connect-timeout",
                "15",
                "--auth-client-required=cephx",
                "--log-to-file=false",
                "--log-to-stderr=false",
                "--log-to-syslog=false",
                "--log-to-journald=false",
                "--err-to-stderr=true",
                "--admin-socket=",
                "--format",
                "json",
                *args,
            ]
            before_spawn = None
            if args[:2] in (("auth", "add"), ("auth", "del")):
                prefix = "client.pal-" + self.env.state["marker"] + "-"
                role = args[2].removeprefix(prefix)
                if args[2] != self._entity(role):
                    raise CephCommandError(
                        "Ceph mutation identity differs before spawn; HOLD", command_pending=False, started=False
                    )

                def before_spawn():
                    callback = getattr(self.env, "native_ceph_mutation_boundary", None)
                    if not callable(callback):
                        raise CloudEnvError("Ceph mutation requires the original grant boundary; HOLD")
                    callback(args[1], role)

            return _command(argv, pass_fds=tuple(fds), before_spawn=before_spawn)
        finally:
            for fd in fds:
                os.close(fd)

    def _entity(self, role):
        marker = self.env.state.get("marker")
        if (
            not isinstance(marker, str)
            or not re.fullmatch(r"[a-f0-9]{32}", marker)
            or not isinstance(role, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", role)
        ):
            raise CloudEnvError("Ceph consumer needs an exact environment and bounded run role; HOLD")
        return "client.pal-" + marker + "-" + role

    def _fingerprint(self):
        return hashlib.sha256(json.dumps(self.binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _get(self, entity):
        code, out, error = self._run("auth", "get", entity)
        if (
            code == 2
            and not out.strip()
            and error.strip() == f"Error ENOENT: failed to find {entity} in keyring".encode()
        ):
            return None
        if code != 0:
            if _access_denied(code, out, error):
                raise CloudEnvError(
                    f"Ceph auth get authorization denied (errno={code}); absence is not confirmed; HOLD"
                )
            raise CloudEnvError("fresh Ceph auth lookup failed; absence is not confirmed; HOLD")
        try:
            values = json.loads(out)
            if (
                not isinstance(values, list)
                or len(values) != 1
                or values[0].get("entity") != entity
                or values[0].get("caps") != consumer_caps(self.binding)
            ):
                raise ValueError
            key = values[0].get("key")
            if not isinstance(key, str) or not CEPHX_KEY_RE.fullmatch(key):
                raise ValueError
            return key
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise CloudEnvError("fresh Ceph auth identity, caps or key differs; HOLD") from None

    def _fresh_key(self, record):
        key = self._get(record["id"])
        digest = record.get("key_digest")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise CloudEnvError("recorded Ceph credential fingerprint is invalid; HOLD")
        if digest is None and record.get("status") != "creating":
            raise CloudEnvError("recorded Ceph credential has no original fingerprint; HOLD")
        if key is not None and digest is not None and hashlib.sha256(key.encode()).hexdigest() != digest:
            raise CloudEnvError("fresh Ceph credential differs from its original fingerprint; HOLD")
        return key

    def _check_record(self, value, role):
        if (
            value.get("provider") != "ceph-namespace-v1"
            or value.get("type") != "ceph-path"
            or value.get("id") != self._entity(role)
            or value.get("run_role") != role
            or value.get("marker") != self.env.state["marker"]
            or value.get("project_id") != self.env.state["project_id"]
            or value.get("access_to") != self._entity(role)[7:]
            or value.get("level") != "ro"
            or value.get("binding") != self.binding
            or value.get("binding_digest") != self._fingerprint()
            or value.get("caps") != consumer_caps(self.binding)
        ):
            raise CloudEnvError("recorded Ceph consumer differs from the sealed publication; HOLD")
        if value.get("command_pending") is not False:
            raise CloudEnvError("Ceph mutation process custody is uncertain; HOLD, no mutation replay or settlement")

    def _mutate(self, role, record, *args):
        # Persist custody BEFORE spawn. A parent crash cannot make an in-flight
        # command look safely settled merely because one auth GET sees absence.
        if record.get("command_pending") is not True:
            record = self.env.record("access_rules", role, {**record, "command_pending": True})
        try:
            result = self._run(*args)
        except CephCommandError as error:
            self.env.record(
                "access_rules", role, {**record, "command_pending": error.command_pending, "no_call": not error.started}
            )
            raise
        # Other exceptions leave the durable custody fence in place.
        record = self.env.record("access_rules", role, {**record, "command_pending": False, "no_call": False})
        return result, record

    def _record(self, role):
        value = self.env.resource("access_rules", role)
        if value is not None:
            self._check_record(value, role)
        return value

    def issue(self, role):
        self.preflight()
        entity = self._entity(role)
        record = self._record(role)
        if record is None:
            if self._get(entity) is not None:
                raise CloudEnvError("unrecorded Ceph consumer already exists; never adopt; HOLD")
            record = self.env._intent(
                "access_rules",
                role,
                id=entity,
                provider="ceph-namespace-v1",
                type="ceph-path",
                run_role=role,
                marker=self.env.state["marker"],
                access_to=entity[7:],
                level="ro",
                binding=dict(self.binding),
                binding_digest=self._fingerprint(),
                caps=consumer_caps(self.binding),
                command_pending=True,
            )
            args = ["auth", "add", entity]
            for service, cap in record["caps"].items():
                args.extend((service, cap))
            (code, output, error), record = self._mutate(role, record, *args)
            if code != 0:
                if _access_denied(code, output, error):
                    raise CloudEnvError(f"Ceph auth add authorization denied (errno={code}); HOLD, never replay")
                raise CloudEnvError("Ceph auth creation outcome is uncertain; HOLD, never replay")
        if record.get("status") not in ("creating", "confirmed"):
            raise CloudEnvError("Ceph consumer is deleting or unknown; HOLD")
        key = self._fresh_key(record)
        if key is None:
            raise CloudEnvError("Ceph creation is unconfirmed; HOLD, never replay")
        return self.env.record(
            "access_rules",
            role,
            {**record, "status": "confirmed", "key_digest": hashlib.sha256(key.encode()).hexdigest()},
        )

    def access(self, role):
        record = self._record(role)
        if record is None or record.get("status") != "confirmed":
            raise CloudEnvError("Ceph consumer is not freshly issuable; HOLD")
        key = self._fresh_key(record)
        if key is None:
            raise CloudEnvError("recorded Ceph consumer disappeared; HOLD")
        # Secret remains private in memory until the sealed guest bootstrap path.
        return {
            "export": self.binding["export"],
            "access_to": record["access_to"],
            "access_key": key,
            "fs_name": self.binding["fs_name"],
        }

    def revoke(self, role):
        self.preflight()
        record = self._record(role)
        if record is None:
            return
        if record.get("status") not in ("creating", "confirmed", "deleting"):
            raise CloudEnvError("Ceph consumer lifecycle is unknown; HOLD")
        current = self._fresh_key(record)
        if current is None:
            if record["status"] == "creating" and record.get("no_call") is not True:
                raise CloudEnvError("Ceph creation absence does not settle an unconfirmed creation; HOLD")
            self.env.forget("access_rules", role)
            return
        if record["status"] == "deleting":
            raise CloudEnvError("Ceph deletion remains unconfirmed; HOLD, never replay")
        record = self.env.record(
            "access_rules",
            role,
            {
                **record,
                "status": "deleting",
                "command_pending": True,
                "key_digest": hashlib.sha256(current.encode()).hexdigest(),
            },
        )
        (code, output, error), record = self._mutate(role, record, "auth", "del", record["id"])
        if code != 0:
            if _access_denied(code, output, error):
                raise CloudEnvError(f"Ceph auth del authorization denied (errno={code}); HOLD, never replay")
            raise CloudEnvError("Ceph deletion outcome is uncertain; HOLD, never replay")
        if self._get(record["id"]) is not None:
            raise CloudEnvError("Ceph consumer is still present; HOLD, never replay")
        self.env.forget("access_rules", role)

    def check_absent(self, record):
        """Freshly recheck a historical consumer, including a prior image scope."""
        role, binding = record.get("run_role"), record.get("binding")
        if not isinstance(binding, dict):
            raise CloudEnvError("historical Ceph consumer has no sealed binding; HOLD")
        historical = CephPathAccessManager(self.env, self.configuration, binding)
        historical._check_record(record, role)
        if historical._fresh_key(record) is not None:
            raise CloudEnvError("historical Ceph consumer is still present; HOLD")
