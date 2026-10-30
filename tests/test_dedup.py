import unittest

from vacancy_bot.dedup import Identity, contacts, content, same_vacancy


POST = ("Вакансия: ассистент онлайн-школы\nКомпания: Acme\n"
        "Удалённо, 4 часа в день. Оклад 50 000 ₽. Без опыта.\n"
        "Задачи: вести таблицы учеников; отвечать на письма клиентов.\n"
        "Требования: грамотный русский; Excel.\nКонтакты: @school_hr")


class DedupTests(unittest.TestCase):
    def same(self, left, right):
        return same_vacancy(Identity.from_text(left), Identity.from_text(right))

    def test_contact_formats_are_normalized(self):
        left = contacts("HR: @School_HR, Jobs@Acme.org, +7 (999) 123-45-67")
        right = contacts("Контакты: https://t.me/school_hr, jobs@acme.org, 8 999 123 45 67")
        self.assertEqual(left, right)
        self.assertEqual(len(left), 3)

    def test_channel_footer_and_post_links_are_not_hr_contacts(self):
        self.assertEqual(contacts("Подписывайтесь: @channel_jobs\nhttps://t.me/channel_jobs/123"), frozenset())
        self.assertEqual(contacts("jobs@acme.org"), frozenset({"email:jobs@acme.org"}))

    def test_repost_with_new_format_contact_link_and_footer(self):
        repost = POST.replace("@school_hr", "https://t.me/SCHOOL_HR").replace("50 000", "50000")
        repost = "#вакансия\n📌 " + repost.upper() + "\nПодписывайтесь на @channel_jobs"
        self.assertTrue(self.same(POST, repost))

    def test_minor_description_edits_and_reordered_duties(self):
        repost = POST.replace("вести таблицы учеников; отвечать на письма клиентов",
                              "отвечать на письма клиентов; вести таблицы учеников и расписание")
        self.assertTrue(self.same(POST, repost))

    def test_one_hr_can_recruit_for_different_roles(self):
        self.assertFalse(self.same(POST, POST.replace("ассистент онлайн-школы", "дизайнер интерфейсов")))
        self.assertFalse(self.same(POST, POST.replace("Компания: Acme", "Компания: Other")))
        self.assertFalse(self.same(POST + "\nЛокация: Москва", POST + "\nЛокация: Казань"))

    def test_different_hashtag_roles_do_not_lose_identity(self):
        template = "#вакансия #{role}\nУдалённо, 4 часа в день. Оклад 50 000 ₽. Без опыта.\nКонтакты: @test_hr"
        self.assertFalse(self.same(template.format(role="ассистент"), template.format(role="дизайнер")))

    def test_same_title_and_hr_with_different_duties_stay_separate(self):
        other = POST.replace("вести таблицы учеников; отвечать на письма клиентов",
                             "готовить презентации руководителя; организовывать поездки сотрудников")
        self.assertFalse(self.same(POST, other))

    def test_different_recruiters_with_template_text_stay_separate(self):
        self.assertFalse(self.same(POST, POST.replace("@school_hr", "@other_hr")))

    def test_missing_contacts_need_near_verbatim_description(self):
        plain = POST.replace("Контакты: @school_hr", "")
        self.assertTrue(self.same(plain, plain.upper() + "\nПодписывайтесь на @channel_jobs"))
        self.assertFalse(self.same("Вакансия: ассистент. Удалённо.", "Вакансия: ассистент. Удалённо. Excel."))

    def test_salary_range_is_not_removed_as_a_phone(self):
        self.assertIn("50000 70000", content("Оклад 50 000–70 000 ₽"))
        self.assertEqual(contacts("Оклад 50 000-70 000 ₽"), frozenset())

    def test_seniority_and_numbered_positions_stay_separate(self):
        self.assertFalse(self.same(POST.replace("ассистент онлайн-школы", "Senior Python developer"),
                                   POST.replace("ассистент онлайн-школы", "Junior Python developer")))
        self.assertFalse(self.same(POST.replace("ассистент онлайн-школы", "Copywriter 1"),
                                   POST.replace("ассистент онлайн-школы", "Copywriter 2")))
