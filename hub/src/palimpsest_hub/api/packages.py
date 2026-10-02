"""Native /v1 project packages; never a Docker Distribution or legacy-token write alias."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError

from palimpsest_hub.api.hub import _blob_response
from palimpsest_hub.auth import get_package_member_info, require_token
from palimpsest_hub.config import validate_keystone_id
from palimpsest_hub.package_dto import (
    CachePartition,
    CacheUploadStart,
    EmptyBody,
    KeyCreate,
    PackageUploadStart,
    canonical_tag,
)
from palimpsest_hub.services import package_registry as registry

router = APIRouter(tags=["project-packages"])


def authentication_headers(request):
    authorization = request.headers.get("authorization")
    token = request.headers.get("x-auth-token")
    if authorization is not None and token is not None:
        raise registry.RegistryError(401, "AUTH_REQUIRED", "choose original member token or package key, not both")
    return authorization, token


async def control_member(request: Request, member: dict = Depends(get_package_member_info)):
    authorization, token = authentication_headers(request)
    if authorization is not None:
        raise registry.RegistryError(401, "AUTH_REQUIRED", "control operation requires original member token")
    registry.require_policy(member)
    return member


async def key_actor(request: Request):
    authorization, token = authentication_headers(request)
    if not authorization or not authorization.startswith("Bearer "):
        raise registry.RegistryError(401, "AUTH_REQUIRED", "package key bearer credential required")
    actor = await registry.authenticate_key(authorization[7:])
    assertion = request.headers.get("x-project-id")
    if assertion is not None and assertion != actor.project_id:
        raise registry.RegistryError(403, "PROJECT_SCOPE_MISMATCH", "project assertion does not match package key")
    return actor


async def read_actor(request: Request, namespace: str):
    authorization, token = authentication_headers(request)
    if authorization is not None:
        return await key_actor(request)
    if token is None:
        raise registry.RegistryError(401, "AUTH_REQUIRED", "member token or package key required")
    original = await require_token(request, x_auth_token=token, x_project_id=request.headers.get("x-project-id"))
    member = await get_package_member_info(token_info=original)
    return await registry.member_actor(namespace, member)


@router.get("/projects/current")
async def project_context(member: dict = Depends(control_member)):
    return await registry.project_context(member)


@router.put("/projects/{project_id}/namespace")
async def register_namespace(
    project_id: str, response: Response, body: EmptyBody | None = None, member: dict = Depends(control_member)
):
    _, created = await registry.register_namespace(project_id, member)
    response.status_code = 201 if created else 200
    return await registry.project_context(member)


@router.post("/projects/{namespace}/keys", status_code=201)
async def issue_key(namespace: str, body: KeyCreate, response: Response, member: dict = Depends(control_member)):
    response.headers["Cache-Control"] = "no-store"
    return await registry.issue_key(namespace, member, body)


@router.get("/projects/{namespace}/keys")
async def list_keys(namespace: str, member: dict = Depends(control_member)):
    items = await registry.list_keys(namespace, member)
    return {"project_id": validate_keystone_id(member["project_id"]), "namespace": namespace, "items": items}


@router.delete("/projects/{namespace}/keys/{key_id}", status_code=204)
async def revoke_key(namespace: str, key_id: str, member: dict = Depends(control_member)):
    await registry.revoke_key(namespace, key_id, member)
    return Response(status_code=204)


@router.get("/auth/me")
async def auth_me(actor: registry.Actor = Depends(key_actor)):
    return await registry.auth_me(actor)


@router.get("/projects/{namespace}/packages")
async def inventory(
    namespace: str,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    package_type: Literal["oci-image", "runtime-bundle"] | None = None,
    actor: registry.Actor = Depends(read_actor),
):
    return await registry.inventory(actor, namespace, limit, cursor, package_type)


@router.get("/projects/{namespace}/package")
async def package_detail(namespace: str, package: str = Query(...), actor: registry.Actor = Depends(read_actor)):
    return await registry.package_detail(actor, namespace, package)


@router.get("/projects/{namespace}/versions")
async def versions(
    namespace: str,
    package: str = Query(...),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    actor: registry.Actor = Depends(read_actor),
):
    return await registry.versions(actor, namespace, package, limit, cursor)


@router.get("/projects/{namespace}/resolve")
async def resolve(
    namespace: str,
    response: Response,
    package: str = Query(...),
    tag: str = Query(...),
    actor: registry.Actor = Depends(read_actor),
):
    try:
        canonical_tag(tag)
    except ValueError:
        raise registry.RegistryError(422, "INVALID_TAG", "invalid canonical tag") from None
    result = await registry.version(actor, namespace, package, tag=tag)
    response.headers["ETag"] = '"' + result["root_digest"] + '"'
    return result


@router.get("/projects/{namespace}/versions/{digest}")
async def version(namespace: str, digest: str, package: str = Query(...), actor: registry.Actor = Depends(read_actor)):
    return await registry.version(actor, namespace, package, digest=digest)


def private_blob(request, digest, total, media_type, filename):
    blob_store = registry.store()
    try:
        if blob_store.size(digest) != total:
            raise registry.RegistryError(503, "HUB_UNAVAILABLE", "published blob size mismatch")
    except registry.HubStoreError:
        raise registry.RegistryError(503, "HUB_UNAVAILABLE", "published bytes unavailable") from None
    return _blob_response(
        blob_store,
        digest,
        total=total,
        media_type=media_type,
        filename=filename,
        range_header=request.headers.get("range"),
        cache_control="private, no-store",
    )


@router.get("/projects/{namespace}/versions/{digest}/download")
async def download(
    namespace: str,
    digest: str,
    request: Request,
    package: str = Query(...),
    actor: registry.Actor = Depends(read_actor),
):
    value = await registry.version(actor, namespace, package, digest=digest)
    return private_blob(
        request, value["archive_digest"], value["archive_size_bytes"], "application/x-tar", "package.oci.tar"
    )


@router.get("/projects/{namespace}/versions/{digest}/blobs/{blob_digest}")
async def version_blob(
    namespace: str,
    digest: str,
    blob_digest: str,
    request: Request,
    package: str = Query(...),
    actor: registry.Actor = Depends(read_actor),
):
    value = await registry.version(actor, namespace, package, digest=digest)
    blob_digest = registry.checked_digest(blob_digest)
    descriptor = value["graph"].get(blob_digest)
    if descriptor is None:
        raise registry.RegistryError(404, "NOT_FOUND", "blob is not reachable from authorized version")
    return private_blob(request, blob_digest, descriptor["size_bytes"], descriptor["media_type"], "blob")


@router.post("/projects/{namespace}/uploads", status_code=201)
async def start_upload(
    namespace: str, body: PackageUploadStart, package: str = Query(...), actor: registry.Actor = Depends(key_actor)
):
    return await registry.start_upload(actor, namespace, package, body, "package")


@router.get("/projects/{namespace}/uploads/{upload_id}")
async def upload_status(
    namespace: str, upload_id: str, package: str = Query(...), actor: registry.Actor = Depends(key_actor)
):
    return await registry.upload_status(actor, namespace, package, upload_id, "package")


@router.patch("/projects/{namespace}/uploads/{upload_id}", status_code=204)
async def append_upload(
    namespace: str,
    upload_id: str,
    request: Request,
    package: str = Query(...),
    actor: registry.Actor = Depends(key_actor),
):
    offset = await registry.append_upload(actor, namespace, package, upload_id, "package", request)
    return Response(status_code=204, headers={"Upload-Offset": str(offset)})


@router.put("/projects/{namespace}/uploads/{upload_id}")
async def finalize_upload(
    namespace: str,
    upload_id: str,
    response: Response,
    body: EmptyBody,
    package: str = Query(...),
    actor: registry.Actor = Depends(key_actor),
):
    result, changed = await registry.finalize_upload(actor, namespace, package, upload_id, "package")
    response.status_code = 201 if changed else 200
    return result


@router.delete("/projects/{namespace}/uploads/{upload_id}", status_code=204)
async def abort_upload(
    namespace: str, upload_id: str, package: str = Query(...), actor: registry.Actor = Depends(key_actor)
):
    await registry.abort_upload(actor, namespace, package, upload_id, "package")
    return Response(status_code=204)


@router.get("/projects/{namespace}/cache/resolve")
async def resolve_cache(
    namespace: str,
    package: str = Query(...),
    build_key: str = Query(...),
    cache_scope: str = Query(...),
    platform: str = Query(...),
    builder_fingerprint: str = Query(...),
    actor: registry.Actor = Depends(key_actor),
):
    try:
        partition = CachePartition(
            build_key=build_key, cache_scope=cache_scope, platform=platform, builder_fingerprint=builder_fingerprint
        )
    except ValidationError:
        raise registry.RegistryError(422, "INVALID_CACHE_BINDING", "invalid cache partition") from None
    return await registry.resolve_cache(actor, namespace, package, partition)


@router.get("/projects/{namespace}/cache/archives/{digest}")
async def cache_archive(
    namespace: str, digest: str, request: Request, package: str = Query(...), actor: registry.Actor = Depends(key_actor)
):
    size = await registry.cache_archive(actor, namespace, package, digest)
    return private_blob(request, digest, size, "application/x-tar", "buildkit-cache.tar")


@router.post("/projects/{namespace}/cache/uploads", status_code=201)
async def start_cache_upload(
    namespace: str, body: CacheUploadStart, package: str = Query(...), actor: registry.Actor = Depends(key_actor)
):
    return await registry.start_upload(actor, namespace, package, body, "cache")


@router.get("/projects/{namespace}/cache/uploads/{upload_id}")
async def cache_upload_status(
    namespace: str, upload_id: str, package: str = Query(...), actor: registry.Actor = Depends(key_actor)
):
    return await registry.upload_status(actor, namespace, package, upload_id, "cache")


@router.patch("/projects/{namespace}/cache/uploads/{upload_id}", status_code=204)
async def append_cache_upload(
    namespace: str,
    upload_id: str,
    request: Request,
    package: str = Query(...),
    actor: registry.Actor = Depends(key_actor),
):
    offset = await registry.append_upload(actor, namespace, package, upload_id, "cache", request)
    return Response(status_code=204, headers={"Upload-Offset": str(offset)})


@router.put("/projects/{namespace}/cache/uploads/{upload_id}")
async def finalize_cache_upload(
    namespace: str,
    upload_id: str,
    response: Response,
    body: EmptyBody,
    package: str = Query(...),
    actor: registry.Actor = Depends(key_actor),
):
    result, changed = await registry.finalize_upload(actor, namespace, package, upload_id, "cache")
    response.status_code = 201 if changed else 200
    return result


@router.delete("/projects/{namespace}/cache/uploads/{upload_id}", status_code=204)
async def abort_cache_upload(
    namespace: str, upload_id: str, package: str = Query(...), actor: registry.Actor = Depends(key_actor)
):
    await registry.abort_upload(actor, namespace, package, upload_id, "cache")
    return Response(status_code=204)
