"""Owner-managed, persistent Telegram Bot conversational reply permissions."""

from agent_core import action


@action(
    name="get_telegram_auto_reply_contacts",
    description="Read the saved Telegram Bot auto-reply contact IDs. This is an owner configuration action, not available to third-party senders.",
    default=True,
    action_sets=["core", "telegram"],
    parallelizable=True,
    input_schema={},
    output_schema={
        "status": {"type": "string", "example": "success"},
        "contact_ids": {"type": "array", "example": ["123456789"]},
    },
)
def get_telegram_auto_reply_contacts(input_data: dict) -> dict:
    from agent_core.core.impl.action.context import current_message_origin
    from craftos_integrations.service import get_config

    origin = current_message_origin.get() or {}
    if (origin.get("contact_id") or origin.get("integration_type")) and origin.get(
        "is_self_message"
    ) is not True:
        return {
            "status": "error",
            "message": "Only the owner can inspect auto-reply permissions.",
        }
    config = get_config("telegram_bot") or {}
    return {
        "status": "success",
        "contact_ids": config.get("auto_reply_contact_ids", []),
    }


@action(
    name="set_telegram_contact_auto_reply",
    description=(
        "Enable or disable automatic conversational replies to one selected "
        "Telegram Bot contact, saving permission for future private messages. "
        "Use only on the owner's instruction (for example, 'from now on, "
        "auto-reply to Tham'). Resolve the numeric sender ID from incoming "
        "Telegram messages; never guess an ID or use a display name. "
        "Applies to all connected Telegram bot accounts. "
        "Third parties cannot change this permission."
    ),
    default=True,
    action_sets=["core", "telegram"],
    parallelizable=False,
    input_schema={
        "contact_id": {
            "type": "string",
            "description": "Numeric Telegram user ID of the selected contact.",
            "example": "123456789",
        },
        "enabled": {
            "type": "boolean",
            "description": "True to enable auto-replies; false to disable them.",
            "example": True,
        },
    },
    output_schema={
        "status": {"type": "string", "example": "success"},
        "message": {"type": "string", "example": "Auto-replies enabled."},
        "contact_ids": {"type": "array", "example": ["123456789"]},
    },
)
def set_telegram_contact_auto_reply(input_data: dict) -> dict:
    from agent_core.core.impl.action.context import current_message_origin
    from craftos_integrations.service import modify_config

    origin = current_message_origin.get() or {}
    if (origin.get("contact_id") or origin.get("integration_type")) and origin.get(
        "is_self_message"
    ) is not True:
        return {
            "status": "error",
            "message": "Only the owner can change auto-reply permissions.",
        }
    contact_id = input_data.get("contact_id", "")
    enabled = input_data.get("enabled")
    if (
        not isinstance(contact_id, str)
        or not contact_id.isascii()
        or not contact_id.isdecimal()
        or len(contact_id) > 20
        or contact_id.startswith("0")
        or int(contact_id) <= 0
        or not isinstance(enabled, bool)
    ):
        return {
            "status": "error",
            "message": "A positive numeric Telegram contact_id and boolean enabled are required.",
        }

    def modify(config):
        saved_contacts = config.get("auto_reply_contact_ids", [])
        if not isinstance(saved_contacts, list):
            raise ValueError(
                "Invalid saved contact list; correct it in Telegram Bot settings."
            )
        contacts = list(saved_contacts)
        if enabled:
            if contact_id not in contacts:
                contacts.append(contact_id)
        else:
            contacts = [cid for cid in contacts if cid != contact_id]
        return {"auto_reply_contact_ids": contacts}

    ok, message, saved = modify_config("telegram_bot", modify)
    if not ok:
        return {"status": "error", "message": message}
    return {
        "status": "success",
        "message": f"Auto-replies {'enabled' if enabled else 'disabled'} for Telegram contact {contact_id}.",
        "contact_ids": saved["auto_reply_contact_ids"],
    }
