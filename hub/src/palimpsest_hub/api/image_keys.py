"""Image API key management; only original human Keystone member tokens manage keys."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from palimpsest_hub.auth import original_human_member
from palimpsest_hub.services import image_keys as service
from palimpsest_hub.services.image_keys import ImageKeyCreate

router = APIRouter(tags=["image-keys"])


@router.post("/image-keys", status_code=201, operation_id="issue_image_key")
async def issue_key(body: ImageKeyCreate, response: Response, member: dict = Depends(original_human_member)):
    # The secret is returned exactly once and must never be cached by intermediaries.
    response.headers["Cache-Control"] = "no-store"
    return await service.issue_key(member, body)


@router.get("/image-keys", operation_id="list_image_keys")
async def list_keys(response: Response, member: dict = Depends(original_human_member)):
    response.headers["Cache-Control"] = "no-store"
    return {"project_id": member["project_id"], "items": await service.list_keys(member)}


@router.delete("/image-keys/{key_id}", status_code=204, operation_id="revoke_image_key")
async def revoke_key(key_id: str, member: dict = Depends(original_human_member)):
    await service.revoke_key(member, key_id)
    return Response(status_code=204)
