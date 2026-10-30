import unittest

from vacancy_bot.matcher import classify, split_post
from vacancy_bot.summary import MAX_SUMMARY_LENGTH
from vacancy_bot.description import extract_description


BASE = "Вакансия: ассистент. Удалённо, 4 часа в день. Оклад 50 000 ₽. Без опыта."


class SummaryTests(unittest.TestCase):
    def test_role_tasks_and_conditions_are_composed(self):
        text = BASE + "\nОбязанности:\n• Ведение таблиц\n• Ответы на письма\nТребования: Excel."
        match = classify(text)
        self.assertIn("Позиция: ассистент.", match.summary)
        self.assertIn("Задачи: Ведение таблиц; Ответы на письма.", match.summary)
        self.assertIn("удалённо; 4 ч/день; оклад 50 000 ₽/мес.; без опыта", match.summary)
        self.assertIn("Требования: Excel", match.summary)
        self.assertEqual(match.to_dict()["summary"], match.summary)

    def test_missing_conditions_are_not_invented(self):
        summary = classify("Ищем удалённого ассистента.").summary
        self.assertIn("Позиция: удалённого ассистента.", summary)
        self.assertNotIn("Не указаны:", summary)
        self.assertNotIn("без опыта", summary)
        self.assertNotIn("₽", summary)

    def test_salary_ceiling_stays_a_ceiling(self):
        summary = classify(BASE.replace("Оклад 50 000 ₽", "Зарплата до 60 000 ₽")).summary
        self.assertIn("оплата до 60 000 ₽/мес.", summary)
        self.assertNotIn("оклад 60 000", summary)

    def test_hourly_pay_is_not_presented_as_monthly(self):
        summary = classify(BASE.replace("Оклад 50 000 ₽", "Оплата 500 ₽ в час")).summary
        self.assertNotIn("месячная оплата", summary)
        self.assertNotIn("500 ₽/мес.", summary)

    def test_experience_qualifier_is_preserved(self):
        summary = classify(BASE.replace("Без опыта", "Опыт от 3 лет")).summary
        self.assertIn("опыт от 3 лет", summary)
        self.assertNotIn("до 3 лет", summary)

    def test_only_first_two_tasks_are_used_and_contacts_removed(self):
        summary = classify(BASE + "\nЗадачи:\n1. Ответы на письма\n2. Обработка заявок\n"
                           "3. Подготовка отчётов\nКонтакты: @recruiter https://example.org").summary
        self.assertIn("Ответы на письма", summary)
        self.assertIn("Обработка заявок", summary)
        self.assertNotIn("отчётов", summary)
        self.assertNotIn("recruiter", summary)
        self.assertNotIn("example.org", summary)

    def test_roundup_descriptions_do_not_mix_jobs(self):
        first = BASE + "\nЗадачи: ответы на письма."
        second = BASE.replace("ассистент", "дизайнер") + "\nЗадачи: создание макетов."
        parts = split_post("#вакансия\n" + first + "\n#вакансия\n" + second)
        summaries = [classify(part).summary for part in parts]
        self.assertIn("письма", summaries[0])
        self.assertNotIn("макетов", summaries[0])
        self.assertIn("макетов", summaries[1])
        self.assertNotIn("письма", summaries[1])

    def test_summary_is_short_even_for_long_posts(self):
        text = BASE + "\nОбязанности:\n" + "Работа с документами " * 100 + "\n" + "Ответы на письма " * 100
        self.assertLessEqual(len(classify(text).summary), MAX_SUMMARY_LENGTH)

    def test_non_vacancy_has_no_description(self):
        self.assertEqual(classify("Ищу работу ассистентом. Моё резюме.").summary, "")

    def test_metadata_and_informative_sections_are_extracted(self):
        text = ("Вакансия: Content Manager\nКомпания: Acme\nЗарплата: 50 000 ₽ в месяц"
                "\nЛокация: Москва\nФормат: удалённо\nЗанятость: частичная занятость"
                "\nОбязанности:\n• Писать статьи для блога\n• Вести контент-план"
                "\nТребования:\n• Грамотный русский\n• Знание WordPress"
                "\nУсловия:\n• Оплачиваемое обучение\n• Гибкий график"
                "\nКонтакты: @recruiter https://example.org")
        description = extract_description(text)
        self.assertEqual(description.title, "Content Manager")
        self.assertEqual(description.company, "Acme")
        self.assertEqual(description.salary, "50 000 ₽ в месяц")
        self.assertEqual(description.location, "Москва")
        self.assertEqual(description.work_format, "удалённо")
        self.assertEqual(description.employment_type, "частичная занятость")
        self.assertEqual(description.requirements, ["Грамотный русский", "Знание WordPress"])
        summary = classify(text).summary
        for value in ["Писать статьи", "контент-план", "Грамотный русский", "WordPress", "Оплачиваемое обучение", "Гибкий график"]:
            self.assertIn(value, summary)
        self.assertNotIn("recruiter", summary)
        self.assertNotIn("example.org", summary)

    def test_english_metadata_and_markdown_headings(self):
        description = extract_description(
            "Position: Online Chat Operator at Acme\nSalary: $500/month\nLocation: Berlin"
            "\nEmployment type: part-time\n**Responsibilities:**\nAnswer customer messages"
            "\n**Requirements:**\nWritten English\n**Benefits:**\nPaid training"
        )
        self.assertEqual(description.title, "Online Chat Operator")
        self.assertEqual(description.company, "Acme")
        self.assertEqual(description.salary, "$500/month")
        self.assertEqual(description.location, "Berlin")
        self.assertEqual(description.employment_type, "part-time")
        self.assertEqual(description.responsibilities, ["Answer customer messages"])
        self.assertEqual(description.requirements, ["Written English"])
        self.assertEqual(description.conditions, ["Paid training"])

    def test_format_does_not_turn_office_or_hybrid_into_remote(self):
        for text, expected in [("Формат: офис", "офис"), ("Гибрид, 2 дня в офисе", "гибрид"),
                               ("Работа в офисе", "офис"), ("Remote, 2 дня в офисе", "гибрид"),
                               ("Удалённо, не гибрид", "удалённо"), ("No remote work", ""),
                               ("Удалённо, без посещения офиса", "удалённо"), ("Не удалённо", "")]:
            with self.subTest(text=text):
                self.assertEqual(extract_description(text).work_format, expected)

    def test_unknown_metadata_is_omitted(self):
        description = extract_description("Вакансия: ассистент")
        for name in ("company", "salary", "location", "work_format", "employment_type"):
            self.assertEqual(getattr(description, name), "")
        self.assertEqual(description.responsibilities, [])
        self.assertEqual(description.requirements, [])

    def test_salary_keeps_hourly_rate_ceiling_and_bonuses(self):
        for salary in ["500 ₽ в час", "до 60 000 ₽", "30 000 ₽ + бонусы до 100 000 ₽"]:
            with self.subTest(salary=salary):
                self.assertEqual(extract_description("Вакансия: ассистент\nОплата: " + salary).salary, salary)

    def test_one_line_sections_are_separated(self):
        description = extract_description("Вакансия: редактор. Задачи: писать статьи. Требования: грамотность. Условия: обучение.")
        self.assertEqual(description.responsibilities, ["писать статьи"])
        self.assertEqual(description.requirements, ["грамотность"])
        self.assertEqual(description.conditions, ["обучение"])

    def test_title_does_not_absorb_work_format_or_salary(self):
        description = extract_description("📌 Ищем в команду оператора чата — удалённо, 50 000 ₽\nЧто делать:\n"
                                          "• Отвечать клиентам в чате\n• Вести историю обращений\n"
                                          "Что важно:\n• Грамотная письменная речь")
        self.assertEqual(description.title, "оператора чата")
        self.assertEqual(description.responsibilities, ["Отвечать клиентам в чате", "Вести историю обращений"])
        self.assertEqual(description.requirements, ["Грамотная письменная речь"])

    def test_preamble_and_experience_are_not_job_titles(self):
        description = extract_description("Привет!\nМы небольшая школа.\nТребуется опыт от года.\n"
                                          "Должность: ассистент руководителя\nЗадачи: вести календарь")
        self.assertEqual(description.title, "ассистент руководителя")

    def test_summary_is_structured_and_contact_prose_is_not_a_duty(self):
        text = BASE + "\nОтвечать на письма клиентов.\nПишите HR на jobs@acme.org для отклика."
        summary = classify(text).summary
        self.assertIn("\nЗадачи: Отвечать на письма клиентов.", summary)
        self.assertNotIn("Пишите HR", summary)
        self.assertNotIn("acme.org", summary)

    def test_role_hashtag_survives_later_generic_recruitment_heading(self):
        text = ("#Удаленка\n#SMM в онлайн-школу\nЗадачи:\nВести контент-план\n"
                "Кого мы ищем на самом деле\nНужен человек с вниманием к деталям.\nУсловия: обучение")
        self.assertEqual(extract_description(text).title, "SMM в онлайн-школу")
        self.assertIn("Позиция: SMM в онлайн-школу.", classify(text).summary)

    def test_headline_wins_over_generic_people_in_the_body(self):
        text = "💬 ОНЛАЙН-МЕНЕДЖЕР / ОПЕРАТОР ЧАТА\nУдалённо\nИщем людей в команду.\nЗадачи: переписка"
        self.assertEqual(extract_description(text).title, "ОНЛАЙН-МЕНЕДЖЕР / ОПЕРАТОР ЧАТА")

    def test_hashtag_role_parenthetical_and_unlabelled_contact_footer(self):
        text = ("(#Удаленка) Требуется #ассистент / Помощник руководителя (гибкий график, можно без опыта)\n"
                "Что нужно будет делать:\nВести переписку\nОбрабатывать сообщения\n"
                "Условия:\nОбучение с наставником\n📝 Пишите @test_hr")
        description = extract_description(text)
        self.assertEqual(description.title, "ассистент / Помощник руководителя")
        self.assertEqual(description.conditions, ["Обучение с наставником"])
        self.assertEqual(description.responsibilities, ["Вести переписку", "Обрабатывать сообщения"])

    def test_pipe_format_is_not_a_company_and_paragraph_has_two_clear_tasks(self):
        description = extract_description("Вакансия:\nСпециалист по возвратам | Удалённо\n"
                                          "Задачи:\nПроверять заявки, сверять данные, обновлять статус. Без поиска клиентов.\n"
                                          "Контакт:\nНаписать HR")
        self.assertEqual(description.title, "Специалист по возвратам")
        self.assertEqual(description.company, "")
        self.assertEqual(description.responsibilities[:2], ["Проверять заявки", "сверять данные"])
        self.assertNotIn("Без поиска клиентов", description.responsibilities)

    def test_markdown_role_label_is_not_repeated_in_title(self):
        self.assertEqual(extract_description("**Вакансия:** ассистент\nЗадачи: вести таблицы").title, "ассистент")
