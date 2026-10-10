"""Saved chat conversations and app information for the chat screen."""

from fastapi import APIRouter, Depends

from app import conversations
from app.auth import CurrentUser, current_user
from app.config import get_settings

router = APIRouter(prefix="/api", tags=["chat"])

PRODUCT_NAME = "TCS Knowledge Fabric"


@router.get("/conversations")
def list_conversations(user: CurrentUser = Depends(current_user)):
    """The user's last 10 conversations (on knowledge bases they can still access), newest first."""
    return conversations.list_recent(user)


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, user: CurrentUser = Depends(current_user)):
    conv = conversations.get(user, conversation_id)
    return {
        "id": conv["id"],
        "kb_name": conv["kb_name"],
        "title": conv["title"],
        "messages": conversations.messages(conv["id"]),
    }


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, user: CurrentUser = Depends(current_user)):
    conversations.delete(user, conversation_id)
    return {"deleted": conversation_id}


@router.get("/app-info")
def app_info(user: CurrentUser = Depends(current_user)):
    """What the UI shows about the deployment: product name and where agents connect (MCP)."""
    s = get_settings()
    return {
        "product": PRODUCT_NAME,
        "mcp_url": f"{s.mcp_public_url.rstrip('/')}/mcp",
        "mcp_audience": s.mcp_audience,
        "keycloak_issuer": f"{s.keycloak_url.rstrip('/')}/realms/{s.keycloak_realm}" if s.keycloak_url else None,
        "conversations_kept": get_settings().conversation_retention_count,
    }
