import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from anki_connect import AnkiConnect
from diff_utils import ObjectDiff


def _utc_isoformat(dt):
    return dt.isoformat().replace("+00:00", "Z")


@dataclass
class Note:
    model_name: str
    tags: list[str]
    fields: dict[str, str]
    cards: list[int]
    mod: str

    @staticmethod
    def from_dict(d):
        return Note(
            model_name=d["modelName"],
            tags=d["tags"],
            fields={name: field["value"] for name, field in d["fields"].items() if len(field["value"]) > 0},
            cards=d["cards"],
            mod=_utc_isoformat(datetime.fromtimestamp(d["mod"], tz=timezone.utc)),
        )


# Unsafe characters for macOS, Windows and Linux.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')

def to_filename(text):
    text = _UNSAFE_FILENAME_CHARS.sub("", text)
    return text.strip().rstrip(".")


def retrieve_deck(deck_name) -> dict[int, Note]:
    note_ids = AnkiConnect.invoke("findNotes", query=f'deck:"{deck_name}"')
    raw = AnkiConnect.invoke("notesInfo", notes=note_ids)
    return {n["noteId"]: Note.from_dict(n) for n in raw}


def save_deck(notes, directory, filename_stem):
    timestamp = _utc_isoformat(datetime.now(tz=timezone.utc)).replace(":", "-")
    filename = directory / f"{filename_stem}-{timestamp}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump({str(note_id): asdict(n) for note_id, n in notes.items()}, f, indent=2)
    return filename


def load_prev_notes(directory, filename_stem):
    pattern = f"{filename_stem}-????-??-??T??-??-??.??????Z.json"
    matches = sorted(directory.glob(pattern))
    if not matches:
        return None, None
    filename = matches[-1]
    with open(filename, encoding="utf-8") as f:
        return filename, {int(note_id): Note(**d) for note_id, d in json.load(f).items()}


def _diff_notes(notes, prev_notes):
    d = ObjectDiff()
    for note_id in sorted(set(notes) | set(prev_notes)):
        if note_id not in notes:
            print(f"{note_id} deleted")
        elif note_id not in prev_notes:
            print(f"{note_id} added {notes[note_id].mod}")
        elif notes[note_id] != prev_notes[note_id]:
            note = notes[note_id]
            prev = prev_notes[note_id]
            print(f"{note_id} changed {note.mod}")
            changes = (
                d.compare(prev.model_name, note.model_name)
                + d.compare(prev.tags, note.tags)
                + d.compare(prev.fields, note.fields)
                + d.compare(prev.cards, note.cards)
            )
            for change in changes:
                print(f"* {change}")
            print()


def diff_deck(deck_name, directory) -> bool:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    notes = retrieve_deck(deck_name)
    filename_stem = to_filename(deck_name)
    prev_filename, prev_notes = load_prev_notes(directory, filename_stem)
    if prev_notes is not None and prev_notes == notes:
        print(f"no changes to {deck_name}")
        return False
    filename = save_deck(notes, directory, filename_stem)
    if prev_notes is None:
        print(f"saved {filename}")
        return False
    print(f"saved {filename} (previous {prev_filename})")
    _diff_notes(notes, prev_notes)
    return True


if __name__ == "__main__":
    changed = diff_deck(sys.argv[2], sys.argv[1])
    if changed:
        print(f"deck changed")
