#!/usr/bin/env python3
"""Тесты clip2md на unittest из стандартной библиотеки.

Запуск::

    python3 -m unittest discover -v
"""

import datetime
import os
import tempfile
import unittest

import clip2md


class TestLooksLikeHTML(unittest.TestCase):
    """Эвристика «разметка или текст» — самое хрупкое место, проверяем плотно."""

    def test_real_tag_detected(self):
        self.assertTrue(clip2md.looks_like_html("<p>Текст</p>"))

    def test_entity_without_tags_detected(self):
        self.assertTrue(clip2md.looks_like_html("Cmd&nbsp;+&nbsp;C"))

    def test_math_less_than_is_not_html(self):
        # Регресс: раньше такой текст уходил в HTML-парсер и терял часть строки.
        self.assertFalse(clip2md.looks_like_html("если a < b то ок"))

    def test_angle_placeholder_is_not_html(self):
        self.assertFalse(clip2md.looks_like_html("Cmd + I <info>"))

    def test_empty_string(self):
        self.assertFalse(clip2md.looks_like_html(""))


class TestClean(unittest.TestCase):
    """Конвертация HTML в Markdown и нормализация обычного текста."""

    def test_headings_lists_and_inline(self):
        raw = (
            "<h2>Заголовок</h2><p>Текст с <strong>жирным</strong> "
            "и <em>курсивом</em>.</p><ul><li>раз</li><li>два</li></ul>"
        )
        expected = (
            "## Заголовок\n\n"
            "Текст с **жирным** и *курсивом*.\n\n"
            "- раз\n- два"
        )
        self.assertEqual(clip2md.clean(raw), expected)

    def test_meta_prefix_does_not_swallow_body(self):
        # Регресс: <meta> — void-тег, счётчик пропуска на нём включался
        # навсегда, и заметка выходила пустой.
        raw = '<meta charset="utf-8"><p>Тело заметки</p>'
        self.assertEqual(clip2md.clean(raw), "Тело заметки")

    def test_style_content_dropped(self):
        raw = "<style>.x{color:red}</style><p>Только текст</p>"
        self.assertEqual(clip2md.clean(raw), "Только текст")

    def test_entities_unescaped(self):
        raw = "<p>Cmd + Space &mdash; превью &lt;file&gt;</p>"
        self.assertEqual(clip2md.clean(raw), "Cmd + Space — превью <file>")

    def test_nbsp_becomes_space(self):
        self.assertEqual(clip2md.clean("<p>a&nbsp;b</p>"), "a b")

    def test_carriage_returns_normalised(self):
        # AppleScript отдаёт \r; без замены Obsidian показал бы одну строку.
        self.assertEqual(
            clip2md.clean("первая\rвторая\r\nтретья"),
            "первая\nвторая\nтретья",
        )

    def test_plain_text_passthrough(self):
        self.assertEqual(clip2md.clean("  просто текст  "), "просто текст")


class TestStamp(unittest.TestCase):
    """Имя файла: формат и запрещённые символы."""

    def setUp(self):
        self.moment = datetime.datetime(2026, 8, 21, 13, 22)

    def test_format_shape(self):
        result = clip2md.stamp(self.moment)
        self.assertTrue(result.endswith(".21.08.2026 13.22"), result)

    def test_no_colon_in_name(self):
        # Двоеточие Finder показывает как «/» — в имени его быть не должно.
        self.assertNotIn(":", clip2md.stamp(self.moment))


class TestSaveNote(unittest.TestCase):
    """Запись файла: путь, содержимое, поведение на пустом входе."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.moment = datetime.datetime(2026, 8, 21, 13, 22)

    def test_writes_content_with_single_trailing_newline(self):
        path = clip2md.save_note("## Тест\n\nтело", self.tmp, self.moment)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "## Тест\n\nтело\n")

    def test_creates_missing_directory(self):
        nested = os.path.join(self.tmp, "a", "b")
        path = clip2md.save_note("текст", nested, self.moment)
        self.assertTrue(os.path.isfile(path))

    def test_blank_input_gets_placeholder(self):
        path = clip2md.save_note("   \n  ", self.tmp, self.moment)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("Пустая заметка", handle.read())


class TestResolveVault(unittest.TestCase):
    """Приоритет источников папки: флаг > переменная окружения > дефолт."""

    def tearDown(self):
        os.environ.pop("CLIP2MD_DIR", None)

    def test_explicit_wins(self):
        os.environ["CLIP2MD_DIR"] = "/tmp/from-env"
        self.assertEqual(clip2md.resolve_vault("/tmp/explicit"), "/tmp/explicit")

    def test_env_used_when_no_flag(self):
        os.environ["CLIP2MD_DIR"] = "/tmp/from-env"
        self.assertEqual(clip2md.resolve_vault(), "/tmp/from-env")

    def test_tilde_expanded(self):
        self.assertTrue(clip2md.resolve_vault("~/notes").startswith(os.path.expanduser("~")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
