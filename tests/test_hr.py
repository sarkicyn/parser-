import unittest

from vacancy_bot.dedup import contacts
from vacancy_bot.hr import parse_contact, parse_request


class ContactQueryTests(unittest.TestCase):
    def test_telegram_contact_aliases(self):
        for value in ("@Test_HR", "test_hr", "t.me/Test_HR", "https://t.me/test_hr/", "https://telegram.me/test_hr"):
            with self.subTest(value=value):
                query = parse_contact(value)
                self.assertEqual(query.key, "tg:test_hr")
                self.assertEqual(query.display, "@test_hr")
                self.assertIn(query.key, contacts("HR: " + value if value != "test_hr" else "HR: @test_hr"))

    def test_phone_formats_share_the_storage_contact_key(self):
        for value in ("+7 (999) 123-45-67", "8 999 123 45 67", "79991234567", "9991234567", "+7.999.123.45.67"):
            with self.subTest(value=value):
                query = parse_contact(value)
                self.assertEqual(query.key, "phone:79991234567")
                self.assertEqual(query.display, "+79991234567")
                self.assertIn(query.key, contacts("Контакт: " + value))

    def test_email_and_international_phone(self):
        self.assertEqual(parse_contact("Jobs@Acme.org").key, "email:jobs@acme.org")
        self.assertEqual(parse_contact("+44 7700 900123").key, "phone:447700900123")

    def test_free_text_channel_post_and_multiple_contacts_are_not_queries(self):
        for value in ("Привет", "Вакансия: @test_hr", "https://t.me/test_hr/123", "https://example.com/test_hr",
                      "@test_hr @other_hr", "50000", "@test_hr' OR 1=1 --", "123", ""):
            with self.subTest(value=value):
                self.assertIsNone(parse_contact(value))

    def test_command_pagination_and_history(self):
        for value, archive, page in (("@test_hr", False, 1), ("@test_hr 2", False, 2),
                                     ("@test_hr all", True, 1), ("@test_hr all 3", True, 3),
                                     ("@test_hr ALL 2", True, 2)):
            with self.subTest(value=value):
                query, actual_archive, actual_page = parse_request(value)
                self.assertEqual((query.display, actual_archive, actual_page), ("@test_hr", archive, page))

    def test_spaced_phone_is_not_confused_with_a_page_number(self):
        for value, archive, page in (("+7 999 123 45 67", False, 1), ("+7 999 123 45 67 2", False, 2),
                                     ("8 999 123 45 67 all 2", True, 2)):
            with self.subTest(value=value):
                query, actual_archive, actual_page = parse_request(value)
                self.assertEqual((query.key, actual_archive, actual_page), ("phone:79991234567", archive, page))

    def test_invalid_command_has_an_actionable_error(self):
        for value in ("@test_hr 0", "@test_hr -1", "all", "@test_hr wrong", "https://t.me/test_hr/12"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Пример"):
                parse_request(value)
