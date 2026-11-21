"""Headlines, descriptions and page titles cut from printed text; the edition.toml overlay loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from lanterns.core.display_text import clip_words, letter_page_title, opening_headline
from lanterns.core.edition_overlay import EditionOverlayError, load_edition_overlay, parse_edition_overlay

XX = (
    "To-day, the 22nd of December, we were all taken to Semjonovsky Square. There the death-sentence was read "
    "to us, we were given the Cross to kiss, the dagger was broken over our heads."
)


def test_headline_is_the_first_sentence_when_it_fits() -> None:
    assert opening_headline(XX) == "To-day, the 22nd of December, we were all taken to Semjonovsky Square."


def test_headline_does_not_cut_at_initials_and_falls_back_to_a_clause_break() -> None:
    text = "I wrote to Mme. N. D. Fonvisin yesterday about the books, the money and the parcel that never came, and she answered."
    head = opening_headline(text)
    assert head.endswith("…") and "Mme. N. D. Fonvisin" in head and len(head) <= 91
    assert not head.startswith("I wrote to Mme.…")


def test_headline_without_punctuation_cuts_at_a_word() -> None:
    head = opening_headline("word " * 40)
    assert head.endswith("…") and len(head) <= 91 and not head[:-1].endswith(" ")


def test_short_text_is_returned_whole_and_description_clips_at_155() -> None:
    assert opening_headline("Dearest Brother,") == "Dearest Brother,"
    desc = clip_words(XX + " " + XX, 155)
    assert desc.endswith("…") and len(desc) <= 156


@pytest.mark.parametrize(
    ("sender", "recipient", "date", "want"),
    [
        ("Fyodor Dostoevsky", "his brother Michael", "22 December 1849", "Fyodor Dostoevsky to his brother Michael, 22 December 1849"),
        ("Fyodor Dostoevsky", "", "1854", "Fyodor Dostoevsky, 1854"),
        ("", "Maikov", "", "Letter to Maikov"),
        ("", "", "", "Letter"),
    ],
)
def test_page_title_degrades_gracefully(sender: str, recipient: str, date: str, want: str) -> None:
    assert letter_page_title(sender, recipient, date) == want


def test_overlay_maps_printed_names_and_rejects_conflicts_and_bad_periods(tmp_path: Path) -> None:
    overlay = parse_edition_overlay({"correspondents": [{"name": "Fyodor Dostoevsky", "printed": ["F. Dostoevsky"]}]})
    assert overlay.full_name("F. Dostoevsky") == "Fyodor Dostoevsky"
    assert overlay.full_name("Turgenev") == "Turgenev"
    with pytest.raises(EditionOverlayError):
        parse_edition_overlay({"correspondents": [{"name": "A", "printed": ["X"]}, {"name": "B", "printed": ["X"]}]})
    with pytest.raises(EditionOverlayError):
        parse_edition_overlay({"life_periods": [{"label": "A", "start": "1837", "end": "1849"}]})
    good = {"label": "A", "start": "1837", "end": "1849", "source": "p. xi"}
    with pytest.raises(EditionOverlayError):
        parse_edition_overlay({"life_periods": [good, {**good, "label": "B", "start": "1840", "end": "1850"}]})
    assert load_edition_overlay(tmp_path / "missing.toml").periods == ()


def test_overlay_survives_book_config_save(tmp_path: Path) -> None:
    """Regression: BookConfig.save() rewrites book.toml, so editorial data must live in edition.toml."""
    from lanterns.core.config import BookConfig
    from lanterns.core.paths import RepoPaths

    repo = RepoPaths.from_root(str(tmp_path))
    book_dir = repo.book_dir("save-test-1900")
    book_dir.mkdir(parents=True)
    (book_dir / "book.toml").write_text('[book]\nbook_id = "save-test-1900"\ntitle = "T"\n\n[[life_periods]]\nlabel = "X"\n', encoding="utf-8")
    (book_dir / "edition.toml").write_text(
        '[[life_periods]]\nlabel = "A"\nstart = "1837"\nend = "1849"\nsource = "p. xi"\n', encoding="utf-8"
    )
    BookConfig.load("save-test-1900", paths=repo).save(paths=repo)
    assert "life_periods" not in (book_dir / "book.toml").read_text(encoding="utf-8")
    assert load_edition_overlay(book_dir / "edition.toml").periods[0].label == "A"
