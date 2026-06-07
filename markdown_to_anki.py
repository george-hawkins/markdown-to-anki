import re
import json
import copy
import sys
import urllib.request
import urllib.parse
import configparser
import collections
import markdown
import base64
import argparse
import html
import time
import socket
import logging
import hashlib
from pathlib import Path
from typing import Any

from anki_connect import ANKI_PORT, AnkiConnect
from retrieve_deck import diff_deck

logging.basicConfig(
    filename="markdown_to_anki_log.log",
    level=logging.DEBUG,
    format="%(asctime)s:::%(levelname)s:::%(funcName)s:::%(message)s"
)

MEDIA = {}

BACKUPS = "backups"

ID_PREFIX = "ID: "
TAG_PREFIX = "Tags: "
TAG_SEP = " "
Note_and_id = collections.namedtuple("Note_and_id", ["note", "id"])
NOTE_DICT_TEMPLATE: dict[str, Any] = {
    "deckName": "",
    "modelName": "",
    "fields": {},
    "options": {
        "allowDuplicate": False,
        "duplicateScope": "deck"
    },
    "tags": ["Markdown_to_Anki"],
    # ^So that you can see what was added automatically.
    "audio": []
}

CONFIG_PATH = Path(__file__).resolve().parent / "markdown_to_anki_config.ini"
CONFIG_DATA = {}

DATA_PATH = Path(__file__).resolve().parent / "markdown_to_anki_data.json"

md_parser = markdown.Markdown(
    extensions=[
        "fenced_code",
        "footnotes",
        "md_in_html",
        "tables",
        "nl2br",
        "sane_lists"
    ]
)

ANKI_CLOZE_REGEXP = re.compile(r"{{c\d+::[\s\S]+?}}")


def has_clozes(text):
    """Checks whether text actually has cloze deletions."""
    return bool(ANKI_CLOZE_REGEXP.search(text))


def note_has_clozes(note):
    """Checks whether a note has cloze deletions in any of its fields."""
    return any(has_clozes(field) for field in note["fields"].values())


def write_safe(filename, contents):
    """
    Write contents to filename while keeping a backup.

    If write fails, a backup 'filename.bak' will still exist.
    """
    p = Path(filename)
    tmp = p.with_name(p.name + ".tmp")
    bak = p.with_name(p.name + ".bak")
    tmp.write_text(contents, encoding="utf_8")
    p.rename(bak)
    tmp.rename(p)
    if p.read_text(encoding="utf_8") == contents:
        bak.unlink()


def string_insert(string, position_inserts):
    """
    Insert strings in position_inserts into string, at indices.

    position_inserts will look like:
    [(0, "hi"), (3, "hello"), (5, "beep")]
    """
    offset = 0
    position_inserts = sorted(position_inserts)
    for position, insert_str in position_inserts:
        string = "".join(
            [
                string[:position + offset],
                insert_str,
                string[position + offset:]
            ]
        )
        offset += len(insert_str)
    return string


def file_encode(filepath):
    """Encode the file as base 64."""
    with open(filepath, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def spans(pattern, string):
    """Return a list of span-tuples for matches of pattern in string."""
    return [match.span() for match in pattern.finditer(string)]


def contained_in(span, span_list):
    """Return whether span is contained in span_list (+- 1 leeway)"""
    return any(
        span[0] >= start - 1 and span[1] <= end + 1
        for start, end in span_list
    )


def find_ignore(pattern, string, ignore_spans):
    """Yield all matches for pattern in string not in ignore_spans."""
    return (
        match
        for match in pattern.finditer(string)
        if not contained_in(match.span(), ignore_spans)
    )


def wait_for_port(port, host="localhost", timeout=5.0):
    """Wait until a port starts accepting TCP connections.
    Args:
        port (int): Port number.
        host (str): Host address on which the port should exist.
        timeout (float): In seconds. How long to wait before raising errors.
    Raises:
        TimeoutError: The port isn't accepting connection after time specified
        in `timeout`.
    """
    start_time = time.perf_counter()
    while True:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                break
        except OSError as ex:
            time.sleep(0.01)
            if time.perf_counter() - start_time >= timeout:
                raise TimeoutError(
                    f"Waited too long for the port {port} on host {host} to "
                    "start accepting connections."
                ) from ex


def main():
    """Main functionality of script."""
    if not CONFIG_PATH.exists():
        Config.update_config()
    App()


class DuplicateNoteIDError(Exception):
    """Raised when the same note ID appears more than once in the source."""


class UnknownNoteTypeError(Exception):
    """Raised when a note names a type that doesn't exist in Anki."""


class FormatConverter:
    """Converting Markdown formatting to Anki formatting."""

    # $...$ not preceded by $; body starts non-space/non-$, ends non-space.
    MKD_INLINE_MATH_REGEXP = re.compile(
        r"(?<!\$)\$(?=[^\s$])[\s\S]*?\S\$"
    )
    MKD_BLOCK_MATH_REGEXP = re.compile(r"\$\$[\s\S]*?\$\$")

    MKD_INLINE_CODE_REGEXP = re.compile(
        r"(?<!`)`(?=[^`])[\s\S]*?`"
    )
    MKD_BLOCK_CODE_REGEXP = re.compile(
        r"```[\s\S]*?```"
    )

    ANKI_INLINE_START = r"\("
    ANKI_INLINE_END = r"\)"

    ANKI_BLOCK_START = r"\["
    ANKI_BLOCK_END = r"\]"

    ANKI_MATH_REGEXP = re.compile(r"\\\[[\s\S]*?\\]|\\\([\s\S]*?\\\)")

    MATH_REPLACE = "MKDTOANKIMATH"
    INLINE_CODE_REPLACE = "MKDTOANKICODEINLINE"
    BLOCK_CODE_REPLACE = "MKDTOANKICODEBLOCK"

    IMAGE_REGEXP = re.compile(r'<img alt=".*?" src="(.*?)"')
    SOUND_REGEXP = re.compile(r"\[sound:(.+)]")
    # {...} not touching another brace; optional "c?N:" / "c?N|" number prefix
    # (group 1); body (group 2) may span single newlines but not blank lines.
    CLOZE_REGEXP = re.compile(
        r"(?<!{){(?:c?(\d+)[:|])?(?!{)((?:[^\n]\n?)+?)(?<!})}(?!})"
    )
    URL_REGEXP = re.compile(r"https?://")

    PARA_OPEN = "<p>"
    PARA_CLOSE = "</p>"

    CLOZE_UNSET_NUM = 1

    @staticmethod
    def format_note_with_frozen_fields(note, frozen_fields_dict):
        for field in note["fields"].keys():
            note["fields"][field] += frozen_fields_dict[
                note["modelName"]
            ][field]

    @staticmethod
    def inline_anki_repl(match_object):
        """Get replacement string for Markdown-formatted inline math."""
        found_string = match_object.group(0)
        # Strip Markdown formatting by removing first and last characters
        found_string = found_string[1:-1]
        # Add Anki formatting
        result = FormatConverter.ANKI_INLINE_START + found_string
        result += FormatConverter.ANKI_INLINE_END
        return result

    @staticmethod
    def block_anki_repl(match_object):
        """Get replacement string for Markdown-formatted block math."""
        found_string = match_object.group(0)
        # Strip Markdown formatting by removing first two and last two chars
        found_string = found_string[2:-2]
        # Add Anki formatting
        result = FormatConverter.ANKI_BLOCK_START + found_string
        result += FormatConverter.ANKI_BLOCK_END
        return result

    @staticmethod
    def markdown_to_anki_math(note_text):
        """Convert Markdown-formatted math to Anki-formatted math."""
        return FormatConverter.MKD_INLINE_MATH_REGEXP.sub(
            FormatConverter.inline_anki_repl,
            FormatConverter.MKD_BLOCK_MATH_REGEXP.sub(
                FormatConverter.block_anki_repl, note_text
            )
        )

    @staticmethod
    def cloze_repl(match):
        cloze_id, content = match.group(1), match.group(2)
        if cloze_id is None:
            result = f"{{{{c{FormatConverter.CLOZE_UNSET_NUM}::{content}}}}}"
            FormatConverter.CLOZE_UNSET_NUM += 1
            return result
        else:
            return f"{{{{c{cloze_id}::{content}}}}}"

    @staticmethod
    def curly_to_cloze(text):
        """Change text in curly brackets to Anki-formatted cloze."""
        text = FormatConverter.CLOZE_REGEXP.sub(
            FormatConverter.cloze_repl,
            text
        )
        FormatConverter.CLOZE_UNSET_NUM = 1
        return text

    @staticmethod
    def markdown_parse(text):
        """Apply Markdown conversions to text."""
        text = md_parser.reset().convert(text)
        return text

    @staticmethod
    def is_url(text):
        """Check whether text looks like a url."""
        return bool(
            FormatConverter.URL_REGEXP.match(text)
        )

    @staticmethod
    def get_images(html_text, base_dir: Path = Path(".")):
        """Get all the images that need to be added."""
        for match in FormatConverter.IMAGE_REGEXP.finditer(html_text):
            path = match.group(1)
            if FormatConverter.is_url(path):
                continue  # Skips over images web-hosted.
            path = urllib.parse.unquote(path)
            filename = Path(path).name
            if filename not in App.ADDED_MEDIA and filename not in MEDIA:
                MEDIA[filename] = file_encode(base_dir / path)

    @staticmethod
    def get_audio(html_text, base_dir: Path = Path(".")):
        """Get all the audio that needs to be added."""
        for match in FormatConverter.SOUND_REGEXP.finditer(html_text):
            path = match.group(1)
            filename = Path(path).name
            if filename not in App.ADDED_MEDIA and filename not in MEDIA:
                MEDIA[filename] = file_encode(base_dir / path)

    @staticmethod
    def path_to_filename(match_object):
        """Replace the src in match_object appropriately."""
        found_string, found_path = match_object.group(0), match_object.group(1)
        if FormatConverter.is_url(found_path):
            return found_string  # So urls should not be altered.
        found_string = found_string.replace(
            found_path, Path(urllib.parse.unquote(found_path)).name
        )
        return found_string

    @staticmethod
    def fix_image_src(html_text):
        """Fix the src of the images so that it's relative to Anki."""
        return FormatConverter.IMAGE_REGEXP.sub(
            FormatConverter.path_to_filename,
            html_text
        )

    @staticmethod
    def fix_audio_src(html_text):
        """Fix the audio filenames so that it's relative to Anki."""
        return FormatConverter.SOUND_REGEXP.sub(
            FormatConverter.path_to_filename,
            html_text
        )

    @staticmethod
    def format(note_text, cloze=False, base_dir: Path = Path(".")):
        """Apply all format conversions to note_text."""
        note_text = FormatConverter.markdown_to_anki_math(note_text)
        # Extract the parts that are anki math
        math_matches = [
            math_match.group(0)
            for math_match in FormatConverter.ANKI_MATH_REGEXP.finditer(
                note_text
            )
        ]
        # Replace them to be later added back, so they don't interfere
        # with Markdown parsing
        note_text = FormatConverter.ANKI_MATH_REGEXP.sub(
            FormatConverter.MATH_REPLACE, note_text
        )
        # Now same with code!
        inline_code_matches = [
            code_match.group(0)
            for code_match in FormatConverter.MKD_INLINE_CODE_REGEXP.finditer(
                note_text
            )
        ]
        note_text = FormatConverter.MKD_INLINE_CODE_REGEXP.sub(
            FormatConverter.INLINE_CODE_REPLACE, note_text
        )
        block_code_matches = [
            code_match.group(0)
            for code_match in FormatConverter.MKD_BLOCK_CODE_REGEXP.finditer(
                note_text
            )
        ]
        note_text = FormatConverter.MKD_BLOCK_CODE_REGEXP.sub(
            FormatConverter.BLOCK_CODE_REPLACE, note_text
        )
        if cloze:
            note_text = FormatConverter.curly_to_cloze(note_text)
        for code_match in inline_code_matches:
            note_text = note_text.replace(
                FormatConverter.INLINE_CODE_REPLACE,
                code_match,
                1
            )
        for code_match in block_code_matches:
            note_text = note_text.replace(
                FormatConverter.BLOCK_CODE_REPLACE,
                code_match,
                1
            )
        note_text = FormatConverter.markdown_parse(note_text)
        # Add back the parts that are anki math
        for math_match in math_matches:
            note_text = note_text.replace(
                FormatConverter.MATH_REPLACE,
                html.escape(math_match),
                1
            )
        FormatConverter.get_images(note_text, base_dir)
        FormatConverter.get_audio(note_text, base_dir)
        note_text = FormatConverter.fix_image_src(note_text)
        note_text = FormatConverter.fix_audio_src(note_text)
        note_text = note_text.strip()
        # Remove unnecessary paragraph tag
        if note_text.startswith(
            FormatConverter.PARA_OPEN
        ) and note_text.endswith(
            FormatConverter.PARA_CLOSE
        ):
            note_text = note_text[len(FormatConverter.PARA_OPEN):]
            note_text = note_text[:-len(FormatConverter.PARA_CLOSE)]
        return note_text


def field_names_for(note_type) -> list:
    """Return the Anki field names for note_type, or raise if it's unknown."""
    try:
        return App.FIELDS_DICT[note_type]
    except KeyError:
        raise UnknownNoteTypeError(
            f"Unknown note type {note_type!r}. Your Anki note types are: "
            f"{sorted(App.FIELDS_DICT)}."
        ) from None


def build_note_dict(note, deck, frozen_fields_dict=None) -> dict:
    """Build the AnkiConnect note dict shared by all note types.

    `note` is any object exposing note_type, fields and tags.
    """
    # deepcopy (not .copy()) so each note gets its own "options"/"audio";
    # a shallow copy would alias the module-level template's nested mutables.
    template = copy.deepcopy(NOTE_DICT_TEMPLATE)
    template["modelName"] = note.note_type
    template["fields"] = note.fields
    if frozen_fields_dict:
        FormatConverter.format_note_with_frozen_fields(
            template, frozen_fields_dict
        )
    template["tags"] = template["tags"] + note.tags
    template["deckName"] = deck
    return template


class Note:
    """Manages parsing notes into a dictionary formatted for AnkiConnect.

    Input must be the note text.
    Does NOT deal with finding the note in the file.
    """

    ID_REGEXP = re.compile(
        r"(?:<!--)?" + ID_PREFIX + r"(\d+)"
    )

    def __init__(self, note_text, base_dir: Path = Path(".")):
        """Set up useful variables."""
        self.base_dir = base_dir
        self.text = note_text
        self.lines = self.text.splitlines()
        id_match = Note.ID_REGEXP.match(self.lines[-1])
        if id_match:
            self.identifier = int(id_match.group(1))
            self.lines.pop()  # Remove the identifier line for easier parsing.
        else:
            self.identifier = None
        if self.lines[-1].startswith(TAG_PREFIX):
            self.tags = self.lines.pop()[len(TAG_PREFIX):].split(
                TAG_SEP
            )
        else:
            self.tags = []
        self.note_type = self.lines[0]
        self.field_names = field_names_for(self.note_type)
        self.current_field = self.field_names[0]

    def field_from_line(self, line):
        """From a given line, determine the next field to add text into.

        Then, return the stripped line, and the field."""
        for field in self.field_names:
            if line.startswith(field + ":"):
                return line[len(field + ":"):], field
        return line, self.current_field

    @property
    def fields(self):
        """Get the fields of the note into a dictionary."""
        fields = {field: "" for field in self.field_names}
        for line in self.lines[1:]:
            line, self.current_field = self.field_from_line(line)
            fields[self.current_field] += line + "\n"
        fields = {
            key: FormatConverter.format(
                value.strip(),
                cloze=(
                    "Cloze" in self.note_type
                    and CONFIG_DATA["CurlyCloze"]
                ),
                base_dir=self.base_dir
            )
            for key, value in fields.items()
        }
        return {key: value.strip() for key, value in fields.items()}

    def parse(self, deck, frozen_fields_dict=None) -> Note_and_id:
        """Get a properly formatted dictionary of the note."""
        template = build_note_dict(self, deck, frozen_fields_dict)
        return Note_and_id(note=template, id=self.identifier)


class InlineNote(Note):

    # ID_REGEXP is inherited from Note (identical pattern).
    TAG_REGEXP = re.compile(TAG_PREFIX + r"(.*)")
    TYPE_REGEXP = re.compile(r"\[(.*?)]")  # So e.g. [Basic]

    # noinspection PyMissingConstructor
    def __init__(self, note_text, base_dir: Path = Path(".")):
        self.base_dir = base_dir
        self.text = note_text.strip()
        id_match = InlineNote.ID_REGEXP.search(self.text)
        if id_match is not None:
            self.identifier = int(id_match.group(1))
            self.text = self.text[:id_match.start()]  # Removes identifier
        else:
            self.identifier = None
        tags_match = InlineNote.TAG_REGEXP.search(self.text)
        if tags_match is not None:
            self.tags = tags_match.group(1).split(TAG_SEP)
            self.text = self.text[:tags_match.start()]
        else:
            self.tags = []
        type_match = InlineNote.TYPE_REGEXP.search(self.text)
        if type_match is None:
            raise ValueError(
                f"Inline note is missing a [note type]: {note_text!r}"
            )
        self.note_type = type_match.group(1)
        self.text = self.text[type_match.end():]
        self.field_names = field_names_for(self.note_type)
        self.current_field = self.field_names[0]

    @property
    def fields(self):
        """Get the fields of the note into a dictionary."""
        fields = {field: "" for field in self.field_names}
        for word in self.text.split(" "):
            for field in self.field_names:
                if word == field + ":":
                    self.current_field = field
                    word = ""
            fields[self.current_field] += word + " "
        fields = {
            key: FormatConverter.format(
                value,
                cloze=(
                    "Cloze" in self.note_type
                    and CONFIG_DATA["CurlyCloze"]
                ),
                base_dir=self.base_dir
            )
            for key, value in fields.items()
        }
        return {key: value.strip() for key, value in fields.items()}


class RegexNote:
    ID_REGEXP_STR = r"\n?(?:<!--)?" + ID_PREFIX + r"(\d+).*"
    TAG_REGEXP_STR = r"(" + TAG_PREFIX + r".*)"

    def __init__(self, match_object, note_type, tags=False, has_id=False, base_dir: Path = Path(".")):
        self.base_dir = base_dir
        self.match = match_object
        self.note_type = note_type
        self.groups = list(self.match.groups())
        if has_id:
            # This means id is last group
            self.identifier = int(self.groups.pop())
        else:
            self.identifier = None
        if tags:
            # Even if id were present, tags is now last group
            self.tags = self.groups.pop()[len(TAG_PREFIX):].split(
                TAG_SEP
            )
        else:
            self.tags = []
        self.field_names = field_names_for(self.note_type)

    @property
    def fields(self):
        fields = dict.fromkeys(self.field_names, "")
        for name, match in zip(self.field_names, self.groups):
            if match:
                fields[name] = match
        fields = {
            key: FormatConverter.format(
                value,
                cloze=(
                    "Cloze" in self.note_type
                    and CONFIG_DATA["CurlyCloze"]
                ),
                base_dir=self.base_dir
            )
            for key, value in fields.items()
        }
        return {key: value.strip() for key, value in fields.items()}

    def parse(self, deck, frozen_fields_dict=None) -> Note_and_id | None:
        """Get a properly formatted dictionary of the note."""
        template = build_note_dict(self, deck, frozen_fields_dict)
        # noinspection GrazieInspection
        if "Cloze" in self.note_type and CONFIG_DATA[
            "CurlyCloze"
        ] and not note_has_clozes(template):
            # Skip: a Cloze note with no detected clozes. We can accidentally
            # recognize { in the wrong places, so this guards against that.
            return None
        return Note_and_id(note=template, id=self.identifier)


def set_default(config, section):
    if section not in config:
        config[section] = {}


class Config:
    """Deals with saving and loading the configuration file."""

    @staticmethod
    def setup_syntax(config):
        """Sets up default syntax in the config object."""
        set_default(config, "Syntax")
        config["Syntax"].setdefault(
            "Begin Note", "START"
        )
        config["Syntax"].setdefault(
            "End Note", "END"
        )
        config["Syntax"].setdefault(
            "Begin Inline Note", "STARTI"
        )
        config["Syntax"].setdefault(
            "End Inline Note", "ENDI"
        )
        config["Syntax"].setdefault(
            "Target Deck Line", "TARGET DECK"
        )
        config["Syntax"].setdefault(
            "File Tags Line", "FILE TAGS"
        )
        config["Syntax"].setdefault(
            "Delete Note Line", "DELETE"
        )
        config["Syntax"].setdefault(
            "Frozen Fields Line", "FROZEN"
        )

    @staticmethod
    def setup_defaults(config):
        """Sets up default values in the config file, not to do with syntax."""
        config["DEFAULT"] = {}  # Removes DEFAULT if it's there.
        set_default(config, "Defaults")
        config["Defaults"].setdefault(
            "Tag", "Markdown_to_Anki"
        )
        config["Defaults"].setdefault(
            "Deck", "Default"
        )
        config["Defaults"].setdefault(
            "CurlyCloze", "False"
        )
        config["Defaults"].setdefault(
            "Regex", "False"
        )
        config["Defaults"].setdefault(
            "ID Comments", "True"
        )

    @staticmethod
    def update_config():
        """Update config with new notes."""
        print("Updating configuration file...")
        config = configparser.ConfigParser()
        config.optionxform = str
        if CONFIG_PATH.exists():
            print("Config file exists, reading...")
            config.read(CONFIG_PATH, encoding="utf-8-sig")
        note_types = AnkiConnect.invoke("modelNames")
        set_default(config, "Custom Regexps")
        for note in note_types:
            config["Custom Regexps"].setdefault(note, "")
        Config.setup_syntax(config)
        Config.setup_defaults(config)
        with open(CONFIG_PATH, "w", encoding="utf_8") as configfile:
            config.write(configfile)
        print("Configuration file updated!")

    @staticmethod
    def load_syntax(config):
        """Reads and loads syntax from the config object."""
        CONFIG_DATA["NOTE_PREFIX"] = re.escape(
            config["Syntax"]["Begin Note"]
        )
        CONFIG_DATA["NOTE_SUFFIX"] = re.escape(
            config["Syntax"]["End Note"]
        )
        CONFIG_DATA["INLINE_PREFIX"] = re.escape(
            config["Syntax"]["Begin Inline Note"]
        )
        CONFIG_DATA["INLINE_SUFFIX"] = re.escape(
            config["Syntax"]["End Inline Note"]
        )
        CONFIG_DATA["DECK_LINE"] = re.escape(
            config["Syntax"]["Target Deck Line"]
        )
        CONFIG_DATA["TAG_LINE"] = re.escape(
            config["Syntax"]["File Tags Line"]
        )
        RegexFile.EMPTY_REGEXP = re.compile(
            re.escape(
                config["Syntax"]["Delete Note Line"]
            ) + RegexNote.ID_REGEXP_STR
        )
        CONFIG_DATA["FROZEN_LINE"] = re.escape(
            config["Syntax"]["Frozen Fields Line"]
        )

    @staticmethod
    def load_defaults(config):
        """Loads default values not to do with syntax from config object."""
        NOTE_DICT_TEMPLATE["tags"] = [config["Defaults"]["Tag"]]
        NOTE_DICT_TEMPLATE["deckName"] = config["Defaults"]["Deck"]
        CONFIG_DATA["CurlyCloze"] = config.getboolean(
            "Defaults", "CurlyCloze"
        )
        CONFIG_DATA["Regex"] = config.getboolean(
            "Defaults", "Regex"
        )
        CONFIG_DATA["Comment"] = config.getboolean(
            "Defaults", "ID Comments"
        )

    @staticmethod
    def load_config():
        """Load from an existing config file (assuming it exists)."""
        print("Loading configuration file...")
        config = configparser.ConfigParser()
        config.optionxform = str  # Allows for case sensitivity
        config.read(CONFIG_PATH, encoding="utf-8-sig")
        Config.load_syntax(config)
        Config.load_defaults(config)
        CONFIG_DATA["CUSTOM_REGEXPS"] = config["Custom Regexps"]
        print("Loaded successfully!")


class Data:
    """Class for managing the data file (not meant to be changed by users.)"""

    @staticmethod
    def create_data_file():
        """Creates the data file for the script."""
        print("Creating data file...")
        with open(DATA_PATH, "w") as f:
            json.dump({}, f)

    @staticmethod
    def update_data_file(data):
        """Updates the data file for the script with the given data."""
        print("Updating data file...")
        with open(DATA_PATH, "w") as f:
            json.dump(data, f)

    @staticmethod
    def load_data_file():
        """Loads the data file into memory"""
        with open(DATA_PATH, "r") as f:
            data = json.load(f)
        App.ADDED_MEDIA = data.get("Added Media", [])
        App.FILE_HASHES = data.get("File Hashes", {})


class App:
    """Master class that manages the application."""

    SUPPORTED_EXTS = [".md", ".txt"]

    # Populated at runtime: get_fields()/get_ids() and Data.load_data_file()
    # set the data attributes; gen_regexp() sets the regex attributes.
    FIELDS_DICT = {}
    EXISTING_IDS = []
    ADDED_MEDIA = []
    FILE_HASHES = {}
    NOTE_REGEXP = None
    DECK_REGEXP = None
    TAG_REGEXP = None
    INLINE_REGEXP = None
    FROZEN_REGEXP = None
    # Note IDs seen so far this run, to detect accidentally duplicated IDs.
    SEEN_IDS = set()

    def __init__(self):
        """Execute the main functionality of the script."""
        Config.load_config()
        if not DATA_PATH.exists():
            print("Data file does not exist.")
            Data.create_data_file()
        Data.load_data_file()
        self.get_fields()
        self.get_ids()

        self.parser = argparse.ArgumentParser(
            description="Add cards to Anki from a markdown or text file."
        )
        self.setup_cli_parser()
        args = self.parser.parse_args()
        no_args = True
        if args.update:
            no_args = False
            Config.update_config()
            Config.load_config()
        if args.mediaupdate:
            no_args = False
            Data.create_data_file()
        self.gen_regexp()
        if args.path:
            no_args = False
        if no_args:
            self.parser.print_help()
            return

        App.SEEN_IDS = set()
        self.path: Path = Path(args.path)
        if not self.path.is_dir():
            raise NotADirectoryError(f"{self.path} is not a directory.")
        if args.recurse:
            directories = []
            for root, dirs, files in self.path.walk():
                directories.append(Directory(root, regex=args.regex))
                # Prune "." folders so Path.walk skips them.
                # (Editing dirs while iterating skips entries.)
                dirs[:] = [d for d in dirs if not d.startswith(".")]
        else:
            directories = [Directory(self.path, regex=args.regex)]
        affected_decks = sorted({file.target_deck for directory in directories for file in directory.files})
        backups_path = self.path / BACKUPS
        changed = self.snapshot_decks(affected_decks, backups_path)
        if changed:
            print("Cards already in Anki have been changed there - aborting.")
            sys.exit(1)
        requests = []
        print("Getting tag list")
        requests.append(
            AnkiConnect.request(
                "getTags"
            )
        )
        if len(MEDIA) > 0:
            print("Adding media with these filenames...")
            print(list(MEDIA.keys()))
        requests.append(self.get_add_media())
        print("Adding directory requests...")
        for directory in directories:
            requests.append(directory.requests_1())
        result = AnkiConnect.invoke(
            "multi",
            actions=requests
        )
        tags = AnkiConnect.parse(result[0])
        directory_responses = result[2:]
        for directory, response in zip(directories, directory_responses):
            directory.parse_requests_1(AnkiConnect.parse(response), tags)
        requests = []
        for directory in directories:
            requests.append(directory.requests_2())
        AnkiConnect.invoke(
            "multi",
            actions=requests
        )
        App.ADDED_MEDIA = set(App.ADDED_MEDIA)
        App.ADDED_MEDIA.update(MEDIA.keys())
        App.ADDED_MEDIA = list(App.ADDED_MEDIA)
        for directory in directories:
            App.FILE_HASHES.update(directory.hashes())
        Data.update_data_file(
            {
                "Added Media": App.ADDED_MEDIA,
                "File Hashes": App.FILE_HASHES
            }
        )
        change_counts = self.get_change_counts(directories)
        if sum(change_counts) == 0:
            print("Finished: no changes found.")
            return
        self.print_summary(*change_counts)
        self.snapshot_decks(affected_decks, backups_path)

    def setup_cli_parser(self):
        """Set up the command-line argument parser."""
        self.parser.add_argument(
            "path",
            default=False,
            nargs="?",
            help="Path to the file or directory you want to scan."
        )
        self.parser.add_argument(
            "-u", "--update",
            action="store_true",
            dest="update",
            help="Update config file."
        )
        self.parser.add_argument(
            "-r", "--regex",
            action="store_true",
            dest="regex",
            help="Use custom regex syntax.",
            default=CONFIG_DATA["Regex"]
        )
        self.parser.add_argument(
            "-m", "--mediaupdate",
            action="store_true",
            dest="mediaupdate",
            help="Force addition of media files."
        )
        self.parser.add_argument(
            "-R", "--recurse",
            action="store_true",
            dest="recurse",
            help="Recursively scan subfolders."
        )

    @staticmethod
    def gen_regexp():
        """Generate the regular expressions used by the app."""
        App.NOTE_REGEXP = re.compile(
            r"".join(
                [
                    r"^",
                    CONFIG_DATA["NOTE_PREFIX"],
                    r"\n([\s\S]*?\n)",
                    CONFIG_DATA["NOTE_SUFFIX"],
                    r"\n?"
                ]
            ), flags=re.MULTILINE
        )
        App.DECK_REGEXP = re.compile(
            "".join(
                [
                    r"^",
                    CONFIG_DATA["DECK_LINE"],
                    r"(?:\n|: )(.*)",
                ]
            ), flags=re.MULTILINE
        )
        App.TAG_REGEXP = re.compile(
            r"^" + CONFIG_DATA["TAG_LINE"] + r"(?:\n|: )(.*)",
            flags=re.MULTILINE
        )
        App.INLINE_REGEXP = re.compile(
            "".join(
                [
                    CONFIG_DATA["INLINE_PREFIX"],
                    r"(.*?)",
                    CONFIG_DATA["INLINE_SUFFIX"]
                ]
            )
        )
        App.FROZEN_REGEXP = re.compile(
            CONFIG_DATA["FROZEN_LINE"] + r" - (.*?):\n((?:[^\n]\n?)+)"
        )

    @staticmethod
    def get_add_media():
        """Get the AnkiConnect-formatted add_media request."""
        return AnkiConnect.request(
            "multi",
            actions=[
                AnkiConnect.request(
                    "storeMediaFile",
                    filename=key,
                    data=value
                )
                for key, value in MEDIA.items()
            ]
        )

    @staticmethod
    def get_fields():
        """Get the user's current note types and fields."""
        note_types = AnkiConnect.invoke("modelNames")
        fields_request = [
            AnkiConnect.request(
                "modelFieldNames", modelName=note
            )
            for note in note_types
        ]
        result = AnkiConnect.invoke(
            "multi", actions=fields_request
        )
        App.FIELDS_DICT = {
            note_type: AnkiConnect.parse(fields)
            for note_type, fields in zip(
                note_types,
                result
            )
        }

    @staticmethod
    def get_ids():
        """Get a list of the currently used card IDs."""
        App.EXISTING_IDS = AnkiConnect.invoke("findNotes", query="")

    @staticmethod
    def register_id(note_id, filename):
        """Record a note ID seen in the source; error if it repeats.

        Guards against accidentally duplicated ID comments (e.g. from copying
        a note without removing its ID), which would otherwise be silently
        treated as repeated updates of the same Anki note.
        """
        if note_id in App.SEEN_IDS:
            raise DuplicateNoteIDError(
                f"Note ID {note_id} appears more than once (seen again in "
                f"{filename}). If you copied a note, remove the duplicated "
                f"ID comment from the copy."
            )
        App.SEEN_IDS.add(note_id)

    @staticmethod
    def snapshot_decks(affected_decks, backups_path: Path) -> bool:
        """Snapshot each affected deck."""
        changed = [diff_deck(deck, backups_path) for deck in affected_decks]
        return any(changed)

    @staticmethod
    def get_change_counts(directories):
        scanned_files = [
            f for directory in directories for f in directory.files
        ]
        added = sum(
            len(f.notes_to_add) + len(f.inline_notes_to_add)
            for f in scanned_files
        )
        updated = sum(len(f.notes_to_edit) for f in scanned_files)
        deleted = sum(len(f.notes_to_delete) for f in scanned_files)
        return added, updated, deleted, len(MEDIA)

    @staticmethod
    def print_summary(added, updated, deleted, added_media):
        """Print a one-line summary of what was added/updated/deleted."""
        print(
            f"Finished: {added} note(s) added, {updated} updated, "
            f"{deleted} deleted; {added_media} media file(s) added."
        )


class File:
    """Class for performing script operations at the file-level."""

    def __init__(self, filepath: Path):
        """Perform initial file reading and attribute setting."""
        self.filename = filepath
        with open(self.filename, encoding="utf_8") as f:
            self.file = f.read()
            self.original_file = self.file
        # Populated later by setup_*()/scan_file() and the Directory pipeline.
        self.frozen_fields_dict = {}
        self.target_deck = ""
        self.global_tags = ""
        self.notes_to_add = []
        self.inline_notes_to_add = []
        self.notes_to_edit = []
        self.notes_to_delete = []
        self.id_indexes = []
        self.inline_id_indexes = []
        self.ignore_spans = []
        self.cards = []
        self.note_ids = []
        self.card_ids = []
        self.tags = []

    def setup_frozen_fields_dict(self):
        self.frozen_fields_dict = {
            note_type: dict.fromkeys(fields, "")
            for note_type, fields in App.FIELDS_DICT.items()
        }
        for match in App.FROZEN_REGEXP.finditer(self.file):
            note_type, fields = match.group(1), match.group(2)
            virtual_note = f"{note_type}\n{fields}"
            parsed_fields = Note(virtual_note, base_dir=self.filename.parent).fields
            self.frozen_fields_dict[note_type] = parsed_fields

    def setup_target_deck(self):
        result = App.DECK_REGEXP.search(self.file)
        if result is not None:
            self.target_deck = result.group(1)
        else:
            self.target_deck = NOTE_DICT_TEMPLATE["deckName"]

    def setup_global_tags(self):
        result = App.TAG_REGEXP.search(self.file)
        if result is not None:
            self.global_tags = result.group(1)
        else:
            self.global_tags = ""

    def warn_missing_id(self, note_id):
        """Warn that a note id present in the file isn't in Anki."""
        print(
            f"Warning! Note with id {note_id} in file {self.filename} "
            "does not exist in Anki!"
        )

    @property
    def hash(self):
        return hashlib.sha256(self.file.encode("utf-8")).hexdigest()

    def _begin_scan(self):
        """Log, load file-level config, and reset the per-scan note lists."""
        logging.info(f"Scanning file {self.filename} for notes...")
        self.setup_frozen_fields_dict()
        self.setup_target_deck()
        self.setup_global_tags()
        self.notes_to_add = []
        self.id_indexes = []
        self.notes_to_edit = []
        self.notes_to_delete = []
        self.inline_notes_to_add = []
        self.inline_id_indexes = []
        self.ignore_spans = []

    def _scan_for_deletes(self):
        """Record the ids of notes marked for deletion in the file."""
        for match in RegexFile.EMPTY_REGEXP.finditer(self.file):
            self.notes_to_delete.append(int(match.group(1)))

    def _dispatch_note(self, parsed, position, add_list, index_list):
        """Sort a parsed note into add/edit, or warn if its id is unknown."""
        if parsed.id is None:
            # Need to make sure global_tags get added.
            parsed.note["tags"] += self.global_tags.split(TAG_SEP)
            add_list.append(parsed.note)
            index_list.append(position)
        else:
            App.register_id(parsed.id, self.filename)
            if parsed.id not in App.EXISTING_IDS:
                self.warn_missing_id(parsed.id)
            else:
                self.notes_to_edit.append(parsed)

    def scan_file(self):
        """Sort notes from file into adding vs editing."""
        self._begin_scan()
        for note_match in App.NOTE_REGEXP.finditer(self.file):
            note, position = note_match.group(1), note_match.end(1)
            parsed = Note(note, base_dir=self.filename.parent).parse(
                self.target_deck,
                frozen_fields_dict=self.frozen_fields_dict
            )
            self._dispatch_note(
                parsed, position, self.notes_to_add, self.id_indexes
            )
        for inline_note_match in App.INLINE_REGEXP.finditer(self.file):
            note = inline_note_match.group(1)
            position = inline_note_match.end(1)
            parsed = InlineNote(note, base_dir=self.filename.parent).parse(
                self.target_deck,
                frozen_fields_dict=self.frozen_fields_dict
            )
            self._dispatch_note(
                parsed, position,
                self.inline_notes_to_add, self.inline_id_indexes
            )
        self._scan_for_deletes()

    @staticmethod
    def id_to_str(note_id, inline=False, comment=False):
        """Get the string repr of id."""
        result = f"{ID_PREFIX}{note_id}"
        if comment:
            result = f"<!--{result}-->"
        if inline:
            result += " "
        else:
            result += "\n"
        return result

    def write_ids(self):
        """Write the identifiers to self.file."""
        logging.info(f"Writing new note IDs to file,{self.filename}...")
        self.file = string_insert(
            self.file, list(
                zip(
                    self.id_indexes, [
                        self.id_to_str(note_id, comment=CONFIG_DATA["Comment"])
                        for note_id in self.note_ids[:len(self.notes_to_add)]
                        if note_id is not None
                    ]
                )
            ) + list(
                zip(
                    self.inline_id_indexes, [
                        self.id_to_str(
                            note_id, inline=True,
                            comment=CONFIG_DATA["Comment"]
                        )
                        for note_id in self.note_ids[len(self.notes_to_add):]
                        if note_id is not None
                    ]
                )
            )
        )

    def remove_empties(self):
        """Remove empty notes from self.file."""
        self.file = RegexFile.EMPTY_REGEXP.sub(
            "", self.file
        )

    def write_file(self):
        """Write to the actual os file"""
        if self.file != self.original_file:
            write_safe(self.filename, self.file)

    def get_add_notes(self):
        """Get the AnkiConnect-formatted request to add notes."""
        return AnkiConnect.request(
            "multi",
            actions=[
                AnkiConnect.request(
                    "addNote",
                    note=note
                )
                for note in self.notes_to_add + self.inline_notes_to_add
            ]
        )

    def get_delete_notes(self):
        """Get the AnkiConnect-formatted request to delete a note."""
        return AnkiConnect.request(
            "deleteNotes",
            notes=self.notes_to_delete
        )

    def get_update_fields(self):
        """Get the AnkiConnect-formatted request to update fields."""
        return AnkiConnect.request(
            "multi",
            actions=[
                AnkiConnect.request(
                    "updateNoteFields", note={
                        "id": parsed.id,
                        "fields": parsed.note["fields"],
                        "audio": parsed.note["audio"]
                    }
                )
                for parsed in self.notes_to_edit
            ]
        )

    def get_note_info(self):
        """Get the AnkiConnect-formatted request to get note info."""
        return AnkiConnect.request(
            "notesInfo",
            notes=[
                parsed.id for parsed in self.notes_to_edit
            ]
        )

    def get_cards(self):
        """Get the card IDs for all notes that need to be edited."""
        logging.info("Getting card IDs")
        self.cards = []
        for info in self.card_ids:
            self.cards += info["cards"]

    def get_change_decks(self):
        """Get the AnkiConnect-formatted request to change decks."""
        return AnkiConnect.request(
            "changeDeck",
            cards=self.cards,
            deck=self.target_deck
        )

    def get_clear_tags(self):
        """Get the AnkiConnect-formatted request to clear tags."""
        return AnkiConnect.request(
            "removeTags",
            notes=[parsed.id for parsed in self.notes_to_edit],
            tags=" ".join(self.tags)
        )

    def get_add_tags(self):
        """Get the AnkiConnect-formatted request to add tags."""
        return AnkiConnect.request(
            "multi",
            actions=[
                AnkiConnect.request(
                    "addTags",
                    notes=[parsed.id],
                    tags=" ".join(parsed.note["tags"]) + " " + self.global_tags
                )
                for parsed in self.notes_to_edit
            ]
        )


class RegexFile(File):

    EMPTY_REGEXP = None  # Set by Config.load_syntax().

    def add_spans_to_ignore(self):
        """Mark sections of the file as places not to expect a note."""
        self.ignore_spans += spans(App.NOTE_REGEXP, self.file)
        self.ignore_spans += spans(App.INLINE_REGEXP, self.file)
        self.ignore_spans += spans(
            FormatConverter.MKD_INLINE_MATH_REGEXP, self.file
        )
        self.ignore_spans += spans(
            FormatConverter.MKD_BLOCK_MATH_REGEXP, self.file
        )
        self.ignore_spans += spans(
            FormatConverter.MKD_INLINE_CODE_REGEXP, self.file
        )
        self.ignore_spans += spans(
            FormatConverter.MKD_BLOCK_CODE_REGEXP, self.file
        )

    def scan_file(self):
        """Sort notes from file into adding vs editing."""
        self._begin_scan()
        # add_spans_to_ignore() ensures the script won't match a RegexNote
        # inside a Note or InlineNote.
        self.add_spans_to_ignore()
        for note_type, regexp in CONFIG_DATA["CUSTOM_REGEXPS"].items():
            if regexp:
                self.search(note_type, regexp)
        self._scan_for_deletes()

    def search(self, note_type, regexp):
        """
        Search the file for regex matches of this type,
        ignoring matches inside ignore_spans,
        and adding any matches to ignore_spans.
        """
        regexp_tags_id = re.compile(
            "".join(
                [
                    regexp,
                    RegexNote.TAG_REGEXP_STR,
                    RegexNote.ID_REGEXP_STR
                ]
            ), flags=re.MULTILINE
        )
        regexp_id = re.compile(
            regexp + RegexNote.ID_REGEXP_STR, flags=re.MULTILINE
        )
        regexp_tags = re.compile(
            regexp + RegexNote.TAG_REGEXP_STR, flags=re.MULTILINE
        )
        regexp = re.compile(
            regexp, flags=re.MULTILINE
        )
        for match in find_ignore(regexp_tags_id, self.file, self.ignore_spans):
            # This note has id, so we update it
            self.ignore_spans.append(match.span())
            parsed = RegexNote(match, note_type, tags=True, has_id=True, base_dir=self.filename.parent).parse(
                self.target_deck,
                frozen_fields_dict=self.frozen_fields_dict
            )
            if parsed is None:
                # Cloze note had no detected clozes -- skip it.
                continue
            App.register_id(parsed.id, self.filename)
            if parsed.id not in App.EXISTING_IDS:
                self.warn_missing_id(parsed.id)
            else:
                self.notes_to_edit.append(parsed)
        for match in find_ignore(regexp_id, self.file, self.ignore_spans):
            # This note has id, so we update it
            self.ignore_spans.append(match.span())
            parsed = RegexNote(match, note_type, tags=False, has_id=True, base_dir=self.filename.parent).parse(
                self.target_deck,
                frozen_fields_dict=self.frozen_fields_dict
            )
            if parsed is None:
                # Cloze note had no detected clozes -- skip it.
                continue
            App.register_id(parsed.id, self.filename)
            if parsed.id not in App.EXISTING_IDS:
                self.warn_missing_id(parsed.id)
            else:
                self.notes_to_edit.append(parsed)
        for match in find_ignore(regexp_tags, self.file, self.ignore_spans):
            # This note has no id, so we add it
            self.ignore_spans.append(match.span())
            parsed = RegexNote(match, note_type, tags=True, has_id=False, base_dir=self.filename.parent).parse(
                self.target_deck,
                frozen_fields_dict=self.frozen_fields_dict
            )
            if parsed is None:
                # Cloze note had no detected clozes -- skip it.
                continue
            parsed.note["tags"] += self.global_tags.split(TAG_SEP)
            self.notes_to_add.append(
                parsed.note
            )
            self.id_indexes.append(match.end())
        for match in find_ignore(regexp, self.file, self.ignore_spans):
            # This note has no id, so we update it
            self.ignore_spans.append(match.span())
            parsed = RegexNote(match, note_type, tags=False, has_id=False, base_dir=self.filename.parent).parse(
                self.target_deck,
                frozen_fields_dict=self.frozen_fields_dict
            )
            if parsed is None:
                # Cloze note had no detected clozes -- skip it.
                continue
            parsed.note["tags"] += self.global_tags.split(TAG_SEP)
            self.notes_to_add.append(
                parsed.note
            )
            self.id_indexes.append(match.end())

    def fix_newline_ids(self):
        """Removes double newline then ids from self.file."""
        double_regexp = re.compile(
            r"(?:\r\n|\r|\n){2}(?:<!--)?" + ID_PREFIX + r"\d+"
        )
        self.file = double_regexp.sub(
            lambda x: x.group()[1:],
            self.file
        )

    def write_ids(self):
        """Write the identifiers to self.file."""
        logging.info(f"Writing new note IDs to file,{self.filename}...")
        self.file = string_insert(
            self.file, zip(
                self.id_indexes, [
                    "\n" + File.id_to_str(
                        note_id, comment=CONFIG_DATA["Comment"]
                    )
                    for note_id in self.note_ids
                    if note_id is not None
                ]
            )
        )
        self.fix_newline_ids()


class Directory:
    """Class for managing a directory of files at a time."""

    def __init__(self, path: Path, regex: bool = False):
        """Scan directory for files."""
        self.path = path
        self.file_class: type[File] = RegexFile if regex else File
        self.files = sorted(
            [
                self.file_class(entry)
                for entry in self.path.iterdir()
                if entry.is_file() and entry.suffix in App.SUPPORTED_EXTS
            ], key=lambda f: [
                int(part) if part.isdigit() else part.lower()
                for part in re.split(r"(\d+)", f.filename.name)]
        )
        files_changed = []
        for file in self.files:
            if str(file.filename) in App.FILE_HASHES and (
                file.hash == App.FILE_HASHES[str(file.filename)]
            ):
                # Indicates we've seen this in a scan before,
                # And that it hasn't changed.
                # So, we don't need to do anything with it!
                print("Skipping", file.filename, "as it's been scanned before.")
            else:
                file.scan_file()
                files_changed.append(file)
        self.files = files_changed

    def requests_1(self):
        """Get the 1st HTTP request for this directory."""
        logging.info(f"Forming request 1 for directory {self.path}")
        requests = []
        logging.info("Adding notes into Anki...")
        requests.append(
            AnkiConnect.request(
                "multi",
                actions=[
                    file.get_add_notes()
                    for file in self.files
                ]
            )
        )
        logging.info("Getting card IDs of notes to be edited...")
        requests.append(
            AnkiConnect.request(
                "multi",
                actions=[
                    file.get_note_info()
                    for file in self.files
                ]
            )
        )
        logging.info("Updating fields of existing notes...")
        requests.append(
            AnkiConnect.request(
                "multi",
                actions=[
                    file.get_update_fields()
                    for file in self.files
                ]
            )
        )
        logging.info("Removing empty notes...")
        requests.append(
            AnkiConnect.request(
                "multi",
                actions=[
                    file.get_delete_notes()
                    for file in self.files
                ]
            )
        )
        return AnkiConnect.request(
            "multi",
            actions=requests
        )

    def parse_requests_1(self, requests_1_response, tags):
        response = requests_1_response
        notes_ids = AnkiConnect.parse(response[0])
        cards_ids = AnkiConnect.parse(response[1])
        for note_ids, file in zip(notes_ids, self.files):
            file.note_ids = [
                AnkiConnect.parse(response)
                for response in AnkiConnect.parse(note_ids)
            ]
        for card_ids, file in zip(cards_ids, self.files):
            file.card_ids = AnkiConnect.parse(card_ids)
        for file in self.files:
            file.tags = tags
        for file in self.files:
            file.get_cards()
            file.write_ids()
            logging.info(f"Removing empty notes for file {file.filename}")
            file.remove_empties()
            file.write_file()

    def requests_2(self):
        """Get 2nd big request."""
        logging.info(f"Forming request 2 for directory {self.path}")
        requests = []
        logging.info("Moving cards to target deck...")
        requests.append(
            AnkiConnect.request(
                "multi",
                actions=[
                    file.get_change_decks()
                    for file in self.files
                ]
            )
        )
        logging.info("Replacing tags...")
        requests.append(
            AnkiConnect.request(
                "multi",
                actions=[
                    file.get_clear_tags()
                    for file in self.files
                ]
            )
        )
        requests.append(
            AnkiConnect.request(
                "multi",
                actions=[
                    file.get_add_tags()
                    for file in self.files
                ]
            )
        )
        return AnkiConnect.request(
            "multi",
            actions=requests
        )

    def hashes(self):
        """Return a dictionary of file hashes to use."""
        return {str(file.filename): file.hash for file in self.files}


if __name__ == "__main__":
    print("Attempting to connect to Anki...")
    try:
        wait_for_port(ANKI_PORT)
    except TimeoutError:
        print("Couldn't connect to Anki, please open Anki before running script again.")
    else:
        print("Connected!")
        main()
