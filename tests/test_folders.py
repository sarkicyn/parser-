import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import types

from vacancy_bot.folders import folder_matches, folder_title, response_folder_dialogs


def channel_dialog(raw_id=10, broadcast=True, **settings):
    entity = types.Channel(id=raw_id, title="Jobs", photo=types.ChatPhotoEmpty(),
                           date=datetime.now(timezone.utc), broadcast=broadcast,
                           megagroup=not broadcast, access_hash=1)
    return SimpleNamespace(
        entity=entity, unread_count=settings.get("unread_count", 1),
        dialog=SimpleNamespace(folder_id=settings.get("folder_id"),
                               unread_mark=False,
                               notify_settings=SimpleNamespace(mute_until=settings.get("mute_until"))),
    )


def folder(**changes):
    fields = dict(id=2, title=types.TextWithEntities("Вакансии", []),
                  pinned_peers=[], include_peers=[], exclude_peers=[])
    fields.update(changes)
    return types.DialogFilter(**fields)


class FolderTests(unittest.TestCase):
    def test_explicit_and_pinned_channels(self):
        for key in ("include_peers", "pinned_peers"):
            self.assertTrue(folder_matches(folder(**{key: [types.InputPeerChannel(10, 1)]}), channel_dialog()))

    def test_unrelated_channel_excluded(self):
        self.assertFalse(folder_matches(folder(include_peers=[types.InputPeerChannel(11, 1)]), channel_dialog()))

    def test_categories(self):
        self.assertTrue(folder_matches(folder(broadcasts=True), channel_dialog()))
        self.assertFalse(folder_matches(folder(broadcasts=True), channel_dialog(broadcast=False)))
        self.assertTrue(folder_matches(folder(groups=True), channel_dialog(broadcast=False)))

    def test_explicit_exclusion(self):
        f = folder(broadcasts=True, exclude_peers=[types.InputPeerChannel(10, 1)])
        self.assertFalse(folder_matches(f, channel_dialog()))

    def test_status_exclusions(self):
        self.assertFalse(folder_matches(folder(broadcasts=True, exclude_archived=True), channel_dialog(folder_id=1)))
        self.assertFalse(folder_matches(folder(broadcasts=True, exclude_read=True), channel_dialog(unread_count=0)))
        self.assertFalse(folder_matches(folder(broadcasts=True, exclude_muted=True),
                                        channel_dialog(mute_until=datetime.now(timezone.utc) + timedelta(days=1))))

    def test_explicit_members_override_status_exclusions(self):
        f = folder(include_peers=[types.InputPeerChannel(10, 1)], exclude_read=True)
        self.assertTrue(folder_matches(f, channel_dialog(unread_count=0)))

    def test_shared_folder(self):
        f = types.DialogFilterChatlist(id=3, title=types.TextWithEntities("Работа", []),
                                       pinned_peers=[], include_peers=[types.InputPeerChannel(10, 1)])
        self.assertTrue(folder_matches(f, channel_dialog()))
        self.assertEqual(folder_title(f), "Работа")

    def test_personal_dialog_is_never_read(self):
        dialog = SimpleNamespace(entity=types.User(id=10, first_name="Someone"))
        self.assertFalse(folder_matches(folder(contacts=True, non_contacts=True), dialog))

    def test_hr_folder_includes_only_personal_members_and_respects_exclusions(self):
        dialog = SimpleNamespace(entity=types.User(id=10, first_name="HR"))
        selected = folder(include_peers=[types.InputPeerUser(10, 1)])
        self.assertTrue(folder_matches(selected, dialog, personal=True))
        self.assertFalse(folder_matches(selected, channel_dialog(), personal=True))
        self.assertFalse(folder_matches(folder(include_peers=[types.InputPeerUser(11, 1)]), dialog, personal=True))
        selected.exclude_peers = [types.InputPeerUser(10, 1)]
        self.assertFalse(folder_matches(selected, dialog, personal=True))


class PersonalFolderResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_remote_folder_uses_batched_members_without_enumerating_other_chats(self):
        selected = folder(id=51, title=types.TextWithEntities("💻 Удалёнки", []),
                          include_peers=[types.InputPeerUser(10, 1), types.InputPeerUser(11, 1), types.InputPeerChannel(12, 1)],
                          exclude_peers=[types.InputPeerUser(11, 1)])
        data = SimpleNamespace(users=[types.User(id=10, first_name="HR")],
            dialogs=[SimpleNamespace(peer=types.PeerUser(10), top_message=7, unread_count=0)],
            messages=[SimpleNamespace(id=7, date=datetime.now(timezone.utc))])
        client = AsyncMock(side_effect=[SimpleNamespace(filters=[folder(), selected]), data])
        resolved, dialogs = await response_folder_dialogs(client, "Удаленки")
        self.assertEqual(resolved.id, 51)
        self.assertEqual([dialog.entity.id for dialog in dialogs], [10])
        request = client.await_args_list[1].args[0]
        self.assertEqual(len(request.peers), 1)
        self.assertEqual(request.peers[0].peer.user_id, 10)
        self.assertEqual(dialogs[0].input_entity.user_id, 10)

    async def test_missing_folder_is_an_error_instead_of_scanning_all_chats(self):
        client = AsyncMock(return_value=SimpleNamespace(filters=[folder()]))
        with self.assertRaisesRegex(ValueError, "не найдена"):
            await response_folder_dialogs(client, "Удаленки")
        self.assertEqual(client.await_count, 1)
