#!/usr/bin/env python3
"""clip2md — сохраняет выделенный или скопированный текст в Markdown-файл.

Написан для Apple Shortcuts на macOS: один ярлык с шагом «Запустить сценарий
оболочки» ловит выделение в любом приложении (или буфер обмена, если ничего
не выделено) и кладёт результат в папку заметок под именем вида
«вт.21.08.2026 13.22.md».

Зачем отдельный скрипт, а не три строки shell
---------------------------------------------
Наивный вариант ``cat > file.md`` ломается тремя способами, и каждый из них
пришлось отлаживать на живой системе:

1. Safari и Google AI кладут в буфер обмена сразу несколько представлений
   одного текста. ``clipboard info`` на реальном буфере показал::

       «class HTML», 22065, «class utf8», 5492, string, 0, Unicode text, 6558

   Представление ``string`` — нулевой длины. ``pbpaste`` берёт именно его и
   отдаёт пустоту, поэтому файл создавался, но был пустым. AppleScript-запрос
   ``the clipboard as text`` заставляет macOS отдать текстовое представление.

2. Когда Shortcuts передаёт в скрипт «Веб-страницу Safari», в stdin приходит
   HTML-разметка, а не текст. stdin при этом не пустой, поэтому проверка
   «если пусто — возьми буфер» не срабатывает, и разметка уезжает в заметку.

3. Оболочка внутри песочницы Shortcuts запускается без UTF-8 локали.
   Системный ``date`` возвращает битый первый байт дня недели — в отладочном
   логе имя файла выглядело как ``M-^Aб.22.08.2026 21.03`` вместо ``сб.…``.

Алгоритм
--------
Четыре шага, они же четыре блока в :func:`main`:

1. Прочитать stdin — сырьё от Shortcuts (текст или HTML).
2. Привести к Markdown через :func:`clean`: разметку конвертировать,
   обычный текст только нормализовать.
3. Если после чистки почти ничего не осталось — считать, что выделения не
   было, и взять буфер обмена через :func:`from_clipboard`.
4. Записать файл, имя которого даёт :func:`stamp`.

Использование
-------------
В шаге «Запустить сценарий оболочки» (Shell ``/bin/zsh``, «Передать ввод» —
``в stdin``) достаточно одной строки::

    python3 "$HOME/bin/clip2md.py"

Скрипт печатает путь созданного файла в stdout — он виден в логе Shortcuts.
Зависимостей нет, только стандартная библиотека: внутри песочницы
Shortcuts установка пакетов недоступна.
"""

import argparse
import datetime
import html
import locale
import os
import re
import subprocess
import sys
from html.parser import HTMLParser

__version__ = "1.0.0"

#: Куда складывать заметки по умолчанию. Переопределяется флагом --dir
#: или переменной окружения CLIP2MD_DIR.
DEFAULT_VAULT = "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/VAULT-2"

#: Минимальная длина текста, ниже которой stdin считается пустым.
#: Не ноль: Shortcuts часто передаёт одиночный перевод строки или пробел.
MIN_MEANINGFUL_LEN = 3

#: Теги, вокруг которых нужна пустая строка, иначе Markdown склеит абзацы.
BLOCK_TAGS = frozenset({
    "p", "div", "br", "tr", "section", "article", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "pre", "table",
})

#: Парные служебные теги: их содержимое (CSS, JS) — мусор, а не текст.
#: Только те, у которых есть закрывающий тег, — см. VOID_SKIP_TAGS.
SKIP_TAGS = frozenset({"script", "style", "head", "title"})

#: Void-теги без закрывающей пары. Их нельзя считать через глубину:
#: ``<meta charset="utf-8">`` в начале буфера Safari навсегда включал бы
#: режим пропуска, и заметка выходила бы пустой. Просто игнорируем их.
VOID_SKIP_TAGS = frozenset({"meta", "link", "base", "col", "input", "img"})

#: Уровень заголовка -> префикс Markdown.
HEADING_PREFIXES = {
    "h1": "# ", "h2": "## ", "h3": "### ",
    "h4": "#### ", "h5": "##### ", "h6": "###### ",
}

#: Теги, оборачивающие текст парным маркером Markdown.
INLINE_MARKERS = {
    "strong": "**", "b": "**",
    "em": "*", "i": "*",
    "code": "`",
}

#: Регулярка «в строке есть настоящий HTML-тег». Список тегов закрытый,
#: чтобы текст вида «если a < b» не приняли за разметку.
_TAG_RE = re.compile(
    r"<(?:/?)(?:p|div|span|br|li|ul|ol|h[1-6]|table|tr|td|a|b|i|"
    r"strong|em|code|pre|body|html|meta|font)\b[^>]*>",
    re.IGNORECASE,
)

#: Регулярка «в строке есть HTML-сущность»: бывает текст без тегов,
#: но с &nbsp; или &mdash; внутри.
_ENTITY_RE = re.compile(r"&(?:nbsp|amp|lt|gt|quot|#\d+);")

#: Локали для русского дня недели, в порядке предпочтения.
_TIME_LOCALES = ("ru_RU.UTF-8", "ru_RU")

#: Формат имени файла. Двоеточие в имени недопустимо — Finder показывает
#: его как «/», поэтому между часами и минутами точка.
_STAMP_FORMAT = "%a.%d.%m.%Y %H.%M"


class HTMLToMarkdown(HTMLParser):
    """Потоковый конвертер HTML в Markdown на стандартной библиотеке.

    Дерево документа не строится: парсер идёт по потоку тегов и на каждом
    открытии или закрытии дописывает в накопитель нужный маркер Markdown.
    Для ответа чат-бота или статьи вложенность мелкая, потока достаточно,
    зато нет зависимостей от beautifulsoup4 или html2text.

    Пример:
        >>> parser = HTMLToMarkdown()
        >>> parser.feed("<h2>Заголовок</h2><p>Текст <b>жирный</b></p>")
        >>> parser.close()
        >>> parser.text()
        '## Заголовок\\n\\nТекст **жирный**'
    """

    def __init__(self):
        # convert_charrefs=True: парсер сам разворачивает &nbsp; и &mdash;
        # до вызова handle_data, поэтому сущности не доходят до результата.
        super().__init__(convert_charrefs=True)
        self._chunks = []
        # Счётчик, а не флаг: <style> внутри <head> даёт вложенность, и по
        # одному закрывающему тегу нельзя понять, вышли ли мы наружу.
        self._skip_depth = 0

    @property
    def _skipping(self):
        """True, пока разбор находится внутри <script>, <style> или <head>."""
        return self._skip_depth > 0

    def handle_starttag(self, tag, attrs):
        if tag in VOID_SKIP_TAGS:
            return
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skipping:
            return

        if tag in HEADING_PREFIXES:
            self._chunks.append("\n\n" + HEADING_PREFIXES[tag])
        elif tag == "li":
            self._chunks.append("\n- ")
        elif tag == "br":
            self._chunks.append("\n")
        elif tag in INLINE_MARKERS:
            self._chunks.append(INLINE_MARKERS[tag])
        elif tag in BLOCK_TAGS:
            self._chunks.append("\n\n")

    def handle_endtag(self, tag):
        if tag in VOID_SKIP_TAGS:
            return
        if tag in SKIP_TAGS:
            # max(0, ...) страхует от лишнего закрывающего тега в битой вёрстке.
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skipping:
            return

        # Парный маркер закрываем тем же символом, которым открывали.
        if tag in INLINE_MARKERS:
            self._chunks.append(INLINE_MARKERS[tag])
        elif tag in HEADING_PREFIXES or tag in BLOCK_TAGS:
            self._chunks.append("\n\n")

    def handle_data(self, data):
        if self._skipping:
            return
        self._chunks.append(data)

    def text(self):
        """Собрать накопленные куски в чистый Markdown.

        Порядок правил важен: каждое следующее убирает мусор, оставленный
        предыдущим, — сначала неразрывные пробелы, затем отступы вёрстки,
        потом пробелы по краям строк и только в конце лишние пустые строки.
        """
        text = "".join(self._chunks)
        text = text.replace("\u00a0", " ")          # NBSP ломает вид в Obsidian
        text = re.sub(r"[ \t]+", " ", text)         # схлопнуть отступы вёрстки
        text = re.sub(r" *\n *", "\n", text)        # пробелы по краям строк
        text = re.sub(r"\n{3,}", "\n\n", text)      # максимум одна пустая строка
        return text.strip()


def looks_like_html(raw):
    """Определить, разметка перед нами или обычный текст.

    Проверяется не любой символ ``<``, а известный тег из закрытого списка.
    Иначе текст «если a < b» или «Cmd + I <info>» попал бы в HTML-парсер,
    который молча выбросил бы «тег» вместе с содержимым.

    Args:
        raw: сырая строка со stdin или из буфера обмена.

    Returns:
        True, если строку нужно прогнать через :class:`HTMLToMarkdown`.

    Пример:
        >>> looks_like_html("если a < b")
        False
        >>> looks_like_html("<p>Текст</p>")
        True
        >>> looks_like_html("Cmd&nbsp;+&nbsp;C")
        True
    """
    if not raw:
        return False
    if _TAG_RE.search(raw):
        return True
    return bool(_ENTITY_RE.search(raw))


def clean(raw):
    """Привести сырьё любого вида к Markdown.

    Единая точка входа для шага 2 алгоритма: HTML конвертируется, обычный
    текст только нормализуется. Возврат каретки ``\\r`` приходит из
    AppleScript и без замены превратил бы заметку в одну строку.

    Args:
        raw: сырая строка со stdin или из буфера обмена.

    Returns:
        Markdown-текст без ведущих и замыкающих пробелов.
    """
    if looks_like_html(raw):
        parser = HTMLToMarkdown()
        parser.feed(raw)
        # close() обязателен: без него теряется последний незакрытый кусок.
        parser.close()
        return parser.text()

    text = html.unescape(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def from_clipboard():
    """Достать текст из буфера обмена, пробуя два способа по очереди.

    AppleScript идёт первым: он умеет вытащить текстовое представление из
    «богатого» буфера Safari, где ``pbpaste`` возвращает пустоту (причина
    разобрана в докстринге модуля). ``pbpaste`` остаётся резервом на случай,
    когда доступ к Apple Events не выдан.

    Returns:
        Содержимое буфера или пустая строка, если оба способа не дали текста.
    """
    attempts = (
        ["osascript", "-e", "the clipboard as text"],
        ["pbpaste"],
    )
    for command in attempts:
        try:
            # timeout: osascript подвисает, если не выдан доступ к автоматизации.
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            # Падать здесь нечем помочь — молча пробуем следующий способ.
            continue
        if result.stdout.strip():
            return result.stdout
    return ""


def stamp(moment=None):
    """Собрать метку времени для имени файла: «сб.22.08.2026 21.17».

    Локаль ставится здесь, а не глобально при импорте: оболочка в песочнице
    Shortcuts запускается без UTF-8 локали, и системный ``date`` портит
    первый байт дня недели. Явная ru_RU.UTF-8 в Python даёт корректные
    «сб», «вт», «вс». Если русской локали в системе нет, день недели
    останется английским — это лучше, чем упасть.

    Args:
        moment: момент времени; по умолчанию — сейчас. Параметр нужен тестам.

    Returns:
        Строка вида ``сб.22.08.2026 21.17``, пригодная для имени файла.
    """
    for candidate in _TIME_LOCALES:
        try:
            locale.setlocale(locale.LC_TIME, candidate)
            break
        except locale.Error:
            continue
    return (moment or datetime.datetime.now()).strftime(_STAMP_FORMAT)


def read_stdin():
    """Прочитать stdin, не зависая при запуске из терминала.

    isatty() различает два режима: из Shortcuts stdin — это канал с данными,
    а при ручном запуске в терминале это сам терминал, и ``read()`` ждал бы
    ввода бесконечно.

    Returns:
        Содержимое stdin или пустая строка, если его нет.
    """
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def resolve_vault(explicit=None):
    """Выбрать папку заметок: флаг, затем переменная окружения, затем дефолт.

    Args:
        explicit: значение флага --dir, если он передан.

    Returns:
        Абсолютный путь папки с раскрытым ``~``.
    """
    raw = explicit or os.environ.get("CLIP2MD_DIR") or DEFAULT_VAULT
    return os.path.expanduser(raw)


def save_note(text, directory=None, moment=None):
    """Записать текст в новый Markdown-файл и вернуть путь к нему.

    Args:
        text: готовый Markdown.
        directory: папка заметок; создаётся, если её нет.
        moment: момент времени для имени файла (нужен тестам).

    Returns:
        Абсолютный путь созданного файла.
    """
    now = stamp(moment)
    if not text.strip():
        text = "[Пустая заметка {}]".format(now)

    target = resolve_vault(directory)
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, "{}.md".format(now))
    with open(path, "w", encoding="utf-8") as handle:
        # rstrip плюс явный перевод строки: ровно один \n в конце файла.
        handle.write(text.rstrip() + "\n")
    return path


def parse_args(argv=None):
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="clip2md",
        description="Сохранить выделенный или скопированный текст в .md",
    )
    parser.add_argument(
        "-d", "--dir",
        help="папка для заметок (по умолчанию CLIP2MD_DIR или путь VAULT-2)",
    )
    parser.add_argument(
        "-V", "--version", action="version",
        version="clip2md {}".format(__version__),
    )
    return parser.parse_args(argv)


def main(argv=None):
    """Пройти четыре шага алгоритма и напечатать путь созданного файла."""
    args = parse_args(argv)

    text = clean(read_stdin())

    if len(text.strip()) < MIN_MEANINGFUL_LEN:
        text = clean(from_clipboard())

    print(save_note(text, args.dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
