from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from telethon import types, utils
from telethon.tl.functions.messages import GetDialogFiltersRequest, GetPeerDialogsRequest


def folder_title(folder) -> str:
    title = getattr(folder, "title", "")
    return getattr(title, "text", title)


def folder_matches(folder, dialog, now: datetime | None = None, *, personal: bool = False) -> bool:
    """Keep vacancy sources and personal HR dialogs as separate selections."""
    if not isinstance(dialog.entity, types.User if personal else (types.Channel, types.Chat)):
        return False
    peer_id = utils.get_peer_id(dialog.entity)
    excluded = {utils.get_peer_id(p) for p in getattr(folder, "exclude_peers", [])}
    explicit = {utils.get_peer_id(p) for p in (
        getattr(folder, "include_peers", []) + getattr(folder, "pinned_peers", [])
    )}
    if peer_id in excluded:
        return False
    if peer_id in explicit:
        return True
    if personal:
        flag = "bots" if getattr(dialog.entity, "bot", False) else (
            "contacts" if getattr(dialog.entity, "contact", False) else "non_contacts")
    else:
        broadcast = isinstance(dialog.entity, types.Channel) and bool(dialog.entity.broadcast)
        flag = "broadcasts" if broadcast else "groups"
    if not getattr(folder, flag, False):
        return False
    raw = dialog.dialog
    if getattr(folder, "exclude_archived", False) and getattr(raw, "folder_id", None) == 1:
        return False
    if getattr(folder, "exclude_read", False) and not (dialog.unread_count or getattr(raw, "unread_mark", False)):
        return False
    muted_until = getattr(getattr(raw, "notify_settings", None), "mute_until", None)
    if getattr(folder, "exclude_muted", False) and muted_until:
        now = now or datetime.now(timezone.utc)
        if isinstance(muted_until, datetime):
            if muted_until.replace(tzinfo=muted_until.tzinfo or timezone.utc) > now:
                return False
        elif muted_until > now.timestamp():
            return False
    return True


async def get_folders(client) -> list:
    result = await client(GetDialogFiltersRequest())
    return [f for f in getattr(result, "filters", result) if getattr(f, "id", None) is not None]


def normalized_folder_title(value: str) -> str:
    return "".join(char for char in value.casefold().replace("ё", "е") if char.isalnum())


async def response_folder_dialogs(client, selection: str = "Удаленки"):
    folders = await get_folders(client)
    name = normalized_folder_title(selection)
    matches = [item for item in folders if str(item.id) == selection or normalized_folder_title(folder_title(item)) == name]
    if not matches:
        raise ValueError(f"Папка «{selection}» не найдена. Названия и ID: /folders. Выбрать папку откликов: /responses folder название")
    if len(matches) > 1:
        raise ValueError("Нашлось несколько папок с таким названием. Укажи ID: /responses folder ID")
    selected = matches[0]
    # Explicit custom folders need only one peer-dialog request for each 100
    # users, rather than enumerating the account's unrelated personal chats.
    if not (getattr(selected, "contacts", False) or getattr(selected, "non_contacts", False)):
        excluded = {utils.get_peer_id(item) for item in getattr(selected, "exclude_peers", ())}
        peers = {item.user_id: item for item in list(getattr(selected, "include_peers", ())) +
                 list(getattr(selected, "pinned_peers", ())) if isinstance(item, types.InputPeerUser) and item.user_id not in excluded}
        records = []
        values = list(peers.values())
        for start in range(0, len(values), 100):
            data = await client(GetPeerDialogsRequest([types.InputDialogPeer(peer) for peer in values[start:start + 100]]))
            users = {item.id: item for item in data.users}
            messages = {item.id: item for item in data.messages}
            for raw in data.dialogs:
                user_id = getattr(raw.peer, "user_id", None)
                if user_id in users and user_id in peers:
                    records.append(SimpleNamespace(entity=users[user_id], input_entity=peers[user_id],
                        is_user=True, date=getattr(messages.get(raw.top_message), "date", None), dialog=raw,
                        unread_count=raw.unread_count))
        return selected, records
    records = []
    async for dialog in client.iter_dialogs():
        if folder_matches(selected, dialog, personal=True):
            records.append(dialog)
    return selected, records


async def resolve_folder(client, selection: str):
    folders = await get_folders(client)
    matches = [f for f in folders if str(f.id) == selection or folder_title(f).casefold() == selection.casefold()]
    if not matches:
        raise ValueError("Папка не найдена. /folders покажет названия и ID папок")
    if len(matches) > 1:
        raise ValueError("Есть папки с одинаковым названием. Укажите числовой ID из /folders")
    selected = matches[0]
    records = []
    async for dialog in client.iter_dialogs():
        if folder_matches(selected, dialog):
            entity = dialog.entity
            records.append({
                "peer_id": utils.get_peer_id(entity), "raw_id": entity.id,
                "kind": "channel" if isinstance(entity, types.Channel) else "chat",
                "access_hash": getattr(entity, "access_hash", None),
                "title": entity.title, "username": getattr(entity, "username", None),
                "protected": bool(getattr(entity, "noforwards", False)),
            })
    return selected, records


def source_input(source):
    if source["kind"] == "channel":
        return types.InputPeerChannel(source["raw_id"], source["access_hash"])
    return types.InputPeerChat(source["raw_id"])


def message_link(source, message_id: int) -> str | None:
    if source["username"]:
        return f"https://t.me/{source['username']}/{message_id}"
    if source["kind"] == "channel":
        return f"https://t.me/c/{source['raw_id']}/{message_id}"
    return None
