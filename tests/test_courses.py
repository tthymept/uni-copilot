"""Offline tests for client/courses.py: term picking, grouping, matching, pick()."""

import pytest

from conftest import RAW_COURSES
from uni_copilot.ntulearn_mcp.client.courses import (
    group_sites,
    match_courses,
    other_sites,
    pick_current_term,
    subject_code,
    term_order,
)


# Newest YYS1/YYS2 wins; other term names are ignored.
def test_pick_current_term_newest_semester():
    assert pick_current_term(["25S2", "26S1", "Non-curriculum", "25S1", ""]) == "26S1"


# NTULEARN_TERM (the override) beats whatever the data says.
def test_pick_current_term_override():
    assert pick_current_term(["26S1"], override="25S2") == "25S2"


# No semester-style term at all -> a clear error telling you to set NTULEARN_TERM.
def test_pick_current_term_none_found():
    with pytest.raises(ValueError, match="NTULEARN_TERM"):
        pick_current_term(["Non-curriculum"])


# "26S2" sorts after "26S1" and "25S2"; odd names sort lowest.
def test_term_order():
    assert term_order("26S2") > term_order("26S1") > term_order("25S2") > term_order("Non-curriculum")


# The code is the middle part of the displayId; odd formats keep the whole id.
def test_subject_code():
    assert subject_code("26S1-IE2108-LEC") == "IE2108"
    assert subject_code("TRAINING_01") == "TRAINING_01"


# Lecture + tutorial sites become one subject; unavailable and other-term sites are left out.
def test_group_sites():
    courses = group_sites(RAW_COURSES, "26S1")
    assert [c.code for c in courses] == ["XX1001", "YY2002"]
    assert [s.id for s in courses[0].sites] == ["_10_1", "_11_1"]
    assert [s.id for s in courses[1].sites] == ["_20_1"]
    assert courses[0].name == "XX1001 DATA THINGS (LEC)"  # shortest site name


# Non-semester sites (Non-curriculum, organizations) are listed separately.
def test_other_sites():
    assert [c.code for c in other_sites(RAW_COURSES)] == ["TRAINING_01"]


# Matching: exact code (any case), site id, then words from the name.
def test_match_courses():
    courses = group_sites(RAW_COURSES, "26S1")
    assert [c.code for c in match_courses(courses, "xx1001")] == ["XX1001"]
    assert [c.code for c in match_courses(courses, "_20_1")] == ["YY2002"]
    assert [c.code for c in match_courses(courses, "data things")] == ["XX1001"]
    assert [c.code for c in match_courses(courses, "xx1")] == ["XX1001"]  # part of a code
    assert match_courses(courses, "nothing like this") == []
    assert match_courses(courses, "  ") == []


# CoursesClient end to end on fake data: user id, term, list, and pick().
def test_courses_client(make_courses):
    client, http = make_courses()
    assert client.user_id() == "_u_1"
    assert client.current_term() == "26S1"
    result = client.list_courses()
    assert [c.code for c in result.courses] == ["XX1001", "YY2002"]
    assert any("unavailable" in n for n in result.notes)
    assert [c.code for c in client.pick(None)] == ["XX1001", "YY2002"]
    assert [c.code for c in client.pick("yy2002")] == ["YY2002"]


# pick() refuses no match and several matches, naming this term's codes.
def test_pick_errors(make_courses):
    client, _ = make_courses()
    with pytest.raises(ValueError, match="XX1001, YY2002"):
        client.pick("zzzz")
    with pytest.raises(ValueError, match="several"):
        client.pick("0")  # "0" appears in both subjects' names (XX1001, YY2002)


# Memberships are cached: asking twice makes only one memberships request.
def test_memberships_cached(make_courses):
    client, http = make_courses()
    client.list_courses()
    client.list_courses()
    assert sum("memberships" in c for c in http.calls) == 1
