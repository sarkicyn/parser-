import unittest

from vacancy_bot.matcher import classify
from vacancy_bot.professionals import classify_professional, parse_pro_request


TECH = ("Вакансия: Python-разработчик. Удалённо, полный рабочий день. "
        "Оплата 20 000 ₽, только проценты.\nТребования:\nОпыт работы от 5 лет.\nPython, PostgreSQL, Docker.")


class ProfessionalTests(unittest.TestCase):
    def test_independent_of_personal_hours_salary_fixed_and_experience_limits(self):
        self.assertFalse(classify(TECH).eligible)
        match = classify_professional(TECH)
        self.assertTrue(match.eligible)
        self.assertEqual(match.group, 1)
        self.assertIn("Опыт работы от 5 лет.", match.evidence)
        self.assertTrue(any("PostgreSQL" in evidence for evidence in match.evidence))

    def test_role_groups_require_relevant_qualifications(self):
        for role, requirements, group in (
            ("Backend developer", "Знание Python и SQL.", 1),
            ("QA engineer", "Опыт тестирования от 2 лет.", 1),
            ("аналитик данных", "SQL и Power BI.", 2),
            ("маркетолог", "Яндекс.Директ, Google Ads.", 3),
            ("менеджер по продажам", "Опыт B2B-продаж и знание CRM.", 4),
            ("Product manager", "Agile, Scrum, CustDev, roadmap.", 5),
            ("менеджер проектов", "Jira, Kanban.", 5),
        ):
            with self.subTest(role=role):
                match = classify_professional(f"Вакансия: {role}. Удалённо.\nТребования:\n{requirements}")
                self.assertTrue(match.eligible)
                self.assertEqual(match.group, group)

    def test_remote_must_be_explicit_and_not_hybrid(self):
        for change in ("Работа в офисе", "Гибрид", "Формат не указан", "Не удалённо"):
            with self.subTest(format=change):
                self.assertFalse(classify_professional(TECH.replace("Удалённо", change)).eligible)

    def test_entry_level_optional_skills_and_irrelevant_roles_are_excluded(self):
        for text in (
            "Вакансия: менеджер по продажам. Удалённо. Без опыта. Грамотная речь.",
            "Вакансия: Python-разработчик. Удалённо. Обучим с нуля. Требования: Python.",
            "Вакансия: маркетолог. Удалённо.\nТребования:\nОпыт не обязателен.\nGoogle Ads будет плюсом.",
            "Вакансия: разработчик. Удалённо.\nТребования:\nОпыт 0 лет.",
            "Вакансия: ассистент. Удалённо.\nТребования:\nОпыт 5 лет, Excel.",
            "Ищу работу Python-разработчиком. Удалённо. Опыт работы 5 лет, Python, SQL.",
            "Вакансия: менеджер по продажам. Удалённо. Опытный пользователь ПК.",
        ):
            with self.subTest(text=text):
                self.assertFalse(classify_professional(text).eligible)

    def test_company_roles_and_training_benefits_are_not_required_skills(self):
        for text in (
            "Вакансия: менеджер по продажам. Удалённо.\nО компании:\nУ нашей компании опыт работы 10 лет.\nУсловия:\nОбучение CRM и возможность получить опыт.",
            "Вакансия: модератор. Удалённо.\nО компании:\nМы ищем программистов, аналитиков и маркетологов для наших клиентов. Опыт работы 5 лет.",
            "Вакансия: менеджер по продажам. Удалённо.\nО компании:\nБолее 10 лет опыта B2B-продаж.\nТребования:\nГрамотная речь.",
        ):
            with self.subTest(text=text):
                self.assertFalse(classify_professional(text).eligible)

    def test_inline_required_experience_without_numeric_years_is_accepted(self):
        match = classify_professional("Вакансия: менеджер по продажам. Удалённо. Оклад: 30000 ₽. Требуется опыт B2B-продаж.")
        self.assertTrue(match.eligible)
        self.assertEqual(match.group, 4)

    def test_group_and_page_parsing(self):
        for request, expected in (("", (None, 1)), ("2", (2, 1)), ("page 3", (None, 3)),
                                  ("5 page 2", (5, 2))):
            self.assertEqual(parse_pro_request(request), expected)
        for request in ("0", "6", "page 0", "-1", "page -2", "1 2", "3 page 1000001"):
            with self.subTest(request=request), self.assertRaises(ValueError):
                parse_pro_request(request)
