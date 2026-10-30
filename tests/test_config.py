import os
import unittest
from unittest.mock import patch

from vacancy_bot.config import Config


class ConfigTests(unittest.TestCase):
    BASE = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "test-hash", "BOT_TOKEN": "test-token", "OWNER_TELEGRAM_ID": "111"}

    def load(self, **values):
        with patch.dict(os.environ, dict(self.BASE, **values), clear=True), patch("vacancy_bot.config.load_dotenv"):
            return Config.load()

    def test_defaults_require_only_telegram_credentials(self):
        config = self.load()
        self.assertEqual(config.owner, 111)
        self.assertEqual(config.poll_seconds, 30)
        self.assertEqual(config.responses_folder, "Удаленки")

    def test_telegram_age_cannot_exceed_three_days_with_legacy_settings(self):
        for days, expected in (("1", 1), ("3", 3), ("7", 3), ("30", 3)):
            with self.subTest(days=days):
                config = self.load(MAX_POST_AGE_DAYS=days)
                self.assertEqual(config.tg_max_age_days, expected)
                self.assertEqual(config.max_age_days, int(days))

    def test_source_and_hr_folders_are_separate(self):
        config = self.load(TELEGRAM_FOLDER="WORK", RESPONSES_FOLDER="51")
        self.assertEqual(config.folder, "WORK")
        self.assertEqual(config.responses_folder, "51")

    def test_invalid_telegram_limits_have_actionable_errors(self):
        for settings in ({"POLL_SECONDS":"0"}, {"HISTORY_LIMIT":"1001"},
                         {"MAX_POST_AGE_DAYS":"bad"}, {"MAX_NOTIFICATIONS_PER_CYCLE":"0"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.load(**settings)
