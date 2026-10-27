"""Current native action projection, using the original Keystone subject."""

import asyncio

from fastapi import APIRouter, Depends

from palimpsest_hub.auth import _package_admin_context, get_package_member_info, require_token

router = APIRouter(tags=["policy"])


@router.get("/policy/capabilities")
async def capabilities(token_info: dict = Depends(require_token)):
    if token_info.get("is_system_admin"):
        member = await asyncio.to_thread(_package_admin_context, token_info)
    else:
        member = await get_package_member_info(token_info, allow_empty=True)
    return {
        "service": "palimpsest",
        "capabilities": sorted(member["package_capabilities"]),
        "actions": sorted(member["package_actions"]),
    }
