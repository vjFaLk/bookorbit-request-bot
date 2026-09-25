"""Smallest checks that fail if the pure logic breaks. Run: python test_bot.py"""

from bot.bookorbit import pick_best, sse_events
from bot.main import titles_from


def test_titles_from():
    assert titles_from("/request Dune\nNeuromancer\n\n") == ["Dune", "Neuromancer"]
    assert titles_from("/request@mybot Dune") == ["Dune"]
    assert titles_from("Dune") == ["Dune"]
    assert titles_from("/request") == []


def test_sse_events():
    raw = "event: provider-status\ndata: {\"provider\":\"google\"}\n\ndata: {\"title\":\"Dune\"}\n\n"
    assert list(sse_events(raw.splitlines())) == [("provider-status", '{"provider":"google"}'), (None, '{"title":"Dune"}')]


def test_pick_best():
    c = [
        {"title": "Dune Messiah", "authors": ["Frank Herbert"]},
        {"title": "Dune", "authors": ["Frank Herbert"]},
        {"title": "Dune", "authors": ["Frank Herbert"], "isbn13": "9780441013593"},
    ]
    assert pick_best("Dune", c)["isbn13"] == "9780441013593"
    assert pick_best("Dune Messiah", c)["title"] == "Dune Messiah"


if __name__ == "__main__":
    test_titles_from(); test_sse_events(); test_pick_best(); print("ok")
