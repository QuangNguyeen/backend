"""Tests for the pure vocabulary import parser (no DB, no HTTP)."""

from app.utils.vocabulary_import import build_header_map, parse_import_file


def test_build_header_map_recognizes_aliases():
    headers = ["Term", "Definition", "IPA", "POS", "Notes", "Example"]
    mapping = build_header_map(headers)
    assert mapping == {
        0: "word",
        1: "meaning",
        2: "phonetic",
        3: "part_of_speech",
        4: "note",
        5: "context_sentence",
    }


def test_build_header_map_ignores_unknown_columns():
    mapping = build_header_map(["word", "random_col", "meaning"])
    assert mapping == {0: "word", 2: "meaning"}


def test_parse_csv_maps_rows_to_fields():
    content = b"word,meaning,ipa\napple,qua tao,/ap/\nbook,sach,/buk/\n"
    rows = parse_import_file("words.csv", content)
    assert rows == [
        {"word": "apple", "meaning": "qua tao", "phonetic": "/ap/"},
        {"word": "book", "meaning": "sach", "phonetic": "/buk/"},
    ]


def test_parse_csv_strips_bom_and_whitespace():
    content = "﻿word, meaning\n apple , a fruit \n".encode()
    rows = parse_import_file("words.csv", content)
    assert rows[0]["word"] == "apple"
    assert rows[0]["meaning"] == "a fruit"


def test_parse_empty_file_returns_empty_list():
    assert parse_import_file("empty.csv", b"") == []