"""Smallest checks that fail if the pure logic breaks. Run: python test_bot.py"""

from bot.bookorbit import pick_best, sse_events
from bot.main import bound_recipient, describe, device_type, find_owned, norm, pick_file, titles_from


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


def test_bound_recipient():
    rs = [{"id": 1, "name": "Home Kindle"}, {"id": 2, "name": "tg:42 Val"}, {"id": 3, "name": "tg:420"}, {"id": 4, "name": None}]
    assert bound_recipient(rs, 42)["id"] == 2
    assert bound_recipient(rs, 420)["id"] == 3
    assert bound_recipient(rs, 4) is None


def test_device_type():
    assert device_type("a@Kindle.com") == "kindle"
    assert device_type("a@free.kindle.com") == "kindle"
    assert device_type("a@gmail.com") == "other"


def test_pick_file():
    fs = [{"id": 1, "format": "mp3"}, {"id": 2, "format": "PDF"}, {"id": 3, "format": "epub"}, {"id": 4, "format": "jpg"}]
    assert pick_file(fs)["id"] == 3
    assert pick_file(fs[:2])["id"] == 2
    assert pick_file([fs[0], fs[3]]) is None
    assert pick_file([]) is None


def test_norm():
    assert norm("1984 - George Orwell", ["George Orwell"]) == "1984"
    assert norm("Nineteen Eighty-Four") == "nineteen eighty four"
    assert norm(None) == ""


def test_find_owned():
    cand = {"title": "1984 - George Orwell", "authors": ["George Orwell"]}
    lib = [
        {"id": 1, "title": "1984", "authors": ["George Orwell"], "formats": ["epub"]},
        {"id": 2, "title": "1984", "authors": ["Someone Else"], "formats": ["epub"]},
        {"id": 3, "title": "1985", "authors": ["George Orwell"], "formats": ["epub"]},
    ]
    assert find_owned(cand, lib)["id"] == 1
    assert find_owned(cand, lib[1:]) is None                                   # wrong author, near-miss title
    assert find_owned(cand, [{"id": 4, "title": "1984", "authors": [], "formats": ["mp3"]}]) is None  # audiobook only
    assert find_owned(cand, [{"id": 5, "title": "1984", "authors": []}])["id"] == 5  # no author/format info: accept
    assert find_owned({"title": "", "authors": []}, lib) is None
    assert find_owned({"title": "Animal Farm", "authors": ["George Orwell"]}, lib) is None


def test_describe_without_request_id():
    text = describe({"title": "Dune", "authors": ["Frank Herbert"], "status": "available", "matchedBookId": 7})
    assert "#" not in text and text.endswith("available")


def test_describe_review():
    assert "Wangla" in describe({"id": 1, "title": "Dune", "status": "needs_review"})
    assert "Wangla" not in describe({"id": 1, "title": "Dune", "status": "searching"})


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
