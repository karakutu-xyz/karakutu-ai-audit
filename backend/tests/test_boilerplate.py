"""Invented same-site article families; pure paragraph and score arithmetic."""

from dataclasses import asdict
from time import perf_counter

import pytest

from audit.search.boilerplate import (
    BOILERPLATE_MIN_OWN_TOKENS,
    OWN_PER_SHARED_MIN,
    build_boilerplate,
    measure,
)
from audit.search.textmatch import (
    MOVED_THRESHOLD,
    PARTIAL_MIN,
    SIBLING_MAX,
    directional_containments,
    tokens,
)


HOST = "https://dusulke.example"
BODY = [
    "An invented council approved the amber reservoir programme after inspecting "
    "storage diagrams. Three replacement tanks will supply hillside homes during "
    "summer repairs. Temporary pipes will carry drinking water while technicians "
    "check pressure readings and install the revised pumping equipment.",
    "The fictional finance panel reserved separate funds for equipment checks "
    "and evening maintenance visits. Household tariffs follow quarterly cost "
    "tables while assistance discounts remain available. Public spending sheets "
    "will describe each purchase alongside the scheduled delivery dates.",
    "Residents requested written notices before planned interruptions along the "
    "upper streets. Staff promised telephone answers throughout the installation "
    "period. Volunteer observers will compare daily supply measurements with "
    "previous readings and submit their comments through the neighbourhood desk.",
    "Engineers displayed a miniature model showing the new pipeline route past "
    "the library courtyard. Protective barriers leave walking space beside the "
    "work area. Delivery vehicles must use the eastern entrance during unloading "
    "and depart before the afternoon pedestrian session begins.",
    "Councillors asked the inspection team to explain how winter frost might "
    "affect exposed valves. The team described insulated covers and monthly "
    "testing procedures. Repair crews will keep spare fittings nearby so minor "
    "faults cannot delay the wider construction schedule.",
    "A later meeting will review installation progress using illustrated charts "
    "prepared for public viewing. Written questions can reach the clerk before "
    "that session. The final completion report will list outstanding repairs "
    "and record whether the promised storage capacity was achieved.",
]
FOOTER = [
    "Subscribe through our invented reading service for daily features and "
    "weekly digests. Members can adjust notification choices through their "
    "account settings. Saved columns remain available in accessible formats "
    "with display preferences carried between supported reading devices.",
    "Our fictional publication guide explains corrections and licensing terms "
    "alongside archive navigation instructions. General feedback reaches the "
    "reader desk through the help form. Visit the newsletter menu for delivery "
    "options and information about upcoming editorial features.",
]
OTHER_A = [
    "An invented orchard workshop compares pear seedlings grown inside sheltered "
    "glasshouses. Visitors draw blossom shapes with coloured pencils while "
    "gardeners sort stored fruit into wooden boxes. The demonstration includes "
    "branch wrapping techniques and detailed notes about seasonal frost damage.",
]
OTHER_B = [
    "A fictional pottery exhibition displays blue ceramic bowls beside woven "
    "baskets. Students arrange painted tiles around miniature clay towers while "
    "instructors explain kiln temperatures. Guests receive illustrated cards "
    "describing glaze textures and return tomorrow for another creative session.",
]
REWRITE = [
    "The imaginary assembly authorised additional drinking supplies through "
    "containers beside the upper basin. Machinery will lift deliveries uphill "
    "before warmer weather arrives. A bypass arrangement should maintain "
    "household connections throughout construction under the revised financial plan.",
    "Customers receiving support retain reduced charges following the decision. "
    "Technical staff intend to release measurements showing stronger circulation "
    "across higher neighbourhoods. Financial disclosures should appear regularly "
    "and representatives will revisit implementation once the work has advanced.",
    "Inhabitants sought reassurance about shared standpipes during the works. "
    "Officials undertook to advertise shutdowns and accept questions after "
    "working hours. Documentation remains available for inspection while a "
    "future gathering considers how effectively the expanded network operates.",
]
SIBLING = [
    "Another invented report examines public reactions to the proposed drinking "
    "supply expansion. Shopkeepers discuss customer access near construction "
    "barriers while tenants consider notices about interrupted service. Interview "
    "responses describe expectations rather than reproducing the council decision.",
]


def prose(paragraphs):
    return "\n".join(paragraphs)


def family(candidate):
    """Candidate plus two independent live articles, all on the reserved host."""
    return {
        f"{HOST}/candidate": candidate,
        f"{HOST}/orchard": prose(OTHER_A + FOOTER),
        f"{HOST}/pottery": prose(OTHER_B + FOOTER),
    }


def paragraph_keys(paragraphs):
    return {" ".join(tokens(p)) for p in paragraphs}


def test_shared_footer_is_stripped_from_unrelated_articles():
    removed = prose(BODY + FOOTER)
    candidate = prose(OTHER_A + FOOTER)
    boilerplate = build_boilerplate(removed, family(candidate).values())
    assert boilerplate == paragraph_keys(FOOTER)
    measured = measure(removed, candidate, boilerplate)
    assert max(measured.cleaned) < 0.02
    assert measured.boilerplate_paragraphs == (2, 2)
    assert measured.raw == directional_containments(removed, candidate)


def test_verbatim_copy_survives_two_unrelated_footer_pages():
    removed = prose(BODY + FOOTER)
    boilerplate = build_boilerplate(removed, family(removed).values())
    assert boilerplate == paragraph_keys(FOOTER)
    assert measure(removed, removed, boilerplate).old_in_new >= 0.99


@pytest.mark.parametrize("independent_pages", [1, 2])
def test_two_copies_never_vote_the_article_body_into_boilerplate(
    independent_pages,
):
    removed = prose(BODY + FOOTER)
    copies = [
        removed + "\nInvented compact display preferences panel.",
        removed + "\nFictional small reading controls section.",
    ]
    others = [prose(OTHER_A + FOOTER), prose(OTHER_B + FOOTER)]
    boilerplate = build_boilerplate(
        removed, copies + others[:independent_pages],
    )
    assert not boilerplate & paragraph_keys(BODY)
    for copy in copies:
        assert measure(removed, copy, boilerplate).old_in_new >= 0.99


def test_partial_rewrite_does_not_strip_verbatim_copy(record_property):
    removed = prose(BODY + FOOTER)
    rewrite = prose(BODY[:3] + REWRITE + FOOTER)
    boilerplate = build_boilerplate(
        removed, [removed, rewrite, prose(OTHER_A + FOOTER)],
    )
    assert boilerplate == paragraph_keys(FOOTER)
    assert measure(removed, removed, boilerplate).old_in_new >= 0.99
    measured = measure(removed, rewrite, boilerplate)
    record_property("partial_rewrite_cleaned", measured.cleaned)
    assert PARTIAL_MIN <= measured.old_in_new < MOVED_THRESHOLD


def test_one_other_page_keeps_raw_measurement():
    removed = prose(BODY + FOOTER)
    candidate = prose(OTHER_A + FOOTER)
    boilerplate = build_boilerplate(removed, [candidate])
    assert not boilerplate
    measured = measure(removed, candidate, boilerplate)
    assert measured.cleaned == measured.raw
    assert measured.boilerplate_paragraphs == (0, 0)


def test_removed_entirely_boilerplate_uses_raw_fallback():
    removed = prose(FOOTER)
    candidate = prose(OTHER_A + FOOTER)
    boilerplate = build_boilerplate(removed, family(candidate).values())
    measured = measure(removed, candidate, boilerplate)
    assert measured.cleaned == measured.raw
    assert measured.raw_old_in_new == 1.0
    assert measured.boilerplate_paragraphs == (2, 2)
    both_empty = measure(removed, removed, boilerplate)
    assert both_empty.cleaned == both_empty.raw == (1.0, 1.0)


def test_candidate_entirely_boilerplate_measures_zero():
    removed = prose(BODY + FOOTER)
    boilerplate = build_boilerplate(removed, family(prose(FOOTER)).values())
    measured = measure(removed, prose(FOOTER), boilerplate)
    assert measured.cleaned == (0.0, 0.0)
    assert measured.boilerplate_paragraphs == (2, 2)
    assert measured.raw_new_in_old == 1.0
    # An empty page was unmeasurable before stripping, and stays so: no evidence, no call.
    assert measure(removed, "", boilerplate).cleaned == (None, None)


def test_measurement_returns_numbers_only():
    removed = prose(BODY + FOOTER)
    boilerplate = build_boilerplate(removed, family(removed).values())
    payload = asdict(measure(removed, removed, boilerplate))
    assert set(payload) == {
        "old_in_new", "new_in_old", "raw_old_in_new", "raw_new_in_old",
        "boilerplate_paragraphs",
    }
    assert all(isinstance(value, float) for key, value in payload.items()
               if key != "boilerplate_paragraphs")
    assert all(isinstance(count, int)
               for count in payload["boilerplate_paragraphs"])


def test_normalized_keys_and_short_lines():
    removed = prose(BODY + FOOTER)
    shared = FOOTER[0]
    a = prose(OTHER_A + [shared.upper(), "tiny shared label"])
    b = prose(OTHER_B + ["  " + shared.replace(" ", "  "), "tiny shared label"])
    boilerplate = build_boilerplate(removed, [a, b])
    assert boilerplate == paragraph_keys([shared])
    measured = measure(removed, a + "\n" + shared, boilerplate)
    assert measured.boilerplate_paragraphs == (1, 2)


SHORT_FOOTER = "Invented reader desk answers subscription questions every weekday"


@pytest.mark.parametrize("own_tokens", [29, 30])
def test_distinct_articles_need_minimum_own_tokens_on_each_side(own_tokens):
    a = " ".join(f"orchard{n}" for n in range(own_tokens))
    b = " ".join(f"pottery{n}" for n in range(30))
    boilerplate = build_boilerplate(
        SHORT_FOOTER, [a + "\n" + SHORT_FOOTER, b + "\n" + SHORT_FOOTER])
    assert bool(boilerplate) == (own_tokens >= BOILERPLATE_MIN_OWN_TOKENS)


@pytest.mark.parametrize("shared_tokens,expected", [(80, True), (81, False)])
def test_distinct_articles_need_own_text_relative_to_what_they_share(shared_tokens, expected):
    # Each page carries 40 tokens of its own; at OWN_PER_SHARED_MIN = 0.5 they may share
    # at most 80 tokens and still count as two different articles.
    assert OWN_PER_SHARED_MIN == 0.5
    shared = " ".join(f"furniture{n}" for n in range(shared_tokens))
    a = " ".join(f"orchard{n}" for n in range(40))
    b = " ".join(f"pottery{n}" for n in range(40))
    boilerplate = build_boilerplate("", [a + "\n" + shared, b + "\n" + shared])
    assert bool(boilerplate) == expected


@pytest.mark.parametrize("old,new", [("", ""), ("", "words remain here"),
                                     ("words remain here", "")])
def test_tokenless_sides_preserve_raw_no_evidence(old, new):
    assert measure(old, new, frozenset()).cleaned == (None, None)


def test_twenty_large_pages_are_handled_quickly():
    pages = [prose([
        " ".join(f"family{page}paragraph{line}token{word}" for word in range(20))
        for line in range(200)
    ] + FOOTER) for page in range(20)]
    removed = prose(BODY + FOOTER)
    started = perf_counter()
    boilerplate = build_boilerplate(removed, pages)
    measurements = [measure(removed, text, boilerplate) for text in pages]
    elapsed = perf_counter() - started
    assert boilerplate == paragraph_keys(FOOTER)
    assert all(m.cleaned == (0.0, 0.0) for m in measurements)
    assert elapsed < 2.0


def calibration():
    removed = prose(BODY + FOOTER)
    candidates = {
        "verbatim copy": removed,
        "copy with different furniture": prose(BODY + [
            "Fictional compact controls offer another reading layout.",
        ]),
        "shortened copy (first half)": prose(BODY[:3] + FOOTER),
        "rewrite": prose(BODY[:3] + REWRITE + FOOTER),
        "sibling": prose(SIBLING + FOOTER),
        "footer-only unrelated page": prose(OTHER_A + FOOTER),
    }
    return {
        name: measure(removed, candidate, build_boilerplate(
            removed, family(candidate).values(),
        )) for name, candidate in candidates.items()
    }


def test_calibration_separates_copies_siblings_and_footer(record_property):
    table = calibration()
    for name, measured in table.items():
        record_property(name, {"raw": measured.raw, "cleaned": measured.cleaned})
    for name in ("verbatim copy", "copy with different furniture"):
        assert table[name].old_in_new >= MOVED_THRESHOLD
    assert table["shortened copy (first half)"].old_in_new >= PARTIAL_MIN
    assert PARTIAL_MIN <= table["rewrite"].old_in_new < MOVED_THRESHOLD
    for name in ("sibling", "footer-only unrelated page"):
        assert max(table[name].cleaned) < SIBLING_MAX


TEASER_ONE = [
    "Also on this invented site: a fictional ferry timetable now lists extra evening "
    "crossings, a pretend bakery competition announced its judges, and an imaginary "
    "museum extended its weekend opening hours for visiting school groups.",
]
TEASER_TWO = [
    "More invented reading: a made-up chess club welcomed new junior members, a "
    "fictional cycling route gained painted lanes near the harbour, and a pretend "
    "library added a quiet study corner with borrowed laptops for evening visitors.",
]


def test_copies_with_their_own_teasers_never_vote():
    # Without the copy guard, two copies that each carry a long teaser of their own look
    # like two distinct articles, and their shared article body is voted into boilerplate.
    removed = prose(BODY + FOOTER)
    copies = [prose(BODY + TEASER_ONE + FOOTER), prose(BODY + TEASER_TWO + FOOTER)]
    for teaser in (TEASER_ONE, TEASER_TWO):
        assert len(tokens(teaser[0])) >= BOILERPLATE_MIN_OWN_TOKENS
    boilerplate = build_boilerplate(removed, copies + [prose(OTHER_A + FOOTER), prose(OTHER_B + FOOTER)])
    assert not boilerplate & paragraph_keys(BODY)
    assert boilerplate == paragraph_keys(FOOTER)
    for copy in copies:
        assert measure(removed, copy, boilerplate).old_in_new >= 0.99


def test_an_unmeasurable_page_stays_unmeasured():
    removed = prose(BODY + FOOTER)
    boilerplate = build_boilerplate(removed, [prose(OTHER_A + FOOTER), prose(OTHER_B + FOOTER)])
    assert boilerplate
    measured = measure(removed, "", boilerplate)
    assert measured.cleaned == (None, None)
