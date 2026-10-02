"""Reference proofs for the CAS shared by native and legacy artifact transports."""
from sqlalchemy import select

from palimpsest_hub.models import (
    PackageBlobReference,
    PackageCache,
    PackageVersion,
    PalimpsestHubLayer,
    PalimpsestImageExport,
)


async def package_blob_referenced(session, digest: str) -> bool:
    for column in (PackageBlobReference.blob_digest, PackageVersion.archive_digest, PackageCache.archive_digest):
        if await session.scalar(select(column).where(column == digest).limit(1)) is not None:
            return True
    return False


async def blob_referenced(session, digest: str) -> bool:
    if await package_blob_referenced(session, digest):
        return True
    for column in (PalimpsestHubLayer.blob_digest, PalimpsestImageExport.result_blob_digest):
        if await session.scalar(select(column).where(column == digest).limit(1)) is not None:
            return True
    return False
