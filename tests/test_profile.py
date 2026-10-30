import json
import tempfile
import unittest
from pathlib import Path

from vacancy_bot.matcher import classify
from vacancy_bot.profile import Profile, update_profile


class ProfileTests(unittest.TestCase):
    def test_saved_threshold_migration_preserves_higher_scores(self):
        root = Path(__file__).resolve().parent.parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path = Path(directory) / "profile.json"
            for saved, expected in [(55, 60), (60, 60), (80, 80)]:
                with self.subTest(saved=saved):
                    path.write_text(json.dumps({"min_score": saved, "hours_max": 5}), encoding="utf-8")
                    profile = Profile.load(path)
                    self.assertEqual(profile.min_score, expected)
                    self.assertEqual(profile.hours_max, 5)

    def test_old_salary_ceiling_is_removed_on_load_and_save(self):
        root = Path(__file__).resolve().parent.parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps({"salary_min": 40000, "salary_max": 60000}), encoding="utf-8")
            profile = Profile.load(path)
            result = classify("Вакансия: ассистент. Удалённо, 4 часа в день. Оклад 150 000 ₽. Без опыта.", profile)
            self.assertTrue(result.eligible)
            self.assertEqual(result.score, 100)
            profile.save(path)
            self.assertNotIn("salary_max", json.loads(path.read_text(encoding="utf-8")))

    def test_salary_command_sets_only_the_minimum(self):
        for argument in ["salary 40", "salary 40000"]:
            with self.subTest(argument=argument):
                profile = update_profile(Profile(), argument)
                self.assertEqual(profile.salary_min, 40000)
                self.assertIn("Оклад: от 40 000 ₽/месяц, без верхней границы", profile.describe())
