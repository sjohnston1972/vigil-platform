"""
Out-of-band approval notifications for step-up auth.

Fire-and-forget: all functions catch their own exceptions.
Notification failure never blocks the in-chat approval path.
"""

import logging
import os

import httpx
from azure.communication.email.aio import EmailClient
from azure.identity.aio import DefaultAzureCredential

logger = logging.getLogger(__name__)

GATEWAY_EXTERNAL_URL = os.getenv("GATEWAY_EXTERNAL_URL", "")


async def notify_approvers(step_up_request: dict, tenant_config: dict) -> None:
    """Send approval notification to configured out-of-band channels. Never raises."""
    approve_url = f"{GATEWAY_EXTERNAL_URL}/step-up/{step_up_request['id']}/approve"
    reject_url  = f"{GATEWAY_EXTERNAL_URL}/step-up/{step_up_request['id']}/reject"

    body = {
        "tool":         step_up_request["tool_name"],
        "requested_by": step_up_request["requested_by"],
        "context":      step_up_request["context"],
        "expires_at":   step_up_request["expires_at"],
        "approve_url":  approve_url,
        "reject_url":   reject_url,
    }

    email = tenant_config.get("step_up_notification_email")
    if email:
        try:
            await _send_email(email, body)
        except Exception as exc:
            logger.warning("notify_approvers: email dispatch error", exc_info=exc)

    webhook = tenant_config.get("step_up_webhook_url")
    if webhook:
        try:
            await _post_webhook(webhook, body)
        except Exception as exc:
            logger.warning("notify_approvers: webhook dispatch error", exc_info=exc)


async def _send_email(recipient: str, body: dict) -> None:
    """Send approval email via Azure Communication Services (Managed Identity)."""
    try:
        credential = DefaultAzureCredential()
        client = EmailClient(
            endpoint=os.getenv("ACS_ENDPOINT", ""),
            credential=credential,
        )
        message = {
            "senderAddress": os.getenv("ACS_SENDER_ADDRESS", "noreply@vigil"),
            "recipients": {"to": [{"address": recipient}]},
            "content": {
                "subject": f"[VIGIL] Approval required: {body['tool']}",
                "plainText": (
                    f"Tool: {body['tool']}\n"
                    f"Requested by: {body['requested_by']}\n"
                    f"Expires: {body['expires_at']}\n\n"
                    f"Approve: {body['approve_url']}\n"
                    f"Reject:  {body['reject_url']}"
                ),
            },
        }
        async with client:
            await client.begin_send(message)
    except Exception as exc:
        logger.warning(
            "Email notification failed",
            extra={"recipient": recipient, "tool": body.get("tool")},
            exc_info=exc,
        )


async def _post_webhook(url: str, body: dict) -> None:
    """POST approval payload to a webhook URL. Single retry on failure."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
            return
        except Exception as exc:
            if attempt == 0:
                logger.debug("Webhook attempt 1 failed, retrying", exc_info=exc)
            else:
                logger.warning(
                    "Webhook notification failed after retry",
                    extra={"url": url, "tool": body.get("tool")},
                    exc_info=exc,
                )
