"""Reference proofs for the CAS shared by native and legacy artifact transports."""

from sqlalchemy import select

from palimpsest_hub.models import (
    PackageBlobReference,
    PackageCache,
    PackageVersion,
    PalimpsestHubLayer,
    PalimpsestImageExport,
    RuntimeBlobReference,
)


async def package_blob_referenced(session, digest: str) -> bool:
    for column in (PackageBlobReference.blob_digest, PackageVersion.archive_digest, PackageCache.archive_digest):
        if await session.scalar(select(column).where(column == digest).limit(1)) is not None:
            return True
    return False


async def runtime_blob_referenced(session, digest: str) -> bool:
    return (
        await session.scalar(select(RuntimeBlobReference.digest).where(RuntimeBlobReference.digest == digest).limit(1))
        is not None
    )


async def retain_blob(session, digest: str, resource_kind: str, resource_id: str) -> None:
    """Caller holds this blob's lock through the reference and owning-row commit."""
    key = (resource_kind, resource_id, digest)
    if await session.get(RuntimeBlobReference, key) is None:
        session.add(RuntimeBlobReference(resource_kind=resource_kind, resource_id=resource_id, digest=digest))


async def blob_referenced(session, digest: str) -> bool:
    if await package_blob_referenced(session, digest):
        return True
    if await runtime_blob_referenced(session, digest):
        return True
    for column in (PalimpsestHubLayer.blob_digest, PalimpsestImageExport.result_blob_digest):
        if await session.scalar(select(column).where(column == digest).limit(1)) is not None:
            return True
    return False
