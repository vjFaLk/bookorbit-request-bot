"""Smallest checks that fail if the pure logic breaks. Run: python test_bot.py"""

from bot.bookorbit import rank, sse_events
from bot.main import COMIC_FORMATS, bound_recipient, describe, device_type, find_owned, link_book, media_kind, norm, pick_file, picker, titles_from


def test_titles_from():
    assert titles_from("/request Dune\nNeuromancer\n\n") == ["Dune", "Neuromancer"]
    assert titles_from("/request@mybot Dune") == ["Dune"]
    assert titles_from("Dune") == ["Dune"]
    assert titles_from("/request") == []


def test_sse_events():
    raw = "event: provider-status\ndata: {\"provider\":\"google\"}\n\ndata: {\"title\":\"Dune\"}\n\n"
    assert list(sse_events(raw.splitlines())) == [("provider-status", '{"provider":"google"}'), (None, '{"title":"Dune"}')]


def test_rank():
    c = [
        {"title": "Dune Messiah", "authors": ["Frank Herbert"], "provider": "google"},
        {"title": "Dune", "authors": ["Frank Herbert"], "provider": "openlibrary"},
        {"title": "Dune", "authors": ["Frank Herbert"], "isbn13": "9780441013593", "provider": "google"},
        {"title": "Dune: Graphic Novel", "authors": ["Brian Herbert"], "provider": "google"},
    ]
    top = rank("Dune", c)
    assert [t["title"] for t in top] == ["Dune", "Dune Messiah", "Dune: Graphic Novel"]  # duplicate Dune collapsed
    assert top[0]["isbn13"] == "9780441013593"  # ISBN wins the tie inside the duplicate group
    assert rank("Dune Messiah", c)[0]["title"] == "Dune Messiah"
    assert len(rank("Dune", c, n=2)) == 2


def test_picker():
    text, kb = picker("dune", [{"title": "Dune", "authors": ["Frank Herbert"], "publishedYear": 1965, "provider": "google"}, {"title": "Dune Messiah"}], 42)
    assert "1. <b>Dune</b> — Frank Herbert (1965)" in text and "2. <b>Dune Messiah</b>" in text and "google" not in text
    rows = kb.inline_keyboard
    assert [b.callback_data for b in rows[0]] == ["pick:42:0", "pick:42:1"]
    assert rows[1][0].callback_data == "pick:42:x"


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


def test_find_owned_comic():
    cand = {"title": "Saga #1", "authors": ["Brian K. Vaughan"]}
    cbz = [{"id": 1, "title": "Saga #1", "authors": ["Brian K. Vaughan"], "formats": ["cbz"]}]
    epub = [{"id": 2, "title": "Saga #1", "authors": ["Brian K. Vaughan"], "formats": ["epub"]}]
    assert find_owned(cand, cbz, formats=COMIC_FORMATS)["id"] == 1
    assert find_owned(cand, epub, formats=COMIC_FORMATS) is None      # an e-book is not an owned comic
    assert find_owned(cand, [{"id": 3, "title": "Saga #1", "formats": ["cb7"]}], formats=COMIC_FORMATS)["id"] == 3


def test_media_kind():
    assert media_kind("comic") == "comic"
    assert media_kind("download") == "ebook"
    assert media_kind(None) == "ebook"


def test_link_book():
    text = link_book({"title": "Saga #1", "status": "available", "matchedBookId": 7}, 7, "http://orbit.local:3000/")
    assert text.endswith("http://orbit.local:3000/book/7")


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
