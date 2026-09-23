"""Palimpsest 허브 blob 저장소.

허브는 백엔드가 blob 바이트를 **직접 스트리밍 read/write** 해야 업로드·다운로드가 성립한다.
소비 VM 이 마운트하는 Manila share 와는 다른 관심사이므로 별도 저장소를 둔다.

배치는 OCI image-layout 그대로다 — `<root>/blobs/sha256/<hex>`. 덕분에 번들 export 가
디렉터리를 그대로 tar 로 말면 되고, 로컬 KVM 호스트에서 번들을 펼치면 곧바로 레이어 경로가 된다.

드라이버는 `local_path` 하나다. Swift/S3 드라이버는 후속 — 기존 `services/s3.py` 는
**사용자 토큰 스코프**(`get_user_s3_client(token, user_id, project_id)`)로 설계돼 있어
서비스 자신이 소유하는 blob 에는 그대로 쓸 수 없고, 서비스 계정 자격증명 경로를 새로 만들어야 한다.
그 작업은 허브의 본질(콘텐츠 주소 저장·번들)과 무관하므로 분리했다.

**경로 안전**: digest / 세션 ID 는 경로 조립에 쓰이므로 정규식으로 강제 검증한다.
`normalize_digest` 를 통과한 값만 받지만, 경로를 만드는 지점에서 한 번 더 확인한다(이중 방어).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from palimpsest_hub.config import BuildWorkerSettings, Settings, get_settings
from palimpsest_hub.services.digest import normalize_digest

_logger = logging.getLogger(__name__)

# squashfs 레이어 blob 의 mediaType. OCI image manifest 의 layers[] 에 실린다.
MEDIA_TYPE_LAYER_SQUASHFS = "application/vnd.afterglow.palimpsest.layer.squashfs.v1"
MEDIA_TYPE_LAYER_CONFIG = "application/vnd.afterglow.palimpsest.layer.config.v1+json"
MEDIA_TYPE_BUILDKIT_CACHE = "application/vnd.afterglow.palimpsest.buildkit.cache.v1.tar"
MEDIA_TYPE_IMAGE_RAW = "application/vnd.afterglow.palimpsest.image.raw.v1"
MEDIA_TYPE_IMAGE_QCOW2 = "application/vnd.afterglow.palimpsest.image.qcow2.v1"
MEDIA_TYPE_IMAGE_VMDK = "application/vnd.afterglow.palimpsest.image.vmdk.v1"
MEDIA_TYPE_IMAGE_VDI = "application/vnd.afterglow.palimpsest.image.vdi.v1"
MEDIA_TYPE_IMAGE_VHD = "application/vnd.afterglow.palimpsest.image.vhd.v1"
MEDIA_TYPE_IMAGE_VHDX = "application/vnd.afterglow.palimpsest.image.vhdx.v1"


@dataclass(frozen=True)
class ImageFormatSpec:
    """Public export format mapped to qemu-img and artifact metadata."""

    media_type: str
    qemu_driver: str
    extension: str


# kind='cloud-image' 는 레이어와 같은 blob store·업로드 세션을 쓰되 parent/chain 이 없다.
KIND_BUILDKIT_CACHE = "buildkit-cache"
KIND_CLOUD_IMAGE = "cloud-image"
IMAGE_FORMAT_SPECS = {
    "raw": ImageFormatSpec(MEDIA_TYPE_IMAGE_RAW, "raw", "raw"),
    "qcow2": ImageFormatSpec(MEDIA_TYPE_IMAGE_QCOW2, "qcow2", "qcow2"),
    "vmdk": ImageFormatSpec(MEDIA_TYPE_IMAGE_VMDK, "vmdk", "vmdk"),
    "vdi": ImageFormatSpec(MEDIA_TYPE_IMAGE_VDI, "vdi", "vdi"),
    "vhd": ImageFormatSpec(MEDIA_TYPE_IMAGE_VHD, "vpc", "vhd"),
    "vhdx": ImageFormatSpec(MEDIA_TYPE_IMAGE_VHDX, "vhdx", "vhdx"),
}
DISK_FORMAT_MEDIA_TYPES = {name: spec.media_type for name, spec in IMAGE_FORMAT_SPECS.items()}

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_READ_CHUNK = 1024 * 1024


class HubStoreError(RuntimeError):
    """허브 저장소 오류."""


class HubStoreUnavailable(HubStoreError):
    """`[palimpsest] hub_local_path` 가 설정되지 않아 허브를 쓸 수 없다."""


class HubDigestMismatch(HubStoreError):
    """수신한 바이트의 digest 가 선언된 digest 와 다르다 (fail-closed)."""


class HubStoreLimit(HubStoreError):
    """A local Hub file exceeded an explicit resource ceiling."""


@dataclass(frozen=True)
class FinalizedBlob:
    blob_digest: str
    blob_md5: str
    size_bytes: int


def _digest_hex(digest: str) -> str:
    """digest 에서 경로에 쓸 hex 를 뽑는다. 형식이 어긋나면 거부(경로 traversal 방어)."""
    normalized = normalize_digest(digest)
    if normalized is None:
        raise HubStoreError(f"digest 형식이 유효하지 않습니다: {digest!r}")
    hex_part = normalized[len("sha256:") :]
    if not _HEX64_RE.match(hex_part):  # normalize 를 통과해도 한 번 더 확인
        raise HubStoreError("digest hex 검증 실패")
    return hex_part


class LocalPathBlobStore:
    """로컬 파일시스템(PVC/볼륨) 기반 blob store."""

    def __init__(self, root: Path):
        self.root = root

    def _sync_blob_dir(self) -> None:
        # Sync newly created ancestor entries too; syncing sha256 alone is insufficient.
        for directory in (self.blobs_dir, self.blobs_dir.parent, self.root):
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _sync_existing_blob(self, target: Path) -> None:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise HubStoreError("blob 대상 경로가 일반 파일이 아닙니다")
            os.fsync(fd)
        finally:
            os.close(fd)
        self._sync_blob_dir()

    def _copy_into_blob(self, source: Path, target: Path) -> None:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".upload-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            try:
                with source.open("rb") as input_stream:
                    shutil.copyfileobj(input_stream, temporary, length=_READ_CHUNK)
                temporary.flush()
                os.fsync(temporary.fileno())
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary_path, target)
            self._sync_blob_dir()
        finally:
            temporary_path.unlink(missing_ok=True)

    # ── 경로 ────────────────────────────────────────────────────────────
    @property
    def blobs_dir(self) -> Path:
        return self.root / "blobs" / "sha256"

    @property
    def uploads_dir(self) -> Path:
        return self.root / "uploads"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    def _acquire_lock_file(self, name: str, *, blocking: bool) -> int | None:
        """Open one lock file and take it exclusively; ``None`` means another owner holds it."""
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.locks_dir / name, flags, 0o600)
        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, operation)
        except BlockingIOError:
            os.close(fd)
            return None
        except Exception:
            os.close(fd)
            raise
        return fd

    def acquire_blob_lock(self, digest: str, *, blocking: bool = True) -> int | None:
        """Acquire a cross-process exclusive lock for one validated blob digest."""
        return self._acquire_lock_file(f"{_digest_hex(digest)}.lock", blocking=blocking)

    @staticmethod
    def release_blob_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def blob_path(self, digest: str) -> Path:
        return self.blobs_dir / _digest_hex(digest)

    def upload_path(self, session_id: str) -> Path:
        if not _SESSION_ID_RE.match(session_id):
            raise HubStoreError("업로드 세션 ID 형식이 유효하지 않습니다")
        return self.uploads_dir / session_id

    def acquire_upload_lock(self, session_id: str, *, blocking: bool = True) -> int | None:
        """Serialize offset checks, file writes and DB commits per upload session."""
        self.upload_path(session_id)  # Validate before constructing a lock path.
        return self._acquire_lock_file(f"upload-{session_id}.lock", blocking=blocking)

    def acquire_project_upload_lock(self, project_id: str, *, blocking: bool = True) -> int | None:
        """Serialize creation against the per-project active-session cap."""
        return self._acquire_project_lock(project_id, "upload", blocking=blocking)

    def acquire_project_build_lock(self, project_id: str, *, blocking: bool = True) -> int | None:
        """Serialize count-and-insert against the per-project build cap."""
        return self._acquire_project_lock(project_id, "build", blocking=blocking)

    def _acquire_project_lock(self, project_id: str, kind: str, *, blocking: bool) -> int | None:
        name = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        return self._acquire_lock_file(f"project-{kind}-{name}.lock", blocking=blocking)

    def reconcile_upload(self, session_id: str, expected_size: int) -> None:
        """Discard unacknowledged bytes left by an interrupted PATCH."""
        path = self.upload_path(session_id)
        if path.is_symlink() or not path.is_file() or expected_size < 0:
            raise HubStoreError("업로드 세션을 찾을 수 없습니다")
        with path.open("r+b") as handle:
            actual = os.fstat(handle.fileno()).st_size
            if actual < expected_size:
                raise HubStoreError("업로드 파일이 기록된 offset보다 짧습니다")
            if actual > expected_size:
                handle.truncate(expected_size)
                handle.flush()
                os.fsync(handle.fileno())

    def sync_upload(self, session_id: str) -> None:
        path = self.upload_path(session_id)
        if path.is_symlink() or not path.is_file():
            raise HubStoreError("업로드 세션을 찾을 수 없습니다")
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    # ── 조회 ────────────────────────────────────────────────────────────
    def exists(self, digest: str) -> bool:
        path = self.blob_path(digest)
        return path.is_file() and not path.is_symlink()

    def size(self, digest: str) -> int:
        path = self.blob_path(digest)
        if not path.is_file() or path.is_symlink():
            raise HubStoreError(f"blob 이 없습니다: {digest}")
        return path.stat().st_size

    def open_read(self, digest: str) -> BinaryIO:
        path = self.blob_path(digest)
        if not path.is_file() or path.is_symlink():
            raise HubStoreError(f"blob 이 없습니다: {digest}")
        return path.open("rb")

    def iter_blob(self, digest: str, *, start: int = 0, length: int | None = None) -> Iterator[bytes]:
        """blob 을 청크로 읽는다. HTTP Range 응답에 그대로 쓴다."""
        with self.open_read(digest) as handle:
            if start:
                handle.seek(start)
            remaining = length
            while True:
                want = _READ_CHUNK if remaining is None else min(_READ_CHUNK, remaining)
                if want <= 0:
                    return
                chunk = handle.read(want)
                if not chunk:
                    return
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk

    # ── 업로드 세션 ─────────────────────────────────────────────────────
    def start_upload(self, session_id: str) -> None:
        path = self.upload_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass

    def append_upload(self, session_id: str, chunk: bytes) -> int:
        path = self.upload_path(session_id)
        if not path.is_file() or path.is_symlink():
            raise HubStoreError("업로드 세션을 찾을 수 없습니다")
        with path.open("ab") as handle:
            handle.write(chunk)
        return path.stat().st_size

    def inspect_file(self, source: Path, *, max_bytes: int) -> FinalizedBlob:
        """Hash a stable bounded regular file without publishing it to CAS."""
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if source.is_symlink() or not source.is_file():
            raise HubStoreError("검사할 파일은 일반 파일이어야 합니다")
        initial_size = source.stat().st_size
        if initial_size < 0 or initial_size > max_bytes:
            raise HubStoreLimit("파일이 허용 크기를 초과합니다")

        sha = hashlib.sha256()
        md5 = hashlib.md5()  # noqa: S324 — 보조 검색 키. 무결성 권위는 sha256
        size = 0
        with source.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise HubStoreLimit("파일이 허용 크기를 초과합니다")
                sha.update(chunk)
                md5.update(chunk)
            os.fsync(handle.fileno())
        if size != initial_size or source.stat().st_size != initial_size:
            raise HubStoreError("검사 중 파일 크기가 변경되었습니다")
        return FinalizedBlob(blob_digest=f"sha256:{sha.hexdigest()}", blob_md5=md5.hexdigest(), size_bytes=size)

    def publish_verified(self, source: Path, finalized: FinalizedBlob) -> bool:
        """Publish a previously inspected source while the caller owns its digest lock.

        Returns whether this call created/repaired the CAS target.  The caller must
        retain both the digest lock and source staging through its SQL commit.
        """
        target = self.blob_path(finalized.blob_digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        needs_copy = True
        if target.exists() or target.is_symlink():
            try:
                existing = self.inspect_file(target, max_bytes=max(finalized.size_bytes, 1))
            except HubStoreError:
                existing = None
            if existing is not None and existing == finalized:
                self._sync_existing_blob(target)
                os.utime(target, None, follow_symlinks=False)
                needs_copy = False
        if needs_copy:
            try:
                self._copy_into_blob(source, target)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return True
        return False

    def finalize_upload(self, session_id: str, declared_digest: str | None) -> FinalizedBlob:
        """Legacy standalone finalization; API publication additionally holds SQL lock coverage."""
        path = self.upload_path(session_id)
        finalized = self.inspect_file(path, max_bytes=2**63 - 1)
        if declared_digest is not None:
            expected = normalize_digest(declared_digest)
            if expected is None or not hmac.compare_digest(expected, finalized.blob_digest):
                path.unlink(missing_ok=True)
                raise HubDigestMismatch(f"digest 불일치 — 선언={declared_digest!r} 실제={finalized.blob_digest}")
        lock_fd = self.acquire_blob_lock(finalized.blob_digest)
        try:
            self.publish_verified(path, finalized)
        finally:
            self.release_blob_lock(lock_fd)
        return finalized

    def finalize_upload_inspection(
        self, session_id: str, declared_digest: str | None, *, max_bytes: int
    ) -> FinalizedBlob:
        """Verify staged upload bytes, retaining valid staging for retryable registration."""
        path = self.upload_path(session_id)
        finalized = self.inspect_file(path, max_bytes=max_bytes)
        if declared_digest is not None:
            expected = normalize_digest(declared_digest)
            if expected is None or not hmac.compare_digest(expected, finalized.blob_digest):
                path.unlink(missing_ok=True)
                raise HubDigestMismatch(f"digest 불일치 — 선언={declared_digest!r} 실제={finalized.blob_digest}")
        return finalized

    def abort_upload(self, session_id: str) -> None:
        self.upload_path(session_id).unlink(missing_ok=True)

    def promote_file(self, source: Path, *, max_bytes: int) -> FinalizedBlob:
        """Atomically promote a bounded regular scratch file into the blob store."""
        root = self.root.resolve()
        resolved = source.resolve()
        if source.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
            raise HubStoreError("승격할 파일은 허브 루트 안의 일반 파일이어야 합니다")

        initial_size = resolved.stat().st_size
        if initial_size > max_bytes:
            raise HubStoreError("승격할 파일이 허용 크기를 초과합니다")

        sha = hashlib.sha256()
        md5 = hashlib.md5()  # noqa: S324 — 보조 검색 키. 무결성 권위는 sha256
        size = 0
        with resolved.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise HubStoreError("승격할 파일이 허용 크기를 초과합니다")
                sha.update(chunk)
                md5.update(chunk)
            os.fsync(handle.fileno())

        if size != initial_size or resolved.stat().st_size != initial_size:
            raise HubStoreError("승격 중 파일 크기가 변경되었습니다")

        actual = f"sha256:{sha.hexdigest()}"
        target = self.blob_path(actual)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = self.acquire_blob_lock(actual)
        try:
            if target.exists() or target.is_symlink():
                self._sync_existing_blob(target)
                os.utime(target, None, follow_symlinks=False)
                resolved.unlink(missing_ok=True)
            else:
                os.replace(resolved, target)
                self._sync_blob_dir()
        finally:
            self.release_blob_lock(lock_fd)
        return FinalizedBlob(blob_digest=actual, blob_md5=md5.hexdigest(), size_bytes=size)

    def delete(self, digest: str) -> None:
        self.blob_path(digest).unlink(missing_ok=True)

    def prune_uploads(self, older_than_seconds: int, now_epoch: float) -> int:
        """방치된 업로드 임시 파일을 정리한다. 반환값은 삭제한 개수."""
        if not self.uploads_dir.is_dir():
            return 0
        removed = 0
        for entry in self.uploads_dir.iterdir():
            if not entry.is_file():
                continue
            if now_epoch - entry.stat().st_mtime > older_than_seconds:
                entry.unlink(missing_ok=True)
                removed += 1
        return removed


def get_blob_store(settings: Settings | BuildWorkerSettings | None = None) -> LocalPathBlobStore:
    """설정된 허브 blob store 를 돌려준다. 미설정이면 `HubStoreUnavailable`.

    허브는 선택 기능이다 — 설정하지 않은 배포에서는 엔드포인트가 503 을 준다.
    """
    settings = settings if settings is not None else get_settings()
    root = (settings.palimpsest_hub_local_path or "").strip()
    if not root:
        raise HubStoreUnavailable("[palimpsest] hub_local_path 가 설정되지 않았습니다 — 허브 기능이 비활성입니다")
    return LocalPathBlobStore(Path(root))


_LOCK_POLL_INITIAL_SECONDS = 0.002
_LOCK_POLL_MAX_SECONDS = 0.05


async def acquire_lock_by_polling(attempt: Callable[[], int | None]) -> int:
    """Acquire a flock without ever parking a worker thread on the wait.

    A blocking ``flock`` holds its thread for the whole wait, so enough waiters
    fill the pool and the current owner's own work — or its unlock — can never be
    scheduled.  Each attempt here returns immediately, so waiting costs only an
    event-loop sleep.  Ordering is therefore not FIFO; the per-project session and
    build caps are what bound contention on any one lock.
    """
    delay = _LOCK_POLL_INITIAL_SECONDS
    while True:
        task = asyncio.create_task(asyncio.to_thread(attempt))
        try:
            fd = await asyncio.shield(task)
        except asyncio.CancelledError:
            # The attempt cannot be cancelled; release whatever it still wins.
            def release_when_ready(done: asyncio.Task[int | None]) -> None:
                try:
                    acquired = done.result()
                except Exception:
                    return
                if acquired is not None:
                    LocalPathBlobStore.release_blob_lock(acquired)

            task.add_done_callback(release_when_ready)
            raise
        if fd is not None:
            return fd
        await asyncio.sleep(delay)
        delay = min(delay * 2, _LOCK_POLL_MAX_SECONDS)


async def write_upload_stream(
    store: LocalPathBlobStore,
    session_id: str,
    chunks: AsyncIterator[bytes],
    *,
    already_received: int = 0,
    max_bytes: int,
) -> int:
    """업로드 스트림을 이어붙인다. 상한 초과 시 세션을 폐기하고 거부한다.

    파일 IO 는 이벤트 루프를 막지 않도록 스레드로 넘긴다.
    """
    total = already_received
    async for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            await asyncio.to_thread(store.abort_upload, session_id)
            raise HubStoreError(f"blob 크기 상한을 초과했습니다 (>{max_bytes} bytes)")
        await asyncio.to_thread(store.append_upload, session_id, chunk)
    return total
