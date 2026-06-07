Markdown to Anki
================

There are various Markdown-to-Anki solutions, most of them have extremely low take-up and have not been actively maintained. This is not another such solution. This page just documents using the core Python script in the [`Obsidian_to_Anki` plugin](https://github.com/ObsidianToAnki/Obsidian_to_Anki) as a general solution for managing Anki cards as Markdown.

The last commit to the `Obsidian_to_Anki` was in February 2024, so it's no more alive than most of the other similar solutions. However, it's the only solution that obtained any degree of popularity. And in the end the script isn't that complicated, it was actively developed at one stage and is probably as good as it needs to be.

I've copied over the `obsidian_to_anki.py` script to this repo, renamed it to `markdown_to_anki.py` and made some minor changes, 99%+ of the credit goes to the original author Rubaiyat Khondaker (who I believe is GitHub user [Pseudonium](https://github.com/Pseudonium).

`Obsidian_to_Anki` depends on the [Anki-Connect plugin](https://git.sr.ht/~foosoft/anki-connect) which is actively maintained. All the real smarts are there.

Note: Anki-Connect is an Anki plugin while `Obsidian_to_Anki` is a plugin for a Markdown editor called [Obsidian](https://obsidian.md/). On this page we're going to look at how to use the core of `Obsidian_to_Anki` without Obsidian itself.

Workflow
--------

This workflow treats your Markdown files as the canonical "source of truth". It's a `git` friendly approach:

* **Explicit IDs**: When you run the script, it scans your Markdown for cards. If a card is new, it creates it in Anki and writes an ID back into your Markdown file (e.g., ID: 1712345678).
* **Idempotency** On the next run, it sees that ID. If you’ve edited the text, it updates the existing card in Anki instead of duplicating it.

As the ID is plain text in your file, there's no issue tracking the life of a card in `git` from creation and on through to any subsequent modifications.

All Anki note types, including custom ones, are supported, not just the default ones.

Setup
-----

Anki needs to be [installed](https://apps.ankiweb.net/#downloads) along with the [Anki-Connect plugin](https://git.sr.ht/~foosoft/anki-connect#installation).

Notes:

* The Anki-Connect `README` covers the need to disable a feature on Macs that's called _App Nap_ and was introduced in OS X Mavericks. _App Nap_ has got a lot smarter since then and disabling it is almost certainly unnecessary on any more up-to-date macOS version.
* The original `Obsidian_to_Anki` installation notes cover modifying the config for the Anki-Connect plugin. This is only necessary if using the Obsidian Markdown editor itself - which is _not_ the case here

The `Obsidian_to_Anki` [GitHub repo](https://github.com/ObsidianToAnki/Obsidian_to_Anki) is mainly support functionality for the Obsidian side of things. We don't need any of that, we just need its main Python script.

```
$ git clone git@github.com:george-hawkins/markdown-to-anki.git
$ cd markdown-to-anki
$ ls *.py
markdown_to_anki.py
```

Then create a virtual environment containing the `markdown` package, the single dependency that the script needs:

```
$ python3 -m venv venv
$ source venv/bin/activate
(venv) $ pip install --upgrade pip
(venv) $ pip install markdown
```

Run `markdown_to_anki.py` for the first time (Anki must already be running with the Anki-Connect plugin installed):

```
(venv) $ python markdown_to_anki.py 
Attempting to connect to Anki...
Connected!
Updating configuration file...
Configuration file updated!
Loading configuration file...
Loaded successfully!
Data file does not exist, creating...
Creating data file...
usage: markdown_to_anki.py [-h] [-u] [-r] [-m] [-R] [path]

Add cards to Anki from a markdown or text file.

positional arguments:
  path               Path to the file or directory you want to scan.

options:
  -h, --help         show this help message and exit
  -u, --update       Update config file.
  -r, --regex        Use custom regex syntax.
  -m, --mediaupdate  Force addition of media files.
  -R, --recurse      Recursively scan subfolders.
```

Several things happen:

* It connects to Anki (via Anki-Connect).
* It queries Anki, in particular it asks Anki if it has any custom note types, and creates a `markdown_to_anki_config.ini` file.
* It creates a `markdown_to_anki_data.json` file.
* Finally, it prints out the command-line usage details.

**Note**: the `--update` option is only needed if you're also using the `--regex` option and want the `.ini` file to be updated with the names of any new note types that you've added in Anki. This is fairly advanced usage that you'll probably never need.

Adding to a deck
-----------------

Create a file like this:

```
(venv) $ mkdir decks
(venv) $ vim decks/math.md
```

And add:

```
TARGET DECK: Mathematics
START
Basic
Front: Card A
Alpha
Back: Beta
Gamma
Tags: Testing
END
STARTI [Basic] Card B. Back: Epsilon ENDI
```

This defines two cards, one using the verbose multiline format and one using the compact inline format.

The `TARGET DECK` specifies the Anki deck to which the cards belong.

**IMPORTANT:** each file needs to start with a `TARGET DECK` and you can't switch `TARGET DECK` within a file. Cards for different decks need to go in different files. You can split a deck over multiple files, i.e. there's no issues with multiple files having the same `TARGET DECK` value.

If the deck does not already exist in Anki, it'll throw an exception like this when you try to upload it:

```
Adding directory requests...
Traceback (most recent call last):
  File ".../markdown_to_anki.py", line 1777, in <module>
    main()
  File ".../markdown_to_anki.py", line 217, in main
    App()
  File ".../markdown_to_anki.py", line 974, in __init__
    directory.parse_requests_1(AnkiConnect.parse(response), tags)
  File ".../markdown_to_anki.py", line 1708, in parse_requests_1
    AnkiConnect.parse(response)
  File ".../markdown_to_anki.py", line 245, in parse
    raise Exception(response['error'])
Exception: deck was not found: Mathematics
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
```

The verbose multiline format card is delimited by `FRONT` and `END` and its first line specifies the Anki [note type](https://docs.ankiweb.net/getting-started.html#note-types) (in this case _Basic_):

```
START
Basic
Front: Card A
Alpha
Back: Beta
Gamma
Tags: Testing
END
```

After the note type are the values for that note types fields, e.g. the value for the `Front` field here is:

```
Card A
Alpha
```

The value can continue over multiple lines, any lines that don't start with a field name followed by a colon (e.g. `Back:`) are assumed to be a continuation of the current field.

The inline format card is delimited by `FRONTI` and `ENDI`:

```
STARTI [Basic] Card B. Back: Epsilon ENDI
```

Here you can see that the first field is not introduced by `Front:`. The first field name can actually be omitted in both the multiline and inline formats. When using the inline format, you have to use `<br>` to add a newline in a value:

```
STARTI [Basic] Card C. Back: foo<br>bar ENDI
```

### Uploading the cards

Assuming the target deck exists, the Markdown cards can be uploaded like so:

```
(venv) $ python markdown_to_anki.py decks
Attempting to connect to Anki...
Connected!
Loading configuration file...
Loaded successfully!
Getting tag list
Adding directory requests...
Updating data file...
```

**Important:** this will rewrite your Markdown files to add in IDs for any new cards:

```
(venv) $ cat decks/math.md 
TARGET DECK: Mathematics
START
Basic
Front: Card A
Alpha
Back: Beta
Gamma
Tags: Testing
<!--ID: 1775999965858-->
END
STARTI [Basic] Card B. Back: Epsilon <!--ID: 1775999965861--> ENDI
```

See the `<!-- ID: ... -->` entries that have been added.

If you then find you've e.g. made a spelling mistake on one of these cards, you can correct it and next time `markdown_to_anki.py` is run, it'll use the card's ID to update the existing card rather than create a new one.

**Warning:** the script isn't too smart, so if you copy an existing card and forget to remove its `<!-- ID: ... -->` element then the last card with that ID in the file will "win". This is easy to fix up afterward _if you notice the problem_.

### Deleting a note

To delete a note, just replace its definition with:

```
DELETE
ID: 123456789
```

Where `ID` is obviously the ID associated with the definition.

### Custom note types

The above examples, used the _Basic_ note type, but you can specify any note type, including custom ones, that you see if you select _Tools / Manage Note Types_ in Anki. If you select a note type there, and click the _Fields_ button, you can see the field names that you can specify for that note type.

Keeping note type and field names simple makes things easier when working with `markdown_to_anki.py`. E.g. I have a custom note type called `3. All-Purpose Card` that has fields names like `• Make 2 cards? (y = yes, blank = no)`. This is annoying to type correctly as part of a card definition and looks a bit odd:

```
START
3. All-Purpose Card
Alpha
Back (a single word/phrase, no context): Beta
• Make 2 cards? (y = yes, blank = no): Gamma
END
```

### Media

Media that you _reference inside a note_ is uploaded automatically — this covers images and audio:

* **Images** use Markdown's image syntax, e.g. `![A diagram](images/diagram.png)`.
* **Audio** uses Anki's sound syntax, e.g. `[sound:pronunciation.mp3]`.

The referenced path is resolved _relative to the Markdown file_ that contains the note. So given a card in `decks/math.md`:

```
START
Basic
Front: What does the diagram look like?
Back: ![A diagram](images/diagram.png)
END
```

The script looks for the image at `decks/images/diagram.png`. When it runs, it reads the file, uses Anki-Connect to copy it into Anki's `collection.media` folder, and then rewrites the reference in the card to use just the bare filename (`diagram.png`). Anki's `collection.media` is a single flat folder, so the subfolder you keep media in locally is purely for your own organization — and it means media filenames have to be unique (two different files both called `diagram.png` would collide).

Once a file has been uploaded, this is recorded in `markdown_to_anki_data.json`. This file is just used to speed up future uploads:

* For Markdown files, it records a SHA, and only reuploads the file if its SHA has changed since the last upload.
* For media files (audio, images etc.), it just records the files name and doesn't upload it when it encounters it on the next upload.

So if you change the contents of a media file but don't change its name, the changed version won't be uploaded to Anki unless you use the `--mediaupdate` argument to force media to be reuploaded.

The script does not handle media files that are not part of cards, e.g. font files that you reference in card templates. You have to handle these yourself.

### Updating .ini and ...\_data.json

There are two command line "update" arguments:

* `-u` / `--update` — refreshes `markdown_to_anki_config.ini` by re-querying Anki for its current note types and fields. Use this when you've added or renamed note types in Anki.
*  `-m` / `--mediaupdate` — this is misnamed as it completely resets the `markdown_to_anki_data.json` which causes not just all media files to be reuploaded but also forces all Markdown files to be reuploaded. Use this if you've accidentally deleted notes or the related media.

### Going further

For lots more information, see the [getting-started section](https://github.com/ObsidianToAnki/Obsidian_to_Anki/wiki/Steps-for-new-users) of the `Obsidian_to_Anki` wiki.

Development
-----------

Intall the necessary dependencies for linting and testing:

```
(venv) $ pip install pytest ruff
```

And run the test suite and linter like so:

```
(venv) $ python -m pytest
(venv) $ ruff check .
```

Notes
-----

Each time you run `markdown_to_anki.py`, it updates a file called `markdown_to_anki_data.json`, this just allows `markdown_to_anki.py` to avoid redoing work it's already done. It will ignore files and media that have not changed since the last time it was run - this can become important if you end up with huge amounts of cards and/or media. If you delete or lose this file, it'll be recreated and simply result in the related import being slower than it would be otherwise .
