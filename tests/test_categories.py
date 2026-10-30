import unittest
from dataclasses import replace
from unittest.mock import patch

from vacancy_bot.categories import categorize
from vacancy_bot.matcher import classify, split_post
from vacancy_bot.profile import Profile


CONDITIONS = "\nУдалённо, 4 часа в день. Оклад 50 000 ₽. Без опыта."


def vacancy(title, duties=""):
    return "Вакансия: " + title + CONDITIONS + ("\nОбязанности: " + duties if duties else "")


class CategoryTests(unittest.TestCase):
    def test_bilingual_role_families(self):
        titles = {
            1: ["SMM Manager", "Text Editor", "Copywriter", "Scriptwriter", "Content Manager",
                "Proofreader", "Social Media Manager", "Content Creator", "Technical Writer",
                "Редактор", "Копирайтер", "Сценарист", "Корректор", "Контент-менеджер",
                "SMM-специалист", "Менеджер социальных сетей"],
            2: ["Online Chat Support Specialist", "Chat Moderator", "Customer Support Specialist",
                "Online Chat Operator", "Community Moderator", "Content Moderator", "Support Agent",
                "Модератор чата", "Оператор онлайн-чата", "Специалист поддержки", "Оператор техподдержки"],
            3: ["Junior Project Manager", "Product Manager", "Sales Manager", "Account Manager",
                "Business Development Manager", "Customer Success Manager", "Project Coordinator",
                "Менеджер проектов", "Менеджер по продажам", "Аккаунт-менеджер",
                "Менеджер по работе с клиентами", "Менеджер по развитию бизнеса"],
            4: ["Data Entry Specialist", "Специалист по вводу данных", "Бухгалтер", "Python Developer"],
        }
        for group, roles in titles.items():
            for title in roles:
                with self.subTest(title=title, group=group):
                    match = classify(vacancy(title))
                    self.assertTrue(match.eligible)
                    self.assertEqual(match.category, group)
                    self.assertEqual(match.to_dict()["category"], group)

    def test_unusual_titles_use_responsibilities(self):
        cases = [
            ("Мастер смыслов", "Писать тексты для лендингов; редактировать статьи", 1),
            ("Brand Story Architect", "Craft compelling copy for landing pages; proofread articles", 1),
            ("Друг пользователей", "Отвечать на обращения в чате; помогать пользователям решать проблемы", 2),
            ("Community Guardian", "Moderate community comments; remove spam and abuse", 2),
            ("Delivery Champion", "Coordinate project teams; track budgets and deadlines", 3),
            ("Партнёр бизнеса", "Вести переговоры с клиентами; привлекать новых клиентов", 3),
            ("Специалист", "Ввод данных в таблицы; сверка документов", 4),
        ]
        for title, duties, group in cases:
            with self.subTest(title=title):
                self.assertEqual(classify(vacancy(title, duties)).category, group)

    def test_unheaded_responsibilities_are_recognized(self):
        text = vacancy("Мастер смыслов") + "\nПисать статьи для блога и редактировать тексты."
        self.assertEqual(classify(text).category, 1)
        text = vacancy("Brand Voice") + "\nCraft compelling copy for articles and landing pages."
        self.assertEqual(classify(text).category, 1)

    def test_unheaded_duties_after_metadata_are_not_lost(self):
        text = vacancy("Мастер смыслов") + "\nКомпания: Acme\nПисать статьи и редактировать тексты."
        self.assertEqual(classify(text).category, 1)

    def test_unheaded_benefits_and_background_do_not_vote(self):
        text = vacancy("Data Entry Specialist") + "\nМы создаём тексты и редактируем статьи.\nУсловия: обучение написанию статей."
        self.assertEqual(classify(text).category, 4)

    def test_primary_responsibilities_override_misleading_title(self):
        self.assertEqual(classify(vacancy("Content Manager", "Модерировать контент; удалять спам и нарушителей")).category, 2)
        self.assertEqual(classify(vacancy("Support Agent", "Привлекать клиентов; вести переговоры с клиентами")).category, 3)
        self.assertEqual(classify(vacancy("SMM Manager", "Управлять проектами команды; контролировать бюджет и сроки")).category, 3)

    def test_title_breaks_equal_responsibility_votes(self):
        text = vacancy("Project Manager", "Write articles; answer messages; coordinate projects")
        self.assertEqual(classify(text).category, 3)

    def test_requirements_and_company_background_are_not_duties(self):
        text = vacancy("Data Entry Specialist", "Ввод данных в таблицы") + (
            "\nТребования: опыт в продажах и редактировании статей."
            "\nО компании: мы создаём контент и управляем проектами."
        )
        self.assertEqual(classify(text).category, 4)

    def test_negated_sales_does_not_change_chat_support(self):
        text = vacancy("Специалист", "Отвечать на сообщения в чате; без продаж услуг и привлечения клиентов")
        self.assertEqual(classify(text).category, 2)

    def test_phone_only_support_is_not_text_support(self):
        self.assertEqual(classify(vacancy("Support Agent", "Assist customers by phone only")).category, 4)

    def test_group_four_does_not_bypass_any_eligibility_filter(self):
        base = vacancy("Data Entry Specialist")
        rejected = [base.replace("4 часа", "8 часов"), base.replace("Без опыта", "Опыт 5 лет"),
                    base.replace("Удалённо", "Работа в офисе"), base.replace("50 000", "30 000")]
        for text in rejected:
            with self.subTest(text=text), patch("vacancy_bot.matcher.categorize") as categorize_mock:
                match = classify(text)
                self.assertFalse(match.eligible)
                self.assertIsNone(match.category)
                categorize_mock.assert_not_called()
        self.assertIsNone(classify(base, replace(Profile(), exclude_keywords=["Data Entry"])).category)
        self.assertIsNone(classify("Ищем специалиста", replace(Profile(), include_unknown=False)).category)

    def test_roundup_roles_do_not_bleed_across_items(self):
        parts = split_post(vacancy("Copywriter") + "\n\n" + vacancy("Support Agent"))
        self.assertEqual([classify(part).category for part in parts], [1, 2])

    def test_direct_classifier_returns_one_stable_group(self):
        self.assertEqual(categorize("Вакансия: менеджер\nЗадачи: ввод данных"), 4)
        self.assertIn(categorize(vacancy("Специалист", "Write articles; answer messages")), (1, 2))
