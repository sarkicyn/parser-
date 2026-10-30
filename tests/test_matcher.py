import unittest
from dataclasses import replace

from vacancy_bot.matcher import classify, extract, split_post
from vacancy_bot.profile import Profile, update_profile


BASE = "Вакансия: ассистент. Удалённо, 4 часа в день. Оклад 50 000 ₽ в месяц. Без опыта."


class MatchingTests(unittest.TestCase):
    def test_complete_match(self):
        result = classify(BASE)
        self.assertTrue(result.eligible)
        self.assertEqual(result.score, 100)
        self.assertEqual(result.unknowns, [])

    def test_experience_rejections(self):
        for experience in ["от 4 лет", "3–5 лет", "более 3 лет", "свыше трех лет",
                           "не менее 5 лет", "5+ years", "от 40 месяцев"]:
            with self.subTest(experience=experience):
                result = classify(BASE.replace("Без опыта", "Опыт " + experience))
                self.assertFalse(result.eligible, result.to_dict())

    def test_experience_allowed(self):
        for experience in ["от 1 до 3 лет", "2 года", "до 3 лет", "не более 3 лет", "6 месяцев"]:
            with self.subTest(experience=experience):
                result = classify(BASE.replace("Без опыта", "Опыт " + experience))
                self.assertTrue(result.eligible)
                self.assertLessEqual(result.facts.experience_max, 3)

    def test_reversed_english_experience(self):
        self.assertFalse(classify(BASE.replace("Без опыта", "5+ years of experience")).eligible)

    def test_company_age_is_not_required_experience(self):
        result = classify(BASE.replace("Без опыта", "Без опыта, компания на рынке 5 лет"))
        self.assertTrue(result.eligible, result.to_dict())

    def test_optional_experience_does_not_reject(self):
        result = classify(BASE.replace("Без опыта", "Опыт 5 лет будет преимуществом"))
        self.assertTrue(result.eligible)
        self.assertIsNone(result.facts.experience_max)

    def test_overwork_rejected(self):
        for hours in ["8 часов", "4–8 часов", "более 6 часов", "полный рабочий день"]:
            with self.subTest(hours=hours):
                self.assertFalse(classify(BASE.replace("4 часа в день", hours)).eligible)

    def test_daily_hour_ranges(self):
        result = classify(BASE.replace("4 часа", "от 2 до 6 часов"))
        self.assertEqual((result.facts.hours_min, result.facts.hours_max), (2, 6))

    def test_clock_schedule(self):
        result = classify(BASE.replace("4 часа в день", "график с 10:00 до 18:00"))
        self.assertFalse(result.eligible)

    def test_weekly_hours_not_daily(self):
        result = extract(BASE.replace("4 часа в день", "20 часов в неделю"))
        self.assertIsNone(result.hours_max)

    def test_response_time_not_working_hours(self):
        result = extract(BASE.replace("4 часа в день", "частичная занятость, ответ в течение 2 часов"))
        self.assertIsNone(result.hours_max)

    def test_remote_negations(self):
        for mode in ["удалёнки нет", "не удалённо", "гибрид", "работа в офисе", "формат: офис", "on-site"]:
            with self.subTest(mode=mode):
                self.assertFalse(classify(BASE.replace("Удалённо", mode)).eligible)

    def test_no_office_visits_is_remote(self):
        result = classify(BASE.replace("Удалённо", "Удалённо, без посещения офиса"))
        self.assertTrue(result.eligible)

    def test_salary_range_shared_suffix(self):
        result = extract(BASE.replace("50 000 ₽", "40–60 тыс. руб."))
        self.assertEqual((result.salary_min, result.salary_max), (40000, 60000))

    def test_salary_dot_thousands(self):
        self.assertEqual(extract(BASE.replace("50 000", "50.000")).salary_min, 50000)

    def test_fixed_low_salary_not_rescued_by_bonus(self):
        for pay in ["Оклад 30 000 ₽ + бонусы до 100 000 ₽ в месяц",
                    "Оклад 30 000 + премия до 100 000 ₽ в месяц"]:
            with self.subTest(pay=pay):
                result = classify(BASE.replace("Оклад 50 000 ₽ в месяц", pay))
                self.assertFalse(result.eligible, result.to_dict())

    def test_salary_below_minimum(self):
        for salary in ["35 000", "39 999"]:
            with self.subTest(salary=salary):
                self.assertFalse(classify(BASE.replace("50 000", salary)).eligible)

    def test_salary_has_no_upper_limit_or_score_penalty(self):
        for salary in ["40 000", "60 000", "70 000", "150 000", "1 000 000"]:
            with self.subTest(salary=salary):
                result = classify(BASE.replace("50 000", salary), replace(Profile(), min_score=100))
                self.assertTrue(result.eligible, result.to_dict())
                self.assertEqual(result.score, 100)

    def test_high_salary_does_not_override_other_conditions(self):
        high_salary = BASE.replace("50 000", "150 000")
        for text in [high_salary.replace("4 часа", "8 часов"),
                     high_salary.replace("Без опыта", "Опыт 5 лет"),
                     high_salary.replace("Удалённо", "Работа в офисе")]:
            with self.subTest(text=text):
                self.assertFalse(classify(text).eligible)

    def test_ceiling_is_not_guaranteed_salary(self):
        result = classify(BASE.replace("Оклад 50 000 ₽", "Зарплата до 60 000 ₽"))
        self.assertTrue(result.eligible)
        self.assertEqual(result.verdict, "Нужно уточнить")
        self.assertIsNone(result.facts.salary_min)

    def test_nonmonthly_salary_is_unknown(self):
        result = classify(BASE.replace("Оклад 50 000 ₽ в месяц", "Оплата 500 ₽ в час"))
        self.assertFalse(result.facts.salary_monthly)
        self.assertIn("Нет гарантированной месячной суммы в рублях", result.unknowns)

    def test_foreign_currency_never_assumed_rubles(self):
        result = classify(BASE.replace("50 000 ₽", "50 000 USD"))
        self.assertIn("Нет гарантированной месячной суммы в рублях", result.unknowns)

    def test_commission_only_rejected(self):
        self.assertFalse(classify(BASE.replace("Оклад 50 000 ₽ в месяц", "Только процент, без оклада")).eligible)

    def test_fixed_plus_commission_allowed(self):
        self.assertTrue(classify(BASE.replace("Оклад 50 000 ₽", "Оклад 50 000 ₽ + процент")).eligible)

    def test_self_advertisements_rejected(self):
        self.assertFalse(classify(BASE.replace("Вакансия: ассистент", "Ищу работу ассистентом")).eligible)

    def test_unknowns_can_be_hidden(self):
        text = "Ищем удалённого ассистента. Частичная занятость."
        self.assertTrue(classify(text).eligible)
        self.assertFalse(classify(text, replace(Profile(), include_unknown=False)).eligible)

    def test_score_threshold_is_inclusive(self):
        profile = replace(Profile(), keywords=["Excel", "почта", "чат", "таблицы", "поддержка"])
        for terms, score, eligible in [("Excel, почта, чат", 59, False),
                                       ("Excel, почта, чат, таблицы", 60, True)]:
            with self.subTest(score=score):
                result = classify("Ищем удалённого ассистента. " + terms, profile)
                self.assertEqual(result.score, score)
                self.assertEqual(result.eligible, eligible)

    def test_lower_profile_cannot_bypass_score_floor(self):
        profile = replace(Profile(), min_score=55, keywords=["Excel"])
        result = classify("Ищем удалённого ассистента.", profile)
        self.assertEqual(result.score, 56)
        self.assertFalse(result.eligible)
        self.assertEqual(result.verdict, "Ниже порога")

    def test_higher_score_threshold_is_preserved(self):
        profile = update_profile(Profile(), "score 70")
        result = classify("Ищем удалённого ассистента.", profile)
        self.assertEqual(result.score, 61)
        self.assertFalse(result.eligible)

    def test_configurable_criteria(self):
        profile = update_profile(Profile(), "hours 2 8")
        self.assertTrue(classify(BASE.replace("4 часа", "8 часов"), profile).eligible)
        profile = update_profile(profile, "exclude ассистент")
        self.assertFalse(classify(BASE, profile).eligible)

    def test_keywords_use_word_boundaries(self):
        profile = replace(Profile(), exclude_keywords=["продажи"])
        self.assertTrue(classify(BASE + " Без автопродажи.", profile).eligible)
        self.assertFalse(classify(BASE + " Обязанности: продажи.", profile).eligible)

    def test_roundup_keeps_requirements_separate(self):
        post = "#вакансия\n" + BASE + "\n\n#вакансия\n" + BASE.replace("Без опыта", "Опыт 5 лет")
        parts = split_post(post)
        self.assertEqual(len(parts), 2)
        self.assertTrue(classify(parts[0]).eligible)
        self.assertFalse(classify(parts[1]).eligible)

    def test_single_post_not_split_at_hashtag_footer(self):
        self.assertEqual(len(split_post(BASE + "\n#вакансия #удалёнка")), 1)

    def test_invalid_settings_are_rejected(self):
        for arguments in ["hours 8 2", "salary -1", "salary 40 60", "experience -1", "score 101", "score 59",
                          "remote maybe", "hours nan 6", "salary inf", "salary nan"]:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    update_profile(Profile(), arguments)


if __name__ == "__main__":
    unittest.main()
