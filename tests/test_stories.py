"""The stories module owns real durable state, so these pin the behaviors the
frontend relies on: vote dedup and toggling, the top list excluding unvoted
stories, and comment threading staying inside one story."""

import pytest

from app import auth, stories


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(stories, "DB_PATH", tmp_path / "stories.db")


def make(from_station="Berlin Hbf", to_station="", departure="", train="",
         problems=None, problem_other="",
         title="Stranded at platform 9", text="x" * 20, author=""):
    return stories.create_story(
        from_station, to_station, departure, train, problems or [], problem_other,
        title, text, author
    )


def user(name):
    _kind, _name, magic, _code = auth.register(name, f"{name}@example.org")
    account, _session = auth.consume(magic)
    return account["id"]


def test_create_returns_full_row():
    row = make(author="Max")
    assert row["id"] == 1
    assert row["from_station"] == "Berlin Hbf"
    assert row["to_station"] == ""       # a story can stay at one station
    assert row["departure"] == ""
    assert row["author"] == "Max"
    assert row["score"] == 0
    assert row["comments"] == 0
    assert row["ts"]


def test_new_lists_latest_first_and_paginates():
    first, second, third = make(title="one"), make(title="two"), make(title="three")
    titles = [s["title"] for s in stories.list_stories("new", 30, 0)]
    assert titles == ["three", "two", "one"]
    page = stories.list_stories("new", 2, 2)
    assert [s["title"] for s in page] == ["one"]


def test_vote_dedup_and_toggle():
    story = make()
    alice, bob = user("alice"), user("bob")
    assert stories.set_vote(story["id"], alice, True) == {"score": 1, "voted": True}
    # the same account voting again is a no-op, not a second vote
    assert stories.set_vote(story["id"], alice, True)["score"] == 1
    assert stories.set_vote(story["id"], bob, True)["score"] == 2
    assert stories.set_vote(story["id"], alice, False) == {"score": 1, "voted": False}
    # clearing a vote that isn't there stays a no-op
    assert stories.set_vote(story["id"], alice, False)["score"] == 1


def test_vote_on_missing_story():
    assert stories.set_vote(999, user("alice"), True) is None


def test_top_excludes_unvoted_and_orders_by_score():
    quiet = make(title="quiet")
    mid = make(title="mid")
    hot = make(title="hot")
    alice, bob = user("alice"), user("bob")
    for voter in (alice, bob):
        stories.set_vote(hot["id"], voter, True)
    stories.set_vote(mid["id"], alice, True)
    top = stories.list_stories("top", 5, 0)
    assert [s["title"] for s in top] == ["hot", "mid"]
    assert [s["score"] for s in top] == [2, 1]


def test_liked_and_commented_keep_every_story_and_break_ties_newest_first():
    quiet = make(title="quiet")
    mid = make(title="mid")
    hot = make(title="hot")
    alice, bob = user("alice"), user("bob")
    for voter in (alice, bob):
        stories.set_vote(hot["id"], voter, True)
    stories.set_vote(mid["id"], alice, True)
    stories.add_comment(mid["id"], None, "Anna", "same here")
    liked = [s["title"] for s in stories.list_stories("liked", 5, 0)]
    assert liked == ["hot", "mid", "quiet"]
    commented = [s["title"] for s in stories.list_stories("commented", 5, 0)]
    assert commented == ["mid", "hot", "quiet"]


def test_removed_story_drops_out_of_the_ranked_lists():
    gone = make(title="gone", author="Anna")
    kept = make(title="kept")
    alice = user("alice")
    stories.set_vote(gone["id"], alice, True)
    stories.add_comment(gone["id"], None, "Bob", "reply keeps the thread")
    assert stories.delete_story(gone["id"], "Anna")
    for sort in ("top", "liked", "commented"):
        assert "gone" not in [s["title"] for s in stories.list_stories(sort, 5, 0)]
    # the tombstone still holds its place on the new list for the thread
    listed = stories.list_stories("new", 5, 0)
    assert [s["deleted"] for s in listed] == [False, True]


def test_voted_flag_is_per_viewer():
    story = make()
    alice, bob = user("alice"), user("bob")
    stories.set_vote(story["id"], alice, True)
    assert stories.list_stories("new", 1, 0, alice)[0]["voted"] is True
    assert stories.list_stories("new", 1, 0, bob)[0]["voted"] is False
    # anonymous readers never see a voted arrow
    assert stories.list_stories("new", 1, 0, None)[0]["voted"] is False


def test_comments_thread_and_count():
    story = make()
    root = stories.add_comment(story["id"], None, "Anna", "same thing happened to me")
    reply = stories.add_comment(story["id"], root["id"], "", "which platform?")
    listed = stories.list_comments(story["id"])
    assert [c["id"] for c in listed] == [root["id"], reply["id"]]
    assert listed[1]["parent_id"] == root["id"]
    assert listed[0]["parent_id"] is None
    assert stories.list_stories("new", 1, 0)[0]["comments"] == 2


def test_comment_parent_must_be_on_same_story():
    story_a, story_b = make(), make()
    root_a = stories.add_comment(story_a["id"], None, "", "first")
    with pytest.raises(ValueError):
        stories.add_comment(story_b["id"], root_a["id"], "", "cross-story reply")
    # a parent id that doesn't exist at all is rejected the same way
    with pytest.raises(ValueError):
        stories.add_comment(story_a["id"], 999, "", "orphan reply")


def test_comments_on_missing_story():
    assert stories.list_comments(999) is None
    assert stories.add_comment(999, None, "", "hello") is None


def test_the_journey_round_trips():
    row = make(from_station="Hannover Hbf", to_station="Berlin Hbf",
               departure="2026-08-21T22:45", train="ICE 574")
    assert (row["from_station"], row["to_station"], row["departure"], row["train"]) == (
        "Hannover Hbf", "Berlin Hbf", "2026-08-21T22:45", "ICE 574")
    assert stories.list_stories("new", 10, 0)[0]["from_station"] == "Hannover Hbf"


def test_the_train_is_optional():
    # the replacement bus has no number, and the story is still about it
    assert make()["train"] == ""


def test_a_pre_journey_story_survives_the_rename(tmp_path, monkeypatch):
    """Stories written when a story named one station must keep it, as the
    origin - the column is renamed, not dropped and re-added empty."""
    import sqlite3
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as raw:
        raw.executescript(
            "CREATE TABLE stories (id INTEGER PRIMARY KEY, ts TEXT NOT NULL,"
            " station TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,"
            " author TEXT NOT NULL DEFAULT '');"
            "INSERT INTO stories (ts, station, title, text, author)"
            " VALUES ('2026-01-01T00:00:00+00:00', 'Kassel-Wilhelmshoehe',"
            " 'Old one', 'x', 'Max');"
        )
    monkeypatch.setattr(stories, "DB_PATH", path)
    row = stories.list_stories("new", 10, 0)[0]
    assert row["from_station"] == "Kassel-Wilhelmshoehe"
    assert row["to_station"] == "" and row["departure"] == ""


def test_problems_round_trip_in_offer_order():
    """Read back in the order the form offers them, not GROUP_CONCAT's - a
    story that was cancelled AND had no wifi should lead with the cancellation."""
    row = make(problems=["wifi", "cancelled", "delay"])
    assert row["problems"] == ["delay", "cancelled", "wifi"]
    assert stories.list_stories("new", 10, 0)[0]["problems"] == ["delay", "cancelled", "wifi"]


def test_a_story_without_problems_reads_as_an_empty_list():
    assert make()["problems"] == []


def test_unknown_codes_are_dropped_rather_than_stored():
    assert make(problems=["delay", "teleported"])["problems"] == ["delay"]


def test_other_text_is_kept_only_with_the_other_chip():
    assert make(problems=["other"], problem_other="doors froze")["problem_other"] \
        == "doors froze"
    # without the chip there is nothing for the text to specify
    assert make(problems=["delay"], problem_other="doors froze")["problem_other"] == ""


def test_the_api_and_the_store_agree_on_the_problem_codes():
    """main.ProblemCode validates what stories.PROBLEMS orders and labels; a
    code added to one and not the other is either a 422 or an unlabelled row."""
    from typing import get_args

    from app.main import ProblemCode

    assert set(get_args(ProblemCode)) == set(stories.PROBLEMS)


def test_deleting_a_story_takes_its_problems_with_it():
    from contextlib import closing

    row = make(problems=["delay", "wifi"])
    with closing(stories.connect()) as conn, conn:
        conn.execute("DELETE FROM stories WHERE id = ?", (row["id"],))
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM story_problems WHERE story_id = ?", (row["id"],)
        ).fetchone()["n"]
    assert left == 0


# --- editing and removal ----------------------------------------------------

def test_only_the_author_can_edit_a_story():
    row = make(author="Max", title="Old title")
    assert stories.edit_story(row["id"], "Meike", "Hijacked", "y" * 20) is None
    updated = stories.edit_story(row["id"], "Max", "New title", "y" * 20)
    assert (updated["title"], updated["edited"]) == ("New title", True)
    assert make()["edited"] is False  # an untouched story is not marked


def test_an_anonymized_story_cannot_be_claimed_by_an_empty_author():
    """delete_account blanks author to ''. An ownership check comparing '' to
    '' would hand every erased post to anyone with an empty session name."""
    row = make(author="")
    assert stories.edit_story(row["id"], "", "Mine now", "y" * 20) is None
    assert stories.delete_story(row["id"], "") is False


def test_a_story_nobody_replied_to_is_deleted_outright():
    row = make(author="Max", problems=["delay"])
    assert stories.delete_story(row["id"], "Max") is True
    assert stories.list_stories("new", 10, 0) == []


def test_a_story_with_comments_survives_as_an_empty_tombstone():
    """Cascading would take other people's comments with it, so the row stays
    - stripped of everything the author put in it."""
    row = make(author="Max", to_station="Berlin Hbf", train="ICE 1",
               problems=["delay"], title="Regret this")
    stories.add_comment(row["id"], None, "Meike", "happened to me too")
    assert stories.delete_story(row["id"], "Max") is True

    left = stories.list_stories("new", 10, 0)[0]
    assert left["deleted"] is True
    assert (left["title"], left["text"], left["author"]) == ("", "", "")
    assert (left["from_station"], left["train"], left["problems"]) == ("", "", [])
    assert stories.list_comments(row["id"])[0]["text"] == "happened to me too"


def test_a_removed_story_is_frozen():
    row = make(author="Max")
    stories.add_comment(row["id"], None, "Meike", "hi")
    stories.delete_story(row["id"], "Max")
    assert stories.edit_story(row["id"], "Max", "Back again", "y" * 20) is None
    assert stories.delete_story(row["id"], "Max") is False


def test_comment_votes_are_per_account_and_toggle():
    story = make()
    c = stories.add_comment(story["id"], None, "Max", "hi")
    max_id, meike_id = user("Max"), user("Meike")
    assert stories.set_comment_vote(c["id"], max_id, True) == {"score": 1, "voted": True}
    assert stories.set_comment_vote(c["id"], max_id, True) == {"score": 1, "voted": True}
    assert stories.set_comment_vote(c["id"], meike_id, True)["score"] == 2
    assert stories.set_comment_vote(c["id"], max_id, False) == {"score": 1, "voted": False}
    assert stories.list_comments(story["id"], meike_id)[0]["voted"] is True
    assert stories.list_comments(story["id"], max_id)[0]["voted"] is False


def test_a_leaf_comment_is_deleted_but_a_replied_one_is_tombstoned():
    story = make()
    parent = stories.add_comment(story["id"], None, "Max", "parent")
    leaf = stories.add_comment(story["id"], None, "Max", "leaf")
    stories.add_comment(story["id"], parent["id"], "Meike", "reply")

    assert stories.delete_comment(leaf["id"], "Max") is True
    assert stories.delete_comment(parent["id"], "Max") is True
    left = {c["id"]: c for c in stories.list_comments(story["id"])}
    assert leaf["id"] not in left
    assert left[parent["id"]]["deleted"] is True
    assert (left[parent["id"]]["text"], left[parent["id"]]["author"]) == ("", "")
    assert left[parent["id"]]["parent_id"] is None
    # the reply is still attached to the tombstone rather than orphaned
    assert any(c["parent_id"] == parent["id"] for c in left.values())


def test_only_the_author_can_edit_or_delete_a_comment():
    story = make()
    c = stories.add_comment(story["id"], None, "Max", "mine")
    assert stories.edit_comment(c["id"], "Meike", "yours now") is None
    assert stories.delete_comment(c["id"], "Meike") is False
    assert stories.edit_comment(c["id"], "Max", "reworded")["text"] == "reworded"


def test_a_removed_comment_cannot_be_voted_on():
    story = make()
    parent = stories.add_comment(story["id"], None, "Max", "parent")
    stories.add_comment(story["id"], parent["id"], "Meike", "reply")
    stories.delete_comment(parent["id"], "Max")
    assert stories.set_comment_vote(parent["id"], user("Max"), True) is None


# --- the tally board --------------------------------------------------------

def _backdate(story_id, ts):
    from contextlib import closing

    with closing(stories.connect()) as conn, conn:
        conn.execute("UPDATE stories SET ts = ? WHERE id = ?", (ts, story_id))


def test_problem_counts_cover_every_code_with_zeros():
    counts = stories.count_problems("month")
    assert set(counts) == set(stories.PROBLEMS)
    assert all(n == 0 for n in counts.values())


def test_problem_counts_count_reports_not_stories():
    make(problems=["delay", "wifi"])
    make(problems=["delay"])
    counts = stories.count_problems("month")
    assert (counts["delay"], counts["wifi"], counts["wc"]) == (2, 1, 0)


def test_problem_counts_respect_the_calendar_span():
    old = make(problems=["wc"])
    _backdate(old["id"], "2001-01-15T12:00:00+00:00")
    make(problems=["wc"])
    assert stories.count_problems("week")["wc"] == 1
    assert stories.count_problems("month")["wc"] == 1
    assert stories.count_problems("year")["wc"] == 1
    assert stories.count_problems("all")["wc"] == 2


def test_a_tombstoned_story_leaves_the_tally():
    row = make(problems=["crowding"], author="Max")
    stories.add_comment(row["id"], None, "Meike", "same here")  # forces a tombstone
    assert stories.count_problems("all")["crowding"] == 1
    assert stories.delete_story(row["id"], "Max") is True
    assert stories.count_problems("all")["crowding"] == 0


def tap(uid, code, vote=True, **leg):
    leg = {"from_station": "Hannover Hbf", **leg}
    return stories.set_report(uid, code, vote, **leg)


def test_a_tap_counts_like_a_story_report():
    uid = user("Max")
    make(problems=["delay"])
    assert tap(uid, "delay") is True
    assert stories.count_problems("week")["delay"] == 2
    assert stories.count_problems("all")["delay"] == 2
    assert stories.my_reports(uid) == ["delay"]


def test_a_tap_keeps_the_leg_it_was_made_on():
    from contextlib import closing

    uid = user("Max")
    tap(uid, "delay", to_station="Berlin Hbf", departure="2026-08-23T09:11", train="ICE 574")
    tap(uid, "delay", train="ICE 578")  # same day: one row, the last leg named
    with closing(stories.connect()) as conn:
        rows = conn.execute(
            "SELECT from_station, to_station, departure, train FROM problem_reports"
        ).fetchall()
    assert [tuple(r) for r in rows] == [("Hannover Hbf", "", "", "ICE 578")]


def test_the_free_text_belongs_to_the_other_tile_only():
    from contextlib import closing

    uid = user("Max")
    tap(uid, "other", problem_other="doors froze")
    tap(uid, "delay", problem_other="doors froze")
    with closing(stories.connect()) as conn:
        rows = conn.execute(
            "SELECT code, problem_other FROM problem_reports ORDER BY code"
        ).fetchall()
    assert [tuple(r) for r in rows] == [("delay", ""), ("other", "doors froze")]


def test_a_tap_is_once_per_account_and_day_and_toggles_off():
    uid = user("Max")
    tap(uid, "wc")
    tap(uid, "wc")
    assert stories.count_problems("month")["wc"] == 1
    assert tap(uid, "wc", False) is False
    assert stories.count_problems("month")["wc"] == 0
    assert stories.my_reports(uid) == []


def test_taps_from_two_accounts_both_count():
    tap(user("Max"), "wifi")
    tap(user("Meike"), "wifi")
    assert stories.count_problems("month")["wifi"] == 2


def test_my_reports_come_back_in_board_order():
    uid = user("Max")
    for code in ("wifi", "delay", "ac"):
        tap(uid, code)
    assert stories.my_reports(uid) == ["delay", "ac", "wifi"]


def test_an_unknown_tile_cannot_be_tapped():
    assert tap(user("Max"), "teleported") is None
    assert stories.count_problems("all") == dict.fromkeys(stories.PROBLEMS, 0)
